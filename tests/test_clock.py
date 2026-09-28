"""clock.py 单元测试：北京时间换算、跨午夜时段。"""

from __future__ import annotations

from datetime import datetime, timezone

from CharTyr_MaiWork.maiwork.clock import BJ, bj, day_key, in_range, now, parse_hhmm_range


def _utc(y, mo, d, h, mi, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


class TestBj:
    def test_epoch_to_beijing(self) -> None:
        # 2026-01-02 00:30 北京时间 = 2026-01-01 16:30 UTC
        ts = _utc(2026, 1, 1, 16, 30)
        t = bj(ts)
        assert t.tzinfo == BJ
        assert (t.year, t.month, t.day, t.hour, t.minute) == (2026, 1, 2, 0, 30)

    def test_bj_is_utc8(self) -> None:
        ts = _utc(2026, 6, 1, 4, 0)
        assert bj(ts).utcoffset().total_seconds() == 8 * 3600

    def test_day_key_uses_beijing(self) -> None:
        # UTC 16:30 → 北京次日 00:30
        assert day_key(_utc(2026, 1, 1, 16, 30)) == "2026-01-02"
        assert day_key(_utc(2026, 1, 1, 15, 59)) == "2026-01-01"

    def test_now_is_epoch_float(self) -> None:
        ts = now()
        assert isinstance(ts, float)
        assert ts > 1_700_000_000


class TestParseRange:
    def test_cross_midnight(self) -> None:
        assert parse_hhmm_range("23:00-08:00") == (1380, 480)

    def test_same_day(self) -> None:
        assert parse_hhmm_range("09:30-18:45") == (570, 1125)

    def test_zero(self) -> None:
        assert parse_hhmm_range("00:00-00:00") == (0, 0)


class TestInRange:
    def test_cross_midnight_inside(self) -> None:
        rng = parse_hhmm_range("23:00-08:00")
        # 北京 23:30 = UTC 15:30
        assert in_range(_utc(2026, 1, 1, 15, 30), rng) is True
        # 北京 03:00（次日）= UTC 19:00（前一天 03:00 = 当天 UTC 19:00）
        assert in_range(_utc(2026, 1, 1, 19, 0), rng) is True
        # 边界：正好 23:00（北京）属于范围；正好 08:00（北京）不属于
        assert in_range(_utc(2026, 1, 1, 15, 0), rng) is True    # 北京 23:00
        assert in_range(_utc(2026, 1, 2, 0, 0), rng) is False   # 北京 08:00（开区间）

    def test_cross_midnight_outside(self) -> None:
        rng = parse_hhmm_range("23:00-08:00")
        # 北京 12:00 = UTC 04:00
        assert in_range(_utc(2026, 1, 1, 4, 0), rng) is False
        # 北京 22:59 = UTC 14:59
        assert in_range(_utc(2026, 1, 1, 14, 59), rng) is False
        # 北京 08:01 = UTC 00:01
        assert in_range(_utc(2026, 1, 2, 0, 1), rng) is False

    def test_start_equals_end_is_all_day(self) -> None:
        rng = parse_hhmm_range("00:00-00:00")
        assert in_range(_utc(2026, 1, 1, 0, 0), rng) is True
        assert in_range(_utc(2026, 1, 1, 12, 0), rng) is True

    def test_same_day_range(self) -> None:
        rng = parse_hhmm_range("09:00-18:00")
        # 北京 10:00 = UTC 02:00
        assert in_range(_utc(2026, 1, 1, 2, 0), rng) is True
        # 北京 20:00 = UTC 12:00
        assert in_range(_utc(2026, 1, 1, 12, 0), rng) is False
