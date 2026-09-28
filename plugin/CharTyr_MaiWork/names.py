"""QQ 群名清洗（线上实测：群名里常带 QQ 特殊标记被错误解码的乱码片段）。

实测样例：`<$ÿĀD\x0e>FUCK超级地球（尼尼孩孩Major冠军⚠️）<$ÿĀD\x0e>`，
要洗成 `FUCK超级地球（尼尼孩孩Major冠军⚠️）`。

在群名「进视图 / 进库」的地方统一过 clean_group_name：
- profile._refresh_group_info 写库那一下（groups.name 从此就是干净的）；
- console 的 GroupSummary / GroupView / settings.groups（库里可能还有旧脏行，
  视图层兜底再过一遍，老库也不用迁移）。

规则：
1. 去掉 Unicode 控制字符（类别 Cc / Cf——换行、制表也算控制，一并去掉，
   但 emoji 和 ⚠️ 这类符号是 So/Sk，不受影响）；
2. 去掉 `<$…>` 这种片段：整段（含尖括号）长度 ≤8 且内部含控制字符或
   ÿ(U+00FF) / Ā(U+0100) 这类「一眼乱码」的，当 QQ 特殊标记错误解码处理，整段去掉。
   不含脏字符的 <$…>（比如 <$ABC>）、超过 8 个字符的，一律留下（别误伤正常群名）；
3. 合并所有连续空白成一个空格、strip；
4. 洗完空了 → 「群 <群号>」；没给群号 → 「群」。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# <= 8 字符（含 <>）的 <$...> 片段候选
_DOLLAR_RE = re.compile(r"<\$[^<>]{0,6}>")
# 错误解码出来的「一眼乱码」（ÿ = U+00FF、Ā = U+0100 这两族拉丁扩展，
# 正常 QQ 群名几乎不可能用到这里；中文、emoji、正常符号都不在里头）
_GARBAGE_RE = re.compile(r"[ÿĀ]")


def _is_control(ch: str) -> bool:
    """Unicode 控制字符（Cc 或 Cf）。"""
    return unicodedata.category(ch) in ("Cc", "Cf")


def clean_group_name(raw: Any, group_id: str = "") -> str:
    """清洗 QQ 群名；空了回「群 <群号>」（群号也没有就「群」）。"""
    s = str(raw or "")
    fallback = f"群 {group_id}" if group_id else "群"
    if not s.strip():
        return fallback
    # 1) 先去掉 QQ 特殊标记样式的 <$…> 片段（要逐个看内容再决定）
    def _drop_dollar(m: "re.Match[str]") -> str:
        seg = m.group(0)
        inner = seg[2:-1]
        if any(_is_control(c) for c in inner) or _GARBAGE_RE.search(inner):
            return ""
        return seg

    s = _DOLLAR_RE.sub(_drop_dollar, s)
    # 2) 去掉所有 Unicode 控制字符（含残留的控制符）
    s = "".join(ch for ch in s if not _is_control(ch))
    # 3) 合并空白、strip
    s = re.sub(r"\s+", " ", s).strip()
    return s or fallback
