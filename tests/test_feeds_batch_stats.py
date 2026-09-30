"""feeds 每轮资讯统计：搜了几次（web_search 调用数）、看了几篇（fetch_page 成功数）、
收了几条（最终写入的资讯条数）。

- 每轮（batch）一份，存 kv["feeds.batch_stats.<batch_id>"]（不动 store.py）；
- news_view 的批次条目按轮暴露 stats = {searches, pages, kept}；
- 跳过的轮（_skipped_batch）也写一份全 0，前端能统一读；收集阶段之后才失败的轮
  （例如打分失败）保留已经发生的 searches / pages，kept=0；
- 老数据（这功能之前落的批次）读出来是 None，前端显示「没统计」。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fakes import FakeModelsQueue, ensure_pick_fallback, two_phase_workers_run

from test_feeds import (  # noqa: F401
    GID,
    NOW,
    UnavailableSearch,
    _FOCUS_JSON,
    _SCORES_JSON,
    _WORKER_ITEMS,
    _TimePatch,
    _make_feeds,
    _ok_report,
    _run,
    _seed_batch_and_items,
)

from CharTyr_MaiWork.maiwork.models import ModelError


class CountingWorkers:
    """假 workers：先把这轮的工具调用按 kwargs["task_id"] 落进 tool_calls（模拟真 workers
    的落库），再按两阶段派发出报告——用来验证 feeds 真的把每轮的工具用量算成统计。

    2026-10-01 起撒网是代码按计划搜（不再派子 agent）：web_search 记录由这些用例
    挂在 collect 标记（feeds-collect:）名下模拟代码真搜了这么多次；fetch_page 记录
    照旧挂在第一组核验（feeds-verify: 的 :0 后缀）名下。
    """

    def __init__(self, store, report: Any, rows: list[tuple[str, bool, str]]) -> None:
        self._store = store
        self.report = report
        self.rows = list(rows)
        self.calls: List[Dict[str, Any]] = []

    def _record(self, mark: str, want_tool: str) -> None:
        with self._store.tx() as conn:
            for tool, ok, inp in self.rows:
                if tool != want_tool:
                    continue
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                    " VALUES (?, ?, ?, '子 agent #1', ?, ?, 'out', 3, ?, '')",
                    (NOW, GID, mark, tool, inp, 1 if ok else 0),
                )

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        mark = str(kwargs.get("task_id") or "")
        if mark.startswith("feeds-verify:") and mark.endswith(":0"):
            self._record(mark, "fetch_page")
        return await two_phase_workers_run(self.report, brief, kwargs)


def _rows() -> list[tuple[str, bool, str]]:
    return [
        ("web_search", True, "搜 1"),
        ("web_search", False, "搜 2（失败也算搜了一次）"),
        # 打开记录要对得上候选的链接：说打开过却没记录的候选会被派去补打开（news_recheck）
        ("fetch_page", True, "https://example.com/board"),
        ("fetch_page", True, "https://news.com/llm-deploy"),
        ("fetch_page", True, "https://food.com/peach"),
        ("fetch_page", False, "https://a.example/3"),  # 没打开的页不算「看过」
    ]


def test_batch_stats_recorded_and_exposed(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON])
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path, models=models)
    counting = CountingWorkers(store, _ok_report(_WORKER_ITEMS), _rows())
    feeds._workers = counting  # 换成会落 tool_calls 的假 workers（真 workers 也这么落）
    # 撒网（代码按计划搜）这轮真搜了这么多次，按老 collect 标记落库（统计按它点数）
    orig_two_phase = feeds._collect_two_phase

    async def _seeded_two_phase(gid, focus, _settings, *, collect_mark, stats_out):
        counting._record(collect_mark, "web_search")
        return await orig_two_phase(gid, focus, _settings, collect_mark=collect_mark, stats_out=stats_out)

    feeds._collect_two_phase = _seeded_two_phase

    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 2
        batches = feeds.news_view(GID)

    # 2026-10-01 起撒网不再派子 agent：全程没有任何 feeds-discover: 派工
    assert counting.calls
    assert not any(str(c.get("task_id") or "").startswith("feeds-discover:") for c in counting.calls)
    assert len(batches) == 1
    stats = batches[0]["stats"]
    # 两阶段恒生效后 stats 还带漏斗（funnel）；basic 三个数按老口径对
    assert [stats[k] for k in ("searches", "pages", "kept")] == [2, 3, 2]

    # 批次记录上真的落了 kv（按 batch id，不动 store.py 的表结构）
    saved = store.kv_get(f"feeds.batch_stats.{batches[0]['id']}")
    assert [saved[k] for k in ("searches", "pages", "kept")] == [2, 3, 2]


def test_skipped_batch_stats_all_zero(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(
        tmp_path, search=UnavailableSearch()
    )
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
        batches = feeds.news_view(GID)
    assert len(batches) == 1
    assert batches[0]["skipped"] is True
    assert batches[0]["stats"] == {"searches": 0, "pages": 0, "kept": 0}
    assert store.kv_get(f"feeds.batch_stats.{batches[0]['id']}") == {
        "searches": 0, "pages": 0, "kept": 0,
    }


def test_score_failure_keeps_collected_counts(tmp_path) -> None:
    """收集成功、打分失败 → 这轮记 skipped，但已经搜过/看过的次数保留。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, ModelError("打分端点挂了")])
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path, models=models)
    counting = CountingWorkers(store, _ok_report(_WORKER_ITEMS), _rows()[:3])
    feeds._workers = counting
    orig_two_phase = feeds._collect_two_phase

    async def _seeded_two_phase(gid, focus, _settings, *, collect_mark, stats_out):
        counting._record(collect_mark, "web_search")
        return await orig_two_phase(gid, focus, _settings, collect_mark=collect_mark, stats_out=stats_out)

    feeds._collect_two_phase = _seeded_two_phase

    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
        batches = feeds.news_view(GID)
    assert len(batches) == 1
    assert batches[0]["skipped"] is True
    stats = batches[0]["stats"]
    # 收集阶段之后才失败：已经搜过 / 看过的次数保留（funnel 另带，见上）
    assert [stats[k] for k in ("searches", "pages", "kept")] == [2, 1, 0]


def test_old_batch_has_no_stats(tmp_path) -> None:
    """老数据：这功能之前落的批次没有 kv → stats 为 None（前端显示「没统计」）。"""
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path)
    with _TimePatch():
        _seed_batch_and_items(store, GID, created=NOW, items=[("旧条目", "a.com/x", 4.0)])
        batches = feeds.news_view(GID)
    assert len(batches) == 1
    assert batches[0]["stats"] is None


def test_stats_of_one_batch_do_not_leak_into_another(tmp_path) -> None:
    """两轮各记各的：按 batch id 存，互不串。"""

    def _models() -> FakeModelsQueue:
        # 每轮主模型四问：定关注点、挑（占位）、打分、写帖子（帖子给个解析不了的，走回落）
        return ensure_pick_fallback(
            FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, "写帖子的回复坏了"])
        )

    solo = {"items": [{
        "title": "另一条完全不同的消息", "url": "https://other.example/only-one",
        "summary": "另一件事，和上一轮没关系。可以看看。", "kind": "news",
        "published": NOW - 3600, "fetched": True, "quote": "原文里写着这件事", "paywall": False,
    }]}
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path, models=_models())

    orig_two_phase = feeds._collect_two_phase

    def _wire(counting_rows, report):
        feeds._workers = CountingWorkers(store, report, counting_rows)
        # 这一轮「代码撒网」要找的东西也换（假搜索是从 workers 预置供的，换回合得跟着换）
        from fakes import FakePlannedSearch

        if isinstance(feeds._search, FakePlannedSearch):
            data = getattr(report, "data", None) or {}
            feeds._search._items_callable = None  # 不再懒取旧 workers 的 report
            feeds._search.items_by_focus = {1: list(data.get("items") or [])}
            feeds._search.planned_searches = {}
            feeds._search.set_planned(False)

        async def _seeded(gid, focus, _settings, *, collect_mark, stats_out):
            feeds._workers._record(collect_mark, "web_search")
            return await orig_two_phase(gid, focus, _settings, collect_mark=collect_mark, stats_out=stats_out)

        feeds._collect_two_phase = _seeded

    _wire(_rows(), _ok_report(_WORKER_ITEMS))
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
        feeds._models = _models()
        _wire([("web_search", True, "q"), ("fetch_page", True, "u")], _ok_report(solo))
        assert _run(feeds.prepare_news(GID)) == 1
        batches = feeds.news_view(GID)

    assert len(batches) == 2
    by_id = {b["id"]: b["stats"] for b in batches}
    assert [by_id[1][k] for k in ("searches", "pages", "kept")] == [2, 3, 2]
    assert [by_id[2][k] for k in ("searches", "pages", "kept")] == [1, 1, 1]
