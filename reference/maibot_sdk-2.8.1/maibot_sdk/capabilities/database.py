"""数据库能力代理

对应旧系统的 database_api，所有方法底层转发为 cap.call RPC。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class DatabaseCapability:
    """数据库操作能力"""

    def __init__(self, ctx: PluginContext):
        self._ctx = ctx

    async def query(
        self,
        model_name: str,
        query_type: str = "get",
        data: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
        order_by: list[str] | None = None,
        limit: int | None = None,
        single_result: bool = False,
    ) -> Any:
        """查询数据

        Args:
            model_name: 模型名，对应 Host 侧 database_model 中的类名
            query_type: 查询类型，可选 get/create/update/delete/count
            data: create/update 时写入的数据
            filters: 过滤条件
            order_by: 排序字段
            limit: 返回条数限制
            single_result: 是否只取一条结果
        """
        return await self._ctx.call_capability(
            "database.query",
            model_name=model_name,
            query_type=query_type,
            data=data,
            filters=filters or {},
            order_by=order_by or [],
            limit=limit,
            single_result=single_result,
        )

    async def save(
        self,
        model_name: str,
        data: dict[str, Any],
        key_field: str = "id",
        key_value: Any = None,
    ) -> Any:
        """保存数据（插入或更新）

        Args:
            model_name: 模型名，对应 Host 侧 database_model 中的类名
            data: 要保存的数据
            key_field: 用于查找已有记录的字段名
            key_value: 用于查找已有记录的字段值（为 None 时表示插入）
        """
        return await self._ctx.call_capability(
            "database.save",
            model_name=model_name,
            data=data,
            key_field=key_field,
            key_value=key_value,
        )

    async def get(
        self,
        model_name: str,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        order_by: str | list[str] | None = None,
        single_result: bool = False,
    ) -> Any:
        """按条件获取数据

        Args:
            model_name: 模型名，对应 Host 侧 database_model 中的类名
            filters: 过滤条件
            limit: 返回条数限制
            order_by: 排序字段，支持字符串或字符串列表
            single_result: 是否只取一条结果
        """
        return await self._ctx.call_capability(
            "database.get",
            model_name=model_name,
            filters=filters or {},
            limit=limit,
            order_by=order_by,
            single_result=single_result,
        )

    async def delete(
        self,
        model_name: str,
        filters: dict[str, Any],
    ) -> Any:
        """删除数据

        Args:
            model_name: 模型名，对应 Host 侧 database_model 中的类名
            filters: 过滤条件
        """
        return await self._ctx.call_capability(
            "database.delete",
            model_name=model_name,
            filters=filters,
        )

    async def count(
        self,
        model_name: str,
        filters: dict[str, Any] | None = None,
    ) -> int:
        """计数

        Args:
            model_name: 模型名，对应 Host 侧 database_model 中的类名
            filters: 过滤条件
        """
        result = await self._ctx.call_capability(
            "database.count",
            model_name=model_name,
            filters=filters or {},
        )
        if isinstance(result, dict):
            result = result.get("count")
        return int(result) if result is not None else 0
