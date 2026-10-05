"""任务双岗协作的 lane 存储（docs/20 §六）。

一个任务挂着几条 lane：`lead`（领队 = 主模型）和 `worker:<n>`（第 n 条活的干活
子 agent）。每条 lane 一份**持久对话**（OpenAI messages，不含 system），被打回后
同一条 lane 带着前情接着改，而不是从零重来。

边界（docs/20 §6.1 / §七）：
- 只在**同一个任务内部**记前情：任务到终态时 `Tasks.transition` 在同一事务里调
  `close_task_lanes` 清空；不跨任务、不跨群（任务岗仍是「不跨任务留记忆」）。
- 保存是有条件的：任务已到终态 / 需求版本不对 / 群号不对 → 不写（取消、改版之后
  晚到的结果不复活对话）。
- system 不存：规矩、本群做法、工具说明每轮现算，权限变了当轮生效。
- 续用前 `prepare_history` 把本轮不给的工具的调用记录改写成文字（不留工具调用格式，
  模型不会以为还能用；真调用时 Tools.call 的硬门照拦）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from . import clock

logger = logging.getLogger("maiwork.lanes")

# 不能再保存 lane 的任务状态（终态 + failed：failed 只能由人重开，重开就从零来）
_CLOSED_TASK_STATUSES = ("completed", "cancelled", "rejected", "failed")

# 改写成文字时每段结果最多留多少字（前情提要够用就行，别把旧工具大结果整段搬回来）
_REWRITE_RESULT_CHARS = 1200
_REWRITE_ARGS_CHARS = 300

_DANGLING_REPLY = "（这一步之后已经把结果交回给领队，等验收；领队的反馈在下面）"


# 带 lane 的活（任务双岗协作，docs/20 §四 / 第三步）追加进子 agent 的 system：
# 可以对说明本身提异议，由领队决定；不许自己改需求。
LANE_WORKER_RULES = (
    "你在这个任务里有一条自己的对话：上面的前情是你之前在这个任务里做过的，接着干，别从头再来。"
    "觉得这次的说明本身有问题（做不到、前后矛盾、有明显更好的做法）：先把能做的部分做好，"
    "再在 submit_result 的 challenge 里写清理由、依据和建议，由领队决定；不要自己改需求，"
    "也不要因为有异议就什么都不交。"
)


def close_task_lanes(conn: Any, task_id: str, *, lanes: str = "all", now: float | None = None) -> None:
    """同一事务里把任务的 lane 清空并标 closed（Tasks.transition / revise 调）。

    lanes="all" 全部；"workers" 只关干活 lane（需求改版：旧活可能已经不对，领队留着）。
    """
    ts = clock.now() if now is None else now
    sql = "UPDATE task_lanes SET messages='[]', status='closed', updated=? WHERE task_id=?"
    params: list[Any] = [ts, str(task_id)]
    if lanes == "workers":
        sql += " AND lane LIKE 'worker:%'"
    conn.execute(sql, params)


def _as_messages(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict) and m.get("role")]


def _tool_names(msg: dict) -> list[str]:
    out = []
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            out.append(str((tc.get("function") or {}).get("name") or ""))
    return out


def _clip(text: Any, n: int) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[:n] + " …（截断）"


def close_dangling_tool_calls(messages: Iterable[Any]) -> list[dict]:
    """给没收到回复的工具调用补一条 tool 回复（交回 submit_result 时就是这样悬着的）。

    严格的端点要求每个 assistant.tool_calls 后面都有对应 id 的 tool 消息，否则 400。
    """
    msgs = _as_messages(list(messages or []))
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        out.append(m)
        i += 1
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        want = [str(tc.get("id") or "") for tc in m["tool_calls"] if isinstance(tc, dict)]
        seen: set[str] = set()
        while i < len(msgs) and msgs[i].get("role") == "tool":
            seen.add(str(msgs[i].get("tool_call_id") or ""))
            out.append(msgs[i])
            i += 1
        for tc in m["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or "")
            if cid and cid not in seen:
                out.append({
                    "role": "tool", "tool_call_id": cid,
                    "name": str((tc.get("function") or {}).get("name") or ""),
                    "content": _DANGLING_REPLY,
                })
        del want
    return out


def prepare_history(messages: Iterable[Any], *, allowed: Iterable[str]) -> list[dict]:
    """续用前整理一条 lane 的历史（docs/20 §6.2）。

    - 去掉 system（每轮现算）和坏数据；
    - 补齐悬着的工具调用回复；
    - 一组工具调用里只要有本轮不给的工具，整组改写成一条 assistant 文字
      （「之前用过 X（参数），结果：…；这一轮不能再用」），不留工具调用格式。
    """
    allowed_set = {str(x) for x in (allowed or ())}
    msgs = close_dangling_tool_calls([m for m in _as_messages(list(messages or [])) if m.get("role") != "system"])
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        i += 1
        if m.get("role") == "tool":
            # 前面没有 assistant(tool_calls) 的孤儿回复：严格端点会拒，丢掉
            continue
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            out.append(m)
            continue
        group = [m]
        while i < len(msgs) and msgs[i].get("role") == "tool":
            group.append(msgs[i])
            i += 1
        names = [n for n in _tool_names(m) if n]
        if all(n in allowed_set for n in names):
            out.extend(group)
            continue
        results = {str(t.get("tool_call_id") or ""): t.get("content") for t in group[1:]}
        lines = []
        if str(m.get("content") or "").strip():
            lines.append(str(m["content"]).strip())
        for tc in m["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            res = results.get(str(tc.get("id") or ""), "")
            note = "" if name in allowed_set else "（这一轮不能再用这个工具）"
            lines.append(
                f"之前用过 {name}{note}，参数：{_clip(args, _REWRITE_ARGS_CHARS)}；"
                f"结果：{_clip(res, _REWRITE_RESULT_CHARS)}"
            )
        out.append({"role": "assistant", "content": "\n".join(lines)})
    return out


class TaskLanes:
    """task_lanes 表的读写（Store 之上的一薄层）。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    @staticmethod
    def _row(row: Any) -> dict:
        return {
            "task_id": str(row["task_id"]),
            "lane": str(row["lane"]),
            "group_id": str(row["group_id"]),
            "kind": str(row["kind"] or ""),
            "model": str(row["model"] or ""),
            "req_version": int(row["req_version"] or 1),
            "messages": _as_messages(row["messages"]),
            "snapshot": str(row["snapshot"] or ""),
            "escalated": bool(row["escalated"]),
            "handoff_id": str(row["handoff_id"] or ""),
            "status": str(row["status"] or ""),
            "updated": float(row["updated"] or 0),
        }

    def load(self, task_id: str, lane: str, *, group_id: str) -> dict | None:
        row = self._store._conn.execute(
            "SELECT * FROM task_lanes WHERE task_id=? AND lane=? AND group_id=?",
            (str(task_id), str(lane), str(group_id)),
        ).fetchone()
        return self._row(row) if row is not None else None

    def list(self, task_id: str, *, group_id: str) -> list[dict]:
        rows = self._store._conn.execute(
            "SELECT * FROM task_lanes WHERE task_id=? AND group_id=? ORDER BY lane",
            (str(task_id), str(group_id)),
        ).fetchall()
        return [self._row(r) for r in rows]

    def save(
        self,
        task_id: str,
        lane: str,
        *,
        group_id: str,
        kind: str,
        messages: list[dict],
        req_version: int,
        model: str | None = None,
        escalated: bool | None = None,
        snapshot: str | None = None,
        handoff_id: str | None = None,
    ) -> bool:
        """有条件保存：任务属于这个群、没到终态、需求版本对得上才写。返回写没写。"""
        now = clock.now()
        msgs = close_dangling_tool_calls([m for m in _as_messages(messages) if m.get("role") != "system"])
        body = json.dumps(msgs, ensure_ascii=False)
        with self._store.tx() as conn:
            t = conn.execute(
                "SELECT status, req_version, group_id FROM tasks WHERE id=?", (str(task_id),)
            ).fetchone()
            if t is None or str(t["group_id"]) != str(group_id):
                return False
            if str(t["status"]) in _CLOSED_TASK_STATUSES:
                return False
            if int(t["req_version"] or 1) != int(req_version):
                return False
            old = conn.execute(
                "SELECT model, escalated, snapshot, handoff_id FROM task_lanes WHERE task_id=? AND lane=?",
                (str(task_id), str(lane)),
            ).fetchone()
            model_v = str(model) if model is not None else (str(old["model"]) if old is not None else "")
            esc_v = (1 if escalated else 0) if escalated is not None else (int(old["escalated"]) if old is not None else 0)
            snap_v = str(snapshot) if snapshot is not None else (str(old["snapshot"]) if old is not None else "")
            hid_v = str(handoff_id) if handoff_id is not None else (str(old["handoff_id"]) if old is not None else "")
            conn.execute(
                "INSERT INTO task_lanes (task_id, lane, group_id, kind, model, req_version, messages,"
                " snapshot, escalated, handoff_id, status, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)"
                " ON CONFLICT(task_id, lane) DO UPDATE SET group_id=excluded.group_id, kind=excluded.kind,"
                " model=excluded.model, req_version=excluded.req_version, messages=excluded.messages,"
                " snapshot=excluded.snapshot, escalated=excluded.escalated,"
                " handoff_id=excluded.handoff_id, status='open',"
                " updated=excluded.updated",
                (str(task_id), str(lane), str(group_id), str(kind or ""), model_v, int(req_version),
                 body, snap_v, esc_v, hid_v, now, now),
            )
        return True

    def reset(self, task_id: str, lane: str, *, group_id: str) -> None:
        """这条 lane 从零重开（压缩失败 / 换了岗位）：清对话，计数和提要保留备查。"""
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE task_lanes SET messages='[]', escalated=0, updated=? WHERE task_id=? AND lane=? AND group_id=?",
                (clock.now(), str(task_id), str(lane), str(group_id)),
            )
