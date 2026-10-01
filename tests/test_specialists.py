"""specialists.py 单元测试：Agents 用假实现（A 的 agents.py 可能还没就位），
Workers 用假实现查注入，Tools/Skills 用真实现往 tmp 底座。

测试重点（契约 B 部分）：
- 服务群 / 岗位 enabled 验证在任何记录/模型调用之前；禁用返回失败 report 不换通才。
- 请求 tools 与角色硬白名单取交集，交集记录 handoff.tools。
- 模型「捏造」不在本轮名单里的工具（含越过白名单的工具）不能被执行。
- allowed_skills = 角色名单 ∩ skills.list 动态开关，被手动停用的 skill 立刻出局。
- 取消 → CancelledError 重抛 + handoff 落 cancelled；异常 → failed。绝不 auto accept。
- handoff_id 回填 WorkerReport（默认 ''，老构造不坏）。
- review 直通 Agents.review。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.skills import Skills, set_disabled
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport


# ----------------------------------------------------------------------
# 假 Agents（按契约 agents.py 的接口）
# ----------------------------------------------------------------------


class FakeAgents:
    """按契约接口实现的内存假 Agents。records 顺序记调用，便于断言顺序。"""

    DEFAULT_TOOLS = {
        "news": ("web_search", "fetch_page", "read_profile", "search_chat",
                 "read_chat_history", "list_skills", "read_skill", "submit_result"),
        "idea": ("web_search", "fetch_page", "read_profile", "search_chat",
                 "read_chat_history", "list_skills", "read_skill", "submit_result"),
        "goal": ("web_search", "fetch_page", "read_profile", "search_chat",
                 "read_chat_history", "list_skills", "read_skill", "submit_result"),
        "task": None,
    }

    def __init__(self, served=("900000001",), enabled=True):
        self._served = set(served)
        self._enabled = enabled
        self.records: list[tuple] = []
        self.calls: dict[str, int] = {}
        self._profiles = {
            k: {"kind": k, "title": k, "instructions": "", "skills": [],
                "enabled": enabled, "tools": tools}
            for k, tools in self.DEFAULT_TOOLS.items()
        }
        self._memory: dict[tuple[str, str], dict] = {}
        self._handoffs: dict[str, dict] = {}
        self._counter = 0

    def _tick(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def _verify_served(self, gid):
        if str(gid) not in self._served:
            raise ValueError(f"非服务群：{gid}")

    def _rec(self, tup):
        self.records.append(tup)
        self._tick(tup[0])

    def profiles(self):
        return [dict(p) for p in self._profiles.values()]

    def profile(self, kind):
        p = self._profiles.get(kind)
        if p is None:
            raise ValueError(f"不存在的岗位：{kind}")
        return dict(p)

    def update_profile(self, kind, patch):
        self._rec(("update_profile", kind, patch))
        bad = set(patch) - {"title", "instructions", "skills", "enabled"}
        if bad:
            raise ValueError(f"不允许的字段：{bad}")
        self._profiles[kind].update(patch)
        return dict(self._profiles[kind])

    def memory(self, gid, kind):
        self._verify_served(gid)
        m = self._memory.setdefault((str(gid), str(kind)), {"notes": "", "learned": []})
        return {"notes": m["notes"], "learned": list(m["learned"])}

    def set_notes(self, gid, kind, notes):
        self._rec(("set_notes", gid, kind))
        self._verify_served(gid)
        if len(str(notes)) > 2000:
            raise ValueError("notes 过长")
        self._memory.setdefault((str(gid), str(kind)), {"notes": "", "learned": []})["notes"] = str(notes)
        return self.memory(gid, kind)

    def remember(self, gid, kind, text, refs=(), source_id="", now=None):
        self._rec(("remember", gid, kind))
        self._verify_served(gid)
        if kind == "task":
            return
        m = self._memory.setdefault((str(gid), str(kind)), {"notes": "", "learned": []})
        if source_id:
            for ent in m["learned"]:
                if ent.get("source_id") == source_id:
                    return
        if len(str(text)) > 1200:
            raise ValueError("text 过长")
        m["learned"].append({"text": str(text), "refs": list(refs), "source_id": source_id, "updated": 0.0})
        m["learned"] = m["learned"][-12:]

    def prompt(self, gid, kind):
        self._verify_served(gid)
        m = self._memory.setdefault((str(gid), str(kind)), {"notes": "", "learned": []})
        parts = [f"岗位：{kind}"]
        if m["notes"]:
            parts.append(f"notes: {m['notes']}")
        if m["learned"]:
            parts.append("learned: " + "; ".join(e["text"] for e in m["learned"]))
        return "\n".join(parts)

    def begin(self, gid, kind, brief, *, task_id='', phase='', parent_id='', tools=(), skills=(), criteria=None):
        self._rec(("begin", gid, kind))
        self._verify_served(gid)
        profile = self.profile(kind)
        if not profile.get("enabled"):
            raise ValueError(f"岗位 {kind} 已停用")
        self._counter += 1
        hid = f"H-{self._counter}"
        self._handoffs[hid] = {
            "id": hid, "group_id": str(gid), "kind": kind, "task_id": task_id,
            "phase": phase, "parent_id": parent_id, "status": "queued",
            "tools": list(tools), "skills": list(skills),
        }
        return hid

    def running(self, gid, id):
        self._rec(("running", gid, id))
        self._verify_served(gid)
        h = self._handoffs[str(id)]
        assert h["group_id"] == str(gid)
        if h["status"] != "queued":
            raise ValueError("不是 queued")
        h["status"] = "running"

    def returned(self, gid, id, summary, data=None, evidence=(), *, ok=True, error=''):
        self._rec(("returned", gid, id, ok))
        self._verify_served(gid)
        h = self._handoffs[str(id)]
        assert h["group_id"] == str(gid)
        if h["status"] != "running":
            raise ValueError("不是 running")
        h["status"] = "returned" if ok else "failed"
        h["summary"] = summary
        h["data"] = data
        h["evidence"] = list(evidence)
        h["error"] = error

    def review(self, gid, id, accepted, summary, refs=(), *, learn=True):
        self._rec(("review", gid, id, accepted))
        self._verify_served(gid)
        h = self._handoffs[str(id)]
        assert h["group_id"] == str(gid)
        if h["status"] != "returned":
            raise ValueError("不是 returned")
        h["status"] = "accepted" if accepted else "rejected"
        if accepted and learn and h["kind"] != "task":
            self.remember(gid, h["kind"], str(summary), refs, source_id=str(id))

    def fail(self, gid, id, error, *, state='failed'):
        self._rec(("fail", gid, id, state))
        self._verify_served(gid)
        h = self._handoffs[str(id)]
        assert h["group_id"] == str(gid)
        if h["status"] not in ("queued", "running", "returned"):
            raise ValueError("终态不许改")
        h["status"] = state
        h["error"] = error

    def handoffs(self, gid, kind=None, limit=20):
        self._verify_served(gid)
        items = [dict(h) for h in self._handoffs.values() if h["group_id"] == str(gid)]
        if kind:
            items = [h for h in items if h["kind"] == kind]
        return items[:limit]

    def handoff(self, gid, id):
        self._verify_served(gid)
        h = self._handoffs.get(str(id))
        if h is None or h["group_id"] != str(gid):
            return None
        return dict(h)


# ----------------------------------------------------------------------
# 假 Workers（查 ctx 注入 + 回放 WorkerReport）
# ----------------------------------------------------------------------


@dataclass
class WorkersCall:
    brief: str
    tools: list[str]
    group_id: str
    task_id: str
    skills_hint: str
    actor: str
    agent_type: str
    allowed_tools: object
    allowed_skills: object
    system_extra: str


class FakeWorkers:
    def __init__(self, results=None, fail_exc=None):
        self.queue = list(results or [])
        self.fail_exc = fail_exc
        self.calls: list[WorkersCall] = []

    async def run(self, brief, *, group_id, tools, task_id="", actor="子 agent #1",
                  max_steps=0, output_schema=None, workspace=None, skills_hint=None,
                  system_extra="", deadline_ts=None, artifact_scope=None,
                  agent_type="task", allowed_tools=None, allowed_skills=None):
        self.calls.append(
            WorkersCall(
                brief=brief, tools=list(tools), group_id=group_id, task_id=task_id,
                skills_hint=skills_hint or "", actor=actor,
                agent_type=agent_type,
                allowed_tools=tuple(allowed_tools) if allowed_tools is not None else None,
                allowed_skills=tuple(allowed_skills) if allowed_skills is not None else None,
                system_extra=system_extra,
            )
        )
        if self.fail_exc is not None:
            raise self.fail_exc
        if not self.queue:
            return WorkerReport(ok=True, summary="stub ok")
        r = self.queue.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class FakeSettings:
    def __init__(self, served_gid="900000001"):
        self.groups = {served_gid: object()}

    def is_served(self, gid):
        return str(gid) in self.groups


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def skills(tmp_path, store):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "news-skill").mkdir()
    (root / "news-skill" / "SKILL.md").write_text(
        "---\nname: news-skill\ndescription: 资讯岗技能\n---\nNews 专用。", encoding="utf-8")
    (root / "main-skill").mkdir()
    (root / "main-skill" / "SKILL.md").write_text(
        "---\nname: main-skill\ndescription: 主模型专用\nmetadata:\n  maiwork-roles: main\n---\nMain 专用。",
        encoding="utf-8")
    yield Skills(tmp_path, builtin_root=None, store=store, settings=FakeSettings())


def _mk_specialists(agents, workers, skills):
    from CharTyr_MaiWork.maiwork.specialists import Specialists
    return Specialists(agents, workers, skills)


# ----------------------------------------------------------------------
# 服务群 / 岗位 enabled：在任何记录/模型调用之前
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_kind_returns_failure_before_any_record(store, skills):
    agents = FakeAgents(enabled=False)
    workers = FakeWorkers()
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("news", "找今天的资讯", group_id="900000001")
    assert report.ok is False
    assert "停用" in report.error
    assert agents.calls == {}
    assert workers.calls == []


@pytest.mark.asyncio
async def test_unserved_group_rejected_before_begin(store, skills):
    agents = FakeAgents(served=())
    workers = FakeWorkers()
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("news", "找资讯", group_id="999")
    assert report.ok is False
    assert agents.calls == {}
    assert workers.calls == []


@pytest.mark.asyncio
async def test_unknown_kind_rejected(store, skills):
    agents = FakeAgents()
    sp = _mk_specialists(agents, FakeWorkers(), skills)
    report = await sp.run("planner", "x", group_id="900000001")
    assert report.ok is False
    assert agents.calls == {}


# ----------------------------------------------------------------------
# 工具交集 + 注入 Workers
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_kind_tools_passthrough_and_agent_type(store, skills):
    """task：tools=None（不受岗位白名单裁剪），用请求 tools；agent_type 传给 Workers。"""
    agents = FakeAgents()
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("task", "整理 README", group_id="900000001",
                          tools=["read_file", "write_file", "submit_result"])
    assert report.ok is True
    assert workers.calls[0].tools == ["read_file", "write_file", "submit_result"]
    assert workers.calls[0].agent_type == "task"
    # task kind 也走 begin 记录
    assert agents.calls.get("begin", 0) == 1


@pytest.mark.asyncio
async def test_news_agent_type_and_skills_whitelist_passed(store, skills):
    agents = FakeAgents()
    agents.update_profile("news", {"skills": ["news-skill"]})
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001")
    call = workers.calls[0]
    assert call.agent_type == "news"
    assert call.allowed_skills is not None
    assert "news-skill" in call.allowed_skills
    # main-skill roles=main，不该给 worker
    assert "main-skill" not in call.allowed_skills
    # 角色 system 段（岗位 prompt）注入
    assert "岗位：news" in call.system_extra


@pytest.mark.asyncio
async def test_news_requested_tools_intersect_role_whitelist(store, skills):
    agents = FakeAgents()
    agents.update_profile("news", {"skills": []})
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001",
                 tools=["web_search", "mcp_search", "submit_result"])
    # mcp_search 不在 news 白名单 → 被裁掉
    assert "mcp_search" not in workers.calls[0].tools
    assert "web_search" in workers.calls[0].tools
    # allowed_tools 硬权限也不能含 mcp_search
    assert workers.calls[0].allowed_tools is not None
    assert "mcp_search" not in workers.calls[0].allowed_tools


@pytest.mark.asyncio
async def test_news_default_tools_from_role_whitelist(store, skills):
    """请求 tools=None 时：专岗默认从岗位白名单取（read-only 调研那一套）。"""
    agents = FakeAgents()
    agents.update_profile("news", {"skills": []})
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001")
    tools = workers.calls[0].tools
    assert "web_search" in tools and "fetch_page" in tools
    assert "submit_result" in tools
    assert "write_file" not in tools
    assert "mcp_search" not in tools


@pytest.mark.asyncio
async def test_specialist_no_mcp_even_if_explicitly_requested(store, skills):
    agents = FakeAgents()
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("idea", "出构想", group_id="900000001", tools=["mcp_search"])
    assert workers.calls[0].tools == [] or "mcp_search" not in workers.calls[0].tools


@pytest.mark.asyncio
async def test_task_skills_none_keeps_backward_compat(store, skills):
    """task 岗位没设 skill 名单 → allowed_skills=None 通才，老行为不变。"""
    agents = FakeAgents()
    agents._profiles["task"]["skills"] = None  # task 无岗位名单
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("task", "干活", group_id="900000001", tools=["submit_result"])
    assert workers.calls[0].allowed_skills is None


@pytest.mark.asyncio
async def test_disabled_skill_not_in_allowed_skills(store, skills):
    agents = FakeAgents()
    agents.update_profile("news", {"skills": ["news-skill"]})
    set_disabled(store, "news-skill", True)
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001")
    assert workers.calls[0].allowed_skills is not None
    assert "news-skill" not in workers.calls[0].allowed_skills


@pytest.mark.asyncio
async def test_goal_without_extra_skills_gets_empty_allowed_skills(store, skills):
    """goal profile skills=[]（六家search没列进去也要不到）→ allowed_skills 空名单。"""
    agents = FakeAgents()
    agents.update_profile("goal", {"skills": []})
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("goal", "追目标", group_id="900000001")
    assert workers.calls[0].allowed_skills is not None
    assert len(workers.calls[0].allowed_skills) == 0


# ----------------------------------------------------------------------
# handoff 状态机
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_records_begin_running_returned_no_auto_accept(store, skills):
    agents = FakeAgents()
    agents.update_profile("news", {"skills": []})
    workers = FakeWorkers([
        WorkerReport(ok=True, summary="找到 3 条", data={"n": 3}, evidence=["https://a.com/1"])
    ])
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("news", "找资讯", group_id="900000001",
                          phase="collect", task_id="T-9", parent_id="P-1")
    assert report.ok is True
    kinds = [r[0] for r in agents.records]
    assert kinds == ["update_profile", "begin", "running", "returned"]
    handoffs = agents.handoffs("900000001", "news")
    assert len(handoffs) == 1
    h = handoffs[0]
    assert h["status"] == "returned"
    assert h["phase"] == "collect"
    assert h["task_id"] == "T-9"
    assert h["parent_id"] == "P-1"
    # handoff_id 回填 report，默认 '' 向后兼容
    assert report.handoff_id == h["id"]
    assert isinstance(report.handoff_id, str) and report.handoff_id


@pytest.mark.asyncio
async def test_worker_failure_marks_handoff_failed(store, skills):
    agents = FakeAgents()
    workers = FakeWorkers([WorkerReport(ok=False, summary="模型炸了", error="模型调用失败")])
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("news", "找资讯", group_id="900000001")
    assert report.ok is False
    h = agents.handoffs("900000001", "news")[0]
    assert h["status"] == "failed"
    assert h["error"]
    # 失败也不进记忆（没有 remember 调用）
    assert agents.calls.get("remember", 0) == 0


@pytest.mark.asyncio
async def test_worker_exception_marks_handoff_failed(store, skills):
    agents = FakeAgents()
    workers = FakeWorkers(fail_exc=RuntimeError("workers 炸了"))
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("news", "找资讯", group_id="900000001")
    assert report.ok is False
    assert "炸了" in report.error or "失败" in report.error
    h = agents.handoffs("900000001", "news")[0]
    assert h["status"] == "failed"


@pytest.mark.asyncio
async def test_cancelled_reraises_and_marks_cancelled(store, skills):
    agents = FakeAgents()
    workers = FakeWorkers(fail_exc=asyncio.CancelledError("用户取消"))
    sp = _mk_specialists(agents, workers, skills)
    with pytest.raises(asyncio.CancelledError):
        await sp.run("news", "找资讯", group_id="900000001")
    h = agents.handoffs("900000001", "news")[0]
    assert h["status"] == "cancelled"
    # fail(..., state='cancelled') 被调用
    assert any(r[0] == "fail" and r[3] == "cancelled" for r in agents.records)


@pytest.mark.asyncio
async def test_worker_stopped_by_task_state_marks_cancelled_not_failed(store, skills):
    """外部审查 2026-10-02：子 agent 因任务取消/暂停自己停手 → 交接单记 cancelled（不是 failed）。"""
    agents = FakeAgents()
    workers = FakeWorkers([WorkerReport(ok=False, summary="任务已是「cancelled」，子 agent 停手",
                                        error="任务已取消或结束", stopped=True)])
    sp = _mk_specialists(agents, workers, skills)
    report = await sp.run("task", "干活", group_id="900000001", task_id="T-1")
    assert report.ok is False and report.stopped is True
    h = agents.handoffs("900000001", "task")[0]
    assert h["status"] == "cancelled"


@pytest.mark.asyncio
async def test_report_default_handoff_id_is_empty_string():
    """老调用方 Workers 直接返回的 report 没有 handoff_id 也不炸（默认 ''）。"""
    r = WorkerReport(ok=True, summary="s")
    assert r.handoff_id == ""


# ----------------------------------------------------------------------
# review
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_delegates_to_agents_review_by_id(store, skills):
    agents = FakeAgents()
    sp = _mk_specialists(agents, FakeWorkers(), skills)
    hid = agents.begin("900000001", "news", "找资讯", task_id="T-1")
    agents.running("900000001", hid)
    agents.returned("900000001", hid, "找到 3 条")
    agents.records.clear()
    sp.review("900000001", hid, True, "摘要", refs=("https://a.com",))
    assert agents.calls.get("review", 0) == 1
    assert agents.handoff("900000001", hid)["status"] == "accepted"


@pytest.mark.asyncio
async def test_review_by_report_object(store, skills):
    agents = FakeAgents()
    sp = _mk_specialists(agents, FakeWorkers(), skills)
    hid = agents.begin("900000001", "news", "找资讯")
    agents.running("900000001", hid)
    agents.returned("900000001", hid, "s")
    agents.records.clear()
    report = WorkerReport(ok=True, summary="s")
    report.handoff_id = hid
    sp.review("900000001", report, True, "摘要")
    assert agents.calls.get("review", 0) == 1


# ----------------------------------------------------------------------
# 硬工具执行约束：模型捏造工具不能执行（走真实 Workers + Tools）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_forged_tool_not_executed(store, skills, tmp_path):
    """真 Workers.run：模型回的 tool_call 不在本轮严格名单里 → 拒绝执行并落库失败。"""
    from CharTyr_MaiWork.maiwork.workers import Workers as RealWorkers

    t = Tools(store)

    async def web_search(ctx, args):
        return ToolResult(ok=True, output="1. 结果 https://a.com/1", data=[{"url": "https://a.com/1"}])

    async def mcp_search(ctx, args):
        return ToolResult(ok=True, output="MCP 结果", data={})

    async def submit_result(ctx, args):
        return ToolResult(
            ok=True,
            output=args.get("summary", ""),
            data={"summary": args.get("summary", ""), "data": args.get("data"), "evidence": args.get("evidence", [])},
        )

    for name, handler in (("web_search", web_search), ("mcp_search", mcp_search), ("submit_result", submit_result)):
        t.register(Tool(name=name, description=name,
                        parameters={"type": "object", "properties": {"query": {"type": "string"}, "summary": {"type": "string"}}, "required": []},
                        roles=frozenset({"worker"}), handler=handler, timeout_s=5.0))

    turn = {"n": 0}

    class Models:
        async def chat(self, role=None, messages=None, **kwargs):
            turn["n"] += 1
            if turn["n"] == 1:
                return type("R", (), {"text": "",
                                      "tool_calls": [{"id": "c1", "type": "function",
                                                      "function": {"name": "mcp_search",
                                                                   "arguments": json.dumps({"query": "x"})}}]})()
            return type("R", (), {"text": "",
                                  "tool_calls": [{"id": "c2", "type": "function",
                                                  "function": {"name": "submit_result",
                                                               "arguments": json.dumps({"summary": "ok"})}}]})()

    agents = FakeAgents()
    agents.update_profile("news", {"skills": []})
    real_workers = RealWorkers(Models(), t)
    sp = _mk_specialists(agents, real_workers, skills)
    report = await sp.run("news", "找资讯", group_id="900000001")
    assert report.ok is True
    rows = store.read().execute("SELECT ok, error FROM tool_calls WHERE tool=?", ("mcp_search",)).fetchall()
    assert rows, "mcp_search 调用应留痕"
    assert rows[0]["ok"] == 0


# ----------------------------------------------------------------------
# audit 修补：闸顺序 + 白名单永不放大 + skills 名单永不放大
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unserved_group_does_not_read_profile_before_served_check(store, skills, monkeypatch):
    """run 开头必须先用 memory(gid, kind) 验证服务群，再 profile；
    非服务群连字段/全局 KV 都不该读（Agents.profile 不得被调）。"""
    agents = FakeAgents(served=())

    profile_calls: list[str] = []
    original_profile = agents.profile

    def spy_profile(kind):
        profile_calls.append(str(kind))
        return original_profile(kind)

    agents.profile = spy_profile  # type: ignore[method-assign]

    sp = _mk_specialists(agents, FakeWorkers(), skills)
    report = await sp.run("news", "找资讯", group_id="999")
    assert report.ok is False
    assert profile_calls == []  # profile 未被调（连岗位白名单都不探）


@pytest.mark.asyncio
async def test_non_task_profile_tools_none_is_fail_closed_not_fail_open(store, skills):
    """非 task 岗位 profile.tools=None（job 被改坏）：按岗位默认白名单，不让它变「无上限」。"""
    agents = FakeAgents()
    # 模拟坏 profile：news.tools=None（按契约应是 tuple）
    agents._profiles["news"]["tools"] = None
    agents.update_profile("news", {"skills": []})
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001", tools=["mcp_search", "web_search"])
    # 仍按 news 默认白名单交集：mcp_search 被裁；web_search 通过
    assert "mcp_search" not in workers.calls[0].tools
    assert "web_search" in workers.calls[0].tools


@pytest.mark.asyncio
async def test_non_task_profile_tools_whitelist_cannot_exceed_default(store, skills):
    """job profile.tools 加进了 run_command / vm_run：news 默认上限比它小，按 news 默认收。"""
    agents = FakeAgents()
    # 坏 profile 应当被上限天花板压住
    agents._profiles["news"]["tools"] = ("web_search", "run_command", "vm_run", "submit_result")
    agents.update_profile("news", {"skills": []})
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001")
    tools = workers.calls[0].tools
    assert "run_command" not in tools
    assert "vm_run" not in tools
    assert "web_search" in tools
    assert "submit_result" in tools


@pytest.mark.asyncio
async def test_non_task_profile_skills_none_returns_empty_tuple(store, skills):
    """非 task 岗位 profile.skills=None 也应该拿到 allowed_skills=()（不给 skill），
    不能因为「没写名单」就当「通才全部 skill」放开。"""
    agents = FakeAgents()
    agents._profiles["news"]["skills"] = None  # 坏 profile
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001")
    assert workers.calls[0].allowed_skills is not None
    assert workers.calls[0].allowed_skills == ()


@pytest.mark.asyncio
async def test_non_task_profile_skills_malformed_returns_empty_tuple(store, skills):
    """岗位 skills 字段坏了（不是 list）→ allowed_skills=()，永不 fail-open。"""
    agents = FakeAgents()
    agents._profiles["news"]["skills"] = "news-skill"  # 错写成 str
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("news", "找资讯", group_id="900000001")
    assert workers.calls[0].allowed_skills == ()


@pytest.mark.asyncio
async def test_task_profile_skills_none_keeps_open(store, skills):
    """task 岗位 skills=None 保持向后兼容（=None 通才）——仅限 task。"""
    agents = FakeAgents()
    agents._profiles["task"]["skills"] = None
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("task", "干活", group_id="900000001", tools=["submit_result"])
    assert workers.calls[0].allowed_skills is None


@pytest.mark.asyncio
async def test_task_profile_tools_none_keeps_unrestricted(store, skills):
    """task 岗位 tools=None 不受岗位硬白名单限制（task 是通用执行者）。"""
    agents = FakeAgents()
    workers = FakeWorkers([WorkerReport(ok=True, summary="ok")])
    sp = _mk_specialists(agents, workers, skills)
    await sp.run("task", "干活", group_id="900000001",
                 tools=["read_file", "run_command", "submit_result"])
    # task 的全部请求 tools 通过（但 workers 那层还会把它们当硬名单，所以这是上限）
    assert set(workers.calls[0].tools) == {"read_file", "run_command", "submit_result"}
