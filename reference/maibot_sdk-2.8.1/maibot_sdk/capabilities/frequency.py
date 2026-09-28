"""发言频率能力代理

对应旧系统的 frequency_api，所有方法底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class FrequencyCapability:
    """发言频率控制能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_current_talk_value(self, chat_id: str) -> Any:
        """获取当前聊天的 talk value"""
        return await self._ctx.call_capability(
            "frequency.get_current_talk_value",
            chat_id=chat_id,
        )

    async def set_adjust(self, chat_id: str, value: float) -> Any:
        """设置频率调整值"""
        return await self._ctx.call_capability(
            "frequency.set_adjust",
            chat_id=chat_id,
            value=value,
        )

    async def get_adjust(self, chat_id: str) -> Any:
        """获取频率调整值"""
        return await self._ctx.call_capability(
            "frequency.get_adjust",
            chat_id=chat_id,
        )
