"""构想别提已经做过 / 正在做的事（线上巡检 2026-10-02：最新一条构想要帮找提丰的图，
10-01 已有任务做过这件事）。出构想时把本群最近 30 天的任务（取消 / 驳回的除外）带进提示词。"""

from __future__ import annotations

from maiwork import clock
from test_feeds import GID, _IDEA_JSON, _make_idea_feeds, _run
from fakes import FakeModelsQueue


def _task(store, tid: str, title: str, status: str, created: float, gid: str = GID) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated) VALUES (?, ?, '', ?, ?, ?, ?)",
            (tid, gid, title, status, created, created),
        )


def test_idea_prompt_lists_recent_tasks(tmp_path) -> None:
    # _make_idea_feeds 不 patch clock（群聊按真实「现在」种），任务时间也必须按同一个时钟算；
    # 以前用固定的 test_feeds.NOW（2026-09-21），真实日期过了 NOW+30 天后「最近 30 天」窗口
    # 就把 T-1 / T-2 也排除了——测试到点自己变红（2026-10-21 发现）。
    now = clock.now()
    models = FakeModelsQueue(ready=True, replies=[_IDEA_JSON])
    store, settings, feeds, models, *_ = _make_idea_feeds(tmp_path, models=models)
    _task(store, "T-1", "找提丰的高清图", "completed", now - 86400)
    _task(store, "T-2", "整理暗潮配装表", "running", now - 3600)
    _task(store, "T-3", "被取消的事", "cancelled", now - 3600)
    _task(store, "T-4", "很久以前的事", "completed", now - 40 * 86400)
    _task(store, "T-5", "别的群的事", "completed", now - 3600, gid="999")
    _run(feeds.make_idea(GID))
    prompt = models.calls[0][1][-1]["content"]
    assert "找提丰的高清图" in prompt and "整理暗潮配装表" in prompt
    for t in ("被取消的事", "很久以前的事", "别的群的事"):
        assert t not in prompt
    assert "做过" in prompt
