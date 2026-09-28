"""LLM Provider 插件基类。

第三方插件可以通过继承该基类实现具体 Provider，再在插件方法上使用
``@LLMProvider`` 装饰器把请求转发给 Provider 实例。
"""

from abc import ABC, abstractmethod
from typing import Any


class LLMProviderBase(ABC):
    """第三方 LLM Provider 的推荐基类。"""

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        """按操作类型分发 LLM 请求。

        Args:
            operation: 请求操作类型。
            request: Host 侧序列化后的 LLM 请求。

        Returns:
            dict[str, Any]: 可由 Host 恢复为 APIResponse 的响应字典。

        Raises:
            ValueError: 当操作类型不受支持时抛出。
        """
        if operation == "response":
            return await self.get_response(request)
        if operation == "embedding":
            return await self.get_embedding(request)
        if operation == "audio_transcription":
            return await self.get_audio_transcriptions(request)
        raise ValueError(f"不支持的 LLM Provider 操作类型: {operation}")

    @abstractmethod
    async def get_response(self, request: dict[str, Any]) -> dict[str, Any]:
        """生成文本或多模态响应。

        Args:
            request: Host 侧序列化后的响应请求。

        Returns:
            dict[str, Any]: 响应字典，至少可包含 ``content``、``reasoning_content``、
            ``tool_calls``、``usage`` 等字段。
        """
        raise NotImplementedError

    async def get_embedding(self, request: dict[str, Any]) -> dict[str, Any]:
        """生成文本嵌入。

        Args:
            request: Host 侧序列化后的嵌入请求。

        Returns:
            dict[str, Any]: 响应字典，需包含 ``embedding`` 字段。

        Raises:
            NotImplementedError: 子类未实现嵌入能力时抛出。
        """
        raise NotImplementedError("当前 LLM Provider 未实现 embedding")

    async def get_audio_transcriptions(self, request: dict[str, Any]) -> dict[str, Any]:
        """生成音频转写。

        Args:
            request: Host 侧序列化后的音频转写请求。

        Returns:
            dict[str, Any]: 响应字典，需包含 ``content`` 字段。

        Raises:
            NotImplementedError: 子类未实现音频转写能力时抛出。
        """
        raise NotImplementedError("当前 LLM Provider 未实现 audio_transcription")
