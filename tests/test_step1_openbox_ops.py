"""Step 1 开箱即用 / 多平台 / 每日硬上限 / 安全备份。"""

from __future__ import annotations

import sqlite3
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock, onboarding, rules, usage_alerts
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments import capability
from CharTyr_MaiWork.maiwork.models import is_template_placeholder
from CharTyr_MaiWork.maiwork.store import Store

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PASSWORD = "步骤一测试密码-显眼-987"
G = "900000001"
TODAY_TS = datetime(2026, 1, 2, 12, 0, tzinfo=clock.BJ).timestamp()
TODAY = "2026-01-02"


# ---------------------------------------------------------------- 1. 默认管理员
def test_default_admins_empty() -> None:
    s, _ = load_settings({})
    assert s.approval.admins == ()
    src = (PLUGIN_DIR / "maiwork" / "rules.py").read_text("utf-8")
    assert "100000001" not in src
    assert "100000001" not in (PLUGIN_DIR / "maiwork" / "config.py").read_text("utf-8")


# ---------------------------------------------------------------- 2. 占位符
def test_placeholder_helper() -> None:
    for v in ("sk-填你的端点密钥", "填你的端点密钥", "填你的密钥", "填服务商那边的模型名"):
        assert is_template_placeholder(v)
    for v in ("", "sk-test", "gpt-4o-mini", "https://api.example.com/v1"):
        assert not is_template_placeholder(v)


def _example_raw(data_dir: Path, *, real: bool = False) -> dict:
    raw = tomllib.loads((PLUGIN_DIR / "config.example.toml").read_text("utf-8"))
    raw["groups"] = {"serve": [{"group": f"qq:{G}", "workspace": "tinker"}]}
    raw["console"] = {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""}
    raw["storage"] = {"data_dir": str(data_dir)}
    raw.setdefault("plugin", {})["enabled"] = True
    if real:
        for ep in raw.get("endpoints", []):
            ep["api_key"] = "sk-real"
        for m in raw.get("model_list", []):
            m["model"] = "gpt-4o-mini"
    return raw


async def _start(raw: dict):
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=PLUGIN_DIR)
    app.profiles_cls = FakeProfiles
    await app.start()
    client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    await client.post("/api/login", json={"password": PASSWORD})
    return app, client


@pytest.mark.asyncio
async def test_example_toml_unedited_not_ready(tmp_path: Path) -> None:
    app, client = await _start(_example_raw(tmp_path / "d"))
    try:
        assert app.models.settings().ready() is False
        assert app.models.settings().key_set is False
        assert onboarding.view(app)["show"] is True
        assert app.models.candidates_for_entry("main") == []
    finally:
        await client.close()
        await app.stop()


@pytest.mark.asyncio
async def test_example_toml_filled_is_ready(tmp_path: Path) -> None:
    app, client = await _start(_example_raw(tmp_path / "d", real=True))
    try:
        assert app.models.candidates_for_entry("main")  # 换成真值后候选能解析出来
    finally:
        await client.close()
        await app.stop()


# ---------------------------------------------------------------- 3. systemd
def test_docker_no_systemd_bus_stopped() -> None:
    caps = capability.LocalCaps(is_linux=True, is_root=True, has_systemd_run=True, run_as_exists=True, has_systemd_bus=False)
    dec = capability.detect(caps)
    assert dec.mode == "stopped" and dec.ok is False
    assert "systemd 托管" in dec.reason
    root = capability.resolve_workspace_root(Path("/home/maiwork/workspaces"), dec, data_dir=Path("/data/mw"))
    assert root == Path("/data/mw/workspaces")


# ---------------------------------------------------------------- 5. 硬上限
def _hard_settings(limit: int):
    s, problems = load_settings({"groups": {"serve": [{"group": f"qq:{G}"}]}, "usage": {"hard_limit_daily_tokens": limit}})
    assert not problems, problems
    return s


def _usage(store: Store, gid: str, tokens: int, role: str = "main") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
            " prompt_tokens, completion_tokens, ok, ms, error)"
            " VALUES (?, ?, ?, 'm', '', ?, '', ?, 0, 1, 0, '')",
            (TODAY_TS, TODAY, role, gid, tokens),
        )


def test_hard_limit_config_and_schema() -> None:
    assert _hard_settings(0).usage.hard_limit_daily_tokens == 0
    assert _hard_settings(5000).usage.hard_limit_daily_tokens == 5000
    keys = [f["key"] for f in rules.CONFIG_SCHEMA]
    i = keys.index("usage.alert_daily_tokens")
    assert keys[i + 1] == "usage.hard_limit_daily_tokens"
    assert "hard_limit_daily_tokens" in (PLUGIN_DIR / "config.example.toml").read_text("utf-8")


def test_hard_limit_status_and_alert(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.migrate()
    _usage(store, G, 6000)
    _usage(store, "", 5000, role="worker")
    st = usage_alerts.daily_hard_limit_status(store, _hard_settings(0), now=TODAY_TS)
    assert st["tripped"] is False and st["limit"] == 0
    st = usage_alerts.daily_hard_limit_status(store, _hard_settings(20000), now=TODAY_TS)
    assert st == {"tripped": False, "limit": 20000, "used": 11000, "day": TODAY}
    s = _hard_settings(10000)
    st = usage_alerts.daily_hard_limit_status(store, s, now=TODAY_TS)
    assert st["tripped"] is True and st["used"] == 11000
    out = usage_alerts.check(store, lambda: s, TODAY_TS)
    assert [a["kind"] for a in out] == ["hard_limit"]
    assert usage_alerts.check(store, lambda: s, TODAY_TS) == []  # 一天一次
    assert store.kv_get(f"usage.alerted.hard.{TODAY}") is not None


class _Boom:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"tripped 时不许碰模型：{name}")


@pytest.mark.asyncio
async def test_hard_limit_blocks_background(tmp_path: Path, monkeypatch) -> None:
    from CharTyr_MaiWork.maiwork import feeds as feeds_mod, skill_explore, topics as topics_mod

    store = Store(tmp_path / "t.db")
    store.migrate()
    s = _hard_settings(100)
    _usage(store, G, 1000)
    monkeypatch.setattr(clock, "now", lambda: TODAY_TS)
    assert usage_alerts.daily_hard_limit_status(store, s)["tripped"] is True

    class _Agents:
        _get_settings = staticmethod(lambda: s)

        def _verify_served(self, gid: str) -> None:
            return None

    out = await skill_explore.run(store, _Boom(), _Boom(), _Boom(), _Agents(), G, TODAY_TS)
    assert out["ran"] is False and "硬上限" in out["reason"]

    t = topics_mod.Topics.__new__(topics_mod.Topics)
    t._store, t._get_settings, t._models = store, (lambda: s), _Boom()
    res = await t.check(G, TODAY_TS)
    assert str(res).startswith("skip:") and "硬上限" in res

    f = feeds_mod.Feeds.__new__(feeds_mod.Feeds)
    f._store, f._get_settings, f._models = store, (lambda: s), _Boom()
    f._round_note_hids = feeds_mod.contextvars.ContextVar("test_note_hids", default=[])
    assert await f.make_idea(G) is None
    assert not await f.prepare_news(G)

    called = []

    async def _fake_news_round(gid: str, *, scope=None) -> int:
        called.append(("news", gid))
        return 2

    async def _fake_idea_round(gid: str, *, scope=None) -> int | None:
        called.append(("idea", gid))
        return 7

    f._prepare_news_round = _fake_news_round
    f._make_idea_round = _fake_idea_round
    assert await f.prepare_news(G, manual=True) == 2
    assert await f.make_idea(G, manual=True) == 7
    assert called == [("news", G), ("idea", G)]


# ---------------------------------------------------------------- 4/6 web
@pytest_asyncio.fixture
async def web_app(tmp_path: Path):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G}", "workspace": "tinker"}, {"group": "telegram:-1001234", "workspace": "tg"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(tmp_path / "data")},
    }
    app, client = await _start(raw)
    yield app, client
    await client.close()
    await app.stop()


@pytest.mark.asyncio
async def test_focus_non_qq_ids(web_app) -> None:
    app, client = web_app
    r = await client.post(f"/api/groups/{G}/focus", json={"user_id": "abc", "action": "add"})
    assert r.status == 400
    r = await client.post("/api/groups/-1001234/focus", json={"user_id": "AbC_123-x", "action": "add"})
    assert r.status == 400  # '-' 只许打头
    r = await client.post("/api/groups/-1001234/focus", json={"user_id": "o_ABCdef123", "action": "add"})
    assert r.status == 200, await r.text()
    r = await client.post("/api/groups/-1001234/focus", json={"user_id": "a b", "action": "add"})
    assert r.status == 400 and "账号格式不正确" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_onboarding_admins_from_group_approvals(web_app) -> None:
    app, _client = web_app
    assert onboarding._checks(app)["admins"] is False
    from CharTyr_MaiWork.maiwork.group_approval import GroupApprovals

    GroupApprovals(app.store, get_settings=app.get_settings).set(
        G, {"approvers": ["qq:123456789"], "exempt_users": [], "exempt_group": False, "required": True}
    )
    assert onboarding._checks(app)["admins"] is True


@pytest.mark.asyncio
async def test_backup_scrubbed(web_app, tmp_path: Path) -> None:
    app, client = web_app
    with app.store.tx() as conn:
        for k in ("auth.session.x", "console.secret", "group_admin.123", "social.qq", "extensions.mcp", "extensions.search"):
            app.store.kv_set(conn, k, {"token": "TOPSECRET-VALUE"})
        app.store.kv_set(conn, "keep.me", {"v": 1})
    anon = TestClient(TestServer(app.console.app))
    await anon.start_server()
    try:
        assert (await anon.get("/api/settings/backup")).status in (401, 403)
    finally:
        await anon.close()
    r = await client.get("/api/settings/backup")
    assert r.status == 200
    assert "maiwork-backup-" in r.headers["Content-Disposition"]
    data = await r.read()
    assert b"TOPSECRET-VALUE" not in data
    p = tmp_path / "b.db"
    p.write_bytes(data)
    conn = sqlite3.connect(p)
    keys = {row[0] for row in conn.execute("SELECT key FROM kv")}
    conn.close()
    assert "keep.me" in keys
    assert not any(k.startswith(("auth.", "console.", "group_admin.", "social.", "extensions.mcp", "extensions.search")) for k in keys)
