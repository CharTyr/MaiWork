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
