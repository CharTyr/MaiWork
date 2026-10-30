"""岗位工具计算：做法（skill）的 allowed-tools → 调研岗工具（design §2.2）。

- skill_tools / granted_tools / phase_tools / granting_skill_text / describe_tools；
- _role_whitelist = 出厂 ∪ 做法带来的（调研岗才有；profile.tools 仍只能收窄出厂部分）；
- 安全底线：只有「Skills.list(worker) 里已启用的」做法能带工具；
  「本群/…」名字一律不带（agent_skills 本来就不在 list 里，这里是双保险）；
- 没做法声明工具时：名单与今天逐字一致。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.skills import Skills
from CharTyr_MaiWork.maiwork.specialists import AUTO_PHASES, RESEARCH_KINDS, Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools

from test_specialists import FakeAgents, FakeWorkers  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _mk_skill(root, name, allowed="", roles="worker", body="按这个做法干"):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    at = f"allowed-tools: {allowed}\n" if allowed else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\nroles: {roles}\n{at}---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def skills(tmp_path, store):
    root = tmp_path / "skills"
    root.mkdir()
    yield Skills(tmp_path, builtin_root=None, store=store)


def _tools(store, tmp_path):
    """真 Tools + 真注册的 exec 工具（读 traits 要用）。"""
    from CharTyr_MaiWork.maiwork.config import load_settings
    from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
    from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools

    settings, _ = load_settings({"environments": {"local_mode": "direct",
                                                  "workspace_root": str(tmp_path / "ws")}})
    env = LocalEnv(lambda: settings)
    tools = Tools(store)

    class _Host:
        async def messages(self, *a, **kw):
            return []

    register_exec_tools(tools, env=env, host=_Host(), get_settings=lambda: settings)

    async def fake(ctx, args):
        return ToolResult(ok=True, output="x")

    for name in ("web_search", "social_search", "fetch_page", "read_profile",
                 "search_chat", "submit_result"):
        tools.register(Tool(
            name=name, description=name, parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker"}), handler=fake, timeout_s=5.0,
        ))
    for name in ("list_skills", "read_skill"):
        tools.register(Tool(
            name=name, description=name, parameters={"type": "object", "properties": {}},
            roles=frozenset({"worker", "main"}), handler=fake, timeout_s=5.0,
        ))
    return tools


def _sp(agents, workers, skills, *, env=None, tools=None):
    return Specialists(agents, workers, skills, env=env, tools=tools)


# ----------------------------------------------------------------------
# skill_tools / granted_tools
# ----------------------------------------------------------------------


class TestSkillTools:
    def test_attached_skill_grants_registered_tools(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "news-cli", allowed="run_command write_file")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["news-cli"]})
        tools = _tools(store, tmp_path)
        sp = _sp(agents, FakeWorkers(), skills, tools=tools)
        assert sp.granted_tools("news") == ["run_command", "write_file"]
        st = sp.skill_tools("news")
        assert len(st) == 1 and st[0]["skill"] == "news-cli"
        assert st[0]["unknown"] == []
        by_name = {t["name"]: t for t in st[0]["tools"]}
        assert by_name["run_command"] == {"name": "run_command", "registered": True,
                                          "exec_class": True, "task_only": False}

    def test_unregistered_names_reported_not_granted(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "odd", allowed="mcp_flovia_flovia_x Bash(git:*) fetch_page")
        agents = FakeAgents()
        agents.update_profile("idea", {"skills": ["odd"]})
        tools = _tools(store, tmp_path)
        sp = _sp(agents, FakeWorkers(), skills, tools=tools)
        st = sp.skill_tools("idea")
        assert sorted(st[0]["unknown"]) == ["Bash(git:*)", "mcp_flovia_flovia_x"]
        # 只有已注册的进 granted
        assert sp.granted_tools("idea") == ["fetch_page"]

    def test_no_tools_registry_means_nothing_granted(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "news-cli", allowed="run_command")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["news-cli"]})
        sp = _sp(agents, FakeWorkers(), skills)  # tools=None（老构造）
        assert sp.granted_tools("news") == []
        st = sp.skill_tools("news")
        assert st[0]["unknown"] == ["run_command"]
        assert all(t["registered"] is False for t in st[0]["tools"])

    def test_main_only_tool_counts_as_unregistered(self, store, tmp_path, skills):
        """做法声明只给主模型的工具：算 unknown，不进 granted（注册的口径 = 注册给 worker）。"""
        _mk_skill(tmp_path, "m", allowed="group_notice_send run_command")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["m"]})
        tools = _tools(store, tmp_path)

        async def fake(ctx, args):
            return ToolResult(ok=True, output="x")

        tools.register(Tool(
            name="group_notice_send", description="g",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"main"}), handler=fake,
        ))
        sp = _sp(agents, FakeWorkers(), skills, tools=tools)
        st = sp.skill_tools("news")
        assert st[0]["unknown"] == ["group_notice_send"]
        by_name = {t["name"]: t for t in st[0]["tools"]}
        assert by_name["group_notice_send"]["registered"] is False
        assert sp.granted_tools("news") == ["run_command"]
        assert "group_notice_send" not in sp.phase_tools("news", "verify")

    def test_non_research_kind_empty(self, store, tmp_path, skills):
        agents = FakeAgents()
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        assert sp.skill_tools("task") == []
        assert sp.granted_tools("task") == []
        assert sp.granted_tools("main") == []

    def test_group_skill_names_never_grant(self, store, tmp_path, skills):
        """安全底线双保险：名字带 本群/ 或路径符的做法，哪怕被塞进岗位名单也不带工具。"""
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["本群/私下做法", "evil/../x"]})
        tools = _tools(store, tmp_path)

        class EvilSkills:
            def list(self, role=None):
                return [{"name": "本群/私下做法", "allowed_tools": ["run_command"]},
                        {"name": "evil/../x", "allowed_tools": ["run_command"]}]

            def read(self, name):
                return "正文"

        sp = _sp(agents, FakeWorkers(), EvilSkills(), tools=tools)
        assert sp.skill_tools("news") == []
        assert sp.granted_tools("news") == []
        assert "run_command" not in sp.effective_tools("news", None)


# ----------------------------------------------------------------------
# _role_whitelist / effective_tools
# ----------------------------------------------------------------------


class TestWhitelist:
    def test_effective_tools_factory_plus_granted(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "news-cli", allowed="run_command mcp_x")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["news-cli"]})
        tools = _tools(store, tmp_path)
        sp = _sp(agents, FakeWorkers(), skills, tools=tools)
        eff = sp.effective_tools("news", None)
        assert "run_command" in eff          # 做法带来的
        assert "mcp_x" not in eff            # 未注册不算
        for t in ("web_search", "fetch_page", "submit_result"):
            assert t in eff                  # 出厂的还在

    def test_profile_tools_narrows_only_factory_part(self, store, tmp_path, skills):
        """profile.tools 收窄的是出厂部分；做法带来的工具不被它压掉。"""
        _mk_skill(tmp_path, "news-cli", allowed="run_command")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["news-cli"]})
        agents._profiles["news"]["tools"] = ("web_search", "submit_result")
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        eff = sp.effective_tools("news", None)
        assert "web_search" in eff and "run_command" in eff
        assert "fetch_page" not in eff        # 出厂部分被收窄了

    def test_profile_tools_still_cannot_widen(self, store, tmp_path, skills):
        """旧红线不变：profile.tools 写 run_command 不给（它只能收窄出厂部分）。"""
        agents = FakeAgents()
        agents.update_profile("news", {"skills": []})
        agents._profiles["news"]["tools"] = ("web_search", "run_command", "submit_result")
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        eff = sp.effective_tools("news", None)
        assert "run_command" not in eff

    def test_task_kind_unaffected(self, store, tmp_path, skills):
        agents = FakeAgents()
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        eff = sp.effective_tools("task", ["read_file", "run_command"])
        assert set(eff) == {"read_file", "run_command", "submit_result"}

    def test_no_skills_identical_to_before(self, store, tmp_path, skills):
        """零行为变化锁：没做法声明工具时 effective_tools 与旧口径逐字一致。"""
        agents = FakeAgents()
        agents.update_profile("news", {"skills": []})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        # FakeAgents 的 news profile.tools ∩ 出厂名单（= 改动前的输出）
        expected = [t for t in FakeAgents.DEFAULT_TOOLS["news"]
                    if t in ("web_search", "social_search", "fetch_page", "read_profile",
                             "search_chat", "read_chat_history", "list_skills", "read_skill",
                             "submit_result")]
        assert sp.effective_tools("news", None) == expected


# ----------------------------------------------------------------------
# phase_tools
# ----------------------------------------------------------------------


class TestPhaseTools:
    def test_auto_phases_table(self):
        """表里的环节名单 = 今天调用点写死的名单，逐字搬进来。"""
        assert AUTO_PHASES[("news", "verify")] == ("fetch_page",)
        assert AUTO_PHASES[("news", "recheck")] == ("fetch_page", "web_search")
        assert AUTO_PHASES[("news", "supplement")] == ()
        assert AUTO_PHASES[("idea", "investigate")] == (
            "web_search", "social_search", "fetch_page", "read_profile",
            "list_skills", "read_skill")
        assert AUTO_PHASES[("goal", "check")] == (
            "web_search", "social_search", "fetch_page", "read_profile")
        assert set(RESEARCH_KINDS) == {"news", "idea", "goal"}

    def test_phase_tools_base_only(self, store, tmp_path, skills):
        agents = FakeAgents()
        agents.update_profile("idea", {"skills": []})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        assert sp.phase_tools("idea", "investigate") == list(AUTO_PHASES[("idea", "investigate")])
        assert sp.phase_tools("news", "verify") == ["fetch_page"]

    def test_phase_tools_plus_granted_minus_task_only(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "g", allowed="run_command generate_image mcp_x")
        agents = FakeAgents()
        agents.update_profile("goal", {"skills": ["g"]})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        tools = _tools(store, tmp_path)
        # generate_image 注册成 task_only → 不进自动回合
        async def fake(ctx, args):
            return ToolResult(ok=True, output="x")
        tools.register(Tool(name="generate_image", description="g",
                            parameters={"type": "object", "properties": {}},
                            roles=frozenset({"worker"}), handler=fake,
                            needs_workspace=True, exec_class=True, task_only=True))
        out = sp.phase_tools("goal", "check")
        assert out[:4] == list(AUTO_PHASES[("goal", "check")])
        assert "run_command" in out
        assert "generate_image" not in out
        assert "mcp_x" not in out

    def test_unknown_phase_empty_plus_granted(self, store, tmp_path, skills):
        """表里没有的环节（如 news supplement）：基础名单空，只给做法带来的。"""
        _mk_skill(tmp_path, "g", allowed="read_file")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["g"]})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        assert sp.phase_tools("news", "supplement") == ["read_file"]
        # 但没做法工具时 supplement 为空 → 上层不跑
        agents.update_profile("news", {"skills": []})
        assert sp.phase_tools("news", "supplement") == []


# ----------------------------------------------------------------------
# granting_skill_text
# ----------------------------------------------------------------------


class TestGrantingSkillText:
    def test_body_included_marked_admin(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "g", allowed="run_command", body="第一步先跑命令看输出")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["g"]})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        text = sp.granting_skill_text("news", ["run_command"])
        assert "第一步先跑命令看输出" in text
        assert "管理员" in text
        assert "g" in text

    def test_only_granting_skills_included(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "granter", allowed="run_command", body="带工具的做法")
        _mk_skill(tmp_path, "plain", allowed="", body="没声明工具的做法")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["granter", "plain"]})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        text = sp.granting_skill_text("news", ["run_command"])
        assert "带工具的做法" in text
        assert "没声明工具的做法" not in text

    def test_truncation_limits(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "big", allowed="run_command", body="长" * 5000)
        _mk_skill(tmp_path, "big2", allowed="write_file", body="长" * 5000)
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["big", "big2"]})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        text = sp.granting_skill_text("news", ["run_command", "write_file"])
        assert len(text) <= 6200
        assert "read_skill" in text  # 截断时提示全文用 read_skill 读


# ----------------------------------------------------------------------
# describe_tools
# ----------------------------------------------------------------------


class TestDescribeTools:
    def test_research_kind_shape(self, store, tmp_path, skills):
        _mk_skill(tmp_path, "g", allowed="run_command")
        agents = FakeAgents()
        agents.update_profile("news", {"skills": ["g"]})
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        d = sp.describe_tools("news")
        assert d["mode"] == "fixed_plus_skills"
        assert "web_search" in d["factory"]
        assert d["skills"][0]["skill"] == "g"
        assert d["phases"]["verify"] == ["fetch_page", "run_command"]

    def test_task_kind_per_job(self, store, tmp_path, skills):
        agents = FakeAgents()
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        d = sp.describe_tools("task")
        assert d["mode"] == "per_job"
        assert "run_command" in d["candidates"]
        assert "不额外生效" in d["note"]

    def test_main_kind(self, store, tmp_path, skills):
        agents = FakeAgents()
        sp = _sp(agents, FakeWorkers(), skills, tools=_tools(store, tmp_path))
        d = sp.describe_tools("main")
        assert d["mode"] == "main"
        assert "管理员对话只有管理员工具" in d["note"]
        assert isinstance(d["plan_review"], list)
