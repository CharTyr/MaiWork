"""撒网登记簿（两阶段找资讯，「广撒网再挑着打开」）：候选从程序拿，不信子 agent 交回。

两阶段的第一阶段（撒网子 agent）只许用 web_search；它每次搜出来的结果由
web_search 工具 handler（tools_builtin.py）在 task_id 有开着的 run 时顺手记进来，
feeds.py 在子 agent 跑完后 close_run 一次性取走。

口径：
- open_run(task_id)：开一个登记簿（同名再开 = 清掉重来）；
- record(task_id, *, query, focus, provider, results)：记一次搜索的结果。
  results 是 search.py 归一化后的 [{title, url, snippet, published, provider?...}]；
  focus 是关注点编号（int），没有就 None；没开着的 task_id 直接忽略（不炸）；
- close_run(task_id) -> [ {title, url, snippet, published, query, focus, provider, queries} ]：
  取走并关掉。按规范化链接去重（feeds._normalize_url），同一链接只留先见的那条，
  别的问法攒进它的 queries（原序去重）。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger("maiwork.discovery")

# task_id -> {"order": [url_key...], "by_key": {url_key: candidate}}
_RUNS: dict[str, dict[str, Any]] = {}
# task_id -> {规范化链接: 第一次见到它的工具名}（「本轮工具输出里真出现过」登记簿；
# 补找防编造用——子 agent 交回的链接必须在这本账里才算数）
_SEEN: dict[str, dict[str, str]] = {}
_LOCK = threading.Lock()

_URL_RE = re.compile(r"https?://[^\s<>\)\]'\"“”‘’，。；]+", re.IGNORECASE)


def _normalize(url: str) -> str:
    """链接规范化键（复用 feeds 的；拿到空串 = 这条不能算候选）。"""
    from .feeds import _normalize_url

    return _normalize_url(url)


def open_run(task_id: str) -> None:
    tid = str(task_id or "")
    if not tid:
        return
    with _LOCK:
        _RUNS[tid] = {"order": [], "by_key": {}}


def record(task_id: str, *, query: str, focus: Any, provider: str, results: Any) -> None:
    tid = str(task_id or "")
    if not tid:
        return
    try:
        focus_i = int(focus) if focus is not None else None
    except (TypeError, ValueError):
        focus_i = None
    q = str(query or "")
    with _LOCK:
        run = _RUNS.get(tid)
        if run is None:
            return
        if not isinstance(results, list):
            return
        for raw in results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            if not url:
                continue
            key = _normalize(url)
            if not key:
                continue
            existing = run["by_key"].get(key)
            if existing is not None:
                # 同一链接：只留先见的那条；别的问法攒进 queries
                if q and q not in existing["queries"]:
                    existing["queries"].append(q)
                continue
            published = raw.get("published")
            candidate = {
                "title": str(raw.get("title") or "").strip(),
                "url": url,
                "snippet": str(raw.get("snippet") or "").strip(),
                "published": float(published) if isinstance(published, (int, float)) and published else None,
                "query": q,
                "focus": focus_i,
                "provider": str(raw.get("provider") or provider or "").strip(),
                "queries": [q] if q else [],
            }
            run["by_key"][key] = candidate
            run["order"].append(key)


def note_output(task_id: str, tool_name: str, text: Any) -> None:
    """把一次工具调用**完整输出**里出现过的 http(s) 链接记进「见过」账（task_id 开着才记）。

    供「补找」防编造：子 agent 交回的链接必须在某次工具输出里真出现过才算数。
    - task_id 没开登记簿 → 直接返回（绝大多数调用走这条，零开销）；
    - 规范化用 feeds 的同一套 _normalize_url；
    - 同一链接只记第一次见到它的工具名；
    - 任何内部异常吞掉记日志，绝不能影响工具结果本身。
    """
    tid = str(task_id or "")
    if not tid:
        return
    try:
        with _LOCK:
            if tid not in _RUNS:
                return
            seen = _SEEN.setdefault(tid, {})
        for m in _URL_RE.finditer(str(text or "")):
            key = _normalize(m.group(0))
            if not key:
                continue
            with _LOCK:
                if key not in seen:
                    seen[key] = str(tool_name or "")
    except Exception:
        logger.debug("note_output 记账失败（task %s 工具 %s），吞掉", tid, tool_name, exc_info=True)


def take_seen(task_id: str) -> dict[str, str]:
    """取走「见过」账（规范化链接 → 第一个报出它的工具名）；没开过的 → 空。"""
    tid = str(task_id or "")
    with _LOCK:
        return dict(_SEEN.get(tid) or {})


def close_run(task_id: str) -> list[dict]:
    tid = str(task_id or "")
    with _LOCK:
        run = _RUNS.pop(tid, None)
        _SEEN.pop(tid, None)  # 见过账跟登记簿同生命周期，不泄漏
    if run is None:
        return []
    return [run["by_key"][key] for key in run["order"]]
