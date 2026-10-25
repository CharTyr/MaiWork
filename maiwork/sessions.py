"""工作会话的纯数据与规则；写方法只使用调用方事务，不调模型、宿主或发送。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock
from .store import Store, next_id

EVENT_KINDS = frozenset({
    "instruction", "new_work", "supplement", "correction", "pause", "resume", "end",
    "progress_query", "clarify_q", "clarify_a", "adopt", "admin_stop",
})
EVENT_STATUSES = frozenset({"received", "applied", "stale", "rejected"})
_OWNER_INSTRUCTIONS = EVENT_KINDS - {"clarify_q", "admin_stop"}


def _dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Sessions:
    def __init__(self, store: Store) -> None:
        self._store = store
        self.on_change = None
        self._change_pending = False

    def _changed(self):
        """下一轮事件循环再通知：调用方此时已提交或回滚。"""
        import asyncio
        if self.on_change is None or self._change_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._change_pending = True
        def notify():
            self._change_pending = False
            if self.on_change is not None:
                self.on_change()
        loop.call_soon(notify)


    def open_for_request(self, conn: sqlite3.Connection, request_row: Any) -> dict | None:
        """群友任务请求建立时开会话；群里已有未关会话时，排队留给调度层。"""
        row = dict(request_row)
        owner = str(row.get("requester_id") or "").strip()
        if row.get("kind") != "task" or not owner or row.get("source", "") not in {"", "idea"}:
            return None
        platform = str(row.get("platform") or "").strip()
        gid = str(row.get("group_id") or "").strip()
        rid = str(row.get("id") or "").strip()
        if not platform or not gid or not rid:
            raise ValueError("开会话需要可信的平台、群号和请求编号")
        if conn.execute(
            "SELECT 1 FROM task_sessions WHERE platform=? AND group_id=? AND state!='closed'",
            (platform, gid),
        ).fetchone() is not None:
            return None
        # 同一请求关闭后也不能被重投重新开会话。
        if conn.execute("SELECT 1 FROM task_sessions WHERE request_id=?", (rid,)).fetchone():
            return None
        sid, now = next_id(conn, "S"), clock.now()
        conn.execute(
            "INSERT INTO task_sessions(id,platform,group_id,owner_id,request_id,state,opened_at,last_active_at)"
            " VALUES(?,?,?,?,?,'awaiting_approval',?,?)",
            (sid, platform, gid, owner, rid, now, now),
        )
        self._changed()
        return _dict(conn.execute("SELECT * FROM task_sessions WHERE id=?", (sid,)).fetchone())

    def get_open(self, group_id: str, *, platform: str) -> dict | None:
        """按可信的平台和群号读取；平台必须显式传入。"""
        return _dict(self._store.read().execute(
            "SELECT * FROM task_sessions WHERE platform=? AND group_id=? AND state!='closed'",
            (str(platform), str(group_id)),
        ).fetchone())

    def get_open_for_owner(self, platform: str, group_id: str, user_id: str) -> dict | None:
        """引用、昵称和正文都不参与身份判定。"""
        row = self.get_open(group_id, platform=platform)
        return row if row is not None and row["owner_id"] == str(user_id) else None

    def open_groups(self) -> set[tuple[str, str]]:
        """返回未关闭的（平台，群号），供后续组件开关对齐。"""
        return {(str(r["platform"]), str(r["group_id"])) for r in self._store.read().execute(
            "SELECT platform,group_id FROM task_sessions WHERE state!='closed'"
        )}

    def close(self, conn: sqlite3.Connection, session_id: str, reason: str) -> None:
        """幂等关闭并清租约引用；实际电脑释放由后续租约执行层负责。"""
        if not str(reason).strip():
            raise ValueError("关闭会话必须说明原因")
        conn.execute(
            "UPDATE task_sessions SET state='closed',closed_at=?,closed_reason=?,env_lease=NULL,"
            " revision=revision+1 WHERE id=? AND state!='closed'",
            (clock.now(), str(reason), str(session_id)),
        )
        self._changed()

    def on_request_rejected(self, conn: sqlite3.Connection, request_id: str) -> None:
        """和请求拒绝同一个事务，关闭它已经打开的会话。"""
        row = self.request_session(conn, request_id)
        if row is not None:
            self.close(conn, row["id"], "rejected")

    def attach_request(self, conn: sqlite3.Connection, session_id: str, request_row: Any) -> None:
        """主人在会话里派新活：批准前先绑定，免批落地也用同一事务。"""
        request = dict(request_row)
        session = conn.execute("SELECT * FROM task_sessions WHERE id=? AND state!='closed'", (session_id,)).fetchone()
        if session is None or session["group_id"] != request["group_id"] or session["owner_id"] != request["requester_id"]:
            raise ValueError("新请求只能接在本群主人的未关闭会话里")
        self._store.kv_set(conn, "session.request." + str(request["id"]), session_id)
        self._changed()

    def request_session(self, conn: sqlite3.Connection, request_id: str):
        """原请求和后续请求共用关联查询；关闭后的会话不再接任务。"""
        row = conn.execute("SELECT value FROM kv WHERE key=?", ("session.request." + str(request_id),)).fetchone()
        sid = json.loads(row[0]) if row else ""
        return conn.execute(
            "SELECT * FROM task_sessions WHERE (request_id=? OR id=?) AND state!='closed'",
            (str(request_id), sid),
        ).fetchone()

    def on_task_landed(self, conn: sqlite3.Connection, request_id: str, task_id: str) -> None:
        """批准落地时关联任务、递增轮次；重复通知不占第二个轮次。"""
        session = self.request_session(conn, request_id)
        if session is None:
            return
        task = conn.execute("SELECT group_id,session_id FROM tasks WHERE id=?", (str(task_id),)).fetchone()
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        if task["group_id"] != session["group_id"]:
            raise ValueError("任务与会话不在同一群")
        if task["session_id"] is not None:
            if task["session_id"] != session["id"]:
                raise ValueError("任务已经属于另一个会话")
            return
        turn = conn.execute(
            "SELECT COALESCE(MAX(turn_no),0)+1 FROM tasks WHERE session_id=?", (session["id"],)
        ).fetchone()[0]
        conn.execute("UPDATE tasks SET session_id=?,turn_no=? WHERE id=?", (session["id"], turn, str(task_id)))
        conn.execute(
            "UPDATE task_sessions SET state='working',current_task_id=?,revision=revision+1 WHERE id=?",
            (str(task_id), session["id"]),
        )
        self.sync_task_state(conn, session["id"])
        self._changed()

    def sync_task_state(self, conn: sqlite3.Connection, session_id: str) -> None:
        """任务状态变化后的同事务投影；失败也结束本轮，绝不复活旧任务。"""
        session = conn.execute("SELECT * FROM task_sessions WHERE id=? AND state!='closed'", (session_id,)).fetchone()
        if session is None:
            return
        rows = conn.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY turn_no", (session_id,)).fetchall()
        if not rows:
            return
        active = [r for r in rows if r["status"] not in {"completed", "failed", "cancelled", "rejected"}]
        current = next((r for r in active if r["status"] in {"running", "reviewing"}), active[0] if active else rows[-1])
        status = current["status"]
        state = {"waiting_input": "asking", "shelved": "asking", "paused": "paused", "pending_approval": "awaiting_approval"}.get(status, "working" if active else "idle")
        if not active:
            pending = conn.execute("SELECT value FROM kv WHERE key=?", ("session.pending." + session_id,)).fetchone()
            rid = json.loads(pending[0]) if pending else session["request_id"]
            awaiting = conn.execute("SELECT 1 FROM requests WHERE id=? AND status='pending'", (rid,)).fetchone()
            clarification = conn.execute("SELECT 1 FROM kv WHERE key=?", ("session.clarification." + session_id,)).fetchone()
            if awaiting:
                state = "awaiting_approval"
            elif clarification:
                state = "asking"
            elif session["state"] == "paused":
                state = "paused"
        delivered = max([float(r["finished_ts"] or r["updated"]) for r in rows if r["status"] == "completed"] or [float(session["last_delivery_at"] or 0)])
        if state != session["state"] or current["id"] != session["current_task_id"] or delivered > float(session["last_delivery_at"] or 0):
            conn.execute("UPDATE task_sessions SET state=?,current_task_id=?,last_delivery_at=MAX(COALESCE(last_delivery_at,0),?),revision=revision+1 WHERE id=?", (state, current["id"], delivered, session_id))
            self._changed()

    def record_event(
        self, conn: sqlite3.Connection, session_id: str, *, source_key: str, kind: str, text: str,
        user_id: str = "", quoted_ref: str = "", status: str = "received", note: str = "",
    ) -> dict:
        """在调用方写事务里分配序号、幂等收件；只有主人有效指令刷新活跃时间。"""
        if not str(source_key).strip() or kind not in EVENT_KINDS or status not in EVENT_STATUSES:
            raise ValueError("会话事件来源、类型或状态不合法")
        existing = conn.execute("SELECT * FROM session_events WHERE source_key=?", (source_key,)).fetchone()
        if existing is not None:
            if existing["session_id"] != str(session_id):
                raise ValueError("来源消息已经属于另一个会话")
            return dict(existing)
        row = conn.execute("SELECT * FROM task_sessions WHERE id=?", (str(session_id),)).fetchone()
        if row is None:
            raise KeyError(f"会话不存在: {session_id}")
        now = clock.now()
        revision = int(row["revision"])
        if (row["state"] != "closed" and str(user_id) == row["owner_id"]
                and kind in _OWNER_INSTRUCTIONS and status in {"received", "applied"}):
            revision += 1
            conn.execute(
                "UPDATE task_sessions SET last_active_at=MAX(last_active_at,?),revision=? WHERE id=?",
                (now, revision, str(session_id)),
            )
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM session_events WHERE session_id=?", (str(session_id),)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO session_events(session_id,seq,source_key,kind,text,quoted_ref,status,revision,received_at,applied_at,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(session_id), seq, source_key, kind, str(text), str(quoted_ref), status, revision,
             now, now if status == "applied" else None, str(note)),
        )
        self._changed()
        return dict(conn.execute("SELECT * FROM session_events WHERE id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def idle_deadline(row: Any, *, idle_minutes: float = 60) -> float | None:
        """只有空闲与暂停计时，交付和主人指令中较晚的一个作为起点。"""
        row = dict(row)
        if row["state"] not in {"idle", "paused"}:
            return None
        return max(float(row.get("last_delivery_at") or 0), float(row.get("last_active_at") or 0)) + float(idle_minutes) * 60

    @classmethod
    def is_idle_expired(cls, row: Any, *, now: float | None = None, idle_minutes: float = 60) -> bool:
        """只计算是否到期，不在读路径里偷偷关闭会话。"""
        deadline = cls.idle_deadline(row, idle_minutes=idle_minutes)
        return deadline is not None and (clock.now() if now is None else float(now)) >= deadline
