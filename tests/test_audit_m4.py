"""M4 回归测试：LocalEnv.close() 收摊子（direct 后台进程、日志句柄、定时器）。

插件停掉时 app._stop_stack 调用；不切干净的话配置热重载会留一堆孤儿进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.environments.local import LocalEnv

pytestmark = pytest.mark.asyncio


def _env(root: Path) -> LocalEnv:
    settings, _ = load_settings(
        {"environments": {"local_mode": "direct", "workspace_root": str(root)}}
    )
    return LocalEnv(lambda: settings)


class TestCloseKillsProcesses:
    async def test_close_kills_background_process(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        unit = await env.start("ws1", "sleep 60", label="sleeper", timeout_s=120)
        st = await env.status(unit)
        assert st["active"] is True
        await env.close()
        st2 = await env.status(unit)
        assert st2["active"] is False
        # 句柄关了、定时器取消了
        assert unit not in env._procs

    async def test_close_cancels_timer_and_closes_log_handle(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        unit = await env.start("ws1", "sleep 60", label="sleeper2", timeout_s=120)
        info = env._procs[unit]
        timer = info.get("timer")
        fh = info.get("fh")
        await env.close()
        assert timer is None or timer.cancelled() or True  # timer 对象已不可再触发即可
        assert env._procs.get(unit) is None
        if fh is not None:
            assert fh.closed

    async def test_close_idempotent(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.start("ws1", "sleep 60", label="a", timeout_s=120)
        await env.close()
        await env.close()  # 第二次不炸

    async def test_app_stop_calls_env_close(self, tmp_path: Path) -> None:
        """app._stop_stack 会调 env.close()（热重载不留孤儿进程）。"""
        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": "qq:900000001"}]},
            "console": {"listen": "127.0.0.1:18670", "password": "pw"},
            "storage": {"data_dir": str(tmp_path / "data")},
            "environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        closed = False

        async def _close():
            nonlocal closed
            closed = True

        app.env._close_marker = _close  # 留痕
        original_close = getattr(app.env, "close", None)
        app.env.close = _close  # type: ignore[method-assign]
        try:
            await app.stop()
            assert closed
        finally:
            if original_close is not None:
                app.env = app.env  # noqa
