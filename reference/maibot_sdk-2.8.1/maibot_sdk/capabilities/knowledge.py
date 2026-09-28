"""知识库能力代理

提供 LPMM 知识库检索功能。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class KnowledgeCapability:
    """知识库检索能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def search(self, query: str, limit: int = 5) -> Any:
        """搜索知识库

        Args:
            query: 搜索查询文本
            limit: 返回结果数量上限
        """
        return await self._ctx.call_capability(
            "knowledge.search",
            query=query,
            limit=limit,
        )
