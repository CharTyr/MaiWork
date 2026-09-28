"""MaiBot Plugin SDK

插件开发的唯一依赖入口。插件不得导入 src.*，只能通过本 SDK 获取能力。

核心导出：
- MaiBotPlugin: 插件基类
- Tool, API, Command, EventHandler, HookHandler, MessageGateway, HomeCard: 组件声明装饰器
- Action: 兼容旧插件的装饰器别名，内部会自动转换为 Tool 声明
- PluginContext / PluginPaths: 插件运行时上下文与标准路径
"""

from .capabilities.send import SendResult
from .components import (
    API,
    Action,
    Command,
    EventHandler,
    HomeCard,
    HookHandler,
    LLMProvider,
    MessageGateway,
    Tool,
    WorkflowStep,
)
from .config import Field, PluginConfigBase
from .context import PluginContext, PluginPaths
from .llm_provider import LLMProviderBase
from .plugin import MaiBotPlugin
from .types import CONFIG_RELOAD_SCOPE_SELF, ON_BOT_CONFIG_RELOAD, ON_MODEL_CONFIG_RELOAD

__version__ = "2.8.1"

__all__ = [
    "MaiBotPlugin",
    "API",
    "Action",
    "Command",
    "Tool",
    "EventHandler",
    "HookHandler",
    "HomeCard",
    "LLMProvider",
    "LLMProviderBase",
    "MessageGateway",
    "WorkflowStep",
    "PluginConfigBase",
    "Field",
    "PluginContext",
    "PluginPaths",
    "SendResult",
    "CONFIG_RELOAD_SCOPE_SELF",
    "ON_BOT_CONFIG_RELOAD",
    "ON_MODEL_CONFIG_RELOAD",
]
