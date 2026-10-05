"""发布版本号元数据：发布前必须一起对齐（0.8.0 见 docs/19；0.9.0 = 构想可行性闸 + 资讯来源多样化；0.9.1 = 取源跟随跳转 + 搜索软倾斜 + 找来源搜索 + 优质来源占比；0.9.2 = 任务双岗协作（docs/20），库号 35，2026-10-05）。

只读 manifest / plugin.py / config.py 里的版本常量，不碰线上配置：
- 发布版本 = 0.9.2（`_manifest.json` 的 version 与 `plugin.PLUGIN_VERSION` 必须一致）；
- `CONFIG_VERSION` 仍是 0.4.6 —— 本次不加配置项，**不升配置版本**，
  宿主也就不需要为这次 bump 改任何插件配置；
- manifest 声明的 SDK / 宿主版本范围仍然覆盖当前开发（SDK 2.8.1）与线上宿主
  （MaiCore 1.3.2 / SDK 2.8.2，docs/06）；
- 不给 MaiBot 的 planner 注册任何工具（设计红线）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from packaging.version import Version

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))

RELEASE_VERSION = "0.9.2"
CONFIG_VERSION_EXPECTED = "0.4.6"
# 声明范围必须覆盖的版本：本地开发 SDK / 线上宿主与 SDK（docs/06 §QQ 官方适配器统一账号 2026-10-03）
SDK_VERSIONS_COVERED = ("2.8.1", "2.8.2")
HOST_VERSION_COVERED = "1.3.2"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_release_version_is_0_9_2():
    from CharTyr_MaiWork.plugin import PLUGIN_VERSION

    assert PLUGIN_VERSION == RELEASE_VERSION, PLUGIN_VERSION


def test_manifest_and_code_version_parity():
    from CharTyr_MaiWork.plugin import PLUGIN_ID, PLUGIN_VERSION

    assert MANIFEST["version"] == PLUGIN_VERSION == RELEASE_VERSION
    assert MANIFEST["id"] == PLUGIN_ID
    assert SEMVER.match(MANIFEST["version"])


def test_config_version_still_0_4_6():
    from CharTyr_MaiWork.maiwork.config import CONFIG_VERSION

    assert CONFIG_VERSION == CONFIG_VERSION_EXPECTED, CONFIG_VERSION


def test_declared_sdk_and_host_range_still_supported():
    sdk = MANIFEST["sdk"]
    host = MANIFEST["host_application"]
    for key in (sdk, host):
        assert SEMVER.match(key["min_version"]) and SEMVER.match(key["max_version"])
        assert Version(key["min_version"]) <= Version(key["max_version"])
    for v in SDK_VERSIONS_COVERED:
        assert Version(sdk["min_version"]) <= Version(v) <= Version(sdk["max_version"]), v
    assert Version(host["min_version"]) <= Version(HOST_VERSION_COVERED) <= Version(host["max_version"])


def test_no_planner_tools_registered():
    """只挂收消息钩子和 planner 备忘钩子，组件里不许出现任何工具注册。"""
    from CharTyr_MaiWork.plugin import create_plugin

    comps = create_plugin().get_components()
    text = repr(comps)
    assert "chat.receive.after_process" in text
    for c in comps:
        kind = str(c.get("component_type") or c.get("type") or "").lower()
        assert "tool" not in kind, c
