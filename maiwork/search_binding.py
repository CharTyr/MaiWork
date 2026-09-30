"""联网搜索绑定（docs/02 §10、docs/07 §10.3）：搜索走「扩展」里指定的一个 MCP 的某个工具；
「抓网页正文」可以另选一个 MCP 的工具（2026-09-28 起，两者不必是同一家）。

- 存法：kv["extensions.search"] = {"mcp": 搜索扩展名, "tool": 搜索工具名,
  "extract_mcp": 抓正文扩展名或 "", "extract_tool": 抓正文工具名或 ""}。
  老记录没有 extract_mcp：有 extract_tool 时视为和搜索同一家（行为不变）。
  **只存数据库，不进 config.toml**（网页保存的东西不写回 plugins/，会触发全部插件重载）。
- 候选清单：所有已启用 MCP 扩展的工具，按名字/描述猜哪个像搜索、哪个像抓正文（guess_tool_role），
  前端据此给下拉排序和标徽章。
- 状态（status_of）：没绑定 / 绑定的扩展不在了 / 扩展没启用 / 扩展没连上 / 工具不在了 / 好了——
  中文说明，设置总览的健康项和 GET /api/extensions/search 都用它。
- 扩展被删除 / 改名：调用方（console 路由）调 clear_binding(store, mcp=名字) 跟着清掉。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from . import extensions_web, search_presets

logger = logging.getLogger("maiwork.search_binding")

KV_SEARCH = "extensions.search"

_BINDING_KEYS = ("mcp", "tool", "extract_mcp", "extract_tool")


# ----------------------------------------------------------------------
# 读 / 写 / 清
# ----------------------------------------------------------------------


def _norm_names(raw: Any) -> list[str]:
    """fallback/broad 名单规范化：只收字符串、去空白去重、保序；不是列表 → []。"""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for n in raw:
        s = str(n or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _norm_binding(raw: Any) -> dict[str, Any] | None:
    """kv 里读出来的东西规范化成 {"mcp","tool","extract_mcp","extract_tool",
    "fallback":[名字], "broad":[名字]}；不合法 → None。

    extract_tool 为空 → extract_mcp 也为空；有 extract_tool 但没写 extract_mcp（老记录）→ 同搜索那家。
    老记录没有 fallback/broad → 空列表。
    """
    if not isinstance(raw, dict):
        return None
    mcp = str(raw.get("mcp") or "").strip()
    tool = str(raw.get("tool") or "").strip()
    if not mcp or not tool:
        return None
    extract_tool = str(raw.get("extract_tool") or "").strip()
    extract_mcp = (str(raw.get("extract_mcp") or "").strip() or mcp) if extract_tool else ""
    return {
        "mcp": mcp, "tool": tool, "extract_mcp": extract_mcp, "extract_tool": extract_tool,
        "fallback": _norm_names(raw.get("fallback")),
        "broad": _norm_names(raw.get("broad")),
    }


def get_binding(store: Any) -> dict[str, str] | None:
    """当前搜索绑定；没有 / 被改坏 → None。"""
    try:
        return _norm_binding(store.kv_get(KV_SEARCH))
    except Exception:
        return None


def set_binding(store: Any, binding: dict[str, Any]) -> dict[str, str]:
    """直接写绑定（不校验扩展/工具在不在——校验走 save_binding）。返回规范化后的绑定。"""
    norm = _norm_binding(binding)
    if norm is None:
        raise ValueError("绑定要写成 {\"mcp\": 扩展名, \"tool\": 搜索工具名}")
    with store.tx() as conn:
        store.kv_set(conn, KV_SEARCH, norm)
    logger.info("搜索绑定已保存：用 %s 的 %s%s", norm["mcp"], norm["tool"],
                f"（抓正文 {norm['extract_mcp']} 的 {norm['extract_tool']}）" if norm["extract_tool"] else "")
    return norm


def clear_binding(store: Any, *, mcp: str | None = None) -> bool:
    """解绑。mcp 给了时（扩展被删 / 改名）：它是搜索那家 → 整个清掉；
    只是抓正文那家 → 只去掉抓正文，搜索照旧。返回有没有真的改动。幂等。"""
    current = get_binding(store)
    if current is None:
        return False
    if mcp is not None and current["mcp"] != str(mcp):
        changed = False
        updated = dict(current)
        if current["extract_tool"] and current["extract_mcp"] == str(mcp):
            updated["extract_mcp"] = ""
            updated["extract_tool"] = ""
            logger.info("抓正文绑定已解除（原绑定：%s 的 %s）", current["extract_mcp"], current["extract_tool"])
            changed = True
        # 被删/改名的扩展也要从 fallback / broad 名单里剔掉，名单不留死名字
        name = str(mcp)
        for key in ("fallback", "broad"):
            if name in current.get(key, []):
                updated[key] = [n for n in current.get(key, []) if n != name]
                changed = True
        if changed:
            set_binding(store, updated)
            return True
        return False
    with store.tx() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (KV_SEARCH,))
    logger.info("搜索绑定已解除（原绑定：%s 的 %s）", current["mcp"], current["tool"])
    return True


def save_binding(
    store: Any,
    settings: Any,
    body: dict[str, Any],
    *,
    tool_spec_of: Callable[[str, str], dict | None],
) -> dict[str, str]:
    """校验 + 写绑定。tool_spec_of(扩展名, 工具名) → 工具的 spec（含 inputSchema）或 None。

    可选 "fallback" / "broad"：扩展名列表（主家挂了递补 / 撒大网多搜几家），
    只收存在且 url 被搜索预设认得出的扩展。
    ValueError（中文）：扩展不存在 / 工具不存在 / 搜索和抓正文是同一个工具 /
    fallback/broad 名单不合法。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体要写成 {\"mcp\": 扩展名, \"tool\": 搜索工具名, \"extract_mcp\": 可选, \"extract_tool\": 可选}")
    mcp = str(body.get("mcp") or "").strip()
    tool = str(body.get("tool") or "").strip()
    extract_tool = str(body.get("extract_tool") or "").strip()
    extract_mcp = (str(body.get("extract_mcp") or "").strip() or mcp) if extract_tool else ""
    if not mcp or not tool:
        raise ValueError("要给出 mcp（扩展名）和 tool（搜索工具名）")
    names = {e.name for e in extensions_web.merged_entries(settings, store)}
    for name in (mcp, extract_mcp):
        if name and name not in names:
            raise ValueError(f"没有这个扩展「{name}」——先去 设置 → 扩展 里添加")
    if tool_spec_of(mcp, tool) is None:
        raise ValueError(f"扩展 {mcp} 没有这个工具「{tool}」——先 reload 一下扩展拿最新工具清单")
    if extract_tool:
        if extract_mcp == mcp and extract_tool == tool:
            raise ValueError("搜索和抓正文不能是同一个工具")
        if tool_spec_of(extract_mcp, extract_tool) is None:
            raise ValueError(f"扩展 {extract_mcp} 没有这个工具「{extract_tool}」——先 reload 一下扩展拿最新工具清单")
    # fallback（主家挂了递补）/ broad（撒大网多搜几家）名单：只收存在、且 url 被
    # 搜索预设（search_presets）认得出的扩展——别家参数我们定不死，递补/撒网都用不了。
    entries = extensions_web.merged_entries(settings, store)
    url_of = {e.name: e.url for e in entries}
    fallback = _norm_names(body.get("fallback"))
    broad = _norm_names(body.get("broad"))
    for label, lst in (("递补搜索", fallback), ("撒网搜索", broad)):
        for name in lst:
            if name not in names:
                raise ValueError(f"{label}名单里的「{name}」不是已有的扩展——先去 设置 → 扩展 里添加")
            if name == mcp:
                raise ValueError(f"{label}名单不用写主搜索家自己（{name}）")
            if search_presets.preset_of_url(url_of.get(name, "")) is None:
                raise ValueError(
                    f"扩展「{name}」的地址不是已知搜索服务（预设认不出），{label}用不了它"
                )
    return set_binding(store, {"mcp": mcp, "tool": tool, "extract_mcp": extract_mcp, "extract_tool": extract_tool,
                               "fallback": fallback, "broad": broad})


# ----------------------------------------------------------------------
# 状态（可用性判断 + 中文说明）
# ----------------------------------------------------------------------


def _entry_of(settings: Any, store: Any, name: str) -> Any | None:
    for e in extensions_web.merged_entries(settings, store):
        if e.name == name:
            return e
    return None


def status_of(
    store: Any,
    settings: Any,
    runtime_of: Callable[[str], Any],
) -> tuple[bool, str]:
    """(可用, 中文说明)。runtime_of(扩展名) → 扩展运行状态（extensions.runtime_of）或 None。

    「可用」只看搜索那家；抓正文那家有问题不影响搜索，只在说明里带一句。
    """
    binding = get_binding(store)
    if binding is None:
        return False, "还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索"
    problem = tool_problem(store, settings, runtime_of, binding["mcp"], binding["tool"], role="搜索")
    if problem:
        return False, problem
    text = f"用 {binding['mcp']} 的 {binding['tool']}"
    if binding["extract_tool"]:
        eproblem = tool_problem(
            store, settings, runtime_of, binding["extract_mcp"], binding["extract_tool"], role="抓正文"
        )
        if eproblem:
            text += f"（{eproblem}）"
        elif binding["extract_mcp"] == binding["mcp"]:
            text += f"（抓正文 {binding['extract_tool']}）"
        else:
            text += f"（抓正文用 {binding['extract_mcp']} 的 {binding['extract_tool']}）"
    return True, text


def tool_problem(
    store: Any, settings: Any, runtime_of: Callable[[str], Any], mcp: str, tool: str, *, role: str
) -> str:
    """某家扩展的某个工具现在能不能用；能用 → ""，不能用 → 中文说明（role：搜索 / 抓正文）。"""
    entry = _entry_of(settings, store, mcp)
    if entry is None:
        return f"{role}绑定的扩展「{mcp}」不在了：去 设置 → 扩展 重新选一个"
    if not entry.enabled:
        return f"{role}绑定的扩展「{mcp}」没启用：去 设置 → 扩展 打开它"
    runtime = runtime_of(mcp)
    client = getattr(runtime, "client", None) if runtime is not None else None
    if client is None:
        return f"{role}绑定的扩展「{mcp}」还没连上：去 设置 → 扩展 里 reload 一下"
    tools_remote = runtime.tools_remote() if hasattr(runtime, "tools_remote") else {}
    if tool not in tools_remote:
        return f"扩展「{mcp}」没有这个工具「{tool}」：去 设置 → 扩展 重新绑定"
    return ""


# ----------------------------------------------------------------------
# 候选清单（GET /api/extensions/search 的 candidates 段）
# ----------------------------------------------------------------------

_SEARCH_HINT = re.compile(r"search|搜索|检索|find|lookup|query", re.IGNORECASE)
_EXTRACT_HINT = re.compile(r"extract|contents?|crawl|scrape|fetch|read|正文|抓取|抽", re.IGNORECASE)


def guess_tool_role(name: str, description: str = "") -> str:
    """按名字/描述猜这个工具像「搜索」还是「抓正文」；"search" | "extract" | ""。"""
    text = f"{name} {description}"
    if _SEARCH_HINT.search(text):
        return "search"
    if _EXTRACT_HINT.search(text):
        return "extract"
    return ""


def candidates_of(settings: Any, store: Any, runtime_of: Callable[[str], Any]) -> list[dict[str, Any]]:
    """所有已启用 MCP 扩展的工具清单：[{"mcp", "tools": [{"name","description","guess"}]}]。"""
    out: list[dict[str, Any]] = []
    for entry in extensions_web.merged_entries(settings, store):
        if not entry.enabled:
            continue
        runtime = runtime_of(entry.name)
        tools_remote: dict = {}
        if runtime is not None and hasattr(runtime, "tools_remote"):
            try:
                tools_remote = runtime.tools_remote() or {}
            except Exception:
                tools_remote = {}
        tools: list[dict[str, Any]] = []
        for remote_name, spec in sorted(tools_remote.items()):
            if not isinstance(spec, dict):
                spec = {}
            desc = str(spec.get("description") or "")
            tools.append({
                "name": remote_name,
                "description": desc,
                "guess": guess_tool_role(remote_name, desc),
            })
        out.append({"mcp": entry.name, "tools": tools})
    return out


def search_view(store: Any, settings: Any, runtime_of: Callable[[str], Any]) -> dict[str, Any]:
    """GET /api/extensions/search 的响应：{binding, status: {ok, text}, candidates}。"""
    ok, text = status_of(store, settings, runtime_of)
    return {
        "binding": get_binding(store),
        "status": {"ok": ok, "text": text},
        "candidates": candidates_of(settings, store, runtime_of),
    }


def search_role_of(store: Any, ext_name: str) -> str:
    """GET /api/extensions 条目上的徽章："search"（绑定的搜索工具在它家）/
    "extract"（只有抓正文在它家）/ ""。"""
    binding = get_binding(store)
    if binding is None:
        return ""
    if binding["mcp"] == ext_name:
        return "search"
    if binding["extract_tool"] and binding["extract_mcp"] == ext_name:
        return "extract"
    return ""
