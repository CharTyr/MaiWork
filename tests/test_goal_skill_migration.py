"""Goal migration and the reserved local skill migration keep both DB shapes safe."""
from CharTyr_MaiWork.maiwork.store import Store


def test_fresh_db_reuses_existing_skill_tables(tmp_path):
    store = Store(tmp_path / "new.db")
    try:
        assert store.migrate() == 44
        names = {r[0] for r in store.read().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"agent_skills", "agent_skill_versions"} <= names
        assert "skills" not in names
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(goals)")}
        assert {"stale_notified_ts", "admin_chat_id"} <= cols
        assert store.migrate() == 44
    finally:
        store.close()


def test_reserved_migration_preserves_local_legacy_skill_data(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(path)
    try:
        store.migrate()
        with store.tx() as conn:
            conn.execute("CREATE TABLE skills (id TEXT PRIMARY KEY, content TEXT)")
            conn.execute("INSERT INTO skills VALUES ('legacy', 'keep this local draft')")
            conn.execute("PRAGMA user_version=42")
        assert store.migrate() == 44
        assert store.read().execute("SELECT content FROM skills WHERE id='legacy'").fetchone()[0] == "keep this local draft"
    finally:
        store.close()
