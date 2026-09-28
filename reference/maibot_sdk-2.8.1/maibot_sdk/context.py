"""插件运行时上下文

为插件提供能力代理接口、标准路径和日志入口，所有能力调用通过上下文发起。
PluginContext 由 Runner SDK Runtime 在插件加载时注入。
"""

import logging as stdlib_logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maibot_sdk.capabilities.api import APICapability
from maibot_sdk.capabilities.chat import ChatCapability
from maibot_sdk.capabilities.component import ComponentCapability
from maibot_sdk.capabilities.config import ConfigCapability
from maibot_sdk.capabilities.database import DatabaseCapability
from maibot_sdk.capabilities.emoji import EmojiCapability
from maibot_sdk.capabilities.frequency import FrequencyCapability
from maibot_sdk.capabilities.gateway import GatewayCapability
from maibot_sdk.capabilities.knowledge import KnowledgeCapability
from maibot_sdk.capabilities.llm import LLMCapability
from maibot_sdk.capabilities.maisaka import MaisakaCapability
from maibot_sdk.capabilities.message import MessageCapability
from maibot_sdk.capabilities.person import PersonCapability
from maibot_sdk.capabilities.render import RenderCapability
from maibot_sdk.capabilities.send import SendCapability
from maibot_sdk.capabilities.statistics import StatisticsCapability
from maibot_sdk.capabilities.tool import ToolCapability

# RPC 调用函数类型: async (method, plugin_id, payload, timeout_ms=None) -> result
RpcCallFn = Callable[..., Awaitable[Any]]

_CAPABILITY_RESULT_KEYS: dict[str, str] = {
    "api.call": "result",
    "api.get": "api",
    "api.list": "apis",
    "chat.get_all_streams": "streams",
    "chat.get_group_streams": "streams",
    "chat.get_private_streams": "streams",
    "chat.get_stream_by_group_id": "stream",
    "chat.get_stream_by_user_id": "stream",
    "component.get_all_plugins": "plugins",
    "component.get_plugin_info": "plugin",
    "component.list_loaded_plugins": "plugins",
    "component.list_registered_plugins": "plugins",
    "config.get": "value",
    "config.get_all": "value",
    "config.get_plugin": "value",
    "database.count": "count",
    "database.delete": "result",
    "database.get": "result",
    "database.query": "result",
    "database.save": "result",
    "emoji.get_all": "emojis",
    "emoji.get_by_description": "emoji",
    "emoji.get_count": "count",
    "emoji.get_emotions": "emotions",
    "emoji.get_info": "info",
    "emoji.get_random": "emojis",
    "frequency.get_adjust": "value",
    "frequency.get_current_talk_value": "value",
    "knowledge.search": "content",
    "llm.get_available_models": "models",
    "message.build_readable": "text",
    "message.count_new": "count",
    "message.get_by_id": "message",
    "message.get_by_time": "messages",
    "message.get_by_time_in_chat": "messages",
    "message.get_recent": "messages",
    "person.get_id": "person_id",
    "person.get_id_by_name": "person_id",
    "person.get_value": "value",
    "render.html2png": "result",
    "statistics.local.message_trend": "series",
    "statistics.local.model_trend": "series",
    "statistics.local.models": "models",
    "statistics.local.online_time_trend": "series",
    "statistics.local.token_distribution": "distribution",
    "statistics.local.token_trend": "series",
    "statistics.local.tool_trend": "series",
    "tool.get_definitions": "tools",
}

_BOOLEAN_SUCCESS_CAPABILITIES = {
    "frequency.set_adjust",
    "send.command",
    "send.custom",
    "send.emoji",
    "send.forward",
    "send.hybrid",
    "send.image",
    "send.text",
}

_DETAILED_SEND_CAPABILITIES = {
    "send.command",
    "send.custom",
    "send.emoji",
    "send.forward",
    "send.hybrid",
    "send.image",
    "send.text",
}

_ALLOWED_RAW_HOST_METHODS = frozenset(
    {
        "cap.call",
        "host.route_message",
        "host.update_message_gateway_state",
    }
)


@dataclass(frozen=True)
class PluginPaths:
    """插件运行时被授予的标准路径。"""

    data_dir: Path
    runtime_dir: Path


class PluginContext:
    """插件运行时上下文

    插件通过 self.ctx 访问此对象，获取所有能力代理。

    日志使用方式：

        # 推荐：直接使用 stdlib logging，日志自动通过 IPC 传输到主进程
        self.ctx.logger.info("插件已启动")

        # 或直接使用 logging.getLogger("插件名")
        import logging
        logger = logging.getLogger(__name__)   # 名称不以 plugin. 开头也能正常工作
    """

    def __init__(self, plugin_id: str, rpc_call: RpcCallFn | None = None, paths: PluginPaths | None = None) -> None:
        """初始化插件运行时上下文。

        Args:
            plugin_id: 当前插件 ID。
            rpc_call: RPC 调用函数，由 Runner 注入。
            paths: 插件运行时路径，由 Runner 按插件 ID 分配。
        """
        self._plugin_id: str = plugin_id
        self._rpc_call: RpcCallFn | None = rpc_call
        self._logger: stdlib_logging.Logger | None = None
        self.paths: PluginPaths = paths or PluginPaths(
            data_dir=Path("data") / "plugins" / plugin_id,
            runtime_dir=Path("temp") / "plugins" / plugin_id,
        )
        current_ctx: Any = self

        # 能力代理
        self.api: APICapability = APICapability(current_ctx)
        self.gateway: GatewayCapability = GatewayCapability(current_ctx)
        self.send: SendCapability = SendCapability(current_ctx)
        self.db: DatabaseCapability = DatabaseCapability(current_ctx)
        self.llm: LLMCapability = LLMCapability(current_ctx)
        self.config: ConfigCapability = ConfigCapability(current_ctx)
        self.emoji: EmojiCapability = EmojiCapability(current_ctx)
        self.message: MessageCapability = MessageCapability(current_ctx)
        self.frequency: FrequencyCapability = FrequencyCapability(current_ctx)
        self.component: ComponentCapability = ComponentCapability(current_ctx)
        self.chat: ChatCapability = ChatCapability(current_ctx)
        self.person: PersonCapability = PersonCapability(current_ctx)
        self.render: RenderCapability = RenderCapability(current_ctx)
        self.knowledge: KnowledgeCapability = KnowledgeCapability(current_ctx)
        self.tool: ToolCapability = ToolCapability(current_ctx)
        self.statistics: StatisticsCapability = StatisticsCapability(current_ctx)
        self.maisaka: MaisakaCapability = MaisakaCapability(current_ctx)

    @property
    def plugin_id(self) -> str:
        """返回当前插件 ID。

        Returns:
            str: 当前插件 ID。
        """
        return self._plugin_id

    @property
    def logger(self) -> stdlib_logging.Logger:
        """返回属于本插件的标准 Logger。

        Logger 名称为 ``plugin.<plugin_id>``，在 Runner 进程中该
        Logger 的展示会被 :class:`RunnerIPCLogHandler` 自动
        技持到主进程。

        使用示例::

            self.ctx.logger.info("消息已处理")
            self.ctx.logger.error("出错了", exc_info=True)
        """
        if self._logger is None:
            self._logger = stdlib_logging.getLogger(f"plugin.{self._plugin_id}")
        return self._logger

    async def call_host_method(
        self,
        method: str,
        *,
        plugin_id: str = "",
        payload: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        """调用 Host 暴露的原始 RPC 方法。

        Args:
            method: Host 侧 RPC 方法名。
            plugin_id: 可选的目标插件 ID；Runner 端会强制绑定为当前插件身份。
            payload: 原始 RPC 载荷。
            timeout_ms: 可选的本次 RPC 超时时间，单位毫秒。

        Returns:
            Any: Host 方法返回的业务数据。

        Raises:
            RuntimeError: 当前上下文尚未注入可用的 RPC 调用函数时抛出。
            PermissionError: 当前插件尝试调用未开放的 Host 原始 RPC 方法时抛出。
        """
        if self._rpc_call is None:
            raise RuntimeError("PluginContext 尚未初始化 RPC 连接")
        normalized_method = str(method or "").strip()
        if normalized_method not in _ALLOWED_RAW_HOST_METHODS:
            raise PermissionError(
                f"插件不允许直接调用 Host 原始 RPC 方法: {normalized_method or '<empty>'}。"
                "请优先使用 self.ctx 上的能力代理。"
            )

        if timeout_ms is None:
            return await self._rpc_call(normalized_method, plugin_id or self._plugin_id, payload)
        return await self._rpc_call(
            normalized_method,
            plugin_id or self._plugin_id,
            payload,
            timeout_ms=timeout_ms,
        )

    async def call_capability(
        self,
        capability: str,
        timeout_ms: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """调用一项能力。

        Args:
            capability: 能力名称，如 ``send.text``、``db.query``。
            timeout_ms: 可选的本次能力 RPC 超时时间，单位毫秒。
            **kwargs: 能力参数。

        Returns:
            Any: 能力调用结果。
        """
        result = await self.call_host_method(
            "cap.call",
            payload={
                "capability": capability,
                "args": kwargs,
            },
            timeout_ms=timeout_ms,
        )
        return self._normalize_capability_result(
            capability,
            result,
            return_details=bool(kwargs.get("return_details", False)),
        )

    @staticmethod
    def _normalize_capability_result(
        capability: str,
        result: Any,
        *,
        return_details: bool = False,
    ) -> Any:
        """将 Host 侧 RPC 包装结果还原成插件更直观的返回值。"""
        if not isinstance(result, dict) or "success" not in result:
            return result

        if capability in _CAPABILITY_RESULT_KEYS:
            result_key = _CAPABILITY_RESULT_KEYS[capability]
            if result_key in result:
                return result[result_key]

        if capability in _DETAILED_SEND_CAPABILITIES and return_details:
            return {
                "sent": bool(result.get("sent", result.get("success"))),
                "message_id": result.get("message_id"),
            }

        if capability in _BOOLEAN_SUCCESS_CAPABILITIES:
            return bool(result.get("success"))

        return result
