"""资讯批次适配器（news_batches）：一轮资讯 = 一件活，归 news 栏。

- active：没有（一轮是一次性的，不存在「在跑的资讯批次」）。
- finished：created 在窗口内；个人向批次（note personal:*）不上板；
  skipped=1 是「这轮没留」（ok=None 中性），不是失败。
- trace：库号 47 起批次带 run_ref → exact；老批次 run_ref 空 → 同群 +
  窗口（created 前 30 分钟到后 5 分钟）best-effort，如实报 time_window。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _f
from ..trace import Trace

SOURCE = "news_batch"


def _row_to_item(r: Any, now: float) -> dict[str, Any]:
    created = _f(r["created"])
    kept = int(r["kept"] or 0)
    found = int(r["found"] or 0)
    skipped = int(r["skipped"] or 0)
    note = str(r["note"] or "")
    if skipped:
        title = f"找了一轮资讯，跳过：{note[:40]}" if note else "找了一轮资讯，这轮跳过"
    elif kept > 0:
        title = f"找了一轮资讯，留下 {kept}/{found} 条"
    else:
        title = "找了一轮资讯，这轮没有可留的"
    item = {
        "source": SOURCE, "id": int(r["id"]), "lane": "news",
        "group_id": str(r["group_id"] or ""),
        "title": title,
        "status": "skipped" if skipped else "done",
        "status_text": "跳过了" if skipped else "找完了",
        "phase": "",
        "started": created, "finished": created, "updated": created,
        "ok": (None if skipped else True), "stuck": False, "task_id": "",
        "_run_ref": str(r["run_ref"] or ""),
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    run_ref = str(item.get("_run_ref") or "")
    created = item["started"]
    if run_ref:
        return Trace(kind="run_ref", value=run_ref, group_id=item["group_id"],
                     start=created - 1800, end=created + 300)
    return Trace(kind="window", group_id=item["group_id"],
                 start=created - 1800, end=created + 300)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    return []


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, found, kept, skipped, note, created, run_ref FROM news_batches"
        " WHERE created >= ? AND note NOT LIKE 'personal:%'"
        " ORDER BY created DESC LIMIT 4000",
        (since,),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    try:
        bid = int(item_id)
    except (TypeError, ValueError):
        return None
    r = ctx.fetch_one(
        "SELECT id, group_id, found, kept, skipped, note, created, run_ref FROM news_batches WHERE id=?",
        (bid,),
    )
    if r is None:
        return None
    if str(r["group_id"] or "") not in ctx.served:
        return None
    if str(r["note"] or "").startswith("personal:"):
        return None  # 个人向批次不上板
    return _row_to_item(r, ctx.now)
