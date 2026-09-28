"""源码扫描：host.py 以外的 .py 文件里不许出现 call_capability；也不许用 __getattr__ 绕扫描。"""

from __future__ import annotations

from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# maiwork/host.py 是唯一允许调用 call_capability 的模块
_ALLOWED = {"host.py"}
# tests/ 和 tests_host/ 不扫
_SKIP_DIRS = {"tests", "tests_host", "__pycache__"}


def _py_files(root: Path, *, keep_allowed: bool = False) -> list[Path]:
    """递归收集插件目录下所有 .py（排除 ._* 苹果垃圾文件、tests；默认也排除 host.py）。"""
    results: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        if p.name.startswith("._"):
            continue
        if not keep_allowed and p.name in _ALLOWED and rel_parts[:-1] == ("maiwork",):
            continue
        results.append(p)
    return results


def test_no_call_capability_outside_host(plugin_dir: Path) -> None:
    offenders: list[str] = []
    for py in _py_files(plugin_dir):
        src = py.read_text(encoding="utf-8")
        if "call_capability" in src:
            offenders.append(str(py.relative_to(plugin_dir)))
    assert offenders == [], (
        "除 host.py 以外的文件里不许出现 call_capability：" + ", ".join(offenders)
    )


# 动态属性替身只能有一个用途：outbox._RowWithResult（把 result dict 投到数据库行上）。
# 不许再用 __getattr__ 造「能力替身」来绕上面那条扫描（插件中心审核整改 2：plugin.py
# 原来的 _NullCtx 就是这样，已删——没有宿主 ctx 时干脆不启动 app）。
_ALLOWED_GETATTR = {"maiwork/outbox.py"}


def test_no_getattr_capability_shim(plugin_dir: Path) -> None:
    offenders: list[str] = []
    for py in _py_files(plugin_dir, keep_allowed=True):
        rel = str(py.relative_to(plugin_dir))
        if rel in _ALLOWED_GETATTR:
            continue
        if "def __getattr__" in py.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == [], (
        "不许用 __getattr__ 做动态属性替身（绕源码扫描）；只允许 "
        + "、".join(sorted(_ALLOWED_GETATTR))
        + "："
        + ", ".join(offenders)
    )


def test_outbox_row_proxy_is_the_only_exception(plugin_dir: Path) -> None:
    """例外本身写明白：outbox.py 的 __getattr__ 挂在 _RowWithResult 上，不是能力替身。"""
    src = (plugin_dir / "maiwork" / "outbox.py").read_text(encoding="utf-8")
    assert "class _RowWithResult:" in src
    assert "_NullCtx" not in src
