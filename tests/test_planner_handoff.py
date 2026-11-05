"""Planner handoff helpers, SDK registration, and app routing contracts."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from CharTyr_MaiWork.maiwork import delivery
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.plugin import MaiWorkPlugin
from fakes import hook_message
from test_intake_m3 import _FakeJev, _FakeMentions, _drain, G1
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store


def _payload(**extra):
    return {"session_id": "session", "items": [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": 'system'}]}, {"parts": [{"type": "text", "text": '<message msg_id="m1">make it</message>'}]}], "tool_definitions": [{"name": "maiwork_handoff"}, {"function": {"name": "other"}}], **extra}


def _app(tmp_path):
    app = MaiWorkApp(None, {}, tmp_path)
    app._started = True
    app._settings = SimpleNamespace(is_served=lambda gid: gid == "g", quick_judge=SimpleNamespace(planner_tool=True))
    app._session_to_served_group = {"session": "g"}
    app._feature_on = lambda gid, module: True
    app.mentions = SimpleNamespace(inject=Mock(return_value=None))
    app._intake = SimpleNamespace(note_planner_request=Mock(), note_planner_response=Mock(), handoff_from_planner=AsyncMock(return_value={"success": True, "content": "accepted"}))
    return app


def test_display_id_normalizes_and_hashes():
    assert delivery.to_display_message_id(None) == ""
    assert delivery.to_display_message_id(" 123456789012 ") == "123456789012"
    raw = "a-long-message-id-你好"
    alias = "m" + base64.b32encode(hashlib.sha1(raw.encode("utf-8")).digest()).decode("ascii").lower()[:6]
    assert delivery.to_display_message_id(" " + raw + " ") == alias


def test_note_msg_user_records_alias_within_limit():
    mentions = delivery.Mentions(None, lambda: None)
    mentions._recent_msg_user_limit = 2
    mentions.note_msg_user("g", "a-long-message-id", "u")
    ring = mentions._recent_msg_users["g"]
    assert ring["a-long-message-id"] == ring[delivery.to_display_message_id("a-long-message-id")] == "u"
    mentions.note_msg_user("g", "last", "v")
    assert len(ring) == 2


def test_extract_ids_orders_deduplicates_and_skips_self():
    payload = {"items": [{"parts": [{"type": "text", "text": '<message msg_id="first">a</message><message msg_id="self" is_self_message="true">b</message><message msg_id="second">c</message>'}]}, {"parts": [{"type": "text", "text": '<message msg_id="first">again</message>'}]}, {"parts": [{"type": "text", "text": '<message msg_id="prefix">unfinished'}]}]}
    assert delivery.extract_planner_msg_ids(payload) == ["first", "second", "prefix"]
    assert delivery.extract_planner_msg_ids(None) == []


def test_strip_tool_preserves_items_and_unrelated_definitions():
    payload = _payload(tool_definitions=[{"name": "maiwork_handoff"}, {"function": {"name": "chartyr.maiwork.maiwork_handoff"}}, {"name": "other"}, "opaque"])
    result = delivery.strip_planner_tool(payload)
    assert result is not payload and result["items"] is payload["items"]
    assert result["tool_definitions"] == [{"name": "other"}, "opaque"]
    assert len(payload["tool_definitions"]) == 4
    assert delivery.strip_planner_tool({"tool_definitions": []}) is None
    assert delivery.strip_planner_tool({"tool_definitions": [{"name": "other"}]}) is None


def test_sdk_registration():
    components = MaiWorkPlugin().get_components()
    tool = next(c for c in components if c["name"] == "maiwork_handoff")
    assert tool["type"] == "TOOL"
    assert tool["chat_scope"] == "group"
    assert tool["metadata"]["visibility"] == "visible"
    assert "stop_after_execution" not in str(tool)
    parameters = {p["name"]: p for p in tool["metadata"]["parameters"]}
    assert set(parameters) == {"msg_id", "kind", "title"}
    assert parameters["msg_id"]["param_type"] == "string" and parameters["msg_id"]["required"]
    assert parameters["kind"]["required"] and parameters["kind"]["enum_values"] == ["task", "goal", "reminder"]
    assert parameters["title"]["required"] is False and parameters["title"]["default"] == ""
    assert '<message msg_id="...">' in parameters["msg_id"]["description"]
    assert all(word in parameters["kind"]["description"] for word in ("task", "goal", "reminder", "后台", "长期", "定时"))
    assert "30" in parameters["title"]["description"]
    hook = next(c for c in components if c["name"] == "maiwork_planner_after")
    assert hook["metadata"]["hook"] == "maisaka.planner.after_response"
    assert hook["metadata"]["mode"] == "observe"
    assert hook["metadata"]["timeout_ms"] == 1000
    assert hook["metadata"]["error_policy"] == "skip"


@pytest.mark.asyncio
async def test_plugin_unstarted_strips_and_tool_reports_failure():
    plugin = MaiWorkPlugin()
    payload = _payload()
    result = await plugin.maiwork_mentions(**payload)
    assert result["modified_kwargs"]["tool_definitions"] == [{"function": {"name": "other"}}]
    failure = await plugin.maiwork_handoff(msg_id="m1", kind="task")
    assert failure["success"] is False and failure["error"] == failure["content"]
    assert await plugin.maiwork_planner_after() == {"action": "continue"}


@pytest.mark.parametrize("state", ["unstarted", "unknown", "unserved", "missing_settings", "disabled", "task_off", "no_session", "no_mentions", "ambiguous"])
def test_before_request_strips_ineligible_tool_without_reads(tmp_path, state):
    app = _app(tmp_path)
    payload = _payload()
    if state == "unstarted": app._started = False
    elif state == "unknown": payload["session_id"] = "unknown"
    elif state == "unserved": app._settings.is_served = lambda gid: False
    elif state == "missing_settings": app._settings = None
    elif state == "disabled": app._settings.quick_judge.planner_tool = False
    elif state == "task_off": app._feature_on = lambda *args: False
    elif state == "no_session": payload.pop("session_id")
    elif state == "no_mentions": app.mentions = None
    elif state == "ambiguous": app._ambiguous_sessions.add("session")
    app.store = Mock()
    app.store.read.side_effect = AssertionError("planner routing must not read the database")
    result = app.on_planner_before_request(payload)
    assert result["modified_kwargs"]["tool_definitions"] == [{"function": {"name": "other"}}]
    assert result["modified_kwargs"]["items"] is payload["items"]
    app.store.read.assert_not_called()
    app._intake.note_planner_request.assert_not_called()


def test_before_request_served_notes_and_injects(tmp_path):
    app = _app(tmp_path)
    payload = _payload()
    assert app.on_planner_before_request(payload) == {"action": "continue"}
    app._intake.note_planner_request.assert_called_once_with("g", "session", ["m1"])
    assert app.mentions.inject.call_args.args[0] is payload
    assert app.mentions.inject.call_args.kwargs["group_id"] == "g"
    app._intake.note_planner_request.reset_mock()
    app.on_planner_before_request(_payload(tool_definitions=[]))
    app._intake.note_planner_request.assert_not_called()


def test_after_response_only_routes_served_cached_session(tmp_path):
    app = _app(tmp_path)
    output = [{"type": "text", "text": "hi"}]
    assert app.on_planner_after_response({"session_id": "session", "output_items": output}) == {"action": "continue"}
    app._intake.note_planner_response.assert_called_once_with("g", "session", output)
    app._intake.note_planner_response.reset_mock()
    app.on_planner_after_response({"session_id": "unknown", "output_items": output})
    app._intake.note_planner_response.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_routes_without_trusting_user_id(tmp_path):
    app = _app(tmp_path)
    result = await app.on_planner_handoff({"chat_id": " session ", "platform": " qq ", "msg_id": " m1 ", "kind": " task ", "title": " work ", "user_id": "forged"})
    assert result["success"] is True
    app._intake.handoff_from_planner.assert_awaited_once_with(group_id="g", platform="qq", session_id="session", msg_id="m1", kind="task", title="work")
    app._started = False
    result = await app.on_planner_handoff({"session_id": "session"})
    assert result["success"] is False and result["error"] == result["content"]


@pytest.mark.asyncio
async def test_plugin_forwards_and_contains_callback_exceptions():
    plugin = MaiWorkPlugin()
    plugin._app = SimpleNamespace(on_planner_handoff=AsyncMock(return_value={"success": True}), on_planner_after_response=Mock())
    assert await plugin.maiwork_handoff(msg_id="m1", kind="task") == {"success": True}
    plugin._app.on_planner_handoff.assert_awaited_once_with({"msg_id": "m1", "kind": "task"})
    assert await plugin.maiwork_planner_after(session_id="session", output_items=[]) == {"action": "continue"}
    plugin._app.on_planner_after_response.assert_called_once_with({"session_id": "session", "output_items": []})
    plugin._app.on_planner_handoff.side_effect = RuntimeError("broken")
    result = await plugin.maiwork_handoff(msg_id="m1", kind="task")
    assert result["success"] is False and result["error"] == result["content"]
    plugin._app.on_planner_after_response.side_effect = RuntimeError("broken")
    assert await plugin.maiwork_planner_after() == {"action": "continue"}


def test_before_request_injects_after_stripping_and_keeps_modified_context(tmp_path):
    app = _app(tmp_path)
    app._settings.quick_judge.planner_tool = False
    payload = _payload()
    injected = {"items": ["injected"], "tool_definitions": []}
    app.mentions.inject.return_value = injected
    assert app.on_planner_before_request(payload) == {"action": "continue", "modified_kwargs": injected}
    working = app.mentions.inject.call_args.args[0]
    assert working["tool_definitions"] == [{"function": {"name": "other"}}]
    assert working["items"] is payload["items"]
    assert len(payload["tool_definitions"]) == 2


def test_after_response_unstarted_unserved_and_exceptions_continue(tmp_path):
    app = _app(tmp_path)
    for state in ("unstarted", "unserved", "missing_intake"):
        app._started = state != "unstarted"
        app._settings.is_served = lambda gid: state != "unserved"
        intake = app._intake
        if state == "missing_intake":
            app._intake = None
        assert app.on_planner_after_response({"session_id": "session"}) == {"action": "continue"}
        intake.note_planner_response.assert_not_called()
    app._intake = intake
    app._settings.is_served = lambda gid: True
    app._intake.note_planner_response.side_effect = RuntimeError("broken")
    assert app.on_planner_after_response({"session_id": "session"}) == {"action": "continue"}


@pytest.fixture
def handoff_env(tmp_path):
    settings, _ = load_settings({"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{G1}"}]}, "approval": {"admins": ["qq:admin_B"], "exempt_users": ["qq:admin_B"]}})
    store = Store(tmp_path / "handoff.db")
    store.migrate()
    approvals = Approvals(store, lambda: settings, Tasks(store, lambda: settings), Goals(store, lambda: settings))
    spawned = []
    jev = _FakeJev({"kind": ("prepare", .99, .99)})
    mentions = _FakeMentions()
    intake = Intake(lambda: settings, Signals(), store=store, approvals=approvals, mentions=mentions,
                    jev=jev, bot_qq="bot", spawn=spawned.append, planner_handoff=True,
                    planner_wait_s=.01, planner_extend_s=.01)
    yield SimpleNamespace(intake=intake, store=store, spawned=spawned, jev=jev, mentions=mentions, settings=settings)
    for coro in spawned:
        coro.close()
    store.close()


def calls(*names):
    return [{"item_type": "FunctionCallItem", "tool_call": {"func_name": name}} for name in names]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "goal"])
@pytest.mark.parametrize("at", [False, True])
async def test_handoff_authentic_sender_and_idempotency(handoff_env, kind, at):
    e = handoff_env
    await e.intake.handle(hook_message(group_id=G1, message_id="request1", user_id="user_A", nickname="成员", text="整理一份资料", is_at=at))
    first = await e.intake.handoff_from_planner(group_id=G1, msg_id="request1", kind=kind)
    assert first["success"] and "R-" in first["content"]
    assert await e.intake.handoff_from_planner(group_id=G1, msg_id="request1", kind=kind) == first
    await _drain(e.spawned)
    rows = e.store.read().execute("SELECT * FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["requester_id"] == "user_A"
    assert rows[0]["status"] == "pending"
    assert rows[0]["kind"] == kind
    assert not e.jev.calls


@pytest.mark.asyncio
async def test_alias_and_pending_record_lookup(handoff_env):
    from CharTyr_MaiWork.maiwork.delivery import to_display_message_id
    e = handoff_env
    mid = "ROBOT1.0_long_original_message_id"
    await e.intake.handle(hook_message(group_id=G1, message_id=mid, user_id="user_A", text="准备资料"))
    result = await e.intake.handoff_from_planner(group_id=G1, msg_id=to_display_message_id(mid), kind="task")
    assert result["success"]
    e.intake._recent_msgs.clear()
    e.intake._pending.record_pending(G1, {"message_id": "pending1", "user_id": "user_A", "text": "盯着更新"}, "test")
    assert (await e.intake.handoff_from_planner(group_id=G1, msg_id="pending1", kind="goal"))["success"]
    rows = e.store.read().execute("SELECT message_id FROM requests ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [mid, "pending1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gid,platform,mid,kind", [("other", "", "x", "task"), (G1, "discord", "x", "task"), (G1, "", "", "task"), (G1, "", "unknown", "task"), (G1, "", "x", "invalid")])
async def test_rejects_invalid_handoff_without_writes(handoff_env, gid, platform, mid, kind):
    e = handoff_env
    before = e.store.read().total_changes
    result = await e.intake.handoff_from_planner(group_id=gid, platform=platform, msg_id=mid, kind=kind)
    assert result["success"] is False and result["error"] and result["content"]
    assert e.store.read().total_changes == before
    assert not e.spawned and not e.jev.calls and not e.mentions.items


@pytest.mark.asyncio
@pytest.mark.parametrize("fetched_gid,uid,success", [(G1, "user_A", True), ("other", "user_A", False), (G1, "bot", False), (G1, "", False)])
async def test_host_message_lookup_validates_group_and_speaker(handoff_env, fetched_gid, uid, success):
    e = handoff_env
    looked = []
    async def lookup(mid, session):
        looked.append((mid, session))
        return {"message_id": mid, "group_id": fetched_gid, "message_info": {"user_info": {"user_id": uid}}, "processed_plain_text": "准备资料"}
    e.intake._message_lookup = lookup
    result = await e.intake.handoff_from_planner(group_id=G1, session_id="session", msg_id="host1", kind="task")
    assert result["success"] is success
    assert looked == [("host1", "session")]
    assert e.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == int(success)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [True, False])
async def test_planner_chat_closes_pending_without_jev(handoff_env, finish):
    e = handoff_env
    await e.intake.handle(hook_message(group_id=G1, message_id="chat1", user_id="user_A", text="你好", is_at=True))
    assert not e.mentions.ephemeral
    e.intake.note_planner_request(G1, "session", ["chat1"])
    e.intake.note_planner_response(G1, "session", calls("reply"))
    if finish:
        e.intake.note_planner_request(G1, "session", ["chat1"])
        e.intake.note_planner_response(G1, "session", calls("stop"))
    await _drain(e.spawned)
    row = e.intake._pending.get(G1, "chat1")
    assert row["state"] == "none" and row["decision"]["why"] == "planner 已按闲聊处理"
    assert not e.jev.calls
    assert not e.store.read().execute("SELECT id FROM requests").fetchall()


@pytest.mark.asyncio
async def test_memory_round_then_handoff(handoff_env):
    e = handoff_env
    await e.intake.handle(hook_message(group_id=G1, message_id="multi1", user_id="user_A", text="准备资料", is_at=True))
    e.intake.note_planner_request(G1, "session", ["multi1"])
    e.intake.note_planner_response(G1, "session", calls("query_memory"))
    assert e.intake._pending.get(G1, "multi1")["state"] == "pending"
    e.intake.note_planner_response(G1, "session", calls("maiwork_handoff"))
    assert (await e.intake.handoff_from_planner(group_id=G1, msg_id="multi1", kind="task"))["success"]
    await _drain(e.spawned)
    assert not e.jev.calls


@pytest.mark.asyncio
async def test_timeout_fallback_then_late_handoff_is_single_request(handoff_env):
    e = handoff_env
    await e.intake.handle(hook_message(group_id=G1, message_id="late1", user_id="user_A", text="准备资料", is_at=True))
    await _drain(e.spawned)
    assert len(e.jev.calls) == 1
    assert (await e.intake.handoff_from_planner(group_id=G1, msg_id="late1", kind="goal"))["success"]
    assert e.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reminder_returns_before_slow_parser_and_records_unmentioned_message(handoff_env):
    e = handoff_env
    parsed = []
    async def slow(gid, msg):
        assert e.intake._pending.get(gid, msg["message_id"]) is not None
        parsed.append(msg)
        await asyncio.sleep(.02)
    e.intake._on_reminder = slow
    await e.intake.handle(hook_message(group_id=G1, message_id="remind1", user_id="user_A", text="明天提醒我"))
    started = time.monotonic()
    result = await e.intake.handoff_from_planner(group_id=G1, msg_id="remind1", kind="reminder")
    assert result["success"] and time.monotonic() - started < .1
    assert not parsed
    assert e.intake._pending.get(G1, "remind1") is not None
    await e.intake.handoff_from_planner(group_id=G1, msg_id="remind1", kind="reminder")
    await _drain(e.spawned)
    assert len(parsed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,prefix", [("task", "T-"), ("goal", "G-")])
async def test_admin_auto_approval_receipt_has_real_work_id(handoff_env, kind, prefix):
    e = handoff_env
    await e.intake.handle(hook_message(group_id=G1, message_id="admin1", user_id="admin_B", text="准备资料"))
    result = await e.intake.handoff_from_planner(group_id=G1, msg_id="admin1", kind=kind)
    assert result["success"] and "R-" in result["content"] and prefix in result["content"]
    assert e.store.read().execute("SELECT status FROM requests").fetchone()[0] == "approved"


@pytest.mark.asyncio
async def test_app_never_attributes_coalesced_message_to_host_admin(handoff_env, tmp_path):
    from CharTyr_MaiWork.maiwork.app import MaiWorkApp
    e = handoff_env
    app = MaiWorkApp(None, {}, tmp_path)
    app._started, app._settings, app._intake = True, e.settings, e.intake
    app._session_to_served_group = {"s": G1}
    await e.intake.handle(hook_message(group_id=G1, message_id="user1", user_id="user_A", text="准备资料"))
    result = await app.on_planner_handoff({"session_id": "s", "msg_id": "user1", "kind": "task", "user_id": "admin_B"})
    assert result["success"]
    row = e.store.read().execute("SELECT requester_id,status FROM requests").fetchone()
    assert tuple(row) == ("user_A", "pending")


@pytest.mark.asyncio
async def test_toggle_immediately_restores_legacy_path(handoff_env):
    e = handoff_env
    e.intake._get_settings = lambda: SimpleNamespace(is_served=lambda gid: gid == G1, platform_of=lambda gid: "qq", quick_judge=SimpleNamespace(planner_tool=False))
    await e.intake._handle_at(e.intake._get_settings(), G1, "user_A", "成员", "legacy", "准备资料")
    assert len(e.mentions.ephemeral) == 1
    await _drain(e.spawned)
    assert len(e.jev.calls) == 1


@pytest.mark.asyncio
async def test_timeout_and_handoff_overlap_does_not_duplicate(handoff_env):
    e = handoff_env
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_jev(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"kind": ("prepare", .99, .99)}
    e.jev.ask = slow_jev
    await e.intake.handle(hook_message(group_id=G1, message_id="race1", user_id="user_A", text="准备资料", is_at=True))
    judge = next(c for c in e.spawned if c.cr_code.co_name == "_judge_at")
    e.spawned.remove(judge)
    worker = asyncio.create_task(judge)
    await asyncio.wait_for(entered.wait(), 1)
    result = await e.intake.handoff_from_planner(group_id=G1, msg_id="race1", kind="goal")
    release.set()
    await worker
    await _drain(e.spawned)
    assert result["success"]
    assert e.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reminder_decision_is_persisted_before_tool_returns(handoff_env):
    e = handoff_env
    e.intake._on_reminder = lambda *args: True
    await e.intake.handle(hook_message(group_id=G1, message_id="reminder2", user_id="user_A", text="明天提醒我"))
    result = await e.intake.handoff_from_planner(group_id=G1, msg_id="reminder2", kind="reminder")
    assert result["success"]
    assert e.intake._pending.get(G1, "reminder2")["decision"]["kind"] == "reminder"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_kind", ["reminder", "task"])
async def test_fallback_reminder_in_progress_then_handoff_only_parses_once(handoff_env, tool_kind):
    e = handoff_env
    e.jev.answers = {"kind": ("reminder", .99, .99)}
    entered, release = asyncio.Event(), asyncio.Event()
    parsed = []
    async def parser(gid, msg):
        parsed.append(msg)
        entered.set()
        await release.wait()
    e.intake._on_reminder = parser
    await e.intake.handle(hook_message(group_id=G1, message_id="remindrace", user_id="user_A", text="明天提醒我", is_at=True))
    judge = next(c for c in e.spawned if c.cr_code.co_name == "_judge_at")
    e.spawned.remove(judge)
    await judge
    reminder = next(c for c in e.spawned if c.cr_code.co_name == "_run_reminder")
    e.spawned.remove(reminder)
    running = asyncio.create_task(reminder)
    await asyncio.wait_for(entered.wait(), 1)
    assert (await e.intake.handoff_from_planner(group_id=G1, msg_id="remindrace", kind=tool_kind))["success"]
    release.set()
    await running
    await _drain(e.spawned)
    assert len(parsed) == 1
    assert e.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_planner_tracking_prunes_stale_entries_and_caps_memory(handoff_env, monkeypatch):
    from CharTyr_MaiWork.maiwork import clock
    e = handoff_env
    monkeypatch.setattr(clock, "now", lambda: 1.0)
    e.intake.note_planner_request(G1, "old", ["old"])
    monkeypatch.setattr(clock, "now", lambda: 122.0)
    for n in range(520):
        e.intake.note_planner_request(G1, str(n), [str(n)])
    assert (G1, "old") not in e.intake._at_waiters
    assert (G1, "old") not in e.intake._session_planner_mids
    assert len(e.intake._at_waiters) <= 512
    assert len(e.intake._session_planner_mids) <= 512


@pytest.mark.asyncio
async def test_late_handoff_after_autoapproved_fallback_includes_work_id(handoff_env):
    e = handoff_env
    await e.intake.handle(hook_message(group_id=G1, message_id="autolate", user_id="admin_B", text="准备资料", is_at=True))
    await _drain(e.spawned)
    result = await e.intake.handoff_from_planner(group_id=G1, msg_id="autolate", kind="task")
    assert result["success"] and "R-" in result["content"] and "T-" in result["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_kind,tool_kind", [("prepare", "reminder"), ("reminder", "task")])
async def test_different_late_kind_does_not_create_second_work(handoff_env, fallback_kind, tool_kind):
    e = handoff_env
    parsed = []
    e.intake._on_reminder = lambda gid, msg: parsed.append(msg)
    e.jev.answers = {"kind": (fallback_kind, .99, .99)}
    await e.intake.handle(hook_message(group_id=G1, message_id="crosskind", user_id="user_A", text="明天准备资料", is_at=True))
    judge = next(c for c in e.spawned if c.cr_code.co_name == "_judge_at")
    e.spawned.remove(judge)
    await judge
    await e.intake.handoff_from_planner(group_id=G1, msg_id="crosskind", kind=tool_kind)
    await _drain(e.spawned)
    assert e.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] + len(parsed) == 1


@pytest.mark.asyncio
async def test_handoff_sets_mention_keywords_and_respects_feature_disable_cutoff(handoff_env, monkeypatch):
    from CharTyr_MaiWork.maiwork import clock, group_features
    e = handoff_env
    e.intake._mentions = delivery.Mentions(e.store, lambda: e.settings)
    monkeypatch.setattr(clock, "now", lambda: 100.0)
    await e.intake.handle(hook_message(group_id=G1, message_id="kw1", user_id="user_A", text="整理季度复盘报告", ts=100.0))
    res = await e.intake.handoff_from_planner(group_id=G1, msg_id="kw1", kind="task")
    assert res["success"]
    kw = e.store.read().execute("SELECT keywords FROM mentions WHERE group_id=? AND key LIKE 'request:%'", (G1,)).fetchone()[0]
    assert "r-" in kw.lower() and "整理季度复盘报告" in kw
    await e.intake.handle(hook_message(group_id=G1, message_id="stale1", user_id="user_A", text="再整理一份名单", ts=100.0))
    group_features.set_config(e.store, G1, {"task": False}, e.settings, now=110.0)
    group_features.set_config(e.store, G1, {"task": True}, e.settings, now=120.0)
    monkeypatch.setattr(clock, "now", lambda: 125.0)
    stale = await e.intake.handoff_from_planner(group_id=G1, msg_id="stale1", kind="task")
    assert stale["success"] is False
    assert e.store.read().execute("SELECT COUNT(*) FROM requests WHERE message_id='stale1'").fetchone()[0] == 0
