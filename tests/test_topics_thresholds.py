"""冷场开话题放宽门槛（2026-10-10 用户：「一次都没开过的话，应该放宽条件」）。

线上实测（topic_log 130 次带分数的判断，10-01 ~ 10-10）：
- Jev 选「可以开」(fine) 时，ok 那一问几乎都落在 0.54~0.58，旧门槛 0.6 → 0 次过关；
- 候选的 fit 多在 0.1~0.4，旧门槛 0.5 也很少过。
按历史回放：ok ≥ 0.5（仍要求 reason == fine）+ fit ≥ 0.4 → 9 天 3 个群约 8 次，一天 1 次上下。
第 1 层闸门（睡觉时段、每日上限、最小间隔、安静时长）一个都不动。
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeHost, SignalsStub  # noqa: E402
from test_topics import GID, SID, _Jev, _bj_ts, _make_topics, _seed_candidate  # noqa: E402

T = _bj_ts(2026, 9, 27, 15, 0)
_CFG = {"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}}


async def _check(tmp_path, response: dict):
    signals = SignalsStub()
    signals.mark(GID, SID, T - 3600.0)  # 安静 60 分钟
    fake = _Jev(response=response)
    store, _settings, topics, *_ = _make_topics(
        tmp_path, signals=signals, jev=fake, host=FakeHost(msgs=[], session_id=SID), cfg=_CFG
    )
    _seed_candidate(store, check_now=T, title="测试候选")
    out = await topics.check(GID, T)
    assert fake.calls, out
    row = store.read().execute("SELECT jev FROM topic_log").fetchone()
    return out, json.loads(row["jev"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ok,fit",
    [
        (0.54, 0.55),  # 10-04 12:27 线上实况：「行商浪人恋爱线打卡」
        (0.57, 0.46),  # 10-07 10:54：手柄摇杆漂移
        (0.56, 0.40),  # 10-09 11:46：ZATO 入门表
        (0.50, 0.40),  # 正好在新线上
    ],
)
async def test_fine_with_typical_scores_now_opens(tmp_path, ok, fit):
    out, jev = await _check(tmp_path, {"ok": ok, "reason": ("fine", 0.8, 0.8), "fit_0": fit})
    assert out.startswith("queued:topic_id="), out
    assert jev["ok_need"] == 0.5 and jev["fit_need"] == 0.4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"ok": 0.49, "reason": ("fine", 0.8, 0.8), "fit_0": 0.8},           # ok 不够
        {"ok": 0.56, "reason": ("fine", 0.8, 0.8), "fit_0": 0.39},          # fit 不够
        {"ok": 0.60, "reason": ("open_question", 0.8, 0.8), "fit_0": 0.8},  # 有人在等回复：照旧不开
        {"ok": 0.60, "reason": ("mood", 0.8, 0.8), "fit_0": 0.8},           # 气氛不对：照旧不开
    ],
)
async def test_still_refuses_below_new_line_or_wrong_state(tmp_path, response):
    out, _jev = await _check(tmp_path, response)
    assert out.startswith("skip:judged_no"), out
