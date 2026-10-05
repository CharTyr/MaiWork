"""任务双岗协作第三步（docs/20 §四、§九）：干活的可以对说明提异议（challenge）。

- submit_result 多一个可选 challenge {reason, evidence, suggestion}；没写理由当没提。
- Workers 把它放进 WorkerReport.challenge；
- 领队验收时看到异议，必须表态（challenge_ok）；记 task.lane_challenge 事件（采纳比例靠它算）；
- 领队记录里记得这次异议。
- 排计划时告诉领队这一轮干活的是哪个模型（弱模型说明要写细）。
"""

from __future__ import annotations

import json

import pytest

from tests.test_coordinator import (  # noqa: F401  (fixtures)
    GID,
    _create_task,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)
from tests.test_coordinator_lanes import BAD, GOOD, TEXT_PLAN, LaneModels, LaneSpecialists, _kinds, _setup

CH = {"reason": "要求的官方下载页已经下线", "evidence": ["https://a.com/404"], "suggestion": "改用镜像站"}


class ChallengeSpecialists(LaneSpecialists):
    async def run(self, kind, brief, **kw):
        rep = await super().run(kind, brief, **kw)
        if len(self.calls) == 1:
            rep.challenge = dict(CH)
        return rep


def _review_with(ok, pass_=True):
    return json.dumps({"pass": pass_, "review": "通过" if pass_ else "没过：换镜像站重做", "missing": [],
                       "artifact": "", "note": "", "challenge_ok": ok}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_lead_sees_challenge_and_decides(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, _review_with(True)])
    coord, _ = _setup(mem_store, settings, env, tools, tasks, goals, models)
    coord._specialists = ChallengeSpecialists()  # noqa: SLF001
    await coord.run_task(tid)
    prompt = [c[1] for c in models.calls if c[2].get("purpose") == "coordinator.review"][0][-1]["content"]
    assert "要求的官方下载页已经下线" in prompt and "改用镜像站" in prompt
    assert "challenge_ok" in prompt
    ev = [p for k, p in _kinds(mem_store, tid) if k == "task.lane_challenge"]
    assert len(ev) == 1 and ev[0]["accepted"] is True and "采纳" in ev[0]["note"]


@pytest.mark.asyncio
async def test_rejected_challenge_recorded_and_remembered(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, _review_with(False, pass_=False), TEXT_PLAN, GOOD])
    coord, _ = _setup(mem_store, settings, env, tools, tasks, goals, models)
    coord._specialists = ChallengeSpecialists()  # noqa: SLF001
    await coord.run_task(tid)
    ev = [p for k, p in _kinds(mem_store, tid) if k == "task.lane_challenge"]
    assert ev[0]["accepted"] is False and "没采纳" in ev[0]["note"]
    second_plan = [c[1] for c in models.calls if c[2].get("purpose") == "coordinator.plan"][1]
    assert "要求的官方下载页已经下线" in json.dumps(second_plan[:-1], ensure_ascii=False)


@pytest.mark.asyncio
async def test_no_challenge_no_prompt_section(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, GOOD])
    coord, _ = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    prompt = [c[1] for c in models.calls if c[2].get("purpose") == "coordinator.review"][0][-1]["content"]
    assert "challenge_ok" not in prompt
    assert not [k for k, _ in _kinds(mem_store, tid) if k == "task.lane_challenge"]


class LabelModels(LaneModels):
    def model_label(self, kind, *, escalate=False):
        return "强模型" if escalate else "快模型"


@pytest.mark.asyncio
async def test_plan_prompt_names_worker_model(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LabelModels([TEXT_PLAN, BAD, TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _ = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    plans = [c[1][-1]["content"] for c in models.calls if c[2].get("purpose") == "coordinator.plan"]
    assert "「快模型」" in plans[0] and "写细" in plans[0]
    assert "「强模型」" in plans[2], "被打回两次的那一轮告诉领队换成了谁"
