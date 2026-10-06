"""card_push 构想提一嘴的兜底话（2026-10 第二步：措辞只落到具体的事）。

兜底只用构想自己的字段（title / step / items 里安全的、规范化过的那一句），
不用没核实过的 origin —— 那是模型自己写的，不能拿来对群友说「你之前说过」；
也不再拼「突然想到一件事，要不要我来弄？」这种空话。说不出具体事就返回
空串（由 flush 记固定原因作废），不硬凑一句万金油。

纯函数，同步测试（不调模型、不碰库）。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork import card_push, voice

UID = "31415926"


def test_fallback_uses_normalized_title_action():
    act = card_push._concrete_action("我可以帮群里做个番剧追更表", "", [], "")
    assert act == "做个番剧追更表"
    assert card_push._fallback(act, False, "") == "我可以帮群里做个番剧追更表，要不要我来弄？"


def test_fallback_personal_and_no_qq():
    act = card_push._concrete_action("我可以帮你把例程理一下", "", [], UID)
    t = card_push._fallback(act, True, UID)
    assert t == "我可以帮你把例程理一下，要不要我来弄？"
    assert UID not in t
    for w in ("画像", "注意到", "你最近", "记得你"):
        assert w not in t


def test_fallback_prefers_step_then_item_then_title():
    assert card_push._concrete_action("标题", "先列出要跑的例程", [], "") == "列出要跑的例程"
    items = [{"kind": "task", "title": "例程清单", "desc": "把例程列成一张表"}]
    assert card_push._concrete_action("标题", "", items, "") == "把例程列成一张表"
    assert card_push._concrete_action("我可以帮群里做个周报", "", [], "") == "做个周报"


def test_fallback_empty_when_no_concrete_source():
    assert card_push._concrete_action("", "", [], "") == ""
    assert card_push._concrete_action("给群里做点事", "", [], "") == ""
    assert card_push._fallback("", False, "") == ""
    assert card_push._fallback("", True, UID) == ""


def test_fallback_drops_leaky_title_and_never_self_intro():
    assert card_push._concrete_action("我可以帮你，记得你说过想考研", "", [], UID) == ""
    for title, personal, at in (
        ("我可以帮群里做个番剧追更表", False, ""),
        ("我可以帮你把例程理一下", True, UID),
    ):
        act = card_push._concrete_action(title, "", [], at)
        t = card_push._fallback(act, personal, at)
        assert t.endswith("？")
        assert not voice.is_self_intro(t), t


def test_fallback_is_not_the_old_generic_line():
    text = card_push._fallback("做个番剧追更表", False, "")
    assert "突然想到" not in text and "搭把手" not in text
