"""群资讯卡片里的图解必须完整可读（2026-10-15 用户要）。

卡片是一张图片，没法点：网页上的「标签页」在图片里只能看到第一页、横向滚动的表格右边看不到、
写不下的标签被省略号吃掉、非头条的图解被截半。所以卡片截图用「平铺版」：
- 标签页全部摊开，每页上面一行页名，没有可点的标签条；
- 柱状图标签、表格单元格折行，不出现省略号和横向滚动；
- 新格式图解要么整块画出来，要么不画（条目照样有文字摘要 + 二维码），绝不截半。
"""

from __future__ import annotations

import base64
import json
import os
import pwd
import struct
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import news_card, news_viz, viz_spec

SPEC = {
    "title": "GPT-6 Sol 跑分与价格",
    "caption": "和上一代比",
    "blocks": [
        {"type": "stats", "items": [
            {"value": 43, "unit": "%", "label": "Terminal-Bench"},
            {"value": 2, "unit": "$", "label": "输入/百万 token", "change": "−50%"},
        ]},
        {"type": "tabs", "tabs": [
            {"label": "跑分", "blocks": [{"type": "compare", "unit": "%", "items": [
                {"label": "Sol", "value": 58}, {"label": "上一代", "value": 54}, {"label": "Luna", "value": 44}]}]},
            {"label": "价格表", "blocks": [{"type": "table",
                "columns": ["型号", "输入价格（每百万）", "输出价格（每百万）", "上下文窗口", "发布日期"],
                "rows": [["GPT-6 Sol", "$2.00", "$8.00", "400K tokens", "2026-10-01"],
                         ["GPT-5.6", "$4.00", "$16.00", "256K tokens", "2026-05-12"]]}]},
            {"label": "逐月", "blocks": [{"type": "bars", "unit": "%", "items": [
                {"label": "2026年1月份", "value": 31}, {"label": "2026年2月份", "value": 37},
                {"label": "2026年3月份", "value": 40}, {"label": "2026年4月份", "value": 43}]}]},
        ]},
        {"type": "takeaway", "text": "Sol 跑分更高，价格降了一半"},
    ],
}


def _spec() -> dict:
    return viz_spec.validate(SPEC)


def _body(html: str) -> str:
    return html.split("<body>", 1)[1]


# ---------------------------------------------------------------------------
# 平铺版 HTML（不用浏览器）
# ---------------------------------------------------------------------------


def test_static_page_lays_out_every_tab() -> None:
    html = viz_spec.page(_spec(), uid="v1", static=True)
    assert "<input" not in _body(html) and "vs-tabbar" not in _body(html)  # 没有可点的标签条
    for label in ("跑分", "价格表", "逐月"):
        assert f'<h4 class="vs-sec-h">{label}</h4>' in html      # 每页一行页名
    for text in ("Luna", "256K tokens", "2026年4月份", "Sol 跑分更高"):
        assert text in html
    assert news_viz.STATIC_MARK in html


def test_web_page_unchanged() -> None:
    """网页用的仍是可点的标签页（只有卡片截图换平铺版）。"""
    html = viz_spec.page(_spec(), uid="v1")
    assert "<input" in _body(html) and "vs-tabbar" in _body(html)
    assert news_viz.STATIC_MARK not in html


def test_card_html_for_uses_static_for_spec_rows(tmp_path: Path) -> None:
    from CharTyr_MaiWork.maiwork.store import Store

    store = Store(tmp_path / "s.db")
    store.migrate()
    g = "900000001"
    with store.tx() as conn:
        bid = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created) VALUES (?, 1, 1, 1, 0, '', 1)", (g,)
        ).lastrowid
        iid = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key, score, created, kind, rejected,"
            " target_user_id, image_url, body) VALUES (?, ?, 't', 's', '[]', 'k', 1, 1, 'news', 0, '', '', '')",
            (bid, g),
        ).lastrowid
    news_viz._set(store, g, iid, "ok", html=news_viz.SPEC_PREFIX + json.dumps(_spec()), now=1.0)  # noqa: SLF001
    card = news_viz.card_html_for(store, g, iid)
    assert card is not None and news_viz.STATIC_MARK in card and "<input" not in _body(card)
    web = news_viz.html_for(store, g, iid)
    assert web is not None and "<input" in web


# ---------------------------------------------------------------------------
# 真浏览器：摊开的每一块都看得见、没有被吃掉的字、不截半
# ---------------------------------------------------------------------------


def _browsers(monkeypatch) -> None:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    for cache in (real_home / "Library/Caches/ms-playwright", real_home / ".cache/ms-playwright"):
        if cache.is_dir():
            monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
            return
    pytest.skip("本机没装 playwright 浏览器")


CHECK_JS = """() => {
  const bad = [];
  document.querySelectorAll('.vs *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' && el.textContent.trim()) bad.push('hidden:' + el.className);
    if (el.scrollWidth > el.clientWidth + 1 && (cs.overflowX === 'auto' || cs.overflowX === 'hidden' || cs.overflowX === 'scroll'))
      bad.push('clipped:' + el.className + ':' + el.textContent.slice(0, 20));
  });
  return bad;
}"""


@pytest.mark.asyncio
async def test_static_page_nothing_hidden_or_clipped(monkeypatch) -> None:
    _browsers(monkeypatch)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            pg = await b.new_page(viewport={"width": news_card._VIZ_W, "height": 400})  # noqa: SLF001
            await pg.set_content(viz_spec.page(_spec(), uid="v1", static=True))
            assert await pg.evaluate(CHECK_JS) == []
            # 对照：网页版在卡片宽度下确实会藏东西（证明这个检查查得出来）
            await pg.set_content(viz_spec.page(_spec(), uid="v1"))
            assert await pg.evaluate(CHECK_JS) != []
        finally:
            await b.close()


def _png_h(uri: str) -> int:
    raw = base64.b64decode(uri.split(",", 1)[1])
    return struct.unpack(">I", raw[20:24])[0]


@pytest.mark.asyncio
async def test_shoot_static_viz_never_cut(monkeypatch) -> None:
    """新格式图解：第二条也整块画出来（不再截到 300 高）；超出总高度预算的就不画，不截半。"""
    _browsers(monkeypatch)
    from playwright.async_api import async_playwright

    html = viz_spec.page(_spec(), uid="v1", static=True)
    data = {"items": [
        {"title": "一", "viz_html": html},
        {"title": "二", "viz_html": html},
        {"title": "三", "viz_html": html},
    ]}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            pg = await b.new_page(viewport={"width": news_card._VIZ_W, "height": 400})  # noqa: SLF001
            await pg.set_content(html)
            real_h = int(await pg.evaluate("Math.ceil(document.documentElement.scrollHeight)"))
            await pg.close()
            assert real_h > 400                       # 比截图窗口高：老代码在这里悄悄截掉了下半截
            probe = {"items": [{"title": "一", "viz_html": html}]}
            await news_card._shoot_viz(b, probe)  # noqa: SLF001  头条不受预算限制
            one_h = _png_h(probe["items"][0]["viz_img"]) // news_card._SCALE  # noqa: SLF001
            assert one_h == real_h                    # 整块都在图里
            assert one_h > news_card._VIZ_SUB_MAX_H  # noqa: SLF001  这块比老的截断高度高
            assert probe["items"][0]["viz_cut"] is False
            # 2026-10-15 用户定：第二、三条一样带图解——三条都整块画出
            await news_card._shoot_viz(b, data)  # noqa: SLF001
        finally:
            await b.close()
    for it in data["items"]:
        assert _png_h(it["viz_img"]) // news_card._SCALE == one_h and it["viz_cut"] is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_old_html_viz_also_never_cut(monkeypatch) -> None:
    """线上 2026-10-15 用户报：第二条起的图解底部被截掉、虚化。老格式 HTML 图解也一样：
    整块画（不再截到 300 高 + 渐隐），放不下预算就不画。"""
    _browsers(monkeypatch)
    from playwright.async_api import async_playwright

    old = '<!doctype html><html><body style="margin:0"><div style="height:640px;background:#eef">老图解</div></body></html>'
    data = {"items": [
        {"title": "一", "viz_html": old},
        {"title": "二", "viz_html": old},
        {"title": "三", "viz_html": old},
    ]}
    monkeypatch.setattr(news_card, "_VIZ_STATIC_MAX_H", 2400)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            await news_card._shoot_viz(b, data)  # noqa: SLF001
        finally:
            await b.close()
    for it in data["items"]:                         # 三条都整块画（用户定：后面的一样带图解）
        assert _png_h(it["viz_img"]) // news_card._SCALE == 640  # noqa: SLF001
        assert it["viz_cut"] is False


@pytest.mark.asyncio
async def test_runaway_tall_viz_skipped_not_cut(monkeypatch) -> None:
    """单张失控的超长图解（> _VIZ_STATIC_MAX_H）不画，也不截半。"""
    _browsers(monkeypatch)
    from playwright.async_api import async_playwright

    tall = '<!doctype html><html><body style="margin:0"><div style="height:900px">长</div></body></html>'
    data = {"items": [{"title": "一", "viz_html": tall}, {"title": "二", "viz_html": tall}]}
    monkeypatch.setattr(news_card, "_VIZ_STATIC_MAX_H", 800)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            await news_card._shoot_viz(b, data)  # noqa: SLF001
        finally:
            await b.close()
    assert all("viz_img" not in it for it in data["items"])
