"""任务安全网（0.4.0）：[tasks] token_limit / run_seconds，超限自动 paused + paused_reason 暴露 + 继续后重新算。"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks


class _Settings:
    class tasks:
        token_limit = 1000
        run_seconds = 60

    def workspace_of(self, gid: str) -> str:
        return f"g{gid}"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def tasks(store):
    return Tasks(store, lambda: _Settings())


def _mk_task(tasks) -> str:
    return tasks.create("900000001", title="t", req="r", criteria=[], source="request")


class TestPausedReasonColumn:
    def test_migration_adds_column(self, store):
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(tasks)")}
        assert "paused_reason" in cols

    def test_transition_paused_with_reason(self, tasks, store):
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(tid, "paused", reason="安全网：token 超限", paused_reason={"kind": "tokens", "limit": 1000, "used": 1200})
        row = tasks.get(tid)
        assert row["status"] == "paused"
        pr = json.loads(row["paused_reason"])
        assert pr["kind"] == "tokens"
        assert pr["limit"] == 1000
        assert pr["used"] == 1200

    def test_resume_clears_and_sets_baseline(self, tasks, store):
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(tid, "paused", reason="安全网", paused_reason={"kind": "tokens", "limit": 1000, "used": 1200})
        # 记三笔 usage 让 resume 有「当前用量」可记
        now = clock.now()
        with store.tx() as conn:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id, prompt_tokens, completion_tokens, ok, ms, error)"
                    " VALUES (?, ?, 'main', 'm', 'p', '900000001', ?, 100, 50, 1, 10, '')",
                    (now, clock.day_key(now), tid),
                )
        tasks.transition(tid, "queued", reason="管理员继续")
        row = tasks.get(tid)
        assert row["paused_reason"] in ("", None)
        base = store.kv_get(f"task.net_base.{tid}")
        assert base is not None
        assert base["tokens"] == 450  # 3 × (100+50)
        assert base["resume_ts"] > 0

    def test_meta_shows_auto_pause(self, tasks):
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(tid, "paused", reason="安全网", paused_reason={"kind": "time", "limit": 10800, "used": 10900})
        view = tasks.list_view("900000001")
        assert view[0]["paused_reason"]["kind"] == "time"
        # 网页上给人看的一句话：大白话，不露 token 数字 / 秒数 / 「安全网」这种词
        assert view[0]["meta"] == "自动暂停：做得太久了，等你决定"

    def test_detail_view_exposes(self, tasks):
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(tid, "paused", reason="安全网", paused_reason={"kind": "tokens", "limit": 2000000, "used": 2100000})
        d = tasks.detail_view(tid, admin=True)
        assert d["paused_reason"]["kind"] == "tokens"
        # 群友版也带（自动暂停原因不涉及敏感信息）
        d2 = tasks.detail_view(tid, admin=False)
        assert d2["paused_reason"]["kind"] == "tokens"


class TestCoordinatorNet:
    @pytest.mark.asyncio
    async def test_token_limit_pauses(self, tmp_path):
        """coordinator 在一次主模型调后用掉 > limit 的 token：任务自动 paused。"""
        from CharTyr_MaiWork.maiwork.coordinator import Coordinator
        from CharTyr_MaiWork.maiwork.models import ChatResult

        class _S:
            class tasks:
                token_limit = 500
                run_seconds = 0

            class models:
                context_window = 128000

            def workspace_of(self, gid):
                return f"g{gid}"

        store = Store(tmp_path / "t.db")
        store.migrate()
        tasks = Tasks(store, lambda: _S())

        class _Ready:
            def ready(self):
                return True

        class _M:
            def settings(self):
                return _Ready()

            async def chat(self, role=None, messages=None, **kw):
                # 回一次 plan JSON，但 usage 已超线（在 chat 内部写 usage 的测试桩直接插库）
                tid = kw.get("task_id") or ""
                if tid:
                    now = clock.now()
                    with store.tx() as conn:
                        conn.execute(
                            "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id, prompt_tokens, completion_tokens, ok, ms, error)"
                            " VALUES (?, ?, 'main', 'm', 'p', '900000001', ?, 400, 300, 1, 10, '')",
                            (now, clock.day_key(now), tid),
                        )
                return ChatResult(
                    text='{"criteria":["c1"], "deliver_kind":"text", "jobs": [], "question": null, "env": "local"}',
                    tool_calls=[], model="m", prompt_tokens=400, completion_tokens=300, raw_message={},
                )

        coord = Coordinator(
            store=store, models=_M(), workers=None, tools=None, tasks=tasks, goals=None,
            delivery=None, outbox=None, env=None, profiles=None, get_settings=lambda: _S(),
        )
        tid = tasks.create("900000001", title="t", req="r", criteria=[], source="request")
        await coord.run_task(tid)
        row = tasks.get(tid)
        # 任务：token 超限 → paused，原因 kind=tokens
        assert row["status"] == "paused"
        assert json.loads(row["paused_reason"] or "{}")["kind"] == "tokens"

    @pytest.mark.asyncio
    async def test_time_limit_pauses(self, tmp_path):
        """跑太久（started_ts 远早于现在）：paused kind=time。"""
        from CharTyr_MaiWork.maiwork.coordinator import Coordinator
        from CharTyr_MaiWork.maiwork.models import ChatResult

        class _S:
            class tasks:
                token_limit = 0
                run_seconds = 60

            class models:
                context_window = 128000

            def workspace_of(self, gid):
                return f"g{gid}"

        store = Store(tmp_path / "t.db")
        store.migrate()
        tasks = Tasks(store, lambda: _S())
        tid = tasks.create("900000001", title="t", req="r", criteria=[], source="request")
        tasks.transition(tid, "running", reason="开工")
        # 把 started_ts 改到 2 小时前
        with store.tx() as conn:
            conn.execute("UPDATE tasks SET started_ts=? WHERE id=?", (clock.now() - 7200, tid))

        class _Ready:
            def ready(self):
                return True

        class _M:
            def settings(self):
                return _Ready()

            async def chat(self, role=None, messages=None, **kw):
                return ChatResult(
                    text='{"criteria":["c1"], "deliver_kind":"text", "jobs": [], "question": null, "env": "local"}',
                    tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={},
                )

        coord = Coordinator(
            store=store, models=_M(), workers=None, tools=None, tasks=tasks, goals=None,
            delivery=None, outbox=None, env=None, profiles=None, get_settings=lambda: _S(),
        )
        # 手动走一遍安全网判停（run_task 的全链路需要假 Workers 等，交给 e2e）
        reason = await coord._net_check(tid)
        assert reason is not None and reason["kind"] == "time"
        row = tasks.get(tid)
        assert row["status"] == "paused"


class _FlakyConn:
    """包一层连接：查 usage 的前 fail_times 次抛异常（模拟偶发锁冲突 / 表读不了）。

    其它语句一律转给真连接，所以 Tasks.get / kv_get 这些读还是正常的。
    """

    def __init__(self, conn, *, fail_times: int = 1) -> None:
        self._conn = conn
        self.fail_times = int(fail_times)
        self.usage_calls = 0

    def execute(self, sql, *args):
        if "FROM usage" in str(sql):
            self.usage_calls += 1
            if self.usage_calls <= self.fail_times:
                raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _tasks_with(store, *, token_limit: int, run_seconds: int = 0) -> Tasks:
    class _S:
        class tasks:
            pass

        def workspace_of(self, gid: str) -> str:
            return f"g{gid}"

    _S.tasks.token_limit = token_limit
    _S.tasks.run_seconds = run_seconds
    return Tasks(store, lambda: _S())


class TestNetCheckUsageUnknown:
    """读不到用量时不许当「一个 token 都没花」（fail-open）——有上限就停下来等人。"""

    def test_usage_read_failure_pauses_fail_closed(self, store, monkeypatch, caplog):
        tasks = _tasks_with(store, token_limit=1000)
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        proxy = _FlakyConn(store.read(), fail_times=99)
        monkeypatch.setattr(store, "read", lambda: proxy)
        with caplog.at_level(logging.WARNING):
            reason = tasks.net_check(tid)
        assert reason == {"kind": "tokens_unknown", "limit": 1000}
        assert proxy.usage_calls == 2  # 读失败先立刻重试一次，才判定
        row = tasks.get(tid)
        assert row["status"] == "paused"
        # 暂停说明（note）进 task.paused 事件的 reason（和手动暂停一样不改 review 字段）
        ev = store.read().execute(
            "SELECT payload FROM events WHERE entity='task' AND entity_id=? AND kind='task.paused'"
            " ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert "无法核算用量" in str(json.loads(ev["payload"])["reason"])
        pr = json.loads(row["paused_reason"])
        assert pr["kind"] == "tokens_unknown" and pr["limit"] == 1000
        assert "读不到" in caplog.text or "用量" in caplog.text  # 留一条 warning

    def test_usage_read_retry_success_does_not_pause(self, store, monkeypatch):
        """一次偶发失败、重试读到数：照常用量判，别冤枉暂停。"""
        tasks = _tasks_with(store, token_limit=1000)
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        now = clock.now()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id, prompt_tokens, completion_tokens, ok, ms, error)"
                " VALUES (?, ?, 'main', 'm', 'p', '900000001', ?, 200, 100, 1, 10, '')",
                (now, clock.day_key(now), tid),
            )
        proxy = _FlakyConn(store.read(), fail_times=1)
        monkeypatch.setattr(store, "read", lambda: proxy)
        assert tasks.net_check(tid) is None
        assert proxy.usage_calls == 2
        assert tasks.get(tid)["status"] == "running"

    def test_usage_read_failure_ignored_when_token_limit_off(self, store, monkeypatch):
        """token_limit = 0（不限）：读不到用量也不停，只是重试一次。"""
        tasks = _tasks_with(store, token_limit=0, run_seconds=3600)
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        proxy = _FlakyConn(store.read(), fail_times=99)
        monkeypatch.setattr(store, "read", lambda: proxy)
        assert tasks.net_check(tid) is None
        assert proxy.usage_calls == 2
        assert tasks.get(tid)["status"] == "running"

    def test_usage_read_failure_still_applies_time_limit(self, store, monkeypatch):
        """读不到用量不影响时长判断：该按跑太久停下来还是停。"""
        tasks = _tasks_with(store, token_limit=0, run_seconds=60)
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        with store.tx() as conn:
            conn.execute("UPDATE tasks SET started_ts=? WHERE id=?", (clock.now() - 7200, tid))
        proxy = _FlakyConn(store.read(), fail_times=99)
        monkeypatch.setattr(store, "read", lambda: proxy)
        reason = tasks.net_check(tid)
        assert reason is not None and reason["kind"] == "time"
        assert tasks.get(tid)["status"] == "paused"

    def test_tokens_unknown_reason_rendered(self, tasks):
        """新 kind 要能写进库、并且网页三种视图都有一句安全中文。"""
        tid = _mk_task(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(
            tid, "paused", reason="安全网：无法核算用量，已自动暂停",
            paused_reason={"kind": "tokens_unknown", "limit": 2000000},
        )
        view = tasks.list_view("900000001")
        item = view[0]
        assert item["paused_reason"]["kind"] == "tokens_unknown"
        assert item["meta"] == "自动暂停：用量没核出来，等你决定"
        assert "自动暂停" in item["paused_reason"]["text"]
        d = tasks.detail_view(tid, admin=True)
        assert d["paused_reason"]["kind"] == "tokens_unknown"
        assert d["paused_reason"]["text"]
        d2 = tasks.detail_view(tid, admin=False)
        assert d2["paused_reason"]["kind"] == "tokens_unknown"
