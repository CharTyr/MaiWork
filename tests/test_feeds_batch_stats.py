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

from fakes import FakeModelsQueue

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
    的落库），再交回预置报告——用来验证 feeds 真的把每轮的工具用量算成统计。"""

    def __init__(self, store, report: Any, rows: list[tuple[str, bool, str]]) -> None:
        self._store = store
        self.report = report
        self.rows = list(rows)
        self.calls: List[Dict[str, Any]] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        mark = str(kwargs.get("task_id") or "")
        with self._store.tx() as conn:
            for tool, ok, inp in self.rows:
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                    " VALUES (?, ?, ?, '子 agent #1', ?, ?, 'out', 3, ?, '')",
                    (NOW, GID, mark, tool, inp, 1 if ok else 0),
                )
        if isinstance(self.report, BaseException):
            raise self.report
        return self.report


def _rows() -> list[tuple[str, bool, str]]:
    return [
        ("web_search", True, "搜 1"),
        ("web_search", False, "搜 2（失败也算搜了一次）"),
        ("fetch_page", True, "https://a.example/1"),
        ("fetch_page", True, "https://a.example/2"),
        ("fetch_page", False, "https://a.example/3"),  # 没打开的页不算「看过」
    ]


def test_batch_stats_recorded_and_exposed(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON])
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path, models=models)
    counting = CountingWorkers(store, _ok_report(_WORKER_ITEMS), _rows())
    feeds._workers = counting  # 换成会落 tool_calls 的假 workers（真 workers 也这么落）

    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
        assert got == 2
        batches = feeds.news_view(GID)

    # 子 agent 这轮拿到了带标记的 task_id（统计就按它算）
    assert counting.calls and str(counting.calls[0].get("task_id") or "")
    assert len(batches) == 1
    stats = batches[0]["stats"]
    assert stats == {"searches": 2, "pages": 2, "kept": 2}

    # 批次记录上真的落了 kv（按 batch id，不动 store.py 的表结构）
    saved = store.kv_get(f"feeds.batch_stats.{batches[0]['id']}")
    assert saved == {"searches": 2, "pages": 2, "kept": 2}


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
    """子 agent 收集成功、打分失败 → 这轮记 skipped，但已经搜过/看过的次数保留。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, ModelError("打分端点挂了")])
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path, models=models)
    feeds._workers = CountingWorkers(store, _ok_report(_WORKER_ITEMS), _rows()[:3])

    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
        batches = feeds.news_view(GID)
    assert len(batches) == 1
    assert batches[0]["skipped"] is True
    assert batches[0]["stats"] == {"searches": 2, "pages": 1, "kept": 0}


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
        # 每轮主模型三问：定关注点、打分、写帖子（帖子给个解析不了的，走回落）
        return FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, _SCORES_JSON, "写帖子的回复坏了"])

    solo = {"items": [{
        "title": "另一条完全不同的消息", "url": "https://other.example/only-one",
        "summary": "另一件事，和上一轮没关系。可以看看。", "kind": "news",
        "published": NOW - 3600, "fetched": True, "quote": "原文里写着这件事", "paywall": False,
    }]}
    store, settings, feeds, models, workers, topics, _profiles = _make_feeds(tmp_path, models=_models())
    feeds._workers = CountingWorkers(store, _ok_report(_WORKER_ITEMS), _rows())

    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 2
        feeds._models = _models()
        feeds._workers = CountingWorkers(
            store, _ok_report(solo), [("web_search", True, "q"), ("fetch_page", True, "u")]
        )
        assert _run(feeds.prepare_news(GID)) == 1
        batches = feeds.news_view(GID)

    assert len(batches) == 2
    by_id = {b["id"]: b["stats"] for b in batches}
    assert by_id[1] == {"searches": 2, "pages": 2, "kept": 2}
    assert by_id[2] == {"searches": 1, "pages": 1, "kept": 1}
