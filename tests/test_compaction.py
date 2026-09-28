"""compaction.py 的单元测试：估算 / 触发线 / 超长工具结果截断 / 摘要切分 / 超长错误 / 大结果落盘。"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import compaction


def _msg(role, content="", **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


class TestEstimate:
    def test_known_ratio(self):
        assert compaction.estimate_tokens("abcd") == 4   # ceil(4 / 1.3)
        assert compaction.estimate_tokens("abc") == 3
        assert compaction.estimate_tokens("a") == 1
        assert compaction.estimate_tokens("") == 0

    def test_messages_includes_tool_call_args(self):
        m = _msg("assistant", "hi", tool_calls=[{"function": {"name": "x", "arguments": '{"a": 1}'}}])
        assert compaction.estimate_message_tokens(m) == compaction.estimate_tokens("hi") + compaction.estimate_tokens('{"a": 1}')

    def test_whole_list(self):
        msgs = [_msg("system", "sys"), _msg("user", "hello")]
        assert compaction.estimate_tokens_in_messages(msgs) == compaction.estimate_tokens("sys") + compaction.estimate_tokens("hello")


class TestThreshold:
    def test_formula(self):
        # min(W*0.8, W - O - 65536)
        assert compaction.compact_threshold(context_window=100000, output_reserve=0) == 34464
        assert compaction.compact_threshold(context_window=128000, output_reserve=8192) == min(102400, 128000 - 8192 - 65536)


class TestTruncateBigToolOutputs:
    def test_small_untouched(self):
        msgs = [_msg("system", "s"), _msg("tool", "x" * 1000)]
        kept, n = compaction.truncate_big_tool_outputs(msgs)
        assert n == 0
        assert kept is msgs  # 不动返回原列表

    def test_big_tool_truncated_head_tail(self):
        big = "头" * 4096 + "中" * 10000 + "尾" * 2000
        msgs = [_msg("system", "s"), _msg("user", "u"), _msg("assistant", "", tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]), _msg("tool", big, tool_call_id="c1")]
        kept, n = compaction.truncate_big_tool_outputs(msgs, keep_recent_n=0)
        assert n == 1
        body = kept[3]["content"]
        assert len(body) <= 4096 + 1024 + 100
        assert body.startswith("头" * 10)
        assert body.endswith("尾" * 100)
        assert "省略" in body

    def test_keep_recent_n(self):
        big = "x" * 20000
        msgs = [_msg("tool", big), _msg("tool", big), _msg("tool", big)]
        kept, n = compaction.truncate_big_tool_outputs(msgs, keep_recent_n=1)
        assert n == 2
        # 最近 1 条 tool 结果不动，前两条被截
        assert n == 2
        assert kept[2]["content"] == big
        assert "省略" in kept[0]["content"]


class _FakeModels:
    def __init__(self, replies=None, error=None):
        self.queue = list(replies or [])
        self.error = error
        self.calls = []

    async def chat(self, role, messages, **kw):
        self.calls.append((role, [dict(m) for m in messages], kw))
        if self.error is not None:
            raise self.error
        from CharTyrMaiWork_stub import ChatResult  # noqa
        return self.queue.pop(0) if self.queue else None


class TestSplitGroups:
    def test_tool_pairs_joined(self):
        # assistant(tool_calls) + 对应 tool 结果必须成对
        msgs = [
            _msg("system", "s"),
            _msg("user", "u1"),
            _msg("assistant", "a1", tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]),
            _msg("tool", "r1", tool_call_id="c1"),
            _msg("assistant", "a2"),
        ]
        groups = compaction._split_groups(msgs)
        # system 一组、user 一组、assistant+tool 一组、assistant 一组
        assert [g[0]["role"] for g in groups] == ["system", "user", "assistant", "assistant"]
        assert len(groups[2]) == 2  # assistant + tool 一组


class TestKeepRecentAndCutIntent:
    def test_keep_recent_about_16pct(self):
        # 构造：system + 4 个早期 user + 10 个近期 user；window 使 keep 比例可见
        msgs = [_msg("system", "sys")] + [_msg("user", f"e{i}: " + "x" * 1000) for i in range(14)]
        keep, cut = compaction.pick_cut_point(msgs, context_window=8192, output_reserve=256)
        # system 永不进摘要、也永不删
        assert keep[0]["role"] == "system"
        # cut 是开头那段（不含 system）
        assert cut and all(m["role"] != "system" for m in cut)
        # keep 的大致千分比 ≈ 16% of (W-O)
        target = int((8192 - 256) * 0.16)
        kept_tokens = compaction.estimate_tokens_in_messages(keep)
        assert kept_tokens <= compaction.estimate_tokens(msgs[0]["content"]) + target + 600  # 粗略上界


class TestCompactLooped:
    @pytest.mark.asyncio
    async def test_below_threshold_calls_nothing(self):
        from CharTyr_MaiWork.maiwork.models import ChatResult

        class M:
            calls = []

            async def chat(self, *a, **k):
                raise AssertionError("不该调模型")

        msgs = [_msg("system", "s"), _msg("user", "hello")]
        out = await compaction.maybe_compact(msgs, models=M(), role="main", context_window=128000, output_reserve=8192, purpose="t")
        assert out == msgs

    @pytest.mark.asyncio
    async def test_big_tool_only_solves_without_summary(self):
        # 只有超长 tool 结果超了触发线 → 截断后不再超 → 不调摘要
        class M:
            async def chat(self, *a, **k):
                raise AssertionError("不该调模型")

        big = "x" * 30000
        msgs = [_msg("system", "s"), _msg("user", "u"), _msg("assistant", "", tool_calls=[{"id": "c", "function": {"name": "f", "arguments": "{}"}}]), _msg("tool", big, tool_call_id="c")]
        out = await compaction.maybe_compact(msgs, models=M(), role="main", context_window=8192, output_reserve=0, keep_recent_n=0)
        assert "省略" in out[3]["content"]

    @pytest.mark.asyncio
    async def test_summary_replaces_old_part_and_logged(self):
        from CharTyr_MaiWork.maiwork.models import ChatResult

        summary_text = "8 节摘要" * 100

        class M:
            def __init__(self):
                self.calls = []

            async def chat(self, role, messages, **kw):
                self.calls.append(kw)
                return ChatResult(text=summary_text, tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={})

        old = [_msg("user", f"早{i}" + "x" * 2000) for i in range(6)]
        recent = [_msg("assistant", f"新{i}" + "y" * 500) for i in range(3)]
        msgs = [_msg("system", "sys")] + old + recent
        m = M()
        out = await compaction.maybe_compact(msgs, models=m, role="main", context_window=8192, output_reserve=256, purpose="chat")
        # 摘要是一条 user 消息，带 8 个小节标题
        assert out[1]["role"] == "user"
        body = out[1]["content"]
        for marker in ("Primary Request and Intent", "Key Technical Concepts", "Files and Code",
                       "Errors and Fixes", "Pending Jobs", "Current Work", "Next Step", "Critical Context"):
            assert marker in body
        # 最近的消息原样保留
        assert out[-1]["content"] == recent[-1]["content"]
        # purpose 标 compact
        assert any(c.get("purpose", "").endswith("compact") for c in m.calls)

    @pytest.mark.asyncio
    async def test_summary_failure_keeps_original(self):
        from CharTyr_MaiWork.maiwork.models import ModelError

        class M:
            async def chat(self, *a, **k):
                raise ModelError("端点挂了", status=500)

        msgs = [_msg("system", "sys")] + [_msg("user", "x" * 2000) for _ in range(8)]
        out = await compaction.maybe_compact(msgs, models=M(), role="main", context_window=8192, output_reserve=256, purpose="t")
        assert out == msgs  # 原样继续

    @pytest.mark.asyncio
    async def test_context_length_error_trim_and_retry_once(self):
        from CharTyr_MaiWork.maiwork.models import ChatResult, ModelError

        class M:
            def __init__(self):
                self.calls = []

            async def chat(self, role, messages, **kw):
                self.calls.append([dict(x) for x in messages])
                if len(self.calls) == 1:
                    raise ModelError("context_length_exceeded prompt too long", status=400)
                return ChatResult(text='{"pass": true, "review": "过"}', tool_calls=[], model="m", prompt_tokens=1, completion_tokens=1, raw_message={})

        msgs = [_msg("system", "s")] + [_msg("user", f"第{i}" + "z" * 3000) for i in range(6)]
        m = M()
        out = await compaction.chat_with_retry_on_long_context(msgs, models=m, role="main", purpose="p")
        assert len(m.calls) == 2
        # 第二次的 messages 比第一次短（最旧一段被裁）
        assert len(m.calls[1]) < len(m.calls[0])

    @pytest.mark.asyncio
    async def test_context_length_error_twice_raises(self):
        from CharTyr_MaiWork.maiwork.models import ModelError

        class M:
            async def chat(self, *a, **k):
                raise ModelError("context_length_exceeded", status=400)

        msgs = [_msg("system", "s")] + [_msg("user", "z" * 100) for _ in range(3)]
        with pytest.raises(ModelError):
            await compaction.chat_with_retry_on_long_context(msgs, models=M(), role="main", purpose="p")

    def test_is_context_length_error(self):
        assert compaction.looks_like_context_length("maximum context length exceeded")
        assert compaction.looks_like_context_length("This model's maximum context length is 128000 tokens")
        assert compaction.looks_like_context_length("prompt is too long: 200k tokens > max")
        assert compaction.looks_like_context_length("context_length_exceeded")
        assert compaction.looks_like_context_length("超出上下文限制")
        assert not compaction.looks_like_context_length("端点返回 500")


class TestSpill:
    def test_small_result_no_spill(self, tmp_path):
        keep = compaction.spill_big_output("短输出", tmp_path)
        assert keep == "短输出"

    def test_big_result_written_to_file(self, tmp_path):
        big = "前" * 30000 + "尾" * 30000
        out = compaction.spill_big_output(big, tmp_path)
        # 对话里：开头 + 结尾 + 文件路径说明
        assert len(out) < len(big) // 2
        assert "省略" in out
        files = sorted(p for p in tmp_path.glob("*.txt") if p.is_file())
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big
        assert files[0].name in out

    def test_spill_counter_names(self, tmp_path):
        big = "x" * 60000
        a = compaction.spill_big_output(big, tmp_path)
        b = compaction.spill_big_output(big, tmp_path)
        names = sorted(p.name for p in tmp_path.glob("*.txt") if p.is_file())
        assert len(names) == 2
        assert a != b


class TestRepeatNudger:
    def test_third_fifth_eighth(self):
        n = compaction.RepeatCallNudger()
        outs = [n.nudge("web_search", {"query": "same"}) for _ in range(8)]
        assert outs[0] == "" and outs[1] == ""
        assert "第 3 次" in outs[2]
        assert outs[3] == ""
        assert "第 5 次" in outs[4]
        assert outs[5] == "" and outs[6] == ""
        assert "第 8 次" in outs[7]

    def test_different_args_not_repeat(self):
        n = compaction.RepeatCallNudger()
        n.nudge("web_search", {"query": "a"})
        n.nudge("web_search", {"query": "a"})
        assert n.nudge("web_search", {"query": "b"}) == ""

    def test_args_normalization(self):
        n = compaction.RepeatCallNudger()
        n.nudge("web_search", {"query": "a", "limit": 3})
        n.nudge("web_search", {"limit": 3, "query": "a"})  # 顺序不同算同一次
        assert "第 3 次" in n.nudge("web_search", {"query": "a", "limit": 3})

    def test_user_message_resets(self):
        n = compaction.RepeatCallNudger()
        n.nudge("web_search", {"query": "a"})
        n.nudge("web_search", {"query": "a"})
        n.note_user_message()
        assert n.nudge("web_search", {"query": "a"}) == ""
