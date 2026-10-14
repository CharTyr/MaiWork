"""待判 @ 请求的持久化与恢复（2026-10-13 生产巡检：9 条 pending_asks 搁了 3~15 天无人判）。

要点：
- 判断结论（decision）先落库、和提炼游标同一个事务，落地失败不丢；
- 落地失败按指数退避重试；明确判成闲聊（none）才算终态；
- 超过 48 小时还没落地的留着标「待复核」，不悄悄丢；
- 恢复与画像游标无关、可重复跑，不重复建请求；
- 提醒：目标 + 回话 + 完成标记同一个事务；
- v40 老库迁移：已 handled 的行成终态，未处理的保持待判。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fakes import FakeHost, FakeModelsQueue

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.pending_asks import PendingAsks
from CharTyr_MaiWork.maiwork.profile import Profiles, _parse_bj_when
from CharTyr_MaiWork.maiwork.store import _MIGRATIONS, Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

GID = "900000001"
OTHER = "999999"
T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()


def _settings():
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "profile": {"backfill_days": 30, "backfill_max_messages": 1500, "batch_messages": 3},
    }
    settings, problems = load_settings(raw)
    assert not problems
    return settings


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch):
    box = {"t": T0}
    monkeypatch.setattr(clock, "now", lambda: box["t"])
    return box


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


def _msg(mid: str, ts: float, user: str = "u1", name: str = "阿一", text: str = "你好") -> Msg:
    return Msg(id=mid, ts=ts, user_id=user, user_name=name, text=text,
               is_bot=False, is_at=False, is_picture=False, reply_to="")


def _row(store: Store, mid: str, gid: str = GID):
    return store.read().execute(
        "SELECT * FROM pending_asks WHERE group_id=? AND message_id=?", (gid, mid)
    ).fetchone()


def _count(store: Store, sql: str, *args) -> int:
    return int(store.read().execute(sql, args).fetchone()[0])


class _Graph:
    def __init__(self, store: Store, host: FakeHost, models: FakeModelsQueue) -> None:
        settings = _settings()
        self.tasks = Tasks(store, lambda: settings)
        self.goals = Goals(store, lambda: settings)
        self.approvals = Approvals(store, lambda: settings, self.tasks, self.goals)
        self.outbox = Outbox(store, host, Pushes(store, lambda: settings), Mentions(store, lambda: settings),
                             lambda: settings)
        self.profiles = Profiles(store, host, models, lambda: settings)
        self.profiles.set_request_deps(approvals=self.approvals, goals=self.goals, outbox=self.outbox)
        self.asks = self.profiles.pending_asks


def _msg_dict(mid: str, ts: float = T0 - 60, text: str = "@MaiBot 帮我整理资料") -> dict:
    return {"message_id": mid, "user_id": "u2", "user_name": "阿二", "text": text, "timestamp": ts}


# ----------------------------------------------------------------------
# PendingAsks 基本契约
# ----------------------------------------------------------------------


class TestPendingAsksApi:
    def test_record_keeps_first_seen_and_never_reopens(self, store: Store, now) -> None:
        asks = PendingAsks(store, _settings)
        assert asks.record_pending(GID, _msg_dict("m1", T0 - 600), "jev_无答案") is not None
        now["t"] = T0 + 3600
        asks.record_pending(GID, _msg_dict("m1", T0 + 3000, text="改过的文字"), "再来一次")
        r = _row(store, "m1")
        assert r["ts"] == T0 - 600 and r["first_seen"] == T0 - 600
        assert r["text"] == "改过的文字"
        assert asks.decide(GID, "m1", {"kind": "none"}) is True
        asks.record_pending(GID, _msg_dict("m1"), "又来")
        r = _row(store, "m1")
        assert r["state"] == "none" and r["handled"] == 1  # 终态不被重新打开

    def test_non_served_group_writes_nothing(self, store: Store, now) -> None:
        asks = PendingAsks(store, _settings)
        assert asks.record_pending(OTHER, _msg_dict("x1"), "jev_无答案") is None
        assert asks.decide(OTHER, "x1", {"kind": "prepare", "title": "x"}) is False
        assert _count(store, "SELECT COUNT(*) FROM pending_asks") == 0

    def test_decision_is_json_and_handled_tracks_terminal(self, store: Store, now) -> None:
        asks = PendingAsks(store, _settings)
        asks.record_pending(GID, _msg_dict("m1"), "r")
        assert asks.decide(GID, "m1", {"kind": "prepare", "title": "整理资料", "via": "测试"}) is True
        r = _row(store, "m1")
        assert r["state"] == "decided" and r["handled"] == 0
        assert json.loads(r["decision"])["kind"] == "prepare"
        assert asks.complete(GID, "m1", result_kind="request", result_id="") is False  # 没有真 id 不算完成
        assert asks.complete(GID, "m1", result_kind="request", result_id="R-1") is True
        r = _row(store, "m1")
        assert r["state"] == "completed" and r["handled"] == 1 and r["result_id"] == "R-1"
        assert asks.decide(GID, "m1", {"kind": "goal", "title": "改主意"}) is False

    def test_screened_is_terminal_with_empty_id(self, store: Store, now) -> None:
        asks = PendingAsks(store, _settings)
        asks.record_pending(GID, _msg_dict("m1"), "r")
        asks.decide(GID, "m1", {"kind": "goal", "title": "x"})
        assert asks.complete(GID, "m1", result_kind="screened") is True
        r = _row(store, "m1")
        assert r["state"] == "screened" and r["handled"] == 1

    def test_reminder_without_time_stays_pending_with_partial_intent(self, store: Store, now) -> None:
        asks = PendingAsks(store, _settings)
        asks.record_pending(GID, _msg_dict("m1"), "r")
        assert asks.decide(GID, "m1", {"kind": "reminder", "title": "交周报", "when": "明早"}) is False
        r = _row(store, "m1")
        assert r["state"] == "pending" and r["handled"] == 0
        assert json.loads(r["decision"])["kind"] == "reminder"
        assert asks.decide(GID, "m1", {"kind": "reminder", "title": "交周报", "when": "2026-09-27 09:00"}) is True
        d = json.loads(_row(store, "m1")["decision"])
        assert d["remind_ts"] == _parse_bj_when("2026-09-27 09:00")

    def test_fail_backs_off_exponentially(self, store: Store, now) -> None:
        asks = PendingAsks(store, _settings)
        asks.record_pending(GID, _msg_dict("m1"), "r")
        asks.decide(GID, "m1", {"kind": "prepare", "title": "x"})
        asks.fail(GID, "m1", "boom")
        r1 = _row(store, "m1")
        asks.fail(GID, "m1", "boom2")
        r2 = _row(store, "m1")
        assert r1["attempts"] == 1 and r2["attempts"] == 2
        assert r2["next_try_ts"] - T0 > r1["next_try_ts"] - T0 > 0
        assert r2["last_error"] == "boom2" and r2["state"] == "decided" and r2["handled"] == 0
        assert asks.due(GID, now=T0) == []
        assert [d["message_id"] for d in asks.due(GID, now=r2["next_try_ts"])] == ["m1"]


# ----------------------------------------------------------------------
# 画像提炼：结论和游标同一事务；落地失败可恢复
# ----------------------------------------------------------------------


class _BrokenApprovals:
    def __init__(self, real: Approvals) -> None:
        self.real = real
        self.fail = True

    def create(self, *a, **kw):
        if self.fail:
            raise RuntimeError("模拟落地失败")
        return self.real.create(*a, **kw)


class TestProfileDurability:
    @pytest.mark.asyncio
    async def test_landing_failure_keeps_decision_and_recovery_lands_once(self, store: Store, now) -> None:
        reply = '{"asks": [{"i": 1, "kind": "prepare", "title": "整理清单"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"帮我整理清单{i}") for i in range(3)])
        g = _Graph(store, host, models)
        broken = _BrokenApprovals(g.approvals)
        g.profiles.set_request_deps(approvals=broken)
        r = await g.profiles.tick(GID)
        assert r.refreshed is True
        row = _row(store, "m0")
        assert row is not None and row["state"] == "decided" and row["handled"] == 0
        assert json.loads(row["decision"])["title"] == "整理清单"
        assert row["attempts"] == 1
        assert _count(store, "SELECT COUNT(*) FROM requests") == 0
        # 游标已推进（这批不再重读），但结论还在
        broken.fail = False
        assert await g.profiles.recover_pending_asks(GID) == 0  # 还没到退避时间
        now["t"] = float(row["next_try_ts"]) + 1
        assert await g.profiles.recover_pending_asks(GID) == 1
        assert await g.profiles.recover_pending_asks(GID) == 0
        assert _count(store, "SELECT COUNT(*) FROM requests WHERE message_id='m0'") == 1
        row = _row(store, "m0")
        assert row["state"] == "completed" and row["handled"] == 1 and row["result_id"]

    @pytest.mark.asyncio
    async def test_decision_committed_with_cursor_before_landing(self, store: Store, now) -> None:
        reply = '{"asks": [{"i": 1, "kind": "goal", "title": "盯铝价"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models)
        seen = {}

        class Probe:
            def create(self, gid, **kw):
                seen["row"] = _row(store, kw["message_id"])
                seen["refresh"] = store.read().execute(
                    "SELECT last_refresh_ts FROM groups WHERE group_id=?", (GID,)
                ).fetchone()[0]
                return g.approvals.create(gid, **kw)

        g.profiles.set_request_deps(approvals=Probe())
        await g.profiles.tick(GID)
        assert seen["row"] is not None and seen["row"]["state"] == "decided"
        assert seen["refresh"] > 0

    @pytest.mark.asyncio
    async def test_explicit_none_is_terminal(self, store: Store, now) -> None:
        reply = '{"asks": [{"i": 2, "kind": "none"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models)
        g.asks.record_pending(GID, {"message_id": "m1", "user_id": "u1", "text": "@MaiBot 哈哈",
                                    "timestamp": T0 - 99}, "jev_无答案")
        await g.profiles.tick(GID)
        r = _row(store, "m1")
        assert r["state"] == "none" and r["handled"] == 1

    @pytest.mark.asyncio
    async def test_reminder_lands_atomically_and_once(self, store: Store, now) -> None:
        reply = '{"asks": [{"i": 1, "kind": "reminder", "title": "交周报", "when": "2026-09-27 09:00"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg("m1", T0 - 60, user="u7", name="阿七", text="提醒我明早交周报"),
                         _msg("m2", T0 - 50), _msg("m3", T0 - 40)])
        g = _Graph(store, host, models)

        class BrokenOutbox:
            def enqueue(self, *a, **kw):
                raise RuntimeError("回话入队失败")

        g.profiles.set_request_deps(outbox=BrokenOutbox())
        await g.profiles.tick(GID)
        # 回话失败 → 目标也不能留下半截
        assert _count(store, "SELECT COUNT(*) FROM goals") == 0
        assert _row(store, "m1")["state"] == "decided"
        g.profiles.set_request_deps(outbox=g.outbox)
        now["t"] = float(_row(store, "m1")["next_try_ts"]) + 1
        assert await g.profiles.recover_pending_asks(GID) == 1
        assert await g.profiles.recover_pending_asks(GID) == 0
        assert _count(store, "SELECT COUNT(*) FROM goals") == 1
        assert _count(store, "SELECT COUNT(*) FROM outbox WHERE key LIKE 'ask-remind:%'") == 1
        r = _row(store, "m1")
        assert r["state"] == "completed" and r["result_kind"] == "reminder" and r["result_id"]


# ----------------------------------------------------------------------
# 独立恢复：不依赖画像游标；48 小时待复核
# ----------------------------------------------------------------------


class TestRecovery:
    @pytest.mark.asyncio
    async def test_undecided_old_ask_classified_independent_of_cursor(self, store: Store, now) -> None:
        reply = '{"asks": [{"i": 1, "kind": "prepare", "title": "整理资料"}]}'
        models = FakeModelsQueue(replies=[reply])
        g = _Graph(store, FakeHost([]), models)
        g.asks.record_pending(GID, _msg_dict("old", T0 - 3 * 3600), "jev_无答案")
        assert await g.profiles.recover_pending_asks(GID) == 1
        assert len(models.calls) == 1
        assert _count(store, "SELECT COUNT(*) FROM requests WHERE message_id='old'") == 1

    @pytest.mark.asyncio
    async def test_unanswered_classification_is_not_none(self, store: Store, now) -> None:
        models = FakeModelsQueue(replies=['{"asks": []}'])
        g = _Graph(store, FakeHost([]), models)
        g.asks.record_pending(GID, _msg_dict("old", T0 - 3 * 3600), "jev_无答案")
        assert await g.profiles.recover_pending_asks(GID) == 0
        r = _row(store, "old")
        assert r["state"] == "pending" and r["handled"] == 0 and r["attempts"] == 1
        # 退避期内不重复花钱
        await g.profiles.recover_pending_asks(GID)
        assert len(models.calls) == 1

    @pytest.mark.asyncio
    async def test_stale_ask_flagged_for_review_not_discarded(self, store: Store, now) -> None:
        models = FakeModelsQueue(replies=[])
        g = _Graph(store, FakeHost([]), models)
        g.asks.record_pending(GID, _msg_dict("stale", T0 - 49 * 3600), "jev_无答案")
        assert await g.profiles.recover_pending_asks(GID) == 0
        r = _row(store, "stale")
        assert r is not None and r["review"] == 1 and r["state"] == "review"
        assert models.calls == []  # 过期的不再调模型
        assert _count(store, "SELECT COUNT(*) FROM requests") == 0

    @pytest.mark.asyncio
    async def test_non_served_group_recovery_is_zero_work(self, store: Store, now) -> None:
        models = FakeModelsQueue(replies=[])
        g = _Graph(store, FakeHost([]), models)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO pending_asks (group_id, message_id, ts, reason, handled)"
                " VALUES (?, 'x', ?, 'r', 0)", (OTHER, T0 - 3 * 3600)
            )
        assert await g.profiles.recover_pending_asks(OTHER) == 0
        assert models.calls == []


# ----------------------------------------------------------------------
# 来源消息去重 / 原子写
# ----------------------------------------------------------------------


class TestDedupAndAtomic:
    def test_approvals_create_dedups_by_source_message(self, store: Store, now) -> None:
        settings = _settings()
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        ap = Approvals(store, lambda: settings, tasks, goals)
        a = ap.create(GID, kind="task", title="一", quote="q", via="v", requester_id="u",
                      requester_name="n", message_id="m1")
        b = ap.create(GID, kind="task", title="二", quote="q", via="v", requester_id="u",
                      requester_name="n", message_id="m1")
        assert a["id"] and b["id"] == a["id"]
        assert _count(store, "SELECT COUNT(*) FROM requests WHERE message_id='m1'") == 1

    def test_member_goal_and_outbox_join_caller_transaction(self, store: Store, now) -> None:
        settings = _settings()
        goals = Goals(store, lambda: settings)
        outbox = Outbox(store, FakeHost([]), Pushes(store, lambda: settings), Mentions(store, lambda: settings),
                        lambda: settings)
        with pytest.raises(RuntimeError):
            with store.tx() as conn:
                goals.create_member(GID, who_id="u", who_name="n", title="t", due_ts=T0, remind_ts=T0, conn=conn)
                outbox.enqueue("ask-remind:x", GID, "text", {"text": "x"}, conn=conn)
                raise RuntimeError("回滚")
        assert _count(store, "SELECT COUNT(*) FROM goals") == 0
        assert _count(store, "SELECT COUNT(*) FROM outbox") == 0


# ----------------------------------------------------------------------
# 迁移：v40 老库
# ----------------------------------------------------------------------


def test_migration_from_v40_backfills_state(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    for fn in _MIGRATIONS[:40]:
        fn(conn)
    conn.execute("PRAGMA user_version=40")
    conn.execute("INSERT INTO pending_asks (group_id, message_id, ts, reason, handled)"
                 " VALUES (?, 'done', 100, 'r', 1)", (GID,))
    conn.execute("INSERT INTO pending_asks (group_id, message_id, ts, reason, handled)"
                 " VALUES (?, 'open', 200, 'r', 0)", (GID,))
    conn.close()
    s = Store(path)
    try:
        assert s.migrate() == len(_MIGRATIONS) >= 41
        assert s.path == path
        done = s.read().execute("SELECT * FROM pending_asks WHERE message_id='done'").fetchone()
        opened = s.read().execute("SELECT * FROM pending_asks WHERE message_id='open'").fetchone()
        assert done["state"] == "completed" and done["handled"] == 1 and done["result_kind"] == "legacy"
        assert opened["state"] == "pending" and opened["handled"] == 0
        assert opened["first_seen"] == 200
    finally:
        s.close()
