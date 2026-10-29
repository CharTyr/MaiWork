"""主模型条目（main_runs，库号 46）：主模型每一次独立判断 / 一轮处理在库里有条目。

看板「主模型」栏就靠这张表（GET /api/board 的 lanes 里 kind=main，GET
/api/board/items/main/{id} 出 steps）。

- **条目**：开始写 `running`、结束写 `done` / `failed` / `cancelled`；进程异常中断
  留下的 `running` 由 `close_orphans`（启动时跑）标 `cancelled`，看板读取也把
  超过 `RUN_STALE_S` 还没动的按已中断显示。
- **关联**：这一轮里写的 model_calls / tool_calls 带 `run_ref` = 条目 `ref`
  （`main:<purpose>:<gid>:<ms>:<seq>`，全局唯一）。写入走 `run_ref.py` 的
  contextvars——调用方只管开 `scope` / `auto`，models / tools / jev 自己读
  `run_ref.current()` 落库，不用把 ref 挨个传下去。
- **自动开**：`models.chat` 只有一行 `async with auto(...)`；「什么时候自动记一件」
  的规则全在 `auto` 里：主模型、不属于任务（`task_id` 空）、不是核验调用、不是
  `session.triage`、当前不在任何一轮里 → 记一件；否则什么都不做。一轮多次调用的
  入口（管理员对话 _turn、反馈复盘轮、个人向资讯轮）用显式 `scope` 把这一轮的多次
  调用并进同一条目。
- **不开**：task_id 非空的调用（T-* 任务内的计划 / 验收，那条目在任务栏）、
  agent 不是 main（专岗）、session.triage（会话入口分流，热重载期自己的短事务）。
- **隐私 / 上限**：title 只说「在判断什么」（purpose 中文名，≤80 字），不放群友
  原话 / QQ 号 / 对话全文；error ≤400 字；写入任何一步失败只打日志，绝不影响
  模型调用本身。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from . import clock, run_ref

logger = logging.getLogger("maiwork.main_run")

# 超过这么久还显示 running 的条目，看板按「已中断」显示（等不到人来收尾的正常情况）
RUN_STALE_S = 6 * 3600
TITLE_MAX = 80          # main_runs.title（看板 lanes / item 都直接显示它）
_ERROR_MAX = 400        # main_runs.error 上限


def _title_for(purpose: str) -> str:
    """条目标题：purpose 中文名，认不出就「主模型处理（purpose 原文截短）」。"""
    try:
        from .console.views import purpose_name
        name = purpose_name(purpose)
    except Exception:
        name = ""
    if not name or name == str(purpose or ""):
        name = f"主模型处理（{str(purpose or '?')[:40]}）" if purpose else "主模型处理"
    return str(name)[:TITLE_MAX]


class MainRuns:
    """main_runs 的写入入口。所有写库失败只记日志（绝不影响业务）。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 开 / 收
    # ------------------------------------------------------------------

    def _begin(self, group_id: str, purpose: str, title: str = "") -> str:
        """插一条 running，返回它的 ref（也用于 model_calls / tool_calls 的 run_ref）。"""
        ref = run_ref.new(f"main:{str(purpose or '')[:60]}", group_id)
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(group_id or ""), str(purpose or "")[:200],
                     str(title or "")[:TITLE_MAX] or _title_for(purpose),
                     "running", clock.now(), 0.0, 0, "", ref),
                )
        except Exception:
            logger.exception("写 main_runs（begin）失败（purpose=%s group=%s）", purpose, group_id)
            return ""  # 连 ref 都不给：后面的落库拿不到它，这轮的调用就当没条目
        return ref

    def _finish(self, ref: str, status: str, error: str = "") -> None:
        if not ref:
            return
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "UPDATE main_runs SET status=?, finished=?, ok=?, error=? WHERE ref=?",
                    (str(status), clock.now(), 1 if status == "done" else 0,
                     str(error or "")[:_ERROR_MAX], ref),
                )
        except Exception:
            logger.exception("写 main_runs（finish %s）失败（ref=%s）", status, ref)

    # ------------------------------------------------------------------
    # scope：一轮多次调用并进同一条目（admin_chat._turn / _feedback_round / personal 轮）
    # ------------------------------------------------------------------

    def scope(self, group_id: str, purpose: str, title: str = "") -> "_MainRunScope":
        """async with 上下文：进去开条目、CURRENT_REF 设成它的 ref；出来按结局写终态。

        在里面发生的每次 models.chat / tools.call 都会自动挂这条 ref（含别的
        agent 的调用——这一轮里专岗调用也是这一轮的一部分）。
        """
        return _MainRunScope(self, group_id=group_id, purpose=purpose, title=title)

    def close_orphans(self, reason: str = "进程中断，没能收尾") -> int:
        """启动收尾：上次进程留下的 running 全部标 cancelled（幂等）。返回标了几条。"""
        try:
            with self._store.tx() as conn:
                cur = conn.execute(
                    "UPDATE main_runs SET status='cancelled', finished=?, ok=0, error=?"
                    " WHERE status='running'",
                    (clock.now(), str(reason or "")[:_ERROR_MAX]),
                )
                return int(cur.rowcount or 0)
        except Exception:
            logger.exception("收尾 main_runs 遗留 running 失败")
            return 0


def auto(store: Any, *, agent: str, role: str = "", task_id: str = "",
         purpose: str = "", group_id: str = "",
         verifying: bool = False) -> "_AutoScope":
    """models.chat 的自动兜底：这次调用要不要顺手记一条主模型条目。

    规则（和库号 46 上线时 models.chat 里的判断一字不差，逐条有测试守）：

    - `agent` 空时按 `role` 推（main → main，其余 → task），和 chat 的口径一致；
    - **记一件**：有效岗位是 main、task_id 空、不是核验调用（`_candidates`）、
      purpose 不是 `session.triage`、`run_ref.current()` 空（不在任何一轮里——
      显式 scope / tagged 轮里的调用不重复开）；
    - 否则什么都不做（`_AutoScope` 是空壳，进出不写库、不设标记）。

    开条目的任何一步失败（`_begin` 抛错、`__aenter__` 抛错）只记日志：
    调用照跑，只是这轮没有条目。收尾（`__aexit__`）同样：取消 / 失败 / 成功
    三个结局各写终态，写不进去只打日志。
    """
    agent_kind = str(agent or "").strip() or (
        "main" if str(role or "") == "main" else "task")
    should = (
        agent_kind == "main"
        and not str(task_id or "").strip()
        and not verifying
        and str(purpose or "") != "session.triage"
        and not run_ref.current()
    )
    scope: _MainRunScope | None = None
    if should:
        scope = _MainRunScope(MainRuns(store), group_id=str(group_id or ""),
                              purpose=str(purpose or ""), title="")
    return _AutoScope(scope)


class _AutoScope:
    """auto() 的 async 上下文：该开就开（开失败就当没开），不该开就是空壳。"""

    def __init__(self, scope: "_MainRunScope | None") -> None:
        self._scope = scope
        self._entered = False

    @property
    def ref(self) -> str:
        return self._scope.ref if self._scope is not None and self._entered else ""

    async def __aenter__(self) -> "_AutoScope":
        if self._scope is not None:
            try:
                await self._scope.__aenter__()
                self._entered = True
            except Exception:
                logger.exception("开 main_run 失败，这次调用照跑")
                self._scope = None
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._scope is not None and self._entered:
            try:
                await self._scope.__aexit__(exc_type, exc, tb)
            except Exception:
                logger.exception("收尾 main_run 失败")
        return False  # 不吞异常（含 CancelledError——照常往外抛，条目已标 cancelled）


class _MainRunScope:
    def __init__(self, runs: MainRuns, *, group_id: str, purpose: str, title: str = "") -> None:
        self._runs = runs
        self._gid = str(group_id or "")
        self._purpose = str(purpose or "")
        self._title = str(title or "")
        self._token: contextvars.Token | None = None
        self._ref = ""

    @property
    def ref(self) -> str:
        return self._ref

    async def __aenter__(self) -> "_MainRunScope":
        self._ref = self._runs._begin(self._gid, self._purpose, self._title)
        if self._ref:
            self._token = run_ref.CURRENT_REF.set(self._ref)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if self._token is not None:
                run_ref.CURRENT_REF.reset(self._token)
        except Exception:
            logger.exception("复位 main_run ref 失败")
        if not self._ref:
            return False
        if exc is None:
            self._runs._finish(self._ref, "done")
        elif isinstance(exc, asyncio.CancelledError):
            self._runs._finish(self._ref, "cancelled", "被取消（热重载 / 停用）")
        else:
            self._runs._finish(self._ref, "failed", f"{type(exc).__name__}: {exc}")
        return False  # 不吞异常
