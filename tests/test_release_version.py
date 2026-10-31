"""发布版本号元数据：发布前必须一起对齐（0.8.0 见 docs/19；0.9.0 = 构想可行性闸 + 资讯来源多样化；0.9.1 = 取源跟随跳转 + 搜索软倾斜 + 找来源搜索 + 优质来源占比；0.9.2 = 任务双岗协作（docs/20），库号 35，2026-10-05；0.9.3 = 任务执行重设计 + 资讯卡片改版 + 个人提议核实 + 交接包 + 上下文压缩二轮 + 交付体验 + 派活备忘，库号 35 → 37，2026-10-09；0.9.4 = 多家判断服务 + 派活判断兜底，配置版本 0.4.8，库号不变，2026-10-11；0.9.5 = 派活判断兜底精简（一条 @ 立刻判、Jev 带前文、待批显示判断依据）+ /mw do 指令直接派活，配置版本 0.4.9，库号不变，2026-10-11；0.9.6 = /mw 帮助 + 指令回复带下一步提示 + 群页「群指令」卡片，配置 / 库号都不变，2026-10-11；0.9.7 = 安全与稳定加固：管理员对话改关键设置要确认、子 agent 默认碰不到本机与内网、运行账号禁 root、自动审核防注入、读不到用量就暂停、聊天记录查重走索引表、网页加载失败可重试，配置版本 0.5.0，库号 → 40，2026-10-12；0.9.8 = 派活持久补办与待复核、资讯本轮正文证据与自检安全回落，配置版本仍 0.5.0，库号 41，2026-10-14；0.9.9 = 图解改零件清单（群卡片平铺完整可读）+ 临时机器加 Freestyle（额外添加）+ 生图模型 + 内置 skill 五件套，配置版本 0.5.1，库号不变，2026-10-15；0.10.0 = 在 0.9.9 之上：@ 钩子不再等 Jev（避开宿主熔断）、资讯卡片失败改发文字 + 手动重发 + JPEG、待复核提醒与 14 天归档、生图接通（报告页放图、群里发图、排计划认得）、办公文档三库改可选组件，配置版本仍 0.5.1，库号仍 41，2026-10-15；0.10.1 = 界面与提示文字整理：网页提示 / 状态 / 报错改简洁统一的写法、机器报错只显示中文原因、修正几处设置路径的说法，配置版本仍 0.5.1，库号仍 41，2026-10-15；0.10.2 = 任务优先在机器上执行（有机器就上机器、本机兜底）+ Freestyle 默认每台最长 90 分钟、每天最多 5 台，配置版本仍 0.5.1，库号仍 41，2026-10-16；0.10.3 = 社交平台搜索、Twitter 网页补搜与安全读帖、复古终端主题和按钮音效，配置版本仍 0.5.1，库号仍 41，2026-10-17；0.11.0 = 群内工作会话 + 开箱即用（here.now 默认公开、群文件重传与原因、缺 SSH 修法、引导交付自检）+ Docker 检查与字体 / 画图浏览器一键装，配置版本 0.5.3，库号 44，2026-10-25；0.11.1 = 修复与加固：后台任务留引用、登录限流表清理、补 8 个索引、密码改 scrypt（老哈希登录时自动升级）、[console] trusted_proxies、用量历史挪出事件循环、Freestyle 分片上传，配置版本仍 0.5.3，库号 45，2026-10-27；0.11.2 = Windows 上社交平台独立环境改用 venv\Scripts\*.exe（安装与找 CLI），配置 / 库号都不变，2026-10-27；0.12.0 = 管理员看板（每个专岗一栏进行中 / 历史、条目详情含工具调用与思考链）+ 各轮条目精确关联 run_ref + 模型思考过程进调用日志，配置版本 0.5.4（[console] board_hours），库号 45 → 47（45 = 公开 0.11.1 的补索引，46 = main_runs，47 = 各轮条目 run_ref），2026-10-30；0.12.1 = 专岗便条板（各岗互相留话、检查通过才贴）+ 管理员对话改 MCP / 专岗 / 端点 / 模型库（复用网页校验、密钥不进对话）+ 文档分现状与历史，配置版本仍 0.5.4，库号 47 → 48（48 = 便条板 agent_notes），2026-10-30；0.13.0 = 调研岗做法工具（skill allowed-tools 带注册给 worker 的工具、自动回合单独工作区、资讯补找防编造）+ 管理员对话诊断（read_logs 含工具调用、news_manage 漏斗行、tool_run 试调）+ 管理员工具大瘦身（63 → 25 个合并入口工具 + action 分派，确认边界不变）+ 管理员对话输入框回车换行（Ctrl/Cmd+Enter 发送），配置版本仍 0.5.4，库号仍 48，2026-10-31；0.14.0 = 三条主动通道同素材 12h 互斥与冷场统计修正 + 备忘录定向投放与纯内存临时备忘（outbox 瘦身，库号 48 → 49）+ 群友提醒四修 + 只读回填宿主图片/表情包识图描述 + 引用构想提一嘴直接派活 + 本地成品永久副本、外链过期重发、分享页图解、交接包核实链接、开话题记录折叠、资讯主题筛选与搜索 + 网页交互动效与触屏体验打磨（手机抽屉跟手下滑关闭、复制原地变形、菜单方向展开与温和减动效），配置版本仍 0.5.4，库号 49，2026-10-31；0.15.0 = 看图任务闭环（多模态协议适配 openai/anthropic/responses、视觉候选链优先 vision=true、read_image/inspect_image 与 read_file 图片魔数拦截）+ 能力边界三道刹车（unsupported 诚实收尾、开工前看图能力闸、首轮无进展止损）+ 闲时自主探索成熟做法（skill_explore 每群每天最多 1 次隔离沙箱查公开文档并做真机最小验证，过四道硬门槛后存入本群 task 做法库）+ 网页设置与群管理全面改版打磨（全局设置 11 合 6 配页内二级标签与旧地址兼容、复杂表单统一进右侧抽屉、模型能力就绪条、群页管理员「动态与画像 | 本群设置」二分栏与常驻即点即存开关、自动探索做法标签与空筛选重置），配置版本仍 0.5.4，库号仍 49，2026-10-31）。

只读 manifest / plugin.py / config.py 里的版本常量，不碰线上配置：
- 发布版本 = 0.15.0（`_manifest.json` 的 version 与 `plugin.PLUGIN_VERSION` 必须一致）；
- `CONFIG_VERSION` = 0.5.4（0.5.4 新增 [console] board_hours；0.5.3 新增 [console] herenow_share），
  所有字段均有默认值（0.11.1 新加的 [console] trusted_proxies 默认空，不升配置版本）；
  库号 49（45 补索引、46 main_runs、47 条目精确关联 run_ref、48 专岗便条板 agent_notes、49 备忘录定向投放 target_stream_id 与 outbox 瘦身）；
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

RELEASE_VERSION = "0.15.0"
CONFIG_VERSION_EXPECTED = "0.5.4"  # 0.5.4：[console] board_hours（任务看板）
# SDK 兼容区间 = 实测端点本身，不写「大版本 + .99」的宽口。宿主对 SDK 超范围是硬错误
# （manifest_validator._validate_runtime_compatibility 直接把 SDK 不兼容 append 进 errors；
# Host 才有「同主次版本、只是修订号更高 → 记 warning 按兼容模式加载」的回落，SDK 没有），
# 所以这两个数字就是加载闸门，写宽等于替未验证的 SDK 打包票。端点证据与实测记录见 docs/06。
SDK_MIN_VERIFIED = "2.8.1"   # 本地开发环境与仓库 SDK 副本；requirements-dev.txt:18 固定该提交
SDK_MAX_VERIFIED = "2.16.0"  # 2026-10-25 本地实测：MaiBot dev 1.3.15 + SDK 2.16.0 跑 tests_host（导入 / 钩子 / 会话命令注册 / 生命周期）；线上宿主仍是 2.12.0（见 docs/06）
SDK_VERSIONS_SEEN = ("2.8.1", "2.8.2", "2.10.0", "2.12.0", "2.16.0")
HOST_VERSION_COVERED = "1.3.5"
# 宿主最高适配版本（用户 2026-10-11 定）：写成「<主>.<次>.99」，不写 1.99.99 这种不规范的宽口；
# 每次发版先查 MaiBot dev 分支 pyproject.toml 的版本，次版本超过这里就跟上（2026-10-31 再次核实 dev pyproject = 1.3.19，次版本仍 1.3，上限不变）。
HOST_MAX_VERSION = "1.3.99"
HOST_DEV_VERSION_SEEN = "1.3.19"
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
