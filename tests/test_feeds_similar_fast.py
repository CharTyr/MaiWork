"""_any_similar（资讯标题查重提速）：判定必须和 any(_similar(...)) 逐字相同。

随机中英混合串、空串、完全相同、长度悬殊、阈值 0.6/0.75/0.85/1.0 上逐个对比；
再加一批「只改几个字」的近邻串压边界。原写法逐对算 ratio 当参照。
"""

from __future__ import annotations

import json
import random
import threading

import pytest

from CharTyr_MaiWork.maiwork.feeds import _any_similar, _similar

_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz0123456789 "
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工"
)


def _rand_str(rng: random.Random, lo: int = 0, hi: int = 60) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(lo, hi)))


@pytest.mark.parametrize("threshold", [0.6, 0.75, 0.85, 1.0])
def test_any_similar_matches_pairwise_random(threshold: float) -> None:
    rng = random.Random(20261107)
    corpus = [_rand_str(rng) for _ in range(40)]
    corpus += ["", "完全一样的新闻标题", "a", "x" * 500]
    for i in range(80):
        text = _rand_str(rng) if i % 3 else rng.choice(corpus)
        expected = any(_similar(text, t) >= threshold for t in corpus)
        assert _any_similar(text, corpus, threshold) == expected


@pytest.mark.parametrize("threshold", [0.6, 0.75, 0.85, 0.9, 0.95, 1.0])
def test_any_similar_near_duplicate_boundary(threshold: float) -> None:
    """近邻串（只改几个字）压阈值边界：判定仍须一致。"""
    rng = random.Random(7)
    base = "腾讯发布新一代混元大模型 参数规模翻倍"
    corpus = []
    for _ in range(20):
        s = list(base)
        for _ in range(rng.randrange(0, 6)):
            s[rng.randrange(len(s))] = rng.choice(_ALPHABET)
        corpus.append("".join(s))
    corpus.append(base)
    for text in (base, base[:-2], base + "（图）", base[:5]):
        expected = any(_similar(text, t) >= threshold for t in corpus)
        assert _any_similar(text, corpus, threshold) == expected


def test_any_similar_empty_cases() -> None:
    assert _any_similar("任意标题", [], 0.85) is False
    # 空串对空串 ratio=1.0，和 any(_similar) 一致算撞
    assert _any_similar("", ["x", ""], 0.85) is True
    assert _any_similar("", ["x", "y"], 0.85) is False


# ----------------------------------------------------------------------
# 挪出事件循环：粗筛 / 硬淘汰跑在工作线程，判定结果不变
# ----------------------------------------------------------------------


def test_prefilter_runs_off_event_loop(tmp_path, monkeypatch) -> None:
    """_collect_two_phase 里 _prefilter 挪到工作线程；本轮内重标题照常被刷。"""
    from test_feeds_explore import (
        GID,
        _cand_item,
        _feeds,
        _ok_report,
        _run,
    )

    _store, settings, feeds, _models, workers, _profiles, _topics = _feeds(tmp_path)
    title = "本地大模型推理框架发布新版本"
    workers.report = _ok_report({
        "items": [_cand_item(0, title=title), _cand_item(1, title=title)],
    })
    main_ident = threading.get_ident()
    seen: list[int] = []
    orig = feeds._prefilter

    def _spy(gid, s, c):
        seen.append(threading.get_ident())
        return orig(gid, s, c)

    monkeypatch.setattr(feeds, "_prefilter", _spy)
    out = _run(feeds._collect_two_phase(
        GID, [{"query": "q", "why": "w", "angle": "", "source": ""}], settings,
        collect_mark="feeds-collect:t", stats_out={},
    ))
    assert seen and all(t != main_ident for t in seen)
    assert len(out) == 1, "两条同标题的候选只留一条"


def test_hard_reject_runs_off_event_loop(tmp_path, monkeypatch) -> None:
    """prepare_news 里 _hard_reject_code 挪到工作线程；近重标题照常硬拒。

    0.815 的相似度能过粗筛（0.85 闸）但过不了一道（_TITLE_DEDUP_RATIO=0.8），
    正好验证工作线程里跑的 _any_similar 判定不变。
    """
    from test_feeds_freshness import (
        GID,
        NOW,
        FakeWorkers,
        _FOCUS_JSON,
        _TimePatch,
        _make_feeds,
        _ok_report,
        _run,
    )
    from fakes import FakeModelsQueue

    item = {
        "title": "本地大模型推理框架发布新版本",
        "url": "https://cand.example.com/a",
        "summary": "摘要讲清楚这件事。",
        "kind": "news",
        "published": NOW - 3600,
        "fetched": True,
        "quote": "原文依据在正文里。",
        "paywall": False,
    }
    store, _settings, feeds, _models, _w, _topics, _p = _make_feeds(
        tmp_path,
        models=FakeModelsQueue(ready=True, replies=[_FOCUS_JSON]),
        workers=FakeWorkers(_ok_report({"items": [item]})),
    )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, created, rejected)"
            " VALUES (0, ?, '本地大模型推理框架新版发布', 'https://old.example.com/x', ?, 0)",
            (GID, NOW - 3600),
        )
    main_ident = threading.get_ident()
    seen: list[int] = []
    seen_items: list[dict] = []
    orig = feeds._hard_reject_code

    def _spy(gid, s, c, **kw):
        seen.append(threading.get_ident())
        survivors = orig(gid, s, c, **kw)
        seen_items.extend(c)
        return survivors

    monkeypatch.setattr(feeds, "_hard_reject_code", _spy)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert seen and all(t != main_ident for t in seen)
    dup = next(i for i in seen_items if i["title"] == item["title"])
    assert dup.get("reject") and "标题高度相似" in dup["reject"][1]


def test_personal_hard_reject_runs_off_event_loop(tmp_path, monkeypatch) -> None:
    """个人向 _hard_reject_code 同样挪出事件循环；撞最近发过的标题照常拒。"""
    from test_feeds import FakeSearch
    from test_personal import (
        GID,
        NOW,
        UID,
        _make_personal,
        _personal_scores_json,
        _run,
        _time_patch,
    )
    from fakes import FakeModelsQueue

    store, _s, personal, _m, _w, _t, _p = _make_personal(
        tmp_path,
        models=FakeModelsQueue(
            ready=True,
            replies=[
                json.dumps({"focus": [{"query": "FPGA 学习板", "why": "在做"}], "idea": None},
                           ensure_ascii=False),
                _personal_scores_json(),
                json.dumps({"posts": []}, ensure_ascii=False),
            ],
        ),
        items=[{
            "title": "",
            "url": "https://example.com/fpga-board",
            "body": "《新开源 FPGA 学习板上架》\n一块新的 FPGA 学习板正式上架，资料齐全，原文依据充分。",
            "summary": "一块新的 FPGA 学习板。",
            "kind": "news",
            "published": NOW - 86400,
            "fetched": True,
            "quote": "一块新的 FPGA 学习板正式上架，资料齐全，原文依据充分。",
            "paywall": False,
        }],
    )
    # 已发过标题（和候选「新开源 FPGA 学习板上架」相似度 0.93 ≥ 0.8）→ 硬拒
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, created, rejected)"
            " VALUES (0, ?, '新款开源 FPGA 学习板上架', 'https://old.example.com/y', ?, 0)",
            (GID, NOW - 3600),
        )
    main_ident = threading.get_ident()
    seen: list[int] = []
    seen_items: list[dict] = []
    orig = personal._hard_reject_code

    def _spy(gid, c, **kw):
        seen.append(threading.get_ident())
        survivors = orig(gid, c, **kw)
        seen_items.extend(c)
        return survivors

    monkeypatch.setattr(personal, "_hard_reject_code", _spy)
    with _time_patch():
        _run(personal.prepare_personal(GID, UID))
    assert seen and all(t != main_ident for t in seen)
    dup = next(i for i in seen_items if "FPGA" in i["title"])
    assert dup.get("reject") and "标题高度相似" in dup["reject"][1]
