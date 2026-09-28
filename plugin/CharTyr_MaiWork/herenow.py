"""herenow.py：here.now 匿名发布（无账号、无 API key，24 小时过期）。

接口形状以 docs/06-宿主接口事实.md 末尾「here.now 匿名发布」一节为准：
1. POST /api/v1/publish（不带 Authorization，带 x-herenow-client）→
   得到 slug / siteUrl / upload.versionId / upload.uploads[] / upload.finalizeUrl /
   claimToken / claimUrl / expiresAt
2. 每个 upload.uploads[i]：PUT url，只带返回的 headers（预签名 1 小时有效，幂等，
   失败可以重试 1 次）
3. POST upload.finalizeUrl {"versionId"} → finalize 成功前站点不算上线

我们自己的上限（比官方更紧）
- 文件数 ≤2500（和官方一致）
- 单文件 ≤50MB（自定，不能大于总量上限，否则单文件限制形同虚设）
- 总量 ≤200MB（我们自己定的，别一次发太多）

错误统一 HereNowError（中文）；429 时带 retry_after（秒）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx

from .models import _redact

logger = logging.getLogger("maiwork.herenow")

PUBLISH_URL = "https://here.now/api/v1/publish"

# 我们自己的上限
MAX_FILES = 2500
MAX_FILE_BYTES = 50 * 1024 * 1024   # 50MB 单文件（不能大于总量上限，否则单文件限制形同虚设）
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200MB 总量（自定，比官方紧）

_ERR_MAX = 300

# 常见扩展名 → Content-Type（未知 → application/octet-stream）
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}


class HereNowError(Exception):
    """here.now 发布失败（中文简述）；429 时 retry_after 是秒数。"""

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _skip_name(name: str) -> bool:
    """隐藏文件（. 开头）、._ 开头（macOS 资源叉）都跳过。"""
    return name.startswith(".") or name.startswith("._")


def _iter_files(directory: Path) -> Iterator[tuple[str, Path, int]]:
    """遍历发布目录，产出 (相对路径用 /, 绝对 Path, size)。跳过隐藏文件。

    安全（S3/M3）：
    - 任何符号链接（文件或目录）一律跳过并记日志——插件线上是 root，跟随链接
      会把工作区外的文件（如 /root/.typesafe_key）发到公开 here.now；
    - 每个文件 resolve 后必须仍在 目录.resolve() 之内（防中间目录被替换成链接）；
    - 大小用 lstat 先取（发布前不读内容就能判超限）。
    """
    directory = Path(directory)
    try:
        root_resolved = directory.resolve()
    except OSError:
        return
    for p in sorted(directory.rglob("*")):
        if p.is_symlink():
            logger.warning("发布目录里的符号链接已跳过：%s", p)
            continue
        if not p.is_file():
            continue
        rel_parts = p.relative_to(directory).parts
        if any(_skip_name(part) for part in rel_parts):
            continue
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved != root_resolved and root_resolved not in resolved.parents:
            logger.warning("发布目录里的文件解析到目录外，已跳过：%s → %s", p, resolved)
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        yield "/".join(rel_parts), p, size


def _parse_expires_at(value: Any) -> Optional[float]:
    """ISO8601 字符串 → epoch；解析不了 → None。"""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _error_from_response(resp: httpx.Response, what: str) -> HereNowError:
    """把 here.now 的错误响应变成中文 HereNowError。

    429 → 带 retry_after；400 + missingFiles → 「上传没到位」；
    其他 → 状态码 + error/message 字段（去密钥、截 300 字）。
    """
    body: dict[str, Any] = {}
    try:
        data = resp.json()
        if isinstance(data, dict):
            body = data
    except Exception:
        body = {}
    if resp.status_code == 429:
        retry_after: Optional[float] = None
        ra = body.get("retry_after")
        if ra is None:
            ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        try:
            if ra is not None:
                retry_after = float(ra)
        except (TypeError, ValueError):
            retry_after = None
        return HereNowError(
            f"{what}被 here.now 限流（429），稍后再试",
            retry_after=retry_after,
        )
    err = str(body.get("error") or "")
    if resp.status_code == 400 and err == "missingFiles":
        return HereNowError(f"{what}失败：here.now 说上传没到位")
    detail = err or str(body.get("message") or "")
    if err and body.get("message") and str(body["message"]) != err:
        detail = f"{err}：{body['message']}"
    if not detail:
        detail = resp.text[:_ERR_MAX]
    detail = _redact(detail, [])
    return HereNowError(f"{what}失败（here.now 返回 {resp.status_code}）：{detail}")


class HereNow:
    """here.now 匿名发布。transport 是给测试用的 httpx.MockTransport。"""

    def __init__(self, transport: Any = None, client_header: str = "maiwork/plugin") -> None:
        self._transport = transport
        self._client_header = str(client_header or "maiwork/plugin")

    async def publish(self, directory: Path) -> dict:
        """发布一个目录，返回 {"url", "slug", "expires_ts", "claim_url", "claim_token"}。"""
        directory = Path(directory)
        if not directory.is_dir():
            raise HereNowError(f"发布目录不存在或不是目录：{directory}")

        # 1) 扫描 + 上限检查（不发出任何网络请求）
        manifest: list[dict[str, Any]] = []
        contents: dict[str, bytes] = {}
        total = 0
        count = 0
        for rel, path, size in _iter_files(directory):
            count += 1
            if count > MAX_FILES:
                raise HereNowError(f"文件太多（超过 {MAX_FILES} 个），here.now 发不了")
            if size > MAX_FILE_BYTES:
                raise HereNowError(
                    f"文件 {rel} 太大（超过 50MB），here.now 发不了"
                )
            total += size
            if total > MAX_TOTAL_BYTES:
                raise HereNowError(
                    f"全部文件加起来太大（超过 200MB），here.now 发不了"
                )
            try:
                data = path.read_bytes()
            except OSError as e:
                raise HereNowError(f"读不了文件 {rel}：{e}") from None
            manifest.append(
                {
                    "path": rel,
                    "size": size,
                    "contentType": _content_type(path),
                    "hash": hashlib.sha256(data).hexdigest(),
                }
            )
            contents[rel] = data

        if not manifest:
            raise HereNowError("目录里没有可以发布的文件")

        async with httpx.AsyncClient(
            transport=self._transport, timeout=60.0
        ) as client:
            # 2) create
            try:
                resp = await client.post(
                    PUBLISH_URL,
                    json={"files": manifest},
                    headers={
                        "x-herenow-client": self._client_header,
                        "content-type": "application/json",
                    },
                )
            except httpx.HTTPError as e:
                raise HereNowError(f"连不上 here.now：{type(e).__name__}") from None
            if resp.status_code != 200:
                raise _error_from_response(resp, "发布")
            try:
                created = resp.json()
            except Exception:
                raise HereNowError("here.now 返回的不是 JSON") from None
            if not isinstance(created, dict):
                raise HereNowError("here.now 返回格式不对")

            slug = str(created.get("slug") or "")
            create_site_url = str(created.get("siteUrl") or "")
            upload = created.get("upload") if isinstance(created.get("upload"), dict) else {}
            version_id = str(upload.get("versionId") or "")
            uploads = upload.get("uploads") if isinstance(upload.get("uploads"), list) else []
            finalize_url = str(upload.get("finalizeUrl") or "")

            # 3) 逐个 PUT 预签名上传；失败重试 1 次（幂等）
            for up in uploads:
                if not isinstance(up, dict):
                    continue
                url = str(up.get("url") or "")
                rel = str(up.get("path") or "")
                if not url:
                    continue
                headers = up.get("headers") if isinstance(up.get("headers"), dict) else {}
                data = contents.get(rel, b"")
                last_exc: Optional[HereNowError] = None
                for attempt in range(2):  # 首次 + 重试 1 次
                    try:
                        put = await client.put(url, content=data, headers=dict(headers))
                    except httpx.HTTPError as e:
                        last_exc = HereNowError(
                            f"上传 {rel} 到 here.now 失败：{type(e).__name__}"
                        )
                        continue
                    if put.status_code in (200, 201, 204):
                        last_exc = None
                        break
                    last_exc = _error_from_response(put, f"上传 {rel} ")
                if last_exc is not None:
                    raise last_exc

            # 4) finalize
            if not finalize_url:
                raise HereNowError("here.now 没返回 finalizeUrl")
            try:
                fin = await client.post(finalize_url, json={"versionId": version_id})
            except httpx.HTTPError as e:
                raise HereNowError(f"连不上 here.now：{type(e).__name__}") from None
            if fin.status_code != 200:
                raise _error_from_response(fin, "发布")
            try:
                finished = fin.json()
            except Exception:
                finished = {}
            if not isinstance(finished, dict):
                finished = {}

        url = str(finished.get("siteUrl") or "") or create_site_url
        expires_ts = _parse_expires_at(created.get("expiresAt"))
        if expires_ts is None:
            expires_ts = time.time() + 24 * 3600
        return {
            "url": url,
            "slug": slug,
            "expires_ts": float(expires_ts),
            "claim_url": str(created.get("claimUrl") or ""),
            "claim_token": str(created.get("claimToken") or ""),
        }
