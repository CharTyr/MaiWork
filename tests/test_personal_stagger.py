"""个人向错开启动（线上巡检 2026-10-02）：3 个群 9 个人全挤在 09:01–09:35 跑，
和 09:00 那次收消息钩子超时撞在同一刻。改成每人一个固定的「最早几点开始」，
按 (群, 人) 散在 9:00–12:00 之间；过了这个点照旧到期，一天还是最多一次。"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.personal import personal_start_ts

from test_personal import GID, UID, _bj_ts, _make_personal, _time_patch


def test_start_spread_within_morning_and_stable() -> None:
    day = _bj_ts(2026, 8, 31, 10)
    starts = [personal_start_ts("g1", f"u{i}", day) for i in range(40)]
    lo, hi = _bj_ts(2026, 8, 31, 9), _bj_ts(2026, 8, 31, 12)
    assert all(lo <= s < hi for s in starts)
    assert len({int(s // 600) for s in starts}) >= 8, "要真散开，不能都挤一处"
    assert personal_start_ts("g1", "u1", day) == personal_start_ts("g1", "u1", day + 3600)
    # 不同天各自的 9 点起算
    assert personal_start_ts("g1", "u1", day + 86400) - personal_start_ts("g1", "u1", day) == 86400


def test_due_waits_until_own_start(tmp_path) -> None:
    store, settings, personal, *_ = _make_personal(tmp_path)
    start = personal_start_ts(GID, UID, _bj_ts(2026, 8, 31, 10))
    with _time_patch():
        if start > _bj_ts(2026, 8, 31, 9):
            assert personal.due(GID, start - 1) == []
        assert personal.due(GID, start) == [UID]
        assert personal.due(GID, _bj_ts(2026, 8, 31, 15)) == [UID]
