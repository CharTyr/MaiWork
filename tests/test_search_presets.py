"""search_presets.py 预设 + search.py 预设适配的测试（2026-10）。

- 预设参数映射：6 家（keenable/tavily/exa/you/firecrawl/tinyfish）的 search_args / extract_args /
  entry_for / preset_of_url / filter_to_schema —— 纯函数，直接断字段。
- 结果解析用 tests/fixtures/search_presets/ 下 2026-09-30 实测抓的真回包（长字符串在 JSON 内部截短到 3000 字，JSON 本身完整）。
- Search 用一个假 extensions（预设 URL + 假 client）测：预设参数、isError 报错、
  fallback 顺序、search_with / broad_providers / binding 校验、web_search 工具透传 site/news。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.mcp_client import MCPError
from CharTyr_MaiWork.maiwork.search import Search, SearchError, SearchUnavailable, _SNIPPET_MAX
from CharTyr_MaiWork.maiwork import search as search_mod
from CharTyr_MaiWork.maiwork import search_presets
from CharTyr_MaiWork.maiwork.search_presets import (
    PRESETS,
    entry_for,
    extract_args,
    filter_to_schema,
    preset_of_url,
    search_args,
)
from CharTyr_MaiWork.maiwork.search_binding import get_binding, save_binding, set_binding, clear_binding
from CharTyr_MaiWork.maiwork.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "search_presets"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# search_args：6 家的参数映射
# ----------------------------------------------------------------------


class TestSearchArgs:
    def test_keenable_basic(self) -> None:
        args = search_args("keenable", "任天堂新机", limit=8)
        assert args == {
            "query": "任天堂新机",
            "max_results": 8,
            "mode": "pro",
        }

    def test_keenable_days_site_news(self) -> None:
        args = search_args("keenable", "更新", limit=99, days=7, site="nintendo.com", news=True)
        assert args["published_after"] == "7d"
        assert args["site"] == "nintendo.com"
        assert args["max_results"] == 50  # 上限 50
        assert args["mode"] == "pro"

    def test_tavily_basic(self) -> None:
        args = search_args("tavily", "MaiBot", limit=5)
        assert args == {"query": "MaiBot", "max_results": 5, "topic": "general"}

    def test_tavily_days_ladder(self) -> None:
        assert search_args("tavily", "q", limit=1, days=1)["time_range"] == "day"
        assert search_args("tavily", "q", limit=1, days=7)["time_range"] == "week"
        assert search_args("tavily", "q", limit=1, days=31)["time_range"] == "month"
        assert search_args("tavily", "q", limit=1, days=90)["time_range"] == "year"

    def test_tavily_site(self) -> None:
        args = search_args("tavily", "q", limit=3, site="example.com")
        assert args["include_domains"] == ["example.com"]
        # 实测 MCP 端目前只认 general：news=True 时也不能改成 news
        args2 = search_args("tavily", "q", limit=3, news=True)
        assert args2["topic"] == "general"

    def test_exa_days_news(self) -> None:
        args = search_args("exa", "Switch 2", limit=4, days=14, news=True)
        assert args["query"] == "Switch 2"
        assert args["numResults"] == 4
        assert args["category"] == "news"
        iso = args["startPublishedDate"]
        assert isinstance(iso, str) and iso.endswith("Z") and "T" in iso

    def test_exa_omit_empty(self) -> None:
        args = search_args("exa", "q", limit=2)
        assert "startPublishedDate" not in args
        assert "category" not in args
        assert "includeDomains" not in args

    def test_exa_site(self) -> None:
        args = search_args("exa", "q", limit=2, site="cnet.com")
        assert args["includeDomains"] == ["cnet.com"]

    def test_you_days_site_free(self) -> None:
        args = search_args("you", "MaiBot", limit=6, days=2, site="you.com")
        assert args["count"] == 6
        assert args["freshness"] == "week"
        # 没有 include-domain 参数：实测待验证的旁路是往 query 拼 site:
        assert args["query"] == "MaiBot site:you.com"

    def test_you_plain(self) -> None:
        assert search_args("you", "q", limit=8) == {"query": "q", "count": 8}

    def test_firecrawl_news(self) -> None:
        args = search_args("firecrawl", "q", limit=9, days=3, news=True, site="x.com")
        assert args["limit"] == 9
        # 不按 sources 过滤：web + news 两个源一起要，让日期过滤靠 tbs
        assert args["sources"] == ["web", "news"]
        assert args["tbs"] == "qdr:d"
        assert args["includeDomains"] == ["x.com"]

    def test_firecrawl_tbs_ladder(self) -> None:
        assert search_args("firecrawl", "q", limit=1, days=1)["tbs"] == "qdr:d"
        assert search_args("firecrawl", "q", limit=1, days=7)["tbs"] == "qdr:w"
        assert search_args("firecrawl", "q", limit=1, days=30)["tbs"] == "qdr:m"
        assert search_args("firecrawl", "q", limit=1, days=200)["tbs"] == "qdr:y"

    def test_tinyfish_days_news(self) -> None:
        args = search_args("tinyfish", "q", limit=5, days=7, news=True, site="a.com,b.com")
        assert args["query"] == "q"
        # after_date 是 YYYY-MM-DD（今天减 7 天）
        from datetime import datetime, timedelta, timezone

        expect = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        assert args["after_date"] == expect
        assert args["domain_type"] == "news"
        assert args["include_domains"] == "a.com,b.com"

    def test_unknown_preset_raises(self) -> None:
        with pytest.raises(ValueError):
            search_args("没这家", "q", limit=1)


# ----------------------------------------------------------------------
# extract_args / entry_for / preset_of_url / filter_to_schema
# ----------------------------------------------------------------------


class TestExtractArgs:
    def test_keenable_extract(self) -> None:
        assert extract_args("keenable", "https://x.com/a", keyed=False) == {
            "url": "https://x.com/a", "live": True, "max_chars": 20000,
        }

    def test_tavily_exa_firecrawl_tinyfish(self) -> None:
        assert extract_args("tavily", "https://u", keyed=False) == {
            "urls": ["https://u"], "format": "markdown"}
        assert extract_args("exa", "https://u", keyed=False) == {
            "urls": ["https://u"], "maxCharacters": 20000}
        assert extract_args("firecrawl", "https://u", keyed=False) == {
            "url": "https://u", "formats": ["markdown"], "onlyMainContent": True}
        assert extract_args("tinyfish", "https://u", keyed=True) == {
            "urls": ["https://u"], "format": "markdown"}

    def test_you_keyed_falls_back_generic(self) -> None:
        # you-contents 的 schema 没有密钥没法核实 → None 表示走通用 map_extract_arguments
        assert extract_args("you", "https://u", keyed=True) is None
        # 免密钥版没有抓正文工具
        assert extract_args("you", "https://u", keyed=False) is None
        assert PRESETS["you"].extract_tool == ""
        assert PRESETS["you"].extract_tool_key == "you-contents"


class TestEntryFor:
    def test_free_entry(self) -> None:
        e = entry_for("keenable")
        assert e["name"] == "keenable"
        assert e["url"] == "https://api.keenable.ai/mcp"
        assert e["headers"] == {}
        assert e["roles"] == ["worker"]
        assert e["enabled"] is True

    def test_key_entry(self) -> None:
        e = entry_for("tavily", key="tv-testkey123")
        assert e["url"] == "https://mcp.tavily.com/mcp/"
        assert e["headers"] == {"Authorization": "Bearer tv-testkey123"}

    def test_key_entry_no_prefix(self) -> None:
        e = entry_for("keenable", key="k-9")
        assert e["headers"] == {"X-API-Key": "k-9"}  # keenable 不带 Bearer
        e2 = entry_for("exa", key="e-1")
        assert e2["headers"] == {"x-api-key": "e-1"}
        e3 = entry_for("you", key="y-1")
        assert e3["headers"] == {"Authorization": "Bearer y-1"}

    def test_tavily_free_has_keyless_header(self) -> None:
        e = entry_for("tavily")
        assert e["headers"] == {"X-Tavily-Access-Mode": "keyless"}

    def test_nonfree_without_key_raises_chinese(self) -> None:
        with pytest.raises(ValueError) as exc:
            entry_for("tinyfish")
        assert "https://agent.tinyfish.ai/api-keys" in str(exc.value)

    def test_preset_meta(self) -> None:
        assert list(PRESETS) == ["keenable", "tavily", "exa", "you", "firecrawl", "tinyfish"]
        assert PRESETS["you"].logo == "you.svg"
        assert PRESETS["tinyfish"].free is False
        assert PRESETS["firecrawl"].skill == "search-firecrawl"
        for p in PRESETS.values():
            assert p.label and p.free_note and p.key_page_url and p.docs_url

    def test_preset_of_url(self) -> None:
        assert preset_of_url("https://api.keenable.ai/mcp") is PRESETS["keenable"]
        assert preset_of_url("https://mcp.tavily.com/mcp/") is PRESETS["tavily"]
        assert preset_of_url("https://mcp.exa.ai/mcp?tools=web_search_exa") is PRESETS["exa"]
        assert preset_of_url("https://api.you.com/mcp?profile=free") is PRESETS["you"]
        assert preset_of_url("https://mcp.firecrawl.dev/v2/mcp") is PRESETS["firecrawl"]
        assert preset_of_url("https://agent.tinyfish.ai/mcp") is PRESETS["tinyfish"]
        assert preset_of_url("https://unknown.example/mcp") is None
        assert preset_of_url("not a url") is None


class TestFilterToSchema:
    def test_drops_undeclared_keys(self) -> None:
        schema = {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}}
        out = filter_to_schema({"query": "q", "max_results": 5, "mode": "pro"}, schema)
        assert out == {"query": "q", "max_results": 5}

    def test_no_properties_keeps_everything(self) -> None:
        assert filter_to_schema({"a": 1}, {"type": "object"}) == {"a": 1}
        assert filter_to_schema({"a": 1}, {}) == {"a": 1}

    def test_keeps_none_values(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        assert filter_to_schema({"x": None}, schema) == {"x": None}


# ----------------------------------------------------------------------
# 假扩展 / 假 client（沿用 test_search.py 的形状）
# ----------------------------------------------------------------------


class FakeClient:
    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._queue = list(results or [])
        self._error = error

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self._error is not None:
            raise self._error
        if not self._queue:
            raise AssertionError("没有预设回包了")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeExt:
    """和 test_search.py 的 FakeExt 同构；带真实 url 好让 preset_of_url 认出来。"""

    def __init__(self, name: str, url: str, *, enabled: bool = True,
                 headers: dict | None = None, tools: dict | None = None, client=None) -> None:
        self.name = name
        self.url = url
        self.enabled = enabled
        self.headers = dict(headers or {})
        self._tools = tools or {}
        self.client = client

    def tools_remote(self) -> dict:
        return self._tools


class FakeExtensions:
    def __init__(self, exts: dict[str, FakeExt]) -> None:
        self._exts = exts
        raw = {"extensions": {"mcp": [
            {"name": e.name, "url": e.url, "enabled": e.enabled, "headers": e.headers} for e in exts.values()
        ]}}
        self._settings, _ = load_settings(raw)
        self._get_settings = lambda: self._settings

    def runtime_of(self, name: str):
        return self._exts.get(name)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _search(store: Store, exts: dict[str, FakeExt]) -> Search:
    return Search(store, lambda: FakeExtensions(exts))


def _text_result(payload) -> dict:
    return {"content": [{"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)}]}


KEENABLE_URL = "https://api.keenable.ai/mcp"


def _keenable_tools() -> dict:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "site": {"type": "string"},
            "published_after": {"type": "string"},
            "max_results": {"type": "integer"},
            "mode": {"type": "string", "enum": ["realtime", "pro"]},
            "session_id": {"type": "string"},  # 我们不该发它
        },
        "required": ["query"],
    }
    return {"search_web_pages": {"name": "search_web_pages", "inputSchema": schema}}


# ----------------------------------------------------------------------
# normalize_search_results 对实测 fixture
# ----------------------------------------------------------------------


class TestNormalizeFixtures:
    def test_keenable_text_blocks(self) -> None:
        out = search_mod.normalize_search_results(_fixture("keenable_search.json"))
        # 实测回包是全文本块：必须拆出多条（>=3），每条有 url 和发布时间
        assert len(out) >= 3
        urls = [r["url"] for r in out]
        assert all(u.startswith("http") for u in urls)
        titles = [r["title"] for r in out]
        assert any("Welcome Tour" in t for t in titles)
        pub = [r["published"] for r in out]
        assert all(isinstance(p, (int, float)) for p in pub), pub
        # 摘要从 Snippets: 里来
        assert any("VRR" in r["snippet"] or "firmware" in r["snippet"] for r in out)

    def test_exa_json_results(self) -> None:
        # 文本是 JSON：带出 url + title + publishedDate
        out = search_mod.normalize_search_results(_fixture("exa_search.json"))
        assert len(out) >= 3, "exa fixture 应拆出多条结果"
        first = out[0]
        assert first["url"].startswith("https://www.nintendolife.com/")
        assert isinstance(first["published"], (int, float))

    def test_you_structured_web(self) -> None:
        out = search_mod.normalize_search_results(_fixture("you_search.json"))
        assert out and out[0]["url"] == "https://en.wikipedia.org/wiki/Nintendo_Switch_2"
        assert out[0]["title"]
        assert isinstance(out[0]["published"], (int, float))  # page_age

    def test_firecrawl_web_and_news(self) -> None:
        # data.web + data.news 都要收集；news 的 "3 days ago" 解不了 → published None
        out = search_mod.normalize_search_results(_fixture("firecrawl_search.json"))
        urls = [r["url"] for r in out]
        assert any("youtube.com" in u for u in urls)  # web 第一条
        assert any("cnet.com" in u for u in urls)  # news 第一条
        news_items = [r for r in out if "cnet" in r["url"]]
        assert news_items and news_items[0]["published"] is None

    def test_iserror_raises_searcherror_tavily_message(self) -> None:
        with pytest.raises(SearchError) as exc:
            search_mod.normalize_search_results(_fixture("tavily_search_keyless_cap_error.json"))
        assert "Tavily" in str(exc.value)
        assert "app.tavily.com" in str(exc.value)

    def test_iserror_generic_message(self) -> None:
        bad = {"isError": True, "content": [{"type": "text", "text": "boom 出错了"}]}
        with pytest.raises(SearchError) as exc:
            search_mod.normalize_search_results(bad)
        assert not str(exc.value).startswith("搜索失败")  # isError 直接报原文
        assert "boom" in str(exc.value)


class TestNormalizeExtractFixtures:
    def test_keenable_extract_keeps_title(self) -> None:
        text = search_mod.normalize_extract_text(_fixture("keenable_extract.json"))
        assert text.startswith("《Nintendo Switch 2》\n")
        assert "video game console" in text

    def test_exa_extract_keeps_title(self) -> None:
        text = search_mod.normalize_extract_text(_fixture("exa_extract.json"))
        assert text.startswith("《Nintendo Switch 2》\n")

    def test_firecrawl_extract_body(self) -> None:
        text = search_mod.normalize_extract_text(_fixture("firecrawl_extract.json"))
        # 文本是 JSON：应捞出 markdown
        assert "Jump to content" in text

    def test_tavily_extract_body_and_title(self) -> None:
        text = search_mod.normalize_extract_text(_fixture("tavily_extract.json"))
        assert "Nintendo Switch 2 - Wikipedia" in text
        assert "Jump to content" in text

    def test_extract_iserror_raises(self) -> None:
        bad = {"isError": True, "content": [{"type": "text", "text": "抽取失败的原因"}]}
        with pytest.raises(SearchError):
            search_mod.normalize_extract_text(bad)


class TestPublishedOf:
    def test_variants(self) -> None:
        fn = search_mod._published_of
        assert fn({"published_at": "2026-09-24"}) is not None
        assert fn({"publishedDate": "2026-09-24T01:30:00.000Z"}) is not None
        assert fn({"page_age": "2026-09-24T02:58:12"}) is not None
        assert fn({"date": "2026-09-24"}) is not None
        assert fn({"published": 1790121600}) == 1790121600.0
        # "3 days ago" 之类的相对话解析不了 → None（不许编时间）
        assert fn({"date": "3 days ago"}) is None
        assert fn({}) is None


class TestSnippetMax:
    def test_snippet_max_is_800(self) -> None:
        assert _SNIPPET_MAX == 800

    def test_keenable_snippets_truncated_to_800(self) -> None:
        out = search_mod.normalize_search_results(_fixture("keenable_search.json"))
        assert out
        assert all(len(r["snippet"]) <= 800 for r in out)
        # 长摘要该超过老的 500 ——证明真的抬了上限
        assert any(len(r["snippet"]) > 500 for r in out)


# ----------------------------------------------------------------------
# Search 走预设参数（keenable 真 url）vs 未知 url 走通用
# ----------------------------------------------------------------------


class TestSearchPresetPath:
    def _bind(self, store: Store, client, tools: dict | None = None) -> dict[str, FakeExt]:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": ""})
        return {"keenable": FakeExt("keenable", KEENABLE_URL,
                                    tools=tools if tools is not None else _keenable_tools(),
                                    client=client)}

    @pytest.mark.asyncio
    async def test_keenable_uses_preset_args(self, store: Store) -> None:
        client = FakeClient([_fixture("keenable_search.json")])
        exts = self._bind(store, client)
        s = _search(store, exts)
        out = await s.search("任天堂新机", limit=3, days=7, site="nintendo.com", news=True)
        name, args = client.calls[0]
        assert name == "search_web_pages"
        # 预设参数（不是通用映射）：mode pro、published_after "7d"、site
        assert args["mode"] == "pro"
        assert args["published_after"] == "7d"
        assert args["site"] == "nintendo.com"
        assert args["max_results"] == 3
        assert "query" in args
        assert "session_id" not in args
        assert len(out) == 3
        assert all(r["url"].startswith("http") for r in out)

    @pytest.mark.asyncio
    async def test_unknown_url_uses_generic_path(self, store: Store) -> None:
        client = FakeClient([_text_result({"results": [{"url": "https://e.com", "content": "好"}]})])
        set_binding(store, {"mcp": "odd", "tool": "odd-search", "extract_tool": ""})
        schema = {"type": "object", "properties": {"q": {"type": "string"}, "top_k": {"type": "integer"}},
                  "required": ["q"]}
        exts = {"odd": FakeExt("odd", "https://odd.example/mcp",
                               tools={"odd-search": {"name": "odd-search", "inputSchema": schema}},
                               client=client)}
        s = _search(store, exts)
        await s.search("问题", limit=9, site="x.com", news=True)  # site/news 在通用路上被忽略
        _, args = client.calls[0]
        assert args == {"q": "问题", "top_k": 9}

    @pytest.mark.asyncio
    async def test_filter_to_schema_drops_preset_extras(self, store: Store) -> None:
        client = FakeClient([_fixture("keenable_search.json")])
        tools = _keenable_tools()
        # 把 schema 里的 mode 抠掉 → filter_to_schema 应把它滤掉不发
        tools["search_web_pages"]["inputSchema"]["properties"].pop("mode")
        exts = self._bind(store, client, tools)
        s = _search(store, exts)
        await s.search("q", limit=2)
        _, args = client.calls[0]
        assert "mode" not in args
        assert "session_id" not in args

    @pytest.mark.asyncio
    async def test_iserror_from_call_raises_searcherror_with_friendly_text(self, store: Store) -> None:
        client = FakeClient([_fixture("tavily_search_keyless_cap_error.json")])
        exts = self._bind(store, client)
        s = _search(store, exts)
        with pytest.raises(SearchError) as exc:
            await s.search("x")
        assert "Tavily" in str(exc.value)

    @pytest.mark.asyncio
    async def test_results_carry_provider(self, store: Store) -> None:
        client = FakeClient([_fixture("keenable_search.json")])
        exts = self._bind(store, client)
        s = _search(store, exts)
        out = await s.search("q", limit=2)
        assert out and all(r["provider"] == "keenable" for r in out)


# ----------------------------------------------------------------------
# fallback / broad / search_with
# ----------------------------------------------------------------------


class TestFallback:
    def _setup(self, store: Store, main_results, backup_results) -> tuple[FakeClient, FakeClient, Search]:
        """主用 keenable，fallback=exa-x（名字故意不叫 exa：预设识别的是 url 不是名字）。"""
        keenable_client = FakeClient(main_results)
        exa_client = FakeClient(backup_results)
        set_binding(store, {
            "mcp": "keenable", "tool": "search_web_pages", "extract_tool": "",
            "fallback": ["exa-x"], "broad": [],
        })
        exts = {
            "keenable": FakeExt("keenable", KEENABLE_URL, tools=_keenable_tools(), client=keenable_client),
            "exa-x": FakeExt("exa-x", "https://mcp.exa.ai/mcp",
                             tools={"web_search_advanced_exa": {"name": "web_search_advanced_exa", "inputSchema": {}}},
                             client=exa_client),
        }
        return keenable_client, exa_client, _search(store, exts)

    @pytest.mark.asyncio
    async def test_fallback_on_iserror(self, store: Store) -> None:
        _, exa_client, s = self._setup(
            store,
            [_fixture("tavily_search_keyless_cap_error.json")],  # 主家回 isError
            [_fixture("you_search.json")],
        )
        out = await s.search("Switch", limit=2)
        assert exa_client.calls, "fallback 那家该被调到"
        name, args = exa_client.calls[0]
        assert name == "web_search_advanced_exa"  # 预设工具名
        assert args["query"] == "Switch"
        assert out and out[0]["url"].startswith("http")
        assert out[0]["provider"] == "exa-x"

    @pytest.mark.asyncio
    async def test_all_fail_raises_last_error(self, store: Store) -> None:
        _, _, s = self._setup(store, [MCPError("主家挂了")], [MCPError("备胎也挂了")])
        with pytest.raises(SearchError) as exc:
            await s.search("q")
        assert "exa-x" in str(exc.value)  # 抛的是最后（备胎）的错误

    @pytest.mark.asyncio
    async def test_fallback_skips_unknown_url(self, store: Store) -> None:
        """fallback 名单里没被预设认出来的 url 不能用：直接跳过。"""
        keenable_client = FakeClient(error=MCPError("主家没救"))
        odd_client = FakeClient([_fixture("you_search.json")])
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": "",
                            "fallback": ["odd"]})
        exts = {
            "keenable": FakeExt("keenable", KEENABLE_URL, tools=_keenable_tools(), client=keenable_client),
            "odd": FakeExt("odd", "https://odd.example/mcp",
                           tools={"odd-search": {"name": "odd-search", "inputSchema": {}}},
                           client=odd_client),
        }
        s = _search(store, exts)
        with pytest.raises(SearchError):
            await s.search("q")
        assert odd_client.calls == []

    @pytest.mark.asyncio
    async def test_search_with_specific_provider(self, store: Store) -> None:
        exa_client = FakeClient([_fixture("you_search.json")])
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": "",
                            "broad": ["exa-x"]})
        exts = {
            "keenable": FakeExt("keenable", KEENABLE_URL, tools=_keenable_tools(), client=FakeClient()),
            "exa-x": FakeExt("exa-x", "https://mcp.exa.ai/mcp",
                             tools={"web_search_advanced_exa": {"name": "web_search_advanced_exa", "inputSchema": {}}},
                             client=exa_client),
        }
        s = _search(store, exts)
        out = await s.search_with("exa-x", "q", limit=1)
        assert exa_client.calls and exa_client.calls[0][0] == "web_search_advanced_exa"
        assert out[0]["provider"] == "exa-x"

    @pytest.mark.asyncio
    async def test_search_with_unknown_provider_raises(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": ""})
        exts = {"keenable": FakeExt("keenable", KEENABLE_URL, tools=_keenable_tools(), client=FakeClient())}
        s = _search(store, exts)
        with pytest.raises(SearchUnavailable):
            await s.search_with("exa-x", "q")

    def test_broad_providers(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": "",
                            "broad": ["exa-x", "odd", "被关的"]})
        exts = {
            "keenable": FakeExt("keenable", KEENABLE_URL, tools=_keenable_tools(), client=FakeClient()),
            "exa-x": FakeExt("exa-x", "https://mcp.exa.ai/mcp",
                             tools={"web_search_advanced_exa": {"name": "web_search_advanced_exa", "inputSchema": {}}},
                             client=FakeClient()),
            "odd": FakeExt("odd", "https://odd.example/mcp",
                           tools={"odd-search": {"name": "odd-search", "inputSchema": {}}},
                           client=FakeClient()),
            "被关的": FakeExt("被关的", "https://mcp.tavily.com/mcp/",
                              enabled=False,
                              tools={"tavily_search": {"name": "tavily_search", "inputSchema": {}}},
                              client=FakeClient()),
        }
        s = _search(store, exts)
        names = s.broad_providers()
        # 主家 + broad 里认得出、开着的（odd 没被预设认出、被关的被关了）
        assert names == ["keenable", "exa-x"]


# ----------------------------------------------------------------------
# binding：fallback / broad 校验、clear 连带清理
# ----------------------------------------------------------------------


def _settings_with(names: dict[str, dict]):
    raw = {"extensions": {"mcp": [
        {"name": name, "url": d["url"], "enabled": d.get("enabled", True), "headers": {}}
        for name, d in names.items()
    ]}}
    settings, _ = load_settings(raw)
    return settings


class TestBindingValidation:
    def _tool_spec_of(self, tools: dict[str, dict]):
        def fn(name: str, tool: str):
            return (tools.get(name) or {}).get(tool)
        return fn

    def test_save_binding_accepts_fallback_broad(self, store: Store) -> None:
        settings = _settings_with({
            "keenable": {"url": KEENABLE_URL},
            "exa-x": {"url": "https://mcp.exa.ai/mcp"},
        })
        tools = {"keenable": {"search_web_pages": {}}, "exa-x": {"web_search_advanced_exa": {}}}
        out = save_binding(store, settings, {
            "mcp": "keenable", "tool": "search_web_pages", "extract_tool": "",
            "fallback": ["exa-x"], "broad": ["exa-x"],
        }, tool_spec_of=self._tool_spec_of(tools))
        assert out["fallback"] == ["exa-x"]
        assert out["broad"] == ["exa-x"]
        assert get_binding(store)["fallback"] == ["exa-x"]

    def test_save_binding_rejects_unknown_name(self, store: Store) -> None:
        settings = _settings_with({"keenable": {"url": KEENABLE_URL}})
        tools = {"keenable": {"search_web_pages": {}}}
        with pytest.raises(ValueError) as exc:
            save_binding(store, settings, {
                "mcp": "keenable", "tool": "search_web_pages",
                "fallback": ["没这家"],
            }, tool_spec_of=self._tool_spec_of(tools))
        assert "没这家" in str(exc.value)

    def test_save_binding_rejects_unpreset_url(self, store: Store) -> None:
        settings = _settings_with({
            "keenable": {"url": KEENABLE_URL},
            "odd": {"url": "https://odd.example/mcp"},
        })
        tools = {"keenable": {"search_web_pages": {}}, "odd": {"odd-search": {}}}
        with pytest.raises(ValueError) as exc:
            save_binding(store, settings, {
                "mcp": "keenable", "tool": "search_web_pages",
                "fallback": ["odd"],
            }, tool_spec_of=self._tool_spec_of(tools))
        assert "预设" in str(exc.value) or "odd" in str(exc.value)

    def test_old_binding_record_without_lists_still_works(self, store: Store) -> None:
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": ""})
        b = get_binding(store)
        assert b["fallback"] == [] and b["broad"] == []

    def test_clear_binding_removes_from_lists(self, store: Store) -> None:
        set_binding(store, {
            "mcp": "keenable", "tool": "search_web_pages", "extract_tool": "",
            "fallback": ["exa-x", "gone"], "broad": ["gone"],
        })
        assert clear_binding(store, mcp="gone") is True
        b = get_binding(store)
        assert b["fallback"] == ["exa-x"]
        assert b["broad"] == []
        # 主家被清 → 整个解绑
        assert clear_binding(store, mcp="keenable") is True
        assert get_binding(store) is None


# ----------------------------------------------------------------------
# web_search 工具：site / news 透传
# ----------------------------------------------------------------------


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_site_news_passed_through(self, store: Store) -> None:
        from CharTyr_MaiWork.maiwork.tools import Tools, ToolContext
        from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

        client = FakeClient([_fixture("keenable_search.json")])
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages", "extract_tool": ""})
        exts = {"keenable": FakeExt("keenable", KEENABLE_URL, tools=_keenable_tools(), client=client)}
        s = _search(store, exts)

        class _Profiles:
            def entries(self, gid):
                return []

        tools = Tools(store)
        register_builtin(tools, search=s, profiles=_Profiles())
        tool_def = tools.get("web_search", "worker")
        props = tool_def.parameters["properties"]
        assert "site" in props and "news" in props

        ctx = ToolContext(group_id="900000001", task_id="T-1", actor="子 agent #1", role="worker")
        res = await tool_def.handler(ctx, {"query": "任天堂", "limit": 2, "site": "nintendo.com", "news": True})
        assert res.ok
        _, args = client.calls[0]
        assert args["site"] == "nintendo.com"
        # 输出格式不带 provider（保持输出稳定）
        assert "provider" not in res.output
