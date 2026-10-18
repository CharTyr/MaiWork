"""environments/openssh_client.py：本机 OpenSSH 客户端的最小环境与启动名。

专用机器（ssh.py）和一次性 VM（railway.py）共用这一份，所以这里的行为
就是两边的行为——改这里等于同时改两处，测试必须把两个平台都钉住。
"""

from __future__ import annotations

from typing import Any

import pytest

from CharTyr_MaiWork.maiwork.environments import openssh_client as oc


# ---------------------------------------------------------------- 最小环境


def test_posix_env_keeps_caller_path_and_drops_process_secrets() -> None:
    env = oc.minimal_env(
        posix_path="/custom/bin:/usr/bin",
        platform="posix",
        environ={"PATH": "/untrusted", "TOKEN": "secret", "PROGRAMDATA": "/not-used"},
    )
    assert env == {"PATH": "/custom/bin:/usr/bin", "LANG": "C.UTF-8"}


def test_posix_env_defaults_to_standard_path() -> None:
    env = oc.minimal_env(platform="posix", environ={})
    assert env["PATH"] == oc.POSIX_MINIMAL_PATH


def test_windows_env_uses_only_trusted_system_paths() -> None:
    env = oc.minimal_env(
        platform="nt",
        environ={
            "SystemRoot": r"C:\Windows",
            "PROGRAMDATA": r"C:\ProgramData",
            "PATH": r"C:\Users\alice\bin",
            "TOKEN": "secret",
        },
    )
    assert env == {
        "PATH": r"C:\Windows\System32\OpenSSH;C:\Windows\System32",
        "LANG": "C.UTF-8",
        "PROGRAMDATA": r"C:\ProgramData",
    }


def test_windows_env_accepts_windir_fallback() -> None:
    env = oc.minimal_env(platform="nt", environ={"WINDIR": r"D:\Windows"})
    assert env["PATH"] == r"D:\Windows\System32\OpenSSH;D:\Windows\System32"
    assert "PROGRAMDATA" not in env


def test_windows_env_without_system_root_is_empty() -> None:
    env = oc.minimal_env(platform="nt", environ={})
    assert env["PATH"] == ""
    assert "PROGRAMDATA" not in env


def test_windows_env_without_system_root_is_flagged_broken() -> None:
    assert oc.windows_env_broken({"PATH": ""}, platform="nt") is True
    assert oc.windows_env_broken({"PATH": "  "}, platform="nt") is True


def test_posix_env_is_never_flagged_broken() -> None:
    """POSIX 上 PATH 空不空都跟 SystemRoot 无关，别误报系统环境异常。"""
    assert oc.windows_env_broken({"PATH": ""}, platform="posix") is False


def test_healthy_windows_env_is_not_flagged_broken() -> None:
    assert oc.windows_env_broken({"PATH": r"C:\Windows\System32"}, platform="nt") is False


# ---------------------------------------------------------------- 启动名


def test_posix_tool_stays_bare_even_when_which_finds_it() -> None:
    """POSIX 子进程按自己的 env PATH 找命令：保持裸名字，行为不变。"""
    assert oc.tool("ssh", path="/usr/bin", platform="posix", which=lambda *a, **k: "/usr/bin/ssh") == "ssh"


def test_windows_tool_uses_absolute_path() -> None:
    """Windows 上 CreateProcess 用父进程 PATH，必须给绝对路径。"""
    got = oc.tool("scp", path=r"C:\Windows\System32\OpenSSH", platform="nt",
                  which=lambda name, *, path=None: rf"C:\Windows\System32\OpenSSH\{name}.exe")
    assert got == r"C:\Windows\System32\OpenSSH\scp.exe"


def test_windows_tool_falls_back_to_bare_name_when_missing() -> None:
    """找不到就退回裸名字，不在这里抛——由调用方的「缺命令」检查先拦住。"""
    assert oc.tool("ssh", path="", platform="nt", which=lambda *a, **k: None) == "ssh"


def test_resolve_tools_reports_missing_as_empty_string() -> None:
    def which(name: str, *, path: Any = None) -> Any:
        return None if name == "scp" else f"/usr/bin/{name}"

    got = oc.resolve_tools("/usr/bin", which=which)
    assert got == {"ssh-keygen": "/usr/bin/ssh-keygen", "ssh": "/usr/bin/ssh", "scp": ""}


def test_resolve_tools_searches_the_given_path_only() -> None:
    """必须把调用方给的最小 PATH 传下去，不能落到插件进程的 PATH 上。"""
    seen: list[Any] = []

    def which(name: str, *, path: Any = None) -> str:
        seen.append(path)
        return f"/opt/{name}"

    oc.resolve_tools("/opt", which=which)
    assert seen == ["/opt", "/opt", "/opt"]


@pytest.mark.parametrize("name", oc.TOOL_NAMES)
def test_tool_names_cover_the_three_clients(name: str) -> None:
    assert name in ("ssh", "scp", "ssh-keygen")
