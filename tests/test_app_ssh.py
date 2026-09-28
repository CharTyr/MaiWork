"""专用 SSH 机器接进 app / 网页（2026-09-28）。

- app 启动就建 SshEnv（没配机器也建：先生成 key，网页上先把公钥给用户）；
- machine_* 工具注册给子 agent；Coordinator 拿到同一个 SshEnv；
- 后台循环每 30 分钟（或机器名单变了）连一遍，结果进网页「运行状态」的「专用机器」一项，
  这一项带上公钥（copy 字段），前端给个复制按钮。
"""

from __future__ import annotations

import asyncio
import socket
import types
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.console.views import _ssh_health


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeSshEnv:
    instances: list = []

    def __init__(self, get_settings, data_dir, **kw):
        self.get_settings = get_settings
        self.checks = 0
        FakeSshEnv.instances.append(self)

    def machines(self):
        return [{"name": m.name, "host": m.host, "note": m.note} for m in self.get_settings().environments.ssh]

    def available(self):
        return bool(self.machines())

    async def ensure_key(self):
        return "ssh-ed25519 AAAAFAKE maiwork"

    def public_key(self):
        return "ssh-ed25519 AAAAFAKE maiwork"

    async def check_all(self):
        self.checks += 1
        return []

    def status(self):
        return [{"name": "甲", "host": "u@1.2.3.4", "ok": True, "error": "", "ts": 1.0, "busy": False}]

    def box_for(self, tid):
        return None


@pytest.mark.asyncio
async def test_app_builds_ssh_registers_tools_and_passes_to_coordinator(tmp_path: Path) -> None:
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": "qq:900000001"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": "pw"},
        "storage": {"data_dir": str(tmp_path / "data")},
        "models": {"base_url": "http://127.0.0.1:9/v1", "api_key": "k", "main": "m", "worker": "w"},
        "environments": {"ssh": [{"name": "甲", "host": "u@1.2.3.4", "note": "4 核"}]},
    }
    app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    app.ssh_factory = FakeSshEnv
    await app.start()
    try:
        assert isinstance(app.ssh, FakeSshEnv)
        names = {s["function"]["name"] for s in app.tools.specs("worker")}
        assert {"machine_run", "machine_put_file", "machine_read_file", "machine_fetch_file"} <= names
        assert getattr(app.coordinator, "_ssh", None) is app.ssh
        await app.run_loop_once()
        await asyncio.sleep(0.05)
        assert app.ssh.checks >= 1
    finally:
        await app.stop()


def _svc(ssh, machines):
    env = types.SimpleNamespace(ssh=tuple(types.SimpleNamespace(name=n, host="u@h", note="") for n in machines))
    return types.SimpleNamespace(ssh=ssh, get_settings=lambda: types.SimpleNamespace(environments=env))


def test_health_no_machines_still_shows_pubkey() -> None:
    ssh = FakeSshEnv.__new__(FakeSshEnv)
    ssh.status = lambda: []
    h = _ssh_health(_svc(ssh, []))
    assert h["name"] == "专用机器" and h["state"] == "off"
    assert h["copy"].startswith("ssh-ed25519 ")
    assert "authorized_keys" in h["text"]


def test_health_ok_and_warn() -> None:
    ssh = FakeSshEnv.__new__(FakeSshEnv)
    ssh.status = lambda: [
        {"name": "甲", "ok": True, "error": "", "ts": 1.0, "busy": False},
        {"name": "乙", "ok": False, "error": "登录被拒：把公钥加进去", "ts": 1.0, "busy": False},
    ]
    h = _ssh_health(_svc(ssh, ["甲", "乙"]))
    assert h["state"] == "warn" and "乙" in h["text"] and "登录被拒" in h["text"]
    ssh.status = lambda: [{"name": "甲", "ok": True, "error": "", "ts": 1.0, "busy": True}]
    h = _ssh_health(_svc(ssh, ["甲"]))
    assert h["state"] == "ok" and "甲" in h["text"]


def test_health_none_when_no_ssh_env() -> None:
    assert _ssh_health(types.SimpleNamespace(ssh=None, get_settings=lambda: None)) is None


def test_health_icons_exist_in_icon_set() -> None:
    """运行状态各项的图标都得是 static/assets/icons 里真有的（不然前端回落成星星）。"""
    import re

    icons = {p.stem for p in (Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "assets" / "icons").glob("*.png")}
    src = (Path(__file__).resolve().parents[1] / "maiwork" / "console" / "views.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"key": "[a-z_]+", "icon": "([a-z]+)"', src))
    assert used and used <= icons, used - icons
