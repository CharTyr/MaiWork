"""A07 验收测试：省上下文时，重要的中段约束不能在摘要前消失。

审查复现用例：docs/audits/0.7.0/harness/test_backend_audit.py 的
test_compaction_omits_middle_decision_before_summary（20000 字 user +
「AUDIT_MIDDLE_RULE_DO_NOT_PUBLISH」开头的 15000 字 user + 12000 字 tool）。

改法要点（docs/13-0.7.0整体审查.md A07）：
1. 序列化按重要性分配预算：user / assistant 决定性文字优先；超长 tool 各自截头尾；
   不再整体 head/tail 一刀切。
2. 优先内容仍超预算：分段摘要（按 _split_groups 成组切块，工具调用与结果不拆散），
   逐块摘要再合并成最终 8 节；分段数有上限。
3. 摘要提示明确要求约束 / 禁止事项 / 批准范围 / 未完成事项原样列出。
4. 失败语义不变：maybe_compact 摘要失败原样返回不抛；summarize_messages 失败抛 ModelError。
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
    """假 models：记录每次 chat 的 messages，按队列返回摘要文本。"""

    def __init__(self, replies=None, error=None):
        self.queue = list(replies or [])
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        text = self.queue.pop(0) if self.queue else "摘要"
        return ChatResult(text=text, tool_calls=[], model="m",
                          prompt_tokens=1, completion_tokens=1, raw_message={})


def _all_prompt_text(models: _RecordingModels) -> str:
    """所有被记录的 chat 调用里，user 消息文本拼一起（分段时每块一次调用）。"""
    return "\n".join(
        str(m.get("content") or "")
        for call in models.calls
        for m in call
    )


def _summary_stub(section_marker: str = "") -> str:
    """一份带 8 节标题的假摘要（合并提示要求 8 节齐全）。"""
    sections = (
        "Primary Request and Intent", "Key Technical Concepts", "Files and Code",
        "Errors and Fixes", "Pending Jobs", "Current Work", "Next Step", "Critical Context",
    )
    body = "\n\n".join(f"## {s}\n要点{section_marker}" for s in sections)
    return body


# ---------------------------------------------------------------------------
# 1. 序列化层：约束放在头 / 中 / 尾，都必须出现在送给摘要模型的输入里
# ---------------------------------------------------------------------------


class TestSerializeKeepsConstraints:
    """_serialize_cut 不能再整体 head/tail 一刀切把中段 user 约束切掉。"""

    def test_audit_case_middle_rule_kept(self):
        """审查复现用例：20000 字 user + 15000 字带标记 user + 12000 字 tool。"""
        marker = "AUDIT_MIDDLE_RULE_DO_NOT_PUBLISH"
        piece = [
            _msg("user", "A" * 20000),
            _msg("user", marker + "：无论搜到什么，都不要对外发布。" + "B" * 15000),
            _msg("tool", "C" * 12000),
        ]
        serialized = compaction._serialize_cut(piece)
        assert marker in serialized

    def test_constraint_at_head_kept(self):
        marker = "HEAD_RULE_KEEP_ME"
        piece = [
            _msg("user", marker + "：任务范围只限本群。" + "H" * 18000),
            _msg("user", "M" * 15000),
            _msg("tool", "T" * 12000),
        ]
        serialized = compaction._serialize_cut(piece)
        assert marker in serialized

    def test_constraint_at_tail_kept(self):
        marker = "TAIL_RULE_KEEP_ME"
        piece = [
            _msg("user", "H" * 18000),
            _msg("tool", "T" * 12000),
            _msg("user", marker + "：批准范围只到草稿。" + "T" * 9000),
        ]
        serialized = compaction._serialize_cut(piece)
        assert marker in serialized

    def test_assistant_decision_kept(self):
        """assistant 的决定性文字也是优先内容，不能被中段一刀切。"""
        marker = "ASSISTANT_DECISION_KEEP_ME"
        piece = [
            _msg("user", "U" * 18000),
            _msg("assistant", marker + "：我决定采用方案 B，不碰线上。" + "D" * 12000),
            _msg("tool", "T" * 14000),
        ]
        serialized = compaction._serialize_cut(piece)
        assert marker in serialized

    def test_no_global_head_tail_on_whole_text(self):
        """整体一刀切会把中段 user 消息截没；新实现里每条 user 的开头都要保住。"""
        head_marker = "FIRST_USER_OPENING"
        mid_marker = "SECOND_USER_OPENING"
        piece = [
            _msg("user", head_marker + " " + "a" * 15000),
            _msg("user", mid_marker + " " + "b" * 15000),
            _msg("tool", "c" * 15000),
        ]
        serialized = compaction._serialize_cut(piece)
        assert head_marker in serialized
        assert mid_marker in serialized

    def test_long_tool_truncated_individually_with_omission_note(self):
        """超长 tool 结果各自截头尾（保留 spill 路径说明），而不是挤掉别的消息。"""
        spill_path = "/tmp/spill-1234567890-abcd1234.txt"
        tool_body = "开始" + "x" * 30000 + f"（完整输出在：{spill_path}）" + "结束"
        piece = [
            _msg("user", "USER_CONSTRAINT_VISIBLE " + "u" * 9000),
            _msg("tool", tool_body),
            _msg("user", "u2"),
        ]
        serialized = compaction._serialize_cut(piece, max_chars=16000)
        assert "USER_CONSTRAINT_VISIBLE" in serialized
        # tool 被单独截断：有省略说明，且 spill 路径说明保留
        assert "省略" in serialized
        assert spill_path in serialized

    def test_short_piece_untouched(self):
        """不超预算的短对话原样序列化（行为不变）。"""
        piece = [_msg("user", "你好"), _msg("assistant", "在的")]
        serialized = compaction._serialize_cut(piece)
        assert "你好" in serialized and "在的" in serialized


# ---------------------------------------------------------------------------
# 2. 摘要提示：必须要求约束 / 禁止事项 / 批准范围 / 未完成事项原样列出
# ---------------------------------------------------------------------------


class TestSummaryPrompt:
    @pytest.mark.asyncio
    async def test_prompt_demands_constraints_verbatim(self):
        models = _RecordingModels(replies=[_summary_stub()])
        piece = [_msg("user", "做个资讯"), _msg("assistant", "好")]
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        prompt = models.calls[0][0]["content"]
        # 提示里要明确要求：约束、禁止事项、批准范围、未完成事项原样列出
        assert "约束" in prompt
        assert "禁止" in prompt
        assert "批准" in prompt
        assert ("未完成" in prompt) or ("Pending" in prompt)
        assert "原样" in prompt


# ---------------------------------------------------------------------------
# 3. 分段摘要：优先内容本身超预算时按组切块；工具调用与结果不拆散；块数有上限
# ---------------------------------------------------------------------------


class TestChunkedSummarization:
    @pytest.mark.asyncio
    async def test_middle_constraint_survives_chunking(self):
        """分段场景：约束在中段的 user 里，所有送给模型的输入里至少一处含约束。"""
        marker = "MIDDLE_CONSTRAINT_NEVER_PUBLISH"
        # 构造：约束消息本身很长（优先内容本身就超预算），逼出分段路径
        piece = [
            _msg("user", "W" * 20000),
            _msg("assistant", "", tool_calls=[_tool_call("c1", "web_search")]),
            _msg("tool", "R" * 15000, tool_call_id="c1"),
            _msg("user", marker + "：这条规则管全程。" + "X" * 20000),
            _msg("assistant", "A" * 15000),
            _msg("user", "V" * 18000),
        ]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert len(models.calls) >= 1
        assert marker in _all_prompt_text(models)

    @pytest.mark.asyncio
    async def test_tool_call_and_result_never_split(self):
        """切块时 assistant(tool_calls) 和它的 tool 结果必须在同一块的输入里。"""
        call_marker = "CALL_MARKER_UNIQUE"
        result_marker = "RESULT_MARKER_UNIQUE"
        big = "y" * 14000
        piece = [
            _msg("user", "U" * 20000),
            _msg("assistant", "", tool_calls=[_tool_call("c9", call_marker)]),
            _msg("tool", result_marker + big, tool_call_id="c9"),
            _msg("user", "V" * 20000),
            _msg("user", "W" * 20000),
        ]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(16)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        # 每一次调用（每一块）里：出现了调用名就必须同时出现结果标记
        for call in models.calls:
            text = "\n".join(str(m.get("content") or "") for m in call)
            if call_marker in text:
                assert result_marker in text, "工具调用和结果被拆到了不同块"

    @pytest.mark.asyncio
    async def test_chunk_count_capped(self):
        """分段数有上限（≤4）：超长内容再多，块摘要调用次数不能无限涨。"""
        piece = [_msg("user", f"块{i}" + "z" * 25000) for i in range(10)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(64)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        # 块摘要调用 ≤ 4 次 + 1 次合并 = 最多 5 次
        assert len(models.calls) <= 5

    @pytest.mark.asyncio
    async def test_capped_chunks_still_keep_user_openings(self):
        """块数到顶后对单条做头尾截断时：每条 user 开头至少留一段，且写明省略字数。"""
        markers = [f"USER_OPENING_{i}" for i in range(10)]
        piece = [_msg("user", markers[i] + " " + "z" * 25000) for i in range(10)]
        models = _RecordingModels(replies=[_summary_stub() for _ in range(64)])
        await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        text = _all_prompt_text(models)
        for mk in markers:
            assert mk in text, f"user 消息开头 {mk} 丢了"
        assert "省略" in text  # 明确写了「此处省略 N 字」之类的说明

    @pytest.mark.asyncio
    async def test_chunk_summaries_merged_into_eight_sections(self):
        """多块摘要后还有一次合并调用，产出最终 8 节摘要。"""
        piece = [_msg("user", f"段{i}" + "q" * 26000) for i in range(4)]
        models = _RecordingModels(replies=[_summary_stub(f"块{i}") for i in range(16)])
        final = await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert len(models.calls) >= 2  # 至少一次块摘要 + 一次合并
        # 最终返回文本带 8 节标题
        for marker in ("Primary Request and Intent", "Pending Jobs", "Next Step"):
            assert marker in final


# ---------------------------------------------------------------------------
# 4. 行为不变：普通短对话只调一次模型；失败语义不变
# ---------------------------------------------------------------------------


class TestUnchangedBehavior:
    @pytest.mark.asyncio
    async def test_short_conversation_single_model_call(self):
        """普通短对话：一次模型调用出 8 节摘要（不分段、不合并）。"""
        piece = [_msg("user", "帮我查一下 MaiBot 的文档"), _msg("assistant", "好，我看看")]
        models = _RecordingModels(replies=[_summary_stub()])
        final = await compaction.summarize_messages(piece, models=models, role="main", purpose="t")
        assert len(models.calls) == 1
        assert "Primary Request and Intent" in final

    @pytest.mark.asyncio
    async def test_summarize_messages_failure_raises_model_error(self):
        """公开版失败语义：抛 ModelError。"""
        models = _RecordingModels(error=ModelError("端点挂了", status=500))
        with pytest.raises(ModelError):
            await compaction.summarize_messages(
                [_msg("user", "x" * 30000) for _ in range(4)],
                models=models, role="main", purpose="t",
            )

    @pytest.mark.asyncio
    async def test_maybe_compact_failure_returns_original(self):
        """maybe_compact 摘要失败：原样返回，不抛。"""
        models = _RecordingModels(error=ModelError("端点挂了", status=500))
        msgs = [_msg("system", "sys")] + [_msg("user", "x" * 2000) for _ in range(8)]
        out = await compaction.maybe_compact(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t",
        )
        assert out == msgs

    @pytest.mark.asyncio
    async def test_maybe_compact_middle_constraint_in_summary_input(self):
        """走完整 maybe_compact：中段约束必须出现在送给摘要模型的输入里。"""
        marker = "COMPACT_MIDDLE_RULE"
        # 体量要足够大、老段足够多，估算才会过触发线（threshold = max(8192, min(6553, …))）
        old = [
            _msg("user", "A" * 9000),
            _msg("user", marker + "：不要发布。" + "B" * 9000),
            _msg("assistant", "", tool_calls=[_tool_call("c1", "web_search")]),
            _msg("tool", "C" * 9000, tool_call_id="c1"),
            _msg("assistant", "D" * 4000),
        ]
        recent = [_msg("user", "最近的活 " + "r" * 200)]
        msgs = [_msg("system", "sys")] + old + recent
        models = _RecordingModels(replies=[_summary_stub() for _ in range(8)])
        out = await compaction.maybe_compact(
            msgs, models=models, role="main", context_window=8192,
            output_reserve=256, purpose="t",
        )
        # 确实触发了摘要（有调用记录）
        assert models.calls, "预期触发摘要"
        assert marker in _all_prompt_text(models)
        # 摘要消息替换最老一段，recent 原样保留
        assert out[-1]["content"] == recent[-1]["content"]
