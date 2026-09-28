"""管理员对话的存储（_m_admin_chat 迁移）：三张表建好、老库追加迁移幂等、新库全量迁移到最新版。"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork.store import _MIGRATIONS, Store


def _tables(store: Store) -> set[str]:
    rows = store.read().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(r["name"]) for r in rows}


def _cols(store: Store, table: str) -> list[str]:
    return [str(r["name"]) for r in store.read().execute(f"PRAGMA table_info({table})")]


class TestMigration:
    def test_fresh_db_has_admin_chat_tables(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        tables = _tables(store)
        assert "admin_chats" in tables
        assert "admin_chat_msgs" in tables
        assert "admin_chat_pending" in tables

    def test_admin_chats_columns(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        cols = _cols(store, "admin_chats")
        for name in ("id", "title", "group_id", "created", "updated", "archived"):
            assert name in cols, f"admin_chats 缺列 {name}"

    def test_admin_chat_msgs_columns(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        cols = _cols(store, "admin_chat_msgs")
        for name in ("id", "chat_id", "ts", "role", "content", "tool_calls", "tool_call_id", "name", "meta"):
            assert name in cols, f"admin_chat_msgs 缺列 {name}"

    def test_admin_chat_pending_columns(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        cols = _cols(store, "admin_chat_pending")
        for name in ("id", "chat_id", "msg_id", "tool", "args", "summary", "status", "result", "created", "decided"):
            assert name in cols, f"admin_chat_pending 缺列 {name}"

    def test_old_db_migrates_forward(self, tmp_path: Path) -> None:
        """停在上一版的老库：migrate 只补新迁移，版本到最新，旧迁移不重复跑。"""
        store = Store(tmp_path / "t.db")
        old_version = len(_MIGRATIONS) - 1
        for fn in _MIGRATIONS[:old_version]:
            fn(store.read())
        store.read().execute(f"PRAGMA user_version={old_version}")
        version = store.migrate()
        assert version == len(_MIGRATIONS)
        assert "admin_chats" in _tables(store)
        # 幂等：再 migrate 一次不炸、版本不变
        assert store.migrate() == len(_MIGRATIONS)

    def test_pending_status_default(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO admin_chats (title, group_id, created, updated) VALUES ('t', '', 1, 1)"
            )
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content) VALUES (1, 1, 'assistant', '')"
            )
            conn.execute(
                "INSERT INTO admin_chat_pending (chat_id, msg_id, tool, args, summary, created)"
                " VALUES (1, 1, 'send_group_message', '{}', '发到群里', 1)"
            )
        row = store.read().execute("SELECT status FROM admin_chat_pending").fetchone()
        assert str(row["status"]) == "pending"
        row2 = store.read().execute("SELECT archived FROM admin_chats").fetchone()
        assert int(row2["archived"]) == 0
