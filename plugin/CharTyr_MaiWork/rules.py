"""网页可改的规则（docs/02 相关节）：kv["rules.override"] 存「改过的字段」，有效设置 = config.toml 被它覆盖。

- 可改项（白名单）：
  [delivery] quiet_hours、push_per_day；[topics] enabled、per_day、min_gap_hours；
  [approval] required、admins、exempt_groups、exempt_users、remind；
  [feeds] news_slots、max_items、web_min_avg、pool_min_avg、guides。
- 存法：只存改过的字段（{节: {字段: 值}}），写成和 config.toml 一样的值 = 自动清掉这条覆盖；
  一个字段校验不过，整次 patch 不落库（ValueError 中文原因）。**网页保存的设置存数据库，
  不写 config.toml**（写 plugins/ 会让全部插件重载）。
- 合并：app 在「Settings 的唯一出口」（get_settings）之后挂这一层——effective_settings 用
  dataclasses.replace 只换掉被覆盖的子节（基础 Settings 是 frozen dataclass，绝不能改它），
  并缓存：kv 的值作为缓存键的一部分，override 一变就重建；不改就和基础对象热更新一样零开销。
  所有模块读到的都是合并后的有效值（topics 开关关掉下一轮立刻停，靠的就是这里）。
- 校验和 config.toml 同一套语义：睡觉时段 HH:MM-HH:MM、news_slots 每项 HH:MM、
  QQ 号纯数字、数值范围按 config 的语义（越界在网页是 400 拒绝，不是夹值）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import fields as _dataclass_fields
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .config import (
    GroupSetting,
    SSHServer,
    Settings,
    _WORKSPACE_RE,
    normalize_domain,
)

logger = logging.getLogger("maiwork.rules")

KV_OVERRIDE = "rules.override"  # kv 键：{节: {字段: 值}}，只存改过的

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_QQ_RE = re.compile(r"^\d+$")


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


def _check_qq_list(field_zh: str) -> Any:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f'{field_zh}要填 QQ 号列表（纯数字），如 ["10001"]')
        out: list[str] = []
        for raw in value:
            s = raw.strip() if isinstance(raw, str) else str(raw)
            if not s or not _QQ_RE.match(s):
                raise ValueError(f"{field_zh}里的 {raw!r} 不是纯数字 QQ 号")
            if s not in out:
                out.append(s)
        return out

    return check


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


# 节 -> 字段 -> 校验函数。不在表里的字段一律「不认识的规则」拒掉。
_FIELDS: dict[str, dict[str, Any]] = {
    "delivery": {
        "quiet_hours": _check_quiet_hours,
        "push_per_day": _int_range("每天推送上限", 1, 10),
    },
    "topics": {
        "enabled": _bool_of("开话题开关"),
        "per_day": _int_range("每天开话题上限", 1, 10),
        "min_gap_hours": _int_range("开话题最小间隔（小时）", 1, 24),
    },
    "approval": {
        "required": _bool_of("派活批准开关"),
        "admins": _check_account_list("管理员"),
        "exempt_groups": _check_account_list("免批的群"),
        "exempt_users": _check_account_list("免批的人"),
        "remind": _bool_of("待批提醒开关"),
        "auto_review": _bool_of("自动审核开关"),
        "auto_review_daily": _int_range("每群每天最多自动批", 0, 50),
    },
    "feeds": {
        "news_slots": _check_news_slots,
        "max_items": _int_range("每批资讯上限", 1, 20),
        "web_min_avg": _float_range("上网页的五项平均门槛", 1.0, 5.0),
        "pool_min_avg": _float_range("进话题候选池的平均门槛", 1.0, 5.0),
        "guides": _bool_of("找文章开关"),
    },
}

# 网页写进来的「节: {字段: 值}」形态限制
_PATCH_SECTIONS_MAX = 4
_PATCH_FIELDS_MAX = 20


def _flat_base_value(base: Settings, section: str, field: str) -> Any:
    """config.toml 里这个字段的值（列表/元组转 list、数据类转成 dict 列表，方便和网页 JSON 比对）。

    字段不在 Settings 里（JevSetting 的 api_key 这类后加的字段也走 getattr 兜底）→ None。
    """
    if section == "groups" and field == "serve":
        # Settings.groups 是顶层字段（{群号: GroupSetting}），不是子节
        return [{"group": f"qq:{gid}", "workspace": g.workspace} for gid, g in base.groups.items()]
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


def _field_list_zh() -> str:
    return "、".join(f"{s}.{f}" for s, checks in _FIELDS.items() for f in checks)


def validate_patch(key: str, value: Any) -> tuple[str, Any]:
    """校验单个字段：("节.字段", 规范化后的值)；不合格抛 ValueError(中文)。"""
    key_s = str(key or "").strip()
    if key_s.count(".") != 1:
        raise ValueError(f'规则名要写成 "节.字段"，如 delivery.quiet_hours；收到 "{key_s}"')
    section, _, field = key_s.partition(".")
    checks = _FIELDS.get(section)
    if checks is None or field not in checks:
        raise ValueError(f'不认识的规则 "{key_s}"（能改的只有：{_field_list_zh()}）')
    return key_s, checks[field](value)


def _flatten_patch(body: Any) -> dict[str, Any]:
    """网页的 {节: {字段: 值}} → {"节.字段": 值}；形态不对抛 ValueError（中文）。"""
    if not isinstance(body, dict) or not body:
        raise ValueError('请求体要是 {节: {字段: 值}}，如 {"delivery": {"push_per_day": 5}}')
    if len(body) > _PATCH_SECTIONS_MAX:
        raise ValueError("一次最多改 4 个节的规则")
    flat: dict[str, Any] = {}
    for section, fields in body.items():
        s = str(section or "").strip()
        if s not in _FIELDS:
            raise ValueError(f'不认识的节 "{s}"（能改的只有：delivery、topics、approval、feeds）')
        if not isinstance(fields, dict) or not fields:
            continue
        for field, value in fields.items():
            flat[f"{s}.{str(field or '').strip()}"] = value
    if len(flat) > _PATCH_FIELDS_MAX:
        raise ValueError("一次最多改 20 个字段")
    return flat


# ----------------------------------------------------------------------
# 读 / 写 / 重置
# ----------------------------------------------------------------------


def read_override(store: Any) -> dict[str, dict[str, Any]]:
    """kv 里的覆盖（{节: {字段: 值}}）；没有或不是表 → {}。"""
    try:
        raw = store.kv_get(KV_OVERRIDE)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for section, fields in raw.items():
        if isinstance(fields, dict) and str(section) in _FIELDS:
            out[str(section)] = dict(fields)
    return out


def save_patch(store: Any, body: Any, *, base: Settings | None = None) -> dict[str, dict[str, Any]]:
    """校验 → 写 kv（和已有覆盖合并）。返回写库后的完整覆盖。

    - 全部字段校验通过才落库（一个失败整次不动）；
    - base 给了时：值和 config.toml 一样的字段视为「没改」，这条覆盖不写/清掉。
    """
    flat = _flatten_patch(body)
    validated: dict[str, dict[str, Any]] = {}
    for key, value in flat.items():
        key_s, v = validate_patch(key, value)
        section, _, field = key_s.partition(".")
        validated.setdefault(section, {})[field] = v
    # 值和 config.toml 一样 =「没改」：这条覆盖不写，已有的也清掉（恢复成文件值）。
    to_clear: list[str] = []
    if base is not None:
        for section in list(validated.keys()):
            for field in list(validated[section].keys()):
                if validated[section][field] == _base_value(base, section, field):
                    to_clear.append(f"{section}.{field}")
                    del validated[section][field]
            if not validated[section]:
                del validated[section]
    override = read_override(store)
    for key in to_clear:
        sec, _, fld = key.partition(".")
        if sec in override and fld in override.get(sec, {}):
            del override[sec][fld]
            if not override[sec]:
                del override[sec]
    for section, fields in validated.items():
        override.setdefault(section, {}).update(fields)
    override = {s: f for s, f in override.items() if f}
    with store.tx() as conn:
        store.kv_set(conn, KV_OVERRIDE, override)
    logger.info("规则覆盖已保存：%s", "、".join(f"{s}.{f}" for s in override for f in override[s]) or "（空）")
    return override


def reset_field(store: Any, key: str) -> dict[str, dict[str, Any]]:
    """清掉一个字段的覆盖（恢复用 config.toml 的值）。不存在也幂等；名字不认识抛 ValueError。"""
    key_s = str(key or "").strip()
    if key_s.count(".") != 1:
        raise ValueError(f'规则名要写成 "节.字段"，如 delivery.quiet_hours；收到 "{key_s}"')
    section, _, field = key_s.partition(".")
    if section not in _FIELDS or field not in _FIELDS[section]:
        raise ValueError(f'不认识的规则 "{key_s}"（能改的只有：{_field_list_zh()}）')
    override = read_override(store)
    fields = override.get(section)
    if fields is not None and field in fields:
        del fields[field]
        if not fields:
            del override[section]
        with store.tx() as conn:
            store.kv_set(conn, KV_OVERRIDE, override)
        logger.info("规则覆盖已重置：%s（恢复用 config.toml 的值）", key_s)
    return override


# ----------------------------------------------------------------------
# 合并（有效设置）
# ----------------------------------------------------------------------

# 合并时列表型字段换成 tuple（Settings 的字段类型是 tuple）
_LIST_FIELDS: dict[str, frozenset[str]] = {
    "approval": frozenset({"admins", "exempt_groups", "exempt_users"}),
    "feeds": frozenset({"news_slots"}),
}


def _base_value(base: Settings, section: str, field: str) -> Any:
    """config.toml 里这个字段的值（列表型转 list 方便和网页的 JSON 比对）。"""
    value = getattr(getattr(base, section), field)
    if isinstance(value, tuple):
        return list(value)
    return value


def effective_settings(base: Settings, override: Any) -> Settings:
    """base 被 override 覆盖后的有效设置（新对象或 base 本身）。

    只换掉被覆盖的子节（dataclasses.replace，一层；基础 Settings 是 frozen，绝不改它）。
    kv 里混进不认识的 / 坏掉的字段静默忽略（只用网页自己写过的白名单形状，
    容不得把有效设置搞炸）。
    """
    if not isinstance(override, dict) or not override:
        return base
    kwargs: dict[str, Any] = {}
    for section, fields in override.items():
        checks = _FIELDS.get(section)
        if checks is None or not isinstance(fields, dict):
            continue
        changes: dict[str, Any] = {}
        for field, value in fields.items():
            if field not in checks:
                continue
            try:
                v = checks[field](value)
            except ValueError:
                continue
            if field in _LIST_FIELDS.get(section, frozenset()):
                v = tuple(v)
            changes[field] = v
        if not changes:
            continue
        section_obj = getattr(base, section, None)
        if section_obj is None:
            continue
        kwargs[section] = replace(section_obj, **changes)
    if not kwargs:
        return base
    return replace(base, **kwargs)


# ----------------------------------------------------------------------
# 响应结构（GET /api/settings/rules 的数据层）
# ----------------------------------------------------------------------


def rules_view(base: Settings, store: Any, *, effective: Settings | None = None) -> dict[str, Any]:
    """{"values": 有效值, "overridden": [改过的"节.字段"], "defaults_from_file": config 值}。"""
    override = read_override(store)
    eff = effective if effective is not None else effective_settings(base, override)

    def _dump_section(setting_obj: Any, section: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in _FIELDS[section]:
            value = getattr(setting_obj, field)
            if isinstance(value, tuple):
                value = list(value)
            out[field] = value
        return out

    overridden = sorted(f"{s}.{f}" for s, fields in override.items() for f in fields)
    return {
        "values": {s: _dump_section(getattr(eff, s), s) for s in _FIELDS},
        "overridden": overridden,
        "defaults_from_file": {s: _dump_section(getattr(base, s), s) for s in _FIELDS},
    }


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
        raise ValueError('"direct" 没有任何隔离，只给本机开发测试用，只能在 config.toml 文件里改；网页只接受 "systemd"')
    if s != "systemd":
        raise ValueError('本机执行方式网页上只能填 "systemd"（"direct" 无隔离，只能在 config.toml 文件里改）')
    return s


def _check_serve_groups(value: Any) -> list[dict[str, str]]:
    """服务群整表校验（语义同 config._parse_groups：qq:纯数字、workspace 白名单、不重复）。"""
    if not isinstance(value, list):
        raise ValueError('服务群列表要写成 [{"group": "qq:号码", "workspace": "工作区名"}, ...]')
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"服务群条目不是表：{item!r}")
        grp = str(item.get("group") or "").strip()
        ws = str(item.get("workspace") or "").strip()
        if not grp.startswith("qq:"):
            raise ValueError(f'服务群 "{grp or item!r}" 不是 qq 平台（只支持 "qq:号码"）')
        gid = grp[3:].strip()
        if not gid.isdigit():
            raise ValueError(f'服务群 "{grp}" 的号码不是纯数字')
        if gid in seen:
            raise ValueError(f'服务群 "{grp}" 重复出现')
        seen.add(gid)
        ws = ws or f"g{gid}"
        if not _WORKSPACE_RE.match(ws):
            raise ValueError(f'服务群 "{grp}" 的工作区名 "{ws}" 不合法（只能用字母、数字、下划线、横线，1~64 个字符）')
        out.append({"group": f"qq:{gid}", "workspace": ws})
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

def _F(key: str, label: str, help: str, ftype: str, *, applies: str = "now",
       min: Any = None, max: Any = None, options: Any = None,
       readonly: bool = False, readonly_reason: str = "", check: Any = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "key": key, "label": label, "help": help, "type": ftype, "applies": applies,
        "readonly": bool(readonly),
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
    _F("groups.serve", "服务群列表", "写成 qq:群号。工作区名相同的群会共享群画像", "serve_groups"),

    # ---- focus ----
    _F("focus.max_members", "每群最多关注几个人", "", "int", min=1, max=20),
    _F("focus.personal_profile", "建个人画像", "关掉就只了解群，不了解个人", "bool"),
    _F("focus.personal_feeds", "出个人向资讯和构想", "只有本人和管理员看得到。需要先开「建个人画像」", "bool"),
    _F("focus.personal_per_day", "个人向资讯每次最多几条", "", "int", min=1, max=10),

    # ---- feeds ----
    _F("feeds.news_slots", "每天备资讯的时段", "每天这几个时间去找资讯（北京时间），比如 09:00", "time_list"),
    _F("feeds.news_jitter_minutes", "时段随机浮动（分钟）", "", "int", min=0, max=120),
    _F("feeds.ideas_per_day", "每天构想上限", "", "int", min=0, max=10),
    _F("feeds.max_items", "每批资讯上限", "", "int", min=1, max=20),
    _F("feeds.lookback_days", "去重回看天数", "和最近这么多天的资讯去重", "int", min=1, max=90),
    _F("feeds.blocked_domains", "来源屏蔽名单", "这些网站的内容不再出现", "list_str", check=_check_domain_list("来源屏蔽名单")),
    _F("feeds.web_min_avg", "上网页的平均分门槛", "满分 5 分，越高越挑", "float", min=1.0, max=5.0),
    _F("feeds.pool_min_avg", "进话题候选池的平均分门槛", "满分 5 分，越高越挑", "float", min=1.0, max=5.0),
    _F("feeds.guides", "同时找文章", "教程、好文章和好用的工具", "bool"),
    _F("feeds.collect_minutes", "每轮找资讯最多几分钟", "到点就把已经找到的交回来，不会白找", "int", min=1, max=60),

    # ---- goals ----
    _F("goals.propose", "主动提目标", "MaiWork 觉得群里该长期做件事时，提一个等你批准", "bool"),

    # ---- topics ----
    _F("topics.enabled", "冷场开话题", "群里冷场时开个话题", "bool"),
    _F("topics.speaker", "开话题谁说", "maiwork = MaiWork 直接说一句；maibot = 请 MaiBot 开口", "enum", options=["maiwork", "maibot"]),
    _F("topics.per_day", "每天开话题上限", "", "int", min=1, max=10),
    _F("topics.min_gap_hours", "两次开话题最小间隔（小时）", "", "int", min=1, max=24),
    _F("topics.candidate_ttl_hours", "话题候选有效期（小时）", "", "int", min=1, max=72),

    # ---- delivery ----
    _F("delivery.push_per_day", "每天推送上限", "包括开话题", "int", min=1, max=10),
    _F("delivery.quiet_hours", "睡觉时段", "这段时间不打扰群，比如 23:00-08:00", "str", check=_check_quiet_hours),
    _F("delivery.mention_ttl_minutes", "可提起清单有效期（分钟）", "MaiBot 聊天时可以顺口提起的时限", "int", min=1, max=1440),

    # ---- approval ----
    _F("approval.required", "派活要批准", "群友派的活，管理员批准后才开工", "bool"),
    _F("approval.admins", "bot 管理员", "写成 qq:账号", "list_str", check=_check_account_list("管理员")),
    _F("approval.exempt_groups", "免批的群", "这些群派的活不用批准。写成 qq:群号", "list_str", check=_check_account_list("免批的群")),
    _F("approval.exempt_users", "免批的人", "这些人派的活不用批准。写成 qq:账号", "list_str", check=_check_account_list("免批的人")),
    _F("approval.remind", "待批提醒", "超过 24 小时没人批，在群里提醒一次", "bool"),
    _F("approval.auto_review", "自动审核轻活", "调研、找东西、做个小网页这类低风险小活，直接开工", "bool"),
    _F("approval.auto_review_daily", "每群每天最多自动批", "0 = 关掉自动审核；要花钱、对外发消息、大工程照旧等你批",
       "int", min=0, max=50),

    # ---- jev ----
    _F("jev.enabled", "用 Jev 快速判断", "关掉会慢一些、贵一些", "bool"),
    _F("jev.timeout_ms", "Jev 超时（毫秒）", "", "int", min=200, max=1200),
    _F("jev.api_key", "Jev 密钥", "", "secret"),
    _F("jev.key_file", "Jev 密钥文件路径", "一般不用填", "str", check=_check_path_str("Jev 密钥文件路径")),
    _F("jev.api_url", "Jev 服务端地址", "一般不用改", "str", check=_check_jev_url),
    _F("jev.model", "Jev 模型名", "一般不用改", "str", check=_check_name_str("Jev 模型名")),

    # ---- usage ----
    _F("usage.alert_daily_tokens", "每日 token 提醒线", "0 = 不提醒", "int", min=0, max=10**9),
    _F("usage.alert_task_tokens", "单任务 token 提醒线", "0 = 不提醒", "int", min=0, max=10**9),

    # ---- models ----
    _F("models.context_window", "上下文长度（tokens）", "模型一次能记住多长。快满时 MaiWork 会先精简旧内容，再把更早的对话整理成摘要", "int", min=8192, max=2_000_000),

    # ---- tasks ----
    _F("tasks.token_limit", "单个任务最多用多少 token", "超过就自动暂停等你决定（0 = 不限）；点「继续」后重新计算", "int", min=0, max=10**9),
    _F("tasks.run_seconds", "单个任务最长做多久（秒）", "做满就自动暂停等你决定（0 = 不限，默认 10800 = 3 小时）；点「继续」后重新计时", "int", min=0, max=30*86400),

    # ---- console ----
    _F("console.listen", "网页监听地址", "写成 IP:端口", "str", check=_check_listen),
    _F("console.password", "管理员密码", "", "secret"),
    _F("console.public_url", "网页对外地址", "用来生成群链接，比如 https://maiwork.example.com", "str", check=_check_public_url),

    # ---- environments ----
    _F("environments.railway", "允许用 Railway 临时 VM", "", "bool"),
    _F("environments.ssh", "专用 SSH 机器", "派活时优先用这些机器", "ssh_list"),
    _F("environments.workspace_root", "远端工作区根目录", "", "str", check=_check_workspace_root),
    _F("environments.memory_max", "单条命令内存上限", "比如 512M、1G", "str", check=_check_memory_max),
    _F("environments.runtime_max_sec", "单条命令最长秒数", "", "int", min=10, max=86400),
    _F("environments.local_mode", "本机执行方式", "", "enum", options=["systemd"], check=_check_local_mode),
    _F("environments.run_as", "本机运行用户", "", "str", check=_check_run_as),
    _F("environments.max_parallel", "同时子 agent 数", "", "int", min=1, max=10),
    _F("environments.command_timeout_s", "子 agent 单条命令默认时长（秒）", "", "int", min=10, max=3600),
    _F("environments.railway_daily_max", "Railway 每日上限", "官方限制每天最多 3 台", "int", min=1, max=3),
    _F("environments.verify_per_round", "每轮实测条数", "", "int", min=1, max=3),
    _F("environments.verify_minutes", "单条实测时长上限（分钟）", "", "int", min=1, max=60),

    # ---- profile ----
    _F("profile.batch_messages", "攒多少条提炼一次", "", "int", min=10, max=1000),
    _F("profile.max_interval_hours", "提炼最长间隔（小时）", "", "int", min=1, max=72),
    _F("profile.backfill_days", "首次往回读几天", "", "int", min=1, max=30),
    _F("profile.backfill_max_messages", "首次最多读多少条", "", "int", min=100, max=10000),
    _F("profile.weekly_day", "每周整理日", "0 = 周一，6 = 周日", "int", min=0, max=6),
    _F("profile.read_interval_minutes", "读消息间隔（分钟）", "", "int", min=1, max=120),

    # ---- storage ----
    _F("storage.data_dir", "数据目录", "", "str",
       readonly=True, readonly_reason="只能在服务器上改"),

    # ---- group_space ----
    _F("group_space.enabled", "开群空间", "群文件、公告和相册", "bool"),
    _F("group_space.notice_per_day", "每天群公告上限", "", "int", min=1, max=5),
]

CONFIG_SECTIONS: list[dict[str, str]] = [
    {"id": "groups", "label": "服务群"},
    {"id": "focus", "label": "关注成员"},
    {"id": "feeds", "label": "资讯"},
    {"id": "goals", "label": "目标"},
    {"id": "topics", "label": "冷场开话题"},
    {"id": "delivery", "label": "推送"},
    {"id": "approval", "label": "派活批准"},
    {"id": "tasks", "label": "任务安全网"},
    {"id": "models", "label": "模型"},
    {"id": "jev", "label": "快速判断"},
    {"id": "usage", "label": "用量提醒"},
    {"id": "console", "label": "网页"},
    {"id": "environments", "label": "执行环境"},
    {"id": "profile", "label": "群画像"},
    {"id": "storage", "label": "数据存储"},
    {"id": "group_space", "label": "群空间"},
]

CONFIG_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in CONFIG_SCHEMA}

KV_CONFIG_OVERRIDE = "config.override"  # 已废弃的 kv 键名（迁移用；数据库不再存配置覆盖层）

# secret 字段 → 配置文件字段 / 优先级更高的环境变量（判 source 用；网页只进不出，
# 值直接写进 config.toml 的对应键——明文，用户明确同意）
SECRET_FIELDS: dict[str, dict[str, Any]] = {
    "jev.api_key": {
        "setting": ("jev", "api_key"),
        "env": ("TYPESAFE_API_KEY", "TYPESAFE_KEY_FILE"),
    },
    "console.password": {"setting": ("console", "password"), "env": ()},
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
    - 普通字段：写进 config.toml 对应键；
    - secret 字段：没给 / 给 "" = 不改；给 null = 清空（文件里写 ""）；
      给非空字符串 = 写进 config.toml（明文）。console.password 额外要 body 里
      带正确的 current_password，改成功后旧的自动生成密码哈希失效。
    返回实际写进文件的「节.字段」清单（调用方负责写后应用）。
    """
    from . import config_file

    if not isinstance(body, dict):
        raise ValueError('请求体要写成 {"节.字段": 值}，如 {"topics.per_day": 5}')
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
                             "[extensions] 走「设置 → 扩展」、plugin.config_version 不可改）")
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
        }
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
        fields = [_field_view(spec) for spec in CONFIG_SCHEMA if spec["key"].split(".", 1)[0] == sid]
        if fields:
            sections.append({"id": sid, "label": meta["label"], "fields": fields})
    return {"file": "config.toml", "sections": sections, "reload_pending": []}
