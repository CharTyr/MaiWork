"""Jina Reader（https://r.jina.ai）：打开网页读正文的首选路（2026-09-28 用户定）。

fetch_page 的顺序：Jina Reader → 「抓网页正文」工具（联网搜索里绑定的 MCP）→ 服务器直接打开。
这里只管第一步：读得到就返回正文；**有任何问题都算失败**，让调用方马上换下一条路，不等不重试。

- 请求：GET https://r.jina.ai/<原网址>，Accept: application/json（拿标题 / 发布时间 / 正文分开的字段）；
  不传 X-Timeout（官方文档：传了会一直等满这个时长，线上实测 18~22 秒；不传多数 1~2 秒）。
- 限速（官方：不带 key 每个 IP 每分钟 20 次，带 key 500 次）：本地先按 18 / 450 次数兜住，
  用满直接失败换路，不排队；Jina 回 429 → 冷却 60 秒不再请求；401 / 402（密钥不对 / 额度用完）
  → 冷却 10 分钟。冷却状态只在内存里。
- 「读到了但不算数」：原网页 4xx/5xx、验证 / 拦截页（知乎「安全验证」实测）、正文太短（Reddit 实测为空）。
- 密钥只放在 Authorization 头里；返回的任何文字都不带它。
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import httpx

logger = logging.getLogger("maiwork.reader")

JINA_ENDPOINT = "https://r.jina.ai/"
_FREE_PER_MIN = 18        # 官方 20，留余量
_KEY_PER_MIN = 450        # 官方 500
_COOLDOWN_LIMIT_S = 60.0  # 429 之后
_COOLDOWN_AUTH_S = 600.0  # 401 / 402 之后
_TIMEOUT_S = 25.0
_MIN_BODY_CHARS = 60
_TEXT_MAX_CHARS = 8000    # 和直接打开一样截 8000 字
# 读到的是验证 / 拦截 / 错误页（看标题；正文很短时也看开头）
_BLOCKED = re.compile(
    r"安全验证|人机验证|验证码|请完成验证|访问验证|访问受限|"
    r"just a moment|attention required|access denied|verify you are human|"
    r"security check|are you a robot|captcha|403 forbidden|404 not found|页面不存在|页面找不到",
    re.IGNORECASE,
)


@dataclass
class ReadResult:
    ok: bool
    text: str = ""        # 成功：《标题》+（发布时间）+ 正文，截 8000 字
    reason: str = ""      # 失败原因（中文，给子 agent / 日志看；不含密钥）
    image_url: str = ""
    final_url: str = ""
    published: str = ""


class JinaReader:
    def __init__(
        self,
        get_settings: Callable[[], Any],
        *,
        transport: Any = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._get_settings = get_settings
        self._transport = transport
        self._now = now
        self._sent: deque[float] = deque()
        self._cool_until = 0.0
        self._cool_why = ""

    # ------------------------------------------------------------------
    # 配置 / 状态
    # ------------------------------------------------------------------

    def _cfg(self) -> tuple[bool, str]:
        try:
            r = getattr(self._get_settings(), "reader", None)
        except Exception:
            r = None
        if r is None:
            return True, ""
        return bool(getattr(r, "jina_enabled", True)), str(getattr(r, "jina_api_key", "") or "").strip()

    def enabled(self) -> bool:
        return self._cfg()[0]

    def _per_min(self, key: str) -> int:
        return _KEY_PER_MIN if key else _FREE_PER_MIN

    def status(self) -> tuple[bool, str]:
        """(能用, 中文说明)：网页「运行状态」用。"""
        enabled, key = self._cfg()
        if not enabled:
            return False, "Jina Reader 关着：打开网页先用抓正文工具，再不行直接打开"
        left = self._cool_until - self._now()
        if left > 0:
            return False, f"Jina Reader 暂停中（{self._cool_why}），约 {int(left) + 1} 秒后恢复；这期间换别的路打开"
        if key:
            return True, "先用 Jina Reader（带密钥，每分钟 500 次）"
        return True, "先用 Jina Reader（免费额度，每分钟 20 次，按服务器 IP 算）"

    def _cool(self, seconds: float, why: str) -> None:
        self._cool_until = self._now() + seconds
        self._cool_why = why
        logger.info("Jina Reader 暂停 %d 秒：%s", int(seconds), why)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def read(self, url: str) -> ReadResult:
        enabled, key = self._cfg()
        if not enabled:
            return ReadResult(False, reason="Jina Reader 关着")
        now = self._now()
        if now < self._cool_until:
            return ReadResult(False, reason=f"Jina Reader 暂停中（{self._cool_why}）")
        while self._sent and now - self._sent[0] >= 60.0:
            self._sent.popleft()
        limit = self._per_min(key)
        if len(self._sent) >= limit:
            return ReadResult(False, reason=f"Jina Reader 这一分钟用满了（{limit} 次）")
        self._sent.append(now)

        headers = {
            "Accept": "application/json",
            "X-Retain-Images": "none",   # 正文里不要图片链接（省字数；封面图另从 metadata 拿）
            "X-Retain-Links": "text",    # 链接只留文字
        }
        if key:
            if not key.isascii():
                return ReadResult(False, reason="Jina Reader 密钥里有中文或全角字符，检查一下是不是复制多了")
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT_S) as client:
                resp = await client.get(JINA_ENDPOINT + url, headers=headers)
        except httpx.TimeoutException:
            return ReadResult(False, reason="Jina Reader 超时")
        except httpx.HTTPError as e:
            return ReadResult(False, reason=f"Jina Reader 连不上（{type(e).__name__}）")

        code = resp.status_code
        if code == 429:
            self._cool(_COOLDOWN_LIMIT_S, "被限流")
            return ReadResult(False, reason="Jina Reader 限流了")
        if code in (401, 402):
            why = "密钥不对" if code == 401 else "额度用完了"
            if key:
                self._cool(_COOLDOWN_AUTH_S, why)
            return ReadResult(False, reason=f"Jina Reader {why}（{code}）")
        if code == 403:
            return ReadResult(False, reason="Jina Reader 拒绝读这个网站（403）")
        if code != 200:
            return ReadResult(False, reason=f"Jina Reader 返回 {code}")
        try:
            payload = resp.json()
        except ValueError:
            return ReadResult(False, reason="Jina Reader 回的不是 JSON")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return ReadResult(False, reason="Jina Reader 没给正文")

        origin = data.get("httpStatus")
        if isinstance(origin, int) and origin >= 400:
            return ReadResult(False, reason=f"原网页返回 {origin}")
        title = str(data.get("title") or "").strip()
        body = str(data.get("content") or "").strip()
        if _BLOCKED.search(title) or (len(body) < 1500 and _BLOCKED.search(body[:300])):
            return ReadResult(False, reason="读到的是验证 / 拦截页，不是正文")
        if len(body) < _MIN_BODY_CHARS:
            return ReadResult(False, reason="读到的正文是空的")

        published = str(data.get("publishedTime") or "").strip()
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        image = str(meta.get("og:image") or "").strip()
        if not image.startswith(("http://", "https://")):
            image = ""
        text = (f"《{title}》\n" if title else "") + (f"（发布时间：{published}）\n" if published else "") + body
        if len(text) > _TEXT_MAX_CHARS:
            text = text[:_TEXT_MAX_CHARS] + " …（后面还有，已截断）"
        final_url = str(data.get("url") or "").strip() or url
        return ReadResult(True, text=text, image_url=image, final_url=final_url, published=published)
