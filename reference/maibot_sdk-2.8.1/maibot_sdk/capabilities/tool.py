"""工具内省能力代理

提供 LLM 工具定义查询功能。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class ToolCapability:
    """工具内省能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_definitions(self) -> Any:
        """获取所有可用的 LLM 工具定义

        Returns:
            工具定义列表，每个工具包含 name/description/parameters
        """
        return await self._ctx.call_capability("tool.get_definitions")
