"""端点 / 模型库的网页 API（改版 1a；替代旧 /api/settings/models*）。

路由（都只给总管理员；写操作同源 guard 走 _write）：
- GET    /api/settings/endpoints              → {endpoints:[…无密钥…], models:[…]}
- PUT    /api/settings/endpoints/{id}         → 建/改端点（api_key 空 = 不改；写 config.toml 并立刻生效）
- DELETE /api/settings/endpoints/{id}         → 删端点（模型库还挂着它拒 400 中文）
- POST   /api/settings/endpoints/{id}/test    → 测连接（可带未存的 base_url/api_key/protocol）
- PUT    /api/settings/model-list/{id}        → 建/改模型条目
- DELETE /api/settings/model-list/{id}        → 删模型条目（岗位还在用拒 400，消息点名岗位）
旧 PUT /api/settings/models、POST /api/settings/models/test 已删 → 404。
"""

from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import config_file
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings

PASSWORD = "端点密码-显眼"
G1 = "900000001"
SECRET = "[redacted]"


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
    }
    endpoints = over.pop("endpoints", None)
    if endpoints is not None:
        raw["endpoints"] = endpoints
    model_list = over.pop("model_list", None)
    if model_list is not None:
        raw["model_list"] = model_list
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


def _write_plugin_dir(plug_dir: Path, raw: dict) -> None:
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            t = tomlkit.table()
            for k, v in values.items():
                t[k] = v
            doc[section] = t
        elif isinstance(values, list):
            aot = tomlkit.aot()
            for item in values:
                t = tomlkit.table()
                for k, v in item.items():
                    t[k] = v
                aot.append(t)
            doc[section] = aot
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


class _FakeListTransport:
    """列模型假端点：按 URL 返回数据，记录头。"""

    def __init__(self, payload=None, status: int = 200) -> None:
        self.payload = payload if payload is not None else {"object": "list", "data": [{"id": "m-a"}, {"id": "m-b"}]}
        self.status = status
        self.seen: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append({"url": str(request.url), "headers": dict(request.headers)})
        return httpx.Response(self.status, json=self.payload)


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = _raw_config(data_dir)
    _write_plugin_dir(plug_dir, raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    app.http_transport = httpx.MockTransport(_FakeListTransport())
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200
    yield type("E", (), {"app": app, "client": client, "plug_dir": plug_dir, "data_dir": data_dir})()
    await client.close()
    await app.stop()


class TestEndpointsListGet:
    @pytest.mark.asyncio
    async def test_endpoints_get_shape_and_secret_never_out(self, env) -> None:
        # 先建一个端点 + 一个模型
        r = await env.client.put(
            "/api/settings/endpoints/e1",
            json={"id": "e1", "name": "一号端点", "protocol": "openai",
                  "base_url": "https://a.test/v1/", "api_key": SECRET,
                  "retries": 3, "max_concurrency": 4, "max_rpm": 120},
        )
        assert r.status == 200, await r.text()
        r = await env.client.client.put if False else None
        r = await env.client.put(
            "/api/settings/model-list/m1",
            json={"endpoint": "e1", "model": "gpt-x", "name": "主模型甲",
                  "efforts": ["low", "high"], "vision": True,
                  "context_window": 200000, "max_tokens": 100000},
        )
        assert r.status == 200, await r.text()
        r = await env.client.get("/api/settings/endpoints")
        assert r.status == 200
        data = await r.json()
        assert list(data.keys()) == ["endpoints", "models"]
        e0 = data["endpoints"][0]
        assert e0["id"] == "e1" and e0["name"] == "一号端点" and e0["protocol"] == "openai"
        assert e0["base_url"] == "https://a.test/v1"
        assert e0["key_set"] is True
        assert (e0["retries"], e0["max_concurrency"], e0["max_rpm"]) == (3, 4, 120)
        m0 = data["models"][0]
        assert m0["id"] == "m1" and m0["endpoint"] == "e1" and m0["model"] == "gpt-x"
        assert m0["name"] == "主模型甲" and m0["efforts"] == ["low", "high"] and m0["vision"] is True
        assert (m0["context_window"], m0["max_tokens"]) == (200000, 100000)
        # 密钥永远不出接口
        whole = json.dumps(data, ensure_ascii=False)
        assert SECRET not in whole and "api_key" not in whole

    @pytest.mark.asyncio
    async def test_endpoints_get_requires_admin(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        plug_dir = tmp_path / "plug"
        raw = _raw_config(data_dir)
        _write_plugin_dir(plug_dir, raw)
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=plug_dir)
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            server = TestServer(app.console.app)
            client = TestClient(server)
            await client.start_server()
            try:
                r = await client.get("/api/settings/endpoints")
                assert r.status == 401
            finally:
                await client.close()
        finally:
            await app.stop()


class TestEndpointPut:
    @pytest.mark.asyncio
    async def test_create_writes_file_and_hot_applies(self, env) -> None:
        r = await env.client.put(
            "/api/settings/endpoints/e1",
            json={"id": "e1", "name": "甲", "base_url": "https://a.test/v1", "api_key": SECRET},
        )
        assert r.status == 200, await r.text()
        # 文件里真的写进去了（含密钥；config.toml 是唯一真实来源）
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert "[[endpoints]]" in text and 'id = "e1"' in text and SECRET in text
        # 立刻生效（不走宿主文件监控）
        s = env.app.get_settings()
        assert [e.id for e in s.endpoints] == ["e1"]
        assert s.endpoints[0].api_key == SECRET

    @pytest.mark.asyncio
    async def test_update_keeps_key_when_empty(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        assert r.status == 200
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "name": "改名", "api_key": ""})
        assert r.status == 200
        s = env.app.get_settings()
        assert s.endpoints[0].name == "改名" and s.endpoints[0].api_key == SECRET
        # null 也一样 = 不改
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "name": "再改"})
        assert r.status == 200
        assert env.app.get_settings().endpoints[0].api_key == SECRET

    @pytest.mark.asyncio
    async def test_create_id_must_match_and_validate(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "OTHER"})
        assert r.status == 400
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "not-a-url"})
        assert r.status == 400
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "retries": 99})
        assert r.status == 400
        r = await env.client.put("/api/settings/endpoints/BAD ID", json={"id": "BAD ID", "base_url": "https://a.test/v1"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_create_duplicate_id_via_body_rejected(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1"})
        assert r.status == 200
        r = await env.client.put("/api/settings/endpoints/e2", json={"id": "e1", "base_url": "https://b.test/v1"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_create_second_endpoint_appends(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        assert r.status == 200
        r = await env.client.put("/api/settings/endpoints/e2", json={"id": "e2", "protocol": "anthropic", "base_url": "https://claude.test", "api_key": "other"})
        assert r.status == 200, await r.text()
        ids = [e.id for e in env.app.get_settings().endpoints]
        assert ids == ["e1", "e2"]

    @pytest.mark.asyncio
    async def test_put_wrong_origin_blocked(self, env) -> None:
        """同源 guard（_write）：跨源 Origin 一律拒。先建个端点再看 DELETE。"""
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        assert r.status == 200
        # 用带 Origin 头的请求测（降级 403）
        r = await env.client.put(
            "/api/settings/endpoints/e1",
            json={"id": "e1"},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status in (403, 400) and env.app.get_settings().endpoints[0].name == "e1"


class TestEndpointDelete:
    @pytest.mark.asyncio
    async def test_delete_works(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        models_before = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        r = await env.client.delete("/api/settings/endpoints/e1")
        assert r.status == 200, await r.text()
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert "e1" not in text
        assert env.app.get_settings().endpoints == ()

    @pytest.mark.asyncio
    async def test_delete_refused_when_models_attached(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        await env.client.put("/api/settings/model-list/m1", json={"endpoint": "e1", "model": "x"})
        r = await env.client.delete("/api/settings/endpoints/e1")
        assert r.status == 400
        data = await r.json()
        assert "端点" in data["error"] and ("模型" in data["error"] or "1" in data["error"])


class TestEndpointTest:
    @pytest.mark.asyncio
    async def test_test_connection_openai(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        tr = env.app.models._transport  # MockTransport handler 记录的假端点
        r = await env.client.post("/api/settings/endpoints/e1/test", json={})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True and data["models"] == ["m-a", "m-b"]
        # 存到 kv
        checked = env.app.store.kv_get("endpoints.checked.e1")
        assert checked and checked["available"] == ["m-a", "m-b"] and checked["checked_at"] > 0
        # GET 并回来显示
        r = await env.client.get("/api/settings/endpoints")
        e0 = (await r.json())["endpoints"][0]
        assert e0["available"] == ["m-a", "m-b"] and e0["checked_at"] > 0
        # 请求头里用的就是存的密钥（不在响应里）
        calls = tr.handler.seen
        assert calls[-1]["headers"].get("authorization") == f"Bearer {SECRET}"

    @pytest.mark.asyncio
    async def test_test_unsaved_values_allowed(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        r = await env.client.post(
            "/api/settings/endpoints/e1/test",
            json={"base_url": "https://other.test/v1", "api_key": "tmp-secret-12345", "protocol": "openai"},
        )
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True
        tr = env.app.models._transport
        calls = tr.handler.seen
        assert calls[-1]["url"].startswith("https://other.test/v1/models")
        assert calls[-1]["headers"].get("authorization") == "Bearer tmp-secret-12345"
        assert "tmp-secret-12345" not in json.dumps(data, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_test_unknown_endpoint_404(self, env) -> None:
        # 没存的端点 id + body 没给 base_url → 404；body 给了 base_url 就能测未存值
        r = await env.client.post("/api/settings/endpoints/ghost/test", json={})
        assert r.status == 404
        r = await env.client.post("/api/settings/endpoints/ghost/test", json={"base_url": "https://x.test/v1", "api_key": "k"})
        assert r.status == 200
        assert (await r.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_test_failure_message_in_chinese(self, env) -> None:
        tr = env.app.models._transport
        tr.handler.status = 500
        tr.handler.payload = {"error": "爆炸"}
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        r = await env.client.post("/api/settings/endpoints/e1/test", json={})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is False and data["models"] == [] and data["error"]
        assert SECRET not in json.dumps(data, ensure_ascii=False)
        tr.handler.status = 200
        tr.handler.payload = None  # 复位

    @pytest.mark.asyncio
    async def test_test_anthropic_headers(self, env) -> None:
        await env.client.put(
            "/api/settings/endpoints/ant",
            json={"id": "ant", "protocol": "anthropic", "base_url": "https://claude.test", "api_key": "k-ant"},
        )
        r = await env.client.post("/api/settings/endpoints/ant/test", json={})
        assert r.status == 200
        tr = env.app.models._transport
        call = tr.handler.seen[-1]
        assert call["url"] == "https://claude.test/v1/models"
        assert call["headers"].get("x-api-key") == "k-ant"
        assert call["headers"].get("anthropic-version") == "2023-06-01"


class TestModelListApi:
    @pytest.mark.asyncio
    async def test_create_requires_existing_endpoint(self, env) -> None:
        r = await env.client.put("/api/settings/model-list/m1", json={"endpoint": "ghost", "model": "x"})
        assert r.status == 400
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        # model 空 → 400
        r = await env.client.put("/api/settings/model-list/m1", json={"endpoint": "e1", "model": ""})
        assert r.status == 400
        # path id 当条目 id（body 不带 id 也行）
        r = await env.client.put("/api/settings/model-list/m1", json={"endpoint": "e1", "model": "x"})
        assert r.status == 200, await r.text()
        assert [m.id for m in env.app.get_settings().model_list] == ["m1"]

    @pytest.mark.asyncio
    async def test_create_and_update_validations(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        # max_tokens 必须 < context_window
        r = await env.client.put("/api/settings/model-list/m1", json={"id": "m1", "endpoint": "e1", "model": "x", "context_window": 10000, "max_tokens": 10000})
        assert r.status == 400
        # 好参数 → 200
        r = await env.client.put("/api/settings/model-list/m1", json={"id": "m1", "endpoint": "e1", "model": "x", "context_window": 64000, "max_tokens": 8192})
        assert r.status == 200
        m = env.app.get_settings().model_list[0]
        assert (m.context_window, m.max_tokens) == (64000, 8192)
        # 文件里写了
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert "[[model_list]]" in text

    @pytest.mark.asyncio
    async def test_delete_refused_when_agent_uses_it(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        await env.client.put("/api/settings/model-list/m1", json={"id": "m1", "endpoint": "e1", "model": "x"})
        await env.client.put("/api/agents/main", json={"model": "m1"})
        r = await env.client.put("/api/agents/news", json={"backup": "m1"})
        assert r.status == 200
        r = await env.client.delete("/api/settings/model-list/m1")
        assert r.status == 400
        data = await r.json()
        assert "主模型" in data["error"] and "资讯" in data["error"]

    @pytest.mark.asyncio
    async def test_delete_works_when_unused(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        await env.client.put("/api/settings/model-list/m1", json={"id": "m1", "endpoint": "e1", "model": "x"})
        r = await env.client.delete("/api/settings/model-list/m1")
        assert r.status == 200
        assert env.app.get_settings().model_list == ()


class TestOldRoutesGone:
    @pytest.mark.asyncio
    async def test_old_models_routes_404(self, env) -> None:
        r = await env.client.put("/api/settings/models", json={"base_url": "https://a.test/v1", "main": "x", "worker": "y"})
        assert r.status == 404
        r = await env.client.post("/api/settings/models/test", json={"base_url": "https://a.test/v1"})
        assert r.status == 404


class TestRulesSchemaNoModels:
    def test_rules_schema_without_models_keys(self) -> None:
        from CharTyr_MaiWork.maiwork import rules

        keys = [f.get("key") or f.get("k") for f in rules.CONFIG_SCHEMA]
        assert not any(str(k).startswith("models.") for k in keys)


class TestSettingsViewSummary:
    @pytest.mark.asyncio
    async def test_settings_view_models_has_ready_and_main_label(self, env) -> None:
        r1 = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        assert r1.status == 200, await r1.text()
        r2 = await env.client.put("/api/settings/model-list/m1", json={"id": "m1", "endpoint": "e1", "model": "gpt-x", "name": "主选模型"})
        assert r2.status == 200, await r2.text()
        r3 = await env.client.put("/api/settings/model-list/w1", json={"id": "w1", "endpoint": "e1", "model": "gpt-y"})
        assert r3.status == 200, await r3.text()
        r4 = await env.client.put("/api/agents/main", json={"model": "m1"})
        assert r4.status == 200, await r4.text()
        r5 = await env.client.put("/api/agents/task", json={"model": "w1"})
        assert r5.status == 200, await r5.text()
        r = await env.client.get("/api/settings")
        assert r.status == 200
        data = await r.json()
        m = data["models"]
        assert m["ready"] is True
        assert m["main_label"] == "主选模型"
        assert SECRET not in json.dumps(m, ensure_ascii=False)
