"""真 Chromium 验证群页「功能启用」区块：三开关渲染、关任务、保存后状态与提示。

只连本地假宿主，不发群消息。本机装了 playwright 浏览器才跑。
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles
from test_group_controls_runtime_wiring import G1, ADMIN_PW, _raw_config, _write_plugin_dir

from CharTyr_MaiWork.maiwork.app import MaiWorkApp


def _browsers(monkeypatch) -> None:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    for cache in (real_home / "Library/Caches/ms-playwright", real_home / ".cache/ms-playwright"):
        if cache.is_dir():
            monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
            return
    pytest.skip("本机没装 playwright 浏览器")


@pytest.mark.asyncio
async def test_group_feature_switches_render_and_toggle(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("playwright.async_api")
    _browsers(monkeypatch)
    from playwright.async_api import async_playwright

    raw = _raw_config(tmp_path / "data")
    _write_plugin_dir(tmp_path / "plug", raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=tmp_path / "plug")
    app.profiles_cls = FakeProfiles
    await app.start()
    tid = app.tasks.create(G1, title="等待手动恢复的任务", req="test", criteria=[], source="test")
    app.spawn_run_task = lambda _tid: None
    server = TestServer(app.console.app)
    await server.start_server()
    try:
        base = str(server.make_url("/")).rstrip("/")
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True)
            try:
                ctx = await b.new_context(viewport={"width": 760, "height": 900})
                r = await ctx.request.post(base + "/api/login", data={"password": ADMIN_PW})
                assert r.status == 200
                page = await ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(base + f"/#/{G1}/group")
                # 群控区渲染出「功能启用」与三个开关
                await page.wait_for_selector(".groupctl-slot", timeout=15000)
                html = await page.inner_html(".groupctl-slot")
                assert "功能启用" in html and "资讯" in html and "构想" in html and "任务" in html
                # 开「功能启用」的编辑（该区块的修改按钮），草稿默认任务开着
                await page.click('[data-act="gctl-features-edit"]')
                await page.wait_for_selector("#gctl-task", timeout=5000)
                assert await page.is_checked("#gctl-task")
                # 关掉任务并保存
                await page.evaluate("() => { const el = document.querySelector('#gctl-task'); el.checked = false; el.dispatchEvent(new Event('input', {bubbles:true})); }")
                await page.click('[data-act="gctl-features-save"]')
                await page.wait_for_function(
                    "document.querySelector('.groupctl-slot') && document.querySelector('.groupctl-slot').innerHTML.includes('已关闭')",
                    timeout=8000,
                )
                # 后端真的关了
                r2 = await ctx.request.get(base + f"/api/groups/{G1}/features")
                body = await r2.json()
                assert body["task"] is False and body["news"] is True and body["idea"] is True
                await page.goto(base + f"/#/{G1}/tasks")
                await page.reload()
                await page.click(f'[data-act="task"][data-id="{tid}"]')
                recovery = page.locator('[data-act="task-op"][data-op="resume"]')
                await recovery.wait_for(timeout=8000)
                assert "重新开启" in await page.inner_text("body")
                blocked = await ctx.request.post(base + f"/api/tasks/{tid}/resume", data={})
                assert blocked.status == 409
                enabled = await ctx.request.put(base + f"/api/groups/{G1}/features", data={"task": True})
                assert enabled.status == 200
                assert app.tasks.get(tid)["paused_reason"]
                async with page.expect_response(lambda r: f"/api/tasks/{tid}/resume" in r.url) as resumed:
                    await recovery.click()
                assert (await resumed.value).status == 200
                assert not app.tasks.get(tid)["paused_reason"]
                await page.wait_for_function("!document.querySelector('[data-act=\"task-op\"][data-op=\"resume\"]')")
                assert not errors, errors
                await ctx.close()
            finally:
                await b.close()
    finally:
        await server.close()
        await app.stop()
