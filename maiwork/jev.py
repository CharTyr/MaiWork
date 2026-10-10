"""jev.py：判断服务 HTTP 客户端（TypeSafe Jev / OpenAI Decisions 等）+ 熔断 + judgments 落库。

- 现在支持多家判断服务（2026-10）：内置 TypeSafe 那个仍用旧的 [jev] api_url/api_key/model
  字段和密钥链；用户自己加的写在 [[jev_endpoints]]，[jev] use 选现在用哪一个
  （见 jev_presets.py 的内置预设、resolve_target）。
- 密钥读取顺序（只对内置那个；照 reference/jev/client.py::resolve_key；2026-10 起网页
  改的密钥也写进 config.toml，数据库不再存覆盖层）：
  环境变量 TYPESAFE_API_KEY → [jev] api_key（配置，网页改的也写这里）
  → TYPESAFE_KEY_FILE 环境变量 → [jev] key_file（配置，默认 ~/.typesafe_key）。
  符号链接或非文件拒绝读取。自己加的服务用条目自己的 api_key。
- 密钥不打印、不入库；错误消息返回给调用方前已去掉密钥。
- 协议两种（jev_presets.PROTOCOLS）：systemone 直接 POST {model,state,questions}；
  openai_decisions 走 OpenAI Decisions 形状（{model,input,questions:[…]}），应答由
  normalize_response 折回 systemone 形状再统一校验。
- 答案校验：noul 在 [0,1]；choice 的标签必须在 criteria 里，probabilities 键集合
  和 criteria 一致。校验不过 → 返回 None。
- 熔断：连续失败 3 次停 60 秒；**按当前目标各算**，换了服务就重新开始。
- 每次调用写 judgments 表（state_summary 为 state JSON 截 800 字；失败原因前面带
  "[目标 id] "，看得出是哪个服务挂的）。
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

# 网页「测试」按钮发的那道小请求（固定内容，不碰真实业务）
_TEST_STATE = {"message": "你好，今天天气不错"}
_TEST_QUESTIONS: Dict[str, Any] = {
    "greeting": {"type": "noul", "instructions": "这条消息是不是在打招呼？"},
    "lang": {
        "type": "choice",
        "instructions": "这条消息是什么语言？",
        "criteria": {"zh": "中文", "en": "英文"},
    },
}


class JevError(Exception):
    """Jev 调用错误（含超时）。message 已去掉密钥。"""


@dataclass(frozen=True)
class JevTarget:
    """现在这一次判断打给谁：一个内置 / 自己加的判断服务。"""

    id: str
    name: str
    protocol: str   # systemone / openai_decisions
    url: str
    model: str
    key: str = ""   # "" = 没配密钥，用不了


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


def resolve_target(settings: Settings) -> Optional[JevTarget]:
    """按 [jev] use 算出现在打给谁；找不到（选了不存在的 id）返回 None。

    - use "" / "typesafe"：内置那个，用旧的 [jev] api_url / model + 原来的密钥链。
    - 别的：settings.jev_endpoints 里同 id 的那条，密钥用条目自己的 api_key。
    key 为 "" 表示这条虽然存在、但没有密钥，用不了（调用方看 available()）。
    """
    try:
        from .jev_presets import PRESETS

        jev = getattr(settings, "jev", None)
        if jev is None:
            return None
        use = str(getattr(jev, "use", "") or "").strip()
        if use in ("", "typesafe"):
            builtin = PRESETS.get("typesafe")
            return JevTarget(
                id="typesafe",
                name=str(getattr(builtin, "name", "") or "TypeSafe 官方"),
                protocol="systemone",
                url=str(getattr(jev, "api_url", "") or DEFAULT_API_URL).strip() or DEFAULT_API_URL,
                model=str(getattr(jev, "model", "") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
                key=_resolve_key(settings),
            )
        for ep in (getattr(settings, "jev_endpoints", ()) or ()):
            if str(getattr(ep, "id", "")) != use:
                continue
            return JevTarget(
                id=use,
                name=str(getattr(ep, "name", "") or use),
                protocol=str(getattr(ep, "protocol", "") or "systemone"),
                url=str(getattr(ep, "url", "") or ""),
                model=str(getattr(ep, "model", "") or ""),
                key=str(getattr(ep, "api_key", "") or "").strip(),
            )
        return None
    except Exception:
        logger.debug("算当前判断服务出错", exc_info=True)
        return None


# ----------------------------------------------------------------------
# 协议适配（纯函数）：请求拼装 / 应答折回 systemone 形状
# ----------------------------------------------------------------------


def _as_instruction_text(raw: Any) -> str:
    """instructions 是字符串就原样；别的类型 json.dumps（不猜它的意思）。"""
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False, default=str)


def _probabilities_map(items: Any) -> Dict[str, Any]:
    """OpenAI 形状的 probabilities 数组 → {标签: 概率}（保序）。

    每项取 value，没有就取 label，都没有就用序号当键（和文档里的
    "str(value or index)" 一致）。
    """
    out: Dict[str, Any] = {}
    if not isinstance(items, list):
        return out
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        key = item.get("value")
        if key is None:
            key = item.get("label")
        if key is None:
            key = index
        out[str(key)] = item.get("probability")
    return out


def build_body(protocol: str, model: str, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
    """按协议拼请求体（纯函数）。不认识的协议 / 题型直接 ValueError。"""
    if protocol == "systemone":
        return {"model": model, "state": state, "questions": questions}
    if protocol != "openai_decisions":
        raise ValueError(f"unknown protocol: {protocol}")
    items: List[Dict[str, Any]] = []
    for name, question in questions.items():
        if not isinstance(question, dict):
            raise ValueError(f"invalid question: {name}")
        qtype = question.get("type")
        instructions = _as_instruction_text(question.get("instructions", ""))
        if qtype == "noul":
            criteria = question.get("criteria")
            if isinstance(criteria, dict) and "true" in criteria and "false" in criteria:
                yes = _as_instruction_text(criteria.get("true"))
                no = _as_instruction_text(criteria.get("false"))
                instructions = f"{instructions}（是：{yes}；否：{no}）"
            items.append({"type": "predicate", "name": str(name), "instructions": instructions})
        elif qtype == "choice":
            criteria = question.get("criteria") or {}
            if not isinstance(criteria, dict):
                raise ValueError(f"invalid criteria: {name}")
            items.append({
                "type": "choice", "name": str(name), "instructions": instructions,
                "choices": [
                    {"value": str(label), "description": _as_instruction_text(desc)}
                    for label, desc in criteria.items()
                ],
            })
        elif qtype == "score":
            criteria = question.get("criteria") or []
            if not isinstance(criteria, (list, tuple)):
                raise ValueError(f"invalid criteria: {name}")
            levels = [{"label": c if isinstance(c, str) else _as_instruction_text(c)} for c in criteria]
            items.append({"type": "score", "name": str(name), "instructions": instructions, "levels": levels})
        else:
            raise ValueError(f"unknown question type: {qtype}")
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, default=str)
    return {"model": model, "input": text, "questions": items}


def normalize_response(protocol: str, data: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
    """把各家应答折回 systemone 形状 {"answers": {名字: …}}，交给 validate_answers。

    - systemone：本身就是这个形状，原样返回。Cloudflare Workers AI 的 REST 会包一层
      ``{"result": {...}, "success": …, "errors": […]}``，这里拆开；``success=false``
      就拿第一条 errors[].message 当错误（遮过密钥）。其余（TypeSafe 原厂、OpenRouter、
      OpenCode、Upstage、Inception、Liquid 等）不带这层，直接过。
    - openai_decisions：``answers`` 是数组，按 name 映射成 systemone 的字典；
      refusal 直接算失败。
    """
    if protocol == "systemone":
        if not isinstance(data, dict):
            raise ValueError("invalid response")
        if data.get("success") is False:
            errors = data.get("errors")
            message = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message") or "")
            raise ValueError(_sanitize_error(message or "systemone 调用失败", "", 200))
        payload = data
        if "answers" not in payload:
            result = payload.get("result")
            if isinstance(result, dict) and "answers" in result:
                payload = result
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise ValueError("invalid response")
        return payload
    if protocol != "openai_decisions":
        raise ValueError(f"unknown protocol: {protocol}")
    raw = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("invalid response")
    answers: Dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        atype = entry.get("type")
        if atype == "refusal":
            raise ValueError(f"refusal: {name}")
        if atype == "predicate":
            answers[name] = {"type": "noul", "noul": entry.get("probability")}
        elif atype == "choice":
            answers[name] = {
                "type": "choice",
                "choice": entry.get("choice"),
                "probabilities": _probabilities_map(entry.get("probabilities")),
                "confidence": entry.get("confidence"),
            }
        elif atype == "score":
            answers[name] = {
                "type": "score",
                "score": entry.get("score"),
                "probabilities": _probabilities_map(entry.get("probabilities")),
                "confidence": entry.get("confidence"),
            }
        else:
            raise ValueError(f"unknown answer type: {atype}")
    return {"answers": answers}


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
        # 熔断按目标各算：记着上次用的是哪个服务，换了就重新开始
        self._target_id = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 配置 / 密钥
    # ------------------------------------------------------------------

    def _settings(self) -> Settings:
        s = self._get_settings()
        if s is None:
            raise JevError("settings 还没准备好")
        return s

    def _target(self) -> Optional[JevTarget]:
        """现在打给谁；换了目标就把失败计数和熔断重置。"""
        target = resolve_target(self._settings())
        if target is None:
            return None
        if target.id != self._target_id:
            self._consecutive_failures = 0
            self._breaker_open_until = 0.0
            self._target_id = target.id
        return target

    def _key(self) -> str:
        """当前目标的密钥；不存在或不可用返回 ""，不抛异常。"""
        try:
            target = self._target()
            return str(target.key or "") if target is not None else ""
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
        """开着、当前目标存在且有密钥、没在熔断。"""
        try:
            s = self._settings()
            if not getattr(s.jev, "enabled", True):
                return False
            target = self._target()
            if target is None or not str(target.key or "").strip():
                return False
            return not self._breaker_open()
        except Exception:
            return False

    def current_target_info(self) -> Dict[str, Any]:
        """现在用哪个判断服务（给网页健康行）；**永远不含密钥**。"""
        info: Dict[str, Any] = {"id": "", "name": "", "protocol": "", "model": "", "key_set": False}
        try:
            target = self._target()
        except Exception:
            target = None
        if target is None:
            return info
        info["id"] = str(target.id)
        info["name"] = str(target.name)
        info["protocol"] = str(target.protocol)
        info["model"] = str(target.model)
        info["key_set"] = bool(str(target.key or "").strip())
        return info

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
        target = self._target()
        if target is None:
            return None
        key = str(target.key or "").strip()
        if not key:
            return None
        tag = f"[{target.id}] "

        if timeout_ms is None:
            timeout_ms = getattr(settings.jev, "timeout_ms", 1500)
        timeout_s = max(0.1, float(timeout_ms) / 1000.0)
        try:
            body = build_body(target.protocol, target.model, state, questions)
        except ValueError as exc:
            logger.warning("拼判断请求失败（%s）：%s", target.id, exc)
            return None

        async with self._lock:  # 熔断计数里有共享状态，串行请求
            # 再次检查（拿到锁后可能别人刚成功/失败过）
            if self._breaker_open():
                logger.debug("Jev 熔断中，跳过本次调用（purpose=%s）", purpose)
                return None
            start = clock.now()
            answers: Optional[Dict[str, Any]] = None
            try:
                resp = await self._client.post(
                    target.url,
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=timeout_s,
                )
                ms = int((clock.now() - start) * 1000)
                if resp.status_code != 200:
                    error_text = f"{tag}http_{resp.status_code}"
                    try:
                        body_text = resp.text[:200]
                    except Exception:
                        body_text = ""
                    if body_text:
                        error_text += f": {body_text}"
                    self._record_failure(ms, error_text, purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                try:
                    data = resp.json()
                except ValueError:
                    self._record_failure(ms, f"{tag}bad_json", purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                try:
                    normalized = normalize_response(target.protocol, data, questions)
                except ValueError as exc:
                    self._record_failure(ms, f"{tag}invalid_response: {exc}", purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                try:
                    answers = validate_answers(normalized, questions)
                except ValueError as exc:
                    self._record_failure(ms, f"{tag}invalid_answers: {exc}", purpose=purpose, group_id=group_id, state=state, key=key)
                    return None
                self._record_success()
                # 成功也落库
                self._log_success(
                    ms,
                    purpose=purpose,
                    group_id=group_id,
                    state=state,
                    answers_raw=normalized.get("answers", {}),
                    answers_validated=answers,
                    key=key,
                )
                return answers
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                ms = int((clock.now() - start) * 1000)
                self._record_failure(ms, f"{tag}{type(exc).__name__}", purpose=purpose, group_id=group_id, state=state, key=key)
                return None
            except Exception as exc:
                ms = int((clock.now() - start) * 1000)
                self._record_failure(ms, f"{tag}unexpected: {exc}", purpose=purpose, group_id=group_id, state=state, key=key)
                return None

    # ------------------------------------------------------------------
    # test_endpoint（网页「测试」按钮）
    # ------------------------------------------------------------------

    async def test_endpoint(
        self,
        *,
        protocol: str,
        url: str,
        model: str,
        key: str,
        timeout_ms: int = 8000,
    ) -> Dict[str, Any]:
        """真发一次小请求试试这个服务；**不动熔断、不写 judgments、不影响业务**。

        返回 {"ok": bool, "ms": int, "error": str, "answers": {…验证过的，元组转 list}}；
        error 已去掉密钥；HTTP 状态错误会带一小段正文（≤200 字）。
        """
        from .jev_presets import PROTOCOLS

        started = clock.now()

        def _fail(message: str, *, ms: int = -1) -> Dict[str, Any]:
            return {
                "ok": False,
                "ms": int(ms if ms >= 0 else (clock.now() - started) * 1000),
                "error": _sanitize_error(message or "测试失败", key),
                "answers": {},
            }

        if protocol not in PROTOCOLS:
            return _fail(f"协议只认 {' / '.join(PROTOCOLS)}")
        if not str(url or "").strip() or not str(model or "").strip():
            return _fail("地址和模型名都要填")
        try:
            body = build_body(protocol, str(model), _TEST_STATE, _TEST_QUESTIONS)
        except ValueError as exc:
            return _fail(f"请求拼不出来：{exc}")
        try:
            resp = await self._client.post(
                str(url),
                json=body,
                headers={"Authorization": f"Bearer {str(key)}"},
                timeout=max(0.1, float(timeout_ms) / 1000.0),
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return _fail(type(exc).__name__)
        except Exception as exc:
            return _fail(f"unexpected: {exc}")
        ms = int((clock.now() - started) * 1000)
        if resp.status_code != 200:
            try:
                short = resp.text[:200]
            except Exception:
                short = ""
            return _fail(f"http_{resp.status_code}: {short}" if short else f"http_{resp.status_code}", ms=ms)
        try:
            data = resp.json()
        except ValueError:
            return _fail("返回不是 JSON", ms=ms)
        try:
            normalized = normalize_response(protocol, data, _TEST_QUESTIONS)
            answers = validate_answers(normalized, _TEST_QUESTIONS)
        except ValueError as exc:
            return _fail(f"答案不合法：{exc}", ms=ms)
        out: Dict[str, Any] = {}
        for name, value in answers.items():
            out[name] = list(value) if isinstance(value, tuple) else value
        return {"ok": True, "ms": ms, "error": "", "answers": out}

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
