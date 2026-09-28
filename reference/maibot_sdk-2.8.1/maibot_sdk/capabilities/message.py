"""消息能力代理

对应旧系统的 message_api，所有方法底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class MessageCapability:
    """消息查询能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_recent(self, chat_id: str, limit: int = 10) -> Any:
        """获取最近消息

        Args:
            chat_id: 聊天流 ID
            limit: 消息条数
        """
        return await self._ctx.call_capability(
            "message.get_recent",
            chat_id=chat_id,
            limit=limit,
        )

    async def get_by_id(
        self,
        message_id: str,
        *,
        chat_id: str = "",
        stream_id: str = "",
        include_binary_data: bool = False,
    ) -> Any:
        """根据消息 ID 获取单条消息。

        Args:
            message_id: 消息 ID。
            chat_id: 可选的聊天流 ID。
            stream_id: 可选的聊天流 ID，等价于 ``chat_id``。
            include_binary_data: 是否包含图片、语音等二进制数据。
        """

        return await self._ctx.call_capability(
            "message.get_by_id",
            message_id=message_id,
            chat_id=chat_id,
            stream_id=stream_id,
            include_binary_data=include_binary_data,
        )

    async def build_readable(self, messages: Any, **kwargs: Any) -> Any:
        """构建可读的消息字符串

        Args:
            messages: 消息列表（从 get_recent 等方法获取）
        """
        return await self._ctx.call_capability(
            "message.build_readable",
            messages=messages,
            **kwargs,
        )

    async def get_by_time(self, start_time: str, end_time: str, **kwargs: Any) -> Any:
        """按时间范围获取消息"""
        return await self._ctx.call_capability(
            "message.get_by_time",
            start_time=start_time,
            end_time=end_time,
            **kwargs,
        )

    async def get_by_time_in_chat(self, chat_id: str, start_time: str, end_time: str, **kwargs: Any) -> Any:
        """按时间范围获取指定聊天流的消息

        Args:
            chat_id: 聊天流 ID
            start_time: 起始时间
            end_time: 结束时间
        """
        return await self._ctx.call_capability(
            "message.get_by_time_in_chat",
            chat_id=chat_id,
            start_time=start_time,
            end_time=end_time,
            **kwargs,
        )

    async def count_new(self, chat_id: str, since: str) -> Any:
        """统计新消息数量"""
        return await self._ctx.call_capability(
            "message.count_new",
            chat_id=chat_id,
            since=since,
        )
