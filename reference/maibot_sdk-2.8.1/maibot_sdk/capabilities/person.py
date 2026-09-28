"""人物/关系能力代理

提供对人物数据库的查询功能。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class PersonCapability:
    """人物信息查询能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def get_id(self, platform: str, user_id: str) -> Any:
        """根据平台和用户 ID 获取内部人物 ID

        Args:
            platform: 平台标识
            user_id: 平台用户 ID
        """
        return await self._ctx.call_capability(
            "person.get_id",
            platform=platform,
            user_id=user_id,
        )

    async def get_value(self, person_id: str, field_name: str) -> Any:
        """获取人物的指定字段值

        Args:
            person_id: 内部人物 ID
            field_name: 字段名称（如 name, state 等）
        """
        return await self._ctx.call_capability(
            "person.get_value",
            person_id=person_id,
            field_name=field_name,
        )

    async def get_id_by_name(self, person_name: str) -> Any:
        """根据名称查找人物 ID

        Args:
            person_name: 人物名称
        """
        return await self._ctx.call_capability(
            "person.get_id_by_name",
            person_name=person_name,
        )
