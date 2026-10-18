"""Feature shutdown boundaries: real isolated SQLite, fake sender, no network."""
from __future__ import annotations

import pytest

from test_outbox import GID, NOON, _make, _seed_task
from CharTyr_MaiWork.maiwork import clock, group_features, group_push
from CharTyr_MaiWork.maiwork.goals import Goals, STALE_TIMEOUT_REASON


CASES = [
    ("news_card", "news_card:1", "news", None),
    ("news_card", "news_card:1:fallback", "news", None),
    ("idea_mention", "idea_mention:1", "idea", None),
    ("goal_result", "goal-found:G-1:1", "task", None),
    ("goal_stale", "goal-stale:G-1:today", "task", None),
    ("reminder", "goal-remind:M-1:today", "task", None),
    ("status", "goal-ask:M-1", "task", None),
    ("status", "goal-renew:M-1", "task", None),
    ("status", "task-wait-remind:T-1", "task", "T-1"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("push_kind,key,module,task_id", CASES)
@pytest.mark.parametrize("reopen", [False, True])
async def test_shutdown_discards_unsent_module_messages(tmp_path, monkeypatch, push_kind, key, module, task_id, reopen):
    store, settings, host, _, _, ob = _make(tmp_path)
    try:
        monkeypatch.setattr(clock, "now", lambda: NOON)
        group_push.set_config(store, GID, {"news_card_enabled": True, "idea_mention_enabled": True}, settings)
        _seed_task(store)
        oid = ob.enqueue(key, GID, "text", {"text": "旧消息", "push_kind": push_kind}, task_id=task_id)
        group_features.set_config(store, GID, {module: False}, settings, now=NOON + 1)
        if reopen:
            group_features.set_config(store, GID, {module: True}, settings, now=NOON + 2)
        outcomes = []
        ob.add_result_hook(lambda info: outcomes.append(info["outcome"]))
        await ob.flush(NOON + 3)
        row = store.read().execute("SELECT status, attempts FROM outbox WHERE id=?", (oid,)).fetchone()
        assert host.texts == []
        assert row["status"] == "dropped"
        assert row["attempts"] == 0
        assert outcomes == ["dropped"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_delivery_can_finish_while_tasks_disabled(tmp_path, monkeypatch):
    store, settings, host, _, _, ob = _make(tmp_path)
    try:
        monkeypatch.setattr(clock, "now", lambda: NOON)
        _seed_task(store)
        ob.enqueue("task:T-1:deliver", GID, "text", {"text": "成品", "push_kind": "delivery"}, task_id="T-1")
        group_features.set_config(store, GID, {"task": False}, settings, now=NOON + 1)
        await ob.flush(NOON + 2)
        assert len(host.texts) == 1
        assert host.texts[0]["text"] == "成品"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_new_module_message_after_reenable_can_send(tmp_path, monkeypatch):
    store, settings, host, _, _, ob = _make(tmp_path)
    try:
        group_push.set_config(store, GID, {"idea_mention_enabled": True}, settings)
        group_features.set_config(store, GID, {"idea": False}, settings, now=NOON)
        group_features.set_config(store, GID, {"idea": True}, settings, now=NOON + 1)
        monkeypatch.setattr(clock, "now", lambda: NOON + 2)
        ob.enqueue("idea_mention:2", GID, "text", {"text": "新构想", "push_kind": "idea_mention"})
        await ob.flush(NOON + 3)
        assert len(host.texts) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_message_enqueued_while_disabled_stays_discarded_after_reenable(tmp_path, monkeypatch):
    store, settings, host, _, _, ob = _make(tmp_path)
    try:
        group_push.set_config(store, GID, {"idea_mention_enabled": True}, settings)
        group_features.set_config(store, GID, {"idea": False}, settings, now=NOON)
        monkeypatch.setattr(clock, "now", lambda: NOON + 1)
        oid = ob.enqueue("idea_mention:late", GID, "text", {"text": "晚到消息", "push_kind": "idea_mention"})
        group_features.set_config(store, GID, {"idea": True}, settings, now=NOON + 2)
        await ob.flush(NOON + 3)
        assert not host.texts
        assert store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()["status"] == "dropped"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,push_kind", [("topic:1", "topic"), ("task:T-1:status", "status")])
async def test_unrelated_topics_and_task_status_can_finish(tmp_path, monkeypatch, key, push_kind):
    store, settings, host, _, _, ob = _make(tmp_path)
    try:
        monkeypatch.setattr(clock, "now", lambda: NOON)
        _seed_task(store)
        ob.enqueue(key, GID, "text", {"text": "正常收尾", "push_kind": push_kind}, task_id="T-1" if push_kind == "status" else None)
        group_features.set_config(store, GID, {"news": False, "idea": False, "task": False}, settings, now=NOON + 1)
        await ob.flush(NOON + 2)
        assert len(host.texts) == 1
    finally:
        store.close()


def _goal(store, settings, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOON)
    goals = Goals(store, lambda: settings)
    gid = goals.create_agent(GID, title="检查", body="", criteria=["有证据"], by_text="管理员")
    return goals, gid


def test_stale_refresh_leaves_disabled_goals_untouched(tmp_path, monkeypatch):
    store, settings, _, _, _, _ = _make(tmp_path)
    try:
        goals, gid = _goal(store, settings, monkeypatch)
        group_features.set_config(store, GID, {"task": False}, settings, now=NOON + 1)
        before = goals.get(gid)
        assert goals.refresh_stale(NOON + 86400, models_ready=True) == []
        assert goals.get(gid) == before
    finally:
        store.close()


def test_stale_refresh_restarts_timeout_after_reenable(tmp_path, monkeypatch):
    store, settings, _, _, _, _ = _make(tmp_path)
    try:
        goals, gid = _goal(store, settings, monkeypatch)
        group_features.set_config(store, GID, {"task": False}, settings, now=NOON + 1)
        resumed = NOON + 86400
        group_features.set_config(store, GID, {"task": True}, settings, now=resumed)
        assert goals.refresh_stale(resumed + 1, models_ready=True) == []
        assert not goals.get(gid)["stale_reason"]
        assert goals.refresh_stale(resumed + 7201, models_ready=True) == [gid]
        assert goals.get(gid)["stale_reason"] == STALE_TIMEOUT_REASON
    finally:
        store.close()
