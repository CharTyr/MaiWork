"""coordinator.py 执行环境选择（docs/02 §9、docs/07 §11.4 + §11.1b、docs/09）。

主模型按计划 JSON 里的 "env" 自己选「本机隔离环境」还是「railway.new 一次性 VM」：

- 默认 local（快、便宜、能直接交付）；计划给 "env":"local" 走原有路径（不碰 railway）；
- 选 railway 且拿到机器 → 子 agent 工具集是 vm_run/vm_put_file/vm_read_file + 新加的
  vm_fetch_file（保留 read_file/write_file/list_files 写脚本、收成品），**不含** run_command
  等本机命令工具；结束（成功 / 失败 / 异常）一定 release；
- 选 railway 拿不到机器（配额用完 / 同时占用 / refused → acquire 返回 None）→ 回落 local，
  任务 env 字段写清「一次性机器拿不到，改在本机做：原因」；
- [environments] railway=false（或 railway 没就位）→ 计划提示词里根本没有 railway 这个选项；
- vm_fetch_file：目标路径过 LocalEnv.resolve 校验，越界（..）一律拒；成功拷回 artifacts/<任务ID>/；
- 剩余时间护栏：build_expires_at 距现在 < 10 分钟时，给子 agent 的每步结果附提醒。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.coordinator import Coordinator
from CharTyr_MaiWork.environments.local import LocalEnv
from CharTyr_MaiWork.environments.railway import Box
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.models import ModelError
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks
from CharTyr_MaiWork.tools import Tools
from CharTyr_MaiWork.tools_exec import register_exec_tools
from CharTyr_MaiWork.tools_railway import register_vm_tools
from CharTyr_MaiWork.workers import WorkerReport

pytestmark = pytest.mark.asyncio

GID = "900000001"
NOW = 1_790_000_000.0


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class ReplayChatResult:
    def __init__(self, text, tool_calls=None):
        self.text = text
        self.tool_calls = list(tool_calls or [])


class ModelsQueue:
    """假 Models：预设 chat 返回文本队列（按顺序回放）；每次 chat 记 (role, kwargs)。"""

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

    async def chat(self, role, messages, **kwargs):
        snap = [dict(m) for m in messages]
        self.calls.append((role, snap, kwargs))
        if not self.reply_queue:
            return ReplayChatResult("{}")
        item = self.reply_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ReplayChatResult(str(item))


class FakeWorkers:
    """假 Workers：记录被调时的工具名单，按队列返回 WorkerReport。"""

    def __init__(self, reports=None):
        self.reports = list(reports or [])
        self.calls: list[dict] = []
        self.before_return = None

    async def run(self, brief, *, group_id, tools, task_id="", actor="", max_steps=12,
                  output_schema=None, workspace=None):
        self.calls.append(
            {
                "brief": brief,
                "group_id": group_id,
                "tools": list(tools),
                "task_id": task_id,
                "actor": actor,
                "max_steps": max_steps,
                "output_schema": output_schema,
                "workspace": workspace,
            }
        )
        if self.before_return is not None:
            out = self.before_return()
            if hasattr(out, "__await__"):
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
        self.delivered.append(
            {"task_id": task_id, "kind": kind, "path": Path(path), "name": name, "note": note}
        )
        return 1

    def delivery_records(self, task_id):
        return []


class FakeOutbox:
    def __init__(self):
        self.enqueued: list[dict] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        self.enqueued.append(
            {"key": key, "group_id": group_id, "kind": kind,
             "payload": dict(payload or {}), "task_id": task_id}
        )
        return len(self.enqueued)


class FakeRailwayEnv:
    """假 RailwayEnv：控制 acquire 能不能拿到机器、记 release 次数、current_box。

    - give_box=True：acquire 返回一台 Box（到期时间可配），同时 current_box 有值；
    - give_box=False：acquire 返回 None（拿不到）。
    """

    def __init__(self, give_box: bool = True, *, remaining_s: float = 3600.0):
        self.give_box = give_box
        self.remaining_s = remaining_s
        self.acquire_calls: list[str] = []
        self.release_calls = 0
        self._box: Box | None = None
        self.get_calls: list[dict] = []
        self.run_calls: list[dict] = []

    async def acquire(self, job_id: str) -> Box | None:
        self.acquire_calls.append(str(job_id))
        if not self.give_box:
            return None
        self._box = Box(
            job_id=str(job_id),
            key_path=Path("/tmp/fake-railway-id"),
            expires_ts=clock.now() + self.remaining_s,
            preview_url="",
            claim_url="",
            key_dir=Path("/tmp"),
        )
        return self._box

    async def release(self, box: Box | None) -> None:
        if box is not None:
            self.release_calls += 1
            if self._box is box:
                self._box = None

    @property
    def current_box(self) -> Box | None:
        return self._box

    async def run(self, box: Any, command: str, *, timeout_s: int) -> Any:
        from CharTyr_MaiWork.environments.local import RunResult

        self.run_calls.append({"cmd": str(command), "timeout_s": int(timeout_s)})
        return RunResult(exit_code=0, stdout="ok", stderr="", ms=5, timed_out=False, oom=False)

    async def put(self, box: Any, local_path: Any, remote_path: str) -> None:
        pass

    async def get(self, box: Any, remote_path: str, local_path: Any) -> None:
        self.get_calls.append({"remote": str(remote_path), "local": str(local_path)})
        # 假 scp：在被拷回的本地路径落内容（模拟真拿到文件）
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("REMOTE-FETCHED", encoding="utf-8")


class _Profiles:
    def entries(self, group_id):
        return []


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch):
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
    def __init__(self, ws_root: Path, *, railway: bool = True):
        self._ws_root = Path(ws_root)
        self.environments = type(
            "Env",
            (),
            {
                "workspace_root": Path(ws_root),
                "max_parallel": 2,
                "local_mode": "direct",
                "run_as": "maiwork",
                "memory_max": "512M",
                "runtime_max_sec": 1800,
                "command_timeout_s": 300,
                "railway": railway,
                "railway_daily_max": 2,
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


def _build(
    *,
    mem_store: Store,
    settings,
    env,
    tools: Tools,
    tasks: Tasks,
    goals: Goals,
    models: ModelsQueue,
    workers: FakeWorkers,
    railway: FakeRailwayEnv | None = None,
    delivery: FakeDelivery | None = None,
    outbox: FakeOutbox | None = None,
) -> Coordinator:
    delivery = delivery or FakeDelivery()
    outbox = outbox or FakeOutbox()

    register_exec_tools(
        tools, env=env, host=None, get_settings=lambda: settings, session_of=lambda _g: "sess-1",
    )
    if railway is not None:
        # 模拟 app 接线：注册了 vm_*；给 local_env 才会注册 vm_fetch_file
        register_vm_tools(
            tools, get_box=lambda: railway.current_box, env=railway, local_env=env,
        )
    return Coordinator(
        mem_store,
        models,
        workers,
        tools,
        tasks,
        goals,
        delivery,
        outbox,
        env,
        _Profiles(),
        lambda: settings,
        railway=railway,
    )


def _create_task(tasks: Tasks, *, title="整理", req="做一页总结", criteria=(), **kw) -> str:
    kwargs = {"title": title, "req": req, "criteria": list(criteria), "source": "test"}
    kwargs.update(kw)
    return tasks.create(GID, **kwargs)


def _plan(criteria=None, deliver_kind="view", jobs=None, question=None, env="local", env_reason=""):
    if jobs is None:
        jobs = [{"brief": "做一页总结", "tools": ["write_file", "list_files", "run_command"]}]
    if criteria is None:
        criteria = ["包含链接"]
    return json.dumps(
        {
            "criteria": criteria,
            "deliver_kind": deliver_kind,
            "jobs": jobs,
            "question": question,
            "env": env,
            "env_reason": env_reason,
        },
        ensure_ascii=False,
    )


def _review(pass_=True, artifact="artifacts/T-1/index.html", review="看着不错", note="好了") -> str:
    return json.dumps(
        {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": note},
        ensure_ascii=False,
    )


async def _railway_tool_call(tools: Tools, name: str, args: dict, ctx: Any):
    return await tools.call(name, args, ctx)


# ---------------------------------------------------------------------------
# 1. 计划选 local → 走原有路径（不碰 railway，工具里有 run_command）
# ---------------------------------------------------------------------------


async def test_local_keeps_local_tools_and_no_acquire(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(env="local"), _review(pass_=True)])
    workers = FakeWorkers()
    railway = FakeRailwayEnv(give_box=True)

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, railway=railway,
    )
    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    # 没碰一次性机器
    assert railway.acquire_calls == []
    assert railway.release_calls == 0
    # 子 agent 拿到的还是本机命令工具（run_command），没被换成 vm_*
    assert workers.calls, "应该派过活"
    for call in workers.calls:
        assert "run_command" in call["tools"]
        assert not any(t.startswith("vm_") for t in call["tools"])


# ---------------------------------------------------------------------------
# 2. 选 railway 且拿到 → 工具含 vm_* + vm_fetch_file、不含 run_command；结束必 release
# ---------------------------------------------------------------------------


async def test_railway_success_uses_vm_tools_and_releases(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(
        replies=[_plan(env="railway", env_reason="要装一堆系统依赖"), _review(pass_=True)]
    )
    workers = FakeWorkers()
    railway = FakeRailwayEnv(give_box=True)

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, railway=railway,
    )
    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    # 申请过一台、且结束时释放了
    assert len(railway.acquire_calls) == 1
    assert railway.release_calls == 1
    # 子 agent 工具集：vm_* 齐 + vm_fetch_file；没有 run_command
    assert workers.calls, "应该派过活"
    for call in workers.calls:
        names = set(call["tools"])
        assert "vm_run" in names
        assert "vm_put_file" in names
        assert "vm_read_file" in names
        assert "vm_fetch_file" in names
        assert "run_command" not in names
        assert "start_process" not in names
        # 本机工作区文件工具保留（写脚本 / 收成品）
        assert "write_file" in names
        assert "read_file" in names
        assert "list_files" in names
    # brief 提醒成品要用 vm_fetch_file 拿回
    brief_all = "\n".join(c["brief"] for c in workers.calls)
    assert "vm_fetch_file" in brief_all


async def test_railway_releases_even_when_worker_raises(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """异常路径：子 agent 调用直接抛错，也得把那次拿到的机器 release 掉。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(env="railway"), ModelError("worker 阶段模型挂了")])
    workers = FakeWorkers(reports=[ModelError("worker 阶段模型挂了")])
    railway = FakeRailwayEnv(give_box=True)
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, railway=railway,
    )

    await coordinator.run_task(tid)
    # 至少申请过一次，且每次申请到的都被释放
    assert len(railway.acquire_calls) >= 1
    assert railway.release_calls == len(railway.acquire_calls)


# ---------------------------------------------------------------------------
# 3. 选 railway 拿不到 → 回落 local，env 写清原因
# ---------------------------------------------------------------------------


async def test_railway_unavailable_falls_back_to_local_with_reason(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(env="railway"), _review(pass_=True)])
    workers = FakeWorkers()
    railway = FakeRailwayEnv(give_box=False)  # 拿不到

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, railway=railway,
    )
    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    # 申请了但没拿到，拿不到就不存在 release
    assert len(railway.acquire_calls) == 1
    assert railway.release_calls == 0
    # 反而用回本机命令工具
    for call in workers.calls:
        assert "run_command" in call["tools"]
        assert not any(t.startswith("vm_") for t in call["tools"])
    # env 字段写清「拿不到，改在本机做」+ 原因
    env_text = str(tasks.get(tid)["env"] or "")
    assert "本机" in env_text
    assert ("拿不到" in env_text) or ("改在本机" in env_text) or ("改回" in env_text)


# ---------------------------------------------------------------------------
# 4. railway=false → 计划提示词里没有 railway 选项
# ---------------------------------------------------------------------------


async def test_railway_off_omits_railway_choice_in_prompt(
    mem_store: Store, settings, env, tools, tasks, goals, tmp_path
):
    off_settings = _Settings(tmp_path / "workspaces", railway=False)
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(env="local"), _review(pass_=True)])
    workers = FakeWorkers()

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    register_exec_tools(
        tools, env=env, host=None, get_settings=lambda: off_settings, session_of=lambda _g: "s",
    )
    coordinator = Coordinator(
        mem_store, models, workers, tools, tasks, goals,
        FakeDelivery(), FakeOutbox(), env, _Profiles(), lambda: off_settings,
        railway=None,
    )
    await coordinator.run_task(tid)

    plan_call = models.calls[0]
    prompt_text = "\n".join(str(m.get("content")) for m in plan_call[1])
    assert '"env"' in prompt_text  # 字段仍在
    assert "railway" not in prompt_text


async def test_railway_on_offers_railway_choice_in_prompt(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(env="local"), _review(pass_=True)])
    workers = FakeWorkers()
    railway = FakeRailwayEnv(give_box=True)

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, railway=railway,
    )
    await coordinator.run_task(tid)
    plan_call = models.calls[0]
    prompt_text = "\n".join(str(m.get("content")) for m in plan_call[1])
    assert "railway" in prompt_text


# ---------------------------------------------------------------------------
# 5. vm_fetch_file：越界拒、成功拷回 artifacts
# ---------------------------------------------------------------------------


async def test_vm_fetch_file_rejects_out_of_bounds(
    mem_store: Store, settings, env, tools, tasks, goals
):
    from CharTyr_MaiWork.tools import ToolContext

    railway = FakeRailwayEnv(give_box=True)
    await railway.acquire("T-9")
    register_vm_tools(tools, get_box=lambda: railway.current_box, env=railway, local_env=env)

    ws = env.workspace("g" + GID)
    ctx = ToolContext(group_id=GID, task_id="T-9", actor="子 agent", role="worker", workspace=ws)
    r = await _railway_tool_call(
        tools, "vm_fetch_file", {"remote_path": "/app/out.html", "local_name": "../evil.html"}, ctx
    )
    assert not r.ok


async def test_vm_fetch_file_copies_into_artifacts(
    mem_store: Store, settings, env, tools, tasks, goals
):
    from CharTyr_MaiWork.tools import ToolContext

    railway = FakeRailwayEnv(give_box=True)
    await railway.acquire("T-7")
    register_vm_tools(tools, get_box=lambda: railway.current_box, env=railway, local_env=env)

    ws = env.workspace("g" + GID)
    ctx = ToolContext(group_id=GID, task_id="T-7", actor="子 agent", role="worker", workspace=ws)
    r = await _railway_tool_call(
        tools, "vm_fetch_file", {"remote_path": "/app/result.html", "local_name": "result.html"}, ctx
    )
    assert r.ok, r.error
    target = env.resolve("g" + GID, "artifacts/T-7/result.html")
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "REMOTE-FETCHED"


# ---------------------------------------------------------------------------
# 6. 剩余时间护栏：<10 分钟时，子 agent 每步结果附提醒
# ---------------------------------------------------------------------------


async def test_near_expiry_appends_reminder_to_vm_tool_output(
    mem_store: Store, settings, env, tools, tasks, goals
):
    from CharTyr_MaiWork.tools import ToolContext

    railway = FakeRailwayEnv(give_box=True, remaining_s=300.0)  # 只剩 5 分钟（<10）
    await railway.acquire("T-5")
    register_vm_tools(tools, get_box=lambda: railway.current_box, env=railway, local_env=env)

    ws = env.workspace("g" + GID)
    ctx = ToolContext(group_id=GID, task_id="T-5", actor="子 agent", role="worker", workspace=ws)
    r = await _railway_tool_call(tools, "vm_run", {"command": "ls /app"}, ctx)
    assert r.ok
    assert "到期" in r.output


async def test_expired_vm_tools_fail_chinese(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """已经过 60 分钟窗口（剩余 ≤0）→ vm 工具一律失败，提示别再用这台机器。"""
    from CharTyr_MaiWork.tools import ToolContext

    railway = FakeRailwayEnv(give_box=True, remaining_s=-5.0)  # 已过期
    await railway.acquire("T-6")
    register_vm_tools(tools, get_box=lambda: railway.current_box, env=railway, local_env=env)

    ws = env.workspace("g" + GID)
    ctx = ToolContext(group_id=GID, task_id="T-6", actor="子 agent", role="worker", workspace=ws)
    for name, args in (
        ("vm_run", {"command": "ls"}),
        ("vm_fetch_file", {"remote_path": "/app/x.html", "local_name": "x.html"}),
    ):
        r = await _railway_tool_call(tools, name, args, ctx)
        assert not r.ok
        assert "到期" in r.error or "到点" in r.error
