"""联网搜索 MCP 预设（2026-10，docs/07 §10.3）：6 家测过/查过的搜索服务的定死知识。

解决的问题（都是实测出来的）：
- search.py 靠猜字段名做通用映射，各家其实不吃这一套：Keenable 的日期参数叫
  published_after（相对 "7d" 也行）会被通用映射漏掉；Tavily 的 time_range 没类型、
  start_date 要 YYYY-MM-DD；You 的 freshness 没 enum；Exa 默认工具没有日期过滤……
  预设把每家的参数名/取值定死，不再靠 schema 猜。
- 各家端点地址 / 密钥头 / 免费口径集中在这里，网页搜索设置页直接拿 PRESETS 渲染
  （logo 文件名只是字符串，图由前端自带）。

数据时效（2026-09-30 实测 tools/list + 各家官方文档）：
- keenable：免密钥可用（公共额度）；填密钥走同一个 url，头 X-API-Key。
- tavily：免密钥要带 X-Tavily-Access-Mode: keyless（每 IP 每月有上限，见 fixture
  tavily_search_keyless_cap_error.json）；密钥走 Authorization: Bearer。
  实测 MCP 端 topic 目前只收 "general"（2026-09-30），news 发了会被拒——写死 general。
- exa：免密钥但要 ?tools= 参数把高级工具暴露出来；密钥头 x-api-key。
- you：免密钥 ?profile=free 只有 you-search / you-discover（没有抓正文）；
  密钥版才有 you-contents（它的 inputSchema 没密钥看不到 → 抓正文参数走通用映射）。
- firecrawl：免密钥 /mcp，密钥 /v2/mcp + Bearer。搜索结果分 data.web / data.news 两组。
- tinyfish：**没有免密钥**（401），搜索和抓正文免费但要注册拿密钥。
  ⚠️ tinyfish 没实测过（没有密钥）：参数名按官方文档 https://docs.tinyfish.ai/ 写的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("maiwork.search_presets")

_EXTRACT_MAX_CHARS = 20000


@dataclass(frozen=True)
class Preset:
    """一家搜索服务的定死知识。logo / skill 只是名字字符串（文件由前端/技能目录自带）。"""

    id: str                       # "keenable" 等；也当默认扩展名
    label: str                    # 显示名 "Keenable"
    free: bool                    # 免密钥能不能用
    free_note: str                # 免费口径一句话（设置页展示用）
    url_free: str                 # 免密钥端点
    url_key: str                  # 填密钥端点（多数和 url_free 相同）
    free_headers: dict[str, str]  # 免密钥也要带的头（如 tavily 的 keyless 头）
    key_header: str               # 密钥放哪个 HTTP 头
    key_prefix: str               # "Bearer " 或 ""
    search_tool: str              # 搜索工具名
    extract_tool: str             # 抓正文工具名（免密钥版）；"" = 没有
    extract_tool_key: str         # 抓正文工具名（密钥版，可能与免密钥不同，如 you → you-contents）
    key_page_url: str             # 拿密钥的页面
    docs_url: str                 # 文档
    hosts: tuple[str, ...]        # 认 url 用的主机名
    logo: str                     # f"{id}.svg"（文件名，前端供图）
    skill: str                    # f"search-{id}"


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            id="keenable",
            label="Keenable",
            free=True,
            free_note="免密钥可用（公共额度，每小时 1000 次）；填密钥每月免费 10 万次",
            url_free="https://api.keenable.ai/mcp",
            url_key="https://api.keenable.ai/mcp",
            free_headers={},
            key_header="X-API-Key",
            key_prefix="",
            search_tool="search_web_pages",
            extract_tool="fetch_page_content",
            extract_tool_key="fetch_page_content",
            key_page_url="https://app.keenable.ai/console",
            docs_url="https://docs.keenable.ai/",
            hosts=("api.keenable.ai",),
            logo="keenable.svg",
            skill="search-keenable",
        ),
        Preset(
            id="tavily",
            label="Tavily",
            free=True,
            free_note="免密钥可用（每 IP 每月有免费额度，用完当月就得填密钥）",
            url_free="https://mcp.tavily.com/mcp/",
            url_key="https://mcp.tavily.com/mcp/",
            free_headers={"X-Tavily-Access-Mode": "keyless"},
            key_header="Authorization",
            key_prefix="Bearer ",
            search_tool="tavily_search",
            extract_tool="tavily_extract",
            extract_tool_key="tavily_extract",
            key_page_url="https://app.tavily.com",
            docs_url="https://docs.tavily.com/",
            hosts=("mcp.tavily.com",),
            logo="tavily.svg",
            skill="search-tavily",
        ),
        Preset(
            id="exa",
            label="Exa",
            free=True,
            free_note="免密钥可用（公共额度）；填密钥更稳",
            # ?tools= 必须带：不带就只暴露默认搜索，没有高级搜索和抓网页（2026-09-30 实测）
            url_free="https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa,web_search_advanced_exa",
            url_key="https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa,web_search_advanced_exa",
            free_headers={},
            key_header="x-api-key",
            key_prefix="",
            search_tool="web_search_advanced_exa",
            extract_tool="web_fetch_exa",
            extract_tool_key="web_fetch_exa",
            key_page_url="https://dashboard.exa.ai/api-keys",
            docs_url="https://exa.ai/docs",
            hosts=("mcp.exa.ai",),
            logo="exa.svg",
            skill="search-exa",
        ),
        Preset(
            id="you",
            label="You.com",
            free=True,
            free_note="免密钥可用（只能搜索，没有抓正文）；填密钥才有抓正文",
            url_free="https://api.you.com/mcp?profile=free",
            url_key="https://api.you.com/mcp",
            free_headers={},
            key_header="Authorization",
            key_prefix="Bearer ",
            search_tool="you-search",
            extract_tool="",               # 免密钥版没有抓正文工具
            extract_tool_key="you-contents",  # 密钥版才有（schema 待有密钥时核实）
            key_page_url="https://you.com/platform",
            docs_url="https://you.com/docs",
            hosts=("api.you.com",),
            logo="you.svg",
            skill="search-you",
        ),
        Preset(
            id="firecrawl",
            label="Firecrawl",
            free=True,
            free_note="免密钥可用（公共额度）；抓正文也能用",
            url_free="https://mcp.firecrawl.dev/mcp",
            url_key="https://mcp.firecrawl.dev/v2/mcp",
            free_headers={},
            key_header="Authorization",
            key_prefix="Bearer ",
            search_tool="firecrawl_search",
            extract_tool="firecrawl_scrape",
            extract_tool_key="firecrawl_scrape",
            key_page_url="https://www.firecrawl.dev/app/api-keys",
            docs_url="https://docs.firecrawl.dev/",
            hosts=("mcp.firecrawl.dev",),
            logo="firecrawl.svg",
            skill="search-firecrawl",
        ),
        Preset(
            id="tinyfish",
            label="TinyFish",
            free=False,
            free_note="要注册拿密钥（搜索和抓正文免费）",
            url_free="",                    # 没有免密钥（2026-09-30 实测 401）
            url_key="https://agent.tinyfish.ai/mcp",
            free_headers={},
            key_header="Authorization",
            key_prefix="Bearer ",
            search_tool="search",
            extract_tool="fetch_content",
            extract_tool_key="fetch_content",
            key_page_url="https://agent.tinyfish.ai/api-keys",
            docs_url="https://docs.tinyfish.ai/",
            hosts=("agent.tinyfish.ai",),
            logo="tinyfish.svg",
            skill="search-tinyfish",
        ),
    )
}


# ----------------------------------------------------------------------
# 认 url / 生成扩展条目
# ----------------------------------------------------------------------


def preset_of_url(url: str) -> Preset | None:
    """按主机名认：这个 MCP 端点是预设里的哪一家；认不出 → None。"""
    try:
        host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for p in PRESETS.values():
        if host in p.hosts:
            return p
    return None


def entry_for(preset_id: str, key: str | None = None) -> dict[str, Any]:
    """生成能直接喂 extensions_web.create 的请求体
    {"name","url","headers","roles","enabled"}；名字默认用预设 id。

    非免费的预设没给密钥 → ValueError（中文，指去拿密钥的页面）。
    """
    preset = PRESETS.get(str(preset_id or ""))
    if preset is None:
        raise ValueError(f"没有这个搜索预设「{preset_id}」")
    key_s = str(key or "").strip()
    if key_s:
        url = preset.url_key
        headers = {preset.key_header: f"{preset.key_prefix}{key_s}"}
    else:
        if not preset.free:
            raise ValueError(
                f"{preset.label} 没有免密钥版：先去 {preset.key_page_url} 注册拿一个密钥再填"
            )
        url = preset.url_free
        headers = dict(preset.free_headers)
    return {
        "name": preset.id,
        "url": url,
        "headers": headers,
        "roles": ["worker"],
        "enabled": True,
    }


# ----------------------------------------------------------------------
# 参数映射（每家定死）
# ----------------------------------------------------------------------


def _since_iso(days: int) -> str:
    """days 天前的 ISO（Exa 风格）：2026-09-23T00:00:00.000Z。"""
    since = datetime.now(timezone.utc) - timedelta(days=int(days))
    return since.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _since_date(days: int) -> str:
    """days 天前的 YYYY-MM-DD（TinyFish after_date）。"""
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%d")


def _time_range_of(days: int) -> str:
    """天数 → day|week|month|year（Tavily time_range / You freshness 共用口径）。"""
    d = int(days)
    if d <= 1:
        return "day"
    if d <= 7:
        return "week"
    if d <= 31:
        return "month"
    return "year"


def _firecrawl_tbs(days: int) -> str:
    """天数 → Google tbs 语法 qdr:d/w/m/y。这里 ≤3 天用 d：只露 1 天会漏掉大批结果，
    d 的粒度是「近一天/近几天」，比 w 更贴「最近几天」的语义。"""
    d = int(days)
    if d <= 3:
        return "qdr:d"
    if d <= 7:
        return "qdr:w"
    if d <= 31:
        return "qdr:m"
    return "qdr:y"


def search_args(
    preset_id: str,
    query: str,
    *,
    limit: int,
    days: int | None = None,
    site: str = "",
    news: bool = False,
) -> dict[str, Any]:
    """每家的搜索参数定死映射；空参数不发。query 为空在调用方先挡。"""
    pid = str(preset_id or "")
    if pid not in PRESETS:
        raise ValueError(f"没有这个搜索预设「{preset_id}」")
    q = str(query or "").strip()
    limit_i = max(1, int(limit))
    days_i = int(days) if days else 0
    site_s = str(site or "").strip()

    if pid == "keenable":
        args: dict[str, Any] = {"query": q, "max_results": min(limit_i, 50)}
        if days_i:
            args["published_after"] = f"{days_i}d"  # 也吃 YYYY-MM-DD；相对话更贴合「最近 N 天」
        if site_s:
            args["site"] = site_s
        # 官方建议问句式搜索（自然语言描述理想页面），mode 用精度更高的 pro
        args["mode"] = "pro"
        return args

    if pid == "tavily":
        args = {"query": q, "max_results": limit_i}
        if days_i:
            # 选 time_range 不选 start_date：time_range 是「最近 N 天」滚动窗口，
            # 和 days 语义一致；start_date 是固定日期，过了那天会越来越宽。
            args["time_range"] = _time_range_of(days_i)
        if site_s:
            args["include_domains"] = [site_s]
        # 2026-09-30 实测 MCP 端 topic 只收 "general"（news 会被拒），写死
        args["topic"] = "general"
        return args

    if pid == "exa":
        args = {"query": q, "numResults": limit_i}
        if days_i:
            args["startPublishedDate"] = _since_iso(days_i)
        if site_s:
            args["includeDomains"] = [site_s]
        if news:
            args["category"] = "news"
        return args

    if pid == "you":
        # 没有 include-domain 参数：向 query 拼 " site:<域名>" 限制（官方文档路数，**待实测**）
        args = {"query": q + (f" site:{site_s}" if site_s else ""), "count": limit_i}
        if days_i:
            args["freshness"] = _time_range_of(days_i)
        return args

    if pid == "firecrawl":
        args = {"query": q, "limit": limit_i}  # limit 按源类型分别算（web N 条 + news N 条）
        args["sources"] = ["web", "news"] if news else ["web", "news"]
        if days_i:
            args["tbs"] = _firecrawl_tbs(days_i)
        if site_s:
            args["includeDomains"] = [site_s]
        return args

    if pid == "tinyfish":
        # ⚠️ 未实测（没密钥）：按官方文档写的
        args = {"query": q}
        if days_i:
            args["after_date"] = _since_date(days_i)
        if site_s:
            args["include_domains"] = site_s  # 逗号分隔字符串，不是数组
        if news:
            args["domain_type"] = "news"
        return args

    raise ValueError(f"没有这个搜索预设「{preset_id}」")  # pragma: no cover


def extract_args(preset_id: str, url: str, *, keyed: bool) -> dict[str, Any] | None:
    """抓正文参数。返回 None 表示「别用预设，走通用 map_extract_arguments」
    （you 的 you-contents schema 没密钥核实不了）。url 空直接给空参数（调用方挡）。"""
    pid = str(preset_id or "")
    u = str(url or "")
    if pid == "keenable":
        # live=True：不然只回它索引里有的页面
        return {"url": u, "live": True, "max_chars": _EXTRACT_MAX_CHARS}
    if pid == "tavily":
        return {"urls": [u], "format": "markdown"}
    if pid == "exa":
        return {"urls": [u], "maxCharacters": _EXTRACT_MAX_CHARS}
    if pid == "firecrawl":
        return {"url": u, "formats": ["markdown"], "onlyMainContent": True}
    if pid == "tinyfish":
        return {"urls": [u], "format": "markdown"}  # 未实测（没密钥）
    if pid == "you":
        # 免密钥版没有抓正文工具；密钥版 you-contents 的 schema 待核实 → 通用映射
        return None
    raise ValueError(f"没有这个搜索预设「{preset_id}」")


def filter_to_schema(args: dict[str, Any], schema: Any) -> dict[str, Any]:
    """防各家 schema 漂移：运行时的 inputSchema 有 properties 就只留它声明过的键。
    滤掉了什么记 debug 日志（值不进日志，防泄）。"""
    if not isinstance(schema, dict):
        return dict(args)
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return dict(args)
    out = {k: v for k, v in args.items() if k in props}
    dropped = sorted(set(args) - set(out))
    if dropped:
        logger.debug("搜索参数被运行时 schema 滤掉：%s", ",".join(dropped))
    return out
