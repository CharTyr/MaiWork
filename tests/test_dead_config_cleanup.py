"""死配置键清理（docs/18 第一步）：feeds.min_score / feeds.ideas_per_day /
delivery.mention_ttl_minutes 从配置模型、网页配置表、示例和存量 config.toml 里删掉。

- load_settings 里再写这三个键：静默忽略，不进 Settings、不记问题（问题清单只报真问题）；
- 启动迁移把这三个键从存量 config.toml 里删掉（有改动才重写文件；没有 = 文件不动）；
- 迁移幂等：第二遍跑返回 []、文件一字节不动。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.migrations import migrate_dead_config_keys


def _plugin_dir(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "plug"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(body, encoding="utf-8")
    return d


class TestDeadKeysIgnored:
    """三个死键在 load_settings 里被静默忽略。"""

    def test_settings_have_no_dead_fields(self) -> None:
        s, problems = load_settings(
            {"feeds": {"min_score": 0.8, "ideas_per_day": 5}, "delivery": {"mention_ttl_minutes": 60}}
        )
        assert not hasattr(s.feeds, "min_score")
        assert not hasattr(s.feeds, "ideas_per_day")
        assert not hasattr(s.delivery, "mention_ttl_minutes")
        # 死键不记问题（key 已退役，提到它只是噪音）
        assert problems == []

    def test_delivery_setting_signature(self) -> None:
        import dataclasses

        names = {f.name for f in dataclasses.fields(type(load_settings({})[0].delivery))}
        assert names == {"push_per_day", "quiet_hours"}

    def test_feeds_setting_signature(self) -> None:
        import dataclasses

        names = {f.name for f in dataclasses.fields(type(load_settings({})[0].feeds))}
        assert "min_score" not in names
        assert "ideas_per_day" not in names


class TestConfigSchema:
    """网页「全部配置」的 schema 里不再有这三个键。"""

    def test_schema_has_no_dead_keys(self) -> None:
        from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY

        assert "feeds.min_score" not in CONFIG_BY_KEY
        assert "feeds.ideas_per_day" not in CONFIG_BY_KEY
        assert "delivery.mention_ttl_minutes" not in CONFIG_BY_KEY


class TestMigration:
    def test_deletes_dead_keys(self, tmp_path: Path) -> None:
        plug = _plugin_dir(
            tmp_path,
            "[plugin]\nenabled = true\n\n"
            "[feeds]\nmin_score = 0.6\nideas_per_day = 1\nmax_items = 10\n\n"
            "[delivery]\nmention_ttl_minutes = 120\npush_per_day = 3\n",
        )
        deleted = migrate_dead_config_keys(plug, tmp_path / "data")
        assert set(deleted) == {"feeds.min_score", "feeds.ideas_per_day", "delivery.mention_ttl_minutes"}
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "min_score" not in text
        assert "ideas_per_day" not in text
        assert "mention_ttl_minutes" not in text
        # 活着的键不被碰
        assert "max_items = 10" in text
        assert "push_per_day = 3" in text
        assert "enabled = true" in text

    def test_no_dead_keys_no_rewrite(self, tmp_path: Path) -> None:
        plug = _plugin_dir(tmp_path, "[feeds]\nmax_items = 5\n")
        before = (plug / "config.toml").read_text(encoding="utf-8")
        deleted = migrate_dead_config_keys(plug, tmp_path / "data")
        assert deleted == []
        assert (plug / "config.toml").read_text(encoding="utf-8") == before
        # 没出活就不该有备份
        assert not (tmp_path / "data" / "config-backups").exists()

    def test_missing_file_noop(self, tmp_path: Path) -> None:
        plug = tmp_path / "nope"
        plug.mkdir()
        assert migrate_dead_config_keys(plug, tmp_path / "data") == []

    def test_broken_toml_noop(self, tmp_path: Path) -> None:
        plug = _plugin_dir(tmp_path, "[feeds\n\xe8\xbf\x99\xe6\x98\xaf\xe5\x9d\x8f\xe6\x96\x87\xe4\xbb\xb6")
        assert migrate_dead_config_keys(plug, tmp_path / "data") == []

    def test_idempotent(self, tmp_path: Path) -> None:
        plug = _plugin_dir(tmp_path, "[delivery]\nmention_ttl_minutes = 60\n")
        first = migrate_dead_config_keys(plug, tmp_path / "data")
        assert first == ["delivery.mention_ttl_minutes"]
        time.sleep(0.01)
        before = (plug / "config.toml").read_text(encoding="utf-8")
        second = migrate_dead_config_keys(plug, tmp_path / "data")
        assert second == []
        assert (plug / "config.toml").read_text(encoding="utf-8") == before

    def test_load_settings_after_migration(self, tmp_path: Path) -> None:
        """迁移后的文件能正常解析、无问题。"""
        import tomllib

        plug = _plugin_dir(
            tmp_path,
            "[feeds]\nmin_score = 0.8\nmax_items = 7\n\n[delivery]\nmention_ttl_minutes = 90\n",
        )
        migrate_dead_config_keys(plug, tmp_path / "data")
        raw = tomllib.loads((plug / "config.toml").read_text(encoding="utf-8"))
        s, problems = load_settings(raw)
        assert problems == []
        assert s.feeds.max_items == 7


class TestGoalsProposeRemoved:
    def test_propose_key_and_empty_section_removed(self, tmp_path: Path) -> None:
        """[goals] 只有 propose 一项 → 键删掉后整节连删（不留空表）；再跑幂等。"""
        plug = _plugin_dir(tmp_path, "[goals]\npropose = true\n\n[topics]\nper_day = 2\n")
        deleted = migrate_dead_config_keys(plug, tmp_path / "data")
        assert "goals.propose" in deleted
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "goals" not in text
        assert "[topics]" in text and "per_day = 2" in text
        # 幂等
        assert migrate_dead_config_keys(plug, tmp_path / "data") == []

    def test_propose_key_with_other_keys_keeps_section(self, tmp_path: Path) -> None:
        """[goals] 还有别的键 → 只删 propose，节保留。"""
        plug = _plugin_dir(tmp_path, "[goals]\npropose = true\nother = 5\n")
        deleted = migrate_dead_config_keys(plug, tmp_path / "data")
        assert deleted == ["goals.propose"]
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "[goals]" in text and "other = 5" in text


class TestGoalProposalKvCleanup:
    def _mk_store(self, tmp_path: Path):
        from CharTyr_MaiWork.maiwork.store import Store

        s = Store(tmp_path / "data" / "maiwork.db")
        s.migrate()
        return s

    def test_leftover_kv_deleted(self, tmp_path: Path) -> None:
        """goals.propose_day.<群号> 和 sched.<群号>.goal 都删；别的 sched 键不动。"""
        from CharTyr_MaiWork.maiwork.migrations import migrate_goal_proposal_kv

        store = self._mk_store(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "goals.propose_day.900000001", "2026-10-11")
            store.kv_set(conn, "sched.900000001.goal", "2026-10-10")
            store.kv_set(conn, "sched.900000001.idea", "2026-10-11")  # 构想的事别动
            store.kv_set(conn, "sched.900000001.news", ["2026-10-11|08:30"])
        deleted = migrate_goal_proposal_kv(store)
        assert sorted(deleted) == ["goals.propose_day.900000001", "sched.900000001.goal"]
        assert store.kv_get("goals.propose_day.900000001") is None
        assert store.kv_get("sched.900000001.goal") is None
        assert store.kv_get("sched.900000001.idea") == "2026-10-11"
        assert store.kv_get("sched.900000001.news") == ["2026-10-11|08:30"]

    def test_idempotent_no_keys(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.migrations import migrate_goal_proposal_kv

        store = self._mk_store(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "sched.900000001.idea", "2026-10-11")
        assert migrate_goal_proposal_kv(store) == []
        assert store.kv_get("sched.900000001.idea") == "2026-10-11"
