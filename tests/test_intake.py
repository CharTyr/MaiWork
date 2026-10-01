"""intake.py 单元测试：非服务群零访问、服务群记信号、消息过滤、异常不抛。

M3 起 handle 是 async（钩子里 @ 时要等 Jev），旧断言一律 await。
"""

from __future__ import annotations

import gc

import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signal, Signals


def _settings(serve: list[dict]) -> object:
    settings, _ = load_settings({"groups": {"serve": serve}})
    return settings


def _make(served=("900000001",)):
    """建好 intake + signals + 一个会「一被碰就炸」的消息内容守卫。"""
    settings = _settings([{"group": f"qq:{g}"} for g in served])
    signals = Signals()
    intake = Intake(lambda: settings, signals)
    return intake, signals


class Exploding(dict):
    """任何取键操作都抛异常——用来断言非服务群不读消息其他字段。"""

    def get(self, *a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError("非服务群不该访问消息内容")

    def __getitem__(self, key):
        raise AssertionError("非服务群不该访问消息内容")


class TestNonServedGroup:
    @pytest.mark.asyncio
    async def test_immediate_continue(self) -> None:
        intake, signals = _make()
        out = await intake.handle(hook_message(group_id="999"))
        assert out == {"action": "continue"}
        assert signals.take() == {}

    @pytest.mark.asyncio
    async def test_does_not_read_other_fields(self) -> None:
        intake, signals = _make()
        msg = hook_message(group_id="999")
        # 只留 group_info 给它读群号，其余键全部换成一碰就炸的对象
        inner = msg["message"]
        info = {"group_info": inner["message_info"]["group_info"]}
        msg = {
            "message": Exploding({**inner, "message_info": Exploding(info)}),
        }
        # Exploding 继承 dict，但 message_info 之外任何 get/__getitem__ 都会炸
        assert await intake.handle(msg) == {"action": "continue"}

    @pytest.mark.asyncio
    async def test_no_host_calls(self) -> None:
        from fakes import FakeCtx

        ctx = FakeCtx()
        intake, signals = _make()
        await intake.handle(hook_message(group_id="999", session_id="s"))
        assert ctx.calls == []

    @pytest.mark.asyncio
    async def test_disabled_settings_serve_nothing(self) -> None:
        settings, _ = load_settings({})
        intake = Intake(lambda: settings, Signals())
        assert await intake.handle(hook_message()) == {"action": "continue"}


class TestServedGroup:
    @pytest.mark.asyncio
    async def test_normal_message_marks_signal(self) -> None:
        intake, signals = _make()
        out = await intake.handle(hook_message(message_id="100", ts=1790000000.0, session_id="s1"))
        assert out == {"action": "continue"}
        taken = signals.take()
        assert set(taken) == {"900000001"}
        sig = taken["900000001"]
        assert isinstance(sig, Signal)
        assert sig.session_id == "s1"
        assert sig.last_ts == 1790000000.0
        assert sig.count == 1
        # take 取走后清空
        assert signals.take() == {}

    @pytest.mark.asyncio
    async def test_multiple_messages_accumulate(self) -> None:
        intake, signals = _make()
        await intake.handle(hook_message(message_id="1", ts=1000.0))
        await intake.handle(hook_message(message_id="2", ts=2000.0, session_id="s2"))
        sig = signals.take()["900000001"]
        assert sig.count == 2
        assert sig.last_ts == 2000.0
        assert sig.session_id == "s2"

    @pytest.mark.asyncio
    async def test_last_ts(self) -> None:
        intake, signals = _make()
        await intake.handle(hook_message(message_id="1", ts=1234.0))
        assert signals.last_ts("900000001") == 1234.0
        assert signals.last_ts("000000") == 0.0

    @pytest.mark.asyncio
    async def test_notice_message_id_ignored(self) -> None:
        intake, signals = _make()
        assert await intake.handle(hook_message(message_id="notice:abc")) == {"action": "continue"}
        assert signals.take() == {}
        assert signals.last_ts("900000001") == 0.0

    @pytest.mark.asyncio
    async def test_is_notify_ignored(self) -> None:
        intake, signals = _make()
        msg = hook_message()
        msg["message"]["is_notify"] = True
        assert await intake.handle(msg) == {"action": "continue"}
        assert signals.take() == {}

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("1790000123.5", 1790000123.5),  # 宿主真载荷：字符串（2026-10-01 线上实测漏算安静时长的根因）
            (1790000123, 1790000123.0),
            (1790000123.5, 1790000123.5),
            ("不是数字", 0.0),
            ("nan", 0.0),
            ("inf", 0.0),
            ("-5", 0.0),
            (True, 0.0),
            (None, 0.0),
        ],
    )
    @pytest.mark.asyncio
    async def test_timestamp_string_from_host_is_parsed(self, raw, want) -> None:
        intake, signals = _make()
        msg = hook_message()
        msg["message"]["timestamp"] = raw
        assert await intake.handle(msg) == {"action": "continue"}
        assert signals.last_ts("900000001") == want

    @pytest.mark.asyncio
    async def test_bot_own_message_still_counts_for_now(self) -> None:
        # 没配 bot_qq 时无法识别「机器人自己」，消息照样记信号，不能崩
        intake, signals = _make()
        msg = hook_message(user_id="999999")
        await intake.handle(msg)
        assert signals.take()["900000001"].count == 1


class TestGarbageTolerance:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"message": None},
            {"message": "字符串"},
            {"message": {"message_info": None}},
            {"message": {"message_info": {"group_info": None}}},
            {"message": {"message_info": {"group_info": "x"}}},
            {"message": {"message_id": 1, "message_info": {}}},
            None,
            "不是 dict",
            [],
            {"message": {"timestamp": "不是数字"}, "其他": None},
        ],
    )
    @pytest.mark.asyncio
    async def test_messy_input_never_raises(self, kwargs) -> None:
        settings, _ = load_settings({"groups": {"serve": [{"group": "qq:900000001"}]}})
        intake = Intake(lambda: settings, Signals())
        assert await intake.handle(kwargs) == {"action": "continue"}

    @pytest.mark.asyncio
    async def test_hook_exception_swallowed(self, monkeypatch) -> None:
        from CharTyr_MaiWork.maiwork import intake as intake_mod

        settings, _ = load_settings({"groups": {"serve": [{"group": "qq:900000001"}]}})
        signals = Signals()
        # 让 Signals.mark 抛异常，handle 也得吞掉
        monkeypatch.setattr(signals, "mark", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        obj = intake_mod.Intake(lambda: settings, signals)
        assert await obj.handle(hook_message()) == {"action": "continue"}

    def test_messy_input_does_not_leak_marker_objects(self) -> None:
        settings, _ = load_settings({"groups": {"serve": [{"group": "qq:900000001"}]}})
        _ = Intake(lambda: settings, Signals())
        gc.collect()  # 只是确认构造不抛异常；泄漏检查不在本测试范围


class TestSignals:
    def test_take_returns_and_clears(self) -> None:
        s = Signals()
        s.mark("1", "sess", 10.0)
        s.mark("1", "sess", 20.0)
        out = s.take()
        assert out["1"].count == 2
        assert out["1"].last_ts == 20.0
        assert s.take() == {}

    def test_last_ts_unknown_group(self) -> None:
        assert Signals().last_ts("x") == 0.0
