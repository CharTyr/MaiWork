"""头像接口测试（tests/test_avatar.py）：

- GET /api/avatar/bot 不用登录；自定义上传 → 200 + 魔数 Content-Type；
  没自定义时按 QQ（qlogo 出站下载走 MockTransport，不碰真网）→ 缓存 24h →
  失败回落旧缓存 → 都没有 404（前端回落默认图）。
- GET /api/settings/avatar / POST / DELETE：只管理员；POST 两种 JSON 形态
  {"url"} / {"data","mime"}；魔数不符、超 2MB、坏 base64 都 400。
- 版本号 v 随自定义变化递增；/api/me 的 bot.avatar 是 /api/avatar/bot?v=N。
- QQ 号不出现在任何响应头 / 响应体里。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.app import MaiWorkApp

SECRET = "sk-test-十分显眼的密钥AaBbCc123"
PASSWORD = "测试密码-非常显眼-不要出现在日志里"
G1 = "900000001"
BOT_QQ = "987654321"

# 1x1 PNG（合法魔数）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF_BYTES = b"GIF89a" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x10\x00\x00\x00" + b"WEBP" + b"\x00" * 16
TEXT_BYTES = b"this is not an image at all, just text"


def _raw_config(data_dir: Path, port: int) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": f"127.0.0.1:{port}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


def _free_port() -> int:
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Qlogo:
    """假 qlogo：记录请求次数；mode 控制返回。"""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.mode = "png"  # png | 500 | text | big

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            if self.mode == "500":
                return httpx.Response(500, content=b"boom")
            if self.mode == "text":
                return httpx.Response(200, content=TEXT_BYTES)
            if self.mode == "big":
                return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024))
            return httpx.Response(200, content=PNG_BYTES)

        return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    qlogo = _Qlogo()
    raw = _raw_config(tmp_path / "data", _free_port())
    ctx = FakeCtx({"config.get": lambda key, **kw: {"bot.qq_account": BOT_QQ, "bot.platform": "qq", "bot.nickname": "测试AI"}.get(key)})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    app.avatar_transport = qlogo.transport()
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield type("Env", (), {"app": app, "client": client, "tmp_path": tmp_path, "qlogo": qlogo})
    await client.close()
    await app.stop()


async def _login(env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


class TestBotAvatarNoLogin:
    @pytest.mark.asyncio
    async def test_bot_avatar_no_login_needed(self, env) -> None:
        """登录页也要显示：不带任何身份也 200。"""
        r = await env.client.get("/api/avatar/bot")
        assert r.status == 200
        assert r.headers["Content-Type"] == "image/png"
        assert (await r.read()) == PNG_BYTES
        # QQ 号不出现在任何响应头
        for v in r.headers.values():
            assert BOT_QQ not in str(v)

    @pytest.mark.asyncio
    async def test_qq_avatar_cached_24h(self, env) -> None:
        """第一次下载后 24 小时内不再出站。"""
        r1 = await env.client.get("/api/avatar/bot")
        assert r1.status == 200
        assert len(env.qlogo.requests) == 1
        r2 = await env.client.get("/api/avatar/bot")
        assert r2.status == 200
        assert len(env.qlogo.requests) == 1  # 用了磁盘缓存
        # 缓存文件存在
        cache = env.tmp_path / "data" / "console" / "avatar_cache"
        assert list(cache.glob("bot_qq.*"))

    @pytest.mark.asyncio
    async def test_download_fail_falls_back_to_stale_cache(self, env) -> None:
        r1 = await env.client.get("/api/avatar/bot")
        assert r1.status == 200
        # 弄坏缓存时间（超过 24h）再让下载失败 → 用旧缓存
        cache = env.tmp_path / "data" / "console" / "avatar_cache"
        f = next(cache.glob("bot_qq.*"))
        import os

        old = 1_000_000_000  # 2001 年，肯定过期
        os.utime(f, (old, old))
        env.qlogo.mode = "500"
        r2 = await env.client.get("/api/avatar/bot")
        assert r2.status == 200
        assert (await r2.read()) == PNG_BYTES

    @pytest.mark.asyncio
    async def test_download_fail_no_cache_is_404(self, tmp_path: Path) -> None:
        qlogo = _Qlogo()
        qlogo.mode = "500"
        raw = _raw_config(tmp_path / "data", _free_port())
        ctx = FakeCtx({"config.get": lambda key, **kw: {"bot.qq_account": BOT_QQ, "bot.platform": "qq"}.get(key)})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        app.avatar_transport = qlogo.transport()
        await app.start()
        try:
            server = TestServer(app.console.app)
            client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
            await client.start_server()
            r = await client.get("/api/avatar/bot")
            assert r.status == 404  # 前端回落 /static/assets/bot.jpg
            await client.close()
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_non_image_response_not_cached(self, tmp_path: Path) -> None:
        qlogo = _Qlogo()
        qlogo.mode = "text"
        raw = _raw_config(tmp_path / "data", _free_port())
        ctx = FakeCtx({"config.get": lambda key, **kw: {"bot.qq_account": BOT_QQ, "bot.platform": "qq"}.get(key)})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        app.avatar_transport = qlogo.transport()
        await app.start()
        try:
            server = TestServer(app.console.app)
            client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
            await client.start_server()
            r = await client.get("/api/avatar/bot")
            assert r.status == 404
            cache = tmp_path / "data" / "console" / "avatar_cache"
            assert not list(cache.glob("bot_qq.*")) if cache.exists() else True
            await client.close()
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_no_qq_is_404(self, tmp_path: Path) -> None:
        """host 给不出 QQ（platform 不是 qq / qq_account 空）→ 404 走默认图。"""
        raw = _raw_config(tmp_path / "data", _free_port())
        ctx = FakeCtx({"config.get": lambda key, **kw: {"bot.qq_account": "", "bot.platform": "qq"}.get(key)})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            server = TestServer(app.console.app)
            client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
            await client.start_server()
            r = await client.get("/api/avatar/bot")
            assert r.status == 404
            await client.close()
        finally:
            await app.stop()


class TestAvatarSettings:
    @pytest.mark.asyncio
    async def test_settings_avatar_requires_admin(self, env) -> None:
        assert (await env.client.get("/api/settings/avatar")).status == 401
        assert (await env.client.post("/api/settings/avatar", json={"url": "https://x.test/a.png"})).status == 401
        r = await env.client.request("DELETE", "/api/settings/avatar")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_get_default_state(self, env) -> None:
        await _login(env)
        r = await env.client.get("/api/settings/avatar")
        assert r.status == 200
        data = await r.json()
        assert data["source"] == "qq"  # host 给了 bot.qq_account
        assert data["platform"] == "qq"
        assert data["url"].startswith("/api/avatar/bot?v=")
        assert data["custom_kind"] == ""
        assert data["custom_url"] == ""
        assert BOT_QQ not in json.dumps(data)

    @pytest.mark.asyncio
    async def test_post_url_form(self, env) -> None:
        await _login(env)
        r = await env.client.post("/api/settings/avatar", json={"url": "https://example.com/a.png"})
        assert r.status == 200
        data = await r.json()
        assert data["source"] == "custom"
        assert data["custom_kind"] == "url"
        assert data["custom_url"] == "https://example.com/a.png"
        assert data["url"] == "https://example.com/a.png"
        # bot 头像接口 302 到这个网址
        r2 = await env.client.get("/api/avatar/bot", allow_redirects=False)
        assert r2.status == 302
        assert r2.headers["Location"] == "https://example.com/a.png"

    @pytest.mark.asyncio
    async def test_post_url_rejects_non_http(self, env) -> None:
        await _login(env)
        for bad in ("ftp://x/a.png", "javascript:alert(1)", "/local/path.png", ""):
            r = await env.client.post("/api/settings/avatar", json={"url": bad})
            assert r.status == 400, bad

    @pytest.mark.asyncio
    async def test_post_upload_png(self, env) -> None:
        await _login(env)
        b64 = base64.b64encode(PNG_BYTES).decode()
        r = await env.client.post("/api/settings/avatar", json={"data": b64, "mime": "image/png"})
        assert r.status == 200
        data = await r.json()
        assert data["source"] == "custom"
        assert data["custom_kind"] == "upload"
        assert data["custom_url"] == ""
        assert data["url"].startswith("/api/avatar/bot?v=")
        # 真的存盘了
        f = env.tmp_path / "data" / "console" / "avatar_bot.png"
        assert f.is_file() and f.read_bytes() == PNG_BYTES
        # 取回来：Content-Type 按魔数
        r2 = await env.client.get("/api/avatar/bot")
        assert r2.status == 200
        assert r2.headers["Content-Type"] == "image/png"
        assert (await r2.read()) == PNG_BYTES

    @pytest.mark.asyncio
    async def test_post_upload_rejects_bad_magic(self, env) -> None:
        await _login(env)
        b64 = base64.b64encode(TEXT_BYTES).decode()
        r = await env.client.post("/api/settings/avatar", json={"data": b64, "mime": "image/png"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_post_upload_rejects_bad_base64(self, env) -> None:
        await _login(env)
        r = await env.client.post("/api/settings/avatar", json={"data": "!!!不是base64!!!", "mime": "image/png"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_post_upload_rejects_oversize(self, env) -> None:
        await _login(env)
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024)  # 超过 2MB
        b64 = base64.b64encode(big).decode()
        r = await env.client.post("/api/settings/avatar", json={"data": b64, "mime": "image/png"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_post_neither_field_400(self, env) -> None:
        await _login(env)
        r = await env.client.post("/api/settings/avatar", json={"hello": "world"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_delete_restores_qq(self, env) -> None:
        await _login(env)
        b64 = base64.b64encode(PNG_BYTES).decode()
        await env.client.post("/api/settings/avatar", json={"data": b64, "mime": "image/png"})
        r = await env.client.request("DELETE", "/api/settings/avatar")
        assert r.status == 200
        data = await r.json()
        assert data["source"] == "qq"
        assert data["custom_kind"] == ""
        # 自定义文件删了
        assert not list((env.tmp_path / "data" / "console").glob("avatar_bot.*"))
        # bot 头像回到 qlogo 代理
        r2 = await env.client.get("/api/avatar/bot")
        assert r2.status == 200
        assert (await r2.read()) == PNG_BYTES

    @pytest.mark.asyncio
    async def test_version_bumps_on_changes(self, env) -> None:
        await _login(env)
        r = await env.client.get("/api/settings/avatar")
        v0 = (await r.json())["url"]
        b64 = base64.b64encode(PNG_BYTES).decode()
        r = await env.client.post("/api/settings/avatar", json={"data": b64, "mime": "image/png"})
        v1 = (await r.json())["url"]
        assert v1 != v0
        r = await env.client.request("DELETE", "/api/settings/avatar")
        v2 = (await r.json())["url"]
        assert v2 != v1

    @pytest.mark.asyncio
    async def test_me_bot_avatar_uses_versioned_url(self, env) -> None:
        r = await env.client.get("/api/me")
        data = await r.json()
        assert data["bot"]["avatar"].startswith("/api/avatar/bot?v=")
        assert "bot.jpg" not in data["bot"]["avatar"]

    @pytest.mark.asyncio
    async def test_jpeg_gif_webp_magic_accepted(self, env) -> None:
        await _login(env)
        for blob, ext in ((JPEG_BYTES, ".jpg"), (GIF_BYTES, ".gif"), (WEBP_BYTES, ".webp")):
            b64 = base64.b64encode(blob).decode()
            r = await env.client.post("/api/settings/avatar", json={"data": b64, "mime": "image/x"})
            assert r.status == 200
            f = env.tmp_path / "data" / "console" / f"avatar_bot{ext}"
            assert f.is_file(), ext
            await env.client.request("DELETE", "/api/settings/avatar")
