"""coordinator 主模型侧上下文压缩（0.4.0）：工具排计划 / 验收多轮累积超线时，
先跑一次压缩（截旧结果 / 摘要），模型回「上下文超长」裁最旧一段再重试一次。"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

pytestmark = pytest.mark.asyncio


class _FakeSettingsModels:
    context_window = 8192


class _FakeSettings:
    models = _FakeSettingsModels()


class _Models:
    def __init__(self, gate=None):
        self.calls = []
        self.plan = []
        self._gate = gate

    def settings(self):
        return type("S", (), {"ready": lambda: True, "context_window": 8192})()

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append((role, [dict(m) for m in messages], dict(kw)))
        if str(kw.get("purpose") or "").endswith(":compact"):
            from CharTyr_MaiWork.maiwork.models import ChatResult
            return ChatResult(text="Primary Request and Intent 摘要 8 节", tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={})
        if self._gate is not None:
            return self._gate(messages)


class _Tools:
    def specs(self, role, names=None):
        return [
            {"type": "function", "function": {"name": "list_skills", "description": "d", "parameters": {"type": "object", "properties": {}}}},
        ]

    async def call(self, name, args, ctx):
        from CharTyr_MaiWork.maiwork.tools import ToolResult
        return ToolResult(ok=True, output="x" * 20000)


class _Env:
    def workspace(self, name):
        return None


def _coordinator(models):
    store_dir = None
    import tempfile, pathlib
    tmp = tempfile.mkdtemp()
    store = Store(pathlib.Path(tmp) / "t.db")
    store.migrate()
    tasks = Tasks(store, lambda: _FakeSettings())
    coord = Coordinator(
        store=store,
        models=models,
        workers=None,
        tools=_Tools(),
        tasks=tasks,
        goals=None,
        delivery=None,
        outbox=None,
        env=_Env(),
        profiles=None,
        get_settings=lambda: _FakeSettings(),
    )
    return coord


class TestMainCompaction:
    async def test_plan_loop_compacts_before_chat(self):
        """排计划工具回合多次后，上下文超线 -> 摘要消息出现在后续调用里。"""
        from CharTyr_MaiWork.maiwork.models import ChatResult

        big_out = "x" * 20000
        rounds = {"n": 0}

        def gate(messages):
            rounds["n"] += 1
            if rounds["n"] <= 4:
                return ChatResult(
                    text="", tool_calls=[{"id": f"c{rounds['n']}", "type": "function", "function": {"name": "list_skills", "arguments": "{}"}}],
                    model="m", prompt_tokens=1, completion_tokens=1, raw_message={},
                )
            return ChatResult(
                text='{"criteria": ["标准1"], "deliver_kind": "text", "jobs": [{"brief": "查个东西", "tools": []}], "question": null, "env": "local"}',
                tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={},
            )

        models = _Models(gate=gate)
        coord = _coordinator(models)
        plan = await coord._plan({"id": "T-1", "group_id": "9", "title": "测试", "req": "r", "criteria": [], "workspace": "g9"})
        assert plan["deliver_kind"] in ("view", "file", "text")
        # 中间有 :compact 调用
        assert any(str(k.get("purpose") or "").endswith(":compact") for _r, _m, k in models.calls)
        # 最后一轮的 messages：第一行为 system(user role for plan, but summary inserted as user)
        # 至少存在一条带摘要标记的 user 消息
        assert any("前面对话的摘要" in str(m.get("content") or "") for _r, msgs, _k in models.calls for m in msgs)

    async def test_context_length_error_trims_and_retries(self):
        """模型返回上下文超长 -> 裁掉最旧一段重试一次。"""
        from CharTyr_MaiWork.maiwork.models import ChatResult, ModelError

        state = {"n": 0}

        def gate(messages):
            state["n"] += 1
            if state["n"] == 1:
                raise ModelError("maximum context length exceeded", status=400)
            return ChatResult(text='{"done": true}', tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={})

        models = _Models(gate=gate)
        coord = _coordinator(models)
        messages = [
            {"role": "user", "content": "第一问" + "x" * 4000},
            {"role": "assistant", "content": "回答1" + "y" * 4000},
            {"role": "user", "content": "第二问"},
        ]
        result = await coord._chat_main(messages, purpose="t", json_mode=False)
        assert state["n"] == 2
        assert result.text
        # 第二次调用的 messages 少了最旧一段
        assert len(models.calls[1][1]) < len(models.calls[0][1])
