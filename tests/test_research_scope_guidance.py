"""T-10 巡检回归：调研范围按原话；打开链接不是事实正确的证明。

模型桩只验证真正发给计划/验收模型的提示，不冒称语义判定或线上费用已改善。
"""
from __future__ import annotations

import pytest

from tests.test_coordinator import (
    FakeWorkers, ModelsQueue, _build, _create_task, _plan, _review,
    env, fixed_clock, goals, mem_store, settings, tasks, tools,
)

pytestmark = pytest.mark.asyncio


def _coordinator(mem_store, settings, env, tools, tasks, goals, models):
    return _build(mem_store=mem_store, settings=settings, env=env, tools=tools,
                  tasks=tasks, goals=goals, models=models, workers=FakeWorkers())


@pytest.mark.parametrize("req", ["搜搜这条银行新闻，是真的吗？", "详细调研，做一个有时间轴的单页网页"])
async def test_planning_scales_to_original_request(mem_store, settings, env, tools, tasks, goals, req):
    models = ModelsQueue([_plan(deliver_kind="text")])
    coord = _coordinator(mem_store, settings, env, tools, tasks, goals, models)
    tid = _create_task(tasks, req=req)
    await coord._plan(tasks.get(tid))
    prompt = models.calls[0][1][-1]["content"]
    assert req in prompt
    assert "调研范围与交付形式按原始需求" in prompt
    assert "不默认升级成全景报告" in prompt
    assert "用户明确要求深入调研、完整盘点或网页时，仍按原话做到" in prompt
    assert "关键问题有可靠依据、未确认的点已说明，就停止扩搜" in prompt


@pytest.mark.parametrize("research", [False, True])
async def test_review_checks_evidence_not_link_count(mem_store, settings, env, tools, tasks, goals, research):
    models = ModelsQueue([_review(artifact="")])
    coord = _coordinator(mem_store, settings, env, tools, tasks, goals, models)
    tid = _create_task(tasks, req="搜搜这条新闻")
    plan = {"criteria": ["核实事实"], "deliver_kind": "text", "research": research}
    await coord._review(tasks.get(tid), plan, "调查结论", [], [])
    prompt = models.calls[0][1][-1]["content"]
    assert "打开过只证明取得过材料，不证明结论正确" in prompt
    assert "不同站点转载同一篇稿件，不算独立佐证" in prompt
    assert "置信程度、时间点或讨论对象不同，不自动等于事实互相矛盾" in prompt
    assert "开头写已证实、后文却说关联未知" in prompt
    assert "不能为了补充项或链接数量要求整轮返工" in prompt


async def test_locked_requirements_are_not_changed_by_scope_guidance(mem_store, settings, env, tools, tasks, goals):
    from CharTyr_MaiWork.maiwork import requirements
    tid = _create_task(tasks, req="完整盘点并制作手机网页")
    task = tasks.get(tid)
    locked = requirements.normalize_requirements(
        [{"text": "完整盘点并制作手机网页", "origin": "原话", "kind": "实做"}], task["req"]
    )
    requirements.save(mem_store, tid, int(task.get("req_version") or 1), locked)
    models = ModelsQueue([_plan(deliver_kind="view")])
    coord = _coordinator(mem_store, settings, env, tools, tasks, goals, models)
    await coord._plan(task)
    assert requirements.load(mem_store, tid, int(task.get("req_version") or 1)) == locked
    prompt = models.calls[0][1][-1]["content"]
    assert "用户明确要求深入调研、完整盘点或网页时，仍按原话做到" in prompt
    assert "不许增删、不许改字" in prompt


async def test_research_framework_distinguishes_source_independence():
    from CharTyr_MaiWork.maiwork.workers import RESEARCH_REPORT_FRAMEWORK
    assert "不同站点转载同一来源，不算独立佐证" in RESEARCH_REPORT_FRAMEWORK
    assert "六段是组织框架，不是扩搜配额" in RESEARCH_REPORT_FRAMEWORK
    assert "置信程度或时间点不同，不自动等于事实矛盾" in RESEARCH_REPORT_FRAMEWORK
    assert "关联未知的机构，不写成已证实由同一攻击者所为" in RESEARCH_REPORT_FRAMEWORK
    # 保留已定的框架，而不是为了缩短活擅自删掉。
    for section in ("一句话结论", "大家都同意的", "有分歧的", "吐槽最多的", "新冒头的", "没人提的"):
        assert section in RESEARCH_REPORT_FRAMEWORK
    assert "≥2 个不同站点支持" not in RESEARCH_REPORT_FRAMEWORK
