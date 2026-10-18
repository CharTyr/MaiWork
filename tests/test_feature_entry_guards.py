"""Final entry guards, using local SQLite and fake workers only."""
from types import SimpleNamespace
import json
import pytest
from CharTyr_MaiWork.maiwork import group_features
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

GID = "900000001"
OTHER = "555000"


def make(tmp_path):
    store = Store(tmp_path / "guards.db")
    store.migrate()
    settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{g}"} for g in (GID, OTHER)]}})
    with store.tx() as conn:
        for gid in (GID, OTHER):
            conn.execute("INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)",
                         (gid, f"s-{gid}", "测试群", f"token-{gid}"))
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    approvals = Approvals(store, lambda: settings, tasks, goals)
    return store, settings, tasks, approvals


def request(approvals, gid=GID, **kw):
    return approvals.create(gid, title="测试活", quote="请准备资料", via="测试", requester_id="123",
                            requester_name="群友", **kw)


def queued(tasks, gid=GID):
    return tasks.create(gid, title="测试活", req="准备资料", criteria=[], source="request",
                        requester_id="123", requester_name="群友", request_id="", icon="package", status="queued")


@pytest.mark.parametrize("kind", ["task", "goal"])
@pytest.mark.parametrize("auto", [False, True])
def test_disabled_create_leaves_no_request_or_work(tmp_path, kind, auto):
    store, settings, tasks, approvals = make(tmp_path)
    approvals._is_auto = lambda *_: auto
    group_features.set_config(store, GID, {"task": False}, settings)
    with pytest.raises(ValueError, match="关闭"):
        request(approvals, kind=kind)
    for table in ("requests", "tasks", "goals"):
        assert store.read().execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert request(approvals, OTHER, kind=kind)["id"]


def test_land_itself_rejects_disabled_and_keeps_pending(tmp_path):
    store, settings, tasks, approvals = make(tmp_path)
    approvals._is_auto = lambda *_: False
    rid = request(approvals, kind="goal")["id"]
    group_features.set_config(store, GID, {"task": False}, settings)
    with pytest.raises(ValueError, match="关闭"):
        with store.tx() as conn:
            approvals._land(conn, approvals._request_row(conn, rid), by="管理员", auto=True)
    assert store.read().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()[0] == "pending"
    assert store.read().execute("SELECT count(*) FROM goals").fetchone()[0] == 0


def coordinator(store, settings, tasks, reached, lock=None):
    obj = Coordinator.__new__(Coordinator)
    obj._store, obj._tasks = store, tasks
    obj._get_settings = lambda: settings
    obj._models = SimpleNamespace(settings=lambda: SimpleNamespace(ready=lambda: True))
    obj._locks = {}
    if lock is not None:
        obj._lock_for = lambda _: lock
    async def attempt(task):
        reached.append(task["id"])
        return "done"
    obj._run_one_attempt = attempt
    return obj


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", [False, True])
async def test_coordinator_never_starts_disabled_or_held_queue(tmp_path, hold):
    store, settings, tasks, _ = make(tmp_path)
    tid = queued(tasks)
    group_features.set_config(store, GID, {"task": False}, settings)
    if hold:
        group_features.set_config(store, GID, {"task": True}, settings)
        with store.tx() as conn:
            conn.execute("UPDATE tasks SET paused_reason=? WHERE id=?", (json.dumps({"kind": "feature_off"}), tid))
    reached = []
    await coordinator(store, settings, tasks, reached).run_task(tid)
    assert reached == []
    assert tasks.get(tid)["status"] == "queued"


@pytest.mark.asyncio
async def test_coordinator_rechecks_after_workspace_wait(tmp_path):
    store, settings, tasks, _ = make(tmp_path)
    tid = queued(tasks)
    class TurnOffWhileWaiting:
        async def __aenter__(self):
            group_features.set_config(store, GID, {"task": False}, settings)
        async def __aexit__(self, *args):
            pass
    reached = []
    await coordinator(store, settings, tasks, reached, TurnOffWhileWaiting()).run_task(tid)
    assert reached == []


@pytest.mark.asyncio
async def test_running_lifecycle_can_retry_and_complete_after_disable(tmp_path):
    store, settings, tasks, _ = make(tmp_path)
    tid = queued(tasks)
    reached = []
    obj = coordinator(store, settings, tasks, reached)
    async def attempt(task):
        reached.append(task["id"])
        if len(reached) == 1:
            group_features.set_config(store, GID, {"task": False}, settings)
            return "retry"
        return "done"
    obj._run_one_attempt = attempt
    await obj.run_task(tid)
    assert reached == [tid, tid]


def intake(store, settings, *, jev=None):
    created, mentions, spawned = [], [], []
    def create(gid, **kw):
        created.append((gid, kw))
        return {"id": "R-1", "status": "approved"}
    obj = Intake(lambda: settings, Signals(), jev=jev,
                 approvals=SimpleNamespace(create=create), mentions=SimpleNamespace(add=lambda *a, **kw: mentions.append(a)),
                 spawn=spawned.append, feature_on=lambda gid, mod: group_features.enabled(store, gid, mod, settings))
    obj._store = store
    return obj, created, mentions, spawned


@pytest.mark.asyncio
async def test_delayed_request_rechecks_switch_at_callback_entry(tmp_path):
    store, settings, _, _ = make(tmp_path)
    obj, created, mentions, _ = intake(store, settings)
    group_features.set_config(store, GID, {"task": False}, settings)
    await obj._create_request(GID, "prepare", 1, "123", "群友", "m-1", "请准备资料")
    assert created == [] and mentions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reenable", [False, True])
async def test_jev_result_is_invalid_after_off_even_if_reenabled(tmp_path, reenable):
    store, settings, _, _ = make(tmp_path)
    class Jev:
        def available(self):
            return True
        async def ask(self, *args, **kwargs):
            group_features.set_config(store, GID, {"task": False}, settings)
            if reenable:
                group_features.set_config(store, GID, {"task": True}, settings)
            return {"kind": ("prepare", "", 1.0)}
    obj, created, mentions, spawned = intake(store, settings, jev=Jev())
    obj._decide = lambda *_: True
    await obj._judge_at(settings, GID, "123", "群友", "m-1", "请准备资料")
    for coro in spawned:
        await coro
    assert created == [] and mentions == []


@pytest.mark.asyncio
async def test_quick_judge_result_keeps_original_cutoff(tmp_path):
    store, settings, _, _ = make(tmp_path)
    obj, created, mentions, _ = intake(store, settings)
    group_features.set_config(store, GID, {"task": False}, settings, now=200)
    group_features.set_config(store, GID, {"task": True}, settings, now=201)
    await obj._qj_request(GID, "prepare", 1, {"message_id": "m-1", "text": "请准备资料", "_feature_started_ts": 100}, "测试活")
    assert created == [] and mentions == []


def test_create_rechecks_switch_after_exemption_lookup(tmp_path):
    store, settings, _, approvals = make(tmp_path)
    def exempt(*_):
        group_features.set_config(store, GID, {"task": False}, settings)
        return True
    approvals._is_auto = exempt
    with pytest.raises(ValueError, match="关闭"):
        request(approvals, kind="goal")
    assert store.read().execute("SELECT count(*) FROM requests").fetchone()[0] == 0


def test_create_rejects_old_judgment_after_reenable(tmp_path):
    store, settings, _, approvals = make(tmp_path)
    group_features.set_config(store, GID, {"task": False}, settings, now=200)
    group_features.set_config(store, GID, {"task": True}, settings, now=201)
    with pytest.raises(ValueError, match="手动"):
        request(approvals, kind="goal", started_at=100)
    assert store.read().execute("SELECT count(*) FROM requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_direct_goal_check_does_no_work_when_task_disabled(tmp_path):
    store, settings, tasks, _ = make(tmp_path)
    group_features.set_config(store, GID, {"task": False}, settings)
    obj = coordinator(store, settings, tasks, [])
    obj._goals = SimpleNamespace(get=lambda _: {"group_id": GID, "kind": "agent", "state": "active"})
    def read_chat(*args, **kwargs):
        pytest.fail("disabled goal check read group chat")
    obj._goal_chat_lines = read_chat
    await obj.check_goal("G-1")


def test_admitted_goal_check_can_finish_after_disable(tmp_path):
    store, settings, tasks, _ = make(tmp_path)
    obj = coordinator(store, settings, tasks, [])
    obj._goals = SimpleNamespace(get=lambda _: {"group_id": GID, "kind": "agent", "state": "active"})
    assert obj._goal_is_active("G-1", GID)
    group_features.set_config(store, GID, {"task": False}, settings)
    assert obj._goal_is_active("G-1", GID)
