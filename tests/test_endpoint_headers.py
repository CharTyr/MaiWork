"""端点请求头覆盖（高级设置）回归测试。

功能约定（需求，主会话定的为准）：
- config.toml 的每个 [[endpoints]] 可以带 `headers` 表（dict[str, str]），规范化进
  EndpointSetting.headers（只读 Mapping，默认空）。
- 实际请求：覆盖同名请求头（大小写不敏感）替代默认头，不追加重复；三协议
  openai / responses（含 SSE）/ anthropic 都同样生效；测试连接 / 拉模型列表 /
  verify_entry 同样吃端点 headers；备用候选切到别的端点时各用各的头。
- 头是凭据：HTTP 错误、重试/回落日志、usage/model_calls 的错误、verify 返回、
  测试连接响应里绝不露任何值（含非 Bearer 的自定义值）；ModelError.message 也不裸。
- verification_stamp 指纹含 headers 指纹（不放明文）：改头 → 旧验证失效。
- GET /api/settings/endpoints 只回 header_names 名字清单，绝不回值。
- PUT：
  - 不给 "headers" 键 → 原样保留；
  - 给了对象 = 完整名单：已存同名（大小写不敏感）且新值是空串 / null → 保留旧值；
    新名字的值必须是非空 string；不在对象里的头删（{} = 清空）；
  - 头名必须是合法 HTTP 头名（可打印 ASCII，禁空格/冒号/控制字符），值允许可打印
    ASCII + \t，禁换行 / 控制字符 / DEL / 非 ASCII；错误消息不含值；大小写重复报错；
  - Host / Content-Length / Transfer-Encoding / Connection / Proxy-Authorization 等
    传输/代理头一律拒（中文说明）。
- POST /test：
  - 临时 headers 只用在这次测试，绝不保存进配置或 kv（kv 里不含任何头值）；
  - 临时 headers 的名字同样覆盖协议默认头，遮罩也覆盖临时值；
  - 临时 headers 校验同 PUT（坏头拒 400 中文，错误不含值）。
- 保存 / 删除别的端点时，已有端点的 headers 原样保留。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import config as cfg
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import EndpointSetting, load_settings
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store

PASSWORD = "端点头密码-显眼"
G1 = "900000001"
SECRET = "hsecret-AAABBBCCC"
CUSTOM_A = "tok-alpha-12345"  # e1 的自定义头值（非 Bearer）
CUSTOM_B = "tok-beta-67890"   # e-b 的自定义头值


# ----------------------------------------------------------------------
# 纯配置层
# ----------------------------------------------------------------------


def _one_endpoint(**kw):
    raw = {
        "endpoints": [
            {
                "id": "e1", "protocol": "openai",
                "base_url": "https://a.test/v1", "api_key": SECRET,
                **kw,
            }
        ],
    }
    settings, problems = load_settings(raw)
    return settings, problems


class TestConfigHeaders:

    def test_default_empty_and_readonly(self) -> None:
        settings, problems = _one_endpoint()
        assert problems == []
        ep = settings.endpoints[0]
        assert isinstance(ep.headers, MappingProxyType) or not isinstance(ep.headers, dict)
        assert len(ep.headers) == 0
        with pytest.raises((AttributeError, TypeError)):
            ep.headers["X"] = "y"  # type: ignore[index]

    def test_parses_and_strips(self) -> None:
        settings, problems = _one_endpoint(headers={"X-Token": "abc", " X-Other ": "  v2"})
        assert problems == []
        heads = settings.endpoints[0].headers
        assert dict(heads) == {"X-Token": "abc", "X-Other": "v2"}

    def test_non_table_dropped_with_problem(self) -> None:
        settings, problems = _one_endpoint(headers=["X-Token: abc"])
        ep = settings.endpoints[0]
        assert len(ep.headers) == 0
        assert any("headers" in p and "e1" in p for p in problems)

    def test_name_character_rules(self) -> None:
        for bad in ("Bearer Token", "X Token", "X:Token", "中开头", "München"):
            settings, problems = _one_endpoint(headers={bad: "v"})
            assert any("headers" in p for p in problems), f"{bad!r} 该被拒"
            assert len(settings.endpoints[0].headers) == 0

    def test_name_length_limit(self) -> None:
        key = "X" * 129
        settings, problems = _one_endpoint(headers={key: "v"})
        assert any("headers" in p and "128" in p for p in problems)
        settings2, problems2 = _one_endpoint(headers={"X" * 128: "v"})
        assert problems2 == []

    def test_value_rules(self) -> None:
        # 非 ASCII / 换行 / 控制 / DEL 拒
        for bad in ("中值", "line\nfeed", "line\rfeed", "ctrl\x00char", "dea\x7fl"):
            settings, problems = _one_endpoint(headers={"X-Token": bad})
            assert any("headers" in p for p in problems), f"{bad!r} 该被拒"
        # 允许可打印 ASCII + tab + 空格在值里
        settings, problems = _one_endpoint(headers={"X-Token": "abc\tdef ghi  "})
        assert problems == []
        assert settings.endpoints[0].headers["X-Token"] == "abc\tdef ghi"

    def test_value_length_limit(self) -> None:
        val = "v" * 8193
        settings, problems = _one_endpoint(headers={"X-Token": val})
        assert any("headers" in p and "8192" in p for p in problems)

    def test_count_limit(self) -> None:
        too_many = {f"X-{i}": "v" for i in range(33)}
        settings, problems = _one_endpoint(headers=too_many)
        assert any("headers" in p and "32" in p for p in problems)

    def test_value_requires_string(self) -> None:
        settings, problems = _one_endpoint(headers={"X-Token": 123})
        assert any("headers" in p for p in problems)


# ----------------------------------------------------------------------
# 校验纯函数（网页/配置共用一套规则）
# ----------------------------------------------------------------------


class TestValidateHelpers:

    def test_valid_token_names(self) -> None:
        assert cfg.validate_endpoint_header_name("Authorization")[0] == ""
        assert cfg.validate_endpoint_header_name("X-Custom_Thing.9")[0] == ""

    def test_name_empty_and_limits(self) -> None:
        err, _ = cfg.validate_endpoint_header_name("   ")
        assert "空" in err
        err, _ = cfg.validate_endpoint_header_name("X" * 129)
        assert "128" in err

    def test_name_character_rejected_with_chinese(self) -> None:
        for bad in ("Bad Name", "Bad:Name", "Fünf", "中名", "na\x01me"):
            err, _ = cfg.validate_endpoint_header_name(bad)
            assert err, f"{bad!r} 该被拒"

    def test_transport_headers_blocked(self) -> None:
        for name in (
            "Host", "Content-Length", "Transfer-Encoding", "Connection",
            "Proxy-Authorization", "Proxy-Connection", "Keep-Alive", "TE", "Trailer", "Upgrade",
        ):
            err, _ = cfg.validate_endpoint_header_name(name)
            assert err and ("传输" in err or "代理" in err), f"{name!r} 该被拒"

    def test_value_rules_bounded(self) -> None:
        assert cfg.validate_endpoint_header_value("") == ""
        assert cfg.validate_endpoint_header_value("abc\t \tdef ") == ""
        assert cfg.validate_endpoint_header_value("line\nfeed")
        assert cfg.validate_endpoint_header_value("ctrl\x00char")
        assert cfg.validate_endpoint_header_value("dea\x7fl")
        assert cfg.validate_endpoint_header_value("中值")
        long_err = cfg.validate_endpoint_header_value("v" * 8193)
        assert "8192" in long_err

    def test_error_messages_never_leak_value(self) -> None:
        bad = "top-secret-value"
        err = cfg.validate_endpoint_header_value(bad + "\ninject")
        assert err and bad not in err
        err_name, _ = cfg.validate_endpoint_header_name("Bad Name" + bad)
        assert err_name and bad not in err_name

    def test_validate_headers_dict(self) -> None:
        pairs, problems = cfg.validate_endpoint_headers_pairs(
            {"X-A": "1", "X-B": "2"}, ["e1"]
        )
        assert problems == [] and pairs == [("X-A", "1"), ("X-B", "2")]
        pairs, problems = cfg.validate_endpoint_headers_pairs({"x-a": "1", "X-A": "2"}, ["e1"])
        assert problems and any("大小写" in p for p in problems)
        pairs, problems = cfg.validate_endpoint_headers_pairs({"X-Token": "中值"}, ["e1"])
        assert problems and "中值" not in problems[0]


# ----------------------------------------------------------------------
# 三协议请求头合并 + 备用隔离 + 遮罩 + 列表 + 验证
# ----------------------------------------------------------------------


def _chat_settings(endpoints, model_list):
    raw = {"endpoints": endpoints, "model_list": model_list}
    settings, problems = load_settings(raw)
    assert problems == []
    return settings


def _make_models(tmp_path: Path, settings, handler, *, model_id: str = "m1"):
    store = Store(tmp_path / "t.db")
    store.migrate()
    agents = Agents(store, lambda: settings)
    if model_id:
        agents.update_profile("main", {"model": model_id})
    models = Models(store, lambda: settings, transport=httpx.MockTransport(handler), agents=agents)
    return store, models


def _sse_reply(text: str = "ok") -> httpx.Response:
    body = (
        'data: {"choices":[{"delta":{"content":"' + text + '"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _endpoints_two():
    return [
        {"id": "e-p", "protocol": "openai", "base_url": "https://p.test/v1", "api_key": "key-ppppp",
         "headers": {"X-Custom-A": CUSTOM_A, "User-Agent": "MaiWork/42"}},
        {"id": "e-b", "protocol": "openai", "base_url": "https://b.test/v1", "api_key": "key-bbbbb",
         "headers": {"X-Custom-B": CUSTOM_B}},
    ]


class TestChatHeaderOverride:
    @pytest.mark.asyncio
    async def test_openai_chat_headers_merge_case_insensitive(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "openai", "base_url": "https://p.test/v1", "api_key": SECRET,
              "headers": {"X-Token": CUSTOM_A, "user-agent": "MaiWorkTest/9", "AUTHORIZATION": "Bearer special"}}],
            [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
        )
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return _sse_reply("ok")

        store, models = _make_models(tmp_path, settings, handler)
        try:
            r = await models.chat("main", [{"role": "user", "content": "hi"}], retries=0)
        finally:
            await models.close(); store.close()
        assert r.text == "ok"
        heads = seen[-1].headers
        # 覆盖：同名字（不同大小写）只留一份，新值生效
        auths = heads.get_list("authorization")
        assert auths == ["Bearer special"]
        assert heads.get("user-agent") == "MaiWorkTest/9"
        assert heads.get("x-token") == CUSTOM_A

    @pytest.mark.asyncio
    async def test_responses_sse_headers(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "responses", "base_url": "https://p.test", "api_key": SECRET,
              "headers": {"X-Token": CUSTOM_A, "Authorization": "Bearer zzz-custom"}}],
            [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
        )
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            body = (
                'data: {"type":"response.completed","response":{"status":"completed",'
                '"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],'
                '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        store, models = _make_models(tmp_path, settings, handler)
        try:
            r = await models.chat("main", [{"role": "user", "content": "hi"}], retries=0)
        finally:
            await models.close(); store.close()
        assert r.text == "ok"
        heads = seen[-1].headers
        assert heads.get_list("authorization") == ["Bearer zzz-custom"]
        assert heads.get("x-token") == CUSTOM_A

    @pytest.mark.asyncio
    async def test_anthropic_headers(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "anthropic", "base_url": "https://c.test", "api_key": "k-ant",
              "headers": {"X-Api-Key": CUSTOM_A, "Anthropic-Version": "2999-01-01"}}],
            [{"id": "m1", "endpoint": "e1", "model": "claude-x"}],
        )
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={
                "type": "message", "role": "assistant", "id": "m",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn", "model": "claude-x",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            })

        store, models = _make_models(tmp_path, settings, handler)
        try:
            r = await models.chat("main", [{"role": "user", "content": "hi"}], retries=0)
        finally:
            await models.close(); store.close()
        assert r.text == "ok"
        heads = seen[-1].headers
        assert heads.get_list("x-api-key") == [CUSTOM_A]
        assert heads.get_list("anthropic-version") == ["2999-01-01"]

    @pytest.mark.asyncio
    async def test_backup_switch_headers_do_not_leak_across_endpoints(self, tmp_path) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            if "p.test" in str(req.url):
                return httpx.Response(500, json={"error": {"message": "p 爆炸"}})
            return _sse_reply("backup ok")

        settings = _chat_settings(
            _endpoints_two(),
            [{"id": "m1", "endpoint": "e-p", "model": "m-p"},
             {"id": "m2", "endpoint": "e-b", "model": "m-b"}],
        )
        store = Store(tmp_path / "t.db")
        store.migrate()
        agents = Agents(store, lambda: settings)
        agents.update_profile("main", {"model": "m1", "backup": "m2"})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(handler), agents=agents)
        try:
            r = await models.chat("main", [{"role": "user", "content": "hi"}], retries=0)
        finally:
            await models.close(); store.close()
        assert r.text == "backup ok"
        assert len(seen) == 2
        first, second = seen[0].headers, seen[1].headers
        assert first.get("x-custom-a") == CUSTOM_A and first.get("x-custom-b") is None
        assert second.get("x-custom-b") == CUSTOM_B and second.get("x-custom-a") is None
        # 两次的 Authorization 各自是自己的（没被 custom-A 串过去）
        assert first.get("authorization") == "Bearer key-ppppp"
        assert second.get("authorization") == "Bearer key-bbbbb"


class TestListModelsAndVerify:
    @pytest.mark.asyncio
    async def test_list_models_openai_merges_headers(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "openai", "base_url": "https://p.test/v1", "api_key": SECRET,
              "headers": {"X-Token": CUSTOM_A, "Authorization": "Bearer zzz"}}],
            [],
        )
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m-a"}]})

        store, models = _make_models(tmp_path, settings, handler, model_id=None)
        try:
            out = await models.list_models("https://p.test/v1", SECRET, protocol="openai")
        finally:
            await models.close(); store.close()
        assert out == ["m-a"]
        heads = seen[-1].headers
        assert heads.get_list("authorization") == ["Bearer zzz"]
        assert heads.get("x-token") == CUSTOM_A

    @pytest.mark.asyncio
    async def test_list_models_anthropic_merges_headers(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "anthropic", "base_url": "https://c.test", "api_key": "k-ant",
              "headers": {"x-api-key": CUSTOM_A, "anthropic-version": "2999-01-01"}}],
            [],
        )
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"models": [{"id": "claude-a"}]})

        store, models = _make_models(tmp_path, settings, handler, model_id=None)
        try:
            out = await models.list_models("https://c.test", "k-ant", protocol="anthropic")
        finally:
            await models.close(); store.close()
        assert out == ["claude-a"]
        heads = seen[-1].headers
        assert heads.get_list("x-api-key") == [CUSTOM_A]
        assert heads.get_list("anthropic-version") == ["2999-01-01"]

    def _verify_settings(self) -> object:
        settings, problems = load_settings({
            "endpoints": [{"id": "default", "base_url": "https://p.test/v1", "api_key": "sk-test",
                           "headers": {"X-Token": CUSTOM_A}}],
            "model_list": [{"id": "m1", "endpoint": "default", "model": "gpt-x"}],
        })
        assert problems == []
        return settings

    @pytest.mark.asyncio
    async def test_verify_uses_headers_and_stamp_changes(self, tmp_path) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            body = json.loads(req.content)
            msg: dict = {"role": "assistant", "content": "OK"}
            if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
                msg = {"role": "assistant", "content": "",
                       "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "maiwork_ping", "arguments": '{"word":"hi"}'}}]}
            return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": msg}],
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = self._verify_settings()
        agents = Agents(store, lambda: settings)
        models = Models(store, lambda: settings, agents=agents, transport=httpx.MockTransport(handler))
        try:
            stamp_before = models.verification_stamp("m1")
            r = await models.verify_entry("m1")
        finally:
            await models.close(); store.close()
        assert r["ok"] is True, r
        heads = seen[-1].headers
        assert heads.get("x-token") == CUSTOM_A
        # 改 headers → 旧验证失效（签名变）
        settings2, problems2 = load_settings({
            "endpoints": [{"id": "default", "base_url": "https://p.test/v1", "api_key": "sk-test",
                           "headers": {"X-Token": CUSTOM_B}}],
            "model_list": [{"id": "m1", "endpoint": "default", "model": "gpt-x"}],
        })
        assert problems2 == []
        store2 = Store(tmp_path / "t2.db")
        store2.migrate()
        models2 = Models(store2, lambda: settings2, agents=Agents(store2, lambda: settings2),
                         transport=httpx.MockTransport(handler))
        try:
            stamp_after = models2.verification_stamp("m1")
        finally:
            await models2.close(); store2.close()
        assert stamp_after != stamp_before


class TestSecretMasking:
    @pytest.mark.asyncio
    async def test_error_and_logs_mask_custom_header_values(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "openai", "base_url": "https://p.test/v1", "api_key": SECRET,
              "headers": {"X-Token": CUSTOM_A}}],
            [{"id": "m1", "endpoint": "e1", "model": "m1"}],
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": f"bad head {CUSTOM_A} / {SECRET}"}})

        store, models = _make_models(tmp_path, settings, handler)
        try:
            with pytest.raises(Exception) as ei:
                await models.chat("main", [{"role": "user", "content": "hi"}], retries=0)
        finally:
            await models.close()
        assert CUSTOM_A not in str(ei.value)
        assert SECRET not in str(ei.value)
        rows_usage = [dict(r) for r in store.read().execute("SELECT * FROM usage ORDER BY id").fetchall()]
        rows_calls = [dict(r) for r in store.read().execute("SELECT * FROM model_calls ORDER BY id").fetchall()]
        for row in rows_usage + rows_calls:
            whole = json.dumps(row, ensure_ascii=False)
            assert CUSTOM_A not in whole
            assert SECRET not in whole
        store.close()

    @pytest.mark.asyncio
    async def test_list_models_marks_all_header_values(self, tmp_path) -> None:
        settings = _chat_settings(
            [{"id": "e1", "protocol": "openai", "base_url": "https://p.test/v1", "api_key": SECRET,
              "headers": {"X-Token": CUSTOM_A}}],
            [],
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": f"no {CUSTOM_A} / {SECRET}"})

        store, models = _make_models(tmp_path, settings, handler, model_id=None)
        try:
            from CharTyr_MaiWork.maiwork.models import ModelError
            with pytest.raises(ModelError) as ei:
                await models.list_models("https://p.test/v1", SECRET, protocol="openai")
        finally:
            await models.close(); store.close()
        msg = str(ei.value)
        assert CUSTOM_A not in msg and SECRET not in msg


# ----------------------------------------------------------------------
# 网页 API（GET / PUT / TEST / DELETE）
# ----------------------------------------------------------------------


def _raw_api_config(data_dir: Path, *, endpoints=None):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
    }
    if endpoints is not None:
        raw["endpoints"] = endpoints
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
                    if isinstance(v, dict):
                        sub = tomlkit.table()
                        for sk, sv in v.items():
                            sub[sk] = sv
                        t[k] = sub
                    else:
                        t[k] = v
                aot.append(t)
            doc[section] = aot
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


class _FakeListTransport:
    def __init__(self, payload=None, status: int = 200) -> None:
        self.seen: list[dict] = []
        self.payload = payload if payload is not None else {"object": "list", "data": [{"id": "m-a"}]}
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append({
            "url": str(request.url),
            "headers": dict(request.headers),
        })
        return httpx.Response(self.status, json=self.payload)


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = _raw_api_config(data_dir)
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


class TestApiGet:

    @pytest.mark.asyncio
    async def test_get_only_header_names_never_values(self, env) -> None:
        r = await env.client.put(
            "/api/settings/endpoints/e1",
            json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                  "headers": {"X-Token": CUSTOM_A, "Authorization": "Bearer zzz"}},
        )
        assert r.status == 200, await r.text()
        r = await env.client.get("/api/settings/endpoints")
        assert r.status == 200
        data = await r.json()
        e0 = next(e for e in data["endpoints"] if e["id"] == "e1")
        assert e0.get("header_names") == ["X-Token", "Authorization"]
        assert CUSTOM_A not in json.dumps(data, ensure_ascii=False)
        assert "zzz" not in json.dumps(data, ensure_ascii=False)


class TestApiPut:

    @pytest.mark.asyncio
    async def test_put_absent_keeps_existing(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": CUSTOM_A}})
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "name": "改名"})
        assert r.status == 200
        ep = env.app.get_settings().endpoints[0]
        assert dict(ep.headers) == {"X-Token": CUSTOM_A}

    @pytest.mark.asyncio
    async def test_put_object_is_full_set(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-A": "1", "X-B": "2"}})
        r = await env.client.put("/api/settings/endpoints/e1",
                                 json={"id": "e1", "headers": {"X-C": "3"}})
        assert r.status == 200
        heads = dict(env.app.get_settings().endpoints[0].headers)
        assert heads == {"X-C": "3"}

    @pytest.mark.asyncio
    async def test_put_empty_clears(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": CUSTOM_A}})
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "headers": {}})
        assert r.status == 200
        assert len(env.app.get_settings().endpoints[0].headers) == 0

    @pytest.mark.asyncio
    async def test_put_same_name_ci_blank_keeps_old_value(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": "old-value-1"}})
        r = await env.client.put("/api/settings/endpoints/e1",
                                 json={"id": "e1", "headers": {"x-token": ""}})
        assert r.status == 200
        assert dict(env.app.get_settings().endpoints[0].headers) == {"x-token": "old-value-1"}
        # null 也一样保留
        r = await env.client.put("/api/settings/endpoints/e1",
                                 json={"id": "e1", "headers": {"X-TOKEN": None}})
        assert r.status == 200
        assert dict(env.app.get_settings().endpoints[0].headers) == {"X-TOKEN": "old-value-1"}

    @pytest.mark.asyncio
    async def test_put_new_name_requires_non_empty_string(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                      "headers": {"X-New": ""}})
        assert r.status == 400
        data = await r.json()
        assert "error" in data
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "headers": {"X-New": None}})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_case_duplicate_rejected(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1",
                                 json={"id": "e1", "base_url": "https://a.test/v1",
                                       "headers": {"x-a": "1", "X-A": "2"}})
        assert r.status == 400
        assert "大小写" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_put_transport_headers_rejected(self, env) -> None:
        for name in ("Host", "Content-Length", "Transfer-Encoding", "Connection", "Proxy-Authorization"):
            r = await env.client.put("/api/settings/endpoints/e1",
                                     json={"id": "e1", "base_url": "https://a.test/v1",
                                           "headers": {name: "v"}})
            assert r.status == 400, f"{name} 该被拒"

    @pytest.mark.asyncio
    async def test_put_error_message_never_leaks_value(self, env) -> None:
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1",
                                                                      "headers": {"X-Token": "line\ninject"}})
        assert r.status == 400
        whole = json.dumps(await r.json(), ensure_ascii=False)
        assert "line" not in whole and "inject" not in whole

    @pytest.mark.asyncio
    async def test_save_other_endpoint_does_not_drop_headers(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": CUSTOM_A}})
        r = await env.client.put("/api/settings/endpoints/e2", json={"id": "e2", "base_url": "https://b.test/v1", "api_key": "k2"})
        assert r.status == 200
        eps = {e.id: dict(e.headers) for e in env.app.get_settings().endpoints}
        assert eps["e1"] == {"X-Token": CUSTOM_A}
        r = await env.client.delete("/api/settings/endpoints/e2")
        assert r.status == 200
        eps = {e.id: dict(e.headers) for e in env.app.get_settings().endpoints}
        assert eps["e1"] == {"X-Token": CUSTOM_A}


class TestApiTest:

    @pytest.mark.asyncio
    async def test_test_with_saved_headers(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": CUSTOM_A}})
        r = await env.client.post("/api/settings/endpoints/e1/test", json={})
        assert r.status == 200
        heads = env.app.models._transport.handler.seen[-1]["headers"]
        assert heads.get("x-token") == CUSTOM_A

    @pytest.mark.asyncio
    async def test_test_temporary_headers_applied_not_saved(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        r = await env.client.post(
            "/api/settings/endpoints/e1/test",
            json={"headers": {"X-Tmp": "tmp-value-abc", "Authorization": "Bearer tmp-override"}},
        )
        assert r.status == 200
        heads = env.app.models._transport.handler.seen[-1]["headers"]
        assert heads.get("x-tmp") == "tmp-value-abc"
        assert heads.get("authorization") == "Bearer tmp-override"
        # 绝对不保存：config 里不留、kv 里不留
        ep = env.app.get_settings().endpoints[0]
        assert len(ep.headers) == 0
        checked = env.app.store.kv_get("endpoints.checked.e1") or {}
        assert "tmp-value-abc" not in json.dumps(checked, ensure_ascii=False)
        assert "tmp-override" not in json.dumps(checked, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_test_saved_endpoint_headers_used_when_body_absent(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": CUSTOM_A}})
        r = await env.client.post("/api/settings/endpoints/e1/test", json={})
        heads = env.app.models._transport.handler.seen[-1]["headers"]
        assert heads.get("x-token") == CUSTOM_A

    @pytest.mark.asyncio
    async def test_test_error_masked_saved_and_temp_values(self, env) -> None:
        tr = env.app.models._transport
        tr.handler.status = 500
        tr.handler.payload = {"error": f"fail {CUSTOM_A} tmpv99 {SECRET}"}
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET,
                                                                  "headers": {"X-Token": CUSTOM_A}})
        r = await env.client.post("/api/settings/endpoints/e1/test",
                                  json={"headers": {"X-Tmp": "tmpv99"}})
        assert r.status == 200
        data = await r.json()
        whole = json.dumps(data, ensure_ascii=False)
        assert data["ok"] is False
        assert CUSTOM_A not in whole and "tmpv99" not in whole and SECRET not in whole
        # 复位
        tr.handler.status = 200
        tr.handler.payload = None

    @pytest.mark.asyncio
    async def test_test_invalid_temp_headers_rejected(self, env) -> None:
        await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://a.test/v1", "api_key": SECRET})
        r = await env.client.post("/api/settings/endpoints/e1/test",
                                  json={"headers": {"Bad Name": "v"}})
        assert r.status == 400
        assert (await r.json())["error"]
        r = await env.client.post("/api/settings/endpoints/e1/test",
                                  json={"headers": {"X-T": "中值"}})
        assert r.status == 400
        r = await env.client.post("/api/settings/endpoints/e1/test",
                                  json={"headers": {"Host": "v"}})
        assert r.status == 400
        r = await env.client.post("/api/settings/endpoints/e1/test",
                                  json={"headers": {"X-New": ""}})
        assert r.status == 400
