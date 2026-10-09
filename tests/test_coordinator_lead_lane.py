"""任务双岗协作第二步（docs/20 §三、§5.2）：领队 lane + 同一条活多步派。

- 领队记得自己前几轮排的计划和验收结论：整段对话原样往后接（用户 2026-10-05 定「做缓存，
  换别的主模型会用上」）——每次请求都是上一次请求的原样延长，计划和验收用同一张工具表，
  身份那一大段只发一次；太长了先压成提要再接着接。每轮的规矩仍现算，放在最后一条提示里。
- 领队和干活的对话分开：干活的看不到领队的提示，领队看不到干活的工具过程。
- 需求改版：干活 lane 重开，领队记录保留，下一轮提示里写明需求改过。
- 验收时领队可以回 next：按交回的结果给某条活派下一步（同一条 lane 接着干），
  每轮最多 2 步，不算没过。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork.lanes import TaskLanes
from tests.test_coordinator import (  # noqa: F401  (fixtures)
    GID,
    _create_task,
    _plan,
    _review,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)
from tests.test_coordinator_lanes import BAD, GOOD, TEXT_PLAN, LaneModels, _kinds, _setup

pytestmark = pytest.mark.asyncio


def _next(job=1, brief="按调研结果做页面", review="还没到验收：先按调研结果做页面") -> str:
    return json.dumps(
        {"pass": False, "review": review, "missing": [], "artifact": "", "note": "",
         "next": [{"job": job, "brief": brief}]},
        ensure_ascii=False,
    )


def _calls(models, purpose):
    return [c[1] for c in models.calls if c[2].get("purpose") == purpose]


async def test_lead_remembers_previous_plan_and_review(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    first, second = _calls(models, "coordinator.plan")
    assert len(first) == 1, "第一轮没有前情"
    earlier = second[:-1]
    assert earlier, "第二轮带着领队自己的前情"
    text = json.dumps(earlier, ensure_ascii=False)
    assert "查资料做一页" in text, "记得自己上一轮派了什么活"
    assert "缺下载链接" in text, "记得上一轮验收为什么没过"
    assert "你是 MaiWork 的主模型" in second[-1]["content"], "规矩每轮现算，放在最后一条"
    # 验收也带着这一轮的计划
    reviews = _calls(models, "coordinator.review")
    assert "查资料做一页" in json.dumps(reviews[-1][:-1], ensure_ascii=False)


async def test_lead_and_worker_conversations_are_separate(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    worker_text = json.dumps([c["history"] for c in spec.calls], ensure_ascii=False)
    assert "你是 MaiWork 的主模型" not in worker_text
    assert "缺下载链接" not in worker_text.replace("没过：缺下载链接", ""), "验收意见只经说明转达"
    lead_text = json.dumps([m for c in models.calls for m in c[1]], ensure_ascii=False)
    assert "第 1 轮做完了" not in lead_text, "领队看不到干活的对话过程"


async def test_lead_lane_saved_while_running_and_cleared_at_end(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    seen = {}
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    orig = spec.run

    async def spying_run(kind, brief, **kw):
        row = TaskLanes(mem_store).load(tid, "lead", group_id=GID)
        seen.setdefault("rows", []).append(row and len(row["messages"]))
        return await orig(kind, brief, **kw)

    spec.run = spying_run
    await coord.run_task(tid)
    assert seen["rows"][0] and seen["rows"][1] > seen["rows"][0], "领队记录随轮次变长"
    row = TaskLanes(mem_store).load(tid, "lead", group_id=GID)
    assert row["messages"] == [] and row["status"] == "closed"


async def test_revise_keeps_lead_and_notes_change(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks, req="做一页 A 的总结")
    models = LaneModels([TEXT_PLAN, BAD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001
    tasks.revise(tid, req="改成做 B 的总结", criteria=[])
    models.reply_queue[:] = [TEXT_PLAN, GOOD]
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    last_plan = _calls(models, "coordinator.plan")[-1]
    earlier = json.dumps(last_plan[:-1], ensure_ascii=False)
    assert "做一页 A 的总结" in earlier, "领队记得改之前的需求"
    assert "需求改过" in last_plan[-1]["content"]
    assert "改成做 B 的总结" in last_plan[-1]["content"]


async def test_next_step_goes_to_same_lane_without_new_attempt(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, _next(), GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    assert int(tasks.get(tid)["attempts"]) == 1, "派下一步不算一次没过"
    assert len(_calls(models, "coordinator.plan")) == 1
    assert len(spec.calls) == 2
    assert "按调研结果做页面" in spec.calls[1]["brief"]
    assert any(m.get("content") == "第 1 轮做完了" for m in spec.calls[1]["history"]), "同一条 lane 接着干"
    assert spec.calls[1]["escalate"] is False
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.lane_next" in kinds
    # 第二次验收记得自己派过下一步
    second_review = _calls(models, "coordinator.review")[-1]
    assert "按调研结果做页面" in json.dumps(second_review[:-1], ensure_ascii=False)


async def test_next_steps_capped_per_attempt(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, _next(brief="第二步"), _next(brief="第三步"), _next(brief="第四步"),
                         TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    briefs = [c["brief"] for c in spec.calls]
    assert not any("第四步" in b for b in briefs), "一轮最多派 2 个下一步"
    assert sum(1 for b in briefs if "第二步" in b or "第三步" in b) == 2
    assert tasks.get(tid)["status"] == "completed"
    assert int(tasks.get(tid)["attempts"]) == 2, "派满了还要下一步 → 按没过返工"


async def test_next_with_bad_job_index_counts_as_not_passed(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, _next(job=5), TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert len(spec.calls) == 2
    assert int(tasks.get(tid)["attempts"]) == 2


async def test_review_prompt_offers_next(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    prompt = _calls(models, "coordinator.review")[0][-1]["content"]
    assert '"next"' in prompt


def _lead_calls(models):
    return [c for c in models.calls if c[2].get("purpose") in ("coordinator.plan", "coordinator.review")]


async def test_each_lead_call_extends_previous_one(mem_store, settings, env, tools, tasks, goals):
    """缓存友好：每次请求的开头和上一次请求一字不差，工具表也不变。"""
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, _next(), GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    calls = _lead_calls(models)
    assert len(calls) == 5
    for prev, cur in zip(calls, calls[1:]):
        assert cur[1][: len(prev[1])] == prev[1], "这次请求是上一次请求的原样延长"
        assert len(cur[1]) > len(prev[1])
    assert all(c[2].get("tools") == calls[0][2].get("tools") for c in calls), "计划和验收用同一张工具表"
    assert calls[0][2].get("tools"), "有工具表"
    assert not any(c[2].get("json_mode") for c in calls), "不在开头插 JSON 提示"


async def test_identity_prefix_sent_once(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    ident = {"v": "【身份】我是这个群的 MaiWork，本群记忆：喜欢简洁。\n"}
    coord._identity_prefix = lambda gid, with_memory=True: ident["v"]  # noqa: SLF001
    orig_review = coord._review  # noqa: SLF001

    async def review_and_change(*a, **kw):
        out = await orig_review(*a, **kw)
        if int(tasks.get(tid)["attempts"]) == 1:
            ident["v"] = "【身份】我是这个群的 MaiWork，本群记忆：喜欢图多。\n"
        return out

    coord._review = review_and_change  # noqa: SLF001
    await coord.run_task(tid)
    calls = _lead_calls(models)
    lasts = [c[1][-1]["content"] for c in calls]
    assert lasts[0].startswith("【身份】") and "喜欢简洁" in lasts[0]
    assert "喜欢简洁" not in lasts[1] and "没变" in lasts[1], "没变就不再发一遍"
    assert "喜欢图多" in lasts[2], "变了就发新的"
    assert "喜欢图多" not in lasts[3]


async def test_long_lead_lane_compacted_once(mem_store, settings, env, tools, tasks, goals, monkeypatch):
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 50)
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    second_plan = _calls(models, "coordinator.plan")[1]
    earlier = second_plan[:-1]
    assert any("试过不行的" in str(m.get("content") or "") for m in earlier), "压成一条提要再接"
    assert len(earlier) < 5, "老的那段被提要替代，不是原样全量重发"
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.lane_compact" in kinds


async def test_lead_compact_failure_keeps_full_history(mem_store, settings, env, tools, tasks, goals, monkeypatch):
    """领队前情压缩没做成：完整历史接着用（不是旧提要、不是空），另记失败事件。"""
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 50)
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD], compact_fail=True)
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed", "压不下去不卡任务"
    second_plan = _calls(models, "coordinator.plan")[1]
    earlier = json.dumps(second_plan[:-1], ensure_ascii=False)
    assert "查资料做一页" in earlier, "完整历史接着用（不是从零）"
    assert "缺下载链接" in earlier, "上一轮验收意见也在前情里"
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.lane_compact_failed" in kinds and "task.lane_compact" not in kinds
