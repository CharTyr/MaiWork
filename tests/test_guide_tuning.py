"""文章（kind=guide）收紧第二轮（2026-10-05 用户拍板，docs/10 §十）。

线上近 3 天：送去打分的候选里文章 73、资讯 88，入选文章 25、资讯 47；两个游戏群约一半文章
是 50～160 天前的。用户定：
1. 每轮最多 1 篇文章（原 2）；
2. 文章只收 60 天内发布的（原 180），搜索时间窗同步 60；「必须有日期」不放宽；
3. 找料时文章最多占一个方向（定关注点的搜索计划里 kind=guide 只留在一个方向里），其余全给资讯。
"""

from __future__ import annotations

import json

from CharTyr_MaiWork.maiwork import feeds as feeds_mod

from fakes import FakeModelsQueue
from test_feeds_freshness import GID, NOW, _TimePatch, _make_feeds, _run


def test_guide_limits_are_tightened() -> None:
    assert feeds_mod.GUIDE_MAX_AGE_DAYS == 60
    assert feeds_mod._GUIDE_ROUND_CAP == 1


def _scored(title: str, kind: str, avg: float, *, site: str) -> dict:
    return {
        "title": title, "kind": kind, "site": site, "topic": title,
        "scores": {"avg": avg, "relevance": 5, "info": 5, "surprise": 3},
    }


def test_only_one_guide_kept_per_round(tmp_path) -> None:
    _store, _settings, feeds, *_ = _make_feeds(tmp_path)
    items = [
        _scored("深度复盘一", "guide", 4.6, site="a.com"),
        _scored("深度复盘二", "guide", 4.4, site="b.com"),
        _scored("一条资讯", "news", 4.2, site="c.com"),
    ]
    feeds._dedup_homogeneous(items, 10, "")
    assert "reject" not in items[0]
    assert items[1]["reject"][0] == "web"
    assert "1 篇" in items[1]["reject"][1]
    assert "reject" not in items[2]


def _focus_reply() -> str:
    def f(q: str) -> dict:
        return {
            "query": q, "why": "w", "source": "long", "intl": False,
            "searches": [
                {"q": f"{q} 新消息", "site": "", "news": True, "kind": "news"},
                {"q": f"{q} 深度分析", "site": "", "news": False, "kind": "guide"},
            ],
        }
    return json.dumps({"focus": [f("方向甲"), f("方向乙"), f("方向丙")], "diverse": None}, ensure_ascii=False)


def test_plan_keeps_guide_searches_in_one_direction_only(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_focus_reply()])
    _store, settings, feeds, models, *_ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        out = _run(feeds._plan_focus(GID, settings))
    with_guide = [f["query"] for f in out if any(s["kind"] == "guide" for s in f.get("searches") or [])]
    assert with_guide == ["方向甲"]
    # 其余方向的 guide 搜索改成找资讯，不丢（搜索次数不变）
    for f in out:
        assert len(f["searches"]) == 2
    prompt = str(models.calls[0][1][0]["content"])
    assert "最多一个方向" in prompt
    assert "60" in prompt
