"""environments/vm_platforms.py：一次性 VM 平台预设表 + 工厂（2026-10-15）。

只验证「表里的知识对不对、工厂挑得对不对、描述文案查得到」——
不发任何网络请求，也不碰真 Freestyle（红线）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments import vm_platforms


def _settings(tmp_path: Path, cfg: dict | None = None) -> Any:
    raw = {"environments": {"workspace_root": str(tmp_path / "ws")}}
    if cfg:
        raw["environments"].update(cfg)
    s, _ = load_settings(raw)
    return s


# ----------------------------------------------------------------------
# 预设表
# ----------------------------------------------------------------------


class TestPresets:
    def test_two_platforms_registered(self) -> None:
        assert set(vm_platforms.PRESETS) == {"railway", "freestyle"}
        assert vm_platforms.kinds() == ("railway", "freestyle")

    def test_ids_match_keys(self) -> None:
        for key, preset in vm_platforms.PRESETS.items():
            assert preset.id == key
            assert preset.kind == key
            assert preset.label
            assert preset.note

    def test_railway_preset(self) -> None:
        p = vm_platforms.get("railway")
        assert p is not None
        assert p.needs_key is False
        assert p.key_url == ""
        assert p.default_snapshot == ""
        assert "railway" in p.console_url

    def test_freestyle_preset(self) -> None:
        p = vm_platforms.get("freestyle")
        assert p is not None
        assert p.needs_key is True
        assert p.key_url == "https://dash.freestyle.sh"
        assert p.console_url == "https://dash.freestyle.sh"
        assert "freestyle.sh" in p.docs_url
        assert p.default_snapshot == "freestyle/ubuntu-sm"
        assert p.ttl_minutes == 60

    def test_freestyle_snapshot_specs(self) -> None:
        """镜像决定规格：sm = 2 核 4G，默认 ubuntu = 4 核 8G（免费档上限）。"""
        p = vm_platforms.get("freestyle")
        assert p is not None
        sm = p.snapshot_spec("freestyle/ubuntu-sm")
        assert sm is not None and (sm.cpu, sm.memory_mib) == (2, 4096)
        big = p.snapshot_spec("freestyle/ubuntu")
        assert big is not None and (big.cpu, big.memory_mib) == (4, 8192)
        # 自定义快照（用户自己拍的）认不出 → None，不瞎猜规格
        assert p.snapshot_spec("sh-1234") is None

    def test_get_unknown_is_none(self) -> None:
        assert vm_platforms.get("nope") is None
        assert vm_platforms.get("") is None


# ----------------------------------------------------------------------
# 开关判定
# ----------------------------------------------------------------------


class TestEnabled:
    def test_railway_follows_environments_railway(self, tmp_path: Path) -> None:
        p = vm_platforms.get("railway")
        assert p is not None
        assert p.enabled(_settings(tmp_path, {"railway": True})) is True
        assert p.enabled(_settings(tmp_path, {"railway": False})) is False

    def test_railway_selected_by_default(self, tmp_path: Path) -> None:
        assert vm_platforms.selected_kind(_settings(tmp_path)) == "railway"

    def test_freestyle_follows_its_own_switch(self, tmp_path: Path) -> None:
        p = vm_platforms.get("freestyle")
        assert p is not None
        assert p.enabled(_settings(tmp_path, {"vm_platform": "freestyle", "freestyle": True})) is True
        assert p.enabled(_settings(tmp_path, {"vm_platform": "railway"})) is False
        # 开关开着就算开，跟「优先用哪家」无关（不再二选一）
        assert p.enabled(_settings(tmp_path, {"vm_platform": "railway", "freestyle": True})) is True

    def test_freestyle_needs_key(self, tmp_path: Path) -> None:
        p = vm_platforms.get("freestyle")
        assert p is not None
        no_key = _settings(tmp_path, {"vm_platform": "freestyle", "freestyle": True})
        assert p.has_key(no_key) is False
        assert p.ready(no_key) is False
        with_key = _settings(tmp_path, {"vm_platform": "freestyle", "freestyle": True, "freestyle_api_key": "fs-x"})
        assert p.has_key(with_key) is True
        assert p.ready(with_key) is True

    def test_railway_ready_without_key(self, tmp_path: Path) -> None:
        p = vm_platforms.get("railway")
        assert p is not None
        assert p.ready(_settings(tmp_path)) is True

    def test_missing_environments_section_is_not_enabled(self) -> None:
        class _NoEnv:
            environments = None

        for p in vm_platforms.PRESETS.values():
            assert p.enabled(_NoEnv()) is False
            assert p.ready(_NoEnv()) is False


# ----------------------------------------------------------------------
# 描述文案（任务 env 字段 / 提示词）
# ----------------------------------------------------------------------


class TestDescribe:
    def test_railway_desc_unchanged(self) -> None:
        class _Box:
            kind = "railway"
            expires_ts = 0.0

        text = vm_platforms.describe("railway", _Box())
        assert text.startswith("railway.new 一次性机器")
        assert "2 核 2G" in text

    def test_freestyle_desc_from_box_resources(self) -> None:
        class _Box:
            kind = "freestyle"
            expires_ts = 0.0
            cpu = 2
            memory_mib = 4096

        text = vm_platforms.describe("freestyle", _Box())
        assert "Freestyle" in text and "2 核 4G" in text

    def test_freestyle_desc_falls_back_to_snapshot_spec(self) -> None:
        class _Box:
            kind = "freestyle"
            expires_ts = 0.0
            snapshot = "freestyle/ubuntu"

        text = vm_platforms.describe("freestyle", _Box())
        assert "4 核 8G" in text

    def test_unknown_kind_still_gives_text(self) -> None:
        class _Box:
            kind = "future"
            expires_ts = 0.0

        text = vm_platforms.describe("future", _Box())
        assert text  # 不抛、也不空


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------


class TestMakeEnv:
    def test_unknown_kind_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            vm_platforms.make_env("nope", lambda: _settings(tmp_path), tmp_path / "data")

    def test_railway_env(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.environments.railway import RailwayEnv

        env = vm_platforms.make_env("railway", lambda: _settings(tmp_path), tmp_path / "data")
        assert isinstance(env, RailwayEnv)

    def test_freestyle_env_gets_transport(self, tmp_path: Path) -> None:
        import httpx

        from CharTyr_MaiWork.maiwork.environments.freestyle import FreestyleEnv

        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        env = vm_platforms.make_env(
            "freestyle", lambda: _settings(tmp_path), tmp_path / "data", transport=transport
        )
        assert isinstance(env, FreestyleEnv)
        assert env._transport is transport  # noqa: SLF001  （测试要证明注入口真接上了）

    def test_kind_of_selected_platform(self, tmp_path: Path) -> None:
        assert vm_platforms.selected_kind(_settings(tmp_path, {"vm_platform": "freestyle", "freestyle": True})) == "freestyle"
        assert vm_platforms.selected_kind(_settings(tmp_path, {"vm_platform": "railway"})) == "railway"
