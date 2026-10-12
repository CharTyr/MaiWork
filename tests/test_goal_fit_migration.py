"""R5 数据迁移 `_m_goal_fit`：requests 加 goal_fit / goal_fit_reason / goal_fit_alt，
goals 加 fit / fit_reason，全是 TEXT NOT NULL DEFAULT ''。

规则：迁移只加列、可重复执行；老库跑到新库号时老行读出来是空串；新库也有这些列。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import store as store_mod
from CharTyr_MaiWork.maiwork.store import Store

# 第 39 步 = goal_fit（40 = 后来加的 chat_log 去重账本，不是本文件的事）
STEP_GOAL_FIT = store_mod._MIGRATIONS.index(store_mod._m_goal_fit) + 1
PREVIOUS = STEP_GOAL_FIT - 1
LATEST = len(store_mod._MIGRATIONS)

REQUEST_COLS = ("goal_fit", "goal_fit_reason", "goal_fit_alt")
GOAL_COLS = ("fit", "fit_reason")


def _columns(store: Store, table: str) -> dict[str, tuple]:
    """列名 → (类型, NOT NULL, 默认值)。"""
    return {
        str(r["name"]): (str(r["type"]), int(r["notnull"]), r["dflt_value"])
        for r in store.read().execute(f"PRAGMA table_info({table})")
    }


def _run_steps(store: Store, upto: int) -> None:
    """顺序跑到第 upto 步为止（1 起算），模仿那个版本跑完的存量库。"""
    for fn in store_mod._MIGRATIONS[:upto]:
        fn(store.read())
    store.read().execute(f"PRAGMA user_version={upto}")


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "m.db")
    yield s
    s.close()


class TestGoalFitMigration:
    def test_step_is_appended_last(self) -> None:
        """新阶段只能往后加：_m_goal_fit 就在第 39 步（第 40 步是后加的 chat_log 去重账本）。"""
        assert STEP_GOAL_FIT == 39, "库号只因 goal_fit 加一步"
        assert store_mod._MIGRATIONS[STEP_GOAL_FIT - 1].__name__ == "_m_goal_fit"

    def test_fresh_db_has_all_five_columns(self, store: Store) -> None:
        """全新库一路迁到最新也有这五列（全走迁移，没有单独的建库 SQL）。"""
        assert store.migrate() == LATEST
        req = _columns(store, "requests")
        goal = _columns(store, "goals")
        for name in REQUEST_COLS:
            assert req[name] == ("TEXT", 1, "''"), name
        for name in GOAL_COLS:
            assert goal[name] == ("TEXT", 1, "''"), name

    def test_existing_db_without_columns_gets_them(self, store: Store) -> None:
        """老库（库号 38）没有这五列，跑一次迁移就补上。"""
        _run_steps(store, PREVIOUS)
        for name in REQUEST_COLS:
            assert name not in _columns(store, "requests")
        for name in GOAL_COLS:
            assert name not in _columns(store, "goals")

        assert store.migrate() == LATEST
        req = _columns(store, "requests")
        goal = _columns(store, "goals")
        assert set(REQUEST_COLS) <= set(req)
        assert set(GOAL_COLS) <= set(goal)

    def test_old_rows_read_back_as_empty_string(self, store: Store) -> None:
        """迁移前落的老行，迁移后新列是空串（不是 NULL）。"""
        _run_steps(store, PREVIOUS)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO requests (id, group_id, kind, title, created, updated)"
                " VALUES ('R-1', '900000001', 'goal', '老请求', 1.0, 1.0)"
            )
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, title, created, updated)"
                " VALUES ('G-1', '900000001', 'agent', '老目标', 1.0, 1.0)"
            )
        store.migrate()

        req = store.read().execute("SELECT * FROM requests WHERE id='R-1'").fetchone()
        goal = store.read().execute("SELECT * FROM goals WHERE id='G-1'").fetchone()
        assert [req[n] for n in REQUEST_COLS] == ["", "", ""]
        assert [goal[n] for n in GOAL_COLS] == ["", ""]

    def test_rerun_is_idempotent(self, store: Store) -> None:
        """同一步跑两遍不报错、列不重复。"""
        store.migrate()
        store_mod._m_goal_fit(store.read())
        store_mod._m_goal_fit(store.read())
        req_names = [c[0] for c in _columns(store, "requests").items()]
        assert [n for n in req_names if n in REQUEST_COLS] == list(REQUEST_COLS)
        goal_names = list(_columns(store, "goals"))
        assert [n for n in goal_names if n in GOAL_COLS] == list(GOAL_COLS)
