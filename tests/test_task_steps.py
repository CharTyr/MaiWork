"""docs/22 §4 第二期：固定步骤 + 各写各的 + 只返工失败的一步 + 止损。

覆盖 A–G（每块一节，含反向用例）：
- A. 计划解析后由代码补先后依赖（research → 非 research），模型自己写了 after 的不动；
- B. 上游没交出资料 → 下游不开工（本轮不进验收、attempt 记 failed、走既有打回）；
- C. 中间步骤（被别人 after 的活）只写 artifacts/<任务>/steps/<步号>/，交付步骤不可写别的步骤目录；
- D. 开工前工具补齐：research 活要搜索 + 抓正文 + write_file；要产出文件的活要 write_file；
- E. 返工复用上一轮交出了资料的步骤（kv task.step.<任务>.<步号>）；
- F. 止损：同一主机连续拒绝 2 次不再试；连续 15 / 25 次没有新收获（workers.py）；
- G. 客观做不到 → 问发起人（第二次尝试起），不再整轮重跑。

用真 Store / 真 Tasks / 真 LocalEnv（direct 模式）+ 假模型队列 + 假 Workers；
不调真模型、不碰网络、不碰宿主。
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
)

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tool, ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from fakes import write_text_reply

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
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


# ---------------------------------------------------------------------------
# 共用桩 / 小工具
# ---------------------------------------------------------------------------


async def _stub_handler(ctx: ToolContext, args: dict) -> ToolResult:  # noqa: ARG001
    return ToolResult(ok=True, output="（测试桩）", data={})


def _register_stub_tools(tools: Tools) -> None:
    """D 要的三样里，搜索 / 抓正文在测试里注册成桩（write_file 由 register_exec_tools 注册）。"""
    for name in ("web_search", "fetch_page"):
        if tools.get(name, "worker") is None:
            tools.register(
                Tool(
                    name=name,
                    description="测试桩（只证明注册表里真有）",
                    parameters={"type": "object", "properties": {}},
                    roles=frozenset({"worker"}),
                    handler=_stub_handler,
                )
            )


def _coord(
    *, mem_store, settings, env, tools, tasks, goals, models, workers,
    delivery=None, outbox=None, specialists=None, stub_tools=True, register_exec=True,
) -> Coordinator:
    if register_exec and tools.get("write_file", "worker") is None:
        register_exec_tools(
            tools, env=env, host=None, get_settings=lambda: settings,
            session_of=lambda gid: "sess-1",
        )
    if stub_tools:
        _register_stub_tools(tools)
    coord = Coordinator(
        mem_store, models, workers, tools, tasks, goals,
        delivery or FakeDelivery(), outbox or FakeOutbox(), env, _Profiles(), lambda: settings,
    )
    if specialists is not None:
        coord._specialists = specialists  # noqa: SLF001
    return coord


def _create_task(tasks: Tasks, *, title="整理", req="做一页总结", criteria=(),
                 reply="（正文）", **kw) -> str:
    """建测试任务；顺手把 text 活的成品正文写进 reply.md（线上 T-13，见 tests/fakes.py）。

    这个文件（和引用它的用例）的计划缺省 deliver_kind="text"，不写正文会被验收改判不通过。
    需要「没有 reply.md」的用例显式传 `reply=None`。
    """
    kwargs = {"title": title, "req": req, "criteria": list(criteria), "source": "test"}
    kwargs.update(kw)
    tid = tasks.create(GID, **kwargs)
    if reply is not None:
        write_text_reply(tasks, tid, str(reply))
    return tid


def _plan_json(jobs, *, criteria=None, deliver_kind="text", question=None, requirements=None) -> str:
    data = {
        "criteria": list(criteria if criteria is not None else ["有东西"]),
        "deliver_kind": deliver_kind,
        "jobs": jobs,
        "question": question,
    }
    if requirements is not None:
        data["requirements"] = requirements
    return json.dumps(data, ensure_ascii=False)


def _job(brief, tools, *, type_="other", after=None, agent=None) -> dict:
    job: dict = {"brief": brief, "tools": list(tools), "type": type_}
    if after:
        job["after"] = list(after)
    if agent:
        job["agent"] = agent
    return job


def _review_json(pass_=True, artifact="", review="过了", items=None) -> str:
    data = {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": "好"}
    if items is not None:
        data["items"] = items
    return json.dumps(data, ensure_ascii=False)


def _events(store: Store, tid: str) -> list[tuple[str, dict]]:
    rows = store.read().execute(
        "SELECT kind, payload FROM events WHERE entity_id=? ORDER BY id", (tid,)
    ).fetchall()
    out: list[tuple[str, dict]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append((str(r["kind"]), payload))
    return out


def _kinds(store: Store, tid: str) -> list[str]:
    return [k for k, _p in _events(store, tid)]


def _payloads(store: Store, tid: str, kind: str) -> list[dict]:
    return [p for k, p in _events(store, tid) if k == kind]


def _write_ws(env, tasks: Tasks, tid: str, rel: str, content: str = "内容") -> Path:
    ws = env.workspace(tasks.get(tid)["workspace"])
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _attempt_rows(store: Store, tid: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT n, status, summary, review FROM attempts WHERE task_id=? ORDER BY n", (tid,)
    ).fetchall()
    return [dict(r) for r in rows]


class StepWorkers:
    """假 Workers：按队列回放 WorkerReport；hook(actor) 在交回前跑（写真实文件用）。"""

    def __init__(self, results=None, hook=None):
        self.results = list(results or [])
        self.hook = hook
        self.calls: list[dict] = []

    async def run(
        self, brief, *, group_id, tools, task_id="", actor="", artifact_scope=None,
        write_scope=None, **kw,
    ):
        self.calls.append({
            "brief": brief, "group_id": group_id, "tools": list(tools), "task_id": task_id,
            "actor": actor, "artifact_scope": artifact_scope, "write_scope": write_scope,
            "kw": dict(kw),
        })
        if self.hook is not None:
            out = self.hook(actor)
            if asyncio.iscoroutine(out):
                await out
        if not self.results:
            return WorkerReport(ok=True, summary=f"{actor} 交回", evidence=[])
        item = self.results.pop(0)
        return item(actor) if callable(item) else item

    def brief_of(self, actor: str) -> str:
        for c in self.calls:
            if c["actor"] == actor:
                return str(c["brief"])
        raise AssertionError(f"没有 {actor} 的调用：{[c['actor'] for c in self.calls]}")


def _plan_prompts(models: ModelsQueue) -> list[str]:
    return [
        "\n".join(str(m.get("content") or "") for m in msgs)
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.plan"
    ]


def _review_prompt_count(models: ModelsQueue) -> int:
    return sum(
        1 for _r, _m, kw in models.calls if str(kw.get("purpose") or "") == "coordinator.review"
    )


# ---------------------------------------------------------------------------
# A. 代码自动补先后依赖
# ---------------------------------------------------------------------------


async def test_auto_after_research_then_build(mem_store, settings, env, tools, tasks, goals):
    """调研 + 制作：制作没写 after → 代码补成 after=[1]，并记事件。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[
        _plan_json([
            _job("调研一下大家怎么看", ["fetch_page", "web_search", "write_file"], type_="research"),
            _job("按调研做页面", ["write_file"], type_="build"),
        ])
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=StepWorkers())
    plan = await coord._plan(tasks.get(tid))

    assert plan["jobs"][0]["after"] == []
    assert plan["jobs"][1]["after"] == [1], "非调研的活要自动等调研"
    payloads = _payloads(mem_store, tid, "task.plan.auto_after")
    assert payloads and payloads[-1].get("jobs") == [2], f"事件要记补了哪几条：{payloads}"


async def test_auto_after_keeps_model_written_after(mem_store, settings, env, tools, tasks, goals):
    """模型自己写了 after 的活不动，也不记自动补的事件。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[
        _plan_json([
            _job("调研一下", ["fetch_page", "web_search", "write_file"], type_="research"),
            _job("按调研做页面", ["write_file"], type_="build", after=[1]),
        ])
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=StepWorkers())
    plan = await coord._plan(tasks.get(tid))

    assert plan["jobs"][1]["after"] == [1]
    assert "task.plan.auto_after" not in _kinds(mem_store, tid)


async def test_auto_after_two_research_no_dependency(mem_store, settings, env, tools, tasks, goals):
    """两条都是调研：调研之间不加依赖，也不记事件。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[
        _plan_json([
            _job("调研 A", ["fetch_page", "web_search", "write_file"], type_="research"),
            _job("调研 B", ["fetch_page", "web_search", "write_file"], type_="research"),
        ])
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=StepWorkers())
    plan = await coord._plan(tasks.get(tid))

    assert plan["jobs"][0]["after"] == [] and plan["jobs"][1]["after"] == []
    assert "task.plan.auto_after" not in _kinds(mem_store, tid)


async def test_auto_after_no_research_no_event(mem_store, settings, env, tools, tasks, goals):
    """没有调研活：一条都不补。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[
        _plan_json([_job("做页面", ["write_file"]), _job("再写一段", ["write_file"])])
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=StepWorkers())
    plan = await coord._plan(tasks.get(tid))

    assert plan["jobs"][0]["after"] == [] and plan["jobs"][1]["after"] == []
    assert "task.plan.auto_after" not in _kinds(mem_store, tid)


async def test_auto_after_does_not_create_cycle(mem_store, settings, env, tools, tasks, goals):
    """自动补的 after 也要过成环清理（模型写了「制作等调研」，不能再补成环）。"""
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[
        _plan_json([
            _job("做页面", ["write_file"]),
            _job("调研一下", ["fetch_page", "web_search", "write_file"], type_="research", after=[1]),
        ])
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=StepWorkers())
    plan = await coord._plan(tasks.get(tid))

    total = sum(len(j.get("after") or []) for j in plan["jobs"])
    assert total < 2, f"自动补不能造出环：{plan['jobs']}"


# ---------------------------------------------------------------------------
# B. 上游没交出资料 → 下游不开工
# ---------------------------------------------------------------------------


async def _run_once(coord, tasks, tid: str) -> str:
    """直接跑一轮尝试（不经过 run_task 的重试循环），返回 "retry" / "done"。"""
    return await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001


def _two_step_plan(*, after=None):
    return _plan_json([
        _job("调研一下大家怎么看", ["fetch_page", "web_search", "write_file"], type_="research"),
        _job("按调研做页面", ["write_file"], type_="build", after=after),
    ])


async def test_dep_not_delivered_skips_downstream(mem_store, settings, env, tools, tasks, goals):
    """上游 ok 但什么都没交出来（没声称路径、steps/ 下也没有文件）→ 下游不开工。"""
    tid = _create_task(tasks)
    workers = StepWorkers(results=[
        WorkerReport(ok=True, summary="调研完了，资料在脑子里", evidence=[]),
    ])
    models = ModelsQueue(replies=[_two_step_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    action = await _run_once(coord, tasks, tid)

    assert action == "retry", "还有尝试次数 → 走既有打回（回队列）"
    assert len(workers.calls) == 1, f"下游不许开工：{[c['actor'] for c in workers.calls]}"
    assert workers.calls[0]["actor"] == "子 agent #1"
    assert tasks.get(tid)["status"] == "queued"
    row = _attempt_rows(mem_store, tid)[-1]
    assert row["status"] == "failed"
    assert "第 1 条活没交出资料" in row["review"]
    assert "依赖它的第 2 条没开工" in row["review"]
    assert "资料在脑子里" in row["review"]
    assert _review_prompt_count(models) == 0, "本轮不进验收（省验收 token）"
    assert "task.job_skipped" in _kinds(mem_store, tid)


async def test_dep_review_truncates_upstream_text(mem_store, settings, env, tools, tasks, goals):
    """上游的说明在验收意见里截到 120 字。"""
    tid = _create_task(tasks)
    long_text = "抓取失败：" + "很长的原因" * 60
    workers = StepWorkers(results=[WorkerReport(ok=True, summary=long_text, evidence=[])])
    models = ModelsQueue(replies=[_two_step_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    review = _attempt_rows(mem_store, tid)[-1]["review"]
    assert "第 1 条活没交出资料" in review
    assert "很长的原因" * 60 not in review
    assert len(review) < 300


async def test_dep_failed_skips_downstream(mem_store, settings, env, tools, tasks, goals):
    """上游 ok=False → 下游也不开工，验收意见里带上游的错误。"""
    tid = _create_task(tasks)
    workers = StepWorkers(results=[WorkerReport(ok=False, summary="", error="抓页面全失败：403")])
    models = ModelsQueue(replies=[_two_step_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 1
    review = _attempt_rows(mem_store, tid)[-1]["review"]
    assert "403" in review and "依赖它的第 2 条没开工" in review


async def test_dep_delivered_downstream_runs_with_data(mem_store, settings, env, tools, tasks, goals):
    """上游真交出了文件 → 下游正常开工，brief 带路径 + 结构化交回（data）。"""
    tid = _create_task(tasks)

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, f"artifacts/{tid}/research.md", "资料正文")

    workers = StepWorkers(
        results=[
            WorkerReport(
                ok=True, summary="调研完成",
                evidence=[f"artifacts/{tid}/research.md"],
                data={"points": ["A", "B"]},
            ),
            WorkerReport(ok=True, summary="页面做完了", evidence=[]),
        ],
        hook=hook,
    )
    models = ModelsQueue(replies=[_two_step_plan(), _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 2, "上游交出了资料 → 下游要开工"
    brief2 = workers.brief_of("子 agent #2")
    assert f"artifacts/{tid}/research.md" in brief2
    assert '"points"' in brief2 or "points" in brief2, f"结构化交回要带进 brief：{brief2[-300:]}"
    assert "job_skipped" not in _kinds(mem_store, tid)


async def test_dep_claimed_missing_path_noted_in_brief(mem_store, settings, env, tools, tasks, goals):
    """上游声称但不存在的路径不传，brief 里注明它不存在；真的那份照传。"""
    tid = _create_task(tasks)

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, f"artifacts/{tid}/real.md", "真资料")

    workers = StepWorkers(
        results=[
            WorkerReport(
                ok=True, summary="调研完成",
                evidence=[f"artifacts/{tid}/real.md", f"artifacts/{tid}/ghost.md"],
                data={"n": 1},
            ),
            WorkerReport(ok=True, summary="页面做完了", evidence=[]),
        ],
        hook=hook,
    )
    models = ModelsQueue(replies=[_two_step_plan(), _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 2
    brief2 = workers.brief_of("子 agent #2")
    assert f"artifacts/{tid}/real.md" in brief2
    assert "ghost.md" in brief2 and "不存在" in brief2, f"声称但不存在的路径要注明：{brief2[-400:]}"


async def test_dep_steps_dir_file_counts_as_delivered(mem_store, settings, env, tools, tasks, goals):
    """上游没声称任何路径，但 steps/<步号>/ 下有非空文件 → 算交出了。"""
    tid = _create_task(tasks)

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, f"artifacts/{tid}/steps/1/research.md", "资料")

    workers = StepWorkers(
        results=[
            WorkerReport(ok=True, summary="调研完成", evidence=[]),
            WorkerReport(ok=True, summary="页面做完了", evidence=[]),
        ],
        hook=hook,
    )
    models = ModelsQueue(replies=[_two_step_plan(), _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 2, "steps/1/ 里有文件就算交出了"
    assert "job_skipped" not in _kinds(mem_store, tid)


async def test_dep_other_task_path_does_not_count(mem_store, settings, env, tools, tasks, goals):
    """上游声称的是**别的任务**目录里存在的文件 → 不算交出（本任务目录才算）。"""
    tid = _create_task(tasks)
    _write_ws(env, tasks, tid, "artifacts/T-other/x.md", "别的任务的")
    workers = StepWorkers(results=[
        WorkerReport(ok=True, summary="调研完成", evidence=["artifacts/T-other/x.md"]),
    ])
    models = ModelsQueue(replies=[_two_step_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 1
    assert "task.job_skipped" in _kinds(mem_store, tid)


async def test_dep_empty_file_does_not_count(mem_store, settings, env, tools, tasks, goals):
    """上游声称的文件存在但是空的 → 不算交出。"""
    tid = _create_task(tasks)

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, f"artifacts/{tid}/research.md", "")

    workers = StepWorkers(
        results=[
            WorkerReport(ok=True, summary="调研完成", evidence=[f"artifacts/{tid}/research.md"]),
        ],
        hook=hook,
    )
    models = ModelsQueue(replies=[_two_step_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 1
    assert "task.job_skipped" in _kinds(mem_store, tid)


# ---------------------------------------------------------------------------
# C. 各步骤分文件夹（中间步骤只写自己的 steps/<步号>/）
# ---------------------------------------------------------------------------

_WS = "ws-1"


class _HostStub:
    async def messages(self, *a, **k):
        return []

    async def knowledge(self, *a, **k):
        return ""


def _file_setup(tmp_path: Path, mem_store: Store):
    from CharTyr_MaiWork.maiwork.config import load_settings

    s, _problems = load_settings(
        {"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")}}
    )
    env = LocalEnv(lambda: s)
    tools = Tools(mem_store)
    register_exec_tools(tools, env=env, host=_HostStub(), get_settings=lambda: s)
    ws = env.workspace(_WS)
    for rel, content in (
        ("artifacts/T-4/index.html", "<html>交付成品</html>"),
        ("artifacts/T-4/steps/1/research.md", "调研稿"),
        ("artifacts/T-4/steps/2/notes.md", "第二份中间稿"),
        ("artifacts/T-2/index.html", "<html>别的任务</html>"),
    ):
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tools, ws


def _ctx(ws: Path, *, scope=None, write_scope=None) -> ToolContext:
    return ToolContext(
        group_id=GID, task_id="T-4", actor="子 agent #1", role="worker", workspace=ws,
        artifact_scope=scope, write_scope=write_scope,
    )


async def test_write_scope_intermediate_only_own_steps_dir(tmp_path, mem_store):
    tools_, ws = _file_setup(tmp_path, mem_store)
    ctx = _ctx(ws, scope=("artifacts/T-4",), write_scope=("artifacts/T-4/steps/1",))

    ok = await tools_.call("write_file", {"path": "artifacts/T-4/steps/1/draft.md", "content": "x"}, ctx)
    assert ok.ok, ok.error
    assert (ws / "artifacts/T-4/steps/1/draft.md").exists()

    bad = await tools_.call("write_file", {"path": "artifacts/T-4/index.html", "content": "覆盖"}, ctx)
    assert not bad.ok
    assert "这一步只能写 artifacts/T-4/steps/1/" in bad.error
    assert (ws / "artifacts/T-4/index.html").read_text(encoding="utf-8") == "<html>交付成品</html>"

    other = await tools_.call("write_file", {"path": "artifacts/T-4/steps/2/hack.md", "content": "x"}, ctx)
    assert not other.ok
    assert not (ws / "artifacts/T-4/steps/2/hack.md").exists()


async def test_write_scope_intermediate_reads_whole_task_dir(tmp_path, mem_store):
    """中间步骤读仍可读整个本任务目录（只限制写）。"""
    tools_, ws = _file_setup(tmp_path, mem_store)
    ctx = _ctx(ws, scope=("artifacts/T-4",), write_scope=("artifacts/T-4/steps/1",))
    r = await tools_.call("read_file", {"path": "artifacts/T-4/index.html"}, ctx)
    assert r.ok and "交付成品" in r.output
    r = await tools_.call("list_files", {"path": "artifacts/T-4"}, ctx)
    assert r.ok and "index.html" in r.output


async def test_write_scope_delivery_cannot_write_steps_dir(tmp_path, mem_store):
    """交付步骤可写整个本任务目录，但不许写别的步骤的 steps/<n>/。"""
    tools_, ws = _file_setup(tmp_path, mem_store)
    ctx = _ctx(ws, scope=("artifacts/T-4",), write_scope=("artifacts/T-4",))

    ok = await tools_.call("write_file", {"path": "artifacts/T-4/index.html", "content": "新成品"}, ctx)
    assert ok.ok, ok.error
    assert (ws / "artifacts/T-4/index.html").read_text(encoding="utf-8") == "新成品"

    bad = await tools_.call("write_file", {"path": "artifacts/T-4/steps/1/research.md", "content": "改调研"}, ctx)
    assert not bad.ok
    assert "别的步骤" in bad.error
    assert (ws / "artifacts/T-4/steps/1/research.md").read_text(encoding="utf-8") == "调研稿"


async def test_write_scope_none_behaves_as_before(tmp_path, mem_store):
    """write_scope=None（老调用方）：行为完全不变。"""
    tools_, ws = _file_setup(tmp_path, mem_store)
    ctx = _ctx(ws, scope=("artifacts/T-4",), write_scope=None)
    r = await tools_.call("write_file", {"path": "artifacts/T-4/steps/1/x.md", "content": "x"}, ctx)
    assert r.ok, r.error


async def test_coordinator_marks_intermediate_step_and_write_scope(mem_store, settings, env, tools, tasks, goals):
    """中间步骤的 brief 写明产出目录 + 派活时带 write_scope；交付步骤 scope 是任务目录。"""
    tid = _create_task(tasks)

    class CaptureWorkers(StepWorkers):
        pass

    workers = CaptureWorkers(results=[
        WorkerReport(ok=True, summary="调研完成", evidence=[f"artifacts/{tid}/steps/1/research.md"]),
        WorkerReport(ok=True, summary="页面做完了", evidence=[]),
    ])

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, f"artifacts/{tid}/steps/1/research.md", "资料")

    workers.hook = hook
    models = ModelsQueue(replies=[_two_step_plan(), _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert len(workers.calls) == 2
    c1, c2 = workers.calls[0], workers.calls[1]
    assert c1["write_scope"] == (f"artifacts/{tid}/steps/1",), c1["write_scope"]
    assert f"artifacts/{tid}/steps/1/" in c1["brief"]
    assert c2["write_scope"] == (f"artifacts/{tid}",), c2["write_scope"]
    assert f"成品放在工作区 artifacts/{tid}/ 下" in c2["brief"]
    assert "这一步的产出写到工作区" not in c2["brief"]


async def test_delivery_step_write_scope_keeps_named_other_dir(mem_store, settings, env, tools, tasks, goals):
    """req 点名了别的任务目录时，交付步骤的 write_scope 仍然放行它（读写的既有行为不变）。"""
    tid = _create_task(tasks, req="接着上次 T-2 的页面，再补一段新的")
    workers = StepWorkers(results=[WorkerReport(ok=True, summary="做完了", evidence=[])])
    models = ModelsQueue(replies=[
        _plan_json([_job("补一段新的", ["write_file"])]),
        _review_json(pass_=True, review="过了"),
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    assert workers.calls[0]["write_scope"] == (f"artifacts/{tid}", "artifacts/T-2")


# ---------------------------------------------------------------------------
# D. 步骤工具补齐（搜索 / 抓正文 / write_file）
# ---------------------------------------------------------------------------


class _ReadOnlySpecialists:
    """只读岗桩（news 那类）：上限里只有搜索 / 抓正文，没有 write_file。"""

    def role_usable(self, kind):  # noqa: ARG002
        return True

    def effective_tools(self, kind, requested, profile=None):  # noqa: ARG002
        allowed = ("web_search", "fetch_page")
        out = [str(t) for t in (requested or []) if str(t) in allowed]
        if "submit_result" not in out:
            out.append("submit_result")
        return out


def _research_job(tools, *, after=None, agent=None):
    return _job("调研一下大家怎么看", tools, type_="research", after=after, agent=agent)


async def _run_check(coord, tid, jobs, criteria=None):
    return coord._exec_capability_self_check(  # noqa: SLF001
        tid, GID,
        {"criteria": list(criteria or ["有东西"]), "jobs": list(jobs)},
        on_remote=False, box=None,
    )


async def test_research_job_gets_search_extract_write(mem_store, settings, env, tools, tasks, goals):
    """调研活缺搜索 / write_file（抓正文已有）→ 按注册表补上，记 task.tool_autofill。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers())
    job = _research_job(["fetch_page"])

    report = await _run_check(coord, tid, [job])

    assert report.blocked is False, report.findings
    assert "web_search" in job["tools"]
    assert "write_file" in job["tools"]
    payloads = _payloads(mem_store, tid, "task.tool_autofill")
    filled = {str(p.get("tool")) for p in payloads}
    assert {"web_search", "write_file"} <= filled, payloads
    assert all(int(p.get("job") or 0) == 1 for p in payloads)
    assert "task.exec_unavailable" not in _kinds(mem_store, tid)


async def test_research_job_complete_needs_nothing(mem_store, settings, env, tools, tasks, goals):
    """调研活三样都有 → 一个都不补、不记事件。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers())
    job = _research_job(["web_search", "fetch_page", "write_file"])

    report = await _run_check(coord, tid, [job])

    assert report.blocked is False
    assert job["tools"] == ["web_search", "fetch_page", "write_file"]
    assert "task.tool_autofill" not in _kinds(mem_store, tid)


async def test_file_job_without_write_file_gets_it(mem_store, settings, env, tools, tasks, goals):
    """非调研活，brief 里写明要产出文件（artifacts/、.md）→ 补 write_file。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers())
    job = _job("把结论整理成 artifacts/T-1/report.md", ["read_file"], type_="other")

    report = await _run_check(coord, tid, [job])

    assert report.blocked is False
    assert "write_file" in job["tools"]
    assert any(str(p.get("tool")) == "write_file" for p in _payloads(mem_store, tid, "task.tool_autofill"))


async def test_plain_job_does_not_need_write_file(mem_store, settings, env, tools, tasks, goals):
    """brief 里没说产出文件、也不是调研 → 不补。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers())
    job = _job("跟群友打听一下这件事", ["read_file"], type_="other")

    report = await _run_check(coord, tid, [job])

    assert report.blocked is False
    assert job["tools"] == ["read_file"]
    assert "task.tool_autofill" not in _kinds(mem_store, tid)


async def test_research_job_missing_tools_blocks(mem_store, settings, env, tools, tasks, goals):
    """注册表里根本没有搜索 / 抓正文工具 → 补不了，blocked（走有界重排 → 暂停）。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers(),
                   stub_tools=False)
    job = _research_job(["fetch_page"])

    report = await _run_check(coord, tid, [job])

    assert report.blocked is True
    assert all(f.need in ("search", "extract") for f in report.blocked_findings)
    assert "task.tool_unavailable" in _kinds(mem_store, tid)
    # 搜索 / 抓正文补不了（只有 write_file 注册着，那个照补）
    filled = {str(p.get("tool")) for p in _payloads(mem_store, tid, "task.tool_autofill")}
    assert not (filled & {"web_search", "fetch_page"}), filled


async def test_file_job_write_file_unavailable_blocks(mem_store, settings, env, tools, tasks, goals):
    """write_file 没注册 → 要产出文件的活补不了 → blocked。"""
    tid = _create_task(tasks)

    async def _h(ctx, args):  # noqa: ARG001
        return ToolResult(ok=True, output="x")

    tools.register(Tool(
        name="read_file", description="桩", parameters={"type": "object", "properties": {}},
        roles=frozenset({"worker"}), handler=_h,
    ))
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers(),
                   stub_tools=False, register_exec=False)
    job = _job("把结论写成 artifacts/T-1/report.md", ["read_file"], type_="other")

    report = await _run_check(coord, tid, [job])

    assert report.blocked is True
    assert [f.need for f in report.blocked_findings] == ["write"]


async def test_research_job_cannot_widen_readonly_role(mem_store, settings, env, tools, tasks, goals):
    """只读岗（news 那类）拿不到 write_file → 不许硬塞，blocked。"""
    tid = _create_task(tasks)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=StepWorkers(),
                   specialists=_ReadOnlySpecialists())
    job = _research_job(["web_search", "fetch_page"], agent="news")

    report = await _run_check(coord, tid, [job])

    assert report.blocked is True
    assert [f.job for f in report.blocked_findings] == [1]
    assert "write_file" not in " ".join(job["tools"])
    note = " ".join(str(p.get("note") or "") for p in _payloads(mem_store, tid, "task.tool_unavailable"))
    assert "write_file" in note or "写文件" in note


# ---------------------------------------------------------------------------
# E. 返工复用已做好的步骤
# ---------------------------------------------------------------------------


def _maybe_deliver(env, tasks, tid: str, actor: str, rel: str, content: str = "资料"):
    """交回前在工作区写出文件（按 actor 决定谁写）。"""

    async def hook(actor_=actor):
        if actor_ == actor:
            _write_ws(env, tasks, tid, rel, content)

    return hook


def _one_step_plan(*, reuse=None, brief="调研一下大家怎么看", type_="research") -> str:
    job = _job(brief, ["web_search", "fetch_page", "write_file"], type_=type_)
    if reuse is not None:
        job["reuse"] = reuse
    return _plan_json([job])


async def test_step_result_saved_in_kv(mem_store, settings, env, tools, tasks, goals):
    """每条活跑完把结果存 kv task.step.<任务>.<步号>。"""
    tid = _create_task(tasks)
    rel = f"artifacts/{tid}/steps/1/research.md"
    workers = StepWorkers(
        results=[WorkerReport(ok=True, summary="调研好了", evidence=[rel], data={"k": 1})],
        hook=_maybe_deliver(env, tasks, tid, "子 agent #1", rel),
    )
    models = ModelsQueue(replies=[_one_step_plan(), _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    rec = mem_store.kv_get(f"task.step.{tid}.1")
    assert isinstance(rec, dict), rec
    assert rec["req_version"] == 1
    assert rec["attempt"] == 1
    assert rec["ok"] is True
    assert rec["delivered"] is True
    assert rec["paths"] == [rel]
    assert "调研好了" in rec["summary"]
    assert "调研一下大家怎么看" in rec["brief"]


async def test_plan_prompt_lists_previous_steps(mem_store, settings, env, tools, tasks, goals):
    """下一轮计划提示列出上一轮各步骤的状态和文件清单，并说明可以写 reuse。"""
    tid = _create_task(tasks)
    rel = f"artifacts/{tid}/steps/1/research.md"
    workers = StepWorkers(
        results=[WorkerReport(ok=True, summary="调研好了", evidence=[rel])],
        hook=_maybe_deliver(env, tasks, tid, "子 agent #1", rel),
    )
    models = ModelsQueue(replies=[
        _one_step_plan(), _review_json(pass_=False, review="还不行"),
        _one_step_plan(brief="把上一轮的资料整理成页面", type_="build"),
        _review_json(pass_=True, review="过了"),
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    prompts = _plan_prompts(models)
    assert len(prompts) == 2, prompts
    second = prompts[1]
    assert "上一轮" in second
    assert "第 1 条" in second
    assert "交出了" in second
    assert rel in second
    assert "reuse" in second


async def test_reuse_skips_worker_and_marks_event(mem_store, settings, env, tools, tasks, goals):
    """第二轮写 reuse: 1 且校验通过 → 不跑 worker，直接用存档结果，记 task.step_reused。"""
    tid = _create_task(tasks)
    rel = f"artifacts/{tid}/steps/1/research.md"
    workers = StepWorkers(
        results=[WorkerReport(ok=True, summary="调研好了：3 条动态", evidence=[rel])],
        hook=_maybe_deliver(env, tasks, tid, "子 agent #1", rel),
    )
    models = ModelsQueue(replies=[
        _one_step_plan(), _review_json(pass_=False, review="不够"),
        _one_step_plan(reuse=1, brief="接着上一轮的调研做页面", type_="build"),
        _review_json(pass_=True, review="过了"),
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    assert len(workers.calls) == 1, f"第二轮该复用，不该再跑子 agent：{workers.calls}"
    assert tasks.get(tid)["status"] == "completed"
    reused = _payloads(mem_store, tid, "task.step_reused")
    assert reused and int(reused[-1].get("reused") or 0) == 1
    rows = _attempt_rows(mem_store, tid)
    assert len(rows) == 2
    assert "调研好了：3 条动态" in str(rows[-1]["summary"])


async def test_reuse_validation_fails_runs_worker(mem_store, settings, env, tools, tasks, goals):
    """文件没了 / 校验不过 → 当普通活正常跑（不记 step_reused）。"""
    tid = _create_task(tasks)
    rel = f"artifacts/{tid}/steps/1/research.md"
    ws = env.workspace(tasks.get(tid)["workspace"])

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, rel, "资料")
        if actor == "子 agent #2":
            (ws / rel).unlink(missing_ok=True)

    workers = StepWorkers(
        results=[
            WorkerReport(ok=True, summary="调研好了", evidence=[rel]),
            WorkerReport(ok=True, summary="又做了一遍", evidence=[]),
        ],
        hook=hook,
    )
    models = ModelsQueue(replies=[
        _one_step_plan(), _review_json(pass_=False, review="不够"),
        _one_step_plan(reuse=1, brief="接着上一轮的调研做页面", type_="build"),
        _review_json(pass_=True, review="过了"),
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    # 第一轮交回后：文件还在 → 存档 delivered=True；第二轮之前把它删掉，模拟「文件没了」
    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001
    (ws / rel).unlink()
    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001

    assert len(workers.calls) == 2, "校验不过就要正常跑 worker"
    assert "task.step_reused" not in _kinds(mem_store, tid)


async def test_reuse_without_brief_uses_stored_brief(mem_store, settings, env, tools, tasks, goals):
    """reuse 的活没写 brief → 用存档里的 brief（校验不过要真跑时也不许空 brief）。"""
    tid = _create_task(tasks)
    rel = f"artifacts/{tid}/steps/1/research.md"
    ws = env.workspace(tasks.get(tid)["workspace"])

    async def hook(actor):
        if actor == "子 agent #1":
            _write_ws(env, tasks, tid, rel, "资料")

    workers = StepWorkers(
        results=[
            WorkerReport(ok=True, summary="调研好了", evidence=[rel]),
            WorkerReport(ok=True, summary="又做了一遍", evidence=[]),
        ],
        hook=hook,
    )
    plan2 = _plan_json([{"tools": ["write_file"], "type": "build", "reuse": 1}])
    models = ModelsQueue(replies=[_one_step_plan(), _review_json(pass_=False, review="不够"),
                                  plan2, _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001
    (ws / rel).unlink()  # 校验不过 → 正常跑
    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001

    assert len(workers.calls) == 2
    assert "调研一下大家怎么看" in workers.calls[1]["brief"], "空 brief 要用存档里的那份"


async def test_step_records_not_reusable_after_req_version_change(mem_store, settings, env, tools, tasks, goals):
    """需求改版（req_version 变了）→ 上一轮的步骤不算数，不复用。"""
    tid = _create_task(tasks)
    rel = f"artifacts/{tid}/steps/1/research.md"
    workers = StepWorkers(
        results=[
            WorkerReport(ok=True, summary="调研好了", evidence=[rel]),
            WorkerReport(ok=True, summary="按新需求又做了一遍", evidence=[]),
        ],
        hook=_maybe_deliver(env, tasks, tid, "子 agent #1", rel),
    )
    models = ModelsQueue(replies=[_one_step_plan(), _review_json(pass_=False, review="不够"),
                                  _one_step_plan(reuse=1, brief="按新需求接着做", type_="build"),
                                  _review_json(pass_=True, review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001
    tasks.revise(tid, req="改一下：再加上对比", criteria=["有东西"])
    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001

    assert len(workers.calls) == 2, "需求改版后不许复用旧版本的步骤"
    assert "task.step_reused" not in _kinds(mem_store, tid)


# ---------------------------------------------------------------------------
# F. 止损（workers.py）
# ---------------------------------------------------------------------------


class _ChatResult:
    def __init__(self, text="", tool_calls=None):
        self.text = text
        self.tool_calls = list(tool_calls or [])


class ScriptedModels:
    """按脚本回放工具调用；脚本用完就交回（submit_result）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple] = []

    def settings(self):
        class _S:
            def ready(self):
                return True

        return _S()

    async def chat(self, role=None, messages=None, **kwargs):
        self.calls.append((role, [dict(m) for m in (messages or [])], kwargs))
        if self.script:
            item = self.script.pop(0)
        else:
            item = {"name": "submit_result", "args": {"summary": "交回", "evidence": []}}
        if isinstance(item, BaseException):
            raise item
        name = str(item["name"])
        args = item.get("args") or {}
        return _ChatResult(tool_calls=[{
            "id": f"c{len(self.calls)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }])


def _f_tools(mem_store, *, fetch, search):
    """给 workers 单测用的工具表：fetch/search 的行为由调用方给，返回 (tools, counts)。"""
    t = Tools(mem_store)
    counts = {"fetch_page": 0, "web_search": 0}

    async def fetch_page(ctx, args):  # noqa: ARG001
        counts["fetch_page"] += 1
        return fetch(args)

    async def web_search(ctx, args):  # noqa: ARG001
        counts["web_search"] += 1
        return search(args)

    async def submit_result(ctx, args):  # noqa: ARG001
        return ToolResult(ok=True, output=str(args.get("summary") or ""), data={
            "summary": args.get("summary") or "",
            "data": args.get("data"),
            "evidence": args.get("evidence") or [],
        })

    for name, handler in (
        ("fetch_page", fetch_page), ("web_search", web_search), ("submit_result", submit_result),
    ):
        t.register(Tool(
            name=name, description="桩", parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=handler, timeout_s=5.0,
        ))
    return t, counts


def _tool_messages(models, index: int = -1) -> str:
    _role, msgs, _kw = models.calls[index]
    return "\n".join(str(m.get("content") or "") for m in msgs if m.get("role") == "tool")


def _all_messages(models, index: int = -1) -> str:
    _role, msgs, _kw = models.calls[index]
    return "\n".join(str(m.get("content") or "") for m in msgs)


def _tool_contents(models, index: int = -1) -> list[str]:
    _role, msgs, _kw = models.calls[index]
    return [str(m.get("content") or "") for m in msgs if m.get("role") == "tool"]


def _search_script(n: int):
    return [{"name": "web_search", "args": {"query": f"第 {i} 个问法"}} for i in range(n)]


async def test_fetch_host_blocked_after_two_failures(mem_store):
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):
        return ToolResult(ok=False, output="", error="页面返回 403")

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, counts = _f_tools(mem_store, fetch=fetch, search=search)
    models = ScriptedModels([
        {"name": "fetch_page", "args": {"url": "https://bad.example/1"}},
        {"name": "fetch_page", "args": {"url": "https://bad.example/2"}},
        {"name": "fetch_page", "args": {"url": "https://bad.example/3"}},
    ])
    w = Workers(models, tools_)
    report = await w.run("查资料", group_id=GID, tools=["fetch_page", "web_search"], task_id="T-1")

    assert report.ok is True
    assert counts["fetch_page"] == 2, "第三次该被短路，不再真去请求"
    assert "已经连续拒绝 2 次" in _tool_messages(models)
    assert "换别的来源" in _tool_messages(models)
    # 短路也要照常落一条 tool_calls
    rows = mem_store.read().execute(
        "SELECT ok FROM tool_calls WHERE tool='fetch_page' AND task_id='T-1' ORDER BY id"
    ).fetchall()
    assert len(rows) == 3, [dict(r) for r in rows]
    assert [int(r["ok"]) for r in rows] == [0, 0, 0]


async def test_fetch_host_failures_counted_per_host(mem_store):
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):
        if "bad.example" in str(args.get("url") or ""):
            return ToolResult(ok=False, output="", error="403")
        return ToolResult(ok=True, output="正文", data={})

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, counts = _f_tools(mem_store, fetch=fetch, search=search)
    models = ScriptedModels([
        {"name": "fetch_page", "args": {"url": "https://bad.example/1"}},
        {"name": "fetch_page", "args": {"url": "https://bad.example/2"}},
        {"name": "fetch_page", "args": {"url": "https://good.example/1"}},
        {"name": "fetch_page", "args": {"url": "https://bad.example/3"}},
    ])
    w = Workers(models, tools_)
    await w.run("查资料", group_id=GID, tools=["fetch_page", "web_search"], task_id="T-1")

    # bad 拦住了，good 没被连坐：真请求 3 次（bad 两次 + good 一次）
    assert counts["fetch_page"] == 3, counts
    contents = _tool_contents(models)
    assert "正文" in contents[2], "另一个主机不该被连坐"
    assert "已经连续拒绝 2 次" not in contents[2]
    assert "已经连续拒绝 2 次" in contents[3], "bad 第三次请求要短路"


async def test_fetch_host_success_resets_failure_count(mem_store):
    """「连续拒绝」：同一网站失败一次、成功一次、再失败一次，不该被拉黑。"""
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):
        if str(args.get("url") or "").endswith("/ok"):
            return ToolResult(ok=True, output="正文", data={})
        return ToolResult(ok=False, output="", error="超时")

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, counts = _f_tools(mem_store, fetch=fetch, search=search)
    models = ScriptedModels([
        {"name": "fetch_page", "args": {"url": "https://flaky.example/a"}},
        {"name": "fetch_page", "args": {"url": "https://flaky.example/ok"}},
        {"name": "fetch_page", "args": {"url": "https://flaky.example/b"}},
        {"name": "fetch_page", "args": {"url": "https://flaky.example/c"}},
    ])
    w = Workers(models, tools_)
    await w.run("查资料", group_id=GID, tools=["fetch_page", "web_search"], task_id="T-1")

    assert counts["fetch_page"] == 4, "成功一次后失败计数要清零，第 3、4 次都该真去请求"
    assert "已经连续拒绝" not in _tool_messages(models)


async def test_no_progress_nudges_at_15(mem_store):
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):  # noqa: ARG001
        return ToolResult(ok=True, output="正文", data={})

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, _counts = _f_tools(mem_store, fetch=fetch, search=search)
    models = ScriptedModels(_search_script(15))
    w = Workers(models, tools_)
    report = await w.run("找资料", group_id=GID, tools=["web_search", "fetch_page"], task_id="T-1")

    assert report.ok is True
    assert "已经连续 15 次没有新收获" in _all_messages(models), "第 15 次没有新收获要插一条 user 提醒"


async def test_no_progress_stops_at_25(mem_store):
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):  # noqa: ARG001
        return ToolResult(ok=True, output="正文", data={})

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, _counts = _f_tools(mem_store, fetch=fetch, search=search)
    models = ScriptedModels(_search_script(25))
    w = Workers(models, tools_)
    report = await w.run("找资料", group_id=GID, tools=["web_search", "fetch_page"], task_id="T-1")

    assert report.ok is True
    assert "已经连续 25 次没有新收获" in _all_messages(models)
    # 第 26 次调用（25 次搜索之后）：只给 submit_result
    _role, _msgs, kw = models.calls[25]
    names = [s["function"]["name"] for s in (kw.get("tools") or [])]
    assert names == ["submit_result"], names


async def test_progress_resets_no_progress_counter(mem_store):
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):  # noqa: ARG001
        return ToolResult(ok=True, output="正文", data={})

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, _counts = _f_tools(mem_store, fetch=fetch, search=search)
    script = _search_script(14) + [
        {"name": "fetch_page", "args": {"url": "https://new.example/x"}},
    ] + _search_script(14)
    models = ScriptedModels(script)
    w = Workers(models, tools_)
    report = await w.run("找资料", group_id=GID, tools=["web_search", "fetch_page"], task_id="T-1")

    assert report.ok is True
    assert "已经连续 15 次没有新收获" not in _all_messages(models), "中间有过一次新打开的地址，计数要清零"
    assert "已经连续 25 次没有新收获" not in _all_messages(models)


async def test_stall_coexists_with_repeat_call_nudger(mem_store):
    """同一参数连用照旧被 RepeatCallNudger 提醒（两条提醒并存）。"""
    from CharTyr_MaiWork.maiwork.workers import Workers

    def fetch(args):  # noqa: ARG001
        return ToolResult(ok=True, output="正文", data={})

    def search(args):  # noqa: ARG001
        return ToolResult(ok=True, output="结果", data=[])

    tools_, _counts = _f_tools(mem_store, fetch=fetch, search=search)
    models = ScriptedModels([{"name": "web_search", "args": {"query": "同一个问法"}}] * 15)
    w = Workers(models, tools_)
    await w.run("找资料", group_id=GID, tools=["web_search", "fetch_page"], task_id="T-1")

    blob = _all_messages(models)
    assert "拿同样的参数" in blob, "原来的重复调用提醒要照旧"
    assert "已经连续 15 次没有新收获" in blob


# ---------------------------------------------------------------------------
# G. 客观做不到 → 问发起人，不再整轮重跑
# ---------------------------------------------------------------------------

_REQ_ITEMS = [{"text": "拿到某网站的独家数据并做成一页", "origin": "原话", "kind": "实做"}]


def _blocked_plan() -> str:
    return _plan_json(
        [_job("去那个网站把数据拿下来", ["web_search", "fetch_page", "write_file"])],
        requirements=_REQ_ITEMS,
    )


def _items_review(*, met: bool, blocked=None, evidence: str = "试过三个入口都返回 403，换了两家镜像也一样") -> str:
    item: dict = {"id": "R1", "met": met, "evidence": evidence}
    if blocked is not None:
        item["blocked"] = blocked
    return _review_json(pass_=False, review="做不到", items=[item, {"id": "R0", "met": True, "evidence": "没编造"}])


def _blocked_task(tasks: Tasks) -> str:
    return _create_task(
        tasks, req="拿到某网站的独家数据并做成一页",
        requester_id="10001", requester_name="小明",
    )


async def test_blocked_ask_only_from_second_attempt(mem_store, settings, env, tools, tasks, goals):
    """第一次即使全部 blocked 也先返工；第二次才问发起人。"""
    tid = _blocked_task(tasks)
    workers = StepWorkers(results=[
        WorkerReport(ok=True, summary="拿不到", evidence=[]),
        WorkerReport(ok=True, summary="还是拿不到", evidence=[]),
    ])
    review = _items_review(met=False, blocked="来源拒绝访问：整站 403")
    models = ModelsQueue(replies=[_blocked_plan(), review, _blocked_plan(), review])
    outbox = FakeOutbox()
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers, outbox=outbox)

    await _run_once(coord, tasks, tid)
    assert tasks.get(tid)["status"] == "queued", "第一次一律先返工"
    assert "task.blocked_ask" not in _kinds(mem_store, tid)
    assert not [e for e in outbox.enqueued if str(e["key"]).startswith("ask:")]

    await _run_once(coord, tasks, tid)
    task = tasks.get(tid)
    assert task["status"] == "waiting_input", task["status"]
    question = str(task.get("question") or "")
    assert "这几条做不到" in question
    assert "R1" in question
    assert "来源拒绝访问" in question
    assert "要按现在做到的部分交付，还是换个要求？" in question
    asks = [e for e in outbox.enqueued if str(e["key"]).startswith(f"ask:{tid}:")]
    assert asks, outbox.enqueued
    assert asks[-1]["payload"]["push_kind"] == "status"
    assert asks[-1]["payload"]["text"].startswith("@小明 ")
    assert "task.blocked_ask" in _kinds(mem_store, tid)
    row = _attempt_rows(mem_store, tid)[-1]
    assert row["status"] == "waiting"


async def test_blocked_ask_waits_for_all_unmet_items_blocked(mem_store, settings, env, tools, tasks, goals):
    """有一条没做到的必须项没写 blocked → 不提问，照旧返工。"""
    tid = _blocked_task(tasks)
    workers = StepWorkers(results=[
        WorkerReport(ok=True, summary="拿不到", evidence=[]),
        WorkerReport(ok=True, summary="还是拿不到", evidence=[]),
    ])

    def review_mixed():
        return _review_json(pass_=False, review="做不到", items=[
            {"id": "R1", "met": False, "evidence": "试过三个入口都 403", "blocked": "来源拒绝访问"},
            {"id": "R0", "met": False, "evidence": "做了点占位"},
        ])

    models = ModelsQueue(replies=[_blocked_plan(), review_mixed(), _blocked_plan(), review_mixed()])
    outbox = FakeOutbox()
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers, outbox=outbox)

    await _run_once(coord, tasks, tid)
    await _run_once(coord, tasks, tid)

    assert tasks.get(tid)["status"] == "queued"
    assert "task.blocked_ask" not in _kinds(mem_store, tid)
    assert not [e for e in outbox.enqueued if str(e["key"]).startswith("ask:")]


async def test_blocked_ask_needs_evidence_of_attempts(mem_store, settings, env, tools, tasks, goals):
    """blocked 写了但证据没说明尝试过什么 → 不提问。"""
    tid = _blocked_task(tasks)
    workers = StepWorkers(results=[
        WorkerReport(ok=True, summary="拿不到", evidence=[]),
        WorkerReport(ok=True, summary="还是拿不到", evidence=[]),
    ])
    review = _items_review(met=False, blocked="来源拒绝访问", evidence="没做到")
    models = ModelsQueue(replies=[_blocked_plan(), review, _blocked_plan(), review])
    outbox = FakeOutbox()
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers, outbox=outbox)

    await _run_once(coord, tasks, tid)
    await _run_once(coord, tasks, tid)

    assert tasks.get(tid)["status"] == "queued"
    assert "task.blocked_ask" not in _kinds(mem_store, tid)


async def test_review_prompt_explains_blocked(mem_store, settings, env, tools, tasks, goals):
    """验收提示里要说明 blocked 只写客观原因。"""
    tid = _blocked_task(tasks)
    workers = StepWorkers(results=[WorkerReport(ok=True, summary="拿不到", evidence=[])])
    models = ModelsQueue(replies=[_blocked_plan(), _items_review(met=False, blocked="来源拒绝访问")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await _run_once(coord, tasks, tid)

    prompt = "\n".join(
        str(m.get("content") or "")
        for _r, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.review"
        for m in msgs
    )
    assert "blocked" in prompt
    assert "来源拒绝访问" in prompt
    assert "没有权限" in prompt or "没人参与" in prompt
