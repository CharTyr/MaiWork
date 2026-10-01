"""card_push._template：构想提一嘴的固定模板（关心式问法，不点名 / 不露画像 / 不推销）。

模板四种情况：有由头（群向 / 个人向）、没由头用标题、标题或由头本身命中词表时用通用问句。
纯函数，同步测试（不调模型、不碰库）。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork import card_push

UID = "31415926"


def test_template_with_origin_group():
    """有 origin（群向）：不点名任何人，顺口问一句要不要帮忙。"""
    t = card_push._template("我可以帮群里做个番剧追更表", False, "", "涂击队百层挑战")
    assert t == "话说之前大家聊的那个涂击队百层挑战后来怎么样了？要我帮忙吗？"


def test_template_with_origin_personal():
    """有 origin（个人向）：只说他自己说过想做的那件事，不带 QQ 号。"""
    t = card_push._template("我可以帮你把例程理一下", True, UID, "FPGA 小板子")
    assert t == "话说你之前想弄的那个FPGA 小板子怎么样了？要我搭把手吗？"
    assert UID not in t
    for w in ("画像", "注意到", "你最近", "记得你"):
        assert w not in t


def test_template_without_origin_uses_title_body():
    """没 origin：标题去掉「我可以帮…」的头，拼成一句顺口的问话。"""
    t = card_push._template("我可以帮群里做个番剧追更表", False, "")
    assert t == "突然想到，做个番剧追更表这事要不要我来弄？"


def test_template_leaky_title_falls_back_to_generic():
    """标题本身泄漏（提到他的情况）→ 用不带标题的通用问句。"""
    t = card_push._template("我可以帮你，记得你说过想考研", True, UID)
    assert "考研" not in t and "记得你" not in t
    assert t.endswith("？")


def test_template_leaky_origin_ignored():
    """origin 命中词表（带 @ / 名字）→ 不用它，回落标题路径。"""
    t = card_push._template("我可以帮群里看看", False, "", "@阿帆的手册")
    assert "@阿帆的手册" not in t
    assert "突然想到" in t
    assert t.endswith("？")


def test_template_title_without_pitch_head_generic():
    t = card_push._template("给群里做点事", False, "")
    assert "给群里做点事" not in t
    assert t.endswith("？")


def test_templates_never_self_intro():
    from CharTyr_MaiWork.maiwork import voice

    for personal in (False, True):
        for origin, title in (("涂击队百层挑战", ""), ("", "我可以帮大家做个番剧追更表"), ("", "")):
            t = card_push._template(title, personal, "10001", origin)
            assert not voice.is_self_intro(t), t
