"""任务详情写短（2026-10-01 用户：「任务写的好长」）。

- 完成标准：3 到 5 条、每条一句话不超过 30 字、只写能检查的那一点；上次验收要返工的点
  写进 jobs 的 brief，不写进完成标准。代码兜底只留前 5 条。
- 验收意见：第一句先给结论，全段不超过 150 字，只说没过的地方；通过就一两句。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from test_task_isolation import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    _coord, env, fixed_clock, goals, mem_store, settings, tasks, tools,
)
from test_coordinator import FakeWorkers, ModelsQueue, _create_task, _review

pytestmark = pytest.mark.asyncio


def _prompts(models: ModelsQueue, purpose: str) -> str:
    return "\n".join(
        m.get("content") or ""
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == purpose
        for m in msgs
        if isinstance(m.get("content"), str)
    )


def _plan(criteria: list[str]) -> str:
    return json.dumps(
        {"criteria": criteria, "deliver_kind": "text",
         "jobs": [{"brief": "做", "tools": ["fetch_page"]}], "question": None},
        ensure_ascii=False,
    )


async def test_plan_prompt_asks_short_criteria(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(["有东西"])])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await coord._plan(tasks.get(tid), prior_review="没过：链接没打开")
    p = _prompts(models, "coordinator.plan")
    assert "3 到 5 条" in p
    assert "30 字" in p
    assert "返工" in p and "brief" in p and "不要写进完成标准" in p


async def test_plan_keeps_at_most_five_criteria(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan([f"标准 {i}" for i in range(1, 8)])])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["criteria"] == ["标准 1", "标准 2", "标准 3", "标准 4", "标准 5"]


async def test_review_prompt_asks_short_conclusion_first(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(["有东西"]), _review(pass_=True, artifact="", review="通过")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await asyncio.wait_for(coord.run_task(tid), timeout=10)
    p = _prompts(models, "coordinator.review")
    assert "150 字" in p
    assert "第一句先写结论" in p
    assert "只说没过的地方" in p
