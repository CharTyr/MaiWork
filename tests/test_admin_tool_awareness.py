"""管理员工具认知修正（implement.md 第 8 步）：skill_manage 的 write / agent_manage 的
update 挂做法 / social_toggle / agent_manage 的 detail 工具段 / env_overview 社交平台。

- 执行类工具（run_command 等）写做法、挂做法 → 待确认小票；
- 只读工具、摘做法 → 直接做；
- agent_manage 的 detail 的 tools 段读 describe_tools（调研岗列做法工具、各环节名单；
  task 写「工具不固定」、main 写「不跑子 agent」）。
"""

from __future__ import annotations

import pytest

from test_tools_admin_real import G1, _call, app  # noqa: F401
from test_admin_settings_tools import _bind, _direct, _refused, _ticket


def _mk_skill(app, name: str, allowed: str = "", body: str = "做法正文"):
    """直接在数据目录放一份做法（绕过确认门，纯 fixture）。"""
    d = app.get_settings().data_dir / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    at = f"allowed-tools: {allowed}\n" if allowed else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\nroles: worker\n{at}---\n\n{body}\n",
        encoding="utf-8",
    )
    return name


class TestSkillWrite:
    @pytest.mark.asyncio
    async def test_create_readonly_direct(self, app):  # noqa: F811
        r = await _direct(app, "skill_manage", {"action": "write", 
            "name": "my-skill-a", "description": "测试", "body": "正文",
            "allowed_tools": ["fetch_page"],
        })
        assert "my-skill-a" in r.output and "已注册 fetch_page" in r.output
        assert "还没挂到岗位" in r.output

    @pytest.mark.asyncio
    async def test_create_exec_tool_needs_confirm(self, app):  # noqa: F811
        pid = await _ticket(app, "skill_manage", {"action": "write", 
            "name": "my-skill-b", "description": "测试", "body": "正文",
            "allowed_tools": ["run_command"],
        })
        row = app.admin_pending._row(pid)
        assert "run_command" in row["summary"]

    @pytest.mark.asyncio
    async def test_create_exec_string_form_needs_confirm(self, app):  # noqa: F811
        """allowed-tools 给空格分隔字符串（合法写法）：执行类工具照样要确认。"""
        pid = await _ticket(app, "skill_manage", {"action": "write", 
            "name": "my-skill-str", "description": "t", "body": "b",
            "allowed_tools": "fetch_page run_command",
        })
        assert "run_command" in app.admin_pending._row(pid)["summary"]

    @pytest.mark.asyncio
    async def test_builtin_refused(self, app):  # noqa: F811
        skills = app.skills.list()
        builtin = next((s["name"] for s in skills if s.get("builtin")), None)
        assert builtin, "测试环境应有内置做法"
        await _refused(app, "skill_manage", {"action": "write", "name": builtin, "body": "x"}, "内置")

    @pytest.mark.asyncio
    async def test_unknown_tool_reported_not_exec(self, app):  # noqa: F811
        r = await _direct(app, "skill_manage", {"action": "write", 
            "name": "my-skill-c", "description": "t", "body": "b",
            "allowed_tools": ["mcp_ghost_x"],
        })
        assert "没注册" in r.output


class TestAgentUpdateSkills:
    @pytest.mark.asyncio
    async def test_attach_exec_skill_needs_confirm(self, app):  # noqa: F811
        _mk_skill(app, "exec-skill-x", allowed="run_command")
        pid = await _ticket(app, "agent_manage", {"action": "update", "kind": "idea", "add_skills": ["exec-skill-x"]})
        assert "run_command" in app.admin_pending._row(pid)["summary"]

    @pytest.mark.asyncio
    async def test_attach_readonly_direct(self, app):  # noqa: F811
        _mk_skill(app, "ro-skill-x", allowed="fetch_page")
        r = await _direct(app, "agent_manage", {"action": "update", "kind": "goal", "add_skills": ["ro-skill-x"]})
        assert "fetch_page" in r.output

    @pytest.mark.asyncio
    async def test_attach_to_task_no_confirm(self, app):  # noqa: F811
        _mk_skill(app, "exec-skill-y", allowed="run_command")
        await _direct(app, "agent_manage", {"action": "update", "kind": "task", "add_skills": ["exec-skill-y"]})

    @pytest.mark.asyncio
    async def test_unknown_skill_refused(self, app):  # noqa: F811
        await _refused(app, "agent_manage", {"action": "update", "kind": "news", "add_skills": "ghost"}, "不存在")

    @pytest.mark.asyncio
    async def test_remove_skill_direct(self, app):  # noqa: F811
        _mk_skill(app, "exec-skill-z", allowed="run_command")
        await _direct(app, "agent_manage", {"action": "update", "kind": "news", "remove_skills": ["exec-skill-z"]})


class TestSocialToggle:
    @pytest.mark.asyncio
    async def test_enable_needs_confirm(self, app):  # noqa: F811
        pid = await _ticket(app, "social_toggle", {"platform": "reddit", "enabled": True})
        assert "reddit" in app.admin_pending._row(pid)["summary"].lower() or "Reddit" in \
            app.admin_pending._row(pid)["summary"]

    @pytest.mark.asyncio
    async def test_disable_direct(self, app):  # noqa: F811
        r = await _direct(app, "social_toggle", {"platform": "reddit", "enabled": False})
        assert "Reddit" in r.output or "reddit" in r.output

    @pytest.mark.asyncio
    async def test_bad_platform_refused(self, app):  # noqa: F811
        await _refused(app, "social_toggle", {"platform": "facebook", "enabled": True}, "没有")

    @pytest.mark.asyncio
    async def test_cookie_arg_refused(self, app):  # noqa: F811
        r = await _refused(app, "social_toggle",
                           {"platform": "twitter", "enabled": True, "cookie": "x=1"},
                           "Cookie")
        assert "登录态" in r.error

    @pytest.mark.asyncio
    async def test_unknown_arg_refused(self, app):  # noqa: F811
        await _refused(app, "social_toggle",
                       {"platform": "twitter", "enabled": True, "note": "x"}, "不认识")


class TestAgentDetailTools:
    @pytest.mark.asyncio
    async def test_research_detail_lists_skill_tools(self, app):  # noqa: F811
        _mk_skill(app, "g-det", allowed="fetch_page mcp_ghost_z")
        await _direct(app, "agent_manage", {"action": "update", "kind": "idea", "add_skills": ["g-det"]})
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "idea"})
        assert r.data["describe"]["mode"] == "fixed_plus_skills"
        assert "做法带来的工具" in r.output
        assert "g-det" in r.output and "mcp_ghost_z" in r.output
        assert "没注册" in r.output  # mcp_ghost_z 标出来
        assert "各环节实际给" in r.output and "构想调查" in r.output

    @pytest.mark.asyncio
    async def test_main_detail_no_misleading_tools(self, app):  # noqa: F811
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "main"})
        assert "不跑子 agent" in r.output or "主模型岗" in r.output
        assert "有效工具（1 个）" not in r.output


class TestEnvOverviewSocial:
    @pytest.mark.asyncio
    async def test_social_lines_present_no_cookie_values(self, app):  # noqa: F811
        r = await _direct(app, "env_overview", {})
        assert "保底补搜会自动搜已启用的平台" in r.output
        assert "Twitter/X 不填 Cookie 时走网页搜索备选" in r.output
        # 至少一个平台行
        assert "安装中" in r.output or "已装" in r.output or "没装" in r.output
