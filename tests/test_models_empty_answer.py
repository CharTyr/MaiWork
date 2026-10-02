"""线上巡检 2026-10-02：HTTP 200 但回答是空的（没文字、没工具调用）不能记成成功。

线上实例：feeds.score 拿到 {"text":"","tool_calls":[]}，客户端记 ok=1，
调用方 JSON 解析失败，已核验的八个候选整轮丢掉。

规矩：空回答 = 这次尝试失败（记 ok=0、写明「回答为空」，用量照记——token 真花了），
按同一套重试 / 换备用走；全空才抛 ModelError。只有工具调用没文字的回答仍是正常回答。

全部用 httpx.MockTransport 假端点，不碰真网络。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.models import ModelError
from test_models_protocols import _FakeAgents, _make, _usage_rows
from test_models_review_fixes import _AGENTS_WITH_BACKUP, _cfg2, _ok_body

_NO_BACKUP = {"main": {"model": "m1"}, "task": {"model": "m1"}}


def _empty_body(proto: str, text: str = "") -> dict:
    if proto == "openai":
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": ""}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 0}}
    if proto == "responses":
        return {"status": "completed", "error": None, "output": [],
                "usage": {"input_tokens": 7, "output_tokens": 0}}
    return {"type": "message", "content": [], "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 0}}


@pytest.mark.asyncio
@pytest.mark.parametrize("proto", ["openai", "responses", "anthropic"])
async def test_empty_answer_falls_back_to_backup(tmp_path, proto) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content or b"{}").get("model")
        seen.append(model)
        if model == "svc-a":
            return httpx.Response(200, json=_empty_body(proto))
        return httpx.Response(200, json=_ok_body(proto, "备用回了"))

    store, models = _make(tmp_path, _cfg2(proto), handler, _FakeAgents(_AGENTS_WITH_BACKUP))
    r = await models.chat("main", [{"role": "user", "content": "x"}])
    assert r.text == "备用回了"
    assert seen == ["svc-a", "svc-b"]
    rows = _usage_rows(store)
    assert rows[0]["ok"] == 0
    assert "回答为空" in rows[0]["error"]
    assert rows[-1]["ok"] == 1
    await models.close()


@pytest.mark.asyncio
async def test_empty_answer_retries_same_model(tmp_path) -> None:
    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(200, json=_empty_body("openai"))
        return httpx.Response(200, json=_ok_body("openai", "第二次有了"))

    store, models = _make(tmp_path, _cfg2("openai", retries=1), handler, _FakeAgents(_NO_BACKUP))
    r = await models.chat("main", [{"role": "user", "content": "x"}])
    assert r.text == "第二次有了"
    assert n["i"] == 2
    await models.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   \n  "])
async def test_all_empty_raises(tmp_path, text) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_empty_body("openai", text))

    store, models = _make(tmp_path, _cfg2("openai"), handler, _FakeAgents(_NO_BACKUP))
    with pytest.raises(ModelError, match="回答为空"):
        await models.chat("main", [{"role": "user", "content": "x"}])
    rows = _usage_rows(store)
    assert rows and all(r["ok"] == 0 for r in rows)
    # 空回答也花了输入 token：照记，不当没花
    assert rows[0]["prompt_tokens"] == 7
    await models.close()


@pytest.mark.asyncio
async def test_tool_call_without_text_is_still_ok(tmp_path) -> None:
    body = {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "web_search", "arguments": "{\"q\": \"x\"}"}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    store, models = _make(tmp_path, _cfg2("openai"), handler, _FakeAgents(_NO_BACKUP))
    r = await models.chat("main", [{"role": "user", "content": "x"}])
    assert r.tool_calls and r.text == ""
    assert _usage_rows(store)[0]["ok"] == 1
    await models.close()
