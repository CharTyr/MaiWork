"""推送节制回归：普通交付受限；群友明确领取的待发成品即时交付，不占额度。

依据 docs/02 §6.4：只有通过 /mw 领取 <任务号> 当场索取才能豁免，
而不是将所有自动 delivery 无条件豁免。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

BJ = timezone(timedelta(hours=8))
GID = "900000001"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
SLEEP = _ts(23, 30)


class _Host:
    def __init__(self) -> None:
        self.texts: list[dict] = []

    async def send_text(self, session_id: str, text: str, *, reply_to: str = ""):
        self.texts.append({"session_id": session_id, "text": text, "reply_to": reply_to})
        return type("R", (), {"message_id": "m1"})()


def _make(tmp_path, cfg: dict | None = None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    merged = {
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "environments": {"workspace_root": str(tmp_path / "ws")},
    }
    if cfg:
        merged.update(cfg)
    settings, _ = load_settings(merged)
    host = _Host()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "s1"))
    return store, settings, host, pushes, ob


def _rows(store):
    return store.read().execute(
        "SELECT id, key, status, error, not_before FROM outbox ORDER BY id"
    ).fetchall()


class TestDeliveryThrottling:
    @pytest.mark.asyncio
    async def test_ordinary_delivery_postponed_by_sleep_hours(self, tmp_path):
        store, _, host, _, ob = _make(tmp_path)
        ob.enqueue("d1", GID, "text", {"text": "成品来了", "push_kind": "delivery"})
        ob.enqueue("s1", GID, "text", {"text": "状态汇报", "push_kind": "status"})
        await ob.flush(SLEEP)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["d1"]["status"] == "pending"
        assert "睡觉" in rows["d1"]["error"]
        assert rows["s1"]["status"] == "pending"
        assert not host.texts

    @pytest.mark.asyncio
    async def test_explicitly_awaited_delivery_can_pass_quiet_hours(self, tmp_path):
        store, _, host, _, ob = _make(tmp_path)
        ob.enqueue("d1", GID, "text", {"text": "当场等的成品", "push_kind": "awaited_delivery"})
        await ob.flush(SLEEP)
        assert _rows(store)[0]["status"] == "sent"
        assert [t["text"] for t in host.texts] == ["当场等的成品"]

    @pytest.mark.asyncio
    async def test_delivery_uses_daily_cap(self, tmp_path):
        """每日上限 1：只能发第一条交付，其余主动推送留到明天。"""
        store, _, host, pushes, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
        ob.enqueue("d1", GID, "text", {"text": "成品一", "push_kind": "delivery"})
        ob.enqueue("d2", GID, "text", {"text": "成品二", "push_kind": "delivery"})
        ob.enqueue("st1", GID, "text", {"text": "状态一", "push_kind": "status"})
        ob.enqueue("st2", GID, "text", {"text": "状态二", "push_kind": "status"})
        await ob.flush(NOON)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["d1"]["status"] == "sent"
        assert rows["d2"]["status"] == "pending"
        assert rows["st1"]["status"] == "pending"
        assert rows["st2"]["status"] == "pending"
        assert rows["d2"]["not_before"] >= _ts(0, day=16)

    @pytest.mark.asyncio
    async def test_delivery_counts_toward_cap(self, tmp_path):
        """先发交付已用当天额度，后续开场白要推迟。"""
        store, _, host, pushes, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
        ob.enqueue("d1", GID, "text", {"text": "成品", "push_kind": "delivery"})
        ob.enqueue("s1", GID, "text", {"text": "冷场开场白", "push_kind": "topic"})
        await ob.flush(NOON)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["d1"]["status"] == "sent"
        assert rows["s1"]["status"] == "pending"

    def test_can_push_delivery_only_when_outside_quiet_hours(self, tmp_path):
        _, _, _, pushes, _ = _make(tmp_path)
        assert pushes.can_push(GID, "delivery", SLEEP) == (False, "睡觉时段")
        assert pushes.can_push(GID, "awaited_delivery", SLEEP) == (True, "")
        ok_topic, why_topic = pushes.can_push(GID, "topic", SLEEP)
        ok_status, _ = pushes.can_push(GID, "status", SLEEP)
        ok_remind, _ = pushes.can_push(GID, "reminder", SLEEP)
        assert not ok_topic and "睡觉" in why_topic
        assert not ok_status
        assert not ok_remind

    def test_count_for_day_includes_delivery(self, tmp_path):
        """当日受限口径包含普通交付和状态推送。"""
        store, _, _, pushes, _ = _make(tmp_path)
        pushes.record(GID, "delivery", "成品", NOON)
        pushes.record(GID, "status", "状态", NOON)
        # 受限口径：交付和状态都计入
        assert pushes._count_for_day(GID, NOON, kind=None) == 2
        # 但记录照记（deliver 的也留痕，供 count_today 全口径/网页统计）
        kinds = {
            r["kind"]
            for r in store.read().execute("SELECT kind FROM pushes WHERE group_id=?", (GID,)).fetchall()
        }
        assert kinds == {"delivery", "status"}

    @pytest.mark.asyncio
    async def test_awaited_delivery_ignores_daily_cap_and_does_not_consume_it(self, tmp_path):
        store, _, host, pushes, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
        pushes.record(GID, "status", "昨天还在做", SLEEP)
        ob.enqueue("claim", GID, "text", {"text": "当场索取的成品", "push_kind": "awaited_delivery"})
        ob.enqueue("ordinary", GID, "text", {"text": "普通交付", "push_kind": "delivery"})
        await ob.flush(SLEEP)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["claim"]["status"] == "sent"
        assert rows["ordinary"]["status"] == "pending"
        assert [t["text"] for t in host.texts] == ["当场索取的成品"]
        assert pushes._count_for_day(GID, SLEEP, kind=None) == 1
        assert store.read().execute(
            "SELECT COUNT(*) c FROM pushes WHERE kind='awaited_delivery'"
        ).fetchone()["c"] == 1  # 仍留审计记录，但不占受限额度

    @pytest.mark.asyncio
    async def test_claim_pending_delivery_promotes_delayed_item_once(self, tmp_path, monkeypatch):
        from CharTyr_MaiWork.maiwork import clock
        from CharTyr_MaiWork.maiwork.commands import Commands
        from CharTyr_MaiWork.maiwork.tasks import Tasks

        store, settings, host, pushes, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="成品", req="", criteria=[], source="test")
        tasks.transition(tid, "running")
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "completed", delivery_kind="text")
        pushes.record(GID, "status", "已经推了一条", SLEEP)
        oid = ob.enqueue(f"task:{tid}:deliver:text", GID, "text", {"text": "真正成品", "push_kind": "delivery"}, task_id=tid)
        ob._postpone(oid, GID, "今天推够了", SLEEP)
        cmd = Commands(store, None, tasks, None, ob, None, lambda: settings)
        monkeypatch.setattr(clock, "now", lambda: SLEEP)
        await cmd.handle(GID, "group-member", "群友", f"/mw 领取 {tid}", "request-1")
        row = store.read().execute("SELECT status, payload FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "sent"
        assert json.loads(row["payload"])["push_kind"] == "awaited_delivery"
        assert [t["text"] for t in host.texts].count("真正成品") == 1
        assert pushes._count_for_day(GID, SLEEP, kind=None) == 1
        await cmd.handle(GID, "group-member", "群友", f"/mw 领取 {tid}", "request-2")
        assert [t["text"] for t in host.texts].count("真正成品") == 1

    @pytest.mark.asyncio
    async def test_claim_is_scoped_to_own_group_and_never_retries_uncertain_file(self, tmp_path, monkeypatch):
        from CharTyr_MaiWork.maiwork import clock
        from CharTyr_MaiWork.maiwork.commands import Commands
        from CharTyr_MaiWork.maiwork.tasks import Tasks

        other_gid = "123456789"
        store, settings, host, _, ob = _make(tmp_path, cfg={
            "groups": {"serve": [{"group": f"qq:{GID}"}, {"group": f"qq:{other_gid}"}]},
        })
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (other_gid, "s-other"))
        tasks = Tasks(store, lambda: settings)
        foreign = tasks.create(other_gid, title="别群成品", req="", criteria=[], source="test")
        own = tasks.create(GID, title="本群文件", req="", criteria=[], source="test")
        for tid in (foreign, own):
            tasks.transition(tid, "running")
            tasks.transition(tid, "reviewing")
            tasks.transition(tid, "completed", delivery_kind="file")
        foreign_id = ob.enqueue(f"task:{foreign}:deliver", other_gid, "file", {
            "path": "unreadable", "push_kind": "delivery",
        }, task_id=foreign, not_before=SLEEP + 86400)
        uncertain_id = ob.enqueue(f"task:{own}:deliver", GID, "file", {
            "path": "unreadable", "push_kind": "delivery",
        }, task_id=own)
        with store.tx() as conn:
            conn.execute("UPDATE outbox SET status='uncertain' WHERE id=?", (uncertain_id,))
        cmd = Commands(store, None, tasks, None, ob, None, lambda: settings)
        monkeypatch.setattr(clock, "now", lambda: SLEEP)
        await cmd.handle(GID, "member", "群友", f"/mw 领取 {foreign}", "foreign-request")
        await cmd.handle(GID, "member", "群友", f"/mw 领取 {own}", "uncertain-request")
        rows = store.read().execute(
            "SELECT id, status, payload FROM outbox WHERE id IN (?, ?) ORDER BY id",
            (foreign_id, uncertain_id),
        ).fetchall()
        assert [r["status"] for r in rows] == ["pending", "uncertain"]
        assert all(json.loads(r["payload"])["push_kind"] == "delivery" for r in rows)
        assert "本群没有" in host.texts[0]["text"]
        assert "管理员" in host.texts[1]["text"]

    def test_claim_does_not_mistake_webonly_failure_notice_for_delivered_artifact(self, tmp_path):
        from CharTyr_MaiWork.maiwork.tasks import Tasks

        store, settings, _, _, ob = _make(tmp_path)
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="失败成品", req="", criteria=[], source="test")
        tasks.transition(tid, "running")
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "completed", delivery_kind="file")
        primary = ob.enqueue(f"task:{tid}:deliver", GID, "file", {
            "path": "missing", "push_kind": "delivery",
        }, task_id=tid)
        notice = ob.enqueue(f"task:{tid}:deliver:webonly", GID, "text", {
            "text": "文件和网页都没成功", "push_kind": "delivery",
        }, task_id=tid)
        with store.tx() as conn:
            conn.execute("UPDATE outbox SET status='failed' WHERE id=?", (primary,))
            conn.execute("UPDATE outbox SET status='sent' WHERE id=?", (notice,))
        assert ob.claim_delivery(GID, tid) == "failed"  # 只有失败告知已发，成品根本没送到
        with store.tx() as conn:
            conn.execute("UPDATE outbox SET status='pending' WHERE id=?", (notice,))
        assert ob.claim_delivery(GID, tid) == "failed"  # 也不把失败告知冒充成品升级成免限额
        row = store.read().execute("SELECT payload FROM outbox WHERE id=?", (notice,)).fetchone()
        assert json.loads(row["payload"])["push_kind"] == "delivery"

    @pytest.mark.asyncio
    async def test_claimed_web_delivery_sends_link_followup_without_cap(self, tmp_path):
        store, settings, host, pushes, _ = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})

        class _HereNow:
            async def publish(self, _path):
                return {"url": "https://example.test/finished", "slug": "finished"}

        ob = Outbox(store, host, pushes, Mentions(store, lambda: settings),
                    lambda: settings, herenow=_HereNow())
        pushes.record(GID, "status", "额度已满", SLEEP)
        ob.enqueue("task:T-1:deliver", GID, "herenow", {
            "dir": str(tmp_path), "note": "已完成", "push_kind": "awaited_delivery",
        }, task_id="T-1")
        await ob.flush(SLEEP)
        rows = store.read().execute("SELECT key, status, payload FROM outbox ORDER BY id").fetchall()
        assert [r["status"] for r in rows] == ["sent", "sent"]
        assert json.loads(rows[1]["payload"])["push_kind"] == "awaited_delivery"
        assert "https://example.test/finished" in host.texts[0]["text"]
        assert pushes._count_for_day(GID, SLEEP, kind=None) == 1

    @pytest.mark.asyncio
    async def test_reminder_still_postponed(self, tmp_path):
        """reminder（目标提醒）在睡觉时段照旧推迟——它不是「用户正在等的交付」。"""
        store, _, host, _, ob = _make(tmp_path)
        ob.enqueue("r1", GID, "text", {"text": "@小明 提醒", "push_kind": "reminder"})
        await ob.flush(SLEEP)
        row = _rows(store)[0]
        assert row["status"] == "pending"
        assert "睡觉" in row["error"]
        assert not host.texts
