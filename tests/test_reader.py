"""reader.py 测试：Jina Reader（https://r.jina.ai）作为打开网页的首选路。

用 httpx.MockTransport 假装 Jina，不碰真实网络。要点：
- 成功：正文带标题、发布时间；
- 「有问题」一律算失败让调用方马上换路：HTTP 错、429 限流（之后冷却一分钟不再请求）、
  401/402 密钥不对/额度用完（冷却更久）、原网页 4xx/5xx、验证页（知乎「安全验证」实测）、空正文；
- 本地限速：不带 key 每分钟最多 18 次（官方 20），带 key 450 次（官方 500），用满直接失败不排队；
- 密钥只放在 Authorization 头里，不出现在任何返回文字里。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.reader import JinaReader

KEY = "jina_SECRET_very_visible_key_123"
URL = "https://news.example.com/a/1.html"


def _settings(**reader):
    s, problems = load_settings({"reader": reader} if reader else {})
    assert problems == []
    return s


def _ok_payload(content="正文" * 100, title="一篇新闻", **extra):
    data = {"title": title, "url": URL, "content": content, "httpStatus": 200}
    data.update(extra)
    return {"code": 200, "status": 20000, "data": data}


class _Jina:
    """假 Jina：按队列回包，记下每次请求。"""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else (200, _ok_payload())
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body) if not isinstance(body, str) else httpx.Response(status, text=body)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _reader(jina: _Jina, clock: _Clock | None = None, **reader_cfg) -> JinaReader:
    settings = _settings(**reader_cfg)
    return JinaReader(lambda: settings, transport=httpx.MockTransport(jina), now=clock or _Clock())


class TestConfig:
    def test_defaults_on_without_key(self) -> None:
        s = _settings()
        assert s.reader.jina_enabled is True
        assert s.reader.jina_api_key == ""

    def test_can_turn_off_and_set_key(self) -> None:
        s = _settings(jina_enabled=False, jina_api_key=KEY)
        assert s.reader.jina_enabled is False
        assert s.reader.jina_api_key == KEY


class TestRead:
    @pytest.mark.asyncio
    async def test_ok_returns_title_time_and_body(self) -> None:
        jina = _Jina((200, _ok_payload(publishedTime="2026-09-28T09:00:12+08:00",
                                        metadata={"og:image": "https://img.example.com/c.jpg"})))
        r = await _reader(jina).read(URL)
        assert r.ok, r.reason
        assert r.text.startswith("《一篇新闻》")
        assert "2026-09-28T09:00:12+08:00" in r.text
        assert "正文正文" in r.text
        assert r.image_url == "https://img.example.com/c.jpg"
        assert r.final_url == URL
        req = jina.requests[0]
        assert str(req.url) == "https://r.jina.ai/" + URL
        assert req.headers["accept"] == "application/json"
        # 不带 key 时不发 Authorization；也不让 Jina 死等（不传 X-Timeout）
        assert "authorization" not in req.headers
        assert "x-timeout" not in req.headers

    @pytest.mark.asyncio
    async def test_long_body_truncated(self) -> None:
        jina = _Jina((200, _ok_payload(content="字" * 20000)))
        r = await _reader(jina).read(URL)
        assert r.ok
        assert len(r.text) < 8200
        assert "已截断" in r.text

    @pytest.mark.asyncio
    async def test_key_goes_in_header_only(self) -> None:
        jina = _Jina((500, {"code": 500, "message": f"server echoed {KEY}"}))
        r = await _reader(jina, jina_api_key=KEY).read(URL)
        assert jina.requests[0].headers["authorization"] == f"Bearer {KEY}"
        assert not r.ok
        assert KEY not in r.reason

    @pytest.mark.asyncio
    async def test_disabled_does_not_call(self) -> None:
        jina = _Jina()
        r = await _reader(jina, jina_enabled=False).read(URL)
        assert not r.ok
        assert jina.requests == []


class TestProblemsMeanFallBack:
    @pytest.mark.asyncio
    async def test_captcha_page_is_failure(self) -> None:
        """知乎实测：Jina 返回 200，但读到的是「安全验证 - 知乎」页。"""
        jina = _Jina((200, _ok_payload(title="安全验证 - 知乎", content="系统监测到您的网络环境存在异常，请点击下方验证按钮进行验证")))
        r = await _reader(jina).read(URL)
        assert not r.ok
        assert "验证" in r.reason

    @pytest.mark.asyncio
    async def test_empty_body_is_failure(self) -> None:
        """Reddit 实测：200 但正文空。"""
        jina = _Jina((200, _ok_payload(title="", content="  ")))
        r = await _reader(jina).read(URL)
        assert not r.ok
        assert "空" in r.reason

    @pytest.mark.asyncio
    async def test_origin_error_status_is_failure(self) -> None:
        jina = _Jina((200, _ok_payload(httpStatus=404, title="404 Not Found", content="页面不存在" * 30)))
        r = await _reader(jina).read(URL)
        assert not r.ok

    @pytest.mark.asyncio
    async def test_anonymous_blocked_site_is_failure(self) -> None:
        """腾讯新闻实测：不带 key 时 Jina 回 403 AbuseAlleviationError。"""
        jina = _Jina((403, {"data": None, "code": 403, "name": "AbuseAlleviationError", "message": "Anonymous access to domain news.qq.com blocked"}))
        r = await _reader(jina).read(URL)
        assert not r.ok
        assert "403" in r.reason or "拒绝" in r.reason

    @pytest.mark.asyncio
    async def test_network_error_is_failure(self) -> None:
        jina = _Jina(httpx.ConnectTimeout("timed out"))
        r = await _reader(jina).read(URL)
        assert not r.ok
        assert r.reason


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_429_then_cooldown_without_calling(self) -> None:
        clock = _Clock()
        jina = _Jina((429, {"code": 429, "message": "rate limit"}))
        rd = _reader(jina, clock)
        r1 = await rd.read(URL)
        assert not r1.ok and "限流" in r1.reason
        # 冷却期内直接失败、不再请求 Jina（马上换路，不白等）
        r2 = await rd.read(URL)
        assert not r2.ok
        assert len(jina.requests) == 1
        # 一分钟后恢复
        clock.t += 61
        r3 = await rd.read(URL)
        assert r3.ok
        assert len(jina.requests) == 2

    @pytest.mark.asyncio
    async def test_bad_key_cools_down_longer(self) -> None:
        clock = _Clock()
        jina = _Jina((401, {"code": 401, "message": "invalid key"}))
        rd = _reader(jina, clock, jina_api_key=KEY)
        assert not (await rd.read(URL)).ok
        clock.t += 61
        assert not (await rd.read(URL)).ok
        assert len(jina.requests) == 1  # 还在冷却
        ok, text = rd.status()
        assert not ok and "密钥" in text

    @pytest.mark.asyncio
    async def test_local_limit_without_key(self) -> None:
        clock = _Clock()
        jina = _Jina(*[(200, _ok_payload())] * 30)
        rd = _reader(jina, clock)
        results = [await rd.read(URL) for _ in range(20)]
        assert sum(r.ok for r in results) == 18
        assert "用满" in results[-1].reason
        assert len(jina.requests) == 18
        clock.t += 61
        assert (await rd.read(URL)).ok

    @pytest.mark.asyncio
    async def test_key_raises_local_limit(self) -> None:
        jina = _Jina(*[(200, _ok_payload())] * 40)
        rd = _reader(jina, jina_api_key=KEY)
        results = [await rd.read(URL) for _ in range(30)]
        assert all(r.ok for r in results)


class TestBadKey:
    @pytest.mark.asyncio
    async def test_non_ascii_key_fails_cleanly(self) -> None:
        """密钥里混进中文（复制时带进来的）：HTTP 头放不下，不能抛异常，直接算失败换路。"""
        jina = _Jina()
        r = await _reader(jina, jina_api_key="jina_abc，").read(URL)
        assert not r.ok and "密钥" in r.reason
        assert jina.requests == []


class TestStatus:
    def test_status_texts(self) -> None:
        ok, text = _reader(_Jina()).status()
        assert ok and "Jina Reader" in text and "20" in text
        ok, text = _reader(_Jina(), jina_api_key=KEY).status()
        assert ok and "500" in text and KEY not in text
        ok, text = _reader(_Jina(), jina_enabled=False).status()
        assert not ok and "关" in text


class TestHealthRow:
    """网页「运行状态」多一行「打开网页」：写清楚三条路现在的样子。"""

    def _svc(self, reader, extract=(True, "You 的 you-contents")):
        class _S:
            def extract_available(self):
                return extract

        return SimpleNamespace(reader=reader, search=_S())

    def test_row_lists_the_order(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import _reader_health

        row = _reader_health(self._svc(_reader(_Jina())))
        assert row["key"] == "reader" and row["state"] == "ok"
        t = row["text"]
        assert t.index("Jina") < t.index("You 的 you-contents") < t.index("直接打开")

    def test_row_warns_when_cooling(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import _reader_health

        rd = _reader(_Jina())
        rd._cool(60, "被限流")
        row = _reader_health(self._svc(rd))
        assert row["state"] == "warn" and "暂停" in row["text"]

    def test_row_without_extract_tool(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import _reader_health

        row = _reader_health(self._svc(_reader(_Jina()), extract=(False, "没选抓正文工具")))
        assert "没选抓正文工具" in row["text"]

    def test_row_when_jina_off(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import _reader_health

        row = _reader_health(self._svc(_reader(_Jina(), jina_enabled=False)))
        assert row["state"] == "ok"
        assert "直接打开" in row["text"] and "Jina" in row["text"]

    def test_app_wires_reader_into_fetch_page(self) -> None:
        """app 启动时建 JinaReader 并交给 register_builtin（源码级检查，防漏接线）。"""
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "maiwork" / "app.py").read_text(encoding="utf-8")
        assert "JinaReader(" in src
        assert "reader=self.reader" in src
        views = (Path(__file__).resolve().parent.parent / "maiwork" / "console" / "views.py").read_text(encoding="utf-8")
        assert "_reader_health(svc)" in views
