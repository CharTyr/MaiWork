"""内置 skill `find-skills`（MaiWork 适配版）+ 排计划提示里的「可用技能」一行（2026-10）。

上游是 vercel-labs/skills 的 skills/find-skills（用户指定 https://www.skills.sh/vercel-labs/skills/find-skills）。
MaiWork 借鉴它的「怎么找、怎么核、怎么给管理员看」，但：

- 只给主模型（roles=main），子是 agent 读不到；
- 不指示自主跑上游 CLI（npx 会下载并执行包），不自动写全局技能目录、不自行安装/更新；
- 外部文档是不可信材料，下载量/星数只是线索不是安全凭证；
- 只推荐，管理员在技能页确认后走已有管理入口；建议只出任务成品/管理员网页，不往群里发；
- 搜索词不带群号/画像/聊天私密内容，本群经验本群隔离。

排计划回合只在「有 main 通用 skill（且当前生效）」时，在提示词里给一行
「技能名：触发说明」，不塞全文；停用后那一行也不再出现，但本群 task 做法照旧可读。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN.parent))

from CharTyr_MaiWork.maiwork import agents as agents_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import skills as skills_mod  # noqa: E402
from CharTyr_MaiWork.maiwork import skills_tools as skills_tools_mod  # noqa: E402
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402
from CharTyr_MaiWork.maiwork.tools import Tools  # noqa: E402
from test_coordinator import (  # noqa: E402
    FakeDelivery,
    FakeWorkers,
    _create_task,
    _plan,
    _review,
)
from test_plan_tools import ScriptedModels, _artifact_writer, _plan_calls, _wire  # noqa: E402

G1 = "900000001"


class _Settings:
    served_groups = (G1,)

    def is_served(self, gid: str) -> bool:
        return str(gid) in self.served_groups


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


# ----------------------------------------------------------------------
# 1. 内置那份本身：front matter / roles / 安全边界文案
# ----------------------------------------------------------------------


def test_builtin_find_skills_is_main_only(tmp_path: Path) -> None:
    sk = skills_mod.Skills(str(tmp_path))
    assert "find-skills" in [i["name"] for i in sk.list("main")]
    assert "find-skills" not in [i["name"] for i in sk.list("worker")]
    assert "find-skills" not in sk.hint("worker")
    assert sk.roles("find-skills") == ["main"]


def test_builtin_find_skills_mentions_source_and_adaptation(tmp_path: Path) -> None:
    text = skills_mod.Skills(str(tmp_path)).read("find-skills")
    assert text
    assert "find-skills" in text
    assert "vercel-labs/skills" in text          # 来源
    assert "MaiWork适配版" in text or "MaiWork 适配版" in text
    assert "skills.sh" in text
    # 上游 CLI 只当生态背景（update/init 也一样提一句），不是给模型的执行指令
    assert "skills find" in text and "skills add" in text
    assert "npx" in text


def test_builtin_find_skills_keeps_safety_rules(tmp_path: Path) -> None:
    text = skills_mod.Skills(str(tmp_path)).read("find-skills")
    # 只推荐、管理员确认后走已有技能管理入口；不自动安装/更新、不跑陌生脚本
    assert "管理员" in text
    assert "只推荐" in text
    for word in ("自动安装", "全局技能目录", "不可信"):
        assert word in text, word
    # 不往群里发、不带群号画像去搜、本群经验本群隔离
    for word in ("不往群里", "群号", "本群"):
        assert word in text, word
    # 下载量只是线索，不是安全凭证；必须看全文/附件/适用性
    for word in ("下载量", "安全凭证", "附件"):
        assert word in text, word
    # 找不到就如实说
    assert "找不到" in text
    # 不让主模型自己干长活：派一个助手
    assert "助手" in text


def test_disabled_find_skills_hidden_from_main(tmp_path: Path, store: Store) -> None:
    sk = skills_mod.Skills(str(tmp_path), store=store)
    skills_mod.set_disabled(store, "find-skills", True)
    assert "find-skills" not in [i["name"] for i in sk.list("main")]
    assert sk.read("find-skills") is None
    assert sk.is_effectively_active("find-skills") is False


# ----------------------------------------------------------------------
# 2. 排计划提示：一行「名字 + 触发说明」，停用即消失；本群做法照旧可读
# ----------------------------------------------------------------------


def _env(store: Store, tmp_path: Path, skills) -> tuple:
    agents = agents_mod.Agents(store, lambda: _Settings())
    agents._ensure_schema()
    tools = Tools(store)
    tools._agents = agents  # noqa: SLF001
    skills_tools_mod.register_skill_tools(tools, skills)
    return agents, tools


@pytest.mark.asyncio
async def test_plan_prompt_shows_find_skills_line_and_reads_group_skill(
    tmp_path: Path, store: Store
) -> None:
    agents, tools = _env(store, tmp_path, skills_mod.Skills(str(tmp_path)))
    agents.skill_add(G1, "task", name="整理报名表", description="按本群惯例整理", body="步骤1：先看群规")

    tool_call = {
        "id": "call-skill-1",
        "type": "function",
        "function": {"name": "read_skill", "arguments": json.dumps({"name": "本群/整理报名表"}, ensure_ascii=False)},
    }
    models = ScriptedModels([{"text": "", "tool_calls": [tool_call]}, _plan(), _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    plans = _plan_calls(models)
    assert len(plans) == 2
    first_prompt = str(plans[0][1][0]["content"])
    # 一行名字 + 触发说明（enabled 且 roles 含 main 才会出现）
    assert "find-skills" in first_prompt
    assert "技能" in first_prompt and "read_skill" in first_prompt
    # 不是全文（不无条件每次塞全文）
    assert "MaiWork适配版" not in first_prompt
    # 真调了 read_skill 读本群做法；第二轮拿到正文
    tool_msgs = [m for m in plans[1][1] if m.get("role") == "tool"]
    assert any("步骤1：先看群规" in str(m.get("content")) for m in tool_msgs)
    assert tasks.get(tid)["status"] == "completed"


@pytest.mark.asyncio
async def test_plan_prompt_hides_disabled_nav_but_group_skill_still_readable(
    tmp_path: Path, store: Store
) -> None:
    skills = skills_mod.Skills(str(tmp_path), store=store)
    skills_mod.set_disabled(store, "find-skills", True)
    agents, tools = _env(store, tmp_path, skills)
    agents.skill_add(G1, "task", name="整理报名表", description="按本群惯例整理", body="步骤1：先看群规")

    models = ScriptedModels([_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    plans = _plan_calls(models)
    assert len(plans) == 1
    prompt = str(plans[0][1][0]["content"])
    assert "find-skills" not in prompt
    # 仍然给了 skill 工具（本群 task 做法）→ 走带工具那条分支，不当空表
    assert plans[0][2]["json_mode"] is False
    names = [t["function"]["name"] for t in (plans[0][2].get("tools") or [])]
    assert names == ["list_skills", "read_skill"]
    assert tasks.get(tid)["status"] == "completed"
