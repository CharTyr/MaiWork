"""图解零件清单（viz_spec.py）：校验、渲染只出转义文字、没有脚本/外部资源、两种用途的边界。"""

from __future__ import annotations

import re

import pytest

from CharTyr_MaiWork.maiwork import viz_spec as vs

GOOD = {
    "title": "GPT-6 Sol 跑分",
    "caption": "和上一代比",
    "blocks": [
        {"type": "stats", "items": [
            {"value": 43, "unit": "%", "label": "Terminal-Bench", "change": "+6"},
            {"value": 58, "unit": "%", "label": "SWE-Atlas-QnA"},
        ]},
        {"type": "compare", "unit": "%", "items": [{"label": "Sol", "value": 58, "tone": "good"}, {"label": "Luna", "value": 44}]},
        {"type": "tabs", "tabs": [
            {"label": "价格", "blocks": [{"type": "bars", "unit": "$", "items": [{"label": "输入", "value": 2}, {"label": "输出", "value": 10}]}]},
            {"label": "时间", "blocks": [{"type": "timeline", "items": [{"when": "9 月 22 日", "text": "发布"}, {"when": "之后", "text": "降价"}]}]},
        ]},
        {"type": "takeaway", "text": "Sol 更强、价格降了 50%"},
    ],
}


def test_validate_and_render_good() -> None:
    spec = vs.validate(GOOD, "news")
    html = vs.render(spec, "n12")
    assert html.startswith('<div class="vs">')
    assert "Terminal-Bench" in html and "58" in html
    assert 'name="n12-t1"' in html and "checked" in html
    assert "<script" not in html.lower()


def test_text_is_escaped() -> None:
    bad = {"blocks": [{"type": "takeaway", "text": '<img src=x onerror="alert(1)"><script>x</script>'}]}
    html = vs.render(vs.validate(bad, "news"))
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("spec,why", [
    ({"blocks": []}, "blocks"),
    ({"blocks": [{"type": "html", "html": "<b>x</b>"}]}, "不在零件清单"),
    ({"blocks": [{"type": "heading", "text": "x"}]}, "不能用"),  # 资讯不许小标题
    ({"blocks": [{"type": "sources", "items": [{"title": "a", "url": "https://a.com"}]}]}, "不能用"),
    ({"blocks": [{"type": "stat", "value": "12 亿", "label": "x"}]}, "数字"),
    ({"blocks": [{"type": "share", "items": [{"label": "a", "value": -1}, {"label": "b", "value": 2}]}]}, "负数"),
    ({"blocks": [{"type": "compare", "items": [{"label": "a", "value": 1}]}]}, "2~8"),
    ({"blocks": [{"type": "takeaway", "text": "x"}] * 5}, "太多"),
    ({"blocks": [{"type": "tabs", "tabs": [
        {"label": "a", "blocks": [{"type": "tabs", "tabs": []}]}, {"label": "b", "blocks": [{"type": "takeaway", "text": "x"}]}]}]}, "套"),
    ({"blocks": [{"type": "table", "columns": ["a", "b"], "rows": [["1"]]}]}, "表头"),
])
def test_validate_rejects(spec, why) -> None:
    with pytest.raises(vs.SpecError) as ei:
        vs.validate(spec, "news")
    assert why in str(ei.value)


def test_report_profile_allows_rich_blocks_and_safe_links() -> None:
    spec = vs.validate({
        "title": "调研报告",
        "blocks": [
            {"type": "heading", "text": "结论"},
            {"type": "text", "text": "正文"},
            {"type": "note", "text": "有一项不确定"},
            {"type": "sources", "items": [{"title": "官网", "url": "https://a.com/x?y=1&z=2"}]},
        ],
    }, "report")
    doc = vs.page(spec, report=True, foot="MaiWork 生成")
    assert '<h1 class="rp-h">调研报告</h1>' in doc
    assert 'href="https://a.com/x?y=1&amp;z=2"' in doc
    assert "default-src 'none'" in doc
    assert "<script" not in doc.lower()
    for bad in ("javascript:alert(1)", "//evil.com", 'https://a.com/"onmouseover=x'):
        with pytest.raises(vs.SpecError):
            vs.validate({"title": "t", "blocks": [{"type": "sources", "items": [{"url": bad}]}]}, "report")


def test_report_requires_title() -> None:
    with pytest.raises(vs.SpecError):
        vs.validate({"blocks": [{"type": "text", "text": "x"}]}, "report")


def test_page_has_no_external_resources() -> None:
    doc = vs.page(vs.validate(GOOD, "news"))
    assert not re.search(r"(?i)(src|href)\s*=\s*['\"]?(https?:|//)", doc)
    assert "@import" not in doc and "url(" not in doc
    assert ".vs-stat" in doc  # 样式内嵌（同一份 viz.css）


def test_texts_of_lists_displayed_numbers() -> None:
    t = vs.texts_of(vs.validate(GOOD, "news"))
    assert "43" in t and "58" in t and "+6" in t and "10" in t and "Sol 更强、价格降了 50%" in t


def test_numbers_render_as_given() -> None:
    spec = vs.validate({"blocks": [{"type": "stat", "value": 0.10, "unit": "$", "label": "x"}]}, "news")
    assert ">0.1<" in vs.render(spec)
    assert vs.texts_of(spec)[0] == "0.1"


def test_catalog_prompt_by_profile() -> None:
    news = vs.catalog_prompt("news")
    rep = vs.catalog_prompt("report")
    assert "stat" in news and "heading" not in news and "sources" not in news
    assert "heading" in rep and "sources" in rep


def test_css_is_single_source() -> None:
    from pathlib import Path

    css = (Path(vs.__file__).parent / "console" / "static" / "viz.css").read_text(encoding="utf-8")
    assert vs.CSS == css and ".vs-tabs" in css
    assert "url(" not in css and "@import" not in css
