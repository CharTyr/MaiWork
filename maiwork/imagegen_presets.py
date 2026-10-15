"""生图服务预设（2026-10-15 生图第一期）：几家「OpenAI 兼容形状」的生图服务定死知识。

解决的问题：模型这边本来没有服务商预设（只有协议三选一，地址和模型名全靠手填），
生图模型名（`gpt-image-1` / `doubao-seedream-4-0-250828` / `Kwai-Kolors/Kolors`）比聊天
模型更难猜，所以这里把「地址 + 默认模型 + 接口形状 + 取密钥页」集中起来，网页「模型」
页在用途选「生图」时一键填入。

**只收 OpenAI 兼容形状的**（research/imagegen.md §2.5）：`POST {base_url}/images/generations`，
一次请求拿结果。异步两段式（阿里百炼 DashScope 通义万相）和 Gemini 的 parts 形状这一期
不做，所以这里也不列。

字段含义见 `Preset`；`logo` 只是文件名（本期网页不用图，留字段给后续）。
数据来源：各家官方文档（2026-10-15 抓取，见 research/imagegen.md §2）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    """一家生图服务的定死知识（不含任何密钥）。"""

    id: str                        # "openai" 等
    label: str                     # 显示名
    image_api: str                 # openai / siliconflow（见 config.IMAGE_APIS）
    protocol: str                  # 端点协议：这几家都是 openai
    base_url: str                  # 端点地址（填进 [[endpoints]].base_url）
    model: str                     # 默认模型名（填进 [[model_list]].model）
    models: tuple[str, ...]        # 可选模型名（网页下拉用）
    key_url: str                   # 去哪拿密钥
    docs_url: str                  # 文档
    note: str                      # 一句话（免费额度 / 注意点）


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            id="openai",
            label="OpenAI",
            image_api="openai",
            protocol="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-image-1",
            models=("gpt-image-1", "gpt-image-1-mini", "dall-e-3"),
            key_url="https://platform.openai.com/api-keys",
            docs_url="https://platform.openai.com/docs/guides/image-generation",
            note="按张计费，没有免费额度。",
        ),
        Preset(
            id="ark",
            label="火山方舟（豆包 Seedream）",
            image_api="openai",
            protocol="openai",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seedream-4-0-250828",
            models=("doubao-seedream-4-0-250828", "doubao-seedream-4-5-251128"),
            key_url="https://console.volcengine.com/ark",
            docs_url="https://www.volcengine.com/docs/82379",
            note="有试用额度；尺寸可以填档位（1K / 2K / 4K）或具体像素（2048x2048）。",
        ),
        Preset(
            id="siliconflow",
            label="硅基流动",
            image_api="siliconflow",
            protocol="openai",
            base_url="https://api.siliconflow.cn/v1",
            model="Kwai-Kolors/Kolors",
            models=("Kwai-Kolors/Kolors",),
            key_url="https://cloud.siliconflow.cn/account/ak",
            docs_url="https://docs.siliconflow.com/cn/api-reference/images/images-generations",
            note="有免费额度；生成的图片链接有效期 1 小时。",
        ),
    )
}

# 给网页 / 文档用的一句话总览
PRESET_IDS: tuple[str, ...] = tuple(PRESETS.keys())


def public_list() -> list[dict]:
    """给网页的预设清单（**绝不含密钥**；密钥只由管理员自己填进端点）。"""
    out: list[dict] = []
    for p in PRESETS.values():
        out.append({
            "id": p.id, "label": p.label, "image_api": p.image_api, "protocol": p.protocol,
            "base_url": p.base_url, "model": p.model, "models": list(p.models),
            "key_url": p.key_url, "docs_url": p.docs_url, "note": p.note,
        })
    return out
