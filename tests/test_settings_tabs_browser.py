"""设置页横向分栏点击时不许「先跳回最左再滑过来」（2026-10-15 用户报：点击滑动时抽搐）。

真浏览器（本机装了 playwright 浏览器才跑）：起 app + console，登录后进「设置 → 全部设置」，
依次点分节标签，逐帧记 `.cfg-jump` 的 scrollLeft：
- 滑动必须从原位置出发、单调走向目标（中途不能掉回 0 或反向）；
- 同一个分栏 DOM 被沿用（不是每点一下就换一个新元素）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from fakes import FakeCtx, FakeProfiles
from test_rules_web import PASSWORD, _raw_config, _write_plugin_dir

from CharTyr_MaiWork.maiwork.app import MaiWorkApp


TRACE_JS = """() => { window.__tr=[]; const t0=performance.now();
  const f=()=>{const b=document.querySelector('#view .cfg-jump'); window.__tr.push(b?b.scrollLeft:-1);
  if(performance.now()-t0<800) requestAnimationFrame(f);}; requestAnimationFrame(f); }"""


def _monotonic(seq: list[float], start: float) -> bool:
    if not seq:
        return True
    end = seq[-1]
    sign = 1 if end >= start else -1
    prev = start
    for v in seq:
        if (v - prev) * sign < -1.5:
            return False
        prev = v
    return True


@pytest.mark.asyncio
async def test_cfg_tabs_slide_without_jump(tmp_path: Path, browser_cache) -> None:
    from playwright.async_api import async_playwright

    raw = _raw_config(tmp_path / "data")
    _write_plugin_dir(tmp_path / "plug", raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=tmp_path / "plug")
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    await server.start_server()
    try:
        base = str(server.make_url("/"))
        async with async_playwright() as p:
            b = await p.chromium.launch()
            try:
                pg = await b.new_page(viewport={"width": 390, "height": 800})
                await pg.goto(base)
                st = await pg.evaluate(
                    """async (pw) => (await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:pw})})).status""",
                    PASSWORD,
                )
                assert st == 200
                await pg.goto(base + "#/settings/rules")
                await pg.reload()
                await pg.wait_for_selector("#view .cfg-jump button")
                n = await pg.locator("#view .cfg-jump button").count()
                assert n >= 8
                await pg.evaluate("window.__bar = document.querySelector('#view .cfg-jump')")
                moved = 0
                for idx in (4, 6, 8, 3):
                    start = await pg.evaluate("document.querySelector('#view .cfg-jump').scrollLeft")
                    await pg.evaluate(TRACE_JS)
                    await pg.evaluate("(i)=>document.querySelectorAll('#view .cfg-jump button')[i].click()", idx)
                    await pg.wait_for_timeout(900)
                    tr = [v for v in await pg.evaluate("window.__tr") if v >= 0]
                    assert _monotonic(tr, start), f"点第 {idx} 个：滚动轨迹有回跳 {tr}（起点 {start}）"
                    if tr and abs(tr[-1] - start) > 2:
                        moved += 1
                    sel = await pg.evaluate("document.querySelector('#view .cfg-jump [aria-selected=\"true\"]').textContent")
                    want = await pg.evaluate("(i)=>document.querySelectorAll('#view .cfg-jump button')[i].textContent", idx)
                    assert sel == want
                assert moved >= 2, "分栏没有滑动（测试没测到东西）"
                assert await pg.evaluate("window.__bar === document.querySelector('#view .cfg-jump')"), "分栏 DOM 没有沿用"
            finally:
                await b.close()
    finally:
        await server.close()
        await app.stop()
