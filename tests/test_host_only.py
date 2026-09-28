"""源码扫描：host.py 以外的 .py 文件里不许出现 call_capability。"""

from __future__ import annotations

from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# host.py 是唯一允许调用 call_capability 的模块
_ALLOWED = {"host.py"}
# tests/ 和 tests_host/ 不扫
_SKIP_DIRS = {"tests", "tests_host", "__pycache__"}


def _py_files(root: Path) -> list[Path]:
    """递归收集插件目录下所有 .py（排除 ._* 苹果垃圾文件、tests、host.py 本身）。"""
    results: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        if p.name.startswith("._"):
            continue
        if p.name in _ALLOWED and len(rel_parts) == 1:
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
