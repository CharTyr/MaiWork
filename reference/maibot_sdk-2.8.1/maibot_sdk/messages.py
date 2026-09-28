"""MaiMessages — 统一消息格式

跨组件消息传递的标准模型。
与旧系统的 MaiMessages 对齐，但使用 Pydantic 重写。
"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field

from maibot_sdk.types import ModifyFlag


class MessageSegment(BaseModel):
    """消息段"""

    type: str = Field(default="text", description="段类型: text, image, at, reply, ...")
    data: dict[str, Any] = Field(default_factory=dict, description="段数据")


class MaiMessages(BaseModel):
    """统一消息格式

    用于 EventHandler/Workflow/Action 之间传递消息。
    保持与旧系统字段兼容。
    """

    # 消息内容
    message_segments: list[MessageSegment] = Field(default_factory=list, description="消息段列表")
    plain_text: str = Field(default="", description="处理后纯文本")

    # LLM 相关
    llm_prompt: str | None = Field(default=None, description="LLM 输入 prompt")
    llm_response_content: str | None = Field(default=None, description="LLM 回复内容")
    llm_response_reasoning: str | None = Field(default=None, description="LLM 推理内容")
    llm_response_model: str | None = Field(default=None, description="使用的 LLM 模型")
    llm_response_tool_call: list[dict[str, Any]] | None = Field(default=None, description="LLM 工具调用")

    # 上下文信息
    stream_id: str | None = Field(default=None, description="聊天流 ID")
    is_group_message: bool = Field(default=False, description="是否群聊消息")
    is_private_message: bool = Field(default=False, description="是否私聊消息")
    message_base_info: dict[str, Any] = Field(default_factory=dict, description="消息基本信息")
    raw_message: Any | None = Field(default=None, description="原始消息对象")

    # Action 信息
    action_usage: list[str] | None = Field(default=None, description="已使用的 Action 列表")

    # 附加数据
    additional_data: dict[str, Any] = Field(default_factory=dict, description="附加数据")

    # 修改权限标志
    modify_flags: dict[str, bool] = Field(
        default_factory=lambda: {
            ModifyFlag.CAN_MODIFY_PROMPT.value: True,
            ModifyFlag.CAN_MODIFY_RESPONSE.value: True,
            ModifyFlag.CAN_MODIFY_MESSAGE.value: True,
        },
        description="修改权限标志",
    )

    def deepcopy(self) -> "MaiMessages":
        """深拷贝当前消息"""
        return deepcopy(self)

    def can_modify(self, flag: ModifyFlag) -> bool:
        """检查是否有修改权限"""
        return self.modify_flags.get(flag, False)

    def set_modify_flag(self, flag: ModifyFlag, value: bool) -> None:
        """设置修改权限标志"""
        self.modify_flags[flag] = value

    def modify_prompt(self, new_prompt: str) -> bool:
        """安全修改 LLM prompt"""
        if not self.can_modify(ModifyFlag.CAN_MODIFY_PROMPT):
            return False
        self.llm_prompt = new_prompt
        return True

    def modify_response(self, new_content: str) -> bool:
        """安全修改 LLM response"""
        if not self.can_modify(ModifyFlag.CAN_MODIFY_RESPONSE):
            return False
        self.llm_response_content = new_content
        return True

    def modify_plain_text(self, new_text: str) -> bool:
        """安全修改纯文本"""
        if not self.can_modify(ModifyFlag.CAN_MODIFY_MESSAGE):
            return False
        self.plain_text = new_text
        return True

    def to_rpc_dict(self) -> dict[str, Any]:
        """序列化为 RPC 传输用 dict"""
        return self.model_dump(mode="json")

    @classmethod
    def from_rpc_dict(cls, data: dict[str, Any]) -> "MaiMessages":
        """从 RPC dict 反序列化"""
        return cls.model_validate(data)
