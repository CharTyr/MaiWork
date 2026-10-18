"""群控页「功能启用」区块接上了按群开关（资讯 / 构想 / 任务）。

静态契约（.trellis/tasks/10-18-independent-feature-switches）：
- 加载时连同 push / approval 一起拉取 /features；
- 三个开关 news / idea / task 进草稿与提交体；
- 任务关闭的提示（连带目标、手动恢复）在页面上有文案；
- 保存走 PUT /api/groups/{gid}/features。
"""

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"


def _text(name: str) -> str:
    return (JS / "pages" / name).read_text(encoding="utf-8")


def test_groupcontrols_loads_features_with_push_and_approval() -> None:
    text = _text("groupcontrols.js")
    assert "/features" in text
    assert "c.features = features" in text
    # 三个开关键
    assert 'FEATURE_KEYS = ["news", "idea", "task"]' in text


def test_groupcontrols_features_block_and_task_off_copy() -> None:
    text = _text("groupcontrols.js")
    assert "功能启用" in text
    # 任务关闭的连带提示（目标检查 / 手动恢复）
    assert "目标检查" in text
    assert "手动恢复" in text
    # 保存走 features 接口
    assert 'data-act="gctl-features-edit"' in text


def test_held_tasks_have_explanation_and_manual_resume():
    detail = (JS / "detail.js").read_text(encoding="utf-8")
    assert 'r.kind === "feature_off"' in detail
    assert 'b("resume", "恢复"' in detail
    assert "重新开启" in detail
    tasks = _text("tasks.js")
    assert 't.paused_reason.kind === "feature_off"' in tasks
    assert "等待手动恢复" in tasks
