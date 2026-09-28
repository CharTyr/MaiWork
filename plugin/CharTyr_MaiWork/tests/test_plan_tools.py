"""排计划回合的主模型工具（docs/02 §7、docs/07 §11.4）。

需求：主模型「排计划」时也能用给它（roles 含 main）的工具查资料 / 读 skill。

- 工具表 = roles 含 main 的 `mcp_*` + `list_skills` / `read_skill`；
  群空间工具、remember、inspect_file/inspect_files、exec 工具一律不给。
- 工具表为空 → 计划回合行为完全不变（一次 json_mode=True、无 tools 的调用）。
- 工具表非空 → 最多 _PLAN_TOOL_LIMIT 轮工具调用；没调工具就解析 JSON，
  解析不了追问一次「请只回 JSON」；用满轮数还没 JSON → 最后 tools=None /
  json_mode=True 强制拿一次。解析容忍 ```json 代码围栏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork import coordinator
from CharTyr_MaiWork.coordinator import Coordinator
from CharTyr_MaiWork.environments.local import LocalEnv
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.skills import Skills
from CharTyr_MaiWork.skills_tools import register_skill_tools
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks
from CharTyr_MaiWork.tools import Tool, ToolContext, ToolResult, Tools
from CharTyr_MaiWork.tools_exec import register_exec_tools
from test_coordinator import (
    FakeDelivery,
    FakeOutbox,
    FakeWorkers,
    ReplayChatResult,
    _Profiles,
    _Settings,
    _create_task,
    _plan,
    _review,
)

GID = "900000001"


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _reg(tools: Tools, name: str, roles: set[str], calls: list | None = None) -> None:
    async def _handler(ctx: ToolContext, args: dict) -> ToolResult:
        if calls is not None:
            calls.append((name, dict(args), ctx))
        return ToolResult(ok=True, output=f"{name} 的结果")

    tools.register(
        Tool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            roles=frozenset(roles),
            handler=_handler,
        )
    )


def _main_skill(root: Path, name: str = "howto") -> Skills:
    """放一个 roles 含 main 的 skill（<root>/skills/<name>/SKILL.md）。"""
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 给主模型的\nroles: [main]\n---\n正文\n", encoding="utf-8")
    return Skills(root)


def _full_registry(tmp_path: Path, store: Store) -> Tools:
    """真 skill 工具 + 各种角色的 MCP 工具 + 不该进工具表的那些。"""
    tools = Tools(store)
    register_skill_tools(tools, _main_skill(tmp_path))
    _reg(tools, "mcp_main_only", {"main"})
    _reg(tools, "mcp_worker_only", {"worker"})
    _reg(tools, "mcp_both", {"main", "worker"})
    _reg(tools, "group_files_list", {"main"})  # 群空间：有自己的回合
    _reg(tools, "remember", {"main"})  # 「记经验」：有自己的回合
    _reg(tools, "inspect_file", {"main"})  # 验收专用
    _reg(tools, "run_command", {"worker"})
    return tools


# ----------------------------------------------------------------------
# 1. main_plan_tool_specs：谁在、谁不在、注册表坏了怎么办
# ----------------------------------------------------------------------


def test_main_plan_tool_table_has_main_mcp_and_skills(tmp_path: Path, store: Store) -> None:
    tools = _full_registry(tmp_path, store)
    names = [s["function"]["name"] for s in coordinator.main_plan_tool_specs(tools)]
    assert names == ["list_skills", "read_skill", "mcp_main_only", "mcp_both"]
    assert "mcp_worker_only" not in names
    assert "run_command" not in names  # exec 工具不进排计划回合
    assert "group_files_list" not in names  # 群空间工具不进
    assert "remember" not in names
    assert "inspect_file" not in names


def test_main_plan_tool_table_registry_error_is_empty() -> None:
    class _Boom:
        def specs(self, *args, **kwargs):
            raise RuntimeError("注册表坏了")

    assert coordinator.main_plan_tool_specs(_Boom()) == []


def test_main_plan_tool_table_without_main_mcp_is_only_skills(
    tmp_path: Path, store: Store
) -> None:
    tools = Tools(store)
    register_skill_tools(tools, _main_skill(tmp_path))
    _reg(tools, "mcp_worker_only", {"worker"})
    names = [s["function"]["name"] for s in coordinator.main_plan_tool_specs(tools)]
    assert names == ["list_skills", "read_skill"]


def test_no_main_skill_no_main_mcp_is_empty(tmp_path: Path, store: Store) -> None:
    """线上常态：skill 工具总注册着，但没有给主模型的 skill、也没有 main MCP → 空表
    （排计划照旧一次纯 JSON 调用，不白白多轮）。"""
    tools = Tools(store)
    register_skill_tools(tools, Skills(tmp_path))  # 空 skills 目录
    _reg(tools, "mcp_worker_only", {"worker"})
    assert coordinator.main_plan_tool_specs(tools) == []


def test_worker_only_skill_does_not_open_skill_tools(tmp_path: Path, store: Store) -> None:
    d = tmp_path / "skills" / "wk"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: wk\nroles: [worker]\n---\nx\n", encoding="utf-8")
    tools = Tools(store)
    register_skill_tools(tools, Skills(tmp_path))
    _reg(tools, "mcp_main_only", {"main"})
    names = [s["function"]["name"] for s in coordinator.main_plan_tool_specs(tools)]
    assert names == ["mcp_main_only"]


# ----------------------------------------------------------------------
# 2. 端到端：计划回合带工具、真调一次、结果回灌、第二轮给 JSON
# ----------------------------------------------------------------------


class ScriptedModels:
    """按脚本回放 chat：str = 纯文本；dict = {text, tool_calls}；Exception = 抛。"""

    def __init__(self, script: list, ready: bool = True) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, list[dict], dict]] = []
        self._ready = ready

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self):
                return ready

        return _S()

    async def chat(self, role, messages, **kwargs):
        self.calls.append((role, [dict(m) for m in messages], kwargs))
        item = self.script.pop(0) if self.script else "{}"
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return ReplayChatResult(item.get("text", ""), item.get("tool_calls"))
        return ReplayChatResult(str(item))


def _wire(tmp_path: Path, store: Store, tools: Tools, models, workers, delivery):
    settings = _Settings(tmp_path / "ws")
    env = LocalEnv(lambda: settings)
    register_exec_tools(
        tools, env=env, host=None, get_settings=lambda: settings, session_of=lambda gid: "s"
    )
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    co = Coordinator(
        store,
        models,
        workers,
        tools,
        tasks,
        goals,
        delivery,
        FakeOutbox(),
        env,
        _Profiles(),
        lambda: settings,
    )
    return co, env, tasks


def _artifact_writer(env, tasks, tid: str):
    async def _write() -> None:
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    return _write


def _plan_calls(models) -> list:
    return [c for c in models.calls if c[2].get("purpose") == "coordinator.plan"]


@pytest.mark.asyncio
async def test_plan_round_calls_main_mcp_tool_then_json(tmp_path: Path, store: Store) -> None:
    tools = Tools(store)
    called: list = []
    _reg(tools, "mcp_gh_main", {"main"}, called)
    _reg(tools, "mcp_gh_worker", {"worker"}, called)
    register_skill_tools(tools, _main_skill(tmp_path))

    tool_call = {
        "id": "call-plan-1",
        "type": "function",
        "function": {"name": "mcp_gh_main", "arguments": json.dumps({"q": "群公告"}, ensure_ascii=False)},
    }
    models = ScriptedModels([{"text": "", "tool_calls": [tool_call]}, _plan(), _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    plans = _plan_calls(models)
    assert len(plans) == 2  # 第一轮调工具，第二轮给 JSON，没有多追
    first, second = plans

    # 第一轮：带工具表、json_mode=False；提示词里有那句「先用工具查」的说明
    assert first[2]["json_mode"] is False
    names = [t["function"]["name"] for t in (first[2].get("tools") or [])]
    assert "mcp_gh_main" in names
    assert "list_skills" in names and "read_skill" in names
    assert "mcp_gh_worker" not in names
    first_prompt = str(first[1][0]["content"])
    assert "先用这些工具" in first_prompt and "查完只回上面的 JSON" in first_prompt

    # 第二轮：工具结果已回灌，工具表还在，仍是 json_mode=False
    assert second[2]["json_mode"] is False
    assert [t["function"]["name"] for t in (second[2].get("tools") or [])] == names
    tool_msgs = [m for m in second[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "mcp_gh_main 的结果" in tool_msgs[0]["content"]
    assert tool_msgs[0].get("tool_call_id") == "call-plan-1"

    # MCP 工具真的被调用了（handler 一次 + 落库 actor=主模型）
    assert [c[0] for c in called] == ["mcp_gh_main"]
    ctx = called[0][2]
    assert ctx.role == "main" and ctx.actor == "主模型"
    assert ctx.group_id == GID and ctx.task_id == tid
    assert ctx.workspace is not None  # 照验收回合那样尽量取到工作区
    rows = store.read().execute(
        "SELECT actor, tool, ok FROM tool_calls WHERE task_id=? ORDER BY id", (tid,)
    ).fetchall()
    mcp_rows = [r for r in rows if r["tool"] == "mcp_gh_main"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0]["actor"] == "主模型" and mcp_rows[0]["ok"] == 1

    # 任务照常往下走：派活 → 验收 → 交付
    assert tasks.get(tid)["status"] == "completed"
    assert len(workers.calls) == 1
    assert [d["task_id"] for d in delivery.delivered] == [tid]
    purposes = sorted({c[2].get("purpose") for c in models.calls})
    assert purposes == ["coordinator.plan", "coordinator.review"]


@pytest.mark.asyncio
async def test_plan_round_without_main_tools_unchanged(tmp_path: Path, store: Store) -> None:
    """没有任何 main 工具（连 skill 工具都没注册）→ 仍是一次 json_mode=True 调用。"""
    tools = Tools(store)
    _reg(tools, "mcp_gh_worker", {"worker"})
    models = ScriptedModels([_plan(), _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    plans = _plan_calls(models)
    assert len(plans) == 1
    assert plans[0][2]["json_mode"] is True
    assert plans[0][2].get("tools") is None
    assert "先用这些工具" not in str(plans[0][1][0]["content"])
    assert tasks.get(tid)["status"] == "completed"
    assert [d["task_id"] for d in delivery.delivered] == [tid]


@pytest.mark.asyncio
async def test_plan_round_exhausted_tools_forced_json(tmp_path: Path, store: Store) -> None:
    """用满 _PLAN_TOOL_LIMIT 轮都在调工具 → 第 7 次 tools=None / json_mode=True 拿 JSON。"""
    limit = coordinator._PLAN_TOOL_LIMIT
    assert limit == 6
    tools = Tools(store)
    called: list = []
    _reg(tools, "mcp_gh_main", {"main"}, called)

    tool_call = {
        "id": "call-plan-x",
        "type": "function",
        "function": {"name": "mcp_gh_main", "arguments": "{}"},
    }
    script = [{"text": "", "tool_calls": [tool_call]} for _ in range(limit)]
    script += [_plan(), _review(pass_=True)]
    models = ScriptedModels(script)
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    plans = _plan_calls(models)
    assert len(plans) == limit + 1
    for c in plans[:limit]:
        assert c[2]["json_mode"] is False
        assert c[2].get("tools")
    forced = plans[-1]
    assert forced[2]["json_mode"] is True
    assert forced[2].get("tools") is None
    # 前 6 轮的工具结果都在最后那次的 messages 里
    assert len([m for m in forced[1] if m.get("role") == "tool"]) == limit
    assert len(called) == limit
    assert tasks.get(tid)["status"] == "completed"
    assert [d["task_id"] for d in delivery.delivered] == [tid]


@pytest.mark.asyncio
async def test_plan_round_tolerates_json_fence(tmp_path: Path, store: Store) -> None:
    """带工具时模型爱加 ```json 围栏：容忍。"""
    tools = Tools(store)
    _reg(tools, "mcp_gh_main", {"main"})
    fenced = "```json\n" + _plan() + "\n```"
    models = ScriptedModels([fenced, _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    assert len(_plan_calls(models)) == 1
    assert tasks.get(tid)["status"] == "completed"
    assert [d["task_id"] for d in delivery.delivered] == [tid]


@pytest.mark.asyncio
async def test_plan_round_non_json_asks_again(tmp_path: Path, store: Store) -> None:
    """没调工具又不是 JSON → 追问一次「请只回 JSON」，再给 JSON 就照常走。"""
    tools = Tools(store)
    _reg(tools, "mcp_gh_main", {"main"})
    models = ScriptedModels(["好的，我先想想", _plan(), _review(pass_=True)])
    workers = FakeWorkers()
    delivery = FakeDelivery()
    co, env, tasks = _wire(tmp_path, store, tools, models, workers, delivery)
    tid = _create_task(tasks)
    workers.before_return = _artifact_writer(env, tasks, tid)

    await co.run_task(tid)

    plans = _plan_calls(models)
    assert len(plans) == 2
    second_msgs = plans[1][1]
    assert any(
        m.get("role") == "user" and "请只回 JSON" in str(m.get("content"))
        for m in second_msgs
    )
    assert tasks.get(tid)["status"] == "completed"
    assert [d["task_id"] for d in delivery.delivered] == [tid]
