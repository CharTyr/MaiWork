"""聊天流能力代理

提供聊天流发现与查询功能。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class ChatCapability:
    """聊天流管理能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_all_streams(self, platform: str = "qq") -> Any:
        """获取所有活跃的聊天流"""
        return await self._ctx.call_capability("chat.get_all_streams", platform=platform)

    async def get_group_streams(self, platform: str = "qq") -> Any:
        """获取所有群聊流"""
        return await self._ctx.call_capability("chat.get_group_streams", platform=platform)

    async def get_private_streams(self, platform: str = "qq") -> Any:
        """获取所有私聊流"""
        return await self._ctx.call_capability("chat.get_private_streams", platform=platform)

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> Any:
        """根据群组 ID 查找聊天流

        Args:
            group_id: 群组 ID
        """
        return await self._ctx.call_capability(
            "chat.get_stream_by_group_id",
            group_id=group_id,
            platform=platform,
        )

    async def get_stream_by_user_id(self, user_id: str, platform: str = "qq") -> Any:
        """根据用户 ID 查找聊天流

        Args:
            user_id: 用户 ID
        """
        return await self._ctx.call_capability(
            "chat.get_stream_by_user_id",
            user_id=user_id,
            platform=platform,
        )

    async def open_session(
        self,
        platform: str = "qq",
        chat_type: str = "private",
        *,
        user_id: str = "",
        group_id: str = "",
        account_id: str = "",
        scope: str = "",
        **kwargs: Any,
    ) -> Any:
        """按平台目标打开或创建一个聊天流。

        Args:
            platform: 平台标识，例如 ``qq``。
            chat_type: 聊天类型，支持 ``private`` 或 ``group``。
            user_id: 私聊目标用户 ID。
            group_id: 群聊目标群 ID。
            account_id: 可选的平台账号 ID，用于多账号路由。
            scope: 可选的路由范围。
            **kwargs: Host 支持的额外参数。
        """

        return await self._ctx.call_capability(
            "chat.open_session",
            platform=platform,
            chat_type=chat_type,
            user_id=user_id,
            group_id=group_id,
            account_id=account_id,
            scope=scope,
            **kwargs,
        )
