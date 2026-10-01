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
- **没绑定**（kv 里没有 extensions.search）→ SearchUnavailable（中文提示去 设置 → 扩展 绑定），
  零联网调用；**绑定了但主家暂时坏了**（扩展不存在/没启用/没连上/初始化失败留下空工具清单/
  工具清单里没这个工具）也按 fallback 名单递补，和调用失败（超时 / 429 / 5xx）一个口径——
  全链都挂了才抛最后一个错（A05）。
- 调用失败 → SearchError（已遮扩展 headers 里的密钥）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .mcp_client import MCPError, McpSessionClient
from . import extensions_web, search_binding, search_presets
from .search_binding import get_binding, status_of, tool_problem

logger = logging.getLogger("maiwork.search")

_MCP_RETRY_DELAY_S = 2.0  # MCP 端点 5xx 时隔几秒重试（只重试一次）
# 摘要截断：Keenable 的单条 Snippets 普遍 500~900 字（tests/fixtures/search_presets/
# keenable_search.json 实测），500 会砍掉一半——抬到 800 覆盖大多数，喂子 agent 也够。
_SNIPPET_MAX = 800
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
    text = first["text"]
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_titled_blocks(text: str) -> list[dict]:
    """Keenable 等直接把结果打成纯文本块的格式（2026-09-30 实测）：

        Title: ...\nURL: ...\nPublished: 2026-09-24\nAcquired: ...\nSnippets:\n<多行摘要>
        \n---\n  再下一条

    通用识别（不只 keenable）：任意一家打成 Title:/URL: 键值块的都拆。
    拆出一条就算成功（调用方决定用不用）；URL 至少要像 http(s) 才算数。
    """
    out: list[dict] = []
    for block in str(text or "").split("\n\n---\n\n"):
        lines = block.splitlines()
        if not any(l.startswith("Title:") for l in lines[:8]):
            continue
        header: dict[str, str] = {}
        snippet_lines: list[str] = []
        in_snippets = False
        for line in lines:
            if not in_snippets:
                m = line.split(":", 1)
                if (
                    len(m) == 2
                    and m[0].strip() in ("Title", "URL", "Published", "Acquired")
                ):
                    key = m[0].strip()
                    value = m[1].strip()
                    if key == "Title":
                        header["title"] = value
                    elif key == "URL":
                        header["url"] = value
                    elif key == "Published":
                        header["published"] = value
                    # Acquired 是「抓进索引的时间」，不当发布时间
                elif line.strip() == "Snippets:":
                    in_snippets = True
            else:
                snippet_lines.append(line)
        if not str(header.get("url") or "").startswith("http"):
            continue
        out.append({
            "title": header.get("title", ""),
            "url": header["url"],
            "snippet": "\n".join(snippet_lines).strip()[:_SNIPPET_MAX],
            "published": _parse_dt_utc(header.get("published", "")),
        })
    return out


def _split_title_url_text(text: str) -> tuple[str, str] | None:
    """「Title: ...\\nURL: ...\\n\\n正文」的纯文本 → (标题, 正文)；不是这个形状 → None。

    Exa 的 web_fetch_exa 还会在标题前加 markdown 的「# 标题」行——# 行优先当标题，
    不然用 Title: 那行（keenable 格式）。
    """
    lines = str(text or "").splitlines()
    if len(lines) < 3:
        return None
    url_idx = -1
    for i in range(min(3, len(lines))):
        if lines[i].startswith("URL: ") and lines[i][5:].strip().startswith("http"):
            url_idx = i
            break
    if url_idx < 0:
        return None
    heading = ""
    title = ""
    for i in range(url_idx):
        line = lines[i].strip()
        if line.startswith("# ") and not heading:
            heading = line[2:].strip()
        if line.startswith("Title:") and not title:
            title = line[6:].strip()
    title = heading or title
    if not title:
        return None
    first_body = url_idx + 1
    if first_body < len(lines) and not lines[first_body].strip():
        first_body += 1  # 跳过 URL 和正文之间的空行
    body = "\n".join(lines[first_body:]).strip()
    if not body:
        return None
    return title, body


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
    for k in ("published", "published_at", "published_date", "publishedDate", "page_age", "date", "age"):
        v = item.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            f = float(v)
            # 只认秒级 epoch（约 2001~2033 年）；毫秒值 / 相对计数不瞎转成时间
            return f if 1_000_000_000 <= f <= 2_000_000_000 else None
        ts = _parse_dt_utc(v)
        if ts is not None:
            return ts
        # "3 days ago" 之类相对话解析不了 → None（不许编时间）
    return None


def _check_mcp_error(result: Any, role: str) -> None:
    """MCP result.isError=True → SearchError（中文；Tavily 免密钥月度上限给专门话术）。

    2026-09-30 实测 Tavily 的月度上限回包「isError 被透出成 False」、错误码塞在
    structuredContent/text 里——对 monthly_cap_reached 这类明显是配额的，
    即便 isError 丢真也照样当错误报。
    """
    if not isinstance(result, dict):
        return
    is_error = bool(result.get("isError"))
    if not is_error:
        # cap 探测：structuredContent.code 或 content 文本里写明的配额错误
        text = _content_text(result)
        sc = result.get("structuredContent")
        code = sc.get("code") if isinstance(sc, dict) else ""
        if "monthly_cap_reached" in str(code or "") or "monthly_cap_reached" in text:
            raise SearchError(
                "Tavily 免密钥额度这个月用完了：去 https://app.tavily.com 拿一个免费密钥填上"
            )
        return
    body = ""
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("message"), str):
        body = sc["message"]
    if not body:
        body = _content_text(result)
    # 空 content 的 isError 也别漏：给个兜底词
    body = body.strip() or "（没给原因）"
    if "monthly_cap_reached" in body:
        raise SearchError(
            "Tavily 免密钥额度这个月用完了：去 https://app.tavily.com 拿一个免费密钥填上"
        )
    raise SearchError(f"{role}端返回错误：{_mask(body, [])}")


def normalize_search_results(result: dict[str, Any]) -> list[dict]:
    """tools/call 的 result → 统一 [{"title","url","snippet","published"}]。

    优先 structuredContent / content[0].text 的 JSON；在任意层找带 url 的 dict 列表
    （取最长的一份，You.com 的 results.web/news 会被拼成一份）；实在拿不到结构，
    把原文本截断当一条（url 空）返回。
    """
    _check_mcp_error(result, "搜索")
    payload = _mcp_structured(result)
    # Keenable 等纯文本块（Title:/URL:/Snippets:）——先按块拆，拆得出来就不走 dict 列表那套
    blocks = _parse_titled_blocks(_content_text(result))
    if blocks:
        return blocks
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
    Keenable / Exa 的抓正文是「Title: ...\\nURL: ...\\n\\n正文」纯文本——标题提出来按《标题》带上。
    """
    _check_mcp_error(result, "抓正文")
    titled = _split_title_url_text(_content_text(result))
    if titled is not None:
        title, body = titled
        return ((f"《{title}》\n" if title else "") + body)[:_EXTRACT_MAX]
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
        """有效搜索链能不能用：主家用得了，或主家坏了但备用名单里有一家能用（A05）。"""
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
        """(client, binding, runtime)；主绑定自己不能用抛 SearchUnavailable（中文）。

        只看主家（不看备用递补）：调用方（search 的主家那一步）拿它失败去换备用，
        整条链可不可用由 status_of / status() 判，别在这里提前下结论。
        """
        binding = get_binding(self._store)
        if binding is None:
            raise SearchUnavailable("还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")
        problem = tool_problem(
            self._store, self._settings(), self._runtime, binding["mcp"], binding["tool"], role="搜索"
        )
        if problem:
            raise SearchUnavailable(problem)
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

    async def search(
        self,
        query: str,
        *,
        limit: int = 8,
        days: int | None = None,
        site: str = "",
        news: bool = False,
    ) -> list[dict]:
        """主绑定先搜；主家「坏了」（没连上 / 初始化失败留下空工具清单 / 工具清单里没这个工具）
        或调用失败（MCPError / isError 的 SearchError / 超时 / 429 / 5xx）都按 binding["fallback"]
        里认得出的预设家一个个递补；全挂抛最后一个错误。

        没绑定 → 直接 SearchUnavailable（不打网络）。site/news 只在预设路上生效。
        每条结果带 "provider" = 实际出结果的扩展名（多provider拼接时认来源用）。"""
        settings = self._settings()
        entries = {e.name: e for e in extensions_web.merged_entries(settings, self._store)}
        binding = get_binding(self._store)
        if binding is None:
            raise SearchUnavailable("还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")
        errors: list[Exception] = []
        names = self._fallback_chain(entries, binding)
        for name in names:
            try:
                out = await self._search_one(name, query, limit=limit, days=days, site=site, news=news,
                                             entries=entries)
            except SearchUnavailable as e:
                # 主家绑定了但这家暂时用不了：记下来接着试备用（A05），不再直接抛
                errors.append(e)
                logger.info("搜索家 %s 现在用不了（%s），试下一家", name, e)
                continue
            except (SearchError, MCPError, asyncio.TimeoutError, TimeoutError) as e:
                errors.append(e)
                logger.info("搜索家 %s 没搜成（%s），试下一家", name, type(e).__name__)
                continue
            return out
        if errors:
            raise errors[-1]
        raise SearchUnavailable("还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")  # 理论到不了

    def _fallback_chain(self, entries: dict[str, Any], binding: dict[str, Any] | None = None) -> list[str]:
        """搜索要试的扩展顺序：[主绑定] + [fallback 名单里认得出预设且启用的]。"""
        if binding is None:
            binding = get_binding(self._store)
        if binding is None:
            raise SearchUnavailable("还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")
        chain = [binding["mcp"]]
        for name in binding.get("fallback") or []:
            entry = entries.get(name)
            if entry is None or not getattr(entry, "enabled", False):
                continue
            if search_presets.preset_of_url(getattr(entry, "url", "")) is None:
                logger.info("fallback 的 %s 不是预设认得出的搜索服务，跳过", name)
                continue
            if name not in chain:
                chain.append(name)
        return chain

    async def search_with(
        self,
        name: str,
        query: str,
        *,
        limit: int = 8,
        days: int | None = None,
        site: str = "",
        news: bool = False,
    ) -> list[dict]:
        """指定一家（启用中且预设认得出的）扩展搜一次——撒大网多provider搜用。"""
        settings = self._settings()
        entries = {e.name: e for e in extensions_web.merged_entries(settings, self._store)}
        if name not in entries:
            raise SearchUnavailable(f"没有这个扩展「{name}」")
        entry = entries[name]
        if search_presets.preset_of_url(getattr(entry, "url", "")) is None:
            raise SearchUnavailable(f"扩展「{name}」不是预设认得出的搜索服务，撒网搜不了")
        return await self._search_one(name, query, limit=limit, days=days, site=site, news=news,
                                      entries=entries)

    def broad_providers(self) -> list[str]:
        """撒大网要搜哪几家：[主绑定] + binding["broad"] 里启用、预设认得出的。"""
        settings = self._settings()
        try:
            entries = {e.name: e for e in extensions_web.merged_entries(settings, self._store)}
        except Exception:
            entries = {}
        binding = get_binding(self._store)
        if binding is None:
            return []
        out = [binding["mcp"]]
        for name in binding.get("broad") or []:
            entry = entries.get(name)
            if entry is None or not getattr(entry, "enabled", False):
                continue
            if search_presets.preset_of_url(getattr(entry, "url", "")) is None:
                continue
            if name not in out:
                out.append(name)
        return out

    async def _search_one(
        self,
        name: str,
        query: str,
        *,
        limit: int,
        days: int | None,
        site: str,
        news: bool,
        entries: dict[str, Any],
    ) -> list[dict]:
        """用指定扩展搜一次。url 被预设认出 → 预设参数（site/news 生效）；
        否则通用 schema 映射（site/news 忽略，保持老行为）。结果带 provider。"""
        binding = get_binding(self._store)
        if binding is None:
            raise SearchUnavailable("还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")
        if name == binding["mcp"]:
            # 主绑定：复用 _client() 的状态判断 + 绑定里记的工具名
            client, _, runtime = self._client()
            tool = binding["tool"]
        else:
            entry = entries.get(name)
            if entry is None:
                raise SearchError(f"搜索家「{name}」不在了")
            runtime = self._runtime(name)
            client = getattr(runtime, "client", None) if runtime is not None else None
            preset = search_presets.preset_of_url(getattr(entry, "url", ""))
            if preset is None:
                raise SearchError(f"搜索家「{name}」不是预设认得出的，补不上")
            if client is None:
                raise SearchError(f"搜索家「{name}」还没连上，补不上")
            tool = preset.search_tool
        schema = self._schema_of(runtime, tool)
        entry = entries.get(name)
        preset = search_presets.preset_of_url(getattr(entry, "url", "")) if entry is not None else None
        if preset is not None:
            arguments = search_presets.filter_to_schema(
                search_presets.search_args(preset.id, query, limit=limit, days=days, site=site, news=news),
                schema,
            )
        else:
            arguments = map_search_arguments(schema, query, limit=limit, days=days)
        try:
            result = await _call_retry_5xx(client, tool, arguments)
        except MCPError as e:
            raise SearchError(_mask(f"搜索失败（{name}）：{e}", self.known_secrets())) from None
        try:
            out = normalize_search_results(result)
        except SearchError as e:
            # isError 也算这家没搜成；带是谁家的好递补/交差
            raise SearchError(f"{name}：{e}") from None
        for item in out:
            item["provider"] = name
        return out[: max(1, int(limit))]

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
        arguments = self._extract_arguments(mcp, url, schema)
        try:
            result = await _call_retry_5xx(runtime.client, tool, arguments)
        except MCPError as e:
            raise SearchError(_mask(f"抽取失败（{mcp}）：{e}", self.known_secrets())) from None
        return normalize_extract_text(result)

    def _extract_arguments(self, mcp: str, url: str, schema: dict) -> dict:
        """抓正文参数：扩展 url 被预设认出 → 预设参数（滤运行时 schema）；
        预设没给固定参数（如 you-contents）/ 认不出 → 通用映射。"""
        try:
            from .config import load_settings  # 无需时早早返回；真要用时兜底的最小空配置也一样走

            settings = self._settings()
            for e in extensions_web.merged_entries(settings, self._store):
                if e.name != mcp:
                    continue
                preset = search_presets.preset_of_url(getattr(e, "url", ""))
                if preset is None:
                    break
                preset_args = search_presets.extract_args(preset.id, url, keyed=bool(
                    getattr(e, "headers", None)))
                if preset_args is None:
                    break  # you-contents 那种 schema 待核实的 → 通用
                return search_presets.filter_to_schema(preset_args, schema)
        except Exception:
            logger.debug("抓正文预设参数读不出来，走通用映射", exc_info=True)
        return map_extract_arguments(schema, url)

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
