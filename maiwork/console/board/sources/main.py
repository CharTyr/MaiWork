"""主模型条目适配器（main_runs，库号 46）：一次独立判断 = 一件活，归 main 栏。

- active：status=running 且 started 距今没超过 RUN_STALE_S；过了时限的
  启动收尾没来及标 cancelled（崩溃 / 断电），看板直接当「已中断」放历史。
- finished：done / failed / cancelled（finished/started 在窗口内）+
  过期 running（显示成 cancelled）。
- trace：run_ref（main_runs.ref；库号 46 起每条都带，精确）。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _f
from ..trace import STEP_TEXT_MAX, Trace

SOURCE = "main"

_STATUS_TEXT = {
    "running": "正在判断", "done": "完成了", "failed": "没做成", "cancelled": "中断 / 取消了",
}

try:
    from ...main_run import RUN_STALE_S as _STALE_S
except Exception:  # main_run 模块缺席的半截环境：不挡看板
    _STALE_S = 6 * 3600


def _visible(ctx: Ctx, r: Any) -> bool:
    """服务群的条目，或不属于任何群的（不聚焦群的管理员对话等，group_id 空）。"""
    gid = str(r["group_id"] or "")
    return not gid or gid in ctx.served


def _row_to_item(r: Any, now: float) -> dict[str, Any]:
    started = _f(r["started"])
    finished = _f(r["finished"])
    status = str(r["status"] or "")
    stale = status == "running" and now - started > _STALE_S
    if stale:
        status = "cancelled"      # 过期 running = 中断
    ok = True if status == "done" else (False if status in ("failed", "cancelled") else None)
    # lanes / 详情同形：stale running 的 finished 回退到 started（当中断那刻）；
    # 没写收尾的 0 也回退（详情不再给库原值——同一件只有一种形状）。
    fin_out = (finished or started) if status != "running" else None
    item = {
        "source": SOURCE, "id": int(r["id"]), "lane": "main",
        "group_id": str(r["group_id"] or ""),
        "title": str(r["title"] or ""),
        "status": status, "status_text": _STATUS_TEXT.get(status, status),
        "phase": "",
        "started": started,
        "finished": fin_out,
        "updated": finished or started,
        "ok": ok, "stuck": False, "task_id": "",
        "_run_ref": str(r["ref"] or ""),
        "error": str(r["error"] or "")[:STEP_TEXT_MAX],
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    return Trace(kind="run_ref", value=str(item.get("_run_ref") or ""),
                 group_id=item["group_id"],
                 start=item["started"], end=item["finished"] or now)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, purpose, title, status, started, finished, ok, error, ref"
        " FROM main_runs WHERE status='running' ORDER BY started DESC",
        (),
    ):
        if not _visible(ctx, r):
            continue
        started = _f(r["started"])
        if ctx.now - started > _STALE_S:
            continue  # 已中断：交给 history 段显示成 cancelled
        out.append(_row_to_item(r, ctx.now))
    return out


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    stale_before = ctx.now - _STALE_S
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, purpose, title, status, started, finished, ok, error, ref"
        " FROM main_runs"
        " WHERE (status != 'running' AND COALESCE(NULLIF(finished,0), started) >= ?)"
        "    OR (status = 'running' AND started <= ? AND started >= ?)"
        " ORDER BY started DESC LIMIT 4000",
        # 过期 running 显示成「在 started 那刻中断」，跟别的历史一样受时间窗限制
        (since, stale_before, since),
    ):
        if not _visible(ctx, r):
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    try:
        rid = int(item_id)
    except (TypeError, ValueError):
        return None
    r = ctx.fetch_one(
        "SELECT id, group_id, purpose, title, status, started, finished, ok, error, ref"
        " FROM main_runs WHERE id=?",
        (rid,),
    )
    if r is None:
        return None
    if not _visible(ctx, r):
        return None
    return _row_to_item(r, ctx.now)
