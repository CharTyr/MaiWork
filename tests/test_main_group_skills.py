"""本群做法（agent_skills）+ 技能工具的接线收尾（2026-10）。

两条召回缺口在这份里钉住：

1. 排计划回合给不给 `list_skills` / `read_skill`，只看「有没有 roles 含 main 的通用 skill」。
   本群 kind=task 的 active 做法（管理员/自我学习总结的「这一类活怎么做」）被漏掉——
   即使 find-skills 停用、数据目录一份 main skill 都没有，本群做法也该让主模型读得到。
   反过来：别的群有做法、本群没有 → 不开工具（不能因为别群有做法就开）；
   archived 不算；本群只有专岗（news/idea/…）做法也不算（主模型读不到它）。

2. `list_skills` / `read_skill` 的本群部分读的是假字段 `ctx.kind`（真实 ToolContext 里
   是 `agent_type`，`kind` 根本不存在），而且 read 的本群分支在 allowed_skills 检查前
   就早返——白名单能被「本群/」名字绕过去。这份统一钉：

   - 只认真实 `ctx.group_id` + `ctx.agent_type`：本群 active 的 task 做法 + 当前岗做法；
   - 归档 / 别的群 / 未服务群一律当「没有」；
   - 岗位白名单同时管 list 和 read，被拒不计 uses；
   - 本群做法只有正文、没有附件，file 参数直接拒（不许悄悄回正文）；
   - 不放开主模型读 worker-only 的通用 skill（既有角色门控不变）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN.parent))

from CharTyr_MaiWork.maiwork import agents as agents_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import coordinator  # noqa: E402
from CharTyr_MaiWork.maiwork import skills as skills_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import skills_tools as skills_tools_mod  # noqa: E402
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools  # noqa: E402

G1 = "900000001"
G2 = "902106124"
G3 = "902106125"  # 未服务群


class _Settings:
    served_groups = (G1, G2)

    def is_served(self, gid: str) -> bool:
        return str(gid) in self.served_groups


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _make_env(store: Store, tmp_path: Path, *, builtin_root=None):
    """空数据目录（默认隔离内置 skill）+ 接上 _agents 的工具表。"""
    agents = agents_mod.Agents(store, lambda: _Settings())
    agents._ensure_schema()
    skills = skills_mod.Skills(str(tmp_path), builtin_root=builtin_root)
    tools = Tools(store)
    tools._agents = agents  # noqa: SLF001
    skills_tools_mod.register_skill_tools(tools, skills)
    return agents, skills, tools


def _plan_names(tools: Tools, gid: str = "") -> list[str]:
    return [s["function"]["name"] for s in coordinator.main_plan_tool_specs(tools, group_id=gid)]


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 1. 排计划门控：本群 task 做法也算「有 skill 可读」
# ----------------------------------------------------------------------


def test_group_task_skill_alone_opens_skill_tools(store: Store, tmp_path: Path) -> None:
    agents, _skills, tools = _make_env(store, tmp_path)
    agents.skill_add(G1, "task", name="整理报名表", description="一类活", body="步骤")
    assert _plan_names(tools, G1) == ["list_skills", "read_skill"]


def test_other_group_skill_does_not_open_tools(store: Store, tmp_path: Path) -> None:
    """别的群有做法、本群没有 → 不开；没给群号 → 不开。"""
    agents, _skills, tools = _make_env(store, tmp_path)
    agents.skill_add(G2, "task", name="整理报名表", description="一类活", body="步骤")
    assert _plan_names(tools, G1) == []
    assert _plan_names(tools) == []


def test_archived_group_skill_does_not_open_tools(store: Store, tmp_path: Path) -> None:
    agents, _skills, tools = _make_env(store, tmp_path)
    sid = agents.skill_add(G1, "task", name="整理报名表", body="步骤")
    agents.skill_update(G1, sid, status="archived")
    assert _plan_names(tools, G1) == []


def test_specialist_group_skill_does_not_open_main_tools(store: Store, tmp_path: Path) -> None:
    """主模型岗位是 task，专岗做法（news）读不到，也就不该为它开工具。"""
    agents, _skills, tools = _make_env(store, tmp_path)
    agents.skill_add(G1, "news", description="", body="资讯做法")
    assert _plan_names(tools, G1) == []


def test_no_skill_anywhere_keeps_empty_table(store: Store, tmp_path: Path) -> None:
    """本群没做法、也没有 main 通用 skill → 空表（排计划照旧一次纯 JSON 调用）。"""
    _agents, _skills, tools = _make_env(store, tmp_path)
    assert _plan_names(tools, G1) == []


def test_disabled_nav_still_opens_for_group_task(store: Store, tmp_path: Path) -> None:
    """find-skills 停用（全局开关）后没有 main 通用 skill，但本群 task 做法仍给 list/read。"""
    agents = agents_mod.Agents(store, lambda: _Settings())
    agents._ensure_schema()
    skills = skills_mod.Skills(str(tmp_path), store=store)
    assert "find-skills" in [i["name"] for i in skills.list("main")]
    skills_mod.set_disabled(store, "find-skills", True)
    assert skills.list("main") == []
    tools = Tools(store)
    tools._agents = agents  # noqa: SLF001
    skills_tools_mod.register_skill_tools(tools, skills)
    agents.skill_add(G1, "task", name="整理报名表", body="步骤")
    assert _plan_names(tools, G1) == ["list_skills", "read_skill"]


# ----------------------------------------------------------------------
# 2. list / read 的本群边界：真实 agent_type + 白名单 + 不计 uses
# ----------------------------------------------------------------------


def test_list_and_read_use_agent_type_and_current_group_kinds(store: Store, tmp_path: Path) -> None:
    agents, _skills, tools = _make_env(store, tmp_path)
    agents.skill_add(G1, "task", name="整理报名表", description="通用做法", body="通用步骤")
    agents.skill_add(G1, "news", description="", body="资讯步骤")
    agents.skill_add(G1, "idea", description="", body="构想步骤")

    ctx = ToolContext(group_id=G1, actor="子 agent #1", role="worker", agent_type="news")
    listed = _run(tools.call("list_skills", {}, ctx))
    assert listed.ok
    assert "本群/整理报名表" in listed.output
    assert "本群/news-本群做法" in listed.output
    assert "本群/idea-本群做法" not in listed.output

    own = _run(tools.call("read_skill", {"name": "本群/news-本群做法"}, ctx))
    assert own.ok and "资讯步骤" in own.output
    other = _run(tools.call("read_skill", {"name": "本群/idea-本群做法"}, ctx))
    assert not other.ok

    # 主模型（role=main，agent_type 默认 task）读本群 task 做法
    main_ctx = ToolContext(group_id=G1, actor="主模型", role="main")
    r = _run(tools.call("read_skill", {"name": "本群/整理报名表"}, main_ctx))
    assert r.ok and "通用步骤" in r.output


def test_allowed_skills_covers_list_and_read_and_no_uses_on_reject(
    store: Store, tmp_path: Path
) -> None:
    agents, _skills, tools = _make_env(store, tmp_path)
    sid = agents.skill_add(G1, "task", name="整理报名表", description="", body="步骤1")

    ctx = ToolContext(group_id=G1, actor="子 agent #1", role="worker", allowed_skills=("别的",))
    listed = _run(tools.call("list_skills", {}, ctx))
    assert "本群/" not in listed.output
    bad = _run(tools.call("read_skill", {"name": "本群/整理报名表"}, ctx))
    assert not bad.ok
    assert agents.skill_get(G1, sid)["uses"] == 0

    ctx2 = ToolContext(
        group_id=G1, actor="子 agent #1", role="worker", allowed_skills=("本群/整理报名表",)
    )
    ok = _run(tools.call("read_skill", {"name": "本群/整理报名表"}, ctx2))
    assert ok.ok and "步骤1" in ok.output
    assert agents.skill_get(G1, sid)["uses"] == 1


def test_group_skill_file_arg_rejected_without_body(store: Store, tmp_path: Path) -> None:
    """本群做法只有正文：file 参数直接拒，不许悄悄把正文回给模型，也不计 uses。"""
    agents, _skills, tools = _make_env(store, tmp_path)
    sid = agents.skill_add(G1, "task", name="整理报名表", description="", body="步骤1")
    ctx = ToolContext(group_id=G1, actor="子 agent #1", role="worker")
    r = _run(tools.call("read_skill", {"name": "本群/整理报名表", "file": "notes.md"}, ctx))
    assert not r.ok
    assert "步骤1" not in r.output
    assert "步骤1" not in (r.error or "")
    assert agents.skill_get(G1, sid)["uses"] == 0


def test_cross_group_and_unserved_group_not_readable(store: Store, tmp_path: Path) -> None:
    agents, _skills, tools = _make_env(store, tmp_path)
    sid = agents.skill_add(G1, "task", name="整理报名表", description="", body="步骤1")
    for gid in (G2, G3):
        ctx = ToolContext(group_id=gid, actor="子 agent #1", role="worker")
        r = _run(tools.call("read_skill", {"name": "本群/整理报名表"}, ctx))
        assert not r.ok, gid
        listed = _run(tools.call("list_skills", {}, ctx))
        assert "本群/" not in listed.output, gid
    assert agents.skill_get(G1, sid)["uses"] == 0


# ----------------------------------------------------------------------
# 3. 角色门控：main 岗看得到/读得到 find-skills，worker 不行；worker-only 对 main 关着
# ----------------------------------------------------------------------


def test_main_sees_find_skills_worker_cannot_read_it(store: Store, tmp_path: Path) -> None:
    _agents, _skills, tools = _make_env(store, tmp_path, builtin_root=skills_mod.BUILTIN_ROOT)
    main_ctx = ToolContext(group_id=G1, actor="主模型", role="main")
    listed = _run(tools.call("list_skills", {}, main_ctx))
    assert "find-skills" in listed.output
    read = _run(tools.call("read_skill", {"name": "find-skills"}, main_ctx))
    assert read.ok and "MaiWork" in read.output

    wctx = ToolContext(group_id=G1, actor="子 agent #1", role="worker")
    wlisted = _run(tools.call("list_skills", {}, wctx))
    assert "find-skills" not in wlisted.output
    wread = _run(tools.call("read_skill", {"name": "find-skills"}, wctx))
    assert not wread.ok


def test_worker_only_generic_skill_stays_closed_to_main(store: Store, tmp_path: Path) -> None:
    d = tmp_path / "skills" / "只给子agent"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: 只给子agent\ndescription: x\nroles: [worker]\n---\n正文", encoding="utf-8"
    )
    _agents, _skills, tools = _make_env(store, tmp_path)
    main_ctx = ToolContext(group_id=G1, actor="主模型", role="main")
    listed = _run(tools.call("list_skills", {}, main_ctx))
    assert "只给子agent" not in listed.output
    read = _run(tools.call("read_skill", {"name": "只给子agent"}, main_ctx))
    assert not read.ok
