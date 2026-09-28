"""插件 API 能力代理。

提供插件之间通过 Host 转发互调的统一入口。
"""

from __future__ import annotations

from builtins import list as builtins_list
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class APICapability:
    """插件 API 调用能力。"""

    def __init__(self, ctx: PluginContext) -> None:
        """初始化插件 API 能力代理。

        Args:
            ctx: 当前插件对应的上下文对象。
        """

        self._ctx = ctx

    async def call(self, api_name: str, *, version: str = "", **kwargs: Any) -> Any:
        """调用其他插件公开的 API。

        Args:
            api_name: API 名称，支持 ``plugin_id.api_name`` 或唯一短名。
            version: 可选的 API 版本。
            **kwargs: 传递给目标 API 的参数。

        Returns:
            Any: 目标 API 返回值。
        """

        return await self._ctx.call_capability(
            "api.call",
            api_name=api_name,
            version=version,
            args=kwargs,
        )

    async def get(self, api_name: str, *, version: str = "") -> Any:
        """获取单个可见 API 的元信息。

        Args:
            api_name: API 名称，支持 ``plugin_id.api_name`` 或唯一短名。
            version: 可选的 API 版本。

        Returns:
            Any: API 元信息；未命中时返回 ``None``。
        """

        return await self._ctx.call_capability(
            "api.get",
            api_name=api_name,
            version=version,
        )

    async def list(self, *, plugin_id: str = "") -> Any:
        """列出当前插件可见的全部 API。

        Args:
            plugin_id: 可选的提供方插件 ID 过滤条件。

        Returns:
            Any: API 元信息列表。
        """

        return await self._ctx.call_capability(
            "api.list",
            plugin_id=plugin_id,
        )

    async def replace_dynamic_apis(
        self,
        apis: builtins_list[dict[str, Any]],
        *,
        offline_reason: str = "动态 API 已下线",
    ) -> bool:
        """用新的动态 API 集合替换当前插件已暴露的动态 API。

        Args:
            apis: 最新的动态 API 声明列表。
            offline_reason: 被下线 API 对外返回的错误原因。

        Returns:
            bool: Host 是否接受本次动态 API 替换请求。
        """

        result = await self._ctx.call_capability(
            "api.replace_dynamic",
            apis=apis,
            offline_reason=offline_reason,
        )
        return bool(isinstance(result, dict) and result.get("success"))
