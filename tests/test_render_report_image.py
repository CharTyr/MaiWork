"""render_report 的 image 零件（2026-10-15 任务 A1）：把任务产物目录里的图内联成 data URI。

规矩：只认本任务成品目录里、按内容是真图（PNG/JPEG/WebP/GIF）的文件；单张 ≤3MB、
整页合计 ≤3MB（网页要落成一个文件、本身还有 5MB 上限）；超了报中文错让子 agent 换小图。
全部用本地临时文件，不发网络请求。
"""

from __future__ import annotations

import base64

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork import tools_exec
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

from fakes import FakeHost

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + (64).to_bytes(4, "little") + b"WEBP" + b"\x00" * 64


@pytest.fixture
def setup(tmp_path):
    s, _ = load_settings({"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")}})
    env = LocalEnv(lambda: s)
    store = Store(tmp_path / "t.db")
    store.migrate()
    tools = Tools(store)
    register_exec_tools(tools, env=env, host=FakeHost(), get_settings=lambda: s)
    ws = env.workspace("ws-1")
    ctx = ToolContext(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker", workspace=ws,
                      artifact_scope=("artifacts/T-1",))
    yield tools, ctx, ws
    store.close()


def _put(ws, rel: str, data: bytes) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _report(*blocks, title="带图的报告") -> dict:
    return {"path": "artifacts/T-1/report.html", "title": title, "blocks": list(blocks)}


def _img(path="images/hero.png", alt="封面：一只猫", caption=""):
    b = {"type": "image", "path": path, "alt": alt}
    if caption:
        b["caption"] = caption
    return b


@pytest.mark.asyncio
async def test_image_inlined_as_data_uri(setup) -> None:
    tools, ctx, ws = setup
    _put(ws, "artifacts/T-1/images/hero.png", PNG)
    r = await tools.call("render_report", _report(_img(caption="这是图说")), ctx)
    assert r.ok, r.error
    doc = (ws / "artifacts/T-1/report.html").read_text(encoding="utf-8")
    want = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert want in doc
    assert "封面：一只猫" in doc and "这是图说" in doc
    # 单个 html 不会裂图：没有指回工作区 / 网络的外部引用
    assert "images/hero.png" not in doc
    assert "http://" not in doc and "https://" not in doc
    assert "default-src 'none'" in doc and "img-src data:" in doc


@pytest.mark.asyncio
async def test_image_accepts_workspace_relative_path(setup) -> None:
    """generate_image 返回的是 artifacts/<任务>/images/x.png，直接照抄也认。"""
    tools, ctx, ws = setup
    _put(ws, "artifacts/T-1/images/hero.png", JPEG)
    r = await tools.call("render_report", _report(_img(path="artifacts/T-1/images/hero.png")), ctx)
    assert r.ok, r.error
    doc = (ws / "artifacts/T-1/report.html").read_text(encoding="utf-8")
    assert "data:image/jpeg;base64," in doc


@pytest.mark.asyncio
@pytest.mark.parametrize("name,data,mime", [
    ("a.png", PNG, "image/png"),
    ("b.jpg", JPEG, "image/jpeg"),
    ("c.gif", GIF, "image/gif"),
    ("d.webp", WEBP, "image/webp"),
])
async def test_image_formats_by_content(setup, name, data, mime) -> None:
    tools, ctx, ws = setup
    _put(ws, f"artifacts/T-1/images/{name}", data)
    r = await tools.call("render_report", _report(_img(path=f"images/{name}")), ctx)
    assert r.ok, r.error
    doc = (ws / "artifacts/T-1/report.html").read_text(encoding="utf-8")
    assert f"data:{mime};base64," in doc


@pytest.mark.asyncio
async def test_image_missing_file_says_so(setup) -> None:
    tools, ctx, ws = setup
    r = await tools.call("render_report", _report(_img(path="images/nope.png")), ctx)
    assert not r.ok
    assert "图" in r.error and "images/nope.png" in r.error
    assert not (ws / "artifacts/T-1/report.html").exists()


@pytest.mark.asyncio
async def test_image_not_a_real_image_by_content(setup) -> None:
    """后缀是 .png、内容不是图 → 按内容拒（别把任意文件塞进交付页）。"""
    tools, ctx, ws = setup
    _put(ws, "artifacts/T-1/images/fake.png", b"just some text, not an image")
    r = await tools.call("render_report", _report(_img(path="images/fake.png")), ctx)
    assert not r.ok
    assert "图片" in r.error and "PNG" in r.error


@pytest.mark.asyncio
async def test_image_outside_task_dir_rejected(setup) -> None:
    """图在别的任务目录里 / 在任务目录外 → 拒（成品目录隔离）。"""
    tools, ctx, ws = setup
    _put(ws, "artifacts/T-2/images/other.png", PNG)
    _put(ws, "images/outside.png", PNG)
    for path in ("artifacts/T-2/images/other.png", "../images/outside.png"):
        r = await tools.call("render_report", _report(_img(path=path)), ctx)
        assert not r.ok, path
    assert not (ws / "artifacts/T-1/report.html").exists()


@pytest.mark.asyncio
async def test_image_symlink_escape_rejected(setup, tmp_path) -> None:
    """指向任务目录外的符号链接：不读、不内联。"""
    tools, ctx, ws = setup
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    d = ws / "artifacts" / "T-1" / "images"
    d.mkdir(parents=True, exist_ok=True)
    (d / "link.png").symlink_to(outside)
    r = await tools.call("render_report", _report(_img(path="images/link.png")), ctx)
    assert not r.ok


@pytest.mark.asyncio
async def test_image_single_size_limit(setup, monkeypatch) -> None:
    tools, ctx, ws = setup
    monkeypatch.setattr(tools_exec, "_IMG_ONE_MAX", 1024)
    _put(ws, "artifacts/T-1/images/big.png", PNG + b"\x00" * 2000)
    r = await tools.call("render_report", _report(_img(path="images/big.png")), ctx)
    assert not r.ok
    assert "单张" in r.error and "小图" in r.error


@pytest.mark.asyncio
async def test_image_page_total_limit(setup, monkeypatch) -> None:
    tools, ctx, ws = setup
    monkeypatch.setattr(tools_exec, "_IMG_PAGE_MAX", 3000)
    for i in range(3):
        _put(ws, f"artifacts/T-1/images/p{i}.png", PNG + b"\x00" * 1500)
    blocks = [_img(path=f"images/p{i}.png") for i in range(3)]
    r = await tools.call("render_report", _report(*blocks), ctx)
    assert not r.ok
    assert "整页" in r.error and "压小" in r.error


def test_image_limits_are_documented_values() -> None:
    """上限就是对外承诺的那两个数（单张 3MB / 整页合计 3MB）。

    整页为什么也是 3MB：报告是用 write_file 写的，工作区单文件硬上限 5MB，data URI 放大约
    4/3——原来写 12MB 时，两张 2MB 的图会在写文件那一步报「单个文件不能超过 5MB」，
    和清单里承诺的上限自相矛盾（见下面两条真 env 用例）。
    """
    assert tools_exec._IMG_ONE_MAX == 3 * 1024 * 1024
    assert tools_exec._IMG_PAGE_MAX == 3 * 1024 * 1024


def _big_png(size: int) -> bytes:
    """一张指定字节数的「真 PNG」（按内容认图，不需要真解码）。"""
    head = b"\x89PNG\r\n\x1a\n"
    return head + b"\x00" * (size - len(head))


@pytest.mark.asyncio
async def test_two_2mb_images_rejected_before_writing(setup) -> None:
    """两张各 2MB（单张都没超）：在内联阶段就拒，并给准确中文原因——不许写坏页面。

    这正是 12MB 旧上限下会「装进去、然后写文件报 5MB」的那个组合。
    """
    tools, ctx, ws = setup
    for name in ("a.png", "b.png"):
        _put(ws, f"artifacts/T-1/images/{name}", _big_png(2 * 1024 * 1024))
    r = await tools.call("render_report", _report(_img(path="images/a.png"), _img(path="images/b.png")), ctx)
    assert not r.ok
    assert "整页" in r.error and "3MB" in r.error and "5MB" in r.error
    assert not (ws / "artifacts/T-1/report.html").exists()   # 没写出半张图的页面


@pytest.mark.asyncio
async def test_images_within_page_budget_write_a_single_html(setup) -> None:
    """整页预算内（两张各 1MB）：正常内联并写成一个 html，且 html 本身没超 5MB。"""
    tools, ctx, ws = setup
    for name in ("a.png", "b.png"):
        _put(ws, f"artifacts/T-1/images/{name}", _big_png(1024 * 1024))
    r = await tools.call("render_report", _report(_img(path="images/a.png"), _img(path="images/b.png")), ctx)
    assert r.ok, r.error
    f = ws / "artifacts/T-1/report.html"
    doc = f.read_text(encoding="utf-8")
    assert doc.count("data:image/png;base64,") == 2
    assert f.stat().st_size < 5 * 1024 * 1024


@pytest.mark.asyncio
async def test_report_without_image_still_works(setup) -> None:
    tools, ctx, ws = setup
    r = await tools.call("render_report", _report({"type": "text", "text": "没有图"}), ctx)
    assert r.ok, r.error


def test_render_report_description_mentions_image_part(setup) -> None:
    tools, _ctx, _ws = setup
    specs = {s["function"]["name"]: s["function"] for s in tools.specs("worker")}
    desc = specs["render_report"]["description"]
    assert "image" in desc and "images/" in desc
