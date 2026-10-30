"""派活管道三处修复（2026-10 线上 T-1/T-2/T-7 复盘）：

1. 验收模型没给结论不再直接判死：先在验收内强制重试（追加「请只输出 JSON 结论」），
   仍不行走「验收不通过」的退回机制（计入 _MAX_ATTEMPTS），不白烧 token。
2. 开工前能力自检：验收标准/交接单要「下载/本地存图/跑代码」但 worker 工具没执行
   能力时，环境允许就自动补 run_command，并在任务事件里留一句大白话；环境不允许就
   不瞎补。
3. 群画像不记群友对机器人下的指令原文（/今日运势、@机器人+指令）：提示词写清楚，
   _apply_ops 顺手把以 / 开头的条目整条滤掉。

用真 Store + 假模型队列（和 test_coordinator.py / test_profile_refresh.py 同一套桩），
不调真网络/模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.coordinator import Coordinator, job_needs_exec_capability
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport

pytestmark = pytest.mark.asyncio

GID = "900000001"
NOW = 1_790_000_000.0


class ReplayChatResult:
    def __init__(self, text, tool_calls=None):
        self.text = text
        self.tool_calls = list(tool_calls or [])


class ModelsQueue:
    """假 Models：预设 chat 返回文本队列（按顺序回放）；空队列回 "{}"。日志记 (role, messages, kwargs)。"""

    def __init__(self, replies=None, ready=True):
        self.reply_queue = list(replies or [])
        self.calls: list[tuple[str, list[dict], dict]] = []
        self._ready = ready

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self):
                return ready

        return _S()

    async def chat(self, role=None, messages=None, **kwargs):
        snap = [dict(m) for m in messages]
        self.calls.append((role, snap, kwargs))
        if not self.reply_queue:
            return ReplayChatResult("{}")
        item = self.reply_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ReplayChatResult(str(item))


class FakeWorkers:
    def __init__(self, reports=None):
        self.reports = list(reports or [])
        self.calls: list[dict] = []
        self.before_return = None

    async def run(self, brief, *, group_id, tools, task_id="", actor="", max_steps=12,
                  output_schema=None, workspace=None, system_extra="", artifact_scope=None,
                  write_scope=None):
        import asyncio as _aio
        self.calls.append({"brief": brief, "tools": list(tools), "task_id": task_id})
        if self.before_return is not None:
            out = self.before_return()
            if _aio.iscoroutine(out):
                await out
        if not self.reports:
            return WorkerReport(ok=True, summary="做完了", evidence=["e1"])
        item = self.reports.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeDelivery:
    def __init__(self):
        self.delivered: list[dict] = []

    async def deliver_task(self, task_id, *, kind, path, name, note):
        self.delivered.append({"task_id": task_id, "kind": kind, "path": Path(path), "name": name, "note": note})
        return 1

    def delivery_records(self, task_id):
        return []


class FakeOutbox:
    def __init__(self):
        self.enqueued: list[dict] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        self.enqueued.append({"key": key, "group_id": group_id, "kind": kind,
                              "payload": dict(payload or {}), "task_id": task_id})
        return len(self.enqueued)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path):
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    yield store
    store.close()


class _Settings:
    def __init__(self, ws_root: Path):
        self._ws_root = Path(ws_root)
        self.environments = type(
            "Env", (), {
                "workspace_root": Path(ws_root), "max_parallel": 2, "local_mode": "direct",
                "run_as": "maiwork", "memory_max": "512M", "runtime_max_sec": 1800,
                "command_timeout_s": 300,
            },
        )()
        self.delivery = type("Dlv", (), {"quiet_hours": "23:00-08:00"})()

    def workspace_of(self, group_id: str) -> str:
        return f"g{group_id}"


@pytest.fixture
def settings(tmp_path):
    return _Settings(tmp_path / "workspaces")


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def tools(mem_store):
    return Tools(mem_store)


@pytest.fixture
def tasks(mem_store, settings):
    return Tasks(mem_store, lambda: settings)


@pytest.fixture
def goals(mem_store, settings):
    return Goals(mem_store, lambda: settings)


class _Profiles:
    def entries(self, group_id):
        return []


def _build(*, mem_store, settings, env, tools, tasks, goals, models, workers,
           delivery=None, outbox=None) -> Coordinator:
    delivery = delivery or FakeDelivery()
    outbox = outbox or FakeOutbox()

    def _session_of(gid):
        return "sess-1"

    register_exec_tools(tools, env=env, host=None, get_settings=lambda: settings, session_of=_session_of)
    return Coordinator(
        mem_store, models, workers, tools, tasks, goals, delivery, outbox, env,
        _Profiles(), lambda: settings,
    )


def _create_task(tasks: Tasks, *, title="整理", req="做一页总结", criteria=(), **kw) -> str:
    kwargs = {"title": title, "req": req, "criteria": list(criteria), "source": "test"}
    kwargs.update(kw)
    return tasks.create(GID, **kwargs)


def _plan(criteria=None, deliver_kind="view", jobs=None, question=None) -> str:
    if jobs is None:
        jobs = [{"brief": "做一页总结", "tools": ["write_file", "list_files"]}]
    if criteria is None:
        criteria = ["包含链接"]
    return json.dumps(
        {"criteria": criteria, "deliver_kind": deliver_kind, "jobs": jobs, "question": question},
        ensure_ascii=False,
    )


def _review(pass_=True, artifact="artifacts/T-1/index.html", review="看着不错", note="好了") -> str:
    return json.dumps(
        {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": note},
        ensure_ascii=False,
    )


async def _write_artifact(workers, env, tasks, tid):
    async def _hook():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    workers.before_return = _hook


# ---------------------------------------------------------------------------
# 修复 1：验收没给结论 → 验收内强制重试；仍不行退回队列，不直接 failed
# ---------------------------------------------------------------------------


async def test_review_inconclusive_retries_then_requeues(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """第 1 次尝试：验收 6 轮全回非 JSON，2 次强制重试也非 JSON → 退回 queued（不是 failed），
    验收意见写清「验收模型没给结论」。第 2 次尝试验收正常 → completed。"""
    tid = _create_task(tasks)
    not_json = "我再看一下，稍等（不是 JSON）"
    models = ModelsQueue(
        replies=[
            _plan(),
            # 6 轮验收全是非 JSON（_REVIEW_TOOL_LIMIT=6），加 2 次强制重试也非 JSON
            not_json, not_json, not_json, not_json, not_json, not_json,
            not_json, not_json,
            # 第 2 次尝试
            _plan(),
            _review(pass_=True),
        ]
    )
    workers = FakeWorkers()
    await _write_artifact(workers, env, tasks, tid)
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=models, workers=workers,
    )
    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "completed", task["status"]

    rows = mem_store.read().execute(
        "SELECT n, status, review FROM attempts WHERE task_id=? ORDER BY n", (tid,)
    ).fetchall()
    assert len(rows) == 2
    # docs/22 §3.3：验收没结论记 inconclusive（不是 failed），不算打回、但仍占一次尝试
    assert rows[0]["status"] == "inconclusive"
    assert "没给结论" in rows[0]["review"]
    assert rows[1]["status"] == "passed"

    # 退回队列的事件 reason 里写清「验收模型没给结论」
    evs = mem_store.read().execute(
        "SELECT payload FROM events WHERE kind='task.queued' AND entity_id=?", (tid,)
    ).fetchall()
    assert any("没给结论" in str(e["payload"]) for e in evs)

    # 不能走 _fail_with_err（不能报故障、不能发 error 通知）
    err_msgs = [e for e in (coordinator._outbox.enqueued)
                if e["kind"] == "text" and e["payload"].get("push_kind") == "error"]
    assert err_msgs == []


async def test_review_inconclusive_second_forced_try_recovers(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """验收 6 轮非 JSON，但第 1 次强制重试就拿到合法 JSON → 不需要退回队列，直接通过。"""
    tid = _create_task(tasks)
    not_json = "（在思考，不是 JSON）"
    models = ModelsQueue(
        replies=[
            _plan(),
            not_json, not_json, not_json, not_json, not_json, not_json,
            _review(pass_=True),  # 强制重试第 1 次给出 JSON
        ]
    )
    workers = FakeWorkers()
    await _write_artifact(workers, env, tasks, tid)
    delivery = FakeDelivery()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, delivery=delivery,
    )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    rows = mem_store.read().execute(
        "SELECT n, status FROM attempts WHERE task_id=? ORDER BY n", (tid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "passed"
    assert len(delivery.delivered) == 1


async def test_review_inconclusive_exhausts_attempts_marks_failed(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """3 次尝试验收全没结论 → 走和「验收不通过」一样的上限：最终 failed，不无限循环。"""
    tid = _create_task(tasks)
    not_json = "（还不是 JSON）"
    replies = []
    for _ in range(3):  # _MAX_ATTEMPTS = 3
        replies.append(_plan())
        replies.extend([not_json] * 8)  # 6 轮工具 + 2 次强制重试
    models = ModelsQueue(replies=replies)
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=["e"])] * 3)
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, outbox=outbox,
    )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["status"] == "failed"
    # 三次尝试都记 inconclusive（没结论不当打回，但次数照样用完）
    rows = mem_store.read().execute(
        "SELECT status FROM attempts WHERE task_id=? ORDER BY n", (tid,)
    ).fetchall()
    assert [r["status"] for r in rows] == ["inconclusive", "inconclusive", "inconclusive"]
    # 「没做成」固定话发到群里（和验收不通过同一路径），不是 error 故障推送
    fail_msgs = [e for e in outbox.enqueued
                 if e["kind"] == "text" and "没做成" in str(e["payload"].get("text") or "")]
    assert len(fail_msgs) == 1


# ---------------------------------------------------------------------------
# 修复 2：开工前能力自检——要本地存图/下载但没执行工具 → 自动补 run_command
# ---------------------------------------------------------------------------


async def test_job_needs_exec_capability_keywords():
    assert job_needs_exec_capability("把图片下载下来保存到本地 artifacts/T-1/") is True
    assert job_needs_exec_capability("把 5 张封面图都下载到本地，验收要看到文件") is True
    assert job_needs_exec_capability("把结果打包成 zip 压缩包") is True
    assert job_needs_exec_capability("跑一下代码确认能运行") is True
    assert job_needs_exec_capability("写一页纯文字总结") is False
    assert job_needs_exec_capability("") is False


async def test_exec_capability_autofills_run_command(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """计划 job brief 要「下载图片保存到本地」但 tools 只给了 write_file → 开工时
    FakeWorkers 收到的 tools 里补上 run_command，并在任务事件里留一句大白话。"""
    tid = _create_task(tasks)
    jobs = [{
        "brief": "把 3 张大 A 走势图下载下来保存到本地 artifacts/T-1/，再做一页 index.html",
        "tools": ["write_file", "read_file", "list_files"],
    }]
    models = ModelsQueue(replies=[_plan(jobs=jobs), _review(pass_=True)])
    workers = FakeWorkers()
    await _write_artifact(workers, env, tasks, tid)
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers,
    )
    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert len(workers.calls) == 1
    assert "run_command" in workers.calls[0]["tools"]
    # 任务事件里有一句大白话说明（不带模型名词）
    evs = mem_store.read().execute(
        "SELECT kind, payload FROM events WHERE entity_id=? ORDER BY id", (tid,)
    ).fetchall()
    blob = "\n".join(f"{e['kind']} {e['payload']}" for e in evs)
    assert "run_command" in blob or "跑命令" in blob or "执行" in blob


async def test_exec_capability_not_needed_no_autofill(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """brief 只是写字 → 不补 run_command，tools 原样给 worker。"""
    tid = _create_task(tasks)
    jobs = [{"brief": "写一页纯文字总结，写到 artifacts/T-1/index.html",
             "tools": ["write_file", "list_files"]}]
    models = ModelsQueue(replies=[_plan(jobs=jobs), _review(pass_=True)])
    workers = FakeWorkers()
    await _write_artifact(workers, env, tasks, tid)
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers,
    )
    await coordinator.run_task(tid)
    assert len(workers.calls) == 1
    assert "run_command" not in workers.calls[0]["tools"]


async def test_exec_capability_kept_when_job_already_has_it(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """job tools 里已经要了 run_command → 不重复加，原样。"""
    tid = _create_task(tasks)
    jobs = [{"brief": "下载图片保存到本地，再做成页面",
             "tools": ["write_file", "run_command"]}]
    models = ModelsQueue(replies=[_plan(jobs=jobs), _review(pass_=True)])
    workers = FakeWorkers()
    await _write_artifact(workers, env, tasks, tid)
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers,
    )
    await coordinator.run_task(tid)
    assert len(workers.calls) == 1
    assert workers.calls[0]["tools"].count("run_command") == 1


# ---------------------------------------------------------------------------
# 修复 3：群画像不记「群友对机器人下的指令原文」
# ---------------------------------------------------------------------------

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.profile import Profiles

import sys as _sys
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TESTS_DIR))
from fakes import FakeHost, FakeModelsQueue  # noqa: E402

T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()


def _profile_settings(tmp_path: Path):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "focus": {"personal_profile": True},
        "environments": {"workspace_root": str(tmp_path / "wsroot")},
        "profile": {"backfill_days": 30, "backfill_max_messages": 1500, "batch_messages": 3},
    }
    settings_obj, problems = load_settings(raw)
    assert not problems
    return settings_obj


def _msg(mid: str, ts: float, user: str = "u1", name: str = "阿一", *, text: str = "你好") -> Msg:
    return Msg(id=mid, ts=ts, user_id=user, user_name=name, text=text,
               is_bot=False, is_at=False, is_picture=False, reply_to="")


@pytest.fixture
def profile_clock(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(clock, "now", lambda: T0)
    return T0


async def test_profile_prompt_forbids_bot_command_verbatim(tmp_path: Path, profile_clock):
    """提示词里明确：不要把群友对机器人下的指令原文记进条目。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        models = FakeModelsQueue(replies=["没有变化"])
        p = Profiles(store, host, models, lambda: _profile_settings(tmp_path))
        await p.tick(GID)
        prompt_text = str(models.calls[0][1])
        assert "指令原文" in prompt_text
        assert "娱乐指令" in prompt_text
    finally:
        store.close()


async def test_profile_drops_slash_command_entries(tmp_path: Path, profile_clock):
    """模型回放的画像条目以 / 开头（指令原文）→ 整条滤掉；概括说法正常入库。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"消息{i}") for i in range(3)])
        reply = "\n".join([
            "新增 | 约定和说法 | /今日运势 | 1",
            "新增 | 约定和说法 | /今日猪猪 | 2",
            "新增 | 约定和说法 | 群里常用机器人的娱乐指令 | 1,2",
        ])
        models = FakeModelsQueue(replies=[reply])
        p = Profiles(store, host, models, lambda: _profile_settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        texts = [e["text"] for e in p.entries(GID)]
        assert "/今日运势" not in texts
        assert "/今日猪猪" not in texts
        assert "群里常用机器人的娱乐指令" in texts
    finally:
        store.close()


async def test_profile_normal_entries_not_dropped(tmp_path: Path, profile_clock):
    """正常的 convention 条目（不以 / 开头）不受影响。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"消息{i}") for i in range(3)])
        reply = "新增 | 约定和说法 | 周五是分享夜 | 1"
        models = FakeModelsQueue(replies=[reply])
        p = Profiles(store, host, models, lambda: _profile_settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        texts = [e["text"] for e in p.entries(GID)]
        assert "周五是分享夜" in texts
    finally:
        store.close()
