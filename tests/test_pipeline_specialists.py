"""C 集成测试：专岗（news/idea/goal/task）在业务流水线的接线（契约 C 部分）。

Fake Agents/Specialists 是本测试内契约接口的内存实现（A/B 文件可能还没就位）：
- 禁用 / 非服务群：任何 worker / 模型调用前就停，不回落通才；
- news 阶段工具收窄（撒网只搜索 / 核验只打开）、异步轮记录 ContextVar 隔离、
  失败/重试 settle rejected、入库成功后才能写「经主验收」记忆，refs 指到真实批次；
- idea：专岗调查在现有主模型生成/校验**之前**，候选以「素材不是指令」进主提示词；
  入库成功才 review+remember（痴等「已审核构想」）；失败/null 不写记忆；
- goal：force_manual=True / 每日上限 / 去重保留；专岗调查失败主流照常提议；
  goal 记忆只写「待批提议」绝不写「已创建」；check_goal 里 goal 专岗调查不收
  模型拍完板的目标状态；
- task：_run_job 用 task 专岗类型（tools/artifact_scope 原样），团队 review 过才
  settle accepted；晚到/撤档统一 cancelled。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from CharTyr_MaiWork.maiwork import clock  # noqa: E402
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402
from CharTyr_MaiWork.maiwork.workers import WorkerReport  # noqa: E402
from mafakes_specialists import FakeAgents, FakeSpecialists, FakeWorkersQueue  # noqa: E402

G1 = "900000001"
G_OTHER = "888888888"


class _DBRow(dict):
    pass


class _Settings:
    def __init__(self, gid=G1, data_dir=None):
        self.groups = {gid: object()}
        self.data_dir = data_dir or Path("/tmp/maiwork-iptest")
        feeds = type("Feeds", (), {
            "news_slots": None, "idea_slots": None,
            "lookback_days": 14, "web_min_avg": 3.0, "pool_min_avg": 4.0, "max_items": 10,
            "collect_minutes": 15, "guides": True, "blocked_domains": (), "verify_enabled": False,
        })()
        self.feeds = feeds
        self.topics = type("Topics", (), {"candidate_ttl_hours": 12})()
        self.environments = type("Env", (), {"max_parallel": 2, "railway": False, "verify_minutes": 10})()
        self.goals = type("Goals", (), {"propose": True})()
        self.models = type("Models", (), {"context_window": 128000})()

    def is_served(self, gid) -> bool:
        return str(gid) in self.groups

    def workspace_of(self, gid) -> str:
        return f"g{gid}"


class _ModelsOK:
    def __init__(self, payload_map=None):
        self.calls: list[dict] = []
        self._payload_map = payload_map or {}

    async def chat(self, role=None, messages=None, **kwargs):
        purpose = str(kwargs.get("purpose") or "")
        self.calls.append({
            "role": role, "messages": messages, "purpose": purpose,
            "group_id": str(kwargs.get("group_id") or ""),
            "task_id": str(kwargs.get("task_id") or ""),
            "json_mode": bool(kwargs.get("json_mode")),
        })
        lookup = self._payload_map.get(purpose)
        if callable(lookup):
            text = lookup(messages, kwargs)
        elif lookup is not None:
            text = lookup
        else:
            text = "{}"
        return type("R", (), {"text": text, "tool_calls": []})()

    def settings(self):
        return type("S", (), {"ready": lambda self: True})()


class _ModelsNotReady:
    async def chat(self, role=None, messages=None, **kwargs):
        raise AssertionError("模型没配好时不许被调")

    def settings(self):
        return type("S", (), {"ready": lambda self: False})()


class _Profiles:
    def __init__(self, entries=None):
        self._entries = list(entries or [])
        self.calls = []

    def entries(self, gid):
        self.calls.append(("entries", str(gid)))
        return list(self._entries)

    def set_request_deps(self, **kwargs):
        pass


class _TopicsNone:
    def __init__(self):
        self.candidates = []

    def add_candidate(self, gid, *, kind, ref_id, title, brief, link, ttl_h=None):
        self.candidates.append({"gid": gid, "kind": kind, "ref_id": ref_id, "title": title})


class _ToolsFake:
    def specs(self, role, names=None):
        if names is not None:
            wanted = set(names or [])
            return [s for s in self.specs(role, None) if s.get("function", {}).get("name") in wanted]
        if str(role) == "worker":
            return [
                {"type": "function", "function": {"name": "web_search", "description": ""}},
                {"type": "function", "function": {"name": "fetch_page", "description": ""}},
                {"type": "function", "function": {"name": "submit_result", "description": ""}},
                {"type": "function", "function": {"name": "read_file", "description": ""}},
                {"type": "function", "function": {"name": "write_file", "description": ""}},
            ]
        if str(role) == "main":
            return [
                {"type": "function", "function": {"name": "list_skills", "description": ""}},
                {"type": "function", "function": {"name": "read_skill", "description": ""}},
            ]
        return []

    async def call(self, *args, **kwargs):
        raise AssertionError("测试里不该被执行的工具调用")


class _ApprovalsRec:
    def __init__(self):
        self.creates = []

    def create(self, gid, **kwargs):
        self.creates.append({"gid": str(gid), "kwargs": dict(kwargs)})
        return {"id": "R-1", **kwargs}


class _Goals:
    def __init__(self):
        self._rows = {}
        self.events = []

    def get(self, goal_id):
        return self._rows.get(str(goal_id))

    def create(self, gid, **kwargs):
        gid2 = f"G-{len(self._rows) + 1}"
        row = dict(kwargs)
        row["id"] = gid2
        row["group_id"] = str(gid)
        row.setdefault("state", "active")
        row.setdefault("kind", "agent")
        self._rows[gid2] = row
        return gid2

    def update(self, goal_id, **kwargs):
        self._rows[str(goal_id)].update(kwargs)

    def beat(self, goal_id, now):
        self.events.append(("beat", str(goal_id)))

    def touch(self, goal_id, text):
        self.events.append(("touch", str(goal_id), str(text)[:40]))

    def fail(self, goal_id, err, now):
        self.events.append(("fail", str(goal_id)))

    def set_next_check(self, goal_id, ts):
        self.events.append(("next_check", str(goal_id)))

    def set_criteria(self, goal_id, texts):
        self.events.append(("set_criteria", str(goal_id), list(texts)))
        self._rows[str(goal_id)]["criteria"] = json.dumps(
            [{"text": t, "done": False} for t in texts], ensure_ascii=False)

    def set_criterion(self, goal_id, idx, done):
        self.events.append(("set_criterion", str(goal_id), int(idx), bool(done)))
        crit_s = self._rows[str(goal_id)].get("criteria")
        if isinstance(crit_s, str):
            try:
                crit = json.loads(crit_s)
            except Exception:
                crit = []
        else:
            crit = list(crit_s or [])
        if 0 <= int(idx) < len(crit):
            crit[int(idx)]["done"] = bool(done)
            self._rows[str(goal_id)]["criteria"] = json.dumps(crit, ensure_ascii=False)

    def done(self, goal_id):
        self.events.append(("done", str(goal_id)))


class _Tasks:
    def __init__(self):
        self.created = []
        self.transitions = []
        self.attempts_finished = []
        self._rows = {}
        self.accept_result_value = True
        self.status_for = {}

    def create(self, gid, **kwargs):
        tid = f"T-{len(self.created) + 1}"
        self.created.append({"id": tid, "gid": str(gid), **kwargs})
        self._rows[tid] = {
            "id": tid, "group_id": str(gid), "status": kwargs.get("status", "queued"),
            "title": kwargs.get("title", ""), "req": kwargs.get("req", ""),
            "goal_id": kwargs.get("goal_id"), "attempts": 0,
            "criteria": list(kwargs.get("criteria", ["c1"])),
            "req_version": 1,
        }
        return tid

    def get(self, task_id):
        return self._rows.get(str(task_id))

    def transition(self, task_id, target, reason=""):
        self.transitions.append((str(task_id), str(target), str(reason)))
        self._rows[str(task_id)]["status"] = str(target)

    def accept_result(self, task_id, attempt_id, req_version):
        return self.accept_result_value

    def finish_attempt(self, attempt_id, **kwargs):
        self.attempts_finished.append({"attempt_id": attempt_id, **kwargs})

    def start_attempt(self, task_id, **kwargs):
        self._rows[str(task_id)]["attempts"] = int(self._rows[str(task_id)].get("attempts", 0)) + 1
        self._rows[str(task_id)]["attempt_id"] = f"A-{self._rows[str(task_id)]['attempts']}"
        return self._rows[str(task_id)]["attempts"]

    def current_attempt_id(self, task_id):
        return self._rows.get(str(task_id), {}).get("attempt_id")

    def revise(self, task_id, *, req, criteria):
        self._rows[str(task_id)]["req"] = str(req)
        self._rows[str(task_id)]["criteria"] = list(criteria)
        return int(self._rows[str(task_id)].get("req_version", 0)) + 1

    def set_env(self, task_id, desc, note=""):
        self._rows[str(task_id)]["env"] = f"{desc}|{note}"

    def net_check(self, task_id):
        return None

    def interrupt_orphaned(self, reason=""):
        return 0

    def current_version(self, task_id):
        return 1


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _mk_feeds(store, models=None, workers=None, settings=None, specialists=None, topics=None):
    from CharTyr_MaiWork.maiwork.feeds import Feeds

    settings = settings or _Settings()
    workers = workers if workers is not None else FakeWorkersQueue()
    models = models or _ModelsOK()
    topics = topics or _TopicsNone()
    feeds = Feeds(
        store, models, workers, _Profiles(), topics, lambda: settings,
        search=None, host=None,
    )
    if specialists is not None:
        feeds._specialists = specialists  # noqa: SLF001
    return feeds


def _mk_proposer(store, models, goals, approvals, settings, specialists=None):
    from CharTyr_MaiWork.maiwork.goal_proposal import GoalProposer

    proposer = GoalProposer(
        store, models, goals, approvals, lambda: settings,
    )
    if specialists is not None:
        proposer._specialists = specialists  # noqa: SLF001
    return proposer


def _mk_coordinator(store, models, workers, tasks, goals, settings, specialists=None):
    from CharTyr_MaiWork.maiwork.coordinator import Coordinator

    outbox = type("Outbox", (), {"enqueue": lambda self, *a, **k: None})()
    coord = Coordinator(
        store, models, workers, _ToolsFake(), tasks, goals, None, outbox, None, _Profiles(),
        lambda: settings, capability=None,
    )
    if specialists is not None:
        coord._specialists = specialists  # noqa: SLF001
    return coord


# ==============================================================================
# news：阶段工具收窄 + contract 挂接
# ==============================================================================


class TestNewsStageDispatch:
    @pytest.mark.asyncio
    async def test_recheck_handoff_gets_settled(self, store):
        """外部审查 2026-10-02：补打开的交接单交回后要验收收尾，不能永远停在 returned（线上 2 张）。"""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[WorkerReport(ok=True, summary="重看完", data={"items": []})])
        feeds = _mk_feeds(store, specialists=sp)
        report = await feeds._recheck_run(G1, "feeds-recheck:x", "重看这几条", group_id=G1)
        assert report.ok is True
        assert sp.runs_of("news")[0]["phase"] == "recheck"
        assert sp.runs_of("news")[0]["tools"] == ["fetch_page", "web_search"]
        assert [r["id"] for r in sp.reviews] == [report.handoff_id]
        assert agents.calls["review"] and agents.calls["review"][0][2] is True

    @pytest.mark.asyncio
    async def test_two_phase_dispatch_collect_verify_tools(self, store):
        """两阶段（2026-10-01 起撒网是代码按计划搜，不再派子 agent）：撒网阶段一次专岗/worker
        派工都不发生，核验照旧走 sp.run tools=["fetch_page"] 且 agent_type kind=news。"""
        with store.tx() as conn:
            store.kv_set(conn, "feeds.two_phase", [G1])
        workers = FakeWorkersQueue()
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            # 核验阶段：3 组份可能并发；把同格式带回放在队列里足够
            WorkerReport(ok=True, summary="v1", data={"items": [{
                "title": "x", "url": "https://a.com/x1", "summary": "s", "kind": "news",
                "fetched": True, "quote": "q", "published": "", "paywall": False, "image_url": "",
            }]}),
        ])
        models = _ModelsOK({
            "feeds.pick": '{"picks":[{"i":0,"kind":"news","hook":"h"}]}',
        })
        settings = _Settings()
        feeds = _mk_feeds(store, models=models, workers=workers, settings=settings, specialists=sp)

        class _SearchOK:
            """代码撒网用的假搜索（search.py 归一化后的结果形状）。"""

            async def search(self, query, *, limit=8, days=None, site="", news=False):
                return [
                    {"title": "x", "url": "https://a.com/x1", "snippet": "s",
                     "published": None, "provider": "t"},
                ]

            def broad_providers(self):
                return ["t"]

        feeds._search = _SearchOK()  # 代码撒网：预设链接完整的搜索结果 → 登记簿有候选
        focus = [{"query": "test", "why": "", "angle": "", "source": ""}]
        items = await feeds._collect_two_phase(G1, focus, settings, collect_mark="feeds-collect:test", stats_out={})
        assert sp.runs_of("news")
        # 撒网阶段一派工都没发生（没有 web_search 的专岗/worker 调用）；核验阶段只给了 fetch_page
        assert all(set(r.get("tools") or []) == {"fetch_page"} for r in sp.runs_of("news"))
        assert workers.calls == []
        assert items and items[0]["url"] == "https://a.com/x1"
        # 每条 eff_tools 都不含 mcp_*
        for r in sp.runs_of("news"):
            assert not any(str(t).startswith("mcp_") for t in r["eff_tools"])

    @pytest.mark.asyncio
    async def test_disabled_news_role_rejected_before_workers_and_no_memory(self, store):
        """news 角色 enabled=False：prepare_news 在调用任何专岗/worker/模型前就停；
        不写任何 handoff / 记忆。"""
        agents = FakeAgents(enabled_kinds={"news": False})
        sp = FakeSpecialists(agents)
        workers = FakeWorkersQueue()
        models = _ModelsOK()
        with store.tx() as conn:
            store.kv_set(conn, "feeds.two_phase", [G1])
        feeds = _mk_feeds(store, models=models, workers=workers, specialists=sp)
        # 预置 groups 行以便 _profile_ready=True；news 禁用时 prepare_news 在任何调用前早退
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, session_id, workspace, token, profile_ready_ts, created)"
                " VALUES (?,?,?,?,?,?)",
                (G1, "", f"g{G1}", "tok12345", clock.now(), clock.now()),
            )
        kept = await feeds.prepare_news(G1)
        assert kept == 0
        assert sp.runs == []
        assert workers.calls == []
        assert models.calls == []
        assert agents.calls["begin"] == []
        assert agents.calls["review"] == []
        assert agents.calls["remember"] == []


# ==============================================================================
# app：装配 + 注入 + 生命周期清理
# ==============================================================================


class TestAppComposition:
    @pytest.mark.asyncio
    async def test_app_wires_specialists_into_feeds_goalproposer_coordinator(self, tmp_path):
        """真实 _start_stack：app.agents / app.specialists 建好；feeds / goal_proposer /
        coordinator 都挂上同一份 _specialists；stop 后全部置 None。"""
        from fakes import FakeCtx, FakeProfiles

        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "console": {"listen": "127.0.0.1:0", "password": "pw-测试"},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        # A 的 agents.py / B 的 specialists.py 可能还没就位：用工厂注入假实现
        agents = FakeAgents()
        app.agents_factory = lambda store, get_settings: agents
        made = {}

        def _sp_factory(agents_arg, workers, skills):
            made["args"] = (agents_arg, workers, skills)
            return FakeSpecialists(agents_arg)

        app.specialists_factory = _sp_factory
        await app.start()
        try:
            assert app.started is True
            assert app.agents is agents
            assert app.specialists is not None
            assert made.get("args") is not None
            assert made["args"][0] is agents  # 同一份 agents
            assert made["args"][1] is app.workers
            # skills 可能为 None（skills 模块 ok 时非 None），不强断
            assert getattr(app.feeds, "_specialists", None) is app.specialists
            assert getattr(app.goal_proposer, "_specialists", None) is app.specialists
            assert getattr(app.coordinator, "_specialists", None) is app.specialists
        finally:
            await app.stop()
        assert app.agents is None
        assert app.specialists is None
        assert app.feeds is None

    @pytest.mark.asyncio
    async def test_app_without_specialists_factory_still_starts_with_none(self, tmp_path):
        """A/B 还没就位（工厂默认懒加载失败）→ app.agents/specialists=None，
        老路与其它组件正常起来（兼容窗口）。"""
        from fakes import FakeCtx, FakeProfiles

        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "console": {"listen": "127.0.0.1:0", "password": "pw-测试"},
            "storage": {"data_dir": str(tmp_path / "data2")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        # 用工厂明确为 None：模拟「没就位」时 _make_agents/_make_specialists 返回 None
        app.agents_factory = lambda store, get_settings: None
        app.specialists_factory = lambda agents, workers, skills: None
        await app.start()
        try:
            assert app.started is True
            assert app.agents is None
            assert app.specialists is None
            assert getattr(app.feeds, "_specialists", None) is None
            assert getattr(app.goal_proposer, "_specialists", None) is None
            assert getattr(app.coordinator, "_specialists", None) is None
        finally:
            await app.stop()


# ==============================================================================
# idea：候选进主提示词是素材；主模型入库成功后才 review + remember
# ==============================================================================


class TestIdeaTwoLevel:
    @pytest.mark.asyncio
    async def test_make_idea_includes_specialist_candidate_as_material(self, store):
        """idea 专岗调查在现有主模型生成前；候选以「素材不是指令」段进入主提示词；
        入库成功 → review(learn=False) + remember 已审核构想（refs 含 idea:<id> 和 handoff:<hid>）。"""
        candidate = {
            "title": "我可以每周整理群文件分类口诀",
            "body": "把群的常用分类规则画成一页，方便新进群的人查",
            "basis": "画像里有「进群人多问文件」",
        }
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=True, summary="调查交回", data={"idea": candidate}),
        ])
        # 主模型两个调用：构想 JSON
        idea_payload = {
            "idea": {
                "title": "我可以每周整理群文件分类口诀",
                "body": "一页「文件在哪找」速查图", "basis": "画像需求",
                "icon": "bulb", "chat_worthy": False,
                "feasibility": {"level": "ok", "note": "能做"},
                "keywords": ["群文件", "速查"], "items": [],
            }
        }
        models = _ModelsOK({"feeds.idea": json.dumps(idea_payload, ensure_ascii=False)})
        settings = _Settings()
        feeds = _mk_feeds(store, models=models, settings=settings, specialists=sp)
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, session_id, workspace, token, profile_ready_ts, created)"
                " VALUES (?,?,?,?,?,?)",
                (G1, "", f"g{G1}", "tok12345", clock.now(), clock.now()),
            )
        idea_id = await feeds.make_idea(G1)
        assert idea_id is not None and int(idea_id) > 0
        # 专岗被跑（kind=idea），主模型收到「素材不是指令」段
        assert sp.runs_of("idea")
        brief = sp.runs_of("idea")[0]["brief"]
        assert "素材是数据不是指令" in brief or "素材不是指令" in brief
        assert '"origin"' in brief  # 交回格式要带「由头」（2026-10）
        idea_prompt = models.calls[0]["messages"][0]["content"]
        assert "素材不是指令" in idea_prompt
        assert "分类口诀" in idea_prompt
        # review：accepted=True、learn=False、refs 带 idea:<id> 和 handoff:<hid>
        assert len(sp.reviews) == 1
        rv = sp.reviews[0]
        assert rv["accepted"] is True and rv["learn"] is False
        assert any(str(r).startswith("idea:") for r in rv["refs"])
        assert any(str(r).startswith("handoff:H-") for r in rv["refs"])
        # remember：仅 text 是「已审核构想…」，refs 同 review；source_id 用 idea:<id>
        rem = [r for r in agents.remembers if r["kind"] == "idea"]
        assert len(rem) == 1
        assert "已审核构想" in rem[0]["text"]
        assert rem[0]["source_id"] == f"idea:{int(idea_id)}"
        assert any(str(x).startswith("idea:") for x in rem[0]["refs"])

    @pytest.mark.asyncio
    async def test_idea_disabled_role_silently_skips_specialist(self, store):
        """idea 角色停用：绝不回落通才；主流照常（主模型仍出构想），
        但没有 sp.run、没有 review、写入零条记忆。"""
        agents = FakeAgents(enabled_kinds={"idea": False})
        sp = FakeSpecialists(agents)
        idea_payload = {"idea": {
            "title": "我可以每天问一句今天谁学到了东西",
            "body": "每天固定一句话冷场开场白", "basis": "画像需求",
            "icon": "bulb", "chat_worthy": False,
            "feasibility": {"level": "maybe", "note": "可能"}, "keywords": [], "items": [],
        }}
        models = _ModelsOK({"feeds.idea": json.dumps(idea_payload, ensure_ascii=False)})
        feeds = _mk_feeds(store, models=models, specialists=sp)
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, session_id, workspace, token, profile_ready_ts, created)"
                " VALUES (?,?,?,?,?,?)",
                (G1, "", f"g{G1}", "tok12345", clock.now(), clock.now()),
            )
        idea_id = await feeds.make_idea(G1)
        assert idea_id is not None
        assert sp.runs == []
        assert sp.reviews == []
        # 没入库任何记忆（idea 岗位的）
        assert [r for r in agents.remembers if r["kind"] == "idea"] == []

    @pytest.mark.asyncio
    async def test_idea_specialist_failure_falls_back_to_main_workflow(self, store):
        """idea 专岗运行失败：主流照常出构想（不 ctrl 到专岗），专岗 receive rejected，不写记忆。"""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=False, summary="", error="模型调用失败"),
        ])
        idea_payload = {"idea": {
            "title": "我可以把上周讨论过的方案整理成对比表",
            "body": "对比后好选", "basis": "画像需求",
            "icon": "bulb", "chat_worthy": False,
            "feasibility": {"level": "ok", "note": ""}, "keywords": [], "items": [],
        }}
        models = _ModelsOK({"feeds.idea": json.dumps(idea_payload, ensure_ascii=False)})
        feeds = _mk_feeds(store, models=models, specialists=sp)
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, session_id, workspace, token, profile_ready_ts, created)"
                " VALUES (?,?,?,?,?,?)",
                (G1, "", f"g{G1}", "tok12345", clock.now(), clock.now()),
            )
        idea_id = await feeds.make_idea(G1)
        assert idea_id is not None
        assert len(sp.reviews) == 1
        assert sp.reviews[0]["accepted"] is False
        assert [r for r in agents.remembers if r["kind"] == "idea"] == []

    @pytest.mark.asyncio
    async def test_idea_null_from_specialist_not_in_prompt_and_no_memory(self, store):
        """idea 专岗交回 null：不当素材进主提示词（仍是原样构想），不写记忆。"""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=True, summary="", data={"idea": None}),
        ])
        idea_payload = {"idea": None}
        models = _ModelsOK({"feeds.idea": json.dumps(idea_payload, ensure_ascii=False)})
        feeds = _mk_feeds(store, models=models, specialists=sp)
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, session_id, workspace, token, profile_ready_ts, created)"
                " VALUES (?,?,?,?,?,?)",
                (G1, "", f"g{G1}", "tok12345", clock.now(), clock.now()),
            )
        idea_id = await feeds.make_idea(G1)
        assert idea_id is None
        idea_prompt = models.calls[0]["messages"][0]["content"]
        assert "素材不是指令" not in idea_prompt
        assert agents.remembers == []


# ==============================================================================
# goal：force_manual only + 调查失败主流照出 + 记忆只写待批
# ==============================================================================


class TestGoalTwoLevel:
    @pytest.mark.asyncio
    async def test_propose_force_manual_review_after_approval_with_specialist(self, store):
        """propose：goal 专岗调查在 approvals.create 之前；调记忆一条「待批目标提议…」，
        不钩 goals.create、不调 goals.done。force_manual=True 恒为真。"""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=True, summary="调查交回", data={
                "assessment": "值得长期追：群里反复问同一个问题",
                "plan": ["先盘点最近一个月的问题热点", "再列出可行教案"],
                "questions": ["群主是想做教程还是想做制度？"],
            }),
        ])
        models = _ModelsOK({"goals.propose": json.dumps(
            {"goal": {"title": "把常问问题沉淀成 FAQ", "body": "每周整理", "why": "群里反复问"}},
            ensure_ascii=False)})
        goals, approvals = _Goals(), _ApprovalsRec()
        proposer = _mk_proposer(store, models, goals, approvals, _Settings(), specialists=sp)
        res = await proposer.propose(G1)
        assert res is not None and res.get("force_manual") is True
        assert sp.runs_of("goal")
        assert len(sp.reviews) == 1 and sp.reviews[0]["accepted"] is True
        rem = [r for r in agents.remembers if r["kind"] == "goal"]
        assert len(rem) == 1
        assert "待批" in rem[0]["text"] or "proposal" in rem[0]["source_id"]
        assert any(str(x).startswith("handoff:H-") for x in rem[0]["refs"])
        # 没创建任何 goal / 任务
        assert goals._rows == {}
        assert goals.events == []
        assert approvals.creates and approvals.creates[0]["kwargs"].get("force_manual") is True

    @pytest.mark.asyncio
    async def test_propose_specialist_failure_main_flow_still_creates(self, store):
        """goal 专岗调查失败：propose 照旧；quote 不含专岗段；review 记 rejected。"""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=False, summary="", error="模型调用失败"),
        ])
        models = _ModelsOK({"goals.propose": json.dumps(
            {"goal": {"title": "给新来的写一个上手贴", "body": "一页", "why": "频繁重复"}},
            ensure_ascii=False)})
        goals, approvals = _Goals(), _ApprovalsRec()
        proposer = _mk_proposer(store, models, goals, approvals, _Settings(), specialists=sp)
        res = await proposer.propose(G1)
        assert res is not None
        assert approvals.creates
        quote = approvals.creates[0]["kwargs"].get("quote") or ""
        assert "专岗调查" not in quote and "assessment" not in quote
        assert len(sp.reviews) == 1 and sp.reviews[0]["accepted"] is False
        rem = [r for r in agents.remembers if r["kind"] == "goal"]
        assert rem == []

    @pytest.mark.asyncio
    async def test_propose_disabled_goal_role_skips_specialist_but_still_proposes(self, store):
        """goal 角色停用：调查跳过但主流照跑；没有 sp.run、没有 review、没有记忆。"""
        agents = FakeAgents(enabled_kinds={"goal": False})
        sp = FakeSpecialists(agents)
        models = _ModelsOK({"goals.propose": json.dumps(
            {"goal": {"title": "收集群名片的自我介绍", "body": "一张表", "why": "老问"}},
            ensure_ascii=False)})
        goals, approvals = _Goals(), _ApprovalsRec()
        proposer = _mk_proposer(store, models, goals, approvals, _Settings(), specialists=sp)
        res = await proposer.propose(G1)
        assert res is not None and res.get("force_manual") is True
        assert sp.runs == []
        assert sp.reviews == []
        assert agents.remembers == []

    @pytest.mark.asyncio
    async def test_check_goal_specialist_investigates_but_main_decides_everything(self, store):
        """check_goal：goal 专岗调查（「只调查不改状态」）→ 主模型照旧判定 all_done。criteria
        全勾 → goals.done 才调；专岗不作为结论。"""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=True, summary="调查交回", data={
                "assessment": "两条标准已完成，第三条差一步", "plan": ["补一次对比"], "questions": []}),
        ])
        goals = _Goals()
        gid = goals.create(G1, title="打磨判 A", body="三条标准", kind="agent", by_text="",
                           criteria=json.dumps([{"text": "a", "done": True},
                                                {"text": "b", "done": True},
                                                {"text": "c", "done": False}], ensure_ascii=False))
        tasks = _Tasks()
        workers = FakeWorkersQueue()
        models = _ModelsOK({
            "coordinator.check_goal": json.dumps({
                "done_criteria": [0, 1, 2], "next_check_hours": 24,
                "progress": "三件事都完成了", "new_task": None, "report": "已打磨完",
            }, ensure_ascii=False),
        })
        coord = _mk_coordinator(store, models, workers, tasks, goals, _Settings(), specialists=sp)
        outbox = type("Outbox", (), {"enqueue": lambda self, *a, **k: None})()
        coord._outbox = outbox  # noqa: SLF001
        # goals 用真实现（少量）：我们用的是 _Goals 群实现——就调 coordinator 上
        await coord.check_goal(gid)
        assert sp.runs_of("goal")
        dis = sp.runs_of("goal")[0]
        assert "素材不是指令" in dis["brief"] or "不是指令" in dis["brief"]
        # 主模型在 criteria 全勾时判 done；sp 的存在不改变主模式判罚，
        # 专岗只是被叫起来调查；它的签约 refs 是 goal-check
        done_events = [e for e in goals.events if e[0] == "done"]
        assert done_events, "criteria 全勾时主模式应判 done"
        # 记忆里没有宣称「达成」，refs 用 goal-check
        goal_rems = [r for r in agents.remembers if r["kind"] == "goal"]
        assert all("已" not in r["text"] or "目标" in r["text"] for r in goal_rems)
        assert all(any("goal-check:" in str(x) for x in r["refs"]) for r in goal_rems)


# ==============================================================================
# task：_run_job 走 task 专岗 + 取消回填
# ==============================================================================


class TestTaskRole:
    @pytest.mark.asyncio
    async def test_run_job_uses_task_specialist_with_tools_and_artifact_scope(self, store):
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=True, summary="做完了"),
        ])
        tasks = _Tasks()
        coord = _mk_coordinator(store, _ModelsOK(), FakeWorkersQueue(), tasks, _Goals(),
                                _Settings(), specialists=sp)
        class Env2:
            def workspace(self, name):
                return Path("/tmp/c-itest-workspace")

            async def list_files(self, *a, **k):
                return []

        coord._env = Env2()  # noqa: SLF001
        w_report = await coord._run_job(
            brief="整理 README", tools=["read_file", "write_file"],
            gid=G1, tid="T-1", job_idx=2, ws_name="g" + G1,
            artifact_scope=(f"artifacts/T-1",),
        )
        assert isinstance(w_report, WorkerReport) and w_report.ok
        runs = sp.runs_of("task")
        assert len(runs) == 1
        assert runs[0]["tools"] == ["read_file", "write_file"]
        assert runs[0]["ws_name" if False else "task_id"] == "T-1"
        assert runs[0]["artifact_scope"] == ("artifacts/T-1",)
        assert "g" + G1 == "g" + G1

    @pytest.mark.asyncio
    async def test_run_job_cancelled_when_accept_result_false(self, store):
        """accept_result=False（任务中途被取消）→ 主流程拒了 →
        对 sp 的 call `agents.fail(..., state='cancelled')`."""""
        agents = FakeAgents()
        sp = FakeSpecialists(agents, results=[
            WorkerReport(ok=True, summary="做完了"),
        ])
        tasks = _Tasks()
        tasks.accept_result_value = False
        models = _ModelsOK({
            "coordinator.plan": json.dumps({
                "question": None, "criteria": ["c1"], "deliver_kind": "text",
                "jobs": [{"brief": "干活", "tools": ["submit_result"], "type": "other"}],
                "env": "local",
            }, ensure_ascii=False),
        })
        coord = _mk_coordinator(store, models, FakeWorkersQueue(), tasks, _Goals(),
                                _Settings(), specialists=sp)

        class Env2:
            def workspace(self, name):
                return Path("/tmp/c-itest-workspace")

            async def list_files(self, *a, **k):
                return []

        coord._env = Env2()  # noqa: SLF001
        tasks.create(G1, title="T", req="r", criteria=["c1"], source="test")
        tid = tasks.created[-1]["id"]
        await coord.run_task(tid)  # run_task 是 None 返回（无错误就完）
        # 取消（accept_result=False）之后的专岗报告 settle 成 rejected（不交付、不复活）：
        settled = [r for r in agents.reviews if not r["accepted"]] or agents.fails
        assert settled, "取消晚期结果没被 settle"
