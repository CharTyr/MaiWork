"""rules.py 单元测试：网页可改的配置项（直写 config.toml 的新契约）。

旧版 kv["rules.override"] 覆盖层已移除（网页/对话改配置直接写 config.toml，
config_file.py 保注释），本文件测现在的公共面：

- save_config_patch：扁平 {"节.字段": 值} 校验 —— 不认识 / 每群自己管 / readonly
  一律 ValueError；secret 字段 ""=不改、null=清空、非空写入（不落日志）；
- reset_config_field：删键回代码默认值；
- is_group_managed / group_managed_hint / full_label / parse_serve_group；
- config_view 响应结构（values / readonly / group_managed 标注）。

文件落点（config_file.write_fields / delete_fields）打桩，不碰真 config.toml。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import rules
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def no_file_writes(monkeypatch):
    """截掉 config.toml 落盘；记录调用方便断言。"""
    from CharTyr_MaiWork.maiwork import config_file

    calls: list[tuple[str, Any]] = []

    def _write(p_dir, d_dir, fields):
        calls.append(("write", dict(fields)))

    def _delete(p_dir, d_dir, keys):
        calls.append(("delete", list(keys)))

    monkeypatch.setattr(config_file, "write_fields", _write)
    monkeypatch.setattr(config_file, "delete_fields", _delete)
    return calls


def _base(cfg: dict | None = None):
    settings, _ = load_settings(cfg or {})
    return settings


# ----------------------------------------------------------------------
# save_config_patch：校验
# ----------------------------------------------------------------------


class TestSavePatch:
    def test_writes_and_returns_sorted_keys(self, store, no_file_writes):
        changed = rules.save_config_patch(
            store, {"focus.max_members": 9, "feeds.max_items": 12}, base=_base(),
        )
        assert changed == ["feeds.max_items", "focus.max_members"]
        assert no_file_writes == [
            ("write", {"feeds.max_items": 12, "focus.max_members": 9})
        ]

    def test_non_dict_body_rejected(self, store, no_file_writes):
        with pytest.raises(ValueError):
            rules.save_config_patch(store, ["feeds.max_items"], base=_base())
        with pytest.raises(ValueError):
            rules.save_config_patch(store, {}, base=_base())
        assert no_file_writes == []

    def test_unknown_field_rejected_and_nothing_written(self, store, no_file_writes):
        with pytest.raises(ValueError, match="不认识"):
            rules.save_config_patch(store, {"nope.field": 1}, base=_base())
        # 一个字段不过整次不写
        with pytest.raises(ValueError):
            rules.save_config_patch(
                store, {"feeds.max_items": 12, "nope.field": 1}, base=_base(),
            )
        assert no_file_writes == []

    def test_readonly_rejected(self, store, no_file_writes):
        with pytest.raises(ValueError):
            rules.save_config_patch(
                store, {"storage.data_dir": "/tmp/x"}, base=_base(),
            )
        assert no_file_writes == []

    def test_group_managed_rejected_with_hint(self, store, no_file_writes):
        for key in ("delivery.push_per_day", "approval.required", "topics.per_day"):
            with pytest.raises(ValueError, match="每个群自己一份"):
                rules.save_config_patch(store, {key: 1}, base=_base())
        assert no_file_writes == []

    def test_models_section_not_writable_here(self, store, no_file_writes):
        with pytest.raises(ValueError, match="模型与专岗"):
            rules.save_config_patch(store, {"models.main": "gpt-x"}, base=_base())

    def test_secret_empty_means_noop(self, store, no_file_writes):
        """secret 字段给 "" = 不改；整次没东西可写 → 返回 []、不碰文件。"""
        changed = rules.save_config_patch(
            store, {"environments.freestyle_api_key": ""}, base=_base(),
        )
        assert changed == []
        assert no_file_writes == []

    def test_secret_write_and_clear(self, store, no_file_writes):
        changed = rules.save_config_patch(
            store, {"environments.freestyle_api_key": "fsk-test"}, base=_base(),
        )
        assert changed == ["environments.freestyle_api_key"]
        assert no_file_writes[-1] == ("write", {"environments.freestyle_api_key": "fsk-test"})
        changed = rules.save_config_patch(
            store, {"environments.freestyle_api_key": None}, base=_base(),
        )
        assert changed == ["environments.freestyle_api_key"]
        assert no_file_writes[-1] == ("write", {"environments.freestyle_api_key": ""})

    def test_int_field_validated(self, store, no_file_writes):
        changed = rules.save_config_patch(
            store, {"feeds.max_items": 5}, base=_base(),
        )
        assert changed == ["feeds.max_items"]
        with pytest.raises(ValueError):
            rules.save_config_patch(
                store, {"feeds.max_items": "不是数字"}, base=_base(),
            )


# ----------------------------------------------------------------------
# reset_config_field
# ----------------------------------------------------------------------


class TestResetField:
    def test_reset_deletes_key(self, store, no_file_writes):
        changed = rules.reset_config_field(store, "feeds.max_items", base=_base())
        assert changed == ["feeds.max_items"]
        assert no_file_writes == [("delete", ["feeds.max_items"])]

    def test_reset_unknown_rejected(self, store, no_file_writes):
        with pytest.raises(ValueError, match="不认识"):
            rules.reset_config_field(store, "nope.field", base=_base())
        assert no_file_writes == []

    def test_reset_group_managed_rejected(self, store, no_file_writes):
        with pytest.raises(ValueError, match="每个群自己一份"):
            rules.reset_config_field(store, "approval.required", base=_base())
        assert no_file_writes == []


# ----------------------------------------------------------------------
# 标注 / 小函数
# ----------------------------------------------------------------------


class TestLabels:
    def test_is_group_managed(self):
        assert rules.is_group_managed("delivery.push_per_day")
        assert rules.is_group_managed("approval.required")
        assert not rules.is_group_managed("feeds.max_items")
        assert not rules.is_group_managed("nope.field")

    def test_group_managed_hint_text(self):
        assert rules.group_managed_hint("approval.required")
        assert rules.group_managed_hint("nope.field") == ""

    def test_full_label(self):
        assert rules.full_label("feeds.max_items") == "资讯 · 每批条数"
        # 不认识的键原样回
        assert rules.full_label("nope.field") == "nope.field"

    def test_parse_serve_group(self):
        plat, gid = rules.parse_serve_group("qq:900000001")
        assert (plat, gid) == ("qq", "900000001")
        with pytest.raises(ValueError, match="缺平台前缀"):
            rules.parse_serve_group("900000001")


# ----------------------------------------------------------------------
# config_view
# ----------------------------------------------------------------------


class TestConfigView:
    def test_view_shape(self, store):
        view = rules.config_view(_base(), store)
        assert isinstance(view, dict)
        assert "sections" in view
        flat = [f for s in view["sections"] for f in s.get("fields", [])]
        keys = {f["key"] for f in flat}
        assert "feeds.max_items" in keys
        # 每群自己管的字段不在「全部配置」里列出（改了也一律 400）
        assert "approval.required" not in keys
        assert "delivery.push_per_day" not in keys
        # secret 字段标 secret、绝不带真值
        sec = {f["key"]: f for f in flat if f.get("type") == "secret"}
        assert "console.password" in sec
        assert all("value" not in f or f.get("value") in (None, "", True, False) for f in sec.values())
