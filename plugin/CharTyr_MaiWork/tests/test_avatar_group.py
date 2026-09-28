"""群头像 + 关注成员显示名/头像（tests/test_avatar_group.py）：

- GET /api/avatar/g/<token>：token = HMAC(console_secret, "avatar-g|<群号>") 前 16 位；
  只认服务群；管理员可看全部服务群，群友只能看自己群（别人的群 403、没这个 token 404）；
  非 qq 平台 404（前端回落 emoji 图标）；代理 https://p.qlogo.cn/gh/<群号>/<群号>/640，
  磁盘缓存 24 小时、下载失败回落旧缓存、魔数判定 Content-Type。
- /api/groups、/api/groups/{ref}、/api/settings 的每个群对象都有 avatar 字段。
- GroupView.focus[]：display_name = 群名片（QQ 昵称）（card 空或与 nickname 相同只留一个，
  两个都没有回落存量 name，存量 name 就是 QQ 号时留空，绝不把 QQ 号当显示名）；
  avatar = /api/avatar/m/<member_token>（qq 才有）。

出站请求走 httpx MockTransport（不碰真网）。
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

from fakes import FakeCtx

from CharTyr_MaiWork.app import MaiWorkApp

PASSWORD = "测试密码-非常显眼-不要出现在日志里"
G1 = "900000001"
G2 = "902106456"
BOT_QQ = "987654321"

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
TEXT_BYTES = b"this is not an image at all, just text"


def _raw_config(data_dir: Path, port: int) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {
            "serve": [
                {"group": f"qq:{G1}", "workspace": "tinker"},
                {"group": f"qq:{G2}", "workspace": "tinker2"},
            ]
        },
        "console": {"listen": f"127.0.0.1:{port}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test-xyz", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


def _free_port() -> int:
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Qlogo:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.mode = "png"

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            if self.mode == "500":
                return httpx.Response(500, content=b"boom")
            if self.mode == "text":
                return httpx.Response(200, content=TEXT_BYTES)
            return httpx.Response(200, content=PNG_BYTES)

        return httpx.MockTransport(handler)


def _cfg(platform: str = "qq"):
    return {"config.get": lambda key, **kw: {"bot.qq_account": BOT_QQ, "bot.platform": platform}.get(key)}


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    qlogo = _Qlogo()
    raw = _raw_config(tmp_path / "data", _free_port())
    app = MaiWorkApp(FakeCtx(_cfg()), raw, plugin_dir=Path(__file__).resolve().parents[1])
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


async def _groups(env, *, admin: bool) -> dict[str, dict]:
    if admin:
        await _login(env)
    r = await env.client.get("/api/groups")
    assert r.status == 200
    return {g["id"]: g for g in await r.json()}


class TestGroupAvatar:
    @pytest.mark.asyncio
    async def test_group_list_has_avatar_and_token_works(self, env) -> None:
        groups = await _groups(env, admin=True)
        assert groups[G1]["avatar"].startswith("/api/avatar/g/")
        token = groups[G1]["avatar"].rsplit("/", 1)[-1]
        r = await env.client.get(f"/api/avatar/g/{token}")
        assert r.status == 200
        assert r.headers["Content-Type"] == "image/png"
        assert (await r.read()) == PNG_BYTES
        assert env.qlogo.requests == [f"https://p.qlogo.cn/gh/{G1}/{G1}/640"]

    @pytest.mark.asyncio
    async def test_group_view_and_settings_have_avatar(self, env) -> None:
        await _login(env)
        r = await env.client.get(f"/api/groups/{G1}")
        assert r.status == 200
        view = await r.json()
        assert view["avatar"].startswith("/api/avatar/g/")
        r = await env.client.get("/api/settings")
        assert r.status == 200
        data = await r.json()
        one = next(g for g in data["groups"] if g["id"] == G1)
        assert one["avatar"].startswith("/api/avatar/g/")

    @pytest.mark.asyncio
    async def test_wrong_token_404(self, env) -> None:
        await _login(env)
        r = await env.client.get("/api/avatar/g/deadbeefdeadbeef")
        assert r.status == 404
        assert env.qlogo.requests == []

    @pytest.mark.asyncio
    async def test_unknown_group_token_404(self, env) -> None:
        await _login(env)
        bad = env.app.avatar.group_token("123456789")
        r = await env.client.get(f"/api/avatar/g/{bad}")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_anonymous_401(self, env) -> None:
        token = env.app.avatar.group_token(G1)
        r = await env.client.get(f"/api/avatar/g/{token}")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_member_own_group_ok_other_group_403(self, env) -> None:
        own = env.app.avatar.group_token(G1)
        other = env.app.avatar.group_token(G2)
        link = env.app.token_of(G1)
        r = await env.client.get(f"/api/avatar/g/{own}", headers={"X-MW-Group": link})
        assert r.status == 200
        r2 = await env.client.get(f"/api/avatar/g/{other}", headers={"X-MW-Group": link})
        assert r2.status == 403

    @pytest.mark.asyncio
    async def test_admin_can_fetch_every_served_group(self, env) -> None:
        await _login(env)
        token = env.app.avatar.group_token(G2)
        r = await env.client.get(f"/api/avatar/g/{token}")
        assert r.status == 200
        assert env.qlogo.requests[-1] == f"https://p.qlogo.cn/gh/{G2}/{G2}/640"

    @pytest.mark.asyncio
    async def test_non_qq_platform_404(self, tmp_path: Path) -> None:
        raw = _raw_config(tmp_path / "data", _free_port())
        app = MaiWorkApp(FakeCtx(_cfg(platform="tg")), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.avatar_transport = _Qlogo().transport()
        await app.start()
        try:
            server = TestServer(app.console.app)
            client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
            await client.start_server()
            await client.post("/api/login", json={"password": PASSWORD})
            token = app.avatar.group_token(G1)
            r = await client.get(f"/api/avatar/g/{token}")
            assert r.status == 404
            await client.close()
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_cached_24h(self, env) -> None:
        await _login(env)
        token = env.app.avatar.group_token(G1)
        assert (await env.client.get(f"/api/avatar/g/{token}")).status == 200
        assert len(env.qlogo.requests) == 1
        assert (await env.client.get(f"/api/avatar/g/{token}")).status == 200
        assert len(env.qlogo.requests) == 1
        cache = env.tmp_path / "data" / "console" / "avatar_cache"
        assert list(cache.glob(f"g_{token}.*"))

    @pytest.mark.asyncio
    async def test_download_fail_falls_back_to_stale_cache(self, env) -> None:
        import os

        await _login(env)
        token = env.app.avatar.group_token(G1)
        assert (await env.client.get(f"/api/avatar/g/{token}")).status == 200
        cache = env.tmp_path / "data" / "console" / "avatar_cache"
        f = next(cache.glob(f"g_{token}.*"))
        old = 1_000_000_000
        os.utime(f, (old, old))
        env.qlogo.mode = "500"
        r = await env.client.get(f"/api/avatar/g/{token}")
        assert r.status == 200
        assert (await r.read()) == PNG_BYTES

    @pytest.mark.asyncio
    async def test_download_fail_no_cache_404(self, env) -> None:
        env.qlogo.mode = "500"
        await _login(env)
        token = env.app.avatar.group_token(G1)
        assert (await env.client.get(f"/api/avatar/g/{token}")).status == 404

    @pytest.mark.asyncio
    async def test_group_avatar_headers_do_not_leak_bot_qq(self, env) -> None:
        groups = await _groups(env, admin=True)
        token = groups[G1]["avatar"].rsplit("/", 1)[-1]
        assert token != G1
        r = await env.client.get(f"/api/avatar/g/{token}")
        for v in r.headers.values():
            assert BOT_QQ not in str(v)


# ----------------------------------------------------------------------
# 关注成员：display_name + avatar
# ----------------------------------------------------------------------


def _seed_focus(app, rows: list[dict]) -> None:
    import time

    with app.store.tx() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO focus_members"
                " (group_id, user_id, name, card, nickname, reasons, pinned, removed, updated)"
                " VALUES (?, ?, ?, ?, ?, '[]', 1, 0, ?)",
                (
                    G1,
                    row["user_id"],
                    row.get("name", ""),
                    row.get("card", ""),
                    row.get("nickname", ""),
                    time.time(),
                ),
            )
        for row in rows:
            if row.get("activity_name"):
                conn.execute(
                    "INSERT INTO member_activity (group_id, user_id, day, count, name)"
                    " VALUES (?, ?, ?, 1, ?)",
                    (G1, row["user_id"], "2026-09-27", row["activity_name"]),
                )


@pytest_asyncio.fixture
async def focus_env(tmp_path: Path):
    qlogo = _Qlogo()
    raw = _raw_config(tmp_path / "data", _free_port())
    app = MaiWorkApp(FakeCtx(_cfg()), raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.avatar_transport = qlogo.transport()
    await app.start()
    _seed_focus(
        app,
        [
            {"user_id": "10001", "card": "小明", "nickname": "ming", "activity_name": "阿明"},
            {"user_id": "10002", "card": "", "nickname": "妮可", "activity_name": "妮可"},
            {"user_id": "10003", "card": "同名", "nickname": "同名", "activity_name": "同名"},
            {"user_id": "10004", "name": "", "activity_name": "老三"},
            {"user_id": "10005"},
        ],
    )
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    await client.post("/api/login", json={"password": PASSWORD})
    yield type("Env", (), {"app": app, "client": client, "tmp_path": tmp_path, "qlogo": qlogo})
    await client.close()
    await app.stop()


async def _focus_map(env) -> dict[str, dict]:
    r = await env.client.get(f"/api/groups/{G1}")
    assert r.status == 200
    view = await r.json()
    return {m["user_id"]: m for m in view["focus"]}


class TestFocusDisplay:
    @pytest.mark.asyncio
    async def test_display_name_card_and_nickname(self, focus_env) -> None:
        members = await _focus_map(focus_env)
        assert members["10001"]["display_name"] == "小明（ming）"
        assert members["10002"]["display_name"] == "妮可"
        assert members["10003"]["display_name"] == "同名"
        assert members["10004"]["display_name"] == "老三"
        assert members["10005"]["display_name"] == ""
        for uid, m in members.items():
            assert m["name"] != uid, uid

    @pytest.mark.asyncio
    async def test_avatar_path_and_fetch(self, focus_env) -> None:
        members = await _focus_map(focus_env)
        tok = members["10001"]["avatar"]
        assert tok.startswith("/api/avatar/m/")
        token = tok.rsplit("/", 1)[-1]
        assert token == focus_env.app.avatar.member_token(G1, "10001")
        r = await focus_env.client.get(tok)
        assert r.status == 200
        assert r.headers["Content-Type"] == "image/png"
        assert any("nk=10001" in u and "s=160" in u for u in focus_env.qlogo.requests)

    @pytest.mark.asyncio
    async def test_non_qq_member_has_no_avatar(self, focus_env) -> None:
        import time

        with focus_env.app.store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members"
                " (group_id, user_id, name, card, nickname, reasons, pinned, removed, updated)"
                " VALUES (?, ?, '', '', '', '[]', 1, 0, ?)",
                (G1, "tg:555", time.time()),
            )
        members = await _focus_map(focus_env)
        assert not members["tg:555"].get("avatar")

    @pytest.mark.asyncio
    async def test_no_qq_number_in_focus_display_fields(self, focus_env) -> None:
        r = await focus_env.client.get(f"/api/groups/{G1}")
        view = await r.json()
        for m in view["focus"]:
            assert m["display_name"] != "10005"
            assert "10005" not in json.dumps(m["display_name"])
