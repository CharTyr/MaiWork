"""数据库配置覆盖层 → config.toml 的一次性迁移（启动时跑，幂等）测试。

线上实测底账：kv["config.override"] 为空，secrets 里有 model_api_key，kv 里应有
models.settings（线上模型是网页配的），config.toml 的 [models] 全空。
迁移完 MaiWork 的模型设置必须和迁移前一模一样；写成功后数据库里的这些键清干净；
再跑一次迁移什么都不动（幂等）。迁移日志绝不打印密钥值。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from CharTyr_MaiWork import config_file, rules
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.models import Models
from CharTyr_MaiWork.migrations import migrate_db_config_to_file
from CharTyr_MaiWork.store import Store


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
        "[models]\n"
        'base_url = ""\n'
        'api_key = ""\n'
        'main = ""\n'
        'main_backup = ""\n'
        'worker = ""\n'
        'worker_backup = ""\n'
        "retries = 5\n"
        "retry_delay_s = 10\n",
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
    def test_config_override_to_file(self, tmp_path: Path, store: Store, caplog) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "config.override", {"topics": {"per_day": 7}, "usage": {"alert_daily_tokens": 999}})
        changed = migrate_db_config_to_file(store, plug, tmp_path / "data")
        assert set(changed) == {"topics.per_day", "usage.alert_daily_tokens"}
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "per_day = 7" in text
        assert "alert_daily_tokens = 999" in text
        assert "# 注释别丢" in text
        assert store.kv_get("config.override") is None

    def test_secrets_to_file(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.secret_set(conn, "jev_api_key", "jev-迁移-显眼")
            store.secret_set(conn, "console_password", "密码-迁移-显眼")
        migrate_db_config_to_file(store, plug, tmp_path / "data")
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert 'api_key = "jev-迁移-显眼"' in text
        assert 'password = "密码-迁移-显眼"' in text
        # 数据库里清干净
        assert store.secret_get("jev_api_key") == ""
        assert store.secret_get("console_password") == ""

    def test_legacy_search_api_key_cleared_not_written(self, tmp_path: Path, store: Store) -> None:
        """老的 search_api_key（[search] 已废，搜索走扩展绑定）：直接清掉，不写进文件。"""
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.secret_set(conn, "search_api_key", "tvly-迁移-显眼")
        migrate_db_config_to_file(store, plug, tmp_path / "data")
        text = (plug / "config.toml").read_text(encoding="utf-8")
        assert "tvly-迁移-显眼" not in text
        assert "[search]" not in text
        assert store.secret_get("search_api_key") == ""

    def test_models_settings_identical_before_after(self, tmp_path: Path, store: Store) -> None:
        """线上形态：kv 有 models.settings、secrets 有 model_api_key、文件 [models] 全空。"""
        plug = _plugin_dir(tmp_path)
        web_settings = {
            "base_url": "https://web.test/v1",
            "main": "m-主",
            "main_backup": "m-备",
            "worker": "w-主",
            "worker_backup": "w-备",
            "retries": 3,
            "retry_delay_s": 9,
            "checked_at": 1_789_999_000.0,
            "available": ["m-主", "w-主"],
        }
        with store.tx() as conn:
            store.kv_set(conn, "models.settings", web_settings)
            store.secret_set(conn, "model_api_key", "sk-迁移-显眼")

        # 迁移前（旧语义）：models 整组从 kv["models.settings"] + secret 读
        expected = dict(web_settings)

        migrate_db_config_to_file(store, plug, tmp_path / "data")

        # 迁移后：数据库清干净，models.settings 从 config.toml 读，和迁移前一模一样
        assert store.kv_get("models.settings") is None
        assert store.secret_get("model_api_key") == ""
        import tomlkit

        parsed = tomlkit.parse((plug / "config.toml").read_text(encoding="utf-8"))
        ms = parsed["models"]
        assert ms["base_url"] == "https://web.test/v1"
        assert ms["main"] == "m-主"
        assert ms["api_key"] == "sk-迁移-显眼"
        assert ms["retries"] == 3
        assert ms["retry_delay_s"] == 9
        # 文件里的新配置重读进 settings（tomlkit 解出来的就是纯 Python 类型，
        # unwrap 成普通 dict 后喂 load_settings），再算 models.settings 要和迁移前
        # 网页那组一模一样
        raw = {k: (v.unwrap() if hasattr(v, "unwrap") else v) for k, v in parsed.items()}
        settings2, _ = load_settings(raw)
        models = Models(store, lambda: settings2)
        after = models.settings()
        assert after.base_url == expected["base_url"]
        assert after.main == expected["main"] and after.main_backup == expected["main_backup"]
        assert after.worker == expected["worker"] and after.worker_backup == expected["worker_backup"]
        assert after.retries == expected["retries"] and after.retry_delay_s == expected["retry_delay_s"]
        assert after.key_set is True
        assert after.source == "config"

    def test_file_nonempty_value_wins_nothing_written(self, tmp_path: Path, store: Store) -> None:
        """文件里该键已有非空值且和数据库不同 → 以数据库为准（网页以前优先级更高）。"""
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "config.override", {"topics": {"per_day": 2}})  # 和文件值一样 → 无需写
            store.secret_set(conn, "jev_api_key", "jev-迁移-显眼")              # 文件空 → 写
        changed = migrate_db_config_to_file(store, plug, tmp_path / "data")
        assert "jev.api_key" in changed
        # topics.per_day 和文件值一样：不必写文件，但 kv 键照样清掉
        assert store.kv_get("config.override") is None

    def test_file_differs_db_wins(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "config.override", {"topics": {"per_day": 9}})
        migrate_db_config_to_file(store, plug, tmp_path / "data")
        assert "per_day = 9" in (plug / "config.toml").read_text(encoding="utf-8")

    def test_idempotent(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "config.override", {"topics": {"per_day": 7}})
            store.secret_set(conn, "model_api_key", "sk-迁移-显眼")
        first = migrate_db_config_to_file(store, plug, tmp_path / "data")
        text1 = (plug / "config.toml").read_text(encoding="utf-8")
        second = migrate_db_config_to_file(store, plug, tmp_path / "data")
        text2 = (plug / "config.toml").read_text(encoding="utf-8")
        assert first
        assert second == []
        assert text1 == text2

    def test_nothing_to_migrate(self, tmp_path: Path, store: Store) -> None:
        plug = _plugin_dir(tmp_path)
        assert migrate_db_config_to_file(store, plug, tmp_path / "data") == []

    def test_no_secret_in_logs(self, tmp_path: Path, store: Store, caplog) -> None:
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.secret_set(conn, "model_api_key", "sk-绝不许-进日志")
            store.secret_set(conn, "console_password", "密码-绝不许-进日志")
        with caplog.at_level(logging.INFO):
            migrate_db_config_to_file(store, plug, tmp_path / "data")
        whole = "\n".join(r.getMessage() for r in caplog.records)
        assert "sk-绝不许-进日志" not in whole
        assert "密码-绝不许-进日志" not in whole

    def test_admin_password_hash_migrated(self, tmp_path: Path, store: Store) -> None:
        """config 里配了 console.password 后，旧的自动生成哈希失效（删掉）。"""
        plug = _plugin_dir(tmp_path)
        with store.tx() as conn:
            store.secret_set(conn, "console_password", "密码-迁移-显眼")
            store.secret_set(conn, "admin_password_hash", "sha256$salt$hash")
        migrate_db_config_to_file(store, plug, tmp_path / "data")
        assert store.secret_get("admin_password_hash") == ""
