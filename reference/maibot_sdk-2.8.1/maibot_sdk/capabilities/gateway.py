"""消息网关运行时能力代理。

为声明了 ``@MessageGateway`` 的插件提供与 Host 路由层交互的统一接口。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class GatewayCapability:
    """消息网关能力代理。"""

    def __init__(self, ctx: PluginContext) -> None:
        """初始化消息网关能力代理。

        Args:
            ctx: 当前插件对应的上下文对象。
        """

        self._ctx = ctx

    async def route_message(
        self,
        gateway_name: str,
        message: dict[str, Any],
        *,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        """将外部平台消息通过指定接收网关注入 Host。

        Args:
            gateway_name: 当前接收网关组件名称。
            message: 网关插件转换后的标准消息字典。
            route_metadata: 可选的路由辅助元数据。
            external_message_id: 可选的外部平台消息 ID。
            dedupe_key: 可选的显式去重键。

        Returns:
            bool: Host 是否接受了本次消息路由。
        """

        result = await self._ctx.call_host_method(
            "host.route_message",
            payload={
                "gateway_name": gateway_name,
                "message": message,
                "route_metadata": route_metadata or {},
                "external_message_id": external_message_id,
                "dedupe_key": dedupe_key,
            },
        )
        if not isinstance(result, dict):
            return False
        return bool(result.get("accepted", False))

    async def update_state(
        self,
        gateway_name: str,
        *,
        ready: bool,
        platform: str = "",
        account_id: str = "",
        scope: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """向 Host 上报指定消息网关的运行时状态。

        Args:
            gateway_name: 目标消息网关组件名称。
            ready: 当前链路是否就绪。
            platform: 当前链路负责的平台名称。
            account_id: 当前链路对应的账号 ID 或 ``self_id``。
            scope: 当前链路对应的作用域标识。
            metadata: 可选的运行时元数据。

        Returns:
            bool: Host 是否接受了本次状态更新。
        """

        result = await self._ctx.call_host_method(
            "host.update_message_gateway_state",
            payload={
                "gateway_name": gateway_name,
                "ready": ready,
                "platform": platform,
                "account_id": account_id,
                "scope": scope,
                "metadata": metadata or {},
            },
        )
        if not isinstance(result, dict):
            return False
        return bool(result.get("accepted", False))

    async def receive_external_message(
        self,
        message: dict[str, Any],
        *,
        gateway_name: str,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        """兼容旧命名，等价于 ``route_message()``。

        Args:
            message: 标准消息字典。
            gateway_name: 当前接收网关组件名称。
            route_metadata: 可选的路由辅助元数据。
            external_message_id: 可选的外部平台消息 ID。
            dedupe_key: 可选的显式去重键。

        Returns:
            bool: Host 是否接受了本次消息路由。
        """

        return await self.route_message(
            gateway_name=gateway_name,
            message=message,
            route_metadata=route_metadata,
            external_message_id=external_message_id,
            dedupe_key=dedupe_key,
        )

    async def update_runtime_state(
        self,
        *,
        gateway_name: str,
        connected: bool,
        platform: str = "",
        account_id: str = "",
        scope: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """兼容旧命名，等价于 ``update_state()``。

        Args:
            gateway_name: 目标消息网关组件名称。
            connected: 当前链路是否已连接并可用。
            platform: 当前链路负责的平台名称。
            account_id: 当前链路对应的账号 ID。
            scope: 当前链路对应的作用域。
            metadata: 可选的运行时元数据。

        Returns:
            bool: Host 是否接受了本次状态更新。
        """

        return await self.update_state(
            gateway_name=gateway_name,
            ready=connected,
            platform=platform,
            account_id=account_id,
            scope=scope,
            metadata=metadata,
        )
