"""没有 POSIX 目录句柄（dir_fd）的平台上的工作区安全读写（主要是原生 Windows）。

Linux / macOS 走 `local.py` 里原有的「可信根目录 fd + 逐段 dir_fd + O_NOFOLLOW」防线，本模块
一行都不参与；只有 `has_dir_fd()` 为 False（原生 Windows，或单测拿掉目录句柄能力模拟）时，
`LocalEnv` 与 `compaction.secure_write_text` 才改走这里。

为什么这里可以用「逐级 lstat 检查 + 原子替换」代替目录句柄：
- 原生 Windows 的本机执行能力恒为 stopped（`capability.py`）：本机不注册 run_command /
  start_process，没有子 agent 进程能在「检查」和「使用」之间并发把目录换成链接；
- 工作区里的东西只经 MaiWork 自己的读写工具落盘（同样逐段拒链接），远端拷回的成品也只落在
  已校验过的目录里。

规则（docs/07-代码接口.md §11.1）：
- 每段名字：拒空段 / `.` / `..` / 路径分隔符 / 冒号（盘符相对路径、NTFS 备用数据流）/
  Windows 非法字符 / 控制字符 / 段尾点或空格 / Windows 保留设备名（CON、PRN、AUX、NUL、
  CONIN$、CONOUT$、COM0~9、LPT0~9，不分大小写，带扩展名也算，如 `NUL.txt`）；
- 从可信锚点开始逐段 `os.lstat`：符号链接（`S_ISLNK`）或 Windows 重解析点
  （`st_reparse_tag != 0`，包括目录联接 junction）一律 PermissionError，绝不跟随；
- 普通文件 `st_nlink > 1`（硬链接）拒；
- 写：同一已校验目录下 `O_CREAT|O_EXCL` 建独占临时文件 → 写完 → 复核父目录链与目标 lstat
  （没变成链接 / 重解析点 / 硬链接、旧文件身份没被调包）→ `os.replace` 原子落位；
- 新建（归档）：`O_CREAT|O_EXCL` 独占创建，已存在就 FileExistsError，绝不覆盖。
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "has_dir_fd",
    "check_part",
    "split_rel",
    "is_link_like",
    "ensure_dirs",
    "lstat_regular",
    "open_read_fd",
    "create_exclusive",
    "replace_atomic",
    "create_new_verified",
]


# 在测试包装 os.open 之前记录真实平台能力；旗标和能力集合仍在调用时检查。
_OPEN_HAS_DIR_FD = os.open in os.supports_dir_fd


def has_dir_fd() -> bool:
    """平台是否具备 local.py 那套目录句柄防线（Linux / macOS 为 True，原生 Windows 为 False）。

    导入时记录真实 `os.open` 是否支持 dir_fd；每次调用仍检查 `O_NOFOLLOW` / `O_DIRECTORY`
    旗标和 `os.supports_dir_fd`。不在调用时按包装函数查集合成员，避免录参数的测试包装函数
    将 Linux / macOS 误判成 Windows。
    单测删掉这两个旗标、清空 `supports_dir_fd` 就能如实模拟原生 Windows（conftest
    `simulate_no_dir_fd`）。
    """
    return bool(
        getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and _OPEN_HAS_DIR_FD
        and os.supports_dir_fd
    )


def _open_extra() -> int:
    """打开文件时附加的旗标：二进制、不让子进程继承；平台有 O_NOFOLLOW 就顺手再挡一道。"""
    return (
        getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )

_BAD_CHARS = frozenset('/\\:<>"|?*')
_RESERVED_RE = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])$", re.IGNORECASE)


def _is_reserved(part: str) -> bool:
    stem = part.split(".", 1)[0].rstrip(" ")
    return bool(_RESERVED_RE.match(stem))


def check_part(part: Any) -> str:
    """单个路径段合法性；不合法 → PermissionError（中文）。返回原样的段。"""
    text = str(part if part is not None else "")
    if text == "..":
        raise PermissionError("路径越界（不允许 ..）")
    if (
        not text
        or text == "."
        or any(ch in _BAD_CHARS for ch in text)
        or any(ord(ch) < 32 for ch in text)
        or text[-1] in ". "
        or _is_reserved(text)
    ):
        raise PermissionError(f"路径段不安全：{text!r}")
    return text


def split_rel(rel: Any) -> list[str]:
    """工作区相对路径 → 段列表（`/` 与 `\\` 都算分隔符，空段和 `.` 忽略）。

    绝对路径 / 盘符路径 → PermissionError；每段再过 `check_part`（`..`、冒号、保留名都在这拒）。
    """
    raw = str(rel or "")
    if raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":") or Path(raw).is_absolute():
        raise PermissionError(f"路径必须是工作区内的相对路径：{raw!r}")
    parts = [p for p in re.split(r"[\\/]+", raw) if p not in ("", ".")]
    for p in parts:
        if p == "..":
            raise PermissionError(f"路径越界（不允许 ..）：{raw!r}")
        check_part(p)
    return parts


def is_link_like(st: os.stat_result) -> bool:
    """符号链接或 Windows 名字代理重解析点（junction 等）。"""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0))


def _check_dir(path: Path, label: str) -> None:
    st = os.lstat(path)
    if is_link_like(st) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"目录不是普通目录或包含符号链接：{label}")


def ensure_dirs(anchor: Path, parts: Iterable[str], *, create: bool = False, mode: int = 0o777) -> Path:
    """从可信锚点逐段 lstat 走下去（缺的段 create=True 时现建），返回最后一级目录。

    锚点本身也必须是真目录（不是链接 / 重解析点）；任一段是链接 / 重解析点 / 不是目录 →
    PermissionError；缺段且不建 → FileNotFoundError。
    """
    cur = Path(anchor)
    _check_dir(cur, str(cur))
    for part in parts:
        check_part(part)
        nxt = cur / part
        try:
            os.lstat(nxt)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(nxt, mode)
            except FileExistsError:
                pass
        _check_dir(nxt, part)
        cur = nxt
    return cur


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    """只有可核对的设备号和非零文件编号相同，才证明是同一文件。

    文件系统未提供编号时不能假定核对成功，读写入口会拒绝操作。
    """
    return bool(a.st_ino and b.st_ino and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino))


def lstat_regular(path: Path, label: str) -> os.stat_result:
    """目标必须是独立的普通文件（不是链接 / 重解析点 / 目录 / 硬链接）；不存在 → FileNotFoundError。"""
    st = os.lstat(path)
    if is_link_like(st):
        raise PermissionError(f"文件不是普通文件或为符号链接：{label}")
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(f"{label!r} 是目录，不是文件")
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise PermissionError(f"文件不是独立的普通文件：{label}")
    return st


def open_read_fd(path: Path, label: str) -> int:
    """lstat 校验后只读打开，打开后再用 fstat 复核身份；返回 fd（调用方负责关）。"""
    before = lstat_regular(path, label)
    fd = os.open(path, os.O_RDONLY | _open_extra())
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or not _same_file(before, after):
            raise PermissionError(f"文件在打开时被替换：{label}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def create_exclusive(path: Path, mode: int = 0o644) -> int:
    """`O_CREAT|O_EXCL` 独占新建（名字已被占，包括被链接占着 → FileExistsError），返回 fd。"""
    check_part(Path(path).name)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _open_extra(), mode)


def _write_all(fd: int, data: bytes, *, mode: int | None = None) -> None:
    """经 fd 写完并关掉；`mode` 给了就在关之前经同一个 fd 改权限位（不按路径再找一次文件）。"""
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        if mode is not None and os.name != "nt" and hasattr(os, "fchmod"):
            # Windows 只有只读位，抄过去反而让后面的替换失败——跳过
            os.fchmod(f.fileno(), mode)


def replace_atomic(
    parent: Path,
    last: str,
    data: bytes,
    *,
    old: os.stat_result | None,
    label: str,
    recheck: Callable[[], Any],
) -> None:
    """同一已校验目录里写独占临时文件，复核后 `os.replace` 原子落位；绝不对旧文件截断 / 写入。

    - `old`：写之前 lstat 到的目标（None = 原来不存在）；
    - `recheck`：替换前重新逐段校验父目录链（目录被换成链接 / 重解析点就抛 PermissionError）。
    """
    check_part(last)
    target = Path(parent) / last
    tmp = Path(parent) / f".maiwork-write-{secrets.token_hex(12)}"
    fd = create_exclusive(tmp, 0o644)
    try:
        try:
            _write_all(fd, data, mode=stat.S_IMODE(old.st_mode) if old is not None else None)
        finally:
            fd = -1
        recheck()
        try:
            current = os.lstat(target)
        except FileNotFoundError:
            current = None
        if (
            (old is None) != (current is None)
            or (old is not None and current is not None and not _same_file(old, current))
            or (
                current is not None
                and (is_link_like(current) or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1)
            )
        ):
            raise PermissionError(f"写入期间文件被替换：{label}")
        os.replace(tmp, target)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def create_new_verified(anchor: Path, parts: Iterable[str], name: str, data: bytes) -> bool:
    """`write_new_file_verified` 的无 dir_fd 版本：逐段建目录、独占新建、写完读回来逐字核对。"""
    check_part(name)
    directory = ensure_dirs(anchor, list(parts), create=True)
    target = directory / name
    fd = create_exclusive(target, 0o644)
    try:
        _write_all(fd, data)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    try:
        fd = open_read_fd(target, name)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as f:
        return f.read() == data
