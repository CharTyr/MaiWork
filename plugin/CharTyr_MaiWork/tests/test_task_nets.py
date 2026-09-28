"""任务安全网（0.4.0）：[tasks] token_limit / run_seconds，超限自动 paused + paused_reason 暴露 + 继续后重新算。"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks


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
        from CharTyr_MaiWork.coordinator import Coordinator
        from CharTyr_MaiWork.models import ChatResult

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

            async def chat(self, role, messages, **kw):
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
        from CharTyr_MaiWork.coordinator import Coordinator
        from CharTyr_MaiWork.models import ChatResult

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

            async def chat(self, role, messages, **kw):
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
