"""任务双岗协作第一步（docs/20 §5.3）：干活 lane 持久化 + 返工接着改 + 升级模型。

规则（用户 2026-10-05 定）：
- 第 1 次没过 → 同一条干活 lane 带着前情接着改；
- 第 2 次没过 → 先压缩这条 lane，再换升级模型接着改（没有可升级的 → 直接判失败）；
- 第 3 次没过 → 判失败；
- 任务到终态 lane 清空；压缩失败退回「从零 + 说明」不卡死。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork.lanes import TaskLanes
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from tests.test_coordinator import (  # noqa: F401  (fixtures)
    GID,
    FakeOutbox,
    FakeWorkers,
    ModelsQueue,
    ReplayChatResult,
    _build,
    _create_task,
    _plan,
    _review,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)

pytestmark = pytest.mark.asyncio


class LaneModels(ModelsQueue):
    """主模型回放 + 升级目标 + 压缩摘要（purpose 以 :compact 结尾的单独回）。"""

    def __init__(self, replies, *, target=("m1", "主模型"), compact_fail=False):
        super().__init__(replies)
        self.target = target
        self.compact_fail = compact_fail
        self.compact_calls = 0

    def escalation_target(self, kind):
        if self.target is None:
            return None
        return {"entry_id": self.target[0], "label": self.target[1]}

    async def chat(self, role=None, messages=None, **kwargs):
        if str(kwargs.get("purpose") or "").endswith(":compact"):
            self.compact_calls += 1
            if self.compact_fail:
                from CharTyr_MaiWork.maiwork.models import ModelError
                raise ModelError("摘要失败")
            return ReplayChatResult("## 目标\n查资料做一页\n## 试过不行的\n第一版缺链接")
        return await super().chat(role, messages, **kwargs)


class LaneSpecialists:
    """假专岗执行层：记下每次拿到的 history / escalate，并像真 Workers 一样往 history 里追加。"""

    def __init__(self):
        from tests.mafakes_specialists import FakeAgents

        self.calls: list[dict] = []
        self._agents = FakeAgents()

    def review(self, *a, **kw):
        return None

    async def run(self, kind, brief, **kw):
        history = kw.get("history")
        self.calls.append({
            "kind": kind, "brief": brief,
            "history": None if history is None else [dict(m) for m in history],
            "escalate": kw.get("escalate", False),
            "parent_id": kw.get("parent_id", ""),
        })
        n = len(self.calls)
        if history is not None:
            history.append({"role": "user", "content": brief})
            history.append({"role": "assistant", "content": f"第 {n} 轮做完了"})
        rep = WorkerReport(ok=True, summary=f"第 {n} 轮交回", evidence=["e"])
        rep.handoff_id = f"H-{n}"
        return rep


def _setup(mem_store, settings, env, tools, tasks, goals, models, outbox=None):
    settings.is_served = lambda group_id: True
    coord = _build(
        mem_store=mem_store, settings=settings, env=env, tools=tools, tasks=tasks,
        goals=goals, models=models, workers=FakeWorkers(), outbox=outbox,
    )
    spec = LaneSpecialists()
    coord._specialists = spec  # noqa: SLF001
    return coord, spec


def _kinds(store, tid):
    rows = store.read().execute(
        "SELECT kind, payload FROM events WHERE entity='task' AND entity_id=? ORDER BY id", (tid,)
    ).fetchall()
    return [(str(r["kind"]), json.loads(r["payload"] or "{}")) for r in rows]


TEXT_PLAN = _plan(deliver_kind="text", jobs=[{"brief": "查资料做一页", "tools": ["web_search"]}])
BAD = _review(pass_=False, review="没过：缺下载链接", artifact="")
GOOD = _review(pass_=True, review="通过", artifact="")


async def test_first_rework_continues_same_lane(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    assert len(spec.calls) == 2
    assert spec.calls[0]["history"] == []
    second = spec.calls[1]["history"]
    assert any("第 1 轮做完了" == m.get("content") for m in second), "返工带着上一轮的前情"
    assert spec.calls[1]["escalate"] is False
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.lane_rework" in kinds
    assert "task.lane_escalate" not in kinds


async def test_rework_plan_prompt_tells_lead_same_worker(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    plan_prompts = [c[1][-1]["content"] for c in models.calls if c[2].get("purpose") == "coordinator.plan"]
    assert len(plan_prompts) == 2
    assert "接着改" in plan_prompts[1]
    assert "接着改" not in plan_prompts[0]


async def test_second_failure_compacts_and_escalates(mem_store, settings, env, tools, tasks, goals, monkeypatch):
    from CharTyr_MaiWork.maiwork import coordinator as co

    monkeypatch.setattr(co, "_LEAD_COMPACT_TOKENS", 20)
    tid = _create_task(tasks)
    long_plan = json.dumps(
        {"criteria": ["包含链接"], "deliver_kind": "text",
         "jobs": [{"brief": "查资料做一页" * 400, "tools": ["web_search"]}], "question": None},
        ensure_ascii=False,
    )
    models = LaneModels([long_plan, BAD, long_plan, BAD, long_plan, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    assert len(spec.calls) == 3
    assert [c["escalate"] for c in spec.calls] == [False, False, True]
    third = spec.calls[2]["history"]
    assert models.compact_calls >= 1, "换模型前压了一次"
    assert any("试过不行的" in str(m.get("content") or "") for m in third), "换模型前先压缩成前情提要"
    assert "前面对话的摘要" in str(third[0].get("content") or ""), "提要接在最前面"
    esc = [p for k, p in _kinds(mem_store, tid) if k == "task.lane_escalate"]
    assert esc and esc[0].get("to") == "主模型"


async def test_no_escalation_target_fails_at_second(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    outbox = FakeOutbox()
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, BAD, TEXT_PLAN, GOOD], target=None)
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models, outbox=outbox)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "failed"
    assert len(spec.calls) == 2
    texts = [e["payload"]["text"] for e in outbox.enqueued if e["kind"] == "text"]
    assert len(texts) == 1 and "没做成" in texts[0]


async def test_third_failure_fails(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD] * 3)
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "failed"
    assert len(spec.calls) == 3


async def test_compaction_failure_keeps_full_history(mem_store, settings, env, tools, tasks, goals):
    """换模型前压缩没做成：完整前情原样接着用（不 reset 成空、不退回旧提要）。"""
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, BAD, TEXT_PLAN, GOOD], compact_fail=True)
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    third = spec.calls[2]["history"]
    assert third, "不是从零开始"
    text = json.dumps(third, ensure_ascii=False)
    assert "第 1 轮做完了" in text and "第 2 轮做完了" in text
    assert spec.calls[2]["escalate"] is True
    kinds = [k for k, _ in _kinds(mem_store, tid)]
    assert "task.lane_compact_failed" in kinds
    assert "task.lane_reset" not in kinds


async def test_terminal_clears_lane(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, _spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    lane = TaskLanes(mem_store).load(tid, "worker:1", group_id=GID)
    assert lane is not None and lane["messages"] == [] and lane["status"] == "closed"


async def test_lane_switches_kind_resets(mem_store, settings, env, tools, tasks, goals):
    """同一条活这次派给了别的岗：旧前情不是它的，从零开始。"""
    tid = _create_task(tasks)
    lanes = TaskLanes(mem_store)
    if str(tasks.get(tid)["status"]) == "pending_approval":
        tasks.transition(tid, "queued")
    lanes.save(tid, "worker:1", group_id=GID, kind="news",
               messages=[{"role": "user", "content": "别的岗的前情"}], req_version=1)
    models = LaneModels([TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert spec.calls[0]["history"] == []


async def test_rework_handoffs_chain_by_parent(mem_store, settings, env, tools, tasks, goals):
    """§八：同一 lane 的第 2、3 轮交接单指向上一轮。"""
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, BAD, TEXT_PLAN, GOOD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    assert [c["parent_id"] for c in spec.calls] == ["", "H-1", "H-2"]


async def test_compaction_failure_does_not_reinject_old_snapshot(mem_store, settings, env, tools, tasks, goals):
    """压不下去 → 手里的完整前情原样接（不拿旧提要顶替、不从 raw 展开补料）。"""
    tid = _create_task(tasks)
    if str(tasks.get(tid)["status"]) == "pending_approval":
        tasks.transition(tid, "queued")
    TaskLanes(mem_store).save(
        tid, "worker:1", group_id=GID, kind="task", req_version=1, snapshot="旧提要：第一版缺链接",
        messages=[{"role": "user", "content": "上一轮交代"},
                  {"role": "assistant", "content": "搜到 3 条"}],
    )
    models = LaneModels([TEXT_PLAN, BAD, TEXT_PLAN, BAD, TEXT_PLAN, GOOD], compact_fail=True)
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    await coord.run_task(tid)
    third = json.dumps(spec.calls[2]["history"], ensure_ascii=False)
    assert "搜到 3 条" in third, "完整前情接着用"
    assert "旧提要：第一版缺链接" not in third, "不拿旧提要顶替"


async def test_lane_survives_new_coordinator(mem_store, settings, env, tools, tasks, goals):
    """插件重载：对话在库里，新的协调器接着用。"""
    tid = _create_task(tasks)
    models = LaneModels([TEXT_PLAN, BAD])
    coord, spec = _setup(mem_store, settings, env, tools, tasks, goals, models)
    # 只跑第一轮：验收没过后 run_task 会立刻再来，这里用只回放两条的模型让第二轮计划失败前先停
    await coord._run_one_attempt(tasks.get(tid))  # noqa: SLF001
    assert tasks.get(tid)["status"] == "queued"
    models2 = LaneModels([TEXT_PLAN, GOOD])
    from CharTyr_MaiWork.maiwork.tools import Tools

    coord2, spec2 = _setup(mem_store, settings, env, Tools(mem_store), tasks, goals, models2)
    await coord2.run_task(tid)
    assert tasks.get(tid)["status"] == "completed"
    assert any(m.get("content") == "第 1 轮做完了" for m in spec2.calls[0]["history"])
