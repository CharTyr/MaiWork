"""store.py 单元测试：迁移幂等、事务回滚、kv/secret、M1 表结构、文件权限。"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "sub" / "maiwork.db")
    s.migrate()
    yield s
    s.close()


class TestMigration:
    def test_migrate_idempotent(self, store: Store) -> None:
        v1 = store.migrate()
        v2 = store.migrate()
        assert v1 == v2
        assert v2 >= 1

    def test_version_persists_across_reopen(self, store: Store, tmp_path: Path) -> None:
        v1 = store.migrate()
        store.close()
        again = Store(tmp_path / "sub" / "maiwork.db")
        assert again.migrate() == v1
        again.close()

    def test_m1_tables_exist(self, store: Store) -> None:
        rows = store.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert {
            "kv",
            "secrets",
            "events",
            "groups",
            "profile_entries",
            "member_activity",
            "activity_bins",
            "focus_members",
            "usage",
        } <= names

    def test_groups_table_columns(self, store: Store) -> None:
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(groups)")}
        assert {
            "group_id", "workspace", "session_id", "name", "member_count", "token",
            "created", "last_msg_ts", "cursor_ts", "cursor_ids", "read_since",
            "profile_ready_ts", "last_refresh_ts", "last_weekly_ts", "fail_count",
        } <= cols

    def test_pragmas(self, store: Store) -> None:
        conn = store.read()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


class TestIdeaItemsMigration:
    """2026-10：构想「包含的项目」+ 派活请求来源两列（store._m_idea_items）。"""

    def test_new_columns_exist(self, store: Store) -> None:
        ideas = {r["name"] for r in store.read().execute("PRAGMA table_info(ideas)")}
        assert "items" in ideas
        reqs = {r["name"] for r in store.read().execute("PRAGMA table_info(requests)")}
        assert {"item_nos", "source"} <= reqs

    def test_old_rows_read_as_empty(self, store: Store) -> None:
        """老构想（没写 items）读出来是 '[]'，不能报错。"""
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, step, effort, created, updated)"
                " VALUES ('g1', '老构想', '第一步', '半天', 0, 0)"
            )
            cur = conn.execute(
                "INSERT INTO requests (id, group_id, kind, title, created, updated)"
                " VALUES ('R-9', 'g1', 'task', '老的', 0, 0)"
            )
            assert cur.rowcount == 1
        row = store.read().execute("SELECT items, step, effort FROM ideas").fetchone()
        assert row["items"] == "[]"
        assert row["step"] == "第一步" and row["effort"] == "半天"  # 老字段照样读得动
        req = store.read().execute("SELECT item_nos, source FROM requests WHERE id='R-9'").fetchone()
        assert req["item_nos"] == "[]" and req["source"] == ""


class TestIdeaOriginMigration:
    """2026-10：构想「由头」列（store._m_idea_origin）——新库建表有、老库启动补列。"""

    def test_new_db_has_origin(self, store: Store) -> None:
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(ideas)")}
        assert "origin" in cols

    def test_old_db_gets_column_added(self, tmp_path: Path) -> None:
        """老库（ideas 没有 origin、user_version 停在迁移前）启动后自动补列，老行读作空串。"""
        from CharTyr_MaiWork.maiwork import store as store_mod

        db = tmp_path / "old" / "m.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE ideas (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,"
            " title TEXT NOT NULL, created REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO ideas (group_id, title, created, updated) VALUES ('g1', '老构想', 0, 0)"
        )
        # 停在「该跑 _m_idea_origin 了」的前一步
        conn.execute(f"PRAGMA user_version={store_mod._MIGRATIONS.index(store_mod._m_idea_origin)}")
        conn.commit()
        conn.close()

        s = Store(db)
        s.migrate()
        cols = {r["name"] for r in s.read().execute("PRAGMA table_info(ideas)")}
        assert "origin" in cols
        row = s.read().execute("SELECT title, origin FROM ideas").fetchone()
        assert row["title"] == "老构想" and row["origin"] == ""
        s.close()


class TestTx:
    def test_tx_commits(self, store: Store) -> None:
        with store.tx() as conn:
            store.event(conn, "test")
        row = store.read().execute("SELECT kind FROM events").fetchone()
        assert row["kind"] == "test"

    def test_tx_rolls_back_event_and_state_together(self, store: Store) -> None:
        with pytest.raises(RuntimeError):
            with store.tx() as conn:
                store.event(conn, "boom")
                store.kv_set(conn, "k1", "v1")
                raise RuntimeError("炸了")
        assert store.read().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.kv_get("k1") is None

    def test_event_payload_has_version(self, store: Store) -> None:
        with store.tx() as conn:
            eid = store.event(
                conn, "msg", group_id="111", entity="u", entity_id="1", payload={"a": 1}
            )
        row = store.read().execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        assert row["v"] == 1
        assert row["group_id"] == "111"
        assert row["payload"] and '"a"' in row["payload"]

    def test_event_defaults(self, store: Store) -> None:
        with store.tx() as conn:
            store.event(conn, "plain")
        row = store.read().execute("SELECT * FROM events").fetchone()
        assert row["group_id"] == "" and row["entity"] == "" and row["entity_id"] == ""


class TestKv:
    def test_kv_roundtrip_json(self, store: Store) -> None:
        with store.tx() as conn:
            store.kv_set(conn, "n", 42)
            store.kv_set(conn, "obj", {"x": [1, 2]})
        assert store.kv_get("n") == 42
        assert store.kv_get("obj") == {"x": [1, 2]}

    def test_kv_default(self, store: Store) -> None:
        assert store.kv_get("missing") is None
        assert store.kv_get("missing", "默认") == "默认"

    def test_kv_overwrite(self, store: Store) -> None:
        with store.tx() as conn:
            store.kv_set(conn, "k", "old")
            store.kv_set(conn, "k", "new")
        assert store.kv_get("k") == "new"


class TestSecrets:
    def test_secret_roundtrip(self, store: Store) -> None:
        with store.tx() as conn:
            store.secret_set(conn, "model_api_key", "sk-xxx")
        assert store.secret_get("model_api_key") == "sk-xxx"

    def test_secret_missing_returns_empty(self, store: Store) -> None:
        assert store.secret_get("nope") == ""

    def test_secret_overwrite(self, store: Store) -> None:
        with store.tx() as conn:
            store.secret_set(conn, "s", "1")
            store.secret_set(conn, "s", "2")
        assert store.secret_get("s") == "2"


class TestFilePermission:
    def test_db_file_0600(self, tmp_path: Path) -> None:
        # exFAT 上 chmod 无效，但 pytest 的 tmp_path 在 APFS（/private/var）下，可以断言
        db = tmp_path / "perm" / "m.db"
        s = Store(db)
        s.migrate()
        s.close()
        mode = stat.S_IMODE(db.stat().st_mode)
        assert mode == 0o600
        dmode = stat.S_IMODE(db.parent.stat().st_mode)
        assert dmode == 0o700

    def test_read_connection_returns_rows(self, store: Store) -> None:
        conn = store.read()
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory is sqlite3.Row
