"""社交平台接口测试（`/api/social/*`）：权限、同源守卫、Cookie 只进不出。

起真 aiohttp 服务器；svc 是轻量假对象（真 Store + 真 social_platforms 模块，
但 `_run_cli` / `_which` 是假的）——绝不真起子进程、绝不真装 CLI。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import social_platforms as sp
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.group_admins import GroupAdmins
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
PASSWORD = "社交平台测试密码-显眼-13579"
GA_PW = "群管理员密码-显眼-24680"
MEMBER_TOKEN = "socialtok1"
COOKIE_VALUE = "auth_token=abc123; ct0=def456"


class _Console:
    def __init__(self, password: str = ""):
        self.password = password


class _Settings:
    def __init__(self, served=(G1,), password: str = "") -> None:
        self.served = set(served)
        self.groups = {gid: {} for gid in served}
        self.console = _Console(password)

    def is_served(self, gid) -> bool:
        return str(gid) in self.served


class _FakeSvc:
    def __init__(self, store: Store, settings: _Settings) -> None:
        self.store = store
        self._settings = settings
        self.group_admins = GroupAdmins(store, get_settings=lambda: settings)

    def get_settings(self):
        return self._settings


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _Settings(served=(G1,), password=PASSWORD)
    svc = _FakeSvc(store, settings)
    svc.group_admins.set_password(G1, GA_PW)
    # 群友链接码
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, token, name) VALUES (?, ?, ?)", (G1, MEMBER_TOKEN, "测试群"))
    server = ConsoleServer(svc)

    async def make_client():
        client = TestClient(TestServer(server.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        return client

    admin = await make_client()
    member = await make_client()
    ga = await make_client()
    anon = await make_client()

    r = await admin.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200
    r = await ga.post("/api/login", json={"password": GA_PW})
    assert r.status == 200 and (await r.json())["role"] == "group_admin"

    # 假 CLI：安装成功、检测成功、搜索回固定假数据
    async def run(argv, *, timeout, env=None, cwd=None):
        if "venv" in argv:
            python = sp._venv_exe(Path(argv[-1]), "python")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        if "pip" in argv:
            bindir = Path(argv[0]).parent
            for platform in sp.PLATFORMS.values():
                cli = bindir / (platform.cli_name + sp._EXE_SUFFIX)
                cli.touch()
                cli.chmod(0o700)
        if "--version" in argv:
            return (0, "fake-cli 1.0.0\n", "")
        return (0, json.dumps([{"title": "假结果", "url": "https://www.bilibili.com/video/BV1xx411c7mD"}], ensure_ascii=False), "")

    monkeypatch.setattr(sp, "_run_cli", run)

    class E:
        pass

    e = E()
    e.store, e.svc, e.server = store, svc, server
    e.admin, e.member, e.ga, e.anon = admin, member, ga, anon
    e.member_headers = {"X-MW-Group": MEMBER_TOKEN}
    yield e
    for c in (admin, member, ga, anon):
        await c.close()
    store.close()


async def _wait_install(store: Store, pid: str, timeout: float = 3.0) -> dict:
    """后台安装是异步的：轮询 kv 直到装上或报错。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        st = (store.kv_get("social.platforms") or {}).get(pid) or {}
        if st.get("cli_path") or st.get("last_error"):
            return st
        await asyncio.sleep(0.02)
    raise AssertionError(f"{pid} 安装一直没跑完")


# ----------------------------------------------------------------------
# 权限：匿名 401、群友 403、群管理员 403
# ----------------------------------------------------------------------


class TestAuth:
    @pytest.mark.asyncio
    async def test_platforms_anonymous_401(self, env) -> None:
        r = await env.anon.get("/api/social/platforms")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_platforms_member_403(self, env) -> None:
        r = await env.member.get("/api/social/platforms", headers=env.member_headers)
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_platforms_group_admin_403(self, env) -> None:
        r = await env.ga.get("/api/social/platforms")
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_write_endpoints_reject_member_and_anon(self, env) -> None:
        for method, url in (
            ("POST", "/api/social/platforms/bilibili/enable"),
            ("POST", "/api/social/platforms/bilibili/disable"),
            ("POST", "/api/social/platforms/bilibili/install"),
            ("POST", "/api/social/platforms/bilibili/check"),
            ("POST", "/api/social/platforms/twitter/cookie"),
            ("DELETE", "/api/social/platforms/twitter/cookie"),
        ):
            r = await env.member.request(method, url, json={}, headers=env.member_headers)
            assert r.status == 403, (method, url, r.status)
            r = await env.anon.request(method, url, json={})
            assert r.status == 401, (method, url, r.status)

    @pytest.mark.asyncio
    async def test_cookie_status_member_403(self, env) -> None:
        r = await env.member.get("/api/social/platforms/twitter/cookie-status", headers=env.member_headers)
        assert r.status == 403
        r = await env.anon.get("/api/social/platforms/twitter/cookie-status")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_group_admin_write_403(self, env) -> None:
        r = await env.ga.post("/api/social/platforms/bilibili/enable", json={})
        assert r.status == 403


# ----------------------------------------------------------------------
# 同源守卫
# ----------------------------------------------------------------------


class TestSameOrigin:
    @pytest.mark.asyncio
    async def test_cross_origin_post_rejected(self, env) -> None:
        for method, url in (
            ("POST", "/api/social/platforms/bilibili/enable"),
            ("POST", "/api/social/platforms/twitter/cookie"),
            ("DELETE", "/api/social/platforms/twitter/cookie"),
        ):
            r = await env.admin.request(method, url, json={"cookie": COOKIE_VALUE}, headers={"Origin": "http://evil.test"})
            assert r.status == 403, (method, url, r.status)
            data = await r.json()
            assert "跨源" in data["error"]

    @pytest.mark.asyncio
    async def test_same_origin_passes(self, env) -> None:
        origin = str(env.admin.make_url("/")).rstrip("/")
        r = await env.admin.post("/api/social/platforms/bilibili/enable", json={}, headers={"Origin": origin})
        assert r.status == 200


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------


class TestRead:
    @pytest.mark.asyncio
    async def test_platforms_list_shape(self, env) -> None:
        r = await env.admin.get("/api/social/platforms")
        assert r.status == 200
        data = await r.json()
        rows = data["platforms"]
        assert [p["id"] for p in rows] == ["twitter", "xiaohongshu", "bilibili", "youtube", "reddit"]
        bili = next(p for p in rows if p["id"] == "bilibili")
        assert bili["needs_cookie"] is False
        assert bili["enabled"] is False and bili["installed"] is False
        tw = next(p for p in rows if p["id"] == "twitter")
        assert tw["needs_cookie"] is True
        assert tw["cookie_set"] is False
        assert tw["cookie_guide"]  # 告诉管理员怎么拿 Cookie

    @pytest.mark.asyncio
    async def test_unknown_platform_404(self, env) -> None:
        for method, url in (
            ("POST", "/api/social/platforms/facebook/enable"),
            ("POST", "/api/social/platforms/facebook/install"),
            ("POST", "/api/social/platforms/facebook/check"),
            ("POST", "/api/social/platforms/facebook/cookie"),
            ("DELETE", "/api/social/platforms/facebook/cookie"),
        ):
            r = await env.admin.request(method, url, json={"cookie": "x"})
            assert r.status == 404, (method, url, r.status)
        r = await env.admin.get("/api/social/platforms/facebook/cookie-status")
        assert r.status == 404


# ----------------------------------------------------------------------
# 开关 / 安装 / 检测
# ----------------------------------------------------------------------


class TestActions:
    @pytest.mark.asyncio
    async def test_enable_disable_roundtrip(self, env) -> None:
        r = await env.admin.post("/api/social/platforms/bilibili/enable", json={})
        assert r.status == 200
        assert (env.store.kv_get("social.platforms") or {})["bilibili"]["enabled"] is True
        r = await env.admin.post("/api/social/platforms/bilibili/disable", json={})
        assert r.status == 200
        assert (env.store.kv_get("social.platforms") or {})["bilibili"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_install_returns_immediately_and_runs_in_background(self, env) -> None:
        r = await env.admin.post("/api/social/platforms/bilibili/install", json={})
        assert r.status == 200
        data = await r.json()
        assert data == {"ok": True, "msg": "正在安装"}
        st = await _wait_install(env.store, "bilibili")
        assert st["cli_path"].endswith("/bili")
        assert st["installed_at"] > 0

    @pytest.mark.asyncio
    async def test_check_ok(self, env) -> None:
        await env.admin.post("/api/social/platforms/bilibili/install", json={})
        await _wait_install(env.store, "bilibili")
        r = await env.admin.post("/api/social/platforms/bilibili/check", json={})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True
        assert data["last_check"] > 0

    @pytest.mark.asyncio
    async def test_check_not_installed(self, env, monkeypatch) -> None:
        r = await env.admin.post("/api/social/platforms/bilibili/check", json={})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is False
        assert data["error"]  # 中文原因


# ----------------------------------------------------------------------
# Cookie：只进不出
# ----------------------------------------------------------------------


class TestCookieApi:
    @pytest.mark.asyncio
    async def test_set_status_clear(self, env) -> None:
        r = await env.admin.get("/api/social/platforms/twitter/cookie-status")
        assert r.status == 200 and (await r.json()) == {"set": False}

        r = await env.admin.post("/api/social/platforms/twitter/cookie", json={"cookie": COOKIE_VALUE})
        assert r.status == 200
        assert json.loads(await r.text()) == {"ok": True}  # 不回显

        r = await env.admin.get("/api/social/platforms/twitter/cookie-status")
        assert (await r.json()) == {"set": True}

        r = await env.admin.request("DELETE", "/api/social/platforms/twitter/cookie")
        assert r.status == 200
        r = await env.admin.get("/api/social/platforms/twitter/cookie-status")
        assert (await r.json()) == {"set": False}

    @pytest.mark.asyncio
    async def test_cookie_never_echoed(self, env) -> None:
        await env.admin.post("/api/social/platforms/twitter/cookie", json={"cookie": COOKIE_VALUE})
        # 平台列表、状态接口的响应文本里都不许出现 Cookie 值
        for url in ("/api/social/platforms", "/api/social/platforms/twitter/cookie-status"):
            r = await env.admin.get(url)
            text = await r.text()
            assert COOKIE_VALUE not in text
            assert "abc123" not in text and "def456" not in text

    @pytest.mark.asyncio
    async def test_cookie_empty_400(self, env) -> None:
        r = await env.admin.post("/api/social/platforms/twitter/cookie", json={"cookie": "  "})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_cookie_not_needed_400(self, env) -> None:
        r = await env.admin.post("/api/social/platforms/bilibili/cookie", json={"cookie": "x"})
        assert r.status == 400
        assert "无需" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_enable_auto_installs_and_double_click_deduplicates(env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def install(store, pid):
        calls.append(pid)
        entered.set()
        await release.wait()
    monkeypatch.setattr(sp, 'install_platform', install)
    first = await env.admin.post('/api/social/platforms/bilibili/enable', json={})
    assert (await first.json())['platform']['installing']
    await entered.wait()
    await env.admin.post('/api/social/platforms/bilibili/install', json={})
    assert calls == ['bilibili']
    response = await env.admin.get('/api/social/platforms')
    assert next(p for p in (await response.json())['platforms'] if p['id'] == 'bilibili')['installing']
    await env.admin.post('/api/social/platforms/bilibili/disable', json={})
    assert not env.server._social_installs
    assert not sp.list_platforms(env.store)[2]['enabled']


@pytest.mark.asyncio
async def test_cleanup_cancels_install_and_unexpected_errors_never_echo(env, monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def install(store, pid):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(sp, 'install_platform', install)
    await env.admin.post('/api/social/platforms/twitter/install', json={})
    await entered.wait()
    await env.server._cleanup_social_installs()
    assert cancelled.is_set() and not env.server._social_installs
    async def check(store, pid):
        raise RuntimeError('private-cookie-123')
    monkeypatch.setattr(sp, 'check_platform', check)
    response = await env.admin.post('/api/social/platforms/twitter/check', json={})
    assert response.status == 500
    assert 'private-cookie-123' not in await response.text()


@pytest.mark.asyncio
async def test_cookie_validation_explains_missing_fields_without_value(env):
    response = await env.admin.post('/api/social/platforms/twitter/cookie', json={'cookie': 'auth_token=do-not-echo'})
    assert response.status == 400
    error = (await response.json())['error']
    assert 'auth_token' in error and 'ct0' in error
    assert 'do-not-echo' not in error
