"""上下文预算矩阵：Models 整包请求预算 / 派发前物理硬闸 / 指纹式实测校正的集成回归。

与 owner 的 `tests/test_models_request_budget.py` 分工：那边测字段与单点行为，
这个文件只测**矩阵与形状**（同一套算术在四个窗口 / 两种输出预留 / 极端 tools 形状下）：

1. 四个真实窗口 64000 / 128000 / 256000 / 500000 的算术一致性 + 装不下就拦；
2. 同一份内容：窗口够就发出去、窗口不够就在派发前被拦（硬闸按窗口判，不是固定上限）；
3. 实际输出预留必须等于真正发出去的 `max_tokens`（32k 与条目默认两种）；
4. 中英混排 + 代码 + JSON 历史 + 超长工具 schema：整包估算要把 tools 数进去，
   做出「历史很小、工具 schema 很大」的形状——不数 tools 的硬闸会放行的那种；
5. 实测校正的指纹口径：中段等长改写 / tools schema 变了 / 预留变了都作废；
   缓存 tokens 只算「本来就占上下文」的部分（anthropic 加缓存读+写，openai 不重复加）。

全部走 httpx.MockTransport（复用 tests/test_models.py 的 FakeEndpoint / _make_new /
_new_cfg / _FakeAgents），不发真网络、不碰线上。

钩子（`Models.request_budget` / `Models.request_usage_snapshot`）还没落地时，
本文件**不在模块顶层 import 任何新符号**：失败点是调用点上的 AttributeError（明确的红），
不是 ImportError / 收集错误。
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import ModelError, Models
from CharTyr_MaiWork.maiwork.store import Store
from tests.test_models import SECRET, FakeEndpoint, _FakeAgents, _make_new, _new_cfg

WINDOWS = (64000, 128000, 256000, 500000)
RESERVE_SMALL = 8192
RESERVE_BIG = 32768


# ---------------------------------------------------------------------------
# 钩子 / 常量的懒取口（落地前是 AttributeError，不是 ImportError）
# ---------------------------------------------------------------------------


def _models_module():
    return importlib.import_module("CharTyr_MaiWork.maiwork.models")


def _const(name: str):
    return getattr(_models_module(), name)


def _hook(models_obj, name: str):
    fn = getattr(models_obj, name)
    assert callable(fn), f"{name} 必须是可调用的"
    return fn


def _budget(models_obj, kind="main", *, messages=None, tools=None, max_tokens=None, escalate=False):
    fn = _hook(models_obj, "request_budget")
    kw = {"messages": list(messages or [])}
    if tools is not None:
        kw["tools"] = tools
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    if escalate:
        kw["escalate"] = True
    return fn(kind, **kw)


def _snap(models_obj, kind="main", **kw):
    return _hook(models_obj, "request_usage_snapshot")(kind, **kw)


# ---------------------------------------------------------------------------
# 假件与矩阵形状
# ---------------------------------------------------------------------------


def _cfg_matrix(primary_window, primary_max=RESERVE_SMALL, backup_window=500000, backup_max=RESERVE_SMALL):
    cfg = _new_cfg()
    cfg["model_list"] = [
        {"id": "m1", "endpoint": "default", "model": "m-main", "name": "主模型展示",
         "context_window": primary_window, "max_tokens": primary_max},
        {"id": "m2", "endpoint": "default", "model": "m-bak",
         "context_window": backup_window, "max_tokens": backup_max},
    ]
    return cfg


def _agents(*, model="m1", backup=""):
    return _FakeAgents({"main": {"model": model, "backup": backup}, "task": {"model": model, "backup": backup}})


def _fresh(tmp_path, name: str, *, ep=None, cfg=None, agents=None):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return _make_new(d, ep, cfg=cfg, agents=agents if agents is not None else _agents())


def _one(content: str) -> list[dict]:
    return [{"role": "user", "content": content}]


def _estimate(models_obj, msgs, *, tools=None, max_tokens=None):
    return _budget(models_obj, "main", messages=msgs, tools=tools, max_tokens=max_tokens).estimated_input_tokens


def _scan_content(models_obj, usable: int, *, tools=None, max_tokens=None, hi=4_000_000):
    """二分出（最大装得下的内容长度, 最小超界的内容长度）。"""

    def over(n: int) -> bool:
        return _estimate(models_obj, _one("x" * n), tools=tools, max_tokens=max_tokens) > usable

    assert over(1) is False, "一条极短消息就超界：测试前提不成立"
    assert over(hi) is True, "扫描上限内都没超界：测试前提不成立"
    lo, high = 1, hi
    while lo + 1 < high:
        mid = (lo + high) // 2
        if over(mid):
            high = mid
        else:
            lo = mid
    return lo, high


def _tool_schema(chars: int) -> list[dict]:
    """一份「描述超长」的工具 schema（字符数可控，用于撑爆窗口）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "inspect_file",
                "description": "读文件。" + "详" * chars,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "路径。" + "段" * chars}},
                    "required": ["path"],
                },
            },
        }
    ]


def _mixed_history(*, extra_zh: int = 0) -> list[dict]:
    """中英混排 + 代码 + JSON 的历史（tools 另加）。"""
    return [
        {"role": "system", "content": "你是 MaiWork 的主模型；只输出 JSON，不要解释。"},
        {"role": "user", "content": "群里的原话：这个表要按周汇总，别丢字段。" + "好" * extra_zh},
        {"role": "assistant", "content": "```python\nfor row in rows:\n    total += row['amount']\n```"},
        {"role": "tool", "tool_call_id": "call_1",
         "content": json.dumps({"ok": True, "rows": [{"id": i, "amount": i * 3} for i in range(50)]},
                               ensure_ascii=False)},
    ]


def _models_with(tmp_path, cfg, transport, agents=None):
    """要自定义 MockTransport（anthropic 响应形状）时自己装一个 Models。"""
    store = Store(tmp_path / "custom.db")
    store.migrate()
    holder = {"settings": load_settings(cfg)[0]}
    return Models(store, lambda: holder["settings"], transport=transport,
                  agents=agents if agents is not None else _agents(model="m1"))


# ---------------------------------------------------------------------------
# 0. 钩子存在性
# ---------------------------------------------------------------------------


def test_budget_hooks_are_present(tmp_path) -> None:
    """两个新钩子必须挂在 Models 上（缺了就是 AttributeError 的红）。"""
    _store, _holder, models = _make_new(tmp_path)
    assert callable(getattr(models, "request_budget"))
    assert callable(getattr(models, "request_usage_snapshot"))


# ---------------------------------------------------------------------------
# 1. 窗口矩阵：算术一致 + 刚好装得下 / 过线就拦
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("window", WINDOWS)
async def test_arithmetic_and_fit_boundary(tmp_path, window) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(tmp_path, "w", ep=ep, cfg=_cfg_matrix(window))
    margin = _const("INPUT_SAFETY_MARGIN")
    b = _budget(models, "main", messages=_one("x" * 130))
    assert b.context_window == window
    assert b.entry_id == "m1" and b.model == "m-main" and b.limit_source == "candidate"
    assert b.max_output_tokens == RESERVE_SMALL == b.output_reserve
    assert b.usable_input_tokens == window - RESERVE_SMALL - margin
    assert b.estimated_input_tokens == (
        b.estimated_message_tokens + b.estimated_tool_tokens + b.overhead_tokens
    ), "整包估算就是 消息 + tools + 固定开销"
    assert b.estimated_tool_tokens == 0 and b.overhead_tokens > 0
    assert b.fits is True and b.shortfall_tokens == 0
    assert b.calibrate_factor == 1.0 and b.usage_source == "estimated"
    assert b.trigger_threshold <= b.usable_input_tokens, "压缩触发线不许越过物理硬闸"
    if window >= 128000:
        assert b.trigger_threshold > 0
    fit_len, over_len = _scan_content(models, b.usable_input_tokens)
    ok = _budget(models, "main", messages=_one("x" * fit_len))
    assert ok.fits is True and ok.shortfall_tokens == 0
    bad = _budget(models, "main", messages=_one("x" * over_len))
    assert bad.fits is False
    assert bad.shortfall_tokens == bad.estimated_input_tokens - bad.usable_input_tokens > 0
    await models.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("window", WINDOWS)
async def test_overfit_raises_before_network(tmp_path, window) -> None:
    """窗口装不下的请求：派发前就报错，端点一次都不许被碰。"""
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(
        tmp_path, "w", ep=ep, cfg=_cfg_matrix(window, backup_window=window), agents=_agents(backup="m2")
    )
    usable = _budget(models, "main", messages=_one("hi")).usable_input_tokens
    _fit_len, over_len = _scan_content(models, usable)
    with pytest.raises(ModelError) as ei:
        await models.chat("main", _one("x" * over_len))
    assert ep.calls == [], "超限请求不许发出去（备用也不许，它同样装不下）"
    msg = str(getattr(ei.value, "message", ei.value))
    assert "上下文" in msg or "放不下" in msg
    await models.close()


@pytest.mark.asyncio
async def test_same_request_blocked_only_by_the_smaller_window(tmp_path) -> None:
    """同一份内容：256000 的窗口发得出去，64000 的窗口派发前被拦——硬闸按窗口判。"""
    ep_small = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    ep_big = FakeEndpoint({"m-main": [{"kind": "ok", "content": "大窗口接住了"}]})
    _s1, _h1, small = _fresh(
        tmp_path, "small", ep=ep_small, cfg=_cfg_matrix(64000, backup_window=64000), agents=_agents(backup="m2")
    )
    _s2, _h2, big = _fresh(
        tmp_path, "big", ep=ep_big, cfg=_cfg_matrix(256000, backup_window=64000), agents=_agents(backup="m2")
    )
    usable_big = _budget(big, "main", messages=_one("hi")).usable_input_tokens
    fit_len, _over = _scan_content(big, usable_big)
    msgs = _one("x" * fit_len)
    assert _budget(big, "main", messages=msgs).fits is True
    assert _budget(small, "main", messages=msgs).fits is False
    with pytest.raises(ModelError):
        await small.chat("main", msgs)
    assert ep_small.calls == []
    r = await big.chat("main", msgs)
    assert r.text == "大窗口接住了"
    assert len(ep_big.calls) == 1 and ep_big.calls[0]["model"] == "m-main"
    assert len(ep_big.calls[0]["body"]["messages"][0]["content"]) == fit_len, "内容不许被悄悄截短"
    await small.close()
    await big.close()


# ---------------------------------------------------------------------------
# 2. 输出预留 = 真正发出去的 max_tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_reserve_matches_outgoing_max_tokens(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(tmp_path, "d", ep=ep, cfg=_cfg_matrix(128000, primary_max=16384))
    msgs = _mixed_history()
    b = _budget(models, "main", messages=msgs)
    assert b.max_output_tokens == b.output_reserve == 16384
    await models.chat("main", msgs)
    assert ep.calls[0]["body"]["max_tokens"] == 16384 == b.output_reserve
    await models.close()


@pytest.mark.asyncio
async def test_32768_reserve_matches_outgoing_max_tokens(tmp_path) -> None:
    """调用方显式要 32768：预算里的预留和请求体必须同一份 32768。"""
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(tmp_path, "big", ep=ep, cfg=_cfg_matrix(500000))
    msgs = _mixed_history()
    margin = _const("INPUT_SAFETY_MARGIN")
    b = _budget(models, "main", messages=msgs, max_tokens=RESERVE_BIG)
    assert b.max_output_tokens == b.output_reserve == RESERVE_BIG
    assert b.usable_input_tokens == 500000 - RESERVE_BIG - margin
    await models.chat("main", msgs, max_tokens=RESERVE_BIG)
    assert ep.calls[0]["body"]["max_tokens"] == RESERVE_BIG == b.output_reserve
    await models.close()


@pytest.mark.asyncio
async def test_reserve_change_shifts_the_line(tmp_path) -> None:
    """同一份内容：默认预留装得下、换 32768 预留就装不下——差多少要能算清。"""
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(tmp_path, "r", ep=ep, cfg=_cfg_matrix(500000))
    base = _budget(models, "main", messages=_one("hi"))
    big = _budget(models, "main", messages=_one("hi"), max_tokens=RESERVE_BIG)
    assert big.usable_input_tokens - base.usable_input_tokens == -(RESERVE_BIG - RESERVE_SMALL)
    fit_len, _over = _scan_content(models, base.usable_input_tokens)
    msgs = _one("x" * fit_len)
    assert _budget(models, "main", messages=msgs).fits is True
    assert _budget(models, "main", messages=msgs, max_tokens=RESERVE_BIG).fits is False
    with pytest.raises(ModelError):
        await models.chat("main", msgs, max_tokens=RESERVE_BIG)
    assert ep.calls == []
    r = await models.chat("main", msgs)
    assert r.text and len(ep.calls) == 1
    assert ep.calls[0]["body"]["max_tokens"] == RESERVE_SMALL
    await models.close()


# ---------------------------------------------------------------------------
# 3. 工具 schema 压过历史 / 混排历史记账
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_schema_dominates_while_history_fits(tmp_path) -> None:
    """历史很小、工具 schema 很大：不把 tools 数进去的硬闸会放行，这里必须拦住。"""
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(tmp_path, "t", ep=ep, cfg=_cfg_matrix(64000))
    history = _one("把上周的活儿汇总一下。" + "好" * 200)
    base = _budget(models, "main", messages=history)
    assert base.fits is True and base.estimated_tool_tokens == 0
    over = None
    prev_tool_tokens = 0
    for chars in (5_000, 10_000, 20_000, 40_000, 80_000, 160_000):
        b = _budget(models, "main", messages=history, tools=_tool_schema(chars))
        assert b.estimated_tool_tokens > prev_tool_tokens, "工具 schema 变大，整包估算必须跟着变大"
        prev_tool_tokens = b.estimated_tool_tokens
        if not b.fits:
            over = b
            break
    assert over is not None, "工具 schema 撑到 16 万字符还装得下：硬闸没数 tools"
    assert over.shortfall_tokens > 0
    assert over.estimated_tool_tokens > over.usable_input_tokens
    assert over.estimated_message_tokens * 4 < over.usable_input_tokens, "历史本身很小：装不下只能是 tools 撑的"
    # 同一份历史不带 tools 依然装得下 → 拦下它靠的就是「数了 tools」
    assert _budget(models, "main", messages=history).fits is True
    big_tools = _tools_at(models, history, over)
    with pytest.raises(ModelError):
        await models.chat("main", history, tools=big_tools)
    assert ep.calls == []
    r = await models.chat("main", history)
    assert r.text and len(ep.calls) == 1
    await models.close()


def _tools_at(models_obj, history, over_budget) -> list[dict]:
    """从「已超界」的预算反查那份 tools（重放同样的形状，保证 chat 用的和断言过的是同一份）。"""
    for chars in (40_000, 80_000, 160_000, 320_000, 640_000):
        tools = _tool_schema(chars)
        if _budget(models_obj, "main", messages=history, tools=tools).fits is False:
            return tools
    raise AssertionError("没找到能撑爆窗口的 tools 形状")


@pytest.mark.asyncio
async def test_mixed_history_and_tool_schema_accounting(tmp_path) -> None:
    """中英混排 + 代码 + JSON 的历史每条都要进估算；tools 另算一份。"""
    ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
    _store, _holder, models = _fresh(tmp_path, "m", ep=ep, cfg=_cfg_matrix(128000))
    msgs = _mixed_history(extra_zh=400)
    b = _budget(models, "main", messages=msgs)
    chars = sum(len(str(m.get("content") or "")) for m in msgs)
    assert b.estimated_message_tokens >= int(chars * 0.5), "字符口径的下限：内容没被漏算"
    assert b.estimated_message_tokens <= chars + len(msgs)
    assert b.estimated_input_tokens == b.estimated_message_tokens + b.overhead_tokens
    with_tools = _budget(models, "main", messages=msgs, tools=_tool_schema(2_000))
    assert with_tools.estimated_tool_tokens > 0
    assert with_tools.estimated_input_tokens == (
        with_tools.estimated_message_tokens + with_tools.estimated_tool_tokens + with_tools.overhead_tokens
    )
    assert with_tools.estimated_input_tokens - b.estimated_input_tokens == with_tools.estimated_tool_tokens
    grown = _budget(models, "main", messages=_mixed_history(extra_zh=4_000))
    assert grown.estimated_message_tokens > b.estimated_message_tokens
    assert _budget(models, "main", messages=msgs).fits is True
    await models.close()


# ---------------------------------------------------------------------------
# 4. 实测校正的指纹口径
# ---------------------------------------------------------------------------


async def _measured(tmp_path, msgs, *, window=128000, usage=None, tools=None, max_tokens=None, name="x"):
    ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": usage or {"prompt_tokens": 2000, "completion_tokens": 3}}]})
    _store, _holder, models = _fresh(tmp_path, name, ep=ep, cfg=_cfg_matrix(window))
    await models.chat("main", msgs, tools=tools, max_tokens=max_tokens)
    return models, ep


@pytest.mark.asyncio
async def test_identical_request_keeps_measurement(tmp_path) -> None:
    msgs = _mixed_history(extra_zh=300)
    models, ep = await _measured(tmp_path, msgs)
    assert len(ep.calls) == 1
    snap = _snap(models, "main", messages=[dict(m) for m in msgs])
    assert snap is not None
    assert snap["measured_input_tokens"] == 2000 and snap["usage_source"] == "measured"
    b = _budget(models, "main", messages=[dict(m) for m in msgs])
    assert b.usage_source == "measured" and b.measured_input_tokens == 2000
    assert b.calibrate_factor == pytest.approx(snap["calibrate_factor"])
    assert b.calibrate_factor > 1.0 and b.calibrate_factor <= _const("CALIBRATION_MAX_FACTOR")
    await models.close()


@pytest.mark.asyncio
async def test_same_length_middle_edit_invalidates_measurement(tmp_path) -> None:
    """首尾一模一样、中段等长替换：长度没变，也算另一个请求（指纹必须覆盖全量 messages）。"""
    head = {"role": "system", "content": "S" * 40}
    tail = {"role": "user", "content": "收尾"}
    before = [head, {"role": "user", "content": "x" * 600 + "AAA" + "y" * 600}, tail]
    after = [head, {"role": "user", "content": "x" * 600 + "BBB" + "y" * 600}, tail]
    models, _ep = await _measured(tmp_path, before)
    same = _snap(models, "main", messages=[dict(m) for m in before])
    assert same is not None and same["measured_input_tokens"] == 2000
    assert _estimate(models, after) == _estimate(models, before), "中段等长替换：估算也不该变"
    assert len(str(after[0])) == len(str(before[0])) and len(str(after[2])) == len(str(before[2]))
    snap = _snap(models, "main", messages=[dict(m) for m in after])
    assert snap is not None, "同一模型有过实测 → 快照要能给出「这次不作数」的结论"
    assert snap["measured_input_tokens"] == 0 and snap["usage_source"] == "estimated"
    await models.close()


@pytest.mark.asyncio
async def test_tool_schema_change_invalidates_measurement(tmp_path) -> None:
    """tools schema 变了 = 另一份请求（实测的输入量不再适用）。"""
    msgs = _one("x" * 1000)
    tools_a = _tool_schema(200)
    models, _ep = await _measured(tmp_path, msgs, tools=tools_a)
    same = _snap(models, "main", messages=[dict(m) for m in msgs], tools=json.loads(json.dumps(tools_a)))
    assert same is not None and same["measured_input_tokens"] == 2000, "值相同（非同一对象）也要认成同一份请求"
    snap = _snap(models, "main", messages=[dict(m) for m in msgs], tools=_tool_schema(4000))
    assert snap is not None
    assert snap["measured_input_tokens"] == 0 and snap["usage_source"] == "estimated"
    await models.close()


@pytest.mark.asyncio
async def test_explicit_output_reserve_change_invalidates_measurement(tmp_path) -> None:
    """显式预留变了 = 另一份请求；同一份请求仍然认得出。"""
    msgs = _one("x" * 1000)
    models, ep = await _measured(tmp_path, msgs, max_tokens=16384)
    assert ep.calls[0]["body"]["max_tokens"] == 16384
    same = _snap(models, "main", messages=[dict(m) for m in msgs], max_tokens=16384)
    assert same is not None and same["measured_input_tokens"] == 2000
    snap = _snap(models, "main", messages=[dict(m) for m in msgs], max_tokens=RESERVE_SMALL)
    assert snap is not None
    assert snap["measured_input_tokens"] == 0 and snap["usage_source"] == "estimated"
    assert _budget(models, "main", messages=msgs, max_tokens=16384).output_reserve == 16384
    await models.close()


@pytest.mark.asyncio
async def test_anthropic_cached_tokens_are_counted_inclusive(tmp_path) -> None:
    """anthropic：input_tokens 不含缓存 → 缓存读 + 缓存写都要算进实测输入。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": "好"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 3,
                      "cache_read_input_tokens": 1500, "cache_creation_input_tokens": 300},
        })

    cfg = {
        "endpoints": [{"id": "a1", "name": "anthropic 端点", "protocol": "anthropic",
                       "base_url": "https://a.test/v1", "api_key": SECRET}],
        "model_list": [{"id": "m1", "endpoint": "a1", "model": "claude-x",
                        "context_window": 200000, "max_tokens": RESERVE_SMALL}],
    }
    models = _models_with(tmp_path, cfg, httpx.MockTransport(handler))
    msgs = _one("x" * 1000)
    await models.chat("main", msgs)
    assert seen and seen[0]["max_tokens"] == RESERVE_SMALL
    snap = _snap(models, "main", messages=msgs)
    assert snap is not None
    assert snap["measured_input_tokens"] == 2300, "500 input + 1500 缓存读 + 300 缓存写"
    assert 1.0 < snap["calibrate_factor"] <= _const("CALIBRATION_MAX_FACTOR")
    await models.close()


@pytest.mark.asyncio
async def test_openai_prompt_tokens_not_double_counted(tmp_path) -> None:
    """openai：prompt_tokens 本来就含缓存命中 → 不许再加一遍 cached_tokens。"""
    usage = {"prompt_tokens": 2000, "completion_tokens": 3,
             "prompt_tokens_details": {"cached_tokens": 1500}}
    ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": usage}]})
    _store, _holder, models = _fresh(tmp_path, "o", ep=ep, cfg=_cfg_matrix(128000))
    msgs = _one("x" * 1000)
    await models.chat("main", msgs)
    snap = _snap(models, "main", messages=msgs)
    assert snap is not None
    assert snap["measured_input_tokens"] == 2000, "含缓存的口径不能再加 cached_tokens"
    assert snap["calibrate_factor"] > 1.0
    await models.close()


# ---------------------------------------------------------------------------
# 5. 换更大的备用把活接过去
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_larger_backup_serves_what_primary_cannot_hold(tmp_path) -> None:
    """首选窗口装不下：不截内容、不改调用方那份，换更大的备用发出去。"""
    ep = FakeEndpoint({"m-bak": [{"kind": "ok", "content": "备用接住了"}]})
    _store, _holder, models = _fresh(
        tmp_path, "b", ep=ep, cfg=_cfg_matrix(64000, backup_window=256000),
        agents=_agents(backup="m2"),
    )
    _s2, _h2, big = _fresh(tmp_path, "sizer", ep=FakeEndpoint({"m-main": [{"kind": "ok"}]}),
                           cfg=_cfg_matrix(256000))
    usable_big = _budget(big, "main", messages=_one("hi")).usable_input_tokens
    fit_len, _over = _scan_content(big, usable_big)
    msgs = _one("x" * fit_len)
    assert _budget(models, "main", messages=msgs).fits is False
    r = await models.chat("main", msgs)
    assert r.text == "备用接住了"
    assert [c["model"] for c in ep.calls] == ["m-bak"], "首选装不下就在派发前换，不留一次白发的请求"
    sent = ep.calls[0]["body"]
    assert sent["max_tokens"] == RESERVE_SMALL
    assert sent["messages"][0]["content"] == msgs[0]["content"], "换备用不许改内容"
    await models.close()
    await big.close()
