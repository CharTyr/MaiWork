"""RSS 条目补质量核验（docs/10-资讯流水线改进计划.md §九 第一步 3，2026-10-05）。

现状：RSS 候选「带 from_rss + fetched=True + quote=源摘要」直接并进池，而
`news_recheck.pick_unverified` 见 `from_rss` 就跳过 → RSS 条目从没被打开原文核对过，
水文 / 洗稿 / 低质转载这道（补打开核对的第 2 条要求）对它们等于没有。

做法：
- `prepare_news` 里把 RSS 并池挪到补打开**之前**，RSS 条目和搜索候选一起进补打开；
- `pick_unverified` 不再跳过 `from_rss`：RSS 条目的 quote 只是源里的摘要，一律要核对；
  RSS 条目优先送（它们从没核对过），总量照旧 `_RECHECK_CAP` 封顶、不多派子 agent；
- RSS-only 轮（搜索坏、只有 RSS）也走补打开，不再整轮跳过。
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


def _log_fetch(store: Store, task_id: str, url: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, ?, ?, '子 agent #1', 'fetch_page', ?, ?, 1)",
            (NOW, GID, task_id, url, "取到正文 500 字（直接打开）"),
        )


def _verdict(index: int, url: str, *, verdict: str = "keep", reason: str = "",
             summary: str = "按原文重写的摘要：两三句讲清楚。", quote: str = "原文里的一句话。",
             published: str = "2026-09-24") -> dict:
    return {"index": index, "verdict": verdict, "url": url, "summary": summary, "quote": quote,
            "published": published, "stale": False, "reason": reason}


class _RecheckWorkers:
    """补打开子 agent 的替身：写这次 task_id 下的 fetch_page 记录，交回预置结论。

    RSS-only 轮里除了补打开不该有别的子 agent（撒网 / 核验都被跳过）。"""

    def __init__(self, store_ref: list, items: list[dict], *, opened: list[str] | None = None) -> None:
        self.store_ref = store_ref
        self.items = list(items)
        self.opened = list(opened or [])
        self.calls: list[dict] = []

    async def run(self, brief: str, **kwargs):
        tid = str(kwargs.get("task_id") or "")
        self.calls.append({"brief": brief, "task_id": tid, **kwargs})
        assert tid.startswith("feeds-recheck:"), f"RSS-only 轮只该派补打开：{tid}"
        for url in self.opened:
            _log_fetch(self.store_ref[0], tid, url)
        return WorkerReport(ok=True, summary="核对好了", data={"items": self.items}, evidence=[], steps=2)


def _score(idx: int, *, topic: str = "话题甲") -> dict:
    return {
        "i": idx, "info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4,
        "profile": 0, "topic": topic, "sensitive": False, "grounded": True,
        "junk": False, "junk_reason": "", "same_as_recent": False, "why": "和画像对得上", "icon": "robot",
    }


def _post(idx: int, title: str) -> dict:
    return {"i": idx, "title": title, "body": f"{title}的正文，按口吻写的。", "reason": "群里正聊着",
            "refs": [], "audience": [], "keywords": ["关键词甲"]}


def _make_feeds(tmp_path, *, workers: _RecheckWorkers, replies: list[Any]) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)", (GID, 1_700_000_000.0))
    settings, _ = load_settings({})
    models = FakeModelsQueue(ready=True, replies=list(replies))
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [{"category": "ongoing", "text": "在做开源硬件项目"}]
    topics = _Topics()
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings, search=_UnavailableSearch())
    return store, settings, feeds, models, workers, topics


def _recheck_calls(workers: _RecheckWorkers) -> list[dict]:
    return [c for c in workers.calls]


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
# ----------------------------------------------------------------------


def _setup_rss_only(tmp_path, *, items: list[dict], opened: list[str], replies: list[Any]) -> tuple:
    ref: list = [None]
    workers = _RecheckWorkers(ref, items, opened=opened)
    store, settings, feeds, models, _w, _topics = _make_feeds(tmp_path, workers=workers, replies=replies)
    ref[0] = store
    rss.add_feed(store, GID, url="https://good.example.com/feed", title="好博客", feed_id="rG", now=NOW)
    cands = [_rss_cand("https://ok.example.com/fresh")]

    async def fake_collect(gid, settings):
        return list(cands)

    feeds._collect_rss = fake_collect
    return store, settings, feeds, workers


def test_rss_item_dispatched_to_recheck_and_dropped_as_low_quality(tmp_path) -> None:
    """RSS 条目：补打开打开原文后判「低质转载」→ 硬拒（quote 是源摘要，不算核对过）。"""
    reason = "低质转载：整段搬运，没注明出处"
    store, settings, feeds, workers = _setup_rss_only(
        tmp_path,
        items=[_verdict(0, "https://ok.example.com/fresh", verdict="drop", reason=reason)],
        opened=["https://ok.example.com/fresh"],
        replies=[],
    )
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 0, "内容不合格的 RSS 条目要丢掉"
    calls = _recheck_calls(workers)
    assert len(calls) == 1, "RSS-only 轮也要派一次补打开"
    assert "https://ok.example.com/fresh" in str(calls[0]["brief"])
    row = store.read().execute("SELECT * FROM news_items ORDER BY id").fetchall()[0]
    assert int(row["rejected"]) == 1
    assert row["reject_gate"] == "hard"
    assert "补打开核对" in str(row["reject_reason"]) and reason in str(row["reject_reason"])
    store.close()


def test_rss_item_verified_kept_with_rewritten_quote(tmp_path) -> None:
    """核对通过：摘要 / 依据按原文重写，日期用原文的，站点是真实域名（不是源标题）。"""
    store, settings, feeds, workers = _setup_rss_only(
        tmp_path,
        items=[_verdict(0, "https://ok.example.com/fresh")],
        opened=["https://ok.example.com/fresh"],
        replies=[
            json.dumps({"scores": [_score(0, topic="硬件")]}, ensure_ascii=False),
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


def test_rss_only_round_without_check_result_still_accepts_rss(tmp_path) -> None:
    """补打开没交回结论（子 agent 空手 / 打不开）：RSS 条目照旧往下走，不能说丢就丢。"""
    store, settings, feeds, workers = _setup_rss_only(
        tmp_path,
        items=[],
        opened=[],
        replies=[
            json.dumps({"scores": [_score(0, topic="硬件")]}, ensure_ascii=False),
            json.dumps({"posts": [_post(0, "RSS 水文一条")]}, ensure_ascii=False),
        ],
    )
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1, "没核对出问题就不丢（宁缺毋滥只针对判定的问题）"
    assert len(_recheck_calls(workers)) == 1
    store.close()
