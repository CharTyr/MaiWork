"""开工前能力自检的复核修复（2026-10，docs/18 §六「开工前对验收要的交付物和工具」复核）。

问题（实测）：`_exec_capability_self_check` 以前只看计划里的原始 `job["tools"]` 和
注册表里有没有 `run_command`，然后往 `job["tools"]` 追加 `run_command`，并记一条
「已自动补上」。可真正跑活时 `Specialists._resolve_tools` 会按岗位上限过滤：
news/idea/goal 的上限是只读调研工具，`run_command` 当场被删掉。于是自检说「补上了」，
子 agent 实际一个执行工具都没有，验收必然连打回（线上 T-7 白烧 token 的成因）。
反向也漏：计划里本来就写了 `run_command`（或被岗位过滤掉 / 根本没注册）时，老写法
直接当成「有执行能力」跳过，实际同样跑不了。

修法：自检改为按「Workers 实际会拿到的工具」判定——环境换名（local / railway 的 vm_*
/ 专用机器的 machine_*）→ 岗位角色门控（复用 `Specialists.effective_tools`，和
`specialists.run` 同一份解析，不再两套分叉）→ 注册表真伪。能补的只有「岗位允许 +
环境具备 + 注册表真有」的交集；补不了就记一条说得清的提醒，绝不假装可执行，
也不越权扩大岗位上限、不改门槛。

覆盖（用户点的六项 + 取消/验收不判死）：
1. news 岗位的工具被上限过滤：补不进去、不记 autofill、明确记 unavailable；
2. task 岗位保留可补（本机 run_command 能补上）；
3. disabled 专岗 / 没挂 specialists 的直连回落路；
4. 工具没注册（含「计划里写了但注册表没有」的假能力）；
5. ssh / railway 组合的实际可用性（vm_* / machine_* 也要过岗位门控与注册表）；
6. 真实 `_run_job`（真 Specialists + 真 Agents + 真 Tools）抓 Workers 实际拿到的 tools；
7. 取消不复活、验收没结论不判死：新自检不改变这两条既有行为。

用真 Store / 真 Agents / 真 Tools / 真 LocalEnv，模型用队列桩、Workers 用抓取桩，
不调真模型、不碰网络、不碰宿主。
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.specialists import Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport

GID = "900000001"
NOW = 1_790_000_000.0
NEED_EXEC_BRIEF = "把 3 张封面图下载下来保存到本地 artifacts/T-1/，再做一页 index.html"
PLAIN_BRIEF = "写一页纯文字总结，写到 artifacts/T-1/index.html"


# ----------------------------------------------------------------------
# 桩：模型队列 / 抓工具名单的 Workers / 交付 / 发件箱 / profiles
# ----------------------------------------------------------------------


class ReplayChatResult:
    def __init__(self, text, tool_calls=None):
        self.text = text
        self.tool_calls = list(tool_calls or [])


class ModelsQueue:
    """假 Models：按顺序回放 chat 文本；空队列回 "{}"。"""

    def __init__(self, replies=None, ready=True):
        self.reply_queue = list(replies or [])
        self.calls: list[tuple] = []
        self._ready = ready

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self):
                return ready

        return _S()

    async def chat(self, role=None, messages=None, **kwargs):
        self.calls.append((role, [dict(m) for m in (messages or [])], kwargs))
        if not self.reply_queue:
            return ReplayChatResult("{}")
        item = self.reply_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ReplayChatResult(str(item))


class CaptureWorkers:
    """抓子 agent 实际拿到的 tools（Specialists 会经这里把有效名单交下去）。"""

    def __init__(self, reports=None):
        self.reports = list(reports or [])
        self.calls: list[dict] = []
        self.before_return = None

    async def run(
        self, brief, *, group_id, tools=None, task_id="", actor="", max_steps=0,
        output_schema=None, workspace=None, skills_hint="", system_extra="",
        deadline_ts=None, artifact_scope=None, agent_type="task",
        allowed_tools=None, allowed_skills=None, agent=None, used_tools=None,
    ):
        self.calls.append({
            "brief": brief,
            "tools": list(tools or []),
            "allowed_tools": list(allowed_tools or []),
            "agent_type": str(agent_type),
            "task_id": task_id,
        })
        if self.before_return is not None:
            out = self.before_return()
            if asyncio.iscoroutine(out):
                await out
        if not self.reports:
            return WorkerReport(ok=True, summary="做完了", evidence=["e1"])
        item = self.reports.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeDelivery:
    def __init__(self):
        self.delivered: list[dict] = []

    async def deliver_task(self, task_id, *, kind, path, name, note):
        self.delivered.append({"task_id": task_id, "kind": kind, "path": Path(path)})
        return 1

    def delivery_records(self, task_id):
        return []


class FakeOutbox:
    def __init__(self):
        self.enqueued: list[dict] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0):
        self.enqueued.append({"key": key, "group_id": group_id, "kind": kind,
                              "payload": dict(payload or {}), "task_id": task_id})
        return len(self.enqueued)


class FakeProfiles:
    def entries(self, group_id):
        return []


class FakeSkills:
    """skills 目录桩：本用例只关心 tools，skill 名单一律空。"""

    def list(self, role):
        return []


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch):
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def settings(tmp_path):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "g1"}]},
        "console": {"listen": "127.0.0.1:0", "password": "x"},
        "models": {"base_url": "https://ep.test/v1", "api_key": "k", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(tmp_path / "data")},
        "environments": {"workspace_root": str(tmp_path / "ws")},
    }
    loaded, problems = load_settings(raw)
    assert loaded is not None, problems
    return loaded


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def agents(store, settings):
    return Agents(store, lambda: settings)


@pytest.fixture
def tools(store, env, settings):
    t = Tools(store)
    register_exec_tools(
        t, env=env, host=None, get_settings=lambda: settings,
        session_of=lambda _gid: "sess-1",
    )
    return t


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _add_worker_tool(tools: Tools, name: str) -> None:
    async def _h(ctx, args):
        return ToolResult(ok=True, output="x")

    tools.register(Tool(
        name=name, description="t", parameters={"type": "object"},
        roles=frozenset({"worker"}), handler=_h,
    ))


def _make_specialists(agents, workers) -> Specialists:
    return Specialists(agents, workers, FakeSkills())


def _coordinator(
    store, settings, env, tools, agents, workers, *,
    specialists=None, models=None, capability=None, railway=None, ssh=None,
) -> Coordinator:
    coord = Coordinator(
        store, models, workers, tools, Tasks(store, lambda: settings),
        Goals(store, lambda: settings), FakeDelivery(), FakeOutbox(), env,
        FakeProfiles(), lambda: settings,
        capability=capability, railway=railway, ssh=ssh,
    )
    coord._specialists = specialists  # noqa: SLF001
    return coord


def _plan(*, jobs, deliver_kind="view", criteria=None) -> str:
    return json.dumps(
        {
            "criteria": list(criteria or ["每张图都落到本地，附出处链接"]),
            "deliver_kind": deliver_kind,
            "jobs": jobs,
            "question": None,
        },
        ensure_ascii=False,
    )


def _review(pass_=True, artifact="") -> str:
    return json.dumps(
        {"pass": pass_, "review": "看着不错", "missing": [], "artifact": artifact, "note": "好了"},
        ensure_ascii=False,
    )


def _events(store: Store, tid: str) -> list[tuple[str, dict]]:
    rows = store.read().execute(
        "SELECT kind, payload FROM events WHERE entity_id=? ORDER BY id", (tid,)
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append((str(r["kind"]), payload))
    return out


def _kinds(store: Store, tid: str) -> list[str]:
    return [k for k, _ in _events(store, tid)]


def _note_for(store: Store, tid: str, kind: str) -> str:
    for k, payload in _events(store, tid):
        if k == kind:
            return str(payload.get("note") or "")
    return ""


def _write_artifact(workers: CaptureWorkers, env, tasks, tid: str) -> None:
    async def _hook():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _hook


def _job(tools_list, brief=NEED_EXEC_BRIEF, agent="task") -> dict:
    return {"brief": brief, "tools": list(tools_list), "agent": agent, "type": "build"}


def _run_check(coord: Coordinator, plan: dict, *, on_remote=False, box=None) -> None:
    coord._exec_capability_self_check(  # noqa: SLF001
        "T-1", GID, plan, on_remote=on_remote, box=box
    )


# ----------------------------------------------------------------------
# 1. news 岗位：工具被上限过滤 → 不假装可执行
# ----------------------------------------------------------------------


class TestNewsRoleToolCeiling:
    def test_news_cannot_be_given_run_command_and_says_so(
        self, store, settings, env, tools, agents
    ):
        """news 的活要下载/存本地：老写法会往计划里塞 run_command 并记「已补」，
        实际上 Specialists 的岗位上限会把它删掉。现在必须：计划不被改、不记 autofill、
        记一条明确的 unavailable（点名岗位）。"""
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["web_search"], agent="news")]}

        _run_check(coord, plan)

        assert plan["jobs"][0]["tools"] == ["web_search"], "不许往计划里塞岗位拿不到的工具"
        kinds = _kinds(store, "T-1")
        assert "task.exec_autofill" not in kinds
        assert "task.exec_unavailable" in kinds
        note = _note_for(store, "T-1", "task.exec_unavailable")
        assert "news" in note and ("做不成" in note or "拿不到" in note)

    def test_news_effective_tools_really_exclude_exec_via_shared_resolver(
        self, store, settings, env, tools, agents
    ):
        """自检用的名单必须和真跑活用的名单是同一份：直接调 Specialists.effective_tools，
        run_command / vm_run / machine_run 都被岗位上限压掉。"""
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        job = _job(["web_search", "run_command", "vm_run", "machine_run"], agent="news")

        effective, kind, why = coord._job_effective_tools(  # noqa: SLF001
            job, on_remote=False, box=None
        )

        assert kind == "news" and why == ""
        assert "web_search" in effective
        for forbidden in ("run_command", "vm_run", "machine_run"):
            assert forbidden not in effective, forbidden
        # 和 specialists.run 内部同一份解析（唯一入口，不是两套分叉）
        assert set(effective) == set(specialists.effective_tools("news", job["tools"]))


# ----------------------------------------------------------------------
# 2. task 岗位：本机能跑命令 → 保留「可以补」
# ----------------------------------------------------------------------


class TestTaskRoleStillFillable:
    def test_task_job_gets_run_command_autofilled(self, store, settings, env, tools, agents):
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["write_file"], agent="task")]}

        _run_check(coord, plan)

        assert "run_command" in plan["jobs"][0]["tools"]
        kinds = _kinds(store, "T-1")
        assert "task.exec_autofill" in kinds
        assert "task.exec_unavailable" not in kinds
        assert "run_command" in _note_for(store, "T-1", "task.exec_autofill")

    def test_task_job_without_need_is_untouched(self, store, settings, env, tools, agents):
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["看着好"], "jobs": [_job(["write_file"], brief=PLAIN_BRIEF)]}

        _run_check(coord, plan)

        assert plan["jobs"][0]["tools"] == ["write_file"]
        assert _kinds(store, "T-1") == []


# ----------------------------------------------------------------------
# 3. disabled 专岗 / 没挂 specialists 的直连回落
# ----------------------------------------------------------------------


class TestDisabledRoleAndFallback:
    def test_disabled_specialist_is_not_given_tools(self, store, settings, env, tools, agents):
        """岗位停用：这条活根本不会跑，别往它身上补执行工具；要明确记下来。"""
        agents.update_profile("news", {"enabled": False})
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["web_search"], agent="news")]}

        _run_check(coord, plan)

        assert plan["jobs"][0]["tools"] == ["web_search"]
        kinds = _kinds(store, "T-1")
        assert "task.exec_autofill" not in kinds
        assert "task.exec_unavailable" in kinds
        note = _note_for(store, "T-1", "task.exec_unavailable")
        assert "不可用" in note and "news" in note
        # 真跑一遍：岗位停用 → specialists 直接拒，Workers 一次都没被叫
        report = asyncio.run(specialists.run(
            "news", NEED_EXEC_BRIEF, group_id=GID, task_id="T-1", tools=["web_search"],
        ))
        assert report.ok is False
        assert workers.calls == []

    def test_without_specialists_direct_path_still_fills(
        self, store, settings, env, tools, agents
    ):
        """没挂 specialists（启动早期 / 老调用 / 单测直连）：等于 _run_job 直连 Workers，
        本机能跑命令就给补上——回落路不能被顺手改坏。"""
        workers = CaptureWorkers()
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=None)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["write_file"], agent="task")]}

        _run_check(coord, plan)

        assert "run_command" in plan["jobs"][0]["tools"]
        assert "task.exec_autofill" in _kinds(store, "T-1")

    def test_without_specialists_unregistered_tool_is_not_pretended(
        self, store, settings, env, agents
    ):
        """直连路也一样：注册表里没有 run_command 就不补、不假装。"""
        bare = Tools(store)
        _add_worker_tool(bare, "write_file")
        workers = CaptureWorkers()
        coord = _coordinator(store, settings, env, bare, agents, workers, specialists=None)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["write_file"], agent="task")]}

        _run_check(coord, plan)

        assert plan["jobs"][0]["tools"] == ["write_file"]
        kinds = _kinds(store, "T-1")
        assert "task.exec_autofill" not in kinds
        assert "task.exec_unavailable" in kinds


# ----------------------------------------------------------------------
# 4. 工具没注册（含计划里写了假名字）
# ----------------------------------------------------------------------


class TestUnregisteredTool:
    def test_planned_but_unregistered_exec_tool_is_not_capability(
        self, store, settings, env, agents
    ):
        """计划里写着 run_command、注册表里其实没有（受限被摘 / 名字拼错）：
        老写法看到名字就跳过，现在必须按注册表判「没有」并明说。"""
        bare = Tools(store)
        _add_worker_tool(bare, "write_file")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, bare, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"],
                "jobs": [_job(["write_file", "run_command"], agent="task")]}

        _run_check(coord, plan)

        assert "task.exec_autofill" not in _kinds(store, "T-1")
        assert "task.exec_unavailable" in _kinds(store, "T-1")

    def test_registered_exec_tool_needs_no_action(self, store, settings, env, tools, agents):
        """真注册了 + 岗位放行 → 本来就能做，不补也不报警。"""
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"],
                "jobs": [_job(["write_file", "run_command"], agent="task")]}

        _run_check(coord, plan)

        assert plan["jobs"][0]["tools"] == ["write_file", "run_command"]
        assert _kinds(store, "T-1") == []


# ----------------------------------------------------------------------
# 5. ssh / railway 组合的实际可用性
# ----------------------------------------------------------------------


def _railway_box():
    return types.SimpleNamespace(kind="railway", expires_ts=NOW + 3600, name="r1")


def _ssh_box():
    return types.SimpleNamespace(kind="ssh", name="vps-1")


class TestRemoteEnvCombos:
    def test_railway_task_has_real_vm_run(self, store, settings, env, tools, agents):
        """railway + 通用执行岗：vm_* 换名后还在、也真注册了 → 有执行能力，不报警。"""
        _add_worker_tool(tools, "vm_run")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"],
                "jobs": [_job(["read_file", "write_file"], agent="task")]}

        _run_check(coord, plan, on_remote=True, box=_railway_box())

        effective, _kind, _why = coord._job_effective_tools(  # noqa: SLF001
            plan["jobs"][0], on_remote=True, box=_railway_box()
        )
        assert "vm_run" in effective
        assert _kinds(store, "T-1") == []

    def test_railway_news_role_still_cannot_exec(self, store, settings, env, tools, agents):
        """railway + news：vm_* 由环境换名补进来了，但岗位只读上限把它压掉——
        不能因为「在远端」就当成有执行能力（老写法 on_remote 直接 return，正是这个坑）。"""
        _add_worker_tool(tools, "vm_run")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["web_search"], agent="news")]}

        _run_check(coord, plan, on_remote=True, box=_railway_box())

        assert plan["jobs"][0]["tools"] == ["web_search"]
        kinds = _kinds(store, "T-1")
        assert "task.exec_autofill" not in kinds
        assert "task.exec_unavailable" in kinds
        assert "news" in _note_for(store, "T-1", "task.exec_unavailable")

    def test_ssh_task_has_real_machine_run(self, store, settings, env, tools, agents):
        _add_worker_tool(tools, "machine_run")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"],
                "jobs": [_job(["read_file", "write_file"], agent="task")]}

        _run_check(coord, plan, on_remote=True, box=_ssh_box())

        effective, _kind, _why = coord._job_effective_tools(  # noqa: SLF001
            plan["jobs"][0], on_remote=True, box=_ssh_box()
        )
        assert "machine_run" in effective
        assert _kinds(store, "T-1") == []

    def test_ssh_news_role_still_cannot_exec(self, store, settings, env, tools, agents):
        _add_worker_tool(tools, "machine_run")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"], "jobs": [_job(["web_search"], agent="news")]}

        _run_check(coord, plan, on_remote=True, box=_ssh_box())

        assert "task.exec_autofill" not in _kinds(store, "T-1")
        assert "task.exec_unavailable" in _kinds(store, "T-1")

    def test_railway_without_vm_tools_registered_is_not_capability(
        self, store, settings, env, tools, agents
    ):
        """railway 有机器但 vm_* 没注册：名字在名单里也调不通，不许当成能执行。"""
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(store, settings, env, tools, agents, workers, specialists=specialists)
        plan = {"criteria": ["每张图都落到本地"],
                "jobs": [_job(["read_file", "write_file"], agent="task")]}

        _run_check(coord, plan, on_remote=True, box=_railway_box())

        kinds = _kinds(store, "T-1")
        assert "task.exec_autofill" not in kinds
        assert "task.exec_unavailable" in kinds


# ----------------------------------------------------------------------
# 6. 真实 _run_job + 真 Specialists：抓 Workers 实际拿到的 tools
# ----------------------------------------------------------------------


class TestRealRunJobCapturesResolvedTools:
    def test_news_run_job_hands_readonly_tools_only(
        self, store, settings, env, tools, agents
    ):
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="找图", req="找图并存到本地", criteria=["附出处"], source="test")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(
            store, settings, env, tools, agents, workers, specialists=specialists,
            models=ModelsQueue(),
        )
        coord._tasks = tasks  # noqa: SLF001

        report = asyncio.run(coord._run_job(  # noqa: SLF001
            brief=NEED_EXEC_BRIEF,
            tools=["web_search", "run_command"],
            gid=GID,
            tid=tid,
            job_idx=1,
            ws_name="g1",
            agent="news",
        ))

        assert report.ok is True
        assert len(workers.calls) == 1
        handed = workers.calls[0]["tools"]
        assert "web_search" in handed
        assert "run_command" not in handed, "岗位上限必须真的把 run_command 过滤掉"
        assert workers.calls[0]["agent_type"] == "news"

    def test_task_run_job_hands_run_command_when_registered(
        self, store, settings, env, tools, agents
    ):
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="存图", req="存到本地", criteria=["附出处"], source="test")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(
            store, settings, env, tools, agents, workers, specialists=specialists,
            models=ModelsQueue(),
        )
        coord._tasks = tasks  # noqa: SLF001

        report = asyncio.run(coord._run_job(  # noqa: SLF001
            brief=NEED_EXEC_BRIEF,
            tools=["write_file", "run_command"],
            gid=GID,
            tid=tid,
            job_idx=1,
            ws_name="g1",
            agent="task",
        ))

        assert report.ok is True
        assert "run_command" in workers.calls[0]["tools"]
        assert workers.calls[0]["agent_type"] == "task"

    def test_full_run_task_news_job_without_exec_pauses_after_replan(
        self, store, settings, env, tools, agents
    ):
        """端到端：run_task 计划 → 能力闸 → 有界重排（仍拿不到）→ **暂停**，零子 agent。

        语义变更（2026-10 复核收口）：改这条之前，news 的活要下载但拿不到执行工具时只记
        一条 `task.exec_unavailable` 就照常派活、验收，白烧 token。现在的最终口径是
        「开工前对不上先补工具或改标准」：自检把说明反馈给主模型重排一次计划（只改 jobs），
        重排回来还是 news 拿不到执行工具 → 暂停（不是失败），一个子 agent 都不派。
        完整的有界重排/暂停契约见 test_exec_capability_gate.py。
        """
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="找图", req="找图并存到本地", criteria=["附出处"], source="test")
        workers = CaptureWorkers()
        _write_artifact(workers, env, tasks, tid)
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan(jobs=[_job(["web_search"], agent="news")]),
            _plan(jobs=[_job(["web_search"], agent="news")]),  # 重排一次：还是拿不到执行工具
            _review(pass_=True, artifact=f"artifacts/{tid}/index.html"),
        ])
        coord = _coordinator(
            store, settings, env, tools, agents, workers, specialists=specialists, models=models,
        )

        asyncio.run(coord.run_task(tid))

        assert tasks.get(tid)["status"] == "paused"
        assert workers.calls == [], "暂停在派活之前，零执行"
        assert "task.exec_unavailable" in _kinds(store, tid)
        assert "task.exec_replanned" in _kinds(store, tid)
        assert "task.exec_autofill" not in _kinds(store, tid)
        assert "task.failed" not in _kinds(store, tid)


# ----------------------------------------------------------------------
# 7. 取消不复活 / 验收没结论不判死：新自检不动这两条
# ----------------------------------------------------------------------


class TestCancellationAndInconclusivePreserved:
    def test_cancelled_task_is_not_started(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="找图", req="找图", criteria=["附出处"], source="test")
        tasks.transition(tid, "cancelled", reason="测试取消")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord = _coordinator(
            store, settings, env, tools, agents, workers, specialists=specialists,
            models=ModelsQueue(replies=[_plan(jobs=[_job(["web_search"], agent="news")])]),
        )

        asyncio.run(coord.run_task(tid))

        assert tasks.get(tid)["status"] == "cancelled"
        assert workers.calls == []

    def test_inconclusive_review_still_requeues_not_failed(
        self, store, settings, env, tools, agents
    ):
        """要执行工具的活 + 验收一直不给结论：走既有「退回重跑」，不判死；
        第二轮正常验收 → completed。

        语义变更（2026-10 复核收口）：这条原先用「news + 要下载的活」，但那种活现在会在
        开工前被能力闸拦下（重排一次仍拿不到 → 暂停，见上一条）。这里改成工具齐备的 task
        活（run_command 真注册过），要验的「验收没结论 → 退回重跑、不判死」这条保护原样保留。
        """
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(GID, title="找图", req="找图并存到本地", criteria=["附出处"], source="test")
        workers = CaptureWorkers()
        _write_artifact(workers, env, tasks, tid)
        specialists = _make_specialists(agents, workers)
        not_json = "我再看一下，稍等（不是 JSON）"
        exec_job = _job(["write_file", "run_command"], agent="task")
        models = ModelsQueue(replies=[
            _plan(jobs=[exec_job]),
            not_json, not_json, not_json, not_json, not_json, not_json,
            not_json, not_json,
            _plan(jobs=[exec_job], criteria=["第二轮验收标准-marker"]),
            _review(pass_=True, artifact=f"artifacts/{tid}/index.html"),
        ])
        coord = _coordinator(
            store, settings, env, tools, agents, workers, specialists=specialists, models=models,
        )

        asyncio.run(coord.run_task(tid))

        assert tasks.get(tid)["status"] == "completed"
        assert len(workers.calls) == 2  # 第一轮 + 退回重跑的第二轮
        handoffs = agents.handoffs(GID, 'task', limit=10)
        assert [h['criteria'] for h in handoffs] == [
            ['第二轮验收标准-marker'], ['每张图都落到本地，附出处链接'],
        ]
        assert json.loads(handoffs[0]['review'])['summary'] == '看着不错'
