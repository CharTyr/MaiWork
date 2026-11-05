"""专岗交接单适配器（agent_handoffs）：一件交接单 = 一件活。

- lane：交接单的 kind（news / idea / c_*…；task 岗的也是 handoff 进 task 栏）。
- active：status running / returned（queued 排队不算在跑）；updated 距今
  >600s → stuck。
- finished：status accepted / rejected / failed / cancelled（窗口内）。
- trace：run_ref=handoff:<id> → 这一张单自己的轮（exact）；run_ref=共享轮 ref
  → 同 ref 里切 [created, updated+1]（exact）；task_id=T-* → 按交接单时窗切；
  其它 task_id → 全程拉；都没有 → window（如实报 time_window）。
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


def _ref_select(ctx: Ctx) -> str:
    """run_ref 列的取法：库号 50 起有；半截库没有 → 空串占位，别让整条查挂。"""
    try:
        cols = {str(r["name"]) for r in ctx.fetch("PRAGMA table_info(agent_handoffs)")}
    except Exception:
        cols = set()
    return "run_ref" if "run_ref" in cols else "'' AS run_ref"


def _row_to_item(r: Any, lane: str, now: float) -> dict[str, Any]:
    created = _f(r["created"])
    updated = _f(r["updated"]) or created
    status = str(r["status"] or "")
    run_ref = str(r["run_ref"] or "") if "run_ref" in r.keys() else ""
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
        "_run_ref": run_ref,
        # 失败原因：只取坏消息（error 列空时回退 review 里的验收意见）——
        # accepted 的 review 是「核验通过」，不能进红色错误横幅
        "error": (str(r["error"] or r["review"] or "")
                  if status in ("failed", "rejected", "cancelled") else ""),
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    run_ref = str(item.get("_run_ref") or "")
    tid = str(item.get("task_id") or "")
    if run_ref.startswith("handoff:"):
        # 这张单自己的轮：ref 一对一，整段就是它的过程
        return Trace(kind="run_ref", value=run_ref, group_id=item["group_id"])
    if run_ref:
        # 共享轮 ref（news:/idea:/goal:…）：同 ref 里只切这张单的时间窗，
        # 别家的步不混进来（counts / last_step 走同一套切片）。
        end = item["updated"] + 1.0 if item["status"] in _DONE else now
        return Trace(kind="run_ref", value=run_ref, group_id=item["group_id"],
                     start=item["started"], end=end, slice_ts=True)
    if tid.startswith("T-"):
        if item["status"] not in _DONE or item["updated"] > item["started"]:
            # 老 T-* 交接单（没 run_ref）：task_id 精确但要按交接单时窗切——
            # reviewed 可以晚于执行，窗端到 updated+1s。slice_ts 让批量
            # counts / last_step 走同一套窗，不跟父任务抢整段计数。
            end = item["updated"] + 1.0 if item["status"] in _DONE else now
            return Trace(kind="task_id", value=tid, group_id=item["group_id"],
                         start=item["started"], end=end, slice_ts=True)
        # 合成种子 / 老库：updated == created 没时窗可切 → 全程拉（同宽窗口径）
        return Trace(kind="task_id", value=tid, group_id=item["group_id"],
                     windowed=False)
    if tid:
        # 其它 task_id（feeds-verify:* 等老形状）：全程拉，不切窗
        return Trace(kind="task_id", value=tid, group_id=item["group_id"],
                     windowed=False)
    # 交接单没挂任何钥匙的（队列期 / 老数据）：退到同群 + 时间窗查，如实报
    # time_window。窗口 end 用 now（不是 finished）：终态也拉到当前——reviewed
    # 晚于执行，封到 finished 会把验收后的步切掉。
    return Trace(kind="window", group_id=item["group_id"],
                 start=item["started"], end=now)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(_RUNNING))
    ref_sel = _ref_select(ctx)
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, kind, task_id, phase, status, brief, summary, review, error,"
        f" created, updated, {ref_sel} FROM agent_handoffs WHERE status IN ({marks})"
        " ORDER BY updated DESC",
        _RUNNING,
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, str(r["kind"] or ""), ctx.now))
    return out


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(_DONE))
    ref_sel = _ref_select(ctx)
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, kind, task_id, phase, status, brief, summary, review, error,"
        f" created, updated, {ref_sel} FROM agent_handoffs WHERE status IN ({marks})"
        " AND updated >= ? ORDER BY updated DESC LIMIT 4000",
        (*_DONE, since),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, str(r["kind"] or ""), ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    r = ctx.fetch_one(
        "SELECT id, group_id, kind, task_id, phase, status, brief, summary, review, error,"
        f" created, updated, {_ref_select(ctx)} FROM agent_handoffs WHERE id=?",
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
