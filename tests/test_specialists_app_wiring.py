"""真实 app 装配：Agents/Specialists 挂到资讯、目标、任务三处；本岗本群记忆进提示；工具按岗位收窄。"""
from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.specialists import Specialists
from CharTyr_MaiWork.maiwork.workers import WorkerReport
from test_app import _app, G1, G2


@pytest.mark.asyncio
async def test_real_app_wires_specialists_and_injects_own_group_memory(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await app.start()
    try:
        assert isinstance(app.agents, Agents)
        sp = app.feeds._specialists
        assert isinstance(sp, Specialists)
        assert app.coordinator._specialists is sp and app.goal_proposer._specialists is sp
        app.agents.set_notes(G1, "news", "本群只看开源硬件")
        app.agents.set_notes(G2, "news", "另一个群的秘密口味")
        app.agents.remember(G1, "news", "已验收：官方博客可靠", source_id="t1")
        seen: dict = {}

        async def fake_run(brief, **kw):
            seen.update(kw)
            seen["brief"] = brief
            return WorkerReport(ok=True, summary="候选", data={"items": []})

        sp._workers.run = fake_run
        r = await sp.run("news", "找资讯", group_id=G1, phase="discover", tools=["web_search", "run_command"])
        text = seen["brief"] + str(seen.get("system_extra", ""))
        assert "本群只看开源硬件" in text and "官方博客可靠" in text
        assert "另一个群的秘密口味" not in text
        assert "run_command" not in seen["tools"] and seen["agent_type"] == "news"
        assert app.agents.handoff(G1, r.handoff_id)["status"] == "returned"
        assert app.agents.memory(G1, "news")["learned"][0]["source_id"] == "t1"
    finally:
        await app.stop()
    assert app.feeds is None or getattr(app.feeds, "_specialists", None) is None
