"""正在跑的轮适配器（round ref 名册，库号 50）：轮还没落库条目，但看板要
实时看到它（news / idea / goal / topic 各栏的「正在跑」）。

- active：`run_ref.active_rounds()` 里登记的轮 → 所在栏的 running 条目
  （source=round、id=ref）。只服务群；登记超过 RUN_STALE_S 的当已中断，
  不上板（名册一般靠 scope 退出摘掉，这条是兜底）。
- finished：[]（轮收尾后不再是「轮」——它的条目落在 news_batches / ideas /
  topic_log / goals / agent_handoffs / main_runs 里，由各自的源出）。
- get：活的轮直接给 running 条目（trace=run_ref → link=exact，步骤实时读
  tool_calls / model_calls）；已结束的按 ref 前缀回落到落库条目：
  news:→news_batches、idea:→ideas、topic:→topic_log、goal:→goals 行
  （goals 用 id 不是 run_ref）、handoff:→agent_handoffs.run_ref。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _f
from ..trace import Trace
from .... import run_ref

SOURCE = "round"

try:
    from ....main_run import RUN_STALE_S as _STALE_S
except Exception:  # main_run 模块缺席的半截环境：不挡看板
    _STALE_S = 6 * 3600


def _round_item(r: dict[str, Any], now: float) -> dict[str, Any]:
    started = _f(r.get("started"))
    ref = str(r.get("ref") or "")
    item = {
        "source": SOURCE, "id": ref,
        "lane": str(r.get("lane") or "main") or "main",
        "group_id": str(r.get("group_id") or ""),
        "title": str(r.get("title") or "正在跑"),
        "status": "running", "status_text": "正在跑",
        "phase": "",
        "started": started,
        "finished": None,
        "updated": started,
        "ok": None,
        "stuck": now - started > 600.0,
        "task_id": str(r.get("task_id") or ""),
        "_run_ref": ref,   # 子交接单按 run_ref 精确挂到这张活卡（折叠 / rounds / counts）
    }
    item["trace"] = Trace(kind="run_ref", value=ref,
                          group_id=item["group_id"],
                          start=started, end=now)
    return item


def active(ctx: Ctx) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        rounds = run_ref.active_rounds()
    except Exception:
        return []
    for r in rounds:
        gid = str(r.get("group_id") or "")
        if gid not in ctx.served:
            continue  # 非服务群：零暴露
        if ctx.now - _f(r.get("started")) > _STALE_S:
            continue  # 登记太久没摘掉 = 早就断了，不当「正在跑」
        out.append(_round_item(r, ctx.now))
    return out


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:  # noqa: ARG001
    return []


def _fallback_item(ctx: Ctx, ref: str) -> dict[str, Any] | None:
    """轮结束后按 ref 前缀找落库条目（source 换成那张表自己的，结果同直开条目）。"""
    if ref.startswith("news:"):
        from . import news as news_src
        r = ctx.fetch_one(
            "SELECT id, group_id, found, kept, skipped, note, created, run_ref"
            " FROM news_batches WHERE run_ref=? ORDER BY id DESC LIMIT 1",
            (ref,),
        )
        return news_src.get(ctx, r["id"]) if r is not None else None
    if ref.startswith("idea:"):
        from . import idea as idea_src
        r = ctx.fetch_one(
            "SELECT id FROM ideas WHERE run_ref=? ORDER BY id DESC LIMIT 1",
            (ref,),
        )
        return idea_src.get(ctx, r["id"]) if r is not None else None
    if ref.startswith("topic:"):
        from . import topic as topic_src
        r = ctx.fetch_one(
            "SELECT id FROM topic_log WHERE run_ref=? ORDER BY id DESC LIMIT 1",
            (ref,),
        )
        return topic_src.get(ctx, r["id"]) if r is not None else None
    if ref.startswith("goal:"):
        # goal:<goal_id>:<ms>:<seq>：第二段就是目标 id（goals 用 id 不用 run_ref）
        parts = ref.split(":")
        if len(parts) >= 2 and parts[1]:
            from . import goal as goal_src
            return goal_src.get(ctx, parts[1])
        return None
    if ref.startswith("handoff:"):
        # handoff:<交接单号>：交接单上记了同一份 ref
        from . import handoff as handoff_src
        r = ctx.fetch_one(
            "SELECT id FROM agent_handoffs WHERE run_ref=? ORDER BY updated DESC LIMIT 1",
            (ref,),
        )
        if r is None and len(ref.split(":")) >= 2:
            r = {"id": ref.split(":")[1]}
        return handoff_src.get(ctx, r["id"]) if r is not None else None
    return None


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    ref = str(item_id or "")
    if not ref:
        return None
    try:
        info = run_ref.get_active_round(ref)
    except Exception:
        info = None
    if info is not None:
        gid = str(info.get("group_id") or "")
        if gid not in ctx.served:
            return None  # 非服务群：零暴露
        return _round_item(info, ctx.now)
    return _fallback_item(ctx, ref)
