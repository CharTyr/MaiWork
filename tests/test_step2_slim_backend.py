"""Step 2 后端减负瘦身测试（TDD）。

覆盖三大模块：
1. 轻任务真快车道（scale == "brief"）：
   - Workers.run(..., allow_plain_reply=True)：无工具调用且有正文时，直接返回 ok=True + data={"reply": ...}，不跑 2 轮催促；
     默认 allow_plain_reply=False 保持原样（2 轮催促后失败）。
   - Specialists.run / Coordinator._run_job 透传 allow_plain_reply，兼容不接收该参数的轻量测试桩。
   - Coordinator._maybe_sync_text_reply：data.reply/text 优先 → 字符串 data → brief 规模下非平凡 summary 自动落盘 reply.md。
   - Coordinator._handle_passed：brief 保留 _review，跳过 _groupspace_round / _remember_round / _summarize_task_to_skill（含 maibot_already_answered 路径）。
2. 自定义专岗自动进入主模型分派提示词（免手改 main/AGENTS.md）：
   - POST /api/agents / agent_create_custom 支持可选 instructions（及 when 兜底）并校验类型与长度（≤3000），profile 键集不变。
   - Identity.agent_init_custom(..., when=...) 写入真实分派条件，DELETE 仍能干净移除自动生成的行。
   - Coordinator._custom_agents_prompt_lines 带上职责/何时分派摘要；_known_dispatch_kinds 排除已停用的自定义专岗。
   - main.md 预设温柔放开「清单中带分派说明的自定义专岗可直接用」。
3. here.now 分享页补齐 guides 与机器人真名 + 群友视图隐藏内部报错：
   - share.render_page(tab="news") 渲染好文（guides）与单条好文高亮，遵守全部无脚本/无外链/防泄漏红线。
   - share.render_page(tab="ideas") 把 `/mw 派活 构想 #N` 排第一，`@<机器人真名> 构想 #N` 排第二；Share 默认视图取真名且不污染原结构。
   - Feeds.news_view(admin=False) 把内部配置/报错批次 note 统一脱敏为「本轮暂无新资讯」，admin=True 保留原文供排障。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import clock, settings_ops, share
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.environments import LocalEnv
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.skills import Skills
from CharTyr_MaiWork.maiwork.specialists import Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport, Workers
from tests import test_coordinator as ce

pytestmark = pytest.mark.asyncio

GID = ce.GID
PASSWORD = "测试密码-step2-12345"


@dataclass
class _FakeChatResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    model: str = "fake-worker"
    prompt_tokens: int = 10
    completion_tokens: int = 5
    raw_message: dict = field(default_factory=dict)


class _ReplayModels:
    def __init__(self, results: list[Any]) -> None:
        self.queue = list(results)
        self.calls: list[tuple[Any, list[dict], dict]] = []

    async def chat(self, role: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        self.calls.append((role, [dict(m) for m in (messages or [])], dict(kwargs)))
        if not self.queue:
            return _FakeChatResult(text="（队列空了）")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_worker_tools(store: Store) -> Tools:
    t = Tools(store)

    async def web_search(ctx, args):
        return ToolResult(ok=True, output="1. 立秋节气 2026-08-07 https://a.example/liqiu")

    async def submit_result(ctx, args):
        return ToolResult(
            ok=True,
            output=str(args.get("summary") or ""),
            data={
                "summary": str(args.get("summary") or ""),
                "data": args.get("data"),
                "evidence": args.get("evidence") or [],
            },
        )

    for name, fn in (("web_search", web_search), ("submit_result", submit_result)):
        t.register(
            Tool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                roles=frozenset({"worker"}),
                handler=fn,
            )
        )
    return t


def _make_settings(ws_root: Path) -> Any:
    cfg = {
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "environments": {"workspace_root": str(ws_root)},
    }
    return load_settings(cfg)[0]


# ===========================================================================
# Module 1: 轻任务真快车道 (scale == "brief")
# ===========================================================================


async def test_workers_allow_plain_reply_vs_default_behavior(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    store.migrate()
    try:
        tools = _make_worker_tools(store)

        # 1) allow_plain_reply=True：直接纯文本作答 → 零催促，ok=True，data={"reply": ...}
        m_fast = _ReplayModels([_FakeChatResult(text="  2026 年立秋是 8 月 7 日。  ")])
        w_fast = Workers(m_fast, tools)
        hist: list[dict] = []
        rep_fast = await w_fast.run(
            "今年立秋哪天？",
            group_id=GID,
            tools=["web_search"],
            allow_plain_reply=True,
            history=hist,
        )
        assert rep_fast.ok is True
        assert rep_fast.summary == "2026 年立秋是 8 月 7 日。"
        assert rep_fast.evidence == []
        assert rep_fast.data == {"reply": "2026 年立秋是 8 月 7 日。"}
        assert len(m_fast.calls) == 1  # 没有 _MAX_NUDGES 催促轮
        assert any(m.get("role") == "assistant" and "立秋" in str(m.get("content") or "") for m in hist)

        # 2) allow_plain_reply=True：先调工具搜一次，第二步纯文本作答 → 2 步完成
        tc = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"query":"2026 立秋"}'},
        }
        m_two = _ReplayModels([
            _FakeChatResult(text="", tool_calls=[tc]),
            _FakeChatResult(text="查过了，2026 年立秋是 8 月 7 日（https://a.example/liqiu）。"),
        ])
        w_two = Workers(m_two, tools)
        rep_two = await w_two.run(
            "今年立秋哪天？",
            group_id=GID,
            tools=["web_search"],
            allow_plain_reply=True,
        )
        assert rep_two.ok is True
        assert rep_two.steps == 2
        assert rep_two.data == {"reply": "查过了，2026 年立秋是 8 月 7 日（https://a.example/liqiu）。"}
        assert len(m_two.calls) == 2

        # 3) allow_plain_reply=True 但模型回了空串 → 仍然判失败（不把空话当答案）
        m_empty = _ReplayModels([_FakeChatResult(text="   ")])
        w_empty = Workers(m_empty, tools)
        rep_empty = await w_empty.run(
            "空回复测试",
            group_id=GID,
            tools=["web_search"],
            allow_plain_reply=True,
        )
        assert rep_empty.ok is False

        # 4) 默认 allow_plain_reply=False：一字不改，催 2 次共调 3 轮后判失败
        m_strict = _ReplayModels([
            _FakeChatResult(text="我只说话不调工具 1"),
            _FakeChatResult(text="我只说话不调工具 2"),
            _FakeChatResult(text="我只说话不调工具 3"),
        ])
        w_strict = Workers(m_strict, tools)
        rep_strict = await w_strict.run(
            "做一份完整报告",
            group_id=GID,
            tools=["web_search"],
        )
        assert rep_strict.ok is False
        assert len(m_strict.calls) == 3
        assert "submit_result" in rep_strict.error
    finally:
        store.close()


async def test_specialists_and_coordinator_thread_allow_plain_reply_and_stub_fallback(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "sp.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        env = LocalEnv(lambda: settings)
        tools = _make_worker_tools(store)
        agents = Agents(store, lambda: settings)
        skills = Skills(tmp_path / "skills", store=store, settings=settings)

        # 1) 真 Specialists + 真 Workers：Coordinator._run_job(scale="brief") 透传 allow_plain_reply=True
        m_worker = _ReplayModels([_FakeChatResult(text="一句话答复：不用带伞。")])
        real_workers = Workers(m_worker, tools)
        sp = Specialists(agents, real_workers, skills, tools=tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks, req="今天出门要带伞吗？", reply=None)
        ws_name = tasks.get(tid)["workspace"]

        coord = ce._build(
            mem_store=store,
            settings=settings,
            env=env,
            tools=tools,
            tasks=tasks,
            goals=goals,
            models=ce.ModelsQueue(),
            workers=real_workers,
        )
        coord._specialists = sp  # noqa: SLF001

        rep = await coord._run_job(  # noqa: SLF001
            brief="今天出门要带伞吗？",
            tools=["web_search"],
            gid=GID,
            tid=tid,
            job_idx=1,
            ws_name=ws_name,
            deliver_kind="text",
            scale="brief",
        )
        assert rep.ok is True
        assert len(m_worker.calls) == 1
        reply_file = env.resolve(ws_name, f"artifacts/{tid}/reply.md")
        assert reply_file.read_text(encoding="utf-8") == "一句话答复：不用带伞。"

        # 2) 最小假桩（不收 allow_plain_reply、没有 **kwargs）不会因 TypeError 崩掉
        class _MinimalWorkersStub:
            async def run(
                self, brief: str, *, group_id: str, tools: list[str], task_id: str = "",
                actor: str = "", max_steps: int = 0, output_schema: Any = None,
                workspace: Any = None, skills_hint: Any = None, system_extra: str = "",
                deadline_ts: Any = None, artifact_scope: Any = None, write_scope: Any = None,
                agent_type: str = "task", allowed_tools: Any = None, allowed_skills: Any = None,
                used_tools: Any = None,
            ) -> WorkerReport:
                return WorkerReport(ok=True, summary="来自最小桩的简短回答")

        sp_stub = Specialists(agents, _MinimalWorkersStub(), skills, tools=tools)
        rep_stub = await sp_stub.run(
            "task", "测试最小桩", group_id=GID, task_id=tid,
            tools=["web_search"], allow_plain_reply=True,
        )
        assert rep_stub.ok is True
        assert rep_stub.summary == "来自最小桩的简短回答"

        class _MinimalSpecialistsStub:
            agents = None

            async def run(
                self, kind: str, brief: str, *, group_id: str, task_id: str = "",
                criteria: Any = None, tools: Any = None, actor: str = "",
                workspace: Any = None, artifact_scope: Any = None, write_scope: Any = None,
                suppress_skills: bool = False,
            ) -> WorkerReport:
                return WorkerReport(ok=True, summary="来自最小专岗桩的答复")

        tid2 = ce._create_task(tasks, req="第二个小问题", reply=None)
        ws_name2 = tasks.get(tid2)["workspace"]
        coord._specialists = _MinimalSpecialistsStub()  # noqa: SLF001
        rep_sp_stub = await coord._run_job(  # noqa: SLF001
            brief="第二个小问题",
            tools=["web_search"],
            gid=GID,
            tid=tid2,
            job_idx=1,
            ws_name=ws_name2,
            deliver_kind="text",
            scale="brief",
        )
        assert rep_sp_stub.ok is True
        assert env.resolve(ws_name2, f"artifacts/{tid2}/reply.md").read_text(encoding="utf-8") == "来自最小专岗桩的答复"
    finally:
        store.close()


async def test_maybe_sync_text_reply_priorities_and_trivial_summary_guard(tmp_path: Path) -> None:
    store = Store(tmp_path / "sync.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        env = LocalEnv(lambda: settings)
        tools = _make_worker_tools(store)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=ce.ModelsQueue(),
            workers=ce.FakeWorkers(),
        )

        # 1) dict data["reply"] 优先级高于 summary
        t1 = ce._create_task(tasks, req="q1", reply=None)
        ws1 = tasks.get(t1)["workspace"]
        await coord._maybe_sync_text_reply(  # noqa: SLF001
            ws1, t1, deliver_kind="text", steps_dir="", scale="brief",
            report=WorkerReport(ok=True, summary="done", data={"reply": "来自 data.reply 的正文"}),
        )
        assert env.resolve(ws1, f"artifacts/{t1}/reply.md").read_text(encoding="utf-8") == "来自 data.reply 的正文"

        # 2) 字符串 data
        t2 = ce._create_task(tasks, req="q2", reply=None)
        ws2 = tasks.get(t2)["workspace"]
        await coord._maybe_sync_text_reply(  # noqa: SLF001
            ws2, t2, deliver_kind="text", steps_dir="", scale="standard",
            report=WorkerReport(ok=True, summary="done", data="  字符串 data 回复  "),
        )
        assert env.resolve(ws2, f"artifacts/{t2}/reply.md").read_text(encoding="utf-8") == "字符串 data 回复"

        # 3) brief 下 summary 非平凡词 → 落盘；平凡状态词不落盘
        for trivial in ("done", "OK", "Completed", "完成", "已完成"):
            tx = ce._create_task(tasks, req="q-trivial", reply=None)
            wsx = tasks.get(tx)["workspace"]
            await coord._maybe_sync_text_reply(  # noqa: SLF001
                wsx, tx, deliver_kind="text", steps_dir="", scale="brief",
                report=WorkerReport(ok=True, summary=trivial, data=None),
            )
            assert not env.resolve(wsx, f"artifacts/{tx}/reply.md").exists(), trivial

        t3 = ce._create_task(tasks, req="q3", reply=None)
        ws3 = tasks.get(t3)["workspace"]
        await coord._maybe_sync_text_reply(  # noqa: SLF001
            ws3, t3, deliver_kind="text", steps_dir="", scale="brief",
            report=WorkerReport(ok=True, summary="周六下午两点开会。", data={}),
        )
        assert env.resolve(ws3, f"artifacts/{t3}/reply.md").read_text(encoding="utf-8") == "周六下午两点开会。"

        # 4) standard 规模下不把 summary 当 reply.md 同步
        t4 = ce._create_task(tasks, req="q4", reply=None)
        ws4 = tasks.get(t4)["workspace"]
        await coord._maybe_sync_text_reply(  # noqa: SLF001
            ws4, t4, deliver_kind="text", steps_dir="", scale="standard",
            report=WorkerReport(ok=True, summary="周六下午两点开会。", data=None),
        )
        assert not env.resolve(ws4, f"artifacts/{t4}/reply.md").exists()
    finally:
        store.close()


async def test_brief_handle_passed_keeps_review_and_skips_heavy_post_rounds(tmp_path: Path) -> None:
    store = Store(tmp_path / "hp.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        env = LocalEnv(lambda: settings)
        tools = _make_worker_tools(store)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)

        plan_brief = json.dumps({
            "criteria": ["直接回答"],
            "scale": "brief",
            "deliver_kind": "text",
            "jobs": [{"brief": "回答问题", "tools": ["web_search"]}],
            "question": None,
        }, ensure_ascii=False)
        review_pass = ce._review(pass_=True, artifact="", review="答得清楚", note="立秋是 8 月 7 日。")

        models = ce.ModelsQueue([plan_brief, review_pass])
        m_worker = _ReplayModels([_FakeChatResult(text="2026 年立秋是 8 月 7 日。")])
        real_workers = Workers(m_worker, tools)
        outbox = ce.FakeOutbox()
        delivery = ce.FakeDelivery()

        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=real_workers,
            outbox=outbox, delivery=delivery,
        )

        called = {"groupspace": 0, "remember": 0, "summarize_skill": 0}

        async def _spy_gs(*a, **kw):
            called["groupspace"] += 1

        async def _spy_rem(*a, **kw):
            called["remember"] += 1

        async def _spy_skill(*a, **kw):
            called["summarize_skill"] += 1

        coord._groupspace_round = _spy_gs  # type: ignore[method-assign]  # noqa: SLF001
        coord._remember_round = _spy_rem  # type: ignore[method-assign]  # noqa: SLF001
        coord._summarize_task_to_skill = _spy_skill  # type: ignore[method-assign]  # noqa: SLF001

        tid = ce._create_task(tasks, req="今年立秋哪天？", reply=None)
        await coord.run_task(tid)

        assert tasks.get(tid)["status"] == "completed"
        ws_done = tasks.get(tid)["workspace"]
        assert env.resolve(ws_done, f"artifacts/{tid}/reply.md").read_text(encoding="utf-8") == "2026 年立秋是 8 月 7 日。"
        # _review 仍然跑了（plan + review 两次主模型 JSON 调用）
        assert len(models.calls) == 2
        # 三项重型收尾全部跳过
        assert called == {"groupspace": 0, "remember": 0, "summarize_skill": 0}

        # 再验 maibot_already_answered=True 早退分支同样不跑 _summarize_task_to_skill
        tid2 = ce._create_task(tasks, req="立秋哪天 2")
        ws2 = tasks.get(tid2)["workspace"]
        await env.write_file(ws2, f"artifacts/{tid2}/reply.md", "8 月 7 日。")
        tasks.transition(tid2, "running", reason="开工")

        async def _already(*a, **kw):
            return True

        coord._maibot_already_answered = _already  # type: ignore[method-assign]  # noqa: SLF001
        await coord._handle_passed(  # noqa: SLF001
            tid2, GID, ws2,
            {"scale": "brief", "deliver_kind": "text"},
            {"passed": True, "review": "ok", "artifact": "", "note": "8 月 7 日"},
        )
        assert called == {"groupspace": 0, "remember": 0, "summarize_skill": 0}
    finally:
        store.close()


# ===========================================================================
# Module 2: 自定义专岗自动进入主模型分派提示词
# ===========================================================================


class _ConsoleCfg:
    def __init__(self, password: str = "") -> None:
        self.password = password


class _SimpleSettings:
    def __init__(self, password: str = "") -> None:
        self.console = _ConsoleCfg(password)
        self.model_list = ()

    def is_served(self, gid: Any) -> bool:
        return str(gid) == GID


class _AgentsSvc:
    def __init__(self, store: Store, settings: Any, agents: Agents, identity: Identity) -> None:
        self.store = store
        self._settings = settings
        self.agents = agents
        self.identity = identity

    def get_settings(self) -> Any:
        return self._settings


async def test_custom_agent_create_instructions_when_stub_and_delete(tmp_path: Path) -> None:
    store = Store(tmp_path / "ag.db")
    store.migrate()
    settings = _SimpleSettings(password=PASSWORD)
    agents = Agents(store, lambda: settings)
    identity = Identity(tmp_path / "data", store, lambda: settings, host=None)
    await identity.ensure_started()
    svc = _AgentsSvc(store, settings, agents, identity)

    server = ConsoleServer(svc)
    test_server = TestServer(server.app)
    client = TestClient(test_server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        await client.post("/api/login", json={"password": PASSWORD})

        # 1) POST /api/agents 带 instructions：落库且 profile 字段集合严格不变
        r1 = await client.post("/api/agents", json={
            "title": "表格员",
            "instructions": "  群友要整理表格、透视或统计数据时派给它  ",
        })
        assert r1.status == 200
        p1 = await r1.json()
        k1 = p1["kind"]
        assert p1["instructions"] == "群友要整理表格、透视或统计数据时派给它"
        expected_keys = {
            "kind", "title", "instructions", "skills", "enabled", "tools",
            "fish_seed", "model", "effort", "backup", "escalate",
        }
        assert set(agents.profile(k1).keys()) == expected_keys

        # main/AGENTS.md 里写的是实际分派说明，不是「什么时候派给它？（请补上）」
        main_md = identity.agent_read("main", "agents")["text"]
        assert f"- 「表格员」({k1})：群友要整理表格、透视或统计数据时派给它" in main_md
        assert f"({k1})：什么时候派给它" not in main_md

        # 2) POST /api/agents 带 when 兜底：同样落进 instructions，DELETE 时能干净清掉该行
        r2 = await client.post("/api/agents", json={
            "title": "翻译官",
            "when": "需要把长篇外文资料译成中文时派给它",
        })
        assert r2.status == 200
        p2 = await r2.json()
        k2 = p2["kind"]
        assert p2["instructions"] == "需要把长篇外文资料译成中文时派给它"
        assert f"- 「翻译官」({k2})：需要把长篇外文资料译成中文时派给它" in identity.agent_read("main", "agents")["text"]

        r_del = await client.delete(f"/api/agents/{k2}")
        assert r_del.status == 200
        assert k2 not in identity.agent_read("main", "agents")["text"]

        # 3) 校验：非字符串或超过 3000 字返回 400，且不留孤儿专岗
        before_kinds = set(agents.custom_kinds())
        r_bad_type = await client.post("/api/agents", json={"title": "坏类型", "instructions": 123})
        assert r_bad_type.status == 400
        r_too_long = await client.post("/api/agents", json={"title": "太长了", "instructions": "字" * 3001})
        assert r_too_long.status == 400
        assert set(agents.custom_kinds()) == before_kinds - {k2}

        # 直接调 settings_ops.agent_create_custom 也做同样校验
        with pytest.raises(settings_ops.OpError) as ei:
            await settings_ops.agent_create_custom(svc, "超长二号", instructions="x" * 3001)
        assert ei.value.status == 400
    finally:
        await client.close()
        store.close()


async def test_coordinator_custom_agents_prompt_lines_and_disabled_guard(tmp_path: Path) -> None:
    store = Store(tmp_path / "cag.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        env = LocalEnv(lambda: settings)
        tools = _make_worker_tools(store)
        agents = Agents(store, lambda: settings)
        skills = Skills(tmp_path / "skills", store=store, settings=settings)
        sp = Specialists(agents, ce.FakeWorkers(), skills, tools=tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)

        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=ce.ModelsQueue(),
            workers=ce.FakeWorkers(),
        )
        coord._specialists = sp  # noqa: SLF001

        p_with = agents.create_custom("表格员")
        agents.update_profile(p_with["kind"], {"instructions": "整理 Excel\n和统计数据时派给它"})
        p_empty = agents.create_custom("杂务员")
        p_off = agents.create_custom("停用员")
        agents.update_profile(p_off["kind"], {"instructions": "已停用", "enabled": False})

        lines = coord._custom_agents_prompt_lines()  # noqa: SLF001
        joined = "\n".join(lines)
        assert f"- 「表格员」kind={p_with['kind']}（职责/何时分派：整理 Excel 和统计数据时派给它）" in joined
        assert f"- 「杂务员」kind={p_empty['kind']}" in joined
        assert f"kind={p_empty['kind']}（" not in joined
        assert p_off["kind"] not in joined

        known = coord._known_dispatch_kinds()  # noqa: SLF001
        assert p_with["kind"] in known
        assert p_empty["kind"] in known
        assert p_off["kind"] not in known

        # main.md 预设：不再死卡「没写进 AGENTS.md 就绝不用」，兼容清单里带分派说明的自定义专岗
        preset_path = Path(__file__).resolve().parents[1] / "maiwork" / "agent_presets" / "main.md"
        preset_text = preset_path.read_text(encoding="utf-8")
        assert "没写的专岗你不会用它" not in preset_text
        assert "下面是你新建的专岗" in preset_text
    finally:
        store.close()


# ===========================================================================
# Module 3: here.now 分享页补齐 guides 与机器人真名 + 群友视图隐藏内部报错
# ===========================================================================


async def test_share_news_renders_guides_and_single_guide_safely() -> None:
    data = {
        "id": GID,
        "name": "测试群",
        "view": {
            "news": [{
                "id": 1,
                "items": [{
                    "id": 11,
                    "title": "今日快讯",
                    "summary": "快讯摘要",
                    "why": "大家在聊",
                    "sources": [{"url": "https://news.example/11", "site": "快讯网"}],
                }],
            }],
            "guides": [{
                "id": 99,
                "title": "Rust 异步深度指南<script>alert(1)</script>",
                "summary": "从 Future 状态机讲起。",
                "why": "群里在写异步服务",
                "profile_ref": "画像敏感词-不许外泄",
                "keywords": ["关键词-不许外泄"],
                "refs": [{"who": "小王", "user_id": "u999", "text": "想看篇讲透 Future 的"}],
                "sources": [
                    {"url": "https://guide.example/rust-async", "site": "技术周刊"},
                    {"url": "javascript:alert(2)", "site": "坏链接"},
                ],
            }],
        },
    }

    full_html = share.render_page(data, tab="news")
    assert "今日快讯" in full_html
    assert "好文" in full_html
    assert "Rust 异步深度指南&lt;script&gt;alert(1)&lt;/script&gt;" in full_html
    assert "从 Future 状态机讲起。" in full_html
    assert 'href="https://guide.example/rust-async"' in full_html
    for bad in ("<script>", "javascript:", "<link", 'src="http', "画像敏感词-不许外泄", "关键词-不许外泄", "u999"):
        assert bad not in full_html

    # 指定 item 命中 guide id → 高亮/单列该篇好文
    single_html = share.render_page(data, tab="news", item="99")
    assert "Rust 异步深度指南" in single_html
    assert "今日快讯" not in single_html


async def test_share_ideas_uses_real_bot_name_and_mw_first(tmp_path: Path) -> None:
    data = {
        "id": GID,
        "name": "测试群",
        "bot_name": "小夏<b>",
        "view": {
            "ideas": [{"id": 12, "title": "做个周报汇总", "body": "自动汇总群聊精华", "state": "new"}],
        },
    }
    html = share.render_page(data, tab="ideas")
    cmd_mw = "/mw 派活 构想 #12"
    cmd_at = "@小夏&lt;b&gt; 构想 #12"
    assert cmd_mw in html
    assert cmd_at in html
    assert "@机器人" not in html
    assert html.index(cmd_mw) < html.index(cmd_at)

    # 验证 Share._default_view_fn 从 svc.host.bot_name_cache 取机器人真名且不污染原视图
    store = Store(tmp_path / "sh.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")

        class _Host:
            bot_name_cache = "麦麦真名"

        class _Profiles:
            def pulse(self, gid, end=None):
                return []

            def get(self, gid):
                return {}

            def focus(self, gid):
                return []

        class _FakeSvcForShare:
            def __init__(self) -> None:
                self.store = store
                self.host = _Host()
                self.profiles = _Profiles()

            def get_settings(self):
                return settings

        pages: list[str] = []

        class _HN:
            async def publish(self, directory: Path) -> dict:
                pages.append((Path(directory) / "index.html").read_text(encoding="utf-8"))
                return {"url": "https://s1.here.now/", "slug": "s1"}

        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, 's', '群A', 'tok1')",
                (GID,),
            )
            conn.execute(
                "INSERT INTO ideas (group_id, icon, kind, title, body, basis, step, effort, state, created, updated)"
                " VALUES (?, 'bulb', 'help', '做一个群日历', '说明', '', '', '', 'new', ?, ?)",
                (GID, clock.now(), clock.now()),
            )
        svc = _FakeSvcForShare()
        svc.feeds = Feeds(  # type: ignore[attr-defined]
            store=store, get_settings=lambda: settings, models=None,
            workers=None, topics=None, profiles=svc.profiles,
        )
        sh = share.Share(store, _HN(), lambda: settings, svc=svc)
        url = await sh.publish(GID, tab="ideas")
        assert url == "https://s1.here.now/"
        assert "/mw 派活 构想 #1" in pages[0]
        assert "@麦麦真名 构想 #1" in pages[0]
        assert pages[0].index("/mw 派活 构想 #1") < pages[0].index("@麦麦真名 构想 #1")
    finally:
        store.close()


async def test_feeds_news_view_scrubs_internal_notes_only_for_non_admin(tmp_path: Path) -> None:
    store = Store(tmp_path / "fd.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")

        class _Profiles:
            def get(self, gid):
                return {}

        feeds = Feeds(
            store=store,
            get_settings=lambda: settings,
            models=None,
            workers=None,
            topics=None,
            profiles=_Profiles(),
        )
        now = clock.now()
        raw_notes = [
            "还没指定联网搜索：请到 设置 → 工具与平台 选一个搜索或 MCP",
            "模型出关注点失败：upstream timeout",
            "搜索用不了（403），RSS 这轮也没取到新条目",
            "子 agent 出了意外：Traceback (most recent call last): RuntimeError",
            "RuntimeException: connection reset",
            "这轮没有够门槛的新资讯",
        ]
        with store.tx() as conn:
            for idx, note in enumerate(raw_notes):
                conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 0, 0, 1, ?, ?)",
                    (GID, now - idx * 10, note, now - idx * 10),
                )

        member_batches = feeds.news_view(GID, admin=False)
        admin_batches = feeds.news_view(GID, admin=True)

        assert [b["note"] for b in admin_batches] == raw_notes
        assert [b["note"] for b in member_batches] == [
            "本轮暂无新资讯",
            "本轮暂无新资讯",
            "本轮暂无新资讯",
            "本轮暂无新资讯",
            "本轮暂无新资讯",
            "这轮没有够门槛的新资讯",
        ]
    finally:
        store.close()
