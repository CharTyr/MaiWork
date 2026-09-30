"""M2 内置工具（docs/07-代码接口.md §10.2）：web_search / fetch_page / read_profile / submit_result。

register_builtin(tools, *, search, profiles, ...) 一次把四个工具注册进 Tools。

fetch_page 打开顺序（2026-09-28 用户定）：Jina Reader（reader.py）→「抓网页正文」工具（联网搜索里
绑定的 MCP，可以和搜索不是同一家）→ 服务器直接打开。前一条路有任何问题（限流、超时、验证页、空正文…）
马上换下一条；Jina 关着时退回老顺序：直接打开，被网站拦了再用抓正文工具。

fetch_page 安全规则：
- 只允许 http/https；
- 解析主机名（可注入 resolver 方便测试），命中内网/本机/链路本地/保留地址一律拒；
- 手动跟随跳转（最多 5 次），每一跳的目标地址都重新做同样的检查——
  防止「外网 URL 302 到 169.254.169.254 元数据接口」这种内网穿透；
- 响应体最多读 200KB；只处理 text/html、text/plain、application/json；
- HTML 提取优先用 trafilatura（宿主有，本地测试机未必有，try/except 兜底），
  否则用标准库 html.parser 去 script/style/nav 等再压缩空白；同时抽 <title>。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_builtin")

_MAX_REDIRECTS = 5
_MAX_BODY_BYTES = 200 * 1024      # 响应体最多读 200KB
_TEXT_MAX_CHARS = 8000            # 正文截 8000 字
_ALLOWED_PREFIXES = ("text/html", "text/plain", "application/json")
_SKIP_TAGS = frozenset({"script", "style", "nav", "noscript", "iframe", "svg", "head"})

CATEGORY_NAMES = {
    "recent": "最近在聊",
    "interest": "长期兴趣",
    "ongoing": "在做的事",
    "convention": "约定和说法",
    "resource": "常用资源",
}


# ----------------------------------------------------------------------
# 内网地址检查
# ----------------------------------------------------------------------

Resolver = Callable[[str], list[str]]



def _maybe_json(text: str) -> tuple[Any, bool]:
    """字符串看着像 JSON（去掉 ```json 围栏后以 { 或 [ 开头）就解析。

    返回 (值, 解析失败?)；不像 JSON 的原样返回、不算失败。
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    if not t or t[0] not in "{[":
        return text, False
    try:
        return json.loads(t), False
    except ValueError:
        return text, True

def _default_resolver(host: str) -> list[str]:
    """默认 DNS 解析：返回该主机名的所有 A/AAAA 地址字符串。"""
    infos = socket.getaddrinfo(host, None)
    return sorted({str(info[4][0]) for info in infos})


def _ip_is_forbidden(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True  # 解析不出来的当成可疑，拒
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _host_is_forbidden(host: str, resolver: Resolver) -> bool:
    """主机名（含 localhost、IP 字面量、解析结果任一命中内网）→ True。"""
    host = host.strip().lower().strip("[]")
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _ip_is_forbidden(host)
    try:
        ips = resolver(host)
    except Exception:
        return True  # DNS 解析失败宁可拒
    if not ips:
        return True
    return any(_ip_is_forbidden(ip) for ip in ips)


# ----------------------------------------------------------------------
# HTML 正文提取
# ----------------------------------------------------------------------


def _extract_with_trafilatura(html: str) -> tuple[str, str] | None:
    """宿主有 trafilatura 就用它；没有/失败返回 None 走兜底。返回 (title, text)。"""
    try:
        import trafilatura  # type: ignore
    except Exception:
        return None
    try:
        text = trafilatura.extract(html or "", include_comments=False, include_tables=True)
        if text is None:
            return None
        return "", str(text)
    except Exception:
        return None


class _TextExtractor(HTMLParser):
    """标准库兜底：去 script/style/nav 等，收集文字和 <title>。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # title 在 head 里，要先判 title 再判跳过标签，否则标题被 head 的跳过逻辑吃掉
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth > 0:
            return
        else:
            self.parts.append(data)


def _squeeze(text: str) -> str:
    """压缩空白：连续空白变一个空格，连续空行变一个换行。"""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_fallback(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # HTML 再烂也不能炸
        pass
    return _squeeze("".join(parser.title_parts)), _squeeze("".join(parser.parts))


def _extract_html(html: str) -> tuple[str, str]:
    """(title, 正文)；优先 trafilatura，失败用标准库兜底。"""
    via = _extract_with_trafilatura(html)
    if via is not None:
        title_fb, _ = _extract_fallback(html)  # trafilatura 只出正文，标题仍用兜底抽
        return title_fb, _squeeze(via[1])
    return _extract_fallback(html)


# ----------------------------------------------------------------------
# fetch_page
# ----------------------------------------------------------------------


class _OgImageParser(HTMLParser):
    """从 HTML 里抽 og:image 的 content（meta property= / name=、引号单双都行、属性乱序都行）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.image or tag.lower() != "meta":
            return
        attr = {str(k).lower(): (v or "") for k, v in attrs}
        kind = (attr.get("property") or attr.get("name") or "").strip().lower()
        if kind in ("og:image", "og:image:url", "og:image:secure_url"):
            self.image = attr.get("content", "").strip()


def _absolute_http_url(raw: str, base: str) -> str:
    """洗净成 http(s) 绝对地址；相对地址按 base 解析；别的协议 / 解析不了 → ""。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    resolved = urljoin(base, s)
    try:
        p = urlparse(resolved)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    return p.geturl()


def _url_problem(url: str, resolver: Resolver) -> str:
    """交给任何一条路之前先查：只收公开的 http(s) 地址；有问题 → 中文原因，没问题 → ""。"""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return f"只支持 http/https 链接，{parsed.scheme or '(没有协议)'} 不支持"
    if not parsed.hostname:
        return "链接里没有主机名，打不开"
    if _host_is_forbidden(parsed.hostname, resolver):
        return f"{parsed.hostname} 解析到内网/本机地址，不允许打开"
    return ""


def _extract_og_image(html: str, final_url: str) -> str:
    """页面的 og:image（只收 http(s) 绝对地址；相对地址按最终页面 URL 解析）。"""
    parser = _OgImageParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    return _absolute_http_url(parser.image, final_url)


async def _fetch_page_text(
    url: str, *, transport: Any, resolver: Resolver
) -> tuple[bool, str, str, str]:
    """跟随跳转抓页面，返回 (ok, 文本或错误原因, og:image 或 "", 最终地址)。

    最终地址 = 跟随跳转后真正取到正文的那个 URL（没跳转就等于请求地址）；失败时是 ""。
    交付里引用的常是跳转后的长链，验收引用核对要把它也算作「打开过」。
    错误文本面向子 agent（中文）。
    """
    current = str(url or "").strip()
    for hop in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return False, f"只支持 http/https 链接，{parsed.scheme or '(没有协议)'} 不支持", "", ""
        if not parsed.hostname:
            return False, "链接里没有主机名，打不开", "", ""
        if _host_is_forbidden(parsed.hostname, resolver):
            return False, f"{parsed.hostname} 解析到内网/本机地址，不允许打开", "", ""
        try:
            async with httpx.AsyncClient(transport=transport, timeout=20.0) as client:
                resp = await client.get(current, follow_redirects=False)
        except httpx.HTTPError as e:
            return False, f"打开页面失败：{e}", "", ""
        if resp.status_code in (301, 302, 303, 307, 308):
            if hop >= _MAX_REDIRECTS:
                return False, f"跳转超过 {_MAX_REDIRECTS} 次，放弃", "", ""
            location = resp.headers.get("location") or ""
            if not location:
                return False, f"页面返回 {resp.status_code} 但没给跳转到哪", "", ""
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            return False, f"页面返回 {resp.status_code}", "", ""
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not any(ctype.startswith(p) for p in _ALLOWED_PREFIXES):
            return False, f"页面类型是 {ctype}，只支持网页/纯文本/JSON", "", ""
        raw = resp.content[: _MAX_BODY_BYTES + 1]
        if len(raw) > _MAX_BODY_BYTES:
            raw = raw[:_MAX_BODY_BYTES]
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        image_url = ""
        if ctype.startswith("text/html") or (not ctype and text.lstrip().startswith("<")):
            image_url = _extract_og_image(text, current)
            title, body = _extract_html(text)
            text = (f"《{title}》\n{body}" if title else body) or text
        if len(text) > _TEXT_MAX_CHARS:
            text = text[:_TEXT_MAX_CHARS] + " …（后面还有，已截断）"
        return True, text, image_url, current
    return False, "跳转次数太多，放弃", "", ""


# ----------------------------------------------------------------------
# 最终地址（验收引用核对要认它；不改 tool_calls 表结构，写在 output 摘要里）
# ----------------------------------------------------------------------

# 请求日志里标明是哪条路打开的（排查用）
_VIA_NOTE = {"jina": "（Jina）", "extract": "（抓正文工具）", "direct": "（直接打开）"}

FINAL_URL_LABEL = "最终地址"
# 到「）」/ 空白为止（标记是我们自己写的「（最终地址：<url>）」）
_FINAL_URL_RE = re.compile(FINAL_URL_LABEL + r"[:：]\s*([^\s）]+)")


def final_url_from_summary(text: str) -> str:
    """从 tool_calls 的 output 摘要里取「最终地址」；没有 / 解析不出 → ""。"""
    m = _FINAL_URL_RE.search(str(text or ""))
    return m.group(1) if m else ""


# 扩展抓正文工具的 output 约定：经常开头「Title: …\nURL: <最终地址>」——
# URL: 行也算这个任务真打开过（验收引用核对 / 资讯补打开都要认）。
_FINAL_URL_LINE_RE = re.compile(r"(?m)^\s*URL[:：]\s*(https?://\S+)", re.IGNORECASE)


def _is_extract_like_mcp(tool_name: str) -> bool:
    """工具名像「扩展的抓正文工具」：mcp_ 开头、匹配抓正文特征、且不像搜索。

    特征正则和 search_binding 的 _EXTRACT_HINT / _SEARCH_HINT 同一份（那边的用途是
    给前端排序；这里用来判定「这条调用算不算真打开过原文」）。
    """
    from .search_binding import _EXTRACT_HINT, _SEARCH_HINT  # noqa: SLF001 — 同一份特征定义，不复制

    name = str(tool_name or "")
    if not name.startswith("mcp_"):
        return False
    if _SEARCH_HINT.search(name):
        return False
    return bool(_EXTRACT_HINT.search(name))


def _urls_from_json_input(raw: str) -> list[str]:
    """tool_calls.input 是 JSON 时取其中的 url / urls（列表）/ link；不是 JSON → 原文当一条。"""
    text = str(raw or "").strip()
    if not text:
        return []
    if not text.startswith("{"):
        return [text]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return [text]
    if not isinstance(parsed, dict):
        return [text]
    out: list[str] = []
    url = str(parsed.get("url") or parsed.get("link") or "").strip()
    if url:
        out.append(url)
    urls = parsed.get("urls")
    if isinstance(urls, list):
        out.extend(str(u).strip() for u in urls if str(u or "").strip())
    return out


def opened_urls_from_rows(rows: Any) -> set[str]:
    """tool_calls 记录（dict 行，键 tool/input/output/ok）→ 真打开过的 URL 集合（规范化）。

    认两类成功（ok=1）的调用（web_search / mcp 搜索工具的结果只算「见过」，不算打开过）：
    - fetch_page：请求地址（input 是纯串或 JSON {"url": …}）+ output 里的「最终地址」标记都算；
    - 扩展的抓正文工具（mcp_ 开头、名字像抓正文、不像搜索，见 _is_extract_like_mcp）：
      input JSON 里的 url / urls / link + output 里「URL: <最终地址>」那一行都算。

    验收引用核对（coordinator._opened_urls）和资讯补打开（news_recheck.opened_links）共用它。
    """
    from .coordinator import normalize_link_for_check  # 延迟导入避免循环引用

    opened: set[str] = set()
    for row in rows or []:
        try:
            tool = str(row["tool"] or "")
            ok = int(row["ok"] or 0)
            raw_input = str(row["input"] or "")
            output = str(row["output"] or "")
        except (KeyError, TypeError, ValueError):
            continue
        if not ok:
            continue
        urls: list[str] = []
        if tool == "fetch_page":
            urls.extend(_urls_from_json_input(raw_input))
            final = final_url_from_summary(output)
            if final:
                urls.append(final)
        elif _is_extract_like_mcp(tool):
            urls.extend(_urls_from_json_input(raw_input))
            for m in _FINAL_URL_LINE_RE.finditer(output):
                urls.append(m.group(1))
        else:
            continue
        for one in urls:
            key = normalize_link_for_check(one)
            if key:
                opened.add(key)
    return opened


def final_url_note(url: str) -> str:
    """可解析的最终地址标记（fetch_page 的 tool_calls output 摘要 + 正文提示都用它）。"""
    return f"（{FINAL_URL_LABEL}：{url}）"


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------


_NEWS_SEARCH_MARKS = ("feeds-collect:", "feeds-discover:")  # 找资讯子 agent 的 task_id 前缀
_NEWS_DEFAULT_DAYS = 7


def register_builtin(
    tools: Tools,
    *,
    search: Any,                       # Search（search.py）
    profiles: Any,                     # Profiles（profile.py）
    http_transport: Any = None,        # 测试注入 httpx.MockTransport
    get_settings: Callable[[], Any] | None = None,
    resolver: Resolver | None = None,  # 测试注入 DNS resolver（host -> [ip]）
    reader: Any = None,                # JinaReader（reader.py）；None / 关着 = 不走 Jina
) -> None:
    """把 M2 内置工具注册进 tools。search 可以为 None（届时 web_search 直接报没配）。"""
    resolve = resolver or _default_resolver

    async def web_search(ctx: ToolContext, args: dict) -> ToolResult:
        if search is None:
            return ToolResult(ok=False, output="", error="这个 MaiWork 没配搜索服务，暂时不能联网搜索")
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="", error="query 不能为空")
        days = args.get("days")
        try:
            days_i = int(days) if days is not None else None
        except (TypeError, ValueError):
            days_i = None
        # 找资讯的子 agent 没填 days → 程序补「最近 7 天」（2026-09-30：时间条件写在提示词里会被打折，
        # 改由代码兜底；要找文章它会显式填 180）。其他任务不动。
        if days_i is None and str(ctx.task_id or "").startswith(_NEWS_SEARCH_MARKS):
            days_i = _NEWS_DEFAULT_DAYS
        try:
            limit_i = max(1, min(20, int(args.get("limit") or 8)))
        except (TypeError, ValueError):
            limit_i = 8
        site_s = str(args.get("site") or "").strip()
        news_b = bool(args.get("news") or False)
        try:
            # site/news 只在搜索家被预设（search_presets）认出时生效；别家忽略不报错
            results = await search.search(query, limit=limit_i, days=days_i, site=site_s, news=news_b)
        except Exception as e:  # SearchUnavailable / SearchError 等，message 已去密钥
            return ToolResult(ok=False, output="", error=f"搜索失败：{e}")
        # 撒网登记簿（两阶段找资讯）：这个 task_id 有开着的 run → 结果顺手记一份。
        # focus 参数只有撒网时用（关注点编号）；别的上下文传了也收下但不用。
        focus_i: int | None = None
        try:
            focus_i = int(args.get("focus")) if args.get("focus") is not None else None
        except (TypeError, ValueError):
            focus_i = None
        if str(ctx.task_id or "").startswith("feeds-discover:"):
            try:
                from . import discovery

                discovery.record(
                    str(ctx.task_id or ""), query=query, focus=focus_i,
                    provider="",  # 每条结果自带 provider（search.py 标的实际出结果那家）
                    results=results or [],
                )
            except Exception:
                logger.debug("撒网登记失败（%s）", ctx.task_id, exc_info=True)
        if not results:
            return ToolResult(ok=True, output="没搜到相关结果", data=[])
        lines: list[str] = []
        for i, item in enumerate(results, 1):
            date_text = ""
            published = item.get("published")
            if published:
                from . import clock  # 延迟导入避免循环

                date_text = f"（{clock.bj(float(published)).strftime('%Y-%m-%d')}）"
            lines.append(
                f"{i}. {item.get('title', '')}{date_text}\n   {item.get('url', '')}\n   {item.get('snippet', '')}"
            )
        return ToolResult(ok=True, output="\n".join(lines), data=results)

    def _page_result(url: str, text: str, image_url: str, final_url: str, via: str) -> ToolResult:
        host = urlparse(url).hostname or ""
        if image_url:
            # 拿到封面图就告诉子 agent 一声（交回候选时带 image_url）
            text += f"\n\n（这页有封面图：{image_url}）"
        if final_url and final_url != url:
            # 跳转后的最终地址也告诉子 agent：交付里常引用它，验收引用核对要认
            text += f"\n\n（这个链接跳转到了：{final_url}）"
        return ToolResult(
            ok=True, output=text,
            data={"url": url, "final_url": final_url, "host": host, "image_url": image_url, "via": via},
        )

    async def _try_extract(url: str) -> tuple[str, str]:
        """抓网页正文工具：(正文, 失败原因)。没配 / 用不了 → ("", 原因)。"""
        extract = getattr(search, "extract", None)
        if extract is None:
            return "", "没配"
        try:
            text = str(await extract(url) or "").strip()
        except Exception as e:  # SearchError 等，message 已去密钥
            logger.info("fetch_page 抓正文工具失败（%s）：%s", url, type(e).__name__)
            return "", str(e)[:120] or type(e).__name__
        return (text, "") if text else ("", "没读到正文（没选工具或那家用不了）")

    def _extracted_result(url: str, text: str) -> ToolResult:
        return _page_result(url, text[:20000] + "\n\n（这是经「抓网页正文」工具读到的正文）", "", url, "extract")

    async def fetch_page(ctx: ToolContext, args: dict) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(ok=False, output="", error="url 不能为空")
        # 核验子 agent（feeds-verify:）的代码硬上限：一组的打开页数按组内条数封死
        # （verify_budget.py，feeds._verify_batch 按组开账）。用完直接拒——提示词劝不住
        # 「找日期打转」（2026-09-30 线上实录 8 分钟 23 次），要靠代码拦。
        if str(ctx.task_id or "").startswith("feeds-verify:"):
            from . import verify_budget

            allowed, note = verify_budget.consume(str(ctx.task_id))
            if not allowed:
                return ToolResult(ok=False, output="", error=note)
        use_jina = reader is not None and bool(getattr(reader, "enabled", lambda: False)())
        if use_jina:
            # 新顺序：Jina → 抓正文工具 → 直接打开。先做地址安全检查（内网地址哪条路都不走）。
            problem = _url_problem(url, resolve)
            if problem:
                return ToolResult(ok=False, output="", error=problem)
            got = await reader.read(url)
            if got.ok:
                return _page_result(url, got.text, got.image_url, got.final_url or url, "jina")
            reasons = [f"Jina Reader：{got.reason}"]
            text, why = await _try_extract(url)
            if text:
                return _extracted_result(url, text)
            reasons.append(f"抓正文工具：{why}")
            ok, text, image_url, final_url = await _fetch_page_text(url, transport=http_transport, resolver=resolve)
            if ok:
                return _page_result(url, text, image_url, final_url, "direct")
            reasons.append(f"直接打开：{text}")
            return ToolResult(ok=False, output="", error="打不开这个页面。" + "；".join(reasons))

        # 老顺序（Jina 关着）：直接打开，被网站拦了（403 等）再用抓正文工具。
        # 内网 / 非法地址（安全拒绝）绝不走抓正文。线上实测约三分之一原文页 403。
        ok, text, image_url, final_url = await _fetch_page_text(url, transport=http_transport, resolver=resolve)
        if ok:
            return _page_result(url, text, image_url, final_url, "direct")
        blocked_for_safety = "内网" in text or "不允许" in text or "只支持" in text
        if not blocked_for_safety:
            extracted, _why = await _try_extract(url)
            if extracted:
                return _extracted_result(url, extracted)
        return ToolResult(ok=False, output="", error=text)

    async def read_profile(ctx: ToolContext, args: dict) -> ToolResult:
        gid = str(args.get("group_id") or ctx.group_id or "").strip()
        if gid != str(ctx.group_id):
            return ToolResult(ok=False, output="", error="只能读当前群的画像，读别群的不允许")
        entries = profiles.entries(gid) or []
        if not entries:
            return ToolResult(ok=True, output="本群画像还是空的", data=[])
        by_cat: dict[str, list[dict]] = {}
        for e in entries:
            by_cat.setdefault(str(e.get("category") or ""), []).append(e)
        lines: list[str] = []
        for cat in ("recent", "interest", "ongoing", "convention", "resource"):
            items = by_cat.get(cat)
            if not items:
                continue
            lines.append(f"【{CATEGORY_NAMES.get(cat, cat)}】")
            for e in items:
                lines.append(f"- {e.get('text', '')}")
        # 不在五类里的杂类也带上，防丢
        for cat, items in by_cat.items():
            if cat not in CATEGORY_NAMES:
                lines.append(f"【{cat}】")
                lines.extend(f"- {e.get('text', '')}" for e in items)
        return ToolResult(ok=True, output="\n".join(lines), data=entries)

    async def submit_result(ctx: ToolContext, args: dict) -> ToolResult:
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return ToolResult(ok=False, output="", error="summary 不能为空，给一句「干完了什么」")
        # 有的模型（线上实测 step-5-preview）把 data / evidence 写成 JSON 字符串交回：
        # 看着像 JSON 就解析成对象；解析不出来（多半被截断）→ 交回失败，让模型改成对象重交。
        data = args.get("data")
        if isinstance(data, str):
            parsed, bad = _maybe_json(data)
            if bad:
                return ToolResult(
                    ok=False, output="",
                    error="data 要直接给 JSON 对象，不要写成字符串；这次的字符串也解析不出来（可能太长被截断了），请精简后重新调用 submit_result",
                )
            data = parsed
        evidence = args.get("evidence")
        if isinstance(evidence, str):
            parsed_ev, bad_ev = _maybe_json(evidence)
            evidence = parsed_ev if not bad_ev else evidence
        if evidence is None:
            evidence_list: list[str] = []
        elif isinstance(evidence, list):
            evidence_list = [str(x) for x in evidence]
        else:
            evidence_list = [str(evidence)]
        return ToolResult(
            ok=True,
            output=f"已交回：{summary}",
            data={"summary": summary, "data": data, "evidence": evidence_list},
        )

    tools.register(
        Tool(
            name="web_search",
            description="联网搜索（新闻/网页）。只给出搜索结果列表，要看具体内容再调 fetch_page。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，具体一点"},
                    "days": {"type": "integer", "description": "只看最近多少天的（可选）"},
                    "limit": {"type": "integer", "description": "最多几条，默认 8，上限 20"},
                    "site": {"type": "string", "description": "只搜这个域名（可选，如 \"nintendo.com\"）"},
                    "news": {"type": "boolean", "description": "想要新闻类结果（可选，默认否）"},
                    "focus": {"type": "integer", "description": "撒网任务专用：这条搜索属于第几个关注点（别的任务不用填）"},
                },
                "required": ["query"],
            },
            roles=frozenset({"worker"}),
            handler=web_search,
            summarize=lambda args, res: (
                f"搜索：{args.get('query', '')}",
                (res.output.splitlines()[0] if res.ok and res.output else (res.error or "没搜到")),
            ),
            timeout_s=30.0,
        )
    )
    tools.register(
        Tool(
            name="fetch_page",
            description="打开一个 http/https 链接，取出正文（截 8000 字）。内网/本机地址打不开。有的页面会带发布时间。",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要打开的 http/https 链接"}},
                "required": ["url"],
            },
            roles=frozenset({"worker"}),
            handler=fetch_page,
            summarize=lambda args, res: (
                str(args.get("url", "")),
                (
                    ("取到正文 %d 字" % len(res.output))
                    + _VIA_NOTE.get(str((res.data or {}).get("via") or ""), "")
                    + (
                        final_url_note(str((res.data or {}).get("final_url") or ""))
                        if (res.data or {}).get("final_url")
                        else ""
                    )
                )
                if res.ok
                else (res.error or "打不开"),
            ),
            timeout_s=75.0,
        )
    )
    tools.register(
        Tool(
            name="read_profile",
            description="读当前群的画像（最近在聊 / 长期兴趣 / 在做的事 / 约定和说法 / 常用资源）。只能读本群。",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "群号（可选；只能是自己所在的群）"},
                },
            },
            roles=frozenset({"main", "worker"}),
            handler=read_profile,
            summarize=lambda args, res: (f"读画像（群 {args.get('group_id') or '本群'}）", res.output.splitlines()[0] if res.ok and res.output else (res.error or "空")),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="submit_result",
            description="干完活用这个交回：一段总结 + 结构化数据 + 证据链接。每次任务必须最后调它一次。",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "这次干完的一句话总结"},
                    "data": {"type": "object", "description": "结构化成果（可选）"},
                    "evidence": {"type": "array", "items": {"type": "string"}, "description": "证据链接列表（可选）"},
                },
                "required": ["summary"],
            },
            roles=frozenset({"worker"}),
            handler=submit_result,
            summarize=lambda args, res: ("交回成果", str(args.get("summary", ""))),
            timeout_s=10.0,
        )
    )
