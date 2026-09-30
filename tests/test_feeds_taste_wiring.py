"""口味小结 / 自动反馈 / 优质来源接进资讯提示词（docs/10 第七节第 4、6、7 步的最后一段）。

- 定关注点、打分、写帖子三处都带口味小结（taste.prompt_block：管理员偏好在前）；
- 定关注点的「群友觉得有用」也算上自动好评（回复卡片 / 点开原文 / 群里接着聊），带上原因；
- 找资讯的 brief 带本群优质来源（可以用 site 直奔，最多约三分之一的搜索）。
"""

from __future__ import annotations

import json

from test_feeds_quality import _TimePatch, _make_feeds, _run, GID, NOW

from CharTyr_MaiWork.maiwork import news_feedback, taste


def _prompts(models):
    return {c[2].get("purpose"): c[1][0]["content"] for c in models.calls}


def test_taste_in_focus_score_and_post_prompts(tmp_path):
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    taste.set_manual(store, GID, "这个群爱看开发内幕，不爱营销稿", NOW)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    p = _prompts(models)
    for purpose in ("feeds.focus", "feeds.score", "feeds.post"):
        assert "爱看开发内幕" in p.get(purpose, ""), purpose


def test_auto_positive_feedback_in_focus_prompt(tmp_path):
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, rejected, created) VALUES (1, ?, ?, 'x.example/1', 0, ?)",
            (GID, "被回复过的那条资讯", NOW - 3600),
        )
        iid = int(cur.lastrowid)
    news_feedback.record(store, GID, iid, "reply", actor="a", message_id="m", now=NOW - 1800)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    focus = _prompts(models)["feeds.focus"]
    assert "被回复过的那条资讯" in focus and "有人回复卡片" in focus


def test_trusted_sources_in_focus_prompt(tmp_path):
    """本群优质来源：定关注点提示词带（2026-10-01 起撒网由代码按计划搜，这段从子 agent brief 挪这）。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with store.tx() as conn:
        for i in range(2):
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, scores, rejected, created)"
                " VALUES (1, ?, ?, ?, ?, ?, 0, ?)",
                (GID, f"t{i}", f"gcores.com/{i}", json.dumps([{"site": "gcores.com"}]), json.dumps({"avg": 4.5}), NOW - 3600),
            )
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    focus = _prompts(models)["feeds.focus"]
    assert "gcores.com" in focus and "三分之一" in focus
