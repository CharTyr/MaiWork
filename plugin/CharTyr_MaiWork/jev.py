"""jev.py：TypeSafe Jev HTTP 客户端 + 熔断 + judgments 落库。

- 密钥读取顺序（照 reference/jev/client.py::resolve_key；2026-10 起网页改的密钥
  也写进 config.toml，数据库不再存覆盖层）：
  环境变量 TYPESAFE_API_KEY → [jev] api_key（配置，网页改的也写这里）
  → TYPESAFE_KEY_FILE 环境变量 → [jev] key_file（配置，默认 ~/.typesafe_key）。
  符号链接或非文件拒绝读取。
- 密钥不打印、不入库；错误消息返回给调用方前已去掉密钥。
- 请求：POST {api_url}，Authorization Bearer，body={model,state,questions}，timeout=timeout_ms。
- 答案校验：noul 在 [0,1]；choice 的标签必须在 criteria 里，probabilities 键集合
  和 criteria 一致。校验不过 → 返回 None。
- 熔断：连续失败 3 次停 60 秒。
- 每次调用写 judgments 表（state_summary 为 state JSON 截 800 字）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from . import clock
from .config import Settings
from .store import Store

logger = logging.getLogger("maiwork.jev")

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"

_BREAKER_THRESHOLD = 3      # 连续失败 3 次熔断
_BREAKER_COOLDOWN_S = 60.0  # 熔断时长（秒）

# 状态摘要最大字数（存 judgments）
_STATE_SUMMARY_MAX = 800
_ANSWERS_JSON_MAX = 4000


class JevError(Exception):
    """Jev 调用错误（含超时）。message 已去掉密钥。"""


def _resolve_key(settings: Settings) -> str:
    """读密钥，不打印、不返回给外部。返回 "" 表示没有。"""
    # 1) 环境变量
    env_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if env_key:
        return env_key
    # 2) 配置里的 api_key（网页「设置 → 快速判断」改的也写进这里，config.toml）
    configured = str(getattr(settings.jev, "api_key", "") or "").strip()
    if configured:
        return configured
    # 4) 环境变量 TYPESAFE_KEY_FILE 指定的文件
    env_path = os.environ.get("TYPESAFE_KEY_FILE", "").strip()
    candidate_paths: List[Path] = []
    if env_path:
        candidate_paths.append(Path(env_path).expanduser())
    # 5) 配置的 key_file（默认 ~/.typesafe_key）
    conf_path = str(getattr(settings.jev, "key_file", "") or "").strip()
    if conf_path:
        candidate_paths.append(Path(conf_path).expanduser())
    # 6) ~/.typesafe_key
    candidate_paths.append(Path.home() / ".typesafe_key")

    for path in candidate_paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text().strip()
            if text:
                return text
        except OSError:
            continue
    return ""


def _sanitize_error(err: str, key: str, max_len: int = 300) -> str:
    """错误消息去掉密钥、Bearer 头、sk- 形式，并截断。"""
    out = str(err or "")
    if key:
        out = out.replace(key, "***")
    out = _re_bearer.sub("Bearer ***", out)
    out = _re_sk.sub("sk-***", out)
    if len(out) > max_len:
        out = out[:max_len]
    return out


import re as _re_module

_re_bearer = _re_module.compile(r"(?i)bearer\s+\S+")
_re_sk = _re_module.compile(r"sk-[A-Za-z0-9_\-]{3,}")


def _state_summary(state: Any, key: str, max_len: int = _STATE_SUMMARY_MAX) -> str:
    """state 的 JSON 字符串，截断 max_len 字；去掉密钥。"""
    try:
        text = json.dumps(state, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(state)
    if key:
        text = text.replace(key, "***")
    text = _re_bearer.sub("Bearer ***", text)
    text = _re_sk.sub("sk-***", text)
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def _answers_json(answers: Any, key: str, max_len: int = _ANSWERS_JSON_MAX) -> str:
    """answers 序列化成 JSON 字符串；去掉密钥。"""
    try:
        text = json.dumps(answers, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(answers)
    if key:
        text = text.replace(key, "***")
    text = _re_bearer.sub("Bearer ***", text)
    text = _re_sk.sub("sk-***", text)
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


# ----------------------------------------------------------------------
# 答案校验（照 reference/jev/processor.py 的 _bounded/_probability/_choice）
# ----------------------------------------------------------------------


def _bounded(value: Any) -> float:
    """校验 0..1 的浮点概率。"""
    if isinstance(value, bool):
        raise ValueError("invalid probability")
    if not isinstance(value, (int, float)):
        raise ValueError("invalid probability")
    import math
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("invalid probability")
    return number


def _probability(answer: Any) -> float:
    """校验 noul 答案：type=="noul"，noul 在 [0,1]。"""
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise ValueError("missing noul answer")
    return _bounded(answer.get("noul"))


def _choice(answer: Any, allowed: List[str]) -> Tuple[str, float, float]:
    """校验 choice 答案：标签必须在 allowed 里，probabilities 键集合一致。"""
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("missing choice answer")
    label = answer.get("choice")
    if not isinstance(label, str) or label not in allowed:
        raise ValueError("invalid choice label")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities.keys()) != set(allowed):
        raise ValueError("invalid probabilities")
    return label, _bounded(probabilities.get(label)), _bounded(answer.get("confidence"))


def validate_answers(raw: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
    """校验 Jev 返回的 answers；返回规范化的 {key: float / (label, prob, confidence)}。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
        raise ValueError("invalid response")
    answers = raw["answers"]
    result: Dict[str, Any] = {}
    for key, q in questions.items():
        if not isinstance(q, dict):
            continue
        if "type" not in q:
            continue
        qtype = q["type"]
        if qtype == "noul":
            result[key] = _probability(answers.get(key))
        elif qtype == "choice":
            criteria = q.get("criteria") or {}
            allowed = list(criteria.keys())
            if not allowed:
                raise ValueError(f"choice question {key} has no criteria")
            result[key] = _choice(answers.get(key), allowed)
        else:
            raise ValueError(f"unknown question type: {qtype}")
    return result


# ----------------------------------------------------------------------
# Jev 客户端
# ----------------------------------------------------------------------


class Jev:
    """Jev：快速是非/选择判断器客户端，带熔断和 judgments 落库。

    Key features:
    - available()：开着、有密钥、没在熔断。
    - ask()：失败/校验不过返回 None，调用方走慢路径。
    - 密钥不入日志、不入库；错误消息先去掉密钥。
    """

    def __init__(
        self,
        store: Store,
        get_settings: Callable[[], Settings],
        *,
        transport: Any = None,
    ) -> None:
        self._store = store
        self._get_settings = get_settings
        self._client = httpx.AsyncClient(transport=transport) if transport else httpx.AsyncClient()
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 配置 / 密钥
    # ------------------------------------------------------------------

    def _settings(self) -> Settings:
        s = self._get_settings()
        if s is None:
            raise JevError("settings 还没准备好")
        return s

    def _key(self) -> str:
        """读密钥；不存在或不可用返回 ""，不抛异常。"""
        try:
            return _resolve_key(self._settings())
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    def _breaker_open(self) -> bool:
        return clock.now() < self._breaker_open_until

    def _record_failure(self, ms: int, error: str, *, purpose: str, group_id: str, state: Any, key: str, answers: Any = None) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = clock.now() + _BREAKER_COOLDOWN_S
        self._log_failure(ms, error, purpose=purpose, group_id=group_id, state=state, key=key, answers=answers)

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    def _log_failure(self, ms: int, error: str, *, purpose: str, group_id: str, state: Any, key: str, answers: Any = None) -> None:
        clean_error = _sanitize_error(error, key)
        state_text = _state_summary(state, key)
        answers_text = _answers_json(answers if answers is not None else {}, key)
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO judgments (ts, day, purpose, group_id, state_summary, answers, ms, ok, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clock.now(),
                        clock.day_key(clock.now()),
                        str(purpose or ""),
                        str(group_id or ""),
                        state_text,
                        answers_text,
                        int(ms),
                        0,
                        clean_error,
                    ),
                )
        except Exception:
            logger.exception("写 judgments 失败（purpose=%s group=%s）", purpose, group_id)

    # ------------------------------------------------------------------
    # available / calls_today
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """开着、有密钥、没在熔断。"""
        try:
            s = self._settings()
            if not getattr(s.jev, "enabled", True):
                return False
            if not self._key():
                return False
            return not self._breaker_open()
        except Exception:
            return False

    def calls_today(self) -> int:
        """北京时间的今天，发起过多少次 Jev 调用（含失败）。"""
        day = clock.day_key(clock.now())
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM judgments WHERE day=?", (day,)
        ).fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # ask
    # ------------------------------------------------------------------

    async def ask(
        self,
        state: dict,
        questions: dict,
        *,
        purpose: str,
        group_id: str = "",
        timeout_ms: int | None = None,
    ) -> Optional[Dict[str, Any]]:
        """问一次 Jev；校验不过或失败返回 None，调用方走慢路径。

        参数：
            state: 聊天状态（会被序列化进 judgments，截断后用）。
            questions: {"key": {"type": "noul"|"choice", "instructions": "…", "criteria": {…}?}}
            purpose: 干啥用的（topic / fact / approval…），落库用。
            group_id: 群号，落库用。
            timeout_ms: 单次调用超时，默认 settings.jev.timeout_ms。
        """
        if not self.available():
            return None
        settings = self._settings()
        key = self._key()
        if not key:
            return None

        url = str(getattr(settings.jev, "api_url", "") or DEFAULT_API_URL).strip()
        model = str(getattr(settings.jev, "model", "") or DEFAULT_MODEL).strip()
        if timeout_ms is None:
            timeout_ms = getattr(settings.jev, "timeout_ms", 1500)
        timeout_s = max(0.1, float(timeout_ms) / 1000.0)

        async with self._lock:  # 熔断计数里有共享状态，串行请求
            # 再次检查（拿到锁后可能别人刚成功/失败过）
            if self._breaker_open():
                logger.debug("Jev 熔断中，跳过本次调用（purpose=%s）", purpose)
                return None
            start = clock.now()
            answers: Optional[Dict[str, Any]] = None
            error_text = ""
            ok = False
            try:
                resp = await self._client.post(
                    url,
                    json={"model": model, "state": state, "questions": questions},
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=timeout_s,
                )
                ms = int((clock.now() - start) * 1000)
                if resp.status_code != 200:
                    error_text = f"http_{resp.status_code}"
                    try:
                        body = resp.text[:200]
                    except Exception:
                        body = ""
                    if body:
                        error_text += f": {body}"
                    self._record_failure(ms, error_text, purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                try:
                    data = resp.json()
                except ValueError:
                    self._record_failure(ms, "bad_json", purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                try:
                    answers = validate_answers(data, questions)
                except ValueError as exc:
                    self._record_failure(ms, f"invalid_answers: {exc}", purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                ok = True
                self._record_success()
                # 成功也落库
                self._log_success(
                    ms,
                    purpose=purpose,
                    group_id=group_id,
                    state=state,
                    answers_raw=data.get("answers", {}),
                    answers_validated=answers,
                    key=key,
                )
                return answers
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                ms = int((clock.now() - start) * 1000)
                self._record_failure(ms, type(exc).__name__, purpose=purpose, group_id=group_id, state=state, key=key)
                return None
            except Exception as exc:
                ms = int((clock.now() - start) * 1000)
                self._record_failure(ms, f"unexpected: {exc}", purpose=purpose, group_id=group_id, state=state, key=key)
                return None

    def _log_success(
        self,
        ms: int,
        *,
        purpose: str,
        group_id: str,
        state: Any,
        answers_raw: Any,
        answers_validated: Dict[str, Any],
        key: str,
    ) -> None:
        state_text = _state_summary(state, key)
        # validated 的 answers（最多截断）
        try:
            # tuple 转 list 以便 JSON 序列化
            serializable: Dict[str, Any] = {}
            for k, v in answers_validated.items():
                if isinstance(v, tuple):
                    serializable[k] = list(v)
                else:
                    serializable[k] = v
            answers_text = _answers_json(serializable, key)
        except Exception:
            answers_text = _answers_json(answers_raw, key)
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO judgments (ts, day, purpose, group_id, state_summary, answers, ms, ok, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clock.now(),
                        clock.day_key(clock.now()),
                        str(purpose or ""),
                        str(group_id or ""),
                        state_text,
                        answers_text,
                        int(ms),
                        1,
                        "",
                    ),
                )
        except Exception:
            logger.exception("写 judgments 失败（purpose=%s group=%s）", purpose, group_id)

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()
