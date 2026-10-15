"""可选依赖接口（`/api/optional-deps*`）测试：只给总管理员、装是后台跑、立刻回 202。

起真 aiohttp 服务器；svc 用轻量假对象（真实 Store + 真 OptionalDeps，但执行环境和 pip
runner 都是假的）——**绝不真装包、绝不真起 systemd**。
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.optional_deps import OptionalDeps
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
PASSWORD = "可选依赖测试密码-显眼-98765"
MARK = "MAIWORK_DEPS_JSON="


class _Console:
    def __init__(self, password: str = ""):
        self.password = password


class _Settings:
    def __init__(self, served=(G1,), password: str = "", workspace_root: Any = "") -> None:
        self.served = set(served)
        self.console = _Console(password)
        self.environments = types.SimpleNamespace(workspace_root=str(workspace_root or ""))

    def is_served(self, gid):
        return str(gid) in self.served


class _FakeSvc:
    def __init__(self, store: Store, settings: _Settings, deps) -> None:
        self.store = store
        self._settings = settings
        self.optional_deps = deps

    def get_settings(self):
        return self._settings


class _FakeEnv:
    def __init__(self, stdout: str = "", *, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.calls: list[str] = []

    async def run(self, name, command, *, timeout_s=None, memory=None, cwd=""):
        self.calls.append(command)
        return types.SimpleNamespace(exit_code=self.exit_code, stdout=self.stdout, stderr="", ms=1, timed_out=False, oom=False)


def _probe_stdout(installed: bool, *, soffice: str = "") -> str:
    pkgs = {
        "python-docx": {"installed": installed, "version": "1.1.2"} if installed else {"installed": False, "error": "ModuleNotFoundError"},
        "openpyxl": {"installed": installed, "version": "3.1.5"} if installed else {"installed": False, "error": "ModuleNotFoundError"},
        "python-pptx": {"installed": installed, "version": "0.6.23"} if installed else {"installed": False, "error": "ModuleNotFoundError"},
    }
    data = {"python": "/usr/bin/python3", "version": "3.12.3", "packages": pkgs}
    return MARK + json.dumps(data) + "\n" + (soffice or "MAIWORK_NO_SOFFICE") + "\n"


def _runner(code: int = 0, out: str = "", err: str = ""):
    calls: list[list[str]] = []

    async def runner(argv, *, env, timeout):
        calls.append(list(argv))
        return code, out, err

    return runner, calls


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _Settings(served=(G1,), password=PASSWORD)
    deps = OptionalDeps(
        store, lambda: _Settings(workspace_root=tmp_path / "ws"),
        env=_FakeEnv(_probe_stdout(True, soffice="/usr/bin/soffice")),
        runner=_runner(0, "Successfully installed")[0],
    )
    svc = _FakeSvc(store, settings, deps)
    server = ConsoleServer(svc)
    test_server = TestServer(server.app)
    client = TestClient(test_server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()

    class E:
        pass

    e = E()
    e.store, e.svc, e.deps, e.client = store, svc, deps, client

    async def login(password: str = PASSWORD):
        return await client.post("/api/login", json={"password": password})

    e.login = login
    yield e
    await client.close()
    store.close()


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_requires_admin(env) -> None:
    r = await env.client.get("/api/optional-deps")
    assert r.status == 401


@pytest.mark.asyncio
async def test_install_requires_admin(env) -> None:
    r = await env.client.post("/api/optional-deps/office/install", json={})
    assert r.status == 401


@pytest.mark.asyncio
async def test_install_cross_origin_rejected(env) -> None:
    await env.login()
    r = await env.client.post(
        "/api/optional-deps/office/install", json={}, headers={"Origin": "http://evil.test"}
    )
    assert r.status == 403


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_shape_and_first_check(env) -> None:
    await env.login()
    r = await env.client.get("/api/optional-deps")
    assert r.status == 200
    data = await r.json()
    assert data["available"] is True
    assert data["python"] == "/usr/bin/python3"
    assert data["checked_at"] > 0  # 第一次读就把探测跑了，页面直接看到真状态
    assert data["dir"].endswith("pylibs")
    g = data["groups"][0]
    assert g["id"] == "office"
    assert g["state"] == "ok"
    assert g["installed_count"] == 3 and g["total"] == 3
    assert g["packages"][0]["name"] == "python-docx"
    assert g["packages"][0]["installed"] is True
    assert g["tools"][0]["name"] == "soffice" and g["tools"][0]["found"] is True
    assert g["installing"] is False


@pytest.mark.asyncio
async def test_get_without_module_is_503(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    svc = _FakeSvc(store, _Settings(password=PASSWORD), None)
    server = ConsoleServer(svc)
    client = TestClient(TestServer(server.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        await client.post("/api/login", json={"password": PASSWORD})
        r = await client.get("/api/optional-deps")
        assert r.status == 503
    finally:
        await client.close()
        store.close()


# ----------------------------------------------------------------------
# 装 / 卸
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_returns_202_and_then_ok(env) -> None:
    await env.login()
    r = await env.client.post("/api/optional-deps/office/install", json={})
    assert r.status == 202
    data = await r.json()
    assert data["accepted"] is True
    assert data["groups"][0]["installing"] is True
    assert data["groups"][0]["state"] == "installing"
    await env.deps.wait()
    r2 = await env.client.get("/api/optional-deps")
    data2 = await r2.json()
    assert data2["groups"][0]["state"] == "ok"
    assert data2["groups"][0]["last_install"]["state"] == "ok"


@pytest.mark.asyncio
async def test_install_while_running_is_409(env) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(argv, *, env, timeout):
        started.set()
        await release.wait()
        return 0, "ok", ""

    env.deps._runner = runner
    await env.login()
    first = await env.client.post("/api/optional-deps/office/install", json={})
    assert first.status == 202
    await started.wait()
    second = await env.client.post("/api/optional-deps/office/install", json={})
    assert second.status == 409
    release.set()
    await env.deps.wait()


@pytest.mark.asyncio
async def test_install_unknown_group_404(env) -> None:
    await env.login()
    r = await env.client.post("/api/optional-deps/nope/install", json={})
    assert r.status == 404


@pytest.mark.asyncio
async def test_uninstall_returns_202(env) -> None:
    await env.login()
    r = await env.client.post("/api/optional-deps/office/uninstall", json={})
    assert r.status == 202
    data = await r.json()
    assert data["accepted"] is True
    await env.deps.wait()
    r2 = await env.client.post("/api/optional-deps/nope/uninstall", json={})
    assert r2.status == 404


@pytest.mark.asyncio
async def test_install_and_uninstall_without_local_env_are_409(tmp_path: Path) -> None:
    """本机执行没就位：装 / 卸都要 409 + 中文原因，不能 500。

    卸载这条以前连闸都没有：它会拿「没环境」的错误桩写进 kv，把上次那份好探测结果
    覆盖掉（网页上变成「三库全没了」），所以这里连 kv 一起断言。
    """
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _Settings(served=(G1,), password=PASSWORD)
    deps = OptionalDeps(store, lambda: _Settings(workspace_root=tmp_path / "ws"), env=None)
    svc = _FakeSvc(store, settings, deps)
    server = ConsoleServer(svc)
    client = TestClient(TestServer(server.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        await client.post("/api/login", json={"password": PASSWORD})
        for action in ("install", "uninstall"):
            r = await client.post(f"/api/optional-deps/office/{action}", json={})
            assert r.status == 409, action
            data = await r.json()
            assert "本机执行" in data["error"], action
        assert store.kv_get("optional_deps.check") is None
        assert store.kv_get("optional_deps.office") is None
    finally:
        await client.close()
        store.close()
