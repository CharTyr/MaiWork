"""一次性迁移（启动时跑，幂等）：数据库里的网页配置覆盖层 → config.toml。

2026-10 改造：网页「设置 → 全部配置 / 模型」改成直接写插件自己的 config.toml，
数据库不再存配置覆盖。启动时把旧数据搬过来：

- kv["config.override"]（{节: {字段: 值}}）→ 写进文件对应键；
- secrets：search_api_key / jev_api_key / console_password / model_api_key →
  写进 [search].api_key / [jev].api_key / [console].password / [models].api_key；
- kv["models.settings"]（整组网页模型设置）→ 写进 [models] 各键；
- 文件里该键已有非空值且不同的，以数据库为准（之前网页的优先级更高）；
- 写成功后从数据库删掉这些键；config 里配了 console.password 后，旧的自动生成
  密码哈希（secrets.admin_password_hash）失效，一并删掉；
- 迁移日志绝不打印任何密钥值（只打键名）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import config_file

logger = logging.getLogger("maiwork.migrations")

KV_CONFIG_OVERRIDE = "config.override"
KV_MODELS_SETTINGS = "models.settings"

# secrets 键 → 配置「节.字段」
_SECRET_KEYS: dict[str, str] = {
    "jev_api_key": "jev.api_key",
    "console_password": "console.password",
}

# 老的 search_api_key（2026-10 之前网页填的搜索密钥）：[search] 段已整个删除，
# 搜索改走扩展绑定；配置里的 [search] 由 migrate_search_config_to_extension 搬走，
# 这个孤儿密钥直接清掉（搜索密钥都在扩展的 mcp.<名>.<头名> 里）。
_LEGACY_SEARCH_SECRET = "search_api_key"

# kv["models.settings"] 的键 → [models] 的键（checked_at / available 不进文件，进 kv）
_MODELS_FIELDS = (
    "base_url", "api_key", "main", "main_backup", "worker", "worker_backup",
    "retries", "retry_delay_s",
)


def _read_file_nonempty(text: str) -> dict[str, Any]:
    """文件里已写且非空的值（{节.字段: 值}）；文件坏了按「全空」处理（不拖垮迁移）。"""
    out: dict[str, Any] = {}
    try:
        import tomlkit

        doc = tomlkit.parse(text)
    except Exception:
        return out
    for section, body in doc.items():
        if not isinstance(body, dict):
            continue
        for field, value in body.items():
            if isinstance(value, str) and not value:
                continue
            if isinstance(value, (list,)) and not value:
                continue
            out[f"{section}.{field}"] = value
    return out


def migrate_db_config_to_file(store: Any, plugin_dir: Path | str, data_dir: Path | str) -> list[str]:
    """把数据库里的网页配置搬进 config.toml；返回实际写进文件的「节.字段」清单。

    幂等：数据库里没东西了 → []，文件不动。写文件失败抛 ConfigFileError（app 自己
    兜住记日志，不拖垮启动）。
    """
    # 1. 收集要写的键
    writes: dict[str, Any] = {}
    # 1a. config.override（普通字段整组搬；和文件值一样的不用写，但 kv 键照样清）
    db_plain: dict[str, Any] = {}
    try:
        raw = store.kv_get(KV_CONFIG_OVERRIDE)
    except Exception:
        raw = None
    if isinstance(raw, dict):
        for section, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                db_plain[f"{section}.{field}"] = value
    # 1b. secrets（只进不出的两个密钥）
    db_secrets: dict[str, str] = {}
    for name, key in _SECRET_KEYS.items():
        try:
            value = str(store.secret_get(name) or "")
        except Exception:
            value = ""
        if value:
            db_secrets[key] = value
    # 老的 search_api_key：[search] 段没了，直接清掉（幂等；不打值）
    legacy_search_key = ""
    try:
        legacy_search_key = str(store.secret_get(_LEGACY_SEARCH_SECRET) or "")
    except Exception:
        legacy_search_key = ""
    # 1c. models.settings + model_api_key
    db_models: dict[str, Any] = {}
    try:
        raw_models = store.kv_get(KV_MODELS_SETTINGS)
    except Exception:
        raw_models = None
    if isinstance(raw_models, dict):
        for field in _MODELS_FIELDS:
            if field == "api_key":
                continue
            value = raw_models.get(field)
            if value is None:
                continue
            db_models[f"models.{field}"] = value
    try:
        model_key = str(store.secret_get("model_api_key") or "")
    except Exception:
        model_key = ""
    if model_key:
        db_models["models.api_key"] = model_key

    all_db = {**db_plain, **db_secrets, **db_models}
    if not all_db:
        if legacy_search_key:
            with store.tx() as conn:
                store.secret_delete(conn, _LEGACY_SEARCH_SECRET)
            logger.info("旧的搜索密钥（secrets.search_api_key）已清掉（[search] 段已废，搜索走扩展绑定）")
        return []

    # 2. 文件里已有非空值且和数据库一样的 → 不用写；不同 → 数据库为准（写）
    try:
        file_text = config_file.read_text(plugin_dir)
    except config_file.ConfigFileError:
        file_text = ""
    file_nonempty = _read_file_nonempty(file_text)
    for key, value in all_db.items():
        if key in file_nonempty and file_nonempty[key] == value:
            continue
        writes[key] = value

    # 3. 写文件（整批一次：一次备份、一次覆盖写）
    changed: list[str] = []
    if writes:
        config_file.write_fields(plugin_dir, data_dir, writes)
        changed = sorted(writes)
        logger.info("数据库里的网页配置已搬进 config.toml：%s", "、".join(changed))

    # 4. 清数据库（写完文件才清；写失败抛异常、数据库原样不动，下次启动再迁）
    with store.tx() as conn:
        if db_plain:
            conn.execute("DELETE FROM kv WHERE key=?", (KV_CONFIG_OVERRIDE,))
        for name in _SECRET_KEYS:
            if db_secrets.get(_SECRET_KEYS[name]):
                store.secret_delete(conn, name)
        if isinstance(raw_models, dict):
            conn.execute("DELETE FROM kv WHERE key=?", (KV_MODELS_SETTINGS,))
        if model_key:
            store.secret_delete(conn, "model_api_key")
        # config 里配了 console.password → 旧的自动生成哈希失效
        if db_secrets.get("console.password"):
            store.secret_delete(conn, "admin_password_hash")
        if legacy_search_key:
            store.secret_delete(conn, _LEGACY_SEARCH_SECRET)
    return changed


# ----------------------------------------------------------------------
# [search] 段 → MCP 扩展 + 搜索绑定（2026-10；搜索只走扩展，内置服务全删）
# ----------------------------------------------------------------------

# provider → (默认扩展名, MCP 端点 URL, 是否要 key)
_PROVIDER_EXT: dict[str, tuple[str, str]] = {
    "tavily_mcp": ("tavily", ""),                       # URL 用老配置的 mcp_url
    "tavily": ("tavily", "https://mcp.tavily.com/mcp"),  # 直连改走 Tavily 官方 MCP
    "exa": ("exa", "https://mcp.exa.ai/mcp"),
    "you": ("you", "https://api.you.com/mcp"),
}


def _url_key(url: str) -> str:
    """URL 归一化做「同一家」比较：去 query / fragment、小写 scheme+host+path、
    path 也小写、去末尾斜杠（各家 MCP 端点的 path 实测不区分大小写，query 里常带 key 参数）。"""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    path = parts.path.rstrip("/").lower()
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def _find_ext_by_url(store: Any, url: str) -> str | None:
    """网页扩展里 URL 相同（忽略 query 里的 key 参数、大小写、末尾斜杠）的那个的名字。"""
    from . import extensions_web

    want = _url_key(url)
    if not want:
        return None
    for entry in extensions_web._web_raw_entries(store):
        if _url_key(str(entry.get("url") or "")) == want:
            return str(entry.get("name") or "")
    return None


def _pick_tools(tool_specs: list[dict] | None, *, provider: str) -> tuple[str, str]:
    """(搜索工具名, 抓正文工具名)：优先按名字/描述猜；拿不到清单按各家惯例。"""
    from .search_binding import guess_tool_role

    search_tool = ""
    extract_tool = ""
    for spec in tool_specs or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        guess = guess_tool_role(name, str(spec.get("description") or ""))
        if guess == "search" and not search_tool:
            search_tool = name
        elif guess == "extract" and not extract_tool:
            extract_tool = name
    if not search_tool:
        search_tool = {
            "tavily_mcp": "tavily-search", "tavily": "tavily-search",
            "exa": "web_search_exa", "you": "you-search",
        }.get(provider, "")
    if not extract_tool:
        extract_tool = {
            "tavily_mcp": "tavily-extract", "tavily": "tavily-extract", "you": "you-contents",
        }.get(provider, "")
    return search_tool, extract_tool


def migrate_search_config_to_extension(
    store: Any,
    plugin_dir: Path | str,
    data_dir: Path | str,
    *,
    list_tools: Any = None,
) -> bool:
    """config.toml 里的 [search] 段 → 一个 MCP 扩展 + kv 搜索绑定；幂等，返回有没有干活。

    - 没有 [search] 段 → False（什么都不动）；
    - provider 空 / 不认识 → 不建扩展，但这段死配置清掉（返回 False）；
    - URL 相同（忽略 query、大小写、末尾斜杠）的现有网页扩展 → 复用，不新建、不改它的密钥；
      没有 → 新建（密钥进 secrets["mcp.<名>.Authorization"，值绝不落日志）；
    - 已有搜索绑定（管理员手动配过）→ 不覆盖；
    - 绑定工具：list_tools(名字) 给了就拿真实清单挑名字像 search / extract 的；
      拿不到（启动时扩展还没连）按各家惯例先存名字，不必当场验证；
    - 最后从 config.toml 删掉整个 [search] 段（config_file 会备份）。
    日志只打名字，绝不打密钥值。
    """
    import tomlkit

    try:
        text = config_file.read_text(plugin_dir)
    except config_file.ConfigFileError:
        return False
    try:
        doc = tomlkit.parse(text)
    except Exception:
        return False
    section = doc.get("search")
    if section is None or not isinstance(section, dict):
        return False
    provider = str(section.get("provider") or "").strip().lower()
    api_key = str(section.get("api_key") or "").strip()
    mcp_url = str(section.get("mcp_url") or "").strip()

    did_something = False
    if provider in _PROVIDER_EXT and (api_key or provider == "you"):
        default_name, default_url = _PROVIDER_EXT[provider]
        url = mcp_url if provider == "tavily_mcp" and mcp_url else default_url
        if provider == "you" and not api_key and "profile=" not in url:
            url += ("&" if "?" in url else "?") + "profile=free"
        # 1. 扩展：同 URL 复用，否则新建（密钥进 secrets，不落日志）
        from . import extensions_web

        name = _find_ext_by_url(store, url)
        if name:
            logger.info("[search] 迁移：复用已有的 MCP 扩展 %s（URL 相同）", name)
        else:
            name = default_name
            # 名字被占了（URL 不同）：加后缀换个名
            existing = {str(e.get("name") or "") for e in extensions_web._web_raw_entries(store)}
            n = 2
            while name in existing:
                name = f"{default_name}{n}"
                n += 1
            entries = extensions_web._web_raw_entries(store)
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            entries.append({
                "name": name, "url": url, "enabled": True, "tools": [], "roles": ["worker"],
                "timeout_s": 20, "header_names": sorted(headers.keys()),
            })
            with store.tx() as conn:
                store.kv_set(conn, extensions_web.KV_MCP, entries)
                for h, v in headers.items():
                    store.secret_set(conn, extensions_web.secret_name(name, h), v)
            logger.info("[search] 迁移：已把 %s 搜索服务建成 MCP 扩展 %s（密钥进数据库，只进不出）", provider, name)
        # 2. 绑定：已有绑定不覆盖
        from . import search_binding

        if search_binding.get_binding(store) is None:
            tool_specs: list[dict] | None = None
            if callable(list_tools):
                try:
                    got = list_tools(name)
                    if isinstance(got, list):
                        tool_specs = got
                except Exception:
                    tool_specs = None
            tool, extract_tool = _pick_tools(tool_specs, provider=provider)
            if tool:
                search_binding.set_binding(store, {"mcp": name, "tool": tool, "extract_tool": extract_tool})
                logger.info("[search] 迁移：搜索绑定 → 用 %s 的 %s", name, tool)
        did_something = True
    elif provider:
        logger.info("[search] 迁移：provider = %r 不认识或没密钥，不建扩展，只清掉这段旧配置", provider)

    # 3. 从 config.toml 删掉整个 [search] 段（load_settings 已忽略它；清掉省得管理员困惑）
    try:
        doc2 = tomlkit.parse(config_file.read_text(plugin_dir))
        if doc2.get("search") is not None:
            del doc2["search"]
            config_file._write_back(plugin_dir, data_dir, tomlkit.dumps(doc2), old_text=text)
            logger.info("[search] 迁移：config.toml 的 [search] 段已删除（备份在数据目录 config-backups/）")
    except Exception:
        logger.exception("[search] 迁移：删 config.toml 的 [search] 段出错（绑定已迁好，这段忽略不影响运行）")
    return did_something
