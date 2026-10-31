"""会话第一阶段：迁移、纯数据规则、批准事务及旧回答边界。"""
from __future__ import annotations

import importlib
import sqlite3
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store, _MIGRATIONS
from CharTyr_MaiWork.maiwork.tasks import Tasks

GID = "900000001"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "sessions.db")
    value.migrate()
    yield value
    value.close()


def sessions(store):
    return importlib.import_module("CharTyr_MaiWork.maiwork.sessions").Sessions(store)


def request():
    return dict(id="R-1", group_id=GID, platform="qq", requester_id="10001", kind="task", source="")


def test_schema_44_upgrade_and_idempotence(tmp_path):
    value = Store(tmp_path / "old.db")
    try:
        for step in _MIGRATIONS[:43]:
            step(value.read())
        value.read().execute("PRAGMA user_version=43")
        Tasks(value, lambda: None).create(GID, title="旧任务", req="", criteria=[], source="admin")
        assert value.migrate() == 49
        _MIGRATIONS[-1](value.read())
        assert value.migrate() == 49
        old = value.read().execute("SELECT * FROM tasks WHERE id='T-1'").fetchone()
        assert old["session_id"] is None and old["turn_no"] is None
        assert value.read().execute("SELECT count(*) FROM task_sessions").fetchone()[0] == 0
        assert {r["name"] for r in value.read().execute("PRAGMA table_info(session_events)")} >= {
            "source_key", "seq", "revision", "received_at", "applied_at", "note"}
    finally:
        value.close()


def test_open_owner_platform_and_one_open_per_group(store):
    svc = sessions(store)
    with store.tx() as conn:
        opened = svc.open_for_request(conn, request())
        assert svc.open_for_request(conn, request()) is None
        other = request()
        other.update(id="R-2", platform="telegram")
        second = svc.open_for_request(conn, other)
    assert opened["state"] == "awaiting_approval"
    assert svc.get_open_for_owner("qq", GID, "10001")["id"] == opened["id"]
    assert svc.get_open_for_owner("qq", GID, "999") is None
    assert svc.get_open_for_owner("discord", GID, "10001") is None
    assert second["id"] != opened["id"]
    assert svc.open_groups() == {("qq", GID), ("telegram", GID)}
    with pytest.raises(sqlite3.IntegrityError), store.tx() as conn:
        conn.execute("INSERT INTO task_sessions(id,platform,group_id,owner_id,request_id,state,opened_at,last_active_at) VALUES('S-bad','qq',?,'2','R-3','idle',1,1)", (GID,))


@pytest.mark.parametrize("changes", [{"kind": "goal"}, {"source": "maiwork"}, {"source": "admin"}, {"requester_id": ""}])
def test_ineligible_requests_do_not_open(store, changes):
    row = request()
    row.update(changes)
    with store.tx() as conn:
        assert sessions(store).open_for_request(conn, row) is None


def test_request_rejected_closes_and_releases_reference(store):
    svc = sessions(store)
    with store.tx() as conn:
        opened = svc.open_for_request(conn, request())
        conn.execute("UPDATE task_sessions SET env_lease='box-ref' WHERE id=?", (opened["id"],))
        svc.on_request_rejected(conn, "R-1")
        svc.on_request_rejected(conn, "R-1")
    row = store.read().execute("SELECT * FROM task_sessions").fetchone()
    assert row["state"] == "closed" and row["closed_reason"] == "rejected"
    assert row["closed_at"] is not None and row["env_lease"] is None
    assert svc.get_open(GID, platform="qq") is None


def test_task_landing_turns_and_idempotence(store):
    svc = sessions(store)
    tasks = Tasks(store, lambda: None)
    with store.tx() as conn:
        opened = svc.open_for_request(conn, request())
        first = tasks.create(GID, req="", criteria=[], source="admin", title="第一件", conn=conn)
        second = tasks.create(GID, req="", criteria=[], source="admin", title="第二件", conn=conn)
        svc.on_task_landed(conn, "R-1", first)
        svc.on_task_landed(conn, "R-1", first)
        svc.on_task_landed(conn, "R-1", second)
    rows = store.read().execute("SELECT session_id,turn_no FROM tasks ORDER BY turn_no").fetchall()
    assert [(r["session_id"], r["turn_no"]) for r in rows] == [(opened["id"], 1), (opened["id"], 2)]
    row = svc.get_open(GID, platform="qq")
    assert row["state"] == "working" and row["current_task_id"] == first


def test_events_dedupe_sequence_and_activity(store, monkeypatch):
    svc = sessions(store)
    monkeypatch.setattr(clock, "now", lambda: 100.0)
    with store.tx() as conn:
        opened = svc.open_for_request(conn, request())
        monkeypatch.setattr(clock, "now", lambda: 200.0)
        one = svc.record_event(conn, opened["id"], source_key="qq:g:m1", kind="instruction", text="做这个", user_id="10001")
        duplicate = svc.record_event(conn, opened["id"], source_key="qq:g:m1", kind="instruction", text="重投", user_id="10001")
        monkeypatch.setattr(clock, "now", lambda: 300.0)
        two = svc.record_event(conn, opened["id"], source_key="qq:g:m2", kind="clarify_q", text="问题", user_id="10001")
        svc.record_event(conn, opened["id"], source_key="qq:g:m3", kind="instruction", text="别人", user_id="20002")
        svc.record_event(conn, opened["id"], source_key="qq:g:m4", kind="instruction", text="拒绝", user_id="10001", status="rejected")
    assert one["id"] == duplicate["id"] and one["seq"] == 1 and two["seq"] == 2
    assert svc.get_open(GID, platform="qq")["last_active_at"] == 200.0
    assert store.read().execute("SELECT count(*) FROM session_events").fetchone()[0] == 4


@pytest.mark.parametrize("state", ["awaiting_approval", "working", "asking", "closed", "idle", "paused"])
def test_idle_deadline_states(store, state):
    svc = sessions(store)
    row = dict(state=state, last_active_at=150.0, last_delivery_at=200.0)
    expected = 3800.0 if state in {"idle", "paused"} else None
    assert svc.idle_deadline(row, idle_minutes=60) == expected
    assert svc.is_idle_expired(row, now=3800.0, idle_minutes=60) == (expected is not None)
    assert not svc.is_idle_expired(row, now=3799.0, idle_minutes=60)


def test_event_prune_only_long_closed_sessions(store, monkeypatch):
    svc = sessions(store)
    monkeypatch.setattr(clock, "now", lambda: 10.0)
    with store.tx() as conn:
        old = svc.open_for_request(conn, request())
        svc.record_event(conn, old["id"], source_key="old", kind="instruction", text="旧", user_id="10001")
        svc.close(conn, old["id"], "ended")
        fresh = request()
        fresh["id"] = "R-2"
        current = svc.open_for_request(conn, fresh)
        svc.record_event(conn, current["id"], source_key="open", kind="instruction", text="还开着", user_id="10001")
    assert store.prune(31 * 86400)["session_events"] == 1
    assert store.read().execute("SELECT source_key FROM session_events").fetchone()[0] == "open"


@pytest.mark.parametrize("auto", [False, True])
def test_approvals_open_land_reject_same_transaction(store, auto):
    settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}, "approval": {"required": not auto}})
    svc = sessions(store)
    ap = Approvals(store, lambda: settings, Tasks(store, lambda: settings), Goals(store, lambda: settings), sessions=svc)
    out = ap.create(GID, kind="task", title="测试", quote="请求", via="@", requester_id="10001", requester_name="群友")
    opened = svc.get_open(GID, platform="qq")
    assert opened["request_id"] == out["id"]
    if auto:
        task = store.read().execute("SELECT session_id,turn_no FROM tasks WHERE id=?", (out["task_id"],)).fetchone()
        assert task["session_id"] == opened["id"] and task["turn_no"] == 1
        assert opened["state"] == "working"
    else:
        assert opened["state"] == "awaiting_approval"
        ap.reject(out["id"], by="管理员")
        assert svc.get_open(GID, platform="qq") is None


def test_approval_session_failure_rolls_back_request(store):
    settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
    def fail(conn, row):
        conn.execute("INSERT INTO kv(key,value,updated) VALUES('session-test','1',0)")
        raise RuntimeError("会话写入失败")
    ap = Approvals(store, lambda: settings, Tasks(store, lambda: settings), Goals(store, lambda: settings), sessions=SimpleNamespace(open_for_request=fail))
    with pytest.raises(RuntimeError, match="会话写入失败"):
        ap.create(GID, kind="task", title="测试", quote="请求", via="@", requester_id="10001", requester_name="群友")
    assert store.read().execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert store.read().execute("SELECT count(*) FROM kv WHERE key='session-test'").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_session_hook_failure_rolls_back_approval_decision(store, monkeypatch, action):
    settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}, "approval": {"required": True}})
    svc = sessions(store)
    ap = Approvals(store, lambda: settings, Tasks(store, lambda: settings), Goals(store, lambda: settings), sessions=svc)
    out = ap.create(GID, kind="task", title="事务测试", quote="请求", via="@", requester_id="10001", requester_name="群友")
    opened = svc.get_open(GID, platform="qq")

    def fail(conn, *args):
        assert conn.in_transaction
        conn.execute("UPDATE task_sessions SET state='paused' WHERE id=?", (opened["id"],))
        raise RuntimeError("会话决定失败")

    monkeypatch.setattr(svc, "on_task_landed" if action == "approve" else "on_request_rejected", fail)
    with pytest.raises(RuntimeError, match="会话决定失败"):
        getattr(ap, action)(out["id"], by="管理员")
    assert store.read().execute("SELECT status FROM requests WHERE id=?", (out["id"],)).fetchone()[0] == "pending"
    assert svc.get_open(GID, platform="qq")["state"] == "awaiting_approval"
    assert store.read().execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert store.read().execute("SELECT count(*) FROM events WHERE kind IN ('request.approved','request.rejected')").fetchone()[0] == 0


def test_waiting_answer_map_excludes_session_tasks(store):
    tasks = Tasks(store, lambda: None)
    old = tasks.create(GID, req="", criteria=[], source="admin", title="旧任务")
    new = tasks.create(GID, req="", criteria=[], source="admin", title="会话任务")
    # 先断言行为，旧库没有列时也能重现旧查询泄漏。
    cols = {r["name"] for r in store.read().execute("PRAGMA table_info(tasks)")}
    if "session_id" not in cols:
        store.read().execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
    with store.tx() as conn:
        conn.execute("UPDATE tasks SET status='waiting_input',question_msg_id='q'")
        conn.execute("UPDATE tasks SET session_id='S-1' WHERE id=?", (new,))
    app = object.__new__(MaiWorkApp)
    app.store = store
    app._answer_cache_ts = 0.0
    app._answer_cache = {}
    app._task_prompt_message_ids = lambda ids: {}
    assert [r[0] for r in app._waiting_answer_map(GID)] == [old]


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("platform", ["qq", "qqbot"])
def test_quote_answer_requires_requester_or_group_bot_admin(admin, platform):
    gid = "official-group" if platform == "qqbot" else GID
    settings, problems = load_settings({"groups": {"serve": [{"group": f"{platform}:{gid}"}]}})
    assert settings.platform_of(gid) == platform
    assert not problems

    calls = []
    class Admins:
        def is_admin(self, user_id, platform="qq", *, group_id=None):
            calls.append((user_id, platform, group_id))
            return admin
    spawned = []
    intake = Intake(lambda: settings, Signals(), approvals=Admins(), waiting_tasks=lambda gid: [("T-1", "q", "owner")], on_answer=lambda *args: None, spawn=spawned.append)
    intake._maybe_answer(gid, "outsider", "m", "答案", {"raw_message": "[CQ:reply,id=q]答案", "platform": "qq"})
    count = len(spawned)
    for coroutine in spawned:
        coroutine.close()
    assert count == int(admin)
    assert calls == [("outsider", platform, gid)]
