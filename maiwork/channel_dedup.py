"""channel_dedup.py（v0.14 §1.4）：三条主动通道的「同一条素材」12 小时互斥。

三条主动通道：资讯卡片（news_card）、构想提一嘴（idea_mention）、冷场开场白（topic）。
同一条素材（同一条资讯 / 同一条构想）在 12 小时内、以及排队在途期间，只准在一个
通道发出去——线上同一件事被开场白和卡片连着发（间隔 30 秒），这里把它挡掉。

- 素材的「同一条」身份：候选 / 卡片 / 提一嘴都靠 `kind + ref_id` 认（候选表的
  kind='news'/'idea'、ref_id=news_items.id / ideas.id；卡片 news_cards.item_ids /
  outbox 载荷 candidate_ref_id；提一嘴 idea_mentions.idea_id）。
- 本文件只放纯代码查询（读库、不读群、不调模型），给两个方向共用：
    * 方向 A（topics.py）：开场白选材 / 发送前复核时，看这条素材是不是已经被
      卡片 / 提一嘴发过（sent/uncertain 且 ≤12h）或正在发（pending/queued/sending/
      outbox 在途）。
    * 方向 B（card_push.py）：卡片 / 提一嘴选材 / 发送前复核时，看这条素材是不是
      已经被开场白发过（topic_log 开过场且 ≤12h）或正在发（outbox topic:* 在途）。

所有函数失败关闭取向不同：选材时「读不到就当没冲突」（不因为一句 SQL 坏了就不发），
发送前复核（on_before_send）里「读不到就当有冲突」（宁可作废一条，不放重复内容进群）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Set

logger = logging.getLogger("maiwork.channel_dedup")

# 互斥窗口（秒）：12 小时
CROSS_CHANNEL_DEDUP_S = 12 * 3600.0


# ---------------------------------------------------------------------------
# 方向 A：这条（kind, ref_id）素材是不是已经被「卡片 / 提一嘴」占了？
# ---------------------------------------------------------------------------

def card_used_item_ids(store: Any, gid: str, now: float) -> Set[int]:
    """已经被卡片占用的 news_items.id 集合（在途 + 12h 内已发）。读失败 → 空集。"""
    out: Set[int] = set()
    try:
        # news_cards：在途（pending/queued/sending）+ 12h 内已发（sent/uncertain）
        rows = store.read().execute(
            "SELECT item_ids FROM news_cards WHERE group_id=?"
            " AND (status IN ('pending','queued','sending')"
            "      OR (status IN ('sent','uncertain') AND sent_ts>=?))",
            (str(gid), float(now) - CROSS_CHANNEL_DEDUP_S),
        ).fetchall()
        for r in rows:
            try:
                out.update(int(x) for x in json.loads(r["item_ids"] or "[]"))
            except (TypeError, ValueError):
                continue
        # outbox：还没写 news_cards 的待发卡片（载荷里带 item_ids）
        rows = store.read().execute(
            "SELECT payload FROM outbox WHERE group_id=? AND key LIKE 'news_card:%'"
            " AND status IN ('pending','sending')",
            (str(gid),),
        ).fetchall()
        for r in rows:
            try:
                data = json.loads(r["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            for x in (data.get("item_ids") or []):
                try:
                    out.add(int(x))
                except (TypeError, ValueError):
                    continue
    except Exception:
        logger.debug("读卡片占用素材失败（群 %s），按没冲突处理", gid, exc_info=True)
    return out


def mention_used_idea_ids(store: Any, gid: str, now: float) -> Set[int]:
    """已经被构想提一嘴占用的 ideas.id 集合（在途 + 12h 内已发）。读失败 → 空集。"""
    out: Set[int] = set()
    try:
        rows = store.read().execute(
            "SELECT idea_id FROM idea_mentions WHERE group_id=?"
            " AND (status IN ('pending','queued','sending')"
            "      OR (status IN ('sent','uncertain') AND sent_ts>=?))",
            (str(gid), float(now) - CROSS_CHANNEL_DEDUP_S),
        ).fetchall()
        for r in rows:
            try:
                out.add(int(r["idea_id"]))
            except (TypeError, ValueError):
                continue
    except Exception:
        logger.debug("读提一嘴占用素材失败（群 %s），按没冲突处理", gid, exc_info=True)
    return out


def other_channel_used(store: Any, gid: str, now: float, kind: str, ref_id: int) -> bool:
    """方向 A 判定：这条候选素材（kind, ref_id）是不是已经被卡片 / 提一嘴占了。"""
    try:
        ref_id = int(ref_id)
    except (TypeError, ValueError):
        return False
    if str(kind) == "news":
        return ref_id in card_used_item_ids(store, gid, now)
    if str(kind) == "idea":
        return ref_id in mention_used_idea_ids(store, gid, now)
    return False


# ---------------------------------------------------------------------------
# 方向 B：这条（kind, ref_id）素材是不是已经被「冷场开场白」占了？
# ---------------------------------------------------------------------------

def topic_used_news_ids(store: Any, gid: str, now: float) -> Set[int]:
    """已经被开场白占用的 news_items.id（12h 内开过场 + 在途）。读失败 → 空集。"""
    return _topic_used_ids(store, gid, now, "news")


def topic_used_idea_ids(store: Any, gid: str, now: float) -> Set[int]:
    """已经被开场白占用的 ideas.id（12h 内开过场 + 在途）。读失败 → 空集。"""
    return _topic_used_ids(store, gid, now, "idea")


def _topic_used_ids(store: Any, gid: str, now: float, kind: str) -> Set[int]:
    out: Set[int] = set()
    try:
        # topic_log 开过场的（opener 非空 = 真发出去了）且 ≤12h：候选 → ref_id
        rows = store.read().execute(
            "SELECT c.ref_id FROM topic_log t"
            " JOIN topic_candidates c ON c.id=t.candidate_id"
            " WHERE t.group_id=? AND t.opener!='' AND c.kind=? AND COALESCE(t.followup_due_ts - 600.0, t.ts)>=?",
            (str(gid), str(kind), float(now) - CROSS_CHANNEL_DEDUP_S),
        ).fetchall()
        for r in rows:
            try:
                out.add(int(r["ref_id"]))
            except (TypeError, ValueError):
                continue
        # outbox 里还在排队的开场白：载荷里带它要用的候选 kind/ref_id
        rows = store.read().execute(
            "SELECT payload FROM outbox WHERE group_id=? AND key LIKE 'topic:%'"
            " AND status IN ('pending','sending')",
            (str(gid),),
        ).fetchall()
        for r in rows:
            try:
                data = json.loads(r["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if str(data.get("candidate_kind") or "") == str(kind):
                try:
                    out.add(int(data.get("candidate_ref_id")))
                except (TypeError, ValueError):
                    continue
    except Exception:
        logger.debug("读开场白占用素材失败（群 %s），按没冲突处理", gid, exc_info=True)
    return out


def topic_used(store: Any, gid: str, now: float, kind: str, ref_id: int) -> bool:
    """方向 B 判定：这条素材（kind, ref_id）是不是已经被开场白占了。"""
    try:
        ref_id = int(ref_id)
    except (TypeError, ValueError):
        return False
    if str(kind) == "news":
        return ref_id in topic_used_news_ids(store, gid, now)
    if str(kind) == "idea":
        return ref_id in topic_used_idea_ids(store, gid, now)
    return False
