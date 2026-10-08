"""config.py 单元测试：默认值、服务群解析、非法配置容错、Settings 规范化。"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import (
    CONFIG_VERSION,
    MaiWorkConfig,
    PluginSectionConfig,
    Settings,
    load_settings,
)


def _raw(**overrides):
    """快速拼一个配置字典。"""
    raw = {}
    for key, value in overrides.items():
        parts = key.split(".")
        node = raw
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return raw


def settings_of(raw: dict) -> tuple[Settings, list[str]]:
    return load_settings(raw)


class TestDefaults:
    def test_default_disabled(self) -> None:
        assert MaiWorkConfig().plugin.enabled is False
        assert PluginSectionConfig().enabled is False

    def test_config_version_bumped(self) -> None:
        # 0.3.4：资讯质量标准（docs/02 §4.1）：feeds 加 web_min_avg /
        # pool_min_avg / guides，max_items 默认 3→10；min_score / blocked_domains
        # 2026-10 已删（屏蔽名单挪成按群 kv）。
        # 0.3.5：railway.new 资讯实测：environments 加 railway_daily_max /
        # verify_per_round / verify_minutes
        # 0.3.8：关注成员个人向产出：focus 加 personal_feeds / personal_per_day
        # 0.3.9：模型重试设置：[models] 加 retries / retry_delay_s
        # 0.3.10：管理员 / 免批名单改成「平台:账号」
        # 0.4.0：[models] context_window、[tasks] 安全网、[feeds] collect_minutes
        # 0.4.1：[reader] Jina Reader（打开网页首选）
        # 0.4.2：[models] max_tokens（每次模型调用都带上，缺省 32768）
        # 0.4.3：[feeds] viz_per_day（资讯图解每群每天上限）
        # 0.4.4：[console] update_check、maibot_webui_url（更新提醒）
        # 0.4.5：[[endpoints]] / [[model_list]]（模型改版阶段 1a）
        # 0.4.6：每端点高级请求头覆盖 headers（默认空）
        assert CONFIG_VERSION == "0.4.6"
        assert MaiWorkConfig().plugin.config_version == CONFIG_VERSION

    def test_railway_verify_new_fields_defaults(self) -> None:
        """0.3.5 新字段：railway_daily_max=2、verify_per_round=2、verify_minutes=10。"""
        s, _ = settings_of({})
        assert s.environments.railway_daily_max == 2
        assert s.environments.verify_per_round == 2
        assert s.environments.verify_minutes == 10

    def test_railway_verify_fields_normalized(self) -> None:
        """0.3.5 新字段规范化：下限 1；verify_per_round 额外封顶 3。"""
        s, _ = settings_of(
            {"environments": {"railway_daily_max": 0, "verify_per_round": 99, "verify_minutes": 0}}
        )
        assert s.environments.railway_daily_max == 1
        assert s.environments.verify_per_round == 3
        assert s.environments.verify_minutes == 1

    def test_verify_enabled_defaults_off(self) -> None:
        """[environments] verify_enabled 默认关：备资讯不挑实测、不多调模型、不申请 VM。"""
        s, _ = settings_of({})
        assert s.environments.verify_enabled is False

    def test_verify_enabled_parses_true(self) -> None:
        """配置里写 true → 挑实测开着（当前行为不变）。"""
        s, _ = settings_of({"environments": {"verify_enabled": True}})
        assert s.environments.verify_enabled is True

    def test_default_settings_have_no_groups(self) -> None:
        settings, problems = settings_of({})
        assert settings.enabled is False
        assert settings.groups == {}
        assert problems == []

    def test_default_data_dir(self, plugin_dir: Path) -> None:
        settings, _ = settings_of({})
        assert settings.data_dir == Path(plugin_dir).resolve().parents[1] / "data" / "maiwork"

    def test_default_section_values(self) -> None:
        s, _ = settings_of({})
        assert s.workspace_root == Path("/home/maiwork/workspaces")
        assert s.focus.max_members == 5
        assert s.focus.personal_profile is True
        assert s.feeds.news_slots == ("08:30", "14:00", "19:00")
        assert s.feeds.news_jitter_minutes == 30
        assert not hasattr(s, "goals")  # [goals] 节已删（主动提目标 2026-10 退役）
        assert s.topics.enabled is True
        assert s.topics.speaker == "maiwork"
        assert s.topics.per_day == 2
        assert s.topics.min_gap_hours == 3
        assert s.topics.candidate_ttl_hours == 12
        assert s.delivery.push_per_day == 3
        assert s.delivery.quiet_hours == "23:00-08:00"
        assert s.approval.required is True
        assert s.approval.admins == ("qq:100000001",)  # 默认含部署人的 QQ；要加人往后写
        assert s.approval.exempt_groups == ()
        assert s.approval.exempt_users == ()
        assert s.models.base_url == ""
        assert s.models.main == ""
        assert s.jev.enabled is True
        assert s.jev.timeout_ms == 1200  # G5：钩子阻塞管线，默认和上限都按 1200
        assert s.usage.alert_daily_tokens == 0
        assert s.console.listen == ("127.0.0.1", 18650)
        assert s.console.password == ""
        assert s.console.public_url == ""
        assert s.environments.railway is True
        assert s.environments.ssh == ()
        assert s.environments.workspace_root == Path("/home/maiwork/workspaces")
        assert s.environments.memory_max == "512M"
        assert s.environments.runtime_max_sec == 1800
        assert s.profile.batch_messages == 80
        assert s.profile.max_interval_hours == 3
        assert s.profile.backfill_days == 7
        assert s.profile.backfill_max_messages == 1500
        assert s.profile.weekly_day == 0
        assert s.profile.read_interval_minutes == 10


class TestGroups:
    def test_valid_group(self) -> None:
        s, problems = settings_of(_raw(**{"groups.serve": [{"group": "qq:900000001"}]}))
        assert problems == []
        assert set(s.groups) == {"900000001"}
        assert s.is_served("900000001") is True
        assert s.is_served("111") is False

    def test_workspace_default_and_shared(self) -> None:
        s, _ = settings_of(
            _raw(
                **{
                    "groups.serve": [
                        {"group": "qq:111"},
                        {"group": "qq:222", "workspace": "team-a"},
                        {"group": "qq:333", "workspace": "team-a"},
                    ]
                }
            )
        )
        assert s.workspace_of("111") == "g111"
        assert s.workspace_of("222") == "team-a"
        assert s.workspace_of("333") == "team-a"
        # 没配的群也给默认工作区名
        assert s.workspace_of("999") == "g999"

    def test_duplicate_group_dropped(self) -> None:
        s, problems = settings_of(
            _raw(**{"groups.serve": [{"group": "qq:111"}, {"group": "qq:111"}]})
        )
        assert set(s.groups) == {"111"}
        assert len(problems) == 1
        assert "重复" in problems[0]
        assert "111" in problems[0]

    def test_empty_group_dropped(self) -> None:
        s, problems = settings_of(_raw(**{"groups.serve": [{"group": ""}]}))
        assert s.groups == {}
        assert len(problems) == 1
        assert "空" in problems[0]

    def test_missing_group_field_dropped(self) -> None:
        s, problems = settings_of(_raw(**{"groups.serve": [{"workspace": "x"}]}))
        assert s.groups == {}
        assert len(problems) == 1

    def test_non_qq_platform_dropped(self) -> None:
        s, problems = settings_of(_raw(**{"groups.serve": [{"group": "wx:123"}]}))
        assert s.groups == {}
        assert len(problems) == 1
        assert "qq" in problems[0]
        assert "wx:123" in problems[0]

    def test_non_numeric_group_dropped(self) -> None:
        s, problems = settings_of(_raw(**{"groups.serve": [{"group": "qq:abc"}]}))
        assert s.groups == {}
        assert len(problems) == 1
        assert "数字" in problems[0]
        assert "qq:abc" in problems[0]

    def test_mixed_valid_and_invalid(self) -> None:
        s, problems = settings_of(
            _raw(
                **{
                    "groups.serve": [
                        {"group": "qq:111"},
                        {"group": "wx:123"},
                        {"group": "qq:abc"},
                        {"group": "qq:111"},
                        {"group": ""},
                    ]
                }
            )
        )
        assert set(s.groups) == {"111"}
        assert len(problems) == 4


class TestListen:
    def test_listen_host_port(self) -> None:
        s, problems = settings_of(_raw(**{"console.listen": "0.0.0.0:18650"}))
        assert s.console.listen == ("0.0.0.0", 18650)
        assert problems == []

    def test_listen_invalid_falls_back(self) -> None:
        raw = MaiWorkConfig()
        cfg = raw.model_copy(update={"console": raw.console.model_copy(update={"listen": "::1:8080"})})
        s, problems = load_settings(cfg)
        assert s.console.listen == ("127.0.0.1", 18650)
        assert len(problems) == 1
        assert "listen" in problems[0]

    def test_listen_bad_port_falls_back(self) -> None:
        s, problems = settings_of(_raw(**{"console.listen": "127.0.0.1:99999"}))
        assert s.console.listen == ("127.0.0.1", 18650)
        assert len(problems) == 1


class TestRobustness:
    def test_dict_input_accepts_instance(self) -> None:
        settings, problems = load_settings(MaiWorkConfig())
        assert isinstance(settings, Settings)
        assert problems == []

    def test_wrong_type_section_falls_back_without_exception(self) -> None:
        raw = _raw(**{"groups.serve": [{"group": "qq:111"}]})
        raw["console"] = "not-a-dict"
        s, problems = load_settings(raw)
        assert set(s.groups) == {"111"}
        assert s.console.listen == ("127.0.0.1", 18650)
        assert any("console" in p for p in problems)

    def test_full_garbage_dict_uses_defaults(self) -> None:
        raw = {"plugin": 42, "groups": "x", "feeds": [1, 2], "models": {"base_url": 3}}
        s, problems = load_settings(raw)
        assert isinstance(s, Settings)
        assert s.enabled is False
        assert s.groups == {}
        assert problems  # 至少有记问题

    def test_settings_is_frozen_snapshot(self) -> None:
        s, _ = settings_of({})
        with pytest.raises(Exception):
            s.enabled = True  # type: ignore[misc]
        with pytest.raises(TypeError):
            s.groups["111"] = None  # type: ignore[index]

    def test_instance_input(self) -> None:
        cfg = MaiWorkConfig.model_validate(_raw(**{"groups.serve": [{"group": "qq:555", "workspace": "w"}]}))
        s, problems = load_settings(cfg)
        assert problems == []
        assert s.workspace_of("555") == "w"

    def test_is_served_accepts_only_str(self) -> None:
        s, _ = settings_of(_raw(**{"groups.serve": [{"group": "qq:111"}]}))
        assert s.is_served("111") is True
        assert s.is_served(111) is False  # type: ignore[arg-type]


class TestM2Sections:
    """M2（0.2.0）新增：[jev]/[feeds] 新字段的默认值与容错（[search] 段 2026-10 已删，搜索走扩展绑定）。"""

    def test_legacy_search_section_ignored(self) -> None:
        """老配置里的 [search] 段不报错、不进 Settings（启动迁移会把它搬成扩展 + 绑定后清掉）。"""
        s, problems = settings_of(
            _raw(search={"provider": "tavily", "api_key": "tvly-test", "timeout_s": 5})
        )
        assert problems == []
        assert not hasattr(s, "search")

    def test_legacy_search_bad_section_ignored(self) -> None:
        s, problems = settings_of(_raw(search={"provider": 42}))
        assert problems == []
        assert not hasattr(s, "search")

    def test_jev_new_fields_defaults(self) -> None:
        s, problems = settings_of({})
        assert problems == []
        assert s.jev.key_file == ""
        assert s.jev.api_url == "https://api.typesafe.ai/v1/systemone"
        assert s.jev.model == "jev-1.13.0"

    def test_jev_custom_values(self) -> None:
        s, problems = settings_of(
            _raw(jev={"key_file": "/tmp/k", "api_url": "http://x.local/v1", "model": "jev-2"})
        )
        assert problems == []
        assert s.jev.key_file == "/tmp/k"
        assert s.jev.api_url == "http://x.local/v1"
        assert s.jev.model == "jev-2"

    def test_jev_blank_url_and_model_fall_back(self) -> None:
        s, _ = settings_of(_raw(jev={"api_url": "  ", "model": ""}))
        assert s.jev.api_url == "https://api.typesafe.ai/v1/systemone"
        assert s.jev.model == "jev-1.13.0"

    def test_feeds_new_fields_defaults(self) -> None:
        s, problems = settings_of({})
        assert problems == []
        assert s.feeds.max_items == 10  # 0.3.4 起：质量标准 max_items 默认 10
        assert s.feeds.lookback_days == 14
        # 0.3.4 新字段（docs/02 §4.1 质量标准）
        assert s.feeds.web_min_avg == pytest.approx(3.0)
        assert s.feeds.pool_min_avg == pytest.approx(4.0)
        assert s.feeds.guides is True

    def test_feeds_custom_values(self) -> None:
        s, _ = settings_of(_raw(feeds={"max_items": 5, "lookback_days": 30}))
        assert s.feeds.max_items == 5
        assert s.feeds.lookback_days == 30

    def test_blocked_domains_key_ignored(self) -> None:
        """[feeds] blocked_domains 已删：存量的写了也静默忽略；屏蔽名单只读按群 kv。"""
        s, problems = settings_of(_raw(feeds={"blocked_domains": ["bad.com"]}))
        assert not hasattr(s.feeds, "blocked_domains")
        assert all("blocked_domains" not in p for p in problems)

    def test_environments_m3_defaults(self) -> None:
        """M3 新增字段：local_mode / run_as / max_parallel / command_timeout_s 默认值。"""
        s, problems = settings_of({})
        assert problems == []
        assert s.environments.local_mode == "systemd"
        assert s.environments.run_as == "maiwork"
        assert s.environments.max_parallel == 2
        assert s.environments.command_timeout_s == 300

    def test_environments_m3_custom_values(self) -> None:
        s, problems = settings_of(
            _raw(
                environments={
                    "local_mode": "direct",
                    "run_as": "runner",
                    "max_parallel": 4,
                    "command_timeout_s": 60,
                }
            )
        )
        assert problems == []
        assert s.environments.local_mode == "direct"
        assert s.environments.run_as == "runner"
        assert s.environments.max_parallel == 4
        assert s.environments.command_timeout_s == 60

    def test_environments_bad_local_mode_falls_back_systemd(self) -> None:
        """别的值 → 回落 systemd 并记问题；配置错误不能让插件加载失败。"""
        s, problems = settings_of(_raw(**{"environments.local_mode": "rkt"}))
        assert s.environments.local_mode == "systemd"
        assert len(problems) == 1
        assert "local_mode" in problems[0]

    def test_example_toml_parses_without_problems(self, plugin_dir: Path) -> None:
        """config.example.toml 本身要能解析成合法配置（防止示例和代码脱节）。"""
        example = Path(plugin_dir) / "config.example.toml"
        try:
            import tomllib
        except ImportError:  # pragma: no cover - py<3.11 才有
            pytest.skip("tomllib 需要 py3.11+")
        raw = tomllib.loads(example.read_text(encoding="utf-8"))
        s, problems = load_settings(raw)
        assert problems == []
        assert "[search]" not in example.read_text(encoding="utf-8")  # 搜索走扩展绑定，不进配置文件
        assert s.jev.api_url == "https://api.typesafe.ai/v1/systemone"

    def test_example_toml_uses_new_model_structure(self, plugin_dir: Path) -> None:
        """docs/13 A12：示例必须是新结构（[[endpoints]] + [[model_list]]），不能退回旧 [models] 四槽。"""
        text = (Path(plugin_dir) / "config.example.toml").read_text(encoding="utf-8")
        try:
            import tomllib
        except ImportError:  # pragma: no cover - py<3.11 才有
            pytest.skip("tomllib 需要 py3.11+")
        raw = tomllib.loads(text)
        # 版本标记和代码里的 CONFIG_VERSION 对齐
        assert raw["plugin"]["config_version"] == CONFIG_VERSION
        # 新结构：端点 + 模型库都在，条目能挂上
        assert raw["endpoints"] and raw["model_list"]
        s, problems = load_settings(raw)
        assert problems == []
        ep_ids = {e.id for e in s.endpoints}
        assert all(m.endpoint in ep_ids for m in s.model_list)
        # 旧的 [models] 四槽不该再出现在示例里（注释里的迁移说明不算）
        assert "models" not in raw
        # 示例里不能有看起来像真的密钥 / 地址，统一用占位；
        # 第一位管理员和代码默认对齐（2026-10 docs/18 文档漂移修正）
        assert s.approval.admins == ("qq:100000001",)
        assert all(e.api_key == "" or "填" in e.api_key for e in s.endpoints)
        assert all("example.com" in e.base_url for e in s.endpoints)


def test_full_label_adds_section_so_short_names_read_alone():
    """「全部设置」页名称很短（靠所在分节看懂）；管理员对话里单独出现时要带上节名。"""
    from maiwork import rules
    assert rules.full_label("jev.timeout_ms") == "快速判断 · 等待上限"
    assert rules.full_label("topics.min_gap_hours") == "开话题 · 话题间隔"
    # 项名和节名相同时不重复
    assert rules.full_label("group_space.enabled") == "群空间"
    # 没登记的键退回键名
    assert rules.full_label("nope.x") == "nope.x"
