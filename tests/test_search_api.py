"""联网搜索绑定 API 测试：GET/PUT/DELETE /api/extensions/search + 扩展条目上的 search_role 徽章。

起真 app + console（本机回环，不碰外网）；MCP 连接走 httpx.MockTransport。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.store import Store

PASSWORD = "搜索绑定测试密码-显眼-789"
G1 = "900000001"
MCP_URL = "https://mcp-search.example/mcp"

TAVILY_TOOLS = [
    {
        "name": "tavily-search",
        "description": "搜索网页",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "tavily-extract",
        "description": "抽正文",
        "inputSchema": {"type": "object", "properties": {"urls": {"type": "array"}}, "required": ["urls"]},
    },
]


def _sse(payload: dict, session: str = "sess-api") -> Any:
    import httpx

    body = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream", "mcp-session-id": session})


class _McpServer:
    def __init__(self, tools: list[dict]) -> None:
        self._tools = tools
        self.requests: list[dict] = []

    def __call__(self, request):
        body = json.loads(request.content.decode())
        self.requests.append({"method": body.get("method")})
        method = body.get("method")
        if method == "initialize":
            return _sse({
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}},
            })
        if method == "notifications/initialized":
            import httpx

            return httpx.Response(202, text="")
        if method == "tools/list":
            return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self._tools}})
        return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "{}"}]}})


def _write_plugin_dir(plug_dir: Path, data_dir: Path) -> None:
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    for section, values in {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"admins": ["qq:10001"]},
    }.items():
        t = tomlkit.table()
        for k, v in values.items():
            t[k] = v
        doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    import httpx

    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    _write_plugin_dir(plug_dir, data_dir)
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"admins": ["qq:10001"]},
    }
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    server_stub = _McpServer(TAVILY_TOOLS)
    app.extensions_transport = httpx.MockTransport(server_stub)
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()

    class _E:
        pass

    e = _E()
    e.app, e.client, e.plug_dir, e.data_dir = app, client, plug_dir, data_dir
    yield e
    await client.close()
    await app.stop()


async def _login(env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


async def _add_tavily(env) -> dict:
    r = await env.client.post("/api/extensions/mcp", json={"name": "tavily", "url": MCP_URL, "headers": {"Authorization": "Bearer fake-key-ABC123"}})
    assert r.status == 200, await r.text()
    return await r.json()


class TestSearchApi:
    @pytest.mark.asyncio
    async def test_requires_admin(self, env) -> None:
        assert (await env.client.get("/api/extensions/search")).status == 401
        assert (await env.client.put("/api/extensions/search", json={"mcp": "a", "tool": "b"})).status == 401
        assert (await env.client.delete("/api/extensions/search")).status == 401

    @pytest.mark.asyncio
    async def test_empty_then_bind_then_unbind(self, env) -> None:
        await _login(env)
        # 初始：没绑定
        r = await env.client.get("/api/extensions/search")
        assert r.status == 200
        data = await r.json()
        assert data["binding"] is None
        assert data["status"]["ok"] is False
        assert "还没指定联网搜索" in data["status"]["text"]

        await _add_tavily(env)
        # 候选里出现 tavily 的两个工具，带猜测
        data = await (await env.client.get("/api/extensions/search")).json()
        cand = {c["mcp"]: c for c in data["candidates"]}
        assert "tavily" in cand
        tools = {t["name"]: t for t in cand["tavily"]["tools"]}
        assert tools["tavily-search"]["guess"] == "search"
        assert tools["tavily-extract"]["guess"] == "extract"

        # 绑定
        r = await env.client.put("/api/extensions/search", json={"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"})
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["binding"] == {"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"}
        assert data["status"]["ok"] is True
        assert "tavily-search" in data["status"]["text"]

        # GET /api/extensions 的条目上有徽章
        ext = await (await env.client.get("/api/extensions")).json()
        item = [m for m in ext["mcp"] if m["name"] == "tavily"][0]
        assert item["search_role"] == "search"
        # search 可用
        assert env.app.search.available() is True

        # 解绑
        r = await env.client.delete("/api/extensions/search")
        assert r.status == 200
        data = await r.json()
        assert data["binding"] is None
        assert data["status"]["ok"] is False
        assert env.app.search.available() is False

    @pytest.mark.asyncio
    async def test_put_validation(self, env) -> None:
        await _login(env)
        await _add_tavily(env)
        r = await env.client.put("/api/extensions/search", json={"mcp": "没有", "tool": "x"})
        assert r.status == 400
        assert "没有这个扩展" in (await r.json())["error"]
        r = await env.client.put("/api/extensions/search", json={"mcp": "tavily", "tool": "没有这工具"})
        assert r.status == 400
        assert "没有这个工具" in (await r.json())["error"]
        r = await env.client.put("/api/extensions/search", json={"tool": "tavily-search"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_binding_cleared_when_extension_deleted(self, env) -> None:
        await _login(env)
        await _add_tavily(env)
        await env.client.put("/api/extensions/search", json={"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""})
        r = await env.client.delete("/api/extensions/mcp/tavily")
        assert r.status == 200
        data = await (await env.client.get("/api/extensions/search")).json()
        assert data["binding"] is None
        assert env.app.search.available() is False

    @pytest.mark.asyncio
    async def test_binding_cleared_when_extension_renamed(self, env) -> None:
        """改 url 不算改名；改「名字」= 删掉旧的建新的（前端就这么干）——绑定要跟着旧名清掉。"""
        await _login(env)
        await _add_tavily(env)
        await env.client.put("/api/extensions/search", json={"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""})
        # 模拟改名：删旧建新
        await env.client.delete("/api/extensions/mcp/tavily")
        r = await env.client.post("/api/extensions/mcp", json={"name": "tavily2", "url": MCP_URL})
        assert r.status == 200
        data = await (await env.client.get("/api/extensions/search")).json()
        assert data["binding"] is None

    @pytest.mark.asyncio
    async def test_health_and_onboarding_reflect_binding(self, env) -> None:
        await _login(env)
        # 没绑定：健康状态 warn
        r = await env.client.get("/api/settings")
        states = {h["key"]: h for h in (await r.json())["health"]}
        assert states["search"]["state"] == "warn"
        onb = await (await env.client.get("/api/onboarding")).json()
        assert onb["checks"]["search"] is False
        # 绑上：ok + 文案带扩展名和工具名
        await _add_tavily(env)
        await env.client.put("/api/extensions/search", json={"mcp": "tavily", "tool": "tavily-search", "extract_tool": "tavily-extract"})
        r = await env.client.get("/api/settings")
        states = {h["key"]: h for h in (await r.json())["health"]}
        assert states["search"]["state"] == "ok"
        assert "tavily" in states["search"]["text"] and "tavily-search" in states["search"]["text"]
        onb = await (await env.client.get("/api/onboarding")).json()
        assert onb["checks"]["search"] is True
