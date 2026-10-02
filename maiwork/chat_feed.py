"""资讯反哺 MaiBot 闲聊（2026-10-01 与用户定）：给 delivery.TopicMatcher 用的纯代码零件。

- 关键词修准：纯数字 / 日期、两个字母的英文、英文常用词不算；英文按整词对；
  「具体词」= ≥3 个汉字、≥4 个字母、或字母数字混合的型号（m7、rtx5090）。
- 聊天文本清洗：网址、「[事件-…]」系统提示不参与匹配。
- 「最近有啥新鲜事」这类问话识别。
- 记账 chat_feeds：递给 MaiBot 的每一次（同群同一条 30 分钟内算一次、轮数累加），
  之后 MaiBot 自己的**新**发言里出现了这条的、群友没说过的关键词或链接 → 记「聊到了」，
  资讯状态从 new / pool / expired 改成 mentioned（网页显示「MaiBot 在聊天里提过」）。
全程不调模型、不做网络；在 planner 钩子（1 秒超时）里跑，只做小查询。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable, Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    key TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'topic',
    title TEXT NOT NULL DEFAULT '',
    hit TEXT NOT NULL DEFAULT '[]',
    words TEXT NOT NULL DEFAULT '[]',
    link TEXT NOT NULL DEFAULT '',
    rounds INTEGER NOT NULL DEFAULT 1,
    first_ts REAL NOT NULL DEFAULT 0,
    last_ts REAL NOT NULL DEFAULT 0,
    said_ts REAL,
    said_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chat_feeds_group ON chat_feeds(group_id, last_ts);
CREATE INDEX IF NOT EXISTS idx_chat_feeds_key ON chat_feeds(group_id, key);
"""

EPISODE_S = 30 * 60      # 同一条 30 分钟内的多轮算一次；之后 MaiBot 的新发言在这段时间里判「聊到了」
KEEP_S = 30 * 86400      # 账只留 30 天
SAID_TEXT_MAX = 120

_STOP_ASCII = frozenset(
    """the and for with you are was not but all new now how why what this that from have has its can get got
    one two out use via pro max mini plus well yes lol app web day man let see too off way who did our may
    any her his him she they them then than just like more most some such only also into over very will
    your about after again""".split()
)
_DIGITS_PUNCT = re.compile(r"^[\d\W_]+$")
_ASCII = re.compile(r"^[\x00-\x7f]+$")
_HAS_ALPHA = re.compile(r"[a-z]")
_HAS_DIGIT = re.compile(r"\d")
_CJK = re.compile(r"[\u3400-\u9fff]")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EVENT = re.compile(r"\[事件-[^\]]*\][^\n]*")
# 表情包 / 图片的自动描述不算群友在聊什么（线上 2026-10-02 回放：图片描述带来的命中
# 大多是误撞，「探索」+「switch」撞上不相干的游戏，甚至撞上群友转发的 MaiWork 自己的资讯卡截图）
_EMOJI = re.compile(r"\[(?:表情包|图片)[:：][^\]]*(?:\]|$)")  # 截断的长描述没有右括号，抹到行尾

# 问「最近有什么新鲜事」：时间词 + 新闻词，或 有什么/有啥/来点 + 新闻词
_ASK_NOUN = r"(新闻|新鲜事|资讯|大事|热点|瓜|好玩的事)"
_ASK = (
    re.compile(r"(最近|今天|今日|这两天|这几天|近期|这周)[^。！？\n]{0,8}" + _ASK_NOUN),
    re.compile(r"(有什么|有啥|有没有|来点|来些)[^。！？\n]{0,4}" + _ASK_NOUN),
)


def usable(kw: str) -> bool:
    s = str(kw or "").strip().lower()
    if len(s) < 2 or _DIGITS_PUNCT.match(s):
        return False
    if _ASCII.match(s):
        if _HAS_ALPHA.search(s) and _HAS_DIGIT.search(s):
            return True  # 型号：m7、rtx5090、gpt-6
        return len(s) >= 3 and s not in _STOP_ASCII
    return True


def specific(kw: str) -> bool:
    s = str(kw or "").strip().lower()
    if not usable(s):
        return False
    if _ASCII.match(s):
        return len(s) >= 4 or bool(_HAS_ALPHA.search(s) and _HAS_DIGIT.search(s))
    return len(_CJK.findall(s)) >= 3 or bool(re.search(r"[a-z0-9]", s))  # 中英混合（小米18 pro）也算


def keywords(raw: Any) -> list[str]:
    """JSON 关键词列表 → 小写、去重、只留能用的。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for kw in data:
        s = str(kw or "").strip().lower()
        if s and s not in out and usable(s):
            out.append(s)
    return out


def clean(text: str) -> str:
    return _URL.sub(" ", _EMOJI.sub(" ", _EVENT.sub(" ", str(text or "")))).lower()


def contains(kw: str, text_lower: str) -> bool:
    if _ASCII.match(kw):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text_lower) is not None
    return kw in text_lower


def is_ask(text: str) -> bool:
    s = str(text or "")
    return any(p.search(s) for p in _ASK)


# ----------------------------------------------------------------------
# 记账
# ----------------------------------------------------------------------


def record(conn: sqlite3.Connection, gid: str, entries: Iterable[dict], now: float) -> None:
    """entries: [{key, mode, title, hit, words, link}]；30 分钟内同一条且没聊到 → 轮数 +1。"""
    for e in entries:
        key = str(e.get("key") or "")
        if not key:
            continue
        row = conn.execute(
            "SELECT id FROM chat_feeds WHERE group_id=? AND key=? AND said_ts IS NULL AND last_ts>?"
            " ORDER BY id DESC LIMIT 1",
            (gid, key, now - EPISODE_S),
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE chat_feeds SET rounds=rounds+1, last_ts=? WHERE id=?", (now, int(row["id"])))
            continue
        conn.execute(
            "INSERT INTO chat_feeds (group_id, key, mode, title, hit, words, link, rounds, first_ts, last_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                gid, key, str(e.get("mode") or "topic"), str(e.get("title") or "")[:120],
                json.dumps(list(e.get("hit") or []), ensure_ascii=False),
                json.dumps(list(e.get("words") or []), ensure_ascii=False),
                str(e.get("link") or ""), now, now,
            ),
        )
    conn.execute("DELETE FROM chat_feeds WHERE group_id=? AND last_ts<?", (gid, now - KEEP_S))


def _said(row: Any, text_lower: str) -> bool:
    link = str(row["link"] or "").lower()
    if link and link in text_lower:
        return True
    try:
        hit = set(json.loads(row["hit"] or "[]"))
        words = json.loads(row["words"] or "[]")
    except (TypeError, ValueError):
        return False
    # 群友已经说过的词不算（MaiBot 跟着群友说不代表用了这条）；要出现这条里别的具体词
    return any(w not in hit and specific(w) and contains(w, text_lower) for w in words)


def check_said(conn: sqlite3.Connection, gid: str, bot_texts: list[str], now: float) -> int:
    """MaiBot 的新发言 → 把 30 分钟内递过、还没聊到的对上；返回标了几条。"""
    if not bot_texts:
        return 0
    rows = conn.execute(
        "SELECT id, key, hit, words, link FROM chat_feeds WHERE group_id=? AND said_ts IS NULL AND last_ts>?",
        (gid, now - EPISODE_S),
    ).fetchall()
    n = 0
    for r in rows:
        for t in bot_texts:
            if _said(r, clean(t)):
                conn.execute(
                    "UPDATE chat_feeds SET said_ts=?, said_text=? WHERE id=?",
                    (now, str(t)[:SAID_TEXT_MAX], int(r["id"])),
                )
                key = str(r["key"])
                if key.startswith("news:"):
                    conn.execute(
                        "UPDATE news_items SET status_kind='mentioned', status_at=?"
                        " WHERE id=? AND status_kind IN ('new', 'pool', 'expired')",
                        (now, int(key.split(":", 1)[1])),
                    )
                n += 1
                break
    return n


def item_stats(conn: sqlite3.Connection, gid: str, key: str) -> dict:
    """网页用：递给 MaiBot 几次（按「次」，一次里的多轮不重复算）、最早哪次聊到了。"""
    try:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(said_ts) AS s FROM chat_feeds WHERE group_id=? AND key=?",
            (str(gid), str(key)),
        ).fetchone()
    except sqlite3.Error:
        return {"times": 0, "said_ts": None}
    return {"times": int(r["n"] or 0), "said_ts": (float(r["s"]) if r["s"] is not None else None)}
