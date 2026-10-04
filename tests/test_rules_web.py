"""网页改规则走「全部配置」端点（/api/settings/config）后的行为测试（起真 app + console；只本机回环）。

docs/18 第一步之前，网页改规则走 /api/settings/rules 写 kv["rules.override"]，
会赢过 config.toml（「网页改了不生效」「旧键藏一层」）。现在：
- 旧端点全删（GET/PUT /api/settings/rules、POST /api/settings/rules/reset → 404）；
- 改规则 = PUT /api/settings/config 直写 config.toml，立刻热生效；
- settings 摘要 / 主循环读的都是热更新后的有效值（topics.enabled 关掉下一轮立刻停）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp


PASSWORD = "规则测试密码-显眼-123"
G1 = "900000001"


def _raw_config(data_dir: Path, **over: Any) -> dict:
    raw: dict[str, Any] = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "delivery": {"push_per_day": 3, "quiet_hours": "23:00-08:00"},
        "topics": {"enabled": True, "per_day": 2, "min_gap_hours": 3},
        "approval": {"required": True, "admins": ["10001"], "remind": True},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class _Env:
    def __init__(self, app: MaiWorkApp, client: TestClient, plug_dir: Path) -> None:
        self.app = app
        self.client = client
        self.plug_dir = plug_dir

    def config_text(self) -> str:
        return (self.plug_dir / "config.toml").read_text(encoding="utf-8")


def _write_plugin_dir(plug_dir: Path, raw: dict) -> None:
    """模拟线上真实 config.toml（带注释）造临时插件目录，别让 app 动仓库里那份。"""
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc.add(tomlkit.comment("测试用 config.toml"))
    for section, values in raw.items():
        if not isinstance(values, dict):
            continue
        t = tomlkit.table()
        for k, v in values.items():
            if isinstance(k, str) and ":" in k:  # delivery.quiet_hours 是嵌套 dict 里的普通键
                continue
            t[k] = v
        doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    data_dir = tmp_path / "data"
    plug_dir = tmp_path / "plug"
    raw = _raw_config(data_dir)
    _write_plugin_dir(plug_dir, raw)
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield _Env(app=app, client=client, plug_dir=plug_dir)
    await client.close()
    await app.stop()


async def _login(env: _Env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


class TestOldRulesEndpointsGone:
    """旧 /api/settings/rules 系列已删（no-route → 404，group_admin 也 404）。"""

    @pytest.mark.asyncio
    async def test_old_get_gone(self, env: _Env) -> None:
        await _login(env)
        assert (await env.client.get("/api/settings/rules")).status == 404

    @pytest.mark.asyncio
    async def test_old_put_gone(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/rules", json={"topics": {"enabled": False}})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_old_reset_gone(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.post("/api/settings/rules/reset", json={"field": "x"})
        assert r.status == 404


class TestRulesViaConfig:
    """改规则 = PUT /api/settings/config（扁平 {"节.字段": 值}）直写 config.toml。"""

    @pytest.mark.asyncio
    async def test_put_writes_file_and_takes_effect(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 7})
        assert r.status == 200, await r.json()
        assert "min_gap_hours = 7" in env.config_text()
        assert env.app.get_settings().topics.min_gap_hours == 7  # 热生效不重启

    @pytest.mark.asyncio
    async def test_put_bad_value_400(self, env: _Env) -> None:
        await _login(env)
        before = env.config_text()
        r = await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 99})
        assert r.status == 400
        assert env.config_text() == before  # 文件没动

    @pytest.mark.asyncio
    async def test_group_managed_keys_rejected_400(self, env: _Env) -> None:
        """0.8.0：批准 / 推送 / 开话题那几键归每个群自己管，旧全局 API 明确 400。"""
        await _login(env)
        before = env.config_text()
        for key, value in (
            ("topics.enabled", False),
            ("topics.per_day", 5),
            ("delivery.push_per_day", 8),
            ("approval.required", False),
        ):
            r = await env.client.put("/api/settings/config", json={key: value})
            assert r.status == 400, key
            assert "群" in (await r.json())["error"], key
        assert env.config_text() == before
        # runtime 读的还是每群那一份（全局值没被写热）
        assert env.app.get_settings().delivery.push_per_day == 3

    @pytest.mark.asyncio
    async def test_anonymous_401(self, env: _Env) -> None:
        assert (await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 7})).status == 401

    @pytest.mark.asyncio
    async def test_reset_removes_key_from_file(self, env: _Env) -> None:
        await _login(env)
        await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 7})
        assert "min_gap_hours = 7" in env.config_text()
        r = await env.client.post("/api/settings/config/reset", json={"field": "topics.min_gap_hours"})
        assert r.status == 200, await r.json()
        import tomllib

        parsed = tomllib.loads(env.config_text())
        assert "min_gap_hours" not in parsed.get("topics", {})  # 键删了，回代码默认值
        assert parsed["approval"]["remind"] is True  # 仍归全局的键不动
        assert env.app.store.kv_get(f"group_push.{G1}")["daily_max"] == 3  # 每群节制也没被全局 reset 改掉
        assert env.app.get_settings().topics.min_gap_hours == 3  # raw/文件都没有 → 代码默认 3

    @pytest.mark.asyncio
    async def test_settings_view_uses_effective_values(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 8})
        assert r.status == 200
        r = await env.client.get("/api/settings")
        data = await r.json()
        # 每群那几项（推送上限 / 开话题上限）不再由全局改：摘要还是文件里那份种子值
        assert data["rules"]["push_per_day"] == 3
        assert data["rules"]["topics_per_day"] == 2

    @pytest.mark.asyncio
    async def test_rules_summary_fields(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.get("/api/settings")
        data = await r.json()
        assert data["rules"]["push_per_day"] == 3
        assert data["rules"]["quiet_hours"] == "23:00-08:00"
        assert data["rules"]["topics_per_day"] == 2

    @pytest.mark.asyncio
    async def test_topics_disabled_stops_topic_round(self, env: _Env) -> None:
        """开关现在是每群那一份（group_push）：关掉本群的 → 本群那轮直接 skip:disabled。

        全局 topics.enabled 已经不能从网页改（400）；runtime 只听每群那份。
        """
        from CharTyr_MaiWork.maiwork import clock, group_push

        await _login(env)
        assert env.app.topics is not None
        settings = env.app.get_settings()
        now = clock.now()
        # 睡觉时段调成「零窗口」，免得跑测试正好撞上 23:00-08:00
        group_push.set_config(env.app.store, G1, {"quiet_hours": "00:00-00:00"}, settings)
        on = await env.app.topics.check(G1, now)  # type: ignore[union-attr]
        assert on != "skip:disabled"
        # 全局那键改不了（400），runtime 也不会听
        r = await env.client.put("/api/settings/config", json={"topics.enabled": False})
        assert r.status == 400
        # 改本群那份 → 本群立刻停
        group_push.set_config(env.app.store, G1, {"topics_enabled": False}, settings)
        assert await env.app.topics.check(G1, now) == "skip:disabled"  # type: ignore[union-attr]


def test_retired_speaker_hint_does_not_point_to_missing_control() -> None:
    """topics.speaker 已退役：群页没有对应开关，提示不能把人指过去。"""
    from CharTyr_MaiWork.maiwork import rules as _rules

    hint = _rules.group_managed_hint("topics.speaker")
    assert "退役" in hint
    assert "往群里发" not in hint
