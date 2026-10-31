"""R10: public idea commands must identify visible numbered, actionable items."""
from CharTyr_MaiWork.maiwork.share import render_page


def test_r10_public_idea_has_explicit_item_numbers_and_escaped_content():
    page = render_page({"ideas": [{"id": 12, "title": "选做", "state": "new", "items": [
        {"no": 1, "kind": "task", "title": "做清单", "desc": "<原料>"},
        {"no": 2, "kind": "goal", "title": "盯更新", "desc": "版本发布"},
    ]}]}, tab="ideas")
    assert "1. 做清单" in page and "2. 盯更新" in page
    assert "&lt;原料&gt;" in page
    assert "/mw 派活 构想 #12" in page
    assert "<script" not in page and "<link" not in page


def test_r10_dismissed_and_completed_ideas_have_no_dispatch_command():
    for state in ("dismissed", "done", "completed"):
        page = render_page({"ideas": [{"id": 12, "title": "结束了", "state": state}]}, tab="ideas")
        assert "/mw 派活 构想 #12" not in page, state
        assert "@机器人 构想 #12" not in page, state
