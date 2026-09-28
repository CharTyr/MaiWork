"""M3 数据层 tasks.py：任务状态机（docs/02 §7.2、docs/07 §9.3 / §11.2）。

红线对应用例：
- 非法转移一律 ValueError（中文），终态不可改；
- 取消后晚到的结果不复活（accept_result=False 且 attempt 标 stale，历史保留）；
- req_version 不对的晚到结果同样不接；
- 群友版详情不含 env / timeline。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

NOW = 1_790_000_000.0


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


class _Settings:
    def workspace_of(self, group_id: str) -> str:
        return "ws-demo"


GET_SETTINGS = lambda: _Settings()  # noqa: E731


@pytest.fixture
def tasks(mem_store: Store) -> Tasks:
    return Tasks(mem_store, GET_SETTINGS)


def _create(tasks: Tasks, gid: str = "900000001", **kw) -> str:
    kwargs = {"title": "整理资料", "req": "把上周聊的工具整理成一页", "criteria": ["包含链接"], "source": "test"}
    kwargs.update(kw)
    return tasks.create(gid, **kwargs)


def _status(tasks: Tasks, task_id: str) -> str:
    row = tasks.get(task_id)
    assert row is not None
    return str(row["status"])


def _events(mem_store: Store, kind: str) -> list[dict]:
    rows = mem_store.read().execute(
        "SELECT kind, entity, entity_id, payload, ts FROM events WHERE kind=? ORDER BY id", (kind,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# create / task_versions
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_id_and_writes_version1(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks)
        assert tid == "T-1"
        row = tasks.get(tid)
        assert row["status"] == "queued"
        assert row["req_version"] == 1
        assert row["workspace"] == "ws-demo"
        v = mem_store.read().execute(
            "SELECT version, req, criteria FROM task_versions WHERE task_id=?", (tid,)
        ).fetchone()
        assert v["version"] == 1
        assert json.loads(v["criteria"]) == ["包含链接"]

    def test_create_writes_task_created_event(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks)
        ev = _events(mem_store, "task.queued")
        assert len(ev) == 1
        assert ev[0]["entity_id"] == tid

    def test_create_second_task_gets_next_id(self, tasks: Tasks):
        assert _create(tasks) == "T-1"
        assert _create(tasks) == "T-2"

    def test_create_with_pending_approval_status(self, tasks: Tasks):
        tid = _create(tasks, status="pending_approval")
        assert _status(tasks, tid) == "pending_approval"


# ---------------------------------------------------------------------------
# 合法转移全表抽测（02 §7.2）
# ---------------------------------------------------------------------------

_LEGAL = {
    "pending_approval": {"queued", "rejected", "cancelled"},
    "queued": {"running", "paused", "cancelled"},
    "running": {"reviewing", "waiting_input", "failed", "paused", "cancelled", "queued"},
    "reviewing": {"completed", "running", "queued", "failed", "waiting_input", "cancelled"},
    "waiting_input": {"queued", "running", "shelved", "cancelled"},
    "shelved": {"queued", "cancelled"},
    "paused": {"queued", "cancelled"},
    "failed": {"queued"},
    "completed": set(),
    "cancelled": set(),
    "rejected": set(),
}

_ALL_STATES = list(_LEGAL)


class TestTransitionLegality:
    def test_every_legal_transition_accepted(self, tasks: Tasks, mem_store: Store):
        for src in _ALL_STATES:
            for dst in sorted(_LEGAL[src]):
                tid = _create(tasks, status=src)
                out = tasks.transition(tid, dst)
                assert out["status"] == dst, f"{src} -> {dst} 应被允许"
                ev = _events(mem_store, f"task.{dst}")
                assert any(e["entity_id"] == tid for e in ev), f"{src} -> {dst} 应写事件 task.{dst}"

    def test_every_illegal_transition_rejected(self, tasks: Tasks):
        for src in _ALL_STATES:
            for dst in _ALL_STATES:
                if dst in _LEGAL[src]:
                    continue
                tid = _create(tasks, status=src)
                with pytest.raises(ValueError):
                    tasks.transition(tid, dst), f"{src} -> {dst} 应被拒绝"
                assert _status(tasks, tid) == src, "失败转移不应改变状态"

    def test_illegal_transition_raises_chinese_message(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        with pytest.raises(ValueError) as ei:
            tasks.transition(tid, "completed")
        msg = str(ei.value)
        assert any("\u4e00" <= ch <= "\u9fff" for ch in msg), f"错误消息应是中文: {msg}"

    def test_unknown_state_rejected(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        with pytest.raises(ValueError):
            tasks.transition(tid, "bogus_state")

    def test_missing_task_raises(self, tasks: Tasks):
        with pytest.raises(KeyError):
            tasks.transition("T-999", "queued")


class TestTerminalStates:
    @pytest.mark.parametrize("state", ["completed", "cancelled", "rejected"])
    def test_terminal_states_cannot_change(self, tasks: Tasks, state: str):
        tid = _create(tasks, status=state)
        for dst in _ALL_STATES:
            with pytest.raises(ValueError):
                tasks.transition(tid, dst)
        assert _status(tasks, tid) == state

    def test_terminal_transition_records_finished_ts(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "completed")
        row = tasks.get(tid)
        assert row["finished_ts"] == NOW


class TestTransitionSideEffects:
    def test_first_running_records_started_ts(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        tasks.transition(tid, "running")
        assert tasks.get(tid)["started_ts"] == NOW

    def test_second_running_keeps_original_started_ts(self, tasks: Tasks, fixed_clock):
        tid = _create(tasks, status="queued")
        tasks.transition(tid, "running")
        first = tasks.get(tid)["started_ts"]
        fixed_clock[0] = NOW + 100
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "running")
        assert tasks.get(tid)["started_ts"] == first

    def test_updated_bumps_on_transition(self, tasks: Tasks, fixed_clock):
        tid = _create(tasks, status="queued")
        fixed_clock[0] = NOW + 50
        tasks.transition(tid, "running")
        assert tasks.get(tid)["updated"] == NOW + 50

    def test_fields_updated(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(
            tid, "waiting_input",
            question="要哪种格式？", question_ts=NOW, question_msg_id="m-1",
        )
        row = tasks.get(tid)
        assert row["question"] == "要哪种格式？"
        assert row["question_msg_id"] == "m-1"

    def test_delivery_list_serialized_as_json(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "reviewing")
        tasks.transition(
            tid, "completed",
            review="通过",
            delivery=[{"kind": "here.now", "text": "做好了", "state": "sent", "url": "https://x"}],
        )
        row = tasks.get(tid)
        assert json.loads(row["delivery"]) == [
            {"kind": "here.now", "text": "做好了", "state": "sent", "url": "https://x"}
        ]

    def test_unknown_field_raises(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        with pytest.raises(ValueError):
            tasks.transition(tid, "running", bogus_field=1)

    def test_transition_returns_updated_row(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        row = tasks.transition(tid, "running")
        assert row["id"] == tid
        assert row["status"] == "running"


# ---------------------------------------------------------------------------
# revise / attempts / accept_result
# ---------------------------------------------------------------------------


class TestRevise:
    def test_revise_bumps_version_and_writes_row(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks)
        v = tasks.revise(tid, req="改成两页", criteria=["有链接", "有图片"])
        assert v == 2
        assert tasks.get(tid)["req_version"] == 2
        row = mem_store.read().execute(
            "SELECT req, criteria FROM task_versions WHERE task_id=? AND version=2", (tid,)
        ).fetchone()
        assert row["req"] == "改成两页"
        assert json.loads(row["criteria"]) == ["有链接", "有图片"]

    def test_revise_running_sends_back_to_queued(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="running")
        tasks.revise(tid, req="新需求", criteria=[])
        assert _status(tasks, tid) == "queued"
        assert _events(mem_store, "task.queued") != []

    def test_revise_reviewing_sends_back_to_queued(self, tasks: Tasks):
        tid = _create(tasks, status="reviewing")
        tasks.revise(tid, req="新需求", criteria=[])
        assert _status(tasks, tid) == "queued"

    def test_revise_queued_stays(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        tasks.revise(tid, req="新需求", criteria=[])
        assert _status(tasks, tid) == "queued"


class TestAttempts:
    def test_start_attempt_first_gets_n1(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="queued")
        n = tasks.start_attempt(tid)
        assert n == 1
        row = mem_store.read().execute(
            "SELECT n, req_version, status FROM attempts WHERE task_id=?", (tid,)
        ).fetchone()
        assert row["n"] == 1
        assert row["req_version"] == 1
        assert tasks.get(tid)["attempts"] == 1

    def test_attempt_id_helper(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        assert isinstance(aid, int) and aid >= 1

    def test_finish_attempt_updates_row(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="queued")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        tasks.finish_attempt(aid, status="done", summary="完成了", evidence=["f1.py"], artifacts=["out.html"], review="")
        row = mem_store.read().execute("SELECT status, summary, evidence, artifacts, finished FROM attempts WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "done"
        assert json.loads(row["evidence"]) == ["f1.py"]
        assert row["finished"] is not None

    def test_second_attempt_n2_with_new_version(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="queued")
        tasks.start_attempt(tid)
        tasks.revise(tid, req="v2", criteria=[])
        tasks.start_attempt(tid)
        row = mem_store.read().execute(
            "SELECT n, req_version FROM attempts WHERE task_id=? ORDER BY n DESC LIMIT 1", (tid,)
        ).fetchone()
        assert row["n"] == 2
        assert row["req_version"] == 2
        assert tasks.get(tid)["attempts"] == 2


class TestAcceptResult:
    def test_accepts_current_running(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        assert tasks.accept_result(tid, aid, req_version=1) is True

    @pytest.mark.parametrize("state", ["completed", "cancelled", "rejected", "failed"])
    def test_terminal_state_rejects_and_marks_stale(self, tasks: Tasks, mem_store: Store, state: str):
        tid = _create(tasks, status="queued")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        # 任务已进入终态，晚到的结果一律不接
        mem_store.read().execute("UPDATE tasks SET status=? WHERE id=?", (state, tid))
        assert tasks.accept_result(tid, aid, req_version=1) is False
        row = mem_store.read().execute("SELECT status FROM attempts WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "stale"

    def test_paused_rejects(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="queued")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        mem_store.read().execute("UPDATE tasks SET status='paused' WHERE id=?", (tid,))
        assert tasks.accept_result(tid, aid, req_version=1) is False

    def test_wrong_req_version_rejects_and_marks_stale(self, tasks: Tasks, mem_store: Store):
        tid = _create(tasks, status="running")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        # 需求改了，旧尝试的结果不能冒充满足新需求
        mem_store.read().execute("UPDATE tasks SET req_version=2 WHERE id=?", (tid,))
        assert tasks.accept_result(tid, aid, req_version=1) is False
        assert mem_store.read().execute("SELECT status FROM attempts WHERE id=?", (aid,)).fetchone()["status"] == "stale"

    def test_cancelled_task_result_does_not_revive(self, tasks: Tasks):
        tid = _create(tasks, status="queued")
        tasks.start_attempt(tid)
        aid = tasks.current_attempt_id(tid)
        tasks.transition(tid, "cancelled")
        assert tasks.accept_result(tid, aid, req_version=1) is False
        assert _status(tasks, tid) == "cancelled"

    def test_missing_task_returns_false(self, tasks: Tasks):
        assert tasks.accept_result("T-999", 1, req_version=1) is False


# ---------------------------------------------------------------------------
# list_view / detail_view / running_count
# ---------------------------------------------------------------------------


class TestListView:
    def test_pending_approval_excluded(self, tasks: Tasks):
        _create(tasks, status="pending_approval")
        _create(tasks, status="queued")
        _create(tasks, status="running")
        out = tasks.list_view("900000001")
        statuses = [r["status"] for r in out]
        assert "pending_approval" not in statuses

    def test_fields_and_order(self, tasks: Tasks, fixed_clock):
        t1 = _create(tasks, status="queued", title="第一个")
        fixed_clock[0] = NOW + 10
        t2 = _create(tasks, status="running", title="第二个")
        out = tasks.list_view("900000001")
        assert out[0]["id"] == t2  # 按 updated 倒序
        assert out[1]["id"] == t1
        r = out[0]
        assert r["title"] == "第二个"
        assert r["updated_ts"] == NOW + 10
        assert "icon" in r and "meta" in r and "goal_id" in r and "undelivered" in r

    def test_meta_running(self, tasks: Tasks, fixed_clock):
        tid = _create(tasks, status="queued")
        tasks.transition(tid, "running")  # 此刻 clock=NOW，started_ts=NOW
        tasks.start_attempt(tid)  # attempts = 1
        fixed_clock[0] = NOW + 300  # 跑了 5 分钟
        r = next(x for x in tasks.list_view("900000001") if x["id"] == tid)
        assert r["meta"].startswith("已跑 5 分钟")
        assert "尝试" in r["meta"]

    def test_meta_running_just_started(self, tasks: Tasks, fixed_clock):
        """running 但还不到 1 分钟：显示「刚开始」，不是「已跑 0 分钟」。"""
        tid = _create(tasks, status="queued")
        tasks.transition(tid, "running")
        tasks.start_attempt(tid)
        fixed_clock[0] = NOW + 30  # 30 秒
        r = next(x for x in tasks.list_view("900000001") if x["id"] == tid)
        assert r["meta"].startswith("刚开始")
        assert "尝试" in r["meta"]

    def test_meta_waiting_input(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        long_q = "这是一个很长的问题要问群主" + "字" * 40
        tasks.transition(tid, "waiting_input", question=long_q, question_ts=NOW - 7200)
        r = next(x for x in tasks.list_view("900000001") if x["id"] == tid)
        assert r["meta"].startswith("等回答：")
        assert len(r["meta"].split("等回答：")[1].split(" ")[0].rstrip("，, ·")) <= 30

    def test_meta_completed_with_date(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "completed")
        r = next(x for x in tasks.list_view("900000001") if x["id"] == tid)
        assert r["meta"].startswith("完成于 ")

    def test_meta_completed_undelivered_appended(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "completed", undelivered=1)
        r = next(x for x in tasks.list_view("900000001") if x["id"] == tid)
        assert "做完了但还没发出去" in r["meta"]

    def test_meta_failed(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "failed", review="子 agent 超时两次")
        r = next(x for x in tasks.list_view("900000001") if x["id"] == tid)
        assert r["meta"].startswith("失败：")

    def test_other_group_not_listed(self, tasks: Tasks):
        _create(tasks, gid="900000001")
        _create(tasks, gid="111222333")
        assert all(r["goal_id"] is None for r in tasks.list_view("111222333"))
        assert [r["id"] for r in tasks.list_view("111222333")] != []  # sanity
        assert any(r["id"].startswith("T-1") for r in tasks.list_view("900000001"))


class TestDetailView:
    def test_member_view_has_no_env_and_no_timeline(self, mem_store: Store):
        from CharTyr_MaiWork.maiwork.tools import Tools
        tasks = Tasks(mem_store, GET_SETTINGS, tools=Tools(mem_store))
        tid = _create(tasks, status="running")
        mem_store.read().execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok)"
            " VALUES (?, '900000001', ?, '子 agent #1', 'read_file', 'in', 'out', 35, 1)",
            (NOW, tid),
        )
        d = tasks.detail_view(tid, admin=False)
        assert "env" not in d
        assert "timeline" not in d
        assert d["steps"] == 1  # 群友只看到步数

        d2 = tasks.detail_view(tid, admin=True)
        assert "env" in d2
        assert isinstance(d2["timeline"], list) and len(d2["timeline"]) == 1
        t = d2["timeline"][0]
        assert t["tool"] == "read_file" and t["ok"] is True and t["input"] == "in" and t["ms"] == 35

    def test_structure_matches_spec(self, tasks: Tasks):
        tid = _create(tasks, criteria=["a", "b"])
        d = tasks.detail_view(tid, admin=True)
        assert d["req"] == "把上周聊的工具整理成一页"
        assert d["criteria"] == ["a", "b"]
        assert d["question"] is None
        assert d["review"] == ""
        assert d["delivery"] == []
        assert "updated_ts" in d and "meta" in d and "undelivered" in d

    def test_delivery_decoded(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "reviewing")
        tasks.transition(tid, "completed", delivery=[{"kind": "群文件", "text": "已传", "state": "sent", "url": None}])
        d = tasks.detail_view(tid, admin=True)
        assert d["delivery"][0]["kind"] == "群文件"

    def test_question_shown_when_waiting(self, tasks: Tasks):
        tid = _create(tasks, status="running")
        tasks.transition(tid, "waiting_input", question="要 A 还是 B？", question_ts=NOW)
        assert tasks.detail_view(tid, admin=False)["question"] == "要 A 还是 B？"

    def test_missing_task(self, tasks: Tasks):
        with pytest.raises(KeyError):
            tasks.detail_view("T-999", admin=True)

    def test_tools_none_steps_zero(self, mem_store: Store):
        t2 = Tasks(mem_store, GET_SETTINGS, tools=None)
        tid = t2.create("900000001", title="x", req="y", criteria=[], source="test")
        d = t2.detail_view(tid, admin=True)
        assert d["steps"] == 0
        assert d["timeline"] == []


class TestRunningCount:
    def test_running_only(self, tasks: Tasks, mem_store: Store):
        _create(tasks, status="running")
        _create(tasks, status="running")
        _create(tasks, status="queued")
        assert tasks.running_count("900000001") == 2
        assert tasks.running_count("0") == 0
