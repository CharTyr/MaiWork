"""workers/tools/skills_tools 的专岗护栏测试：

- ToolContext 新增 agent_type / allowed_tools / allowed_skills，None 向后兼容。
- Workers.run 的 tools 名单是硬执行权限：模型捏造未提供工具 → Tools.call 拒绝执行。
- deadline 到期后只剩 submit_result 时可执行，其他工具也被硬权限拦截。
- read_skill / list_skills 按 ctx.allowed_skills 交集（None 通才不变）；
  且仍遵守 skill 全局停用。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from CharTyr_MaiWork.maiwork.skills import Skills, set_disabled
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport, Workers


@dataclass
class FakeChatResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)


class ReplayModels:
    def __init__(self, results):
        self.queue = list(results)
        self.calls = []

    async def chat(self, role=None, messages=None, **kwargs):
        self.calls.append((role, [dict(m) for m in messages], kwargs))
        if not self.queue:
            return FakeChatResult(text="（队列空了）")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _tool_call(name, arguments, call_id="call-1"):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _mk_tools(store):
    t = Tools(store)

    async def web_search(ctx, args):
        return ToolResult(ok=True, output="1. 结果 https://a.com/1", data=[{"url": "https://a.com/1"}])

    async def fetch_page(ctx, args):
        return ToolResult(ok=True, output="正文", data={})

    async def mcp_search(ctx, args):
        return ToolResult(ok=True, output="MCP", data={})

    async def submit_result(ctx, args):
        return ToolResult(
            ok=True, output=args.get("summary", ""),
            data={"summary": args.get("summary", ""), "data": args.get("data"), "evidence": args.get("evidence", [])},
        )

    for name, handler in (("web_search", web_search), ("fetch_page", fetch_page),
                          ("mcp_search", mcp_search), ("submit_result", submit_result)):
        t.register(Tool(name=name, description=name,
                        parameters={"type": "object",
                                    "properties": {"query": {"type": "string"}, "url": {"type": "string"},
                                                   "summary": {"type": "string"}},
                                    "required": []},
                        roles=frozenset({"worker"}), handler=handler, timeout_s=5.0))
    return t


# ----------------------------------------------------------------------
# ToolContext 新字段默认值（向后兼容）
# ----------------------------------------------------------------------

def test_toolcontext_new_fields_default_none():
    ctx = ToolContext(group_id="1")
    assert ctx.agent_type == "task"
    assert ctx.allowed_tools is None
    assert ctx.allowed_skills is None


# ----------------------------------------------------------------------
# Tools.call：allowed_tools 硬权限
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_call_rejects_tool_outside_allowed_tools(store):
    t = _mk_tools(store)
    ctx = ToolContext(group_id="1", role="worker", actor="worker-x",
                      allowed_tools=("web_search", "submit_result"))
    res = await t.call("mcp_search", {"query": "x"}, ctx)
    assert res.ok is False
    assert "不允许" in res.error or "不在" in res.error or "只能" in res.error or "没有" in res.error
    rows = store.read().execute("SELECT ok FROM tool_calls WHERE tool=?", ("mcp_search",)).fetchall()
    assert rows and rows[0]["ok"] == 0


@pytest.mark.asyncio
async def test_tools_call_allowed_none_unchanged_backward_compat(store):
    t = _mk_tools(store)
    ctx = ToolContext(group_id="1", role="worker", actor="w")  # allowed_tools=None
    res = await t.call("web_search", {"query": "x"}, ctx)
    assert res.ok is True


@pytest.mark.asyncio
async def test_tools_call_args_cannot_override_role_or_group(store):
    """工具 args 捏造 role/group 不能越过：role 由 ctx 定，group 由 ctx 定。
    用 read_profile 当探针——它的 handler 只认 ctx.group_id。"""
    t = Tools(store)

    async def read_profile(ctx, args):
        gid = str(args.get("group_id") or ctx.group_id)
        if gid != ctx.group_id:
            return ToolResult(ok=False, output="", error="只能读本群画像")
        return ToolResult(ok=True, output=f"画像（{gid}）")

    t.register(Tool(name="read_profile", description="d",
                    parameters={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker", "main"}), handler=read_profile, timeout_s=5.0))

    ctx = ToolContext(group_id="900000001", role="worker", actor="w",
                      allowed_tools=("read_profile",))
    # 捏造别群 → 拒绝
    res = await t.call("read_profile", {"group_id": "999"}, ctx)
    assert res.ok is False
    # 本群 args 无 group_id → 走 ctx
    res2 = await t.call("read_profile", {}, ctx)
    assert res2.ok is True
    assert "900000001" in res2.output


# ----------------------------------------------------------------------
# Workers.run：模型捏造未提供工具不执行
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workers_run_forged_tool_not_executed(store):
    t = _mk_tools(store)
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("mcp_search", {"query": "x"}, "c1")]),
        FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"}, "c2")]),
    ])
    w = Workers(models, t)
    report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-1",
                         allowed_tools=("web_search", "submit_result"))
    assert report.ok is True
    rows = store.read().execute("SELECT ok, error FROM tool_calls WHERE tool=?", ("mcp_search",)).fetchall()
    assert rows and rows[0]["ok"] == 0
    assert "mcp_search" in rows[0]["error"] or "不允许" in rows[0]["error"] or "不在" in rows[0]["error"]


@pytest.mark.asyncio
async def test_workers_run_agent_type_propagates_to_ctx(store):
    """Workers.run(agent_type=...) → ToolContext.agent_type 传给工具 handler。"""
    seen: list[str] = []
    t = Tools(store)

    async def submit_result(ctx, args):
        seen.append(str(getattr(ctx, "agent_type", "")))
        return ToolResult(ok=True, output="ok",
                          data={"summary": args.get("summary", ""), "data": None, "evidence": []})

    t.register(Tool(name="submit_result", description="d",
                    parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker"}), handler=submit_result, timeout_s=5.0))
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"})])])
    w = Workers(models, t)
    report = await w.run("干活", group_id="1", tools=[], task_id="T-1", agent_type="news")
    assert report.ok is True
    assert seen == ["news"]


@pytest.mark.asyncio
async def test_workers_run_default_agent_type_is_task(store):
    seen: list[str] = []
    t = Tools(store)

    async def submit_result(ctx, args):
        seen.append(str(getattr(ctx, "agent_type", "")))
        return ToolResult(ok=True, output="ok",
                          data={"summary": args.get("summary", ""), "data": None, "evidence": []})

    t.register(Tool(name="submit_result", description="d",
                    parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker"}), handler=submit_result, timeout_s=5.0))
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"})])])
    w = Workers(models, t)
    await w.run("干活", group_id="1", tools=[], task_id="T-1")
    assert seen == ["task"]


@pytest.mark.asyncio
async def test_deadline_wrapup_narrows_allowed_tools(store):
    """时间盒到期：allowed_tools 收窄成只剩 submit_result（新一轮再调别的也会被拒）。

    本测试不触发既有「一轮交不回就失败」的老语义，而是验 allowed_tools 在 wrap-up
    那一刻被改写：用 monkeypatch 在第二轮打开前快照 ctx.allowed_tools。
    """
    t = _mk_tools(store)
    from CharTyr_MaiWork.maiwork import clock
    now = clock.now()
    captured: list[tuple] = []

    original_call = t.call

    async def spy_call(name, args, ctx):
        captured.append((str(name), tuple(ctx.allowed_tools or ())))
        return await original_call(name, args, ctx)

    t.call = spy_call  # type: ignore[method-assign]

    models = ReplayModels([
        # 第一轮且 deadline 已过：workers 在 chat 之前先把 ctx.allowed_tools 收窄
        FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"}, "c1")]),
    ])
    w = Workers(models, t)
    report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-1",
                         deadline_ts=now - 1,  # 已经到期
                         allowed_tools=("web_search", "submit_result"))
    assert report.ok is True
    # submit_result 调用时 allowed_tools 已被收窄成只有 submit_result
    assert captured and captured[0][0] == "submit_result"
    assert captured[0][1] == ("submit_result",)


@pytest.mark.asyncio
async def test_deadline_wrapup_rejects_non_submit_tool_call(store):
    """wrap-up 后模型还想调 web_search（即使在原 allowed_tools 里）→ Tools.call 拒绝。"""
    t = _mk_tools(store)
    from CharTyr_MaiWork.maiwork import clock
    now = clock.now()
    models = ReplayModels([
        # wrap-up 那一轮模型还想调 web_search：被拒；下一轮有机会交回（应当失败，
        # 但我们的关注点是 web_search 这次被拒绝落库）
        FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c1")]),
    ])
    w = Workers(models, t)
    report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-1",
                         deadline_ts=now - 1,
                         allowed_tools=("web_search", "submit_result"))
    # 到期后模型不交 submit_result → 失败（既有语义保留）
    assert report.ok is False
    rows = store.read().execute("SELECT ok, error FROM tool_calls WHERE tool=?", ("web_search",)).fetchall()
    assert rows and rows[0]["ok"] == 0
    assert "不在本轮" in rows[0]["error"] or "名单" in rows[0]["error"]


@pytest.mark.asyncio
async def test_workers_cancelled_error_reraises_not_report(store):
    """asyncio.CancelledError 不许被吞成 WorkerReport：必须向上抛。"""
    t = _mk_tools(store)

    class CancelModels:
        async def chat(self, role=None, messages=None, **kwargs):
            raise asyncio.CancelledError("取消")

    w = Workers(CancelModels(), t)
    with pytest.raises(asyncio.CancelledError):
        await w.run("干活", group_id="1", tools=[], task_id="T-1")


# ----------------------------------------------------------------------
# skills_tools：allowed_skills 交集
# ----------------------------------------------------------------------

def _mk_skills(tmp_path, store):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "s-a").mkdir()
    (root / "s-a" / "SKILL.md").write_text("---\nname: s-a\ndescription: A\n---\nA 内容", encoding="utf-8")
    (root / "s-b").mkdir()
    (root / "s-b" / "SKILL.md").write_text("---\nname: s-b\ndescription: B\n---\nB 内容", encoding="utf-8")
    (root / "s-main").mkdir()
    (root / "s-main" / "SKILL.md").write_text(
        "---\nname: s-main\ndescription: 主模型\nmetadata:\n  maiwork-roles: main\n---\n主模型", encoding="utf-8")
    return Skills(tmp_path, builtin_root=None, store=store)


def _register_skill_tools(t, skills):
    from CharTyr_MaiWork.maiwork.skills_tools import register_skill_tools
    register_skill_tools(t, skills)


@pytest.mark.asyncio
async def test_list_skills_respects_allowed_skills(store, tmp_path):
    skills = _mk_skills(tmp_path, store)
    t = _mk_tools(store)
    _register_skill_tools(t, skills)
    ctx = ToolContext(group_id="1", role="worker", actor="w",
                      allowed_tools=("list_skills",), allowed_skills=("s-a",))
    res = await t.call("list_skills", {}, ctx)
    assert res.ok is True
    names = [i["name"] for i in res.data]
    assert "s-a" in names
    assert "s-b" not in names
    assert "s-main" not in names  # roles=main


@pytest.mark.asyncio
async def test_read_skill_allowed_by_allowed_skills(store, tmp_path):
    skills = _mk_skills(tmp_path, store)
    t = _mk_tools(store)
    _register_skill_tools(t, skills)
    ctx = ToolContext(group_id="1", role="worker", actor="w",
                      allowed_tools=("read_skill",), allowed_skills=("s-a",))
    res = await t.call("read_skill", {"name": "s-a"}, ctx)
    assert res.ok is True
    assert "A 内容" in res.output


@pytest.mark.asyncio
async def test_read_skill_rejected_outside_allowed_skills(store, tmp_path):
    skills = _mk_skills(tmp_path, store)
    t = _mk_tools(store)
    _register_skill_tools(t, skills)
    ctx = ToolContext(group_id="1", role="worker", actor="w",
                      allowed_tools=("read_skill",), allowed_skills=("s-a",))
    res = await t.call("read_skill", {"name": "s-b"}, ctx)
    assert res.ok is False
    assert "s-b" not in res.output


@pytest.mark.asyncio
async def test_read_skill_allowed_skills_none_keeps_old_behavior(store, tmp_path):
    """allowed_skills=None → 通才（照 roles 过滤，不加岗位名单滤网）。"""
    skills = _mk_skills(tmp_path, store)
    t = _mk_tools(store)
    _register_skill_tools(t, skills)
    ctx = ToolContext(group_id="1", role="worker", actor="w")  # allowed_skills=None
    ctx.allowed_tools = None
    res = await t.call("read_skill", {"name": "s-b"}, ctx)
    assert res.ok is True
    assert "B 内容" in res.output


@pytest.mark.asyncio
async def test_read_skill_disabled_globally_blocked_even_if_in_allowed_skills(store, tmp_path):
    skills = _mk_skills(tmp_path, store)
    t = _mk_tools(store)
    _register_skill_tools(t, skills)
    set_disabled(store, "s-a", True)
    ctx = ToolContext(group_id="1", role="worker", actor="w",
                      allowed_tools=("read_skill",), allowed_skills=("s-a",))
    res = await t.call("read_skill", {"name": "s-a"}, ctx)
    assert res.ok is False


@pytest.mark.asyncio
async def test_workers_run_allowed_skills_reach_ctx(store, tmp_path):
    """Workers.run(allowed_skills=...) 传到 ToolContext。"""
    skills = _mk_skills(tmp_path, store)
    t = Tools(store)
    seen: list[object] = []

    async def submit_result(ctx, args):
        seen.append(getattr(ctx, "allowed_skills", "MISSING"))
        return ToolResult(ok=True, output="ok",
                          data={"summary": args.get("summary", ""), "data": None, "evidence": []})

    t.register(Tool(name="submit_result", description="d",
                    parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker"}), handler=submit_result, timeout_s=5.0))
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"})])])
    w = Workers(models, t)
    await w.run("干活", group_id="1", tools=[], task_id="T-1", allowed_skills=("s-a",))
    assert seen == [("s-a",)]


@pytest.mark.asyncio
async def test_hint_only_lists_allowed_skills(store, tmp_path):
    """Workers.run(skills_hint 字符串) 与 allowed_skills 是两路；
    这里 verify 系统提示只带岗位 hint（allowed_skills 的过滤走 skill 工具本身）。"""
    t = Tools(store)
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"})])])

    async def submit_result(ctx, args):
        return ToolResult(ok=True, output="ok",
                          data={"summary": args.get("summary", ""), "data": None, "evidence": []})

    t.register(Tool(name="submit_result", description="d",
                    parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker"}), handler=submit_result, timeout_s=5.0))
    w = Workers(models, t)
    await w.run("干活", group_id="1", tools=[], task_id="T-1",
                skills_hint="- s-a：A\n- s-b：B", allowed_skills=("s-a",))
    system = models.calls[0][1][0]["content"]
    assert "s-a" in system


# ----------------------------------------------------------------------
# 契约修补（review 两条）：workers 每轮默认硬工具名单 + 群绑工具跨群闸
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workers_run_default_allowed_tools_is_requested_plus_submit(store):
    """Workers.run 不传 allowed_tools：默认硬名单 = 请求 tools + submit_result。
    模型捏造名单外工具（即使该工具在 Tools 注册）也被拒——不是只给 spec 提示。"""
    t = _mk_tools(store)
    models = ReplayModels([
        FakeChatResult(tool_calls=[_tool_call("mcp_search", {"query": "x"}, "c1")]),
        FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "ok"}, "c2")]),
    ])
    w = Workers(models, t)
    # 不传 allowed_tools
    report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-1")
    assert report.ok is True
    rows = store.read().execute("SELECT ok FROM tool_calls WHERE tool=?", ("mcp_search",)).fetchall()
    assert rows and rows[0]["ok"] == 0


@pytest.mark.asyncio
async def test_tools_call_worker_group_bound_tool_rejects_crossgroup_args(store):
    """worker 角色调 read_profile 时 args.group_id 越群 → Tools.call 在 handler 前拒。"""
    t = Tools(store)
    called: list[dict] = []

    async def read_profile(ctx, args):
        called.append(dict(args))
        return ToolResult(ok=True, output=f"画像（{args.get('group_id') or ctx.group_id}）")

    t.register(Tool(name="read_profile", description="d",
                    parameters={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker", "main"}), handler=read_profile, timeout_s=5.0))

    ctx = ToolContext(group_id="900000001", role="worker", actor="w")
    res = await t.call("read_profile", {"group_id": "999999"}, ctx)
    assert res.ok is False
    assert called == []  # handler 没有被调用
    rows = store.read().execute("SELECT ok, error FROM tool_calls WHERE tool=?", ("read_profile",)).fetchall()
    assert rows and rows[0]["ok"] == 0


@pytest.mark.asyncio
async def test_tools_call_main_role_can_still_pass_group_id(store):
    """main 角色（验收/计划回合）不在跨群闸内；管理员行为不变。"""
    t = Tools(store)

    async def read_profile(ctx, args):
        return ToolResult(ok=True, output=f"画像（{args.get('group_id') or ctx.group_id}）")

    t.register(Tool(name="read_profile", description="d",
                    parameters={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker", "main"}), handler=read_profile, timeout_s=5.0))

    ctx = ToolContext(group_id="1", role="main", actor="主模型")
    res = await t.call("read_profile", {"group_id": "999999"}, ctx)
    assert res.ok is True  # main 可跨群（老行为）


@pytest.mark.asyncio
async def test_tools_call_worker_own_group_with_explicit_group_id_ok(store):
    """worker 调 read_profile 但 args.group_id == ctx.group_id：放行（越群才拦）。"""
    t = Tools(store)

    async def read_profile(ctx, args):
        return ToolResult(ok=True, output="画像")

    t.register(Tool(name="read_profile", description="d",
                    parameters={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": []},
                    roles=frozenset({"worker", "main"}), handler=read_profile, timeout_s=5.0))

    ctx = ToolContext(group_id="900000001", role="worker", actor="w")
    res = await t.call("read_profile", {"group_id": "900000001"}, ctx)
    assert res.ok is True


@pytest.mark.asyncio
async def test_tools_call_worker_read_chat_history_rejects_crossgroup(store):
    """read_chat_history / search_chat 同属群绑名单。"""
    t = Tools(store)
    seen: list[dict] = []

    async def read_chat_history(ctx, args):
        seen.append(dict(args))
        return ToolResult(ok=True, output="chat")

    t.register(Tool(name="read_chat_history", description="d",
                    parameters={"type": "object",
                                "properties": {"group_id": {"type": "string"}, "hours": {"type": "number"}},
                                "required": []},
                    roles=frozenset({"worker"}), handler=read_chat_history, timeout_s=5.0))

    ctx = ToolContext(group_id="900000001", role="worker", actor="w")
    res = await t.call("read_chat_history", {"group_id": "999", "hours": 1}, ctx)
    assert res.ok is False
    assert seen == []
