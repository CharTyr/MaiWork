"""LLM 能力代理

对应旧系统的 llm_api，所有方法底层转发为 cap.call RPC。
"""
# ruff: noqa: I001,UP006,UP035

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

import base64

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


def _normalize_llm_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """统一补齐兼容字段。

    Args:
        result: 原始能力返回结果。

    Returns:
        Dict[str, Any]: 规范化后的结果字典。
    """
    if "model" not in result and "model_name" in result:
        result["model"] = result["model_name"]
    return result


class LLMCapability:
    """LLM 调用能力。"""

    def __init__(self, ctx: PluginContext):
        """初始化 LLM 能力代理。

        Args:
            ctx: 当前插件上下文。
        """
        self._ctx = ctx

    async def generate(
        self,
        prompt: str | List[Dict[str, Any]],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        *,
        task_name: str = "utils",
        model_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """生成文本。

        Args:
            prompt: 提示文本或消息列表。
            model: 具体模型名称；空字符串表示使用任务的模型选择策略。
            temperature: 温度参数；省略时使用 Host 模型配置。
            max_tokens: 最大 token 数；省略时使用 Host 模型配置。
            task_name: Host 模型任务名，默认使用 ``utils``。
            model_name: 具体模型名称，``model`` 的明确别名。

        Returns:
            Dict[str, Any]: 统一的 LLM 响应字典。
        """
        payload: Dict[str, Any] = dict(kwargs)
        payload.update(
            prompt=prompt,
            model=model,
            task_name=task_name,
        )
        if model_name:
            payload["model_name"] = model_name
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        result = await self._ctx.call_capability("llm.generate", **payload)
        if isinstance(result, dict):
            return _normalize_llm_result(result)
        return {"success": False, "response": "", "reasoning": "", "model": ""}

    async def generate_with_tools(
        self,
        prompt: str | List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        *,
        task_name: str = "utils",
        model_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """执行带工具调用的文本生成。

        Args:
            prompt: 提示文本或消息列表。
            tools: 工具定义列表。
            model: 具体模型名称；空字符串表示使用任务的模型选择策略。
            temperature: 温度参数；省略时使用 Host 模型配置。
            max_tokens: 最大 token 数；省略时使用 Host 模型配置。
            task_name: Host 模型任务名，默认使用 ``utils``。
            model_name: 具体模型名称，``model`` 的明确别名。

        Returns:
            Dict[str, Any]: 统一的 LLM 响应字典。
        """
        payload: Dict[str, Any] = dict(kwargs)
        payload.update(
            prompt=prompt,
            tools=tools,
            model=model,
            task_name=task_name,
        )
        if model_name:
            payload["model_name"] = model_name
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        result = await self._ctx.call_capability("llm.generate_with_tools", **payload)
        if isinstance(result, dict):
            return _normalize_llm_result(result)
        return {"success": False, "response": "", "reasoning": "", "model": "", "tool_calls": []}

    async def embed(
        self,
        text: str | None = None,
        *,
        texts: List[str] | None = None,
        task_name: str = "embedding",
        model: str = "",
        model_name: str = "",
        max_concurrent: int | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """生成文本嵌入向量。

        Args:
            text: 单条文本。
            texts: 多条文本。``text`` 与 ``texts`` 只能传入一个。
            task_name: 模型任务名，默认使用 ``embedding``。
            model: 兼容 Host 的模型任务别名。
            model_name: 兼容 Host 的模型任务别名。
            max_concurrent: 批量嵌入时的最大并发数。
            **kwargs: Host 支持的额外参数。

        Returns:
            Dict[str, Any]: Host 返回的嵌入结果。单条文本通常包含 ``embedding``；
            批量文本通常包含 ``results``。
        """

        result = await self._ctx.call_capability(
            "llm.embed",
            text=text,
            texts=texts,
            task_name=task_name,
            model=model,
            model_name=model_name,
            max_concurrent=max_concurrent,
            **kwargs,
        )
        if isinstance(result, dict):
            return result
        return {}

    async def transcribe_audio(
        self,
        audio: bytes | str | None = None,
        *,
        audio_base64: str = "",
        voice_base64: str = "",
        task_name: str = "voice",
        model: str = "",
        model_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """调用 Host 的 ASR 语音识别模型。

        Args:
            audio: 音频字节，或已编码的 Base64/Data URL 字符串。
            audio_base64: 已编码的音频 Base64。
            voice_base64: ``audio_base64`` 的兼容别名。
            task_name: 模型任务名，默认使用 Host 的 ``voice`` 任务。
            model: 兼容 Host 的模型任务别名。
            model_name: 兼容 Host 的模型任务别名。
            **kwargs: Host 支持的额外参数。

        Returns:
            Dict[str, Any]: Host 返回的识别结果，通常包含 ``success``、``text`` 和 ``content``。
        """

        payload: Dict[str, Any] = dict(kwargs)
        if isinstance(audio, bytes):
            payload["audio_base64"] = base64.b64encode(audio).decode("utf-8")
        elif isinstance(audio, str) and audio.strip():
            payload["audio_base64"] = audio.strip()
        elif audio_base64:
            payload["audio_base64"] = audio_base64
        elif voice_base64:
            payload["voice_base64"] = voice_base64

        payload.update(
            task_name=task_name,
            model=model,
            model_name=model_name,
        )
        result = await self._ctx.call_capability("llm.transcribe_audio", **payload)
        if isinstance(result, dict):
            return result
        return {}

    async def get_available_models(self) -> List[str]:
        """获取可用模型列表。

        Returns:
            List[str]: 当前宿主可见的模型任务名列表。
        """
        result = await self._ctx.call_capability("llm.get_available_models")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            models = result.get("models")
            if isinstance(models, list):
                return models
        return []
