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
from typing import Any

from . import clock

logger = logging.getLogger("maiwork.run_ref")

_REF_SEQ = itertools.count(1)

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


def tagged(ref: str) -> "_TaggedScope":
    """只打标不开条目的 async scope：设 ``CURRENT_REF=<ref>``，不插 main_runs 行。

    用于「这一轮不算主模型独立判断、但过程要精确串起来」的轮（资讯轮 / 构想轮 /
    开话题轮）：轮里的 model_calls / tool_calls 自动挂这个 ref，看板
    按条目上存的同一份 ref 直连（link=exact）。在 scope 里 agent=main 的调用
    也不会自动开 main_runs（`models.chat` 见 `current()` 非空就跳过自动开）。
    """
    return _TaggedScope(str(ref or ""))


class _TaggedScope:
    def __init__(self, ref: str) -> None:
        self._ref = ref
        self._token: contextvars.Token | None = None

    @property
    def ref(self) -> str:
        return self._ref

    async def __aenter__(self) -> "_TaggedScope":
        if self._ref:
            self._token = CURRENT_REF.set(self._ref)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if self._token is not None:
                CURRENT_REF.reset(self._token)
        except Exception:
            logger.exception("复位 round ref 失败")
        return False  # 不吞异常
