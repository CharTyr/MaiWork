"""新配置键测试：models.context_window、tasks 安全网、feeds.collect_minutes（0.4.0）；
models.max_tokens（0.4.2）。"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.config import load_settings


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
        """2026-10 改版 1a：context_window 搬到 [[model_list]] 条目（不进「全部配置」），
        网页在「设置 → 模型 → 建/改模型条目」改；老 settings.models 的夹取语义照旧（兜底 / 迁移读数用）。"""
        from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY

        assert "models.context_window" not in CONFIG_BY_KEY
        # 模型条目越界值：整条被丢（不坏插件），问题列表里说明原因
        settings, problems = load_settings({
            "endpoints": [{"id": "e1", "base_url": "https://x.test/v1", "api_key": "k"}],
            "model_list": [{"id": "m1", "endpoint": "e1", "model": "m", "context_window": 10**9, "max_tokens": 8192}],
        })
        assert settings.model_list == ()
        assert any("context_window" in p for p in problems)


class TestMaxTokens:
    """0.4.2：[models] max_tokens（每次模型调用都带上，默认 32768）。"""

    def test_default(self):
        settings, problems = load_settings({})
        assert settings.models.max_tokens == 32768

    def test_custom_value(self):
        settings, _ = load_settings({"models": {"max_tokens": 8192}})
        assert settings.models.max_tokens == 8192

    def test_clamped(self):
        settings, _ = load_settings({"models": {"max_tokens": 10}})
        assert settings.models.max_tokens == 1024
        settings, _ = load_settings({"models": {"max_tokens": 10**9}})
        assert settings.models.max_tokens == 1_000_000

    def test_garbage_falls_back_to_default(self):
        settings, _ = load_settings({"models": {"max_tokens": "很多"}})
        assert settings.models.max_tokens == 32768

    def test_web_save_schema(self):
        """2026-10 改版 1a：max_tokens 搬进 [[model_list]] 条目（不进「全部配置」），
        且必须 < context_window（不然条目被丢）；老 settings.models 的夹子照旧（兜底 / 迁移读数用）。"""
        from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY

        assert "models.max_tokens" not in CONFIG_BY_KEY
        # 越界（10 < 1024）：整条被丢，问题列表里说明原因
        settings, problems = load_settings({
            "endpoints": [{"id": "e1", "base_url": "https://x.test/v1", "api_key": "k"}],
            "model_list": [{"id": "m1", "endpoint": "e1", "model": "m", "context_window": 128000, "max_tokens": 10}],
        })
        assert settings.model_list == ()
        assert any("max_tokens" in p for p in problems)
        # max_tokens >= context_window 的条目读配置时丢掉并记问题
        settings, problems = load_settings({
            "endpoints": [{"id": "e1", "base_url": "https://x.test/v1", "api_key": "k"}],
            "model_list": [{"id": "m1", "endpoint": "e1", "model": "m", "context_window": 8192, "max_tokens": 8192}],
        })
        assert settings.model_list == ()
        assert problems


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
        from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY, CONFIG_SECTIONS

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
