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


def _local_only_socket_guard(blocked: list) -> object:
    """只放行 localhost：测试进程里任何往外网的连接直接拒掉（返回还原函数）。

    契约测试要在真实宿主 venv 里跑**真实**生命周期（后台循环也会起来），不能让它在服务器上
    替我们往外网发请求。罩子只拦「主动往外连」，不影响本地网页端口的 bind / accept，
    也不改插件任何状态、更不会把插件变成 disabled——所以下面仍然断言真的建了库、
    开了端口、能访问、关掉后端口释放；生命周期是实跑的不是跳过的。
    """

    def _allowed(address: object) -> bool:
        if not isinstance(address, tuple) or not address:
            return True  # AF_UNIX / 其它地址族
        return str(address[0]) in {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}

    def _wrap(orig: object, *, refuse: bool) -> object:
        def call(sock, address):  # noqa: ANN001
            if not _allowed(address):
                blocked.append(str(address))
                if refuse:
                    return 111  # ECONNREFUSED：不建立真连接
                raise OSError(f"契约测试只许 localhost，拒绝外网连接：{address}")
            return orig(sock, address)  # type: ignore[operator]

        return call

    import socket as _socket

    orig_connect = _socket.socket.connect
    orig_connect_ex = _socket.socket.connect_ex
    _socket.socket.connect = _wrap(orig_connect, refuse=False)  # type: ignore[assignment]
    _socket.socket.connect_ex = _wrap(orig_connect_ex, refuse=True)  # type: ignore[assignment]

    def restore() -> None:
        _socket.socket.connect = orig_connect  # type: ignore[assignment]
        _socket.socket.connect_ex = orig_connect_ex  # type: ignore[assignment]

    return restore


class HostContractTest(unittest.TestCase):
    def test_manifest_valid(self) -> None:
        v = ManifestValidator()
        m = v.load_from_plugin_path(PLUGIN_DIR)
        self.assertIsNotNone(m)
        self.assertEqual(v.errors, [])
        self.assertEqual(v.warnings, [])

    def test_manifest_sdk_range_covers_running_sdk(self) -> None:
        """声明的 SDK 区间必须真的覆盖当前宿主 venv 的 SDK。

        宿主对 SDK 超范围是硬错误（`SDK 版本不兼容` 进 errors → 拒载），没有 Host 那种
        「只高一个修订号就按兼容模式加载」的回落，所以这一条把「上限是实测端点」变成
        能在真实宿主 venv 里复现的断言：跑测试用的这个 SDK 版本，就是被验证过的端点。
        """
        import json

        import maibot_sdk
        from packaging.version import Version

        manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        sdk = manifest["sdk"]
        running = str(maibot_sdk.__version__)
        self.assertLessEqual(
            Version(sdk["min_version"]),
            Version(running),
            f"当前宿主 SDK {running} 低于声明下限 {sdk['min_version']}",
        )
        self.assertLessEqual(
            Version(running),
            Version(sdk["max_version"]),
            f"当前宿主 SDK {running} 高于声明上限 {sdk['max_version']}（宿主会拒载）",
        )

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

    def test_session_command_in_real_host_registry(self) -> None:
        """会话原生命令放进**宿主真实的** ComponentRegistry：只认 @ 本 bot 的显示名、
        宿主侧超时取 5000ms、全局关闭后完全不匹配（关着时不打 is_command 的前提）。

        Runner 先 `_apply_plugin_config`（runner_main.py:1614）再 `get_components()`（:1405），
        这里按同样顺序：先 set_plugin_config，再收集组件。
        """
        import tempfile

        from src.plugin_runtime.host.component_registry import ComponentRegistry

        p = create_plugin()
        cfg, _ = p.normalize_plugin_config({
            "plugin": {"enabled": False, "config_version": "0.0.1"},
            "session": {"bot_names": ["東雪蓮", "🪷🪷"]},
            "storage": {"data_dir": tempfile.mkdtemp(prefix="maiwork-ci-cmd-")},
        })
        p.set_plugin_config(cfg)
        comps = p.get_components()
        # 原始声明的类型键是 "type"；Runner 按 runner_main.py:1405-1430 转成
        # ComponentDeclaration(component_type=type, chat_scope, allowed_session, metadata) 再交给注册表
        cmds = [c for c in comps if str(c.get("type") or "").lower() == "command"]
        self.assertEqual([c.get("name") for c in cmds], ["maiwork_session"], cmds)
        decls = [
            {
                "name": str(c.get("name") or ""),
                "component_type": str(c.get("type") or ""),
                "chat_scope": str(c.get("chat_scope", "all") or "all"),
                "allowed_session": list(c.get("allowed_session") or []),
                "metadata": dict(c.get("metadata") or {}),
            }
            for c in comps
        ]

        reg = ComponentRegistry()
        self.assertEqual(reg.register_plugin_components(PLUGIN_ID, decls), len(decls))
        for text in ("@東雪蓮 往下滚", "@🪷🪷 做到哪了", "[reply]别人说的 @東雪蓮  按这个做", "@東雪蓮"):
            hit = reg.find_command_by_text(text)
            self.assertIsNotNone(hit, f"应命中：{text!r}")
            entry, _groups = hit
            self.assertEqual(entry.name, "maiwork_session")
            self.assertEqual(entry.timeout_ms, 5000)
        for text in ("@别人 继续", "今天天气不错", "/mw 状态", "@東雪蓮 /pic 猫", "东雪莲 往下滚", "文字 @東雪蓮 继续"):
            self.assertIsNone(reg.find_command_by_text(text), f"不应命中：{text!r}")

        full = f"{PLUGIN_ID}.maiwork_session"
        self.assertTrue(reg.set_component_enabled(full, False))
        self.assertIsNone(reg.find_command_by_text("@東雪蓮 往下滚"), "全局关闭后仍在匹配")
        self.assertTrue(reg.set_component_enabled(full, True))
        self.assertIsNotNone(reg.find_command_by_text("@東雪蓮 往下滚"))

    def test_session_command_capabilities_declared(self) -> None:
        """manifest 必须声明 component.enable / component.disable，否则开关会 E_CAPABILITY_DENIED。"""
        import json

        manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        for cap in ("component.enable", "component.disable"):
            self.assertIn(cap, manifest["capabilities"])

    def test_old_config_merges(self) -> None:
        """线上旧版 config.toml（0.0.1，只有 [plugin]）能被规范化成新结构，且仍是关闭的。"""
        p = create_plugin()
        cfg, _changed = p.normalize_plugin_config({"plugin": {"enabled": False, "config_version": "0.0.1"}})
        self.assertFalse(cfg["plugin"]["enabled"])
        self.assertIn("groups", cfg)

    def test_enabled_start_stop_in_host_venv(self) -> None:
        """在宿主 venv 里真的开一次：建库、开网页端口、能访问、关掉后端口释放。

        隔离边界（2026-10 加）：本机执行能力按配置里的 run_as 判定；run_as 用户**不存在**时
        宿主会强制把工作区根改到 /var/lib/private/maiwork/workspaces，那契约测试就跑出了
        自己的临时目录。这里把 run_as 指到本机**真实存在**的用户（优先 nobody），判定结果就是
        fixed，工作区根按配置落在 tmp 里（下面有断言）。这不是替身：LocalEnv、生命周期、
        manifest 校验全部用真实宿主代码跑。
        另加只许 localhost 的 socket 罩：后台循环若想往外网发请求，在测试进程里直接拒绝——
        既保证真实生命周期跑起来了（仍然建库 / 开端口 / 关端口），又不替线上发任何外网请求。
        """
        import os
        import pwd
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
        ws = Path(tmp) / "ws"
        run_as = "nobody"
        try:
            pwd.getpwnam(run_as)
        except KeyError:  # 没有 nobody 就退回当前进程用户（一定存在），判定仍是 fixed
            run_as = pwd.getpwuid(os.geteuid()).pw_name
        p = create_plugin()
        raw = {
            "plugin": {"enabled": True, "config_version": "0.0.1"},
            "groups": {"serve": [{"group": "qq:900000001"}]},
            "console": {"listen": f"127.0.0.1:{port}", "password": "contract-test"},
            "storage": {"data_dir": tmp},
            # 工作区根锁进测试临时目录；run_as 是真实存在的用户 → 判定 fixed，不会强制改写
            "environments": {"workspace_root": str(ws), "run_as": run_as},
        }
        cfg, _ = p.normalize_plugin_config(raw)
        p.set_plugin_config(cfg)
        p._set_context(_Ctx())

        async def run() -> None:
            await p.on_load()
            self.assertIsNotNone(p._app, "插件没真启动（不许变成 disabled 的假绿）")
            eff_root = Path(p._app.get_settings().workspace_root)
            self.assertEqual(eff_root, ws, f"工作区根被改写：{eff_root}")
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

        blocked: list = []
        restore = _local_only_socket_guard(blocked)
        try:
            asyncio.run(run())
        finally:
            restore()
        self.assertEqual(blocked, [], f"契约测试期间出现外网连接（只许 localhost）：{blocked[:3]}")



if __name__ == "__main__":
    unittest.main()
