"""冷场开话题「先修判断材料」（2026-10-03 用户选）的回归测试。

背景（docs/18 §五 的实测调查）：365 次判断 0 次开。两处材料问题：
1. 喂给 Jev 的最近 20 条里，机器人自己（MaiBot / MaiWork 同一个 bot 账号，
   host.py 用 `user_id == bot_qq` 判 `Msg.is_bot`）发的话没标出来：机器人自己
   没人接的追问、资讯卡、提醒被 Jev 判成「有问题还没人回」(open_question)，
   于是永远不开。
2. 「不适合开新话题的原因」那题把「可以开(fine)」当选项之一，和 ok 那一问
   互相打架：实测选 fine 时 ok 总在 0.49~0.55，两问从没同时过关。

本文件只盯「喂给 Jev 的判断材料」（提示词 / state），不碰门槛数字
（门槛数字见 test_topics_thresholds.py）和第 1 层闸门（睡觉时段、安静时长、每日上限…）。
共用假对象和构造辅助复用 test_topics.py，不调真网络 / 真模型。
"""

from __future__ import annotations

from typing import Any, List

import pytest

from fakes import FakeHost, SignalsStub  # noqa: E402
from test_topics import (  # noqa: E402  （conftest 已把 tests 目录放进 sys.path）
    GID,
    SID,
    _Jev,
    _bj_ts,
    _make_topics,
    _seed_candidate,
    _text_msg,
)

# 机器人自己发的话在 Jev 材料里的标记（需求给的样子）
BOT_MARK = "[机器人自己]"

T = _bj_ts(2026, 9, 27, 15, 0)
_CFG = {"topics": {"enabled": True, "per_day": 10, "min_gap_hours": 0}}


async def _ask_jev(tmp_path, msgs: List[Any], *, signals_ts: float | None = None):
    """造一个「安静 60 分钟」的冷场，问一次 Jev，返回 (state, questions)。"""
    signals = SignalsStub()
    signals.mark(GID, SID, signals_ts if signals_ts is not None else T - 3600.0)
    host = FakeHost(msgs=msgs, session_id=SID)
    fake = _Jev(response={"ok": 0.4, "reason": ("left", 0.8, 0.8), "fit_0": 0.2})
    store, settings, topics, *_ = _make_topics(
        tmp_path, host=host, signals=signals, jev=fake, cfg=_CFG
    )
    _seed_candidate(store, check_now=T, title="测试候选")
    out = await topics.check(GID, T)
    assert fake.calls, f"该问到 Jev 才对（实际 {out}）"
    state, questions, _purpose, _gid = fake.calls[0]
    return state, questions


# ----------------------------------------------------------------------
# ① 标出机器人自己的话
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_messages_marked_in_jev_state(tmp_path):
    """机器人自己发的话在 state 里带 [机器人自己] 标记；群友的话还是人名。"""
    msgs = [
        _text_msg(T - 1800.0, "这周末有人去漫展吗", uid="10001", name="张三"),
        _text_msg(T - 900.0, "看全部资讯：https://example.com/n/1", uid="bot", name="测试AI", is_bot=True),
        _text_msg(T - 600.0, "后来组顺了吗？要我帮你看看不？", uid="bot", name="测试AI", is_bot=True),
    ]
    state, _questions = await _ask_jev(tmp_path, msgs)

    speakers = [m["speaker"] for m in state["messages"]]
    assert speakers[0] == "张三"
    assert speakers[1] == BOT_MARK, f"机器人自己的资讯卡要标出来，实际 {speakers[1]!r}"
    assert speakers[2] == BOT_MARK, f"机器人自己的追问要标出来，实际 {speakers[2]!r}"
    # 文本本身不动（标记只加在 speaker 上）
    assert state["messages"][1]["text"].startswith("看全部资讯")
    assert state["messages"][2]["text"].startswith("后来组顺了吗")


@pytest.mark.asyncio
async def test_bot_mark_explained_to_jev(tmp_path):
    """提示里说明 [机器人自己] 是机器人自己说的（state 的说明或题目里都算）。"""
    msgs = [_text_msg(T - 600.0, "要我帮你看看不？", uid="bot", name="测试AI", is_bot=True)]
    state, questions = await _ask_jev(tmp_path, msgs)

    blob = " ".join(
        [str(state.get("note") or "")]
        + [str(questions["ok"]["instructions"])]
        + [str(questions["reason"].get("instructions") or "")]
        + [str(v) for v in questions["reason"]["criteria"].values()]
    )
    assert BOT_MARK in blob, "提示里要出现 [机器人自己] 这个标记"
    assert "机器人自己" in blob
    assert "群友" in blob, "要说清只有群友的话才算数"


# ----------------------------------------------------------------------
# ② 机器人自己的追问 / 资讯卡 / 提醒不算「有人在等回复」；
#    最后一条是机器人自己发的时候，群友的冷场时长照样告诉 Jev
#    （只改材料，不动闸门：quiet_minutes 仍按原来的 quiet_ts 算）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_own_question_not_counted_as_open_question(tmp_path):
    """reason 的 open_question 判据要排除机器人自己的追问 / 资讯卡 / 提醒。"""
    msgs = [
        _text_msg(T - 1800.0, "这周末有人去漫展吗", uid="10001", name="张三"),
        _text_msg(T - 600.0, "后来组顺了吗？要我帮你看看不？", uid="bot", name="测试AI", is_bot=True),
    ]
    _state, questions = await _ask_jev(tmp_path, msgs)

    open_q = str(questions["reason"]["criteria"]["open_question"])
    assert BOT_MARK in open_q, "判据里要提到 [机器人自己]"
    assert "不算" in open_q, "要明确机器人自己的话没人接不算「有问题还没人回」"
    assert "群友" in open_q, "要明确只有群友提的问题没人回才算"
    # ok 那一问也要提醒（不然它看到机器人自己的追问就给低分）
    ok_instr = str(questions["ok"]["instructions"])
    assert BOT_MARK in ok_instr
    assert "不算" in ok_instr


@pytest.mark.asyncio
async def test_bot_own_question_not_in_mood_or_left_criteria(tmp_path):
    """别的选项别被顺手写歪：fine/left/mood 的 key 和意思保持原样。"""
    msgs = [_text_msg(T - 600.0, "要我帮你看看不？", uid="bot", name="测试AI", is_bot=True)]
    _state, questions = await _ask_jev(tmp_path, msgs)

    criteria = questions["reason"]["criteria"]
    assert set(criteria.keys()) == {"fine", "left", "open_question", "mood"}
    assert "可以开" in str(criteria["fine"])
    assert "人都走了" in str(criteria["left"])
    assert "气氛不对" in str(criteria["mood"])
    # 机器人自己的话不该出现在 left / mood 的判据里（那是「群友都走了」「气氛不对」）
    assert BOT_MARK not in str(criteria["left"])
    assert BOT_MARK not in str(criteria["mood"])


@pytest.mark.asyncio
async def test_last_bot_message_reports_human_quiet_time(tmp_path):
    """最后一条是机器人自己的 → 另报「群友最后一次发言在 N 分钟前」。"""
    msgs = [
        _text_msg(T - 3000.0, "这周末有人去漫展吗", uid="10001", name="张三"),  # 50 分钟前
        _text_msg(T - 600.0, "看全部资讯：https://example.com/n/1", uid="bot", name="测试AI", is_bot=True),
    ]
    state, questions = await _ask_jev(tmp_path, msgs)

    assert state["last_message_is_bot"] is True
    assert state["last_human_minutes_ago"] == 50
    # 闸门没动：quiet_minutes 仍按 quiet_ts（信号时间）算 = 60
    assert state["quiet_minutes"] == 60
    ok_instr = str(questions["ok"]["instructions"])
    assert "最后一条是机器人自己发的" in ok_instr
    assert "50 分钟前" in ok_instr


@pytest.mark.asyncio
async def test_last_human_message_keeps_plain_wording(tmp_path):
    """最后一条是群友发的 → 不加那两句，措辞跟原来一样。"""
    msgs = [
        _text_msg(T - 1800.0, "看全部资讯：https://example.com/n/1", uid="bot", name="测试AI", is_bot=True),
        _text_msg(T - 600.0, "刚那个方案不错", uid="10001", name="张三"),
    ]
    state, questions = await _ask_jev(tmp_path, msgs)

    assert state["last_message_is_bot"] is False
    assert state["last_human_minutes_ago"] == 10
    ok_instr = str(questions["ok"]["instructions"])
    assert "最后一条是机器人自己发的" not in ok_instr
    assert "60 分钟" in ok_instr


# ----------------------------------------------------------------------
# ③ reason 那题改成不自相矛盾的问法
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reason_question_asks_group_state_not_reason_for_no(tmp_path):
    """题干问「现在群里的状态是哪一种」，不再问「不适合开新话题的原因」。"""
    msgs = [_text_msg(T - 600.0, "在吗")]
    _state, questions = await _ask_jev(tmp_path, msgs)

    reason_q = questions["reason"]
    assert reason_q["type"] == "choice"
    assert "不适合" not in str(reason_q["instructions"]), "题干别再预设「不适合」"
    assert "状态" in str(reason_q["instructions"])
    # choice 的 key 不能动（判定代码和日志兼容）
    assert set(reason_q["criteria"].keys()) == {"fine", "left", "open_question", "mood"}


@pytest.mark.asyncio
async def test_ok_question_does_not_fight_reason_question(tmp_path):
    """ok 那一问问的是「现在开合适吗」，不预设不合适、也不重复 reason 的活。"""
    msgs = [_text_msg(T - 600.0, "在吗")]
    _state, questions = await _ask_jev(tmp_path, msgs)

    ok_q = questions["ok"]
    assert ok_q["type"] == "noul"
    instr = str(ok_q["instructions"])
    assert "合适吗" in instr
    assert "不适合" not in instr
    # 安静时长 / 平时节奏这两个数字还在（原来的材料不能丢）
    assert "60 分钟" in instr
    assert "5.0 分钟" in instr
