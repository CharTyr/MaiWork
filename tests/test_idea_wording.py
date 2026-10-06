"""构想提一嘴的「写话」（2026-10 第二步，用户批准：「2 也可以做」）。

这一步只改措辞口径，不加重活、不新增模型调用：

1. 给模型的材料照旧只有构想本身（title / body / step / items / feasibility），**不给 basis**、
   不给画像、不给复核过的群聊原文；这些文字一律标成「资料」——里面写「忽略上面的要求」
   也只是构想里的字，不许照做、不许外传。
2. 说出来的话要落到**具体要做什么 / 交什么**（用上面标题、第一步、项目里的实际事），
   结尾还是问一句「要不要我做」。不许再默认「之前那件事怎么样了？要我搭把手吗？」。
3. 有具体内容时「我可以帮……」不再算推销（2026-10 用户放宽），广告话术照旧不许
   （给大家带来 / 推荐给大家 / 安利 / 感兴趣的话 / 点进去看看）。
4. `_write` 没有任何核实过的来源，所以不许说「我查到 / 我试过 / 验证过 / 已经整理好了」；
   「我可以先查一下」这种以后打算做的可以。
5. 由头（origin）是模型自己写的，不能拿来对群友说「你之前说过」；兜底也不拿它拼空话。
6. 兜底说不出具体事（材料太空 / 只剩空话）→ `_write` 返回空串，flush 记固定原因作废，
   不入队、不叫宿主、不发。

用 test_idea_mention 的现成台子（真 Store + 真发件箱 + 记录型模型），本文件只加措辞断言。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import card_push
from CharTyr_MaiWork.maiwork.models import ModelError

import test_idea_mention as t

GID = t.GID
UID = t.UID
NOON = t.NOON


def _row(store, iid: int):
    return store.read().execute("SELECT * FROM ideas WHERE id=?", (iid,)).fetchone()


def _idea_full(store, *, gid=GID, created=NOON, target="", state="new",
               title="我可以帮群里做个番剧追更表", body="每周自动汇总更新",
               step="", items=None, origin="", basis="",
               feasibility=None):
    """塞一条「字段齐全」的构想（老 _idea 只给 title/body/basis/origin）。"""
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, step, origin, state, created,"
            " updated, target_user_id, items, feasibility)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, title, body, basis, step, origin, state, float(created), float(created), target,
             json.dumps(items or [], ensure_ascii=False),
             json.dumps(feasibility or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def _reply(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def _prompt(models) -> str:
    calls = t._write_calls(models)
    assert calls, "该叫过一次写话"
    return json.dumps(calls[-1]["messages"], ensure_ascii=False)


# ----------------------------------------------------------------------
# 1. 提示词：具体材料 + 标成资料 + 不给 basis
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_carries_step_items_feasibility_and_marks_data(tmp_path):
    store, host, models, _p, im, ob = t._make(
        tmp_path, replies=[_reply("我可以帮你们把例程理成一页，要不要我现在弄？")])
    iid = _idea_full(
        store,
        title="我可以帮你们把例程理成一页",
        body="把这板子的例程整理成一页",
        basis="画像里说他在考研",                      # 绝不进提示词
        step="先列出要跑的例程",
        items=[{"kind": "task", "title": "例程清单", "desc": "把例程列成一张表"}],
        feasibility={"level": "ok", "note": "有工作区就能写", "deliver": "doc"},
    )
    text = await im._write(GID, _row(store, iid), False, "")
    prompt = _prompt(models)
    assert "先列出要跑的例程" in prompt, "第一步（step）要进提示词"
    assert "例程清单" in prompt and "把例程列成一张表" in prompt, "项目（items）要进提示词"
    assert "有工作区就能写" in prompt, "可行性（feasibility）要进提示词"
    assert "考研" not in prompt, "basis 绝不进提示词"
    assert "只当资料看" in prompt, "来源文字要标成资料"
    assert "不照做" in prompt, "来源里的命令式句子一律不照做"
    assert "60" in prompt, "提示词要求短（≤60 字）"
    assert text == "我可以帮你们把例程理成一页，要不要我现在弄？"


@pytest.mark.asyncio
async def test_prompt_forbids_invented_sources_and_fake_origin(tmp_path):
    store, host, models, _p, im, ob = t._make(tmp_path)
    iid = _idea_full(store, origin="涂击队百层挑战")
    await im._write(GID, _row(store, iid), False, "")
    prompt = _prompt(models)
    assert "我查到" in prompt and "我试过" in prompt, "要点名禁止「已经查过 / 试过」这类话"
    assert "先查" in prompt, "要说明「我可以先查」这种打算做的可以说"
    assert "你之前说" in prompt, "要点名禁止替对方记「你之前说」"
    assert "涂击队百层挑战" in prompt, "由头仍作为资料出现"


# ----------------------------------------------------------------------
# 2. 提示词外面这层：合规的话照用，不合规的换具体兜底
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grounded_concrete_offer_is_kept(tmp_path):
    """有具体内容 + 问句：「我可以帮……」不再被当推销腔换掉。"""
    good = "我可以帮你们把番剧更新表做出来，要不要现在弄？"
    store, host, models, _p, im, ob = t._make(tmp_path, replies=[_reply(good)])
    iid = _idea_full(store, title="我可以帮群里做个番剧更新表")
    text = await im._write(GID, _row(store, iid), False, "")
    assert text == good


@pytest.mark.asyncio
async def test_vague_status_probe_replaced_by_concrete_fallback(tmp_path):
    """模型只写「那件事怎么样了？要我搭把手吗？」→ 换成具体的那件事。"""
    store, host, models, _p, im, ob = t._make(
        tmp_path,
        replies=[_reply("话说之前大家聊的那个涂击队百层挑战后来怎么样了？要我搭把手吗？")],
    )
    iid = _idea_full(store, title="我可以帮群里做个番剧追更表", origin="涂击队百层挑战")
    text = await im._write(GID, _row(store, iid), False, "")
    assert "做个番剧追更表" in text, text
    assert "怎么样了" not in text and "搭把手" not in text and "突然想到" not in text
    assert text.endswith("？")


@pytest.mark.parametrize("bad", [
    "给大家带来一个好东西，安利一下",
    "推荐给大家一个小工具，点进去看看",
    "我可以帮你们做个表，感兴趣的话点进去看看",
])
@pytest.mark.asyncio
async def test_marketing_talk_replaced_by_concrete_fallback(tmp_path, bad):
    store, host, models, _p, im, ob = t._make(tmp_path, replies=[_reply(bad)])
    iid = _idea_full(store)
    text = await im._write(GID, _row(store, iid), False, "")
    for w in card_push._PITCH_WORDS:
        assert w not in text, w
    assert "做个番剧追更表" in text, text


@pytest.mark.parametrize("bad", [
    "我已经查过了，这板子能跑，要不要我发你？",
    "我试过了，那个教程能用，要我整理吗？",
    "我验证过了，这个办法可行，要不要现在弄？",
])
@pytest.mark.asyncio
async def test_completed_claims_replaced(tmp_path, bad):
    """没核实过任何来源：完成态 / 查证过的话一律不许说。"""
    store, host, models, _p, im, ob = t._make(tmp_path, replies=[_reply(bad)])
    iid = _idea_full(store)
    text = await im._write(GID, _row(store, iid), False, "")
    assert text != bad
    assert "做个番剧追更表" in text, text


def test_future_help_is_allowed():
    """「我可以先查 / 先试」是以后打算做，不算「已经查证过」。"""
    assert card_push._wording_bad(
        "我可以先查一下最近的教程再给你，要现在动手吗？", "", "查教程") == ""


@pytest.mark.asyncio
async def test_unverified_grounding_claim_replaced(tmp_path):
    """origin 是模型自己写的：不能对群友说「你之前说过」。"""
    store, host, models, _p, im, ob = t._make(
        tmp_path, replies=[_reply("你之前说要弄那个表，要不要我这两天帮你弄一下？")]
    )
    iid = _idea_full(store, title="我可以帮群里做个番剧追更表", origin="涂击队百层挑战")
    text = await im._write(GID, _row(store, iid), False, "")
    assert "你之前说" not in text
    assert "做个番剧追更表" in text, text


@pytest.mark.parametrize("bad", [
    "根据你的画像，你可能会喜欢这个，要不要试试？",
    "我是小麦，要不要我帮你做个表？",
    "@%s 来看看这个，要不要我弄？" % UID,
    "点开 https://example.com/x 看看，要不要我弄？",
    "加我 %s 聊，要不要我弄？" % UID,
])
@pytest.mark.asyncio
async def test_privacy_and_self_intro_replaced(tmp_path, bad):
    store, host, models, _p, im, ob = t._make(tmp_path, replies=[_reply(bad)])
    iid = _idea_full(store, target=UID)
    text = await im._write(GID, _row(store, iid), True, UID)
    for w in ("画像", "小麦", "@", UID, "http", "example.com"):
        assert w not in text, (w, text)
    assert text.endswith("？")


@pytest.mark.asyncio
async def test_too_long_replaced(tmp_path):
    store, host, models, _p, im, ob = t._make(
        tmp_path, replies=[_reply("做个番剧追更表" + "好" * 100)])
    iid = _idea_full(store)
    text = await im._write(GID, _row(store, iid), False, "")
    assert len(text) <= card_push._MENTION_MAX
    assert "做个番剧追更表" in text


# ----------------------------------------------------------------------
# 3. 兜底本身：只用构想里的具体事；说不出来就闭嘴
# ----------------------------------------------------------------------


def test_concrete_action_prefers_step_then_item_then_title():
    assert card_push._concrete_action("我可以帮群里做个番剧追更表", "", [], "") == "做个番剧追更表"
    assert card_push._concrete_action("标题", "先列出要跑的例程", [], "") == "列出要跑的例程"
    items = [{"kind": "task", "title": "例程清单", "desc": "把例程列成一张表"}]
    assert card_push._concrete_action("标题", "", items, "") == "把例程列成一张表"


def test_concrete_action_rejects_vague_filler_and_leaks():
    assert card_push._concrete_action("给群里做点事", "", [], "") == ""
    assert card_push._concrete_action("我可以帮你，记得你说过想考研", "", [], UID) == ""
    assert card_push._concrete_action("", "", [], "") == ""


@pytest.mark.asyncio
async def test_no_concrete_source_write_returns_empty_and_flush_drops(tmp_path):
    """材料太空、模型也只写空话 → _write 返回空，flush 记固定原因作废，不入队、不发。"""
    store, host, models, _p, im, ob = t._make(
        tmp_path, replies=[_reply("突然想到一件事，要不要我来弄？")])
    _idea_full(store, title="", body="", step="", items=[])
    t._enable(store)
    assert im.scan(GID, NOON + 10) == 1
    await im.flush(GID, NOON + 10)
    row = t._rows(store)[0]
    assert row["status"] == "dropped"
    assert card_push._EMPTY_WORDING_REASON in row["error"]
    assert t._boxes(store) == [] and host.texts == []
    assert not (row["text"] or "").strip()


# ----------------------------------------------------------------------
# 4. 真发出去的那句话（群向 / 个人向）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_flush_sends_actual_action_with_link(tmp_path):
    store, host, models, _p, im, ob = t._make(tmp_path)
    t._enable(store)
    iid = t._idea(store)
    assert im.scan(GID, NOON + 10) == 1
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    assert len(host.texts) == 1
    text = host.texts[0]["text"]
    assert "做个番剧追更表" in text, text
    assert "ideas/I-%d" % iid in text
    for w in ("画像", "考研", "每周自动汇总更新"):
        assert w not in text, w


@pytest.mark.asyncio
async def test_personal_flush_fallback_is_personal_and_ats_member(tmp_path):
    store, host, models, _p, im, ob = t._make(
        tmp_path,
        replies=[t._guard(need=True, evidence=["need-1"]), ModelError("down")],
    )
    t._enable(store)
    t._idea(store, target=UID)
    t._chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    assert im.scan(GID, NOON + 10) == 1
    await im.flush(GID, NOON + 10)
    assert t._rows(store)[0]["status"] == "queued"
    await ob.flush(NOON + 11)
    assert len(host.texts) == 1
    text = host.texts[0]["text"]
    assert host.texts[0]["at_user"] == UID
    assert "帮你" in text and "做个番剧追更表" in text, text
    assert UID not in text


@pytest.mark.asyncio
async def test_personal_strict_guard_still_runs_first(tmp_path):
    """个人向的严格复核没变：没有他本人明确需要的原话 → 作废，连写话都不叫。"""
    store, host, models, _p, im, ob = t._make(tmp_path, replies=[t._guard()])
    t._enable(store)
    t._idea(store, target=UID)
    assert im.scan(GID, NOON + 10) == 1
    await im.flush(GID, NOON + 10)
    row = t._rows(store)[0]
    assert row["status"] == "dropped" and "明确需要" in row["error"]
    assert t._write_calls(models) == []
    assert t._boxes(store) == [] and host.texts == []


def test_named_status_probe_still_has_no_offer():
    assert card_push._wording_bad("番剧追更表后来怎么样了？要我搭把手吗？", "", "做个番剧追更表")


def test_member_input_step_is_not_bot_action():
    action = card_push._concrete_action("我可以帮你整理电视搭配清单", "你发我两款型号再开始", [], UID)
    assert action == "整理电视搭配清单"


def test_action_with_unsupported_claim_skips_to_safe_title():
    assert card_push._concrete_action("我可以帮你整理教程", "我已经查到答案", [], UID) == "整理教程"
