"""docs/22 §3 第一期：需求清单（requirements）+ 代码算验收结果。

覆盖（按文档 §3 的 A–E）：
- A `maiwork/requirements.py`：normalize 各种兜底、is_blocking / criteria_texts、judge、kv 锁定；
- B `coordinator._plan`：首轮拆清单并锁定、之后每轮不许改、需求改版才重拆、没给走旧逻辑；
- C `coordinator._review`：逐条判定 + 代码算过没过（模型的 pass 只作参考）；
- D 验收没结论记 `inconclusive`：不算打回、不触发「没有升级模型就直接判失败」；
- E `tasks.detail_view` 带当前版本清单。

模型桩（ModelsQueue / FakeWorkers / _build / _create_task 等）复用 tests/test_coordinator.py。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork.lanes import TaskLanes
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from tests.test_coordinator import (  # noqa: F401  (fixtures)
    GID,
    FakeDelivery,
    FakeOutbox,
    FakeWorkers,
    ModelsQueue,
    ReplayChatResult,
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

pytestmark = pytest.mark.asyncio


def _req():
    """延迟导入：先写测试跑红时，模块还不存在 → 算失败（不是 skip、不是假绿）。"""
    from CharTyr_MaiWork.maiwork import requirements

    return requirements


def _items():
    """两条常见清单：R1 原话（必须项）、R2 补充（加分项）、R0 底线。"""
    return _req().normalize_requirements(
        [
            {"text": "把投票发起来", "origin": "原话", "kind": "实做"},
            {"text": "画一张结果图", "origin": "补充", "kind": "实做"},
        ],
        "发起投票、统计结果、公示",
    )


def _plan_with_requirements(requirements, criteria=None, deliver_kind="text", jobs=None,
                            question=None):
    """带 requirements 的计划 JSON（给 _plan 用）。"""
    data = {
        "criteria": ["包含链接"] if criteria is None else criteria,
        "deliver_kind": deliver_kind,
        "jobs": jobs if jobs is not None
        else [{"brief": "做一页总结", "tools": ["write_file", "list_files"]}],
        "question": question,
    }
    if requirements is not None:
        data["requirements"] = requirements
    return json.dumps(data, ensure_ascii=False)


def _review_items(verdicts, pass_=True, review="看着不错", artifact="", note="好了"):
    """带逐条判定的验收 JSON。"""
    return json.dumps(
        {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": note,
         "items": verdicts},
        ensure_ascii=False,
    )


def _review_no_items(pass_=True, review="看着不错", artifact="", note="好了"):
    """模型漏了整个 items 的验收 JSON。"""
    return json.dumps(
        {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": note},
        ensure_ascii=False,
    )


def _event_payloads(store, tid, kind):
    rows = store.read().execute(
        "SELECT payload FROM events WHERE kind=? AND entity_id=?", (str(kind), str(tid))
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except (TypeError, ValueError):
            out.append({})
    return out


# ---------------------------------------------------------------------------
# A. requirements.py 纯函数
# ---------------------------------------------------------------------------


async def test_normalize_bad_origin_and_kind_fall_back_to_strict():
    items = _req().normalize_requirements(
        [{"text": "把投票发起来", "origin": "可选", "kind": "网页"}], "发起投票"
    )
    reqs = [i for i in items if i["id"] != "R0"]
    assert len(reqs) == 1
    assert reqs[0]["origin"] == "原话"  # 坏 origin → 原话（宁严勿松）
    assert reqs[0]["kind"] == "按原话"  # 坏 kind → 按原话
    assert reqs[0]["id"] == "R1"


async def test_normalize_missing_origin_and_kind_are_strict():
    items = _req().normalize_requirements([{"text": "做一页"}], "做一页")
    assert items[0]["origin"] == "原话"
    assert items[0]["kind"] == "按原话"


async def test_normalize_drops_empty_and_truncates_to_60():
    items = _req().normalize_requirements(
        ["不是字典", {"text": "   "}, {"text": "长" * 200, "origin": "原话"}], "做一页"
    )
    reqs = [i for i in items if i["id"] != "R0"]
    assert len(reqs) == 1
    assert len(reqs[0]["text"]) == 60
    assert reqs[0]["origin"] == "原话"


async def test_normalize_max_six_items_plus_floor():
    raw = [{"text": f"第 {i} 条", "origin": "补充", "kind": "文稿"} for i in range(1, 9)]
    items = _req().normalize_requirements(raw, "做一页")
    assert [i["id"] for i in items] == ["R1", "R2", "R3", "R4", "R5", "R6", "R0"]
    assert items[-1]["id"] == "R0"


async def test_normalize_inserts_original_item_when_no_original_given():
    req_text = "帮我把群里的投票发起一下，统计结果并公示，谢谢啦"
    items = _req().normalize_requirements(
        [{"text": "做个网页", "origin": "补充", "kind": "实做"}], req_text
    )
    assert [i["id"] for i in items] == ["R1", "R2", "R0"]
    assert items[0]["origin"] == "原话"
    assert items[0]["text"] == "按原话完成：" + req_text[:50]
    assert items[1]["text"] == "做个网页"


async def test_normalize_insert_keeps_six_item_cap():
    raw = [{"text": f"第 {i} 条", "origin": "补充", "kind": "文稿"} for i in range(1, 9)]
    items = _req().normalize_requirements(raw, "做一页")
    reqs = [i for i in items if i["id"] != "R0"]
    assert len(reqs) == 6
    assert reqs[0]["text"].startswith("按原话完成：")
    assert [i["id"] for i in reqs] == ["R1", "R2", "R3", "R4", "R5", "R6"]


async def test_normalize_always_appends_floor():
    for raw in ([], None, "坏数据", [{"text": ""}], [{"text": "x"}]):
        items = _req().normalize_requirements(raw, "做一页")
        assert items[-1] == {
            "id": "R0",
            "text": "内容真实、不编造；交付物能正常打开",
            "origin": "底线",
            "kind": "底线",
        }


async def test_is_blocking_origin_rules():
    r = _req()
    assert r.is_blocking({"origin": "原话"}) is True
    assert r.is_blocking({"origin": "底线"}) is True
    assert r.is_blocking({"origin": "补充"}) is False


async def test_criteria_texts_drops_floor_and_marks_bonus():
    assert _req().criteria_texts(_items()) == ["把投票发起来", "画一张结果图（加分项）"]


async def test_judge_all_met_with_evidence_passes():
    r = _req()
    verdict = r.judge(_items(), [
        {"id": "R1", "met": True, "evidence": "artifacts/T-1/vote.json 有 12 票"},
        {"id": "R2", "met": True, "evidence": "artifacts/T-1/chart.png"},
        {"id": "R0", "met": True, "evidence": "文件能打开"},
    ])
    assert verdict["pass"] is True
    assert verdict["unmet_blocking"] == []
    assert verdict["unmet_bonus"] == []
    assert sorted(verdict["met"]) == ["R0", "R1", "R2"]


async def test_judge_original_item_not_met_fails():
    verdict = _req().judge(_items(), [
        {"id": "R1", "met": False, "evidence": "没做成"},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ])
    assert verdict["pass"] is False
    assert [u["id"] for u in verdict["unmet_blocking"]] == ["R1"]
    assert verdict["unmet_blocking"][0]["why"] == "判定没做到"
    assert verdict["unmet_blocking"][0]["text"] == "把投票发起来"


async def test_judge_met_without_evidence_is_not_done():
    verdict = _req().judge(_items(), [
        {"id": "R1", "met": True, "evidence": "   "},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ])
    assert verdict["pass"] is False
    assert verdict["unmet_blocking"][0]["why"] == "没给证据"


async def test_judge_missing_verdict_is_not_done():
    verdict = _req().judge(_items(), [
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ])
    assert verdict["pass"] is False
    assert verdict["unmet_blocking"][0] == {
        "id": "R1", "text": "把投票发起来", "why": "验收没判这一条",
    }


async def test_judge_bonus_unmet_still_passes():
    verdict = _req().judge(_items(), [
        {"id": "R1", "met": True, "evidence": "投票记录"},
        {"id": "R2", "met": False, "evidence": "没画图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ])
    assert verdict["pass"] is True
    assert [u["id"] for u in verdict["unmet_bonus"]] == ["R2"]
    assert verdict["unmet_blocking"] == []


async def test_judge_floor_not_met_fails():
    verdict = _req().judge(_items(), [
        {"id": "R1", "met": True, "evidence": "投票记录"},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": False, "evidence": "打不开"},
    ])
    assert verdict["pass"] is False
    assert [u["id"] for u in verdict["unmet_blocking"]] == ["R0"]


async def test_judge_tolerates_bad_verdicts():
    verdict = _req().judge(_items(), None)
    assert verdict["pass"] is False
    assert len(verdict["unmet_blocking"]) == 2  # R1 + R0
    assert verdict["met"] == []
    # 非 dict / 非字符串 id / id 对不上 → 一律当没判
    verdict = _req().judge(_items(), [1, "R1", {"id": 1, "met": True, "evidence": "x"},
                                      {"id": "R-other", "met": True, "evidence": "x"}])
    assert verdict["pass"] is False
    assert [u["why"] for u in verdict["unmet_blocking"]] == ["验收没判这一条"] * 2


async def test_judge_met_must_be_literal_true():
    verdict = _req().judge(_items(), [
        {"id": "R1", "met": 1, "evidence": "投票记录"},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ])
    assert verdict["pass"] is False
    assert verdict["unmet_blocking"][0]["id"] == "R1"


async def test_requirements_kv_roundtrip_and_version_guard(mem_store):
    r = _req()
    items = r.normalize_requirements([{"text": "做一页", "origin": "原话", "kind": "实做"}], "做一页")
    assert r.load(mem_store, "T-1", 1) is None  # 没存过
    r.save(mem_store, "T-1", 1, items)
    assert r.load(mem_store, "T-1", 1) == items
    assert r.load(mem_store, "T-1", 2) is None  # 版本对不上 = 没锁定
    saved = mem_store.kv_get("task.requirements.T-1")
    assert saved["req_version"] == 1
    assert isinstance(saved["ts"], float)
    assert saved["items"] == items


# ---------------------------------------------------------------------------
# B. _plan：首轮拆清单并锁定，之后每轮不许改
# ---------------------------------------------------------------------------


def _coordinator(mem_store, settings, env, tools, tasks, goals, replies, workers=None):
    return _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=ModelsQueue(replies=replies), workers=workers or FakeWorkers(),
    )


async def test_plan_first_round_saves_and_locks_requirements(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    models = ModelsQueue(replies=[_plan_with_requirements([
        {"text": "把投票发起来", "origin": "原话", "kind": "实做"},
        {"text": "画一张结果图", "origin": "补充", "kind": "实做"},
    ])])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    plan = await coordinator._plan(tasks.get(tid))

    assert [i["id"] for i in plan["requirements"]] == ["R1", "R2", "R0"]
    assert plan["criteria"] == ["把投票发起来", "画一张结果图（加分项）"]
    saved = mem_store.kv_get(f"task.requirements.{tid}")
    assert saved["req_version"] == 1
    assert saved["items"] == plan["requirements"]
    assert _event_payloads(mem_store, tid, "task.requirements_set") == [
        {"total": 2, "original": 1, "bonus": 1}
    ]


async def test_plan_locked_list_ignores_model_changes(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    first_reqs = [
        {"text": "把投票发起来", "origin": "原话", "kind": "实做"},
        {"text": "画一张结果图", "origin": "补充", "kind": "实做"},
    ]
    changed = [
        {"text": "写个投票方案就行", "origin": "原话", "kind": "文稿"},
        {"text": "不用真投票", "origin": "补充", "kind": "文稿"},
    ]
    models = ModelsQueue(replies=[
        _plan_with_requirements(first_reqs),
        _plan_with_requirements(changed, criteria=["只写方案"]),
    ])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    first = await coordinator._plan(tasks.get(tid))
    second = await coordinator._plan(tasks.get(tid))

    # 清单锁定了：模型这一轮想改（降级成「只写方案」）也不认
    assert second["requirements"] == first["requirements"]
    assert second["requirements"][0]["kind"] == "实做"
    assert second["criteria"] == first["criteria"]
    assert second["criteria"] == ["把投票发起来", "画一张结果图（加分项）"]
    # 每轮都要在提示里列出锁定清单（编号 + 标签），并明说不许改、这一轮只排 jobs
    prompt = models.calls[-1][1][-1]["content"]
    assert "- R1【原话·实做】把投票发起来" in prompt
    assert "- R2【补充·实做】画一张结果图（加分项）" in prompt
    assert "不许改" in prompt
    assert "只排 jobs" in prompt
    # 锁定模式下不再重复保存 / 不再记 requirements_set
    assert _event_payloads(mem_store, tid, "task.requirements_set") == [
        {"total": 2, "original": 1, "bonus": 1}
    ]


async def test_plan_requirement_revision_rebuilds_list(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    models = ModelsQueue(replies=[
        _plan_with_requirements([
            {"text": "把投票发起来", "origin": "原话", "kind": "实做"},
        ]),
        _plan_with_requirements([
            {"text": "改成一页 PPT", "origin": "原话", "kind": "文稿"},
        ]),
    ])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    first = await coordinator._plan(tasks.get(tid))
    tasks.revise(tid, req="改成做一页 PPT", criteria=[])
    second = await coordinator._plan(tasks.get(tid))

    assert first["requirements"][0]["text"] == "把投票发起来"
    assert second["requirements"][0]["text"] == "改成一页 PPT"
    saved = mem_store.kv_get(f"task.requirements.{tid}")
    assert saved["req_version"] == 2
    assert saved["items"] == second["requirements"]
    assert _req().load(mem_store, tid, 1) is None  # 旧版本的清单不再命中


async def test_plan_locked_with_lead_prior_and_version_change(
    mem_store, settings, env, tools, tasks, goals
):
    """清单锁定 + 领队前情停在旧版本（能力重排等场景）：req_note 照样插得进去，
    表头从「当前完成标准」换成「需求清单」也不许崩。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    items = _items()
    _req().save(mem_store, tid, 1, items)
    # 领队记录停在 v1（改版之后这条老记录还在：保存失败 / 能力重排时又排了一次计划）
    TaskLanes(mem_store).save(
        tid, "lead", group_id=GID, kind="main",
        messages=[{"role": "user", "content": "旧的一轮"}], req_version=1,
    )
    tasks.revise(tid, req="发起投票、统计结果、公示三天", criteria=[])
    _req().save(mem_store, tid, 2, items)
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals,
                               replies=[_plan_with_requirements(None)])
    coordinator._specialists = object()  # noqa: SLF001

    plan = await coordinator._plan(tasks.get(tid), lead=True)

    assert plan["requirements"] == items
    prompt = coordinator._models.calls[-1][1][-1]["content"]
    assert "需求改过" in prompt
    assert "R1【原话·实做】把投票发起来" in prompt


async def test_plan_without_requirements_keeps_old_logic(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(criteria=["包含链接"])])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    plan = await coordinator._plan(tasks.get(tid))

    assert plan["requirements"] is None
    assert plan["criteria"] == ["包含链接"]  # 旧逻辑原样
    assert mem_store.kv_get(f"task.requirements.{tid}") is None
    assert len(_event_payloads(mem_store, tid, "task.requirements_missing")) == 1
    # 提示里要写清规则（原话 / 补充 / 不许降级漏掉）
    prompt = models.calls[-1][1][-1]["content"]
    assert "requirements" in prompt and "原话" in prompt and "补充" in prompt


async def test_plan_question_still_saves_requirements(
    mem_store, settings, env, tools, tasks, goals
):
    """模型既要提问又给了清单：清单照样锁在**这一版**需求上（回答后版本升了再重拆）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    models = ModelsQueue(replies=[
        _plan_with_requirements([
            {"text": "把投票发起来", "origin": "原话", "kind": "实做"},
        ], question="投几天？"),
        _plan_with_requirements([
            {"text": "投三天的票", "origin": "原话", "kind": "实做"},
        ], deliver_kind="text"),
        _review_items([
            {"id": "R1", "met": True, "evidence": "artifacts 投票记录"},
            {"id": "R0", "met": True, "evidence": "文件能打开"},
        ]),
    ])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "waiting_input"
    assert mem_store.kv_get(f"task.requirements.{tid}")["req_version"] == 1

    await coordinator.resume(tid, "三天")

    task = tasks.get(tid)
    assert task["req_version"] == 2
    assert tasks.get(tid)["status"] == "completed"
    saved = mem_store.kv_get(f"task.requirements.{tid}")
    assert saved["req_version"] == 2
    assert saved["items"][0]["text"] == "投三天的票"
    # 第二轮验收用的是第二版清单，不是第一版
    review_prompt = [c for c in models.calls if c[2].get("purpose") == "coordinator.review"][-1]
    assert "投三天的票" in review_prompt[1][-1]["content"]
    assert "把投票发起来" not in review_prompt[1][-1]["content"]


async def test_run_task_persists_requirements_as_criteria(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    models = ModelsQueue(replies=[
        _plan_with_requirements([
            {"text": "把投票发起来", "origin": "原话", "kind": "实做"},
            {"text": "画一张结果图", "origin": "补充", "kind": "实做"},
        ], deliver_kind="text"),
        _review_items([
            {"id": "R1", "met": True, "evidence": "artifacts 投票记录"},
            {"id": "R2", "met": False, "evidence": "没画图"},
            {"id": "R0", "met": True, "evidence": "文件能打开"},
        ]),
    ])
    coordinator = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(),
    )

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert json.loads(tasks.get(tid)["criteria"]) == ["把投票发起来", "画一张结果图（加分项）"]


# ---------------------------------------------------------------------------
# C. _review：逐条判定，代码算过没过
# ---------------------------------------------------------------------------


def _plan_locked(items, deliver_kind="text"):
    """已锁定清单的计划（_review 用）。"""
    return {
        "criteria": _req().criteria_texts(items),
        "requirements": items,
        "deliver_kind": deliver_kind,
        "jobs": [{"brief": "干这条活", "tools": ["write_file"], "type": "other",
                  "after": [], "agent": "task"}],
        "question": "",
        "research": False,
    }


async def test_review_model_pass_but_original_unmet_fails(
    mem_store, settings, env, tools, tasks, goals
):
    """模型说 pass，但原话项 met=false → 代码判不过（docs/22 §3.2）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    items = _items()
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals,
                               replies=[_review_items([
                                   {"id": "R1", "met": False, "evidence": "没做成，只写了方案"},
                                   {"id": "R2", "met": True, "evidence": "artifacts/T-1/chart.png"},
                                   {"id": "R0", "met": True, "evidence": "文件能打开"},
                               ], pass_=True, review="我觉得可以了" + "很" * 200)])

    review = await coordinator._review(tasks.get(tid), _plan_locked(items), "总结", ["证据"], [])

    assert review["pass"] is False
    assert review["items_judgement"]["pass"] is False
    assert [u["id"] for u in review["items_judgement"]["unmet_blocking"]] == ["R1"]
    assert review["review"].startswith("没过：")
    assert "R1 把投票发起来（判定没做到）" in review["review"]
    listing = review["review"].split("（验收模型原话：")[0]
    assert len(listing) <= 200
    assert "很" * 121 not in review["review"]  # 模型原话截到 120 字
    # 模型 pass 与代码结果不一致 → 记事件
    assert _event_payloads(mem_store, tid, "task.review_disagree") == [
        {"model_pass": True, "code_pass": False}
    ]
    # 提示里列了清单（编号 + 标签）并说明必须项 / 加分项
    prompt = coordinator._models.calls[-1][1][-1]["content"]
    assert "- R1【原话·实做】把投票发起来" in prompt
    assert "- R0【底线·底线】" in prompt
    assert "必须项" in prompt and "加分项" in prompt
    assert '"items"' in prompt


async def test_review_bonus_only_unmet_still_passes(
    mem_store, settings, env, tools, tasks, goals
):
    """T-7 场景：模型因为加分项没做到说 pass=false，但原话项都做到了 → 代码判过。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    items = _items()
    reply = _review_items([
        {"id": "R1", "met": True, "evidence": "artifacts/T-1/votes.json 12 票"},
        {"id": "R2", "met": False, "evidence": "没画图"},
        {"id": "R0", "met": True, "evidence": "文件能打开"},
    ], pass_=False, review="图没画，算没做完")
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[reply])

    review = await coordinator._review(tasks.get(tid), _plan_locked(items), "总结", ["证据"], [])

    assert review["pass"] is True
    assert review["items_judgement"]["pass"] is True
    assert [u["id"] for u in review["items_judgement"]["unmet_bonus"]] == ["R2"]
    assert review["review"].startswith("通过")
    assert "加分项没做到" in review["review"]
    assert "R2 画一张结果图" in review["review"]
    assert _event_payloads(mem_store, tid, "task.review_disagree") == [
        {"model_pass": False, "code_pass": True}
    ]


async def test_review_met_without_evidence_fails(
    mem_store, settings, env, tools, tasks, goals
):
    """原话项 met=true 但没给证据 = 没做到。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    items = _items()
    reply = _review_items([
        {"id": "R1", "met": True, "evidence": "   "},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ], pass_=True)
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[reply])

    review = await coordinator._review(tasks.get(tid), _plan_locked(items), "总结", ["证据"], [])

    assert review["pass"] is False
    assert review["items_judgement"]["unmet_blocking"][0]["why"] == "没给证据"


async def test_review_missing_verdict_on_original_fails(
    mem_store, settings, env, tools, tasks, goals
):
    """模型漏判原话项：先追问漏掉的那几条一次；追问后还没判 = 没做到（不能靠不说当没这回事）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    reply = _review_items([
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ], pass_=True)
    still_missing = _review_items([{"id": "R2", "met": True, "evidence": "有图"}], pass_=True)
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals,
                               replies=[reply, still_missing])

    review = await coordinator._review(tasks.get(tid), _plan_locked(_items()), "总结", ["证据"], [])

    assert review["pass"] is False
    assert review["items_judgement"]["unmet_blocking"][0]["why"] == "验收没判这一条"
    assert len(coordinator._models.calls) == 2       # 只追问一次
    asked = str(coordinator._models.calls[-1][1][-1]["content"])
    assert "R1" in asked and "R0" not in asked        # 只追问漏掉的必须项


async def test_review_missing_floor_verdict_asks_then_passes(
    mem_store, settings, env, tools, tasks, goals
):
    """模型逐条判了但漏了底线 R0：追问补上后照常判，不白耗一次尝试。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    first = _review_items([
        {"id": "R1", "met": True, "evidence": "artifacts/T-1/votes.json"},
        {"id": "R2", "met": True, "evidence": "artifacts/T-1/chart.png"},
    ], pass_=True)
    fill = _review_items([{"id": "R0", "met": True, "evidence": "页面能打开，数据有来源"}], pass_=True)
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[first, fill])

    review = await coordinator._review(tasks.get(tid), _plan_locked(_items()), "总结", ["证据"], [])

    assert review["pass"] is True
    assert sorted(review["items_judgement"]["met"]) == ["R0", "R1", "R2"]
    last = coordinator._models.calls[-1]
    assert last[2].get("tools") is None and last[2].get("json_mode") is True
    # 追问前先把模型上一次的回答放回对话里，它才知道自己判过什么
    assert last[1][-2]["role"] == "assistant"


async def test_review_missing_items_asks_once_then_judges(
    mem_store, settings, env, tools, tasks, goals
):
    """模型漏了整个 items：追加一轮补问；补上就照常判。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    replies = [
        _review_no_items(pass_=True, review="看着行"),
        _review_items([
            {"id": "R1", "met": True, "evidence": "artifacts/T-1/votes.json"},
            {"id": "R2", "met": True, "evidence": "artifacts/T-1/chart.png"},
            {"id": "R0", "met": True, "evidence": "文件能打开"},
        ], pass_=True, review="逐条核过了"),
    ]
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=replies)

    review = await coordinator._review(tasks.get(tid), _plan_locked(_items()), "总结", ["证据"], [])

    assert review.get("inconclusive") is not True
    assert review["pass"] is True
    assert review["items_judgement"]["pass"] is True
    # 补问用 tools=None + json_mode=True，且明白要求「补上 items、只回 JSON」
    last = models_last_call(coordinator)
    assert last[2].get("json_mode") is True
    assert last[2].get("tools") is None
    assert any("items" in str(m.get("content") or "") for m in last[1])
    assert any("只回 JSON" in str(m.get("content") or "") for m in last[1])


def models_last_call(coordinator):
    return coordinator._models.calls[-1]


async def test_review_missing_items_twice_is_inconclusive(
    mem_store, settings, env, tools, tasks, goals
):
    """补问一次还没有 items → 返回 inconclusive（和现有 inconclusive 同结构）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        _review_no_items(), _review_no_items(),
    ])

    review = await coordinator._review(tasks.get(tid), _plan_locked(_items()), "总结", ["证据"], [])

    assert review["pass"] is False
    assert review["inconclusive"] is True
    assert review["review"]
    assert set(review) == {
        "pass", "inconclusive", "review", "artifact", "note", "missing",
        "link_check", "next", "challenges", "challenge_ok",
    }
    # 补问只问一次：模型共被叫了 2 次（首次 + 1 次补问）
    assert len(coordinator._models.calls) == 2


async def test_review_without_requirements_keeps_old_logic(
    mem_store, settings, env, tools, tasks, goals
):
    """计划里没有 requirements → 完全走旧逻辑（旧测试都依赖这一点）。"""
    tid = _create_task(tasks, req="做一页总结")
    reply = _review(pass_=True, artifact="artifacts/T-1/index.html", review="看着不错")
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[reply])

    review = await coordinator._review(
        tasks.get(tid), {"criteria": ["包含链接"], "deliver_kind": "text"},
        "总结", ["证据"], [],
    )

    assert review["pass"] is True
    assert review["review"] == "看着不错"
    assert "items_judgement" not in review
    assert _event_payloads(mem_store, tid, "task.review_disagree") == []


async def test_review_requirements_mode_still_enforces_artifact_check(
    mem_store, settings, env, tools, tasks, goals
):
    """清单全做到，但成品文件不存在 → 既有硬检查照旧把它改成不过。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    reply = _review_items([
        {"id": "R1", "met": True, "evidence": "投票记录"},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ], pass_=True, artifact=f"artifacts/{tid}/index.html")
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[reply])

    review = await coordinator._review(
        tasks.get(tid), _plan_locked(_items(), deliver_kind="view"), "总结", ["证据"], [],
    )

    assert review["items_judgement"]["pass"] is True
    assert review["pass"] is False
    assert "不存在" in review["review"]


async def test_review_next_only_kept_when_code_says_not_passed(
    mem_store, settings, env, tools, tasks, goals
):
    """next 只在代码结果没过时才留（模型自己的 pass 不作数）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    # 模型说 pass=true，但原话项没做到 → 代码判没过；此时它给的 next 保留
    loose = _review_items([
        {"id": "R1", "met": False, "evidence": "先给了个方案"},
        {"id": "R2", "met": True, "evidence": "有图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ], pass_=True)
    loose = json.loads(loose)
    loose["next"] = [{"job": 1, "brief": "接着把投票真发起来"}]
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals,
                               replies=[json.dumps(loose, ensure_ascii=False)])
    review = await coordinator._review(tasks.get(tid), _plan_locked(_items()), "总结", ["证据"], [],
                                       allow_next=True)
    assert review["pass"] is False
    assert review["next"] == [{"job": 1, "brief": "接着把投票真发起来"}]

    # 模型说 pass=false（只有加分项没做到）→ 代码判过；它给的 next 丢掉
    tight = _review_items([
        {"id": "R1", "met": True, "evidence": "投票记录"},
        {"id": "R2", "met": False, "evidence": "没画图"},
        {"id": "R0", "met": True, "evidence": "能打开"},
    ], pass_=False)
    tight = json.loads(tight)
    tight["next"] = [{"job": 1, "brief": "再补张图"}]
    coordinator._models.reply_queue = [json.dumps(tight, ensure_ascii=False)]
    review2 = await coordinator._review(tasks.get(tid), _plan_locked(_items()), "总结", ["证据"], [],
                                        allow_next=True)
    assert review2["pass"] is True
    assert review2["next"] == []


# ---------------------------------------------------------------------------
# D. 验收没结论 ≠ 被打回（attempts.status = inconclusive）
# ---------------------------------------------------------------------------


def _attempt_rows(store, tid):
    return store.read().execute(
        "SELECT n, status, review FROM attempts WHERE task_id=? ORDER BY n", (tid,)
    ).fetchall()


def _no_fail_message(coordinator):
    return [
        e for e in coordinator._outbox.enqueued
        if "没做成" in str(e["payload"].get("text") or "")
    ]


async def test_inconclusive_attempt_recorded_as_inconclusive(
    mem_store, settings, env, tools, tasks, goals
):
    """验收模型漏了 items、补问一次仍没有 → 这次尝试记 inconclusive（不是 failed），
    任务退回队列重跑，不算打回、不往群里发「没做成」。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        _plan_with_requirements([{"text": "把投票发起来", "origin": "原话", "kind": "实做"}]),
        _review_no_items(),
        _review_no_items(),
    ])

    result = await coordinator._run_one_attempt(tasks.get(tid))

    assert result == "retry"
    assert tasks.get(tid)["status"] == "queued"
    rows = _attempt_rows(mem_store, tid)
    assert [(r["n"], r["status"]) for r in rows] == [(1, "inconclusive")]
    assert "没给逐条判断" in rows[0]["review"]
    # 「没结论」只数 failed 的时候自然不计入打回
    assert coordinator._rejections(tid) == 0
    assert _event_payloads(mem_store, tid, "task.review_inconclusive") == [{"attempt": 1}]
    assert _no_fail_message(coordinator) == []


async def test_inconclusive_still_uses_one_attempt(
    mem_store, settings, env, tools, tasks, goals
):
    """没结论照样占一次尝试（attempt 计数不减，防止无限重跑）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    plan = _plan_with_requirements([{"text": "把投票发起来", "origin": "原话", "kind": "实做"}])
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        plan, _review_no_items(), _review_no_items(),
        plan, _review_no_items(), _review_no_items(),
    ])

    await coordinator._run_one_attempt(tasks.get(tid))
    await coordinator._run_one_attempt(tasks.get(tid))

    assert tasks.get(tid)["attempts"] == 2
    assert [r["status"] for r in _attempt_rows(mem_store, tid)] == [
        "inconclusive", "inconclusive",
    ]
    assert coordinator._rejections(tid) == 0


async def test_two_inconclusive_never_trigger_direct_fail_without_escalation(
    mem_store, settings, env, tools, tasks, goals
):
    """两次没结论 + 挂着专岗 + 没有可升级模型：也不许走「打回 2 次直接判失败」那条捷径
    （`_rejections` 只数 failed）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    plan = _plan_with_requirements([{"text": "把投票发起来", "origin": "原话", "kind": "实做"}])
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        plan, _review_no_items(), _review_no_items(),
        plan, _review_no_items(), _review_no_items(),
    ])

    assert await coordinator._run_one_attempt(tasks.get(tid)) == "retry"
    assert await coordinator._run_one_attempt(tasks.get(tid)) == "retry"
    assert tasks.get(tid)["status"] == "queued"

    # 真的挂上专岗、这一版被打回 0 次：这条捷径不该触发（触发就直接 failed 了）
    coordinator._specialists = object()  # noqa: SLF001
    tasks.transition(tid, "running", reason="测试：模拟这一轮正在跑")
    assert coordinator._handle_unpassed(tid, 2, GID, "验收没结论", "text") == "retry"
    assert tasks.get(tid)["status"] == "queued"


async def test_inconclusive_then_third_attempt_fails_as_before(
    mem_store, settings, env, tools, tasks, goals
):
    """3 次尝试用完照旧判失败（没结论也占次数）。"""
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    plan = _plan_with_requirements([{"text": "把投票发起来", "origin": "原话", "kind": "实做"}])
    coordinator = _coordinator(mem_store, settings, env, tools, tasks, goals, replies=[
        plan, _review_no_items(), _review_no_items(),
        plan, _review_no_items(), _review_no_items(),
        plan, _review_no_items(), _review_no_items(),
    ])

    await coordinator.run_task(tid)

    assert tasks.get(tid)["status"] == "failed"
    assert [r["status"] for r in _attempt_rows(mem_store, tid)] == [
        "inconclusive", "inconclusive", "inconclusive",
    ]
    assert len(_no_fail_message(coordinator)) == 1


# ---------------------------------------------------------------------------
# E. 网页数据：detail_view 带当前版本清单
# ---------------------------------------------------------------------------


async def test_detail_view_includes_current_requirements(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="发起投票、统计结果、公示")
    # 还没锁定 → 空清单（前端据此不画这块）
    assert tasks.detail_view(tid, admin=True)["requirements"] == []
    assert tasks.detail_view(tid, admin=False)["requirements"] == []

    items = _items()
    _req().save(mem_store, tid, 1, items)

    detail = tasks.detail_view(tid, admin=True)
    assert detail["requirements"] == items
    assert [i["id"] for i in detail["requirements"]] == ["R1", "R2", "R0"]
    assert all(set(i) == {"id", "text", "origin", "kind"} for i in detail["requirements"])
    # 群友版也带（「怎样算完成」那块本来群友就看得到）
    assert tasks.detail_view(tid, admin=False)["requirements"] == items

    # 需求改版 → 旧版本清单不再显示，等重新拆
    tasks.revise(tid, req="改成做一页 PPT", criteria=[])
    assert tasks.detail_view(tid, admin=True)["requirements"] == []
