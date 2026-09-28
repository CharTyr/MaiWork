"""本机执行能力自动判定（不加新配置项；docs/02 执行方式自动判定）。

判定结果三种：
- "fixed"：Linux + 有 systemd-run + root + 系统里存在 run_as 用户（默认 maiwork）
  → 固定用户方式（systemd-run --uid=<run_as>，行为和老版本完全一样）；
- "dynamic"：没有这个用户但 Linux + 有 systemd-run + root
  → DynamicUser 方式：固定单元用户名 maiwork-sbx + StateDirectory=maiwork/workspaces/<名>，
  工作区实际在 /var/lib/private/maiwork/workspaces/<名>；
- "stopped"：其它情况（Windows / macOS / 非 root Linux / 容器里没 systemd）
  → 本机不能隔离跑命令；工作区由插件进程直接读写（放数据目录下），
  要跑命令的活只能走 railway 一次性机器。

判定只在启动时跑一次（app 启动算一次、记一行中文日志；配置改了重判）。
探测本身绝不真跑 systemd-run / useradd（单元测试用隔离罩守着）。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("maiwork.environments.capability")

__all__ = ["LocalCaps", "Decision", "detect", "resolve_workspace_root", "DYNAMIC_USER"]

DYNAMIC_USER = "maiwork-sbx"
# DynamicUser + User=<固定名> 时工作区由 StateDirectory 提供，实际落在 /var/lib/private 下
_DYNAMIC_STATE_ROOT = Path("/var/lib/private/maiwork/workspaces")


@dataclass(frozen=True)
class LocalCaps:
    """本机探测事实（只读）。"""

    is_linux: bool
    is_root: bool
    has_systemd_run: bool
    run_as_exists: bool


@dataclass(frozen=True)
class Decision:
    """判定结果。

    - mode：fixed / dynamic / stopped；
    - ok：本机能不能隔离跑命令；
    - exec_kind：isolated（systemd 隔离跑）| plugin（只能插件进程直接读写工作区，不跑命令）；
    - unit_user：systemd 单元里用什么用户（fixed=run_as；dynamic=DYNAMIC_USER；stopped=""）；
    - log_line：启动时记的一行中文日志；
    - hint：网页健康项给用户看的大白话修复建议（ok 时空串）。
    """

    mode: str
    ok: bool
    exec_kind: str
    unit_user: str
    reason: str
    hint: str
    log_line: str


def _probe(run_as: str = "maiwork") -> LocalCaps:
    """探测本机事实。绝不真跑 systemd / useradd；Windows 没有 pwd 也能安全跑。"""
    is_linux = sys.platform.startswith("linux")
    try:
        is_root = bool(os.geteuid() == 0)  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # Windows 没有 geteuid
        is_root = False
    has_systemd_run = False
    if is_linux:
        import shutil

        has_systemd_run = bool(shutil.which("systemd-run"))
    run_as_exists = False
    if is_linux:
        try:
            import pwd  # Windows 没有这个模块

            try:
                pwd.getpwnam(str(run_as or "maiwork"))
                run_as_exists = True
            except KeyError:
                run_as_exists = False
        except ImportError:
            run_as_exists = False
    return LocalCaps(
        is_linux=is_linux, is_root=is_root, has_systemd_run=has_systemd_run, run_as_exists=run_as_exists
    )


def probe(run_as: str = "maiwork") -> Decision:
    """一步到位：探测 + 判定（app 启动时调一次，记 log_line）。"""
    return detect(_probe(run_as), run_as=run_as)


def detect(caps: LocalCaps, run_as: str = "maiwork") -> Decision:
    """把探测事实翻成判定。run_as 只是文案/单元名（caps 里的 run_as_exists 已按它查过）。"""
    run_as = str(run_as or "maiwork")
    if not caps.is_linux:
        return Decision(
            mode="stopped", ok=False, exec_kind="plugin", unit_user="",
            reason="这台机器不是 Linux（Windows / macOS）",
            hint="本机只能做不跑命令的活；要跑命令的活改用「一次性机器」（设置 → 执行环境开 Railway）",
            log_line="本机干活：不能用——这台机器不是 Linux，不能隔离跑命令；工作区放在插件数据目录下，由插件直接读写",
        )
    if not caps.has_systemd_run:
        return Decision(
            mode="stopped", ok=False, exec_kind="plugin", unit_user="",
            reason="系统里没有 systemd-run（常见于 Docker 容器）",
            hint="用 systemd 的 Linux 并以 root 跑 MaiBot 才能隔离；Docker 部署请改用「一次性机器」（设置 → 执行环境开 Railway）",
            log_line="本机干活：不能用——系统里没有 systemd-run（常见于 Docker 容器）；工作区放在插件数据目录下，由插件直接读写",
        )
    if not caps.is_root:
        return Decision(
            mode="stopped", ok=False, exec_kind="plugin", unit_user="",
            reason="MaiBot 不是以 root 运行",
            hint="以 root 跑 MaiBot 才能用 systemd 隔离子 agent；或改用「一次性机器」（设置 → 执行环境开 Railway）",
            log_line="本机干活：不能用——MaiBot 不是以 root 运行，起不了隔离单元；工作区放在插件数据目录下，由插件直接读写",
        )
    if caps.run_as_exists:
        return Decision(
            mode="fixed", ok=True, exec_kind="isolated", unit_user=run_as,
            reason="", hint="",
            log_line=f"本机干活：能用——固定用户 {run_as} + systemd 隔离（工作区按配置）",
        )
    return Decision(
        mode="dynamic", ok=True, exec_kind="isolated", unit_user=DYNAMIC_USER,
        reason="", hint="",
        log_line=(
            f"本机干活：能用——系统里没有 {run_as} 用户，改用 systemd 自动分配的一次性系统用户"
            f"（固定名字 {DYNAMIC_USER}，工作区在 /var/lib/private/maiwork/workspaces 下）"
        ),
    )


def resolve_workspace_root(config_root: Path, dec: Decision, data_dir: Path | None = None) -> Path:
    """工作区根按判定结果落地：

    - fixed：完全尊重配置（线上就是 /home/maiwork/workspaces，不动）；
    - dynamic：强制 /var/lib/private/maiwork/workspaces（systemd 把 StateDirectory 挂在这；
      配置里写的目录在 DynamicUser 下没有用——单元运行时挂载点只认 StateDirectory）；
    - stopped：插件数据目录下的 workspaces/（插件进程直接读写）。
    """
    if dec.mode == "fixed":
        return Path(config_root)
    if dec.mode == "dynamic":
        return _DYNAMIC_STATE_ROOT
    root = Path(data_dir) if data_dir is not None else Path(config_root)
    return root / "workspaces"
