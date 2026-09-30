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
