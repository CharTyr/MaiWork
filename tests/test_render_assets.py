"""画图组件（`maiwork/render_assets.py`）：中文字体 + 画图浏览器一键装。

背景：Docker 版 MaiBot 精简镜像常常没有中文字体和 Chromium，资讯卡片会出方块字或改发文字。
MaiWork 不能改镜像、不能 apt 装系统包，所以把字体 / 浏览器下载到 MaiWork 自己的目录。

全部用假 runner / httpx.MockTransport：**不联网、不真下载、不真起浏览器**。
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from CharTyr_MaiWork.maiwork import news_card
from CharTyr_MaiWork.maiwork import optional_deps as od
from CharTyr_MaiWork.maiwork import render_assets as ra
from CharTyr_MaiWork.maiwork.store import Store


class _Settings:
    def __init__(self, root: Path) -> None:
        self.environments = types.SimpleNamespace(workspace_root=str(root / "workspaces"))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "db.sqlite")
    s.migrate()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset_locator():
    yield
    ra.set_root_getter(None)


FAKE_FONT = b"OTTO" + b"x" * 1000
FAKE_SHA = hashlib.sha256(FAKE_FONT).hexdigest()


def _font(urls=("https://a.example/f.otf", "https://b.example/f.otf"), sha=FAKE_SHA):
    return ra.FontAsset(
        label="测试字体", filename="Test-Regular.otf", size=len(FAKE_FONT), sha256=sha,
        urls=tuple(urls), license="OFL-1.1",
    )


# ----------------------------------------------------------------------
# 字体下载：钉死 sha256、主地址失败走备用
# ----------------------------------------------------------------------


def test_builtin_font_is_pinned() -> None:
    f = ra.FONT
    assert len(f.sha256) == 64 and f.size > 1_000_000
    assert len(f.urls) >= 2 and all(u.startswith("https://") for u in f.urls)
    assert "OFL" in f.license


@pytest.mark.asyncio
async def test_download_ok(tmp_path: Path) -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, content=FAKE_FONT)

    path = await ra.download_font(tmp_path / "fonts", font=_font(), transport=httpx.MockTransport(handler))
    assert path.read_bytes() == FAKE_FONT
    assert seen == ["https://a.example/f.otf"]


@pytest.mark.asyncio
async def test_download_falls_back_to_mirror(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "a.example":
            return httpx.Response(503)
        return httpx.Response(200, content=FAKE_FONT)

    path = await ra.download_font(tmp_path / "fonts", font=_font(), transport=httpx.MockTransport(handler))
    assert path.read_bytes() == FAKE_FONT


@pytest.mark.asyncio
async def test_download_bad_sha_deleted(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"OTTO" + b"y" * 1000)

    with pytest.raises(ra.AssetError) as ei:
        await ra.download_font(tmp_path / "fonts", font=_font(), transport=httpx.MockTransport(handler))
    assert "校验" in str(ei.value)
    left = list((tmp_path / "fonts").glob("*")) if (tmp_path / "fonts").exists() else []
    assert [p for p in left if p.suffix in (".otf", ".part")] == []


@pytest.mark.asyncio
async def test_download_too_big_rejected(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FAKE_FONT * 3)

    with pytest.raises(ra.AssetError):
        await ra.download_font(tmp_path / "fonts", font=_font(), transport=httpx.MockTransport(handler))
    assert not (tmp_path / "fonts" / "Test-Regular.otf").exists()


# ----------------------------------------------------------------------
# 字体探测三态
# ----------------------------------------------------------------------


def test_font_state_three_ways(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    assert ra.font_state(fonts, system=[], font=_font())["state"] == "missing"
    assert ra.font_state(fonts, system=["Noto Sans CJK SC"], font=_font())["state"] == "system"
    fonts.mkdir()
    (fonts / "Test-Regular.otf").write_bytes(FAKE_FONT)
    assert ra.font_state(fonts, system=["Noto Sans CJK SC"], font=_font())["state"] == "ok"


def test_system_cjk_from_fc_list() -> None:
    def runner(argv):
        assert argv[0] == "fc-list"
        return "Noto Sans CJK SC,Noto Sans CJK SC Regular\nNoto Sans CJK SC\n"

    assert ra.system_cjk_fonts(fc_runner=runner, dirs=()) == ["Noto Sans CJK SC"]


def test_system_cjk_scan_dirs(tmp_path: Path) -> None:
    d = tmp_path / "share" / "fonts" / "truetype"
    d.mkdir(parents=True)
    (d / "DejaVuSans.ttf").write_bytes(b"x")
    assert ra.system_cjk_fonts(fc_runner=lambda a: "", dirs=(tmp_path / "share",)) == []
    (d / "wqy-microhei.ttc").write_bytes(b"x")
    assert ra.system_cjk_fonts(fc_runner=lambda a: "", dirs=(tmp_path / "share",)) == ["wqy-microhei.ttc"]


def test_system_cjk_probe_failure_means_none() -> None:
    def boom(argv):
        raise FileNotFoundError("fc-list")

    assert ra.system_cjk_fonts(fc_runner=boom, dirs=()) == []


# ----------------------------------------------------------------------
# Chromium 安装命令 / 环境 / 错误解析
# ----------------------------------------------------------------------


def test_chromium_install_argv_no_deps(tmp_path: Path) -> None:
    argv = ra.chromium_install_argv("/opt/py/bin/python")
    assert argv == ["/opt/py/bin/python", "-m", "playwright", "install", "chromium"]
    assert "--with-deps" not in argv and "install-deps" not in argv


def test_chromium_env_points_to_own_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    env = ra.chromium_env(tmp_path / "ms-playwright", hostlibs=None)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "ms-playwright")
    assert "OPENAI_API_KEY" not in env
    assert "PYTHONPATH" not in env
    env2 = ra.chromium_env(tmp_path / "ms-playwright", hostlibs=tmp_path / "hostlibs")
    assert env2["PYTHONPATH"] == str(tmp_path / "hostlibs")


def test_parse_missing_libs() -> None:
    pw = (
        "BrowserType.launch: \n╔══════╗\n║ Host system is missing dependencies to run browsers. ║\n"
        "║ Missing libraries:                                   ║\n║     libnss3.so                ║\n"
        "║     libnspr4.so   ║\n║     libatk-1.0.so.0 ║\n║     libgbm.so.1 ║\n"
    )
    assert ra.parse_missing_libs(pw) == ["libnss3.so", "libnspr4.so", "libatk-1.0.so.0", "libgbm.so.1"]
    so = "chrome: error while loading shared libraries: libxkbcommon.so.0: cannot open shared object file"
    assert ra.parse_missing_libs(so) == ["libxkbcommon.so.0"]
    assert ra.parse_missing_libs("Timeout 30000ms exceeded") == []


def test_explain_missing_libs_plain_words() -> None:
    text = ra.explain_launch_error("error while loading shared libraries: libnss3.so: cannot open")
    assert "浏览器下载好了" in text and "libnss3.so" in text
    assert "playwright install-deps chromium" in text and "root" in text
    assert "改发文字" in text
    other = ra.explain_launch_error("boom")
    assert "boom" in other and "install-deps" not in other


def test_find_local_chromium(tmp_path: Path) -> None:
    root = tmp_path / "ms-playwright"
    assert ra.find_chromium(root) is None
    exe = root / "chromium_headless_shell-1208" / "chrome-linux" / "headless_shell"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert ra.find_chromium(root) == exe


# ----------------------------------------------------------------------
# RenderAssets：装 / 探测（假 runner）
# ----------------------------------------------------------------------


def _probe_out(**kw: Any) -> str:
    return ra.PROBE_MARK + json.dumps(kw) + "\n"


class _Runner:
    def __init__(self, probe: dict[str, Any], *, install_code: int = 0, make_exe: Path | None = None) -> None:
        self.probe = probe
        self.install_code = install_code
        self.make_exe = make_exe
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def __call__(self, argv, *, env, timeout):
        self.calls.append((list(argv), dict(env)))
        if argv[1:3] == ["-m", "playwright"]:
            if self.make_exe is not None:
                self.make_exe.parent.mkdir(parents=True, exist_ok=True)
                self.make_exe.write_text("x")
                self.make_exe.chmod(0o755)
            return self.install_code, "downloaded", ""
        if argv[1] == "-c":
            return 0, _probe_out(**self.probe), ""
        return 0, "", ""


@pytest.mark.asyncio
async def test_browser_install_flow_ok(tmp_path: Path, store: Store) -> None:
    root = tmp_path
    exe = root / "ms-playwright" / "chromium_headless_shell-1" / "chrome-linux" / "headless_shell"
    runner = _Runner({"playwright": True, "launch": "ok"}, make_exe=exe)
    assets = ra.RenderAssets(store, lambda: _Settings(root), runner=runner, python="/py", system_fonts=lambda: [])
    assets.start_install("browser")
    await assets.wait()
    install = [c for c in runner.calls if c[0][1:3] == ["-m", "playwright"]]
    assert install and install[0][0] == ["/py", "-m", "playwright", "install", "chromium"]
    assert install[0][1]["PLAYWRIGHT_BROWSERS_PATH"] == str(root / "ms-playwright")
    g = {x["id"]: x for x in assets.status_groups()}["browser"]
    assert g["state"] == "ok" and g["last_install"]["state"] == "ok"


@pytest.mark.asyncio
async def test_browser_install_pips_playwright_first(tmp_path: Path, store: Store) -> None:
    runner = _Runner({"playwright": False})
    assets = ra.RenderAssets(store, lambda: _Settings(tmp_path), runner=runner, python="/py", system_fonts=lambda: [])
    assets.start_install("browser")
    await assets.wait()
    pip = [c for c in runner.calls if c[0][1:3] == ["-m", "pip"]]
    assert pip and "--target" in pip[0][0] and pip[0][0][-1].startswith("playwright")
    assert pip[0][0][pip[0][0].index("--target") + 1] == str(tmp_path / "hostlibs")


@pytest.mark.asyncio
async def test_browser_missing_libs_hint(tmp_path: Path, store: Store) -> None:
    exe = tmp_path / "ms-playwright" / "chromium-1" / "chrome-linux" / "chrome"
    runner = _Runner(
        {"playwright": True, "launch": "failed",
         "launch_error": "Host system is missing dependencies to run browsers.\n libnss3.so\n libgbm.so.1"},
        make_exe=exe,
    )
    assets = ra.RenderAssets(store, lambda: _Settings(tmp_path), runner=runner, python="/py", system_fonts=lambda: [])
    assets.start_install("browser")
    await assets.wait()
    g = {x["id"]: x for x in assets.status_groups()}["browser"]
    assert g["state"] == "broken"
    assert "libnss3.so" in g["hint"] and "install-deps" in g["hint"]
    assert g["last_install"]["state"] == "failed"


@pytest.mark.asyncio
async def test_font_install_and_status(tmp_path: Path, store: Store) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FAKE_FONT)

    assets = ra.RenderAssets(
        store, lambda: _Settings(tmp_path), runner=_Runner({"playwright": False}), python="/py",
        transport=httpx.MockTransport(handler), font=_font(), system_fonts=lambda: [],
    )
    await assets.refresh()
    assert {x["id"]: x for x in assets.status_groups()}["fonts"]["state"] == "missing"
    assets.start_install("fonts")
    await assets.wait()
    g = {x["id"]: x for x in assets.status_groups()}["fonts"]
    assert g["state"] == "ok"
    assert (tmp_path / "fonts" / "Test-Regular.otf").is_file()
    assert (tmp_path / "fonts" / "fonts.conf").is_file()
    assets.start_uninstall("fonts")
    await assets.wait()
    assert not (tmp_path / "fonts" / "Test-Regular.otf").exists()
    assert {x["id"]: x for x in assets.status_groups()}["fonts"]["state"] == "missing"


@pytest.mark.asyncio
async def test_optional_deps_merges_asset_groups(tmp_path: Path, store: Store) -> None:
    assets = ra.RenderAssets(store, lambda: _Settings(tmp_path), runner=_Runner({"playwright": False}),
                             python="/py", system_fonts=lambda: [])
    deps = od.OptionalDeps(store, lambda: _Settings(tmp_path), env=None, assets=assets)
    view = deps.status()
    ids = [g["id"] for g in view["groups"]]
    assert ids[0] == "office" and "fonts" in ids and "browser" in ids
    assert {g["id"]: g for g in view["groups"]}["fonts"]["available"] is True
    # 本机执行不可用也能装字体（字体 / 浏览器是给 MaiWork 自己画图用的，不走子 agent）
    deps.start_install("fonts")
    await deps.wait()


# ----------------------------------------------------------------------
# news_card：有字体带 @font-face；没有时与原来一致；启动时用本地浏览器
# ----------------------------------------------------------------------


_DATA = {"group_name": "测试群", "slot_label": "9月29日 · 下午", "items": [{"title": "标题", "brief": "一句话"}]}


def test_card_html_without_font_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(ra, "local_font_path", lambda: None)
    html = news_card.render_html(_DATA, now=0)
    assert "@font-face" not in html
    assert ra.FONT_FAMILY not in html


def test_card_html_with_font(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "x.otf"
    f.write_bytes(b"x")
    monkeypatch.setattr(ra, "local_font_path", lambda: f)
    html = news_card.render_html(_DATA, now=0)
    assert "@font-face" in html and ra.FONT_URL in html and ra.FONT_FAMILY in html
    # 去掉字体那几处，其余和没字体时一致
    monkeypatch.setattr(ra, "local_font_path", lambda: None)
    plain = news_card.render_html(_DATA, now=0)
    assert len(html) > len(plain)


@pytest.mark.asyncio
async def test_render_png_uses_local_browser(monkeypatch, tmp_path: Path) -> None:
    exe = tmp_path / "headless_shell"
    exe.write_text("x")
    monkeypatch.setattr(ra, "local_chromium", lambda: exe)
    monkeypatch.setattr(ra, "local_font_path", lambda: None)
    launched: dict[str, Any] = {}

    class _Loc:
        async def screenshot(self, **kw):
            raise RuntimeError("stop here")

    class _Page:
        async def route(self, *a, **k): ...
        async def set_content(self, *a, **k): ...
        def locator(self, sel): return _Loc()
        async def close(self): ...

    class _Browser:
        async def new_page(self, **kw): return _Page()
        async def close(self): ...

    class _Chromium:
        async def launch(self, **kw):
            launched.update(kw)
            return _Browser()

    class _PW:
        async def __aenter__(self): return types.SimpleNamespace(chromium=_Chromium())
        async def __aexit__(self, *a): return False

    fake = types.ModuleType("playwright.async_api")
    fake.async_playwright = lambda: _PW()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)

    async def no_covers(data, **kw): ...
    monkeypatch.setattr(news_card, "prepare_covers", no_covers)
    with pytest.raises(news_card.RenderError):
        await news_card.render_png(dict(_DATA), timeout_s=5)
    assert launched.get("executable_path") == str(exe)
