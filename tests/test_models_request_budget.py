"""Models 的整包请求预算 + 派发前物理硬闸 + 保守 usage 校正（docs/27 §8 P1）。

全部走 httpx.MockTransport，不发真网络；假端点复用 tests/test_models.py 的 FakeEndpoint。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import (
    CALIBRATION_MAX_FACTOR,
    INPUT_SAFETY_MARGIN,
    MIN_USABLE_INPUT,
    ModelError,
    Models,
    _JSON_ONLY_HINT,
    _strip_internal_keys,
)
from CharTyr_MaiWork.maiwork.store import Store
from tests.test_models import SECRET, FakeEndpoint, _FakeAgents, _make_new, _new_cfg

# 只有 async 测试打标（strict 模式），同步测试不打标避免 asyncio 警告


def _settings_dict(cfg: dict):
    settings, _problems = load_settings(cfg)
    return settings


def _models_with(tmp_path, cfg: dict, transport=None, agents=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    holder = {"settings": _settings_dict(cfg)}
    m = Models(store, lambda: holder["settings"], transport=transport,
               agents=agents if agents is not None else _FakeAgents({"main": {"model": "m1"}}))
    return store, holder, m


def _cfg_windows(m1_window=100000, m1_max=8192, m2_window=200000, m2_max=16384):
    cfg = _new_cfg()
    cfg["model_list"] = [
        {"id": "m1", "endpoint": "default", "model": "m-main", "name": "主模型展示",
         "context_window": m1_window, "max_tokens": m1_max},
        {"id": "m2", "endpoint": "default", "model": "m-bak",
         "context_window": m2_window, "max_tokens": m2_max},
    ]
    return cfg


# ---------------------------------------------------------------------------
# 1. 预算字段
# ---------------------------------------------------------------------------


class TestRequestBudgetFields:
    @pytest.mark.asyncio
    async def test_reports_selected_candidate_limits(self, tmp_path) -> None:
        store, _holder, models = _make_new(tmp_path, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 1300}]
        b = models.request_budget("main", messages=msgs)
        assert b.context_window == 100000
        assert b.max_output_tokens == 8192
        assert b.output_reserve == 8192 == b.max_output_tokens, "预留必须等于实际会发的 max_tokens"
        assert b.usable_input_tokens == 100000 - 8192 - INPUT_SAFETY_MARGIN
        assert b.estimated_input_tokens > 0
        assert b.estimated_message_tokens >= 1000
        assert b.overhead_tokens > 0
        assert b.fits is True and b.shortfall_tokens == 0
        assert b.limit_source == "candidate"
        assert b.model == "m-main" and b.entry_id == "m1"
        assert b.calibrate_factor == 1.0 and b.usage_source == "estimated"
        assert b.trigger_threshold > 0
        await models.close()

    @pytest.mark.asyncio
    async def test_caller_max_tokens_is_the_reserve(self, tmp_path) -> None:
        store, _holder, models = _make_new(tmp_path, cfg=_cfg_windows())
        b = models.request_budget("main", messages=[{"role": "user", "content": "x"}], max_tokens=1234)
        assert (b.max_output_tokens, b.output_reserve) == (1234, 1234)
        await models.close()

    @pytest.mark.asyncio
    async def test_escalate_follows_the_escalation_entry(self, tmp_path) -> None:
        agents = _FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1", "escalate": "m2"}})
        store, _holder, models = _make_new(tmp_path, cfg=_cfg_windows(), agents=agents)
        b = models.request_budget("task", messages=[{"role": "user", "content": "x"}], escalate=True)
        assert b.context_window == 200000 and b.max_output_tokens == 16384
        assert b.escalate is True
        await models.close()

    @pytest.mark.asyncio
    async def test_agent_kind_without_choice_falls_back_to_main(self, tmp_path) -> None:
        agents = _FakeAgents({"main": {"model": "m2"}})
        store, _holder, models = _make_new(tmp_path, cfg=_cfg_windows(), agents=agents)
        b = models.request_budget("news", messages=[{"role": "user", "content": "x"}])
        assert b.context_window == 200000 and b.entry_id == "m2"
        await models.close()

    @pytest.mark.asyncio
    async def test_system_text_is_counted(self, tmp_path) -> None:
        store, _holder, models = _make_new(tmp_path, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 1300}]
        plain = models.request_budget("main", messages=msgs)
        with_sys = models.request_budget("main", messages=msgs, system_text="规" * 1300)
        assert with_sys.estimated_message_tokens == plain.estimated_message_tokens + 1000
        assert with_sys.estimated_input_tokens == plain.estimated_input_tokens + 1000
        await models.close()

    @pytest.mark.asyncio
    async def test_json_mode_counts_the_injected_hint(self, tmp_path) -> None:
        store, _holder, models = _make_new(tmp_path, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 130}]
        plain = models.request_budget("main", messages=msgs)
        js = models.request_budget("main", messages=msgs, json_mode=True)
        hint_tokens = len(_JSON_ONLY_HINT) / 1.3
        assert js.estimated_input_tokens - plain.estimated_input_tokens >= int(hint_tokens)
        # 提示本身也要进指纹：json_mode 变了就不算同一个请求
        assert js.estimated_input_tokens > plain.estimated_input_tokens
        await models.close()

    @pytest.mark.asyncio
    async def test_system_text_invalidates_measured_snapshot(self, tmp_path) -> None:
        usage = {"prompt_tokens": 2000, "completion_tokens": 1}
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": usage}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 500}]
        await models.chat("main", msgs)
        same = models.request_usage_snapshot("main", messages=msgs)
        assert same is not None and same["measured_input_tokens"] == 2000
        # 换了 system_text = 另一个整包请求：实测总量作废
        other = models.request_usage_snapshot("main", messages=msgs, system_text="另一段系统提示")
        assert other is not None and other["measured_input_tokens"] == 0
        assert other["usage_source"] == "estimated"
        # 预算口也认它：system_text 要算进整包估算（比纯 messages 估算的那一项）
        plain = models.request_budget("main", messages=msgs)
        with_sys = models.request_budget("main", messages=msgs, system_text="另一段系统提示")
        assert with_sys.estimated_message_tokens > plain.estimated_message_tokens
        await models.close()


# ---------------------------------------------------------------------------
# 2. 派发前的物理硬闸
# ---------------------------------------------------------------------------


class TestPhysicalFitGuard:
    @pytest.mark.asyncio
    async def test_oversized_request_raises_before_dispatch(self, tmp_path) -> None:
        ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
        store, _holder, models = _make_new(
            tmp_path, ep, cfg=_cfg_windows(m1_window=8192, m1_max=4096)
        )
        msgs = [{"role": "user", "content": "x" * 6000}]
        with pytest.raises(ModelError) as ei:
            await models.chat("main", msgs)
        assert ep.calls == [], "超限请求不许发出去"
        assert "上下文" in ei.value.message or "放不下" in ei.value.message
        await models.close()

    @pytest.mark.asyncio
    async def test_incoherent_output_cap_rejected(self, tmp_path) -> None:
        """max_tokens 大到窗口装不下（配置不自洽）：明确拒绝，不静默少算输出。"""
        ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
        store, _holder, models = _make_new(
            tmp_path, ep, cfg=_cfg_windows(m1_window=8192, m1_max=7000)
        )
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "hi"}])
        assert ep.calls == []
        msg = ei.value.message
        assert "7000" in msg and "8192" in msg
        await models.close()

    @pytest.mark.asyncio
    async def test_backup_with_bigger_window_serves_the_request(self, tmp_path) -> None:
        """首选窗口不够、备用够大：换备用，不改调用方传的内容。"""
        cfg = _cfg_windows(m1_window=8192, m1_max=4096, m2_window=200000, m2_max=8192)
        agents = _FakeAgents({"main": {"model": "m1", "backup": "m2"}})
        ep = FakeEndpoint({"m-bak": [{"kind": "ok", "content": "备用接住了"}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=cfg, agents=agents)
        msgs = [{"role": "user", "content": "x" * 6000}]
        r = await models.chat("main", msgs)
        assert r.text == "备用接住了"
        assert [c["model"] for c in ep.calls] == ["m-bak"]
        await models.close()

    @pytest.mark.asyncio
    async def test_fit_guard_uses_calibration_factor(self, tmp_path) -> None:
        """校正系数参与硬闸：估得偏小也要按保守系数判能不能装下。"""
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": {"prompt_tokens": 4000, "completion_tokens": 1}}]})
        store, _holder, models = _make_new(
            tmp_path, ep, cfg=_cfg_windows(m1_window=8192, m1_max=1024)
        )
        small = [{"role": "user", "content": "x" * 100}]
        await models.chat("main", small)  # 第一次：真实用量远超估算 → 校正
        b = models.request_budget("main", messages=small)
        assert b.calibrate_factor > 3.0
        # 同样一条小消息，校正后按 4 倍算也装不下（8192-1024-512=6656）
        big = [{"role": "user", "content": "y" * 2300}]
        with pytest.raises(ModelError):
            await models.chat("main", big)
        assert ep.chat_count == 1
        await models.close()


# ---------------------------------------------------------------------------
# 3. 保守 usage 校正
# ---------------------------------------------------------------------------


class TestUsageCalibration:
    @pytest.mark.asyncio
    async def test_known_usage_calibrates_bounded_ratio(self, tmp_path) -> None:
        usage = {"prompt_tokens": 2000, "completion_tokens": 3, "total_tokens": 2003}
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": usage}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 1000}]
        await models.chat("main", msgs)
        snap = models.request_usage_snapshot("main", messages=msgs)
        assert snap is not None
        assert snap["measured_input_tokens"] == 2000
        assert snap["usage_source"] == "measured"
        assert snap["estimated_input_tokens"] > 0
        assert snap["calibrate_factor"] == pytest.approx(
            2000 / snap["estimated_input_tokens"], rel=0.02
        )
        assert 1.0 < snap["calibrate_factor"] <= CALIBRATION_MAX_FACTOR
        b = models.request_budget("main", messages=msgs)
        assert b.calibrate_factor == snap["calibrate_factor"]
        assert b.usage_source == "measured"
        await models.close()

    @pytest.mark.asyncio
    async def test_calibration_ratio_is_capped(self, tmp_path) -> None:
        usage = {"prompt_tokens": 9000, "completion_tokens": 1}
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": usage}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        await models.chat("main", [{"role": "user", "content": "x"}])
        snap = models.request_usage_snapshot("main", messages=[{"role": "user", "content": "x"}])
        assert snap["calibrate_factor"] == CALIBRATION_MAX_FACTOR
        await models.close()

    @pytest.mark.asyncio
    async def test_middle_edit_invalidates_measured_total(self, tmp_path) -> None:
        usage = {"prompt_tokens": 2000, "completion_tokens": 1}
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": usage}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        before = [{"role": "user", "content": "x" * 500 + "AAA" + "x" * 500}]
        await models.chat("main", before)
        same = models.request_usage_snapshot("main", messages=before)
        assert same and same["measured_input_tokens"] == 2000
        # 首尾一样、只有中段被改过：也要认出来是另一个请求
        edited = [{"role": "user", "content": "x" * 500 + "BBB" + "x" * 500}]
        snap = models.request_usage_snapshot("main", messages=edited)
        assert snap is not None and snap["measured_input_tokens"] == 0
        assert snap["usage_source"] == "estimated"
        b = models.request_budget("main", messages=edited)
        assert b.usage_source == "estimated" and b.calibrate_factor > 1.0, "系数是模型级保守值，可以留着"
        await models.close()

    @pytest.mark.asyncio
    async def test_unknown_usage_never_calibrates(self, tmp_path) -> None:
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": {}}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 500}]
        await models.chat("main", msgs)
        assert models.request_usage_snapshot("main", messages=msgs) is None
        b = models.request_budget("main", messages=msgs)
        assert b.calibrate_factor == 1.0 and b.usage_source == "estimated"
        await models.close()

    @pytest.mark.asyncio
    async def test_calibration_factor_never_shrinks_across_samples(self, tmp_path) -> None:
        """样本之间只放大不缩小：第二次实测比估算小，也不抹掉已经学到的保守系数。"""
        ep = FakeEndpoint({"m-main": [
            {"kind": "ok", "usage": {"prompt_tokens": 9000, "completion_tokens": 1}},
            {"kind": "ok", "usage": {"prompt_tokens": 100, "completion_tokens": 1}},
        ]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "x" * 500}]
        await models.chat("main", msgs)
        first = models.request_usage_snapshot("main", messages=msgs)
        assert first["calibrate_factor"] == CALIBRATION_MAX_FACTOR
        await models.chat("main", msgs)
        second = models.request_usage_snapshot("main", messages=msgs)
        assert second["samples"] == 2
        assert second["measured_input_tokens"] == 100
        assert second["calibrate_factor"] == CALIBRATION_MAX_FACTOR, "系数不许被后来的小样本缩回去"
        await models.close()

    @pytest.mark.asyncio
    async def test_anthropic_cached_tokens_count_as_input(self, tmp_path) -> None:
        """Anthropic 的 input_tokens 不含缓存：校正口径要加上缓存读/写（缓存也占上下文）。"""
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
                "content": [{"type": "text", "text": "好"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 500, "output_tokens": 3,
                          "cache_read_input_tokens": 1500, "cache_creation_input_tokens": 0},
            })

        cfg = {
            "endpoints": [{"id": "a1", "name": "anthropic 端点", "protocol": "anthropic",
                           "base_url": "https://a.test/v1", "api_key": SECRET}],
            "model_list": [{"id": "m1", "endpoint": "a1", "model": "claude-x",
                            "context_window": 200000, "max_tokens": 8192}],
        }
        store, _holder, models = _models_with(tmp_path, cfg, transport=httpx.MockTransport(handler))
        msgs = [{"role": "user", "content": "x" * 1000}]
        await models.chat("main", msgs, agent="main")
        assert seen and seen[0]["max_tokens"] == 8192
        snap = models.request_usage_snapshot("main", messages=msgs)
        assert snap is not None
        assert snap["measured_input_tokens"] == 2000, "500 input + 1500 缓存读"
        assert snap["calibrate_factor"] > 1.0
        await models.close()


# ---------------------------------------------------------------------------
# 4. 内部字段不上线
# ---------------------------------------------------------------------------


class TestInternalKeys:
    def test_strip_internal_keys_is_pure(self) -> None:
        msgs = [
            {"role": "user", "content": "a", "maiwork_pinned": True, "maiwork_coverage": {"x": 1}},
            {"role": "tool", "content": "b", "tool_call_id": "c1"},
        ]
        out = _strip_internal_keys(msgs)
        assert out[0] == {"role": "user", "content": "a"}
        assert out[1] == msgs[1]
        assert msgs[0]["maiwork_pinned"] is True, "不许就地改调用方的消息"

    @pytest.mark.asyncio
    async def test_pinned_flag_never_sent_to_endpoint(self, tmp_path) -> None:
        ep = FakeEndpoint({"m-main": [{"kind": "ok"}]})
        store, _holder, models = _make_new(tmp_path, ep, cfg=_cfg_windows())
        msgs = [{"role": "user", "content": "需求清单：只许动本任务文件", "maiwork_pinned": True}]
        await models.chat("main", msgs)
        sent = ep.calls[0]["body"]["messages"]
        assert "maiwork_pinned" not in sent[0]
        assert sent[0]["content"] == "需求清单：只许动本任务文件"
        await models.close()
