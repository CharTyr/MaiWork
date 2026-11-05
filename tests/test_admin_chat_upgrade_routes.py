"""HTTP contracts for chat controls, discovery, and skill export.

Adapter tests use stub services; integration tests use real AdminChat and SQLite.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat
from CharTyr_MaiWork.maiwork.console.auth import COOKIE_NAME
from CharTyr_MaiWork.maiwork.console.server import AUTH_KEY, create_app
from test_admin_chat import SECRET, _Svc

from CharTyr_MaiWork.maiwork import extensions_web, skills_web
from test_admin_chat_routes import ChatBusy, G1, G3, TOKEN_G1, env


@pytest.mark.asyncio
@pytest.mark.parametrize("resource,method,key", [
    ("skills", "available_skills", "skills"),
    ("refs", "available_refs", "refs"),
])
async def test_chat_discovery_static_routes_and_group_resolution(env, resource, method, key):
    calls = []

    def discover(group_id="", **kwargs):
        calls.append((group_id, kwargs))
        return [{"name": "example"}]

    setattr(env.chat, method, discover)
    await env.login_admin()
    response = await env.client.get(f"/api/chat/{resource}?group_id={TOKEN_G1}")
    assert response.status == 200
    assert (await response.json())[key] == [{"name": "example"}]
    assert calls[0][0] == G1
    response = await env.client.get(f"/api/chat/{resource}")
    assert response.status == 200
    assert calls[-1][0] == ""
    response = await env.client.get(f"/api/chat/{resource}?group_id={G3}")
    assert response.status == 404
    assert len(calls) == 2


NEW_ROUTES = [
    ("GET", "/api/chat/skills"),
    ("GET", "/api/chat/refs"),
    ("POST", "/api/chat/1/stop"),
    ("POST", "/api/chat/1/retry"),
    ("GET", "/api/extensions/skills/example/export"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", NEW_ROUTES)
async def test_upgrade_routes_require_admin(env, method, path):
    response = await env.client.request(method, path)
    assert response.status == 401
    response = await env.client.request(method, path, headers=env.member_headers)
    assert response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "retry"])
async def test_chat_control_success_and_origin_guard(env, action):
    calls = []

    async def control(cid):
        calls.append(cid)
        return {"stopped": True} if action == "stop" else {"accepted": True, "user_message_id": 12}

    setattr(env.chat, action, control)
    await env.login_admin()
    response = await env.client.post(f"/api/chat/1/{action}")
    assert response.status == 200
    assert await response.json() == ({"stopped": True} if action == "stop" else {"accepted": True, "user_message_id": 12})
    assert calls == [1]
    response = await env.client.post(f"/api/chat/1/{action}", headers={"Origin": "https://foreign.invalid"})
    assert response.status == 403
    assert calls == [1]
    response = await env.client.post(f"/api/chat/nope/{action}")
    assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "retry"])
@pytest.mark.parametrize("error,status", [(KeyError("missing"), 404), (ChatBusy("busy"), 409), (ValueError("invalid"), 400)])
async def test_chat_control_error_mapping(env, action, error, status):
    async def control(cid):
        raise error

    setattr(env.chat, action, control)
    await env.login_admin()
    response = await env.client.post(f"/api/chat/1/{action}")
    assert response.status == status
    assert (await response.json())["error"]


@pytest.mark.asyncio
async def test_messages_forward_optional_skill_without_breaking_old_callers(env):
    calls = []

    async def send(cid, text, **kwargs):
        calls.append((cid, text, kwargs))
        return 19

    env.chat.send = send
    await env.login_admin()
    response = await env.client.post("/api/chat/1/messages", json={"text": "hello", "skill": "example"})
    assert response.status == 202
    assert calls[-1] == (1, "hello", {"skill": "example"})
    response = await env.client.post("/api/chat/1/messages", json={"text": "plain"})
    assert response.status == 202
    assert calls[-1] == (1, "plain", {})


@pytest.mark.asyncio
async def test_compact_focus_optional_json_and_empty_bytes(env):
    calls = []

    async def compact(cid, **kwargs):
        calls.append((cid, kwargs))
        return {"summary": {"text": "summary"}}

    env.chat.compact = compact
    await env.login_admin()
    response = await env.client.post("/api/chat/1/compact", json={"focus": "keep constraints"})
    assert response.status == 200
    assert calls[-1] == (1, {"focus": "keep constraints"})
    response = await env.client.post("/api/chat/1/compact", data=b"")
    assert response.status == 200
    assert calls[-1] == (1, {})
    response = await env.client.post("/api/chat/1/compact", data=b"not json")
    assert response.status == 400
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_skill_export_static_route_zip_attachment(env, monkeypatch, tmp_path):
    calls = []
    env.svc.get_settings().data_dir = tmp_path

    def export(data_dir, store, name):
        calls.append((data_dir, name))
        return b"PK\x03\x04test-archive"

    monkeypatch.setattr(skills_web, "export_zip", export, raising=False)
    await env.login_admin()
    response = await env.client.get("/api/extensions/skills/example/export")
    assert response.status == 200
    assert response.content_type == "application/zip"
    assert "attachment" in response.headers["Content-Disposition"]
    assert ".zip" in response.headers["Content-Disposition"]
    assert await response.read() == b"PK\x03\x04test-archive"
    assert calls == [(tmp_path, "example")]


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(KeyError("missing"), 404), (ValueError("invalid"), 400)])
async def test_skill_export_error_mapping(env, monkeypatch, tmp_path, error, status):
    env.svc.get_settings().data_dir = tmp_path

    def export(*args):
        raise error

    monkeypatch.setattr(skills_web, "export_zip", export, raising=False)
    await env.login_admin()
    response = await env.client.get("/api/extensions/skills/example/export")
    assert response.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_extensions_suggestions_and_logged_fallback(env, monkeypatch, tmp_path, caplog, fails):
    env.svc.get_settings().data_dir = tmp_path
    env.svc.extensions = None
    monkeypatch.setattr(extensions_web, "list_items", lambda *a: [])
    monkeypatch.setattr(skills_web, "list_view", lambda *a: [{"name": "installed"}])

    def suggestions(svc, existing_names=None, **kwargs):
        assert svc is env.svc
        if fails:
            raise RuntimeError("suggestions unavailable")
        return [{"name": "suggested"}]

    monkeypatch.setattr(skills_web, "list_suggestions", suggestions, raising=False)
    await env.login_admin()
    response = await env.client.get("/api/extensions")
    assert response.status == 200
    data = await response.json()
    assert data["skills"] == [{"name": "installed"}]
    assert data["skill_suggestions"] == ([] if fails else [{"name": "suggested"}])
    if fails:
        assert "suggestions unavailable" in caplog.text


@pytest_asyncio.fixture
async def real_http(tmp_path):
    svc = _Svc(tmp_path)
    svc.admin_pending._store = svc.store
    svc.admin_chat = AdminChat(svc)
    app = create_app(svc)
    client = TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    cookie, _ = app[AUTH_KEY].make_cookie()
    client.session.cookie_jar.update_cookies({COOKIE_NAME: cookie})
    cid = svc.admin_chat.create(G1)["id"]
    try:
        yield svc, svc.admin_chat, client, cid
    finally:
        await svc.admin_chat.close()
        await client.close()
        svc.store.close()


@pytest.mark.asyncio
async def test_real_chat_detail_preserves_budget_status(real_http):
    svc, chat, client, cid = real_http
    response = await client.get(f"/api/chat/{cid}")
    assert response.status == 200
    data = await response.json()
    assert data["status"] == chat.detail(cid)["status"]
    assert data["status"]["has_uncompacted"] is False
    response = await client.post(f"/api/chat/{cid}/messages", json={"text": "hello"})
    assert response.status == 202
    assert await chat.wait_idle(cid)
    response = await client.get(f"/api/chat/{cid}")
    assert (await response.json())["status"]["has_uncompacted"] is True


@pytest.mark.asyncio
async def test_real_chat_stop_retry_and_error_statuses(real_http):
    svc, chat, client, cid = real_http
    response = await client.post(f"/api/chat/{cid}/retry")
    assert response.status == 400
    svc.models.gate = asyncio.Event()
    response = await client.post(f"/api/chat/{cid}/messages", json={"text": "original"})
    assert response.status == 202
    response = await client.post(f"/api/chat/{cid}/retry")
    assert response.status == 409
    response = await client.post(f"/api/chat/{cid}/stop")
    assert response.status == 200
    assert await response.json() == {"ok": True, "stopped": True, "chat_id": cid}
    assert not chat.is_busy(cid)
    assert chat.detail(cid)["messages"][-1]["meta"]["kind"] == "stopped"
    svc.models.gate = None
    response = await client.post(f"/api/chat/{cid}/retry")
    assert response.status == 200
    assert await response.json() == {"ok": True, "chat_id": cid}
    assert await chat.wait_idle(cid)
    rows = chat.detail(cid)["messages"]
    assert [m["content"] for m in rows if m["role"] == "user"] == ["original"]
    assert rows[-1]["role"] == "assistant"
    assert not any(m["meta"].get("kind") == "stopped" for m in rows)
    for action in ("stop", "retry"):
        response = await client.post(f"/api/chat/99999/{action}")
        assert response.status == 404


@pytest.mark.asyncio
async def test_real_chat_skill_discovery_selection_and_focus(real_http, monkeypatch):
    svc, chat, client, cid = real_http
    skills_web.create(svc.get_settings().data_dir, svc.store, {
        "name": "http-demo", "description": "HTTP skill", "roles": ["worker"],
        "body": "http_unique_skill_instruction " + SECRET,
    })
    response = await client.get(f"/api/chat/skills?group_id={G1}")
    assert response.status == 200
    assert any(s["name"] == "http-demo" for s in (await response.json())["skills"])
    with svc.store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks(id, group_id, workspace, title, status, created, updated) "
            "VALUES ('HTTP-1', ?, 'test', ?, 'queued', 1, 3)",
            (G1, "HTTP task " + SECRET),
        )
        conn.execute(
            "INSERT INTO ideas(group_id, title, state, created, updated) "
            "VALUES (?, 'unserved private idea', 'new', 1, 6)", (G3,),
        )
    response = await client.get(f"/api/chat/refs?group_id={G1}")
    assert response.status == 200
    refs = (await response.json())["refs"]
    assert any(r["id"] == "HTTP-1" for r in refs)
    assert all(r["group_id"] == G1 for r in refs)
    assert SECRET not in str(refs)
    assert "unserved private idea" not in str(refs)
    response = await client.get(f"/api/chat/refs?group_id={G3}")
    assert response.status == 404
    response = await client.post(f"/api/chat/{cid}/messages", json={"text": "work", "skill": "http-demo"})
    assert response.status == 202
    assert await chat.wait_idle(cid)
    rows = chat.detail(cid)["messages"]
    user = next(m for m in rows if m["role"] == "user")
    assert user["content"] == "work"
    assert user["meta"]["skill"]["name"] == "http-demo"
    assert "http_unique_skill_instruction" in str(svc.models.calls[-1][1])
    assert SECRET not in str(svc.models.calls[-1][1])
    response = await client.post(f"/api/chat/{cid}/messages", json={"text": "", "skill": "http-demo"})
    assert response.status == 202
    assert await chat.wait_idle(cid)
    user = [m for m in chat.detail(cid)["messages"] if m["role"] == "user"][-1]
    assert user["content"] == "请按所选技能处理"
    assert user["meta"]["skill"]["name"] == "http-demo"
    seen = []

    async def summarize(*args, **kwargs):
        seen.append(kwargs["focus"])
        return SimpleNamespace(text="HTTP summary", coverage=None)

    monkeypatch.setattr(compaction, "summarize_messages_ex", summarize)
    response = await client.post(f"/api/chat/{cid}/compact", json={"focus": "keep HTTP constraints " + SECRET})
    assert response.status == 200
    assert "keep HTTP constraints" in seen[0]
    assert SECRET not in seen[0]
    chat._add_msg(cid, "user", "new content after summary")
    response = await client.post(f"/api/chat/{cid}/compact", data=b"")
    assert response.status == 200
