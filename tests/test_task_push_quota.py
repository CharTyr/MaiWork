"""任务自己的消息不占、也不吃每群每日推送额度（用户 2026-10-10 定的口径）。

生产事故：2026-10-10 测试群（`daily_max`=5）17:00 就把额度用完了（2 条资讯卡片 +
1 条构想提一嘴 + 2 条任务交付）。17:08 任务 T-14 需要发起人重发一张图，那条状态行
（outbox key `human:T-14:2`、push_kind=status、task_id=T-14）被推迟到次日 07:00
（「推迟：今天推够了」），发起人一直没被问到。

口径（只改这一类，其它一个字都不动）：
- outbox 行 **task_id 非空** + 载荷 push_kind ∈ {delivery, status}（含交付自动补的
  说明行）→ 闸门按 quota-free kind（`task_delivery` / `task_status`）走：只查服务群 +
  睡觉时段，不看每日额度、也不吃每群开关；留痕按新 kind 记，`count_used` /
  `used_today` 不再算它。
- 睡觉时段照旧推迟（醒来才发）；非服务群照旧不发；`awaited_delivery` 照旧完全豁免。
- 不带 task_id 的行（topic / news_card / idea_mention / 成员提醒 / 目标巡检 /
  不带任务的状态行）节制规则一个字都不变。

全部用真 Store / 真 Outbox / 假宿主，不联网、不调模型。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import (
    TASK_QUOTA_FREE_KINDS,
    UNSERVED_REASON,
    Mentions,
    Pushes,
    task_quota_free_kind,
)
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"   # 服务中的测试群
GONE = "111111111"  # 不在服务名单里的群


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    """北京时间 2026-10-{day} 某时刻的 epoch。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)                  # 非睡觉时段
SLEEP = _ts(23, 30)             # 默认睡觉时段 23:00-08:00 之内
AFTER_QUIET = _ts(8, 0, day=16)  # 那段睡觉时段结束的那一刻
QUIET = "23:00-08:00"


class _Host:
    """假宿主：只记文字 / 群文件；可预置文本发送异常队列。"""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.uploads: list[dict] = []
        self.text_errors: list[BaseException] = []

    async def send_text(self, session_id: str, text: str, *, reply_to: str = "",
                        at_user: str = "", at_name: str = "") -> Any:
        self.texts.append({"session_id": session_id, "text": text, "at_user": at_user})
        if self.text_errors:
            raise self.text_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": f"m{len(self.texts)}"})()

    async def upload_group_file(self, group_id: str, path: str, name: str) -> str:
        self.uploads.append({"group_id": group_id, "path": path, "name": name})
        return "file-id-1"


def _make(tmp_path, *, serve: tuple[str, ...] = (GID,)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings, problems = load_settings({
        "environments": {"workspace_root": str(tmp_path)},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
    })
    assert not problems, problems
    host = _Host()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "sess-1"))
    return store, settings, host, pushes, mentions, ob


def _seed_cap(store: Store, settings: Any, pushes: Pushes, *, daily_max: int = 5,
              used: int = 5, quiet: str = QUIET, now: float = NOON) -> None:
    """定好这个群的每日上限 / 睡觉时段，并先占掉 `used` 份额度（kind=topic，受限的）。"""
    group_push.set_config(
        store, GID,
        {"daily_max": int(daily_max), "quiet_hours": quiet, "news_card_enabled": True},
        settings, now=float(now) - 10,
    )
    for i in range(int(used)):
        pushes.record(GID, "topic", f"已经发过的第 {i + 1} 条", float(now))


def _rows(store: Store):
    return store.read().execute(
        "SELECT id, key, kind, payload, status, error, not_before, task_id"
        " FROM outbox ORDER BY id"
    ).fetchall()


def _by_key(store: Store, key: str):
    return [r for r in _rows(store) if r["key"] == key][0]


def _push_kinds(store: Store) -> list[str]:
    return [r["kind"] for r in store.read().execute("SELECT kind FROM pushes").fetchall()]


# ----------------------------------------------------------------------
# 口径本身：认哪些行、认成什么 kind
# ----------------------------------------------------------------------


async def test_quota_free_kind_only_recognizes_task_rows():
    """只有 task_id 非空 + delivery/status 才算任务自己的消息；其余原样。"""
    assert TASK_QUOTA_FREE_KINDS == frozenset(("task_delivery", "task_status"))
    assert task_quota_free_kind("T-14", "status") == "task_status"
    assert task_quota_free_kind("T-14", "delivery") == "task_delivery"
    # 没有 task_id：成员提醒 / 目标巡检 / 群公告预告这些状态行一点都不变
    assert task_quota_free_kind("", "status") == "status"
    assert task_quota_free_kind(None, "delivery") == "delivery"
    assert task_quota_free_kind("   ", "status") == "status"
    # 带 task_id 但不是这两种 kind 的（卡片 / 提一嘴 / 明确领取）不算
    assert task_quota_free_kind("T-14", "topic") == "topic"
    assert task_quota_free_kind("T-14", "news_card") == "news_card"
    assert task_quota_free_kind("T-14", "awaited_delivery") == "awaited_delivery"


async def test_can_push_quota_free_kind_skips_cap_but_not_quiet_or_served(tmp_path):
    """Pushes.can_push 对 quota-free kind：服务群 + 睡觉时段照查，每日额度不查。"""
    store, settings, _host, pushes, _m, _ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    # 额度满了也放行（任务自己的消息）
    assert pushes.can_push(GID, "task_status", NOON) == (True, "")
    assert pushes.can_push(GID, "task_delivery", NOON) == (True, "")
    # 睡觉时段照拦
    assert pushes.can_push(GID, "task_status", SLEEP) == (False, "睡觉时段")
    assert pushes.can_push(GID, "task_delivery", SLEEP) == (False, "睡觉时段")
    # 非服务群照拦
    assert pushes.can_push(GONE, "task_status", NOON) == (False, UNSERVED_REASON)
    assert pushes.can_push(GONE, "task_delivery", NOON) == (False, UNSERVED_REASON)
    # 对照组：受限的 kind 额度还是满的
    assert pushes.can_push(GID, "news_card", NOON) == (False, "今天推够了")


# ----------------------------------------------------------------------
# 额度满了：任务自己的消息照发、且不占额度
# ----------------------------------------------------------------------


async def test_task_status_at_cap_is_sent_and_does_not_consume_quota(tmp_path):
    """额度用满时，任务状态行（提问 / 需要人 / 失败通知）照发，且不占额度。

    线上 2026-10-10：T-14 要发起人重发一张图，却被「今天推够了」推到次日 07:00。
    """
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    assert pushes.count_used(GID, NOON) == 5
    ob.enqueue("human:T-14:2", GID, "text",
               {"text": "@小林 这张图看不出来，麻烦重发一张", "push_kind": "status"},
               task_id="T-14")
    await ob.flush(NOON)
    row = _by_key(store, "human:T-14:2")
    assert row["status"] == "sent", row["error"]
    assert [t["text"] for t in host.texts] == ["@小林 这张图看不出来，麻烦重发一张"]
    # 不占额度：发完还是 5/5，受限的资讯卡片照样被拦
    assert pushes.count_used(GID, NOON) == 5
    assert pushes.can_push(GID, "news_card", NOON) == (False, "今天推够了")
    # 留痕按 quota-free kind 记（额度口径一致）
    assert _push_kinds(store).count("task_status") == 1


async def test_task_delivery_at_cap_is_sent_and_does_not_consume_quota(tmp_path):
    """额度用满时，任务交付消息照发，且不占额度。"""
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    ob.enqueue("task:T-9:deliver:text", GID, "text",
               {"text": "「周报」做好了：都在附件里", "push_kind": "delivery"}, task_id="T-9")
    await ob.flush(NOON)
    row = _by_key(store, "task:T-9:deliver:text")
    assert row["status"] == "sent", row["error"]
    assert [t["text"] for t in host.texts] == ["「周报」做好了：都在附件里"]
    assert pushes.count_used(GID, NOON) == 5
    assert _push_kinds(store).count("task_delivery") == 1
    # 老的 delivery 口径没被任务行污染（受限留痕还是 0 条）
    assert pushes.count_today(GID, "delivery") == 0


async def test_task_file_delivery_and_followup_note_do_not_use_quota(tmp_path):
    """群文件交付 + 自动补的说明行：都发得出去，说明行也不多记一笔。"""
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    f = tmp_path / "报告.html"
    f.write_text("<html>ok</html>", encoding="utf-8")
    ob.enqueue("task:T-9:deliver:file", GID, "file",
               {"path": str(f), "name": "报告.html", "note": "成品在群文件里",
                "push_kind": "delivery"},
               task_id="T-9")
    await ob.flush(NOON)
    rows = {r["key"]: r for r in _rows(store)}
    assert rows["task:T-9:deliver:file"]["status"] == "sent"
    assert rows["task:T-9:deliver:file:note"]["status"] == "sent"
    assert [t["text"] for t in host.texts] == ["成品在群文件里"]
    assert len(host.uploads) == 1
    assert pushes.count_used(GID, NOON) == 5
    assert _push_kinds(store).count("task_delivery") == 1  # 说明行不占第二笔


async def test_uncertain_task_status_does_not_reserve_quota(tmp_path):
    """结果不明（超时）的任务状态行也不保留额度：它不是群的主动推送。"""
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    host.text_errors.append(asyncio.TimeoutError())
    ob.enqueue("human:T-14:2", GID, "text",
               {"text": "麻烦重发一张图", "push_kind": "status"}, task_id="T-14")
    await ob.flush(NOON)
    row = _by_key(store, "human:T-14:2")
    assert row["status"] == "uncertain"
    assert pushes.count_used(GID, NOON) == 5


# ----------------------------------------------------------------------
# 额度满了：不带 task_id 的行照旧推迟（节制不变）
# ----------------------------------------------------------------------


async def test_rows_without_task_still_postponed_at_cap(tmp_path):
    """不带 task_id 的行：开场白 / 资讯卡片 / 目标巡检状态行 / 成员提醒照旧推迟。"""
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    ob.enqueue("k:topic", GID, "text", {"text": "冷场开场白", "push_kind": "topic"})
    ob.enqueue("k:card", GID, "text", {"text": "资讯卡片", "push_kind": "news_card"})
    ob.enqueue("k:status", GID, "text", {"text": "目标进展怎么样", "push_kind": "status"})
    ob.enqueue("k:remind", GID, "text", {"text": "@阿柒 提醒：交周报", "push_kind": "reminder"})
    await ob.flush(NOON)
    assert host.texts == []
    for key in ("k:topic", "k:card", "k:status", "k:remind"):
        row = _by_key(store, key)
        assert row["status"] == "pending", key
        assert "今天推够了" in row["error"], key


# ----------------------------------------------------------------------
# 睡觉时段 / 非服务群 / 明确领取：原样
# ----------------------------------------------------------------------


async def test_task_rows_still_respect_quiet_hours(tmp_path):
    """睡觉时段里任务自己的消息也要推迟，醒来那一轮才发。"""
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=0, quiet=QUIET)
    ob.enqueue("human:T-14:2", GID, "text",
               {"text": "麻烦重发一张图", "push_kind": "status"}, task_id="T-14")
    ob.enqueue("task:T-14:deliver:text", GID, "text",
               {"text": "成品来了", "push_kind": "delivery"}, task_id="T-14")
    await ob.flush(SLEEP)
    assert host.texts == []
    for key in ("human:T-14:2", "task:T-14:deliver:text"):
        row = _by_key(store, key)
        assert row["status"] == "pending", key
        assert "睡觉" in row["error"], key
        assert float(row["not_before"]) == AFTER_QUIET, key
    await ob.flush(AFTER_QUIET)
    assert [r["status"] for r in _rows(store)] == ["sent", "sent"]
    assert sorted(t["text"] for t in host.texts) == ["成品来了", "麻烦重发一张图"]


async def test_task_rows_still_blocked_for_unserved_group(tmp_path):
    """非服务群零发送：任务自己的消息也不能绕过服务群检查。"""
    store, settings, host, _p, _m, ob = _make(tmp_path)
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GONE, "sess-2"))
    ob.enqueue("human:T-14:2", GONE, "text",
               {"text": "任务状态", "push_kind": "status"}, task_id="T-14")
    ob.enqueue("task:T-14:deliver:text", GONE, "text",
               {"text": "任务交付", "push_kind": "delivery"}, task_id="T-14")
    await ob.flush(NOON, allowed_groups={GONE})
    assert host.texts == []
    for key in ("human:T-14:2", "task:T-14:deliver:text"):
        row = _by_key(store, key)
        assert row["status"] == "pending", key
        assert UNSERVED_REASON in row["error"], key


async def test_awaited_delivery_still_fully_exempt(tmp_path):
    """明确领取（awaited_delivery）照旧完全豁免：额度满了 + 睡觉时段都当场发。"""
    store, settings, host, pushes, _m, ob = _make(tmp_path)
    _seed_cap(store, settings, pushes, daily_max=5, used=5)
    ob.enqueue("claim:T-9", GID, "text",
               {"text": "当场索取的成品", "push_kind": "awaited_delivery"}, task_id="T-9")
    await ob.flush(SLEEP)
    row = _by_key(store, "claim:T-9")
    assert row["status"] == "sent", row["error"]
    assert [t["text"] for t in host.texts] == ["当场索取的成品"]
    assert pushes.count_used(GID, SLEEP) == 5
    assert "awaited_delivery" in _push_kinds(store)
