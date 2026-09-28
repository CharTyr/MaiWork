"""goals.py / coordinator.py / app.py 的「报平安」（docs/02 §4.3）。

- check_goal 每次成功跑完（不管有没有新进展）写 goals.heartbeat_ts、清 stale_reason；
  出错用 kv（goal.fail.<id>）计数，连续 3 次 → stale_reason「连续 3 次检查出错：…」。
- 后台目标轮巡视：now - max(heartbeat_ts, created) 超过 2 倍检查间隔仍没心跳 →
  stale_reason「超过预定检查时间还没动静」；模型没配好 →「模型没配好，暂停检查」。
- goals.view 的 agent 项固定多出 stale / stale_reason / heartbeat_ts 三个字段；
  app.stale_goals_count(group_id) 数「卡住」的（「模型没配好」不算卡住）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.coordinator import Coordinator
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.models import ModelError
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks
from CharTyr_MaiWork.tools import Tools

pytestmark = pytest.mark.asyncio

NOW = 1_790_000_000.0
GID = "900000001"


# ---------------------------------------------------------------------------
# 假对象 / fixture
# ---------------------------------------------------------------------------


class FakeModels:
    """假 Models：按队列回放 chat 文本；ready 可开关。"""

    def __init__(self, replies=(), ready=True):
        self.replies = list(replies)
        self._ready = ready

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self):
                return ready

        return _S()

    async def chat(self, role, messages, **kwargs):
        if not self.replies:
            return SimpleNamespace(text="{}", tool_calls=[])
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(text=str(item), tool_calls=[])


class FakeOutbox:
    def __init__(self):
        self.enqueued: list[dict] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        self.enqueued.append({"key": key, "payload": dict(payload or {})})
        return len(self.enqueued)


class FakeWorkers:
    async def run(self, brief, **kw):  # pragma: no cover - 本文件不走执行
        raise AssertionError("不该跑到子 agent")


class _Profiles:
    def entries(self, group_id):
        return []


class _Settings:
    def workspace_of(self, group_id: str) -> str:
        return "ws-demo"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path):
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    yield store
    store.close()


@pytest.fixture
def settings():
    return _Settings()


@pytest.fixture
def goals(mem_store: Store, settings) -> Goals:
    return Goals(mem_store, lambda: settings)


def _build_coordinator(mem_store, settings, goals, models) -> Coordinator:
    tasks = Tasks(mem_store, lambda: settings)
    return Coordinator(
        mem_store,
        models,
        FakeWorkers(),
        Tools(mem_store),
        tasks,
        goals,
        SimpleNamespace(),  # delivery
        FakeOutbox(),
        None,  # env：check_goal 用不到
        _Profiles(),
        lambda: settings,
    )


def _mk_goal(goals: Goals) -> str:
    return goals.create_agent(
        GID,
        title="盯着活动日历",
        body="每周更新",
        criteria=["不漏场"],
        by_text="",
    )


def _row(store: Store, goal_id: str) -> dict:
    r = store.read().execute(
        "SELECT heartbeat_ts, stale_reason FROM goals WHERE id=?", (goal_id,)
    ).fetchone()
    return dict(r)


def _ok_reply() -> str:
    return json.dumps(
        {
            "done_criteria": [],
            "next_check_hours": 2,
            "progress": None,
            "new_task": None,
            "report": None,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# check_goal：成功写心跳 / 出错计数
# ---------------------------------------------------------------------------


async def test_success_writes_heartbeat_and_clears_stale(mem_store, settings, goals):
    goal_id = _mk_goal(goals)
    # 预置一点「旧伤」：已经成功过的目标心跳要刷新、stale 要清掉
    with mem_store.tx() as conn:
        conn.execute(
            "UPDATE goals SET stale_reason='连续 3 次检查出错：旧错', updated=? WHERE id=?",
            (NOW - 100, goal_id),
        )
        mem_store.kv_set(conn, f"goal.fail.{goal_id}", 3)
    coordinator = _build_coordinator(mem_store, settings, goals, FakeModels([_ok_reply()]))
    await coordinator.check_goal(goal_id)
    row = _row(mem_store, goal_id)
    assert row["heartbeat_ts"] == NOW
    assert row["stale_reason"] == ""
    assert mem_store.kv_get(f"goal.fail.{goal_id}", 0) == 0


async def test_consecutive_3_errors_mark_stale(mem_store, settings, goals):
    goal_id = _mk_goal(goals)
    err = ModelError("端点 500：连不上")
    coordinator = _build_coordinator(
        mem_store, settings, goals, FakeModels([err, err, err])
    )
    await coordinator.check_goal(goal_id)
    await coordinator.check_goal(goal_id)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == ""  # 2 次还不够
    assert row["heartbeat_ts"] is None
    await coordinator.check_goal(goal_id)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"].startswith("连续 3 次检查出错：")
    assert "端点 500" in row["stale_reason"]
    assert len(row["stale_reason"]) <= len("连续 3 次检查出错：") + 60


async def test_success_resets_error_count(mem_store, settings, goals):
    goal_id = _mk_goal(goals)
    err = ModelError("抖一下")
    coordinator = _build_coordinator(
        mem_store, settings, goals, FakeModels([err, err, _ok_reply(), err])
    )
    await coordinator.check_goal(goal_id)
    await coordinator.check_goal(goal_id)
    await coordinator.check_goal(goal_id)  # 成功，计数清零
    await coordinator.check_goal(goal_id)  # 只错 1 次
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == ""
    assert row["heartbeat_ts"] == NOW


async def test_unconfigured_error_not_counted(mem_store, settings, goals):
    """「模型还没配好」类错误不算出错（等配置，不算卡住）。"""
    goal_id = _mk_goal(goals)
    err = ModelError("模型还没配好，先去网页填密钥")
    coordinator = _build_coordinator(
        mem_store, settings, goals, FakeModels([err, err, err])
    )
    for _ in range(3):
        await coordinator.check_goal(goal_id)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == ""
    assert mem_store.kv_get(f"goal.fail.{goal_id}", 0) == 0


async def test_bad_json_counts_as_error(mem_store, settings, goals):
    goal_id = _mk_goal(goals)
    coordinator = _build_coordinator(
        mem_store, settings, goals, FakeModels(["这不是 JSON"])
    )
    await coordinator.check_goal(goal_id)
    row = _row(mem_store, goal_id)
    assert row["heartbeat_ts"] is None
    assert mem_store.kv_get(f"goal.fail.{goal_id}", 0) == 1


async def test_models_not_ready_skips_without_fail(mem_store, settings, goals):
    """模型没配好：跳过检查，不计出错、不写心跳。"""
    goal_id = _mk_goal(goals)
    coordinator = _build_coordinator(
        mem_store, settings, goals, FakeModels(ready=False)
    )
    await coordinator.check_goal(goal_id)
    row = _row(mem_store, goal_id)
    assert row["heartbeat_ts"] is None
    assert row["stale_reason"] == ""
    assert mem_store.kv_get(f"goal.fail.{goal_id}", 0) == 0


# ---------------------------------------------------------------------------
# Goals.refresh_stale：后台目标轮的超时巡视
# ---------------------------------------------------------------------------


async def test_timeout_without_heartbeat_marks_stale(mem_store, settings, goals, fixed_clock):
    fixed_clock[0] = NOW - 3 * 3600  # 3 小时前建的目标
    goal_id = _mk_goal(goals)
    fixed_clock[0] = NOW
    goals.refresh_stale(NOW, models_ready=True)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == "超过预定检查时间还没动静"


async def test_recent_heartbeat_not_stale(mem_store, settings, goals, fixed_clock):
    fixed_clock[0] = NOW - 30
    goal_id = _mk_goal(goals)
    goals.beat(goal_id)  # 30 秒前刚报过平安
    fixed_clock[0] = NOW
    # 预置一条旧 reason，巡视应该清掉
    with mem_store.tx() as conn:
        conn.execute("UPDATE goals SET stale_reason='超过预定检查时间还没动静' WHERE id=?", (goal_id,))
    goals.refresh_stale(NOW, models_ready=True)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == ""


async def test_models_not_ready_reason(mem_store, settings, goals, fixed_clock):
    fixed_clock[0] = NOW - 3 * 3600
    goal_id = _mk_goal(goals)
    fixed_clock[0] = NOW
    goals.refresh_stale(NOW, models_ready=False)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == "模型没配好，暂停检查"


async def test_error_reason_survives_sweep(mem_store, settings, goals, fixed_clock):
    """「连续出错」的原因不被超时巡视冲掉。"""
    fixed_clock[0] = NOW - 3 * 3600
    goal_id = _mk_goal(goals)
    fixed_clock[0] = NOW
    with mem_store.tx() as conn:
        conn.execute(
            "UPDATE goals SET stale_reason='连续 3 次检查出错：端点 500' WHERE id=?", (goal_id,)
        )
    goals.refresh_stale(NOW, models_ready=True)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"].startswith("连续 3 次检查出错")


async def test_window_follows_next_check_interval(mem_store, settings, goals, fixed_clock):
    """间隔按 next_check_ts 算：约好 12 小时一检的，3 小时没心跳不算卡。"""
    fixed_clock[0] = NOW - 3 * 3600
    goal_id = _mk_goal(goals)
    fixed_clock[0] = NOW
    # 下次检查排在创建后 12 小时 → 窗口 = 2 × 12h = 24h，3 小时远没到
    with mem_store.tx() as conn:
        conn.execute("UPDATE goals SET next_check_ts=? WHERE id=?", (NOW - 3 * 3600 + 12 * 3600, goal_id))
    goals.refresh_stale(NOW, models_ready=True)
    row = _row(mem_store, goal_id)
    assert row["stale_reason"] == ""


# ---------------------------------------------------------------------------
# view 字段（前端要用，字段名固定）
# ---------------------------------------------------------------------------


async def test_view_agent_fields(mem_store, settings, goals):
    goal_id = _mk_goal(goals)
    goals.beat(goal_id)
    view = goals.view(GID)
    assert len(view["agent"]) == 1
    item = view["agent"][0]
    assert item["stale"] is False
    assert item["stale_reason"] == ""
    assert item["heartbeat_ts"] == NOW


async def test_view_stale_true_for_timeout_false_for_unconfigured(mem_store, settings, goals, fixed_clock):
    fixed_clock[0] = NOW - 3 * 3600
    goal_id = _mk_goal(goals)
    fixed_clock[0] = NOW
    goals.refresh_stale(NOW, models_ready=True)
    item = goals.view(GID)["agent"][0]
    assert item["stale"] is True
    assert item["stale_reason"] == "超过预定检查时间还没动静"
    assert item["heartbeat_ts"] is None
    # 模型没配好：写出来给人看，但不算「卡住」
    goals.refresh_stale(NOW, models_ready=False)
    item = goals.view(GID)["agent"][0]
    assert item["stale"] is False
    assert item["stale_reason"] == "模型没配好，暂停检查"


async def test_member_view_has_no_stale_fields(mem_store, settings, goals):
    goals.create_member(
        GID, who_id="10001", who_name="阿柒", title="交周报",
        due_ts=NOW + 86400, remind_ts=NOW + 3600,
    )
    item = goals.view(GID)["member"][0]
    assert "stale" not in item
    assert "heartbeat_ts" not in item


# ---------------------------------------------------------------------------
# app 接线：目标轮巡视 + stale_goals_count
# ---------------------------------------------------------------------------


def _app_raw(data_dir: Path, *, listen: str = "127.0.0.1:18711") -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "console": {"listen": listen, "password": "pw-测试"},
        "storage": {"data_dir": str(data_dir)},
    }


async def test_app_goals_round_marks_unconfigured_and_count_stays_zero(tmp_path):
    from fakes import FakeCtx, FakeProfiles

    from CharTyr_MaiWork.app import MaiWorkApp

    app = MaiWorkApp(FakeCtx({}), _app_raw(tmp_path / "data"), plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    try:
        fixed_at = clock.now() - 3 * 3600
        goal_id = app.goals.create_agent(
            GID, title="盯梢", body="", criteria=["c"], by_text=""
        )
        with app.store.tx() as conn:
            conn.execute(
                "UPDATE goals SET created=?, updated=?, next_check_ts=? WHERE id=?",
                (fixed_at, fixed_at, fixed_at + 3600, goal_id),
            )
        await app._goals_round(clock.now())
        row = app.store.read().execute(
            "SELECT stale_reason FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        assert row["stale_reason"] == "模型没配好，暂停检查"
        assert app.stale_goals_count(GID) == 0  # 「模型没配好」不算卡住
    finally:
        await app.stop()


async def test_stale_goals_count_counts_timeout_stale(tmp_path):
    from fakes import FakeCtx, FakeProfiles

    from CharTyr_MaiWork.app import MaiWorkApp

    app = MaiWorkApp(FakeCtx({}), _app_raw(tmp_path / "data2"), plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    try:
        now = clock.now()
        goal_id = app.goals.create_agent(GID, title="盯梢", body="", criteria=["c"], by_text="")
        with app.store.tx() as conn:
            conn.execute(
                "UPDATE goals SET created=?, updated=?, next_check_ts=? WHERE id=?",
                (now - 3 * 3600, now - 3 * 3600, now - 3 * 3600 + 3600, goal_id),
            )
        assert app.stale_goals_count(GID) == 0
        app.goals.refresh_stale(now, models_ready=True)
        assert app.stale_goals_count(GID) == 1
        # 心跳来了、巡视清掉 → 归零
        app.goals.beat(goal_id)
        app.goals.refresh_stale(now, models_ready=True)
        assert app.stale_goals_count(GID) == 0
    finally:
        await app.stop()


async def test_stale_goals_count_not_started(tmp_path):
    from fakes import FakeCtx

    from CharTyr_MaiWork.app import MaiWorkApp

    app = MaiWorkApp(FakeCtx({}), _app_raw(tmp_path / "data3"), plugin_dir=Path(__file__).resolve().parents[1])
    assert app.stale_goals_count(GID) == 0  # 没启动不炸，返回 0
