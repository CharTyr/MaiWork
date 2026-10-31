"""Real Chromium/WebKit checks for the opaque artifact sandbox (local fake host only)."""
import os
import pwd
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from test_console import env, G1
from test_review_artifacts import task, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
async def test_r2_opaque_sandbox_loads_relative_css_and_modules_without_cookie(env, monkeypatch, engine):
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    caches = (real_home / "Library/Caches/ms-playwright", real_home / ".cache/ms-playwright")
    cache = next((p for p in caches if p.is_dir()), None)
    assert cache is not None, "需要已安装的浏览器，不能跳过成品沙盒验收"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    tid = task(env)
    directory = artifact(env, tid)
    (directory / "index.html").write_text('<!doctype html><link rel="stylesheet" href="./style.css"><body><p id="result">loading</p><script type="module" src="./app.mjs"></script></body>')
    (directory / "style.css").write_text("#result { color: rgb(123, 45, 67); }")
    (directory / "part.mjs").write_text('export const message = "module loaded";')
    (directory / "app.mjs").write_text('''import { message } from "./part.mjs";
      document.querySelector("#result").textContent = message;
      try { parent.document.body.dataset.leaked = "yes"; }
      catch { document.body.dataset.parentBlocked = "yes"; }
      try { await fetch("/api/me"); document.body.dataset.fetchBlocked = "no"; }
      catch { document.body.dataset.fetchBlocked = "yes"; }
    ''')
    token = env.app.token_of(G1)
    origin = str(env.client.make_url("/")).rstrip("/")
    path = f"/g/{token}/artifacts/{tid}/"
    async with async_playwright() as pw:
        browser = await getattr(pw, engine).launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            # A plain same-site parent document, with no administrator cookie/API token header.
            await page.goto(origin + "/static/js/util.js")
            await page.set_content(f'<iframe sandbox="allow-scripts" src="{path}"></iframe>')
            frame = page.frame_locator("iframe")
            await expect(frame.locator("#result")).to_have_text("module loaded")
            await expect(frame.locator("#result")).to_have_css("color", "rgb(123, 45, 67)")
            await expect(frame.locator("body")).to_have_attribute("data-parent-blocked", "yes")
            await expect(frame.locator("body")).to_have_attribute("data-fetch-blocked", "yes")
            assert await context.cookies() == []
            assert await page.locator("body").get_attribute("data-leaked") is None
            await context.close()
        finally:
            await browser.close()
