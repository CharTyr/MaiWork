"""问题 A 回归：后台循环不能被群画像提炼卡住；read_interval_minutes 要生效。

- tick(refresh=False)：只读消息 + 统计，不调模型；要不要提炼由返回值 needs_refresh 说。
- 读消息频率：本轮有信号（has_signal=True）最多 60 秒一次；没信号按 read_interval_minutes。
- app.run_loop_once：needs_refresh 时用长任务机制后台跑 profiles.refresh，
  模型再慢（asyncio.Event 挡住）这一轮也照常在短时间内跑完，outbox.flush / goals 巡检照常。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeHost, FakeModelsQueue, FakeProfiles

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.host import Msg
from CharTyr_MaiWork.profile import Profiles
from CharTyr_MaiWork.store import Store

GID = "900000001"
# 2026-09-26 06:05 UTC = 北京时间 14:05（周六）
T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()


def _settings(**profile_over):
    profile = {"backfill_days": 30, "backfill_max_messages": 1500, "batch_messages": 3}
    profile.update(profile_over)
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "profile": profile,
    }
    settings, problems = load_settings(raw)
    assert not problems
    return settings


def _msg(mid: str, ts: float, user: str = "u1", name: str = "阿一", *, text: str = "你好") -> Msg:
    return Msg(
        id=mid, ts=ts, user_id=user, user_name=name, text=text,
        is_bot=False, is_at=False, is_picture=False, reply_to="",
    )


@pytest.fixture
def movable_now(monkeypatch: pytest.MonkeyPatch):
    """clock.now 钉在一个可以往后拨的盒子里。"""
    box = {"t": T0}
    monkeypatch.setattr(clock, "now", lambda: box["t"])
    return box


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


# ----------------------------------------------------------------------
# tick 拆快慢：refresh=False 不调模型，只报「要不要提炼」
# ----------------------------------------------------------------------


class TestTickSplit:
    @pytest.mark.asyncio
    async def test_deferred_tick_reports_needs_refresh_without_model_call(
        self, store: Store, movable_now
    ) -> None:
        settings = _settings()  # batch=3
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = Profiles(store, host, models, lambda: settings)
        r = await p.tick(GID, refresh=False, has_signal=True)
        assert r.read == 3
        assert r.refreshed is False
        assert r.needs_refresh is True  # 攒够了，该后台提炼
        assert models.calls == []  # 但 tick 自己没调模型
        # 后台 refresh 才真提炼
        ok = await p.refresh(GID)
        assert ok is True
        assert len(models.calls) == 1
        row = store.read().execute(
            "SELECT pending_count, profile_ready_ts FROM groups WHERE group_id=?", (GID,)
        ).fetchone()
        assert row["pending_count"] == 0
        assert row["profile_ready_ts"] > 0

    @pytest.mark.asyncio
    async def test_deferred_tick_below_batch_no_need(
        self, store: Store, movable_now
    ) -> None:
        settings = _settings()  # batch=3
        models = FakeModelsQueue(replies=['{"ops": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(2)])  # 只 2 条
        p = Profiles(store, host, models, lambda: settings)
        p.ensure_group(GID)
        with store.tx() as conn:
            # 画像已成形：排除「首次只要 pending>0 就提炼」的触发
            conn.execute(
                "UPDATE groups SET profile_ready_ts=?, last_refresh_ts=? WHERE group_id=?",
                (T0 - 600, T0 - 600, GID),
            )
        r = await p.tick(GID, refresh=False, has_signal=True)
        assert r.read == 2
        assert r.needs_refresh is False
        assert models.calls == []

    @pytest.mark.asyncio
    async def test_default_tick_keeps_inline_refresh_backward_compatible(
        self, store: Store, movable_now
    ) -> None:
        """旧语义：tick 默认照样在内部提炼（老测试 / 手动调用不变）。"""
        settings = _settings()
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = Profiles(store, host, models, lambda: settings)
        r = await p.tick(GID)  # 不传 refresh：默认 inline
        assert r.refreshed is True
        assert len(models.calls) == 1

    @pytest.mark.asyncio
    async def test_force_needs_refresh_reported_in_deferred_mode(
        self, store: Store, movable_now
    ) -> None:
        settings = _settings()
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg("m1", T0 - 10)])
        p = Profiles(store, host, models, lambda: settings)
        r = await p.tick(GID, force=True, refresh=False, has_signal=False)
        assert r.needs_refresh is True
        assert models.calls == []


# ----------------------------------------------------------------------
# 每周整理也挪到后台 refresh 里（deferred 模式 tick 不做）
# ----------------------------------------------------------------------


class TestWeeklyDeferred:
    @pytest.mark.asyncio
    async def test_weekly_runs_in_background_refresh_not_in_tick(
        self, store: Store, movable_now
    ) -> None:
        # T0 是北京周六（weekday()==5），weekly_day=5 才对得上
        settings = _settings(weekly_day=5, batch_messages=1000)
        host = FakeHost()  # 无新消息
        models0 = FakeModelsQueue()
        p0 = Profiles(store, host, models0, lambda: settings)
        p0.ensure_group(GID)
        with store.tx() as conn:
            conn.execute(
                "UPDATE groups SET profile_ready_ts=?, last_refresh_ts=? WHERE group_id=?",
                (T0 - 86400, T0, GID),
            )
        models = FakeModelsQueue(replies=['{"ops": []}'])
        p = Profiles(store, host, models, lambda: settings)
        eid = p.add_entry(GID, "recent", "有条目才值得每周整理")
        with store.tx() as conn:  # 管理员条目默认锁，解锁模拟模型条目好被整理
            conn.execute("UPDATE profile_entries SET locked=0 WHERE id=?", (eid,))
        r = await p.tick(GID, refresh=False, has_signal=False)
        # 到点了：tick 没调模型，但报了「该后台跑一次」
        assert r.needs_refresh is True
        assert models.calls == []
        await p.refresh(GID)
        assert len(models.calls) == 1
        assert "weekly" in str(models.calls[0][2].get("purpose", ""))


# ----------------------------------------------------------------------
# 读消息频率：有信号 60 秒、没信号 read_interval_minutes
# ----------------------------------------------------------------------


class TestReadInterval:
    @pytest.mark.asyncio
    async def test_signal_reads_at_most_every_60_seconds(
        self, store: Store, movable_now
    ) -> None:
        settings = _settings(read_interval_minutes=10)
        host = FakeHost([_msg("m0", T0 - 100), _msg("m1", T0 - 50)])
        p = Profiles(store, host, FakeModelsQueue(ready=False), lambda: settings)
        r1 = await p.tick(GID, refresh=False, has_signal=True)
        assert r1.read == 2
        calls_after_first = len(host.msg_calls)
        assert calls_after_first > 0
        # 同一时刻（0 秒过去）再来：有信号也不重读
        r2 = await p.tick(GID, refresh=False, has_signal=True)
        assert r2.read == 0
        assert len(host.msg_calls) == calls_after_first
        # 61 秒后：有信号 → 读
        movable_now["t"] = T0 + 61
        host.msgs.append(_msg("m2", T0 + 30))
        r3 = await p.tick(GID, refresh=False, has_signal=True)
        assert r3.read == 1
        assert len(host.msg_calls) > calls_after_first

    @pytest.mark.asyncio
    async def test_no_signal_uses_read_interval_minutes(
        self, store: Store, movable_now
    ) -> None:
        settings = _settings(read_interval_minutes=10)
        host = FakeHost([_msg("m0", T0 - 100)]
        )
        p = Profiles(store, host, FakeModelsQueue(ready=False), lambda: settings)
        await p.tick(GID, refresh=False, has_signal=True)
        calls_after_first = len(host.msg_calls)
        # 没信号：61 秒也不够，要等 10 分钟
        movable_now["t"] = T0 + 61
        r = await p.tick(GID, refresh=False, has_signal=False)
        assert r.read == 0
        assert len(host.msg_calls) == calls_after_first
        movable_now["t"] = T0 + 599
        r = await p.tick(GID, refresh=False, has_signal=False)
        assert r.read == 0
        assert len(host.msg_calls) == calls_after_first
        movable_now["t"] = T0 + 601
        r = await p.tick(GID, refresh=False, has_signal=False)
        assert r.read == 0  # 没新消息，但确实去读了一次（msg_calls 涨了）
        assert len(host.msg_calls) > calls_after_first

    @pytest.mark.asyncio
    async def test_default_call_has_no_throttle(
        self, store: Store, movable_now
    ) -> None:
        """不传 has_signal（老调用方式）：不做频率限制，每次都读。"""
        settings = _settings(read_interval_minutes=10)
        host = FakeHost([_msg("m0", T0 - 100)])
        p = Profiles(store, host, FakeModelsQueue(ready=False), lambda: settings)
        await p.tick(GID)
        n = len(host.msg_calls)
        await p.tick(GID)  # 立刻再 tick：老语义照样读
        assert len(host.msg_calls) > n


# ----------------------------------------------------------------------
# app 层：慢提炼不挡后台循环
# ----------------------------------------------------------------------


def _raw(data_dir: Path, listen: str = "127.0.0.1:18662") -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "console": {"listen": listen, "password": "pw-测试"},
        "storage": {"data_dir": str(data_dir)},
    }


def _app(tmp_path: Path) -> MaiWorkApp:
    return MaiWorkApp(
        FakeCtx({}),
        _raw(tmp_path / "data"),
        plugin_dir=Path(__file__).resolve().parents[1],
    )


class TestLoopNotBlockedByDistill:
    @pytest.mark.asyncio
    async def test_run_loop_once_returns_fast_and_m3_round_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profiles = FakeProfiles()
        profiles.next_needs_refresh = True  # 每轮都「该提炼」
        profiles.refresh_block = asyncio.Event()  # 提炼被永远挡住（模拟模型很慢）
        app = _app(tmp_path)
        app.profiles_factory = lambda *a, **kw: profiles
        await app.start()
        try:
            flush_calls: list[float] = []

            async def _flush(now):
                flush_calls.append(float(now))

            due_calls: list[float] = []

            def _due(now):
                due_calls.append(float(now))
                return []

            monkeypatch.setattr(app.outbox, "flush", _flush)
            monkeypatch.setattr(app.goals, "due", _due)
            t0 = time.monotonic()
            await asyncio.wait_for(app.run_loop_once(), timeout=10)
            took = time.monotonic() - t0
            assert took < 5, f"后台循环被提炼卡住了（{took:.1f} 秒）"
            assert flush_calls, "outbox.flush 这一轮必须跑到"
            assert due_calls, "goals 巡检这一轮必须跑到"
            # 提炼被丢进后台长任务：让它真的开始跑（被 Event 挡住）
            for _ in range(100):
                if profiles.refresh_calls:
                    break
                await asyncio.sleep(0.02)
            assert profiles.refresh_calls, "profiles.refresh 应该在后台跑起来"
            # 上一轮的后台提炼还没跑完（被 Event 挡着）→ 这一轮不重复开
            n = len(profiles.refresh_calls)
            await asyncio.wait_for(app.run_loop_once(), timeout=10)
            await asyncio.sleep(0.1)
            assert len(profiles.refresh_calls) == n, "同群同种同时只跑一个"
        finally:
            profiles.refresh_block.set()  # 放走，stop 不卡
            await app.stop()

    @pytest.mark.asyncio
    async def test_loop_spawns_refresh_only_when_needed(self, tmp_path: Path) -> None:
        profiles = FakeProfiles()  # next_needs_refresh 默认 False
        app = _app(tmp_path)
        app.profiles_factory = lambda *a, **kw: profiles
        await app.start()
        try:
            await app.run_loop_once()
            await asyncio.sleep(0.05)
            assert profiles.refresh_calls == []
        finally:
            await app.stop()
