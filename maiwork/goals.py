"""M3 数据层：目标（docs/02 §4.3、01 R5、07 §9.3 goals / §11.2）。

agent 目标（G-n）：群请 MaiWork 盯着或做出来的事，按完成标准验收，闲时静默推进。
成员目标（M-n）：群友自己的事，记下、到时提醒、到期问进展；循环类默认 30 天后停。

规则要点：
- 停机错过的提醒只补一次，不连发；恢复后直接跳到未来的下一次。
- repeat="daily" 的成员目标在 until_ts 之外不再提醒；到 until_ts-1 天没问过就发一次「要不要续」。
- 每个目标 last_ts / last_text 记最近一次实质动作（汇报、问进展、问续期）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from typing import Any, Callable, Iterable

from . import clock, members
from .store import Store, next_id

_AGENT_NEXT_CHECK_S = 3600
_REPEAT_UNTIL_S = 30 * 86400
_RENEW_BEFORE_S = 1 * 86400
_DONE_KEEP_S = 3 * 86400

# docs/22 §5 C（2026-10-07 本地）：目标打勾要证据——evidence 引用本次提示给出的群聊行时，
# 去空白后连续这么多字相同才算「群里真发生了」。criteria 里存的 evidence 上限。
GOAL_EVIDENCE_MIN = 8
GOAL_EVIDENCE_MAX = 200


def _chat_line_body(line: Any) -> str:
    """群聊提示行「- 谁：正文」→ 正文（没有冒号就整行）。"""
    text = str(line or "").strip()
    if text.startswith("-"):
        text = text[1:].strip()
    for sep in ("：", ":"):
        if sep in text:
            return text.split(sep, 1)[1]
    return text


def chat_evidence_match(evidence: Any, chat_lines: Any, *, min_len: int = GOAL_EVIDENCE_MIN) -> bool:
    """docs/22 §5 C：(b) 证据里含有本次提示给出的群聊行正文片段（去空白后连续 ≥8 字）。

    纯函数、可单测：不看任务、不改状态。`chat_lines` 是 `_goal_chat_lines` 的输出
    （每条形如「- 谁：正文」），比对前把两边空白全去掉。
    """
    ev = "".join(str(evidence or "").split())
    if not ev:
        return False
    try:
        n = max(1, int(min_len))
    except (TypeError, ValueError):
        n = GOAL_EVIDENCE_MIN
    for line in chat_lines if isinstance(chat_lines, (list, tuple)) else []:
        body = "".join(_chat_line_body(line).split())
        if len(body) < n:
            continue
        for i in range(len(body) - n + 1):
            if body[i:i + n] in ev:
                return True
    return False


_AGENT_STATES = ("active", "paused", "done", "cancelled")
_ALLOWED_STATE_CHANGES = {
    "active": frozenset({"paused", "done", "cancelled"}),
    "paused": frozenset({"active", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}

# 报平安（docs/02 §4.3）：每次检查写 heartbeat_ts；连续 3 次出错、或超过
# 2 倍检查间隔没心跳 → stale_reason。模型没配好不算卡住，只标「暂停检查」。
STALE_TIMEOUT_REASON = "超过预定检查时间还没动静"
STALE_MODEL_REASON = "模型没配好，暂停检查"
_STALE_ERROR_PREFIX = "连续 3 次检查出错："
_FAIL_THRESHOLD = 3
_FAIL_ERROR_LEN = 60


def stale_is_blocking(reason: str) -> bool:
    """这条 stale_reason 算不算「卡住」（模型没配好的不算，只是暂停检查）。"""
    r = str(reason or "")
    return bool(r) and r != STALE_MODEL_REASON


class Goals:
    def __init__(self, store: Store, get_settings: Callable[[], Any]) -> None:
        self._store = store
        self._get_settings = get_settings

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, goal_id: str) -> dict | None:
        row = self._store.read().execute(
            "SELECT * FROM goals WHERE id=?", (str(goal_id),)
        ).fetchone()
        return {k: row[k] for k in row.keys()} if row is not None else None

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create_agent(
        self,
        group_id: str,
        *,
        title: str,
        body: str,
        criteria: Iterable[str],
        by_text: str,
        request_id: str | None = None,
        icon: str = "bullseye",
        conn: sqlite3.Connection | None = None,
        who_id: str = "",
        who_name: str = "",
    ) -> str:
        """Create an agent goal; an optional caller-owned connection joins its transaction.

        who_id / who_name = 发起人（群友派的活批准落地时传；管理员直接立的留空）。
        `/mw 取消` 的「发起人可以取消」只认 who_id（approvals.can_cancel）。
        """
        now = clock.now()
        crit = [{"text": str(c), "done": False} for c in criteria]
        with (self._store.tx() if conn is None else nullcontext(conn)) as conn:
            gid = next_id(conn, "G")
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, icon, title, body, who_id, who_name, by_text, criteria,"
                " state, next_check_ts, request_id, created, updated)"
                " VALUES (?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    gid, str(group_id), str(icon or "bullseye"), str(title), str(body or ""),
                    str(who_id or ""), str(who_name or ""),
                    str(by_text or ""), json.dumps(crit, ensure_ascii=False),
                    now + _AGENT_NEXT_CHECK_S, request_id, now, now,
                ),
            )
            self._store.event(
                conn, "goal.created", group_id=str(group_id), entity="goal", entity_id=gid,
                payload={"kind": "agent", "title": str(title)},
            )
        return gid

    def create_member(
        self,
        group_id: str,
        *,
        who_id: str,
        who_name: str,
        title: str,
        due_ts: float | None,
        remind_ts: float | None,
        repeat: str | None = None,
    ) -> str:
        now = clock.now()
        repeat_s = str(repeat).strip() if repeat else None
        until = (now + _REPEAT_UNTIL_S) if repeat_s else None
        with self._store.tx() as conn:
            mid = next_id(conn, "M")
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, icon, title, who_id, who_name,"
                " state, due_ts, remind_ts, repeat, until_ts, created, updated)"
                " VALUES (?, ?, 'member', 'alarm', ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (
                    mid, str(group_id), str(title), str(who_id or ""), str(who_name or ""),
                    (float(due_ts) if due_ts is not None else None),
                    (float(remind_ts) if remind_ts is not None else None),
                    repeat_s, until, now, now,
                ),
            )
            self._store.event(
                conn, "goal.created", group_id=str(group_id), entity="goal", entity_id=mid,
                payload={"kind": "member", "title": str(title), "who": str(who_name or "")},
            )
        return mid

    # ------------------------------------------------------------------
    # due / mark
    # ------------------------------------------------------------------

    def due(self, now: float) -> list[dict]:
        """到点的事：remind（提醒）、ask_progress（到期问进展）、check（agent 目标检查）、renew（问续期）。"""
        rows = self._store.read().execute(
            "SELECT * FROM goals WHERE state='active' ORDER BY created ASC, id ASC"
        ).fetchall()
        out: list[dict] = []
        for raw in rows:
            g = {k: raw[k] for k in raw.keys()}
            kind = str(g["kind"])
            if kind == "member":
                if g["remind_ts"] is not None and float(g["remind_ts"]) <= now:
                    out.append({"type": "remind", "goal": self._member_dict(g)})
                elif g["due_ts"] is not None and float(g["due_ts"]) <= now and g["last_ts"] is None:
                    out.append({"type": "ask_progress", "goal": self._member_dict(g)})
                if (
                    g["repeat"] is not None and g["until_ts"] is not None
                    and float(g["until_ts"]) - _RENEW_BEFORE_S <= now
                    and g["last_ts"] is None
                ):
                    out.append({"type": "renew", "goal": self._member_dict(g)})
            else:  # agent
                if g["next_check_ts"] is not None and float(g["next_check_ts"]) <= now:
                    out.append({"type": "check", "goal": self._agent_dict(g)})
        return out

    def mark(
        self,
        goal_id: str,
        type: str,
        now: float,
        next_remind_ts: float | None = None,
    ) -> None:
        """记录这次处理过；按 type 推进下一次的时间点。

        - remind + daily：remind_ts = max(原 remind_ts+1 天, now+1 天)（错过的只补一次）；
          如果已经超过 until_ts，目标直接 state="done"。
        - remind + 非循环：remind_ts = None。
        - ask_progress / renew：用 last_ts 标记「问过」。
        - check：next_check_ts = next_remind_ts（缺省 now+1 小时）。
        """
        t = str(type)
        with self._store.tx() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id=?", (str(goal_id),)).fetchone()
            if row is None:
                raise KeyError(f"目标不存在: {goal_id}")
            # 巡检的结果可能比管理员的暂停/取消晚到，不能再推进它。
            if str(row["state"]) != "active":
                return
            updates: dict[str, Any] = {"updated": float(now)}
            if t == "remind":
                kind = str(row["kind"])
                repeat = row["repeat"]
                until = row["until_ts"]
                if kind == "member" and repeat:
                    nxt = max(float(row["remind_ts"] or now) + 86400, float(now) + 86400)
                    if until is not None and float(now) > float(until):
                        updates.update(state="done", remind_ts=None)
                    else:
                        updates["remind_ts"] = nxt
                else:
                    updates["remind_ts"] = None
            elif t in ("ask_progress", "renew"):
                updates["last_ts"] = float(now)
            elif t == "check":
                updates["next_check_ts"] = float(next_remind_ts) if next_remind_ts is not None else float(now) + _AGENT_NEXT_CHECK_S
            else:
                raise ValueError(f"不认识的处理类型「{type}」")
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE goals SET {cols} WHERE id=?", [*updates.values(), str(goal_id)])

    # ------------------------------------------------------------------
    # 生命周期 / 记进展
    # ------------------------------------------------------------------

    def _set_state(self, goal_id: str, state: str) -> None:
        if state not in _AGENT_STATES:
            raise ValueError(f"不认识的目标状态「{state}」")
        with self._store.tx() as conn:
            row = conn.execute("SELECT state FROM goals WHERE id=?", (str(goal_id),)).fetchone()
            if row is None:
                raise KeyError(f"目标不存在: {goal_id}")
            previous = str(row["state"])
            if state == previous:
                return  # 重复取消/暂停保持幂等，但绝不允许取消/暂停再变成 done
            if state not in _ALLOWED_STATE_CHANGES.get(previous, frozenset()):
                raise ValueError(f"目标 {goal_id} 不能从「{previous}」改成「{state}」")
            conn.execute(
                "UPDATE goals SET state=?, updated=? WHERE id=? AND state=?",
                (state, clock.now(), str(goal_id), previous),
            )

    def pause(self, goal_id: str) -> None:
        self._set_state(goal_id, "paused")

    def resume(self, goal_id: str) -> None:
        self._set_state(goal_id, "active")

    def cancel(self, goal_id: str) -> None:
        self._set_state(goal_id, "cancelled")

    def done(self, goal_id: str) -> None:
        self._set_state(goal_id, "done")

    def touch(self, goal_id: str, text: str) -> None:
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE goals SET last_ts=?, last_text=?, updated=? WHERE id=?",
                (clock.now(), str(text or ""), clock.now(), str(goal_id)),
            )
            if cur.rowcount == 0:
                raise KeyError(f"目标不存在: {goal_id}")

    def set_next_check(self, goal_id: str, ts: float | None) -> None:
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE goals SET next_check_ts=?, updated=? WHERE id=?",
                ((float(ts) if ts is not None else None), clock.now(), str(goal_id)),
            )
            if cur.rowcount == 0:
                raise KeyError(f"目标不存在: {goal_id}")

    def set_criteria(self, goal_id: str, criteria: Iterable[str]) -> list[dict]:
        """整组换掉完成标准（只给「第一次检查补齐验收标准」用；空文字丢弃）。

        和 set_criterion 一样，找不到目标抛 KeyError。
        """
        crit = [{"text": str(c).strip(), "done": False} for c in criteria if str(c).strip()]
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE goals SET criteria=?, updated=? WHERE id=?",
                (json.dumps(crit, ensure_ascii=False), clock.now(), str(goal_id)),
            )
            if cur.rowcount == 0:
                raise KeyError(f"目标不存在: {goal_id}")
        return crit

    def set_criterion(
        self, goal_id: str, index: int, done: bool, *,
        evidence: str | None = None, task_id: str | None = None, ts: float | None = None,
    ) -> None:
        """勾 / 取消一条完成标准。

        docs/22 §5 C（2026-10-07 本地）：打勾要证据——传了 evidence / task_id / ts 时
        一并写进这条 criterion（`evidence` 折叠空白截 200 字、`task_id` 可空、`ts` 默认现在），
        旧调用（只传 done）行为一字不差。
        """
        with self._store.tx() as conn:
            row = conn.execute("SELECT criteria FROM goals WHERE id=?", (str(goal_id),)).fetchone()
            if row is None:
                raise KeyError(f"目标不存在: {goal_id}")
            try:
                crit = json.loads(row["criteria"] or "[]")
            except (TypeError, ValueError):
                crit = []
            if not isinstance(crit, list):
                crit = []
            if index < 0 or index >= len(crit):
                raise IndexError(f"目标 {goal_id} 没有第 {index} 条完成标准")
            item = dict(crit[index])
            item["done"] = bool(done)
            if evidence is not None or task_id is not None or ts is not None:
                item["evidence"] = " ".join(str(evidence or "").split())[:GOAL_EVIDENCE_MAX]
                item["task_id"] = (str(task_id) if task_id else None)
                item["ts"] = float(ts) if ts is not None else clock.now()
            crit[index] = item
            conn.execute(
                "UPDATE goals SET criteria=?, updated=? WHERE id=?",
                (json.dumps(crit, ensure_ascii=False), clock.now(), str(goal_id)),
            )

    # ------------------------------------------------------------------
    # 报平安（docs/02 §4.3）
    # ------------------------------------------------------------------

    @staticmethod
    def _fail_key(goal_id: str) -> str:
        return f"goal.fail.{goal_id}"

    def beat(self, goal_id: str, now: float | None = None) -> None:
        """检查成功跑完：写心跳、清 stale_reason、出错计数归零。"""
        ts = clock.now() if now is None else float(now)
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE goals SET heartbeat_ts=?, stale_reason='', updated=? WHERE id=?",
                (ts, ts, str(goal_id)),
            )
            if cur.rowcount == 0:
                raise KeyError(f"目标不存在: {goal_id}")
            self._store.kv_set(conn, self._fail_key(goal_id), 0)

    def fail(self, goal_id: str, error: str, now: float | None = None) -> int:
        """检查出错计数 +1；连续满 3 次 → stale_reason「连续 3 次检查出错：…」。返回累计次数。"""
        ts = clock.now() if now is None else float(now)
        key = self._fail_key(goal_id)
        count = int(self._store.kv_get(key, 0) or 0) + 1
        with self._store.tx() as conn:
            self._store.kv_set(conn, key, count)
            if count >= _FAIL_THRESHOLD:
                short = str(error or "")[:_FAIL_ERROR_LEN]
                conn.execute(
                    "UPDATE goals SET stale_reason=?, updated=? WHERE id=?",
                    (f"{_STALE_ERROR_PREFIX}{short}", ts, str(goal_id)),
                )
        return count

    def refresh_stale(self, now: float, *, models_ready: bool) -> list[str]:
        """后台目标轮巡视（docs/02 §4.3）：超时没心跳的 agent 目标标原因。返回改过的目标 id。

        - 无心跳时长 = now - max(heartbeat_ts, created)；
          窗口 = 2 ×（next_check_ts 与参照点的间隔，缺省 1 小时）。
        - 超时且模型配好 → 「超过预定检查时间还没动静」；
          超时但模型没配好 → 「模型没配好，暂停检查」（不算卡住）。
        - 未超时 → 清掉巡视类原因；「连续 3 次检查出错：…」由 beat 清，巡视图省事不动。
        """
        rows = self._store.read().execute(
            "SELECT id, created, heartbeat_ts, next_check_ts, stale_reason FROM goals"
            " WHERE kind='agent' AND state='active'"
        ).fetchall()
        changed: list[str] = []
        with self._store.tx() as conn:
            for r in rows:
                gid = str(r["id"])
                current = str(r["stale_reason"] or "")
                if current.startswith(_STALE_ERROR_PREFIX):
                    continue  # 出错原因保留，等下次成功（beat）清
                ref = float(r["heartbeat_ts"]) if r["heartbeat_ts"] is not None else float(r["created"])
                interval = _AGENT_NEXT_CHECK_S
                if r["next_check_ts"] is not None and float(r["next_check_ts"]) > ref:
                    interval = float(r["next_check_ts"]) - ref
                overdue = float(now) - ref > 2 * interval
                if overdue:
                    desired = STALE_TIMEOUT_REASON if models_ready else STALE_MODEL_REASON
                else:
                    desired = ""
                if current == desired:
                    continue
                conn.execute(
                    "UPDATE goals SET stale_reason=?, updated=? WHERE id=?",
                    (desired, float(now), gid),
                )
                changed.append(gid)
        return changed

    # ------------------------------------------------------------------
    # 网页视图（docs/07 §9.3 goals）
    # ------------------------------------------------------------------

    def view(self, group_id: str) -> dict:
        now = clock.now()
        rows = self._store.read().execute(
            "SELECT * FROM goals WHERE group_id=? ORDER BY created ASC, id ASC",
            (str(group_id),),
        ).fetchall()
        agent: list[dict] = []
        member: list[dict] = []
        for raw in rows:
            g = {k: raw[k] for k in raw.keys()}
            state = str(g["state"])
            if state == "cancelled":
                continue
            if state == "done" and now - float(g["updated"]) > _DONE_KEEP_S:
                continue  # done 的只保留 3 天
            if str(g["kind"]) == "agent":
                agent.append(self._agent_view(g))
            else:
                member.append(self._member_view(g))
        return {"agent": agent, "member": member}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _criteria(g: dict) -> list[dict]:
        try:
            out = json.loads(g["criteria"] or "[]")
        except (TypeError, ValueError):
            out = []
        return out if isinstance(out, list) else []

    def _who_name(self, g: dict) -> str:
        """成员目标的显示名：按 who_id 查名册当前名；查不到回落 who_name 快照。"""
        return members.name_of(
            self._store, g.get("group_id"), g.get("who_id"), fallback=g.get("who_name")
        )

    def _agent_dict(self, g: dict) -> dict:
        return {
            "id": str(g["id"]),
            "kind": "agent",
            "icon": str(g["icon"] or "bullseye"),
            "title": str(g["title"]),
            "body": str(g["body"] or ""),
            "by_text": str(g["by_text"] or ""),
            "criteria": self._criteria(g),
            "state": str(g["state"]),
            "next_check_ts": g["next_check_ts"],
            "last_ts": g["last_ts"],
            "last_text": str(g["last_text"] or ""),
            "task_id": (str(g["task_id"]) if g["task_id"] else None),
            "request_id": (str(g["request_id"]) if g["request_id"] else None),
        }

    def _member_dict(self, g: dict) -> dict:
        return {
            "id": str(g["id"]),
            "kind": "member",
            "icon": str(g["icon"] or "alarm"),
            "title": str(g["title"]),
            "who_id": str(g["who_id"] or ""),
            "who_name": self._who_name(g),
            "state": str(g["state"]),
            "due_ts": g["due_ts"],
            "remind_ts": g["remind_ts"],
            "repeat": (str(g["repeat"]) if g["repeat"] else None),
            "until_ts": g["until_ts"],
            "last_ts": g["last_ts"],
            "last_text": str(g["last_text"] or ""),
        }

    def _agent_view(self, g: dict) -> dict:
        last = None
        if g["last_ts"] is not None:
            last = {"ts": float(g["last_ts"]), "text": str(g["last_text"] or "")}
        reason = str(g["stale_reason"] if g["stale_reason"] is not None else "")
        return {
            "id": str(g["id"]),
            "icon": str(g["icon"] or "bullseye"),
            "title": str(g["title"]),
            "body": str(g["body"] or ""),
            "criteria": self._criteria(g),
            "state": str(g["state"]),
            "next_check_ts": g["next_check_ts"],
            "by": str(g["by_text"] or ""),
            "last": last,
            "task_id": (str(g["task_id"]) if g["task_id"] else None),
            # 报平安（字段名固定，前端要用）：stale=卡住（「模型没配好」不算）
            "stale": stale_is_blocking(reason),
            "stale_reason": reason,
            "heartbeat_ts": (float(g["heartbeat_ts"]) if g["heartbeat_ts"] is not None else None),
        }

    def _member_view(self, g: dict) -> dict:
        return {
            "id": str(g["id"]),
            "icon": str(g["icon"] or "alarm"),
            "who": self._who_name(g),
            "title": str(g["title"]),
            "due_ts": g["due_ts"],
            "remind_ts": g["remind_ts"],
            "repeat": (str(g["repeat"]) if g["repeat"] else None),
            "until_ts": g["until_ts"],
            "state": str(g["state"]),
        }
