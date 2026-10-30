"""Specialists.run_auto + 自动回合接线（design §2.2）。

- 名单 = AUTO_PHASES[环节] + 做法带来的非 task_only 工具（与 phase_tools 一致）；
- 没做法声明工具：名单与今天逐字一致、不进 ephemeral、提示不加做法正文；
- 要工作区的工具 → env.ephemeral("rr-"+kind) 单独工作区里跑；env 缺位/不支持/进场抛错
  → 去掉这些工具、记中文 warning、照常跑；
- 拿到做法工具的回合，system_extra 带做法正文。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.skills import Skills
from CharTyr_MaiWork.maiwork.specialists import AUTO_PHASES, Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools

from test_specialists import FakeAgents  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _mk_skill(root, name, allowed="", roles="worker", body="照做法用这些工具"):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    at = f"allowed-tools: {allowed}\n" if allowed else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\nroles: {roles}\n{at}---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def skills(tmp_path, store):
    (tmp_path / "skills").mkdir()
    yield Skills(tmp_path, builtin_root=None, store=store)


class RecWorkers:
    """录全部 kwargs 的假 Workers。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, brief, **kw):
        self.calls.append({"brief": str(brief), **kw})
        from CharTyr_MaiWork.maiwork.workers import WorkerReport

        return WorkerReport(ok=True, summary="ok")


def _tools(store):
    tools = Tools(store)

    async def fake(ctx, args):
        return ToolResult(ok=True, output="x")

    def reg(name, **kw):
        tools.register(Tool(name=name, description=name,
                            parameters={"type": "object", "properties": {}},
                            roles=frozenset({"worker"}), handler=fake, **kw))

    for n in ("web_search", "social_search", "fetch_page", "read_profile",
              "search_chat", "read_chat_history", "list_skills", "read_skill",
              "submit_result"):
        reg(n)
    reg("read_file", needs_workspace=True)
    reg("write_file", needs_workspace=True, exec_class=True)
    reg("run_command", needs_workspace=True, exec_class=True)
    reg("generate_image", needs_workspace=True, exec_class=True, task_only=True)
    return tools


def _env(tmp_path):
    from CharTyr_MaiWork.maiwork.config import load_settings
    from CharTyr_MaiWork.maiwork.environments.local import LocalEnv

    settings, _ = load_settings({"environments": {"local_mode": "direct",
                                                  "workspace_root": str(tmp_path / "ws")}})
    return LocalEnv(lambda: settings)


# ----------------------------------------------------------------------
# 名单与提示
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_skill_tools_identical_list_no_workspace(store, tmp_path, skills):
    """没做法声明工具：名单与今天逐字一致、不给 workspace、提示无做法段。"""
    agents = FakeAgents()
    agents.update_profile("news", {"skills": []})
    workers = RecWorkers()
    sp = Specialists(agents, workers, skills, env=_env(tmp_path), tools=_tools(store))
    report = await sp.run_auto("news", "核对", phase="verify", group_id="900000001",
                               task_id="feeds-verify:x", actor="资讯核验")
    assert report.ok is True
    call = workers.calls[0]
    assert call["tools"] == ["fetch_page", "submit_result"]
    assert call["allowed_tools"] == ("fetch_page", "submit_result")
    assert call["workspace"] is None
    assert "做法" not in (call["system_extra"] or "")
    assert not list(tmp_path.glob("ws/rr-*")), "没要工作区的回合绝不建临时工作区"


@pytest.mark.asyncio
async def test_phase_lists_match_auto_phases(store, tmp_path, skills):
    """三个调研岗的自动环节名单 = 今天的写死名单（零行为变化）。"""
    agents = FakeAgents()
    # 和生产 profile 对齐：出厂 9 个只读工具（FakeAgents 默认少 social_search）
    factory9 = ("web_search", "social_search", "fetch_page", "read_profile",
                "search_chat", "read_chat_history", "list_skills", "read_skill",
                "submit_result")
    for k in ("news", "idea", "goal"):
        agents._profiles[k]["tools"] = factory9
    workers = RecWorkers()
    sp = Specialists(agents, workers, skills, env=_env(tmp_path), tools=_tools(store))
    for kind, phase in (("news", "verify"), ("news", "recheck"),
                        ("idea", "investigate"), ("goal", "check")):
        workers.calls.clear()
        await sp.run_auto(kind, "b", phase=phase, group_id="900000001")
        expected = list(AUTO_PHASES[(kind, phase)]) + ["submit_result"]
        assert workers.calls[0]["tools"] == expected, (kind, phase)


@pytest.mark.asyncio
async def test_skill_granted_ws_tool_runs_in_ephemeral(store, tmp_path, skills):
    """做法声明 run_command（要工作区）→ 这轮在 rr- 开头的单独工作区跑，名单含它。"""
    _mk_skill(tmp_path, "cli", allowed="run_command", body="先跑命令再总结")
    agents = FakeAgents()
    agents.update_profile("idea", {"skills": ["cli"]})
    workers = RecWorkers()
    env = _env(tmp_path)
    sp = Specialists(agents, workers, skills, env=env, tools=_tools(store))
    await sp.run_auto("idea", "调查", phase="investigate", group_id="900000001",
                      deadline_ts=None, actor="构想调查")
    call = workers.calls[0]
    assert "run_command" in call["tools"]
    ws = call["workspace"]
    assert ws is not None and str(ws.name).startswith("rr-idea-")
    assert not ws.exists(), "回合结束单独工作区已删"
    assert "先跑命令再总结" in (call["system_extra"] or "")


@pytest.mark.asyncio
async def test_task_only_tool_never_in_auto_round(store, tmp_path, skills):
    _mk_skill(tmp_path, "img", allowed="generate_image fetch_page")
    agents = FakeAgents()
    agents.update_profile("goal", {"skills": ["img"]})
    workers = RecWorkers()
    sp = Specialists(agents, workers, skills, env=_env(tmp_path), tools=_tools(store))
    await sp.run_auto("goal", "检查", phase="check", group_id="900000001")
    tools = workers.calls[0]["tools"]
    assert "generate_image" not in tools
    assert "fetch_page" in tools


@pytest.mark.asyncio
async def test_no_ws_tool_skill_no_ephemeral(store, tmp_path, skills):
    """做法只声明只读工具（fetch_page）→ 不建临时工作区。"""
    _mk_skill(tmp_path, "ro", allowed="fetch_page")
    agents = FakeAgents()
    agents.update_profile("news", {"skills": ["ro"]})
    workers = RecWorkers()
    sp = Specialists(agents, workers, skills, env=_env(tmp_path), tools=_tools(store))
    await sp.run_auto("news", "核对", phase="verify", group_id="900000001")
    assert workers.calls[0]["workspace"] is None
    assert not list(tmp_path.glob("ws/rr-*"))


# ----------------------------------------------------------------------
# env 缺位 / 不支持 / 进场失败 → 去掉要工作区的工具照常跑
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_none_drops_ws_tools(store, tmp_path, skills, caplog):
    _mk_skill(tmp_path, "cli", allowed="run_command read_file")
    agents = FakeAgents()
    agents.update_profile("idea", {"skills": ["cli"]})
    workers = RecWorkers()
    sp = Specialists(agents, workers, skills, env=None, tools=_tools(store))
    with caplog.at_level(logging.WARNING, logger="maiwork.specialists"):
        report = await sp.run_auto("idea", "调查", phase="investigate", group_id="900000001")
    assert report.ok is True
    tools = workers.calls[0]["tools"]
    assert "run_command" not in tools and "read_file" not in tools
    assert "web_search" in tools
    assert any("工作区" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_env_without_ephemeral_drops_ws_tools(store, tmp_path, skills, caplog):
    _mk_skill(tmp_path, "cli", allowed="run_command")
    agents = FakeAgents()
    agents.update_profile("goal", {"skills": ["cli"]})
    workers = RecWorkers()

    class WeirdEnv:  # 没 ephemeral 的环境对象
        pass

    sp = Specialists(agents, workers, skills, env=WeirdEnv(), tools=_tools(store))
    with caplog.at_level(logging.WARNING, logger="maiwork.specialists"):
        await sp.run_auto("goal", "检查", phase="check", group_id="900000001")
    assert "run_command" not in workers.calls[0]["tools"]


@pytest.mark.asyncio
async def test_ephemeral_enter_raises_drops_ws_tools(store, tmp_path, skills, caplog):
    _mk_skill(tmp_path, "cli", allowed="run_command")
    agents = FakeAgents()
    agents.update_profile("goal", {"skills": ["cli"]})
    workers = RecWorkers()

    class BadCtx:
        async def __aenter__(self):
            raise OSError("起不来")

        async def __aexit__(self, *a):
            return False

    class BadEnv:
        def ephemeral(self, prefix, *, deadline_ts=None):
            return BadCtx()

    sp = Specialists(agents, workers, skills, env=BadEnv(), tools=_tools(store))
    with caplog.at_level(logging.WARNING, logger="maiwork.specialists"):
        report = await sp.run_auto("goal", "检查", phase="check", group_id="900000001")
    assert report.ok is True
    assert "run_command" not in workers.calls[0]["tools"]


@pytest.mark.asyncio
async def test_granting_text_in_system_extra(store, tmp_path, skills):
    _mk_skill(tmp_path, "ro", allowed="fetch_page", body="这份做法的正文内容XYZ")
    agents = FakeAgents()
    agents.update_profile("news", {"skills": ["ro"]})
    workers = RecWorkers()
    sp = Specialists(agents, workers, skills, env=_env(tmp_path), tools=_tools(store))
    await sp.run_auto("news", "核对", phase="verify", group_id="900000001")
    extra = workers.calls[0]["system_extra"] or ""
    assert "这份做法的正文内容XYZ" in extra
    assert "管理员" in extra


# ----------------------------------------------------------------------
# 单独工作区收尾：run 抛异常 / 被取消都要删目录并让异常继续传
# ----------------------------------------------------------------------


class _BoomWorkers:
    async def run(self, brief, **kw):
        self.kw = kw
        raise RuntimeError("模型调用炸了")


class _CancelWorkers:
    async def run(self, brief, **kw):
        self.kw = kw
        raise asyncio.CancelledError()


import asyncio  # noqa: E402


@pytest.mark.asyncio
async def test_run_exception_still_cleans_ephemeral(store, tmp_path, skills):
    _mk_skill(tmp_path, "cli", allowed="run_command")
    agents = FakeAgents()
    agents.update_profile("idea", {"skills": ["cli"]})
    sp = Specialists(agents, _BoomWorkers(), skills, env=_env(tmp_path), tools=_tools(store))
    # run() 自己兜住 worker 异常 → 返回失败 report；工作区照样收尾
    report = await sp.run_auto("idea", "x", phase="investigate", group_id="900000001")
    assert report.ok is False
    assert not list((tmp_path / "ws").glob("rr-idea-*"))


@pytest.mark.asyncio
async def test_run_cancelled_cleans_and_propagates(store, tmp_path, skills):
    _mk_skill(tmp_path, "cli", allowed="run_command")
    agents = FakeAgents()
    agents.update_profile("idea", {"skills": ["cli"]})
    sp = Specialists(agents, _CancelWorkers(), skills, env=_env(tmp_path), tools=_tools(store))
    with pytest.raises(asyncio.CancelledError):
        await sp.run_auto("idea", "x", phase="investigate", group_id="900000001")
    assert not list((tmp_path / "ws").glob("rr-idea-*"))
