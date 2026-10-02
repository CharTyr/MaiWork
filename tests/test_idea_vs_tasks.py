"""构想别提已经做过 / 正在做的事（线上巡检 2026-10-02：最新一条构想要帮找提丰的图，
10-01 已有任务做过这件事）。出构想时把本群最近 30 天的任务（取消 / 驳回的除外）带进提示词。"""

from __future__ import annotations

from test_feeds import GID, NOW, _IDEA_JSON, _make_feeds, _run
from fakes import FakeModelsQueue


def _task(store, tid: str, title: str, status: str, created: float, gid: str = GID) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated) VALUES (?, ?, '', ?, ?, ?, ?)",
            (tid, gid, title, status, created, created),
        )


def test_idea_prompt_lists_recent_tasks(tmp_path) -> None:
    models = FakeModelsQueue(ready=True, replies=[_IDEA_JSON])
    store, settings, feeds, models, *_ = _make_feeds(tmp_path, models=models)
    _task(store, "T-1", "找提丰的高清图", "completed", NOW - 86400)
    _task(store, "T-2", "整理暗潮配装表", "running", NOW - 3600)
    _task(store, "T-3", "被取消的事", "cancelled", NOW - 3600)
    _task(store, "T-4", "很久以前的事", "completed", NOW - 40 * 86400)
    _task(store, "T-5", "别的群的事", "completed", NOW - 3600, gid="999")
    _run(feeds.make_idea(GID))
    prompt = models.calls[0][1][-1]["content"]
    assert "找提丰的高清图" in prompt and "整理暗潮配装表" in prompt
    for t in ("被取消的事", "很久以前的事", "别的群的事"):
        assert t not in prompt
    assert "做过" in prompt
