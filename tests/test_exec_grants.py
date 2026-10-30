"""settings_ops.exec_grants：「这次改动让调研岗新得到哪些执行类工具」（design §2.3）。

- 写/改/安装做法：new_allowed 里 exec_class 的、且旧版本没有的（新建 = 全部执行类）；
- 挂做法到调研岗：被挂做法声明的执行类工具；
- 非执行类（fetch_page 等只读）、摘掉做法、删工具声明 → 空（不需要确认）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import skills_web
from CharTyr_MaiWork.maiwork.settings_ops import exec_grants
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _tools(store):
    tools = Tools(store)

    async def fake(ctx, args):
        return ToolResult(ok=True, output="x")

    def reg(name, **kw):
        tools.register(Tool(name=name, description=name,
                            parameters={"type": "object", "properties": {}},
                            roles=frozenset({"worker"}), handler=fake, **kw))

    reg("fetch_page")
    reg("web_search")
    reg("run_command", needs_workspace=True, exec_class=True)
    reg("write_file", needs_workspace=True, exec_class=True)
    reg("generate_image", needs_workspace=True, exec_class=True, task_only=True)
    return tools


class _Svc:
    """exec_grants 需要的最小 svc：tools / store / get_settings().data_dir。"""

    def __init__(self, tools, data_dir, store):
        self.tools = tools
        self.store = store
        self._s = SimpleNamespace(data_dir=data_dir)

    def get_settings(self):
        return self._s


@pytest.fixture
def svc(store, tmp_path):
    return _Svc(_tools(store), tmp_path, store)


class TestWriteSkill:
    def test_new_skill_with_exec_tool(self, svc):
        assert exec_grants(svc, skill_name="s1",
                           new_allowed=["run_command", "fetch_page"]) == ["run_command"]

    def test_new_skill_readonly_only(self, svc):
        assert exec_grants(svc, skill_name="s1", new_allowed=["fetch_page", "web_search"]) == []

    def test_new_skill_unregistered_name_not_counted(self, svc):
        assert exec_grants(svc, skill_name="s1", new_allowed=["mcp_x", "Bash(git:*)"]) == []

    def test_string_form_also_counts(self, svc):
        """空格分隔字符串写法（skills_web 认这个）→ exec_grants 同样认出来。"""
        assert exec_grants(svc, skill_name="s9",
                           new_allowed="fetch_page run_command") == ["run_command"]

    def test_main_only_tool_not_counted(self, svc, store):
        """只注册给 main 的工具不算「带给调研岗的执行类工具」。"""
        tools = svc.tools

        async def fake(ctx, args):
            return ToolResult(ok=True, output="x")

        tools.register(Tool(name="main_exec", description="m",
                            parameters={"type": "object", "properties": {}},
                            roles=frozenset({"main"}), handler=fake,
                            needs_workspace=True, exec_class=True))
        assert exec_grants(svc, skill_name="s1", new_allowed=["main_exec"]) == []

    def test_update_adding_exec_tool(self, svc, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "s2", "description": "d", "body": "b", "allowed_tools": ["fetch_page"]})
        # 旧版没有 run_command → 算新得到
        assert exec_grants(svc, skill_name="s2",
                           new_allowed=["fetch_page", "run_command"]) == ["run_command"]

    def test_update_keeping_exec_tool(self, svc, tmp_path, store):
        """旧版已经声明过的执行类工具 → 不算新得到（不重复要确认）。"""
        skills_web.create(tmp_path, store, {
            "name": "s3", "description": "d", "body": "b", "allowed_tools": ["run_command"]})
        assert exec_grants(svc, skill_name="s3", new_allowed=["run_command", "fetch_page"]) == []

    def test_update_removing_tools(self, svc, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "s4", "description": "d", "body": "b", "allowed_tools": ["run_command"]})
        assert exec_grants(svc, skill_name="s4", new_allowed=["fetch_page"]) == []
        assert exec_grants(svc, skill_name="s4", new_allowed=[]) == []

    def test_install_counts_like_create(self, svc):
        """安装（zip / skill_install）同写做法：声明执行类就算。"""
        assert exec_grants(svc, skill_name="fresh",
                           new_allowed=["generate_image"]) == ["generate_image"]


class TestAttachSkill:
    def test_attach_exec_skill_to_research_kind(self, svc, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "g1", "description": "d", "body": "b",
            "allowed_tools": ["run_command", "fetch_page"]})
        assert exec_grants(svc, kind="idea", add_skills=["g1"]) == ["run_command"]

    def test_attach_to_task_kind_no_confirm(self, svc, tmp_path, store):
        """task / 自定义专岗不受做法工具影响 → 挂了也不用确认。"""
        skills_web.create(tmp_path, store, {
            "name": "g2", "description": "d", "body": "b", "allowed_tools": ["run_command"]})
        assert exec_grants(svc, kind="task", add_skills=["g2"]) == []
        assert exec_grants(svc, kind="c_xxx", add_skills=["g2"]) == []
        assert exec_grants(svc, kind="main", add_skills=["g2"]) == []

    def test_attach_readonly_skill(self, svc, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "g3", "description": "d", "body": "b", "allowed_tools": ["fetch_page"]})
        assert exec_grants(svc, kind="news", add_skills=["g3"]) == []

    def test_attach_nonexistent_skill(self, svc):
        assert exec_grants(svc, kind="news", add_skills=["ghost"]) == []

    def test_remove_skill_no_confirm(self, svc, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "g5", "description": "d", "body": "b", "allowed_tools": ["run_command"]})
        # remove_skills 不传 add_skills → 空
        assert exec_grants(svc, kind="news") == []

    def test_no_args_is_empty(self, svc):
        assert exec_grants(svc) == []
