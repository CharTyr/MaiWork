"""workers.py 单元测试：假 models 回放一串 ChatResult，验证多轮工具循环。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport, Workers


@dataclass
class FakeChatResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    model: str = "fake-worker"
    prompt_tokens: int = 10
    completion_tokens: int = 5
    raw_message: dict = field(default_factory=dict)


class ReplayModels:
    """假 models：按队列回放 ChatResult；每项可以是 ChatResult / Exception。

    记录每次 chat 的 (role, messages, kwargs) 供断言。"""

    def __init__(self, results):
        self.queue = list(results)
        self.calls = []

    async def chat(self, role=None, messages=None, **kwargs):
        # 深拷贝 messages 的快照（后面会被 workers 继续追加）
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


@pytest.fixture
def tools(store):
    t = Tools(store)

    async def web_search(ctx, args):
        return ToolResult(ok=True, output="1. 某结果 https://a.com/1", data=[{"url": "https://a.com/1"}])

    async def submit_result(ctx, args):
        return ToolResult(
            ok=True,
            output=args.get("summary", ""),
            data={"summary": args.get("summary", ""), "data": args.get("data"), "evidence": args.get("evidence", []),
                  **({"challenge": args["challenge"]} if isinstance(args.get("challenge"), dict) else {})},
        )

    from CharTyr_MaiWork.maiwork.tools import Tool

    for name, handler, roles in (
        ("web_search", web_search, {"worker"}),
        ("submit_result", submit_result, {"worker"}),
    ):
        t.register(
            Tool(
                name=name,
                description="d",
                parameters={"type": "object", "properties": {"summary": {"type": "string"}, "query": {"type": "string"}}, "required": []},
                roles=frozenset(roles),
                handler=handler,
                timeout_s=5.0,
            )
        )
    return t


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_search_then_submit(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "MaiBot"}, "c1")]),
                FakeChatResult(
                    tool_calls=[
                        _tool_call(
                            "submit_result",
                            {"summary": "找到 MaiBot 官网", "data": {"url": "https://a.com/1"}, "evidence": ["https://a.com/1"]},
                            "c2",
                        )
                    ]
                ),
            ]
        )
        w = Workers(models, tools)
        report = await w.run("查一下 MaiBot 是啥", group_id="900000001", tools=["web_search"], task_id="T-1")
        assert report.ok is True
        assert report.summary == "找到 MaiBot 官网"
        assert report.data == {"url": "https://a.com/1"}
        assert report.evidence == ["https://a.com/1"]
        assert report.steps == 2
        assert report.error == ""
        # 两次 chat 都是 worker 角色，第一次给了两个工具（web_search + submit_result）
        role, _msgs, kwargs = models.calls[0]
        assert role == "worker"
        spec_names = {s["function"]["name"] for s in kwargs["tools"]}
        assert spec_names == {"web_search", "submit_result"}
        assert kwargs["group_id"] == "900000001"
        assert kwargs["task_id"] == "T-1"
        # 第二次的 messages 里有 role=tool 的搜索结果
        _role, msgs2, _kw = models.calls[1]
        tool_msgs = [m for m in msgs2 if m.get("role") == "tool"]
        assert tool_msgs and "某结果" in tool_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_tool_message_follows_assistant_with_tool_calls(self, store, tools):
        """线上实测（2026-09-27）：端点报 400「Messages with role 'tool' must be a response to a
        preceding message with 'tool_calls'」——子 agent 回填工具结果前没带上模型那条 tool_calls 消息。
        OpenAI 规范：每条 tool 消息前面必须有一条 assistant(tool_calls 含同一 id)。"""
        calls = [_tool_call("web_search", {"query": "a"}, "c1"), _tool_call("web_search", {"query": "b"}, "c2")]
        models = ReplayModels([
            FakeChatResult(text="先搜两下", tool_calls=calls),
            FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"}, "c3")]),
        ])
        w = Workers(models, tools)
        await w.run("查", group_id="900000001", tools=["web_search"], task_id="T-1")
        _role, msgs2, _kw = models.calls[1]
        for i, m in enumerate(msgs2):
            if m.get("role") != "tool":
                continue
            # 往前找最近一条非 tool 消息：必须是带 tool_calls 的 assistant，且 id 对得上
            j = i - 1
            while j >= 0 and msgs2[j].get("role") == "tool":
                j -= 1
            prev = msgs2[j]
            assert prev.get("role") == "assistant" and prev.get("tool_calls"), msgs2
            assert m["tool_call_id"] in {tc["id"] for tc in prev["tool_calls"]}

    @pytest.mark.asyncio
    async def test_system_prompt_includes_submit_rule(self, store, tools):
        models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"})])])
        w = Workers(models, tools)
        await w.run("测试简报", group_id="1", tools=[], task_id="T-x", actor="子 agent #3")
        _role, msgs, _kw = models.calls[0]
        assert msgs[0]["role"] == "system"
        assert "submit_result" in msgs[0]["content"]
        assert "子" in msgs[0]["content"] or "子 agent" in msgs[0]["content"]
        assert msgs[1] == {"role": "user", "content": "测试简报"}

    @pytest.mark.asyncio
    async def test_output_schema_mentioned_in_prompt(self, store, tools):
        models = ReplayModels([FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"})])])
        w = Workers(models, tools)
        schema = {"type": "object", "properties": {"items": {"type": "array"}}}
        report = await w.run("干活", group_id="1", tools=[], output_schema=schema)
        assert report.ok
        content = models.calls[0][1][0]["content"]
        assert "data" in content and "items" in content


class TestNudge:
    @pytest.mark.asyncio
    async def test_text_without_tool_calls_gets_nudged(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(text="我觉得已经做完了"),  # 不调工具只说话 → 催促
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "做完了"})]),
            ]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=[])
        assert report.ok is True
        assert report.summary == "做完了"
        # 第二次 messages 里带催促
        msgs2 = models.calls[1][1]
        assert any("submit_result" in str(m.get("content", "")) for m in msgs2 if m["role"] == "user")

    @pytest.mark.asyncio
    async def test_nudge_at_most_twice(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(text="第一次废话"),
                FakeChatResult(text="第二次废话"),
                FakeChatResult(text="第三次废话"),  # 已催促 2 次，不再催
            ]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=[])
        assert report.ok is False
        # 0.4.0 起：只说话不调工具，催两次还不调就判失败（不再按步数算）
        assert "submit_result" in report.error or "submit_result" in report.summary
        # 催促最多 2 次：第 2、3 次的 messages 里带催促，之后不再催（直接失败返回）
        assert len(models.calls) == 3
        for i in (1, 2):
            msgs = models.calls[i][1]
            assert any(
                m["role"] == "user" and "submit_result" in str(m.get("content", "")) and m.get("content") != "干活"
                for m in msgs
            ), f"第 {i + 1} 次应带催促"


class TestBadToolCall:
    @pytest.mark.asyncio
    async def test_bad_json_arguments_replied_as_tool_error(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("web_search", "{bad json", "c9")]),
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "补救回来"})]),
            ]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["web_search"])
        assert report.ok is True
        assert report.summary == "补救回来"
        msgs2 = models.calls[1][1]
        tool_msgs = [m for m in msgs2 if m.get("role") == "tool"]
        assert tool_msgs
        assert "JSON" in tool_msgs[0]["content"] or "参数" in tool_msgs[0]["content"]
        # 坏参数也要落库（ok=0）
        row = store.read().execute("SELECT ok, error FROM tool_calls WHERE tool='web_search'").fetchone()
        assert row is not None and row["ok"] == 0

    @pytest.mark.asyncio
    async def test_unknown_tool_name_reply_and_continue(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("ghost_tool", {})]),
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "换路完成"})]),
            ]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=[])
        assert report.ok and report.summary == "换路完成"


class TestStepBudget:
    @pytest.mark.asyncio
    async def test_last_steps_force_wrap_up(self, store, tools):
        """0.4.0 起删掉「只剩 N 步 / 最后一步」提示：没有步数上限/提示，
        但给了 max_steps（>0）的兼容调用还是会到点硬收。"""
        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": f"q{i}"}, f"c{i}")]) for i in range(3)]
            + [FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "交回已找到的 3 条"}, "cz")])]
        )
        w = Workers(models, tools)
        report = await w.run("找资讯", group_id="1", tools=["web_search"], max_steps=2, task_id="T-9")
        # 给了 max_steps=2：第 3 次调用不会发生（只有 2 次 chat），失败
        assert report.ok is False
        assert report.steps == 2
        for _r, msgs, _kw in models.calls:
            for m in msgs:
                assert "只剩" not in str(m.get("content") or "")
                assert "最后一步" not in str(m.get("content") or "")


class TestFailures:
    @pytest.mark.asyncio
    async def test_max_steps_exhausted(self, store, tools):
        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": f"q{i}"}, f"c{i}")]) for i in range(10)]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["web_search"], max_steps=3, task_id="T-3")
        assert report.ok is False
        assert "步数" in report.summary
        assert report.steps == 3
        # 已有进展要带上（搜索结果被记进 summary/error 一带）
        assert report.summary or report.error

    @pytest.mark.asyncio
    async def test_model_error(self, store, tools):
        models = ReplayModels([ModelError("端点挂了", status=500)])
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=[])
        assert report.ok is False
        assert "端点挂了" in report.error

    @pytest.mark.asyncio
    async def test_tool_result_truncated_in_message(self, store, tools):
        from CharTyr_MaiWork.maiwork.tools import Tool

        async def big(ctx, args):
            return ToolResult(ok=True, output="长" * 20000)

        tools.register(
            Tool(
                name="big_out",
                description="d",
                parameters={"type": "object", "properties": {}},
                roles=frozenset({"worker"}),
                handler=big,
                timeout_s=5.0,
            )
        )
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("big_out", {}, "c1")]),
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "行"})]),
            ]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["big_out"])
        assert report.ok
        msgs2 = models.calls[1][1]
        tool_msg = [m for m in msgs2 if m.get("role") == "tool"][0]
        assert len(tool_msg["content"]) <= 6100  # 截 6000 字 + 截断标记


class TestToolCallsPersisted:
    @pytest.mark.asyncio
    async def test_worker_calls_written_with_actor_and_task(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c1")]),
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好"})]),
            ]
        )
        w = Workers(models, tools)
        await w.run("干活", group_id="900000001", tools=["web_search"], task_id="T-7", actor="子 agent #2")
        rows = store.read().execute("SELECT * FROM tool_calls WHERE task_id='T-7' ORDER BY id").fetchall()
        assert len(rows) == 2
        assert {r["tool"] for r in rows} == {"web_search", "submit_result"}
        assert all(r["actor"] == "子 agent #2" for r in rows)
        timeline = tools.recent_calls(task_id="T-7")
        assert len(timeline) == 2


# ----------------------------------------------------------------------
# 0.4.0：去步数上限 + 压缩 + 大结果落盘 + 重复提醒
# ----------------------------------------------------------------------


class _FakeModelsSettings:
    def __init__(self, context_window=128000):
        self.context_window = context_window


class TestNoStepLimit:
    @pytest.mark.asyncio
    async def test_unlimited_by_default(self, store, tools):
        """默认没有步数上限：跑 50 步还能再跑，最后 submit 才算完。"""
        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": f"q{i}"}, f"c{i}")]) for i in range(50)]
            + [FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "终于找齐"}, "cz")])]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-unlimited")
        assert report.ok is True
        assert report.summary == "终于找齐"
        assert report.steps == 51
        # 没有「只剩 N 步」「最后一步」这类提示
        for _role, msgs, _kw in models.calls:
            for m in msgs:
                assert "只剩" not in str(m.get("content") or "")
                assert "最后一步" not in str(m.get("content") or "")

    @pytest.mark.asyncio
    async def test_max_steps_zero_means_unlimited(self, store, tools):
        """max_steps=0 / None 兼容旧调用 = 不限。"""
        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": f"q{i}"}, f"c{i}")]) for i in range(20)]
            + [FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "完"})])]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["web_search"], max_steps=0)
        assert report.ok and report.steps == 21

    @pytest.mark.asyncio
    async def test_max_steps_given_still_limits(self, store, tools):
        """给了 max_steps（>0）还是按它收（兼容保留）。"""
        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": f"q{i}"}, f"c{i}")]) for i in range(10)]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["web_search"], max_steps=3)
        assert report.ok is False
        assert report.steps == 3

    @pytest.mark.asyncio
    async def test_unlimited_nudges_then_fail(self, store, tools):
        """不限步数也要求：只说话不调工具，催两次还没动静就失败。"""
        models = ReplayModels(
            [FakeChatResult(text="废话"), FakeChatResult(text="再废话"), FakeChatResult(text="还废话")]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=[])
        assert report.ok is False
        assert "submit_result" in report.error or "submit_result" in report.summary
        assert len(models.calls) == 3


class TestWorkersCompaction:
    @pytest.mark.asyncio
    async def test_big_tool_results_truncated_before_next_call(self, store, tools):
        """上下文多次超触发线：一直在压（每次都要么截要么摘要），上下文不会无界涨。"""
        big_text = "x" * 30000

        async def big_handler(ctx, args):
            return ToolResult(ok=True, output=big_text)

        from CharTyr_MaiWork.maiwork.tools import Tool

        tools.register(
            Tool(name="big_out", description="d", parameters={"type": "object", "properties": {}},
                 roles=frozenset({"worker"}), handler=big_handler, timeout_s=5.0)
        )
        models = ReplayModels([
            FakeChatResult(tool_calls=[_tool_call("big_out", {}, "c1")]),
            FakeChatResult(tool_calls=[_tool_call("big_out", {}, "c2")]),
            FakeChatResult(tool_calls=[_tool_call("big_out", {}, "c3")]),
            FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "好了"}, "c4")]),
        ])
        orig_chat = models.chat

        async def chat(role=None, messages=None, **kw):
            if str(kw.get("purpose") or "").endswith(":compact"):
                return FakeChatResult(text="Primary Request and Intent：干活；…8 节摘要…")
            return await orig_chat(role, messages, **kw)

        models.chat = chat  # type: ignore[method-assign]
        w = Workers(models, tools, get_settings=lambda: type("S", (), {"models": type("M", (), {"context_window": 8192})()})())
        report = await w.run("干活", group_id="1", tools=["big_out"], task_id="T-big")
        assert report.ok
        # 最后一次调用的上下文已经过压缩（摘要替换了最老一段）
        last_msgs = models.calls[-1][1]
        assert any(
            m.get("role") == "user" and "前面对话的摘要" in str(m.get("content") or "")
            for m in last_msgs
        ), "上下文超线后应当发生过压缩（摘要）"


class TestWorkersContextCompaction:
    @pytest.mark.asyncio
    async def test_summarize_when_history_too_long(self, store, tools):
        """累积上下文超触发线：调一次摘要模型（purpose 带 :compact）把最老一段换成摘要。"""
        big = "x" * 20000

        async def big_handler(ctx, args):
            return ToolResult(ok=True, output=big)

        from CharTyr_MaiWork.maiwork.tools import Tool

        tools.unregister("web_search")
        tools.register(
            Tool(name="web_search", description="d",
                 parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
                 roles=frozenset({"worker"}), handler=big_handler, timeout_s=5.0)
        )
        models = ReplayModels([
            FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "a"}, "c1")]),
            FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "a2"}, "c2")]),
            FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "a3"}, "c3")]),
            FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "a4"}, "c4")]),
            FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "完"}, "c5")]),
        ])
        # 假 models.chat 里 purpose 以 :compact 结尾时回 8 节摘要文本
        orig_chat = models.chat
        compact_calls: list[dict] = []

        async def chat(role=None, messages=None, **kw):
            if str(kw.get("purpose") or "").endswith(":compact"):
                compact_calls.append(kw)
                return FakeChatResult(text="Primary Request and Intent：找资讯；…8 节摘要…")
            return await orig_chat(role, messages, **kw)

        models.chat = chat  # type: ignore[method-assign]
        w = Workers(models, tools, get_settings=lambda: type("S", (), {"models": type("M", (), {"context_window": 8192})()})())
        report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-s")
        assert report.ok, report.error
        assert compact_calls, "超触发线后应该调一次摘要模型"
        # 摘要之后的那次正常调用的 messages：system 在前、其后是摘要 user 消息
        seen_summary = False
        for _r, msgs, _kw in models.calls:
            if any(m.get("role") == "user" and "前面对话的摘要" in str(m.get("content") or "") for m in msgs):
                seen_summary = True
                break
        assert seen_summary, "压缩之后，后续调用的 messages 里应带摘要 user 消息"
        assert msgs[0]["role"] == "system"
        # system prompt 永不压缩（内容原样）
        assert "submit_result" in msgs[0]["content"]


class TestWorkersSpill:
    @pytest.mark.asyncio
    async def test_huge_tool_result_spilled_to_workspace(self, store, tools, tmp_path):
        huge = "前" * 30000 + "尾" * 30000

        async def huge_out(ctx, args):
            return ToolResult(ok=True, output=huge)

        from CharTyr_MaiWork.maiwork.tools import Tool

        tools.register(
            Tool(name="huge_out", description="d",
                 parameters={"type": "object", "properties": {}},
                 roles=frozenset({"worker"}), handler=huge_out, timeout_s=5.0)
        )
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("huge_out", {}, "c1")]),
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "完"}, "c2")]),
            ]
        )
        ws = tmp_path / "spill_workspace"
        ws.mkdir()
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["huge_out"], task_id="T-9", workspace=ws)
        assert report.ok
        spill_dir = ws / "tool_spill" / "T-9"
        assert spill_dir.is_dir()
        files = sorted(spill_dir.glob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == huge
        # 回给模型的 tool 消息提到文件路径（在 6000 截断之内）
        tool_msg = [m for m in models.calls[1][1] if m.get("role") == "tool"][0]
        assert len(tool_msg["content"]) <= 6000
        assert files[0].name in tool_msg["content"]


class TestRepeatNudgeInWorkers:
    @pytest.mark.asyncio
    async def test_third_repeat_gets_nudge(self, store, tools):
        models = ReplayModels(
            [
                FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "same"}, "c1")]),
                FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "same"}, "c2")]),
                FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "same"}, "c3")]),
                FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "完"}, "c4")]),
            ]
        )
        w = Workers(models, tools)
        report = await w.run("干活", group_id="1", tools=["web_search"], task_id="T-r")
        assert report.ok
        # 第 3 次调用后回给模型的内容里带提醒
        msgs4 = models.calls[3][1]
        assert any("连续第" in str(m.get("content") or "") for m in msgs4 if m.get("role") == "tool" or m.get("role") == "user")


class TestDeadlineWrapUp:
    @pytest.mark.asyncio
    async def test_deadline_forces_submit(self, store, tools, monkeypatch):
        """到点：催一次 submit_result；能交出已找到的就 ok。"""
        from CharTyr_MaiWork.maiwork import clock as _clock

        t = [1000.0]
        monkeypatch.setattr(_clock, "now", lambda: t[0])
        # workers 也用 clock（直接 import 的模块名）——monkeypatch 两处
        from CharTyr_MaiWork.maiwork import workers as _workers

        # ⚠️ 这里我们假设 Workers.run 用 clock.now() 判断截止
        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c1")]),
             FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c2")]),
             FakeChatResult(tool_calls=[_tool_call("submit_result", {"summary": "已找到的部分结果"}, "cz")])]
        )
        w = Workers(models, tools)
        async def run():
            return await w.run("干活", group_id="1", tools=["web_search"], task_id="T-d", deadline_ts=1000.0 + 60)
        # 先跑 3 步（不过期），然后让时间走过截止日期
        # 简化：第一步后立刻把时间推到超过 deadline
        orig_chat = models.chat

        async def chat_then_expire(role=None, messages=None, **kwargs):
            out = await orig_chat(role, messages, **kwargs)
            if len(models.calls) == 2:
                t[0] = 1000.0 + 120  # 第二步后过期
            return out

        models.chat = chat_then_expire  # type: ignore[method-assign]
        report = await run()
        assert report.ok is True
        assert report.summary == "已找到的部分结果"
        # 过期后那一轮只给 submit_result 工具
        #（找一次 tools 只有 submit_result 的调用）
        assert any(
            [s["function"]["name"] for s in kw["tools"]] == ["submit_result"]
            for _r, _m, kw in models.calls if kw.get("tools")
        )
        # messages 里带「时间到了」提示
        assert any(
            "时间" in str(m.get("content") or "") or "到点" in str(m.get("content") or "")
            for c in models.calls for m in c[1] if m.get("role") == "user"
        )

    @pytest.mark.asyncio
    async def test_deadline_no_submit_fails(self, store, tools):
        """到期强制交回时仍不交 → 失败（保留进展在 summary 里）。"""
        from CharTyr_MaiWork.maiwork import clock as _clock

        models = ReplayModels(
            [FakeChatResult(tool_calls=[_tool_call("web_search", {"query": "x"}, "c1")]) ] * 5
        )
        w = Workers(models, tools)
        report = await w.run(
            "干活", group_id="1", tools=["web_search"], task_id="T-d2",
            deadline_ts=_clock.now() - 1,  # 一开始就超了
        )
        # 强制交回这一轮模型还想继续调工具 → fail（步数自由、到点硬收）
        assert report.ok is False
        assert "submit_result" in report.summary or "交回" in models.calls or report.error


class TestWorkerAgentKind:
    """1b：Workers.run 把 agent_kind 传给 models.chat；默认 = agent_type。"""

    @pytest.mark.asyncio
    async def test_agent_defaults_to_agent_type(self, tools) -> None:
        w = Workers(models=None, tools=tools)
        models = ReplayModels([FakeChatResult(text="完工")])
        w._models = models
        await w.run("测", group_id="1", tools=[], actor="子 agent #1", agent_type="c_abc123")
        assert models.calls
        assert models.calls[0][2].get("agent") == "c_abc123"

    @pytest.mark.asyncio
    async def test_explicit_agent_overrides(self, tools) -> None:
        w = Workers(models=None, tools=tools)
        models = ReplayModels([FakeChatResult(text="完工")])
        w._models = models
        await w.run("测", group_id="1", tools=[], actor="子 agent #1",
                    agent_type="task", agent="news")
        assert models.calls[0][2].get("agent") == "news"
