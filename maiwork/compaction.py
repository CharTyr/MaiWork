"""上下文压缩（docs/07 §14；workers 子 agent / coordinator 主模型 / admin_chat 共用）。

估算方法（写死，不依赖第三方分词库）：
- tokens ≈ ceil(UTF-8 可见字符数 / 1.3)：中英文混排大致对得上，略偏保守。
  messages 的整体估算还把 assistant 的 tool_calls 参数算进去。
  （模型端点实际用 tokenizer，估算值只用于「什么时候压」，不是计费。）

两级动作（触发线 = min(W×0.8, W − O − 65536)，W = [models] context_window，O = 本次
调用的输出预留 tokens）：
1. 先不调模型：较旧的 tool 结果里超过 8192 字符的，裁成「开头 4096 + 结尾 1024」，
   中间放一句省略说明；裁完估算低于触发线就收工。
2. 仍超：把对话里最老的一段总结成**一条** user 摘要消息（固定 8 个小节），
   最近约 16%（按 W−O 计）原样保留；assistant(tool_calls) 和它的 tool 结果成组
   切分，绝不拆开；system prompt 永不进摘要、永不删。
   摘要调模型照常用量记账（purpose 追加 ":compact"）；摘要失败：原样返回，不抛。

另外三件事也在这里：
- 超大工具结果落盘（spill_big_output）：单条工具输出超过约 5 万字时，完整内容写到
  该任务/对话归属目录下的一个文件，对话里只放「开头 + 结尾 + 路径说明」。
- 「上下文超长」类错误（looks_like_context_length）：chat_with_retry_on_long_context
  把最旧一段裁掉一次再重试一次；再失败原样抛出。
- 重复调用提醒（RepeatCallNudger）：同一工具 + 规范化参数连用第 3/5/8 次给一句提醒；
  新的 user 消息进来计数清零。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from .models import ModelError

logger = logging.getLogger("maiwork.compaction")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 1.3            # 估算比例（见模块 docstring）
WINDOW_FACTOR = 0.8               # 触发线之一：W × 0.8
RESERVE_TOKENS = 65536            # 触发线之二：W − O − 65536
DEFAULT_OUTPUT_RESERVE = 8192     # 调用方没给输出预留时的默认
KEEP_RECENT_FACTOR = 0.16         # 摘要时原样保留最近 «约 16%»（按 W−O 计）
BIG_TOOL_CHARS = 8192             # 压缩第一阶段：超过它的旧 tool 结果会被截
TOOL_HEAD_CHARS = 4096
TOOL_TAIL_CHARS = 1024
SPILL_CHARS = 12500 * 4           # ≈ 5 万字（≈ 12500 tokens）的超长工具结果落盘
SPILL_HEAD_CHARS = 4000
SPILL_TAIL_CHARS = 1200
KEEP_RECENT_TOOL_RESULTS = 1      # 第一阶段里「最近 1 条」tool 结果不算旧
MIN_SUMMARIZE_PIECE = 2           # 可切的「最早一段」至少要几条才有意义

_SUMMARY_SECTIONS = (
    "Primary Request and Intent（最主要的目标）",
    "Key Technical Concepts（关键技术概念）",
    "Files and Code（涉及的文件和代码）",
    "Errors and Fixes（遇到的错误和怎么修的）",
    "Pending Jobs（还没做完的事）",
    "Current Work（刚才正在做的事）",
    "Next Step（下一步要做什么）",
    "Critical Context（其他必须留住的背景）",
)

_LONG_RE = re.compile(
    r"context.{0,30}(length|exceed|too.{0,10}long|overflow)|"
    r"maximum.{0,20}context|"
    r"prompt.{0,30}too.{0,10}long|"
    r"context_length_exceeded|"
    r"超出.{0,6}上下文|上下文.{0,6}超",
    re.IGNORECASE,
)


def looks_like_context_length(text: Any) -> bool:
    """这条错误文本是不是「上下文超长」类（4xx 不重试之外的唯一例外由调用方处理）。"""
    return bool(_LONG_RE.search(str(text or "")))


# ---------------------------------------------------------------------------
# 估算
# ---------------------------------------------------------------------------


def estimate_tokens(text: Any) -> int:
    """一段文本约几个 token（ceil(字符数 / 1.3)）。"""
    s = str(text or "")
    if not s:
        return 0
    return max(1, math.ceil(len(s) / CHARS_PER_TOKEN))


def estimate_message_tokens(msg: dict) -> int:
    """一条消息的估算：content + assistant 的 tool_calls 参数。"""
    if not isinstance(msg, dict):
        return 0
    total = estimate_tokens(msg.get("content"))
    for tc in msg.get("tool_calls") or ():
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if args is not None:
            total += estimate_tokens(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
    return total


def estimate_tokens_in_messages(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages if isinstance(m, dict))


def compact_threshold(context_window: int, output_reserve: int) -> int:
    """触发线：min(W×0.8, W − O − 65536)。"""
    w = max(0, int(context_window))
    o = max(0, int(output_reserve))
    return max(8192, min(int(w * WINDOW_FACTOR), w - o - RESERVE_TOKENS))


def _head_tail(text: str, head: int, tail: int) -> str:
    """开头 head + 结尾 tail，中间一句省略说明。"""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n……（中间省略 {omitted} 字）……\n\n"
        + text[-tail:]
    )


# ---------------------------------------------------------------------------
# 第一阶段：截掉较旧的超大 tool 结果
# ---------------------------------------------------------------------------


def truncate_big_tool_outputs(
    messages: list[dict],
    *,
    keep_recent_n: int = KEEP_RECENT_TOOL_RESULTS,
    big_chars: int = BIG_TOOL_CHARS,
    head: int = TOOL_HEAD_CHARS,
    tail: int = TOOL_TAIL_CHARS,
) -> tuple[list[dict], int]:
    """较旧的 tool 结果里超过 big_chars 的裁成「头 + 尾 + 省略说明」。

    - 最近一次（最近 keep_recent_n 条）tool 结果不动（模型刚拿到，马上要用）；
    - 只动 role == "tool" 的消息；
    - 一处都不用动就返回 (原列表, 0)；否则返回 (新列表, 动了几条)。
    """
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "tool"]
    if not tool_indices:
        return messages, 0
    keep_n = max(0, int(keep_recent_n))
    protected = set(tool_indices[len(tool_indices) - keep_n:]) if keep_n else set()
    out: list[dict] = list(messages)
    changed = 0
    for i in tool_indices:
        if i in protected:
            continue
        m = out[i]
        content = m.get("content")
        if not isinstance(content, str) or len(content) <= big_chars:
            continue
        new_m = dict(m)
        new_m["content"] = _head_tail(content, head, tail)
        out[i] = new_m
        changed += 1
    if changed == 0:
        return messages, 0
    return out, changed


# ---------------------------------------------------------------------------
# 分组成「不可切的小块」：assistant(tool_calls) 和他的 tool 结果一组
# ---------------------------------------------------------------------------


def _split_groups(messages: list[dict]) -> list[list[dict]]:
    """把 messages 按「不可切开」切成组。

    - 每条 user / 没有 tool_calls 的 assistant / system 单独一组；
    - assistant(tool_calls) 后跟的、tool_call_id 匹配的那些 tool 消息并进它的组；
    - 孤儿 tool（前面没有 assistant(tool_calls)）单独一组（不该出现，但万一有）。
    """
    groups: list[list[dict]] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            call_ids = {
                str(tc.get("id") or "")
                for tc in m.get("tool_calls") or ()
                if isinstance(tc, dict) and str(tc.get("id") or "")
            }
            group = [m]
            j = i + 1
            while j < n and messages[j].get("role") == "tool" and (
                not call_ids or str(messages[j].get("tool_call_id") or "") in call_ids
            ):
                group.append(messages[j])
                j += 1
            groups.append(group)
            i = j
            continue
        groups.append([m])
        i += 1
    return groups


def pick_cut_point(
    messages: list[dict],
    *,
    context_window: int,
    output_reserve: int,
    keep_recent_factor: float = KEEP_RECENT_FACTOR,
) -> tuple[list[dict], list[dict]]:
    """(要保留的消息, 要被总结的最老一段)。

    - system 永远在「保留」的开头，永不进「待总结」；
    - 「最近约 16%（按 W−O 计）原样保留」：从末尾往前按 token 数收；
    - tool_call 组不可拆；最后一条 user 消息及其后的一律保留（摘要里要接续上）；
    - 「最老一段」至少要 MIN_SUMMARIZE_PIECE 条才值得总结，否则返回 (全部, [])。
    """
    if not messages:
        return [], []
    groups = _split_groups(messages)

    # system 组（一般在最前面）单独留出来，永不进待总结、永不被砍
    system_groups: list[list[dict]] = []
    body_groups: list[list[dict]] = []
    for g in groups:
        if all(m.get("role") == "system" for m in g):
            system_groups.append(g)
        else:
            body_groups.append(g)

    # 保留目标（token）：(W−O) × keep_recent_factor
    budget = max(1024, int((int(context_window) - max(0, int(output_reserve))) * keep_recent_factor))

    keep_body: list[list[dict]] = []
    used = 0
    cut_body: list[list[dict]] = []
    for i in range(len(body_groups) - 1, -1, -1):
        g = body_groups[i]
        tokens = estimate_tokens_in_messages(g)
        if used + tokens > budget and used >= budget // 2:
            cut_body = body_groups[: i + 1]
            break
        keep_body.insert(0, g)
        used += tokens

    keep = [m for g in (system_groups + keep_body) for m in g]
    cut = [m for g in cut_body for m in g]
    if len(cut) < MIN_SUMMARIZE_PIECE:
        return list(messages), []
    return keep, cut


# ---------------------------------------------------------------------------
# 差一条 user 摘要替换最老一段
# ---------------------------------------------------------------------------


def _serialize_cut(cut: list[dict], *, max_chars: int = 32000) -> str:
    """把要被总结的那一段摊成纯文本给摘要模型看。"""
    lines: list[str] = []
    for m in cut:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "?")
        content = str(m.get("content") or "")
        if m.get("tool_calls"):
            names = []
            for tc in m.get("tool_calls") or ():
                if isinstance(tc, dict):
                    names.append(str((tc.get("function") or {}).get("name") or ""))
            content = content + f"（调用工具：{'、'.join(x for x in names if x)}）"
        lines.append(f"[{role}] {content}")
    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = _head_tail(text, int(max_chars * 0.6), int(max_chars * 0.2))
    return text


def ensure_sections(text: str) -> str:
    """保证 8 个小节标题齐全：模型少写的补一个「（模型没单独写这一节）」。"""
    body = str(text or "").strip()
    missing = [s for s in _SUMMARY_SECTIONS if s.split("（")[0].strip().lower() not in body.lower()]
    if not missing:
        return body
    extra = "\n\n" + "\n".join(f"## {s}\n（上一版摘要没单独写这一节，要点已在其他小节里。）" for s in missing)
    return body + extra if body else "\n".join(f"## {s}\n" for s in _SUMMARY_SECTIONS)


def summary_to_message(text: str) -> dict:
    """摘要文本包成「带 8 个小节标题的一条 user 消息」（8 节由 ensure_sections 兜底齐全）。"""
    body = ensure_sections(text)
    return {
        "role": "user",
        "content": (
            "【前面对话的摘要】（本摘要替代了更早的多轮对话，继续接着做事即可）\n\n"
            + body
        ),
    }


def sections_prompt() -> str:
    """给摘要模型看的 8 个小节清单（中英对照，模型只回内容，不用自带标题也行）。"""
    return "、".join(_SUMMARY_SECTIONS)


async def summarize_messages(
    piece: list[dict],
    *,
    models: Any,
    role: str,
    agent: str | None = None,
    purpose: str,
    group_id: str = "",
    task_id: str = "",
) -> str:
    """公开版：把一段 OpenAI messages 总结成 8 节中文摘要文本（失败抛 ModelError）。"""
    return await _summarize_piece(piece, models=models, role=role, agent=agent, purpose=purpose,
                                  group_id=group_id, task_id=task_id)


async def _summarize_piece(
    piece: list[dict],
    *,
    models: Any,
    role: str,
    agent: str | None = None,
    purpose: str,
    group_id: str = "",
    task_id: str = "",
) -> str:
    """调模型总结一段对话；返回 8 节格式的中文摘要文本。失败抛 ModelError。

    role 只是后兼容口（没传 agent 时 models.chat 自己映射 main⇒main / worker⇒task）；
    1b 起各调用方经 compaction 的 role= 照旧传，真正的岗位有专门的 agent 时走 agent 参数。
    """
    body = _serialize_cut(piece)
    prompt = (
        "把下面这段模型和工具的对话压成一份「站用摘要」，之后新看这段摘要的人 / 模型\n"
        "要能完全接续原来的工作。用中文写，只写事实，怎么做的就怎么写，别评价。\n"
        "必须写齐这 8 个小节，每个小节开头写上这一节的标题（照抄下面的标题，可只写中文）：\n"
        + sections_prompt()
        + "\n\n写得详细一些，每节 1–5 句；没有内容的小节写「无」。\n\n对话：\n" + body
    )
    result = await models.chat(
        role,
        [{"role": "user", "content": prompt}],
        agent=agent,
        purpose=f"{purpose or 'chat'}:compact" if not str(purpose or "").endswith(":compact") else str(purpose),
        group_id=str(group_id or ""),
        task_id=str(task_id or ""),
    )
    return str(result.text or "").strip()


async def maybe_compact(
    messages: list[dict],
    *,
    models: Any,
    role: str,
    agent: str | None = None,
    context_window: int,
    output_reserve: int | None = None,
    purpose: str = "",
    group_id: str = "",
    task_id: str = "",
    keep_recent_n: int = KEEP_RECENT_TOOL_RESULTS,
) -> list[dict]:
    """入口：估算超过触发线时先做第一阶段（截旧 tool 结果），必要时再摘要最老一段。

    - 返回替代用的 messages（可能原样返回）；
    - 摘要失败：原样返回（**不抛错**），任务继续。
    - system prompt 永远在最前面，从未被摘要。
    """
    if not messages:
        return messages
    reserve = DEFAULT_OUTPUT_RESERVE if output_reserve is None else int(output_reserve)
    threshold = compact_threshold(context_window, reserve)
    if estimate_tokens_in_messages(messages) < threshold:
        return messages

    # 第一阶段：截较旧的超长 tool 结果（不调模型）
    stage1, changed = truncate_big_tool_outputs(messages, keep_recent_n=keep_recent_n)
    if changed and estimate_tokens_in_messages(stage1) < threshold:
        return stage1

    # 第二阶段：摘要最老一段
    keep, cut = pick_cut_point(stage1, context_window=context_window, output_reserve=reserve)
    if not cut:
        # 没什么可切：返回第一阶段的成果（至少动过）
        return stage1
    try:
        summary_text = await _summarize_piece(
            cut, models=models, role=role, agent=agent, purpose=purpose,
            group_id=group_id, task_id=task_id,
        )
    except ModelError as e:
        logger.warning("上下文摘要失败（%s），原样继续", e.message if hasattr(e, "message") else e)
        return stage1
    except Exception as e:  # pragma: no cover - 防御
        logger.exception("上下文摘要意外失败")
        return stage1
    if not summary_text:
        return stage1
    summary_msg = summary_to_message(summary_text)
    # 结构：system（如果有，在 keep 开头）+ 摘要 + 其余 keep
    return [summary_msg if m is None else m for m in _merge_keep(keep, summary_msg)]


def _merge_keep(keep: list[dict], summary_msg: dict) -> list[Any]:
    """把摘要消息插到「keep 里第一条非 system」之前；如果没有非 system 就放最后。"""
    out: list[Any] = []
    inserted = False
    for m in keep:
        if not inserted and isinstance(m, dict) and m.get("role") != "system":
            out.append(summary_msg)
            inserted = True
        out.append(m)
    if not inserted:
        out.append(summary_msg)
    return out


# ---------------------------------------------------------------------------
# 「上下文超长」错误：把最旧一段裁掉一次再重试一次
# ---------------------------------------------------------------------------


def drop_oldest_piece(messages: list[dict]) -> list[dict]:
    """裁掉对话里「最老的一段」（system gravity）：

    按 pick_cut_point 的简化版：直接去掉 system 之后第一个组（组不可拆）。
    如果 system 后面什么都没有，返回原样。
    """
    if not messages:
        return messages
    groups = _split_groups(messages)
    if len(groups) <= 1:
        return messages
    # 跳过第一个组（一般是 system）；从第二个组里再跳过 system（防御）→ 去掉下一个组
    idx = 0
    if groups and all(m.get("role") == "system" for m in groups[0]):
        idx = 1
    if idx >= len(groups):
        return messages
    kept_groups = groups[:idx] + groups[idx + 1:]
    return [m for g in kept_groups for m in g]


async def chat_with_retry_on_long_context(
    messages: list[dict],
    *,
    models: Any,
    role: str,
    agent: str | None = None,
    purpose: str = "",
    on_trim: Any = None,
    **chat_kwargs: Any,
) -> Any:
    """调一次 models.chat；撞上「上下文超长」类错误把最旧一段裁掉一次再重试一次。

    agent 传给 models.chat 的岗位 kind（比如专岗回合的 news）；没给就照 role 的旧映射。
    其他错误（包括非上下文类的 4xx）原样抛出。返回成功的结果。
    on_trim: 可选同步回调（messages -> None），裁剪发生时通知调用方（日志 / 时间线）。
    """
    try:
        return await models.chat(role, messages, agent=agent, purpose=purpose, **chat_kwargs)
    except ModelError as e:
        if not looks_like_context_length(e.message if hasattr(e, "message") else str(e)):
            raise
        trimmed = drop_oldest_piece(messages)
        if trimmed == messages or len(trimmed) < len(messages) == 0:
            raise
        try:
            if callable(on_trim):
                on_trim(trimmed)
        except Exception:
            logger.exception("on_trim 回调出错")
        return await models.chat(role, trimmed, agent=agent, purpose=purpose, **chat_kwargs)


# ---------------------------------------------------------------------------
# 大工具结果落盘
# ---------------------------------------------------------------------------


def spill_big_output(
    output: str,
    directory: Any,
    *,
    limit_chars: int = SPILL_CHARS,
    head: int = SPILL_HEAD_CHARS,
    tail: int = SPILL_TAIL_CHARS,
    max_files: int = 60,
) -> str:
    """单个工具结果太长时写进 directory 下的一个文件，对话里放「头 + 尾 + 路径说明」。

    - 不足 limit_chars：原样返回；
    - directory 建不出来 / 写不进去：退回头尾截断（不落盘，不抛错）；
    - 文件名：spill-<时间戳>-<内容摘要前8位>.txt；目录里文件数超过 max_files 时删最旧的。
    """
    text = str(output or "")
    if len(text) < limit_chars:
        return text
    dir_path: Path | None = None
    try:
        dir_path = Path(directory)
        dir_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("建大结果落盘目录失败，退回头尾截断")
        return _head_tail(text, head, tail)

    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]
    import itertools as _it
    import time as _time

    base = f"spill-{int(_time.time() * 1000)}-{digest}"
    name = f"{base}.txt"
    for i in _it.count(1):
        if not (dir_path / name).exists():
            break
        name = f"{base}-{i}.txt"
    try:
        (dir_path / name).write_text(text, encoding="utf-8")
    except Exception:
        logger.exception("写大结果落盘文件失败")
        return _head_tail(text, head, tail)
    _prune_spill_dir(dir_path, max_files)
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n……（输出太长：中间省略 {omitted} 字，完整内容已存到 {dir_path / name}）……\n\n"
        + text[-tail:]
        + f"\n\n（完整输出在：{dir_path / name}）"
    )


def _prune_spill_dir(dir_path: Path, max_files: int) -> None:
    """目录里只留最新 max_files 个 spill-*.txt。"""
    try:
        files = sorted(dir_path.glob("spill-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        logger.exception("清大结果落盘目录失败")


# ---------------------------------------------------------------------------
# 重复调用提醒（只提醒，不拦截）
# ---------------------------------------------------------------------------


class RepeatCallNudger:
    """同一工具 + 规范化参数连续用第 3 / 5 / 8 次时给一句提醒；新的 user 消息清零。

    规范化：参数 JSON 按 key 排序、去掉空白差异；同一个 (工具, 规范化参数) 才算重复。
    """

    POINTS = (3, 5, 8)

    def __init__(self) -> None:
        self._last_key: str = ""
        self._count = 0

    @staticmethod
    def _norm_args(args: Any) -> str:
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                return args.strip()
        else:
            parsed = args
        try:
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(parsed)

    def note_user_message(self) -> None:
        """新的 user 消息进来：重复计数清零。"""
        self._last_key = ""
        self._count = 0

    def nudge(self, tool_name: str, args: Any) -> str:
        """这步调用要不要提醒；需要时返回一句中文，否则空串。"""
        key = f"{tool_name}|{self._norm_args(args)}"
        if key == self._last_key:
            self._count += 1
        else:
            self._last_key = key
            self._count = 1
        if self._count in self.POINTS:
            return (
                f"提醒：你已经连续第 {self._count} 次用「{tool_name}」拿同样的参数，"
                "基本上会得到同样的结果。如果没有新信息，建议换个角度，或者直接用 "
                "submit_result 把已经找到的交回（部分结果也行）。"
            )
        return ""
