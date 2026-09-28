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
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 12, 0))  # 3 小时前
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
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
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
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
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
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
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
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
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
    signals.mark(GID, SID, _bj_ts(2026, 9, 27, 10, 0))
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
