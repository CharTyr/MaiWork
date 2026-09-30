"""自动收的资讯反馈（2026-09-30，docs/10-资讯流水线改进计划.md 第七节第 4 步）。

线上实测：上过网页的 86 条资讯只有 2 个「有用」、8 个「没用」、0 条评价、0 条被群里接话——
显式反馈几乎为零。手册的办法：先自动收「自然发生的反应」，沉默不算差评。

表 news_feedback（ensure_schema 建，store 的迁移也调它）：一条 = 一次反应
{group_id, item_id, kind, weight, ts, actor, message_id, detail}，(kind, item_id, actor, message_id) 唯一 → 幂等。

- reply：群友回复 / 引用了 MaiWork 发的资讯卡片（news_cards.message_id），卡片上每条各记
  WEIGHTS["reply"] / 条数。CardIndex 缓存「群 → {卡片消息编号: 条目}」，60 秒最多查一次库；
  钩子任何错误都吞掉（收消息的钩子永远不能中止消息）。
- click：网页上点开原文（/go/<条目>），只认本群、上过网页的条目，跳到库里存的原链接（不接受外来链接）；
  同一浏览器同一条一天只算一次；每个浏览器每分钟超过 30 次的不记。
- mention：群里接着聊。每轮看本群 24 小时内上网页的条目：发出后 6 小时内的群聊里，命中 ≥2 个关键词或
  1 个 ≥4 字的关键词的发言算候选；候选交给 judge（Jev / 主模型，调用方给）确认，只记「是」的；
  判过的条目记在 kv 里 7 天不再判；每轮每群最多 5 条、30 句。
- actor 一律是「群号:账号」的 sha256 前 16 位（浏览器同理），不存原始账号。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

from . import clock

logger = logging.getLogger("maiwork.news_feedback")

WEIGHTS = {"reply": 3.0, "click": 2.0, "mention": 2.0}
LABELS = {"reply": "有人回复卡片", "click": "网页上有人点开", "mention": "群里接着聊了"}
_CARD_CACHE_S = 60.0
_CARD_DAYS = 7
_CLICK_PER_MIN = 30
_MENTION_ITEM_HOURS = 24
_MENTION_WINDOW_H = 6
_MENTION_MAX_ITEMS = 5
_MENTION_MAX_MSGS = 30
_MENTION_JUDGED_DAYS = 7
_MENTION_SPEAKERS_MAX = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    ts REAL NOT NULL DEFAULT 0,
    actor TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE(kind, item_id, actor, message_id)
);
CREATE INDEX IF NOT EXISTS idx_news_feedback_group ON news_feedback(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_news_feedback_item ON news_feedback(item_id);
"""


def ensure_schema(conn: Any) -> None:
    """建表（幂等）。逐条 execute：executescript 会自己提交，在 store.tx() 里用会把事务搅乱。"""
    for stmt in SCHEMA_SQL.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _ensure(store: Any) -> None:
    """第一次用到某个库时建表（不依赖 store.py 的迁移清单，老库也能直接用）。"""
    if getattr(store, "_news_feedback_ready", False):
        return
    with store.tx() as conn:
        ensure_schema(conn)
    try:
        store._news_feedback_ready = True
    except Exception:
        pass


def actor_hash(group_id: Any, raw: Any) -> str:
    raw_s = str(raw or "").strip()
    if not raw_s:
        return ""
    return hashlib.sha256(f"{group_id}:{raw_s}".encode("utf-8")).hexdigest()[:16]


def record(
    store: Any, group_id: Any, item_id: int, kind: str, *, actor: str = "", message_id: str = "",
    weight: float | None = None, detail: str = "", now: float | None = None,
) -> bool:
    """记一次反应；同一次（kind+条目+人+消息）重复记 → False。"""
    w = float(WEIGHTS.get(kind, 0.0) if weight is None else weight)
    _ensure(store)
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_feedback (group_id, item_id, kind, weight, ts, actor, message_id, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(group_id), int(item_id), str(kind), w, float(now if now is not None else clock.now()),
             str(actor or ""), str(message_id or ""), str(detail or "")[:200]),
        )
        return bool(cur.rowcount)


# ----------------------------------------------------------------------
# 回复 / 引用卡片
# ----------------------------------------------------------------------


class CardIndex:
    """群 → {卡片消息编号: [条目]}，60 秒最多刷一次（按群）。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict[str, list[int]]]] = {}

    def _cards(self, store: Any, gid: str, now: float) -> dict[str, list[int]]:
        hit = self._cache.get(gid)
        if hit is not None and now - hit[0] < _CARD_CACHE_S:
            return hit[1]
        out: dict[str, list[int]] = {}
        rows = store.read().execute(
            "SELECT message_id, item_ids FROM news_cards WHERE group_id=? AND status IN ('sent','uncertain')"
            " AND message_id!='' AND COALESCE(sent_ts, created)>=?",
            (gid, now - _CARD_DAYS * 86400.0),
        ).fetchall()
        for r in rows:
            try:
                ids = [int(x) for x in json.loads(r["item_ids"] or "[]")]
            except (ValueError, TypeError):
                ids = []
            if ids:
                out[str(r["message_id"])] = ids
        self._cache[gid] = (now, out)
        return out

    def on_message(self, store: Any, group_id: Any, user_id: Any, message_id: Any, reply_to: Any, now: float) -> int:
        """收到一条群消息：回复的是资讯卡片 → 给卡片上每条记 reply；返回记了几条。永不抛。"""
        try:
            rid = str(reply_to or "").strip()
            if not rid:
                return 0
            gid = str(group_id)
            ids = self._cards(store, gid, float(now)).get(rid)
            if not ids:
                return 0
            w = WEIGHTS["reply"] / len(ids)
            actor = actor_hash(gid, user_id)
            n = 0
            for iid in ids:
                if record(store, gid, iid, "reply", actor=actor, message_id=str(message_id or ""), weight=w, now=now):
                    n += 1
            return n
        except Exception:
            logger.debug("记卡片回复反馈失败（群 %s）", group_id, exc_info=True)
            return 0


# ----------------------------------------------------------------------
# 点开原文
# ----------------------------------------------------------------------

_click_log: dict[str, list[float]] = {}


def _item_url(row: Any) -> str:
    try:
        src = json.loads(row["sources"] or "[]")
        if isinstance(src, list) and src and isinstance(src[0], dict):
            url = str(src[0].get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    except (ValueError, TypeError):
        pass
    key = str(row["url_key"] or "").strip()
    return f"https://{key}" if key else ""


def click(store: Any, group_id: Any, item_id: int, *, client: Any = "", now: float | None = None) -> str | None:
    """网页上点开原文：本群、上过网页的条目 → 返回库里存的原链接（并记一次 click）；否则 None。"""
    gid = str(group_id)
    t = float(now if now is not None else clock.now())
    try:
        row = store.read().execute(
            "SELECT id, group_id, sources, url_key, rejected FROM news_items WHERE id=?", (int(item_id),)
        ).fetchone()
    except Exception:
        return None
    if row is None or str(row["group_id"]) != gid or int(row["rejected"] or 0):
        return None
    url = _item_url(row)
    if not url:
        return None
    actor = actor_hash(gid, client)
    recent = [x for x in _click_log.get(actor or "-", []) if t - x < 60.0]
    recent.append(t)
    _click_log[actor or "-"] = recent[-(_CLICK_PER_MIN + 1):]
    if len(recent) <= _CLICK_PER_MIN:
        try:
            record(store, gid, int(item_id), "click", actor=actor, message_id=clock.bj(t).strftime("%Y-%m-%d"), now=t)
        except Exception:
            logger.debug("记点击反馈失败", exc_info=True)
    return url


# ----------------------------------------------------------------------
# 群里接着聊
# ----------------------------------------------------------------------


def _judged_key(gid: str) -> str:
    return f"feeds.mention_judged.{gid}"


def _hits(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    hit = [k for k in keywords if k and k.lower() in low]
    return len(hit) >= 2 or any(len(k) >= 4 for k in hit)


async def mention_round(
    store: Any, group_id: Any, now: float, *, judge: Callable[[list[dict]], Awaitable[set[int]]],
) -> int:
    """跑一轮「群里接着聊」：返回这轮确认被接着聊的条目数。judge(cands) → 判「是」的 item_id 集合。"""
    gid = str(group_id)
    try:
        judged: dict = store.kv_get(_judged_key(gid), {}) or {}
    except Exception:
        judged = {}
    judged = {k: v for k, v in judged.items() if now - float(v or 0) < _MENTION_JUDGED_DAYS * 86400.0}
    rows = store.read().execute(
        "SELECT id, title, keywords, created FROM news_items WHERE group_id=? AND rejected=0 AND created>=?"
        " ORDER BY created DESC",
        (gid, now - _MENTION_ITEM_HOURS * 3600.0),
    ).fetchall()
    cands: list[dict] = []
    total_msgs = 0
    for r in rows:
        if str(r["id"]) in judged or len(cands) >= _MENTION_MAX_ITEMS:
            continue
        try:
            kws = [str(k).strip() for k in json.loads(r["keywords"] or "[]") if str(k).strip()]
        except (ValueError, TypeError):
            kws = []
        if not kws:
            continue
        start = float(r["created"] or 0.0)
        end = start + _MENTION_WINDOW_H * 3600.0
        try:
            msgs = store.read().execute(
                "SELECT text, ts, user_id, message_id FROM chat_log WHERE group_id=? AND ts>=? AND ts<=?"
                " ORDER BY ts LIMIT 400",
                (gid, start, min(end, now)),
            ).fetchall()
        except Exception:
            msgs = []
        hit = [dict(m) for m in msgs if _hits(str(m["text"] or ""), kws)]
        if not hit:
            continue
        hit = hit[: max(0, _MENTION_MAX_MSGS - total_msgs)]
        if not hit:
            break
        total_msgs += len(hit)
        cands.append({"item_id": int(r["id"]), "title": str(r["title"] or ""), "keywords": kws, "messages": hit})
    if not cands:
        return 0
    try:
        yes = set(await judge(cands))
    except Exception:
        logger.info("判断「群里接着聊」失败（群 %s），这轮不记", gid, exc_info=True)
        return 0
    n = 0
    for c in cands:
        judged[str(c["item_id"])] = now
        if c["item_id"] not in yes:
            continue
        speakers: dict[str, str] = {}
        for m in c["messages"]:
            a = actor_hash(gid, m.get("user_id"))
            if a and a not in speakers and len(speakers) < _MENTION_SPEAKERS_MAX:
                speakers[a] = str(m.get("message_id") or "")
        w = WEIGHTS["mention"] / max(1, len(speakers))
        added = False
        for a, mid in speakers.items():
            added = record(store, gid, c["item_id"], "mention", actor=a, message_id=mid, weight=w, now=now) or added
        n += 1 if added else 0
    with store.tx() as conn:
        store.kv_set(conn, _judged_key(gid), judged)
    return n


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------


def summary(store: Any, group_id: Any, now: float, *, days: int = 14) -> dict[str, Any]:
    """{items: {item_id: {reply, click, mention, score}}, totals: {reply, click, mention}}。"""
    try:
        _ensure(store)
    except Exception:
        pass
    gid = str(group_id)
    items: dict[int, dict[str, float]] = {}
    totals = {"reply": 0, "click": 0, "mention": 0}
    try:
        rows = store.read().execute(
            "SELECT item_id, kind, weight FROM news_feedback WHERE group_id=? AND ts>=?",
            (gid, now - days * 86400.0),
        ).fetchall()
    except Exception:
        rows = []
    for r in rows:
        k = str(r["kind"])
        e = items.setdefault(int(r["item_id"]), {"reply": 0, "click": 0, "mention": 0, "score": 0.0})
        if k in e:
            e[k] += 1
            totals[k] += 1
        e["score"] = round(e["score"] + float(r["weight"] or 0.0), 3)
    return {"items": items, "totals": totals}
