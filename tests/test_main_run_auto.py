"""`main_run.auto(...)` 的规则逐条守住（docs/33 §2.3）。

models.chat 只剩一句 `async with main_run.auto(...)`；「什么时候自动记一件」
的判定整体在 `main_run.auto` 里，每一条都有专门测试：

- 记一条：有效岗位 main、task_id 空、非核验调用、purpose 非 session.triage、
  `run_ref.current()` 空；
- 不记：agent 不是 main（专岗）、task_id 非空（T-* 任务里）、核验调用
  （`_candidates`）、session.triage、已经在任何一轮里（显式 scope / tagged 轮）；
- 写库任何一步失败（begin / finish / 复位）只打日志，调用照跑；
- 取消（CancelledError）：条目标 cancelled 后异常照常往外抛（不吞）。

这些都是纯 `main_run.auto` 的单元测试（不用真 Models）：进 scope、看
`run_ref.current()` / `main_runs` 行、看异常行为。
"""

from __future__ import annotations

import asyncio

import pytest

from CharTyr_MaiWork.maiwork import main_run, run_ref
from CharTyr_MaiWork.maiwork.store import Store


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    return store


def _runs(store: Store) -> list[dict]:
    rows = store.read().execute("SELECT * FROM main_runs ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


class TestAutoRules:
    """逐条守「什么时候记一件」。"""

    @pytest.mark.asyncio
    async def test_main_without_task_opens(self, tmp_path) -> None:
        store = _store(tmp_path)
        async with main_run.auto(store, agent="main", purpose="admin_chat",
                                 group_id="900000001"):
            assert run_ref.current().startswith("main:")
        assert [r["status"] for r in _runs(store)] == ["done"]

    @pytest.mark.asyncio
    async def test_role_main_fallback_opens(self, tmp_path) -> None:
        """agent 没给、role=main：按 main 算（和 chat 的口径一致）。"""
        store = _store(tmp_path)
        async with main_run.auto(store, agent="", role="main", purpose="x"):
            assert run_ref.current().startswith("main:")
        assert len(_runs(store)) == 1

    @pytest.mark.asyncio
    async def test_worker_agent_no_run(self, tmp_path) -> None:
        store = _store(tmp_path)
        async with main_run.auto(store, agent="task", purpose="worker",
                                 group_id="900000001"):
            assert run_ref.current() == ""
        assert _runs(store) == []

    @pytest.mark.asyncio
    async def test_task_id_no_run(self, tmp_path) -> None:
        """T-* 任务里的调用：条目留在任务栏，不开主模型条目。"""
        store = _store(tmp_path)
        async with main_run.auto(store, agent="main", task_id="T-1",
                                 purpose="coordinator.plan", group_id="900000001"):
            assert run_ref.current() == ""
        assert _runs(store) == []

    @pytest.mark.asyncio
    async def test_verifying_no_run(self, tmp_path) -> None:
        """模型库核验（_candidates 调用）不开条目。"""
        store = _store(tmp_path)
        async with main_run.auto(store, agent="main", purpose="verify",
                                 group_id="900000001", verifying=True):
            assert run_ref.current() == ""
        assert _runs(store) == []

    @pytest.mark.asyncio
    async def test_session_triage_no_run(self, tmp_path) -> None:
        """session.triage 是热重载期自己的短事务，不开条目。"""
        store = _store(tmp_path)
        async with main_run.auto(store, agent="main", purpose="session.triage",
                                 group_id="900000001"):
            assert run_ref.current() == ""
        assert _runs(store) == []

    @pytest.mark.asyncio
    async def test_inside_explicit_scope_no_run(self, tmp_path) -> None:
        """显式 scope 里（run_ref 已挂）：auto 不重复开，外面那条照旧。"""
        store = _store(tmp_path)
        async with main_run.MainRuns(store).scope(group_id="g", purpose="admin_chat"):
            outer = run_ref.current()
            async with main_run.auto(store, agent="main", purpose="admin_chat",
                                     group_id="g"):
                assert run_ref.current() == outer  # 还是外面那条的 ref
        runs = _runs(store)
        assert len(runs) == 1
        assert runs[0]["ref"] == outer

    @pytest.mark.asyncio
    async def test_inside_tagged_round_no_run(self, tmp_path) -> None:
        """tagged 轮（news:/idea:/topic:）里 main 的调用也不开条目。"""
        store = _store(tmp_path)
        async with run_ref.tagged(run_ref.new("topic", "900000001")):
            async with main_run.auto(store, agent="main", purpose="opener",
                                     group_id="900000001"):
                assert run_ref.current().startswith("topic:")
        assert _runs(store) == []


class TestAutoFailure:
    """写库失败只打日志，调用照跑。"""

    @pytest.mark.asyncio
    async def test_begin_failure_call_proceeds(self, tmp_path) -> None:
        store = _store(tmp_path)
        # 表形状弄坏：_begin 会抛（NOT NULL / 列缺），auto 吞掉当没开
        store.read().execute("DROP TABLE main_runs")
        store.read().execute("CREATE TABLE main_runs (id INTEGER PRIMARY KEY, bogus TEXT)")
        async with main_run.auto(store, agent="main", purpose="admin_chat",
                                 group_id="g") as scope:
            assert scope.ref == ""
            assert run_ref.current() == ""
        assert _runs(store) == []

    @pytest.mark.asyncio
    async def test_finish_failure_call_proceeds(self, tmp_path) -> None:
        """begin 成了、finish 写不进去：调用照常返回。"""
        store = _store(tmp_path)
        runner = main_run.MainRuns(store)
        broken = {"failed": False}
        orig_finish = runner._finish

        def _bad_finish(ref, status, error=""):
            if not broken["failed"]:
                broken["failed"] = True
                raise RuntimeError("finish 炸了")
            return orig_finish(ref, status, error)

        runner._finish = _bad_finish  # type: ignore[method-assign]
        scope = runner.scope(group_id="g", purpose="admin_chat")
        scope._runs = runner
        auto_scope = main_run._AutoScope(scope)
        async with auto_scope:
            assert run_ref.current().startswith("main:")
        # 调用方看不到异常；条目停在 running（收尾没写成，正常路径等 close_orphans）
        assert _runs(store)[0]["status"] == "running"


class TestAutoCancel:
    """CancelledError：条目标 cancelled，异常照常往外抛。"""

    @pytest.mark.asyncio
    async def test_cancelled_marks_cancelled_and_reraises(self, tmp_path) -> None:
        store = _store(tmp_path)

        async def _job() -> None:
            async with main_run.auto(store, agent="main", purpose="admin_chat",
                                     group_id="g"):
                await asyncio.sleep(60)

        task = asyncio.create_task(_job())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        runs = _runs(store)
        assert len(runs) == 1
        assert runs[0]["status"] == "cancelled"
        assert "取消" in runs[0]["error"]

    @pytest.mark.asyncio
    async def test_ref_cleared_after_scope(self, tmp_path) -> None:
        """出 scope 后 CURRENT_REF 复位（不污染后面的调用）。"""
        store = _store(tmp_path)
        async with main_run.auto(store, agent="main", purpose="admin_chat", group_id="g"):
            assert run_ref.current().startswith("main:")
        assert run_ref.current() == ""


class TestTagged:
    """run_ref.tagged：只打标不开条目。"""

    @pytest.mark.asyncio
    async def test_tagged_sets_and_restores(self, tmp_path) -> None:
        ref = run_ref.new("news", "900000001")
        assert ref.startswith("news:900000001:")
        async with run_ref.tagged(ref):
            assert run_ref.current() == ref
        assert run_ref.current() == ""

    @pytest.mark.asyncio
    async def test_tagged_empty_ref_is_noop(self, tmp_path) -> None:
        """ref 空（造标记失败路径）：scope 照样进出，不设标记。"""
        async with run_ref.tagged(""):
            assert run_ref.current() == ""
