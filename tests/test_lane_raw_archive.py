"""lane 原始历史归档（task_lane_raw）+ 压缩失败保完整历史。

依据 docs/27 §7/§8 P0（原历史与压缩后的工作视图分开存、失败不覆盖原始历史）：

- 原始历史（raw）追加式单独存；压缩只改工作视图（task_lanes.messages），不改 raw；
- 追加规则只认「上一份已落库的工作视图是新的前缀」这一种安全重叠；接不上时把新正文整份
  当新料追加（**不按内容去重**：一模一样的 user / tool 消息重复出现是正常证据）；
- 摘要把工作视图整段换掉时 raw 原样保留（摘要不算原始历史），压缩前那份由
  `original_messages` 显式带过来归档；
- 保存是一条事务里的有条件写入：任务 / 群 / req_version / status + expect_rev 五道闸，
  晚到 / 并发 / 取消 / 改版的一方整体不写，压缩覆盖元数据也不会单独前进；
- 领队压缩失败（Coordinator._lead_begin）/ 干活 lane 升级压缩失败（Coordinator._lane_open）：
  **完整当前工作历史原样接着用**（不退回旧提要、不清空、不从 raw 展开恢复），raw 留档，
  另记 task.lane_compact_failed；装不下交给下游容量闸明确报错，不静默删要求；
- 主模型请求预算走 compaction.context_budget（整包：messages + 工具 schema），压缩走
  compaction.maybe_compact_ex（raw_history_kept=False / require_recoverable=True）。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import clock, requirements
from CharTyr_MaiWork.maiwork.lanes import (
    SUMMARY_PREFIX,
    TaskLanes,
    close_task_lanes,
    raw_merge,
)
from CharTyr_MaiWork.maiwork.store import _MIGRATIONS, Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

GID = "900000001"
OTHER = "111222333"
NOW = 1_790_000_000.0
LATEST = len(_MIGRATIONS)  # 37 = 36（docs/20 task_lanes + brief）+ 1（lane 原始历史归档）

MSGS = [
    {"role": "user", "content": "派的活：整理一页"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"q\": \"x\"}"}},
    ]},
    {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "搜到 3 条"},
]

SUMMARY_MSG = {"role": "user", "content": SUMMARY_PREFIX + "\n\n## 目标\n整理一页"}
NEW_TURN = {"role": "assistant", "content": "第 2 轮补了链接"}
REPEAT_ASK = {"role": "user", "content": "再查一遍"}


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


def _running_task(tasks: Tasks, req: str = "整理一页") -> str:
    tid = tasks.create(GID, title="整理资料", req=req, criteria=["有链接"], source="test")
    if str(tasks.get(tid)["status"]) == "pending_approval":
        tasks.transition(tid, "queued")
    tasks.transition(tid, "running")
    return tid


def _save(lanes: TaskLanes, tid: str, msgs, **kw) -> bool:
    kw.setdefault("req_version", 1)
    return lanes.save(tid, "worker:1", group_id=GID, kind="task", messages=msgs, **kw)


def _raw(lanes: TaskLanes, tid: str, lane: str = "worker:1", gid: str = GID):
    return lanes.load_raw(tid, lane, group_id=gid)


# ---------------------------------------------------------------------------
# 纯函数：追加式合并（只增不减，不按内容去重）
# ---------------------------------------------------------------------------


class TestRawMerge:
    def test_seeds_from_empty(self) -> None:
        merged, appended = raw_merge([], [], MSGS)
        assert merged == MSGS and appended == len(MSGS)

    def test_extends_by_work_view_prefix(self) -> None:
        merged, appended = raw_merge(MSGS, MSGS, [*MSGS, NEW_TURN])
        assert merged == [*MSGS, NEW_TURN] and appended == 1

    def test_repeat_view_is_not_appended_again(self) -> None:
        merged, appended = raw_merge(MSGS, MSGS, MSGS)
        assert merged == MSGS and appended == 0

    def test_summary_view_keeps_raw(self) -> None:
        """工作视图被摘要整段换掉：raw 原样保留，摘要不算原始历史。"""
        merged, appended = raw_merge(MSGS, MSGS, [SUMMARY_MSG])
        assert merged == MSGS and appended == 0
        assert all(SUMMARY_PREFIX not in str(m.get("content") or "") for m in merged)

    def test_original_archived_when_summary_overwrites(self) -> None:
        """压缩把视图整段换掉：original_messages（压缩前那份）对齐归档。"""
        merged, appended = raw_merge(MSGS, MSGS, [SUMMARY_MSG], original=[*MSGS, NEW_TURN])
        assert merged == [*MSGS, NEW_TURN] and appended == 1

    def test_original_seeds_empty_raw(self) -> None:
        merged, appended = raw_merge([], [], [SUMMARY_MSG], original=MSGS)
        assert merged == MSGS and appended == len(MSGS)

    def test_identical_repeated_messages_are_kept(self) -> None:
        """一模一样的 user / tool 消息重复出现是正常证据，不许按内容去重。"""
        base = [*MSGS, REPEAT_ASK]
        merged, appended = raw_merge(base, base, [*base, REPEAT_ASK, REPEAT_ASK])
        assert merged == [*base, REPEAT_ASK, REPEAT_ASK] and appended == 2

    def test_after_summary_new_user_same_as_earlier_is_kept(self) -> None:
        """摘要之后又问了和压缩前同一句：新消息照常进 raw（不当重复丢掉）。"""
        base = [*MSGS, REPEAT_ASK]
        view = [SUMMARY_MSG, REPEAT_ASK]
        merged, appended = raw_merge(base, [SUMMARY_MSG, *base], view)
        assert merged == [*base, REPEAT_ASK] and appended == 1

    def test_fresh_input_without_original_is_appended(self) -> None:
        """接不上又没给 original（换了岗 / 重开）：新正文整份当新料追加，旧的都不丢。"""
        merged, appended = raw_merge(MSGS, [{"role": "user", "content": "别的岗的前情"}], NEW_TURN
                                     if isinstance(NEW_TURN, list) else [NEW_TURN])
        assert merged[: len(MSGS)] == MSGS and appended == 1

    def test_empty_view_never_shrinks_raw(self) -> None:
        merged, appended = raw_merge(MSGS, MSGS, [])
        assert merged == MSGS and appended == 0

    def test_baseline_mismatch_appends_new_body(self) -> None:
        """视图不是上一份的延长（被改写）：整份当新料，raw 不缩。"""
        old = [{"role": "user", "content": "老活"}]
        merged, appended = raw_merge(old, [{"role": "user", "content": "别的视图"}], [NEW_TURN])
        assert merged == [*old, NEW_TURN] and appended == 1


# ---------------------------------------------------------------------------
# 迁移：库号 37 新表 + 从老 task_lanes 回填
# ---------------------------------------------------------------------------


class TestMigration:
    def test_latest_version_is_37(self) -> None:
        assert LATEST == 37, "库号只因 lane 原始历史归档加一步"
        assert _MIGRATIONS[36].__name__ == "_m_task_lane_raw"

    def test_raw_table_exists_with_coverage_columns(self, store: Store) -> None:
        names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_lane_raw" in names
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(task_lane_raw)")}
        assert {
            "task_id", "lane", "group_id", "kind", "req_version", "messages",
            "rev", "covered_count", "covered_rev", "summary", "status", "created", "updated",
        } <= cols
        assert int(store._conn.execute("PRAGMA user_version").fetchone()[0]) == LATEST

    def test_backfills_raw_from_existing_lanes(self, tmp_path) -> None:
        """存量库（库号 36）里的 lane 工作视图 = 原始历史基线，迁移时回填。"""
        s = Store(tmp_path / "stock36.db")
        try:
            for fn in _MIGRATIONS[:36]:
                fn(s.read())
            s.read().execute("PRAGMA user_version=36")
            with s.tx() as conn:
                conn.execute(
                    "INSERT INTO task_lanes (task_id, lane, group_id, kind, model, req_version,"
                    " messages, snapshot, escalated, handoff_id, status, created, updated)"
                    " VALUES ('T-9', 'worker:1', ?, 'task', '', 1, ?, '旧提要', 0, '', 'open', 0, 0)",
                    (GID, json.dumps(MSGS, ensure_ascii=False)),
                )
                conn.execute(
                    "INSERT INTO task_lanes (task_id, lane, group_id, kind, model, req_version,"
                    " messages, snapshot, escalated, handoff_id, status, created, updated)"
                    " VALUES ('T-9', 'worker:2', ?, 'task', '', 1, '[]', '没前情', 0, '', 'closed', 0, 0)",
                    (GID,),
                )
            assert s.migrate() == LATEST
            raw = TaskLanes(s).load_raw("T-9", "worker:1", group_id=GID)
            assert raw is not None and raw["messages"] == MSGS
            assert raw["status"] == "open" and raw["rev"] == 1
            assert raw["summary"] == "旧提要"
            assert raw["group_id"] == GID and raw["covered_count"] == 0
            # 空前情的 lane 不回填（没东西可留）
            assert TaskLanes(s).load_raw("T-9", "worker:2", group_id=GID) is None
        finally:
            s.close()

    def test_backfill_is_idempotent(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork import store as store_mod

        s = Store(tmp_path / "again.db")
        try:
            s.migrate()
            with s.tx() as conn:
                conn.execute(
                    "INSERT INTO task_lanes (task_id, lane, group_id, kind, model, req_version,"
                    " messages, snapshot, escalated, handoff_id, status, created, updated)"
                    " VALUES ('T-9', 'lead', ?, 'main', '', 1, ?, '', 0, '', 'open', 0, 0)",
                    (GID, json.dumps(MSGS, ensure_ascii=False)),
                )
            store_mod._m_task_lane_raw(s.read())
            store_mod._m_task_lane_raw(s.read())
            raw = TaskLanes(s).load_raw("T-9", "lead", group_id=GID)
            assert raw["messages"] == MSGS and raw["rev"] == 1
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 存档写入：追加、覆盖元数据、守卫
# ---------------------------------------------------------------------------


class TestArchive:
    def test_save_populates_raw(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        assert _save(lanes, tid, MSGS)
        raw = _raw(lanes, tid)
        assert raw["messages"] == MSGS and raw["rev"] == 1
        assert raw["status"] == "open" and raw["kind"] == "task"

    def test_repeat_save_does_not_duplicate(self, tasks: Tasks, lanes: TaskLanes) -> None:
        """同一份工作视图反复保存：raw 不涨、版本不动（防无界重复追加）。"""
        tid = _running_task(tasks)
        for _ in range(3):
            _save(lanes, tid, MSGS)
        raw = _raw(lanes, tid)
        assert raw["messages"] == MSGS and raw["rev"] == 1

    def test_prefix_delta_appends_only_new_tail(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        assert _save(lanes, tid, [*MSGS, NEW_TURN])
        raw = _raw(lanes, tid)
        assert raw["messages"] == [*MSGS, NEW_TURN] and raw["rev"] == 2

    def test_repeated_same_question_is_evidence(self, tasks: Tasks, lanes: TaskLanes) -> None:
        """同一句追问连问两次：两条都进原始历史。"""
        tid = _running_task(tasks)
        _save(lanes, tid, [*MSGS, REPEAT_ASK])
        assert _save(lanes, tid, [*MSGS, REPEAT_ASK, REPEAT_ASK])
        assert _raw(lanes, tid)["messages"] == [*MSGS, REPEAT_ASK, REPEAT_ASK]

    def test_summary_overwrite_preserves_raw(self, tasks: Tasks, lanes: TaskLanes) -> None:
        """摘要把工作视图换成一条提要：raw 一条不减。"""
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        assert _save(lanes, tid, MSGS + [NEW_TURN])
        assert _save(lanes, tid, [SUMMARY_MSG])  # 压缩后的工作视图
        assert lanes.load(tid, "worker:1", group_id=GID)["messages"] == [SUMMARY_MSG]
        raw = _raw(lanes, tid)
        assert raw["messages"] == [*MSGS, NEW_TURN] and raw["rev"] == 2, "raw 不被摘要覆盖"

    def test_summary_then_next_round_appends_after_raw(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        _save(lanes, tid, [SUMMARY_MSG])
        assert _save(lanes, tid, [SUMMARY_MSG, NEW_TURN])
        assert _raw(lanes, tid)["messages"] == [*MSGS, NEW_TURN]

    def test_original_messages_archived_in_same_tx(self, tasks: Tasks, lanes: TaskLanes) -> None:
        """压缩把视图整段换掉：压缩前那份原样归档（同一条事务里写）。"""
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        assert _save(
            lanes, tid, [SUMMARY_MSG], original_messages=[*MSGS, NEW_TURN],
            summary="八节提要", covered_count=len(MSGS), covered_rev=1,
        )
        raw = _raw(lanes, tid)
        assert raw["messages"] == [*MSGS, NEW_TURN] and raw["rev"] == 2
        assert raw["summary"] == "八节提要"
        assert raw["covered_count"] == 3 and raw["covered_rev"] == 1
        assert lanes.load(tid, "worker:1", group_id=GID)["messages"] == [SUMMARY_MSG]

    def test_original_messages_seeds_when_no_raw(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        assert _save(lanes, tid, [SUMMARY_MSG], original_messages=MSGS)
        assert _raw(lanes, tid)["messages"] == MSGS

    def test_expect_rev_guard_rejects_stale_writer(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)  # rev = 1
        assert not _save(lanes, tid, [*MSGS, NEW_TURN], expect_rev=7)
        raw = _raw(lanes, tid)
        assert raw["messages"] == MSGS and raw["rev"] == 1, "落后的一版整体拒写"
        assert _save(lanes, tid, [*MSGS, NEW_TURN], expect_rev=1)
        assert _raw(lanes, tid)["rev"] == 2

    def test_expect_rev_zero_means_no_raw_yet(self, tasks: Tasks, lanes: TaskLanes) -> None:
        """读的时候还没有 raw（版本 0）→ 传 expect_rev=0 照常写；传 1 说明行没了，整体拒写。"""
        tid = _running_task(tasks)
        assert not _save(lanes, tid, MSGS, expect_rev=1), "以为有第 1 版、实际没有 → 拒"
        assert _raw(lanes, tid) is None
        assert _save(lanes, tid, MSGS, expect_rev=0), "读到「还没有」→ 允许这版写"
        assert _raw(lanes, tid)["rev"] == 1
        # 被别人清掉（终态/改版的清理、或别的写入者删过）之后，手里那份 1 版不许复活
        with lanes._store.tx() as conn:
            conn.execute("DELETE FROM task_lane_raw WHERE task_id=? AND lane='worker:1'", (tid,))
        assert not _save(lanes, tid, MSGS, expect_rev=1), "行没了 = 实际 0 版 → 拒"
        assert _raw(lanes, tid) is None

    def test_raw_rev_reads_current_version(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        assert lanes.raw_rev(tid, "worker:1", group_id=GID) is None
        _save(lanes, tid, MSGS)
        assert lanes.raw_rev(tid, "worker:1", group_id=GID) == 1
        assert lanes.raw_rev(tid, "worker:1", group_id=OTHER) is None

    def test_raw_isolation_between_tasks_and_groups(self, tasks: Tasks, lanes: TaskLanes) -> None:
        a = _running_task(tasks)
        b = _running_task(tasks, req="另一件事")
        _save(lanes, a, MSGS)
        _save(lanes, b, [{"role": "user", "content": "B 的前情"}])
        assert _raw(lanes, a)["messages"] == MSGS
        assert _raw(lanes, b)["messages"] == [{"role": "user", "content": "B 的前情"}]
        assert _raw(lanes, a, gid=OTHER) is None, "别的群读不到"
        assert lanes.load_raw(a, "worker:2", group_id=GID) is None

    def test_late_save_after_cancel_is_refused(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        tasks.transition(tid, "cancelled")
        assert not _save(lanes, tid, [*MSGS, NEW_TURN], original_messages=[*MSGS, NEW_TURN])
        raw = _raw(lanes, tid)
        assert raw["messages"] == [] and raw["status"] == "closed"

    def test_revise_refuses_old_version_and_clears_worker_raw(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        v = tasks.revise(tid, req="改成两页", criteria=["有链接"])
        raw = _raw(lanes, tid)
        assert raw["messages"] == [] and raw["status"] == "closed", "改版后旧活的前情清空"
        assert not _save(lanes, tid, MSGS, req_version=1), "旧版本的晚到保存不算数"
        assert _save(lanes, tid, MSGS, req_version=v)
        assert _raw(lanes, tid)["messages"] == MSGS


class TestCompactionCoverage:
    def test_coverage_written_with_view_in_one_save(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        _save(lanes, tid, [*MSGS, NEW_TURN])  # rev = 2
        assert _save(lanes, tid, [SUMMARY_MSG], summary="提要", covered_count=2, covered_rev=2)
        raw = _raw(lanes, tid)
        assert raw["summary"] == "提要" and raw["covered_count"] == 2 and raw["covered_rev"] == 2
        assert raw["messages"] == [*MSGS, NEW_TURN], "记覆盖不动 raw"

    def test_coverage_cannot_advance_when_save_refused(self, tasks: Tasks, lanes: TaskLanes) -> None:
        """保存没过闸（终态 / 版本不对 / expect_rev 不匹配）→ 覆盖元数据也不许前进。"""
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        _save(lanes, tid, [SUMMARY_MSG], summary="老提要", covered_count=1, covered_rev=1)
        # 版本不对
        assert not _save(lanes, tid, [SUMMARY_MSG], summary="新提要", covered_count=5,
                         covered_rev=9, req_version=99)
        # expect_rev 不匹配
        assert not _save(lanes, tid, [SUMMARY_MSG], summary="新提要", covered_count=5,
                         covered_rev=9, expect_rev=99)
        # 任务终态
        tasks.transition(tid, "cancelled")
        assert not _save(lanes, tid, [SUMMARY_MSG], summary="新提要", covered_count=5, covered_rev=9)
        raw = _raw(lanes, tid)
        assert raw["summary"] == "老提要" and raw["covered_count"] == 1 and raw["covered_rev"] == 1

    def test_coverage_is_clamped_to_raw(self, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        _save(lanes, tid, [SUMMARY_MSG], summary="提要", covered_count=99, covered_rev=99)
        assert _raw(lanes, tid)["covered_count"] == len(MSGS)

    def test_close_task_lanes_scoped_to_workers(self, store: Store, tasks: Tasks, lanes: TaskLanes) -> None:
        tid = _running_task(tasks)
        _save(lanes, tid, MSGS)
        lanes.save(tid, "lead", group_id=GID, kind="main", messages=MSGS, req_version=1)
        with store.tx() as conn:
            close_task_lanes(conn, tid, lanes="workers")
        assert _raw(lanes, tid)["messages"] == []
        assert _raw(lanes, tid, lane="lead")["messages"] == MSGS, "需求改版只关干活的 lane"

    def test_close_does_not_touch_other_task(self, store: Store, tasks: Tasks, lanes: TaskLanes) -> None:
        a = _running_task(tasks)
        b = _running_task(tasks, req="另一件事")
        _save(lanes, a, MSGS)
        _save(lanes, b, MSGS)
        with store.tx() as conn:
            close_task_lanes(conn, a)
        assert _raw(lanes, a)["messages"] == []
        assert _raw(lanes, b)["messages"] == MSGS


# ---------------------------------------------------------------------------
# coordinator：压缩失败保完整历史 / 成功记覆盖 / expect_rev / 预算透传
# ---------------------------------------------------------------------------

from tests.test_coordinator import (  # noqa: E402  (fixtures)
    _create_task,
    env,  # noqa: F401
    goals,  # noqa: F401
    mem_store,  # noqa: F401
    settings,  # noqa: F401
    tasks as _coord_tasks,  # noqa: F401
    tools,  # noqa: F401
)
from tests.test_coordinator_lanes import (  # noqa: E402
    BAD,
    GOOD,
    TEXT_PLAN,
    LaneModels,
    _kinds,
    _setup,
)


def _lane_kinds(store, tid):
    return [k for k, _ in _kinds(store, tid)]



def _raw_trace(monkeypatch, store):
    """每次 lane 保存后记一份原始历史快照（终态会把 raw 清空，所以看轨迹而不是看跑完那一刻）。"""
    orig = TaskLanes.save
    trace: list[tuple[str, list[dict] | None]] = []

    def spy(self, task_id, lane, **kw):
        out = orig(self, task_id, lane, **kw)
        row = self.load_raw(task_id, lane, group_id=kw["group_id"])
        trace.append((lane, [dict(m) for m in row["messages"]] if row else None))
        return out

    monkeypatch.setattr(TaskLanes, "save", spy)
    return trace


def m_store_covered(store, tid, lane):
    """跑完之后看覆盖元数据（终态清空的是正文，行和提要字段还在）。"""
    return TaskLanes(store).load_raw(tid, lane, group_id=GID) or {}


def _last(trace, lane):
    got = [msgs for name, msgs in trace if name == lane and msgs is not None]
    return got[-1] if got else None


@pytest.mark.asyncio
async def test_lead_compact_failure_keeps_full_history(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    """领队前情压缩没做成：完整当前历史接着用（不是旧提要、不是空），raw 留档，另记事件。"""
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 50)
    tid = _create_task(_coord_tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD], compact_fail=True)
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    trace = _raw_trace(monkeypatch, mem_store)
    await coord.run_task(tid)
    assert _coord_tasks.get(tid)["status"] == "completed", "压不下去不卡任务"
    plans = [c for c in models.calls if c[2].get("purpose") == "coordinator.plan"]
    assert len(plans) == 2
    earlier = plans[1][1][:-1]
    assert earlier, "没有退回「从零开始」"
    text = json.dumps(earlier, ensure_ascii=False)
    assert "查资料做一页" in text and "缺下载链接" in text, "上一轮排的计划和验收意见都在"
    kinds = _lane_kinds(mem_store, tid)
    assert "task.lane_compact_failed" in kinds
    assert "task.lane_compact" not in kinds
    assert "task.lane_reset" not in kinds, "失败不 reset lane"
    raw_msgs = _last(trace, "lead")
    assert raw_msgs, "原始历史留档"
    assert len(raw_msgs) >= len(earlier)
    failed = m_store_covered(mem_store, tid, "lead")
    assert failed["summary"] == "" and failed["covered_count"] == 0, "失败不许写提要或推进覆盖"


@pytest.mark.asyncio
async def test_lead_compact_failure_ignores_stale_snapshot(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    """有旧提要也不拿它顶掉完整历史，也不从 raw 展开补料：就用手里的完整当前历史。"""
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 50)
    tid = _create_task(_coord_tasks)
    TaskLanes(mem_store).save(
        tid, "lead", group_id=GID, kind="main", messages=MSGS, req_version=1,
        snapshot="旧提要：第一版缺链接",
    )
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD], compact_fail=True)
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    trace = _raw_trace(monkeypatch, mem_store)
    await coord.run_task(tid)
    plans = [c for c in models.calls if c[2].get("purpose") == "coordinator.plan"]
    earlier = json.dumps(plans[0][1][:-1], ensure_ascii=False)
    assert "搜到 3 条" in earlier, "完整当前历史接着用"
    assert "旧提要：第一版缺链接" not in earlier, "不退回旧提要"
    raw_msgs = _last(trace, "lead")
    assert raw_msgs[: len(MSGS)] == MSGS
    raw = TaskLanes(mem_store).load_raw(tid, "lead", group_id=GID)
    assert raw["summary"] == "", "失败不许写提要"


@pytest.mark.asyncio
async def test_lead_compact_success_marks_coverage(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 50)
    tid = _create_task(_coord_tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    trace = _raw_trace(monkeypatch, mem_store)
    await coord.run_task(tid)
    raw_msgs = _last(trace, "lead")
    assert raw_msgs and "查资料做一页" in json.dumps(raw_msgs, ensure_ascii=False), "被摘要掉的那段留在 raw"
    covered = m_store_covered(mem_store, tid, "lead")
    assert covered["summary"], "压缩成功记提要"
    assert covered["covered_count"] >= 1 and covered["covered_rev"] >= 1


@pytest.mark.asyncio
async def test_worker_escalation_compact_failure_keeps_full_history(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    """第 2 次没过换模型前压缩失败：干活 lane 的完整前情接着用，不 reset 成空。"""
    tid = _create_task(_coord_tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, BAD, TEXT_PLAN, GOOD], compact_fail=True)
    coord, spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    trace = _raw_trace(monkeypatch, mem_store)
    await coord.run_task(tid)
    assert len(spec.calls) == 3 and spec.calls[2]["escalate"] is True
    third = spec.calls[2]["history"]
    assert third, "不是从零开始"
    text = json.dumps(third, ensure_ascii=False)
    assert "第 1 轮做完了" in text and "第 2 轮做完了" in text, "两轮前情都在"
    kinds = _lane_kinds(mem_store, tid)
    assert "task.lane_compact_failed" in kinds
    assert not [p for k, p in _kinds(mem_store, tid) if k == "task.lane_reset"], "失败不 reset lane"
    assert len(_last(trace, "worker:1") or []) >= 4, "两轮原始历史都留档"
    failed = m_store_covered(mem_store, tid, "worker:1")
    assert failed["summary"] == "" and failed["covered_count"] == 0, "失败不许写提要或推进覆盖"


@pytest.mark.asyncio
async def test_worker_escalation_compact_success_marks_coverage(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 20)
    tid = _create_task(_coord_tasks)
    # 前情要真超保留预算（保留预算是 1024 token 起）才会切；用一条长说明撑起来
    long_plan = json.dumps(
        {"criteria": ["包含链接"], "deliver_kind": "text",
         "jobs": [{"brief": "查资料做一页" * 400, "tools": ["web_search"]}], "question": None},
        ensure_ascii=False,
    )
    models = LaneModels([long_plan, BAD, long_plan, BAD, long_plan, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    trace = _raw_trace(monkeypatch, mem_store)
    await coord.run_task(tid)
    assert spec.calls[2]["escalate"] is True
    third = spec.calls[2]["history"]
    assert any("试过不行的" in str(m.get("content") or "") for m in third), "换模型前先压成提要"
    assert "第 1 轮做完了" in json.dumps(_last(trace, "worker:1"), ensure_ascii=False), "第 1 轮原始历史还在"
    covered = m_store_covered(mem_store, tid, "worker:1")
    assert covered["summary"] and covered["covered_count"] >= 1 and covered["covered_rev"] >= 1


@pytest.mark.asyncio
async def test_worker_round_appends_raw_tail(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    """返工接着改：raw 追加这一轮的新话，不重复老话。"""
    tid = _create_task(_coord_tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    trace = _raw_trace(monkeypatch, mem_store)
    await coord.run_task(tid)
    snapshots = [msgs for lane, msgs in trace if lane == "worker:1" and msgs]
    assert snapshots, "原始历史有留档"
    text = json.dumps(snapshots[-1], ensure_ascii=False)
    assert text.count("第 1 轮做完了") == 1 and text.count("第 2 轮做完了") == 1, "不重复追加"


@pytest.mark.asyncio
async def test_coordinator_save_pins_loaded_raw_rev(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    """coordinator 自动把「读这条 lane 时看到的原始历史版本」当 expect_rev 传回去。"""
    tid = _create_task(_coord_tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    orig = TaskLanes.save
    seen: list[dict] = []

    def spy(self, task_id, lane, **kw):
        seen.append({"lane": lane, "expect_rev": kw.get("expect_rev")})
        return orig(self, task_id, lane, **kw)

    monkeypatch.setattr(TaskLanes, "save", spy)
    await coord.run_task(tid)
    worker = [s["expect_rev"] for s in seen if s["lane"] == "worker:1"]
    assert worker and worker[0] is None, "第一条活时还没有原始历史，不传 expect_rev"
    assert worker[1] == 1, "第二轮带着读到的版本回去"
    lead = [s["expect_rev"] for s in seen if s["lane"] == "lead"]
    assert any(v is not None for v in lead), "领队保存也带上读到的版本"


@pytest.mark.asyncio
async def test_stale_writer_cannot_clobber_newer_raw(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    """读完之后被别的写入者推进过原始历史 → 这一版整体不写，新的那份留着。"""
    tid = _create_task(_coord_tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    orig = TaskLanes.save
    state = {"n": 0, "results": [], "raw": []}
    foreign = [{"role": "user", "content": "别的一轮写的"}]

    def racing(self, task_id, lane, **kw):
        if lane == "worker:1":
            state["n"] += 1
            if state["n"] == 2:  # 第二轮保存前，另一个写入者抢先推进了一版
                with self._store.tx() as conn:
                    conn.execute(
                        "UPDATE task_lane_raw SET rev=rev+1, messages=? WHERE task_id=? AND lane=?"
                        " AND group_id=?",
                        (json.dumps(foreign, ensure_ascii=False), task_id, lane, GID),
                    )
        out = orig(self, task_id, lane, **kw)
        state["results"].append((lane, out))
        row = self.load_raw(task_id, lane, group_id=kw["group_id"])
        state["raw"].append((lane, [dict(m) for m in row["messages"]] if row else None))
        return out

    monkeypatch.setattr(TaskLanes, "save", racing)
    await coord.run_task(tid)
    worker = [out for lane, out in state["results"] if lane == "worker:1"]
    assert worker and worker[0] is True
    assert worker[1] is False, "落后的那一版被拒（不许覆盖新的原始历史）"
    snaps = [msgs for lane, msgs in state["raw"] if lane == "worker:1"]
    assert snaps[-1] == foreign, "新的那份原始历史留着（落后的一版被拒后没被覆盖）"


# ---------------------------------------------------------------------------
# 主模型请求预算：整包（messages + 工具 schema）→ context_budget → maybe_compact_ex
# ---------------------------------------------------------------------------


class _FakeSettingsModels:
    context_window = 8192
    max_tokens = 2048


class _FakeSettings:
    models = _FakeSettingsModels()


class BudgetModels:
    """公开的假 Models：实现整包预算 + 固定 chat 返回（别的测试文件也可以直接用）。

    - `context_budget_calls`：compaction.context_budget 收到的东西由此断言；
    - `sent`：真正发给模型的 messages（用来验压缩结果是它发的）。
    """

    def __init__(self, *, window: int = 128000, max_tokens: int = 32768) -> None:
        self.window = window
        self.max_tokens = max_tokens
        self.budget_kinds: list[str] = []
        self.sent: list[list[dict]] = []

    def settings(self):
        return type("S", (), {"ready": lambda: True, "context_window": 8192})()

    def limits_for(self, kind=None, *, escalate=False):
        return {"context_window": self.window, "max_tokens": self.max_tokens}

    async def chat(self, role=None, messages=None, **kw):
        from CharTyr_MaiWork.maiwork.models import ChatResult

        self.sent.append([dict(m) for m in messages])
        return ChatResult(text="{}", tool_calls=[], model="m",
                          prompt_tokens=1, completion_tokens=1, raw_message={})


def _budget_coord(models):
    import pathlib
    import tempfile

    from CharTyr_MaiWork.maiwork.coordinator import Coordinator

    store = Store(pathlib.Path(tempfile.mkdtemp()) / "b.db")
    store.migrate()
    return Coordinator(
        store=store, models=models, workers=None, tools=None,
        tasks=Tasks(store, lambda: _FakeSettings()), goals=None, delivery=None,
        outbox=None, env=None, profiles=None, get_settings=lambda: _FakeSettings(),
    )


class _Outcome:
    def __init__(self, messages) -> None:
        self.messages = messages
        self.action = "none"
        self.observations: list = []
        self.coverage = None
        self.failure = None


def _install_compact(monkeypatch, *, view=None):
    """装一份 compaction.context_budget + maybe_compact_ex（另一路要落的同一套 API）。"""
    from CharTyr_MaiWork.maiwork import compaction

    calls: dict[str, list] = {"budget": [], "ex": []}

    def fake_budget(models, role=None, agent=None, escalate=False, messages=None, tools=None,
                    max_tokens=None, context_window=None, json_mode=False):
        calls["budget"].append({
            "role": role, "agent": agent, "messages": messages, "tools": tools,
            "json_mode": json_mode,
        })
        return {"context_window": 256000, "output_reserve": 32768,
                "estimated_input_tokens": 1234, "trigger_threshold": 190000}

    async def fake_ex(messages, **kw):
        calls["ex"].append({"messages": messages, **kw})
        return _Outcome(list(view) if view is not None else list(messages))

    monkeypatch.setattr(compaction, "context_budget", fake_budget)
    monkeypatch.setattr(compaction, "maybe_compact_ex", fake_ex, raising=False)
    return calls


@pytest.mark.asyncio
async def test_chat_main_budget_covers_full_request(monkeypatch):
    """整包预算：messages + 工具 schema 一起给 context_budget，压缩用它的 window/output_reserve。"""
    tools_schema = [{"type": "function", "function": {"name": "list_files"}}]
    calls = _install_compact(monkeypatch)
    models = BudgetModels()
    coord = _budget_coord(models)
    messages = [{"role": "user", "content": "hi"}]
    await coord._chat_main(messages, purpose="coordinator.plan", tools=tools_schema)
    assert calls["budget"][0]["messages"] == messages, "整包：消息进预算"
    assert calls["budget"][0]["tools"] == tools_schema, "整包：工具 schema 进预算"
    assert calls["budget"][0]["role"] == "main" and calls["budget"][0]["agent"] == "main"
    assert calls["ex"][0]["context_window"] == 256000
    assert calls["ex"][0]["output_reserve"] == 32768
    assert calls["ex"][0]["tools"] == tools_schema
    assert calls["ex"][0]["require_recoverable"] is True, "裁剪必须可回读"
    assert calls["ex"][0].get("json_mode") is False


@pytest.mark.asyncio
async def test_chat_main_uses_compacted_view(monkeypatch):
    tools_schema = [{"type": "function", "function": {"name": "list_files"}}]
    compacted = [{"role": "user", "content": "压缩后的视图"}]
    _install_compact(monkeypatch, view=compacted)
    models = BudgetModels()
    coord = _budget_coord(models)
    await coord._chat_main([{"role": "user", "content": "hi"}], purpose="coordinator.plan",
                           tools=tools_schema)
    assert models.sent and models.sent[0] == compacted, "发给模型的是压缩后的视图"


@pytest.mark.asyncio
async def test_chat_main_survives_missing_compact_api(monkeypatch):
    """压缩这一路炸了（API 还没落地 / 预算算不出来）：原样继续，不删要求、不换假摘要。"""
    from CharTyr_MaiWork.maiwork import compaction

    monkeypatch.delattr(compaction, "context_budget", raising=False)
    monkeypatch.delattr(compaction, "maybe_compact_ex", raising=False)
    models = BudgetModels()
    coord = _budget_coord(models)
    messages = [{"role": "user", "content": "第一问"}, {"role": "assistant", "content": "回答"}]
    await coord._chat_main(messages, purpose="t", json_mode=False)
    assert models.sent and models.sent[0] == messages


# ---------------------------------------------------------------------------
# 锁定需求清单：每轮由代码重注入（不做事后抽取），pin_message 在就钉住
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locked_requirements_message_is_pinned(
    mem_store, settings, env, tools, _coord_tasks, goals, monkeypatch
):
    from CharTyr_MaiWork.maiwork import compaction as comp

    pinned: list[dict] = []

    def fake_pin(msg):
        pinned.append(msg)
        return {**msg, "pinned": True}

    monkeypatch.setattr(comp, "pin_message", fake_pin, raising=False)
    tid = _create_task(_coord_tasks)
    requirements.save(mem_store, tid, 1, [{"text": "包含链接", "origin": "原话", "kind": "实做"}])
    models = LaneModels([TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    await coord.run_task(tid)
    assert pinned, "钉住了需求清单消息"
    assert "包含链接" in pinned[0]["content"]
    plans = [c for c in models.calls if c[2].get("purpose") == "coordinator.plan"]
    assert any(m.get("pinned") for m in plans[0][1]), "钉住的消息进了请求"


@pytest.mark.asyncio
async def test_locked_requirements_reinjected_every_round(
    mem_store, settings, env, tools, _coord_tasks, goals
):
    """锁定清单每轮由代码重注入并钉住（不是从摘要里事后抽出来的）。"""
    tid = _create_task(_coord_tasks, req="做一页")
    task = _coord_tasks.get(tid)
    locked = requirements.normalize_requirements(
        [{"text": "包含链接", "origin": "原话", "kind": "实做"}], task["req"]
    )
    requirements.save(mem_store, tid, int(task.get("req_version") or 1), locked)
    models = LaneModels([TEXT_PLAN, TEXT_PLAN])
    coord, _spec = _setup(mem_store, settings, env, tools, _coord_tasks, goals, models)
    await coord._plan(task, lead=True)
    await coord._plan(_coord_tasks.get(tid), lead=True)
    assert len(models.calls) == 2
    for call in models.calls:
        msgs = call[1]
        pinned = [m for m in msgs if m.get("maiwork_pinned")]
        assert pinned and "包含链接" in pinned[0]["content"], "每轮都重注入钉住的需求清单"
        assert "包含链接" in msgs[-1]["content"], "提示词里照旧列出清单"
        assert not any(SUMMARY_PREFIX in str(m.get("content") or "") for m in pinned)


# ---------------------------------------------------------------------------
# 主模型工具输出的归档（docs/27 §7/§8 P1）：6001 / 49999 / 报错 / 归档失败都不许丢中段
# ---------------------------------------------------------------------------


class _BigTools:
    """假工具层：call 返回一段指定正文（可造成功 / 报错）。"""

    def __init__(self, output: str = "", *, ok: bool = True, error: str = "") -> None:
        self.output = output
        self.ok = ok
        self.error = error
        self.calls: list[str] = []

    async def call(self, name, args, ctx):
        from CharTyr_MaiWork.maiwork.tools import ToolResult

        self.calls.append(name)
        if self.ok:
            return ToolResult(ok=True, output=self.output)
        return ToolResult(ok=False, output="", error=self.error or self.output)


def _tool_ctx(workspace, *, reader: bool = True):
    from CharTyr_MaiWork.maiwork.tools import ToolContext

    return ToolContext(
        group_id=GID, task_id="T-1", actor="主模型", workspace=workspace, role="main",
        allowed_tools=("inspect_file", "list_files") if reader else ("list_files",),
    )


def _tool_call(name="fetch_page", call_id="c1"):
    return [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]


async def _run_one_tool(coord, tools_fake, ctx, messages=None):
    coord._tools = tools_fake
    msgs = messages if messages is not None else []
    await coord._run_tool_calls(msgs, _tool_call(), ctx)
    return msgs[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [6001, 49999])
async def test_big_tool_output_archived_with_relative_pointer(tmp_path, size):
    """6001 / 49999 字的主模型工具正文：完整归档到工作区，消息里留相对路径指针。"""
    body = "汉" * size
    coord = _budget_coord(BudgetModels())
    msg = await _run_one_tool(coord, _BigTools(body), _tool_ctx(tmp_path))
    content = str(msg["content"])
    assert "已截断" not in content, "不许再硬截"
    rel = "tool_spill/T-1"
    assert rel in content and "inspect_file" in content, "给的是工作区相对路径 + 回读指引"
    files = list((tmp_path / rel).glob("spill-*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == body, "归档文件是完整正文（不过期、不淘汰）"


@pytest.mark.asyncio
async def test_error_tool_output_is_archived_too(tmp_path):
    body = "错" * 6001
    coord = _budget_coord(BudgetModels())
    msg = await _run_one_tool(coord, _BigTools(ok=False, error=body), _tool_ctx(tmp_path))
    assert "已截断" not in str(msg["content"])
    files = list((tmp_path / "tool_spill" / "T-1").glob("spill-*.txt"))
    assert files and files[0].read_text(encoding="utf-8") == body, "报错也要留档"


@pytest.mark.asyncio
async def test_archive_failure_preserves_full_body(tmp_path):
    """归档没做成（这里：目录位被一个文件占住）→ 完整正文照发，不硬截、不假装能读回。"""
    (tmp_path / "tool_spill").write_text("占位", encoding="utf-8")
    body = "长" * 6001
    coord = _budget_coord(BudgetModels())
    msg = await _run_one_tool(coord, _BigTools(body), _tool_ctx(tmp_path))
    content = str(msg["content"])
    assert content == body, "完整正文原样保留（交给预算闸判）"
    assert "已截断" not in content and "归档" not in content


@pytest.mark.asyncio
async def test_no_reader_tool_keeps_full_body(tmp_path):
    """这一轮没有回读工具 → 不归档也不硬截（宁可让预算闸明确失败，也不丢中段）。"""
    body = "长" * 6001
    coord = _budget_coord(BudgetModels())
    msg = await _run_one_tool(coord, _BigTools(body), _tool_ctx(tmp_path, reader=False))
    assert str(msg["content"]) == body
    assert not (tmp_path / "tool_spill").exists(), "没有回读工具就不写归档"


@pytest.mark.asyncio
async def test_relative_workspace_is_refused(tmp_path, monkeypatch):
    """工作区是相对路径（锚不住）→ 不归档、完整正文照发（不 resolve 把软链接跟到底）。"""
    body = "长" * 6001
    coord = _budget_coord(BudgetModels())
    monkeypatch.chdir(tmp_path)
    msg = await _run_one_tool(coord, _BigTools(body), _tool_ctx("rel-ws"))
    assert str(msg["content"]) == body
    assert not (tmp_path / "rel-ws").exists()


@pytest.mark.asyncio
async def test_default_workspace_outside_or_missing_is_full_body(tmp_path):
    """没有工作区（None）→ 完整正文照发，一个 6000 字硬截都不许有。"""
    body = "长" * 6001
    coord = _budget_coord(BudgetModels())
    msg = await _run_one_tool(coord, _BigTools(body), _tool_ctx(None))
    assert str(msg["content"]) == body


@pytest.mark.asyncio
async def test_symlinked_spill_dir_is_refused(tmp_path):
    """spill 目录是符号链接（想写到工作区外）→ 不归档、完整正文照发。"""
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "tool_spill").symlink_to(outside, target_is_directory=True)
    body = "长" * 6001
    coord = _budget_coord(BudgetModels())
    msg = await _run_one_tool(coord, _BigTools(body), _tool_ctx(ws))
    assert str(msg["content"]) == body
    assert not list(outside.glob("spill-*.txt")), "不许顺着软链接写到工作区外"
