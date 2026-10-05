"""任务双岗协作（docs/20）：干活 lane 的持久对话存储 + 续用时的历史整理。

红线对应用例：
- lane 只在**同一个任务内部**记前情：任务到终态（完成 / 失败 / 取消 / 拒绝）同一事务清空；
- 取消 / 需求改版之后晚到的保存不复活对话（按任务状态 + req_version 判）；
- 别的群读不到这条 lane；
- 续用时 system 不存；本轮不给的工具，它的调用记录改写成文字，不留工具调用格式；
- 交回（submit_result）后悬着没回复的工具调用补一条回复，严格端点不 400。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.lanes import TaskLanes, close_dangling_tool_calls, prepare_history
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

GID = "900000001"
OTHER = "111222333"
NOW = 1_790_000_000.0


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: NOW)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


class _Settings:
    def workspace_of(self, group_id: str) -> str:
        return "ws-demo"


@pytest.fixture
def tasks(store: Store) -> Tasks:
    return Tasks(store, lambda: _Settings())


@pytest.fixture
def lanes(store: Store) -> TaskLanes:
    return TaskLanes(store)


def _running_task(tasks: Tasks) -> str:
    tid = tasks.create(GID, title="整理资料", req="整理一页", criteria=["有链接"], source="test")
    if str(tasks.get(tid)["status"]) == "pending_approval":
        tasks.transition(tid, "queued")
    tasks.transition(tid, "running")
    return tid


MSGS = [
    {"role": "user", "content": "派的活：整理一页"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"q\": \"x\"}"}},
    ]},
    {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "搜到 3 条"},
]


class TestStore:
    def test_migration_adds_task_lanes(self, store: Store) -> None:
        names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_lanes" in names
        assert int(store._conn.execute("PRAGMA user_version").fetchone()[0]) == 35

    def test_save_then_load_roundtrip(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        assert lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        got = lanes.load(tid, "worker:1", group_id=GID)
        assert got is not None
        assert got["messages"] == MSGS
        assert got["kind"] == "task"
        assert got["status"] == "open"

    def test_other_group_cannot_read(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        assert lanes.load(tid, "worker:1", group_id=OTHER) is None

    def test_save_refused_with_wrong_group(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        assert not lanes.save(tid, "worker:1", group_id=OTHER, kind="task", messages=MSGS, req_version=1)
        assert lanes.load(tid, "worker:1", group_id=OTHER) is None

    @pytest.mark.parametrize("to", ["cancelled", "failed", "completed"])
    def test_terminal_transition_clears_messages(self, tasks: Tasks, lanes: TaskLanes, to: str) -> None:
        tid = _running_task(tasks)
        lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        if to == "completed":
            tasks.transition(tid, "reviewing")
        tasks.transition(tid, to)
        got = lanes.load(tid, "worker:1", group_id=GID)
        assert got is not None and got["messages"] == [] and got["status"] == "closed"

    def test_late_save_after_cancel_does_not_revive(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        tasks.transition(tid, "cancelled")
        assert not lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        got = lanes.load(tid, "worker:1", group_id=GID)
        assert got is None or got["messages"] == []

    def test_revise_closes_worker_lanes_and_late_save_refused(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        v = tasks.revise(tid, req="改成两页", criteria=["有链接"])
        got = lanes.load(tid, "worker:1", group_id=GID)
        assert got is not None and got["messages"] == [] and got["status"] == "closed"
        # 旧版本的晚到保存不算数
        assert not lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        # 新版本可以重新开
        assert lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=v)
        assert lanes.load(tid, "worker:1", group_id=GID)["status"] == "open"

    def test_escalated_flag_and_model_persist(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1,
                   model="muse", escalated=True, snapshot="前情提要")
        got = lanes.load(tid, "worker:1", group_id=GID)
        assert got["escalated"] is True and got["model"] == "muse" and got["snapshot"] == "前情提要"
        # 之后不传 escalated / snapshot 的保存不把它们冲掉
        lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        got = lanes.load(tid, "worker:1", group_id=GID)
        assert got["escalated"] is True and got["snapshot"] == "前情提要"

    def test_list_for_task(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=MSGS, req_version=1)
        lanes.save(tid, "worker:2", group_id=GID, kind="task", messages=[], req_version=1)
        assert [x["lane"] for x in lanes.list(tid, group_id=GID)] == ["worker:1", "worker:2"]


class TestHistory:
    def test_prepare_drops_system(self) -> None:
        out = prepare_history([{"role": "system", "content": "旧规矩"}, *MSGS], allowed=("web_search", "submit_result"))
        assert all(m["role"] != "system" for m in out)
        assert out == MSGS

    def test_prepare_rewrites_tools_not_allowed_this_round(self) -> None:
        out = prepare_history(MSGS, allowed=("fetch_page", "submit_result"))
        # 不再出现工具调用格式
        assert not any(m.get("tool_calls") for m in out)
        assert not any(m["role"] == "tool" for m in out)
        text = json.dumps(out, ensure_ascii=False)
        assert "web_search" in text and "搜到 3 条" in text
        assert "这一轮不能再用" in text

    def test_prepare_keeps_allowed_groups_intact(self) -> None:
        out = prepare_history(MSGS, allowed=("web_search",))
        assert out[1]["tool_calls"][0]["function"]["name"] == "web_search"
        assert out[2]["role"] == "tool"

    def test_close_dangling_adds_tool_reply(self) -> None:
        msgs = [
            {"role": "user", "content": "活"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {"name": "submit_result", "arguments": "{}"}},
                {"id": "s2", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
            ]},
        ]
        out = close_dangling_tool_calls(msgs)
        ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
        assert ids == ["s1", "s2"]
        assert out[0] == msgs[0]

    def test_close_dangling_noop_when_complete(self) -> None:
        assert close_dangling_tool_calls(MSGS) == MSGS

    def test_prepare_tolerates_garbage(self) -> None:
        out = prepare_history([None, "x", {"role": "user", "content": "ok"}], allowed=())
        assert out == [{"role": "user", "content": "ok"}]


class TestDetailNotes:
    """§八：返工 / 换模型 / 重开的大白话记录进管理员版任务详情（群友版看不到）。"""

    def _note(self, store: Store, tid: str, kind: str, note: str) -> None:
        with store.tx() as conn:
            store.event(conn, kind, group_id=GID, entity="task", entity_id=tid, payload={"note": note, "lane": "worker:1"})

    def test_admin_sees_lane_notes(self, store: Store, tasks: Tasks) -> None:
        tid = _running_task(tasks)
        self._note(store, tid, "task.lane_rework", "第 1 条活被打回：接着改")
        self._note(store, tid, "task.lane_escalate", "换成「主模型」接着改")
        self._note(store, tid, "task.exec_unavailable", "别的事件不进这里")
        d = tasks.detail_view(tid, admin=True)
        assert [n["note"] for n in d["lane_notes"]] == ["第 1 条活被打回：接着改", "换成「主模型」接着改"]
        assert d["lane_notes"][1]["kind"] == "escalate"

    def test_member_does_not_see_lane_notes(self, store: Store, tasks: Tasks) -> None:
        tid = _running_task(tasks)
        self._note(store, tid, "task.lane_rework", "第 1 条活被打回：接着改")
        assert "lane_notes" not in tasks.detail_view(tid, admin=False)
