"""voice.py：MaiWork 开口说话（开场白 / 构想提一嘴）的人设。

2026-10-01 用户定：MaiWork 所有人设只认 SOUL.md（identity 的 main SOUL）。
SOUL.md 可以在网页上一键从 MaiBot 导入人格（手动、不自动跟），也可以随便改；
运行时**绝不**回退去读 MaiBot 的人格设定（host.config）或拿 MaiBot 的发言当样例。
人设只管说话风格：不自我介绍、不寒暄、不说废话，直接说事。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from CharTyr_MaiWork.maiwork import voice


class Identity:
    def __init__(self, blocks: Dict[str, str] | None = None, *, boom: bool = False) -> None:
        self.blocks = dict(blocks or {})
        self.boom = boom
        self.kinds: List[str] = []

    def prompt_block(self, kind: str, **kw: Any) -> str:
        self.kinds.append(kind)
        if self.boom:
            raise RuntimeError("identity 挂了")
        return self.blocks.get(kind, "")


SOUL = "## MaiWork 的身份\n# 我是谁\n\n我是小麦。性格：热心肠\n# 说话方式\n随口一聊"


def test_soul_only_drives_voice() -> None:
    identity = Identity({"soul": SOUL})
    p = voice.persona(identity)
    assert identity.kinds == ["soul"]
    section = p.section()
    assert section.startswith("## MaiWork 的身份")
    assert "随口一聊" in section
    assert "# 人设" not in section           # 不再拼 MaiBot 的人设行
    assert p.system().startswith("## MaiWork 的身份")


def test_section_forbids_self_intro_and_small_talk() -> None:
    for p in (voice.persona(Identity({"soul": SOUL})), voice.persona(None)):
        s = p.section()
        assert "不自我介绍" in s
        assert "作为 AI" in s
        assert "大家好" in s and "不寒暄" in s
        assert "直接说事" in s


def test_no_soul_means_no_persona_not_maibot() -> None:
    """SOUL 空：不带人设，也不去读 MaiBot（persona 根本不收 host）。"""
    p = voice.persona(Identity({"soul": ""}))
    assert p.soul == ""
    assert "MaiWork 的身份" not in p.section()
    assert "助手" not in p.system()          # 兜底的 system 也不给模型塞「助手」身份
    assert p.system()


def test_identity_errors_never_raise() -> None:
    p = voice.persona(Identity(boom=True))
    assert p.soul == ""
    assert voice.persona(None).soul == ""


def test_persona_has_no_host_reading() -> None:
    """接口上就不收 host：杜绝哪天又偷偷回退读 MaiBot 人格。"""
    import inspect

    assert list(inspect.signature(voice.persona).parameters) == ["identity"]
    assert not hasattr(voice, "persona_context")
    src = inspect.getsource(voice)
    assert "personality." not in src and "bot.nickname" not in src


@pytest.mark.parametrize("text", [
    "我是小麦，话说之前那个百层挑战后来怎么样了？",
    "我叫小麦，想问问大家最近在玩啥",
    "大家好，话说之前那个百层挑战怎么样了？",
    "打扰一下，之前那个复习资料还要吗？",
    "作为 AI 助手，我想问问那个板子怎么样了？",
    "作为一个机器人，我觉得这个挺有意思",
    "嗨，我是群里的 AI 助手，那个表格还要吗？",
    "各位好，最近有个新闻挺有意思",
])
def test_self_intro_detected(text: str) -> None:
    assert voice.is_self_intro(text) is True


@pytest.mark.parametrize("text", [
    "话说之前大家聊的那个涂击队百层挑战后来怎么样了？要我帮忙吗？",
    "突然想到，做个番剧追更表这事要不要我来弄？",
    "我是说之前那个板子，后来到货了没？",
    "我是真没想到战锤 40K 出新预告了",
    "大家觉得这个新预告怎么样？",
    "",
])
def test_normal_talk_not_flagged(text: str) -> None:
    assert voice.is_self_intro(text) is False
