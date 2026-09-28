"""tools.py 单元测试：注册校验、角色限制、参数校验、超时、落库（密钥遮蔽）、specs、recent_calls。"""

from __future__ import annotations

import asyncio

import pytest

from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tools import Tool, ToolContext, ToolResult, Tools


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def tools(store):
    return Tools(store)


def _tool(**over) -> Tool:
    base = dict(
        name="demo",
        description="演示工具",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["query"],
        },
        roles=frozenset({"worker"}),
        handler=None,
        timeout_s=5.0,
    )
    base.update(over)
    return Tool(**base)


def _ctx(**over) -> ToolContext:
    base = dict(group_id="900000001", task_id="T-1", actor="子 agent #1")
    base.update(over)
    return ToolContext(**base)


class TestRegister:
    def test_register_ok(self, tools):
        tools.register(_tool(name="web_search"))
        specs = tools.specs("worker")
        assert [s["function"]["name"] for s in specs] == ["web_search"]

    @pytest.mark.parametrize("bad", ["", "a.b", "中文工具", "x y", "x/y", "a" * 65, "web.search"])
    def test_register_bad_name_raises(self, tools, bad):
        with pytest.raises(ValueError) as e:
            tools.register(_tool(name=bad))
        assert "名字" in str(e.value) or "不合法" in str(e.value)

    @pytest.mark.parametrize("ok", ["a", "A9_-x", "_" * 64, "read_profile2"])
    def test_register_ok_names(self, tools, ok):
        tools.register(_tool(name=ok))

    def test_duplicate_name_raises(self, tools):
        tools.register(_tool(name="dup"))
        with pytest.raises(ValueError) as e:
            tools.register(_tool(name="dup"))
        assert "重复" in str(e.value)


class TestAdminNamespace:
    """管理员对话的工具自成一格：和主模型/子 agent 的同名工具互不冲突、互不串用。"""

    @staticmethod
    def _handler(tag: str):
        async def h(ctx, args):
            return ToolResult(ok=True, output=tag)

        return h

    @pytest.mark.asyncio
    async def test_same_name_admin_and_main_coexist(self, tools):
        tools.register(_tool(name="read_profile", roles=frozenset({"main", "worker"}), handler=self._handler("main")))
        tools.register(_tool(name="read_profile", roles=frozenset({"admin"}), handler=self._handler("admin")))
        admin = await tools.call("read_profile", {"query": "x"}, _ctx(role="admin"))
        main = await tools.call("read_profile", {"query": "x"}, _ctx(role="main"))
        worker = await tools.call("read_profile", {"query": "x"}, _ctx())
        assert (admin.output, main.output, worker.output) == ("admin", "main", "main")
        assert [s["function"]["name"] for s in tools.specs("admin")] == ["read_profile"]
        assert tools.get("read_profile", "admin").roles == frozenset({"admin"})
        assert tools.get("read_profile", "main").roles == frozenset({"main", "worker"})

    @pytest.mark.asyncio
    async def test_admin_only_tool_invisible_to_others(self, tools):
        tools.register(_tool(name="send_group_message", roles=frozenset({"admin"}), handler=self._handler("a")))
        assert tools.specs("worker") == [] and tools.specs("main") == []
        r = await tools.call("send_group_message", {"query": "x"}, _ctx(role="main"))
        assert not r.ok
        assert tools.get("send_group_message", "main") is None

    def test_admin_mixed_with_other_roles_rejected(self, tools):
        with pytest.raises(ValueError):
            tools.register(_tool(name="x", roles=frozenset({"admin", "main"})))

    def test_duplicate_admin_name_raises(self, tools):
        tools.register(_tool(name="dup", roles=frozenset({"admin"})))
        with pytest.raises(ValueError):
            tools.register(_tool(name="dup", roles=frozenset({"admin"})))


class TestSpecs:
    def test_specs_openai_format(self, tools):
        async def h(ctx, args):
            return ToolResult(ok=True, output="好")

        tools.register(_tool(name="fetch_page", handler=h))
        specs = tools.specs("worker")
        assert len(specs) == 1
        spec = specs[0]
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] == "fetch_page"
        assert fn["description"] == "演示工具"
        assert fn["parameters"]["required"] == ["query"]
        assert "timeout_s" not in fn  # 内部字段不泄漏到规格

    def test_specs_filter_by_role(self, tools):
        tools.register(_tool(name="w1", roles=frozenset({"worker"}), handler=None))
        tools.register(_tool(name="m1", roles=frozenset({"main"}), handler=None))
        tools.register(_tool(name="both", roles=frozenset({"main", "worker"}), handler=None))
        assert {s["function"]["name"] for s in tools.specs("worker")} == {"w1", "both"}
        assert {s["function"]["name"] for s in tools.specs("main")} == {"m1", "both"}

    def test_specs_names_subset(self, tools):
        tools.register(_tool(name="a", handler=None))
        tools.register(_tool(name="b", handler=None))
        specs = tools.specs("worker", ["a"])
        assert [s["function"]["name"] for s in specs] == ["a"]


class TestCall:
    @pytest.mark.asyncio
    async def test_call_ok(self, tools):
        async def h(ctx, args):
            return ToolResult(ok=True, output=f"查到{args['n']}条")

        tools.register(_tool(name="web_search", handler=h))
        r = await tools.call("web_search", {"query": "MaiBot", "n": 3}, _ctx())
        assert r.ok is True
        assert r.output == "查到3条"

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, tools):
        r = await tools.call("ghost", {}, _ctx())
        assert r.ok is False
        assert "不认识" in r.error or "没有" in r.error

    @pytest.mark.asyncio
    async def test_call_role_denied(self, tools):
        tools.register(_tool(name="main_only", roles=frozenset({"main"}), handler=None))
        r = await tools.call("main_only", {"query": "x"}, _ctx(actor="子 agent #1", role="worker"))
        assert r.ok is False
        assert "不允许" in r.error or "没权限" in r.error or "角色" in r.error

    @pytest.mark.asyncio
    async def test_call_missing_required_arg(self, tools):
        tools.register(_tool(name="t", handler=None))
        r = await tools.call("t", {"n": 1}, _ctx())  # 缺 query
        assert r.ok is False
        assert "query" in r.error

    @pytest.mark.asyncio
    async def test_call_handler_exception(self, tools):
        async def h(ctx, args):
            raise RuntimeError("炸了")

        tools.register(_tool(name="boom", handler=h))
        r = await tools.call("boom", {"query": "x"}, _ctx())
        assert r.ok is False
        assert "炸了" in r.error

    @pytest.mark.asyncio
    async def test_call_timeout(self, tools):
        async def slow(ctx, args):
            await asyncio.sleep(5)
            return ToolResult(ok=True, output="慢")

        tools.register(_tool(name="slow", handler=slow, timeout_s=0.05))
        r = await tools.call("slow", {"query": "x"}, _ctx())
        assert r.ok is False
        assert "超时" in r.error


class TestPersistedLog:
    @pytest.mark.asyncio
    async def test_call_written_to_db_with_ms(self, tools, store):
        async def h(ctx, args):
            return ToolResult(ok=True, output="输出结果")

        tools.register(_tool(name="t", handler=h, summarize=None))
        r = await tools.call("t", {"query": "天气", "n": 2}, _ctx())
        assert r.ok
        rows = store.read().execute("SELECT * FROM tool_calls").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["tool"] == "t"
        assert row["actor"] == "子 agent #1"
        assert row["task_id"] == "T-1"
        assert row["group_id"] == "900000001"
        assert row["ok"] == 1
        assert row["ms"] >= 0
        assert "天气" in row["input"]
        assert "输出结果" in row["output"]

    @pytest.mark.asyncio
    async def test_secrets_masked_in_db(self, tools, store):
        got = {}

        async def h(ctx, args):
            got.update(args)
            return ToolResult(ok=True, output="ok")

        tools.register(_tool(name="s", handler=h))
        await tools.call(
            "s",
            {"query": "x", "api_key": "sk-secret-123", "token": "tok-abc", "password": "p@ssw0rd",
             "secret_value": "shh", "session": "keep-me"},
            _ctx(),
        )
        assert got["api_key"] == "sk-secret-123"  # handler 拿到的是原文
        row = dict(store.read().execute("SELECT input FROM tool_calls").fetchone())
        assert "sk-secret-123" not in row["input"]
        assert "tok-abc" not in row["input"]
        assert "p@ssw0rd" not in row["input"]
        assert "shh" not in row["input"]
        assert "***" in row["input"]
        assert "keep-me" in row["input"]  # 不含敏感词的键不遮

    @pytest.mark.asyncio
    async def test_summarize_used_when_provided(self, tools, store):
        async def h(ctx, args):
            return ToolResult(ok=True, output="原始输出" * 1000)

        tools.register(
            Tool(
                name="t",
                description="d",
                parameters={"type": "object", "properties": {}},
                roles=frozenset({"worker"}),
                handler=h,
                summarize=lambda args, res: (f"搜了 {args.get('query', '')}", "共 5 条"),
                timeout_s=5.0,
            )
        )
        await tools.call("t", {"query": "禽流感"}, _ctx())
        row = dict(store.read().execute("SELECT input, output FROM tool_calls").fetchone())
        assert row["input"] == "搜了 禽流感"
        assert row["output"] == "共 5 条"

    @pytest.mark.asyncio
    async def test_default_summary_truncates_to_500(self, tools, store):
        async def h(ctx, args):
            return ToolResult(ok=True, output="长" * 2000)

        tools.register(_tool(name="t", handler=h))
        await tools.call("t", {"query": "x" * 1000}, _ctx())
        row = dict(store.read().execute("SELECT input, output FROM tool_calls").fetchone())
        assert len(row["input"]) <= 500
        assert len(row["output"]) <= 500

    @pytest.mark.asyncio
    async def test_failed_call_also_persisted(self, tools, store):
        r = await tools.call("ghost", {}, _ctx())
        assert r.ok is False
        row = dict(store.read().execute("SELECT ok, error FROM tool_calls").fetchone())
        assert row["ok"] == 0
        assert row["error"]


class TestRecentCalls:
    @pytest.mark.asyncio
    async def test_recent_calls_filters_and_order(self, tools, store):
        async def h(ctx, args):
            return ToolResult(ok=True, output="好")

        tools.register(_tool(name="t", handler=h))
        await tools.call("t", {"query": "1"}, _ctx(task_id="T-9"))
        await tools.call("t", {"query": "2"}, _ctx(task_id="T-9"))
        await tools.call("t", {"query": "3"}, _ctx(task_id="T-other"))
        rows = tools.recent_calls(task_id="T-9")
        assert len(rows) == 2
        assert rows[0]["ts"] <= rows[1]["ts"]
        assert all(set(r) >= {"ts", "actor", "tool", "input", "output", "ms", "ok"} for r in rows)
        limited = tools.recent_calls(task_id="T-9", limit=1)
        assert len(limited) == 1

    @pytest.mark.asyncio
    async def test_recent_calls_by_group(self, tools, store):
        async def h(ctx, args):
            return ToolResult(ok=True, output="好")

        tools.register(_tool(name="t", handler=h))
        await tools.call("t", {"query": "a"}, _ctx(group_id="999", actor="主模型"))
        await tools.call("t", {"query": "b"}, _ctx(group_id="900000001"))
        rows = tools.recent_calls(group_id="900000001")
        assert len(rows) == 1
        assert rows[0]["actor"] == "子 agent #1"


class TestToolContextAndResult:
    def test_toolresult_defaults(self):
        r = ToolResult(ok=True, output="x")
        assert r.data is None and r.error == ""

    def test_tool_context_defaults(self):
        c = ToolContext(group_id="1")
        assert c.task_id == "" and c.actor == "主模型"
