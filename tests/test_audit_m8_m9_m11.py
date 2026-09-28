"""M8 / M9 / M11 回归测试。

M8：数据无限增长——Store.prune(now)：events / tool_calls / usage / judgments
保留 30 天，mentions 过期行物理删除；app 后台每天跑一次。

M9：app._on_long_job_done 用 try 包 task.exception()；intake / commands 的
create_task 都加 done callback 记异常（不然后台炸没处看）。

M11：load_settings 校验 workspace 名 ^[A-Za-z0-9_-]{1,64}$；不合法丢该群
配置并记问题。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.store import Store

pytestmark = pytest.mark.asyncio

GID = "900000001"


class TestM11WorkspaceNameValidation:
    def test_bad_workspace_name_group_dropped(self) -> None:
        settings, problems = load_settings(
            {"groups": {"serve": [{"group": f"qq:{GID}", "workspace": "带空格的名字"}]}}
        )
        assert GID not in settings.groups
        assert any("workspace" in p or "工作区" in p for p in problems)

    def test_bad_workspace_name_slash_dropped(self) -> None:
        settings, problems = load_settings(
            {"groups": {"serve": [{"group": f"qq:{GID}", "workspace": "a/b"}]}}
        )
        assert GID not in settings.groups

    def test_good_workspace_name_kept(self) -> None:
        settings, problems = load_settings(
            {"groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker_lab-01"}]}}
        )
        assert settings.groups[GID].workspace == "tinker_lab-01"
        assert not any("工作区名" in p for p in problems)

    def test_empty_workspace_defaults_ok(self) -> None:
        """空 workspace 用默认 g<群号>，合法。"""
        settings, problems = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        assert GID in settings.groups
        assert not any("工作区名" in p for p in problems)

    def test_too_long_workspace_dropped(self) -> None:
        settings, problems = load_settings(
            {"groups": {"serve": [{"group": f"qq:{GID}", "workspace": "x" * 65}]}}
        )
        assert GID not in settings.groups


class TestM8StorePrune:
    def _store(self, tmp_path: Path) -> Store:
        store = Store(tmp_path / "t.db")
        store.migrate()
        return store

    def _seed(self, store: Store, now: float) -> None:
        old = now - 31 * 86400
        recent = now - 86400
        with store.tx() as conn:
            for ts in (old, recent):
                conn.execute(
                    "INSERT INTO events (ts, kind, group_id) VALUES (?, 'x', ?)", (ts, GID)
                )
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, tool) VALUES (?, ?, 't')", (ts, GID)
                )
                conn.execute(
                    "INSERT INTO usage (ts, day, group_id) VALUES (?, '2026-01-01', ?)", (ts, GID)
                )
                conn.execute(
                    "INSERT INTO judgments (ts, purpose, group_id) VALUES (?, 'p', ?)", (ts, GID)
                )
            # mentions：一条过期一条没过期
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, 'old', '老备忘', ?, 5, ?)",
                (GID, old, old),
            )
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, 'new', '新备忘', ?, 5, ?)",
                (GID, now + 86400, recent),  # 明后天才过期，留着
            )

    def test_prune_deletes_old_and_keeps_recent(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        now = clock.now()
        self._seed(store, now)
        store.prune(now)
        for table in ("events", "tool_calls", "usage", "judgments"):
            rows = store.read().execute(f"SELECT ts FROM {table}").fetchall()
            assert len(rows) == 1, f"{table} 没清旧数据"
            assert float(rows[0]["ts"]) > now - 30 * 86400
        keys = [r["key"] for r in store.read().execute("SELECT key FROM mentions").fetchall()]
        assert keys == ["new"]

    def test_prune_returns_counts(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        now = clock.now()
        self._seed(store, now)
        counts = store.prune(now)
        assert isinstance(counts, dict)
        for table in ("events", "tool_calls", "usage", "judgments", "mentions"):
            assert counts.get(table) == 1

    def test_prune_empty_db_noop(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        counts = store.prune(clock.now())
        assert all(v == 0 for v in counts.values())


class TestM9TaskExceptionGuards:
    async def test_on_long_job_done_swallows_cancelled(self, tmp_path: Path) -> None:
        from fakes import FakeCtx, FakeProfiles

        from CharTyr_MaiWork.app import MaiWorkApp

        raw = {
            "plugin": {"enabled": False},
            "storage": {"data_dir": str(tmp_path / "d")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])

        async def boom():
            raise RuntimeError("炸了")

        t = asyncio.ensure_future(boom())
        await asyncio.sleep(0)  # 让它炸完
        t2 = asyncio.ensure_future(asyncio.sleep(60))
        t2.cancel()
        try:
            await t2
        except asyncio.CancelledError:
            pass
        # 都不许抛
        app._on_long_job_done(t)
        app._on_long_job_done(t2)

    async def test_commands_start_task_done_callback_logs(self, tmp_path: Path, caplog) -> None:
        """commands._start_task 的 create_task 带 done callback：协程炸了进日志。"""
        from fakes import FakeCtx, FakeProfiles

        from CharTyr_MaiWork.app import MaiWorkApp
        from CharTyr_MaiWork.commands import Commands
        from CharTyr_MaiWork.config import load_settings
        from CharTyr_MaiWork.store import Store

        store = Store(tmp_path / "t.db")
        store.migrate()
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})

        class _BadCoord:
            async def run_task(self, task_id):
                raise RuntimeError("协调器炸了")

        commands = Commands(
            store, approvals=None, tasks=None, goals=None, outbox=None,
            host=None, get_settings=lambda: settings, coordinator=_BadCoord(),
        )
        import logging

        with caplog.at_level(logging.ERROR, logger="maiwork.commands"):
            commands._start_task("T-9")
            await asyncio.sleep(0.1)  # 等协程炸 + callback 跑
        assert any("协调器炸了" in r.getMessage() or "后台" in r.getMessage() or "出错" in r.getMessage() for r in caplog.records)
