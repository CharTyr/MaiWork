"""本机统计能力代理

提供 Host 侧本机统计数据读取能力，底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class LocalStatisticsCapability:
    """本机 MaiBot 统计数据读取能力。"""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def models(self, *, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
        """获取本机模型维度汇总统计。"""
        result = await self._ctx.call_capability("statistics.local.models", days=days, limit=limit)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            models = result.get("models")
            if isinstance(models, list):
                return models
        return []

    async def model_trend(
        self,
        *,
        days: int = 7,
        bucket: str = "day",
        top_models: int = 10,
        metric: str = "token",
        module_name: str = "",
    ) -> dict[str, Any]:
        """获取本机模型调用趋势。"""
        result = await self._ctx.call_capability(
            "statistics.local.model_trend",
            days=days,
            bucket=bucket,
            top_models=top_models,
            metric=metric,
            module_name=module_name,
        )
        if isinstance(result, dict):
            return result
        return {}

    async def token_trend(
        self,
        *,
        days: int = 7,
        bucket: str = "day",
        group_by: str = "",
        top_items: int = 10,
    ) -> dict[str, Any]:
        """获取本机 token 使用趋势。"""
        result = await self._ctx.call_capability(
            "statistics.local.token_trend",
            days=days,
            bucket=bucket,
            group_by=group_by,
            top_items=top_items,
        )
        if isinstance(result, dict):
            return result
        return {}

    async def token_distribution(
        self,
        *,
        days: int = 7,
        group_by: str = "model",
        top_items: int = 10,
    ) -> dict[str, Any]:
        """获取本机 token 使用分布。"""
        result = await self._ctx.call_capability(
            "statistics.local.token_distribution",
            days=days,
            group_by=group_by,
            top_items=top_items,
        )
        if isinstance(result, dict):
            return result
        return {}

    async def message_trend(self, *, days: int = 7, bucket: str = "day", top_chats: int = 10) -> dict[str, Any]:
        """获取本机聊天流消息量趋势。"""
        result = await self._ctx.call_capability(
            "statistics.local.message_trend",
            days=days,
            bucket=bucket,
            top_chats=top_chats,
        )
        if isinstance(result, dict):
            return result
        return {}

    async def tool_trend(self, *, days: int = 7, bucket: str = "day", top_tools: int = 10) -> dict[str, Any]:
        """获取本机工具调用趋势。"""
        result = await self._ctx.call_capability(
            "statistics.local.tool_trend",
            days=days,
            bucket=bucket,
            top_tools=top_tools,
        )
        if isinstance(result, dict):
            return result
        return {}

    async def online_time_trend(self, *, days: int = 7, bucket: str = "day") -> dict[str, Any]:
        """获取本机在线时长趋势。"""
        result = await self._ctx.call_capability(
            "statistics.local.online_time_trend",
            days=days,
            bucket=bucket,
        )
        if isinstance(result, dict):
            return result
        return {}


class StatisticsCapability:
    """统计能力入口。"""

    def __init__(self, ctx: PluginContext) -> None:
        self.local = LocalStatisticsCapability(ctx)

