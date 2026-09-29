"""资讯卡片渲染（maiwork/news_card.py）。"""

from __future__ import annotations

import builtins
import re

import pytest

from maiwork import news_card

NOW = 1790661349.6  # 2026-09-29 下午（本地时区测试机即可，slot_label 优先）


def _data(**kw):
    d = {
        "group_name": "单机群",
        "slot_label": "9月29日 · 下午",
        "slot_ts": NOW,
        "link": "https://mw.example.com/#/t/news",
        "total": 5,
        "items": [
            {"title": f"标题{i}", "summary": "摘要" * 10, "why": "值得看的理由", "site": "a.com",
             "published_ts": NOW - 7200, "icon": "joystick", "keywords": ["关键词"], "topic": "话题"}
            for i in range(1, 5)
        ],
    }
    d.update(kw)
    return d


def test_slot_word_prefers_label_then_clock():
    assert news_card.slot_word(None, "9月29日 · 晚上") == "晚上"
    assert news_card.slot_word(None, "x · 凌晨") == "凌晨"
    # 认不出的标签按钟点算，结果一定是五个时段之一
    assert news_card.slot_word(NOW, "乱写") in {"凌晨", "早上", "中午", "下午", "晚上"}


def test_at_most_three_items_and_counts():
    h = news_card.render_html(_data(), now=NOW)
    assert h.count('class="item') == 3
    assert "标题4" not in h
    assert "本期精选 <em>3</em> 条" in h and "共找到 5 条" in h
    assert "2 小时前" in h


def test_escapes_text():
    d = _data(items=[{"title": "<script>alert(1)</script>", "summary": "a&b", "why": "", "site": ""}])
    h = news_card.render_html(d, now=NOW)
    assert "<script>alert" not in h
    assert "&lt;script&gt;" in h


def test_no_external_resources():
    """卡片不许引用任何外部地址（图标内嵌 data URI）；链接只出现在消息文字里，不进图片。"""
    h = news_card.render_html(_data(), now=NOW)
    assert not re.search(r"(src|href)=\"https?:", h)
    assert "mw.example.com" not in h
    assert 'src="data:image/' in h


def test_unknown_icon_falls_back():
    d = _data(items=[{"title": "t", "icon": "../../etc/passwd"}])
    h = news_card.render_html(d, now=NOW)
    assert 'src="data:image/' in h


def test_footer_without_link():
    h = news_card.render_html(_data(link=""), now=NOW)
    assert "在 MaiWork 网页里看全部资讯" in h


@pytest.mark.asyncio
async def test_render_png_without_playwright_raises(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(news_card.RenderError):
        await news_card.render_png(_data())


@pytest.mark.asyncio
async def test_render_png_real_browser(monkeypatch):
    """本机装了 playwright + chromium 就真画一张（服务器宿主 venv 自带 playwright 1.58）。"""
    import os
    import pwd
    from pathlib import Path

    pytest.importorskip("playwright.async_api")
    # 测试夹具把 HOME 换成了临时目录；浏览器在真实家目录的缓存里
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    for cache in (real_home / "Library/Caches/ms-playwright", real_home / ".cache/ms-playwright"):
        if cache.is_dir():
            monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
            break
    else:
        pytest.skip("本机没装 playwright 浏览器")
    png = await news_card.render_png(_data())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 20_000


# ----------------------------------------------------------------------
# 配图
# ----------------------------------------------------------------------

def _png(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 80, 40)).save(buf, "PNG")
    return buf.getvalue()


def _public(host):  # 测试 DNS：全部解析成公网地址，除了 evil.internal-ish 的
    return ["10.0.0.5"] if host.startswith("intranet") else ["93.184.216.34"]


def _transport(routes: dict):
    import httpx

    def handler(request):
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404)
        ctype, body = routes[url]
        return httpx.Response(200, headers={"content-type": ctype}, content=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_cover_big_image_ok():
    pytest.importorskip("PIL")
    t = _transport({"https://img.example.com/a.jpg": ("image/png", _png(1600, 900))})
    uri, w = await news_card.fetch_cover("https://img.example.com/a.jpg", transport=t, resolver=_public)
    assert uri.startswith("data:image/jpeg;base64,") and w == 1600


@pytest.mark.asyncio
async def test_fetch_cover_rejects_tiny_svg_and_intranet():
    pytest.importorskip("PIL")
    t = _transport({
        "https://img.example.com/logo.png": ("image/png", _png(64, 64)),
        "https://img.example.com/x.svg": ("image/svg+xml", b"<svg/>"),
        "https://intranet.example.com/a.png": ("image/png", _png(1600, 900)),
    })
    for u in ("https://img.example.com/logo.png", "https://img.example.com/x.svg",
              "https://intranet.example.com/a.png", "file:///etc/passwd"):
        assert await news_card.fetch_cover(u, transport=t, resolver=_public) == ("", 0)


@pytest.mark.asyncio
async def test_fetch_cover_small_image_then_og_bigger():
    """image_url 只是 200×112 预览图 → 去原文找 og:image，大的胜出；找不到大的就用小的当缩略图。"""
    pytest.importorskip("PIL")
    page = b'<html><head><meta property="og:image" content="/big.jpg"></head></html>'
    t = _transport({
        "https://img.example.com/small.jpg": ("image/png", _png(200, 112)),
        "https://news.example.com/a": ("text/html; charset=utf-8", page),
        "https://news.example.com/big.jpg": ("image/png", _png(1280, 720)),
    })
    uri, w = await news_card.fetch_cover("https://img.example.com/small.jpg", "https://news.example.com/a",
                                         transport=t, resolver=_public)
    assert w == 1280 and uri
    t2 = _transport({"https://img.example.com/small.jpg": ("image/png", _png(200, 112))})
    uri, w = await news_card.fetch_cover("https://img.example.com/small.jpg", "https://news.example.com/404",
                                         transport=t2, resolver=_public)
    assert w == 200 and uri


@pytest.mark.asyncio
async def test_prepare_covers_uses_fetch_and_survives_errors():
    calls = []

    async def fake(img, url):
        calls.append((img, url))
        if img == "boom":
            raise RuntimeError("x")
        return ("data:image/jpeg;base64,AAAA", 900)

    d = _data(items=[{"title": "a", "image_url": "i1", "url": "u1"}, {"title": "b", "image_url": "boom"},
                     {"title": "c"}])
    await news_card.prepare_covers(d, fetch=fake)
    assert calls == [("i1", "u1"), ("boom", "")]
    assert d["items"][0]["cover_w"] == 900 and d["items"][1]["cover"] == "" and "cover" not in d["items"][2]


def test_layouts_with_cover():
    uri = "data:image/jpeg;base64,AAAA"
    d = _data(items=[{"title": "头条", "cover": uri, "cover_w": 1200},
                     {"title": "二条", "cover": uri, "cover_w": 200},
                     {"title": "三条", "cover": "https://evil.example.com/x.jpg", "cover_w": 1200}])
    h = news_card.render_html(d, now=NOW)
    assert h.count('class="cover"') == 1          # 只有头条整幅
    assert h.count('class="thumb"') == 1          # 二条缩略图
    assert "evil.example.com" not in h            # 外链封面一律不画
    # 头条封面太小 → 退成缩略图版式
    d2 = _data(items=[{"title": "头条", "cover": uri, "cover_w": 300}])
    h2 = news_card.render_html(d2, now=NOW)
    assert 'class="cover"' not in h2 and 'class="thumb"' in h2


def test_clip_counts_ascii_narrower():
    en = "Ace Combat 8: Wings of Theve Launch Trailer Arrives Ahead of Release"
    assert news_card._clip(en, 60) == en
    assert news_card._clip("中" * 80, 60).endswith("…")
