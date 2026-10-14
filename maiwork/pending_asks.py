"""待判 @ 请求（pending_asks）的状态机：先记、再判、再落地，每一步都落库可恢复。

2026-10-13 生产巡检：9 条 @ 请求在 pending_asks 里搁了 3~15 天——判断结论只在内存里，
落地失败 / 画像游标走过去就再没人管。这里把整条链拆成持久的几步：

- record_pending：收到一条要判的 @（只记服务群）。同一条消息再来只更新文字，
  **不改** 首次时间（ts / first_seen）、不把终态重新打开。
- decide：记下判断结论（decision，JSON）。prepare / goal / reminder → decided（待落地）；
  明确 none → 终态；reminder 没有可用时间 → 结论先存着、仍是 pending（等补时间）。
- fail：落地 / 分类失败，按指数退避排下次（attempts / next_try_ts / last_error）。
- complete：只有真落地了才算完成（request / reminder 必须带真 id；screened 是被相似拒绝筛掉）。

handled 列跟 state 同步：终态（completed / none / screened / review）=1，其余 =0——老读者
（intake.slow_queue、画像提示里的标记）照旧只看 handled。超 48 小时还没落地的标 review
（review=1、handled=1）留在表里给人看，不删。所有写都是短事务；给了 conn 就并进调用方事务。
"""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from typing import Any, Callable, Optional

from . import clock

logger = logging.getLogger("maiwork.pending_asks")

KINDS = ("prepare", "goal", "reminder", "none")
REPEATS = ("daily", "weekly", "workdays")
OPEN_STATES = ("pending", "decided")
TERMINAL_STATES = ("completed", "none", "screened", "review")
STALE_S = 48 * 3600
_BACKOFF_BASE_S = 60.0
_BACKOFF_MAX_S = 6 * 3600.0
_TEXT_MAX = 500
_TITLE_MAX = 30
_ERROR_MAX = 300


class PendingAsks:
    def __init__(self, store: Any, get_settings: Callable[[], Any]) -> None:
        self._store = store
        self._get_settings = get_settings

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _served(self, gid: str) -> bool:
        try:
            return bool(self._get_settings().is_served(str(gid)))
        except Exception:
            return False

    def _tx(self, conn: Any):
        return self._store.tx() if conn is None else nullcontext(conn)

    @staticmethod
    def _row(conn: Any, gid: str, mid: str) -> Any:
        return conn.execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND message_id=?", (gid, mid)
        ).fetchone()

    @staticmethod
    def _field(msg: Any, *names: str) -> Any:
        for n in names:
            v = msg.get(n) if isinstance(msg, dict) else getattr(msg, n, None)
            if v not in (None, ""):
                return v
        return None

    # ------------------------------------------------------------------
    # 记
    # ------------------------------------------------------------------

    def record_pending(self, gid: str, msg: Any, reason: str, *, conn: Any = None) -> Optional[dict]:
        """记一条待判 @（msg 可以是 dict 或 host.Msg）。非服务群 / 没消息号 → None。"""
        gid = str(gid or "")
        if not gid or not self._served(gid):
            return None
        mid = str(self._field(msg, "message_id", "id") or "")
        if not mid:
            return None
        now = clock.now()
        try:
            ts = float(self._field(msg, "timestamp", "ts", "message_ts") or 0) or now
        except (TypeError, ValueError):
            ts = now
        user_id = str(self._field(msg, "user_id") or "")
        user_name = str(self._field(msg, "user_name") or "")
        text = str(self._field(msg, "text") or "")[:_TEXT_MAX]
        with self._tx(conn) as c:
            # 冲突时只在还没判（pending）时刷新文字和原因；时间、状态、结论一概不动
            c.execute(
                "INSERT INTO pending_asks (group_id, message_id, user_id, user_name, text, ts, reason,"
                " handled, state, first_seen, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)"
                " ON CONFLICT(group_id, message_id) DO UPDATE SET"
                " user_id=CASE WHEN excluded.user_id!='' THEN excluded.user_id ELSE pending_asks.user_id END,"
                " user_name=CASE WHEN excluded.user_name!='' THEN excluded.user_name ELSE pending_asks.user_name END,"
                " text=CASE WHEN pending_asks.state='pending' AND excluded.text!='' THEN excluded.text"
                " ELSE pending_asks.text END,"
                " reason=CASE WHEN pending_asks.state='pending' THEN excluded.reason ELSE pending_asks.reason END,"
                " updated=excluded.updated",
                (gid, mid, user_id, user_name, text, ts, str(reason or ""), ts, now),
            )
            row = self._row(c, gid, mid)
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # 判
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(decision: Any) -> tuple[Optional[dict], bool]:
        """→ (要存的结论, 是否完整可落地)。认不出的 kind → (None, False)。"""
        if not isinstance(decision, dict):
            return None, False
        kind = str(decision.get("kind") or "").strip().lower()
        if kind not in KINDS:
            return None, False
        out: dict = {"kind": kind}
        for k in ("title", "via", "why"):
            v = str(decision.get(k) or "").strip()
            if v:
                out[k] = v[:_TITLE_MAX] if k == "title" else v[:200]
        if kind != "reminder":
            return out, True
        from .profile import _parse_bj_when  # profile 顶层导入本模块，这里局部导入防循环

        remind = decision.get("remind_ts")
        try:
            remind_ts = float(remind) if remind is not None and not isinstance(remind, bool) else None
        except (TypeError, ValueError):
            remind_ts = None
        when = str(decision.get("when") or "").strip()
        if remind_ts is None and when:
            remind_ts = _parse_bj_when(when)
        if when:
            out["when"] = when
        due = decision.get("due_ts")
        try:
            due_ts = float(due) if due is not None and not isinstance(due, bool) else None
        except (TypeError, ValueError):
            due_ts = None
        repeat = str(decision.get("repeat") or "").strip().lower()
        out["repeat"] = repeat if repeat in REPEATS else None
        if remind_ts is None or remind_ts <= 0:
            return out, False
        out["remind_ts"] = remind_ts
        out["due_ts"] = due_ts if due_ts is not None else remind_ts
        return out, True

    def decide(self, gid: str, mid: str, decision: dict, *, conn: Any = None) -> bool:
        """记判断结论。返回 True = 结论完整、已进 decided（none 直接终态）。

        行不存在 / 非服务群 / 已是终态 → False（终态不重新打开）。已 decided 的不改主意（返回 True）。
        reminder 缺可用时间：结论先存（state 仍 pending），返回 False。
        """
        gid, mid = str(gid or ""), str(mid or "")
        if not gid or not mid or not self._served(gid):
            return False
        norm, complete = self._normalize(decision)
        if norm is None:
            return False
        now = clock.now()
        with self._tx(conn) as c:
            row = self._row(c, gid, mid)
            if row is None or str(row["state"]) not in OPEN_STATES:
                return False
            if str(row["state"]) == "decided":
                return True
            payload = json.dumps(norm, ensure_ascii=False)
            if not complete:
                c.execute(
                    "UPDATE pending_asks SET decision=?, updated=? WHERE group_id=? AND message_id=?",
                    (payload, now, gid, mid),
                )
                return False
            state, handled = ("none", 1) if norm["kind"] == "none" else ("decided", 0)
            c.execute(
                "UPDATE pending_asks SET decision=?, state=?, handled=?, next_try_ts=0, updated=?"
                " WHERE group_id=? AND message_id=?",
                (payload, state, handled, now, gid, mid),
            )
        return True

    # ------------------------------------------------------------------
    # 失败 / 完成
    # ------------------------------------------------------------------

    def fail(self, gid: str, mid: str, error: Any, *, conn: Any = None) -> bool:
        """落地（decided）或分类（pending）失败：按指数退避排下次。终态不动。"""
        gid, mid = str(gid or ""), str(mid or "")
        now = clock.now()
        with self._tx(conn) as c:
            row = self._row(c, gid, mid)
            if row is None or str(row["state"]) not in OPEN_STATES:
                return False
            attempts = int(row["attempts"] or 0) + 1
            delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** (attempts - 1)))
            c.execute(
                "UPDATE pending_asks SET attempts=?, next_try_ts=?, last_error=?, updated=?"
                " WHERE group_id=? AND message_id=?",
                (attempts, now + delay, str(error or "")[:_ERROR_MAX], now, gid, mid),
            )
        return True

    def complete(
        self, gid: str, mid: str, *, result_kind: str = "request", result_id: str = "", conn: Any = None
    ) -> bool:
        """真落地了才调。request / reminder 必须带真 id；screened 允许空 id。已完成同结果 → True（幂等）。"""
        gid, mid = str(gid or ""), str(mid or "")
        kind = str(result_kind or "").strip()
        rid = str(result_id or "").strip()
        if kind not in ("request", "reminder", "screened"):
            return False
        if kind != "screened" and not rid:
            return False
        state = "screened" if kind == "screened" else "completed"
        now = clock.now()
        with self._tx(conn) as c:
            row = self._row(c, gid, mid)
            if row is None:
                return False
            cur = str(row["state"])
            if cur in ("completed", "screened"):
                return cur == state and str(row["result_id"] or "") == rid
            if cur == "none":
                return False
            # pending / decided / review（复核期间迟到的成功也如实记上）
            c.execute(
                "UPDATE pending_asks SET state=?, handled=1, result_kind=?, result_id=?, last_error='',"
                " updated=? WHERE group_id=? AND message_id=?",
                (state, kind, rid, now, gid, mid),
            )
        return True

    # ------------------------------------------------------------------
    # 查 / 过期
    # ------------------------------------------------------------------

    @staticmethod
    def view(row: Any) -> dict:
        d = dict(row)
        try:
            d["decision"] = json.loads(d.get("decision") or "") if d.get("decision") else None
        except (TypeError, ValueError):
            d["decision"] = None
        return d

    def get(self, gid: str, mid: str) -> Optional[dict]:
        row = self._row(self._store.read(), str(gid), str(mid))
        return self.view(row) if row is not None else None

    def due(self, gid: str, *, now: float | None = None, limit: int = 8) -> list[dict]:
        """判了还没落地、退避时间已到的（最老的先）。"""
        t = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND state='decided' AND next_try_ts<=?"
            " ORDER BY first_seen ASC, message_id ASC LIMIT ?",
            (str(gid), t, max(1, int(limit))),
        ).fetchall()
        return [self.view(r) for r in rows]

    def undecided(self, gid: str, *, before: float, now: float | None = None, limit: int = 8) -> list[dict]:
        """还没判、首次出现早于 before、退避时间已到的（交给独立分类）。"""
        t = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND state='pending' AND (CASE WHEN first_seen>0 THEN first_seen ELSE ts END)<=?"
            " AND next_try_ts<=? ORDER BY first_seen ASC, message_id ASC LIMIT ?",
            (str(gid), float(before), t, max(1, int(limit))),
        ).fetchall()
        return [self.view(r) for r in rows]

    def review_view(self, gid: str, *, limit: int = 100) -> list[dict]:
        """只读本服务群的待复核，最老的先；管理员身份由 HTTP 层校验。"""
        if not self._served(gid):
            return []
        rows = self._store.read().execute(
            "SELECT * FROM pending_asks WHERE group_id=? AND state='review'"
            " ORDER BY first_seen ASC, message_id ASC LIMIT ?", (str(gid), min(100, max(1, int(limit)))),
        ).fetchall()
        return [self.view(r) for r in rows]

    def review_action(self, gid: str, mid: str, action: str, *, by: str,
                      approvals: Any = None, kind: str = "task") -> dict:
        """人工复核，不调用模型/宿主。转请求始终强制待批；失败保留原行。

        create 按来源消息去重；若建请求后关行失败，再点击可复用同一请求。
        全程同步、无 await，网页与后台重试不会在两步中间交错。
        """
        gid, mid = str(gid), str(mid)
        if not self._served(gid):
            raise KeyError("没有这个群")
        if action not in ("request", "ignore") or (action == "request" and kind not in ("task", "goal")):
            raise ValueError("只能转为任务 / 目标请求，或忽略")
        row = self.get(gid, mid)
        if row is None:
            raise KeyError("找不到这条待复核")
        if action == "request" and row["state"] == "completed" and row["result_kind"] == "request":
            return {"ok": True, "request_id": row["result_id"]}
        if action == "ignore" and row["state"] == "none" and row["result_kind"] == "ignored":
            return {"ok": True}
        if row["state"] != "review":
            raise ValueError("这条已处理，请刷新页面")
        rid = ""
        if action == "request":
            if approvals is None:
                raise RuntimeError("批准模块还没准备好")
            decision = row.get("decision") or {}
            res = approvals.create(
                gid, kind=kind, title=str(decision.get("title") or row["text"] or "群里的请求")[:30],
                quote=str(row["text"]), via="管理员从待复核转入", requester_id=str(row["user_id"]),
                requester_name=str(row["user_name"]), message_id=mid, force_manual=True, screen=False,
            )
            rid = str(res.get("id") or "")
            if not rid:
                raise RuntimeError("没有拿到请求编号，请稍后再试")
        with self._store.tx() as conn:
            cur = self._row(conn, gid, mid)
            if cur is None or cur["state"] != "review":
                raise ValueError("这条已处理，请刷新页面")
            conn.execute(
                "UPDATE pending_asks SET state=?, handled=1, review=0, result_kind=?, result_id=?,"
                " updated=? WHERE group_id=? AND message_id=?",
                ("completed" if rid else "none", "request" if rid else "ignored", rid, clock.now(), gid, mid),
            )
            self._store.event(conn, "ask.review", group_id=gid,
                              payload={"message_id": mid, "action": action, "by": by, "request_id": rid})
        return {"ok": True, "request_id": rid} if rid else {"ok": True}

    def mark_stale(self, gid: str, *, now: float | None = None, conn: Any = None) -> int:
        """超 48 小时还没落地的：标 review 留着（handled=1，不再自动重试），不删。"""
        t = clock.now() if now is None else float(now)
        with self._tx(conn) as c:
            cur = c.execute(
                "UPDATE pending_asks SET state='review', review=1, handled=1, updated=?"
                " WHERE group_id=? AND state IN ('pending', 'decided')"
                " AND (CASE WHEN first_seen>0 THEN first_seen ELSE ts END)<?",
                (t, str(gid), t - STALE_S),
            )
            n = int(cur.rowcount or 0)
        if n:
            logger.warning("群 %s 有 %d 条 @ 请求超 48 小时没落地，标为待复核（保留）", gid, n)
        return n
