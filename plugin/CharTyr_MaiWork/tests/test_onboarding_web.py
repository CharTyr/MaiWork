"""首次安装引导（/api/onboarding）端到端测试。

- 新装（模型没配、没走过引导）→ show=True；
- 已经配好模型的老安装 → show=False（不打扰已经在用的人）；
- 完成 / 跳过都记下来，之后 show=False；reset 后又能重新走；
- 非管理员 403；坏 action 400；checks 反映各项配没配。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.app import MaiWorkApp

pytestmark = pytest.mark.asyncio

PASSWORD = "引导测试密码-显眼-321"


def _raw(data_dir: Path, models: dict[str, Any]) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": "qq:900000001", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": models,
        "storage": {"data_dir": str(data_dir)},
    }


async def _start(tmp_path: Path, models: dict[str, Any]):
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, _raw(tmp_path / "data", models), plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    return app, client


@pytest_asyncio.fixture
async def fresh(tmp_path: Path):
    app, client = await _start(tmp_path, {})
    yield app, client
    await client.close()
    await app.stop()


@pytest_asyncio.fixture
async def ready(tmp_path: Path):
    app, client = await _start(tmp_path, {"base_url": "https://ep.test/v1", "api_key": "sk-t", "main": "m", "worker": "w"})
    yield app, client
    await client.close()
    await app.stop()


async def _login(client: TestClient) -> None:
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


async def test_needs_admin(fresh) -> None:
    _app, client = fresh
    r = await client.get("/api/onboarding")
    assert r.status in (401, 403)
    r = await client.post("/api/onboarding", json={"action": "done"})
    assert r.status in (401, 403)


async def test_fresh_install_shows(fresh) -> None:
    _app, client = fresh
    await _login(client)
    d = await (await client.get("/api/onboarding")).json()
    assert d["show"] is True
    assert d["state"] == ""
    assert d["checks"]["models"] is False
    assert d["checks"]["groups"] is True


async def test_skip_then_hidden_and_reset(fresh) -> None:
    _app, client = fresh
    await _login(client)
    r = await client.post("/api/onboarding", json={"action": "skip"})
    assert r.status == 200
    d = await r.json()
    assert d["show"] is False and d["state"] == "skipped"
    d = await (await client.get("/api/onboarding")).json()
    assert d["show"] is False
    d = await (await client.post("/api/onboarding", json={"action": "reset"})).json()
    assert d["show"] is True and d["state"] == ""


async def test_done_recorded(fresh) -> None:
    _app, client = fresh
    await _login(client)
    d = await (await client.post("/api/onboarding", json={"action": "done"})).json()
    assert d["state"] == "done" and d["show"] is False and d["ts"] > 0


async def test_bad_action(fresh) -> None:
    _app, client = fresh
    await _login(client)
    r = await client.post("/api/onboarding", json={"action": "boom"})
    assert r.status == 400


async def test_existing_install_not_bothered(ready) -> None:
    _app, client = ready
    await _login(client)
    d = await (await client.get("/api/onboarding")).json()
    assert d["show"] is False
    assert d["checks"]["models"] is True
