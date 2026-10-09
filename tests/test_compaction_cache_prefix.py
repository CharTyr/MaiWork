"""Models 的「正常请求前缀配方」（compaction 前缀复用）回归测试。

父 agent 定稿的契约：
- **正常**（purpose 不以 `:compact` 结尾）调用**成功后**，按 (agentkind, groupid, taskid,
  purpose) 记一份配方；
- 配方 = 那次**真正发出去**的协议体（含剥过内部键的 messages）+ 实际候选/有效强度/max_tokens/
  mct/协议，**只在内存**、不输出正文、不落库；绝不读 `model_calls` 那份被遮被截的日志；
- 内存有界：最多 16 个 scope、总量 8MB、单个 body 超 512KB 就不存；
- 复用前用同一候选、同一协议重建 body，逐项校验 model / tools / system / 其它参数全等；
  messages 可以**尾部增长**，但整份结构里**最多一处增长**（末条消息 content 可作纯追加；
  Anthropic 并块同样只许纯追加）；中间任何一处变了 → None；
- 追加 `instruction` 当最后一条 user 之后，原来那份前缀必须原样保留；
- 不可复用 / 超预算 / 熔断 / 400 适配 / ModelError 一律 **None**：不发请求、不偷偷换模型、
  retries=0（不重试）；
- 本次**不发** `cache_control`（当前所有适配器都没有显式 Anthropic 缓存设施，加了就是
  给现有请求体测试埋雷；"前缀逐字一致"是为了将来端点自动缓存 / 配置化缓存能吃到）。

全部走 httpx.MockTransport（openai 用 tests/test_models.py 的 FakeEndpoint，另两种协议用本地
handler），不发真网络、不花真钱。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import ChatResult, Models
from CharTyr_MaiWork.maiwork.store import Store
from tests.test_models import SECRET, FakeEndpoint, _FakeAgents, _make_new, _new_cfg

BIG_WINDOW_CFG = dict(primary_window=1_000_000, primary_max=8192)


def _models_with_transport(tmp_path, cfg, transport, *, agents=None, name="t"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    store = Store(d / "m.db")
    store.migrate()
    holder = {"settings": load_settings(cfg)[0]}
    return Models(
        store, lambda: holder["settings"], transport=transport,
        agents=agents if agents is not None else _FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}}),
    )


def _cfg(*, protocol="openai", window=128000, max_tokens=32768, efforts=(), backup=False, agent_model="m1"):
    cfg = _new_cfg()
    entries = [{
        "id": "m1", "endpoint": "default", "model": "m-main", "name": "主模型展示",
        "context_window": window, "max_tokens": max_tokens, "efforts": list(efforts),
    }]
    if backup:
        entries.append({
            "id": "m2", "endpoint": "default", "model": "m-bak",
            "context_window": window, "max_tokens": max_tokens, "efforts": list(efforts),
        })
    cfg["model_list"] = entries
    cfg["endpoints"] = [{
        "id": "default", "name": "端点", "protocol": protocol,
        "base_url": "https://a.test/v1", "api_key": SECRET,
    }]
    return cfg


def _scope(agent, group_id, task_id, purpose):
    """测试里用来在配方表里找某一份配方的身份（断言按身份字段对，不耦合键的分隔符）。"""
    return {"agent": agent, "group_id": group_id, "task_id": task_id, "purpose": purpose}


def _recipe(models, **identity):
    for entry in models.prefix_recipe_info()["scopes"].values():
        if all(entry.get(k) == v for k, v in identity.items()):
            return entry
    return None


def _codes(models):
    """这一份配方里的前缀字符数（用来断言配方有没有被覆盖）。"""
    return {
        (e["agent"], e["group_id"], e["task_id"], e["purpose"]): e["prefix_chars"]
        for e in models.prefix_recipe_info()["scopes"].values()
    }


NORMAL = dict(agent="task", group_id="g1", task_id="T-1", purpose="worker")


def _msgs(*, big=0, extra=None):
    msgs = [
        {"role": "system", "content": "你是 MaiWork 的子 agent" + ("系" * big)},
        {"role": "user", "content": "把上周的活儿汇总一下" + ("x" * big), "maiwork_pinned": True},
    ]
    msgs.extend(extra or [])
    return msgs


def _plain(protocol: str, text: str, *, tool_calls=None) -> httpx.Response:
    if protocol == "anthropic":
        message: dict = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
        if tool_calls:
            message = {"content": [{"type": "tool_use", "id": "c1", "name": "read_file",
                                    "input": {"path": "a.txt"}}], "stop_reason": "tool_use"}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
            **message, "usage": {"input_tokens": 10, "output_tokens": 3},
        })
    if protocol == "responses":
        out = [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
        if tool_calls:
            out = [{"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"}]
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "model": "resp-model", "status": "completed",
            "output": out, "usage": {"input_tokens": 10, "output_tokens": 3},
        })
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]}
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "m-main",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    })


def _wire(protocol: str, *, texts=(), fail_first_500: bool = False, tool_calls=False):
    """本地 MockTransport：记每次请求的 body，按顺序回协议形状的响应。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append({"url": str(request.url), "body": body})
        idx = len(calls) - 1
        if fail_first_500 and idx == 0:
            return httpx.Response(500, json={"error": {"message": "服务端错误"}})
        text = texts[min(idx, len(texts) - 1)] if texts else "好的"
        return _plain(protocol, text, tool_calls=tool_calls)

    return httpx.MockTransport(handler), calls


# ---------------------------------------------------------------------------
# 1. 正常成功 → 记配方 → 复用同一前缀
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_success_records_recipe_and_reuse_sends_same_prefix(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}, {"kind": "ok", "content": "接住了"}]})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg())
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)

    info = models.prefix_recipe_info()
    entry = _recipe(models, model="m-main", protocol="openai", purpose="worker")
    assert entry is not None, f"正常成功要记一份配方：{info}"
    assert entry["entry_id"] == "m1" and entry["mct"] is False and entry["json_mode"] is False
    assert entry["max_tokens"] == 32768 and entry["effort"] == ""
    assert entry["group_id"] == "g1" and entry["task_id"] == "T-1" and entry["agent"] == "task"

    result = await models.chat_compaction_prefix(
        "worker", msgs, instruction="接着干，别改需求", **NORMAL,
    )
    assert isinstance(result, ChatResult) and result.text == "接住了"
    assert len(ep.calls) == 2, "复用必须真发一次"
    stored_msgs = ep.calls[0]["body"]["messages"]
    reuse_msgs = ep.calls[1]["body"]["messages"]
    assert reuse_msgs[: len(stored_msgs)] == stored_msgs, "复用时原前缀要逐字保留"
    assert reuse_msgs[len(stored_msgs):] == [{"role": "user", "content": "接着干，别改需求"}]
    assert "cache_control" not in json.dumps(ep.calls[1]["body"], ensure_ascii=False), (
        "本次不发 cache_control（当前没有显式 Anthropic 缓存设施）"
    )
    assert "maiwork_" not in json.dumps(reuse_msgs, ensure_ascii=False)
    await models.close()


@pytest.mark.asyncio
async def test_large_payload_is_recorded_untruncated(tmp_path) -> None:
    """记的是**发出去的那份** body，不是 model_calls 里被遮被截的日志。"""
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}, {"kind": "ok"}]})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg(window=200000))
    msgs = _msgs(big=6000)                      # 每条 > 4000 字（日志那边会被截）
    await models.chat("worker", msgs, **NORMAL)
    sent = ep.calls[0]["body"]["messages"]
    assert len(sent[0]["content"]) == len("你是 MaiWork 的子 agent") + 6000
    assert len(sent[1]["content"]) == len("把上周的活儿汇总一下") + 6000
    assert all(not k.startswith("maiwork_") for m in sent for k in m)
    entry = _recipe(models, purpose="worker")
    assert entry["prefix_chars"] > 12000, "配方里的前缀长度要覆盖全量正文"
    assert await models.chat_compaction_prefix("worker", msgs, instruction="继续", **NORMAL)
    await models.close()


# ---------------------------------------------------------------------------
# 2. 形状变了就不能复用（None 且不发请求）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_middle_system_tools_or_purpose_is_none(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg())
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    msgs = _msgs(extra=[{"role": "assistant", "content": "中间那条"}, {"role": "user", "content": "再问一句"}])
    await models.chat("worker", msgs, tools=tools, **NORMAL)
    sent = ep.calls[0]
    assert len(ep.calls) == 1

    middle_changed = [
        dict(msgs[0]), dict(msgs[1]), {"role": "assistant", "content": "中间那条改过了"}, dict(msgs[3]),
    ]
    system_changed = [dict(msgs[0], content="换了系统提示"), *[dict(m) for m in msgs[1:]]]
    tools_changed = [{"type": "function", "function": {"name": "别的工具", "parameters": {"type": "object"}}}]

    assert await models.chat_compaction_prefix(
        "worker", middle_changed, instruction="继续", tools=tools, **NORMAL) is None, "中段变了 → None"
    assert await models.chat_compaction_prefix(
        "worker", system_changed, instruction="继续", tools=tools, **NORMAL) is None, "system 变了 → None"
    assert await models.chat_compaction_prefix(
        "worker", msgs, instruction="继续", tools=tools_changed, **NORMAL) is None, "工具表变了 → None"
    assert await models.chat_compaction_prefix(
        "worker", msgs, instruction="继续", tools=tools, agent="task", group_id="g1", task_id="T-1",
        purpose="news") is None, "purpose 换了 → 另一份 scope，没配方 → None"
    assert await models.chat_compaction_prefix(
        "worker", msgs, instruction="继续", tools=tools, agent="news", **{k: v for k, v in NORMAL.items() if k != "agent"}
    ) is None, "agentkind 换了 → None"
    assert await models.chat_compaction_prefix(
        "worker", msgs, instruction="继续", tools=tools, escalate=True, **NORMAL) is None, "escalate 变了 → None"
    assert len(ep.calls) == 1, "不可复用的判定不许发请求"
    assert sent["body"]["messages"] == ep.calls[0]["body"]["messages"]

    # 契约里「工作视图可以 tail 增长」：尾部多一条消息是允许的（这里补一条 user）
    tail_grown = [*[dict(m) for m in msgs], {"role": "user", "content": "又补了一个问题"}]
    assert isinstance(
        await models.chat_compaction_prefix("worker", tail_grown, instruction="继续", tools=tools, **NORMAL),
        ChatResult,
    ), "tail 增长要允许"
    assert len(ep.calls) == 2
    await models.close()


@pytest.mark.asyncio
async def test_tail_growth_and_last_message_extension_are_allowed(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}] * 3})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg())
    msgs = _msgs(extra=[{"role": "assistant", "content": "上一轮结论"}])
    await models.chat("worker", msgs, **NORMAL)
    stored = ep.calls[0]["body"]["messages"]

    grown = [*[dict(m) for m in msgs], {"role": "tool", "name": "read_file", "content": "工具结果"}]
    r1 = await models.chat_compaction_prefix("worker", grown, instruction="继续", **NORMAL)
    assert r1 is not None, "尾部新增消息要允许"
    assert ep.calls[1]["body"]["messages"][: len(stored)] == stored

    last_extended = [*[dict(m) for m in msgs[:-1]],
                     {"role": "assistant", "content": "上一轮结论" + "又补了一段"}]
    r2 = await models.chat_compaction_prefix("worker", last_extended, instruction="继续", **NORMAL)
    assert r2 is not None, "末条消息 content 作纯追加要允许"
    await models.close()


# ---------------------------------------------------------------------------
# 3. 两种协议的并块/顶层 system
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_merged_user_pure_append_and_top_system_kept(tmp_path) -> None:
    transport, calls = _wire("anthropic", texts=["好", "接着干"])
    models = _models_with_transport(tmp_path, _cfg(protocol="anthropic", window=200000), transport)
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)
    first = calls[0]["body"]
    assert "system" in first and isinstance(first["system"], str), "anthropic 的 system 在顶层"
    assert first["messages"] == [{"role": "user", "content": [{"type": "text", "text": msgs[1]["content"]}]}]

    result = await models.chat_compaction_prefix("worker", msgs, instruction="接着干", **NORMAL)
    assert isinstance(result, ChatResult) and len(calls) == 2
    second = calls[1]["body"]
    assert second["system"] == first["system"], "顶层 system 要一模一样"
    blocks = second["messages"][-1]["content"]
    assert blocks[:-1] == first["messages"][-1]["content"], "原块要原样保留"
    assert blocks[-1] == {"type": "text", "text": "接着干"}, "instruction 只许纯追加成新块"
    assert len(second["messages"]) == len(first["messages"]), "并块不新增消息"
    assert "cache_control" not in json.dumps(second, ensure_ascii=False)
    await models.close()


@pytest.mark.asyncio
async def test_responses_protocol_reuse_keeps_instructions_and_input_prefix(tmp_path) -> None:
    transport, calls = _wire("responses", texts=["好", "接着干"])
    models = _models_with_transport(tmp_path, _cfg(protocol="responses", window=200000), transport)
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)
    first = calls[0]["body"]
    result = await models.chat_compaction_prefix("worker", msgs, instruction="接着干", **NORMAL)
    assert isinstance(result, ChatResult) and len(calls) == 2
    second = calls[1]["body"]
    assert second.get("instructions") == first.get("instructions")
    assert second["input"][: len(first["input"])] == first["input"]
    assert second["model"] == first["model"] and second["max_output_tokens"] == first["max_output_tokens"]
    await models.close()


# ---------------------------------------------------------------------------
# 4. 实际候选 / 强度 / mct / json_mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_records_the_model_that_actually_answered(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "status", "status": 500}], "m-bak": [{"kind": "ok"}] * 2})
    agents = _FakeAgents({"main": {"model": "m1", "backup": "m2"}, "task": {"model": "m1", "backup": "m2"}})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg(backup=True), agents=agents)
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)
    assert _recipe(models, model="m-bak") is not None, "记的必须是真回答的那个候选"
    assert _recipe(models, model="m-main") is None
    result = await models.chat_compaction_prefix("worker", msgs, instruction="接着干", **NORMAL)
    assert isinstance(result, ChatResult)
    assert ep.calls[-1]["model"] == "m-bak", "复用要钉住实际那个候选，不许换"
    await models.close()


@pytest.mark.asyncio
async def test_effort_and_mct_changes_invalidate(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}] * 3})
    agents = _FakeAgents({"main": {"model": "m1", "effort": "high"}, "task": {"model": "m1", "effort": "high"}})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg(efforts=("high",)), agents=agents)
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)
    entry = _recipe(models, purpose="worker")
    assert entry["effort"] == "high" and ep.calls[0]["body"].get("reasoning_effort") == "high"
    assert isinstance(await models.chat_compaction_prefix("worker", msgs, instruction="继续", **NORMAL), ChatResult)

    # 岗位强度改了 → 配方作废（不许拿旧前缀配新强度）
    agents._m["task"]["effort"] = "low"
    assert await models.chat_compaction_prefix("worker", msgs, instruction="继续", **NORMAL) is None

    # 学到的 mct（400 点名换字段名）同样让旧配方作废
    agents._m["task"]["effort"] = "high"
    key = next(iter(models.prefix_recipe_info()["scopes"].values()))["cand_key"]
    models._adapt[key] = {"mct": True}
    assert await models.chat_compaction_prefix("worker", msgs, instruction="继续", **NORMAL) is None
    await models.close()


@pytest.mark.asyncio
async def test_json_mode_is_part_of_the_recipe(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}] * 2})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg())
    msgs = _msgs()
    await models.chat("worker", msgs, json_mode=True, **NORMAL)
    assert _recipe(models, json_mode=True) is not None
    assert await models.chat_compaction_prefix("worker", msgs, instruction="继续", json_mode=False, **NORMAL) is None
    assert isinstance(
        await models.chat_compaction_prefix("worker", msgs, instruction="继续", json_mode=True, **NORMAL), ChatResult
    )
    await models.close()


# ---------------------------------------------------------------------------
# 5. 预算 / 上限 / 覆盖规则
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversize_tail_is_none_without_network(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg(window=64000, max_tokens=8192))
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)
    assert len(ep.calls) == 1
    huge = [*[dict(m) for m in msgs], {"role": "user", "content": "y" * 400000}]
    assert await models.chat_compaction_prefix("worker", huge, instruction="继续", **NORMAL) is None
    assert len(ep.calls) == 1, "整包超预算要在派发前就返回 None"
    await models.close()


@pytest.mark.asyncio
async def test_scope_cap_and_single_body_cap(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}] * 40})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg(window=1_000_000))
    for i in range(17):
        await models.chat("worker", _msgs(), agent="task", group_id=f"g{i}", task_id="T", purpose="worker")
    info = models.prefix_recipe_info()
    assert len(info["scopes"]) == 16, "最多 16 个 scope"
    assert info["total_bytes"] <= 8 * 1024 * 1024
    assert _recipe(models, group_id="g0") is None, "最旧的要被淘汰"
    assert _recipe(models, group_id="g16") is not None

    # 单个 body 超过 512KB：不存（宁可没有配方，也不留一份巨型快照）
    before = len(models.prefix_recipe_info()["scopes"])
    await models.chat("worker", [{"role": "user", "content": "z" * 600_000}],
                      agent="task", group_id="huge", task_id="T", purpose="worker")
    assert _recipe(models, group_id="huge") is None, "单 body 超上限不存"
    assert len(models.prefix_recipe_info()["scopes"]) == before
    await models.close()


@pytest.mark.asyncio
async def test_compact_purpose_never_overwrites_normal_recipe(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}] * 3})
    _store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg())
    msgs = _msgs()
    await models.chat("worker", msgs, **NORMAL)
    before = _codes(models)
    assert before[("task", "g1", "T-1", "worker")] > 0

    compact_msgs = [*[dict(m) for m in msgs], {"role": "user", "content": "这次是压缩调用"}]
    await models.chat("worker", compact_msgs, agent="task", group_id="g1", task_id="T-1",
                      purpose="worker:compact")
    assert _codes(models) == before, ":compact 不许覆盖 normal 配方"
    assert _recipe(models, purpose="worker:compact") is None, ":compact 调用不记配方"
    assert isinstance(await models.chat_compaction_prefix("worker", msgs, instruction="继续", **NORMAL), ChatResult)
    await models.close()


@pytest.mark.asyncio
async def test_reuse_returns_tool_calls_for_compaction_to_verify(tmp_path) -> None:
    transport, calls = _wire("openai", texts=["好", "去读文件"], tool_calls=True)
    models = _models_with_transport(tmp_path, _cfg(), transport)
    msgs = _msgs()
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    await models.chat("worker", msgs, tools=tools, **NORMAL)
    result = await models.chat_compaction_prefix("worker", msgs, instruction="继续", tools=tools, **NORMAL)
    assert isinstance(result, ChatResult)
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "read_file", (
        "工具调用原样交回给 compaction 验证"
    )
    assert len(calls) == 2
    await models.close()
