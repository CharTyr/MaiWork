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


@pytest.mark.asyncio
async def test_settings_attention_and_optional_components_responsive(env, monkeypatch, tmp_path):
    import json
    import os
    import pwd
    from pathlib import Path

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    caches = (real_home / 'Library/Caches/ms-playwright', real_home / '.cache/ms-playwright')
    cache = next((p for p in caches if p.is_dir()), None)
    assert cache is not None, '需要已安装的 Chromium，不能跳过真实浏览器验收'
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(cache))
    review_web.seed(env)
    base = str(env.client.make_url('/')).rstrip('/')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            response = await context.request.post(base + '/api/login', data={'password': ADMIN_PW})
            assert response.status == 200
            # 只替换发送状态与探测结果，不调用真实发群或 pip 安装接口。
            async def cards(route):
                await route.fulfill(content_type='application/json', body=json.dumps({'cards': [{
                    'id': 7, 'group_id': G1, 'group_name': '资料整理群',
                    'status': 'failed', 'titles': ['新的研究进展'],
                    'fallback': {'status': 'queued'}, 'error': '图片发送失败',
                }]}))

            async def optional(route):
                await route.fulfill(content_type='application/json', body=json.dumps({
                    'available': True, 'groups': [{
                        'id': 'office', 'label': '办公文档', 'purpose': '制作 Word、Excel、PowerPoint',
                        'state': 'missing', 'installed_count': 0, 'total': 3,
                        'packages': [{'label': name, 'installed': False}
                                     for name in ('Word 文档', 'Excel 表格', 'PowerPoint 演示')],
                    }],
                }))

            await context.route('**/api/news/cards?limit=10', cards)
            await context.route('**/api/optional-deps', optional)
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for width in (390, 760, 1180, 1440):
                await page.set_viewport_size({'width': width, 'height': 900})
                await page.goto(base + '/#/settings/overview')
                await expect(page.get_by_role('heading', name='待处理', exact=True)).to_be_visible()
                await expect(page.get_by_text('文字版等待发送', exact=False)).to_be_visible()
                await expect(page.get_by_text('已改发文字版', exact=False)).to_have_count(0)
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                await page.screenshot(path=str(tmp_path / f'attention-{width}.png'), full_page=True)
                await page.goto(base + '/#/settings/extensions')
                await page.get_by_role('tab', name='可选组件', exact=True).click()
                await expect(page.get_by_role('heading', name='可选组件', exact=True)).to_be_visible()
                await expect(page.get_by_role('button', name='安装', exact=True)).to_be_visible()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                await page.screenshot(path=str(tmp_path / f'optional-{width}.png'), full_page=True)
            assert not errors, errors
            await context.close()
        finally:
            await browser.close()
