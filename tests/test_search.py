"""search.py 通用 MCP 搜索适配层测试（2026-10：内置 tavily/exa/you 全删，只走扩展绑定的 MCP）。

假扩展 + 假 client 注入；不碰真实网络；密钥一律假值。
覆盖：Tavily / You.com / Exa 三种返回形状、纯文本回落、参数按 inputSchema 映射、
extract、5xx 重试一次、没绑定 / 扩展不在 / 扩展没连上 的中文错误、available()。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.mcp_client import MCPError
from CharTyr_MaiWork.maiwork.search import Search, SearchError, SearchUnavailable, _SNIPPET_MAX
from CharTyr_MaiWork.maiwork.search_binding import set_binding
from CharTyr_MaiWork.maiwork.store import Store

TAVILY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索词"},
        "max_results": {"type": "integer"},
        "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
        "topic": {"type": "string", "enum": ["general", "news"]},
    },
    "required": ["query"],
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
    "required": ["urls"],
}

YOU_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "count": {"type": "integer"},
        "freshness": {"type": "string", "enum": ["day", "week", "month", "year"]},
    },
    "required": ["query"],
}

EXA_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "numResults": {"type": "integer"},
        "startPublishedDate": {"type": "string", "description": "ISO date"},
    },
    "required": ["query"],
}


class FakeClient:
    """假 MCP client：录调用，按预设回包/抛错。"""

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
    def __init__(self, name: str, url: str = "https://mcp.example/mcp", enabled: bool = True,
                 headers: dict | None = None, tools: dict | None = None, client=None) -> None:
        self.name = name
        self.url = url
        self.enabled = enabled
        self.headers = dict(headers or {})
        self._tools = tools or {}
        self.client = client

    def tools_remote(self) -> dict:
        return self._tools

    @property
    def entry(self):
        """status_of 走 extensions_web.merged_entries 用的配置条目。"""
        from CharTyr_MaiWork.maiwork.config import McpExtensionSetting

        return McpExtensionSetting(
            name=self.name, url=self.url, enabled=self.enabled,
            headers=self.headers, tools=(), roles=("worker",), timeout_s=20,
        )


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
    settings, _ = load_settings({})
    return Search(store, lambda: FakeExtensions(exts))


def _bind_tavily(store: Store, client, *, extract: bool = True, schema=TAVILY_SCHEMA) -> dict[str, FakeExt]:
    set_binding(store, {"mcp": "tavily", "tool": "tavily-search",
                        "extract_tool": "tavily-extract" if extract else ""})
    tools = {"tavily-search": {"name": "tavily-search", "inputSchema": schema}}
    if extract:
        tools["tavily-extract"] = {"name": "tavily-extract", "inputSchema": EXTRACT_SCHEMA}
    return {"tavily": FakeExt("tavily", tools=tools, client=client)}


def _text_result(payload) -> dict:
    """content[0].text 是 JSON 字符串的回包（Tavily MCP 实测格式）。"""
    import json

    return {"content": [{"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)}]}


_TAVILY_PAYLOAD = {
    "results": [
        {"url": "https://example.com/a", "title": "MaiBot 项目", "content": "一个 QQ 机器人",
         "published_date": "2026-09-26T08:00:00Z"},
        {"url": "https://example.com/b", "title": "无日期", "content": "内容"},
    ]
}


class TestUnavailable:
    @pytest.mark.asyncio
    async def test_no_binding_raises_chinese(self, store: Store) -> None:
        s = _search(store, {})
        with pytest.raises(SearchUnavailable) as e:
            await s.search("MaiBot")
        assert "还没指定联网搜索" in str(e.value)
        assert "设置 → 工具" in str(e.value)

    @pytest.mark.asyncio
    async def test_extension_gone(self, store: Store) -> None:
        set_binding(store, {"mcp": "没了", "tool": "s", "extract_tool": ""})
        s = _search(store, {})
        with pytest.raises(SearchUnavailable) as e:
            await s.search("x")
        assert "不在了" in str(e.value)

    @pytest.mark.asyncio
    async def test_extension_not_connected(self, store: Store) -> None:
        set_binding(store, {"mcp": "tavily", "tool": "tavily-search", "extract_tool": ""})
        exts = {"tavily": FakeExt("tavily", tools={"tavily-search": {}}, client=None)}
        s = _search(store, exts)
        with pytest.raises(SearchUnavailable) as e:
            await s.search("x")
        assert "没连上" in str(e.value)

    def test_available(self, store: Store) -> None:
        s = _search(store, {})
        assert s.available() is False
        client = FakeClient([_text_result({"results": []})])
        exts = _bind_tavily(store, client)
        s2 = _search(store, exts)
        assert s2.available() is True
        # 扩展停用 → 不可用
        exts["tavily"].enabled = False
        assert s2.available() is False

    def test_status_text(self, store: Store) -> None:
        s = _search(store, {})
        ok, text = s.status()
        assert ok is False and "还没指定联网搜索" in text
        client = FakeClient()
        exts = _bind_tavily(store, client)
        s2 = _search(store, exts)
        ok, text = s2.status()
        assert ok is True and "tavily-search" in text


class TestTavilyShape:
    @pytest.mark.asyncio
    async def test_arguments_and_result_mapping(self, store: Store) -> None:
        client = FakeClient([_text_result(_TAVILY_PAYLOAD)])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        out = await s.search("MaiBot 是什么", limit=5, days=13)
        name, args = client.calls[0]
        assert name == "tavily-search"
        assert args["query"] == "MaiBot 是什么"
        assert args["max_results"] == 5
        assert args["time_range"] == "month"  # 13 天 → month（按 schema enum 映射）
        assert len(out) == 2
        first = out[0]
        assert first["title"] == "MaiBot 项目"
        assert first["url"] == "https://example.com/a"
        assert first["snippet"] == "一个 QQ 机器人"
        assert isinstance(first["published"], (int, float))
        assert out[1]["published"] is None

    @pytest.mark.asyncio
    async def test_no_days_omits_time_range(self, store: Store) -> None:
        client = FakeClient([_text_result({"results": []})])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        assert await s.search("x") == []
        _, args = client.calls[0]
        assert "time_range" not in args

    @pytest.mark.asyncio
    async def test_snippet_truncated(self, store: Store) -> None:
        client = FakeClient([_text_result({"results": [{"url": "https://e.com", "content": "字" * 800}]})])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        out = await s.search("x")
        assert len(out[0]["snippet"]) == _SNIPPET_MAX
        assert out[0]["title"] == ""  # 没 title 字段就空串


class TestYouShape:
    @pytest.mark.asyncio
    async def test_structured_content_results_web(self, store: Store) -> None:
        payload = {
            "results": {
                "web": [
                    {"url": "https://you.example/a", "title": "A", "description": "描述A",
                     "snippets": ["片段1", "片段2"], "page_age": "2026-09-25T10:00:00"},
                    {"url": "https://you.example/b", "name": "B",
                     "contents": {"highlights": ["高光"]}},
                ],
                "news": [{"url": "https://you.example/c", "title": "C", "description": "新闻"}],
            }
        }
        client = FakeClient([{"structuredContent": payload, "content": [{"type": "text", "text": ""}]}])
        set_binding(store, {"mcp": "you", "tool": "you-search", "extract_tool": ""})
        exts = {"you": FakeExt("you", tools={"you-search": {"name": "you-search", "inputSchema": YOU_SCHEMA}}, client=client)}
        s = _search(store, exts)
        out = await s.search("x", limit=6, days=2)
        name, args = client.calls[0]
        assert name == "you-search"
        assert args["count"] == 6
        assert args["freshness"] == "week"
        assert [r["url"] for r in out] == ["https://you.example/a", "https://you.example/b", "https://you.example/c"]
        assert "描述A" in out[0]["snippet"] and "片段1" in out[0]["snippet"]
        assert isinstance(out[0]["published"], (int, float))  # 没时区按 UTC 也能解出 epoch
        assert out[1]["title"] == "B"  # name 也当标题
        assert "高光" in out[1]["snippet"]


class TestExaShape:
    @pytest.mark.asyncio
    async def test_nested_results_and_date_mapping(self, store: Store) -> None:
        payload = {
            "results": [
                {"title": "Exa 结果", "url": "https://exa.example/e", "text": "exa 摘要",
                 "publishedDate": "2026-09-25T03:04:05.000Z"},
            ]
        }
        client = FakeClient([_text_result(payload)])
        set_binding(store, {"mcp": "exa", "tool": "web_search_exa", "extract_tool": ""})
        exts = {"exa": FakeExt("exa", tools={"web_search_exa": {"name": "web_search_exa", "inputSchema": EXA_SCHEMA}}, client=client)}
        s = _search(store, exts)
        out = await s.search("MaiWork", limit=4, days=14)
        name, args = client.calls[0]
        assert args["query"] == "MaiWork"
        assert args["numResults"] == 4
        assert "startPublishedDate" in args  # days → ISO 起始日期
        assert args["startPublishedDate"].startswith("20")
        assert out[0]["snippet"] == "exa 摘要"
        assert isinstance(out[0]["published"], (int, float))


class TestParamMapping:
    @pytest.mark.asyncio
    async def test_first_required_string_as_query(self, store: Store) -> None:
        schema = {
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["keyword"],
        }
        client = FakeClient([_text_result({"results": []})])
        set_binding(store, {"mcp": "odd", "tool": "odd-search", "extract_tool": ""})
        exts = {"odd": FakeExt("odd", tools={"odd-search": {"name": "odd-search", "inputSchema": schema}}, client=client)}
        s = _search(store, exts)
        await s.search("找个东西", limit=3, days=5)
        _, args = client.calls[0]
        assert args["keyword"] == "找个东西"
        # n 不在已知条数名里 → 条数不传；days 映射不上 → 不传
        assert "n" not in args

    @pytest.mark.asyncio
    async def test_q_field_and_topk(self, store: Store) -> None:
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["q"],
        }
        client = FakeClient([_text_result({"results": []})])
        set_binding(store, {"mcp": "g", "tool": "g-search", "extract_tool": ""})
        exts = {"g": FakeExt("g", tools={"g-search": {"name": "g-search", "inputSchema": schema}}, client=client)}
        s = _search(store, exts)
        await s.search("问题", limit=9)
        _, args = client.calls[0]
        assert args["q"] == "问题"
        assert args["top_k"] == 9

    @pytest.mark.asyncio
    async def test_days_to_integer_field(self, store: Store) -> None:
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}, "days": {"type": "integer"}},
            "required": ["query"],
        }
        client = FakeClient([_text_result({"results": []})])
        set_binding(store, {"mcp": "d", "tool": "d-search", "extract_tool": ""})
        exts = {"d": FakeExt("d", tools={"d-search": {"name": "d-search", "inputSchema": schema}}, client=client)}
        s = _search(store, exts)
        await s.search("x", days=6)
        _, args = client.calls[0]
        assert args["days"] == 6

    @pytest.mark.asyncio
    async def test_unknown_params_not_passed(self, store: Store) -> None:
        schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        client = FakeClient([_text_result({"results": []})])
        set_binding(store, {"mcp": "m", "tool": "m-search", "extract_tool": ""})
        exts = {"m": FakeExt("m", tools={"m-search": {"name": "m-search", "inputSchema": schema}}, client=client)}
        s = _search(store, exts)
        await s.search("x", limit=7, days=3)
        _, args = client.calls[0]
        assert args == {"query": "x"}


class TestFallbackText:
    @pytest.mark.asyncio
    async def test_plain_text_becomes_one_result(self, store: Store) -> None:
        client = FakeClient([{"content": [{"type": "text", "text": "今天的新闻：MaiBot 发布了新版"}]}])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        out = await s.search("x")
        assert len(out) == 1
        assert out[0]["url"] == ""
        assert "MaiBot" in out[0]["snippet"]

    @pytest.mark.asyncio
    async def test_deeply_nested_list_found(self, store: Store) -> None:
        payload = {"data": {"hits": [{"link": "", "headline": "没 URL 跳过"}, {"url": "https://deep.example/1", "title": "深", "body": "深内容"}]}}
        client = FakeClient([_text_result(payload)])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        out = await s.search("x")
        assert len(out) == 1
        assert out[0]["url"] == "https://deep.example/1"
        assert out[0]["title"] == "深"
        assert out[0]["snippet"] == "深内容"


class TestExtract:
    @pytest.mark.asyncio
    async def test_extract_urls_array(self, store: Store) -> None:
        client = FakeClient([_text_result({"results": [{"url": "https://x.com/a", "title": "标题", "raw_content": "正文内容很长"}]})])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        text = await s.extract("https://x.com/a")
        name, args = client.calls[0]
        assert name == "tavily-extract"
        assert args["urls"] == ["https://x.com/a"]
        assert "正文内容很长" in text and "标题" in text

    @pytest.mark.asyncio
    async def test_extract_single_url_string(self, store: Store) -> None:
        schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
        client = FakeClient([{"structuredContent": {"items": [{"markdown": "# 正文"}]}}])
        set_binding(store, {"mcp": "you", "tool": "you-search", "extract_tool": "you-contents"})
        exts = {"you": FakeExt("you", tools={
            "you-search": {"name": "you-search", "inputSchema": YOU_SCHEMA},
            "you-contents": {"name": "you-contents", "inputSchema": schema},
        }, client=client)}
        s = _search(store, exts)
        text = await s.extract("https://x.com/a")
        _, args = client.calls[0]
        assert args["url"] == "https://x.com/a"
        assert "正文" in text

    @pytest.mark.asyncio
    async def test_extract_empty_without_extract_tool(self, store: Store) -> None:
        client = FakeClient()
        exts = _bind_tavily(store, client, extract=False)
        s = _search(store, exts)
        assert await s.extract("https://x.com/a") == ""
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_extract_empty_when_unbound(self, store: Store) -> None:
        s = _search(store, {})
        assert await s.extract("https://x.com/a") == ""


class TestRetry5xx:
    @pytest.mark.asyncio
    async def test_5xx_retried_once(self, store: Store, monkeypatch) -> None:
        from CharTyr_MaiWork.maiwork import search as search_mod

        monkeypatch.setattr(search_mod, "_MCP_RETRY_DELAY_S", 0.0)
        client = FakeClient([MCPError("MCP 服务返回 HTTP 525"), _text_result({"results": [{"url": "https://e.com", "content": "好"}]})])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        out = await s.search("x")
        assert len(client.calls) == 2
        assert out[0]["url"] == "https://e.com"

    @pytest.mark.asyncio
    async def test_5xx_twice_raises(self, store: Store, monkeypatch) -> None:
        from CharTyr_MaiWork.maiwork import search as search_mod

        monkeypatch.setattr(search_mod, "_MCP_RETRY_DELAY_S", 0.0)
        client = FakeClient([MCPError("MCP 服务返回 HTTP 500"), MCPError("MCP 服务返回 HTTP 525")])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        with pytest.raises(SearchError) as e:
            await s.search("x")
        assert "500" in str(e.value) or "525" in str(e.value)
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_non_5xx_not_retried(self, store: Store) -> None:
        client = FakeClient([MCPError("MCP 服务返回 HTTP 401：bad key")])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        with pytest.raises(SearchError):
            await s.search("x")
        assert len(client.calls) == 1


class TestErrors:
    @pytest.mark.asyncio
    async def test_mcp_error_wrapped_chinese(self, store: Store) -> None:
        client = FakeClient(error=MCPError("连不上"))
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        with pytest.raises(SearchError) as e:
            await s.search("x")
        assert "搜索失败" in str(e.value)
        assert "tavily" in str(e.value)

    @pytest.mark.asyncio
    async def test_headers_secret_not_leaked(self, store: Store) -> None:
        """扩展 headers 里的密钥不能出现在错误文本里。"""
        secret = "ext-SECRET-显眼123"
        client = FakeClient(error=MCPError(f"服务端回了 bad key {secret}"))
        exts = _bind_tavily(store, client)
        exts["tavily"].headers = {"Authorization": f"Bearer {secret}"}
        s = _search(store, exts)
        with pytest.raises(SearchError) as e:
            await s.search("x")
        assert secret not in str(e.value)

    def test_known_secrets_include_headers(self, store: Store) -> None:
        client = FakeClient()
        exts = _bind_tavily(store, client)
        exts["tavily"].headers = {"Authorization": "Bearer ext-SECRET-显眼123"}
        s = _search(store, exts)
        assert "ext-SECRET-显眼123" in s.known_secrets()

    @pytest.mark.asyncio
    async def test_close_is_noop(self, store: Store) -> None:
        client = FakeClient([_text_result({"results": []})])
        exts = _bind_tavily(store, client)
        s = _search(store, exts)
        await s.search("x")
        await s.aclose()  # 不炸；连接归 extensions 管
        await s.close()


class TestExtractFromOtherMcp:
    """搜索和抓正文分属两个 MCP：抽正文走抓正文那家的连接，只看它自己能不能用。"""

    def _two(self, store: Store, *, search_enabled: bool = True):
        search_client = FakeClient()
        extract_client = FakeClient([{"structuredContent": {"items": [{"markdown": "# 另一家抽的正文"}]}}])
        set_binding(store, {"mcp": "keenable", "tool": "search_web_pages",
                            "extract_mcp": "You", "extract_tool": "you-contents"})
        schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
        exts = {
            "keenable": FakeExt("keenable", enabled=search_enabled, tools={
                "search_web_pages": {"name": "search_web_pages", "inputSchema": YOU_SCHEMA},
            }, client=search_client, headers={"X-API-Key": "k-SECRET-甲"}),
            "You": FakeExt("You", tools={
                "you-contents": {"name": "you-contents", "inputSchema": schema},
            }, client=extract_client, headers={"Authorization": "Bearer y-SECRET-乙"}),
        }
        return search_client, extract_client, _search(store, exts)

    @pytest.mark.asyncio
    async def test_extract_calls_the_extract_mcp(self, store: Store) -> None:
        search_client, extract_client, s = self._two(store)
        text = await s.extract("https://x.com/a")
        assert "另一家抽的正文" in text
        assert extract_client.calls and extract_client.calls[0][0] == "you-contents"
        assert search_client.calls == []

    @pytest.mark.asyncio
    async def test_extract_works_even_if_search_mcp_is_off(self, store: Store) -> None:
        _, extract_client, s = self._two(store, search_enabled=False)
        assert "另一家抽的正文" in await s.extract("https://x.com/a")

    def test_known_secrets_cover_both_mcps(self, store: Store) -> None:
        _, _, s = self._two(store)
        secrets = s.known_secrets()
        assert "k-SECRET-甲" in secrets and "y-SECRET-乙" in secrets
