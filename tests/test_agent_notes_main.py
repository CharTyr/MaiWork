"""便条板第 3 步主模型那半（docs/34 §二.2 + 第五节第 2 点）：

- 主模型出构想 / 检查目标 / 定资讯关注点时注入同一段便条（group_context include_notes 一个入口）；
- 主模型在检查专岗交回的同一份回包里可附 0~2 条便条（from_kind=main），不另起模型调用。
先红后绿。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import coordinator as coord_mod
from CharTyr_MaiWork.maiwork import feeds as feeds_mod
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"


class _Settings:
    def is_served(self, gid):
        return str(gid) == G1


@pytest.fixture
def agents(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    a = Agents(s, lambda: _Settings())
    hid = a.begin(G1, "news", "找")
    a.running(G1, hid)
    a.returned(G1, hid, "ok", notes=[{"to": "*", "text": "资讯岗留给大家的便条"}])
    a.review(G1, hid, True, "ok")
    yield a
    s.close()


class _SP:
    def __init__(self, agents):
        self.agents = agents
        self._agents = agents


def test_feeds_group_context_safe_include_notes(agents):
    f = object.__new__(feeds_mod.Feeds)
    f._specialists = _SP(agents)
    assert "资讯岗留给大家的便条" in f._group_context_safe(G1, "idea", include_notes=True)
    assert "资讯岗留给大家的便条" not in f._group_context_safe(G1, "idea")


def test_coordinator_group_context_safe_include_notes(agents):
    c = object.__new__(coord_mod.Coordinator)
    c._specialists = _SP(agents)
    assert "资讯岗留给大家的便条" in c._group_context_safe(G1, "goal", include_notes=True)


def test_main_sites_ask_for_notes():
    """出构想 / 定资讯关注点 / 检查目标这三处要带上便条段。"""
    src_focus = inspect.getsource(feeds_mod.Feeds._plan_focus)
    assert '_group_context_safe(gid, "news", include_notes=True)' in src_focus
    src_idea = inspect.getsource(feeds_mod.Feeds._make_idea_round)
    assert '_group_context_safe(gid, "idea", include_notes=True)' in src_idea
    src_goal = inspect.getsource(coord_mod.Coordinator.check_goal)
    assert "include_notes=True" in src_goal
    # 回包里可附便条（不另起调用）
    assert "notes_for_others" in src_idea and "notes_for_others" in src_goal


def test_goal_main_notes_posted_against_investigation(agents):
    c = object.__new__(coord_mod.Coordinator)
    c._specialists = _SP(agents)
    hid = agents.begin(G1, "goal", "查")
    agents.running(G1, hid)
    agents.returned(G1, hid, "ok")
    agents.review(G1, hid, True, "ok")
    inv = coord_mod.GoalInvestigation(assessment="a", plan=[], questions=[], links=[], raw="",
                                      handoff_id=hid)
    c._goal_post_main_notes(G1, inv, {"notes_for_others": [{"to": "news", "text": "盯着新版本"}]})
    got = agents.notes_of_handoff(G1, hid)
    assert [(n["from_kind"], n["to_kind"], n["body"]) for n in got] == [("main", "news", "盯着新版本")]
    # 没有调查（主模型没检查专岗交回）→ 不贴
    c._goal_post_main_notes(G1, None, {"notes_for_others": [{"to": "*", "text": "不该贴"}]})
    assert len(agents.notes_for(G1, "idea")) == 1  # 只有 fixture 里那条
