"""privacy.py（G7）：关注成员的注记 / 个人画像摘要不进群友可见的文字。

规则（只看本群、removed=0 的关注成员）：
- note 或 persona 的 summary 里任何长度≥8 的连续片段出现在 text 里 → 拒（返回 None）
  （note 和 persona.summary 同步维护，本质上是一份文本的两处存法；都是只给管理员看的
  私下画像，绝不能进群友可见文字）；
- 2026-09-27 调整（用户同意「能在资讯里点名群友」）：光是出现关注成员的**名字**不再拒——
  资讯、开场白、交付说明里可以点名说「这条可能对阿帆有用」，但点名的依据只能是
  他在群里公开说过的话（写帖子流程会带群里的原话，见 feeds）；
- 片段 <8 字的不参与（误伤太大）；
- 群之间互不影响。

用法：模型要出库 / 出库前发送的文字都过一遍；None = 整条作废或换兜底话
（各调用方定：条目丢弃、开场白不发、交付说明换「做好了：<任务标题>」）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("maiwork.privacy")

_NOTE_FRAGMENT = 8


def _focus_rows(store: Any, group_id: str) -> list[dict]:
    try:
        rows = store.read().execute(
            "SELECT name, note, persona FROM focus_members WHERE group_id=? AND removed=0",
            (str(group_id),),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            # persona（JSON 或裸文本；老部署可能是 profile.py 写的 pure 文本）取 summary
            summary = ""
            raw_persona = str(r["persona"] or "").strip()
            if raw_persona:
                try:
                    parsed = json.loads(raw_persona)
                    if isinstance(parsed, dict):
                        summary = str(parsed.get("summary") or "").strip()
                except (ValueError, TypeError):
                    summary = raw_persona
            out.append(
                {
                    "name": str(r["name"] or ""),
                    "note": str(r["note"] or ""),
                    "persona_summary": summary,
                }
            )
        return out
    except Exception:
        logger.debug("读关注成员失败（群 %s），按没有关注成员处理", group_id, exc_info=True)
        return []


def _contains_note_fragment(text: str, note: str) -> bool:
    """note 的任何长度≥8 连续片段出现在 text 里 → True。"""
    n = len(note)
    if n < _NOTE_FRAGMENT:
        return False
    for i in range(0, n - _NOTE_FRAGMENT + 1):
        if note[i : i + _NOTE_FRAGMENT] in text:
            return True
    return False


def scrub(group_id: str, text: str, store: Any) -> Optional[str]:
    """text 通过隐私闸：干净原样返回；命中关注成员注记/画像摘要片段 → None（拒）。

    store 异常按「没有关注成员」处理（放行）——读不了库不能让发消息全停，
    这种时候记日志。
    """
    s = str(text or "")
    if not s:
        return s
    for row in _focus_rows(store, group_id):
        if _contains_note_fragment(s, row["note"]):
            logger.info("隐私闸：含关注成员注记片段，已拦下（群 %s）", group_id)
            return None
        if _contains_note_fragment(s, row["persona_summary"]):
            logger.info("隐私闸：含关注成员画像摘要片段，已拦下（群 %s）", group_id)
            return None
    return s
