"""scheduler.py 单元测试（docs/07-代码接口.md §10.5）。

- 偏移固定可复现（同群同日同时段每次算出来一样）且在 ±jitter 内；
- 窗口内（实际时刻 ~ +45 分钟）due 报 "news" 一次，done 后不再报；
- 错过窗口（> 45 分钟）不补；
- 睡觉时段什么都不返回；
- 群画像没成形 → 空；
- idea：10:00–21:00 且群安静 ≥10 分钟才报，done 后当天不再报；
- 跨天重置（今天的 done 不影响明天）；
- next_news_ts 给下一个时段的实际时刻。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.scheduler import Scheduler
from CharTyr_MaiWork.maiwork.store import Store

BJ = timezone(timedelta(hours=8))
GID = "111"
GID_OTHER = "222"


def _bj_ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=BJ).timestamp()


def _settings(cfg: dict | None = None):
    s, _ = load_settings(cfg or {})
    return s


def _make(tmp_path, *, cfg: dict | None = None, seed: bool = True, ready: bool = True):
    store = Store(tmp_path / "t.db")
    store.migrate()
    if seed:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
                (GID, 1_700_000_000.0 if ready else 0.0),
            )
    settings = _settings(cfg)
    sched = Scheduler(store, lambda: settings)
    return store, settings, sched


# 固定一天（周二），slots 用默认 08:30 / 14:00 / 19:00，jitter 默认 ±30 分钟
Y, M, D = 2026, 9, 22


# ----------------------------------------------------------------------
# 偏移
# ----------------------------------------------------------------------


def test_offset_deterministic_and_in_range(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    now = _bj_ts(Y, M, D, 12, 0)
    a1 = sched.slot_ts(GID, now, "08:30")
    a2 = sched.slot_ts(GID, now, "08:30")
    assert a1 == a2  # 同一群同一天同一时段 → 固定
    base = _bj_ts(Y, M, D, 8, 30)
    assert base - 30 * 60 <= a1 <= base + 30 * 60
    # 不同时段（除了同一天同一时刻的巧合）一般不同；只断言都在各自范围内
    b = sched.slot_ts(GID, now, "14:00")
    base2 = _bj_ts(Y, M, D, 14, 0)
    assert base2 - 30 * 60 <= b <= base2 + 30 * 60
    # 换一天会变（用两天都落在范围内的强约束：各自独立）
    c = sched.slot_ts(GID, now + 86400, "08:30")
    base3 = base + 86400
    assert base3 - 30 * 60 <= c <= base3 + 30 * 60
    # 不同群（通常）不同偏移 —— 不强求不同，只要求独立可复现
    d1 = sched.slot_ts(GID_OTHER, now, "08:30")
    assert base - 30 * 60 <= d1 <= base + 30 * 60


def test_offset_respects_jitter_config(tmp_path) -> None:
    _, _, sched = _make(tmp_path, cfg={"feeds": {"news_jitter_minutes": 5}})
    now = _bj_ts(Y, M, D, 12, 0)
    base = _bj_ts(Y, M, D, 8, 30)
    for slot in ("08:30", "14:00", "19:00"):
        got = sched.slot_ts(GID, now, slot)
        hh, mm = int(slot.split(":")[0]), int(slot.split(":")[1])
        b = _bj_ts(Y, M, D, hh, mm)
        assert b - 5 * 60 <= got <= b + 5 * 60


# ----------------------------------------------------------------------
# news due / done
# ----------------------------------------------------------------------


def test_news_due_once_then_silent(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    slot = "14:00"
    probe = _bj_ts(Y, M, D, 12, 0)
    actual = sched.slot_ts(GID, probe, slot)
    # 实际时刻准点 → due
    assert "news" in sched.due(GID, actual)
    # 窗口内（+44 分钟）→ 仍 due（没 done 过）
    assert "news" in sched.due(GID, actual + 44 * 60)
    # done 以后窗口内不再 due
    sched.done(GID, "news", actual + 5 * 60)
    assert "news" not in sched.due(GID, actual + 10 * 60)


def test_news_missed_window_not_made_up(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    probe = _bj_ts(Y, M, D, 12, 0)
    actual = sched.slot_ts(GID, probe, "14:00")
    # 实际时刻 +46 分钟 → 不补
    assert "news" not in sched.due(GID, actual + 46 * 60)


def test_news_not_yet_time(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    probe = _bj_ts(Y, M, D, 12, 0)
    actual = sched.slot_ts(GID, probe, "14:00")
    assert "news" not in sched.due(GID, actual - 60)  # 还没到


def test_news_quiet_hours_blocks(tmp_path) -> None:
    """把时段配进睡觉时段里 → 永不触发。"""
    cfg = {
        "feeds": {"news_slots": ["02:00"], "news_jitter_minutes": 0},
        "delivery": {"quiet_hours": "23:00-08:00"},
    }
    _, _, sched = _make(tmp_path, cfg=cfg)
    assert "news" not in sched.due(GID, _bj_ts(Y, M, D, 2, 0))


def test_news_idea_quiet_hours_edge(tmp_path) -> None:
    """idea 的时间窗（10:00–21:00）本来就在睡觉时段外；这里只验证睡觉时段本身拦掉。"""
    cfg = {"delivery": {"quiet_hours": "12:00-13:00"}}
    _, _, sched = _make(tmp_path, cfg=cfg)
    noon = _bj_ts(Y, M, D, 12, 30)
    assert sched.due(GID, noon, last_msg_ts=noon - 3600) == []


def test_news_not_ready_group_returns_nothing(tmp_path) -> None:
    _, _, sched = _make(tmp_path, ready=False)
    probe = _bj_ts(Y, M, D, 12, 0)
    actual = sched.slot_ts(GID, probe, "14:00")
    assert sched.due(GID, actual) == []
    # done 也不该报错
    sched.done(GID, "news", actual)


def test_news_unknown_group_returns_nothing(tmp_path) -> None:
    _, _, sched = _make(tmp_path, seed=False)
    now = _bj_ts(Y, M, D, 14, 0)
    assert sched.due("999", now) == []


def test_news_cross_day_reset(tmp_path) -> None:
    """今天做过 slot，明天同一 slot 照做（done 记录只留今/昨）。"""
    _, store_cfg_or_settings, sched = _make(tmp_path)
    probe = _bj_ts(Y, M, D, 12, 0)
    actual_today = sched.slot_ts(GID, probe, "14:00")
    sched.done(GID, "news", actual_today)
    # 明天
    probe2 = probe + 86400
    actual_tomorrow = sched.slot_ts(GID, probe2, "14:00")
    assert "news" in sched.due(GID, actual_tomorrow)


def test_done_records_only_today_and_yesterday(tmp_path) -> None:
    """连续多天在固定北京时刻 done，kv 里只留当天和前一天的标记。"""
    store, _, sched = _make(tmp_path)
    day1 = _bj_ts(Y, M, D, 14, 30)
    for i in range(4):
        sched.done(GID, "news", day1 + i * 86400)
    got = store.kv_get(f"sched.{GID}.news")
    assert isinstance(got, list)
    from CharTyr_MaiWork.maiwork import clock

    # 最后一次 done 的「当天」是 D+3；只留 D+2 和 D+3
    day3 = day1 + 2 * 86400
    day4 = day1 + 3 * 86400
    keep = {clock.day_key(day3), clock.day_key(day4)}
    assert {x.split("|")[0] for x in got} == keep
    assert len(got) == 2


# ----------------------------------------------------------------------
# idea due / done
# ----------------------------------------------------------------------


def test_idea_due_when_quiet_then_done_blocks(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    now = _bj_ts(Y, M, D, 15, 0)
    # 最后一条消息 20 分钟前 → 可以出构想
    assert "idea" in sched.due(GID, now, last_msg_ts=now - 20 * 60)
    sched.done(GID, "idea", now)
    assert "idea" not in sched.due(GID, now + 600, last_msg_ts=now - 20 * 60)
    # 明天又可以
    tomorrow = now + 86400
    assert "idea" in sched.due(GID, tomorrow, last_msg_ts=tomorrow - 20 * 60)


def test_idea_not_quiet_enough(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    now = _bj_ts(Y, M, D, 15, 0)
    assert "idea" not in sched.due(GID, now, last_msg_ts=now - 5 * 60)  # 5 分钟前有人说话


def test_idea_quiet_threshold_exactly_10_minutes(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    now = _bj_ts(Y, M, D, 15, 0)
    assert "idea" in sched.due(GID, now, last_msg_ts=now - 10 * 60)
    assert "idea" not in sched.due(GID, now, last_msg_ts=now - 10 * 60 + 1)


def test_idea_outside_window(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    for hh_mm in ((9, 59), (21, 0), (22, 30)):
        now = _bj_ts(Y, M, D, *hh_mm)
        assert "idea" not in sched.due(GID, now, last_msg_ts=now - 3600), hh_mm
    for hh_mm in ((10, 0), (15, 0), (20, 59)):
        now = _bj_ts(Y, M, D, *hh_mm)
        assert "idea" in sched.due(GID, now, last_msg_ts=now - 3600), hh_mm


def test_idea_last_msg_unknown_ok(tmp_path) -> None:
    """last_msg_ts 不给（0/省略）→ 不知道群里吵不吵，按「够安静」算。"""
    _, _, sched = _make(tmp_path)
    now = _bj_ts(Y, M, D, 15, 0)
    assert "idea" in sched.due(GID, now, last_msg_ts=0)
    assert "idea" in sched.due(GID, now)


def test_idea_day_key_uses_beijing(tmp_path) -> None:
    """北京 23:30（已是当天下半夜前）算的是「今天」；北京 00:30 是「新的一天」。"""
    _, _, sched = _make(tmp_path)
    late = _bj_ts(Y, M, D, 23, 30)
    # 23:30 在 idea 时间窗外，先确认不报
    assert "idea" not in sched.due(GID, late, last_msg_ts=0)
    # done 直接写；第二天 00:30（还在北京第二天）可以再报吗？——00:30 在窗外，不报；
    # 用 done 的 kv 验证日期键：北京 day 是 D 日
    sched.done(GID, "idea", late)
    from CharTyr_MaiWork.maiwork import clock

    assert clock.day_key(late) == f"{Y}-{'%02d' % M}-{'%02d' % D}"


# ----------------------------------------------------------------------
# next_news_ts
# ----------------------------------------------------------------------


def test_next_news_ts_within_day_and_next_day(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    probe = _bj_ts(Y, M, D, 12, 0)
    actual14 = sched.slot_ts(GID, probe, "14:00")
    got = sched.next_news_ts(GID, probe)
    assert got is not None
    # 下一个资讯时刻要么就在今天 14:00 那个位置（含偏移），要么是明天第一个 slot
    candidates = {sched.slot_ts(GID, probe, s) for s in ("14:00", "19:00")}
    tomorrow_probe = probe + 86400
    candidates |= {sched.slot_ts(GID, tomorrow_probe, s) for s in ("08:30", "14:00", "19:00")}
    assert got in candidates
    # 已过今天的全部 slot → 给明天第一个
    late = _bj_ts(Y, M, D, 23, 0)
    got2 = sched.next_news_ts(GID, late)
    expect = min(sched.slot_ts(GID, late + 3600, s) for s in ("08:30", "14:00", "19:00"))
    assert got2 == expect


def test_next_news_ts_none_when_no_slots(tmp_path) -> None:
    _, _, sched = _make(tmp_path, cfg={"feeds": {"news_slots": []}})
    assert sched.next_news_ts(GID, _bj_ts(Y, M, D, 12, 0)) is None


# ----------------------------------------------------------------------
# 主动提目标（goal）due / done：一天一次，只在北京时间 19:00–21:00
# ----------------------------------------------------------------------


def test_goal_due_window_then_done_blocks_today(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    now = _bj_ts(Y, M, D, 19, 30)
    assert "goal" in sched.due(GID, now)
    sched.done(GID, "goal", now)
    assert "goal" not in sched.due(GID, now + 600)
    tomorrow = now + 86400
    assert "goal" in sched.due(GID, tomorrow)  # 第二天又可以


def test_goal_outside_window(tmp_path) -> None:
    _, _, sched = _make(tmp_path)
    for hh_mm in ((18, 59), (21, 0), (22, 30), (2, 0)):
        now = _bj_ts(Y, M, D, *hh_mm)
        assert "goal" not in sched.due(GID, now), hh_mm


def test_goal_switch_off(tmp_path) -> None:
    _, _, sched = _make(tmp_path, cfg={"goals": {"propose": False}})
    assert "goal" not in sched.due(GID, _bj_ts(Y, M, D, 19, 30))


def test_goal_not_ready_group(tmp_path) -> None:
    _, _, sched = _make(tmp_path, ready=False)
    assert "goal" not in sched.due(GID, _bj_ts(Y, M, D, 19, 30))
    sched.done(GID, "goal", _bj_ts(Y, M, D, 19, 30))  # done 不报错
