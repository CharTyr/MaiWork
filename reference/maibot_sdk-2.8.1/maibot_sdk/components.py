"""组件声明装饰器

用于在插件类中声明 Tool/Command/EventHandler/HookHandler/MessageGateway/LLMProvider 等能力。
兼容入口 `Action` 仍然保留，但会在内部转换为 Tool 声明。
"""

import warnings
from collections.abc import Callable
from typing import Any

from .types import (
    ActivationType,
    APIComponentInfo,
    ChatMode,
    CommandComponentInfo,
    ErrorPolicy,
    EventHandlerComponentInfo,
    EventType,
    HomeCardComponentInfo,
    HookHandlerComponentInfo,
    HookMode,
    HookOrder,
    LLMProviderInfo,
    MessageGatewayComponentInfo,
    MessageGatewayRouteType,
    ToolComponentInfo,
    ToolParameterInfo,
    build_tool_detailed_description,
)

# 装饰器签名: 接受函数返回函数
_Decorator = Callable[[Callable[..., Any]], Callable[..., Any]]

# 标记属性名，用于在方法上附加组件信息
_COMPONENT_INFO_ATTR = "__maibot_component_info__"
_LLM_PROVIDER_INFO_ATTR = "__maibot_llm_provider_info__"


def _normalize_tool_descriptions(
    name: str,
    description: str = "",
    brief_description: str = "",
    detailed_description: str = "",
) -> tuple[str, str]:
    """规范化工具的简要描述与详细描述。

    Args:
        name: 工具名称。
        description: 兼容旧参数的描述字段。
        brief_description: 新版简要描述。
        detailed_description: 新版详细描述。

    Returns:
        tuple[str, str]: ``(简要描述, 详细描述)``。
    """

    normalized_brief_description = str(brief_description or description or f"工具 {name}").strip()
    normalized_detailed_description = str(detailed_description or "").strip()
    return normalized_brief_description, normalized_detailed_description


def _build_tool_parameters_schema(
    parameters: list[ToolParameterInfo] | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """将 Tool 装饰器参数定义转换为对象级 Schema。

    Args:
        parameters: Tool 装饰器收到的参数定义。

    Returns:
        dict[str, Any] | None: 规范化后的对象级 Schema。
    """

    if isinstance(parameters, dict):
        if parameters.get("type") == "object" or "properties" in parameters:
            return dict(parameters)

        required_names: list[str] = []
        normalized_properties: dict[str, Any] = {}
        for property_name, property_schema in parameters.items():
            if not isinstance(property_schema, dict):
                continue

            property_schema_copy = dict(property_schema)
            if bool(property_schema_copy.pop("required", False)):
                required_names.append(str(property_name))
            normalized_properties[str(property_name)] = property_schema_copy

        schema: dict[str, Any] = {
            "type": "object",
            "properties": normalized_properties,
        }
        if required_names:
            schema["required"] = required_names
        return schema

    if isinstance(parameters, list) and parameters:
        properties = {parameter.name: parameter.to_parameter_schema() for parameter in parameters}
        required_names = [parameter.name for parameter in parameters if parameter.required]
        schema = {
            "type": "object",
            "properties": properties,
        }
        if required_names:
            schema["required"] = required_names
        return schema

    return None


def _build_action_parameters_schema(action_parameters: dict[str, Any] | None) -> dict[str, Any] | None:
    """将旧 Action 参数定义转换为 Tool 兼容 Schema。

    Args:
        action_parameters: Action 的参数声明。

    Returns:
        dict[str, Any] | None: 统一对象级参数 Schema。
    """

    if not isinstance(action_parameters, dict) or not action_parameters:
        return None

    properties: dict[str, Any] = {}
    for parameter_name, parameter_description in action_parameters.items():
        normalized_name = str(parameter_name or "").strip()
        if not normalized_name:
            continue
        properties[normalized_name] = {
            "type": "string",
            "description": str(parameter_description or "").strip() or "兼容旧 Action 参数",
        }

    if not properties:
        return None
    return {
        "type": "object",
        "properties": properties,
    }


def _build_action_detailed_description(
    action_parameters: dict[str, Any] | None,
    action_require: list[str] | None,
    associated_types: list[str] | None,
    activation_type: ActivationType,
    activation_keywords: list[str] | None,
    action_prompt: str,
    detailed_description: str = "",
) -> str:
    """构造兼容旧 Action 的详细工具描述。

    Args:
        action_parameters: Action 参数定义。
        action_require: Action 使用要求。
        associated_types: Action 关联消息类型。
        activation_type: Action 激活方式。
        activation_keywords: Action 关键词。
        action_prompt: Action 提示语。
        detailed_description: 额外详细描述。

    Returns:
        str: 兼容旧 Action 的详细描述文本。
    """

    parameters_schema = _build_action_parameters_schema(action_parameters)
    description_parts: list[str] = []

    parameter_description = build_tool_detailed_description(parameters_schema)
    if parameter_description:
        description_parts.append(parameter_description)

    normalized_requirements = [str(item).strip() for item in (action_require or []) if str(item).strip()]
    if normalized_requirements:
        description_parts.append("使用建议：\n" + "\n".join(f"- {item}" for item in normalized_requirements))

    normalized_types = [str(item).strip() for item in (associated_types or []) if str(item).strip()]
    if normalized_types:
        description_parts.append(f"适用消息类型：{'、'.join(normalized_types)}。")

    activation_lines = [f"兼容旧 Action 激活方式：{activation_type.value}。"]
    normalized_keywords = [str(item).strip() for item in (activation_keywords or []) if str(item).strip()]
    if normalized_keywords:
        activation_lines.append(f"激活关键词：{'、'.join(normalized_keywords)}。")
    if str(action_prompt or "").strip():
        activation_lines.append(f"原始 Action 提示语：{str(action_prompt).strip()}。")
    description_parts.append("\n".join(activation_lines))

    if str(detailed_description or "").strip():
        description_parts.append(str(detailed_description).strip())

    return "\n\n".join(part for part in description_parts if part).strip()


def Action(
    name: str,
    description: str = "",
    activation_type: ActivationType = ActivationType.ALWAYS,
    activation_keywords: list[str] | None = None,
    activation_probability: float = 1.0,
    chat_mode: ChatMode = ChatMode.NORMAL,
    action_parameters: dict[str, Any] | None = None,
    action_require: list[str] | None = None,
    associated_types: list[str] | None = None,
    parallel_action: bool = False,
    action_prompt: str = "",
    **metadata: Any,
) -> _Decorator:
    """兼容旧 Action 的组件装饰器。

    该装饰器不再生成独立的 Action 声明，而是会把 Action 元数据转换为
    Tool 元数据并注册为工具组件。这样旧插件仍然可以继续使用 ``@Action``，
    但主程序内部只维护一套 Tool 抽象。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将兼容旧 Action 的 Tool 元数据附着到目标函数。

        Args:
            func: 被声明为兼容 Action 的方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """

        warnings.warn(
            "`Action` 装饰器已废弃，将在内部按 Tool 组件注册；请优先改用 `Tool`。",
            DeprecationWarning,
            stacklevel=2,
        )

        brief_description, _ = _normalize_tool_descriptions(name=name, description=description)
        converted_parameters_schema = _build_action_parameters_schema(action_parameters)
        info = ToolComponentInfo(
            name=name,
            description=brief_description,
            brief_description=brief_description,
            detailed_description=_build_action_detailed_description(
                action_parameters=action_parameters,
                action_require=action_require,
                associated_types=associated_types,
                activation_type=activation_type,
                activation_keywords=activation_keywords,
                action_prompt=action_prompt,
            ),
            parameters_raw=converted_parameters_schema or {},
            invoke_method="plugin.invoke_action",
            metadata={
                **metadata,
                "legacy_action": True,
                "legacy_component_type": "ACTION",
                "activation_type": activation_type.value,
                "activation_keywords": list(activation_keywords or []),
                "activation_probability": activation_probability,
                "chat_mode": chat_mode.value,
                "action_parameters": dict(action_parameters or {}),
                "action_require": list(action_require or []),
                "associated_types": list(associated_types or []),
                "parallel_action": parallel_action,
                "action_prompt": action_prompt,
            },
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def Command(
    name: str,
    description: str = "",
    pattern: str = "",
    aliases: list[str] | None = None,
    **metadata: Any,
) -> _Decorator:
    """Command 组件装饰器

    用法：
        @Command("hello", description="打招呼", pattern=r"^/hello")
        async def handle_hello(self, **kwargs):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 Command 元数据附着到目标函数。

        Args:
            func: 被声明为 Command 的方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """
        info = CommandComponentInfo(
            name=name,
            description=description,
            command_pattern=pattern,
            aliases=aliases or [],
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def API(
    name: str,
    description: str = "",
    version: str = "1",
    public: bool = False,
    **metadata: Any,
) -> _Decorator:
    """API 组件装饰器。

    用法：
        @API("render_html", description="渲染 HTML", version="1", public=True)
        async def handle_render_html(self, html: str):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 API 元数据附着到目标函数。

        Args:
            func: 被声明为 API 的方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """

        info = APIComponentInfo(
            name=name,
            description=description,
            version=str(version or "1").strip() or "1",
            public=bool(public),
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def Tool(
    name: str,
    description: str = "",
    brief_description: str = "",
    detailed_description: str = "",
    parameters: list[ToolParameterInfo] | dict[str, Any] | None = None,
    **metadata: Any,
) -> _Decorator:
    """Tool 组件装饰器

    用法（结构化参数）：
        @Tool("search", description="搜索", parameters=[
            ToolParameterInfo(name="query", param_type=ToolParamType.STRING, description="搜索内容"),
        ])
        async def handle_search(self, query: str, **kwargs):
            ...

    用法（dict 参数，兼容旧式声明）：
        @Tool("search", description="搜索", parameters={"query": {"type": "string"}})
        async def handle_search(self, query: str, **kwargs):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 Tool 元数据附着到目标函数。

        Args:
            func: 被声明为 Tool 的方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """
        typed_params: list[ToolParameterInfo] = []
        raw_params: dict[str, Any] = {}
        if isinstance(parameters, list):
            typed_params = parameters
        elif isinstance(parameters, dict):
            raw_params = parameters

        normalized_brief_description, normalized_detailed_description = _normalize_tool_descriptions(
            name=name,
            description=description,
            brief_description=brief_description,
            detailed_description=detailed_description,
        )
        parameters_schema = _build_tool_parameters_schema(parameters)

        info = ToolComponentInfo(
            name=name,
            description=normalized_brief_description,
            brief_description=normalized_brief_description,
            detailed_description=build_tool_detailed_description(
                parameters_schema,
                fallback_description=normalized_detailed_description,
            ),
            parameters=typed_params,
            parameters_raw=raw_params,
            invoke_method="plugin.invoke_tool",
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def EventHandler(
    name: str,
    description: str = "",
    event_type: EventType = EventType.ON_MESSAGE,
    intercept_message: bool = False,
    weight: int = 0,
    **metadata: Any,
) -> _Decorator:
    """EventHandler 组件装饰器

    Args:
        intercept_message: 为 True 时阻塞消息链（同步等待返回），为 False 时异步 fire-and-forget。
        weight: 权重越高越先执行，与旧系统的 weight 语义一致。

    用法：
        @EventHandler("on_start", event_type=EventType.ON_START)
        async def handle_start(self, **kwargs):
            ...

        @EventHandler("msg_filter", event_type=EventType.ON_MESSAGE_PRE_PROCESS,
                       intercept_message=True, weight=100)
        async def filter_message(self, message, **kwargs):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 EventHandler 元数据附着到目标函数。

        Args:
            func: 被声明为 EventHandler 的方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """
        info = EventHandlerComponentInfo(
            name=name,
            description=description,
            event_type=event_type,
            intercept_message=intercept_message,
            weight=weight,
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def HookHandler(
    hook: str,
    *,
    name: str = "",
    description: str = "",
    mode: HookMode = HookMode.BLOCKING,
    order: HookOrder = HookOrder.NORMAL,
    timeout_ms: int = 0,
    error_policy: ErrorPolicy = ErrorPolicy.SKIP,
    **metadata: Any,
) -> _Decorator:
    """声明命名 Hook 处理器。

    Args:
        hook: 订阅的命名 Hook 名称。
        name: 可选的组件名称；留空时默认使用方法名。
        description: 组件描述。
        mode: Hook 处理模式。
        order: Hook 在同一模式内的顺序槽位。
        timeout_ms: 处理器超时，单位为毫秒；传入 ``0`` 表示使用 Hook 默认值。
        error_policy: 异常处理策略。
        **metadata: 额外元数据。

    Returns:
        _Decorator: 用于修饰插件方法的装饰器。

    用法：
        @HookHandler(
            "heart_fc.heart_flow_cycle_start",
            name="cycle_start_guard",
            mode=HookMode.BLOCKING,
            order=HookOrder.EARLY,
        )
        async def handle_cycle_start(self, **kwargs):
            return {"action": "continue", "modified_kwargs": kwargs}

        @HookHandler(
            "heart_fc.heart_flow_cycle_start",
            mode=HookMode.OBSERVE,
        )
        async def observe_cycle_start(self, **kwargs):
            await self.ctx.db.save("hook_log", kwargs)
    """

    normalized_hook = str(hook or "").strip()
    if not normalized_hook:
        raise ValueError("HookHandler 的 hook 名称不能为空")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 HookHandler 元数据附着到目标函数。

        Args:
            func: 被声明为 HookHandler 的方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """
        component_name = str(name or func.__name__).strip()
        if not component_name:
            raise ValueError("HookHandler 的组件名称不能为空")

        info = HookHandlerComponentInfo(
            name=component_name,
            hook=normalized_hook,
            description=description,
            mode=mode,
            order=order,
            timeout_ms=timeout_ms,
            error_policy=error_policy,
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def HomeCard(
    name: str,
    title: str,
    content: Any = "",
    *,
    description: str = "",
    link_url: str = "",
    link_label: str = "",
    icon: str = "",
    width: str = "medium",
    order: int = 1000,
    **metadata: Any,
) -> _Decorator:
    """声明 WebUI 首页卡片。

    卡片内容会以声明式数据交给 WebUI 渲染。为安全起见，WebUI 不执行
    插件提供的 HTML 或 JavaScript；需要富文本时建议使用 Markdown 字符串。
    """

    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("HomeCard 的 name 不能为空")
    normalized_title = str(title or "").strip()
    if not normalized_title:
        raise ValueError("HomeCard 的 title 不能为空")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将首页卡片元数据附着到目标函数。"""

        info = HomeCardComponentInfo(
            name=normalized_name,
            title=normalized_title,
            description=description,
            content=content,
            link_url=str(link_url or "").strip(),
            link_label=str(link_label or "").strip(),
            icon=str(icon or "").strip(),
            width=str(width or "medium").strip().lower() or "medium",
            order=int(order),
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def WorkflowStep(*args: Any, **kwargs: Any) -> _Decorator:
    """已移除的旧装饰器入口。

    `WorkflowStep` 在 SDK 2.0 中被 `HookHandler` 取代。这里保留同名入口，
    仅用于在旧代码仍尝试使用时给出明确错误信息，而不是静默兼容。
    """

    raise RuntimeError("`WorkflowStep` 已移除，请改用 `HookHandler`。这是一个不向后兼容更改。")


def _normalize_message_gateway_route_type(route_type: str) -> MessageGatewayRouteType:
    """规范化消息网关路由类型字符串。

    Args:
        route_type: 用户声明的路由类型字符串。

    Returns:
        MessageGatewayRouteType: 归一化后的路由类型枚举。

    Raises:
        ValueError: 当输入值不是支持的路由类型时抛出。
    """

    normalized_route_type = str(route_type or "").strip().lower()
    alias_map = {
        "recv": MessageGatewayRouteType.RECEIVE,
        "receive": MessageGatewayRouteType.RECEIVE,
        "recive": MessageGatewayRouteType.RECEIVE,
        "send": MessageGatewayRouteType.SEND,
        "duplex": MessageGatewayRouteType.DUPLEX,
    }
    resolved_route_type = alias_map.get(normalized_route_type)
    if resolved_route_type is None:
        raise ValueError(f"不支持的消息网关路由类型: {route_type}")
    return resolved_route_type


def MessageGateway(
    route_type: str,
    *,
    name: str = "",
    description: str = "",
    platform: str = "",
    protocol: str = "",
    account_id: str = "",
    scope: str = "",
    **metadata: Any,
) -> _Decorator:
    """声明消息网关组件。

    Args:
        route_type: 网关路由类型，支持 ``send``、``receive``、``duplex``。
        name: 可选的组件名；留空时默认使用方法名。
        description: 组件描述。
        platform: 可选的平台名称；为空时允许运行时动态上报。
        protocol: 可选的协议或实现名称。
        account_id: 可选的账号 ID 或 ``self_id``。
        scope: 可选的路由作用域。
        **metadata: 额外元数据。

    Returns:
        _Decorator: 用于修饰插件方法的装饰器。
    """

    normalized_route_type = _normalize_message_gateway_route_type(route_type)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 MessageGateway 元数据附着到目标函数。

        Args:
            func: 被声明为消息网关的插件方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """

        component_name = name or func.__name__
        info = MessageGatewayComponentInfo(
            name=component_name,
            description=description,
            route_type=normalized_route_type,
            platform=str(platform or "").strip(),
            protocol=str(protocol or "").strip(),
            account_id=str(account_id or "").strip(),
            scope=str(scope or "").strip(),
            metadata=metadata,
        )
        setattr(func, _COMPONENT_INFO_ATTR, info)
        return func

    return decorator


def LLMProvider(
    client_type: str,
    *,
    name: str = "",
    description: str = "",
    version: str = "1.0.0",
    **metadata: Any,
) -> _Decorator:
    """声明 LLM Provider。

    Args:
        client_type: 客户端类型标识，对应模型配置中的 ``api_providers[].client_type``。
        name: Provider 展示名称；留空时使用 ``client_type``。
        description: Provider 描述。
        version: Provider 实现版本。
        **metadata: 额外元数据。

    Returns:
        _Decorator: 用于修饰插件方法的装饰器。
    """
    normalized_client_type = str(client_type or "").strip()
    if not normalized_client_type:
        raise ValueError("LLMProvider 的 client_type 不能为空")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """将 LLM Provider 元数据附着到目标函数。

        Args:
            func: 被声明为 LLM Provider 的插件方法。

        Returns:
            Callable[..., Any]: 原始函数对象。
        """
        info = LLMProviderInfo(
            client_type=normalized_client_type,
            name=str(name or normalized_client_type).strip(),
            description=description,
            version=str(version or "1.0.0").strip() or "1.0.0",
            metadata=metadata,
        )
        setattr(func, _LLM_PROVIDER_INFO_ATTR, info)
        return func

    return decorator


def collect_components(instance: object) -> list[dict[str, Any]]:
    """从插件实例中收集所有被装饰器标记的组件信息

    Runner 在加载插件后调用此函数。

    Returns:
        组件信息字典列表，每个字典可直接序列化为 ComponentDeclaration
    """
    components: list[dict[str, Any]] = []
    for attr_name in dir(instance):
        try:
            attr = getattr(instance, attr_name)
        except Exception:
            continue
        if callable(attr) and hasattr(attr, _COMPONENT_INFO_ATTR):
            info = getattr(attr, _COMPONENT_INFO_ATTR)
            declared_metadata = info.model_dump(mode="json", exclude={"name", "type", "metadata"})
            component_metadata = {
                **info.metadata,
                **declared_metadata,
                "handler_name": attr_name,
            }
            components.append(
                {
                    "name": info.name,
                    "type": info.type.value,
                    "metadata": component_metadata,
                }
            )
    return components


def collect_llm_providers(instance: object) -> list[dict[str, Any]]:
    """从插件实例中收集所有被装饰器标记的 LLM Provider。

    Args:
        instance: 插件实例。

    Returns:
        list[dict[str, Any]]: LLM Provider 声明字典列表。
    """
    providers: list[dict[str, Any]] = []
    for attr_name in dir(instance):
        try:
            attr = getattr(instance, attr_name)
        except Exception:
            continue
        if callable(attr) and hasattr(attr, _LLM_PROVIDER_INFO_ATTR):
            info = getattr(attr, _LLM_PROVIDER_INFO_ATTR)
            provider_metadata = dict(info.metadata)
            provider_metadata.setdefault("handler_name", attr_name)
            providers.append(
                {
                    "client_type": info.client_type,
                    "name": info.name,
                    "description": info.description,
                    "version": info.version,
                    "metadata": provider_metadata,
                }
            )
    return providers
