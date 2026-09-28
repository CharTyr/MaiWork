"""G1 / direct 生产闸回归测试：线上以 root 运行时禁止 direct；direct 默认整体不生效。

direct 模式不隔离：子 agent 直接以插件进程身份跑（线上就是 root），等于把
root shell 交给模型。2026-10 起：
- 配置里写 local_mode="direct" **默认不生效**（生产路径一律 systemd）并记中文问题；
- 只有进程环境变量 MAIWORK_DEV_ALLOW_DIRECT=1（显式开发开关）且不是 root 才允许 direct；
- 开了开关但是 root → 仍然回落 systemd（子 agent 会直接拿到 root shell）。

注意：conftest 的隔离罩把所有测试默认 patch 成「非 root」且设上开发开关，
本文件专测这些闸门——每个用例自己把它们调成需要的值。
"""

from __future__ import annotations

import sys

import pytest

from CharTyr_MaiWork.maiwork import config
from CharTyr_MaiWork.maiwork.config import DEV_ALLOW_DIRECT_ENV, load_settings


def _raw(local_mode: str) -> dict:
    return {"environments": {"local_mode": local_mode, "workspace_root": "/tmp/x"}}


class TestDirectModeForbiddenAsRoot:
    def test_direct_as_root_falls_back_to_systemd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """开了开发开关、但进程是 root → systemd + 问题清单带原因。"""
        monkeypatch.setattr(config, "_running_as_root", lambda: True)
        monkeypatch.setenv(DEV_ALLOW_DIRECT_ENV, "1")
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "systemd"
        assert any("root" in p and "direct" in p for p in problems)

    def test_direct_as_non_root_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """开了开发开关、普通用户：direct 才照旧能用，不记问题。"""
        monkeypatch.setattr(config, "_running_as_root", lambda: False)
        monkeypatch.setenv(DEV_ALLOW_DIRECT_ENV, "1")
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "direct"
        assert not any("root" in p and "direct" in p for p in problems)

    def test_systemd_unaffected_by_euid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "_running_as_root", lambda: True)
        s, problems = load_settings(_raw("systemd"))
        assert s.environments.local_mode == "systemd"
        assert not any("root" in p and "direct" in p for p in problems)


class TestDirectNeedsDevSwitch:
    def test_direct_without_switch_falls_back_to_systemd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """生产路径（没有开发开关）里 direct 不再生效 → systemd + 中文问题。"""
        monkeypatch.delenv(DEV_ALLOW_DIRECT_ENV, raising=False)
        monkeypatch.setattr(config, "_running_as_root", lambda: False)
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "systemd"
        assert any(
            "direct" in p and "不生效" in p and DEV_ALLOW_DIRECT_ENV in p for p in problems
        ), problems

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", " 1x "])
    def test_switch_must_be_exactly_one(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """开关只认 "1"（去掉空白后完全相等）；别的值等于没开。"""
        monkeypatch.setattr(config, "_running_as_root", lambda: False)
        monkeypatch.setenv(DEV_ALLOW_DIRECT_ENV, value)
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "systemd"
        assert problems

    def test_switch_on_non_root_keeps_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "_running_as_root", lambda: False)
        monkeypatch.setenv(DEV_ALLOW_DIRECT_ENV, "1")
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "direct"
        assert problems == []

    def test_switch_on_but_root_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "_running_as_root", lambda: True)
        monkeypatch.setenv(DEV_ALLOW_DIRECT_ENV, "1")
        s, problems = load_settings(_raw("direct"))
        assert s.environments.local_mode == "systemd"
        assert any("root" in p for p in problems)

    def test_systemd_never_needs_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(DEV_ALLOW_DIRECT_ENV, raising=False)
        s, problems = load_settings(_raw("systemd"))
        assert s.environments.local_mode == "systemd"
        assert problems == []


class TestWindowsImport:
    """C 任务：Windows 没有 pwd 模块——插件照样要能导入、判定成「受限」。

    模拟方式：monkeypatch builtins.__import__，import "pwd" 时抛 ImportError
    （和 Windows 真实表现一致），然后清掉 sys.modules 里的缓存重新导入。
    """

    def _block_pwd(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pwd":
                raise ImportError("No module named 'pwd'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        for mod in (
            "CharTyr_MaiWork.maiwork.environments.local",
            "CharTyr_MaiWork.maiwork.environments",
            "pwd",
        ):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    def test_environments_local_imports_without_pwd(self, monkeypatch):
        self._block_pwd(monkeypatch)
        import importlib

        mod = importlib.import_module("CharTyr_MaiWork.maiwork.environments.local")
        assert mod.pwd is None

        # 本地执行环境照样能建（stopped 平台就当 direct 防线的自然降级）
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            from CharTyr_MaiWork.maiwork.config import load_settings

            s, _ = load_settings({"environments": {"local_mode": "direct", "workspace_root": td}})
            env = mod.LocalEnv(lambda: s)
            assert env.workspace("ws").is_dir()

    def test_capability_detects_stopped_without_pwd(self, monkeypatch):
        self._block_pwd(monkeypatch)
        from CharTyr_MaiWork.maiwork.environments import capability

        dec = capability.detect(capability._probe("maiwork"), run_as="maiwork")
        # 本机是 mac（不是 Linux）：必然 stopped；关键是 import 阶段不炸
        # （conftest 把 capability.probe 换成了测试用的 fixed 假判定，这里直接走真探测）
        assert dec.mode == "stopped"
        assert dec.ok is False
        assert dec.hint  # 有大白话修复建议

    def test_app_imports_and_builds_without_pwd(self, monkeypatch, tmp_path):
        """app.py 的 _import_m2_class("environments.local", ...) 在没 pwd 时也能拿到 LocalEnv。"""
        self._block_pwd(monkeypatch)
        import importlib

        import CharTyr_MaiWork.maiwork.app as app_mod

        importlib.reload(app_mod)
        cls = app_mod._import_m2_class("environments.local", "LocalEnv")
        assert cls is not None, "没 pwd 时 LocalEnv 不能是 None（否则本机执行静默消失）"
