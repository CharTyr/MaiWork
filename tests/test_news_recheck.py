"""资讯候选原文直接打开与核对测试（2026-11 零子 agent 改版）：

日常不再派 `feeds-verify:` / `feeds-recheck:` 子 agent，所有搜索挑出的候选与 RSS 条目
一律由 `feeds._open_candidates` 直接调 `fetch_page` 打开原文、换用跳转 `final_url`、
提取页面发布日期并绑定正文证据；内容支撑、质量与新旧分别由 `_score` 与 `_hard_reject_*` 判定。
同时保留对 `news_recheck.opened_links`（`fetch_page` 与扩展抓正文工具记录解析）的单元测试。
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
        self._record_bodies(step, tid)
        return step["report"]

    @staticmethod
    def _record_bodies(step: Dict[str, Any], tid: str) -> None:
        """补打开真打开的页面，正文里写着它 keep 时抄回的 quote（fetch_page 在本轮记下的正文）。"""
        from fakes import record_fixture_pages

        opened = {str(e[-1] if isinstance(e, (tuple, list)) else e) for e in step.get("open", [])}
        opened |= {str(e[0]) for e in step.get("open", []) if isinstance(e, (tuple, list))}
        data = getattr(step.get("report"), "data", None) or {}
        for v in data.get("items") or []:
            if isinstance(v, dict) and str(v.get("url") or "") in opened:
                record_fixture_pages(tid, [dict(v, fetched=True)])

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
    c0 = _cand(0, title="只看过摘要的新闻", url="https://news.a.com/1")
    c0["summary"] = "搜索摘要里的一句"
    items = [
        c0,
        _cand(1, title="真打开过的新闻", url="https://news.b.com/2"),
    ]
    steps = [
        {"report": _ok({"items": items}), "search": True},
    ]
    store, feeds, workers, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    assert len(_accepted(_rows(store))) == 2
    assert workers.calls == []
    from CharTyr_MaiWork.maiwork.news_recheck import opened_links

    tid_rows = store.read().execute(
        "SELECT DISTINCT task_id FROM tool_calls WHERE tool='fetch_page' AND ok=1"
    ).fetchall()
    assert len(tid_rows) == 1
    opened, records = opened_links(store, str(tid_rows[0]["task_id"]))
    assert records >= 2
    assert "news.a.com/1" in opened and "news.b.com/2" in opened


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
    items = [_cand(0, title="标题党", url="https://news.a.com/1")]
    steps = [
        {"report": _ok({"items": items}), "search": True},
    ]
    scores = _scores_json(_score(0, junk=True, junk_reason="原文没提这件事"))
    store, feeds, workers, _ = _setup(tmp_path, items, steps, scores=scores)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    reason = _rej_reason(_rows(store), "news.a.com") or ""
    assert "原文没提这件事" in reason
    assert workers.calls == []


def test_stale_news_rejected(tmp_path) -> None:
    """线上真例：搜索结果没带日期，打开原文后页面读出过旧发布时间 → 按旧闻拒。"""
    item = _cand(0, title="发售时间的重要更新", url="https://news.a.com/1", published=None)
    item["page_published"] = "2026-09-10"
    steps = [
        {"report": _ok({"items": [item]}), "search": True},
    ]
    store, feeds, _, _ = _setup(tmp_path, [item], steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    reason = _rej_reason(_rows(store), "news.a.com") or ""
    assert "旧闻" in reason


def test_replaced_source_is_used(tmp_path) -> None:
    """打开时跳转到同一件事的最终地址 → 用新链接入库。"""
    item = _cand(0, title="换个来源", url="https://short.a.com/1")
    item["original_url"] = "https://other.b.com/same-story"
    steps = [
        {"report": _ok({"items": [item]}), "search": True},
    ]
    store, feeds, _, _ = _setup(tmp_path, [item], steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    acc = _accepted(_rows(store))
    assert "other.b.com" in acc[0]["url_key"]


def test_claimed_opened_but_no_record_gets_rechecked(tmp_path) -> None:
    """代码直接逐条调 fetch_page 打开两条候选并留痕，不再派 feeds-recheck: 子 agent。"""
    items = [
        _cand(0, title="说打开过其实没有", url="https://news.a.com/1", fetched=True),
        _cand(1, title="真打开过", url="https://news.b.com/2", fetched=True),
    ]
    steps = [
        {"report": _ok({"items": items}), "search": True},
    ]
    store, feeds, workers, _ = _setup(tmp_path, items, steps)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
    assert workers.calls == []
    fetched_inputs = {
        str(r["input"])
        for r in store.read().execute(
            "SELECT input FROM tool_calls WHERE tool='fetch_page' AND ok=1"
        ).fetchall()
    }
    assert "https://news.a.com/1" in fetched_inputs
    assert "https://news.b.com/2" in fetched_inputs


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
    """网页上「看了几篇」要把代码直接 fetch_page 打开成功的算进去。"""
    items = [_cand(0, title="只看过摘要", url="https://news.a.com/1")]
    steps = [
        {"report": _ok({"items": items}), "search": True},
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
