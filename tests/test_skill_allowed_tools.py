"""skill front matter 的 allowed-tools 解析与网页写回（design §2.1）。

- skills.py 解析 `allowed-tools`：空格分隔字符串为主；容错接受 YAML 行内列表；
  原样保留（认不出的名字也留——由岗位工具计算那层判 registered，不在这里丢）。
- Skills.list() 每项带 allowed_tools（去重保序）。
- skills_web.create / update 写回 front matter 再读回一致；get_view / list_view 带出字段。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork.skills import Skills, parse_front_matter, set_disabled
from CharTyr_MaiWork.maiwork import skills_web
from CharTyr_MaiWork.maiwork.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _mk_skill(root, name, front_extra="", body="正文"):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试\n{front_extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


class TestParseFrontMatter:
    def test_space_separated(self):
        front = parse_front_matter(
            "---\nname: x\nallowed-tools: mcp_flovia_flovia_x Bash(git:*) run_command\n---\n正文"
        )
        assert front.get("allowed_tools") == ["mcp_flovia_flovia_x", "Bash(git:*)", "run_command"]

    def test_inline_yaml_list(self):
        front = parse_front_matter(
            "---\nname: x\nallowed-tools: [run_command, write_file]\n---\n正文"
        )
        assert front.get("allowed_tools") == ["run_command", "write_file"]

    def test_quoted_values_stripped(self):
        front = parse_front_matter(
            '---\nname: x\nallowed-tools: "run_command web_search"\n---\n正文'
        )
        assert front.get("allowed_tools") == ["run_command", "web_search"]

    def test_absent_means_empty(self):
        front = parse_front_matter("---\nname: x\ndescription: d\n---\n正文")
        assert front.get("allowed_tools") == []

    def test_dedup_preserve_order(self):
        front = parse_front_matter(
            "---\nname: x\nallowed-tools: a b a c b\n---\n正文"
        )
        assert front.get("allowed_tools") == ["a", "b", "c"]


class TestSkillsList:
    def test_list_items_carry_allowed_tools(self, tmp_path, store):
        _mk_skill(tmp_path, "with-tools",
                  "allowed-tools: run_command mcp_flovia_flovia_x\n")
        _mk_skill(tmp_path, "plain", "")
        skills = Skills(tmp_path, builtin_root=None, store=store)
        items = {i["name"]: i for i in skills.list("worker")}
        assert items["with-tools"]["allowed_tools"] == ["run_command", "mcp_flovia_flovia_x"]
        assert items["plain"]["allowed_tools"] == []

    def test_disabled_skill_not_listed(self, tmp_path, store):
        """停用的做法不进 Skills.list —— 自然也不可能带工具（安全底线的第一道）。"""
        _mk_skill(tmp_path, "off", "allowed-tools: run_command\n")
        set_disabled(store, "off", True)
        skills = Skills(tmp_path, builtin_root=None, store=store)
        assert [i["name"] for i in skills.list("worker")] == []

    def test_main_only_skill_not_in_worker_list(self, tmp_path, store):
        _mk_skill(tmp_path, "mainonly",
                  "metadata:\n  maiwork-roles: main\nallowed-tools: run_command\n")
        skills = Skills(tmp_path, builtin_root=None, store=store)
        assert [i["name"] for i in skills.list("worker")] == []
        items = {i["name"]: i for i in skills.list("main")}
        assert items["mainonly"]["allowed_tools"] == ["run_command"]


class TestSkillsWebRoundTrip:
    def test_create_writes_allowed_tools(self, tmp_path, store):
        view = skills_web.create(tmp_path, store, {
            "name": "s1", "description": "d", "body": "正文",
            "allowed_tools": ["run_command", "mcp_x_y"],
        })
        assert view["allowed_tools"] == ["run_command", "mcp_x_y"]
        text = (tmp_path / "skills" / "s1" / "SKILL.md").read_text(encoding="utf-8")
        assert "allowed-tools: run_command mcp_x_y" in text
        # 读回一致
        skills = Skills(tmp_path, builtin_root=None, store=store)
        assert skills.list("worker")[0]["allowed_tools"] == ["run_command", "mcp_x_y"]

    def test_create_without_allowed_tools_no_line(self, tmp_path, store):
        view = skills_web.create(tmp_path, store, {"name": "s0", "description": "d", "body": "b"})
        assert view["allowed_tools"] == []
        text = (tmp_path / "skills" / "s0" / "SKILL.md").read_text(encoding="utf-8")
        assert "allowed-tools" not in text

    def test_update_allowed_tools(self, tmp_path, store):
        skills_web.create(tmp_path, store, {"name": "s2", "description": "d", "body": "b"})
        view = skills_web.update(tmp_path, store, "s2", {"allowed_tools": ["run_command"]})
        assert view["allowed_tools"] == ["run_command"]
        # 再改成空 → 行消失
        view = skills_web.update(tmp_path, store, "s2", {"allowed_tools": []})
        assert view["allowed_tools"] == []
        text = (tmp_path / "skills" / "s2" / "SKILL.md").read_text(encoding="utf-8")
        assert "allowed-tools" not in text

    def test_update_keeps_allowed_tools_when_not_passed(self, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "s3", "description": "d", "body": "b",
            "allowed_tools": ["run_command"],
        })
        view = skills_web.update(tmp_path, store, "s3", {"description": "d2"})
        assert view["allowed_tools"] == ["run_command"]

    def test_unknown_names_kept_verbatim(self, tmp_path, store):
        """认不出的名字（Claude Code 风格 / 未注册 MCP）原样保留，由上层标注。"""
        view = skills_web.create(tmp_path, store, {
            "name": "s4", "description": "d", "body": "b",
            "allowed_tools": ["Bash(git:*)", "mcp_flovia_flovia_x"],
        })
        assert view["allowed_tools"] == ["Bash(git:*)", "mcp_flovia_flovia_x"]

    def test_allowed_tools_accepts_string_form(self, tmp_path, store):
        """接口方传空格分隔字符串也收（和规范写法一致）。"""
        view = skills_web.create(tmp_path, store, {
            "name": "s5", "description": "d", "body": "b",
            "allowed_tools": "run_command  web_search",
        })
        assert view["allowed_tools"] == ["run_command", "web_search"]

    def test_list_view_carries_field(self, tmp_path, store):
        skills_web.create(tmp_path, store, {
            "name": "s6", "description": "d", "body": "b",
            "allowed_tools": ["run_command"],
        })
        views = {v["name"]: v for v in skills_web.list_view(tmp_path, store)}
        assert views["s6"]["allowed_tools"] == ["run_command"]
