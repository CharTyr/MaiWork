"""跨平台补齐（PRD 10-31 R2+R3）：Windows 画图浏览器识别、系统字体目录、
子进程最小 PATH、社交平台子进程的 Windows 主目录隔离环境。

全部在 macOS / Linux 上用模块平台开关模拟 Windows，并用假的 SystemRoot 环境变量验证
Windows 分支；POSIX 分支断言保持原样。另起一个真实 Python 子进程核对 Path.home()，
不联网、不真起浏览器。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import optional_deps as od
from CharTyr_MaiWork.maiwork import render_assets as ra
from CharTyr_MaiWork.maiwork import social_platforms as sp


def _fake_windows(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """把这台 POSIX 测试机假装成原生 Windows：三个模块各自的 _OS_NAME 桩 + SystemRoot。

    不改 `os.name`：那会让 `Path()` 在 POSIX 上变 WindowsPath 当场炸，测不出真行为。
    """
    for mod in (od, ra, sp):
        monkeypatch.setattr(mod, "_OS_NAME", "nt", raising=False)
    monkeypatch.setenv("SystemRoot", str(root))


def _touch_exe(path: Path) -> Path:
    """造一个 .exe 文件——**故意不给执行位**：Windows 的 .exe 不需要 POSIX 执行位。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


# ----------------------------------------------------------------------
# R2.1 Windows Chromium 可执行文件识别
# ----------------------------------------------------------------------


def test_find_chromium_windows_headless_shell(monkeypatch, tmp_path: Path) -> None:
    _fake_windows(monkeypatch, tmp_path / "winroot")
    base = tmp_path / "ms-playwright"
    exe = _touch_exe(base / "chromium_headless_shell-1208" / "chrome-headless-shell-win64" / "chrome-headless-shell.exe")
    assert ra.find_chromium(base) == exe


def test_find_chromium_windows_legacy_headless_shell(monkeypatch, tmp_path: Path) -> None:
    _fake_windows(monkeypatch, tmp_path / "winroot")
    base = tmp_path / "ms-playwright"
    exe = _touch_exe(base / "chromium_headless_shell-1100" / "chrome-win" / "headless_shell.exe")
    assert ra.find_chromium(base) == exe


def test_find_chromium_windows_full_chrome(monkeypatch, tmp_path: Path) -> None:
    _fake_windows(monkeypatch, tmp_path / "winroot")
    base = tmp_path / "ms-playwright"
    exe = _touch_exe(base / "chromium-1208" / "chrome-win64" / "chrome.exe")
    assert ra.find_chromium(base) == exe


def test_find_chromium_windows_prefers_headless(monkeypatch, tmp_path: Path) -> None:
    """同一目录里无头壳和完整版都有 → 先认无头壳（更小更省内存，与 Linux/macOS 一致）。"""
    _fake_windows(monkeypatch, tmp_path / "winroot")
    base = tmp_path / "ms-playwright"
    headless = _touch_exe(base / "chromium_headless_shell-1208" / "chrome-headless-shell-win64" / "chrome-headless-shell.exe")
    _touch_exe(base / "chromium-1208" / "chrome-win64" / "chrome.exe")
    assert ra.find_chromium(base) == headless


def test_find_chromium_windows_newest_revision(monkeypatch, tmp_path: Path) -> None:
    _fake_windows(monkeypatch, tmp_path / "winroot")
    base = tmp_path / "ms-playwright"
    _touch_exe(base / "chromium-1100" / "chrome-win64" / "chrome.exe")
    new = _touch_exe(base / "chromium-1208" / "chrome-win64" / "chrome.exe")
    assert ra.find_chromium(base) == new


def test_find_chromium_posix_still_requires_exec_bit(tmp_path: Path) -> None:
    """POSIX 防线不变：没执行位的不算可执行文件（防回归）。"""
    base = tmp_path / "ms-playwright"
    exe = base / "chromium_headless_shell-1208" / "chrome-linux" / "headless_shell"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    if os.name == "nt":
        pytest.skip("Windows 真机：.exe 不需要执行位")
    assert ra.find_chromium(base) is None
    exe.chmod(0o755)
    assert ra.find_chromium(base) == exe


# ----------------------------------------------------------------------
# R2.2 macOS / Windows 系统字体目录
# ----------------------------------------------------------------------


def test_system_font_dirs_macos() -> None:
    out = ra._system_font_dirs("macos", env={}, home=Path("/Users/test-user"))
    names = [p.as_posix() for p in out]
    assert names == [
        "/System/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
        "/Users/test-user/Library/Fonts",
    ]


def test_system_font_dirs_windows(monkeypatch, tmp_path: Path) -> None:
    win = tmp_path / "Windows"
    local = tmp_path / "Local"
    out = ra._system_font_dirs(
        "nt",
        env={"SystemRoot": str(win), "LOCALAPPDATA": str(local)},
        home=Path("unused"),
    )
    assert out == [win / "Fonts", local / "Microsoft" / "Windows" / "Fonts"]


def test_system_font_dirs_windows_missing_env(monkeypatch) -> None:
    """Windows 环境变量读不到时不许塞「/Fonts」这种假路径进去。"""
    out = ra._system_font_dirs("nt", env={}, home=Path("unused"))
    assert out == []
    out2 = ra._system_font_dirs("nt", env={"SystemRoot": r"C:\Windows"}, home=Path("h"))
    assert [str(p) for p in out2] == [str(Path(r"C:\Windows") / "Fonts")]


def test_system_font_dirs_posix_unchanged() -> None:
    out = ra._system_font_dirs("posix", env={}, home=Path("/home/u"))
    assert Path("/usr/share/fonts") in out
    assert Path("/home/u/.fonts") in out


def test_system_cjk_fonts_macos_default_dirs() -> None:
    """默认扫描目录按当前平台取：这台 macOS 上应包含 /System/Library/Fonts。"""
    if sys.platform != "darwin":
        pytest.skip("macOS 专属目录断言")
    hits = ra.system_cjk_fonts()
    assert hits, "macOS 自带 STHeiti / Hiragino / Songti，应能检出中文字体"
    assert ra.font_state(None, system=hits, font=ra.FONT)["state"] == "system"


def test_system_cjk_fonts_windows_dir_scan(monkeypatch, tmp_path: Path) -> None:
    """模拟 Windows Fonts 目录：扫描目录分支（fc-list 不存在）能检出中文字体。"""
    fonts = tmp_path / "Windows" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "msyh.ttc").write_bytes(b"x")
    (fonts / "arial.ttf").write_bytes(b"x")
    out = ra.system_cjk_fonts(fc_runner=lambda a: "", dirs=[fonts])
    assert "msyh.ttc" in out and "arial.ttf" not in out
    assert ra.font_state(None, system=out, font=ra.FONT)["state"] == "system"


# ----------------------------------------------------------------------
# R2.3 Windows 子进程最小 PATH
# ----------------------------------------------------------------------


def test_pip_env_windows_path(monkeypatch, tmp_path: Path) -> None:
    win = tmp_path / "Windows"
    _fake_windows(monkeypatch, win)
    env = od.pip_env(tmp_path / "pylibs")
    assert env["PATH"] == os.pathsep.join([str(win / "System32"), str(win)])
    assert env["SystemRoot"] == str(win)


def test_chromium_env_windows_path(monkeypatch, tmp_path: Path) -> None:
    win = tmp_path / "Windows"
    _fake_windows(monkeypatch, win)
    env = ra.chromium_env(tmp_path / "ms-playwright", hostlibs=None)
    assert env["PATH"] == os.pathsep.join([str(win / "System32"), str(win)])
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "ms-playwright")


def test_fixed_path_shared(monkeypatch, tmp_path: Path) -> None:
    """三处用同一个最小 Windows PATH（shared reuse），别各写一份。"""
    win = tmp_path / "Windows"
    _fake_windows(monkeypatch, win)
    fixed = sp._fixed_path()
    assert fixed == os.pathsep.join([str(win / "System32"), str(win)])
    assert od.pip_env(tmp_path / "p")["PATH"] == fixed
    assert ra.chromium_env(tmp_path / "b", hostlibs=None)["PATH"] == fixed


def test_pip_env_posix_path_unchanged(monkeypatch, tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows 真机")
    env = od.pip_env(tmp_path / "pylibs")
    assert env["PATH"] == od.PIP_PATH
    assert "SystemRoot" not in env


# ----------------------------------------------------------------------
# R3 社交平台子进程 Windows 隔离环境
# ----------------------------------------------------------------------


def test_child_env_windows_vars(monkeypatch, tmp_path: Path) -> None:
    win = tmp_path / "Windows"
    _fake_windows(monkeypatch, win)
    monkeypatch.setattr(sp, "_VENV_BIN", "Scripts")
    home = tmp_path / "home"
    root = tmp_path / "root"
    env = sp._child_env(home, root, os_name="nt")
    assert env["USERPROFILE"] == str(home)
    assert env["APPDATA"] == str(home / ".config")
    assert env["LOCALAPPDATA"] == str(home / ".cache")
    assert env["TEMP"] == str(home)
    assert env["TMP"] == str(home)
    assert env["HOME"] == str(home)          # 双写：Windows Python 读 USERPROFILE，保守的库读 HOME
    # 私目录必须落在 home 之下，绝不指向宿主真实用户目录
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        assert Path(env[key]).is_relative_to(home) or Path(env[key]) == home


def test_child_process_home_is_private(tmp_path: Path) -> None:
    home = tmp_path / "private-home"
    env = sp._child_env(home, tmp_path / "root", os_name="nt")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=15, check=True,
    )
    assert Path(result.stdout.strip()) == home


def test_child_env_posix_unchanged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "root"
    env = sp._child_env(home, root, os_name="posix")
    assert "USERPROFILE" not in env
    assert "APPDATA" not in env
    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")


def test_child_env_windows_reddit_credentials(monkeypatch, tmp_path: Path) -> None:
    """rdt-cli 在 Windows 读 APPDATA/rdt-cli/credential.json——把登录态落到子进程 APPDATA 下。"""
    win = tmp_path / "Windows"
    _fake_windows(monkeypatch, win)
    home = tmp_path / "home"
    home.mkdir()
    env = sp._child_env(home, tmp_path / "root", os_name="nt")
    sp._credentials(home, sp.PLATFORMS["reddit"], {"reddit_session": "fixture-session"}, env)
    # XDG 位置（CLI 的跨平台实现读这里）
    xdg = home / ".config" / "rdt-cli" / "credential.json"
    assert xdg.is_file()
    # APPDATA 位置（Windows 原生实现读这里）：与 XDG 对齐，内容同一份
    appdata = Path(env["APPDATA"]) / "rdt-cli" / "credential.json"
    assert appdata.is_file()
    assert appdata.read_text() == xdg.read_text()
    import json
    assert json.loads(appdata.read_text())["cookies"]["reddit_session"] == "fixture-session"


def test_child_env_reddit_posix_no_appdata_duplication(tmp_path: Path) -> None:
    """POSIX 上不建 Windows 专有的 APPDATA 副本（只写 XDG 一处）。"""
    home = tmp_path / "home"
    home.mkdir()
    env = sp._child_env(home, tmp_path / "root")
    sp._credentials(home, sp.PLATFORMS["reddit"], {"reddit_session": "fixture-session"}, env)
    assert (home / ".config" / "rdt-cli" / "credential.json").is_file()
    assert not (home / ".cache" / "rdt-cli").exists()  # LOCALAPPDATA 在 POSIX 下不存在
