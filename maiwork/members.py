"""成员名册：按（群, 平台 id）认人，名字只用来显示、跟着改名更新。

- 平台 id（QQ 平台就是 QQ 号）是唯一身份；群名片 / QQ 昵称只是显示名，随时会改。
- 名册表 ``members(group_id, user_id, name, ts)``：每读到一条消息顺手记发言人当时的显示名
  （名片 > 昵称），按消息时间「新的盖旧的」——补整理重读旧消息时旧名字不会盖掉新名字。
- 显示名永远不是平台 id：名字为空或恰好等于 id 的不记；查不到就用调用方给的快照，
  快照也没有（或快照就是 id）就返回空字符串，由调用方决定写「群友」之类。
- 模型写的文字里提到群友用 ``{@平台id}``（``token(uid)``）；任何给人看 / 给群看 / 给别的模块
  看的出口都要过 ``render``，换成当前名字，不认识的换成「某群友」，平台 id 不外漏。
- 久不说话 / 从没说过话的人：``refresh_from_host`` 在后台直接问 QQ 群名片（没有用 QQ 昵称），
  名字按「此刻」记进名册（``checked_ts`` 记问过的时间，控制多久问一次）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Iterable

logger = logging.getLogger(__name__)

UNKNOWN = "某群友"
TOKEN_RE = re.compile(r"\{@([0-9A-Za-z_\-]{3,40})\}")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS members (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL DEFAULT 0,
    checked_ts REAL NOT NULL DEFAULT 0,
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


# ----------------------------------------------------------------------
# 跟 QQ 对名字（后台；app 每群 30 分钟派一轮，只给服务群）
# ----------------------------------------------------------------------

FOCUS_RECHECK_S = 6 * 3600.0     # 关注成员：6 小时问一次（网页显示「名片（昵称）」）
MEMBER_RECHECK_S = 24 * 3600.0   # 其他人：24 小时问一次


def _refresh_candidates(conn: sqlite3.Connection, gid: str, now: float, limit: int) -> list[str]:
    """要问的 QQ 号：过期的关注成员 → 被引用但名册里没有的 → 名册里过期的（问得最早的在前）。"""
    out: list[str] = []

    def add(uid: Any) -> None:
        u = str(uid or "").strip()
        if u.isdigit() and u not in out:
            out.append(u)

    for r in conn.execute(
        "SELECT user_id FROM focus_members WHERE group_id=? AND removed=0 AND COALESCE(profile_ts,0)<?"
        " ORDER BY COALESCE(profile_ts,0), user_id",
        (gid, now - FOCUS_RECHECK_S),
    ):
        add(r["user_id"])
    for sql in (
        "SELECT DISTINCT target_user_id AS u FROM ideas WHERE group_id=?",
        "SELECT DISTINCT target_user_id AS u FROM news_items WHERE group_id=?",
        "SELECT DISTINCT requester_id AS u FROM requests WHERE group_id=?",
        "SELECT DISTINCT who_id AS u FROM goals WHERE group_id=?",
    ):
        try:
            rows = conn.execute(sql, (gid,)).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            u = str(r["u"] or "").strip()
            if not u:
                continue
            if conn.execute("SELECT 1 FROM members WHERE group_id=? AND user_id=?", (gid, u)).fetchone() is None:
                add(u)
    for r in conn.execute(
        "SELECT user_id FROM members WHERE group_id=? AND checked_ts<? ORDER BY checked_ts, user_id",
        (gid, now - MEMBER_RECHECK_S),
    ):
        add(r["user_id"])
    return out[: max(0, int(limit))]


async def refresh_from_host(store: Any, host: Any, group_id: Any, now: float, *, limit: int = 20) -> int:
    """问 QQ 这些人在本群的群名片 / 昵称，记进名册（和关注成员的 card / nickname 缓存）。

    - 名字 = 群名片，没有就 QQ 昵称；等于 QQ 号的不算名字。按 ts=now 记：之后的新消息能盖它，旧消息不能。
    - 不管问没问到都记 checked_ts（退群 / 接口失败也不反复问）；单个人出错跳过。
    - 调用方保证只对服务群调用。返回拿到名字的人数。日志只写人数。
    """
    ask = getattr(host, "group_member_card", None)
    if store is None or ask is None:
        return 0
    gid = str(group_id or "").strip()
    now = float(now)
    uids = _refresh_candidates(store.read(), gid, now, limit)
    got = 0
    for uid in uids:
        try:
            info = await ask(gid, uid)
        except Exception:
            logger.debug("问 QQ 群名片出错（群 %s），跳过这个人", gid, exc_info=True)
            info = {}
        info = info if isinstance(info, dict) else {}
        card = _clean(info.get("card"), uid)
        nick = _clean(info.get("nickname"), uid)
        name = card or nick
        with store.tx() as conn:
            if name:
                record(conn, gid, uid, name, now)
                got += 1
            conn.execute(
                "INSERT INTO members (group_id, user_id, name, ts, checked_ts) VALUES (?, ?, '', 0, ?)"
                " ON CONFLICT(group_id, user_id) DO UPDATE SET checked_ts=excluded.checked_ts",
                (gid, uid, now),
            )
            if card or nick:
                conn.execute(
                    "UPDATE focus_members SET card=?, nickname=?, profile_ts=? WHERE group_id=? AND user_id=?",
                    (card, nick, now, gid, uid),
                )
            else:
                conn.execute(
                    "UPDATE focus_members SET profile_ts=? WHERE group_id=? AND user_id=?",
                    (now, gid, uid),
                )
    if uids:
        logger.info("群 %s 跟 QQ 对名字：问了 %d 人，拿到 %d 人", gid, len(uids), got)
    return got
