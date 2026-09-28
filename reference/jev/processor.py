"""纯函数式 Planner 前置处理：过滤无关记忆，标记外部事实属性。"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import hashlib
import html
import json
import math
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .stats import Stats

MARKER = "【启发式记忆-内部参考】"
_MEMORY_HEADER = (
    "Internal long-term memory recalled from the current chat impression. "
    "Use it only as reasoning context; do not quote it verbatim to the user."
)
_MEMORY_PREFIX = f"{MARKER}\n{_MEMORY_HEADER}\n\n"

# 固定词表：模型只能在我们给定的标签里选，绝不生成自由文本进入 Planner。
MEMORY_KINDS = ("relevant", "same_entity", "surface_overlap", "unrelated")
MEMORY_KIND_CRITERIA = {
    "relevant": "提供理解当前话题所必需的客观背景",
    "same_entity": "涉及同一个具体主体，但没有提供当前话题所需的背景",
    "surface_overlap": "只是词语或宽泛主题重合，语义主题并不相同",
    "unrelated": "与当前话题没有实质关系",
}

FACT_MARKER_PREFIX = "【Jev客观属性】"
FACT_OBSERVATION = f"{FACT_MARKER_PREFIX}当前聊天包含依赖外部公开资料核验的客观事实要素。"
FACT_OBSERVATION_TEMPLATE = (
    f"{FACT_MARKER_PREFIX}当前聊天包含依赖外部公开资料核验的客观事实要素（类别：{{label}}）。"
)

FACT_KINDS = ("version", "price", "date", "identity", "spec", "none")
FACT_CATEGORY_LABELS = {
    "version": "版本/发布",
    "price": "价格/费用",
    "date": "日期/时效",
    "identity": "公开人物或作品归属",
    "spec": "技术规格",
}
FACT_KIND_CRITERIA = {
    "version": "版本号、发布时间、型号或迭代代次",
    "price": "价格、费用、成本或定价",
    "date": "具体日期、时间点或时效性事件",
    "identity": "公开人物、作品、机构或事物的归属与身份",
    "spec": "技术规格、参数或性能指标",
    "none": "没有依赖外部公开资料核验的要素",
}

_MESSAGE_PREFIX = re.compile(r"^<message\s+([^>]*)>", re.DOTALL)
_ATTR = re.compile(r'([a-zA-Z_]+)="([^"]*)"')
_NUMBERED_ENTRY = re.compile(r"(?m)^([1-9]\d*)\. (.+)$")


@dataclass(frozen=True)
class Settings:
    memory_relevance_threshold: float = 0.65
    fact_signal_threshold: float = 0.70
    max_messages: int = 12
    max_message_chars: int = 1200
    max_total_chars: int = 8000
    max_memories: int = 8
    max_memory_chars: int = 1200
    timeout_s: float = 1.5
    cache_ttl_s: float = 120.0
    max_cache_entries: int = 256
    breaker_failure_threshold: int = 3
    breaker_cooldown_s: float = 60.0
    memory_confirm_threshold: float = 0.60
    memory_kind_min_confidence: float = 0.60
    fact_kind_min_confidence: float = 0.60


@dataclass(frozen=True)
class _MemoryEntry:
    global_index: int
    content: str
    raw_line: str


@dataclass(frozen=True)
class _MemoryBlock:
    item_index: int
    raw_text: str
    entries: Tuple[_MemoryEntry, ...]


@dataclass(frozen=True)
class _Bundle:
    state: Dict[str, Any]
    blocks: Tuple[_MemoryBlock, ...]


@dataclass(frozen=True)
class _Applied:
    memories_dropped: int = 0
    blocks_dropped: int = 0
    memories_kept_same_entity: int = 0
    fact_markers: int = 0
    fact_categorized: int = 0


def _text_of_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks)


def _parse_chat_message(text: str, max_chars: int) -> Optional[Dict[str, str]]:
    match = _MESSAGE_PREFIX.match(text)
    if not match:
        return None
    attrs = dict(_ATTR.findall(match.group(1)))
    body = text[match.end() :]
    if body.endswith("</message>"):
        body = body[: -len("</message>")]
    body = html.unescape(body.strip())
    if not body:
        return None
    body = body[:max_chars]
    is_self = attrs.get("is_self_message", "").lower() == "true"
    return {"speaker": "BOT" if is_self else "USER", "text": body}


def _strict_memory_item_text(item: Any) -> Optional[str]:
    """只接受宿主生成的单 part、独立记忆项；普通聊天中的同名文本不匹配。"""
    if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
        return None
    parts = item.get("parts")
    if not isinstance(parts, list) or len(parts) != 1:
        return None
    part = parts[0]
    if not isinstance(part, dict) or part.get("type") != "text":
        return None
    text = part.get("text")
    if not isinstance(text, str) or not text.startswith(_MEMORY_PREFIX):
        return None
    return text


def _parse_memory_block(text: str, item_index: int, start_index: int, settings: Settings) -> Optional[_MemoryBlock]:
    matches = list(_NUMBERED_ENTRY.finditer(text, len(_MEMORY_PREFIX)))
    if not matches or matches[0].start() != len(_MEMORY_PREFIX):
        return None

    # 宿主逐行输出连续编号；任何额外前后缀或非规范行都按未知输入原样放行。
    cursor = len(_MEMORY_PREFIX)
    for expected_number, match in enumerate(matches, 1):
        if match.start() != cursor or int(match.group(1)) != expected_number:
            return None
        cursor = match.end()
        if expected_number < len(matches):
            if cursor >= len(text) or text[cursor] != "\n":
                return None
            cursor += 1
    if cursor != len(text):
        return None

    # 绝不对未送往 Jev 的尾部候选做隐式删除；超限时整块旁路。
    available = max(0, settings.max_memories - start_index)
    if len(matches) > available:
        return None

    entries = tuple(
        _MemoryEntry(
            global_index=start_index + offset,
            content=match.group(2)[: settings.max_memory_chars],
            raw_line=match.group(0),
        )
        for offset, match in enumerate(matches)
    )
    return _MemoryBlock(item_index=item_index, raw_text=text, entries=entries)


def _collect(items: Any, settings: Settings) -> Optional[_Bundle]:
    if not isinstance(items, list):
        return None
    messages: List[Dict[str, str]] = []
    blocks: List[_MemoryBlock] = []
    memory_count = 0
    for item_index, item in enumerate(items):
        memory_text = _strict_memory_item_text(item)
        if memory_text is not None:
            block = _parse_memory_block(memory_text, item_index, memory_count, settings)
            if block is not None:
                blocks.append(block)
                memory_count += len(block.entries)
            # 规范头但解析/上限不满足时整块旁路，绝不再当普通聊天处理。
            continue

        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        text = _text_of_item(item)
        if not text:
            continue
        message = _parse_chat_message(text, settings.max_message_chars)
        if message is not None:
            messages.append(message)

    # 先选最近窗口，再从新到旧执行总字符预算；旧历史不能毒死当前轮。
    messages = messages[-max(1, settings.max_messages) :]
    remaining = max(0, settings.max_total_chars)
    bounded_reversed: List[Dict[str, str]] = []
    for message in reversed(messages):
        if remaining <= 0:
            break
        text = message["text"][:remaining]
        if not text:
            continue
        bounded_reversed.append({"speaker": message["speaker"], "text": text})
        remaining -= len(text)
    messages = list(reversed(bounded_reversed))
    if not messages:
        return None

    memories = [
        {"id": f"m{entry.global_index}", "content": entry.content}
        for block in blocks
        for entry in block.entries
    ]
    return _Bundle(state={"messages": messages, "memories": memories}, blocks=tuple(blocks))


def collect_state(items: Any, settings: Settings) -> Dict[str, Any]:
    """公开的、无副作用的状态提取函数，测试与离线评估共用。"""
    bundle = _collect(items, settings)
    return copy.deepcopy(bundle.state) if bundle is not None else {}


def build_questions(memory_count: int) -> Dict[str, Dict[str, Any]]:
    questions: Dict[str, Dict[str, Any]] = {}
    for index in range(memory_count):
        questions[f"memory_{index}_relevant"] = {
            "type": "noul",
            "instructions": (
                f"Does `memories[{index}].content` contain objective background that is directly relevant "
                "to understanding the current topic in `messages`, rather than merely sharing words, names, "
                "or a broad theme?"
            ),
        }
        questions[f"memory_{index}_kind"] = {
            "type": "choice",
            "instructions": (
                f"What is the relation between `memories[{index}].content` and the current topic in "
                "`messages`? Pick the single label whose criterion fits best."
            ),
            "criteria": dict(MEMORY_KIND_CRITERIA),
        }
    questions["external_fact_content"] = {
        "type": "noul",
        "instructions": (
            "Does the current conversation in `messages` contain a concrete externally verifiable factual "
            "claim or question whose correctness depends on public information such as a version, price, "
            "date, public identity, release, event, or technical specification? Exclude opinions, jokes, "
            "roleplay, and private experiences that public sources cannot verify."
        ),
    }
    questions["fact_kind"] = {
        "type": "choice",
        "instructions": (
            "Which single kind of externally verifiable fact does the current conversation in `messages` "
            "revolve around most?"
        ),
        "criteria": dict(FACT_KIND_CRITERIA),
    }
    return questions


def _bounded(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid probability")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("invalid probability")
    return number


def _probability(answer: Any) -> float:
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise ValueError("missing noul answer")
    return _bounded(answer.get("noul"))


def _choice(answer: Any, allowed: Sequence[str]) -> Tuple[str, float, float]:
    """校验 choice 答案：标签必须来自固定词表，概率与 confidence 必须有界。"""
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("missing choice answer")
    label = answer.get("choice")
    if not isinstance(label, str) or label not in allowed:
        raise ValueError("invalid choice label")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(allowed):
        raise ValueError("invalid probabilities")
    return label, _bounded(probabilities.get(label)), _bounded(answer.get("confidence"))


def validate_answers(raw: Any, memory_count: int) -> Dict[str, Any]:
    """记忆相关答案缺失即整轮旁路（删除是破坏性操作）；事实类别可缺省。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
        raise ValueError("invalid response")
    answers = raw["answers"]
    result: Dict[str, Any] = {}
    for index in range(memory_count):
        result[f"memory_{index}_relevant"] = _probability(answers.get(f"memory_{index}_relevant"))
        result[f"memory_{index}_kind"] = _choice(answers.get(f"memory_{index}_kind"), MEMORY_KINDS)
    result["external_fact_content"] = _probability(answers.get("external_fact_content"))
    try:
        result["fact_kind"] = _choice(answers.get("fact_kind"), FACT_KINDS)
    except ValueError:
        result["fact_kind"] = None
    return result


def _inject_fact_observation(items: List[Dict[str, Any]], marker: str) -> bool:
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "SystemMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and any(
                line.startswith(FACT_MARKER_PREFIX) for line in text.splitlines()
            ):
                return False

    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "SystemMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            return False
        for part in reversed(parts):
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                part["text"] = part["text"] + "\n\n" + marker
                return True
        return False
    return False


def _apply(
    items: Sequence[Dict[str, Any]],
    bundle: _Bundle,
    answers: Dict[str, Any],
    settings: Settings,
) -> Tuple[List[Dict[str, Any]], _Applied]:
    output: List[Dict[str, Any]] = copy.deepcopy(list(items))
    delete_indices: List[int] = []
    memories_dropped = 0
    blocks_dropped = 0
    memories_kept_same_entity = 0
    fact_markers = 0
    fact_categorized = 0
    for block in bundle.blocks:
        kept = []
        for entry in block.entries:
            index = entry.global_index
            relevance = answers[f"memory_{index}_relevant"]
            if relevance >= settings.memory_relevance_threshold:
                kept.append(entry)
                continue
            kind, probability, confidence = answers[f"memory_{index}_kind"]
            # 只有「低相关」与「分类确认为撞词/无关」两个独立信号同时成立才删。
            # 同主体一律保留：它是低相关里最容易被误删的一类。
            if kind == "same_entity":
                kept.append(entry)
                memories_kept_same_entity += 1
                continue
            if kind == "relevant":
                kept.append(entry)
                continue
            if (
                probability < settings.memory_confirm_threshold
                or confidence < settings.memory_kind_min_confidence
            ):
                kept.append(entry)
                continue
        dropped = len(block.entries) - len(kept)
        memories_dropped += dropped
        if not kept:
            delete_indices.append(block.item_index)
            blocks_dropped += 1
            continue
        if dropped == 0:
            # 全保留时一个字节也不改。
            continue

        # 宿主原块已经过严格格式验证；这里只删除落选的原始行，不重排、不重编号、不规范化。
        filtered = _MEMORY_PREFIX + "\n".join(entry.raw_line for entry in kept)
        item = output[block.item_index]
        parts = item.get("parts") if isinstance(item, dict) else None
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            raise ValueError("memory item shape changed after collection")
        parts[0]["text"] = filtered

    for item_index in sorted(delete_indices, reverse=True):
        del output[item_index]
    if answers["external_fact_content"] >= settings.fact_signal_threshold:
        label = None
        fact_kind = answers.get("fact_kind")
        if fact_kind is not None:
            kind, _probability_value, confidence = fact_kind
            if kind in FACT_CATEGORY_LABELS and confidence >= settings.fact_kind_min_confidence:
                label = FACT_CATEGORY_LABELS[kind]
        marker = FACT_OBSERVATION_TEMPLATE.format(label=label) if label else FACT_OBSERVATION
        if _inject_fact_observation(output, marker):
            fact_markers += 1
            if label:
                fact_categorized += 1
    return output, _Applied(
        memories_dropped=memories_dropped,
        blocks_dropped=blocks_dropped,
        memories_kept_same_entity=memories_kept_same_entity,
        fact_markers=fact_markers,
        fact_categorized=fact_categorized,
    )


class Processor:
    """单轮同步前置器；任何异常都返回 None，调用方保持原请求不变。"""

    def __init__(
        self,
        settings: Settings,
        *,
        evaluate: Callable[[Dict[str, Any], Dict[str, Dict[str, Any]]], Awaitable[Dict[str, Any]]],
        clock: Callable[[], float] = time.monotonic,
        stats: Optional[Stats] = None,
    ) -> None:
        self.settings = settings
        self.evaluate = evaluate
        self.clock = clock
        self.stats = stats if stats is not None else Stats()
        self._cache: Dict[str, Tuple[float, Dict[str, float]]] = {}
        self._inflight: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @staticmethod
    def _cache_key(state: Dict[str, Any]) -> str:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _prune_cache(self, now: float) -> None:
        ttl = max(0.0, float(self.settings.cache_ttl_s))
        self._cache = {key: value for key, value in self._cache.items() if ttl > 0 and now - value[0] < ttl}
        if len(self._cache) > self.settings.max_cache_entries:
            oldest = sorted(self._cache.items(), key=lambda item: item[1][0])
            for key, _ in oldest[: len(self._cache) - self.settings.max_cache_entries]:
                self._cache.pop(key, None)

    def _breaker_allows(self, now: float) -> bool:
        return now >= self._breaker_open_until

    def _record_failure(self, now: float) -> None:
        self._consecutive_failures += 1
        threshold = max(1, int(self.settings.breaker_failure_threshold))
        if self._consecutive_failures >= threshold:
            self._breaker_open_until = now + max(0.0, float(self.settings.breaker_cooldown_s))
            self.stats.breaker_open_events += 1

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    def _release(self, key: str, finished: "asyncio.Future[Dict[str, Any]]") -> None:
        self._inflight.pop(key, None)
        try:
            finished.exception()
        except BaseException:  # noqa: BLE001 - 仅用于消费未取回的异常/取消
            pass

    async def _evaluate_shared(
        self,
        key: str,
        state: Dict[str, Any],
        questions: Dict[str, Dict[str, str]],
    ) -> Dict[str, Any]:
        """同一窗口并发进入时只发一次上游请求，其余等待同一结果。"""
        task = self._inflight.get(key)
        if task is not None:
            self.stats.joins += 1
        else:
            async def _run() -> Dict[str, Any]:
                started = self.clock()
                try:
                    return await self.evaluate(copy.deepcopy(state), copy.deepcopy(questions))
                finally:
                    self.stats.record_latency((self.clock() - started) * 1000.0)

            task = asyncio.ensure_future(_run())
            self._inflight[key] = task
            self.stats.upstream_calls += 1
            task.add_done_callback(lambda finished: self._release(key, finished))
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(0.1, float(self.settings.timeout_s)),
        )

    async def process(self, items: Any) -> Optional[List[Dict[str, Any]]]:
        self.stats.rounds += 1
        bundle = _collect(items, self.settings)
        if bundle is None:
            self.stats.no_state += 1
            return None
        questions = build_questions(len(bundle.state["memories"]))
        key = self._cache_key(bundle.state)
        now = self.clock()
        self._prune_cache(now)
        cached = self._cache.get(key)
        if cached is not None:
            self.stats.cache_hits += 1
            answers = cached[1]
        else:
            if not self._breaker_allows(now):
                self.stats.breaker_skips += 1
                return None
            try:
                raw = await self._evaluate_shared(key, bundle.state, questions)
            except asyncio.TimeoutError:
                self.stats.timeouts += 1
                self._record_failure(now)
                return None
            except Exception:
                self.stats.upstream_fail += 1
                self._record_failure(now)
                return None
            try:
                answers = validate_answers(raw, len(bundle.state["memories"]))
            except Exception:
                self.stats.upstream_fail += 1
                self._record_failure(now)
                return None
            self.stats.upstream_ok += 1
            self._record_success()
            if self.settings.cache_ttl_s > 0:
                self._cache[key] = (now, answers)
        try:
            output, applied = _apply(items, bundle, answers, self.settings)
        except Exception:
            return None
        self.stats.memories_dropped += applied.memories_dropped
        self.stats.blocks_dropped += applied.blocks_dropped
        self.stats.memories_kept_same_entity += applied.memories_kept_same_entity
        self.stats.fact_markers += applied.fact_markers
        self.stats.fact_categorized += applied.fact_categorized
        if output == items:
            self.stats.unchanged += 1
        else:
            self.stats.applied += 1
        return output

    def clear(self) -> None:
        self._cache.clear()
