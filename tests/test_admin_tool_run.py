"""tool_run 管理员工具 + Tools.call(as_role=)（10-30-admin-capabilities 子任务）。

管理员想亲手试一下某个子 agent 工具（「mcp_flovia_flovia_x 到底返回什么？」）时，
要有不真派活就能调一次的口子，而且必须走 Tools.call 唯一入口照常落 tool_calls：

- 只读工具直接调，回显真实输出（截 ~4000 字）；
- 执行类 / 只在派活回合用的工具先出待确认小票，管理员点头后才真调
  （小票摘要带工具名 + 参数摘要）；
- needs_workspace 工具走 env.ephemeral("admin-tool") 的单独临时工作区（用完就删）；
  环境不支持就中文报错，不许偷摸不带工作区跑；
- 不存在 / 只给 main / 管理员工具名 → 明确拒绝；
- 落库的 worker 调用记 actor=管理员对话、task_id=admin-tool:<对话号>。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import tools_admin
from CharTyr_MaiWork.maiwork.tools import Tool, ToolContext, ToolResult

from test_tools_admin import G1, _Svc, _bind_chat

BAD = "99999"


def _ctx(gid: str = G1, cid: int = 7) -> ToolContext:
    """管理员对话同款 ctx：role=admin，chat_id 动态挂上（admin_chat._run_call 就这么干）。"""
    c = ToolContext(group_id=gid, actor="主模型（管理员对话）", role="admin")
    c.chat_id = cid
    return c


@pytest.fixture
def svc(tmp_path: Path) -> _Svc:
    from CharTyr_MaiWork.maiwork.tools_admin import _APPROVED_CTX, _CHAT_CTX, register_admin_tools

    _CHAT_CTX.set((0, 0))
    _APPROVED_CTX.set(None)
    s = _Svc(tmp_path, workspace_root=tmp_path / "ws")

    async def echo(ctx, args):
        return ToolResult(ok=True, output=json.dumps(
            {"args": args, "ws": str(getattr(ctx, "workspace", None) or "")},
            ensure_ascii=False, default=str))

    def reg(name: str, roles=frozenset({"worker"}), **kw) -> None:
        s.tools.register(Tool(
            name=name, description=name,
            parameters={"type": "object", "properties": {}},
            roles=roles, handler=echo, **kw))

    reg("echo_tool")                              # 只读
    reg("exec_tool", exec_class=True)             # 执行类 → 小票
    reg("ws_tool", needs_workspace=True)          # 要临时工作区
    reg("task_tool", task_only=True)              # 只在派活回合用 → 小票
    reg("main_tool", roles=frozenset({"main"}))   # 只给主模型

    async def big(ctx, args):
        return ToolResult(ok=True, output="y" * 5000)

    s.tools.register(Tool(name="big_out", description="b",
                          parameters={"type": "object", "properties": {}},
                          roles=frozenset({"worker"}), handler=big))
    s.admin_pending = register_admin_tools(s.tools, s)
    return s


class TestAsRole:
    """Tools.call 的按角色调用口子：内部代码（tool_run）能让 admin ctx 走 worker 工具表。"""

    @pytest.mark.asyncio
    async def test_admin_ctx_calls_worker_tool_via_as_role(self, svc: _Svc) -> None:
        r = await svc.tools.call("echo_tool", {"a": 1}, _ctx(), as_role="worker")
        assert r.ok, r.error
        row = svc.store.read().execute(
            "SELECT actor, tool FROM tool_calls WHERE tool='echo_tool'"
        ).fetchone()
        assert row is not None and "管理员" in str(row["actor"])

    @pytest.mark.asyncio
    async def test_admin_ctx_without_as_role_still_rejected(self, svc: _Svc) -> None:
        r = await svc.tools.call("echo_tool", {}, _ctx())
        assert not r.ok and "不允许" in r.error


class TestToolRunBasics:
    @pytest.mark.asyncio
    async def test_readonly_tool_runs_directly_and_echoes(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {"name": "echo_tool", "args": {"a": 1}}, _ctx())
        assert r.ok, r.error
        assert "echo_tool" in r.output
        assert '"a": 1' in r.output

    @pytest.mark.asyncio
    async def test_worker_call_logged_as_admin_chat(self, svc: _Svc) -> None:
        await svc.tools.call("tool_run", {"name": "echo_tool"}, _ctx(cid=9))
        row = svc.store.read().execute(
            "SELECT task_id, actor, tool, ok FROM tool_calls WHERE tool='echo_tool'"
        ).fetchone()
        assert row is not None
        assert str(row["task_id"]) == "admin-tool:9"
        assert "管理员" in str(row["actor"])

    @pytest.mark.asyncio
    async def test_worker_error_is_honest(self, svc: _Svc) -> None:
        async def boom(ctx, args):
            raise RuntimeError("服务那边 500 了")

        svc.tools.register(Tool(name="boom_tool", description="b",
                                parameters={"type": "object", "properties": {}},
                                roles=frozenset({"worker"}), handler=boom))
        r = await svc.tools.call("tool_run", {"name": "boom_tool"}, _ctx())
        assert not r.ok and "500" in r.error
        # 真实失败也照样落库
        row = svc.store.read().execute(
            "SELECT ok, error FROM tool_calls WHERE tool='boom_tool'"
        ).fetchone()
        assert row is not None and int(row["ok"]) == 0

    @pytest.mark.asyncio
    async def test_output_truncated_around_4000(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {"name": "big_out"}, _ctx())
        assert r.ok, r.error
        assert len(r.output) < 4300 and r.output.endswith("…")

    @pytest.mark.asyncio
    async def test_args_must_be_object(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {"name": "echo_tool", "args": [1, 2]}, _ctx())
        assert not r.ok and ("JSON" in r.error or "对象" in r.error)

    @pytest.mark.asyncio
    async def test_non_served_group_rejected(self, svc: _Svc) -> None:
        r = await svc.tools.call(
            "tool_run", {"name": "echo_tool", "group_id": BAD}, _ctx())
        assert not r.ok and "服务群" in r.error


class TestToolRunRejects:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {"name": "nope_xyz"}, _ctx())
        assert not r.ok and "没有" in r.error

    @pytest.mark.asyncio
    async def test_main_only_tool(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {"name": "main_tool"}, _ctx())
        assert not r.ok and "子 agent" in r.error

    @pytest.mark.asyncio
    async def test_admin_tool_itself_rejected(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {"name": "list_groups"}, _ctx())
        assert not r.ok and "管理员工具" in r.error

    @pytest.mark.asyncio
    async def test_no_name(self, svc: _Svc) -> None:
        r = await svc.tools.call("tool_run", {}, _ctx())
        assert not r.ok


class TestToolRunConfirm:
    @pytest.mark.asyncio
    async def test_exec_tool_ticket_then_runs(self, svc: _Svc) -> None:
        cid = _bind_chat(svc, cid=42)
        r = await svc.tools.call(
            "tool_run", {"name": "exec_tool", "args": {"cmd": "ls"}}, _ctx(cid=cid))
        assert r.ok and tools_admin.NEED_CONFIRM in r.output
        pid = int(r.data["pending_id"])
        ticket = svc.admin_pending._row(pid)
        assert ticket["tool"] == "tool_run"
        # 小票摘要：工具名 + 参数摘要
        assert "exec_tool" in ticket["summary"] and "cmd" in ticket["summary"]
        # 同意前绝不能真调
        assert svc.store.read().execute(
            "SELECT COUNT(*) AS c FROM tool_calls WHERE tool='exec_tool'"
        ).fetchone()["c"] == 0
        done = await svc.admin_pending.execute(pid, True)
        assert done.ok, done.error
        row = svc.store.read().execute(
            "SELECT task_id, actor FROM tool_calls WHERE tool='exec_tool'"
        ).fetchone()
        assert row is not None and str(row["task_id"]).startswith("admin-tool:")
        assert "管理员" in str(row["actor"])

    @pytest.mark.asyncio
    async def test_task_only_tool_also_ticketed(self, svc: _Svc) -> None:
        cid = _bind_chat(svc, cid=43)
        r = await svc.tools.call("tool_run", {"name": "task_tool", "args": {}}, _ctx(cid=cid))
        assert r.ok and tools_admin.NEED_CONFIRM in r.output
        assert svc.store.read().execute(
            "SELECT COUNT(*) AS c FROM tool_calls WHERE tool='task_tool'"
        ).fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_readonly_tool_no_ticket(self, svc: _Svc) -> None:
        cid = _bind_chat(svc, cid=44)
        before = len(svc.admin_pending.pending())
        r = await svc.tools.call("tool_run", {"name": "echo_tool"}, _ctx(cid=cid))
        assert r.ok, r.error
        assert len(svc.admin_pending.pending()) == before


class TestToolRunWorkspace:
    @pytest.mark.asyncio
    async def test_needs_workspace_gets_ephemeral_then_deleted(self, svc: _Svc) -> None:
        from CharTyr_MaiWork.maiwork.environments.local import LocalEnv

        svc.env = LocalEnv(svc.get_settings)
        r = await svc.tools.call("tool_run", {"name": "ws_tool"}, _ctx())
        assert r.ok, r.error
        ws = json.loads(r.data["output"])["ws"]
        assert ws, "要工作区的工具该拿到一个临时工作区"
        assert Path(ws).name.startswith("admin-tool-")
        assert not Path(ws).exists(), "调用结束临时工作区要删掉"

    @pytest.mark.asyncio
    async def test_needs_workspace_without_env_fails_honest(self, svc: _Svc) -> None:
        svc.env = None  # 服务包没执行环境
        r = await svc.tools.call("tool_run", {"name": "ws_tool"}, _ctx())
        assert not r.ok and "工作区" in r.error
        # 不能落成一笔 worker 调用（只有 tool_run 自己那行）
        rows = svc.store.read().execute(
            "SELECT tool FROM tool_calls WHERE tool='ws_tool'").fetchall()
        assert not rows
