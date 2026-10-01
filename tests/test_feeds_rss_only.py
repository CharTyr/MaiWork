"""A06（docs/13-0.7.0整体审查.md）：只订 RSS、不配置搜索时，RSS 也要照常读。

改前：prepare_news 在收 RSS 前先 `_ensure_search()`，SearchUnavailable 直接 _skipped_batch
返回 0——RSS 一次都没取（复现见 docs/audits/0.7.0/evidence/rss_without_search.json）。

改后：RSS 和搜索是两条候选入口，分别判断。
- 搜索不可用（SearchUnavailable：没配置或暂时坏）但本群有启用的 RSS 源 → 照常取 RSS，
  跳过撒网（_collect_two_phase）和补打开，走 RSS-only 一轮；硬淘汰 / 打分 / 隐私 / 去重不放宽；
  定关注点（_plan_focus 只服务撒网）跳过省 token；
  批次统计里带 source_mode="rss_only"，网页能看出这一轮是 RSS-only。
- 搜索可用 → 原逻辑。
- 两者都不可用（没搜索且没订 RSS）→ 仍 _skipped_batch，原因准确；
  RSS 取了但空、搜索也没有 → 也准确说明。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from fakes import (
    PICK_FALLBACK_REPLY,
    FakeModelsQueue,
    FakeProfiles,
    focus_reply,
    patch_two_phase_feeds,
)

from CharTyr_MaiWork.maiwork import rss
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds, _normalize_url
from CharTyr_MaiWork.maiwork.search import SearchUnavailable
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
G1 = "900000001"


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


class _TimePatch:
    """feeds.clock.now 固定成 NOW。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class _UnavailableSearch:
    """有 available() 但回 False 的搜索桩（像 search.py 的 Search：没绑定/连不上）。"""

    def available(self) -> bool:
        return False

    def status(self) -> tuple:
        return False, "还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索"


class _BrokenSearch:
    """老式假搜索：search() 探测直接抛 SearchUnavailable（暂时坏）。"""

    async def search(self, q, **kw):
        raise SearchUnavailable("搜索服务连不上：超时")


class FakeWorkers:
    """假 workers.run：记录调用；RSS-only 轮里撒网 / 补打开都不该派工。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        from CharTyr_MaiWork.maiwork.workers import WorkerReport

        return WorkerReport(ok=True, summary="好", data={"items": []}, evidence=[], steps=1)


class FakeTopics:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"


def _rss_cand(feed: str, title: str, url: str, ts: float) -> dict:
    """造一条 _collect_rss 交回形状的 RSS 候选。"""
    return {
        "title": title,
        "url": url,
        "summary": "RSS 源里的摘要。",
        "kind": "news",
        "published_raw": ts,
        "published_ts": ts,
        "fetched": True,
        "quote": _QUOTE,
        "paywall": False,
        "image_url": "",
        "url_key": _normalize_url(url),
        "site": "源标题",
        "from_rss": True,
        "_rss_feed": feed,
        "_rss_title": "源标题",
    }


def _search_item_dict() -> dict:
    return {
        "title": "搜索找到的事",
        "url": "https://search.example.com/a",
        "summary": "搜索摘要：这件事确实发生了。",
        "kind": "news",
        "published": NOW - 3600,
        "fetched": True,
        "quote": _QUOTE,
        "paywall": False,
    }


def _score(idx: int, *, topic: str = "话题甲", relevance: float = 4,
           timeliness: float = 4, chat: float = 4) -> dict:
    return {
        "i": idx, "info": 4, "source": 4, "relevance": relevance, "timeliness": timeliness,
        "chat": chat, "profile": 0, "topic": topic,
        "sensitive": False, "grounded": True, "junk": False, "junk_reason": "",
        "same_as_recent": False, "why": "和画像对得上", "icon": "robot",
    }


def _scores_json(*scores: dict) -> str:
    return json.dumps({"scores": list(scores)}, ensure_ascii=False)


def _post(idx: int, title: str) -> dict:
    return {
        "i": idx, "title": title, "body": f"{title}的正文，按口吻写的。",
        "reason": "群里正聊着这个方向", "refs": [], "audience": [],
        "keywords": ["关键词甲", "关键词乙"],
    }


def _posts_json(*posts: dict) -> str:
    return json.dumps({"posts": list(posts)}, ensure_ascii=False)


def _make_feeds(tmp_path, *, models=None, search=None) -> tuple:
    """构造一个 Feeds：画像已 ready、专岗没接（None=老路）。搜索默认给「没配置」桩。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (G1, 1_700_000_000.0),
        )
    settings = _settings()
    if models is None:
        models = FakeModelsQueue(ready=True)
    workers = FakeWorkers()
    profiles = FakeProfiles()
    profiles.entries_map[G1] = [{"category": "ongoing", "text": "在做开源硬件项目"}]
    topics = FakeTopics()
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings,
                  search=search if search is not None else _UnavailableSearch())
    return store, settings, feeds, models, workers, topics


def _batches(store: Store) -> list:
    return store.read().execute(
        "SELECT * FROM news_batches ORDER BY id").fetchall()


def _batch_stats(store: Store, batch_id: int) -> Any:
    return store.kv_get(f"feeds.batch_stats.{int(batch_id)}")


# ----------------------------------------------------------------------
# RSS-only：搜索没配 / 暂时坏，但订了 RSS → 照常出
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rss_only_without_search_configured(tmp_path) -> None:
    """搜索完全未配（available()=False）+ 订了 RSS → 照常取 RSS、打分、写帖、入库；
    不定关注点、不撒网、不补打开；批次统计带 source_mode="rss_only"。"""
    store, settings, feeds, models, workers, topics = _make_feeds(
        tmp_path,
        models=FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, topic="硬件")),
            _posts_json(_post(0, "RSS 新鲜资讯")),
        ]),
        search=_UnavailableSearch(),
    )
    rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                 feed_id="rG", now=NOW)

    async def fake_collect(gid, settings):
        return [_rss_cand("https://good.example.com/feed", "RSS 新鲜资讯",
                          "https://ok.example.com/fresh", NOW - 3600)]

    feeds._collect_rss = fake_collect  # 不打真网络，直接喂 RSS 候选

    with _TimePatch():
        kept = await feeds.prepare_news(G1)

    assert kept == 1, "RSS-only 这一轮要出 1 条"
    rows = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchall()
    assert len(rows) == 1 and "RSS 新鲜资讯" in rows[0]["title"]
    # 不定关注点（省 token）：模型调用里没有 feeds.focus / feeds.pick
    purposes = [str(k.get("purpose") or "") for _r, _m, k in models.calls]
    assert "feeds.focus" not in purposes, "RSS-only 不该花定关注点的 token"
    assert "feeds.pick" not in purposes, "RSS-only 不该有挑这步"
    # 不撒网、不补打开：workers 一次都没派
    assert workers.calls == [], "RSS-only 不该派任何子 agent"
    # 打分、写帖照走（不放宽）
    assert "feeds.score" in purposes and "feeds.post" in purposes
    # 批次统计看得出这一轮是 RSS-only
    batches = _batches(store)
    assert len(batches) == 1
    stats = _batch_stats(store, batches[0]["id"])
    assert isinstance(stats, dict) and stats.get("source_mode") == "rss_only"
    store.close()


@pytest.mark.asyncio
async def test_rss_only_when_search_temporarily_broken(tmp_path) -> None:
    """搜索暂时坏（探测抛 SearchUnavailable）+ 订了 RSS → 一样走 RSS-only。"""
    store, settings, feeds, models, workers, topics = _make_feeds(
        tmp_path,
        models=FakeModelsQueue(ready=True, replies=[
            _scores_json(_score(0, topic="硬件")),
            _posts_json(_post(0, "RSS 新鲜资讯")),
        ]),
        search=_BrokenSearch(),
    )
    rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                 feed_id="rG", now=NOW)

    async def fake_collect(gid, settings):
        return [_rss_cand("https://good.example.com/feed", "RSS 新鲜资讯",
                          "https://ok.example.com/fresh", NOW - 3600)]

    feeds._collect_rss = fake_collect

    with _TimePatch():
        kept = await feeds.prepare_news(G1)

    assert kept == 1
    batches = _batches(store)
    stats = _batch_stats(store, batches[0]["id"])
    assert isinstance(stats, dict) and stats.get("source_mode") == "rss_only"
    store.close()


# ----------------------------------------------------------------------
# 跳过原因要准确
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_search_and_no_rss_subscribed_skips_accurately(tmp_path) -> None:
    """搜索没配 + 一个 RSS 源都没订 → 跳过，原因要同时点出两件事。"""
    store, settings, feeds, models, workers, topics = _make_feeds(
        tmp_path, search=_UnavailableSearch())

    with _TimePatch():
        kept = await feeds.prepare_news(G1)

    assert kept == 0
    batches = _batches(store)
    assert len(batches) == 1 and int(batches[0]["skipped"] or 0) == 1
    note = str(batches[0]["note"] or "")
    assert "RSS" in note, f"原因要提 RSS：{note}"
    assert "搜索" in note, f"原因要提搜索：{note}"
    store.close()


@pytest.mark.asyncio
async def test_no_search_rss_subscribed_but_empty_skips_accurately(tmp_path) -> None:
    """搜索没配、订了 RSS 但这轮一条没取到 → 跳过，原因说明 RSS 没货。"""
    store, settings, feeds, models, workers, topics = _make_feeds(
        tmp_path, search=_UnavailableSearch())
    rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                 feed_id="rG", now=NOW)

    async def fake_collect(gid, settings):
        return []  # 源有，这轮没新条目

    feeds._collect_rss = fake_collect

    with _TimePatch():
        kept = await feeds.prepare_news(G1)

    assert kept == 0
    batches = _batches(store)
    assert len(batches) == 1 and int(batches[0]["skipped"] or 0) == 1
    note = str(batches[0]["note"] or "")
    assert "RSS" in note, f"原因要提 RSS 没取到：{note}"
    # 跳过轮也标 rss_only（网页能看出这轮走的是 RSS 入口）
    stats = _batch_stats(store, batches[0]["id"])
    assert isinstance(stats, dict) and stats.get("source_mode") == "rss_only"
    store.close()


@pytest.mark.asyncio
async def test_rss_feed_disabled_counts_as_no_subscription(tmp_path) -> None:
    """订了但都停用了 = 没订：搜索没配时照样按「没订 RSS」跳过。"""
    store, settings, feeds, models, workers, topics = _make_feeds(
        tmp_path, search=_UnavailableSearch())
    rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                 feed_id="rG", now=NOW)
    rss.toggle_feed(store, G1, "rG", enabled=False)

    with _TimePatch():
        kept = await feeds.prepare_news(G1)

    assert kept == 0
    note = str(_batches(store)[0]["note"] or "")
    assert "RSS" in note and "搜索" in note
    store.close()


# ----------------------------------------------------------------------
# 搜索可用 → 原逻辑（回归：RSS-only 分支不影响正常轮）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_available_runs_normal_round_even_with_rss(tmp_path) -> None:
    """搜索可用 + 订了 RSS：原逻辑——定关注点、撒网照常；RSS 并进候选池。"""
    from test_feeds_quality import _FOCUS_JSON

    store, settings, feeds, models, workers, topics = _make_feeds(
        tmp_path,
        models=FakeModelsQueue(ready=True, replies=[
            _FOCUS_JSON,
            PICK_FALLBACK_REPLY,
            _scores_json(_score(0, topic="硬件")),
            _posts_json(_post(0, "搜索找到的事")),
        ]),
        search=None,  # patch_two_phase_feeds 换假搜索
    )
    rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                 feed_id="rG", now=NOW)
    patch_two_phase_feeds(feeds, models, [_search_item_dict()])

    async def fake_collect(gid, settings):
        return [_rss_cand("https://good.example.com/feed", "RSS 新鲜资讯乙",
                          "https://ok.example.com/fresh-2", NOW - 3600)]

    feeds._collect_rss = fake_collect

    with _TimePatch():
        kept = await feeds.prepare_news(G1)

    assert kept >= 1
    purposes = [str(k.get("purpose") or "") for _r, _m, k in models.calls]
    assert "feeds.focus" in purposes, "搜索可用时照原定关注点"
    batches = _batches(store)
    stats = _batch_stats(store, batches[0]["id"])
    # 正常轮：stats 里没有 rss_only 标记
    assert not (isinstance(stats, dict) and stats.get("source_mode") == "rss_only")
    store.close()
