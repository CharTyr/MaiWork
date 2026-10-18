"""功能开关在 app / tasks / approvals 各层的闸（.trellis/tasks/10-18-independent-feature-switches）。

只测「闸真的挡住了」这一层契约，不重复测各模块内部：

1. tasks.py 的 paused_reason 接受 `feature_off`（功能关闭搁置），视图能给出一句中文。
2. app 的 `_feature_on` / `_feature_disabled_since` 委托 group_features。
3. `_schedule_round`：关闭 news / idea 后到点不开长活、不记 scheduler.done。
4. `_tasks_round`：任务关闭时 queued 不派工、waiting_input 不提醒不搁置；
   带 `feature_off` 暂停原因的任务即使开着也不自动派工（要管理员手动恢复）。
5. `_goals_round`：任务关闭时目标检查 / 提醒 / 失联巡视整轮不动工。
6. `run_news_now` / `make_idea_now`：对应模块关闭 → started=False + 中文原因。
7. approvals.approve：任务关闭时整笔拒绝、请求保持 pending、零落库。
8. waiting_input 的等待计时从重开时刻重算（disabled_ts 之后才算等待）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import group_features
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks, paused_reason_text

pytestmark = pytest.mark.asyncio

GID = "900000001"
OTHER = "555000"


def _make_store(tmp_path: Path, *, groups=(GID,)):
    root = tmp_path / "gate"
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "t.db")
    store.migrate()
    merged = {
        "environments": {"workspace_root": str(root)},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in groups]},
    }
    settings, _ = load_settings(merged)
    with store.tx() as conn:
        for g in set(groups) | {OTHER}:
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
    return store, settings, root


def _mk_tasks(store: Store, settings) -> Tasks:
    return Tasks(store, lambda: settings)


def _mk_task(store: Store, settings, gid: str = GID, **kw: Any) -> str:
    tasks = _mk_tasks(store, settings)
    return tasks.create(
        gid,
        title=kw.pop("title", "测试任务"),
        req=kw.pop("req", "做点事"),
        criteria=[],
        source="request",
        requester_id="31415926",
        requester_name="群友",
        request_id="",
        icon="package",
        status=kw.pop("status", "queued"),
        **kw,
    )


# ---------------------------------------------------------------------------
# tasks.py 的 feature_off 暂停原因
# ---------------------------------------------------------------------------


def test_feature_off_paused_reason_accepted_and_rendered(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    tid = _mk_task(store, settings)
    tasks = _mk_tasks(store, settings)
    tasks.transition(tid, "paused", reason="功能关闭", paused_reason={"kind": "feature_off"})
    row = tasks.get(tid)
    assert row is not None and row["status"] == "paused"
    text = paused_reason_text({"kind": "feature_off"})
    assert "关闭" in text and "手动恢复" in text


def test_resume_feature_off_clears_mark_and_refuses_others(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    tasks = _mk_tasks(store, settings)
    tid = _mk_task(store, settings)
    # 模拟 _mark_feature_off：queued + feature_off
    with store.tx() as conn:
        conn.execute(
            "UPDATE tasks SET paused_reason=? WHERE id=? AND status='queued'",
            (json.dumps({"kind": "feature_off"}), tid),
        )
    # 普通 queued（无标记）不能用这条恢复
    plain = _mk_task(store, settings, title="普通排队")
    with pytest.raises(ValueError):
        tasks.resume_feature_off(plain, by="管理员")
    # 恢复后标记清掉，状态仍 queued（后台循环捞起）
    tasks.resume_feature_off(tid, by="管理员")
    row = tasks.get(tid)
    assert row is not None and row["status"] == "queued"
    assert (row["paused_reason"] or "") == ""
    # 事件记下是谁恢复的
    ev = store.read().execute(
        "SELECT payload FROM events WHERE kind='task.resumed' AND entity_id=?", (tid,)
    ).fetchone()
    assert ev is not None and "feature_off" in (ev["payload"] or "")


# ---------------------------------------------------------------------------
# app 的开关委托
# ---------------------------------------------------------------------------


class _BareApp:
    """不 start 的 MaiWorkApp：只挂 store/settings，测闸的判定本身。"""

    def __init__(self, store, settings):
        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        self._app = MaiWorkApp.__new__(MaiWorkApp)
        self._app.store = store
        self._app._settings = settings

    def on(self, gid, module):
        return self._app._feature_on(gid, module)

    def since(self, gid, module):
        return self._app._feature_disabled_since(gid, module)

    def deny(self, module):
        return self._app._feature_deny_reason(module)


def test_app_feature_delegates_to_group_features(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    app = _BareApp(store, settings)
    assert app.on(GID, "news") is True
    group_features.set_config(store, GID, {"news": False}, settings, by="管理员", now=1000.0)
    assert app.on(GID, "news") is False
    assert app.on(GID, "idea") is True
    assert app.since(GID, "news") == 1000.0
    assert "资讯" in app.deny("news")
    # 别的群不受影响
    assert app.on(OTHER, "news") is True
