"""消息发送能力代理

对应旧系统的 send_api，所有方法底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class SendResult(TypedDict):
    """包含平台最终消息 ID 的发送结果。"""

    sent: bool
    message_id: str | None


class SendCapability:
    """消息发送能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def text(
        self,
        text: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送文本消息

        Args:
            text: 消息文本
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.text",
                text=text,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )

    async def emoji(
        self,
        emoji_data: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送表情

        Args:
            emoji_data: 表情数据（base64）
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.emoji",
                emoji_base64=emoji_data,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )

    async def image(
        self,
        image_data: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送图片

        Args:
            image_data: 图片数据（base64）
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.image",
                image_base64=image_data,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送转发消息

        Args:
            messages: 消息列表
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.forward",
                messages=messages,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )

    async def hybrid(
        self,
        segments: list[dict[str, Any]],
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送混合消息（合并转发中的图文混合）

        Args:
            segments: 消息段列表，每段为 {"type": "text"|"image", "content": "..."}
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.hybrid",
                segments=segments,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )

    async def command(
        self,
        command: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送命令消息

        Args:
            command: 命令内容
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.command",
                command=command,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )

    async def custom(
        self,
        custom_type: str,
        data: Any,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> bool | SendResult:
        """发送自定义类型消息

        Args:
            custom_type: 自定义消息类型标识
            data: 消息数据
            stream_id: 目标消息流 ID
        """
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.custom",
                message_type=custom_type,
                content=data,
                custom_type=custom_type,
                data=data,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
        )
