"""自定义专岗 API 测试（阶段 4）。

覆盖：
- POST   /api/agents {title}      → 建自定义专岗；返回 profile（含 kind=c_<6 位小写字母数字>）；
                                    初始 SOUL（MaiBot 同步或空）+ AGENTS（custom 预设）就位；
                                    默认小鱼、无模型（调用时回落 main）、enabled=True；
                                    主模型 AGENTS.md 自动追加一行 stub（「新建的专岗」小节里）。
- DELETE /api/agents/{kind}       → 只许删自定义：内建 kind 400（中文错误）；
                                    有未结交接单 409/400；删掉后身份目录进 trash/.bak（不硬删）；
                                    主模型 AGENTS.md 里那行 stub 若还保持原样会自动去掉。
- GET /api/agents 和 /api/groups/{gid}/agents 都包含自定义专岗。
- PUT /api/agents/<c_xxx> 模型三件套：自定义 kind 也能选模型（对着模型库校验）。
- 不存在的内建名 400/404；匿名 401。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
PASSWORD = "测试密码-很显眼-12345"
KIND_RE = re.compile(r"^c_[0-9a-z]{6}$")


class _Console:
    def __init__(self, password: str = "") -> None:
        self.password = password


class _Settings:
    def __init__(self, password: str = "") -> None:
        self.console = _Console(password)
        self.model_list = ()

    def is_served(self, gid: Any) -> bool:
        return str(gid) == G1


class _FakeSvc:
    def __init__(self, store: Store, settings: _Settings, agents: Any, identity: Any) -> None:
        self.store = store
        self._settings = settings
        self.agents = agents
        self.identity = identity

    def get_settings(self) -> Any:
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
    e.tmp_path = tmp_path
    e.client = client

    async def login(password: str = PASSWORD):
        return await client.post("/api/login", json={"password": password})

    e.login = login
    yield e
    await client.close()
    store.close()


# ----------------------------------------------------------------------
# POST 建自定义专岗
# ----------------------------------------------------------------------


class TestCreateCustom:
    @pytest.mark.asyncio
    async def test_create_requires_admin(self, env) -> None:
        r = await env.client.post("/api/agents", json={"title": "表格员"})
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_create_returns_profile_with_kind(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "表格员"})
        assert r.status == 200
        p = await r.json()
        assert KIND_RE.match(p["kind"]), p["kind"]
        assert p["title"] == "表格员"
        assert p["enabled"] is True
        assert p["fish_seed"] == ""     # 默认小鱼
        assert p["model"] == ""         # 没选模型（调用时回落 main）
        assert p["effort"] == "" and p["backup"] == ""
        assert isinstance(p["skills"], list) or p["skills"] is None

    @pytest.mark.asyncio
    async def test_create_shows_in_get_agents(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "翻译官"})
        kind = (await r.json())["kind"]
        r2 = await env.client.get("/api/agents")
        kinds = [p["kind"] for p in (await r2.json())["profiles"]]
        assert kind in kinds
        # 内建也都还在
        for k in ("main", "news", "idea", "goal", "task"):
            assert k in kinds

    @pytest.mark.asyncio
    async def test_create_initializes_docs(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "翻译官"})
        kind = (await r.json())["kind"]
        # docs 就位（SOUL 是 MaiBot 同步或空，AGENTS 是 custom 预设）
        r2 = await env.client.get(f"/api/agents/{kind}/docs")
        assert r2.status == 200
        data = await r2.json()
        assert "text" in data["soul"]
        assert data["agents"]["text"]  # custom 预设非空
        # 文件真的落盘
        d = Path(env.identity._root) / "agents" / kind  # noqa: SLF001
        assert (d / "SOUL.md").is_file() and (d / "AGENTS.md").is_file()

    @pytest.mark.asyncio
    async def test_create_appends_stub_to_main_agents(self, env) -> None:
        await env.login()
        before = env.identity.agent_read("main", "agents")["text"]
        r = await env.client.post("/api/agents", json={"title": "画图师"})
        kind = (await r.json())["kind"]
        after = env.identity.agent_read("main", "agents")["text"]
        assert len(after) > len(before)
        # 自动加的 stub 行带标题和 kind
        assert "画图师" in after and kind in after

    @pytest.mark.asyncio
    async def test_create_title_required(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={})
        assert r.status == 400
        r = await env.client.post("/api/agents", json={"title": "   "})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_create_title_too_long(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "长" * 100})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_create_kind_unique(self, env) -> None:
        await env.login()
        r1 = await env.client.post("/api/agents", json={"title": "甲"})
        r2 = await env.client.post("/api/agents", json={"title": "乙"})
        k1 = (await r1.json())["kind"]
        k2 = (await r2.json())["kind"]
        assert k1 != k2


# ----------------------------------------------------------------------
# 自定义专岗出现在群快照里
# ----------------------------------------------------------------------


class TestCustomInGroupView:
    @pytest.mark.asyncio
    async def test_group_agents_includes_custom(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "查票员"})
        kind = (await r.json())["kind"]
        r2 = await env.client.get(f"/api/groups/{G1}/agents")
        assert r2.status == 200
        kinds = [a["kind"] for a in (await r2.json())["agents"]]
        assert kind in kinds


# ----------------------------------------------------------------------
# 自定义专岗能选模型（模型三件套校验）
# ----------------------------------------------------------------------


class TestCustomModelFields:
    @pytest.mark.asyncio
    async def test_custom_can_pick_model(self, env) -> None:
        env.settings.model_list = load_settings(
            {
                "endpoints": [{"id": "default", "base_url": "https://api.test/v1", "api_key": "sk-x"}],
                "model_list": [
                    {"id": "m1", "endpoint": "default", "model": "gpt-fast", "efforts": ["low", "high"]},
                ],
            }
        )[0].model_list
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "写稿员"})
        kind = (await r.json())["kind"]
        r2 = await env.client.put(f"/api/agents/{kind}", json={"model": "m1", "effort": "high"})
        assert r2.status == 200
        data = await r2.json()
        assert data["model"] == "m1" and data["effort"] == "high"
        # 不存在的 effort → 400
        r3 = await env.client.put(f"/api/agents/{kind}", json={"effort": "max"})
        assert r3.status == 400


# ----------------------------------------------------------------------
# DELETE 删自定义专岗
# ----------------------------------------------------------------------


class TestDeleteCustom:
    @pytest.mark.asyncio
    async def test_delete_builtin_rejected_400_chinese(self, env) -> None:
        await env.login()
        for kind in ("main", "news", "idea", "goal", "task"):
            r = await env.client.delete(f"/api/agents/{kind}")
            assert r.status == 400, kind
            data = await r.json()
            assert "内置" in (data.get("error") or "") or "不能删" in (data.get("error") or "")

    @pytest.mark.asyncio
    async def test_delete_unknown_kind_404(self, env) -> None:
        await env.login()
        r = await env.client.delete("/api/agents/c_404xxx")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_delete_removes_profile(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "临时"})
        kind = (await r.json())["kind"]
        r2 = await env.client.delete(f"/api/agents/{kind}")
        assert r2.status == 200
        # profiles 里没了
        r3 = await env.client.get("/api/agents")
        kinds = [p["kind"] for p in (await r3.json())["profiles"]]
        assert kind not in kinds

    @pytest.mark.asyncio
    async def test_delete_removes_stub_from_main_agents(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "临时工"})
        kind = (await r.json())["kind"]
        assert kind in env.identity.agent_read("main", "agents")["text"]
        await env.client.delete(f"/api/agents/{kind}")
        # stub 还是原来的样子（管理员没改过）→ 自动删掉
        assert kind not in env.identity.agent_read("main", "agents")["text"]

    @pytest.mark.asyncio
    async def test_delete_moves_docs_to_trash(self, env) -> None:
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "快照员"})
        kind = (await r.json())["kind"]
        d = Path(env.identity._root) / "agents" / kind  # noqa: SLF001
        assert d.is_dir()
        await env.client.delete(f"/api/agents/{kind}")
        # 原目录不在了，但不是硬删（进了 trash/.bak 或改名目录）
        assert not d.is_dir() or len(list(d.iterdir())) == 0
        trash = Path(env.identity._root) / "agents" / ".trash"  # noqa: SLF001
        # 至少存在「回收站」目录或重命名目录里含原 SOUL/AGENTS
        moved = list(Path(env.identity._root).glob(f"agents/.trash/**/{kind}*"))  # noqa: SLF001
        assert trash.is_dir() or moved

    @pytest.mark.asyncio
    async def test_delete_with_running_handoff_rejected(self, env) -> None:
        """有未结（queued/running/returned）交接单的自定义专岗不许删，返回中文错误。"""
        await env.login()
        r = await env.client.post("/api/agents", json={"title": "跑着的"})
        kind = (await r.json())["kind"]
        # 落一个未结的交接单（queued）
        env.agents.begin(G1, kind, "干点啥", task_id="", phase="")
        r2 = await env.client.delete(f"/api/agents/{kind}")
        assert r2.status in (400, 409)
        data = await r2.json()
        assert "交接" in (data.get("error") or "") or "进行中" in (data.get("error") or "")

    @pytest.mark.asyncio
    async def test_delete_requires_admin(self, env) -> None:
        """匿名直接 DELETE 内建 / 自定义 都 401（走 admin 守卫，和改 profile 一样）。"""
        r = await env.client.delete("/api/agents/news")
        assert r.status == 401
