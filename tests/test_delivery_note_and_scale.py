"""交付说明像人话（问题 A）+ 按请求大小定规模（问题 B，docs/26 问题 C 的收口）。

线上实测（2026-10 复核，docs/26）：
- T-11 交付说明写「ZATO入门表已存成DOC，关键节点和结局都折叠遮挡可放心点开」、
  T-10 写「韩国银行AI攻击事件已整理成手机直开单页，时间线和争议点都标好了来源」——
  只说「做好了」，没有结论 / 最有用的发现。旧代码兜底是「做好了，请查收」。
- T-10 原话只有「麦麦你搜搜，今天韩国银行被ai攻击了」，计划却做出 50KB / 12 节 / 80 来源长页，
  79 分钟、1594 万 token，发起人只回「哦哦哦」。计划提示只有 view/file/text 分别做什么，
  没有规模档。

本文件测：
A. `coordinator.delivery_note_text` / `delivery_note_fallback`（纯函数）：note 空 / 含内部词
   → 按任务标题生成人话兜底；超 60 字 → 在句读处截断（≤60）；否则原样（只折叠空白）。
   `Coordinator._scrub_note` 仍然先过隐私闸（G7 不放宽），被拒也换新兜底。
   验收提示的 note 字段说明改成：≤60 字、第一句给结论、说清下载/打开什么、口语、
   不写任务号 / 内部文件名 / 工具流程词、不点名关注成员。
B. `coordinator.normalize_scale`（纯函数）+ `_plan`：计划 JSON 的 `scale`
   （brief|standard|full，缺省/写错 = standard，老数据兼容）；brief → deliver_kind 一律
   text、jobs 最多 1 条、清单里加一条「篇幅：几句话回答清楚，不做网页/文件」
   （条数满了就挂到底线 R0 的文字后，不占条数）；standard/full 行为不变；验收提示带 scale
   和「篇幅与原话相符：brief 是几句话，要一页就一页」；scale 记进任务事件便于观测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_coordinator import (  # noqa: F401  （fixtures / 模型桩，见该文件）
    GID,
    FakeDelivery,
    FakeOutbox,
    FakeWorkers,
    ModelsQueue,
    _build,
    _create_task,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)

from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 模型桩 / 小工具
# ---------------------------------------------------------------------------


def _note_mod():
    """延迟导入：先写测试跑红时函数还不存在 → 算失败（不是 skip、不是假绿）。"""
    from CharTyr_MaiWork.maiwork import coordinator

    return coordinator


def _req_mod():
    from CharTyr_MaiWork.maiwork import requirements

    return requirements


def _plan_json(*, scale=None, deliver_kind="view", jobs=None, requirements=None,
               criteria=None) -> str:
    data = {
        "criteria": ["包含链接"] if criteria is None else criteria,
        "deliver_kind": deliver_kind,
        "jobs": jobs if jobs is not None
        else [{"brief": "做一页总结", "tools": ["write_file", "list_files"]}],
        "question": None,
    }
    if requirements is not None:
        data["requirements"] = requirements
    if scale is not None:
        data["scale"] = scale
    return json.dumps(data, ensure_ascii=False)


def _review_json(pass_=True, artifact="", review="通过", note="好了") -> str:
    return json.dumps(
        {"pass": pass_, "review": review, "missing": [], "artifact": artifact, "note": note},
        ensure_ascii=False,
    )


def _prompts(models: ModelsQueue, purpose: str) -> str:
    return "\n".join(
        m.get("content") or ""
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == purpose
        for m in msgs
        if isinstance(m.get("content"), str)
    )


def _coord(*, mem_store, settings, env, tools, tasks, goals, models, workers,
           outbox=None) -> object:
    return _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=workers,
        delivery=FakeDelivery(), outbox=outbox or FakeOutbox(),
    )


def _brief_item(plan: dict) -> list[str]:
    return [
        str(i.get("text") or "")
        for i in (plan.get("requirements") or [])
        if "篇幅" in str(i.get("text") or "")
    ]


def _event_payloads(store: Store, tid: str, kind: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT payload FROM events WHERE kind=? AND entity_id=? ORDER BY id", (kind, tid)
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except (ValueError, TypeError):
            out.append({})
    return out


# ===========================================================================
# A. 交付说明
# ===========================================================================


async def test_note_empty_falls_back_to_human_line_from_title():
    delivery_note_text = _note_mod().delivery_note_text
    assert delivery_note_text("", "整理 NAS 清单") == "整理 NAS 清单弄好了，点开就能看"
    assert delivery_note_text("   \n ", "整理 NAS 清单") == "整理 NAS 清单弄好了，点开就能看"


async def test_note_empty_file_kind_says_file_is_in_link():
    out = _note_mod().delivery_note_text("", "月度账单", "file")
    assert "月度账单" in out
    assert out.endswith("文件在链接里")
    assert "做好了，请查收" not in out


async def test_note_empty_text_kind_stays_short():
    out = _note_mod().delivery_note_text("", "韩国银行事件", "text")
    assert "韩国银行事件" in out
    assert "做好了，请查收" not in out
    assert len(out) <= 60


@pytest.mark.parametrize(
    "bad",
    [
        "看 artifacts/T-1/index.html 就行",
        "T-11 的成品已经放好了",
        "research.md 和 steps/2 都写完了",
        "job2 交回的 报告.py 在这",
        "没搞定，详情在 artifacts/ 下",
    ],
)
async def test_note_with_internal_words_uses_fallback(bad):
    out = _note_mod().delivery_note_text(bad, "整理 NAS 清单")
    assert out == "整理 NAS 清单弄好了，点开就能看"
    for token in ("T-", "job", "research.md", "steps/", "artifacts/", ".py"):
        assert token not in out


async def test_note_long_cuts_at_punctuation_not_mid_sentence():
    note = "一" * 50 + "。" + "二" * 30 + "。"
    out = _note_mod().delivery_note_text(note, "整理")
    assert len(out) <= 60
    assert out == "一" * 50 + "。"


async def test_note_long_without_punctuation_still_bounded():
    out = _note_mod().delivery_note_text("三" * 80, "整理")
    assert out == "三" * 60


async def test_note_short_clean_kept_but_whitespace_folded():
    delivery_note_text = _note_mod().delivery_note_text
    assert delivery_note_text("一" * 60, "整理") == "一" * 60
    assert delivery_note_text("韩国银行  这事是钓鱼邮件引起的", "整理") == \
        "韩国银行 这事是钓鱼邮件引起的"


async def test_note_fallback_title_never_exceeds_limit():
    long_title = "很长的任务标题" * 20
    out = _note_mod().delivery_note_fallback(long_title, "view")
    assert len(out) <= 60
    assert out.endswith("点开就能看")


def _store_with_focus(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
            " VALUES (?, 'ufan', '阿帆', '他最近在备考注册建筑师考试，周三晚上没空', 0, 0, 0)",
            (GID,),
        )
    return store


async def test_scrub_note_privacy_hit_uses_new_fallback(tmp_path):
    store = _store_with_focus(tmp_path)
    coord = _note_mod().Coordinator(
        store, models=None, workers=None, tools=None, tasks=None, goals=None,
        delivery=None, outbox=None, env=None, profiles=None, get_settings=lambda: None,
    )
    out = coord._scrub_note(GID, "给备考注册建筑师考试的朋友顺了一份", "整理 NAS 清单")
    assert out == "整理 NAS 清单弄好了，点开就能看"
    assert "备考" not in out


async def test_scrub_note_clean_and_internal_go_through_note_rule(tmp_path):
    store = Store(tmp_path / "x.db")
    store.migrate()
    coord = _note_mod().Coordinator(
        store, models=None, workers=None, tools=None, tasks=None, goals=None,
        delivery=None, outbox=None, env=None, profiles=None, get_settings=lambda: None,
    )
    # 干净短句原样
    assert coord._scrub_note(GID, "韩国银行这事是钓鱼邮件引起的", "整理") == \
        "韩国银行这事是钓鱼邮件引起的"
    # 内部词 → 标题兜底
    assert coord._scrub_note(GID, "看 artifacts/T-1/index.html", "整理") == \
        "整理弄好了，点开就能看"
    # 空 → 标题兜底；file 类说清文件在链接里
    assert coord._scrub_note(GID, "", "月度账单", "file").endswith("文件在链接里")


async def test_review_prompt_note_field_spec_is_human(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_review_json(note="好了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await coord._review(
        tasks.get(tid), {"criteria": ["真实产物"], "deliver_kind": "text"}, "完成", ["e"], []
    )
    p = _prompts(models, "coordinator.review")
    assert "60 字" in p
    assert "第一句" in p and "结论" in p
    assert "下载" in p and "打开" in p
    assert "工具" in p  # 不许暴露工具/流程词
    assert "不点名关注成员" in p


async def test_handle_passed_empty_note_delivers_human_line(mem_store, settings, env, tools,
                                                            tasks, goals):
    # 线上 T-13 起 text 活先发 reply.md 原文；这条测的是「没有正文 → 回落只发交付说明」
    # 那条路（note 为空时的兜底措辞），所以显式不要 reply.md。
    tid = _create_task(tasks, title="韩国银行AI攻击事件", req="搜搜", reply=None)
    tasks.transition(tid, "running")
    tasks.transition(tid, "reviewing")
    outbox = FakeOutbox()
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(), workers=FakeWorkers(),
                   outbox=outbox)
    await coord._handle_passed(
        tid, GID, tasks.get(tid)["workspace"], {"deliver_kind": "text"},
        {"review": "通过", "note": "", "artifact": ""},
    )
    text = outbox.enqueued[-1]["payload"]["text"]
    assert "做好了，请查收" not in text
    assert text == "韩国银行AI攻击事件弄好了，就这几句"


# ===========================================================================
# B. 规模档 scale
# ===========================================================================


@pytest.mark.parametrize(
    "raw,want",
    [
        ("brief", "brief"),
        ("standard", "standard"),
        ("full", "full"),
        (" BRIEF ", "brief"),
        ("Full", "full"),
        ("huge", "standard"),
        ("", "standard"),
        (None, "standard"),
        (123, "standard"),
    ],
)
async def test_normalize_scale_defaults_to_standard(raw, want):
    assert _note_mod().normalize_scale(raw) == want


async def test_plan_prompt_documents_scale(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks, req="麦麦你搜搜，今天韩国银行被ai攻击了")
    models = ModelsQueue(replies=[_plan_json(scale="brief")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await coord._plan(tasks.get(tid))
    p = _prompts(models, "coordinator.plan")
    assert "scale" in p
    for word in ("brief", "standard", "full"):
        assert word in p
    assert "搜搜" in p and "看看" in p and "查一下" in p and "啥情况" in p
    assert "要一页就一页" in p


async def test_plan_brief_forces_text_and_one_job(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks, title="韩国银行AI攻击事件", req="麦麦你搜搜，今天韩国银行被ai攻击了")
    models = ModelsQueue(replies=[_plan_json(
        scale="brief", deliver_kind="view",
        jobs=[{"brief": "做手机网页", "tools": ["write_file"]},
              {"brief": "再补一轮调研", "tools": ["fetch_page"]}],
        requirements=[{"text": "把韩国银行被攻击的事说清楚", "origin": "原话", "kind": "文稿"}],
    )])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["scale"] == "brief"
    assert plan["deliver_kind"] == "text"
    assert len(plan["jobs"]) == 1
    assert plan["jobs"][0]["brief"].startswith("做手机网页")
    assert plan["jobs"][0]["brief"].endswith(_note_mod().BRIEF_JOB_TAIL)
    items = _brief_item(plan)
    assert len(items) == 1 and "几句话" in items[0]
    # 代码加的这条标「补充」（加分项）：进清单、进提示，但不动 judge 的通过公式
    brief_rows = [i for i in plan["requirements"] if "篇幅" in str(i.get("text") or "")]
    assert brief_rows and brief_rows[0]["origin"] == "补充"
    # 清单照旧锁定（原话条目 + 新加一条 + 底线 R0）
    saved = mem_store.kv_get(f"task.requirements.{tid}")
    assert saved is not None
    assert [i["id"] for i in saved["items"]] == ["R1", "R2", "R0"]


async def test_brief_job_tail_helper_is_idempotent():
    coord = _note_mod()
    once = coord.with_brief_job_tail("做手机网页")
    assert once.startswith("做手机网页")
    assert once.endswith(coord.BRIEF_JOB_TAIL)
    assert once.count(coord.BRIEF_JOB_TAIL) == 1
    # 幂等：已经带了这句就不再追加
    assert coord.with_brief_job_tail(once) == once
    # 存档按 200 字截断、整句被截掉尾巴时也不许再追加一遍（只看开头那半句）
    truncated = ("长说明" * 80)[:170] + coord.BRIEF_JOB_TAIL[:20]
    assert coord.with_brief_job_tail(truncated) == truncated
    # 空 brief 原样（不凭空造一条活）
    assert coord.with_brief_job_tail("") == ""
    assert coord.with_brief_job_tail(None) == ""


async def test_plan_brief_appends_tail_to_the_single_job(mem_store, settings, env, tools,
                                                         tasks, goals):
    tid = _create_task(tasks, req="搜搜韩国银行")
    coord = _note_mod()
    models = ModelsQueue(replies=[_plan_json(
        scale="brief", deliver_kind="view",
        jobs=[{"brief": "做手机网页", "tools": ["write_file"]},
              {"brief": "再补调研", "tools": ["fetch_page"]}],
    )])
    c = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
               tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await c._plan(tasks.get(tid))
    assert len(plan["jobs"]) == 1
    brief = plan["jobs"][0]["brief"]
    assert brief.count(coord.BRIEF_JOB_TAIL) == 1
    assert brief.endswith(coord.BRIEF_JOB_TAIL)
    assert "不做网页、文件或长报告" in brief


async def test_plan_standard_does_not_append_brief_job_tail(mem_store, settings, env, tools,
                                                            tasks, goals):
    tid = _create_task(tasks, req="整理成一页网页")
    coord = _note_mod()
    jobs = [{"brief": "调研", "tools": ["fetch_page"]},
            {"brief": "做页面", "tools": ["write_file"]}]
    models = ModelsQueue(replies=[_plan_json(scale="standard", deliver_kind="view", jobs=jobs)])
    c = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
               tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await c._plan(tasks.get(tid))
    assert [j["brief"] for j in plan["jobs"]] == ["调研", "做页面"]
    assert coord.BRIEF_JOB_TAIL not in "\n".join(j["brief"] for j in plan["jobs"])


async def test_plan_brief_item_survives_next_round_without_duplicate(mem_store, settings, env,
                                                                    tools, tasks, goals):
    tid = _create_task(tasks, req="搜搜韩国银行")
    models = ModelsQueue(replies=[
        _plan_json(scale="brief", deliver_kind="view",
                   requirements=[{"text": "说清这件事", "origin": "原话", "kind": "文稿"}]),
        _plan_json(scale="brief", deliver_kind="view"),
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    first = await coord._plan(tasks.get(tid))
    second = await coord._plan(tasks.get(tid))
    assert len(_brief_item(first)) == 1
    assert len(_brief_item(second)) == 1
    assert second["deliver_kind"] == "text"
    assert len(second["requirements"]) == len(first["requirements"])


async def test_plan_brief_without_requirements_keeps_old_logic_but_criteria_note(
    mem_store, settings, env, tools, tasks, goals
):
    tid = _create_task(tasks, req="搜搜韩国银行")
    models = ModelsQueue(replies=[_plan_json(scale="brief", deliver_kind="file")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["requirements"] is None
    assert mem_store.kv_get(f"task.requirements.{tid}") is None
    assert plan["deliver_kind"] == "text"
    assert any("篇幅" in str(c) for c in plan["criteria"])


async def test_plan_standard_and_full_keep_shape(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks, req="整理成一页网页")
    jobs = [{"brief": "调研", "tools": ["fetch_page"]}, {"brief": "做页面", "tools": ["write_file"]}]
    models = ModelsQueue(replies=[
        _plan_json(scale="standard", deliver_kind="view", jobs=jobs),
        _plan_json(scale="full", deliver_kind="view", jobs=jobs),
    ])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    standard = await coord._plan(tasks.get(tid))
    assert standard["scale"] == "standard"
    assert standard["deliver_kind"] == "view"
    assert len(standard["jobs"]) == 2
    full = await coord._plan(tasks.get(tid))
    assert full["scale"] == "full"
    assert full["deliver_kind"] == "view"
    assert len(full["jobs"]) == 2


async def test_plan_missing_or_bad_scale_is_standard(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan_json(scale=None), _plan_json(scale="huge")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    first = await coord._plan(tasks.get(tid))
    second = await coord._plan(tasks.get(tid))
    assert first["scale"] == "standard" and second["scale"] == "standard"
    assert first["deliver_kind"] == "view" and second["deliver_kind"] == "view"


async def test_plan_scale_is_recorded_in_event_and_log(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks, req="搜搜")
    models = ModelsQueue(replies=[_plan_json(scale="brief", deliver_kind="view")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await coord._plan(tasks.get(tid))
    payloads = _event_payloads(mem_store, tid, "task.plan.scale")
    assert payloads and payloads[-1].get("scale") == "brief"


async def test_review_prompt_carries_scale_and_length_criterion(mem_store, settings, env, tools,
                                                                tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_review_json()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    await coord._review(
        tasks.get(tid),
        {"criteria": ["真实产物"], "deliver_kind": "text", "scale": "brief"},
        "完成", ["e"], [],
    )
    p = _prompts(models, "coordinator.review")
    assert "brief" in p
    assert "篇幅与原话相符：brief 是几句话，要一页就一页" in p


async def test_requirements_with_brief_scale_respects_item_limit():
    req = _req_mod()
    raw = [
        {"text": f"要求 {i}", "origin": "原话", "kind": "实做"} for i in range(1, 7)
    ]
    items = req.normalize_requirements(raw, "原话")
    assert len([i for i in items if i["id"] != req.FLOOR_ID]) == req.MAX_ITEMS
    out = req.with_brief_scale(items)
    # 条数满了：不占条数，挂到底线 R0 的文字后面
    assert len([i for i in out if i["id"] != req.FLOOR_ID]) == req.MAX_ITEMS
    assert req.BRIEF_SCALE_TEXT in out[-1]["text"]
    assert out[-1]["id"] == req.FLOOR_ID
    # 幂等：再来一次不变
    assert req.with_brief_scale(out) == out


async def test_brief_scale_item_is_bonus_and_does_not_change_pass_formula():
    """代码加的篇幅要求标「补充」：模型不判它也不影响 judge 的 pass（不改通过公式）。"""
    req = _req_mod()
    items = req.with_brief_scale(
        req.normalize_requirements(
            [{"text": "把这事说清楚", "origin": "原话", "kind": "文稿"}], "搜搜韩国银行"
        )
    )
    bonus = [i for i in items if req.BRIEF_SCALE_TEXT in str(i["text"])]
    assert len(bonus) == 1
    assert bonus[0]["origin"] == req.ORIGIN_BONUS
    assert req.is_blocking(bonus[0]) is False
    verdicts = [
        {"id": i["id"], "met": True, "evidence": "证据在这"}
        for i in items
        if i["id"] != bonus[0]["id"]
    ]
    assert req.judge(items, verdicts)["pass"] is True
    # 计划 / 验收提示里看得到（加分项）
    assert req.BRIEF_SCALE_TEXT in req.prompt_line(bonus[0])
    assert any(req.BRIEF_SCALE_TEXT in c for c in req.criteria_texts(items))
