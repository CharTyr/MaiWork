"""console API 认知修正（implement.md 第 9 步）：agents 工具视图 + confirm_exec 门。

- GET /api/agents：profile 带 tools_view；顶层 worker_tool_choices（特性位齐）；
- POST/PUT /api/extensions/skills、upload、PUT /api/agents：做法带执行类工具时
  不带 confirm_exec → 400；带了 → 放行。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.group_admins import GroupAdmins
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools

PASSWORD = "控制台测试密码-显眼-97531"
G1 = "900000001"


class _Settings:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.served = {G1}
        self.groups = {G1: {}}
        self.console = SimpleNamespace(password=PASSWORD)

    def is_served(self, gid):
        return str(gid) in self.served


class _FakeAgents:
    def __init__(self):
        self._profiles = {
            "news": {"kind": "news", "title": "资讯", "skills": [], "tools": None, "enabled": True},
            "task": {"kind": "task", "title": "任务", "skills": None, "tools": None, "enabled": True},
        }

    def profiles(self):
        return [dict(p) for p in self._profiles.values()]

    def profile(self, kind):
        return dict(self._profiles[kind])

    def update_profile(self, kind, body):
        # 和真 Agents 一样：不认识的键一律拒（confirm_exec 必须由 server 先剥掉）
        allowed = {"title", "instructions", "skills", "enabled", "fish_seed",
                   "model", "effort", "backup", "escalate", "tools"}
        bad = set(body) - allowed
        if bad:
            raise ValueError(f"不认识的键：{'、'.join(sorted(bad))}")
        self._profiles[kind].update({k: v for k, v in body.items() if k in allowed})
        return dict(self._profiles[kind])


class _FakeSvc:
    def __init__(self, store, settings, data_dir):
        self.store = store
        self._settings = settings
        self.data_dir = data_dir
        self.group_admins = GroupAdmins(store, get_settings=lambda: settings)
        self.agents = _FakeAgents()
        self.specialists = None
        self.tools = Tools(store)

        async def fake(ctx, args):
            return ToolResult(ok=True, output="x")

        def reg(name, **kw):
            self.tools.register(Tool(name=name, description=name + " 说明",
                                     parameters={"type": "object", "properties": {}},
                                     roles=frozenset({"worker"}), handler=fake, **kw))

        reg("fetch_page")
        reg("run_command", needs_workspace=True, exec_class=True)
        reg("generate_image", needs_workspace=True, exec_class=True, task_only=True)
        reg("submit_result")

    def get_settings(self):
        return self._settings


@pytest_asyncio.fixture
async def env(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    (tmp_path / "skills").mkdir()
    settings = _Settings(tmp_path)
    svc = _FakeSvc(store, settings, tmp_path)
    server = ConsoleServer(svc)
    client = TestClient(TestServer(server.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200
    e = SimpleNamespace(client=client, svc=svc, store=store, tmp=tmp_path)
    yield e
    await client.close()
    store.close()


def _zip_with_skill(name: str, allowed: str = "") -> bytes:
    at = f"allowed-tools: {allowed}\n" if allowed else ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md",
                    f"---\nname: {name}\ndescription: d\n---\n\nbody\n" if not at else
                    f"---\nname: {name}\ndescription: d\n{at}---\n\nbody\n")
    return buf.getvalue()


class TestAgentsView:
    @pytest.mark.asyncio
    async def test_worker_tool_choices_and_tools_view(self, env):
        r = await env.client.get("/api/agents")
        assert r.status == 200
        body = await r.json()
        choices = {c["name"]: c for c in body["worker_tool_choices"]}
        assert "submit_result" not in choices
        assert choices["run_command"]["exec_class"] is True
        assert choices["run_command"]["needs_workspace"] is True
        assert choices["generate_image"]["task_only"] is True
        assert choices["fetch_page"]["exec_class"] is False
        assert choices["fetch_page"]["label"]
        # 没接 specialists 时 tools_view 省略
        assert all("tools_view" not in p for p in body["profiles"])


class TestSkillConfirmGate:
    @pytest.mark.asyncio
    async def test_create_exec_requires_confirm(self, env):
        r = await env.client.post("/api/extensions/skills", json={
            "name": "s-exec", "description": "d", "body": "b",
            "allowed_tools": ["run_command", "fetch_page"]})
        assert r.status == 400
        assert "run_command" in (await r.json())["error"]
        r = await env.client.post("/api/extensions/skills", json={
            "name": "s-exec", "description": "d", "body": "b",
            "allowed_tools": ["run_command", "fetch_page"], "confirm_exec": True})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_create_exec_string_form_requires_confirm(self, env):
        """空格分隔字符串写法：执行类工具也要 confirm_exec（安全旁路回归）。"""
        r = await env.client.post("/api/extensions/skills", json={
            "name": "s-str", "description": "d", "body": "b",
            "allowed_tools": "fetch_page run_command"})
        assert r.status == 400
        r = await env.client.post("/api/extensions/skills", json={
            "name": "s-str", "description": "d", "body": "b",
            "allowed_tools": "fetch_page run_command", "confirm_exec": True})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_update_exec_string_form_requires_confirm(self, env):
        await env.client.post("/api/extensions/skills", json={
            "name": "s-upd-str", "description": "d", "body": "b",
            "allowed_tools": "fetch_page"})
        r = await env.client.put("/api/extensions/skills/s-upd-str", json={
            "allowed_tools": "fetch_page run_command"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_create_readonly_direct(self, env):
        r = await env.client.post("/api/extensions/skills", json={
            "name": "s-ro", "description": "d", "body": "b",
            "allowed_tools": ["fetch_page"]})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_update_exec_requires_confirm(self, env):
        await env.client.post("/api/extensions/skills", json={
            "name": "s-upd", "description": "d", "body": "b",
            "allowed_tools": ["fetch_page"]})
        r = await env.client.put("/api/extensions/skills/s-upd", json={
            "allowed_tools": ["fetch_page", "run_command"]})
        assert r.status == 400
        r = await env.client.put("/api/extensions/skills/s-upd", json={
            "allowed_tools": ["fetch_page", "run_command"], "confirm_exec": True})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_upload_exec_requires_confirm(self, env):
        blob = _zip_with_skill("s-zip", allowed="run_command")
        r = await env.client.post("/api/extensions/skills/upload", data=blob,
                                  headers={"Content-Type": "application/zip"})
        assert r.status == 400
        assert "run_command" in (await r.json())["error"]
        r = await env.client.post("/api/extensions/skills/upload?confirm_exec=1", data=blob,
                                  headers={"Content-Type": "application/zip"})
        assert r.status == 200
        view = await r.json()
        assert view.get("allowed_tools") == ["run_command"]

    @pytest.mark.asyncio
    async def test_upload_readonly_direct(self, env):
        blob = _zip_with_skill("s-zip-ro", allowed="")
        r = await env.client.post("/api/extensions/skills/upload", data=blob,
                                  headers={"Content-Type": "application/zip"})
        assert r.status == 200


class TestAgentSkillsGate:
    @pytest.mark.asyncio
    async def test_put_skills_exec_requires_confirm(self, env):
        await env.client.post("/api/extensions/skills", json={
            "name": "g-exec", "description": "d", "body": "b",
            "allowed_tools": ["run_command"], "confirm_exec": True})
        r = await env.client.put("/api/agents/news", json={"skills": ["g-exec"]})
        assert r.status == 400
        r = await env.client.put("/api/agents/news",
                                 json={"skills": ["g-exec"], "confirm_exec": True})
        assert r.status == 200
        assert (await r.json())["skills"] == ["g-exec"]

    @pytest.mark.asyncio
    async def test_put_skills_task_no_gate(self, env):
        # task 不受做法工具影响 → 挂执行类做法也不用 confirm_exec
        r = await env.client.put("/api/agents/task", json={"skills": ["g-exec"]})
        assert r.status == 200
