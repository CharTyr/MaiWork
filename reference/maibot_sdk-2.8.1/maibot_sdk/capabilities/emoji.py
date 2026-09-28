"""表情包能力代理

对应旧系统的 emoji_api，所有方法底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class EmojiCapability:
    """表情包管理能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_random(self, count: int = 5) -> Any:
        """随机获取表情包

        Args:
            count: 获取数量
        """
        return await self._ctx.call_capability(
            "emoji.get_random",
            count=count,
        )

    async def get_by_description(self, description: str, limit: int = 5) -> Any:
        """根据描述搜索表情包"""
        return await self._ctx.call_capability(
            "emoji.get_by_description",
            description=description,
            limit=limit,
        )

    async def get_count(self) -> Any:
        """获取表情包总数"""
        return await self._ctx.call_capability("emoji.get_count")

    async def get_info(self) -> Any:
        """获取表情包统计信息"""
        return await self._ctx.call_capability("emoji.get_info")

    async def get_emotions(self) -> Any:
        """获取所有情感标签"""
        return await self._ctx.call_capability("emoji.get_emotions")

    async def get_all(self) -> Any:
        """获取所有表情包"""
        return await self._ctx.call_capability("emoji.get_all")

    async def register_emoji(self, emoji_base64: str) -> Any:
        """注册表情包

        Args:
            emoji_base64: 表情图片 base64 数据
        """
        return await self._ctx.call_capability(
            "emoji.register",
            emoji_base64=emoji_base64,
        )

    async def delete_emoji(self, emoji_hash: str, keep_desc: bool | None = None) -> Any:
        """删除表情包

        Args:
            emoji_hash: 表情包 SHA256 哈希。
            keep_desc: 是否保留描述缓存。为 ``True`` 时删除文件但保留数据库描述记录；
                为 ``False`` 时同时删除数据库记录；为 ``None`` 时由 Host 根据是否有描述自动决定。
        """
        payload: dict[str, Any] = {"emoji_hash": emoji_hash}
        if keep_desc is not None:
            payload["keep_desc"] = keep_desc
        return await self._ctx.call_capability("emoji.delete", **payload)
