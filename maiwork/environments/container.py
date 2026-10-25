"""Docker / 容器环境探测（2026-10-25）：首次引导和「运行状态」用来提示 Docker 用户。

只读几个文件，不调用任何命令；所有路径/读取函数都可注入，测试不依赖真机。
- in_docker()：/.dockerenv 存在；或 /proc/1/cgroup 含 docker / containerd / kubepods；
  或 /run/.containerenv 存在（podman）。
- data_dir_persisted(data_dir)：读 /proc/self/mountinfo，找覆盖 data_dir 的最长挂载点：
  挂载点是 "/" 且文件系统是 overlay（容器可写层）→ False（重建容器会丢）；
  其它（bind mount / volume）→ True；读不到 / 解析不了 → None（不判断）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

_CGROUP_MARKS = ("docker", "containerd", "kubepods")


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def in_docker(*, exists: Callable[[str], bool] = os.path.exists,
              read: Callable[[str], str | None] = _read) -> bool:
    try:
        if exists("/.dockerenv") or exists("/run/.containerenv"):
            return True
    except Exception:
        pass
    text = read("/proc/1/cgroup") or ""
    return any(m in text for m in _CGROUP_MARKS)


def _unescape(s: str) -> str:
    # mountinfo 把空格等写成 \040 这样的八进制
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """→ [(挂载点, 文件系统类型)]。格式：id pid maj:min root mountpoint opts ... - fstype src opts。"""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 7 or "-" not in parts[6:]:
            continue
        sep = parts.index("-", 6)
        if sep + 1 >= len(parts):
            continue
        rows.append((_unescape(parts[4]), parts[sep + 1]))
    return rows


def _covers(mount: str, path: str) -> bool:
    if mount == "/":
        return path.startswith("/")
    return path == mount or path.startswith(mount.rstrip("/") + "/")


def data_dir_persisted(data_dir: str | os.PathLike, *,
                       read: Callable[[str], str | None] = _read) -> bool | None:
    text = read("/proc/self/mountinfo")
    if not text:
        return None
    try:
        path = os.path.abspath(str(data_dir))
        rows = [r for r in parse_mountinfo(text) if _covers(r[0], path)]
    except Exception:
        return None
    if not rows:
        return None
    # 同一挂载点出现多次时后挂的覆盖前面的，所以长度相同取最后一条
    best = max(enumerate(rows), key=lambda e: (len(e[1][0]), e[0]))[1]
    mount, fstype = best
    if mount == "/" and fstype == "overlay":
        return False
    return True
