"""成员名册：按（群, 平台 id）认人，名字只用来显示、跟着改名更新。

- 平台 id（QQ 平台就是 QQ 号）是唯一身份；群名片 / QQ 昵称只是显示名，随时会改。
- 名册表 ``members(group_id, user_id, name, ts)``：每读到一条消息顺手记发言人当时的显示名
  （名片 > 昵称），按消息时间「新的盖旧的」——补整理重读旧消息时旧名字不会盖掉新名字。
- 显示名永远不是平台 id：名字为空或恰好等于 id 的不记；查不到就用调用方给的快照，
  快照也没有（或快照就是 id）就返回空字符串，由调用方决定写「群友」之类。
- 模型写的文字里提到群友用 ``{@平台id}``（``token(uid)``）；任何给人看 / 给群看 / 给别的模块
  看的出口都要过 ``render``，换成当前名字，不认识的换成「某群友」，平台 id 不外漏。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

UNKNOWN = "某群友"
TOKEN_RE = re.compile(r"\{@([0-9A-Za-z_\-]{3,40})\}")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS members (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);
"""


def token(user_id: Any) -> str:
    return "{@" + str(user_id or "").strip() + "}"


def _clean(name: Any, user_id: Any) -> str:
    n = str(name or "").strip()
    if not n or n == str(user_id or "").strip():
        return ""
    return n


def record(conn: sqlite3.Connection, group_id: Any, user_id: Any, name: Any, ts: float) -> None:
    """记一次「这个人此刻叫这个名字」（在调用方事务里）。旧消息不盖新名字；空名 / 名字就是 id 不记。"""
    gid = str(group_id or "").strip()
    uid = str(user_id or "").strip()
    n = _clean(name, uid)
    if not gid or not uid or not n:
        return
    conn.execute(
        "INSERT INTO members (group_id, user_id, name, ts) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(group_id, user_id) DO UPDATE SET name=excluded.name, ts=excluded.ts"
        " WHERE excluded.ts >= members.ts",
        (gid, uid, n, float(ts or 0)),
    )


def _conn(store_or_conn: Any) -> sqlite3.Connection:
    if isinstance(store_or_conn, sqlite3.Connection):
        return store_or_conn
    return store_or_conn.read()


def names_of(store_or_conn: Any, group_id: Any, user_ids: Iterable[Any]) -> dict[str, str]:
    """一批 id → 当前名字（查不到的不出现在结果里）。"""
    ids = sorted({str(u or "").strip() for u in user_ids if str(u or "").strip()})
    if not ids:
        return {}
    out: dict[str, str] = {}
    conn = _conn(store_or_conn)
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        rows = conn.execute(
            f"SELECT user_id, name FROM members WHERE group_id=? AND user_id IN ({','.join('?' * len(chunk))})",
            (str(group_id), *chunk),
        ).fetchall()
        for r in rows:
            n = _clean(r["name"], r["user_id"])
            if n:
                out[str(r["user_id"])] = n
    return out


def name_of(store_or_conn: Any, group_id: Any, user_id: Any, fallback: Any = "") -> str:
    """当前显示名；查不到用 fallback（老记录里的名字快照）；都没有 → ""（绝不返回 id）。"""
    uid = str(user_id or "").strip()
    if uid:
        got = names_of(store_or_conn, group_id, [uid]).get(uid)
        if got:
            return got
    return _clean(fallback, uid)


def render(store_or_conn: Any, group_id: Any, text: Any) -> str:
    """把文字里的 {@id} 换成当前名字；不认识的换「某群友」。"""
    s = str(text or "")
    if "{@" not in s:
        return s
    names = names_of(store_or_conn, group_id, TOKEN_RE.findall(s))
    return TOKEN_RE.sub(lambda m: names.get(m.group(1)) or UNKNOWN, s)


def legend(store_or_conn: Any, group_id: Any, texts: Iterable[Any]) -> str:
    """给模型看的对照表：这些文字里出现的 {@id} 现在叫什么。没有就返回 ""。"""
    ids: list[str] = []
    for t in texts:
        for uid in TOKEN_RE.findall(str(t or "")):
            if uid not in ids:
                ids.append(uid)
    if not ids:
        return ""
    names = names_of(store_or_conn, group_id, ids)
    return "、".join(f"{token(u)}={names.get(u) or '（不认识，可能已退群）'}" for u in ids)


def seed_from_activity(conn: sqlite3.Connection) -> None:
    """老库上线名册：从 member_activity 取每人最近一天的名字（ts=0，之后任何新消息都能盖掉）。"""
    rows = conn.execute(
        "SELECT a.group_id, a.user_id, a.name FROM member_activity a"
        " JOIN (SELECT group_id, user_id, MAX(day) AS d FROM member_activity GROUP BY group_id, user_id) b"
        " ON a.group_id=b.group_id AND a.user_id=b.user_id AND a.day=b.d"
    ).fetchall()
    for r in rows:
        record(conn, r["group_id"], r["user_id"], r["name"], 0.0)


_BARE_ID_RE = re.compile(r"(?<![\d@])\d{5,12}(?!\d)")


def _known_names(conn: sqlite3.Connection, gid: str, uid: str) -> list[str]:
    """这个人用过的名字（名册当前名 + 发言统计里的历史名），长的在前。"""
    names: set[str] = set()
    for r in conn.execute("SELECT name FROM members WHERE group_id=? AND user_id=?", (gid, uid)):
        names.add(str(r["name"] or "").strip())
    try:
        for r in conn.execute(
            "SELECT DISTINCT name FROM member_activity WHERE group_id=? AND user_id=?", (gid, uid)
        ):
            names.add(str(r["name"] or "").strip())
    except sqlite3.Error:
        pass
    return sorted((n for n in names if n and n != uid), key=len, reverse=True)


def tokenize_ids(store_or_conn: Any, group_id: Any, text: Any) -> str:
    """模型写进文字的「本群认识的平台 id」一律转成 {@id}（名字(QQ id) / {@id}(id) / QQ id / 裸 id）。

    只动本群名册里有的 id，别的数字（价格、编号）不碰。
    """
    s = str(text or "")
    gid = str(group_id or "")
    cands = {m.group(0) for m in _BARE_ID_RE.finditer(s)}
    if not cands:
        return s
    conn = _conn(store_or_conn)
    known = set(names_of(conn, gid, cands))
    for uid in sorted(known, key=len, reverse=True):
        tok = token(uid)
        paren = r"\s*[（(]\s*(?:QQ\s*[:：号]?\s*)?" + re.escape(uid) + r"\s*[)）]"
        for name in _known_names(conn, gid, uid):
            s = re.sub(re.escape(name) + paren, lambda _m: tok, s)
        s = re.sub(re.escape(tok) + paren, lambda _m: tok, s)
        s = re.sub(r"QQ\s*[:：号]?\s*" + re.escape(uid) + r"(?!\d)", lambda _m: tok, s)
        s = re.sub(r"(?<![\d@])" + re.escape(uid) + r"(?!\d)", lambda _m: tok, s)
    return s
