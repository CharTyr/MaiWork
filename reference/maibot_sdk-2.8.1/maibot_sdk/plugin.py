"""MaiBot 插件基类。

所有 SDK 插件必须继承此基类。
插件通过装饰器声明组件，通过 ``self.ctx`` 访问能力代理。
"""

# ruff: noqa: I001

from collections.abc import Callable, Iterable, Mapping
from inspect import isawaitable, iscoroutinefunction
from typing import Any, ClassVar

import logging

from .components import collect_components, collect_llm_providers
from .config import (
    PluginConfigBase,
    build_plugin_default_config,
    extract_plugin_config_version,
    generate_plugin_config_schema,
    is_plugin_config_class,
    merge_plugin_config_data,
    validate_plugin_config,
)
from .context import PluginContext
from .types import normalize_config_reload_subscription


class MaiBotPlugin:
    """SDK 插件基类。

    用法示例：

        from maibot_sdk import MaiBotPlugin, Command, Tool

        class MyPlugin(MaiBotPlugin):
            @Tool("greet", description="打招呼")
            async def handle_greet(self, stream_id: str = "", **kwargs):
                await self.ctx.send.text("你好！", stream_id)
                return {"content": "已回复"}

            @Command("hello", pattern=r"^/hello")
            async def handle_hello(self, **kwargs):
                await self.ctx.send.text("Hello!", kwargs["stream_id"])
                return True, "Hello!", 2

        def create_plugin():
            return MyPlugin()
    """

    config_reload_subscriptions: ClassVar[Iterable[str]] = ()
    """插件订阅的全局配置热重载范围。

    仅支持订阅 ``bot`` 和 ``model`` 两类全局配置广播。插件自身的
    ``config.toml`` 变化不通过该字段声明，而是固定通过
    :meth:`on_config_update` 接收。
    """

    config_model: ClassVar[type[PluginConfigBase] | None] = None
    """插件配置模型类。

    插件作者可通过继承 :class:`PluginConfigBase` 声明配置结构，并将模型类
    赋值给该属性。Runner 会据此自动生成默认配置、补齐缺失字段，并向 WebUI
    暴露可渲染的配置 Schema。
    """

    def __init__(self) -> None:
        """初始化插件基类状态。"""
        self._ctx: PluginContext | None = None
        self._dynamic_api_components: dict[str, dict[str, Any]] = {}
        self._dynamic_api_handlers: dict[str, Callable[..., Any]] = {}
        self._plugin_config_data: dict[str, Any] = {}
        self._plugin_config_instance: PluginConfigBase | None = None

    def _get_logger(self) -> logging.Logger:
        """获取当前插件可用的日志记录器。

        Returns:
            logging.Logger: 优先返回运行时上下文中的插件日志器；若上下文尚未注入，
            则回退到 SDK 模块级日志器。
        """

        if self._ctx is not None:
            return self._ctx.logger
        return logging.getLogger("maibot_sdk.plugin")

    @classmethod
    def get_config_model(cls) -> type[PluginConfigBase] | None:
        """返回当前插件声明的配置模型类。

        Returns:
            Optional[Type[PluginConfigBase]]: 配置模型类；未声明时返回 ``None``。
        """

        candidate = cls.config_model
        return candidate if is_plugin_config_class(candidate) else None

    @classmethod
    def has_config_model(cls) -> bool:
        """判断当前插件是否声明了配置模型。

        Returns:
            bool: 若已声明 ``config_model``，返回 ``True``。
        """

        return cls.get_config_model() is not None

    @classmethod
    def build_default_config(cls) -> dict[str, Any]:
        """构造当前插件的默认配置字典。

        Returns:
            Dict[str, Any]: 默认配置字典；未声明配置模型时返回空字典。
        """

        config_class = cls.get_config_model()
        if config_class is None:
            return {}
        return build_plugin_default_config(config_class)

    @classmethod
    def build_config_schema(
        cls,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict[str, Any]:
        """构造当前插件的 WebUI 配置 Schema。

        Args:
            plugin_id: 插件 ID。
            plugin_name: 插件名称。
            plugin_version: 插件版本。
            plugin_description: 插件描述。
            plugin_author: 插件作者。

        Returns:
            Dict[str, Any]: 插件配置 Schema；未声明配置模型时返回空字典。
        """

        config_class = cls.get_config_model()
        if config_class is None:
            return {}
        return generate_plugin_config_schema(
            config_class,
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )

    def normalize_plugin_config(self, config_data: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """根据配置模型补齐并规范化插件配置。

        Args:
            config_data: 原始配置数据。

        Returns:
            tuple[Dict[str, Any], bool]: 规范化后的配置字典，以及是否产生了自动补齐
            或归一化变更。
        """

        raw_config: dict[str, Any] = dict(config_data) if isinstance(config_data, Mapping) else {}
        config_class = type(self).get_config_model()
        if config_class is None:
            return raw_config, False

        default_config = type(self).build_default_config()
        extract_plugin_config_version(default_config)
        if not raw_config:
            validated_default_config = validate_plugin_config(config_class, default_config)
            return validated_default_config.model_dump(mode="python"), bool(default_config)

        extract_plugin_config_version(raw_config)
        merged_config, changed = merge_plugin_config_data(default_config, raw_config)
        validated_config = validate_plugin_config(config_class, merged_config)
        normalized_config = validated_config.model_dump(mode="python")
        return normalized_config, changed or normalized_config != merged_config

    def set_plugin_config(self, config: dict[str, Any]) -> None:
        """设置当前插件配置，并在需要时构造强类型配置实例。

        Args:
            config: 当前最新配置字典。
        """

        normalized_config, _ = self.normalize_plugin_config(config)
        self._plugin_config_data = normalized_config

        config_class = type(self).get_config_model()
        if config_class is None:
            self._plugin_config_instance = None
            return

        try:
            self._plugin_config_instance = validate_plugin_config(config_class, normalized_config)
        except Exception as exc:
            self._plugin_config_instance = None
            self._get_logger().warning(f"插件配置校验失败，将仅保留原始配置字典: {exc}")

    @property
    def config(self) -> PluginConfigBase:
        """返回当前插件的强类型配置实例。

        Returns:
            PluginConfigBase: 当前插件的配置模型实例。

        Raises:
            RuntimeError: 未声明配置模型或配置尚未完成注入时抛出。
        """

        if not type(self).has_config_model():
            raise RuntimeError("当前插件未声明 config_model，无法通过 config 属性访问强类型配置")
        if self._plugin_config_instance is None:
            raise RuntimeError("当前插件配置尚未完成注入")
        return self._plugin_config_instance

    def get_plugin_config_data(self) -> dict[str, Any]:
        """返回当前插件持有的原始配置字典副本。

        Returns:
            Dict[str, Any]: 原始配置字典副本。
        """

        return dict(self._plugin_config_data)

    @property
    def ctx(self) -> PluginContext:
        """返回插件运行时上下文。

        Returns:
            PluginContext: 由 Runner 注入的上下文对象。

        Raises:
            RuntimeError: 当前插件尚未注入上下文时抛出。
        """
        if self._ctx is None:
            raise RuntimeError("插件上下文尚未初始化，确保在 Runner 环境中运行")
        return self._ctx

    def _set_context(self, ctx: PluginContext) -> None:
        """由 Runner 注入上下文对象。

        Args:
            ctx: 当前插件对应的运行时上下文。
        """
        self._ctx = ctx

    def get_default_config(self) -> dict[str, Any]:
        """返回当前插件的默认配置。

        Returns:
            Dict[str, Any]: 默认配置字典。
        """

        return type(self).build_default_config()

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict[str, Any]:
        """返回当前插件的 WebUI 配置 Schema。

        Args:
            plugin_id: 插件 ID。
            plugin_name: 插件名称。
            plugin_version: 插件版本。
            plugin_description: 插件描述。
            plugin_author: 插件作者。

        Returns:
            Dict[str, Any]: 当前插件的 WebUI 配置 Schema。
        """

        return type(self).build_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )

    def get_components(self) -> list[dict[str, Any]]:
        """收集所有被装饰器标记的组件信息

        由 Runner 自动调用，无需手动覆盖。
        """
        components = collect_components(self)
        components.extend(self.get_dynamic_api_components())
        return components

    def get_llm_providers(self) -> list[dict[str, Any]]:
        """收集所有被装饰器标记的 LLM Provider 信息。

        Returns:
            list[dict[str, Any]]: 可直接同步到 Host 的 LLM Provider 声明列表。
        """
        return collect_llm_providers(self)

    @staticmethod
    def _build_dynamic_api_key(name: str, version: str) -> str:
        """构造动态 API 的稳定键。"""

        normalized_name = str(name or "").strip()
        normalized_version = str(version or "1").strip() or "1"
        return f"{normalized_name}@{normalized_version}"

    def register_dynamic_api(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        description: str = "",
        version: str = "1",
        public: bool = False,
        handler_name: str = "",
        **metadata: Any,
    ) -> dict[str, Any]:
        """注册一个动态 API 定义。

        Args:
            name: 对外暴露的 API 名称。
            handler: 当前 API 对应的处理函数。
            description: API 描述。
            version: API 版本。
            public: 是否允许其他插件调用。
            handler_name: 可选的内部处理器名称；留空时自动生成。
            **metadata: 额外元数据。

        Returns:
            dict[str, Any]: 可直接同步到 Host 的组件声明。
        """

        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("动态 API 名称不能为空")
        normalized_version = str(version or "1").strip() or "1"
        resolved_handler_name = str(handler_name or f"dynamic_api__{normalized_name}__{normalized_version}").strip()
        if not resolved_handler_name:
            raise ValueError("动态 API handler_name 不能为空")

        component_metadata: dict[str, Any] = {
            "description": description,
            "version": normalized_version,
            "public": bool(public),
            "dynamic": True,
            "handler_name": resolved_handler_name,
            **metadata,
        }
        component: dict[str, Any] = {
            "name": normalized_name,
            "type": "API",
            "metadata": component_metadata,
        }
        self._dynamic_api_components[self._build_dynamic_api_key(normalized_name, normalized_version)] = component
        self._dynamic_api_handlers[resolved_handler_name] = handler
        return {
            "name": component["name"],
            "type": component["type"],
            "metadata": dict(component_metadata),
        }

    def unregister_dynamic_api(self, name: str, *, version: str = "1") -> bool:
        """注销一个动态 API 定义。"""

        api_key = self._build_dynamic_api_key(name, version)
        component = self._dynamic_api_components.pop(api_key, None)
        if component is None:
            return False

        handler_name = str(component["metadata"].get("handler_name", "") or "").strip()
        if handler_name:
            handler_still_used = any(
                str(candidate["metadata"].get("handler_name", "") or "").strip() == handler_name
                for candidate in self._dynamic_api_components.values()
            )
            if not handler_still_used:
                self._dynamic_api_handlers.pop(handler_name, None)
        return True

    def clear_dynamic_apis(self) -> None:
        """清空当前插件维护的全部动态 API。"""

        self._dynamic_api_components.clear()
        self._dynamic_api_handlers.clear()

    def get_dynamic_api_components(self) -> list[dict[str, Any]]:
        """返回当前插件维护的动态 API 组件声明。"""

        return [
            {
                "name": component["name"],
                "type": component["type"],
                "metadata": dict(component["metadata"]),
            }
            for component in self._dynamic_api_components.values()
        ]

    async def sync_dynamic_apis(self, *, offline_reason: str = "动态 API 已下线") -> bool:
        """将当前动态 API 集合同步到 Host。"""

        return await self.ctx.api.replace_dynamic_apis(
            self.get_dynamic_api_components(),
            offline_reason=offline_reason,
        )

    async def invoke_component(self, component_name: str, **kwargs: Any) -> Any:
        """为动态 API 提供默认的组件调用分发。"""

        handler = self._dynamic_api_handlers.get(component_name)
        if handler is None:
            raise AttributeError(f"插件未注册动态组件处理器: {component_name}")

        if iscoroutinefunction(handler):
            return await handler(**kwargs)

        result = handler(**kwargs)
        if isawaitable(result):
            return await result
        return result

    def get_config_reload_subscriptions(self) -> list[str]:
        """返回当前插件订阅的全局配置热重载范围。

        Returns:
            list[str]: 归一化后的订阅范围列表，仅包含 ``bot`` 和 ``model``。

        Raises:
            TypeError: 当 ``config_reload_subscriptions`` 不是可迭代对象时抛出。
            ValueError: 当订阅值不受支持时抛出。
        """

        raw_subscriptions = type(self).config_reload_subscriptions
        if isinstance(raw_subscriptions, str):
            raise TypeError("config_reload_subscriptions 必须是可迭代集合，不能直接使用字符串")
        if not isinstance(raw_subscriptions, Iterable):
            raise TypeError("config_reload_subscriptions 必须是可迭代集合")

        normalized_subscriptions: set[str] = set()
        for subscription in raw_subscriptions:
            normalized_subscriptions.add(normalize_config_reload_subscription(subscription))
        return sorted(normalized_subscriptions)

    async def on_load(self) -> None:
        """插件加载完成后的回调。

        Raises:
            NotImplementedError: 子类未覆盖该生命周期方法时抛出。
        """
        raise NotImplementedError("插件必须实现 on_load() 来处理加载生命周期")

    async def on_unload(self) -> None:
        """插件卸载前的回调。

        Raises:
            NotImplementedError: 子类未覆盖该生命周期方法时抛出。
        """
        raise NotImplementedError("插件必须实现 on_unload() 来处理卸载生命周期")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """处理配置热更新事件。

        Args:
            scope: 配置变更范围，取值为 ``self``、``bot``、``model``。
            config_data: 当前范围对应的最新配置数据。
            version: 配置版本号。

        Raises:
            NotImplementedError: 子类未覆盖该生命周期方法时抛出。
        """
        raise NotImplementedError("插件必须实现 on_config_update() 来处理配置热重载")
