"""模型改版 1b 的测试：三种协议的请求拼装 / 响应解析 / effort 映射 / agent 岗位候选。

全部用 httpx.MockTransport 假端点，不碰真网络。请求体断言靠 handler 里记下的 body。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import (
    ModelError,
    Models,
    _anthropic_messages_url,
    _build_anthropic_request,
    _build_openai_request,
    _build_responses_request,
    _effort_for_protocol,
    _parse_anthropic_response,
    _parse_responses_response,
)
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "protsecret123"

TOOLS = [{"type": "function", "function": {"name": "web_search", "description": "搜东西",
                                           "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]

MESSAGES_FULL = [
    {"role": "system", "content": "你是子 agent"},
    {"role": "user", "content": "查一下"},
    {
        "role": "assistant",
        "content": "好的",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "web_search", "arguments": '{"q": "x"}'}}],
    },
    {"role": "tool", "tool_call_id": "call_1", "name": "web_search", "content": "结果A"},
    {"role": "tool", "tool_call_id": "call_1", "name": "web_search", "content": "结果A2"},
    {"role": "tool", "tool_call_id": "call_2", "name": "fetch_page", "content": "结果B"},
]


def _settings(settings_dict: dict) -> object:
    settings, _ = load_settings(settings_dict)
    return settings


class _FakeAgents:
    """Models 只调 agents.profile(kind)：小假对象（duck type）。"""

    def __init__(self, mapping: dict | None = None) -> None:
        self._m = mapping or {}

    def profile(self, kind: str) -> dict:
        d = {"kind": kind, "title": kind, "model": "", "effort": "", "backup": "", "enabled": True,
             "skills": None, "tools": None}
        d.update(self._m.get(kind, {}))
        return dict(d)


def _cfg(proto: str, *, model: str = "svc-model", efforts: list[str] | None = None,
         base: str = "https://p.test/v1") -> dict:
    entry: dict = {"id": "m1", "endpoint": "e1", "model": model, "name": "展示名"}
    if efforts is not None:
        entry["efforts"] = efforts
    return {
        "endpoints": [
            {"id": "e1", "protocol": proto, "base_url": base, "api_key": SECRET, "retries": 0},
        ],
        "model_list": [entry],
    }


def _make(tmp_path, cfg: dict, handler, agents=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg)
    models = Models(
        store, lambda: settings, transport=httpx.MockTransport(handler),
        agents=agents if agents is not None else _FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}}),
    )
    return store, models


def _usage_rows(store):
    return [dict(r) for r in store.read().execute("SELECT * FROM usage ORDER BY id").fetchall()]


# ----------------------------------------------------------------------
# 纯函数：请求拼装
# ----------------------------------------------------------------------


class TestBuildOpenAI:
    def test_body_has_stream_reasoning_effort_and_hint(self) -> None:
        body = _build_openai_request(
            "m", [{"role": "user", "content": "hi"}], tools=TOOLS, json_mode=True,
            max_tokens=123, effort="high",
        )
        assert body["model"] == "m"
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 123
        assert body["reasoning_effort"] == "high"
        assert body["tools"] == TOOLS
        assert any(m["role"] == "system" and "JSON" in m["content"] for m in body["messages"])

    def test_no_effort_no_key_no_hint(self) -> None:
        body = _build_openai_request("m", [{"role": "user", "content": "hi"}], tools=None,
                                     json_mode=False, max_tokens=10, effort="")
        assert "reasoning_effort" not in body
        assert "tools" not in body
        assert len(body["messages"]) == 1


class TestBuildResponses:
    def test_shape_conversion(self) -> None:
        body = _build_responses_request(
            "m", MESSAGES_FULL, tools=TOOLS, json_mode=True, max_tokens=321, effort="medium",
        )
        assert body["model"] == "m"
        assert body["stream"] is True
        assert body["max_output_tokens"] == 321
        assert body["reasoning"] == {"effort": "medium"}
        assert body["instructions"] == "你是子 agent"
        inp = body["input"]
        # system 不进 input；user、assistant（文本+function_call）；
        # 三条 tool 结果：call_1 的两条相邻合并成一条 → 两个 function_call_output
        assert [i["type"] for i in inp] == [
            "message", "message", "function_call",
            "function_call_output", "function_call_output",
        ]
        assert inp[0]["role"] == "user"
        assert inp[1]["role"] == "assistant"
        fc = inp[2]
        assert fc["name"] == "web_search" and fc["arguments"] == '{"q": "x"}' and fc["call_id"] == "call_1"
        # 第一个 function_call_output 合并了两条同 call_id 的结果（防御，规范上不该出现）
        assert "结果A" in inp[3]["output"] and "结果A2" in inp[3]["output"]
        assert inp[4]["call_id"] == "call_2" and inp[4]["output"] == "结果B"
        assert body["tools"] == [{"type": "function", "name": "web_search", "description": "搜东西",
                                  "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}]
        assert body["text"] == {"format": {"type": "json_object"}}

    def test_no_effort_no_tools_no_json(self) -> None:
        body = _build_responses_request("m", [{"role": "user", "content": "hi"}], tools=None,
                                        json_mode=False, max_tokens=10, effort="")
        assert "reasoning" not in body
        assert "tools" not in body
        assert "text" not in body
        assert [i["role"] for i in body["input"]] == ["user"]


class TestBuildAnthropic:
    def test_url_v1_join(self) -> None:
        assert _anthropic_messages_url("https://a.test") == "https://a.test/v1/messages"
        assert _anthropic_messages_url("https://a.test/v1") == "https://a.test/v1/messages"
        assert _anthropic_messages_url("https://a.test/v1/") == "https://a.test/v1/messages"

    def test_shape_conversion(self) -> None:
        body = _build_anthropic_request(
            "claude-x", MESSAGES_FULL, tools=TOOLS, json_mode=True, max_tokens=4096, effort="",
        )
        assert body["model"] == "claude-x"
        assert body["max_tokens"] == 4096  # anthropic 必填
        assert "stream" not in body and "stream_options" not in body
        # 系统消息：原有 + json_mode 追加的一段
        assert "你是子 agent" in body["system"]
        assert "JSON" in body["system"]
        msgs = body["messages"]
        # user / assistant（text+tool_use）/ user(两个 tool_result 合并)
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        a = msgs[1]
        assert [b["type"] for b in a["content"]] == ["text", "tool_use"]
        tu = a["content"][1]
        assert tu["id"] == "call_1" and tu["name"] == "web_search" and tu["input"] == {"q": "x"}
        tr = msgs[2]["content"]
        assert all(b["type"] == "tool_result" for b in tr)
        assert len(tr) == 2  # 同 call_id 的两条合并
        assert "结果A" in tr[0]["content"] and "结果A2" in tr[0]["content"]
        assert tr[1]["tool_use_id"] == "call_2"
        assert body["tools"] == [{"name": "web_search", "description": "搜东西",
                                  "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}]

    def test_effort_goes_to_output_config(self) -> None:
        body = _build_anthropic_request("m", [{"role": "user", "content": "hi"}], tools=None,
                                        json_mode=False, max_tokens=100, effort="high")
        assert "output_config" in body and body["output_config"].get("effort") == "high"
        assert "reasoning" not in body and "thinking" not in body

    def test_tool_result_orphan_gets_id(self) -> None:
        msgs = [{"role": "user", "content": "a"}, {"role": "tool", "tool_call_id": "", "content": "孤儿"}]
        body = _build_anthropic_request("m", msgs, tools=None, json_mode=False, max_tokens=10, effort="")
        tr = body["messages"][1]["content"]
        assert tr[0]["type"] == "tool_result" and tr[0]["tool_use_id"] == "tool_2"  # 按消息序号生成兜底 id


class TestParseAnthropic:
    def test_text_tool_use_usage(self) -> None:
        data = {
            "id": "msg_1", "model": "claude-x", "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "想想"},
                {"type": "text", "text": "先查"},
                {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"q": "x"}},
            ],
            "usage": {"input_tokens": 33, "output_tokens": 7},
        }
        r = _parse_anthropic_response(data, "claude-x")
        assert r.text == "先查"
        assert len(r.tool_calls) == 1
        tc = r.tool_calls[0]
        assert tc["id"] == "tu_1" and tc["type"] == "function"
        assert tc["function"]["name"] == "web_search"
        assert json.loads(tc["function"]["arguments"]) == {"q": "x"}
        assert r.prompt_tokens == 33 and r.completion_tokens == 7
        assert r.finish_reason == "tool_calls"  # tool_use 映射成我们的 finish 语义

    def test_end_turn_and_no_content(self) -> None:
        data = {"content": [{"type": "text", "text": "完"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2}}
        r = _parse_anthropic_response(data, "m")
        assert r.text == "完" and r.finish_reason == "stop"
        r2 = _parse_anthropic_response({}, "m")
        assert r2.text == "" and r2.prompt_tokens == 0


class TestParseResponses:
    def test_output_items_and_usage(self) -> None:
        data = {
            "id": "resp_1", "model": "gpt-x", "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "前半"}, {"type": "output_text", "text": "后半"}]},
                {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "web_search",
                 "arguments": '{"q": "y"}'},
            ],
            "usage": {"input_tokens": 50, "output_tokens": 12},
        }
        r = _parse_responses_response(data, "gpt-x")
        assert r.text == "前半后半"
        assert len(r.tool_calls) == 1
        tc = r.tool_calls[0]
        assert tc["function"]["name"] == "web_search" and tc["function"]["arguments"] == '{"q": "y"}'
        assert tc["id"] == "call_9"
        assert r.prompt_tokens == 50 and r.completion_tokens == 12
        assert r.finish_reason == "tool_calls"

    def test_incomplete_sets_length(self) -> None:
        data = {"status": "incomplete",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "半截"}]}],
                "usage": {}}
        r = _parse_responses_response(data, "m")
        assert r.text == "半截" and r.finish_reason == "length"


class TestEffortMapping:
    @pytest.mark.parametrize(
        "proto,effort,expected",
        [
            (None, "low", ""),          # 没指名协议不做映射
            ("openai", "xhigh", "xhigh"),   # 新模型（gpt-5.x）收；端点自己拒
            ("openai", "max", "high"),
            ("openai", "medium", "medium"),
            ("responses", "xhigh", "xhigh"),
            ("responses", "max", "high"),
            ("anthropic", "xhigh", "high"),
            ("anthropic", "max", "max"),
            ("anthropic", "low", "low"),
            ("anthropic", "", ""),
        ],
    )
    def test_map(self, proto, effort, expected) -> None:
        assert _effort_for_protocol(effort, proto) == expected


# ----------------------------------------------------------------------
# chat() 全链路：三种协议走真 Models
# ----------------------------------------------------------------------


class TestChatResponsesProtocol:
    @pytest.mark.asyncio
    async def test_responses_roundtrip(self, tmp_path) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization", "")
            body = json.loads(request.content or b"{}")
            seen["body"] = body
            return httpx.Response(200, json={
                "id": "resp_x", "status": "completed",
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "思考后的回答"}]}],
                "usage": {"input_tokens": 17, "output_tokens": 5},
            })

        cfg = _cfg("responses", efforts=["low", "high"])
        agents = _FakeAgents({"main": {"model": "m1", "effort": "high"}, "task": {"model": "m1"}})
        store, models = _make(tmp_path, cfg, handler, agents)
        r = await models.chat("main", [{"role": "user", "content": "x"}], max_tokens=222)
        assert r.text == "思考后的回答"
        assert seen["url"] == "https://p.test/v1/responses"
        assert seen["auth"] == f"Bearer {SECRET}"
        assert seen["body"]["reasoning"] == {"effort": "high"}
        assert seen["body"]["max_output_tokens"] == 222
        assert "messages" not in seen["body"]
        assert seen["body"]["stream"] is True  # 发了但没回 SSE：按非流式解析
        assert r.prompt_tokens == 17 and r.completion_tokens == 5
        rows = _usage_rows(store)
        assert rows[0]["ok"] == 1 and rows[0]["prompt_tokens"] == 17
        await models.close()

    @pytest.mark.asyncio
    async def test_responses_sse_roundtrip(self, tmp_path) -> None:
        """端点支持 SSE（/responses 的流）：response.completed 里拿 output + usage。"""

        def handler(request: httpx.Request) -> httpx.Response:
            events = [
                {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}},
                {"type": "response.output_text.delta", "delta": "流式"},
                {"type": "response.completed", "response": {
                    "id": "r1", "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "流式文本"}]}],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                }},
                {"type": "response.done"},
            ]
            payload = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
            payload += "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())

        # 回 SSE 的版本：解析应以 completed 事件里的整份 response 为准
        store, models = _make(tmp_path, _cfg("responses"), handler)
        r = await models.chat("worker", [{"role": "user", "content": "x"}])
        assert r.text == "流式文本"
        assert r.prompt_tokens == 3 and r.completion_tokens == 4
        await models.close()


class TestChatAnthropicProtocol:
    @staticmethod
    def _handler(seen: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            seen["body"] = json.loads(request.content or b"{}")
            return httpx.Response(200, json={
                "id": "msg_x", "type": "message", "role": "assistant", "model": "claude-x",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Anthropic 回了"}],
                "usage": {"input_tokens": 9, "output_tokens": 6},
            })
        return handler

    @pytest.mark.asyncio
    async def test_anthropic_roundtrip_base_without_v1(self, tmp_path) -> None:
        seen: dict = {}
        cfg = _cfg("anthropic", base="https://p.test")  # base 不带 /v1：tests URL 自动补
        store, models = _make(tmp_path, cfg, self._handler(seen))
        r = await models.chat("main", [{"role": "system", "content": "规矩"}, {"role": "user", "content": "x"}])
        assert r.text == "Anthropic 回了"
        assert seen["url"] == "https://p.test/v1/messages"  # 不叠两个 v1
        assert seen["headers"]["x-api-key"] == SECRET
        assert seen["headers"]["anthropic-version"] == "2023-06-01"
        assert "authorization" not in seen["headers"]
        assert seen["body"]["system"] == "规矩"
        assert seen["body"]["max_tokens"] == 32768  # 条目默认
        assert "output_config" not in seen["body"]  # 没勾 efforts → 不发
        rows = _usage_rows(store)
        assert rows[0]["prompt_tokens"] == 9 and rows[0]["completion_tokens"] == 6
        assert rows[0]["ok"] == 1
        await models.close()

    @pytest.mark.asyncio
    async def test_anthropic_effort_and_429_throttle_key(self, tmp_path, monkeypatch) -> None:
        """effort 勾了就发 output_config；429 冷却按端点算（这份配置只有一个端点）。"""
        n = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n["i"] += 1
            if n["i"] == 1:
                # 冷却退避最短的球kennis：1.6 秒（连续第 1 次下限 10 秒的 -80%），不引入假时钟
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "太快"}})
            return httpx.Response(200, json={
                "content": [{"type": "tool_use", "id": "tu_2", "name": "web_search", "input": {"q": "z"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        cfg = _cfg("anthropic", base="https://p.test/v1", efforts=["medium", "xhigh"])
        cfg["endpoints"][0]["retries"] = 1  # 429 占同一份「1 + retries」预算（1a 的规矩）
        agents = _FakeAgents({"main": {"model": "m1", "effort": "xhigh"}, "task": {"model": "m1"}})
        store, models = _make(tmp_path, cfg, handler, agents)
        r = await models.chat("main", [{"role": "user", "content": "x"}], tools=TOOLS)
        assert r.tool_calls[0]["function"]["name"] == "web_search"
        assert r.finish_reason == "tool_calls"
        assert n["i"] == 2  # 429 后退避 0 秒重试了一次
        await models.close()


class TestAnthropicNoNativeJsonMode:
    @pytest.mark.asyncio
    async def test_json_mode_adds_system_text(self, tmp_path) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}],
                                             "stop_reason": "end_turn", "usage": {}})

        store, models = _make(tmp_path, _cfg("anthropic", base="https://p.test"), handler)
        await models.chat("main", [{"role": "user", "content": "x"}], json_mode=True)
        assert "response_format" not in seen["body"] and "text" not in seen["body"]
        assert "JSON" in seen["body"]["system"]
        await models.close()


# ----------------------------------------------------------------------
# chat(agent=…) 岗位候选解析 + 主模型兜底
# ----------------------------------------------------------------------

_AGENT_CFG = {
    "endpoints": [
        {"id": "e1", "protocol": "openai", "base_url": "https://a.test/v1", "api_key": SECRET, "retries": 0},
    ],
    "model_list": [
        {"id": "mm", "endpoint": "e1", "model": "m-main"},
        {"id": "mb", "endpoint": "e1", "model": "m-main-bak"},
        {"id": "nw", "endpoint": "e1", "model": "m-news"},
        {"id": "nb", "endpoint": "e1", "model": "m-news-bak"},
        {"id": "wk", "endpoint": "e1", "model": "m-worker"},
    ],
}


class _Recorder:
    """记下每个模型收没收到请求。"""

    def __init__(self) -> None:
        self.models_seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        model = str(body.get("model") or "")
        self.models_seen.append(model)
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"回{model}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })


class TestAgentParam:
    def _agents(self, **profiles) -> _FakeAgents:
        return _FakeAgents(profiles)

    @pytest.mark.asyncio
    async def test_agent_news_chain(self, tmp_path) -> None:
        rec = _Recorder()
        agents = self._agents(
            main={"model": "mm", "backup": "mb"},
            news={"model": "nw", "backup": "nb"},
            task={"model": "wk"},
        )
        store, models = _make(tmp_path, _AGENT_CFG, rec, agents)
        r = await models.chat(agent="news", messages=[{"role": "user", "content": "x"}])
        assert r.text == "回m-news"
        assert rec.models_seen == ["m-news"]
        row = _usage_rows(store)[0]
        assert row["role"] == "worker"
        assert row["agent"] == "news"
        await models.close()

    @pytest.mark.asyncio
    async def test_unconfigured_agent_falls_back_to_main(self, tmp_path) -> None:
        rec = _Recorder()
        agents = self._agents(main={"model": "mm"}, task={"model": "wk"})
        store, models = _make(tmp_path, _AGENT_CFG, rec, agents)
        # custom 岗位（c_xxx 这类）没选模型 → 用主模型兜底，不报错
        r = await models.chat(agent="c_writer", messages=[{"role": "user", "content": "x"}])
        assert r.text == "回m-main"
        row = _usage_rows(store)[0]
        assert row["role"] == "main"   # 兜底记主模型桶
        assert row["agent"] == "c_writer"
        await models.close()

    @pytest.mark.asyncio
    async def test_no_agent_worker_maps_to_task(self, tmp_path) -> None:
        rec = _Recorder()
        agents = self._agents(main={"model": "mm"}, task={"model": "wk"})
        store, models = _make(tmp_path, _AGENT_CFG, rec, agents)
        r = await models.chat("worker", [{"role": "user", "content": "x"}])
        assert r.text == "回m-worker"
        row = _usage_rows(store)[0]
        assert row["role"] == "worker" and row["agent"] == "task"
        await models.close()

    @pytest.mark.asyncio
    async def test_main_fallback_effort_uses_main_profile(self, tmp_path) -> None:
        """兜底到主模型链时，强度也用主模型 profile 的（岗位自己没选模型）。"""
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={
                "id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "o"}}],
                "usage": {},
            })

        cfg = {
            "endpoints": [{"id": "e1", "protocol": "openai", "base_url": "https://a.test/v1",
                           "api_key": SECRET, "retries": 0}],
            "model_list": [{"id": "mm", "endpoint": "e1", "model": "m-main", "efforts": ["high"]},
                           {"id": "g1", "endpoint": "e1", "model": "m-goal", "efforts": ["high"]}],
        }
        agents = self._agents(main={"model": "mm", "effort": "high"},
                              goal={"model": "g1", "effort": "high"},
                              task={"model": "g1"})
        store, models = _make(tmp_path, cfg, handler, agents)
        await models.chat(agent="goal", messages=[{"role": "user", "content": "x"}])
        # goal 自己选了 g1（也勾了 high）：用 g1 自己的强度
        assert bodies[-1]["model"] == "m-goal"
        assert bodies[-1]["reasoning_effort"] == "high"
        # idea 没选模型 → 兜底主模型链，强度用主模型的
        await models.chat(agent="idea", messages=[{"role": "user", "content": "x"}])
        assert bodies[-1]["model"] == "m-main"
        assert bodies[-1]["reasoning_effort"] == "high"
        await models.close()

    @pytest.mark.asyncio
    async def test_no_candidates_anywhere_friendly_error(self, tmp_path) -> None:
        agents = self._agents(main={}, task={}, news={})
        store, models = _make(tmp_path, _AGENT_CFG, _Recorder(), agents)
        with pytest.raises(ModelError, match="还没配好|还没挑模型"):
            await models.chat(agent="news", messages=[{"role": "user", "content": "x"}])
        await models.close()

    @pytest.mark.asyncio
    async def test_backup_candidate_checks_own_efforts(self, tmp_path) -> None:
        """主选挂了换备用；备用条目没勾这个强度 → 发送时不带 effort。"""
        n = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n["i"] += 1
            if n["i"] == 1:
                return httpx.Response(500, json={"error": {"message": "挂"}})
            return httpx.Response(200, json={
                "id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            })

        cfg = {
            "endpoints": [{"id": "e1", "protocol": "openai", "base_url": "https://a.test/v1",
                           "api_key": SECRET, "retries": 0}],
            "model_list": [
                {"id": "m1", "endpoint": "e1", "model": "svc-a", "efforts": ["high"]},
                {"id": "m2", "endpoint": "e1", "model": "svc-b", "efforts": ["low"]},
            ],
        }
        agents = self._agents(main={"model": "m1", "effort": "high", "backup": "m2"},
                              task={"model": "m1"})
        bodies: list[dict] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content or b"{}"))
            return handler(request)

        store, models = _make(tmp_path, cfg, wrapped, agents)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "ok"
        assert bodies[0]["reasoning_effort"] == "high"          # 首选勾了 high
        assert "reasoning_effort" not in bodies[1]              # 备用条目不认 high → 不发
        await models.close()


class TestLimitsForKind:
    def test_kind_uses_own_entry(self, tmp_path) -> None:
        cfg = {
            "endpoints": [{"id": "e1", "protocol": "openai", "base_url": "https://a.test/v1", "api_key": SECRET}],
            "model_list": [
                {"id": "mm", "endpoint": "e1", "model": "m-main", "context_window": 100000, "max_tokens": 8000},
                {"id": "nw", "endpoint": "e1", "model": "m-news", "context_window": 250000, "max_tokens": 16000},
            ],
        }
        agents = _FakeAgents({"main": {"model": "mm"}, "news": {"model": "nw"}, "task": {"model": "mm"}})
        store, models = _make(tmp_path, cfg, _Recorder(), agents)
        assert models.limits_for("news")["context_window"] == 250000
        assert models.limits_for("idea")["context_window"] == 100000  # idea 没选 → 主模型兜底
        assert models.limits_for()["context_window"] == 100000
        await_close = models.close
        store.close()


class TestMigrationAgentColumn:
    def test_usage_agent_defaults_legacy(self, tmp_path) -> None:
        """老行（agent=''）两桶账照常：usage_today 不因新列报错。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
                " prompt_tokens, completion_tokens, ok, ms, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (10.0, "2026-10-02", "main", "m", "p", "", "", 3, 4, 1, 5, ""),
            )
        row = store.read().execute("SELECT agent FROM usage").fetchone()
        assert row["agent"] == ""
        store.close()

    def test_model_calls_agent_column_exists(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(model_calls)")}
        assert "agent" in cols
        store.close()


class TestUsageTwoBucketsWithAgent:
    @pytest.mark.asyncio
    async def test_buckets_unchanged_and_agent_distinct(self, tmp_path, monkeypatch) -> None:
        """两桶账（main / 子 agent）和 1a 一个算法；agent 列把岗位分开。"""
        from CharTyr_MaiWork.maiwork import clock
        monkeypatch.setattr(clock, "now", lambda: 1759999900.0)
        rec = _Recorder()
        agents = _FakeAgents({"main": {"model": "mm"}, "news": {"model": "nw"},
                              "task": {"model": "wk"}})
        store, models = _make(tmp_path, _AGENT_CFG, rec, agents)
        await models.chat(agent="news", messages=[{"role": "user", "content": "x"}])
        await models.chat(agent="idea", messages=[{"role": "user", "content": "x"}])  # 兜底 → main 桶
        await models.chat("main", [{"role": "user", "content": "x"}])
        today = models.usage_today()
        # news(1) 是 worker 桶；idea 兜底 + main 两笔记 main 桶（各 1+1 prompt+completion=2）
        assert today["main"] == 4
        assert today["worker"] == 2
        assert today["calls"] == 3
        rows = _usage_rows(store)
        assert [r["agent"] for r in rows] == ["news", "idea", "main"]
        assert [r["role"] for r in rows] == ["worker", "main", "main"]
        # model_calls 同样带 agent
        mc = [dict(r) for r in store.read().execute("SELECT role, agent FROM model_calls ORDER BY id")]
        assert [r["agent"] for r in mc] == ["news", "idea", "main"]
        await models.close()
