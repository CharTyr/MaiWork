"""Tool 特性声明（needs_workspace / exec_class / task_only）+ Tools.traits。

设计（.trellis/tasks/10-30-admin-tool-awareness/design.md §1）：
- 工具自己声明特性，老注册点不传 = 全 False；
- 调研岗「能带什么工具、要不要单独工作区、要不要确认」全部由这三个位算；
- 未注册的名字 → traits() 返回 None；MCP 工具三个都 False。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.tools_imagegen import register_image_tools
from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools
from CharTyr_MaiWork.maiwork.tools_ssh import register_machine_tools


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _settings(root: Path):
    s, _ = load_settings({"environments": {"local_mode": "direct", "workspace_root": str(root)}})
    return s


def _env(tmp_path: Path):
    from CharTyr_MaiWork.maiwork.environments.local import LocalEnv

    settings = _settings(tmp_path / "ws")
    return LocalEnv(lambda: settings)


def _tools_with_exec(store, tmp_path) -> Tools:
    """注册 exec + imagegen + vm + machine 全家桶的 Tools。"""
    tools = Tools(store)
    env = _env(tmp_path)
    settings = _settings(tmp_path / "ws")

    class _Host:
        async def messages(self, *a, **kw):
            return []

        async def knowledge(self, *a, **kw):
            return ""

    register_exec_tools(tools, env=env, host=_Host(), get_settings=lambda: settings)
    register_image_tools(tools, get_settings=lambda: settings, env=env, store=store)

    class _FakeVmEnv:
        max_run_timeout_s = 600

        async def run(self, *a, **kw):
            raise RuntimeError("不该真跑")

        async def put(self, *a, **kw):
            raise RuntimeError("不该真跑")

        async def get(self, *a, **kw):
            raise RuntimeError("不该真跑")

    register_vm_tools(tools, get_box=lambda: None, env=_FakeVmEnv(), local_env=env)
    register_machine_tools(tools, get_box=lambda tid: None, env=_FakeVmEnv(), local_env=env)
    return tools


def _traits(tools: Tools, name: str) -> dict:
    t = tools.traits(name)
    assert t is not None, f"{name} 应该已注册"
    return t


class TestBuiltinToolTraits:
    """逐个断言内置工具的注册特性（事实表，写死防回归）。"""

    @pytest.mark.parametrize("name", ["read_file", "write_file", "list_files",
                                      "run_command", "start_process", "check_process", "stop_process"])
    def test_needs_workspace_tools(self, store, tmp_path, name):
        tools = _tools_with_exec(store, tmp_path)
        assert _traits(tools, name)["needs_workspace"] is True

    @pytest.mark.parametrize("name", ["run_command", "start_process", "stop_process",
                                      "write_file", "render_report", "generate_image",
                                      "vm_run", "vm_put_file", "vm_read_file", "vm_fetch_file",
                                      "machine_run", "machine_put_file", "machine_read_file",
                                      "machine_fetch_file"])
    def test_exec_class_tools(self, store, tmp_path, name):
        tools = _tools_with_exec(store, tmp_path)
        assert _traits(tools, name)["exec_class"] is True

    @pytest.mark.parametrize("name", ["read_file", "list_files", "check_process"])
    def test_non_exec_class_tools(self, store, tmp_path, name):
        """只读/文件查看类不算执行类（带上不需要确认门）。"""
        tools = _tools_with_exec(store, tmp_path)
        assert _traits(tools, name)["exec_class"] is False

    @pytest.mark.parametrize("name", ["render_report", "generate_image",
                                      "vm_run", "vm_put_file", "vm_read_file", "vm_fetch_file",
                                      "machine_run", "machine_put_file", "machine_read_file",
                                      "machine_fetch_file"])
    def test_task_only_tools(self, store, tmp_path, name):
        tools = _tools_with_exec(store, tmp_path)
        assert _traits(tools, name)["task_only"] is True

    @pytest.mark.parametrize("name", ["read_file", "write_file", "list_files",
                                      "run_command", "start_process", "check_process", "stop_process"])
    def test_regular_tools_not_task_only(self, store, tmp_path, name):
        tools = _tools_with_exec(store, tmp_path)
        assert _traits(tools, name)["task_only"] is False


class TestTraitsDefaults:
    def test_unregistered_returns_none(self, store):
        tools = Tools(store)
        assert tools.traits("mcp_flovia_flovia_x") is None
        assert tools.traits("Bash(git:*)") is None

    def test_old_style_tool_defaults_all_false(self, store):
        """老注册点不传三个字段 → 全 False（MCP 工具等老调用方行为不变）。"""
        tools = Tools(store)

        async def handler(ctx, args):
            return ToolResult(ok=True, output="x")

        tools.register(Tool(
            name="mcp_flovia_flovia_x", description="mcp",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=handler,
        ))
        assert tools.traits("mcp_flovia_flovia_x") == {
            "needs_workspace": False, "exec_class": False, "task_only": False,
            "roles": ["worker"],
        }

    def test_main_only_tool_reports_roles(self, store):
        """main-only 工具（群空间等）traits 带出 roles——调用方据此判「注册给子 agent 吗」。"""
        tools = Tools(store)

        async def handler(ctx, args):
            return ToolResult(ok=True, output="x")

        tools.register(Tool(
            name="group_notice_send", description="g",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"main"}), handler=handler,
        ))
        tr = tools.traits("group_notice_send")
        assert tr is not None and tr["roles"] == ["main"]
