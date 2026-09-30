"""通用网页配置接口（/api/settings/config）端到端测试（起真 app + console；本机回环，不碰外网）。

2026-10：网页改配置 = 直写插件目录下的 config.toml（测试用临时插件目录+临时 config.toml）。
覆盖：
- schema 覆盖面：config.py 各节配置字段（除 models / extensions / plugin.config_version /
  feeds.min_score 弃用字段外）每个都在 rules.py 的 CONFIG_SCHEMA 里——防以后加配置忘了加网页项；
- GET 形状：file/sections/fields 各字段齐全（value/default/changed），secret 不给值只给 set/source；
- 校验：坏值 400 且 config.toml 不变；readonly 拒绝；local_mode=direct 拒绝；改密码要旧密码；
- 写文件：PUT 后 config.toml 里能看到新值（注释保留）、get_settings() 立刻生效；
- 密钥只进不出：PUT 写进 config.toml（明文），GET 永远不回值，source=file/env；
- reset：删掉这个键回代码默认值；
- 插件目录不留新文件（除 config.toml 内容本身），备份在数据目录 config-backups/。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import rules as rules_mod
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.jev import _resolve_key
from CharTyr_MaiWork.maiwork.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()

PASSWORD = "配置测试密码-显眼-456"
NEW_PASSWORD = "新密码-显眼-789"
G1 = "900000001"


def _raw_config(data_dir: Path, **over: Any) -> dict:
    raw: dict[str, Any] = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"admins": ["10001"]},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class _Env:
    def __init__(self, app: MaiWorkApp, client: TestClient, plug_dir: Path, data_dir: Path) -> None:
        self.app = app
        self.client = client
        self.plug_dir = plug_dir
        self.data_dir = data_dir

    def config_text(self) -> str:
        return (self.plug_dir / "config.toml").read_text(encoding="utf-8")


def _write_plugin_dir(plug_dir: Path, raw: dict) -> None:
    """造一个临时插件目录：config.toml 按 raw 的配置写出来（模拟线上真实文件）。"""
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc.add(tomlkit.comment("测试用 config.toml；注释不许丢"))
    for section, values in raw.items():
        if not isinstance(values, dict):
            continue
        t = tomlkit.table()
        for k, v in values.items():
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
    yield _Env(app=app, client=client, plug_dir=plug_dir, data_dir=data_dir)
    await client.close()
    await app.stop()


async def _login(env: _Env, password: str = PASSWORD) -> None:
    r = await env.client.post("/api/login", json={"password": password})
    assert r.status == 200


def _field(data: dict, key: str) -> dict:
    for section in data["sections"]:
        for f in section["fields"]:
            if f["key"] == key:
                return f
    raise AssertionError(f"字段 {key} 不在返回里")


class TestSchemaCoverage:
    def test_every_config_field_in_schema(self) -> None:
        """config.py 各节字段（models / extensions 节、plugin 节（版本标记和总开关）、弃用的
        feeds.min_score 除外）每一个都要在 CONFIG_SCHEMA 里——防以后加配置忘了加网页项。"""
        from CharTyr_MaiWork.maiwork import config as config_mod

        schema_keys = {f["key"] for f in rules_mod.CONFIG_SCHEMA}
        missing: list[str] = []
        for section, cls in config_mod._SECTIONS:
            if section in ("models", "extensions"):
                continue
            for name in cls.model_fields:
                if name == "config_version":
                    continue
                if section == "plugin" and name == "enabled":
                    continue  # 总开关不上网页（关了网页自己就没了；2026-09-28 用户要求拿掉）
                if section == "feeds" and name == "min_score":
                    continue  # 已弃用（2026-09-27 起改看 web_min_avg）
                key = f"{section}.{name}"
                if key not in schema_keys:
                    missing.append(key)
        assert not missing, f"config 里的这些字段没进 CONFIG_SCHEMA：{missing}"

    def test_schema_field_shapes(self) -> None:
        """每个 schema 条目：key 是「节.字段」、类型合法、enum 必有 options。"""
        types = {"bool", "int", "float", "str", "enum", "list_str", "secret", "serve_groups", "ssh_list", "time_list"}
        for f in rules_mod.CONFIG_SCHEMA:
            assert f["key"].count(".") == 1
            assert f["type"] in types
            # 说明是可选的（2026-09-28：网页文案按产品口吻精简，名字已经说清楚的不再配说明）
            assert f["label"] and isinstance(f["help"], str)
            assert f["applies"] in ("now", "reload")
            if f["type"] == "enum":
                assert f.get("options"), f'{f["key"]} 是 enum 但没给 options'
            if f.get("readonly"):
                assert f.get("readonly_reason"), f'{f["key"]} readonly 要给理由'


class TestConfigApi:
    @pytest.mark.asyncio
    async def test_get_requires_admin(self, env: _Env) -> None:
        assert (await env.client.get("/api/settings/config")).status == 401
        assert (await env.client.put("/api/settings/config", json={"topics.per_day": 3})).status == 401
        assert (await env.client.post("/api/settings/config/reset", json={"field": "topics.per_day"})).status == 401

    @pytest.mark.asyncio
    async def test_get_shape(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.get("/api/settings/config")
        assert r.status == 200
        data = await r.json()
        assert data["reload_pending"] == []
        ids = [s["id"] for s in data["sections"]]
        # 「启用 MaiWork」总开关不上网页（关了网页自己就没了），整个「插件」分区不列
        assert "plugin.enabled" not in {f["key"] for sec in data["sections"] for f in sec["fields"]}
        # 0.4.0：模型上下文窗口（models 节）+ 任务安全网（tasks 节）也上网页「全部配置」
        assert ids == [
            "groups", "focus", "feeds", "goals", "topics", "delivery", "approval",
            "tasks", "models", "jev", "usage", "console", "environments", "profile",
            "storage", "group_space", "reader",
        ]
        assert data["file"] == "config.toml"
        f = _field(data, "topics.per_day")
        assert f["value"] == 2 and f["default"] == 2 and f["changed"] is False
        assert "file_value" not in f and "overridden" not in f
        assert f["type"] == "int" and f["applies"] == "now"
        assert f["min"] == 1 and f["max"] == 10

    @pytest.mark.asyncio
    async def test_secret_never_echoed(self, env: _Env) -> None:
        """secret 类型：GET 不回 value/file_value，只给 set/source；文件里配的 source=file。"""
        await _login(env)
        data = await (await env.client.get("/api/settings/config")).json()
        for key in ("jev.api_key", "console.password"):
            f = _field(data, key)
            assert "value" not in f and "file_value" not in f
            assert isinstance(f["set"], bool) and f["source"] in ("file", "env", "none")
        assert _field(data, "console.password")["source"] == "file"
        assert _field(data, "jev.api_key")["set"] is False
        assert _field(data, "jev.api_key")["source"] == "none"
        # [search] 段 2026-10 已删（搜索走扩展绑定，不进配置表）
        assert "search" not in {s["id"] for s in data["sections"]}

    @pytest.mark.asyncio
    async def test_readonly_rejected(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"storage.data_dir": "/tmp/x"})
        assert r.status == 400
        assert "数据目录" in (await r.json())["error"]
        r = await env.client.put("/api/settings/config", json={"plugin.enabled": False})
        assert r.status == 400
        # reset 也不能动 readonly
        r = await env.client.post("/api/settings/config/reset", json={"field": "storage.data_dir"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_local_mode_direct_rejected(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"environments.local_mode": "direct"})
        assert r.status == 400
        assert "config.toml" in (await r.json())["error"]
        r = await env.client.put("/api/settings/config", json={"environments.local_mode": "systemd"})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_validation_failure_400_no_write(self, env: _Env) -> None:
        await _login(env)
        before = env.config_text()
        r = await env.client.put(
            "/api/settings/config",
            json={"topics.per_day": 99, "delivery.push_per_day": 7},
        )
        assert r.status == 400
        # 校验失败：config.toml 一个字节都没动，数据库也没有覆盖层
        assert env.config_text() == before
        assert env.app.store.kv_get(rules_mod.KV_CONFIG_OVERRIDE) is None
        assert env.app.get_settings().delivery.push_per_day == 3
        # 不认识的键
        r = await env.client.put("/api/settings/config", json={"bogus.key": 1})
        assert r.status == 400
        # models / extensions 节不进通用表
        r = await env.client.put("/api/settings/config", json={"models.main": "x"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_put_writes_file_and_effective_now(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put(
            "/api/settings/config",
            json={"topics.per_day": 5, "profile.read_interval_minutes": 30, "usage.alert_daily_tokens": 1000},
        )
        assert r.status == 200
        data = await r.json()
        assert _field(data, "topics.per_day")["value"] == 5
        assert _field(data, "topics.per_day")["changed"] is True
        assert _field(data, "topics.per_day")["default"] == 2
        # config.toml 里真的写进去了（注释也还在）
        text = env.config_text()
        assert "per_day = 5" in text
        assert "read_interval_minutes = 30" in text
        assert "alert_daily_tokens = 1000" in text
        assert "注释不许丢" in text
        # 数据库没有配置覆盖层
        assert env.app.store.kv_get(rules_mod.KV_CONFIG_OVERRIDE) is None
        # 立刻生效（不重启）
        settings = env.app.get_settings()
        assert settings.topics.per_day == 5
        assert settings.profile.read_interval_minutes == 30
        assert settings.usage.alert_daily_tokens == 1000
        # 再写一次别的值 = 覆盖同一个键
        r = await env.client.put("/api/settings/config", json={"topics.per_day": 2})
        assert r.status == 200
        assert env.app.get_settings().topics.per_day == 2
        # 备份写到了数据目录
        backups = list((env.data_dir / "config-backups").glob("config.toml-*"))
        assert backups
        # 插件目录没有留下任何新文件
        assert sorted(p.name for p in env.plug_dir.iterdir()) == ["config.toml"]

    @pytest.mark.asyncio
    async def test_groups_serve_hot_apply_no_reload_pending(self, env: _Env) -> None:
        """groups.serve 以前要重载才生效；现在写进 config.toml 立刻热生效，reload_pending 永远 []。"""
        await _login(env)
        r = await env.client.put(
            "/api/settings/config",
            json={"groups.serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": "qq:10086", "workspace": ""}]},
        )
        assert r.status == 200
        data = await r.json()
        assert data["reload_pending"] == []
        f = _field(data, "groups.serve")
        assert f["applies"] == "now" and f["changed"] is True
        assert f["value"] == [
            {"group": f"qq:{G1}", "workspace": "tinker"},
            {"group": "qq:10086", "workspace": "g10086"},
        ]
        # 立刻热生效：新群出现在 get_settings 里
        assert env.app.get_settings().is_served("10086")
        # reset：删掉这个键回代码默认（空服务群列表）
        r = await env.client.post("/api/settings/config/reset", json={"field": "groups.serve"})
        assert (await r.json())["reload_pending"] == []
        assert not env.app.get_settings().is_served("10086")
        # 文件里 [[groups.serve]] 也没了
        assert "[[groups.serve]]" not in env.config_text()

    @pytest.mark.asyncio
    async def test_groups_serve_validation(self, env: _Env) -> None:
        await _login(env)
        bad = [
            [{"group": "abc:1"}],                                  # 不是 qq 平台
            [{"group": "qq:abc"}],                                 # 群号不是数字
            [{"group": f"qq:{G1}", "workspace": "带空格"}],        # workspace 不合法
            [{"group": f"qq:{G1}"}, {"group": f"qq:{G1}"}],        # 重复
        ]
        before = env.config_text()
        for value in bad:
            r = await env.client.put("/api/settings/config", json={"groups.serve": value})
            assert r.status == 400, value
        assert env.config_text() == before

    @pytest.mark.asyncio
    async def test_groups_serve_accepts_telegram(self, env: _Env) -> None:
        """网页设置也能存 Telegram 群（tg: 归一成 telegram:，默认工作区名清洗过）。"""
        await _login(env)
        r = await env.client.put(
            "/api/settings/config",
            json={"groups.serve": [{"group": "tg:-1001234567890"}, {"group": "telegram:-100123::tg-topic::mt=5"}]},
        )
        assert r.status == 200, await r.text()
        s = env.app.get_settings()
        assert s.is_served("-1001234567890") and s.platform_of("-1001234567890") == "telegram"
        assert s.is_served("-100123::tg-topic::mt=5")
        assert "telegram:-1001234567890" in env.config_text()

    @pytest.mark.asyncio
    async def test_ssh_list_and_time_list(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.put(
            "/api/settings/config",
            json={
                "environments.ssh": [{"name": "vps1", "host": "maiwork@1.2.3.4:22", "note": "小机器"}],
                "feeds.news_slots": ["08:30", "20:00"],
            },
        )
        assert r.status == 200
        data = await r.json()
        assert _field(data, "environments.ssh")["value"][0]["host"] == "maiwork@1.2.3.4:22"
        assert _field(data, "feeds.news_slots")["value"] == ["08:30", "20:00"]
        assert env.app.get_settings().environments.ssh[0].name == "vps1"
        assert env.app.get_settings().feeds.news_slots == ("08:30", "20:00")
        # 坏条目
        r = await env.client.put("/api/settings/config", json={"environments.ssh": [{"name": "", "host": ""}]})
        assert r.status == 400
        r = await env.client.put("/api/settings/config", json={"feeds.news_slots": ["8点半"]})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_secret_set_and_clear(self, env: _Env) -> None:
        await _login(env)
        # 设密钥：写进 config.toml（明文），GET 不回值、来源 file，数据库 secrets 表没有
        r = await env.client.put("/api/settings/config", json={"jev.api_key": "jev-web-key-显眼"})
        assert r.status == 200
        data = await r.json()
        f = _field(data, "jev.api_key")
        assert f["source"] == "file" and f["set"] is True
        assert "value" not in f
        assert 'api_key = "jev-web-key-显眼"' in env.config_text()
        assert env.app.store.secret_get("jev_api_key") == ""
        # 并进已知密钥遮罩
        assert "jev-web-key-显眼" in env.app._known_secrets()
        # 有效设置立刻跟上
        assert env.app.get_settings().jev.api_key == "jev-web-key-显眼"
        # PUT 不给这个键 / 给空串 = 不改
        r = await env.client.put("/api/settings/config", json={"topics.per_day": 3})
        assert r.status == 200
        assert 'api_key = "jev-web-key-显眼"' in env.config_text()
        r = await env.client.put("/api/settings/config", json={"jev.api_key": ""})
        assert r.status == 200
        assert 'api_key = "jev-web-key-显眼"' in env.config_text()
        # 给 null = 清空（文件里写 ""）
        r = await env.client.put("/api/settings/config", json={"jev.api_key": None})
        assert r.status == 200
        data = await r.json()
        f = _field(data, "jev.api_key")
        assert f["set"] is False
        assert 'api_key = ""' in env.config_text()
        assert env.app.get_settings().jev.api_key == ""
        # reset = 删掉这个键回代码默认（空）
        await env.client.put("/api/settings/config", json={"jev.api_key": "jev-web-key2"})
        r = await env.client.post("/api/settings/config/reset", json={"field": "jev.api_key"})
        assert r.status == 200
        assert env.app.get_settings().jev.api_key == ""
        # 密钥不出现在接口返回的任何字符串里
        raw = await r.text()
        assert "jev-web-key2" not in raw

    @pytest.mark.asyncio
    async def test_password_change_requires_current(self, env: _Env) -> None:
        await _login(env)
        # 不给旧密码
        r = await env.client.put("/api/settings/config", json={"console.password": NEW_PASSWORD})
        assert r.status == 400
        # 给错旧密码
        r = await env.client.put(
            "/api/settings/config",
            json={"console.password": NEW_PASSWORD, "current_password": "不对"},
        )
        assert r.status == 400
        assert f'password = "{PASSWORD}"' in env.config_text()
        # 给对旧密码
        r = await env.client.put(
            "/api/settings/config",
            json={"console.password": NEW_PASSWORD, "current_password": PASSWORD},
        )
        assert r.status == 200
        data = await r.json()
        f = _field(data, "console.password")
        assert f["source"] == "file" and f["set"] is True
        assert f'password = "{NEW_PASSWORD}"' in env.config_text()
        # 新密码能登录（旧 cookie 已失效：密码指纹变了）
        r = await env.client.post("/api/login", json={"password": NEW_PASSWORD})
        assert r.status == 200
        # reset：删掉 console.password 键 → 回代码默认（空 → 自动生成那把）
        r = await env.client.post("/api/settings/config/reset", json={"field": "console.password"})
        assert r.status == 200
        assert "password" not in env.config_text().split("[console]")[1].split("[")[0]
        # 自动生成的密码已就位（哈希在数据库里）
        assert env.app.store.secret_get("admin_password_hash")


class TestJevKeyPriority:
    """jev 密钥顺序：环境变量 TYPESAFE_API_KEY → [jev] api_key（网页改的也写进 config.toml）→ key_file → ~/.typesafe_key。"""

    def _settings(self, tmp_path: Path, jev: dict | None = None) -> Any:
        from CharTyr_MaiWork.maiwork.config import load_settings

        raw: dict[str, Any] = {"storage": {"data_dir": str(tmp_path / "data")}}
        if jev is not None:
            raw["jev"] = jev
        settings, _ = load_settings(raw)
        return settings

    def test_config_api_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("TYPESAFE_KEY_FILE", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
        settings = self._settings(tmp_path, {"api_key": "jev-cfg-key-显眼"})
        assert _resolve_key(settings) == "jev-cfg-key-显眼"

    def test_env_beats_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量优先级最高（网页改的也写进 config.toml，没有中间层了）。"""
        monkeypatch.setenv("TYPESAFE_API_KEY", "jev-env-key-显眼")
        settings = self._settings(tmp_path, {"api_key": "jev-cfg-key"})
        assert _resolve_key(settings) == "jev-env-key-显眼"

    def test_key_file_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("TYPESAFE_KEY_FILE", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
        key_file = tmp_path / "mykey"
        key_file.write_text("jev-file-key-显眼\n", encoding="utf-8")
        settings = self._settings(tmp_path, {"key_file": str(key_file)})
        assert _resolve_key(settings) == "jev-file-key-显眼"

    @pytest.mark.asyncio
    async def test_web_jev_key_via_api(self, env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
        """网页设 jev 密钥 = 写进 config.toml；Jev 客户端立刻拿得到（文件/env 顺序照旧）。"""
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("TYPESAFE_KEY_FILE", raising=False)
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"jev.api_key": "jev-web-api-key-显眼"})
        assert r.status == 200
        data = await r.json()
        assert _field(data, "jev.api_key")["source"] == "file"
        assert 'api_key = "jev-web-api-key-显眼"' in env.config_text()
        assert env.app.jev._key() == "jev-web-api-key-显眼"
        assert "jev-web-api-key-显眼" in env.app._known_secrets()
        # 清空 → 文件里写 ""（这里 env 也没有 → 空）
        r = await env.client.put("/api/settings/config", json={"jev.api_key": None})
        assert r.status == 200
        assert env.app.jev._key() == ""
