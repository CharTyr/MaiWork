"""test_task_answer.py：waiting_input / shelved 任务的「有人回答 → 恢复」链路。

docs/02 §7.2：waiting_input 的问题发到群里后，有人回复那条提问 → 恢复任务
（coordinator.resume）；搁置的也一样。修法三件事都要测：
- outbox 发出 ask: 的 text 成功后，把 QQ 消息 ID 回写到任务 question_msg_id；
- intake 在服务群里识别「回复那条提问」或「发起人 @ 机器人（只有一个等待任务）」
  → 后台 spawn coordinator.resume；
- 恢复后任务详情时间线能看到「收到回答：前 60 字」（tool_calls 行）+ 库里留 task.answer 事件。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fakes import FakeCoordinator, FakeCtx, FakeProfiles, hook_message

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

pytestmark = pytest.mark.asyncio

G1 = "900000001"
NOW = 1_790_000_000.0


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件所有用例把 clock.now 冻结在 NOW（北京时间 2026-09-21 22:13，不在默认睡觉时段 23:00-08:00）。

    产品按真实墙钟判断睡觉时段：不冻结的话，在 23:00-08:00 之间跑这份测试，
    ask: 的 text 会被 Pushes 正确推迟（outbox 仍 pending、不回写 question_msg_id），
    断言就会「假红」，看着像消息回写坏了，其实产品行为是对的。
    冻结后 question_ts / flush 的 now / 模拟消息时间（_hook_reply 也用 NOW）三者一致，
    结果不再随墙钟变化。monkeypatch 每个用例自动还原，不泄漏到别的测试文件。
    """
    monkeypatch.setattr(clock, "now", lambda: NOW)


# ---------------------------------------------------------------------------
# intake 层：答案识别（注入 waiting_tasks 缓存 + on_answer 回调 + 录 spawn）
# ---------------------------------------------------------------------------


class _AnswerSpy:
    """录 on_answer(group_id, task_id, text)；协程由 spawn 捕获，_drain 跑掉。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, group_id: str, task_id: str, text: str) -> None:
        self.calls.append((str(group_id), str(task_id), str(text)))


def _hook_reply(
    *,
    group_id: str = G1,
    user_id: str = "10001",
    text: str = "要 json 格式",
    message_id: str = "500",
    reply_to: str = "",
    is_at: bool = False,
) -> dict:
    """造一条服务群消息；raw_message 里带回复段时用 [CQ:reply] 形式（docs/06）。"""
    raw = f"[CQ:reply,id={reply_to}]{text}" if reply_to else text
    return {
        "message": {
            "message_id": message_id,
            "timestamp": NOW,
            "platform": "qq",
            "message_info": {
                "user_info": {"user_id": user_id, "user_nickname": "群友"},
                "group_info": {"group_id": group_id, "group_name": "测试群"},
            },
            "raw_message": raw,
            "is_at": is_at,
            "is_mentioned": is_at,
            "is_command": False,
            "is_emoji": False,
            "is_picture": False,
            "is_notify": False,
            "session_id": "sess-1",
            "processed_plain_text": text,
        }
    }


def _make_intake(
    *,
    waiting: list[tuple[str, str, str]] | None = None,
    bot_qq: str = "",
    serve=(G1,),
):
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
        }
    )
    signals = Signals()
    spawned: list = []
    spy = _AnswerSpy()
    intake = Intake(
        lambda: settings,
        signals,
        bot_qq=bot_qq,
        spawn=lambda coro: spawned.append(coro),
        on_answer=spy,
        waiting_tasks=lambda gid: list(waiting or []) if str(gid) in serve else [],
    )
    return intake, signals, spawned, spy


async def _drain(spawned: list) -> None:
    for coro in list(spawned):
        await coro


class TestIntakeAnswerDetection:
    async def test_reply_to_question_triggers_answer(self) -> None:
        intake, signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        msg = _hook_reply(reply_to="9001")
        assert await intake.handle(msg) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == [(G1, "T-1", "要 json 格式")]
        # 信号照样记
        assert signals.take()[G1].count == 1

    async def test_reply_to_unknown_message_no_trigger(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        assert await intake.handle(_hook_reply(reply_to="7777")) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []

    async def test_other_person_reply_does_not_trigger(self) -> None:
        # 别人随便说话（不带回复、不带 @、又不是发起人等任务）→ 不触发
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        assert await intake.handle(_hook_reply(user_id="20002")) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []

    async def test_requester_at_bot_single_waiting_triggers(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        # 发起人 @ 机器人、群里只有这一个等待任务（不带回复段也认）
        assert await intake.handle(_hook_reply(is_at=True)) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == [(G1, "T-1", "要 json 格式")]

    async def test_multiple_waiting_do_not_guess(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001"), ("T-2", "9002", "10001")],
        )
        # 发起人 @ 机器人但有两个等待任务 → 不猜
        assert await intake.handle(_hook_reply(is_at=True)) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []

    async def test_non_requester_at_bot_no_trigger(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        # 20002 ≠ requester_id：@ 机器人也不算回答
        assert await intake.handle(_hook_reply(user_id="20002", is_at=True)) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []

    async def test_mw_command_not_answer(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        msg = _hook_reply(text="/mw 取消 T-1", reply_to="9001", message_id="501")
        assert await intake.handle(msg) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []

    async def test_bot_own_message_not_answer(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
            bot_qq="10001",
        )
        # 机器人自己的消息（user_id == bot_qq）就算回复了提问也不算
        assert await intake.handle(_hook_reply(reply_to="9001")) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []

    async def test_same_message_once(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        msg = _hook_reply(reply_to="9001")
        await intake.handle(msg)
        await intake.handle(msg)
        await _drain(spawned)
        assert len(spy.calls) == 1

    async def test_non_served_group_zero_handling(self) -> None:
        intake, signals, spawned, spy = _make_intake(
            waiting=[],
        )
        # 消息来自非服务群：还没轮到答案识别就已经 continue 了
        msg = _hook_reply(group_id="333333", reply_to="9001")
        assert await intake.handle(msg) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []
        assert signals.take() == {}

    async def test_empty_text_from_reply_not_triggered(self) -> None:
        intake, _signals, spawned, spy = _make_intake(
            waiting=[("T-1", "9001", "10001")],
        )
        assert await intake.handle(_hook_reply(text="", reply_to="9001")) == {"action": "continue"}
        await _drain(spawned)
        assert spy.calls == []


# ---------------------------------------------------------------------------
# app / outbox 接线层：ask: 发出 → question_msg_id；回复 → FakeCoordinator.resume
# ---------------------------------------------------------------------------


def _raw(data_dir: Path, *, serve=(G1,)) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": "pw-测试"},
        "storage": {"data_dir": str(data_dir)},
    }


def _app(tmp_path: Path, *, serve=(G1,)) -> MaiWorkApp:
    ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "fake-msg-id"}})
    app = MaiWorkApp(ctx, _raw(tmp_path / "data", serve=serve), plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    return app


def _fix_session(app: MaiWorkApp, gid: str = G1) -> None:
    """outbox 发 text 要 groups.session_id；测试里直接补足（真链路是 run_loop_once 的 remember 写）。"""
    with app.store.tx() as conn:
        conn.execute("UPDATE groups SET session_id=? WHERE group_id=?", ("sess-1", gid))


class TestAskMessageIdWriteback:
    async def test_outbox_ask_writes_question_msg_id(self, tmp_path: Path) -> None:
        """outbox.flush 发出 ask:{tid}:{n} 的 text 成功后，回写任务 question_msg_id。"""
        app = _app(tmp_path)
        await app.start()
        try:
            _fix_session(app)
            tid = app.tasks.create(G1, title="等回答", req="", criteria=[], source="test", status="queued")
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="要哪种格式？", question_ts=clock.now())
            oid = app.outbox.enqueue(
                f"ask:{tid}:1",
                G1,
                "text",
                {"text": "要哪种格式？", "push_kind": "status"},
                task_id=tid,
            )
            await app.outbox.flush(clock.now())
            row = app.store.read().execute("SELECT status, result FROM outbox WHERE id=?", (oid,)).fetchone()
            assert row["status"] == "sent"
            mid = json.loads(row["result"] or "{}").get("message_id")
            assert mid == "fake-msg-id"
            t = app.tasks.get(tid)
            assert str(t["question_msg_id"] or "") == "fake-msg-id"
        finally:
            await app.stop()

    async def test_outbox_human_ask_writes_question_msg_id(self, tmp_path: Path) -> None:
        """「需要有人参与」的提问（key human:{tid}:{n}）发出后同样回写 question_msg_id。

        线上 T-14（2026-10-10）：这条没回写，发起人引用回复那条提问时认不出是回答，
        只能靠「@ 机器人」那条规则兜底。
        """
        app = _app(tmp_path)
        await app.start()
        try:
            _fix_session(app)
            tid = app.tasks.create(G1, title="改图", req="", criteria=[], source="test", status="queued")
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="请重发原图", question_ts=clock.now())
            oid = app.outbox.enqueue(
                f"human:{tid}:2",
                G1,
                "text",
                {"text": "这一步需要有人参与：请重发原图", "push_kind": "status"},
                task_id=tid,
            )
            await app.outbox.flush(clock.now())
            row = app.store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()
            assert row["status"] == "sent"
            t = app.tasks.get(tid)
            assert str(t["question_msg_id"] or "") == "fake-msg-id"
        finally:
            await app.stop()

    async def test_non_ask_text_does_not_touch_question_msg_id(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        await app.start()
        try:
            _fix_session(app)
            tid = app.tasks.create(G1, title="普通", req="", criteria=[], source="test", status="queued")
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="问一句？", question_ts=clock.now())
            app.outbox.enqueue(
                f"task-wait-remind:{tid}",
                G1,
                "text",
                {"text": "还在等回答：问一句？", "push_kind": "status"},
            )
            await app.outbox.flush(clock.now())
            t = app.tasks.get(tid)
            assert str(t["question_msg_id"] or "") == ""
        finally:
            await app.stop()


class TestReplyResumesTaskEndToEnd:
    async def test_full_chain_reply_then_resume(self, tmp_path: Path) -> None:
        """整条链：ask 发出 → 写回 question_msg_id → 收到回复 → FakeCoordinator.resume 被调。"""
        app = _app(tmp_path)
        coord = FakeCoordinator()
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            _fix_session(app)
            tid = app.tasks.create(
                G1, title="等回答", req="做个表", criteria=[], source="test",
                requester_id="10001", requester_name="阿柒", status="queued",
            )
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="要哪种格式？", question_ts=clock.now())
            app.outbox.enqueue(
                f"ask:{tid}:1", G1, "text",
                {"text": "@阿柒 要哪种格式？", "push_kind": "status"}, task_id=tid,
            )
            await app.outbox.flush(clock.now())
            assert str(app.tasks.get(tid)["question_msg_id"] or "") == "fake-msg-id"

            # 群里收到一条回复了那条提问的消息
            reply = _hook_reply(text="json 就行", reply_to="fake-msg-id", message_id="m-777")
            assert await app._intake.handle(reply) == {"action": "continue"}
            # resume 是 spawn 出去的：等它落到
            for _ in range(50):
                if coord.resume_calls:
                    break
                await asyncio.sleep(0.02)
            assert coord.resume_calls == [(tid, "json 就行")]
        finally:
            await app.stop()

    async def test_shelved_also_resumes(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        coord = FakeCoordinator()
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            _fix_session(app)
            tid = app.tasks.create(
                G1, title="搁置的", req="做个表", criteria=[], source="test",
                requester_id="10001", requester_name="阿柒", status="queued",
            )
            app.tasks.transition(tid, "running")
            app.tasks.transition(tid, "waiting_input", question="预算多少？", question_ts=clock.now())
            app.tasks.transition(tid, "shelved", reason="超 24 小时")
            # 搁置任务那条提问之前发出去时写的 question_msg_id，这里直接补进库
            with app.store.tx() as conn:
                conn.execute("UPDATE tasks SET question_msg_id=? WHERE id=?", ("m-q-1", tid))
            if hasattr(app, "_invalidate_answer_cache"):
                app._invalidate_answer_cache()
            reply = _hook_reply(text="五百以内", reply_to="m-q-1", message_id="m-778")
            assert await app._intake.handle(reply) == {"action": "continue"}
            for _ in range(50):
                if coord.resume_calls:
                    break
                await asyncio.sleep(0.02)
            assert coord.resume_calls == [(tid, "五百以内")]
        finally:
            await app.stop()


class TestQuoteAnyPromptOfTheTask:
    """2026-10-10（线上 T-14）：提问末尾统一写「引用这条消息回复我就行」——
    6 小时「还在等回答」的提醒也写了，所以引用那条提醒回复也要认得出是回答。"""

    @staticmethod
    def _waiting_task_with_reminder(app: MaiWorkApp) -> str:
        tid = app.tasks.create(
            G1, title="改图", req="改成生气的", criteria=[], source="test",
            requester_id="10001", requester_name="阿柒", status="queued",
        )
        app.tasks.transition(tid, "running")
        app.tasks.transition(tid, "waiting_input", question="请补原图", question_ts=clock.now())
        oid = app.outbox.enqueue(
            f"task-wait-remind:{tid}", G1, "text",
            {"text": "「改图」还在等回答：请补原图（引用这条消息回复我就行）", "push_kind": "status"},
            task_id=tid,
        )
        with app.store.tx() as conn:
            conn.execute("UPDATE tasks SET question_msg_id=? WHERE id=?", ("m-q-1", tid))
            conn.execute(
                "UPDATE outbox SET status='sent', result=? WHERE id=?",
                (json.dumps({"message_id": "m-remind-1"}), oid),
            )
        app._invalidate_answer_cache()
        return tid

    async def test_quoting_the_reminder_resumes(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        coord = FakeCoordinator()
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            _fix_session(app)
            tid = self._waiting_task_with_reminder(app)
            reply = _hook_reply(text="图在这", reply_to="m-remind-1", message_id="m-801")
            assert await app._intake.handle(reply) == {"action": "continue"}
            for _ in range(50):
                if coord.resume_calls:
                    break
                await asyncio.sleep(0.02)
            assert coord.resume_calls == [(tid, "图在这")]
        finally:
            await app.stop()

    async def test_quoting_the_original_question_still_resumes(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        coord = FakeCoordinator()
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            _fix_session(app)
            tid = self._waiting_task_with_reminder(app)
            reply = _hook_reply(text="好了", reply_to="m-q-1", message_id="m-802")
            assert await app._intake.handle(reply) == {"action": "continue"}
            for _ in range(50):
                if coord.resume_calls:
                    break
                await asyncio.sleep(0.02)
            assert coord.resume_calls == [(tid, "好了")]
        finally:
            await app.stop()

    async def test_at_bot_rule_counts_tasks_not_prompts(self, tmp_path: Path) -> None:
        """同一个任务有两条提问消息（原提问 + 提醒），「@ 机器人 + 只有一个等待任务」仍成立。"""
        app = _app(tmp_path)
        coord = FakeCoordinator()
        app.coordinator_factory = lambda *a, **kw: coord
        await app.start()
        try:
            _fix_session(app)
            tid = self._waiting_task_with_reminder(app)
            reply = _hook_reply(text="弄好了", message_id="m-803", is_at=True)
            assert await app._intake.handle(reply) == {"action": "continue"}
            for _ in range(50):
                if coord.resume_calls:
                    break
                await asyncio.sleep(0.02)
            assert coord.resume_calls == [(tid, "弄好了")]
        finally:
            await app.stop()


# ---------------------------------------------------------------------------
# coordinator.resume 层：状态 + 「收到回答」事件进时间线
# ---------------------------------------------------------------------------


class _FakeOutbox:
    def __init__(self) -> None:
        self.enqueued: list = []

    def enqueue(self, *a, **kw):
        self.enqueued.append((a, kw))
        return 1


def _make_coordinator(store: Store, tasks: Tasks) -> Coordinator:
    class _Models:
        def settings(self):
            class _S:
                def ready(self):
                    return False  # resume 之后 run_task 会因没配好而停在 queued，不真跑主模型

            return _S()

    class _Cfg:
        class environments:  # noqa: D106 - 简单配置存根
            max_parallel = 2
            run_as = "maiwork"
            memory_max = "512M"
            railway = False

        def workspace_of(self, gid: str) -> str:
            return f"g{gid}"

    return Coordinator(
        store,
        _Models(),
        workers=None,
        tools=None,
        tasks=tasks,
        goals=None,
        delivery=None,
        outbox=_FakeOutbox(),
        env=None,
        profiles=None,
        get_settings=lambda: _Cfg(),
    )


class TestResumeRecordsEvent:
    async def test_resume_records_answer_event_and_moves_to_queued(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "resume.db")
        store.migrate()
        try:
            settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{G1}"}]}})
            tasks = Tasks(store, lambda: settings)
            tid = tasks.create(G1, title="等回答", req="原始需求", criteria=[], source="test", status="queued")
            tasks.transition(tid, "running")
            long_answer = "这是一个回答" * 20  # 远超 60 字
            tasks.transition(tid, "waiting_input", question="问？", question_ts=clock.now())
            coord = _make_coordinator(store, tasks)
            await coord.resume(tid, long_answer)
            t = tasks.get(tid)
            assert t["status"] in ("queued", "running")  # 模型没配好：留在 queued
            # req 追加了回答
            assert long_answer in str(t["req"])
            # 事件表有 task.answer（payload 带前 60 字）
            ev = store.read().execute(
                "SELECT payload FROM events WHERE kind='task.answer' AND entity_id=?", (tid,)
            ).fetchone()
            assert ev is not None
            assert long_answer[:10] in str(ev["payload"])
            payload = json.loads(ev["payload"])
            assert len(str(payload.get("text") or "")) <= 60
            # 时间线（tool_calls）能看到「收到回答：前 60 字」
            rows = store.read().execute(
                "SELECT tool, output FROM tool_calls WHERE task_id=? ORDER BY ts", (tid,)
            ).fetchall()
            tools = [str(r["tool"]) for r in rows]
            assert "收到回答" in tools
            out_row = [r for r in rows if str(r["tool"]) == "收到回答"][-1]
            assert str(out_row["output"]).startswith(long_answer[:5])
            assert len(str(out_row["output"])) <= 60
        finally:
            store.close()


def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_PORT: list[int] = []


def _port() -> int:
    """整个测试文件只挑一次：同一测试里前后两份配置端口要一样，不然会被当成「改了网页地址」重启。"""
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]
