"""生图模型的网页接口测试（真 app + console，本机回环，不发外网）。

覆盖：
- PUT /api/settings/model-list/{id} 能存 use="image" / image_api / 尺寸 / 张数，GET 读得回来；
- 不认识的值 → 400 中文原因（不静默回落），config.toml 不变；
- use="image" 的条目不能当专岗模型（PUT /api/agents/{kind} → 400）；
- 生图预设清单 GET /api/settings/imagegen（不含任何密钥）；
- 「用哪个生图模型」选择：PUT /api/settings/imagegen，只认 use="image" 的条目。
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp

PASSWORD = "生图网页密码-显眼"
G1 = "900000001"
SECRET = "sk-imagegen-endpoint-DO-NOT-ECHO"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _image_transport(request: httpx.Request) -> httpx.Response:
    """假生图端点：POST 回一张 base64 图（不发真网络）。"""
    import base64

    if request.method == "POST":
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    return httpx.Response(200, json={"data": []})


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


def _raw(data_dir: Path, root: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "environments": {"local_mode": "direct", "workspace_root": str(root / "ws-root")},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
        "endpoints": [
            {"id": "e1", "name": "一号端点", "protocol": "openai",
             "base_url": "https://api.test/v1", "api_key": SECRET},
        ],
        "model_list": [
            {"id": "m1", "endpoint": "e1", "model": "gpt-x", "name": "聊天"},
            {"id": "img1", "endpoint": "e1", "model": "gpt-image-1", "name": "生图",
             "use": "image", "image_api": "openai", "image_max_n": 2},
        ],
    }


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = _raw(data_dir, tmp_path)
    _write_plugin_dir(plug_dir, raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    app.http_transport = httpx.MockTransport(_image_transport)
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200
    yield type("E", (), {"app": app, "client": client, "plug_dir": plug_dir, "data_dir": data_dir})()
    await client.close()
    await app.stop()


def _model(view: dict, mid: str) -> dict:
    return next(m for m in view["models"] if m["id"] == mid)


class TestModelEntryFields:
    @pytest.mark.asyncio
    async def test_get_returns_use_and_image_fields(self, env) -> None:
        r = await env.client.get("/api/settings/endpoints")
        assert r.status == 200
        view = await r.json()
        img = _model(view, "img1")
        assert img["use"] == "image"
        assert img["image_api"] == "openai"
        assert img["image_default_size"] == ""
        assert img["image_max_n"] == 2
        chat = _model(view, "m1")
        assert chat["use"] == "chat"

    @pytest.mark.asyncio
    async def test_put_image_entry_roundtrip(self, env) -> None:
        r = await env.client.put("/api/settings/model-list/img2", json={
            "endpoint": "e1", "model": "Kwai-Kolors/Kolors", "name": "硅基流动生图",
            "use": "image", "image_api": "siliconflow",
            "image_default_size": "1024x768", "image_max_n": 4,
        })
        assert r.status == 200, await r.text()
        view = await r.json()
        img = _model(view, "img2")
        assert (img["use"], img["image_api"], img["image_default_size"], img["image_max_n"]) == (
            "image", "siliconflow", "1024x768", 4,
        )
        # 写进文件了（不是只在内存）
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert 'use = "image"' in text and 'image_api = "siliconflow"' in text
        # 再读一次也还在（往返不丢字段）
        view2 = await (await env.client.get("/api/settings/endpoints")).json()
        assert _model(view2, "img2")["image_api"] == "siliconflow"

    @pytest.mark.asyncio
    async def test_edit_chat_entry_to_image(self, env) -> None:
        r = await env.client.put("/api/settings/model-list/m1", json={
            "endpoint": "e1", "model": "gpt-image-1", "use": "image", "image_api": "openai",
        })
        assert r.status == 200, await r.text()
        assert _model(await r.json(), "m1")["use"] == "image"

    @pytest.mark.asyncio
    async def test_unknown_use_rejected_400(self, env) -> None:
        before = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        r = await env.client.put("/api/settings/model-list/m1", json={
            "endpoint": "e1", "model": "gpt-x", "use": "画图",
        })
        assert r.status == 400, await r.text()
        body = await r.json()
        assert "use" in body["error"]
        assert (env.plug_dir / "config.toml").read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    async def test_unknown_image_api_rejected_400(self, env) -> None:
        r = await env.client.put("/api/settings/model-list/m1", json={
            "endpoint": "e1", "model": "gpt-x", "use": "image", "image_api": "gemini",
        })
        assert r.status == 400, await r.text()
        body = await r.json()
        assert "image_api" in body["error"] and "gemini" in body["error"]

    @pytest.mark.asyncio
    async def test_image_max_n_out_of_range_rejected_400(self, env) -> None:
        r = await env.client.put("/api/settings/model-list/m1", json={
            "endpoint": "e1", "model": "gpt-x", "use": "image", "image_max_n": 99,
        })
        assert r.status == 400, await r.text()
        assert "image_max_n" in (await r.json())["error"]


class TestImageEntryIsNotAChatModel:
    @pytest.mark.asyncio
    async def test_agents_profile_rejects_image_entry_400(self, env) -> None:
        r = await env.client.put("/api/agents/main", json={"model": "img1"})
        assert r.status == 400, await r.text()
        assert "模型" in (await r.json())["error"]
        # 聊天条目照样能选
        r2 = await env.client.put("/api/agents/main", json={"model": "m1"})
        assert r2.status == 200, await r2.text()


class TestPresets:
    @pytest.mark.asyncio
    async def test_presets_listed_without_secrets(self, env) -> None:
        r = await env.client.get("/api/settings/imagegen")
        assert r.status == 200
        view = await r.json()
        presets = view.get("presets")
        assert isinstance(presets, list) and presets, view.keys()
        ids = {p["id"] for p in presets}
        assert {"openai", "ark", "siliconflow"} <= ids
        blob = await r.text()
        assert SECRET not in blob
        for p in presets:
            assert p["label"] and p["base_url"].startswith("https://")
            assert p["model"] and p["image_api"] in ("openai", "siliconflow")


class TestImageModelSelection:
    @pytest.mark.asyncio
    async def test_default_selection_is_first_image_entry(self, env) -> None:
        view = await (await env.client.get("/api/settings/imagegen")).json()
        assert view["model"] == "img1"

    @pytest.mark.asyncio
    async def test_put_selection_and_read_back(self, env) -> None:
        r = await env.client.put("/api/settings/model-list/img2", json={
            "endpoint": "e1", "model": "gpt-image-1-mini", "use": "image", "image_api": "openai",
        })
        assert r.status == 200, await r.text()
        r = await env.client.put("/api/settings/imagegen", json={"model": "img2"})
        assert r.status == 200, await r.text()
        assert (await r.json())["model"] == "img2"
        view = await (await env.client.get("/api/settings/imagegen")).json()
        assert view["model"] == "img2"
        # 存的是 kv，不是 config.toml（换用哪个不用改配置文件）
        assert env.app.store.kv_get("imagegen.model") == "img2"
        text = (env.plug_dir / "config.toml").read_text(encoding="utf-8")
        assert "[imagegen]" not in text  # 选择存 kv，不写进 config.toml

    @pytest.mark.asyncio
    async def test_selecting_chat_entry_rejected_400(self, env) -> None:
        r = await env.client.put("/api/settings/imagegen", json={"model": "m1"})
        assert r.status == 400, await r.text()
        assert "生图" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_selecting_unknown_entry_rejected_400(self, env) -> None:
        r = await env.client.put("/api/settings/imagegen", json={"model": "ghost"})
        assert r.status == 400, await r.text()

    @pytest.mark.asyncio
    async def test_clear_selection_falls_back_to_first(self, env) -> None:
        await env.client.put("/api/settings/imagegen", json={"model": "img1"})
        r = await env.client.put("/api/settings/imagegen", json={"model": ""})
        assert r.status == 200, await r.text()
        assert (await r.json())["model"] == "img1"
        assert not env.app.store.kv_get("imagegen.model")

    @pytest.mark.asyncio
    async def test_deleted_selection_falls_back(self, env) -> None:
        await env.client.put("/api/settings/imagegen", json={"model": "img1"})
        r = await env.client.delete("/api/settings/model-list/img1")
        assert r.status == 200, await r.text()
        view = await (await env.client.get("/api/settings/imagegen")).json()
        assert view["model"] == ""

    @pytest.mark.asyncio
    async def test_no_image_entry_at_all(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        plug_dir = tmp_path / "plug"
        raw = _raw(data_dir, tmp_path)
        raw["model_list"] = [{"id": "m1", "endpoint": "e1", "model": "gpt-x"}]
        _write_plugin_dir(plug_dir, raw)
        app = MaiWorkApp(FakeCtx({"config.get": "1"}), raw, plugin_dir=plug_dir)
        app.profiles_cls = FakeProfiles
        await app.start()
        server = TestServer(app.console.app)
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        try:
            await client.post("/api/login", json={"password": PASSWORD})
            view = await (await client.get("/api/settings/imagegen")).json()
            assert view["model"] == ""
        finally:
            await client.close()
            await app.stop()


class TestSecretNeverLeaves:
    @pytest.mark.asyncio
    async def test_endpoint_key_not_in_image_responses(self, env) -> None:
        bodies = []
        r = await env.client.put("/api/settings/model-list/img2", json={
            "endpoint": "e1", "model": "gpt-image-1", "use": "image", "image_api": "openai",
        })
        bodies.append(await r.text())
        bodies.append(await (await env.client.get("/api/settings/endpoints")).text())
        bodies.append(await (await env.client.put("/api/settings/imagegen", json={"model": "img1"})).text())
        bodies.append(await (await env.client.get("/api/settings/imagegen")).text())
        for body in bodies:
            assert SECRET not in body
            assert "api_key" not in body


class TestAppWiring:
    """接线端到端：真 app 注册了 generate_image，走一次真调用能把图写进任务目录。"""

    @pytest.mark.asyncio
    async def test_app_registers_and_runs_generate_image(self, env) -> None:
        from CharTyr_MaiWork.maiwork.tools import ToolContext

        assert env.app.tools.get("generate_image", "worker") is not None
        assert env.app.tools.get("generate_image", "main") is None
        ws = env.app.env.workspace("tinker")
        ctx = ToolContext(
            group_id=G1, task_id="T-9", actor="子 agent #1", role="worker",
            workspace=ws, artifact_scope=("artifacts/T-9",),
        )
        r = await env.app.tools.call("generate_image", {"prompt": "一只红熊猫", "filename": "hero"}, ctx)
        assert r.ok, r.error
        rel = r.data["files"][0]
        assert rel == "artifacts/T-9/images/hero.png"
        assert (ws / rel).read_bytes() == PNG
        # 记账落库（管理员能看到生图花了多少次）
        stat = env.app.store.kv_get("imagegen.calls")
        assert isinstance(stat, dict) and stat["ok"] == 1 and stat["images"] == 1
