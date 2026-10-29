"""目标适配器（goals）：一件目标 = 一件活，归 goal 栏。

- active：state=active（进行中）；没有「卡住」概念（stuck 恒 False）。
- finished：updated 在窗口内的行（新立 active + done/cancelled 都进历史；
  active 行在历史里是「新立」一条目，finished=None / ok=None）。
- trace：目标检查走 task_id `goal-check:<id>`（协调器历来这么挂；精确）。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _f
from ..trace import Trace

SOURCE = "goal"

_STATUS_TEXT = {
    "active": "进行中", "paused": "已暂停", "done": "完成了", "cancelled": "已取消",
}


def _row_to_item(r: Any, now: float) -> dict[str, Any]:
    created = _f(r["created"])
    updated = _f(r["updated"]) or created
    state = str(r["state"] or "")
    item = {
        "source": SOURCE, "id": str(r["id"]), "lane": "goal",
        "group_id": str(r["group_id"] or ""),
        "title": str(r["title"] or ""),
        "status": state, "status_text": _STATUS_TEXT.get(state, state),
        "phase": "",
        "started": created,
        "finished": updated if state in ("done", "cancelled") else None,
        "updated": updated,
        "ok": (True if state == "done" else (False if state == "cancelled" else None)),
        "stuck": False,
        "task_id": f"goal-check:{r['id']}",
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    return Trace(kind="task_id", value=str(item["task_id"]), group_id=item["group_id"],
                 start=item["started"], end=item["finished"] or now)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    """目标没有「在跑」段：进行中（active）的目标本来就是一个持续状态，
    看板不把它当「正在做的事」放 running；它随窗口内 updated 进 history
    （新立 active = 「立了个目标」的条目，终态也是 history）。"""
    return []


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, title, state, created, updated FROM goals"
        " WHERE updated >= ? ORDER BY updated DESC LIMIT 4000",
        (since,),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    r = ctx.fetch_one(
        "SELECT id, group_id, title, state, created, updated FROM goals WHERE id=?",
        (str(item_id or ""),),
    )
    if r is None:
        return None
    if str(r["group_id"] or "") not in ctx.served:
        return None
    return _row_to_item(r, ctx.now)
