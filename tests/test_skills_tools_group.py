"""skills_tools.read_skill / list_skills：支持「本群/<name>」走 group skill（docs/17 §七.2）。

- task kind：列出最多 12 份；本岗专岗再来一份「本群/<kind>-本群做法」；
- ctx.group_id 不对 / 没接 agents → 「没有」；
- read_skill 计算 uses + last_used；
- 跨群读不到。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN.parent))

from CharTyr_MaiWork.maiwork import agents as agents_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import skills_tools as skills_tools_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import skills as skills_mod  # noqa: E402
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402
from CharTyr_MaiWork.maiwork.tools import Tools  # noqa: E402


G1 = "900000001"
G2 = "902106124"


class _Settings:
    served_groups = (G1, G2)

    def is_served(self, gid: str) -> bool:
        return str(gid) in self.served_groups


def _ctx(gid: str, agent_type: str = ""):
    # 岗位字段是 ToolContext.agent_type（2026-10 起；老写法读 ctx.kind 是空想字段）
    return SimpleNamespace(group_id=gid, agent_type=agent_type, role="worker")


def _env(tmp_path):
    store = Store(tmp_path / "t.db")
    store.migrate()
    agents = agents_mod.Agents(store, lambda: _Settings())
    agents._ensure_schema()
    skills = skills_mod.Skills(str(tmp_path))
    tools = Tools(store)
    tools._agents = agents  # noqa: SLF001
    skills_tools_mod.register_skill_tools(tools, skills)
    return store, agents, tools


def _run(coro):
    return asyncio.run(coro)


def _handle(tools, name):
    return tools._tools[name].handler  # noqa: SLF001



class TestListSkills:
    def test_no_group_no_agents_no_locals(self, tmp_path):
        _store, _a, tools = _env(tmp_path)
        list_handler = _handle(tools, "list_skills")
        r = _run(list_handler(_ctx(gid=""), {}))
        assert r.ok
        assert "本群/" not in r.output

    def test_with_group_shows_task_skills(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        agents.skill_add(G1, "task", name="整理报名表", description="通用做法", body="步骤")
        agents.skill_add(G1, "news", description="", body="资讯做法")
        list_handler = _handle(tools, "list_skills")
        r = _run(list_handler(_ctx(G1), {}))
        assert r.ok
        assert "本群/整理报名表" in r.output  # task 活
        # kind_hint 空时不出专岗本岗
        assert "本群/news-本群做法" not in r.output

    def test_kind_hint_shows_specialist_skill(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        agents.skill_add(G1, "news", description="", body="资讯做法")
        list_handler = _handle(tools, "list_skills")
        r = _run(list_handler(_ctx(G1, agent_type="news"), {}))
        assert "本群/news-本群做法" in r.output

    def test_kind_main_no_skill(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        agents.skill_add(G1, "news", description="", body="做法")
        list_handler = _handle(tools, "list_skills")
        r = _run(list_handler(_ctx(G1, agent_type="main"), {}))
        # 虽然 agents 拒 task 而 kind_hint 是 main 时不出专岗本岗 — 列表里
        assert "本群/news-本群做法" not in r.output


class TestReadSkill:
    def test_group_skill_read_ok_and_touch(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        sid = agents.skill_add(G1, "task", name="整理报名表", description="", body="步骤1：……")
        read_handler = _handle(tools, "read_skill")
        r = _run(read_handler(_ctx(G1), {"name": "本群/整理报名表"}))
        assert r.ok
        assert "步骤1" in r.output
        # touch use
        row = agents.skill_get(G1, sid)
        assert row["uses"] >= 1
        assert row["last_used"] > 0

    def test_cross_group_returns_not_found(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        agents.skill_add(G1, "task", name="整理报名表", description="", body="")
        read_handler = _handle(tools, "read_skill")
        r = _run(read_handler(_ctx(G2), {"name": "本群/整理报名表"}))
        assert not r.ok

    def test_wrong_group_returns_not_found(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        agents.skill_add(G2, "task", name="整理报名表", description="", body="")
        read_handler = _handle(tools, "read_skill")
        # G2 的 skill 不该被 G1 边界读
        r = _run(read_handler(_ctx(G1), {"name": "本群/整理报名表"}))
        assert not r.ok

    def test_specialist_skill_read_from_own_kind(self, tmp_path):
        _store, agents, tools = _env(tmp_path)
        agents.skill_add(G1, "news", description="", body="只挑想看的线索")
        read_handler = _handle(tools, "read_skill")
        r = _run(read_handler(_ctx(G1, agent_type="news"), {"name": "本群/news-本群做法"}))
        assert r.ok
        assert "线索" in r.output
