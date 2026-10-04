"""profile.py 第一部分测试：增量读消息、统计、群信息（不调模型的部分）。

时钟：测试里把 clock.now 钉在 T0（2026-09-26 14:05 北京时间），消息时间戳都相对 T0 取。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.profile import Profiles

from fakes import FakeHost, FakeModels

GID = "900000001"
# 2026-09-26 06:05 UTC = 北京时间 14:05
T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()
assert clock.day_key(T0) == "2026-09-26"
assert clock.bj(T0).hour == 14


def _settings(**profile_over):
    profile = {"backfill_days": 30, "backfill_max_messages": 1500}
    profile.update(profile_over)
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "profile": profile,
    }
    settings, problems = load_settings(raw)
    assert not problems
    return settings


SETTINGS = _settings()


def _msg(
    mid: str,
    ts: float,
    user: str = "u1",
    name: str = "阿一",
    *,
    bot: bool = False,
    at: bool = False,
    reply: str = "",
    text: str = "你好",
) -> Msg:
    return Msg(
        id=mid,
        ts=ts,
        user_id=user,
        user_name=name,
        text=text,
        is_bot=bot,
        is_at=at,
        is_picture=False,
        reply_to=reply,
    )


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(clock, "now", lambda: T0)
    return T0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


def _make(
    store: Store,
    host: FakeHost,
    *,
    ready: bool = False,  # 本文件测的是不调模型的部分；模型配好会真触发提炼，故默认没配好
    settings=SETTINGS,
) -> Profiles:
    return Profiles(store, host, FakeModels(ready), lambda: settings)


def _activity_total(store: Store, gid: str = GID) -> int:
    row = store.read().execute(
        "SELECT COALESCE(SUM(count), 0) AS c FROM member_activity WHERE group_id=?",
        (gid,),
    ).fetchone()
    return int(row["c"])


def _bins_total(store: Store, gid: str = GID) -> int:
    row = store.read().execute(
        "SELECT COALESCE(SUM(count), 0) AS c FROM activity_bins WHERE group_id=?",
        (gid,),
    ).fetchone()
    return int(row["c"])


# ----------------------------------------------------------------------
# remember_session / ensure_group
# ----------------------------------------------------------------------


class TestRememberSession:
    def test_creates_row_with_token_and_workspace(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        p.remember_session(GID, "sess-1", T0 - 60)
        row = store.read().execute(
            "SELECT * FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row is not None
        assert row["session_id"] == "sess-1"
        assert row["workspace"] == "tinker"
        assert row["created"] == T0
        assert row["last_msg_ts"] == T0 - 60
        token = row["token"]
        assert token and len(token) == 8 and token.isalnum()

    def test_last_msg_ts_keeps_max(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        p.remember_session(GID, "sess-1", T0 - 10)
        p.remember_session(GID, "sess-2", T0 - 999)  # 更旧的 ts 不回退
        row = store.read().execute(
            "SELECT * FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["session_id"] == "sess-2"
        assert row["last_msg_ts"] == T0 - 10

    def test_ensure_group_creates_and_reuses(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        row1 = p.ensure_group(GID)
        row2 = p.ensure_group(GID)
        assert row1["group_id"] == GID
        assert row1["token"] == row2["token"]  # 不重复建、token 不变
        assert row1["workspace"] == "tinker"


# ----------------------------------------------------------------------
# tick：门控与回读
# ----------------------------------------------------------------------


class TestTickGate:
    @pytest.mark.asyncio
    async def test_non_served_group_skips_without_touching_host(self, store: Store) -> None:
        host = FakeHost([_msg("m1", T0 - 60)])
        p = _make(store, host)
        r = await p.tick("999999")
        assert r.read == 0
        assert r.skipped_reason
        assert host.msg_calls == []
        assert host.session_calls == []
        assert host.info_calls == []

    @pytest.mark.asyncio
    async def test_session_failure_skips_read(
        self, store: Store, frozen_now: float
    ) -> None:
        host = FakeHost([_msg("m1", T0 - 60)], session_error=RuntimeError("炸了"))
        p = _make(store, host)
        r = await p.tick(GID)
        assert r.read == 0
        assert r.skipped_reason
        assert host.msg_calls == []

    @pytest.mark.asyncio
    async def test_first_tick_gets_session_from_host(
        self, store: Store, frozen_now: float
    ) -> None:
        host = FakeHost([_msg("m1", T0 - 60)], session_id="sess-x")
        p = _make(store, host)
        r = await p.tick(GID)
        assert host.session_calls == [GID]
        assert r.read == 1
        row = store.read().execute(
            "SELECT session_id FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["session_id"] == "sess-x"
        # 第二次 tick 不再问 host（用表里记的）
        await p.tick(GID)
        assert host.session_calls == [GID]

    @pytest.mark.asyncio
    async def test_never_read_group_re_resolves_saved_session(
        self, store: Store, frozen_now: float
    ) -> None:
        """线上实测（2026-09-27）：第一次拿到的是旧的空会话，存进了 groups 表，
        之后一直读空。还没读到过任何消息的群，每次都重新问 host，不信表里存的。"""
        p = _make(store, FakeHost([], session_id="old-empty"))
        await p.tick(GID)  # 读空：read_since 仍是 0
        host = FakeHost([_msg("m1", T0 - 60)], session_id="live")
        p2 = _make(store, host)
        r = await p2.tick(GID)
        assert host.session_calls == [GID]
        assert r.read == 1
        row = store.read().execute("SELECT session_id FROM groups WHERE group_id=?", (GID,)).fetchone()
        assert row["session_id"] == "live"

    @pytest.mark.asyncio
    async def test_models_not_ready_still_counts(self, store: Store, frozen_now: float) -> None:
        host = FakeHost([_msg("m1", T0 - 60), _msg("m2", T0 - 30), _msg("m3", T0 - 10)])
        p = _make(store, host, ready=False)
        r = await p.tick(GID)
        assert r.read == 3
        assert r.refreshed is False
        assert _activity_total(store) == 3
        assert _bins_total(store) == 3


class TestBackfill:
    @pytest.mark.asyncio
    async def test_first_read_limited_by_backfill_max_and_read_since(
        self, store: Store, frozen_now: float
    ) -> None:
        msgs = [_msg(f"m{i}", T0 - (101 - i) * 100) for i in range(1, 101)]  # 100 条
        host = FakeHost(msgs)
        settings = _settings(backfill_max_messages=30)
        p = _make(store, host, settings=settings)
        r = await p.tick(GID)
        assert r.read == 30
        row = store.read().execute(
            "SELECT read_since FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["read_since"] == msgs[70].ts  # 取最新的 30 条（m71..m100）
        assert _activity_total(store) == 30
        # 再 tick 没有新消息：一条也不重复读
        r2 = await p.tick(GID)
        assert r2.read == 0
        assert _activity_total(store) == 30

    @pytest.mark.asyncio
    async def test_first_read_respects_backfill_days(
        self, store: Store, frozen_now: float
    ) -> None:
        settings = _settings(backfill_days=2)
        host = FakeHost([_msg("m1", T0 - 3600)])
        p = _make(store, host, settings=settings)
        r = await p.tick(GID)
        assert r.read == 1
        # 回读起点不能早于两天前
        assert host.msg_calls[0][1] >= T0 - 2 * 86400


# ----------------------------------------------------------------------
# tick：游标去重与分页
# ----------------------------------------------------------------------


class TestCursor:
    @pytest.mark.asyncio
    async def test_same_ts_messages_not_double_counted(
        self, store: Store, frozen_now: float
    ) -> None:
        same_ts = T0 - 60
        host = FakeHost(session_id="sess-1")
        host.msgs = [_msg("m1", same_ts), _msg("m2", same_ts)]
        p = _make(store, host)
        r1 = await p.tick(GID)
        assert r1.read == 2
        assert _activity_total(store) == 2

        # 库里冒出了同 ts 的第三条，重读时 m1/m2 不能重复计数
        host.msgs = [_msg("m1", same_ts), _msg("m2", same_ts), _msg("m3", same_ts)]
        r2 = await p.tick(GID)
        assert r2.read == 1
        assert _activity_total(store) == 3

        # 第三次：完全没有新消息
        r3 = await p.tick(GID)
        assert r3.read == 0
        assert _activity_total(store) == 3

    @pytest.mark.asyncio
    async def test_pending_count_accumulates(self, store: Store, frozen_now: float) -> None:
        same_ts = T0 - 60
        host = FakeHost([_msg("m1", same_ts), _msg("m2", same_ts)])
        p = _make(store, host)
        r1 = await p.tick(GID)
        assert r1.new_messages == 2
        host.msgs.append(_msg("m3", same_ts))
        r2 = await p.tick(GID)
        assert r2.read == 1
        assert r2.new_messages == 3  # 第一部分 _maybe_refresh 是空的，不清零
        row = store.read().execute(
            "SELECT pending_count FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["pending_count"] == 3

    @pytest.mark.asyncio
    async def test_pagination_continues_at_last_ts(self, store: Store, frozen_now: float) -> None:
        """首次回读是一次「最新 N 条」；之后增量读按页往后翻，要用 earliest
        （线上宿主默认 latest，翻页会跳过中间的消息——2026-09-27 上线实测踩到）。"""
        host = FakeHost([_msg("m-first", T0 - 5000)])
        p = _make(store, host)
        await p.tick(GID)
        msgs = [_msg(f"m{i}", T0 - 2000 + i) for i in range(450)]
        host.msgs = sorted(host.msgs + msgs, key=lambda m: m.ts)
        host.msg_calls.clear()
        host.msg_modes = []
        r = await p.tick(GID)
        assert r.read == 450
        assert _activity_total(store) == 451
        # 每页最多 200 条，450 条要 3 次调用，全是 earliest
        assert len(host.msg_calls) == 3
        assert host.msg_modes == ["earliest"] * 3
        # 第二次翻页从上一页最后一条的 ts 接着读（第一页含已读的 m-first，
        # 所以第一页最后一条是 m198）
        assert host.msg_calls[1][1] == msgs[198].ts
        # 再 tick：全部已读，一条也不多读
        r2 = await p.tick(GID)
        assert r2.read == 0
        assert _activity_total(store) == 451

    @pytest.mark.asyncio
    async def test_first_backfill_takes_latest_n_in_one_call(
        self, store: Store, frozen_now: float
    ) -> None:
        msgs = [_msg(f"m{i}", T0 - 3000 + i) for i in range(2000)]
        host = FakeHost(msgs)
        p = _make(store, host, settings=_settings(backfill_max_messages=1500))
        r = await p.tick(GID)
        assert r.read == 1500
        assert host.msg_modes == ["latest"]
        assert host.msg_calls[0][3] == 1500
        # 取到的是最新的 1500 条，不是最早的
        row = store.read().execute("SELECT read_since, cursor_ts FROM groups WHERE group_id=?", (GID,)).fetchone()
        assert row["read_since"] == msgs[500].ts
        assert row["cursor_ts"] == msgs[-1].ts


# ----------------------------------------------------------------------
# tick：统计（成员活跃、互动）
# ----------------------------------------------------------------------


class TestStats:
    @pytest.mark.asyncio
    async def test_bot_messages_not_in_member_activity_but_in_bins(
        self, store: Store, frozen_now: float
    ) -> None:
        host = FakeHost(
            [
                _msg("b1", T0 - 100, user="bot", name="MaiBot", bot=True),
                _msg("h1", T0 - 50),
            ]
        )
        p = _make(store, host)
        await p.tick(GID)
        assert _activity_total(store) == 1  # 只有人的
        assert _bins_total(store) == 2  # 桶含机器人的
        rows = store.read().execute(
            "SELECT message_id FROM bot_messages WHERE group_id=?", (GID,)
        ).fetchall()
        assert [r["message_id"] for r in rows] == ["b1"]

    @pytest.mark.asyncio
    async def test_member_activity_day_uses_beijing(self, store: Store, frozen_now: float) -> None:
        # 北京时间 2026-09-26 00:30 (= 2026-09-25 16:30 UTC)
        ts = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc).timestamp()
        host = FakeHost([_msg("m1", ts, name="改名后")])
        p = _make(store, host)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT day, count, name FROM member_activity WHERE group_id=? AND user_id='u1'",
            (GID,),
        ).fetchone()
        assert row["day"] == "2026-09-26"
        assert row["count"] == 1
        assert row["name"] == "改名后"


# ----------------------------------------------------------------------
# tick：群信息（名字、人数）
# ----------------------------------------------------------------------


class TestGroupInfo:
    @pytest.mark.asyncio
    async def test_group_name_filled_on_first_tick(self, store: Store, frozen_now: float) -> None:
        host = FakeHost(info={"group_name": "折腾研究所", "member_count": 214})
        p = _make(store, host)
        await p.tick(GID)
        assert host.info_calls == [GID]
        row = store.read().execute(
            "SELECT name, member_count, info_ts FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["name"] == "折腾研究所"
        assert row["member_count"] == 214
        assert row["info_ts"] == T0
        # 第二次 tick：信息还新鲜，不再问 host
        await p.tick(GID)
        assert host.info_calls == [GID]

    @pytest.mark.asyncio
    async def test_group_info_name_key_fallback(self, store: Store, frozen_now: float) -> None:
        host = FakeHost(info={"name": "乙群", "member_count": 3})
        p = _make(store, host)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT name, member_count FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["name"] == "乙群"
        assert row["member_count"] == 3

    @pytest.mark.asyncio
    async def test_group_info_unavailable_keeps_empty_and_retries(
        self, store: Store, frozen_now: float
    ) -> None:
        host = FakeHost(info={})
        p = _make(store, host)
        await p.tick(GID)
        await p.tick(GID)
        assert host.info_calls == [GID, GID]  # 拿不到就跳过重试
        row = store.read().execute(
            "SELECT name, info_ts FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        # 群里本来就没名（空）→ 视为「没拿到」不写库：name 保持空、info_ts 仍 0，下轮重试
        assert row["name"] == ""
        assert row["info_ts"] == 0


# ----------------------------------------------------------------------
# 画像条目的管理员操作
# ----------------------------------------------------------------------


class TestEntries:
    def _seed(self, store: Store, p: Profiles) -> None:
        # 人造三条消息，只为后面 focus 测试有名有姓
        p.remember_session(GID, "sess-1", T0)

    def test_add_entry_validates_category(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        with pytest.raises(ValueError):
            p.add_entry(GID, "不存在类", "文本")
        with pytest.raises(ValueError):
            p.add_entry(GID, "recent", "")  # 空文本也拒
        eid = p.add_entry(GID, "recent", "最近在聊装修")
        assert isinstance(eid, int) and eid > 0

    def test_add_entry_source_admin_and_locked(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        eid = p.add_entry(GID, "interest", "长期兴趣是硬件折腾")
        row = store.read().execute(
            "SELECT * FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["group_id"] == GID
        assert row["category"] == "interest"
        assert row["source"] == "admin"
        assert row["locked"] == 1
        assert row["deleted"] == 0
        assert row["first_ts"] == T0 and row["last_ts"] == T0

    def test_entries_excludes_deleted_and_sorted(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        a = p.add_entry(GID, "recent", "旧条目")
        b = p.add_entry(GID, "recent", "新条目")
        c = p.add_entry(GID, "ongoing", "在做的事")
        # 让 b 的 last_ts 更大（通过再 edit 触发 updated/last_ts 也行，这里直接改库模拟模型更新）
        with store.tx() as conn:
            conn.execute("UPDATE profile_entries SET last_ts=? WHERE id=?", (T0 + 100, b))
        p.delete_entry(c)
        entries = p.entries(GID)
        assert [e["id"] for e in entries] == [b, a]  # deleted=1 的不出来；recent 里新的在前
        assert all(e["category"] == "recent" for e in entries)

    def test_edit_entry_text_auto_locks(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        eid = p.add_entry(GID, "convention", "老规矩")
        with store.tx() as conn:
            conn.execute("UPDATE profile_entries SET locked=0 WHERE id=?", (eid,))
        p.edit_entry(eid, text="新规矩")
        row = store.read().execute(
            "SELECT text, locked, last_ts FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["text"] == "新规矩"
        assert row["locked"] == 1  # 改文字自动锁
        assert row["last_ts"] == T0

    def test_edit_entry_locked_only(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        eid = p.add_entry(GID, "resource", "常用资源")
        p.edit_entry(eid, locked=False)
        row = store.read().execute(
            "SELECT text, locked FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["text"] == "常用资源"  # 文字不动
        assert row["locked"] == 0
        p.edit_entry(eid, locked=True)
        row = store.read().execute(
            "SELECT locked FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["locked"] == 1

    def test_delete_entry_tombstone(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        eid = p.add_entry(GID, "recent", "要删的")
        p.delete_entry(eid)
        row = store.read().execute(
            "SELECT deleted FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["deleted"] == 1  # 墓碑，不是真删行
        assert p.entries(GID) == []

    def test_entry_writes_are_events(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        eid = p.add_entry(GID, "recent", "第一条")
        p.edit_entry(eid, text="改")
        p.delete_entry(eid)
        rows = store.read().execute(
            "SELECT kind FROM events WHERE group_id=? AND entity='profile_entry' ORDER BY id",
            (GID,),
        ).fetchall()
        kinds = [r["kind"] for r in rows]
        assert kinds == [
            "profile_entry.add",
            "profile_entry.edit",
            "profile_entry.delete",
        ]


# ----------------------------------------------------------------------
# 群脉搏 pulse
# ----------------------------------------------------------------------


class TestPulse:
    @pytest.mark.asyncio
    async def test_pulse_alignment_and_zero_fill(self, store: Store, frozen_now: float) -> None:
        # 桶 B-1 两条、B-2 一条、B-4 一条；B（end 所在桶）和 B-3 没有 → 补 0
        bin_now = (T0 // 900) * 900
        assert bin_now + 900 > T0  # 保证下面的消息时间都不超过「现在」
        msgs = [
            _msg("m1", bin_now - 900 + 10),
            _msg("m2", bin_now - 900 + 500),
            _msg("m3", bin_now - 2 * 900 + 5),
            _msg("m4", bin_now - 4 * 900 + 800),
        ]
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)

        pulse = p.pulse(GID, end=T0, hours=2)
        assert len(pulse) == 8
        assert pulse[-1] == 0  # end(T0) 所在桶没有消息
        assert pulse[-2] == 2
        assert pulse[-3] == 1
        assert pulse[-4] == 0  # 缺的补 0
        assert pulse[-5] == 1
        assert sum(pulse[:3]) == 0

    @pytest.mark.asyncio
    async def test_pulse_end_on_bin_edge(self, store: Store, frozen_now: float) -> None:
        bin_now = (T0 // 900) * 900
        host = FakeHost([_msg("m1", bin_now - 900 + 1)])
        p = _make(store, host)
        await p.tick(GID)
        # end 正好压桶边界：floor 对齐后最后一桶就是 end 所在的 bin
        pulse = p.pulse(GID, end=bin_now, hours=1)
        assert len(pulse) == 4
        assert pulse[-1] == 0
        assert pulse[-2] == 1

    def test_pulse_empty_group_all_zero(self, store: Store) -> None:
        p = _make(store, FakeHost())
        assert p.pulse(GID, end=T0, hours=24) == [0] * 96


# ----------------------------------------------------------------------
# usual_gap：平时这个钟点的发言间隔
# ----------------------------------------------------------------------


def _same_hour_ts(days_ago: int, minute_offset: int = 0) -> float:
    """days_ago 天前、北京时间和 T0 同一个钟点（14 点）的 ts。"""
    bj_midnight = clock.bj(T0).replace(hour=0, minute=0, second=0, microsecond=0)
    base = bj_midnight.timestamp() - days_ago * 86400
    return base + 14 * 3600 + 300 + minute_offset  # 14:05 起，同一小时


class TestUsualGap:
    @pytest.mark.asyncio
    async def test_too_few_samples_returns_none(self, store: Store, frozen_now: float) -> None:
        msgs = []
        for d in (1, 2, 3):  # 只有 3 天有数据（< 5）
            msgs.append(_msg(f"m{d}", _same_hour_ts(d)))
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)
        assert p.usual_gap(GID, T0) is None

    @pytest.mark.asyncio
    async def test_enough_samples_value(self, store: Store, frozen_now: float) -> None:
        msgs = []
        counts = {1: 1, 2: 2, 3: 1, 4: 2, 5: 1, 6: 2}  # 6 天共 9 条
        for d, n in counts.items():
            for i in range(n):
                msgs.append(_msg(f"m{d}_{i}", _same_hour_ts(d, i * 60)))
        # 今天同一钟点也来 5 条——不该算进去
        today_same_hour = [
            _msg(f"t{i}", clock.bj(T0).replace(hour=14, minute=10 + i, second=0, microsecond=0).timestamp())
            for i in range(5)
        ]
        host = FakeHost(msgs + today_same_hour)
        p = _make(store, host)
        await p.tick(GID)
        gap = p.usual_gap(GID, T0)
        assert gap is not None
        # n = 9 条 / 6 天 = 每小时 1.5 条 → 3600 / 1.5 = 2400 秒
        assert gap == pytest.approx(2400.0)

    @pytest.mark.asyncio
    async def test_other_hour_not_counted(self, store: Store, frozen_now: float) -> None:
        msgs = []
        for d in range(1, 8):  # 7 天，但都在早上 8 点（和 14 点不同钟点）
            bj_midnight = clock.bj(T0).replace(hour=0, minute=0, second=0, microsecond=0)
            msgs.append(_msg(f"m{d}", bj_midnight.timestamp() - d * 86400 + 8 * 3600))
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)
        assert p.usual_gap(GID, T0) is None

    def test_no_data_returns_none(self, store: Store) -> None:
        p = _make(store, FakeHost())
        assert p.usual_gap(GID, T0) is None


# ----------------------------------------------------------------------
# 关注成员 focus / set_focus
# ----------------------------------------------------------------------

REASON_ACTIVE = "最活跃"
REASON_REQ = "提过请求"
REASON_PIN = "管理员加的"


def _speaker_msgs(prefix: str, n: int, user: str, name: str, step: int = 500) -> list[Msg]:
    """某个成员在 10 天内的 n 条普通发言。"""
    out = []
    base = T0 - 10 * 86400
    for i in range(n):
        out.append(_msg(f"{prefix}{i}", base + i * step, user=user, name=name))
    return out


class TestFocus:
    @pytest.mark.asyncio
    async def test_reasons_assigned_and_removed_excluded(
        self, store: Store, frozen_now: float
    ) -> None:
        msgs: list[Msg] = []
        # 发言数：a 5、b 4、c 3 —— 活跃前三
        msgs += _speaker_msgs("a", 5, "ua", "小A")
        msgs += _speaker_msgs("b", 4, "ub", "小B")
        msgs += _speaker_msgs("c", 3, "uc", "小C")
        # 互动多但发言少：x 有 3 次 @、不再因为「和 MaiBot 聊得多」入选（已退役，2026-09-27）
        msgs.append(_msg("bot0", T0 - 80000, user="botqq", name="MaiBot", bot=True))
        for i in range(3):
            msgs.append(_msg(f"xa{i}", T0 - 70000 + i, user="ux", name="小X", at=True))
        # d 回复了 MaiBot 3 次；但被管理员手工排除（removed 排除依然生效）
        for i in range(3):
            msgs.append(_msg(f"d{i}", T0 - 60000 + i, user="ud", name="小D", reply="bot0"))
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)

        p.set_focus(GID, "ud", "remove")
        got = p.focus(GID)
        by_user = {m["user_id"]: m for m in got}
        assert set(by_user) == {"ua", "ub", "uc"}
        assert by_user["ua"]["reasons"] == [REASON_ACTIVE]
        assert by_user["ua"]["name"] == "小A"
        # 互动多的人不再自动入选
        assert "ux" not in by_user
        assert "ud" not in by_user
        assert all("note" in m and "pinned" in m for m in got)

    @pytest.mark.asyncio
    async def test_chatty_only_user_not_selected_but_pinned_ok(
        self, store: Store, frozen_now: float
    ) -> None:
        """互动多但发言少的成员不再因为「和 MaiBot 聊得多」入选；管理员手动加才进。"""
        # 3 个更活跃的人压住 x 的 3 条发言：uc 和 ux 并列 3 条时按 user_id 顺序 uc 在前
        msgs = (
            _speaker_msgs("a", 5, "ua", "小A")
            + _speaker_msgs("b", 4, "ub", "小B")
            + _speaker_msgs("c", 3, "uc", "小C")
        )
        msgs.append(_msg("bot0", T0 - 80000, user="botqq", name="MaiBot", bot=True))
        for i in range(3):
            msgs.append(_msg(f"xa{i}", T0 - 70000 + i, user="ux", name="小X", at=True))
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)
        got0 = p.focus(GID)
        assert "ux" not in {m["user_id"] for m in got0}  # 互动多但发言第三开外：不入库
        p.set_focus(GID, "ux", "add")  # 管理员加
        got = p.focus(GID)
        by_user = {m["user_id"]: m for m in got}
        assert set(by_user) == {"ua", "ub", "uc", "ux"}
        assert by_user["ux"]["reasons"] == [REASON_PIN]  # 只有「管理员加的」，没有 CHAT
        assert by_user["ux"]["pinned"] is True

    @pytest.mark.asyncio
    async def test_merged_reasons_and_requesters(
        self, store: Store, frozen_now: float
    ) -> None:
        msgs = _speaker_msgs("a", 5, "ua", "小A")
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)
        got = p.focus(GID, requesters=["ua", "uz"])
        by_user = {m["user_id"]: m for m in got}
        assert by_user["ua"]["reasons"] == [REASON_ACTIVE, REASON_REQ]
        assert by_user["uz"]["reasons"] == [REASON_REQ]

    @pytest.mark.asyncio
    async def test_pinned_survives_and_max_members_caps(
        self, store: Store, frozen_now: float
    ) -> None:
        # max_members=2：活跃前 2 能进，pinned 的人保住（把人挤出来）
        settings_raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
            "focus": {"max_members": 2},
            "profile": {"backfill_days": 30},
        }
        settings, problems = load_settings(settings_raw)
        assert not problems
        msgs = _speaker_msgs("a", 5, "ua", "小A") + _speaker_msgs("b", 4, "ub", "小B")
        host = FakeHost(msgs)
        p = _make(store, host, settings=settings)
        await p.tick(GID)
        p.set_focus(GID, "up", "add")  # 管理员加（无任何发言 / 互动，不占活跃名额）
        got = p.focus(GID, requesters=["ur"])
        by_user = {m["user_id"]: m for m in got}
        assert len(got) <= 2  # 上限
        assert by_user["up"]["pinned"] is True
        assert REASON_PIN in by_user["up"]["reasons"]
        assert set(by_user["up"]["reasons"]) == {REASON_PIN}  # 只「管理员加的」，没别的
        # 库里落行数也不超过上限
        rows = store.read().execute(
            "SELECT user_id FROM focus_members WHERE group_id=? AND removed=0", (GID,)
        ).fetchall()
        assert len(rows) == 2
        assert {r["user_id"] for r in rows} == {m["user_id"] for m in got}
        # 请求者没被加进库（落选）
        assert "ur" not in {r["user_id"] for r in rows}

    @pytest.mark.asyncio
    async def test_dropout_removes_row_and_note(self, store: Store, frozen_now: float) -> None:
        host = FakeHost(_speaker_msgs("a", 3, "ua", "小A"))
        p = _make(store, host)
        await p.tick(GID)
        got = p.focus(GID)
        assert [m["user_id"] for m in got] == ["ua"]
        # 手工塞一条 note（模拟第二部分写过个人画像）
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET note='爱聊硬件' WHERE group_id=? AND user_id=?",
                (GID, "ua"),
            )
        # ua 之后 30 天没再说话 → 落选 → 删行，note 一起没了
        clock.now = lambda: T0 + 31 * 86400
        got2 = p.focus(GID)
        assert got2 == []
        row = store.read().execute(
            "SELECT * FROM focus_members WHERE group_id=? AND user_id=?", (GID, "ua")
        ).fetchone()
        assert row is None
        # pinned 的落选不删行
        p.set_focus(GID, "ub", "add")
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET note='管理员在意的人' WHERE group_id=? AND user_id=?",
                (GID, "ub"),
            )
        got3 = p.focus(GID)
        assert [m["user_id"] for m in got3] == ["ub"]
        row = store.read().execute(
            "SELECT note, pinned FROM focus_members WHERE group_id=? AND user_id=?",
            (GID, "ub"),
        ).fetchone()
        assert row["note"] == "管理员在意的人"
        assert row["pinned"] == 1

    @pytest.mark.asyncio
    async def test_personal_profile_off_clears_all_notes(
        self, store: Store, frozen_now: float
    ) -> None:
        settings_raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
            "focus": {"personal_profile": False},
            "profile": {"backfill_days": 30},
        }
        settings, problems = load_settings(settings_raw)
        assert not problems
        host = FakeHost(_speaker_msgs("a", 3, "ua", "小A") + _speaker_msgs("b", 2, "ub", "小B"))
        p = _make(store, host, settings=settings)
        await p.tick(GID)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, pinned, updated)"
                " VALUES (?, 'uleft', '旧人', '旧 note', 1, ?)",
                (GID, T0),
            )
        got = p.focus(GID)
        assert {m["user_id"] for m in got} >= {"ua", "uleft"}
        rows = store.read().execute(
            "SELECT user_id, note FROM focus_members WHERE group_id=?", (GID,)
        ).fetchall()
        assert all(r["note"] == "" for r in rows)  # 所有 note 清空

    @pytest.mark.asyncio
    async def test_set_focus_row_management(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        await p.tick(GID)
        p.set_focus(GID, "unew", "add")  # 行不存在就建
        row = store.read().execute(
            "SELECT pinned, removed, note FROM focus_members WHERE group_id=? AND user_id=?",
            (GID, "unew"),
        ).fetchone()
        assert (row["pinned"], row["removed"], row["note"]) == (1, 0, "")
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET note='有点东西' WHERE group_id=? AND user_id=?",
                (GID, "unew"),
            )
        p.set_focus(GID, "unew", "remove")
        row = store.read().execute(
            "SELECT pinned, removed, note FROM focus_members WHERE group_id=? AND user_id=?",
            (GID, "unew"),
        ).fetchone()
        assert (row["pinned"], row["removed"], row["note"]) == (0, 1, "")  # note 清掉
        p.set_focus(GID, "unew", "auto")
        row = store.read().execute(
            "SELECT pinned, removed FROM focus_members WHERE group_id=? AND user_id=?",
            (GID, "unew"),
        ).fetchone()
        assert (row["pinned"], row["removed"]) == (0, 0)
        # 加回来的 removed=0,pinned=1
        p.set_focus(GID, "unew", "add")
        row = store.read().execute(
            "SELECT pinned, removed FROM focus_members WHERE group_id=? AND user_id=?",
            (GID, "unew"),
        ).fetchone()
        assert (row["pinned"], row["removed"]) == (1, 0)

    @pytest.mark.asyncio
    async def test_set_focus_events(self, store: Store, frozen_now: float) -> None:
        p = _make(store, FakeHost())
        await p.tick(GID)
        p.set_focus(GID, "u1", "add")
        p.set_focus(GID, "u1", "remove")
        rows = store.read().execute(
            "SELECT kind FROM events WHERE entity='focus_member' ORDER BY id"
        ).fetchall()
        assert [r["kind"] for r in rows] == ["focus_member.set", "focus_member.set"]

    @pytest.mark.asyncio
    async def test_set_focus_invalid_action(self, store: Store) -> None:
        p = _make(store, FakeHost())
        with pytest.raises(ValueError):
            p.set_focus(GID, "u1", "乱来")


# ----------------------------------------------------------------------
# tick：last_msg_ts（网页「安静多久」用）
# ----------------------------------------------------------------------


class TestLastMsgTs:
    @pytest.mark.asyncio
    async def test_tick_updates_last_msg_ts_to_latest_new_message(
        self, store: Store, frozen_now: float
    ) -> None:
        host = FakeHost([_msg("m1", T0 - 300), _msg("m2", T0 - 120), _msg("m3", T0 - 50)])
        p = _make(store, host)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT last_msg_ts FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["last_msg_ts"] == T0 - 50  # 本轮新消息的最大 ts

        # 再来一条更旧的（乱序插入也不回退）和一条更新的
        host.msgs.append(_msg("m0", T0 - 400))
        host.msgs.append(_msg("m4", T0 - 10))
        await p.tick(GID)
        row = store.read().execute(
            "SELECT last_msg_ts FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["last_msg_ts"] == T0 - 10

    @pytest.mark.asyncio
    async def test_focus_uses_latest_name(self, store: Store, frozen_now: float) -> None:
        msgs = [
            _msg("n0", T0 - 5 * 86400, user="ua", name="旧名"),
            _msg("n1", T0 - 80000, user="ua", name="中间名"),
            _msg("n2", T0 - 60, user="ua", name="新名字"),
        ]
        host = FakeHost(msgs)
        p = _make(store, host)
        await p.tick(GID)
        got = p.focus(GID)
        assert got[0]["name"] == "新名字"


class TestMemberInteractionsDropped:
    def test_table_dropped_by_migration(self, store: Store) -> None:
        """2026-10 docs/18 第一步：互动计数彻底退役，表在库迁移里 DROP。"""
        tables = {r["name"] for r in store.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "member_interactions" not in tables

    def test_upgrade_drops_table_with_rows(self, tmp_path) -> None:
        """老库（表还在、且有数据）→ migrate 完表没了。"""
        import sqlite3

        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE member_interactions (group_id TEXT, user_id TEXT, day TEXT,"
            " count INTEGER, PRIMARY KEY (group_id, user_id, day))"
        )
        conn.execute("INSERT INTO member_interactions VALUES ('g1', 'u1', '2026-10-01', 3)")
        conn.execute("PRAGMA user_version=31")
        conn.commit()
        conn.close()
        s = Store(path)
        try:
            s.migrate()
            tables = {r["name"] for r in s.read().execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "member_interactions" not in tables
        finally:
            s.close()
