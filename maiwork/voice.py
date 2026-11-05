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
    "- 口吻按上面「MaiWork 的身份」来（身份设定只管语气和选词，绝不在话里提自己的名字、昵称、年龄、性别、所在城市或「作为玩家 / 鉴赏家」这类人设标签）；没有就用自然的口语。",
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


# SOUL 里声明的名字 / 昵称
_SOUL_IAM = re.compile(r"我是\s*([^\s（(，,。；;：:\n]{2,12})")
_SOUL_IAM_SKIP = re.compile(r"^(?:说|真|觉得|在|不是|想|有点|一个|一名|群里|本群|这群|这个群)")
# 「我是玩家」这类泛称不是名字，不拿来拦话
_GENERIC_NOUNS = frozenset({"玩家", "老玩家", "游戏玩家", "朋友", "老熟人", "熟人", "群友",
                            "气氛组", "搭档", "助手", "机器人", "AI", "ai"})
_SOUL_NICK = re.compile(r"(?:你叫|名叫|昵称[是为：:\s]*)\s*([^\n。；;！!？?（(]{2,30})")
_SOUL_SPLIT = re.compile(r"[、，,/／\s]+")

# 自贴人设标签（「作为续作」这类不是人的不算）
_PERSONA_TAG = (
    re.compile(r"我作为[^\s，,。！？!?]{1,12}"),
    re.compile(r"作为(?:一个|一名|重度|深度|资深)?(?:游戏玩家|玩家|女生|男生|艺术鉴赏家|鉴赏家)"),
)

# 思考草稿 / 复述提示词（开场白护栏）
_LEAKED_DRAFT = (
    re.compile(r"(?:不超过|别超过|控制在)\s*\d+\s*字"),
    re.compile(r"\d+\s*字以[内下]"),
    re.compile(r"字数(?:算一下|够了|差不多)"),
    re.compile(r"不点名|不招揽|只回开场白|不用问句结尾|不必以问句结尾"),
    re.compile(r"冷场(?:了|之后).{0,15}(?:聊一句|起个话头|随口)"),
    re.compile(r"(?:我得|我要|得|先)(?:是|用|按|保持).{0,15}口吻"),
    re.compile(r"(?:^\s*(?:用|按)|[，,。！？!?\n]\s*(?:用|按)|(?:我|咱|先|得|要|注意|保持)[^\s，,。！？!?]{0,4}[是用按保持])[^\s，,。！？!?]{1,12}的?口吻"),
    re.compile(r"挑(?:一?个)?具体(?:细节|点|切口)\s*[：:]"),
    re.compile(r"</?think>|<｜end▁of▁thinking｜>|<\|end_of_thinking\|>|(?:^|\s)end\s+[\u4e00-\u9fff]"),
)


def soul_names(soul: Any) -> tuple[str, ...]:
    """SOUL 里声明的名字 / 昵称（「我是X」「你叫X」「昵称：X、Y」），按出现顺序去重。"""
    text = str(soul or "")
    out: list[str] = []
    for m in _SOUL_IAM.finditer(text):
        name = m.group(1).strip()
        if name and not _SOUL_IAM_SKIP.match(name) and name not in _GENERIC_NOUNS and name not in out:
            out.append(name)
    for m in _SOUL_NICK.finditer(text):
        for tok in _SOUL_SPLIT.split(m.group(1)):
            tok = tok.strip().strip("「」『』\"'“”‘’")
            if 2 <= len(tok) <= 12 and tok not in _GENERIC_NOUNS and tok not in out:
                out.append(tok)
    return tuple(out)


def is_leaked_draft(text: Any) -> bool:
    """这句话是不是混进了思考草稿 / 复述提示词（纯函数）。"""
    s = str(text or "")
    if not s.strip():
        return False
    return any(p.search(s) for p in _LEAKED_DRAFT)


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

    def leaks_identity(self, text: Any, *, allow_text: Any = "") -> bool:
        """话里有没有露人设：自我介绍、提 MaiWork / MaiBot、自贴人设标签、说出 SOUL 里的名字。
        allow_text（如话题标题 / 简报）里本来就有的词放过。"""
        if not text:
            return False
        if is_self_intro(text):
            return True
        s = str(text).strip()
        allow_low = str(allow_text or "").lower()
        low = s.lower()
        for word in ("maiwork", "maibot"):
            if word in low and word not in allow_low:
                return True
        if any(p.search(s) for p in _PERSONA_TAG):
            return True
        for name in soul_names(self.soul):
            if name.lower() in allow_low:
                continue
            if name.isascii():
                hit = re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", s, re.IGNORECASE)
            else:
                hit = name in s
            if hit:
                return True
        return False


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
