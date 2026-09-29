"""tools_builtin.py 单元测试：内网拒绝、跳转链检查、正文提取、read_profile 跨群拒绝、submit_result。"""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.search import Search, SearchUnavailable
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

from fakes import FakeProfiles


class FakeSearch:
    """假搜索：回放预设结果；search_error 非空时抛它。"""

    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    async def search(self, query, *, limit=8, days=None):
        self.calls.append((query, limit, days))
        if self.error is not None:
            raise self.error
        return list(self.results)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings():
    raw = {"groups": {"serve": [{"group": "qq:900000001"}]}}
    s, _ = load_settings(raw)
    return s


def _public_dns(host: str):
    return ["93.184.216.34"]  # example.com 的公网 IP


def _make(store, settings, *, search=None, profiles=None, transport=None, resolver=None):
    tools = Tools(store)
    register_builtin(
        tools,
        search=search if search is not None else FakeSearch(),
        profiles=profiles if profiles is not None else FakeProfiles(),
        http_transport=transport,
        get_settings=lambda: settings,
        resolver=resolver or _public_dns,
    )
    return tools


def _ctx(**over):
    base = dict(group_id="900000001", task_id="T-1", actor="子 agent #1", role="worker")
    base.update(over)
    return ToolContext(**base)


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_formats_results(self, store, settings):
        search = FakeSearch(
            [
                {"title": "标题一", "url": "https://a.com/1", "snippet": "摘要一", "published": 1790000000.0},
                {"title": "标题二", "url": "https://b.com/2", "snippet": "摘要二", "published": None},
            ]
        )
        tools = _make(store, settings, search=search)
        r = await tools.call("web_search", {"query": "MaiBot"}, _ctx())
        assert r.ok
        assert "1." in r.output and "标题一" in r.output and "https://a.com/1" in r.output
        assert "2." in r.output and "标题二" in r.output
        assert search.calls[0][0] == "MaiBot"

    @pytest.mark.asyncio
    async def test_passes_days_and_limit(self, store, settings):
        search = FakeSearch([])
        tools = _make(store, settings, search=search)
        r = await tools.call("web_search", {"query": "x", "days": 7, "limit": 5}, _ctx())
        assert r.ok
        assert search.calls[0] == ("x", 5, 7)

    @pytest.mark.asyncio
    async def test_search_unavailable_returns_not_ok(self, store, settings):
        search = FakeSearch(error=SearchUnavailable("没配"))
        tools = _make(store, settings, search=search)
        r = await tools.call("web_search", {"query": "x"}, _ctx())
        assert r.ok is False
        assert "没配" in r.error or "搜索" in r.error

    @pytest.mark.asyncio
    async def test_web_search_is_worker_only(self, store, settings):
        tools = _make(store, settings)
        r = await tools.call("web_search", {"query": "x"}, _ctx(role="main", actor="主模型"))
        assert r.ok is False


class TestFetchPage:
    def _html(self, title="测试页", body="正文内容"):
        return (
            f"<html><head><title>{title}</title></head><body>"
            f"<nav>导航</nav><script>var x=1;</script><style>p{{}}</style>"
            f"<p>{body}</p></body></html>"
        )

    @pytest.mark.asyncio
    async def test_fetch_html_extracts_text_and_title(self, store, settings):
        """底线（有没有 trafilatura 都得满足）：正文、标题在，script 去掉。"""
        def handler(request):
            return httpx.Response(200, text=self._html(body="MaiWork 是一个插件"), headers={"content-type": "text/html; charset=utf-8"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/page"}, _ctx())
        assert r.ok
        assert "MaiWork 是一个插件" in r.output
        assert "测试页" in r.output
        assert "var x=1" not in r.output  # script 去掉

    @pytest.mark.asyncio
    async def test_fetch_html_fallback_strips_nav_and_script(self, store, settings, monkeypatch):
        """trafilatura 导入失败（本机没装/线上装坏）→ 标准库兜底：正文、标题在，nav/script/style 全去掉。

        trafilatura 自己会保留导航栏文字，「去 nav」只是兜底的承诺，不能拿来卡两种提取器。
        """
        def handler(request):
            return httpx.Response(200, text=self._html(body="MaiWork 是一个插件"), headers={"content-type": "text/html; charset=utf-8"})

        # sys.modules 里塞 None → 函数内 import trafilatura 直接 ImportError，走兜底
        monkeypatch.setitem(sys.modules, "trafilatura", None)
        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/page"}, _ctx())
        assert r.ok
        assert "MaiWork 是一个插件" in r.output
        assert "测试页" in r.output
        assert "var x=1" not in r.output  # script 去掉
        assert "导航" not in r.output      # nav 去掉

    @pytest.mark.asyncio
    async def test_reject_private_ip_literal(self, store, settings):
        called = []

        def handler(request):
            called.append(1)
            return httpx.Response(200, text="x")

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        for url in ["http://127.0.0.1/", "http://localhost:8080/x", "http://192.168.1.5/", "http://10.0.0.3/", "http://169.254.1.1/"]:
            r = await tools.call("fetch_page", {"url": url}, _ctx())
            assert r.ok is False, url
            assert "内网" in r.error or "本机" in r.error or "不允许" in r.error
        assert not called  # 根本没发请求

    @pytest.mark.asyncio
    async def test_reject_private_dns_result(self, store, settings):
        def handler(request):
            return httpx.Response(200, text="x")

        def bad_resolver(host):
            return ["192.168.1.10"]

        tools = _make(store, settings, transport=httpx.MockTransport(handler), resolver=bad_resolver)
        r = await tools.call("fetch_page", {"url": "http://evil.example.com/"}, _ctx())
        assert r.ok is False
        assert "内网" in r.error or "不允许" in r.error

    @pytest.mark.asyncio
    async def test_reject_redirect_to_private(self, store, settings):
        def handler(request):
            # 外网地址 → 302 到内网
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/"}, _ctx())
        assert r.ok is False
        assert "内网" in r.error or "跳转" in r.error or "不允许" in r.error

    @pytest.mark.asyncio
    async def test_follow_redirect_to_public(self, store, settings):
        def handler(request):
            if request.url.path == "/start":
                return httpx.Response(301, headers={"location": "/final"})
            return httpx.Response(200, text=self._html(body="最终正文"), headers={"content-type": "text/html"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/start"}, _ctx())
        assert r.ok
        assert "最终正文" in r.output

    @pytest.mark.asyncio
    async def test_fetch_page_leaves_final_url_after_redirect(self, store, settings):
        """跟随跳转后要留下最终地址：交付里引用最终地址时，验收引用核对才不会误判「没打开过」。

        - 结果 data 里带 final_url；
        - tool_calls 的 output 摘要里有可解析的「最终地址」标记（不改表结构）。
        """
        def handler(request):
            if request.url.path == "/short":
                return httpx.Response(302, headers={"location": "https://long.example/very/long?a=1"})
            return httpx.Response(200, text=self._html(body="长链正文"), headers={"content-type": "text/html"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/short"}, _ctx())
        assert r.ok
        assert r.data["url"] == "http://example.com/short"  # 请求的短链照旧留着
        assert r.data["final_url"] == "https://long.example/very/long?a=1"

        row = store.read().execute(
            "SELECT input, output FROM tool_calls WHERE tool='fetch_page'"
        ).fetchone()
        assert row["input"] == "http://example.com/short"
        assert "最终地址" in str(row["output"])
        assert "https://long.example/very/long?a=1" in str(row["output"])

    @pytest.mark.asyncio
    async def test_reject_non_http_scheme(self, store, settings):
        tools = _make(store, settings)
        for url in ["file:///etc/passwd", "ftp://x.com/a", "gopher://x/"]:
            r = await tools.call("fetch_page", {"url": url}, _ctx())
            assert r.ok is False, url

    @pytest.mark.asyncio
    async def test_reject_unsupported_content_type(self, store, settings):
        def handler(request):
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/a.png"}, _ctx())
        assert r.ok is False
        assert "类型" in r.error or "不支持" in r.error

    @pytest.mark.asyncio
    async def test_json_body_passes(self, store, settings):
        def handler(request):
            return httpx.Response(200, json={"a": 1}, headers={"content-type": "application/json"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/api"}, _ctx())
        assert r.ok
        json.loads(r.output)  # 输出是合法 JSON 文本

    @pytest.mark.asyncio
    async def test_plain_text_passes(self, store, settings):
        def handler(request):
            return httpx.Response(200, text="纯文本内容", headers={"content-type": "text/plain"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/a.txt"}, _ctx())
        assert r.ok and "纯文本内容" in r.output

    @pytest.mark.asyncio
    async def test_truncates_long_body(self, store, settings):
        def handler(request):
            return httpx.Response(200, text="字" * 20000, headers={"content-type": "text/plain"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/long"}, _ctx())
        assert r.ok
        assert len(r.output) <= 8100  # 截 8000 + 截断标记

    @pytest.mark.asyncio
    async def test_http_error(self, store, settings):
        def handler(request):
            return httpx.Response(500, text="boom")

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/"}, _ctx())
        assert r.ok is False
        assert "500" in r.error


class TestFetchPageOgImage:
    """fetch_page 额外返回 og:image（只收 http(s) 绝对地址；没有就 image_url=""）。"""

    @pytest.mark.asyncio
    async def test_og_image_absolute(self, store, settings):
        html = (
            '<html><head><title>页</title>'
            '<meta property="og:image" content="https://cdn.example.com/cover.jpg?v=2">'
            "</head><body><p>正文</p></body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/a"}, _ctx())
        assert r.ok
        assert r.data["image_url"] == "https://cdn.example.com/cover.jpg?v=2"

    @pytest.mark.asyncio
    async def test_og_image_name_attr_and_single_quotes(self, store, settings):
        """name= 写法、单引号、标签属性乱序都认。"""
        html = (
            "<html><head><title>页</title>"
            "<meta content='https://img.example.com/p.png' name='og:image'>"
            "</head><body><p>正文</p></body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/a"}, _ctx())
        assert r.ok
        assert r.data["image_url"] == "https://img.example.com/p.png"

    @pytest.mark.asyncio
    async def test_og_image_relative_resolved(self, store, settings):
        """相对地址按最终页面 URL 解析成绝对地址。"""
        html = (
            '<html><head><title>页</title>'
            '<meta property="og:image" content="/static/cover.jpg">'
            "</head><body><p>正文</p></body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "https://example.com/news/article"}, _ctx())
        assert r.ok
        assert r.data["image_url"] == "https://example.com/static/cover.jpg"

    @pytest.mark.asyncio
    async def test_og_image_missing_or_bad_scheme(self, store, settings):
        """没有 og:image → 空串；非 http(s)（data:）→ 空串。"""
        html_no = "<html><head><title>页</title></head><body><p>正文</p></body></html>"
        html_data = (
            '<html><head><meta property="og:image" content="data:image/png;base64,AAAA">'
            "</head><body><p>正文</p></body></html>"
        )
        state = {"html": html_no}

        def handler(request):
            return httpx.Response(200, text=state["html"], headers={"content-type": "text/html"})

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "http://example.com/a"}, _ctx())
        assert r.ok
        assert r.data["image_url"] == ""

        state["html"] = html_data
        r = await tools.call("fetch_page", {"url": "http://example.com/a"}, _ctx())
        assert r.ok
        assert r.data["image_url"] == ""


class TestReadProfile:
    @pytest.mark.asyncio
    async def test_read_own_group_profile(self, store, settings):
        profiles = FakeProfiles()
        profiles.entries_map["900000001"] = [
            {"id": 1, "category": "recent", "text": "最近在聊新店", "evidence_count": 5, "last_ts": 1.0},
            {"id": 2, "category": "interest", "text": "喜欢折腾智能家居", "evidence_count": 9, "last_ts": 2.0},
        ]
        tools = _make(store, settings, profiles=profiles)
        r = await tools.call("read_profile", {}, _ctx())
        assert r.ok
        assert "最近在聊新店" in r.output
        assert "喜欢折腾智能家居" in r.output
        assert "最近在聊" in r.output  # 类别名

    @pytest.mark.asyncio
    async def test_reject_other_group(self, store, settings):
        profiles = FakeProfiles()
        profiles.entries_map["999"] = [{"id": 1, "category": "recent", "text": "别群的秘密", "evidence_count": 1, "last_ts": 1.0}]
        tools = _make(store, settings, profiles=profiles)
        r = await tools.call("read_profile", {"group_id": "999"}, _ctx(group_id="900000001"))
        assert r.ok is False
        assert "别群的秘密" not in str(store.read().execute("SELECT output FROM tool_calls ORDER BY id DESC LIMIT 1").fetchone()["output"])

    @pytest.mark.asyncio
    async def test_main_and_worker_can_read(self, store, settings):
        profiles = FakeProfiles()
        profiles.entries_map["900000001"] = []
        tools = _make(store, settings, profiles=profiles)
        for ctx in (_ctx(role="main", actor="主模型"), _ctx(role="worker", actor="子 agent #1")):
            r = await tools.call("read_profile", {}, ctx)
            assert r.ok, (ctx.actor, r.error)


class TestSubmitResult:
    @pytest.mark.asyncio
    async def test_submit_result_passes_through(self, store, settings):
        tools = _make(store, settings)
        r = await tools.call(
            "submit_result",
            {"summary": "找好了", "data": {"items": [1, 2]}, "evidence": ["https://a.com/1"]},
            _ctx(),
        )
        assert r.ok
        assert r.data["summary"] == "找好了"
        assert r.data["data"] == {"items": [1, 2]}
        assert r.data["evidence"] == ["https://a.com/1"]

    @pytest.mark.asyncio
    async def test_submit_data_as_json_string_is_parsed(self, store, settings):
        """线上实测（2026-09-28，step-5-preview）：模型把 data 写成 JSON 字符串交回，
        以前原样透传 → 调用方判「格式不对」整轮作废。现在字符串能解析成对象就当对象用。"""
        tools = _make(store, settings)
        r = await tools.call(
            "submit_result",
            {"summary": "找好了", "data": '{"items": [{"title": "t"}]}', "evidence": '["https://a.com/1"]'},
            _ctx(),
        )
        assert r.ok
        assert r.data["data"] == {"items": [{"title": "t"}]}
        assert r.data["evidence"] == ["https://a.com/1"]

    @pytest.mark.asyncio
    async def test_submit_data_broken_string_asks_model_to_resubmit(self, store, settings):
        """字符串解析不出来（比如被截断）→ 交回失败，报错告诉模型改成对象重交，而不是悄悄吞掉。"""
        tools = _make(store, settings)
        r = await tools.call("submit_result", {"summary": "找好了", "data": '{"items": [{"title": "t"'}, _ctx())
        assert r.ok is False
        assert "data" in (r.error or "")

    @pytest.mark.asyncio
    async def test_submit_needs_summary(self, store, settings):
        tools = _make(store, settings)
        r = await tools.call("submit_result", {}, _ctx())
        assert r.ok is False

    @pytest.mark.asyncio
    async def test_worker_only(self, store, settings):
        tools = _make(store, settings)
        r = await tools.call("submit_result", {"summary": "x"}, _ctx(role="main", actor="主模型"))
        assert r.ok is False


class TestSpecsComplete:
    def test_builtin_tools_registered(self, store, settings):
        tools = _make(store, settings)
        worker_specs = {s["function"]["name"] for s in tools.specs("worker")}
        assert {"web_search", "fetch_page", "read_profile", "submit_result"} <= worker_specs
        main_specs = {s["function"]["name"] for s in tools.specs("main")}
        assert "read_profile" in main_specs
        assert "web_search" not in main_specs


class _ExtractSearch(FakeSearch):
    def __init__(self, text="", error=None):
        super().__init__()
        self.text = text
        self.extract_error = error
        self.extract_calls = []

    async def extract(self, url):
        self.extract_calls.append(url)
        if self.extract_error is not None:
            raise self.extract_error
        return self.text


class TestFetchPageExtractFallback:
    """线上实测（2026-09-27）：资讯备料时约三分之一原文页 403（nintendolife、reddit…），
    子 agent 打不开就交不了「原文依据」。普通抓取被拦时改用搜索服务的正文抽取（Tavily extract）。"""

    @pytest.mark.asyncio
    async def test_403_falls_back_to_extract(self, store, settings):
        def handler(request):
            return httpx.Response(403, text="forbidden")

        s = _ExtractSearch(text="Metroid Dread 实体版涨到 100 美元以上")
        tools = _make(store, settings, search=s, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "https://www.nintendolife.com/news/x"}, _ctx())
        assert r.ok
        assert "100 美元" in r.output
        assert s.extract_calls == ["https://www.nintendolife.com/news/x"]
        assert r.data.get("via") == "extract"

    @pytest.mark.asyncio
    async def test_private_address_never_falls_back(self, store, settings):
        s = _ExtractSearch(text="不该出现")
        tools = _make(store, settings, search=s, resolver=lambda host: ["10.0.0.5"])
        r = await tools.call("fetch_page", {"url": "http://intranet.example/x"}, _ctx())
        assert not r.ok
        assert s.extract_calls == []

    @pytest.mark.asyncio
    async def test_extract_also_fails_keeps_original_error(self, store, settings):
        def handler(request):
            return httpx.Response(403, text="forbidden")

        s = _ExtractSearch(error=RuntimeError("抽取也失败"))
        tools = _make(store, settings, search=s, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "https://a.example/x"}, _ctx())
        assert not r.ok and "403" in r.error

    @pytest.mark.asyncio
    async def test_search_without_extract_unchanged(self, store, settings):
        def handler(request):
            return httpx.Response(403, text="forbidden")

        tools = _make(store, settings, transport=httpx.MockTransport(handler))
        r = await tools.call("fetch_page", {"url": "https://a.example/x"}, _ctx())
        assert not r.ok and "403" in r.error


# ----------------------------------------------------------------------
# 打开网页的顺序（2026-09-28 用户定）：Jina Reader → 抓网页正文工具 → 服务器直接打开
# ----------------------------------------------------------------------

from CharTyr_MaiWork.maiwork.reader import ReadResult


class _FakeReader:
    def __init__(self, result: ReadResult | None = None, enabled: bool = True):
        self.result = result or ReadResult(True, text="《Jina 读到的标题》\n Jina 读到的正文", final_url="")
        self._enabled = enabled
        self.calls: list[str] = []

    def enabled(self) -> bool:
        return self._enabled

    async def read(self, url):
        self.calls.append(url)
        return self.result


def _make_r(store, settings, *, reader, search=None, transport=None, resolver=None):
    tools = Tools(store)
    register_builtin(
        tools,
        search=search if search is not None else FakeSearch(),
        profiles=FakeProfiles(),
        http_transport=transport,
        get_settings=lambda: settings,
        resolver=resolver or _public_dns,
        reader=reader,
    )
    return tools


class _Direct:
    """假「直接打开」：记下有没有被打开过。"""

    def __init__(self, status=200, text="<html><head><title>直接打开的</title></head><body><p>直接打开读到的正文</p></body></html>"):
        self.hits = 0
        self.status = status
        self.text = text

    def __call__(self, request):
        self.hits += 1
        return httpx.Response(self.status, text=self.text, headers={"content-type": "text/html; charset=utf-8"})


class TestOpenOrder:
    @pytest.mark.asyncio
    async def test_jina_first(self, store, settings):
        rd, direct, s = _FakeReader(), _Direct(), _ExtractSearch(text="抓正文工具的")
        tools = _make_r(store, settings, reader=rd, search=s, transport=httpx.MockTransport(direct))
        r = await tools.call("fetch_page", {"url": "https://news.example.com/a"}, _ctx())
        assert r.ok and "Jina 读到的正文" in r.output
        assert r.data["via"] == "jina"
        assert rd.calls == ["https://news.example.com/a"]
        assert s.extract_calls == [] and direct.hits == 0

    @pytest.mark.asyncio
    async def test_jina_problem_then_extract_tool(self, store, settings):
        rd = _FakeReader(ReadResult(False, reason="Jina Reader 限流了"))
        direct, s = _Direct(), _ExtractSearch(text="抓正文工具读到的正文")
        tools = _make_r(store, settings, reader=rd, search=s, transport=httpx.MockTransport(direct))
        r = await tools.call("fetch_page", {"url": "https://news.example.com/a"}, _ctx())
        assert r.ok and "抓正文工具读到的正文" in r.output
        assert r.data["via"] == "extract"
        assert direct.hits == 0

    @pytest.mark.asyncio
    async def test_no_extract_tool_then_direct(self, store, settings):
        rd = _FakeReader(ReadResult(False, reason="读到的是验证 / 拦截页，不是正文"))
        direct = _Direct()
        tools = _make_r(store, settings, reader=rd, transport=httpx.MockTransport(direct))
        r = await tools.call("fetch_page", {"url": "https://zhuanlan.example.com/p/1"}, _ctx())
        assert r.ok and "直接打开读到的正文" in r.output
        assert r.data["via"] == "direct"

    @pytest.mark.asyncio
    async def test_extract_empty_then_direct(self, store, settings):
        rd = _FakeReader(ReadResult(False, reason="Jina Reader 超时"))
        direct, s = _Direct(), _ExtractSearch(text="")
        tools = _make_r(store, settings, reader=rd, search=s, transport=httpx.MockTransport(direct))
        r = await tools.call("fetch_page", {"url": "https://news.example.com/a"}, _ctx())
        assert r.ok and r.data["via"] == "direct"
        assert s.extract_calls == ["https://news.example.com/a"]

    @pytest.mark.asyncio
    async def test_all_fail_says_why_for_each(self, store, settings):
        rd = _FakeReader(ReadResult(False, reason="Jina Reader 限流了"))
        s = _ExtractSearch(error=RuntimeError("抽取失败（You）：连不上"))
        tools = _make_r(store, settings, reader=rd, search=s, transport=httpx.MockTransport(_Direct(status=403)))
        r = await tools.call("fetch_page", {"url": "https://news.example.com/a"}, _ctx())
        assert not r.ok
        assert "Jina" in r.error and "限流" in r.error
        assert "抓正文" in r.error
        assert "403" in r.error

    @pytest.mark.asyncio
    async def test_private_address_never_sent_to_jina(self, store, settings):
        rd, s = _FakeReader(), _ExtractSearch(text="不该出现")
        tools = _make_r(store, settings, reader=rd, search=s, resolver=lambda host: ["10.0.0.5"])
        r = await tools.call("fetch_page", {"url": "http://intranet.example/x"}, _ctx())
        assert not r.ok
        assert rd.calls == [] and s.extract_calls == []

    @pytest.mark.asyncio
    async def test_jina_off_keeps_old_order(self, store, settings):
        rd, direct = _FakeReader(enabled=False), _Direct()
        tools = _make_r(store, settings, reader=rd, transport=httpx.MockTransport(direct))
        r = await tools.call("fetch_page", {"url": "https://news.example.com/a"}, _ctx())
        assert r.ok and r.data["via"] == "direct"
        assert rd.calls == []

    @pytest.mark.asyncio
    async def test_log_line_says_which_way(self, store, settings):
        """请求日志里的摘要要看得出是哪条路打开的（排查用），最终地址标记照旧能解析。"""
        from CharTyr_MaiWork.maiwork.tools_builtin import final_url_from_summary

        rd = _FakeReader(ReadResult(True, text="《t》\n正文" * 20, final_url="https://news.example.com/a?final=1"))
        tools = _make_r(store, settings, reader=rd)
        await tools.call("fetch_page", {"url": "https://news.example.com/a"}, _ctx())
        row = store.read().execute("SELECT output FROM tool_calls WHERE tool='fetch_page' ORDER BY id DESC LIMIT 1").fetchone()
        assert "Jina" in row["output"]
        assert final_url_from_summary(row["output"]) == "https://news.example.com/a?final=1"

    def test_timeout_leaves_room_for_three_ways(self, store, settings):
        tools = _make_r(store, settings, reader=_FakeReader())
        assert tools.get("fetch_page", "worker").timeout_s >= 60


class TestOpenedUrlsFromRows:
    """验收引用核对 / 资讯补打开共用的「打开记录」解析（2026-10 整改：认扩展的抓正文工具）。

    线上事实：子 agent 实际用扩展的抓正文工具 mcp_keenable_fetch_page_content 成功
    打开了 31 次（input 是 JSON {"url": "https://..."}，output 开头有
    「Title: …\\nURL: <最终地址>」），全被当成「没打开过」→ 验收判「37 条链接
    27 条没打开过」→ 失败。
    """

    def test_fetch_page_json_input_url_counts(self):
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{"tool": "fetch_page", "input": '{"url": "https://a.example/x"}',
                 "output": "取到正文 500 字", "ok": 1}]
        opened = opened_urls_from_rows(rows)
        assert "a.example/x" in opened

    def test_fetch_page_plain_text_input_counts(self):
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{"tool": "fetch_page", "input": "https://a.example/y",
                 "output": "取到正文", "ok": 1}]
        opened = opened_urls_from_rows(rows)
        assert "a.example/y" in opened

    def test_mcp_extract_tool_json_input_counts(self):
        """mcp_keenable_fetch_page_content：JSON input 取 url；output 里「URL: <最终地址>」也算。"""
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{
            "tool": "mcp_keenable_fetch_page_content",
            "input": '{"url": "https://b.example/story"}',
            "output": "Title: 童年诡事录后续\nURL: https://b.example/story?full=1\n\n正文……",
            "ok": 1,
        }]
        opened = opened_urls_from_rows(rows)
        assert "b.example/story" in opened
        assert "b.example/story?full=1" in opened

    def test_mcp_extract_tool_urls_list_and_link_key(self):
        """input 兼容 urls 列表和 link 键。"""
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{
            "tool": "mcp_x_crawl",
            "input": '{"urls": ["https://c.example/1", "https://c.example/2"]}',
            "output": "抓好了 2 条", "ok": 1,
        }, {
            "tool": "mcp_y_scrape",
            "input": '{"link": "https://d.example/3"}',
            "output": "Title: t\nURL: https://d.example/3", "ok": 1,
        }]
        opened = opened_urls_from_rows(rows)
        assert "c.example/1" in opened
        assert "c.example/2" in opened
        assert "d.example/3" in opened

    def test_search_tool_does_not_count(self):
        """搜索类工具（web_search、mcp_xxx_search_web_pages）一律不算打开过。"""
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{
            "tool": "mcp_keenable_search_web_pages",
            "input": '{"query": "后续动态"}',
            "output": "1. 童年诡事录 https://e.example/never", "ok": 1,
        }, {
            "tool": "web_search",
            "input": "搜", "output": "https://e.example/never2", "ok": 1,
        }, {
            "tool": "mcp_z_lookup_query",
            "input": '{"url": "https://e.example/never3"}',
            "output": "Title: x", "ok": 1,
        }]
        opened = opened_urls_from_rows(rows)
        assert not opened

    def test_failed_mcp_call_does_not_count(self):
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{
            "tool": "mcp_keenable_fetch_page_content",
            "input": '{"url": "https://f.example/fail"}',
            "output": "", "ok": 0,
        }]
        assert not opened_urls_from_rows(rows)

    def test_final_url_label_in_output_counts(self):
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{
            "tool": "fetch_page",
            "input": "http://short.example/s",
            "output": "取到正文 500 字（Jina）（最终地址：https://long.example/very/long?a=1）",
            "ok": 1,
        }]
        opened = opened_urls_from_rows(rows)
        assert "short.example/s" in opened
        assert "long.example/very/long?a=1" in opened

    def test_non_extract_mcp_tool_ignored(self):
        """mcp_ 开头但不像抓正文也不像搜索的（例如图片生成）不算。"""
        from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows

        rows = [{
            "tool": "mcp_img_generate",
            "input": '{"url": "https://g.example/nope"}',
            "output": "好了", "ok": 1,
        }]
        assert not opened_urls_from_rows(rows)
