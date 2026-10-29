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
from typing import Any

try:  # Windows 没有 pwd 模块；查不到账号就只按名字拦（下面只拦 "root" 这个名字）
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None  # type: ignore[assignment]

from .config import (
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


def _check_proxy_list(value: Any) -> list[str]:
    """console.trusted_proxies：每一项都得是 IP 或网段。"""
    import ipaddress

    if not isinstance(value, list):
        raise ValueError("反向代理地址要填 IP 或网段的列表")
    out: list[str] = []
    for raw in value:
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise ValueError(f"「{s}」不是合法的 IP 或网段（例如 127.0.0.1、10.0.0.0/8）") from None
        if s not in out:
            out.append(s)
    return out


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


_JEV_USE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _check_jev_use(value: Any) -> str:
    """「现在用哪个判断服务」：内置 typesafe，或 [[jev_endpoints]] 的 id。"""
    s = str(value or "").strip()
    if s in ("", "typesafe"):
        return "typesafe"
    if not _JEV_USE_RE.match(s):
        raise ValueError("判断服务 id 只能用小写字母、数字、_、-（1~32 个字符），或内置的 typesafe")
    return s


def _check_name_str(field_zh: str) -> Any:
    def check(value: Any) -> str:
        s = str(value or "").strip()
        if not s:
            raise ValueError(f"{field_zh}不能为空")
        return s

    return check


def _run_as_uid(name: str) -> int | None:
    """查这个账号的 uid；系统里没有这个账号 / 平台没有 pwd 模块 → None。"""
    if pwd is None:
        return None
    try:
        return int(pwd.getpwnam(str(name)).pw_uid)
    except (KeyError, OSError, ValueError):
        return None


def _check_run_as(value: Any) -> str:
    """运行账号：Linux 用户名，且不许是 root / uid 为 0 的账号（子 agent 拿到 root 就没有隔离了）。

    账号还不存在时照旧放行（网页上可以先填、后建用户）；直接写 config.toml 绕过这里的，
    由 environments/local.py 拼 systemd 单元前再拦一次。
    """
    s = str(value or "").strip()
    if not s or not re.match(r"^[a-z_][a-z0-9_-]*$", s):
        raise ValueError("运行用户名要是合法的 Linux 用户名（小写字母/数字/下划线/横线）")
    if s == "root" or _run_as_uid(s) == 0:
        raise ValueError("运行账号不能是 root 或 uid 为 0 的账号（子 agent 会拿到 root 权限）")
    return s


def _check_workspace_root(value: Any) -> str:
    s = str(value or "").strip()
    if not s.startswith("/"):
        raise ValueError("工作区根目录要填绝对路径（/ 开头）")
    return s


# Freestyle 的镜像：官方 slug（freestyle/ubuntu-sm）或用户自己拍的快照（sh-<uuid>）。
_FREESTYLE_SNAPSHOT_RE = re.compile(r"^(?:freestyle/[a-z0-9][a-z0-9-]{0,62}|[a-z0-9][a-z0-9-]{0,62})$")


def _check_freestyle_snapshot(value: Any) -> str:
    s = str(value or "").strip()
    if not _FREESTYLE_SNAPSHOT_RE.match(s):
        raise ValueError(
            "机器规格要填 Freestyle 的镜像名：官方镜像如 freestyle/ubuntu-sm（2 核 4G）、"
            "freestyle/ubuntu（4 核 8G，免费档上限），或你自建的快照名（sh- 开头）"
        )
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
        # options 可以是纯字符串，也可以是 {"value","label"}（网页下拉显示 label）
        options = [str(o.get("value") if isinstance(o, dict) else o) for o in spec.get("options") or ()]
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
    "topics.candidate_ttl_hours", "console.listen", "console.trusted_proxies",
    "tasks.token_limit", "tasks.run_seconds", "storage.data_dir",
    "jev.timeout_ms", "jev.key_file", "jev.api_url", "jev.model", "jev.use",
    "quick_judge.model", "quick_judge.daily_max",
    "session.max_running_per_group", "session.triage_timeout_ms", "session.bot_names",
    "environments.workspace_root", "environments.memory_max", "environments.runtime_max_sec",
    "environments.local_mode", "environments.run_as", "environments.max_parallel",
    "environments.command_timeout_s", "environments.railway_daily_max",
    "environments.freestyle_snapshot", "environments.freestyle_ttl_minutes",
    "environments.freestyle_idle_minutes", "environments.freestyle_daily_max",
    "environments.verify_per_round", "environments.verify_minutes",
    "profile.batch_messages", "profile.max_interval_hours", "profile.backfill_days",
    "profile.backfill_max_messages", "profile.weekly_day", "profile.read_interval_minutes",
})


# 0.8.0：以前全局一份、现在每个群自己一份的键 → 到哪个群页面改（给网页/工具的错误提示）。
# 值还从 config.toml 读（Settings 里那些字段留着），但只当**新群第一次的迁移种子**；
# 运行时的真源是 kv["group_approval.<群号>"] / kv["group_push.<群号>"]。
GROUP_MANAGED_KEYS: dict[str, str] = {
    "approval.required": "群页面 → 任务审批（审批人、是否需审批、免审批成员，每个群单独设置）",
    "approval.admins": "群页面 → 任务审批（审批人、是否需审批、免审批成员，每个群单独设置）",
    "approval.exempt_groups": "群页面 → 任务审批（审批人、是否需审批、免审批成员，每个群单独设置）",
    "approval.exempt_users": "群页面 → 任务审批（审批人、是否需审批、免审批成员，每个群单独设置）",
    "topics.enabled": "群页面 → 主动发言（开启话题、资讯卡片、构想提及、每日上限、免打扰时段，每个群单独设置）",
    "topics.speaker": "已停用：开场白统一由 MaiWork 发送，无需设置",
    "topics.per_day": "群页面 → 主动发言（开启话题、资讯卡片、构想提及、每日上限、免打扰时段，每个群单独设置）",
    "delivery.push_per_day": "群页面 → 主动发言（开启话题、资讯卡片、构想提及、每日上限、免打扰时段，每个群单独设置）",
    "delivery.quiet_hours": "群页面 → 主动发言（开启话题、资讯卡片、构想提及、每日上限、免打扰时段，每个群单独设置）",
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
    _F("groups.serve", "服务群", "格式为 平台:群号，例如 qq:123456；同一工作区的群共享画像。", "serve_groups"),

    # ---- focus ----
    _F("focus.max_members", "关注人数", "每个群最多关注的成员数。", "int", min=1, max=20),
    _F("focus.personal_profile", "个人画像", "关闭后仅建立群画像，不建立个人画像。", "bool"),
    _F("focus.personal_feeds", "个人推荐", "需先开启个人画像；推荐内容仅本人和管理员可见。", "bool"),
    _F("focus.personal_per_day", "个人资讯数", "每批次为每位成员推荐的最大条数。", "int", min=1, max=10),

    # ---- feeds ----
    _F("feeds.news_slots", "资讯搜集时间", "每天搜集资讯的时间点（北京时间）。", "time_list"),
    _F("feeds.news_jitter_minutes", "时间浮动", "在设定时间前后随机浮动的分钟数。", "int", min=0, max=120),
    _F("feeds.max_items", "每批条数", "每批次最多保留的资讯条数。", "int", min=1, max=20),
    _F("feeds.lookback_days", "查重天数", "与最近若干天的资讯比对，去除重复内容。", "int", min=1, max=90),
    _F("feeds.web_min_avg", "网页展示分数线", "满分 5 分，分数线越高筛选越严格。", "float", min=1.0, max=5.0),
    _F("feeds.pool_min_avg", "话题分数线", "满分 5 分，达到分数线的资讯才会用于开启话题。", "float", min=1.0, max=5.0),
    _F("feeds.guides", "搜集文章", "同时搜集教程、优质文章和工具。", "bool"),
    _F("feeds.collect_minutes", "搜集时长", "超过时长后，以已搜集到的内容为准。", "int", min=1, max=60),
    _F("feeds.viz_per_day", "每日图解数", "为无配图的资讯生成图解；0 表示不生成。", "int", min=0, max=10),

    # ---- goals ----

    # ---- topics ----
    # 0.8.0：enabled / speaker / per_day 归每个群自己一份（group_managed，不在网页列出、
    # 不能改）；config.toml 里那几行只作新群第一次的迁移种子（migrations.migrate_group_controls）。
    _F("topics.enabled", "开启话题", "已改为在群页面中按群设置。", "bool"),
    _F("topics.speaker", "发言方", "已停用，改为在群页面中设置。", "enum", options=["maiwork", "maibot"]),
    _F("topics.per_day", "每日话题数", "已并入群页面中的每日上限。", "int", min=1, max=10),
    _F("topics.min_gap_hours", "话题间隔", "两次开启话题之间的最少小时数。", "int", min=1, max=24),
    _F("topics.candidate_ttl_hours", "话题有效期", "备选话题的有效小时数。", "int", min=1, max=72),

    # ---- delivery ----
    # 0.8.0：push_per_day / quiet_hours 归每个群自己一份（group_managed），只作迁移种子。
    _F("delivery.push_per_day", "每日推送数", "已并入群页面中的每日上限。", "int", min=1, max=10),
    _F("delivery.quiet_hours", "免打扰时段", "已改为在群页面中按群设置。", "str", check=_check_quiet_hours),

    # ---- approval ----
    # 0.8.0：required / admins / exempt_groups / exempt_users 归每个群自己一份
    # （kv["group_approval.<群号>"]，group_managed），只作迁移种子；这里保留规格是为了
    # schema 覆盖面检查与老种子的校验口径。
    _F("approval.required", "任务需审批", "已改为在群页面中按群设置。", "bool"),
    _F("approval.admins", "审批人", "已改为在群页面中按群设置。", "list_str", check=_check_account_list("管理员")),
    _F("approval.exempt_groups", "免审批群", "已改为在群页面中按群设置。", "list_str", check=_check_account_list("免批的群")),
    _F("approval.exempt_users", "免审批成员", "已改为在群页面中按群设置。", "list_str", check=_check_account_list("免批的人")),
    _F("approval.remind", "待审批提醒", "任务超过一天未审批时，在群内发送提醒。", "bool"),
    _F("approval.auto_review", "小型任务自动审批", "查资料、制作简单网页等小型任务无需人工审批，直接开始执行。", "bool"),
    _F("approval.auto_review_daily", "自动审批上限", "每个群每天自动审批的任务数；0 表示关闭。涉及费用、对外发送消息或规模较大的任务仍需人工审批。",
       "int", min=0, max=50),

    # ---- jev ----
    _F("jev.enabled", "快速判断", "关闭后判断改由主模型处理，速度较慢、成本较高。", "bool"),
    _F("jev.timeout_ms", "超时（毫秒）", "超过该时长未响应则放弃本次判断。", "int", min=200, max=1200),
    _F("jev.api_key", "Jev 密钥", "", "secret"),
    _F("jev.key_file", "密钥文件", "通常无需填写。", "str", check=_check_path_str("Jev 密钥文件路径")),
    _F("jev.api_url", "服务地址", "通常无需修改。", "str", check=_check_jev_url),
    _F("jev.model", "模型名称", "通常无需修改。", "str", check=_check_name_str("Jev 模型名")),
    _F("jev.use", "当前服务", "内置服务填写 typesafe，或填写自定义服务的 id；通常在「设置 → 模型 → 快速判断」中切换。",
       "str", check=_check_jev_use),

    # ---- quick_judge ----
    _F("quick_judge.enabled", "任务识别备用方案", "未配置 Jev 或 Jev 无法确定时，使用模型判断该 @ 消息是否为任务请求。", "bool"),
    _F("quick_judge.model", "判断模型", "填写模型库中的条目 id；留空则与主模型相同。", "str"),
    _F("quick_judge.keyword_filter", "关键词预筛选", "不含请求词的 @ 消息视为闲聊；表述含糊的任务请求可能被遗漏。", "bool"),
    _F("quick_judge.daily_max", "每日判断上限", "每个群每天最多判断的次数，每条 @ 消息计一次；0 表示不使用模型判断。", "int", min=0, max=500),

    # ---- session（群友工作会话；docs/02 §9.2 补记、任务 10-20-group-task-sessions） ----
    _F("session.enabled", "连续对话",
       "群友派的活批准后，发起人可以继续 @ 机器人说「往下滚」「改成英文」「做到哪了」「结束吧」，"
       "在同一段工作对话里接着做；关闭后与以前一样，每次 @ 都当一件新事。", "bool"),
    _F("session.relay_text", "工作回应",
       "连续对话里，把 MaiWork 边做边说的话直接发到群里（口吻按 MaiWork 人设；工具调用和内部过程不发）。"
       "关闭后只发等批准、交付等固定的话。", "bool"),
    _F("session.idle_close_minutes", "闲置结束（分钟）",
       "没有活在跑、发起人也一直没再 @，过了这么久就悄悄结束这段对话，借用的电脑一并释放。",
       "int", min=1, max=10080),
    _F("session.max_running_per_group", "每群同时进行数", "一个群同一时间只做一件活，其余排队。",
       "int", min=1, max=1, readonly=True, readonly_reason="当前固定为 1"),
    _F("session.triage_timeout_ms", "判断时限（毫秒）",
       "判断一句 @ 是给 MaiWork 的还是闲聊，最多等多久；超时当闲聊交给 MaiBot。", "int", min=300, max=1500),
    _F("session.bot_names", "机器人显示名",
       "机器人在各群里被 @ 时显示的名字（群名片或昵称）。通常无需填写，MaiWork 会自动学；"
       "新增的名字在插件下次加载时生效。", "list_str"),

    # ---- usage ----
    _F("usage.alert_daily_tokens", "每日用量提醒线", "当日用量超过该值时提醒；0 表示不提醒。", "int", min=0, max=10**9),
    _F("usage.alert_task_tokens", "单任务用量提醒线", "单个任务用量超过该值时提醒；0 表示不提醒。", "int", min=0, max=10**9),

    # ---- models（2026-10 改版 1a：端点/模型库走 /api/settings/endpoints*、/api/settings/model-list*；
    #      [models].* 从「全部配置」拿掉，这里不登记） ----

    # ---- tasks ----
    _F("tasks.token_limit", "用量上限", "单个任务用量超过该值时暂停；0 表示不限制。", "int", min=0, max=10**9),
    _F("tasks.run_seconds", "时长上限（秒）", "单个任务运行超过该时长时暂停；0 表示不限制。", "int", min=0, max=30*86400),

    # ---- console ----
    _F("console.listen", "监听地址", "格式为 IP:端口。", "str", check=_check_listen),
    _F("console.password", "管理密码", "", "secret"),
    _F("console.public_url", "公开网址", "用于生成群链接，例如 https://maiwork.example.com。", "str", check=_check_public_url),
    _F("console.board_hours", "看板时间范围", "看板「最近」一栏显示多少小时内的记录。", "int", min=1, max=168),
    _F("console.herenow_share", "用 here.now 自动公开", "没填公开网址时，群友要看的页面自动发到 here.now 公开页，拿到链接的人都能看，24 小时后失效。", "bool"),
    _F("console.update_check", "检查更新", "有新版本时在网页中提示。", "bool"),
    _F("console.maibot_webui_url", "MaiBot 网址", "填写后，更新提示中可直接跳转，例如 http://127.0.0.1:8001。", "str", check=_check_public_url),
    _F("console.trusted_proxies", "反向代理地址",
       "网页放在 nginx、frp 等反向代理后面时，填代理的 IP 或网段（如 127.0.0.1）。登录限流会按代理转发的真实 IP 计数；没用代理就留空。",
       "list_str", check=_check_proxy_list),

    # ---- environments ----
    _F("environments.railway", "Railway 临时机器", "允许使用 Railway 一次性机器（免密钥、匿名，用完等它自己过期）。", "bool"),
    _F("environments.freestyle", "Freestyle 临时机器",
       "允许使用 Freestyle 一次性机器（需要自己注册并填写 Freestyle 密钥，用完可主动销毁）。可以和 Railway 同时启用，互为备用。", "bool"),
    _F("environments.vm_platform", "优先用哪家临时机器",
       "Railway 和 Freestyle 都启用时优先使用哪一家；优先那家不可用（故障、当天额度用完、正被占用）"
       "会自动切换另一家。只启用一家时此项无作用。",
       "enum", options=[
           {"value": "railway", "label": "Railway（免密钥、匿名）"},
           {"value": "freestyle", "label": "Freestyle（要自己的 API key）"},
       ]),
    _F("environments.freestyle_api_key", "Freestyle 密钥",
       "在 https://dash.freestyle.sh 注册后创建一个 API key（只在创建时显示一次）。填写后不回显，"
       "也可用环境变量 FREESTYLE_API_KEY。",
       "secret"),
    _F("environments.ssh", "自有机器", "自有服务器，需要执行命令的任务优先使用。配置步骤：① 将「概况 → 运行状态」中的公钥添加到机器的 ~/.ssh/authorized_keys；② 在主模型的 AGENTS.md 中写明每台机器的配置和用途，主模型据此选择。", "ssh_list"),
    _F("environments.workspace_root", "远端目录", "远端机器上存放文件的目录。", "str", check=_check_workspace_root),
    _F("environments.memory_max", "内存上限", "单条命令可使用的最大内存，例如 1G。", "str", check=_check_memory_max),
    _F("environments.runtime_max_sec", "命令时长上限（秒）", "单条命令的最长运行时间。", "int", min=10, max=86400),
    _F("environments.local_mode", "本机隔离方式", "在本机运行命令时使用的隔离方式。", "enum", options=["systemd"], check=_check_local_mode),
    _F("environments.run_as", "运行账号", "在本机运行命令时使用的系统账号。", "str", check=_check_run_as),
    _F("environments.local_network", "允许访问本机和内网", "开启后，执行命令可访问本机服务与内网地址。默认关闭。", "bool"),
    _F("environments.max_parallel", "并行数", "最多同时运行的子 agent 数量。", "int", min=1, max=10),
    _F("environments.command_timeout_s", "默认命令时长（秒）", "单条命令的默认超时时间。", "int", min=10, max=3600),
    _F("environments.railway_daily_max", "每日 Railway 机器数", "每天最多使用的 Railway 临时机器数量（官方上限为 3）。", "int", min=1, max=3),
    _F("environments.freestyle_snapshot", "机器规格",
       "Freestyle 机器用哪个镜像，它决定 CPU、内存和磁盘：freestyle/ubuntu-sm 是 2 核 4G，"
       "freestyle/ubuntu 是 4 核 8G（免费档上限）。", "str", check=_check_freestyle_snapshot),
    _F("environments.freestyle_ttl_minutes", "最长存活（分钟）",
       "Freestyle 机器从创建时刻起算，到点自动删除。", "int", min=5, max=1440),
    _F("environments.freestyle_idle_minutes", "空闲休眠（分钟）",
       "Freestyle 机器无网络活动多久后自动休眠；休眠只算存储费，文件仍可读取。", "int", min=1, max=1440),
    _F("environments.freestyle_daily_max", "每日 Freestyle 机器数",
       "每天最多用几台 Freestyle 机器。免费档每月含 200 vCPU 小时 / 400 GiB 内存小时，"
       "按「分配的资源 × 占用时长」计费，默认的 2 核 4G 镜像一个月约能用 100 机时。"
       "实际用量通常远低于上限；当月额度用完后 Freestyle 会拒绝新建机器，任务改回本机执行。", "int", min=1, max=10),
    _F("environments.verify_enabled", "资讯实测", "选取部分资讯进行实际测试，结果显示在网页中。", "bool"),
    _F("environments.verify_per_round", "每轮实测条数", "每轮实际测试的资讯条数。", "int", min=1, max=3),
    _F("environments.verify_minutes", "实测时长（分钟）", "每条资讯的最长测试时间。", "int", min=1, max=60),

    # ---- profile ----
    _F("profile.batch_messages", "更新频率", "累计达到该条数的新消息后更新一次画像。", "int", min=10, max=1000),
    _F("profile.max_interval_hours", "最长间隔（小时）", "即使消息不足，超过该时长也会更新画像。", "int", min=1, max=72),
    _F("profile.backfill_days", "回溯天数", "首次加入群时回溯读取的天数。", "int", min=1, max=30),
    _F("profile.backfill_max_messages", "回溯条数", "首次加入群时最多回溯读取的消息条数。", "int", min=100, max=10000),
    _F("profile.weekly_day", "每周整理日", "0 表示周一，6 表示周日。", "int", min=0, max=6),
    _F("profile.read_interval_minutes", "读取间隔（分钟）", "读取新消息的时间间隔。", "int", min=1, max=120),

    # ---- storage ----
    _F("storage.data_dir", "数据目录", "仅可在服务器上修改。", "str",
       readonly=True, readonly_reason="仅可在服务器上修改"),

    # ---- group_space ----
    _F("group_space.enabled", "群空间", "允许使用群文件、群公告和群相册。", "bool"),
    _F("group_space.notice_per_day", "每日公告数", "每天最多发布的群公告数量。", "int", min=1, max=5),

    # ---- reader ----
    _F("reader.jina_enabled", "使用 Jina 读取",
       "优先使用 Jina 读取网页，失败时改用其他方式。", "bool"),
    _F("reader.jina_api_key", "Jina 密钥",
       "可选。填写后每分钟可调用 500 次（未填写为 20 次），可在 jina.ai 免费申请。", "secret"),
]

CONFIG_SECTIONS: list[dict[str, str]] = [
    {"id": "groups", "label": "服务群"},
    {"id": "focus", "label": "关注成员"},
    {"id": "feeds", "label": "资讯"},
    {"id": "goals", "label": "目标"},
    {"id": "topics", "label": "话题"},
    {"id": "delivery", "label": "主动发言"},
    {"id": "approval", "label": "任务审批"},
    {"id": "tasks", "label": "任务上限"},
    {"id": "models", "label": "模型"},
    {"id": "jev", "label": "快速判断"},
    {"id": "quick_judge", "label": "任务识别备用方案"},
    {"id": "session", "label": "连续对话"},
    {"id": "usage", "label": "用量提醒"},
    {"id": "console", "label": "网页"},
    {"id": "environments", "label": "运行环境"},
    {"id": "profile", "label": "群画像"},
    {"id": "storage", "label": "数据"},
    {"id": "group_space", "label": "群空间"},
    {"id": "reader", "label": "网页读取"},
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
    # Freestyle 的账号级 API key（2026-10-15）：只进不出；环境变量优先
    "environments.freestyle_api_key": {
        "setting": ("environments", "freestyle_api_key"),
        "env": ("FREESTYLE_API_KEY",),
    },
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
