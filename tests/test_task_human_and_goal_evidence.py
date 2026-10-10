"""docs/22 §5 第三期：需要真人参与 → 等人并提醒发起人；目标打勾要证据。

覆盖：
- A 清单里有 `kind=真人` 时计划提示写清「子 agent 只能准备材料、不能假装有人参与」；
  `_review` 清单模式多返回 `human_items`（没做到的必须项里带 needs_human 且去空白 ≥6 字）；
  本轮没过 + 没做到的必须项**全部**是真人 → waiting_input + 发件箱一条 @发起人
  （push_kind=status、key 前缀 `human:`）+ attempt 记 waiting + kv `task.human_wait.<tid>`；
  第一次尝试也等；混有非真人未做到 → 照常返工；needs_human 太短不算。
- B `resume`：有 human_wait → 清单不重拆、版本更新、步骤存档改版本可 reuse、
  kv 删除、事件 `task.human_resumed`；没有 human_wait → 照旧重拆；
  第二次进 human_wait → question 前缀「还差一点：」。
- C `check_goal`：done_criteria 新格式 + 证据校验（任务完成 / 群聊片段）；
  旧格式（裸整数）与校验不过被拒并记 `goal.tick_rejected`；通过存 evidence/task_id/ts
  并记 `goal.criterion_done`；群友视图（非 admin）不带 evidence。

桩复用 tests/test_task_steps.py / tests/test_coordinator.py。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork.tasks import Tasks
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
from test_task_steps import (  # noqa: F401  (stubs: StepWorkers / _coord / _plan_json …)
    StepWorkers,
    _attempt_rows,
    _coord,
    _create_task,
    _job,
    _kinds,
    _payloads,
    _plan_json,
    _review_json,
    _run_once,
    _write_ws,
)

pytestmark = pytest.mark.asyncio

HUMAN_NEEDS = "请发起人在群里发起投票，投完把结果回给我"


def _req():
    from CharTyr_MaiWork.maiwork import requirements

    return requirements


def _human_plan(req_text="让群友投票选出首开线路"):
    return _plan_json(
        [_job("准备投票选项、说明文案和统计表模板", ["write_file"])],
        deliver_kind="text",
        requirements=[{"text": req_text, "origin": "原话", "kind": "真人"}],
    )


def _human_review(*, met=False, needs=HUMAN_NEEDS, evidence="材料准备好了，还没人投"):
    item: dict = {"id": "R1", "met": met, "evidence": evidence}
    if needs is not None:
        item["needs_human"] = needs
    return _review_json(
        pass_=False, review="还缺真人参与",
        items=[item, {"id": "R0", "met": True, "evidence": "没编造"}],
    )


def _human_task(tasks: Tasks) -> str:
    return _create_task(
        tasks, req="发起投票、统计结果、公示",
        requester_id="10001", requester_name="小明",
    )


def _make_coord(*, mem_store, settings, env, tools, tasks, goals, replies=None,
                workers=None, outbox=None, models=None):
    return _coord(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models or ModelsQueue(replies=replies or []),
        workers=workers or StepWorkers(), outbox=outbox,
    )


def _review_prompt(models) -> str:
    parts = [
        "\n".join(str(m.get("content") or "") for m in msgs)
        for _r, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.review"
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# A. 需要真人参与 → 等人并提醒发起人
# ---------------------------------------------------------------------------


async def test_plan_prompt_tells_subagent_not_to_fake_human(
    mem_store, settings, env, tools, tasks, goals
):
    """清单里有 kind=真人 → 计划提示写清只能准备材料、不能假装有人参与。"""
    tid = _human_task(tasks)
    models = ModelsQueue(replies=[_human_plan(), _human_plan()])
    coord = _make_coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                        tasks=tasks, goals=goals, models=models)
    await coord._plan(tasks.get(tid))       # 首轮锁清单
    await coord._plan(tasks.get(tid))       # 第二轮提示要带真人说明

    prompt = models.calls[-1][1][-1]["content"]
    assert "R1【原话·真人】" in prompt
    assert "不能假装有人参与" in prompt
    assert "不能编造" in prompt
    assert "准备" in prompt
    # 首轮要 requirements 时，kind 说明里也写明「真人」是什么
    first = models.calls[0][1][-1]["content"]
    assert "真人=必须群友或某个真人实际参与" in first
    assert "不能假装有人参与" in first


async def test_review_prompt_explains_needs_human_and_supplement(
    mem_store, settings, env, tools, tasks, goals
):
    """验收提示写清 needs_human 什么情况才写；有【发起人补充】时说明它能当真人证据。"""
    tid = _human_task(tasks)
    items = _req().normalize_requirements(
        [{"text": "让群友投票选出首开线路", "origin": "原话", "kind": "真人"}], "发起投票"
    )
    plan = {
        "criteria": _req().criteria_texts(items), "requirements": items,
        "deliver_kind": "text",
        "jobs": [{"brief": "备料", "tools": ["write_file"], "type": "other",
                  "after": [], "agent": "task"}],
        "question": "", "research": False,
    }
    models = ModelsQueue(replies=[_human_review()])
    coord = _make_coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                        tasks=tasks, goals=goals, replies=[_human_review()])
    await coord._review(tasks.get(tid), plan, "总结", ["证据"], [])
    prompt = _review_prompt(coord._models)
    assert "needs_human" in prompt
    assert "实际参与" in prompt

    # 发起人补充过 → 验收提示写明【发起人补充】可以当真人参与条目的证据
    tasks.revise(tid, req="发起投票\n\n【发起人补充】投完了：A 线 12 票", criteria=[])
    coord._models.reply_queue = [_human_review()]
    await coord._review(tasks.get(tid), plan, "总结", ["证据"], [])
    prompt2 = _review_prompt(coord._models)
    assert "【发起人补充】" in prompt2
    assert "证据" in prompt2


async def test_human_wait_all_unmet_are_human_on_first_attempt(
    mem_store, settings, env, tools, tasks, goals
):
    """第一次尝试：没做到的必须项全是真人 → 直接转 waiting_input 并 @发起人。"""
    tid = _human_task(tasks)
    outbox = FakeOutbox()
    coord = _make_coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                        tasks=tasks, goals=goals, replies=[_human_plan(), _human_review()],
                        outbox=outbox)

    assert await _run_once(coord, tasks, tid) == "done"

    task = tasks.get(tid)
    assert task["status"] == "waiting_input"
    question = str(task["question"])
    assert question.startswith("这一步需要有人参与：")
    assert "R1" in question and "让群友投票选出首开线路" in question
    assert HUMAN_NEEDS in question
    assert "准备好的材料在任务页里" in question
    assert "做完后回复我结果，我接着做。" in question
    assert len(question) <= 200

    asks = [e for e in outbox.enqueued if str(e["key"]).startswith(f"human:{tid}:")]
    assert len(asks) == 1, outbox.enqueued
    assert asks[0]["key"] == f"human:{tid}:1"
    assert asks[0]["payload"]["push_kind"] == "status"
    assert asks[0]["payload"]["text"].startswith("@小明 ")
    # 2026-10-10：群里那条末尾告诉人怎么回（引用回复才认得出是回答）；存进任务的 question 不带
    assert asks[0]["payload"]["text"].endswith("（引用这条消息回复我就行）")
    assert "引用这条消息" not in question

    assert _attempt_rows(mem_store, tid)[-1]["status"] == "waiting"

    kv = mem_store.kv_get(f"task.human_wait.{tid}")
    assert kv["req_version"] == 1
    assert kv["item_ids"] == ["R1"]
    assert isinstance(kv["ts"], float)
    payloads = _payloads(mem_store, tid, "task.human_wait")
    assert payloads and payloads[-1]["items"] == ["R1"]


async def test_human_wait_keeps_rework_when_a_required_item_is_not_human(
    mem_store, settings, env, tools, tasks, goals
):
    """还有别的必须项没做到（不带 needs_human）→ 照常返工，不问人。"""
    tid = _human_task(tasks)
    outbox = FakeOutbox()
    mixed = _review_json(pass_=False, review="还差", items=[
        {"id": "R1", "met": False, "evidence": "还没人投", "needs_human": HUMAN_NEEDS},
        {"id": "R0", "met": False, "evidence": "做了点占位"},
    ])
    coord = _make_coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                        tasks=tasks, goals=goals, replies=[_human_plan(), mixed],
                        outbox=outbox)

    assert await _run_once(coord, tasks, tid) == "retry"
    assert tasks.get(tid)["status"] == "queued"
    assert mem_store.kv_get(f"task.human_wait.{tid}") is None
    assert not [e for e in outbox.enqueued if str(e["key"]).startswith("human:")]


async def test_human_wait_needs_human_too_short_is_ignored(
    mem_store, settings, env, tools, tasks, goals
):
    """needs_human 去空白不到 6 个字 = 没写清要谁做什么 → 不算，照常返工。"""
    tid = _human_task(tasks)
    outbox = FakeOutbox()
    coord = _make_coord(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, replies=[_human_plan(), _human_review(needs="等你")], outbox=outbox,
    )

    assert await _run_once(coord, tasks, tid) == "retry"
    assert tasks.get(tid)["status"] == "queued"
    assert mem_store.kv_get(f"task.human_wait.{tid}") is None
    assert not [e for e in outbox.enqueued if str(e["key"]).startswith("human:")]


# ---------------------------------------------------------------------------
# B. 拿到结果后保留清单继续
# ---------------------------------------------------------------------------


def _reuse_plan() -> str:
    return json.dumps(
        {
            "criteria": [],
            "deliver_kind": "text",
            "jobs": [{"brief": "按发起人回复收尾", "tools": ["write_file"], "type": "other",
                      "reuse": 1}],
            "question": None,
        },
        ensure_ascii=False,
    )


def _passed_review() -> str:
    return _review_json(pass_=True, review="真人结果拿到了", items=[
        {"id": "R1", "met": True, "evidence": "发起人回复：A 线 12 票"},
        {"id": "R0", "met": True, "evidence": "没编造"},
    ])


async def test_resume_after_human_wait_keeps_list_and_steps(
    mem_store, settings, env, tools, tasks, goals
):
    """有人工等待记录 → resume 不重拆清单、步骤存档改到新版本（可 reuse）、删 kv、记事件。"""
    tid = _human_task(tasks)

    def hook(actor):  # noqa: ARG001
        _write_ws(env, tasks, tid, f"artifacts/{tid}/steps/1/投票材料.md", "选项和说明")

    outbox = FakeOutbox()
    coord = _make_coord(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, replies=[_human_plan(), _human_review(), _reuse_plan(), _passed_review()],
        workers=StepWorkers(hook=hook), outbox=outbox,
    )

    await _run_once(coord, tasks, tid)
    assert tasks.get(tid)["status"] == "waiting_input"
    old_items = _req().load(mem_store, tid, 1)
    assert old_items

    await coord.resume(tid, "投完了：A 线 12 票")

    task = tasks.get(tid)
    assert task["req_version"] == 2
    assert "【发起人补充】投完了：A 线 12 票" in str(task["req"])
    assert _req().load(mem_store, tid, 2) == old_items      # 不重拆
    assert mem_store.kv_get(f"task.human_wait.{tid}") is None
    assert "task.human_resumed" in _kinds(mem_store, tid)
    assert mem_store.kv_get(f"task.step.{tid}.1")["req_version"] == 2
    assert "task.step_reused" in _kinds(mem_store, tid)
    assert tasks.get(tid)["status"] == "completed"


async def test_resume_without_human_wait_still_rebuilds_list(
    mem_store, settings, env, tools, tasks, goals
):
    """普通缺信息（没人工等待记录）→ resume 后照旧重新拆清单（回归）。"""
    tid = _human_task(tasks)
    first = _plan_json(
        [_job("先写个临时说明", ["write_file"])], deliver_kind="text",
        requirements=[{"text": "按现在理解的做", "origin": "原话", "kind": "文稿"}],
        question="投票投几天？",
    )
    second = _plan_json(
        [_job("按三天重做", ["write_file"])], deliver_kind="text",
        requirements=[{"text": "投三天的票", "origin": "原话", "kind": "实做"}],
    )
    coord = _make_coord(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals,
        replies=[first, second, _review_json(pass_=True, items=[
            {"id": "R1", "met": True, "evidence": "投票记录"}, {"id": "R0", "met": True, "evidence": "能打开"},
        ])],
    )

    await _run_once(coord, tasks, tid)
    assert tasks.get(tid)["status"] == "waiting_input"
    assert mem_store.kv_get(f"task.human_wait.{tid}") is None

    await coord.resume(tid, "三天")

    saved = mem_store.kv_get(f"task.requirements.{tid}")
    assert saved["req_version"] == 2
    assert saved["items"][0]["text"] == "投三天的票"       # 重拆了，不是旧清单
    assert "task.human_resumed" not in _kinds(mem_store, tid)


async def test_second_human_wait_question_has_prefix(
    mem_store, settings, env, tools, tasks, goals
):
    """发起人回复还不够、同一条目第二次进 human_wait → question 前缀「还差一点：」。"""
    tid = _human_task(tasks)
    outbox = FakeOutbox()
    coord = _make_coord(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals,
        replies=[_human_plan(), _human_review(),          # 第一次等人
                 _human_plan(), _human_review()],         # 回复后还是不够 → 第二次等人
        outbox=outbox,
    )

    await _run_once(coord, tasks, tid)
    assert str(tasks.get(tid)["question"]).startswith("这一步需要有人参与：")

    await coord.resume(tid, "还没投")

    task = tasks.get(tid)
    assert task["status"] == "waiting_input"
    assert str(task["question"]).startswith("还差一点：这一步需要有人参与：")
    asks = [e for e in outbox.enqueued if str(e["key"]).startswith(f"human:{tid}:")]
    assert [e["key"] for e in asks] == [f"human:{tid}:1", f"human:{tid}:2"]
    assert mem_store.kv_get(f"task.human_wait.{tid}")["req_version"] == 2


# ---------------------------------------------------------------------------
# C. 目标完成标准打勾必须有证据
# ---------------------------------------------------------------------------


def _mk_goal(goals) -> str:
    return goals.create_agent(
        GID, title="首开线路", body="群投票选定首开线路并公示结果",
        criteria=["群投票选定首开线路并公示结果"], by_text="",
    )


def _seed_chat(store, rows) -> None:
    with store.tx() as conn:
        for i, (who, text) in enumerate(rows):
            conn.execute(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (text, GID, f"hgel{i}", NOW - i * 60, f"hgu{i}", who),
            )


def _goal_reply(done_criteria):
    return {
        "done_criteria": done_criteria, "next_check_hours": 2, "progress": None,
        "new_task": None, "report": None,
    }


async def _run_goal_check(mem_store, settings, env, tools, tasks, goals, goal_id, data):
    models = ModelsQueue(replies=[json.dumps(data, ensure_ascii=False)])
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=FakeOutbox(),
    )
    await coord.check_goal(goal_id)
    return coord


def _crit(goals, goal_id):
    return json.loads(goals.get(goal_id)["criteria"])


def _goal_events(store, goal_id, kind):
    rows = store.read().execute(
        "SELECT payload FROM events WHERE kind=? AND entity_id=?", (str(kind), str(goal_id))
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except (TypeError, ValueError):
            out.append({})
    return out


async def test_goal_bare_index_rejected_with_event(
    mem_store, settings, env, tools, tasks, goals
):
    """旧格式（裸整数）→ 不勾，记 goal.tick_rejected「没给证据」。"""
    goal_id = _mk_goal(goals)
    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id, _goal_reply([0])
    )

    assert _crit(goals, goal_id)[0]["done"] is False
    assert _goal_events(mem_store, goal_id, "goal.tick_rejected") == [
        {"index": 0, "reason": "没给证据"}
    ]
    assert _goal_events(mem_store, goal_id, "goal.criterion_done") == []


async def test_goal_task_not_completed_rejected(
    mem_store, settings, env, tools, tasks, goals
):
    """task_id 写的是本目标下的任务、但没完成 → 不勾，记「任务没完成」。"""
    goal_id = _mk_goal(goals)
    tid = tasks.create(GID, title="发起投票", req="发起投票", criteria=[], source="goal",
                       goal_id=goal_id, status="queued")
    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id,
        _goal_reply([{"index": 0, "task_id": tid, "evidence": "任务交付了投票结果"}]),
    )

    assert _crit(goals, goal_id)[0]["done"] is False
    assert _goal_events(mem_store, goal_id, "goal.tick_rejected") == [
        {"index": 0, "reason": "任务没完成"}
    ]


async def test_goal_completed_task_with_passed_attempt_accepted(
    mem_store, settings, env, tools, tasks, goals
):
    """本目标下、已完成、有验收通过记录的任务 → 勾上并存 evidence/task_id/ts。"""
    goal_id = _mk_goal(goals)
    tid = tasks.create(GID, title="发起投票", req="发起投票", criteria=[], source="goal",
                       goal_id=goal_id, status="queued")
    tasks.transition(tid, "running", reason="测试")
    tasks.start_attempt(tid)
    tasks.finish_attempt(tasks.current_attempt_id(tid), status="passed", summary="投票完成")
    tasks.transition(tid, "reviewing", reason="测试")
    tasks.transition(tid, "completed", reason="验收通过")

    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id,
        _goal_reply([{"index": 0, "task_id": tid, "evidence": "任务 T 交付了投票结果页"}]),
    )

    item = _crit(goals, goal_id)[0]
    assert item["done"] is True
    assert item["task_id"] == tid
    assert "投票结果" in item["evidence"] and len(item["evidence"]) <= 200
    assert isinstance(item["ts"], float)
    payloads = _goal_events(mem_store, goal_id, "goal.criterion_done")
    assert payloads and payloads[-1]["index"] == 0 and payloads[-1]["task_id"] == tid
    assert "投票结果" in payloads[-1]["evidence"]


async def test_goal_chat_fragment_accepted(mem_store, settings, env, tools, tasks, goals):
    """evidence 引用了本次提示给出的群聊行里 ≥8 个连续字 → 勾上。"""
    goal_id = _mk_goal(goals)
    _seed_chat(mem_store, [("阿柒", "投票结果已经出来了，A 线 12 票")])
    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id,
        _goal_reply([{"index": 0, "task_id": None,
                      "evidence": "阿柒在群里说：投票结果已经出来了"}]),
    )

    item = _crit(goals, goal_id)[0]
    assert item["done"] is True
    assert item["task_id"] is None
    assert "投票结果已经出来了" in item["evidence"]


async def test_goal_chat_fragment_mismatch_rejected(
    mem_store, settings, env, tools, tasks, goals
):
    """task_id 空、evidence 又对不上群聊 → 不勾，记「证据对不上群聊」。"""
    goal_id = _mk_goal(goals)
    _seed_chat(mem_store, [("阿柒", "投票结果出来了，A 线 12 票")])
    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id,
        _goal_reply([{"index": 0, "task_id": None, "evidence": "我觉得已经做完了"}]),
    )

    assert _crit(goals, goal_id)[0]["done"] is False
    assert _goal_events(mem_store, goal_id, "goal.tick_rejected") == [
        {"index": 0, "reason": "证据对不上群聊"}
    ]


async def test_goal_evidence_short_fragment_not_enough(
    mem_store, settings, env, tools, tasks, goals
):
    """群聊片段去空白后连续不足 8 个字 → 不算。"""
    goal_id = _mk_goal(goals)
    _seed_chat(mem_store, [("阿柒", "投票结果出来了")])
    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id,
        _goal_reply([{"index": 0, "task_id": None, "evidence": "投票结果"}]),
    )

    assert _crit(goals, goal_id)[0]["done"] is False
    assert _goal_events(mem_store, goal_id, "goal.tick_rejected")[0]["reason"] == "证据对不上群聊"


async def test_goal_missing_evidence_rejected(
    mem_store, settings, env, tools, tasks, goals
):
    """有 index 但 evidence 空的条目 → 「没给证据」。"""
    goal_id = _mk_goal(goals)
    await _run_goal_check(
        mem_store, settings, env, tools, tasks, goals, goal_id,
        _goal_reply([{"index": 0, "task_id": None, "evidence": "  "}]),
    )

    assert _crit(goals, goal_id)[0]["done"] is False
    assert _goal_events(mem_store, goal_id, "goal.tick_rejected")[0]["reason"] == "没给证据"


async def test_goal_view_hides_evidence_for_non_admin(mem_store, goals):
    """群友视图只带 task_id / ts，不带 evidence（可能是群友原话）。"""
    from CharTyr_MaiWork.maiwork.console import views as console_views

    goal_id = _mk_goal(goals)
    goals.set_criterion(goal_id, 0, True, evidence="阿柒：投票结果出来了",
                        task_id="T-3", ts=NOW)

    svc = SimpleNamespace(goals=goals)
    admin = console_views._goals_view(svc, GID, admin=True)["agent"][0]["criteria"][0]
    public = console_views._goals_view(svc, GID, admin=False)["agent"][0]["criteria"][0]
    assert admin["evidence"] == "阿柒：投票结果出来了"
    assert public["done"] is True
    assert public["task_id"] == "T-3"
    assert public["ts"] == NOW
    assert "evidence" not in public


async def test_chat_evidence_match_pure_function():
    """chat_evidence_match：去空白后连续 ≥8 字相同才算（纯函数）。"""
    from CharTyr_MaiWork.maiwork import goals as goals_mod

    lines = ["- 阿柒：投票结果已经出来了，A 线 12 票"]
    assert goals_mod.chat_evidence_match("投票结果已经出来了", lines) is True
    assert goals_mod.chat_evidence_match("结 果 已 经 出 来", lines) is False  # 只有 6 个字
    assert goals_mod.chat_evidence_match("", lines) is False
    assert goals_mod.chat_evidence_match("投票结果已经出来了", []) is False
    assert goals_mod.chat_evidence_match("随便一句别的", lines) is False
    # 跨空白拼接也算
    assert goals_mod.chat_evidence_match("投票结果\n已经出来了", lines) is True
