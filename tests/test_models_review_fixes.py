"""外部审查 2026-10-02（docs/16）模型调用层的修复：

- HTTP 200 但响应体是错误（中转站常见）：三家协议都当真实错误——重试、换备用，不当成「空回答」；
- Responses 流里的 response.incomplete 是合法终态（保住已生成的内容），response.failed / error 事件带原因失败；
- OpenAI 官方推理模型不认 max_tokens：400 提示改用 max_completion_tokens 时换参数名重发，同端点同模型记住；
- 思考强度原样发（OpenAI/Anthropic 现在都有 xhigh / max）；端点拒了就降一档重发（max→xhigh→high→不发）；
- Responses / Anthropic 的「工具结果回传后再调一轮」完整回环。

全部用 httpx.MockTransport 假端点，不碰真网络。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.models import ModelError
from test_models_protocols import SECRET, TOOLS, _FakeAgents, _make, _usage_rows


def _cfg2(proto: str, *, efforts: list[str] | None = None, retries: int = 0) -> dict:
    """一个端点两条模型：首选 svc-a、备用 svc-b。"""
    def entry(mid: str, model: str) -> dict:
        e: dict = {"id": mid, "endpoint": "e1", "model": model}
        if efforts is not None:
            e["efforts"] = efforts
        return e

    return {
        "endpoints": [{"id": "e1", "protocol": proto, "base_url": "https://p.test/v1",
                       "api_key": SECRET, "retries": retries}],
        "model_list": [entry("m1", "svc-a"), entry("m2", "svc-b")],
    }


_AGENTS_WITH_BACKUP = {"main": {"model": "m1", "backup": "m2"}, "task": {"model": "m1"}}


def _ok_body(proto: str, text: str) -> dict:
    if proto == "openai":
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}], "usage": {}}
    if proto == "responses":
        return {"status": "completed", "error": None,
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    return {"type": "message", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}


_ERROR_BODIES = {
    "openai": {"error": {"message": "upstream 挂了", "type": "upstream_error"}},
    "responses": {"status": "failed", "error": {"code": "server_error", "message": "upstream 挂了"}, "output": []},
    "anthropic": {"type": "error", "error": {"type": "overloaded_error", "message": "upstream 挂了"}},
}


# ----------------------------------------------------------------------
# 200 + 错误体
# ----------------------------------------------------------------------


class TestEmbeddedErrorBody:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("proto", ["openai", "responses", "anthropic"])
    async def test_falls_back_to_backup(self, tmp_path, proto) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content or b"{}").get("model")
            seen.append(model)
            if model == "svc-a":
                return httpx.Response(200, json=_ERROR_BODIES[proto])
            return httpx.Response(200, json=_ok_body(proto, "备用回了"))

        store, models = _make(tmp_path, _cfg2(proto), handler, _FakeAgents(_AGENTS_WITH_BACKUP))
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用回了"
        assert seen == ["svc-a", "svc-b"]
        rows = _usage_rows(store)
        assert rows[0]["ok"] == 0 and "upstream 挂了" in rows[0]["error"]
        await models.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("proto", ["openai", "responses", "anthropic"])
    async def test_no_backup_raises_not_empty_answer(self, tmp_path, proto) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ERROR_BODIES[proto])

        agents = _FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}})
        store, models = _make(tmp_path, _cfg2(proto), handler, agents)
        with pytest.raises(ModelError, match="upstream 挂了"):
            await models.chat("main", [{"role": "user", "content": "x"}])
        await models.close()

    @pytest.mark.asyncio
    async def test_error_null_is_success(self, tmp_path) -> None:
        """Responses 正常回答里本来就带 "error": null，不能误判。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body("responses", "正常"))

        store, models = _make(tmp_path, _cfg2("responses"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "正常"
        await models.close()


# ----------------------------------------------------------------------
# Responses SSE 终态
# ----------------------------------------------------------------------


def _sse(events: list[dict], done: bool = False) -> httpx.Response:
    payload = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    if done:
        payload += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())


class TestResponsesStreamTerminal:
    @pytest.mark.asyncio
    async def test_incomplete_keeps_text(self, tmp_path) -> None:
        n = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n["i"] += 1
            return _sse([
                {"type": "response.output_text.delta", "delta": "半截"},
                {"type": "response.incomplete", "response": {
                    "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "写到一半"}]}],
                    "usage": {"input_tokens": 5, "output_tokens": 9}}},
            ])

        cfg = _cfg2("responses", retries=2)
        store, models = _make(tmp_path, cfg, handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "写到一半"
        assert r.finish_reason == "length"
        assert r.completion_tokens == 9
        assert n["i"] == 1  # 不当断流重试
        await models.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", [
        {"type": "response.failed", "response": {"status": "failed",
                                                 "error": {"code": "server_error", "message": "模型内部出错"}}},
        {"type": "error", "code": "server_error", "message": "模型内部出错"},
    ])
    async def test_failed_reports_reason(self, tmp_path, event) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _sse([{"type": "response.created", "response": {"status": "in_progress"}}, event])

        store, models = _make(tmp_path, _cfg2("responses"), handler)
        with pytest.raises(ModelError, match="模型内部出错"):
            await models.chat("main", [{"role": "user", "content": "x"}])
        await models.close()


# ----------------------------------------------------------------------
# max_completion_tokens 适配
# ----------------------------------------------------------------------


class TestMaxCompletionTokens:
    @pytest.mark.asyncio
    async def test_switches_param_and_remembers(self, tmp_path) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            bodies.append(body)
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {
                    "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                               "Use 'max_completion_tokens' instead.",
                    "type": "invalid_request_error", "param": "max_tokens", "code": "unsupported_parameter"}})
            return httpx.Response(200, json=_ok_body("openai", "推理模型回了"))

        store, models = _make(tmp_path, _cfg2("openai"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}], max_tokens=500)
        assert r.text == "推理模型回了"
        assert len(bodies) == 2
        assert bodies[1]["max_completion_tokens"] == 500 and "max_tokens" not in bodies[1]
        # 同端点同模型第二次直接用新参数名，不再白挨一次 400
        await models.chat("main", [{"role": "user", "content": "y"}], max_tokens=500)
        assert len(bodies) == 3 and "max_completion_tokens" in bodies[2]
        await models.close()

    @pytest.mark.asyncio
    async def test_other_400_still_raises(self, tmp_path) -> None:
        n = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n["i"] += 1
            return httpx.Response(400, json={"error": {"message": "messages 格式不对"}})

        store, models = _make(tmp_path, _cfg2("openai"), handler)
        with pytest.raises(ModelError, match="400"):
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert n["i"] == 1
        await models.close()


# ----------------------------------------------------------------------
# 思考强度：原样发，被拒降档
# ----------------------------------------------------------------------


def _effort_of(proto: str, body: dict) -> str:
    if proto == "openai":
        return str(body.get("reasoning_effort") or "")
    if proto == "responses":
        return str((body.get("reasoning") or {}).get("effort") or "")
    return str((body.get("output_config") or {}).get("effort") or "")


class TestEffortStepDown:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("proto", ["openai", "responses", "anthropic"])
    async def test_max_sent_as_is_then_steps_down(self, tmp_path, proto) -> None:
        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            e = _effort_of(proto, json.loads(request.content or b"{}"))
            sent.append(e)
            if e in ("max", "xhigh"):
                return httpx.Response(400, json={"error": {
                    "message": f"Unsupported value: reasoning effort does not support '{e}' with this model."}})
            return httpx.Response(200, json=_ok_body(proto, "降档后回了"))

        agents = _FakeAgents({"main": {"model": "m1", "effort": "max"}, "task": {"model": "m1"}})
        store, models = _make(tmp_path, _cfg2(proto, efforts=["high", "xhigh", "max"]), handler, agents)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "降档后回了"
        assert sent == ["max", "xhigh", "high"]
        # 记住：下次直接从 high 发
        await models.chat("main", [{"role": "user", "content": "y"}])
        assert sent[-1] == "high" and len(sent) == 4
        await models.close()

    @pytest.mark.asyncio
    async def test_low_rejected_drops_effort(self, tmp_path) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            bodies.append(body)
            if "output_config" in body:
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": "output_config.effort: not supported on this model"}})
            return httpx.Response(200, json=_ok_body("anthropic", "不带强度回了"))

        agents = _FakeAgents({"main": {"model": "m1", "effort": "low"}, "task": {"model": "m1"}})
        store, models = _make(tmp_path, _cfg2("anthropic", efforts=["low"]), handler, agents)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "不带强度回了"
        assert len(bodies) == 2 and "output_config" not in bodies[1]
        await models.close()


# ----------------------------------------------------------------------
# 工具结果回传后再调一轮（完整回环）
# ----------------------------------------------------------------------


class TestToolLoopRoundtrip:
    @pytest.mark.asyncio
    async def test_responses_tool_loop(self, tmp_path) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content or b"{}"))
            if len(bodies) == 1:
                return httpx.Response(200, json={"status": "completed", "output": [
                    {"type": "function_call", "id": "fc_1", "call_id": "call_7", "name": "web_search",
                     "arguments": '{"q": "猫"}'}], "usage": {}})
            return httpx.Response(200, json=_ok_body("responses", "查到了：猫"))

        store, models = _make(tmp_path, _cfg2("responses"), handler)
        msgs: list[dict] = [{"role": "system", "content": "规矩"}, {"role": "user", "content": "查猫"}]
        r1 = await models.chat("main", msgs, tools=TOOLS)
        assert r1.tool_calls and r1.tool_calls[0]["id"] == "call_7"
        msgs += [r1.raw_message, {"role": "tool", "tool_call_id": "call_7", "content": "猫是猫科动物"}]
        r2 = await models.chat("main", msgs, tools=TOOLS)
        assert r2.text == "查到了：猫"
        inp = bodies[1]["input"]
        fc = [i for i in inp if i["type"] == "function_call"]
        out = [i for i in inp if i["type"] == "function_call_output"]
        assert fc[0]["call_id"] == "call_7" and fc[0]["name"] == "web_search"
        assert out[0]["call_id"] == "call_7" and out[0]["output"] == "猫是猫科动物"
        assert inp.index(fc[0]) < inp.index(out[0])
        await models.close()

    @pytest.mark.asyncio
    async def test_anthropic_tool_loop(self, tmp_path) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content or b"{}"))
            if len(bodies) == 1:
                return httpx.Response(200, json={"type": "message", "stop_reason": "tool_use", "content": [
                    {"type": "text", "text": "我查一下"},
                    {"type": "tool_use", "id": "toolu_9", "name": "web_search", "input": {"q": "猫"}}],
                    "usage": {}})
            return httpx.Response(200, json=_ok_body("anthropic", "查到了：猫"))

        store, models = _make(tmp_path, _cfg2("anthropic"), handler)
        msgs: list[dict] = [{"role": "user", "content": "查猫"}]
        r1 = await models.chat("main", msgs, tools=TOOLS)
        assert r1.finish_reason == "tool_calls"
        msgs += [r1.raw_message, {"role": "tool", "tool_call_id": "toolu_9", "content": "猫是猫科动物"}]
        r2 = await models.chat("main", msgs, tools=TOOLS)
        assert r2.text == "查到了：猫"
        m = bodies[1]["messages"]
        assert [x["role"] for x in m] == ["user", "assistant", "user"]
        tu = [b for b in m[1]["content"] if b["type"] == "tool_use"][0]
        assert tu["id"] == "toolu_9" and tu["input"] == {"q": "猫"}
        tr = m[2]["content"][0]
        assert tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_9" and tr["content"] == "猫是猫科动物"
        await models.close()
