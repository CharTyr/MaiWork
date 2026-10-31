"""钩子预算：收消息 / planner 钩子必须立刻放行——不等模型、不等 Jev、不等网络。

线上实测（2026-10，4 天）：intake 钩子超时 9 次、mentions 钩子超时 3 次；宿主熔断器
连续 3 次失败就熔断 60 秒，期间**所有群**的 MaiWork 钩子都被跳过（收不到消息信号、
planner 备忘也注入不了）。所以：

- @ 消息在钩子里只做纯本地、快的事（记信号、落 pending_asks），Jev 判词丢后台任务，
  判完走原来的后续路径；
- 钩子内部再给自己一个硬上限（intake 800ms、planner 备忘 500ms），超了就放弃这次
  钩子里的附加工作、返回「不改动消息」，并记一条中文日志。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from pathlib import Path

import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork import intake as intake_mod
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"
# 钩子预算（intake 800ms）：留点余量给慢 CI，断言 < 0.9s
_HOOK_BUDGET_S = 0.9


def _settings(*, jev_timeout_ms: int = 1200):
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{GID}"}]},
            "jev": {"timeout_ms": jev_timeout_ms},
        }
    )
    assert not problems
    return settings


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "hook.db")
    store.migrate()
    return store


class _SleepyJev:
    """会 sleep 的假 Jev：模拟模型 / 网络慢，判词要 2 秒才回来。"""

    def __init__(self, delay_s: float = 2.0, answer=None) -> None:
        self.delay_s = float(delay_s)
        self.answer = {"kind": ("prepare", 0.92, 0.9)} if answer is None else answer
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    async def ask(self, state, questions, *, purpose: str, group_id: str = "", timeout_ms=None):
        self.calls.append({"state": state, "purpose": purpose, "group_id": group_id, "timeout_ms": timeout_ms})
        await asyncio.sleep(self.delay_s)
        return self.answer


class _Approvals:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create(self, group_id: str, **kw):
        self.created.append((str(group_id), dict(kw)))
        return {"id": "R-1", "status": "pending"}


class _Mentions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5,
            target_user_id: str = "", keywords="") -> None:
        self.items.append({"group_id": str(group_id), "text": str(text), "key": str(key)})


def _make_intake(tmp_path: Path, *, jev=None):
    """装好 intake：spawn 只登记协程（后台活由测试自己 drain，确定性强）。"""
    settings = _settings()
    store = _store(tmp_path)
    spawned: list = []
    intake = Intake(
        lambda: settings,
        Signals(),
        jev=jev,
        approvals=_Approvals(),
        mentions=_Mentions(),
        store=store,
        spawn=lambda coro: spawned.append(coro),
    )
    return intake, store, spawned


async def _drain(spawned: list) -> None:
    """把钩子 spawn 出来的后台协程跑完（后台里再 spawn 出来的也跑，直到清空）。"""
    while spawned:
        await spawned.pop(0)


# ----------------------------------------------------------------------
# 1) @ 消息：钩子不等 Jev；判词在后台跑完并按原来的结果落地
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_hook_returns_before_slow_jev_and_background_lands(tmp_path) -> None:
    """假 Jev 睡 2 秒：钩子 < 0.9s 返回，后台跑完判词照样建待批请求。"""
    jev = _SleepyJev(delay_s=2.0)
    intake, store, spawned = _make_intake(tmp_path, jev=jev)
    try:
        start = time.monotonic()
        out = await intake.handle(
            hook_message(GID, text="帮我整理一份铝坨坨的资料", is_at=True, message_id="m-slow-jev")
        )
        took = time.monotonic() - start
        assert out == {"action": "continue"}
        assert took < _HOOK_BUDGET_S, f"钩子等了 Jev（{took:.2f}s）"
        # 钩子回来时判词还没到，什么都没落地
        assert jev.calls == []
        assert intake._approvals.created == []
        # 但这条 @ 已经落库（0.9.8 的持久化不破）
        row = store.read().execute(
            "SELECT state, handled FROM pending_asks WHERE message_id='m-slow-jev'"
        ).fetchone()
        assert row is not None and row["state"] == "pending" and row["handled"] == 0

        # 后台把判词跑完：Jev 判 prepare → 建待批请求 → 关掉待判行
        await _drain(spawned)
        assert len(jev.calls) == 1
        assert jev.calls[0]["purpose"] == "intake"
        assert jev.calls[0]["timeout_ms"] and jev.calls[0]["timeout_ms"] <= 1200
        assert len(intake._approvals.created) == 1
        gid, kw = intake._approvals.created[0]
        assert gid == GID and kw["kind"] == "task" and kw["message_id"] == "m-slow-jev"
        assert "Jev" in kw["via"]
        row = store.read().execute(
            "SELECT state, handled, result_id FROM pending_asks WHERE message_id='m-slow-jev'"
        ).fetchone()
        assert row["state"] == "completed" and row["handled"] == 1 and row["result_id"] == "R-1"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_at_hook_uses_background_timeout_net_for_dead_jev(tmp_path, monkeypatch) -> None:
    """Jev 卡死不回（外层保险到点）：后台走慢路径写 pending_asks，钩子早已返回。"""
    monkeypatch.setattr(intake_mod, "_JEV_BG_WAIT_MAX_MS", 50)
    jev = _SleepyJev(delay_s=5.0)
    intake, store, spawned = _make_intake(tmp_path, jev=jev)
    try:
        start = time.monotonic()
        out = await intake.handle(hook_message(GID, text="帮我整理资料", is_at=True, message_id="m-dead-jev"))
        assert out == {"action": "continue"}
        assert time.monotonic() - start < _HOOK_BUDGET_S
        await _drain(spawned)
        row = store.read().execute(
            "SELECT state, handled, reason FROM pending_asks WHERE message_id='m-dead-jev'"
        ).fetchone()
        assert row["state"] == "pending" and row["handled"] == 0
        assert row["reason"] == "jev_无答案"
        assert intake._approvals.created == []
    finally:
        store.close()


# ----------------------------------------------------------------------
# 2) 钩子自己的硬上限：慢的本地附加工作被切掉，不抛异常、不改动消息
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_hook_budget_cuts_slow_local_work(tmp_path, caplog) -> None:
    intake, store, _spawned = _make_intake(tmp_path)

    async def _very_slow(*args, **kwargs):
        await asyncio.sleep(5)

    intake._m3 = _very_slow  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.INFO, logger="maiwork.intake"):
            start = time.monotonic()
            out = await intake.handle(hook_message(GID, text="普通消息", message_id="m-budget"))
            took = time.monotonic() - start
        assert out == {"action": "continue"}
        assert took < _HOOK_BUDGET_S + 0.2, f"钩子没有按预算切掉慢活（{took:.2f}s）"
        assert any("预算" in r.getMessage() for r in caplog.records)
    finally:
        store.close()


# ----------------------------------------------------------------------
# 3) planner 备忘钩子：本地附加工作也有硬上限
# ----------------------------------------------------------------------


def _planner_kwargs(session_id: str = "sess-1") -> dict:
    return {
        "item_schema_version": 1,
        "session_id": session_id,
        "items": [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "原来的人格"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "群友"}]},
        ],
    }


def _mentions_with_memo(tmp_path: Path) -> tuple[Mentions, Store]:
    store = _store(tmp_path)
    settings = _settings()
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "sess-1"))
    mentions = Mentions(store, lambda: settings)
    mentions.add(GID, "最近那个开源项目", key="k1", ttl_s=3600, turns=3)
    return mentions, store


def test_mentions_inject_skips_when_deadline_already_passed(tmp_path, caplog) -> None:
    """预算已用完：不拼备忘、不改动消息，记一条中文日志。"""
    mentions, store = _mentions_with_memo(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger="maiwork.delivery"):
            out = mentions.inject(copy.deepcopy(_planner_kwargs()), group_id=GID, deadline=time.monotonic() - 1)
        assert out is None
        assert any("预算" in r.getMessage() for r in caplog.records)
    finally:
        store.close()


def test_mentions_inject_gives_up_when_memo_build_is_too_slow(tmp_path, caplog) -> None:
    """拼备忘本身花超了预算：后面的深拷贝 / 回传整包都不做，直接「不改动消息」。"""
    mentions, store = _mentions_with_memo(tmp_path)
    try:
        def _slow_memo(group_id: str, kwargs) -> str:
            time.sleep(0.6)
            return "【MaiWork 备忘】\n- 慢活拼出来的"

        mentions._memo_with_topics = _slow_memo  # type: ignore[method-assign]
        with caplog.at_level(logging.INFO, logger="maiwork.delivery"):
            out = mentions.inject(
                copy.deepcopy(_planner_kwargs()), group_id=GID, deadline=time.monotonic() + 0.5
            )
        assert out is None
        assert any("预算" in r.getMessage() for r in caplog.records)
    finally:
        store.close()


def test_mentions_inject_within_deadline_still_appends(tmp_path) -> None:
    """给了预算但没超：照常追加备忘（加了 deadline 不许改变正常路径）。"""
    mentions, store = _mentions_with_memo(tmp_path)
    try:
        out = mentions.inject(
            copy.deepcopy(_planner_kwargs()), group_id=GID, deadline=time.monotonic() + 5
        )
        assert out is not None
        text = out["items"][0]["parts"][-1]["text"]
        assert "【MaiWork 备忘】" in text and "最近那个开源项目" in text
    finally:
        store.close()
