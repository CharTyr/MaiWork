"""viz_spec 的 image 零件（2026-10-15 任务 A1）：只有 report 用途有、路径校验严、渲染不写脚本。

先写测试再写实现（见 AGENTS.md「先写测试再写实现」）。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import viz_spec as vs


def _img(**kw) -> dict:
    b = {"type": "image", "path": "images/hero.png", "alt": "封面：一只猫"}
    b.update(kw)
    return b


# ----------------------------------------------------------------------
# 零件清单
# ----------------------------------------------------------------------


def test_image_part_is_report_only() -> None:
    assert "image" in vs.CATALOG
    assert vs.CATALOG["image"]["profiles"] == ("report",)
    report = {k for k, v in vs.CATALOG.items() if "report" in v["profiles"]}
    news = {k for k, v in vs.CATALOG.items() if "news" in v["profiles"]}
    assert "image" in report and "image" not in news


def test_catalog_prompt_documents_image_for_report_only() -> None:
    report = vs.catalog_prompt("report")
    assert "image" in report
    assert "images/" in report  # 说明里要讲清路径长什么样
    assert "image" not in vs.catalog_prompt("news")


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


def test_validate_accepts_image_and_keeps_fields() -> None:
    spec = vs.validate(
        {"title": "报告", "blocks": [_img(caption="图下小字")]}, "report"
    )
    b = spec["blocks"][0]
    assert b == {
        "type": "image",
        "path": "images/hero.png",
        "alt": "封面：一只猫",
        "caption": "图下小字",
    }


def test_validate_accepts_workspace_relative_path() -> None:
    """generate_image 返回的是工作区相对路径（artifacts/<任务>/images/x.png），也认。"""
    spec = vs.validate(
        {"title": "报告", "blocks": [_img(path="artifacts/T-1/images/hero.png")]}, "report"
    )
    assert spec["blocks"][0]["path"] == "artifacts/T-1/images/hero.png"


def test_validate_rejects_image_in_news() -> None:
    with pytest.raises(vs.SpecError) as ei:
        vs.validate({"blocks": [_img()]}, "news")
    assert "不能用" in str(ei.value)


@pytest.mark.parametrize("path,why", [
    ("", "不能为空"),
    ("/etc/passwd.png", "相对路径"),
    ("../secret.png", "相对路径"),
    ("images/../../secret.png", "相对路径"),
    ("images\\hero.png", "相对路径"),
    ("images/hero.txt", "图片"),
    ("images/hero", "图片"),
    ("images/" + "x" * 300 + ".png", "太长"),
])
def test_validate_rejects_bad_image_path(path: str, why: str) -> None:
    with pytest.raises(vs.SpecError) as ei:
        vs.validate({"title": "报告", "blocks": [_img(path=path)]}, "report")
    assert why in str(ei.value)


def test_validate_requires_alt_text() -> None:
    with pytest.raises(vs.SpecError) as ei:
        vs.validate({"title": "报告", "blocks": [_img(alt="")]}, "report")
    assert "alt" in str(ei.value)


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------


def test_render_image_uses_inlined_src_and_escapes_alt() -> None:
    spec = vs.validate(
        {"title": "报告", "blocks": [_img(alt='<script>alert(1)</script>', caption="图说")]},
        "report",
    )
    spec["blocks"][0]["src"] = "data:image/png;base64,AAAA"
    html = vs.render(spec, "r")
    assert 'src="data:image/png;base64,AAAA"' in html
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html
    assert "图说" in html


def test_render_skips_image_without_inlined_src() -> None:
    """没内联成功（没 src）的图不画成裂图——render_report 会先报错，这里是双保险。"""
    spec = vs.validate({"title": "报告", "blocks": [_img()]}, "report")
    html = vs.render(spec, "r")
    assert "<img" not in html


def test_page_csp_still_only_allows_data_images() -> None:
    """CSP 不放宽：图必须走 data: 内联，页面不许引外部资源。"""
    spec = vs.validate({"title": "报告", "blocks": [_img()]}, "report")
    spec["blocks"][0]["src"] = "data:image/png;base64,AAAA"
    doc = vs.page(spec, uid="r", report=True)
    assert "img-src data:" in doc and "default-src 'none'" in doc
    assert doc.count("<script") == 0
    # 页面里只有内联的那张图，没有别的外部资源
    assert 'src="data:image/png;base64,AAAA"' in doc


def test_texts_of_includes_image_alt() -> None:
    spec = vs.validate({"title": "报告", "blocks": [_img()]}, "report")
    assert "封面：一只猫" in vs.texts_of(spec)


def test_catalog_prompt_states_report_limits_that_validate_enforces():
    """M8（2026-10-15 巡检）：给模型看的限制要和校验对得上，报告页的宽限要写出来，免得白白被拒或自我设限。"""
    from CharTyr_MaiWork.maiwork import viz_spec

    rep = viz_spec.catalog_prompt("report")
    for frag in ("表格最多 40 行", "要点 1~30 条", "小标题最长 80 字", "来源最多 40 条", "标签页最多 12 组", "页签名最长 16 字"):
        assert frag in rep, frag
    news = viz_spec.catalog_prompt("news")
    assert "页签名最长 16 字" in news and "表格最多 40 行" not in news
    # 和校验真的一致：40 行表、30 条要点、16 字页签在报告页都能过
    viz_spec.validate({"title": "t", "blocks": [
        {"type": "table", "columns": ["a", "b"], "rows": [["x", "1"]] * 40},
        {"type": "list", "items": ["一"] * 30},
        {"type": "tabs", "tabs": [{"label": "页" * 16, "blocks": [{"type": "text", "text": "x"}]},
                                  {"label": "二", "blocks": [{"type": "text", "text": "y"}]}]},
    ]}, "report")
