"""模型客户端 + 模型设置（网页可改）+ 用量统计 + 最近请求日志。

2026-10 改版 1a（计划见 docs/12）：

- 三层配置：config.toml 的 `[[endpoints]]` 端点 + `[[model_list]]` 模型库 +
  专岗自选（kv `agents.profiles` 的 model/effort/backup，main/news/idea/goal/task）。
  旧 `[models]` 四槽是过渡兜底：model_list 空且旧四槽有值时按旧规则干
  （启动迁移一次搬走；写新配置的网页不再落 `[models]`）。
- chat()（1b 起）：三种协议各有适配层（请求拼装 / 响应解析都是纯函数，
  _build_openai_request / _build_responses_request / _build_anthropic_request +
  _parse_chat / _parse_responses_response / _parse_anthropic_response）：
  openai 走 /chat/completions（流式）；responses 走 /responses；anthropic 走
  /v1/messages（非流式）。干活岗位由 chat(agent=<kind>) 定：候选链 = 该岗位
  profile 的 model→backup；岗位没解析出候选时用主模型（main）的链兜底
  （强度也跟着读主模型的）。旧口 role 保住：不传 agent 时 role="main" ⇒
  agent="main"，role="worker" ⇒ agent="task"（聊主对话 / 搬运的话永远 main）。
- 思考强度：岗位 profile.effort 只在所选条目的 efforts 里勾了才发（条目空 =
  永不发）；按协议映射（openai/responses：max 夹 high；anthropic：xhigh 夹 high、
  output_config.effort——各家的 xhigh/max 覆盖面待实测）。
- 用量：usage / model_calls 各多一列 agent（岗位 kind）；role 维持
  「主模型 / 子 agent」两桶账（兜底用主模型链的调用也记 main 桶）。
- 就绪 = 「主模型」+「任务」岗位各自解析出至少 1 个候选（岗位选了条目、条目端点
  在、端点有 base_url + api_key）；旧四槽模式照旧（base_url/key/main/worker 齐）。
- settings() 缓存键 = （Settings 对象本身 `is` 判等 + kv["models.checked"] 当前值）：
  Settings 对象换新（配置热更新）必然重算，不能光记 id()——旧对象回收后
  新对象复用同地址会吃到上一次的摘要。
- 密钥只进不出：返回网页的任何结构都不含密钥（用 key_set 布尔值代替）；错误消息、
  日志、usage.error、model_calls 里都不许出现密钥（_redact 统一遮掉）。
- chat() 可重试错误（网络错误、429、408、5xx）同一模型最多「1 + retries」次；
  非 429 的两次之间等 retry_delay_s 秒；429 改由端点级冷却决定等待
  （Retry-After 秒数 / HTTP 日期，封顶 120 秒；没有就按连续 429 次数
  10/20/40/60 秒退避、封顶 60 秒，加 ±20% 抖动）；
  用完再尝候选里的下一条，同样规则；其他 4xx 不重试、不换，直接抛。
  chat(retries=n) 可临时覆盖设置里的重试次数（后台主循环里直接 await 的调用传 1）。
- 端点级限流（EndpointThrottle，状态只在进程内存、不落库）：按
  `{端点 id}|{normalize(base_url)}` 分门（同一端点条目改地址自然隔两门）；
  同步发上限 max_concurrency / 429 冷却 / max_rpm（0 = 关）都按候选端点读。
- list_models(base_url, api_key, protocol="openai")：网页「测试连接」；
  openai/responses 走 `{base}/models` 带 Bearer；anthropic 走 `{base}/v1/models`
  （`{base}` 已含 /v1 时不重加）带 `x-api-key` + `anthropic-version: 2023-06-01`；
  结果的可用模型列表存 `kv["endpoints.checked.<id>"]`（console/server 路由记 id）。
- 上下文窗口 / 最大输出：`limits_for(kind)` = 岗位首选候选条目的值；旧四槽兜底
  用 `Settings.models` 的旧全局值。压缩（workers/coordinator/admin_chat）先问它。
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
# json_mode 的做法：提示里要求（不发 response_format，见 chat() 里的实验说明）
_JSON_ONLY_HINT = "只输出一个完整的 JSON 对象（按下面要求的全部字段），不要输出任何别的文字。"
_RETRY_DELAY_CAP_S = 60.0   # 非 429 重试等待的封顶（retry_delay_s 本身 1~60）
_COOLDOWN_BACKOFF_S = (10.0, 20.0, 40.0, 60.0)  # 没有 Retry-After 时按连续 429 次数退避
_COOLDOWN_CAP_S = 60.0      # 退避封顶
_COOLDOWN_JITTER = 0.2      # 退避 ±20% 抖动
_RPM_WINDOW_S = 60.0        # 每分钟上限的滑动窗口
_MAX_CONCURRENCY_DEFAULT = 2  # 每个端点同一时刻最多几个在途请求
# 思考强度合法档位（与 config.py 的 EFFORT_LEVELS 一致；独立拷贝防导入环）
_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


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
    """模型设置摘要（给网页总览/徽标用；不含密钥）。

    2026-10 改版 1a：真身在 [[endpoints]] + [[model_list]]（配置）+ 专岗档案的
    model/effort/backup（kv["agents.profiles"]）。这里的主备/数值字段 = 主模型
    （main / worker 两个视角各自的「首选」）解析结果。没接 agents（老链路）时按
    旧 [models] 四槽砖块算。
    """

    base_url: str
    main: str            # 主模型服务商模型名（旧调用方的习惯）
    main_backup: str
    worker: str
    worker_backup: str
    key_set: bool
    source: str  # "config" | "none"（网页改的也写 config.toml，所以只有这两种）
    checked_at: float = 0.0
    available: list[str] = field(default_factory=list)
    retries: int = 5         # 主模型端点的重试设置（0~10）
    retry_delay_s: int = 10  # 主模型端点两次重试间隔（1~60）
    max_concurrency: int = 2  # 主模型端点并发上限（1~8）
    max_rpm: int = 0          # 主模型端点每分钟上限（0 = 不限）
    context_window: int = 128000  # 主模型所选条目的上下文（tokens），上下文压缩用
    max_tokens: int = 32768  # 主模型所选条目的最大输出（tokens）
    main_label: str = ""    # 主模型显示名（模型库的 name；网页摘要用）
    worker_label: str = ""
    _ready: bool = False

    def ready(self) -> bool:
        """主模型能用、子 agent 模型也能用（主模型端点地址 + 密钥齐全）。"""
        return bool(self._ready)

    def public(self) -> dict:
        """给网页的字典（不含密钥）。"""
        return {
            "base_url": self.base_url,
            "key_set": self.key_set,
            "main": self.main,
            "main_backup": self.main_backup,
            "worker": self.worker,
            "worker_backup": self.worker_backup,
            "main_label": self.main_label,
            "worker_label": self.worker_label,
            "source": self.source,
            "checked_at": self.checked_at,
            "available": list(self.available),
            "ready": self.ready(),
            "retries": self.retries,
            "retry_delay_s": self.retry_delay_s,
            "max_concurrency": self.max_concurrency,
            "max_rpm": self.max_rpm,
            "context_window": self.context_window,
            "max_tokens": self.max_tokens,
        }


@dataclass
class _Candidate:
    """一次调用的候选（首选 / 备用）：模型库条目 + 它挂的端点 + 这个候选能发的思考强度。"""

    service_model: str     # 发给端点的模型名
    label: str             # 日志/摘要显示用
    endpoint: Any          # config.EndpointSetting
    context_window: int
    max_tokens: int        # 这个条目的默认输出上限（调用方没传用它）
    efforts: tuple = ()    # 条目 efforts（agent profile 的强度只在它勾了才发）



class _StreamError(Exception):
    """流式回答中途出错（流里报错 / 没收完就断）：按可重试的 5xx 处理。"""


def _effort_for_protocol(effort: str, protocol: str | None) -> str:
    """岗位强度 → 这个协议真发出去的值；不能发（没请求强度 / 没指名协议）返回 ""。

    - openai /responses：reasoning_effort / reasoning.effort。OpenAI 文档写过
      minimal/low/medium/high，新模型（gpt-5.1 那代起）加了 xhigh——xhigh 原样发，
      端点不收就用自己的 rejection 说话（上限两侧的「max」没有这值，夹成 high 最接近）。
    - anthropic：output_config.effort（放行 low/medium/high/max；xhigh 不是官方值，
      夹成 high）。「max 只部分模型支持」「output_config 的写法」待实测。
    映射只按协议，不看具体模型名——同一端点代发多家模型时没法猜，靠条目的 efforts
    勾选先把了一层关（没勾根本不走这里）。
    """
    e = str(effort or "").strip().lower()
    if not e or not protocol:
        return ""
    p = str(protocol or "").strip().lower()
    if p in ("openai", "responses"):
        return "high" if e == "max" else e
    if p == "anthropic":
        return "high" if e == "xhigh" else e
    return ""


# ----------------------------------------------------------------------
# 协议层请求拼装（纯函数，单测直接对形状）
# ----------------------------------------------------------------------


def _content_text(content: Any) -> str:
    """OpenAI 消息的 content（str 或 [{'type':'text','text':…}]）拿成纯文本；别的给 ""。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return ""


def _json_messages(messages: list[dict], json_mode: bool) -> list[dict]:
    """json_mode 的「在提示里要求」前置一条 system 提示（2026-09-29 线上对照实验的规矩，
    见 chat() 里的说明）。"""
    return [{"role": "system", "content": _JSON_ONLY_HINT}, *messages] if json_mode else list(messages)


def _build_openai_request(
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None,
    json_mode: bool,
    max_tokens: int,
    effort: str = "",
) -> dict[str, Any]:
    """OpenAI 兼容 /chat/completions 的请求体（流式 + 带 usage）。"""
    body: dict[str, Any] = {
        "model": model,
        "messages": _json_messages(messages, json_mode),
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": int(max_tokens),
    }
    if tools:
        body["tools"] = tools
    mapped = _effort_for_protocol(effort, "openai")
    if mapped:
        body["reasoning_effort"] = mapped
    return body


def _build_responses_request(
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None,
    json_mode: bool,
    max_tokens: int,
    effort: str = "",
) -> dict[str, Any]:
    """OpenAI Responses /responses 的请求体（非流式；stream 仍发 True——端点回普通 JSON 也行）：

    - system 消息进 instructions + system-role input items（两者都给，老/新端点兼容度更高）；
    - user/assistant 文本 → {"role", content: [{input_text|output_text}]}；
    - assistant 的 tool_calls → function_call（call_id/name/arguments）；待实测部分端点收 flat message；
    - role=tool 的结果 → function_call_output（同一 call_id 的连续多条合并成一条 output）；
    - tools → {"type":"function", name, description, parameters}（顶层，不是包一层 function）。
    """
    system_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        text = _content_text(m.get("content"))
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            if json_mode and not system_parts:
                system_parts.append(_JSON_ONLY_HINT)  # 没有 system 就把 JSON 要求塞 instructions
            if text:
                input_items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})
            continue
        if role == "assistant":
            if text:
                input_items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                call_id = str(tc.get("id") or "")
                name = str(fn.get("name") or "")
                if not name:
                    continue
                input_items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": str(fn.get("arguments") or ""),
                })
            continue
        if role == "tool":
            call_id = str(m.get("tool_call_id") or "")
            parts = [text]
            # 连续多条 tool（同一 call_id）：后面兄弟并进来时设了 _merged_into 就不再出条目
            out = {"type": "function_call_output", "call_id": call_id, "output": text}
            input_items.append(out)
            continue
        # 别的角色（developer 等）：当 user 文本兜底
        if text:
            input_items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})
    # 同 call_id 的连续 function_call_output 合并（保持序）：相邻才并
    merged: list[dict[str, Any]] = []
    for item in input_items:
        if (
            item.get("type") == "function_call_output"
            and merged
            and merged[-1].get("type") == "function_call_output"
            and merged[-1].get("call_id") == item.get("call_id")
        ):
            merged[-1]["output"] = f'{merged[-1]["output"]}\n{item.get("output") or ""}'
        else:
            merged.append(dict(item))
    body: dict[str, Any] = {
        "model": model,
        "input": merged,
        "stream": True,
        "max_output_tokens": int(max_tokens),
    }
    if system_parts:
        body["instructions"] = "\n\n".join(str(p) for p in system_parts if p)
    out_tools: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        name = str(fn.get("name") or "")
        if not name:
            continue
        spec: dict[str, Any] = {"type": "function", "name": name}
        if fn.get("description"):
            spec["description"] = str(fn["description"])
        if fn.get("parameters") is not None:
            spec["parameters"] = fn["parameters"]
        out_tools.append(spec)
    if out_tools:
        body["tools"] = out_tools
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    mapped = _effort_for_protocol(effort, "responses")
    if mapped:
        body["reasoning"] = {"effort": mapped}
    return body


def _anthropic_messages_url(base_url: str) -> str:
    """messages 的地址：{base}/v1/messages；base 已带 /v1 时不叠（同 _anthropic_models_url）。"""
    base = str(base_url or "").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _build_anthropic_request(
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None,
    json_mode: bool,
    max_tokens: int,
    effort: str = "",
) -> dict[str, Any]:
    """Anthropic Messages /v1/messages 的请求体：

    - system 消息（可能多条）合成顶层 system 字符串；json_mode 没原生开关，
      在 system 末尾追加「只输出 JSON」的硬要求；
    - assistant 的文本 + tool_calls → content 里 text + tool_use 块（input 要真对象）；
    - role=tool → user 消息里的 tool_result 块（is_error 不发——我们拿到的都是「
      工具给回来的内容」）；连续的 tool 消息并进同一条 user；同名 tool_use_id 的
      连续结果合并成一个块；
    - tools → {name, description, input_schema}；max_tokens 必填（调用方保证带上）；
    - effort → output_config（文档写法，待实测）；绝不发 thinking（budget 那套我们不用）。
    """
    system_parts: list[str] = []
    out_messages: list[dict[str, Any]] = []
    i = 0
    seq = list(messages if isinstance(messages, list) else [])
    n = len(seq)
    while i < n:
        m = seq[i]
        if not isinstance(m, dict):
            i += 1
            continue
        role = str(m.get("role") or "")
        if role == "system":
            text = _content_text(m.get("content"))
            if text:
                system_parts.append(text)
            i += 1
            continue
        if role == "user":
            out_messages.append({"role": "user", "content": [{"type": "text", "text": _content_text(m.get("content")) or ""}]})
            i += 1
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _content_text(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                if not name:
                    continue
                raw_args = fn.get("arguments")
                if isinstance(raw_args, dict):
                    tool_input: Any = raw_args
                else:
                    try:
                        parsed = json.loads(str(raw_args or ""))
                    except (TypeError, ValueError):
                        parsed = None
                    tool_input = parsed if isinstance(parsed, dict) else {"arguments": str(raw_args or "")}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": name,
                    "input": tool_input,
                })
            out_messages.append({"role": "assistant", "content": blocks})
            i += 1
            continue
        if role == "tool":
            # 连续的 tool 消息收成一条 user：同 call_id 的合并成一个 tool_result
            blocks: list[dict[str, Any]] = []
            j = i
            while j < n and isinstance(seq[j], dict) and seq[j].get("role") == "tool":
                tm = seq[j]
                call_id = str(tm.get("tool_call_id") or "") or f"tool_{j + 1}"
                content = _content_text(tm.get("content"))
                if blocks and blocks[-1].get("tool_use_id") == call_id:
                    blocks[-1]["content"] = f'{blocks[-1]["content"]}\n{content}'
                else:
                    blocks.append({"type": "tool_result", "tool_use_id": call_id, "content": content})
                j += 1
            out_messages.append({"role": "user", "content": blocks})
            i = j
            continue
        # 别的角色：当 user 文本兜底
        out_messages.append({"role": "user", "content": [{"type": "text", "text": _content_text(m.get("content")) or ""}]})
        i += 1
    if json_mode:
        system_parts.append(_JSON_ONLY_HINT)
    body: dict[str, Any] = {"model": model, "max_tokens": int(max_tokens)}
    if system_parts:
        body["system"] = "\n\n".join(str(p) for p in system_parts if p)
    # 兜底合相邻同角色（用户 history 一般不规整，anthropic 官方要求严格交替）；
    # 但 user（纯文本）和 user（tool_result 块）不能混进同一条——那是两种事
    smoothed: list[dict[str, Any]] = []
    for msg in out_messages:
        is_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"]
        )
        if smoothed and smoothed[-1]["role"] == msg["role"] and smoothed[-1]["_tr"] == is_tool_result:
            smoothed[-1]["content"].extend(msg["content"])
        else:
            smoothed.append({"role": msg["role"], "content": list(msg["content"]), "_tr": is_tool_result})
    for msg in smoothed:
        msg.pop("_tr", None)
    body["messages"] = smoothed
    out_tools: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        name = str(fn.get("name") or "")
        if not name:
            continue
        spec: dict[str, Any] = {"name": name, "input_schema": fn.get("parameters") or {"type": "object"}}
        if fn.get("description"):
            spec["description"] = str(fn["description"])
        out_tools.append(spec)
    if out_tools:
        body["tools"] = out_tools
    mapped = _effort_for_protocol(effort, "anthropic")
    if mapped:
        # 文档写法（platform.claude.com 的 Effort 页）；「output_config 这层壳、
        # max 只部分模型收」都待实测——模型不收会是 400，像别的 4xx 一样直接抛出来。
        body["output_config"] = {"effort": mapped}
    return body


# ----------------------------------------------------------------------
# 协议层响应解析（纯函数）：统一收成 ChatResult 的料（text/tool_calls/usage/finish）
# ----------------------------------------------------------------------


def _finish_from_stop_reason(reason: Any) -> str:
    """Anthropic stop_reason → 我们的 finish_reason（跟 openai 的词对齐）。"""
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "pause_turn": "stop",
        "refusal": "stop",
    }.get(str(reason or ""), str(reason or ""))


def _parse_anthropic_response(data: Any, model: str) -> "ChatResult":
    """Anthropic /v1/messages 的响应：content 里 text + tool_use 块；usage 的 input/output_tokens。"""
    if not isinstance(data, dict):
        raise ModelError("模型返回格式不对：不是 JSON 对象")
    content = data.get("content")
    if content is None:
        content = []
    if not isinstance(content, list):
        raise ModelError("模型返回格式不对：content 不是数组")
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    raw_blocks: list[dict[str, Any]] = []
    for i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        raw_blocks.append(block)
        if btype == "text":
            texts.append(str(block.get("text") or ""))
        elif btype == "tool_use":
            name = str(block.get("name") or "")
            if not name:
                continue
            inp = block.get("input")
            args = inp if isinstance(inp, str) else json.dumps(inp if inp is not None else {}, ensure_ascii=False)
            tool_calls.append({
                "id": str(block.get("id") or f"callu_{i}"),
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
        # thinking / redacted_thinking / 别的块：不进结果（跟 openai 那条 reasoning_content 一个处理）
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if raw_blocks:
        message["_blocks"] = raw_blocks  # 留底（含 thinking），谁也不读也不回显
    return ChatResult(
        text="".join(texts),
        tool_calls=tool_calls,
        model=model,
        prompt_tokens=int(usage.get("input_tokens") or 0),
        completion_tokens=int(usage.get("output_tokens") or 0),
        raw_message=message,
        finish_reason=_finish_from_stop_reason(data.get("stop_reason")),
    )


def _parse_responses_response(data: Any, model: str) -> "ChatResult":
    """Responses 的响应：output 数组里 message（output_text 文本）+ function_call。"""
    if not isinstance(data, dict):
        raise ModelError("模型返回格式不对：不是 JSON 对象")
    output = data.get("output")
    if output is None:
        output = []
    if not isinstance(output, list):
        raise ModelError("模型返回格式不对：output 不是数组")
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for i, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(str(part.get("text") or ""))
        elif itype == "function_call":
            name = str(item.get("name") or "")
            if not name:
                continue
            tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or f"call_{i}"),
                "type": "function",
                "function": {"name": name, "arguments": str(item.get("arguments") or "")},
            })
        # reasoning / refusal 等其它 item 不进结果
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    status = str(data.get("status") or "")
    finish = "tool_calls" if tool_calls else ("length" if status == "incomplete" else "")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return ChatResult(
        text="".join(texts),
        tool_calls=tool_calls,
        model=model,
        prompt_tokens=int(usage.get("input_tokens") or 0),
        completion_tokens=int(usage.get("output_tokens") or 0),
        raw_message=message,
        finish_reason=finish,
    )


def _responses_from_events(events: list[dict]) -> dict:
    """Responses 的 SSE 事件流收成「非流式形状」：拿 response.completed 里那份整的
    （在里面没有/坏了才从零散 delta 拼——想流式省等的端点才走这条路）。"""
    completed: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "response.completed" and isinstance(ev.get("response"), dict):
            completed = ev["response"]
        elif ev.get("type") == "response.failed":
            err = ev.get("response") or ev.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err or "response.failed")
            raise _StreamError(str(msg)[:500])
    if completed:
        return completed
    # 没有 completed：从 output_text.delta / output_item 事件自己拼（保底，待实测哪些端点这样回）
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    status = ""
    usage: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = str(ev.get("type") or "")
        if t == "response.output_text.delta":
            texts.append(str(ev.get("delta") or ""))
        elif t in ("response.output_item.done",) and isinstance(ev.get("item"), dict):
            item = ev["item"]
            if item.get("type") == "function_call":
                tool_calls.append({
                    "id": str(item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}"),
                    "type": "function",
                    "function": {"name": str(item.get("name") or ""), "arguments": str(item.get("arguments") or "")},
                })
        elif t in ("response.in_progress", "response.created"):
            resp = ev.get("response")
            if isinstance(resp, dict) and resp.get("status"):
                status = str(resp["status"])
    out: dict[str, Any] = {
        "status": status or "completed",
        "output": [],
        "usage": usage,
    }
    if texts:
        out["output"].append({"type": "message", "content": [{"type": "output_text", "text": "".join(texts)}]})
    out["output"].extend({"type": "function_call", **tc, "call_id": tc["id"],
                          "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}
                         for tc in tool_calls)
    return out


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict]
    model: str
    prompt_tokens: int
    completion_tokens: int
    raw_message: dict
    # "stop" / "tool_calls" / "length"（被 max_tokens 截断）…；端点没给就空字符串
    finish_reason: str = ""


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
    if patch.get("context_window") is not None and not _valid_int(patch.get("context_window"), 8192, 2_000_000):
        problems.append("上下文长度必须是 8192~2000000 的整数（tokens）")
    if patch.get("max_tokens") is not None and not _valid_int(patch.get("max_tokens"), 1024, 1_000_000):
        problems.append("最大输出（max_tokens）必须是 1024~1000000 的整数（tokens）")
    return problems


@dataclass
class _LegacyBrick:
    """旧 [models] 四槽砖块（迁移前的过渡读口；clamp 规则和 0.4.4 一致）。"""

    base_url: str = ""
    api_key: str = ""
    main: str = ""
    main_backup: str = ""
    worker: str = ""
    worker_backup: str = ""
    retries: Any = 5
    retry_delay_s: Any = 10
    max_concurrency: Any = 2
    max_rpm: Any = 0
    context_window: Any = 128000
    max_tokens: Any = 32768

    def __post_init__(self) -> None:
        self.retries = self._clamped(self.retries, 0, 10, 5)
        self.retry_delay_s = self._clamped(self.retry_delay_s, 1, 60, 10)
        self.max_concurrency = self._clamped(self.max_concurrency, 1, 8, 2)
        self.max_rpm = self._clamped(self.max_rpm, 0, 600, 0)
        self.context_window = self._clamped(self.context_window, 8192, 2_000_000, 128000)
        self.max_tokens = self._clamped(self.max_tokens, 1024, 1_000_000, 32768)

    @staticmethod
    def _clamped(value: Any, low: int, high: int, default: int) -> int:
        if not _int_in(value, low, high):
            return default
        return int(value)

    def any_value(self) -> bool:
        return bool(
            self.base_url or self.api_key or self.main or self.main_backup
            or self.worker or self.worker_backup
        )

    def to_endpoint(self) -> Any:
        """老链路：把砖块装成一个 endpoint id 为 __legacy__ 的端点。"""
        from . import config as _cfg

        return _cfg.EndpointSetting(
            id="__legacy__", name="旧配置", protocol="openai",
            base_url=self.base_url, api_key=self.api_key,
            retries=int(self.retries), retry_delay_s=int(self.retry_delay_s),
            max_concurrency=int(self.max_concurrency), max_rpm=int(self.max_rpm),
        )


class Models:
    def __init__(
        self,
        store: Store,
        get_settings: Callable[[], Settings],
        *,
        transport=None,
        config_writer: Any = None,
        agents: Any = None,
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
        # 专岗（agents.py）：2026-10 改版 1a 起「谁用哪个模型」读岗位 profile
        # （kv["agents.profiles"] 的 model/backup）。没接（老测试/启动早期）→ 旧 [models] 四槽。
        self._agents = agents

    def set_agents(self, agents: Any) -> None:
        """app 在 Agents 就位后挂上（Models 建得比 Agents 早）；同时清缓存重算。"""
        self._agents = agents
        self._cache = None

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def settings(self) -> ModelSettings:
        """config.toml 的 [models] / [[endpoints]]+[[model_list]]+岗位选择是唯一来源
        （网页保存也写它）；结果内存缓存。

        get_settings 回调返回的 Settings 对象变了（配置热更新）就重算；
        kv["models.checked"]（测试连接的回写）变了也重算。
        缓存钉住 **Settings 对象本身**（`is` 判等）——光记 id() 会在旧对象被回收后
        新对象复用同一地址时吃到上一次的摘要（出现过：写端点热应用后 ready 还是 False）。
        """
        settings = self._get_settings()
        try:
            checked = self._store.kv_get("models.checked")
        except Exception:
            checked = None
        if self._cache is not None and self._cache[0] is settings and self._cache[1] == checked:
            return self._cache[2]
        computed = self._compute(settings, checked)
        self._cache = (settings, checked, computed)
        return computed

    def _compute(self, settings: Settings, checked: Any) -> ModelSettings:
        cfg = settings.models.__dict__ if settings.models else {}
        old = _LegacyBrick(
            base_url=str(cfg.get("base_url") or "").strip().rstrip("/"),
            api_key=str(cfg.get("api_key") or ""),
            main=str(cfg.get("main") or "").strip(),
            main_backup=str(cfg.get("main_backup") or "").strip(),
            worker=str(cfg.get("worker") or "").strip(),
            worker_backup=str(cfg.get("worker_backup") or "").strip(),
            retries=cfg.get("retries"), retry_delay_s=cfg.get("retry_delay_s"),
            max_concurrency=cfg.get("max_concurrency"), max_rpm=cfg.get("max_rpm"),
            context_window=cfg.get("context_window"), max_tokens=cfg.get("max_tokens"),
        )
        endpoints = tuple(getattr(settings, "endpoints", ()) or ())
        model_list = tuple(getattr(settings, "model_list", ()) or ())
        agents = self._agents

        if agents is not None:
            main_cs = self._resolve_candidates("main", endpoints, model_list, old, agents)
            worker_cs = self._resolve_candidates("task", endpoints, model_list, old, agents)
            primary_ep = main_cs[0].endpoint if main_cs else None
            first = main_cs[0] if main_cs else None
            first_w = worker_cs[0] if worker_cs else None
            source = "config" if (endpoints or old.any_value()) else "none"
            base_url = primary_ep.base_url if primary_ep else old.base_url
            ready = bool(first and first_w)
            out = ModelSettings(
                base_url=base_url,
                main=first.service_model if first else "",
                main_backup=main_cs[1].service_model if len(main_cs) > 1 else "",
                worker=first_w.service_model if first_w else "",
                worker_backup=worker_cs[1].service_model if len(worker_cs) > 1 else "",
                key_set=bool(primary_ep and primary_ep.api_key),
                source=source,
                retries=int(getattr(primary_ep, "retries", old.retries)) if primary_ep else old.retries,
                retry_delay_s=int(getattr(primary_ep, "retry_delay_s", old.retry_delay_s)) if primary_ep else old.retry_delay_s,
                max_concurrency=int(getattr(primary_ep, "max_concurrency", old.max_concurrency)) if primary_ep else old.max_concurrency,
                max_rpm=int(getattr(primary_ep, "max_rpm", old.max_rpm)) if primary_ep else old.max_rpm,
                context_window=int(first.context_window) if first else old.context_window,
                max_tokens=int(first.max_tokens) if first else old.max_tokens,
                main_label=str(first.label if first else ""),
                worker_label=str(first_w.label if first_w else ""),
                _ready=ready,
            )
        else:
            # 老链路（没接 agents）：按旧 [models] 四槽砖块算
            source = "config" if old.any_value() else "none"
            out = ModelSettings(
                base_url=old.base_url,
                main=old.main,
                main_backup=old.main_backup,
                worker=old.worker,
                worker_backup=old.worker_backup,
                key_set=bool(old.api_key),
                source=source,
                retries=old.retries,
                retry_delay_s=old.retry_delay_s,
                max_concurrency=old.max_concurrency,
                max_rpm=old.max_rpm,
                context_window=old.context_window,
                max_tokens=old.max_tokens,
                main_label=old.main,
                worker_label=old.worker,
                _ready=bool(old.base_url and old.api_key and old.main and old.worker),
            )
        # checked_at / available：测试连接的回写（不是配置，存 kv），端点一致才并进来显示
        if isinstance(checked, dict):
            if str(checked.get("base_url") or "").strip().rstrip("/") == out.base_url and out.base_url:
                try:
                    out.checked_at = float(checked.get("checked_at") or 0)
                except (TypeError, ValueError):
                    out.checked_at = 0.0
                raw_available = checked.get("available")
                if isinstance(raw_available, list):
                    out.available = [str(x) for x in raw_available]
        return out

    # ------------------------------------------------------------------
    # 候选解析（2026-10 改版 1a）：岗位 profile.model/backup → 模型库条目 + 端点
    # ------------------------------------------------------------------

    def _resolve_candidates(
        self,
        kind: str,
        endpoints: tuple,
        model_list: tuple,
        old: "_LegacyBrick",
        agents: Any,
    ) -> list[_Candidate]:
        """把这个岗位（"main" / "task" 等）解析成候选链（首选 + 备用）。

        - profile.model 空 / 模型库里没有 / 端点没了 / 端点缺 base_url 或 api_key →
          这个候选不算数（ready 判定会跟着变 False——和旧「缺一项不干活」的规矩一致）；
        - agents.profile 抛错 / 值不对：直接当没选（读路径容错，不拦启动）；
        - 阶段 1a：只返回候选，不区分协议；chat 里用非 openai 协议的端点会给中文错（1b 接）。
        """
        try:
            profile = agents.profile(kind)
        except Exception:
            profile = None
        if not isinstance(profile, dict):
            profile = {}
        by_id = {str(getattr(m, "id", "") or ""): m for m in model_list}
        ep_by_id = {str(getattr(e, "id", "") or ""): e for e in endpoints}
        out: list[_Candidate] = []
        for raw_id in (profile.get("model"), profile.get("backup")):
            entry_id = str(raw_id or "").strip()
            if not entry_id:
                continue
            entry = by_id.get(entry_id)
            if entry is None:
                continue
            ep = ep_by_id.get(str(getattr(entry, "endpoint", "") or ""))
            if ep is None:
                continue
            if not str(getattr(ep, "base_url", "") or "").strip():
                continue
            if not str(getattr(ep, "api_key", "") or "").strip():
                continue
            service_model = str(getattr(entry, "model", "") or "").strip()
            if not service_model:
                continue
            out.append(
                _Candidate(
                    service_model=service_model,
                    label=str(getattr(entry, "name", "") or service_model),
                    endpoint=ep,
                    context_window=int(getattr(entry, "context_window", 128000) or 128000),
                    max_tokens=int(getattr(entry, "max_tokens", 32768) or 32768),
                    efforts=tuple(
                        str(v) for v in (getattr(entry, "efforts", ()) or ()) if str(v) in _EFFORT_LEVELS
                    ),
                )
            )
        if not out and not model_list and old.any_value():
            # 迁移前的过渡：库里还没 [[model_list]] 也没有岗位选择，旧 [models] 四槽还在
            # → 按老规矩用四槽（迁移把岗位选好之后自动走开上面那条）
            ep = old.to_endpoint() if old.base_url else None
            if ep is not None:
                names = (old.main, old.main_backup) if kind == "main" else (old.worker, old.worker_backup)
                for name in names:
                    if name:
                        out.append(
                            _Candidate(
                                service_model=name, label=name, endpoint=ep,
                                context_window=old.context_window, max_tokens=old.max_tokens,
                            )
                        )
        return out

    def _current_candidates(self, kind: str) -> list[_Candidate]:
        """chat/limits_for 用：传岗位 kind（"main" / "news" / "task" / c_xxx…）。

        岗位自己没解析出候选（没选模型 / 选的条目失效）→ 主模型（"main"）的链兜底
        （docs/12：找不到就用主模型兜底）；主模型也没有 → 空列表，chat 给中文错。
        """
        settings = self._get_settings()
        old = self._legacy_brick(settings)
        agents = self._agents
        kind_s = str(kind or "").strip() or "main"
        if agents is not None:
            endpoints = tuple(getattr(settings, "endpoints", ()) or ())
            model_list = tuple(getattr(settings, "model_list", ()) or ())
            out = self._resolve_candidates(kind_s, endpoints, model_list, old, agents)
            if not out and kind_s != "main":
                out = self._resolve_candidates("main", endpoints, model_list, old, agents)
            return out
        # 老链路：旧 [models] 四槽 → 一个假端点 + 四槽候选
        ep = old.to_endpoint()
        out: list[_Candidate] = []
        names = (old.main, old.main_backup) if kind_s == "main" else (old.worker, old.worker_backup)
        for name in names:
            if name:
                out.append(
                    _Candidate(
                        service_model=name, label=name, endpoint=ep,
                        context_window=old.context_window, max_tokens=old.max_tokens,
                    )
                )
        return out

    def _profile_effort(self, kind: str) -> str:
        """这个岗位 profile 里选的思考强度（读坏了 / 没选 = ""）。

        兜底的强度跟着链走：调用方传过来的是「实际干活链路」的岗位（自己的链成了=自己，
        兜底=主模型），所以这里永远只读这一个 kind。
        """
        agents = self._agents
        if agents is None:
            return ""
        try:
            profile = agents.profile(str(kind or ""))
        except Exception:
            return ""
        if isinstance(profile, dict):
            v = str(profile.get("effort") or "").strip().lower()
            if v in _EFFORT_LEVELS:
                return v
        return ""

    def limits_for(self, kind: str | None = None) -> dict:
        """这个岗位（空 = 主模型）用哪个模型的上下文窗口 / 最大输出（上下文压缩、
        画像摘要这些从「settings.models 全局值」改成「所选模型条目」的读口）。

        与 chat() 同一条解析路：岗位没选模型时用主模型的条目兜底。
        读不到（没接 agents / 都没选）回落旧 [models] 全局值，再不行系统默认。
        """
        kind_s = str(kind or "").strip() or "main"
        try:
            cands = self._current_candidates(kind_s)
        except Exception:
            cands = []
        if cands:
            return {"context_window": cands[0].context_window, "max_tokens": cands[0].max_tokens}
        settings = self._get_settings()
        old = self._legacy_brick(settings)
        return {"context_window": old.context_window, "max_tokens": old.max_tokens}

    def _legacy_brick(self, settings: Settings) -> "_LegacyBrick":
        cfg = getattr(settings, "models", None)
        cfg_d = cfg.__dict__ if cfg else {}
        return _LegacyBrick(
            base_url=str(cfg_d.get("base_url") or "").strip().rstrip("/"),
            api_key=str(cfg_d.get("api_key") or ""),
            main=str(cfg_d.get("main") or "").strip(),
            main_backup=str(cfg_d.get("main_backup") or "").strip(),
            worker=str(cfg_d.get("worker") or "").strip(),
            worker_backup=str(cfg_d.get("worker_backup") or "").strip(),
            retries=cfg_d.get("retries"), retry_delay_s=cfg_d.get("retry_delay_s"),
            max_concurrency=cfg_d.get("max_concurrency"), max_rpm=cfg_d.get("max_rpm"),
            context_window=cfg_d.get("context_window"), max_tokens=cfg_d.get("max_tokens"),
        )

    def _current_key(self, settings: Settings) -> str:
        """密钥只认 config.toml 的 [models] api_key（数据库覆盖层已废弃）。"""
        return str(settings.models.api_key or "")

    def _all_endpoint_keys(self, settings: Settings) -> list[str]:
        """本次调用可能用到的全部密钥（遮罩名单；绝不落日志值本身）。"""
        out: list[str] = []
        legacy = self._current_key(settings)
        if legacy:
            out.append(legacy)
        for ep in (getattr(settings, "endpoints", ()) or ()):
            k = str(getattr(ep, "api_key", "") or "")
            if k and k not in out:
                out.append(k)
        return out

    def endpoint_key(self, endpoint_id: str) -> str:
        """给网页「测试连接」用：按端点 id 取密钥（不给密钥值出网页，只进请求头）。"""
        settings = self._get_settings()
        for ep in (getattr(settings, "endpoints", ()) or ()):
            if str(getattr(ep, "id", "") or "") == str(endpoint_id or ""):
                return str(getattr(ep, "api_key", "") or "")
        return ""

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
        # retries / retry_delay_s / 限流两项 / 上下文长度 / 最大输出：没传保留当前有效值
        # （别把网页上没动的重试清零）
        for key in ("retries", "retry_delay_s", "max_concurrency", "max_rpm", "context_window", "max_tokens"):
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

    @staticmethod
    def _anthropic_models_url(base_url: str) -> str:
        """anthropic：GET {base}/v1/models；base 已以 /v1 结尾时不叠两个 v1。"""
        base = str(base_url or "").strip().rstrip("/")
        if base.lower().endswith("/v1"):
            return base + "/models"
        return base + "/v1/models"

    async def list_models(self, base_url: str, api_key: str = "", protocol: str = "openai") -> list[str]:
        """列端点上的模型（「测试连接」也用它）。三种协议：

        - openai / responses：GET {base}/models（Authorization: Bearer key）
        - anthropic：GET {base}/v1/models（x-api-key + anthropic-version: 2023-06-01；
          base 已带 /v1 时不叠）
        返回模型 id 字符串列表（{"data":[...]}；{"models":[...]} 认 name/id）。
        api_key 空则用已存的（旧 [models] 密钥；新链路调用方会传端点密钥）。没有密钥抛
        ModelError。成功时不自动保存。端点在 429 冷却里直接报「约 N 秒后再试」。
        """
        key = str(api_key or "") or self._current_key(self._get_settings())
        if not key:
            raise ModelError("没有可用的模型密钥")
        proto = str(protocol or "openai").strip().lower()
        base = str(base_url or "").strip().rstrip("/")
        if proto == "anthropic":
            url = self._anthropic_models_url(base)
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            url = base + "/models"
            headers = {"Authorization": f"Bearer {key}"}
        endpoint = self._throttle.normalize(base)
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
            resp = await client.get(url, headers=headers, timeout=30)
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
            if not isinstance(data, dict):
                raise ValueError
            items = data.get("data") or data.get("models")
            if not isinstance(items, list):
                raise ValueError
            out: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or item.get("name") or "").strip()
                if mid:
                    out.append(mid)
            return out
        except ValueError:
            raise ModelError("模型列表返回格式不对") from None

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        role: Literal["main", "worker"] | None = None,
        messages: list[dict] | None = None,
        *,
        agent: str | None = None,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        purpose: str = "",
        group_id: str = "",
        task_id: str = "",
        timeout: float = 120,
        retries: int | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """调一次模型。

        干活岗位由 agent 决定（kind：main / news / idea / goal / task / c_xxx…）：
        它的 profile.model→backup 是候选链；它自己没解析出候选时用主模型（"main"）
        的链兜底（计划的规矩），此时强度也读主模型的。旧调用口 role 保住：
        role="main" 没给 agent ⇒ agent="main"；role="worker" 没给 agent ⇒ agent="task"。

        重试规则：可重试的错误 = 网络错误（httpx.HTTPError）、429、408、5xx。
        同一个模型最多「1 + retries」次；非 429 的两次之间等 retry_delay_s 秒；
        429 由端点级冷却决定等待（Retry-After 秒数 / HTTP 日期，封顶 120 秒；
        没有就按连续 429 次数 10/20/40/60 秒退避、封顶 60 秒，加 ±20% 抖动）；
        用完再换备用模型，备用同样规则。其他 4xx 不重试、不换备用，直接抛。
        retries=None 用设置里的；主循环里直接 await 的调用传 1（别让循环卡几分钟）。
        max_tokens=None 用所选条目的 max_tokens（缺省 32768）；传了以调用方为准。
        请求体里总是带 max_tokens（有些端点没有它会出错；anthropic 必填）。
        每次尝试（成功或失败）写一条 usage + 一条 model_calls；usage 的 role 记
        实际干活链路分桶（主模型兜底也记 main），agent 记调用方报的岗位。
        发请求前过端点限流门：并发上限 max_concurrency（缺省 2）+ 429 冷却 +
        每分钟上限 max_rpm（缺省 0 = 关）；等待可取消，状态只在内存。
        """
        settings = self._get_settings()  # 缓存判热更新用
        s = self.settings()
        if not s.ready():
            raise ModelError("模型还没配好")

        msgs = list(messages or [])
        agent_kind = str(agent or "").strip()
        if not agent_kind:
            agent_kind = "main" if str(role or "") == "main" else "task"
        candidates = self._current_candidates(agent_kind)
        if not candidates:
            if agent_kind == "main":
                raise ModelError("「主模型」还没挑模型：到网页「专岗」页给它选一个模型")
            raise ModelError(f"「{agent_kind}」专岗还没挑模型，连主模型也没选好：到网页「专岗」页先选一个模型")

        # 记录桶（两桶账不变）：自己的链成了 = 它自己（main 桶 / 别的都 worker 桶）；
        # 岗位没候选兜底到主模型链 = 主模型桶
        role_kind = agent_kind
        if self._agents is not None and agent_kind != "main":
            try:
                own = self._resolve_candidates(
                    agent_kind,
                    tuple(getattr(settings, "endpoints", ()) or ()),
                    tuple(getattr(settings, "model_list", ()) or ()),
                    self._legacy_brick(settings),
                    self._agents,
                )
            except Exception:
                own = []
            if not own:
                role_kind = "main"
        role_effective = "main" if role_kind == "main" else "worker"
        # 思考强度：跟「用谁的链」一致——用它自己的 profile 强度；兜底用主模型的
        requested_effort = self._profile_effort(role_kind)

        # 所有端点的密钥都进遮罩名单（备用在另一个端点时也可能泄进错误文本）
        secret_keys = self._all_endpoint_keys(settings)
        last_err: ModelError | None = None
        attempt = 0
        client = self._get_client()
        for pos, cand in enumerate(candidates):
            model = cand.service_model
            ep = cand.endpoint
            key = str(getattr(ep, "api_key", "") or "")
            base_url = str(getattr(ep, "base_url", "") or "").rstrip("/")
            protocol = str(getattr(ep, "protocol", "openai") or "openai").strip().lower()
            ep_retries = _limit_from(ep, "retries", 5, 0, 10)
            ep_delay_s = float(_limit_from(ep, "retry_delay_s", 10, 1, 60))
            max_conc = _limit_from(ep, "max_concurrency", _MAX_CONCURRENCY_DEFAULT, 1, 64)
            max_rpm = _limit_from(ep, "max_rpm", 0, 0, 1_000_000)
            # 三种协议的地址 + 请求头；不认识的（防配置漏校验）跳过这个候选尝下一个
            if protocol == "openai":
                url = base_url + "/chat/completions"
                headers = {"Authorization": f"Bearer {key}"}
            elif protocol == "responses":
                url = base_url + "/responses"
                headers = {"Authorization": f"Bearer {key}"}
            elif protocol == "anthropic":
                url = _anthropic_messages_url(base_url)
                headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            else:
                last_err = ModelError(f"端点「{getattr(ep, 'name', '')}」的协议「{protocol}」不认识（只能 openai / responses / anthropic）")
                continue
            # 这个候选能发的强度：岗位请求的强度得在这个条目的 efforts 里勾了才发；
            # 条目没勾任何 efforts（空）= 不支持思考强度 → 永不发
            effort_send = requested_effort if requested_effort in cand.efforts else ""
            # 限流键：端点 id + 规范化地址（同一 id 换地址也不串冷却）
            endpoint = f"{getattr(ep, 'id', '')}|{self._throttle.normalize(base_url)}"
            max_tries = 1 + (ep_retries if retries is None else max(0, int(retries)))
            body_max_tokens = int(max_tokens) if (max_tokens is not None and int(max_tokens) > 0) else int(cand.max_tokens or 32768)
            if body_max_tokens <= 0:
                body_max_tokens = 32768
            log_request = self._build_log_request(msgs, tools, json_mode, secret_keys, body_max_tokens)
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
                        wait = min(max(ep_delay_s, 0.0), _RETRY_DELAY_CAP_S)
                        logger.warning(
                            "模型 %s 第 %d 次尝试失败（%s），%.0f 秒后重试（%d/%d）",
                            model, try_n - 1, last_err, wait, try_n, max_tries,
                        )
                        await _SLEEP(wait)
                elif pos > 0:
                    logger.warning("模型 %s 失败（%s），换备用 %s 再试", candidates[pos - 1].service_model, last_err, model)
                # 请求体按协议拼（纯函数，各自注释里有 json_mode / effort 的规矩）
                body: dict[str, Any]
                if protocol == "openai":
                    body = _build_openai_request(model, msgs, tools=tools, json_mode=json_mode,
                                                 max_tokens=body_max_tokens, effort=effort_send)
                elif protocol == "responses":
                    body = _build_responses_request(model, msgs, tools=tools, json_mode=json_mode,
                                                    max_tokens=body_max_tokens, effort=effort_send)
                else:  # anthropic
                    body = _build_anthropic_request(model, msgs, tools=tools, json_mode=json_mode,
                                                    max_tokens=body_max_tokens, effort=effort_send)
                start = clock.now()
                status = 0
                try:
                    async with self._throttle.slot(endpoint, concurrency=max_conc, max_rpm=max_rpm):
                        status, err_text, data = await asyncio.wait_for(
                            self._post_protocol(client, protocol, url, body, headers, timeout), timeout=timeout
                        )
                except (httpx.HTTPError, asyncio.TimeoutError, _StreamError) as e:
                    ms = int((clock.now() - start) * 1000)
                    if isinstance(e, asyncio.TimeoutError):
                        detail = f"网络错误（ReadTimeout）：超过 {timeout:.0f} 秒还没回完"
                    elif isinstance(e, _StreamError):
                        detail = f"流中断：{e}"
                    else:
                        d = str(e).strip()
                        detail = f"网络错误（{type(e).__name__}）{('：' + d) if d else ''}"
                    last_err = ModelError(_redact(detail, [key]), status=502 if isinstance(e, _StreamError) else None)
                    self._log_attempt(
                        model, role_effective, attempt, ok=False, status=status if isinstance(e, _StreamError) else 0, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=last_err.message,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id, agent=agent_kind,
                    )
                    continue
                ms = int((clock.now() - start) * 1000)
                if status >= 500 or status in (429, 408):
                    last_err = ModelError(
                        _redact(f"端点返回 {status}：{err_text}", [key]),
                        status=status,
                    )
                    if status == 429:
                        # 整个端点进冷却：Retry-After（秒数 / HTTP 日期）优先，封顶 120 秒；
                        # 没有就按连续 429 次数退避（10/20/40/60，封顶 60，±20% 抖动）
                        self._throttle.note_429(
                            endpoint,
                            _parse_retry_after((data or {}).get("retry_after"), _NOW()),
                            concurrency=max_conc,
                        )
                    self._log_attempt(
                        model, role_effective, attempt, ok=False, status=status, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=last_err.message,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id, agent=agent_kind,
                    )
                    continue
                if status != 200:
                    # 其他 4xx：不重试、不换备用，直接抛
                    err = ModelError(
                        _redact(f"端点返回 {status}：{err_text}", [key]),
                        status=status,
                    )
                    self._log_attempt(
                        model, role_effective, attempt, ok=False, status=status, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=err.message,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id, agent=agent_kind,
                    )
                    raise err
                self._throttle.note_success(endpoint, concurrency=max_conc)
                try:
                    if protocol == "openai":
                        result = self._parse_chat(data, model)
                    elif protocol == "responses":
                        result = _parse_responses_response(data, model)
                    else:
                        result = _parse_anthropic_response(data, model)
                except ModelError as e:
                    msg = _redact(str(e), [key])
                    self._log_attempt(
                        model, role_effective, attempt, ok=False, status=200, ms=ms,
                        prompt_tokens=0, completion_tokens=0, error=msg,
                        request=log_request, response=None, keys=secret_keys,
                        purpose=purpose, group_id=group_id, task_id=task_id, agent=agent_kind,
                    )
                    raise
                log_response = self._build_log_response(data, result, secret_keys)
                self._log_attempt(
                    model, role_effective, attempt, ok=True, status=200, ms=ms,
                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                    error="", request=log_request, response=log_response, keys=secret_keys,
                    purpose=purpose, group_id=group_id, task_id=task_id, agent=agent_kind,
                )
                return result
        assert last_err is not None
        raise last_err

    async def _post_protocol(
        self,
        client: httpx.AsyncClient,
        protocol: str,
        url: str,
        body: dict,
        headers: dict | None,
        timeout: float,
    ) -> tuple[int, str, dict]:
        """按协议发一次请求，统一回 (状态码, 出错时响应文本, 拼好的「非流式」形状 data)。

        - openai：/chat/completions 流式（缘由见 _stream_openai）；data 是 openai 形状；
        - responses：/responses。stream=True 发出去，端点回 SSE 就把事件收成整份
          （_responses_from_events），回普通 JSON 直接用；
        - anthropic：/v1/messages 非流式。
        """
        if protocol == "openai":
            return await self._stream_openai(client, url, body, headers or {}, timeout)
        if protocol == "anthropic":
            return await self._post_json(client, url, body, headers or {}, timeout)
        return await self._post_responses(client, url, body, headers or {}, timeout)

    @staticmethod
    async def _post_json(
        client: httpx.AsyncClient, url: str, body: dict, headers: dict, timeout: float
    ) -> tuple[int, str, dict]:
        """非流式 POST（anthropic messages 走它）；非 200 回响应文本，429 带 retry_after。"""
        resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return resp.status_code, resp.text, {"retry_after": resp.headers.get("Retry-After")}
        try:
            data = resp.json()
        except ValueError:
            raise ModelError("模型返回格式不对：不是 JSON") from None
        return 200, "", data if isinstance(data, dict) else {}

    @classmethod
    async def _post_responses(
        cls,
        client: httpx.AsyncClient, url: str, body: dict, headers: dict, timeout: float
    ) -> tuple[int, str, dict]:
        """Responses：响是 SSE 就收事件拼整份；不是 SSE（多数兼容端点）当 JSON 直接解析。"""
        async with client.stream("POST", url, json=body, headers=headers, timeout=timeout) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return resp.status_code, resp.text, {"retry_after": resp.headers.get("Retry-After")}
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                await resp.aread()
                try:
                    data = resp.json()
                except ValueError:
                    raise ModelError("模型返回格式不对：不是 JSON") from None
                return 200, "", data if isinstance(data, dict) else {}
            events: list[dict] = []
            done = False
            completed_seen = False
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise _StreamError(str(msg)[:500])
                events.append(obj)
                if obj.get("type") == "response.completed":
                    completed_seen = True
            if not done and not completed_seen:
                raise _StreamError("连接断了，回答没收完")
            return 200, "", _responses_from_events(events)

    @staticmethod
    async def _stream_openai(
        client: httpx.AsyncClient, url: str, body: dict, headers: dict, timeout: float
    ) -> tuple[int, str, dict]:
        """发一次（流式）请求，返回 (状态码, 出错时的响应文本, 拼好的「非流式」形状 data)。

        200 + text/event-stream：逐行读 SSE，拼 content / tool_calls（按 index 拼 arguments）/
        finish_reason / usage；reasoning_content 只让连接保持有字节，不进结果。
        流里出现 {"error": ...}、或既没 [DONE] 也没 finish_reason 就断了 → _StreamError（可重试）。
        200 + 别的类型（端点不支持流）：按普通 JSON 解析。
        非 200：返回响应文本；429 时 data 里带 retry_after 头。
        """
        async with client.stream(
            "POST", url, json=body, headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return resp.status_code, resp.text, {"retry_after": resp.headers.get("Retry-After")}
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" not in ctype:
                await resp.aread()
                try:
                    data = resp.json()
                except ValueError:
                    raise ModelError("模型返回格式不对：不是 JSON") from None
                return 200, "", data if isinstance(data, dict) else {}
            text_parts: list[str] = []
            calls: dict[int, dict] = {}
            finish_reason = ""
            usage: dict = {}
            done = False
            role = "assistant"
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else err
                    raise _StreamError(str(msg or err)[:500])
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    if not isinstance(ch, dict):
                        continue
                    if ch.get("finish_reason"):
                        finish_reason = str(ch["finish_reason"])
                    delta = ch.get("delta") or ch.get("message") or {}
                    if not isinstance(delta, dict):
                        continue
                    if delta.get("role"):
                        role = str(delta["role"])
                    if isinstance(delta.get("content"), str):
                        text_parts.append(delta["content"])
                    for pos, tc in enumerate(delta.get("tool_calls") or []):
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        idx = int(idx) if isinstance(idx, int) else pos
                        slot = calls.setdefault(
                            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        )
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        if tc.get("type"):
                            slot["type"] = str(tc["type"])
                        fn = tc.get("function") or {}
                        if isinstance(fn, dict):
                            name = fn.get("name")
                            if name and name != slot["function"]["name"]:
                                slot["function"]["name"] += str(name)
                            if isinstance(fn.get("arguments"), str):
                                slot["function"]["arguments"] += fn["arguments"]
            if not done and not finish_reason:
                raise _StreamError("连接断了，回答没收完")
            message: dict[str, Any] = {"role": role, "content": "".join(text_parts)}
            if calls:
                message["tool_calls"] = [calls[i] for i in sorted(calls)]
            return 200, "", {
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": usage,
            }

    # ------------------------------------------------------------------
    # 请求日志（model_calls）：每次尝试一行，密钥绝不入库
    # ------------------------------------------------------------------

    @staticmethod
    def _build_log_request(
        messages: list[dict], tools: list[dict] | None, json_mode: bool, keys: list[str],
        max_tokens: int | None = None,
    ) -> str:
        """把这次调用请求底稿拼成 JSON 字符串（只拼一次，每次尝试复用）。

        只存 max_tokens（这次实际带的输出上限，放最前面，整份被截也看得到）、
        messages（content 截 _LOG_CONTENT_MAX；tool_calls 只要名字+参数截 _LOG_TOOL_ARGS_MAX）、
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
            {"max_tokens": max_tokens, "messages": out_messages, "tools": tool_names, "json_mode": bool(json_mode)},
            ensure_ascii=False,
        )
        return payload[:_LOG_REQUEST_MAX]

    @staticmethod
    def _build_log_response(data: Any, result: ChatResult, keys: list[str]) -> str:
        """响应 JSON：text（截 _LOG_RESPONSE_TEXT_MAX）/tool_calls（name+arguments 截 1000）/finish_reason。"""
        finish_reason = ""
        try:
            finish_reason = str((data.get("choices") or [{}])[0].get("finish_reason") or "")
        except Exception:
            finish_reason = ""
        if not finish_reason:
            finish_reason = str(result.finish_reason or "")
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
        agent: str = "",
    ) -> None:
        """每次尝试写两条账：usage（原有）+ model_calls（管理员网页看）。写失败只记日志。"""
        now = clock.now()
        error_db = _redact_full(error, keys)[:_LOG_ERR_MAX]
        agent_s = str(agent or "")
        # 1) usage（原路；agent 列是 1b 新增的岗位账，role 两桶不变）
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO usage (ts, day, role, agent, model, purpose, group_id, task_id,"
                    " prompt_tokens, completion_tokens, ok, ms, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        clock.day_key(now),
                        str(role),
                        agent_s,
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
                    "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt, ok,"
                    " status, ms, prompt_tokens, completion_tokens, error, request, response)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        str(purpose or ""),
                        str(role),
                        agent_s,
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
            finish_reason=str(choice.get("finish_reason") or "") if isinstance(choice, dict) else "",
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
