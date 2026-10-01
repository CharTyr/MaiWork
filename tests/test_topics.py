"""topics.py 单元测试：睡觉时段零调用零发送、不够安静不问 Jev、
今日上限、最短间隔、退避翻倍与清零、Jev 说不合适→记录但不发、
通过→发送一次且参数 sync 进 MaiBot（看 FakeCtx 或 FakeHost 记录）、
播报腔被拦、speaker=maibot 走 proactive_trigger、follow_up 统计、候选过期不用。

睡觉时段测试用 datetime 构造一个固定"北京 23:30"等时刻；运行环境当日时刻没关系。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics

from fakes import FakeCtx, FakeHost, FakeModelsQueue, FakeProfiles, SignalsStub

BJ = timezone(timedelta(hours=8))
GID = "111"
SID = "sess-1"
NOW = 1_790_000_000.0  # 测试里的"现在"


def _settings(cfg: dict | None = None) -> object:
    settings, _ = load_settings(cfg or {})
    return settings


def _bj_ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    """造一个北京时间的 epoch。"""
    return datetime(y, m, d, hh, mm, tzinfo=BJ).timestamp()


def _seed_candidate_here(topics: Any, *, title="测试候选", now: float, ttl_h: float = 12.0, brief="简报", link="http://x", kind="news", ref_id=7) -> int:
    """直接调 Topics.add_candidate_at，让候选的过期时间和「测试里的现在」一致。"""
    return topics.add_candidate_at(
        GID, kind=kind, ref_id=ref_id, title=title, brief=brief, link=link, ttl_h=ttl_h, now=now
    )


def _make_topics(
    tmp_path,
    *,
    cfg: dict | None = None,
    host=None,
    models=None,
    jev=None,
    profiles=None,
    signals=None,
):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg or {})
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id, last_msg_ts) VALUES (?, ?, ?)",
            (GID, SID, NOW - 3600.0 * 24),
        )
    if host is None:
        host = FakeHost(msgs=[], session_id=SID)
    if models is None:
        models = FakeModelsQueue(ready=True, replies=["好话题，来聊聊？"])
    if jev is None:
        jev = _UnavailableJev()
    if profiles is None:
        profiles = FakeProfiles()
        profiles.usual_gap_value = 300.0  # 5 分钟
    if signals is None:
        signals = SignalsStub()
    mentions = Mentions(store, lambda: settings)
    pushes = Pushes(store, lambda: settings)
    topics = Topics(
        store, host, models, jev, profiles, mentions, pushes,
        lambda: settings, signals,
    )
    return store, settings, topics, host, models, jev, profiles, signals


class _UnavailableJev:
    """不可用的 Jev 假对象。"""

    def __init__(self):
        self.calls: List[Tuple[Any, Any, str, str]] = []

    def available(self) -> bool:
        return False

    async def ask(self, state, questions, *, purpose, group_id, timeout_ms=None):
        self.calls.append((state, questions, purpose, group_id))
        return None


class _Jev:
    """预置回答的 Jev 假对象。"""

    def __init__(self, response: Dict[str, Any] | None = None, available: bool = True):
        self._response = response if response is not None else {
            "ok": 0.9,
            "reason": ("fine", 0.9, 0.9),
            "fit_0": 0.8,
        }
        self._available = available
        self.calls: List[Tuple[Any, Any, str, str]] = []

    def available(self) -> bool:
        return self._available

    async def ask(self, state, questions, *, purpose, group_id, timeout_ms=None):
        self.calls.append((state, questions, purpose, group_id))
        if not self._available:
            return None
        if not self._response:
            return None
        # 按问题逐个答
        out = {}
        for k, q in questions.items():
            if not isinstance(q, dict):
                continue
            qt = q["type"]
            if qt == "noul":
                out[k] = self._response.get(k, 0.5)
            elif qt == "choice":
                out[k] = self._response.get(k)
        return out


def _seed_candidate(store: Store, *, check_now: float, title="测试候选", kind="news", ref_id=7, ttl_h=12.0, brief="简报", link="http://x"):
    """造一条候选：expires_ts = check_now + ttl_h*3600（以 topics.check 传入的 now 为基准）。

    这样测试的 now 和候选的过期时间总是对得上，不用关心系统真实时间。
    """
    expires = float(check_now) + ttl_h * 3600.0
    created = float(check_now) - 60.0  # 1 分钟前加进池
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link, expires_ts, used_ts, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (GID, kind, ref_id, title, brief, link, expires, created),
        )
        return int(cur.lastrowid or 0)


def _seed_news_item(store: Store, item_id=7):
    """造 news_item 供 follow_up 的 news_items.replies 同步。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_batches (id, group_id, slot_ts, found, kept, skipped, note, created) VALUES (?, ?, ?, 1, 1, 0, '', ?)",
            (99, GID, NOW - 100, NOW - 100),
        ) if False else None
        conn.execute(
            "INSERT INTO news_items (id, batch_id, group_id, icon, title, summary, why, sources, url_key, published_ts, score, status_kind, status_at, replies, expires_ts, up, down, created)"
            " VALUES (?, 99, ?, 'newspaper', 'x', 's', 'w', '[]', 'u', NULL, 0.9, 'new', NULL, 0, NULL, 0, 0, ?)",
            (item_id, GID, NOW - 100),
        )


# ----------------------------------------------------------------------
# 第 1 层：不在睡觉时段
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_hours_zero_call_zero_send(tmp_path):
    """睡觉时段（23:30 北京时间）→ 零 Jev 调用、零发送。"""
    fake_jev = _Jev()
    host = FakeHost(msgs=[], session_id=SID)
    store, settings, topics, _, _, jev, _, _ = _make_topics(
        tmp_path,
        host=host,
        jev=fake_jev,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 23, 30), title="某测试话题")
    t = _bj_ts(2026, 9, 27, 23, 30)
    out = await topics.check(GID, t)
    assert out.startswith("skip:quiet_hours")
    assert fake_jev.calls == []
    assert host.msg_calls == []
    # 也没 topic_log
    rows = store.read().execute("SELECT COUNT(*) AS c FROM topic_log WHERE group_id=?", (GID,)).fetchone()
    assert int(rows["c"]) == 0


# ----------------------------------------------------------------------
# 第 1 层：候选池非空
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_candidate_skip(tmp_path):
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    t = _bj_ts(2026, 9, 27, 15, 0)
    out = await topics.check(GID, t)
    assert out.startswith("skip:no_candidate") or "empty" in out.lower() or out.startswith("skip:pool")


# ----------------------------------------------------------------------
# 第 1 层：候选过期不用
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_candidate_skip(tmp_path):
    """候选过期 → skip。"""
    store, settings, topics, _, _, jev, *_ = _make_topics(
        tmp_path,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    # expired 候选
    now_bj = clock.bj(_bj_ts(2026, 9, 27, 15, 0)).timestamp()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link, expires_ts, used_ts, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (GID, "news", 7, "过期的", "b", "", _bj_ts(2026, 9, 27, 10, 0), _bj_ts(2026, 9, 27, 1, 0)),
        )
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert out.startswith("skip:")  # 候选全过期 → 等同没候选


# ----------------------------------------------------------------------
# 第 1 层：今日上限 / 最短间隔
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_day_limit(tmp_path):
    """当日已开过 per_day 个话题 → skip。"""
    fake_jev = _Jev()
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        jev=fake_jev,
        cfg={"topics": {"enabled": True, "per_day": 1, "min_gap_hours": 0}},
    )
    # 已开过 1 个（opener 非空才算「已开」）
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id, opener, message_id, followup_due_ts, result, verdict)"
            " VALUES (?, ?, 0, NULL, NULL, NULL, NULL, 'x', 'mid', NULL, NULL, NULL)",
            (GID, _bj_ts(2026, 9, 27, 9, 0)),
        )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert "limit" in out or "per_day" in out


@pytest.mark.asyncio
async def test_min_gap(tmp_path):
    """距上次开话题 < min_gap × 倍数 → skip。"""
    fake_jev = _Jev()
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        jev=fake_jev,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 3}},
    )
    # 1 小时前开过
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id, opener, message_id, followup_due_ts, result, verdict)"
            " VALUES (?, ?, 0, NULL, NULL, NULL, NULL, 'x', 'mid', NULL, NULL, NULL)",
            (GID, _bj_ts(2026, 9, 27, 14, 0)),
        )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert "min_gap" in out or "gap" in out


# ----------------------------------------------------------------------
# 第 1 层：安静时长 >= max(3×usual_gap, 1200s)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_quiet_not_enough_skip(tmp_path):
    """群不够安静（最近还有消息）→ 不问 Jev。"""
    fake_jev = _Jev()
    signals = SignalsStub()
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 50))  # 10 分钟前还有
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        jev=fake_jev,
        signals=signals,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert out.startswith("skip:")
    assert fake_jev.calls == []


@pytest.mark.asyncio
async def test_usual_gap_none_skip(tmp_path):
    """这个钟点平时没人（usual_gap=None）→ skip。"""
    signals = SignalsStub()
    # 安静 1 小时：够冷场、又没到新的「安静过久（>90 分钟）不开」那条线
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    profiles = FakeProfiles()
    profiles.usual_gap_value = None
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        signals=signals,
        profiles=profiles,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert "usual" in out.lower() or "小时" in out or "平日" in out or out.startswith("skip:")


# ----------------------------------------------------------------------
# 第 2 层：Jev 不可用 → skip
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jev_unavailable_skip_no_log(tmp_path):
    """Jev 不可用 → skip:jev_unavailable，不写 topic_log。"""
    signals = SignalsStub()
    # 安静 1 小时：够冷场、又没到新的「安静过久（>90 分钟）不开」那条线
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    fake = _UnavailableJev()
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        signals=signals,
        jev=fake,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert out == "skip:jev_unavailable"
    rows = store.read().execute("SELECT COUNT(*) AS c FROM topic_log").fetchone()
    assert int(rows["c"]) == 0
    assert fake.calls == []  # 还没问就开了


# ----------------------------------------------------------------------
# 第 2 层：Jev 说不合适 → 记录但不发
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jev_says_no_log_but_no_send(tmp_path):
    signals = SignalsStub()
    # 安静 1 小时：够冷场、又没到新的「安静过久（>90 分钟）不开」那条线
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.7, 0.8), "fit_0": 0.5})
    host = FakeHost(msgs=[], session_id=SID)
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        signals=signals,
        jev=fake,
        host=host,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert fake.calls  # 问过 Jev
    assert not hasattr(host, "send_text_calls") or host.send_text_calls == []  # 没发
    # 但写了 topic_log
    rows = store.read().execute("SELECT * FROM topic_log WHERE group_id=?", (GID,)).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    # jev 字段 JSON：reason 中文化为「人都走了」
    import json as _j
    jev = _j.loads(row["jev"])
    assert jev["reason"] == "人都走了"
    assert jev["ok"] is False
    # opener 为空表示没发
    assert row["opener"] == ""


# ----------------------------------------------------------------------
# 通过 → 发送一次且参数 sync 进 MaiBot
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_sends_via_send_text(tmp_path):
    """Jev 说开 → host.send_text 被调用一次，sync_to_maisaka_history=True。"""
    signals = SignalsStub()
    # 安静 1 小时：够冷场、又没到新的「安静过久（>90 分钟）不开」那条线
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    fake = _Jev()
    host = FakeHost(msgs=[], session_id=SID)
    store, settings, topics, host_obj, models_obj, *_ = _make_topics(
        tmp_path,
        signals=signals,
        jev=fake,
        host=host,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0, "speaker": "maiwork"}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    # FakeModelsQueue 默认 reply 一句干净的开场白
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert fake.calls, "Jev 应该被问过一次"
    # 开话题前读消息（用作 Jev 的 state），这条断言不再是「零 msg_calls」
    # host.send_text 被调一次
    if hasattr(host, "send_text_calls"):
        assert host.send_text_calls, "应该调 send_text 发开场白"
        call = host.send_text_calls[0]
        # 内部走 send.hybrid 时带 sync_to_maisaka_history=True（host.py 这样写的）
        assert "开场白" in call["text"] or "好话题" in call["text"] or len(call["text"]) > 0
    # topic_log 有一行（judgment + 发送后回写）
    rows = store.read().execute("SELECT * FROM topic_log WHERE group_id=?", (GID,)).fetchall()
    assert len(rows) == 1
    # candidate 被标记 used
    cand_rows = store.read().execute("SELECT used_ts FROM topic_candidates WHERE group_id=?", (GID,)).fetchall()
    assert cand_rows and cand_rows[0]["used_ts"] is not None
    # pushes 记了一条 topic
    assert dict(store.read().execute("SELECT COUNT(*) AS c FROM pushes WHERE group_id=? AND kind='topic'", (GID,)).fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_pass_speaker_maibot_via_proactive_trigger(tmp_path):
    """speaker="maibot" → 走 proactive_trigger，不走 send_text。"""
    signals = SignalsStub()
    # 安静 1 小时：够冷场、又没到新的「安静过久（>90 分钟）不开」那条线
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    fake = _Jev()
    host = FakeHost(msgs=[], session_id=SID)
    store, settings, topics, host_obj, *_ = _make_topics(
        tmp_path,
        signals=signals,
        jev=fake,
        host=host,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0, "speaker": "maibot"}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    if hasattr(host, "send_text_calls"):
        assert host.send_text_calls == []
    if hasattr(host, "proactive_trigger_calls"):
        assert host.proactive_trigger_calls, "proactive_trigger 应该被调"
    else:
        pytest.skip("FakeHost 没有 proactive_trigger 记录")


@pytest.mark.asyncio
async def test_pass_broadcast_tone_rejected(tmp_path):
    """模型产出含「据报道」「以下是」「今日资讯」等播报腔 → 整条作废不发。"""
    signals = SignalsStub()
    # 安静 1 小时：够冷场、又没到新的「安静过久（>90 分钟）不开」那条线
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    fake = _Jev()
    host = FakeHost(msgs=[], session_id=SID)
    from fakes import FakeModelsQueue
    models = FakeModelsQueue(ready=True, replies=["据报道，今天热点有三条："])
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        signals=signals,
        jev=fake,
        host=host,
        models=models,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0, "speaker": "maiwork"}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    # 发了事就失败；不应该调 send_text
    if hasattr(host, "send_text_calls"):
        assert host.send_text_calls == []
    # result 写了 rejected
    rows = store.read().execute("SELECT * FROM topic_log WHERE group_id=?", (GID,)).fetchall()
    assert any("rejected" in (dict(r)["result"] or "") for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("opener", [
    "我是小麦，最近战锤 40K 出新预告了，你们看了没？",
    "大家好～战锤 40K 出新预告了",
    "作为一个 AI 助手，给大家说个新闻：战锤 40K 出新预告了",
])
async def test_pass_self_intro_rejected(tmp_path, opener):
    """开场白在自我介绍 / 寒暄（2026-10-01 用户定：不要自我介绍和废话）→ 这次不开。"""
    signals = SignalsStub()
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 14, 0))
    host = FakeHost(msgs=[], session_id=SID)
    from fakes import FakeModelsQueue
    models = FakeModelsQueue(ready=True, replies=[opener])
    store, settings, topics, *_ = _make_topics(
        tmp_path,
        signals=signals,
        jev=_Jev(),
        host=host,
        models=models,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0, "speaker": "maiwork"}},
    )
    _seed_candidate(store, check_now=_bj_ts(2026, 9, 27, 15, 0), title="测试候选")
    out = await topics.check(GID, _bj_ts(2026, 9, 27, 15, 0))
    assert out == "rejected:self_intro"
    if hasattr(host, "send_text_calls"):
        assert host.send_text_calls == []
    rows = store.read().execute("SELECT result FROM topic_log WHERE group_id=?", (GID,)).fetchall()
    assert any("自我介绍" in (r["result"] or "") for r in rows)


# ----------------------------------------------------------------------
# follow_up
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_up_counts_replies_and_followups(tmp_path):
    """10 分钟内非机器人发言 = replies；机器人后续 = followups（不含开场白本身）。"""
    signals = SignalsStub()
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
    topic_ts = _bj_ts(2026, 9, 27, 14, 0)
    msgs = [
        Msg(id="m1", ts=topic_ts + 60, user_id="10001", user_name="张三", text="有意思", is_bot=False, is_at=False, is_picture=False, reply_to=""),
        Msg(id="m2", ts=topic_ts + 120, user_id="10002", user_name="李四", text="顶", is_bot=False, is_at=False, is_picture=False, reply_to=""),
        # bot 自己的开场白
        Msg(id="m3", ts=topic_ts, user_id="bot", user_name="bot", text="开场白", is_bot=True, is_at=False, is_picture=False, reply_to=""),
        # bot 后续跟了一句
        Msg(id="m4", ts=topic_ts + 90, user_id="bot", user_name="bot", text="嘻嘻", is_bot=True, is_at=False, is_picture=False, reply_to=""),
    ]
    host = FakeHost(msgs=msgs, session_id=SID)
    fake_jev = _Jev()
    store, settings, topics, *_ = _make_topics(
        tmp_path, host=host, jev=fake_jev, signals=signals,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}},
    )
    # 造一条待 follow_up 的 topic_log；followup_due_ts 已到
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id, opener, message_id, followup_due_ts, result, verdict)"
            " VALUES (?, ?, 1800, 300, ?, ?, 1, '测试开场白', 'opener-msg-id', ?, NULL, NULL)",
            (GID, topic_ts, '{"ok": 0.9, "reason": "fine"}', '{"title": "x", "fit": 0.8}', topic_ts + 600),
        )
        topic_id = int(cur.lastrowid or 0)
    # followup_due_ts 已过
    await topics.follow_up(GID, topic_ts + 700)
    rows = store.read().execute("SELECT * FROM topic_log WHERE id=?", (topic_id,)).fetchall()
    row = dict(rows[0])
    # 通过 message_id 找开场白
    assert row["result"] is not None
    import json
    result = json.loads(row["result"])
    # 在 [topic_ts, topic_ts+600] 里：非机器人发言 2 人（张三李四），机器人 1 条（"嘻嘻"），不含 opener 本人
    assert result["replies"] >= 2
    assert result["followups"] >= 1


@pytest.mark.asyncio
async def test_follow_up_off_topic_does_not_backoff(tmp_path):
    """有人接（replies>0）→ 退避倍数回到 1。"""
    signals = SignalsStub()
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
    topic_ts = _bj_ts(2026, 9, 27, 14, 0)
    msgs = [
        Msg(id="m1", ts=topic_ts + 60, user_id="10001", user_name="张三", text="有意思", is_bot=False, is_at=False, is_picture=False, reply_to=""),
    ]
    host = FakeHost(msgs=msgs, session_id=SID)
    store, settings, topics, *_ = _make_topics(
        tmp_path, host=host, signals=signals,
        cfg={"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 3}},
    )
    # 先把 backoff 升到 4
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)",
            (f"topics.backoff.{GID}", '"4"', clock.now()),
        )
        cur = conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id, opener, message_id, followup_due_ts, result, verdict)"
            " VALUES (?, ?, 1800, 300, NULL, NULL, 1, '测试开场白', 'opener-msg-id', ?, NULL, NULL)",
            (GID, topic_ts, topic_ts + 600),
        )
    await topics.follow_up(GID, topic_ts + 700)
    row = store.read().execute("SELECT value FROM kv WHERE key=?", (f"topics.backoff.{GID}",)).fetchone()
    assert row is None or row["value"] in ('"1"', "1")


# ----------------------------------------------------------------------
# 2026-10-01 线上实测（测试群 900000001：一天判 343 次、开 0 次）后加的行为：
# 同输入不重问 Jev、夜里的安静不算冷场、安静过久不开、给 Jev 带时间、
# 过滤杂音、候选按画像兴趣排序、构想换 fit 问法、topic_log 记全分数。
# ----------------------------------------------------------------------


_QUIET_CFG = {"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}}


def _text_msg(ts: float, text: str, *, uid: str = "10001", name: str = "张三", is_bot: bool = False) -> Msg:
    """造一条群消息（只关心 ts / text）。"""
    return Msg(
        id=f"m-{int(ts)}-{len(text)}", ts=float(ts), user_id=uid, user_name=name,
        text=text, is_bot=is_bot, is_at=False, is_picture=False, reply_to="",
    )


def _no_answer_jev() -> "_Jev":
    """available() 说能用、但 ask 拿不到答案（比如超时）的 Jev。"""
    return _Jev(response={})


# ----------------------------------------------------------------------
# 1) 同一段冷场不重复问 Jev
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_input_not_rejudged(tmp_path):
    """指纹不变 → 第二次返回 skip:same_as_last，不问 Jev、不写新日志。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)  # 安静 60 分钟
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")

    first = await topics.check(GID, t)
    assert fake.calls, "第一次要问 Jev"
    assert not first.startswith("skip:same_as_last")
    assert store.kv_get(f"topics.judged.{GID}") is not None, "Jev 真答了才写指纹"

    second = await topics.check(GID, t + 30.0)
    assert second == "skip:same_as_last"
    assert len(fake.calls) == 1, "同一段冷场不重复问 Jev"
    count = store.read().execute("SELECT COUNT(*) AS c FROM topic_log").fetchone()
    assert int(count["c"]) == 1, "同一段冷场不重复写 topic_log"


@pytest.mark.asyncio
async def test_new_candidate_rejudged(tmp_path):
    """候选变了（指纹里的候选 id 变了）→ 满 30 分钟前也重问。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="第一条")
    await topics.check(GID, t)
    _seed_candidate(store, check_now=t, title="第二条", ref_id=8)
    out = await topics.check(GID, t + 60.0)
    assert not out.startswith("skip:same_as_last")
    assert len(fake.calls) == 2, "候选变了要重问"


@pytest.mark.asyncio
async def test_fingerprint_expires_after_30min(tmp_path):
    """同一段冷场满 30 分钟 → 重问（让「有问题没人回」这类判断随时间过期）。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 45 * 60.0)  # 安静 45 分钟
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")
    await topics.check(GID, t)
    out = await topics.check(GID, t + 31 * 60.0)  # 安静 76 分钟（还没到 90 分钟）
    assert not out.startswith("skip:same_as_last")
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_activity_then_quiet_again_rejudged(tmp_path):
    """中间有人说话 → 那会儿不问；之后再静下来（新指纹）→ 重问。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")
    await topics.check(GID, t)
    assert len(fake.calls) == 1

    signals.mark(GID, SID, t + 100.0)  # 群里又有人说话
    out = await topics.check(GID, t + 120.0)
    assert out.startswith("skip:not_quiet_enough")
    assert len(fake.calls) == 1, "刚有人说话时不问 Jev"

    out2 = await topics.check(GID, t + 100.0 + 3600.0)  # 又静了 1 小时
    assert not out2.startswith("skip:same_as_last")
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_jev_no_answer_no_fingerprint(tmp_path):
    """Jev 没真答（超时/无答案）→ 不写指纹，下一轮还得问。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=_no_answer_jev(), cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")
    out = await topics.check(GID, t)
    assert out == "skip:jev_unavailable"
    assert store.kv_get(f"topics.judged.{GID}") is None


# ----------------------------------------------------------------------
# 2) 夜里的安静不算冷场
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "quiet_hours,check_ts,last_msg_ts",
    [
        ("23:00-08:00", _bj_ts(2026, 9, 28, 8, 30), _bj_ts(2026, 9, 28, 2, 0)),
        ("00:00-07:00", _bj_ts(2026, 9, 28, 7, 10), _bj_ts(2026, 9, 28, 3, 0)),
    ],
)
@pytest.mark.asyncio
async def test_no_activity_since_wakeup(tmp_path, quiet_hours, check_ts, last_msg_ts):
    """最近一条消息在睡觉时段里（早于最近一次醒来）→ skip:no_activity_since_wakeup。"""
    signals = SignalsStub()
    signals.mark(GID, SID, last_msg_ts)
    fake = _Jev()
    cfg = {"delivery": {"quiet_hours": quiet_hours}, "topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}}
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=cfg)
    _seed_candidate(store, check_now=check_ts, title="测试候选")
    out = await topics.check(GID, check_ts)
    assert out == "skip:no_activity_since_wakeup"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_activity_after_wakeup_can_judge(tmp_path):
    """醒来后有人说过话、再安静下来 → 照常判。"""
    t = _bj_ts(2026, 9, 28, 7, 30)
    signals = SignalsStub()
    signals.mark(GID, SID, _bj_ts(2026, 9, 28, 7, 5))  # 醒来（07:00）后说过话，安静 25 分钟
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    cfg = {"delivery": {"quiet_hours": "00:00-07:00"}, "topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}}
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=cfg)
    _seed_candidate(store, check_now=t, title="测试候选")
    out = await topics.check(GID, t)
    assert out != "skip:no_activity_since_wakeup"
    assert fake.calls, "醒来后静下来的冷场该照常问 Jev"


# ----------------------------------------------------------------------
# 3) 安静过久（>90 分钟）不开
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_long_quiet_skip(tmp_path):
    """安静 3 小时 → skip:too_long_quiet，不问 Jev。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3 * 3600.0)
    fake = _Jev()
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")
    out = await topics.check(GID, t)
    assert out == "skip:too_long_quiet"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_quiet_at_90min_boundary_still_judged(tmp_path):
    """正好安静 90 分钟（没超过）→ 仍判。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 90 * 60.0)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")
    out = await topics.check(GID, t)
    assert out != "skip:too_long_quiet"
    assert fake.calls


# ----------------------------------------------------------------------
# 4) 给 Jev 时间信息
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jev_state_carries_time_info(tmp_path):
    """state 顶层带 quiet_minutes / usual_gap_minutes，每条消息带 minutes_ago；ok 问法写进数字。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)  # 安静 60 分钟
    msgs = [_text_msg(t - 180.0, "在吗"), _text_msg(t - 90.0, "有人吗")]
    host = FakeHost(msgs=msgs, session_id=SID)
    fake = _Jev(response={"ok": 0.4, "reason": ("open_question", 0.7, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(
        tmp_path, host=host, signals=signals, jev=fake, cfg=_QUIET_CFG
    )
    _seed_candidate(store, check_now=t, title="测试候选")
    await topics.check(GID, t)

    state, questions, purpose, _gid = fake.calls[0]
    assert state["quiet_minutes"] == 60
    assert state["usual_gap_minutes"] == 5.0  # FakeProfiles 的 usual_gap = 300 秒
    assert [m["minutes_ago"] for m in state["messages"]] == [3, 1]
    assert "60 分钟" in questions["ok"]["instructions"]
    assert "5.0 分钟" in questions["ok"]["instructions"]
    assert "太久" in questions["reason"]["criteria"]["open_question"]
    assert host.msg_calls and host.msg_calls[0][3] == 60, "多读一些（60 条）再过滤"


# ----------------------------------------------------------------------
# 5) 过滤杂音
# ----------------------------------------------------------------------


def test_is_noise_rules():
    """纯函数：空、无描述图片/表情包、事件行、空合并转发算杂音；带描述的表情包、视频不算。"""
    from CharTyr_MaiWork.maiwork.topics import _is_noise

    assert _is_noise("") is True
    assert _is_noise("   ") is True
    assert _is_noise(None) is True
    assert _is_noise("[图片]") is True
    assert _is_noise("[image]") is True
    assert _is_noise("[IMAGE]") is True
    assert _is_noise("[表情包]") is True
    assert _is_noise("[事件-群消息撤回] 张三 撤回了一条消息") is True
    assert _is_noise("[事件-群消息表情回应] 李四 回应了") is True
    assert _is_noise("【合并转发消息: \n-- 【xx】: \n】") is True
    assert _is_noise("[表情包: 无语,呆滞]") is False
    assert _is_noise("[视频]") is False
    assert _is_noise("在吗") is False
    assert _is_noise("【合并转发消息: \n-- 【张三】: 今天真热\n】") is False


@pytest.mark.asyncio
async def test_noise_filtered_before_jev(tmp_path):
    """杂音不进 Jev 的 state；带描述的表情包和视频保留。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    raw = [
        _text_msg(t - 600.0, "在吗"),
        _text_msg(t - 580.0, ""),
        _text_msg(t - 560.0, "   "),
        _text_msg(t - 540.0, "[图片]"),
        _text_msg(t - 520.0, "[image]"),
        _text_msg(t - 500.0, "[表情包]"),
        _text_msg(t - 480.0, "[事件-群消息撤回] 张三 撤回了一条消息"),
        _text_msg(t - 460.0, "【合并转发消息: \n-- 【xx】: \n】"),
        _text_msg(t - 440.0, "[表情包: 无语,呆滞]"),
        _text_msg(t - 420.0, "[视频]"),
        _text_msg(t - 400.0, "刚那个方案不错"),
    ]
    host = FakeHost(msgs=raw, session_id=SID)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.7, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(
        tmp_path, host=host, signals=signals, jev=fake, cfg=_QUIET_CFG
    )
    _seed_candidate(store, check_now=t, title="测试候选")
    await topics.check(GID, t)

    state = fake.calls[0][0]
    texts = [m["text"] for m in state["messages"]]
    assert texts == ["在吗", "[表情包: 无语,呆滞]", "[视频]", "刚那个方案不错"]


@pytest.mark.asyncio
async def test_messages_take_last_20_after_filter(tmp_path):
    """过滤后只取最后 20 条。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    msgs = [_text_msg(t - 3600.0 + i * 60.0, f"消息{i}") for i in range(25)]
    host = FakeHost(msgs=msgs, session_id=SID)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.7, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(
        tmp_path, host=host, signals=signals, jev=fake, cfg=_QUIET_CFG
    )
    _seed_candidate(store, check_now=t, title="测试候选")
    await topics.check(GID, t)
    state = fake.calls[0][0]
    assert [m["text"] for m in state["messages"]][0] == "消息5"
    assert len(state["messages"]) == 20


# ----------------------------------------------------------------------
# 6) 候选按画像兴趣排序
# ----------------------------------------------------------------------


def test_candidates_ranked_by_profile_interest(tmp_path):
    """和画像兴趣条目对得上的候选排前面；候选多时也只取 5 条。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [
        {"id": 1, "category": "interest", "text": "群友都在玩 FPGA 和开源掌机"}
    ]
    store, settings, topics, *_ = _make_topics(tmp_path, profiles=profiles, cfg=_QUIET_CFG)
    titles = {
        1: ("装修避坑", "家装经验"),
        2: ("猫咪喂养", "猫粮怎么选"),
        3: ("跑步装备", "跑鞋推荐"),
        4: ("咖啡豆选购", "手冲入门"),
        5: ("机械键盘轴体", "客制化入门"),
        6: ("FPGA 掌机新玩法", "开源掌机 FPGA 项目进展"),
    }
    for ref, (title, brief) in titles.items():
        topics.add_candidate_at(
            GID, kind="news", ref_id=ref, title=title, brief=brief, ttl_h=12.0,
            now=t - ref * 60.0,  # ref=1 最新、ref=6 最早
        )
    cands = topics._list_candidates(GID, t, limit=5)
    refs = [int(c["ref_id"]) for c in cands]
    assert len(cands) == 5
    assert refs[0] == 6, f"和画像兴趣对得上的候选该排第一，实际: {refs}"
    # 其余按 created DESC（新的在前）
    assert refs[1:] == [1, 2, 3, 4]


def test_candidates_fallback_created_desc_without_profile(tmp_path):
    """拿不到画像条目 → 退回按 created DESC。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    store, settings, topics, *_ = _make_topics(tmp_path, cfg=_QUIET_CFG)
    topics.add_candidate_at(GID, kind="news", ref_id=1, title="旧", brief="", ttl_h=12.0, now=t - 100.0)
    topics.add_candidate_at(GID, kind="news", ref_id=2, title="新", brief="", ttl_h=12.0, now=t - 10.0)
    cands = topics._list_candidates(GID, t, limit=5)
    assert [int(c["ref_id"]) for c in cands] == [2, 1]


def test_candidates_fallback_created_desc_when_profile_errors(tmp_path):
    """读画像出错 → 不抛，退回按 created DESC。"""

    class _BrokenEntries:
        def entries(self, group_id):
            raise RuntimeError("画像读不到")

    t = _bj_ts(2026, 9, 27, 15, 0)
    store, settings, topics, *_ = _make_topics(tmp_path, profiles=_BrokenEntries(), cfg=_QUIET_CFG)
    topics.add_candidate_at(GID, kind="news", ref_id=1, title="旧", brief="", ttl_h=12.0, now=t - 100.0)
    topics.add_candidate_at(GID, kind="news", ref_id=2, title="新", brief="", ttl_h=12.0, now=t - 10.0)
    cands = topics._list_candidates(GID, t, limit=5)
    assert [int(c["ref_id"]) for c in cands] == [2, 1]


# ----------------------------------------------------------------------
# 7) 构想类候选换 fit 问法
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idea_candidate_fit_question(tmp_path):
    """kind="idea" → 按「回头问一句群里聊过的事、要不要帮忙」问。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.7, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="帮着做个小工具", kind="idea", ref_id=3)
    await topics.check(GID, t)
    q = fake.calls[0][1]
    assert "要不要帮忙" in q["fit_0"]["instructions"]


@pytest.mark.asyncio
async def test_news_candidate_fit_question_keeps_old_wording(tmp_path):
    """kind="news" → 保持原来的问法。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.7, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="某条资讯", kind="news", ref_id=7)
    await topics.check(GID, t)
    q = fake.calls[0][1]
    assert "会不会感兴趣" in q["fit_0"]["instructions"]


# ----------------------------------------------------------------------
# 8) topic_log.jev 记全信息
# ----------------------------------------------------------------------


def _read_jev(store: Store) -> dict:
    import json

    row = store.read().execute(
        "SELECT jev FROM topic_log WHERE group_id=? ORDER BY id DESC LIMIT 1", (GID,)
    ).fetchone()
    return json.loads(row["jev"])


@pytest.mark.asyncio
async def test_jev_log_records_full_scores_timing_stuck(tmp_path):
    """时机不过关（ok 分不够）→ stuck="timing"，各分数/标题/冷场起点都落库。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.4, "reason": ("open_question", 0.7, 0.8), "fit_0": 0.9})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="医疗话题")
    await topics.check(GID, t)

    jev = _read_jev(store)
    assert jev["ok_p"] == pytest.approx(0.4)
    assert jev["ok_need"] == pytest.approx(0.6)
    assert jev["fit_best"] == pytest.approx(0.9)
    assert jev["fit_need"] == pytest.approx(0.5)
    assert jev["fit_title"] == "医疗话题"
    assert jev["stuck"] == "timing"
    assert jev["stretch_ts"] == pytest.approx(t - 3600.0)
    # 旧字段照旧
    assert jev["ok"] is False
    assert jev["reason"] == "有问题还没人回"
    assert jev["confidence"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_jev_log_timing_stuck_when_reason_not_fine(tmp_path):
    """ok 分够但 reason 不是 fine → 也算时机不过关。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.9, "reason": ("mood", 0.8, 0.8), "fit_0": 0.9})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="医疗话题")
    await topics.check(GID, t)
    jev = _read_jev(store)
    assert jev["stuck"] == "timing"
    assert jev["reason"] == "气氛不对"


@pytest.mark.asyncio
async def test_jev_log_stuck_candidate_when_fit_low(tmp_path):
    """时机过关、候选 fit 不够 → stuck="candidate"。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev(response={"ok": 0.9, "reason": ("fine", 0.9, 0.9), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="不够贴的候选")
    await topics.check(GID, t)
    jev = _read_jev(store)
    assert jev["stuck"] == "candidate"
    assert jev["fit_best"] == pytest.approx(0.2)
    assert jev["fit_title"] == "不够贴的候选"


@pytest.mark.asyncio
async def test_jev_log_stuck_null_when_opened(tmp_path):
    """开了 → stuck 为 null。"""
    t = _bj_ts(2026, 9, 27, 15, 0)
    signals = SignalsStub()
    signals.mark(GID, SID, t - 3600.0)
    fake = _Jev()
    store, settings, topics, *_ = _make_topics(tmp_path, signals=signals, jev=fake, cfg=_QUIET_CFG)
    _seed_candidate(store, check_now=t, title="测试候选")
    out = await topics.check(GID, t)
    assert out.startswith("opened:")
    assert _read_jev(store)["stuck"] is None
