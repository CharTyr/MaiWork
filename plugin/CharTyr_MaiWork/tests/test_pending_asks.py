"""问题 B：@ 的慢路径落地 pending_asks；主模型读群时发现请求（asks）。

intake 侧：
- Jev 明确判 none 且把握 ≥0.6 → 什么都不记（不进 pending_asks、不写事件）；
- Jev 不可用 / 超时 / 答案无效 / 把握不够 → 写 pending_asks(handled=0)，不写 intake.slow 事件。

profile 提炼侧：
- 提示词里给 pending_asks 未处理的消息标「这条 @ 了 MaiBot，Jev 没判出来，请你判断」，
  规则里声明 asks 输出；
- 模型输出 asks：prepare/goal → approvals 落待批（message_id 去重）；reminder+when →
  goals.create_member + outbox 回「记下了，<时间> 提醒你」（reply_to 原消息）；
  判过的 pending_asks 标 handled；普通讨论（没 asks）什么都不建；
  机器人消息、非服务群消息绝不处理。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fakes import FakeCtx, FakeHost, FakeModelsQueue, FakeProfiles, hook_message

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.approvals import Approvals
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.delivery import Mentions, Pushes
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.host import Msg
from CharTyr_MaiWork.intake import Intake, Signals
from CharTyr_MaiWork.outbox import Outbox
from CharTyr_MaiWork.profile import Profiles, _parse_bj_when
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks

GID = "900000001"
# 2026-09-26 06:05 UTC = 北京时间 14:05（周六）
T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()


def _settings(serve=(GID,), **profile_over):
    profile = {"backfill_days": 30, "backfill_max_messages": 1500, "batch_messages": 3}
    profile.update(profile_over)
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}", "workspace": "tinker"} for g in serve]},
        "profile": profile,
    }
    settings, problems = load_settings(raw)
    assert not problems
    return settings


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(clock, "now", lambda: T0)
    return T0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


def _msg(mid: str, ts: float, user: str = "u1", name: str = "阿一", *, bot: bool = False, text: str = "你好") -> Msg:
    return Msg(
        id=mid, ts=ts, user_id=user, user_name=name, text=text,
        is_bot=bot, is_at=False, is_picture=False, reply_to="",
    )


def _pending_rows(store: Store, gid: str = GID) -> list:
    return store.read().execute(
        "SELECT * FROM pending_asks WHERE group_id=? ORDER BY ts, message_id", (gid,)
    ).fetchall()


def _events(store: Store, kind: str) -> list:
    return store.read().execute(
        "SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)
    ).fetchall()


# ----------------------------------------------------------------------
# intake 慢路径 → pending_asks
# ----------------------------------------------------------------------


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
        self.calls.append({"state": state, "purpose": purpose, "timeout_ms": timeout_ms})
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return self.answers


def _make_intake(store: Store | None, *, jev=None, jev_timeout_ms: int = 1200):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "jev": {"timeout_ms": jev_timeout_ms},
    }
    settings, problems = load_settings(raw)
    assert not problems
    signals = Signals()
    spawned: list = []
    intake = Intake(
        lambda: settings,
        signals,
        jev=jev,
        spawn=lambda coro: spawned.append(coro),
        store=store,
    )
    return intake, signals, spawned


async def _drain(spawned: list) -> None:
    for coro in list(spawned):
        await coro


class TestIntakeSlowPath:
    @pytest.mark.asyncio
    async def test_confident_none_records_nothing(self, store: Store, frozen_now: float) -> None:
        jev = _FakeJev({"kind": ("none", 0.95, 0.92)})
        intake, signals, spawned = _make_intake(store, jev=jev)
        await intake.handle(hook_message(is_at=True, text="哈哈哈你说得对", message_id="m-none"))
        await _drain(spawned)
        assert intake.slow_queue == []
        assert _pending_rows(store) == []
        assert _events(store, "intake.slow") == []

    @pytest.mark.asyncio
    async def test_timeout_goes_pending_asks(self, store: Store, frozen_now: float) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)}, delay_s=2.0)
        intake, signals, spawned = _make_intake(store, jev=jev, jev_timeout_ms=300)
        await intake.handle(
            hook_message(is_at=True, user_id="20002", nickname="阿柒", message_id="m-slow", text="@她 帮我整理")
        )
        rows = _pending_rows(store)
        assert len(rows) == 1
        r = rows[0]
        assert r["message_id"] == "m-slow"
        assert r["user_id"] == "20002"
        assert r["user_name"] == "阿柒"
        assert r["text"] == "@她 帮我整理"
        assert r["reason"] == "jev_无答案"
        assert r["handled"] == 0
        assert r["ts"] == T0
        # slow_queue 属性返回未处理的行（兼容老调用）
        sq = intake.slow_queue
        assert len(sq) == 1 and sq[0]["message_id"] == "m-slow" and sq[0]["user_name"] == "阿柒"
        assert _events(store, "intake.slow") == []  # 不再写事件

    @pytest.mark.asyncio
    async def test_invalid_answer_goes_pending(self, store: Store, frozen_now: float) -> None:
        jev = _FakeJev({"kind": "not-a-tuple"})
        intake, _, _ = _make_intake(store, jev=jev)
        await intake.handle(hook_message(is_at=True, message_id="m-bad", text="@她 在吗"))
        assert [r["message_id"] for r in _pending_rows(store)] == ["m-bad"]

    @pytest.mark.asyncio
    async def test_low_confidence_goes_pending(self, store: Store, frozen_now: float) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.7, 0.4)})
        intake, _, _ = _make_intake(store, jev=jev)
        await intake.handle(hook_message(is_at=True, message_id="m-low", text="@她 也许帮我看看"))
        rows = _pending_rows(store)
        assert len(rows) == 1 and "0.40" in rows[0]["reason"]

    @pytest.mark.asyncio
    async def test_none_low_confidence_goes_pending(self, store: Store, frozen_now: float) -> None:
        jev = _FakeJev({"kind": ("none", 0.5, 0.4)})
        intake, _, _ = _make_intake(store, jev=jev)
        await intake.handle(hook_message(is_at=True, message_id="m-nlow", text="嗯嗯"))
        assert [r["message_id"] for r in _pending_rows(store)] == ["m-nlow"]

    @pytest.mark.asyncio
    async def test_same_message_upserts_not_duplicates(self, store: Store, frozen_now: float) -> None:
        jev = _FakeJev({"kind": ("prepare", 0.5, 0.3)})
        intake, _, _ = _make_intake(store, jev=jev)
        await intake.handle(hook_message(is_at=True, message_id="m-x", text="第一次"))
        await intake.handle(hook_message(is_at=True, message_id="m-x", text="第二次"))
        rows = _pending_rows(store)
        assert len(rows) == 1
        assert rows[0]["text"] == "第二次"  # 后到的覆盖

    @pytest.mark.asyncio
    async def test_confident_prepare_does_not_touch_pending(self, store: Store, frozen_now: float) -> None:
        # 快路径（Jev 有把握判 prepare）照走 approvals，不进 pending_asks
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)})
        raw = {"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{GID}"}]}}
        settings, _ = load_settings(raw)
        created: list = []

        class _A:
            def create(self, gid, **kw):
                created.append(kw)
                return {"id": "R-1", "status": "pending"}

        spawned: list = []
        intake = Intake(
            lambda: settings, Signals(), jev=jev, approvals=_A(),
            spawn=lambda coro: spawned.append(coro), store=store,
        )
        await intake.handle(hook_message(is_at=True, message_id="m-fast", text="@她 帮我整理"))
        await _drain(spawned)
        assert _pending_rows(store) == []
        assert created and created[0]["message_id"] == "m-fast"  # 走的还是快路径


# ----------------------------------------------------------------------
# profile 提炼：asks 提示词标注 + 落地
# ----------------------------------------------------------------------


class _Graph:
    """真依赖图：Tasks / Goals / Approvals / Outbox / Profiles（asks 接线）。"""

    def __init__(self, store: Store, host: FakeHost, models: FakeModelsQueue, settings) -> None:
        self.settings = settings
        self.tasks = Tasks(store, lambda: settings)
        self.goals = Goals(store, lambda: settings)
        self.approvals = Approvals(store, lambda: settings, self.tasks, self.goals)
        pushes = Pushes(store, lambda: settings)
        mentions = Mentions(store, lambda: settings)
        self.outbox = Outbox(store, host, pushes, mentions, lambda: settings)
        self.profiles = Profiles(store, host, models, lambda: settings)
        self.profiles.set_request_deps(approvals=self.approvals, goals=self.goals, outbox=self.outbox)


class TestAskPrompt:
    @pytest.mark.asyncio
    async def test_pending_asks_flagged_in_prompt_and_rules_declared(
        self, store: Store, frozen_now: float
    ) -> None:
        settings = _settings()
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        msgs = [_msg("m1", T0 - 100, text="随便聊聊"), _msg("m9", T0 - 90, user="u2", name="阿二", text="@她 在吗")]
        host = FakeHost(msgs)
        g = _Graph(store, host, models, settings)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO pending_asks (group_id, message_id, user_id, user_name, text, ts, reason, handled)"
                " VALUES (?, 'm9', 'u2', '阿二', '@她 在吗', ?, 'jev_无答案', 0)",
                (GID, T0 - 90),
            )
        g.profiles.ensure_group(GID)
        with store.tx() as conn:  # 手工攒够触发条件：pending 攒够、游标盖过这批消息
            conn.execute(
                "UPDATE groups SET pending_count=3, session_id='sess-1', cursor_ts=? WHERE group_id=?",
                (T0, GID),
            )
        ok = await g.profiles.refresh(GID, force=True)
        assert ok is True
        prompt = models.calls[0][1][1]["content"]
        # 未处理的那条标出来了，别的没标（只看 [序号] 开头的消息行；规则段也含这句话）
        msg_lines = [ln for ln in prompt.splitlines() if ln.lstrip().startswith("[")]
        flagged = [ln for ln in msg_lines if "这条 @ 了 MaiBot，Jev 没判出来，请你判断" in ln]
        assert len(flagged) == 1
        assert "@她 在吗" in flagged[0]
        normal_line = [ln for ln in msg_lines if "随便聊聊" in ln]
        assert normal_line and "Jev 没判出来" not in normal_line[0]
        # 规则里声明了 asks 和输出格式
        assert '"asks"' in prompt
        assert "prepare|goal|reminder" in prompt


class TestAskApply:
    @pytest.mark.asyncio
    async def test_prepare_ask_creates_pending_request(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = json.dumps(
            {"ops": [], "people": [],
             "asks": [{"i": 1, "kind": "prepare", "title": "整理铝坨坨选购清单"}]},
            ensure_ascii=False,
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"MaiBot 帮我整理清单{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        r = await g.profiles.tick(GID)
        assert r.refreshed is True
        rows = store.read().execute(
            "SELECT kind, title, requester_id, requester_name, message_id, via, status"
            " FROM requests WHERE group_id=?", (GID,)
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "task"
        assert row["title"] == "整理铝坨坨选购清单"
        assert row["requester_id"] == "u1"
        assert row["requester_name"] == "阿一"
        assert row["message_id"] == "m0"
        assert "主模型读群时发现" in row["via"]
        assert row["status"] == "pending"  # 默认要批准，未批准不开工

    @pytest.mark.asyncio
    async def test_other_plugin_command_never_becomes_request(self, store: Store, frozen_now: float) -> None:
        """线上实测（2026-09-27）：群友发 `/pic nsfw …`（MaiBot 画图插件的指令），
        主模型读群时把它当成了请求，建出一条待批。别的插件的 / 指令一律不当 MaiWork 的活。"""
        settings = _settings()
        reply = json.dumps({"ops": [], "people": [], "asks": [
            {"i": 1, "kind": "prepare", "title": "生成图"},
            {"i": 2, "kind": "prepare", "title": "生成图2"},
            {"i": 3, "kind": "reminder", "title": "提醒", "when": "2026-09-27 09:00"},
        ]}, ensure_ascii=False)
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([
            _msg("m0", T0 - 100, text="/pic nsfw 画个东雪莲"),
            _msg("m1", T0 - 90, text="  ／draw 猫"),
            _msg("m2", T0 - 80, text="!remind 明天九点"),
        ])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        assert store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert store.read().execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_prompt_marks_commands_as_not_for_maiwork(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        models = FakeModelsQueue(replies=['{"ops": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=t) for i, t in enumerate(["/pic 猫", "你好", "早"])])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        prompt = json.dumps(models.calls[0], ensure_ascii=False) if hasattr(models, "calls") else ""
        assert "别的插件的指令" in prompt

    @pytest.mark.asyncio
    async def test_goal_ask_creates_goal_kind_request(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = '{"ops": [], "asks": [{"i": 2, "kind": "goal", "title": "盯着铝价"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        rows = store.read().execute(
            "SELECT kind, title, message_id FROM requests WHERE group_id=?", (GID,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "goal"
        assert rows[0]["title"] == "盯着铝价"
        assert rows[0]["message_id"] == "m1"

    @pytest.mark.asyncio
    async def test_same_message_not_duplicated(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = '{"asks": [{"i": 1, "kind": "prepare", "title": "整理清单"}]}'
        models = FakeModelsQueue(replies=[reply, reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        n1 = store.read().execute(
            "SELECT COUNT(*) AS c FROM requests WHERE group_id=?", (GID,)
        ).fetchone()["c"]
        assert n1 == 1
        # 强制再提炼同一批（游标清零重新回读），同一条消息不该再建
        with store.tx() as conn:
            conn.execute("UPDATE groups SET last_refresh_ts=0 WHERE group_id=?", (GID,))
        await g.profiles.refresh(GID, force=True)
        n2 = store.read().execute(
            "SELECT COUNT(*) AS c FROM requests WHERE group_id=?", (GID,)
        ).fetchone()["c"]
        assert n2 == 1

    @pytest.mark.asyncio
    async def test_existing_request_from_fast_path_blocks_duplicate(
        self, store: Store, frozen_now: float
    ) -> None:
        settings = _settings()
        reply = '{"asks": [{"i": 1, "kind": "prepare", "title": "整理清单"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        # intake 快路径已经给 m0 建过请求（任意状态）
        g.approvals.create(
            GID, kind="task", title="已有请求", quote="", via="群里 @ · Jev 判断是「准备东西」",
            requester_id="u1", requester_name="阿一", message_id="m0",
        )
        await g.profiles.tick(GID)
        rows = store.read().execute(
            "SELECT title FROM requests WHERE group_id=? AND message_id='m0'", (GID,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "已有请求"  # 没有被读群发现的覆盖 / 加一

    @pytest.mark.asyncio
    async def test_reminder_ask_creates_member_goal_and_replies(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = (
            '{"asks": [{"i": 1, "kind": "reminder", "title": "交周报", "when": "2026-09-27 09:00"}]}'
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg("m1", T0 - 60, user="u7", name="阿七", text="MaiBot 提醒我明早交周报")]
                        + [_msg(f"m{i}", T0 - 50 + i, text="x") for i in range(2, 4)])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        want_ts = _parse_bj_when("2026-09-27 09:00")
        assert want_ts is not None
        rows = store.read().execute(
            "SELECT kind, who_id, who_name, title, due_ts, remind_ts, state FROM goals WHERE group_id=?",
            (GID,),
        ).fetchall()
        assert len(rows) == 1
        g0 = rows[0]
        assert g0["kind"] == "member"
        assert g0["who_id"] == "u7"
        assert g0["who_name"] == "阿七"
        assert g0["title"] == "交周报"
        assert g0["due_ts"] == want_ts
        assert g0["remind_ts"] == want_ts
        # outbox 里有一句固定回话（reply_to 原消息、push_kind=status）
        oks = store.read().execute(
            "SELECT key, group_id, kind, payload FROM outbox WHERE key LIKE 'ask-remind:%'"
        ).fetchall()
        assert len(oks) == 1
        payload = json.loads(oks[0]["payload"])
        assert payload["text"] == "记下了，2026-09-27 09:00 提醒你"
        assert payload["reply_to"] == "m1"
        assert payload["push_kind"] == "status"

    @pytest.mark.asyncio
    async def test_reminder_not_duplicated_on_second_read(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = '{"asks": [{"i": 1, "kind": "reminder", "title": "交周报", "when": "2026-09-27 09:00"}]}'
        models = FakeModelsQueue(replies=[reply, reply])
        host = FakeHost([_msg("m1", T0 - 60, text="提醒我交周报")] + [_msg("m2", T0 - 50, text="x"), _msg("m3", T0 - 40, text="y")])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        with store.tx() as conn:
            conn.execute("UPDATE groups SET last_refresh_ts=0 WHERE group_id=?", (GID,))
        await g.profiles.refresh(GID, force=True)
        n = store.read().execute(
            "SELECT COUNT(*) AS c FROM goals WHERE group_id=?", (GID,)
        ).fetchone()["c"]
        assert n == 1
        o = store.read().execute(
            "SELECT COUNT(*) AS c FROM outbox WHERE key LIKE 'ask-remind:%'"
        ).fetchone()["c"]
        assert o == 1

    @pytest.mark.asyncio
    async def test_reminder_without_when_skipped(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = '{"asks": [{"i": 1, "kind": "reminder", "title": "交周报", "when": ""}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        assert store.read().execute("SELECT COUNT(*) AS c FROM goals WHERE group_id=?", (GID,)).fetchone()["c"] == 0
        assert store.read().execute("SELECT COUNT(*) AS c FROM outbox", ()).fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_plain_discussion_creates_nothing(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])  # 没 asks
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"谁来整理一下{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        assert store.read().execute("SELECT COUNT(*) AS c FROM requests", ()).fetchone()["c"] == 0
        assert store.read().execute("SELECT COUNT(*) AS c FROM goals", ()).fetchone()["c"] == 0
        assert store.read().execute("SELECT COUNT(*) AS c FROM outbox", ()).fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_bot_message_ask_ignored(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        # 模型把第 1 条（机器人自己的消息）也报成请求：必须被忽略
        reply = (
            '{"asks": ['
            '{"i": 1, "kind": "prepare", "title": "机器人消息不算"},'
            '{"i": 2, "kind": "prepare", "title": "真人消息算"}'
            "]}"
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([
            _msg("m0", T0 - 100, bot=True, text="我是机器人"),
            _msg("m1", T0 - 90, text="MaiBot 帮我整理"),
            _msg("m2", T0 - 80, text="路过"),
        ])
        g = _Graph(store, host, models, settings)
        await g.profiles.tick(GID)
        rows = store.read().execute("SELECT title, message_id FROM requests", ()).fetchall()
        assert len(rows) == 1
        assert rows[0]["message_id"] == "m1"

    @pytest.mark.asyncio
    async def test_non_served_group_never_applied(self, store: Store, frozen_now: float) -> None:
        settings = _settings(serve=(GID,))  # 只服务 GID
        reply = '{"asks": [{"i": 1, "kind": "prepare", "title": "不该落地"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg("x1", T0 - 100, text="MaiBot 帮我整理")])
        g = _Graph(store, host, models, settings)
        # 非服务群 999999 直接调 refresh（库里手工给它建行，走到模型那一步）
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, workspace, session_id, token, created,"
                " cursor_ts, pending_count) VALUES ('999999', 'tinker', 'sess-1', 'tok99999', ?, ?, 1)",
                (T0, T0 - 10),
            )
        await g.profiles.refresh("999999", force=True)
        assert store.read().execute("SELECT COUNT(*) AS c FROM requests", ()).fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_pending_ask_marked_handled_after_judged(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = (
            '{"asks": [{"i": 1, "kind": "prepare", "title": "整理这份资料"}]}'
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg("m1", T0 - 60, user="u2", name="阿二", text="@她 帮我整理这份资料")]
                        + [_msg("m2", T0 - 50, text="x"), _msg("m3", T0 - 40, text="y")])
        g = _Graph(store, host, models, settings)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO pending_asks (group_id, message_id, user_id, user_name, text, ts, reason, handled)"
                " VALUES (?, 'm1', 'u2', '阿二', '@她 帮我整理这份资料', ?, 'jev_无答案', 0)",
                (GID, T0 - 60),
            )
        await g.profiles.tick(GID)
        row = store.read().execute(
            "SELECT handled FROM pending_asks WHERE group_id=? AND message_id='m1'", (GID,)
        ).fetchone()
        assert row["handled"] == 1
        # 且确实建了待批
        rows = store.read().execute(
            "SELECT title, requester_name FROM requests WHERE message_id='m1'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["requester_name"] == "阿二"

    @pytest.mark.asyncio
    async def test_pending_ask_unjudged_stays_unhandled(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])  # 模型没判它
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"消息{i}") for i in range(3)])
        g = _Graph(store, host, models, settings)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO pending_asks (group_id, message_id, user_id, user_name, text, ts, reason, handled)"
                " VALUES (?, 'm1', 'u1', '阿一', '@她 在吗', ?, 'jev_无答案', 0)",
                (GID, T0 - 90),
            )
        await g.profiles.tick(GID)
        row = store.read().execute(
            "SELECT handled FROM pending_asks WHERE group_id=? AND message_id='m1'", (GID,)
        ).fetchone()
        assert row["handled"] == 0  # 没被说中的留着，下次再判

    @pytest.mark.asyncio
    async def test_unwired_profiles_ignores_asks_safely(self, store: Store, frozen_now: float) -> None:
        settings = _settings()
        reply = '{"asks": [{"i": 1, "kind": "prepare", "title": "没人接"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i * 10, text=f"消息{i}") for i in range(3)])
        p = Profiles(store, host, models, lambda: settings)  # 没 set_request_deps
        r = await p.tick(GID)
        assert r.refreshed is True  # 提炼照常
        assert store.read().execute("SELECT COUNT(*) AS c FROM requests", ()).fetchone()["c"] == 0


class TestParseBjWhen:
    def test_ok(self) -> None:
        ts = _parse_bj_when("2026-09-27 09:00")
        assert ts is not None
        d = clock.bj(ts)
        assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 9, 27, 9, 0)

    def test_garbage(self) -> None:
        assert _parse_bj_when("") is None
        assert _parse_bj_when("明天早上九点") is None
        assert _parse_bj_when("2026-13-45 99:99") is None


# ----------------------------------------------------------------------
# app 启动接线：profiles 拿到 approvals/goals/outbox
# ----------------------------------------------------------------------


def _raw(data_dir: Path, listen: str = "127.0.0.1:18663") -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "console": {"listen": listen, "password": "pw-测试"},
        "storage": {"data_dir": str(data_dir)},
    }


class TestAppWiring:
    @pytest.mark.asyncio
    async def test_profiles_get_request_deps_after_start(self, tmp_path: Path) -> None:
        profiles = FakeProfiles()
        app = MaiWorkApp(FakeCtx({}), _raw(tmp_path / "data"), plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_factory = lambda *a, **kw: profiles
        await app.start()
        try:
            assert profiles.request_deps is not None
            ap, go, ob = profiles.request_deps
            assert ap is app.approvals and go is app.goals and ob is app.outbox
        finally:
            await app.stop()
