"""专岗 API 测试（契约 A 的 server.py 部分）：/api/agents* 与 /api/groups/{gid}/agents*。

起真 aiohttp 服务器；svc 用轻量假对象（真实 Store + 真 Agents），登录走真 ConsoleAuth
（config 密码路径：settings.console.password）。

覆盖：
- /api/agents GET/PUT 只给总管理员（匿名 401）。
- PUT 严格：未知字段 400、布尔严格 400、超长 400、未知 kind 400；同源 guard（跨源 403）。
- /api/groups/{gid}/agents GET：管理员或本群 group_admin；成员 403，匿名 401，非服务群 404。
- GET 快照含四种岗位（task 也在：notes='' / learned=[] / 只有 recent_handoffs）。
- PUT memory notes：news 能写（返回 memory），task 拒绝 400；超长 / 非字符串 400。
- handoffs 列表：kind 过滤 / 有界 / 跨群不行。
- svc.agents 缺位 → 503。
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
PASSWORD = "测试密码-很显眼-12345"


class _Console:
    def __init__(self, password: str = ""):
        self.password = password


class _Settings:
    def __init__(self, served=(G1,), password: str = ""):
        self.served = set(served)
        self.console = _Console(password)

    def is_served(self, gid):
        return str(gid) in self.served


class _FakeSvc:
    """ConsoleServer 需要的最小 svc：store / get_settings / agents（可摘除）。"""

    def __init__(self, store: Store, settings: _Settings, agents) -> None:
        self.store = store
        self._settings = settings
        self.agents = agents

    def get_settings(self):
        return self._settings


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    # 密码写在 settings.console.password（ConsoleAuth 认 config 密码的路径）
    settings = _Settings(served=(G1, G2), password=PASSWORD)
    agents = Agents(store, lambda: settings)
    svc = _FakeSvc(store, settings, agents)

    server = ConsoleServer(svc)
    test_server = TestServer(server.app)
    client = TestClient(test_server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    base = f"http://127.0.0.1:{test_server.port}"

    class Env:
        pass

    e = Env()
    e.store = store
    e.settings = settings
    e.agents = agents
    e.svc = svc
    e.client = client
    e.base = base

    async def login(password: str = PASSWORD):
        return await client.post("/api/login", json={"password": password})

    e.login = login
    yield e
    await client.close()
    store.close()


# ----------------------------------------------------------------------
# /api/agents（全局岗位配置，只总管理员）
# ----------------------------------------------------------------------


class TestGlobalAgentsApi:
    @pytest.mark.asyncio
    async def test_get_requires_admin(self, env) -> None:
        r = await env.client.get("/api/agents")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_get_profiles(self, env) -> None:
        await env.login()
        r = await env.client.get("/api/agents")
        assert r.status == 200
        data = await r.json()
        assert "profiles" in data
        assert [p["kind"] for p in data["profiles"]] == ["news", "idea", "goal", "task"]

    @pytest.mark.asyncio
    async def test_put_profile(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news", json={"title": "资讯小队", "enabled": False})
        assert r.status == 200
        data = await r.json()
        assert data["title"] == "资讯小队"
        assert data["enabled"] is False

    @pytest.mark.asyncio
    async def test_get_profiles_default_titles_and_fish_seed(self, env) -> None:
        await env.login()
        r = await env.client.get("/api/agents")
        assert r.status == 200
        data = await r.json()
        by_kind = {p["kind"]: p for p in data["profiles"]}
        assert by_kind["news"]["title"] == "资讯"
        assert by_kind["idea"]["title"] == "构想"
        assert by_kind["goal"]["title"] == "目标"
        assert by_kind["task"]["title"] == "通用任务"
        for p in data["profiles"]:
            assert p["fish_seed"] == ""

    @pytest.mark.asyncio
    async def test_put_fish_seed_roundtrip(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news", json={"fish_seed": "koi-01"})
        assert r.status == 200
        data = await r.json()
        assert data["fish_seed"] == "koi-01"
        r = await env.client.get("/api/agents")
        data = await r.json()
        by_kind = {p["kind"]: p for p in data["profiles"]}
        assert by_kind["news"]["fish_seed"] == "koi-01"
        # task 也能改
        r = await env.client.put("/api/agents/task", json={"fish_seed": "task_fish"})
        assert r.status == 200
        data = await r.json()
        assert data["fish_seed"] == "task_fish"
        assert data["title"] == "通用任务"  # 其它字段没被动到

    @pytest.mark.asyncio
    async def test_put_fish_seed_rejects_invalid(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news", json={"fish_seed": "a" * 33})
        assert r.status == 400
        r = await env.client.put("/api/agents/news", json={"fish_seed": "a b"})
        assert r.status == 400
        r = await env.client.put("/api/agents/news", json={"fish_seed": "鱼"})
        assert r.status == 400
        r = await env.client.put("/api/agents/news", json={"fish_seed": 123})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_unknown_field(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news", json={"tools": ["write_file"]})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_bad_bool(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news", json={"enabled": "true"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_too_long(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/news", json={"title": "x" * 41})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_unknown_kind_404_or_400(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/agents/ghost", json={"title": "x"})
        assert r.status in (400, 404)

    @pytest.mark.asyncio
    async def test_put_origin_guard(self, env) -> None:
        await env.login()
        host = env.base.split("://", 1)[1]
        r = await env.client.put(
            "/api/agents/news",
            json={"title": "ok"},
            headers={"Origin": "http://evil.example", "Host": host},
        )
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_put_same_origin_ok(self, env) -> None:
        await env.login()
        host = env.base.split("://", 1)[1]
        r = await env.client.put(
            "/api/agents/news",
            json={"title": "同源"},
            headers={"Origin": f"http://{host}", "Host": host},
        )
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_missing_agents_module_503(self, env) -> None:
        env.svc.agents = None
        await env.login()
        r = await env.client.get("/api/agents")
        assert r.status == 503


# ----------------------------------------------------------------------
# /api/groups/{gid}/agents（本群岗位快照）
# ----------------------------------------------------------------------


class TestGroupAgentsApi:
    @pytest.mark.asyncio
    async def test_anonymous_401(self, env) -> None:
        r = await env.client.get(f"/api/groups/{G1}/agents")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_unknown_group_404(self, env) -> None:
        await env.login()
        r = await env.client.get("/api/groups/999999/agents")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_get_snapshot_four_kinds(self, env) -> None:
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/agents")
        assert r.status == 200
        data = await r.json()
        assert data["group_id"] == G1
        kinds = [a["kind"] for a in data["agents"]]
        assert kinds == ["news", "idea", "goal", "task"]
        for a in data["agents"]:
            assert "kind" in a and "title" in a
            assert "notes" in a and "learned" in a and "recent_handoffs" in a
        task = next(a for a in data["agents"] if a["kind"] == "task")
        assert task["notes"] == ""
        assert task["learned"] == []

    @pytest.mark.asyncio
    async def test_get_cross_group_with_admin_ok(self, env) -> None:
        """总管理员看任何服务群都可以。"""
        await env.login()
        r = await env.client.get(f"/api/groups/{G2}/agents")
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_member_token_403(self, env) -> None:
        """群友（X-MW-Group 链接码）不够格。"""
        import secrets as _s

        from CharTyr_MaiWork.maiwork import clock
        token = _s.token_urlsafe(8)
        with env.store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, token, created) VALUES (?, ?, ?)",
                (G1, token, clock.now()),
            )
        r = await env.client.get(f"/api/groups/{G1}/agents", headers={"X-MW-Group": token})
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_missing_agents_module_503(self, env) -> None:
        env.svc.agents = None
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/agents")
        assert r.status == 503


class TestGroupMemoryApi:
    @pytest.mark.asyncio
    async def test_put_notes(self, env) -> None:
        await env.login()
        r = await env.client.put(f"/api/groups/{G1}/agents/news/memory", json={"notes": "爱看硬件"})
        assert r.status == 200
        data = await r.json()
        assert data["notes"] == "爱看硬件"
        # 真的写进去了
        assert env.agents.memory(G1, "news")["notes"] == "爱看硬件"

    @pytest.mark.asyncio
    async def test_put_notes_task_rejected(self, env) -> None:
        """task 岗位没记忆可写。"""
        await env.login()
        r = await env.client.put(f"/api/groups/{G1}/agents/task/memory", json={"notes": "x"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_notes_too_long(self, env) -> None:
        await env.login()
        r = await env.client.put(f"/api/groups/{G1}/agents/news/memory", json={"notes": "x" * 2001})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_notes_non_string(self, env) -> None:
        await env.login()
        r = await env.client.put(f"/api/groups/{G1}/agents/news/memory", json={"notes": 123})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_notes_unknown_group_404(self, env) -> None:
        await env.login()
        r = await env.client.put("/api/groups/999999/agents/news/memory", json={"notes": "x"})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_put_notes_anonymous_401(self, env) -> None:
        r = await env.client.put(f"/api/groups/{G1}/agents/news/memory", json={"notes": "x"})
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_put_notes_origin_guard(self, env) -> None:
        await env.login()
        host = env.base.split("://", 1)[1]
        r = await env.client.put(
            f"/api/groups/{G1}/agents/news/memory",
            json={"notes": "ok"},
            headers={"Origin": "http://evil.example", "Host": host},
        )
        assert r.status == 403


class TestGroupHandoffsApi:
    async def _mk_handoffs(self, env) -> list[str]:
        ids = []
        ids.append(env.agents.begin(G1, "news", "news-b1"))
        ids.append(env.agents.begin(G1, "news", "news-b2"))
        ids.append(env.agents.begin(G1, "idea", "idea-b1"))
        env.agents.begin(G2, "news", "g2-b1")
        return ids

    @pytest.mark.asyncio
    async def test_handoffs_list(self, env) -> None:
        await self._mk_handoffs(env)
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/agents/handoffs")
        assert r.status == 200
        data = await r.json()
        assert "items" in data
        assert len(data["items"]) == 3  # 只本群

    @pytest.mark.asyncio
    async def test_handoffs_kind_filter(self, env) -> None:
        await self._mk_handoffs(env)
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/agents/handoffs?kind=news")
        assert r.status == 200
        data = await r.json()
        assert len(data["items"]) == 2
        assert all(i["kind"] == "news" for i in data["items"])

    @pytest.mark.asyncio
    async def test_handoffs_fields(self, env) -> None:
        await self._mk_handoffs(env)
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/agents/handoffs")
        data = await r.json()
        item = data["items"][0]
        for f in ("id", "group_id", "kind", "task_id", "phase", "parent_id",
                  "status", "brief", "created", "updated"):
            assert f in item, f"缺字段 {f}"

    @pytest.mark.asyncio
    async def test_handoffs_anonymous_401(self, env) -> None:
        r = await env.client.get(f"/api/groups/{G1}/agents/handoffs")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_handoffs_unknown_group_404(self, env) -> None:
        await env.login()
        r = await env.client.get("/api/groups/999999/agents/handoffs")
        assert r.status == 404
