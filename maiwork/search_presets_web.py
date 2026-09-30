"""预设搜索服务的网页层（2026-09-30，docs/10-资讯流水线改进计划.md 第七节第 1 步）。

预设本身（地址、密钥放哪、参数怎么对接）在 search_presets.py；这里只管「在扩展里打开 / 填密钥 /
引导一次配好」，落到的仍是普通的网页 MCP 条目（extensions_web：kv 存条目、头值只进 secrets）。

- presets_view：六家预设 + 各自现在的状态（有没有对应条目、开没开、有没有填自己的密钥）。
  已有条目按地址认（search_presets.preset_of_url）：线上老的 keenable、You 不用重配。
- activate：打开一家。没有条目 → 新建（名字默认预设 id，撞了不相干的同名条目就加后缀）；
  有条目 → 按需换地址 / 换头并打开。key 给了 = 用自己的密钥；clear_key = 改回免密钥
  （只有免费的能改回）；都不给 = 密钥保持原样。还没有搜索绑定时，这家自动成为主搜索。
- setup：首次引导一次配好——先全部校验（要密钥的没填就整批拒，不写一半），再逐个打开；
  第一家当主搜索，其余当备用（fallback），原来的「广撒网」名单里还开着的保留。
config.toml 里来源的条目网页改不了（地址 / 头），只能开关：遇到要改的就报错让去改 config。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable

from . import extensions_web, search_binding
from .search_presets import PRESETS, Preset, entry_for, preset_of_url

logger = logging.getLogger("maiwork.search_presets_web")

LOGO_DIR = "/static/assets/logos"


def _logo(p: Preset) -> str:
    return f"{LOGO_DIR}/{p.id}.png"


def _entries(settings: Any, store: Any) -> list[Any]:
    return list(extensions_web.merged_entries(settings, store))


def _entry_of_preset(settings: Any, store: Any, p: Preset) -> Any | None:
    """这家预设对应的已有条目（按地址认；有几个就取第一个开着的，否则第一个）。"""
    hits = [e for e in _entries(settings, store) if preset_of_url(e.url) is p]
    if not hits:
        return None
    return next((e for e in hits if e.enabled), hits[0])


def _header_names(entry: Any) -> list[str]:
    headers = getattr(entry, "headers", None)
    names = [str(h) for h in headers.keys()] if isinstance(headers, Mapping) else []
    names += [str(h) for h in (getattr(entry, "header_names", None) or [])]
    return names


def _key_set(p: Preset, entry: Any) -> bool:
    """条目上有没有填自己的密钥：带着密钥头（且这个头不是免密钥本来就带的那个）。"""
    if entry is None:
        return False
    names = {h.lower() for h in _header_names(entry)}
    if p.key_header.lower() not in names:
        return False
    return p.key_header.lower() not in {h.lower() for h in p.free_headers}


def presets_view(settings: Any, store: Any, runtime_of: Callable[[str], Any]) -> list[dict[str, Any]]:
    """GET /api/extensions/presets 的 presets 段（顺序同 PRESETS；头值一个字都不回）。"""
    out: list[dict[str, Any]] = []
    for p in PRESETS.values():
        entry = _entry_of_preset(settings, store, p)
        ok = False
        if entry is not None and entry.enabled:
            runtime = runtime_of(entry.name)
            ok = bool(runtime is not None and getattr(runtime, "client", None) is not None)
        out.append({
            "id": p.id,
            "label": p.label,
            "free": p.free,
            "free_note": p.free_note,
            "key_page_url": p.key_page_url,
            "docs_url": p.docs_url,
            "logo": _logo(p),
            "entry": entry.name if entry is not None else None,
            "source": (extensions_web.source_of(settings, store, entry.name) if entry is not None else None),
            "enabled": bool(entry is not None and entry.enabled),
            "ok": ok,
            "key_set": _key_set(p, entry),
        })
    return out


def _free_name(settings: Any, store: Any, base: str) -> str:
    taken = {e.name for e in _entries(settings, store)}
    if base not in taken:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in taken:
            return cand
    raise ValueError(f"扩展名「{base}」被占满了，先删掉几个重名的")


def _check(p: Preset, entry: Any, key: str, clear_key: bool) -> None:
    """只校验不写：要密钥的没密钥、要改回免密钥但没有免密钥版。"""
    if clear_key and not p.free:
        raise ValueError(f"{p.label} 没有免密钥版，不能去掉密钥")
    if not p.free and not key and not _key_set(p, entry):
        raise ValueError(f"{p.label} 要先填 API 密钥：去 {p.key_page_url} 注册拿一个")


def _preset(preset_id: str) -> Preset:
    p = PRESETS.get(str(preset_id or ""))
    if p is None:
        raise KeyError(preset_id)
    return p


def _extract_tool(p: Preset, keyed: bool) -> str:
    return (p.extract_tool_key if keyed else p.extract_tool) or ""


def _bind_main(store: Any, p: Preset, name: str, keyed: bool, *, fallback: list[str] | None = None,
               broad: list[str] | None = None) -> None:
    ex = _extract_tool(p, keyed)
    body: dict[str, Any] = {
        "mcp": name,
        "tool": p.search_tool,
        "extract_mcp": name if ex else "",
        "extract_tool": ex,
    }
    if fallback is not None:
        body["fallback"] = fallback
    if broad is not None:
        body["broad"] = broad
    search_binding.set_binding(store, body)


def activate(
    store: Any,
    settings: Any,
    preset_id: str,
    *,
    key: str | None = None,
    clear_key: bool = False,
    bind_if_unbound: bool = True,
) -> tuple[str, bool]:
    """打开一家预设 → (扩展名, 这次有没有顺手把它设成主搜索)。

    KeyError 没有这个预设；ValueError（中文）要密钥没填 / 配置文件里的条目要改地址或头。
    """
    p = _preset(preset_id)
    key_s = str(key or "").strip()
    entry = _entry_of_preset(settings, store, p)
    _check(p, entry, key_s, clear_key)
    if entry is None:
        body = entry_for(p.id, key_s or None)
        body["name"] = _free_name(settings, store, p.id)
        body.setdefault("tools", [])
        body.setdefault("timeout_s", 30)
        extensions_web.create(store, settings, body)
        name = body["name"]
        keyed = bool(key_s)
    else:
        name = entry.name
        source = extensions_web.source_of(settings, store, name)
        if key_s or clear_key:
            if source != "web":
                raise ValueError(f"「{name}」写在配置文件里，网页上改不了密钥：请改 config.toml")
            want = entry_for(p.id, key_s or None)
            old_names = set(_header_names(entry))
            extensions_web.update(store, name, {
                "url": want["url"],
                "headers": want["headers"],
                "remove_headers": sorted(h for h in old_names if h not in want["headers"]),
            })
        if not entry.enabled:
            extensions_web.toggle(store, settings, name, True)
        keyed = bool(key_s) or (not clear_key and _key_set(p, entry))
    bound = False
    if bind_if_unbound and search_binding.get_binding(store) is None:
        _bind_main(store, p, name, keyed)
        bound = True
    logger.info("打开预设搜索服务 %s（扩展 %s，%s）", p.id, name, "自己的密钥" if keyed else "免密钥")
    return name, bound


def setup(store: Any, settings: Any, items: list[dict[str, Any]]) -> list[str]:
    """首次引导：[{id, key?}, …] → 打开的扩展名清单（顺序同 items）。第一家当主搜索，其余当备用。"""
    if not isinstance(items, list) or not items:
        raise ValueError("至少选一个搜索服务")
    plan: list[tuple[Preset, str]] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("每一项要写成 {\"id\": 预设, \"key\": 可选密钥}")
        p = PRESETS.get(str(it.get("id") or ""))
        if p is None:
            raise ValueError(f"没有这个搜索服务「{it.get('id')}」")
        if p.id in seen:
            continue
        seen.add(p.id)
        key_s = str(it.get("key") or "").strip()
        _check(p, _entry_of_preset(settings, store, p), key_s, False)
        plan.append((p, key_s))
    names: list[str] = []
    for p, key_s in plan:
        name, _ = activate(store, settings, p.id, key=key_s or None, bind_if_unbound=False)
        names.append(name)
    main_p, main_key = plan[0]
    main_entry = _entry_of_preset(settings, store, main_p)
    old = search_binding.get_binding(store) or {}
    enabled = {e.name for e in _entries(settings, store) if e.enabled}
    broad = [n for n in (old.get("broad") or []) if n in enabled and n != names[0]]
    _bind_main(store, main_p, names[0], bool(main_key) or _key_set(main_p, main_entry),
               fallback=names[1:], broad=broad)
    return names
