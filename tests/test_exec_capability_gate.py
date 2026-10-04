"""开工前能力闸：有界重排一次 → 还是做不到就暂停（2026-10 复核收口）。

背景（docs/18 §六 101，用户口径「开工前对不上先补工具或改标准」）：主会话复核发现，
`_exec_capability_self_check` 修好「假 autofill」之后**仍然只是往时间线写一条
`task.exec_unavailable` 就照常开工**——活照派、token 照烧，等于没改。最终口径：

1. 排计划提示里先让主模型知道每个岗位**实际**能拿到的工具和环境（和真跑活同一份解析），
   第一次计划就尽量别把「要下载落盘 / 跑命令」的活派给只读岗；
2. 开工前自检发现「这条活要下载/本地存文件/跑命令，但子 agent 实际拿不到执行工具」时，
   把说明反馈给主模型，**只重排一次**计划（只许改 jobs：改派岗位或改做法）；
3. 重排后**再查一次**；还是拿不到 → **暂停**（不是失败）：Workers 零执行、明确 reason、
   不扣最多 3 次的尝试资源、已经拿到的一次性机器必须释放、晚到的取消/终态不复活；
4. 重排有上限（`_CAPABILITY_REPLAN_LIMIT = 1`），全流程不会无限重 plan；
   验收不给结论仍是既有的「退回重跑」（见 test_effective_capability_review_fixes.py）。

覆盖（用户点的项）：真 news 岗位上限压掉执行工具 → 重排成 task → 成功；改计划仍不够 →
paused + 零 Workers + 不扣尝试资源；重排期间取消不复活；不越权放大岗位；不降低用户
criteria；disabled / 注册缺失 / 解析异常（含远端换名）一律 fail-closed；railway 机器
暂停前释放；排计划提示带岗位实际工具；远端只有 vm_put_file 不算执行能力。

用真 Store / 真 Agents / 真 Tools / 真 LocalEnv / 真 Tasks，模型用队列桩、Workers 用抓取桩，
不调真模型、不碰网络、不碰宿主。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.tasks import Tasks
from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    CaptureWorkers,
    FakeDelivery,
    FakeOutbox,
    FakeProfiles,
    ModelsQueue,
    _add_worker_tool,
    _events,
    _job,
    _kinds,
    _make_specialists,
    _note_for,
    _railway_box,
    _review,
    _ssh_box,
    _write_artifact,
    agents,
    env,
    fixed_clock,
    settings,
    store,
    tools,
)

pytestmark = pytest.mark.asyncio

CRITERIA = ["每张图都落到本地，附出处链接"]


def _plan_json(*, jobs, criteria=None, deliver_kind="view", env=None, question=None) -> str:
    """计划 JSON（比共用 _plan 多一个 env 字段，用来指定跑在哪台机器上）。"""
    data = {
        "criteria": list(criteria if criteria is not None else CRITERIA),
        "deliver_kind": deliver_kind,
        "jobs": jobs,
        "question": question,
    }
    if env:
        data["env"] = env
    return json.dumps(data, ensure_ascii=False)


def _coord(
    store, settings, env, tools, agents, workers, *,
    specialists=None, models=None, railway=None, ssh=None,
):
    """和共用 _coordinator 一样，但把 outbox / delivery 也交回给用例检查。"""
    outbox, delivery = FakeOutbox(), FakeDelivery()
    coord = Coordinator(
        store, models, workers, tools, Tasks(store, lambda: settings),
        Goals(store, lambda: settings), delivery, outbox, env, FakeProfiles(), lambda: settings,
        capability=None, railway=railway, ssh=ssh,
    )
    coord._specialists = specialists  # noqa: SLF001
    return coord, outbox, delivery


def _plan_prompts(models: ModelsQueue) -> list[str]:
    return [
        "\n".join(m.get("content") or "" for m in msgs if isinstance(m.get("content"), str))
        for _role, msgs, kw in models.calls
        if str(kw.get("purpose") or "") == "coordinator.plan"
    ]


def _failed_messages(outbox: FakeOutbox) -> list[str]:
    return [
        str((e.get("payload") or {}).get("text") or "")
        for e in outbox.enqueued
        if "没做成" in str((e.get("payload") or {}).get("text") or "")
    ]


def _new_task(tasks: Tasks, *, criteria=None) -> str:
    return tasks.create(
        GID, title="找图", req="找图并存到本地", criteria=list(criteria or CRITERIA), source="test",
    )


class BoomResolveSpecialists:
    """解析工具名单就炸的桩：解析不出来必须 fail-closed，不许把请求名单当真名单。"""

    def __init__(self, agents_mod):
        self.agents = agents_mod

    def role_usable(self, kind):  # noqa: ARG002
        return True

    def effective_tools(self, kind, requested, profile=None):  # noqa: ARG002
        raise RuntimeError("解析炸了")


class FakeRailway:
    """一次性机器桩：记下申请 / 释放，不碰网络。"""

    def __init__(self, box):
        self.box = box
        self.acquired: list[str] = []
        self.released: list = []

    async def acquire(self, tid):
        self.acquired.append(str(tid))
        return self.box

    async def release(self, box):
        self.released.append(box)


class CancelOnSecondPlan(ModelsQueue):
    """第二次排计划（= 开工前能力闸的重排）时同步把任务取消，模拟「重排期间用户取消」。"""

    def __init__(self, replies, tasks, tid):
        super().__init__(replies)
        self._tasks = tasks
        self._tid = tid
        self.plan_calls = 0

    async def chat(self, role=None, messages=None, **kwargs):
        if str(kwargs.get("purpose") or "") == "coordinator.plan":
            self.plan_calls += 1
            if self.plan_calls == 2:
                self._tasks.transition(self._tid, "cancelled", reason="用户取消")
        return await super().chat(role=role, messages=messages, **kwargs)


# ----------------------------------------------------------------------
# 1. 真 news 岗位拿不到执行工具 → 重排成 task → 做完
# ----------------------------------------------------------------------


class TestBoundedReplanFixesRoleMismatch:
    async def test_news_job_replans_to_task_and_completes(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        _write_artifact(workers, env, tasks, tid)
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["write_file", "read_file", "list_files"], agent="task")]),
            _review(pass_=True, artifact=f"artifacts/{tid}/index.html"),
        ])
        coord, outbox, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "completed"
        assert len(workers.calls) == 1
        handed = workers.calls[0]["tools"]
        assert "run_command" in handed, "重排成 task 后要把执行工具补上（真注册过才补）"
        assert workers.calls[0]["agent_type"] == "task"
        # 只重排一次（有界）
        prompts = _plan_prompts(models)
        assert len(prompts) == 2
        assert "news" in prompts[1]
        assert "执行工具" in prompts[1]
        assert "完成标准" in prompts[1] and "不要改" in prompts[1]
        kinds = _kinds(store, tid)
        assert "task.exec_replanned" in kinds
        assert "task.exec_autofill" in kinds
        assert _failed_messages(outbox) == []


# ----------------------------------------------------------------------
# 2. 重排仍不够 → 暂停：零执行、不扣尝试、明确 reason、不是失败
# ----------------------------------------------------------------------


class TestReplanStillBlockedPauses:
    async def test_pauses_without_workers_and_without_attempt_cost(
        self, store, settings, env, tools, agents
    ):
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["web_search"], agent="news")]),  # 第三条不该被用掉
        ])
        coord, outbox, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        t = tasks.get(tid)
        assert t["status"] == "paused", "还是做不到 → 暂停，不是失败"
        assert workers.calls == [], "暂停在派活之前，Workers 必须零执行"
        assert int(t["attempts"]) == 0, "不扣任务尝试资源"
        # 这次尝试只标 stale，不当失败算
        row = store.read().execute(
            "SELECT status FROM attempts WHERE task_id=? ORDER BY n DESC LIMIT 1", (tid,)
        ).fetchone()
        assert row is not None and row["status"] == "stale"
        kinds = _kinds(store, tid)
        assert "task.failed" not in kinds
        assert "task.exec_unavailable" in kinds
        assert "task.exec_replanned" in kinds
        # 有界：第三次计划回复没被用掉
        assert len(_plan_prompts(models)) == 2
        assert len(models.reply_queue) == 1
        # reason 说得清（点名岗位 + 下一步），暂停事件 + 真实 paused_reason 都看得到
        paused = [p for k, p in _events(store, tid) if k == "task.paused"]
        assert paused, "要有 task.paused 事件（带 reason）"
        reason = str(paused[-1].get("reason") or "")
        assert "news" in reason and "执行工具" in reason and "继续" in reason
        # 为什么停走真正的 paused_reason（kind=capability），不再借 env 的注记夹带
        pr = json.loads(t["paused_reason"])
        assert pr["kind"] == "capability" and pr["jobs"] == [1]
        assert "执行工具" in pr["text"] and "继续" in pr["text"]
        assert "开工前对不上" not in str(t.get("env") or "")
        # 不往群里发「没做成」
        assert _failed_messages(outbox) == []

    async def test_replan_with_no_jobs_pauses(self, store, settings, env, tools, agents):
        """重排回一个空 jobs：等于没给出办法 → 照样暂停，不拿空计划往下跑。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []
        assert "task.exec_unavailable" in _kinds(store, tid)


# ----------------------------------------------------------------------
# 3. 重排期间取消：不复活
# ----------------------------------------------------------------------


class TestCancelDuringReplan:
    async def test_cancel_during_replan_stays_cancelled(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = CancelOnSecondPlan(
            [
                _plan_json(jobs=[_job(["web_search"], agent="news")]),
                _plan_json(jobs=[_job(["write_file"], agent="task")]),
            ],
            tasks, tid,
        )
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "cancelled", "重排期间取消，不能被暂停/重排复活"
        assert workers.calls == []
        assert "task.paused" not in _kinds(store, tid)


# ----------------------------------------------------------------------
# 4. 不越权放大岗位 / 不降低用户 criteria
# ----------------------------------------------------------------------


class TestNoEscalationNoCriteriaLowering:
    async def test_replan_cannot_widen_readonly_role(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["web_search", "run_command", "vm_run"], agent="news")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == [], "岗位上限拿不到执行工具 → 不派活"
        # 岗位上限那份解析没有被放大（自检复用同一入口）
        effective, kind, why = coord._job_effective_tools(  # noqa: SLF001
            _job(["web_search", "run_command", "vm_run"], agent="news"),
            on_remote=False, box=None,
        )
        assert kind == "news" and why == ""
        assert not any(t in effective for t in ("run_command", "vm_run", "machine_run"))
        assert "task.exec_autofill" not in _kinds(store, tid)

    async def test_replan_cannot_lower_user_criteria(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        criteria = ["每张图都落到本地，附出处链接", "每张图注明来源"]
        tid = _new_task(tasks, criteria=criteria)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")], criteria=criteria),
            _plan_json(jobs=[_job(["web_search"], agent="news")], criteria=["有东西就行"]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert json.loads(tasks.get(tid)["criteria"]) == criteria, "不许顺手降低用户定的验收标准"
        prompts = _plan_prompts(models)
        assert len(prompts) == 2
        assert "完成标准" in prompts[1] and "不要改" in prompts[1]
        assert workers.calls == []


# ----------------------------------------------------------------------
# 5. 解析不出来一律 fail-closed
# ----------------------------------------------------------------------


class TestFailClosedResolvers:
    async def test_resolver_exception_is_not_treated_as_requested(
        self, store, settings, env, tools, agents
    ):
        workers = CaptureWorkers()
        specialists = BoomResolveSpecialists(agents)
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=ModelsQueue(),
        )

        effective, kind, why = coord._job_effective_tools(  # noqa: SLF001
            _job(["write_file", "run_command"], agent="task"), on_remote=False, box=None
        )

        assert kind == "task"
        assert effective == [], "解析异常不能把请求名单当成真名单（fail-closed）"
        assert "解析" in why

    async def test_resolver_exception_pauses_instead_of_burning_tokens(
        self, store, settings, env, tools, agents
    ):
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

        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []
        note = _note_for(store, tid, "task.exec_unavailable")
        assert "解析" in note
        assert "task.exec_autofill" not in _kinds(store, tid)

    async def test_remote_rename_exception_is_not_treated_as_requested(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=ModelsQueue(),
        )

        def _boom(requested, box):
            raise RuntimeError("换名炸了")

        monkeypatch.setattr(Coordinator, "_remote_job_tools", staticmethod(_boom))

        effective, kind, why = coord._job_effective_tools(  # noqa: SLF001
            _job(["write_file", "run_command"], agent="task"),
            on_remote=True, box=_railway_box(),
        )

        assert kind == "task"
        assert effective == [], "远端换名失败也不能当真名单"
        assert "远端" in why and "没法确认" in why

    async def test_disabled_role_fails_closed_and_pauses(self, store, settings, env, tools, agents):
        agents.update_profile("news", {"enabled": False})
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        effective, kind, why = coord._job_effective_tools(  # noqa: SLF001
            _job(["web_search"], agent="news"), on_remote=False, box=None
        )
        assert effective == [] and "不可用" in why

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []
        assert "不可用" in _note_for(store, tid, "task.exec_unavailable")

    async def test_unregistered_exec_tool_fails_closed_and_pauses(
        self, store, settings, env, agents
    ):
        from CharTyr_MaiWork.maiwork.tools import Tools

        bare = Tools(store)
        _add_worker_tool(bare, "write_file")  # run_command 没注册 = 拿不到
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["write_file", "run_command"], agent="task")]),
            _plan_json(jobs=[_job(["write_file", "run_command"], agent="task")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, bare, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []
        assert "task.exec_autofill" not in _kinds(store, tid)
        assert "task.exec_unavailable" in _kinds(store, tid)


# ----------------------------------------------------------------------
# 6. 一次性机器：暂停前必须释放
# ----------------------------------------------------------------------


class TestRemoteBoxRelease:
    async def test_railway_box_released_before_pause(self, store, settings, env, tools, agents):
        box = _railway_box()
        railway = FakeRailway(box)
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="railway"),
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="railway"),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models, railway=railway,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert railway.acquired == [tid]
        assert railway.released == [box], "暂停也必须释放一次性机器"
        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []


# ----------------------------------------------------------------------
# 7. 排计划提示：先说清岗位实际工具 / 环境
# ----------------------------------------------------------------------


class TestPlanPromptRoleTools:
    async def test_plan_prompt_lists_role_tools_and_env(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[_plan_json(jobs=[_job(["write_file"], agent="task")])])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord._plan(tasks.get(tid)), timeout=10)  # noqa: SLF001

        prompts = _plan_prompts(models)
        assert len(prompts) == 1
        p = prompts[0]
        assert "news" in p and "只读" in p
        assert "没有执行工具" in p
        assert "task" in p and "你给什么它就用什么" in p
        assert "干活环境" in p


# ----------------------------------------------------------------------
# 8. 远端：只有搬文件的工具不算执行能力
# ----------------------------------------------------------------------


class TestRemoteFillOnlyExecTools:
    async def test_put_file_is_not_exec_capability(self, store, settings, env, tools, agents):
        _add_worker_tool(tools, "vm_put_file")  # vm_run 没注册
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="railway"),
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="railway"),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
            railway=FakeRailway(_railway_box()),
        )

        report = coord._exec_capability_self_check(  # noqa: SLF001
            tid, GID,
            {"criteria": list(CRITERIA), "jobs": [_job(["read_file", "write_file"], agent="task")]},
            on_remote=True, box=_railway_box(),
        )
        assert report.blocked is True
        kinds = _kinds(store, tid)
        assert "task.exec_autofill" not in kinds
        assert "task.exec_unavailable" in kinds

        await asyncio.wait_for(coord.run_task(tid), timeout=10)
        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []


class FakeSsh:
    """专用机器桩：记下申请 / 释放，不碰网络。"""

    def __init__(self, box):
        self.box = box
        self.acquired: list[str] = []
        self.released: list = []

    def available(self):
        return True

    def machines(self):
        return [{"name": "vps-1"}]

    async def acquire(self, tid, prefer=""):  # noqa: ARG002
        self.acquired.append(str(tid))
        return self.box

    async def release(self, box):
        self.released.append(box)


class TestRemoteBoxReleaseSsh:
    async def test_ssh_box_released_before_pause(self, store, settings, env, tools, agents):
        box = _ssh_box()
        ssh = FakeSsh(box)
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="ssh"),
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="ssh"),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models, ssh=ssh,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert ssh.acquired == [tid]
        assert ssh.released == [box], "暂停也必须释放专用机器"
        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == []


class TestNoReplanWhenFine:
    async def test_fine_plan_gets_no_extra_model_call(self, store, settings, env, tools, agents):
        """本来就能做（task 岗 + 本机有 run_command）→ 一次计划都不多花，不重排。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        _write_artifact(workers, env, tasks, tid)
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["write_file", "run_command"], agent="task")]),
            _review(pass_=True, artifact=f"artifacts/{tid}/index.html"),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "completed"
        assert len(_plan_prompts(models)) == 1
        assert "task.exec_replanned" not in _kinds(store, tid)
        assert "task.exec_unavailable" not in _kinds(store, tid)
