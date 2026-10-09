"""子 agent 执行器（M2，docs/07-代码接口.md §10.3、需求 R9）。

主模型不亲自干长活：派给子 agent，子 agent 用 models.chat(agent=<岗位>, …) 自己
多轮循环，只能用名单里的工具（外加 submit_result），**只能通过 submit_result
交回**（summary、data、evidence）——不能自己宣布完成，由调用方（主模型 / feeds）验收。

0.4.0 起：
- **没有步数上限**：以调用 submit_result 结束；只说话不调工具催两次（_MAX_NUDGES），
  还不调就判失败。max_steps 参数保留兼容（None / 0 = 不限；调用方不再传 12 / 16）。
- 上下文压缩（compaction.py，共用）：估算超触发线先截旧 tool 结果（不调模型），
  仍超再调模型把最老一段总结成 8 节摘要；system 永不压缩、tool/assistant 成组切分；
  摘要失败原样继续；模型回「上下文超长」裁最旧一段重试一次。
- 大工具结果落盘归档（docs/27 §7 / §8 P1）：**任何会被单条 tool 消息上限（6000 字）截断**
  的工具输出（成功、报错都算）都把完整正文写到 `<workspace>/tool_spill/<任务ID>/`，对话里
  留头 + 尾 + **工作区相对路径**指针（用 `read_file(path=…, offset=, limit=)` 逐页读回）；
  **worker 这边不再做任何 6000 字硬截断**：归档成功给指针、归档失败 / 没有工作区都原样
  保留完整正文，装不下交给预算闸明确失败（明确失败 > 悄悄丢中段）。
- 原始历史 vs 工作视图：`history` 是 lane 的工作视图（会被压缩改写）；`raw_history` 是这一轮
  的**原始对话**（只增不改，压缩切掉的那段和压缩前的旧工具正文原样留着），交给调用方落库。
- 重复调用提醒：同一工具 + 规范化参数连用第 3 / 5 / 8 次往对话里加一句提醒
  （只提醒，不拦截）；有新的 user 消息进来计数清零。
- 模型调用 ModelError：ok=False，error 带原因。
- 模型岗位（1b）：run(agent=<kind>) 决定 models.chat 的候选链；默认 = agent_type
  （specialists 派来的回合自动用所在岗位的模型选择：model/effort/backup）。
- 任务安全网（app.schedule_task_net / Coordinator 侧，不是这个文件）：暂停时
  workers 下一步开头看到 paused 就停手返回。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import clock, compaction, lanes
from .models import ModelError
from .tools import ToolContext, Tools

logger = logging.getLogger("maiwork.workers")

_TOOL_MSG_MAX = 6000    # 回给模型的单条 tool 消息截 6000 字（spill 之后一般远小于它）
_MAX_NUDGES = 2         # 最多催几次「请调用 submit_result 交回」
# 分页文件工具（tools_exec 的 read_file / inspect_file）：它们的页大小是照
# _TOOL_MSG_MAX 定的，正常永远碰不到下面的截断。真碰到了说明页比预算大（改坏了），
# 那时不能只说一句「已截断」让模型以为本页读完了——要明确让它重读本页同一段。
_FILE_TOOLS = ("read_file", "inspect_file")

# ---------------------------------------------------------------------------
# 工具输出归档（docs/27 §8 P1：6001~49999 字以前既被截断又没有归档）
#
# 判定只有一条：**这条 tool 消息会不会被单条消息上限（_TOOL_MSG_MAX）截断**。会，就必须有
# 完整归档（成功、报错都算）。阈值取 _TOOL_MSG_MAX + 1 = 6001（父 agent 定的口径：不留
# 「被截断但没归档」的段）。写归档**不走 compaction.spill_big_output**——它用普通 Path 写，
# 会顺着工作区里预埋的符号链接把正文写到工作区外面；改走
# `environments.local.write_new_file_verified`（逐段 O_NOFOLLOW + O_CREAT|O_EXCL + 写完读回来核对）。
# 不新增任何配置字段。
# ---------------------------------------------------------------------------

_ARCHIVE_LINE = _TOOL_MSG_MAX + 1      # 6001：够到它就一定会被截断 → 必须归档
_ARCHIVE_HEAD = 3600                   # 归档后对话里留的开头
_ARCHIVE_TAIL = 1000                   # 归档后对话里留的结尾
_ARCHIVE_PAGE = 5000                   # 回读指引里给的一页字数（tools_exec 的页上限）
_ARCHIVE_FAIL_NOTE = (
    "注意：刚才 {name} 的输出（{chars} 字）太长、没能归档到工作区，完整正文就在上面那条"
    "工具结果里、一个字没删；没有可以按路径回读的文件。"
    "上下文装不下时这一步会明确报错，不会悄悄把中段丢掉。"
)


@dataclass
class _SpillZone:
    """这一轮子 agent 的工具输出归档地：工作区内、按任务分目录。"""

    base: Path            # 工作区根（可信锚点；安全写从它开始逐段走）
    rel_dir: str          # 工作区相对目录（给模型看的那种形式）

    @property
    def directory(self) -> Path:
        return self.base / self.rel_dir


def _spill_zone(workspace: Any, task_id: str) -> _SpillZone | None:
    """`<workspace>/tool_spill/<任务ID>/` 和它的工作区相对路径；缺一个就不归档。"""
    if workspace is None or not task_id:
        return None
    try:
        base = Path(workspace)
        rel = str((base / "tool_spill" / str(task_id)).relative_to(base)).replace("\\", "/")
    except Exception:
        logger.exception("算工具输出归档目录出错，这一轮不归档")
        return None
    if not rel or rel.startswith(".."):
        return None
    return _SpillZone(base=base, rel_dir=rel)


def _pointer_line(rel: str, total: int) -> str:
    """回读指针行（归档后回给模型 / compaction 剪枝时贴在正文尾部的那段）。"""
    return (
        f"【完整输出在工作区文件】{rel}（共 {total} 字）\n"
        f"回读：read_file(path=\"{rel}\", offset=0, limit={_ARCHIVE_PAGE})，"
        f"照返回里的「下一页 offset」一页页往后读（offset 从 0 数、单位字符，"
        f"一页最多 {_ARCHIVE_PAGE} 字）。路径要用工作区内的相对路径（绝对路径读不了）；"
        "别以为你只看到了这一段。"
    )


def _archive_pointer(text: str, rel: str, *, file_tool: bool) -> str:
    """归档成功时回给模型的那段：头 + 尾 + 工作区相对路径 + 分页回读指引。"""
    total = len(text)
    omitted = max(0, total - _ARCHIVE_HEAD - _ARCHIVE_TAIL)
    foot = "\n\n" + _pointer_line(rel, total)
    if file_tool:
        foot += "\n另外：这页正文本身就超了单条消息预算——别往后翻，请用更小的 limit 重读同一个 offset。"
    return (
        text[:_ARCHIVE_HEAD]
        + f"\n\n……（输出太长：中间省略 {omitted} 字；完整 {total} 字已归档到工作区文件 {rel}）……\n\n"
        + text[-_ARCHIVE_TAIL:]
        + foot
    )


def _write_archive(text: str, zone: _SpillZone) -> str:
    """把完整正文**安全地**落盘归档；成功回工作区相对路径，失败回 ""。

    - 写走 `environments.local.write_new_file_verified`：从工作区根开始逐段 `O_NOFOLLOW`
      打开（子 agent 能预埋符号链接 / 硬链接，普通 `Path.write_text` 会把正文写到工作区外），
      新文件用 `O_CREAT|O_EXCL|O_NOFOLLOW` 建，写完**从同一个 dir_fd 读回来逐字核对**；
    - 所以「指针能不能读回」不靠猜：核对不过 / 写不进去都算没归档，调用方原样保留完整正文；
    - 文件名 `spill-<毫秒>-<内容摘要8>-<随机8>.txt`：随机段保证不撞名，摘要只给人和检索看；
    - 这里**不清理**旧归档：还在跑的任务可能正拿着旧指针回读（目录按任务分，随工作区走）。
    """
    text = str(text or "")
    if not text:
        return ""
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]
    name = f"spill-{int(clock.now() * 1000)}-{digest}-{secrets.token_hex(4)}.txt"
    try:
        from .environments.local import write_new_file_verified

        written = write_new_file_verified(
            zone.base, zone.rel_dir, name, text.encode("utf-8")
        )
    except Exception:
        logger.warning("工具输出归档写失败（%d 字）：按没归档处理", len(text), exc_info=True)
        return ""
    if not written:
        logger.warning("工具输出归档读回来对不上（%d 字）：按没归档处理", len(text))
        return ""
    return f"{zone.rel_dir}/{name}"


def _archive_tool_output(text: str, zone: _SpillZone, *, file_tool: bool = False) -> tuple[str, str]:
    """把会超预算的工具正文落盘归档，返回 (回给模型的文字, 归档文件的工作区相对路径)。

    成功 → 头 + 尾 + 工作区相对指针 + 回读指引；
    失败（没目录 / 写不进去 / 盘上核对不到）→ **原样返回完整正文** + 空指针。调用方据此另加
    一条「没归档」提示，并且**不许再硬截**：中段宁可让预算闸明确失败，也不许悄悄丢。
    """
    text = str(text or "")
    rel = _write_archive(text, zone)
    if not rel:
        return text, ""
    return _archive_pointer(text, rel, file_tool=file_tool), rel


def _archive_message(message: dict, zone: _SpillZone) -> str:
    """compaction 剪枝用的归档回调：把这条 tool 正文落盘，**只回指针行**。

    compaction 自己会做「头 + 尾」裁剪，所以这里只回指针（回整段指针文本会把头尾塞两遍）。
    归档失败回 ""：那条就不剪（`require_recoverable=True`），宁可让预算闸明确失败。
    """
    text = str(message.get("content") or "")
    rel = _write_archive(text, zone)
    if not rel:
        return ""
    return _pointer_line(rel, len(text))


def _archive_hook(zone: _SpillZone | None):
    """给 compaction 的 archive 回调；没有可用归档目录 → None（那条都不剪）。"""
    if zone is None:
        return None
    return lambda message: _archive_message(message, zone)


def _log_compaction(actor: str, outcome: Any) -> None:
    """压缩结果记一笔（docs/27 §8 P2 的观测）：quiet 时走 debug，动过就 info。"""
    if outcome is None:
        return
    try:
        obs = outcome.observations or {}
        inp = obs.get("input") or {}
        prune = obs.get("prune") or {}
        line = (
            "子 agent（%s）上下文：action=%s window=%s reserve=%s threshold=%s pruned=%s failure=%s"
        )
        args = (
            actor, getattr(outcome, "action", ""), inp.get("context_window"),
            inp.get("output_reserve"), inp.get("threshold"), prune.get("count"),
            (obs.get("failure") or {}).get("kind") or "",
        )
        if str(getattr(outcome, "action", "none")) == "none":
            logger.debug(line, *args)
        else:
            logger.info(line, *args)
    except Exception:
        logger.debug("记压缩观测出错", exc_info=True)


def _has_dangling_tool_calls(messages: list[dict]) -> bool:
    """assistant(tool_calls) 有没有没配上 tool 回复的（严格端点少一条就 400）。"""
    answered = {
        str(m.get("tool_call_id") or "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "tool"
    }
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or ():
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or "")
            if cid and cid not in answered:
                return True
    return False


def _repair_tool_pairing(messages: list[dict], actor: str) -> None:
    """压缩之后把弄丢的工具回复补回来（原地改，只补不删；配对好好的就一个字不动）。

    配对保不保是压缩那一层的事，但不该由子 agent 赌：严格端点少一条 tool 回复直接 400，
    整个子 agent 一步都走不下去（线上踩过）。
    """
    try:
        if not _has_dangling_tool_calls(messages):
            return
        messages[:] = lanes.close_dangling_tool_calls(messages)
        logger.warning("压缩后的对话有没配对的工具调用（%s），已补回占位回复", actor)
    except Exception:
        logger.exception("补工具配对失败（%s），原样继续", actor)

# ---------------------------------------------------------------------------
# docs/22 §4 F（2026-10-07 本地）止损：抓取失败按主机计 + 连续没进展
# ---------------------------------------------------------------------------

FETCH_HOST_FAIL_LIMIT = 2      # 同一主机连续失败满这么多次 → 再请求直接短路
NO_PROGRESS_NUDGE_AT = 15      # 连续这么多次搜索/抓取没有新打开的地址 → 插一条 user 提醒
NO_PROGRESS_STOP_AT = 25       # 再连续到这么多次 → 和 deadline 一样只给 submit_result


def _host_fail_note() -> str:
    return (
        f"这个网站已经连续拒绝 {FETCH_HOST_FAIL_LIMIT} 次，别再试它；"
        "换别的来源，或在交回里如实写拿不到"
    )


def _no_progress_nudge() -> str:
    return (
        f"已经连续 {NO_PROGRESS_NUDGE_AT} 次没有新收获，停止继续搜，"
        "用现有资料完成并交回，拿不到的如实写"
    )


def _no_progress_stop() -> str:
    return (
        f"已经连续 {NO_PROGRESS_STOP_AT} 次没有新收获：请立刻用 submit_result "
        "把已经拿到的交回（拿不到的如实写），不要再搜、不要再抓。"
    )


def _tool_kind(name: Any) -> str:
    """这条调用算「搜索」还是「抓取」（止损只数这两类）；别的 → ""。"""
    n = str(name or "")
    try:
        from .tools_builtin import _is_extract_like_mcp

        if n == "fetch_page" or _is_extract_like_mcp(n):
            return "fetch"
    except Exception:
        if n == "fetch_page":
            return "fetch"
    try:
        from .search_binding import _EXTRACT_HINT, _SEARCH_HINT  # noqa: SLF001 — 同一份特征

        if n == "web_search":
            return "search"
        if n.startswith("mcp_") and _SEARCH_HINT.search(n) and not _EXTRACT_HINT.search(n):
            return "search"
    except Exception:
        if n == "web_search":
            return "search"
    return ""


def _hosts_of_args(name: Any, args: Any) -> list[str]:
    """这次抓取请求打向哪些主机（去重、小写）；拿不到 → 空。"""
    if _tool_kind(name) != "fetch" or not isinstance(args, dict):
        return []
    urls: list[str] = []
    for key in ("url", "link"):
        v = str(args.get(key) or "").strip()
        if v:
            urls.append(v)
    many = args.get("urls")
    if isinstance(many, list):
        urls.extend(str(u).strip() for u in many if str(u or "").strip())
    out: list[str] = []
    for raw in urls:
        host = _host_of_url(raw)
        if host and host not in out:
            out.append(host)
    return out


def _host_of_url(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return ""
    return str(host or "").lower()


def _opened_urls_of_call(name: Any, args: Any, output: Any) -> set[str]:
    """这次调用**真打开过**的（规范化）地址：fetch_page / 抓正文类 MCP 工具才有。"""
    if _tool_kind(name) != "fetch":
        return set()
    urls: list[str] = []
    if isinstance(args, dict):
        for key in ("url", "link"):
            v = str(args.get(key) or "").strip()
            if v:
                urls.append(v)
        many = args.get("urls")
        if isinstance(many, list):
            urls.extend(str(u).strip() for u in many if str(u or "").strip())
    try:
        from .tools_builtin import (
            _FINAL_URL_LINE_RE,
            _is_extract_like_mcp,
            final_url_from_summary,
        )
        from .coordinator import normalize_link_for_check

        text = str(output or "")
        if str(name) == "fetch_page":
            final = final_url_from_summary(text)
            if final:
                urls.append(final)
        elif _is_extract_like_mcp(str(name)):
            for m in _FINAL_URL_LINE_RE.finditer(text):
                urls.append(m.group(1))
    except Exception:
        return set()
    out: set[str] = set()
    for raw in urls:
        key = normalize_link_for_check(raw)
        if key:
            out.add(key)
    return out


class _StallGuard:
    """一条活的止损账本（docs/22 §4 F）：按主机记失败 + 连续没进展。

    - `gate` 装到 ToolContext.call_gate 上：同一主机失败满了就**短路**（不真发请求，
      但仍然照常落一条 tool_calls；文案让它换来源）。
    - `after` 每次工具调用后记一笔，返回要追加给模型的提醒（"" = 不用提醒）；
      到 `NO_PROGRESS_STOP_AT` 时置 `stop_requested`，由 worker 循环收紧成只给 submit_result。
    """

    def __init__(self) -> None:
        self.host_fails: dict[str, int] = {}
        self.seen_urls: set[str] = set()
        self.no_progress = 0
        self.stop_requested = False

    def gate(self, name: Any, args: Any) -> Any:
        from .tools import ToolResult

        for host in _hosts_of_args(name, args):
            if self.host_fails.get(host, 0) >= FETCH_HOST_FAIL_LIMIT:
                return ToolResult(ok=False, output="", error=_host_fail_note())
        return None

    def after(self, name: Any, args: Any, result: Any) -> str:
        kind = _tool_kind(name)
        if not kind:
            return ""
        if kind == "fetch" and bool(getattr(result, "ok", False)):
            # 「连续」拒绝：这个网站打开成功一次，它的失败计数就清零
            for host in _hosts_of_args(name, args):
                self.host_fails.pop(host, None)
            fresh = _opened_urls_of_call(name, args, getattr(result, "output", ""))
            new = fresh - self.seen_urls
            if new:
                self.seen_urls |= fresh
                self.no_progress = 0
                return ""
        elif kind == "fetch":
            for host in _hosts_of_args(name, args):
                self.host_fails[host] = self.host_fails.get(host, 0) + 1
        self.no_progress += 1
        if self.no_progress >= NO_PROGRESS_STOP_AT:
            self.stop_requested = True
            logger.info("子 agent 连续 %d 次搜索/抓取没有新收获，止损收紧到只交回", self.no_progress)
            return ""
        if self.no_progress == NO_PROGRESS_NUDGE_AT:
            logger.info("子 agent 连续 %d 次搜索/抓取没有新收获，插一条提醒", self.no_progress)
            return _no_progress_nudge()
        return ""


# 调研类子任务的报告框架（docs/02 §7.2 真实验收的配套）：主模型在计划里把子任务标成
# research 时，coordinator 把这段作为 system 提示的追加段传进来（system_extra）。
# 只给调研/对比/盘点这类要出结论的活；做东西（build）不加。文案给子 agent 看，不写实现细节。
# 2026-10 线上 T-10 复核（用户口述）：六段只是**组织框架**，不是扩搜配额；「多源」按
# 独立来源算（转载不算独立佐证）；说法不一致 ≠ 事实矛盾；关联未知的机构不能写成
# 已证实由同一攻击者所为。
RESEARCH_REPORT_FRAMEWORK = (
    "这是一件调研类的活，交给群里的成果按这个框架组织，别写成一堆链接：\n"
    "1. 一句话结论；\n"
    "2. 大家都同意的；\n"
    "3. 有分歧的（把分歧摆出来，不要取平均）；\n"
    "4. 吐槽最多的；\n"
    "5. 新冒头的；\n"
    "6. 没人提的（值得注意的空白）。\n"
    "六段是组织框架，不是扩搜配额：按任务原话和手上已有的证据组织，"
    "关键问题有可靠依据、未确认的点已说明就停，不用为了凑满每一节再去搜。\n"
    "每一点都要带链接，并标明「多源」（至少 2 个独立采集或独立确认的来源；"
    "不同站点转载同一来源，不算独立佐证）还是「单源」。\n"
    "别把说法不一致直接当成事实矛盾：置信程度或时间点不同，不自动等于事实矛盾；"
    "对关联未知的机构，不写成已证实由同一攻击者所为。"
)

# 线上 T-11：成品里出现了 job1 / research.md / T-11 / 「上一步」「主模型」这些内部说法，
# 群友看得见。由 coordinator._enrich_brief 追加到**每条活**（调研 + 制作）的 brief 末尾。
AUDIENCE_NOTE = (
    "成品是给群友看的：不写 job 编号、不写 research.md / artifacts/ 这类文件路径、"
    "不写任务号、不写「上一步」「主模型」「子 agent」这类内部说法——"
    "要引用别的东西就说清是什么（比如「第 1 步查到的资料」「这次的调研记录」）。"
)


@dataclass
class WorkerReport:
    ok: bool
    summary: str
    data: Any = None
    evidence: list[str] = field(default_factory=list)
    steps: int = 0
    error: str = ""
    # 专岗（specialists.py）：这次回合在 Agents 里登记的交接单 id。
    # 默认 '' 向后兼容（Workers.run 直出的 report 没有交接单，老调用方不用动）。
    handoff_id: str = ""
    # 因为任务被取消 / 暂停 / 挂起而停手（不是自己干砸了）：交接单记 cancelled 不记 failed
    stopped: bool = False
    # 任务双岗协作第三步（docs/20 §四）：对说明本身的异议 {reason, evidence, suggestion}；没提 = None
    challenge: dict | None = None


def _system_prompt(
    actor: str,
    group_id: str,
    output_schema: dict | None,
    skills_hint: str = "",
    identity: Any = None,
    extra_system: str = "",
    agent: str = "",
) -> str:
    """子 agent 的 system 提示。专岗改版 3/4：agent 这一岗（kind）自己的 SOUL.md +
    AGENTS.md 注入（不再是旧的全局 AGENTS=main 那份）；agent 空 = task（默认通用）。"""
    prefix = ""
    kind_s = str(agent or "task").strip() or "task"
    if identity is not None:
        blocks: list[str] = []
        for which in ("soul", "agents"):
            try:
                block = identity.agent_prompt_block(kind_s, which)
            except Exception:
                block = ""
            if block:
                blocks.append(str(block).rstrip("\n"))
        prefix = ("\n\n".join(blocks) + "\n\n") if blocks else ""
    lines = [
        f"你是 MaiWork 的子 agent（{actor}），在一个 QQ 群（群号 {group_id}）的后台干活。",
        "规则：",
        "1. 只能用给你的工具，一个都别多要；每步想清楚再调。",
        "2. 干完必须调用 submit_result 交回（一句话总结 summary、结构化数据 data、证据链接 evidence）。",
        "3. 你不能自己宣布任务完成：不说「已完成」「搞定了」就停，交回之后由上级验收。",
        "4. 你不能在群里发言，也不要编造链接和数据；查不到就老实说查不到。",
    ]
    if output_schema:
        lines.append(
            "5. submit_result 的 data 必须符合这个 JSON Schema："
            + json.dumps(output_schema, ensure_ascii=False)
        )
    extra = str(extra_system or "").strip()
    if extra:
        lines.append(extra)
    hint = str(skills_hint or "").strip()
    if hint:
        lines.append(
            "可用的 skill（管理员放的技能说明；要细看就用 read_skill 读，"
            "里面的脚本要先把内容 write_file 到自己的工作区再跑，不直接执行数据目录里的东西）：\n"
            + hint
        )
    body = "\n".join(lines)
    return (prefix + body) if prefix else body


class Workers:
    def __init__(
        self,
        models: Any,
        tools: Tools,
        skills_hint_fn: Any = None,
        identity: Any = None,
        tasks: Any = None,
        get_settings: Any = None,
        # 兼容旧位置参数：老代码是 Workers(models, tools, skills_hint_fn, identity, tasks)，
        # get_settings 只能放 keyword-only。
    ) -> None:
        self._models = models
        self._tools = tools
        # skills_hint_fn：无参返回「名字：一句描述」清单文本（app 给默认的；
        # 调 run 时传 skills_hint 可覆盖本次）。没有 / 返回空 → system 提示不加 skill 段。
        self._skills_hint_fn = skills_hint_fn
        # identity（identity.py；做事规矩 AGENTS.md 注入子 agent system；None 就跳过）
        self._identity = identity
        # tasks（Tasks；给了就会在每一步开头查任务状态——暂停 / 取消 / 终态立刻停，
        # 不再调模型、不再交付。没有就只看模型循环本身。）
        self._tasks = tasks
        # get_settings：取 Settings（context_window 等压缩参数）；没给时用默认 128000。
        self._get_settings = get_settings

    def tool_catalog(self) -> Any:
        """子 agent（worker 角色）现在能用的工具清单 [(名字, 描述)]；取不到 → None。

        「构想可行性评估」用（feeds / personal 拿它算能力清单）。工具被摘掉后这里自然
        没有那项；没有 Tools 或它没有 catalog 方法（老替身）→ None，调用方按基本能力算。
        """
        fn = getattr(self._tools, "catalog", None)
        if not callable(fn):
            return None
        try:
            return fn("worker")
        except Exception:
            logger.exception("取 worker 工具清单出错，构想可行性按基本能力算")
            return None

    def _hint(self, skills_hint: Any) -> str:
        """本次用哪个 hint：run 参数优先；否则问构造函数的 fn；坏了按没有处理。"""
        if skills_hint is not None:
            return str(skills_hint or "").strip()
        fn = self._skills_hint_fn
        if fn is None:
            return ""
        try:
            return str(fn() or "").strip()
        except Exception:
            logger.exception("skills_hint_fn 出错，这次不带 skill 清单")
            return ""

    def _context_window(self, kind: str = "task", *, escalate: bool = False) -> int:
        """上下文窗口（tokens）：2026-10 改版起认「所选模型」的窗口
        （models.limits_for(<岗位>)，岗位是 task / news / goal / c_xxx…，跟本轮 agent 走）；
        取不到回落旧全局值，再用默认 128000。"""
        try:
            fn = getattr(self._models, "limits_for", None)
            if callable(fn):
                if escalate:
                    lim = fn(str(kind or "task") or "task", escalate=True)
                else:
                    lim = fn(str(kind or "task") or "task")
                v = int((lim or {}).get("context_window") or 0)
                if v > 0:
                    return v
        except Exception:
            pass
        try:
            getter = self._get_settings
            if getter is None:
                return 128000
            settings = getter()
            return int(getattr(getattr(settings, "models", None), "context_window", None) or 128000)
        except Exception:
            return 128000

    def _task_status(self, task_id: str) -> str:
        """任务当前状态；没接 tasks 或查不到返回 ""。"""
        if self._tasks is None or not task_id:
            return ""
        try:
            row = self._tasks.get(task_id)
        except Exception:
            return ""
        if row is None:
            return ""
        return str(row.get("status") or "")

    async def run(
        self,
        brief: str,
        *,
        group_id: str,
        tools: list[str],
        task_id: str = "",
        actor: str = "子 agent #1",
        max_steps: int | None = 0,
        output_schema: dict | None = None,
        workspace: Any = None,
        skills_hint: Any = None,
        system_extra: str = "",
        deadline_ts: float | None = None,
        artifact_scope: tuple[str, ...] | None = None,
        # 各步骤分文件夹（docs/22 §4 C）：这条活的写范围（工作区相对目录）。
        # None = 不限制写（老调用方行为不变）。
        write_scope: tuple[str, ...] | None = None,
        # 专岗（specialists.py）注入：执行身份与本论硬权限。None 向后兼容。
        agent_type: str = "task",
        allowed_tools: tuple[str, ...] | list[str] | None = None,
        allowed_skills: tuple[str, ...] | list[str] | None = None,
        # 1b：模型侧的岗位 kind（默认等于 agent_type——专岗回合就用专岗自己的模型；
        # 只涉及执行身份、不涉及模型选型的调用方不用动）
        agent: str | None = None,
        used_tools: set[str] | None = None,
        # 任务双岗协作（docs/20 §6.2）：lane 的持久对话（不含 system）。给了就接着上次干，
        # 跑完原地换成这一轮结束时的对话；None = 老做法（每次全新对话，资讯/构想/目标用）。
        history: list[dict] | None = None,
        # 原始历史（docs/27 §9）：这一轮的完整原始对话（不含 system），**只增不改**——
        # 压缩切掉的那段、压缩前的旧工具正文都原样留着，tool 正文存的是**完整正文**
        # （不是「头 + 尾 + 指针」那份工作视图投影；归档指针是额外的，不替代它）。
        # 调用方给上一轮的 canonical 就传进来（从这里接着写，不从工作视图抄，免得把
        # 摘要行 / 指针投影当原始历史）跑完原地装进同一份，交给调用方落库
        # （TaskLanes.save(..., original_messages=…)）；不传就没这个口。
        raw_history: list[dict] | None = None,
        # 第 2 次没过换升级模型（docs/20 §5.3）：原样透传给 models.chat
        escalate: bool = False,
    ) -> WorkerReport:
        # 本轮硬权限工具名单：默认 = 请求 tools + submit_result（每轮都硬门）；
        # allowed_tools 给了再收窄成「请求 ∩ allowed_tools」（submit_result 保底）。
        # 这不是 spec 提示：模型捏造名单外工具 Tools.call 直接拒绝落库。
        requested = [str(t) for t in (tools or []) if str(t or "").strip()]
        base: list[str] = list(requested)
        if "submit_result" not in base:
            base.append("submit_result")
        if allowed_tools is None:
            hard_tools: tuple[str, ...] = tuple(base)
        else:
            allowed_set = set(str(x) for x in allowed_tools)
            narrowed = [n for n in base if n in allowed_set]
            if "submit_result" not in narrowed:
                narrowed.append("submit_result")
            hard_tools = tuple(narrowed)
        specs = self._tools.specs("worker", list(hard_tools))
        messages: list[dict] = [
            # system 提示按这一岗（agent_type）拿：它自己的 SOUL/AGENTS 注入
            {"role": "system", "content": _system_prompt(
                actor, group_id, output_schema, self._hint(skills_hint),
                identity=self._identity, extra_system=system_extra, agent=str(agent_type or "task"),
            )},
        ]
        if history:
            messages.extend(lanes.prepare_history(history, allowed=hard_tools))
        # 原始日志（docs/27 §9）：有上一轮的 canonical 原始历史（raw_history 传进来的）就从
        # 它接着写——**不从工作视图（history）抄**：工作视图可能已经带摘要行、tool 正文也换成了
        # 指针投影，抄它等于把 canonical 的完整正文丢掉一段。两边都没有才是这一轮的空日志。
        baseline = raw_history if raw_history else history
        raw_log: list[dict] = [dict(m) for m in (baseline or []) if isinstance(m, dict)]

        def _append(message: dict[str, Any]) -> None:
            """messages 和原始日志一起追加（两个列表各自持有自己的 dict，互不牵连）。"""
            messages.append(message)
            raw_log.append(dict(message))

        _append({"role": "user", "content": str(brief)})
        # docs/22 §4 F：这一轮子 agent 的止损账本（按主机计失败 + 连续没进展）
        stall = _StallGuard()
        ctx = ToolContext(
            group_id=str(group_id),
            task_id=str(task_id),
            actor=actor,
            workspace=workspace,
            role="worker",
            artifact_scope=artifact_scope,
            write_scope=write_scope,
            agent_type=str(agent_type or "task"),
            allowed_tools=hard_tools,
            used_tools=used_tools,
            allowed_skills=(
                tuple(str(x) for x in allowed_skills) if allowed_skills is not None else None
            ),
            call_gate=stall.gate,
        )
        try:
            steps = 0
            nudges = 0
            progress: list[str] = []  # 已有进展（失败 / 交回不了时汇报用）
            nudger = compaction.RepeatCallNudger()
            # 模型岗位 = 干活身份（agent=… 没给就用 agent_type）：岗位自己的模型/强度/备用
            # 自动生效；没配该岗候选的在 Models 侧兜底主模型链。
            agent_kind = str(agent or agent_type or "task")
            wrapped_up = False  # 到期强制交回的标记（一次：append 提示 + 只给 submit_result）
            stall_wrapped = False  # 止损收紧成只交回的标记（同样只做一次）
            spill_zone = _spill_zone(workspace, task_id)
            archive_notes: list[str] = []  # 归档失败要告诉模型的提示（这一轮攒着）

            while True:
                # 0 = 不限；给了正的 max_steps 还按老规矩收尾（兼容）
                if max_steps and steps >= max_steps:
                    break
                # 安全网：单任务 token / 时长超限 → 自动 paused（不再调模型）。
                # coordinator 的 _chat_main 同样会查；子 agent 长跑的那段时间靠这里兜底。
                if task_id and self._tasks is not None:
                    try:
                        reason = self._tasks.net_check(task_id)
                    except Exception:
                        reason = None
                    if reason:
                        return WorkerReport(
                            ok=False,
                            summary=(
                                "任务被安全网自动暂停（{}），子 agent 停手，没跑完的从恢复那刻接着干"
                            ).format(
                                "token 超线" if reason.get("kind") == "tokens" else "时长超线"
                            ),
                            steps=steps,
                            error="任务被安全网自动暂停",
                        )
                # 时间盒（feeds 资讯收集 15 分钟这类）：到点把「已找到的」立即交回，
                # 不是丢弃——一次强制交回机会（只给 submit_result），交不成按失败收场。
                if deadline_ts is not None and clock.now() >= deadline_ts:
                    if wrapped_up:
                        return WorkerReport(
                            ok=False,
                            summary=f"到点了，子 agent 没能用 submit_result 把已找到的交回。已有进展：{self._progress_text(progress)}",
                            steps=steps,
                            error="时间到，子 agent 没交回",
                        )
                    wrapped_up = True
                    if specs:
                        specs = [
                            x for x in (specs or [])
                            if (x.get("function") or {}).get("name") == "submit_result"
                        ] or specs
                    # 硬权限同步收紧：到期后只剩 submit_result 可调（Tools.call 层也会拦）。
                    ctx.allowed_tools = ("submit_result",)
                    _append(
                        {
                            "role": "user",
                            "content": (
                                "时间到了：请立刻用 submit_result 把已经找到的东西交回"
                                "（summary 必填；部分结果也算；没找到合格的就如实交回空结果并说明原因）。"
                            ),
                        }
                    )
                    nudger.note_user_message()
                # 每一步开头查一次任务状态：终态 / 暂停 / 挂起等到不该继续的状态立刻停——
                # 不再调模型、不再交付（线上踩过「取消了子 agent 还跑 40 秒」）
                status = self._task_status(task_id)
                if task_id and status:
                    if status in ("cancelled", "completed", "failed", "rejected"):
                        return WorkerReport(
                            ok=False,
                            summary=f"任务已是「{status}」，子 agent 停手，不再调模型",
                            steps=steps,
                            error="任务已取消或结束",
                            stopped=True,
                        )
                    if status in ("paused", "waiting_input", "shelved"):
                        return WorkerReport(
                            ok=False,
                            summary=f"任务已「{status}」，子 agent 先停手；任务恢复后再来",
                            steps=steps,
                            error="任务已暂停" if status == "paused" else "任务已挂起",
                            stopped=True,
                        )
                steps += 1
                # 上下文压缩（docs/27 的口）：整包预算 + 可回读剪枝 + 最老一段摘要，全在
                # compaction.maybe_compact_ex 里（它自己用 context_budget → models.request_budget
                # / limits_for 解析窗口与输出预留，不再由这里硬写 8192）。摘要失败它会把
                # 原 history 一条不动地返回（action="failed_original"），这里只管接结果。
                try:
                    outcome = await compaction.maybe_compact_ex(
                        messages,
                        models=self._models,
                        role="worker",
                        agent=agent_kind,
                        tools=specs or None,
                        escalate=escalate,
                        purpose="worker",
                        group_id=str(group_id),
                        task_id=str(task_id),
                        keep_recent_n=1,
                        # 剪枝只剪「能回读」的：正文里已有指针行，或这次经这个回调归档
                        archive=_archive_hook(spill_zone),
                        require_recoverable=True,
                    )
                    new_messages = outcome.messages if outcome is not None else None
                    if new_messages and new_messages is not messages:
                        # 原地替换：外层 finally 要拿同一个列表写回 lane 的 history
                        messages[:] = list(new_messages)
                        # 压缩是别人写的，配对标不保不该由子 agent 赌（严格端点少一条 400）
                        _repair_tool_pairing(messages, actor)
                    _log_compaction(actor, outcome)
                except Exception:
                    logger.exception("上下文压缩失败（%s），原样继续", actor)
                try:
                    result = await compaction.chat_with_retry_on_long_context(
                        messages,
                        models=self._models,
                        role="worker",
                        agent=agent_kind,
                        tools=specs or None,
                        purpose="worker",
                        group_id=str(group_id),
                        task_id=str(task_id),
                        on_trim=lambda trimmed: messages.__setitem__(slice(None), list(trimmed)),
                        **({"escalate": True} if escalate else {}),
                    )
                except ModelError as e:
                    logger.warning("子 agent（%s）模型调用失败：%s", actor, e.message)
                    return WorkerReport(ok=False, summary="", steps=steps, error=f"模型调用失败：{e.message}")

                # 记录 assistant 这一轮（有文本留作进展参考）
                if result.text.strip():
                    progress.append(result.text.strip()[:200])

                tool_calls = result.tool_calls or []
                if not tool_calls:
                    if result.text.strip() and nudges < _MAX_NUDGES:
                        nudges += 1
                        _append({"role": "assistant", "content": result.text})
                        _append(
                            {
                                "role": "user",
                                "content": "请调用 submit_result 工具把成果交回（summary 必填）；没干完就继续用工具干活。",
                            }
                        )
                        nudger.note_user_message()
                        continue
                    # 不调工具也没东西可催了 → 判失败
                    return WorkerReport(
                        ok=False,
                        summary=f"子 agent 一直没调用 submit_result 交回，催促两次后还是只说话不调工具。已有进展：{self._progress_text(progress)}",
                        steps=steps,
                        error="子 agent 没有通过 submit_result 交回",
                    )

                # OpenAI 规范：tool 结果前面必须先有这条 assistant(tool_calls)，否则严格的端点直接 400
                # （线上实测踩到：子 agent 一步都走不下去，资讯一批都出不来）
                _append({"role": "assistant", "content": result.text or "", "tool_calls": tool_calls})
                submitted = await self._run_tool_calls(
                    tool_calls, ctx, messages, nudger, spill_zone, stall, raw_log, archive_notes
                )
                if submitted is not None:
                    report = submitted
                    report.steps = steps
                    return report
                # docs/22 §4 F：连续没进展到止损线 → 和 deadline 一样只给 submit_result
                if stall.stop_requested and not stall_wrapped:
                    stall_wrapped = True
                    if specs:
                        specs = [
                            x for x in (specs or [])
                            if (x.get("function") or {}).get("name") == "submit_result"
                        ] or specs
                    ctx.allowed_tools = ("submit_result",)
                    _append({"role": "user", "content": _no_progress_stop()})
                    nudger.note_user_message()

            # 给了 max_steps（>0）且用完：兼容的老失败路径
            return WorkerReport(
                ok=False,
                summary=f"步数用完（{max_steps} 步）：子 agent 没能交回。已有进展：{self._progress_text(progress)}",
                steps=steps,
                error="步数用完",
            )
        finally:
            if history is not None:
                # 写回这一轮结束时的对话（不含 system；悬着的工具调用补回复），由调用方落库。
                # 这是**工作视图**：压缩可能已经把它改写过（切段进了摘要、旧 tool 正文被裁）。
                history[:] = lanes.close_dangling_tool_calls(messages[1:])
            if raw_history is not None:
                # 原始历史（只增不改）：压缩切掉的那段、压缩前的旧工具正文都还在里面。
                raw_history[:] = lanes.close_dangling_tool_calls(raw_log)

    @staticmethod
    def _progress_text(progress: list[str]) -> str:
        if not progress:
            return "（无）"
        return "；".join(progress[-3:])

    async def _run_tool_calls(
        self,
        tool_calls: list[dict],
        ctx: ToolContext,
        messages: list[dict],
        nudger: compaction.RepeatCallNudger | None = None,
        spill_zone: "_SpillZone | None" = None,
        stall: "_StallGuard | None" = None,
        raw_log: list[dict] | None = None,
        archive_notes: list[str] | None = None,
    ) -> WorkerReport | None:
        """顺序执行这一轮的工具调用并追加 tool 消息；遇到 submit_result 成功就构造报告返回。

        docs/22 §4 F：`stall` 给了就在每次调用后记一笔止损账（同一主机失败、连续没进展）；
        要提醒的话攒起来，等这一轮所有 tool 消息都追加完再插一条 user 消息——不能把
        user 消息插在同一个 assistant(tool_calls) 的多条 tool 结果中间（严格的端点会 400）。

        工具输出归档（docs/27 §8 P1）：只要这条消息会被单条上限截断（成功 / 报错都算），
        就先落盘归档、只给头尾 + 工作区相对指针；归档失败则**原样保留完整正文**、不再硬截，
        另记一条提示让模型知道没归档（`raw_log` 一起留档：它是这一轮的原始对话）。
        """
        stall_notes: list[str] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (ValueError, TypeError):
                    args = raw_args  # 透传给 tools.call 统一判错、落库
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            result = await self._tools.call(name, args, ctx)

            # submit_result 成功 = 交回
            if name == "submit_result" and result.ok and isinstance(result.data, dict):
                data = result.data
                evidence = data.get("evidence")
                return WorkerReport(
                    ok=True,
                    summary=str(data.get("summary") or ""),
                    data=data.get("data"),
                    evidence=[str(x) for x in evidence] if isinstance(evidence, list) else [],
                    challenge=data.get("challenge") if isinstance(data.get("challenge"), dict) else None,
                )

            # 其余（或 submit 失败）→ 把结果作为 role=tool 消息回给模型继续。
            # `full` = 这条工具**真正的完整输出**（原始历史里存的就是它）；`content` 是给
            # 模型的工作视图（可能已换成「头 + 尾 + 归档指针」）。
            full = result.output if result.ok else f"出错了：{result.error or result.output}"
            content = full
            archived = False
            if spill_zone is not None and content and len(content) >= _ARCHIVE_LINE:
                try:
                    content, archived_rel = _archive_tool_output(
                        content, spill_zone, file_tool=name in _FILE_TOOLS
                    )
                    archived = bool(archived_rel)
                except Exception:
                    logger.exception("工具输出归档出错（%s），按完整正文继续", name)
                    archived = False
                if not archived and archive_notes is not None:
                    archive_notes.append(
                        _ARCHIVE_FAIL_NOTE.format(name=name or "这个工具", chars=len(full))
                    )
                    logger.warning(
                        "工具 %s 的输出 %d 字没能归档：完整正文原样交给模型，交给预算闸判",
                        name or "（无名）", len(full),
                    )
            # **这里不再做任何 6000 字硬截断**（docs/27 §8 P1 / 父 agent 口径）：放得下就原样，
            # 放不下由预算闸（chat_with_retry_on_long_context）明确报错——「明确失败」比
            # 「悄悄丢掉中段、还看起来像读完了」正确。归档失败的正文同理，一个字都不删。
            # 文件工具页超预算是 tools_exec 那层的问题（它保证每页 ≤ 单条预算）：留一条
            # warning 便于定位，但不许因此动正文（线上 T-10 的教训是「别把半页当整页」，
            # 现在连半页都不会出现——要么整页，要么明确失败）。
            if len(content) > _TOOL_MSG_MAX and name in _FILE_TOOLS:
                logger.warning(
                    "文件工具 %s 的返回 %d 字超过单条 tool 消息上限 %d 字（tools_exec 应保证每页不超）",
                    name, len(content), _TOOL_MSG_MAX,
                )
            # 重复调用提醒：同参数连用第 3/5/8 次附加一句（只提醒，不拦截）
            if nudger is not None and name:
                try:
                    repeat_nudge = nudger.nudge(name, args)
                except Exception:
                    repeat_nudge = ""
                if repeat_nudge:
                    content = content + "\n\n" + repeat_nudge
            msg: dict[str, Any] = {"role": "tool", "content": content}
            if tc.get("id"):
                msg["tool_call_id"] = str(tc["id"])
            if name:
                msg["name"] = name
            messages.append(msg)
            if raw_log is not None:
                # 原始历史存**完整正文**（不是工作视图那份指针投影）：两者分开存，
                # 视图可以剪、原始那份不少一个字（父 agent 定的口径）。
                raw_msg = dict(msg)
                raw_msg["content"] = full
                raw_log.append(raw_msg)
            # docs/22 §4 F：止损账（同主机失败 / 连续没进展）
            if stall is not None:
                try:
                    note = stall.after(name, args, result)
                except Exception:
                    logger.exception("止损账记一笔出错（%s），按不提醒处理", name)
                    note = ""
                if note:
                    stall_notes.append(note)
        # 提醒一律等这一轮的 tool 消息都排完再插（不能插在 tool 结果中间）；归档失败的提示
        # 排在最前——模型要先知道「上一条其实没归档、正文是全的」。
        for note in [*(archive_notes or []), *stall_notes]:
            messages.append({"role": "user", "content": note})
            if raw_log is not None:
                raw_log.append({"role": "user", "content": note})
            if nudger is not None:
                nudger.note_user_message()
        return None
