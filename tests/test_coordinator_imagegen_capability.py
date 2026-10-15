"""排计划知道能画图（2026-10-15 任务 A3）：

- 配好了生图模型（`use="image"` 的条目 + 端点有地址密钥）→ 排计划提示里多一句
  「能用 generate_image 画照片 / 插画 / 封面 / 概念图；数字图表别用它」，交付形态也认 image；
- 没配 → 一个字都不提（不许吹没有的能力）；
- 任务明显要「画图 / 生图 / 配图 / 海报 / 插画」而没配生图模型 → 和现有能力闸一样：
  记 `task.exec_unavailable`（need=image）+ 中文原因（去设置里加生图模型），有界重排后暂停；
- 配了 + 注册表真有 generate_image → 给这条活补上（记 `task.tool_autofill`）。

用真 Store / 真 Agents / 真 Tools / 真 LocalEnv，模型用队列桩、Workers 用抓取桩，不调真模型。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools

from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    CaptureWorkers,
    FakeOutbox,
    FakeDelivery,
    FakeProfiles,
    ModelsQueue,
    _add_worker_tool,
    _coordinator,
    _events,
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
from CharTyr_MaiWork.maiwork.coordinator import Coordinator, job_needs_image_capability
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.tasks import Tasks

pytestmark = pytest.mark.asyncio

IMG_ENTRY = {
    "id": "img1", "endpoint": "default", "model": "gpt-image-1", "name": "生图",
    "use": "image", "image_api": "openai", "image_max_n": 2,
}
DRAW_BRIEF = "帮群友画一张封面图，存到 artifacts/T-1/ 下"


def _settings_with_image(settings, *, api_key: str = "sk-image-key"):
    """把生图条目接进现有 settings：一个端点（有地址和密钥）+ 一条 use="image" 的条目。"""
    import dataclasses

    from CharTyr_MaiWork.maiwork.config import EndpointSetting, ModelEntry

    eps = tuple(settings.endpoints)
    if not eps:
        eps = (EndpointSetting(
            id="default", name="默认", protocol="openai",
            base_url="https://api.test/v1", api_key=api_key,
        ),)
    entry = ModelEntry(
        id="img1", endpoint=eps[0].id, model="gpt-image-1", name="生图",
        use="image", image_api="openai", image_max_n=2,
    )
    return dataclasses.replace(settings, endpoints=eps, model_list=(*settings.model_list, entry))


def _imagegen_tool(tools: Tools, name: str = "generate_image") -> None:
    async def _h(ctx, args):
        return ToolResult(ok=True, output="x")

    tools.register(Tool(
        name=name, description="t", parameters={"type": "object"},
        roles=frozenset({"worker"}), handler=_h,
    ))


def _plan_prompts(models: ModelsQueue) -> str:
    return "\n".join(
        m.get("content") or ""
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.plan"
        for m in msgs
        if isinstance(m.get("content"), str)
    )


def _coord_with_settings(store, settings, env, tools, agents, workers, models):
    """和共用 _coordinator 一样，但 get_settings 换成这一份（带 / 不带生图模型）。"""
    coord = Coordinator(
        store, models, workers, tools, Tasks(store, lambda: settings),
        Goals(store, lambda: settings), FakeDelivery(), FakeOutbox(), env,
        FakeProfiles(), lambda: settings,
    )
    coord._specialists = _make_specialists(agents, workers)  # noqa: SLF001
    return coord


# ----------------------------------------------------------------------
# 排计划提示：配了才提
# ----------------------------------------------------------------------


async def test_plan_prompt_mentions_imagegen_when_configured(
    store, settings, env, tools, agents
):
    with_img = _settings_with_image(settings)
    models = ModelsQueue(replies=['{"criteria":["x"],"deliver_kind":"view","jobs":[{"brief":"做一页","tools":["write_file"]}]}'])
    workers = CaptureWorkers()
    coord = _coord_with_settings(store, with_img, env, tools, agents, workers, models)

    tasks = Tasks(store, lambda: settings)
    tid = tasks.create(GID, title="画图", req="画一张封面", criteria=["画一张封面图"], source="test")
    await coord._plan(tasks.get(tid))  # noqa: SLF001

    prompt = _plan_prompts(models)
    assert "generate_image" in prompt
    assert "画" in prompt and "数字" in prompt
    assert '"view|file|text|image"' in prompt


async def test_plan_prompt_says_nothing_when_not_configured(
    store, settings, env, tools, agents
):
    """没配生图模型：提示词里一个字都不许提生图（不吹没有的能力）。"""
    models = ModelsQueue(replies=['{"criteria":["x"],"deliver_kind":"view","jobs":[{"brief":"做一页","tools":["write_file"]}]}'])
    workers = CaptureWorkers()
    coord = _coord_with_settings(store, settings, env, tools, agents, workers, models)

    tasks = Tasks(store, lambda: settings)
    tid = tasks.create(GID, title="画图", req="画一张封面", criteria=["画一张封面图"], source="test")
    await coord._plan(tasks.get(tid))  # noqa: SLF001

    prompt = _plan_prompts(models)
    assert "generate_image" not in prompt
    assert "生图" not in prompt
    # 交付形态本身照样认 image（能不能做由能力闸管）
    assert "image" in prompt


# ----------------------------------------------------------------------
# 开工前能力闸：要画图但没配生图模型
# ----------------------------------------------------------------------


async def test_needs_image_without_model_is_blocked_with_chinese_reason(
    store, settings, env, tools, agents
):
    # 工具一直注册着（生产里 register_image_tools 挂在注册表上），缺的是「没配生图模型」
    _imagegen_tool(tools)
    workers = CaptureWorkers()
    specialists = _make_specialists(agents, workers)
    coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
    plan = {"criteria": ["画一张封面图"], "jobs": [_job(["write_file"], brief=DRAW_BRIEF)]}

    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    kinds = _kinds(store, "T-1")
    assert "task.exec_unavailable" in kinds
    note = _note_for(store, "T-1", "task.exec_unavailable")
    assert "生图模型" in note and "设置" in note
    assert "task.tool_autofill" not in kinds
    assert "generate_image" not in plan["jobs"][0]["tools"]


async def test_needs_image_without_model_blocks_the_report(store, settings, env, tools, agents):
    """blocked=True：run_attempt 据此先重排一次、再不行暂停（和现有能力闸同一条路）。"""
    _imagegen_tool(tools)
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["画一张封面图"], "jobs": [_job(["write_file"], brief=DRAW_BRIEF)]}
    report = coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001
    assert report.blocked is True
    assert any(f.need == "image" for f in report.blocked_findings)
    assert "生图" in report.pause_reason()


async def test_needs_image_with_model_autofills_generate_image(
    store, settings, env, agents
):
    """配了生图模型 + 注册表真有 generate_image → 给这条活补上，记 tool_autofill。"""
    with_img = _settings_with_image(settings)
    tools = Tools(store)
    _add_worker_tool(tools, "write_file")
    _imagegen_tool(tools)
    workers = CaptureWorkers()
    specialists = _make_specialists(agents, workers)
    coord = _coordinator(store, with_img, env, tools, agents, workers, specialists=specialists)
    plan = {"criteria": ["画一张封面图"], "jobs": [_job(["write_file"], brief=DRAW_BRIEF)]}

    report = coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001

    assert report.blocked is False
    assert "generate_image" in plan["jobs"][0]["tools"]
    assert "task.tool_autofill" in _kinds(store, "T-1")
    assert "generate_image" in report.filled_tools


async def test_criteria_only_drawing_word_does_not_block_other_deliveries(
    store, settings, env, tools, agents
):
    """完成标准里顺带提一句「画一张结果图」（补充 / 加分项）不把 view / text 任务卡住。"""
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["把投票发起来", "画一张结果图（加分项）"], "deliver_kind": "view",
            "jobs": [_job(["write_file"], brief="做一页投票页")]}
    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001
    assert "task.exec_unavailable" not in _kinds(store, "T-1")


async def test_criteria_drawing_word_blocks_image_delivery(store, settings, env, tools, agents):
    """交付形态就是图片（deliver_kind=image）时，完成标准里的画图要求照样拦。"""
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["两张封面图"], "deliver_kind": "image",
            "jobs": [_job(["write_file"], brief="把成品图整理到 artifacts/T-1/images/ 下")]}
    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001
    # 这条活的 brief 没提画图 → 不拦；交付形态是图片但图是现成的，也不该拦
    assert "task.exec_unavailable" not in _kinds(store, "T-1")
    plan2 = {"criteria": ["画两张封面图"], "deliver_kind": "image",
             "jobs": [_job(["write_file"], brief="按需求把图做出来")]}
    coord._exec_capability_self_check("T-1", GID, plan2, on_remote=False)  # noqa: SLF001
    assert "task.exec_unavailable" in _kinds(store, "T-1")


async def test_image_words_do_not_fire_on_plain_picture_words(store, settings, env, tools, agents):
    """「把图片整理好」不是「画图」：别把整理图片的活也拦下来。"""
    workers = CaptureWorkers()
    coord = _coordinator(store, settings, env, tools, agents, workers,
                         specialists=_make_specialists(agents, workers))
    plan = {"criteria": ["两张图都落到 artifacts/T-1/images/"],
            "jobs": [_job(["write_file"], brief="把两张成品图整理到 artifacts/T-1/images/ 下")]}
    coord._exec_capability_self_check("T-1", GID, plan, on_remote=False)  # noqa: SLF001
    assert "task.exec_unavailable" not in _kinds(store, "T-1")


# ----------------------------------------------------------------------
# 「要画图」判据（纯函数，2026-10-15 复核修正）：图表不算生图，封面 / 插画照算
# ----------------------------------------------------------------------


async def test_job_needs_image_ignores_charts() -> None:
    """图表 / 流程图 / 思维导图是确定性渲染的活：不能因为句子里有这些就要生图模型。

    老判据（裸子串）把「做一个图表」「生成图表」「画图表」都判成要生图 → 没配生图模型时
    一条本该出图表的活会被能力闸拦下暂停，原因还写着「去设置加生图模型」。
    2026-10-15 复核补两条同一类漏判：`绘制图表`（词尾不是「图」的说法，负向前瞻看错了位置）
    与 `画一张流程图 / 画一张思维导图`（只有量词的说法只挡了「图表」两个字）。
    """
    for text in (
        "做一个图表，把三个模型的价格对比清楚",
        "生成图表",
        "画图表",
        "画一张图表",
        "绘制图表",
        "绘制思维导图",
        "手绘流程图",
        "画一张流程图",
        "画一张思维导图",
        "画一张导图",
        "生成流程图",
        "画流程图",
        "做一个数据图表",
        "做一个流程图",
        "做一个思维导图",
        "做一张表格",
        "做一个网页，图表用表格呈现",
        "把这三张封面图下载下来保存到本地",
        "把图片整理好",
    ):
        assert job_needs_image_capability(text) is False, text


async def test_job_needs_image_still_catches_real_drawing() -> None:
    """真正要画的说法一个都不能漏（负向前瞻只看紧跟的那几个字，不整句跳过）。"""
    for text in (
        "画一张封面图",
        "画一张图片",
        "画一张图",
        "绘制一张封面",
        "来张插画",
        "出图给群友看",
        "生成图片",
        "海报设计",
        "整理一份插画集",
        "画出来",
        "配图",
        "手绘一张",
        "做一个概念图",
    ):
        assert job_needs_image_capability(text) is True, text


async def test_job_needs_image_chart_and_cover_in_one_sentence() -> None:
    """同一句里既有图表 / 流程图又要封面：仍然算要生图（不是「见图表整句跳过」）。"""
    assert job_needs_image_capability("做一个图表，再画一张封面") is True
    assert job_needs_image_capability("画一张图表，再画一张封面") is True
    assert job_needs_image_capability("画一张流程图，再画一张封面") is True
    assert job_needs_image_capability("绘制图表，再画一张封面") is True
