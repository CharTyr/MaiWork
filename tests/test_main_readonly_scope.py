"""排计划 / 验收这两个主模型只读回合的**硬权限名单**（2026-10）。

已实测的缺口（假工具复现，不是真 QQ）：`_plan` 只把「list_skills / read_skill + roles
含 main 的 MCP」写进 specs 交给模型看，但给 `_run_tool_calls` 的 ToolContext 没设
`allowed_tools`（是 None）。`Tools.call` 那层只按角色查名——模型只要捏造一个
role=main、名字不在本轮 specs 里的工具（`remember` / `group_notice_send` / 群空间工具），
handler 就会真被调用、落库 ok=1。内置 find-skills 入库后这一回合默认开启（有 main
通用 skill → 给 tool 表），缺口常驻。`_review` 是同样形态的另一个只读回合，也缺这层闸。

这份测试钉住：

1. `_plan`：捏造本轮没给的 main 工具 → handler 不跑、落库 ok=0、计划照常产出；
2. 关掉 find-skills、只有本群 task 做法时同样拒（合法 list_skills 照常可用）；
3. 合法 main MCP 不被误拦（真调得到，避免收紧过头）；
4. `_review`：捏造主模型 side-effect 工具（remember / 群空间）拒；inspect_file /
   合法 main MCP 照常可用。

全是假模型（ScriptedModels）+ 只追加 list 的假 handler（零真实副作用）；
不碰真模型 / SSH / QQ / 真实网络。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import agents as agents_mod
from CharTyr_MaiWork.maiwork import skills as skills_mod
from CharTyr_MaiWork.maiwork import skills_tools as skills_tools_mod
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tools
from test_coordinator import (
    FakeDelivery,
    FakeWorkers,
    _create_task,
    _plan,
    _review,
)
from test_plan_tools import ScriptedModels, _main_skill, _plan_calls, _reg, _wire

GID = "900000001"

# 工具层硬权限闸给的中文错误（Tools.call 里那一句）；测试按这句话认「这轮被拒」。
SCOPE_ERROR = "不在本轮允许使用的名单里"


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


class _Served:
    """Agents 要的最小 Settings：只服务 GID。"""

    served_groups = (GID,)

    def is_served(self, gid: str) -> bool:
        return str(gid) in self.served_groups


def _call(cid: str, name: str, args: dict | None = None) -> dict:
    """模型回传的一条 tool_call（arguments 是 JSON 字符串，跟真实协议一致）。"""
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)},
    }


def _tool_rows(store: Store, tid: str) -> list[dict]:
    """这个任务落库的工具调用（按时间顺序）：名字 / ok / 错误 / 输出。"""
    return [
        {
            "tool": str(r["tool"]),
            "ok": int(r["ok"]),
            "error": str(r["error"] or ""),
            "output": str(r["output"] or ""),
        }
        for r in store.read()
        .execute(
            "SELECT tool, ok, error, output FROM tool_calls WHERE task_id=? ORDER BY id",
            (tid,),
        )
        .fetchall()
    ]


def _group_only_env(tmp_path: Path, store: Store):
    """只有本群 kind=task 做法、没有任何 main 通用 skill 的环境（内置那份也隔离掉）。"""
    agents = agents_mod.Agents(store, lambda: _Served())
    agents._ensure_schema()  # noqa: SLF001
    skills = skills_mod.Skills(str(tmp_path), builtin_root=None)
    tools = Tools(store)
    tools._agents = agents  # noqa: SLF001 —— 真实装配里 app 也是这么挂的
    skills_tools_mod.register_skill_tools(tools, skills)
    agents.skill_add(GID, "task", name="整理报名表", description="按本群惯例", body="步骤1：先看群规")
    return agents, tools


# ----------------------------------------------------------------------
# 1. _plan：捏造本轮没给的 main 工具 → 拒，且照常给出合法计划
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_rejects_fabricated_main_tools(tmp_path: Path, store: Store) -> None:
    tools = Tools(store)
    called: list = []
    _reg(tools, "mcp_gh_main", {"main"}, called)
    _reg(tools, "remember", {"main"}, called)           # 「记经验」有自己的专门回合
    _reg(tools, "group_notice_send", {"main"}, called)  # 群空间也有自己的专门回合
    skills_tools_mod.register_skill_tools(tools, _main_skill(tmp_path))

    fakes = [
        _call("f1", "remember", {"text": "记一笔"}),
        _call("f2", "group_notice_send", {"text": "写公告"}),
    ]
    models = ScriptedModels([{"text": "", "tool_calls": fakes}, _plan()])
    co, _env, tasks = _wire(tmp_path, store, tools, models, FakeWorkers(), FakeDelivery())
    tid = _create_task(tasks)

    plan = await co._plan(tasks.get(tid))

    # 捏造的工具 handler 一次都没跑；两次调用都落库成失败（留痕，不调 handler）
    assert called == []
    rows = _tool_rows(store, tid)
    assert [(r["tool"], r["ok"]) for r in rows] == [("remember", 0), ("group_notice_send", 0)]
    for r in rows:
        assert SCOPE_ERROR in r["error"]
    # 拒完照常拿到合法计划：任务不顺断
    assert plan["criteria"] == ["包含链接"]
    assert len(plan["jobs"]) == 1
    assert plan["jobs"][0]["brief"] == "做一页总结"


# ----------------------------------------------------------------------
# 2. 关掉 find-skills、只有本群 task 做法：同样拒；合法 list_skills 照常
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_rejects_fabricated_tools_with_only_group_task_skill(
    tmp_path: Path, store: Store
) -> None:
    _agents, tools = _group_only_env(tmp_path, store)
    called: list = []
    _reg(tools, "remember", {"main"}, called)
    _reg(tools, "group_notice_send", {"main"}, called)

    fakes = [
        _call("g1", "remember", {}),
        _call("g2", "group_notice_send", {}),
        _call("g3", "list_skills", {}),  # 本轮真给了 → 必须照常放行
    ]
    models = ScriptedModels([{"text": "", "tool_calls": fakes}, _plan()])
    co, _env, tasks = _wire(tmp_path, store, tools, models, FakeWorkers(), FakeDelivery())
    tid = _create_task(tasks)

    plan = await co._plan(tasks.get(tid))

    assert called == []
    rows = _tool_rows(store, tid)
    assert [(r["tool"], r["ok"]) for r in rows] == [
        ("remember", 0),
        ("group_notice_send", 0),
        ("list_skills", 1),
    ]
    assert SCOPE_ERROR in rows[0]["error"] and SCOPE_ERROR in rows[1]["error"]
    # 本群做法真列出来、回灌给模型了（不是把 list_skills 也一起拦掉）
    second = _plan_calls(models)[1]
    tool_msgs = [m for m in second[1] if m.get("role") == "tool"]
    assert any("本群/整理报名表" in str(m.get("content")) for m in tool_msgs)
    assert plan["criteria"] == ["包含链接"]


# ----------------------------------------------------------------------
# 3. 合法 main MCP 不被误拦
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_allows_legit_main_mcp(tmp_path: Path, store: Store) -> None:
    tools = Tools(store)
    called: list = []
    _reg(tools, "mcp_gh_main", {"main"}, called)
    skills_tools_mod.register_skill_tools(tools, _main_skill(tmp_path))

    models = ScriptedModels(
        [{"text": "", "tool_calls": [_call("ok1", "mcp_gh_main", {"q": "群公告"})]}, _plan()]
    )
    co, _env, tasks = _wire(tmp_path, store, tools, models, FakeWorkers(), FakeDelivery())
    tid = _create_task(tasks)

    plan = await co._plan(tasks.get(tid))

    assert [c[0] for c in called] == ["mcp_gh_main"]
    ctx = called[0][2]
    assert ctx.role == "main"
    # 名单就是本轮真给出的工具表：合法 MCP 在内，专门回合的 remember / 群空间不在
    assert ctx.allowed_tools is not None
    assert "mcp_gh_main" in ctx.allowed_tools
    assert "remember" not in ctx.allowed_tools
    assert "group_notice_send" not in ctx.allowed_tools
    rows = _tool_rows(store, tid)
    assert [(r["tool"], r["ok"]) for r in rows] == [("mcp_gh_main", 1)]
    assert plan["criteria"] == ["包含链接"]


# ----------------------------------------------------------------------
# 4. _review：捏造 side-effect 工具拒；inspect / 合法 main MCP 照常
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_rejects_fabricated_side_effect_tools(tmp_path: Path, store: Store) -> None:
    tools = Tools(store)
    called: list = []
    _reg(tools, "mcp_gh_main", {"main"}, called)
    _reg(tools, "remember", {"main"}, called)
    _reg(tools, "group_notice_send", {"main"}, called)

    fakes = [
        _call("r1", "remember", {"text": "x"}),
        _call("r2", "group_notice_send", {"text": "y"}),
    ]
    models = ScriptedModels(
        [{"text": "", "tool_calls": fakes}, _review(pass_=False, artifact="", review="没过")]
    )
    co, _env, tasks = _wire(tmp_path, store, tools, models, FakeWorkers(), FakeDelivery())
    tid = _create_task(tasks)

    review = await co._review(
        tasks.get(tid), {"criteria": ["真实产物"], "deliver_kind": "view"}, "完成", ["e"], []
    )

    assert called == []
    rows = _tool_rows(store, tid)
    assert [(r["tool"], r["ok"]) for r in rows] == [("remember", 0), ("group_notice_send", 0)]
    for r in rows:
        assert SCOPE_ERROR in r["error"]
    # 拒完照常给出验收结论（不把任务莫名终结）
    assert review["pass"] is False
    assert review["review"] == "没过"


@pytest.mark.asyncio
async def test_review_allows_inspect_and_legit_main_mcp(tmp_path: Path, store: Store) -> None:
    tools = Tools(store)
    called: list = []
    _reg(tools, "mcp_gh_main", {"main"}, called)

    models = ScriptedModels([])
    co, env, tasks = _wire(tmp_path, store, tools, models, FakeWorkers(), FakeDelivery())
    tid = _create_task(tasks)
    # 真写一个成品：inspect_file 要读得到才算没被过度拦
    artifact_rel = f"artifacts/{tid}/index.html"
    ws = env.workspace(tasks.get(tid)["workspace"])
    (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
    (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    models.script = [
        {
            "text": "",
            "tool_calls": [
                _call("i1", "inspect_file", {"path": artifact_rel}),
                _call("i2", "mcp_gh_main", {"q": "核对"}),
            ],
        },
        _review(pass_=True, artifact=artifact_rel),
    ]

    review = await co._review(
        tasks.get(tid), {"criteria": ["真实产物"], "deliver_kind": "view"}, "完成", ["e"], []
    )

    assert review["pass"] is True
    assert [c[0] for c in called] == ["mcp_gh_main"]
    rows = _tool_rows(store, tid)
    assert [(r["tool"], r["ok"]) for r in rows] == [("inspect_file", 1), ("mcp_gh_main", 1)]
    for r in rows:
        assert SCOPE_ERROR not in r["error"]
