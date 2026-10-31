"""Round-2 polish regressions (S1 ~ S4)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from test_console import env, G1
from test_idea_mention_reply import (
    GID,
    NOW,
    _FakeApprovals,
    _FakeJev,
    _drain,
    _make,
    _seed_idea_and_mention,
)
from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.session_gate import SessionGate
from CharTyr_MaiWork.maiwork.sessions import Sessions
from CharTyr_MaiWork.maiwork.share import render_page
from CharTyr_MaiWork.maiwork.store import Store


def _real_qq_quote_msg(*, quoted_text: str, user_reply: str, reply_to: str, user_id: str = "20002", mid: str = "rq-1"):
    """Simulate real MaiBot QQ quote-reply payload (docs/06:114-121)."""
    return {
        "message": {
            "platform": "qq",
            "message_id": mid,
            "timestamp": str(NOW),
            "is_at": True,
            "is_mentioned": True,
            "is_command": False,
            "is_emoji": False,
            "is_picture": False,
            "is_notify": False,
            "session_id": f"qq:group:{GID}",
            "processed_plain_text": f"[reply]{quoted_text} @Mai Bot {user_reply}",
            "message_info": {
                "platform": "qq",
                "group_info": {"group_id": GID, "group_name": "测试群"},
                "user_info": {"user_id": user_id, "user_nickname": "阿明"},
            },
            "raw_message": {
                "type": "seglist",
                "data": [
                    {"type": "reply", "data": {"id": reply_to, "target_message_id": reply_to}},
                    {
                        "type": "at",
                        "data": {
                            "qq": "10001",
                            "target_user_id": "10001",
                            "target_user_cardname": "Mai Bot",
                            "target_user_nickname": "MaiBot",
                        },
                    },
                    {"type": "text", "data": {"text": f" {user_reply}"}},
                ],
            },
        }
    }


@pytest.mark.asyncio
async def test_s1_real_qq_reply_wrapper_and_spaced_bot_name(tmp_path):
    """Real QQ `[reply]<quoted bot text> @Mai Bot 好，做第 2 个` must ignore quoted text."""
    jev = _FakeJev({"verdict": ("chat", 1.0, 1.0)})
    approvals = _FakeApprovals()
    intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals)
    iid = _seed_idea_and_mention(store, intake, mention_mid="im-qq-1")
    # Quoted mention contains both "不用" (would false-trigger reject) and "1. ... 2. ..."!
    msg = _real_qq_quote_msg(
        quoted_text="要不要我帮你们做：1. 查清原因 2. 整理步骤（你们不用自己动手）",
        user_reply="好，做第 2 个",
        reply_to="im-qq-1",
    )
    await intake.handle(msg)
    await _drain(spawned)
    assert len(approvals.created) == 1
    assert approvals.created[0][1]["idea_id"] == iid
    assert approvals.created[0][1]["items"] == [2]
    assert jev.calls == []


@pytest.mark.asyncio
async def test_s1_plain_reply_prefix_fallback_without_segments(tmp_path):
    """Fallback when raw_message is plain text still strips `[reply]... @Bot`."""
    jev = _FakeJev({"verdict": ("chat", 1.0, 1.0)})
    approvals = _FakeApprovals()
    intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals)
    iid = _seed_idea_and_mention(store, intake, mention_mid="im-qq-2")
    await intake._handle_idea_mention_reply(
        intake._get_settings(),
        GID,
        "20002",
        "阿明",
        "rq-2",
        "[reply]1. 第一项 2. 第二项（不用慌） @MaiBot 做第 2 个",
        {"idea_id": iid, "title": "想法"},
    )
    assert len(approvals.created) == 1
    assert approvals.created[0][1]["items"] == [2]
    assert jev.calls == []


@pytest.mark.asyncio
async def test_s1_idea_reply_marks_session_gate_to_skip_native_command(tmp_path):
    """After Intake claims an idea reply, native command `maiwork_session` must skip it."""
    store = Store(tmp_path / "gate.db")
    store.migrate()
    try:
        settings, _ = load_settings(
            {
                "plugin": {"enabled": True},
                "groups": {"serve": [{"group": f"qq:{GID}"}]},
                "approval": {"admins": ["qq:99999"]},
            }
        )
        sessions = Sessions(store)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO requests (id, group_id, requester_id, requester_name, kind, title, quote, status, created, updated)"
                " VALUES ('R-1', ?, '20002', '阿明', 'task', '旧任务', 'd', 'approved', ?, ?)",
                (GID, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO tasks (id, group_id, workspace, title, req, criteria, status, source, created, updated)"
                " VALUES ('T-1', ?, 'ws', '旧任务', 'r', '[]', 'running', 'R-1', ?, ?)",
                (GID, NOW, NOW),
            )
            row_s = sessions.open_for_request(
                conn,
                {"id": "R-1", "group_id": GID, "platform": "qq", "requester_id": "20002", "kind": "task", "source": ""},
            )
            sid = row_s["id"]
            conn.execute("UPDATE task_sessions SET state='working', current_task_id='T-1' WHERE id=?", (sid,))
        triage_calls = []

        async def fake_triage(state, *, admin, timeout_ms):
            triage_calls.append((state, admin))
            return {"label": "stop" if admin else "for_maiwork", "confidence": 0.99, "stop": admin}

        gate = SessionGate(
            store,
            sessions,
            lambda: settings,
            bot_ids=lambda p: {"10001"},
            is_admin=lambda uid, plat, gid=None: str(uid) == "99999",
            triage=fake_triage,
        )
        gate.refresh()
        spawned = []
        approvals = _FakeApprovals(admins={"99999"})
        intake = Intake(
            lambda: settings,
            Signals(),
            approvals=approvals,
            store=store,
            spawn=lambda c: spawned.append(c),
            session_gate=gate,
            bot_qq="10001",
        )
        _seed_idea_and_mention(store, intake, mention_mid="im-gate-1")

        # Group admin quote-replies "不用做了" to an idea mention while someone else's session is open!
        admin_msg = _real_qq_quote_msg(
            quoted_text="要不要试试这个构想",
            user_reply="不用做了",
            reply_to="im-gate-1",
            user_id="99999",
            mid="rq-admin-stop",
        )
        await intake.handle(admin_msg)
        native_res = await gate.handle(admin_msg)
        await _drain(spawned)

        assert native_res == (True, None, False)
        assert triage_calls == []
        row = store.read().execute("SELECT state FROM task_sessions WHERE id=?", (sid,)).fetchone()
        assert row["state"] != "closed"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_s2_symlinked_parent_mount_and_dotdot_workspace_root_succeed(env, monkeypatch):
    """OS/ops symlink above workspace_root (e.g. /tmp -> /private/tmp, /data -> /mnt/data) must work."""
    real_disk = env.tmp_path / "real_disk"
    real_ws = real_disk / "workspaces"
    real_ws.mkdir(parents=True)
    mount_link = env.tmp_path / "mnt_data"
    mount_link.symlink_to(real_disk, target_is_directory=True)
    # Also include an uncollapsed `..` segment in workspace_root
    configured_root = mount_link / "sub" / ".." / "workspaces"
    (mount_link / "sub").mkdir(parents=True, exist_ok=True)

    orig_get_settings = env.app.get_settings

    def patched_settings():
        s = orig_get_settings()
        object.__setattr__(s, "workspace_root", configured_root)
        object.__setattr__(s.environments, "workspace_root", configured_root)
        return s

    monkeypatch.setattr(env.app, "get_settings", patched_settings)
    env.app.console._svc.get_settings = patched_settings
    env.app.outbox._get_settings = patched_settings

    tid = env.app.tasks.create(G1, title="挂载盘任务", req="r", criteria=[], source="test", status="completed")
    base = real_ws / "tinker" / "artifacts" / tid
    base.mkdir(parents=True)
    (base / "index.html").write_text('<link rel="stylesheet" href="./style.css"><p>挂载盘正常打开</p>', encoding="utf-8")
    (base / "style.css").write_text("body { color: green; }", encoding="utf-8")
    with env.app.store.tx() as conn:
        conn.execute(
            "INSERT INTO attempts (task_id,n,req_version,status,artifacts,started) VALUES (?,1,1,'passed',?,0)",
            (tid, json.dumps([f"artifacts/{tid}/index.html"])),
        )

    token = env.app.token_of(G1)
    r_index = await env.client.get(f"/g/{token}/artifacts/{tid}/")
    r_css = await env.client.get(f"/g/{token}/artifacts/{tid}/style.css")
    assert r_index.status == 200 and "挂载盘正常打开" in await r_index.text()
    assert r_css.status == 200

    # Handoff excerpt also works under symlink-mounted workspace_root
    r_ho = await env.client.get(f"/api/handoff/task/{tid}", headers={"X-MW-Group": token})
    assert r_ho.status == 200
    assert "挂载盘正常打开" in (await r_ho.json())["markdown"]

    # Meanwhile, a symlink *inside* the workspace artifact tree is still rejected!
    outside = env.tmp_path / "secret.txt"
    outside.write_text("top-secret", encoding="utf-8")
    (base / "leak.txt").symlink_to(outside)
    r_evil = await env.client.get(f"/g/{token}/artifacts/{tid}/leak.txt")
    assert r_evil.status == 403


@pytest.mark.asyncio
async def test_s3_group_admin_force_republish_before_expiry(env):
    """Group admin clicking '重新生成外链' before expiry must be allowed (`force=True`)."""
    tid = env.app.tasks.create(G1, title="群管重发", req="r", criteria=[], source="test", status="completed")
    base = env.tmp_path / "workspaces/tinker/artifacts" / tid
    base.mkdir(parents=True)
    (base / "index.html").write_text("<p>v1</p>", encoding="utf-8")
    ob = env.app.outbox
    oid = ob.enqueue(f"task:{tid}:deliver", G1, "herenow", {"note": "成品", "push_kind": "delivery"}, task_id=tid)
    ob._set(oid, status="sent", result={"url": "https://still-valid.here.now/", "expires_ts": clock.now() + 7200})

    class Publisher:
        async def publish(self, directory):
            return {"url": "https://group-admin-fresh.here.now/", "expires_ts": clock.now() + 86400}

    ob._herenow = Publisher()
    env.app.group_admins.set_password(G1, "ga-pass-123456")

    login = await env.login("ga-pass-123456")
    assert login.status == 200 and (await login.json())["role"] == "group_admin"
    resp = await env.client.post(f"/api/tasks/{tid}/republish", json={})
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["url"] == "https://group-admin-fresh.here.now/"


def test_s4_public_share_hides_dispatch_command_for_started_pending_and_talk():
    """Only `new` / `wanted` non-talk ideas should show dispatch commands on static share page."""
    for idea in (
        {"id": 1, "title": "已开工", "state": "started", "kind": "help"},
        {"id": 2, "title": "待审批", "state": "pending", "kind": "help"},
        {"id": 3, "title": "纯聊天话题", "state": "new", "kind": "talk"},
    ):
        page = render_page({"ideas": [idea]}, tab="ideas")
        assert "/mw 派活 构想" not in page, idea
        assert "@机器人 构想" not in page, idea

    wanted_page = render_page({"ideas": [{"id": 4, "title": "想做", "state": "wanted", "kind": "help"}]}, tab="ideas")
    assert "/mw 派活 构想 #4" in wanted_page
