"""网页可改的配置（docs/02 相关节）：唯一真实来源是 config.toml，数据库不再存覆盖层。

- 「全部配置」（/api/settings/config）和「管理员对话 set_rules 工具」都走
  save_config_patch / reset_config_field 直写 config.toml（config_file.py 保注释、
  先备份到 <data_dir>/config-backups、写完 0600），写完立刻热生效（app.apply_config_text）。
- 校验和 config.toml 同一套语义：睡觉时段 HH:MM-HH:MM、news_slots 每项 HH:MM、
  「平台:账号」名单、数值范围按 CONFIG_SCHEMA 的类型和 min/max（越界在网页是 400 拒绝，不是夹值）。
- 2026-10 之前曾有一层 kv["rules.override"] 盖过文件值（「网页改了不生效」的坑）：
  已在启动迁移里把值搬进 config.toml 后删键（migrations.migrate_rules_override_to_file）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .config import (
    GroupSetting,
    Settings,
    _WORKSPACE_RE,
    _default_workspace,
    normalize_domain,
    parse_serve_group,
)

logger = logging.getLogger("maiwork.rules")

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


# ----------------------------------------------------------------------
# 字段白名单 + 校验
# ----------------------------------------------------------------------


def _bool_of(field_zh: str) -> Any:
    """返回校验函数：只收真布尔（防 JSON 的 1 / "yes" 混进来）。"""

    def check(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field_zh}是开关，只能填 true / false")
        return value

    return check


def _int_range(field_zh: str, lo: int, hi: int) -> Any:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_zh}要填 {lo}~{hi} 的整数")
        if not lo <= value <= hi:
            raise ValueError(f"{field_zh}只支持 {lo}~{hi}，{value} 不在范围里")
        return value

    return check


def _float_range(field_zh: str, lo: float, hi: float) -> Any:
    def check(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field_zh}要填 {lo:g}~{hi:g} 的数字")
        v = float(value)
        if not lo <= v <= hi:
            raise ValueError(f"{field_zh}只支持 {lo:g}~{hi:g}，{value} 不在范围里")
        return v

    return check


def _check_quiet_hours(value: Any) -> str:
    s = str(value or "").strip()
    if "-" not in s:
        raise ValueError('睡觉时段要写成 "HH:MM-HH:MM"，如 23:00-08:00')
    a, _, b = s.partition("-")
    if not _HHMM_RE.match(a.strip()) or not _HHMM_RE.match(b.strip()):
        raise ValueError('睡觉时段要写成 "HH:MM-HH:MM"，如 23:00-08:00（小时 00~23、分钟 00~59）')
    return f"{a.strip()}-{b.strip()}"


def _check_news_slots(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError('资讯时段要填至少一个 "HH:MM"，如 ["08:30", "19:00"]')
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not _HHMM_RE.match(raw.strip()):
            raise ValueError(f'资讯时段 {raw!r} 不是合法的 "HH:MM"（小时 00~23、分钟 00~59）')
        s = raw.strip()
        if s not in out:
            out.append(s)
    return out


def _check_account_list(field_zh: str) -> Any:
    """「平台:账号」列表（MaiBot 标准写法）；只写数字当 qq；统一成小写平台、去重。"""
    from .config import norm_account

    def check(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f'{field_zh}要是一个列表，每条写成「平台:账号」，如 ["qq:100000001"]')
        out: list[str] = []
        for raw in value:
            n = norm_account(raw)
            if not n:
                raise ValueError(f"{field_zh}里的 {raw!r} 认不出，要写成「平台:账号」，如 qq:100000001")
            if n not in out:
                out.append(n)
        return out

    return check


def _flat_base_value(base: Settings, section: str, field: str) -> Any:
    """config.toml 里这个字段的值（列表/元组转 list、数据类转成 dict 列表，方便和网页 JSON 比对）。

    字段不在 Settings 里（JevSetting 的 api_key 这类后加的字段也走 getattr 兜底）→ None。
    """
    if section == "groups" and field == "serve":
        # Settings.groups 是顶层字段（{群号: GroupSetting}），不是子节
        return [{"group": f"{getattr(g, 'platform', 'qq') or 'qq'}:{gid}", "workspace": g.workspace}
                for gid, g in base.groups.items()]
    obj = getattr(base, section, None)
    if obj is None:
        return None
    if field == "listen" and section == "console":
        # ConsoleSetting.listen 是 (host, port)，config.toml 里写的是 "IP:端口"
        host, port = getattr(obj, "listen", ("127.0.0.1", 18650))
        return f"{host}:{port}"
    value = getattr(obj, field, None)
    return _jsonish(value)


def _jsonish(value: Any) -> Any:
    """设置值转成网页/JSON 形态：tuple→list、Path→str、dataclass 列表→dict 列表。其他原样。"""
    if isinstance(value, tuple):
        return [_jsonish(v) for v in value]
    if isinstance(value, list):
        return [_jsonish(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonish(v) for k, v in value.__dict__.items()}
    if isinstance(value, Path):
        return str(value)
    return value


# ======================================================================
# 通用配置表（/api/settings/config，2026-10 新增；2026-10 改成直写 config.toml）
#
# config.toml 的每个配置项（models / extensions 两节有专用页面不进这里；
# plugin.config_version 不列）都在 CONFIG_SCHEMA 里登记一项：
# key（"节.字段"）、中文 label、一句大白话 help、type、min/max、options、
# applies（全部 now = 写进文件后立刻热生效；字段保留兼容，不再有 reload）、
# readonly + readonly_reason。
#
# 存储：网页改任何一项 = 直接写进插件目录下的 config.toml（config_file.py，
# 保留注释、写前重读、先备份旧内容到 <data_dir>/config-backups、写完 0600），
# config.toml 是唯一真实来源，数据库不再存配置覆盖层。reset = 删掉这个键，
# 回到代码默认值。
# secret 字段（jev.api_key / console.password）也写进
# config.toml（明文，用户明确同意）；GET 照旧只进不出——只回 {set, source}
# （"env" | "file" | "none"，环境变量优先级照旧最高），永远不回值。
# 生效：写完文件立刻在本进程应用（app.apply_config_text，走和
# on_config_update 一样的 update_config 路径），不等宿主的文件监控；
# 宿主随后再发一次 on_config_update 是同样内容，幂等。
#
# 0.8.0 群控归一：有几项以前是全局、现在**每个群自己一份**（谁能批本群的活 / 免批、
# 冷场开话题、往群里发多少 / 几点不打扰）。它们仍留在 CONFIG_SCHEMA 里（schema 覆盖面
# 检查 + 老种子的校验口径，规格别丢），但标了 group_managed：不在「全部配置」页面出现、
# 不能 PUT / reset，POST/PUT 会明确回一句「到群页管理」。config.toml 里那几行只作
# **新群第一次的迁移种子**（migrations.migrate_group_controls），不是第二个运行来源。
# ======================================================================


# ----------------------------------------------------------------------
# 各类型校验（值不合法抛 ValueError(中文)，返回规范化后的值）
# ----------------------------------------------------------------------


def _check_listen(value: Any) -> str:
    """console.listen：必须是 IP:端口（每段 0~255，端口 1~65535）。"""
    s = str(value or "").strip()
    m = re.match(r"^(?P<host>(?:\d{1,3}\.){3}\d{1,3}):(?P<port>\d{1,5})$", s)
    port = int(m.group("port")) if m else -1
    if not (m and 1 <= port <= 65535 and all(0 <= int(o) <= 255 for o in m.group("host").split("."))):
        raise ValueError('网页监听地址要写成 "IP:端口"（如 127.0.0.1:18650），不支持域名')
    return f"{m.group('host')}:{port}"


def _check_domain_list(field_zh: str) -> Any:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_zh}要填域名列表")
        out: list[str] = []
        for raw in value:
            d = normalize_domain(raw)
            if not d:
                raise ValueError(f"{field_zh}里的 {raw!r} 不是合法域名")
            if d not in out:
                out.append(d)
        return out

    return check


def _check_str_list(field_zh: str) -> Any:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_zh}要填一个列表")
        out: list[str] = []
        for raw in value:
            s = str(raw or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    return check


def _check_path_str(field_zh: str) -> Any:
    """绝对路径字符串；空允许（表示用默认），相对路径拒绝。"""

    def check(value: Any) -> str:
        s = str(value or "").strip()
        if s and not s.startswith(("/", "~/")):
            raise ValueError(f"{field_zh}要填绝对路径（/ 或 ~ 开头），或留空用默认")
        return s

    return check


def _check_memory_max(value: Any) -> str:
    s = str(value or "").strip().upper()
    if not re.match(r"^\d+[KMG]$", s):
        raise ValueError('内存上限要写成「数字+K/M/G」，如 512M、1G')
    return s


def _check_public_url(value: Any) -> str:
    s = str(value or "").strip().rstrip("/")
    if s and not s.startswith(("http://", "https://")):
        raise ValueError("对外地址要以 http:// 或 https:// 开头，或留空")
    return s


def _check_jev_url(value: Any) -> str:
    s = str(value or "").strip()
    if not s.startswith("https://"):
        raise ValueError("Jev 服务端地址必须 https:// 开头（密钥走这个请求，http 会泄露）")
    return s


def _check_name_str(field_zh: str) -> Any:
    def check(value: Any) -> str:
        s = str(value or "").strip()
        if not s:
            raise ValueError(f"{field_zh}不能为空")
        return s

    return check


def _check_run_as(value: Any) -> str:
    s = str(value or "").strip()
    if not s or not re.match(r"^[a-z_][a-z0-9_-]*$", s):
        raise ValueError("运行用户名要是合法的 Linux 用户名（小写字母/数字/下划线/横线）")
    return s


def _check_workspace_root(value: Any) -> str:
    s = str(value or "").strip()
    if not s.startswith("/"):
        raise ValueError("工作区根目录要填绝对路径（/ 开头）")
    return s


def _check_local_mode(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s == "direct":
        raise ValueError(
            '"direct" 没有任何隔离，生产一律按 systemd；只给本机开发测试用——'
            "config.toml 里写它默认也不生效，要本机设环境变量 MAIWORK_DEV_ALLOW_DIRECT=1；"
            '网页只接受 "systemd"'
        )
    if s != "systemd":
        raise ValueError(
            '本机执行方式网页上只能填 "systemd"（"direct" 无隔离，config.toml 里写它默认也不生效，'
            "要本机设 MAIWORK_DEV_ALLOW_DIRECT=1）"
        )
    return s


def _check_serve_groups(value: Any) -> list[dict[str, str]]:
    """服务群整表校验（语义同 config._parse_groups：parse_serve_group 的写法、workspace 白名单、不重复）。"""
    if not isinstance(value, list):
        raise ValueError('服务群列表要写成 [{"group": "qq:号码", "workspace": "工作区名"}, ...]')
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"服务群条目不是表：{item!r}")
        grp = str(item.get("group") or "").strip()
        ws = str(item.get("workspace") or "").strip()
        platform, gid = parse_serve_group(grp)  # 写错抛 ValueError（中文原因）
        if gid in seen:
            raise ValueError(f'服务群 "{grp}" 重复出现')
        seen.add(gid)
        ws = ws or _default_workspace(gid, platform)
        if not _WORKSPACE_RE.match(ws):
            raise ValueError(f'服务群 "{grp}" 的工作区名 "{ws}" 不合法（只能用字母、数字、下划线、横线，1~64 个字符）')
        out.append({"group": f"{platform}:{gid}", "workspace": ws})
    return out


def _check_ssh_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError('SSH 机器列表要写成 [{"name": "名字", "host": "user@地址:端口", "note": "备注"}, ...]')
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"SSH 机器条目不是表：{item!r}")
        name = str(item.get("name") or "").strip()
        host = str(item.get("host") or "").strip()
        note = str(item.get("note") or "").strip()
        if not name or not host:
            raise ValueError("SSH 机器条目的 name 和 host 都不能为空")
        from .environments.ssh import parse_host

        parse_host(host)  # 写法不对（- 开头、带空格 / shell 字符、端口越界）直接拒，中文原因
        entry = {"name": name, "host": host, "note": note}
        if entry not in out:
            out.append(entry)
    return out


# ----------------------------------------------------------------------
# 通用校验调度（按声明的类型 + min/max/options）
# ----------------------------------------------------------------------

_PATCH_MAX = 60  # 一次 PUT 最多带的字段数


def _validate_typed(spec: dict[str, Any], value: Any) -> Any:
    """按 schema 条目校验一个值；返回规范化的 JSON 形态值（列表是 list 不是 tuple）。"""
    label = spec["label"]
    ftype = spec["type"]
    if ftype == "bool":
        return _bool_of(label)(value)
    if ftype == "int":
        v = _int_range(label, int(spec["min"]), int(spec["max"]))(value)
        return v
    if ftype == "float":
        return _float_range(label, float(spec["min"]), float(spec["max"]))(value)
    if ftype == "str":
        if not isinstance(value, str):
            raise ValueError(f"{label}要填字符串")
        return value
    if ftype == "enum":
        options = [str(o) for o in spec.get("options") or ()]
        s = str(value or "").strip()
        if s not in options:
            raise ValueError(f"{label}只能是：{'、'.join(options)}")
        return s
    if ftype == "time_list":
        return _check_news_slots(value)
    if ftype == "list_str":
        return _check_str_list(label)(value)
    if ftype == "serve_groups":
        return _check_serve_groups(value)
    if ftype == "ssh_list":
        return _check_ssh_list(value)
    raise ValueError(f"{label}的类型 {ftype} 不认识")


def _validate_generic(spec: dict[str, Any], value: Any) -> Any:
    """先跑自定义 check（有就只信它），否则按 type 校验。"""
    check = spec.get("check")
    if check is not None:
        return check(value)
    return _validate_typed(spec, value)


# ----------------------------------------------------------------------
# 字段声明表
# ----------------------------------------------------------------------

# 用户常调的留在基础项；实现节奏、机器资源和服务地址归高级。
_ADVANCED_KEYS = frozenset({
    "feeds.news_jitter_minutes", "feeds.lookback_days", "feeds.web_min_avg",
    "feeds.pool_min_avg", "feeds.collect_minutes", "topics.min_gap_hours",
    "topics.candidate_ttl_hours", "console.listen",
    "tasks.token_limit", "tasks.run_seconds", "storage.data_dir",
    "jev.timeout_ms", "jev.key_file", "jev.api_url", "jev.model",
    "environments.workspace_root", "environments.memory_max", "environments.runtime_max_sec",
    "environments.local_mode", "environments.run_as", "environments.max_parallel",
    "environments.command_timeout_s", "environments.railway_daily_max",
    "environments.verify_per_round", "environments.verify_minutes",
    "profile.batch_messages", "profile.max_interval_hours", "profile.backfill_days",
    "profile.backfill_max_messages", "profile.weekly_day", "profile.read_interval_minutes",
})


# 0.8.0：以前全局一份、现在每个群自己一份的键 → 到哪个群页面改（给网页/工具的错误提示）。
# 值还从 config.toml 读（Settings 里那些字段留着），但只当**新群第一次的迁移种子**；
# 运行时的真源是 kv["group_approval.<群号>"] / kv["group_push.<群号>"]。
GROUP_MANAGED_KEYS: dict[str, str] = {
    "approval.required": "群 → 派活审批（谁能批 / 要不要批 / 免批，每个群一份）",
    "approval.admins": "群 → 派活审批（谁能批 / 要不要批 / 免批，每个群一份）",
    "approval.exempt_groups": "群 → 派活审批（谁能批 / 要不要批 / 免批，每个群一份）",
    "approval.exempt_users": "群 → 派活审批（谁能批 / 要不要批 / 免批，每个群一份）",
    "topics.enabled": "群 → 主动发言（开话题 / 资讯卡 / 提一嘴 / 每日上限 / 睡觉时段，每个群一份）",
    "topics.speaker": "已退役：开话题现在都由 MaiWork 自己说一句，不用再设",
    "topics.per_day": "群 → 主动发言（开话题 / 资讯卡 / 提一嘴 / 每日上限 / 睡觉时段，每个群一份）",
    "delivery.push_per_day": "群 → 主动发言（开话题 / 资讯卡 / 提一嘴 / 每日上限 / 睡觉时段，每个群一份）",
    "delivery.quiet_hours": "群 → 主动发言（开话题 / 资讯卡 / 提一嘴 / 每日上限 / 睡觉时段，每个群一份）",
}


def is_group_managed(key: Any) -> bool:
    """这个键是不是已经归「每个群自己一份」管（不再从全局网页 / set_rules 改）。"""
    return str(key or "") in GROUP_MANAGED_KEYS


def group_managed_hint(key: Any) -> str:
    """归群管的键：给用户的中文去处说明；不归群管返回 ""。"""
    return GROUP_MANAGED_KEYS.get(str(key or ""), "")


def _F(key: str, label: str, help: str, ftype: str, *, applies: str = "now",
       min: Any = None, max: Any = None, options: Any = None,
       readonly: bool = False, readonly_reason: str = "", check: Any = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "key": key, "label": label, "help": help, "type": ftype, "applies": applies,
        "readonly": bool(readonly), "advanced": key in _ADVANCED_KEYS,
        "group_managed": key in GROUP_MANAGED_KEYS,
    }
    if readonly_reason:
        spec["readonly_reason"] = readonly_reason
    if min is not None:
        spec["min"] = min
    if max is not None:
        spec["max"] = max
    if options is not None:
        spec["options"] = list(options)
    if check is not None:
        spec["check"] = check
    return spec


CONFIG_SCHEMA: list[dict[str, Any]] = [
    # ---- plugin 整节不列：config_version 是版本标记；enabled 总开关从网页关掉，
    # 网页自己跟着没了，只能改 config.toml（2026-09-28 按用户要求从网页拿掉） ----

    # ---- groups ----
    _F("groups.serve", "服务的群", "写成 qq:群号；同工作区共享画像", "serve_groups"),

    # ---- focus ----
    _F("focus.max_members", "关注人数", "每个群最多关注几人", "int", min=1, max=20),
    _F("focus.personal_profile", "个人画像", "关掉后只了解群，不了解个人", "bool"),
    _F("focus.personal_feeds", "个人推荐", "要先开个人画像；仅本人和管理员可见", "bool"),
    _F("focus.personal_per_day", "个人资讯数", "每次给一个人最多几条", "int", min=1, max=10),

    # ---- feeds ----
    _F("feeds.news_slots", "找资讯时间", "每天几点找，北京时间", "time_list"),
    _F("feeds.news_jitter_minutes", "时间浮动", "前后随机错开几分钟", "int", min=0, max=120),
    _F("feeds.max_items", "每批条数", "一批最多留几条", "int", min=1, max=20),
    _F("feeds.lookback_days", "查重天数", "和最近几天的比，重复不要", "int", min=1, max=90),
    _F("feeds.web_min_avg", "上网页分数", "满分 5，越高越挑", "float", min=1.0, max=5.0),
    _F("feeds.pool_min_avg", "开话题分数", "满分 5，够分才拿去开话题", "float", min=1.0, max=5.0),
    _F("feeds.guides", "找文章", "顺便找教程、好文章和工具", "bool"),
    _F("feeds.collect_minutes", "找资讯时长", "到点就交回已找到的", "int", min=1, max=60),
    _F("feeds.viz_per_day", "每日图解", "没配图的画张图；0 = 不画", "int", min=0, max=10),

    # ---- goals ----

    # ---- topics ----
    # 0.8.0：enabled / speaker / per_day 归每个群自己一份（group_managed，不在网页列出、
    # 不能改）；config.toml 里那几行只作新群第一次的迁移种子（migrations.migrate_group_controls）。
    _F("topics.enabled", "开话题", "已改到群页单独设", "bool"),
    _F("topics.speaker", "谁来开口", "已停用，改到群页", "enum", options=["maiwork", "maibot"]),
    _F("topics.per_day", "每日话题数", "已并入群页每日上限", "int", min=1, max=10),
    _F("topics.min_gap_hours", "话题间隔", "两次至少隔几小时", "int", min=1, max=24),
    _F("topics.candidate_ttl_hours", "话题保鲜", "备好的话题几小时内有效", "int", min=1, max=72),

    # ---- delivery ----
    # 0.8.0：push_per_day / quiet_hours 归每个群自己一份（group_managed），只作迁移种子。
    _F("delivery.push_per_day", "每日推送", "已并入群页每日上限", "int", min=1, max=10),
    _F("delivery.quiet_hours", "睡觉时段", "已改到群页单独设", "str", check=_check_quiet_hours),

    # ---- approval ----
    # 0.8.0：required / admins / exempt_groups / exempt_users 归每个群自己一份
    # （kv["group_approval.<群号>"]，group_managed），只作迁移种子；这里保留规格是为了
    # schema 覆盖面检查与老种子的校验口径。
    _F("approval.required", "派活要批", "已改到群页单独设", "bool"),
    _F("approval.admins", "批准人", "已改到群页单独设", "list_str", check=_check_account_list("管理员")),
    _F("approval.exempt_groups", "免批的群", "已改到群页单独设", "list_str", check=_check_account_list("免批的群")),
    _F("approval.exempt_users", "免批的人", "已改到群页单独设", "list_str", check=_check_account_list("免批的人")),
    _F("approval.remind", "待批提醒", "一天没人批就在群里提醒", "bool"),
    _F("approval.auto_review", "小活自动批", "查资料、做小网页这类直接开工", "bool"),
    _F("approval.auto_review_daily", "自动批上限", "每群每天几件；0 = 关。花钱、对外发消息、大工程照样等你批",
       "int", min=0, max=50),

    # ---- jev ----
    _F("jev.enabled", "快速判断", "关掉会慢一点、贵一点", "bool"),
    _F("jev.timeout_ms", "等待上限", "超过几毫秒就不等了", "int", min=200, max=1200),
    _F("jev.api_key", "Jev 密钥", "", "secret"),
    _F("jev.key_file", "密钥文件", "一般不用填", "str", check=_check_path_str("Jev 密钥文件路径")),
    _F("jev.api_url", "服务地址", "一般不用改", "str", check=_check_jev_url),
    _F("jev.model", "模型名", "一般不用改", "str", check=_check_name_str("Jev 模型名")),

    # ---- usage ----
    _F("usage.alert_daily_tokens", "每日提醒线", "每天用超就提醒；0 = 不提醒", "int", min=0, max=10**9),
    _F("usage.alert_task_tokens", "任务提醒线", "一件任务用超就提醒；0 = 不提醒", "int", min=0, max=10**9),

    # ---- models（2026-10 改版 1a：端点/模型库走 /api/settings/endpoints*、/api/settings/model-list*；
    #      [models].* 从「全部配置」拿掉，这里不登记） ----

    # ---- tasks ----
    _F("tasks.token_limit", "用量上限", "一件任务用超就暂停；0 = 不限", "int", min=0, max=10**9),
    _F("tasks.run_seconds", "时长上限", "做太久就暂停（秒）；0 = 不限", "int", min=0, max=30*86400),

    # ---- console ----
    _F("console.listen", "监听地址", "写成 IP:端口", "str", check=_check_listen),
    _F("console.password", "管理密码", "", "secret"),
    _F("console.public_url", "公开网址", "群链接要用，如 https://maiwork.example.com", "str", check=_check_public_url),
    _F("console.update_check", "检查更新", "有新版就在网页提醒", "bool"),
    _F("console.maibot_webui_url", "MaiBot 网址", "填了更新时能一键直达，如 http://127.0.0.1:8001", "str", check=_check_public_url),

    # ---- environments ----
    _F("environments.railway", "临时机器", "允许用 Railway 一次性机器", "bool"),
    _F("environments.ssh", "自有机器", "你自己的服务器，要跑命令的活优先用它。设置分两步：① 把「概况 → 运行状态」里的公钥加到机器的 ~/.ssh/authorized_keys；② 在主模型的 AGENTS.md 里写清每台机器的情况和用途，主模型会照着挑", "ssh_list"),
    _F("environments.workspace_root", "远端目录", "在远端机器上放文件的地方", "str", check=_check_workspace_root),
    _F("environments.memory_max", "内存上限", "每条命令最多用多少，如 1G", "str", check=_check_memory_max),
    _F("environments.runtime_max_sec", "命令时长", "每条命令最多跑几秒", "int", min=10, max=86400),
    _F("environments.local_mode", "本机方式", "本机怎么隔离运行", "enum", options=["systemd"], check=_check_local_mode),
    _F("environments.run_as", "运行账号", "本机用哪个账号跑", "str", check=_check_run_as),
    _F("environments.max_parallel", "同时几个", "最多几个子 agent 一起干", "int", min=1, max=10),
    _F("environments.command_timeout_s", "默认时长", "每条命令默认跑几秒", "int", min=10, max=3600),
    _F("environments.railway_daily_max", "每日台数", "每天最多几台（官方上限 3）", "int", min=1, max=3),
    _F("environments.verify_enabled", "实测资讯", "挑几条真跑一遍，结果放网页", "bool"),
    _F("environments.verify_per_round", "实测条数", "每轮试几条", "int", min=1, max=3),
    _F("environments.verify_minutes", "实测时长", "每条最多试几分钟", "int", min=1, max=60),

    # ---- profile ----
    _F("profile.batch_messages", "更新频率", "攒够几条消息更新一次画像", "int", min=10, max=1000),
    _F("profile.max_interval_hours", "最长间隔", "最多隔几小时也要更新", "int", min=1, max=72),
    _F("profile.backfill_days", "回读天数", "刚加入时往回读几天", "int", min=1, max=30),
    _F("profile.backfill_max_messages", "回读条数", "刚加入时最多读几条", "int", min=100, max=10000),
    _F("profile.weekly_day", "周整理日", "0 = 周一，6 = 周日", "int", min=0, max=6),
    _F("profile.read_interval_minutes", "读取间隔", "每隔几分钟读新消息", "int", min=1, max=120),

    # ---- storage ----
    _F("storage.data_dir", "数据目录", "只能在服务器改", "str",
       readonly=True, readonly_reason="只能在服务器改"),

    # ---- group_space ----
    _F("group_space.enabled", "群空间", "允许用群文件、公告和相册", "bool"),
    _F("group_space.notice_per_day", "每日公告", "每天最多发几条公告", "int", min=1, max=5),

    # ---- reader ----
    _F("reader.jina_enabled", "用 Jina 读",
       "先用 Jina，读不到再换", "bool"),
    _F("reader.jina_api_key", "Jina 密钥",
       "可选；填了每分钟 500 次（不填 20 次），jina.ai 免费申请", "secret"),
]

CONFIG_SECTIONS: list[dict[str, str]] = [
    {"id": "groups", "label": "服务的群"},
    {"id": "focus", "label": "关注的人"},
    {"id": "feeds", "label": "资讯"},
    {"id": "goals", "label": "目标"},
    {"id": "topics", "label": "开话题"},
    {"id": "delivery", "label": "主动发言"},
    {"id": "approval", "label": "派活审批"},
    {"id": "tasks", "label": "任务上限"},
    {"id": "models", "label": "模型"},
    {"id": "jev", "label": "快速判断"},
    {"id": "usage", "label": "用量提醒"},
    {"id": "console", "label": "网页"},
    {"id": "environments", "label": "干活机器"},
    {"id": "profile", "label": "群画像"},
    {"id": "storage", "label": "数据"},
    {"id": "group_space", "label": "群空间"},
    {"id": "reader", "label": "读网页"},
]

CONFIG_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in CONFIG_SCHEMA}
_SECTION_LABEL: dict[str, str] = {s["id"]: s["label"] for s in CONFIG_SECTIONS}


def full_label(key: str) -> str:
    """「节名 · 项名」。网页上项名很短、靠所在分节看懂；管理员对话等单独出现的地方用这个。"""
    spec = CONFIG_BY_KEY.get(key)
    if not spec:
        return key
    name = str(spec.get("label") or key)
    sec = _SECTION_LABEL.get(key.partition(".")[0], "")
    return name if not sec or sec == name else f"{sec} · {name}"

KV_CONFIG_OVERRIDE = "config.override"  # 已废弃的 kv 键名（迁移用；数据库不再存配置覆盖层）

# secret 字段 → 配置文件字段 / 优先级更高的环境变量（判 source 用；网页只进不出，
# 值直接写进 config.toml 的对应键——明文，用户明确同意）
SECRET_FIELDS: dict[str, dict[str, Any]] = {
    "jev.api_key": {
        "setting": ("jev", "api_key"),
        "env": ("TYPESAFE_API_KEY", "TYPESAFE_KEY_FILE"),
    },
    "console.password": {"setting": ("console", "password"), "env": ()},
    "reader.jina_api_key": {"setting": ("reader", "jina_api_key"), "env": ()},
}


# ----------------------------------------------------------------------
# 通用配置写 / 重置（直写 config.toml；数据库不再存配置覆盖层）
# ----------------------------------------------------------------------


def _norm_key(key: Any) -> str:
    return str(key or "").strip()


def read_config_override(store: Any) -> dict[str, dict[str, Any]]:
    """兼容残留：数据库覆盖层已废弃（配置唯一真实来源是 config.toml），永远 {}。"""
    return {}


def _ctx_of(base: Settings | None, data_dir: Any = None, plugin_dir: Any = None) -> tuple[Any, Any]:
    """从 base Settings 推 (plugin_dir, data_dir)；plugin_dir 缺省用 config._PLUGIN_DIR。"""
    from . import config as _config

    p_dir = Path(plugin_dir) if plugin_dir else _config._PLUGIN_DIR
    if data_dir is not None:
        d_dir = Path(data_dir)
    elif base is not None:
        d_dir = Path(base.data_dir)
    else:
        d_dir = _config._default_data_dir()
    return p_dir, d_dir


def save_config_patch(store: Any, body: Any, *, base: Settings, plugin_dir: Any = None) -> list[str]:
    """PUT 语义：扁平 {"节.字段": 值}（"current_password" 是改密码用的，不是字段）。

    - 全部字段先校验，一个不过整次不写文件（ValueError 中文原因）；
    - readonly 字段、不认识的字段一律 400；
    - 0.8.0 归每个群自己管的字段（approval.required/admins/exempt_*、topics.enabled/
      speaker/per_day、delivery.push_per_day/quiet_hours）也一律 400，并明确说去哪改——
      绝不写进文件让 runtime 不听（旧配置那几行只作新群的迁移种子）；
    - 普通字段：写进 config.toml 对应键；
    - secret 字段：没给 / 给 "" = 不改；给 null = 清空（文件里写 ""）；
      给非空字符串 = 写进 config.toml（明文）。console.password 额外要 body 里
      带正确的 current_password，改成功后旧的自动生成密码哈希失效。
    返回实际写进文件的「节.字段」清单（调用方负责写后应用）。
    """
    from . import config_file

    if not isinstance(body, dict):
        raise ValueError('请求体要写成 {"节.字段": 值}，如 {"feeds.max_items": 12}')
    items = {k: v for k, v in body.items() if _norm_key(k) != "current_password"}
    if not items:
        raise ValueError("没有要改的字段")
    if len(items) > _PATCH_MAX:
        raise ValueError(f"一次最多改 {_PATCH_MAX} 个字段")

    validated: dict[str, tuple[dict[str, Any], Any]] = {}
    for raw_key, value in items.items():
        key = _norm_key(raw_key)
        spec = CONFIG_BY_KEY.get(key)
        if spec is None:
            raise ValueError(f'不认识或不能在这里改的字段 "{key}"（[models] 走「设置 → 模型」、'
                             "[extensions] 走「设置 → 工具」、plugin.config_version 不可改）")
        hint = group_managed_hint(key)
        if hint:
            raise ValueError(f"「{spec['label']}」现在每个群自己一份，到「{hint}」里改；"
                             "config.toml 里那一行只作新群的迁移种子，不再从网页改")
        if spec.get("readonly"):
            raise ValueError(f"{spec['label']}不能从网页改：{spec.get('readonly_reason') or '只能改 config.toml 文件'}")
        if spec["type"] == "secret":
            if value is None or isinstance(value, str):
                validated[key] = (spec, value)
            else:
                raise ValueError(f"{spec['label']}要填字符串（null = 清空）")
        else:
            validated[key] = (spec, _validate_generic(spec, value))

    pw_pair = validated.get("console.password")
    if pw_pair is not None and isinstance(pw_pair[1], str) and pw_pair[1]:
        from .console.auth import ConsoleAuth

        auth = ConsoleAuth(store, lambda: base)
        current = body.get("current_password")
        if not current:
            raise ValueError("改管理员密码要同时填当前密码（current_password）核对")
        if not auth.verify_password(str(current)):
            raise ValueError("当前密码不对，改密码失败")

    writes: dict[str, Any] = {}
    for key, (spec, value) in validated.items():
        if spec["type"] == "secret":
            if value is None:
                writes[key] = ""      # null = 清空（写 ""）
            elif value != "":
                writes[key] = value   # 非空字符串 = 写入
            continue                  # "" = 不改
        writes[key] = value

    if not writes:
        return []
    p_dir, d_dir = _ctx_of(base, plugin_dir=plugin_dir)
    config_file.write_fields(p_dir, d_dir, writes)
    # 改密码：旧的自动生成哈希失效（有文件密码了）；已登录会话照旧
    # （cookie 混密码指纹，新指纹一生成旧 cookie 自动失效——ConsoleAuth 现读）。
    if "console.password" in writes and store is not None:
        try:
            with store.tx() as conn:
                store.secret_delete(conn, "admin_password_hash")
        except Exception:
            logger.exception("清旧的自动生成密码哈希出错")
    changed = "、".join(sorted(writes))
    logger.info("配置已写进 config.toml：%s（密钥值不落日志）", changed)
    return sorted(writes)


def reset_config_field(store: Any, key: Any, *, base: Settings | None = None, plugin_dir: Any = None) -> list[str]:
    """删掉 config.toml 里的这个键，回到代码默认值（secret 字段 = 删键，回空）。幂等。"""
    from . import config_file

    key_s = _norm_key(key)
    spec = CONFIG_BY_KEY.get(key_s)
    if spec is None:
        raise ValueError(f'不认识或不能在这里改的字段 "{key_s}"')
    hint = group_managed_hint(key_s)
    if hint:
        raise ValueError(f"「{spec['label']}」现在每个群自己一份，到「{hint}」里改；"
                         "config.toml 里那一行只作新群的迁移种子，不再从网页改")
    if spec.get("readonly"):
        raise ValueError(f"{spec['label']}不能从网页改：{spec.get('readonly_reason') or '只能改 config.toml 文件'}")
    p_dir, d_dir = _ctx_of(base, plugin_dir=plugin_dir)
    config_file.delete_fields(p_dir, d_dir, [key_s])
    logger.info("配置已从 config.toml 删掉（回代码默认值）：%s", key_s)
    return [key_s]


# ----------------------------------------------------------------------
# 视图（GET /api/settings/config 的数据层）
# ----------------------------------------------------------------------

# 代码默认值（Settings 的默认快照）：模块级惰性算一次（load_settings({}) 全默认）。
_DEFAULT_SETTINGS: Settings | None = None


def _default_settings() -> Settings:
    global _DEFAULT_SETTINGS
    if _DEFAULT_SETTINGS is None:
        from .config import load_settings

        _DEFAULT_SETTINGS, _ = load_settings({})
    return _DEFAULT_SETTINGS


def _secret_state(store: Any, base: Settings, key: str) -> dict[str, Any]:
    """secret 字段的 {set, source}；环境变量优先，其次 config.toml（file）。"""
    meta = SECRET_FIELDS[key]
    for env_name in meta.get("env") or ():
        if os.environ.get(env_name, "").strip():
            return {"set": True, "source": "env"}
    section, field = meta["setting"]
    file_value = str(getattr(getattr(base, section, None), field, "") or "")
    if file_value:
        return {"set": True, "source": "file"}
    if key == "console.password":
        # 文件没配：看自动生成的在不在
        try:
            if store is not None and store.secret_get("admin_password_hash"):
                return {"set": True, "source": "file"}
        except Exception:
            pass
    if key == "jev.api_key":
        # key_file / ~/.typesafe_key 里有内容也算「文件里配的」
        try:
            from . import jev as _jev

            probe = replace(base, jev=replace(base.jev, api_key=""))
            if _jev._resolve_key(probe):
                return {"set": True, "source": "file"}
        except Exception:
            pass
    return {"set": False, "source": "none"}


def config_view(base: Settings, store: Any, *, effective: Settings | None = None) -> dict[str, Any]:
    """{"file": "config.toml", "sections": [...], "reload_pending": []}。

    普通字段：value = 文件里的有效值（base 就是 config.toml 解析出的快照）、
    default = 代码默认值、changed = value != default。
    secret 字段：只进不出，只给 set/source。
    reload_pending 字段保留兼容——全部配置都热生效了，永远是 []。
    """
    defaults = _default_settings()

    def _field_view(spec: dict[str, Any]) -> dict[str, Any]:
        key = spec["key"]
        section, _, field = key.partition(".")
        out: dict[str, Any] = {
            "key": key,
            "label": spec["label"],
            "help": spec["help"],
            "type": spec["type"],
            "applies": spec["applies"],
            "readonly": bool(spec.get("readonly")),
            "advanced": bool(spec.get("advanced")),
        }
        if spec.get("group_managed"):
            # 0.8.0 归每个群自己管：不在「全部配置」列出（网页/工具改了也一律 400）
            out["group_managed"] = True
            out["group_managed_hint"] = group_managed_hint(key)
        if spec.get("readonly_reason"):
            out["readonly_reason"] = spec["readonly_reason"]
        if "min" in spec:
            out["min"] = spec["min"]
        if "max" in spec:
            out["max"] = spec["max"]
        if "options" in spec:
            out["options"] = spec["options"]
        if spec["type"] == "secret":
            out.update(_secret_state(store, base, key))
            return out
        value = _jsonish(_flat_base_value(base, section, field))
        default = _jsonish(_flat_base_value(defaults, section, field))
        out["value"] = value
        out["default"] = default
        out["changed"] = value != default
        return out

    sections: list[dict[str, Any]] = []
    for meta in CONFIG_SECTIONS:
        sid = meta["id"]
        fields = [
            _field_view(spec) for spec in CONFIG_SCHEMA
            if spec["key"].split(".", 1)[0] == sid and not spec.get("group_managed")
        ]
        if fields:
            sections.append({"id": sid, "label": meta["label"], "fields": fields})
    return {"file": "config.toml", "sections": sections, "reload_pending": []}
