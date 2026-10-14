"""构想的 kind（docs/18 §十，2026-10-11 用户拍板）：help（MaiWork 帮一步）/ talk（值得聊一聊）。

契约：
- 迁移：库号 38 给 ideas 加 kind（NOT NULL DEFAULT ''）；老行 / 老库升级后都是 ''；
- `clean_idea_kind`：只认 talk，别的（含空 / 乱写 / 数字）一律 help；
- 群向 make_idea：kind=help 要给 items 且照老规矩过可行性闸；kind=talk 不动手
  （items 丢掉、不查可行性），但仍要 evidence + 三段 basis；老式（kind 缺）当 help；
- talk 一律进话题候选池（不看 chat_worthy）；help 照旧看 chat_worthy；
- ideas_view（群友版 / 管理员版）都带 kind；老构想是 ''；
- `do`：talk 直接 ValueError（网页 400）；help 与老构想照常开工；
- 交接包（handoff.build_idea）对没有 items 的 talk 构想照样能生成。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.feeds import (
    IDEA_KIND_HELP,
    IDEA_KIND_TALK,
    clean_idea_kind,
)
from CharTyr_MaiWork.maiwork.store import _MIGRATIONS, Store

from fakes import FakeModelsQueue, idea_reply, seed_idea_chat
from test_feeds import GID, _make_feeds, _run, _TimePatch
from test_personal import (
    GID as PGID,
    UID,
    _make_personal,
    _personal_scores_json,
    _time_patch,
)
from test_personal import _run as _prun

NOW = 1_790_000_000.0


def _feeds_with_chat(tmp_path, reply: str):
    models = FakeModelsQueue(ready=True, replies=[reply])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, models=models)
    seed_idea_chat(store, GID, now=NOW)
    return store, feeds, models, workers, topics


# ----------------------------------------------------------------------
# 迁移：库号 38 = ideas.kind；39 = goal fit；40 = chat_log 去重账本（本文件只认 kind 那一步）
# ----------------------------------------------------------------------


class TestMigration:
    def test_latest_version_is_41(self) -> None:
        assert len(_MIGRATIONS) == 41, "库号只因构想 kind / goal fit / chat_log 去重账本 / 待判请求状态各加一步"
        assert _MIGRATIONS[37].__name__ == "_m_idea_kind"
        assert _MIGRATIONS[39].__name__ == "_m_chat_log_ids"

    def test_fresh_db_has_kind_default_empty(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        assert store.migrate() == 41
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(ideas)")}
        assert "kind" in cols
        assert int(store._conn.execute("PRAGMA user_version").fetchone()[0]) == 41
        # 不写 kind 的老式插入（老代码 / 老数据）拿到 ''
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, created, updated) VALUES (?, '老构想', 'b', 1, 1)",
                (GID,),
            )
        row = store.read().execute("SELECT kind FROM ideas").fetchone()
        assert row["kind"] == ""

    def test_old_db_upgrades_and_old_rows_get_empty(self, tmp_path) -> None:
        """库号 37 的老库（没有 kind 列）迁到最新：加列，老行 ''。"""
        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            for i, fn in enumerate(_MIGRATIONS[:37]):
                fn(conn)
                conn.execute(f"PRAGMA user_version={i + 1}")
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, created, updated) VALUES (?, '老构想', 'b', 1, 1)",
                (GID,),
            )
            assert "kind" not in {r["name"] for r in conn.execute("PRAGMA table_info(ideas)")}
        finally:
            conn.close()
        store = Store(path)
        assert store.migrate() == 41
        row = store.read().execute("SELECT title, kind FROM ideas").fetchone()
        assert row["title"] == "老构想" and row["kind"] == ""
        store.close()


# ----------------------------------------------------------------------
# 清洗：只认 talk，别的都是 help
# ----------------------------------------------------------------------


class TestCleanKind:
    def test_only_talk_is_talk(self) -> None:
        for raw in ("talk", "TALK", " talk ", "Talk"):
            assert clean_idea_kind(raw) == IDEA_KIND_TALK

    def test_everything_else_is_help(self) -> None:
        for raw in (None, "", "  ", "help", "HELP", "乱写", 42, "play"):
            assert clean_idea_kind(raw) == IDEA_KIND_HELP

    def test_constants(self) -> None:
        assert (IDEA_KIND_HELP, IDEA_KIND_TALK) == ("help", "talk")


# ----------------------------------------------------------------------
# 群向：help / talk 两种落库
# ----------------------------------------------------------------------


class TestMakeIdeaKind:
    def test_help_keeps_items_and_feasibility(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="help"))
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT kind, items, feasibility FROM ideas WHERE id=?", (got,)).fetchone()
        assert row["kind"] == "help"
        assert json.loads(row["items"])[0]["title"] == "查清故障原因"
        assert json.loads(row["feasibility"])["level"] == "ok"

    def test_help_without_items_is_blocked(self, tmp_path) -> None:
        """说了「帮一步」却没写要做哪一步：不入库，管理员在拦截名单里看得到。"""
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="help", items=[]))
        with _TimePatch():
            assert _run(feeds.make_idea(GID)) is None
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        blocked = store.kv_get(f"ideas.blocked.{GID}", [])
        assert len(blocked) == 1
        assert blocked[0]["reason"] == "说是帮一步，却没写要做哪一步"

    def test_unknown_kind_is_help(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="project"))
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT kind FROM ideas WHERE id=?", (got,)).fetchone()
        assert row["kind"] == "help"

    def test_talk_has_no_items_and_no_feasibility(self, tmp_path) -> None:
        reply = idea_reply(
            kind="talk",
            items=[{"kind": "task", "title": "这步不该留下", "desc": ""}],
            feasibility={"level": "maybe", "note": "做不到", "uses": [], "deliver": "doc", "needs_members": True},
        )
        store, feeds, *_r = _feeds_with_chat(tmp_path, reply)
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        row = store.read().execute("SELECT kind, items, feasibility FROM ideas WHERE id=?", (got,)).fetchone()
        assert row["kind"] == "talk"
        assert row["items"] == "[]" and row["feasibility"] == ""

    def test_talk_always_goes_to_topics(self, tmp_path) -> None:
        """话题进候选池不看 chat_worthy（它就等着冷场时被提起来）。"""
        _store, feeds, _m, _w, topics = _feeds_with_chat(
            tmp_path, idea_reply(kind="talk", chat_worthy=False)
        )
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        assert [c["ref_id"] for c in topics.calls] == [got]
        assert topics.calls[0]["kind"] == "idea"

    def test_help_still_respects_chat_worthy(self, tmp_path) -> None:
        _store, feeds, _m, _w, topics = _feeds_with_chat(
            tmp_path, idea_reply(kind="help", chat_worthy=False)
        )
        with _TimePatch():
            assert isinstance(_run(feeds.make_idea(GID)), int)
        assert topics.calls == []

    def test_views_expose_kind(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="talk"))
        with _TimePatch():
            got = _run(feeds.make_idea(GID))
        with store.tx() as conn:  # 老构想：没有 kind
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, created, updated) VALUES (?, '老构想', 'b', 1, 1)",
                (GID,),
            )
        by_id = {v["id"]: v for v in feeds.ideas_view(GID)}
        assert by_id[got]["kind"] == "talk"
        by_id_admin = {v["id"]: v for v in feeds.ideas_view(GID, admin=True)}
        assert by_id_admin[got]["kind"] == "talk"
        legacy_id = [i for i in by_id if i != got][0]
        assert by_id[legacy_id]["kind"] == ""


# ----------------------------------------------------------------------
# do：talk 没有 MaiWork 要动手的部分
# ----------------------------------------------------------------------


class TestIdeaActionKind:
    def _seed(self, store, *, kind: str, title: str, items: str = "[]") -> int:
        now = clock.now()
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, kind, icon, title, body, state, created, updated, items)"
                " VALUES (?, ?, 'bulb', ?, 'b', 'new', ?, ?, ?)",
                (GID, kind, title, now, now, items),
            )
            return int(cur.lastrowid or 0)

    def test_do_on_talk_raises(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="talk"))
        iid = self._seed(store, kind="talk", title="聊聊手柄")
        try:
            feeds.idea_action(iid, "do", by="管理员")
        except ValueError as e:
            assert str(e) == "这条是话题，没有 MaiWork 要动手的部分"
        else:
            raise AssertionError("talk 构想不该能开工")
        row = store.read().execute("SELECT state FROM ideas WHERE id=?", (iid,)).fetchone()
        assert row["state"] == "new"

    def test_do_on_help_still_works(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="talk"))
        iid = self._seed(
            store, kind="help", title="把手柄救活",
            items=json.dumps([{"kind": "task", "title": "查清故障", "desc": ""}], ensure_ascii=False),
        )
        out = feeds.idea_action(iid, "do", by="管理员")
        assert out["state"] == "started"

    def test_do_on_legacy_idea_unchanged(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="talk"))
        iid = self._seed(store, kind="", title="老构想")
        out = feeds.idea_action(iid, "do", by="管理员")
        assert out["state"] == "started"

    def test_dismiss_on_talk_still_works(self, tmp_path) -> None:
        store, feeds, *_r = _feeds_with_chat(tmp_path, idea_reply(kind="talk"))
        iid = self._seed(store, kind="talk", title="聊聊手柄")
        out = feeds.idea_action(iid, "dismiss", by="管理员")
        assert out["state"] == "dismissed"


# ----------------------------------------------------------------------
# 交接包：talk 没有 items 也能生成
# ----------------------------------------------------------------------


class TestHandoffTalkIdea:
    def test_build_idea_without_items(self) -> None:
        from CharTyr_MaiWork.maiwork.handoff import build_idea

        view = {
            "id": 7, "kind": "talk", "title": "来一场手柄复活大会",
            "body": "大家把坏手柄带来，看谁的能救活。", "basis": "痛点：坏手柄都扔着",
            "items": [], "keywords": [], "state": "new",
        }
        pack = build_idea(view, "", None, ())
        assert pack["filename"] == "maiwork-idea-7.md"
        assert "来一场手柄复活大会" in pack["markdown"]
        assert "要做什么" in pack["markdown"]


# ----------------------------------------------------------------------
# 个人向：kind 同一套口径
# ----------------------------------------------------------------------


class TestPersonalKind:
    def _reply(self, **over) -> str:
        idea = {
            "kind": "help",
            "title": "把这块板的例程理一下",
            "body": "理成能照着跑的一页",
            "step": "先列出要跑的例程",
            "effort": "半天",
            "origin": "FPGA 小板子",
            "basis": {"pain": "例程散着找不到", "value": "少走弯路", "interest": "他在做板子"},
            "items": [{"kind": "task", "title": "理一下例程", "desc": ""}],
            "feasibility": {
                "level": "ok", "note": "能做：整理成文档", "uses": ["write"],
                "deliver": "doc", "needs_members": False,
            },
        }
        idea.update(over)
        return json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": idea}, ensure_ascii=False)

    def _run_personal(self, tmp_path, reply: str):
        models = FakeModelsQueue(
            ready=True,
            replies=[reply, _personal_scores_json(), json.dumps({"posts": []}, ensure_ascii=False)],
        )
        store, settings, personal, models, _w, _t, _p = _make_personal(tmp_path, models=models)
        with _time_patch():
            _prun(personal.prepare_personal(PGID, UID))
        return store

    def test_talk_personal_idea_has_no_items(self, tmp_path) -> None:
        store = self._run_personal(
            tmp_path,
            self._reply(kind="talk", items=[{"kind": "task", "title": "不该留下", "desc": ""}]),
        )
        row = store.read().execute(
            "SELECT kind, items, feasibility FROM ideas WHERE target_user_id=?", (UID,)
        ).fetchone()
        assert row["kind"] == "talk"
        assert row["items"] == "[]" and row["feasibility"] == ""

    def test_help_personal_idea_keeps_items(self, tmp_path) -> None:
        store = self._run_personal(tmp_path, self._reply(kind="help"))
        row = store.read().execute(
            "SELECT kind, items FROM ideas WHERE target_user_id=?", (UID,)
        ).fetchone()
        assert row["kind"] == "help" and json.loads(row["items"])[0]["title"] == "理一下例程"


# ----------------------------------------------------------------------
# 网页：talk 构想点「直接开工」→ 400（ValueError 的映射）
# ----------------------------------------------------------------------


from test_console import G1, PASSWORD, SimpleEnv, env  # noqa: E402,F401  (env 是 fixture)


class TestConsoleDoOnTalk:
    @pytest.mark.asyncio
    async def test_console_maps_value_error_to_400(self, env: SimpleEnv) -> None:
        now = clock.now()
        with env.app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, kind, icon, title, body, state, created, updated)"
                " VALUES (?, 'talk', 'bulb', '聊聊手柄怎么救', 'b', 'new', ?, ?)",
                (G1, now, now),
            )
            iid = int(cur.lastrowid or 0)
        await env.login(PASSWORD)
        r = await env.client.post(f"/api/ideas/{iid}/do", json={})
        assert r.status == 400, r.status
        body = await r.json()
        assert "话题" in body.get("error", "")
        # 状态没被顺手改掉
        row = env.app.store.read().execute("SELECT state FROM ideas WHERE id=?", (iid,)).fetchone()
        assert row["state"] == "new"
