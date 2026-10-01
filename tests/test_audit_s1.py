"""S1 回归测试：非服务群残留数据零派工、零调模型、零发送。

审计发现（以当前代码为准）：
- app.py 的 _tasks_round / _goals_round / _approval_round 没按服务群过滤——配置删群后，
  库里残留的 queued 任务照样被 spawn 派工、active 目标的到期提醒照样入队、
  pending 待批照样 24 小时提醒；
- outbox.flush 也没按服务群过滤——pending 发件箱照样发。
- 配置热更新/启动时，对不再服务的群要就地标记（不删数据）：
  pending 发件箱 → cancelled（error 写「这个群已不在服务列表」）、
  queued/waiting_input 任务 → cancelled（同上 reason）、
  active 目标 → cancelled、pending 待批 → expired。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=BJ).timestamp()  # 非睡觉时段的固定时刻

G1 = "900000001"  # 服务群
G2 = "123456789"  # 曾是服务群、配置里被删掉
NOT_SERVED_REASON = "这个群已不在服务列表"


def _raw(data_dir: Path, groups: list[str] | None = None) -> dict:
    serve = [{"group": f"qq:{g}"} for g in (groups if groups is not None else [G1, G2])]
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": serve},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": "pw-测试"},
        "storage": {"data_dir": str(data_dir)},
        "models": {"base_url": "http://127.0.0.1:9/v1", "api_key": "test-key", "main": "m1", "worker": "w1"},
    }


def _app(tmp_path: Path, raw: dict) -> MaiWorkApp:
    app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    return app


def _seed_residual(app: MaiWorkApp) -> None:
    """往库里塞 G2（非服务群）的残留：queued 任务、waiting_input 任务、active 目标、
    pending 待批、pending 发件箱；以及 G1 的对照组（ должны不受影响）。"""
    now = clock.now()
    with app.store.tx() as conn:
        # G2 残留
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
            " VALUES ('T-gone-q', ?, 'ws', '残留排队任务', 'queued', ?, ?)",
            (G2, now, now),
        )
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, question, question_ts, created, updated)"
            " VALUES ('T-gone-w', ?, 'ws', '残留等回答任务', 'waiting_input', '要回答吗', ?, ?, ?)",
            (G2, now - 7200, now, now),
        )
        conn.execute(
            "INSERT INTO goals (id, group_id, kind, title, state, created, updated)"
            " VALUES ('G-gone', ?, 'agent', '残留目标', 'active', ?, ?)",
            (G2, now, now),
        )
        conn.execute(
            "INSERT INTO requests (id, group_id, kind, title, status, created, updated)"
            " VALUES ('R-gone', ?, 'task', '残留待批', 'pending', ?, ?)",
            (G2, now - 25 * 3600, now),  # 超过 24 小时，足以触发提醒
        )
        conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result, error, not_before, created, updated)"
            " VALUES ('k-gone', ?, 'text', ?, 'pending', 0, '{}', '', 0, ?, ?)",
            (G2, json.dumps({"text": "残留待发", "push_kind": "status"}, ensure_ascii=False), now, now),
        )
        # G1 对照组（服务群，绝不能被误伤）
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
            " VALUES ('T-keep', ?, 'ws', '正常排队任务', 'queued', ?, ?)",
            (G1, now, now),
        )
        conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result, error, not_before, created, updated)"
            " VALUES ('k-keep', ?, 'text', ?, 'pending', 0, '{}', '', 0, ?, ?)",
            (G1, json.dumps({"text": "正常待发", "push_kind": "status"}, ensure_ascii=False), now, now),
        )


class TestRoundsFilterNonServed:
    """巡检入口只处理 settings.is_served 的行。"""

    async def test_tasks_round_skips_non_served(self, tmp_path: Path) -> None:
        """_tasks_round：非服务群的 queued 不派工、waiting_input 不提醒不搁置。"""
        app = _app(tmp_path, _raw(tmp_path / "data", groups=[G1]))
        await app.start()
        try:
            _seed_residual(app)
            spawned: list[str] = []
            app.spawn_run_task = lambda tid: spawned.append(tid)  # type: ignore[assignment]
            app._tasks_round(clock.now())
            assert "T-gone-q" not in spawned
            # waiting_input 到 6 小时该提醒的，非服务群不入队
            keys = [r["key"] for r in app.store.read().execute("SELECT key FROM outbox").fetchall()]
            assert not any(str(k).startswith("task-wait-remind:T-gone-w") for k in keys)
        finally:
            await app.stop()

    async def test_goals_round_skips_non_served(self, tmp_path: Path) -> None:
        """_goals_round：非服务群的到期提醒不入队、不推进 mark。"""
        app = _app(tmp_path, _raw(tmp_path / "data", groups=[G1]))
        await app.start()
        try:
            now = clock.now()
            with app.store.tx() as conn:
                conn.execute(
                    "INSERT INTO goals (id, group_id, kind, title, who_id, who_name, state,"
                    " due_ts, remind_ts, created, updated)"
                    " VALUES ('M-gone', ?, 'member', '残留提醒', 'u1', '小明', 'active', ?, ?, ?, ?)",
                    (G2, now - 100, now - 100, now, now),
                )
                # check 类型也不许 spawn coordinator.check_goal
                conn.execute(
                    "INSERT INTO goals (id, group_id, kind, title, state, next_check_ts, created, updated)"
                    " VALUES ('G-gone2', ?, 'agent', '残留检查', 'active', ?, ?, ?)",
                    (G2, now - 100, now, now),
                )
            checks: list[str] = []
            class _Coord:
                async def check_goal(self, gid):
                    checks.append(gid)
            app.coordinator = _Coord()
            await app._goals_round(now)
            keys = [r["key"] for r in app.store.read().execute("SELECT key FROM outbox").fetchall()]
            assert not any("M-gone" in str(k) for k in keys)
            assert checks == []
            # 没推进：remind_ts 还在原地
            row = app.store.read().execute("SELECT remind_ts FROM goals WHERE id='M-gone'").fetchone()
            assert float(row["remind_ts"]) == pytest.approx(now - 100)
        finally:
            await app.stop()

    async def test_approval_round_skips_non_served(self, tmp_path: Path) -> None:
        """_approval_round：非服务群的超期待批不发提醒（过期清理照常，那是数据维护）。"""
        app = _app(tmp_path, _raw(tmp_path / "data", groups=[G1]))
        await app.start()
        try:
            _seed_residual(app)
            app._approval_round(clock.now())
            rows = app.store.read().execute(
                "SELECT key, payload FROM outbox WHERE key LIKE 'approval-remind:%'"
            ).fetchall()
            assert all(G2 not in str(r["key"]) for r in rows)
            # 待批也没被标 reminded
            row = app.store.read().execute("SELECT reminded_ts FROM requests WHERE id='R-gone'").fetchone()
            assert row["reminded_ts"] is None
        finally:
            await app.stop()

    async def test_outbox_flush_skips_non_served(self, tmp_path: Path) -> None:
        """outbox.flush：非服务群的 pending 不发（留在原地，等回收逻辑标记）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings, _ = load_settings(_raw(tmp_path / "data", groups=[G1]))
        host_calls: list[dict] = []

        class _Host:
            async def send_text(self, session_id, text, *, reply_to=""):
                host_calls.append({"session_id": session_id, "text": text})
                return type("R", (), {"message_id": "m1"})()

        pushes = Pushes(store, lambda: settings)
        mentions = Mentions(store, lambda: settings)
        ob = Outbox(store, _Host(), pushes, mentions, lambda: settings)
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (G1, "s1"))
            conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (G2, "s2"))
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, not_before, created, updated)"
                " VALUES ('k-g2', ?, 'text', '{\"text\": \"x\", \"push_kind\": \"status\"}', 'pending', 0, 0, 0)",
                (G2,),
            )
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, not_before, created, updated)"
                " VALUES ('k-g1', ?, 'text', '{\"text\": \"y\", \"push_kind\": \"status\"}', 'pending', 0, 0, 0)",
                (G1,),
            )
        await ob.flush(NOON)
        # 只发了 G1 的
        assert [c["session_id"] for c in host_calls] == ["s1"]
        rows = {r["key"]: r["status"] for r in store.read().execute("SELECT key, status FROM outbox").fetchall()}
        assert rows["k-g1"] == "sent"
        assert rows["k-g2"] == "pending"


class TestReconcileOnConfigChange:
    """配置热更新删群 / 启动时：对不再服务的群就地标记，不删数据。"""

    async def test_update_config_marks_residual(self, tmp_path: Path) -> None:
        """开着改配置删掉 G2：G2 的 pending 发件 → cancelled、queued/waiting_input 任务 →
        cancelled、active 目标 → cancelled、pending 待批 → expired；G1 的不动。"""
        app = _app(tmp_path, _raw(tmp_path / "data", groups=[G1, G2]))
        await app.start()
        try:
            _seed_residual(app)
            # 热更新：删掉 G2
            await app.update_config(_raw(tmp_path / "data", groups=[G1]))
            store = app.store
            t1 = store.read().execute("SELECT status FROM tasks WHERE id='T-gone-q'").fetchone()
            t2 = store.read().execute("SELECT status FROM tasks WHERE id='T-gone-w'").fetchone()
            g1 = store.read().execute("SELECT state FROM goals WHERE id='G-gone'").fetchone()
            r1 = store.read().execute("SELECT status FROM requests WHERE id='R-gone'").fetchone()
            o1 = store.read().execute("SELECT status, error FROM outbox WHERE key='k-gone'").fetchone()
            assert t1["status"] == "cancelled"
            assert t2["status"] == "cancelled"
            assert g1["state"] == "cancelled"
            assert r1["status"] == "expired"
            assert o1["status"] == "cancelled"
            assert NOT_SERVED_REASON in str(o1["error"])
            # G1 对照组原地不动
            tk = store.read().execute("SELECT status FROM tasks WHERE id='T-keep'").fetchone()
            ok_ = store.read().execute("SELECT status FROM outbox WHERE key='k-keep'").fetchone()
            assert tk["status"] == "queued"
            assert ok_["status"] == "pending"
        finally:
            await app.stop()

    async def test_removed_group_cancels_already_running_and_reviewing_tasks(self, tmp_path: Path) -> None:
        app = _app(tmp_path, _raw(tmp_path / "data", groups=[G1, G2]))
        await app.start()
        try:
            now = clock.now()
            with app.store.tx() as conn:
                for tid, gid, status in (
                    ("T-gone-running", G2, "running"),
                    ("T-gone-reviewing", G2, "reviewing"),
                    ("T-keep-running", G1, "running"),
                ):
                    conn.execute(
                        "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
                        " VALUES (?, ?, 'ws', '执行中', ?, ?, ?)",
                        (tid, gid, status, now, now),
                    )
            stopped: list[str] = []
            app.cancel_task_run = lambda tid: stopped.append(tid) or True
            await app.update_config(_raw(tmp_path / "data", groups=[G1]))
            statuses = {r["id"]: r["status"] for r in app.store.read().execute(
                "SELECT id, status FROM tasks WHERE id LIKE 'T-%-running' OR id='T-gone-reviewing'"
            )}
            assert statuses["T-gone-running"] == "cancelled"
            assert statuses["T-gone-reviewing"] == "cancelled"
            assert statuses["T-keep-running"] == "running"
            assert set(stopped) == {"T-gone-running", "T-gone-reviewing"}
        finally:
            await app.stop()

    async def test_start_interrupts_orphaned_work_instead_of_replaying(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        db_file = data_dir / "maiwork.db"
        db_file.parent.mkdir(parents=True)
        store = Store(db_file)
        store.migrate()
        now = clock.now()
        with store.tx() as conn:
            for tid, status in (("T-crashed-running", "running"), ("T-crashed-review", "reviewing")):
                conn.execute(
                    "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
                    " VALUES (?, ?, 'ws', '中断的任务', ?, ?, ?)",
                    (tid, G1, status, now, now),
                )
        store.close()
        app = _app(tmp_path, _raw(data_dir, groups=[G1]))
        await app.start()
        try:
            for tid in ("T-crashed-running", "T-crashed-review"):
                task = app.tasks.get(tid)
                assert task["status"] == "paused"
                events = app.store.read().execute(
                    "SELECT payload FROM events WHERE entity_id=? AND kind='task.paused'", (tid,)
                ).fetchall()
                assert events and "中断" in str(events[-1]["payload"])
            dispatched: list[str] = []
            app.spawn_run_task = lambda tid: dispatched.append(tid)
            app._tasks_round(now)
            assert not dispatched
        finally:
            await app.stop()

    async def test_cancel_task_run_settles_its_handoffs(self, tmp_path: Path) -> None:
        """外部审查 2026-10-02：取消任务后，它名下交回了没验收的交接单收成 cancelled。"""
        app = _app(tmp_path, _raw(tmp_path / "data", groups=[G1]))
        await app.start()
        try:
            assert app.agents is not None
            tid = app.tasks.create(G1, title="执行中", req="做好", criteria=[], source="test")
            hid = app.agents.begin(G1, "task", "子活", task_id=tid)
            app.agents.running(G1, hid)
            app.agents.returned(G1, hid, "交回了", ok=True)
            app.tasks.transition(tid, "cancelled", reason="发起人取消")
            app.cancel_task_run(tid)
            assert app.agents.handoff(G1, hid)["status"] == "cancelled"
        finally:
            await app.stop()

    async def test_start_settles_handoffs_left_by_previous_process(self, tmp_path: Path) -> None:
        """线上残留：资讯补打开的交接单停在 returned。重启后没人会来验收，启动时收成 cancelled。"""
        data_dir = tmp_path / "data"
        app = _app(tmp_path, _raw(data_dir, groups=[G1]))
        await app.start()
        hid = app.agents.begin(G1, "news", "重看", task_id="feeds-recheck:x")
        app.agents.running(G1, hid)
        app.agents.returned(G1, hid, "s", ok=True)
        await app.stop()
        app2 = _app(tmp_path, _raw(data_dir, groups=[G1]))
        await app2.start()
        try:
            assert app2.agents.handoff(G1, hid)["status"] == "cancelled"
        finally:
            await app2.stop()

    async def test_clean_stop_pauses_running_task_and_stales_attempt(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        app = _app(tmp_path, _raw(data_dir, groups=[G1]))
        await app.start()
        tid = app.tasks.create(G1, title="执行中", req="做好", criteria=[], source="test")
        app.tasks.transition(tid, "running")
        app.tasks.start_attempt(tid)
        await app.stop()
        reopened = Store(data_dir / "maiwork.db")
        try:
            task = reopened.read().execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            attempt = reopened.read().execute(
                "SELECT status FROM attempts WHERE task_id=? ORDER BY n DESC LIMIT 1", (tid,)
            ).fetchone()
            assert task["status"] == "paused"
            assert attempt["status"] == "stale"
        finally:
            reopened.close()

    async def test_start_marks_residual_from_earlier_db(self, tmp_path: Path) -> None:
        """冷启动：库里本来就有 G2 残留（上次在配置里、这次不在了），start 时标掉。"""
        data_dir = tmp_path / "data"
        # 第一轮：两群都在，造残留
        app1 = _app(tmp_path, _raw(data_dir, groups=[G1, G2]))
        await app1.start()
        _seed_residual(app1)
        await app1.stop()
        # 第二轮：配置里只有 G1，冷启动照样回收 G2 残留
        app2 = _app(tmp_path, _raw(data_dir, groups=[G1]))
        await app2.start()
        try:
            store = app2.store
            t1 = store.read().execute("SELECT status FROM tasks WHERE id='T-gone-q'").fetchone()
            o1 = store.read().execute("SELECT status FROM outbox WHERE key='k-gone'").fetchone()
            assert t1["status"] == "cancelled"
            assert o1["status"] == "cancelled"
        finally:
            await app2.stop()

    async def test_cancelled_outbox_not_flushed(self, tmp_path: Path) -> None:
        """cancelled 的发件永远不再发。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings, _ = load_settings(_raw(tmp_path / "data", groups=[G1]))
        calls: list[str] = []

        class _Host:
            async def send_text(self, session_id, text, *, reply_to=""):
                calls.append(text)
                return type("R", (), {"message_id": "m1"})()

        ob = Outbox(store, _Host(), Pushes(store, lambda: settings), Mentions(store, lambda: settings), lambda: settings)
        now = clock.now()
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (G1, "s1"))
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, not_before, created, updated)"
                " VALUES ('k-cancel', ?, 'text', '{\"text\": \"x\", \"push_kind\": \"status\"}', 'cancelled', 0, ?, ?)",
                (G1, now, now),
            )
        await ob.flush(now)
        assert calls == []


def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_PORT: list[int] = []


def _port() -> int:
    """整个测试文件只挑一次：同一测试里前后两份配置端口要一样，不然会被当成「改了网页地址」重启。"""
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]
