"""R7 检查间隔上下限：`Coordinator._clamp_check_hours`（纯静态方法）。

缺失 / 非数 / ≤0 / NaN / inf → 24；<1 → 1；>72 → 72；其余原值。
最后一条用 check_goal 端到端确认真的走了这个夹取。
"""

from __future__ import annotations

import json
import math

import pytest

from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from test_coordinator import (  # noqa: F401  (fixtures / stubs)
    GID,
    NOW,
    FakeOutbox,
    FakeWorkers,
    ModelsQueue,
    _build,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)

def clamp(*args):
    """延迟取属性：方法还没实现时是测试失败（不是收集错误）。"""
    return Coordinator._clamp_check_hours(*args)


class TestClampCheckHours:
    def test_missing_and_bad_values_fall_back_to_24(self) -> None:
        assert clamp(None) == 24.0
        assert clamp() == 24.0            # 模型没给这个键
        assert clamp("abc") == 24.0
        assert clamp("") == 24.0
        assert clamp([]) == 24.0

    def test_zero_and_negative_fall_back_to_24(self) -> None:
        assert clamp(0) == 24.0
        assert clamp(0.0) == 24.0
        assert clamp(-5) == 24.0
        assert clamp("-5") == 24.0

    def test_nan_and_inf_fall_back_to_24(self) -> None:
        assert clamp(float("nan")) == 24.0
        assert clamp(float("inf")) == 24.0
        assert clamp(float("-inf")) == 24.0

    def test_below_one_hour_becomes_one(self) -> None:
        assert clamp(0.2) == 1.0
        assert clamp(0.999) == 1.0

    def test_above_72_becomes_72(self) -> None:
        assert clamp(500) == 72.0
        assert clamp(72.5) == 72.0

    def test_in_range_kept(self) -> None:
        assert clamp(6) == 6.0
        assert clamp(1) == 1.0
        assert clamp(72) == 72.0
        assert clamp("12") == 12.0
        assert isinstance(clamp(6), float)

    def test_result_is_finite_float(self) -> None:
        for v in (None, "abc", 0, -5, 0.2, 6, 500, float("nan"), float("inf")):
            out = clamp(v)
            assert isinstance(out, float) and math.isfinite(out)


@pytest.mark.asyncio
async def test_check_goal_clamps_model_hours(
    mem_store, settings, env, tools, tasks, goals
):
    """模型回 500 小时 → 目标的下次检查被夹到 72 小时。"""
    goal_id = goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
    models = ModelsQueue(replies=[json.dumps({
        "done_criteria": [], "next_check_hours": 500, "progress": None,
        "new_task": None, "report": None,
    }, ensure_ascii=False)])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=FakeOutbox(),
    )
    await coord.check_goal(goal_id)

    assert goals.get(goal_id)["next_check_ts"] == NOW + 72 * 3600
