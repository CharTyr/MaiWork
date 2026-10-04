"""口味小结 / 自动反馈 / 优质来源接进资讯提示词（docs/10 第七节第 4、6、7 步的最后一段）。

- 定关注点、打分、写帖子三处都带口味小结（taste.prompt_block：管理员偏好在前）；
- 定关注点的「群友觉得有用」也算上自动好评（回复卡片 / 点开原文 / 群里接着聊），带上原因；
- 找资讯的 brief 带本群优质来源（可以用 site 直奔，最多约三分之一的搜索）。
"""

from __future__ import annotations

import json

from test_feeds_quality import _TimePatch, _make_feeds, _run, GID, NOW

from CharTyr_MaiWork.maiwork import news_feedback


def _prompts(models):
    return {c[2].get("purpose"): c[1][0]["content"] for c in models.calls}


def test_news_skill_in_focus_score_and_post_prompts(tmp_path):
    """每群三份（docs/17 §八.2）：news 岗 skill 正文进 定关注点/ 打分 / 写帖子。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    # 手工写进 news 岗 skill 正文（这是「本群做法」——taste 的换血位置）
    from types import SimpleNamespace

    from CharTyr_MaiWork.maiwork import agents as agents_mod

    class _Settings:
        served_groups = (GID,)
        data_dir = str(tmp_path)

        def is_served(self, gid: str) -> bool:
            return str(gid) in self.served_groups

    agents_mod.Agents._ensure_schema = agents_mod.Agents._ensure_schema  # noqa: SLF001
    agents = agents_mod.Agents(store, lambda: _Settings())
    agents._ensure_schema()
    feeds._agents = agents  # noqa: SLF001  # feeds._group_context_safe 会在 _specialists=None 时回落到 _agents
    try:
        agents.skill_add(GID, "news", description="", body="爱看开发内幕，不爱营销稿")
    except Exception:
        pass
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
