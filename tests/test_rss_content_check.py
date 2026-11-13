"""RSS 条目补质量核验（docs/10-资讯流水线改进计划.md §九 第一步 3，2026-10-05）。

现状：RSS 候选「带 from_rss + fetched=True + quote=源摘要」直接并进池，而
`news_recheck.pick_unverified` 见 `from_rss` 就跳过 → RSS 条目从没被打开原文核对过，
水文 / 洗稿 / 低质转载这道（补打开核对的第 2 条要求）对它们等于没有。

做法：
- `prepare_news` 里把 RSS 并池挪到打开原文**之前**，RSS 条目和搜索候选一起开原文；
- `pick_unverified` 不再跳过 `from_rss`：RSS 条目的 quote 只是源里的摘要，一律要核对；
  RSS 条目优先送（它们从没核对过），总量照旧 `_RECHECK_CAP` 封顶；
- RSS-only 轮（搜索坏、只有 RSS）也走打开原文，不再整轮跳过。

2026-11 零子 agent 改版：打开原文不再派补打开子 agent——主流程代码 fetch_page
（_open_candidates），水文 / 洗稿 / 低质转载由打分环节的 junk 闸判（评分提示词照
搬原核验清单），没读到正文照样硬拒。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from fakes import FakeModelsQueue, FakeProfiles

from CharTyr_MaiWork.maiwork import news_recheck, rss
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds, _normalize_url
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import WorkerReport

NOW = 1_790_000_000.0
GID = "900000001"


def _run(coro):
    return asyncio.run(coro)


class _TimePatch:
    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class _UnavailableSearch:
    def available(self) -> bool:
        return False

    def status(self) -> tuple:
        return False, "还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索"


class _Topics:
    def __init__(self) -> None:
        self.calls: list = []

    def add_candidate(self, group_id, **kw):
        self.calls.append({"group_id": group_id, **kw})


def _rss_cand(url: str, *, title: str = "RSS 水文一条", ts: float = NOW - 3600) -> dict:
    return {
        "title": title,
        "url": url,
        "summary": "RSS 源里的摘要。",
        "kind": "news",
        "published_raw": ts,
        "published_ts": ts,
        "fetched": True,
        "quote": "RSS 源里的原文依据。",
        "paywall": False,
        "image_url": "",
        "url_key": _normalize_url(url),
        "site": "源标题",
        "from_rss": True,
        "_rss_feed": "https://good.example.com/feed",
        "_rss_title": "源标题",
    }


class _FakeWorkers:
    """假 workers.run：只记录调用（零子 agent 改版后一轮里一次都不该派）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, brief: str, **kwargs):
        self.calls.append({"brief": brief, "task_id": str(kwargs.get("task_id") or ""), **kwargs})
        tid = str(kwargs.get("task_id") or "")
        assert not tid.startswith(("feeds-recheck:", "feeds-verify:", "feeds-discover:")), \
            f"零子 agent 改版后不该派资讯子 agent：{tid}"
        return WorkerReport(ok=True, summary="好", data={"items": []}, evidence=[], steps=1)


def _score(idx: int, *, topic: str = "话题甲") -> dict:
    return {
        "i": idx, "info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4,
        "profile": 0, "topic": topic, "sensitive": False, "grounded": True,
        "junk": False, "junk_reason": "", "same_as_recent": False, "why": "和画像对得上", "icon": "robot",
    }


def _post(idx: int, title: str) -> dict:
    return {"i": idx, "title": title, "body": f"{title}的正文，按口吻写的。", "reason": "群里正聊着",
            "refs": [], "audience": [], "keywords": ["关键词甲"]}


def _make_feeds(tmp_path, *, workers=None, replies: list[Any]) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)", (GID, 1_700_000_000.0))
    settings, _ = load_settings({})
    models = FakeModelsQueue(ready=True, replies=list(replies))
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [{"category": "ongoing", "text": "在做开源硬件项目"}]
    topics = _Topics()
    if workers is None:
        workers = _FakeWorkers()
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings, search=_UnavailableSearch())
    return store, settings, feeds, models, workers, topics


# ----------------------------------------------------------------------
# 单元：RSS 条目进补打开名单，且优先（它们从没核对过）
# ----------------------------------------------------------------------


def test_pick_unverified_includes_rss_items() -> None:
    cands = [
        _rss_cand("https://ok.example.com/rss1"),
        {"title": "搜索候选", "url": "https://s.example.com/1", "summary": "s", "kind": "news",
         "fetched": True, "quote": "原文依据", "url_key": _normalize_url("https://s.example.com/1")},
    ]
    picked = news_recheck.pick_unverified(cands, opened=set(), records=0)
    assert picked == [0], "RSS 条目（quote 只是源摘要）也要补打开核对"


def test_pick_unverified_puts_rss_first_and_keeps_cap() -> None:
    rss_items = [_rss_cand(f"https://ok.example.com/rss{i}") for i in range(6)]
    other = [
        {"title": f"没打开的搜索候选{i}", "url": f"https://s.example.com/{i}", "summary": "s",
         "kind": "news", "fetched": False, "quote": "", "url_key": _normalize_url(f"https://s.example.com/{i}")}
        for i in range(8)
    ]
    picked = news_recheck.pick_unverified(rss_items + other, opened=set(), records=0)
    assert picked[:6] == [0, 1, 2, 3, 4, 5], "RSS 条目优先送"
    assert len(picked) == 14
    # 上层按 _RECHECK_CAP 截断：RSS 6 条 + 2 条搜索候选，不多派子 agent
    assert len(picked[: news_recheck._RECHECK_CAP]) == 8
    assert sum(1 for i in picked[: news_recheck._RECHECK_CAP] if i < 6) == 6


def test_already_rejected_items_not_picked() -> None:
    cands = [_rss_cand("https://ok.example.com/rss1")]
    cands[0]["reject"] = ("hard", "以前拒过")
    assert news_recheck.pick_unverified(cands, opened=set(), records=0) == []


# ----------------------------------------------------------------------
# 端到端（RSS-only 轮：搜索坏、只有 RSS，第 1 步说的两条入口之一）
# 2026-11 零子 agent 改版：开原文 = 主流程代码 fetch_page（fixture）；质量闸 = 打分 junk
# ----------------------------------------------------------------------


def _setup_rss_only(tmp_path, *, replies: list[Any], wire_tools: bool = True) -> tuple:
    """RSS-only 轮：一个 RSS 候选 + fixture fetch_page（正文里真写着它的 quote）。"""
    workers = _FakeWorkers()
    store, settings, feeds, models, _w, _topics = _make_feeds(tmp_path, workers=workers, replies=replies)
    rss.add_feed(store, GID, url="https://good.example.com/feed", title="好博客", feed_id="rG", now=NOW)
    cands = [_rss_cand("https://ok.example.com/fresh")]

    async def fake_collect(gid, settings):
        return list(cands)

    feeds._collect_rss = fake_collect
    if wire_tools:
        from fakes import make_fixture_page_tools, _wrap_score_enrichment

        # 预置同名同链条目：fixture 正文（fixture_article）写着它的 quote / summary
        preset = [{"title": cands[0]["title"], "url": cands[0]["url"],
                   "quote": cands[0]["quote"], "summary": cands[0]["summary"],
                   "published": cands[0]["published_ts"], "fetched": True}]
        feeds.set_tools(make_fixture_page_tools(store, preset))
        _wrap_score_enrichment(models, preset)
        models._fixture_tools = feeds._tools
    return store, settings, feeds, workers


def test_rss_item_dispatched_to_recheck_and_dropped_as_low_quality(tmp_path) -> None:
    """RSS 条目：代码真打开原文后，打分判「低质转载」（junk）→ 硬拒。"""
    reason = "低质转载：整段搬运，没注明出处"
    store, settings, feeds, workers = _setup_rss_only(
        tmp_path,
        replies=[
            json.dumps({"scores": [dict(_score(0, topic="硬件"),
                                        junk=True, junk_reason=reason)]},
                       ensure_ascii=False),
        ],
    )
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 0, "内容不合格的 RSS 条目要丢掉"
    assert workers.calls == [], "零子 agent 改版后 RSS-only 轮不派任何子 agent"
    fetched = store.read().execute(
        "SELECT * FROM tool_calls WHERE tool='fetch_page'").fetchall()
    assert any("https://ok.example.com/fresh" in str(r["input"]) for r in fetched), \
        "RSS 原文要真打开过"
    row = store.read().execute("SELECT * FROM news_items ORDER BY id").fetchall()[0]
    assert int(row["rejected"]) == 1
    assert row["reject_gate"] == "hard"
    assert "垃圾" in str(row["reject_reason"]) and reason in str(row["reject_reason"])
    store.close()


def test_rss_item_verified_kept_with_rewritten_quote(tmp_path) -> None:
    """核对通过：摘要 / 依据按原文写回（打分抽取），站点是真实域名（不是源标题）。"""
    score = dict(_score(0, topic="硬件"),
                 summary="按原文重写的摘要：两三句讲清楚。",
                 quote="RSS 源里的原文依据。",
                 conditions=[])
    store, settings, feeds, workers = _setup_rss_only(
        tmp_path,
        replies=[
            json.dumps({"scores": [score]}, ensure_ascii=False),
            json.dumps({"posts": [_post(0, "RSS 水文一条")]}, ensure_ascii=False),
        ],
    )
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    row = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchall()[0]
    assert row["summary"] == "按原文重写的摘要：两三句讲清楚。"
    src = json.loads(row["sources"])[0]
    assert src["site"] == "ok.example.com"
    store.close()


def test_rss_only_round_without_check_result_rejects_unread_rss(tmp_path) -> None:
    """原文打不开：RSS 条目的 quote 只是源摘要，本轮没读到正文就没有原文依据——
    硬拒并写清理由（2026-10-13 巡检：仅标题级核对的卡片照样发了）。
    以前这里「没核对出问题就放行」，等于把 RSS 源摘要当成原文依据。"""
    store, settings, feeds, workers = _setup_rss_only(
        tmp_path, replies=[], wire_tools=False)
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 0, "没读到正文的 RSS 条目不能凭源摘要发出去"
    assert workers.calls == []
    row = store.read().execute("SELECT * FROM news_items ORDER BY id").fetchall()[0]
    assert int(row["rejected"]) == 1 and row["reject_gate"] == "hard"
    assert "正文" in str(row["reject_reason"]) or "原文" in str(row["reject_reason"])
    assert not [c for _r, _m, c in feeds._models.calls if c.get("purpose") == "feeds.score"], \
        "没幸存者就不该花打分的钱"
    store.close()
