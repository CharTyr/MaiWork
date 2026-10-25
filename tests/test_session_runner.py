"""会话控制器：真实库 + 隔离执行器，验证收件、应用与调度边界。"""
from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.sessions import Sessions
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

GID = "900000001"
OWNER = "10001"


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "runner.db")
    store.migrate()
    settings, _ = load_settings({"plugin": {"enabled": True},
        "groups": {"serve": [{"group": "qq:" + GID}]},
        "approval": {"required": True}, "session": {"bot_names": ["東雪蓮"]}})
    get_settings = lambda: settings
    tasks = Tasks(store, get_settings)
    sessions = Sessions(store)
    approvals = Approvals(store, get_settings, tasks, Goals(store, get_settings), sessions=sessions)
    host = SimpleNamespace(message_by_id=AsyncMock(return_value=None),
        session_for_group=AsyncMock(return_value="chat1"))
    outbox = Outbox(store, host, Pushes(store, get_settings), Mentions(store, get_settings), get_settings)
    started, cancelled, judged = [], [], []
    async def classify(state):
        judged.append(state)
        return {"action": "continue"}
    def make():
        cls = importlib.import_module("CharTyr_MaiWork.maiwork.session_runner").SessionRunner
        return cls(store, sessions, tasks, approvals, outbox, get_settings,
            start_task=started.append, cancel_task=cancelled.append, classify=classify,
            host=host, mentions=Mentions(store, get_settings))
    r = SimpleNamespace(store=store, tasks=tasks, sessions=sessions, approvals=approvals,
        host=host, outbox=outbox, started=started, cancelled=cancelled, judged=judged,
        make=make, settings=settings)
    yield r
    store.close()


def open_idle(r):
    req = r.approvals.create(GID, kind="task", title="网页截图", quote="打开网页截图",
        via="at", requester_id=OWNER, requester_name="小明", message_id="original")
    sid = r.sessions.get_open(GID, platform="qq")["id"]
    with r.store.tx() as conn:
        conn.execute("UPDATE requests SET status='approved' WHERE id=?", (req["id"],))
        conn.execute("UPDATE task_sessions SET state='idle' WHERE id=?", (sid,))
    return sid


def event(r, sid, text="往下滚", kind="instruction", mid="m1", quote=""):
    with r.store.tx() as conn:
        return r.sessions.record_event(conn, sid, source_key=f"qq:{GID}:{mid}",
            text=text, kind=kind, user_id=OWNER, quoted_ref=quote)


def session(r, sid):
    return dict(r.store.read().execute("SELECT * FROM task_sessions WHERE id=?", (sid,)).fetchone())


def events(r):
    return [dict(x) for x in r.store.read().execute("SELECT * FROM session_events ORDER BY seq")]


def payloads(r):
    return [json.loads(x[0]) for x in r.store.read().execute("SELECT payload FROM outbox ORDER BY id")]


def linked_task(r, sid, *, title="上一轮", status="completed"):
    tid = r.tasks.create(GID, title=title, req="需求", criteria=[], source="request",
        requester_id=OWNER, requester_name="小明")
    with r.store.tx() as conn:
        turn = conn.execute("SELECT COALESCE(MAX(turn_no),0)+1 FROM tasks WHERE session_id=?", (sid,)).fetchone()[0]
        conn.execute("UPDATE tasks SET session_id=?,turn_no=?,status=?,review='已经截图',delivery='[\"artifacts/旧图.png\"]' WHERE id=?", (sid, turn, status, tid))
        conn.execute("UPDATE task_sessions SET current_task_id=? WHERE id=?", (tid, sid))
    return tid


@pytest.mark.asyncio
async def test_ordered_idempotent_continuation_and_restart(rig):
    r = rig
    sid = open_idle(r)
    previous = linked_task(r, sid)
    event(r, sid)
    event(r, sid, "做到哪了", kind="progress_query", mid="m2")
    runner = r.make()
    await asyncio.gather(runner.tick(), runner.tick())
    await r.make().tick()
    rows = list(r.store.read().execute("SELECT * FROM tasks ORDER BY turn_no"))
    assert len(rows) == 2 and rows[0]["id"] == previous
    assert rows[0]["status"] == "completed"
    assert rows[1]["session_id"] == sid and rows[1]["turn_no"] == 2
    assert all(x in rows[1]["req"] for x in ["往下滚", "上一轮", "已经截图", "artifacts/旧图.png"])
    assert r.store.read().execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert [x["status"] for x in events(r)] == ["applied", "applied"]
    assert all(x["applied_at"] is not None and x["note"] for x in events(r))
    assert [x["text"] for x in r.judged] == ["往下滚"]
    assert "往下滚" in payloads(r)[0]["text"] and payloads(r)[0]["push_kind"] == "command"


@pytest.mark.asyncio
async def test_new_work_approval_lands_next_turn_same_session(rig):
    r = rig
    sid = open_idle(r)
    linked_task(r, sid)
    event(r, sid, "再帮我写周报")
    runner = r.make()
    runner._classify = AsyncMock(return_value={"action": "new_work"})
    await runner.tick()
    pending = r.approvals.pending_view(GID)
    assert len(pending) == 1
    assert session(r, sid)["state"] == "awaiting_approval"
    r.approvals.approve(pending[0]["id"], by="管理员")
    await runner.tick()
    rows = list(r.store.read().execute("SELECT * FROM tasks ORDER BY turn_no"))
    assert len(rows) == 2 and rows[1]["session_id"] == sid and rows[1]["turn_no"] == 2
    assert r.sessions.get_open(GID, platform="qq")["id"] == sid


@pytest.mark.asyncio
async def test_uncertain_asks_once_and_answer_keeps_original_instruction(rig):
    r = rig
    sid = open_idle(r)
    event(r, sid, "用那个")
    runner = r.make()
    runner._classify = AsyncMock(return_value={"action": "unsure"})
    await runner.tick()
    await r.make().tick()
    assert session(r, sid)["state"] == "asking"
    assert len(payloads(r)) == 1 and payloads(r)[0]["text"].endswith("@我回答就行")
    assert payloads(r)[0]["push_kind"] == "command"
    assert not r.started
    event(r, sid, "继续上一件，用第二个链接", kind="clarify_a", mid="answer")
    await r.make().tick()
    task = r.tasks.get(r.started[0])
    assert "用那个" in task["req"] and "第二个链接" in task["req"]


@pytest.mark.asyncio
async def test_running_change_revises_current_task_once(rig):
    """阶段 4：活在跑时改要求 → 当前任务换新需求版本、排回队列重来，旧结果自然作废；重放不重复。"""
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    event(r, sid, "改成英文")
    event(r, sid, "现在进度", kind="progress_query", mid="m2")
    runner = r.make()
    await runner.tick()
    task = r.tasks.get(tid)
    assert task["req_version"] == 2 and task["status"] == "queued" and "改成英文" in task["req"]
    assert r.store.read().execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    assert [e["status"] for e in events(r)] == ["applied", "applied"]
    assert tid in r.started
    assert payloads(r)[0]["push_kind"] == "command" and payloads(r)[0]["at_user"] == OWNER
    # 崩溃重放：同一事件再应用一遍，需求不再追加
    with r.store.tx() as conn:
        conn.execute("UPDATE session_events SET status='received' WHERE seq=1 AND session_id=?", (sid,))
    await r.make().tick()
    assert r.tasks.get(tid)["req_version"] == 2 and r.tasks.get(tid)["req"].count("改成英文") == 1


@pytest.mark.asyncio
async def test_running_pause_then_resume(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    event(r, sid, "暂停")
    runner = r.make()
    await runner.tick()
    assert r.tasks.get(tid)["status"] == "paused" and session(r, sid)["state"] == "paused"
    assert events(r)[0]["status"] == "applied" and not r.started
    await r.make().tick()  # 重放 / 再巡检不出错、不重复回话
    assert len(payloads(r)) == 1
    event(r, sid, "继续", mid="m2")
    await runner.tick()
    assert r.tasks.get(tid)["status"] == "queued" and r.started == [tid]
    assert session(r, sid)["state"] == "working"


@pytest.mark.asyncio
async def test_change_while_paused_updates_requirement_but_stays_paused(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="paused")
    with r.store.tx() as conn:
        conn.execute("UPDATE task_sessions SET state='paused' WHERE id=?", (sid,))
    event(r, sid, "改成英文")
    await r.make().tick()
    task = r.tasks.get(tid)
    assert task["status"] == "paused" and task["req_version"] == 2 and "改成英文" in task["req"]
    assert not r.started


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["结束吧", "结束"])
async def test_end_while_waiting_answer_is_not_taken_as_answer(rig, text):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="waiting_input")
    event(r, sid, text, kind="clarify_a")
    await r.make().tick()
    assert r.tasks.get(tid)["status"] == "cancelled"
    assert session(r, sid)["closed_reason"] == "ended"
    assert r.store.kv_get("session.answer." + tid) is None


@pytest.mark.asyncio
async def test_progress_and_end_while_running(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    event(r, sid, "现在进度", kind="progress_query", mid="m2")
    runner = r.make()
    await runner.tick()
    assert events(r)[0]["status"] == "applied"
    event(r, sid, "结束吧", kind="end", mid="m3")
    await runner.tick()
    assert r.cancelled == [tid] and r.tasks.get(tid)["status"] == "cancelled"
    assert session(r, sid)["closed_reason"] == "ended"
    assert payloads(r)[-1]["push_kind"] == "command"


@pytest.mark.asyncio
async def test_idle_expiry_of_paused_session_cancels_paused_task(rig):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="paused")
    with r.store.tx() as conn:
        conn.execute("UPDATE task_sessions SET state='paused',last_active_at=100,last_delivery_at=200 WHERE id=?", (sid,))
    await r.make().tick(now=3800)
    assert session(r, sid)["closed_reason"] == "idle_expired"
    assert r.tasks.get(tid)["status"] == "cancelled"
    assert not payloads(r)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reason", [("end", "ended"), ("admin_stop", "admin_stop")])
async def test_explicit_close_cancels_all_queued_and_running(rig, kind, reason):
    r = rig
    sid = open_idle(r)
    ids = [linked_task(r, sid, status=s) for s in ["running", "queued"]]
    event(r, sid, "结束", kind=kind)
    await r.make().tick()
    assert session(r, sid)["state"] == "closed" and session(r, sid)["closed_reason"] == reason
    assert all(r.tasks.get(t)["status"] == "cancelled" for t in ids)
    assert len(payloads(r)) == 1 and payloads(r)[0]["push_kind"] == "command"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,closes", [("idle", True), ("paused", True), ("working", False), ("asking", False), ("awaiting_approval", False)])
async def test_idle_expiry_sixty_minutes_only_idle_or_paused(rig, state, closes):
    r = rig
    sid = open_idle(r)
    with r.store.tx() as conn:
        conn.execute("UPDATE task_sessions SET state=?,last_active_at=100,last_delivery_at=200 WHERE id=?", (state, sid))
    runner = r.make()
    await runner.tick(now=3799)
    assert session(r, sid)["state"] != "closed"
    await runner.tick(now=3800)
    assert (session(r, sid)["state"] == "closed") == closes
    assert not payloads(r)


@pytest.mark.asyncio
async def test_other_approved_request_supersedes_idle_or_queues_running(rig):
    r = rig
    sid = open_idle(r)
    running = linked_task(r, sid, status="running")
    request = r.approvals.create(GID, kind="task", title="别人的活", quote="写报告", via="at",
        requester_id="20002", requester_name="小红", message_id="other")
    landed = r.approvals.approve(request["id"], by="管理员")
    tid = landed["task_id"]
    runner = r.make()
    assert not runner.admit(tid, running_ids={running})
    assert not runner.admit(tid, running_ids={running})
    assert session(r, sid)["state"] != "closed" and r.tasks.get(tid)["session_id"] is None
    assert len(payloads(r)) == 1 and "前面还有一个任务" in payloads(r)[0]["text"]
    assert payloads(r)[0]["at_user"] == "20002" and payloads(r)[0]["push_kind"] == "status"
    r.tasks.transition(running, "reviewing")
    r.tasks.transition(running, "completed")
    assert runner.admit(tid, running_ids=set())
    assert session(r, sid)["closed_reason"] == "superseded"
    current = r.sessions.get_open(GID, platform="qq")
    assert current["owner_id"] == "20002" and current["current_task_id"] == tid
    assert r.tasks.get(tid)["session_id"] == current["id"]
    assert "先忙别的了" in payloads(r)[-1]["text"] and payloads(r)[-1]["push_kind"] == "status"


@pytest.mark.asyncio
async def test_other_approved_request_supersedes_session_only_awaiting_approval(rig):
    """D12：会话只是在等批准、没有活在跑，就不挡别人已批准的活。"""
    r = rig
    sid = open_idle(r)
    with r.store.tx() as conn:
        conn.execute("UPDATE task_sessions SET state='awaiting_approval' WHERE id=?", (sid,))
    request = r.approvals.create(GID, kind="task", title="别人的活", quote="写报告", via="at",
        requester_id="20002", requester_name="小红", message_id="other2")
    tid = r.approvals.approve(request["id"], by="管理员")["task_id"]
    runner = r.make()
    assert runner.admit(tid, running_ids=set())
    assert session(r, sid)["closed_reason"] == "superseded"
    assert r.sessions.get_open(GID, platform="qq")["owner_id"] == "20002"


@pytest.mark.asyncio
async def test_legacy_task_in_unserved_group_keeps_old_admission(rig):
    """非会话旧任务：群不在服务列表时，开工闸不改旧行为。"""
    r = rig
    tid = r.tasks.create("777", title="旧任务", req="需求", criteria=[], source="request",
        requester_id=OWNER, requester_name="小明")
    with r.store.tx() as conn:
        conn.execute("UPDATE tasks SET request_id='R-legacy' WHERE id=?", (tid,))
    assert r.make().admit(tid, running_ids=set())


@pytest.mark.asyncio
async def test_multi_task_landing_runs_serially_current_follows_actual_task(rig):
    r = rig
    sid = open_idle(r)
    first = linked_task(r, sid, status="queued", title="第一件")
    second = linked_task(r, sid, status="queued", title="第二件")
    runner = r.make()
    assert not runner.admit(second, running_ids=set())
    assert runner.admit(first, running_ids=set())
    assert not runner.admit(second, running_ids={first})
    assert session(r, sid)["current_task_id"] == first
    r.tasks.transition(first, "running")
    assert session(r, sid)["state"] == "working"
    r.tasks.transition(first, "reviewing")
    r.tasks.transition(first, "completed")
    assert runner.admit(second, running_ids=set())
    assert session(r, sid)["current_task_id"] == second
    r.tasks.transition(second, "running")
    r.tasks.transition(second, "reviewing")
    r.tasks.transition(second, "completed")
    assert session(r, sid)["state"] == "idle" and session(r, sid)["last_delivery_at"] > 0


@pytest.mark.asyncio
async def test_approval_supplement_is_atomic_and_not_new_request(rig):
    r = rig
    sid = open_idle(r)
    rid = session(r, sid)["request_id"]
    with r.store.tx() as conn:
        conn.execute("UPDATE requests SET status='pending' WHERE id=?", (rid,))
        conn.execute("UPDATE task_sessions SET state='awaiting_approval' WHERE id=?", (sid,))
    event(r, sid, "加英文版", kind="supplement")
    await r.make().tick()
    await r.make().tick()
    request = r.store.read().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    assert request["quote"].count("加英文版") == 1
    assert not r.started and events(r)[0]["status"] == "applied"


@pytest.mark.asyncio
async def test_session_question_and_gate_answer_use_existing_resume(rig):
    from CharTyr_MaiWork.maiwork.coordinator import Coordinator
    from CharTyr_MaiWork.maiwork.session_gate import SessionGate, build_pattern
    from test_session_gate import message, BOT
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    coord = object.__new__(Coordinator)
    coord._store, coord._tasks, coord._outbox = r.store, r.tasks, r.outbox
    assert coord._wait_for_originator(tid, GID, r.tasks.start_attempt(tid), "选哪个？", reason="缺信息", outbox_key="ask:test", summary="等待回答")
    assert payloads(r)[0]["text"].endswith("@我回答就行")
    assert session(r, sid)["state"] == "asking"
    gate = SessionGate(r.store, r.sessions, lambda: r.settings, bot_ids=lambda p: {BOT},
        is_admin=lambda *a: False, triage=AsyncMock(return_value={"label": "for_maiwork", "confidence": .99}))
    gate.registered_pattern = build_pattern(["東雪蓮"], [BOT])
    gate.refresh()
    assert (await gate.handle(message("@東雪蓮 第二个")))[2]
    runner = r.make()
    await runner.tick()
    assert r.store.kv_get("session.answer." + tid)["answer"] == "第二个"
    assert r.started == [tid]
    old = r.tasks.create(GID, title="老任务", req="旧需求", criteria=[], source="admin", status="running")
    assert coord._wait_for_originator(old, GID, r.tasks.start_attempt(old), "请补充", reason="缺信息", outbox_key="ask:old", summary="等待")
    assert "引用这条消息回复我就行" in payloads(r)[-1]["text"]


@pytest.mark.asyncio
async def test_material_fetch_is_bounded_and_failure_degrades(rig):
    r = rig
    sid = open_idle(r)
    event(r, sid, "按他说的做", kind="adopt", quote="quoted")
    r.host.message_by_id.side_effect = [RuntimeError("gone"), {"processed_plain_text": "采用这个链接 https://example.com"}]
    await r.make().tick()
    assert r.host.message_by_id.await_count == 2
    task = r.tasks.get(r.started[0])
    assert "https://example.com" in task["req"] and "仅作素材" in task["req"]
    assert "文字" in events(r)[0]["note"]
    materials = r.store.kv_get("session.materials." + task["id"])
    assert materials["message_id"] == "m1" and materials["quoted_ref"] == "quoted"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,label", [("awaiting_approval", "等管理员批准"), ("working", "在做"), ("idle", "已交付"), ("asking", "在等他回答"), ("paused", "已暂停")])
async def test_one_factual_memo_per_state_keeps_budget(rig, state, label):
    r = rig
    sid = open_idle(r)
    with r.store.tx() as conn:
        conn.execute("UPDATE task_sessions SET state=? WHERE id=?", (state, sid))
    runner = r.make()
    runner.update_memo(session(r, sid))
    runner.update_memo(session(r, sid))
    rows = list(r.store.read().execute("SELECT text FROM mentions"))
    assert len(rows) == 1
    text = rows[0][0]
    assert all(x in text for x in ["小明", "网页截图", label, "进度由 MaiWork 回答"])
    assert "不要" not in text and "不用" not in text and len(text) < 200


@pytest.mark.asyncio
async def test_app_serial_spawn_and_stop_leave_no_controller_jobs(tmp_path):
    from test_app import _app
    app = _app(tmp_path)
    entered, release, second_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    class Coordinator:
        async def run_task(self, tid):
            if app.tasks.get(tid)["status"] != "queued":
                return
            calls.append(tid)
            app.tasks.transition(tid, "running")
            entered.set()
            await release.wait()
            app.tasks.transition(tid, "reviewing")
            app.tasks.transition(tid, "completed")
            if len(calls) == 2:
                second_done.set()
    app.coordinator_factory = lambda *a, **k: Coordinator()
    await app.start()
    try:
        request = app.approvals.create(GID, kind="task", title="两件事", quote="先后做",
            via="at", requester_id=OWNER, requester_name="小明", force_manual=True)
        first = app.approvals.approve(request["id"], by="管理员")["task_id"]
        second = app.tasks.create(GID, title="第二件", req="后做", criteria=[], source="session")
        with app.store.tx() as conn:
            app.sessions.on_task_landed(conn, request["id"], second)
        sid = app.tasks.get(first)["session_id"]
        app.spawn_run_task(first)
        app.spawn_run_task(second)
        await asyncio.wait_for(entered.wait(), 2)
        assert calls == [first] and session(app, sid)["current_task_id"] == first
        app._models_ready = lambda: True
        release.set()
        first_job = next(job for job in app._bg_jobs if job.get_name() == "maiwork-task-" + first)
        await asyncio.wait_for(first_job, 2)
        await asyncio.wait_for(second_done.wait(), 2)
        assert calls == [first, second]
        assert session(app, sid)["state"] == "idle" and session(app, sid)["last_delivery_at"] > 0
    finally:
        release.set()
        await app.stop()
    assert not app._bg_jobs and app._session_gate_task is None
    assert app.session_runner is None and not app._session_runner_pending


@pytest.mark.asyncio
async def test_answer_replay_after_revision_does_not_duplicate_answer(rig, monkeypatch):
    from CharTyr_MaiWork.maiwork.coordinator import Coordinator
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="waiting_input")
    coord = object.__new__(Coordinator)
    coord._store, coord._tasks = r.store, r.tasks
    coord._human_wait_record = lambda *a: None
    coord._record_answer = lambda *a: None
    coord.run_task = AsyncMock()
    real_transition = r.tasks.transition
    def crash(*args, **kwargs):
        raise RuntimeError("crash after revise")
    monkeypatch.setattr(r.tasks, "transition", crash)
    with pytest.raises(RuntimeError, match="crash after revise"):
        await coord.resume(tid, "用第二个链接", session_event_id=123)
    monkeypatch.setattr(r.tasks, "transition", real_transition)
    await coord.resume(tid, "用第二个链接", session_event_id=123)
    task = r.tasks.get(tid)
    assert task["req"].count("用第二个链接") == 1 and task["req_version"] == 2
    assert task["status"] == "queued"


@pytest.mark.asyncio
async def test_disabled_runner_reads_nothing_and_admits_old_behavior(rig, monkeypatch):
    r = rig
    runner = r.make()
    runner._settings = lambda: replace(r.settings, session=replace(r.settings.session, enabled=False))
    monkeypatch.setattr(r.store, "read", lambda: pytest.fail("关闭后不该读库"))
    await runner.tick()
    assert runner.admit("T-no-read", running_ids=set())
    assert not r.started and not r.judged


@pytest.mark.asyncio
async def test_app_revised_task_restarts_without_waiting_for_loop(tmp_path):
    """阶段 4：运行中改了要求 → 旧那轮停下后马上按新要求重开，不等 30 秒主循环。"""
    from test_app import _app
    app = _app(tmp_path)
    app.loop_interval = 3600
    entered, release, rerun = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    class Coordinator:
        async def run_task(self, tid):
            task = app.tasks.get(tid)
            if task["status"] != "queued":
                return
            calls.append(task["req_version"])
            app.tasks.transition(tid, "running")
            if len(calls) == 1:
                entered.set()
                await release.wait()
                return  # 旧那轮发现被改了要求，收手（状态已被 revise 排回 queued）
            rerun.set()
    app.coordinator_factory = lambda *a, **k: Coordinator()
    await app.start()
    try:
        app._models_ready = lambda: True
        request = app.approvals.create(GID, kind="task", title="截图", quote="打开网页截图",
            via="at", requester_id=OWNER, requester_name="小明", force_manual=True)
        tid = app.approvals.approve(request["id"], by="管理员")["task_id"]
        app.spawn_run_task(tid)
        await asyncio.wait_for(entered.wait(), 2)
        app.tasks.revise(tid, req="打开网页截图\n【主人补充】改成英文", criteria=[])
        assert app.tasks.get(tid)["status"] == "queued"
        release.set()
        await asyncio.wait_for(rerun.wait(), 2)
        assert calls == [1, 2]
    finally:
        release.set()
        await app.stop()
    assert not app._bg_jobs
