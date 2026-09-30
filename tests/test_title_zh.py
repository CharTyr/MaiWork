"""资讯 / 文章标题译成中文（2026-09-30 用户要求：外文标题群友可能不看）。

- 写帖子时模型顺带给 title_zh；原标题不是中文时才采用，入库 title 用中文，
  原标题留在 sources[0].title（网页来源卡片显示原文标题）；
- 原标题本来就是中文 → 不换；title_zh 不含汉字 → 不换；
- 查重：最近发过的条目按中文标题 + 原标题都比，外文同题换个链接照样拦住。
"""

from __future__ import annotations

import json

from CharTyr_MaiWork.maiwork.feeds import _adopt_title_zh, _looks_chinese

from fakes import FakeModelsQueue
from test_humane import (
    GID,
    NOW,
    _FOCUS_JSON,
    _QUOTE,
    _TimePatch,
    _make_feeds,
    _ok_report,
    _run,
    _seed_chat,
    FakeWorkers,
)

_EN = "New open-source FPGA dev board released"
_ZH = "新款开源 FPGA 开发板发布"


def _items(title: str, url: str = "https://example.com/board") -> dict:
    return {
        "items": [
            {"title": title, "url": url, "summary": "一块新板子。配置不错。社区关注高。", "kind": "news",
             "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
        ]
    }


def _scores(title: str) -> str:
    return json.dumps(
        {"scores": [
            {"i": 0, "title": title, "info": 5, "source": 4, "relevance": 5,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "FPGA", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "群里的硬件项目正好用得上", "icon": "tools"},
        ]},
        ensure_ascii=False,
    )


def _post(title: str, title_zh: str) -> str:
    return json.dumps(
        {"posts": [
            {"i": 0, "title": title, "title_zh": title_zh, "body": "板子插上就能跑。",
             "reason": "群里在聊 FPGA", "refs": [1], "audience": ["阿一"], "keywords": ["FPGA"]},
        ]},
        ensure_ascii=False,
    )


def _prepare(tmp_path, title: str, title_zh: str):
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _scores(title), _post(title, title_zh)])
    store, settings, feeds, models, workers, topics, _p = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers(_ok_report(_items(title))),
    )
    _seed_chat(store)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    return store, feeds, models, topics, got


def test_looks_chinese() -> None:
    assert _looks_chinese("新款开源 FPGA 开发板发布")
    assert not _looks_chinese("New FPGA board")
    assert not _looks_chinese("新しいFPGAボードが登場")  # 日文（有假名）
    assert not _looks_chinese("새로운 FPGA 보드")


def test_adopt_title_zh_rules() -> None:
    it = {"title": _EN}
    _adopt_title_zh(it, {"title_zh": "  " + _ZH + "  "})
    assert it["title_zh"] == _ZH
    it2 = {"title": "中文原标题"}
    _adopt_title_zh(it2, {"title_zh": "另一个中文标题"})
    assert "title_zh" not in it2  # 本来就是中文 → 不换
    it3 = {"title": _EN}
    _adopt_title_zh(it3, {"title_zh": "Still English"})
    assert "title_zh" not in it3  # 译文不含汉字 → 不换
    it4 = {"title": _EN}
    _adopt_title_zh(it4, {"title_zh": "中" * 300})
    assert len(it4["title_zh"]) <= 80


def test_english_title_stored_in_chinese_with_original_in_sources(tmp_path) -> None:
    store, feeds, models, topics, got = _prepare(tmp_path, _EN, _ZH)
    assert got == 1
    row = store.read().execute("SELECT title, sources FROM news_items WHERE rejected=0").fetchone()
    assert row["title"] == _ZH
    assert json.loads(row["sources"])[0]["title"] == _EN
    # 进话题候选池的也是中文标题
    assert topics.calls and topics.calls[0]["title"] == _ZH


def test_post_prompt_asks_for_title_zh(tmp_path) -> None:
    store, feeds, models, topics, got = _prepare(tmp_path, _EN, _ZH)
    prompts = [str(c) for c in getattr(models, "calls", [])]
    assert any("title_zh" in p for p in prompts)


def test_chinese_title_unchanged(tmp_path) -> None:
    store, *_rest = _prepare(tmp_path, "新开源 FPGA 开发板发布", "别的中文标题")
    row = store.read().execute("SELECT title, sources FROM news_items WHERE rejected=0").fetchone()
    assert row["title"] == "新开源 FPGA 开发板发布"
    assert json.loads(row["sources"])[0]["title"] == "新开源 FPGA 开发板发布"


def test_dedup_still_sees_original_title(tmp_path) -> None:
    """库里存的是中文标题，但外文同题换个链接再来 → 仍按原标题判重复拦下。"""
    store, feeds, models, topics, got = _prepare(tmp_path, _EN, _ZH)
    assert got == 1
    models2 = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _scores(_EN), _post(_EN, _ZH)])
    feeds._models = models2
    feeds._workers = FakeWorkers(_ok_report(_items(_EN, url="https://mirror.example.org/board-news")))
    with _TimePatch():
        got2 = _run(feeds.prepare_news(GID))
    assert got2 == 0
    n = store.read().execute("SELECT COUNT(*) AS n FROM news_items WHERE rejected=0").fetchone()["n"]
    assert n == 1


def test_personal_news_title_translated(tmp_path) -> None:
    """个人向资讯同样换中文标题，原标题进 sources。"""
    import test_personal as tp

    items = {"items": [
        {"title": _EN, "url": "https://example.com/board", "summary": "一块新的 FPGA 学习板。便宜。资料全。",
         "kind": "news", "published": tp.NOW - 86400, "fetched": True, "quote": tp._QUOTE, "paywall": False},
    ]}
    scores = json.dumps({"scores": [
        {"i": 0, "title": _EN, "info": 5, "source": 4, "relevance": 5,
         "timeliness": 4, "chat": 4, "profile": 0, "topic": "FPGA", "sensitive": False,
         "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
         "why": "他在做 FPGA 学习板", "icon": "tools"},
    ]}, ensure_ascii=False)
    posts = json.dumps({"posts": [
        {"i": 0, "title": _EN, "title_zh": _ZH, "body": "你可能用得上……", "reason": "你在弄这个",
         "refs": [], "audience": [], "keywords": ["FPGA"]},
    ]}, ensure_ascii=False)
    models = FakeModelsQueue(ready=True, replies=[
        json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False),
        scores, posts,
    ])
    store, _s, personal, models, *_r = tp._make_personal(
        tmp_path, models=models, workers=tp.FakeWorkers(tp._ok_report(items)),
    )
    with tp._time_patch():
        assert tp._run(personal.prepare_personal(tp.GID, tp.UID)) == 1
    row = store.read().execute("SELECT title, sources FROM news_items WHERE rejected=0").fetchone()
    assert row["title"] == _ZH
    assert json.loads(row["sources"])[0]["title"] == _EN
    assert any("title_zh" in str(c) for c in models.calls)
