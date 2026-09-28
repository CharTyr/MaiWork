"""Maisaka 能力代理。

提供向 Maisaka 上下文追加消息、触发主动任务的能力。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class MaisakaContextCapability:
    """Maisaka 上下文能力。"""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def append(
        self,
        stream_id: str,
        segments: list[dict[str, Any]],
        *,
        visible_text: str = "",
        source_kind: str = "",
        message_id: str = "",
        **kwargs: Any,
    ) -> Any:
        """向指定聊天流的 Maisaka 上下文追加一条图文消息。

        Args:
            stream_id: 目标聊天流 ID。
            segments: 消息段列表。
            visible_text: 可选的可见文本摘要。
            source_kind: 可选的消息来源标识。
            message_id: 可选的消息 ID。
            **kwargs: Host 支持的额外参数。
        """

        return await self._ctx.call_capability(
            "maisaka.context.append",
            stream_id=stream_id,
            segments=segments,
            visible_text=visible_text,
            source_kind=source_kind,
            message_id=message_id,
            **kwargs,
        )


class MaisakaProactiveCapability:
    """Maisaka 主动任务能力。"""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def trigger(
        self,
        stream_id: str,
        intent: str,
        *,
        reason: str = "",
        priority: str = "",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """请求 Maisaka 基于指定聊天流主动处理一轮对话。

        Args:
            stream_id: 目标聊天流 ID，必须是 Host 中已存在的聊天流。
            intent: 主动任务意图。
            reason: 可选的触发原因。
            priority: 可选的优先级。
            metadata: 可选的任务元数据。
            **kwargs: Host 支持的额外参数。
        """

        return await self._ctx.call_capability(
            "maisaka.proactive.trigger",
            stream_id=stream_id,
            intent=intent,
            reason=reason,
            priority=priority,
            metadata=metadata or {},
            **kwargs,
        )


class MaisakaCapability:
    """Maisaka 能力入口。"""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self.context = MaisakaContextCapability(ctx)
        self.proactive = MaisakaProactiveCapability(ctx)

    async def append_context(
        self,
        stream_id: str,
        segments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """向 Maisaka 上下文追加消息。"""

        return await self.context.append(stream_id, segments, **kwargs)

    async def trigger_proactive(
        self,
        stream_id: str,
        intent: str,
        **kwargs: Any,
    ) -> Any:
        """触发 Maisaka 主动任务。"""

        return await self.proactive.trigger(stream_id, intent, **kwargs)
