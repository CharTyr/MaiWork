"""联网搜索的通用 MCP 适配层（2026-10 重写，docs/07 §10.3）。

内置的 tavily / tavily_mcp / exa / you 专用代码已全部删掉：搜索只能走「扩展」里
绑定的那个 MCP 的某个工具（绑定见 search_binding.py，存在数据库 kv["extensions.search"]，
不进 config.toml；线上老的 [search] 配置由 migrations.py 一次性迁移成一个扩展）。

Search(store, get_extensions)：
- async search(query, *, limit=8, days=None) → [{"title","url","snippet","published"}]
  （published = epoch 秒或 None）。参数按绑定工具的 inputSchema 通用映射：
  查询词 → query / q / 第一个必填 string 字段；条数 → count / max_results / num_results /
  limit / top_k；时间 → days（整数）/ freshness 或 time_range（枚举 day|week|month|year）/
  start_published_date 或带 "date" 的字段（ISO 日期）；映射不上就不传。
- 结果通用归一化：优先 structuredContent，其次 content[].text 尝试 JSON 解析；在任意层
  找「带 url 的 dict 列表」，每条抽 url / title（title|name）/ snippet（snippet|description|
  content|text|highlights|snippets 拼接，截 _SNIPPET_MAX）/ published（published|
  published_date|page_age|date|age，用 _parse_dt_utc）；实在拿不到结构就把原文本截断当一条。
- async extract(url) → 正文文本（绑了 extract_tool 才抽；没绑返回 ""）。参数按 schema 决定
  urls（array）还是 url（string）；结果抽 markdown|content|raw_content|text。
  抓正文可以和搜索分属两个 MCP（绑定里的 extract_mcp）：走抓正文那家自己的连接，
  只看那家能不能用，搜索那家关了也不影响。
- 调用复用绑定扩展在 extensions 里的现有 client（runtime_of(name).client），不自己另建连接；
  MCP 报 "HTTP 5xx" 等 _MCP_RETRY_DELAY_S 秒重试一次。
- 没绑定 / 绑定的扩展不在了、没启用、没连上 → SearchUnavailable（中文提示去 设置 → 扩展 绑定）；
  调用失败 → SearchError（已遮扩展 headers 里的密钥）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .mcp_client import MCPError, McpSessionClient
from . import search_binding
from .search_binding import get_binding, status_of, tool_problem

logger = logging.getLogger("maiwork.search")

_MCP_RETRY_DELAY_S = 2.0  # MCP 端点 5xx 时隔几秒重试（只重试一次）
_SNIPPET_MAX = 500
_EXTRACT_MAX = 20000

# 参数名候选（按优先级）
_QUERY_FIELDS = ("query", "q")
_LIMIT_FIELDS = ("count", "max_results", "num_results", "numResults", "limit", "top_k")
_FRESHNESS_ENUMS = ("day", "week", "month", "year")


class SearchUnavailable(Exception):
    """搜索没配好（没绑定 / 绑定的扩展不在、没启用、没连上）；调用方应跳过本次备料。"""


class SearchError(Exception):
    """搜索请求失败（MCP 调用、返回解析）；message 已去掉已知密钥。"""


def _mask(text: str, keys: list[str]) -> str:
    """把错误原文里的已知密钥遮掉，顺手截短。"""
    out = str(text or "")
    for key in keys:
        if key:
            out = out.replace(key, "***")
    return out[:300]


def _secret_variants(value: str) -> list[str]:
    """一个头值的遮罩口径：整串 + Bearer/Bearer 前缀剥掉后的裸密钥。"""
    v = str(value or "").strip()
    if not v:
        return []
    out = [v]
    low = v.lower()
    if low.startswith("bearer "):
        bare = v[7:].strip()
        if bare:
            out.append(bare)
    return out


def _parse_dt_utc(s: Any) -> float | None:
    """ISO 时间 → epoch 秒；没带时区的按 UTC。解析不了返回 None。"""
    if not s:
        return None
    try:
        text = str(s).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _time_range_of(days: int) -> str:
    """天数 → 枚举：≤1 day、≤7 week、≤31 month、否则 year。"""
    d = int(days)
    if d <= 1:
        return "day"
    if d <= 7:
        return "week"
    if d <= 31:
        return "month"
    return "year"


# ----------------------------------------------------------------------
# 参数按 inputSchema 映射
# ----------------------------------------------------------------------


def _schema_props(schema: Any) -> tuple[dict[str, dict], list[str]]:
    if not isinstance(schema, dict):
        return {}, []
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
    required = schema.get("required")
    req = [str(r) for r in required if isinstance(r, str)] if isinstance(required, list) else []
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in props.items()}, req


def _field_type(spec: dict) -> str:
    t = spec.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list) and t:
        return str(t[0])
    return ""


def _query_field(props: dict[str, dict], required: list[str]) -> str:
    for name in _QUERY_FIELDS:
        if name in props and _field_type(props[name]) in ("string", ""):
            return name
    for name in required:
        if name in props and _field_type(props[name]) == "string":
            return name
    # 第一个 string 字段兜底
    for name, spec in props.items():
        if _field_type(spec) == "string":
            return name
    return ""


def map_search_arguments(schema: Any, query: str, *, limit: int, days: int | None) -> dict[str, Any]:
    """{query, limit, days} → 按绑定工具的 inputSchema 拼 arguments；映射不上的不传。"""
    props, required = _schema_props(schema)
    args: dict[str, Any] = {}
    qf = _query_field(props, required)
    if qf:
        args[qf] = query
    else:
        args["query"] = query  # schema 完全没声明时按惯例给 query（服务端不认会报错，文本也看得懂）
    for name in _LIMIT_FIELDS:
        if name in props and _field_type(props[name]) in ("integer", "number"):
            args[name] = max(1, int(limit))
            break
    if days:
        d = int(days)
        done = False
        for name in ("days",):
            if name in props and _field_type(props[name]) in ("integer", "number"):
                args[name] = d
                done = True
                break
        if not done:
            for name in ("freshness", "time_range"):
                spec = props.get(name)
                if spec and _field_type(spec) == "string":
                    enum = spec.get("enum")
                    if isinstance(enum, list) and any(str(e) in _FRESHNESS_ENUMS for e in enum):
                        args[name] = _time_range_of(d)
                        done = True
                        break
        if not done:
            for name in ("start_published_date", "startPublishedDate"):
                spec = props.get(name)
                if spec and _field_type(spec) == "string":
                    since = datetime.now(timezone.utc) - timedelta(days=d)
                    args[name] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    done = True
                    break
        if not done:
            for name, spec in props.items():
                if "date" in name.lower() and _field_type(spec) == "string":
                    since = datetime.now(timezone.utc) - timedelta(days=d)
                    args[name] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    break
    return args


def map_extract_arguments(schema: Any, url: str) -> dict[str, Any]:
    """extract：schema 有 urls（array）→ {"urls": [url]}；有 url（string）→ {"url": url}；
    都没有按惯例给 {"urls": [url]}（Tavily 惯例）。formats 字段声明了就给 markdown。"""
    props, _ = _schema_props(schema)
    args: dict[str, Any] = {}
    urls_spec = props.get("urls")
    if urls_spec is not None and _field_type(urls_spec) == "array":
        args["urls"] = [url]
    elif props.get("url") is not None and _field_type(props["url"]) == "string":
        args["url"] = url
    elif urls_spec is not None:
        args["urls"] = [url]
    else:
        args["urls"] = [url]
    formats_spec = props.get("formats")
    if formats_spec is not None and _field_type(formats_spec) == "array":
        args["formats"] = ["markdown"]
    return args


# ----------------------------------------------------------------------
# 结果归一化
# ----------------------------------------------------------------------


def _mcp_tool_json(result: dict[str, Any]) -> dict | None:
    """MCP tools/call 的 result → content[0].text 里的 JSON 对象；不是 JSON/不是对象返回 None。"""
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, dict) or not isinstance(first.get("text"), str):
        return None
    try:
        payload = json.loads(first["text"])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _mcp_structured(result: dict[str, Any]) -> dict | None:
    """优先 result.structuredContent（MCP 2025-06 规范的结构化结果）；没有再退回 content[0].text 的 JSON。"""
    sc = result.get("structuredContent") if isinstance(result, dict) else None
    if isinstance(sc, dict):
        return sc
    return _mcp_tool_json(result) if isinstance(result, dict) else None


def _content_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(p for p in parts if p)


def _find_url_lists(node: Any, out: list[list[dict]], depth: int = 0) -> None:
    """在任意层找「带 url 的 dict 列表」（至少一条有 url）。"""
    if depth > 6:
        return
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if dicts and any(str(x.get("url") or "").strip() for x in dicts):
            out.append(dicts)
            return
        for x in node:
            _find_url_lists(x, out, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            _find_url_lists(v, out, depth + 1)


def _first_str(item: dict, *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _snippet_of(item: dict) -> str:
    parts: list[str] = []
    main = _first_str(item, "snippet", "description", "content", "text", "body", "summary")
    if main:
        parts.append(main)
    contents = item.get("contents")
    if isinstance(contents, dict):
        hl = contents.get("highlights")
        if isinstance(hl, list):
            parts += [str(h).strip() for h in hl if str(h).strip()]
    elif isinstance(contents, list):
        parts += [str(h).strip() for h in contents if isinstance(h, str) and h.strip()]
    for k in ("highlights", "snippets"):
        v = item.get(k)
        if isinstance(v, list):
            parts += [str(h).strip() for h in v if str(h).strip()]
    # 去重保持顺序
    seen: set[str] = set()
    uniq = [p for p in parts if p and not (p in seen or seen.add(p))]
    return "\n".join(uniq)[:_SNIPPET_MAX]


def _published_of(item: dict) -> float | None:
    for k in ("published", "published_date", "publishedDate", "page_age", "date", "age"):
        v = item.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            return float(v)
        ts = _parse_dt_utc(v)
        if ts is not None:
            return ts
    return None


def normalize_search_results(result: dict[str, Any]) -> list[dict]:
    """tools/call 的 result → 统一 [{"title","url","snippet","published"}]。

    优先 structuredContent / content[0].text 的 JSON；在任意层找带 url 的 dict 列表
    （取最长的一份，You.com 的 results.web/news 会被拼成一份）；实在拿不到结构，
    把原文本截断当一条（url 空）返回。
    """
    payload = _mcp_structured(result)
    lists: list[list[dict]] = []
    if payload is not None:
        _find_url_lists(payload, lists)
    out: list[dict] = []
    seen: set[str] = set()
    for lst in lists:
        for item in lst:
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "title": _first_str(item, "title", "name"),
                "url": url,
                "snippet": _snippet_of(item),
                "published": _published_of(item),
            })
    if out:
        return out
    # 纯文本回落：整段文本当一条结果（本身是 JSON 的就别当文本给人看了）
    text = _content_text(result).strip()
    if text:
        try:
            json.loads(text)
            return []
        except (ValueError, TypeError):
            pass
        return [{"title": "", "url": "", "snippet": text[:_SNIPPET_MAX], "published": None}]
    return []


def normalize_extract_text(result: dict[str, Any]) -> str:
    """extract 工具的 result → 正文文本（截 _EXTRACT_MAX）。

    在结构化结果里找 markdown|raw_content|content|text 里最长的一段；找不到退回 content 的纯文本。
    """
    payload = _mcp_structured(result) or {}
    candidates: list[str] = []

    def _collect(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            for k in ("markdown", "raw_content", "content", "text"):
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _collect(v, depth + 1)
        elif isinstance(node, list):
            for x in node:
                _collect(x, depth + 1)

    _collect(payload)
    if candidates:
        best = max(candidates, key=len)
        # 带上标题（找得到的第一个）
        title = ""
        def _find_title(node: Any, depth: int = 0) -> str:
            if depth > 6:
                return ""
            if isinstance(node, dict):
                t = node.get("title")
                if isinstance(t, str) and t.strip():
                    return t.strip()
                for v in node.values():
                    r = _find_title(v, depth + 1)
                    if r:
                        return r
            elif isinstance(node, list):
                for x in node:
                    r = _find_title(x, depth + 1)
                    if r:
                        return r
            return ""
        title = _find_title(payload)
        return ((f"《{title}》\n" if title else "") + best)[:_EXTRACT_MAX]
    text = _content_text(result).strip()
    return text[:_EXTRACT_MAX]


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


class Search:
    """通用 MCP 搜索适配层。连接复用 extensions 里那个扩展的 client，不自己建。"""

    def __init__(self, store: Any, get_extensions: Callable[[], Any]) -> None:
        self._store = store
        self._get_extensions = get_extensions

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _runtime(self, name: str) -> Any:
        extensions = self._extensions()
        if extensions is None:
            return None
        getter = getattr(extensions, "runtime_of", None)
        return getter(name) if callable(getter) else None

    def _extensions(self) -> Any:
        try:
            return self._get_extensions() if self._get_extensions is not None else None
        except Exception:
            return None

    def _settings(self) -> Any:
        """配置快照：Extensions 有 _get_settings 就用它的；没有（假对象/老测试）给最小空配置。

        搜索绑定只用到 [extensions] 节（merged_entries），别的节用不上——给最小空配置
        既不绑死构造参数，也让「只有假 Extensions」的测试照常跑。
        """
        extensions = self._extensions()
        gs = getattr(extensions, "_get_settings", None) if extensions is not None else None
        if callable(gs):
            try:
                settings = gs()
                if settings is not None:
                    return settings
            except Exception:
                pass
        from .config import load_settings

        settings, _ = load_settings({})
        return settings

    def status(self) -> tuple[bool, str]:
        """(可用, 中文说明)；views 的健康项 / onboarding 的 checks 用它。"""
        try:
            settings = self._settings()
        except Exception:
            settings = None
        return status_of(self._store, settings, self._runtime)

    def available(self) -> bool:
        """有绑定且该扩展已启用、连得上、工具在。"""
        try:
            ok, _ = self.status()
            return ok
        except Exception:
            return False

    def known_secrets(self) -> list[str]:
        """绑定扩展 headers 里的值（并进 Tools 的遮罩，防错误文本/日志泄露）。

        优先运行状态里的那份（热改过的真实值），拿不到再回配置/数据库合并出来的条目。
        """
        out: list[str] = []
        try:
            binding = get_binding(self._store)
            if binding is None:
                return out
            names = [binding["mcp"]]
            if binding["extract_mcp"] and binding["extract_mcp"] not in names:
                names.append(binding["extract_mcp"])
            for name in names:
                headers: dict = {}
                runtime = self._runtime(name)
                rt_headers = getattr(runtime, "headers", None) if runtime is not None else None
                if isinstance(rt_headers, dict) and rt_headers:
                    headers = rt_headers
                else:
                    entry = None
                    settings = self._settings()
                    if settings is not None:
                        from . import extensions_web

                        for e in extensions_web.merged_entries(settings, self._store):
                            if e.name == name:
                                entry = e
                                break
                    if entry is not None:
                        headers = dict(getattr(entry, "headers", {}) or {})
                for v in headers.values():
                    for piece in _secret_variants(str(v or "")):
                        if piece and piece not in out:
                            out.append(piece)
        except Exception:
            pass
        return out

    async def aclose(self) -> None:
        """没事做：连接归 extensions 管（reload / remove / 插件停都会收它）。"""
        return None

    # close() 的别名，app 用 getattr 找 "close"
    close = aclose

    # ------------------------------------------------------------------
    # 连接（复用绑定扩展的现有 client）
    # ------------------------------------------------------------------

    def _client(self) -> tuple[Any, dict[str, str], Any]:
        """(client, binding, runtime)；不可用抛 SearchUnavailable（中文）。"""
        binding = get_binding(self._store)
        if binding is None:
            raise SearchUnavailable("还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")
        settings = self._settings()
        ok, text = status_of(self._store, settings, self._runtime)
        if not ok:
            raise SearchUnavailable(text)
        runtime = self._runtime(binding["mcp"])
        return runtime.client, binding, runtime

    def _schema_of(self, runtime: Any, tool_name: str) -> dict:
        try:
            tools = runtime.tools_remote() or {}
        except Exception:
            tools = {}
        spec = tools.get(tool_name)
        schema = spec.get("inputSchema") if isinstance(spec, dict) else None
        return schema if isinstance(schema, dict) else {}

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    async def search(self, query: str, *, limit: int = 8, days: int | None = None) -> list[dict]:
        client, binding, runtime = self._client()
        schema = self._schema_of(runtime, binding["tool"])
        arguments = map_search_arguments(schema, query, limit=limit, days=days)
        try:
            result = await _call_retry_5xx(client, binding["tool"], arguments)
        except MCPError as e:
            raise SearchError(_mask(f"搜索失败（{binding['mcp']}）：{e}", self.known_secrets())) from None
        return normalize_search_results(result)[: max(1, int(limit))]

    # ------------------------------------------------------------------
    # 抓正文
    # ------------------------------------------------------------------

    async def extract(self, url: str) -> str:
        """抽取一个网页的正文（fetch_page 的备用路）。没绑 extract_tool / 那家用不了 → ""。"""
        ok, _text = self.extract_available()
        if not ok:
            return ""
        binding = get_binding(self._store) or {}
        mcp, tool = binding["extract_mcp"], binding["extract_tool"]
        runtime = self._runtime(mcp)
        schema = self._schema_of(runtime, tool)
        arguments = map_extract_arguments(schema, url)
        try:
            result = await _call_retry_5xx(runtime.client, tool, arguments)
        except MCPError as e:
            raise SearchError(_mask(f"抽取失败（{mcp}）：{e}", self.known_secrets())) from None
        return normalize_extract_text(result)

    def extract_available(self) -> tuple[bool, str]:
        """抓正文工具现在能不能用：(能用, 中文说明)。只看抓正文那家自己（它可以和搜索不是同一家，
        搜索那家关了不影响这里）。没绑 → (False, "没选抓正文工具")。"""
        binding = get_binding(self._store)
        if binding is None or not binding["extract_tool"]:
            return False, "没选抓正文工具"
        try:
            problem = tool_problem(
                self._store, self._settings(), self._runtime,
                binding["extract_mcp"], binding["extract_tool"], role="抓正文",
            )
        except Exception:
            problem = "抓正文扩展状态读不出来"
        if problem:
            return False, problem
        return True, f"{binding['extract_mcp']} 的 {binding['extract_tool']}"


def _is_5xx(err: MCPError) -> bool:
    return "HTTP 5" in str(err)


async def _call_retry_5xx(client: McpSessionClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """tools/call；端点回 5xx（多半是服务方或前面 CDN 临时出问题）隔一会儿重试一次。"""
    try:
        return await client.call_tool(name, arguments)
    except MCPError as e:
        if not _is_5xx(e):
            raise
        logger.info("MCP 搜索端点临时出错（%s），%s 秒后重试一次", str(e)[:80], _MCP_RETRY_DELAY_S)
    await asyncio.sleep(_MCP_RETRY_DELAY_S)
    return await client.call_tool(name, arguments)
