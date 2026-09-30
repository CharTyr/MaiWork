"""真实专岗存储 + 执行层契约集成；工作器是无外部调用的测试替身。"""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.skills import Skills
from CharTyr_MaiWork.maiwork.specialists import Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from test_app import _raw, G1, G2


class StubWorker:
    def __init__(self):
        self.calls = []
        self.cancel = False

    async def run(self, brief, **kwargs):
        self.calls.append((brief, kwargs))
        if self.cancel:
            raise asyncio.CancelledError
        return WorkerReport(ok=True, summary="这是待验收候选，不能当作已确认事实", data={"candidate": "候选"}, evidence=["https://example.com/source"])


@pytest.fixture
def env(tmp_path: Path):
    settings, problems = load_settings(_raw(tmp_path / "data"))
    assert not problems
    store = Store(tmp_path / "state.db")
    store.migrate()
    agents = Agents(store, lambda: settings)
    worker = StubWorker()
    skills = Skills(tmp_path, store=store, settings=lambda: settings)
    sp = Specialists(agents, worker, skills)
    yield agents, worker, sp
    store.close()


@pytest.mark.asyncio
async def test_real_state_submission_is_not_acceptance(env):
    agents, worker, sp = env
    report = await sp.run("news", "核对来源", group_id=G1, phase="verify", tools=["fetch_page", "run_command"])
    assert report.ok and report.handoff_id
    assert agents.handoff(G1, report.handoff_id)["status"] == "returned"
    assert agents.memory(G1, "news")["learned"] == []
    assert "run_command" not in worker.calls[0][1]["tools"]
    sp.review(G1, report, True, "主流程采用了经核验的资讯", refs=["https://example.com/source"], learn=False)
    assert agents.handoff(G1, report.handoff_id)["status"] == "accepted"
    assert agents.memory(G1, "news")["learned"] == []
    agents.remember(G1, "news", "已验收：这条有原始来源", refs=["https://example.com/source"], source_id="news_batch:1")
    assert len(agents.memory(G1, "news")["learned"]) == 1
    assert agents.memory(G1, "idea")["learned"] == []
    assert agents.memory(G2, "news")["learned"] == []


@pytest.mark.asyncio
async def test_real_state_cross_group_review_and_rejection(env):
    agents, worker, sp = env
    report = await sp.run("idea", "查可行性", group_id=G1, tools=[])
    with pytest.raises((ValueError, KeyError)):
        sp.review(G2, report, True, "越群验收不允许")
    assert agents.handoff(G1, report.handoff_id)["status"] == "returned"
    sp.review(G1, report, False, "缺乏依据，未采用")
    assert agents.handoff(G1, report.handoff_id)["status"] == "rejected"
    assert agents.memory(G1, "idea")["learned"] == []
    with pytest.raises((ValueError, KeyError)):
        sp.review(G1, report, True, "不能复活已拒绝结果")


@pytest.mark.asyncio
async def test_real_state_cancellation_closes_trace(env):
    agents, worker, sp = env
    worker.cancel = True
    with pytest.raises(asyncio.CancelledError):
        await sp.run("goal", "调查目标阻碍", group_id=G1, tools=[])
    items = agents.handoffs(G1, "goal")
    assert len(items) == 1 and items[0]["status"] == "cancelled"
    assert agents.memory(G1, "goal")["learned"] == []


@pytest.mark.asyncio
async def test_real_state_generic_task_does_not_learn_across_tasks(env):
    agents, worker, sp = env
    report = await sp.run("task", "执行本次获批任务", group_id=G1, tools=["write_file"], task_id="T-test")
    assert "write_file" in worker.calls[0][1]["tools"]
    sp.review(G1, report, True, "主流程已经核对成品", refs=["task:T-test"])
    assert agents.handoff(G1, report.handoff_id)["status"] == "accepted"
    assert agents.memory(G1, "task")["learned"] == []
