"""真 Chromium 验证复核界面；只连本地假宿主，不发群消息。"""
import pytest
from playwright.async_api import async_playwright, expect

import test_pending_ask_review_web as review_web
from test_group_admin import G1, ADMIN_PW

env = review_web.env


@pytest.mark.asyncio
async def test_review_real_browser_responsive_and_convert(env, monkeypatch):
    # The isolation fixture replaces HOME; select only the installed browser cache,
    # keeping the fake HOME and all host/systemd isolation guards intact.
    import os
    import pwd
    from pathlib import Path
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    caches = (real_home / 'Library/Caches/ms-playwright', real_home / '.cache/ms-playwright')
    cache = next((p for p in caches if p.is_dir()), None)
    assert cache is not None, '需要已安装的 Chromium，不能跳过真实浏览器验收'
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(cache))
    pa = review_web.seed(env)
    base = str(env.client.make_url('/')).rstrip('/')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            r = await context.request.post(base + '/api/login', data={'password': ADMIN_PW})
            assert r.status == 200
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: dialog.accept())
            await page.goto(base + '/#/' + G1 + '/tasks')
            await expect(page.get_by_role('heading', name='待复核', exact=False)).to_be_visible()
            for width in (390, 760, 1180, 1440):
                await page.set_viewport_size({'width': width, 'height': 900})
                await expect(page.get_by_role('button', name='转为任务请求', exact=True)).to_be_visible()
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), width
            await page.get_by_role('button', name='转为任务请求', exact=True).click()
            await expect(page.get_by_role('button', name='转为任务请求', exact=True)).to_have_count(0)
            await expect(page.get_by_role('button', name='批准', exact=True)).to_be_visible()
            assert pa.get(G1, 'old')['state'] == 'completed'
            assert len(env.app.approvals.pending_view(G1)) == 1
            assert not errors, errors
            await context.close()
        finally:
            await browser.close()
