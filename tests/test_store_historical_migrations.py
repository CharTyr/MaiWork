"""store.py 历史迁移保真：已发布的历史步骤只许往后加，不许就地改（docs/07 §四）。

2026-10 的 0.8.0 整理里，两处「已经发布过」的迁移被就地改了：

- 第 2 步 ``_m_profile`` 把 ``member_interactions`` 的表定义删掉了；
- 第 31 步 ``_m_chat_feeds`` 改成空函数 ``pass``（当年是 ``import chat_feed`` 建表）。

后果：新建库跑到库号 31 的结构，跟线上存量库（0.7.9 = 库号 31）当年跑出来的结构
不一样。顺序迁移的语义就是「库号 = 历史步数」，同一个库号必须是同一套结构，否则
以后任何一步想「读老表补数据」都读不到、拿两个不同结构的库也没法对账。

这个测试文件把当年结构钉死，**SQL 原文从 git 历史抄下来**（不再 import 已退役的
``chat_feed`` 运行模块，也不去 git 里读文件——测试要能离线跑）：

- 第 2 步：``bot_messages`` + ``member_interactions``，``groups`` 加
  ``pending_count`` / ``info_ts``；
- 第 31 步：``chat_feeds`` 表 + ``idx_chat_feeds_group`` / ``idx_chat_feeds_key``；
- 收尾仍是第 32 步 DROP ``member_interactions``、第 33 步 DROP ``chat_feeds``、
  第 34 步建 ``agent_skills`` / ``agent_skill_versions`` / ``group_rules`` /
  ``group_rule_versions``，库号仍然是 34。

老环境兼容：这里只用老 SQLite 都认的写法（``CREATE TABLE/INDEX IF NOT EXISTS``、
``PRAGMA table_info``），不用 3.25+ 的 ``RENAME/DROP COLUMN``、3.35+ 的 ``RETURNING``、
``->`` / ``STRICT`` / ``WITHOUT ROWID`` 等新语法，免得老 SQLite 上跑不动。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import store as store_mod
from CharTyr_MaiWork.maiwork.store import _MIGRATIONS, Store

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# 当年（0.7.9 / 库号 31，git cc8c9a6）两步历史迁移的 SQL 原文，抄自
# store.py::_M_PROFILE_SQL 与 chat_feed.py::SCHEMA_SQL。改测试里的这几行 =
# 改历史，不能改；要动的是 store.py 有没有忠实照抄。
_YEAR_PROFILE_SQL = """
CREATE TABLE IF NOT EXISTS bot_messages (
    group_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (group_id, message_id)
);
CREATE TABLE IF NOT EXISTS member_interactions (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id, day)
);
"""

_YEAR_CHAT_FEEDS_SQL = """
CREATE TABLE IF NOT EXISTS chat_feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    key TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'topic',
    title TEXT NOT NULL DEFAULT '',
    hit TEXT NOT NULL DEFAULT '[]',
    words TEXT NOT NULL DEFAULT '[]',
    link TEXT NOT NULL DEFAULT '',
    rounds INTEGER NOT NULL DEFAULT 1,
    first_ts REAL NOT NULL DEFAULT 0,
    last_ts REAL NOT NULL DEFAULT 0,
    said_ts REAL,
    said_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chat_feeds_group ON chat_feeds(group_id, last_ts);
CREATE INDEX IF NOT EXISTS idx_chat_feeds_key ON chat_feeds(group_id, key);
"""

STEP_PROFILE = 2      # _m_profile
STEP_CHAT_FEEDS = 31  # _m_chat_feeds
LATEST = len(_MIGRATIONS)  # 36（34 = 历史步骤 + 35 task_lanes + 36 news_items.brief）

_DEAD_MODULE = "CharTyr_MaiWork.maiwork.chat_feed"


# ----------------------------------------------------------------------
# 小工具：只看结构，不碰业务
# ----------------------------------------------------------------------

def _tables(store: Store) -> set:
    rows = store.read().execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {str(r["name"]) for r in rows}


def _columns(store: Store, table: str) -> list:
    """PRAGMA table_info 的 (名字, 类型, NOT NULL, 默认值, 主键位次)。"""
    return [
        (str(r["name"]), str(r["type"]), int(r["notnull"]), r["dflt_value"], int(r["pk"]))
        for r in store.read().execute(f"PRAGMA table_info({table})")
    ]


def _index_names(store: Store, table: str) -> set:
    rows = store.read().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    )
    return {str(r["name"]) for r in rows}


def _index_cols(conn: sqlite3.Connection, index: str) -> list:
    return [str(r["name"]) for r in conn.execute(f"PRAGMA index_info({index})")]


def _run_steps(store: Store, upto: int) -> None:
    """顺序跑到第 upto 步为止（1 起算），并把 user_version 落成 upto——
    模仿当年那个版本跑完的库（存量库就是这样）。"""
    for fn in _MIGRATIONS[:upto]:
        fn(store.read())
    store.read().execute(f"PRAGMA user_version={upto}")


def _ref_columns(sql: str, table: str) -> list:
    """拿「当年原文」在一张抛空的库里建一遍，读它的结构当参照物。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(sql)
        return [
            (str(r["name"]), str(r["type"]), int(r["notnull"]), r["dflt_value"], int(r["pk"]))
            for r in conn.execute(f"PRAGMA table_info({table})")
        ]
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "m.db")
    yield s
    s.close()


# ----------------------------------------------------------------------
# 第 2 步：群活跃统计（bot_messages + member_interactions）
# ----------------------------------------------------------------------

class TestStep2ProfileHistory:
    def test_step2_creates_both_history_tables(self, store: Store) -> None:
        """跑到库号 2：当年建的两张表都在，member_interactions 结构一字不差。"""
        _run_steps(store, STEP_PROFILE)
        names = _tables(store)
        assert {"bot_messages", "member_interactions"} <= names
        assert _columns(store, "member_interactions") == _ref_columns(
            _YEAR_PROFILE_SQL, "member_interactions")
        assert _columns(store, "bot_messages") == _ref_columns(_YEAR_PROFILE_SQL, "bot_messages")

    def test_step2_member_interactions_primary_key_and_default(self, store: Store) -> None:
        """主键 (group_id, user_id, day)、count 默认 0 且非空——当年挑人靠它按天攒数。"""
        _run_steps(store, STEP_PROFILE)
        cols = {c[0]: c for c in _columns(store, "member_interactions")}
        assert list(cols) == ["group_id", "user_id", "day", "count"]
        assert [c[0] for c in _columns(store, "member_interactions") if c[4]] == [
            "group_id", "user_id", "day",
        ]
        assert cols["count"][3] == "0" and cols["count"][2] == 1

    def test_step2_groups_columns(self, store: Store) -> None:
        """groups 的两列（pending_count / info_ts）也照样补上。"""
        _run_steps(store, STEP_PROFILE)
        cols = {c[0] for c in _columns(store, "groups")}
        assert {"pending_count", "info_ts"} <= cols

    def test_step2_rerun_is_idempotent(self, store: Store) -> None:
        """同一步跑两遍（老库版本号停得不准时会发生）不报错、结构不变。"""
        _run_steps(store, 1)  # 先跑第 1 步把 M1 表建出来
        store_mod._m_profile(store.read())
        store_mod._m_profile(store.read())
        assert "member_interactions" in _tables(store)


# ----------------------------------------------------------------------
# 第 31 步：资讯反哺的账（chat_feeds + 两个索引）
# ----------------------------------------------------------------------

class TestStep31ChatFeedsHistory:
    def test_step31_table_matches_year_of_record(self, store: Store) -> None:
        """跑到库号 31：chat_feeds 表结构与当年（chat_feed.py::SCHEMA_SQL）逐列一致。"""
        _run_steps(store, STEP_CHAT_FEEDS)
        assert "chat_feeds" in _tables(store)
        assert _columns(store, "chat_feeds") == _ref_columns(_YEAR_CHAT_FEEDS_SQL, "chat_feeds")

    def test_step31_indexes_match_year_of_record(self, store: Store) -> None:
        """两个索引（group_id,last_ts / group_id,key）在，索引列顺序也对。"""
        _run_steps(store, STEP_CHAT_FEEDS)
        assert _index_names(store, "chat_feeds") == {"idx_chat_feeds_group", "idx_chat_feeds_key"}
        assert _index_cols(store.read(), "idx_chat_feeds_group") == ["group_id", "last_ts"]
        assert _index_cols(store.read(), "idx_chat_feeds_key") == ["group_id", "key"]

    def test_step31_rerun_is_idempotent(self, store: Store) -> None:
        store_mod._m_chat_feeds(store.read())
        store_mod._m_chat_feeds(store.read())
        assert _index_names(store, "chat_feeds") == {"idx_chat_feeds_group", "idx_chat_feeds_key"}

    def test_step31_does_not_revive_dead_chat_feed_module(self, store: Store) -> None:
        """chat_feed 运行模块已退役：文件不在，迁移步骤不 import 它。"""
        sys.modules.pop(_DEAD_MODULE, None)
        assert not (PLUGIN_DIR / "maiwork" / "chat_feed.py").exists(), "chat_feed.py 运行模块应已删除"
        _run_steps(store, STEP_CHAT_FEEDS)
        assert _DEAD_MODULE not in sys.modules, "历史迁移不该 import 已退役的 chat_feed"

    def test_step31_function_has_no_import(self) -> None:
        """第 31 步是内联的历史 SQL，不是「import 老模块再取 SCHEMA_SQL」。"""
        src = inspect.getsource(store_mod._m_chat_feeds)
        assert "import" not in src


# ----------------------------------------------------------------------
# 全量迁移：库号 36（34 之后加 task_lanes、36 加 news_items.brief），
# 两张历史表由追加的 DROP 步骤收尾
# ----------------------------------------------------------------------

class TestFullMigrate:
    def test_latest_version_is_36(self) -> None:
        # 34 = 历史步骤恢复原样后的库号；35 = 任务双岗协作的 task_lanes（docs/20）；
        # 36 = 资讯卡片短摘要 brief（news_items.brief）
        assert LATEST == 36, "库号只因 docs/20 task_lanes 和 news_items.brief 各加一步"
        assert store_mod._MIGRATIONS[34].__name__ == "_m_task_lanes"
        assert store_mod._MIGRATIONS[35].__name__ == "_m_news_brief"

    def test_fresh_db_has_no_history_tables_at_34(self, store: Store) -> None:
        """新库一路迁到最新：两张历史表建了又被后面的 DROP 拆掉，终态干净。"""
        assert store.migrate() == LATEST
        names = _tables(store)
        assert "member_interactions" not in names
        assert "chat_feeds" not in names

    def test_fresh_db_has_skills_and_rules_tables(self, store: Store) -> None:
        """第 34 步真建了每群三份的表（不是被历史表带崩了）。"""
        store.migrate()
        names = _tables(store)
        assert {"agent_skills", "agent_skill_versions", "group_rules", "group_rule_versions"} <= names
        assert "idx_agent_skills" in _index_names(store, "agent_skills")
        assert "idx_agent_skill_versions" in _index_names(store, "agent_skill_versions")
        assert "idx_group_rule_versions" in _index_names(store, "group_rule_versions")

    def test_no_dead_module_imported_by_full_migrate(self, tmp_path: Path) -> None:
        sys.modules.pop(_DEAD_MODULE, None)
        s = Store(tmp_path / "fresh.db")
        try:
            s.migrate()
        finally:
            s.close()
        assert _DEAD_MODULE not in sys.modules

    def test_step34_does_not_drop_migration_input_tables(self, tmp_path: Path) -> None:
        """第 34 步只 DROP 从没上线的 agent_lessons。agent_memory_notes 是「每群规矩」
        启动迁移的输入（migrations.py 要读老提醒），在这里拆掉就丢数据了。"""
        s = Store(tmp_path / "keep.db")
        try:
            _run_steps(s, 33)  # 跑到第 33 步
            with s.tx() as conn:
                conn.execute(
                    "CREATE TABLE agent_memory_notes"
                    " (group_id TEXT, kind TEXT, notes TEXT, updated REAL)"
                )
                conn.execute(
                    "INSERT INTO agent_memory_notes VALUES ('900000001', 'news', '老提醒', 1.0)"
                )
            assert store_mod._MIGRATIONS[33].__name__ == "_m_agent_skills_group_rules"
            store_mod._MIGRATIONS[33](s.read())  # 只跑第 34 步
            rows = s.read().execute("SELECT notes FROM agent_memory_notes").fetchall()
            assert [r["notes"] for r in rows] == ["老提醒"], "第 34 步不该动迁移输入表"
        finally:
            s.close()

    def test_integrity_ok_after_full_migrate(self, tmp_path: Path) -> None:
        s = Store(tmp_path / "fresh.db")
        try:
            s.migrate()
            assert s.read().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            s.close()


# ----------------------------------------------------------------------
# 存量库（线上 0.7.9 = 库号 31）升级
# ----------------------------------------------------------------------

class TestStockV31Upgrade:
    def _stock_v31(self, path: Path) -> Store:
        """造一个「当年线上跑过的 0.7.9 库」：库号 31、两张历史表里有数据、news 也有。"""
        s = Store(path)
        _run_steps(s, STEP_CHAT_FEEDS)
        with s.tx() as conn:
            conn.execute(
                "INSERT INTO member_interactions (group_id, user_id, day, count)"
                " VALUES ('900000001', 'u1', '2026-09-20', 7)"
            )
            conn.execute(
                "INSERT INTO chat_feeds (group_id, key, title, rounds, first_ts, last_ts)"
                " VALUES ('900000001', 'k1', '一条老资讯', 2, 1.0, 2.0)"
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary)"
                " VALUES (1, '900000001', '老新闻', '')"
            )
            s.kv_set(conn, "feeds.taste.900000001", {"text": "老口味"})
        return s

    def test_upgrade_to_34_drops_history_tables_keeps_everything_else(self, tmp_path: Path) -> None:
        s = self._stock_v31(tmp_path / "stock.db")
        try:
            assert s.migrate() == LATEST
            names = _tables(s)
            assert "member_interactions" not in names
            assert "chat_feeds" not in names
            # 别的数据一根汗毛都不能少
            assert s.read().execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 1
            assert s.read().execute("SELECT title FROM news_items").fetchone()["title"] == "老新闻"
            assert s.kv_get("feeds.taste.900000001") == {"text": "老口味"}
            assert {"agent_skills", "group_rules"} <= names
        finally:
            s.close()

    def test_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        """升级完再启动一次（migrate 再跑）什么都不做、不报错、数据还在。"""
        s = self._stock_v31(tmp_path / "stock.db")
        try:
            s.migrate()
            assert s.migrate() == LATEST
            assert s.read().execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 1
            assert s.kv_get("feeds.taste.900000001") == {"text": "老口味"}
        finally:
            s.close()

    def test_drop_does_not_touch_news_tables(self, tmp_path: Path) -> None:
        """DROP 只拆两张历史表：news_items 的老列一列不丢、顺序不变、行数不动
        （后追加的步骤只许往后挂新列，如库号 36 的 brief）。"""
        s = self._stock_v31(tmp_path / "stock.db")
        try:
            before_cols = _columns(s, "news_items")
            s.migrate()
            after_cols = _columns(s, "news_items")
            assert after_cols[: len(before_cols)] == before_cols
            assert [c[0] for c in after_cols[len(before_cols):]] == ["brief"]
            assert s.read().execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 1
        finally:
            s.close()

    def test_v31_db_without_history_tables_still_upgrades(self, tmp_path: Path) -> None:
        """已经被 0.8.0 坏迁移造出来的库（库号 31、没有 chat_feeds）也要能升到 34。"""
        s = Store(tmp_path / "broken.db")
        try:
            _run_steps(s, STEP_CHAT_FEEDS - 1)  # 跑了 1..30
            s.read().execute(f"PRAGMA user_version={STEP_CHAT_FEEDS}")  # 假装第 31 步跑过（空跑）
            assert s.migrate() == LATEST
            names = _tables(s)
            assert "chat_feeds" not in names and "member_interactions" not in names
            assert "agent_skills" in names
        finally:
            s.close()


# ----------------------------------------------------------------------
# 陌生/更老的库号，以及老 SQLite 语法兼容
# ----------------------------------------------------------------------

class TestUnknownOlderState:
    def test_unknown_lower_user_version_does_not_crash(self, tmp_path: Path) -> None:
        """user_version 被改小 / 认不出来（备份、手工拷库常见）时，重跑全量迁移不炸。"""
        s = Store(tmp_path / "odd.db")
        try:
            s.migrate()
            s.read().execute("PRAGMA user_version=0")
            assert s.migrate() == LATEST
            assert {"agent_skills", "group_rules"} <= _tables(s)
        finally:
            s.close()

    def test_old_shape_db_upgrades(self, tmp_path: Path) -> None:
        """很老的库（只有 M1 + 老互动表）也能一路补到 34。"""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(store_mod._M1_SQL)
        conn.executescript(_YEAR_PROFILE_SQL)
        conn.executescript(_YEAR_CHAT_FEEDS_SQL)
        conn.execute("INSERT INTO member_interactions VALUES ('g1', 'u1', '2026-01-01', 1)")
        conn.execute("PRAGMA user_version=0")
        conn.commit()
        conn.close()

        s = Store(db)
        try:
            assert s.migrate() == LATEST
            names = _tables(s)
            assert "member_interactions" not in names and "chat_feeds" not in names
        finally:
            s.close()

    @pytest.mark.parametrize("sql_name", ["_M_PROFILE_SQL", "_M_CHAT_FEEDS_SQL"])
    def test_history_sql_is_plain_old_ddl(self, sql_name: str) -> None:
        """历史 SQL 只用老 SQLite 都支持的写法（老环境升级不会被语法卡住）。"""
        sql = getattr(store_mod, sql_name)
        upper = sql.upper()
        for bad in ("WITHOUT ROWID", "STRICT", "RETURNING", "GENERATED ALWAYS", "DROP COLUMN",
                    "RENAME COLUMN", "->"):
            assert bad not in upper, f"{sql_name} 用了新 SQLite 才有的写法：{bad}"
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            assert stmt.upper().startswith(("CREATE TABLE", "CREATE INDEX")), stmt[:60]

    def test_history_sql_executes_on_plain_sqlite(self) -> None:
        """两步历史 SQL 拿到一个空 SQLite 里直接能跑（不依赖任何运行模块）。"""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(store_mod._M_PROFILE_SQL)
            conn.executescript(store_mod._M_CHAT_FEEDS_SQL)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"bot_messages", "member_interactions", "chat_feeds"} <= names
        finally:
            conn.close()
