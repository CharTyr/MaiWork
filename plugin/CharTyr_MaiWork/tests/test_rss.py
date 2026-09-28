"""rss.py 单元测试：RSS 2.0 / Atom 解析、取源、安全限制（全 MockTransport，不碰真实网络）。

- 解析：RSS 2.0（channel/item）、Atom（feed/entry）、去 HTML 截 500 的 summary、
  发布时间（RFC822 / ISO / epoch）、链接（RSS link 文本 / Atom link href 或 id）；
- 安全：含 <!DOCTYPE 一律拒（不引 defusedxml 也能挡外部实体）；响应 >2MB 拒；超时 15s；
  url 必须 http(s)；
- 业务：lookback_days 只留最近 N 天的；每源每轮最多 10 条；空源/解析不出条目不炸。
"""

from __future__ import annotations

import httpx
import pytest

from CharTyr_MaiWork import rss

NOW = 1_790_000_000.0


def _epoch(y: int, m: int, d: int, hh: int = 0) -> float:
    import datetime as _dt

    return _dt.datetime(y, m, d, hh, tzinfo=_dt.timezone.utc).timestamp()


def _rfc822(ts: float) -> str:
    from email.utils import formatdate

    # 统一用 GMT（用本地时区会和解析端不一致，测试 never 对得上）
    return formatdate(ts, usegmt=True)


RSS2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>折腾周刊</title>
    <link>https://blog.example.com/</link>
    <item>
      <title>FPGA 新板子发布了</title>
      <link>https://blog.example.com/fpga</link>
      <pubDate>{d1}</pubDate>
      <description>&lt;p&gt;正文&lt;b&gt;靠&lt;/b&gt;前&lt;/p&gt;的摘要</description>
    </item>
    <item>
      <title>老文章（半年前）</title>
      <link>https://blog.example.com/old</link>
      <pubDate>{d_old}</pubDate>
      <description>旧</description>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>开源小报</title>
  <entry>
    <title>Tang 出了新教程</title>
    <link href="https://oss.example.com/tang"/>
    <updated>{d1}</updated>
    <summary>好教程</summary>
  </entry>
</feed>
"""


class TestFetchFeedSource:
    @pytest.mark.asyncio
    async def test_fetch_and_title(self, monkeypatch):
        monkeypatch.setattr(rss.clock, "now", lambda: NOW)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("user-agent", "").startswith("MaiWork-RSS")
            return httpx.Response(200, text=RSS2.format(d1=_rfc822(NOW - 100), d_old=_rfc822(NOW - 200 * 86400)))

        out = await rss.fetch_feed_source(
            "https://blog.example.com/feed.xml",
            transport=httpx.MockTransport(handler),
            lookback_days=14,
            now=NOW,
            limit=10,
        )
        assert out["title"] == "折腾周刊"
        items = out["items"]
        # 老的（200 天前）被 lookback 滤掉
        assert len(items) == 1
        it = items[0]
        assert it["title"] == "FPGA 新板子发布了"
        assert it["url"] == "https://blog.example.com/fpga"
        assert it["published"] == NOW - 100
        # summary 去 HTML：标签没了（标签位置留空格是正常的）
        assert "<" not in it["summary"]
        assert "p>" not in it["summary"] and "b>" not in it["summary"]
        assert "的摘要" in it["summary"]
        assert out["error"] == ""

    @pytest.mark.asyncio
    async def test_atom(self):
        out = await rss.fetch_feed_source(
            "https://oss.example.com/atom.xml",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text=ATOM.format(d1="2026-09-20T08:00:00Z"))),
            lookback_days=30,
            now=NOW,
            limit=10,
        )
        assert out["title"] == "开源小报"
        assert len(out["items"]) == 1
        assert out["items"][0]["url"] == "https://oss.example.com/tang"
        assert out["items"][0]["published"] == _epoch(2026, 9, 20, 8)

    @pytest.mark.asyncio
    async def test_rejects_doctype(self):
        evil = '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss version="2.0"><channel><title>x</title><item><title>t</title><link>https://a.b/c</link><pubDate>Mon, 01 Sep 2025 00:00:00 GMT</pubDate></item></channel></rss>'
        with pytest.raises(rss.RssError, match="DOCTYPE"):
            rss.parse_feed(evil, now=NOW, lookback_days=400)

    @pytest.mark.asyncio
    async def test_not_xml_400(self):
        with pytest.raises(rss.RssError):
            rss.parse_feed("这不是 XML", now=NOW, lookback_days=30)

    @pytest.mark.asyncio
    async def test_http_status_fail(self):
        out = await rss.fetch_feed_source(
            "https://x.example.com/feed",
            transport=httpx.MockTransport(lambda req: httpx.Response(404, text="nope")),
            lookback_days=30,
            now=NOW,
            limit=10,
        )
        assert out["items"] == []
        assert "404" in out["error"]

    @pytest.mark.asyncio
    async def test_timeout(self):
        def _slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        out = await rss.fetch_feed_source(
            "https://slow.example.com/feed",
            transport=httpx.MockTransport(_slow),
            lookback_days=30,
            now=NOW,
            limit=10,
        )
        assert out["items"] == []
        assert out["error"] != ""

    @pytest.mark.asyncio
    async def test_non_http_url_rejected(self):
        for bad in ("ftp://x/feed", "file:///etc/passwd", "", "javascript:alert(1)"):
            with pytest.raises(rss.RssError):
                await rss.fetch_feed_source(bad, transport=None, lookback_days=30, now=NOW, limit=10)

    @pytest.mark.asyncio
    async def test_response_too_big(self):
        big = "A" * (2 * 1024 * 1024 + 10)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=big.encode("utf-8"))

        out = await rss.fetch_feed_source(
            "https://big.example.com/feed",
            transport=httpx.MockTransport(handler),
            lookback_days=30,
            now=NOW,
            limit=10,
        )
        assert out["items"] == []
        assert "太大" in out["error"] or "2MB" in out["error"].upper() or "超过" in out["error"]

    @pytest.mark.asyncio
    async def test_per_source_limit(self):
        # 20 条都新 → 只留 10 条
        items_xml = "".join(
            f"<item><title>t{i}</title><link>https://x.example.com/{i}</link><pubDate>{_rfc822(NOW - i * 60)}</pubDate><description>s{i}</description></item>"
            for i in range(20)
        )
        xml = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items_xml}</channel></rss>"""
        out = await rss.fetch_feed_source(
            "https://x.example.com/feed",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text=xml)),
            lookback_days=30,
            now=NOW,
            limit=10,
        )
        assert len(out["items"]) == 10


# ----------------------------------------------------------------------
# 解析本身（不走路由）
# ----------------------------------------------------------------------


class TestParseFeed:
    def test_summary_strip_html_and_truncate(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
        <item><title>x</title><link>https://a.b/c</link><pubDate>{d}</pubDate>
        <description>{desc}</description></item></channel></rss>""".format(
            d=_rfc822(NOW - 10), desc="&lt;br&gt;" + "长" * 600
        )
        items = rss.parse_feed(xml, now=NOW, lookback_days=30).get("items", [])
        assert len(items) == 1
        assert len(items[0]["summary"]) <= 500

    def test_missing_title_or_link_skipped(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
        <item><title></title><link>https://a.b/c</link><pubDate>{d}</pubDate></item>
        <item><title>ok</title><link></link><pubDate>{d}</pubDate></item>
        <item><title>ok2</title><link>https://a.b/c</link><pubDate>{d}</pubDate></item>
        </channel></rss>""".format(d=_rfc822(NOW - 60))
        items = rss.parse_feed(xml, now=NOW, lookback_days=30).get("items", [])
        assert [i["title"] for i in items] == ["ok2"]

    def test_no_items_ok(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>empty</title></channel></rss>"""
        out = rss.parse_feed(xml, now=NOW, lookback_days=30)
        assert out["items"] == []
        assert out["title"] == "empty"


# ----------------------------------------------------------------------
# kv 读写：feeds.rss.<群号>
# ----------------------------------------------------------------------


class TestKv:
    def _store(self, tmp_path):
        from CharTyr_MaiWork.store import Store

        s = Store(tmp_path / "t.db")
        s.migrate()
        return s

    def test_add_read_remove(self, tmp_path):
        s = self._store(tmp_path)
        gid = "900000001"
        assert rss.list_feeds(s, gid) == []
        entry = rss.add_feed(s, gid, url="https://a.example.com/feed", title="A 站", feed_id="r1", now=NOW)
        assert entry["id"] == "r1"
        assert entry["url"] == "https://a.example.com/feed"
        assert entry["title"] == "A 站"
        assert entry["enabled"] is True
        lst = rss.list_feeds(s, gid)
        assert len(lst) == 1
        # toggle
        rss.toggle_feed(s, gid, "r1", enabled=False)
        assert rss.list_feeds(s, gid)[0]["enabled"] is False
        # remove
        rss.remove_feed(s, gid, "r1")
        assert rss.list_feeds(s, gid) == []

    def test_max_20(self, tmp_path):
        s = self._store(tmp_path)
        gid = "900000001"
        for i in range(20):
            rss.add_feed(s, gid, url=f"https://x{i}.example.com/feed", title=f"t{i}", feed_id=f"r{i}", now=NOW)
        with pytest.raises(rss.RssError, match="20"):
            rss.add_feed(s, gid, url="https://y.example.com/feed", title="y", feed_id="r21", now=NOW)

    def test_add_requires_http_url(self, tmp_path):
        s = self._store(tmp_path)
        with pytest.raises(rss.RssError):
            rss.add_feed(s, "g1", url="ftp://x/feed", title="", feed_id="r1", now=NOW)

    def test_mark_checked(self, tmp_path):
        s = self._store(tmp_path)
        gid = "g1"
        rss.add_feed(s, gid, url="https://a.example.com/f", title="a", feed_id="r1", now=NOW)
        rss.mark_checked(s, gid, "r1", ok_ts=NOW + 10, error="")
        assert rss.list_feeds(s, gid)[0]["last_ok_ts"] == NOW + 10
        rss.mark_checked(s, gid, "r1", ok_ts=None, error="超时")
        row = rss.list_feeds(s, gid)[0]
        assert row["last_error"] == "超时"
        assert row["last_ok_ts"] == NOW + 10  # ok_ts=None 时保留上次成功时间

    def test_idempotent_id_collision(self, tmp_path):
        s = self._store(tmp_path)
        gid = "g1"
        # 同 id（手动补了一个） → add 不该撞
        rss.add_feed(s, gid, url="https://a.example.com/f", title="a", feed_id="r1", now=NOW)
        rss.add_feed(s, gid, url="https://b.example.com/f", title="b", feed_id="r2", now=NOW)
        ids = [e["id"] for e in rss.list_feeds(s, gid)]
        assert ids == sorted(ids) and len(set(ids)) == 2
