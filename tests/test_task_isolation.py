"""任务隔离与子任务依赖（2026-10，线上 T-4 三次尝试全败的整改）：

线上事实（群 900000001，「整理童年诡事录的后续动态」）：
1. 主模型把活拆成「子 agent #1 调研写 artifacts/T-4/research.md」和
   「子 agent #2 按 research.md 做 index.html」，结果两个 job 全部同时开跑：
   #2 找不到 research.md 就去翻整个群工作区，读了别的任务的 artifacts/T-2、
   artifacts/a-share-5d-202609 的东西，把成品做成了 A 股行情；后面几次也是同时
   开工、互相覆盖。
2. 任务之间串文件：群工作区一群一个（g<群号>/），子 agent 能 list / read
   别的任务的 artifacts/。

整改（本文件测）：
- A. 计划 JSON 的 jobs 每项可以带可选字段 after（1 基编号列表）：后一步要用
  前一步的产出时声明，调度按依赖分层——没有 after 的照旧并发（受信号量）；
  非法 / 自依赖 / 成环 → 当没写 after（logger.warning，不卡死）。依赖的 job
  失败 / 异常：后一步照样开工，brief 里写「前一步没做成」。后一步开工时把
  依赖的那（几）步的交回摘要和成品路径追加进它的 brief。
- C. 任务的子 agent 只能碰自己的成品目录（artifacts/T-4/）和任务原文点名的
  artifacts 目录（如「接着改 T-2 的页面」）；ToolContext 加 artifact_scope
  （默认 None = 不限制）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from test_coordinator import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    NOW,
    FakeDelivery,
    FakeOutbox,
    ModelsQueue,
    _Profiles,
    _Settings,
    _build,
    _create_task,
    _plan,
    _review,
)

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport, Workers

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures（照 test_review_link_check.py 的写法，本地定义不依赖别模块的收集）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path: Path):
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    yield store
    store.close()


@pytest.fixture
def settings(tmp_path: Path):
    return _Settings(tmp_path / "workspaces")


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def tools(mem_store: Store) -> Tools:
    return Tools(mem_store)


@pytest.fixture
def tasks(mem_store: Store, settings) -> Tasks:
    return Tasks(mem_store, lambda: settings)


@pytest.fixture
def goals(mem_store: Store, settings) -> Goals:
    return Goals(mem_store, lambda: settings)


def _coord(*, mem_store, settings, env, tools, tasks, goals, models, workers) -> Coordinator:
    return _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        delivery=FakeDelivery(),
        outbox=FakeOutbox(),
    )


class FakeWorkers:
    """记录 each run 的调用参数（含 artifact_scope）；report_fn(actor) 决定返回什么。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.report_fn = None

    async def run(self, brief, *, group_id, tools, task_id="", actor="", max_steps=12,
                  output_schema=None, workspace=None, system_extra="", artifact_scope=None):
        self.calls.append({"brief": brief, "group_id": group_id, "tools": list(tools),
                           "task_id": task_id, "actor": actor, "artifact_scope": artifact_scope})
        fn = self.report_fn
        if fn is not None:
            return fn(actor)
        return WorkerReport(ok=True, summary=f"{actor} 交回", evidence=[])


def _jobs_plan(jobs: list[dict], deliver_kind: str = "text") -> str:
    return json.dumps(
        {"criteria": ["有东西"], "deliver_kind": deliver_kind, "jobs": jobs, "question": None},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# A1. 计划解析：after 留进 plan（conversion-tool 只管字段本身，不跑执行）
# ---------------------------------------------------------------------------


async def test_plan_keeps_after(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    raw = _jobs_plan([
        {"brief": "调研", "tools": ["fetch_page"], "type": "research"},
        {"brief": "按调研做页面", "tools": ["write_file"], "type": "build", "after": [1]},
    ])
    models = ModelsQueue(replies=[raw])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["jobs"][0].get("after") == []
    assert plan["jobs"][1].get("after") == [1]


async def test_plan_rejects_out_of_range_after(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    raw = _jobs_plan([
        {"brief": "调研", "tools": ["fetch_page"]},
        {"brief": "按调研做页面", "tools": ["write_file"], "after": [5, "x", 0]},
    ])
    models = ModelsQueue(replies=[raw])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["jobs"][1].get("after") == []


async def test_plan_rejects_self_dependency(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    raw = _jobs_plan([
        {"brief": "调研", "tools": ["fetch_page"]},
        {"brief": "自己做自己", "tools": ["write_file"], "after": [2]},
    ])
    models = ModelsQueue(replies=[raw])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["jobs"][1].get("after") == []


async def test_plan_rejects_cyclic_after(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    raw = _jobs_plan([
        {"brief": "第一步", "tools": ["fetch_page"], "after": [2]},
        {"brief": "第二步", "tools": ["write_file"], "after": [1]},
    ])
    models = ModelsQueue(replies=[raw])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    # 成环 → 至少一边的 after 被清掉（当没写），具体哪边无所谓，关键是别卡死
    total_after = sum(len(j.get("after") or []) for j in plan["jobs"])
    assert total_after < 2


# ---------------------------------------------------------------------------
# A2. 调度：after 的先后、brief 注入、失败照样开工、reports 顺序、无 after 并发
# ---------------------------------------------------------------------------


async def _run_with_events(models, workers, jobs_raw):
    """跑一轮 run_task，返回 (calls, reports_order_check)。"""
    return None  # 占位，不用


async def test_after_job_starts_only_after_dependency(mem_store, settings, env, tools, tasks, goals):
    """有 after 时后一步在前一步结束后才开始；后一步 brief 带前一步摘要。"""
    tid = _create_task(tasks)
    started: dict[str, asyncio.Event] = {"子 agent #1": asyncio.Event(), "子 agent #2": asyncio.Event()}
    order: list[str] = []

    class EventWorkers:
        def __init__(self):
            self.calls: list[dict] = []

        async def run(self, brief, *, group_id, tools, task_id="", actor="", **kw):
            self.calls.append({"brief": brief, "actor": actor})
            order.append(actor)
            started[actor].set()
            if actor == "子 agent #1":
                # 等着：如果 #2 已经开了，这里就是并发（不合要求）
                await asyncio.sleep(0.05)
                return WorkerReport(ok=True, summary="调研摘要：整理出 3 条后续动态", evidence=[])
            return WorkerReport(ok=True, summary="页面做完了", evidence=[])

    workers = EventWorkers()
    raw = _jobs_plan([
        {"brief": "调研", "tools": ["fetch_page"], "type": "research"},
        {"brief": "按调研做页面", "tools": ["write_file"], "type": "build", "after": [1]},
    ])
    models = ModelsQueue(replies=[raw, _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)

    assert order == ["子 agent #1", "子 agent #2"], f"后一步必须等前一步跑完才开工：{order}"
    brief2 = workers.calls[1]["brief"]
    assert "调研摘要：整理出 3 条后续动态" in brief2
    assert "前一步交回的" in brief2


async def test_after_job_still_runs_when_dependency_failed(mem_store, settings, env, tools, tasks, goals):
    """依赖的 job 失败：后一步照样开工，brief 里告诉它前一步没做成。"""
    tid = _create_task(tasks)

    class FailingWorkers:
        def __init__(self):
            self.calls: list[dict] = []

        async def run(self, brief, *, group_id, tools, task_id="", actor="", **kw):
            self.calls.append({"brief": brief, "actor": actor})
            if actor == "子 agent #1":
                return WorkerReport(ok=False, summary="", error="抓页面全失败：403")
            return WorkerReport(ok=True, summary="页面做完了", evidence=[])

    workers = FailingWorkers()
    raw = _jobs_plan([
        {"brief": "调研", "tools": ["fetch_page"], "type": "research"},
        {"brief": "按调研做页面", "tools": ["write_file"], "type": "build", "after": [1]},
    ])
    models = ModelsQueue(replies=[raw, _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)

    assert len(workers.calls) == 2, "前一步失败，后一步也必须照样开工（不许跳过去别处找替代品）"
    brief2 = workers.calls[1]["brief"]
    assert "没做成" in brief2
    assert "抓页面全失败：403" in brief2


async def test_after_job_receives_artifact_paths_from_dependency(mem_store, settings, env, tools, tasks, goals):
    """前一步 evidence 里带工作区路径时，后一步 brief 里能看到这些路径。"""
    tid = _create_task(tasks)

    class PathWorkers:
        def __init__(self):
            self.calls: list[dict] = []

        async def run(self, brief, *, group_id, tools, task_id="", actor="", **kw):
            self.calls.append({"brief": brief, "actor": actor})
            if actor == "子 agent #1":
                return WorkerReport(
                    ok=True, summary="调研完成",
                    evidence=[f"artifacts/{tid}/research.md"],
                )
            return WorkerReport(ok=True, summary="页面做完了", evidence=[])

    workers = PathWorkers()
    raw = _jobs_plan([
        {"brief": "调研", "tools": ["fetch_page"], "type": "research"},
        {"brief": "按调研做页面", "tools": ["write_file"], "type": "build", "after": [1]},
    ])
    models = ModelsQueue(replies=[raw, _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)

    brief2 = workers.calls[1]["brief"]
    assert f"artifacts/{tid}/research.md" in brief2


async def test_reports_order_matches_jobs_order_with_after(mem_store, settings, env, tools, tasks, goals):
    """有 after 时 reports 顺序仍与 jobs 顺序一致（验收代码按下标用）。"""
    tid = _create_task(tasks)
    thirds: list[str] = []

    class OrderWorkers:
        def __init__(self):
            self.calls: list[dict] = []

        async def run(self, brief, *, group_id, tools, task_id="", actor="", **kw):
            self.calls.append({"brief": brief, "actor": actor})
            # 交回标注自己是哪个 actor，最后一并出现在 attempt summary 里
            return WorkerReport(ok=True, summary=f"AA-{actor}", evidence=[])

    workers = OrderWorkers()
    raw = _jobs_plan([
        {"brief": "第一步", "tools": ["fetch_page"]},
        {"brief": "第二步等第一步", "tools": ["write_file"], "after": [1]},
    ])
    models = ModelsQueue(replies=[raw, _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)

    # attempt summary 按 jobs 顺序拼（AA-子 agent #1 在前，AA-子 agent #2 在后）
    row = mem_store.read().execute(
        "SELECT summary FROM attempts WHERE task_id=? ORDER BY n DESC LIMIT 1", (tid,)
    ).fetchone()
    summ = str(row["summary"] if row else "")
    i1 = summ.find("AA-子 agent #1")
    i2 = summ.find("AA-子 agent #2")
    assert i1 != -1 and i2 != -1, f"两个 job 的摘要都该在：{summ}"
    assert i1 < i2, f"顺序不对（#1 应在 #2 前）：{summ}"


async def test_no_after_still_concurrent(mem_store, settings, env, tools, tasks, goals):
    """没有 after 的两个 job 照旧并发（都开跑了才各自结束）。"""
    tid = _create_task(tasks)
    started: dict[str, asyncio.Event] = {"子 agent #1": asyncio.Event(), "子 agent #2": asyncio.Event()}

    class ConcurrentWorkers:
        def __init__(self):
            self.calls: list[dict] = []

        async def run(self, brief, *, group_id, tools, task_id="", actor="", **kw):
            self.calls.append({"brief": brief, "actor": actor})
            started[actor].set()
            # 等另一个也开了再交回：不并发的话这里会死锁（一段超时兜底）
            try:
                others = [a for a in started if a != actor]
                if others:
                    await asyncio.wait_for(started[others[0]].wait(), timeout=2)
            except asyncio.TimeoutError:
                raise AssertionError("两个 job 没有并发（一个等另一个先结束）")
            return WorkerReport(ok=True, summary=f"{actor} 完", evidence=[])

    workers = ConcurrentWorkers()
    raw = _jobs_plan([
        {"brief": "第一件事", "tools": ["fetch_page"]},
        {"brief": "第二件事", "tools": ["write_file"]},
    ])
    models = ModelsQueue(replies=[raw, _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)
    assert len(workers.calls) == 2


async def test_invalid_after_does_not_deadlock(mem_store, settings, env, tools, tasks, goals):
    """非法 after（越界 / 自依赖）：当没写 after，不卡死（一轮内跑完）。"""
    tid = _create_task(tasks)

    class QuickWorkers:
        def __init__(self):
            self.calls: list[dict] = []

        async def run(self, brief, *, group_id, tools, task_id="", actor="", **kw):
            self.calls.append({"brief": brief, "actor": actor})
            return WorkerReport(ok=True, summary=f"{actor} 完", evidence=[])

    workers = QuickWorkers()
    raw = _jobs_plan([
        {"brief": "第一步", "tools": ["fetch_page"]},
        {"brief": "after 写成 99 和 'x'，该被当没写", "tools": ["write_file"], "after": [99, "x", 2]},
    ])
    models = ModelsQueue(replies=[raw, _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await asyncio.wait_for(coord.run_task(tid), timeout=10)
    assert len(workers.calls) == 2


async def test_plan_prompt_mentions_after(mem_store, settings, env, tools, tasks, goals):
    """计划提示词里要写清 after 的用法（先调研再做页面时后一步写 after）。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_jobs_plan([{"brief": "调研", "tools": ["fetch_page"]}])])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await coord._plan(tasks.get(tid))
    plan_prompts = "\n".join(
        m.get("content") or ""
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.plan"
        for m in msgs
    )
    assert "after" in plan_prompts


# ---------------------------------------------------------------------------
# C. 任务的子 agent 只能碰自己的成品目录
# ---------------------------------------------------------------------------

_WS = "ws-1"


def _file_setup(tmp_path: Path, mem_store: Store, settings):
    """真 LocalEnv + 真注册工具；写好几个任务的 artifacts 目录；返回 (tools, ws_path, store)。"""
    s, _ = __import__("CharTyr_MaiWork.maiwork.config", fromlist=["load_settings"]).load_settings(
        {"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")}}
    )
    env = LocalEnv(lambda: s)
    tools = Tools(mem_store)

    class _H:
        async def messages(self, *a, **k):
            return []

        async def knowledge(self, *a, **k):
            return ""

    register_exec_tools(tools, env=env, host=_H(), get_settings=lambda: s)
    ws = env.workspace(_WS)
    for rel, content in (
        ("artifacts/T-4/index.html", "<html>本任务</html>"),
        ("artifacts/T-2/index.html", "<html>上次任务</html>"),
        ("artifacts/other-task/x.md", "别的任务"),
        ("PROFILE-902.md", "群画像"),
    ):
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tools, ws, mem_store


def _scoped_ctx(ws: Path, scope: tuple[str, ...] | None) -> ToolContext:
    return ToolContext(
        group_id=GID, task_id="T-4", actor="子 agent #1", role="worker", workspace=ws,
        artifact_scope=scope,
    )


async def test_scope_read_write_own_dir_ok(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("read_file", {"path": "artifacts/T-4/index.html"}, ctx)
    assert r.ok and "本任务" in r.output
    r = await tools_.call("write_file", {"path": "artifacts/T-4/new.md", "content": "x"}, ctx)
    assert r.ok


async def test_scope_read_other_task_dir_denied(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("read_file", {"path": "artifacts/T-2/index.html"}, ctx)
    assert not r.ok
    assert "别的任务" in r.error
    assert "artifacts/T-4" in r.error


async def test_scope_write_other_task_dir_denied(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("write_file", {"path": "artifacts/T-2/hack.md", "content": "x"}, ctx)
    assert not r.ok
    assert "别的任务" in r.error
    assert not (ws / "artifacts/T-2/hack.md").exists()


async def test_scope_list_artifacts_filters_other_dirs(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("list_files", {"path": "artifacts"}, ctx)
    assert r.ok
    assert "T-4" in r.output
    assert "T-2" not in r.output
    assert "other-task" not in r.output


async def test_scope_list_root_filters_other_artifacts(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("list_files", {"path": ""}, ctx)
    assert r.ok
    assert "T-2" not in r.output
    assert "PROFILE-902.md" in r.output  # artifacts/ 之外的不受影响


async def test_scope_list_other_task_dir_denied(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("list_files", {"path": "artifacts/T-2"}, ctx)
    assert not r.ok
    assert "别的任务" in r.error


async def test_scope_named_dir_allowed(tmp_path, mem_store, settings):
    """任务原文点名的别的 artifacts 目录放行（「接着改 T-2 的页面」那种活）。"""
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4", "artifacts/T-2"))
    r = await tools_.call("read_file", {"path": "artifacts/T-2/index.html"}, ctx)
    assert r.ok and "上次任务" in r.output


async def test_scope_bypass_with_dot_segments_denied(tmp_path, mem_store, settings):
    """路径绕过（./artifacts/../artifacts/T-2、结尾斜杠）也要拦。"""
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("read_file", {"path": "./artifacts/T-2/index.html"}, ctx)
    assert not r.ok and "别的任务" in r.error
    r = await tools_.call("read_file", {"path": "artifacts/./T-2/index.html"}, ctx)
    assert not r.ok and "别的任务" in r.error


async def test_scope_none_behaves_as_before(tmp_path, mem_store, settings):
    """scope=None（管理员对话、资讯等老调用方）：行为完全不变。"""
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, None)
    r = await tools_.call("read_file", {"path": "artifacts/T-2/index.html"}, ctx)
    assert r.ok and "上次任务" in r.output
    r = await tools_.call("list_files", {"path": "artifacts"}, ctx)
    assert "T-2" in r.output


async def test_scope_profile_files_not_affected(tmp_path, mem_store, settings):
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = _scoped_ctx(ws, ("artifacts/T-4",))
    r = await tools_.call("read_file", {"path": "PROFILE-902.md"}, ctx)
    assert r.ok and "群画像" in r.output


async def test_scope_main_inspect_unaffected(tmp_path, mem_store, settings):
    """主模型验收用的 inspect_file（role=main 没有 scope）：不受隔离限制。"""
    tools_, ws, _ = _file_setup(tmp_path, mem_store, settings)
    ctx = ToolContext(group_id=GID, task_id="T-4", actor="主模型", role="main", workspace=ws)
    r = await tools_.call("inspect_file", {"path": "artifacts/T-2/index.html"}, ctx)
    assert r.ok and "上次任务" in r.output


# ---------------------------------------------------------------------------
# C-集成：coordinator 派任务子 agent 时传 scope（workers 收到的 ctx 要带它）
# ---------------------------------------------------------------------------


async def test_coordinator_passes_scope_to_workers(mem_store, settings, env, tools, tasks, goals):
    """coordinator 派任务子 agent 时：ToolContext.artifact_scope = 本任务目录 + req 点名的目录。"""
    tid = _create_task(tasks, req="接着上次 T-2 的页面，再补一段新的")
    captured: list[ToolContext] = []

    class SpyTools(Tools):
        async def call(self, name, args, ctx):
            captured.append(ctx)
            if name == "submit_result":
                return ToolResult(ok=True, output="", data={"summary": "好了", "data": None, "evidence": []})
            return ToolResult(ok=False, output="", error="spy 截停")

    class SpyModels:
        def settings(self):
            class _S:
                def ready(self):
                    return True

            return _S()

        def __init__(self):
            self._n = 0

        async def chat(self, role, messages, **kw):
            self._n += 1
            name = "submit_result" if self._n >= 2 else "read_file"
            return type("R", (), {"text": "", "tool_calls": [
                {"id": f"c{self._n}", "function": {"name": name, "arguments": "{}"}}
            ]})()

    spy_tools = SpyTools(mem_store)
    workers = Workers(SpyModels(), spy_tools, get_settings=lambda: settings, tasks=tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals,
                   models=ModelsQueue(replies=[
                       _jobs_plan([{"brief": "补一段新的", "tools": ["read_file"]}]),
                       _review(pass_=True, artifact="", review="过了"),
                   ]),
                   workers=workers)
    await coord.run_task(tid)

    assert captured, "子 agent 一次工具都没调到（spy 没截到），没法核对它的 ToolContext"
    scope = getattr(captured[0], "artifact_scope", None)
    assert scope is not None, "任务子 agent 的 ToolContext 没带 artifact_scope"
    assert f"artifacts/{tid}" in scope
    assert "artifacts/T-2" in scope  # req 里点名的「T-2」要放行


async def test_coordinator_scope_defaults_when_req_names_nothing(mem_store, settings, env, tools, tasks, goals):
    """req 没点名别的任务 / 别的 artifacts 目录时，scope 只有本任务目录。"""
    tid = _create_task(tasks, req="做个新页面")
    captured: list[ToolContext] = []

    class SpyTools(Tools):
        async def call(self, name, args, ctx):
            captured.append(ctx)
            if name == "submit_result":
                return ToolResult(ok=True, output="", data={"summary": "好了", "data": None, "evidence": []})
            return ToolResult(ok=False, output="", error="spy 截停")

    class SpyModels:
        def settings(self):
            class _S:
                def ready(self):
                    return True

            return _S()

        def __init__(self):
            self._n = 0

        async def chat(self, role, messages, **kw):
            self._n += 1
            name = "submit_result" if self._n >= 2 else "read_file"
            return type("R", (), {"text": "", "tool_calls": [
                {"id": f"c{self._n}", "function": {"name": name, "arguments": "{}"}}
            ]})()

    spy_tools = SpyTools(mem_store)
    workers = Workers(SpyModels(), spy_tools, get_settings=lambda: settings, tasks=tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals,
                   models=ModelsQueue(replies=[
                       _jobs_plan([{"brief": "做个新页面", "tools": ["read_file"]}]),
                       _review(pass_=True, artifact="", review="过了"),
                   ]),
                   workers=workers)
    await coord.run_task(tid)

    assert captured
    scope = getattr(captured[0], "artifact_scope", None)
    assert scope == (f"artifacts/{tid}",) or list(scope or []) == [f"artifacts/{tid}"]


async def test_enrich_brief_mentions_isolation(mem_store, settings, env, tools, tasks, goals):
    """派给子 agent 的 brief 里有一句：只用本任务目录，别的任务的文件别读别用。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers())
    brief = coord._enrich_brief("做个页面", tid, "file")
    assert f"artifacts/{tid}" in brief
    assert "别的任务" in brief
