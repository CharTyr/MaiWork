"""真实 app 装配：本群 Agents 要接到同一张工具注册表（技能读取接线收尾，2026-10）。

要钉住的缺口：`skills_tools.list_skills` / `read_skill` 的「本群/…」部分，和
coordinator 排计划门控的 `_group_has_task_skills`，都是从
`getattr(tools, "_agents", None)` 上查本群做法；可是 `MaiWorkApp._wire_specialists`
只把 Agents 挂给了 topics / personal / card_push 三个模块，从来没挂到 `self.tools`。
于是真实启动后主模型永远看不到 / 读不到「本群/<做法>」，而既有测试因为手工写了
`tools._agents = agents` 而假绿。

这份测试**不手工补 `tools._agents`**：一律走 `test_app._app` 的真实装配（假宿主
FakeCtx、未配模型、纯本地），再用真实 ToolContext 调注册表里的工具来验证。

覆盖：
1. 启动后 `app.tools._agents is app.agents`（同一个对象，不另建实例）；
2. G1 有 task 做法、G2 有另一份：主模型（role=main）读得到 G1 的，读不到 G2 的，
   G2 的列表里也不串 G1 的；未服务群一律读不到；
3. 真实子 agent（role=worker、agent_type="news"、allowed_skills=None）看得到本岗做法；
4. 关掉内置 find-skills 后，排计划仍因「本群有 task 做法」给出 list_skills / read_skill；
5. stop 后不持有旧的对象/库，再 start 重新挂一遍新实例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import coordinator, skills as skills_mod
from CharTyr_MaiWork.maiwork.tools import ToolContext
from test_app import G1, G2, _app

G3 = "902106199"  # 未服务群（config 里只服务 G1 / G2）

TASK_SKILL_G1 = "整理报名表"
TASK_SKILL_G2 = "隔壁群独有的活法"
NEWS_SKILL_G1 = "news-本群做法"


def _put_news_body(app, body: str) -> None:
    """news 专岗草稿由启动迁移先建（空正文）：已有就改正文，没有才新建。"""
    cur = app.agents.skills(G1, "news", include_archived=True)
    if cur:
        app.agents.skill_update(G1, cur[0]["id"], body=body, source="admin")
    else:
        app.agents.skill_add(G1, "news", description="资讯岗做法", body=body)


def _add_group_skills(app) -> None:
    """按真实的「本群三份」形状往两个群各放一份做法 + G1 一份专岗做法。"""
    app.agents.skill_add(
        G1, "task", name=TASK_SKILL_G1, description="一类活", body="先建目录再逐条登记"
    )
    _put_news_body(app, "先看原始出处再写")
    app.agents.skill_add(
        G2, "task", name=TASK_SKILL_G2, description="别群的活法", body="别群正文"
    )


def _main_ctx(gid: str = G1) -> ToolContext:
    return ToolContext(group_id=gid, actor="主模型", role="main")


def _plan_names(tools) -> list[str]:
    return [s["function"]["name"] for s in coordinator.main_plan_tool_specs(tools, group_id=G1)]


# ----------------------------------------------------------------------
# 1. 接线 + 主模型读本群 task 做法；跨群 / 未服务群读不到
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_app_start_wires_agents_into_tools_registry(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await app.start()
    try:
        # 接线本体：工具表认的就是这份 Agents（同一个对象，不是另建的实例）
        assert app.agents is not None
        assert getattr(app.tools, "_agents", None) is app.agents
        # 既有接线没被顶掉：专岗仍挂在资讯 / 协调器上
        assert app.feeds._specialists is app.specialists
        assert app.specialists is not None

        _add_group_skills(app)

        # 主模型（默认岗位 task）列得到 / 读得到本群 task 做法
        listed = await app.tools.call("list_skills", {}, _main_ctx(G1))
        assert listed.ok, listed.error
        assert f"本群/{TASK_SKILL_G1}" in listed.output
        # 别的群的做法不串进来；专岗做法主模型也读不到
        assert f"本群/{TASK_SKILL_G2}" not in listed.output
        assert f"本群/{NEWS_SKILL_G1}" not in listed.output

        read = await app.tools.call("read_skill", {"name": f"本群/{TASK_SKILL_G1}"}, _main_ctx(G1))
        assert read.ok, read.error
        assert "先建目录再逐条登记" in read.output

        # 跨群：G2 里读 G1 的做法 → 当「没有」；反过来同理
        cross = await app.tools.call("read_skill", {"name": f"本群/{TASK_SKILL_G1}"}, _main_ctx(G2))
        assert not cross.ok
        assert "先建目录再逐条登记" not in cross.output
        g2_listed = await app.tools.call("list_skills", {}, _main_ctx(G2))
        assert f"本群/{TASK_SKILL_G1}" not in g2_listed.output
        assert f"本群/{TASK_SKILL_G2}" in g2_listed.output

        # 未服务群：一样读不到（零读取、不 leak 名字）
        unserved = await app.tools.call(
            "read_skill", {"name": f"本群/{TASK_SKILL_G1}"}, _main_ctx(G3)
        )
        assert not unserved.ok
        assert "先建目录再逐条登记" not in unserved.output
    finally:
        await app.stop()


# ----------------------------------------------------------------------
# 2. 真实子 agent：按 agent_type 看得到本岗做法（allowed_skills=None）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_worker_news_sees_own_post_skill(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await app.start()
    try:
        _add_group_skills(app)
        ctx = ToolContext(
            group_id=G1,
            actor="子 agent #1",
            role="worker",
            agent_type="news",
            allowed_skills=None,  # 通才白名单：只靠本岗过滤
        )
        listed = await app.tools.call("list_skills", {}, ctx)
        assert listed.ok, listed.error
        assert f"本群/{NEWS_SKILL_G1}" in listed.output
        assert f"本群/{TASK_SKILL_G1}" in listed.output  # 本群通用做法也看得见

        read = await app.tools.call("read_skill", {"name": f"本群/{NEWS_SKILL_G1}"}, ctx)
        assert read.ok, read.error
        assert "先看原始出处再写" in read.output

        # 主模型（岗位 task）读不到专岗做法
        main_read = await app.tools.call(
            "read_skill", {"name": f"本群/{NEWS_SKILL_G1}"}, _main_ctx(G1)
        )
        assert not main_read.ok
    finally:
        await app.stop()


# ----------------------------------------------------------------------
# 3. 关掉 find-skills 后：排计划仍因本群 task 做法给出技能工具
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_find_skills_keeps_group_task_skills_in_plan_tools(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await app.start()
    try:
        # 前提：内置导航 skill 在，且是主模型可用的那份
        assert "find-skills" in [i["name"] for i in app.skills.list("main")]
        assert _plan_names(app.tools) == ["list_skills", "read_skill"]

        skills_mod.set_disabled(app.store, "find-skills", True)
        assert app.skills.list("main") == []  # 全局那一份没了

        _add_group_skills(app)
        # 本群有 kind=task 的 active 做法 → 排计划照样给 list / read
        assert _plan_names(app.tools) == ["list_skills", "read_skill"]
        # 未服务群没有做法 → 不给（不 leak 别群做法）
        assert coordinator.main_plan_tool_specs(app.tools, group_id=G3) == []
    finally:
        await app.stop()


# ----------------------------------------------------------------------
# 4. stop 不持有旧对象 / 旧库；再 start 重新挂一遍新实例
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_then_start_rewires_fresh_agents(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await app.start()
    old_agents = app.agents
    old_tools = app.tools
    assert getattr(old_tools, "_agents", None) is old_agents
    await app.stop()
    # 收干净：工具表和 Agents 都不再被 app 持有
    assert app.tools is None
    assert app.agents is None

    await app.start()
    try:
        assert app.agents is not None and app.agents is not old_agents
        assert app.tools is not None and app.tools is not old_tools
        assert getattr(app.tools, "_agents", None) is app.agents
    finally:
        await app.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_specialists", [False, True])
async def test_context_services_wired_without_specialists(tmp_path, missing_specialists):
    """纯提示词入口和提一嘴都认真实 Agents，不依赖 Specialists 能否构造。"""
    app = _app(tmp_path)
    if missing_specialists:
        app.specialists_factory = lambda *_: None
    await app.start()
    try:
        assert app.agents is not None
        if missing_specialists:
            assert app.specialists is None
        app.agents.group_rules_set(G1, "本群接线标记，只许讲本群的事", updated_by="admin")
        for name in ("tools", "topics", "personal", "card_push", "idea_mention"):
            service = getattr(app, name)
            assert service is not None, name
            assert getattr(service, "_agents", None) is app.agents, name
        for name in ("topics", "personal", "idea_mention"):
            assert "本群接线标记" in getattr(app, name)._group_context_safe(G1, "main"), name
            assert "本群接线标记" not in getattr(app, name)._group_context_safe(G2, "main"), name
    finally:
        await app.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_specialists", [False, True])
async def test_feeds_and_coordinator_see_group_rules_without_specialists(tmp_path, missing_specialists):
    """资讯和派活主模型也要认 app 的同一份 Agents：专岗没就位时本群规矩照样注入。"""
    app = _app(tmp_path)
    if missing_specialists:
        app.specialists_factory = lambda *_: None
    await app.start()
    try:
        app.agents.group_rules_set(G1, "资讯接线标记，只许讲本群的事", updated_by="admin")
        for name in ("feeds", "coordinator"):
            service = getattr(app, name)
            assert service is not None, name
            assert "资讯接线标记" in service._group_context_safe(G1, "news"), name
            assert "资讯接线标记" not in service._group_context_safe(G2, "news"), name
    finally:
        await app.stop()
