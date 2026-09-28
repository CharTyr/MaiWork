"""网页规则接口 + 有效设置生效的端到端测试（起真 app + console；本机回环，不碰外网）。

- GET /api/settings/rules：鉴权（匿名 401）、response 三件套（values/overridden/defaults_from_file）；
- PUT：改完 kv 落库、get_settings() 立刻返回合并后的有效值（不重启、下一轮就生效）、
  写坏的字段 400 且不落库、跨源 Origin 403；
- POST reset：恢复 config.toml 的值；
- 立刻生效：topics.enabled 关掉后 app.get_settings().topics.enabled 当场 False，
  主循环 _topics_round 不再执行开话题（下一轮的「不做任何事」验证）；
- settings 视图 rules 摘要用有效值。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork import rules as rules_mod

SECRET = "sk-rules-web-test-显眼Aa1"
PASSWORD = "规则测试密码-显眼-123"
G1 = "900000001"


def _raw_config(data_dir: Path, **over: Any) -> dict:
    raw: dict[str, Any] = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "delivery": {"push_per_day": 3, "quiet_hours": "23:00-08:00"},
        "topics": {"enabled": True, "per_day": 2, "min_gap_hours": 3},
        "approval": {"required": True, "admins": ["10001"], "remind": True},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class _Env:
    def __init__(self, app: MaiWorkApp, client: TestClient) -> None:
        self.app = app
        self.client = client


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = _raw_config(tmp_path / "data")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield _Env(app=app, client=client)
    await client.close()
    await app.stop()


async def _login(env: _Env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


class TestRulesApi:
    @pytest.mark.asyncio
    async def test_get_requires_admin(self, env: _Env) -> None:
        r = await env.client.get("/api/settings/rules")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_get_shape_and_defaults(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.get("/api/settings/rules")
        assert r.status == 200
        data = await r.json()
        assert data["overridden"] == []
        assert data["values"]["delivery"]["push_per_day"] == 3
        assert data["values"]["delivery"]["quiet_hours"] == "23:00-08:00"
        assert data["values"]["topics"] == {"enabled": True, "per_day": 2, "min_gap_hours": 3}
        assert data["values"]["approval"]["admins"] == ["qq:10001"]
        assert data["values"]["feeds"]["news_slots"] == ["08:30", "14:00", "19:00"]
        assert data["defaults_from_file"]["delivery"]["push_per_day"] == 3
        assert set(data["values"].keys()) == {"delivery", "topics", "approval", "feeds"}
        assert set(data["defaults_from_file"].keys()) == set(data["values"].keys())

    @pytest.mark.asyncio
    async def test_put_merges_and_takes_effect_immediately(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/rules", json={"topics": {"enabled": False}, "delivery": {"push_per_day": 7}})
        assert r.status == 200
        data = await r.json()
        assert data["values"]["topics"]["enabled"] is False
        assert data["values"]["delivery"]["push_per_day"] == 7
        assert sorted(data["overridden"]) == ["delivery.push_per_day", "topics.enabled"]
        assert data["defaults_from_file"]["topics"]["enabled"] is True
        # 不重启：app.get_settings() 立刻给合并后的有效值
        settings = env.app.get_settings()
        assert settings.topics.enabled is False
        assert settings.delivery.push_per_day == 7
        # 基础 config 不变（settings.problems 那一份还是文件值）
        assert env.app.problems is not None
        # kv 里只存改过的
        over = env.app.store.kv_get(rules_mod.KV_OVERRIDE)
        assert over == {"topics": {"enabled": False}, "delivery": {"push_per_day": 7}}

    @pytest.mark.asyncio
    async def test_put_invalid_field_400_and_no_write(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/rules", json={"delivery": {"push_per_day": 99, "quiet_hours": "22:00-07:00"}})
        assert r.status == 400
        body = await r.json()
        assert "推送上限" in body["error"]
        assert env.app.store.kv_get(rules_mod.KV_OVERRIDE) is None
        assert env.app.get_settings().delivery.push_per_day == 3

    @pytest.mark.asyncio
    async def test_put_unknown_field_400(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/rules", json={"models": {"main": "x"}})
        assert r.status == 400
        # delivery.mention_ttl_minutes 不在白名单
        r2 = await env.client.put("/api/settings/rules", json={"delivery": {"mention_ttl_minutes": 60}})
        assert r2.status == 400

    @pytest.mark.asyncio
    async def test_put_same_as_config_clears_override(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/rules", json={"delivery": {"push_per_day": 7}})
        assert r.status == 200
        r2 = await env.client.put("/api/settings/rules", json={"delivery": {"push_per_day": 3}})
        assert r2.status == 200
        data = await r2.json()
        assert data["overridden"] == []
        assert env.app.store.kv_get(rules_mod.KV_OVERRIDE) in (None, {})
        assert env.app.get_settings().delivery.push_per_day == 3

    @pytest.mark.asyncio
    async def test_put_origin_mismatch_403(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put(
            "/api/settings/rules",
            json={"topics": {"enabled": False}},
            headers={"Origin": "http://evil.example.com:1"},
        )
        assert r.status == 403
        assert env.app.store.kv_get(rules_mod.KV_OVERRIDE) is None

    @pytest.mark.asyncio
    async def test_put_requires_admin(self, env: _Env) -> None:
        r = await env.client.put("/api/settings/rules", json={"topics": {"enabled": False}})
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_reset_field(self, env: _Env) -> None:
        await _login(env)
        await env.client.put("/api/settings/rules", json={"delivery": {"push_per_day": 7, "quiet_hours": "22:00-07:00"}})
        r = await env.client.post("/api/settings/rules/reset", json={"field": "delivery.push_per_day"})
        assert r.status == 200
        data = await r.json()
        assert data["overridden"] == ["delivery.quiet_hours"]
        assert data["values"]["delivery"]["push_per_day"] == 3
        assert data["values"]["delivery"]["quiet_hours"] == "22:00-07:00"
        assert env.app.get_settings().delivery.push_per_day == 3

    @pytest.mark.asyncio
    async def test_reset_unknown_field_400(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.post("/api/settings/rules/reset", json={"field": "delivery.mention_ttl_minutes"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_settings_view_uses_effective_values(self, env: _Env) -> None:
        await _login(env)
        await env.client.put("/api/settings/rules", json={"delivery": {"push_per_day": 8}, "topics": {"per_day": 5}})
        r = await env.client.get("/api/settings")
        assert r.status == 200
        data = await r.json()
        assert data["rules"]["push_per_day"] == 8
        assert data["rules"]["topics_per_day"] == 5

    @pytest.mark.asyncio
    async def test_topics_disabled_stops_topic_round(self, env: _Env) -> None:
        """topics.enabled 关掉要立刻停：下一轮主循环 _topics_round 不调 topics.check。"""
        await _login(env)

        class _SpyTopics:
            def __init__(self) -> None:
                self.check_calls: list[str] = []
                self.follow_up_calls: list[str] = []

            async def check(self, gid: str, now: float) -> str:
                self.check_calls.append(gid)
                return "skip:test"

            async def follow_up(self, gid: str, now: float) -> None:
                self.follow_up_calls.append(gid)

        spy = _SpyTopics()
        env.app.topics = spy
        # 先跑一轮：还是会调（开关没关）
        await env.app.run_loop_once()
        assert spy.check_calls == [G1]
        # 关掉开关 → 下一轮就不该再调（读的是有效设置，不用重启）
        await env.client.put("/api/settings/rules", json={"topics": {"enabled": False}})
        await env.app.run_loop_once()
        assert spy.check_calls == [G1]  # 没增加
