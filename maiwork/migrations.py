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
import sqlite3
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
    # 官方 MCP 工具名 2026-10 实测：tavily_search / tavily_extract（下划线版）；
    # 老的连字符版（tavily-search）只存在于旧文档。这里只影响「还没迁过」的老配置
    # 首次迁移时写进绑定的名字（幂等键，迁移过的库不会再来一次）——改成下划线是安全的。
    if not search_tool:
        search_tool = {
            "tavily_mcp": "tavily_search", "tavily": "tavily_search",
            "exa": "web_search_exa", "you": "you-search",
        }.get(provider, "")
    if not extract_tool:
        extract_tool = {
            "tavily_mcp": "tavily_extract", "tavily": "tavily_extract", "you": "you-contents",
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


# ----------------------------------------------------------------------
# 死配置键清理（2026-10 docs/18 第一步）
# ----------------------------------------------------------------------

# 已从配置模型里删掉的键：启动时把存量 config.toml 里的这几行一并清掉，
# 省得管理员看到「问题清单」式困惑。只清键本身，节里的其他行一字不动。
_DEAD_CONFIG_KEYS: tuple[str, ...] = (
    "feeds.min_score",             # 0.3.4 起已不看它（改看 web_min_avg）
    "feeds.ideas_per_day",         # 从来没人消费的构想上限
    "delivery.mention_ttl_minutes",  # 「可提起清单」机制已随新鲜事递料一起退役
    "goals.propose",               # 主动提目标 2026-10 docs/18 第一步删掉（节删空后连节一起清）
)


def migrate_dead_config_keys(plugin_dir: Path | str, data_dir: Path | str) -> list[str]:
    """把 _DEAD_CONFIG_KEYS 从存量 config.toml 里删掉（一次备份、一次覆盖写）。

    幂等：一个死键都没有 → []、文件不动（不备份、不重写）；有就重写一次。
    文件不存在 / 解析失败 → []、什么都不动（不拖垮启动）。
    返回实际删掉的「节.字段」清单。
    """
    import tomlkit

    try:
        text = config_file.read_text(plugin_dir)
    except config_file.ConfigFileError:
        return []
    try:
        doc = tomlkit.parse(text)
    except Exception:
        return []
    deleted: list[str] = []
    for full_key in _DEAD_CONFIG_KEYS:
        section, _, field = full_key.partition(".")
        sec = doc.get(section)
        if isinstance(sec, dict) and field in sec:
            del sec[field]
            deleted.append(full_key)
    # 删完某个键后这节一个键都不剩 → 连节一起删（比如 [goals] 只有 propose 那一项）
    for key in list(doc.keys()):
        sec = doc.get(key)
        if isinstance(sec, dict) and not sec and any(d.startswith(key + ".") for d in deleted):
            del doc[key]
    if not deleted:
        return []
    config_file._write_back(plugin_dir, data_dir, tomlkit.dumps(doc), old_text=text)
    logger.info("死配置键已从 config.toml 删掉：%s", "、".join(deleted))
    return sorted(deleted)


# ----------------------------------------------------------------------
# kv["rules.override"] → config.toml（docs/18 第一步；这层旧覆盖赢文件值的坑没了）
# ----------------------------------------------------------------------

KV_RULES_OVERRIDE = "rules.override"  # 旧网页覆盖层的 kv 键（值 {节: {字段: 值}}）


def migrate_rules_override_to_file(store: Any, plugin_dir: Path | str, data_dir: Path | str) -> list[str]:
    """把 kv["rules.override"] 的值写进 config.toml，写成功才删 kv 键。

    幂等；失败（文件坏了 / 写不了）抛 ConfigFileError，kv 键留着下次启动再迁。
    日志只打改过的键名，绝不打值。
    返回实际写进文件的「节.字段」清单（值和文件一样的键也清 kv，但不算进清单）。
    """
    import tomlkit

    try:
        raw = store.kv_get(KV_RULES_OVERRIDE)
    except Exception:
        raw = None
    if not isinstance(raw, dict) or not raw:
        return []
    items: dict[str, Any] = {}
    try:
        for section, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                key = f"{section}.{field}"
                if isinstance(value, (dict,)):  # 只收标量/列表/布尔/数字（防御：不写 tables 进文件）
                    continue
                items[key] = value
    except Exception:
        items = {}
    if not items:
        # 形状全坏：直接清掉 kv 键（这层规则已认不出任何东西）
        try:
            with store.tx() as conn:
                store.kv_delete(conn, KV_RULES_OVERRIDE)
        except Exception:
            logger.exception("清空 rules.override 出错")
        return []
    try:
        text = config_file.read_text(plugin_dir)
    except config_file.ConfigFileError:
        raise
    try:
        doc = tomlkit.parse(text)
    except Exception as e:
        raise config_file.ConfigFileError(f"config.toml 解析失败（文件坏了？）：{e}") from None
    file_values: dict[str, Any] = {}
    for key in items:
        section, _, field = key.partition(".")
        sec = doc.get(section)
        if isinstance(sec, dict) and field in sec:
            file_values[key] = sec[field]
    same_keys = {k for k, v in items.items() if k in file_values and _toml_same(file_values[k], v)}
    write_keys = [k for k in sorted(items) if k not in same_keys]
    if write_keys:
        config_file.write_fields(plugin_dir, data_dir, {k: items[k] for k in write_keys})
    try:
        with store.tx() as conn:
            store.kv_delete(conn, KV_RULES_OVERRIDE)
    except Exception:
        logger.exception("rules.override 写文件成功后清 kv 键出错")
    if write_keys:
        logger.info("规则覆盖已搬进 config.toml：%s；kv[" "rules.override" "] 已删除", "、".join(write_keys))
    else:
        logger.info("规则覆盖和 config.toml 值一致，不用写文件；kv[" "rules.override" "] 已删除")
    return write_keys


def _toml_same(file_value: Any, kv_value: Any) -> bool:
    """tomlkit 解析出的文件值和 kv 里的 JSON 值算不算同一个（列表逐项比）。"""
    try:
        fv = list(file_value) if isinstance(file_value, (list, tuple)) or type(file_value).__name__ in ("Array",) else file_value
        if isinstance(fv, list):
            return [str(x) for x in fv] == [str(x) for x in (kv_value if isinstance(kv_value, list) else [])]
        return fv == kv_value or str(fv) == str(kv_value)
    except Exception:
        return False


# ----------------------------------------------------------------------
# 主动提目标残留 kv（docs/18 第一步；功能已删，几天的标记留着没用了）
# ----------------------------------------------------------------------

_GOAL_LEFTOVER_PREFIXES = ("goals.propose_day.",)  # 每个群一条「今天提没提过」
_GOAL_LEFTOVER_SUFFIXES = (".goal",)              # sched.<群号>.goal


def migrate_goal_proposal_kv(store: Any) -> list[str]:
    """删掉主动提目标留下的 kv 行（goals.propose_day.<群号>、sched.<群号>.goal）。

    幂等：一行都没有 → []；有 → 删掉并返回删掉的键清单（只打键名不打值——值只
    是日期字符串，没敏感信息，但习惯上也就这么办）。出错整批不动、下次再试。
    """
    try:
        keys: list[str] = []
        with store.tx() as conn:
            rows = conn.execute("SELECT key FROM kv").fetchall()
            for row in rows:
                key = str(row[0] if not isinstance(row, sqlite3.Row) else row["key"])
                if any(key.startswith(p) for p in _GOAL_LEFTOVER_PREFIXES) or (
                    key.startswith("sched.") and any(key.endswith(s) for s in _GOAL_LEFTOVER_SUFFIXES)
                ):
                    keys.append(key)
            for key in keys:
                store.kv_delete(conn, key)
    except Exception:
        logger.exception("清主动提目标残留 kv 出错，下次启动再试")
        return []
    if keys:
        logger.info("主动提目标残留 kv 已清：%d 条（%s …）", len(keys), "、".join(sorted(keys)[:8]))
    return sorted(keys)


# ----------------------------------------------------------------------
# 屏蔽域名归一成按群 kv（docs/18 第一步；原来 config.toml / 全局 kv 两份）
# ----------------------------------------------------------------------

KV_BLOCKED_GLOBAL = "feeds.blocked_domains"  # 旧全局屏蔽名单 kv 键


def migrate_blocked_domains_to_groups(
    store: Any, plugin_dir: Path | str, data_dir: Path | str, serve_gids: list[str] | tuple[str, ...]
) -> list[str] | None:
    """把「config.toml [feeds] blocked_domains ∪ 全局 kv["feeds.blocked_domains"]」
    拷进每个服务群的 kv["feeds.blocked.<gid>"]，然后删来源：全局 kv 键 + 文件里的键。

    幂等：两个来源键都没了 → 什么都不动（不重写文件），免得把管理员
    后来在某个群里改的名单盖回去。全空（生产现状）时只清两处来源，不给群写空名单。
    域名不是密钥，日志可以打名单。
    返回：这次实际迁掉的并集（空 = 只清来源不写群）；None = 已迁过，什么都没动。
    """
    from .config import normalize_domain
    import tomlkit

    def _norm(names: Any) -> list[str]:
        if not isinstance(names, (list, tuple)):
            return []
        out: set[str] = set()
        for n in names:
            d = normalize_domain(n)
            if d:
                out.add(d)
        return sorted(out)

    try:
        text = config_file.read_text(plugin_dir)
    except config_file.ConfigFileError as e:
        if "找不到" in str(e):
            text = ""  # 文件不存在 = 文件侧没东西可迁，只清 kv；不写文件
        else:
            raise
    try:
        doc = tomlkit.parse(text)
    except Exception as e:
        raise config_file.ConfigFileError(f"config.toml 解析失败（文件坏了？）：{e}") from None
    sec = doc.get("feeds")
    file_names: list[str] = []
    new_text: str | None = None
    if isinstance(sec, dict) and "blocked_domains" in sec:
        file_names = _norm(sec.get("blocked_domains"))
        del sec["blocked_domains"]
        new_text = tomlkit.dumps(doc)

    kv_raw = store.kv_get(KV_BLOCKED_GLOBAL, None)
    has_kv = isinstance(kv_raw, list)
    kv_names = _norm(kv_raw)
    if not file_names and not kv_names and new_text is None and not has_kv:
        return None  # 已迁过 / 从来没有过 → 不动

    union = sorted(set(file_names) | set(kv_names))
    if union:
        for gid in serve_gids:
            if not gid:
                continue
            with store.tx() as conn:
                store.kv_set(conn, f"feeds.blocked.{gid}", union)
        logger.info("屏蔽名单已按群迁好（共 %d 个域名，进了 %d 个服务群）", len(union), len(list(serve_gids)))
    if new_text is not None:
        config_file._write_back(plugin_dir, data_dir, new_text, old_text=text)
    if has_kv:
        with store.tx() as conn:
            store.kv_delete(conn, KV_BLOCKED_GLOBAL)
    if union:
        logger.info("按群屏蔽名单来源已清：%s", "、".join(union[:50]))
    return union


# ----------------------------------------------------------------------
# 旧 [models] → [[endpoints]] + [[model_list]] + 专岗选择（2026-10 改版 1a）
# ----------------------------------------------------------------------

# 旧 [models] 里要删掉的键（迁移成功后整节从文件消失）
_OLD_MODELS_KEYS = (
    "base_url", "api_key", "main", "main_backup", "worker", "worker_backup",
    "retries", "retry_delay_s", "max_concurrency", "max_rpm",
    "context_window", "max_tokens",
)

# 「主模型」岗位（main）跟着旧 main 槽；四个执行岗位跟着旧 worker 槽
_WORKER_KINDS = ("news", "idea", "goal", "task")


def _clamp_or_default(value: Any, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if low <= n <= high else default


def migrate_models_config_to_endpoints(store: Any, plugin_dir: Path | str, data_dir: Path | str) -> bool:
    """旧 [models] 一次搬走（幂等）：端点 + 模型库 + 专岗 model/backup，删旧 [models]。

    - 触发条件：config.toml 的 [models] base_url 非空，且还没有 [[endpoints]]（有了 = 迁过）。
    - 顺序：先写 kv["agents.profiles"] 的岗位选择（读路径容错，写砸了旧四槽照样兜底），
      再通过 config_file 备份+重写 config.toml。第二遍跑（旧 [models] 已删 / 无 base_url）→ False。
    - 密钥只写进 [[endpoints]] api_key，日志绝不打值。
    返回 True = 这次真迁移了。
    """
    import tomlkit

    # 1. 判定 + 取旧值（文件坏了 / 没 base_url / 已有 endpoints → 不动）
    try:
        text = config_file.read_text(plugin_dir)
    except config_file.ConfigFileError:
        logger.warning("模型配置迁移：读不到 config.toml，跳过")
        return False
    try:
        doc = tomlkit.parse(text)
    except Exception:
        logger.warning("模型配置迁移：config.toml 解析失败（文件坏了？），跳过")
        return False
    # 有真条目才算迁过；空的 endpoints = []（按 schema 补齐的默认值）照样搬
    if doc.get("endpoints"):
        return False
    models_sec = doc.get("models")
    if not isinstance(models_sec, dict):
        return False
    base_url = str(models_sec.get("base_url") or "").strip()
    if not base_url:
        return False
    api_key = str(models_sec.get("api_key") or "").strip()
    slots = {
        name: str(models_sec.get(name) or "").strip()
        for name in ("main", "main_backup", "worker", "worker_backup")
    }
    retries = _clamp_or_default(models_sec.get("retries"), 0, 10, 5)
    retry_delay_s = _clamp_or_default(models_sec.get("retry_delay_s"), 1, 60, 10)
    max_concurrency = _clamp_or_default(models_sec.get("max_concurrency"), 1, 8, 2)
    max_rpm = _clamp_or_default(models_sec.get("max_rpm"), 0, 600, 0)
    context_window = _clamp_or_default(models_sec.get("context_window"), 8192, 2_000_000, 128000)
    max_tokens = _clamp_or_default(models_sec.get("max_tokens"), 1024, 1_000_000, 32768)
    # context_window/max_tokens 要满足「最大输出 < 上下文窗口」，不然 load_settings 会丢条目
    if max_tokens >= context_window:
        max_tokens = max(1024, context_window - 1024)

    # 2. 模型库条目：四个槽按名字去重（同名共用一条，id = m1/m2/...）
    ids_by_model: dict[str, str] = {}
    model_ids: dict[str, str] = {}  # 槽名 → 模型库条目 id（空槽没有）
    for slot in ("main", "main_backup", "worker", "worker_backup"):
        name = slots[slot]
        if not name:
            continue
        if name not in ids_by_model:
            ids_by_model[name] = f"m{len(ids_by_model) + 1}"
        model_ids[slot] = ids_by_model[name]

    # 3. 岗位选择先写 kv（容错读；此刻文件还没动，写砸也只当是「没选过」）
    try:
        raw_profiles = store.kv_get("agents.profiles", {})
        if not isinstance(raw_profiles, dict):
            raw_profiles = {}
        profiles = {k: (dict(v) if isinstance(v, dict) else {}) for k, v in raw_profiles.items()}
        main_patch = {"model": model_ids.get("main", ""), "backup": model_ids.get("main_backup", "")}
        worker_patch = {"model": model_ids.get("worker", ""), "backup": model_ids.get("worker_backup", "")}
        profiles.setdefault("main", {}).update(main_patch)
        # 旧值兜底：主备同条目时读路径会把 backup 清掉（备用不许=首选），这里也就不写同值
        if main_patch["backup"] == main_patch["model"]:
            profiles["main"]["backup"] = ""
        for kind in _WORKER_KINDS:
            profiles.setdefault(kind, {}).update(worker_patch)
            if worker_patch["backup"] == worker_patch["model"]:
                profiles[kind]["backup"] = ""
        with store.tx() as conn:
            store.kv_set(conn, "agents.profiles", profiles)
        logger.info(
            "模型配置迁移：岗位选择已写好（主模型=%s 备=%s；资讯/构想/目标/任务=%s 备=%s）",
            main_patch["model"] or "（无）", profiles["main"].get("backup") or "（无）",
            worker_patch["model"] or "（无）", profiles["task"].get("backup") or "（无）",
        )
    except Exception:
        logger.exception("模型配置迁移：写岗位选择出错，但继续搬 config.toml（岗位选择之后可在网页补）")

    # 4. 重写 config.toml：加 [[endpoints]] / [[model_list]]，删旧 [models]
    endpoint = tomlkit.table()
    endpoint.add("id", "default")
    endpoint.add("name", "默认端点")
    endpoint.add("protocol", "openai")
    endpoint.add("base_url", base_url)
    endpoint.add("api_key", api_key)
    endpoint.add("retries", retries)
    endpoint.add("retry_delay_s", retry_delay_s)
    endpoint.add("max_concurrency", max_concurrency)
    endpoint.add("max_rpm", max_rpm)
    endpoints_aot = tomlkit.aot()
    endpoints_aot.append(endpoint)
    doc["endpoints"] = endpoints_aot

    model_aot = tomlkit.aot()
    for model_name, entry_id in ids_by_model.items():
        item = tomlkit.table()
        item.add("id", entry_id)
        item.add("endpoint", "default")
        item.add("model", model_name)
        item.add("context_window", context_window)
        item.add("max_tokens", max_tokens)
        model_aot.append(item)
    doc["model_list"] = model_aot

    if doc.get("models") is not None:
        for key in list(_OLD_MODELS_KEYS):
            try:
                if key in doc["models"]:
                    del doc["models"][key]
            except Exception:
                pass
        try:
            if len(doc["models"]) == 0:
                del doc["models"]
        except Exception:
            pass

    new_text = tomlkit.dumps(doc)
    config_file._write_back(plugin_dir, data_dir, new_text, old_text=text)  # 内部先备份
    logger.info(
        "模型配置迁移：旧 [models] 已搬成 [[endpoints]]（1 个）+ [[model_list]]（%d 条），"
        "旧 [models] 已从 config.toml 删除（备份在数据目录 config-backups/）",
        len(ids_by_model),
    )
    return True


# ----------------------------------------------------------------------
# 每群三份（docs/17 §八.1 + A 节，2026-10）启动迁移
# ----------------------------------------------------------------------
# 幂等判定：
#   - 本群规矩（group_rules）的 row：已迁过 = row 存在且有 group_rule_versions 里
#     任何一条；第一次拼的内容来自 notes/pref/memory，拼完马上写规矩（留一版 migrate
#     版本记录）。下一次再启动：row 在 + updated_by=migrate → 仅补「上次之后新出来的
#     那段」，不覆盖管理员后来手改的内容（updated_by 不是 migrate 时跳过拼接）。
#   - 资讯 skill：已迁过 = kind=news 的 agent_skills 里已有 name=news-本群做法 那一份。
#     还没建好就新建并把口味正文本进 body；已建好就不动。
#
# 删除旧来源（成功拼完才删）：
#   - agent_memory_notes（每群每岗 rows）、kv["feeds.pref.<gid>"]、kv["feeds.taste.<gid>"]；
#   - identity/memory/<gid>.md：先备份到 <data_dir>/identity/mem.bak/<gid>.md，再清空
#     （identity.Identity 用文件字路径读，这里走文件操作，不上 Identity 类本身，避开循环依赖）。
#
# 参数：get_data_dir(gid) 给 identity 的 data_dir（其实全局一个）；identity 参数没被用到
# 的文义只是占位（调用方可以先不传，这里读 Path(settings.data_dir)）。


def _gc_served_gids(settings: Any, store: Any = None) -> list[str]:
    """服务群名单：settings.groups 优先；没有（测试里 settings 只给 is_served）回退到
    agent_memory_notes / kv feeds.pref / feeds.taste 里出现过的群号 + is_served 过滤。"""
    out: set[str] = set()
    try:
        groups = getattr(settings, "groups", None) or {}
        if hasattr(groups, "keys"):
            for k in groups.keys():
                ks = str(k or "").strip()
                if ks:
                    out.add(ks)
    except Exception:
        pass
    if not out and store is not None:
        try:
            rows = store.read().execute(
                "SELECT DISTINCT group_id FROM agent_memory_notes").fetchall()
            for r in rows:
                ks = str(r["group_id"] or "").strip()
                if ks:
                    out.add(ks)
        except Exception:
            pass
        try:
            rows = store.read().execute(
                "SELECT key FROM kv WHERE key LIKE 'feeds.pref.%' OR key LIKE 'feeds.taste.%'").fetchall()
            for r in rows:
                k = str(r["key"] or "")
                for prefix in ("feeds.pref.", "feeds.taste."):
                    if k.startswith(prefix):
                        out.add(k[len(prefix):])
                        break
        except Exception:
            pass
    # 最后过 is_served（测试里的 settings 没有 groups 属性但能给 is_served）
    ok: list[str] = []
    for gid in sorted(out):
        try:
            fn = getattr(settings, "is_served", None)
            if callable(fn) and fn(gid):
                ok.append(gid)
        except Exception:
            continue
    return ok


def _write_backup_once(bak: Path, text: str) -> bool:
    """把 text 存到 `bak`：**绝不覆写**最早那份原件。

    - `bak` 已有同样内容 → True（不用写）；
    - `bak` 已存在但内容不同（管理员后来手写过新内容）→ 另存唯一后缀档
      `<名>.1.md`、`<名>.2.md` …（已存在同内容的档也算备份过），绝不盖掉原件；
    - 任何写失败 → False（调用方据此**不清原文件**，下次再试）。
    """
    try:
        bak.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("备份每群记忆失败（%s），这条跳过清空", bak)
        return False
    candidates: list[Path] = [bak]
    if bak.exists():
        try:
            if bak.read_text(encoding="utf-8") == text:
                return True
        except OSError:
            pass
        n = 1
        while n <= 1000:
            alt = bak.with_name(f"{bak.stem}.{n}{bak.suffix}")
            if not alt.exists():
                candidates = [alt]
                break
            try:
                if alt.read_text(encoding="utf-8") == text:
                    return True  # 这份新内容早就单独备份过
            except OSError:
                pass
            n += 1
        else:
            logger.warning("每群记忆备份后缀档太多（%s），本次不备份", bak)
            return False
    target = candidates[0]
    try:
        target.write_text(text, encoding="utf-8")
    except OSError:
        logger.exception("备份每群记忆失败（%s），这条跳过清空", target)
        return False
    return True


def _gc_backup_memory_file(mem_file: Path, backup_root: Path) -> None:
    """把 memory/<gid>.md 旧内容备份到 identity/mem.bak/，再把原文件清空。

    幂等 / 保真：备份**先写成功再清原文件**；最早那份原件绝不覆写（新内容另存唯一
    后缀档）；备份写失败 → 原文件一个字不动（源保留，下次再试）。
    """
    if not mem_file.exists():
        return
    try:
        text = mem_file.read_text(encoding="utf-8")
    except OSError:
        return
    if not str(text or "").strip():
        # 本来就是空的，只顺手删（备份意义不大）
        try:
            mem_file.unlink()
        except OSError:
            pass
        return
    if not _write_backup_once(backup_root / mem_file.name, text):
        return
    try:
        mem_file.write_text("", encoding="utf-8")
    except OSError:
        logger.exception("清空每群记忆失败（%s）", mem_file)


def _gc_collect_notes_text(store: Any, gid: str) -> str:
    """本群提醒 agent_memory_notes 合一段：每岗按 alphabetical 拼，空的跳过。"""
    try:
        rows = store.read().execute(
            "SELECT kind, notes FROM agent_memory_notes WHERE group_id=? ORDER BY kind",
            (gid,),
        ).fetchall()
    except Exception:
        return ""
    parts: list[str] = []
    title_of = {"news": "资讯", "idea": "构想", "goal": "目标", "task": "通用"}
    for r in rows:
        notes = str(r["notes"] or "").strip()
        if not notes:
            continue
        title = title_of.get(str(r["kind"] or ""), str(r["kind"] or ""))
        parts.append(f"【{title}岗的工作册（管理员写的）】\n{notes}")
    return "\n\n".join(parts)


def migrate_group_context_to_rules_and_skills(
    store: Any,
    settings: Any,
    identity: Any,
    get_data_dir: Any = None,
) -> dict[str, Any]:
    """把旧 6 处「这个群该怎么做」的散件拼进每群三份；返回 {rules_updated:[gid], skills_created:[gid]}。

    幂等：已迁过的群 / 已经建起 skill 的群再来一次什么都不做；
    非服务群一律不碰。"""
    from .agents import Agents  # 局部导入以防环
    from .clock import now as _now

    agents = Agents(store, lambda: settings)
    out: dict[str, Any] = {"rules_updated": [], "skills_created": []}
    try:
        gids = _gc_served_gids(settings, store=store)
    except Exception:
        gids = []
    if not gids:
        return out
    if get_data_dir is None:
        dd = getattr(settings, "data_dir", None)
        get_data_dir = lambda _g: str(dd) if dd is not None else ""  # noqa: E731
    for gid in gids:
        try:
            _migrate_one_group(store, settings, agents, gid, get_data_dir, out, _now())
        except Exception:
            logger.exception("每群三份迁移失败（群 %s），跳过这个群", gid)
    return out


def _migrate_one_group(
    store: Any,
    settings: Any,
    agents: Any,
    gid: str,
    get_data_dir: Any,
    out: dict[str, Any],
    now: float,
) -> None:
    pref = str(store.kv_get(f"feeds.pref.{gid}", "") or "").strip()
    taste_raw = store.kv_get(f"feeds.taste.{gid}", None) or {}
    taste_text = str(taste_raw.get("text") or "").strip() if isinstance(taste_raw, dict) else ""
    notes_block = _gc_collect_notes_text(store, gid)
    dd_str = str(get_data_dir(gid) or "").strip()
    mem_file = (Path(dd_str) / "identity" / "memory" / f"{gid}.md") if dd_str else None
    mem_text = ""
    if mem_file is not None and mem_file.exists():
        try:
            mem_text = mem_file.read_text(encoding="utf-8").strip()
        except OSError:
            mem_text = ""

    # 1) 资讯 skill：把口味小结拼成初始正文（只在没有那份时）
    skill_exists = bool(agents.skills(gid, "news", include_archived=True))
    if not skill_exists:
        try:
            agents.skill_add(gid, "news", description="", body=taste_text,
                             source="migrate", note="开迁移：口味小结进本群做法")
            if taste_text:
                out["skills_created"].append(gid)
            skill_exists = True
        except FileExistsError:
            skill_exists = True  # 别人先建了；口味当已迁过
    if skill_exists:
        # skill 已就位 → 口味 kv 已经迁过，清掉（它以后不会再被读）
        try:
            if store.kv_get(f"feeds.taste.{gid}") is not None:
                with store.tx() as conn:
                    store.kv_delete(conn, f"feeds.taste.{gid}")
        except Exception:
            pass

    # 2) 本群规矩：拼 notes + pref + memory（原文原段）；只在没有、或上一次是我们 migrate 来的时候才补。
    #    幂等口径：**按整段内容精确判重**（不用标题 marker 粗判）——正文里已经有这一整段
    #    就跳过，不再重复 append（清理失败重跑时不会越拼越长）；真正新出现的段才补一次。
    cur = agents.group_rules_get(gid)
    body = str(cur.get("body") or "")
    owns_body = (not body) or cur.get("updated_by") == "migrate"   # 管理员手改权威不动

    pref_part = f"【管理员写的资讯偏好】\n{pref}" if pref else ""
    notes_part = notes_block or ""
    mem_part = f"【群里之前的工作记忆】\n{mem_text}" if mem_text else ""

    new_parts: list[str] = []
    if owns_body:
        for part in (notes_part, pref_part, mem_part):
            if part and part not in body:
                new_parts.append(part)

    final_body = body
    if new_parts:
        candidate = (body + "\n\n" if body else "") + "\n\n".join(new_parts)
        if len(candidate) <= 3000:
            try:
                agents.group_rules_set(gid, candidate, updated_by="migrate")
                out["rules_updated"].append(gid)
                final_body = candidate
            except ValueError:
                logger.warning("本群规矩迁移后超长不覆盖（群 %s），内容保留在旧处", gid)
        else:
            logger.warning("本群规矩迁移后超长不覆盖（群 %s），内容保留在旧处", gid)

    # 3) 删旧来源：只有正文归我们管（空 / 上次 migrate 来的）、且这段内容确实已在正文里
    #    才删；超长写不下 / 管理员手改过 → 源一律留着。逐项独立重试（一项失败不拖累其他），
    #    所以「已经迁进去、只是清理失败」的下一轮还能接着清。
    if owns_body:
        for part, clearer in (
            (notes_part, lambda: _gc_try_clear_notes(store, gid)),
            (pref_part, lambda: _gc_try_clear_pref(store, gid)),
        ):
            if not part or part in final_body:
                clearer()
        if mem_file is not None and (not mem_part or mem_part in final_body):
            try:
                bak_root = (Path(dd_str) / "identity" / "mem.bak") if dd_str else None
                if bak_root is not None:
                    _gc_backup_memory_file(mem_file, bak_root)
                else:
                    mem_file.write_text("", encoding="utf-8")
            except Exception:
                logger.exception("备份每群记忆出错（群 %s）", gid)


def _gc_try_clear_pref(store: Any, gid: str) -> None:
    """清 kv["feeds.pref.<gid>"] 旧来源；失败只记日志（下次启动再清）。"""
    try:
        with store.tx() as conn:
            store.kv_delete(conn, f"feeds.pref.{gid}")
    except Exception:
        logger.exception("清资讯偏好旧来源出错（群 %s）", gid)


def _gc_try_clear_notes(store: Any, gid: str) -> None:
    """清 agent_memory_notes 里本群的行（**不 DROP 表**）；失败只记日志。"""
    try:
        with store.tx() as conn:
            conn.execute("DELETE FROM agent_memory_notes WHERE group_id=?", (gid,))
    except Exception:
        logger.debug("清本群提醒旧来源表出错（群 %s，表可能还没建）", gid, exc_info=True)


# ----------------------------------------------------------------------
# 群控归一（0.8.0 docs/18 §五 + 往群里发）：全局旧键 → 每个服务群一份
# ----------------------------------------------------------------------
#
# 以前「谁能批本群的活 / 免批」「冷场开话题 / 往群里发多少 / 几点不打扰」是全局一份
# （config.toml）。0.8.0 起每个服务群自己一份（kv["group_approval.<群号>"] /
# kv["group_push.<群号>"]），全局这几行只作**新群第一次的迁移种子**——先按服务群把
# 每群那份种好（用还没清过的 settings），确认真落库了，才把全局旧键从 config.toml 删掉。
# 失败就一个都不删（源保留，下次启动再迁）。
#
# 注意：这几个键**不能**进 _DEAD_CONFIG_KEYS——deadclean 是启动第一步，会先把它删了，
# 那样就没种子可迁了。父会话（app.py）把本函数接在 deadclean **之前**。
#
# 日志只打键名 / 群号，绝不打值（可能含密钥或账号）。

# 旧的全局「谁能批 / 免批」四键（group_approval.py 的种子；种好删）
GROUP_APPROVAL_SEED_KEYS: tuple[str, ...] = (
    "approval.required",
    "approval.admins",
    "approval.exempt_groups",
    "approval.exempt_users",
)

# 旧的全局「开话题 / 往群里发多少 / 几点不打扰」五键（group_push.py 的种子；种好删）
GROUP_PUSH_SEED_KEYS: tuple[str, ...] = (
    "topics.enabled",
    "topics.speaker",
    "topics.per_day",
    "delivery.push_per_day",
    "delivery.quiet_hours",
)


def _served_group_ids(settings: Any) -> list[str]:
    """配置里的服务群（稳定排序）；认不出 / 一个都没有 → []。"""
    groups = getattr(settings, "groups", None)
    keys = getattr(groups, "keys", None)
    if not callable(keys):
        return []
    try:
        raw = list(keys())
    except Exception:
        return []
    return sorted({str(g).strip() for g in raw if str(g or "").strip()})


def _resolve_push_module(explicit: Any = None) -> Any:
    """拿 group_push 模块；合同对不上（没有 get_config / KV_PREFIX）→ None（降级只迁批准）。

    只要求 get_config(store, gid, settings)：种每群那一条就是它的本职。别的接口
    （set_config / view …）本迁移不用，模块还没长好也不该把启动拖下水。
    """
    if explicit is not None:
        if callable(getattr(explicit, "get_config", None)) and isinstance(getattr(explicit, "KV_PREFIX", None), str):
            return explicit
        return None
    try:
        from . import group_push
    except Exception:
        logger.warning("往群里发模块没就位，本次只迁批准名单（推送旧键先留着）")
        return None
    if not callable(getattr(group_push, "get_config", None)):
        return None
    if not isinstance(getattr(group_push, "KV_PREFIX", None), str):
        return None
    return group_push


def _seed_ready_settings(settings: Any) -> Any:
    """给群控归一用的「允许播种」settings 副本。

    这个函数**就是**旧全局设置 → 每群一份的物化动作：helper 的惰性种子门
    （`group_controls_seed_ready`）对 App 的普通读取是关着的，但迁移这一步必须能种。
    真 Settings 才 replace（顺带把门打开）；假 settings / 没有这个字段的照原样传。
    """
    import dataclasses

    if not dataclasses.is_dataclass(settings):
        return settings
    if getattr(settings, "group_controls_seed_ready", True) is True:
        return settings
    try:
        return dataclasses.replace(settings, group_controls_seed_ready=True)
    except Exception:
        return settings


def migrate_group_controls(
    store: Any,
    settings: Any,
    plugin_dir: Path | str,
    data_dir: Path | str,
    *,
    push_module: Any = None,
) -> dict[str, Any]:
    """0.8.0 群控归一的启动迁移（幂等）。返回一次运行的报告：

    {
      "seeded_approval": [群号…],   # 这次真种下的每群批准名单（已有记录不算）
      "seeded_push":     [群号…],   # 这次真种下的每群推送设置
      "deleted":         ["approval.admins", …],  # 真从 config.toml 删掉的旧键
      "push_available":  bool,      # 往群里发那部分这次种成功、可迁
      "problem":         "",        # 非空 = 有东西没做完，旧键一律保留
    }
    """
    result: dict[str, Any] = {
        "seeded_approval": [],
        "seeded_push": [],
        "deleted": [],
        "push_available": False,
        "problem": "",
    }
    if store is None or settings is None:
        return result
    groups = _served_group_ids(settings)
    if not groups:
        # 一个服务群都没有：没有「每群一份」可种，全局键留着（以后加的群还要靠它当种子）
        return result

    # 1) 每群批准名单（group_approval）：先种，确认落库
    try:
        from .group_approval import KV_PREFIX as _approval_kv
        from .group_approval import GroupApprovals
    except Exception:
        logger.exception("每群批准名单模块没就位，本次不迁（旧键全部保留）")
        result["problem"] = "每群批准名单模块没就位，旧键先留着"
        return result
    # 迁移这一步本身就是「旧全局 → 每群一份」的物化：显式把 helper 的惰性种子门打开
    # （App 的普通读取门是关着的），保证这次真能种下去。
    seed_settings = _seed_ready_settings(settings)
    approvals = GroupApprovals(store, get_settings=lambda: seed_settings)
    for gid in groups:
        try:
            before = store.kv_get(_approval_kv + gid, None)
            approvals.get(gid)  # 惰性种（已有记录不动）
            after = store.kv_get(_approval_kv + gid, None)
            if not isinstance(after, dict):
                raise RuntimeError("每群批准名单没落库")
            if not isinstance(before, dict):
                result["seeded_approval"].append(gid)
        except Exception:
            logger.exception("群 %s 的批准名单迁移失败（旧的全局键先留着，下次启动再试）", gid)
            result["problem"] = f"群 {gid} 的批准名单没能落库，旧的全局键先留着"
            return result

    # 2) 每群「往群里发」（group_push，接口对上才做）：同样先种、确认落库
    push = _resolve_push_module(push_module)
    if push is None:
        result["problem"] = result["problem"] or "往群里发模块接口没对上，推送旧键先留着"
    else:
        ok = True
        for gid in groups:
            try:
                before = store.kv_get(push.KV_PREFIX + gid, None)
                push.get_config(store, gid, seed_settings)
                after = store.kv_get(push.KV_PREFIX + gid, None)
                if not isinstance(after, dict):
                    raise RuntimeError("每群推送设置没落库")
                if not isinstance(before, dict):
                    result["seeded_push"].append(gid)
            except Exception:
                logger.exception("群 %s 的往群里发设置迁移失败（推送旧键先留着，下次启动再试）", gid)
                ok = False
                break
        result["push_available"] = bool(ok)
        if not ok:
            result["problem"] = result["problem"] or "往群里发设置没能全部落库，推送旧键先留着"

    # 3) 都种好了才清源：读一次文件、一次备份、一次覆盖写
    import tomlkit

    if not config_file.config_path(plugin_dir).exists():
        # 没有配置文件 = 文件里也没有旧键可删；每群记录已种好，这轮算做完
        return result
    try:
        text = config_file.read_text(plugin_dir)
        doc = tomlkit.parse(text)
    except Exception:
        logger.exception("读 / 解析 config.toml 失败，旧全局键一个不删（下次启动再试）")
        result["problem"] = result["problem"] or "config.toml 读不了或解析失败，旧键先留着"
        return result

    want = list(GROUP_APPROVAL_SEED_KEYS)
    if result["push_available"]:
        want.extend(GROUP_PUSH_SEED_KEYS)
    deletions: list[str] = []
    for full_key in want:
        section, _, field = full_key.partition(".")
        sec = doc.get(section)
        if isinstance(sec, dict) and field in sec:
            del sec[field]
            deletions.append(full_key)
    # 刚删空的节连节一起删（比如 [delivery] 只有那两个键）；还有别的键的节留
    for name in list(doc.keys()):
        sec = doc.get(name)
        if isinstance(sec, dict) and not sec and any(k.startswith(name + ".") for k in deletions):
            del doc[name]
    if not deletions:
        return result
    config_file._write_back(plugin_dir, data_dir, tomlkit.dumps(doc), old_text=text)
    result["deleted"] = sorted(deletions)
    logger.info(
        "群控归一迁移：%d 个服务群的每群配置已就位，旧全局键已从 config.toml 删掉：%s",
        len(groups), "、".join(result["deleted"]),
    )
    return result
