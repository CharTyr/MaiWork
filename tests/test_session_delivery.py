"""3b：交付去重、睡觉时段和有限立即唤醒。"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_session_runner import rig, open_idle, linked_task, session, GID
from test_coordinator import _build, _plan, _review, ModelsQueue, FakeWorkers, FakeDelivery, FakeOutbox
from test_outbox import _make, _seed_task, _artifact_file, _rows, SLEEP
from test_text_delivery import _seed_request, _host_with_bot_reply, LETTER
from test_app import _app
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.tools import Tools
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.outbox import Delivery
from CharTyr_MaiWork.maiwork import clock


def coordinator(r, *, models=None, host=None, outbox=None):
    return _build(mem_store=r.store, settings=r.settings, env=LocalEnv(lambda:r.settings),
        tools=Tools(r.store), tasks=r.tasks, goals=Goals(r.store, lambda:r.settings),
        models=models or ModelsQueue(), workers=FakeWorkers(), host=host, outbox=outbox)


@pytest.mark.asyncio
async def test_coordinator_calls_main_speech_only_for_session_task(rig, monkeypatch):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="queued")
    c = coordinator(r)
    c._session_speech = SimpleNamespace(start=AsyncMock(), finish=AsyncMock(return_value="页面核对好了，点开就能看。"))
    c._plan = AsyncMock(return_value={"question": "选哪一个页面？"})
    await c.run_task(tid)
    c._session_speech.start.assert_awaited_once()
    c._session_speech.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_revision_during_plan_dispatches_nothing_and_voids_attempt(rig):
    """阶段 4：主模型排计划时主人改了要求 → 不派子 agent，这一轮记作废，任务排队等按新要求重开。"""
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="queued")
    c = coordinator(r)
    async def plan(task, **kwargs):
        r.tasks.revise(tid, req="需求\n【主人补充】改成英文", criteria=[])
        return {"question": "", "jobs": [{"brief": "做一页", "tools": ["write_file"]}],
                "criteria": ["包含链接"], "deliver_kind": "view"}
    c._plan = plan
    await c.run_task(tid)
    assert c._workers.calls == []
    assert r.tasks.get(tid)["status"] == "queued" and r.tasks.get(tid)["req_version"] == 2
    rows = r.store.read().execute("SELECT status FROM attempts WHERE task_id=?", (tid,)).fetchall()
    assert [row["status"] for row in rows] == ["stale"]


@pytest.mark.asyncio
@pytest.mark.parametrize("in_session", [True, False])
async def test_main_text_beside_tool_calls_is_relayed_only_for_session_task(rig, in_session):
    """D8：主模型边说边调工具时，那句自然话交给会话发言（来源 main）；工具本身不发。"""
    from CharTyr_MaiWork.maiwork.tools import ToolContext
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid, status="running")
    if not in_session:
        with r.store.tx() as conn:
            conn.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (tid,))
    c = coordinator(r)
    relayed = []
    c._session_speech = SimpleNamespace(relay=lambda task, text, *, source: relayed.append((task["id"], text, source)))
    ctx = ToolContext(group_id=GID, task_id=tid, actor="主模型", workspace=None, role="main", allowed_tools=())
    calls = [{"id": "c1", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}}]
    await c._run_tool_calls([], calls, ctx, assistant_text="我先打开页面看看。")
    assert relayed == ([(tid, "我先打开页面看看。", "main")] if in_session else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("turn,source,enabled,expected", [(2,"request",True,False), (1,"session",True,False), (1,"request",True,True), (2,"request",False,True)])
async def test_maibot_quote_dedup_only_first_round(rig, turn, source, enabled, expected):
    r = rig
    sid = open_idle(r)
    tid = linked_task(r, sid)
    _seed_request(r.store)
    with r.store.tx() as conn:
        conn.execute("UPDATE tasks SET turn_no=?,source=?,request_id='REQ-1' WHERE id=?", (turn,source,tid))
    r.settings = replace(r.settings, session=replace(r.settings.session,enabled=enabled))
    host = _host_with_bot_reply(reply_text="帮我写封信\n"+LETTER)
    c = coordinator(r, host=host)
    assert await c._maibot_already_answered(tid, GID, our_len=len(LETTER)) is expected


@pytest.mark.asyncio
async def test_delivery_dedup_tracks_task_revision_and_artifact(tmp_path):
    store, settings, host, pushes, mentions, ob = _make(tmp_path)
    _seed_task(store)
    with store.tx() as conn:
        conn.execute("UPDATE tasks SET session_id='S-test',turn_no=2 WHERE id='T-1'")
    delivery = Delivery(store,ob)
    path = _artifact_file(tmp_path,"a.txt",text="第一版")
    first = await delivery.deliver_task("T-1",kind="file",path=path,name="a.txt",note="交付说明")
    assert await delivery.deliver_task("T-1",kind="file",path=path,name="a.txt",note="另一句说明") == first
    with store.tx() as conn:
        conn.execute("UPDATE tasks SET req_version=2 WHERE id='T-1'")
    second = await delivery.deliver_task("T-1",kind="file",path=path,name="a.txt",note="第二版")
    assert second != first
    path2 = _artifact_file(tmp_path,"b.txt",text="另一份产物")
    third = await delivery.deliver_task("T-1",kind="file",path=path2,name="b.txt",note="同版另一份")
    assert third not in {first,second}
    assert len(_rows(store)) == 3


@pytest.mark.asyncio
async def test_command_at_reply_bypasses_quiet_status_still_waits(tmp_path):
    store, _, host, _, _, ob = _make(tmp_path)
    _seed_task(store)
    for kind in ["command", "status"]:
        ob.enqueue(kind,GID,"text",{"text":kind,"push_kind":kind,"at_user":"10001","reply_to":"m-owner"},task_id="T-1")
    await ob.flush(SLEEP)
    assert [m["text"] for m in host.texts] == ["command"]
    assert host.texts[0]["at_user"] == "10001" and host.texts[0]["reply_to"] == "m-owner"
    rows = _rows(store)
    assert rows[0]["status"] == "sent" and rows[1]["status"] == "pending" and rows[1]["not_before"] > SLEEP


@pytest.mark.asyncio
async def test_command_enqueue_wakes_after_commit_only(tmp_path):
    store, _, _, _, _, ob = _make(tmp_path)
    wakes=[]
    ob.set_wake_hook(lambda delay: wakes.append((delay,store.read().in_transaction)))
    with store.tx() as conn:
        ob.enqueue("session:direct",GID,"text",{"text":"进度回答","push_kind":"command"},conn=conn)
        assert not wakes
    await asyncio.sleep(0)
    assert wakes == [(0,False)]
    ob.enqueue("session:status",GID,"text",{"text":"排队通知","push_kind":"status"})
    await asyncio.sleep(0)
    assert len(wakes)==1


@pytest.mark.asyncio
async def test_app_immediate_flush_is_finite_coalesced_and_served(tmp_path):
    app = _app(tmp_path)
    from CharTyr_MaiWork.maiwork.config import load_settings
    app._settings, _ = load_settings({"groups": {"serve": [{"group": "qq:" + GID}]}})
    entered, release = asyncio.Event(), asyncio.Event()
    calls=[]
    async def flush(now, *, allowed_groups=None):
        calls.append(allowed_groups)
        entered.set()
        await release.wait()
    app.outbox=SimpleNamespace(flush=flush)
    for _ in range(10):
        app._wake_outbox()
    await asyncio.wait_for(entered.wait(),.5)
    assert len(app._bg_jobs)==1
    release.set()
    await asyncio.wait_for(asyncio.gather(*list(app._bg_jobs)),.5)
    await asyncio.sleep(0)
    assert not app._bg_jobs and len(calls)==1
    assert calls[0]==set(app.get_settings().groups)


@pytest.mark.asyncio
async def test_final_main_sentence_and_pending_line_share_text_delivery(rig, tmp_path):
    from CharTyr_MaiWork.maiwork.session_speech import SessionSpeech
    r = rig
    r.settings = replace(r.settings, workspace_root=tmp_path / "work", environments=replace(r.settings.environments,workspace_root=tmp_path / "work",local_mode="direct"))
    sid = open_idle(r)
    tid = linked_task(r, sid, status="reviewing")
    models = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(text="我把这页核对好了。", tool_calls=[])))
    c = coordinator(r, outbox=r.outbox)
    s = c._session_speech = SessionSpeech(r.store,r.outbox,lambda:r.settings,models=models)
    path = c._env.workspace(r.tasks.get(tid)["workspace"]) / "artifacts" / tid / "reply.md"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("这是验收通过的正文。",encoding="utf-8")
    s.relay(r.tasks.get(tid),"我再核对一下页面。",source="main")
    await c._handle_passed(tid,GID,r.tasks.get(tid)["workspace"],{"deliver_kind":"text"},
        {"review":"通过","note":"点开就能看。","artifact":f"artifacts/{tid}/reply.md"})
    rows=list(r.store.read().execute("SELECT * FROM outbox WHERE status='pending'"))
    assert len(rows)==1
    payload=json.loads(rows[0]["payload"])
    assert all(t in payload["text"] for t in ["我把这页核对好了。","我再核对一下页面。","这是验收通过的正文。"])
    assert payload["push_kind"]=="command" and payload["at_user"]=="10001" and payload["reply_to"]=="original"
    assert r.tasks.get(tid)["status"]=="completed" and session(r,sid)["state"]!="closed"


@pytest.mark.asyncio
async def test_new_revision_recovery_ignores_old_sent_artifact(tmp_path):
    store, settings, host, pushes, mentions, ob = _make(tmp_path)
    _seed_task(store)
    with store.tx() as conn:
        conn.execute("UPDATE tasks SET session_id='S-test',turn_no=2,delivery_kind='text' WHERE id='T-1'")
    from CharTyr_MaiWork.maiwork.session_speech import delivery_key
    old_key=delivery_key(store,settings,"T-1","text:旧正文")+":text"
    oid=ob.enqueue(old_key,GID,"text",{"text":"旧正文","push_kind":"delivery"},task_id="T-1")
    with store.tx() as conn:
        conn.execute("UPDATE outbox SET status='sent' WHERE id=?",(oid,))
        conn.execute("UPDATE tasks SET req_version=2 WHERE id='T-1'")
    delivery=Delivery(store,ob)
    assert delivery.undelivered("T-1")
    assert await delivery.reenqueue_missing("T-1",LocalEnv(lambda:settings))
    rows=_rows(store)
    assert len(rows)==2 and ":r2:" in rows[1]["key"]


@pytest.mark.asyncio
async def test_closing_proposal_reaches_real_at_gate(tmp_path):
    from test_session_gate import message
    # 采用相同真实入口构造，但不直接调用 pytest fixture。
    from CharTyr_MaiWork.maiwork.session_gate import SessionGate, build_pattern
    from CharTyr_MaiWork.maiwork.sessions import Sessions
    from CharTyr_MaiWork.maiwork.store import Store
    from CharTyr_MaiWork.maiwork.config import load_settings
    store=Store(tmp_path/"gate.db")
    store.migrate()
    settings,_=load_settings({"plugin":{"enabled":True},"groups":{"serve":[{"group":"qq:"+GID}]},"session":{"bot_names":["東雪蓮"]}})
    sessions=Sessions(store)
    with store.tx() as conn:
        row=sessions.open_for_request(conn,dict(id="R-1",group_id=GID,platform="qq",requester_id="10001",kind="task",source=""))
        conn.execute("UPDATE task_sessions SET state='working' WHERE id=?",(row["id"],))
        store.kv_set(conn,"session.closing."+row["id"],{"task_id":"T-1","req_version":1})
    seen=[]
    async def triage(state,**kwargs):
        seen.append(state)
        return {"label":"for_maiwork","confidence":.95}
    gate=SessionGate(store,sessions,lambda:settings,bot_ids=lambda p:{"100000002"},triage=triage,is_admin=lambda *a:False)
    gate.refresh()
    try:
        assert await gate.handle(message("@東雪蓮 确认"))==(True,None,True)
        assert seen[0]["closing_proposed"]=={"task_id":"T-1","req_version":1}
        assert not await gate.handle(message("@東雪蓮 确认",uid="20002",mid="other")) == (True,None,True)
        assert not await gate.handle(message("@東雪蓮 确认",at=False,mid="fake")) == (True,None,True)
    finally:
        store.close()
