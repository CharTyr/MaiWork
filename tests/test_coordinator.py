"""coordinator.py 单元测试：主模型协调器（docs/02 §7、docs/07 §11.4）。

用真 Store + 真 Tasks/Goals + FakeModelsQueue（按顺序回放计划/验收 JSON）+
假 Workers（可设返回 WorkerReport、可在返回前「取消任务」模拟晚到）+
direct 模式 LocalEnv（tmp_path 当 workspace_root，假 worker 在工作区真的写文件）+
假 Delivery/Outbox（记录调用）。

红线对应用例：
- 取消后晚到结果不交付、状态保持 cancelled；
- 需求改版后旧结果不冒充（accept_result False）；
- 同工作区同一时刻只有一个主模型回合（asyncio.Lock per workspace）；
- 验收 pass 且非 text 时 artifact 必须真实存在，不存在视为不通过；
- 主模型验收的工具调用落 tool_calls（actor=主模型）；
- 子 agent 不能宣布完成（这里：只有 coordinator 把任务改成 completed）；
- ModelError / 执行环境故障 → failed + report_error。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport

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
        # 深拷贝 messages 快照
        snap = [dict(m) for m in messages]
        self.calls.append((role, snap, kwargs))
        if not self.reply_queue:
            return ReplayChatResult("{}")
        item = self.reply_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ReplayChatResult(str(item))


class FakeWorkers:
    """假 Workers：按队列返回 WorkerReport；可在返回前执行一段钩子（如「取消任务」）。"""

    def __init__(self, reports=None):
        self.reports = list(reports or [])
        self.calls: list[dict] = []
        self.before_return = None  # async callable()：返回前执行（用来模拟晚到）

    async def run(self, brief, *, group_id, tools, task_id="", actor="", max_steps=12,
                  output_schema=None, workspace=None, system_extra="", artifact_scope=None):
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
                "system_extra": system_extra,
                "artifact_scope": artifact_scope,
            }
        )
        if self.before_return is not None:
            out = self.before_return()
            if asyncio.iscoroutine(out):
                await out
        if not self.reports:
            return WorkerReport(ok=True, summary="做完了", evidence=["e1"])
        item = self.reports.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeDelivery:
    """假 Delivery：记录 deliver_task 调用。"""

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
    """假 Outbox：记录 enqueue + report_error 相关调用。"""

    def __init__(self):
        self.enqueued: list[dict] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        self.enqueued.append(
            {
                "key": key,
                "group_id": group_id,
                "kind": kind,
                "payload": dict(payload or {}),
                "task_id": task_id,
            }
        )
        return len(self.enqueued)


# ---------------------------------------------------------------------------
# 通用 fixture / 工具
# ---------------------------------------------------------------------------


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
    """最小 Settings：workspace_of、environments（max_parallel / workspace_root）、delivery。"""

    def __init__(self, ws_root: Path):
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
    delivery: FakeDelivery | None = None,
    outbox: FakeOutbox | None = None,
) -> Coordinator:
    delivery = delivery or FakeDelivery()
    outbox = outbox or FakeOutbox()

    def _session_of(gid):
        return "sess-1"

    register_exec_tools(
        tools,
        env=env,
        host=None,
        get_settings=lambda: settings,
        session_of=_session_of,
    )
    # 主模型只读工具（inspect_file / inspect_files）已经由 register_exec_tools 注册。
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


# ---------------------------------------------------------------------------
# 正常一轮 → completed + deliver_task(view)
# ---------------------------------------------------------------------------


async def test_run_task_does_not_start_after_group_removed(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    settings.is_served = lambda group_id: False
    models = ModelsQueue(replies=[_plan()])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["status"] == "queued"
    assert models.calls == []


async def test_review_rejects_artifact_from_other_task_directory(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    ws = env.workspace(tasks.get(tid)["workspace"])
    misplaced = ws / "artifacts" / "T-other" / "index.html"
    misplaced.parent.mkdir(parents=True, exist_ok=True)
    misplaced.write_text("<html>不是本任务成品</html>", encoding="utf-8")
    models = ModelsQueue(replies=[_review(artifact="artifacts/T-other/index.html")])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=models, workers=FakeWorkers(),
    )
    review = await coordinator._review(tasks.get(tid), {"criteria": ["真实产物"], "deliver_kind": "view"},
                                        "完成", ["evidence"], [])
    assert review["pass"] is False
    assert "本任务" in review["review"]


async def test_handle_passed_missing_artifact_is_not_completed(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    tasks.transition(tid, "running")
    tasks.transition(tid, "reviewing")
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(),
    )
    await coordinator._handle_passed(
        tid, GID, tasks.get(tid)["workspace"], {"deliver_kind": "file"},
        {"review": "通过", "note": "好了", "artifact": "artifacts/T-other/missing.txt"},
    )
    assert tasks.get(tid)["status"] != "completed"


async def test_run_task_success_view_delivered(
    mem_store: Store, settings, env, tools, tasks, goals, tmp_path
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    outbox = FakeOutbox()

    # 假 worker 在工作区真的写出 artifacts/T-1/index.html
    async def _write_artifact():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write_artifact

    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        delivery=delivery,
        outbox=outbox,
    )

    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "completed"
    assert task["delivery_kind"] == "view"
    assert len(delivery.delivered) == 1
    d = delivery.delivered[0]
    assert d["task_id"] == tid
    assert d["kind"] == "view"
    assert d["name"]
    assert d["note"]


# ---------------------------------------------------------------------------
# 验收 pass 但文件不存在 → 视为不通过重试
# ---------------------------------------------------------------------------


async def test_review_pass_but_artifact_missing_counts_as_fail(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    # 第 1 轮：计划 + 验收 pass 但 artifact 不存在 → 不通过
    # 第 2 轮：计划 + 验收 pass 且 artifact 存在（worker 写出来） → 通过
    models = ModelsQueue(
        replies=[
            _plan(),
            _review(pass_=True, artifact="artifacts/T-1/index.html"),
            _plan(),
            _review(pass_=True, artifact="artifacts/T-1/index.html"),
        ]
    )
    workers = FakeWorkers(reports=[
        WorkerReport(ok=True, summary="做完了", evidence=["e1"]),
        WorkerReport(ok=True, summary="做完了", evidence=["e1"]),
    ])

    writes = {"n": 0}

    async def _maybe_write():
        writes["n"] += 1
        if writes["n"] < 2:
            return  # 第 1 次故意不写 → 验收 pass 但文件不存在
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _maybe_write  # 只在第 1 次生效，第 2 次需要在 plan 后再设

    delivery = FakeDelivery()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        delivery=delivery,
        outbox=outbox,
    )
    # 第 1 次跑前挂 hook
    workers.before_return = _maybe_write
    await coordinator.run_task(tid)

    # 第 1 轮失败后 coordinator 内部立刻再跑一轮；为第 2 轮挂 hook
    # 协调器内部同一锁内顺序执行，所以我们在外再触发一次不行；
    # 预先把 hook 设成「看次数」即可（上面 _maybe_write 内部按次数决定写不写）。
    # run_task 一次会把第 2 轮也跑完（queued → running），所以直接断言：

    task = tasks.get(tid)
    # 第 2 轮 worker 写出了文件、验收通过 → completed
    assert task["status"] == "completed"
    rows = mem_store.read().execute(
        "SELECT n, status, review FROM attempts WHERE task_id=? ORDER BY n", (tid,)
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed"  # 第 1 次虽然 pass 但文件不存在 → failed
    assert rows[1]["status"] == "passed"


# ---------------------------------------------------------------------------
# 3 次不通过 → failed + 固定话
# ---------------------------------------------------------------------------


async def test_three_failures_marks_failed_and_enqueues_fixed_line(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    bad_review = _review(pass_=False, review="缺一块关键内容")
    models = ModelsQueue(replies=[_plan(), bad_review] * 3)
    workers = FakeWorkers(
        reports=[WorkerReport(ok=True, summary="做完了", evidence=["e"])] * 3
    )
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        outbox=outbox,
    )
    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "failed"
    texts = [e for e in outbox.enqueued if e["kind"] == "text"]
    assert len(texts) == 1
    text = texts[0]["payload"]["text"]
    assert "没做成" in text
    assert "缺一块关键内容" in text


# ---------------------------------------------------------------------------
# question → waiting_input + 群里 @ 发起人问一句
# ---------------------------------------------------------------------------


async def test_question_moves_to_waiting_input_and_asks(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, requester_id="10001", requester_name="阿明")
    models = ModelsQueue(replies=[_plan(question="需要确认：要简体还是繁体？")])
    workers = FakeWorkers()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        outbox=outbox,
    )
    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "waiting_input"
    assert task["question"] == "需要确认：要简体还是繁体？"
    # 有一次发件（push_kind=status）
    texts = [e for e in outbox.enqueued if e["kind"] == "text"]
    assert len(texts) == 1
    assert texts[0]["payload"].get("push_kind") == "status"
    assert "阿明" in texts[0]["payload"]["text"]
    # worker 不该被调
    assert workers.calls == []


async def test_question_asks_current_requester_name(mem_store: Store, settings, env, tools, tasks, goals):
    """提问 @ 发起人用名册当前名（按 requester_id），不是老快照。"""
    from CharTyr_MaiWork.maiwork import members

    tid = _create_task(tasks, requester_id="10001", requester_name="阿明")
    with mem_store.tx() as conn:
        members.record(conn, GID, "10001", "阿明改了名", 1e10)
    models = ModelsQueue(replies=[_plan(question="需要确认：要简体还是繁体？")])
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=outbox,
    )
    await coordinator.run_task(tid)
    text = [e for e in outbox.enqueued if e["kind"] == "text"][0]["payload"]["text"]
    assert "@阿明改了名 " in text and "10001" not in text


# ---------------------------------------------------------------------------
# 执行中被取消 → 晚到结果不交付、状态保持 cancelled
# ---------------------------------------------------------------------------


async def test_cancel_during_execution_late_result_not_delivered(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan()])
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=["e"])])

    # 模拟「子 agent 快返回前，任务被用户取消」
    def _cancel():
        tasks.transition(tid, "cancelled", reason="用户取消")

    workers.before_return = _cancel
    delivery = FakeDelivery()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        delivery=delivery,
    )
    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "cancelled"
    assert delivery.delivered == []


# ---------------------------------------------------------------------------
# 需求改版后旧结果不冒充
# ---------------------------------------------------------------------------


async def test_revise_during_execution_old_result_stale(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan()])
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=["e"])])

    def _revise():
        # worker 跑途中，别人把需求改了：revise 会 req_version+1 且任务回到 queued
        tasks.revise(tid, req="新需求：改成两份", criteria=["两页"])

    workers.before_return = _revise
    delivery = FakeDelivery()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        delivery=delivery,
    )
    await coordinator.run_task(tid)

    task = tasks.get(tid)
    # 不接受旧结果；任务留在 queued 等下一轮（本测试不追第二轮）
    assert task["status"] == "queued"
    assert task["req_version"] == 2
    assert delivery.delivered == []


# ---------------------------------------------------------------------------
# 同工作区两个任务串行（锁）
# ---------------------------------------------------------------------------


async def test_same_workspace_tasks_serialized(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid1 = _create_task(tasks, title="任务1")
    tid2 = _create_task(tasks, title="任务2")  # 同群 → 同工作区
    models = ModelsQueue(
        replies=[_plan(), _review(), _plan(), _review(artifact="artifacts/T-2/index.html")]
    )
    entered: list[str] = []
    order: list[str] = []

    class _Worker:
        async def run(self, brief, *, group_id, tools, task_id="", actor="", max_steps=12,
                      output_schema=None, workspace=None, system_extra="", artifact_scope=None):
            entered.append(task_id)
            order.append(f"enter:{task_id}")
            await asyncio.sleep(0.05)
            # 写文件
            ws = workspace
            (ws / "artifacts" / task_id).mkdir(parents=True, exist_ok=True)
            (ws / "artifacts" / task_id / "index.html").write_text("<html>ok</html>", encoding="utf-8")
            order.append(f"exit:{task_id}")
            return WorkerReport(ok=True, summary="ok", evidence=["e"])

    workers = _Worker()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
    )
    await asyncio.gather(coordinator.run_task(tid1), coordinator.run_task(tid2))

    # 两个完成
    assert tasks.get(tid1)["status"] == "completed"
    assert tasks.get(tid2)["status"] == "completed"
    # 严格串行：第 2 个 enter 必须在第 1 个 exit 之后
    assert order.index(f"enter:{tid2}") > order.index(f"exit:{tid1}") or \
           order.index(f"enter:{tid1}") > order.index(f"exit:{tid2}")


# ---------------------------------------------------------------------------
# 主模型验收用 inspect_file 落 tool_calls（actor=主模型）
# ---------------------------------------------------------------------------


async def test_review_inspect_calls_logged(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    # 验收时主模型调 inspect_file 一次再下结论
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "inspect_file",
            "arguments": json.dumps({"path": f"artifacts/{tid}/index.html"}),
        },
    }

    class _ChatOneToolCall:
        text = ""

        def __init__(self):
            self.tool_calls = [tool_call]

    models = ModelsQueue(replies=[_plan()])
    # 第一次验收返回带 tool_call；之后主模型再发一条结论
    class _MixedModels(ModelsQueue):
        async def chat(self, role, messages, **kwargs):
            # 记录
            self.calls.append((role, [dict(m) for m in messages], kwargs))
            n = len(self.calls)
            if n == 1:
                return ReplayChatResult(_plan())
            if n == 2:
                return _ChatOneToolCall()
            return ReplayChatResult(_review(pass_=True))

    models = _MixedModels(replies=[])
    workers = FakeWorkers()

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
    )
    await coordinator.run_task(tid)

    rows = mem_store.read().execute(
        "SELECT actor, tool, ok FROM tool_calls WHERE task_id=? ORDER BY id", (tid,)
    ).fetchall()
    inspect_calls = [r for r in rows if r["tool"] == "inspect_file"]
    assert len(inspect_calls) == 1
    assert inspect_calls[0]["actor"] == "主模型"
    assert inspect_calls[0]["ok"] == 1


# ---------------------------------------------------------------------------
# ModelError → failed + report_error
# ---------------------------------------------------------------------------


async def test_model_error_marks_failed_and_reports(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[ModelError("端点返回 500：internal error")])
    workers = FakeWorkers()
    outbox = FakeOutbox()

    # 找一个真 report_error 记录：直接查库（report_error 走 error_reports 表）
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        outbox=outbox,
    )
    await coordinator.run_task(tid)

    task = tasks.get(tid)
    assert task["status"] == "failed"
    err_rows = mem_store.read().execute("SELECT * FROM error_reports").fetchall()
    assert len(err_rows) >= 1
    # outbox 也排上了一条 push_kind=error
    err_msgs = [
        e for e in outbox.enqueued
        if e["kind"] == "text" and e["payload"].get("push_kind") == "error"
    ]
    assert len(err_msgs) >= 1


@pytest.mark.parametrize("state", ["cancelled", "paused"])
async def test_late_plan_failure_after_stop_does_not_report_to_group(
    mem_store: Store, settings, env, tools, tasks, goals, state: str
):
    tid = _create_task(tasks)

    class _LateFailure(ModelsQueue):
        async def chat(self, role, messages, **kwargs):
            tasks.transition(tid, state, reason="管理员已停止")
            raise ModelError("晚到的模型异常")

    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=_LateFailure(), workers=FakeWorkers(), outbox=outbox,
    )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["status"] == state
    assert not outbox.enqueued
    assert mem_store.read().execute("SELECT COUNT(*) c FROM error_reports").fetchone()["c"] == 0
    attempt = mem_store.read().execute("SELECT status FROM attempts WHERE task_id=?", (tid,)).fetchone()
    assert attempt["status"] == "stale"


async def test_old_attempt_error_cannot_fail_new_running_attempt(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    tasks.transition(tid, "running")
    tasks.start_attempt(tid)
    old_id = tasks.current_attempt_id(tid)
    tasks.transition(tid, "paused")
    tasks.transition(tid, "queued")
    tasks.transition(tid, "running")
    tasks.start_attempt(tid)
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(), outbox=outbox,
    )
    coordinator._fail_with_err(tid, old_id, "旧尝试的晚到错误", GID)
    assert tasks.get(tid)["status"] == "running"
    assert mem_store.read().execute("SELECT status FROM attempts WHERE id=?", (old_id,)).fetchone()["status"] == "stale"
    assert outbox.enqueued == []


# ---------------------------------------------------------------------------
# check_goal：勾选完成标准、新建下级任务、无新信息不发消息、全满足 → done
# ---------------------------------------------------------------------------


def _seed_goal(goals: Goals) -> str:
    return goals.create_agent(
        GID,
        title="把工具链跑通",
        body="帮群友把工具链跑通",
        criteria=["写好脚本", "跑通测试"],
        by_text="2026-10",
    )


async def test_check_goal_ticks_criteria_and_creates_task(
    mem_store: Store, settings, env, tools, tasks, goals
):
    gid_goal = _seed_goal(goals)
    models = ModelsQueue(
        replies=[
            json.dumps(
                {
                    "done_criteria": [0],
                    "next_check_hours": 12,
                    "progress": "脚本写了一半",
                    "new_task": {"title": "把测试跑起来", "req": "把 pytest 跑通",
                                 "criteria": ["pytest 全过"]},
                    "report": None,
                },
                ensure_ascii=False,
            )
        ]
    )
    workers = FakeWorkers()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        outbox=outbox,
    )
    await coordinator.check_goal(gid_goal)

    goal = goals.get(gid_goal)
    crit = json.loads(goal["criteria"])
    assert crit[0]["done"] is True
    assert crit[1]["done"] is False
    assert goal["state"] == "active"
    # 新建了下级任务且立刻 run_task（会被计划+验收跑一轮；FakeWorkers 默认回 ok）
    rows = mem_store.read().execute(
        "SELECT id, goal_id, title, source FROM tasks WHERE goal_id=?", (gid_goal,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "把测试跑起来"
    assert rows[0]["source"] == "goal"


@pytest.mark.parametrize("stopped", ["cancelled", "paused"])
async def test_check_goal_discards_late_model_result_after_stop(
    mem_store: Store, settings, env, tools, tasks, goals, stopped
):
    goal_id = _seed_goal(goals)
    models = ModelsQueue()
    workers = FakeWorkers()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=models, workers=workers, outbox=outbox,
    )

    async def late_reply(*args, **kwargs):
        # 模拟主模型请求还在等待时，管理员取消或暂停了目标。
        (goals.cancel if stopped == "cancelled" else goals.pause)(goal_id)
        return ReplayChatResult(json.dumps({
            "done_criteria": [0, 1], "next_check_hours": 1,
            "progress": "晚到进展", "new_task": {"title": "晚到任务", "req": "不能执行"},
            "report": "晚到汇报",
        }, ensure_ascii=False))

    models.chat = late_reply
    await coordinator.check_goal(goal_id)
    goal = goals.get(goal_id)
    assert goal["state"] == stopped
    assert goal["heartbeat_ts"] is None
    assert goal["last_text"] in (None, "")
    assert mem_store.read().execute("SELECT COUNT(*) FROM tasks WHERE goal_id=?", (goal_id,)).fetchone()[0] == 0
    assert outbox.enqueued == []
    assert workers.calls == []


async def test_check_goal_does_not_report_late_failure_after_cancel(
    mem_store: Store, settings, env, tools, tasks, goals
):
    goal_id = _seed_goal(goals)
    models = ModelsQueue()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools,
        tasks=tasks, goals=goals, models=models, workers=FakeWorkers(), outbox=outbox,
    )

    async def late_failure(*args, **kwargs):
        goals.cancel(goal_id)
        raise ModelError("请求已经取消，模型超时的错误不该进群")

    models.chat = late_failure
    await coordinator.check_goal(goal_id)
    assert goals.get(goal_id)["state"] == "cancelled"
    assert outbox.enqueued == []


async def test_check_goal_no_new_info_silences(
    mem_store: Store, settings, env, tools, tasks, goals
):
    gid_goal = _seed_goal(goals)
    models = ModelsQueue(
        replies=[
            json.dumps(
                {
                    "done_criteria": [],
                    "next_check_hours": 24,
                    "progress": None,
                    "new_task": None,
                    "report": None,
                },
                ensure_ascii=False,
            )
        ]
    )
    workers = FakeWorkers()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        outbox=outbox,
    )
    await coordinator.check_goal(gid_goal)
    assert outbox.enqueued == []  # 无新信息不发任何消息


async def test_check_goal_all_criteria_done_marks_done(
    mem_store: Store, settings, env, tools, tasks, goals
):
    gid_goal = goals.create_agent(
        GID,
        title="搞定",
        body="x",
        criteria=["唯一一条"],
        by_text="",
    )
    models = ModelsQueue(
        replies=[
            json.dumps(
                {
                    "done_criteria": [0],
                    "next_check_hours": 0,
                    "progress": "完事了",
                    "new_task": None,
                    "report": "搞定了",
                },
                ensure_ascii=False,
            )
        ]
    )
    workers = FakeWorkers()
    outbox = FakeOutbox()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        outbox=outbox,
    )
    await coordinator.check_goal(gid_goal)
    goal = goals.get(gid_goal)
    assert goal["state"] == "done"
    # 完成话发一条
    texts = [
        e
        for e in outbox.enqueued
        if e["kind"] == "text" and "完成" in e["payload"]["text"] or "搞定" in e["payload"]["text"]
    ]
    assert texts  # 有一句完成的话


# ---------------------------------------------------------------------------
# resume：waiting_input 收回答 → queued → run_task
# ---------------------------------------------------------------------------


async def test_resume_waiting_input_appends_req_and_reruns(
    mem_store: Store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    # 先让任务进入 waiting_input
    models = ModelsQueue(replies=[_plan(question="需要确认：要简体还是繁体？")])
    workers = FakeWorkers()
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
    )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["status"] == "waiting_input"

    # 收到回答 → resume → queued → 立刻再 run（计划+验收 pass + 写文件）
    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    models.reply_queue = [_plan(), _review(pass_=True)]

    await coordinator.resume(tid, "简体就行")
    task = tasks.get(tid)
    assert task["status"] == "completed"
    # req 追加了回答
    assert "简体就行" in task["req"]


# ---------------------------------------------------------------------------
# tasks.tokens 汇总：按 task_id 求和写回 tasks.tokens
# ---------------------------------------------------------------------------


async def test_tokens_aggregated_to_tasks(mem_store: Store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(), _review(pass_=True)])
    workers = FakeWorkers()

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    coordinator = _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
    )
    # 手动塞两条 usage，按 task_id 求和后应写回 tasks.tokens
    with mem_store.tx() as conn:
        conn.execute(
            "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
            " prompt_tokens, completion_tokens, ok, ms, error)"
            " VALUES (?, ?, 'main', 'm', 'plan', ?, ?, 100, 50, 1, 1, '')",
            (NOW, clock.day_key(NOW), GID, tid),
        )
        conn.execute(
            "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
            " prompt_tokens, completion_tokens, ok, ms, error)"
            " VALUES (?, ?, 'worker', 'w', 'work', ?, ?, 200, 100, 1, 1, '')",
            (NOW, clock.day_key(NOW), GID, tid),
        )
    await coordinator.run_task(tid)
    assert tasks.get(tid)["tokens"] >= 450  # 100+50+200+100


# ---------------------------------------------------------------------------
# check_goal 带群聊上下文（2026-10）：第一次检查补验收标准；每次都带最近群聊
# ---------------------------------------------------------------------------


def _seed_chat(store: Store, gid: str, rows: list[tuple[str, str]]) -> None:
    with store.tx() as conn:
        for i, (who, text) in enumerate(rows):
            conn.execute(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (text, gid, f"gm{i}", NOW - i * 60, f"gu{i}", who),
            )


async def test_check_goal_first_check_fills_criteria_from_chat(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """criteria 为空 → 第一次检查用群聊补出验收标准并落库。"""
    goal_id = goals.create_agent(GID, title="把群里的工具链跑通", body="帮群友跑通", criteria=[], by_text="")
    _seed_chat(mem_store, GID, [("阿柒", "脚本还是报错，缺个依赖"), ("老李", "我刚测了，换个版本就好了")])
    models = ModelsQueue(
        replies=[
            json.dumps(
                {
                    "criteria": ["脚本能在本机跑通", "群里有人验证过"],
                    "done_criteria": [],
                    "next_check_hours": 12,
                    "progress": None,
                    "new_task": None,
                    "report": None,
                },
                ensure_ascii=False,
            )
        ]
    )
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=FakeOutbox(),
    )
    await coordinator.check_goal(goal_id)

    goal = goals.get(goal_id)
    crit = json.loads(goal["criteria"])
    assert [c["text"] for c in crit] == ["脚本能在本机跑通", "群里有人验证过"]
    assert all(c["done"] is False for c in crit)
    prompt = models.calls[0][1][-1]["content"]
    assert "脚本还是报错，缺个依赖" in prompt          # 群聊片段进了提示词
    assert "第一次检查" in prompt and '"criteria"' in prompt


async def test_check_goal_prompt_always_carries_recent_chat(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """已有 criteria 的常规检查：提示词照样带最近群聊，但不许改已有标准。"""
    gid_goal = _seed_goal(goals)  # criteria = 写好脚本 / 跑通测试
    long_line = "今天群里在聊" + "很长的进展" * 40  # 300+ 字，要截断
    _seed_chat(mem_store, GID, [("阿柒", "已经把脚本提交了"), ("老李", long_line)])
    models = ModelsQueue(
        replies=[
            json.dumps(
                {
                    "criteria": ["模型乱改的标准"],
                    "done_criteria": [0],
                    "next_check_hours": 12,
                    "progress": None,
                    "new_task": None,
                    "report": None,
                },
                ensure_ascii=False,
            )
        ]
    )
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=FakeOutbox(),
    )
    await coordinator.check_goal(gid_goal)

    prompt = models.calls[0][1][-1]["content"]
    assert "已经把脚本提交了" in prompt
    assert "今天群里在聊" in prompt
    assert "很长的进展" * 30 not in prompt       # 每条截到 80 字
    assert "第一次检查" not in prompt            # 已有标准就不再要它补
    crit = [c["text"] for c in json.loads(goals.get(gid_goal)["criteria"])]
    assert crit == ["写好脚本", "跑通测试"]       # 没被模型的 criteria 乱改


# ---------------------------------------------------------------------------
# 交付路径必须落在本任务的成品目录 artifacts/<task_id>/ 里（插件中心审核整改 6）
# ---------------------------------------------------------------------------
#
# 背景：验收返回的 review["artifact"] 原先把 "." 或 "tasks/..." 也放行，会把整个
# 工作区（tasks/、runtime/、tools/…）发到 here.now 公开页面或群文件。现在解析后的
# 真实路径必须等于或位于 <工作区>/artifacts/<task_id>/ 之下（resolve 后比较，防 ../
# 和符号链接），否则不交付、记日志、任务照常 done（和解析失败一样）。


def _write_in_workspace(env, tasks, tid: str, files: dict) -> Path:
    """在任务工作区里按相对路径写文件（父目录自动建）；返回工作区根。"""
    ws = env.workspace(tasks.get(tid)["workspace"])
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


async def _deliver_with_artifact(
    mem_store, settings, env, tools, tasks, goals, tid: str, artifact: str, files: dict
):
    """跑一轮「计划 → worker 写文件 → 验收 pass(artifact)」，返回 (task, delivery)。"""
    _write_in_workspace(env, tasks, tid, files)
    models = ModelsQueue(replies=[_plan(), _review(pass_=True, artifact=artifact)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers, delivery=delivery, outbox=FakeOutbox(),
    )
    # 每次重试都重写一遍（重试会再跑 worker）
    workers.before_return = lambda: _write_in_workspace(env, tasks, tid, files)
    await coordinator.run_task(tid)
    return tasks.get(tid), delivery


@pytest.mark.parametrize(
    "artifact",
    [".", "tasks/x.txt", f"artifacts/T-其他/index.html"],
)
async def test_delivery_rejects_outside_task_artifact_dir(
    mem_store: Store, settings, env, tools, tasks, goals, artifact: str
):
    """".", "tasks/x.txt", "artifacts/别的任务/x" → 不交付，也不能标已完成。"""
    tid = _create_task(tasks)
    files = {"tasks/x.txt": "内部草稿", "artifacts/T-其他/index.html": "<html>别人的</html>"}
    task, delivery = await _deliver_with_artifact(
        mem_store, settings, env, tools, tasks, goals, tid, artifact, files
    )
    assert task["status"] == "failed"             # 找不到合规成品不冒充完成
    assert delivery.delivered == []               # 一个渠道都没发


async def test_delivery_rejects_symlink_escape(
    mem_store: Store, settings, env, tools, tasks, goals, tmp_path
):
    """artifacts/<本任务>/link.html 是指向工作区外的符号链接 → 不交付。"""
    tid = _create_task(tasks)
    files = {"artifacts/__tid__/real.txt": "x"}
    files = {k.replace("__tid__", tid): v for k, v in files.items()}
    ws = _write_in_workspace(env, tasks, tid, files)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("机密", encoding="utf-8")
    (ws / "artifacts" / tid / "link.html").symlink_to(outside)

    models = ModelsQueue(replies=[_plan(), _review(pass_=True, artifact=f"artifacts/{tid}/link.html")])
    delivery = FakeDelivery()
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), delivery=delivery, outbox=FakeOutbox(),
    )
    await coordinator.run_task(tid)
    assert delivery.delivered == []
    # 1 次不通过后重试 3 次 → failed；关键是**从来没交付**
    assert tasks.get(tid)["status"] == "failed"


async def test_delivery_allows_file_inside_task_artifact_dir(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """artifacts/<本任务>/index.html → 照常交付（且交的是解析后的绝对路径）。"""
    tid = _create_task(tasks)
    task, delivery = await _deliver_with_artifact(
        mem_store, settings, env, tools, tasks, goals, tid,
        f"artifacts/{tid}/index.html", {f"artifacts/{tid}/index.html": "<html>ok</html>"},
    )
    assert task["status"] == "completed"
    assert len(delivery.delivered) == 1
    d = delivery.delivered[0]
    assert d["path"].name == "index.html"
    assert (d["path"].parent / "index.html").exists() or d["path"].name == "index.html"


async def test_delivery_allows_task_artifact_dir_itself(
    mem_store: Store, settings, env, tools, tasks, goals
):
    """artifacts/<本任务> 整个目录 → 放行（发目录）。"""
    tid = _create_task(tasks)
    task, delivery = await _deliver_with_artifact(
        mem_store, settings, env, tools, tasks, goals, tid,
        f"artifacts/{tid}", {f"artifacts/{tid}/index.html": "<html>ok</html>"},
    )
    assert task["status"] == "completed"
    assert len(delivery.delivered) == 1
    assert delivery.delivered[0]["path"] == env.workspace(tasks.get(tid)["workspace"]) / "artifacts" / tid
