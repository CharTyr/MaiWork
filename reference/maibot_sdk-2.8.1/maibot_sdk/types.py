"""SDK 类型定义。

定义插件开发中使用的公共类型。
"""

from copy import deepcopy
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

_COMPONENT_TYPE_ALIASES: dict[str, str] = {
    "ACTION": "ACTION",
    "API": "API",
    "COMMAND": "COMMAND",
    "EVENT_HANDLER": "EVENT_HANDLER",
    "HOOK_HANDLER": "HOOK_HANDLER",
    "HOME_CARD": "HOME_CARD",
    "MESSAGE_GATEWAY": "MESSAGE_GATEWAY",
    "TOOL": "TOOL",
    "action": "ACTION",
    "api": "API",
    "command": "COMMAND",
    "event_handler": "EVENT_HANDLER",
    "hook_handler": "HOOK_HANDLER",
    "home_card": "HOME_CARD",
    "message_gateway": "MESSAGE_GATEWAY",
    "tool": "TOOL",
}
_REMOVED_COMPONENT_TYPE_ALIASES = {"WORKFLOW_STEP", "workflow_step"}
CONFIG_RELOAD_SCOPE_SELF = "self"
ON_BOT_CONFIG_RELOAD = "bot"
ON_MODEL_CONFIG_RELOAD = "model"
_CONFIG_RELOAD_SUBSCRIPTION_ALIASES: dict[str, str] = {
    "bot": ON_BOT_CONFIG_RELOAD,
    "model": ON_MODEL_CONFIG_RELOAD,
}


def _normalize_schema_type_name(raw_type: Any) -> str:
    """将任意 Schema 类型值规范化为可读字符串。

    Args:
        raw_type: 原始类型值。

    Returns:
        str: 规范化后的类型名称。
    """

    normalized_type = str(raw_type or "").strip().lower()
    if not normalized_type:
        return "string"
    if normalized_type in {"number", "integer", "boolean", "array", "object"}:
        return normalized_type
    return normalized_type


def build_tool_detailed_description(
    parameters_schema: dict[str, Any] | None,
    fallback_description: str = "",
) -> str:
    """根据对象级参数 Schema 生成工具详细描述。

    Args:
        parameters_schema: 工具参数对象 Schema。
        fallback_description: 无法从 Schema 推导时使用的兜底说明。

    Returns:
        str: 生成后的详细描述文本。
    """

    if not parameters_schema:
        return fallback_description.strip()

    properties = parameters_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return fallback_description.strip()

    required_names = {str(name).strip() for name in parameters_schema.get("required", []) if str(name).strip()}

    lines = ["参数说明："]
    for parameter_name, parameter_schema in properties.items():
        if not isinstance(parameter_schema, dict):
            continue

        normalized_name = str(parameter_name).strip()
        required_text = "必填" if normalized_name in required_names else "可选"
        parameter_type = _normalize_schema_type_name(parameter_schema.get("type"))
        parameter_description = str(parameter_schema.get("description", "") or "").strip() or "无额外说明"
        line = f"- {normalized_name}：{parameter_type}，{required_text}。{parameter_description}"

        enum_values = parameter_schema.get("enum")
        if isinstance(enum_values, list) and enum_values:
            line += f" 可选值：{'、'.join(str(item) for item in enum_values)}。"

        if "default" in parameter_schema:
            line += f" 默认值：{parameter_schema['default']}。"

        lines.append(line)

    if len(lines) == 1:
        return fallback_description.strip()

    if fallback_description.strip():
        lines.append("")
        lines.append(fallback_description.strip())
    return "\n".join(lines).strip()


def normalize_component_type_name(component_type: Any) -> str:
    """将组件类型归一化为协议层使用的大写字符串。

    SDK 2.0 起，组件协议值统一为大写。为降低迁移成本，仍接受大小写不同但
    语义相同的写法，例如 ``action`` / ``ACTION``。

    `WorkflowStep` 已被 `HookHandler` 替代，属于明确的 breaking change，
    因此这里不会再把 ``workflow_step`` 静默映射为 ``HOOK_HANDLER``。
    """

    value = getattr(component_type, "value", component_type)
    normalized_value = str(value or "").strip()
    if not normalized_value:
        raise ValueError("组件类型不能为空")
    if normalized_value in _REMOVED_COMPONENT_TYPE_ALIASES:
        raise ValueError("`WorkflowStep` 已移除，请改用 `HookHandler` / `HOOK_HANDLER`。")
    normalized_name = _COMPONENT_TYPE_ALIASES.get(normalized_value)
    if normalized_name is None:
        raise ValueError(f"不支持的组件类型: {normalized_value}")
    return normalized_name


def normalize_config_reload_subscription(subscription: Any) -> str:
    """将配置热重载订阅值归一化为协议层使用的字符串。

    Args:
        subscription: 插件声明的原始订阅值。

    Returns:
        str: 归一化后的订阅范围，仅支持 ``bot`` 或 ``model``。

    Raises:
        ValueError: 当订阅值不受支持时抛出。
    """

    normalized_value = str(subscription or "").strip().lower()
    subscription_name = _CONFIG_RELOAD_SUBSCRIPTION_ALIASES.get(normalized_value)
    if subscription_name is None:
        raise ValueError(f"不支持的配置热重载订阅: {subscription}")
    return subscription_name


# ─── 枚举类型 ──────────────────────────────────────────────────────


class ComponentType(str, Enum):
    """组件类型"""

    ACTION = "ACTION"
    API = "API"
    COMMAND = "COMMAND"
    TOOL = "TOOL"
    EVENT_HANDLER = "EVENT_HANDLER"
    HOOK_HANDLER = "HOOK_HANDLER"
    HOME_CARD = "HOME_CARD"
    MESSAGE_GATEWAY = "MESSAGE_GATEWAY"

    @classmethod
    def from_value(cls, component_type: Any) -> "ComponentType":
        """从大小写兼容的输入值恢复为 SDK 组件类型枚举。"""

        return cls(normalize_component_type_name(component_type))


class ActivationType(str, Enum):
    """Action 激活类型"""

    NEVER = "never"
    ALWAYS = "always"
    RANDOM = "random"
    KEYWORD = "keyword"


class ChatMode(str, Enum):
    """聊天模式"""

    FOCUS = "focus"
    NORMAL = "normal"
    PRIORITY = "priority"
    ALL = "all"


class EventType(str, Enum):
    """事件类型 — 与旧系统完全对齐"""

    UNKNOWN = "unknown"
    ON_START = "on_start"
    ON_STOP = "on_stop"
    ON_MESSAGE_PRE_PROCESS = "on_message_pre_process"
    ON_MESSAGE = "on_message"
    ON_PLAN = "on_plan"
    POST_LLM = "post_llm"
    AFTER_LLM = "after_llm"
    POST_SEND_PRE_PROCESS = "post_send_pre_process"
    POST_SEND = "post_send"
    AFTER_SEND = "after_send"


class ToolParamType(str, Enum):
    """工具参数类型"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


class MessageGatewayRouteType(str, Enum):
    """消息网关路由类型。"""

    SEND = "send"
    RECEIVE = "receive"
    DUPLEX = "duplex"


class HookMode(str, Enum):
    """Hook 处理模式。"""

    BLOCKING = "blocking"
    OBSERVE = "observe"


class HookOrder(str, Enum):
    """Hook 在同一模式内的顺序槽位。"""

    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"


class ModifyFlag(str, Enum):
    """消息可修改标志"""

    CAN_MODIFY_PROMPT = "can_modify_prompt"
    CAN_MODIFY_RESPONSE = "can_modify_response"
    CAN_MODIFY_MESSAGE = "can_modify_message"


class ErrorPolicy(str, Enum):
    """Hook 异常处理策略"""

    ABORT = "abort"  # 异常时终止当前 hook 调用
    SKIP = "skip"  # 记录日志，跳过此 hook 继续
    LOG = "log"  # 记录日志，并继续执行后续 hook


# ─── 工具参数定义 ──────────────────────────────────────────────────


class ToolParameterInfo(BaseModel):
    """工具参数定义"""

    name: str = Field(description="参数名称")
    param_type: ToolParamType = Field(default=ToolParamType.STRING, description="参数类型")
    description: str = Field(default="", description="参数描述")
    required: bool = Field(default=True, description="是否必需")
    enum_values: list[Any] | None = Field(default=None, description="可选枚举值列表")
    items_schema: dict[str, Any] | None = Field(default=None, description="数组元素 Schema")
    properties: dict[str, dict[str, Any]] | None = Field(default=None, description="对象属性定义")
    required_properties: list[str] = Field(default_factory=list, description="对象内部必填字段")
    additional_properties: bool | dict[str, Any] | None = Field(default=None, description="是否允许额外字段")
    default: Any = Field(default=None, description="默认值")

    def to_parameter_schema(self) -> dict[str, Any]:
        """将 SDK 工具参数定义转换为 JSON Schema 属性。

        Returns:
            dict[str, Any]: 参数对应的 Schema 片段。
        """
        schema: dict[str, Any] = {
            "type": self._get_json_schema_type(),
            "description": self.description,
        }
        if self.enum_values:
            schema["enum"] = list(self.enum_values)
        if self.default is not None:
            schema["default"] = deepcopy(self.default)
        if self.param_type == ToolParamType.ARRAY and self.items_schema is not None:
            schema["items"] = deepcopy(self.items_schema)
        if self.param_type == ToolParamType.OBJECT:
            schema["properties"] = deepcopy(self.properties or {})
            if self.required_properties:
                schema["required"] = list(self.required_properties)
            if self.additional_properties is not None:
                schema["additionalProperties"] = deepcopy(self.additional_properties)
        return schema

    def _get_json_schema_type(self) -> str:
        """获取参数对应的 JSON Schema 类型名。

        Returns:
            str: JSON Schema 类型名。
        """
        if self.param_type in {ToolParamType.FLOAT, ToolParamType.NUMBER}:
            return "number"
        return self.param_type.value


# ─── 组件信息 ──────────────────────────────────────────────────────


class ComponentInfo(BaseModel):
    """组件信息（SDK 侧声明）"""

    name: str = Field(description="组件名称")
    type: ComponentType = Field(description="组件类型")
    description: str = Field(default="", description="组件描述")
    enabled: bool = Field(default=True, description="组件是否启用")
    metadata: dict[str, Any] = Field(default_factory=dict, description="组件元数据")


class ActionComponentInfo(ComponentInfo):
    """Action 组件信息"""

    type: ComponentType = ComponentType.ACTION
    activation_type: ActivationType = ActivationType.ALWAYS
    activation_keywords: list[str] = Field(default_factory=list)
    activation_probability: float = 1.0
    chat_mode: ChatMode = ChatMode.NORMAL
    action_parameters: dict[str, Any] = Field(default_factory=dict, description="Action 参数定义")
    action_require: list[str] = Field(default_factory=list, description="Action 前置需求")
    associated_types: list[str] = Field(default_factory=list, description="关联消息类型")
    parallel_action: bool = Field(default=False, description="是否可并行执行")
    action_prompt: str = Field(default="", description="Action 的 LLM 提示语")


class APIComponentInfo(ComponentInfo):
    """API 组件信息"""

    type: ComponentType = ComponentType.API
    version: str = Field(default="1", description="API 版本")
    public: bool = Field(default=False, description="是否允许其他插件调用")


class CommandComponentInfo(ComponentInfo):
    """Command 组件信息"""

    type: ComponentType = ComponentType.COMMAND
    command_pattern: str = Field(default="", description="命令正则模式")
    aliases: list[str] = Field(default_factory=list, description="命令别名")


class ToolComponentInfo(ComponentInfo):
    """Tool 组件信息"""

    type: ComponentType = ComponentType.TOOL
    brief_description: str = Field(default="", description="工具简要描述")
    detailed_description: str = Field(default="", description="工具详细描述")
    parameters: list[ToolParameterInfo] = Field(default_factory=list, description="结构化参数定义")
    parameters_raw: dict[str, Any] = Field(default_factory=dict, description="原始参数 schema（兼容 dict 声明）")
    invoke_method: str = Field(default="plugin.invoke_tool", description="调用当前工具时使用的 RPC 方法")

    def get_brief_description(self) -> str:
        """获取工具的简要描述。

        Returns:
            str: 归一化后的简要描述。
        """

        return str(self.brief_description or self.description or f"工具 {self.name}").strip()

    def get_parameters_schema(self) -> dict[str, Any] | None:
        """获取对象级工具参数 Schema。

        Returns:
            dict[str, Any] | None: 对象级参数 Schema；无参数时返回 `None`。
        """
        if self.parameters_raw:
            if self.parameters_raw.get("type") == "object" or "properties" in self.parameters_raw:
                return deepcopy(self.parameters_raw)
            required_names: list[str] = []
            normalized_properties: dict[str, Any] = {}
            for property_name, property_schema in self.parameters_raw.items():
                if not isinstance(property_schema, dict):
                    continue
                property_schema_copy = deepcopy(property_schema)
                if bool(property_schema_copy.pop("required", False)):
                    required_names.append(str(property_name))
                normalized_properties[str(property_name)] = property_schema_copy
            raw_schema: dict[str, Any] = {
                "type": "object",
                "properties": normalized_properties,
            }
            if required_names:
                raw_schema["required"] = required_names
            return raw_schema
        if not self.parameters:
            return None
        properties = {parameter.name: parameter.to_parameter_schema() for parameter in self.parameters}
        required_names = [parameter.name for parameter in self.parameters if parameter.required]
        tool_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required_names:
            tool_schema["required"] = required_names
        return tool_schema

    def get_detailed_description(self) -> str:
        """获取工具的详细描述。

        Returns:
            str: 包含参数说明的详细描述。
        """

        return build_tool_detailed_description(
            self.get_parameters_schema(),
            fallback_description=str(self.detailed_description or "").strip(),
        )


class EventHandlerComponentInfo(ComponentInfo):
    """EventHandler 组件信息"""

    type: ComponentType = ComponentType.EVENT_HANDLER
    event_type: EventType = EventType.ON_MESSAGE
    intercept_message: bool = Field(default=False, description="是否阻塞消息链")
    weight: int = Field(default=0, description="权重/优先级（越高越先执行）")


class HookHandlerComponentInfo(ComponentInfo):
    """HookHandler 组件信息"""

    type: ComponentType = ComponentType.HOOK_HANDLER
    hook: str = Field(description="订阅的命名 Hook 名称")
    mode: HookMode = Field(default=HookMode.BLOCKING, description="处理模式：blocking 或 observe")
    order: HookOrder = Field(default=HookOrder.NORMAL, description="同一模式内的顺序槽位")
    timeout_ms: int = Field(default=0, description="处理器超时(ms)，0 表示使用 Hook 默认值")
    error_policy: ErrorPolicy = Field(default=ErrorPolicy.SKIP, description="异常处理策略")


class MessageGatewayComponentInfo(ComponentInfo):
    """消息网关组件信息。"""

    type: ComponentType = ComponentType.MESSAGE_GATEWAY
    route_type: MessageGatewayRouteType = Field(description="消息网关路由类型")
    platform: str = Field(default="", description="可选的平台名称，允许在运行时动态上报")
    protocol: str = Field(default="", description="可选的协议或接入方言名称")
    account_id: str = Field(default="", description="可选的账号 ID 或 self_id")
    scope: str = Field(default="", description="可选的路由作用域")


class HomeCardComponentInfo(ComponentInfo):
    """WebUI 首页卡片组件信息。"""

    type: ComponentType = ComponentType.HOME_CARD
    title: str = Field(description="卡片标题")
    content: Any = Field(default="", description="卡片内容，建议使用 Markdown 字符串或结构化内容块")
    link_url: str = Field(default="", description="点击跳转地址，可为 WebUI 内部路径或 http(s) 外链")
    link_label: str = Field(default="", description="跳转按钮文案")
    icon: str = Field(default="", description="可选图标名")
    width: str = Field(default="medium", description="卡片宽度：small/medium/large/wide/full")
    order: int = Field(default=1000, description="默认排序值，越小越靠前")


class LLMProviderInfo(BaseModel):
    """LLM Provider 声明信息。"""

    client_type: str = Field(description="客户端类型标识，对应模型配置中的 api_providers[].client_type")
    name: str = Field(default="", description="Provider 展示名称")
    description: str = Field(default="", description="Provider 描述")
    version: str = Field(default="1.0.0", description="Provider 实现版本")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Provider 元数据")


# ─── 通用响应 ──────────────────────────────────────────────────────


class CapabilityResult(BaseModel):
    """能力调用结果"""

    success: bool
    result: Any = None
    error: str = ""
