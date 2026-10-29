"""任务看板（GET /api/board）：管理员一眼看清「谁在干什么、刚做了什么」。

对外只有三件事（server.py 只接线）：
- `board_view(svc, hours=, now=)` → `{now, hours, stuck_after_s, lanes}`；
- `item_view(svc, source, id, after=, now=)` → `{item, link, steps}`；
- `normalize_hours`（hours 参数夹 1..168）。

`lanes`：每专岗一栏（main → news → idea → goal → task → topic → 自定义
c_*），各栏 `{kind, kind_title, fish_seed, running, history, counts}`；
`running` 现在没结束的、`history` 窗口内结束的（`?hours=`，默认配置
`console.board_hours`=24）。`item_view`：单件的逐步过程（tool_calls +
model_calls 文本 + Jev 结论摘要，按 ts 升序，最多 500 步；`?after=` 增量）。
**同一件活只有一种形状**：详情的 item 和 lanes 里那件逐字段一致（详情可
多 error / counts / last_step）；link 只由实际用的钥匙决定（window →
time_window，task_id / run_ref → exact，没有覆盖）。

「这一步属于哪一件」只在 `trace.py`：三把钥匙（task_id / run_ref /
同群+时间窗）统一成 `Trace`；适配器（`sources/`）只负责给钥匙和 Item，
不认识另一类的表。第一版的 `running / agents / recent / agent_fish`
四个顶层字段已删（网页不用，每 10 秒白查一遍库）；小鱼种子在 `lanes[].fish_seed`。

隐私：只放标题和一句中文说明；不放成员原话、构想依据（basis）、QQ 号 /
requester_id、消息正文；个人向构想标题一律「个人构想」。查不到 / 表还没建
→ 当空算，绝不 500。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import views
from .context import Ctx, DEFAULT_HOURS, STUCK_AFTER_S, normalize_hours
from .trace import Trace, batch_counts, batch_last_step, steps as _trace_steps
from .sources import goal as goal_src
from .sources import handoff as handoff_src
from .sources import idea as idea_src
from .sources import main as main_src
from .sources import news as news_src
from .sources import task as task_src
from .sources import topic as topic_src

logger = logging.getLogger("maiwork.console.board")

HISTORY_LIMIT = 50           # 每条 lane 的历史最多多少条

__all__ = ["board_view", "item_view", "normalize_hours"]

_SOURCES = {
    "handoff": handoff_src,
    "task": task_src,
    "news_batch": news_src,
    "idea": idea_src,
    "goal": goal_src,
    "topic": topic_src,
    "main": main_src,
}


def board_view(svc: Any, *, hours: Any = None, now: float | None = None) -> dict[str, Any]:
    """拼 /api/board 的返回。hours=None 时用配置 console.board_hours；now 可注入。"""
    settings = svc.get_settings() if svc is not None else None
    default_hours = int(getattr(getattr(settings, "console", None), "board_hours", DEFAULT_HOURS) or DEFAULT_HOURS)
    hours_i = normalize_hours(hours, default_hours)
    ctx = Ctx(svc, now=now)
    since = ctx.now - hours_i * 3600

    running_items: list[dict[str, Any]] = []
    history_items: list[dict[str, Any]] = []
    for src in _SOURCES.values():
        running_items.extend(src.active(ctx))
        history_items.extend(src.finished(ctx, since))

    # 批量：每个 Trace 的「几步工具 / 几步思考 / 最后一步」
    all_items = running_items + history_items
    traces = [it["trace"] for it in all_items]
    counts_map = batch_counts(ctx, traces)
    last_map = batch_last_step(ctx, traces)
    for it, tr in zip(all_items, traces):
        it["_trace_key"] = tr.key
        c = counts_map.get(tr.key) or {}
        it["counts"] = {"tools": int(c.get("tools") or 0), "thinks": int(c.get("thinks") or 0)}
        it["last_step"] = last_map.get(tr.key)

    lanes: dict[str, dict[str, Any]] = {}
    for kind in ctx.lane_kinds():
        lanes[kind] = {
            "kind": kind,
            "kind_title": ctx.lane_title(kind),
            "fish_seed": ctx.fish.get(kind, ""),
            "running": [],
            "history": [],
            "counts": {"running": 0, "done": 0, "failed": 0},
        }
    for it in running_items:
        lane = lanes.get(it["lane"]) or lanes.setdefault(it["lane"], {
            "kind": it["lane"], "kind_title": ctx.lane_title(it["lane"]),
            "fish_seed": ctx.fish.get(it["lane"], ""),
            "running": [], "history": [], "counts": {"running": 0, "done": 0, "failed": 0},
        })
        lane["running"].append(_lane_out(ctx, it))
        lane["counts"]["running"] += 1
    for it in history_items:
        lane = lanes.get(it["lane"])
        if lane is None:
            continue
        if len(lane["history"]) >= HISTORY_LIMIT:
            lane["counts"]["done" if it["ok"] is not False else "failed"] += 1
            continue
        lane["history"].append(_lane_out(ctx, it))
        lane["counts"]["done" if it["ok"] is not False else "failed"] += 1
    for lane in lanes.values():
        lane["running"].sort(key=lambda x: (-float(x["updated"] or 0), str(x["id"])))
        lane["history"].sort(key=lambda x: (-float(x["finished"] or x["updated"] or 0), str(x["id"])))

    return {
        "now": ctx.now,
        "hours": hours_i,
        "stuck_after_s": STUCK_AFTER_S,
        "lanes": [lanes[k] for k in ctx.lane_kinds() if k in lanes],
    }


def _lane_out(ctx: Ctx, it: dict[str, Any]) -> dict[str, Any]:
    """Item → lanes 里那件：去内部字段、剥 error（只在详情）、补 counts/last_step 兜底。"""
    out = ctx.finish_item(it)
    out.pop("error", None)        # error 只在详情 item；lanes 不放
    out.setdefault("counts", {"tools": 0, "thinks": 0})
    out.setdefault("last_step", None)
    return out


def _counts_last(ctx: Ctx, trace: Trace) -> tuple[dict[str, int], dict[str, Any] | None]:
    """单件的 counts / last_step（详情用，和 lanes 段同一份批量口径）。"""
    counts = batch_counts(ctx, [trace]).get(trace.key) or {}
    last = batch_last_step(ctx, [trace]).get(trace.key)
    return ({"tools": int(counts.get("tools") or 0), "thinks": int(counts.get("thinks") or 0)}, last)


def item_view(svc: Any, source: Any, item_id: Any, *, after: Any = None,
              now: float | None = None) -> dict[str, Any] | None:
    """单件的逐步过程。找不到 / 非服务群 / 不认的 source → None（路由层转 404）。

    item 和 lanes 里那件同形：同一套 _row_to_item + finish_item，另补
    counts / last_step（lanes 段也批量算它）；error 详情保留。
    """
    source_s = str(source or "").strip()
    id_s = str(item_id or "").strip()
    if not source_s or not id_s:
        return None
    src = _SOURCES.get(source_s)
    if src is None:
        return None
    ctx = Ctx(svc, now=now)
    after_f = 0.0
    try:
        after_f = float(str(after or "0")) if after not in (None, "") else 0.0
    except (TypeError, ValueError):
        after_f = 0.0

    item = src.get(ctx, id_s)
    if item is None:
        return None
    trace = item["trace"]
    extra = item.get("_extra_think")
    counts, last_step = _counts_last(ctx, trace)
    out_item = ctx.finish_item(item)
    out_item["counts"] = counts
    out_item["last_step"] = last_step
    body = _trace_steps(ctx, trace, out_item, after=after_f, extra_think=extra)
    return {"item": out_item, "link": body["link"], "steps": body["steps"]}
