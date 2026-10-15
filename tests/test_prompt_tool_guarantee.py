"""提示词点名的工具保证拿得到（2026-10-15 任务 B）。

背景：几处提示词会点名工具——子 agent 的 system 提示说「要细看就用 read_skill 读」、
调研活的六段框架说「read_skill 读 research-report / 用 render_report 交网页」、
view 活的 brief 说「用 render_report 生成 index.html」——但 kind=task 的子 agent 的
工具名单 = 计划 `job["tools"]` + submit_result，主模型没写就没有，提示词等于空头支票。

两条修法（双保险）：
1. **自动补工具**：开工前能力自检里，凡是提示词会点名的工具（read_skill / render_report /
   generate_image），只要注册表真有、岗位上限放行，就补进这条活的名单（记 `task.tool_autofill`）；
   岗位上限压掉的（news/idea/goal 只读岗）不硬塞。
2. **提示词跟着名单走**：名单里没有那个工具时，提示词就不提它的名字（workers 的 system
   提示 / 调研框架、coordinator 的 brief）。

用真 Store / 真 Agents / 真 Tools / 真 LocalEnv，模型用队列桩、Workers 用抓取桩。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
from CharTyr_MaiWork.maiwork.workers import RESEARCH_REPORT_FRAMEWORK, Workers, research_report_framework

from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    CaptureWorkers,
    ModelsQueue,
    _add_worker_tool,
    _coordinator,
    _job,
    _kinds,
    _make_specialists,
    _note_for,
    agents,
    env,
    fixed_clock,
    settings,
    store,
    tools,
)
from test_coordinator import (  # noqa: F401
    FakeWorkers,
    _build,
    _create_task,
    goals,
    mem_store,
    tasks,
)
from test_workers import FakeChatResult, ReplayModels, _tool_call

pytestmark = pytest.mark.asyncio


class FakeSkills:
    """skills 目录桩：list(role) 回一份 worker skill（read_skill 才有意义）。"""

    def __init__(self, names=("research-report",)) -> None:
        self._names = list(names)

    def list(self, role="worker"):
        return [
            {"name": n, "description": "怎么做事", "roles": ["worker"]} for n in self._names
        ]


# ======================================================================
# 1. 自动补工具
# ======================================================================


async def test_view_job_gets_render_report_autofilled(store, settings, env, tools, agents):
    """view 活：计划没写 render_report，开工前补上（子 agent 最终名单里真有）。"""
    workers = CaptureWorkers()
    specialists = _make_specialists(agents, workers)
    coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
    plan = {"criteria": ["包含链接"], "deliver_kind": "view",
            "jobs": [_job(["write_file"], brief="做一页总结")]}

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert "render_report" in plan["jobs"][0]["tools"]
    # 真跑活时子 agent 拿到的那份名单（Specialists.effective_tools 同一入口）里也有
    effective = specialists.effective_tools("task", plan["jobs"][0]["tools"])
    assert "render_report" in effective
    assert "task.tool_autofill" in _kinds(store, "T-1")
    assert "render_report" in _note_for(store, "T-1", "task.tool_autofill")


async def test_research_job_gets_render_report_autofilled(store, settings, env, tools, agents):
    """调研活（system 提示会点名 render_report 交网页）：同样补上。"""
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["给出结论"], "deliver_kind": "file",
            "jobs": [_job(["web_search", "fetch_page", "write_file"], brief="查一下大家怎么看",
                          agent="task")]}
    plan["jobs"][0]["type"] = "research"

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert "render_report" in plan["jobs"][0]["tools"]


async def test_read_skill_autofilled_when_worker_skills_exist(store, settings, env, tools, agents):
    """有可用 worker skill 时：system 提示会点名 read_skill → 补进名单。"""
    _add_worker_tool(tools, "read_skill")
    tools.skill_registry = FakeSkills()  # type: ignore[attr-defined]
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["包含链接"], "deliver_kind": "view",
            "jobs": [_job(["write_file"], brief="做一页总结")]}

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert "read_skill" in plan["jobs"][0]["tools"]


async def test_read_skill_not_autofilled_without_worker_skills(store, settings, env, tools, agents):
    """没有可用 worker skill：不补（system 提示也不会提 read_skill）。"""
    _add_worker_tool(tools, "read_skill")
    tools.skill_registry = FakeSkills(names=())  # type: ignore[attr-defined]
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["包含链接"], "deliver_kind": "view",
            "jobs": [_job(["write_file"], brief="做一页总结")]}

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert "read_skill" not in plan["jobs"][0]["tools"]


async def test_not_registered_tool_is_not_filled(store, settings, env, agents):
    """注册表里没有 render_report / read_skill：一个字都不往计划里塞（不留假工具）。"""
    bare = Tools(store)
    _add_worker_tool(bare, "write_file")
    bare.skill_registry = FakeSkills()  # type: ignore[attr-defined]
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, bare, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["包含链接"], "deliver_kind": "view",
            "jobs": [_job(["write_file"], brief="做一页总结")]}

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert plan["jobs"][0]["tools"] == ["write_file"]
    assert "task.tool_autofill" not in _kinds(store, "T-1")


async def test_news_role_does_not_get_render_report(store, settings, env, tools, agents):
    """只读调研岗（news）的上限里没有 render_report：不硬塞（不越权放大岗位）。"""
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["给出结论"], "deliver_kind": "view",
            "jobs": [_job(["web_search", "fetch_page", "write_file"], brief="查一下大家怎么看",
                          agent="news")]}

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert "render_report" not in plan["jobs"][0]["tools"]


# ======================================================================
# 2. 提示词跟着名单走
# ======================================================================


async def test_worker_system_prompt_omits_read_skill_when_tool_absent(store, tools):
    """没注册 read_skill（名单里没有）→ system 提示不出现 read_skill。"""
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"})]),
    ])
    w = Workers(models, tools)
    await w.run("查一下", group_id=GID, tools=["web_search"],
                skills_hint="- research-report：调研怎么做事")

    _role, msgs, _kw = models.calls[0]
    system = msgs[0]["content"]
    assert "read_skill" not in system
    # skill 清单照给（名字和说明本身有用），只是不提那个读它的工具
    assert "research-report" in system


async def test_worker_system_prompt_mentions_read_skill_when_tool_present(store, tools):
    _add_worker_tool(tools, "read_skill")
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"})]),
    ])
    w = Workers(models, tools)
    await w.run("查一下", group_id=GID, tools=["web_search", "read_skill"],
                skills_hint="- research-report：调研怎么做事")

    _role, msgs, _kw = models.calls[0]
    assert "read_skill" in msgs[0]["content"]


async def test_worker_system_prompt_without_skills_hint_has_no_read_skill(store, tools):
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"})]),
    ])
    w = Workers(models, tools)
    await w.run("查一下", group_id=GID, tools=["web_search"], skills_hint="")
    _role, msgs, _kw = models.calls[0]
    assert "read_skill" not in msgs[0]["content"]


async def test_research_framework_names_only_available_tools() -> None:
    """调研六段框架：read_skill / render_report 只在真有那个工具时才点名。"""
    full = research_report_framework()
    assert "read_skill" in full and "render_report" in full
    assert RESEARCH_REPORT_FRAMEWORK == full  # 常量照旧是「都有」的那份（老测试不变）
    no_render = research_report_framework(read_skill=True, render_report=False)
    assert "render_report" not in no_render and "read_skill" in no_render
    no_read = research_report_framework(read_skill=False, render_report=True)
    assert "read_skill" not in no_read and "render_report" in no_read
    neither = research_report_framework(read_skill=False, render_report=False)
    assert "read_skill" not in neither and "render_report" not in neither
    # 六段骨架一个字都不少
    for seg in ("一句话结论", "大家都同意的", "有分歧的", "吐槽最多的", "新冒头的", "没人提的"):
        assert seg in neither


async def test_run_job_system_extra_follows_the_tool_list(
    mem_store, settings, env, tools, tasks, goals
):
    """调研活的 system_extra：名单里没有 render_report / read_skill 时就不提它们的名字。"""
    tid = _create_task(tasks, req="查一下", reply=None)
    workers = FakeWorkers()
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=Tools(mem_store), tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=workers,
    )
    ws_name = str(tasks.get(tid)["workspace"])

    await coord._run_job(  # noqa: SLF001
        brief="查一下大家怎么看", tools=["web_search"], gid=GID, tid=tid,
        job_idx=1, ws_name=ws_name, job_type="research",
    )
    extra = str(workers.calls[0]["system_extra"])
    assert "一句话结论" in extra  # 框架本身照给
    assert "read_skill" not in extra and "render_report" not in extra

    workers.calls.clear()
    await coord._run_job(  # noqa: SLF001
        brief="查一下大家怎么看", tools=["web_search", "read_skill", "render_report"],
        gid=GID, tid=tid, job_idx=1, ws_name=ws_name, job_type="research",
    )
    extra2 = str(workers.calls[0]["system_extra"])
    assert "read_skill" in extra2 and "render_report" in extra2


async def test_enrich_brief_view_names_render_report_only_when_available(
    mem_store, settings, env, tools, tasks, goals
):
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=Tools(mem_store), tasks=tasks,
        goals=goals, models=ModelsQueue(), workers=FakeWorkers(),
    )
    with_tool = coord._enrich_brief("做一页总结", "T-9", "view", tools=("write_file", "render_report"))
    assert "render_report" in with_tool
    without = coord._enrich_brief("做一页总结", "T-9", "view", tools=("write_file",))
    assert "render_report" not in without
    assert "index.html" in without  # 做法照讲，只是不点名拿不到的工具
