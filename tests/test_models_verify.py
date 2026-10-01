"""「验证所选模型」（docs/13 A03，2026-10）。

测试连接只列模型；验证选中的模型 = 一次短回答 + 一次无副作用工具往返，用条目真实的
max_tokens 发（实际干活就是这么发的）。最大输出不被接受（400 且提到 max_tokens）时，
有界地降两档试一次，成功就告诉调用方建议值；401/403 之类直接如实停下。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio


def _settings(max_tokens: int = 32768):
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "endpoints": [{"id": "default", "base_url": "https://api.test/v1", "api_key": "sk-test"}],
            "model_list": [{"id": "m1", "endpoint": "default", "model": "gpt-x", "max_tokens": max_tokens}],
        }
    )
    assert problems == []
    return settings


def _reply(content: str = "OK", tool_calls=None) -> httpx.Response:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": msg}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 1}})


def _mk(tmp_path: Path, handler, max_tokens: int = 32768):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _settings(max_tokens)
    agents = Agents(store, lambda: settings)
    return store, Models(store, lambda: settings, agents=agents, transport=httpx.MockTransport(handler))


async def test_verify_ok_chat_and_tools(tmp_path: Path) -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [{"id": "c1", "type": "function",
                                "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"hi\"}"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["chat_ok"] is True and r["tools_ok"] is True
    assert r["calls"] == len(seen) <= 3
    # 用条目真实的最大输出发
    assert all(b.get("max_tokens") == 32768 or b.get("max_completion_tokens") == 32768 for b in seen)
    # 工具往返：最后一次请求里带着工具结果
    assert any(m.get("role") == "tool" for m in seen[-1]["messages"])


async def test_verify_small_output_limit_suggests_lower(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        mt = body.get("max_tokens") or body.get("max_completion_tokens") or 0
        if mt > 8192:
            return httpx.Response(400, json={"error": {"message": "max_tokens must be <= 8192"}})
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [{"id": "c1", "type": "function",
                                "function": {"name": "maiwork_ping", "arguments": "{}"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["chat_ok"] is True
    assert r["suggested_max_tokens"] == 8192
    assert "8192" in r["note"]


async def test_verify_auth_error_stops(tmp_path: Path) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is False and r["chat_ok"] is False
    assert "401" in r["error"]
    assert len(calls) == 1  # 不重试、不降级乱试


async def test_verify_no_tool_support_is_warning(tmp_path: Path) -> None:
    store, models = _mk(tmp_path, lambda req: _reply("我不会用工具"))
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["chat_ok"] is True and r["tools_ok"] is False
    assert r["ok"] is True  # 能回答就能用；工具不行是警告
    assert "工具" in r["note"]


async def test_verify_unknown_entry(tmp_path: Path) -> None:
    store, models = _mk(tmp_path, lambda req: _reply())
    try:
        r = await models.verify_entry("nope")
    finally:
        await models.close(); store.close()
    assert r["ok"] is False and "没有" in r["error"]


async def test_verify_records_usage_with_purpose(tmp_path: Path) -> None:
    store, models = _mk(tmp_path, lambda req: _reply())
    try:
        await models.verify_entry("m1")
        rows = store.read().execute("SELECT purpose FROM usage").fetchall()
    finally:
        await models.close(); store.close()
    assert rows and all(str(r[0]).startswith("verify") for r in rows)


async def test_verify_empty_reply_is_not_ok(tmp_path: Path) -> None:
    """2026-10 真实端点实测：中转站某些「格式 × 模型」组合回 200，但 content 为空、output_tokens=0。
    这不算能回答——要如实判失败，不能给绿勾。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}}],
                                         "usage": {"prompt_tokens": 16, "completion_tokens": 0}})

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is False and r["chat_ok"] is False
    assert "空" in r["error"]
    assert r["calls"] == 1


# ----------------------------------------------------------------------
# docs/14 R04（2026-10）：工具验证不许假通过
# ----------------------------------------------------------------------


async def test_wrong_tool_name_is_not_tools_ok(tmp_path: Path) -> None:
    """调了根本没提供的工具 → 不算工具通过（普通回答能力照给）。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [{"id": "c1", "type": "function",
                                "function": {"name": "nonexistent_review_tool", "arguments": "{\"word\":\"hi\"}"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["chat_ok"] is True and r["tools_ok"] is False
    assert "maiwork_ping" in r["note"]


async def test_bad_arguments_json_is_not_tools_ok(tmp_path: Path) -> None:
    """工具参数不是合法 JSON → 不算工具通过，不能把破参数递回去装成功。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [{"id": "c1", "type": "function",
                                "function": {"name": "maiwork_ping", "arguments": "{oops"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["tools_ok"] is False
    assert "参数" in r["note"]
    assert r["calls"] == 2  # 没有第三次请求


async def test_word_must_be_string(tmp_path: Path) -> None:
    """参数是 JSON 对象但 word 不是字符串（或压根没有）→ 不算工具通过。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [{"id": "c1", "type": "function",
                                "function": {"name": "maiwork_ping", "arguments": "{\"word\": 42}"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["tools_ok"] is False
    assert r["calls"] == 2


async def test_empty_final_answer_after_tool_is_not_tools_ok(tmp_path: Path) -> None:
    """R04 实测例：工具结果递回去后回了空文本 200 → 不许判工具通过。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tools"):
            if not any(m.get("role") == "tool" for m in body["messages"]):
                return _reply("", [{"id": "c1", "type": "function",
                                    "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"hi\"}"}}])
            return _reply("")  # 第三次：空文本
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["tools_ok"] is False
    assert "空" in r["note"]
    assert r["calls"] == 3


async def test_tool_call_again_after_result_is_not_tools_ok(tmp_path: Path) -> None:
    """第三次还继续调工具（没有最终答复）→ 不算工具通过。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tools"):
            return _reply("", [{"id": "c2", "type": "function",
                                "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"again\"}"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["tools_ok"] is False
    assert r["calls"] == 3


async def test_multiple_mixed_tool_calls_are_not_verified(tmp_path: Path) -> None:
    """合法调用不能掩盖同批未提供的工具，不用错误结果来假装全部通过。"""
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [
                {"id": "cA", "type": "function",
                 "function": {"name": "some_other_tool", "arguments": "{}"}},
                {"id": "cB", "type": "function",
                 "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"hi\"}"}},
                {"id": "cC", "type": "function",
                 "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"ho\"}"}},
            ])
        return _reply("都收到了")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is True and r["tools_ok"] is False
    assert "工具" in r["note"]
    assert len(seen) == 2  # 混入没提供的工具，不用某个合法调用掩盖非法调用


async def test_hello_tool_calls_only_is_not_an_answer(tmp_path: Path) -> None:
    """第一次短答一个字都没有、只有 tool_calls → 不算「能正常回答」。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return _reply("", [{"id": "c1", "type": "function",
                            "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"hi\"}"}}])

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
    finally:
        await models.close(); store.close()
    assert r["ok"] is False and r["chat_ok"] is False
    assert "空" in r["error"]
    assert r["calls"] == 1


async def test_stamp_follows_settings_swap(tmp_path: Path) -> None:
    """verification_stamp 每次实时读当前配置：换成新 Settings 对象（热更新）后签名立刻跟上。"""
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    v1 = _settings()
    v2, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "endpoints": [{"id": "default", "base_url": "https://api.changed/v1", "api_key": "sk-test"}],
            "model_list": [{"id": "m1", "endpoint": "default", "model": "gpt-x", "max_tokens": 32768}],
        }
    )
    assert problems == []
    box = {"settings": v1}
    agents = Agents(store, lambda: box["settings"])
    models = Models(store, lambda: box["settings"], agents=agents,
                    transport=httpx.MockTransport(lambda req: _reply()))
    try:
        a = models.verification_stamp("m1")
        box["settings"] = v2
        assert models.verification_stamp("m1") != a
    finally:
        await models.close(); store.close()


async def test_verify_result_carries_fingerprints(tmp_path: Path) -> None:
    """结果带 requested_fingerprint / fingerprint；降档通过时两者不同，fingerprint 对应建议值。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        mt = body.get("max_tokens") or body.get("max_completion_tokens") or 0
        if mt > 8192:
            return httpx.Response(400, json={"error": {"message": "max_tokens must be <= 8192"}})
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            return _reply("", [{"id": "c1", "type": "function",
                                "function": {"name": "maiwork_ping", "arguments": "{\"word\":\"hi\"}"}}])
        return _reply("OK")

    store, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
        assert r["ok"] is True and r["suggested_max_tokens"] == 8192
        assert r["requested_fingerprint"] and r["fingerprint"]
        assert r["fingerprint"] != r["requested_fingerprint"]
        assert r["requested_fingerprint"] == models.verification_stamp("m1")
        assert r["fingerprint"] == models.verification_stamp("m1", max_tokens=8192)
    finally:
        await models.close(); store.close()


async def test_all_valid_parallel_pings_receive_results(tmp_path):
    def handler(req):
        body = json.loads(req.content)
        if body.get('tools'):
            results = [m for m in body['messages'] if m.get('role') == 'tool']
            if not results:
                return _reply('', [{'id':i,'type':'function','function':{'name':'maiwork_ping','arguments':'{"word":"hi"}'}} for i in ('p1','p2')])
            if [(m['tool_call_id'],m['content']) for m in results] != [('p1','pong'),('p2','pong')]:
                return httpx.Response(400,json={'error':{'message':'missing tool results'}})
        return _reply('OK')
    store, models = _mk(tmp_path,handler)
    try:
        result = await models.verify_entry('m1')
        assert result['ok'] is True and result['tools_ok'] is True
        assert result['calls'] == 3
    finally:
        await models.close(); store.close()
