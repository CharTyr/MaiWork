"""voice.py：MaiWork 自己开口（开场白 / 构想提一嘴）时的人设与口吻规矩。

2026-10-01 用户定：**MaiWork 所有人设只认 SOUL.md**（identity 的 main SOUL）。
- SOUL.md 可以在网页上一键「从 MaiBot 同步」导入人格——只在点按钮时导入，不自动跟着变；
  管理员改过的 SOUL.md 就按改过的来。
- 运行时**绝不**回退去读 MaiBot 的人格设定（宿主配置里的昵称 / 性格 / 回复风格），
  也不拿 MaiBot 的发言当语气样例。SOUL 空就是没人设，自然口语。
- 人设只管说话风格：不自我介绍、不寒暄、不说废话，直接说事。生成结果再用
  is_self_intro 兜一道（开场白命中就这次不开；构想提一嘴命中就换模板）。

读不到 SOUL（identity 没就位 / 出错）一律当空，绝不抛。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("maiwork.voice")

# 提示词里的口吻规矩（开场白和构想提一嘴共用；测试按这几句认）
SPEAK_RULES = (
    "# 说话要求",
    "- 口吻按上面「MaiWork 的身份」来；没有就用自然的口语。",
    "- 不自我介绍：不说「我是……」「我叫……」，不说「作为 AI / 助手 / 机器人」。",
    "- 不寒暄、不铺垫：不说「大家好」「各位好」「打扰一下」这类开场，直接说事。",
)
_NO_SOUL_RULES = (
    "# 说话要求",
    "- 用自然的口语。",
    *SPEAK_RULES[2:],
)
# SOUL 空时的 system：只说在干嘛，不塞任何身份（塞「助手」会引出「作为助手……」）
_PLAIN_SYSTEM = "你在一个 QQ 群里说一两句话。"

# 自我介绍 / 寒暄（生成结果的护栏）
_SELF_INTRO = (
    # 开头就「我是 / 我叫」——「我是说」「我是真…」「我是觉得」这类口头语放过
    re.compile(r"^\s*(?:嗨|哈喽|hi|hello|你们好)?[，,！!～~\s]*我(?:是(?!说|真|觉得|在|不是|想|有点)|叫)",
               re.IGNORECASE),
    # 「我是群里的 AI 助手」这类，出现在哪都算
    re.compile(r"我是(?:你们|大家|群里|本群|这个群)?的?\s*(?:AI|人工智能|助手|小助手|群助手|机器人|MaiWork|MaiBot)",
               re.IGNORECASE),
    re.compile(r"作为(?:一个|一名|你们的|大家的|群里的)?\s*(?:AI|人工智能|助手|小助手|群助手|机器人|语言模型)",
               re.IGNORECASE),
    # 寒暄开场
    re.compile(r"^\s*(?:大家好|各位好|大家晚上好|大家早上好|大家下午好|打扰一下|打扰大家|打扰了)"),
)


def is_self_intro(text: Any) -> bool:
    """这句话是不是在自我介绍 / 寒暄开场（纯函数）。"""
    s = str(text or "").strip()
    if not s:
        return False
    return any(p.search(s) for p in _SELF_INTRO)


@dataclass
class Persona:
    """MaiWork 开口时的人设：只有 SOUL（可能为空）。"""

    soul: str = ""

    def section(self) -> str:
        """提示词里的段落：SOUL（有就放最前面）+ 口吻规矩。"""
        soul = str(self.soul or "").strip()
        rules = "\n".join(SPEAK_RULES if soul else _NO_SOUL_RULES)
        return f"{soul}\n\n{rules}" if soul else rules

    def system(self) -> str:
        """system 那一句：有 SOUL 就是 SOUL；没有就只说在干嘛。"""
        soul = str(self.soul or "").strip()
        return soul or _PLAIN_SYSTEM


def persona(identity: Any) -> Persona:
    """读 SOUL（identity.prompt_block("soul")）；没有 identity / 读不到 → 空人设，绝不抛。"""
    if identity is None:
        return Persona()
    try:
        soul = str(identity.prompt_block("soul") or "").strip()
    except Exception:
        logger.debug("读 SOUL 失败，按没有处理", exc_info=True)
        soul = ""
    return Persona(soul=soul)
