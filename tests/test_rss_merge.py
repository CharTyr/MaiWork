"""RSS 条目并进候选池 + 真机接线测试（2026-11，docs/02-设计.md §4.1）。

线上问题：RSS 取回后只当「优先看这些链接」写进子 agent 的 brief，子 agent 不一定交回，
订阅源等于白订；另外 `_rss_transport` 忘了赋值，取源全走 AttributeError（只报「意外错误」）。
修法：
- 每轮取回后按「新的在前、跨源轮询」最多 6 条**直接并进候选列表**，和搜索候选走同一套
  硬淘汰 / 7 天新鲜度 / 打分 / 话题饱和（不另开绿灯）；按 url_key 和候选、已入库
  news_items 去重；条目带 from_rss + 源标题（sources.site 用源标题）便于排查；
- 真 app + FakeCtx + MockTransport 跑一次真实取源路径：解析出条目、last_ok_ts > 0、
  没有「意外错误」。
"""

from __future__ import annotations

import asyncio
import json
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from fakes import (
    PICK_FALLBACK_REPLY,
    FakeCtx,
    FakeModelsQueue,
    FakeProfiles,
    ensure_pick_fallback,
    focus_reply,
    patch_two_phase_feeds,
    two_phase_workers_run,
)

from CharTyr_MaiWork.maiwork import rss
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds, _normalize_url
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
G1 = "900000001"


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


class _TimePatch:
    """feeds.clock.now 固定成 NOW（RSS 的 pubDate 都围着 NOW 造）。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class FakeWorkers:
    """假的 workers.run：预置一份 WorkerReport，并记录 brief。

    两阶段恒生效：feeds-discover 把预置 items 种进撒网登记簿、feeds-verify 按 brief 链接交回。
    """

    def __init__(self, report: Any = None) -> None:
        self.report = report
        self.calls: List[Dict[str, Any]] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        return await two_phase_workers_run(self.report, brief, kwargs)


class FakeTopics:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


class _EmptySearch:
    async def search(self, q, **kw):
        return []


def _ok_report(items: list[dict]) -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data={"items": items}, evidence=[], steps=3)


_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"
_FOCUS_JSON = focus_reply("FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向")


def _search_item(idx: int, *, title: str, url: str, published: Any = NOW - 3600) -> dict:
    return {
        "title": title,
        "url": url,
        "summary": f"摘要{idx}：两三句话讲清楚这件事。",
        "kind": "news",
        "published": published,
        "fetched": True,
        "quote": _QUOTE,
        "paywall": False,
    }


def _rss_cand(feed: str, title: str, url: str, ts: float) -> dict:
    """造一条 _collect_rss 交回形状的 RSS 候选（跟 feeds._collect_rss 里的字段对齐）。"""
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
        "_rss_feed": feed,
        "_rss_title": "源标题",
    }


def _score(idx: int, *, topic: str | None = None, relevance: float = 4,
           timeliness: float = 4, chat: float = 4) -> dict:
    return {
        "i": idx, "info": 4, "source": 4, "relevance": relevance, "timeliness": timeliness,
        "chat": chat, "profile": 0, "topic": topic if topic is not None else f"话题{idx}",
        "sensitive": False, "grounded": True, "junk": False, "junk_reason": "",
        "same_as_recent": False, "why": "和画像对得上", "icon": "robot",
    }


def _scores_json(*scores: dict) -> str:
    return json.dumps({"scores": list(scores)}, ensure_ascii=False)


def _posts_json(*posts: dict) -> str:
    return json.dumps({"posts": list(posts)}, ensure_ascii=False)


def _post(idx: int, title: str) -> dict:
    return {
        "i": idx, "title": title, "body": f"{title}的正文，按口吻写的。",
        "reason": "群里正聊着这个方向", "refs": [], "audience": [],
        "keywords": ["关键词甲", "关键词乙"],
    }


def _make_feeds(tmp_path, *, models=None, workers=None) -> tuple:
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
    ensure_pick_fallback(models)
    if workers is None:
        workers = FakeWorkers(_ok_report([]))
    profiles = FakeProfiles()
    profiles.entries_map[G1] = [{"category": "ongoing", "text": "在做开源硬件项目"}]
    topics = FakeTopics()
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings,
                  search=_EmptySearch())
    return store, settings, feeds, models, workers, topics


def _rss_xml(items: list[tuple[str, str, float]]) -> str:
    """items: [(title, link, epoch)] → 一份 RSS 2.0 文档。"""
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<pubDate>{formatdate(ts, usegmt=True)}</pubDate>"
        f"<description>RSS 源里的摘要。</description></item>"
        for t, u, ts in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>好博客</title>'
        + body + "</channel></rss>"
    )


def _rejected_rows(store: Store) -> list:
    return store.read().execute(
        "SELECT * FROM news_items WHERE rejected=1 ORDER BY id").fetchall()


def _accepted_rows(store: Store) -> list:
    return store.read().execute(
        "SELECT * FROM news_items WHERE rejected=0 ORDER BY id").fetchall()


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 并池本身：新的在前、跨源轮询不超过 6 条、url 去重
# ----------------------------------------------------------------------


class TestMergeRssCandidates:
    def test_round_robin_cap_six_newest_first(self, tmp_path) -> None:
        """两个源各 3 条 → 只并 6 条，两个源轮着来（一个源占不满）；各自新的先并。"""
        store, settings, feeds, _models, _workers, _topics = _make_feeds(tmp_path)
        f_a, f_b = "https://a.example.com/feed", "https://b.example.com/feed"
        rss_items = [
            _rss_cand(f_a, "A1", "https://a.example.com/1", NOW - 900),
            _rss_cand(f_a, "A2", "https://a.example.com/2", NOW - 700),
            _rss_cand(f_a, "A3", "https://a.example.com/3", NOW - 500),
            _rss_cand(f_b, "B1", "https://b.example.com/1", NOW - 100),
            _rss_cand(f_b, "B2", "https://b.example.com/2", NOW - 300),
            _rss_cand(f_b, "B3", "https://b.example.com/3", NOW - 600),
        ]
        candidates: list[dict] = []

        merged = feeds._merge_rss_candidates(G1, settings, candidates, rss_items)

        assert merged == 6, "上限 6 条"
        assert len(candidates) == 6
        # 跨源轮询：B 的最新一条（NOW-100）最先，接着 A 的最新一条（NOW-500），如此交替
        assert [c["_rss_feed"] for c in candidates] == [f_b, f_a, f_b, f_a, f_b, f_a]
        assert [c["title"] for c in candidates] == ["B1", "A3", "B2", "A2", "B3", "A1"]
        assert all(c["from_rss"] is True for c in candidates)
        store.close()

    def test_url_dedup_against_candidates_and_stored(self, tmp_path) -> None:
        """url_key 和这批候选、和已入库的 news_items 重复的都不并；重复的 RSS 自身只留一条。"""
        store, settings, feeds, *_ = _make_feeds(tmp_path)
        dup_in_batch = "https://s.example.com/same"
        dup_stored = "https://old.example.com/done"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, url_key, summary, score,"
                " created, rejected) VALUES (0, ?, '已出过的', ?, '摘要', 4.0, ?, 0)",
                (G1, _normalize_url(dup_stored), NOW - 86400),
            )
        candidates = [
            {"title": "搜索候选", "url": dup_in_batch, "summary": "摘要", "kind": "news",
             "fetched": True, "quote": _QUOTE, "url_key": _normalize_url(dup_in_batch)},
        ]
        rss_items = [
            _rss_cand("https://a.example.com/feed", "候选里有了", dup_in_batch, NOW - 100),
            _rss_cand("https://a.example.com/feed", "库里有了", dup_stored, NOW - 200),
            _rss_cand("https://a.example.com/feed", "新的", "https://a.example.com/new", NOW - 300),
            _rss_cand("https://b.example.com/feed", "从别的源又给一遍", "https://a.example.com/new", NOW - 400),
        ]

        # 候选和入库时间都围着 NOW 造；判重窗口也必须用同一测试时钟。
        with _TimePatch():
            merged = feeds._merge_rss_candidates(G1, settings, candidates, rss_items)

        assert merged == 1, "只有一条真正新的能并进来"
        assert len(candidates) == 2
        assert candidates[1]["title"] == "新的"
        assert all(c["url"] != dup_in_batch and c["url"] != dup_stored for c in candidates[1:])
        store.close()

    def test_same_title_different_url_merged_once(self, tmp_path) -> None:
        """同一标题不同链接（线上回放：机核同一篇文章和视频各一条）只并一条；
        和这批候选标题相同的也不并。标题比较忽略空白和大小写。"""
        store, settings, feeds, *_ = _make_feeds(tmp_path)
        candidates = [
            {"title": "搜索里已有的 标题", "url": "https://s.example.com/x", "summary": "摘要",
             "kind": "news", "fetched": True, "quote": _QUOTE,
             "url_key": _normalize_url("https://s.example.com/x")},
        ]
        rss_items = [
            _rss_cand("https://a.example.com/feed", "西蒙教你怎么点涮羊肉！| 核吃海塞（下）",
                      "https://a.example.com/articles/1", NOW - 100),
            _rss_cand("https://a.example.com/feed", "西蒙教你怎么点涮羊肉！ | 核吃海塞（下）",
                      "https://a.example.com/videos/1", NOW - 200),
            _rss_cand("https://b.example.com/feed", "搜索里已有的标题", "https://b.example.com/y", NOW - 300),
        ]

        merged = feeds._merge_rss_candidates(G1, settings, candidates, rss_items)

        assert merged == 1
        assert candidates[1]["url"] == "https://a.example.com/articles/1"
        store.close()


# ----------------------------------------------------------------------
# 并进池后照走同一套门槛：新鲜度硬拒 / 和搜索候选一起打分
# ----------------------------------------------------------------------


_GOOD = (("RSS 新鲜资讯", "https://ok.example.com/fresh", NOW - 3600),)


class TestRssInPipeline:
    @pytest.mark.asyncio
    async def test_old_rss_item_hard_rejected(self, tmp_path) -> None:
        """RSS 里 10 天前的资讯（kind=news）照走 7 天新鲜度硬规则，被硬拒；新的那条照常出。"""
        old_ts = NOW - 10 * 86400
        xml = _rss_xml([
            ("RSS 十天前的旧闻", "https://ok.example.com/old", old_ts),
            ("RSS 新鲜资讯", "https://ok.example.com/fresh", NOW - 3600),
        ])
        store, settings, feeds, models, workers, topics = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="硬件")),
                _posts_json(_post(0, "RSS 新鲜资讯")),
            ]),
            workers=FakeWorkers(_ok_report([])),  # 搜索子 agent 什么都没交回，池里只有 RSS
        )
        feeds._rss_transport = httpx.MockTransport(lambda req: httpx.Response(200, text=xml))
        rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                     feed_id="rG", now=NOW)

        with _TimePatch():
            kept = await feeds.prepare_news(G1)

        assert kept == 1, "只有新的那条能出"
        rejected = _rejected_rows(store)
        assert len(rejected) == 1
        assert rejected[0]["title"] == "RSS 十天前的旧闻"
        assert rejected[0]["reject_gate"] == "hard"
        assert "旧闻" in str(rejected[0]["reject_reason"])
        accepted = _accepted_rows(store)
        assert [r["title"] for r in accepted] == ["RSS 新鲜资讯"]
        store.close()

    @pytest.mark.asyncio
    async def test_rss_candidates_scored_with_search_candidates(self, tmp_path) -> None:
        """RSS 条目并进候选列表后，和搜索候选在同一批里打分（打分提示词两条都在、都拿到分）。"""
        xml = _rss_xml(_GOOD)
        store, settings, feeds, models, workers, topics = _make_feeds(
            tmp_path,
            models=FakeModelsQueue(ready=True, replies=[
                _FOCUS_JSON,
                _scores_json(_score(0, topic="搜索话题"), _score(1, topic="RSS话题")),
                _posts_json(_post(0, "搜索来的候选"), _post(1, "RSS 新鲜资讯")),
            ]),
            workers=FakeWorkers(_ok_report([
                _search_item(0, title="搜索来的候选", url="https://s.example.com/one"),
            ])),
        )
        # 2026-10-01 撒网改代码按计划搜：「搜索回来的候选」挪给假搜索出（替换 _EmptySearch）
        patch_two_phase_feeds(feeds, models, [{
            "title": "搜索来的候选", "url": "https://s.example.com/one", "summary": "摘要",
            "kind": "news", "published": NOW - 3600, "quote": _QUOTE,
        }])
        feeds._rss_transport = httpx.MockTransport(lambda req: httpx.Response(200, text=xml))
        rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                     feed_id="rG", now=NOW)

        with _TimePatch():
            kept = await feeds.prepare_news(G1)

        assert kept == 2, "搜索候选和 RSS 候选都过门槛"
        # 打分提示词里两条都在（RSS 那条排在被追加的后面；
        # 两阶段后队列里隔着 feeds.pick，不能按下标 1 拿，按 purpose 找）
        score_call = next(c for c in models.calls if str(c[2].get("purpose") or "") == "feeds.score")
        score_prompt = str(score_call[1][0]["content"])
        assert "搜索来的候选" in score_prompt and "RSS 新鲜资讯" in score_prompt
        assert "https://ok.example.com/fresh" in score_prompt
        accepted = {r["title"]: r for r in _accepted_rows(store)}
        assert set(accepted) == {"搜索来的候选", "RSS 新鲜资讯"}
        # RSS 那条真的被打过分（不是绕过打分混进来的），来源显示源标题
        rss_row = accepted["RSS 新鲜资讯"]
        assert json.loads(rss_row["scores"])["avg"] >= 4.0
        assert json.loads(rss_row["sources"])[0]["site"] == "好博客"
        store.close()


# ----------------------------------------------------------------------
# 真 app：真实取源路径（_rss_transport 接线）
# ----------------------------------------------------------------------


def _rss_handler(request: httpx.Request) -> httpx.Response:
    if "good" in str(request.url):
        # pubDate 按真实时钟造一小时前（这条测试不固定 feeds.clock.now）
        import time

        xml = _rss_xml([("RSS 好文", "https://ok.example.com/rss1", time.time() - 3600)])
        return httpx.Response(200, text=xml)
    return httpx.Response(404, text="no")


class TestRealRssCollect:
    @pytest.mark.asyncio
    async def test_real_app_collect_parses_and_marks_ok(self, tmp_path, caplog) -> None:
        """真 app（FakeCtx + MockTransport）跑一次取源：解析出条目、last_ok_ts > 0、无「意外错误」。"""
        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
            "console": {"listen": "127.0.0.1:18663", "password": "pw-123456", "public_url": ""},
            "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test-not-real",
                       "main": "main-m", "worker": "worker-m"},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
        ctx = FakeCtx({"config.get": "987654321"})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        app.rss_transport = httpx.MockTransport(_rss_handler)
        await app.start()
        try:
            rss.add_feed(app.store, G1, url="https://good.example.com/feed", title="好博客",
                         feed_id="rG", now=NOW)
            import logging

            with caplog.at_level(logging.INFO):
                items = await app.feeds._collect_rss(G1, app.get_settings())

            assert [i["title"] for i in items] == ["RSS 好文"], "条目要解析出来"
            assert items[0]["from_rss"] is True
            assert items[0]["_rss_title"] == "好博客"
            row = rss.list_feeds(app.store, G1)[0]
            assert row["last_ok_ts"] > 0, "取源成功要记 last_ok_ts"
            assert "意外错误" not in str(row["last_error"])
            assert "意外错误" not in caplog.text
        finally:
            await app.stop()
