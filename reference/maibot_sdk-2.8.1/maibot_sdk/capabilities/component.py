"""组件管理能力代理

对应旧系统的 component_manage_api + plugin_manage_api。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from maibot_sdk.types import normalize_component_type_name

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class ComponentCapability:
    """插件和组件管理能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_all_plugins(self) -> Any:
        """获取所有已注册插件信息"""
        return await self._ctx.call_capability("component.get_all_plugins")

    async def get_plugin_info(self, plugin_name: str) -> Any:
        """获取指定插件信息"""
        return await self._ctx.call_capability(
            "component.get_plugin_info",
            plugin_name=plugin_name,
        )

    async def get_plugin_config_schema(self, plugin_name: str) -> Any:
        """获取指定插件的配置 Schema。"""

        return await self._ctx.call_capability(
            "component.get_plugin_config_schema",
            plugin_name=plugin_name,
        )

    async def list_loaded_plugins(self) -> Any:
        """列出已加载的插件"""
        return await self._ctx.call_capability("component.list_loaded_plugins")

    async def list_registered_plugins(self) -> Any:
        """列出已注册的插件"""
        return await self._ctx.call_capability("component.list_registered_plugins")

    async def enable_component(
        self,
        name: str,
        component_type: str,
        scope: str = "global",
        stream_id: str = "",
        version: str = "",
    ) -> Any:
        """启用组件"""
        return await self._ctx.call_capability(
            "component.enable",
            name=name,
            component_type=normalize_component_type_name(component_type),
            scope=scope,
            stream_id=stream_id,
            version=version,
        )

    async def disable_component(
        self,
        name: str,
        component_type: str,
        scope: str = "global",
        stream_id: str = "",
        version: str = "",
    ) -> Any:
        """禁用组件"""
        return await self._ctx.call_capability(
            "component.disable",
            name=name,
            component_type=normalize_component_type_name(component_type),
            scope=scope,
            stream_id=stream_id,
            version=version,
        )

    async def load_plugin(self, plugin_name: str) -> Any:
        """加载指定插件"""
        return await self._ctx.call_capability(
            "component.load_plugin",
            plugin_name=plugin_name,
        )

    async def unload_plugin(self, plugin_name: str) -> Any:
        """卸载指定插件"""
        return await self._ctx.call_capability(
            "component.unload_plugin",
            plugin_name=plugin_name,
        )

    async def reload_plugin(self, plugin_name: str) -> Any:
        """重新加载指定插件"""
        return await self._ctx.call_capability(
            "component.reload_plugin",
            plugin_name=plugin_name,
        )
