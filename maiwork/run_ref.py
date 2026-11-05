"""「这一轮」标记（run_ref）：把一串模型 / 工具 / 判断调用精确串到一件活上。

机制是 contextvars：在 `tagged(ref)` 或主模型条目 `MainRuns.scope` / `auto`
打开的区间里，`CURRENT_REF` 被设成这一轮的标记；`models._log_attempt` /
`tools._persist` / `jev` 写 `model_calls` / `tool_calls` / `judgments` 时读它落
`run_ref` 列；`create_task` 派生的协程继承 context，子 agent 的调用也挂在同一轮。

两种用法：

- `tagged(ref)`：只打标不开条目——资讯轮 / 构想轮 / 开话题轮用
  （`new("news", gid)` 造 `news:<gid>:<ms>:<seq>`）。轮里的调用不算「主模型
  独立判断」，不开 `main_runs` 条目；看板按条目行上存的同一份 ref 直连。
- `main_run.MainRuns.scope(...)` / `main_run.auto(...)`：开 `main_runs` 条目
  的同时把 `CURRENT_REF` 设成条目 `ref`（`main:<purpose>:<gid>:<ms>:<seq>`）。

`current()` 在任何调用点拿现行标记；没开任何 scope 就是 `""`（调用照旧，
只是看板那条「过程」拼不进来）。
"""

from __future__ import annotations

import contextvars
import itertools
import logging
import threading
from typing import Any

from . import clock

logger = logging.getLogger("maiwork.run_ref")

_REF_SEQ = itertools.count(1)

# 正在跑的轮（ref → {ref, lane, group_id, title, task_id, started}）。看板的
# round 源从这里读「正在跑」条目；轮收尾（scope 退出）就摘掉。线程安全：
# 主循环协程和后台任务都可能在轮里。
_ACTIVE_ROUNDS: dict[str, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()

# 当前这条协程正挂在哪一轮下（ref 串）。models._log_attempt / tools._persist /
# jev 写 model_calls / tool_calls / judgments 时读它；没挂就是 ""。
CURRENT_REF: contextvars.ContextVar[str] = contextvars.ContextVar(
    "maiwork_main_run_ref", default="")


def current() -> str:
    """这次协程现行的轮标记；没开 scope 也没自动开就是 ``""``。"""
    try:
        return str(CURRENT_REF.get() or "")
    except Exception:
        return ""


def detach() -> None:
    """在**新起的后台任务里**第一句调：把继承来的轮标记清掉。

    `create_task` 会复制当时的 context：管理员对话一轮里点「重新整理画像」起的长活
    会带着那一轮的 ref 跑完（调用挂错条目、`main_run.auto` 也因此不给它自己开条目），
    哪怕那一轮早就收尾了。新任务的 context 是复制品，这里清掉不影响父任务。
    """
    try:
        CURRENT_REF.set("")
    except Exception:
        pass


async def detached(coro: Any) -> Any:
    """包一层：先 `detach()` 再跑 coro。给「交出去就不管」的后台派工用。"""
    detach()
    return await coro


def new(kind: str, group_id: str) -> str:
    """造一条轮标记：``<kind>:<gid>:<ms>:<seq>``（和 main_runs 的 ref 同一形状）。"""
    return (f"{str(kind or '')[:40]}:{str(group_id or '')[:40]}:"
            f"{int(clock.now() * 1000)}:{next(_REF_SEQ)}")


def tagged(ref: str, *, lane: str = "", group_id: str = "",
           title: str = "", task_id: str = "") -> "_TaggedScope":
    """只打标不开条目的 async scope：设 ``CURRENT_REF=<ref>``，不插 main_runs 行。

    用于「这一轮不算主模型独立判断、但过程要精确串起来」的轮（资讯轮 / 构想轮 /
    开话题轮 / 目标检查 / 交接单自带的轮）：轮里的 model_calls / tool_calls
    自动挂这个 ref，看板按条目上存的同一份 ref 直连（link=exact）。在 scope
    里 agent=main 的调用也不会自动开 main_runs（`models.chat` 见 `current()`
    非空就跳过自动开）。

    lane + group_id 都给了 → 一进 scope 就登记「正在跑」（看板 round 源上板）；
    要过了前置检查才上板的轮（topics.check / feeds），先 `tagged(ref)` 进来、
    检查过后调 `scope.activate(lane=…, group_id=…)`。交接单那种不上板的轮
    （`handoff:<id>`）不传 lane / group_id 就行。
    """
    return _TaggedScope(
        str(ref or ""), lane=str(lane or ""), group_id=str(group_id or ""),
        title=str(title or ""), task_id=str(task_id or ""))


def active_rounds() -> list[dict[str, Any]]:
    """正在跑的轮的名册（浅拷贝；看板读，别处别改里面的字段）。"""
    with _ACTIVE_LOCK:
        return [dict(v) for v in _ACTIVE_ROUNDS.values()]


def get_active_round(ref: str) -> dict[str, Any] | None:
    ref = str(ref or "")
    if not ref:
        return None
    with _ACTIVE_LOCK:
        info = _ACTIVE_ROUNDS.get(ref)
        return dict(info) if info is not None else None


class _TaggedScope:
    _TITLE_MAX = 80

    def __init__(self, ref: str, *, lane: str = "", group_id: str = "",
                 title: str = "", task_id: str = "") -> None:
        self._ref = ref
        self._token: contextvars.Token | None = None
        self._entry: dict[str, Any] | None = None
        if ref and lane and group_id:
            self._pending = {"lane": lane, "group_id": group_id,
                             "title": title, "task_id": task_id}
        else:
            self._pending = None

    @property
    def ref(self) -> str:
        return self._ref

    def activate(self, *, lane: str, group_id: str, title: str = "",
                 task_id: str = "") -> None:
        """后置登记「正在跑」：前置检查都过了、真要干活了再调。

        ref / lane / group_id 任一为空就不登记（上板的轮才需要齐这三样）。
        幂等：重复调用只更新可见字段。
        """
        if not (self._ref and str(lane or "") and str(group_id or "")):
            return
        entry = {
            "ref": self._ref,
            "lane": str(lane or "")[:40],
            "group_id": str(group_id or "")[:40],
            "title": str(title or "")[: self._TITLE_MAX],
            "task_id": str(task_id or "")[:80],
            "started": clock.now(),
        }
        with _ACTIVE_LOCK:
            old = _ACTIVE_ROUNDS.get(self._ref)
            if old is not None:
                entry["started"] = old.get("started") or entry["started"]
            _ACTIVE_ROUNDS[self._ref] = entry
        self._entry = entry

    async def __aenter__(self) -> "_TaggedScope":
        if self._ref:
            self._token = CURRENT_REF.set(self._ref)
        if self._pending is not None:
            self.activate(
                lane=str(self._pending.get("lane") or ""),
                group_id=str(self._pending.get("group_id") or ""),
                title=str(self._pending.get("title") or ""),
                task_id=str(self._pending.get("task_id") or ""),
            )
            self._pending = None
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if self._token is not None:
                CURRENT_REF.reset(self._token)
        except Exception:
            logger.exception("复位 round ref 失败")
        try:
            if self._ref:
                with _ACTIVE_LOCK:
                    _ACTIVE_ROUNDS.pop(self._ref, None)
        except Exception:
            logger.exception("摘掉轮名册失败")
        return False  # 不吞异常
