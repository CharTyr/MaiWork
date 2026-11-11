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

import json
import logging
from typing import Any

from .context import DEFAULT_HOURS, STUCK_AFTER_S, Ctx, _clip, _f, normalize_hours
from .sources import goal as goal_src
from .sources import handoff as handoff_src
from .sources import idea as idea_src
from .sources import main as main_src
from .sources import news as news_src
from .sources import round as round_src
from .sources import task as task_src
from .sources import topic as topic_src
from .trace import Trace, batch_counts, batch_last_step
from .trace import steps as _trace_steps

logger = logging.getLogger("maiwork.console.board")

HISTORY_LIMIT = 50           # 每条 lane 的历史最多多少条
BOARD_NOTES_LIMIT = 50       # 顶层 notes：窗口内最近的便条上限
PARENT_NOTES_LIMIT = 50      # 父卡详情聚合的子单便条上限

__all__ = ["board_view", "item_view", "normalize_hours"]

_SOURCES = {
    "handoff": handoff_src,
    "task": task_src,
    "news_batch": news_src,
    "idea": idea_src,
    "goal": goal_src,
    "topic": topic_src,
    "main": main_src,
    "round": round_src,
}


def _parent_key(item: dict[str, Any]) -> tuple[str, str] | None:
    """这件（非交接单）认领子交接单的精确库键：(列名, 值)。

    只认写死的三种挂法，不搞模糊猜（run_ref 空的老单子一律留独立卡）：
    - task / goal：agent_handoffs.task_id == item.task_id（goal 的是 goal-check:<id>）
    - news_batch / idea：agent_handoffs.run_ref == item._run_ref（非空才算）
    - round（lane 是 news / idea / goal）：agent_handoffs.run_ref == 轮的 ref
    """
    source = str(item.get("source") or "")
    if source in ("task", "goal"):
        tid = str(item.get("task_id") or "")
        return ("task_id", tid) if tid else None
    if source in ("news_batch", "idea"):
        ref = str(item.get("_run_ref") or "")
        return ("run_ref", ref) if ref else None
    if source == "round" and str(item.get("lane") or "") in ("news", "idea", "goal"):
        ref = str(item.get("_run_ref") or item.get("id") or "")
        return ("run_ref", ref) if ref else None
    return None


def _child_stats(ctx: Ctx, items: list[dict[str, Any]]
                 ) -> dict[int, tuple[int, int]]:
    """id(item) → (handoff_count, note_count)：子交接单数 + 挂上去的便条数。

    handoff 本身数自己那张单的便条；task / goal / news_batch / idea / round
    按 _parent_key 的子单（含跨栏的 c_*）数；其它来源恒 0。不按 since 限：
    卡片统计的是「这件总共有几单几条」，和详情同源 → 同形。
    """
    out: dict[int, tuple[int, int]] = {}
    if ctx.conn is None:
        return out
    # 父卡：(列, 值) 分组成一次 IN 查询；交接单：(group_id, id) → note 数
    by_col: dict[str, dict[str, list[tuple[str, int]]]] = {}
    hid_index: dict[tuple[str, str], list[int]] = {}
    for it in items:
        if str(it.get("source") or "") == "handoff":
            key = (str(it.get("group_id") or ""), str(it.get("id") or ""))
            hid_index.setdefault(key, []).append(id(it))
            continue
        pk = _parent_key(it)
        if pk is not None:
            by_col.setdefault(pk[0], {}).setdefault(pk[1], []).append(
                (str(it.get("group_id") or ""), id(it)))

    # 子单（id, group_id, col值）一把全捞，按库键回挂到父卡
    for col, val_map in by_col.items():
        marks = ",".join("?" * len(val_map))
        rows = ctx.fetch(
            f"SELECT id, group_id, {col} AS pkey FROM agent_handoffs WHERE {col} IN ({marks})",
            tuple(val_map.keys()),
        )
        for val, holders in val_map.items():
            kids = [(str(r["id"]), str(r["group_id"] or ""))
                    for r in rows if str(r["pkey"] or "") == val]
            for gid, iid in holders:
                mine = [hid for hid, hgid in kids if hgid == gid]
                out[iid] = (len(mine), _notes_count(ctx, gid, mine))

    # 交接单自己的 note_count（按群分组查，同 id 跨群不串）
    gids = sorted({k[0] for k in hid_index})
    for gid in gids:
        hids = sorted(h for g, h in hid_index if g == gid)
        if not hids:
            continue
        marks = ",".join("?" * len(hids))
        counts = {
            str(r["handoff_id"]): int(r["n"] or 0)
            for r in ctx.fetch(
                f"SELECT handoff_id, COUNT(*) AS n FROM agent_notes"
                f" WHERE group_id=? AND handoff_id IN ({marks}) GROUP BY handoff_id",
                (gid, *hids),
            )
        }
        for hid in hids:
            for iid in hid_index.get((gid, hid), []):
                out[iid] = (0, counts.get(hid, 0))
    return out


def _notes_count(ctx: Ctx, gid: str, handoff_ids: list[str]) -> int:
    """这组交接单下面贴了几条便条。"""
    if not handoff_ids:
        return 0
    marks = ",".join("?" * len(handoff_ids))
    row = ctx.fetch_one(
        f"SELECT COUNT(*) AS n FROM agent_notes WHERE group_id=? AND handoff_id IN ({marks})",
        (gid, *handoff_ids),
    )
    return int(row["n"]) if row else 0


def _child_rows(ctx: Ctx, item: dict[str, Any]) -> list[Any]:
    """父卡的子交接单行（按 created 升序；没有父键 → 空）。"""
    pk = _parent_key(item)
    if pk is None:
        return []
    col, val = pk
    return ctx.fetch(
        "SELECT id, kind, phase, status, summary, review, error, run_ref, created, updated"
        f" FROM agent_handoffs WHERE group_id=? AND {col}=? ORDER BY created ASC, id ASC",
        (str(item.get("group_id") or ""), val),
    )


def _rounds_of(ctx: Ctx, rows: list[Any]) -> list[dict[str, Any]]:
    """子交接单行 → 详情 rounds（第几轮、谁、什么状态、小结 / 验收 / 失败原因）。"""
    out: list[dict[str, Any]] = []
    for idx, r in enumerate(rows, start=1):
        status = str(r["status"] or "")
        out.append({
            "id": str(r["id"]),
            "no": idx,
            "kind": str(r["kind"] or ""),
            "kind_title": ctx.lane_title(str(r["kind"] or "")),
            "phase": str(r["phase"] or ""),
            "status": status,
            "status_text": handoff_src._STATUS_TEXT.get(status, status),
            "started": _f(r["created"]),
            "finished": _f(r["updated"]) if status in handoff_src._DONE else None,
            "ok": (True if status == "accepted" else (False if status in handoff_src._DONE else None)),
            "summary": _clip(str(r["summary"] or ""), 200),
            "review": _clip(str(r["review"] or ""), 200),
            "error": _clip(str(r["error"] or ""), 200),
        })
    return out


def _parent_notes(ctx: Ctx, gid: str, rows: list[Any]) -> list[dict[str, Any]]:
    """父卡详情聚合的便条：它名下所有子交接单贴上板的（按贴出顺序）。"""
    hids = [str(r["id"]) for r in rows]
    if not hids:
        return []
    marks = ",".join("?" * len(hids))
    return [
        {"from_kind": str(n["from_kind"]), "to_kind": str(n["to_kind"]),
         "body": str(n["body"]), "created": _f(n["created"])}
        for n in ctx.fetch(
            "SELECT from_kind, to_kind, body, created FROM agent_notes"
            f" WHERE group_id=? AND handoff_id IN ({marks}) ORDER BY id LIMIT {PARENT_NOTES_LIMIT}",
            (gid, *hids),
        )
    ]


def _round_map_of(rows: list[Any]) -> dict[str, str]:
    """共享轮 ref → 交接单号：steps 的 round_id 标哪轮用（handoff:<id> 的不查表）。"""
    out: dict[str, str] = {}
    for r in rows:
        ref = str(r["run_ref"] or "") if "run_ref" in r.keys() else ""
        if ref and not ref.startswith("handoff:"):
            out[ref] = str(r["id"])
    return out


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

    all_items = running_items + history_items

    # 子交接单折叠：同栏且能认到板上爸的手单，收进爸的卡片（rounds）里；
    # 还在跑的单不藏进已结束的爸（跑着的活必须留一眼看到）。
    parents_by_lane: dict[str, dict[tuple, bool]] = {}
    for it in all_items:
        pk = _parent_key(it)
        if pk is None:
            continue
        key = (str(it.get("group_id") or ""), pk[0], pk[1])
        lane_map = parents_by_lane.setdefault(str(it.get("lane") or ""), {})
        # 同键多张父卡：全结束才算「爸都走了」（还有活爸 → 跑着的单照常折进去）
        lane_map[key] = lane_map.get(key, True) and (it.get("finished") is not None)

    def _folded(it: dict[str, Any]) -> bool:
        if str(it.get("source") or "") != "handoff":
            return False
        lane_map = parents_by_lane.get(str(it.get("lane") or ""), {})
        gid = str(it.get("group_id") or "")
        for col in ("task_id", "run_ref"):
            val = str(it.get("task_id") or "") if col == "task_id" else str(it.get("_run_ref") or "")
            k = (gid, col, val)
            if not val or k not in lane_map:
                continue
            if it.get("finished") is None and lane_map[k]:
                continue    # 活单不藏进已结束的爸
            return True
        return False

    visible_running = [it for it in running_items if not _folded(it)]
    visible_history = [it for it in history_items if not _folded(it)]

    # 批量：每个 Trace 的「几步工具 / 几步思考 / 最后一步」
    traces = [it["trace"] for it in all_items]
    counts_map = batch_counts(ctx, traces)
    last_map = batch_last_step(ctx, traces)
    stats_map = _child_stats(ctx, all_items)
    for it, tr in zip(all_items, traces):
        it["_trace_key"] = tr.key
        c = counts_map.get(tr.key) or {}
        it["counts"] = {"tools": int(c.get("tools") or 0), "thinks": int(c.get("thinks") or 0)}
        it["last_step"] = last_map.get(tr.key)
        hc, nc = stats_map.get(id(it), (0, 0))
        it["handoff_count"] = hc
        it["note_count"] = nc

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
    for it in visible_running:
        lane = lanes.get(it["lane"]) or lanes.setdefault(it["lane"], {
            "kind": it["lane"], "kind_title": ctx.lane_title(it["lane"]),
            "fish_seed": ctx.fish.get(it["lane"], ""),
            "running": [], "history": [], "counts": {"running": 0, "done": 0, "failed": 0},
        })
        lane["running"].append(_lane_out(ctx, it))
        lane["counts"]["running"] += 1
    for it in visible_history:
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
        "notes": _board_notes(ctx, since),
        # 「正在做什么」实时块（库号 51，runtime_bus）：SSE 推送之外的首屏快照；
        # 没接总线给空壳（形状不变），事件里只补群名，绝不加内容字段
        "live": _live_view(ctx, svc),
    }


def _live_view(ctx: Ctx, svc: Any) -> dict[str, Any]:
    """runtime_bus.snapshot() 的看板形状；没接总线 → 空的同形壳（绝不 500）。"""
    bus = getattr(svc, "runtime_bus", None) if svc is not None else None
    if bus is None:
        return {"seq": 0, "ts": ctx.now, "active": [], "recent": []}
    try:
        snap = bus.snapshot()
    except Exception:
        return {"seq": 0, "ts": ctx.now, "active": [], "recent": []}
    if not isinstance(snap, dict):
        return {"seq": 0, "ts": ctx.now, "active": [], "recent": []}
    # 给事件补群名（复制着改，不动总线里的那份）
    recent = []
    for e in list(snap.get("recent") or []):
        if isinstance(e, dict):
            ev = dict(e)
            gid = str(ev.get("group_id") or "")
            if gid and gid != "admin":
                ev["group_name"] = ctx.group_name(gid)
            recent.append(ev)
    active = []
    for f in list(snap.get("active") or []):
        if isinstance(f, dict):
            fl = dict(f)
            gid = str(fl.get("group_id") or "")
            if gid and gid != "admin":
                fl["group_name"] = ctx.group_name(gid)
            active.append(fl)
    return {
        "seq": int(snap.get("seq") or 0),
        "ts": _f(snap.get("ts") or ctx.now),
        "active": active,
        "recent": recent,
    }


def _latest_context_map(ctx: Ctx, trace: Trace) -> dict[str, Any] | None:
    """这条链路上最近一次思考（model_calls）的上下文分组地图；没有 → None。

    按 Trace 的钥匙找：task_id / run_ref / 同群+时间窗（和 steps() 同一口径，
    只取最新一条 context_map 非空的）。查不到 / 表还没建 → None（绝不 500）。
    """
    if ctx.conn is None:
        return None
    col = {
        "task_id": "task_id",
        "run_ref": "run_ref",
    }.get(trace.kind)
    try:
        if col and trace.value:
            row = ctx.fetch_one(
                f"SELECT context_map FROM model_calls WHERE {col}=? AND context_map != ''"
                " ORDER BY id DESC LIMIT 1",
                (trace.value,),
            )
        else:
            start = trace.start
            end = trace.end if trace.end else ctx.now
            row = ctx.fetch_one(
                "SELECT context_map FROM model_calls WHERE group_id=? AND ts>=? AND ts<=?"
                " AND context_map != '' ORDER BY id DESC LIMIT 1",
                (str(trace.group_id or ""), start, end),
            )
    except Exception:
        return None
    if row is None:
        return None
    raw = str(row["context_map"] or "")
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _board_notes(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    """顶层便条板：窗口内服务群贴上板的便条（新→旧，至多 50）。"""
    if not ctx.served:
        return []
    marks = ",".join("?" * len(ctx.served))
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, from_kind, to_kind, body, handoff_id, created"
        f" FROM agent_notes WHERE created >= ? AND group_id IN ({marks})"
        f" ORDER BY created DESC, id DESC LIMIT {BOARD_NOTES_LIMIT}",
        (since, *sorted(ctx.served)),
    ):
        gid = str(r["group_id"] or "")
        out.append({
            "id": int(r["id"]),
            "group_id": gid,
            "group_name": ctx.group_name(gid),
            "from_kind": str(r["from_kind"] or ""),
            "to_kind": str(r["to_kind"] or ""),
            "body": str(r["body"] or ""),
            "handoff_id": str(r["handoff_id"] or ""),
            "created": _f(r["created"]),
        })
    return out


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

    # 子交接单：父卡给 rounds / 聚合便条 / steps 的 round_id 名册；叶卡空
    children = _child_rows(ctx, item)
    rounds = _rounds_of(ctx, children)
    round_map = _round_map_of(children)
    if "_notes" in item:  # 交接单详情带自己的便条（docs/34）
        notes = item["_notes"]
    elif children:
        notes = _parent_notes(ctx, str(item.get("group_id") or ""), children)
    else:
        notes = []

    hc, nc = _child_stats(ctx, [item]).get(id(item), (0, 0))
    item["handoff_count"] = hc
    item["note_count"] = nc
    out_item = ctx.finish_item(item)
    out_item["counts"] = counts
    out_item["last_step"] = last_step
    body = _trace_steps(ctx, trace, out_item, after=after_f, extra_think=extra,
                        round_map=round_map)
    out: dict[str, Any] = {
        "item": out_item,
        "link": body["link"],
        "steps": body["steps"],
        "total_steps": body["total_steps"],
        "truncated": body["truncated"],
        "rounds": rounds,
        "notes": notes,
    }
    # 最近一次思考的上下文地图（库号 51）：有数据才挂——老库 / 没有 model_calls
    # 的详情保持原形状（行为不变黄金对照过）
    ctx_map = _latest_context_map(ctx, trace)
    if ctx_map is not None:
        out["context_map"] = ctx_map
    return out
