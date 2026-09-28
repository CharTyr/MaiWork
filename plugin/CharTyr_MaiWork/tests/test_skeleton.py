"""本地单元测试（Mac 上用 ~/.venvs/maiwork 跑，不需要 MaiBot 宿主）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if not (PLUGIN_DIR / "plugin.py").is_file():
    raise RuntimeError(f"找不到插件目录: {PLUGIN_DIR}")
sys.path.insert(0, str(PLUGIN_DIR.parent))

from CharTyr_MaiWork.plugin import MaiWorkConfig, create_plugin  # noqa: E402


def test_default_disabled() -> None:
    assert MaiWorkConfig().plugin.enabled is False


def test_lifecycle() -> None:
    p = create_plugin()
    asyncio.run(p.on_load())
    asyncio.run(p.on_config_update("self", {}, "0.0.1"))
    asyncio.run(p.on_unload())
