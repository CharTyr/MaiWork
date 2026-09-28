"""网页管理 MCP 扩展的数据层（docs/02 §10、docs/07 §10.9）。

存法（**网页保存的东西存数据库，不写回 config.toml**——写 plugins/ 会触发全部插件重载）：

- kv["extensions.mcp"] = [{name, url, tools, roles, enabled, timeout_s, header_names}]
  （header_names 只有头的**名字**，值一个字母都不进 kv）；
- 每个头的值存 secrets["mcp.<name>.<头名字>"]（**只进不出**：任何接口、日志、
  tool_calls 都不回显；网页上只显示「已填」）。
- config.toml 里 [[extensions.mcp]] 的网页开关：kv["extensions.mcp.disabled"] = [名字] 名单。

合并规则（merged）：
- 网页里有同名的 → 以网页为准（整条替换 config 那条）；
- config 的条目仍生效；disabled 名单只把「开」盖成「关」（config 里就是关的网页开不了）。
- 网页删除只删网页添加的条目和它名下的 secrets；config 的删不了（接口层挡 409）。

web 条目校验和 config.py 的 [[extensions.mcp]] 同一套规则（名字 [A-Za-z0-9_-]{1,32}、
url 必须 https://、roles 只允许 worker/main、timeout_s ≥1）。

roles 的口径：网页提交必须是列表、只能含 worker/main、**不能为空**——不合法记
problems（接口层 → 400）；没传时新增默认 ["worker"]、修改保持原值。config.toml
里的坏值仍按 config.py 的容错回落 worker（管理员手写配置，不拖垮）。
"""

from __future__ import annotations

import logging
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .config import McpExtensionSetting, Settings

logger = logging.getLogger("maiwork.extensions_web")

KV_MCP = "extensions.mcp"                 # 网页加的 MCP 条目（不含头值）
KV_MCP_DISABLED = "extensions.mcp.disabled"  # config 来源被网页关掉的名单
_SECRET_PREFIX = "mcp"                    # secrets 名：mcp.<扩展名>.<头名>

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# HTTP 头名字（RFC 7230 token），只允许 ASCII
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_TOOL_NAMES_MAX = 50


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def secret_name(ext_name: str, header_name: str) -> str:
    return f"{_SECRET_PREFIX}.{ext_name}.{header_name}"


def url_no_query(url: str) -> str:
    """完整地址但去掉查询串和 fragment（防 query 里的 token 被回显/落日志）。"""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _valid_header_name(name: str) -> bool:
    try:
        name.encode("ascii")
    except (UnicodeEncodeError, AttributeError):
        return False
    return bool(_HEADER_NAME_RE.match(name))


# ----------------------------------------------------------------------
# 读：kv / secrets → 规范化条目
# ----------------------------------------------------------------------


def _web_raw_entries(store: Any) -> list[dict[str, Any]]:
    raw = store.kv_get(KV_MCP)
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _web_entry_to_setting(store: Any, entry: dict[str, Any]) -> McpExtensionSetting | None:
    """kv 里一条网页条目 → McpExtensionSetting（头值从 secrets 解析进来）。
    坏条目（被手工改坏的）整条跳过并记日志，不拖垮合并。
    """
    name = str(entry.get("name") or "").strip()
    url = str(entry.get("url") or "").strip()
    if not _NAME_RE.match(name) or not url.startswith("https://"):
        logger.warning("kv 里的网页 MCP 条目 %s 不合法，已跳过", name or "?")
        return None
    headers: dict[str, str] = {}
    names_raw = entry.get("header_names")
    if isinstance(names_raw, list):
        for h in names_raw:
            h_s = str(h or "").strip()
            if not h_s or not _valid_header_name(h_s):
                continue
            v = store.secret_get(secret_name(name, h_s))
            if v:
                headers[h_s] = v
    tools: list[str] = []
    if isinstance(entry.get("tools"), list):
        for t in entry["tools"]:
            t_s = str(t or "").strip()
            if t_s and t_s not in tools:
                tools.append(t_s)
    roles: list[str] = []
    if isinstance(entry.get("roles"), list):
        roles = sorted({str(r).strip() for r in entry["roles"] if str(r).strip() in ("worker", "main")})
    if not roles:
        roles = ["worker"]
    try:
        timeout_s = max(1, int(entry.get("timeout_s") or 20))
    except (TypeError, ValueError):
        timeout_s = 20
    return McpExtensionSetting(
        name=name,
        url=url,
        enabled=bool(entry.get("enabled", True)),
        headers=MappingProxyType(headers),
        tools=tuple(tools),
        roles=tuple(roles),
        timeout_s=timeout_s,
    )


def _disabled_names(store: Any) -> list[str]:
    raw = store.kv_get(KV_MCP_DISABLED)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for n in raw:
        n_s = str(n or "").strip()
        if _NAME_RE.match(n_s) and n_s not in out:
            out.append(n_s)
    return out


def merged(settings: Settings, store: Any) -> list[tuple[McpExtensionSetting, str]]:
    """合并后的 MCP 条目：[(setting, "web"|"config")]。config 在前、网页在后。

    - 网页同名 → 以网页为准（config 那条整条不出现）；
    - config 条目在 disabled 名单里 → enabled 盖成 False。
    """
    import dataclasses

    config_entries = list(getattr(getattr(settings, "extensions", None), "mcp", ()) or ())
    disabled = set(_disabled_names(store))
    web: list[McpExtensionSetting] = []
    if store is not None:
        for raw in _web_raw_entries(store):
            s = _web_entry_to_setting(store, raw)
            if s is not None:
                web.append(s)
    web_names = {s.name for s in web}
    out: list[tuple[McpExtensionSetting, str]] = []
    for e in config_entries:
        if e.name in web_names:
            continue  # 网页同名，以网页为准
        if e.enabled and e.name in disabled:
            e = dataclasses.replace(e, enabled=False)
        out.append((e, "config"))
    out.extend((s, "web") for s in web)
    return out


def merged_entries(settings: Settings, store: Any) -> tuple[McpExtensionSetting, ...]:
    """只有 setting 的合并结果（extensions.py 连接用）。"""
    return tuple(s for s, _ in merged(settings, store))


def source_of(settings: Settings, store: Any, name: str) -> str | None:
    """这个合并后条目的来源："web" / "config" / None（没有这个名字）。"""
    for s, source in merged(settings, store):
        if s.name == name:
            return source
    return None


# ----------------------------------------------------------------------
# 视图（GET /api/extensions 的单项结构；头值绝不回显）
# ----------------------------------------------------------------------


def _item_view(setting: McpExtensionSetting, source: str, runtime: Any, *, store: Any = None) -> dict[str, Any]:
    ok = bool(getattr(runtime, "ok", False)) if runtime is not None else False
    tools_map = getattr(runtime, "tools", None) if runtime is not None else None
    registered = sorted(tools_map.keys())[:_TOOL_NAMES_MAX] if isinstance(tools_map, dict) else []
    error = str(getattr(runtime, "error", "") or "") if runtime is not None else ""
    # 双保险：运行时 error 里万一带了头值，这里再遮一次
    for v in dict(setting.headers).values():
        if v:
            error = error.replace(v, "***")
    search_role = ""
    if store is not None:
        try:
            from . import search_binding

            search_role = search_binding.search_role_of(store, setting.name)
        except Exception:
            search_role = ""
    return {
        "name": setting.name,
        "url": url_no_query(setting.url),
        "source": source,
        "enabled": bool(setting.enabled),
        "search_role": search_role,
        "ok": ok,
        "tools": len(tools_map) if isinstance(tools_map, dict) else 0,
        "tool_names": registered,
        "error": error,
        "roles": sorted(setting.roles),
        "tools_filter": list(setting.tools),
        "header_names": sorted(dict(setting.headers).keys()),
        "headers_set": bool(dict(setting.headers)),
        "timeout_s": int(setting.timeout_s),
    }


def list_items(settings: Settings, store: Any, extensions: Any) -> list[dict[str, Any]]:
    """GET /api/extensions 的 mcp 段（合并后的全部条目）。"""
    out: list[dict[str, Any]] = []
    for setting, source in merged(settings, store):
        runtime = None
        if extensions is not None:
            getter = getattr(extensions, "runtime_of", None)
            if callable(getter):
                runtime = getter(setting.name)
        out.append(_item_view(setting, source, runtime, store=store))
    return out


# ----------------------------------------------------------------------
# 写：校验 + 落库
# ----------------------------------------------------------------------


def _validated_roles(body: dict[str, Any], problems: list[str]) -> list[str] | None:
    """网页提交的 roles：没传（缺字段 / None）→ None，由调用方决定默认还是保持原值。

    必须是列表、只能含 "worker" / "main"、**不能为空**；不合法往 problems 里记
    （接口层统一 → 400），不再静默回落 worker。config.toml 那侧的容错在 config.py，
    是另一套（管理员手写配置，坏值回落 worker 不拖垮）。
    """
    if body.get("roles") is None:
        return None
    raw = body.get("roles")
    if not isinstance(raw, list):
        problems.append("roles 要是列表")
        return None
    values = [str(r).strip() for r in raw]
    if [v for v in values if v and v not in ("worker", "main")]:
        problems.append('roles 只认 "worker" / "main"')
    roles = sorted({v for v in values if v in ("worker", "main")})
    if not roles:
        problems.append("roles 不能为空（至少选一个：worker / main）")
    return roles or None


def _validate_public_fields(body: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """name/url/tools/roles/enabled/timeout_s 的规范化（headers 单独处理）。"""
    out: dict[str, Any] = {}
    name = str(body.get("name") or "").strip()
    if not _NAME_RE.match(name):
        problems.append("扩展名字不合法（只能用字母、数字、下划线、横线，1~32 个字符）")
    out["name"] = name
    url = str(body.get("url") or "").strip()
    if not url:
        problems.append("MCP 端点地址必填")
    elif not url.startswith("https://"):
        problems.append("MCP 端点地址必须 https:// 开头（密钥走这个请求，http 会泄露）")
    out["url"] = url
    tools: list[str] = []
    tools_raw = body.get("tools") or []
    if not isinstance(tools_raw, list):
        problems.append("tools 要是列表")
    else:
        for t in tools_raw:
            t_s = str(t or "").strip()
            if t_s and t_s not in tools:
                tools.append(t_s)
    out["tools"] = tools
    roles = _validated_roles(body, problems)
    out["roles"] = roles if roles is not None else ["worker"]  # 新增没传 → 默认 worker
    out["enabled"] = bool(body.get("enabled", True))
    try:
        timeout_s = int(body.get("timeout_s") or 20)
    except (TypeError, ValueError):
        problems.append("timeout_s 要是整数（秒）")
        timeout_s = 20
    out["timeout_s"] = max(1, timeout_s)
    return out


def _validate_headers(raw: Any, *, allow_empty: bool, problems: list[str]) -> dict[str, str]:
    """网页提交的 headers {名字: 值}。allow_empty=False（新增）时值必须非空；
    True（修改）时空字符串 = 不改这个头（外面过滤）。
    值必须 ASCII（要进 HTTP 请求头）；非 ASCII 记问题。
    """
    out: dict[str, str] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        problems.append("headers 要是 {名字: 值} 的表")
        return out
    for k, v in raw.items():
        k_s = str(k or "").strip()
        if not _valid_header_name(k_s):
            problems.append(f"请求头名字 {k_s[:40]!r} 不合法")
            continue
        v_s = str(v or "")
        if not v_s:
            if allow_empty:
                out[k_s] = ""
            else:
                problems.append(f"请求头 {k_s} 的值不能为空（新增时；要删头请在修改里用 remove_headers）")
            continue
        try:
            v_s.encode("ascii")
        except UnicodeEncodeError:
            problems.append(f"请求头 {k_s} 的值只支持 ASCII（要放进 HTTP 请求头）")
            continue
        out[k_s] = v_s
    return out


def create(store: Any, settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """新增一个网页 MCP 条目。ValueError 校验失败；FileExistsError 重名（网页/config 都算）。

    返回 kv 里存的条目结构（不含头值）。同一事务写 kv + secrets。
    """
    problems: list[str] = []
    public = _validate_public_fields(body, problems)
    headers = _validate_headers(body.get("headers"), allow_empty=False, problems=problems)
    if problems:
        raise ValueError("；".join(problems))
    name = public["name"]
    if source_of(settings, store, name) is not None:
        raise FileExistsError(f"已经有叫「{name}」的 MCP 扩展了（重名）")
    entry = {
        "name": name,
        "url": public["url"],
        "enabled": public["enabled"],
        "tools": public["tools"],
        "roles": public["roles"],
        "timeout_s": public["timeout_s"],
        "header_names": sorted(headers.keys()),
    }
    entries = _web_raw_entries(store)
    entries.append(entry)
    with store.tx() as conn:
        store.kv_set(conn, KV_MCP, entries)
        for h_name, h_value in headers.items():
            if h_value:
                store.secret_set(conn, secret_name(name, h_name), h_value)
    logger.info("网页新增 MCP 扩展 %s（%s），头 %d 个（值只进不出）", name, url_no_query(entry["url"]), len(headers))
    return entry


def update(store: Any, name: str, body: dict[str, Any]) -> dict[str, Any]:
    """修改一个网页 MCP 条目（只允许 source=web，接口层先挡 config）。

    不传 / None 的字段保持原值；headers 里空字符串 = 不改这个头；remove_headers 删头。
    KeyError 没有这个网页条目；ValueError 校验失败。
    """
    name = str(name or "").strip()
    entries = _web_raw_entries(store)
    idx = next((i for i, e in enumerate(entries) if str(e.get("name") or "") == name), -1)
    if idx < 0:
        raise KeyError(name)
    current = dict(entries[idx])
    problems: list[str] = []
    # url / tools / roles / enabled / timeout_s：传了才改（None 视为没传）
    if body.get("url") is not None:
        url = str(body.get("url") or "").strip()
        if not url.startswith("https://"):
            problems.append("MCP 端点地址必须 https:// 开头（密钥走这个请求，http 会泄露）")
        else:
            current["url"] = url
    if body.get("tools") is not None:
        tools_raw = body.get("tools")
        if not isinstance(tools_raw, list):
            problems.append("tools 要是列表")
        else:
            tools: list[str] = []
            for t in tools_raw:
                t_s = str(t or "").strip()
                if t_s and t_s not in tools:
                    tools.append(t_s)
            current["tools"] = tools
    roles = _validated_roles(body, problems)
    if roles is not None:
        current["roles"] = roles  # 没传 → 保持原值
    if body.get("enabled") is not None:
        current["enabled"] = bool(body.get("enabled"))
    if body.get("timeout_s") is not None:
        try:
            current["timeout_s"] = max(1, int(body.get("timeout_s")))
        except (TypeError, ValueError):
            problems.append("timeout_s 要是整数（秒）")
    headers = _validate_headers(body.get("headers"), allow_empty=True, problems=problems) if "headers" in body else {}
    remove_headers: list[str] = []
    remove_raw = body.get("remove_headers")
    if remove_raw is not None:
        if not isinstance(remove_raw, list):
            problems.append("remove_headers 要是列表")
        else:
            for h in remove_raw:
                h_s = str(h or "").strip()
                if h_s and h_s not in remove_headers:
                    remove_headers.append(h_s)
    if problems:
        raise ValueError("；".join(problems))
    # 头名集合：旧的 + 新增/覆盖的 - 删掉的（空字符串 = 不改，不进集合也不动值）
    names = {str(h) for h in (current.get("header_names") or []) if str(h)}
    for h_name, h_value in headers.items():
        if h_value:
            names.add(h_name)
    for h_name in remove_headers:
        names.discard(h_name)
    current["header_names"] = sorted(names)
    entries[idx] = current
    with store.tx() as conn:
        store.kv_set(conn, KV_MCP, entries)
        for h_name, h_value in headers.items():
            if h_value:  # 空字符串 = 不改这个头
                store.secret_set(conn, secret_name(name, h_name), h_value)
        if remove_headers:
            prefix = secret_name(name, "")
            rows = conn.execute("SELECT name FROM secrets WHERE name LIKE ?", (f"{secret_name(name, '')}%",)).fetchall()
            for row in rows:
                s_name = str(row["name"])
                if not s_name.startswith(prefix):
                    continue
                h_name = s_name[len(prefix):]
                if h_name in remove_headers:
                    conn.execute("DELETE FROM secrets WHERE name=?", (s_name,))
    logger.info("网页修改 MCP 扩展 %s（头值只进不出）", name)
    return current


def delete(store: Any, name: str) -> None:
    """删掉一个网页 MCP 条目和它名下的全部 secrets。KeyError 没有这个网页条目。"""
    name = str(name or "").strip()
    entries = _web_raw_entries(store)
    kept = [e for e in entries if str(e.get("name") or "") != name]
    if len(kept) == len(entries):
        raise KeyError(name)
    prefix = secret_name(name, "")
    with store.tx() as conn:
        store.kv_set(conn, KV_MCP, kept)
        rows = conn.execute("SELECT name FROM secrets WHERE name LIKE ?", (f"{prefix}%",)).fetchall()
        for row in rows:
            s_name = str(row["name"])
            if s_name.startswith(prefix):
                conn.execute("DELETE FROM secrets WHERE name=?", (s_name,))
        # 顺手从 disabled 名单摘掉（防御：同名 config 条目以后加回来不该是关的）
        disabled = [n for n in _disabled_names(store) if n != name]
        if disabled != _disabled_names(store):
            store.kv_set(conn, KV_MCP_DISABLED, disabled)
    logger.info("网页删除 MCP 扩展 %s", name)


def toggle(store: Any, settings: Settings, name: str, enabled: bool) -> None:
    """开关一次。网页条目改它自己的 enabled；config 条目走 disabled 名单
    （名单只把「开」盖成「关」——config 里就是关的网页开不了）。
    KeyError 没有这个名字；ValueError config 来源且配置文件里就是关的。
    """
    name = str(name or "").strip()
    source = source_of(settings, store, name)
    if source is None:
        raise KeyError(name)
    if source == "web":
        entries = _web_raw_entries(store)
        for e in entries:
            if str(e.get("name") or "") == name:
                e["enabled"] = bool(enabled)
                break
        with store.tx() as conn:
            store.kv_set(conn, KV_MCP, entries)
    else:
        cfg = next(s for s, src in merged(settings, store) if s.name == name and src == "config")
        cfg_enabled_in_file = any(
            e.name == name and e.enabled
            for e in (getattr(getattr(settings, "extensions", None), "mcp", ()) or ())
        )
        if enabled and not cfg_enabled_in_file:
            raise ValueError("这个扩展在配置文件里就是关的，网页开不了——请改 config.toml")
        disabled = _disabled_names(store)
        if enabled:
            disabled = [n for n in disabled if n != name]
        elif name not in disabled:
            disabled.append(name)
        with store.tx() as conn:
            store.kv_set(conn, KV_MCP_DISABLED, disabled)
        _ = cfg
    logger.info("网页把 MCP 扩展 %s 开关拨成 %s", name, "开" if enabled else "关")


def stored_headers_for(store: Any, settings: Settings, name: str) -> dict[str, str]:
    """试连接口用：取这个名字（网页或 config）已存的头值。"""
    for s, _ in merged(settings, store):
        if s.name == name:
            return dict(s.headers)
    return {}
