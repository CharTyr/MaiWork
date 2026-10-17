"""主题与音效包接口测试（design.md §3，任务 10-16-web-theme-sound）。

- GET /api/theme：任何访客（含未登录）可读，不含 data_dir 路径；
- PUT /api/theme、GET /api/sound-packs、上传、GitHub 安装、DELETE：只总管理员，写操作过同源守卫；
- GET /api/sound-packs/{id}/{kind}：任何访客；kind 只认 6 种；按格式给 Content-Type，可缓存。
测试音频是几字节假文件头；GitHub 走 MockTransport。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp

PASSWORD = "测试密码-主题接口"
G1 = "900000001"
OGG = b"OggS" + b"\x00" * 60
MP3 = b"ID3" + b"\x03\x00" + b"\x00" * 40
WAV = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"\x00" * 40
EVIL = {"Origin": "http://evil.example.com:3"}


def _raw_config(data_dir: Path, port: int) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": f"127.0.0.1:{port}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


def _free_port() -> int:
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in entries.items():
            zf.writestr(k, v.encode("utf-8") if isinstance(v, str) else v)
    return buf.getvalue()


PACK = _zip({"pack.json": json.dumps({"name": "demo"}), "click.ogg": OGG, "error.mp3": MP3, "open.wav": WAV})


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    raw = _raw_config(data_dir, _free_port())
    ctx = FakeCtx({"config.get": lambda key, **kw: {"bot.qq_account": "1", "bot.platform": "qq", "bot.nickname": "测试AI"}.get(key)})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield type("Env", (), {"app": app, "client": client, "data_dir": data_dir})
    await client.close()
    await app.stop()


async def _login(env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


def _member(env) -> dict:
    return {"X-MW-Group": env.app.token_of(G1)}


async def _upload(env, headers: dict | None = None):
    h = {"Content-Type": "application/zip", "X-Filename": "demo.zip"}
    h.update(headers or {})
    return await env.client.post("/api/sound-packs/upload", data=PACK, headers=h)


class TestThemeRead:
    @pytest.mark.asyncio
    async def test_anonymous_can_read(self, env) -> None:
        r = await env.client.get("/api/theme")
        assert r.status == 200
        body = await r.json()
        assert body["default"] == "default"
        assert {t["id"] for t in body["themes"]} == {"default", "retro"}
        assert body["sounds"] == {}

    @pytest.mark.asyncio
    async def test_no_data_dir_path(self, env) -> None:
        await _login(env)
        r = await _upload(env)
        assert r.status == 200
        pid = (await r.json())["id"]
        r = await env.client.put("/api/theme", json={"default": "retro", "sounds": {"retro": pid}})
        assert r.status == 200
        env.client.session.cookie_jar.clear()
        r = await env.client.get("/api/theme")
        text = await r.text()
        assert str(env.data_dir) not in text
        assert "sound_packs" not in text
        body = json.loads(text)
        assert body["default"] == "retro"
        assert body["sounds"]["retro"]["files"]["click"] == f"/api/sound-packs/{pid}/click"


class TestPermissions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            ("PUT", "/api/theme", {"json": {"default": "retro"}}),
            ("GET", "/api/sound-packs", {}),
            ("POST", "/api/sound-packs/upload", {"data": PACK, "headers": {"Content-Type": "application/zip"}}),
            ("POST", "/api/sound-packs/github", {"json": {"url": "https://github.com/foo/bar"}}),
            ("DELETE", "/api/sound-packs/demo", {}),
        ],
    )
    async def test_anonymous_and_member_refused(self, env, method, path, kwargs) -> None:
        r = await env.client.request(method, path, **kwargs)
        assert r.status == 401
        headers = dict(kwargs.get("headers") or {})
        headers.update(_member(env))
        kw = dict(kwargs)
        kw["headers"] = headers
        r = await env.client.request(method, path, **kw)
        assert r.status == 403
        # 什么都没装上、默认主题没变
        assert not (env.data_dir / "console" / "sound_packs").exists() or not any(
            (env.data_dir / "console" / "sound_packs").iterdir()
        )
        assert (await (await env.client.get("/api/theme")).json())["default"] == "default"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            ("PUT", "/api/theme", {"json": {"default": "retro"}}),
            ("POST", "/api/sound-packs/upload", {"data": PACK, "headers": {"Content-Type": "application/zip"}}),
            ("POST", "/api/sound-packs/github", {"json": {"url": "https://github.com/foo/bar"}}),
            ("DELETE", "/api/sound-packs/demo", {}),
        ],
    )
    async def test_cross_origin_refused(self, env, method, path, kwargs) -> None:
        await _login(env)
        headers = dict(kwargs.get("headers") or {})
        headers.update(EVIL)
        kw = dict(kwargs)
        kw["headers"] = headers
        r = await env.client.request(method, path, **kw)
        assert r.status == 403
        assert "跨源" in (await r.json())["error"]


class TestAdminFlow:
    @pytest.mark.asyncio
    async def test_upload_list_bind_delete(self, env) -> None:
        await _login(env)
        r = await _upload(env)
        assert r.status == 200
        view = await r.json()
        assert view["id"] == "demo"
        assert sorted(view["sounds"]) == ["click", "error", "open"]

        r = await env.client.get("/api/sound-packs")
        assert r.status == 200
        packs = (await r.json())["packs"]
        assert [p["id"] for p in packs] == ["demo"]
        assert str(env.data_dir) not in json.dumps(packs, ensure_ascii=False)

        r = await env.client.put("/api/theme", json={"sounds": {"retro": "demo"}})
        assert r.status == 200
        assert (await r.json())["sounds"]["retro"]["pack"] == "demo"

        r = await env.client.put("/api/theme", json={"sounds": {"default": "demo"}})
        assert r.status == 400
        r = await env.client.put("/api/theme", json={"default": "neon"})
        assert r.status == 400

        r = await env.client.delete("/api/sound-packs/demo")
        assert r.status == 200
        assert not (env.data_dir / "console" / "sound_packs" / "demo").exists()
        assert (await (await env.client.get("/api/theme")).json())["sounds"] == {}
        r = await env.client.delete("/api/sound-packs/demo")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_upload_multipart(self, env) -> None:
        await _login(env)
        form = aiohttp.FormData()
        form.add_field("file", PACK, filename="demo.zip", content_type="application/zip")
        r = await env.client.post("/api/sound-packs/upload", data=form)
        assert r.status == 200
        assert (await r.json())["id"] == "demo"

    @pytest.mark.asyncio
    async def test_upload_over_1mb(self, env) -> None:
        """aiohttp 默认请求体上限 1MB；音效包 zip 允许到 10MB，raw 与 multipart 都要收得下。"""
        import os

        big = _zip({f"{k}.ogg": b"OggS" + os.urandom(700 * 1024) for k in ("click", "hover", "open")})
        assert len(big) > 2 * 1024 * 1024
        await _login(env)
        r = await env.client.post("/api/sound-packs/upload", data=big, headers={"Content-Type": "application/zip"})
        assert r.status == 200, await r.text()
        form = aiohttp.FormData()
        form.add_field("file", big, filename="big.zip", content_type="application/zip")
        r = await env.client.post("/api/sound-packs/upload", data=form)
        assert r.status == 200, await r.text()

    @pytest.mark.asyncio
    async def test_upload_too_big_400(self, env) -> None:
        await _login(env)
        r = await env.client.post(
            "/api/sound-packs/upload", data=b"\x00" * (10 * 1024 * 1024 + 10), headers={"Content-Type": "application/zip"}
        )
        assert r.status in (400, 413)
        assert "10MB" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_upload_bad_zip_400(self, env) -> None:
        await _login(env)
        blob = _zip({"click.mp3": b"not audio"})
        r = await env.client.post("/api/sound-packs/upload", data=blob, headers={"Content-Type": "application/zip"})
        assert r.status == 400
        assert "click.mp3 不是有效的音频文件" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_github_install(self, env) -> None:
        await _login(env)
        repo = _zip({"bar-main/click.ogg": OGG})

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "api.github.com":
                return httpx.Response(200, json={"default_branch": "main"})
            return httpx.Response(200, content=repo)

        env.app.sound_pack_transport = httpx.MockTransport(handler)
        r = await env.client.post("/api/sound-packs/github", json={"url": "https://github.com/foo/bar"})
        assert r.status == 200, await r.text()
        view = await r.json()
        assert view["source"] == "https://github.com/foo/bar"
        r = await env.client.post("/api/sound-packs/github", json={"url": "https://evil.com/foo/bar"})
        assert r.status == 400


class TestAudioFile:
    @pytest.mark.asyncio
    async def test_audio_public_with_content_type(self, env) -> None:
        await _login(env)
        assert (await _upload(env)).status == 200
        env.client.session.cookie_jar.clear()
        for kind, ctype, data in (("click", "audio/ogg", OGG), ("error", "audio/mpeg", MP3), ("open", "audio/wav", WAV)):
            r = await env.client.get(f"/api/sound-packs/demo/{kind}")
            assert r.status == 200
            assert r.headers["Content-Type"] == ctype
            assert r.headers["Cache-Control"] == "public, max-age=86400"
            assert await r.read() == data

    @pytest.mark.asyncio
    async def test_bad_kind_or_id_404(self, env) -> None:
        await _login(env)
        assert (await _upload(env)).status == 200
        for path in (
            "/api/sound-packs/demo/boom",
            "/api/sound-packs/demo/hover",  # 包里没有这一类
            "/api/sound-packs/nope/click",
            "/api/sound-packs/DEMO/click",
            "/api/sound-packs/..%2Fx/click",
        ):
            r = await env.client.get(path)
            assert r.status == 404, path
