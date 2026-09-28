"""persona.py 测试：关注成员发言留存（focus_messages）、到期刷新、提示词、隐私红线。

时钟钉在 T0（2026-09-26 14:05 北京时间）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.profile import Profiles
from CharTyr_MaiWork.maiwork.persona import Personas

from fakes import FakeHost, FakeModels, FakeModelsQueue

GID = "900000001"
T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()
assert clock.day_key(T0) == "2026-09-26"


def _settings(**focus_over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "profile": {"backfill_days": 30, "backfill_max_messages": 1500},
    }
    if focus_over:
        raw["focus"] = focus_over
    settings, problems = load_settings(raw)
    assert not problems
    return settings


SETTINGS = _settings()


def _msg(
    mid: str,
    ts: float,
    user: str = "u1",
    name: str = "阿一",
    *,
    bot: bool = False,
    at: bool = False,
    reply: str = "",
    text: str = "你好",
) -> Msg:
    return Msg(
        id=mid,
        ts=ts,
        user_id=user,
        user_name=name,
        text=text,
        is_bot=bot,
        is_at=at,
        is_picture=False,
        reply_to=reply,
    )


def _msgs_of(user: str, name: str, n: int, *, start: float, step: int = 60, text: str = "") -> list[Msg]:
    return [
        _msg(f"{user}-{i}", start + i * step, user=user, name=name, text=text or f"{name}的第{i}句")
        for i in range(n)
    ]


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


def _focus_messages(store: Store, user_id: str, gid: str = GID) -> list[dict]:
    rows = store.read().execute(
        "SELECT user_id, ts, message_id, text FROM focus_messages"
        " WHERE group_id=? AND user_id=? ORDER BY ts",
        (gid, user_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _focus_member(store: Store, user_id: str, gid: str = GID):
    return store.read().execute(
        "SELECT * FROM focus_members WHERE group_id=? AND user_id=?",
        (gid, user_id),
    ).fetchone()


async def _setup_focus_with_messages(store: Store, user: str, name: str, n: int, *, host=None) -> Profiles:
    """先 tick（空消息）建群、再把人 pin 成关注成员、再 tick 读 n 条该人消息。"""
    p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
    await p0.tick(GID)
    p0.set_focus(GID, user, "add")
    host = host or FakeHost(_msgs_of(user, name, n, start=T0 - 1000))
    p = Profiles(store, host, FakeModels(False), lambda: SETTINGS)
    await p.tick(GID)
    return p


# ----------------------------------------------------------------------
# focus_messages：tick 只在「关注成员 + personal_profile 开」时存；清理 14 天 / 300 条
# ----------------------------------------------------------------------


class TestFocusMessagesStorage:
    @pytest.mark.asyncio
    async def test_tick_stores_only_focus_members_when_personal_profile_on(
        self, store: Store, frozen_now: float
    ) -> None:
        msgs = _msgs_of("ua", "小A", 4, start=T0 - 1000) + _msgs_of("ub", "小B", 6, start=T0 - 900)
        host = FakeHost(msgs)
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        p = Profiles(store, host, FakeModels(False), lambda: SETTINGS)
        await p.tick(GID)
        got_a = _focus_messages(store, "ua")
        got_b = _focus_messages(store, "ub")
        assert len(got_a) == 4
        assert got_b == []
        assert all(len(r["text"]) <= 300 for r in got_a)
        assert {r["message_id"] for r in got_a} == {f"ua-{i}" for i in range(4)}

    @pytest.mark.asyncio
    async def test_tick_does_not_store_when_personal_profile_off(
        self, store: Store, frozen_now: float
    ) -> None:
        settings_off = _settings(personal_profile=False)
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: settings_off)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        host = FakeHost(_msgs_of("ua", "小A", 5, start=T0 - 1000))
        p = Profiles(store, host, FakeModels(False), lambda: settings_off)
        await p.tick(GID)
        assert _focus_messages(store, "ua") == []

    @pytest.mark.asyncio
    async def test_tick_prunes_messages_older_than_14_days(
        self, store: Store, frozen_now: float
    ) -> None:
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        with store.tx() as conn:
            for i in range(2):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 15 * 86400 + i, f"old-{i}", "旧话"),
                )
            for i in range(3):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 100 + i, f"recent-{i}", "新话"),
                )
        host = FakeHost(_msgs_of("ua", "小A", 1, start=T0 - 5))
        p = Profiles(store, host, FakeModels(False), lambda: SETTINGS)
        await p.tick(GID)
        rows = _focus_messages(store, "ua")
        ids = {r["message_id"] for r in rows}
        assert "old-0" not in ids and "old-1" not in ids
        assert {"recent-0", "recent-1", "recent-2"}.issubset(ids)
        assert "ua-0" in ids

    @pytest.mark.asyncio
    async def test_tick_keeps_at_most_300_messages_per_person(
        self, store: Store, frozen_now: float
    ) -> None:
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        with store.tx() as conn:
            for i in range(301):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 2000 + i, f"old-{i}", f"第{i}句"),
                )
        host = FakeHost(_msgs_of("ua", "小A", 1, start=T0 - 5))
        p = Profiles(store, host, FakeModels(False), lambda: SETTINGS)
        await p.tick(GID)
        rows = _focus_messages(store, "ua")
        assert len(rows) == 300
        assert any(r["message_id"] == "ua-0" for r in rows)

    @pytest.mark.asyncio
    async def test_tick_text_truncated_to_300_chars(
        self, store: Store, frozen_now: float
    ) -> None:
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        host = FakeHost([_msg("ua-x", T0 - 10, user="ua", name="小A", text="长" * 500)])
        p = Profiles(store, host, FakeModels(False), lambda: SETTINGS)
        await p.tick(GID)
        rows = _focus_messages(store, "ua")
        assert rows and rows[0]["text"] == "长" * 300


# ----------------------------------------------------------------------
# 落选 / 关掉：persona 和 focus_messages 一起删
# ----------------------------------------------------------------------


class TestCleanupOnDropout:
    @pytest.mark.asyncio
    async def test_dropout_deletes_focus_messages_and_persona(
        self, store: Store, frozen_now: float
    ) -> None:
        await _setup_focus_with_messages(store, "ua", "小A", 4)
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=? WHERE group_id=? AND user_id=?",
                ('{"summary":"在忙"}', T0 - 100, GID, "ua"),
            )
        assert _focus_messages(store, "ua")
        p = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        p.set_focus(GID, "ua", "auto")
        clock.now = lambda: T0 + 31 * 86400
        got = p.focus(GID)
        assert "ua" not in {m["user_id"] for m in got}
        assert _focus_member(store, "ua") is None
        assert _focus_messages(store, "ua") == []

    @pytest.mark.asyncio
    async def test_personal_profile_off_deletes_focus_messages_and_persona(
        self, store: Store, frozen_now: float
    ) -> None:
        await _setup_focus_with_messages(store, "ua", "小A", 3)
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=? WHERE group_id=? AND user_id=?",
                ('{"summary":"在忙"}', T0 - 10, GID, "ua"),
            )
        assert _focus_messages(store, "ua")
        settings_off = _settings(personal_profile=False)
        p2 = Profiles(store, FakeHost([]), FakeModels(False), lambda: settings_off)
        await p2.tick(GID)
        p2.focus(GID)
        assert _focus_messages(store, "ua") == []
        row = _focus_member(store, "ua")
        assert row is not None
        assert row["persona"] == "" and float(row["persona_ts"] or 0) == 0 and row["note"] == ""

    @pytest.mark.asyncio
    async def test_set_focus_remove_deletes_focus_messages_and_persona(
        self, store: Store, frozen_now: float
    ) -> None:
        p = await _setup_focus_with_messages(store, "ua", "小A", 3)
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=? WHERE group_id=? AND user_id=?",
                ('{"summary":"悄悄话"}', T0 - 10, GID, "ua"),
            )
        p.set_focus(GID, "ua", "remove")
        assert _focus_messages(store, "ua") == []
        row = _focus_member(store, "ua")
        assert row is not None
        assert row["persona"] == "" and float(row["persona_ts"] or 0) == 0
        assert row["note"] == "" and row["removed"] == 1


# ----------------------------------------------------------------------
# Personas.due：到期判断
# ----------------------------------------------------------------------


class TestPersonaDue:
    @pytest.mark.asyncio
    async def test_due_requires_24h_since_last_refresh_and_5_new_messages(
        self, store: Store, frozen_now: float
    ) -> None:
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        personas = Personas(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)

        assert personas.due(GID, T0) == []

        with store.tx() as conn:
            for i in range(6):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 300 + i, f"m-{i}", f"第{i}句"),
                )
        assert personas.due(GID, T0) == ["ua"]

        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona_ts=? WHERE group_id=? AND user_id=?",
                (T0 - 2 * 3600, GID, "ua"),
            )
        assert personas.due(GID, T0) == []

        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona_ts=? WHERE group_id=? AND user_id=?",
                (T0 - 25 * 3600, GID, "ua"),
            )
            conn.execute("DELETE FROM focus_messages WHERE group_id=?", (GID,))
            for i in range(3):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 1800 + i * 10, f"n-{i}", f"新{i}"),
                )
        assert personas.due(GID, T0) == []

        with store.tx() as conn:
            for i in range(3, 5):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 900 + i * 10, f"n-{i}", f"新{i}"),
                )
        assert personas.due(GID, T0) == ["ua"]

    @pytest.mark.asyncio
    async def test_due_ignores_removed_members_and_cleans_their_rows(
        self, store: Store, frozen_now: float
    ) -> None:
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        p0.set_focus(GID, "ua", "remove")
        with store.tx() as conn:
            for i in range(6):
                conn.execute(
                    "INSERT OR IGNORE INTO focus_messages (group_id, user_id, ts, message_id, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (GID, "ua", T0 - 500 + i, f"x-{i}", "旧话"),
                )
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=? WHERE group_id=? AND user_id=?",
                ('{"summary":"旧"}', T0 - 3 * 86400, GID, "ua"),
            )
        personas = Personas(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        assert personas.due(GID, T0) == []
        assert _focus_messages(store, "ua") == []
        row = _focus_member(store, "ua")
        assert row is not None and row["persona"] == "" and float(row["persona_ts"] or 0) == 0

    @pytest.mark.asyncio
    async def test_due_returns_empty_when_personal_profile_off(
        self, store: Store, frozen_now: float
    ) -> None:
        settings_off = _settings(personal_profile=False)
        personas = Personas(store, FakeHost([]), FakeModels(False), lambda: settings_off)
        assert personas.due(GID, T0) == []


# ----------------------------------------------------------------------
# Personas.refresh：素材、提示词、knowledge 无 person_id、入库
# ----------------------------------------------------------------------


class TestPersonaRefresh:
    @pytest.mark.asyncio
    async def test_refresh_prompt_contains_messages_and_memory(
        self, store: Store, frozen_now: float
    ) -> None:
        knowledge_calls: list = []

        class _FakeHostWithKnowledge(FakeHost):
            async def knowledge(self, query, **kwargs):
                knowledge_calls.append((query, kwargs))
                return "小A 前阵子在学 Rust，还给群友推荐过书"

        msgs = _msgs_of("ua", "小A", 6, start=T0 - 700)
        host = _FakeHostWithKnowledge(msgs)
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        p = Profiles(store, host, FakeModels(False), lambda: SETTINGS)
        await p.tick(GID)
        # 线上 focus() 会把 member_activity 里的最近名字写进 focus_members；这里直接同步
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET name=? WHERE group_id=? AND user_id=?",
                ("小A", GID, "ua"),
            )

        payload = {
            "summary": "在学 Rust，热心帮人",
            "doing": ["学 Rust"],
            "cares": ["系统编程"],
            "asked": ["希望有简版教材"],
            "style": "话少",
        }
        models = FakeModelsQueue(replies=[json.dumps(payload, ensure_ascii=False)])
        personas = Personas(store, host, models, lambda: SETTINGS)
        ok = await personas.refresh(GID, "ua")
        assert ok is True

        assert len(knowledge_calls) == 1
        query, kwargs = knowledge_calls[0]
        assert "小A" in query
        assert "最近在做什么" in query and "关心什么" in query
        assert "person_id" not in kwargs
        assert kwargs.get("chat_id") == "sess-1"
        assert kwargs.get("limit") == 6

        _, messages, _ = models.calls[0]
        dump = json.dumps(messages, ensure_ascii=False)
        assert "第0句" in dump
        assert "前阵子在学 Rust" in dump  # 记忆检索文本原样带进了提示词

        row = _focus_member(store, "ua")
        persona = json.loads(row["persona"])
        assert persona == payload
        assert row["note"] == "在学 Rust，热心帮人"
        assert float(row["persona_ts"]) > 0

    @pytest.mark.asyncio
    async def test_refresh_skips_when_model_not_ready(
        self, store: Store, frozen_now: float
    ) -> None:
        await _setup_focus_with_messages(store, "ua", "小A", 3)
        personas = Personas(store, FakeHost([]), FakeModels(ready=False), lambda: SETTINGS)
        assert await personas.refresh(GID, "ua") is False
        row = _focus_member(store, "ua")
        assert row["persona"] == "" and float(row["persona_ts"] or 0) == 0

    @pytest.mark.asyncio
    async def test_refresh_skips_removed_and_non_member(
        self, store: Store, frozen_now: float
    ) -> None:
        models = FakeModelsQueue(replies=[json.dumps({"summary": "x"})])
        personas = Personas(store, FakeHost([]), models, lambda: SETTINGS)
        assert await personas.refresh(GID, "ghost") is False
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        p0.set_focus(GID, "ua", "remove")
        assert await personas.refresh(GID, "ua") is False
        assert models.calls == []

    @pytest.mark.asyncio
    async def test_refresh_truncates_overlong_lists_and_items(
        self, store: Store, frozen_now: float
    ) -> None:
        await _setup_focus_with_messages(store, "ua", "小A", 3)
        payload = {
            "summary": "s" * 200,
            "doing": ["d" * 80 for _ in range(8)],
            "cares": [],
            "asked": ["a" * 50],
            "style": "s" * 60,
        }
        models = FakeModelsQueue(replies=[json.dumps(payload)])
        personas = Personas(store, FakeHost([]), models, lambda: SETTINGS)
        ok = await personas.refresh(GID, "ua")
        assert ok is True
        row = _focus_member(store, "ua")
        persona = json.loads(row["persona"])
        assert len(persona["doing"]) == 5
        assert all(len(s) <= 40 for s in persona["doing"])
        assert len(persona["summary"]) <= 120
        assert len(persona["style"]) <= 40
        assert persona["asked"] == ["a" * 40]

    @pytest.mark.asyncio
    async def test_refresh_bad_json_returns_false(self, store: Store, frozen_now: float) -> None:
        await _setup_focus_with_messages(store, "ua", "小A", 3)
        models = FakeModelsQueue(replies=["这不是 JSON"])
        personas = Personas(store, FakeHost([]), models, lambda: SETTINGS)
        assert await personas.refresh(GID, "ua") is False
        row = _focus_member(store, "ua")
        assert row["persona"] == ""
        assert float(row["persona_ts"] or 0) == 0


# ----------------------------------------------------------------------
# profile.py：persona 存在时，people 注记不再覆盖 note（persona 的 summary 优先）
# ----------------------------------------------------------------------


class TestPeopleNoteNotOverwritingPersona:
    @pytest.mark.asyncio
    async def test_people_note_skipped_when_persona_present(
        self, store: Store, frozen_now: float
    ) -> None:
        """persona 要塞：群画像提炼时输出的 people 注记不得盖掉 persona 的 summary。"""
        await _setup_focus_with_messages(store, "ua", "小A", 3)
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=?, note=? WHERE group_id=? AND user_id=?",
                ('{"summary":"在学Rust"}', T0 - 60, "在学Rust", GID, "ua"),
            )
        p = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        with store.tx() as conn:
            wrote = p._apply_people(conn, GID, [{"user_id": "ua", "note": "换了新的注记"}], T0)
        assert wrote is False  # 不算「写了注记」
        row = _focus_member(store, "ua")
        assert row["note"] == "在学Rust", "persona 的 summary 优先，people 注记没盖掉"

    @pytest.mark.asyncio
    async def test_people_note_written_when_no_persona_yet(
        self, store: Store, frozen_now: float
    ) -> None:
        """没有 persona 时，people 注记照常写进 note（兼容旧显示）。"""
        await _setup_focus_with_messages(store, "ua", "小A", 3)
        p = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        with store.tx() as conn:
            wrote = p._apply_people(conn, GID, [{"user_id": "ua", "note": "他还是爱聊硬件"}], T0)
        assert wrote is True
        row = _focus_member(store, "ua")
        assert row["note"] == "他还是爱聊硬件"
        assert row["persona"] == "", "people 注记不产生 persona，只是 note"


# ----------------------------------------------------------------------
# PROFILE-<群号>.md：persona / focus_members 内容不进文件（管理员也要打开、
# 共享工作区的群里派过 agent 能看到）
# ----------------------------------------------------------------------


class TestProfileMdExcludesPersona:
    @pytest.mark.asyncio
    async def test_profile_md_has_no_persona_summary(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        ws_root = tmp_path / "wsroot"
        (ws_root / "ws-tinker").mkdir(parents=True)
        settings = load_settings({
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "ws-tinker"}]},
            "environments": {"workspace_root": str(ws_root)},
            "profile": {"backfill_days": 30},
        })[0]
        p0 = Profiles(store, FakeHost([]), FakeModels(False), lambda: settings)
        await p0.tick(GID)
        p0.set_focus(GID, "ua", "add")
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET name='小A', persona=?, persona_ts=?, note=? WHERE group_id=? AND user_id=?",
                ('{"summary":"这人超爱Python深夜调试"}', T0 - 30, "这人超爱Python深夜调试", GID, "ua"),
            )
            conn.execute(
                "INSERT INTO profile_entries (group_id, category, text, evidence_count, first_ts, last_ts, locked, deleted, source, updated)"
                " VALUES (?, 'interest', '全员爱折腾', 0, ?, ?, 0, 0, 'model', ?)",
                (GID, T0 - 1000, T0 - 1000, T0 - 1000),
            )
        p0._write_profile_md(GID)
        md_path = ws_root / "ws-tinker" / f"PROFILE-{GID}.md"
        assert md_path.is_file(), "PROFILE-<群号>.md 该写出来"
        md = md_path.read_text(encoding="utf-8")
        assert "Python" not in md, "persona 的 summary 绝不进 PROFILE"
        assert "小A" not in md, "关注成员名字不进 PROFILE"
        assert "全员爱折腾" in md


# ----------------------------------------------------------------------
# console：GroupView.focus 带 persona（仅管理员）；群友视图没有 focus
# ----------------------------------------------------------------------


class TestGroupViewFocusPersona:
    """只走 views._focus_list（GroupView.focus 的填装点）：管理员专属的 persona 字段长这样。"""

    def _svc(self, store: Store):
        class _Svc:
            def __init__(self, s: Store) -> None:
                self.profiles = Profiles(s, FakeHost([]), FakeModels(False), lambda: SETTINGS)

        return _Svc(store)

    def test_focus_items_have_persona_or_null(self, store: Store, frozen_now: float) -> None:
        from CharTyr_MaiWork.maiwork.console import views as view_mod

        p = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        p.remember_session(GID, "sess-1", T0)
        p.set_focus(GID, "ua", "add")
        p.set_focus(GID, "ub", "add")
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=?, note=? WHERE group_id=? AND user_id=?",
                ('{"summary":"在玩Go","doing":["搭监控"],"cares":["系统"],"asked":[],"style":"话少"}',
                 T0 - 10, "在玩Go", GID, "ua"),
            )
        out = view_mod._focus_list(self._svc(store), GID)
        by_id = {m["user_id"]: m for m in out}
        pa = by_id["ua"]["persona"]
        assert pa is not None
        assert pa == {
            "summary": "在玩Go",
            "doing": ["搭监控"],
            "cares": ["系统"],
            "asked": [],
            "style": "话少",
            "updated_ts": T0 - 10,
        }
        assert by_id["ub"]["persona"] is None, "还没做过画像的是 null"

    def test_bad_persona_json_gives_null(self, store: Store, frozen_now: float) -> None:
        """库里 persona 是残缺 JSON（或根本不是 JSON）：不顶 view 崩，是 null。"""
        from CharTyr_MaiWork.maiwork.console import views as view_mod

        p = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        p.remember_session(GID, "sess-1", T0)
        p.set_focus(GID, "ua", "add")
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=? WHERE group_id=? AND user_id=?",
                ("{这不是合法json", GID, "ua"),
            )
        out = view_mod._focus_list(self._svc(store), GID)
        assert out[0]["persona"] is None

    def test_member_view_real_group_view_drops_focus(self, store: Store, frozen_now: float) -> None:
        """真实 views.group_view（admin=False）→ focus 整个字段被拿走，persona 绝无可能泄露。"""
        from CharTyr_MaiWork.maiwork.console import views as view_mod

        p = Profiles(store, FakeHost([]), FakeModels(False), lambda: SETTINGS)
        p.remember_session(GID, "sess-1", T0)
        p.set_focus(GID, "ua", "add")
        with store.tx() as conn:
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=? WHERE group_id=? AND user_id=?",
                ('{"summary":"在玩Go的秘密"}', T0 - 10, GID, "ua"),
            )

        class _Svc:
            def __init__(self, s: Store) -> None:
                self.store = s  # views 走 svc.store.read()
                self.profiles = Profiles(s, FakeHost([]), FakeModels(False), lambda: SETTINGS)
                self.signals = type("S", (), {"last_ts": lambda self_, gid: 0.0})()  # noqa: ARG005
                self.feeds = None
                self.topics = None
                self.goals = None
                self.tasks = None
                self.approvals = None
                self.delivery = None
                self.scheduler = None
                self.models = FakeModels(False)

            def get_settings(self):
                return SETTINGS

        svc = _Svc(store)
        view = view_mod.group_view(svc, GID, admin=False)
        assert "focus" not in view
        dump = json.dumps(view, ensure_ascii=False)
        assert "在玩Go的秘密" not in dump
        # persona 三个字也不会在群友视图里出现（除了服务群名等无关字段）
        assert '"persona"' not in dump


# ----------------------------------------------------------------------
# app.py 轮巡：每群每轮最多一个到期的刷新跑后台任务；同一群同刻只跑一个
# ----------------------------------------------------------------------


class TestAppPersonaRound:
    @pytest.mark.asyncio
    async def test_app_spawns_at_most_one_refresh_per_group_per_round(
        self, store: Store, frozen_now: float
    ) -> None:
        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        # 不用 _app / FakeCtx，直接用 MaiWorkApp 但只验 _persona_round 这一块
        app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
        app._settings = SETTINGS
        app.store = store

        spawned: list[str] = []

        class _FakePersonas:
            def due(self, group_id: str, now: float):
                return ["uaa", "ubb", "ucc"]

            async def refresh(self, group_id: str, user_id: str) -> bool:
                spawned.append(user_id)
                return True

        app.personas = _FakePersonas()
        app._persona_round(GID, T0)
        assert spawned == [], "_persona_round 只 spawn，不直接 refresh"
        # 后台任务起的
        import asyncio
        for _ in range(50):
            if spawned and (GID, "persona") not in app._running_jobs:
                break
            await asyncio.sleep(0.02)
        assert spawned == ["uaa"], f"每轮最多 spawn 一个：{spawned}"
        assert (GID, "persona") not in app._running_jobs, "干完就释放，下轮能再来"

    @pytest.mark.asyncio
    async def test_app_persona_round_one_at_a_time_and_drains_queue(
        self, store: Store, frozen_now: float
    ) -> None:
        """同一群同刻只跑一个：due=[A,B,C] 时，跑完 A 之前不能又 spawn 一个新的。"""
        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
        app._settings = SETTINGS
        app.store = store

        import asyncio
        started: list[str] = []

        class _SlowPersonas:
            def due(self, group_id: str, now: float):
                return ["uaa", "ubb"]

            async def refresh(self, group_id: str, user_id: str) -> bool:
                started.append(user_id)
                await asyncio.sleep(0.05)
                return True

        app.personas = _SlowPersonas()
        app._persona_round(GID, T0)
        # 在第一个跑完前再来两轮：不重复 spawn
        app._persona_round(GID, T0 + 30)
        app._persona_round(GID, T0 + 60)
        for _ in range(60):
            if len(started) >= 1:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        assert started == ["uaa"], f"同一群同刻只跑一个，跨轮顺序消化：{started}"

    @pytest.mark.asyncio
    async def test_app_persona_round_personal_profile_off_returns(
        self, store: Store, frozen_now: float
    ) -> None:
        """personal_profile 关掉时，完全不调 due、不 spawn。"""
        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        settings_off = _settings(personal_profile=False)
        app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
        app._settings = settings_off
        app.store = store

        called = {"due": 0}

        class _CountPersonas:
            def due(self, group_id: str, now: float):
                called["due"] += 1
                return ["uaa"]

            async def refresh(self, group_id: str, user_id: str) -> bool:
                return True

        app.personas = _CountPersonas()
        app._persona_round(GID, T0)
        assert called["due"] == 0, "开关关掉时 due 都不调"
        assert (GID, "persona") not in app._running_jobs

