"""TypeSafe Jev HTTP 客户端；不记录请求正文与凭据。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import httpx

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"


class TypeSafeError(Exception):
    pass


def resolve_key(configured: str = "", key_file: str = "", *, env: Optional[Mapping[str, str]] = None) -> str:
    source = os.environ if env is None else env
    key = str(source.get("TYPESAFE_API_KEY", "") or configured or "").strip()
    if key:
        return key
    path = str(source.get("TYPESAFE_KEY_FILE", "") or key_file or (Path.home() / ".typesafe_key"))
    try:
        candidate = Path(path).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            return ""
        return candidate.read_text().strip()
    except OSError:
        return ""


class TypeSafeClient:
    def __init__(
        self,
        *,
        http: Any,
        api_url: str = DEFAULT_API_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 1.5,
    ) -> None:
        self.http = http
        self.api_url = str(api_url or DEFAULT_API_URL).strip()
        self.model = str(model or DEFAULT_MODEL).strip()
        self.timeout_s = max(0.1, float(timeout_s))

    async def evaluate(
        self,
        api_key: str,
        state: Dict[str, Any],
        questions: Dict[str, Dict[str, str]],
    ) -> Dict[str, Any]:
        if not api_key:
            raise TypeSafeError("no_api_key")
        payload = {"model": self.model, "state": state, "questions": questions}
        try:
            response = await self.http.post(
                self.api_url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.timeout_s,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise TypeSafeError(type(exc).__name__) from exc
        if response.status_code != 200:
            raise TypeSafeError(f"http_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise TypeSafeError("bad_json") from exc
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise TypeSafeError("missing_answers")
        return data
