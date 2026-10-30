"""执行方式自动判定的 app/coordinator 接线（docs/02 执行方式自动判定）。

- 启动时判定一次并记一行中文日志；判定结果给 LocalEnv（argv 分 fixed/dynamic）；
- 「受限」时：run_command/start_process/check_process/stop_process 不注册进 worker 工具表
  （文件工具还在），工作区根换到数据目录下的 workspaces/；
- coordinator：本来要在本机跑的任务，本机不能用时不往本机派（落空也要明确告诉）；
- 网页健康项各档文案。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.environments import capability


def _dec(mode: str, ok: bool, hint: str = "") -> capability.Decision:
    return capability.Decision(
        mode=mode, ok=ok, exec_kind="isolated" if ok else "plugin",
        unit_user="maiwork" if mode == "fixed" else ("maiwork-sbx" if mode == "dynamic" else ""),
        reason=hint, hint=hint, log_line=f"本机干活：测试判定 {mode}",
    )


def _raw(data_dir: Path, ws_root: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": "qq:900000001", "workspace": "g1"}]},
        "console": {"listen": "127.0.0.1:0", "password": "x"},
        "models": {"base_url": "https://ep.test/v1", "api_key": "k", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "environments": {"workspace_root": str(ws_root)},
    }


def _make_app(tmp_path: Path, decision: capability.Decision) -> MaiWorkApp:
    """不走真 start（会起网页/后台），只验接线函数本身。

    起 app 前手动装好 settings（真 start 里 load_settings 之后才判定执行能力）。
    """
    from CharTyr_MaiWork.maiwork.config import load_settings

    app = MaiWorkApp(FakeCtx({}), _raw(tmp_path / "data", tmp_path / "ws"), plugin_dir=tmp_path)
    app.profiles_cls = FakeProfiles
    app._settings, _problems = load_settings(app._raw_config)
    return app


class TestCapabilityOnApp:
    def test_capability_attribute_and_log(self, tmp_path, caplog):
        app = _make_app(tmp_path, _dec("fixed", True))
        app.capability_probe = lambda run_as: _dec("fixed", True)
        import logging
        with caplog.at_level(logging.INFO, logger="maiwork.app"):
            dec = app._detect_local_capability()
        assert dec.mode == "fixed"
        assert getattr(app, "capability", None) is not None
        assert any("本机干活" in r.getMessage() for r in caplog.records)

    def test_workspace_root_stopped_moves_to_data_dir(self, tmp_path):
        app = _make_app(tmp_path, _dec("stopped", False, hint="h"))
        app.capability_probe = lambda run_as: _dec("stopped", False)
        dec = app._detect_local_capability()
        root = app._workspace_root_for(dec)
        assert root == (tmp_path / "data" / "workspaces")

    def test_workspace_root_fixed_respects_config(self, tmp_path):
        app = _make_app(tmp_path, _dec("fixed", True))
        root = app._workspace_root_for(_dec("fixed", True))
        assert root == tmp_path / "ws"

    def test_workspace_root_dynamic_private(self, tmp_path):
        app = _make_app(tmp_path, _dec("dynamic", True))
        root = app._workspace_root_for(_dec("dynamic", True))
        assert str(root) == "/var/lib/private/maiwork/workspaces"


class TestStoppedUnregistersCommandTools:
    def test_stopped_removes_command_tools(self, tmp_path):
        from CharTyr_MaiWork.maiwork.tools import Tools

        app = _make_app(tmp_path, _dec("stopped", False))
        app.capability = _dec("stopped", False)
        tools = Tools(None)
        # 放一个假的命令工具，模拟 register_exec_tools 注册过
        from CharTyr_MaiWork.maiwork.tools import Tool

        async def _h(ctx, args):
            from CharTyr_MaiWork.maiwork.tools import ToolResult
            return ToolResult(ok=True, output="x")

        for name in ("run_command", "start_process", "check_process", "stop_process", "read_file"):
            tools.register(Tool(name=name, description="t", parameters={"type": "object"},
                                roles=frozenset({"worker"}), handler=_h))
        app._drop_command_tools_if_stopped(tools)
        for name in ("run_command", "start_process", "check_process", "stop_process"):
            assert tools.get(name, "worker") is None, name
        assert tools.get("read_file", "worker") is not None

    def test_fixed_keeps_command_tools(self, tmp_path):
        from CharTyr_MaiWork.maiwork.tools import Tool, Tools

        app = _make_app(tmp_path, _dec("fixed", True))
        app.capability = _dec("fixed", True)
        tools = Tools(None)

        async def _h(ctx, args):
            from CharTyr_MaiWork.maiwork.tools import ToolResult
            return ToolResult(ok=True, output="x")

        tools.register(Tool(name="run_command", description="t", parameters={"type": "object"},
                            roles=frozenset({"worker"}), handler=_h))
        app._drop_command_tools_if_stopped(tools)
        assert tools.get("run_command", "worker") is not None


class TestCoordinatorStoppedFallback:
    def _coord(self, tmp_path, *, local_ok: bool, railway_ok: bool):
        """最小 coordinator 替身：只验 _setup_exec_env 的选择。"""
        from CharTyr_MaiWork.maiwork.coordinator import Coordinator

        class _Cap:
            pass

        cap = _dec("stopped", False) if not local_ok else _dec("fixed", True)
        c = Coordinator.__new__(Coordinator)
        c._railway = types.SimpleNamespace() if railway_ok else None
        c._get_settings = lambda: type("S", (), {"environments": type("E", (), {
            "railway": True, "run_as": "maiwork", "memory_max": "512M"})()})()
        c._capability = cap
        notes: list[str] = []
        envs: list[str] = []

        class _Tasks:
            def set_env(self, tid, desc, note=""):
                envs.append(desc)
                if note:
                    notes.append(note)

        c._tasks = _Tasks()
        return c, notes, envs

    @pytest.mark.asyncio
    async def test_stopped_local_railway_falls_back(self, tmp_path):
        c, notes, envs = self._coord(tmp_path, local_ok=False, railway_ok=True)
        c._railway_available = lambda: True

        class _Box:
            expires_ts = 0

        async def _acquire(tid):
            return _Box()

        c._railway.acquire = _acquire
        c._fetch_acquire_reason = lambda: __import__("asyncio").sleep(0, result="r")
        on_rail, box = await c._setup_exec_env("t1", {"env": "local"}, "900000001")
        assert on_rail is True and box is not None
        assert notes and "不能隔离跑命令" in notes[0]

    @pytest.mark.asyncio
    async def test_stopped_local_without_railway_returns_no_exec(self, tmp_path):
        c, notes, envs = self._coord(tmp_path, local_ok=False, railway_ok=False)
        c._railway_available = lambda: False
        c._fetch_acquire_reason = lambda: __import__("asyncio").sleep(0, result="r")
        on_rail, box = await c._setup_exec_env("t1", {"env": "local"}, "900000001")
        assert on_rail is False and box is None
        assert notes and "不能隔离跑命令" in notes[0]
        assert envs and "受限" in envs[0]

    @pytest.mark.asyncio
    async def test_fixed_local_unchanged(self, tmp_path):
        c, notes, envs = self._coord(tmp_path, local_ok=True, railway_ok=False)
        on_rail, box = await c._setup_exec_env("t1", {"env": "local"}, "900000001")
        assert on_rail is False and box is None
        assert not notes
        assert envs and "本机" in envs[0]


class TestLocalenvHealthCopy:
    """网页「本机」健康项各档文案（大白话，给装插件的人看）。"""

    def _health(self, tmp_path, decision):
        from CharTyr_MaiWork.maiwork.console.views import _localenv_health

        app = _make_app(tmp_path, decision)
        app.capability = decision
        return _localenv_health(app)

    def test_fixed_copy(self, tmp_path):
        h = self._health(tmp_path, _dec("fixed", True))
        assert h["name"] == "本机"
        assert h["state"] == "ok"
        assert h["text"] == "隔离运行（固定账号 maiwork）"

    def test_dynamic_copy(self, tmp_path):
        h = self._health(tmp_path, _dec("dynamic", True))
        assert h["name"] == "本机"
        assert h["state"] == "ok"
        assert h["text"] == "隔离运行（自动分配临时账号，不用建用户）"

    def test_stopped_copy_says_why(self, tmp_path):
        dec = capability.Decision(
            mode="stopped", ok=False, exec_kind="plugin", unit_user="",
            reason="这台机器不是 Linux（Windows / macOS）", hint="换 Railway",
            log_line="本机干活：不能用——这台机器不是 Linux",
        )
        h = self._health(tmp_path, dec)
        assert h["name"] == "本机"
        assert h["state"] == "off"
        assert "没开" in h["text"] and "这台机器不是 Linux" in h["text"]
        assert "临时机器" in h["text"]
