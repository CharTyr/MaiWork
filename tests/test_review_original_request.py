"""验收（coordinator._review）必须看群友原始需求的回归测试（2026-10 本地修复）。

已核实的缺口：`_review` 的提示词里只放了 `task.title` 和 `plan.criteria`，没放
`task.req`。于是任务 T9 的原话「在群内发起投票选定首开线路，统计结果并公示开档
时间与打卡规则」被计划降成一张静态页（没有投票流水），验收照样通过。

这里守住这几件事：
1. 每一轮验收的提示词都带**当前** `task.req` 原文（原样、不截断）；
2. 提示词明说 criteria / 子 agent 建议只是细化，不能删改用户硬性要求；
3. 原则是**有条件的**（2026-10 复核整改）：原需求点名要实际操作 / 真实统计时，
   方案、说明、占位不能替代实做，缺能力 / 缺人参与不算完成，没做 / 没数据不是通过
   理由；原需求本来只要调研 / 设计 / 方案 / 纯文字时，就按原需求评这份文稿本身，
   不许反过来强加它没要求的操作、参与或上线。两头都不许自己改口径。
4. 领队 lane（lead=True）必须把这一轮的提示存进前情，且新一轮的提示带当前版需求
   ——旧 req 不能替代当前 req；不自动改旧任务状态。

断言的是**假模型真实收到的 messages**（FakeModels 捕获），不是源码文本。
不靠「投票」关键词硬编码，所以下面也钉住提示词里不出现这类词。
"""

from __future__ import annotations


import pytest

from CharTyr_MaiWork.maiwork.lanes import TaskLanes
from tests.test_coordinator import (  # noqa: F401  (fixtures)
    GID,
    FakeWorkers,
    ModelsQueue,
    _build,
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
from tests.test_coordinator_lanes import LaneModels, _setup

pytestmark = pytest.mark.asyncio

# 证据里的 T9 原话；加长 + 换行，尾巴有结束标记，确认没有被任何截断吞掉。
T9_REQ = (
    "在群内发起投票选定首开线路，统计结果并公示开档时间与打卡规则。\n"
    "要求：投票要真的在群里发起并能收到群友的选择；结束后把每条线路的得票数、"
    "参与人数统计出来，做成群里点得开的网页公示；同时写清开档时间和打卡规则。"
    "\n\n" + ("补充说明：细则以群友在这条需求里写的为准，不许改动。\n" * 60)
    + "【需求结束标记-END】"
)

NEW_REQ = "改成：只在群里公示开档时间，不做投票。" + "（改版说明）" * 30 + "【新需求结束标记-END】"

PLAIN_PLAN = {"criteria": ["包含链接"], "deliver_kind": "text", "jobs": []}


def _review_user_texts(models: ModelsQueue) -> list[str]:
    """每次验收真正发给模型的最末一条 user 消息（提示词都在这里）。"""
    out = []
    for _role, messages, kwargs in models.calls:
        if kwargs.get("purpose") == "coordinator.review":
            out.append(str(messages[-1].get("content") or ""))
    return out


class FakeModels(ModelsQueue):
    """按顺序回放验收 JSON，并保留每次 chat 的 messages 快照（父类已经保留）。"""


async def _one_review(coord, tasks_obj, tid, *, lead=False, plan=None) -> dict:
    return await coord._review(
        tasks_obj.get(tid),
        dict(plan or PLAIN_PLAN),
        "子 agent 说做完了",
        ["evidence-1"],
        [],
        lead=lead,
    )


# ---------------------------------------------------------------------------
# 1. 每轮验收都带当前 req 原文
# ---------------------------------------------------------------------------


async def test_review_prompt_carries_original_request_verbatim(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    models = FakeModels(replies=[_review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await _one_review(coord, tasks, tid)

    prompts = _review_user_texts(models)
    assert len(prompts) == 1
    prompt = prompts[0]
    assert T9_REQ in prompt, "验收提示必须原样带上当前 task.req"
    assert "【需求结束标记-END】" in prompt, "原文末尾的标记没被截断"
    assert len(T9_REQ) > 1000 and "…（截断）" not in prompt, "需求原文不许截断"
    assert "真源" in prompt, "要说清 req 是验收的真源"


async def test_review_prompt_does_not_treat_criteria_as_the_request(
    mem_store, settings, env, tools, tasks, goals
):
    """criteria 只放「完成标准」段，且被明说只是细化、不能替代硬性要求。"""
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    models = FakeModels(replies=[_review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await _one_review(coord, tasks, tid, plan={"criteria": ["做一张静态页"], "deliver_kind": "text", "jobs": []})

    prompt = _review_user_texts(models)[0]
    assert "原始需求" in prompt and "完成标准" in prompt
    assert "细化" in prompt and "不能删改" in prompt, "criteria / 子 agent 建议只是细化"
    assert "硬性要求" in prompt, "用户硬性要求不能被删改"
    # 缺能力 / 缺人参与 / 只给方案 / 占位 不能当完成
    assert "缺能力" in prompt and "占位" in prompt
    assert "已经做完" in prompt or "已完成" in prompt
    # 没执行、没数据不是通过理由，要按原要求看实际证据
    assert "不是通过的理由" in prompt
    assert "实际" in prompt and "证据" in prompt
    # 不做关键词硬编码：验收规则里不许出现「投票」这类词（原文里有是群友写的，不算）
    assert "投票" not in prompt.replace(T9_REQ, ""), "不许靠「投票」之类的关键词硬编码"


async def test_review_prompt_tolerates_missing_req_field(
    mem_store, settings, env, tools, tasks, goals
):
    """老调用方可能传没有 req 键的 task（兼容）；空需求写「（空）」，不抛异常。"""
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    models = FakeModels(replies=[_review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    bare = dict(tasks.get(tid))
    bare.pop("req", None)
    review = await coord._review(bare, dict(PLAIN_PLAN), "总结", [], [])
    assert review["pass"] is True
    prompt = _review_user_texts(models)[0]
    assert "（空）" in prompt


# ---------------------------------------------------------------------------
# 2. 验收不改旧任务状态（不自动改旧 task）
# ---------------------------------------------------------------------------


async def test_review_does_not_touch_task_state_or_req(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    before = tasks.get(tid)
    models = FakeModels(replies=[_review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await _one_review(coord, tasks, tid)
    after = tasks.get(tid)
    assert after["status"] == before["status"]
    assert after["req"] == before["req"]
    assert int(after["req_version"]) == int(before["req_version"])


# ---------------------------------------------------------------------------
# 3. 真派活路径：验收拿到的是库里的当前 req
# ---------------------------------------------------------------------------


async def test_run_task_review_prompt_uses_current_task_req(
    mem_store, settings, env, tools, tasks, goals
):
    settings.is_served = lambda group_id: True
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    plan = _plan(
        criteria=["群里有能点开的投票页"],
        deliver_kind="text",
        jobs=[{"brief": "发起投票并统计", "tools": ["write_file", "list_files"]}],
    )
    models = FakeModels(replies=[plan, _review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    prompt = _review_user_texts(models)[0]
    assert T9_REQ in prompt
    assert "群里有能点开的投票页" in prompt


# ---------------------------------------------------------------------------
# 4. 领队 lane：当前 req 进提示、进前情，旧 req 不能替代
# ---------------------------------------------------------------------------


async def test_lead_review_keeps_current_req_after_revision(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    models = LaneModels(replies=[_review(pass_=False, review="没过：没有投票流水", artifact=""),
                                 _review(pass_=True, artifact="")])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)

    # 第一轮验收（需求第 1 版）
    await _one_review(coord, tasks, tid, lead=True)
    # 需求改版（第 2 版）：领队记录保留、干活 lane 关掉
    assert tasks.revise(tid, req=NEW_REQ, criteria=["包含链接"]) == 2
    # 第二轮验收（需求第 2 版）
    await _one_review(coord, tasks, tid, lead=True)

    prompts = _review_user_texts(models)
    assert len(prompts) == 2
    assert T9_REQ in prompts[0]
    assert NEW_REQ in prompts[1], "第二轮提示带当前版需求原文"
    assert T9_REQ not in prompts[1], "当前 req 不许被旧 req 顶替"

    # 领队 lane 真把这一轮的提示存进了前情：下一轮前情里就有当前 req
    row = TaskLanes(mem_store).load(tid, "lead", group_id=GID)
    assert row is not None and row["status"] == "open"
    assert int(row["req_version"]) == 2
    saved = [str(m.get("content") or "") for m in row["messages"]]
    assert any(NEW_REQ in text for text in saved), "本轮提示必须进前情"
    assert any(T9_REQ in text for text in saved), "旧前情原样保留，但最新一条是当前 req"


async def test_lead_review_without_history_still_sends_current_req(
    mem_store, settings, env, tools, tasks, goals
):
    """第一次领队验收没有前情，照样原样带当前 req。"""
    tid = _create_task(tasks, title="开档", req=T9_REQ)
    models = LaneModels(replies=[_review(pass_=True, artifact="")])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await _one_review(coord, tasks, tid, lead=True)
    prompts = _review_user_texts(models)
    assert len(prompts) == 1
    assert T9_REQ in prompts[0]
    assert "（上面是你在这个任务里" not in prompts[0], "没有前情不该插前情说明"


# ---------------------------------------------------------------------------
# 5. 条件边界一：原需求只要方案 / 纯文字，不许被强加实做
# ---------------------------------------------------------------------------

PLAN_ONLY_REQ = (
    "先给我一份上线方案：写清步骤、风险和回滚办法。只要一份文字说明，不要真上线，"
    "也不用别人跟着操作，评审过了我自己安排。\n"
    + "（补充：按书面材料评审就行，不要求任何落地动作。）\n" * 20
    + "【只要方案-END】"
)


async def test_review_prompt_plan_only_request_is_not_forced_into_real_actions(
    mem_store, settings, env, tools, tasks, goals
):
    """原需求只要方案 / 纯文字时：按这份文稿本身评，不能强加未要求的操作 / 参与 / 上线。"""
    tid = _create_task(tasks, title="上线方案", req=PLAN_ONLY_REQ)
    models = FakeModels(replies=[_review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await _one_review(
        coord, tasks, tid,
        plan={"criteria": ["方案写清步骤、风险和回滚"], "deliver_kind": "text", "jobs": []},
    )

    prompt = _review_user_texts(models)[0]
    assert PLAN_ONLY_REQ in prompt, "原文照给"
    # 条件边界一：只要方案 / 纯文字 → 评这份文稿本身
    assert "评这份文稿" in prompt, "只要方案 / 文字时按这份文稿评"
    assert "强加" in prompt and "没要求的操作" in prompt, "不许强加未要求的操作 / 参与 / 上线"
    # 反向：不许把「只要方案」拔高成必须真操作
    assert "拔高" in prompt, "不许把只要方案的需求拔高成必须真操作"
    # 实做那半条还在，但被条件限定住，不是无条件套在方案类需求上
    assert "不能替代实做" in prompt and "原始需求要实做时" in prompt
    # 不做关键词硬编码（这条 req 里没有「投票」）
    assert "投票" not in prompt


# ---------------------------------------------------------------------------
# 6. 条件边界二：原需求点名要真做，方案 / 说明 / 占位不能替代
# ---------------------------------------------------------------------------

REAL_WORK_REQ = (
    "逐个私聊群友收集空闲时间，把真收到的回复统计成结果页在群里公示。\n"
    + "（补充：数字必须是真收到回复后数出来的，不能用示例或估算顶替。）\n" * 20
    + "【实做需求-END】"
)


async def test_review_prompt_real_work_request_cannot_be_downgraded_to_plan(
    mem_store, settings, env, tools, tasks, goals
):
    """原需求点名要真做（收集 / 统计 / 公示）时：方案、说明、占位不能替代实做。"""
    tid = _create_task(tasks, title="统计空闲时间", req=REAL_WORK_REQ)
    models = FakeModels(replies=[_review(pass_=True, artifact="")])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )
    await _one_review(
        coord, tasks, tid,
        plan={"criteria": ["结果页在群里公示"], "deliver_kind": "text", "jobs": []},
    )

    prompt = _review_user_texts(models)[0]
    assert REAL_WORK_REQ in prompt, "原文照给"
    assert "原始需求要实做时" in prompt, "实做那半条要被条件限定住"
    assert "不能替代实做" in prompt, "只写方案 / 说明 / 占位不能替代实做"
    assert "降成方案" in prompt, "不许把点名的实做降成方案 / 说明"
    assert "不是通过的理由" in prompt, "没做 / 没数据不是通过理由"
    assert "缺能力" in prompt and "占位" in prompt
    # 两道反向门都在：不许降标准，也不许给方案类需求加码
    assert "拔高" in prompt
