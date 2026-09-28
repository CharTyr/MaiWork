"""M2 / M12 回归测试。

M2：tools_exec 的 check_process / stop_process 也要校验 label（用 LocalEnv._check_label），
不然 systemd 模式下 systemctl show/stop 会拿到模型给的任意 unit 名。

M12：environments/local.py 里拼进 bash -lc 的 cwd、ws、log_path 都用 shlex.quote——
工作区子目录名可以带空格 / 特殊字符（resolve 不拦这些），不 quote 就是注入面。
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.tools import Tools, ToolContext
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

pytestmark = pytest.mark.asyncio


def _env(root: Path) -> LocalEnv:
    settings, _ = load_settings(
        {"environments": {"local_mode": "direct", "workspace_root": str(root)}}
    )
    return LocalEnv(lambda: settings)


class TestM2LabelValidation:
    def _tools_ctx(self, tmp_path: Path, env: LocalEnv):
        store = Store(tmp_path / "t.db")
        store.migrate()
        tools = Tools(store)
        settings, _ = load_settings(
            {"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "wsroot")}}
        )
        register_exec_tools(tools, env=env, host=None, get_settings=lambda: settings, session_of=lambda gid: "")
        ctx = ToolContext(group_id="1", task_id="T-1", actor="子 agent #1", workspace=tmp_path / "wsroot" / "ws1", role="worker")
        return tools, ctx

    async def test_check_process_bad_label_rejected(self, tmp_path: Path) -> None:
        """label 带斜线/空格 → 工具直接报错，不去查 systemctl。"""
        env = _env(tmp_path)
        tools, ctx = self._tools_ctx(tmp_path, env)
        result = await tools.call("check_process", {"label": "a/b"}, ctx)
        assert not result.ok
        assert "标签" in (result.error or "")
        result2 = await tools.call("check_process", {"label": "a b"}, ctx)
        assert not result2.ok

    async def test_stop_process_bad_label_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        tools, ctx = self._tools_ctx(tmp_path, env)
        result = await tools.call("stop_process", {"label": "../evil"}, ctx)
        assert not result.ok
        assert "标签" in (result.error or "")

    async def test_good_label_accepted(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        tools, ctx = self._tools_ctx(tmp_path, env)
        result = await tools.call("stop_process", {"label": "ok_name-1"}, ctx)
        assert result.ok  # 没在跑也不报错（幂等）


class TestM12ShellQuote:
    async def test_run_command_cwd_quoted(self, tmp_path: Path) -> None:
        """run(cwd=...) 拼进 bash -lc 的 cwd 用 shlex.quote；带空格的子目录不炸也不注入。"""
        env = _env(tmp_path)
        env.workspace("ws1")
        calls: list[dict] = []

        async def fake_runner(argv, *, cwd, env, timeout):
            calls.append({"argv": list(argv)})
            return 0, "ok", ""

        env._runner = fake_runner
        await env.write_file("ws1", "sub dir/hi.txt", "x")
        await env.run("ws1", "pwd && ls", cwd="sub dir", timeout_s=10)
        argv = calls[0]["argv"]
        bash_cmd = argv[argv.index("-lc") + 1] if "-lc" in argv else ""
        # bash -lc 收到的命令里 cwd 必须是 quote 过的（'sub dir'），不能裸奔
        assert "'sub dir'" in bash_cmd

    async def test_start_background_inner_quoted(self, tmp_path: Path) -> None:
        """start() 拼的 inner：ws 和 log_path 都 quote。

        workspace_root 走配置可以带空格（工作区名 / 标签是白名单但根目录不是），
        根目录带空格时 inner 里的 ws / log_path 必须是 quote 过的。"""
        root = tmp_path / "ws root"  # 带空格的根目录
        # systemd 模式走 _exec(argv)（可被假 runner 录下来），inner 就一个 bash -lc 参数
        settings, _ = load_settings(
            {"environments": {"local_mode": "systemd", "workspace_root": str(root)}}
        )
        env = LocalEnv(lambda: settings)
        calls: list[dict] = []

        async def fake_runner(argv, *, cwd, env, timeout):
            calls.append({"argv": list(argv)})
            return 0, "", ""

        env._runner = fake_runner
        await env.start("ws1", "echo hi", label="t1", timeout_s=10)
        argv = calls[0]["argv"]
        bash_cmd = argv[argv.index("-lc") + 1] if "-lc" in argv else ""
        ws = env.workspace("ws1")  # .../ws root/ws1
        log_path = ws / "runtime" / "logs" / "t1.log"
        assert shlex.quote(str(ws)) in bash_cmd
        assert shlex.quote(str(log_path)) in bash_cmd

    async def test_start_command_semicolon_in_ws_name_rejected_anyway(self, tmp_path: Path) -> None:
        """工作区名本身就白名单 [A-Za-z0-9_-]，分号进不来——quote 是兜底的第二道。"""
        env = _env(tmp_path)
        with pytest.raises(ValueError):
            await env.start("bad;name", "echo hi", label="t1", timeout_s=10)
