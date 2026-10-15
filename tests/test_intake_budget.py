"""收消息入口必须及时放行；已接到的消息、后台落地失败都能恢复。

2026-10 起钩子**不等 Jev**（判词丢后台任务 `_judge_at`）：这里量的是钩子本身的返回
时间，后台判词 / 落地用 spawn 捕获的协程手动抽干（`for coro in spawned: await coro`，
列表迭代器能看到后台里再 spawn 出来的协程）。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from fakes import hook_message
from test_intake_m3 import _FakeApprovals, _FakeJev, _settings
from test_quick_judge import _Rec, _item, _make_qj, _one

from CharTyr_MaiWork.maiwork import intake as intake_mod
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"


def _store(tmp_path):
    store = Store(tmp_path / "intake.db")
    store.migrate()
    return store


@pytest.mark.asyncio
async def test_slow_jev_leaves_headroom_for_host_timeout():
    """Jev 要 2 秒：钩子不等它（<0.9s 返回），判词在后台跑完再走原来的路。"""
    settings = _settings(jev_timeout_ms=1200)
    jev = _FakeJev(delay_s=2)
    spawned: list = []
    intake = Intake(lambda: settings, Signals(), jev=jev, spawn=lambda coro: spawned.append(coro))
    start = time.monotonic()
    assert await intake.handle(hook_message(GID, text="帮我整理资料", is_at=True)) == {"action": "continue"}
    assert time.monotonic() - start < 0.9
    assert jev.calls == []          # 钩子里连问都没问
    for coro in spawned:
        await coro
    assert jev.calls[0]["timeout_ms"] <= 1200   # 后台按 [jev] timeout_ms 问
    assert intake.slow_queue                    # 没接 approvals：后台走慢路径


@pytest.mark.asyncio
async def test_feedback_is_deferred_and_does_not_block_loop(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    spawned = []
    calls = []
    intake = Intake(lambda: settings, Signals(), store=store, spawn=spawned.append)

    def feedback(*args):
        calls.append(args)
        time.sleep(0.25)

    intake._card_index.on_message = feedback
    msg = hook_message(GID, message_id="reply-1", text="这条不错")
    msg["message"]["reply_to"] = "card-1"
    assert await intake.handle(msg) == {"action": "continue"}
    assert not calls, "查卡片和写反馈不能留在阻塞钩子里"
    assert len(spawned) == 1
    task = asyncio.create_task(spawned.pop())
    start = time.monotonic()
    await asyncio.sleep(0.04)
    assert time.monotonic() - start < 0.16, "后台反馈也不能同步堵住共享事件循环"
    await task
    assert len(calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_cancel_cleanup_does_not_extend_hook_budget(monkeypatch):
    """Jev 卡死被后台外层保险掐断：取消收尾（0.45s）也不占钩子时间。"""
    settings = _settings(jev_timeout_ms=1200)
    cleaned = asyncio.Event()

    class SlowCancel(_FakeJev):
        async def ask(self, *args, **kwargs):
            try:
                await asyncio.sleep(3)
            finally:
                await asyncio.sleep(0.45)
                cleaned.set()

    monkeypatch.setattr(intake_mod, "_JEV_BG_WAIT_MAX_MS", 100)
    spawned: list = []
    intake = Intake(lambda: settings, Signals(), jev=SlowCancel(), spawn=lambda coro: spawned.append(coro))
    start = time.monotonic()
    assert await intake.handle(hook_message(GID, text="帮我整理资料", is_at=True)) == {"action": "continue"}
    assert time.monotonic() - start < 0.9
    for coro in spawned:
        await coro
    assert intake.slow_queue     # 掐断 → 慢路径
    await asyncio.wait_for(cleaned.wait(), timeout=1)


@pytest.mark.asyncio
async def test_message_is_durable_before_jev_await(tmp_path):
    store = _store(tmp_path)
    settings = _settings()
    observed = []

    class Probe(_FakeJev):
        async def ask(self, *args, **kwargs):
            row = store.read().execute(
                "SELECT * FROM pending_asks WHERE group_id=? AND message_id=?", (GID, "probe")
            ).fetchone()
            observed.append(row)
            return {"kind": ("none", 0.95, 0.95)}

    spawned: list = []
    intake = Intake(
        lambda: settings, Signals(), store=store, jev=Probe(), spawn=lambda coro: spawned.append(coro)
    )
    assert await intake.handle(hook_message(GID, message_id="probe", text="晚上好", is_at=True)) == {"action": "continue"}
    # 钩子里还没问 Jev（判词在后台），但这条 @ 已经落库了
    row = store.read().execute("SELECT handled FROM pending_asks WHERE message_id='probe'").fetchone()
    assert row is not None and row["handled"] == 0
    for coro in spawned:
        await coro
    assert observed and observed[0] is not None
    row = store.read().execute("SELECT handled FROM pending_asks WHERE message_id='probe'").fetchone()
    assert row["handled"] == 1, "只有明确闲聊结论才关闭待判行"
    store.close()


@pytest.mark.asyncio
async def test_failed_request_landing_keeps_persisted_intent(tmp_path):
    store = _store(tmp_path)
    settings = _settings()
    spawned = []

    class Broken(_FakeApprovals):
        def create(self, *args, **kwargs):
            raise RuntimeError("模拟落地失败")

    intake = Intake(
        lambda: settings, Signals(), store=store,
        jev=_FakeJev({"kind": ("prepare", 0.95, 0.95)}),
        approvals=Broken(), spawn=spawned.append,
    )
    assert await intake.handle(hook_message(GID, message_id="failed", text="帮我整理资料", is_at=True)) == {"action": "continue"}
    for coro in spawned:
        await coro
    row = store.read().execute("SELECT * FROM pending_asks WHERE message_id='failed'").fetchone()
    assert row is not None and row["handled"] == 0
    assert row["decision"] and "prepare" in row["decision"], "失败后必须保留要建什么"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prepare", "reminder"])
async def test_quick_judge_callback_failure_returns_to_pending(kind):
    class Broken(_Rec):
        def on_request(self, *args):
            raise RuntimeError("请求落地失败")

        def on_reminder(self, *args):
            raise RuntimeError("提醒落地失败")

    qj, models, rec = _make_qj(replies=[_one(kind, 0.95, "资料")], rec=Broken())
    assert qj.enqueue(GID, _item("failed"))
    await qj.join()
    assert len(rec.slows) == 1, "回调异常不能当作已落地"
    await qj.close()


@pytest.mark.asyncio
async def test_quick_judge_cancelled_callback_returns_to_pending():
    entered = asyncio.Event()

    class Interrupted(_Rec):
        async def on_reminder(self, *args):
            entered.set()
            await asyncio.Event().wait()

    qj, models, rec = _make_qj(replies=[_one("reminder", 0.95)], rec=Interrupted())
    assert qj.enqueue(GID, _item("cancelled"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    await qj.close()
    assert len(rec.slows) == 1, "落地中被取消的消息仍须交回慢路径"
