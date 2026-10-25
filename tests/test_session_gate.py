"""会话原生入口：身份闸、持久化和失败回落的契约。"""
from __future__ import annotations
import asyncio
import sqlite3
import time
import pytest
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.sessions import Sessions
from CharTyr_MaiWork.maiwork.session_gate import SessionGate, build_pattern, matches
from CharTyr_MaiWork.maiwork.store import Store
GID = "900000001"
BOT = "100000002"
OWNER = "10001"

def message(text="@東雪蓮 往下做", *, uid=OWNER, mid="m1", at=True):
    return dict(group_id=GID, platform="qq", user_id=uid, text=text, message={
        "message_id": mid, "platform": "qq", "processed_plain_text": text,
        "message_info": {"group_info": {"group_id": GID}, "user_info": {"user_id": uid}},
        "raw_message": [{"type": "at", "data": {"target_user_id": BOT,
            "target_user_cardname": "東雪蓮"}}] if at else [], "is_at": True})

@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "gate.db")
    store.migrate()
    settings, _ = load_settings({"plugin": {"enabled": True},
        "groups": {"serve": [{"group": "qq:" + GID}]},
        "session": {"bot_names": ["東雪蓮"]}})
    sessions = Sessions(store)
    with store.tx() as conn:
        row = sessions.open_for_request(conn, dict(id="R-1", group_id=GID,
            platform="qq", requester_id=OWNER, kind="task", source=""))
        conn.execute("UPDATE task_sessions SET state='working' WHERE id=?", (row["id"],))
    calls = []
    async def triage(state, *, admin, timeout_ms):
        calls.append((state, admin, timeout_ms))
        return {"label": "for_maiwork", "confidence": .95}
    gate = SessionGate(store, sessions, lambda: settings, bot_ids=lambda p: {BOT},
        is_admin=lambda uid, p, gid: uid == "admin", triage=triage)
    gate.registered_pattern = build_pattern(["東雪蓮"], [BOT])
    gate.refresh()
    yield store, sessions, row, gate, calls
    store.close()

def test_session_config_bounds_and_bad_section():
    settings, problems = load_settings({"session": {"triage_timeout_ms": 99999,
        "bot_names": [" 東雪蓮 ", "東雪蓮", "🪷🪷", "x" * 500], "max_running_per_group": 9}})
    assert settings.session.triage_timeout_ms == 1500
    assert settings.session.max_running_per_group == 1
    assert settings.session.bot_names == ("東雪蓮", "🪷🪷")
    assert problems
    bad, problems = load_settings({"session": []})
    assert bad.session.enabled and bad.session.relay_text
    assert bad.session.idle_close_minutes == 60 and problems
    low, _ = load_settings({"session": {"triage_timeout_ms": 1}})
    assert low.session.triage_timeout_ms == 300

@pytest.mark.parametrize("text", ["@🪷🪷 往下滚", "[reply]<被引用正文> @東雪蓮  做到哪了", "@名", "@名   ", "@a+b 继续", "@100000002 继续"])
def test_narrow_pattern_accepts_real_display_forms(text):
    assert matches(build_pattern(["🪷🪷", "東雪蓮", "名", "a+b"], [BOT]), text)

@pytest.mark.parametrize("text", ["@其他人 继续", "/mw 状态", "/pic @名", "@aab 继续", "@名字 继续", "文字 @名 继续", "@名 /music 起风了"])
def test_narrow_pattern_does_not_broaden(text):
    assert not matches(build_pattern(["名", "a+b"], []), text)
    assert not matches(build_pattern([], []), text)

@pytest.mark.asyncio
async def test_instruction_commit_and_restart_dedupe(setup):
    store, sessions, row, gate, calls = setup
    assert await gate.handle(message()) == (True, None, True)
    event = dict(store.read().execute("SELECT * FROM session_events").fetchone())
    assert event["kind"] == "instruction" and event["text"] == "往下做"
    assert len(calls) == 1
    gate._decisions.clear()
    assert await gate.handle(message()) == (True, None, True)
    assert len(calls) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"group_id": "unknown"}, {"platform": "telegram"}, {"user_id": "stranger"}])
async def test_early_rejection_is_zero_database_and_model(setup, monkeypatch, change):
    store, _, _, gate, calls = setup
    monkeypatch.setattr(store, "read", lambda: pytest.fail("身份闸前读了数据库"))
    monkeypatch.setattr(gate, "_connect", lambda: pytest.fail("身份闸前打开了数据库"))
    kwargs = message()
    kwargs.update(change)
    assert await gate.handle(kwargs) == (True, None, False)
    assert not calls

@pytest.mark.asyncio
async def test_textual_at_is_not_authorization(setup):
    _, _, _, gate, calls = setup
    assert await gate.handle(message(at=False)) == (True, None, False)
    assert not calls

@pytest.mark.asyncio
@pytest.mark.parametrize("state,label,expected", [("awaiting_approval", "for_maiwork", "supplement"),
    ("asking", "for_maiwork", "clarify_a"), ("working", "unsure", "clarify_q")])
async def test_state_routes(setup, state, label, expected):
    store, _, row, gate, _ = setup
    store.read().execute("UPDATE task_sessions SET state=? WHERE id=?", (state, row["id"]))
    gate.refresh()
    async def triage(*a, **k): return {"label": label, "confidence": .95}
    gate._triage = triage
    assert (await gate.handle(message()))[2]
    assert store.read().execute("SELECT kind FROM session_events").fetchone()[0] == expected
    if expected == "clarify_q":
        assert store.read().execute("SELECT state FROM task_sessions").fetchone()[0] == "asking"
        payload = store.read().execute("SELECT payload FROM outbox").fetchone()[0]
        assert "@我回答就行" in payload and 'command' in payload

@pytest.mark.asyncio
async def test_adoption_requires_explicit_body_and_quote(setup):
    store, _, _, gate, _ = setup
    kwargs = message("[reply]别人说的 @東雪蓮 按这条做")
    kwargs["message"]["raw_message"].insert(0, {"type": "reply", "data": {
        "target_message_id": "quote1", "target_message_content": "别人说的"}})
    assert (await gate.handle(kwargs))[2]
    event = store.read().execute("SELECT * FROM session_events").fetchone()
    assert event["kind"] == "adopt" and event["quoted_ref"] == "quote1"
    assert "别人说的" not in event["text"]

@pytest.mark.asyncio
async def test_admin_can_only_stop(setup):
    store, _, _, gate, calls = setup
    assert not (await gate.handle(message(uid="admin")))[2]
    assert calls[0][1] is True
    async def stop(*a, **k): return {"stop": True, "confidence": .95}
    gate._triage = stop
    assert (await gate.handle(message(uid="admin", mid="m2")))[2]
    assert store.read().execute("SELECT kind FROM session_events").fetchone()[0] == "admin_stop"

@pytest.mark.asyncio
async def test_chat_durable_dedupe_and_timeout_continue(setup):
    _, _, _, gate, calls = setup
    async def chat(*a, **k):
        calls.append(1)
        return {"label": "chat", "confidence": .99}
    gate._triage = chat
    assert not (await gate.handle(message()))[2]
    gate._decisions.clear()
    assert not (await gate.handle(message()))[2]
    assert len(calls) == 1
    async def slow(*a, **k): await asyncio.sleep(10)
    gate._triage = slow
    started = time.monotonic()
    assert not (await gate.handle(message(mid="m2")))[2]
    assert time.monotonic() - started < 1.6

@pytest.mark.asyncio
async def test_write_lock_continue_without_late_commit(setup):
    store, _, _, gate, _ = setup
    other = sqlite3.connect(store.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert not (await gate.handle(message()))[2]
        assert time.monotonic() - started < .3
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert store.read().execute("SELECT count(*) FROM session_events").fetchone()[0] == 0

@pytest.mark.asyncio
async def test_component_alignment_retries_failure(setup):
    _, _, _, gate, _ = setup
    calls = []
    class Host:
        async def set_session_command_enabled(self, enabled):
            calls.append(enabled)
            if len(calls) == 1: raise RuntimeError("failed")
    gate.host = Host()
    await gate.align()
    assert not gate.command_enabled
    await gate.align()
    assert gate.command_enabled and calls == [True, True]

def test_name_learning_is_authentic_bounded_and_pattern_stays_fixed(setup):
    store, _, _, gate, _ = setup
    original = gate.registered_pattern
    kwargs = message()
    kwargs["message"]["raw_message"][0]["data"]["target_user_cardname"] = "🪷🪷"
    assert gate.learn_names(kwargs)
    assert not gate.learn_names(message(at=False))
    assert gate.registered_pattern == original
    assert "🪷🪷" in store.kv_get("session.bot_names", [])


@pytest.mark.asyncio
async def test_intake_yields_before_legacy_answer_and_at(setup, monkeypatch):
    from CharTyr_MaiWork.maiwork.intake import Intake
    from CharTyr_MaiWork.maiwork.intake import Signals
    _, _, _, gate, _ = setup
    seen = []
    class Gate:
        def intake(self, kwargs, spawn):
            seen.append(kwargs)
            return True
    intake = Intake(gate._settings, Signals(), session_gate=Gate(), bot_qq=BOT)
    monkeypatch.setattr(intake, "_maybe_answer", lambda *a: pytest.fail("旧回答路径重复处理会话消息"))
    async def old(*a): pytest.fail("旧派活路径重复处理会话消息")
    monkeypatch.setattr(intake, "_handle_at", old)
    await intake._m3(gate._settings(), GID, message()["message"])
    assert seen[0]["user_id"] == OWNER


@pytest.mark.parametrize("command_on", [True, False])
def test_admin_at_keeps_legacy_request_path(setup, command_on):
    """管理员（非主人）的 @ 只多一道「是不是叫停」检查，不能让旧派活入口让路：
    否则会话开着时管理员正常派的新活（如「帮我查一下X」）会被吞掉（D18 只授予叫停）。"""
    _, _, _, gate, _ = setup
    gate.command_enabled = command_on
    spawned = []
    def spawn(coro):
        spawned.append(coro)
        coro.close()
    assert gate.intake(message("@東雪蓮 帮我查一下明天的天气", uid="admin"), spawn) is False
    # 命令没接住（降级）时，后台仍跑一次叫停判断；命令接得住时交给命令，不重复判
    assert len(spawned) == (0 if command_on else 1)
    # 主人照旧让路
    assert gate.intake(message(), spawn) is True


@pytest.mark.asyncio
async def test_low_confidence_work_asks_instead_of_silently_dropping(setup):
    store, _, _, gate, _ = setup
    async def unsure(*args, **kwargs):
        return {"label": "for_maiwork", "confidence": .4}
    gate._triage = unsure
    assert await gate.handle(message()) == (True, None, True)
    assert store.read().execute("SELECT kind FROM session_events").fetchone()[0] == "clarify_q"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["按他说的做", "用他发的链接"])
async def test_owner_explicitly_adopts_quoted_material(setup, body):
    store, _, _, gate, _ = setup
    kwargs = message("[reply]他说的内容 @東雪蓮 " + body)
    kwargs["message"]["raw_message"].insert(0, {"type": "reply", "data": {
        "target_message_id": "quoted", "target_message_content": "材料", "target_message_sender_id": "other"}})
    assert await gate.handle(kwargs) == (True, None, True)
    event = store.read().execute("SELECT kind,quoted_ref FROM session_events").fetchone()
    assert tuple(event) == ("adopt", "quoted")


def test_registration_uses_sdk_metadata_and_fixed_escaped_candidates(tmp_path):
    import re
    from CharTyr_MaiWork.plugin import create_plugin
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    with store.tx() as conn:
        store.kv_set(conn, "session.bot_names", ["🪷🪷", "a+b"])
    store.close()
    plugin = create_plugin()
    plugin._data_dir_override = str(tmp_path)
    plugin._plugin_config_data = {"session": {"bot_names": ["東雪蓮"]}}
    component = next(c for c in plugin.get_components() if c["name"] == "maiwork_session")
    metadata = component["metadata"]
    assert metadata["timeout_ms"] == 5000 and metadata["intercept_message_level"] == 0
    pattern = metadata["command_pattern"]
    for text in ["@東雪蓮 继续", "@🪷🪷", "@a+b   ", "[reply]引用 @東雪蓮 继续"]:
        assert re.search(pattern, text)
    for text in ["@别人 继续", "@aab 继续", "@東雪蓮 /pic 图"]:
        assert not re.search(pattern, text)
    assert plugin._registered_session_pattern == pattern


@pytest.mark.asyncio
async def test_bot_account_set_keeps_same_platform_accounts():
    from CharTyr_MaiWork.maiwork.host import Host
    class Ctx:
        async def call_capability(self, capability, **kwargs):
            return ["tg:111", "telegram:222", "qq:333"]
    host = Host(Ctx(), bot_qq=BOT)
    await host._bot_accounts()
    assert host.cached_bot_accounts("telegram") == {"111", "222"}
    assert host.cached_bot_accounts("qq") == {BOT, "333"}


@pytest.mark.asyncio
async def test_triage_state_contains_session_facts_without_unrelated_quote(setup):
    _, _, _, gate, calls = setup
    kwargs = message()
    kwargs["message"]["raw_message"].insert(0, {"type": "reply", "data": {
        "target_message_id": "unrelated", "target_message_content": "旁人聊天"}})
    await gate.handle(kwargs)
    state = calls[0][0]
    assert state["state"] == "working"
    assert state["waiting_answer"] is False
    assert state["quote"] == ""


@pytest.mark.asyncio
async def test_jev_contract_instructions_and_low_confidence(setup):
    _, _, _, gate, _ = setup
    gate._triage = None
    class Jev:
        def available(self): return True
        async def ask(self, state, questions, **kwargs):
            assert questions["route"]["instructions"]
            return {"route": ("for_maiwork", .95, .4)}
    gate.jev = Jev()
    assert await gate.handle(message()) == (True, None, True)
    event = gate.store.read().execute("SELECT kind FROM session_events").fetchone()
    assert event[0] == "clarify_q"


@pytest.mark.asyncio
async def test_jev_unavailable_uses_quick_judge_model_candidates(setup):
    from types import SimpleNamespace
    from dataclasses import replace
    _, _, _, gate, _ = setup
    settings = gate._settings()
    gate._settings = lambda: replace(settings, quick_judge=replace(settings.quick_judge, model="quick-model"))
    gate._triage = None
    seen = []
    class Jev:
        def available(self): return False
    class Models:
        def candidates_for_entry(self, slot):
            assert slot == "quick-model"
            return ["candidate"]
        async def chat(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(text='{"label":"for_maiwork","confidence":0.95}')
    gate.jev, gate.models = Jev(), Models()
    assert await gate.handle(message()) == (True, None, True)
    assert seen[0]["_candidates"] == ["candidate"]
    assert seen[0]["purpose"] == "session.triage"


@pytest.mark.asyncio
async def test_cancellation_never_commits_later(setup):
    store, _, _, gate, _ = setup
    started = asyncio.Event()
    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    gate._triage = slow
    task = asyncio.create_task(gate.handle(message()))
    await started.wait()
    task.cancel()
    assert await task == (True, None, False)
    assert store.read().execute("SELECT count(*) FROM session_events").fetchone()[0] == 0
    assert not gate._inflight


@pytest.mark.asyncio
async def test_revision_fence_rejects_judgment_for_changed_session(setup):
    store, _, row, gate, _ = setup
    async def triage(*args, **kwargs):
        store.read().execute("UPDATE task_sessions SET revision=revision+1 WHERE id=?", (row["id"],))
        return {"label": "for_maiwork", "confidence": .95}
    gate._triage = triage
    assert await gate.handle(message()) == (True, None, False)
    assert store.read().execute("SELECT count(*) FROM session_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_session_change_notification_observes_committed_state(setup):
    store, sessions, row, _, _ = setup
    observed = []
    sessions.on_change = lambda: observed.append(sessions.get_open(GID, platform="qq"))
    with store.tx() as conn:
        sessions.close(conn, row["id"], "test")
        assert not observed
    await asyncio.sleep(0)
    assert observed == [None]

