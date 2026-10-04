"""kv["rules.override"] → config.toml 的一次性迁移（启动时跑，幂等）。

docs/18 第一步：旧覆盖层会赢过网页「全部配置」写的 config.toml（「网页改了不生效」）。
迁移把 kv 里的值写进文件（值和文件一样的键直接清 kv 不算改动），**写成功才删 kv 键**；
文件坏了 / 写不了 → 抛错、kv 留着，下次启动再迁。日志只打键名，绝不打值。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import config_file
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.migrations import KV_RULES_OVERRIDE, migrate_rules_override_to_file
from CharTyr_MaiWork.maiwork.store import Store


def _plugin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plug"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(
        "# 注释别丢\n"
        "[plugin]\n"
        "enabled = true\n"
        "\n"
        "[topics]\n"
        "per_day = 2\n"
        "\n"
        "[delivery]\n"
        "push_per_day = 3\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "data" / "m.db")
    s.migrate()
    yield s
    s.close()


class TestMigrate:
    def test_override_moves_into_file(self, tmp_path: Path, store: Store, caplog) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, KV_RULES_OVERRIDE, {"topics": {"per_day": 7}, "delivery": {"push_per_day": 3}})
        with caplog.at_level(logging.INFO, logger="maiwork.migrations"):
            written = migrate_rules_override_to_file(store, plug, tmp_path / "data")
        # per_day 改了（2→7）；push_per_day 和文件一样（3）不写
        assert written == ["topics.per_day"]
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "per_day = 7" in text
        assert "# 注释别丢" in text
        assert store.kv_get(KV_RULES_OVERRIDE) is None
        # 日志只打键名不打值
        for rec in caplog.records:
            assert "7" not in rec.getMessage() or "topics.per_day" in rec.getMessage()

    def test_all_same_as_file_just_deletes_kv(self, tmp_path: Path, store: Store) -> None:
        """生产现状：{"topics":{"enabled":true}} 与文件值一致 → 不写文件、只清 kv。"""
        plug = _plugin_dir(tmp_path)
        text0 = (plug / "config.toml").read_text(encoding="utf-8")
        with store.tx() as conn:
            store.kv_set(conn, KV_RULES_OVERRIDE, {"topics": {"per_day": 2}})
        written = migrate_rules_override_to_file(store, plug, tmp_path / "data")
        assert written == []
        assert (plug / "config.toml").read_text(encoding="utf-8") == text0  # 文件一字节没动
        assert store.kv_get(KV_RULES_OVERRIDE) is None

    def test_no_override_noop(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path)
        assert migrate_rules_override_to_file(store, plug, tmp_path / "data") == []
        assert (plug / "config.toml").read_text(encoding="utf-8") != ""

    def test_bad_shapes_cleared(self, tmp_path: Path, store: Store) -> None:
        """形状全坏（值是 table / 不是 dict）→ 直接清 kv 不写文件。"""
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, KV_RULES_OVERRIDE, {"delivery": {"push_per_day": {"weird": 1}}})
        written = migrate_rules_override_to_file(store, plug, tmp_path / "data")
        assert written == []
        assert store.kv_get(KV_RULES_OVERRIDE) is None

    def test_write_failure_keeps_kv_for_retry(self, tmp_path: Path, store: Store) -> None:
        """文件坏了 → 抛 ConfigFileError，kv 留着，下次启动能再迁成功。"""
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, KV_RULES_OVERRIDE, {"topics": {"per_day": 9}})
        (plug / "config.toml").write_text("[topics\n坏文件", encoding="utf-8")
        with pytest.raises(config_file.ConfigFileError):
            migrate_rules_override_to_file(store, plug, tmp_path / "data")
        assert store.kv_get(KV_RULES_OVERRIDE) == {"topics": {"per_day": 9}}
        # 修好后重迁成功
        (plug / "config.toml").write_text("[topics]\nper_day = 2\n", encoding="utf-8")
        written = migrate_rules_override_to_file(store, plug, tmp_path / "data")
        assert written == ["topics.per_day"]
        assert store.kv_get(KV_RULES_OVERRIDE) is None

    def test_lists_and_bool_written(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(
                conn, KV_RULES_OVERRIDE,
                {"topics": {"enabled": False}, "feeds": {"news_slots": ["09:00", "20:00"]}},
            )
        written = migrate_rules_override_to_file(store, plug, tmp_path / "data")
        assert set(written) == {"topics.enabled", "feeds.news_slots"}
        import tomllib

        raw = tomllib.loads((plug / "config.toml").read_text(encoding="utf-8"))
        assert raw["topics"]["enabled"] is False
        assert raw["feeds"]["news_slots"] == ["09:00", "20:00"]
        # 迁完的配置能正常解析
        s, _ = load_settings(raw)
        assert s.topics.enabled is False
        assert list(s.feeds.news_slots) == ["09:00", "20:00"]
