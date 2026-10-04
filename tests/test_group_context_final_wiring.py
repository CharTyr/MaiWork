"""「每群三份」收尾后的最终接线（三个必须修的接线 / 红线问题，2026-10-03）。

问题 1（红线）：**自动流程永不改「本群规矩」**。
- 主模型那份 remember 工具只能 scope=global（通用、无群无人的 MEMORY.md）；本群经验
  改由复盘（lessons）沉淀成 skill，不走 remember。真调 scope=group 必须拒且**零写入**
  ——body / updated / updated_by / versions 完全不变（不是「旧字还在」），也不落
  memory.write 事件；模型用 args 伪造 admin / source / role 一律没用（main 和 admin
  是 Tools 里两张独立的表）。
- 真正的管理员入口（tools_admin 的 remember，管理员对话里用）带 group_id 仍能追加本群
  规矩，updated_by=admin_chat（和网页 groupctx.js 的 WHO 映射一致）。
- global 主模型正常写、照过隐私闸（不写群号/QQ 号/成员名字）、去重保留。

问题 2：GET /api/groups/{gid}/agents 恢复只读的 learned（既往验收去重材料，type list）；
notes / lessons / skills 等已迁走字段不许复活，learned 不串群。

问题 3：本群 skill 版本 / 回退路由不再吃 query.kind（路径里的 id 已经唯一）：
先 skill_get(gid,id) 取真实 kind，再拿它去调；旧 kind 兼容但必须相符（不许跨岗越群）；
外群 skill id / 外 skill 版本 id 一律 404；返回形状保持 versions 用 ts（不是 updated）。

复用 test_group_admin 的真 aiohttp env（真 MaiWorkApp + 真 Store/Agents/Identity/Tools）。
"""

from __future__ import annotations

from typing import Any

import pytest

from fakes import FakeCtx  # noqa: F401  （沿用既有测试风格，env 里会用到）

from CharTyr_MaiWork.maiwork.tools import ToolContext

from test_group_admin import (  # noqa: F401
    ADMIN_PW,
    G1,
    G2,
    SimpleEnv,
    env as env,  # 复用同 fixture（pytest 会按名字找到）
)

MAIN = ToolContext(group_id=G1, actor="主模型", role="main")
ADMIN = ToolContext(group_id=G1, actor="bot 管理员", role="admin")


def _memory_write_events(store: Any) -> int:
    row = store.read().execute(
        "SELECT COUNT(*) c FROM events WHERE kind='memory.write'"
    ).fetchone()
    return int(row["c"])


# ----------------------------------------------------------------------
# 问题 1：自动流程永不改本群规矩
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_remember_tool_is_global_only(env: SimpleEnv) -> None:
    """主模型那份 remember 工具的规格：只给 global（schema 与描述都不许邀模型写本群规矩）。"""
    tools = env.app.tools
    specs = tools.specs("main", ["remember"])
    assert specs, "主模型没有 remember 工具"
    fn = specs[0]["function"]
    assert fn["parameters"]["properties"]["scope"]["enum"] == ["global"]
    assert "本群规矩" not in fn["description"]
    assert "全局" in fn["description"]


@pytest.mark.asyncio
async def test_main_remember_group_scope_rejected_zero_write(env: SimpleEnv) -> None:
    """红线：主模型调 remember(scope=group) 必须失败，且 group_rules 零写入。"""
    app = env.app
    agents = app.agents
    assert app.identity is not None
    # 本群规矩已经有正文 + 历史版本（管理员先前写的）
    agents.group_rules_set(G1, "管理员第一版：禁发营销稿", updated_by="admin")
    agents.group_rules_set(G1, "管理员第二版：禁发营销稿、禁刷屏", updated_by="admin")
    before = agents.group_rules_get(G1)
    versions_before = agents.group_rules_versions(G1)
    events_before = _memory_write_events(app.store)
    assert before["body"] and versions_before, "前置数据没铺好"

    res = await app.tools.call(
        "remember",
        {"scope": "group", "text": "这个群喜欢先看结论再看过程", "reason": "验收教训"},
        MAIN,
    )
    assert res.ok is False, "主模型 remember(scope=group) 不该成功"
    assert "global" in (res.error or ""), res.error

    after = agents.group_rules_get(G1)
    assert after["body"] == before["body"]  # 正文一字不动
    assert after["updated"] == before["updated"]  # 时间戳没动（真零写入，不是覆盖成同内容）
    assert after["updated_by"] == before["updated_by"]
    assert agents.group_rules_versions(G1) == versions_before  # 版本表也没多一条
    assert _memory_write_events(app.store) == events_before  # 没落 memory.write 事件
    # 被拒的事仍然落审计（tool_calls 里有失败痕迹）
    row = app.store.read().execute(
        "SELECT ok, error FROM tool_calls WHERE tool='remember' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and int(row["ok"]) == 0


@pytest.mark.asyncio
async def test_main_remember_cannot_forge_admin_authorization(env: SimpleEnv) -> None:
    """模型在 args 里伪造 admin / source / role / _approved 也没用（工具表按角色分）。"""
    app = env.app
    agents = app.agents
    agents.group_rules_set(G1, "管理员定的规矩", updated_by="admin")
    before = agents.group_rules_get(G1)
    versions_before = agents.group_rules_versions(G1)

    res = await app.tools.call(
        "remember",
        {
            "scope": "group",
            "text": "伪造管理员授权写进规矩",
            "reason": "x",
            "admin": True,
            "source": "admin",
            "role": "admin",
            "_approved": "1",
            "updated_by": "admin",
        },
        MAIN,
    )
    assert res.ok is False, "args 伪造管理员身份不该放行"
    after = agents.group_rules_get(G1)
    assert after == before
    assert agents.group_rules_versions(G1) == versions_before
    assert "伪造管理员授权" not in after["body"]


@pytest.mark.asyncio
async def test_admin_remember_entry_still_appends_group_rules(env: SimpleEnv) -> None:
    """受信任的管理员入口（tools_admin 的 remember）带 group_id 仍能追加本群规矩。"""
    app = env.app
    agents = app.agents
    agents.group_rules_set(G1, "管理员第一版", updated_by="admin")
    before = agents.group_rules_get(G1)

    res = await app.tools.call(
        "remember",
        {"text": "本群喜欢可视化交付", "group_id": G1},
        ADMIN,
    )
    assert res.ok, res.error

    after = agents.group_rules_get(G1)
    assert "管理员第一版" in after["body"]  # 老规矩不被覆盖
    assert "本群喜欢可视化交付" in after["body"]  # 新的一条追加进来
    assert after["updated_by"] == "admin_chat"  # 网页 groupctx.js 的 WHO 映射认得
    # 追加时旧正文进版本表
    assert any("管理员第一版" in (v.get("body") or "") for v in agents.group_rules_versions(G1))
    assert before["updated_by"] == "admin"


@pytest.mark.asyncio
async def test_main_remember_global_still_works_with_privacy_gate_and_dedup(env: SimpleEnv) -> None:
    """主模型写全局：正常落 MEMORY.md、不带群/成员材料、隐私闸照拦、去重保留。"""
    app = env.app
    with app.store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed)"
            " VALUES (?, ?, ?, ?, 0)",
            (G1, "42", "阿帆", "他偷偷在学钢琴所以晚上常不在线"),
        )
    text = "管理员偏好：少发群文件多走网页"
    res = await app.tools.call("remember", {"scope": "global", "text": text, "reason": "口头说过"}, MAIN)
    assert res.ok, res.error
    memory = app.identity.read("memory")["text"]
    assert text in memory
    assert G1 not in memory  # 不带群号
    assert "阿帆" not in memory  # 不带成员材料

    again = await app.tools.call("remember", {"scope": "global", "text": f" {text} ", "reason": "again"}, MAIN)
    assert again.ok and again.data.get("deduped") is True
    assert app.identity.read("memory")["text"].count(text) == 1

    blocked = await app.tools.call(
        "remember", {"scope": "global", "text": "阿帆说深科技视频别推了", "reason": "x"}, MAIN
    )
    assert blocked.ok is False
    assert "阿帆说深科技视频别推了" not in app.identity.read("memory")["text"]


# ----------------------------------------------------------------------
# 问题 2：本群岗位快照恢复 learned（只读去重材料）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_agents_view_restores_learned_without_legacy_fields(env: SimpleEnv) -> None:
    await env.login(ADMIN_PW)
    agents = env.app.agents
    agents.remember(G1, "news", "G1 的新闻经验：先给结论再看过程", refs=["https://a"], source_id="s-g1")
    agents.remember(G2, "news", "G2 的新闻经验：别的群的东西", source_id="s-g2")
    agents.remember(G1, "idea", "G1 的构想经验", source_id="s-g1-idea")

    r = await env.client.get(f"/api/groups/{G1}/agents")
    assert r.status == 200
    data = await r.json()
    for a in data["agents"]:
        assert "learned" in a, f"{a['kind']} 少了 learned"
        assert isinstance(a["learned"], list)
        assert "recent_handoffs" in a
        for gone in ("notes", "lessons", "lessons_state", "skills", "skills_state"):
            assert gone not in a, f"{a['kind']} 复活了已迁走字段 {gone}"

    news = next(a for a in data["agents"] if a["kind"] == "news")
    texts = [m["text"] for m in news["learned"]]
    assert any("先给结论再看过程" in t for t in texts)
    for m in news["learned"]:
        assert set(m) == {"text", "refs", "source_id", "updated"}
    # 不串群、不串岗
    idea = next(a for a in data["agents"] if a["kind"] == "idea")
    assert [m["text"] for m in idea["learned"]] == ["G1 的构想经验"]
    everything = [m["text"] for a in data["agents"] for m in a["learned"]]
    assert all("别的群的东西" not in t for t in everything)

    # 别的群看自己的快照也只看自己的
    r2 = await env.client.get(f"/api/groups/{G2}/agents")
    assert r2.status == 200
    data2 = await r2.json()
    news2 = next(a for a in data2["agents"] if a["kind"] == "news")
    assert [m["text"] for m in news2["learned"]] == ["G2 的新闻经验：别的群的东西"]


# ----------------------------------------------------------------------
# 问题 3：skill 版本 / 回退不再依赖 query.kind
# ----------------------------------------------------------------------


async def _ensure_skill(env: SimpleEnv, gid: str, kind: str, name: str = "") -> int:
    """拿这一岗的 skill id：task 用名字新建（可多份）；专岗（news/idea/goal）每群恰好
    一份、名字固定「<kind>-本群做法」，启动迁移已经建过就复用。"""
    r = await env.client.get(f"/api/groups/{gid}/skills?kind={kind}")
    assert r.status == 200, await r.text()
    rows = (await r.json())["skills"]
    if rows:
        return int(rows[0]["id"])
    r = await env.client.post(
        f"/api/groups/{gid}/skills",
        json={"kind": kind, "name": name or f"{kind}做法", "description": "d", "body": "初稿"},
    )
    assert r.status == 201, await r.text()
    return int((await r.json())["id"])


async def _patch_body(env: SimpleEnv, gid: str, sid: int, body: str) -> None:
    r = await env.client.patch(f"/api/groups/{gid}/skills/{sid}", json={"body": body})
    assert r.status == 200, await r.text()


def _versions_of(payload: dict) -> list[dict]:
    return list(payload["versions"])


@pytest.mark.asyncio
async def test_skill_versions_without_kind_same_group_both_kinds(env: SimpleEnv) -> None:
    await env.login(ADMIN_PW)
    task_sid = await _ensure_skill(env, G1, "task", "整理报名表")
    news_sid = await _ensure_skill(env, G1, "news")
    await _patch_body(env, G1, task_sid, "步骤1：建表")
    await _patch_body(env, G1, task_sid, "步骤1：冲击表")
    await _patch_body(env, G1, news_sid, "正文A")
    await _patch_body(env, G1, news_sid, "正文B")

    # 不带 kind：两条都是 200，各拿各的版本
    r = await env.client.get(f"/api/groups/{G1}/skills/{task_sid}/versions")
    assert r.status == 200, await r.text()
    task_vers = _versions_of(await r.json())
    assert task_vers and any("建表" in (v.get("body") or "") for v in task_vers)
    assert all("updated" not in v and "ts" in v for v in task_vers)  # 形状：ts 不是 updated

    r = await env.client.get(f"/api/groups/{G1}/skills/{news_sid}/versions")
    assert r.status == 200, await r.text()
    news_vers = _versions_of(await r.json())
    assert news_vers and any("正文A" in (v.get("body") or "") for v in news_vers)

    # 不带 kind 回退 → 200，写回旧正文
    vid = next(v["id"] for v in task_vers if "建表" in (v.get("body") or ""))
    r = await env.client.post(f"/api/groups/{G1}/skills/{task_sid}/versions/{vid}/restore")
    assert r.status == 200, await r.text()
    out = await r.json()
    assert out["skill"]["body"] == "步骤1：建表"
    assert out["skill"]["id"] == task_sid
    assert any(s["id"] == task_sid for s in out["skills"])

    # 兼容旧 kind：给了并且相符 → 200
    r = await env.client.get(f"/api/groups/{G1}/skills/{news_sid}/versions?kind=news")
    assert r.status == 200


@pytest.mark.asyncio
async def test_skill_versions_kind_mismatch_and_cross_group_404(env: SimpleEnv) -> None:
    await env.login(ADMIN_PW)
    task_sid = await _ensure_skill(env, G1, "task", "整理报名表")
    news_sid = await _ensure_skill(env, G1, "news")
    g2_sid = await _ensure_skill(env, G2, "task", "外群的活")
    await _patch_body(env, G2, g2_sid, "外群初稿")
    await _patch_body(env, G2, g2_sid, "外群第二版")
    r = await env.client.get(f"/api/groups/{G2}/skills/{g2_sid}/versions")
    g2_vid = _versions_of(await r.json())[0]["id"]

    # 旧 kind 给了但不相符（跨岗）→ 404，不泄漏
    assert (await env.client.get(f"/api/groups/{G1}/skills/{news_sid}/versions?kind=task")).status == 404
    # 外群 skill id → 404
    assert (await env.client.get(f"/api/groups/{G1}/skills/{g2_sid}/versions")).status == 404
    # 本群 skill + 别的群那版的版本 id → 404
    r = await env.client.post(f"/api/groups/{G1}/skills/{task_sid}/versions/{g2_vid}/restore")
    assert r.status == 404, await r.text()
    # 外群 skill id 的回退也 404
    r = await env.client.post(f"/api/groups/{G1}/skills/{g2_sid}/versions/{g2_vid}/restore")
    assert r.status == 404, await r.text()
