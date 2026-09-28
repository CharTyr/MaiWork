"""扩展（skill + MCP）测试（docs/02 §10、docs/07 §10.9）。

- MCP：[[extensions.mcp]] 规范化、启动连接（MockTransport，不碰真实网络）、
  工具注册命名 mcp_<name>_<工具名>（白名单、roles、inputSchema 透传）、
  调用映射（content 的 text 拼接、截 8000、isError → ok=False）、
  headers 密钥不进 tool_calls / 日志 / settings 响应、连接失败不影响启动、
  reload 接口（管理员；群友 403、匿名 401）。
- skill：<data_dir>/skills/<名>/SKILL.md（极简 front matter 解析，不用 yaml 库）、
  list/read/read_file（越界、符号链接拒绝）、list_skills / read_skill 工具、
  Workers 的 skills_hint（构造函数或 run 参数注入 system 提示，最多 20 条）。
- 红线：不给 MaiBot 的 planner 注册任何组件（plugin.get_components 没有新增）。

密钥一律假值，HTTP 一律 httpx.MockTransport。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.config import CONFIG_VERSION, load_settings
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tools import Tool, ToolContext, ToolResult, Tools

G1 = "900000001"
MCP_URL = "https://mcp-ext.example/mcp"
HEADER_SECRET = "ext-header-SECRET-AaBb123"  # ASCII：httpx 请求头只收 ASCII，真实密钥也都是 ASCII

# ----------------------------------------------------------------------
# MCP 端点桩（按 docs/06「Tavily MCP 端点」实测格式）
# ----------------------------------------------------------------------


def _sse(payload: dict, session: str = "") -> httpx.Response:
    body = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    headers = {"content-type": "text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    return httpx.Response(200, text=body, headers=headers)


class _McpServer:
    """可编程 MCP 端点：initialize/initialized 自动回；tools/list 给 preset；tools/call 走回调。"""

    def __init__(self, tools: list[dict], on_call=None, *, session: str = "sess-ext") -> None:
        self.requests: list[dict] = []
        self._tools = tools
        self._on_call = on_call
        self._session = session
        self.init_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.requests.append({"method": body.get("method"), "headers": dict(request.headers), "body": body})
        method = body.get("method")
        if method == "initialize":
            self.init_count += 1
            return _sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "ext-mcp", "version": "1.0"},
                    },
                },
                session=self._session,
            )
        if method == "notifications/initialized":
            return httpx.Response(202, text="")
        if method == "tools/list":
            return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self._tools}})
        if method == "tools/call":
            if self._on_call is None:
                return _sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
                    }
                )
            return self._on_call(request, body)
        return httpx.Response(500, text=f"unknown method {method}")

    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]

    def tool_call_bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests if r["method"] == "tools/call"]


def _tool_spec(name: str, description: str = "", schema: dict | None = None) -> dict:
    return {
        "name": name,
        "description": description or f"{name} 的说明",
        "inputSchema": schema or {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }


def _failing_server(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("连不上（测试桩）")


# ----------------------------------------------------------------------
# config：[[extensions.mcp]] 规范化
# ----------------------------------------------------------------------


class TestMcpConfig:
    def test_version_bumped(self) -> None:
        # 0.3.6：同事加 personal_feeds / personal_per_day；0.3.7：新增 [extensions] 节（[[extensions.mcp]]）；
        # 0.3.8：个人向产出本地升版（personal.py / news_items.target_user_id）
        # 0.3.9：模型重试设置（[models] retries / retry_delay_s）
        assert tuple(int(x) for x in CONFIG_VERSION.split(".")) >= (0, 3, 9)

    def test_absent_section_gives_empty(self) -> None:
        s, problems = load_settings({})
        assert s.extensions.mcp == ()
        assert problems == []

    def test_happy_entry(self) -> None:
        s, problems = load_settings(
            {
                "extensions": {
                    "mcp": [
                        {
                            "name": "gh",
                            "url": MCP_URL,
                            "enabled": True,
                            "headers": {"Authorization": "Bearer x"},
                            "tools": ["get_issue"],
                            "roles": ["main", "worker"],
                        }
                    ]
                }
            }
        )
        assert problems == []
        assert len(s.extensions.mcp) == 1
        e = s.extensions.mcp[0]
        assert e.name == "gh"
        assert e.url == MCP_URL
        assert e.enabled is True
        assert e.headers == {"Authorization": "Bearer x"}
        assert e.tools == ("get_issue",)
        assert e.roles == ("main", "worker")

    def test_defaults(self) -> None:
        s, problems = load_settings({"extensions": {"mcp": [{"name": "a", "url": MCP_URL}]}})
        assert problems == []
        e = s.extensions.mcp[0]
        assert e.enabled is True
        assert e.headers == {}
        assert e.tools == ()
        assert e.roles == ("worker",)

    def test_bad_name_dropped(self) -> None:
        for name in ("", "带点.name", "带空格 name", "超" * 33):
            s, problems = load_settings({"extensions": {"mcp": [{"name": name, "url": MCP_URL}]}})
            assert s.extensions.mcp == ()
            assert any("名字" in p or "name" in p for p in problems), (name, problems)

    def test_non_https_dropped(self) -> None:
        s, problems = load_settings({"extensions": {"mcp": [{"name": "a", "url": "http://mcp.example/mcp"}]}})
        assert s.extensions.mcp == ()
        assert any("https" in p for p in problems)

    def test_empty_url_dropped(self) -> None:
        s, problems = load_settings({"extensions": {"mcp": [{"name": "a"}]}})
        assert s.extensions.mcp == ()

    def test_tools_whitelist_normalized(self) -> None:
        s, _ = load_settings({"extensions": {"mcp": [{"name": "a", "url": MCP_URL, "tools": "not-a-list"}]}})
        assert s.extensions.mcp[0].tools == ()

    def test_roles_filtered_and_defaulted(self) -> None:
        s, _ = load_settings({"extensions": {"mcp": [{"name": "a", "url": MCP_URL, "roles": ["boss", "main"]}]}})
        assert s.extensions.mcp[0].roles == ("main",)
        s2, _ = load_settings({"extensions": {"mcp": [{"name": "a", "url": MCP_URL, "roles": ["boss"]}]}})
        assert s2.extensions.mcp[0].roles == ("worker",)

    def test_entry_not_a_table_dropped(self) -> None:
        s, problems = load_settings({"extensions": {"mcp": ["不是表"]}})
        assert s.extensions.mcp == ()
        assert problems


# ----------------------------------------------------------------------
# Extensions：连接、注册、调用
# ----------------------------------------------------------------------


def _mcp_setting(**over) -> object:
    raw = {
        "extensions": {
            "mcp": [
                {
                    "name": over.pop("name", "gh"),
                    "url": over.pop("url", MCP_URL),
                    "enabled": over.pop("enabled", True),
                    "headers": over.pop("headers", {}),
                    "tools": over.pop("tools", []),
                    "roles": over.pop("roles", ["worker"]),
                }
            ]
        }
    }
    s, problems = load_settings(raw)
    assert problems == [], problems
    return s


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _tools(store: Store) -> Tools:
    return Tools(store)


def _ctx(role: str = "worker") -> ToolContext:
    return ToolContext(group_id=G1, task_id="T-1", actor="子 agent #1", role=role)


class TestMcpRegistration:
    @pytest.mark.asyncio
    async def test_register_and_named(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        server = _McpServer([_tool_spec("get_issue"), _tool_spec("list.repos")])
        s = _mcp_setting()
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            names = [t["function"]["name"] for t in tools.specs("worker")]
            assert "mcp_gh_get_issue" in names
            # 非法字符（点号）替换成 _
            assert "mcp_gh_list_repos" in names
            assert server.methods() == ["initialize", "notifications/initialized", "tools/list"]
            info = ext.info()
            assert info[0]["ok"] is True
            assert info[0]["tools"] == 2
            assert info[0]["error"] == ""
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_whitelist(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        server = _McpServer([_tool_spec("a"), _tool_spec("b"), _tool_spec("c")])
        s = _mcp_setting(tools=["a", "c"])
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            names = [t["function"]["name"] for t in tools.specs("worker")]
            assert names == ["mcp_gh_a", "mcp_gh_c"]
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_roles_and_schema_passthrough(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        server = _McpServer([_tool_spec("deep", description="深工具", schema=schema)])
        s = _mcp_setting(roles=["main"])
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            worker_specs = tools.specs("worker")
            assert worker_specs == []  # worker 不能用
            main_specs = tools.specs("main")
            assert len(main_specs) == 1
            fn = main_specs[0]["function"]
            assert fn["name"] == "mcp_gh_deep"
            assert fn["description"] == "深工具"
            assert fn["parameters"] == schema
            # 角色不符的调用被拒并落库
            res = await tools.call("mcp_gh_deep", {"n": 1}, _ctx(role="worker"))
            assert res.ok is False
            assert "只有" in res.error or "角色" in res.error or "不允许" in res.error
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_disabled_not_connected(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        server = _McpServer([_tool_spec("a")])
        s = _mcp_setting(enabled=False)
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            assert tools.specs("worker") == []
            assert server.requests == []  # 根本没连
            assert ext.info()[0]["enabled"] is False
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_long_tool_name_truncated_to_64(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        long_name = "t" * 100
        server = _McpServer([_tool_spec(long_name)])
        s = _mcp_setting(name="ext")
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            names = [t["function"]["name"] for t in tools.specs("worker")]
            assert len(names) == 1 and len(names[0]) <= 64
        finally:
            await ext.aclose()


class TestMcpCall:
    @pytest.mark.asyncio
    async def test_call_maps_arguments_and_joins_text(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        def on_call(request: httpx.Request, body: dict) -> httpx.Response:
            return _sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [
                            {"type": "text", "text": "第一段"},
                            {"type": "text", "text": "第二段"},
                        ],
                        "isError": False,
                    },
                }
            )

        server = _McpServer(
            [_tool_spec("who", schema={"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]})],
            on_call,
        )
        s = _mcp_setting(headers={"Authorization": f"Bearer {HEADER_SECRET}"})
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            res = await tools.call("mcp_gh_who", {"who": "小明"}, _ctx())
            assert res.ok is True
            assert "第一段" in res.output and "第二段" in res.output
            body = server.tool_call_bodies()[0]
            assert body["params"]["name"] == "who"
            assert body["params"]["arguments"] == {"who": "小明"}
            # 每次调用照常落 tool_calls
            calls = tools.recent_calls(task_id="T-1")
            assert any(c["tool"] == "mcp_gh_who" and c["ok"] for c in calls)
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_output_truncated_8000(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        def on_call(request: httpx.Request, body: dict) -> httpx.Response:
            return _sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "字" * 9000}], "isError": False},
                }
            )

        server = _McpServer([_tool_spec("big")], on_call)
        s = _mcp_setting()
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            res = await tools.call("mcp_gh_big", {"q": "x"}, _ctx())
            assert res.ok is True
            assert len(res.output) <= 8000
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_iserror_result_not_ok(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        def on_call(request: httpx.Request, body: dict) -> httpx.Response:
            return _sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "参数不对"}], "isError": True},
                }
            )

        server = _McpServer([_tool_spec("bad")], on_call)
        s = _mcp_setting()
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            res = await tools.call("mcp_gh_bad", {"q": "x"}, _ctx())
            assert res.ok is False
            assert "参数不对" in (res.error or res.output)
        finally:
            await ext.aclose()

    @pytest.mark.asyncio
    async def test_network_failure_in_call_is_tool_failure(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        server = _McpServer([_tool_spec("x")], lambda r, b: httpx.Response(500, text=f"key={HEADER_SECRET}"))
        s = _mcp_setting(headers={"X-Key": HEADER_SECRET})
        tools = _tools(store)
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            res = await tools.call("mcp_gh_x", {"q": "1"}, _ctx())
            assert res.ok is False
            assert HEADER_SECRET not in res.error
        finally:
            await ext.aclose()


class TestMcpSecretMasking:
    @pytest.mark.asyncio
    async def test_header_secret_masked_everywhere(self, store: Store, caplog: pytest.LogCaptureFixture) -> None:
        """headers 里的密钥：不落 tool_calls、不进日志、不在 extensions.info() 里。"""
        import logging

        from CharTyr_MaiWork.extensions import Extensions

        # 服务端把密钥回显进返回文本（最坏的泄漏情形）
        def on_call(request: httpx.Request, body: dict) -> httpx.Response:
            return _sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": f"回应 {HEADER_SECRET}"}], "isError": False},
                }
            )

        server = _McpServer([_tool_spec("leaky")], on_call)
        s = _mcp_setting(headers={"Authorization": f"Bearer {HEADER_SECRET}"})
        tools = Tools(store, get_known_secrets=lambda: [HEADER_SECRET])
        ext = Extensions(lambda: s, transport=httpx.MockTransport(server))
        with caplog.at_level(logging.DEBUG, logger="maiwork"):
            await ext.start(tools)
            try:
                res = await tools.call("mcp_gh_leaky", {"q": f"带密钥 {HEADER_SECRET}"}, _ctx())
            finally:
                await ext.aclose()
        assert res.ok is True
        for c in tools.recent_calls(task_id="T-1"):
            assert HEADER_SECRET not in c["input"]
            assert HEADER_SECRET not in c["output"]
        assert HEADER_SECRET not in caplog.text
        for item in ext.info():
            assert HEADER_SECRET not in json.dumps(item, ensure_ascii=False)

    def test_app_known_secrets_includes_mcp_headers(self, tmp_path: Path) -> None:
        from test_app import _app, _raw

        raw = _raw(tmp_path / "data")
        raw["extensions"] = {"mcp": [{"name": "gh", "url": MCP_URL, "headers": {"X-Key": HEADER_SECRET}}]}
        app = _app(tmp_path, raw=raw)
        app._settings, app.problems = load_settings(raw)
        assert HEADER_SECRET in app._known_secrets()


class TestMcpFailureAndHealth:
    @pytest.mark.asyncio
    async def test_connect_failure_keeps_startup_and_marks_health(self, tmp_path: Path) -> None:
        """连不上不影响启动：app.start() 成功、健康状态里能看到错误。"""
        from test_app import _app, _raw

        raw = _raw(tmp_path / "data")
        raw["extensions"] = {"mcp": [{"name": "gh", "url": MCP_URL}]}
        app = _app(tmp_path, raw=raw)
        app.extensions_transport = httpx.MockTransport(_failing_server)
        await app.start()
        try:
            assert app.started is True
            info = app.extensions.info()
            assert info[0]["ok"] is False
            assert info[0]["error"]  # 有错误文字
            assert info[0]["tools"] == 0
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_reload_endpoint_reconnects_and_refreshes(self, tmp_path: Path) -> None:
        """POST /api/extensions/mcp/{name}/reload：管理员 200、工具表刷新；群友 403；匿名 401。"""
        import aiohttp
        from aiohttp.test_utils import TestClient, TestServer

        from test_app import _app, _raw
        from test_console import PASSWORD

        state = {"tools": [_tool_spec("one")], "init": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            method = body.get("method")
            if method == "initialize":
                state["init"] += 1
                return _sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "x"}},
                    },
                    session=f"sess-{state['init']}",
                )
            if method == "notifications/initialized":
                return httpx.Response(202, text="")
            if method == "tools/list":
                return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": state["tools"]}})
            return httpx.Response(500, text="unknown")

        raw = _raw(tmp_path / "data")
        raw["console"]["password"] = PASSWORD
        raw["extensions"] = {"mcp": [{"name": "gh", "url": MCP_URL}]}
        app = _app(tmp_path, raw=raw)
        app.extensions_transport = httpx.MockTransport(handler)
        await app.start()
        server_client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await server_client.start_server()
        try:
            assert "mcp_gh_one" in [t["function"]["name"] for t in app.tools.specs("worker")]
            # 匿名 → 401
            r = await server_client.post("/api/extensions/mcp/gh/reload", json={})
            assert r.status == 401
            # 群友 → 403
            token = app.token_of(G1)
            r = await server_client.post("/api/extensions/mcp/gh/reload", json={}, headers={"X-MW-Group": token})
            assert r.status == 403
            # 管理员 → 200，且工具表换成新列表
            await server_client.post("/api/login", json={"password": PASSWORD})
            state["tools"] = [_tool_spec("two")]
            r = await server_client.post("/api/extensions/mcp/gh/reload", json={})
            assert r.status == 200
            data = await r.json()
            assert data["ok"] is True
            assert data["tools"] == 1
            names = [t["function"]["name"] for t in app.tools.specs("worker")]
            assert "mcp_gh_two" in names and "mcp_gh_one" not in names
            # 名字不存在的扩展 → 404
            r = await server_client.post("/api/extensions/mcp/nope/reload", json={})
            assert r.status == 404
            # 响应不回显 headers
            assert "headers" not in data
        finally:
            await server_client.close()
            await app.stop()


class TestSettingsExtensionsView:
    @pytest.mark.asyncio
    async def test_settings_extensions_shape(self, tmp_path: Path) -> None:
        """GET /api/settings 的 extensions 段：mcp 只到 host（不带路径/query，防带 token）、
        不回显 headers；skills 给 name+description。"""
        import aiohttp
        from aiohttp.test_utils import TestClient, TestServer

        from test_app import _app, _raw
        from test_console import PASSWORD

        server = _McpServer([_tool_spec("a"), _tool_spec("b")])
        raw = _raw(tmp_path / "data")
        raw["console"]["password"] = PASSWORD
        raw["extensions"] = {
            "mcp": [
                {"name": "gh", "url": f"{MCP_URL}?key=should-not-leak", "headers": {"X-Key": HEADER_SECRET}},
                {"name": "down", "url": "https://down.example/mcp"},
            ]
        }
        app = _app(tmp_path, raw=raw)
        app.extensions_transport = httpx.MockTransport(server)
        # 造一个 skill
        data_dir = tmp_path / "data"
        skill_dir = data_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: 演示 skill\n---\n\n# 演示\n正文\n", encoding="utf-8"
        )
        await app.start()
        client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        try:
            await client.post("/api/login", json={"password": PASSWORD})
            r = await client.get("/api/settings")
            assert r.status == 200
            data = await r.json()
            text = json.dumps(data, ensure_ascii=False)
            assert HEADER_SECRET not in text
            assert "should-not-leak" not in text  # url 只到 host，query 不回显
            ext = data["extensions"]
            assert isinstance(ext["mcp"], list) and isinstance(ext["skills"], list)
            gh = next(m for m in ext["mcp"] if m["name"] == "gh")
            assert set(gh) == {"name", "url", "enabled", "ok", "tools", "error"}
            assert gh["url"] == "https://mcp-ext.example"
            assert gh["ok"] is True and gh["tools"] == 2 and gh["error"] == ""
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# skill：Skills 目录读取
# ----------------------------------------------------------------------


def _make_skill(data_dir: Path, name: str, *, front: str = "", body: str = "", files: dict[str, str] | None = None) -> Path:
    d = data_dir / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(front + body, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class TestSkills:
    def test_list_and_front_matter(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        _make_skill(
            tmp_path,
            "pptx",
            front="---\nname: pptx\ndescription: 做 PPT 的套路\nroles: worker, main\n---\n",
            body="# 步骤\n先列大纲\n",
        )
        _make_skill(tmp_path, "bare", body="没有 front matter 的正文\n")
        sk = Skills(tmp_path)
        items = sk.list()
        by_name = {i["name"]: i for i in items}
        assert set(by_name) == {"pptx", "bare"}
        assert by_name["pptx"]["description"] == "做 PPT 的套路"
        assert set(by_name["pptx"]["roles"]) == {"main", "worker"}
        assert isinstance(by_name["pptx"]["path"], str) and "pptx" in by_name["pptx"]["path"]
        # 没 front matter 的：description 空、roles 默认 worker
        assert by_name["bare"]["description"] == ""
        assert set(by_name["bare"]["roles"]) == {"worker"}

    def test_name_mismatch_uses_dir_name(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        _make_skill(tmp_path, "dir-name", front="---\nname: 别的名字\ndescription: x\n---\n")
        sk = Skills(tmp_path)
        assert [i["name"] for i in sk.list()] == ["dir-name"]

    def test_list_max_20_and_hint(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        for i in range(25):
            _make_skill(tmp_path, f"s{i:02d}", front=f"---\nname: s{i:02d}\ndescription: 第{i}个\n---\n")
        sk = Skills(tmp_path)
        assert len(sk.list()) <= 20
        hint = sk.hint()
        assert hint.count("\n") + 1 <= 20
        assert "s00" in hint and "第0个" in hint

    def test_read_full_text_and_cap(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        _make_skill(
            tmp_path,
            "big",
            front="---\nname: big\ndescription: d\n---\n",
            body="字" * 50000,
        )
        sk = Skills(tmp_path)
        text = sk.read("big")
        assert text is not None
        assert len(text) <= 40 * 1024

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        assert Skills(tmp_path).read("nope") is None
        assert Skills(tmp_path).read_file("nope", "a.txt") is None

    def test_read_file_ok(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        _make_skill(tmp_path, "kit", files={"scripts/build.py": "print('hi')\n"})
        sk = Skills(tmp_path)
        assert sk.read_file("kit", "scripts/build.py") == "print('hi')\n"

    def test_read_file_traversal_rejected(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        _make_skill(tmp_path, "kit", files={"a.txt": "ok"})
        sk = Skills(tmp_path)
        for rel in ("../a.txt", "..", "./../../etc/passwd", "/etc/passwd", "a/../../b.txt"):
            assert sk.read_file("kit", rel) is None, rel

    def test_read_file_symlink_rejected(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        d = _make_skill(tmp_path, "kit", files={"a.txt": "ok"})
        outside = tmp_path / "outside.txt"
        outside.write_text("外面的东西", encoding="utf-8")
        link = d / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("这个文件系统不支持符号链接")
        sk = Skills(tmp_path)
        assert sk.read_file("kit", "link.txt") is None
        # 名字本身是符号链接的整个 skill 目录也拒
        linkdir = tmp_path / "skills" / "linked"
        try:
            linkdir.symlink_to(d)
        except OSError:
            pass
        else:
            assert sk.read("linked") is None
            assert all(i["name"] != "linked" for i in sk.list())

    def test_skills_dir_missing_is_empty(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills

        sk = Skills(tmp_path / "不存在")
        assert sk.list() == []
        assert sk.read("x") is None
        assert sk.hint() == ""


class TestSkillTools:
    @pytest.mark.asyncio
    async def test_list_skills_and_read_skill(self, store: Store, tmp_path: Path) -> None:
        from CharTyr_MaiWork.skills import Skills
        from CharTyr_MaiWork.skills_tools import register_skill_tools

        _make_skill(
            tmp_path,
            "pptx",
            front="---\nname: pptx\ndescription: 做 PPT\n---\n",
            body="# 做法\n第一步\n",
            files={"模板.md": "模板内容"},
        )
        tools = Tools(store)
        register_skill_tools(tools, Skills(tmp_path))
        # 两个角色都能用
        assert "list_skills" in [t["function"]["name"] for t in tools.specs("worker")]
        assert "list_skills" in [t["function"]["name"] for t in tools.specs("main")]
        ctx = _ctx("worker")
        res = await tools.call("list_skills", {}, ctx)
        assert res.ok is True
        assert "pptx" in res.output and "做 PPT" in res.output
        res = await tools.call("read_skill", {"name": "pptx"}, ctx)
        assert res.ok is True
        assert "# 做法" in res.output
        res = await tools.call("read_skill", {"name": "pptx", "file": "模板.md"}, ctx)
        assert res.ok is True
        assert "模板内容" in res.output
        # 不存在 / 越界 → ok=False，面向模型的中文错误
        res = await tools.call("read_skill", {"name": "nope"}, ctx)
        assert res.ok is False
        res = await tools.call("read_skill", {"name": "pptx", "file": "../x"}, ctx)
        assert res.ok is False


# ----------------------------------------------------------------------
# Workers 的 skills_hint
# ----------------------------------------------------------------------


class ReplayModels:
    def __init__(self, results):
        self.queue = list(results)
        self.calls = []

    async def chat(self, role, messages, **kwargs):
        self.calls.append((role, [dict(m) for m in messages], kwargs))
        if not self.queue:
            return type("R", (), {"text": "", "tool_calls": []})()
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _submit_call():
    return type(
        "R",
        (),
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "submit_result", "arguments": json.dumps({"summary": "done"})},
                }
            ],
        },
    )()


def _submit_tools(store: Store) -> Tools:
    tools = Tools(store)

    async def submit(ctx, args):
        return ToolResult(ok=True, output="ok", data={"summary": args.get("summary", "")})

    tools.register(
        Tool(
            name="submit_result",
            description="交回",
            parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
            roles=frozenset({"worker"}),
            handler=submit,
        )
    )
    return tools


class TestWorkersSkillsHint:
    @pytest.mark.asyncio
    async def test_hint_in_system_prompt(self, store: Store) -> None:
        from CharTyr_MaiWork.workers import Workers

        tools = _submit_tools(store)
        models = ReplayModels([_submit_call()])
        w = Workers(models, tools, skills_hint_fn=lambda: "- pptx：做 PPT\n- docx：写文档")
        report = await w.run("干活", group_id=G1, tools=[])
        assert report.ok is True
        system = models.calls[0][1][0]["content"]
        assert "pptx：做 PPT" in system
        assert "docx：写文档" in system

    @pytest.mark.asyncio
    async def test_run_argument_overrides_default(self, store: Store) -> None:
        from CharTyr_MaiWork.workers import Workers

        tools = _submit_tools(store)
        models = ReplayModels([_submit_call(), _submit_call()])
        w = Workers(models, tools, skills_hint_fn=lambda: "默认提示")
        await w.run("干活", group_id=G1, tools=[], skills_hint="本次专用提示")
        system1 = models.calls[0][1][0]["content"]
        assert "本次专用提示" in system1 and "默认提示" not in system1
        # 不传时用构造函数的默认
        await w.run("干活2", group_id=G1, tools=[])
        system2 = models.calls[1][1][0]["content"]
        assert "默认提示" in system2

    @pytest.mark.asyncio
    async def test_no_hint_keeps_prompt_unchanged(self, store: Store) -> None:
        from CharTyr_MaiWork.workers import Workers, _system_prompt

        tools = _submit_tools(store)
        models = ReplayModels([])
        w = Workers(models, tools)
        # 老行为：没有 hint 时 system 提示和以前一模一样
        assert w._hint(None) == ""
        base = _system_prompt("子 agent #1", G1, None)
        assert "skill" not in base.lower()


# ----------------------------------------------------------------------
# 红线：不给 MaiBot 的 planner 注册任何组件
# ----------------------------------------------------------------------


class TestNoHostComponentRegistration:
    def test_no_new_components(self) -> None:
        from CharTyr_MaiWork.plugin import create_plugin

        comps = create_plugin().get_components()
        text = repr(comps)
        # 还是只有那两个钩子，没有新增任何组件（MCP/skill 只进 MaiWork 自己的 Tools）
        assert "maiwork_intake" in text
        assert "maiwork_mentions" in text
        assert "list_skills" not in text
        assert "read_skill" not in text
        assert "mcp_" not in text
        for c in comps:
            kind = str(c.get("component_type") or c.get("type") or "").lower()
            assert "tool" not in kind, c
