"""宿主契约测试：必须在线上 MaiBot 的 venv 里跑（见 docs/04-部署与验证.md）。

缺宿主依赖时直接报错，不许 skip——否则会“全绿”但一上线就加载失败。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from src.plugin_runtime.runner.manifest_validator import ManifestValidator  # noqa: E402
from src.plugin_runtime.runner.plugin_loader import PluginLoader  # noqa: E402

from CharTyr_MaiWork.plugin import PLUGIN_ID, create_plugin  # noqa: E402


class HostContractTest(unittest.TestCase):
    def test_manifest_valid(self) -> None:
        v = ManifestValidator()
        m = v.load_from_plugin_path(PLUGIN_DIR)
        self.assertIsNotNone(m)
        self.assertEqual(v.errors, [])
        self.assertEqual(v.warnings, [])

    def test_lifecycle_contract(self) -> None:
        PluginLoader._validate_sdk_plugin_contract(PLUGIN_ID, create_plugin())

    def test_default_disabled(self) -> None:
        from CharTyr_MaiWork.plugin import MaiWorkConfig

        self.assertFalse(MaiWorkConfig().plugin.enabled)

    def test_lifecycle_runs(self) -> None:
        p = create_plugin()
        asyncio.run(p.on_load())
        asyncio.run(p.on_unload())

    def test_all_modules_import(self) -> None:
        """宿主 venv 里每个模块都能导入（相对导入、依赖版本）。"""
        import importlib

        # 入口 plugin.py + maiwork/ 子包里的全部模块（含 console / environments / platforms）
        importlib.import_module("CharTyr_MaiWork.plugin")
        src = PLUGIN_DIR / "maiwork"
        names = []
        for f in sorted(src.rglob("*.py")):
            if f.name.startswith("._") or "__pycache__" in f.parts:
                continue
            rel = f.relative_to(src).with_suffix("")
            parts = [p for p in rel.parts if p != "__init__"]
            names.append(".".join(["maiwork", *parts]))
        assert len(names) > 40, names
        for n in names:
            importlib.import_module(f"CharTyr_MaiWork.{n}")

    def test_hook_registered(self) -> None:
        comps = create_plugin().get_components()
        text = repr(comps)
        self.assertIn("chat.receive.after_process", text)
        self.assertIn("maiwork_intake", text)
        # 不给 MaiBot 的 planner 注册任何工具
        for c in comps:
            kind = str(c.get("component_type") or c.get("type") or "").lower()
            self.assertNotIn("tool", kind, c)

    def test_old_config_merges(self) -> None:
        """线上旧版 config.toml（0.0.1，只有 [plugin]）能被规范化成新结构，且仍是关闭的。"""
        p = create_plugin()
        cfg, _changed = p.normalize_plugin_config({"plugin": {"enabled": False, "config_version": "0.0.1"}})
        self.assertFalse(cfg["plugin"]["enabled"])
        self.assertIn("groups", cfg)

    def test_enabled_start_stop_in_host_venv(self) -> None:
        """在宿主 venv 里真的开一次：建库、开网页端口、能访问、关掉后端口释放。"""
        import socket
        import tempfile
        import urllib.request

        class _Ctx:
            async def call_capability(self, name, **kw):  # noqa: ANN001
                raise RuntimeError("契约测试里没有宿主")

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        tmp = tempfile.mkdtemp(prefix="maiwork-ci-")
        p = create_plugin()
        raw = {
            "plugin": {"enabled": True, "config_version": "0.0.1"},
            "groups": {"serve": [{"group": "qq:900000001"}]},
            "console": {"listen": f"127.0.0.1:{port}", "password": "contract-test"},
            "storage": {"data_dir": tmp},
        }
        cfg, _ = p.normalize_plugin_config(raw)
        p.set_plugin_config(cfg)
        p._set_context(_Ctx())

        async def run() -> None:
            await p.on_load()
            loop = asyncio.get_running_loop()
            body = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/api/me", timeout=5).read()
            )
            self.assertIn(b'"role"', body)
            self.assertTrue((Path(tmp) / "maiwork.db").exists())
            off = dict(cfg)
            off["plugin"] = dict(cfg["plugin"], enabled=False)
            await p.on_config_update("self", off, "x")
            with self.assertRaises(Exception):
                await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/api/me", timeout=2).read()
                )
            await p.on_unload()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
