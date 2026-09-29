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
- 自动审核（auto_review.py，2026-10）：落了 pending 就回调 `set_review_hook` 注入的函数，
  由 app 把「主模型看一眼该不该直接批」spawn 到后台；批通过走 `approve(by="MaiWork 自动审核",
  auto_reason=…)`——和人批同一条落地路径。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Iterable

from . import clock
from .goals import Goals
from .store import Store, next_id
from .tasks import Tasks

logger = logging.getLogger("maiwork.approvals")

_KINDS = ("task", "goal")
_REMIND_AFTER_S = 24 * 3600
_EXPIRE_AFTER_S = 7 * 86400


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _row_get(row: Any, key: str, default: Any = "") -> Any:
    """行里有这个列就取，没有（老库 schema / 测试假行）→ 默认值，不抛。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _norm_item_nos(items: Any) -> list[int]:
    """项目序号规范化：正的整数、去重、保序；别的（None / 空 / 乱值）→ []。"""
    if not isinstance(items, (list, tuple)):
        return []
    out: list[int] = []
    for x in items:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def _parse_item_nos(raw: Any) -> list[int]:
    """读请求行里的 item_nos（JSON 串）→ [int]；坏数据按 []（= 全部项目）处理。"""
    if isinstance(raw, list):
        return _norm_item_nos(raw)
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return _norm_item_nos(data)


class Approvals:
    def __init__(self, store: Store, get_settings: Callable[[], Any], tasks: Tasks, goals: Goals) -> None:
        self._store = store
        self._get_settings = get_settings
        self._tasks = tasks
        self._goals = goals
        # 自动审核回调（auto_review.py；app 注入）：刚记下一条要人批的请求 → hook(请求 id, 群号)。
        # 只登记、不在这里做慢活（审核是异步的，绝不卡住收消息钩子）。
        self._review_hook: Callable[[str, str], Any] | None = None

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
        items: list[int] | None = None,
        source: str = "",
        force_manual: bool = False,
    ) -> dict:
        """记一条待批请求。

        - `items`：从构想转来的请求里，群友点名要做的项目序号（None / [] = 全部项目）；
        - `source`：来源标记（`"idea"` = 来自构想、`"maiwork"` = MaiWork 主动提议、空 = 群友 @）；
        - `force_manual=True`：**永远**要管理员批准（免批群 / 免批人 / required=False 一律不生效）。
          MaiWork 自己提的目标走这条（红线：MaiWork 不能替管理员拍板立目标）。

        落了 pending 之后会通知自动审核（`set_review_hook` 注入的回调，见 auto_review.py）：
        回调只登记，慢的判断在后台协程里做，create 立刻返回。
        """
        kind_s = _norm(kind) or "task"
        if kind_s not in _KINDS:
            raise ValueError(f"不认识的请求类型「{kind}」（只能是 task / goal）")
        gid = _norm(group_id)
        now = clock.now()
        auto = False if force_manual else self._is_auto(gid, requester_id)
        item_nos = _norm_item_nos(items)
        with self._store.tx() as conn:
            rid = next_id(conn, "R")
            conn.execute(
                "INSERT INTO requests (id, group_id, kind, title, quote, via, icon,"
                " requester_id, requester_name, message_id, idea_id, item_nos, source,"
                " force_manual, status, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid, gid, kind_s, _norm(title), str(quote or ""), str(via or ""),
                    _norm(icon) or "magnifier", _norm(requester_id), _norm(requester_name),
                    _norm(message_id), (int(idea_id) if idea_id is not None else None),
                    json.dumps(item_nos, ensure_ascii=False), _norm(source),
                    1 if force_manual else 0,
                    "approved" if auto else "pending", now, now,
                ),
            )
            self._store.event(
                conn, "request.approved" if auto else "request.pending",
                group_id=gid, entity="request", entity_id=rid,
                payload={"title": _norm(title), "kind": kind_s, "auto": auto, "source": _norm(source)},
            )
            # 免批路径也必须让请求与其产出的所有任务/目标同生共死。
            landed = self._land(conn, self._request_row(conn, rid), by="自动批准", auto=True) if auto else None
        if auto:
            assert landed is not None
            return landed
        self._notify_review(rid, gid)
        return {"id": rid, "status": "pending", "auto": None}

    def set_review_hook(self, hook: Callable[[str, str], Any] | None) -> None:
        """接上「刚记下一条待批请求」的回调（app 注入；hook(请求 id, 群号)）。

        没有钩子（测试 / 模块没就位）→ 待批请求照旧等人批，什么都不发生。
        """
        self._review_hook = hook

    def _notify_review(self, request_id: str, group_id: str) -> None:
        """只登记，绝不在这里做慢活（自动审核要调模型，必须由调用方 spawn 到后台）。"""
        hook = self._review_hook
        if hook is None:
            return
        try:
            hook(str(request_id), str(group_id))
        except Exception:
            logger.debug("自动审核回调出错（请求 %s），这条留给人批", request_id, exc_info=True)

    def approve(
        self, request_id: str, *, by: str, auto_reason: str = "",
        auto_daily: tuple[str, int] | None = None,
    ) -> dict:
        """Approve and land as one transaction. Auto review additionally reserves its daily slot.

        ``auto_daily=(Beijing day, cap)`` is for auto review only: quota check,
        request decision, all work creation and counter increment commit together.
        """
        rid = _norm(request_id)
        now = clock.now()
        reason_s = _norm(auto_reason)
        with self._store.tx() as conn:
            row = self._request_row(conn, rid)
            if row is None:
                raise KeyError(f"请求不存在: {request_id}")
            if str(row["status"]) != "pending":
                raise ValueError(f"请求 {rid} 已经在「{row['status']}」状态，不能再批准")
            quota: tuple[str, str, int] | None = None
            if auto_daily is not None:
                day, cap = auto_daily
                if str(row["kind"]) != "task" or row["force_manual"] or row["source"] == "maiwork":
                    raise ValueError("这条请求必须由管理员批准")
                if row["idea_id"] is not None and any(
                    it["kind"] == "goal" for it in self.picked_idea_items(row)
                ):
                    raise ValueError("包含目标的构想必须由管理员批准")
                gid = str(row["group_id"])
                key = f"auto_review.day.{gid}"
                old = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                try:
                    counter = json.loads(old["value"]) if old is not None else None
                    n = max(0, int(counter.get("n") or 0)) if isinstance(counter, dict) and counter.get("day") == day else 0
                except (TypeError, ValueError):
                    n = 0
                if not gid or cap <= 0 or n >= cap:
                    raise ValueError("这个群今天的自动批准额度已满")
                quota = (key, day, n + 1)
            conn.execute(
                "UPDATE requests SET status='approved', decided_by=?, decided_ts=?,"
                " auto_reason=?, updated=? WHERE id=?",
                (_norm(by), now, reason_s, now, rid),
            )
            payload: dict[str, Any] = {"by": _norm(by)}
            if reason_s:
                payload["auto_reason"] = reason_s
            self._store.event(
                conn, "request.approved", group_id=str(row["group_id"]),
                entity="request", entity_id=rid, payload=payload,
            )
            landed = self._land(conn, row, by=_norm(by), auto=False)
            if quota is not None:
                key, day, n = quota
                self._store.kv_set(conn, key, {"day": day, "n": n})
        return landed

    def reject(self, request_id: str, *, by: str) -> dict:
        rid = _norm(request_id)
        now = clock.now()
        with self._store.tx() as conn:
            row = self._request_row(conn, rid)
            if row is None:
                raise KeyError(f"请求不存在: {request_id}")
            if str(row["status"]) != "pending":
                raise ValueError(f"请求 {rid} 已经在「{row['status']}」状态，不能再拒绝")
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

    def _land(self, conn: sqlite3.Connection, row: dict, *, by: str, auto: bool) -> dict:
        """在批准请求的事务中创建所有 task / goal 并回写引用。

        构想选中的项目逐个建，任何一个失败即回滚整条请求与全部项目。
        """
        request_id = str(row["id"])
        kind = str(row["kind"])
        gid = str(row["group_id"])
        requester_name = str(row["requester_name"])
        by_text = f"{requester_name} 发起 · {'自动批准' if auto else f'{by} 批准'}"
        if row["idea_id"] is not None:
            by_text = f"{by_text} · 来自构想"

        task_ids: list[str] = []
        goal_ids: list[str] = []
        idea = self._idea_of_request(row)
        picked = self.picked_idea_items(row)
        if picked:
            ctx = self._idea_context(idea or {})
            for it in picked:
                if it["kind"] == "goal":
                    goal_ids.append(
                        self._goals.create_agent(
                            gid,
                            title=it["title"],
                            body=self._join_req(it["desc"], ctx),
                            criteria=[],
                            by_text=by_text,
                            request_id=request_id,
                            icon=str(row["icon"] or "bullseye"),
                            conn=conn,
                        )
                    )
                else:
                    task_ids.append(
                        self._tasks.create(
                            gid,
                            title=it["title"],
                            req=self._join_req(it["desc"], ctx),
                            criteria=[],
                            source="request",
                            requester_id=str(row["requester_id"]),
                            requester_name=requester_name,
                            request_id=request_id,
                            icon=str(row["icon"] or "package"),
                            status="queued",
                            conn=conn,
                        )
                    )
        elif kind == "task":
            task_ids.append(
                self._tasks.create(
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
                    conn=conn,
                )
            )
        else:  # goal
            goal_ids.append(
                self._goals.create_agent(
                    gid,
                    title=str(row["title"]),
                    body=str(row["quote"] or ""),
                    criteria=[],
                    by_text=by_text,
                    request_id=request_id,
                    icon=str(row["icon"] or "bullseye"),
                    conn=conn,
                )
            )
        task_id = task_ids[0] if task_ids else None
        goal_id = goal_ids[0] if goal_ids else None
        now = clock.now()
        conn.execute(
            "UPDATE requests SET task_id=?, goal_id=?, task_ids=?, updated=? WHERE id=?",
            (task_id, goal_id, json.dumps(task_ids, ensure_ascii=False), now, request_id),
        )
        if row["idea_id"] is not None and (task_id is not None or goal_id is not None):
            # 保留已 started 的构想原指向；其它状态和请求一起提交。
            conn.execute(
                "UPDATE ideas SET state='started', task_id=?, updated=?"
                " WHERE id=? AND state IN ('new', 'wanted', 'pending')",
                (task_id, now, int(row["idea_id"])),
            )
        return {
            "id": request_id, "status": "approved", "auto": auto,
            "task_id": task_id, "goal_id": goal_id,
            "task_ids": task_ids, "goal_ids": goal_ids,
        }

    def _idea_of_request(self, row: dict) -> dict | None:
        """请求带的 idea_id 对应的构想行（含 items）；没有 / 查不到 → None。"""
        if row.get("idea_id") is None:
            return None
        try:
            got = self._store.read().execute(
                "SELECT id, title, body, items FROM ideas WHERE id=?", (int(row["idea_id"]),)
            ).fetchone()
        except Exception:
            return None
        return {k: got[k] for k in got.keys()} if got is not None else None

    def picked_idea_items(self, row: dict) -> list[dict]:
        """这条请求（requests 行）从构想里点名选中的项目；不来自构想 / 没项目 → []。

        公开给自动审核用（auto_review.py 判「选中的项目里有没有 goal」）；落地 `_land`
        也走这一个口径。
        """
        idea = self._idea_of_request(row)
        if not idea:
            return []
        return self._picked_idea_items(idea, _parse_item_nos(row.get("item_nos")))

    @staticmethod
    def _picked_idea_items(idea: dict, item_nos: list[int]) -> list[dict]:
        """构想里被点名的项目（按序号 1 起）；item_nos 空 = 全部。

        序号一个都没对上（比如写了 9、但这条构想只有 3 项）→ 当「全部」处理：写错序号
        不该变成什么都不做，也不该拿请求标题硬凑一个。反正落地前有人工批准这一关。
        """
        from .feeds import parse_idea_items

        items = parse_idea_items(idea.get("items"))
        if not items:
            return []
        if not item_nos:
            return items
        picked = [it for i, it in enumerate(items, start=1) if i in item_nos]
        return picked or items

    @staticmethod
    def _idea_context(idea: dict) -> str:
        title = str(idea.get("title") or "").strip()
        body = str(idea.get("body") or "").strip()
        ctx = f"来自构想 #{int(idea['id'])}《{title}》"
        if body:
            ctx = f"{ctx}：{body}"
        return ctx

    @staticmethod
    def _join_req(desc: str, ctx: str) -> str:
        d = str(desc or "").strip()
        return f"{d}\n\n{ctx}".strip() if d else ctx

    def group_of(self, request_id: str) -> str | None:
        """这个待批请求属于哪个群；没有这条 → None（网页 / 群指令鉴权用）。"""
        row = self._get(_norm(request_id))
        return str(row["group_id"]) if row is not None else None

    # ------------------------------------------------------------------
    # 网页视图（docs/07 §9.3 tasks.pending）
    # ------------------------------------------------------------------

    def pending_view(self, group_id: str) -> list[dict]:
        """待批队列，旧的在前。

        `approved_by` / `auto_reason` 是批准信息契约（docs/07 §9.3）：待批的当然是空串，
        自动审核通过的行 decided_by 是「MaiWork 自动审核」、auto_reason 是一句话理由
        （任务视图用 `auto_info_by_task` 拿这两个字段）。
        """
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
                "source": str(_row_get(r, "source") or ""),
                "items": _parse_item_nos(_row_get(r, "item_nos")),
                "idea_id": (int(r["idea_id"]) if _row_get(r, "idea_id", None) is not None else None),
                "age_s": max(0, int(now - float(r["created"]))),
                "approved_by": str(_row_get(r, "decided_by") or ""),
                "auto_reason": str(_row_get(r, "auto_reason") or ""),
            }
            for r in rows
        ]

    def auto_info_by_task(self, task_ids: Iterable[str]) -> dict[str, dict]:
        """任务 id → 批准信息：`{"approved_by": str, "auto_reason": str}`（网页任务视图用）。

        - 自动审核通过的任务：approved_by = 「MaiWork 自动审核」，auto_reason = 一句话理由，
          前端显示「自动审核通过：<理由>」；
        - 人批的任务：approved_by 是批准人（如「网页管理员」/QQ 号），auto_reason 空串；
        - 免批直接落地的任务：没走 approve，两个字段都没有 → 这个任务不在结果里；
        - 一条请求拆成多个任务落地（构想按项目建）时，**每个**任务都查得到——按
          requests.task_ids（JSON 数组）反查；老数据没有这一列 / 是空的 → 回落 task_id；
        - 不知道的任务 id 也安静跳过（不抛）。
        """
        ids = [str(t) for t in (task_ids or []) if str(t)]
        if not ids:
            return {}
        out: dict[str, dict] = {}
        id_set = set(ids)
        try:
            holders = ",".join("?" for _ in ids)
            rows = self._store.read().execute(
                f"SELECT id, task_id, task_ids, decided_by, auto_reason FROM requests"
                f" WHERE task_id IN ({holders})"
                f" OR (task_ids NOT IN ('', '[]') AND (decided_by != '' OR auto_reason != ''))",
                tuple(ids),
            ).fetchall()
        except Exception:
            logger.debug("读任务的批准信息失败", exc_info=True)
            return {}
        for r in rows:
            by = str(_row_get(r, "decided_by", "") or "")
            reason = str(_row_get(r, "auto_reason", "") or "")
            if not by and not reason:
                continue
            info = {"approved_by": by, "auto_reason": reason}
            hit: list[str] = []
            first = str(_row_get(r, "task_id", "") or "")
            if first:
                hit.append(first)
            raw = _row_get(r, "task_ids", "")
            if isinstance(raw, (list, tuple)):
                listed = [str(x) for x in raw]
            else:
                try:
                    parsed = json.loads(str(raw or "") or "[]")
                except (TypeError, ValueError):
                    parsed = []
                listed = [str(x) for x in parsed] if isinstance(parsed, list) else []
            hit.extend(listed)
            for tid in dict.fromkeys(hit):  # 去重保序；只回这次问到的任务
                if tid in id_set:
                    out[tid] = info
        return out

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

    @staticmethod
    def _request_row(conn: sqlite3.Connection, request_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (_norm(request_id),)).fetchone()
        return {k: row[k] for k in row.keys()} if row is not None else None

    def _get(self, request_id: str) -> dict | None:
        return self._request_row(self._store.read(), request_id)

    def _find(self, kind: str, obj_id: str) -> dict | None:
        table = "tasks" if kind == "task" else "goals"
        row = self._store.read().execute(
            f"SELECT * FROM {table} WHERE id=?", (_norm(obj_id),)
        ).fetchone()
        return {k: row[k] for k in row.keys()} if row is not None else None
