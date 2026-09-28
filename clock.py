"""时间相关：epoch、北京时间换算、睡觉时段判断。

存库一律用 epoch 秒（float）；展示和「几点」判断用北京时间（固定 UTC+8，不用 zoneinfo）。
测试里可以 monkeypatch clock.now。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))


def now() -> float:
    """当前时间的 epoch 秒。"""
    return time.time()


def bj(ts: float) -> datetime:
    """epoch -> 北京时间 datetime。"""
    return datetime.fromtimestamp(ts, tz=BJ)


def day_key(ts: float) -> str:
    """北京时间的 "YYYY-MM-DD"。"""
    return bj(ts).strftime("%Y-%m-%d")


def parse_hhmm_range(s: str) -> tuple[int, int]:
    '''"23:00-08:00" -> (1380, 480)，单位是分钟。'''
    start_s, end_s = str(s).split("-", 1)
    sh, sm = start_s.split(":", 1)
    eh, em = end_s.split(":", 1)
    return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)


def in_range(ts: float, rng: tuple[int, int]) -> bool:
    """ts（epoch）是否落在一天里的某时段（北京时间）。支持跨午夜。

    时段按 [start, end) 解释；start == end 或跨度 >= 24 小时视为全天。
    """
    start, end = rng
    if start == end or end - start >= 1440:
        return True
    t = bj(ts)
    m = t.hour * 60 + t.minute
    if start < end:
        return start <= m < end
    # 跨午夜：例如 23:00-08:00，23:00 之后或 08:00 之前都算
    return m >= start or m < end
