"""整包预算 / 物理硬闸接线 / 只改投影的剪枝 / 用户原文程序化保护（docs/27 §8 P0–P1）。

这里只测 compaction.py 与 models.py 新增的语义：
1. 摘要失败必须返回**真正原始**的 history（不是第一阶段剪过 tool 正文的投影）；
2. 超长 tool 结果没有可回读路径就不剪（剪枝必须能回读）；
3. 最新 user 原文 / 打了 pin 的需求原文由程序保护，不进摘要；
4. 整包预算口（context_budget / resolve_output_reserve）认实际选中模型的窗口与输出上限；
5. models.Models.request_budget 的物理硬闸在派发前拦下超限请求（见 test_models_request_budget.py）。
"""

from __future__ import annotations

import copy

import pytest

from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.models import ChatResult, ModelError

SUMMARY_STUB = "Primary Request and Intent：把活干完\nPending Jobs：无\nNext Step：等"


def _msg(role, content="", **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def _tool_call(call_id="c1", name="read_file"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


class _Models:
    """假模型：按脚本回摘要 / 抛错；记录每次收到的完整 prompt。"""

    def __init__(self, replies=None, error=None, limits=None, budget=None):
        self.replies = list(replies or [SUMMARY_STUB])
        self.error = error
        self.limits = limits
        self.budget = budget
        self.calls: list[list[dict]] = []

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append([dict(m) for m in (messages or [])])
        if self.error is not None:
            raise self.error
        text = self.replies.pop(0) if self.replies else SUMMARY_STUB
        return ChatResult(text=text, tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={})

    def limits_for(self, kind=None, *, escalate=False):
        if self.limits is None:
            raise AttributeError("没接 limits_for")
        return dict(self.limits)

    def request_budget(self, kind=None, **kw):
        if self.budget is None:
            raise AttributeError("没接 request_budget")
        return dict(self.budget)

    @property
    def prompts(self) -> list[str]:
        return ["\n".join(str(m.get("content") or "") for m in call) for call in self.calls]


def _filler_users(n: int, tag: str = "f") -> list[dict]:
    """n 条 ≈1542 tokens 的老 user 消息（撑过 8192 的触发线，又装得进 keep 预算）。"""
    return [_msg("user", f"{tag}{i} " + "x" * 2000) for i in range(n)]


def _new_wiring_cfg(*, window: int, max_tokens: int) -> dict:
    """真实 Models 用的配置（一个端点 + 一条模型库条目）。"""
    from tests.test_models import _new_cfg

    cfg = _new_cfg()
    cfg["model_list"] = [
        {"id": "m1", "endpoint": "default", "model": "m-main", "name": "主模型展示",
         "context_window": window, "max_tokens": max_tokens},
    ]
    return cfg


def _history(*, recoverable: bool = False) -> list[dict]:
    """system + 两条老 user + 一组工具调用 + 一条最近的 user。

    recoverable=True 时那条超大 tool 结果自带 spill 指针行（可回读）。
    """
    big = "T" * 20000
    if recoverable:
        big += "\n\n（完整输出在：/tmp/spill-1.txt）"
    return [
        _msg("system", "sys"),
        _msg("user", "老约束A" + "x" * 9000),
        _msg("user", "老约束B" + "y" * 9000),
        _msg("assistant", "", tool_calls=[_tool_call()]),
        _msg("tool", big, tool_call_id="c1"),
        _msg("user", "最近的活 " + "r" * 200),
    ]


# ---------------------------------------------------------------------------
# 1. 失败 = 真正原始 history（不是 stage1 剪过的投影）
# ---------------------------------------------------------------------------


class TestFailureKeepsTrueOriginal:
    @pytest.mark.asyncio
    async def test_summary_failure_returns_true_original_not_stage1(self):
        msgs = _history()
        snapshot = copy.deepcopy(msgs)
        models = _Models(error=ModelError("端点挂了", status=500))
        out = await compaction.maybe_compact(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t", keep_recent_n=0,
        )
        assert out is msgs, "失败必须返回调用方传进来的那份 history"
        assert out == snapshot
        assert len(out[4]["content"]) == 20000, "没有可回读路径的大工具结果不许被静默剪掉"

    @pytest.mark.asyncio
    async def test_outcome_records_failure_and_prune_observation(self):
        msgs = _history()
        models = _Models(error=ModelError("端点挂了", status=500))
        outcome = await compaction.maybe_compact_ex(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t", keep_recent_n=0,
        )
        assert outcome.messages is msgs
        assert outcome.action == "failed_original"
        assert outcome.changed is False
        assert outcome.failure and "端点" in outcome.failure
        # 分开观测：这次一条都没剪（没有归档、也没有现成指针）
        assert outcome.observations["prune"]["count"] == 0
        assert outcome.observations["prune"]["skipped_unrecoverable"] == 1
        # 输入尺寸与触发线分开记
        assert outcome.observations["input"]["tokens_before"] > outcome.observations["input"]["threshold"]
        assert outcome.observations["failure"]["kind"]

    @pytest.mark.asyncio
    async def test_empty_summary_returns_original(self):
        msgs = _history(recoverable=True)
        models = _Models(replies=["   "])
        outcome = await compaction.maybe_compact_ex(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t", keep_recent_n=0,
        )
        assert outcome.messages is msgs and outcome.action == "failed_original"
        assert "空" in (outcome.failure or "")


# ---------------------------------------------------------------------------
# 2. 剪枝必须能回读（没有归档路径就不剪）
# ---------------------------------------------------------------------------


class TestPruneRecoverability:
    def test_require_recoverable_skips_without_archive(self):
        big = "z" * 20000
        msgs = [_msg("tool", big)]
        res = compaction.prune_big_tool_outputs(msgs, keep_recent_n=0, require_recoverable=True)
        assert res.changed == 0
        assert res.skipped_unrecoverable == 1
        assert res.messages[0]["content"] == big

    def test_archive_hook_writes_full_output_and_keeps_pointer(self):
        big = "z" * 20000
        msgs = [_msg("tool", big)]
        archived: list[dict] = []

        def archive(msg):
            archived.append(dict(msg))
            return "（完整输出在：/tmp/spill-9.txt）"

        res = compaction.prune_big_tool_outputs(
            msgs, keep_recent_n=0, archive=archive, require_recoverable=True
        )
        assert res.changed == 1 and res.pointers == 1
        assert archived and archived[0]["content"] == big
        body = res.messages[0]["content"]
        assert "/tmp/spill-9.txt" in body and "省略" in body

    def test_archive_dir_writes_full_output(self, tmp_path):
        big = "z" * 30000
        msgs = [_msg("tool", big)]
        res = compaction.prune_big_tool_outputs(
            msgs, keep_recent_n=0, archive_dir=tmp_path, require_recoverable=True
        )
        assert res.changed == 1 and res.pointers == 1
        files = list(tmp_path.glob("spill-*.txt"))
        assert files and files[0].read_text(encoding="utf-8") == big
        assert "完整输出在" in res.messages[0]["content"]

    def test_existing_spill_pointer_counts_as_recoverable(self):
        big = "z" * 20000 + "\n\n（完整输出在：/tmp/spill-1.txt）"
        msgs = [_msg("tool", big)]
        res = compaction.prune_big_tool_outputs(msgs, keep_recent_n=0, require_recoverable=True)
        assert res.changed == 1
        assert "/tmp/spill-1.txt" in res.messages[0]["content"]

    def test_legacy_truncate_keeps_old_behavior(self):
        """老的直接调用口保持兼容：没有指针也照旧头尾截（这里不改它的语义）。"""
        big = "z" * 20000
        msgs = [_msg("tool", big)]
        kept, n = compaction.truncate_big_tool_outputs(msgs, keep_recent_n=0)
        assert n == 1 and "省略" in kept[0]["content"]

    @pytest.mark.asyncio
    async def test_maybe_compact_ex_uses_archive_and_reports_prune(self, tmp_path):
        msgs = _history()
        models = _Models(replies=[SUMMARY_STUB, SUMMARY_STUB])
        outcome = await compaction.maybe_compact_ex(
            msgs, models=models, role="main", context_window=8192, output_reserve=256,
            purpose="t", keep_recent_n=0, archive_dir=tmp_path,
        )
        assert outcome.observations["prune"]["count"] == 1
        assert outcome.observations["prune"]["pointers"] == 1
        assert len(msgs[4]["content"]) == 20000, "原来的那条不能被就地改掉"
        assert any("/tmp" in str(m.get("content") or "") or "spill-" in str(m.get("content") or "")
                   for m in outcome.messages)

    def test_archive_dir_symlink_is_refused(self, tmp_path):
        """落盘目录是软链接：一律不写（软链接能把写入引到工作区外），保留完整正文。"""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        big = "z" * 60000  # 超过 SPILL_CHARS（50000）才会真落盘
        assert compaction.spill_big_output(big, link) == big, "不跟随软链接，退回完整正文"
        assert not list(real.glob("*.txt")), "一个字都不许写进软链接目标"
        res = compaction.prune_big_tool_outputs(
            [_msg("tool", big)], keep_recent_n=0, archive_dir=link, require_recoverable=True
        )
        assert res.changed == 0 and res.skipped_unrecoverable == 1
        assert res.messages[0]["content"] == big
        # 目录不存在时照旧能落盘（新目录由 mkdir 建）
        ok_dir = tmp_path / "fresh" / "spill"
        out = compaction.spill_big_output(big, ok_dir)
        assert out != big
        assert list(ok_dir.glob("spill-*.txt"))

    def test_spill_write_failure_keeps_full_body(self, tmp_path):
        """写不进去（这里用文件占住目录名）：保留完整正文，不头尾截断、不假装归档成功。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("我是文件，不是目录", encoding="utf-8")
        big = "z" * 30000
        assert compaction.spill_big_output(big, blocker) == big

    def test_raw_history_kept_is_not_a_recoverability_basis(self):
        """raw lane 留了原文 ≠ 模型读得回来：这个兼容参数不许让不可回读的 tool 结果被剪。"""
        big = "z" * 20000
        res = compaction.prune_big_tool_outputs(
            [_msg("tool", big)], keep_recent_n=0, require_recoverable=True, raw_history_kept=True,
        )
        assert res.changed == 0 and res.skipped_unrecoverable == 1


# ---------------------------------------------------------------------------
# 3. 最新 user 原文 / pin 住的需求原文由程序保护
# ---------------------------------------------------------------------------


class TestProgrammaticProtection:
    def test_latest_user_stays_out_of_cut(self):
        msgs = [
            _msg("system", "sys"),
            _msg("user", "u1 " + "a" * 2000),
            _msg("user", "u2 " + "b" * 2000),
            _msg("user", "LATEST-USER " + "c" * 2000),
            _msg("assistant", "a1 " + "d" * 2000),
            _msg("assistant", "a2 " + "e" * 2000),
        ]
        keep, cut = compaction.pick_cut_point(msgs, context_window=8192, output_reserve=256)
        assert cut, "这个体量应该切得出最老一段"
        assert not any("LATEST-USER" in str(m.get("content") or "") for m in cut)
        assert any("LATEST-USER" in str(m.get("content") or "") for m in keep)
        # keep 内顺序还是原来的先后
        idx = [msgs.index(m) for m in keep]
        assert idx == sorted(idx)

    def test_without_protection_latest_user_is_summarized(self):
        msgs = [
            _msg("system", "sys"),
            _msg("user", "u1 " + "a" * 2000),
            _msg("user", "u2 " + "b" * 2000),
            _msg("user", "LATEST-USER " + "c" * 2000),
            _msg("assistant", "a1 " + "d" * 2000),
            _msg("assistant", "a2 " + "e" * 2000),
        ]
        keep, cut = compaction.pick_cut_point(
            msgs, context_window=8192, output_reserve=256, protect_latest_user=False
        )
        assert any("LATEST-USER" in str(m.get("content") or "") for m in cut)

    def test_pinned_requirements_are_never_summarized(self):
        pinned = compaction.pin_message(_msg("user", "需求清单R2：只许动本任务文件"))
        assert compaction.is_pinned(pinned)
        msgs = [
            _msg("system", "sys"),
            _msg("user", "u1 " + "a" * 2000),
            pinned,
            _msg("user", "u2 " + "b" * 2000),
            _msg("assistant", "a1 " + "d" * 2000),
            _msg("assistant", "a2 " + "e" * 2000),
        ]
        keep, cut = compaction.pick_cut_point(msgs, context_window=8192, output_reserve=256)
        assert cut
        assert not any(compaction.is_pinned(m) for m in cut)
        assert any(compaction.is_pinned(m) for m in keep)

    @pytest.mark.asyncio
    async def test_compaction_keeps_pinned_original_and_never_sends_it_to_summarizer(self):
        pinned = compaction.pin_message(_msg("user", "需求清单R3：禁止发布到公网"))
        msgs = (
            [_msg("system", "sys")]
            + _filler_users(2, "u")
            + [pinned]
            + _filler_users(3, "v")
            + [_msg("assistant", "a1 " + "d" * 2000), _msg("assistant", "a2 " + "e" * 2000)]
            + [_msg("user", "最近的活 " + "r" * 200)]
        )
        models = _Models(replies=[SUMMARY_STUB, SUMMARY_STUB])
        outcome = await compaction.maybe_compact_ex(
            msgs, models=models, role="main", context_window=8192, output_reserve=256, purpose="t",
        )
        assert outcome.action == "summarized"
        assert not any("禁止发布到公网" in p for p in models.prompts), "pin 住的原文不许进摘要输入"
        kept = [m for m in outcome.messages if compaction.is_pinned(m)]
        assert len(kept) == 1 and "禁止发布到公网" in kept[0]["content"]


# ---------------------------------------------------------------------------
# 4. 预算口：实际选中模型的窗口 / 输出上限
# ---------------------------------------------------------------------------


class _BudgetRecorder:
    def __init__(self, window=64000, max_tokens=8192, factor=1.0):
        self.window = window
        self.max_tokens = max_tokens
        self.factor = factor
        self.kinds: list[tuple] = []

    def request_budget(self, kind=None, **kw):
        self.kinds.append((kind, dict(kw)))
        return {
            "kind": str(kind or ""),
            "context_window": self.window,
            "max_tokens": self.max_tokens,
            "max_output_tokens": self.max_tokens,
            "output_reserve": self.max_tokens,
            "calibrate_factor": self.factor,
        }

    def limits_for(self, kind=None, *, escalate=False):
        self.kinds.append((kind, {"escalate": escalate}))
        return {"context_window": self.window, "max_tokens": self.max_tokens}


class TestBudgetEntryPoints:
    def test_role_maps_to_agent_kind(self):
        models = _BudgetRecorder()
        budget = compaction.context_budget(models, role="worker")
        assert budget["context_window"] == 64000
        assert models.kinds[0][0] == "task"
        assert models.kinds[0][1]["escalate"] is False

    def test_agent_argument_wins_and_escalate_forwarded(self):
        models = _BudgetRecorder()
        compaction.context_budget(models, role="worker", agent="news", escalate=True)
        assert models.kinds[0][0] == "news"
        assert models.kinds[0][1]["escalate"] is True

    def test_explicit_output_reserve_wins(self):
        models = _BudgetRecorder()
        budget = compaction.resolve_context_budget(
            models, role="main", context_window=200000, output_reserve=1234
        )
        assert budget["output_reserve"] == 1234
        assert budget["context_window"] == 200000

    def test_none_reserve_uses_selected_model_max_tokens(self):
        models = _BudgetRecorder(window=64000, max_tokens=8192)
        assert compaction.resolve_output_reserve(models, role="main") == 8192

    def test_fallback_when_models_has_no_budget_api(self):
        class _Bare:
            pass

        assert compaction.resolve_output_reserve(_Bare(), role="main") == compaction.DEFAULT_OUTPUT_RESERVE

    def test_fallback_uses_limits_for(self):
        class _Limits:
            def limits_for(self, kind=None, *, escalate=False):
                return {"context_window": 256000, "max_tokens": 16384}

        assert compaction.resolve_output_reserve(_Limits(), role="main") == 16384
        assert compaction.resolve_context_budget(_Limits(), role="main")["context_window"] == 256000

    @pytest.mark.asyncio
    async def test_maybe_compact_ex_uses_budget_when_caller_gives_none(self):
        """output_reserve=None + 有预算口 → 预留取实际选中模型的 max_tokens（不再写死 8192）。"""
        msgs = _history()
        models = _Models(
            replies=[SUMMARY_STUB, SUMMARY_STUB],
            budget={"context_window": 128000, "max_tokens": 32768, "output_reserve": 32768},
        )
        outcome = await compaction.maybe_compact_ex(
            msgs, models=models, role="main", purpose="t", output_reserve=None,
        )
        assert outcome.observations["input"]["output_reserve"] == 32768
        assert outcome.observations["input"]["context_window"] == 128000

    def test_calibrate_factor_is_applied_to_input_estimate(self):
        class _Cal:
            def request_budget(self, kind=None, **kw):
                return {"context_window": 128000, "max_tokens": 8192,
                        "output_reserve": 8192, "calibrate_factor": 2.0}

        msgs = [_msg("user", "x" * 1300)]
        base = compaction.estimate_tokens_in_messages(msgs)
        assert compaction.calibrated_tokens_in_messages(msgs, factor=2.0) == 2 * base
        budget = compaction.context_budget(_Cal(), role="main", messages=msgs)
        assert budget["calibrate_factor"] == 2.0


# ---------------------------------------------------------------------------
# 5. 摘要调用本身：focus 有界、硬约束规矩仍在
# ---------------------------------------------------------------------------


class TestSummaryFocus:
    @pytest.mark.asyncio
    async def test_focus_is_bounded_and_constraints_rules_still_present(self):
        models = _Models(replies=[SUMMARY_STUB])
        focus = "重点看代码样本" * 100  # 远超上限
        await compaction.summarize_messages(
            [_msg("user", "帮我改一下"), _msg("assistant", "好")],
            models=models, role="main", purpose="t", focus=focus,
        )
        prompt = models.prompts[0]
        assert "重点看代码样本" in prompt
        assert len(focus) > compaction.FOCUS_MAX_CHARS
        assert prompt.count("重点看代码样本") * len("重点看代码样本") <= compaction.FOCUS_MAX_CHARS
        # 硬约束规矩没被 focus 顶掉
        assert "原样逐条列出" in prompt

    @pytest.mark.asyncio
    async def test_focus_empty_is_noise_free(self):
        models = _Models(replies=[SUMMARY_STUB])
        await compaction.summarize_messages(
            [_msg("user", "帮我改一下")], models=models, role="main", purpose="t", focus="   ",
        )
        assert "重点" not in models.prompts[0]


# ---------------------------------------------------------------------------
# 6. 真实 Models × compaction：跨文件接线回归（dataclass 预算口 / 校正 / 大工具表）
# ---------------------------------------------------------------------------


class TestProductionWiring:
    """矩阵只测 models 接口是抓不到「compaction 读不到 dataclass」这类接线的。"""

    @pytest.mark.asyncio
    async def test_context_budget_reads_real_models_dataclass_budget(self, tmp_path):
        from tests.test_models import FakeEndpoint, _make_new

        cfg = _new_wiring_cfg(window=200000, max_tokens=8192)
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "usage": {"prompt_tokens": 4000, "completion_tokens": 1}}]})
        _store, _holder, models = _make_new(tmp_path, ep, cfg=cfg)
        msgs = [_msg("user", "x" * 1000)]
        tools = [{
            "type": "function",
            "function": {"name": "list_files", "description": "d" * 3000,
                         "parameters": {"type": "object", "properties": {}}},
        }]
        await models.chat("main", msgs, tools=tools)  # 写下真 usage 校正

        budget = compaction.context_budget(models, role="main", messages=msgs, tools=tools)
        assert budget["limit_source"] == "candidate", "必须真读到 Models.request_budget（不是回落 limits_for）"
        assert budget["usage_source"] == "measured", "实测用量要带出来"
        assert budget["calibrate_factor"] > 1.0, "保守校正系数要带出来"
        assert budget["estimated_tool_tokens"] >= 2000, "工具 schema 要算进整包"
        assert budget["context_window"] == 200000 and budget["output_reserve"] == 8192
        assert budget["estimated_input_tokens"] >= 4000, "实测（4000）比估算大 → 取实测"

        info = compaction.estimate_request_input(models, role="main", messages=msgs, tools=tools)
        assert info["estimated_input_tokens"] == budget["estimated_input_tokens"]
        assert info["usable_input_tokens"] == 200000 - 8192 - 512
        assert info["trigger_threshold"] == compaction.compact_threshold(200000, 8192)
        assert info["fits"] is True
        await models.close()

    def test_huge_tool_schema_alone_can_break_the_window(self):
        """工具表大到把窗口吃光：整包估算要看得出来（不是只看 messages）。"""
        class _Bare:
            pass

        tools = [
            {"type": "function",
             "function": {"name": f"t{i}", "description": "d" * 1000,
                          "parameters": {"type": "object", "properties": {}}}}
            for i in range(40)
        ]
        info = compaction.estimate_request_input(
            _Bare(), role="main", messages=[_msg("user", "hi")], tools=tools,
            context_window=8192, output_reserve=256,
        )
        assert info["estimated_tool_tokens"] >= 30000
        assert info["estimated_input_tokens"] > info["trigger_threshold"], "只看 messages 会漏掉它"
        assert info["fits"] is False and info["shortfall_tokens"] > 0


# ---------------------------------------------------------------------------
# 7. 触发线夹在「真能装下的输入」以内（小窗口不许出现「不到线 → 直接撞硬闸」）
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    def test_threshold_never_exceeds_capacity(self):
        from CharTyr_MaiWork.maiwork.models import INPUT_SAFETY_MARGIN

        for window, reserve in (
            (8192, 0), (8192, 256), (8192, 4096), (8192, 7680),
            (64000, 8192), (128000, 32768), (200000, 100000), (2_000_000, 32768),
        ):
            usable = max(1, window - reserve - INPUT_SAFETY_MARGIN)
            threshold = compaction.compact_threshold(window, reserve)
            assert 1 <= threshold <= usable, (window, reserve, threshold, usable)

    def test_small_window_threshold_equals_capacity(self):
        """小窗口下地板 8192 曾经比可用输入还大：现在就地夹到可用输入。"""
        window, reserve = 8192, 256
        usable = max(1, window - reserve - 512)
        assert usable < 8192, "这个用例前提：地板比可用输入大"
        assert compaction.compact_threshold(window, reserve) == usable

    def test_large_window_formula_unchanged(self):
        assert compaction.compact_threshold(context_window=100000, output_reserve=0) == 34464
        assert compaction.compact_threshold(context_window=128000, output_reserve=8192) == (
            128000 - 8192 - 65536
        )


class TestCachePrefixCrossFileWiring:
    """真实 Models（含 chat_compaction_prefix）× compaction：接线漂移要能被抓出来。"""

    @pytest.mark.asyncio
    async def test_compaction_calls_the_real_cache_prefix_api(self, tmp_path):
        from tests.test_models import FakeEndpoint, _make_new

        cfg = _new_wiring_cfg(window=128000, max_tokens=8192)
        ep = FakeEndpoint({"m-main": [{"kind": "ok", "content": "Primary Request and Intent：正常回答"}]})
        _store, _holder, models = _make_new(tmp_path, ep, cfg=cfg)
        history = (
            [_msg("system", "sys")]
            + [_msg("user", f"OLD{i} " + "x" * 9000) for i in range(10)]
            + [_msg("user", "最近的原话：接着改 compaction.py")]
        )
        # 先跑一次正常调用：让 models 记下这条岗位 / purpose 的前缀配方
        await models.chat("main", history, agent="main", purpose="coordinator.plan")
        outcome = await compaction.maybe_compact_ex(
            history, models=models, role="main", agent="main", context_window=128000,
            output_reserve=8192, purpose="coordinator.plan",
        )
        obs = outcome.observations["summary"]
        assert obs, "触发了压缩就该有摘要观测"
        # 契约层：签名必须能被真实 API 接住（签名漂移会变成 api_signature；真出错是 api_error）
        assert obs["fast_path"] not in ("api_signature", "api_error"), obs["fast_path"]
        assert obs["fast_path"] != "no_api", "真实 Models 应该已经有 chat_compaction_prefix"
        if obs["path"] == "cached_prefix":
            start, end = obs["covered_indices"]
            assert end - start + 1 == outcome.coverage.covered_messages
            assert obs["calls"] == 1
            assert outcome.messages[0] == {"role": "system", "content": "sys"}
        else:
            # 快路径现在不给结果（配方对不上 / 超预算）：必须回落独立路径且照样出摘要
            assert obs["path"] == "independent"
            assert outcome.action in ("summarized", "failed_original")
        await models.close()
