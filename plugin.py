"""MaiWork 插件入口：注册收消息钩子，生命周期里创建 / 关闭 MaiWorkApp，其余全部转发。

- on_load：config.plugin.enabled → 建 MaiWorkApp 并 start；任何异常记日志不抛，
  保证插件照常加载（MaiBot 不受影响）。
- on_unload：app.stop()。
- on_config_update：scope=="self" 时交给 app.update_config；没 app 且新配置 enabled → 新建并 start。
- 收消息钩子永远返回 {"action": "continue"}（外层还有一道 try/except 兜底）。
"""

from __future__ import annotations

import logging
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode

from .maiwork.config import (
    CONFIG_VERSION,
    MaiWorkConfig,
    PluginSectionConfig,
    Settings,
    load_settings,
)

logger = logging.getLogger("maiwork")

PLUGIN_ID = "chartyr.maiwork"
PLUGIN_VERSION = "0.11.2"

__all__ = [
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "CONFIG_VERSION",
    "PluginSectionConfig",
    "MaiWorkConfig",
    "Settings",
    "load_settings",
    "MaiWorkPlugin",
    "create_plugin",
]

_PLUGIN_DIR = Path(__file__).resolve().parent


class MaiWorkPlugin(MaiBotPlugin):
    config_model = MaiWorkConfig

    # 测试钩子：显式指定数据目录（线上留空，走 config 默认/显式配置）
    _data_dir_override: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._app = None
        self._registered_session_pattern = r"(?!)"

    def get_components(self):
        """注册前只读自己的候选名；本次加载中正则不再改变。"""
        from .maiwork.session_gate import build_pattern, bounded_names

        settings, _ = load_settings(self._with_data_dir_override(self._self_config()))
        names = list(settings.session.bot_names)
        path = settings.data_dir / "maiwork.db"
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
            try:
                row = conn.execute("SELECT value FROM kv WHERE key='session.bot_names'").fetchone()
                if row:
                    names.extend(bounded_names(json.loads(row[0])))
            finally:
                conn.close()
        except (sqlite3.Error, ValueError, TypeError):
            logger.debug("加载时没有可用的机器人显示名记录，先用配置候选")
        self._registered_session_pattern = build_pattern(names, [])
        components = super().get_components()
        for component in components:
            if component.get("name") == "maiwork_session":
                component["metadata"]["command_pattern"] = self._registered_session_pattern
        return components

    @Command("maiwork_session", description="工作会话 @ 入口", pattern=r"(?!)",
        timeout_ms=5000, intercept_message_level=0)
    async def maiwork_session(self, **kwargs):
        """失败明确放行，不向宿主返回文本，不在回调里发送消息。"""
        try:
            gate = getattr(self._app, "session_gate", None)
            if gate is not None:
                return await gate.handle(kwargs)
        except BaseException:
            logger.warning("会话原生命令未完成，消息继续", exc_info=True)
        return True, None, False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _self_config(self) -> Dict[str, Any]:
        """宿主给插件的配置（优先 get_plugin_config_data，退回强类型 config）。"""
        try:
            data = self.get_plugin_config_data()
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
        try:
            cfg = self.config
            if hasattr(cfg, "model_dump"):
                return cfg.model_dump(mode="python")
        except Exception:
            pass
        return {}

    async def _start_app(self, config_data: Dict[str, Any]) -> None:
        from .maiwork.app import MaiWorkApp

        # 没有宿主 ctx 就不启动（只在极端情况出现：测试里直接建插件实例）。
        # 这里**不用替身**：以前那个动态属性（__getattr__）替身是为了绕自己的源码扫描
        # （tests/test_host_only.py 不许 host.py 以外出现宿主能力调用方法名），
        # 审核不接受；没有 ctx 就什么都不干，插件本体照常加载。
        ctx = self._ctx
        if ctx is None:
            logger.warning("宿主上下文还没注入，这次不启动 MaiWork（插件本体照常加载）")
            return
        raw = self._with_data_dir_override(config_data or {})
        app = MaiWorkApp(ctx, raw, plugin_dir=_PLUGIN_DIR)
        app.registered_session_pattern = self._registered_session_pattern
        try:
            await app.start()
        except Exception:
            logger.exception("MaiWork 启动出错，插件本体照常加载")
            return
        self._app = app

    def _with_data_dir_override(self, config_data: Dict[str, Any]) -> Dict[str, Any]:
        """测试钩子：显式指定数据目录时覆盖 storage.data_dir（浅拷贝，不动入参）。"""
        raw = dict(config_data)
        override = str(getattr(self, "_data_dir_override", "") or "")
        if override:
            sect = dict(raw.get("storage") or {})
            sect["data_dir"] = override
            raw["storage"] = sect
        return raw

    async def _stop_app(self) -> None:
        app, self._app = self._app, None
        if app is not None:
            try:
                await app.stop()
            except Exception:
                logger.exception("MaiWork 关闭出错")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        try:
            if self._ctx is not None:
                from .maiwork.host import Host
                await Host(self._ctx).set_session_command_enabled(False)
        except Exception:
            logger.warning("加载时关闭会话原生命令失败，应用启动后重试", exc_info=True)
        try:
            cfg = self._self_config()
            if cfg.get("plugin", {}).get("enabled"):
                await self._start_app(cfg)
                logger.info("MaiWork %s 已加载", PLUGIN_VERSION)
            else:
                logger.info("MaiWork %s 已加载（未启用，什么都不干）", PLUGIN_VERSION)
        except Exception:
            logger.exception("MaiWork on_load 出错，插件照常加载")

    async def on_unload(self) -> None:
        try:
            await self._stop_app()
        except Exception:
            logger.exception("MaiWork on_unload 出错")
        logger.info("MaiWork 已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        try:
            if scope != "self":
                return
            data = self._with_data_dir_override(config_data if isinstance(config_data, dict) else {})
            if self._app is not None:
                await self._app.update_config(data)
            elif data.get("plugin", {}).get("enabled"):
                await self._start_app(data)
        except Exception:
            logger.exception("MaiWork 配置热更新出错")

    # ------------------------------------------------------------------
    # 收消息钩子：只转给 app，永不中止消息
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.after_process",
        name="maiwork_intake",
        description="MaiWork：服务群新消息信号",
        mode=HookMode.BLOCKING,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def maiwork_intake(self, **kwargs: Any) -> Dict[str, Any]:
        # 返回值只有 {"action": "continue"}：**不回传整条消息载荷**（宿主推过来的
        # kwargs 可能带图片原始字节，单帧上限 16MiB；回程帧越轻越好）。
        # 钩子内部也不等模型 / Jev / 网络：@ 的判词在后台任务里做，本地附加工作有
        # 800ms 预算（intake._HOOK_BUDGET_MS）。宿主连续 3 次超时会熔断 60 秒。
        try:
            if self._app is None:
                return {"action": "continue"}
            return await self._app.on_message(kwargs)
        except Exception:
            logger.exception("MaiWork 收消息钩子出错，已吞掉")
            return {"action": "continue"}

    # ------------------------------------------------------------------
    # planner 备忘钩子：把「可提起清单」塞进 MaiBot 的 planner 请求
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.before_request",
        name="maiwork_mentions",
        description="MaiWork：往 planner 的 system 里追加可提起备忘",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def maiwork_mentions(self, **kwargs: Any) -> Dict[str, Any]:
        # 返回约定出处：reference/maibot_sdk-2.8.1/maibot_sdk/components.py 的
        # HookHandler docstring——BLOCKING 钩子改写载荷用
        # {"action": "continue", "modified_kwargs": <整体替换的 kwargs>}；
        # 不改就是 {"action": "continue"}（manifest 不写 modifies）。
        # 宿主侧解析见 docs/06：allow_abort=False，败了只警告并忽略。
        # 有备忘要注入时**必须**回传整包（BLOCKING 改写的约定写法，大小与入参同阶、
        # 插件侧不放大）；没货 / 超预算就只回 {"action": "continue"}，不回传整包。
        # 本地附加工作（查库拼备忘 + 深拷贝）有 500ms 预算，在
        # app.on_planner_before_request → Mentions.inject 里按 deadline 自检。
        try:
            if self._app is None:
                return {"action": "continue"}
            return self._app.on_planner_before_request(kwargs)
        except Exception:
            logger.exception("MaiWork planner 备忘钩子出错，已吞掉")
            return {"action": "continue"}


def create_plugin() -> MaiWorkPlugin:
    return MaiWorkPlugin()
