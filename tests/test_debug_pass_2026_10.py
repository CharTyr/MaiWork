"""2026-10-26 全项目排查补的几处：补索引、后台任务留引用、登录限流表不只增不减。"""
from __future__ import annotations

import asyncio
import gc

import pytest

from CharTyr_MaiWork.maiwork import tools_admin
from CharTyr_MaiWork.maiwork.console import auth as auth_mod
from CharTyr_MaiWork.maiwork.console.auth import ConsoleAuth
from CharTyr_MaiWork.maiwork.store import Store


def _plan(store: Store, sql: str, params: tuple) -> str:
    rows = store.read().execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(r["detail"]) for r in rows)


def test_hot_queries_use_indexes(tmp_path) -> None:
    store = Store(tmp_path / "mw.db")
    try:
        store.migrate()
        cases = [
            ("SELECT ts FROM events WHERE entity='task' AND entity_id=?", ("T-1",), "idx_events_entity"),
            ("SELECT ts FROM events WHERE kind='x' AND group_id=? AND ts>=?", ("g", 0.0), "idx_events_kind_group"),
            ("SELECT summary FROM attempts WHERE task_id=? ORDER BY id DESC LIMIT 1", ("T-1",), "idx_attempts_task"),
            ("SELECT SUM(prompt_tokens) FROM usage WHERE task_id=?", ("T-1",), "idx_usage_task"),
            ("SELECT id FROM outbox WHERE status='pending' AND not_before<=? ORDER BY id", (0.0,), "idx_outbox_status"),
            ("SELECT id FROM requests WHERE group_id=? AND status='pending'", ("g",), "idx_requests_group_status"),
        ]
        for sql, params, index in cases:
            assert index in _plan(store, sql, params), sql
    finally:
        store.close()


@pytest.mark.asyncio
async def test_admin_background_task_keeps_reference() -> None:
    done = asyncio.Event()

    async def job() -> None:
        await asyncio.sleep(0)
        gc.collect()
        done.set()

    tools_admin._spawn_bg(job())
    assert len(tools_admin._BG_TASKS) >= 1
    await asyncio.wait_for(done.wait(), 1)
    await asyncio.sleep(0)
    assert not any(not t.done() for t in tools_admin._BG_TASKS)


def test_admin_background_task_without_loop_closes_coroutine() -> None:
    async def job() -> None:
        return None

    coro = job()
    with pytest.raises(RuntimeError):
        tools_admin._spawn_bg(coro)
    assert coro.cr_frame is None  # 已关掉，不会再报 never awaited


def test_login_limiter_does_not_keep_empty_entries(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "mw.db")
    try:
        store.migrate()
        auth = ConsoleAuth(store, lambda: None)
        for i in range(50):
            assert auth.login_blocked(f"10.0.0.{i}") is False
        assert auth._fails == {}

        now = [1000.0]
        monkeypatch.setattr(auth_mod.clock, "now", lambda: now[0])
        monkeypatch.setattr(auth_mod, "_FAILS_PRUNE_AT", 3)
        for i in range(3):
            auth.record_login_fail(f"10.0.1.{i}")
        now[0] += auth_mod._WINDOW + 1
        auth.record_login_fail("10.0.2.1")
        auth.record_login_fail("10.0.2.2")
        assert set(auth._fails) == {"10.0.2.1", "10.0.2.2"}

        for _ in range(auth_mod._MAX_TRIES):
            auth.record_login_fail("10.0.3.3")
        assert auth.login_blocked("10.0.3.3") is True
    finally:
        store.close()


def test_login_limiter_has_hard_cap_and_keeps_active_failures(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "mw.db")
    try:
        store.migrate()
        auth = ConsoleAuth(store, lambda: None)
        now = [1000.0]
        monkeypatch.setattr(auth_mod.clock, "now", lambda: now[0])
        monkeypatch.setattr(auth_mod, "_FAILS_PRUNE_AT", 2)
        monkeypatch.setattr(auth_mod, "_FAILS_MAX", 3)
        # 一个 IP 的失败里只有最后一条过期（时钟往回拨过），窗口内那条还在 → 不能整条摘掉
        auth._fails["old"] = [now[0] + 100, now[0] - auth_mod._WINDOW - 1]
        for i in range(5):
            now[0] += 1
            auth.record_login_fail(f"10.0.4.{i}")
        assert len(auth._fails) <= 3
        assert "10.0.4.4" in auth._fails  # 刚失败的不会被淘汰
        auth._fails.clear()
        auth._fails["skew"] = [now[0] + 10, now[0] - auth_mod._WINDOW - 1]
        auth._fails["x"] = [now[0]]
        auth.record_login_fail("y")
        assert auth._fails["skew"] == [now[0] + 10]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_store_read_from_worker_thread_is_separate_and_read_only(tmp_path) -> None:
    import sqlite3

    store = Store(tmp_path / "mw.db")
    try:
        store.migrate()
        with store.tx() as conn:
            store.kv_set(conn, "k", {"v": 1})

        def work():
            conn = store.read()
            assert conn is not store._conn
            assert store.read() is conn  # 同一线程复用
            got = store.kv_get("k")
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM kv")
            return got

        assert await asyncio.to_thread(work) == {"v": 1}
        assert store.read() is store._conn
        assert store.kv_get("k") == {"v": 1}
    finally:
        store.close()
    assert store._side_conns == []
