"""coordinator 派活的 jobs[].agent 参数（专岗改版 4/4）测试。

主模型计划 JSON 的 jobs[] 每项可带 "agent"（要派给哪个专岗，默认 "task"）：
- 内建 kind（news/idea/goal/task）+ 自定义 kind（c_xxx）都接；
- 不存在的 kind → 当没写（回落 task，warning），别把主模型的笔误当 500；
- 主模型提示词里会列出当前在册的自定义专岗（标题 + kind），帮助它自己判断派给谁。

这条路只验证 _plan 解析 和 _run_job 分发：
- _plan 出来 jobs[i]["agent"] = 主模型挑的岗；
- _run_job 走 specialists.run(kind=那个岗)（specialists fake 拿得到）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.coordinator import Coordinator, _parse_plan_json
from CharTyr_MaiWork.maiwork.store import Store

GID = "111"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _agents(store: Store) -> Agents:
    from CharTyr_MaiWork.maiwork.config import load_settings
    settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
    return Agents(store, lambda: settings)


def _mk_coordinator(store: Store, agents: Agents, specialists: Any) -> Coordinator:
    """最小 Coordinator：只够 _parse_jobs / _run_job 单测。"""
    coord = Coordinator(
        store, models=None, workers=None, tools=None, tasks=None, goals=None,
        delivery=None, outbox=None, env=None, profiles=None,
        get_settings=lambda: None,
    )
    coord._specialists = specialists  # noqa: SLF001
    return coord


class _FakeSpecialists:
    """只吃 kind 记下来的 specialists.run stub。"""

    def __init__(self, agents_mod: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        # Specialists.agents 是只读外露（真件也这个形状）；coordinator 靠它读自定义专岗名单
        self._agents_mod = agents_mod

    @property
    def agents(self) -> Any:
        return self._agents_mod

    async def run(self, kind: str, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"kind": kind, "brief": brief, **kwargs})
        from CharTyr_MaiWork.maiwork.workers import WorkerReport
        return WorkerReport(ok=True, summary=f"干了 {kind}")


# ----------------------------------------------------------------------
# _plan 解析：jobs[].agent
# ----------------------------------------------------------------------


def test_parse_plan_json_reads_agent_field(store: Store) -> None:
    agents = _agents(store)
    spec = _FakeSpecialists()
    coord = _mk_coordinator(store, agents, spec)
    raw = """
    {"criteria": ["有成品"],
     "deliver_kind": "file",
     "jobs": [
       {"brief": "去查资料", "type": "research", "tools": ["web_search"]},
       {"brief": "按调研写稿子", "type": "build", "tools": [], "agent": "news"}
     ]}
    """
    data = _parse_plan_json(raw)
    assert data is not None
    # 主模型 JSON 里 agent 字段直通到 plan.jobs（_plan 里做合法性清洗）
    jobs = data.get("jobs") or []
    assert isinstance(jobs, list) and len(jobs) == 2
    assert str(jobs[1].get("agent")) == "news"


# ----------------------------------------------------------------------
# _plan 提示词：列出在册的自定义专岗
# ----------------------------------------------------------------------


def test_plan_prompt_lists_custom_kinds(store: Store) -> None:
    """主模型提示词里会告知「现在有哪些自定义专岗」——标题和 kind 都列。"""
    agents = _agents(store)
    agents.create_custom("制图师")
    agents.create_custom("翻译组")
    spec = _FakeSpecialists(agents)
    coord = _mk_coordinator(store, agents, spec)
    lines = coord._custom_agents_prompt_lines()  # 约定的方法名：没在册时回 []
    assert any("制图师" in ln for ln in lines)
    assert any("翻译组" in ln for ln in lines)
    kinds = [k for k in agents.custom_kinds()]
    assert all(any(k in ln for ln in lines) for k in kinds)


def test_plan_prompt_empty_when_no_custom(store: Store) -> None:
    agents = _agents(store)
    spec = _FakeSpecialists(agents)
    coord = _mk_coordinator(store, agents, spec)
    assert coord._custom_agents_prompt_lines() == []
