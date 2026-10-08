"""交接包网页部分（docs/24 §三）：读源码文本的契约检查，不开浏览器。"""

from __future__ import annotations

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"
CSS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "style.css"

if not JS.is_dir():  # 找不到插件目录直接报错，不许 skip
    raise RuntimeError(f"找不到前端目录：{JS}")


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def test_handoff_module_calls_preview_and_counter():
    src = _read("handoff.js")
    assert "/api/handoff/" in src
    assert "/taken" in src, "复制 / 下载要记一次"
    assert "clientId()" in src, "计数用现有的浏览器随机标识"
    assert '"GET"' in src and '"POST"' in src


def test_handoff_download_is_local_markdown_file():
    src = _read("handoff.js")
    assert "new Blob(" in src and "text/markdown" in src, "下载在浏览器里直接存预览文字"
    assert ".download = " in src, "用服务端给的文件名"


def test_handoff_copy_has_manual_fallback():
    src = _read("handoff.js")
    assert "navigator.clipboard" in src
    assert ".select()" in src, "复制失败时选中文本让人手动复制"


def test_handoff_ignores_stale_responses():
    src = _read("handoff.js")
    assert "seq" in src, "快速连开两个时，旧请求的结果不能盖住新的"


def test_entry_buttons_on_idea_and_task_detail():
    ideas = _read("pages/ideas.js")
    detail = _read("detail.js")
    assert 'data-act="handoff" data-kind="idea"' in ideas
    assert 'data-act="handoff" data-kind="task"' in detail
    assert "交给我的 agent" in ideas and "交给我的 agent" in detail
    assert 'case "handoff"' in _read("actions.js")


def test_handoff_count_only_for_admins():
    for name in ("pages/ideas.js", "detail.js"):
        src = _read(name)
        assert "handoff_count" in src, name
        # 计数那一段必须挂在 gadmin() 判断下
        idx = src.index("handoff_count")
        near = src[idx : idx + 200]
        assert "gadmin() &&" in near and "被带走" in near, f"{name}：被带走次数只给管理员看"


def test_sheet_renders_handoff_and_returns_to_detail_on_phone():
    sheet = _read("sheet.js")
    assert 'kind === "handoff"' in sheet
    assert "handoffSheet" in sheet
    # 手机上从详情抽屉打开的，关掉要回到详情，不能把详情一起关没
    assert "back" in sheet


def test_handoff_idea_sends_picked_items():
    src = _read("handoff.js")
    assert "ideaPicked" in src and "items=" in src


def test_handoff_styles_exist():
    css = CSS.read_text(encoding="utf-8")
    assert ".ho-text" in css
