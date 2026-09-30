"""配置模型（宿主 config.toml）+ Settings（规范化后的只读快照）。

- MaiWorkConfig 是宿主加载/校验用的 pydantic 模型，各节对应 docs/02-设计.md §12.4。
- load_settings 把 MaiWorkConfig（或裸 dict）规范化成 Settings；非法条目丢弃并记
  中文问题，绝不抛异常——配置写错不能让插件加载失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


CONFIG_VERSION = "0.4.4"  # 0.4.4：[console] update_check、maibot_webui_url；0.4.3：[feeds] viz_per_day；0.4.2：[models] max_tokens；0.4.1：[reader] Jina Reader；0.4.0：[models] context_window、[tasks] 安全网、[feeds] collect_minutes

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

    __ui_label__ = "服务群"
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
    __ui_label__ = "关注成员"
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
    ideas_per_day: int = Field(default=1, description="每群每天最多出几个构想")
    min_score: float = Field(default=0.6, description="（已不用，2026-09-27 起改看 web_min_avg）旧的资讯入选总分门槛")
    max_items: int = Field(default=10, description="每批最多入选几条资讯")
    lookback_days: int = Field(default=14, description="和最近多少天已出的资讯去重")
    blocked_domains: list[str] = Field(default_factory=list, description="来源屏蔽名单（域名，含其子域）")
    web_min_avg: float = Field(default=3.0, description="资讯/文章上网页的五项平均分门槛（1~5）")
    pool_min_avg: float = Field(default=4.0, description="资讯进群里开话题候选池的平均分门槛（1~5）")
    guides: bool = Field(default=True, description="备资讯时要不要同时找「文章」（教程、好文章、工具介绍）")
    collect_minutes: int = Field(default=15, description="资讯收集子 agent 每轮最多跑多少分钟（到点把已找到的交回来）")
    viz_per_day: int = Field(default=3, description="每群每天最多给几条没配图、数据多的资讯做「图解」小图（0 = 不做）")


class GoalsSectionConfig(PluginConfigBase):
    __ui_label__ = "目标"
    __ui_icon__ = "bullseye"
    __ui_order__ = 6

    propose: bool = Field(default=True, description="MaiWork 觉得群里缺个长期目标时，主动提一个等管理员批准（每群每天最多一次）")


class TopicsSectionConfig(PluginConfigBase):
    __ui_label__ = "冷场开话题"
    __ui_icon__ = "message-circle"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="冷场时是否开话题")
    speaker: str = Field(default="maiwork", description='谁说：maiwork=按人设直接发；maibot=请 MaiBot 自己开口（它可能不说）')
    per_day: int = Field(default=2, description="每群每天最多开几个话题")
    min_gap_hours: int = Field(default=3, description="同一群两次开话题的最小间隔（小时）")
    candidate_ttl_hours: int = Field(default=12, description="话题候选池里一条资讯多少小时后过期")


class DeliverySectionConfig(PluginConfigBase):
    __ui_label__ = "推送"
    __ui_icon__ = "send"
    __ui_order__ = 5

    push_per_day: int = Field(default=3, description="每群每天主动推到群里的上限（含开话题）")
    quiet_hours: str = Field(default="23:00-08:00", description="睡觉时段（北京时间），不开话题，其他主动推送推迟")
    mention_ttl_minutes: int = Field(default=120, description="「可提起清单」里一条的有效期（分钟）")


class ApprovalSectionConfig(PluginConfigBase):
    __ui_label__ = "派活批准"
    __ui_icon__ = "shield-check"
    __ui_order__ = 6

    required: bool = Field(default=True, description="群友派的活是否要 bot 管理员批准后才能开工")
    admins: list[str] = Field(default_factory=lambda: ["qq:100000001"], description="bot 管理员，写成「平台:账号」，如 qq:100000001（只写数字当 qq）")
    exempt_groups: list[str] = Field(default_factory=list, description="免批的群，写成「平台:群号」，如 qq:900000001")
    exempt_users: list[str] = Field(default_factory=list, description="派活免批的人，写成「平台:账号」，如 qq:10001")
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


class UsageSectionConfig(PluginConfigBase):
    __ui_label__ = "用量提醒"
    __ui_icon__ = "bar-chart"
    __ui_order__ = 9

    alert_daily_tokens: int = Field(default=0, description="一天 token 超过这个数就提醒；0=不提醒；只提醒不暂停")
    alert_task_tokens: int = Field(default=0, description="单个任务 token 超过这个数就提醒；0=不提醒")


class TasksSectionConfig(PluginConfigBase):
    __ui_label__ = "任务安全网"
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

    __ui_label__ = "SSH 机器"
    __ui_icon__ = "server"

    name: str = Field(default="", description="这台机器的名字")
    host: str = Field(default="", description="地址，如 user@1.2.3.4:22")
    note: str = Field(default="", description="备注")


class EnvironmentsSectionConfig(PluginConfigBase):
    __ui_label__ = "执行环境"
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
    __ui_label__ = "数据存储"
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
    __ui_label__ = "扩展"
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
    __ui_label__ = "打开网页"
    __ui_icon__ = "globe"
    __ui_order__ = 16

    jina_enabled: bool = Field(default=True, description="打开网页读正文先用 Jina Reader（r.jina.ai）；读不到 / 限流就马上换「抓网页正文」工具，再不行直接打开")
    jina_api_key: str = Field(default="", description="Jina Reader 密钥（可选）：不填每分钟 20 次（按服务器 IP 算），填了 500 次；只进不出，不写日志")


class MaiWorkConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    groups: GroupsSectionConfig = Field(default_factory=GroupsSectionConfig)
    focus: FocusSectionConfig = Field(default_factory=FocusSectionConfig)
    feeds: FeedsSectionConfig = Field(default_factory=FeedsSectionConfig)
    goals: GoalsSectionConfig = Field(default_factory=GoalsSectionConfig)
    topics: TopicsSectionConfig = Field(default_factory=TopicsSectionConfig)
    delivery: DeliverySectionConfig = Field(default_factory=DeliverySectionConfig)
    approval: ApprovalSectionConfig = Field(default_factory=ApprovalSectionConfig)
    models: ModelsSectionConfig = Field(default_factory=ModelsSectionConfig)
    jev: JevSectionConfig = Field(default_factory=JevSectionConfig)
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
    ideas_per_day: int
    min_score: float         # 已不用（2026-09-27 质量标准），留做兼容
    max_items: int
    lookback_days: int
    blocked_domains: tuple[str, ...]
    web_min_avg: float
    pool_min_avg: float
    guides: bool
    collect_minutes: int = 15  # 资讯收集子 agent 每轮时间盒（到点把已找到的交回）
    viz_per_day: int = 3       # 资讯图解每群每天上限（0 = 关），news_viz.py


@dataclass(frozen=True)
class GoalsSetting:
    propose: bool = True


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
    mention_ttl_minutes: int


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


@dataclass(frozen=True)
class JevSetting:
    enabled: bool
    timeout_ms: int
    key_file: str
    api_url: str
    model: str
    api_key: str = ""


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
    goals: GoalsSetting
    topics: TopicsSetting
    delivery: DeliverySetting
    approval: ApprovalSetting
    models: ModelsSetting
    jev: JevSetting
    usage: UsageSetting
    console: ConsoleSetting
    environments: EnvironmentsSetting
    profile: ProfileSetting
    tasks: TasksSetting = TasksSetting()
    extensions: ExtensionsSetting = ExtensionsSetting(mcp=())
    group_space: GroupSpaceSetting = GroupSpaceSetting(enabled=True, notice_per_day=1)
    reader: ReaderSetting = ReaderSetting()
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

    feeds 的屏蔽名单（[feeds] blocked_domains 和 kv）、/api/feeds/domains 入参都走这里。
    """
    s = str(raw or "").strip().lower()
    if s.startswith("www."):
        s = s[4:]
    if not _DOMAIN_RE.match(s):
        return ""
    return s


def _parse_feeds(feeds: FeedsSectionConfig, problems: list[str], *, min_score_explicit: bool = False) -> FeedsSetting:
    """[feeds] 节规范化。

    - blocked_domains 逐个规范化（小写、去 www.、和子域匹配用同一串）；不合法的丢弃记问题。
    - web_min_avg / pool_min_avg 夹到 1~5。
    - min_score 于 2026-09-27 的质量标准起不再使用（改看 web_min_avg）：字段保留兼容；
      用户在配置里显式写了 min_score 时，问题清单里点一句不再用它。
    """
    blocked: list[str] = []
    for raw in feeds.blocked_domains or []:
        d = normalize_domain(raw)
        if not d:
            problems.append(f"[feeds] blocked_domains 里的 {str(raw)!r} 不是合法域名，已忽略")
            continue
        if d not in blocked:
            blocked.append(d)

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

    # 宿主生成 config.toml 时会把默认值 0.6 也写进去（线上实测），默认值不算「改过」
    if min_score_explicit and abs(float(feeds.min_score) - 0.6) > 1e-9:
        problems.append("[feeds] min_score 已不用（2026-09-27 起改看 web_min_avg），可以删掉这一行")
    return FeedsSetting(
        news_slots=tuple(str(s) for s in feeds.news_slots),
        news_jitter_minutes=int(feeds.news_jitter_minutes),
        ideas_per_day=int(feeds.ideas_per_day),
        min_score=float(feeds.min_score),
        max_items=max(1, int(feeds.max_items)),
        lookback_days=max(1, int(feeds.lookback_days)),
        blocked_domains=tuple(blocked),
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
    )


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


_SECTIONS: tuple[tuple[str, type[PluginConfigBase]], ...] = (
    ("plugin", PluginSectionConfig),
    ("groups", GroupsSectionConfig),
    ("focus", FocusSectionConfig),
    ("feeds", FeedsSectionConfig),
    ("goals", GoalsSectionConfig),
    ("topics", TopicsSectionConfig),
    ("delivery", DeliverySectionConfig),
    ("approval", ApprovalSectionConfig),
    ("models", ModelsSectionConfig),
    ("jev", JevSectionConfig),
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
    min_score_explicit = False
    if isinstance(raw, MaiWorkConfig):
        raw_mapping = raw.model_dump(mode="python")
        # 模型实例：pydantic 的 fields_set 记了验证时显式给过的字段（默认 dump 什么都有，
        # 不能靠「键在不在」判断）
        try:
            min_score_explicit = "min_score" in raw.feeds.model_fields_set
        except Exception:
            min_score_explicit = False
    elif isinstance(raw, Mapping):
        raw_mapping = dict(raw)
        raw_feeds = raw_mapping.get("feeds")
        min_score_explicit = isinstance(raw_feeds, Mapping) and "min_score" in raw_feeds
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
    goals = sections["goals"]
    topics = sections["topics"]
    delivery = sections["delivery"]
    approval = sections["approval"]
    models = sections["models"]
    jev = sections["jev"]
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
    assert isinstance(goals, GoalsSectionConfig)
    assert isinstance(topics, TopicsSectionConfig)
    assert isinstance(delivery, DeliverySectionConfig)
    assert isinstance(approval, ApprovalSectionConfig)
    assert isinstance(models, ModelsSectionConfig)
    assert isinstance(jev, JevSectionConfig)
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
        feeds=_parse_feeds(feeds, problems, min_score_explicit=min_score_explicit),
        goals=GoalsSetting(propose=bool(getattr(goals, "propose", True))),
        topics=TopicsSetting(
            enabled=bool(topics.enabled), speaker=str(topics.speaker or "maiwork"),
            per_day=int(topics.per_day), min_gap_hours=int(topics.min_gap_hours),
            candidate_ttl_hours=int(topics.candidate_ttl_hours),
        ),
        delivery=DeliverySetting(
            push_per_day=int(delivery.push_per_day),
            quiet_hours=str(delivery.quiet_hours or "23:00-08:00"),
            mention_ttl_minutes=int(delivery.mention_ttl_minutes),
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
