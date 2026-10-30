"""专岗交接单适配器（agent_handoffs）：一件交接单 = 一件活。

- lane：交接单的 kind（news / idea / c_*…；task 岗的也是 handoff 进 task 栏）。
- active：status running / returned（queued 排队不算在跑）；updated 距今
  >600s → stuck。
- finished：status accepted / rejected / failed / cancelled（窗口内）。
- trace：task_id（交接单干活的是 T-* 任务；精确）；任务没挂上 → window
  （如实报 time_window）。窗口 end 用 now 不封 finished：reviewed 晚于执行，
  封到 finished 会把验收后的步切掉。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _f
from ..trace import Trace

SOURCE = "handoff"

_STATUS_TEXT = {
    "queued": "排队中", "running": "正在跑", "returned": "已交回待验收",
    "accepted": "已通过", "rejected": "已打回", "failed": "没做成", "cancelled": "已取消",
}
_RUNNING = ("running", "returned")
_DONE = ("accepted", "rejected", "failed", "cancelled")


def _row_to_item(r: Any, lane: str, now: float) -> dict[str, Any]:
    created = _f(r["created"])
    updated = _f(r["updated"]) or created
    status = str(r["status"] or "")
    item = {
        "source": SOURCE, "id": str(r["id"]), "lane": lane,
        "group_id": str(r["group_id"] or ""),
        "title": str(r["brief"] or ""),
        "status": status, "status_text": _STATUS_TEXT.get(status, status),
        "phase": str(r["phase"] or ""),
        "started": created,
        "finished": updated if status in _DONE else None,
        "updated": updated,
        "ok": (True if status == "accepted" else (False if status in _DONE else None)),
        "stuck": bool(status in _RUNNING and now - updated > 600),
        "task_id": str(r["task_id"] or ""),
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    tid = str(item.get("task_id") or "")
    if tid:
        # task_id 本来就精确；updated 可能等于 created（立即失败 / 测试）、
        # reviewed 晚于执行，窗太紧反而漏 → 不按 ts 过滤（全程拉）
        return Trace(kind="task_id", value=tid, group_id=item["group_id"], windowed=False)
    # 交接单没挂任务的（队列期 / 老数据）：退到同群 + 时间窗查，如实报
    # time_window。窗口 end 用 now（不是 finished）：终态也拉到当前——reviewed
    # 晚于执行，封到 finished 会把验收后的步切掉。
    return Trace(kind="window", group_id=item["group_id"],
                 start=item["started"], end=now)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(_RUNNING))
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, kind, task_id, phase, status, brief, created, updated"
        f" FROM agent_handoffs WHERE status IN ({marks}) ORDER BY updated DESC",
        _RUNNING,
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, str(r["kind"] or ""), ctx.now))
    return out


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(_DONE))
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, kind, task_id, phase, status, brief, created, updated"
        f" FROM agent_handoffs WHERE status IN ({marks}) AND updated >= ?"
        " ORDER BY updated DESC LIMIT 4000",
        (*_DONE, since),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, str(r["kind"] or ""), ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    r = ctx.fetch_one(
        "SELECT id, group_id, kind, task_id, phase, status, brief, created, updated"
        " FROM agent_handoffs WHERE id=?",
        (str(item_id or ""),),
    )
    if r is None:
        return None
    if str(r["group_id"] or "") not in ctx.served:
        return None
    item = _row_to_item(r, str(r["kind"] or ""), ctx.now)
    # 便条板（docs/34）：这张交接单检查通过时贴上板的便条（专岗自己的 + 主模型附的）
    item["_notes"] = [
        {"from_kind": str(n["from_kind"]), "to_kind": str(n["to_kind"]),
         "body": str(n["body"]), "created": _f(n["created"])}
        for n in ctx.fetch(
            "SELECT from_kind, to_kind, body, created FROM agent_notes"
            " WHERE group_id=? AND handoff_id=? ORDER BY id LIMIT 20",
            (item["group_id"], item["id"]),
        )
    ]
    return item
