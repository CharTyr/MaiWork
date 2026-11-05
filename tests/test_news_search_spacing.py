"""资讯页搜索框与顶部分栏的间距回归（2026-11 用户报：`.news-search` 贴着 `.news-bar`，0px）。

真浏览器（本机装了 playwright 浏览器才跑）：起 app + console，种一条资讯批次 +
一条保留条目，管理员登录后进 `#/<群号>/news`：

- `资讯` 分栏：`.news-search` 顶边距 `.news-bar` 底边 ≥ 12px（修前 margin 折叠
  `-10px + 10px = 0`，搜索框直接贴上去了）；`.news-search` 到批次 `<h1 class="h-page">`
  ≥ 16px；
- `文章` 分栏：`.news-bar` 之后直接是 `<h1 class="h-page">`，间距保持 12–30px
  （设计节奏手机 24px / 桌面 16px）；
- 手机视口（390px）：`#news-q` 字号 ≥ 16px（iOS 点按不自动放大）、圆角 ≥ 12px；
  切 `data-theme="retro"` 时输入框圆角归零（下沉屏幕语言）。
"""

from __future__ import annotations

import json

import pytest
from playwright.async_api import async_playwright

from CharTyr_MaiWork.maiwork import clock
from test_console import G1, PASSWORD, env  # noqa: F401  (env fixture)


def _seed_news(env) -> None:
    """种一条资讯批次 + 一条 kind='news' 的保留条目（viewNews 才会渲染 .news-search）。"""
    now = clock.now()
    with env.app.store.tx() as conn:
        # 群画像成形（profile_ready_ts 非空）→ g.fresh=False → 走正常资讯页而不是
        # 「正在建立群画像」空态。
        conn.execute(
            "UPDATE groups SET profile_ready_ts=?, read_since=? WHERE group_id=?",
            (now, now - 3600, G1),
        )
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (G1, now, now),
        )
        bid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key,"
            " score, created, kind, rejected, target_user_id)"
            " VALUES (?, ?, '本地大模型又更新了', '一句话摘要', '[]', 'a.com/1', 4.0, ?, 'news', 0, '')",
            (bid, G1, now),
        )


async def _open_news(pg, env) -> None:
    """登录并打开当前群的资讯页（管理员网址用群号）。"""
    st = await pg.evaluate(
        """async (pw) => (await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:pw})})).status""",
        PASSWORD,
    )
    assert st == 200
    token = env.app.token_of(G1)
    await pg.goto(f"{pg.url.split('#')[0]}#/{token}/news")
    await pg.wait_for_selector("#view .news-bar")


def _gap(pg, top_sel: str, bottom_sel: str) -> float:
    return pg.evaluate(
        """([top, bottom]) => {
          const t = document.querySelector(top);
          const b = document.querySelector(bottom);
          if (!t || !b) return -999;
          return b.getBoundingClientRect().top - t.getBoundingClientRect().bottom;
        }""",
        [top_sel, bottom_sel],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 860}, {"width": 390, "height": 800}])
async def test_news_search_does_not_touch_news_bar(env, browser_cache, viewport) -> None:
    """`.news-search` 不许贴 `.news-bar`：修前两条 margin 折成 0px，这里必须 ≥12px。"""
    _seed_news(env)
    base = env.base
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            pg = await b.new_page(viewport=viewport)
            await pg.goto(base)
            await _open_news(pg, env)
            await pg.wait_for_selector("#view .news-search input")
            bar_to_search = await _gap(pg, "#view .news-bar", "#view .news-search")
            assert bar_to_search >= 12, (
                f"视口 {viewport['width']}px：.news-search 顶边距 .news-bar 底边只有"
                f" {bar_to_search}px（margin 折叠成 0 → 搜索框贴上去了）"
            )
            search_to_h1 = await _gap(pg, "#view .news-search", "#view .h-page")
            assert search_to_h1 >= 16, (
                f"视口 {viewport['width']}px：.news-search 到批次标题只有 {search_to_h1}px"
            )
        finally:
            await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 860}, {"width": 390, "height": 800}])
async def test_guides_tab_keeps_bar_to_title_rhythm(env, browser_cache, viewport) -> None:
    """`文章` 分栏：`.news-bar` 后紧跟 `<h1 class="h-page">`，节奏 12–30px
    （负 margin 时代靠 `-10px + 34px = 24px` 撑出来；改成正 margin 后要靠
    `.news-bar + .h-page` 邻接规则兜住，不能塌掉也不能翻倍）。"""
    _seed_news(env)
    base = env.base
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            pg = await b.new_page(viewport=viewport)
            await pg.goto(base)
            await _open_news(pg, env)
            await pg.click('#view [data-act="news-tab"][data-t="guides"]')
            await pg.wait_for_selector("#view .h-page")
            gap = await _gap(pg, "#view .news-bar", "#view .h-page")
            assert 12 <= gap <= 30, (
                f"视口 {viewport['width']}px：文章页 .news-bar 到标题间距 {gap}px，"
                "应落在 12–30px 节奏内"
            )
        finally:
            await b.close()


@pytest.mark.asyncio
async def test_mobile_search_input_style_and_retro_radius(env, browser_cache) -> None:
    """手机视口：`#news-q` 字号 ≥16px（iOS 点按不放大）、圆角 ≥12px；
    `data-theme="retro"` 时圆角必须是 0（下沉屏幕输入框语言）。"""
    _seed_news(env)
    base = env.base
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            pg = await b.new_page(viewport={"width": 390, "height": 800})
            await pg.goto(base)
            await _open_news(pg, env)
            await pg.wait_for_selector("#view .news-search input")
            font_size = await pg.evaluate(
                "parseFloat(getComputedStyle(document.querySelector('#news-q')).fontSize)"
            )
            radius = await pg.evaluate(
                "parseFloat(getComputedStyle(document.querySelector('#news-q')).borderRadius)"
            )
            assert font_size >= 16, f"手机端 #news-q 字号 {font_size}px < 16px，iOS 点按会放大页面"
            assert radius >= 12, f"默认主题 #news-q 圆角 {radius}px < 12px（design.md §6.1 输入 12–16px）"

            await pg.evaluate("document.documentElement.setAttribute('data-theme', 'retro')")
            retro_radius = await pg.evaluate(
                "getComputedStyle(document.querySelector('#news-q')).borderRadius"
            )
            assert retro_radius == "0px", (
                f"retro 主题 #news-q 圆角应为 0px（下沉屏幕），实际 {retro_radius}"
            )
        finally:
            await b.close()
