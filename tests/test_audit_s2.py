"""S2 回归测试：用户正在等的交付不受睡觉时段/每日上限限制、也不计入上限。

依据 02 §6.4：「只有故障报错和用户当时正在等的交付不受限」。
- push_kind="delivery"（交付成品 + 交付说明）：睡觉时段照发、超过每日上限照发、
  不占每日上限额度；
- push_kind="topic" / "status" / "reminder"：照旧受睡觉时段和每日上限约束。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

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


class TestDeliveryExempt:
    async def test_delivery_not_postponed_by_sleep_hours(self, tmp_path):
        """睡觉时段：delivery 照发；status 照旧推迟。"""
        store, _, host, _, ob = _make(tmp_path)
        ob.enqueue("d1", GID, "text", {"text": "成品来了", "push_kind": "delivery"})
        ob.enqueue("s1", GID, "text", {"text": "状态汇报", "push_kind": "status"})
        await ob.flush(SLEEP)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["d1"]["status"] == "sent"
        assert rows["s1"]["status"] == "pending"
        assert "睡觉" in rows["s1"]["error"]
        assert [t["text"] for t in host.texts] == ["成品来了"]

    async def test_delivery_not_limited_by_daily_cap(self, tmp_path):
        """每日上限 1：delivery 连发两条都发出去；status 第二条被推迟到明天。"""
        store, _, host, pushes, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
        ob.enqueue("d1", GID, "text", {"text": "成品一", "push_kind": "delivery"})
        ob.enqueue("d2", GID, "text", {"text": "成品二", "push_kind": "delivery"})
        ob.enqueue("st1", GID, "text", {"text": "状态一", "push_kind": "status"})
        ob.enqueue("st2", GID, "text", {"text": "状态二", "push_kind": "status"})
        await ob.flush(NOON)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["d1"]["status"] == "sent"
        assert rows["d2"]["status"] == "sent"
        # status 第一条占掉上限发出去，第二条推迟
        assert rows["st1"]["status"] == "sent"
        assert rows["st2"]["status"] == "pending"
        assert rows["st2"]["not_before"] >= _ts(0, day=16)

    async def test_delivery_does_not_count_toward_cap(self, tmp_path):
        """delivery 不占每日上限额度：先发 delivery，status 第一条照样能发。"""
        store, _, host, pushes, ob = _make(tmp_path, cfg={"delivery": {"push_per_day": 1}})
        ob.enqueue("d1", GID, "text", {"text": "成品", "push_kind": "delivery"})
        ob.enqueue("s1", GID, "text", {"text": "冷场开场白", "push_kind": "topic"})
        await ob.flush(NOON)
        rows = {r["key"]: r for r in _rows(store)}
        assert rows["d1"]["status"] == "sent"
        # delivery 没占额度 → topic 这一条还在上限内，能发
        assert rows["s1"]["status"] == "sent"

    def test_can_push_delivery_always_true(self, tmp_path):
        """Pushes.can_push：delivery 在睡觉时段也 True；topic / status / reminder False。"""
        _, _, _, pushes, _ = _make(tmp_path)
        assert pushes.can_push(GID, "delivery", SLEEP) == (True, "")
        ok_topic, why_topic = pushes.can_push(GID, "topic", SLEEP)
        ok_status, _ = pushes.can_push(GID, "status", SLEEP)
        ok_remind, _ = pushes.can_push(GID, "reminder", SLEEP)
        assert not ok_topic and "睡觉" in why_topic
        assert not ok_status
        assert not ok_remind

    def test_count_for_day_excludes_delivery(self, tmp_path):
        """当日计数（受限口径）不含 delivery；记录照记（count_today 全口径仍看得到）。"""
        store, _, _, pushes, _ = _make(tmp_path)
        pushes.record(GID, "delivery", "成品", NOON)
        pushes.record(GID, "status", "状态", NOON)
        # 受限口径：只看 status（delivery 豁免）
        assert pushes._count_for_day(GID, NOON, kind=None) == 1
        # 但记录照记（deliver 的也留痕，供 count_today 全口径/网页统计）
        kinds = {
            r["kind"]
            for r in store.read().execute("SELECT kind FROM pushes WHERE group_id=?", (GID,)).fetchall()
        }
        assert kinds == {"delivery", "status"}

    async def test_reminder_still_postponed(self, tmp_path):
        """reminder（目标提醒）在睡觉时段照旧推迟——它不是「用户正在等的交付」。"""
        store, _, host, _, ob = _make(tmp_path)
        ob.enqueue("r1", GID, "text", {"text": "@小明 提醒", "push_kind": "reminder"})
        await ob.flush(SLEEP)
        row = _rows(store)[0]
        assert row["status"] == "pending"
        assert "睡觉" in row["error"]
        assert not host.texts
