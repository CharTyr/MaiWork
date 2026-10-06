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

def _png(w: int, h: int, *, flat: bool = False) -> bytes:
    """测试图：默认是「像照片」的随机彩色噪声（能过 cover_filter）；flat=True 是白底 logo 样的平涂图。"""
    import io
    import random

    from PIL import Image, ImageDraw, ImageFilter

    if flat:
        im = Image.new("RGB", (w, h), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle([w // 4, h // 3, w // 2, h * 2 // 3], fill=(120, 110, 100))
        d.ellipse([w // 2, h // 3, w * 3 // 4, h * 2 // 3], fill=(180, 30, 50))
    else:
        rnd = random.Random(w * 7 + h)
        im = Image.new("RGB", (w, h))
        im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
        im = im.filter(ImageFilter.GaussianBlur(1))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fetch_cover_rejects_logo_like_images():
    """2026-10-06 用户要：og:image 是站点 logo / 带字横幅的不用，宁可不放图（cover_filter）。"""
    pytest.importorskip("PIL")
    t = _transport({
        "https://img.example.com/a.jpg": ("image/png", _png(1200, 630, flat=True)),
        "https://img.example.com/site-logo-fb.png": ("image/png", _png(1200, 630)),
    })
    assert await news_card.fetch_cover("https://img.example.com/a.jpg", transport=t, resolver=_public) == ("", 0)
    assert await news_card.fetch_cover("https://img.example.com/site-logo-fb.png",
                                       transport=t, resolver=_public) == ("", 0)


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


def _jpeg_uri(rgb=(30, 90, 220), w=640, h=360) -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def test_layouts_with_cover():
    """2026-10-06 用户定：有配图的每一条都整幅放在标题上面，放进统一比例的画框（1+4 方案）。"""
    uri = "data:image/jpeg;base64,AAAA"
    d = _data(items=[{"title": "头条", "cover": uri, "cover_w": 1200},
                     {"title": "二条", "cover": uri, "cover_w": 900},
                     {"title": "三条", "cover": "https://evil.example.com/x.jpg", "cover_w": 1200}])
    h = news_card.render_html(d, now=NOW)
    assert h.count('<div class="frame">') == 2
    assert 'class="thumb"' not in h
    assert "evil.example.com" not in h            # 外链封面一律不画
    assert h.index('<div class="frame">') < h.index("二条") and h.count(' has-cover"') == 2


def test_cover_frame_uniform_ratio_never_cropped():
    """画框比例统一（头条 16:9、其余 2:1）；图本身 object-fit:contain 完整显示，
    框里空出来的地方用同一张图模糊放大垫底（只是背景，不是裁图）。"""
    uri = "data:image/jpeg;base64,AAAA"
    h = news_card.render_html(_data(items=[{"title": "头条", "cover": uri, "cover_w": 1200},
                                           {"title": "二条", "cover": uri, "cover_w": 1200}]), now=NOW)
    css = h[h.index("<style>"):h.index("</style>")]
    assert ".frame{" in css and "aspect-ratio:16/9" in css
    assert ".item:not(.lead) .frame{aspect-ratio:2/1}" in css
    img_rule = css[css.index(".frame img{"):].split("}", 1)[0]
    assert "object-fit:contain" in img_rule
    assert "object-fit:cover" not in css and "background-size:cover;background-position:center;background-color" not in css
    assert f'<div class="frame"><div class="bg" style="background-image:url({uri})"></div><img src="{uri}"' in h
    # 图贴边、字要有内边距（2026-10-06 预览里出过字贴边的回归）
    assert ".item.has-cover{padding:0;" in css and ".has-cover .body{padding:" in css


def test_small_cover_not_upscaled():
    """原图不够宽（< _COVER_WIDE_W）也进画框，但按原大小居中，不硬拉满变糊。"""
    uri = "data:image/jpeg;base64,AAAA"
    h = news_card.render_html(_data(items=[{"title": "t", "cover": uri, "cover_w": 300}]), now=NOW)
    assert f'<img class="small" src="{uri}"' in h and 'class="thumb"' not in h


def test_tint_palette_readable_and_skips_grey():
    """取色（方案 4）：浅底 + 深字，对比度够；灰图（几乎没饱和度）不取，沿用天空配色。"""
    def lum(hexc):
        r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    for rgb in [(220, 30, 40), (30, 90, 220), (250, 220, 40), (20, 160, 80)]:
        pal = news_card.tint_palette(rgb)
        assert pal and set(pal) == {"bg", "ink", "glow"}
        ratio = (lum(pal["bg"]) + 0.05) / (lum(pal["ink"]) + 0.05)
        assert ratio >= 7, (rgb, pal, ratio)
    assert news_card.tint_palette((128, 128, 130)) is None


def test_cover_tint_from_data_uri():
    pytest.importorskip("PIL")
    pal = news_card.cover_tint(_jpeg_uri((30, 90, 220)))
    assert pal is not None
    r, g, b = (int(pal["ink"][i:i + 2], 16) for i in (1, 3, 5))
    assert b > r and b > g                       # 蓝图 → 蓝色系
    assert news_card.cover_tint("data:image/jpeg;base64,坏数据") is None
    assert news_card.cover_tint("") is None


def test_item_uses_cover_tint_else_sky_colors():
    uri = "data:image/jpeg;base64,AAAA"
    pal = {"bg": "#eef3ff", "ink": "#123a80", "glow": "rgba(30,90,220,.32)"}
    h = news_card.render_html(_data(items=[{"title": "有色", "cover": uri, "cover_w": 1200, "tint": pal},
                                           {"title": "无色"}]), now=NOW)
    assert 'style="--why-bg:#eef3ff;--why-ink:#123a80;--glow:rgba(30,90,220,.32)"' in h
    second = h[h.index("无色") - 200:h.index("无色")]
    assert "--why-bg" not in second               # 没配图的那条沿用天空配色


@pytest.mark.asyncio
async def test_prepare_covers_sets_tint():
    pytest.importorskip("PIL")
    uri = _jpeg_uri((220, 30, 40))

    async def fake(img, url):
        return uri, 640

    d = _data(items=[{"title": "a", "url": "https://x.example.com/a"}])
    await news_card.prepare_covers(d, fetch=fake)
    assert d["items"][0]["tint"] and d["items"][0]["tint"]["ink"].startswith("#")


def test_cards_overlap_the_sky_layered():
    """2026-09-29 用户要：资讯卡片往上压住天空背景的下半截，形成前后层次（视差感）；
    天体也有一部分被第一张卡片挡住。不再用底部那道白色山丘波浪。"""
    h = news_card.render_html(_data(), now=NOW)
    assert news_card._OVERLAP >= 80
    assert f"margin-top:-{news_card._OVERLAP}px" in h
    assert 'class="hill"' not in h
    # 天体底边落进卡片压住的那一截（中午太阳在高处，不算）
    for word, sky in news_card._SKIES.items():
        if word == "中午":
            continue
        size = news_card._ORB_SIZE[sky["orb"]]
        orb_bottom = news_card._SKY_H * sky["orb_y"] + size / 2
        assert orb_bottom > news_card._SKY_H - news_card._OVERLAP, word


def test_every_slot_renders_with_light_text():
    """苹果天气那种做法：五个时段都是白字压在渐变天空上。"""
    for word in news_card._SKIES:
        h = news_card.render_html(_data(slot_label=f"9月29日 · {word}"), now=NOW)
        assert "--ink:#fff" in h, word


def test_viz_image_shown_when_no_cover():
    """没封面、但有图解截图（viz_img）的条目：卡片里画出图解；有封面的仍用封面，不重复画图解。"""
    img = "data:image/png;base64,iVBORw0KGgo="
    items = [
        {"title": "有图解没封面", "summary": "s", "viz_img": img},
        {"title": "有封面也有图解", "summary": "s", "cover": "data:image/jpeg;base64,/9j/", "viz_img": img},
        {"title": "外链图解不画", "summary": "s", "viz_img": "https://evil.example/x.png"},
    ]
    h = news_card.render_html(_data(items=items), now=NOW)
    assert h.count('class="viz"') == 1
    assert "evil.example" not in h


def test_viz_pages_selection():
    """要截图解的：前 3 条里没封面、有图解整页的。"""
    data = {"items": [
        {"title": "a", "viz_html": "<html>1</html>"},
        {"title": "b", "cover": "data:image/jpeg;base64,x", "viz_html": "<html>2</html>"},
        {"title": "c"},
    ]}
    assert [it["title"] for it in news_card._viz_todo(data)] == ["a"]


def test_title_and_why_not_truncated():
    """2026-09-29 用户要：标题、「值得看」显示完整，不截断（摘要 2026-10-06 改成短摘要，见下）。"""
    long_title = "很长的标题" * 20
    long_why = "值得看的理由很长，" * 15
    items = [{"title": long_title, "brief": "短摘要。", "why": long_why, "site": "a.com"},
             {"title": "二", "brief": "短。"}]
    h = news_card.render_html(_data(items=items), now=NOW)
    assert long_title in h and long_why.strip() in h


def test_brief_replaces_long_summary():
    """2026-10-06 用户要：卡片上的摘要更简短——有 brief 就只放 brief，长 summary 不进卡片。"""
    long_sum = "这是一段很长的摘要原文内容。" * 20
    items = [{"title": "一", "brief": "一句话短摘要，带关键数字 150ms。", "summary": long_sum}]
    h = news_card.render_html(_data(items=items), now=NOW)
    assert "一句话短摘要，带关键数字 150ms。" in h
    assert long_sum not in h and "这是一段很长的摘要原文内容" not in h


def test_summary_fallback_cut_at_sentence():
    """老资讯没有 brief：从 summary 按句号取开头一两句，不超过上限，不把整段长摘要搬上来。"""
    s = "第一句讲清楚发生了什么事情。第二句补一个关键数字 30fps。" + "后面还有很多很长的细节描述，" * 20 + "结尾。"
    out = news_card.short_summary(s)
    assert out.startswith("第一句讲清楚发生了什么事情。")
    assert out.endswith("。") and len(out) <= news_card._BRIEF_MAX + 1
    # 第一句就超长：在逗号处断开、加省略号
    long_one = "一个很长很长的句子，" * 20 + "完。"
    cut = news_card.short_summary(long_one)
    assert cut.endswith("…") and len(cut) <= news_card._BRIEF_MAX + 1
    assert news_card.short_summary("") == ""
    assert news_card.short_summary("短。") == "短。"


def test_why_only_on_lead_item():
    """2026-10-06 用户定：「值得看」只放头条，第二、三条只留标题 + 短摘要。"""
    items = [{"title": f"t{i}", "brief": "b。", "why": f"理由{i}"} for i in range(1, 4)]
    h = news_card.render_html(_data(items=items), now=NOW)
    assert "理由1" in h and "理由2" not in h and "理由3" not in h
    assert h.count('class="why"') == 1


def test_qr_code_when_link():
    """2026-10-06 用户要：卡片上加二维码，扫码进本群 MaiWork 网页；链接原文不以文字出现在图里。"""
    h = news_card.render_html(_data(), now=NOW)
    m = re.search(r'class="qr"[^>]*src="(data:image/svg\+xml;base64,[^"]+)"', h)
    assert m, "有链接就要画二维码"
    assert "mw.example.com" not in h
    h2 = news_card.render_html(_data(link=""), now=NOW)
    assert 'class="qr"' not in h2


def test_guide_items_hide_publish_date_news_items_keep_it():
    """2026-10-05 用户：发群的资讯卡片上，文章（kind=guide）不显示发布日期；资讯照旧显示「N 天前」。"""
    items = [
        {"title": "资讯一条", "summary": "s", "site": "a.com", "published_ts": NOW - 7200, "kind": "news"},
        {"title": "老文章一篇", "summary": "s", "site": "b.com", "published_ts": NOW - 40 * 86400, "kind": "guide"},
    ]
    h = news_card.render_html(_data(items=items, total=2), now=NOW)
    assert "2 小时前" in h
    guide_part = h[h.index("老文章一篇"):]
    assert "b.com" in guide_part
    assert "天前" not in guide_part and "月" not in guide_part.split("b.com", 1)[1].split("</div>", 1)[0]
