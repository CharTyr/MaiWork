"""构想适配器（ideas）：一件构想 = 一件活，归 idea 栏。

- active：没有（构想是一次性入库的）。
- finished：created 在窗口内；个人向构想（target_user_id 非空）title 一律
  写成「个人构想」，不露原话。
- trace：库号 47 起构想带 run_ref → exact；老构想 → 同群 + 窗口（created
  前后 10 分钟）best-effort，如实报 time_window。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _f
from ..trace import Trace

SOURCE = "idea"


def _row_to_item(r: Any, now: float) -> dict[str, Any]:
    personal = bool(str(r["target_user_id"] or "").strip())
    title = "个人构想" if personal else str(r["title"] or "")
    created = _f(r["created"])
    updated = _f(r["updated"]) or created
    item = {
        "source": SOURCE, "id": int(r["id"]), "lane": "idea",
        "group_id": str(r["group_id"] or ""),
        "title": title,
        "status": str(r["state"] or "new"), "status_text": "有了新构想",
        "phase": "",
        "started": created, "finished": created, "updated": updated,
        "ok": True, "stuck": False, "task_id": "",
        "_run_ref": str(r["run_ref"] or ""),
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    run_ref = str(item.get("_run_ref") or "")
    created = item["started"]
    if run_ref:
        return Trace(kind="run_ref", value=run_ref, group_id=item["group_id"],
                     start=created - 600, end=created + 600)
    return Trace(kind="window", group_id=item["group_id"],
                 start=created - 600, end=created + 600)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    return []


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, title, target_user_id, state, created, updated, run_ref FROM ideas"
        " WHERE created >= ? ORDER BY created DESC LIMIT 4000",
        (since,),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return None
    r = ctx.fetch_one(
        "SELECT id, group_id, title, target_user_id, state, created, updated, run_ref FROM ideas WHERE id=?",
        (iid,),
    )
    if r is None:
        return None
    if str(r["group_id"] or "") not in ctx.served:
        return None
    return _row_to_item(r, ctx.now)
