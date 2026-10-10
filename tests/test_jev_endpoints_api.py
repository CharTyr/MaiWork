"""判断服务（Jev / Decisions）的网页 API。

路由（都只给总管理员；写操作同源 guard 走 _write）：
- GET    /api/settings/jev                          → {enabled, timeout_ms, use, use_ok, endpoints:[…], presets:[…]}
- PUT    /api/settings/jev/endpoints/{id}           → 建/改（内置 typesafe 写 [jev] api_url/model；空密钥 = 不改）
- DELETE /api/settings/jev/endpoints/{id}           → 删（内置拒 400；正在用拒 400；不存在 404）
- PUT    /api/settings/jev/use                      → 换当前用的那个（写 [jev] use）
- POST   /api/settings/jev/endpoints/{id}/test      → 真发一次小请求（可带未存的 url/model/api_key/protocol）

密钥永远不出接口：只给 key_set / key_source。
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

from CharTyr_MaiWork.maiwork.app import MaiWorkApp

PASSWORD = "判断服务密码-显眼"
G1 = "900000001"
SECRET = "[redacted]"
SECRET2 = "jev-tmp-secret-12345"

PRESET_ORDER = [
    "typesafe", "openrouter", "opencode", "commandcode", "vercel",
    "upstage", "inception", "liquid", "cloudflare", "openai",
]

SYSTEMONE_OK = {"answers": {
    "greeting": {"type": "noul", "noul": 0.9},
    "lang": {"type": "choice", "choice": "zh",
             "probabilities": {"zh": 0.9, "en": 0.1}, "confidence": 0.8},
}}
OPENAI_OK = {"answers": [
    {"type": "predicate", "name": "greeting", "probability": 0.9},
    {"type": "choice", "name": "lang", "choice": "zh",
     "probabilities": [{"value": "zh", "probability": 0.9}, {"value": "en", "probability": 0.1}],
     "confidence": 0.8},
]}


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
    }
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


class _FakeJevTransport:
    """判断服务假端点：记 URL / 头，可按需返回错误。"""

    def __init__(self) -> None:
        self.status = 200
        self.payload = dict(SYSTEMONE_OK)
        self.seen: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append({"url": str(request.url), "headers": dict(request.headers),
                          "body": json.loads(request.content or b"{}")})
        if self.status != 200:
            return httpx.Response(self.status, json=self.payload)
        return httpx.Response(200, json=self.payload)


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = _raw_config(data_dir)
    _write_plugin_dir(plug_dir, raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    tr = _FakeJevTransport()
    app.jev_transport = httpx.MockTransport(tr.handler)
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200
    yield type("E", (), {"app": app, "client": client, "plug_dir": plug_dir,
                         "data_dir": data_dir, "tr": tr})()
    await client.close()
    await app.stop()


async def _create_or(env, **over) -> httpx.Response:
    body = {"preset": "openrouter", "api_key": SECRET}
    body.update(over)
    return await env.client.put("/api/settings/jev/endpoints/or", json=body)


class TestJevGet:
    @pytest.mark.asyncio
    async def test_shape_builtin_first(self, env) -> None:
        r = await _create_or(env)
        assert r.status == 200, await r.text()
        data = await r.json()
        assert list(data.keys()) == ["enabled", "timeout_ms", "use", "use_ok", "endpoints", "presets", "quick_judge"]
        assert data["enabled"] is True
        assert data["use"] == "typesafe"
        eps = data["endpoints"]
        assert [e["id"] for e in eps] == ["typesafe", "or"]
        b = eps[0]
        assert b["name"] == "TypeSafe 官方" and b["preset"] == "typesafe"
        assert b["protocol"] == "systemone" and b["builtin"] is True
        assert b["url"] == "https://api.typesafe.ai/v1/systemone"
        assert b["model"] == "jev-1.13.0"
        assert b["key_set"] is False and b["key_source"] == "none"
        c = eps[1]
        assert c["name"] == "OpenRouter" and c["preset"] == "openrouter"
        assert c["url"] == "https://openrouter.ai/api/v1/systemone"
        assert c["model"] == "typesafe/jev-1.13" and c["key_set"] is True and c["builtin"] is False
        assert "key_source" not in c

    @pytest.mark.asyncio
    async def test_quick_judge_block(self, env) -> None:
        """「没有 Jev 时怎么判断派活」：现值 + 今天所有群判了几次（kv 计数）。"""
        from CharTyr_MaiWork.maiwork import clock
        from CharTyr_MaiWork.maiwork.quick_judge import calls_key

        day = clock.day_key(clock.now())
        with env.app.store.tx() as conn:
            env.app.store.kv_set(conn, calls_key(G1, day), 3)
            env.app.store.kv_set(conn, calls_key("123", "2000-01-01"), 9)
        data = await (await env.client.get("/api/settings/jev")).json()
        q = data["quick_judge"]
        assert q == {"enabled": True, "model": "", "keyword_filter": False, "daily_max": 30, "today": 3}

    @pytest.mark.asyncio
    async def test_quick_judge_saved_via_config_put(self, env) -> None:
        """网页那块的保存走 PUT /api/settings/config，四个键都要收、回读一致。"""
        body = {"quick_judge.enabled": False, "quick_judge.model": "",
                "quick_judge.keyword_filter": True, "quick_judge.daily_max": 12}
        r = await env.client.put("/api/settings/config", json=body)
        assert r.status == 200, await r.text()
        q = (await (await env.client.get("/api/settings/jev")).json())["quick_judge"]
        assert q["enabled"] is False and q["keyword_filter"] is True and q["daily_max"] == 12

    @pytest.mark.asyncio
    async def test_presets_list(self, env) -> None:
        data = await (await env.client.get("/api/settings/jev")).json()
        presets = data["presets"]
        assert [p["id"] for p in presets] == PRESET_ORDER
        for p in presets:
            assert set(p.keys()) == {
                "id", "name", "protocol", "url", "model", "models", "docs_url", "key_url", "note",
            }
            assert isinstance(p["models"], list) and p["models"]
        by_id = {p["id"]: p for p in presets}
        assert by_id["openai"]["protocol"] == "openai_decisions"
        assert by_id["typesafe"]["url"] == "https://api.typesafe.ai/v1/systemone"
        assert by_id["openrouter"]["key_url"] == "https://openrouter.ai/keys"

    @pytest.mark.asyncio
    async def test_secret_never_out(self, env) -> None:
        await _create_or(env)
        data = await (await env.client.get("/api/settings/jev")).json()
        whole = json.dumps(data, ensure_ascii=False)
        assert SECRET not in whole and "api_key" not in whole

    @pytest.mark.asyncio
    async def test_use_ok_follows_key(self, env) -> None:
        data = await (await env.client.get("/api/settings/jev")).json()
        assert data["use_ok"] is False  # 内置没密钥
        await _create_or(env)
        r = await env.client.put("/api/settings/jev/use", json={"id": "or"})
        assert r.status == 200
        data = await r.json()
        assert data["use"] == "or" and data["use_ok"] is True

    @pytest.mark.asyncio
    async def test_requires_admin(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        plug_dir = tmp_path / "plug"
        _write_plugin_dir(plug_dir, _raw_config(data_dir))
        app = MaiWorkApp(FakeCtx({}), _raw_config(data_dir), plugin_dir=plug_dir)
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            server = TestServer(app.console.app)
            client = TestClient(server)
            await client.start_server()
            try:
                r = await client.get("/api/settings/jev")
                assert r.status == 401
                r = await client.put("/api/settings/jev/use", json={"id": "typesafe"})
                assert r.status == 401
            finally:
                await client.close()
        finally:
            await app.stop()


class TestJevBuiltinPut:
    @pytest.mark.asyncio
    async def test_write_url_model_key(self, env) -> None:
        r = await env.client.put(
            "/api/settings/jev/endpoints/typesafe",
            json={"url": "https://proxy.test/v1/systemone", "model": "jev-x", "api_key": SECRET},
        )
        assert r.status == 200, await r.text()
        s = env.app.get_settings()
        assert (s.jev.api_url, s.jev.model, s.jev.api_key) == (
            "https://proxy.test/v1/systemone", "jev-x", SECRET,
        )
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert 'api_url = "https://proxy.test/v1/systemone"' in text and SECRET in text
        view = await r.json()
        assert view["endpoints"][0]["url"] == "https://proxy.test/v1/systemone"
        assert view["endpoints"][0]["model"] == "jev-x"
        assert view["endpoints"][0]["key_set"] is True
        assert view["endpoints"][0]["key_source"] == "file"

    @pytest.mark.asyncio
    async def test_empty_key_keeps_old(self, env) -> None:
        await env.client.put("/api/settings/jev/endpoints/typesafe", json={"api_key": SECRET})
        r = await env.client.put("/api/settings/jev/endpoints/typesafe", json={"model": "jev-y", "api_key": ""})
        assert r.status == 200
        s = env.app.get_settings()
        assert (s.jev.api_key, s.jev.model) == (SECRET, "jev-y")
        r = await env.client.put("/api/settings/jev/endpoints/typesafe", json={"model": "jev-z"})
        assert r.status == 200
        assert env.app.get_settings().jev.api_key == SECRET

    @pytest.mark.asyncio
    async def test_validations(self, env) -> None:
        r = await env.client.put("/api/settings/jev/endpoints/typesafe", json={"url": "http://api.test/v1/systemone"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/typesafe",
                                json={"url": "https://api.cloudflare.com/{account_id}/x"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/typesafe", json={"model": ""})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/typesafe", json={"model": "m" * 201})
        assert r.status == 400


class TestJevEndpointPut:
    @pytest.mark.asyncio
    async def test_create_from_preset(self, env) -> None:
        r = await _create_or(env)
        assert r.status == 200, await r.text()
        eps = env.app.get_settings().jev_endpoints
        assert len(eps) == 1
        e = eps[0]
        assert (e.id, e.preset, e.name, e.protocol, e.url, e.model, e.api_key) == (
            "or", "openrouter", "OpenRouter", "systemone",
            "https://openrouter.ai/api/v1/systemone", "typesafe/jev-1.13", SECRET,
        )
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert "[[jev_endpoints]]" in text and 'id = "or"' in text and SECRET in text

    @pytest.mark.asyncio
    async def test_create_custom_and_update_keeps_key(self, env) -> None:
        r = await env.client.put("/api/settings/jev/endpoints/mine", json={
            "name": "自家判断", "protocol": "openai_decisions",
            "url": "https://x.test/v1/decisions", "model": "gpt-6-luna", "api_key": SECRET,
        })
        assert r.status == 200, await r.text()
        r = await env.client.put("/api/settings/jev/endpoints/mine", json={"name": "改名", "api_key": ""})
        assert r.status == 200
        e = env.app.get_settings().jev_endpoints[0]
        assert (e.name, e.api_key, e.protocol) == ("改名", SECRET, "openai_decisions")
        r = await env.client.put("/api/settings/jev/endpoints/mine", json={"name": "再改"})
        assert r.status == 200
        assert env.app.get_settings().jev_endpoints[0].api_key == SECRET

    @pytest.mark.asyncio
    async def test_two_endpoints_append(self, env) -> None:
        assert (await _create_or(env)).status == 200
        r = await env.client.put("/api/settings/jev/endpoints/oa", json={"preset": "openai", "api_key": "k2"})
        assert r.status == 200, await r.text()
        assert [e.id for e in env.app.get_settings().jev_endpoints] == ["or", "oa"]

    @pytest.mark.asyncio
    async def test_validations(self, env) -> None:
        r = await env.client.put("/api/settings/jev/endpoints/Bad", json={"url": "https://x.test/v1/systemone", "model": "m"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/x", json={"preset": "nope", "url": "https://x.test/v1/systemone", "model": "m"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/x", json={"protocol": "nope", "url": "https://x.test/v1/systemone", "model": "m"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/x", json={"url": "http://api.test/v1/systemone", "model": "m"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/x", json={"url": "https://x.test/v1/systemone", "model": ""})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/endpoints/cf", json={"preset": "cloudflare", "api_key": "k"})
        assert r.status == 400
        assert "account_id" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_local_http_allowed(self, env) -> None:
        r = await env.client.put("/api/settings/jev/endpoints/local", json={
            "url": "http://127.0.0.1:8080/v1/systemone", "model": "local-jev", "api_key": "k",
        })
        assert r.status == 200, await r.text()
        assert env.app.get_settings().jev_endpoints[0].url == "http://127.0.0.1:8080/v1/systemone"

    @pytest.mark.asyncio
    async def test_wrong_origin_blocked(self, env) -> None:
        assert (await _create_or(env)).status == 200
        r = await env.client.put(
            "/api/settings/jev/endpoints/or", json={"name": "x"},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status in (403, 400)
        assert env.app.get_settings().jev_endpoints[0].name == "OpenRouter"


class TestJevDelete:
    @pytest.mark.asyncio
    async def test_builtin_refused(self, env) -> None:
        r = await env.client.delete("/api/settings/jev/endpoints/typesafe")
        assert r.status == 400
        assert "内置" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_in_use_refused(self, env) -> None:
        await _create_or(env)
        await env.client.put("/api/settings/jev/use", json={"id": "or"})
        r = await env.client.delete("/api/settings/jev/endpoints/or")
        assert r.status == 400
        assert "正在用" in (await r.json())["error"]
        assert len(env.app.get_settings().jev_endpoints) == 1

    @pytest.mark.asyncio
    async def test_unknown_404(self, env) -> None:
        r = await env.client.delete("/api/settings/jev/endpoints/ghost")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_delete_works(self, env) -> None:
        await _create_or(env)
        r = await env.client.delete("/api/settings/jev/endpoints/or")
        assert r.status == 200, await r.text()
        assert env.app.get_settings().jev_endpoints == ()
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert "jev_endpoints" not in text


class TestJevUsePut:
    @pytest.mark.asyncio
    async def test_switch_and_persist(self, env) -> None:
        await _create_or(env)
        r = await env.client.put("/api/settings/jev/use", json={"id": "or"})
        assert r.status == 200, await r.text()
        assert env.app.get_settings().jev.use == "or"
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert 'use = "or"' in text
        assert (await r.json())["use"] == "or"
        r = await env.client.put("/api/settings/jev/use", json={"id": "typesafe"})
        assert r.status == 200 and env.app.get_settings().jev.use == "typesafe"

    @pytest.mark.asyncio
    async def test_unknown_and_missing_rejected(self, env) -> None:
        r = await env.client.put("/api/settings/jev/use", json={"id": "ghost"})
        assert r.status == 400
        r = await env.client.put("/api/settings/jev/use", json={})
        assert r.status == 400
        assert env.app.get_settings().jev.use == "typesafe"


class TestJevTestRoute:
    @pytest.mark.asyncio
    async def test_test_builtin_ok(self, env) -> None:
        await env.client.put("/api/settings/jev/endpoints/typesafe", json={"api_key": SECRET})
        r = await env.client.post("/api/settings/jev/endpoints/typesafe/test", json={})
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["ok"] is True and isinstance(data["ms"], int) and data["error"] == ""
        assert data["answers"] == {"greeting": 0.9, "lang": ["zh", 0.9, 0.8]}
        call = env.tr.seen[-1]
        assert call["url"] == "https://api.typesafe.ai/v1/systemone"
        assert call["headers"].get("authorization") == f"Bearer {SECRET}"
        assert SECRET not in json.dumps(data, ensure_ascii=False)
        # 测试不动熔断、不写 judgments
        assert env.app.jev.available() is True
        assert env.app.jev.calls_today() == 0

    @pytest.mark.asyncio
    async def test_test_custom_endpoint_openai(self, env) -> None:
        env.tr.payload = dict(OPENAI_OK)
        await env.client.put("/api/settings/jev/endpoints/oa", json={"preset": "openai", "api_key": "oa-key"})
        r = await env.client.post("/api/settings/jev/endpoints/oa/test", json={})
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["ok"] is True
        call = env.tr.seen[-1]
        assert call["url"] == "https://api.openai.com/v1/decisions"
        assert call["body"]["questions"][0]["type"] == "predicate"

    @pytest.mark.asyncio
    async def test_test_unsaved_values(self, env) -> None:
        await _create_or(env)
        r = await env.client.post("/api/settings/jev/endpoints/or/test", json={
            "url": "https://other.test/v1/systemone", "model": "m2", "api_key": SECRET2,
            "protocol": "systemone",
        })
        assert r.status == 200, await r.text()
        call = env.tr.seen[-1]
        assert call["url"] == "https://other.test/v1/systemone"
        assert call["headers"].get("authorization") == f"Bearer {SECRET2}"
        assert call["body"]["model"] == "m2"
        assert SECRET2 not in json.dumps(await r.json(), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_no_key_says_fill_key(self, env) -> None:
        r = await env.client.post("/api/settings/jev/endpoints/typesafe/test", json={})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is False and data["error"] == "先填密钥"
        assert env.tr.seen == []

    @pytest.mark.asyncio
    async def test_unknown_without_url_404(self, env) -> None:
        r = await env.client.post("/api/settings/jev/endpoints/ghost/test", json={})
        assert r.status == 404
        r = await env.client.post("/api/settings/jev/endpoints/ghost/test", json={
            "url": "https://x.test/v1/systemone", "model": "m", "api_key": "k",
        })
        assert r.status == 200 and (await r.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_bad_protocol_and_url_400(self, env) -> None:
        r = await env.client.post("/api/settings/jev/endpoints/typesafe/test",
                                  json={"protocol": "nope", "url": "https://x.test/v1/systemone"})
        assert r.status == 400
        r = await env.client.post("/api/settings/jev/endpoints/typesafe/test",
                                  json={"url": "http://api.test/v1/systemone"})
        assert r.status == 400
        r = await env.client.post("/api/settings/jev/endpoints/typesafe/test",
                                  json={"url": "https://api.cloudflare.com/{account_id}/x"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_upstream_error_not_500(self, env) -> None:
        env.tr.status = 500
        env.tr.payload = {"error": "爆炸"}
        await env.client.put("/api/settings/jev/endpoints/typesafe", json={"api_key": SECRET})
        r = await env.client.post("/api/settings/jev/endpoints/typesafe/test", json={})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is False and data["error"] and "http_500" in data["error"]
        assert SECRET not in json.dumps(data, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_requires_admin(self, env) -> None:
        # 匿名（新 client，不带登录 cookie）试测试接口
        server = TestServer(env.app.console.app)
        client = TestClient(server)
        await client.start_server()
        try:
            r = await client.post("/api/settings/jev/endpoints/typesafe/test", json={})
            assert r.status == 401
        finally:
            await client.close()


class TestKnownSecrets:
    @pytest.mark.asyncio
    async def test_custom_endpoint_key_is_masked(self, env) -> None:
        await _create_or(env)
        assert SECRET in env.app.known_secrets()
        assert env.app.get_settings().jev_endpoints[0].api_key == SECRET
