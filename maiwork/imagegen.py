"""生图适配层（2026-10-15 生图第一期）：把「一次生图请求」拼出来、把结果读出来。

第一期只做**同步、一次拿结果**的两家（都是 `POST {base_url}/images/generations`）：

| image_api | 谁 | 请求字段 | 结果字段 |
|---|---|---|---|
| `openai` | OpenAI `gpt-image-1`、火山方舟 Seedream | `model` / `prompt` / `size` / `n` | `data[].b64_json` 或 `data[].url` |
| `siliconflow` | 硅基流动 | `model` / `prompt` / `image_size` / `batch_size` | `images[].url`（URL 只活 1 小时） |

异步两段式（阿里百炼 DashScope 通义万相）和完全不同的形状（Gemini 的 parts）**这一期不做**，
见 `.trellis/tasks/10-15-viz-component-ui/research/imagegen.md` §3.6 的分期。

三条必须做对的点（对齐 AGENTS.md 红线）：
1. **密钥只进请求头**：`build_request` 把密钥放进 `Authorization`，绝不进 body / URL / 日志；
2. **URL 结果必须立刻下载落盘**：各家的临时 URL 只活 1~24 小时，交付里绝不引用它；
3. **下载走公网闸**：图片 URL 是**外部给的**，可能指向内网（SSRF）——每一跳都查
   `tools_builtin._host_is_forbidden`，并限制大小（流式读、超限就停）。

本模块不落盘、不记账，只管「发请求 → 拿回图片字节」；落盘 / 范围检查 / 记账在
`tools_imagegen.py`（工具层）。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger("maiwork.imagegen")

# 一次生图的整体超时（比聊天的 120 秒长：生图本身就要十几秒到几分钟）
DEFAULT_TIMEOUT_S = 180.0
# 单张图的大小上限（防被塞一个巨大的响应把内存 / 磁盘吃光）
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 跟随跳转的次数上限（图片 URL 常见 302 到 CDN）
MAX_REDIRECTS = 3
# 错误文案里带多少服务商原文（去密钥后）
_ERROR_TEXT_MAX = 200

SUPPORTED_APIS: tuple[str, ...] = ("openai", "siliconflow")

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class ImageGenError(Exception):
    """生图失败。message 是中文人话，**已去密钥**（调用方可以原样给模型 / 记库）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        self.status = status


@dataclass
class ImageResult:
    """一张生成好的图（内存里的字节）。"""

    data: bytes
    ext: str                 # png / jpg / gif / webp
    mime: str
    model: str
    size: str = ""
    source: str = "b64"      # b64 / url（这张图是怎么拿到的，只用于日志）


def _redact(text: Any, secrets: list[str]) -> str:
    """把已知密钥从文案里抹掉（工具层还会再过一遍统一遮罩，这里是第一道）。"""
    out = str(text or "")
    for s in secrets or []:
        if s:
            out = out.replace(s, "***")
    return out


def image_ext(data: bytes) -> str:
    """按 magic bytes 认图片格式；认不出来回 ""（调用方按「不是图片」处理）。"""
    raw = bytes(data or b"")
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    # webp：RIFF....WEBP
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return ""


_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def decode_b64(text: Any) -> bytes:
    """解 base64（容错缺失的 padding）；坏串抛中文 ImageGenError。"""
    s = str(text or "").strip()
    if not s:
        raise ImageGenError("生图服务返回了空的图片数据")
    if s.startswith("data:"):  # data:image/png;base64,xxxx
        s = s.split(",", 1)[-1]
    pad = (-len(s)) % 4
    try:
        return base64.b64decode(s + "=" * pad, validate=False)
    except (binascii.Error, ValueError):
        raise ImageGenError("生图服务返回的图片数据不是合法的 base64，这次结果用不了") from None


# ----------------------------------------------------------------------
# 请求
# ----------------------------------------------------------------------


def build_request(
    api: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    size: str = "",
    n: int = 1,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """拼一次生图请求：返回 (url, headers, body)。

    - 地址统一是 `{base_url}/images/generations`（两家都一样）；
    - `size` 空 = 不发这个字段（让服务商按默认来）；
    - 端点的自定义请求头（高级设置）按名字大小写不敏感替换默认头（和聊天一个口径）；
    - 密钥只在 `Authorization` 头里，绝不进 body / URL。
    """
    api_s = str(api or "").strip().lower()
    if api_s not in SUPPORTED_APIS:
        raise ImageGenError(
            f"生图接口形状「{api_s or '(空)'}」这一期还不支持（只能 {' / '.join(SUPPORTED_APIS)}）"
        )
    base = str(base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ImageGenError("生图端点地址要以 http:// 或 https:// 开头")
    url = base + "/images/generations"
    body: dict[str, Any] = {
        "model": str(model or "").strip(),
        "prompt": str(prompt or ""),
    }
    size_s = str(size or "").strip()
    try:
        n_i = max(1, int(n))
    except (TypeError, ValueError):
        n_i = 1
    if api_s == "siliconflow":
        if size_s:
            body["image_size"] = size_s
        body["batch_size"] = n_i
    else:
        if size_s:
            body["size"] = size_s
        body["n"] = n_i
    headers = {"Authorization": f"Bearer {str(api_key or '')}", "Content-Type": "application/json"}
    for name, value in dict(extra_headers or {}).items():
        key_s = str(name or "").strip()
        if not key_s:
            continue
        for existing in [k for k in headers if k.lower() == key_s.lower()]:
            del headers[existing]
        headers[key_s] = str(value)
    return url, headers, body


def parse_response(api: str, payload: Any) -> list[dict[str, str]]:
    """从生图响应里取出图片条目：`[{"b64": …}]` 或 `[{"url": …}]`（保序）。

    - 结果容器两家不同：openai 系 `data[]`，硅基流动 `images[]`（也容错认 `data[]`）；
    - 一条里 `b64_json` 优先，没有再看 `url`；
    - 组图里某条失败（带 `error`）只跳过那一条，不当整次失败；
    - 一条都没有 → 抛中文 ImageGenError。
    """
    if not isinstance(payload, dict):
        raise ImageGenError("生图服务返回的不是 JSON 对象，这次结果用不了")
    raw = payload.get("data")
    if not isinstance(raw, list):
        raw = payload.get("images")
    out: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json") or item.get("b64")
            if isinstance(b64, str) and b64.strip():
                out.append({"b64": b64.strip()})
                continue
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                out.append({"url": url.strip()})
    if not out:
        raise ImageGenError("生图服务没有返回图片（既没有图片数据也没有图片地址），这次结果用不了")
    return out


# ----------------------------------------------------------------------
# 下载（SSRF 闸 + 大小上限）
# ----------------------------------------------------------------------


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Any = None,
    max_bytes: int | None = None,
    secrets: list[str] | None = None,
) -> bytes:
    """下载一张图（流式读 + 每一跳查内网 + 大小上限）。

    图片地址是**外部给的**：这里复用 `tools_builtin` 的 `_host_is_forbidden`
    （和 fetch_page 同一套闸），别只靠子 agent 的 systemd 隔离——实现放在主进程侧时
    那层保护根本不生效。
    """
    from .tools_builtin import _default_resolver, _host_is_forbidden

    resolve = resolver or _default_resolver
    limit = int(MAX_IMAGE_BYTES if max_bytes is None else max_bytes)
    current = str(url or "").strip()
    secrets = list(secrets or [])
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https"):
            raise ImageGenError(
                f"图片地址只支持 http/https，{parts.scheme or '(没有协议)'} 不支持，这次结果用不了"
            )
        if not parts.hostname:
            raise ImageGenError("图片地址里没有主机名，下不了")
        if _host_is_forbidden(parts.hostname, resolve):
            raise ImageGenError(f"图片地址指向内网 / 本机（{parts.hostname}），不下载")
        try:
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location") or ""
                    if not location:
                        raise ImageGenError(f"图片地址返回 {resp.status_code} 但没给跳转到哪")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    raise ImageGenError(f"下载图片失败（HTTP {resp.status_code}）")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise ImageGenError(
                            f"图片太大（超过 {limit // 1024 // 1024}MB），这次结果用不了"
                        )
                return bytes(buf)
        except ImageGenError:
            raise
        except httpx.HTTPError as e:
            raise ImageGenError(f"下载图片失败：{_redact(e, secrets)}") from None
    raise ImageGenError(f"图片地址跳转超过 {MAX_REDIRECTS} 次，不下载")


# ----------------------------------------------------------------------
# 发请求
# ----------------------------------------------------------------------


def _http_error_message(status: int, text: str) -> str:
    """把服务商的 HTTP 错误翻成中文人话（别把英文原文直接甩给子 agent）。"""
    body = str(text or "")
    low = body.lower()
    if status in (401, 403):
        return f"生图服务说密钥不对或没有权限（HTTP {status}），检查端点里的密钥"
    if status == 404:
        return "生图地址不对：这个端点没有 images/generations（HTTP 404），检查端点地址"
    if status == 429:
        return "生图服务现在忙（HTTP 429），等一会儿再试"
    if any(k in low for k in ("datainspection", "ipinfringement", "content_policy", "moderation",
                              "safety", "sensitive", "risk")):
        return "这个提示词被服务商判定可能不合规 / 涉及版权内容，换个说法再试"
    if any(k in low for k in ("image_size", "invalid size", "size must", "unsupported size")):
        return f"这个尺寸服务商不接受（HTTP {status}）：换一个尺寸再试"
    tail = body.strip().replace("\n", " ")[:_ERROR_TEXT_MAX]
    return f"生图失败（HTTP {status}）：{tail}" if tail else f"生图失败（HTTP {status}）"


async def generate(
    *,
    api: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    size: str = "",
    n: int = 1,
    extra_headers: Mapping[str, str] | None = None,
    transport: Any = None,
    resolver: Any = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_bytes: int | None = None,
) -> list[ImageResult]:
    """发一次生图请求，回若干张图（字节已在内存里）。

    - 非 200 → ImageGenError（中文、已去密钥、带状态码）；
    - `b64_json` 直接用；`url` 立刻下载（临时 URL 会失效，绝不外传）；
    - 认不出图片格式 / 超过大小上限 → ImageGenError。
    """
    url, headers, body = build_request(
        api, base_url, api_key, model, prompt, size=size, n=n, extra_headers=extra_headers,
    )
    limit = int(MAX_IMAGE_BYTES if max_bytes is None else max_bytes)
    secrets = [str(api_key or "")] + [str(v) for v in (extra_headers or {}).values() if v]
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                raise ImageGenError(
                    _redact(_http_error_message(resp.status_code, resp.text), secrets),
                    status=resp.status_code,
                )
            try:
                payload = resp.json()
            except (ValueError, json.JSONDecodeError):
                raise ImageGenError("生图服务返回的不是 JSON，这次结果用不了") from None
            items = parse_response(api, payload)
            out: list[ImageResult] = []
            for item in items:
                if "b64" in item:
                    data = decode_b64(item["b64"])
                    source = "b64"
                else:
                    data = await download_image(
                        client, item["url"], resolver=resolver, max_bytes=limit, secrets=secrets,
                    )
                    source = "url"
                if not data:
                    raise ImageGenError("生图服务返回了空的图片数据")
                if len(data) > limit:
                    raise ImageGenError(
                        f"图片太大（超过 {limit // 1024 // 1024}MB），这次结果用不了"
                    )
                ext = image_ext(data)
                if not ext:
                    raise ImageGenError("生图服务返回的不是图片（认不出图片格式），这次结果用不了")
                out.append(ImageResult(
                    data=data, ext=ext, mime=_MIME_BY_EXT.get(ext, "application/octet-stream"),
                    model=str(model or ""), size=str(size or ""), source=source,
                ))
            return out
    except ImageGenError:
        raise
    except httpx.HTTPError as e:
        raise ImageGenError(f"生图请求失败：{_redact(e, secrets)}") from None
