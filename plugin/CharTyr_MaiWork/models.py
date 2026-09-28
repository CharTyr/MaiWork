"""OpenAI 兼容客户端 + 模型设置（网页可改）+ 用量统计 + 最近请求日志。

- 设置来源：config.toml 的 [models]（网页「设置 → 模型」改的也是它——直写文件，
  数据库不再存配置覆盖层）；都没有 source="none"。checked_at / available
  （测试连接的结果）不是配置，存 kv["models.checked"]，GET 时并回来显示。
- 密钥只进不出：返回网页的任何结构都不含密钥（用 key_set 布尔值代替）；错误消息、
  日志、usage.error、model_calls 里都不许出现密钥（_redact 统一遮掉）。
- chat() 可重试错误（网络错误、429、408、5xx）同一模型最多「1 + retries」次；
  非 429 的两次之间等 retry_delay_s 秒；429 改由端点级冷却决定等待
  （Retry-After 秒数 / HTTP 日期，封顶 120 秒；没有就按连续 429 次数
  10/20/40/60 秒退避、封顶 60 秒，加 ±20% 抖动）；
  用完再换备用模型，备用同样规则；其他 4xx 不重试、不换备用，直接抛。
  chat(retries=n) 可临时覆盖设置里的重试次数（后台主循环里直接 await 的调用传 1）。
- 端点级限流（EndpointThrottle，状态只在进程内存、不落库）：同一 base_url
  （规范化：小写、去末尾斜杠）同一时刻最多 max_concurrency 个在途请求
  （[models] 可选字段，缺省 2），超出的排队不报错；任何一次 429 → 整个端点冷却，
  冷却期同端点的新请求和重试都先等到冷却结束；成功一次后连续 429 计数清零；
  [models] 可选字段 max_rpm > 0 时按端点做 60 秒滑动窗口限速（缺省 0 = 关）。
- 每次尝试（含失败）写一条 usage + 一条 model_calls（管理员网页看，docs/07 §9）。
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

import httpx

from . import clock
from .config import Settings
from .store import Store

logger = logging.getLogger("maiwork.models")

_BEARER_RE = re.compile(r"(?i)Bearer\s+\S+")
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{3,}")
_ERR_MAX = 300

# model_calls 表各列截断上限（docs/07 §9.4）
_LOG_ERR_MAX = 1000         # error
_LOG_CONTENT_MAX = 4000     # message.content
_LOG_TOOL_ARGS_MAX = 500    # 请求里 message.tool_calls 的 arguments
_LOG_REQUEST_MAX = 80 * 1024  # 整份 request JSON
_LOG_RESPONSE_TEXT_MAX = 20000  # response.text
_LOG_TOOL_OUT_ARGS_MAX = 1000   # 响应里 tool_calls 的 arguments
_LOG_PRUNE_EVERY = 200      # 每写多少行顺手清一次超量/超期
_RETRY_AFTER_CAP_S = 120.0  # 429 冷却里 Retry-After 的封顶
_RETRY_DELAY_CAP_S = 60.0   # 非 429 重试等待的封顶（retry_delay_s 本身 1~60）
_COOLDOWN_BACKOFF_S = (10.0, 20.0, 40.0, 60.0)  # 没有 Retry-After 时按连续 429 次数退避
_COOLDOWN_CAP_S = 60.0      # 退避封顶
_COOLDOWN_JITTER = 0.2      # 退避 ±20% 抖动
_RPM_WINDOW_S = 60.0        # 每分钟上限的滑动窗口
_MAX_CONCURRENCY_DEFAULT = 2  # 每个端点同一时刻最多几个在途请求


async def _SLEEP(seconds: float) -> None:
    """重试前等待；单独一个模块级函数方便测试换成不等真秒数。"""
    await asyncio.sleep(seconds)


def _NOW() -> float:
    """限流用的当前时间；单独一个函数方便测试换成假时钟。"""
    return clock.now()


def _jitter(seconds: float) -> float:
    """±20% 抖动（多个等待者别在同一时刻一齐醒来）。"""
    if seconds <= 0:
        return 0.0
    return seconds * random.uniform(1 - _COOLDOWN_JITTER, 1 + _COOLDOWN_JITTER)


def _parse_retry_after(value: Any, now: float) -> float:
    """Retry-After 头 → 还差几秒；秒数和 HTTP 日期两种都认，解析不了给 0。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if when is None:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        return max(0.0, when.timestamp() - now)
    except (OverflowError, OSError, ValueError):
        return 0.0


def _endpoint_host(endpoint: str) -> str:
    """日志里只写主机名（不打完整地址、更不会打密钥）。"""
    try:
        return urlsplit(endpoint).netloc or endpoint
    except ValueError:
        return endpoint


def _limit_from(models_cfg: Any, name: str, default: int, low: int, high: int) -> int:
    """读可选的 max_concurrency / max_rpm；字段不存在、类型不对或越界都回默认。"""
    value = getattr(models_cfg, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if low <= value <= high else default


def _redact(text: str, secrets: list[str]) -> str:
    """把错误原文里的密钥遮掉：已知密钥、"Bearer xxx"、"sk-..." 形式；截断到 300 字。"""
    out = str(text or "")
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    out = _BEARER_RE.sub("Bearer ***", out)
    out = _SK_RE.sub("sk-***", out)
    if len(out) > _ERR_MAX:
        out = out[:_ERR_MAX]
    return out


def _redact_full(text: Any, secrets: list[str]) -> str:
    """同 _redact 但不截断（截断由调用方按各自上限做）；纯文本统一遮罩入口。"""
    out = "" if text is None else str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    out = _BEARER_RE.sub("Bearer ***", out)
    out = _SK_RE.sub("sk-***", out)
    return out


@dataclass
class _EndpointState:
    """一个端点的限流状态（只在内存、只属于一个事件循环）。"""

    loop: Any
    limit: int
    sem: asyncio.Semaphore
    lock: asyncio.Lock
    cooldown_until: float = 0.0
    consecutive_429: int = 0
    sends: list[float] = field(default_factory=list)


class EndpointThrottle:
    """端点级限流：并发上限 + 429 冷却 + 可选每分钟上限（状态只在进程内存）。

    - 端点 = base_url 规范化（去空格、小写、去末尾斜杠）；不同端点各算各的。
    - 冷却 / 每分钟上限只算一次等待就往下走，不循环重算（假时钟下循环会打转，
      真时钟下也不差那几毫秒）。
    - asyncio.Semaphore / Lock 绑定事件循环：换了循环（测试里每个测试一个新循环）
      就重建状态。
    - 等待用模块级 _SLEEP / _NOW（可注入）；取消时 CancelledError 原样抛出。
    """

    def __init__(self, *, now=None, sleep=None) -> None:
        self._states: dict[str, _EndpointState] = {}
        self._now = now
        self._sleep = sleep

    def _now_s(self) -> float:
        return self._now() if self._now is not None else _NOW()

    async def _sleep_s(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
        else:
            await _SLEEP(seconds)

    @staticmethod
    def normalize(base_url: str) -> str:
        """端点键：去空格、小写、去末尾斜杠。"""
        return str(base_url or "").strip().rstrip("/").lower()

    def _state(self, endpoint: str, concurrency: int) -> _EndpointState:
        loop = asyncio.get_running_loop()
        limit = max(1, int(concurrency))
        st = self._states.get(endpoint)
        if st is not None and st.loop is loop:
            if st.limit != limit:  # 设置改了并发上限：只换名额，冷却 / 计数保留
                st.limit = limit
                st.sem = asyncio.Semaphore(limit)
            return st
        st = _EndpointState(loop=loop, limit=limit, sem=asyncio.Semaphore(limit), lock=asyncio.Lock())
        self._states[endpoint] = st
        return st

    @staticmethod
    def _trim(st: _EndpointState, now: float) -> None:
        """每分钟窗口只留最近 60 秒里发出去的请求。"""
        cutoff = now - _RPM_WINDOW_S
        st.sends = [ts for ts in st.sends if ts > cutoff]

    def _wait_s(self, st: _EndpointState, max_rpm: int) -> float:
        """还要等几秒（冷却 + 每分钟窗口），0 = 现在就能发。调用方持锁。"""
        now = self._now_s()
        self._trim(st, now)
        wait = max(0.0, st.cooldown_until - now)
        if max_rpm > 0 and len(st.sends) >= max_rpm:
            wait = max(wait, st.sends[0] + _RPM_WINDOW_S - now)
        return wait

    @asynccontextmanager
    async def slot(self, base_url: str, *, concurrency: int, max_rpm: int):
        """占一个并发名额，先等 429 冷却 / 每分钟额度，发完请求（退出）才释放名额。"""
        st = self._state(self.normalize(base_url), concurrency)
        async with st.sem:
            async with st.lock:
                wait = self._wait_s(st, max_rpm)
            if wait > 0:
                await self._sleep_s(wait)
            async with st.lock:
                now = self._now_s()
                self._trim(st, now)
                st.sends.append(now)
            yield

    async def wait_cooldown(self, base_url: str, *, concurrency: int = _MAX_CONCURRENCY_DEFAULT) -> None:
        """只等 429 冷却：列模型 / 测连接用（不占并发名额、不算每分钟额度）。"""
        st = self._state(self.normalize(base_url), concurrency)
        async with st.lock:
            wait = max(0.0, st.cooldown_until - self._now_s())
        if wait > 0:
            await self._sleep_s(wait)

    def note_429(
        self, base_url: str, retry_after_s: float | None, *, concurrency: int = _MAX_CONCURRENCY_DEFAULT
    ) -> float:
        """记一次 429：整个端点进冷却，返回本次冷却秒数（不打密钥、只打主机名）。"""
        endpoint = self.normalize(base_url)
        st = self._state(endpoint, concurrency)
        now = self._now_s()
        st.consecutive_429 += 1
        if retry_after_s and retry_after_s > 0:
            cooldown = min(float(retry_after_s), _RETRY_AFTER_CAP_S)
        else:
            step = _COOLDOWN_BACKOFF_S[min(st.consecutive_429 - 1, len(_COOLDOWN_BACKOFF_S) - 1)]
            cooldown = min(_jitter(step), _COOLDOWN_CAP_S)
        st.cooldown_until = max(st.cooldown_until, now + cooldown)
        logger.info(
            "模型端点 %s 触发限流（429），冷却 %.0f 秒（连续第 %d 次）",
            _endpoint_host(endpoint), cooldown, st.consecutive_429,
        )
        return cooldown

    def note_success(self, base_url: str, *, concurrency: int = _MAX_CONCURRENCY_DEFAULT) -> None:
        """成功一次：连续 429 计数清零（已经过去的冷却不撤回）。"""
        st = self._state(self.normalize(base_url), concurrency)
        st.consecutive_429 = 0

    def cooldown_remaining(self, base_url: str) -> float:
        """当前冷却还剩几秒（日志用）。"""
        st = self._states.get(self.normalize(base_url))
        if st is None:
            return 0.0
        return max(0.0, st.cooldown_until - self._now_s())


class ModelError(Exception):
    """模型调用失败；message 已去掉密钥。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ModelSettings:
    base_url: str
    main: str
    main_backup: str
    worker: str
    worker_backup: str
    key_set: bool
    source: str  # "config" | "none"（网页改的也写 config.toml，所以只有这两种）
    checked_at: float = 0.0
    available: list[str] = field(default_factory=list)
    retries: int = 5         # 同一模型可重试失败最多几次（0~10）
    retry_delay_s: int = 10  # 两次重试之间等几秒（1~60）
    max_concurrency: int = 2  # 同一端点同时最多几个在途请求（1~8）
    max_rpm: int = 0          # 同一端点每分钟最多几次（0 = 不限）

    def ready(self) -> bool:
        """端点、密钥、主模型、子 agent 模型都有。"""
        return bool(self.base_url and self.key_set and self.main and self.worker)

    def public(self) -> dict:
        """给网页的字典（不含密钥）。"""
        return {
            "base_url": self.base_url,
            "key_set": self.key_set,
            "main": self.main,
            "main_backup": self.main_backup,
            "worker": self.worker,
            "worker_backup": self.worker_backup,
            "source": self.source,
            "checked_at": self.checked_at,
            "available": list(self.available),
            "ready": self.ready(),
            "retries": self.retries,
            "retry_delay_s": self.retry_delay_s,
            "max_concurrency": self.max_concurrency,
            "max_rpm": self.max_rpm,
        }


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict]
    model: str
    prompt_tokens: int
    completion_tokens: int
    raw_message: dict


def _valid_int(value: Any, low: int, high: int) -> bool:
    """必须是干净整数（bool、float、数字字符串都不行；None 表示没传不算错误）。"""
    if value is None:
        return True
    return _int_in(value, low, high)


def _int_in(value: Any, low: int, high: int) -> bool:
    """干净整数且在 [low, high]；None / float / str / bool 都不算。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return low <= value <= high


def _validate(patch: dict) -> list[str]:
    """保存前校验；返回中文问题清单（空 = 通过）。"""
    problems: list[str] = []
    base_url = str(patch.get("base_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        problems.append("端点地址必须是 http(s) 开头的网址")
    main = str(patch.get("main") or "").strip()
    worker = str(patch.get("worker") or "").strip()
    if not main:
        problems.append("主模型必填")
    if not worker:
        problems.append("子 agent 模型必填")
    main_backup = str(patch.get("main_backup") or "").strip()
    worker_backup = str(patch.get("worker_backup") or "").strip()
    if main_backup and main_backup == main:
        problems.append("主模型备用不能和主模型相同")
    if worker_backup and worker_backup == worker:
        problems.append("子 agent 模型备用不能和子 agent 模型相同")
    if patch.get("retries") is not None and not _valid_int(patch.get("retries"), 0, 10):
        problems.append("重试次数（retries）必须是 0~10 的整数")
    if patch.get("retry_delay_s") is not None and not _valid_int(patch.get("retry_delay_s"), 1, 60):
        problems.append("重试间隔（retry_delay_s）必须是 1~60 的整数（秒）")
    if patch.get("max_concurrency") is not None and not _valid_int(patch.get("max_concurrency"), 1, 8):
        problems.append("同时请求数（max_concurrency）必须是 1~8 的整数")
    if patch.get("max_rpm") is not None and not _valid_int(patch.get("max_rpm"), 0, 600):
        problems.append("每分钟上限（max_rpm）必须是 0~600 的整数（0 = 不限）")
    return problems


class Models:
    def __init__(
        self,
        store: Store,
        get_settings: Callable[[], Settings],
        *,
        transport=None,
        config_writer: Any = None,
    ) -> None:
        self._store = store
        self._get_settings = get_settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # 端点级限流（并发上限 / 429 冷却 / 每分钟上限）；状态只在内存、不落库
        self._throttle = EndpointThrottle()
        # settings() 内存缓存；(Settings 对象 id, kv["models.checked"] 版本标记, ModelSettings)
        self._cache: tuple[int, Any, ModelSettings] | None = None
        # 写 config.toml 的钩子：app 注入（保存 → 写文件 → 立刻在本进程应用）；
        # 测试没注入时保存直接报错（网页链路一定有 app 注入）。
        self._config_writer = config_writer

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def settings(self) -> ModelSettings:
        """config.toml 的 [models] 是唯一来源（网页保存也写它）；结果内存缓存。

        get_settings 回调返回的 Settings 对象变了（配置热更新）就重算；
        kv["models.checked"]（测试连接的回写）变了也重算。
        """
        settings = self._get_settings()
        try:
            checked = self._store.kv_get("models.checked")
        except Exception:
            checked = None
        if self._cache is not None and self._cache[0] == id(settings) and self._cache[1] == checked:
            return self._cache[2]
        computed = self._compute(settings, checked)
        self._cache = (id(settings), checked, computed)
        return computed

    def _compute(self, settings: Settings, checked: Any) -> ModelSettings:
        cfg = settings.models.__dict__ if settings.models else {}
        base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        main = str(cfg.get("main") or "").strip()
        main_backup = str(cfg.get("main_backup") or "").strip()
        worker = str(cfg.get("worker") or "").strip()
        worker_backup = str(cfg.get("worker_backup") or "").strip()
        source = "config"
        if not (base_url or main or worker or main_backup or worker_backup or str(cfg.get("api_key") or "")):
            source = "none"
        # checked_at / available：测试连接的回写（不是配置，存 kv），端点一致才并进来显示
        checked_at = 0.0
        available: list[str] = []
        if isinstance(checked, dict):
            if str(checked.get("base_url") or "").strip().rstrip("/") == base_url and base_url:
                try:
                    checked_at = float(checked.get("checked_at") or 0)
                except (TypeError, ValueError):
                    checked_at = 0.0
                raw_available = checked.get("available")
                if isinstance(raw_available, list):
                    available = [str(x) for x in raw_available]
        retries = cfg.get("retries")
        if not _int_in(retries, 0, 10):
            retries = 5
        retry_delay_s = cfg.get("retry_delay_s")
        if not _int_in(retry_delay_s, 1, 60):
            retry_delay_s = 10
        max_concurrency = cfg.get("max_concurrency")
        if not _int_in(max_concurrency, 1, 8):
            max_concurrency = 2
        max_rpm = cfg.get("max_rpm")
        if not _int_in(max_rpm, 0, 600):
            max_rpm = 0
        return ModelSettings(
            base_url=base_url,
            main=main,
            main_backup=main_backup,
            worker=worker,
            worker_backup=worker_backup,
            key_set=bool(self._current_key(settings)),
            source=source,
            checked_at=checked_at,
            available=available,
            retries=int(retries),
            retry_delay_s=int(retry_delay_s),
            max_concurrency=int(max_concurrency),
            max_rpm=int(max_rpm),
        )

    def _current_key(self, settings: Settings) -> str:
        """密钥只认 config.toml 的 [models] api_key（数据库覆盖层已废弃）。"""
        return str(settings.models.api_key or "")

    def save(self, patch: dict) -> ModelSettings:
        """保存网页设置 = 写进 config.toml 的 [models]（明文密钥，用户明确同意）。

        校验失败抛 ValueError(中文说明)。api_key 为空 / None / 不传表示不改。
        写文件后由 config_writer 钩子立刻在本进程应用（app.apply_config_text）。
        checked_at / available 不进 config.toml，存 kv["models.checked"]。
        """
        from . import config_file

        patch = dict(patch or {})
        problems = _validate(patch)
        if problems:
            raise ValueError("；".join(problems))
        if self._config_writer is None:
            raise ValueError("保存通道还没就位（插件没完全启动？），稍后再试")

        current = self.settings()
        writes: dict[str, Any] = {
            "models.base_url": str(patch["base_url"]).strip().rstrip("/"),
            "models.main": str(patch["main"]).strip(),
            "models.main_backup": str(patch.get("main_backup") or "").strip(),
            "models.worker": str(patch["worker"]).strip(),
            "models.worker_backup": str(patch.get("worker_backup") or "").strip(),
        }
        # retries / retry_delay_s / 限流两项：没传保留当前有效值（别把网页上没动的重试清零）
        for key in ("retries", "retry_delay_s", "max_concurrency", "max_rpm"):
            if patch.get(key) is not None:
                writes[f"models.{key}"] = int(patch[key])
            else:
                writes[f"models.{key}"] = int(getattr(current, key))
        api_key = str(patch.get("api_key") or "").strip()
        if api_key:
            writes["models.api_key"] = api_key
        # checked_at / available：存 kv（不是配置）；没传保留旧值
        checked_value: dict[str, Any] = {}
        old_checked = self._store.kv_get("models.checked")
        if isinstance(old_checked, dict):
            checked_value.update(old_checked)
        if "checked_at" in patch and patch["checked_at"] is not None:
            checked_value["checked_at"] = float(patch["checked_at"])
        if "available" in patch and patch["available"] is not None:
            checked_value["available"] = [str(x) for x in patch["available"]]
        checked_value["base_url"] = writes["models.base_url"]

        # 1) 写 config.toml（失败抛错 → 数据库不动，不留半截）
        result = self._config_writer(writes)

        # 2) 文件写成功后才落 kv / 事件（payload 不含密钥）
        now = clock.now()
        with self._store.tx() as conn:
            self._store.kv_set(conn, "models.checked", checked_value)
            self._store.event(
                conn,
                "models.saved",
                payload={
                    "base_url": writes["models.base_url"],
                    "main": writes["models.main"],
                    "main_backup": writes["models.main_backup"],
                    "worker": writes["models.worker"],
                    "worker_backup": writes["models.worker_backup"],
                    "retries": writes["models.retries"],
                    "retry_delay_s": writes["models.retry_delay_s"],
                    "key_set": bool(api_key) or bool(self._current_key(self._get_settings())),
                    "ts": now,
                },
            )
        self._cache = None
        out = self.settings()
        logger.info("模型设置已写进 config.toml：base_url=%s", writes["models.base_url"])
        return out

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_models(self, base_url: str, api_key: str = "") -> list[str]:
        """GET {base_url}/models，返回 data[].id。api_key 空则用已存的。

        没有密钥抛 ModelError。成功时不自动保存。
        """
        key = api_key or self._current_key(self._get_settings())
        if not key:
            raise ModelError("没有可用的模型密钥")
        url = str(base_url or "").strip().rstrip("/") + "/models"
        endpoint = self._throttle.normalize(base_url)
        conc = _limit_from(
            getattr(self._get_settings(), "models", None), "max_concurrency", _MAX_CONCURRENCY_DEFAULT, 1, 64
        )
        # 列模型 / 测连接：端点还在 429 冷却就直接说还要等几秒（别马上再打，也别让网页干转圈）；
        # 不占并发名额、不算每分钟额度
        left = self._throttle.cooldown_remaining(endpoint)
        if left > 0:
            raise ModelError(f"这个端点刚被限流（429），约 {max(1, round(left))} 秒后再试")
        client = self._get_client()
        try:
            resp = await client.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        except httpx.HTTPError as e:
            raise ModelError(_redact(f"请求模型列表失败：{e}", [key])) from None
        if resp.status_code == 429:
            self._throttle.note_429(
                endpoint, _parse_retry_after(resp.headers.get("Retry-After"), _NOW()), concurrency=conc
            )
        elif resp.status_code == 200:
            self._throttle.note_success(endpoint, concurrency=conc)
        if resp.status_code != 200:
            raise ModelError(
                _redact(f"模型列表接口返回 {resp.status_code}：{resp.text}", [key]),
                status=resp.status_code,
            )
        try:
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError
            return [str(item["id"]) for item in items if isinstance(item, dict) and "id" in item]
        except ValueError:
            raise ModelError("模型列表返回格式不对") from None

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        role: Literal["main", "worker"],
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        purpose: str = "",
        group_id: str = "",
        task_id: str = "",
        timeout: float = 120,
        retries: int | None = None,
    ) -> ChatResult:
        """调一次模型。

        重试规则：可重试的错误 = 网络错误（httpx.HTTPError）、429、408、5xx。
        同一个模型最多「1 + retries」次；非 429 的两次之间等 retry_delay_s 秒；
        429 由端点级冷却决定等待（Retry-After 秒数 / HTTP 日期，封顶 120 秒；
        没有就按连续 429 次数 10/20/40/60 秒退避、封顶 60 秒，加 ±20% 抖动）；
        用完再换备用模型，备用同样规则。其他 4xx 不重试、不换备用，直接抛。
        retries=None 用设置里的；主循环里直接 await 的调用传 1（别让循环卡几分钟）。
        每次尝试（成功或失败）写一条 usage + 一条 model_calls。
        发请求前过端点限流门：并发上限 max_concurrency（缺省 2）+ 429 冷却 +
        每分钟上限 max_rpm（缺省 0 = 关）；等待可取消，状态只在内存。
        """
        settings = self._get_settings()  # 缓存判热更新用
        s = self.settings()
        if not s.ready():
            raise ModelError("模型还没配好")
        key = self._current_key(settings)

        if role == "main":
            candidates = [m for m in (s.main, s.main_backup) if m]
        else:
            candidates = [m for m in (s.worker, s.worker_backup) if m]

        max_tries = 1 + (s.retries if retries is None else max(0, int(retries)))
        delay_s = float(s.retry_delay_s)
        secret_keys = [k for k in (key,) if k]
        # 可选设置（字段由 config 提供，可能还没有）：端点并发上限 / 每分钟上限
        models_cfg = getattr(settings, "models", None)
        max_conc = _limit_from(models_cfg, "max_concurrency", _MAX_CONCURRENCY_DEFAULT, 1, 64)
        max_rpm = _limit_from(models_cfg, "max_rpm", 0, 0, 1_000_000)
        endpoint = s.base_url

        client = self._get_client()
        url = s.base_url + "/chat/completions"
        last_err: ModelError | None = None
        # 快照一份请求日志底稿：messages 统一截断+遮罩只算一次，每次尝试直接存
        log_request = self._build_log_request(messages, tools, json_mode, secret_keys)
        attempt = 0
        for pos, model in enumerate(candidates):
            try_n = 0
            while try_n < max_tries:
                try_n += 1
                attempt += 1
                if try_n > 1:
                    if getattr(last_err, "status", None) == 429:
                        # 429 的等待交给端点冷却门（本端点所有请求一起等，别各等各的）
                        logger.warning(
                            "模型 %s 第 %d 次尝试失败（%s），端点冷却中（剩约 %.0f 秒）后重试（%d/%d）",
                            model, try_n - 1, last_err,
                            self._throttle.cooldown_remaining(endpoint), try_n, max_tries,
                        )
                    else:
                        wait = min(max(delay_s, 0.0), _RETRY_DELAY_CAP_S)
                        logger.warning(
                            "模型 %s 第 %d 次尝试失败（%s），%.0f 秒后重试（%d/%d）",
                            model, try_n - 1, last_err, wait, try_n, max_tries,
                        )
                        await _SLEEP(wait)
                elif pos > 0:
                    logger.warning("模型 %s 失败（%s），换备用 %s 再试", candidates[pos - 1], last_err, model)
                body: dict[str, Any] = {"model": model, "messages": messages}
                if tools:
                    body["tools"] = tools
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                start = clock.now()
                try:
                    async with self._throttle.slot(endpoint, concurrency=max_conc, max_rpm=max_rpm):
                        resp = await client.post(
                            url,
                            json=body,
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=timeout,
                        )
                except httpx.HTTPError as e:
                    ms = int((clock.now() - start) * 1000)
                    detail = str(e).strip()
                    last_err = ModelError(_redact(f"网络错误（{type(e).__name__}）{('：' + detail) if detail else ''}", [key]))
                    self._log_attempt(
                        model, role, attempt, ok=False, status=0, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=last_err.message,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id,
                    )
                    continue
                ms = int((clock.now() - start) * 1000)
                if resp.status_code >= 500 or resp.status_code in (429, 408):
                    last_err = ModelError(
                        _redact(f"端点返回 {resp.status_code}：{resp.text}", [key]),
                        status=resp.status_code,
                    )
                    if resp.status_code == 429:
                        # 整个端点进冷却：Retry-After（秒数 / HTTP 日期）优先，封顶 120 秒；
                        # 没有就按连续 429 次数退避（10/20/40/60，封顶 60，±20% 抖动）
                        self._throttle.note_429(
                            endpoint,
                            _parse_retry_after(resp.headers.get("Retry-After"), _NOW()),
                            concurrency=max_conc,
                        )
                    self._log_attempt(
                        model, role, attempt, ok=False, status=resp.status_code, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=last_err.message,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id,
                    )
                    continue
                if resp.status_code != 200:
                    # 其他 4xx：不重试、不换备用，直接抛
                    err = ModelError(
                        _redact(f"端点返回 {resp.status_code}：{resp.text}", [key]),
                        status=resp.status_code,
                    )
                    self._log_attempt(
                        model, role, attempt, ok=False, status=resp.status_code, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=err.message,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id,
                    )
                    raise err
                self._throttle.note_success(endpoint, concurrency=max_conc)
                try:
                    result = self._parse_chat(resp.json(), model)
                except ModelError as e:
                    msg = _redact(str(e), [key])
                    self._log_attempt(
                        model, role, attempt, ok=False, status=200, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=msg,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id,
                    )
                    raise
                log_response = self._build_log_response(resp, result, secret_keys)
                self._log_attempt(
                    model, role, attempt, ok=True, status=200, ms=ms,
                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                    error="", request=log_request, response=log_response, keys=secret_keys,
                    purpose=purpose, group_id=group_id, task_id=task_id,
                )
                return result
        assert last_err is not None
        raise last_err

    # ------------------------------------------------------------------
    # 请求日志（model_calls）：每次尝试一行，密钥绝不入库
    # ------------------------------------------------------------------

    @staticmethod
    def _build_log_request(messages: list[dict], tools: list[dict] | None, json_mode: bool, keys: list[str]) -> str:
        """把这次调用请求底稿拼成 JSON 字符串（只拼一次，每次尝试复用）。

        只存 messages（content 截 _LOG_CONTENT_MAX；tool_calls 只要名字+参数截 _LOG_TOOL_ARGS_MAX）、
        工具名列表、json_mode——不存 headers / 模型名中可能带的服务商名之外，一切文本过遮罩。
        整份 JSON 截 _LOG_REQUEST_MAX（截断后不再直接 json.loads，详情接口自己 JSON 修复）。
        """
        out_messages: list[dict] = []
        for m in messages if isinstance(messages, list) else []:
            if not isinstance(m, dict):
                continue
            entry: dict[str, Any] = {
                "role": _redact_full(m.get("role"), keys),
                "content": _redact_full(m.get("content"), keys)[:_LOG_CONTENT_MAX],
            }
            tcs = m.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                entry["tool_calls"] = [
                    {
                        "name": _redact_full((tc.get("function") or {}).get("name") if isinstance(tc, dict) else None, keys),
                        "arguments": _redact_full(
                            (tc.get("function") or {}).get("arguments") if isinstance(tc, dict) else None, keys
                        )[:_LOG_TOOL_ARGS_MAX],
                    }
                    for tc in tcs
                ]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = _redact_full(m.get("tool_call_id"), keys)
            if m.get("name"):
                entry["name"] = _redact_full(m.get("name"), keys)
            out_messages.append(entry)
        tool_names: list[str] = []
        for t in tools or []:
            if isinstance(t, dict):
                name = (t.get("function") or {}).get("name")
                if name:
                    tool_names.append(_redact_full(name, keys))
        payload = json.dumps(
            {"messages": out_messages, "tools": tool_names, "json_mode": bool(json_mode)},
            ensure_ascii=False,
        )
        return payload[:_LOG_REQUEST_MAX]

    @staticmethod
    def _build_log_response(resp: httpx.Response, result: ChatResult, keys: list[str]) -> str:
        """响应 JSON：text（截 _LOG_RESPONSE_TEXT_MAX）/tool_calls（name+arguments 截 1000）/finish_reason。"""
        finish_reason = ""
        try:
            data = resp.json()
            finish_reason = str((data.get("choices") or [{}])[0].get("finish_reason") or "")
        except Exception:
            finish_reason = ""
        out_tcs: list[dict] = []
        for tc in result.tool_calls:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if not isinstance(fn, dict):
                continue
            out_tcs.append(
                {
                    "name": _redact_full(fn.get("name"), keys),
                    "arguments": _redact_full(fn.get("arguments"), keys)[:_LOG_TOOL_OUT_ARGS_MAX],
                }
            )
        return json.dumps(
            {"text": _redact_full(result.text, keys)[:_LOG_RESPONSE_TEXT_MAX],
             "tool_calls": out_tcs,
             "finish_reason": finish_reason},
            ensure_ascii=False,
        )

    def _log_attempt(
        self,
        model: str,
        role: str,
        attempt: int,
        *,
        ok: bool,
        status: int,
        ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        error: str,
        request: str,
        response: str | None,
        keys: list[str],
        purpose: str,
        group_id: str,
        task_id: str,
    ) -> None:
        """每次尝试写两条账：usage（原有）+ model_calls（管理员网页看）。写失败只记日志。"""
        now = clock.now()
        error_db = _redact_full(error, keys)[:_LOG_ERR_MAX]
        # 1) usage（原路）
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
                    " prompt_tokens, completion_tokens, ok, ms, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        clock.day_key(now),
                        str(role),
                        str(model),
                        str(purpose or ""),
                        str(group_id or ""),
                        str(task_id or ""),
                        int(prompt_tokens),
                        int(completion_tokens),
                        1 if ok else 0,
                        int(ms),
                        str(error or ""),
                    ),
                )
        except Exception:
            logger.exception("写 usage 失败（model=%s ok=%s）", model, ok)
        # 2) model_calls（请求日志）
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO model_calls (ts, purpose, role, model, group_id, task_id, attempt, ok,"
                    " status, ms, prompt_tokens, completion_tokens, error, request, response)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        str(purpose or ""),
                        str(role),
                        str(model),
                        str(group_id or ""),
                        str(task_id or ""),
                        int(attempt),
                        1 if ok else 0,
                        int(status),
                        int(ms),
                        int(prompt_tokens),
                        int(completion_tokens),
                        error_db,
                        str(request or "{}"),
                        str(response) if response else "{}",
                    ),
                )
                row = conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()
                count = int(row["c"]) if row is not None else 0
                if count > 0 and count % _LOG_PRUNE_EVERY == 0:
                    removed = self._store._prune_model_calls_tx(conn, now)
                    if removed:
                        logger.info("model_calls 顺手清理了 %d 行（每 %d 行一次）", removed, _LOG_PRUNE_EVERY)
        except Exception:
            logger.exception("写 model_calls 失败（model=%s ok=%s）", model, ok)

    @staticmethod
    def _parse_chat(data: dict, model: str) -> ChatResult:
        """解析 choices[0].message 的 content / tool_calls，usage 的 tokens（缺就 0）。"""
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("模型返回格式不对：缺少 choices[0].message") from None
        if not isinstance(message, dict):
            raise ModelError("模型返回格式不对：message 不是对象")
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ChatResult(
            text=str(content) if content is not None else "",
            tool_calls=[dict(t) for t in tool_calls] if isinstance(tool_calls, list) else [],
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw_message=message,
        )

    # ------------------------------------------------------------------
    # 用量
    # ------------------------------------------------------------------

    def usage_today(self) -> dict:
        """按北京时间今天汇总 {"main", "worker", "calls", "errors"}。"""
        day = clock.day_key(clock.now())
        row = self._store.read().execute(
            "SELECT COALESCE(SUM(CASE WHEN role='main' THEN prompt_tokens + completion_tokens END), 0) AS main_tokens,"
            " COALESCE(SUM(CASE WHEN role='worker' THEN prompt_tokens + completion_tokens END), 0) AS worker_tokens,"
            " COUNT(*) AS calls,"
            " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) AS errors"
            " FROM usage WHERE day = ?",
            (day,),
        ).fetchone()
        return {
            "main": int(row["main_tokens"]),
            "worker": int(row["worker_tokens"]),
            "calls": int(row["calls"]),
            "errors": int(row["errors"]),
        }
