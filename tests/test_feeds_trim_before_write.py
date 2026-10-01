"""C01（docs/13-0.7.0整体审查.md）：确定会淘汰的条目，不该先花钱写帖子。

改前：prepare_news 的 ⑤.5 `_write_posts`（写帖 + 原文自检，两次模型调用）排在
⑥ `_dedup_homogeneous`（同话题≤2、同域名≤3、敏感≤1、diverse≤2、总数≤max_items）之前——
注定要被名额刷掉的条目也照写帖、照自检。

改后：打分 / 门槛完成后，先做确定性的容量与同质化限制，再只给最终要发的条目写帖及自检；
被淘汰的仍落库并保留淘汰原因。个人向（personal.py）同理：先截前三再写。

验收（审查原文）：构造 12 个过线但最终只留 4 个的候选；断言写帖和自检的模型请求里
只包含 4 个条目；最终来源、排序、淘汰原因不退步。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fakes import PICK_FALLBACK_REPLY, FakeModelsQueue, focus_reply
from test_feeds_quality import (
    GID,
    NOW,
    _TimePatch,
    _cand,
    _make_feeds,
    _rows,
    _run,
    _score,
    _scores_json,
)


def _posts_json(*posts: dict) -> str:
    return json.dumps({"posts": list(posts)}, ensure_ascii=False)


def _model_calls(models: FakeModelsQueue, purpose: str) -> list:
    return [m for _r, m, k in models.calls if str(k.get("purpose") or "") == purpose]


def _titles_in_prompt(messages: list, titles: list) -> list:
    """这次模型调用的提示词里出现了哪些候选标题。"""
    text = "\n".join(str(m.get("content") or "") for m in messages)
    return [t for t in titles if t in text]


# 12 个两两不相似的标题（粗筛 _similar>=0.85 会刷近似标题，得造得够散）
_TITLES_12 = [
    "量子芯片全新架构发布", "本地部署实战全记录", "编辑部圆桌会谈纪要", "开源周报精选集",
    "硬件入门避坑清单", "某语言运行时细节解析", "无线电通联小知识", "云端成本控制心得",
    "独立游戏开发杂记", "数据库索引漫谈", "相机传感器科普", "家用路由折腾史",
]


# ----------------------------------------------------------------------
# 群向：12 个过线、只留 4 个 → 写帖 / 自检只见 4 个
# ----------------------------------------------------------------------


def test_write_posts_only_for_survivors_after_trim(tmp_path) -> None:
    """12 条全过第二道门槛（不同话题、不同域名、全过线），max_items=4 →
    写帖和原文自检的模型请求里只出现最终留下的 4 条；其余 8 条落库、原因不退步。"""
    cfg = {"feeds": {"max_items": 4}}
    items = [
        _cand(i, title=_TITLES_12[i], url=f"https://site-{i}.example.com/article-{i}")
        for i in range(12)
    ]
    # 打分：编号越大 chat 越高 → avg 越高 → 最终留第 8..11 条
    # （打分每批最多 8 条：12 条要两批，喂两份打分回复，编号是全批统一编号）
    scores_a = _scores_json(*[
        _score(i, topic=f"话题{i}", chat=3.0 + i * 0.1)
        for i in range(8)
    ])
    scores_b = _scores_json(*[
        _score(i, topic=f"话题{i}", chat=3.0 + i * 0.1)
        for i in range(8, 12)
    ])
    models = FakeModelsQueue(ready=True, replies=[
        focus_reply(),
        PICK_FALLBACK_REPLY,
        scores_a,
        scores_b,
        _posts_json(*[
            {"i": k, "title": t, "body": f"{t}的正文。", "reason": "对得上",
             "refs": [], "audience": [], "keywords": ["甲"]}
            for k, t in enumerate(_TITLES_12[8:])
        ]),
        '{"unsupported": []}',  # 自检：全都没问题
    ])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, models=models, cfg=cfg)

    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))

    assert kept == 4, f"max_items=4 时只留 4 条，实际 {kept}"

    # 被淘汰的仍落库、带淘汰原因（名额类：超出本轮上限）。
    # 注：12 条候选全挂在方向 1，粗筛按方向均衡（每方向 ≤40% 名额）先刷 2 条——
    # 两阶段后被粗筛刷掉的不落 news_items（去漏斗 rejects 看），所以落库 10 条。
    rows = _rows(store, "SELECT title, rejected, reject_gate, reject_reason, body FROM news_items")
    assert len(rows) == 10
    accepted = [r for r in rows if not r["rejected"]]
    rejected = [r for r in rows if r["rejected"]]
    assert len(accepted) == 4 and len(rejected) == 6
    # 留哪 4 条由 avg 决定（打分两批并发，不断言具体名次——只断言「写帖只写给留下的」）
    kept_titles = {r["title"] for r in accepted}
    dropped_titles = {r["title"] for r in rejected}
    # 写帖（feeds.post，含漏写重试）请求里只能出现留下的 4 条标题
    post_calls = _model_calls(models, "feeds.post")
    assert post_calls, "写帖模型要被调用"
    for messages in post_calls:
        appeared = _titles_in_prompt(messages, _TITLES_12)
        assert set(appeared) <= kept_titles, (
            f"写帖请求里不该出现注定淘汰的条目：{set(appeared) & dropped_titles}")
    # 自检（feeds.post_check）同理
    for messages in _model_calls(models, "feeds.post_check"):
        appeared = _titles_in_prompt(messages, _TITLES_12)
        assert set(appeared) <= kept_titles, (
            f"自检请求里不该出现注定淘汰的条目：{set(appeared) & dropped_titles}")
    for r in rejected:
        assert r["reject_reason"], "被淘汰的要留原因"
        assert "上限" in str(r["reject_reason"]) or "留分高" in str(r["reject_reason"])
    # 通过的有帖子正文；被刷掉的没花写帖钱（body 空）
    for r in accepted:
        assert str(r["body"] or "").strip(), "留下的要有帖子正文"
    for r in rejected:
        assert not str(r["body"] or "").strip(), f"被淘汰的不该有写好的帖子：{r['title']}"
    store.close()


def test_trim_reasons_unchanged_and_order_kept(tmp_path) -> None:
    """来源 / 排序 / 淘汰解释不退步：同话题第 3 条、同域名第 4 条按原句式拒，
    且这些被拒的不再出现在写帖请求里。"""
    # 标题两两不近似（粗筛 _similar>=0.85 会刷近似标题）；同话题靠打分里的 topic 归堆
    items = [
        _cand(0, title="量子芯片全新架构发布", url="https://a.example.com/1"),
        _cand(1, title="编辑部圆桌会谈纪要", url="https://b.example.com/2"),
        _cand(2, title="无线电通联小知识", url="https://c.example.com/3"),
        _cand(3, title="本地部署实战全记录", url="https://same.example.com/1"),
        _cand(4, title="开源周报精选集", url="https://same.example.com/2"),
        _cand(5, title="独立游戏开发杂记", url="https://same.example.com/3"),
        _cand(6, title="相机传感器科普", url="https://same.example.com/4"),
    ]
    scores = _scores_json(
        _score(0, topic="撞车话题", chat=4.5),
        _score(1, topic="撞车话题", chat=4.4),
        _score(2, topic="撞车话题", chat=4.3),  # 同话题第 3 条 → 拒
        _score(3, topic="别的甲", chat=4.5),
        _score(4, topic="别的乙", chat=4.4),
        _score(5, topic="别的丙", chat=4.3),
        _score(6, topic="别的丁", chat=4.2),    # 同域名第 4 条 → 拒
    )
    kept_titles = ["量子芯片全新架构发布", "编辑部圆桌会谈纪要",
                   "本地部署实战全记录", "开源周报精选集", "独立游戏开发杂记"]
    models = FakeModelsQueue(ready=True, replies=[
        focus_reply(),
        PICK_FALLBACK_REPLY,
        scores,
        _posts_json(*[
            {"i": k, "title": t, "body": f"{t}的正文。", "reason": "对得上",
             "refs": [], "audience": [], "keywords": ["甲"]}
            for k, t in enumerate(kept_titles)
        ]),
        '{"unsupported": []}',
    ])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, models=models)

    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))

    assert kept == 5, f"实际留下 {kept} 条"
    rows = _rows(store)
    by_title = {r["title"]: r for r in rows}
    # 原淘汰句式不退步
    assert "留分高" in str(by_title["无线电通联小知识"]["reject_reason"])
    assert "留分高" in str(by_title["相机传感器科普"]["reject_reason"])
    # 被拒的没进写帖请求
    for messages in _model_calls(models, "feeds.post"):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        assert "无线电通联小知识" not in text and "相机传感器科普" not in text
    store.close()


def test_post_check_fallback_does_not_change_kept_count(tmp_path) -> None:
    """自检（_check_posts）只回落正文、不新增淘汰；先截后写后没有意外回补：
    最终条数不受自检结论影响。"""
    titles = _TITLES_12[:5]
    items = [_cand(i, title=titles[i], url=f"https://site-{i}.example.com/p") for i in range(5)]
    scores = _scores_json(*[_score(i, topic=f"话题{i}", chat=3.0 + i * 0.1) for i in range(5)])
    models = FakeModelsQueue(ready=True, replies=[
        focus_reply(),
        PICK_FALLBACK_REPLY,
        scores,
        _posts_json(*[
            {"i": k, "title": t, "body": f"{t}的正文。", "reason": "对得上",
             "refs": [], "audience": [], "keywords": ["甲"]}
            for k, t in enumerate(titles[2:])  # max_items=3 → 留 2,3,4
        ]),
        '{"unsupported": [{"i": 0, "phrases": ["首个"]}]}',  # 自检判留下的第 1 条有撑不住的说法
    ])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, models=models, cfg={"feeds": {"max_items": 3}})

    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))

    # 自检回落正文不等于淘汰：条数不变，仍 3 条
    assert kept == 3
    rows = _rows(store, "SELECT title, rejected, body FROM news_items WHERE rejected=0")
    assert len(rows) == 3
    # 被自检点名的那条：正文回落成摘要（不再是有撑不住说法的帖子）
    by_title = {r["title"]: r for r in rows}
    fallen = by_title[titles[2]]
    assert "首个" not in str(fallen["body"]) or str(fallen["body"]).startswith("摘要")
    store.close()
