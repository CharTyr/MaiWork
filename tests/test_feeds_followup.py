"""重复 vs 后续进展 + 写帖子后对原文自检（docs/10 第七节第 5 步，2026-09-30）。

- 打分模型判「和最近发过的某条」的关系 relation：duplicate（同样的事实换个说法）/ update（有新版本、
  新数字、新决定、新结果）/ context（补背景，事实没变）/ unrelated；update 必须写出 new_fact（比上次多了什么），
  写不出就按重复拒；
- update 放行，入库带 followup={of_title, new_fact}，网页标「后续」；同一件事这轮最多放一条后续（留分高的）；
- context 不算重复，照常打分；duplicate 照旧硬拒；
- 写完帖子再对一遍原文：自检说某条帖子有原文撑不住的说法 → 那条正文回落原摘要；自检失败不拖累出资讯。
"""

from __future__ import annotations

import json

from fakes import FakeModelsQueue
from test_feeds_freshness import (
    GID, _FOCUS_JSON, _TimePatch, _cand, _insert_published, _make_feeds, _ok_report, _post, _posts_json,
    _rejected_rows, _run, _score, _scores_json, FakeWorkers,
)

PUBLISHED = "《我的世界：地下城2》下周发售"


def _accepted(store):
    return store.read().execute("SELECT * FROM news_items WHERE rejected=0 AND title!=? ORDER BY id", (PUBLISHED,)).fetchall()


def _feeds(tmp_path, scores, items, extra_replies=()):
    return _make_feeds(
        tmp_path,
        models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, scores, *extra_replies]),
        workers=FakeWorkers(_ok_report({"items": items})),
    )


def test_update_with_new_fact_is_kept_and_marked(tmp_path):
    items = [_cand(0, url="https://a.com/dungeons2-interview", title="制作人详解《地下城2》开放世界和通关后玩法")]
    scores = _scores_json(_score(0, topic="我的世界", dup_of="R1", relation="update",
                                 new_fact="制作人首次讲了通关后玩法和装备构筑"))
    store, settings, feeds, models, *_ = _feeds(tmp_path, scores, items, [_posts_json(_post(0, "t"))])
    with _TimePatch():
        _insert_published(store, GID, title=PUBLISHED, topic="我的世界", url_key="old.com/1")
        assert _run(feeds.prepare_news(GID)) == 1
    row = _accepted(store)[0]
    fu = json.loads(row["followup"])
    assert fu["new_fact"] == "制作人首次讲了通关后玩法和装备构筑"
    assert fu["of_title"].startswith("《我的世界")


def test_update_without_new_fact_rejected_as_duplicate(tmp_path):
    items = [_cand(0, url="https://a.com/x", title="《地下城2》下周发售，别忘了")]
    scores = _scores_json(_score(0, topic="我的世界", dup_of="R1", relation="update", new_fact=""))
    store, settings, feeds, *_ = _feeds(tmp_path, scores, items)
    with _TimePatch():
        _insert_published(store, GID, title=PUBLISHED, topic="我的世界", url_key="old.com/1")
        assert _run(feeds.prepare_news(GID)) == 0
    assert "同一件事" in _rejected_rows(store)[0]["reject_reason"]


def test_same_as_recent_but_update_is_kept(tmp_path):
    items = [_cand(0, url="https://a.com/y", title="《地下城2》首周销量公布")]
    scores = _scores_json(_score(0, topic="我的世界", same_as_recent=True, relation="update", new_fact="首周销量 200 万"))
    store, settings, feeds, *_ = _feeds(tmp_path, scores, items, [_posts_json(_post(0, "t"))])
    with _TimePatch():
        _insert_published(store, GID, title=PUBLISHED, topic="我的世界", url_key="old.com/1")
        assert _run(feeds.prepare_news(GID)) == 1


def test_context_not_treated_as_duplicate(tmp_path):
    items = [_cand(0, url="https://a.com/z", title="《地下城》系列十年回顾")]
    scores = _scores_json(_score(0, topic="我的世界", same_as_recent=True, relation="context"))
    store, settings, feeds, *_ = _feeds(tmp_path, scores, items, [_posts_json(_post(0, "t"))])
    with _TimePatch():
        _insert_published(store, GID, title=PUBLISHED, topic="我的世界", url_key="old.com/1")
        assert _run(feeds.prepare_news(GID)) == 1
    assert _accepted(store)[0]["followup"] in ("", None)


def test_only_one_update_per_event_per_round(tmp_path):
    items = [
        _cand(0, url="https://a.com/u1", title="制作人详解《地下城2》开放世界"),
        _cand(1, url="https://b.com/u2", title="《地下城2》首周销量公布"),
    ]
    scores = _scores_json(
        _score(0, topic="我的世界", dup_of="R1", relation="update", new_fact="讲了开放世界", info=4),
        _score(1, topic="我的世界B", dup_of="R1", relation="update", new_fact="首周销量", info=5),
    )
    store, settings, feeds, *_ = _feeds(tmp_path, scores, items, [_posts_json(_post(0, "t"), _post(1, "t"))])
    with _TimePatch():
        _insert_published(store, GID, title=PUBLISHED, topic="我的世界", url_key="old.com/1")
        assert _run(feeds.prepare_news(GID)) == 1
    kept = _accepted(store)
    assert "销量" in kept[0]["title"]
    assert any("后续" in (r["reject_reason"] or "") for r in _rejected_rows(store))


def test_score_prompt_explains_relation(tmp_path):
    store, settings, feeds, models, *_ = _feeds(tmp_path, _scores_json(_score(0)), [_cand(0)], [_posts_json(_post(0, "t"))])
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    # 打分那次调用的提示词（两阶段后队列里隔着 feeds.pick，不能按下标 1 拿）
    score_call = next(c for c in models.calls if str(c[2].get("purpose") or "") == "feeds.score")
    prompt = str(score_call[1][0]["content"])
    assert "relation" in prompt and "new_fact" in prompt and "update" in prompt


def test_faithfulness_check_falls_back_unsupported_post(tmp_path):
    items = [_cand(0, url="https://a.com/1", title="甲"), _cand(1, url="https://b.com/2", title="乙")]
    scores = _scores_json(_score(0, topic="话题甲"), _score(1, topic="话题乙"))
    posts = _posts_json(_post(0, "甲", body="甲的帖子，说它是史上最快（原文没说）"), _post(1, "乙", body="乙的帖子正文"))
    check = json.dumps({"unsupported": [{"i": 0, "phrases": ["史上最快"]}]}, ensure_ascii=False)
    store, settings, feeds, models, *_ = _feeds(tmp_path, scores, items, [posts, check])
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    rows = {r["title"]: r for r in _accepted(store)}
    assert "史上最快" not in rows["甲"]["body"]
    assert rows["乙"]["body"] == "乙的帖子正文"
    assert any(c[2].get("purpose") == "feeds.post_check" for c in models.calls)


def test_faithfulness_check_failure_keeps_item_but_body_falls_back_to_summary(tmp_path):
    """自检失败不拖累出资讯（条目照留），但没核对过的改写不放出去：正文回落摘要
    （线上巡检 2026-10-02：改写会添错，摘要通常更准）。"""
    items = [_cand(0, url="https://a.com/1", title="甲")]
    posts = _posts_json(_post(0, "甲", body="甲的帖子正文"))
    store, settings, feeds, models, *_ = _feeds(tmp_path, _scores_json(_score(0)), items, [posts, "不是 JSON"])
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    row = _accepted(store)[0]
    assert row["body"] != "甲的帖子正文"
    assert row["body"] == items[0]["summary"]
