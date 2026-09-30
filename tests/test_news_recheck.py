"""资讯候选「补打开」测试（2026-09-29 用户定：主流程派一个子 agent 去打开、抓正文并核对，核对用子 agent 模型）。

背景（线上实测 2026-09-28 23:39 那轮）：找资讯的子 agent 15 分钟时间盒到点，只看过搜索摘要的
3 条交了回来（fetched=false），被第一道「原文没打开过/打不开」全扔，那轮一条没发。

现在：第一道之前，把「没打开过」+「说打开过但这轮没有打开记录」的候选一批交给一个补打开子 agent：
- 它用 fetch_page 打开原文（打不开可以 web_search 找同一件事的别的来源，换了就改 url）；
- 对照原文核对：摘要站不站得住（站不住 drop）、按原文重写摘要和原文依据、填发布时间、判旧闻（stale）；
- 代码只认它**真打开过**的链接（查这次补打开的 fetch_page 记录），嘴上说 keep 但没打开的照旧按没打开淘汰。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import WorkerReport

from test_feeds_quality import (
    GID,
    NOW,
    _TimePatch,
    _accepted,
    _cand,
    _make_feeds,
    _rejected,
    _rej_reason,
    _rows,
    _run,
    _score,
    _scores_json,
)


def _log_fetch(store: Store, task_id: str, url: str, *, ok: bool = True, final: str = "") -> None:
    out = f"取到正文 500 字（Jina）（最终地址：{final}）" if final else "取到正文 500 字（Jina）"
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, ?, ?, '子 agent #1', 'fetch_page', ?, ?, ?)",
            (NOW, GID, task_id, url, out if ok else "", 1 if ok else 0),
        )


def _log_search(store: Store, task_id: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, ?, ?, '子 agent #1', 'web_search', '搜', '8 条', 1)",
            (NOW, GID, task_id),
        )


class SeqWorkers:
    """按顺序回放：两阶段恒生效后，「找资讯那一步」的预置由 seed_collect 挪给
    假搜索（2026-10-01 起撒网是代码按计划搜，不再派子 agent），核验按链接交回；
    第二步才是补打开的子 agent。每一步可以顺带「打开」一些网址
    （往 tool_calls 写这次 task_id 的 fetch_page 记录）。"""

    def __init__(self, store_ref: list, steps: list) -> None:
        self.store_ref = store_ref
        self.steps = list(steps)
        self.calls: List[Dict[str, Any]] = []
        self._collect_step: Dict[str, Any] | None = None  # 第一步（找资讯）回放缓存

    def _log(self, step: Dict[str, Any], tid: str) -> None:
        store = self.store_ref[0]
        for entry in step.get("open", []):
            if isinstance(entry, (tuple, list)) and entry:  # (url, final)：跳转后的最终地址
                _log_fetch(store, tid, entry[0], final=str(entry[1]) if len(entry) > 1 else "")
            else:
                _log_fetch(store, tid, entry)
        if step.get("search"):
            _log_search(store, tid)

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        tid = str(kwargs.get("task_id") or "")
        from fakes import patch_two_phase_feeds, two_phase_workers_run

        if tid.startswith("feeds-verify:") and self._collect_step is not None:
            # 核验交回「找资讯」那步的预置 items（按 brief 里列的链接分）
            return await two_phase_workers_run(self._collect_step["report"], brief, kwargs)
        # 补打开等后续步骤：按原顺序回放（2026-10-01 起撒网不派子 agent，第一步直接落到这）
        assert self.steps, "用例步骤不够用"
        step = self.steps.pop(0)
        self._log(step, tid)
        if isinstance(step.get("report"), BaseException):
            raise step["report"]
        return step["report"]

    def seed_collect(self, feeds: Any, models: Any = None) -> None:
        """把「找资讯那一步」的预置挪成现在的两步：登记簿候选由假搜索出 + 顺带记录落库。

        2026-10-01 前：第一步是派 feeds-discover: 子 agent；现在代码撒网不派工，
        预置 report.data["items"] 交给 FakePlannedSearch 出（patch_two_phase_feeds），
        「顺带打开 / 搜过」的记录仍挂 collect_mark 名义（补打开 probe 只查它）。
        collect_mark 只有运行时才知道 → 落库延后到 seeds 那一刻（patch feeds._collect_two_phase）。
        """
        from fakes import patch_two_phase_feeds

        step = self.steps.pop(0)
        self._collect_step = step
        data = getattr(step["report"], "data", None) or {}
        items = list(data.get("items") or [])
        patch_two_phase_feeds(feeds, models, items)
        store = self.store_ref[0]

        orig = feeds._collect_two_phase

        async def _seeded(gid, focus, settings, *, collect_mark, stats_out):
            self._log(step, collect_mark)
            for url in step.get("collect_open", []):
                _log_fetch(store, collect_mark, url)
            return await orig(gid, focus, settings, collect_mark=collect_mark, stats_out=stats_out)

        feeds._collect_two_phase = _seeded


def _ok(data: dict) -> WorkerReport:
    return WorkerReport(ok=True, summary="好了", data=data, evidence=[], steps=3)


def _verdict(index: int, url: str, *, verdict: str = "keep", stale: bool = False, reason: str = "",
             summary: str = "按原文重写的摘要：两三句讲清楚。", quote: str = "原文里的一句话。",
             published: str = "2026-09-24") -> dict:
    return {"index": index, "verdict": verdict, "url": url, "summary": summary, "quote": quote,
            "published": published, "stale": stale, "reason": reason}


def _setup(tmp_path, items, steps, *, scores=None):
    ref: list = [None]
    workers = SeqWorkers(ref, steps)
    if scores is None:
        scores = _scores_json(*[_score(i, topic=f"话题{i}") for i in range(len(items))])
    store, settings, feeds, models, _w, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores, workers=workers
    )
    ref[0] = store
    # 「找资讯那一步」从 workers 回放挪成假搜索供给（撒网不再派子 agent），顺带记录照挂 collect 名义
    workers.seed_collect(feeds, models)
    return store, feeds, workers, models


def test_unopened_item_is_opened_checked_and_kept(tmp_path) -> None:
    items = [
        _cand(0, title="只看过摘要的新闻", url="https://news.a.com/1", fetched=False, quote="搜索摘要里的一句"),
        _cand(1, title="真打开过的新闻", url="https://news.b.com/2"),
    ]
    steps = [
        {"report": _ok({"items": items}), "open": ["https://news.b.com/2"], "search": True},
        {"report": _ok({"items": [_verdict(0, "https://news.a.com/1")]}), "open": ["https://news.a.com/1"]},
    ]
    store, feeds, workers, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    assert len(_accepted(_rows(store))) == 2
    # 补打开只派了一次，只带没打开过的那条（两阶段恒生效：前面还多撒网 + 核验两个环节）
    recheck_calls = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-recheck:")]
    assert len(recheck_calls) == 1
    rc = recheck_calls[0]
    assert str(rc["task_id"]).startswith("feeds-recheck:")
    assert set(rc["tools"]) == {"fetch_page", "web_search"}
    assert rc["deadline_ts"] and rc["deadline_ts"] > NOW
    assert "https://news.a.com/1" in rc["brief"]
    assert "https://news.b.com/2" not in rc["brief"]
    assert "首页" in rc["brief"]  # 叮嘱别开首页 / 列表页
    assert "今天" in rc["brief"]  # 给了今天的日期（判旧闻要用）


def test_says_keep_but_never_opened_is_still_rejected(tmp_path) -> None:
    items = [_cand(0, title="嘴上说看过", url="https://news.a.com/1", fetched=False)]
    steps = [
        {"report": _ok({"items": items}), "search": True},
        {"report": _ok({"items": [_verdict(0, "https://news.a.com/1")]})},  # 没有 open
    ]
    store, feeds, _, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    assert "原文没打开过" in (_rej_reason(_rows(store), "news.a.com") or "")


def test_drop_when_page_does_not_support_it(tmp_path) -> None:
    items = [_cand(0, title="标题党", url="https://news.a.com/1", fetched=False)]
    steps = [
        {"report": _ok({"items": items}), "search": True},
        {"report": _ok({"items": [_verdict(0, "https://news.a.com/1", verdict="drop", reason="原文没提这件事")]}),
         "open": ["https://news.a.com/1"]},
    ]
    store, feeds, _, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    reason = _rej_reason(_rows(store), "news.a.com") or ""
    assert "补打开核对" in reason and "原文没提这件事" in reason


def test_stale_news_rejected(tmp_path) -> None:
    """线上真例：9 月 26 日转发的公告，正文说「争取 9 月 20 日上线」——事情已经过去了。"""
    items = [_cand(0, title="发售时间的重要更新", url="https://news.a.com/1", fetched=False)]
    steps = [
        {"report": _ok({"items": items}), "search": True},
        {"report": _ok({"items": [_verdict(0, "https://news.a.com/1", stale=True,
                                            reason="正文说争取 9 月 20 日上线，已经过去")]}),
         "open": ["https://news.a.com/1"]},
    ]
    store, feeds, _, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    reason = _rej_reason(_rows(store), "news.a.com") or ""
    assert "旧闻" in reason and "9 月 20 日" in reason


def test_replaced_source_is_used(tmp_path) -> None:
    """原链接打不开，子 agent 找了同一件事的别的来源并打开了 → 用新链接。"""
    items = [_cand(0, title="换个来源", url="https://blocked.a.com/1", fetched=False)]
    steps = [
        {"report": _ok({"items": items}), "search": True},
        {"report": _ok({"items": [_verdict(0, "https://other.b.com/same-story")]}),
         "open": ["https://other.b.com/same-story"]},
    ]
    store, feeds, _, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    acc = _accepted(_rows(store))
    assert "other.b.com" in acc[0]["url_key"]


def test_claimed_opened_but_no_record_gets_rechecked(tmp_path) -> None:
    """子 agent 说 fetched=true，但这轮的打开记录里没有这个链接 → 也补打开核对。"""
    items = [
        _cand(0, title="说打开过其实没有", url="https://news.a.com/1", fetched=True),
        _cand(1, title="真打开过", url="https://news.b.com/2", fetched=True),
    ]
    steps = [
        {"report": _ok({"items": items}), "open": ["https://news.b.com/2"], "search": True,
         # 真打开过的那条，把「这轮打开过」记到 collect 名义下（补打开的 probe 只查它）
         "collect_open": ["https://news.b.com/2"]},
        {"report": _ok({"items": [_verdict(0, "https://news.a.com/1")]}), "open": ["https://news.a.com/1"]},
    ]
    store, feeds, workers, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    # 嘴上说 opened、没留记录的也照样派去补打开
    recheck_calls = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-recheck:")]
    assert len(recheck_calls) == 1
    assert "https://news.a.com/1" in str(recheck_calls[0]["brief"])


def test_final_url_counts_as_opened(tmp_path) -> None:
    """交回的是跳转后的地址：打开记录里的「最终地址」也算打开过，不用补。"""
    items = [_cand(0, title="跳转过的", url="https://news.a.com/final", fetched=True)]
    ref: list = [None]
    # 找资讯那轮的 task_id 要等 prepare_news 生成：collect 那步的「打开记录」顺带写成
    # 「短链 → 最终地址」（(url, final) 元组，见 SeqWorkers._log）
    workers = SeqWorkers(ref, [{"report": _ok({"items": items}), "search": True,
                                 "open": [("https://short.a.com/x", "https://news.a.com/final")]}])
    store, settings, feeds, models, _w, topics, _ = _make_feeds(
        tmp_path, items=items, scores=_scores_json(_score(0)), workers=workers
    )
    ref[0] = store
    workers.seed_collect(feeds, models)
    _log_fetch(store, "占位", "x")  # 不相干的记录
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    assert not [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-recheck:")]  # 没派补打开


def test_all_opened_no_recheck(tmp_path) -> None:
    items = [_cand(0, url="https://news.a.com/1"), _cand(1, url="https://news.b.com/2")]
    steps = [{"report": _ok({"items": items}), "open": ["https://news.a.com/1", "https://news.b.com/2"], "search": True}]
    store, feeds, workers, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    assert not [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-recheck:")]


def test_recheck_failure_does_not_break_the_round(tmp_path) -> None:
    items = [
        _cand(0, title="没打开", url="https://news.a.com/1", fetched=False),
        _cand(1, title="正常", url="https://news.b.com/2"),
    ]
    steps = [
        {"report": _ok({"items": items}), "open": ["https://news.b.com/2"], "search": True},
        {"report": RuntimeError("补打开的子 agent 炸了")},
    ]
    store, feeds, _, _ = _setup(tmp_path, items, steps, scores=_scores_json(_score(0, topic="t")))
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    assert "原文没打开过" in (_rej_reason(_rows(store), "news.a.com") or "")


def test_batch_stats_count_recheck_pages(tmp_path) -> None:
    """网页上「看了几篇」要把补打开的也算进去。"""
    items = [_cand(0, title="只看过摘要", url="https://news.a.com/1", fetched=False)]
    steps = [
        {"report": _ok({"items": items}), "search": True},
        {"report": _ok({"items": [_verdict(0, "https://news.a.com/1")]}), "open": ["https://news.a.com/1"]},
    ]
    store, feeds, _, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    batch = store.read().execute("SELECT id FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    stats = store.kv_get(f"feeds.batch_stats.{int(batch['id'])}")
    assert stats["pages"] == 1 and stats["searches"] == 1


def _log_mcp_fetch(store: Store, task_id: str, url: str, *, ok: bool = True, final: str = "") -> None:
    import json as _json
    inp = _json.dumps({"url": url}, ensure_ascii=False)
    out = f"Title: 标题\nURL: {final or url}\n\n正文……" if ok else ""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, ?, ?, '子 agent #1', 'mcp_keenable_fetch_page_content', ?, ?, ?)",
            (NOW, GID, task_id, inp, out, 1 if ok else 0),
        )


def test_mcp_fetch_page_content_counts_as_opened(tmp_path):
    """资讯收集用扩展抓正文工具 mcp_keenable_fetch_page_content 打开过原文的候选：
    不再被当成「说打开过但没打开」送去补打开。本地直接对 opened_links 断言。"""
    from CharTyr_MaiWork.maiwork.news_recheck import opened_links

    store = Store(tmp_path / "t.db")
    store.migrate()
    try:
        tid = "feeds-collect:900000001:1"
        _log_mcp_fetch(store, tid, "https://mcp.example/news-1")
        _log_search(store, tid)  # 搜索记录不算打开
        opened, records = opened_links(store, tid)
        assert records == 2
        assert "mcp.example/news-1" in opened
    finally:
        store.close()


def test_mcp_search_tool_does_not_count_as_opened(tmp_path):
    from CharTyr_MaiWork.maiwork.news_recheck import opened_links

    store = Store(tmp_path / "t.db")
    store.migrate()
    try:
        tid = "feeds-collect:900000001:2"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
                " VALUES (?, ?, ?, '子 agent #1', 'mcp_keenable_search_web_pages',"
                " '{\"query\": \"q\"}', '1. x https://search.example/hit', 1)",
                (NOW, GID, tid),
            )
        opened, records = opened_links(store, tid)
        assert records == 1
        assert "search.example/hit" not in opened
    finally:
        store.close()


def test_mcp_final_url_line_counts_as_opened(tmp_path):
    """抓正文工具 output 里「URL: <最终地址>」也算打开过（跳转 / 带参数的最终地址）。"""
    from CharTyr_MaiWork.maiwork.news_recheck import opened_links

    store = Store(tmp_path / "t.db")
    store.migrate()
    try:
        tid = "feeds-collect:900000001:3"
        _log_mcp_fetch(store, tid, "https://short.example/s", final="https://long.example/final?a=1")
        opened, records = opened_links(store, tid)
        assert "short.example/s" in opened
        assert "long.example/final?a=1" in opened
    finally:
        store.close()
