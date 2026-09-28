"""网页管理扩展（MCP + skill）的接口测试（docs/02 §10、docs/07 §10.9）。

- 存法：kv["extensions.mcp"] 存网页加的条目（不含头值）；头值 secrets["mcp.<名字>.<头名>"]
  （密钥只进不出）；config 来源的开关存 kv["extensions.mcp.disabled"] 名单。
- 合并：网页同名 → 以网页为准；config 的仍生效（只能开关不能改地址）；删除只删网页加的。
- 接口全部仅管理员（群友 403、匿名 401），写接口过同源检查。
- 密钥断言：任何响应、日志、tool_calls 里都搜不到头的值。
- 改 / 删 / 开关后工具表立刻跟着变（重新注册 / 注销）；新密钥并进 Tools 遮罩。
- skill：网页新增写 <data_dir>/skills/<名字>/SKILL.md（名字 [A-Za-z0-9_-]{1,64}，目录 0700
  尽量设置、exFAT chmod 失败不报错；单个 ≤40KB）；删除拒绝符号链接、不越界。

密钥一律假值，HTTP 一律 httpx.MockTransport，不碰真实网络。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_app import _app, _raw
from test_console import PASSWORD
from test_extensions import G1, HEADER_SECRET, MCP_URL, _McpServer, _sse, _tool_spec

WEB_MCP = "webx"                       # 网页新增的扩展名
WEB_SECRET = "web-SECRET-Zz9Qw8Er7Ty6"  # ASCII 假密钥
WEB_URL = "https://web-ext.example/mcp"


# ----------------------------------------------------------------------
# 起 app + 网页客户端
# ----------------------------------------------------------------------


async def _make(tmp_path: Path, *, server=None, config_mcp: list[dict] | None = None):
    """起一个完整 app（MockTransport 的 MCP 端点）+ 已开服的 TestClient。"""
    raw = _raw(tmp_path / "data")
    raw["console"]["password"] = PASSWORD
    if config_mcp is not None:
        raw["extensions"] = {"mcp": config_mcp}
    app = _app(tmp_path, raw=raw)
    app.extensions_transport = httpx.MockTransport(server if server is not None else _McpServer([]))
    await app.start()
    client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    return app, client


async def _login(client: TestClient) -> None:
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


def _tool_names(app) -> list[str]:
    return [t["function"]["name"] for t in app.tools.specs("worker")]


def _kv(app, key: str):
    return app.store.kv_get(key)


# ----------------------------------------------------------------------
# GET /api/extensions：结构与鉴权
# ----------------------------------------------------------------------


class TestGetExtensions:
    @pytest.mark.asyncio
    async def test_anonymous_401_member_403(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            r = await client.get("/api/extensions")
            assert r.status == 401
            token = app.token_of(G1)
            r = await client.get("/api/extensions", headers={"X-MW-Group": token})
            assert r.status == 403
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_shape_config_and_web_sources(self, tmp_path: Path) -> None:
        """config 的标 source=config（开关可用、其余只读）；网页加的标 source=web。"""
        server = _McpServer([_tool_spec("a"), _tool_spec("b")])
        app, client = await _make(
            tmp_path,
            server=server,
            config_mcp=[
                {"name": "gh", "url": f"{MCP_URL}?key=query-SECRET", "headers": {"X-Key": HEADER_SECRET}},
            ],
        )
        try:
            await _login(client)
            # 网页再加一个
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": WEB_MCP, "url": WEB_URL, "headers": {"Authorization": f"Bearer {WEB_SECRET}"}},
            )
            assert r.status == 200
            r = await client.get("/api/extensions")
            assert r.status == 200
            data = await r.json()
            assert set(data.keys()) == {"mcp", "skills"}
            text = json.dumps(data, ensure_ascii=False)
            assert HEADER_SECRET not in text
            assert WEB_SECRET not in text
            assert "query-SECRET" not in text  # url 去掉查询串
            by_name = {m["name"]: m for m in data["mcp"]}
            cfg = by_name["gh"]
            assert cfg["source"] == "config"
            assert cfg["url"] == "https://mcp-ext.example/mcp"  # 保留路径、去掉查询串
            assert cfg["header_names"] == ["X-Key"]
            assert cfg["headers_set"] is True
            assert cfg["ok"] is True
            assert cfg["tools"] == 2
            assert cfg["tool_names"] == ["mcp_gh_a", "mcp_gh_b"]
            web = by_name[WEB_MCP]
            assert web["source"] == "web"
            assert web["header_names"] == ["Authorization"]
            assert web["headers_set"] is True
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# POST /api/extensions/mcp：新增
# ----------------------------------------------------------------------


class TestCreateMcp:
    @pytest.mark.asyncio
    async def test_create_connects_registers_and_returns_item(self, tmp_path: Path) -> None:
        server = _McpServer([_tool_spec("a"), _tool_spec("b")])
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            before = server.init_count
            r = await client.post(
                "/api/extensions/mcp",
                json={
                    "name": WEB_MCP,
                    "url": WEB_URL,
                    "headers": {"Authorization": f"Bearer {WEB_SECRET}"},
                    "tools": ["a"],
                    "roles": ["worker"],
                    "timeout_s": 15,
                },
            )
            assert r.status == 200
            item = await r.json()
            assert item["name"] == WEB_MCP
            assert item["url"] == WEB_URL
            assert item["source"] == "web"
            assert item["enabled"] is True
            assert item["ok"] is True          # 保存后立即连了一次
            assert item["error"] == ""
            assert item["tools"] == 1           # 白名单只放 a
            assert item["tool_names"] == ["mcp_webx_a"]
            assert item["roles"] == ["worker"]
            assert item["tools_filter"] == ["a"]
            assert item["header_names"] == ["Authorization"]
            assert item["headers_set"] is True
            assert item["timeout_s"] == 15
            assert server.init_count > before  # 真的连了
            # 工具立即注册
            assert "mcp_webx_a" in _tool_names(app)
            assert "mcp_webx_b" not in _tool_names(app)  # 白名单挡掉
            # 落库：kv 不含头值，secrets 里能读到
            entries = _kv(app, "extensions.mcp")
            assert isinstance(entries, list) and len(entries) == 1
            entry = entries[0]
            assert entry["name"] == WEB_MCP and entry["header_names"] == ["Authorization"]
            assert WEB_SECRET not in json.dumps(entry, ensure_ascii=False)
            assert app.store.secret_get(f"mcp.{WEB_MCP}.Authorization") == f"Bearer {WEB_SECRET}"
            # 响应里没有密钥
            assert WEB_SECRET not in json.dumps(item, ensure_ascii=False)
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_create_duplicate_409(self, tmp_path: Path) -> None:
        """重名（网页加的 / config 里有的）都 409。"""
        app, client = await _make(
            tmp_path, config_mcp=[{"name": "gh", "url": MCP_URL}]
        )
        try:
            await _login(client)
            body = {"name": WEB_MCP, "url": WEB_URL}
            r = await client.post("/api/extensions/mcp", json=body)
            assert r.status == 200
            r = await client.post("/api/extensions/mcp", json=body)
            assert r.status == 409
            r = await client.post("/api/extensions/mcp", json={"name": "gh", "url": WEB_URL})
            assert r.status == 409
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_create_validation_400(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            for bad in ("", "带点.name", "带空格 name", "超" * 33):
                r = await client.post("/api/extensions/mcp", json={"name": bad, "url": WEB_URL})
                assert r.status == 400, bad
            r = await client.post("/api/extensions/mcp", json={"name": "ok", "url": "http://x.example/mcp"})
            assert r.status == 400
            r = await client.post("/api/extensions/mcp", json={"name": "ok2"})
            assert r.status == 400
            # 名字校验过了但都不该写进库
            assert _kv(app, "extensions.mcp") is None
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_create_member_403_anonymous_401(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            body = {"name": WEB_MCP, "url": WEB_URL}
            r = await client.post("/api/extensions/mcp", json=body)
            assert r.status == 401
            token = app.token_of(G1)
            r = await client.post("/api/extensions/mcp", json=body, headers={"X-MW-Group": token})
            assert r.status == 403
            assert _kv(app, "extensions.mcp") is None  # 没写进去
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_write_origin_guard(self, tmp_path: Path) -> None:
        """写接口过同源检查：跨源 Origin → 403，东西不写库。"""
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": WEB_MCP, "url": WEB_URL},
                headers={"Origin": "http://evil.example"},
            )
            assert r.status == 403
            assert _kv(app, "extensions.mcp") is None
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_create_connect_failure_still_saved(self, tmp_path: Path) -> None:
        """连不上也保存：单项 ok=False + error 有原因；工具 0 个。"""

        def _fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连不上（测试桩）")

        app, client = await _make(tmp_path, server=_fail)
        try:
            await _login(client)
            r = await client.post("/api/extensions/mcp", json={"name": WEB_MCP, "url": WEB_URL})
            assert r.status == 200
            item = await r.json()
            assert item["ok"] is False
            assert item["error"]
            assert item["tools"] == 0
            entries = _kv(app, "extensions.mcp")
            assert len(entries) == 1  # 已保存
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# 密钥只进不出
# ----------------------------------------------------------------------


class TestSecretOnlyIn:
    @pytest.mark.asyncio
    async def test_secret_never_leaks(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """新增 / 改 / 试连 / 工具调用：响应、日志、tool_calls 里都搜不到头值。"""
        from CharTyr_MaiWork.maiwork.tools import ToolContext

        # 服务端把密钥回显进返回文本（最坏泄漏情形）
        def on_call(request: httpx.Request, body: dict) -> httpx.Response:
            return _sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": f"回应 {WEB_SECRET}"}], "isError": False},
                }
            )

        server = _McpServer([_tool_spec("leaky")], on_call)
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            with caplog.at_level(logging.DEBUG, logger="maiwork"):
                r = await client.post(
                    "/api/extensions/mcp",
                    json={"name": WEB_MCP, "url": WEB_URL, "headers": {"Authorization": WEB_SECRET}},
                )
                assert r.status == 200
                r = await client.get("/api/extensions")
                assert r.status == 200
                get_text = json.dumps(await r.json(), ensure_ascii=False)
                # 试连（头值不该回显）
                r = await client.post(
                    "/api/extensions/mcp/test",
                    json={"url": WEB_URL, "headers": {"X-Key": WEB_SECRET}},
                )
                assert r.status == 200
                test_text = json.dumps(await r.json(), ensure_ascii=False)
                # 调一次工具（服务端回显密钥 → 落库摘要必须遮住）
                res = await app.tools.call(
                    "mcp_webx_leaky",
                    {"q": f"带密钥 {WEB_SECRET}"},
                    ToolContext(group_id=G1, task_id="T-9", actor="子 agent #1", role="worker"),
                )
                assert res.ok is True
            calls = app.tools.recent_calls(task_id="T-9")
            for c in calls:
                assert WEB_SECRET not in c["input"]
                assert WEB_SECRET not in c["output"]
            assert WEB_SECRET not in get_text
            assert WEB_SECRET not in test_text
            assert WEB_SECRET not in caplog.text
            # 新密钥并进已知密钥遮罩
            assert WEB_SECRET in app._known_secrets()
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# PUT /api/extensions/mcp/{name}：修改
# ----------------------------------------------------------------------


class TestUpdateMcp:
    @pytest.mark.asyncio
    async def test_update_url_tools_and_headers(self, tmp_path: Path) -> None:
        server = _McpServer([_tool_spec("a"), _tool_spec("b")])
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": WEB_MCP, "url": WEB_URL, "headers": {"X-Key": WEB_SECRET}},
            )
            assert r.status == 200
            # 改：换地址（查询串不回显）、白名单只要 b、加一个新头、留空的头 = 不改
            r = await client.put(
                f"/api/extensions/mcp/{WEB_MCP}",
                json={
                    "url": f"{WEB_URL}?new=1",
                    "tools": ["b"],
                    "headers": {"X-Key": "", "Authorization": f"Bearer {WEB_SECRET}-2"},
                },
            )
            assert r.status == 200
            item = await r.json()
            assert item["url"] == WEB_URL  # 查询串不回显
            assert item["tools_filter"] == ["b"]
            assert item["tool_names"] == ["mcp_webx_b"]
            assert sorted(item["header_names"]) == ["Authorization", "X-Key"]
            text = json.dumps(item, ensure_ascii=False)
            assert WEB_SECRET not in text
            # 头值：留空的没改，新的已存
            assert app.store.secret_get(f"mcp.{WEB_MCP}.X-Key") == WEB_SECRET
            assert app.store.secret_get(f"mcp.{WEB_MCP}.Authorization") == f"Bearer {WEB_SECRET}-2"
            # 工具表立即换
            names = _tool_names(app)
            assert "mcp_webx_b" in names and "mcp_webx_a" not in names
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_update_remove_headers(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp",
                json={
                    "name": WEB_MCP,
                    "url": WEB_URL,
                    "headers": {"X-A": "aaa", "X-B": "bbb"},
                },
            )
            assert r.status == 200
            r = await client.put(
                f"/api/extensions/mcp/{WEB_MCP}",
                json={"remove_headers": ["X-A"]},
            )
            assert r.status == 200
            item = await r.json()
            assert item["header_names"] == ["X-B"]
            # secrets 里的删掉
            assert app.store.secret_get(f"mcp.{WEB_MCP}.X-A") == ""
            assert app.store.secret_get(f"mcp.{WEB_MCP}.X-B") == "bbb"
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_update_config_source_rejected(self, tmp_path: Path) -> None:
        """config 来源的只能开关不能改地址：PUT → 409；不存在 → 404。"""
        app, client = await _make(tmp_path, config_mcp=[{"name": "gh", "url": MCP_URL}])
        try:
            await _login(client)
            r = await client.put("/api/extensions/mcp/gh", json={"url": WEB_URL})
            assert r.status == 409
            r = await client.put("/api/extensions/mcp/nope", json={"url": WEB_URL})
            assert r.status == 404
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# DELETE /api/extensions/mcp/{name}
# ----------------------------------------------------------------------


class TestDeleteMcp:
    @pytest.mark.asyncio
    async def test_delete_web_entry_unregisters_and_cleans_secrets(self, tmp_path: Path) -> None:
        server = _McpServer([_tool_spec("a")])
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": WEB_MCP, "url": WEB_URL, "headers": {"X-Key": WEB_SECRET}},
            )
            assert r.status == 200
            assert "mcp_webx_a" in _tool_names(app)
            r = await client.delete(f"/api/extensions/mcp/{WEB_MCP}")
            assert r.status == 200
            assert "mcp_webx_a" not in _tool_names(app)  # 工具立刻注销
            assert _kv(app, "extensions.mcp") == []
            assert app.store.secret_get(f"mcp.{WEB_MCP}.X-Key") == ""
            # GET 里也没了
            r = await client.get("/api/extensions")
            names = [m["name"] for m in (await r.json())["mcp"]]
            assert WEB_MCP not in names
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_delete_config_source_forbidden(self, tmp_path: Path) -> None:
        """config 来源的不能删（409）；不存在 404。"""
        app, client = await _make(tmp_path, config_mcp=[{"name": "gh", "url": MCP_URL}])
        try:
            await _login(client)
            r = await client.delete("/api/extensions/mcp/gh")
            assert r.status == 409
            r = await client.delete("/api/extensions/mcp/nope")
            assert r.status == 404
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_delete_member_403(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            token = app.token_of(G1)
            r = await client.delete(f"/api/extensions/mcp/{WEB_MCP}", headers={"X-MW-Group": token})
            assert r.status == 403
            r = await client.delete(f"/api/extensions/mcp/{WEB_MCP}")
            assert r.status == 401
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# POST /api/extensions/mcp/{name}/toggle：开关（config 来源也能开关）
# ----------------------------------------------------------------------


class TestToggleMcp:
    @pytest.mark.asyncio
    async def test_toggle_web_entry(self, tmp_path: Path) -> None:
        server = _McpServer([_tool_spec("a")])
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            r = await client.post("/api/extensions/mcp", json={"name": WEB_MCP, "url": WEB_URL})
            assert r.status == 200
            assert "mcp_webx_a" in _tool_names(app)
            r = await client.post(f"/api/extensions/mcp/{WEB_MCP}/toggle", json={"enabled": False})
            assert r.status == 200
            item = await r.json()
            assert item["enabled"] is False
            assert item["ok"] is False
            assert "mcp_webx_a" not in _tool_names(app)  # 立即注销
            r = await client.post(f"/api/extensions/mcp/{WEB_MCP}/toggle", json={"enabled": True})
            assert r.status == 200
            assert (await r.json())["enabled"] is True
            assert "mcp_webx_a" in _tool_names(app)  # 立即注册回来
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_config_entry_via_disabled_list(self, tmp_path: Path) -> None:
        """config 来源：开关存 kv["extensions.mcp.disabled"] 名单；关掉立即摘工具。"""
        server = _McpServer([_tool_spec("cfg")])
        app, client = await _make(tmp_path, server=server, config_mcp=[{"name": "gh", "url": MCP_URL}])
        try:
            await _login(client)
            assert "mcp_gh_cfg" in _tool_names(app)
            r = await client.post("/api/extensions/mcp/gh/toggle", json={"enabled": False})
            assert r.status == 200
            item = await r.json()
            assert item["source"] == "config"
            assert item["enabled"] is False
            assert "mcp_gh_cfg" not in _tool_names(app)
            assert _kv(app, "extensions.mcp.disabled") == ["gh"]
            r = await client.post("/api/extensions/mcp/gh/toggle", json={"enabled": True})
            assert r.status == 200
            assert (await r.json())["enabled"] is True
            assert "mcp_gh_cfg" in _tool_names(app)
            assert _kv(app, "extensions.mcp.disabled") == []
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_toggle_bad_input_and_auth(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path, config_mcp=[{"name": "gh", "url": MCP_URL}])
        try:
            await _login(client)
            r = await client.post("/api/extensions/mcp/gh/toggle", json={})
            assert r.status == 400
            r = await client.post("/api/extensions/mcp/nope/toggle", json={"enabled": True})
            assert r.status == 404
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# POST /api/extensions/mcp/test：只试连不保存
# ----------------------------------------------------------------------


class TestMcpTestEndpoint:
    @pytest.mark.asyncio
    async def test_ok_lists_tool_names(self, tmp_path: Path) -> None:
        server = _McpServer([_tool_spec("a"), _tool_spec("b")])
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            r = await client.post("/api/extensions/mcp/test", json={"url": WEB_URL})
            assert r.status == 200
            data = await r.json()
            assert data == {"ok": True, "tools": ["a", "b"], "error": ""}
            assert _kv(app, "extensions.mcp") is None  # 不保存
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_failure_masks_headers(self, tmp_path: Path) -> None:
        def _fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=f"key={WEB_SECRET}")

        app, client = await _make(tmp_path, server=_fail)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp/test",
                json={"url": WEB_URL, "headers": {"X-Key": WEB_SECRET}},
            )
            assert r.status == 200
            data = await r.json()
            assert data["ok"] is False
            assert data["error"]
            assert WEB_SECRET not in json.dumps(data, ensure_ascii=False)
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_empty_headers_fall_back_to_stored(self, tmp_path: Path) -> None:
        """headers 为空 + 传 name：用已存的密钥（试连只 initialize/list，直接看请求头）。"""
        server = _McpServer([_tool_spec("a")])
        app, client = await _make(tmp_path, server=server)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": WEB_MCP, "url": WEB_URL, "headers": {"Authorization": f"Bearer {WEB_SECRET}"}},
            )
            assert r.status == 200
            before = len(server.requests)
            r = await client.post("/api/extensions/mcp/test", json={"url": WEB_URL, "name": WEB_MCP})
            assert r.status == 200
            assert (await r.json())["ok"] is True
            new_reqs = server.requests[before:]
            assert new_reqs, "试连应该发过请求"
            assert all(str(req["headers"].get("authorization") or "") == f"Bearer {WEB_SECRET}" for req in new_reqs)
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_validation_and_auth(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            r = await client.post("/api/extensions/mcp/test", json={"url": WEB_URL})
            assert r.status == 401
            token = app.token_of(G1)
            r = await client.post(
                "/api/extensions/mcp/test", json={"url": WEB_URL}, headers={"X-MW-Group": token}
            )
            assert r.status == 403
            await _login(client)
            r = await client.post("/api/extensions/mcp/test", json={"url": "http://x.example/mcp"})
            assert r.status == 400
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# 网页同名 → 以网页为准（覆盖 config 条目）
# ----------------------------------------------------------------------


class TestWebOverridesConfig:
    @pytest.mark.asyncio
    async def test_web_entry_shadows_config(self, tmp_path: Path) -> None:
        """config 里已有 gh：网页加同名的 → 合并结果只有一条，且是网页的（以网页为准）。"""
        from CharTyr_MaiWork.maiwork.extensions_web import merged

        app, client = await _make(
            tmp_path,
            server=_McpServer([_tool_spec("a")]),
            config_mcp=[{"name": "gh", "url": MCP_URL, "tools": ["zzz"]}],
        )
        try:
            settings = app.get_settings()
            with app.store.tx() as conn:
                app.store.kv_set(
                    conn,
                    "extensions.mcp",
                    [{"name": "gh", "url": WEB_URL, "tools": [], "roles": ["worker"], "enabled": True,
                      "timeout_s": 20, "header_names": []}],
                )
            pairs = merged(settings, app.store)
            assert len(pairs) == 1
            setting, source = pairs[0]
            assert source == "web"
            assert setting.url == WEB_URL  # 以网页为准
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# GET /api/settings：extensions 段加 manage:true，旧结构不动
# ----------------------------------------------------------------------


class TestSettingsManageFlag:
    @pytest.mark.asyncio
    async def test_settings_extensions_manage_true(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path, config_mcp=[{"name": "gh", "url": MCP_URL}])
        try:
            await _login(client)
            r = await client.get("/api/settings")
            assert r.status == 200
            ext = (await r.json())["extensions"]
            assert ext["manage"] is True
            assert isinstance(ext["mcp"], list) and isinstance(ext["skills"], list)
            gh = next(m for m in ext["mcp"] if m["name"] == "gh")
            assert set(gh) == {"name", "url", "enabled", "ok", "tools", "error"}  # 旧结构没动
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# skill 网页管理
# ----------------------------------------------------------------------


def _skill_dir(app) -> Path:
    return app.get_settings().data_dir / "skills"


class TestSkillCreateRead:
    @pytest.mark.asyncio
    async def test_create_get_update_delete(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            # 新增
            r = await client.post(
                "/api/extensions/skills",
                json={"name": "pptx", "description": "做 PPT 的套路", "roles": ["worker"], "body": "# 步骤\n先列大纲\n"},
            )
            assert r.status == 200
            item = await r.json()
            assert item["name"] == "pptx"
            assert item["description"] == "做 PPT 的套路"
            assert item["roles"] == ["worker"]
            assert item["source"] == "web"
            assert item["body"] == "# 步骤\n先列大纲\n"
            # 真的落到 <data_dir>/skills/pptx/SKILL.md，front matter + 正文
            md = (_skill_dir(app) / "pptx" / "SKILL.md").read_text(encoding="utf-8")
            assert "description: 做 PPT 的套路" in md
            assert "# 步骤" in md
            # GET 回来
            r = await client.get("/api/extensions/skills/pptx")
            assert r.status == 200
            got = await r.json()
            assert got["body"] == "# 步骤\n先列大纲\n"
            assert got["files"] == []
            # 改
            r = await client.put(
                "/api/extensions/skills/pptx",
                json={"description": "改过的描述", "body": "# 新正文\n"},
            )
            assert r.status == 200
            got = await client.get("/api/extensions/skills/pptx")
            data = await got.json()
            assert data["description"] == "改过的描述"
            assert data["body"] == "# 新正文\n"
            # GET /api/extensions 的 skills 清单也跟着变（现读目录，不缓存）
            r = await client.get("/api/extensions")
            skills = (await r.json())["skills"]
            one = next(s for s in skills if s["name"] == "pptx")
            assert one["description"] == "改过的描述"
            assert one["source"] == "web"
            assert one["size"] > 0 and one["updated_ts"] > 0
            assert one["roles"] == ["worker"]
            # 子 agent 的 list_skills 工具下一次就读到新的
            from CharTyr_MaiWork.maiwork.tools import ToolContext

            res = await app.tools.call(
                "list_skills", {}, ToolContext(group_id=G1, task_id="T-1", actor="子 agent #1", role="worker")
            )
            assert res.ok is True and "改过的描述" in res.output
            # 删
            r = await client.delete("/api/extensions/skills/pptx")
            assert r.status == 200
            assert not (_skill_dir(app) / "pptx").exists()
            r = await client.get("/api/extensions/skills/pptx")
            assert r.status == 404
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_dir_chmod_700_when_supported(self, tmp_path: Path) -> None:
        import os
        import stat

        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/skills", json={"name": "perm", "description": "", "body": "x"}
            )
            assert r.status == 200
            mode = stat.S_IMODE(os.stat(_skill_dir(app) / "perm").st_mode)
            if mode != 0o700:
                pytest.skip("这个文件系统不支持权限位（exFAT）")
            assert stat.S_IMODE(os.stat(_skill_dir(app) / "perm").st_mode) == 0o700
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_duplicate_409_and_name_validation(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            body = {"name": "demo", "description": "", "body": "x"}
            r = await client.post("/api/extensions/skills", json=body)
            assert r.status == 200
            r = await client.post("/api/extensions/skills", json=body)
            assert r.status == 409
            for bad in ("", "带点.name", "带空格", "超" * 65, ".."):
                r = await client.post("/api/extensions/skills", json={"name": bad, "body": "x"})
                assert r.status == 400, bad
            # 带 / 的名字：校验直接 400，不建目录
            r = await client.post("/api/extensions/skills", json={"name": "a/b", "body": "x"})
            assert r.status == 400
            assert not (_skill_dir(app) / "a").exists()
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_body_size_cap_40kb(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            big = "字" * 14000  # 约 42KB
            r = await client.post("/api/extensions/skills", json={"name": "big", "body": big})
            assert r.status == 400
            assert not (_skill_dir(app) / "big").exists()
            # 新增一个小的，再改大也不行
            r = await client.post("/api/extensions/skills", json={"name": "small", "body": "x"})
            assert r.status == 200
            r = await client.put("/api/extensions/skills/small", json={"body": big})
            assert r.status == 400
            got = await client.get("/api/extensions/skills/small")
            assert (await got.json())["body"] == "x\n"  # 没改成（渲染统一补末尾换行）
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_member_403_anonymous_401(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            token = app.token_of(G1)
            for method, path, kw in (
                ("get", "/api/extensions/skills/x", {}),
                ("post", "/api/extensions/skills", {"json": {"name": "x", "body": "y"}}),
                ("delete", "/api/extensions/skills/x", {}),
            ):
                r = await getattr(client, method)(path, **kw)
                assert r.status == 401, (method, path)
                r = await getattr(client, method)(path, headers={"X-MW-Group": token}, **kw)
                assert r.status == 403, (method, path)
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_files_listed_and_file_source(self, tmp_path: Path) -> None:
        """目录手动放的 skill 标 source=file；附属文件只读列出。"""
        app, client = await _make(tmp_path)
        try:
            d = _skill_dir(app) / "kit"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\ndescription: 手动放的\n---\n\n正文\n", encoding="utf-8")
            (d / "模板.md").write_text("模板", encoding="utf-8")
            await _login(client)
            r = await client.get("/api/extensions/skills/kit")
            assert r.status == 200
            data = await r.json()
            assert data["source"] == "file"
            assert data["body"] == "正文\n"
            assert "模板.md" in data["files"]
            assert "SKILL.md" not in data["files"]
            r = await client.get("/api/extensions")
            one = next(s for s in (await r.json())["skills"] if s["name"] == "kit")
            assert one["source"] == "file"
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_delete_rejects_symlink(self, tmp_path: Path) -> None:
        """DELETE 拒绝符号链接目录：不把链接指向的真目录删掉。"""
        app, client = await _make(tmp_path)
        try:
            real = tmp_path / "real-target"
            real.mkdir()
            (real / "SKILL.md").write_text("真目录", encoding="utf-8")
            link_root = _skill_dir(app)
            link_root.mkdir(parents=True, exist_ok=True)
            link = link_root / "linked"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                pytest.skip("这个文件系统不支持符号链接")
            await _login(client)
            r = await client.delete("/api/extensions/skills/linked")
            assert r.status == 409
            assert real.exists() and (real / "SKILL.md").exists()  # 目标没被碰
            # 名字造假的越界形态也进不来
            for bad in ("..", "../x"):
                r = await client.delete(f"/api/extensions/skills/{bad}")
                assert r.status in (400, 404), bad
        finally:
            await client.close()
            await app.stop()

    @pytest.mark.asyncio
    async def test_delete_not_escape_skills_dir(self, tmp_path: Path) -> None:
        """数据层直接调 delete：名字不合法 / 越界 → 报错且 skills 目录外的东西原样。"""
        from CharTyr_MaiWork.maiwork import skills_web

        app, client = await _make(tmp_path)
        try:
            root = _skill_dir(app)
            root.mkdir(parents=True, exist_ok=True)
            outside = root.parent / "守在外面.md"
            outside.write_text("不许动", encoding="utf-8")
            for bad in ("", "..", ".", "../守在外面", "a/b"):
                with pytest.raises((ValueError, KeyError)):
                    skills_web.delete(app.get_settings().data_dir, app.store, bad)
            assert outside.exists()
        finally:
            await client.close()
            await app.stop()
