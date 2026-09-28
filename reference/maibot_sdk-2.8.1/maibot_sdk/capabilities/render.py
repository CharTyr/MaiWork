"""浏览器渲染能力代理。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


class RenderCapability:
    """浏览器渲染能力。"""

    def __init__(self, ctx: PluginContext) -> None:
        """初始化浏览器渲染能力代理。

        Args:
            ctx: 当前插件的上下文对象。
        """

        self._ctx = ctx

    async def html2png(
        self,
        html: str,
        *,
        selector: str = "body",
        viewport: dict[str, int] | None = None,
        device_scale_factor: float = 2.0,
        full_page: bool = False,
        omit_background: bool = False,
        wait_until: str = "load",
        wait_for_selector: str = "",
        wait_for_timeout_ms: int = 0,
        render_timeout_ms: int = 0,
        allow_network: bool = False,
    ) -> Any:
        """将 HTML 内容渲染为 PNG 图片。

        Args:
            html: 待渲染的 HTML 字符串。
            selector: 需要截图的目标选择器，默认截取 ``body``。
            viewport: 视口大小，格式为 ``{\"width\": int, \"height\": int}``。
            device_scale_factor: 设备像素比。
            full_page: 当 ``selector`` 为 ``body`` 时，是否截取整页。
            omit_background: 是否去掉默认白色背景。
            wait_until: HTML 写入页面后的等待阶段。
            wait_for_selector: 渲染完成后额外等待出现的选择器。
            wait_for_timeout_ms: 额外静态等待时长（毫秒）。
            render_timeout_ms: 本次渲染超时时长（毫秒）。
            allow_network: 是否允许页面访问外部网络资源。

        Returns:
            Any: 渲染结果，通常包含 ``image_base64``、``mime_type``、``width`` 和 ``height``。
        """

        capability = "render.html2png"
        result = await self._ctx.call_host_method(
            "cap.call",
            payload={
                "capability": capability,
                "args": {
                    "html": html,
                    "selector": selector,
                    "viewport": viewport or {},
                    "device_scale_factor": device_scale_factor,
                    "full_page": full_page,
                    "omit_background": omit_background,
                    "wait_until": wait_until,
                    "wait_for_selector": wait_for_selector,
                    "wait_for_timeout_ms": wait_for_timeout_ms,
                    "render_timeout_ms": render_timeout_ms,
                    "allow_network": allow_network,
                },
            },
        )
        return self._ctx._normalize_capability_result(capability, result)
