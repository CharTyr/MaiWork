"""专岗 SOUL/AGENTS 文档 API 测试（阶段 3）。

起真 aiohttp 服务器；svc 用轻量假对象（真实 Store + 真 Agents + 真 Identity）。
覆盖阶段 3 的全部 HTTP 形状（前端 static/js/settings/agents.js 已经按这些路由写的）：

- GET    /api/agents/{kind}/docs            → {"soul","agents","limits"}（管理员）
- PUT    /api/agents/{kind}/docs/soul       {"text"} → 同 GET 单项
- PUT    /api/agents/{kind}/docs/agents     {"text"} → 同 GET 单项
- POST   /api/agents/{kind}/docs/soul/sync  → 从 MaiBot 重同步（旧版存 .bak）
- POST   /api/agents/{kind}/docs/agents/reset → 换回预设
- 未知 kind 一律 404；匿名 401；超限 400
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
PASSWORD = "测试密码-很显眼-12345"


class _Console:
    def __init__(self, password: str = ""):
        self.password = password


class _Settings:
    def __init__(self, password: str = ""):
        self.console = _Console(password)
        self.model_list = ()

    def is_served(self, gid):
        return str(gid) == G1


class _FakeSvc:
    """ConsoleServer 需要的最小 svc：store / get_settings / agents / identity。"""

    def __init__(self, store: Store, settings: _Settings, agents, identity) -> None:
        self.store = store
        self._settings = settings
        self.agents = agents
        self.identity = identity

    def get_settings(self):
        return self._settings


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _Settings(password=PASSWORD)
    agents = Agents(store, lambda: settings)
    identity = Identity(tmp_path / "data", store, lambda: settings, host=None)
    await identity.ensure_started()
    svc = _FakeSvc(store, settings, agents, identity)

    server = ConsoleServer(svc)
    test_server = TestServer(server.app)
    client = TestClient(test_server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()

    class Env:
        pass

    e = Env()
    e.store = store
    e.settings = settings
    e.agents = agents
    e.identity = identity
    e.client = client

    async def login(password: str = PASSWORD):
        return await client.post("/api/login", json={"password": password})

    e.login = login
    yield e
    await client.close()
    store.close()


# ----------------------------------------------------------------------
# GET docs
# ----------------------------------------------------------------------


class TestGetDocs:
    @pytest.mark.asyncio
    async def test_get_requires_admin(self, env) -> None:
        r = await env.client.get("/api/agents/news/docs")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_get_docs_shape(self, env) -> None:
        await env.login()
        r = await env.client.get("/api/agents/news/docs")
        assert r.status == 200
        data = await r.json()
        assert set(data.keys()) >= {"soul", "agents", "limits"}
        assert "text" in data["soul"] and "updated_ts" in data["soul"]
        assert "synced_from_maibot" in data["soul"]
        assert "text" in data["agents"] and "updated_ts" in data["agents"]
        assert data["limits"] == {"soul": 16384, "agents": 16384}

    @pytest.mark.asyncio
    async def test_get_docs_each_builtin_kind(self, env) -> None:
        """五种内建 kind 都能拿到 docs（首次启动已经把文件就位）。"""
        await env.login()
        for kind in ("main", "news", "idea", "goal", "task"):
            r = await env.client.get(f"/api/agents/{kind}/docs")
            assert r.status == 200, kind

    @pytest.mark.asyncio
    async def test_get_docs_unknown_kind_404(self, env) -> None:
        await env.login()
        r = await env.client.get("/api/agents/ghost/docs")
        assert r.status == 404


# ----------------------------------------------------------------------
# PUT docs/soul 和 docs/agents
# ----------------------------------------------------------------------


class TestPutDocs:
    @pytest.mark.asyncio
    async def test_put_soul_saves_and_returns_shape(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news/docs/soul", json={"text": "news 的新人格"})
        assert r.status == 200
        data = await r.json()
        assert data["text"] == "news 的新人格"
        assert data["synced_from_maibot"] is False  # 手动改过
        # 回读一见
        r2 = await env.client.get("/api/agents/news/docs")
        assert (await r2.json())["soul"]["text"] == "news 的新人格"

    @pytest.mark.asyncio
    async def test_put_agents_saves(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/goal/docs/agents", json={"text": "我改的 goal 规矩"})
        assert r.status == 200
        data = await r.json()
        assert data["text"] == "我改的 goal 规矩"
        assert "synced_from_maibot" not in data  # agents 不带这个键

    @pytest.mark.asyncio
    async def test_put_over_limit_400(self, env) -> None:
        await env.login()
        big = "长" * 20000  # 超 16KB
        r = await env.client.put("/api/agents/news/docs/soul", json={"text": big})
        assert r.status == 400
        r = await env.client.put("/api/agents/news/docs/agents", json={"text": big})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_unknown_kind_404(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/ghost/docs/soul", json={"text": "x"})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_put_requires_admin(self, env) -> None:
        r = await env.client.put("/api/agents/news/docs/soul", json={"text": "x"})
        assert r.status == 401


# ----------------------------------------------------------------------
# POST soul/sync 和 agents/reset
# ----------------------------------------------------------------------


class TestSyncAndReset:
    @pytest.mark.asyncio
    async def test_soul_sync_empty_persona_returns_empty(self, env) -> None:
        """MaiBot 没人格（host=None）：同步后 SOUL 是空的，不报错。"""
        await env.login()
        # 先塞点旧内容
        await env.client.put("/api/agents/news/docs/soul", json={"text": "旧版"})
        r = await env.client.post("/api/agents/news/docs/soul/sync", json={})
        assert r.status == 200
        data = await r.json()
        # host=None → 渲染出的就是空模板人格部分的兜底版，含「# 我是谁」结构
        assert isinstance(data["text"], str)
        assert data["synced_from_maibot"] is True

    @pytest.mark.asyncio
    async def test_soul_sync_saves_bak(self, env) -> None:
        await env.login()
        await env.client.put("/api/agents/news/docs/soul", json={"text": "我改过的版本"})
        r = await env.client.post("/api/agents/news/docs/soul/sync", json={})
        assert r.status == 200
        # 备份文件落盘了
        bak = Path(env.identity._root) / "agents" / "news" / "SOUL.md.bak"  # noqa: SLF001
        assert bak.is_file()
        assert "我改过的版本" in bak.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_agents_reset_restores_preset(self, env) -> None:
        await env.login()
        await env.client.put("/api/agents/task/docs/agents", json={"text": "我乱改的"})
        r = await env.client.post("/api/agents/task/docs/agents/reset", json={})
        assert r.status == 200
        data = await r.json()
        preset = (Path(__file__).resolve().parents[1] / "maiwork" / "agent_presets" / "task.md").read_text(encoding="utf-8")
        assert data["text"].strip() == preset.strip()

    @pytest.mark.asyncio
    async def test_reset_unknown_kind_404(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents/ghost/docs/agents/reset", json={})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_sync_requires_admin(self, env) -> None:
        r = await env.client.post("/api/agents/news/docs/soul/sync", json={})
        assert r.status == 401


# ----------------------------------------------------------------------
# 向后兼容：旧的全局 SOUL/AGENTS 路由仍可用（别名到 main 的 docs）
# ----------------------------------------------------------------------


class TestLegacyAliases:
    @pytest.mark.asyncio
    async def test_old_identity_get_still_works(self, env) -> None:
        """老前端（记忆页除外）读的 /api/identity：soul/agents 返回 main 的，memory 照常。"""
        await env.login()
        r = await env.client.get("/api/identity")
        assert r.status == 200
        data = await r.json()
        # soul / agents 落的是 main 的文档
        assert data["soul"]["text"] == env.identity.agent_read("main", "soul")["text"]
        assert data["agents"]["text"] == env.identity.agent_read("main", "agents")["text"]
        # memory / group_memory 不动
        assert "memory" in data and "group_memory" in data
