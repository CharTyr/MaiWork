"""intake.py M3 单元测试：@ 识别（Jev）、/mw 指令、机器人自己的消息、慢路径。

红线断言：
- 非服务群：@、/mw、关键词——零 Jev 调用、零 approvals、零 mentions、零 commands。
- 钩子里唯一的慢事是等 Jev（≤ [jev] timeout_ms）；其他全部 spawn 到后台。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fakes import hook_message

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.intake import Intake, Signals

G1 = "900000001"


def _settings(serve=(G1,), *, jev_timeout_ms: int = 1500):
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
            "jev": {"timeout_ms": jev_timeout_ms},
        }
    )
    return settings


class _FakeJev:
    """假的 Jev：available 可控；ask 记录调用、可预设答案、可拖时间。"""

    def __init__(self, answers=None, *, delay_s: float = 0.0, available: bool = True) -> None:
        self.answers = answers
        self.delay_s = delay_s
        self._available = available
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    async def ask(self, state, questions, *, purpose: str, group_id: str = "", timeout_ms=None):
        self.calls.append(
            {"state": state, "questions": questions, "purpose": purpose, "group_id": group_id, "timeout_ms": timeout_ms}
        )
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return self.answers


class _FakeApprovals:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create(self, group_id: str, **kw):
        self.created.append((str(group_id), dict(kw)))
        return {"id": "R-1", "status": "pending", "auto": False, "task_id": None, "goal_id": None}


class _FakeMentions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5) -> None:
        self.items.append({"group_id": str(group_id), "text": str(text), "key": str(key), "ttl_s": float(ttl_s)})


class _FakeCommands:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle(self, group_id: str, user_id: str, user_name: str, text: str, message_id: str) -> None:
        self.calls.append(
            {"group_id": group_id, "user_id": user_id, "user_name": user_name, "text": text, "message_id": message_id}
        )


def _make(
    *,
    jev=None,
    approvals=None,
    mentions=None,
    commands=None,
    bot_qq="",
    on_reminder=None,
    jev_timeout_ms: int = 1500,
):
    """装好 intake；spawn 捕获协程进 spawned（钩子只登记，测试手动跑）。"""
    settings = _settings(jev_timeout_ms=jev_timeout_ms)
    signals = Signals()
    spawned: list = []
    intake = Intake(
        lambda: settings,
        signals,
        jev=jev,
        approvals=approvals,
        mentions=mentions,
        commands=commands,
        bot_qq=bot_qq,
        spawn=lambda coro: spawned.append(coro),
        on_reminder=on_reminder,
    )
    return intake, signals, spawned


async def _drain(spawned: list) -> None:
    """把钩子 spawn 出来的后台协程跑完。"""
    for coro in list(spawned):
        await coro


# ----------------------------------------------------------------------
# 非服务群：零 Jev 零调用
# ----------------------------------------------------------------------


class TestM3NonServedGroup:
    @pytest.mark.asyncio
    async def test_at_message_zero_calls(self) -> None:
        jev, approvals, mentions, commands = _FakeJev(), _FakeApprovals(), _FakeMentions(), _FakeCommands()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions, commands=commands)
        out = await intake.handle(hook_message(group_id="999999", is_at=True, text="@她 帮我整理一下资料"))
        assert out == {"action": "continue"}
        assert jev.calls == []
        assert approvals.created == []
        assert mentions.items == []
        assert commands.calls == []
        assert spawned == []
        assert signals.take() == {}

    @pytest.mark.asyncio
    async def test_mw_command_zero_calls(self) -> None:
        jev, approvals, mentions, commands = _FakeJev(), _FakeApprovals(), _FakeMentions(), _FakeCommands()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions, commands=commands)
        out = await intake.handle(hook_message(group_id="999999", text="/mw"))
        assert out == {"action": "continue"}
        assert jev.calls == []
        assert commands.calls == []
        assert mentions.items == []
        assert spawned == []
        assert signals.take() == {}

    @pytest.mark.asyncio
    async def test_keyword_message_zero_calls(self) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.99, 0.99)})
        approvals, mentions = _FakeApprovals(), _FakeMentions()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions)
        out = await intake.handle(hook_message(group_id="999999", text="谁帮我整理一下"))
        assert out == {"action": "continue"}
        assert jev.calls == []
        assert approvals.created == []
        assert spawned == []


# ----------------------------------------------------------------------
# 服务群 @ 识别
# ----------------------------------------------------------------------


class TestAtRecognition:
    @pytest.mark.asyncio
    async def test_at_other_plugin_command_skips_jev_and_approvals(self) -> None:
        """线上实测：`@MaiBot /pic nsfw …` 被算成了 MaiWork 的活。别的插件的指令不问 Jev、不建请求。"""
        jev = _FakeJev({"kind": ("prepare", 0.99, 0.99)})
        approvals, mentions = _FakeApprovals(), _FakeMentions()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions)
        out = await intake.handle(hook_message(is_at=True, message_id="m-p", text="/pic nsfw 画个东雪莲"))
        assert out == {"action": "continue"}
        await _drain(spawned)
        assert jev.calls == []
        assert approvals.created == []
        assert intake.slow_queue == []

    @pytest.mark.asyncio
    async def test_prepare_creates_approval_in_background(self) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.91, 0.86)})
        approvals, mentions = _FakeApprovals(), _FakeMentions()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions)
        out = await intake.handle(
            hook_message(is_at=True, user_id="20002", nickname="阿柒", message_id="m-7", text="帮我整理一份铝坨坨的资料")
        )
        assert out == {"action": "continue"}
        # 钩子里只问 Jev：approvals 还没被碰（在后台协程里）
        assert len(jev.calls) == 1
        assert approvals.created == []
        await _drain(spawned)
        assert len(approvals.created) == 1
        gid, kw = approvals.created[0]
        assert gid == G1
        assert kw["kind"] == "task"
        assert kw["title"] == "帮我整理一份铝坨坨的资料"
        assert kw["quote"] == "帮我整理一份铝坨坨的资料"
        assert kw["requester_id"] == "20002"
        assert kw["requester_name"] == "阿柒"
        assert kw["message_id"] == "m-7"
        assert "把握 0.86" in kw["via"]
        assert "Jev" in kw["via"]
        # 可提起清单有一句「已记下，等管理员批准」
        assert len(mentions.items) == 1
        m = mentions.items[0]
        assert m["group_id"] == G1
        assert "阿柒" in m["text"]
        assert "MaiWork 已经记下" in m["text"]
        assert m["ttl_s"] == 30 * 60

    @pytest.mark.asyncio
    async def test_goal_kind_creates_goal_request(self) -> None:
        jev = _FakeJev({"kind": ("goal", 0.8, 0.77)})
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=_FakeMentions())
        await intake.handle(hook_message(is_at=True, text="帮我盯着这个事儿"))
        await _drain(spawned)
        assert approvals.created[0][1]["kind"] == "goal"
        assert approvals.created[0][1]["title"]

    @pytest.mark.asyncio
    async def test_state_text_truncated_to_500(self) -> None:
        jev = _FakeJev({"kind": ("none", 0.99, 0.99)})
        intake, signals, spawned = _make(jev=jev)
        long_text = "长" * 800
        await intake.handle(hook_message(is_at=True, text=long_text))
        state_text = jev.calls[0]["state"]["messages"][0]["text"]
        assert len(state_text) == 500
        assert jev.calls[0]["state"]["messages"][0]["speaker"] == "USER"

    @pytest.mark.asyncio
    async def test_low_confidence_prepare_goes_slow_queue(self) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.7, 0.4)})
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(jev=jev, approvals=approvals)
        await intake.handle(hook_message(is_at=True, text="也许帮我看看"))
        await _drain(spawned)
        assert approvals.created == []
        assert len(intake.slow_queue) == 1

    @pytest.mark.asyncio
    async def test_none_kind_confident_records_nothing(self) -> None:
        # Jev 明确判 none 且把握 ≥0.6：正常闲聊，不进慢路径、什么都不记
        jev = _FakeJev({"kind": ("none", 0.95, 0.9)})
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(jev=jev, approvals=approvals)
        await intake.handle(hook_message(is_at=True, text="哈哈哈你说得对"))
        await _drain(spawned)
        assert approvals.created == []
        assert intake.slow_queue == []

    @pytest.mark.asyncio
    async def test_none_kind_low_confidence_goes_slow_queue(self) -> None:
        # 判 none 但把握不够：只是闲聊的把握不足，进慢路径等主模型读群时再判
        jev = _FakeJev({"kind": ("none", 0.55, 0.4)})
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(jev=jev, approvals=approvals)
        await intake.handle(hook_message(is_at=True, text="也许吧哈哈"))
        await _drain(spawned)
        assert approvals.created == []
        assert len(intake.slow_queue) == 1

    @pytest.mark.asyncio
    async def test_reminder_invokes_callback(self) -> None:
        jev = _FakeJev({"kind": ("reminder", 0.9, 0.88)})
        captured: list[tuple[str, dict]] = []

        async def _on_reminder(group_id: str, msg: dict) -> None:
            captured.append((group_id, msg))

        intake, signals, spawned = _make(jev=jev, on_reminder=_on_reminder)
        await intake.handle(
            hook_message(is_at=True, user_id="20002", nickname="阿柒", message_id="m-9", text="明天下午三点提醒我吃药")
        )
        await _drain(spawned)
        assert len(captured) == 1
        gid, msg = captured[0]
        assert gid == G1
        assert msg["user_id"] == "20002"
        assert msg["text"] == "明天下午三点提醒我吃药"
        assert msg["message_id"] == "m-9"

    @pytest.mark.asyncio
    async def test_plain_message_no_jev(self) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.99, 0.99)})
        intake, signals, spawned = _make(jev=jev, approvals=_FakeApprovals())
        out = await intake.handle(hook_message(is_at=False, text="谁帮我整理一下"))
        assert out == {"action": "continue"}
        assert jev.calls == []
        assert spawned == []
        # 普通消息照样记信号
        assert signals.last_ts(G1) > 0


# ----------------------------------------------------------------------
# Jev 超时 / 不可用：钩子按时返回，慢路径只记队列
# ----------------------------------------------------------------------


class TestJevTimeout:
    @pytest.mark.asyncio
    async def test_hook_returns_within_timeout_budget(self) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)}, delay_s=2.0)
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, jev_timeout_ms=300)
        t0 = time.monotonic()
        out = await intake.handle(hook_message(is_at=True, text="帮我整理资料"))
        took = time.monotonic() - t0
        assert out == {"action": "continue"}
        assert took < 0.8, f"钩子不该等 Jev 超时太久（实际 {took:.2f}s）"
        assert approvals.created == []
        assert len(intake.slow_queue) == 1

    @pytest.mark.asyncio
    async def test_jev_unavailable_no_wait_slow_queue(self) -> None:
        jev = _FakeJev(available=False)
        intake, signals, spawned = _make(jev=jev, approvals=_FakeApprovals())
        t0 = time.monotonic()
        await intake.handle(hook_message(is_at=True, text="帮我整理资料"))
        took = time.monotonic() - t0
        assert took < 0.3
        assert jev.calls == []  # 不可用根本不问
        assert len(intake.slow_queue) == 1


# ----------------------------------------------------------------------
# 机器人自己的消息：只记信号，不当请求
# ----------------------------------------------------------------------


class TestBotOwnMessage:
    @pytest.mark.asyncio
    async def test_bot_own_at_not_a_request(self) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.99, 0.99)})
        approvals, mentions, commands = _FakeApprovals(), _FakeMentions(), _FakeCommands()
        intake, signals, spawned = _make(
            jev=jev, approvals=approvals, mentions=mentions, commands=commands, bot_qq="424242"
        )
        out = await intake.handle(
            hook_message(is_at=True, user_id="424242", message_id="m-bot", ts=1790000001.0)
        )
        assert out == {"action": "continue"}
        assert jev.calls == []
        assert approvals.created == []
        assert commands.calls == []
        assert spawned == []
        # 信号照样记
        assert signals.last_ts(G1) == 1790000001.0


# ----------------------------------------------------------------------
# /mw 指令：交 commands（后台），钩子立刻返回
# ----------------------------------------------------------------------


class TestMwCommand:
    @pytest.mark.asyncio
    async def test_command_routed_to_commands_and_mention_added(self) -> None:
        commands, mentions = _FakeCommands(), _FakeMentions()
        intake, signals, spawned = _make(commands=commands, mentions=mentions)
        out = await intake.handle(
            hook_message(user_id="20002", nickname="阿柒", message_id="m-cmd", text="/mw 批准 R-1")
        )
        assert out == {"action": "continue"}
        # commands 在后台协程里，钩子里还没跑
        assert commands.calls == []
        # 但 mentions 同步记了一句（说明已处理，MaiBot 不用回）
        assert len(mentions.items) == 1
        assert "指令" in mentions.items[0]["text"]
        assert mentions.items[0]["ttl_s"] == 120
        await _drain(spawned)
        assert len(commands.calls) == 1
        call = commands.calls[0]
        assert call["group_id"] == G1
        assert call["user_id"] == "20002"
        assert call["text"] == "/mw 批准 R-1"
        assert call["message_id"] == "m-cmd"

    @pytest.mark.asyncio
    async def test_mw_like_word_is_not_a_command(self) -> None:
        commands = _FakeCommands()
        jev = _FakeJev({"kind": ("none", 0.9, 0.9)})
        intake, signals, spawned = _make(commands=commands, jev=jev)
        await intake.handle(hook_message(is_at=True, text="/mwo 不是指令"))
        assert commands.calls == []

    @pytest.mark.asyncio
    async def test_command_without_commands_dependency_is_safe(self) -> None:
        intake, signals, spawned = _make(commands=None, mentions=_FakeMentions())
        out = await intake.handle(hook_message(text="/mw"))
        assert out == {"action": "continue"}
        await _drain(spawned)  # 不炸


# ----------------------------------------------------------------------
# 异常兜底：依赖炸了钩子和后台协程都不许炸出声
# ----------------------------------------------------------------------


class TestM3Resilience:
    @pytest.mark.asyncio
    async def test_approvals_exception_swallowed_in_background(self) -> None:
        class _BoomApprovals:
            def create(self, *a, **kw):
                raise RuntimeError("炸")

        jev = _FakeJev({"kind": ("task", 0.9, 0.9)})  # 故意给表外的标签
        intake, signals, spawned = _make(
            jev=_FakeJev({"kind": ("prepare", 0.9, 0.9)}), approvals=_BoomApprovals()
        )
        out = await intake.handle(hook_message(is_at=True, text="帮我整理"))
        assert out == {"action": "continue"}
        await _drain(spawned)  # 后台异常被吞，不往外抛

    @pytest.mark.asyncio
    async def test_unexpected_jev_answer_shape_goes_slow(self) -> None:
        jev = _FakeJev({"kind": "not-a-tuple"})
        intake, signals, spawned = _make(jev=jev, approvals=_FakeApprovals())
        await intake.handle(hook_message(is_at=True, text="帮我整理"))
        await _drain(spawned)
        assert intake.slow_queue, "答案形状不对也要走慢路径"



@pytest.mark.asyncio
async def test_at_with_other_plugin_command_is_ignored() -> None:
    """线上实测：`@MaiBot /pic …` 是画图插件的指令，不问 Jev、不进慢路径、不建请求。"""
    from CharTyr_MaiWork.intake import _is_command
    assert _is_command("/pic nsfw 猫")
    assert _is_command("  ！remind 明天")
    assert not _is_command("帮我整理一份清单 /pic 在后面也不算指令")
