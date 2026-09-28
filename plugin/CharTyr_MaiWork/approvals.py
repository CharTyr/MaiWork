"""M3 数据层：派活待批请求（docs/02 §5、01 R6、07 §9.3 tasks.pending / §11.2）。

红线（02 §5）：群友派的活（任务、agent 目标、构想转成的活）按配置要 bot 管理员批准，
**未批准不开工**。免批的就直接落地。

规则：
- 免批条件：`approval.required=False`，或群号出现在 `exempt_groups`（配置里写 "qq:123" 或 "123" 都认），
  或发起人出现在 `exempt_users`。
- 非免批 → pending；approve/reject 只能处理 pending，重复操作抛 ValueError。
- 落地：kind="task" → Tasks.create(status="queued", source="request", request_id=…)，
  把 request.task_id 指回去；kind="goal" → Goals.create_agent(...)。
- pending 超 24 小时 → 提醒一次（记 reminded_ts，提醒过的不再提醒）；
  pending 超 7 天 → expired。
- 取消的权限：发起人、群主/群管理（group_role 由调用方从适配器拿）、bot 管理员。
"""

from __future__ import annotations

from typing import Any, Callable

from . import clock
from .goals import Goals
from .store import Store, next_id
from .tasks import Tasks

_KINDS = ("task", "goal")
_REMIND_AFTER_S = 24 * 3600
_EXPIRE_AFTER_S = 7 * 86400


def _norm(s: Any) -> str:
    return str(s or "").strip()


class Approvals:
    def __init__(self, store: Store, get_settings: Callable[[], Any], tasks: Tasks, goals: Goals) -> None:
        self._store = store
        self._get_settings = get_settings
        self._tasks = tasks
        self._goals = goals

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------

    def is_admin(self, user_id: Any, platform: str = "qq") -> bool:
        """按「平台:账号」比对（MaiBot 标准写法）；目前服务的都是 qq 群，platform 默认 qq。"""
        from .config import norm_account

        uid = _norm(user_id)
        if not uid:
            return False
        target = norm_account(f"{platform or 'qq'}:{uid}")
        return bool(target) and any(norm_account(a) == target for a in self._approval().admins)

    def _approval(self) -> Any:
        return self._get_settings().approval

    def _is_auto(self, group_id: str, requester_id: Any) -> bool:
        approval = self._approval()
        if not bool(getattr(approval, "required", True)):
            return True
        from .config import norm_account

        gkey = norm_account(f"qq:{_norm(group_id)}")
        if gkey and any(norm_account(g) == gkey for g in getattr(approval, "exempt_groups", ())):
            return True
        rid = _norm(requester_id)
        ukey = norm_account(f"qq:{rid}") if rid else ""
        if ukey and any(norm_account(u) == ukey for u in getattr(approval, "exempt_users", ())):
            return True
        return False

    # ------------------------------------------------------------------
    # create / approve / reject
    # ------------------------------------------------------------------

    def create(
        self,
        group_id: str,
        *,
        kind: str,
        title: str,
        quote: str,
        via: str,
        requester_id: str,
        requester_name: str,
        message_id: str = "",
        idea_id: int | None = None,
        icon: str = "magnifier",
    ) -> dict:
        kind_s = _norm(kind) or "task"
        if kind_s not in _KINDS:
            raise ValueError(f"不认识的请求类型「{kind}」（只能是 task / goal）")
        gid = _norm(group_id)
        now = clock.now()
        auto = self._is_auto(gid, requester_id)
        with self._store.tx() as conn:
            rid = next_id(conn, "R")
            conn.execute(
                "INSERT INTO requests (id, group_id, kind, title, quote, via, icon,"
                " requester_id, requester_name, message_id, idea_id, status, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid, gid, kind_s, _norm(title), str(quote or ""), str(via or ""),
                    _norm(icon) or "magnifier", _norm(requester_id), _norm(requester_name),
                    _norm(message_id), (int(idea_id) if idea_id is not None else None),
                    "approved" if auto else "pending", now, now,
                ),
            )
            self._store.event(
                conn, "request.approved" if auto else "request.pending",
                group_id=gid, entity="request", entity_id=rid,
                payload={"title": _norm(title), "kind": kind_s, "auto": auto},
            )
        if auto:
            return self._land(rid, by="自动批准", auto=True)
        return {"id": rid, "status": "pending", "auto": None}

    def approve(self, request_id: str, *, by: str) -> dict:
        rid = _norm(request_id)
        row = self._get(rid)
        if row is None:
            raise KeyError(f"请求不存在: {request_id}")
        if str(row["status"]) != "pending":
            raise ValueError(f"请求 {rid} 已经在「{row['status']}」状态，不能再批准")
        now = clock.now()
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE requests SET status='approved', decided_by=?, decided_ts=?, updated=? WHERE id=?",
                (_norm(by), now, now, rid),
            )
            self._store.event(
                conn, "request.approved", group_id=str(row["group_id"]),
                entity="request", entity_id=rid, payload={"by": _norm(by)},
            )
        return self._land(rid, by=_norm(by), auto=False)

    def reject(self, request_id: str, *, by: str) -> dict:
        rid = _norm(request_id)
        row = self._get(rid)
        if row is None:
            raise KeyError(f"请求不存在: {request_id}")
        if str(row["status"]) != "pending":
            raise ValueError(f"请求 {rid} 已经在「{row['status']}」状态，不能再拒绝")
        now = clock.now()
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE requests SET status='rejected', decided_by=?, decided_ts=?, updated=? WHERE id=?",
                (_norm(by), now, now, rid),
            )
            self._store.event(
                conn, "request.rejected", group_id=str(row["group_id"]),
                entity="request", entity_id=rid, payload={"by": _norm(by)},
            )
            if row["idea_id"] is not None:
                # 来自构想的请求被拒：构想回到「新想法」，别一直挂着「等批准」
                conn.execute(
                    "UPDATE ideas SET state='new', requested_by=NULL, updated=? WHERE id=? AND state IN ('wanted', 'pending')",
                    (now, int(row["idea_id"])),
                )
        return {"id": rid, "status": "rejected", "auto": False, "task_id": None, "goal_id": None}

    def _land(self, request_id: str, *, by: str, auto: bool) -> dict:
        """把已批准的请求落成 task / goal；把 request.task_id / goal_id 指回去。"""
        row = self._get(request_id)
        assert row is not None
        kind = str(row["kind"])
        gid = str(row["group_id"])
        requester_name = str(row["requester_name"])
        by_text = f"{requester_name} 发起 · {'自动批准' if auto else f'{by} 批准'}"
        if row["idea_id"] is not None:
            by_text = f"{by_text} · 来自构想"

        task_id = None
        goal_id = None
        if kind == "task":
            task_id = self._tasks.create(
                gid,
                title=str(row["title"]),
                req=str(row["quote"] or row["title"]),
                criteria=[],
                source="request",
                requester_id=str(row["requester_id"]),
                requester_name=requester_name,
                request_id=request_id,
                icon=str(row["icon"] or "package"),
                status="queued",
            )
        else:  # goal
            goal_id = self._goals.create_agent(
                gid,
                title=str(row["title"]),
                body=str(row["quote"] or ""),
                criteria=[],
                by_text=by_text,
                request_id=request_id,
                icon=str(row["icon"] or "bullseye"),
            )
        now = clock.now()
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE requests SET task_id=?, goal_id=?, updated=? WHERE id=?",
                (task_id, goal_id, now, request_id),
            )
            if row["idea_id"] is not None and task_id is not None:
                # 来自构想的请求被批准（手动批准路径）：构想也要标 started 并回写 task_id。
                # 目前免批（app.on_idea_want）和网页「做这个」（app._on_idea_started）已经
                # 各自回写；唯独手动批准漏了——这里是统一兜底（只动 pending/wanted/new 的，
                # 不覆盖已有 started 构想，防重复批准时乱指）。
                conn.execute(
                    "UPDATE ideas SET state='started', task_id=?, updated=?"
                    " WHERE id=? AND state IN ('new', 'wanted', 'pending')",
                    (task_id, now, int(row["idea_id"])),
                )
        return {
            "id": request_id, "status": "approved", "auto": auto,
            "task_id": task_id, "goal_id": goal_id,
        }

    # ------------------------------------------------------------------
    # 网页视图（docs/07 §9.3 tasks.pending）
    # ------------------------------------------------------------------

    def pending_view(self, group_id: str) -> list[dict]:
        """待批队列，旧的在前。"""
        rows = self._store.read().execute(
            "SELECT * FROM requests WHERE group_id=? AND status='pending' ORDER BY created ASC, id ASC",
            (_norm(group_id),),
        ).fetchall()
        now = clock.now()
        return [
            {
                "id": str(r["id"]),
                "icon": str(r["icon"] or "magnifier"),
                "title": str(r["title"]),
                "who": str(r["requester_name"]),
                "ts": float(r["created"]),
                "quote": str(r["quote"] or ""),
                "via": str(r["via"] or ""),
                "age_s": max(0, int(now - float(r["created"]))),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 提醒 / 过期
    # ------------------------------------------------------------------

    def due_reminders(self, now: float) -> list[dict]:
        """pending 超 24 小时且还没提醒过的；旧在最前。"""
        rows = self._store.read().execute(
            "SELECT * FROM requests WHERE status='pending' AND reminded_ts IS NULL"
            " AND created <= ? ORDER BY created ASC",
            (float(now) - _REMIND_AFTER_S,),
        ).fetchall()
        return [
            {
                "id": str(r["id"]),
                "group_id": str(r["group_id"]),
                "title": str(r["title"]),
                "who": str(r["requester_name"]),
                "ts": float(r["created"]),
                "age_s": max(0, int(float(now) - float(r["created"]))),
            }
            for r in rows
        ]

    def mark_reminded(self, request_id: str, now: float) -> None:
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE requests SET reminded_ts=?, updated=? WHERE id=?",
                (float(now), float(now), _norm(request_id)),
            )

    def expire(self, now: float) -> list[str]:
        """pending 超 7 天 → expired；返回这些请求的 id。"""
        rows = self._store.read().execute(
            "SELECT id, group_id FROM requests WHERE status='pending' AND created <= ?",
            (float(now) - _EXPIRE_AFTER_S,),
        ).fetchall()
        ids = [str(r["id"]) for r in rows]
        if not ids:
            return []
        with self._store.tx() as conn:
            for r in rows:
                conn.execute(
                    "UPDATE requests SET status='expired', updated=? WHERE id=?",
                    (float(now), str(r["id"])),
                )
                self._store.event(
                    conn, "request.expired", group_id=str(r["group_id"]),
                    entity="request", entity_id=str(r["id"]),
                )
        return ids

    # ------------------------------------------------------------------
    # 取消的权限（发起人 / 群主 / 群管理 / bot 管理员；网页上由管理员账号登入，另行校验）
    # ------------------------------------------------------------------

    def can_cancel(self, kind: str, obj_id: str, user_id: Any, *, group_role: str = "") -> bool:
        uid = _norm(user_id)
        if not uid:
            return False
        if self.is_admin(uid):
            return True
        if _norm(group_role) in ("owner", "admin"):
            return True
        obj = self._find(kind, obj_id)
        if obj is None:
            return False
        if kind == "task":
            return _norm(obj.get("requester_id")) == uid
        # goal：agent 目标用 who_id 记发起人；成员目标是本人的事，本人可取消
        return _norm(obj.get("who_id")) == uid

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get(self, request_id: str) -> dict | None:
        row = self._store.read().execute(
            "SELECT * FROM requests WHERE id=?", (_norm(request_id),)
        ).fetchone()
        return {k: row[k] for k in row.keys()} if row is not None else None

    def _find(self, kind: str, obj_id: str) -> dict | None:
        table = "tasks" if kind == "task" else "goals"
        row = self._store.read().execute(
            f"SELECT * FROM {table} WHERE id=?", (_norm(obj_id),)
        ).fetchone()
        return {k: row[k] for k in row.keys()} if row is not None else None
