"""生产接线回归：**真实 Models**（httpx.MockTransport）→ `compaction` 的整包预算口。

这个文件只盯一件事：生产路径上真正被调用的那几个函数，能不能原样吃到
`Models.request_budget()` 返回的**真实预算对象**（dataclass `RequestBudget`），
包括实测 usage 校正出来的系数 / 实测输入量、工具 schema 的计数、以及隐式输出预留。
假模型（只实现 `chat` / `limits_for` 的小类）测不出这条接线——`tests/test_compaction_*`
那批已经覆盖了假对象，这里的假对象只有 `httpx.MockTransport` 一层。

被盯的接线（生产调用点：coordinator.py:920 / :4892、admin_chat.py:1426、workers.py:811）：
  `compaction.context_budget(...)` / `compaction.estimate_request_input(...)`
  / `compaction.maybe_compact_ex(...)`
      → `compaction.resolve_context_budget(...)`
      → `models.request_budget(kind, messages=…, tools=…, max_tokens=…, escalate=…, json_mode=…)`

回归的坑（本次已修）：`resolve_context_budget` 只认 `isinstance(got, dict)`，
而 `Models.request_budget` 返回的是 dataclass `RequestBudget` → 对象被丢掉、回落
`models.limits_for(kind)`：`calibrate_factor` / `measured_input_tokens` / 整包估算一起
丢失（系数退回 1.0、usage_source 退回 estimated），压缩触发线按没校正的小估算算；
连 `limits_for` 也没有的老接线连输出预留都会退回默认 8192。断言一律拿
`models.request_budget(...).as_dict()` 当基准对账。

第四条（5 轮增量工作摘要）用的是同一条真实接线：`maybe_compact_ex` 自动认工作视图开头
那版摘要当上一版（不重复压一遍、老摘要被新摘要顶替），跑 5 轮看「只有一个摘要块 +
上一版摘要真的进了这次摘要输入 + 累计覆盖单调不减」。

全部走 MockTransport：不发真网络、不打真端点、不碰线上数据。
"""

from __future__ import annotations

import re

import pytest

from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.models import Models
from tests.test_models import FakeEndpoint, _FakeAgents, _make_new, _new_cfg

# 端点报的输入用量：prompt_tokens **已经含**缓存命中（20000），
# 真实占上下文的输入就是 60000——不能再把 cached 加一遍（加一遍会变成 80000）。
USAGE_INCL_CACHE = {
    "prompt_tokens": 60000,
    "completion_tokens": 5,
    "prompt_tokens_details": {"cached_tokens": 20000},
}

SUMMARY_STUB = "Primary Request and Intent：接着干\nPending Jobs：无\nNext Step：等"
_SUMMARY_IDX_RE = re.compile(r"第(\d+)版摘要")


def _one(content: str) -> list[dict]:
    return [{"role": "user", "content": content}]


def _tool_schema(chars: int) -> list[dict]:
    """一份「描述很长」的工具 schema（字符数可控）。"""
    return [{
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
    }]


def _models(tmp_path, steps, *, name="m", cfg=None, agents=None):
    """真 Models + MockTransport；cfg 默认就是「不写窗口/输出上限」的隐式形态。"""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    ep = FakeEndpoint({"m-main": steps})
    store, _holder, models = _make_new(
        d, ep, cfg=cfg if cfg is not None else _new_cfg(),
        agents=agents if agents is not None else _FakeAgents({"main": {"model": "m1"}}),
    )
    return models, ep, store


async def _measure(models, msgs, **kw):
    """先真发一次（MockTransport），把实测 usage 喂进校正。

    请求指纹覆盖 messages + tools + max_tokens：这里必须跟后面要核对的调用用**同一份**
    请求，不然实测按设计作废（那条另见 test_models_request_budget 的指纹用例）。
    """
    await models.chat("main", msgs, **kw)


# ---------------------------------------------------------------------------
# 1. 实测校正（含缓存口径）必须原样穿过 compaction 的预算口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_measured_calibration_survives_the_compaction_budget_api(tmp_path) -> None:
    models, ep, _store = _models(tmp_path, [{"kind": "ok", "usage": USAGE_INCL_CACHE}])
    msgs = _one("x" * 60000)
    tools = _tool_schema(200)
    await _measure(models, msgs, tools=tools)
    assert len(ep.calls) == 1

    b = models.request_budget("main", messages=msgs, tools=tools).as_dict()
    assert b["measured_input_tokens"] == 60000, "prompt_tokens 含缓存：不能再加 cached_tokens"
    assert b["calibrate_factor"] > 1.0 and b["usage_source"] == "measured"

    budget = compaction.context_budget(models, role="main", messages=msgs, tools=tools)
    assert budget["calibrate_factor"] == b["calibrate_factor"], "真实预算对象的校正系数被丢了"
    assert budget["measured_input_tokens"] == 60000
    assert budget["usage_source"] == "measured"
    assert budget["context_window"] == b["context_window"]
    assert budget["output_reserve"] == b["output_reserve"]

    est = compaction.estimate_request_input(models, role="main", messages=msgs, tools=tools)
    assert est["estimated_input_tokens"] == b["estimated_input_tokens"], "整包估算要跟真实预算对象一致"
    assert est["estimated_tool_tokens"] == b["estimated_tool_tokens"] > 0
    assert est["estimated_message_tokens"] == b["estimated_message_tokens"]
    assert est["calibrate_factor"] == b["calibrate_factor"]
    assert est["usage_source"] == "measured"
    await models.close()


@pytest.mark.asyncio
async def test_budget_api_matches_dataclass_field_by_field(tmp_path) -> None:
    """逐字段对账：dataclass 里压缩口会读的字段，compaction 侧一个不许少。"""
    models, _ep, _store = _models(tmp_path, [{"kind": "ok", "usage": USAGE_INCL_CACHE}])
    msgs = _one("x" * 30000)
    await _measure(models, msgs)
    b = models.request_budget("main", messages=msgs).as_dict()
    budget = compaction.context_budget(models, role="main", messages=msgs)
    for key in (
        "kind", "entry_id", "model", "context_window", "max_output_tokens", "output_reserve",
        "estimated_message_tokens", "estimated_tool_tokens", "overhead_tokens",
        "estimated_input_tokens", "usable_input_tokens", "trigger_threshold", "fits",
        "shortfall_tokens", "calibrate_factor", "measured_input_tokens", "usage_source",
        "limit_source",
    ):
        assert key in b, f"RequestBudget 少了字段 {key}"
        assert budget[key] == b[key], f"字段 {key} 没穿过 compaction 预算口"
    await models.close()


# ---------------------------------------------------------------------------
# 2. 没有 limits_for 的接线（只有 request_budget）+ 隐式输出预留 32768
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_budget_only_wiring_keeps_window_and_implicit_reserve(tmp_path, monkeypatch) -> None:
    """老接线只有 request_budget 时，窗口 / 预留 / 系数也必须来自它，不能退回默认 8192。"""
    models, _ep, _store = _models(tmp_path, [{"kind": "ok", "usage": USAGE_INCL_CACHE}])
    msgs = _one("x" * 60000)
    await _measure(models, msgs)
    b = models.request_budget("main", messages=msgs).as_dict()
    assert b["context_window"] == 128000 and b["output_reserve"] == 32768, "默认条目就是 128000/32768"
    monkeypatch.delattr(Models, "limits_for")          # 只剩 request_budget 那条路
    budget = compaction.context_budget(models, role="main", messages=msgs)
    assert budget["output_reserve"] == 32768, "隐式输出预留要来自实际条目，不是 DEFAULT_OUTPUT_RESERVE(8192)"
    assert budget["context_window"] == 128000
    assert budget["calibrate_factor"] == b["calibrate_factor"] > 1.0
    assert budget["measured_input_tokens"] == 60000
    est = compaction.estimate_request_input(models, role="main", messages=msgs)
    assert est["estimated_input_tokens"] == b["estimated_input_tokens"]
    assert est["trigger_threshold"] == compaction.compact_threshold(128000, 32768)
    await models.close()


# ---------------------------------------------------------------------------
# 3. 工具 schema 参与触发线（整包口径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_schema_counts_toward_the_trigger(tmp_path) -> None:
    models, _ep, _store = _models(tmp_path, [{"kind": "ok", "content": SUMMARY_STUB}])
    threshold = compaction.compact_threshold(128000, 32768)
    # 三条历史（最新那条被保护）→ 有东西可切；加起来还不到触发线，靠工具 schema 顶过去
    msgs = [{"role": "user", "content": "x" * 11000} for _ in range(3)]
    tools = _tool_schema(3200)
    without = compaction.estimate_request_input(models, role="main", messages=msgs)
    with_tools = compaction.estimate_request_input(models, role="main", messages=msgs, tools=tools)
    assert without["estimated_input_tokens"] < threshold, "测试前提：历史单独不到触发线"
    assert with_tools["estimated_input_tokens"] >= threshold, "测试前提：加上工具 schema 才到"
    assert with_tools["estimated_tool_tokens"] > 0 and without["estimated_tool_tokens"] == 0
    assert with_tools["output_reserve"] == 32768, "输出预留用条目值"
    assert with_tools["trigger_threshold"] == compaction.compact_threshold(with_tools["context_window"], 32768)

    quiet = await compaction.maybe_compact_ex(
        [dict(m) for m in msgs], models=models, role="main", agent="main",
    )
    assert quiet.action == "none", "不带工具 schema：还没到触发线"
    outcome = await compaction.maybe_compact_ex(
        [dict(m) for m in msgs], models=models, role="main", agent="main", tools=tools,
    )
    assert outcome.action == "summarized", f"带工具 schema 该触发压缩，实际 {outcome.action}"
    assert outcome.observations["input"]["estimated_tool_tokens"] > 0
    assert outcome.observations["input"]["threshold"] == threshold
    assert outcome.observations["input"]["output_reserve"] == 32768
    await models.close()


# ---------------------------------------------------------------------------
# 4. 5 轮增量工作摘要：自动认上一版、只有一个摘要块
# ---------------------------------------------------------------------------


def _filler(n: int, tag: str) -> dict:
    return {"role": "user", "content": f"{tag}-{n} " + "x" * 2000}


def _append_until_trigger(models, view, tools, *, tag: str, cap: int = 40) -> bool:
    """往工作视图里塞新内容，直到真到触发线、且确实有东西可切。"""
    window, reserve = 128000, 32768
    for n in range(cap):
        view.append(_filler(n, tag))
        info = compaction.estimate_request_input(models, role="main", messages=view, tools=tools)
        if info["estimated_input_tokens"] < info["trigger_threshold"]:
            continue
        plan = compaction.plan_projection(view, context_window=window, output_reserve=reserve)
        if plan.cut:
            return True
    return False


@pytest.mark.asyncio
async def test_five_round_incremental_working_summary_has_one_block(tmp_path) -> None:
    steps = [{"kind": "ok", "content": f"第{i}版摘要 Primary Request and Intent：接着干"} for i in range(1, 12)]
    models, ep, _store = _models(tmp_path, steps)
    tools = _tool_schema(200)
    view: list[dict] = [{"role": "system", "content": "你是 MaiWork 的主模型"}]
    seen: list[str] = []
    cumulative = 0
    for round_no in range(1, 6):
        calls_before = len(ep.calls)
        assert _append_until_trigger(models, view, tools, tag=f"r{round_no}"), f"第 {round_no} 轮没到触发线"
        outcome = await compaction.maybe_compact_ex(
            [dict(m) for m in view], models=models, role="main", agent="main", tools=tools,
        )
        assert outcome.action == "summarized", f"第 {round_no} 轮没自动摘要：{outcome.action} / {outcome.failure}"
        view = [dict(m) for m in outcome.messages]
        blocks = [i for i, m in enumerate(view) if compaction.is_summary_message(m)]
        leading_system = sum(1 for m in view[: blocks[0]] if str(m.get("role") or "") == "system") if blocks else 0
        assert len(blocks) == 1, f"第 {round_no} 轮摘要块该只有一个，实际 {blocks}"
        assert blocks[0] == leading_system, (
            f"第 {round_no} 轮摘要块该紧跟在 system 之后，实际下标 {blocks[0]}"
        )
        body = str(view[blocks[0]].get("content") or "")
        assert body.startswith(compaction.SUMMARY_MARKER)
        assert sum(str(m.get("content") or "").count(compaction.SUMMARY_MARKER) for m in view) == 1, "摘要块不许重复"
        found = _SUMMARY_IDX_RE.search(body)
        assert found, f"第 {round_no} 轮头部摘要不是模型这轮产出的：{body[:80]!r}"
        seen.append(found.group(0))
        bodies = [str(m.get("content") or "") for c in ep.calls[calls_before:] for m in c["body"]["messages"]]
        if round_no > 1:
            assert any(seen[round_no - 2] in text for text in bodies), (
                f"第 {round_no} 轮的摘要输入里没有上一版摘要 {seen[round_no - 2]}：每次都从头摘要了"
            )
        assert outcome.coverage is not None
        assert outcome.coverage.cumulative_covered >= cumulative
        cumulative = outcome.coverage.cumulative_covered
    assert len(set(seen)) == 5, f"5 轮各自产出一版摘要，实际 {seen}"
    await models.close()


# ---------------------------------------------------------------------------
# 5. 自动认上一版摘要（生产形状）——现在红，见下面 docstring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_previous_summary_must_fire_in_production_view_shape(tmp_path) -> None:
    """增量摘要的「自动认上一版」必须真的认出来（`workers.py:815` 就靠它）。

    冲突点：`maybe_compact_ex` 的 docstring 承诺「调用方没显式给 previous_summary 时，
    自动认工作 messages 里开头那一段已有的摘要…把它的正文当上一版摘要、它的覆盖统计当
    累计基数，并从这次的来源里去掉（不重复压一遍摘要）」；`extract_previous_summary` 自己
    的 docstring 也允许 system / 钉住的原话排在摘要前面。但实现里一旦在摘要后面遇到**任何
    真实消息**就直接 `return "", {}`——而生产的工作视图永远是
    「[system, 摘要, 后面还有更近的消息]」（压缩完就是这样），所以这个自动路径永远不触发：
    `previous_summary_auto` 恒为 False、覆盖基数不累计、上一版摘要被当成普通来源重压一遍
    （`workers.py:815` 的调用没传 previous_summary，正好走这条路）。
    通道显式传 previous_summary 的调用点（coordinator.py:944/4921、admin_chat.py:1570）不受影响。

    这条断言是「要么修自动认法、要么把 docstring / 观测字段一起去掉」的二选一证据。
    """
    models, _ep, _store = _models(tmp_path, [{"kind": "ok", "content": SUMMARY_STUB}])
    tools = _tool_schema(200)
    old = compaction.summary_to_message(
        "第0版摘要正文：约束 A 不许丢",
        coverage={"covered_messages": 900, "cumulative_covered": 1000},
    )
    view: list[dict] = [{"role": "system", "content": "你是 MaiWork 的主模型"}, old]
    assert compaction.extract_previous_summary(view)[0], "system 之后的摘要要认出来（前缀位置）"
    assert _append_until_trigger(models, view, tools, tag="auto"), "没到触发线"
    assert compaction.extract_previous_summary(view)[0], (
        "生产形状（摘要后面还有更近的消息）也要认出来：现在这里返回空 → 自动增量摘要从不触发"
    )
    outcome = await compaction.maybe_compact_ex(
        [dict(m) for m in view], models=models, role="main", agent="main", tools=tools,
    )
    assert outcome.action == "summarized"
    obs = outcome.observations["summary"]
    assert obs["previous_summary_auto"] is True, "docstring 承诺的自动认上一版没发生"
    assert obs["previous_summary_used"] is True, "上一版摘要没进这次摘要输入"
    assert obs["cumulative_covered"] >= 1000, "累计覆盖要以上一版为基数（现在从零重算）"
    await models.close()
