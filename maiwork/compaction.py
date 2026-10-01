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
   摘要输入按重要性分配预算（A07）：user / assistant 决定性文字优先原样保留，
   超长 tool 结果各自截头尾（spill 路径说明保留），不再整体 head/tail 一刀切；
   优先内容本身仍超预算时切块分段摘要（≤4 块，工具调用与结果不拆散），
   再把块摘要合并成最终 8 节。R05 起分段在丢内容之前发生：单条超长的
   user/assistant 会被切成「消息内连续片段」进不同的块（带「第 k/n 段」接续
   标记），中段约束不再被预截丢掉；每次完整 prompt（含模板、[role] 和接续
   标记）都不超过 SERIALIZE_MAX_CHARS。装不进 4 块时在调用前抛 ModelError；
   分段/最终摘要为空或合并输入超预算时，也明确拒绝替换原对话
   （summarize_messages 抛、maybe_compact 吞掉后原 history 一条不动），
   不把 head/tail 截过的缺片输入发给模型再宣称摘要成功。
   摘要调模型照常用量记账（purpose 追加 ":compact"）；摘要失败：原样返回，不抛。

另外三件事也在这里：
- 超大工具结果落盘（spill_big_output）：单条工具输出超过约 5 万字时，完整内容写到
  该任务/对话归属目录下的一个文件，对话里只放「开头 + 结尾 + 路径说明」。
- 「上下文超长」类错误（looks_like_context_length）：chat_with_retry_on_long_context
  保留旧调用签名，但窗口不足时明确报错，不删掉最旧要求来伪装成功。
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

# ---------------------------------------------------------------------------
# R05 改动总览（docs/14-0.7.1整改复核.md R05；2026-10-01 收紧为硬失败语义）
#
# 1. 分段在丢内容之前发生：_serialize_cut 不再对超长 user/assistant 做 head/tail
#    预截；序列化只动超长 tool 结果（有 spill 完整归档可回读），user/assistant
#    全文保留。超预算由分段摘要按「消息内连续片段」切块兜底，48k/52k 单条消息
#    的中段约束随所在块原样进摘要输入。
# 2. 预算硬保障：任何一次发给摘要模型的「对话：」正文 ≤ SERIALIZE_MAX_CHARS
#    （含 [role] 前缀和分段接续标记）。装不进 MAX_SUMMARY_CHUNKS 块（承载量
#    ≈ 4 × 32k 字符）= 摘要做不了——**在任何模型调用之前抛 ModelError**，
#    不靠端点 context_length 报错，更不把 head/tail 截过的缺片输入发给模型
#    再宣称摘要成功。合并输入超预算同样抛 ModelError，不悄悄裁掉某段摘要。
# 3. 失败语义：summarize_messages 抛 ModelError；maybe_compact 吞掉后**原样
#    返回完整 history**（不替成缺料的假摘要），admin_chat 手动整理给 400。
#    超承载量时用户看到的是「整理没成功」，不是一份丢了约束的假摘要。
# ---------------------------------------------------------------------------

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

SERIALIZE_MAX_CHARS = 32000        # 摘要输入总预算（字符）
MAX_SUMMARY_CHUNKS = 4             # 分段摘要的块数上限（块摘要 + 1 次合并）
_TOOL_TRUNC_CHARS = 6000           # 序列化时单条 tool 结果超过它就先各自截头尾
_TOOL_TRUNC_HEAD = 3600
_TOOL_TRUNC_TAIL = 1600
_FIRST_PARA_MAX = 8000             # _truncate_message_text 保首段的硬上限（防御，不许突破预算）


_SUMMARY_RULES = (
    "特别要求：用户 / 管理员给出的约束、禁止事项（比如「不要发布」「不许碰线上」）、\n"
    "批准范围、还没做完的事项，必须**原样逐条列出**，放在 Primary Request and Intent 或\n"
    "Pending Jobs 那一节；这些是继续工作的红线，一条都不能丢、不能改写口气。\n"
)


def _summary_prompt(body: str, *, part: int = 0, parts: int = 0) -> str:
    """摘要提示。part/parts > 0 时表示这是分段摘要的第 part 块（共 parts 块）。"""
    chunk_note = ""
    if parts > 1:
        chunk_note = (
            f"注意：对话太长，这是第 {part}/{parts} 段。只总结这一段的内容；\n"
            "约束、禁止事项、批准范围照样原样列出（后面会有人把各段摘要合并）。\n"
        )
    return (
        "把下面这段模型和工具的对话压成一份「站用摘要」，之后新看这段摘要的人 / 模型\n"
        "要能完全接续原来的工作。用中文写，只写事实，怎么做的就怎么写，别评价。\n"
        + _SUMMARY_RULES
        + "必须写齐这 8 个小节，每个小节开头写上这一节的标题（照抄下面的标题，可只写中文）：\n"
        + sections_prompt()
        + "\n\n写得详细一些，每节 1–5 句；没有内容的小节写「无」。\n\n"
        + chunk_note
        + "对话：\n" + body
    )


def _merge_prompt(chunk_summaries: list[str]) -> str:
    """把各段摘要合并成最终一份 8 节摘要的提示。"""
    joined = "\n\n".join(
        f"【第 {i + 1} 段摘要】\n{s}" for i, s in enumerate(chunk_summaries)
    )
    return (
        "下面是一段长对话分几段做出的摘要。把它们合并成一份「站用摘要」，之后新看这份\n"
        "摘要的人 / 模型要能完全接续原来的工作。用中文写，只写事实，别评价；重复的合并，\n"
        "冲突的以最新的为准。\n"
        + _SUMMARY_RULES
        + "必须写齐这 8 个小节，每个小节开头写上这一节的标题（照抄下面的标题，可只写中文）：\n"
        + sections_prompt()
        + "\n\n写得详细一些，每节 1–5 句；没有内容的小节写「无」。\n\n"
        + "各段摘要：\n" + joined
    )


def _body_text(msgs: list[dict]) -> str:
    """一组消息（可含消息内片段）序列化后的正文（和摘要调用实际发出去的一致）。"""
    return _serialize_cut(msgs)


def _chunk_groups(groups: list[list[dict]], *, max_chars: int, max_chunks: int) -> list[list[dict]]:
    """把 _split_groups 的组按「每块序列化后 ≤ max_chars」切块。

    - 工具组（assistant(tool_calls) + tool 结果）不可拆，整块进同一块；
    - 普通单条 user / assistant 超长按 _split_message_parts 切成「消息内连续片段」，
      片段可以分进不同的块——分段在丢内容之前发生，中段约束不再被预截丢掉；
    - 返回空列表 = 装不进 max_chunks 块（超出承载量），调用方必须在调模型之前
      抛 ModelError，不许拿截断过的缺片输入去摘要。
    """
    if not groups:
        return []

    chunks: list[list[dict]] = []
    current: list[dict] = []

    def _flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = []

    for g in groups:
        atomic = any(m.get("tool_calls") or m.get("role") == "tool" for m in g)
        if atomic:
            # 工具组不拆：整块进同一块（超长 tool 结果序列化时已各自截头尾）
            if current and len(_body_text(current + g)) > max_chars:
                _flush()
            current.extend(g)
            continue
        # 普通消息：按消息内片段切，片段可分进不同的块
        for part in _fit_parts(g, max_chars=max_chars):
            if current and len(_body_text(current + [part])) > max_chars:
                _flush()
            current.append(part)
    _flush()

    if len(chunks) > max_chunks:
        return []  # 超出承载量：调用方明确失败，不静默丢
    return chunks


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


async def _chat_once(
    prompt: str,
    *,
    models: Any,
    role: str,
    agent: str | None,
    purpose: str,
    group_id: str,
    task_id: str,
) -> str:
    """摘要专用的一次 models.chat（purpose 记账追加 ":compact"）。"""
    result = await models.chat(
        role,
        [{"role": "user", "content": prompt}],
        agent=agent,
        purpose=f"{purpose or 'chat'}:compact" if not str(purpose or "").endswith(":compact") else str(purpose),
        group_id=str(group_id or ""),
        task_id=str(task_id or ""),
    )
    text = str(result.text or "").strip()
    if not text or getattr(result, "tool_calls", None):
        raise ModelError("摘要未完成：模型回了空摘要或仍要调用工具，原对话未替换", status=0)
    return text


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

    优先内容本身仍超预算时：按 _split_groups 的成组规则把 cut 切成若干块
    （≤ MAX_SUMMARY_CHUNKS，工具调用与结果不拆散；单条超长 user/assistant
    切成消息内连续片段，分段在丢内容之前发生），逐块摘要后再合并成最终 8 节摘要。
    预算按**实际 prompt 全长**算（含摘要模板，模板开销用 len(_summary_prompt(""))
    动态实测，不用估算）：每一次发给模型的 user content 都 ≤ SERIALIZE_MAX_CHARS。
    装不进 4 块（超出承载量）或合并输入超预算：**在调模型之前**
    抛 ModelError——不把 head/tail 截过的缺片输入发给模型再宣称摘要成功；
    maybe_compact 吞掉后原 history 一条不动。
    """
    # 单发预算 = 总预算 − 实际模板开销（动态实测）
    single_budget = SERIALIZE_MAX_CHARS - len(_summary_prompt(""))
    body = _serialize_cut(piece)
    if len(body) <= single_budget:
        return await _chat_once(
            _summary_prompt(body), models=models, role=role, agent=agent,
            purpose=purpose, group_id=group_id, task_id=task_id,
        )

    # 分段摘要：工具组不拆；普通消息按消息内片段切。
    # 块预算 = 总预算 − 分段模板的实际开销（part/parts 只占个位数，长度按上限实测）。
    # 装不进 MAX_SUMMARY_CHUNKS 块 = 超出承载量：在调模型之前抛 ModelError，
    # 不许拿 head/tail 截过的缺片输入去摘要再宣称成功（R05 收紧）。
    chunk_budget = SERIALIZE_MAX_CHARS - len(
        _summary_prompt("", part=MAX_SUMMARY_CHUNKS, parts=MAX_SUMMARY_CHUNKS)
    )
    groups = _split_groups([m for m in piece if isinstance(m, dict)])
    chunks = _chunk_groups(groups, max_chars=chunk_budget, max_chunks=MAX_SUMMARY_CHUNKS)
    if not chunks:
        raise ModelError(
            f"待摘要内容超出承载量（{MAX_SUMMARY_CHUNKS} 块 × 每块预算 {chunk_budget} 字），"
            "摘要没做成，原对话一条没动",
            status=0,
        )
    chunk_summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        chunk_body = _serialize_cut(chunk)
        prompt = _summary_prompt(chunk_body, part=i + 1, parts=len(chunks))
        if len(prompt) > SERIALIZE_MAX_CHARS:  # 防御：预算算法失灵也不许发超预算 prompt
            raise ModelError("分段摘要输入超预算，摘要没做成，原对话一条没动", status=0)
        text = await _chat_once(
            prompt, models=models, role=role, agent=agent,
            purpose=purpose, group_id=group_id, task_id=task_id,
        )
        chunk_summaries.append(text)
    if len(chunk_summaries) == 1:
        return chunk_summaries[0]
    # 合并成最终 8 节摘要；合并输入按实际长度 ≤ 预算，超了明确失败，不悄悄裁某段摘要
    merge_prompt = _merge_prompt(chunk_summaries)
    if len(merge_prompt) > SERIALIZE_MAX_CHARS:
        raise ModelError(
            "各段摘要拼起来超出合并输入预算，摘要没做成，原对话一条没动",
            status=0,
        )
    return await _chat_once(
        merge_prompt, models=models, role=role, agent=agent,
        purpose=purpose, group_id=group_id, task_id=task_id,
    )


def _serialize_one(m: dict) -> str:
    """一条消息摊成一行「[role] 内容」（assistant 带工具调用名；消息内片段带接续标记）。"""
    role = str(m.get("role") or "?")
    content = str(m.get("content") or "")
    if m.get("tool_calls"):
        names = []
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                names.append(str((tc.get("function") or {}).get("name") or ""))
        content = content + f"（调用工具：{'、'.join(x for x in names if x)}）"
    part, part_of = m.get("part"), m.get("part_of")
    if part and part_of and int(part_of) > 1:
        content = f"（第 {int(part)}/{int(part_of)} 段，接上一段）" + content
    return f"[{role}] {content}"


def _truncate_message_text(text: str, *, head: int, tail: int, keep_first_paragraph: bool) -> str:
    """单段文本头尾截断，中间明确写「此处省略 N 字」。

    现在只用于超长 tool 结果（user/assistant 一律不截——分段在丢内容之前发生，
    装不下就抛 ModelError）。keep_first_paragraph 是保留的防御口：保第一段也有
    硬上限 _FIRST_PARA_MAX，绝不允许「保首段」把输出顶到预算之外。
    """
    if len(text) <= head + tail:
        return text
    real_head = head
    if keep_first_paragraph:
        first_para_end = text.find("\n")
        if 0 < first_para_end < len(text) - tail:
            real_head = max(head, min(first_para_end, _FIRST_PARA_MAX))
    real_head = min(real_head, len(text) - tail)
    omitted = len(text) - real_head - tail
    return (
        text[:real_head]
        + f"\n\n……（此处省略 {omitted} 字）……\n\n"
        + text[-tail:]
    )


def _truncate_tool_for_serialize(text: str) -> str:
    """超长 tool 结果各自截头尾；spill 落盘的路径说明行必须留住。"""
    if len(text) <= _TOOL_TRUNC_CHARS:
        return text
    out = _truncate_message_text(text, head=_TOOL_TRUNC_HEAD, tail=_TOOL_TRUNC_TAIL,
                                 keep_first_paragraph=False)
    # spill 落盘的路径说明（「（完整输出在：…）」）可能不在头尾窗口里：找出来补上
    for line in text.splitlines():
        s = line.strip()
        if ("完整输出在" in s or "完整内容已存到" in s) and s not in out:
            out += f"\n{s}"
    return out


def _serialize_cut(cut: list[dict], *, max_chars: int = SERIALIZE_MAX_CHARS) -> str:
    """把要被总结的那一段摊成纯文本给摘要模型看。

    按重要性分配预算（不再整体 head/tail 一刀切）：
    - user / assistant 决定性文字原样保留（R05：不再对它们做 head/tail 预截，
      分段在丢内容之前发生——超预算由分段摘要按消息内片段切块，见 _fit_parts；
      片段也装不进 4 块时 _summarize_piece 直接抛 ModelError，不在这里偷截）；
    - 超长的 tool 结果各自截头尾（spill 落盘的路径说明保留，完整内容可回读）。
    """
    msgs = [m for m in cut if isinstance(m, dict)]
    text = "\n".join(_serialize_one(m) for m in msgs).strip()
    if len(text) <= max_chars:
        return text

    # 只动超长 tool 结果（低优先内容，有 spill 归档）；user/assistant 不动
    lines = []
    for m in msgs:
        if m.get("role") == "tool" and not m.get("tool_calls"):
            body = _truncate_tool_for_serialize(str(m.get("content") or ""))
            lines.append(f"[tool] {body}")
        else:
            lines.append(_serialize_one(m))
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# R05：消息内分段——单条超长 user/assistant 切成「连续片段」，分段在丢内容之前
# ---------------------------------------------------------------------------


def _split_message_parts(m: dict, *, max_chars: int) -> list[dict]:
    """一条消息按序列化长度切成若干「片段消息」（content 连续切片，不丢字）。

    - 片段是 content 的连续切片，拼起来就是原文；只有长度原因才切；
    - 切了多段的片段带 part/part_of（「第 k/n 段」），序列化时写成接续标记；
    - tool 消息不切（tool 结果序列化时已各自截头尾，且有 spill 完整归档）；
    - max_chars 是整条序列化行的上限（含 [role] 前缀和接续标记），调用方给
      预算时要把这两样算进去。
    """
    line = _serialize_one(m)
    if len(line) <= max_chars:
        return [m]
    if m.get("role") == "tool":
        return [m]
    prefix = f"[{m.get('role') or '?'}] "
    suffix = ""
    if m.get("tool_calls"):
        names = [str((tc.get("function") or {}).get("name") or "")
                 for tc in m.get("tool_calls") or () if isinstance(tc, dict)]
        suffix = f"（调用工具：{'、'.join(x for x in names if x)}）"
    content = str(m.get("content") or "")
    # 每段正文预算：行上限 − 前缀 − 工具调用名后缀 − 接续标记（≈ 22 字）
    per = max(200, max_chars - len(prefix) - len(suffix) - 32)
    slices = [content[i:i + per] for i in range(0, len(content), per)] or [""]
    out: list[dict] = []
    n = len(slices)
    for k, piece_text in enumerate(slices):
        part = dict(m)
        part["content"] = piece_text
        if k != n - 1:
            part.pop("tool_calls", None)  # 工具调用名只挂在最后一段（_serialize_one 拼）
        if n > 1:
            part["part"] = k + 1
            part["part_of"] = n
        out.append(part)
    return out


def _fit_parts(msgs: list[dict], *, max_chars: int) -> list[dict]:
    """把一组消息逐条切成片段（_split_message_parts），使整组序列化 ≤ max_chars。

    返回的片段列表顺序不变、内容不丢（tool 除外：序列化层已截头尾）。
    整组仍超预算时由调用方继续按片段切分块。
    """
    out: list[dict] = []
    for m in msgs:
        if isinstance(m, dict):
            out.extend(_split_message_parts(m, max_chars=max_chars))
    return out


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
# 「上下文超长」错误：明确报错，不绕行丢弃任务约束
# ---------------------------------------------------------------------------


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
    """保留旧调用口，超长时安全失败，不静默删除要求/批准范围后再试。

    无损摘要由 maybe_compact 提前执行；摘要不能承载或端点窗口仍不足时，
    不允许退回整组丢弃历史的旧路径。on_trim 保留为调用兼容参数，但不再调用。
    非上下文错误原样抛出；原 messages 不变，不发第二次被删改内容的请求。
    """
    try:
        return await models.chat(role, messages, agent=agent, purpose=purpose, **chat_kwargs)
    except ModelError as e:
        if not looks_like_context_length(e.message):
            raise
        raise ModelError(
            "这个模型的上下文放不下当前对话，本次未丢弃任务要求或批准范围。"
            "请先整理对话、把大段材料拆开，或修正模型的实际上下文设置后再试。"
            f"（端点原因：{e.message}）",
            status=e.status,
        ) from e


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
