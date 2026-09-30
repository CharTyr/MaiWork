"""核验子 agent（feeds-verify:）的打开页数账本：代码硬上限，防「找日期打转」。

为什么要有它（2026-09-30 线上实测）：一个核验子 agent 为了给一条 YouTube 视频找
发布时间，8 分钟里调了 23 次 fetch_page（invidious / piped 镜像、web.archive、
oEmbed……），提示词劝不住。所以上限做进代码：

- feeds._verify_batch 按组 open_run（上限 = cap_for(组内条数)）；
- tools_builtin 的 fetch_page handler 看到 task_id 在账本就 consume 一次，
  用完直接回拒绝话（中文，告诉子 agent 别再开、用已经打开的内容交回）；
- 任务结束 close_run，不残留；
- 没开账本的 task_id（普通任务、老模式 feeds-collect: 找资讯）一律放行。

账本只活在本进程里（内存 + 锁）：核验组几分钟就结束，重启后旧标记本来也不会再用。
"""

from __future__ import annotations

import threading
from typing import Any

# task_id -> {"cap": int, "used": int}
_RUNS: dict[str, dict[str, int]] = {}
_LOCK = threading.Lock()

# 绝对上限（防「组内条数多就无限开」；一组最多先开这么多页）
_CAP_MAX = 6

# 拒绝时回给子 agent 的话（中文、一句话；让它直接收尾交回，别再挣扎）
_REFUSE_NOTE = (
    "这组核验的打开次数已经用完，别再打开新页面了——"
    "用已经打开到的内容按格式交回；没打开的条目跳过，"
    "页面上找不到发布日期的用搜索结果自带的日期（没有就留空）。"
)


def cap_for(items_in_group: int) -> int:
    """一组任务的打开页数上限：约（组内条数 × 2）+ 1，最多 _CAP_MAX 次，最少 1 次。

    一倍的关系：每条候选开一次候选链接本身，打不开最多再换一个备用地址，
    再多一次容错 —— 超出基本就是在打转了（找镜像 / 翻存档站）。
    """
    try:
        n = int(items_in_group)
    except (TypeError, ValueError):
        n = 0
    return max(1, min(n * 2 + 1, _CAP_MAX))


def open_run(task_id: str, *, cap_page_calls: int) -> None:
    """给一个核验任务开账本（同名再开 = 清掉重来）。cap≤0 当 1。"""
    tid = str(task_id or "")
    if not tid:
        return
    try:
        cap = max(1, int(cap_page_calls))
    except (TypeError, ValueError):
        cap = 1
    with _LOCK:
        _RUNS[tid] = {"cap": cap, "used": 0}


def close_run(task_id: str) -> None:
    """任务收尾，关掉账本（不关的话旧标记残留，理论上同名复用会受影响）。"""
    tid = str(task_id or "")
    with _LOCK:
        _RUNS.pop(tid, None)


def consume(task_id: str) -> tuple[bool, str]:
    """记一次打开页数。允许 → (True, "")；超上限 → (False, 拒绝话)。

    没开账本的 task_id 一律放行（别的任务不受影响）。
    """
    tid = str(task_id or "")
    if not tid:
        return True, ""
    with _LOCK:
        run = _RUNS.get(tid)
        if run is None:
            return True, ""
        if run["used"] >= run["cap"]:
            return False, _REFUSE_NOTE
        run["used"] += 1
        return True, ""


def _status(task_id: str) -> dict[str, int] | None:
    """（调试 / 测试用）这组账本现况；没开过 → None。"""
    tid = str(task_id or "")
    with _LOCK:
        run = _RUNS.get(tid)
        return dict(run) if run is not None else None
