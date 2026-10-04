"""能力自检**内部**出意外必须 fail-closed（2026-10 复核，接 test_capability_pause_reason）。

问题（实测，父会话读 git diff 发现）：run_attempt 外层那条 fail-closed（安全暂停「能力
检查没完成」）是真的，但 `_exec_capability_self_check` 末尾还留着一个
`except Exception: logger.exception("…不挡开工")`，把自检**内部**的意外异常吞掉、
返回空 findings 的 report。于是：

- 计划里第一条要执行的活在 `_try_fill_exec_tool` / `_exec_fill_plan` / criteria 处理
  等处内部 unexpected raise → findings 为空、blocked=False → 外层永远收不到异常 →
  照旧派 Workers（硬 fail-open，白烧 token）；
- 前面几条活已经 filled、后面一条内部炸了，同样被吞成「检查通过」。

既有的 18 个 `test_capability_pause_reason` 用例都是直接 monkeypatch 整个 checker
让它抛错，只验了外层兜底，验不到现实里这条内层吞异常。

修法口径：自检内部意外异常**向上抛**，由 run_attempt 外层既有 fail-closed 兜住
（reason 用真实的「能力检查没完成」，不误说「重排过一次还做不到」）；正常查不到 /
岗位不可用 / 工具没注册 / 名单解析不出来仍由 `_job_effective_tools` 返回空名单 + why，
照旧 blocked 并记 unavailable（诊断不削）。纯审计事件写库失败（_record_exec_event）
不算检查失败，不挡开工。

本文件不 mock 整个 checker：真 Store / 真 Tasks / 真 Agents / 真 Tools / 真 LocalEnv /
真 Specialists，模型用队列桩、Workers 用抓取桩（不调真模型、不碰网络、不碰宿主），
只在 checker **内部**的 `_try_fill_exec_tool` 上让第 1 / 第 2 条活真抛。

不变量（每条用例都点）：Workers 零执行、不扣尝试、一次性 / 专用机器先释放、晚到的
取消不复活、不误说重排过一次、不记 failed、不往群里发「没做成」。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.tasks import Tasks
from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    CaptureWorkers,
    ModelsQueue,
    _job,
    _make_specialists,
    _railway_box,
    _review,
    agents,
    env,
    fixed_clock,
    settings,
    store,
    tools,
)
from test_exec_capability_gate import (  # noqa: F401
    BoomResolveSpecialists,
    FakeRailway,
    _coord,
    _failed_messages,
    _kinds,
    _new_task,
    _plan_json,
    _plan_prompts,
)

pytestmark = pytest.mark.asyncio

_RAISE = "补执行工具内部炸了"


def _paused_report(row: dict) -> dict:
    pr = json.loads(row["paused_reason"])
    assert pr["kind"] == "capability"
    # 真实 reason：内部意外 = 「能力检查没完成」，不是「重排过一次还做不到」
    assert "能力检查没完成" in pr["text"], pr["text"]
    assert "重排" not in pr["text"], "内部意外不许误说成「已经反馈主模型重排过一次」"
    return pr


# ----------------------------------------------------------------------
# 1. 第一条（也是唯一一条）要执行的活：内部 raise → 外层 fail-closed
# ----------------------------------------------------------------------


class TestFirstJobInternalRaise:
    async def test_first_job_raise_pauses_and_releases_remote(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        """`_try_fill_exec_tool` 真抛（不是 mock 整个 checker）：安全暂停，零 Workers。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        box = _railway_box()
        railway = FakeRailway(box)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="railway"),
            _review(pass_=True, artifact=f"artifacts/{tid}/index.html"),  # 只有 fail-open 才会被用掉
        ])
        coord, outbox, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models, railway=railway,
        )

        seen: list[dict] = []

        def _boom(job, *, on_remote, box):  # noqa: ARG001
            seen.append(job)
            raise RuntimeError(_RAISE)

        monkeypatch.setattr(coord, "_try_fill_exec_tool", _boom)

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert seen, "必须真的走到 checker 内部的补工具那一步（内层 catch 就是在这里吞的）"
        row = tasks.get(tid)
        assert row["status"] == "paused", "内部意外 → 安全暂停，不许照常开工"
        assert workers.calls == [], "一个子 agent 都不许跑"
        assert int(row["attempts"]) == 0, "零执行不扣尝试资源"
        _paused_report(row)
        kinds = _kinds(store, tid)
        assert "task.exec_check_incomplete" in kinds
        assert "task.failed" not in kinds
        assert "task.exec_replanned" not in kinds, "内部意外不花重排那次模型调用"
        assert len(_plan_prompts(models)) == 1, "没有再排一次计划"
        assert railway.released == [box], "先释放一次性机器再暂停"
        assert _failed_messages(outbox) == [], "不往群里发「没做成」"

    async def test_internal_raise_does_not_revive_cancelled(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        """内部 raise 时任务已被取消：安全暂停也不许把取消复活成 paused。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["write_file"], agent="task")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        def _cancel_then_boom(job, *, on_remote, box):  # noqa: ARG001
            tasks.transition(tid, "cancelled", reason="用户取消")
            raise RuntimeError(_RAISE)

        monkeypatch.setattr(coord, "_try_fill_exec_tool", _cancel_then_boom)

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "cancelled", "取消优先：不许被安全暂停复活"
        assert "task.paused" not in _kinds(store, tid)
        assert workers.calls == []


# ----------------------------------------------------------------------
# 2. 前面几条活已经 filled，后面一条才内部 raise：照样零 Workers
# ----------------------------------------------------------------------


class TestLaterJobInternalRaiseAfterPartialFill:
    async def test_second_job_raise_after_first_filled_still_zero_workers(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        """第一条活真补上了（filled 事件都记了），第二条才内部 raise → 也不许派 Workers。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[
                _job(["write_file"], agent="task"),
                _job(["write_file"], agent="task"),
            ]),
            _review(pass_=True, artifact=f"artifacts/{tid}/index.html"),  # 只有 fail-open 才会被用掉
        ])
        coord, outbox, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        real = Coordinator._try_fill_exec_tool
        calls: list[dict] = []

        def _fill_then_boom(job, *, on_remote, box):
            calls.append(job)
            if len(calls) >= 2:
                raise RuntimeError("第二条活" + _RAISE)
            return real(coord, job, on_remote=on_remote, box=box)

        monkeypatch.setattr(coord, "_try_fill_exec_tool", _fill_then_boom)

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert len(calls) == 2, "第一条走真补、第二条才抛"
        assert "run_command" in calls[0]["tools"], "第一条活确实被补上了（部分 filled）"
        assert "run_command" not in calls[1]["tools"], "第二条还没轮到补就抛了"
        row = tasks.get(tid)
        assert row["status"] == "paused"
        assert workers.calls == [], "哪怕前面已经 filled，也不许派 Workers"
        assert int(row["attempts"]) == 0
        _paused_report(row)
        kinds = _kinds(store, tid)
        assert "task.exec_autofill" in kinds, "第一条的审计结论保留"
        assert "task.exec_check_incomplete" in kinds
        assert "task.failed" not in kinds
        assert "task.exec_replanned" not in kinds
        assert len(_plan_prompts(models)) == 1
        assert _failed_messages(outbox) == []


# ----------------------------------------------------------------------
# 3. 对照：正常「拿不到」（解析不出来）仍是 blocked 诊断，不升级成 check_incomplete
# ----------------------------------------------------------------------


class TestNormalUnavailableIsStillDiagnosed:
    async def test_resolver_exception_stays_capability_blocked(
        self, store, settings, env, tools, agents
    ):
        """`_job_effective_tools` 自己吞的解析错是**正常诊断**：照旧 unavailable + 重排，
        不许因为上面的整改把它升级成「能力检查没完成」（诊断不能被削）。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = BoomResolveSpecialists(agents)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["write_file"], agent="task")]),
            _plan_json(jobs=[_job(["write_file"], agent="task")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        row = tasks.get(tid)
        assert row["status"] == "paused"
        assert workers.calls == []
        kinds = _kinds(store, tid)
        assert "task.exec_unavailable" in kinds
        assert "task.exec_check_incomplete" not in kinds, "正常解析失败不是「检查没完成」"
        pr = json.loads(row["paused_reason"])
        assert pr["kind"] == "capability"
        assert "重排" in pr["text"], "正常 blocked 仍走「反馈主模型重排过一次」那条话"
        assert "能力检查没完成" not in pr["text"]
        assert len(_plan_prompts(models)) == 2


# ----------------------------------------------------------------------
# 4. 反向探针：内层 catch 必须真的不存在了（异常能冒出来）
# ----------------------------------------------------------------------


class TestCheckerLetsExceptionEscape:
    def test_direct_call_reraises_internal_exception(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        """直接调 checker：内部异常必须抛出去（而不是被吞成空 report）。"""
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=ModelsQueue(),
        )
        plan = {
            "criteria": ["每张图都落到本地"],
            "jobs": [_job(["write_file"], agent="task")],
        }

        def _boom(job, *, on_remote, box):  # noqa: ARG001
            raise RuntimeError(_RAISE)

        monkeypatch.setattr(coord, "_try_fill_exec_tool", _boom)

        with pytest.raises(RuntimeError, match=_RAISE):
            coord._exec_capability_self_check(  # noqa: SLF001
                "T-1", GID, plan, on_remote=False, box=None
            )
