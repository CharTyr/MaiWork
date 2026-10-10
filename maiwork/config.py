"""配置模型（宿主 config.toml）+ Settings（规范化后的只读快照）。

- MaiWorkConfig 是宿主加载/校验用的 pydantic 模型，各节对应 docs/02-设计.md §12.4。
- load_settings 把 MaiWorkConfig（或裸 dict）规范化成 Settings；非法条目丢弃并记
  中文问题，绝不抛异常——配置写错不能让插件加载失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from maibot_sdk import Field, PluginConfigBase

_ACCOUNT_RE = re.compile(r"^([a-z][a-z0-9_]{0,15}):(\S{1,64})$")

# 「平台」别名 → 规范名（bot_config 里 Telegram 账号写成 "tg:..."）
_PLATFORM_ALIASES: dict[str, str] = {
    "tg": "telegram",
    "qqofficial": "qqbot",
    "qq_official": "qqbot",
}

# MaiWork 内部平台标签 → 宿主里的平台名。qqbot = QQ 官方机器人（社区 qq-official-adapter），
# 它在宿主里的平台名也是 "qq"，靠群号形态（openid，不是纯数字）和 SnowLuma 的群分开（docs/06）
_HOST_PLATFORM: dict[str, str] = {
    "qqbot": "qq",
}


def _norm_platform(platform: str) -> str:
    """平台名规范化：小写；别名（tg → telegram）换规范名。"""
    p = str(platform or "").strip().lower()
    return _PLATFORM_ALIASES.get(p, p)


def has_onebot(platform: str) -> bool:
    """这个平台的群有没有 OneBot / napcat 能力（群文件、公告、相册、成员身份、api.call）。
    只有 SnowLuma 接的 qq 群有；telegram、qqbot（QQ 官方机器人）等一律没有。"""
    return str(platform or "qq").strip().lower() == "qq"


def host_platform(platform: str) -> str:
    """MaiWork 内部平台标签 → 宿主（MaiBot）里的平台名（qqbot → qq，其余一一对应）。"""
    p = str(platform or "qq").strip().lower() or "qq"
    return _HOST_PLATFORM.get(p, p)


def norm_account(raw: object) -> str:
    """账号统一成 MaiBot 的写法「平台:账号」（小写平台）。只写数字的旧写法当 qq。认不出返回 ""。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    if ":" not in s:
        return f"qq:{s}" if s.isdigit() else ""
    platform, _, acc = s.partition(":")
    cand = f"{_norm_platform(platform)}:{acc.strip()}"
    return cand if _ACCOUNT_RE.match(cand) else ""


def _norm_accounts(values: object, field_zh: str, problems: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for v in (values or []):
        n = norm_account(v)
        if not n:
            problems.append(f"[approval] {field_zh}里的 {v!r} 认不出，要写成「平台:账号」（如 qq:100000001），这条先跳过")
            continue
        if n not in out:
            out.append(n)
    return tuple(out)


CONFIG_VERSION = "0.4.8"  # 0.4.8：[quick_judge] 派活判断兜底（Jev 不在 / 拿不准时用模型快速判群里 @ 的是不是派活）；0.4.7：[jev] use + [[jev_endpoints]]（多判断服务：预设 / 自己加 / 换着用）；0.4.6：每端点高级请求头 headers（默认空）；0.4.5：[[endpoints]] / [[model_list]]（模型改版阶段 1a）；0.4.4：[console] update_check、maibot_webui_url；0.4.3：[feeds] viz_per_day；0.4.2：[models] max_tokens；0.4.1：[reader] Jina Reader；0.4.0：[models] context_window、[tasks] 安全网、[feeds] collect_minutes

# 0.8.0 群控归一（docs/18 §五 + 往群里发）：下面这些全局键**只作新群第一次的迁移种子**
# （migrations.migrate_group_controls 按服务群种进 kv["group_approval.<群号>"] /
# kv["group_push.<群号>"]），运行时的唯一真源是每群那一份。字段留在配置模型里只为
# 兼容旧 config.toml 不报错 / 迁移读得到旧值；网页与 set_rules 都不再能改它们。
SEED_ONLY_GLOBAL_KEYS: tuple[str, ...] = (
    "approval.required",
    "approval.admins",
    "approval.exempt_groups",
    "approval.exempt_users",
    "topics.enabled",
    "topics.speaker",
    "topics.per_day",
    "delivery.push_per_day",
    "delivery.quiet_hours",
)

# 插件目录 = 本文件所在目录；默认数据目录 = 插件目录上两级 / data / maiwork
# （线上 <MaiBot>/plugins/CharTyr_MaiWork → <MaiBot>/data/maiwork）
# 插件根目录（plugins/CharTyr_MaiWork）：代码在 maiwork/ 子包里，所以往上两级
_PLUGIN_DIR = Path(__file__).resolve().parent.parent


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    # 宿主在没写 enabled 时会当成开启，所以这里必须显式默认 False。
    enabled: bool = Field(default=False, description="是否启用 MaiWork；默认关闭")
    config_version: str = Field(default=CONFIG_VERSION, description="配置版本")


class ServeGroupConfig(PluginConfigBase):
    """一个服务群；多个群填同一个 workspace 即共享一个工作区。"""

    __ui_label__ = "服务的群"
    __ui_icon__ = "users"

    group: str = Field(
        default="",
        description=(
            '群号，格式 "qq:号码"、"telegram:群 ID"（tg: 也是 telegram）或 "qqbot:群 openid"（QQ 官方机器人）；'
            '如 "qq:123456789"、"telegram:-1001234567890"'
        ),
    )
    workspace: str = Field(default="", description='工作区名；空则用 "g<群号>"；多个群填同一个值即共享工作区')


class GroupsSectionConfig(PluginConfigBase):
    __ui_label__ = "服务群"
    __ui_icon__ = "users"
    __ui_order__ = 1

    serve: list[ServeGroupConfig] = Field(default_factory=list, description="MaiWork 服务的群列表")


class FocusSectionConfig(PluginConfigBase):
    __ui_label__ = "关注的人"
    __ui_icon__ = "star"
    __ui_order__ = 2

    max_members: int = Field(default=5, description="每群最多关注几个人")
    personal_profile: bool = Field(default=True, description="是否给关注成员建个人画像；关掉就只看群")
    personal_feeds: bool = Field(default=True, description="是否给有画像的关注成员出个人向的资讯和构想（只给本人和管理员看，不进群视图）")
    personal_per_day: int = Field(default=3, description="个人向资讯每人每次最多出几条")


class FeedsSectionConfig(PluginConfigBase):
    __ui_label__ = "资讯"
    __ui_icon__ = "newspaper"
    __ui_order__ = 3

    news_slots: list[str] = Field(default_factory=lambda: ["08:30", "14:00", "19:00"], description='每天备资讯的时段，"HH:MM"（北京时间）')
    news_jitter_minutes: int = Field(default=30, description="每个时段随机提前/推后多少分钟以内")
    max_items: int = Field(default=10, description="每批最多入选几条资讯")
    lookback_days: int = Field(default=14, description="和最近多少天已出的资讯去重")
    web_min_avg: float = Field(default=3.0, description="资讯/文章上网页的五项平均分门槛（1~5）")
    pool_min_avg: float = Field(default=4.0, description="资讯进群里开话题候选池的平均分门槛（1~5）")
    guides: bool = Field(default=True, description="备资讯时要不要同时找「文章」（教程、好文章、工具介绍）")
    collect_minutes: int = Field(default=15, description="资讯收集子 agent 每轮最多跑多少分钟（到点把已找到的交回来）")
    viz_per_day: int = Field(default=3, description="每群每天最多给几条没配图、数据多的资讯做「图解」小图（0 = 不做）")


class TopicsSectionConfig(PluginConfigBase):
    __ui_label__ = "开话题"
    __ui_icon__ = "message-circle"
    __ui_order__ = 4

    # enabled / speaker / per_day：0.8.0 起每群一份（group_push.topics_enabled / daily_max；
    # speaker 已退役）——这里只作**新群第一次的迁移种子**，不是第二个运行来源。
    enabled: bool = Field(default=True, description="冷场时是否开话题（种子；现在每群一份）")
    speaker: str = Field(default="maiwork", description='谁说：maiwork=按人设直接发；maibot=请 MaiBot 自己开口（已退役）')
    per_day: int = Field(default=2, description="每群每天最多开几个话题（种子；现在并进每群每日总上限）")
    min_gap_hours: int = Field(default=3, description="同一群两次开话题的最小间隔（小时）")
    candidate_ttl_hours: int = Field(default=12, description="话题候选池里一条资讯多少小时后过期")


class DeliverySectionConfig(PluginConfigBase):
    __ui_label__ = "主动发言"
    __ui_icon__ = "send"
    __ui_order__ = 5

    # 0.8.0 起每群一份（group_push.daily_max / quiet_hours）——只作新群的迁移种子。
    push_per_day: int = Field(default=3, description="每群每天主动推到群里的上限（种子；现在归每群 daily_max）")
    quiet_hours: str = Field(default="23:00-08:00", description="睡觉时段（北京时间）（种子；现在每群一份）")


class ApprovalSectionConfig(PluginConfigBase):
    __ui_label__ = "派活审批"
    __ui_icon__ = "shield-check"
    __ui_order__ = 6

    # required / admins / exempt_groups / exempt_users：0.8.0 起每群一份
    # （kv["group_approval.<群号>"]，group_approval.py）——只作新群的迁移种子。
    required: bool = Field(default=True, description="群友派的活是否要 bot 管理员批准后才能开工（种子；现在每群一份）")
    admins: list[str] = Field(default_factory=lambda: ["qq:100000001"], description="bot 管理员，写成「平台:账号」（种子；现在每群一份）")
    exempt_groups: list[str] = Field(default_factory=list, description="免批的群（种子；现在每群一份），写成「平台:群号」")
    exempt_users: list[str] = Field(default_factory=list, description="派活免批的人（种子；现在每群一份），写成「平台:账号」")
    remind: bool = Field(default=True, description="待批超过 24 小时没人处理时，在群里提醒管理员一次（可关）")
    auto_review: bool = Field(default=True, description="低风险的小活（调研、找东西、做个小网页、出个 PDF）由主模型看过就直接开工；高风险 / 大工程 / 说不清的仍然等你批")
    auto_review_daily: int = Field(default=5, description="每个群每天最多这样自动开工几件；0 = 关掉自动审核")


class ModelsSectionConfig(PluginConfigBase):
    __ui_label__ = "模型端点"
    __ui_icon__ = "robot"
    __ui_order__ = 7

    base_url: str = Field(default="", description="OpenAI 兼容端点地址，如 https://example.com/v1")
    api_key: str = Field(default="", description="端点密钥；只进不出，不写日志")
    main: str = Field(default="", description="主模型名（理解群、决定做什么、验收）")
    main_backup: str = Field(default="", description="主模型备用名（不能为空字符串以外的同值）")
    worker: str = Field(default="", description="子 agent 模型名（干活的）")
    worker_backup: str = Field(default="", description="子 agent 模型备用名")
    retries: int = Field(default=5, description="同一模型调用失败最多重试几次（0~10）；主备模型各自按这个数重试")
    retry_delay_s: int = Field(default=10, description="两次重试之间等几秒（1~60）")
    max_concurrency: int = Field(default=2, description="同一个端点同时最多几个请求在路上（1~8）；被限流（429）多就调成 1")
    max_rpm: int = Field(default=0, description="同一个端点每分钟最多发几次（0 = 不限，最多 600）")
    context_window: int = Field(default=128000, description="模型的上下文窗口（tokens）；对话快满时先截旧工具结果、再总结旧对话")
    max_tokens: int = Field(default=32768, description="一次回答最多写多少 token（1024~1000000）；每次调用都会带上，有些端点不传会出问题")


class EndpointItemConfig(PluginConfigBase):
    """一个 [[endpoints]] 条目（2026-10 模型改版阶段 1a）。

    端点 = 一个模型服务商：base_url + api_key（只进不出）+ 协议 + 重试 / 限流。
    校验在 load_settings 的 _parse_endpoints 里逐条做（坏条目丢条目、记问题，
    不拖垮整段），这里只收留字段。
    """

    __ui_label__ = "模型端点"
    __ui_icon__ = "server"

    id: str = Field(default="", description="端点 id（字母、数字、_、-，1~24 个字符，不能重复）")
    name: str = Field(default="", description="显示名（≤40 字）；空 = 用 id")
    protocol: str = Field(default="openai", description="协议：openai（/chat/completions）/ anthropic（/v1/messages）/ responses（/v1/responses）")
    base_url: str = Field(default="", description="端点地址，http(s):// 开头")
    api_key: str = Field(default="", description="端点密钥；只进不出，不写日志")
    headers: dict[str, str] = Field(default_factory=dict, description="高级请求头覆盖；名称不分大小写，值只进不出，不写日志")
    retries: int = Field(default=5, description="同一模型调用失败最多重试几次（0~10）")
    retry_delay_s: int = Field(default=10, description="两次重试之间等几秒（1~60）")
    max_concurrency: int = Field(default=2, description="这个端点同时最多几个请求在路上（1~8）")
    max_rpm: int = Field(default=0, description="这个端点每分钟最多发几次（0 = 不限，最多 600）")


class ModelListItemConfig(PluginConfigBase):
    """一个 [[model_list]] 条目（2026-10 模型改版阶段 1a）。

    模型库里的一条：挂在某个端点下，写清服务商的模型名 + 能力参数。
    校验在 _parse_model_list 里逐条做（endpoint 必须指向存在的端点）。
    """

    __ui_label__ = "模型库条目"
    __ui_icon__ = "robot"

    id: str = Field(default="", description="模型条目 id（字母、数字、_、-，1~24 个字符，不能重复）")
    endpoint: str = Field(default="", description="这个模型属于哪个端点（端点 id）")
    model: str = Field(default="", description="服务商的模型名（非空，≤200 字）")
    name: str = Field(default="", description="显示名（≤60 字）；空 = 用模型名")
    efforts: list[str] = Field(default_factory=list, description="支持的思考强度（low / medium / high / xhigh / max 里挑几个，保序；空 = 不支持思考强度）")
    vision: bool = Field(default=False, description="支不支持图片输入")
    context_window: int = Field(default=128000, description="上下文窗口（tokens，8192~2000000）")
    max_tokens: int = Field(default=32768, description="一次回答最多写多少 token（1024~1000000，必须小于上下文窗口）")


class JevEndpointItemConfig(PluginConfigBase):
    """一个 [[jev_endpoints]] 条目（2026-10：多判断服务）。

    用户自己加的一家判断服务：地址 + 密钥（只进不出）+ 协议 + 模型名。
    `preset` 空 = 自定义；填了预设名（jev_presets.PRESETS 的键）就按预设补没填的
    地址 / 模型 / 协议 / 名字。校验在 load_settings 的 _parse_jev_endpoints 里逐条做
    （坏条目丢条目、记问题，不拖垮整段），这里只收留字段。
    """

    __ui_label__ = "判断服务"
    __ui_icon__ = "zap"

    id: str = Field(default="", description="服务 id（小写字母/数字/_/-，1~32 个字符，不能重复）")
    name: str = Field(default="", description="显示名（≤40 字）；空 = 用预设名或 id")
    preset: str = Field(default="", description="内置预设 id；空 = 自定义")
    protocol: str = Field(default="systemone", description="协议：systemone / openai_decisions")
    url: str = Field(default="", description="请求地址，https:// 开头（本机地址可以 http://）")
    model: str = Field(default="", description="模型名（非空，≤200 字）")
    api_key: str = Field(default="", description="服务密钥；只进不出，不写日志")


class JevSectionConfig(PluginConfigBase):
    __ui_label__ = "快速判断"
    __ui_icon__ = "zap"
    __ui_order__ = 8

    enabled: bool = Field(default=True, description="是否用 Jev 快速判断；关掉则都走主模型慢路径")
    timeout_ms: int = Field(default=1200, description="Jev 单次判断超时（毫秒，200~1200；钩子阻塞管线，不能等太久）")
    api_key: str = Field(default="", description="Jev 密钥；优先级低于环境变量和网页存的密钥；只进不出，不写日志")
    key_file: str = Field(default="", description="Jev 密钥文件路径；空 = ~/.typesafe_key；只读取，不写日志、不入库")
    api_url: str = Field(default="https://api.typesafe.ai/v1/systemone", description="Jev 服务端地址")
    model: str = Field(default="jev-1.13.0", description="Jev 模型名")
    use: str = Field(default="typesafe", description='现在用哪个判断服务：内置 "typesafe"（= 上面的 api_url/api_key/model）或 [[jev_endpoints]] 的 id')


class QuickJudgeSectionConfig(PluginConfigBase):
    """[quick_judge] 派活判断兜底（2026-10）。

    Jev 没配 / 连不上 / 拿不准时，把群里 @ 的消息攒一小批，用主模型快速判一次
    「是不是派活」。判不准的仍然回落 pending_asks（主模型读群时再判）。
    """

    __ui_label__ = "派活判断兜底"
    __ui_icon__ = "zap"
    __ui_order__ = 8

    enabled: bool = Field(default=True, description="没配 Jev 或 Jev 拿不准时，用模型快速判断群里 @ 的是不是派活")
    model: str = Field(default="", description='用哪个模型判断（[[model_list]] 的条目 id）；空 = 跟主模型用同一条链；填的条目不存在也用主模型的链')
    keyword_filter: bool = Field(default=False, description="先用请求词过一遍：没有「帮我、整理、查一下、提醒我」这类词的 @ 直接当闲聊，不花模型钱；可能漏掉说法含糊的派活")
    daily_max: int = Field(default=30, description="每个群每天最多判几次（0 = 一次都不用模型，全走老慢路径）")
    batch_wait_s: int = Field(default=15, description="第一条 @ 进来后等几秒，把这段时间里的 @ 合成一次判断（攒够 6 条就早点判）")


class UsageSectionConfig(PluginConfigBase):
    __ui_label__ = "用量提醒"
    __ui_icon__ = "bar-chart"
    __ui_order__ = 9

    alert_daily_tokens: int = Field(default=0, description="一天 token 超过这个数就提醒；0=不提醒；只提醒不暂停")
    alert_task_tokens: int = Field(default=0, description="单个任务 token 超过这个数就提醒；0=不提醒")


class TasksSectionConfig(PluginConfigBase):
    __ui_label__ = "任务上限"
    __ui_icon__ = "shield"
    __ui_order__ = 9

    token_limit: int = Field(default=2_000_000, description="单个任务累计 token 超过这个数就自动暂停（0 = 不限）")
    run_seconds: int = Field(default=10800, description="单个任务开工满这么多秒就自动暂停（0 = 不限）")


class ConsoleSectionConfig(PluginConfigBase):
    __ui_label__ = "网页"
    __ui_icon__ = "monitor"
    __ui_order__ = 10

    listen: str = Field(default="127.0.0.1:18650", description="网页监听地址，必须为 IP:端口")
    password: str = Field(default="", description="管理员密码；空则首次启动自动生成")
    public_url: str = Field(default="", description="网页对外的地址（如 https://maiwork.example.com）；用于拼群链接")
    update_check: bool = Field(default=True, description="管理员打开网页时顺手查 GitHub 上有没有 MaiWork 新版（只提醒，不自动更新）")
    maibot_webui_url: str = Field(default="", description="MaiBot 自己网页的地址；填了，更新提醒里的按钮直接跳过去")


class SSHServerConfig(PluginConfigBase):
    """一台可用的远程执行环境。"""

    __ui_label__ = "自有机器"
    __ui_icon__ = "server"

    name: str = Field(default="", description="这台机器的名字")
    host: str = Field(default="", description="地址，如 user@1.2.3.4:22")
    note: str = Field(default="", description="备注")


class EnvironmentsSectionConfig(PluginConfigBase):
    __ui_label__ = "干活机器"
    __ui_icon__ = "server"
    __ui_order__ = 11

    railway: bool = Field(default=True, description="是否允许用 Railway 临时 VM 跑活")
    ssh: list[SSHServerConfig] = Field(default_factory=list, description="已有的专用 VM/VPS（按顺序优先用）")
    workspace_root: str = Field(default="/home/maiwork/workspaces", description="工作区根目录；每个工作区是它下面的一个子目录")
    memory_max: str = Field(default="512M", description="单个命令的内存上限，如 512M / 1G")
    runtime_max_sec: int = Field(default=1800, description="单条命令最长跑多少秒")
    local_mode: str = Field(
        default="systemd",
        description=(
            "本机执行方式，只支持 \"systemd\"（隔离）。\"direct\" 不给任何隔离、生产一律按 systemd："
            "只有本机开发测试显式设了环境变量 MAIWORK_DEV_ALLOW_DIRECT=1 且不是 root 时才生效"
        ),
    )
    run_as: str = Field(default="maiwork", description="本机命令以哪个系统用户身份跑（systemd 模式的 --uid/--gid）")
    max_parallel: int = Field(default=2, description="每个工作区同时跑的子 agent 个数")
    command_timeout_s: int = Field(default=300, description="子 agent 单条命令的默认时长上限（秒）")
    railway_daily_max: int = Field(default=2, description="Railway 一次性 VM 每天最多用几台（同一出口 IP 每天最多 3 台，留 1 台给派活）")
    verify_enabled: bool = Field(default=False, description="备资讯时挑几条上 Railway 一次性 VM 实测（默认关）")
    verify_per_round: int = Field(default=2, description="每轮备资讯最多实测几条（同一台 VM 里依次测）")
    verify_minutes: int = Field(default=10, description="单条资讯/文章实测的时长上限（分钟）")


class ProfileSectionConfig(PluginConfigBase):
    __ui_label__ = "群画像"
    __ui_icon__ = "book"
    __ui_order__ = 12

    batch_messages: int = Field(default=80, description="攒够多少条新消息就交主模型提炼一次")
    max_interval_hours: int = Field(default=3, description="距上次提炼最多隔几个小时（且至少 5 条新消息）就提炼")
    backfill_days: int = Field(default=7, description="第一次建画像往回读多少天的消息")
    backfill_max_messages: int = Field(default=1500, description="第一次建画像最多读多少条消息")
    weekly_day: int = Field(default=0, description="每周哪一天整体整理画像；0=周一，6=周日")
    read_interval_minutes: int = Field(default=10, description="后台每隔多少分钟增量读一次新消息")


class StorageSectionConfig(PluginConfigBase):
    __ui_label__ = "数据"
    __ui_icon__ = "database"
    __ui_order__ = 13

    data_dir: str = Field(default="", description="数据目录；空 = <MaiBot>/data/maiwork（插件目录上两级下的 data/maiwork）")


class McpExtensionConfig(PluginConfigBase):
    """一个 MCP 扩展（0.3.6；docs/02 §10：MaiWork 自己加载，不挂 MaiBot planner）。"""

    __ui_label__ = "MCP 扩展"
    __ui_icon__ = "plug"

    name: str = Field(default="", description="这个扩展的名字；只用字母、数字、下划线、横线，1~32 个字符")
    url: str = Field(default="", description="MCP 端点地址（Streamable HTTP），必须 https:// 开头")
    enabled: bool = Field(default=True, description="是否启用这个扩展；关掉就不连接、不注册工具")
    headers: dict[str, str] = Field(default_factory=dict, description="额外请求头（密钥放这里）；只由代码读取，不回显、不写日志")
    tools: list[str] = Field(default_factory=list, description="工具白名单（远端工具名）；空 = 全部都注册")
    roles: list[str] = Field(default_factory=lambda: ["worker"], description='谁能用这些工具："worker"（默认，子 agent）可含 "main"（主模型）')
    timeout_s: int = Field(default=20, description="单次请求超时（秒）")


class ExtensionsSectionConfig(PluginConfigBase):
    __ui_label__ = "工具"
    __ui_icon__ = "puzzle"
    __ui_order__ = 14

    mcp: list[McpExtensionConfig] = Field(default_factory=list, description="MCP 扩展列表（只给 MaiWork 自己的子 agent / 主模型，不挂 MaiBot planner）")


class GroupSpaceSectionConfig(PluginConfigBase):
    __ui_label__ = "群空间"
    __ui_icon__ = "folder"
    __ui_order__ = 15

    enabled: bool = Field(default=True, description="是否开群空间（群文件 / 公告 / 相册）；适配器没开放的接口照样不出现")
    notice_per_day: int = Field(default=1, description="每群每天最多发几条群公告（发之前会先在群里说一句预告）")


class ReaderSectionConfig(PluginConfigBase):
    __ui_label__ = "读网页"
    __ui_icon__ = "globe"
    __ui_order__ = 16

    jina_enabled: bool = Field(default=True, description="打开网页读正文先用 Jina Reader（r.jina.ai）；读不到 / 限流就马上换「抓网页正文」工具，再不行直接打开")
    jina_api_key: str = Field(default="", description="Jina Reader 密钥（可选）：不填每分钟 20 次（按服务器 IP 算），填了 500 次；只进不出，不写日志")


class MaiWorkConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    groups: GroupsSectionConfig = Field(default_factory=GroupsSectionConfig)
    focus: FocusSectionConfig = Field(default_factory=FocusSectionConfig)
    feeds: FeedsSectionConfig = Field(default_factory=FeedsSectionConfig)
    topics: TopicsSectionConfig = Field(default_factory=TopicsSectionConfig)
    delivery: DeliverySectionConfig = Field(default_factory=DeliverySectionConfig)
    approval: ApprovalSectionConfig = Field(default_factory=ApprovalSectionConfig)
    models: ModelsSectionConfig = Field(default_factory=ModelsSectionConfig)
    endpoints: list[EndpointItemConfig] = Field(default_factory=list, description="模型端点列表（2026-10 改版：替代旧 [models] 的单一端点）")
    model_list: list[ModelListItemConfig] = Field(default_factory=list, description="模型库（挂在端点下；各专岗从这里挑模型）")
    jev: JevSectionConfig = Field(default_factory=JevSectionConfig)
    jev_endpoints: list[JevEndpointItemConfig] = Field(default_factory=list, description="自己加的判断服务（[jev] use 选中的那个生效）")
    quick_judge: QuickJudgeSectionConfig = Field(default_factory=QuickJudgeSectionConfig)
    usage: UsageSectionConfig = Field(default_factory=UsageSectionConfig)
    tasks: TasksSectionConfig = Field(default_factory=TasksSectionConfig)
    console: ConsoleSectionConfig = Field(default_factory=ConsoleSectionConfig)
    environments: EnvironmentsSectionConfig = Field(default_factory=EnvironmentsSectionConfig)
    profile: ProfileSectionConfig = Field(default_factory=ProfileSectionConfig)
    storage: StorageSectionConfig = Field(default_factory=StorageSectionConfig)
    extensions: ExtensionsSectionConfig = Field(default_factory=ExtensionsSectionConfig)
    group_space: GroupSpaceSectionConfig = Field(default_factory=GroupSpaceSectionConfig)
    reader: ReaderSectionConfig = Field(default_factory=ReaderSectionConfig)


# ----------------------------------------------------------------------
# Settings：规范化后的只读快照
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class GroupSetting:
    group_id: str
    workspace: str
    platform: str = "qq"  # 见 _parse_groups：qq / telegram（tg 当 telegram 的别名）


@dataclass(frozen=True)
class FocusSetting:
    max_members: int
    personal_profile: bool
    personal_feeds: bool = True
    personal_per_day: int = 3


@dataclass(frozen=True)
class FeedsSetting:
    news_slots: tuple[str, ...]
    news_jitter_minutes: int
    max_items: int
    lookback_days: int
    web_min_avg: float
    pool_min_avg: float
    guides: bool
    collect_minutes: int = 15  # 资讯收集子 agent 每轮时间盒（到点把已找到的交回）
    viz_per_day: int = 3       # 资讯图解每群每天上限（0 = 关），news_viz.py



@dataclass(frozen=True)
class TopicsSetting:
    enabled: bool
    speaker: str
    per_day: int
    min_gap_hours: int
    candidate_ttl_hours: int


@dataclass(frozen=True)
class DeliverySetting:
    push_per_day: int
    quiet_hours: str


@dataclass(frozen=True)
class ApprovalSetting:
    required: bool
    admins: tuple[str, ...]
    exempt_groups: tuple[str, ...]
    exempt_users: tuple[str, ...]
    remind: bool = True
    # 自动审核（auto_review.py）：低风险轻活由主模型判断后直接开工；
    # auto_review_daily 是每群每天上限（0 = 关）
    auto_review: bool = True
    auto_review_daily: int = 5


@dataclass(frozen=True)
class ModelsSetting:
    base_url: str
    api_key: str
    main: str
    main_backup: str
    worker: str
    worker_backup: str
    retries: int = 5
    retry_delay_s: int = 10
    max_concurrency: int = 2
    max_rpm: int = 0
    context_window: int = 128000  # 模型上下文窗口（tokens），0.4.0 起用于上下文压缩
    max_tokens: int = 32768  # 一次回答最多写多少 token，0.4.2 起每次模型调用都带上


# 思考强度的合法取值（保序放在 [[model_list]] efforts 里；空 = 不支持思考强度）
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

ENDPOINT_PROTOCOLS: tuple[str, ...] = ("openai", "anthropic", "responses")

_ID_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


# 端点请求头覆盖（高级设置）：可打印 ASCII 头名，禁传/代理头（RFC 7230 的
# connection-specific 传输字段 + 隧道验证类头不该让管理员填，会破坏传输层或经代理走错路）。
_BLOCKED_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "host", "content-length", "transfer-encoding", "connection",
        "keep-alive", "te", "trailer", "upgrade",
        "proxy-authorization", "proxy-authenticate", "proxy-connection", "via",
    }
)

_HEADER_NAME_MAX = 128
_HEADER_VALUE_MAX = 8192
_ENDPOINT_HEADERS_MAX = 32


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def validate_endpoint_header_name(name: Any) -> tuple[str, int]:
    """校验一个端点请求头名（规则同 docs/02 设计；RFC 7230 的 token 语法）。

    回 (中文错误, 违例处下标)；通过 = ("", -1)。错误文案只说规则，不携带头名本身
    （头名走网页「按行加头」时也会进错误提示，不带更保稳）。
    """
    s = str(name or "").strip(" \t")
    if not s:
        return "请求头名不能为空", -1
    if len(s) > _HEADER_NAME_MAX:
        return f"请求头名太长（上限 {_HEADER_NAME_MAX} 个字符）", -1
    if not _HEADER_NAME_RE.fullmatch(s):
        # 仅可打印 ASCII + HTTP token 字符（!#$%&'*+-.^_`|~ 和字母数字）；冒号 / 空格 /
        # 控制字符 / 非 ASCII / 别的标点一律拒
        return (
            "请求头名不合法（只能是 HTTP token 字符：字母数字和 !#$%&'*+-.^_`|~，"
            "不含空格、冒号、非 ASCII）",
            -1,
        )
    if s.lower() in _BLOCKED_HEADER_NAMES:
        return (
            f"请求头 {s} 是传输 / 代理专用头：让它随便盖会破坏 HTTP 传输（分块、长度、连接）"
            "，或经代理走错验证，这类头一律不收",
            -1,
        )
    return "", -1


def validate_endpoint_header_value(value: Any) -> str:
    """校验一个端点请求头值；通过 = ""。允许可打印 ASCII + TAB；禁换行 / 控制 / DEL /
    非 ASCII。错误文案只说规则，绝不携带值（value 常是密钥）。"""
    s = str(value) if value is not None else ""
    if len(s) > _HEADER_VALUE_MAX:
        return f"请求头值太长（上限 {_HEADER_VALUE_MAX} 个字符）"
    for ch in s:
        o = ord(ch)
        if ch == "\t":
            continue
        if o < 0x20 or o > 0x7E:
            return (
                "请求头值含不合法字符（只能用可打印 ASCII 和 TAB；不含换行、控制字符、"
                "DEL、非 ASCII）"
            )
    return ""


def validate_endpoint_headers_pairs(raw: Any, eid_obj: Any) -> tuple[list[tuple[str, str]], list[str]]:
    """把「名字→值」对象收成成对清单（保序）；不合法的条目逐个丢，记中文问题。

    - 名字长度 ≤128、可打印 ASCII（禁空格 / 冒号 / 控制 / 非 ASCII）、传输/代理头拒；
    - 值必须是非空 string（空串 / None / 别的类型 = 丢条目）；
    - 同名大小写重复报错；数量 ≤32。
    错误文案只说规则 + 永远带 headers 标签（不落明文值），成对清单里放
    (规范化名字, 去首尾空白的值)。
    """
    pairs: list[tuple[str, str]] = []
    problems: list[str] = []
    if raw is None:
        return pairs, problems
    eid = str(eid_obj or "").strip() or "?"
    if not isinstance(raw, Mapping):
        problems.append(f'端点 headers 解析：端点 "{eid}" 的 headers 不是表，整份 headers 丢弃')
        return pairs, problems
    for k, v in raw.items():
        err, _ = validate_endpoint_header_name(k)
        if err:
            problems.append(f'端点 headers 解析（"{eid}"）：{err}，这条头丢弃')
            continue
        if not isinstance(v, str):
            problems.append(
                f'端点 headers 解析（"{eid}"）：一个请求头的值不是字符串（类型不对），这条头丢弃'
            )
            continue
        err_v = validate_endpoint_header_value(v)
        if err_v:
            problems.append(f'端点 headers 解析（"{eid}"）：{err_v}，这条头丢弃')
            continue
        pairs.append((str(k).strip(), str(v).strip()))
    # 同名大小写重复整份丢（不让「两个同名不同大小写」蒙混过；网页/配置共用一条规则）
    lowered: dict[str, str] = {}
    duplicate = False
    for name, _ in pairs:
        low = name.lower()
        if low in lowered:
            duplicate = True
        lowered[low] = name
    if duplicate:
        problems.append(
            f'端点 headers 解析（"{eid}"）：请求头同名不同大小写重复（大小写不敏感），整份 headers 丢弃改前请先排开'
        )
        return [], problems
    if len(pairs) > _ENDPOINT_HEADERS_MAX:
        problems.append(
            f'端点 headers 解析（"{eid}"）：请求头个数超了（最多 {_ENDPOINT_HEADERS_MAX} 个），整份 headers 丢弃'
        )
        return [], problems
    return pairs, problems


@dataclass(frozen=True)
class EndpointSetting:
    """一个 [[endpoints]] 规范化后的快照（2026-10 模型改版阶段 1a）。"""

    id: str
    name: str
    protocol: str  # openai / anthropic / responses
    base_url: str
    api_key: str
    retries: int = 5
    retry_delay_s: int = 10
    max_concurrency: int = 2
    max_rpm: int = 0
    # 请求头覆盖（高级设置）：内部 MappingProxyType，外部当 Mapping 用；
    # 名字保留原始大小写（大小写不敏感合并时按它替代默认头），值是凭据（只进不出）
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ModelEntry:
    """一个 [[model_list]] 规范化后的快照（2026-10 模型改版阶段 1a）。"""

    id: str
    endpoint: str       # 端点 id（解析时已确认存在）
    model: str          # 服务商的模型名
    name: str
    efforts: tuple[str, ...] = ()
    vision: bool = False
    context_window: int = 128000
    max_tokens: int = 32768


@dataclass(frozen=True)
class JevEndpointSetting:
    """一个 [[jev_endpoints]] 规范化后的快照（2026-10：多判断服务）。"""

    id: str
    name: str
    preset: str   # "" = 自定义
    protocol: str
    url: str
    model: str
    api_key: str = ""


@dataclass(frozen=True)
class JevSetting:
    enabled: bool
    timeout_ms: int
    key_file: str
    api_url: str
    model: str
    api_key: str = ""
    # 现在用哪个判断服务："" / "typesafe" = 上面这组内置字段；别的 = jev_endpoints 的 id
    use: str = "typesafe"


@dataclass(frozen=True)
class QuickJudgeSetting:
    """[quick_judge] 规范化后的快照（2026-10：派活判断兜底）。"""

    enabled: bool = True
    model: str = ""            # [[model_list]] 条目 id；空 = 主模型的链
    keyword_filter: bool = False
    daily_max: int = 30        # 每群每天模型调用次数上限（0 = 不用模型）
    batch_wait_s: int = 15     # 攒批等待秒数（3~120）


@dataclass(frozen=True)
class UsageSetting:
    alert_daily_tokens: int
    alert_task_tokens: int


@dataclass(frozen=True)
class TasksSetting:
    token_limit: int = 2_000_000  # 单任务累计 token 自动暂停线（0 = 不限）
    run_seconds: int = 10800      # 单任务开工时长自动暂停线（秒；0 = 不限）


def _http_url_or_empty(v: object) -> str:
    """只认 http(s):// 开头的地址（去尾部斜杠）；别的一律当没填。"""
    s = str(v or "").strip().rstrip("/")
    return s if re.match(r"^https?://[^\s]+$", s) else ""


@dataclass(frozen=True)
class ConsoleSetting:
    # (host, port)；默认 ("127.0.0.1", 18650)
    listen: tuple[str, int]
    password: str
    public_url: str
    update_check: bool = True
    maibot_webui_url: str = ""


@dataclass(frozen=True)
class SSHServer:
    name: str
    host: str
    note: str


@dataclass(frozen=True)
class EnvironmentsSetting:
    railway: bool
    ssh: tuple[SSHServer, ...]
    workspace_root: Path
    memory_max: str
    runtime_max_sec: int
    local_mode: str  # "systemd" | "direct"（direct 无隔离，只在本机开发开关下才可能留下）
    run_as: str
    max_parallel: int
    command_timeout_s: int
    railway_daily_max: int   # Railway 一次性 VM 每天最多用几台（官方同一出口 IP 最多 3 台/天）
    verify_enabled: bool    # 备资讯时要不要挑几条上 Railway 一次性 VM 实测（默认关）
    verify_per_round: int    # 每轮备资讯最多实测几条
    verify_minutes: int      # 单条实测的时长上限（分钟）


@dataclass(frozen=True)
class ProfileSetting:
    batch_messages: int
    max_interval_hours: int
    backfill_days: int
    backfill_max_messages: int
    weekly_day: int
    read_interval_minutes: int


@dataclass(frozen=True)
class McpExtensionSetting:
    """一个 [[extensions.mcp]] 规范化后的快照。headers 的 MappingProxyType 外部当 dict 用。"""

    name: str
    url: str
    enabled: bool
    headers: Mapping[str, str]
    tools: tuple[str, ...]      # 白名单；空 = 全部
    roles: tuple[str, ...]      # ("worker",) 默认，可含 "main"
    timeout_s: int


@dataclass(frozen=True)
class ExtensionsSetting:
    mcp: tuple[McpExtensionSetting, ...]


@dataclass(frozen=True)
class GroupSpaceSetting:
    enabled: bool
    notice_per_day: int


@dataclass(frozen=True)
class ReaderSetting:
    jina_enabled: bool = True
    jina_api_key: str = ""


@dataclass(frozen=True)
class Settings:
    """规范化后的只读配置快照。groups 键是纯数字群号字符串。"""

    enabled: bool
    groups: Mapping[str, GroupSetting]  # 内部放 MappingProxyType，外部当 dict 用
    data_dir: Path
    workspace_root: Path
    focus: FocusSetting
    feeds: FeedsSetting
    topics: TopicsSetting
    delivery: DeliverySetting
    approval: ApprovalSetting
    models: ModelsSetting
    jev: JevSetting
    usage: UsageSetting
    console: ConsoleSetting
    environments: EnvironmentsSetting
    profile: ProfileSetting
    # 2026-10 模型改版阶段 1a：端点 + 模型库（替代旧 [models] 的单一端点/四槽）
    endpoints: tuple[EndpointSetting, ...] = ()
    model_list: tuple[ModelEntry, ...] = ()
    # 2026-10 多判断服务：自己加的判断服务（[jev] use 选中的那个生效）
    jev_endpoints: tuple[JevEndpointSetting, ...] = ()
    # 2026-10 派活判断兜底（Jev 不在 / 拿不准时用模型快速判）
    quick_judge: QuickJudgeSetting = QuickJudgeSetting()
    tasks: TasksSetting = TasksSetting()
    extensions: ExtensionsSetting = ExtensionsSetting(mcp=())
    group_space: GroupSpaceSetting = GroupSpaceSetting(enabled=True, notice_per_day=1)
    reader: ReaderSetting = ReaderSetting()
    # 内部派生运行时字段（**不是** SDK / config.toml 可编辑键，不参与 schema / config_version）：
    # 群控归一（全局旧键 → 每群一份）这一轮是否已经「物化成功 + 有效重读成功」。
    # True（缺省）= 老合同：直接拿 load_settings 结果的数据层调用照旧惰性播种；
    # App 启动时先设 False，只有 migrate_group_controls 真跑成功才开（见
    # app._open_group_controls_seed_gate）。门关着时 GroupApprovals / group_push
    # 读**已有**合法记录照旧尊重，但缺失记录只回安全默认、绝不播种。
    group_controls_seed_ready: bool = True
    problems: tuple[str, ...] = ()

    def is_served(self, group_id: str) -> bool:
        return isinstance(group_id, str) and group_id in self.groups

    def platform_of(self, group_id: str) -> str:
        """服务群所在平台「qq」/「telegram」；不认识的群返回 "qq"（老行为）。"""
        g = self.groups.get(group_id) if isinstance(group_id, str) else None
        return str(getattr(g, "platform", "") or "qq") if g else "qq"

    def workspace_of(self, group_id: str) -> str:
        """没配就是 "g<群号>"。"""
        g = self.groups.get(group_id)
        return g.workspace if g else f"g{group_id}"


# ----------------------------------------------------------------------
# load_settings
# ----------------------------------------------------------------------

_LISTEN_RE = re.compile(r"^(?P<host>(?:\d{1,3}\.){3}\d{1,3}):(?P<port>\d{1,5})$")
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Telegram 虚拟群 ID（docs/06）：普通群是负数 chat_id（如 "-1001234567890"），
# 话题群形如 "<chat_id>::tg-topic::mt=<id>"，所以允许 :、=、_、|、.、-、字母数字。
_TG_GROUP_ID_RE = re.compile(r"^[-0-9A-Za-z:=_|.]{1,64}$")
# QQ 官方机器人的群 ID 是 group_openid（字母数字串，docs/06）；纯数字的是 SnowLuma 群号，不收
_QQBOT_GROUP_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _default_data_dir() -> Path:
    return _PLUGIN_DIR.parent.parent / "data" / "maiwork"


def _sanitize_workspace_name(raw: str) -> str:
    """Telegram 群 ID 可能带 :、= 等 _WORKSPACE_RE 不允许的字符 → 换成 '_'。
    保证默认工作区名能过 _WORKSPACE_RE（也仅用于默认生成，用户手填的工作区名
    有自己的白名单校验）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or "")) or "g"


def _default_workspace(gid: str, platform: str) -> str:
    """默认工作区名：QQ 群保持原样 g<群号>；Telegram 群把不合法字符清洗后拼 g。"""
    gid_s = str(gid or "").strip()
    if platform == "qq":
        return f"g{gid_s}"
    return f"g{_sanitize_workspace_name(gid_s)}"


def parse_serve_group(grp: str) -> tuple[str, str]:
    """一条服务群写法 → (平台, 群 ID)。写错抛 ValueError（中文原因）。
    config.toml 和网页设置共用这一套规则：
    - "qq:纯数字"；
    - "telegram:群 ID" / "tg:群 ID"（负数 chat_id，或话题群 "<chat_id>::tg-topic::mt=<id>"）；
    - "qqbot:群 openid"（QQ 官方机器人；群 ID 是 openid，不能是纯数字群号）。"""
    grp = str(grp or "").strip()
    if not grp:
        raise ValueError("服务群条目群号为空")
    if ":" not in grp:
        raise ValueError(f'服务群 "{grp}" 缺平台前缀（要写成 "qq:号码"、"telegram:群 ID" 或 "qqbot:群 openid"）')
    platform_raw, _, raw_id = grp.partition(":")
    platform = _norm_platform(platform_raw)
    gid = raw_id.strip()
    if platform == "qq":
        if not gid.isdigit():
            raise ValueError(f'服务群 "{grp}" 的 qq 群号不是纯数字')
    elif platform == "telegram":
        if not gid or not _TG_GROUP_ID_RE.match(gid):
            raise ValueError(
                f'服务群 "{grp}" 的 telegram 群 ID 不合法（只能是字母、数字、横线、下划线、冒号、等号、竖线、点，长度≤64）'
            )
    elif platform == "qqbot":
        if not gid or not _QQBOT_GROUP_ID_RE.match(gid):
            raise ValueError(f'服务群 "{grp}" 的 QQ 官方群 ID 不合法（是 openid：字母、数字、横线、下划线，长度≤64）')
        if gid.isdigit():
            raise ValueError(f'服务群 "{grp}" 是纯数字：QQ 官方机器人的群 ID 是 openid；普通 QQ 群请写 "qq:{gid}"')
    else:
        raise ValueError(f'服务群 "{grp}" 是暂不支持的 "{platform}" 平台（目前只支持 qq / telegram / qqbot）')
    return platform, gid


def _parse_groups(raw_sections: Mapping[str, Any], problems: list[str]) -> dict[str, GroupSetting]:
    groups: dict[str, GroupSetting] = {}
    serve = raw_sections.get("groups", {}).get("serve", [])
    if not isinstance(serve, list):
        problems.append("[groups] serve 必须是列表，本次忽略")
        return groups
    for item in serve:
        try:
            if isinstance(item, ServeGroupConfig):
                grp, ws = item.group, item.workspace
            elif isinstance(item, Mapping):
                grp, ws = item.get("group", ""), item.get("workspace", "")
            else:
                problems.append(f"服务群条目不是表：{item!r}，已丢弃")
                continue
            grp = str(grp or "").strip()
            if not grp:
                problems.append("服务群条目群号为空，已丢弃")
                continue
            try:
                platform, gid = parse_serve_group(grp)
            except ValueError as e:
                problems.append(f"{e}，已丢弃")
                continue
            if gid in groups:
                problems.append(f'服务群 "{grp}" 重复出现，只保留第一个，后一个已丢弃')
                continue
            if ws is None or str(ws).strip() == "":
                ws_str = _default_workspace(gid, platform)
            else:
                ws_str = str(ws).strip()
            # M11：workspace 名将直接成为目录名拼进 shell 命令，白名单之外的一律
            # 丢这条群配置（不拖垮整节）并记问题
            if not _WORKSPACE_RE.match(ws_str):
                problems.append(
                    f'服务群 "{grp}" 的工作区名 "{ws_str}" 不合法'
                    "（只能用字母、数字、下划线、横线，1~64 个字符），此群配置已丢弃"
                )
                continue
            groups[gid] = GroupSetting(group_id=gid, workspace=ws_str, platform=platform)
        except Exception as e:  # 任何意外都吞掉，记问题
            problems.append(f"服务群条目解析出错（{e}），已丢弃")
    # G6：多个群同一个 workspace（共享工作区），子 agent 能看到所有这些群的画像，
    # 是设计允许的共享，但要在问题清单里点一句，免得管理员以为画像互不可见
    by_ws: dict[str, list[str]] = {}
    for gid, gsetting in groups.items():
        by_ws.setdefault(gsetting.workspace, []).append(gid)
    for ws_name, gids in by_ws.items():
        if len(gids) > 1:
            problems.append(
                f'工作区 "{ws_name}" 被 {len(gids)} 个群（{"、".join(sorted(gids))}）共用：'
                "共享工作区的群会互相看到群画像"
            )
    return groups


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def normalize_domain(raw: Any) -> str:
    """域名规范化：小写、去 www. 前缀、去首尾空白；明显不合法 → 空串。

    每群屏蔽名单（kv["feeds.blocked.<gid>"]）、/api/groups/{gid}/feeds/domains 入参都走这里。
    """
    s = str(raw or "").strip().lower()
    if s.startswith("www."):
        s = s[4:]
    if not _DOMAIN_RE.match(s):
        return ""
    return s


def _parse_feeds(feeds: FeedsSectionConfig, problems: list[str]) -> FeedsSetting:
    """[feeds] 节规范化。

    - web_min_avg / pool_min_avg 夹到 1~5。
    -（2026-10：屏蔽名单挪成按群 kv，blocked_domains 不再是配置项；存量的随启动
      migrate_blocked_domains_to_groups 迁进每个群的 kv，文件里的键删掉。）
    """

    def _clamp_score(name: str, value: float, default: float) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            problems.append(f"[feeds] {name} = {value!r} 不是数字，本次按 {default} 处理")
            return default
        if not 1.0 <= v <= 5.0:
            vv = max(1.0, min(5.0, v))
            problems.append(f"[feeds] {name} = {v} 超出 1~5 的范围，已按 {vv} 处理")
            return vv
        return v

    return FeedsSetting(
        news_slots=tuple(str(s) for s in feeds.news_slots),
        news_jitter_minutes=int(feeds.news_jitter_minutes),
        max_items=max(1, int(feeds.max_items)),
        lookback_days=max(1, int(feeds.lookback_days)),
        web_min_avg=_clamp_score("web_min_avg", feeds.web_min_avg, 3.0),
        pool_min_avg=_clamp_score("pool_min_avg", feeds.pool_min_avg, 4.0),
        guides=bool(feeds.guides),
        collect_minutes=max(1, min(60, _clamp_int(getattr(feeds, "collect_minutes", 15), 1, 60, 15))),
        viz_per_day=_clamp_int(getattr(feeds, "viz_per_day", 3), 0, 10, 3),
    )


def _parse_jev(jev: JevSectionConfig, problems: list[str]) -> JevSetting:
    """[jev] 节规范化。

    timeout_ms 夹到 [200, 1200]（G5）：收消息钩子等 Jev 是阻塞宿主管线的，
    上限 1200 毫秒保体验；下限 200 毫秒避免配太小导致永远超时。越界记问题。
    """
    raw_timeout = int(jev.timeout_ms)
    timeout = max(200, min(1200, raw_timeout))
    if timeout != raw_timeout:
        problems.append(f"[jev] timeout_ms = {raw_timeout} 超出 200~1200 毫秒范围，已按 {timeout} 处理")
    return JevSetting(
        enabled=bool(jev.enabled),
        timeout_ms=timeout,
        key_file=str(jev.key_file or "").strip(),
        api_url=str(jev.api_url or "").strip() or "https://api.typesafe.ai/v1/systemone",
        model=str(jev.model or "").strip() or "jev-1.13.0",
        api_key=str(getattr(jev, "api_key", "") or "").strip(),
        use=str(getattr(jev, "use", "") or "").strip() or "typesafe",
    )


def _parse_quick_judge(qj: QuickJudgeSectionConfig, problems: list[str]) -> QuickJudgeSetting:
    """[quick_judge] 节规范化：数值夹到合法区间并记中文问题，坏值不抛。"""
    raw_daily = int(getattr(qj, "daily_max", 30))
    daily = max(0, min(500, raw_daily))
    if daily != raw_daily:
        problems.append(f"[quick_judge] daily_max = {raw_daily} 超出 0~500 范围，已按 {daily} 处理")
    raw_wait = int(getattr(qj, "batch_wait_s", 15))
    wait = max(3, min(120, raw_wait))
    if wait != raw_wait:
        problems.append(f"[quick_judge] batch_wait_s = {raw_wait} 超出 3~120 秒范围，已按 {wait} 处理")
    return QuickJudgeSetting(
        enabled=bool(getattr(qj, "enabled", True)),
        model=str(getattr(qj, "model", "") or "").strip(),
        keyword_filter=bool(getattr(qj, "keyword_filter", False)),
        daily_max=daily,
        batch_wait_s=wait,
    )


# 判断服务 id：小写字母开头，字母/数字/_/-，1~32 个字（比 [[endpoints]] 宽松一点，
# 因为预设 id 里可能有下划线组合）；"typesafe" 是内置那个，不许占。
_JEV_ENDPOINT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def jev_endpoint_url_problem(url: str) -> str:
    """地址检查：返回中文问题；"" 表示合法。

    - 还带 ``{...}`` 占位（Cloudflare 预设的 ``{account_id}``）→ 提醒换掉；
    - ``https://`` 一律可以；
    - ``http://`` 只允许本机（localhost / 127.0.0.1 / ::1）：密钥跟着请求走，外网
      明文等于泄露；本机自己跑的开源判断模型没有这个顾虑。
    """
    if "{" in url or "}" in url:
        return "地址里还有没替换的占位（比如 {account_id}），换成你自己的"
    if url.startswith("https://"):
        return ""
    if url.startswith("http://"):
        rest = url[len("http://"):].split("/", 1)[0].strip()
        rest = rest.rsplit("@", 1)[-1]
        if rest.startswith("["):  # IPv6 字面量 [::1]:port
            host = rest[1:rest.find("]")] if "]" in rest else rest
        else:
            host = rest.split(":", 1)[0]
        if host in ("localhost", "127.0.0.1", "::1"):
            return ""
        return "外网地址必须是 https://（密钥走这个请求，明文 http 会泄露）；本机地址可以用 http://"
    return "地址必须是 https:// 开头的网址（本机地址可以 http://）"


def _parse_jev_endpoints(raw: Any, problems: list[str]) -> tuple[JevEndpointSetting, ...]:
    """[[jev_endpoints]] 逐条规范化：坏条目丢弃记中文问题，绝不抛异常。

    填了 `preset` 的条目，没填的协议 / 地址 / 模型名 / 显示名从预设补齐。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("[[jev_endpoints]] 必须是表数组，本次整段忽略")
        return ()
    from .jev_presets import PRESETS, PROTOCOLS

    out: list[JevEndpointSetting] = []
    seen: set[str] = set()
    for item in raw:
        try:
            entry = _as_item_mapping(item, JevEndpointItemConfig)
            if entry is None:
                problems.append(f"判断服务条目不是表：{str(item)[:60]}，已丢弃")
                continue
            eid = str(entry.get("id") or "").strip()
            if not _JEV_ENDPOINT_ID_RE.match(eid):
                problems.append(
                    f'判断服务 id "{eid or str(entry.get("id"))}" 不合法（只能用小写字母、数字、_、-，1~32 个字符），此条已丢弃'
                )
                continue
            if eid == "typesafe":
                problems.append('判断服务 id "typesafe" 是内置的，不能占用，此条已丢弃')
                continue
            if eid in seen:
                problems.append(f'判断服务 id "{eid}" 重复出现，只保留第一个，后一个已丢弃')
                continue
            preset = str(entry.get("preset") or "").strip()
            preset_obj = None
            if preset:
                preset_obj = PRESETS.get(preset)
                if preset_obj is None:
                    problems.append(f'判断服务 "{eid}" 的预设 "{preset}" 不认识，按自定义处理')
                    preset = ""
            protocol = str(entry.get("protocol") or "").strip().lower()
            if not protocol and preset_obj is not None:
                protocol = preset_obj.protocol
            if not protocol:
                protocol = "systemone"
            if protocol not in PROTOCOLS:
                problems.append(
                    f'判断服务 "{eid}" 的协议 "{protocol}" 不认识（只能是 systemone / openai_decisions），此条已丢弃'
                )
                continue
            url = str(entry.get("url") or "").strip().rstrip("/")
            if not url and preset_obj is not None:
                url = preset_obj.url.rstrip("/")
            url_problem = jev_endpoint_url_problem(url)
            if url_problem:
                problems.append(f'判断服务 "{eid}"：{url_problem}，此条已丢弃')
                continue
            model = str(entry.get("model") or "").strip()
            if not model and preset_obj is not None:
                model = preset_obj.model
            if not model:
                problems.append(f'判断服务 "{eid}" 的模型名不能为空，此条已丢弃')
                continue
            if len(model) > 200:
                problems.append(f'判断服务 "{eid}" 的模型名太长（上限 200 字），此条已丢弃')
                continue
            name = str(entry.get("name") or "").strip()
            if not name and preset_obj is not None:
                name = preset_obj.name
            if len(name) > 40:
                problems.append(f'判断服务 "{eid}" 的名字太长（上限 40 字），此条已丢弃')
                continue
            if not name:
                name = eid
            out.append(
                JevEndpointSetting(
                    id=eid, name=name, preset=preset, protocol=protocol,
                    url=url, model=model, api_key=str(entry.get("api_key") or "").strip(),
                )
            )
            seen.add(eid)
        except Exception as e:
            label = ""
            try:
                label = str(item.get("id") or "")  # type: ignore[union-attr]
            except Exception:
                label = ""
            where = f"「{label}」" if label else ""
            problems.append(f"判断服务条目{where}解析出错（{e}），已丢弃")
    return tuple(out)


def _running_as_root() -> bool:
    """当前进程是不是 root（Linux/Mac；没有 geteuid 的平台按不是 root 处理）。"""
    try:
        import os

        return os.geteuid() == 0
    except (AttributeError, OSError):  # pragma: no cover - 非 POSIX 平台没有 geteuid
        return False


# 显式开发开关（不新增配置项）：只有本机开发测试才该有「无隔离的 direct」。
# 生产路径（systemd 服务/容器）里进程环境变量不会带它 → 配置里写 direct 一律按 systemd。
DEV_ALLOW_DIRECT_ENV = "MAIWORK_DEV_ALLOW_DIRECT"


def _dev_allow_direct() -> bool:
    """进程环境变量 MAIWORK_DEV_ALLOW_DIRECT=1 才算开了开发开关。"""
    import os

    return str(os.environ.get(DEV_ALLOW_DIRECT_ENV) or "").strip() == "1"


def _parse_environments(
    env: EnvironmentsSectionConfig, workspace_root: Path, problems: list[str]
) -> EnvironmentsSetting:
    """[environments] 节规范化。

    local_mode 只允许 "systemd" / "direct"；写错回落 systemd（线上必须隔离），并记中文问题。
    direct **默认不生效**：它不给子 agent 任何隔离，生产路径一律按 systemd，并记中文问题；
    只有本机开发测试显式设了环境变量 MAIWORK_DEV_ALLOW_DIRECT=1、且进程不是 root 时才允许
    （G1：插件以 root 跑时 direct = 子 agent 直接拿 root shell，即使开了开关也回落 systemd）。
    """
    local_mode = str(env.local_mode or "").strip().lower()
    if local_mode not in ("systemd", "direct"):
        problems.append(
            f'[environments] local_mode = "{local_mode}" 不认识（只能是 systemd / direct），本次按 systemd 处理'
        )
        local_mode = "systemd"
    if local_mode == "direct" and not _dev_allow_direct():
        problems.append(
            '[environments] local_mode = "direct" 不生效（它没有任何隔离，生产一律按 systemd）：'
            "只有本机开发测试显式设了环境变量 MAIWORK_DEV_ALLOW_DIRECT=1 才允许，本次按 systemd 处理"
        )
        local_mode = "systemd"
    if local_mode == "direct" and _running_as_root():
        problems.append("以 root 运行时不允许 direct（子 agent 会直接拿到 root 权限），本次按 systemd 处理")
        local_mode = "systemd"
    return EnvironmentsSetting(
        railway=bool(env.railway),
        ssh=tuple(SSHServer(name=str(s.name or ""), host=str(s.host or ""), note=str(s.note or "")) for s in env.ssh),
        workspace_root=workspace_root,
        memory_max=str(env.memory_max or "512M"),
        runtime_max_sec=int(env.runtime_max_sec),
        local_mode=local_mode,
        run_as=str(env.run_as or "").strip() or "maiwork",
        max_parallel=max(1, int(env.max_parallel)),
        command_timeout_s=max(1, int(env.command_timeout_s)),
        railway_daily_max=max(1, int(env.railway_daily_max)),
        verify_enabled=bool(env.verify_enabled),
        verify_per_round=max(1, min(3, int(env.verify_per_round))),
        verify_minutes=max(1, int(env.verify_minutes)),
    )


def _parse_listen(console: ConsoleSectionConfig, problems: list[str]) -> tuple[str, int]:
    raw = str(console.listen or "").strip()
    m = _LISTEN_RE.match(raw)
    port = int(m.group("port")) if m else -1
    if m and 1 <= port <= 65535:
        octets = m.group("host").split(".")
        if all(0 <= int(o) <= 255 for o in octets):
            return m.group("host"), port
    problems.append(f'console.listen = "{raw}" 不是合法的 "IP:端口"，本次用默认 127.0.0.1:18650')
    return "127.0.0.1", 18650


_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _parse_extensions(raw_extensions: Any, problems: list[str]) -> ExtensionsSetting:
    """[extensions] 节规范化（0.3.6），从原始输入解析（像 _parse_groups 一样逐条容错，
    单条坏了丢单条，不拖垮整节——pydantic 逐条验证做不到这么细）。

    每个 [[extensions.mcp]] 逐条校验：name 不合法 / url 空或非 https:// 的整条丢弃记问题；
    headers 键值一律 str 化（密钥只进不出，由代码读取）；条目不是表 → 丢弃记问题。
    """
    out: list[McpExtensionSetting] = []
    if not isinstance(raw_extensions, Mapping):
        return ExtensionsSetting(mcp=())
    mcp_list = raw_extensions.get("mcp")
    if mcp_list is None:
        return ExtensionsSetting(mcp=())
    if not isinstance(mcp_list, list):
        problems.append("[extensions] mcp 必须是列表，本次忽略")
        return ExtensionsSetting(mcp=())
    for item in mcp_list:
        try:
            if isinstance(item, McpExtensionConfig):
                entry: Mapping[str, Any] = item.model_dump(mode="python")
            elif isinstance(item, Mapping):
                entry = item
            else:
                problems.append(f"[extensions] MCP 扩展条目不是表：{item!r}，已丢弃")
                continue
            name = str(entry.get("name") or "").strip()
            if not _MCP_NAME_RE.match(name):
                problems.append(
                    f'[extensions] MCP 扩展名字 "{name}" 不合法（只能用字母、数字、下划线、横线，1~32 个字符），此条已丢弃'
                )
                continue
            url = str(entry.get("url") or "").strip()
            if not url:
                problems.append(f'[extensions] MCP 扩展 "{name}" 没配 url，此条已丢弃')
                continue
            if not url.startswith("https://"):
                problems.append(f'[extensions] MCP 扩展 "{name}" 的 url 必须 https:// 开头（密钥走这个请求，http 会泄露），此条已丢弃')
                continue
            headers: dict[str, str] = {}
            headers_raw = entry.get("headers")
            if headers_raw is None:
                headers_raw = {}
            if isinstance(headers_raw, Mapping):
                for k, v in headers_raw.items():
                    k_s = str(k or "").strip()
                    if not k_s:
                        continue
                    headers[k_s] = str(v)
            else:
                problems.append(f'[extensions] MCP 扩展 "{name}" 的 headers 不是表，本次按没有处理')
            tools: list[str] = []
            tools_raw = entry.get("tools")
            if tools_raw is None:
                tools_raw = []
            if isinstance(tools_raw, (list, tuple)):
                for t in tools_raw:
                    t_s = str(t or "").strip()
                    if t_s and t_s not in tools:
                        tools.append(t_s)
            else:
                problems.append(f'[extensions] MCP 扩展 "{name}" 的 tools 不是列表，本次按全部处理')
            roles: list[str] = []
            roles_raw = entry.get("roles")
            if roles_raw is None:
                roles_raw = []
            if isinstance(roles_raw, (list, tuple)):
                roles = sorted({str(r).strip() for r in roles_raw if str(r).strip() in ("worker", "main")})
            elif roles_raw:
                problems.append(f'[extensions] MCP 扩展 "{name}" 的 roles 不是列表，本次按 worker 处理')
            if not roles:
                roles = ["worker"]
            try:
                timeout_s = max(1, int(entry.get("timeout_s") or 20))
            except (TypeError, ValueError):
                timeout_s = 20
            out.append(
                McpExtensionSetting(
                    name=name,
                    url=url,
                    enabled=bool(entry.get("enabled", True)),
                    headers=MappingProxyType(headers),
                    tools=tuple(tools),
                    roles=tuple(roles),
                    timeout_s=timeout_s,
                )
            )
        except Exception as e:  # 任何意外都吞掉，记问题（配置写错不能让插件加载失败）
            problems.append(f"[extensions] MCP 扩展条目解析出错（{e}），已丢弃")
    return ExtensionsSetting(mcp=tuple(out))


def _require_int(value: Any, low: int, high: int, what: str) -> int:
    """[[endpoints]]/[[model_list]] 里的整数：bool / 非整数 / 越界一律 ValueError（中文）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} 要是 {low}~{high} 的整数")
    if not low <= value <= high:
        raise ValueError(f"{what} = {value} 超出 {low}~{high} 的范围")
    return value


def _as_item_mapping(item: Any, cls: type[PluginConfigBase]) -> Mapping[str, Any] | None:
    """[[endpoints]]/[[model_list]] 单条一律成映射；不是表 → None（调用方丢条目记问题）。"""
    if isinstance(item, cls):
        return item.model_dump(mode="python")
    if isinstance(item, Mapping):
        return item
    return None


def _parse_endpoints(raw: Any, problems: list[str]) -> tuple[EndpointSetting, ...]:
    """[[endpoints]] 逐条规范化：坏条目丢弃记中文问题，绝不抛异常。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("[[endpoints]] 必须是表数组，本次整段忽略")
        return ()
    out: list[EndpointSetting] = []
    seen: set[str] = set()
    for item in raw:
        try:
            entry = _as_item_mapping(item, EndpointItemConfig)
            if entry is None:
                problems.append(f"端点条目不是表：{str(item)[:60]}，已丢弃")
                continue
            eid = str(entry.get("id") or "").strip()
            if not _ID_RE.match(eid):
                problems.append(
                    f'端点 id "{eid or str(entry.get("id"))}" 不合法（只能用小写字母、数字、_、-，1~24 个字符），此条已丢弃'
                )
                continue
            if eid in seen:
                problems.append(f'端点 id "{eid}" 重复出现，只保留第一个，后一个已丢弃')
                continue
            protocol = str(entry.get("protocol") or "openai").strip().lower()
            if protocol not in ENDPOINT_PROTOCOLS:
                problems.append(
                    f'端点 "{eid}" 的协议 "{protocol}" 不认识（只能是 openai / anthropic / responses），此条已丢弃'
                )
                continue
            base_url = str(entry.get("base_url") or "").strip().rstrip("/")
            if not re.match(r"^https?://[^\s]+$", base_url):
                problems.append(f'端点 "{eid}" 的地址必须是 http(s) 开头的网址，此条已丢弃')
                continue
            name = str(entry.get("name") or "").strip()
            if len(name) > 40:
                problems.append(f'端点 "{eid}" 的名字太长（上限 40 字），此条已丢弃')
                continue
            if not name:
                name = eid
            api_key = str(entry.get("api_key") or "").strip()
            retries = _require_int(entry.get("retries", 5), 0, 10, f'端点 "{eid}" 的 retries')
            retry_delay_s = _require_int(entry.get("retry_delay_s", 10), 1, 60, f'端点 "{eid}" 的 retry_delay_s')
            max_concurrency = _require_int(entry.get("max_concurrency", 2), 1, 8, f'端点 "{eid}" 的 max_concurrency')
            max_rpm = _require_int(entry.get("max_rpm", 0), 0, 600, f'端点 "{eid}" 的 max_rpm')
            # 请求头覆盖（高级设置）：非法条目逐个丢记问题，合法（名字, 值）对 MappingProxyType
            headers_pairs, headers_problems = validate_endpoint_headers_pairs(entry.get("headers"), eid)
            for msg in headers_problems:
                problems.append(msg)
            out.append(
                EndpointSetting(
                    id=eid, name=name, protocol=protocol, base_url=base_url, api_key=api_key,
                    retries=retries, retry_delay_s=retry_delay_s,
                    max_concurrency=max_concurrency, max_rpm=max_rpm,
                    headers=MappingProxyType(dict(headers_pairs)),
                )
            )
            seen.add(eid)
        except Exception as e:
            label = ""
            try:
                label = str(item.get("id") or "")  # type: ignore[union-attr]
            except Exception:
                label = ""
            where = f"「{label}」" if label else ""
            problems.append(f"端点条目{where}解析出错（{e}），已丢弃")
    return tuple(out)


def _parse_model_list(
    raw: Any, endpoints: tuple[EndpointSetting, ...], problems: list[str]
) -> tuple[ModelEntry, ...]:
    """[[model_list]] 逐条规范化：endpoint 必须指向存在的端点；坏条目丢弃记问题。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("[[model_list]] 必须是表数组，本次整段忽略")
        return ()
    endpoint_ids = {e.id for e in endpoints}
    out: list[ModelEntry] = []
    seen: set[str] = set()
    for item in raw:
        try:
            entry = _as_item_mapping(item, ModelListItemConfig)
            if entry is None:
                problems.append(f"模型库条目不是表：{str(item)[:60]}，已丢弃")
                continue
            mid = str(entry.get("id") or "").strip()
            if not _ID_RE.match(mid):
                problems.append(
                    f'模型库条目 id "{mid or str(entry.get("id"))}" 不合法（只能用小写字母、数字、_、-，1~24 个字符），此条已丢弃'
                )
                continue
            if mid in seen:
                problems.append(f'模型库条目 id "{mid}" 重复出现，只保留第一个，后一个已丢弃')
                continue
            endpoint = str(entry.get("endpoint") or "").strip()
            if endpoint not in endpoint_ids:
                shown = endpoint or "（空）"
                problems.append(f'模型库条目 "{mid}" 指的端点 "{shown}" 不存在，此条已丢弃')
                continue
            model = str(entry.get("model") or "").strip()
            if not model:
                problems.append(f'模型库条目 "{mid}" 的模型名不能为空，此条已丢弃')
                continue
            if len(model) > 200:
                problems.append(f'模型库条目 "{mid}" 的模型名太长（上限 200 字），此条已丢弃')
                continue
            name = str(entry.get("name") or "").strip()
            if len(name) > 60:
                problems.append(f'模型库条目 "{mid}" 的显示名太长（上限 60 字），此条已丢弃')
                continue
            if not name:
                name = model
            efforts: list[str] = []
            raw_efforts = entry.get("efforts")
            if raw_efforts is None:
                raw_efforts = []
            if not isinstance(raw_efforts, (list, tuple)):
                problems.append(f'模型库条目 "{mid}" 的 efforts 不是列表，本次按没有处理')
            else:
                for v in raw_efforts:
                    v_s = str(v or "").strip().lower()
                    if v_s in EFFORT_LEVELS:
                        if v_s not in efforts:
                            efforts.append(v_s)
                    elif v_s:
                        problems.append(f'模型库条目 "{mid}" 的思考强度 "{v_s}" 不认识（只能是 low / medium / high / xhigh / max），已去掉这一项')
            vision_raw = entry.get("vision", False)
            if not isinstance(vision_raw, bool):
                problems.append(f'模型库条目 "{mid}" 的 vision 必须是 true/false，此条已丢弃')
                continue
            context_window = _require_int(
                entry.get("context_window", 128000), 8192, 2_000_000, f'模型库条目 "{mid}" 的 context_window'
            )
            max_tokens = _require_int(
                entry.get("max_tokens", 32768), 1024, 1_000_000, f'模型库条目 "{mid}" 的 max_tokens'
            )
            if max_tokens >= context_window:
                problems.append(
                    f'模型库条目 "{mid}" 的 max_tokens（{max_tokens}）必须小于 context_window（{context_window}），此条已丢弃'
                )
                continue
            out.append(
                ModelEntry(
                    id=mid, endpoint=endpoint, model=model, name=name,
                    efforts=tuple(efforts), vision=vision_raw,
                    context_window=context_window, max_tokens=max_tokens,
                )
            )
            seen.add(mid)
        except Exception as e:
            label = ""
            try:
                label = str(item.get("id") or "")  # type: ignore[union-attr]
            except Exception:
                label = ""
            where = f"「{label}」" if label else ""
            problems.append(f"模型库条目{where}解析出错（{e}），已丢弃")
    return tuple(out)


_SECTIONS: tuple[tuple[str, type[PluginConfigBase]], ...] = (
    ("plugin", PluginSectionConfig),
    ("groups", GroupsSectionConfig),
    ("focus", FocusSectionConfig),
    ("feeds", FeedsSectionConfig),
    ("topics", TopicsSectionConfig),
    ("delivery", DeliverySectionConfig),
    ("approval", ApprovalSectionConfig),
    ("models", ModelsSectionConfig),
    ("jev", JevSectionConfig),
    ("quick_judge", QuickJudgeSectionConfig),
    ("usage", UsageSectionConfig),
    ("tasks", TasksSectionConfig),
    ("console", ConsoleSectionConfig),
    ("environments", EnvironmentsSectionConfig),
    ("profile", ProfileSectionConfig),
    ("storage", StorageSectionConfig),
    ("extensions", ExtensionsSectionConfig),
    ("group_space", GroupSpaceSectionConfig),
    ("reader", ReaderSectionConfig),
)


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """夹成 [low, high] 的整数；不是整数 / 越界 → 夹边或默认。不抛（配置错了插件照常要起）。"""
    if isinstance(value, bool):  # True/False 不是合法重试设置
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def load_settings(raw: MaiWorkConfig | dict) -> tuple[Settings, list[str]]:
    """把 MaiWorkConfig 实例或 dict 规范化成 (Settings, problems)。

    - 绝不抛异常；坏条目丢弃并记中文问题。
    - groups 单独解析（服务群条目非法要逐条丢、不能拖垮整节）。
    - 个别节整节类型不对：只回落那一节用默认，其余节照常保留。
    - raw 不是 dict / 配置模型：整体用默认值 + 记问题。
    """
    problems: list[str] = []

    raw_mapping: dict[str, Any]
    if isinstance(raw, MaiWorkConfig):
        raw_mapping = raw.model_dump(mode="python")
    elif isinstance(raw, Mapping):
        raw_mapping = dict(raw)
    else:
        problems.append(f"配置不是字典/配置模型（{type(raw).__name__}），整体用默认值")
        raw_mapping = {}

    # 服务群：从原始输入解析（模型自身不做"qq:前缀/纯数字/去重"判断）
    raw_groups = raw_mapping.get("groups")
    if not isinstance(raw_groups, Mapping):
        raw_groups = {}
    groups = _parse_groups({"groups": raw_groups}, problems)

    # 逐节校验：某一节坏了只回落那一节
    sections: dict[str, PluginConfigBase] = {}
    for name, cls in _SECTIONS:
        value = raw_mapping.get(name)
        if value is None:
            sections[name] = cls()
            continue
        if isinstance(value, cls):
            sections[name] = value
            continue
        try:
            sections[name] = cls.model_validate(value)
        except Exception:
            problems.append(f"配置节 [{name}] 值不合法，整节用默认值")
            sections[name] = cls()

    plugin = sections["plugin"]
    focus = sections["focus"]
    feeds = sections["feeds"]
    topics = sections["topics"]
    delivery = sections["delivery"]
    approval = sections["approval"]
    models = sections["models"]
    jev = sections["jev"]
    quick_judge = sections["quick_judge"]
    usage = sections["usage"]
    tasks_cfg = sections["tasks"]
    console = sections["console"]
    env = sections["environments"]
    profile = sections["profile"]
    storage = sections["storage"]
    extensions = sections["extensions"]
    group_space = sections["group_space"]
    reader = sections["reader"]
    assert isinstance(plugin, PluginSectionConfig)
    assert isinstance(focus, FocusSectionConfig)
    assert isinstance(feeds, FeedsSectionConfig)
    assert isinstance(topics, TopicsSectionConfig)
    assert isinstance(delivery, DeliverySectionConfig)
    assert isinstance(approval, ApprovalSectionConfig)
    assert isinstance(models, ModelsSectionConfig)
    assert isinstance(jev, JevSectionConfig)
    assert isinstance(quick_judge, QuickJudgeSectionConfig)
    assert isinstance(usage, UsageSectionConfig)
    assert isinstance(tasks_cfg, TasksSectionConfig)
    assert isinstance(console, ConsoleSectionConfig)
    assert isinstance(env, EnvironmentsSectionConfig)
    assert isinstance(profile, ProfileSectionConfig)
    assert isinstance(storage, StorageSectionConfig)
    assert isinstance(extensions, ExtensionsSectionConfig)
    assert isinstance(group_space, GroupSpaceSectionConfig)
    assert isinstance(reader, ReaderSectionConfig)

    # 扩展：从原始输入解析（像服务群一样逐条容错，坏的丢单条不拖累整节）
    raw_extensions = raw_mapping.get("extensions")
    if not isinstance(raw_extensions, Mapping):
        raw_extensions = {}

    # 端点 + 模型库（2026-10 模型改版阶段 1a）：从原始输入逐条解析（坏条目丢单条记问题）
    endpoints_parsed = _parse_endpoints(raw_mapping.get("endpoints"), problems)
    model_list_parsed = _parse_model_list(raw_mapping.get("model_list"), endpoints_parsed, problems)
    # 自己加的判断服务（2026-10）：同样逐条解析，坏的丢单条
    jev_endpoints_parsed = _parse_jev_endpoints(raw_mapping.get("jev_endpoints"), problems)

    # data_dir：空 = 默认位置
    data_dir_str = str(storage.data_dir or "").strip()
    data_dir = Path(data_dir_str) if data_dir_str else _default_data_dir()

    listen = _parse_listen(console, problems)

    workspace_root = Path(str(env.workspace_root or "/home/maiwork/workspaces"))

    settings = Settings(
        enabled=bool(plugin.enabled),
        groups=MappingProxyType(groups),
        data_dir=data_dir,
        workspace_root=workspace_root,
        focus=FocusSetting(
            max_members=int(focus.max_members),
            personal_profile=bool(focus.personal_profile),
            personal_feeds=bool(getattr(focus, "personal_feeds", True)),
            personal_per_day=max(1, int(getattr(focus, "personal_per_day", 3))),
        ),
        feeds=_parse_feeds(feeds, problems),
        topics=TopicsSetting(
            enabled=bool(topics.enabled), speaker=str(topics.speaker or "maiwork"),
            per_day=int(topics.per_day), min_gap_hours=int(topics.min_gap_hours),
            candidate_ttl_hours=int(topics.candidate_ttl_hours),
        ),
        delivery=DeliverySetting(
            push_per_day=int(delivery.push_per_day),
            quiet_hours=str(delivery.quiet_hours or "23:00-08:00"),
        ),
        approval=ApprovalSetting(
            required=bool(approval.required),
            admins=_norm_accounts(approval.admins, "管理员", problems),
            exempt_groups=_norm_accounts(approval.exempt_groups, "免批的群", problems),
            exempt_users=_norm_accounts(approval.exempt_users, "免批的人", problems),
            remind=bool(getattr(approval, "remind", True)),
            auto_review=bool(getattr(approval, "auto_review", True)),
            auto_review_daily=_clamp_int(
                getattr(approval, "auto_review_daily", 5), 0, 50, 5
            ),
        ),
        models=ModelsSetting(
            base_url=str(models.base_url or ""), api_key=str(models.api_key or ""),
            main=str(models.main or ""), main_backup=str(models.main_backup or ""),
            worker=str(models.worker or ""), worker_backup=str(models.worker_backup or ""),
            retries=_clamp_int(models.retries, 0, 10, 5),
            retry_delay_s=_clamp_int(models.retry_delay_s, 1, 60, 10),
            max_concurrency=_clamp_int(getattr(models, "max_concurrency", 2), 1, 8, 2),
            max_rpm=_clamp_int(getattr(models, "max_rpm", 0), 0, 600, 0),
            context_window=_clamp_int(getattr(models, "context_window", 128000), 8192, 2_000_000, 128000),
            max_tokens=_clamp_int(getattr(models, "max_tokens", 32768), 1024, 1_000_000, 32768),
        ),
        jev=_parse_jev(jev, problems),
        quick_judge=_parse_quick_judge(quick_judge, problems),
        usage=UsageSetting(
            alert_daily_tokens=int(usage.alert_daily_tokens),
            alert_task_tokens=int(usage.alert_task_tokens),
        ),
        tasks=TasksSetting(
            token_limit=max(0, _clamp_int(getattr(tasks_cfg, "token_limit", 2_000_000), 0, 10**9, 2_000_000)),
            run_seconds=max(0, _clamp_int(getattr(tasks_cfg, "run_seconds", 10800), 0, 30 * 86400, 10800)),
        ),
        console=ConsoleSetting(
            listen=listen,
            password=str(console.password or ""),
            public_url=str(console.public_url or "").rstrip("/"),
            update_check=bool(getattr(console, "update_check", True)),
            maibot_webui_url=_http_url_or_empty(getattr(console, "maibot_webui_url", "")),
        ),
        endpoints=endpoints_parsed,
        model_list=model_list_parsed,
        jev_endpoints=jev_endpoints_parsed,
        environments=_parse_environments(env, workspace_root, problems),
        profile=ProfileSetting(
            batch_messages=int(profile.batch_messages),
            max_interval_hours=int(profile.max_interval_hours),
            backfill_days=int(profile.backfill_days),
            backfill_max_messages=int(profile.backfill_max_messages),
            weekly_day=int(profile.weekly_day),
            read_interval_minutes=int(profile.read_interval_minutes),
        ),
        extensions=_parse_extensions(raw_extensions, problems),
        group_space=GroupSpaceSetting(
            enabled=bool(group_space.enabled),
            notice_per_day=max(1, int(group_space.notice_per_day)),
        ),
        reader=ReaderSetting(
            jina_enabled=bool(reader.jina_enabled),
            jina_api_key=str(reader.jina_api_key or "").strip(),
        ),
        problems=tuple(problems),
    )
    return settings, problems
