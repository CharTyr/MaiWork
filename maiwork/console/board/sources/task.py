"""任务适配器（tasks）：一件 T-* 任务 = 一件活，永远归 task 栏。

- active：queued / running / waiting_input / reviewing（shelved/paused 不进
  running 列表）；「功能关闭搁置」的 queued 和「等人答复」不算卡住。
- finished：done / failed / cancelled（窗口内）。
- trace：task_id=任务自己的 id（历来精确）。窗口 end 用原始 finished_ts
  （不是回落后的 finished 字段——updated=created 的种子会把干活时间切掉）。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _paused_kind, _f
from ..trace import Trace

SOURCE = "task"

_STATUS_TEXT = {
    "queued": "排队中", "running": "正在跑", "waiting_input": "等人答复",
    "reviewing": "等验收", "paused": "已暂停", "shelved": "已搁置",
    "done": "完成了", "failed": "没做成", "cancelled": "已取消",
}
_RUNNING = ("queued", "running", "waiting_input", "reviewing")
_DONE = ("done", "failed", "cancelled")


def _row_to_item(r: Any, now: float) -> dict[str, Any]:
    created = _f(r["created"])
    started = _f(r["started_ts"]) or created
    status = str(r["status"] or "")
    # 终态：finished/updated 都回落 finished_ts → updated → started（列表看成对
    # 起止，详情同形）；在跑：finished=None、updated 回退或 started。
    if status in _DONE:
        finished = _f(r["finished_ts"]) or _f(r["updated"]) or started
        updated = finished
    else:
        finished = None
        updated = _f(r["updated"]) or started
    held = (status == "queued" and _paused_kind(r["paused_reason"]) == "feature_off") or status == "waiting_input"
    item = {
        "source": SOURCE, "id": str(r["id"]), "lane": "task",
        "group_id": str(r["group_id"] or ""),
        "title": str(r["title"] or ""),
        "status": status, "status_text": _STATUS_TEXT.get(status, status),
        "phase": "",
        "started": started,
        "finished": finished,
        "updated": updated,
        "ok": (True if status == "done" else (False if status in _DONE else None)),
        "stuck": bool(status in _RUNNING and not held and now - updated > 600),
        "task_id": str(r["id"]),
        "_finished_ts": _f(r["finished_ts"]),   # 原始 finished_ts（trace 窗口用；回落值会封死窗口）
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    # 窗口 end 用 finished_ts（finished 字段已回落 updated，不能拿它封窗——
    # 种子里 updated=created 会把干活那几十分钟切掉）。finished_ts 空 → 还开着 → 到 now。
    return Trace(kind="task_id", value=str(item["id"]), group_id=item["group_id"],
                 start=item["started"], end=_f(item.get("_finished_ts")) or now)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(_RUNNING))
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, title, status, paused_reason, created, updated, started_ts, finished_ts"
        f" FROM tasks WHERE status IN ({marks}) ORDER BY updated DESC",
        _RUNNING,
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, title, status, paused_reason, created, updated, started_ts, finished_ts"
        " FROM tasks WHERE status IN ('done','failed','cancelled')"
        " AND COALESCE(finished_ts, updated) >= ?"
        " ORDER BY COALESCE(finished_ts, updated) DESC LIMIT 4000",
        (since,),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    r = ctx.fetch_one(
        "SELECT id, group_id, title, status, paused_reason, created, updated, started_ts, finished_ts"
        " FROM tasks WHERE id=?",
        (str(item_id or ""),),
    )
    if r is None:
        return None
    if str(r["group_id"] or "") not in ctx.served:
        return None
    return _row_to_item(r, ctx.now)
