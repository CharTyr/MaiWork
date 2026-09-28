"""配置能力代理

对应旧系统的 config_api，所有方法底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class ConfigCapability:
    """配置读写能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get(self, key: str, default: Any = None) -> Any:
        """获取配置值

        Args:
            key: 配置键（支持点分割，如 "section.key"）
            default: 默认值
        """
        result = await self._ctx.call_capability(
            "config.get",
            key=key,
            default=default,
        )
        return result if result is not None else default

    async def get_plugin(self, plugin_name: str | None = None) -> dict[str, Any]:
        """获取插件级配置

        Args:
            plugin_name: 插件名称，为 None 时获取当前插件配置
        """
        result = await self._ctx.call_capability(
            "config.get_plugin",
            plugin_name=plugin_name,
        )
        return result if isinstance(result, dict) else {}

    async def get_all(self) -> dict[str, Any]:
        """获取当前插件的全部配置"""
        result = await self._ctx.call_capability("config.get_all")
        return result if isinstance(result, dict) else {}
