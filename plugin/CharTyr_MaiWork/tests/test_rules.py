"""rules.py 单元测试：网页可改的规则（kv["rules.override"] → 有效设置合并层）。

- 校验：字段白名单 / HH:MM-HH:MM / HH:MM 列表 / QQ 号纯数字 / 数值范围 / 类型；
- 合并：base（config.toml 的 Settings）被 override 逐字段覆盖，子节用 dataclasses.replace 出新对象；
- 读写：只存改过的字段；一个字段写坏了整次不落库（ValueError 中文原因）；
- 响应结构：GET 的 values / overridden / defaults_from_file。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _base(cfg: dict | None = None):
    settings, _ = load_settings(cfg or {})
    return settings


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


class TestValidate:
    def test_valid_patch(self):
        from CharTyr_MaiWork.rules import validate_patch

        key, value = validate_patch("delivery.quiet_hours", "22:30-07:00")
        assert key == "delivery.quiet_hours"
        assert value == "22:30-07:00"

    def test_unknown_section_rejected(self):
        from CharTyr_MaiWork.rules import validate_patch

        with pytest.raises(ValueError, match="不认识的规则"):
            validate_patch("models.main", "x")

    def test_unknown_field_rejected(self):
        from CharTyr_MaiWork.rules import validate_patch

        with pytest.raises(ValueError, match="不认识的规则"):
            validate_patch("delivery.mention_ttl_minutes", 30)

    def test_bad_shape(self):
        from CharTyr_MaiWork.rules import validate_patch

        for bad in ("delivery", "delivery.", ".quiet_hours", "delivery.quiet_hours.x", "Delivery.quiet_hours"):
            with pytest.raises(ValueError):
                validate_patch(bad, "23:00-08:00")

    def test_quiet_hours_format(self):
        from CharTyr_MaiWork.rules import validate_patch

        for bad in ("23:00", "23:00-25:00", "23:00-8:00", "23:60-08:00", "23-08", "23:00~08:00", "", 2300, None):
            with pytest.raises(ValueError, match="睡觉时段"):
                validate_patch("delivery.quiet_hours", bad)
        # 同刻 = 全天，合法
        key, v = validate_patch("delivery.quiet_hours", "08:00-08:00")
        assert v == "08:00-08:00"

    def test_news_slots(self):
        from CharTyr_MaiWork.rules import validate_patch

        _, v = validate_patch("feeds.news_slots", ["08:30", " 14:00 ", "19:00"])
        assert v == ["08:30", "14:00", "19:00"]
        for bad in (["8:30"], ["24:00"], ["12:60"], "08:30", ["08:30", 8], []):
            with pytest.raises(ValueError, match="时段"):
                validate_patch("feeds.news_slots", bad)

    def test_push_per_day_range(self):
        from CharTyr_MaiWork.rules import validate_patch

        _, v = validate_patch("delivery.push_per_day", 5)
        assert v == 5
        for bad in (0, 11, "abc", None, True, 1.5):
            with pytest.raises(ValueError, match="推送上限"):
                validate_patch("delivery.push_per_day", bad)

    def test_topics_fields(self):
        from CharTyr_MaiWork.rules import validate_patch

        _, v = validate_patch("topics.enabled", False)
        assert v is False
        _, v = validate_patch("topics.per_day", 1)
        assert v == 1
        _, v = validate_patch("topics.min_gap_hours", 24)
        assert v == 24
        with pytest.raises(ValueError):
            validate_patch("topics.per_day", 0)
        with pytest.raises(ValueError):
            validate_patch("topics.per_day", 11)
        with pytest.raises(ValueError):
            validate_patch("topics.min_gap_hours", 25)
        with pytest.raises(ValueError, match="开关"):
            validate_patch("topics.enabled", "yes")

    def test_approval_fields(self):
        from CharTyr_MaiWork.rules import validate_patch

        key, v = validate_patch("approval.admins", ["10001", " 10002 "])
        assert v == ["qq:10001", "qq:10002"]  # 纯数字当 qq（「平台:账号」写法）
        _, v = validate_patch("approval.exempt_groups", [])
        assert v == []
        _, v = validate_patch("approval.exempt_users", ["998"])
        assert v == ["qq:998"]
        for field in ("approval.admins", "approval.exempt_groups", "approval.exempt_users"):
            for bad in (["abc"], ["10001a"], ["123 45"], "10001", None):
                with pytest.raises(ValueError, match="平台:账号"):
                    validate_patch(field, bad)
        with pytest.raises(ValueError, match="开关"):
            validate_patch("approval.required", 1)
        with pytest.raises(ValueError, match="开关"):
            validate_patch("approval.remind", [])

    def test_feeds_scores(self):
        from CharTyr_MaiWork.rules import validate_patch

        _, v = validate_patch("feeds.web_min_avg", 3.5)
        assert v == 3.5
        _, v = validate_patch("feeds.pool_min_avg", 5)
        assert v == 5.0
        for field in ("feeds.web_min_avg", "feeds.pool_min_avg"):
            for bad in (0.9, 5.1, "3.5", None, True):
                with pytest.raises(ValueError, match="门槛"):
                    validate_patch(field, bad)
        _, v = validate_patch("feeds.max_items", 1)
        assert v == 1
        with pytest.raises(ValueError):
            validate_patch("feeds.max_items", 21)
        with pytest.raises(ValueError):
            validate_patch("feeds.max_items", 0)
        with pytest.raises(ValueError, match="开关"):
            validate_patch("feeds.guides", "true")


# ----------------------------------------------------------------------
# 读写（只存改过的，整次校验失败不落库）
# ----------------------------------------------------------------------


class TestSave:
    def test_save_and_read(self, store: Store):
        from CharTyr_MaiWork import rules

        out = rules.save_patch(store, {"delivery": {"push_per_day": 5}, "topics": {"enabled": False}})
        assert out == {"delivery": {"push_per_day": 5}, "topics": {"enabled": False}}
        assert store.kv_get(rules.KV_OVERRIDE) == {"delivery": {"push_per_day": 5}, "topics": {"enabled": False}}

    def test_save_merges_with_existing(self, store: Store):
        from CharTyr_MaiWork import rules

        rules.save_patch(store, {"delivery": {"push_per_day": 5}})
        rules.save_patch(store, {"delivery": {"quiet_hours": "22:00-07:00"}})
        assert store.kv_get(rules.KV_OVERRIDE) == {
            "delivery": {"push_per_day": 5, "quiet_hours": "22:00-07:00"}
        }

    def test_bad_field_rejects_whole_patch(self, store: Store):
        from CharTyr_MaiWork import rules

        with pytest.raises(ValueError):
            rules.save_patch(store, {"delivery": {"push_per_day": 0, "quiet_hours": "22:00-07:00"}})
        assert store.kv_get(rules.KV_OVERRIDE) is None

    def test_empty_section_ignored(self, store: Store):
        from CharTyr_MaiWork import rules

        out = rules.save_patch(store, {"delivery": {}, "topics": {"enabled": False}})
        assert out == {"topics": {"enabled": False}}

    def test_na_equal_override_removed(self, store: Store):
        """写成和 config.toml 一样的值 = 没改，这条覆盖自动清掉。"""
        from CharTyr_MaiWork import rules

        base = _base({"delivery": {"push_per_day": 3}})
        out = rules.save_patch(store, {"delivery": {"push_per_day": 3}}, base=base)
        assert out == {}
        assert store.kv_get(rules.KV_OVERRIDE) in (None, {})

    def test_unknown_key_in_patch_rejected(self, store: Store):
        from CharTyr_MaiWork import rules

        with pytest.raises(ValueError):
            rules.save_patch(store, {"delivery": {"push_per_day": 3, "bogus": 1}})
        assert store.kv_get(rules.KV_OVERRIDE) is None

    def test_reset_field(self, store: Store):
        from CharTyr_MaiWork import rules

        rules.save_patch(store, {"delivery": {"push_per_day": 5, "quiet_hours": "22:00-07:00"}})
        rules.reset_field(store, "delivery.push_per_day")
        assert store.kv_get(rules.KV_OVERRIDE) == {"delivery": {"quiet_hours": "22:00-07:00"}}
        # reset 一个不存在的字段：幂等
        rules.reset_field(store, "delivery.push_per_day")
        rules.reset_field(store, "feeds.guides")
        with pytest.raises(ValueError):
            rules.reset_field(store, "delivery.mention_ttl_minutes")
        with pytest.raises(ValueError):
            rules.reset_field(store, "bogus")


# ----------------------------------------------------------------------
# 合并
# ----------------------------------------------------------------------


class TestMerge:
    def test_merge_replaces_named_fields(self, store: Store):
        from CharTyr_MaiWork import rules

        base = _base({"delivery": {"push_per_day": 3, "quiet_hours": "23:00-08:00"}})
        over = {"delivery": {"push_per_day": 6}}
        merged = rules.effective_settings(base, over)
        assert merged is not base
        assert merged.delivery.push_per_day == 6
        assert merged.delivery.quiet_hours == "23:00-08:00"
        # base 本身没被改（frozen 的意味）
        assert base.delivery.push_per_day == 3

    def test_merge_all_sections(self, store: Store):
        from CharTyr_MaiWork import rules

        base = _base({})
        over = {
            "delivery": {"quiet_hours": "22:00-09:00", "push_per_day": 1},
            "topics": {"enabled": False, "per_day": 4, "min_gap_hours": 6},
            "approval": {"required": False, "admins": ["111"], "exempt_groups": ["222"], "exempt_users": ["333"], "remind": False},
            "feeds": {"news_slots": ["09:00"], "max_items": 7, "web_min_avg": 2.5, "pool_min_avg": 4.5, "guides": False},
        }
        merged = rules.effective_settings(base, over)
        assert merged.delivery.quiet_hours == "22:00-09:00"
        assert merged.delivery.push_per_day == 1
        assert merged.topics.enabled is False
        assert merged.topics.per_day == 4
        assert merged.topics.min_gap_hours == 6
        assert merged.approval.required is False
        assert merged.approval.admins == ("qq:111",)
        assert merged.approval.exempt_groups == ("qq:222",)
        assert merged.approval.exempt_users == ("qq:333",)
        assert merged.approval.remind is False
        assert merged.feeds.news_slots == ("09:00",)
        assert merged.feeds.max_items == 7
        assert merged.feeds.web_min_avg == 2.5
        assert merged.feeds.pool_min_avg == 4.5
        assert merged.feeds.guides is False
        # 没覆盖的节原样（同一对象）
        assert merged.models is base.models
        assert merged.groups is base.groups

    def test_merge_ignores_dirty_kv(self, store: Store):
        """kv 被人手改坏了：能用的字段接着用，坏的静默忽略（不能让网页设置把配置搞炸）。"""
        from CharTyr_MaiWork import rules

        base = _base({"delivery": {"push_per_day": 3}})
        over = {"delivery": {"push_per_day": "not-a-number", "quiet_hours": "22:00-07:00"}, "bogus": {"x": 1}}
        merged = rules.effective_settings(base, over)
        assert merged.delivery.push_per_day == 3
        assert merged.delivery.quiet_hours == "22:00-07:00"

    def test_merge_none_override_returns_base(self, store: Store):
        from CharTyr_MaiWork import rules

        base = _base({})
        assert rules.effective_settings(base, None) is base
        assert rules.effective_settings(base, {}) is base
        assert rules.effective_settings(base, "junk") is base


# ----------------------------------------------------------------------
# 响应结构（GET /api/settings/rules 的数据层）
# ----------------------------------------------------------------------


class TestView:
    def test_view_structure_and_overridden(self, store: Store):
        from CharTyr_MaiWork import rules

        base = _base({
            "delivery": {"push_per_day": 3, "quiet_hours": "23:00-08:00"},
            "approval": {"required": True, "admins": ["100000001"], "remind": True},
        })
        view = rules.rules_view(base, store)
        assert set(view["values"].keys()) == {"delivery", "topics", "approval", "feeds"}
        assert view["values"]["delivery"] == {"quiet_hours": "23:00-08:00", "push_per_day": 3}
        assert view["values"]["feeds"]["news_slots"] == ["08:30", "14:00", "19:00"]
        assert view["values"]["feeds"]["guides"] is True
        assert view["values"]["approval"]["admins"] == ["qq:100000001"]
        assert view["overridden"] == []
        # defaults_from_file 同结构（config.toml 的原始值）
        assert view["defaults_from_file"]["delivery"]["push_per_day"] == 3
        assert view["defaults_from_file"]["topics"]["enabled"] is True

        rules.save_patch(store, {"delivery": {"push_per_day": 9}, "approval": {"remind": False}})
        view2 = rules.rules_view(base, store)
        assert view2["values"]["delivery"]["push_per_day"] == 9
        assert view2["values"]["approval"]["remind"] is False
        assert sorted(view2["overridden"]) == ["approval.remind", "delivery.push_per_day"]
        # defaults 保持 config 原值
        assert view2["defaults_from_file"]["delivery"]["push_per_day"] == 3
        assert view2["defaults_from_file"]["approval"]["remind"] is True
