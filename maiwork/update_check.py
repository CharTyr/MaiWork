"""更新提醒：定期查 GitHub 上 MaiWork 的最新版本号，只提醒、不自动更新。

为什么不在 MaiWork 网页里直接更新（docs/02 §更新提醒）：插件换自己的文件，新版起不来网页就一起挂、
没处回退；写 plugins/ 反正会让全部插件重载；公网网页上放「能换代码的按钮」风险大；
MaiBot 自己的插件管理已经会重新下载、保留 config.toml、留备份。所以这里只做：
- 查最新版本：先 raw.githubusercontent.com 的 _manifest.json，失败再 jsDelivr 镜像；
  manifest 的 id 必须是 chartyr.maiwork，否则不认。
- 更新说明：GitHub 最新一条提交的首行，且必须以这个版本号开头（发版提交的写法），否则不给。
- 频率：成功后 6 小时内不再查；失败 30 分钟后才重试；失败不清上次结果、不报错，只记 error。
- 只在管理员打开网页时顺手查（maybe_refresh），不另起后台循环。
出站用 httpx（transport 可注入，测试不碰真实网络）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("maiwork.update_check")

PLUGIN_ID = "chartyr.maiwork"
REPO = "CharTyr/MaiWork"
MANIFEST_URLS = (
    f"https://raw.githubusercontent.com/{REPO}/main/_manifest.json",
    f"https://cdn.jsdelivr.net/gh/{REPO}@main/_manifest.json",
)
COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits?per_page=1"
REPO_URL = f"https://github.com/{REPO}"
CHECK_INTERVAL_S = 6 * 3600
RETRY_AFTER_FAIL_S = 30 * 60
TIMEOUT_S = 8.0
NOTES_MAX = 200


def local_version(manifest: Path | None = None) -> str:
    """本机装的版本：读插件目录里的 _manifest.json（读不到回空串，提醒就不出）。"""
    path = manifest or Path(__file__).resolve().parents[1] / "_manifest.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        return ""


def parse_version(v: str) -> tuple[int, ...] | None:
    s = str(v or "").strip().lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+)*", s):
        return None
    return tuple(int(x) for x in s.split("."))


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


class UpdateCheck:
    def __init__(
        self,
        current: str,
        *,
        transport: Any = None,
        now: Callable[[], float] = time.time,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._current = str(current)
        self._transport = transport
        self._now = now
        self._enabled = enabled or (lambda: True)
        self._latest = ""
        self._notes = ""
        self._checked_ts = 0.0
        self._error = ""
        self._next_ts = 0.0
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        enabled = bool(self._enabled())
        return {
            "enabled": enabled,
            "current": self._current,
            "latest": self._latest,
            "newer": enabled and is_newer(self._latest, self._current),
            "notes": self._notes,
            "checked_ts": self._checked_ts or None,
            "error": self._error,
            "repo_url": REPO_URL,
        }

    async def maybe_refresh(self) -> dict[str, Any]:
        """到点了才查（成功 6 小时一次、失败 30 分钟后重试）；没到点直接回缓存。"""
        if not self._enabled():
            return self.status()
        if self._now() < self._next_ts or self._lock.locked():
            return self.status()
        return await self.refresh()

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            try:
                import httpx
            except ImportError:  # pragma: no cover - httpx 是硬依赖
                self._error = "缺 httpx"
                self._next_ts = self._now() + RETRY_AFTER_FAIL_S
                return self.status()
            kwargs: dict[str, Any] = {"timeout": httpx.Timeout(TIMEOUT_S), "follow_redirects": True}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    latest = await self._fetch_version(client)
                    if not latest:
                        self._error = "查不到最新版本（GitHub 和镜像都没连上或内容不对）"
                        self._next_ts = self._now() + RETRY_AFTER_FAIL_S
                        return self.status()
                    notes = await self._fetch_notes(client, latest)
            except Exception as exc:  # 网络层意外：安静记下
                logger.info("MaiWork 更新检查失败：%s", exc)
                self._error = "查更新时出错"
                self._next_ts = self._now() + RETRY_AFTER_FAIL_S
                return self.status()
            self._latest = latest
            self._notes = notes
            self._checked_ts = self._now()
            self._error = ""
            self._next_ts = self._checked_ts + CHECK_INTERVAL_S
            return self.status()

    async def _fetch_version(self, client: Any) -> str:
        for url in MANIFEST_URLS:
            try:
                resp = await client.get(url)
                if resp.status_code // 100 != 2:
                    continue
                data = resp.json()
            except Exception:
                continue
            if not isinstance(data, dict) or str(data.get("id") or "") != PLUGIN_ID:
                continue
            v = str(data.get("version") or "").strip()
            if parse_version(v) is not None:
                return v
        return ""

    async def _fetch_notes(self, client: Any, latest: str) -> str:
        try:
            resp = await client.get(COMMITS_URL, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code // 100 != 2:
                return ""
            data = resp.json()
            msg = str(((data or [{}])[0].get("commit") or {}).get("message") or "")
        except Exception:
            return ""
        first = msg.strip().splitlines()[0].strip() if msg.strip() else ""
        if not first.lstrip("vV").startswith(latest):
            return ""
        return first[:NOTES_MAX]
