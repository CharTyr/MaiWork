"""生图适配层（maiwork/imagegen.py）纯函数测试：请求形状 / 响应形状 / 错误文案。

先写测试再写实现（本文件在 imagegen.py 之前提交时必须是红的）。
不连真网络：这里只测纯函数，下载 / 发请求的路径在 test_imagegen_tool.py 用
httpx.MockTransport 打桩。
"""

from __future__ import annotations

import base64
import json

import pytest

from CharTyr_MaiWork.maiwork.imagegen import (
    ImageGenError,
    build_request,
    image_ext,
    parse_response,
)

KEY = "sk-生图密钥-不要泄漏-1234"


class TestBuildRequestOpenAI:
    def test_url_path_and_body(self) -> None:
        url, headers, body = build_request(
            "openai", "https://api.openai.com/v1/", KEY, "gpt-image-1",
            "一只戴宇航头盔的红熊猫", size="1024x1024", n=2,
        )
        assert url == "https://api.openai.com/v1/images/generations"
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert body["model"] == "gpt-image-1"
        assert body["prompt"] == "一只戴宇航头盔的红熊猫"
        assert body["size"] == "1024x1024"
        assert body["n"] == 2
        # 密钥只在请求头里，绝不进 body / URL
        blob = json.dumps(body, ensure_ascii=False) + url
        assert KEY not in blob and "api_key" not in blob

    def test_size_omitted_when_empty(self) -> None:
        _, _, body = build_request("openai", "https://a.test/v1", KEY, "m", "画只猫")
        assert "size" not in body
        assert body["n"] == 1

    def test_no_response_format_gpt_image_rejects_it(self) -> None:
        """GPT image 系不支持 response_format，发了会 400：我们不主动发。"""
        _, _, body = build_request("openai", "https://a.test/v1", KEY, "gpt-image-1", "画只猫")
        assert "response_format" not in body

    def test_ark_shaped_same_as_openai(self) -> None:
        url, _, body = build_request(
            "openai", "https://ark.cn-beijing.volces.com/api/v3", KEY,
            "doubao-seedream-4-0-250828", "特写肖像", size="2K",
        )
        assert url == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
        assert body["size"] == "2K"

    def test_endpoint_headers_override_bearer(self) -> None:
        """端点高级设置里的同名请求头（大小写不敏感）替换默认头，不是追加。"""
        _, headers, _ = build_request(
            "openai", "https://a.test/v1", KEY, "m", "画只猫",
            extra_headers={"authorization": "Custom xyz"},
        )
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["authorization"] == "Custom xyz"
        assert list(headers).count("Authorization") + list(headers).count("authorization") == 1


class TestBuildRequestSiliconFlow:
    def test_uses_image_size_and_batch_size(self) -> None:
        url, headers, body = build_request(
            "siliconflow", "https://api.siliconflow.cn/v1", KEY, "Kwai-Kolors/Kolors",
            "海岛与海鸥", size="1024x1024", n=3,
        )
        assert url == "https://api.siliconflow.cn/v1/images/generations"
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert body["image_size"] == "1024x1024"
        assert body["batch_size"] == 3
        assert "size" not in body and "n" not in body

    def test_omits_size_when_empty(self) -> None:
        _, _, body = build_request("siliconflow", "https://api.siliconflow.cn/v1", KEY, "m", "画只猫")
        assert "image_size" not in body
        assert body["batch_size"] == 1


def test_unknown_api_rejected() -> None:
    with pytest.raises(ImageGenError) as e:
        build_request("gemini", "https://a.test/v1", KEY, "m", "画只猫")
    assert "gemini" in str(e.value)


class TestParseResponse:
    def test_openai_b64(self) -> None:
        out = parse_response("openai", {"data": [{"b64_json": "aGVsbG8="}]})
        assert out == [{"b64": "aGVsbG8="}]

    def test_openai_url(self) -> None:
        out = parse_response("openai", {"data": [{"url": "https://img.test/a.png"}]})
        assert out == [{"url": "https://img.test/a.png"}]

    def test_openai_mixed_prefers_b64_per_item(self) -> None:
        out = parse_response(
            "openai", {"data": [{"b64_json": "aGk="}, {"url": "https://img.test/b.png"}]}
        )
        assert out == [{"b64": "aGk="}, {"url": "https://img.test/b.png"}]

    def test_ark_url(self) -> None:
        out = parse_response("openai", {"data": [{"url": "https://ark.test/x.jpeg", "size": "2048x2048"}]})
        assert out == [{"url": "https://ark.test/x.jpeg"}]

    def test_siliconflow_images_not_data(self) -> None:
        out = parse_response("siliconflow", {"images": [{"url": "https://sf.test/a.png"}]})
        assert out == [{"url": "https://sf.test/a.png"}]

    def test_siliconflow_tolerates_data_key(self) -> None:
        """有些中转把 images[] 写成 data[]：能读就读，别整次失败。"""
        out = parse_response("siliconflow", {"data": [{"url": "https://sf.test/a.png"}]})
        assert out == [{"url": "https://sf.test/a.png"}]

    def test_nothing_useful_raises_without_key(self) -> None:
        with pytest.raises(ImageGenError) as e:
            parse_response("openai", {"data": [{"revised_prompt": "…"}]})
        assert "图片" in str(e.value)
        assert KEY not in str(e.value)

    def test_empty_payload_raises(self) -> None:
        with pytest.raises(ImageGenError):
            parse_response("openai", {})

    def test_error_item_is_skipped_not_fatal(self) -> None:
        """组图里失败项带 error：跳过它，只要还有成功的就不算整次失败。"""
        out = parse_response(
            "openai", {"data": [{"error": {"message": "x"}}, {"b64_json": "aGk="}]}
        )
        assert out == [{"b64": "aGk="}]


class TestImageExt:
    def test_png(self) -> None:
        assert image_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "png"

    def test_jpeg(self) -> None:
        assert image_ext(b"\xff\xd8\xff\xe0" + b"0" * 8) == "jpg"

    def test_webp(self) -> None:
        assert image_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"

    def test_gif(self) -> None:
        assert image_ext(b"GIF89a" + b"0" * 8) == "gif"

    def test_not_an_image(self) -> None:
        assert image_ext(b"<html>nope</html>") == ""
        assert image_ext(b"") == ""


def test_b64_decode_helper_roundtrip() -> None:
    from CharTyr_MaiWork.maiwork.imagegen import decode_b64

    raw = b"\x89PNG\r\n\x1a\nhello"
    assert decode_b64(base64.b64encode(raw).decode()) == raw


def test_bad_b64_raises_chinese_error() -> None:
    from CharTyr_MaiWork.maiwork.imagegen import decode_b64

    with pytest.raises(ImageGenError) as e:
        decode_b64("这不是 base64!!")
    assert "图片" in str(e.value)
