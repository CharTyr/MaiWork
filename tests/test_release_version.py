"""发布版本号元数据：发布前必须一起对齐（0.8.0 见 docs/19；0.9.0 = 构想可行性闸 + 资讯来源多样化；0.9.1 = 取源跟随跳转 + 搜索软倾斜 + 找来源搜索 + 优质来源占比；0.9.2 = 任务双岗协作（docs/20），库号 35，2026-10-05；0.9.3 = 任务执行重设计 + 资讯卡片改版 + 个人提议核实 + 交接包 + 上下文压缩二轮 + 交付体验 + 派活备忘，库号 35 → 37，2026-10-09；0.9.4 = 多家判断服务 + 派活判断兜底，配置版本 0.4.8，库号不变，2026-10-11；0.9.5 = 派活判断兜底精简（一条 @ 立刻判、Jev 带前文、待批显示判断依据）+ /mw do 指令直接派活，配置版本 0.4.9，库号不变，2026-10-11；0.9.6 = /mw 帮助 + 指令回复带下一步提示 + 群页「群指令」卡片，配置 / 库号都不变，2026-10-11；0.9.7 = 安全与稳定加固：管理员对话改关键设置要确认、子 agent 默认碰不到本机与内网、运行账号禁 root、自动审核防注入、读不到用量就暂停、聊天记录查重走索引表、网页加载失败可重试，配置版本 0.5.0，库号 → 40，2026-10-12；0.9.8 = 派活持久补办与待复核、资讯本轮正文证据与自检安全回落，配置版本仍 0.5.0，库号 41，2026-10-14；0.9.9 = 图解改零件清单（群卡片平铺完整可读）+ 临时机器加 Freestyle（额外添加）+ 生图模型 + 内置 skill 五件套，配置版本 0.5.1，库号不变，2026-10-15；0.10.0 = 在 0.9.9 之上：@ 钩子不再等 Jev（避开宿主熔断）、资讯卡片失败改发文字 + 手动重发 + JPEG、待复核提醒与 14 天归档、生图接通（报告页放图、群里发图、排计划认得）、办公文档三库改可选组件，配置版本仍 0.5.1，库号仍 41，2026-10-15；0.10.1 = 界面与提示文字整理：网页提示 / 状态 / 报错改简洁统一的写法、机器报错只显示中文原因、修正几处设置路径的说法，配置版本仍 0.5.1，库号仍 41，2026-10-15；0.10.2 = 任务优先在机器上执行（有机器就上机器、本机兜底）+ Freestyle 默认每台最长 90 分钟、每天最多 5 台，配置版本仍 0.5.1，库号仍 41，2026-10-16；0.10.3 = 社交平台搜索、Twitter 网页补搜与安全读帖、复古终端主题和按钮音效，配置版本仍 0.5.1，库号仍 41，2026-10-17）。

只读 manifest / plugin.py / config.py 里的版本常量，不碰线上配置：
- 发布版本 = 0.10.4（`_manifest.json` 的 version 与 `plugin.PLUGIN_VERSION` 必须一致）；
- `CONFIG_VERSION` 0.5.0 → 0.5.1（新加 [environments] freestyle 开关 + vm_platform + freestyle_*，默认仍只开 railway，
  行为不变；老配置照常读），宿主不需要为这次 bump 改任何插件配置；库号保持 41；
- manifest 声明的宿主版本范围仍然覆盖线上宿主（MaiCore 1.3.5，docs/06；更早实测过 1.3.2），
  SDK 区间只写实测端点本身（见下面常量与 test_sdk_bounds_are_exact_verified_endpoints）；
- 不给 MaiBot 的 planner 注册任何工具（设计红线）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from packaging.version import Version

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))

RELEASE_VERSION = "0.10.4"
CONFIG_VERSION_EXPECTED = "0.5.1"
# SDK 兼容区间 = 实测端点本身，不写「大版本 + .99」的宽口。宿主对 SDK 超范围是硬错误
# （manifest_validator._validate_runtime_compatibility 直接把 SDK 不兼容 append 进 errors；
# Host 才有「同主次版本、只是修订号更高 → 记 warning 按兼容模式加载」的回落，SDK 没有），
# 所以这两个数字就是加载闸门，写宽等于替未验证的 SDK 打包票。端点证据与实测记录见 docs/06。
SDK_MIN_VERIFIED = "2.8.1"   # 本地开发环境与仓库 SDK 副本；requirements-dev.txt:18 固定该提交
SDK_MAX_VERIFIED = "2.12.0"  # 线上宿主 venv 实测（2.8.2 / 2.10.0 更早实测过，见 docs/06）
SDK_VERSIONS_SEEN = ("2.8.1", "2.8.2", "2.10.0", "2.12.0")
HOST_VERSION_COVERED = "1.3.5"
# 宿主最高适配版本（用户 2026-10-11 定）：写成「<主>.<次>.99」，不写 1.99.99 这种不规范的宽口；
# 每次发版先查 MaiBot dev 分支 pyproject.toml 的版本，次版本超过这里就跟上（2026-10-17 再次通过 GitHub API 核实 dev = 1.3.10）。
HOST_MAX_VERSION = "1.3.99"
HOST_DEV_VERSION_SEEN = "1.3.10"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_release_version_matches():
    from CharTyr_MaiWork.plugin import PLUGIN_VERSION

    assert PLUGIN_VERSION == RELEASE_VERSION, PLUGIN_VERSION


def test_manifest_and_code_version_parity():
    from CharTyr_MaiWork.plugin import PLUGIN_ID, PLUGIN_VERSION

    assert MANIFEST["version"] == PLUGIN_VERSION == RELEASE_VERSION
    assert MANIFEST["id"] == PLUGIN_ID
    assert SEMVER.match(MANIFEST["version"])


def test_config_version_expected():
    from CharTyr_MaiWork.maiwork.config import CONFIG_VERSION

    assert CONFIG_VERSION == CONFIG_VERSION_EXPECTED, CONFIG_VERSION


def test_declared_sdk_and_host_range_still_supported():
    sdk = MANIFEST["sdk"]
    host = MANIFEST["host_application"]
    for key in (sdk, host):
        assert SEMVER.match(key["min_version"]) and SEMVER.match(key["max_version"])
        assert Version(key["min_version"]) <= Version(key["max_version"])
    for v in SDK_VERSIONS_SEEN:
        assert Version(sdk["min_version"]) <= Version(v) <= Version(sdk["max_version"]), v
    assert Version(host["min_version"]) <= Version(HOST_VERSION_COVERED) <= Version(host["max_version"])


def test_sdk_bounds_are_exact_verified_endpoints():
    """SDK 上下限必须等于实测端点本身：声明「跑过的版本」，不给没跑过的版本打包票。

    审核阻断点就是这两个数字。上限只认实测过的最高版本，不把整条次版本线一次认下来；
    代价是 2.12.1 这类没测过的修订号会被排除，这是严格声明的必然结果。
    """
    sdk = MANIFEST["sdk"]
    assert sdk["min_version"] == SDK_MIN_VERIFIED, sdk
    assert sdk["max_version"] == SDK_MAX_VERIFIED, sdk
    # 端点 = 已实测版本集合的两个极端，防止常量被改成没测过的数字
    assert Version(SDK_MIN_VERIFIED) == min(Version(v) for v in SDK_VERSIONS_SEEN), SDK_MIN_VERIFIED
    assert Version(SDK_MAX_VERIFIED) == max(Version(v) for v in SDK_VERSIONS_SEEN), SDK_MAX_VERIFIED


def test_host_max_version_is_minor_99():
    host_max = MANIFEST["host_application"]["max_version"]
    assert host_max == HOST_MAX_VERSION, host_max
    assert host_max.endswith(".99") and not host_max.startswith("1.99")
    assert Version(HOST_DEV_VERSION_SEEN) <= Version(host_max)


def test_no_planner_tools_registered():
    """只挂收消息钩子和 planner 备忘钩子，组件里不许出现任何工具注册。"""
    from CharTyr_MaiWork.plugin import create_plugin

    comps = create_plugin().get_components()
    text = repr(comps)
    assert "chat.receive.after_process" in text
    for c in comps:
        kind = str(c.get("component_type") or c.get("type") or "").lower()
        assert "tool" not in kind, c
