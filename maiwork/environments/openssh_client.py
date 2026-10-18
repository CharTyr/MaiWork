"""本机 OpenSSH 客户端（ssh / scp / ssh-keygen）的最小环境与启动名。

专用 SSH 机器（`ssh.py`）和一次性 VM（`railway.py`）都要在本机起这三个命令，
而且规矩一样：**只给最小环境，绝不继承插件进程的环境变量**（防止密钥漏给子进程）。
平台差异集中在这里，两个调用方不再各写一份（写两份迟早会漂）。

两个平台事实决定了这里的写法：

1. **Windows 上必须给 `PROGRAMDATA`**。Win32-OpenSSH 从 `%PROGRAMDATA%\\ssh\\`
   读系统级 `ssh_config`，环境里没有这个变量时它不报错、直接退出 255。
2. **Windows 上必须用绝对路径启动**。`CreateProcess` 找可执行文件用的是**父进程**
   的 `PATH`，传给子进程的 `env["PATH"]` 它不认；传裸名字会去父进程 PATH、甚至
   当前目录里找（当前目录常常正是放私钥的地方）。POSIX 没这个问题——子进程按
   自己 `env` 里的 `PATH` 找，所以那边保持裸名字，行为和以前一字不差。
"""

from __future__ import annotations

import ntpath
import os
import shutil
from typing import Any, Mapping, Sequence

__all__ = [
    "IS_WINDOWS",
    "TOOL_NAMES",
    "minimal_env",
    "windows_env_broken",
    "resolve_tools",
    "tool",
]

IS_WINDOWS = os.name == "nt"

#: 要在本机起的三个客户端命令。
TOOL_NAMES = ("ssh-keygen", "ssh", "scp")

#: POSIX 上给子进程的 PATH（系统目录，写死；不用插件进程的 PATH）。
POSIX_MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


def minimal_env(
    *,
    posix_path: str = POSIX_MINIMAL_PATH,
    platform: str | None = None,
    environ: Any = None,
) -> dict[str, str]:
    """ssh 子进程的最小环境（PATH / LANG，Windows 另加 PROGRAMDATA）。

    `posix_path` 由调用方给（各模块原本的常量不同，保持各自行为不变）；
    `platform` / `environ` 只为测试注入，生产一律用真实值。
    """
    platform = os.name if platform is None else platform
    source = os.environ if environ is None else environ
    if platform != "nt":
        return {"PATH": posix_path, "LANG": "C.UTF-8"}

    system_root = str(source.get("SystemRoot") or source.get("WINDIR") or "").strip()
    path = ""
    if system_root:
        system32 = ntpath.join(system_root, "System32")
        path = ";".join((ntpath.join(system32, "OpenSSH"), system32))
    env = {"PATH": path, "LANG": "C.UTF-8"}
    program_data = str(source.get("PROGRAMDATA") or "").strip()
    if program_data:
        env["PROGRAMDATA"] = program_data
    return env


def windows_env_broken(env: Mapping[str, str], *, platform: str | None = None) -> bool:
    """Windows 上 PATH 是空的 = 读不到 SystemRoot/WINDIR，连系统目录都定位不了。

    这时说「缺 ssh 命令」是假话（用户装了也修不好），要单独说系统环境异常。
    """
    platform = os.name if platform is None else platform
    return platform == "nt" and not str(env.get("PATH") or "").strip()


def resolve_tools(
    path: str,
    *,
    names: Sequence[str] = TOOL_NAMES,
    which: Any = None,
) -> dict[str, str]:
    """按给定 PATH 找每个命令的绝对路径；找不到的值是空串。

    `which` 只为测试注入，生产用 `shutil.which`。
    """
    finder = shutil.which if which is None else which
    return {name: (finder(name, path=path) or "") for name in names}


def tool(name: str, *, path: str, platform: str | None = None, which: Any = None) -> str:
    """启动某个客户端命令时用的名字。

    - POSIX：裸名字（子进程按自己 env 的 PATH 找，行为一直如此）；
    - Windows：`which` 找到的绝对路径；找不到就退回裸名字，由调用方的
      「缺命令」检查先拦住（这里不抛，免得初始化路径上多一种失败形态）。
    """
    platform = os.name if platform is None else platform
    if platform != "nt":
        return name
    return resolve_tools(path, names=(name,), which=which).get(name) or name
