"""Planner before_request 的最小改写边界。"""
from __future__ import annotations

from typing import Any, Dict


async def run_before_request(processor: Any, *, enabled: bool, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """只允许替换 items；其余 planner 参数原样保留。"""
    if not enabled:
        return {"action": "continue"}
    items = kwargs.get("items")
    if not isinstance(items, list):
        return {"action": "continue"}
    processed = await processor.process(items)
    if processed is None or processed == items:
        return {"action": "continue"}
    modified_kwargs = dict(kwargs)
    modified_kwargs["items"] = processed
    return {"action": "continue", "modified_kwargs": modified_kwargs}
