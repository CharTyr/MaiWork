"""构想可行性闸认「生图」本事（2026-10-15 任务 A4）：

- 子 agent 工具名单里有 generate_image → 能力清单多一项 image（能画照片 / 插画 / 封面 / 概念图）；
- 没有 → 清单里没有 image，模型写 uses=["image"] 过不了闸（照旧 fail-closed）；
- 做不到清单：图片不再一律说「做不到」，改成「视频 / 音频做不到；图片要有 image 本事」；
- 头注释同步（不在本文件断言，靠 review）。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork import idea_feasibility as fz

WORKER_TOOLS_WITH_IMAGE = [
    ("web_search", "搜"), ("fetch_page", "打开网页"), ("write_file", "写文件"),
    ("generate_image", "AI 生图"),
]
WORKER_TOOLS_WITHOUT_IMAGE = [
    ("web_search", "搜"), ("fetch_page", "打开网页"), ("write_file", "写文件"),
]


def test_inventory_has_image_capability_when_tool_present() -> None:
    inv = fz.inventory(WORKER_TOOLS_WITH_IMAGE)
    assert "image" in inv["caps"]
    assert "generate_image" in inv["tools"]


def test_inventory_has_no_image_capability_without_tool() -> None:
    inv = fz.inventory(WORKER_TOOLS_WITHOUT_IMAGE)
    assert "image" not in inv["caps"]


def test_prompt_section_describes_image_capability() -> None:
    text = fz.prompt_section(fz.inventory(WORKER_TOOLS_WITH_IMAGE))
    assert "image" in text
    assert "生图" in text or "画" in text


def test_cannot_no_longer_claims_pictures_are_impossible() -> None:
    """图片不再是「一律做不到」：那句话只说视频 / 音频。"""
    joined = "\n".join(fz.CANNOT)
    assert "生成图片 / 视频 / 音频" not in joined
    assert "视频" in joined and "音频" in joined


def test_prompt_section_says_video_audio_impossible() -> None:
    text = fz.prompt_section(fz.inventory(WORKER_TOOLS_WITH_IMAGE))
    assert "视频" in text and "音频" in text


def test_check_accepts_image_use_when_tool_present() -> None:
    inv = fz.inventory(WORKER_TOOLS_WITH_IMAGE)
    ok, reason, norm = fz.check(
        {"level": "ok", "note": "能画", "uses": ["image"], "deliver": "page",
         "needs_members": False},
        inv,
    )
    assert ok is True, reason
    assert norm["uses"] == ["image"]


def test_check_rejects_image_use_without_tool() -> None:
    inv = fz.inventory(WORKER_TOOLS_WITHOUT_IMAGE)
    ok, reason, _norm = fz.check(
        {"level": "ok", "note": "能画", "uses": ["image"], "deliver": "page",
         "needs_members": False},
        inv,
    )
    assert ok is False
    assert "image" in reason
