"""本机命令的 PYTHONPATH 注入（`environments/local.py` × `optional_deps.py` 的接线）。

为什么要这一层：可选依赖（办公文档三库）装在 MaiWork 自己的目录里，子 agent 的本机
命令**只拿到最小环境**（PATH / HOME / LANG，绝不继承插件进程的环境）。所以必须由
LocalEnv 在拼环境时把那个目录加进 `PYTHONPATH`，子 agent 的 `python3` 才 import 得到；
宿主（MaiBot 的 venv / 插件进程）不受影响。

- direct 模式真跑（echo 出来对）；
- systemd 模式只测拼出来的 argv（注入假 runner 录参数，不真起单元）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv


def _settings(root: Path, *, mode: str = "direct"):
    s, _ = load_settings({"environments": {"local_mode": mode, "workspace_root": str(root)}})
    return s


def _env(root: Path, *, mode: str = "direct", runner=None) -> LocalEnv:
    settings = _settings(root, mode=mode)
    return LocalEnv(lambda: settings, runner=runner)


def _recording_runner(exit_code: int = 0, out: str = "", err: str = ""):
    calls: list[dict] = []

    async def runner(argv, *, cwd, env, timeout):
        calls.append({"argv": list(argv), "cwd": str(cwd), "env": dict(env), "timeout": timeout})
        return exit_code, out, err

    return runner, calls


def _setenv(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in argv:
        if item.startswith("--setenv="):
            key, _, value = item[len("--setenv="):].partition("=")
            out[key] = value
    return out


@pytest.mark.skipif(os.name == "nt", reason="此测试真实启动 /bin/bash，仅适用于 POSIX")
@pytest.mark.asyncio
async def test_direct_command_gets_pythonpath(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    libs = tmp_path / "pylibs"
    libs.mkdir(parents=True)
    env = _env(root)
    r = await env.run("w1", 'echo "[$PYTHONPATH]"', timeout_s=10)
    assert r.exit_code == 0
    assert f"[{libs}]" in r.stdout


@pytest.mark.skipif(os.name == "nt", reason="此测试真实启动 /bin/bash，仅适用于 POSIX")
@pytest.mark.asyncio
async def test_direct_command_without_libs_has_no_pythonpath(tmp_path: Path) -> None:
    """还没装（目录不存在）时不要塞一个空 PYTHONPATH 进去。"""
    root = tmp_path / "ws"
    env = _env(root)
    r = await env.run("w1", 'echo "[$PYTHONPATH]"', timeout_s=10)
    assert r.stdout.strip() == "[]"


@pytest.mark.skipif(os.name == "nt", reason="此测试真实启动 /bin/bash，仅适用于 POSIX")
@pytest.mark.asyncio
async def test_direct_command_still_minimal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """加了 PYTHONPATH 也不许把宿主环境带进来。"""
    monkeypatch.setenv("MAIWORK_SECRET_TEST", "绝密值")
    monkeypatch.setenv("PYTHONPATH", "/tmp/宿主的路径")
    root = tmp_path / "ws"
    (tmp_path / "pylibs").mkdir(parents=True)
    env = _env(root)
    r = await env.run("w1", 'echo "[$MAIWORK_SECRET_TEST][$PYTHONPATH]"', timeout_s=10)
    assert r.stdout.strip() == f"[][{tmp_path / 'pylibs'}]"


@pytest.mark.asyncio
async def test_systemd_unit_gets_pythonpath_setenv(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    libs = tmp_path / "pylibs"
    libs.mkdir(parents=True)
    runner, calls = _recording_runner()
    env = _env(root, mode="systemd", runner=runner)
    await env.run("w1", "echo hi", timeout_s=10)
    got = _setenv(calls[0]["argv"])
    assert got["PYTHONPATH"] == str(libs)
    assert got["PATH"].startswith("/usr/local/bin:/usr/bin:/bin")


@pytest.mark.asyncio
async def test_systemd_unit_no_pythonpath_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    runner, calls = _recording_runner()
    env = _env(root, mode="systemd", runner=runner)
    await env.run("w1", "echo hi", timeout_s=10)
    assert "PYTHONPATH" not in _setenv(calls[0]["argv"])


def test_minimal_env_keys_are_stable(tmp_path: Path) -> None:
    """最小环境的键集合：没装库时就是老的三件套（老行为一字不变）。"""
    root = tmp_path / "ws"
    env = _env(root)
    assert env._minimal_env(root) == {
        "PATH": LocalEnv._MINIMAL_PATH,
        "HOME": str(root),
        "LANG": "C.UTF-8",
    }
