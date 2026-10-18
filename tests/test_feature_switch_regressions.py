"""Switch persistence and scheduler regressions; no host/network calls."""
from types import SimpleNamespace
import pytest
from test_feature_gates import _make_store, _mk_task, _mk_tasks, GID
from CharTyr_MaiWork.maiwork import group_features
from CharTyr_MaiWork.maiwork.app import MaiWorkApp


def bare_app(store, settings):
    app = MaiWorkApp.__new__(MaiWorkApp)
    app.store, app._settings = store, settings
    app.tasks = _mk_tasks(store, settings)
    app.outbox = None
    app._models_ready = lambda: True
    app._invalidate_answer_cache = lambda: None
    return app


def test_fast_toggle_holds_backlog_until_manual_resume(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    tid = _mk_task(store, settings)
    app = bare_app(store, settings)
    starts = []
    app.spawn_run_task = starts.append
    group_features.set_config(store, GID, {"task": False}, settings, now=1000)
    group_features.set_config(store, GID, {"task": True}, settings, now=1001)
    app._tasks_round(1002)
    assert starts == []
    assert app.tasks.get(tid)["paused_reason"]
    app.tasks.resume_feature_off(tid, by="管理员")
    app._tasks_round(1003)
    assert starts == [tid]


def test_resume_refused_while_disabled(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    tid = _mk_task(store, settings)
    group_features.set_config(store, GID, {"task": False}, settings)
    tasks = _mk_tasks(store, settings)
    before = tasks.get(tid)
    assert '"feature_off"' in before["paused_reason"]
    with pytest.raises(ValueError, match="关闭"):
        tasks.resume_feature_off(tid)
    assert tasks.get(tid) == before


def test_reenable_restarts_waiting_clock(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    tid = _mk_task(store, settings, status="waiting_input")
    with store.tx() as conn:
        conn.execute("UPDATE tasks SET question_ts=1000 WHERE id=?", (tid,))
    group_features.set_config(store, GID, {"task": False}, settings, now=2000)
    group_features.set_config(store, GID, {"task": True}, settings, now=92000)
    bare_app(store, settings)._tasks_round(92001)
    assert _mk_tasks(store, settings).get(tid)["status"] == "waiting_input"


def test_shutdown_history_survives_reenable(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    group_features.set_config(store, GID, {"idea": False}, settings, now=1000)
    group_features.set_config(store, GID, {"idea": True}, settings, now=1001)
    cfg = group_features.get(store, GID, settings)
    assert cfg["last_disabled_ts"]["idea"] == 1000
    assert cfg["enabled_since"]["idea"] == 1001


@pytest.mark.parametrize("raw", ["{broken", "null", "[]", '{"task":"false","news":true,"idea":false}'])
def test_corrupt_existing_config_fails_closed(tmp_path, raw):
    store, settings, _ = _make_store(tmp_path)
    with store.tx() as conn:
        conn.execute("INSERT INTO kv(key,value,updated) VALUES(?,?,0)", (f"group_features.{GID}", raw))
    cfg = group_features.get(store, GID, settings)
    assert cfg["task"] is False
    assert cfg["news"] is (raw.startswith('{"task"'))


def test_task_creation_refused_while_disabled(tmp_path):
    store, settings, _ = _make_store(tmp_path)
    group_features.set_config(store, GID, {"task": False}, settings)
    with pytest.raises(ValueError, match="关闭"):
        _mk_tasks(store, settings).create(GID, title="test", req="test", criteria=[], source="test")


def test_read_error_fails_closed(tmp_path):
    _, settings, _ = _make_store(tmp_path)
    def broken(*args):
        raise RuntimeError("unavailable")
    store = SimpleNamespace(kv_get=broken, read=broken)
    assert group_features.enabled(store, GID, "task", settings) is False
