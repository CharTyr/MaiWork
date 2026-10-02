"""个人向资讯也要「写完对一遍原文」（线上巡检 2026-10-02 后补）。

以前个人向写完帖子直接落库，没有群资讯那道对原文自检；现在共用 feeds.check_post_bodies：
被点名有原文撑不住说法的那条正文回落摘要；自检自己失败，改写过的正文全部回落摘要。
"""

from __future__ import annotations

import json

from fakes import FakeModelsQueue
from test_personal import GID, UID, _make_personal, _personal_scores_json, _run, _time_patch


def _focus() -> str:
    return json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False)


def _posts() -> str:
    return json.dumps({"posts": [
        {"i": 0, "title": "新开源 FPGA 学习板上架", "body": "你在弄 FPGA，这块板子今天官方刚发布。",
         "reason": "你在弄这个", "refs": [], "audience": [], "keywords": ["FPGA"]},
        {"i": 1, "title": "本地小模型量化教程", "body": "这篇教程讲小机器跑量化。",
         "reason": "你在弄这个", "refs": [], "audience": [], "keywords": ["量化"]},
    ]}, ensure_ascii=False)


def _bodies(store) -> dict:
    rows = store.read().execute(
        "SELECT title, body, summary FROM news_items WHERE target_user_id=? AND rejected=0", (UID,)
    ).fetchall()
    return {r["title"]: (r["body"], r["summary"]) for r in rows}


def test_personal_posts_are_checked_against_source(tmp_path) -> None:
    check = json.dumps({"unsupported": [{"i": 0, "phrases": ["今天官方刚发布"]}]}, ensure_ascii=False)
    models = FakeModelsQueue(ready=True, replies=[_focus(), _personal_scores_json(), _posts(), check])
    store, _s, personal, models, *_ = _make_personal(tmp_path, models=models)
    with _time_patch():
        assert _run(personal.prepare_personal(GID, UID)) == 2
    purposes = [str(k.get("purpose") or "") for _r, _m, k in models.calls]
    assert "personal.post_check" in purposes
    b = _bodies(store)
    body0, summary0 = b["新开源 FPGA 学习板上架"]
    assert "今天官方刚发布" not in body0 and body0 == summary0
    assert b["本地小模型量化教程"][0] == "这篇教程讲小机器跑量化。"


def test_personal_check_failure_falls_back_to_summary(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_focus(), _personal_scores_json(), _posts(), "不是 JSON"])
    store, _s, personal, models, *_ = _make_personal(tmp_path, models=models)
    with _time_patch():
        assert _run(personal.prepare_personal(GID, UID)) == 2
    for body, summary in _bodies(store).values():
        assert body == summary
