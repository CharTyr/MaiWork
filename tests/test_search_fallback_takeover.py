"""A05 回归：主搜索坏了，已连好的备用要接管。

对应 docs/13-0.7.0整体审查.md §A05（证据 docs/audits/0.7.0/evidence/search-fallback.json）。
要分清两件事：

- **根本没绑定**：照旧 SearchUnavailable、零联网调用、available()=False；
- **绑定了但主家暂时坏了**（扩展没连上 / 工具清单缺 / 初始化失败 / 超时 / 429 / 5xx）：
  按 binding["fallback"] 名单递补；available() 按有效搜索链算；status() 说明
  「主搜索 X 暂时不可用（原因）——正在用备用 Y」；备用也全坏才抛最后一个错。

抓正文（extract）不在这次范围：搜索递补不许破坏它。
不碰真实网络：扩展连接走 httpx.MockTransport，假扩展直接注入假 client。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork import search_presets
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.extensions import Extensions
from CharTyr_MaiWork.maiwork.mcp_client import MCPError
from CharTyr_MaiWork.maiwork.search import Search, SearchError, SearchUnavailable
from CharTyr_MaiWork.maiwork.search_binding import set_binding
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tools

from test_extensions import _McpServer, _sse, _tool_spec

PRIMARY = "keenable"
FALLBACK = "tavily"
EXTRACT_MCP = "You"
PRIMARY_TOOL = search_presets.PRESETS[PRIMARY].search_tool     # search_web_pages
FALLBACK_TOOL = search_presets.PRESETS[FALLBACK].search_tool   # tavily_search
PRIMARY_URL = search_presets.PRESETS[PRIMARY].url_free
FALLBACK_URL = search_presets.PRESETS[FALLBACK].url_free
EXTRACT_URL = search_presets.PRESETS["you"].url_free

QUERY_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
URLS_SCHEMA = {"type": "object", "properties": {"urls": {"type": "array"}}, "required": ["urls"]}


# ----------------------------------------------------------------------
# 假扩展 / 假 client（比 test_search.py 的 FakeExt 多一个 ok/error，用来模拟「连过但初始化失败」）
# ----------------------------------------------------------------------


class _Client:
    """假 MCP client：录调用，按预设回包或抛错。"""

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


class _Ext:
    """假扩展运行体。ok 不给就不设这个属性（老假对象口径）。"""

    def __init__(self, name: str, url: str, *, enabled: bool = True, client=None,
                 tools: dict | None = None, headers: dict | None = None,
                 ok: bool | None = None, error: str = "") -> None:
        self.name = name
        self.url = url
        self.enabled = enabled
        self.client = client
        self.headers = dict(headers or {})
        self._tools = dict(tools or {})
        self.error = error
        if ok is not None:
            self.ok = ok

    def tools_remote(self) -> dict:
        return dict(self._tools)


class _Exts:
    """假 Extensions：runtime_of + _get_settings（Search._settings 认这个）。"""

    def __init__(self, exts: list[_Ext]) -> None:
        self._exts = {e.name: e for e in exts}
        raw = {"extensions": {"mcp": [
            {"name": e.name, "url": e.url, "enabled": e.enabled, "headers": e.headers} for e in exts
        ]}}
        self._settings, problems = load_settings(raw)
        assert problems == []

    def runtime_of(self, name: str):
        return self._exts.get(name)

    def _get_settings(self):
        return self._settings


def _tools(*names: str, schema: dict = QUERY_SCHEMA) -> dict:
    return {n: {"name": n, "inputSchema": schema} for n in names}


def _json_result(title: str, url: str) -> dict:
    payload = {"results": [{"url": url, "title": title, "content": "假结果正文"}]}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _search(store: Store, exts: list[_Ext], *, fallback: list[str] | None = None,
            extract: bool = False) -> Search:
    binding: dict = {"mcp": PRIMARY, "tool": PRIMARY_TOOL, "fallback": list(fallback or [])}
    if extract:
        binding.update({"extract_mcp": EXTRACT_MCP, "extract_tool": "you-contents"})
    set_binding(store, binding)
    return Search(store, lambda: _Exts(exts))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _broken_primary(*, client=None, tools=None, ok=None, error="") -> _Ext:
    return _Ext(PRIMARY, PRIMARY_URL, client=client, tools=tools or {}, ok=ok, error=error)


def _working_fallback(client: _Client) -> _Ext:
    return _Ext(FALLBACK, FALLBACK_URL, client=client, tools=_tools(FALLBACK_TOOL), ok=True)


# ----------------------------------------------------------------------
# 主家坏（初始化失败 / 工具清单缺）→ 备用接管
# ----------------------------------------------------------------------


class TestPrimaryBrokenFallbackTakesOver:
    @pytest.mark.asyncio
    async def test_primary_client_none_fallback_takes_over(self, store: Store) -> None:
        """主家扩展根本没连上（runtime.client 为 None）：备用接管。"""
        fb_client = _Client([_json_result("备用搜到的", "https://fallback.example/a")])
        s = _search(store, [_broken_primary(), _working_fallback(fb_client)], fallback=[FALLBACK])

        ok, text = s.status()
        assert ok is True
        assert "暂时不可用" in text and PRIMARY in text and "没连上" in text
        assert "备用" in text and FALLBACK in text and FALLBACK_TOOL in text
        assert s.available() is True

        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]
        assert fb_client.calls and fb_client.calls[0][0] == FALLBACK_TOOL

    @pytest.mark.asyncio
    async def test_primary_empty_tool_list_fallback_takes_over(self, store: Store) -> None:
        """主家初始化失败会留下 client 壳 + 空工具清单：不许当成「绑定填错」直接抛。"""
        primary_client = _Client()  # 有壳，但一个工具都没有
        fb_client = _Client([_json_result("备用搜到的", "https://fallback.example/b")])
        s = _search(
            store,
            [_broken_primary(client=primary_client), _working_fallback(fb_client)],
            fallback=[FALLBACK],
        )
        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]
        assert primary_client.calls == []  # 清单里没这个工具，根本不该拿它去调

    @pytest.mark.asyncio
    async def test_primary_init_failed_with_error_attribute(self, store: Store) -> None:
        """真运行体会给 ok=False + error（连不上）：状态说明要带原因。"""
        primary = _broken_primary(client=object(), ok=False, error="连不上：MCP 服务返回 HTTP 503")
        fb_client = _Client([_json_result("备用搜到的", "https://fallback.example/c")])
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        ok, text = s.status()
        assert ok is True
        assert "503" in text and "暂时不可用" in text and FALLBACK in text

    @pytest.mark.asyncio
    async def test_primary_tool_missing_fallback_takes_over(self, store: Store) -> None:
        """主家连上了、也有工具，但清单里没有绑定那个搜索工具：备用接管。"""
        primary_client = _Client()
        primary = _Ext(PRIMARY, PRIMARY_URL, client=primary_client,
                       tools=_tools("fetch_page_content"), ok=True)
        fb_client = _Client([_json_result("备用搜到的", "https://fallback.example/d")])
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]
        assert primary_client.calls == []


# ----------------------------------------------------------------------
# 主家调用失败（超时 / 429 / 5xx）→ 备用接管
# ----------------------------------------------------------------------


class TestPrimaryCallFailureFallbackTakesOver:
    def _pair(self, primary_error: Exception, *, fallback_results=None):
        primary = _Ext(PRIMARY, PRIMARY_URL, client=_Client(error=primary_error),
                       tools=_tools(PRIMARY_TOOL), ok=True)
        fb_client = _Client(fallback_results or [_json_result("备用搜到的", "https://fallback.example/e")])
        return primary, fb_client

    @pytest.mark.asyncio
    async def test_timeout(self, store: Store) -> None:
        primary, fb_client = self._pair(asyncio.TimeoutError("请求超时"))
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]

    @pytest.mark.asyncio
    async def test_429(self, store: Store) -> None:
        primary, fb_client = self._pair(MCPError("MCP 服务返回 HTTP 429：rate limited"))
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]
        assert len(primary.client.calls) == 1  # 429 不重试，直接换家

    @pytest.mark.asyncio
    async def test_5xx(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        from CharTyr_MaiWork.maiwork import search as search_mod

        monkeypatch.setattr(search_mod, "_MCP_RETRY_DELAY_S", 0.0)
        primary, fb_client = self._pair(MCPError("MCP 服务返回 HTTP 503"))
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]
        assert len(primary.client.calls) == 2  # 先按老规则重试一次，再换家


# ----------------------------------------------------------------------
# 备用也全坏 / 完全没绑定
# ----------------------------------------------------------------------


class TestEverythingBroken:
    @pytest.mark.asyncio
    async def test_primary_broken_and_fallback_call_fails_raises_fallback_error(self, store: Store) -> None:
        """备用连上了但真调用也失败：抛最后一个错（备用那家）。"""
        primary = _broken_primary()
        fb_client = _Client(error=MCPError("MCP 服务返回 HTTP 500"))
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        with pytest.raises(SearchError) as e:
            await s.search("MaiWork")
        assert FALLBACK in str(e.value)  # 抛的是最后试的那家
        assert "500" in str(e.value)

    @pytest.mark.asyncio
    async def test_all_providers_unusable_raises_and_status_reports_primary(self, store: Store) -> None:
        """主家和备用都不能用：抛最后试的那家的错；状态说明给主家原因，不说「正在用备用」。"""
        fallback = _Ext(FALLBACK, FALLBACK_URL, client=None, tools={})
        s = _search(store, [_broken_primary(), fallback], fallback=[FALLBACK])
        with pytest.raises((SearchError, SearchUnavailable)) as e:
            await s.search("MaiWork")
        assert "没连上" in str(e.value)
        assert s.available() is False
        ok, text = s.status()
        assert ok is False and "没连上" in text and PRIMARY in text
        assert "正在用备用" not in text

    @pytest.mark.asyncio
    async def test_primary_broken_without_fallback_keeps_old_error(self, store: Store) -> None:
        """没配备用：主家坏还是老口径的 SearchUnavailable（不编一个备用出来）。"""
        s = _search(store, [_broken_primary()])
        with pytest.raises(SearchUnavailable) as e:
            await s.search("MaiWork")
        assert "没连上" in str(e.value)


class TestNoBinding:
    @pytest.mark.asyncio
    async def test_no_binding_zero_calls_and_unavailable(self, store: Store) -> None:
        """完全没绑定：零联网调用，照旧 SearchUnavailable，available()=False。"""
        primary_client = _Client([_json_result("不该被调用", "https://never.example/a")])
        fb_client = _Client([_json_result("不该被调用", "https://never.example/b")])
        exts = [
            _Ext(PRIMARY, PRIMARY_URL, client=primary_client, tools=_tools(PRIMARY_TOOL), ok=True),
            _Ext(FALLBACK, FALLBACK_URL, client=fb_client, tools=_tools(FALLBACK_TOOL), ok=True),
        ]
        s = Search(store, lambda: _Exts(exts))
        with pytest.raises(SearchUnavailable) as e:
            await s.search("MaiWork")
        assert "还没指定联网搜索" in str(e.value)
        assert primary_client.calls == [] and fb_client.calls == []
        assert s.available() is False
        ok, text = s.status()
        assert ok is False and "还没指定联网搜索" in text


# ----------------------------------------------------------------------
# 状态文案：主家好了不提备用；主家坏了才说「正在用备用」
# ----------------------------------------------------------------------


class TestStatusText:
    @pytest.mark.asyncio
    async def test_primary_ok_does_not_mention_fallback(self, store: Store) -> None:
        primary_client = _Client([_json_result("主家搜到的", "https://primary.example/a")])
        primary = _Ext(PRIMARY, PRIMARY_URL, client=primary_client, tools=_tools(PRIMARY_TOOL), ok=True)
        fb_client = _Client([_json_result("备用不该被用", "https://fallback.example/f")])
        s = _search(store, [primary, _working_fallback(fb_client)], fallback=[FALLBACK])
        ok, text = s.status()
        assert ok is True and "备用" not in text
        assert PRIMARY in text and PRIMARY_TOOL in text
        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [PRIMARY]
        assert fb_client.calls == []

    @pytest.mark.asyncio
    async def test_primary_ok_fallback_broken_still_ok(self, store: Store) -> None:
        """主家好、备用坏：可用，不该因为备用坏就报不可用。"""
        primary_client = _Client([_json_result("主家搜到的", "https://primary.example/b")])
        primary = _Ext(PRIMARY, PRIMARY_URL, client=primary_client, tools=_tools(PRIMARY_TOOL), ok=True)
        fallback = _Ext(FALLBACK, FALLBACK_URL, client=None, tools={})
        s = _search(store, [primary, fallback], fallback=[FALLBACK])
        assert s.available() is True
        ok, text = s.status()
        assert ok is True and "备用" not in text


# ----------------------------------------------------------------------
# 抓正文不受搜索递补影响
# ----------------------------------------------------------------------


class TestExtractUnaffected:
    @pytest.mark.asyncio
    async def test_extract_still_uses_its_own_mcp(self, store: Store) -> None:
        extract_client = _Client([{"structuredContent": {"items": [{"markdown": "另一家抽的正文"}]}}])
        s = _search(
            store,
            [
                _broken_primary(),
                _working_fallback(_Client([_json_result("备用搜到的", "https://fallback.example/g")])),
                _Ext(EXTRACT_MCP, EXTRACT_URL, client=extract_client,
                     tools=_tools("you-contents", schema=URLS_SCHEMA), ok=True),
            ],
            fallback=[FALLBACK],
            extract=True,
        )
        text = await s.extract("https://x.com/a")
        assert "另一家抽的正文" in text
        assert extract_client.calls and extract_client.calls[0][0] == "you-contents"
        assert s.extract_available()[0] is True


# ----------------------------------------------------------------------
# 集成：真 Extensions + MockTransport（复刻审查里的主备场景）
# ----------------------------------------------------------------------


def _mcp_result(req, body):
    return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {
        "content": [{"type": "text", "text": json.dumps(
            {"results": [{"title": "备用搜到的", "url": "https://fallback.example/a", "content": "假结果正文"}]},
            ensure_ascii=False)}],
        "isError": False,
    }})


@pytest.mark.asyncio
async def test_real_extensions_primary_init_failure_fallback_takes_over(tmp_path: Path) -> None:
    """真运行体：主家 initialize 被 503 拒，留下 client 壳 + 空工具清单；备用接管。"""
    working = _McpServer(
        [_tool_spec(FALLBACK_TOOL, schema=QUERY_SCHEMA), _tool_spec("tavily_extract", schema=URLS_SCHEMA)],
        on_call=_mcp_result,
    )

    def router(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.keenable.ai":
            return httpx.Response(503, text="primary initialization unavailable")
        return working(req)

    settings, problems = load_settings({"extensions": {"mcp": [
        {"name": PRIMARY, "url": PRIMARY_URL, "enabled": True},
        {"name": FALLBACK, "url": FALLBACK_URL, "enabled": True},
    ]}})
    assert problems == []
    store = Store(tmp_path / "t.db")
    store.migrate()
    tools = Tools(store)
    exts = Extensions(lambda: settings, transport=httpx.MockTransport(router), store=store)
    await exts.start(tools)
    try:
        primary = exts.runtime_of(PRIMARY)
        assert primary is not None and primary.ok is False
        assert primary.client is not None  # 壳还在：这就是审查里说的「不能当成绑定填错」
        assert primary.tools_remote() == {}
        assert "503" in primary.error

        set_binding(store, {"mcp": PRIMARY, "tool": PRIMARY_TOOL, "fallback": [FALLBACK]})
        s = Search(store, lambda: exts)
        ok, text = s.status()
        assert ok is True
        assert "暂时不可用" in text and PRIMARY in text and "503" in text
        assert "备用" in text and FALLBACK in text and FALLBACK_TOOL in text
        assert s.available() is True

        out = await s.search("MaiWork")
        assert [r["provider"] for r in out] == [FALLBACK]
        assert "tools/call" in working.methods()
    finally:
        await exts.aclose()
        store.close()
