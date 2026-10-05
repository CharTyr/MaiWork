"""取源安全地跟随少量重定向（2026-10-05 用户：「安全的跟随少量跳转」）。

线上首轮来源地图 27 个候选只过了 2 个，最多的失败是网站把地址 301/302 到别处
（nintendo.com → www.nintendo.com 之类），而原来取源一律不跟随重定向。
现在：最多跟 3 跳；每一跳都重新做公网校验（字面 IP / localhost / 用户名密码 / 协议）
并在生产里重新解析 DNS、固定连到审过的 IP；全程共用一个总时限和字节上限。
"""
from __future__ import annotations

import socket

import httpx
import pytest

from CharTyr_MaiWork.maiwork import auto_sources, rss

NOW = 1_790_000_000.0
FEED = '<?xml version="1.0"?><rss version="2.0"><channel><title>终点</title>' \
       '<item><title>一篇</title><link>https://www.example.com/p/1</link>' \
       '<pubDate>Mon, 28 Sep 2026 00:00:00 +0000</pubDate></item></channel></rss>'


def _chain(routes: dict[str, httpx.Response], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        hit = routes.get(str(request.url))
        return hit if hit is not None else httpx.Response(404)
    return httpx.MockTransport(handler)


def _to(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


@pytest.mark.asyncio
async def test_follows_public_redirect_and_reports_final_url():
    seen: list[str] = []
    t = _chain({
        "https://example.com/feed": _to("https://www.example.com/feed"),
        "https://www.example.com/feed": httpx.Response(200, text=FEED),
    }, seen)
    got = await rss.fetch_bytes("https://example.com/feed", transport=t)
    assert got["error"] == "" and got["status"] == 200
    assert got["url"] == "https://www.example.com/feed"
    assert seen == ["https://example.com/feed", "https://www.example.com/feed"]


@pytest.mark.asyncio
async def test_relative_location_resolved_against_current_url():
    seen: list[str] = []
    t = _chain({
        "https://example.com/rss": _to("/feed.xml", 302),
        "https://example.com/feed.xml": httpx.Response(200, text=FEED),
    }, seen)
    out = await rss.fetch_feed_source("https://example.com/rss", transport=t, now=NOW, lookback_days=30)
    assert out["error"] == "" and out["title"] == "终点"
    assert out["url"] == "https://example.com/feed.xml"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_all_redirect_codes_followed(status):
    seen: list[str] = []
    t = _chain({
        "https://a.example.com/x": _to("https://b.example.com/x", status),
        "https://b.example.com/x": httpx.Response(200, text="ok"),
    }, seen)
    got = await rss.fetch_bytes("https://a.example.com/x", transport=t)
    assert got["error"] == "" and got["body"] == b"ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    "http://127.0.0.1/internal", "http://169.254.169.254/latest/meta-data/", "http://[::1]/x",
    "http://localhost/x", "http://10.0.0.2/x", "http://user:pw@public.example.com/x",
    "file:///etc/passwd", "ftp://public.example.com/x", "gopher://public.example.com/x",
])
async def test_redirect_to_unsafe_target_is_never_requested(target):
    seen: list[str] = []
    t = _chain({"https://news.example.com/feed": _to(target, 302)}, seen)
    got = await rss.fetch_bytes("https://news.example.com/feed", transport=t)
    assert seen == ["https://news.example.com/feed"]
    assert got["body"] is None and got["error"]
    assert "跳转" in got["error"]


@pytest.mark.asyncio
async def test_too_many_hops_stops_after_three_follows():
    seen: list[str] = []
    routes = {f"https://h{i}.example.com/": _to(f"https://h{i + 1}.example.com/") for i in range(10)}
    got = await rss.fetch_bytes("https://h0.example.com/", transport=_chain(routes, seen))
    assert len(seen) == rss._MAX_REDIRECTS + 1 == 4
    assert got["body"] is None and "跳转" in got["error"]


@pytest.mark.asyncio
async def test_redirect_loop_rejected():
    seen: list[str] = []
    t = _chain({
        "https://a.example.com/": _to("https://b.example.com/"),
        "https://b.example.com/": _to("https://a.example.com/"),
    }, seen)
    got = await rss.fetch_bytes("https://a.example.com/", transport=t)
    assert got["body"] is None and "跳转" in got["error"]
    assert len(seen) <= rss._MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_redirect_without_location_is_plain_failure():
    seen: list[str] = []
    got = await rss.fetch_bytes(
        "https://a.example.com/", transport=_chain({"https://a.example.com/": httpx.Response(302)}, seen)
    )
    assert got["body"] is None and "302" in got["error"]


@pytest.mark.asyncio
async def test_production_rechecks_dns_and_pins_ip_on_every_hop(monkeypatch):
    """生产：第二跳的域名也重新解析、只连审过的 IP；第二跳解析到内网 → 不发第二个请求。"""
    answers = {"good.example.com": "93.184.215.14", "www.good.example.com": "93.184.215.15",
               "evil.example.com": "10.0.0.8"}
    seen: list[httpx.Request] = []

    def fake_dns(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    def fake_transport(*a, **k):
        assert k.get("trust_env") is False

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            host = request.headers["host"]
            if host == "good.example.com":
                return _to("https://www.good.example.com/feed")
            if host == "www.good.example.com":
                return _to("https://evil.example.com/feed", 302)
            return httpx.Response(200, text="should not get here")
        return httpx.MockTransport(handler)

    monkeypatch.setattr(rss.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(rss.httpx, "AsyncHTTPTransport", fake_transport)
    got = await rss.fetch_bytes("https://good.example.com/feed")
    assert [r.url.host for r in seen] == ["93.184.215.14", "93.184.215.15"]
    assert seen[1].headers["host"] == "www.good.example.com"
    assert seen[1].extensions["sni_hostname"] == "www.good.example.com"
    assert got["body"] is None and got["error"]


@pytest.mark.asyncio
async def test_total_time_limit_covers_all_hops():
    import asyncio

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return _to(str(request.url).replace("/a", "/b") if request.url.path == "/a" else "https://x.example.com/c")

    got = await rss.fetch_bytes("https://x.example.com/a", transport=httpx.MockTransport(slow), timeout_s=0.5)
    assert got["body"] is None and "超时" in got["error"]


@pytest.mark.asyncio
async def test_discover_feed_records_final_feed_url_and_uses_final_home_as_base():
    """来源地图：nintendo.com → www.nintendo.com 这类跳转后能找到 RSS，并存终点地址。"""
    home_html = '<html><head><link rel="alternate" type="application/rss+xml" href="/news/feed"></head></html>'
    seen: list[str] = []
    t = _chain({
        "https://example.com/": _to("https://www.example.com/"),
        "https://www.example.com/": httpx.Response(200, text=home_html),
        "https://www.example.com/news/feed": _to("https://www.example.com/news/feed.xml", 308),
        "https://www.example.com/news/feed.xml": httpx.Response(200, text=FEED),
    }, seen)
    got = await auto_sources.discover_feed("example.com", transport=t, now=NOW)
    assert got["error"] == ""
    assert got["url"] == "https://www.example.com/news/feed.xml"
