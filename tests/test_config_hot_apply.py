"""写后立即生效 + 宿主重复发 on_config_update 幂等 + group_space 热开关。

- PUT /api/settings/config 写完文件，本进程立刻应用（不等宿主文件监控）；
- 宿主随后再发一次 on_config_update（同一份内容）→ update_config 幂等跳过，
  不重复刷日志刷状态、不重启控制台；
- group_space.enabled 开→关 / 关→开 热生效（组件摘掉/现建现探测）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp

PASSWORD = "热更新密码-显眼"
G1 = "900000001"


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "storage": {"data_dir": str(data_dir)},
        "group_space": {"enabled": False},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


def _write_plugin_dir(plug_dir: Path, raw: dict) -> None:
    import tomlkit

    plug_dir.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
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
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield type("E", (), {"app": app, "client": client, "plug_dir": plug_dir})()
    await client.close()
    await app.stop()


async def _login(env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


class TestApplyNow:
    @pytest.mark.asyncio
    async def test_put_applies_immediately(self, env) -> None:
        """PUT 返回时 get_settings() 已是新值（不等宿主文件监控）。

        用仍归全局的 `topics.min_gap_hours`：`topics.per_day` / `delivery.quiet_hours`
        这类 0.8.0 起每群一份，网页写它们会被明确 400（见 test_group_controls_migration）。
        """
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 9})
        assert r.status == 200
        assert env.app.get_settings().topics.min_gap_hours == 9

    @pytest.mark.asyncio
    async def test_host_duplicate_update_is_idempotent(self, env, caplog) -> None:
        """宿主文件监控随后发一次同内容的 on_config_update：幂等跳过（不再刷日志、
        不重启控制台、不重新探测群空间）。"""
        await _login(env)
        r = await env.client.put("/api/settings/config", json={"topics.min_gap_hours": 9})
        assert r.status == 200
        assert env.app.get_settings().topics.min_gap_hours == 9
        listen_before = env.app._listen
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="maiwork.app"):
            # 宿主发的同一份内容（整份规范化后的配置）
            await env.app.update_config(dict(env.app._raw_config))
        assert "配置已更新" not in "\n".join(r.getMessage() for r in caplog.records)
        assert env.app._listen == listen_before

    @pytest.mark.asyncio
    async def test_different_content_still_applies(self, env) -> None:
        """内容真变了（宿主那边手改了文件）→ 照常更新。"""
        await _login(env)
        raw = dict(env.app._raw_config)
        raw["topics"] = {"min_gap_hours": 4}
        await env.app.update_config(raw)
        assert env.app.get_settings().topics.min_gap_hours == 4


class TestGroupSpaceHotToggle:
    @pytest.mark.asyncio
    async def test_enable_disable_hot(self, env) -> None:
        """group_space.enabled 关→开：现建现探测；开→关：摘掉组件。"""
        await _login(env)
        assert env.app.group_space is None  # fixture 里 enabled=false
        r = await env.client.put("/api/settings/config", json={"group_space.enabled": True})
        assert r.status == 200
        assert env.app.group_space is not None
        r = await env.client.put("/api/settings/config", json={"group_space.enabled": False})
        assert r.status == 200
        assert env.app.group_space is None

    @pytest.mark.asyncio
    async def test_console_listen_hot(self, env) -> None:
        """console.listen 改了：控制台换地址重启（_listen 跟着新值）。

        测试环境 listen 是 127.0.0.1:0（随机端口）；改成一个具体的空闲端口，
        _listen 跟着新值，控制台对象没留在半挂状态。
        """
        import socket as _s

        with _s.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        await _login(env)
        old_listen = env.app._listen
        r = await env.client.put("/api/settings/config", json={"console.listen": f"127.0.0.1:{free_port}"})
        assert r.status == 200
        assert env.app._listen == ("127.0.0.1", free_port)
        assert env.app._listen != old_listen
        assert env.app.console is not None
