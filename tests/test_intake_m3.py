"""intake.py M3 单元测试：@ 识别（Jev）、/mw 指令、机器人自己的消息、慢路径。

红线断言：
- 非服务群：@、/mw、关键词——零 Jev 调用、零 approvals、零 mentions、零 commands。
- 钩子里**不等 Jev**：@ 时钩子只落 pending_asks 并把判词丢后台（`_judge_at`），
  其他慢活也全部 spawn 到后台。测试里用 `_drain(spawned)` 把后台协程抽干再看结果。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signals

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
        self.ephemeral: list[dict] = []
        self.dropped: list[tuple[str, str]] = []
        self.msg_users: list[tuple[str, str, str]] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5,
            target_user_id: str = "", keywords="") -> None:
        self.items.append({"group_id": str(group_id), "text": str(text), "key": str(key),
                           "ttl_s": float(ttl_s), "target_user_id": str(target_user_id)})

    def add_ephemeral(self, group_id: str, text: str, *, key: str, ttl_s: float = 15.0,
                      turns: int = 1, target_user_id: str = "", keywords="") -> None:
        self.ephemeral.append({"group_id": str(group_id), "text": str(text), "key": str(key),
                               "target_user_id": str(target_user_id)})

    def drop_ephemeral(self, group_id: str, key: str) -> None:
        self.dropped.append((str(group_id), str(key)))

    def note_msg_user(self, group_id: str, message_id: str, user_id: str) -> None:
        self.msg_users.append((str(group_id), str(message_id), str(user_id)))


class _FakeCommands:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle(
        self, group_id: str, user_id: str, user_name: str, text: str, message_id: str, reply_to: str = ""
    ) -> None:
        self.calls.append(
            {
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "text": text,
                "message_id": message_id,
                "reply_to": reply_to,
            }
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
    """把钩子 spawn 出来的后台协程跑完（后台里再 spawn 出来的也跑，直到清空）。"""
    while spawned:
        await spawned.pop(0)


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
        # 钩子里连 Jev 都不问（判词在后台协程里），approvals 也还没被碰
        assert jev.calls == []
        assert approvals.created == []
        await _drain(spawned)
        assert len(jev.calls) == 1
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
        # 可提起清单有一句「已由 MaiWork 接手（等管理员批准），你不要自己做」
        assert len(mentions.items) == 1
        m = mentions.items[0]
        assert m["group_id"] == G1
        assert "阿柒" in m["text"]
        assert "已由 MaiWork 接手" in m["text"]
        assert "等管理员批准" in m["text"]
        assert "不要自己做" in m["text"]
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
        await _drain(spawned)
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
    async def test_hook_does_not_wait_for_slow_jev(self) -> None:
        """Jev 要 2 秒：钩子立刻返回，判词在后台跑完、按原来的结果建待批请求。"""
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)}, delay_s=2.0)
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, jev_timeout_ms=300)
        t0 = time.monotonic()
        out = await intake.handle(hook_message(is_at=True, text="帮我整理资料"))
        took = time.monotonic() - t0
        assert out == {"action": "continue"}
        assert took < 0.8, f"钩子不该等 Jev（实际 {took:.2f}s）"
        assert approvals.created == []
        await _drain(spawned)     # 后台等 Jev 的外层保险是 8 秒，2 秒的判词跑得完
        assert len(approvals.created) == 1

    @pytest.mark.asyncio
    async def test_jev_unavailable_no_wait_slow_queue(self) -> None:
        jev = _FakeJev(available=False)
        intake, signals, spawned = _make(jev=jev, approvals=_FakeApprovals())
        t0 = time.monotonic()
        await intake.handle(hook_message(is_at=True, text="帮我整理资料"))
        took = time.monotonic() - t0
        assert took < 0.3
        await _drain(spawned)   # 后台：Jev 不可用 → 慢路径
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

    @pytest.mark.asyncio
    async def test_reply_to_field_passed_through(self) -> None:
        """`/mw do` 引用了谁 → 原样交给 commands（引用正文要按这个 id 去取）。"""
        commands = _FakeCommands()
        intake, signals, spawned = _make(commands=commands)
        kwargs = hook_message(user_id="20002", text="/mw do")
        kwargs["message"]["reply_to"] = "m-quoted"
        await intake.handle(kwargs)
        await _drain(spawned)
        assert commands.calls[0]["reply_to"] == "m-quoted"

    @pytest.mark.asyncio
    async def test_reply_to_parsed_from_raw_cq_reply(self) -> None:
        """老消息只有 raw_message 里的 [CQ:reply,id=…] → 也认出来。"""
        commands = _FakeCommands()
        intake, signals, spawned = _make(commands=commands)
        kwargs = hook_message(user_id="20002", text="/mw do")
        kwargs["message"]["raw_message"] = "[CQ:reply,id=777888] [CQ:text,text=/mw do]"
        await intake.handle(kwargs)
        await _drain(spawned)
        assert commands.calls[0]["reply_to"] == "777888"


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
    from CharTyr_MaiWork.maiwork.intake import _is_command
    assert _is_command("/pic nsfw 猫")
    assert _is_command("  ！remind 明天")
    assert not _is_command("帮我整理一份清单 /pic 在后面也不算指令")


# ----------------------------------------------------------------------
# 构想引用：认「（构想 #12，要做：1、3）」（2026-10）
# ----------------------------------------------------------------------


class TestIdeaRefWithItems:
    def test_parse_wanted_forms(self) -> None:
        from CharTyr_MaiWork.maiwork.intake import parse_idea_wanted
        # 没写「要做」→ None（= 全部项目）
        assert parse_idea_wanted("") is None
        assert parse_idea_wanted("，") is None
        assert parse_idea_wanted("，谢谢") is None
        # 中英文逗号 / 顿号 / 空格 混着都认
        assert parse_idea_wanted("，要做：1、3") == [1, 3]
        assert parse_idea_wanted("，要做 1,3") == [1, 3]
        assert parse_idea_wanted(" 要做: 2 4 ") == [2, 4]
        assert parse_idea_wanted("，要做：１、３") == [1, 3]   # 全角数字
        assert parse_idea_wanted("，要做：1、1、2") == [1, 2]  # 去重
        assert parse_idea_wanted("，要做：0、2") == [2]       # 序号从 1 起
        # 写了「要做」但一个数字都没有 → None（当全部，别卡住）
        assert parse_idea_wanted("，要做：全部") is None

    @pytest.mark.asyncio
    async def test_idea_request_carries_selected_items(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'books', '做铝价表', '整理铝价', ?, 'new', 0, 0)",
                (G1, json.dumps([
                    {"kind": "task", "title": "抓铝价数据"},
                    {"kind": "goal", "title": "每周更新铝价表"},
                    {"kind": "task", "title": "发给群友"},
                ], ensure_ascii=False)),
            )
        approvals, mentions = _FakeApprovals(), _FakeMentions()
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)})
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions)
        intake._store = store
        try:
            text = "帮我做这个构想：做铝价表 / 整理铝价 /（构想 #1，要做：1、3）"
            out = await intake.handle(hook_message(is_at=True, text=text))
            assert out == {"action": "continue"}
            await _drain(spawned)
        finally:
            store.close()
        assert jev.calls == []            # 认出构想编号就不问 Jev
        assert len(approvals.created) == 1
        gid, kw = approvals.created[0]
        assert gid == G1
        assert kw["idea_id"] == 1
        assert kw["items"] == [1, 3]       # 选中的序号存进请求
        assert kw["source"] == "idea"
        assert "来自构想 #1" in kw["via"]

    @pytest.mark.asyncio
    async def test_idea_request_without_wanted_means_all(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'bulb', '做个清单', '列条目', '[]', 'new', 0, 0)",
                (G1,),
            )
        approvals = _FakeApprovals()
        intake, signals, spawned = _make(approvals=approvals, jev=_FakeJev(None))
        intake._store = store
        try:
            await intake.handle(hook_message(is_at=True, text="帮我做这个构想（构想 #1）"))
            await _drain(spawned)
        finally:
            store.close()
        assert len(approvals.created) == 1
        assert approvals.created[0][1]["items"] is None   # 没写「要做」= 全部


# ----------------------------------------------------------------------
# @ 判活期间的防抢答临时备忘（Step 2.3）
# ----------------------------------------------------------------------


class TestAtAntiPreempt:
    @pytest.mark.asyncio
    async def test_handle_at_adds_ephemeral_then_judge_drops_it(self) -> None:
        """@ 一落就塞「正在判断」临时备忘（定向本人），后台判完在 finally 里撤掉。"""
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)})
        approvals, mentions = _FakeApprovals(), _FakeMentions()
        intake, signals, spawned = _make(jev=jev, approvals=approvals, mentions=mentions)
        out = await intake.handle(
            hook_message(is_at=True, user_id="20002", nickname="阿柒", message_id="m-9", text="帮我做个表")
        )
        assert out == {"action": "continue"}
        # 钩子里就塞了防抢答备忘（key=request:at-m-9，定向说话人 20002）
        assert any(e["key"] == "request:at-m-9" for e in mentions.ephemeral)
        eph = next(e for e in mentions.ephemeral if e["key"] == "request:at-m-9")
        assert eph["target_user_id"] == "20002"
        assert "正在后台判断" in eph["text"]
        # 后台判完 → 撤掉
        await _drain(spawned)
        assert (G1, "request:at-m-9") in mentions.dropped

    @pytest.mark.asyncio
    async def test_judge_at_error_still_drops_ephemeral(self) -> None:
        """后台判词出错也照样撤（finally）——不能留一条「正在判断」卡着。"""
        jev = _FakeJev(None)  # Jev 无答案 → 走 fallback
        mentions = _FakeMentions()
        intake, signals, spawned = _make(jev=jev, mentions=mentions)
        await intake.handle(
            hook_message(is_at=True, user_id="20002", message_id="m-err", text="帮我做个表")
        )
        await _drain(spawned)
        assert (G1, "request:at-m-err") in mentions.dropped

    @pytest.mark.asyncio
    async def test_non_at_message_no_ephemeral(self) -> None:
        """普通消息不塞防抢答备忘。"""
        mentions = _FakeMentions()
        intake, signals, spawned = _make(mentions=mentions)
        await intake.handle(hook_message(text="随便聊聊", message_id="m-x"))
        assert mentions.ephemeral == []

    @pytest.mark.asyncio
    async def test_msg_user_backfilled_for_targeted_memo(self) -> None:
        """on_message 把 message_id→user_id 记进 Mentions（定向备忘认发言人用）。"""
        mentions = _FakeMentions()
        intake, signals, spawned = _make(mentions=mentions)
        await intake.handle(hook_message(user_id="20002", message_id="m-u", text="hello"))
        assert (G1, "m-u", "20002") in mentions.msg_users
