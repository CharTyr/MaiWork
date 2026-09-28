"""M3 数据层：任务（docs/02 §7.2 状态机、docs/07 §9.3 / §11.2）。

只放数据和规则：不调模型、不调宿主、不发消息。

要点：
- 合法转移严格按 02 §7.2 的表；非法转移抛 ValueError（中文），状态不变。
- transition 在一个事务里完成：改状态 + 更新 updated + 写事件（kind "task.<to>"）。
- 到 running 第一次记 started_ts（重进不清）；到任一终态记 finished_ts。
- revise 存新需求版本；任务在 running/reviewing 时排回 queued（旧尝试作废）。
- 取消 / 失败 / 完成 / 暂停后，晚到的结果一律不接（accept_result=False），attempt 标 stale，
  历史保留，任务状态绝不复活（红线：「取消 → …晚到的结果只记历史，不会让任务复活或再次交付」）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Iterable

from . import clock
from .store import Store, next_id

_STATUS_KINDS = (
    "pending_approval", "queued", "running", "waiting_input", "shelved",
    "reviewing", "completed", "failed", "paused", "cancelled", "rejected",
)

# 02 §7.2 的合法转移表：任何不在表里的转移都是非法的
_LEGAL: dict[str, frozenset[str]] = {
    "pending_approval": frozenset({"queued", "rejected", "cancelled"}),
    "queued": frozenset({"running", "paused", "cancelled"}),
    "running": frozenset({"reviewing", "waiting_input", "failed", "paused", "cancelled", "queued"}),
    "reviewing": frozenset({"completed", "running", "queued", "failed", "waiting_input", "cancelled"}),
    "waiting_input": frozenset({"queued", "running", "shelved", "cancelled"}),
    "shelved": frozenset({"queued", "cancelled"}),
    "paused": frozenset({"queued", "cancelled"}),
    "failed": frozenset({"queued"}),   # 失败只允许重试（回 queued）
    "completed": frozenset(),           # 终态；只允许重新交付，不改状态
    "cancelled": frozenset(),
    "rejected": frozenset(),
}

# transition(**fields) 允许顺带更新的列（delivery 给 list 会序列化成 JSON）
_MUTABLE_FIELDS = frozenset({
    "review", "question", "question_ts", "question_msg_id",
    "env", "delivery_kind", "delivery", "undelivered", "tokens",
})

_TERMINAL = frozenset({"completed", "cancelled", "rejected"})


def _load_json(s: Any, default: Any) -> Any:
    try:
        return json.loads(s) if s else default
    except (ValueError, TypeError):
        return default


class Tasks:
    def __init__(self, store: Store, get_settings: Callable[[], Any], tools: Any = None) -> None:
        self._store = store
        self._get_settings = get_settings
        self._tools = tools

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> dict | None:
        row = self._store.read().execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    # ------------------------------------------------------------------
    # create / revise
    # ------------------------------------------------------------------

    def create(
        self,
        group_id: str,
        *,
        title: str,
        req: str,
        criteria: Iterable[str],
        source: str,
        requester_id: str = "",
        requester_name: str = "",
        request_id: str | None = None,
        goal_id: str | None = None,
        icon: str = "package",
        status: str = "queued",
        env: str = "",
        delivery_kind: str = "",
    ) -> str:
        gid = str(group_id)
        now = clock.now()
        criteria_l = [str(c) for c in criteria]
        workspace = self._workspace_for(gid)
        with self._store.tx() as conn:
            tid = next_id(conn, "T")
            conn.execute(
                "INSERT INTO tasks (id, group_id, workspace, source, request_id, goal_id,"
                " requester_id, requester_name, icon, title, req, req_version, criteria,"
                " status, env, delivery_kind, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    tid, gid, workspace, str(source or ""), request_id, goal_id,
                    str(requester_id or ""), str(requester_name or ""), str(icon or "package"),
                    str(title), str(req or ""), json.dumps(criteria_l, ensure_ascii=False),
                    str(status), str(env or ""), str(delivery_kind or ""), now, now,
                ),
            )
            conn.execute(
                "INSERT INTO task_versions (task_id, version, req, criteria, ts) VALUES (?, 1, ?, ?, ?)",
                (tid, str(req or ""), json.dumps(criteria_l, ensure_ascii=False), now),
            )
            self._store.event(
                conn, f"task.{status}", group_id=gid, entity="task", entity_id=tid,
                payload={"title": str(title), "source": str(source or "")},
            )
        return tid

    def revise(self, task_id: str, *, req: str, criteria: Iterable[str]) -> int:
        """存一个新需求版本；任务在 running / reviewing 时排回 queued（旧尝试作废）。"""
        now = clock.now()
        criteria_l = [str(c) for c in criteria]
        with self._store.tx() as conn:
            row = conn.execute("SELECT status, req_version, group_id FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            v = int(row["req_version"]) + 1
            conn.execute(
                "UPDATE tasks SET req=?, criteria=?, req_version=?, updated=? WHERE id=?",
                (str(req or ""), json.dumps(criteria_l, ensure_ascii=False), v, now, str(task_id)),
            )
            conn.execute(
                "INSERT INTO task_versions (task_id, version, req, criteria, ts) VALUES (?, ?, ?, ?, ?)",
                (str(task_id), v, str(req or ""), json.dumps(criteria_l, ensure_ascii=False), now),
            )
            if str(row["status"]) in ("running", "reviewing"):
                conn.execute("UPDATE tasks SET status='queued', updated=? WHERE id=?", (now, str(task_id)))
                self._store.event(
                    conn, "task.queued", group_id=str(row["group_id"]), entity="task", entity_id=str(task_id),
                    payload={"reason": "需求改版，重新排队", "version": v},
                )
        return v

    # ------------------------------------------------------------------
    # transition
    # ------------------------------------------------------------------

    def transition(self, task_id: str, to: str, *, reason: str = "", **fields: Any) -> dict:
        to_s = str(to)
        unknown = set(fields) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"不能更新的字段: {sorted(unknown)}")
        if to_s not in _LEGAL:
            raise ValueError(f"不认识的任务状态「{to_s}」")
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            src = str(row["status"])
            if to_s not in _LEGAL.get(src, frozenset()):
                raise ValueError(f"任务 {task_id} 不能从「{src}」改成「{to_s}」")

            updates: dict[str, Any] = {"status": to_s, "updated": now}
            if to_s == "running" and row["started_ts"] is None:
                updates["started_ts"] = now
            if to_s in _TERMINAL:
                updates["finished_ts"] = now
            for key, value in fields.items():
                if key == "delivery" and isinstance(value, (list, tuple)):
                    updates[key] = json.dumps(list(value), ensure_ascii=False)
                elif key == "undelivered":
                    updates[key] = 1 if value else 0
                else:
                    updates[key] = value

            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", [*updates.values(), str(task_id)])
            payload: dict[str, Any] = {}
            if reason:
                payload["reason"] = str(reason)
            if fields:
                payload["fields"] = {
                    k: (json.loads(v) if k == "delivery" else v)
                    for k, v in updates.items() if k in fields
                }
            self._store.event(
                conn, f"task.{to_s}", group_id=str(row["group_id"]), entity="task", entity_id=str(task_id),
                payload=payload or None,
            )
        return self.get(task_id)  # type: ignore[return-value]

    def set_env(self, task_id: str, env_text: str, *, note: str = "") -> None:
        """把任务在哪做的（本机 / 一次性机器）写进 env 字段；事件的 payload 同时进网页时间线。

        - env_text 是给人看的一句（如「本机 · maiwork 用户 · 内存上限 512M」或
          「railway.new 一次性机器 · 2 核 2G · 到期 14:32」）；
        - note 非空时（如「一次性机器拿不到，改在本机做：原因」）追加进 env 字段，
          并以 task.env 事件落一条，管理员在任务详情的时间线里能看到。
        """
        text = str(env_text or "").strip()
        note_s = str(note or "").strip()
        if note_s and note_s not in text:
            text = f"{text}（{note_s}）" if text else note_s
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute("SELECT group_id FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            conn.execute("UPDATE tasks SET env=?, updated=? WHERE id=?", (text, now, str(task_id)))
            payload: dict[str, Any] = {"env": text}
            if note_s:
                payload["note"] = note_s
            self._store.event(
                conn, "task.env", group_id=str(row["group_id"]), entity="task",
                entity_id=str(task_id), payload=payload,
            )

    # ------------------------------------------------------------------
    # attempts
    # ------------------------------------------------------------------

    def start_attempt(self, task_id: str) -> int:
        """开一次新尝试：n = 当前尝试数 + 1，钉住当前 req_version；tasks.attempts 同步 +1。返回 n。"""
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute("SELECT req_version, attempts FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            n = int(row["attempts"]) + 1
            conn.execute(
                "INSERT INTO attempts (task_id, n, req_version, status, started) VALUES (?, ?, ?, 'running', ?)",
                (str(task_id), n, int(row["req_version"]), now),
            )
            conn.execute("UPDATE tasks SET attempts=?, updated=? WHERE id=?", (n, now, str(task_id)))
        return n

    def current_attempt_id(self, task_id: str) -> int | None:
        """当前那次尝试（n 最大）的行 id；还没有尝试则 None。"""
        row = self._store.read().execute(
            "SELECT id FROM attempts WHERE task_id=? ORDER BY n DESC LIMIT 1", (str(task_id),)
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        summary: str = "",
        evidence: Iterable[str] = (),
        artifacts: Iterable[str] = (),
        review: str = "",
    ) -> None:
        """回写一次尝试的结果。只更新尝试本身；愿不愿接收走 accept_result。"""
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE attempts SET status=?, summary=?, evidence=?, artifacts=?, review=?, finished=?"
                " WHERE id=?",
                (
                    str(status), str(summary or ""),
                    json.dumps([str(x) for x in evidence], ensure_ascii=False),
                    json.dumps([str(x) for x in artifacts], ensure_ascii=False),
                    str(review or ""), clock.now(), int(attempt_id),
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"尝试不存在: {attempt_id}")

    def accept_result(self, task_id: str, attempt_id: int, req_version: int) -> bool:
        """这次交回能不能算数。

        任务已取消 / 失败 / 完成（终态）、被拒、暂停，或者交回对应的 req_version
        和任务当前版本对不上 → False；同时把该 attempt 标 stale
        （历史保留，任务不复活、不再交付）。
        """
        with self._store.tx() as conn:
            row = conn.execute("SELECT status, req_version FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                return False
            status = str(row["status"])
            current_version = int(row["req_version"])
            if (
                status in _TERMINAL
                or status in ("paused", "failed")
                or int(req_version) != current_version
            ):
                conn.execute(
                    "UPDATE attempts SET status='stale', finished=COALESCE(finished, ?) WHERE id=?",
                    (clock.now(), int(attempt_id)),
                )
                return False
            return True

    # ------------------------------------------------------------------
    # 网页视图（docs/07 §9.3；字段名以前端为准）
    # ------------------------------------------------------------------

    def list_view(self, group_id: str) -> list[dict]:
        """tasks.list：不含 pending_approval（那些走待批列表）。按 updated 倒序，最多 50 条。"""
        rows = self._store.read().execute(
            "SELECT * FROM tasks WHERE group_id=? AND status != 'pending_approval'"
            " ORDER BY updated DESC LIMIT 50",
            (str(group_id),),
        ).fetchall()
        return [self._list_item(r) for r in rows]

    def detail_view(self, task_id: str, *, admin: bool) -> dict:
        """任务详情。群友版不含 env / timeline，只看得到 steps（步数）。

        G3：群友版还不含 tokens / workspace / source / request_id / requester_id——
        这些是实现细节（花了多少 token、工作区名、从哪来的），群友不需要；
        server 层群友分支会再剥一层兜底。
        """
        row = self._store.read().execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        item = self._list_item(row)
        item.update(
            {
                "req": str(row["req"]),
                "criteria": _load_json(row["criteria"], []),
                "review": str(row["review"] or ""),
                "delivery": _load_json(row["delivery"], []),
                "question": (str(row["question"]) if row["question"] is not None else None),
                "delivery_kind": str(row["delivery_kind"] or ""),
                "attempts": int(row["attempts"]),
                "tokens": int(row["tokens"]),
                "workspace": str(row["workspace"]),
                "source": str(row["source"] or ""),
                "request_id": (str(row["request_id"]) if row["request_id"] else None),
                "started_ts": row["started_ts"],
                "finished_ts": row["finished_ts"],
            }
        )
        calls = self._calls(str(task_id))
        item["steps"] = len(calls)
        if admin:
            item["env"] = str(row["env"] or "")
            item["timeline"] = calls
        else:
            # G3：群友看不到 token 数 / 工作区 / 来源 / 请求 ID / 发起人 QQ
            for key in ("tokens", "workspace", "source", "request_id", "requester_id"):
                item.pop(key, None)
        return item

    def running_count(self, group_id: str) -> int:
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE group_id=? AND status='running'",
            (str(group_id),),
        ).fetchone()
        return int(row["c"]) if row is not None else 0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _workspace_for(self, gid: str) -> str:
        try:
            settings = self._get_settings()
            fn = getattr(settings, "workspace_of", None)
            if callable(fn):
                return str(fn(gid))
        except Exception:
            pass
        return f"g{gid}"

    def _calls(self, task_id: str) -> list[dict]:
        if self._tools is None:
            return []
        try:
            calls = self._tools.recent_calls(task_id=task_id)
        except Exception:
            return []
        return [
            {
                "ts": float(c["ts"]),
                "actor": str(c["actor"]),
                "tool": str(c["tool"]),
                "input": str(c["input"]),
                "output": str(c["output"]),
                "ms": int(c["ms"]),
                "ok": bool(c["ok"]),
            }
            for c in calls
        ]

    @staticmethod
    def _meta(row: sqlite3.Row) -> str:
        """§9.3 的 meta：服务端拼好的一句概况。"""
        status = str(row["status"])
        now = clock.now()
        attempts = int(row["attempts"] or 0)
        if status == "running":
            mins = max(0, int((now - float(row["started_ts"] or now)) / 60))
            if mins < 1:
                return f"刚开始 · {attempts} 次尝试"
            return f"已跑 {mins} 分钟 · {attempts} 次尝试"
        if status == "waiting_input":
            question = str(row["question"] or "")[:30]
            qts = float(row["question_ts"] or now)
            hours = max(0, int((now - qts) / 3600))
            return f"等回答：{question} · 已问 {hours} 小时"
        if status == "shelved":
            return "已搁置，有人回复那条提问就恢复"
        if status == "reviewing":
            return "验收中"
        if status == "queued":
            return "排队中"
        if status == "paused":
            return "已暂停"
        if status == "cancelled":
            return "已取消"
        if status == "rejected":
            return "未批准"
        if status == "failed":
            reason = str(row["review"] or "")[:40]
            return f"失败：{reason}" if reason else "失败"
        if status == "completed":
            finished = row["finished_ts"]
            when = clock.bj(float(finished)).strftime("%m-%d %H:%M") if finished else ""
            base = f"完成于 {when}"
            if int(row["undelivered"] or 0):
                return f"{base} · 做完了但还没发出去"
            return base
        return ""

    def _list_item(self, row: sqlite3.Row) -> dict:
        return {
            "id": str(row["id"]),
            "icon": str(row["icon"] or "package"),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "meta": self._meta(row),
            "goal_id": (str(row["goal_id"]) if row["goal_id"] else None),
            "updated_ts": float(row["updated"]),
            "undelivered": bool(row["undelivered"]),
        }
