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
import threading
from typing import Any

logger = logging.getLogger("maiwork.discovery")

# task_id -> {"order": [url_key...], "by_key": {url_key: candidate}}
_RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


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


def close_run(task_id: str) -> list[dict]:
    tid = str(task_id or "")
    with _LOCK:
        run = _RUNS.pop(tid, None)
    if run is None:
        return []
    return [run["by_key"][key] for key in run["order"]]
