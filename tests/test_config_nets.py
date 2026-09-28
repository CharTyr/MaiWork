"""新配置键测试：models.context_window、tasks 安全网、feeds.collect_minutes（0.4.0）。"""

from __future__ import annotations

from CharTyr_MaiWork.config import load_settings


class TestContextWindow:
    def test_default(self):
        settings, problems = load_settings({})
        assert settings.models.context_window == 128000

    def test_clamped(self):
        settings, problems = load_settings({"models": {"context_window": 100}})
        assert settings.models.context_window == 8192
        settings, problems = load_settings({"models": {"context_window": 10**9}})
        assert settings.models.context_window == 2_000_000

    def test_web_save_schema(self):
        from CharTyr_MaiWork.rules import CONFIG_BY_KEY

        assert "models.context_window" in CONFIG_BY_KEY
        spec = CONFIG_BY_KEY["models.context_window"]
        assert spec["type"] == "int"
        assert spec["label"]
        assert spec["help"] or spec.get("label")


class TestTaskNets:
    def test_defaults(self):
        settings, _ = load_settings({})
        assert settings.tasks.token_limit == 2_000_000
        assert settings.tasks.run_seconds == 3 * 3600

    def test_zero_means_off(self):
        settings, _ = load_settings({"tasks": {"token_limit": 0, "run_seconds": 0}})
        assert settings.tasks.token_limit == 0
        assert settings.tasks.run_seconds == 0

    def test_web_save_schema(self):
        from CharTyr_MaiWork.rules import CONFIG_BY_KEY, CONFIG_SECTIONS

        assert "tasks.token_limit" in CONFIG_BY_KEY
        assert "tasks.run_seconds" in CONFIG_BY_KEY
        assert any(s["id"] == "tasks" for s in CONFIG_SECTIONS)


class TestFeedsCollectMinutes:
    def test_default(self):
        settings, _ = load_settings({})
        assert settings.feeds.collect_minutes == 15

    def test_clamped(self):
        settings, _ = load_settings({"feeds": {"collect_minutes": 0}})
        assert settings.feeds.collect_minutes == 1
        settings, _ = load_settings({"feeds": {"collect_minutes": 999}})
        assert settings.feeds.collect_minutes == 60
