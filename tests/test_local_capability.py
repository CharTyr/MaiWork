"""本机执行能力自动判定（docs/02 执行方式自动判定）。

三种结果：
- fixed：Linux + systemd-run + root + 系统里有 run_as 用户 → 现在的固定用户方式（行为不变）；
- dynamic：没这个用户但 Linux+systemd-run+root → DynamicUser（固定名字 + StateDirectory）；
- stopped：其它情况（非 Linux / 没 systemd-run / 非 root / Docker）→ 本机不能隔离跑命令。

测试只注入假探针，绝不真跑 systemd / useradd（见 conftest 隔离罩）。
"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork.maiwork.environments import capability


def _probe(platform: str = "linux", root: bool = True, systemd: bool = True, user: bool = True):
    return capability.LocalCaps(
        is_linux=platform.startswith("linux"),
        is_root=root,
        has_systemd_run=systemd,
        run_as_exists=user,
    )


class TestDetect:
    def test_fixed_when_user_exists(self):
        caps = _probe(user=True)
        r = capability.detect(caps, run_as="maiwork")
        assert r.mode == "fixed"
        assert r.ok is True
        assert r.unit_user == "maiwork"
        assert r.exec_kind == "isolated"
        assert r.hint == ""
        assert "固定用户" in r.log_line

    def test_dynamic_when_user_missing(self):
        caps = _probe(user=False)
        r = capability.detect(caps, run_as="maiwork")
        assert r.mode == "dynamic"
        assert r.ok is True
        assert r.unit_user == capability.DYNAMIC_USER
        assert r.exec_kind == "isolated"
        assert "一次性系统用户" in r.log_line

    def test_stopped_not_linux(self):
        r = capability.detect(_probe(platform="darwin"), run_as="maiwork")
        assert r.mode == "stopped"
        assert r.ok is False
        assert r.exec_kind == "plugin"
        assert "不是 Linux" in r.log_line
        assert r.hint

    def test_stopped_not_root(self):
        r = capability.detect(_probe(root=False), run_as="maiwork")
        assert r.mode == "stopped"
        assert "root" in r.log_line

    def test_stopped_no_systemd_run(self):
        r = capability.detect(_probe(systemd=False), run_as="maiwork")
        assert r.mode == "stopped"
        assert "systemd" in r.log_line

    def test_stopped_reasons_in_priority_order(self):
        # 全不满足：先说不是 Linux
        r = capability.detect(_probe(platform="windows", root=False, systemd=False, user=False), run_as="x")
        assert r.mode == "stopped"
        assert "不是 Linux" in r.log_line

    def test_windows_import_fallback_marks_unsupported(self, monkeypatch):
        """Windows 没有 pwd 模块：_probe 不能因为 import pwd 失败而炸。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pwd":
                raise ImportError("No module named 'pwd'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        caps = capability._probe()
        # 本机是 mac/linux：其余探测照常，但绝不能抛 ImportError
        assert isinstance(caps.is_root, bool)


class TestWorkspaceRoot:
    def test_fixed_uses_config_root(self):
        r = capability.resolve_workspace_root(
            Path("/home/maiwork/workspaces"), capability.detect(_probe(user=True), run_as="maiwork")
        )
        assert r == Path("/home/maiwork/workspaces")

    def test_dynamic_uses_private_statedir(self):
        dec = capability.detect(_probe(user=False), run_as="maiwork")
        r = capability.resolve_workspace_root(Path("/home/maiwork/workspaces"), dec)
        assert r == Path("/var/lib/private/maiwork/workspaces")

    def test_stopped_uses_data_dir(self):
        dec = capability.detect(_probe(root=False), run_as="maiwork")
        r = capability.resolve_workspace_root(
            Path("/home/maiwork/workspaces"), dec, data_dir=Path("/data/mw")
        )
        assert r == Path("/data/mw/workspaces")


class TestProbeIsolated:
    def test_probe_no_real_systemd(self, monkeypatch):
        """探测绝不能真跑 systemd-run（隔离罩要求）。"""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda _n: None)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不许真跑 subprocess")),
        )
        caps = capability._probe()
        assert caps.has_systemd_run is False
