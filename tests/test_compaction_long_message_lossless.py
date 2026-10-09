"""R05（docs/14-0.7.1整改复核.md）验收：分段在丢内容之前发生，摘要输入永远 bounded。

漏洞基线（2db1b90）：
- 单条 user 48045 字、关键句在中间：_serialize_cut 先 head 6000 + tail 2000 预截，
  截完 8560 字 < 32000 → 分段不触发 → 关键句消失（evidence: long-user-middle-rule.json）。
- 首段 40000 字的 user：keep_first_paragraph 突破 32000 → 序列化 42029 字
  （evidence: summary-budget-escape.json）。

本文件用假 models 捕获**实际发给模型的 prompt**，断言 marker 真的在输入里
（不是返回 stub 自称保留）。不断言真实模型质量。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.models import ChatResult, ModelError


def _msg(role, content="", **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def _tool_call(call_id="c1", name="f", args="{}"):
    return {"id": call_id, "function": {"name": name, "arguments": args}}


class _RecordingModels:
    """假 models：记录每次 chat 的 messages，按队列返回 8 节摘要文本。"""

    def __init__(self, replies=None, error=None, fail_after=None):
        self.queue = list(replies or [])
        self.error = error
        self.fail_after = fail_after  # 第 N 次（1 基）之后全部抛 ModelError
        self.calls: list[list[dict]] = []

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise ModelError("context_length_exceeded: prompt too long", status=400)
        text = self.queue.pop(0) if self.queue else _summary_stub()
        return ChatResult(text=text, tool_calls=[], model="m",
                          prompt_tokens=1, completion_tokens=1, raw_message={})


def _all_prompt_text(models: _RecordingModels) -> str:
    return "\n".join(
        str(m.get("content") or "")
        for call in models.calls
        for m in call
    )


def _summary_stub(tag: str = "") -> str:
    sections = (
        "Primary Request and Intent", "Key Technical Concepts", "Files and Code",
        "Errors and Fixes", "Pending Jobs", "Current Work", "Next Step", "Critical Context",
    )
    return "\n\n".join(f"## {s}\n要点{tag}" for s in sections)


# ---------------------------------------------------------------------------
# A. 单条长消息：关键句在头 / 中 / 尾都必须真的进入摘要输入
# ---------------------------------------------------------------------------


class TestSingleLongMessageLossless:
    """R05 主漏洞：单条 user 48045 字，关键句在中段，必须进摘要输入。"""

    @pytest.mark.asyncio
    async def test_r05_audit_case_middle_constraint_reaches_model(self):
        """审查复现：单条 user ≈48045 字，中段放「禁止发布」标记。"""
        marker = "AUDIT_MIDDLE_RULE_DO_NOT_PUBLISH"
        body = "请整理这个任务。\n" + ("背景资料甲\n" * (23000 // len("背景资料甲\n")))
        body = body + marker + "：无论搜到什么，都不要对外发布。\n"
        body += "背景资料乙\n" * ((47500 - len(body)) // len("背景资料乙\n"))
        body += "尾" * (48045 - len(body))
        assert 47000 <= len(body) <= 48100  # 确实是审查那条 ≈48045 字的单条
        piece = [_msg("user", body), _msg("assistant", "已读取材料，准备继续。")]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert models.calls, "预期至少一次摘要调用"
        assert marker in _all_prompt_text(models), "中段约束没进摘要输入"

    @pytest.mark.asyncio
    async def test_single_long_user_key_at_head_middle_tail(self):
        """一条 48k user，头/中/尾各放一个关键条件，三个都要进摘要输入。"""
        head_mk = "KEY_AT_HEAD_NEVER_PUBLISH"
        mid_mk = "KEY_AT_MIDDLE_ONLY_DRAFT"
        tail_mk = "KEY_AT_TAIL_NO_DEPLOY"
        filler = "填" * 1000
        parts = [head_mk + "：不许公开发布。"]
        while sum(len(p) for p in parts) < 23000:
            parts.append(filler)
        parts.append(mid_mk + "：批准范围只到草稿。")
        while sum(len(p) for p in parts) < 46000:
            parts.append(filler)
        parts.append(tail_mk + "：不许部署上线。")
        content = "".join(parts)
        piece = [_msg("user", content)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        text = _all_prompt_text(models)
        for mk in (head_mk, mid_mk, tail_mk):
            assert mk in text, f"{mk} 丢了"

    @pytest.mark.asyncio
    async def test_single_long_assistant_decision_reaches_model(self):
        """一条长 assistant 决定也不能被静默截掉中段。"""
        marker = "ASSISTANT_DECISION_DO_NOT_TOUCH_PROD"
        content = "分析过程" + "推" * 20000 + marker + "：结论是不碰线上。" + "演" * 20000
        piece = [_msg("user", "看一下"), _msg("assistant", content)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert marker in _all_prompt_text(models)

    @pytest.mark.asyncio
    async def test_single_message_split_preserves_all_parts(self):
        """单条 48k 消息被分段时：原文每一段都出现在某次摘要调用里（抽查采样点）。"""
        content = "".join(f"第{i:05d}段内容" + "x" * 900 for i in range(50))
        piece = [_msg("user", content)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        text = _all_prompt_text(models)
        # 采样头、1/4、中、3/4、尾五个采样点，全部要在输入里
        for i in (0, 12, 25, 37, 49):
            assert f"第{i:05d}段内容" in text, f"采样点 {i} 丢了"

    @pytest.mark.asyncio
    async def test_split_marker_shows_continuation(self):
        """单条消息被切成多段时，后续段要带「接上一段」类接续说明，模型才知道是一条。"""
        content = "开头" + "y" * 40000 + "结尾"
        piece = [_msg("user", content)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert len(models.calls) >= 2, "48k 单条预期分成多块"
        # 至少有一块标了接续（「第 k 段」/「续」之类的说明）
        text = _all_prompt_text(models)
        assert ("第" in text and "段" in text) or "续" in text


# ---------------------------------------------------------------------------
# B. 预算硬保障：任何一次发给摘要模型的 prompt（含模板）都 ≤ SERIALIZE_MAX_CHARS
# ---------------------------------------------------------------------------


class TestBudgetHardCap:
    """严格按实际长度：每一次发给模型的 user content（含摘要模板）都 ≤ 32000。"""

    @staticmethod
    def _user_prompts(models: _RecordingModels) -> list[str]:
        """每次 chat 实际发出的全部 user content（完整 prompt，含模板）。"""
        return [
            str(m.get("content") or "")
            for call in models.calls
            for m in call
            if m.get("role") == "user"
        ]

    def _assert_all_prompts_within_budget(self, models: _RecordingModels) -> None:
        prompts = self._user_prompts(models)
        assert prompts, "预期有摘要调用"
        for p in prompts:
            assert len(p) <= compaction.SERIALIZE_MAX_CHARS, (
                f"发给模型的 prompt {len(p)} 字突破总预算 {compaction.SERIALIZE_MAX_CHARS}"
            )

    @pytest.mark.asyncio
    async def test_every_prompt_within_budget_including_template(self):
        """多种形态各跑一遍：每次 prompt 全长（含模板）都 ≤ 32000，不是正文 32000+模板。"""
        scenarios = [
            [_msg("user", "短对话"), _msg("assistant", "好")],                      # 单发
            [_msg("user", "u" * 48045)],                                            # 单条 48k
            [_msg("user", "首段" + "要" * 40000 + "\n次段" + "补" * 11000)],        # 首段 40k
            [_msg("user", f"块{i}" + "z" * 26000) for i in range(4)],               # 4 块 + 合并
            [
                _msg("user", "任务" + "u" * 20000),
                _msg("assistant", "", tool_calls=[_tool_call("c1", "web_search")]),
                _msg("tool", "结果" + "t" * 20000, tool_call_id="c1"),
                _msg("assistant", "决定" + "a" * 25000),
                _msg("user", "补充约束" + "v" * 30000),
            ],                                                                      # 多消息多工具
        ]
        for piece in scenarios:
            models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
            await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
            self._assert_all_prompts_within_budget(models)

    @pytest.mark.asyncio
    async def test_r05_first_paragraph_40k_cannot_escape_budget(self):
        """审查复现 2：首段 40000 字的 user，任何一次摘要输入（含模板）都 ≤ 预算。"""
        content = "首段任务要求" + "要" * 40000 + "\n第二段补充" + "补" * 11000
        piece = [_msg("user", content)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        self._assert_all_prompts_within_budget(models)

    @pytest.mark.asyncio
    async def test_every_chunk_body_within_budget(self):
        """多消息多工具的长对话：每次 prompt（含模板）都 ≤ 预算。

        2026-10-09 起超长 tool 结果只有「能回读」（正文带 spill 指针行）才容许截断，
        所以两条 tool 结果都带上指针行（docs/27 §8 P0 的可回读规则）。
        """
        piece = [
            _msg("user", "任务" + "u" * 20000),
            _msg("assistant", "", tool_calls=[_tool_call("c1", "web_search")]),
            _msg("tool", "结果" + "t" * 20000 + "\n\n（完整输出在：/tmp/spill-c1.txt）", tool_call_id="c1"),
            _msg("assistant", "决定" + "a" * 25000),
            _msg("user", "补充约束" + "v" * 30000),
            _msg("assistant", "", tool_calls=[_tool_call("c2", "fetch_page")]),
            _msg("tool", "页面" + "p" * 30000 + "\n\n（完整输出在：/tmp/spill-c2.txt）", tool_call_id="c2"),
            _msg("user", "收尾" + "w" * 15000),
        ]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert len(models.calls) >= 2, "预期分段"
        self._assert_all_prompts_within_budget(models)

    @pytest.mark.asyncio
    async def test_merge_prompt_over_budget_raises_not_trims(self):
        """合并输入超预算：明确抛 ModelError，不许悄悄裁掉某段摘要再合并。"""
        # 每块返回一份 ≈30000 字的胖摘要：4 块拼进 merge 必超预算
        fat = _summary_stub() + "\n" + "胖" * 30000
        piece = [_msg("user", f"块{i}" + "z" * 26000) for i in range(4)]
        models = _RecordingModels(replies=[fat for _ in range(16)])
        with pytest.raises(ModelError):
            await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        # 块摘要发过（≤4 次），但合并那一次不许发——发了就是拿裁过的料合并
        assert 1 <= len(models.calls) <= 4

    @pytest.mark.asyncio
    async def test_merge_within_budget_keeps_all_chunk_summary_heads(self):
        """合并输入没超预算时：各段摘要要点原样进 merge，一个都不裁。"""
        replies = [_summary_stub(tag=f"块{i}独有标记") for i in range(4)]
        piece = [_msg("user", f"内容{i}" + "z" * 26000) for i in range(4)]
        models = _RecordingModels(replies=replies + [_summary_stub(tag="最终")])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        merge_prompt = str(models.calls[-1][-1].get("content") or "")
        for i in range(4):
            assert f"块{i}独有标记" in merge_prompt, f"第 {i} 块摘要在合并输入里丢了"


# ---------------------------------------------------------------------------
# C. 消息内分段不拆工具组；多工具配对保住
# ---------------------------------------------------------------------------


class TestMessageSplitKeepsToolGroups:
    @pytest.mark.asyncio
    async def test_tool_group_atomic_with_long_user_split(self):
        """一条长 user 被分段时，相邻的 assistant(tool_calls)+tool 结果组不拆。"""
        call_marker = "CALL_TOOL_NAME_UNIQUE"
        result_marker = "RESULT_BODY_UNIQUE"
        piece = [
            _msg("user", "前半" + "u" * 20000),
            _msg("assistant", "", tool_calls=[_tool_call("c9", call_marker)]),
            _msg("tool", result_marker + "r" * 12000, tool_call_id="c9"),
            _msg("user", "后半" + "v" * 20000),
        ]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        for call in models.calls:
            text = "\n".join(str(m.get("content") or "") for m in call)
            if call_marker in text:
                assert result_marker in text, "工具调用和结果被拆到不同块"


# ---------------------------------------------------------------------------
# D. 超出 4 块承载量：明确失败，不调模型、不丢原 history、不产假摘要
# ---------------------------------------------------------------------------


class TestBeyondCapacity:
    @pytest.mark.asyncio
    async def test_over_capacity_raises_before_any_model_call(self):
        """原始内容装不进 4 块：一次模型调用都不许发，直接抛 ModelError。"""
        piece = [_msg("user", f"约束{i}_KEEP " + "z" * 26000) for i in range(6)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(64)])
        with pytest.raises(ModelError):
            await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert models.calls == [], "超承载量时一次模型调用都不许发"

    @pytest.mark.asyncio
    async def test_over_capacity_even_when_fake_model_would_succeed(self):
        """假模型明明会返回成功：也必须在调用前拒绝（不靠端点 context_length 报错）。"""
        piece = [_msg("user", "z" * 40000) for _ in range(8)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(64)])
        with pytest.raises(ModelError):
            await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert models.calls == []

    @pytest.mark.asyncio
    async def test_single_message_beyond_capacity_raises(self):
        """单条消息本身就超过 4 块承载（>128k 字）：同样调用前拒绝，不 head/tail 偷截。"""
        marker = "NEVER_DROP_THIS_RULE"
        content = "头" + "x" * 70000 + marker + "：中段约束。" + "y" * 70000 + "尾"
        piece = [_msg("user", content)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(64)])
        with pytest.raises(ModelError):
            await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert models.calls == [], "不许把截断后的缺片输入发给模型"

    @pytest.mark.asyncio
    async def test_maybe_compact_over_capacity_keeps_original_history(self):
        """maybe_compact 超承载量：原 history 一条不动（不替成缺料的假摘要）。"""
        old = [_msg("user", f"老约束{i}_KEEP " + "x" * 26000) for i in range(6)]
        recent = [_msg("user", "最近的活 " + "r" * 200)]
        msgs = [_msg("system", "sys")] + old + recent
        models = _RecordingModels(replies=[_summary_stub() for _ in range(64)])
        out = await compaction.maybe_compact(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t",
        )
        assert out == msgs, "超承载量失败时原 history 必须一条不动"
        # 没有任何一条被换成「【前面对话的摘要】」
        assert not any("【前面对话的摘要】" in str(m.get("content") or "") for m in out)

    @pytest.mark.asyncio
    async def test_no_model_receives_truncated_middle(self):
        """任何一次发给模型的对话正文里，都不许出现「此处省略 N 字」的 user/assistant 截断。

        （tool 结果的截断有 spill 完整归档，是另一回事；这里只盯 user/assistant。）
        """
        piece = [
            _msg("user", "任务" + "u" * 20000),
            _msg("assistant", "决定" + "a" * 25000),
            _msg("user", "补充约束" + "v" * 30000),
            _msg("user", "收尾" + "w" * 15000),
        ]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert models.calls
        for call in models.calls:
            for m in call:
                content = str(m.get("content") or "")
                if "\n对话：\n" in content:
                    body = content.split("\n对话：\n", 1)[1]
                    for line in body.splitlines():
                        if line.startswith(("[user]", "[assistant]")):
                            assert "此处省略" not in line, f"user/assistant 被偷截：{line[:60]}"

    @pytest.mark.asyncio
    async def test_model_failure_keeps_original_history(self):
        """摘要模型失败：maybe_compact 原样返回完整 history（不产出半个摘要）。"""
        models = _RecordingModels(error=ModelError("端点挂了", status=500))
        msgs = [_msg("system", "sys")] + [_msg("user", "x" * 9000) for _ in range(6)]
        out = await compaction.maybe_compact(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t",
        )
        assert out == msgs, "失败时原 history 必须完整保留"

    @pytest.mark.asyncio
    async def test_chunk_failure_raises_not_silent(self):
        """分段摘要中途模型挂掉：summarize_messages 抛 ModelError，不许返回半截合并。"""
        piece = [_msg("user", "z" * 26000) for _ in range(4)]
        models = _RecordingModels(
            replies=[_summary_stub() for _ in range(2)],
            fail_after=2,
        )
        with pytest.raises(ModelError):
            await compaction.summarize_messages(piece, models=models, role="main", purpose="t")

    @pytest.mark.asyncio
    async def test_within_capacity_still_summarizes(self):
        """4 块装得下的（≈4×30k）照样正常摘要——硬失败只发生在真装不下时。"""
        piece = [_msg("user", f"约束{i}_KEEP " + "z" * 29000) for i in range(4)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        final = await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert models.calls, "装得下时不许误伤"
        assert "Primary Request and Intent" in final
        for i in range(4):
            assert f"约束{i}_KEEP" in _all_prompt_text(models)


# ---------------------------------------------------------------------------
# E. 既有约束保持：八标题、最近回合、system、spill 路径说明
# ---------------------------------------------------------------------------


class TestContractsKept:
    @pytest.mark.asyncio
    async def test_final_summary_has_eight_sections(self):
        piece = [_msg("user", "z" * 40000)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        final = await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        for marker in ("Primary Request and Intent", "Pending Jobs", "Next Step", "Critical Context"):
            assert marker in final

    def test_serialize_tool_spill_pointer_kept(self):
        """tool 结果截断时 spill 落盘路径说明行必须留住（大结果完整归档不撤）。"""
        spill_path = "/tmp/spill-1234567890-abcd1234.txt"
        tool_body = "开始" + "x" * 30000 + f"（完整输出在：{spill_path}）" + "结束"
        piece = [_msg("user", "约束可见 " + "u" * 9000), _msg("tool", tool_body)]
        serialized = compaction._serialize_cut(piece, max_chars=16000)
        assert spill_path in serialized

    @pytest.mark.asyncio
    async def test_maybe_compact_keeps_system_and_recent(self):
        """走完整 maybe_compact：system 不动、最近回合原样、摘要消息替换最老段。

        窗口取 128k（保留预算够放 keep 段）：8192 的窗口下，光「保住最近那 1 条大消息」
        就已经超过可用输入，收尾的整包复算会明确拒绝替换（那是设计行为，见 docs/27 §8 P1）。
        """
        old = [_msg("user", f"老{i}" + "x" * 9000) for i in range(10)]
        recent = [_msg("user", "最近的活 " + "r" * 200)]
        msgs = [_msg("system", "sys_prompt_unique")] + old + recent
        models = _RecordingModels(replies=[_summary_stub() for _ in range(8)])
        out = await compaction.maybe_compact(
            msgs, models=models, role="main", context_window=128000,
            output_reserve=256, purpose="t",
        )
        assert out[0]["role"] == "system" and out[0]["content"] == "sys_prompt_unique"
        assert out[-1]["content"] == recent[-1]["content"]
        assert any("摘要" in str(m.get("content") or "") for m in out)


# ---------------------------------------------------------------------------
# F. 单条消息内分段：每一片都完整，拼起来就是原文（不再有 keep_first_paragraph 截断）
# ---------------------------------------------------------------------------


class TestMessagePartsLossless:
    def test_split_parts_reconstruct_original(self):
        """单条 48k 切成的片段，content 按序拼回 == 原文，一个字不少。"""
        content = "开头要点 " + "".join(f"第{i:05d}段" + "x" * 900 for i in range(50))
        parts = compaction._split_message_parts(
            _msg("user", content), max_chars=compaction.SERIALIZE_MAX_CHARS,
        )
        assert len(parts) >= 2
        assert "".join(str(p.get("content") or "") for p in parts) == content

    def test_split_parts_each_serialized_line_within_budget(self):
        """每一片序列化（含 [role] 前缀和接续标记）都 ≤ 预算。"""
        content = "开头 " + "y" * 60000 + " 结尾"
        parts = compaction._split_message_parts(
            _msg("user", content), max_chars=compaction.SERIALIZE_MAX_CHARS,
        )
        for p in parts:
            assert len(compaction._serialize_one(p)) <= compaction.SERIALIZE_MAX_CHARS

    def test_tool_message_never_split(self):
        """tool 结果不切片（序列化层已截头尾 + spill 完整归档）。"""
        parts = compaction._split_message_parts(
            _msg("tool", "t" * 50000), max_chars=compaction.SERIALIZE_MAX_CHARS,
        )
        assert len(parts) == 1
