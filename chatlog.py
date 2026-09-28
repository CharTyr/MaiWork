"""chatlog.py（docs/02-设计.md §4.1「有人味」，2026-09-27 与用户定）：群里最近 14 天发言的只读副本。

用途两件：
1. 资讯写「我发这条的原因」时，拿 search_chat 查到的群里真实原话（带时间和名字）做依据，
   「什么时候聊过、谁说的」都能点回去；
2. MaiBot 在群里聊到相关话题时，用关键词匹配把那条资讯接上（§6.3，另一任务做）。

表：chat_log（FTS5，tokenize='trigram'，中文可用；列 text / group_id / message_id / ts /
user_id / user_name，由 store._m_humane 建好）。只读本群的。

- record_messages(store, group_id, msgs, *, now)：profile.tick 统计之后把本轮新消息塞进来；
  只收非机器人、文本非空、不是「[图片]」占位的；同一 message_id 重复插入不动原文。
- prune_old(store, group_id, *, now, keep_days=14)：清 14 天前的；每天（北京时间）最多清一次
  （kv 记上次清的日子），tick 每次都调也便宜。
- search_chat(store, group_id, query, *, days=14, limit=8, now=None)：
  [{ts, who, text, message_id}]，新的在前；trigram 要 ≥3 个字符，短词自动换 LIKE 兜底；
  只查本群；空查询 / 没命中 → []。
- recent_chat(store, group_id, *, hours=48, limit=60, now=None)：
  本群最近 hours 小时内的发言，[{ts, who, text, message_id}]，按时间**从旧到新**
  （取最新的 limit 条再正序），单条 text 截 _RESULT_TEXT_MAX；只查本群；
  库错误按「没有」处理 → []。给 _plan_focus / _collect / make_idea 看「群里真实在聊什么」用。
"""

from __future__ import annotations

import logging
from typing import Any

from . import clock

logger = logging.getLogger("maiwork.chatlog")

_KEEP_DAYS = 14
_TRIGRAM_MIN = 3           # trigram 索引要求查询词至少几个字符
_TEXT_MAX = 500            # 单条入库最多留多少字（防刷屏长文撑爆）
_RESULT_TEXT_MAX = 200     # 查询返回的单条摘要上限
_PICTURE_PLACEHOLDER = "[图片]"
_RECENT_SCAN_MAX = 200       # recent_chat 单次最多取多少条（limit 的上限）


def record_messages(store: Any, group_id: str, msgs: list, *, now: float) -> int:
    """把一批新消息写进 chat_log；返回实际写入条数。库里出错记日志、不抛。"""
    gid = str(group_id)
    rows: list[tuple] = []
    for m in msgs or []:
        try:
            if getattr(m, "is_bot", False):
                continue
            text = str(getattr(m, "text", "") or "").strip()
            if not text or text == _PICTURE_PLACEHOLDER:
                continue
            mid = str(getattr(m, "id", "") or "").strip()
            if not mid:
                continue
            rows.append(
                (
                    text[:_TEXT_MAX],
                    gid,
                    mid,
                    float(getattr(m, "ts", 0.0) or 0.0),
                    str(getattr(m, "user_id", "") or ""),
                    str(getattr(m, "user_name", "") or ""),
                )
            )
        except Exception:
            logger.debug("chat_log 跳过一条坏消息（群 %s）", gid, exc_info=True)
    if not rows:
        return 0
    try:
        with store.tx() as conn:
            # FTS5 表没有唯一约束，INSERT OR IGNORE 拦不住重复；同一 message_id
            # 已存在就跳过（不改原文）。
            conn.executemany(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " SELECT ?, ?, ?, ?, ?, ?"
                " WHERE NOT EXISTS ("
                " SELECT 1 FROM chat_log WHERE group_id=? AND message_id=?)",
                [(*r, r[1], r[2]) for r in rows],
            )
    except Exception:
        logger.info("chat_log 写入失败（群 %s）", gid, exc_info=True)
        return 0
    return len(rows)


def prune_old(store: Any, group_id: str, *, now: float, keep_days: int = _KEEP_DAYS) -> int:
    """清 keep_days 天前的；每天（北京时间）最多清一次，返回删掉条数（没清返回 0）。"""
    gid = str(group_id)
    day = clock.day_key(float(now))
    kv_key = f"chatlog.prune_day.{gid}"
    try:
        if str(store.kv_get(kv_key, "") or "") == day:
            return 0
        cutoff = float(now) - max(1, int(keep_days)) * 86400.0
        with store.tx() as conn:
            cur = conn.execute("DELETE FROM chat_log WHERE group_id=? AND ts<?", (gid, cutoff))
            store.kv_set(conn, kv_key, day)
        return int(cur.rowcount or 0)
    except Exception:
        logger.info("chat_log 清理失败（群 %s）", gid, exc_info=True)
        return 0


def _fts_escape(query: str) -> str:
    """FTS5 查询串转义：只把它当一段普通文本（phrase）匹配，不让特殊字符炸查询。"""
    return '"' + query.replace('"', ' ') + '"'


def recent_chat(
    store: Any,
    group_id: str,
    *,
    hours: float = 48,
    limit: int = 60,
    now: float | None = None,
) -> list[dict]:
    """本群最近 hours 小时内的发言。返回 [{ts, who, text, message_id}]，按时间从旧到新。

    最多 limit 条：先取最新的 limit 条，再翻成从旧到新（给提示词看「最近在聊什么」用）。
    单条 text 截 _RESULT_TEXT_MAX。只查本群；任何库错误按「没有」处理（返回 []）。
    """
    gid = str(group_id)
    if now is None:
        now = clock.now()
    since = float(now) - max(1.0, float(hours)) * 3600.0
    limit_i = max(1, min(_RECENT_SCAN_MAX, int(limit)))
    try:
        rows = store.read().execute(
            "SELECT text, ts, user_name, message_id FROM chat_log"
            " WHERE group_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (gid, since, limit_i),
        ).fetchall()
    except Exception:
        logger.info("chat_log 最近发言查询失败（群 %s）", gid, exc_info=True)
        return []
    out: list[dict] = []
    for r in reversed(rows):  # 取的是最新 limit 条（倒序），翻成正序
        out.append(
            {
                "ts": float(r["ts"] or 0.0),
                "who": str(r["user_name"] or ""),
                "text": str(r["text"] or "")[:_RESULT_TEXT_MAX],
                "message_id": str(r["message_id"] or ""),
            }
        )
    return out


def search_chat(
    store: Any,
    group_id: str,
    query: str,
    *,
    days: int = _KEEP_DAYS,
    limit: int = 8,
    now: float | None = None,
    uid: str | None = None,
) -> list[dict]:
    """在本群 chat_log 里查相关原话。返回 [{ts, who, text, message_id}]，新的在前。

    查询词 ≥3 个字符走 FTS5（trigram）；更短的词 FTS 匹配不了，走 LIKE 兜底。
    任何库错误按「查不到」处理（返回 []），写帖子那边少了依据照样能写。
    给了 uid 时只取该 user_id 的发言（个人向资讯用：引用的原话只来自他本人）。
    """
    gid = str(group_id)
    q = str(query or "").strip()
    if not q:
        return []
    if now is None:
        now = clock.now()
    since = float(now) - max(1, int(days)) * 86400.0
    limit_i = max(1, min(50, int(limit)))
    uid = str(uid).strip() if uid else None
    # 限定某人时多取一些再过滤留 limit 条该人的（避免 LIKE/FTS 返回里该人排在后面被截掉）
    scan_i = limit_i * 5 if uid else limit_i
    try:
        if len(q) >= _TRIGRAM_MIN:
            if uid:
                sql = (
                    "SELECT text, ts, user_name, message_id FROM chat_log"
                    " WHERE chat_log MATCH ? AND group_id=? AND ts>=? AND user_id=?"
                    " ORDER BY ts DESC LIMIT ?"
                )
                rows = store.read().execute(sql, (_fts_escape(q), gid, since, uid, scan_i)).fetchall()
            else:
                sql = (
                    "SELECT text, ts, user_name, message_id FROM chat_log"
                    " WHERE chat_log MATCH ? AND group_id=? AND ts>=?"
                    " ORDER BY ts DESC LIMIT ?"
                )
                rows = store.read().execute(sql, (_fts_escape(q), gid, since, scan_i)).fetchall()
        else:
            pat = "%" + _like_escape(q) + "%"
            if uid:
                sql = (
                    "SELECT text, ts, user_name, message_id FROM chat_log"
                    " WHERE group_id=? AND ts>=? AND user_id=? AND text LIKE ? ESCAPE '\\'"
                    " ORDER BY ts DESC LIMIT ?"
                )
                rows = store.read().execute(sql, (gid, since, uid, pat, scan_i)).fetchall()
            else:
                sql = (
                    "SELECT text, ts, user_name, message_id FROM chat_log"
                    " WHERE group_id=? AND ts>=? AND text LIKE ? ESCAPE '\\'"
                    " ORDER BY ts DESC LIMIT ?"
                )
                rows = store.read().execute(sql, (gid, since, pat, scan_i)).fetchall()
    except Exception:
        logger.info("chat_log 查询失败（群 %s 词 %r）", gid, q[:30], exc_info=True)
        return []
    rows = rows[:limit_i]
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "ts": float(r["ts"] or 0.0),
                "who": str(r["user_name"] or ""),
                "text": str(r["text"] or "")[:_RESULT_TEXT_MAX],
                "message_id": str(r["message_id"] or ""),
            }
        )
    return out


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
