"""子 agent 执行器（M2，docs/07-代码接口.md §10.3、需求 R9）。

主模型不亲自干长活：派给子 agent，子 agent 用 models.chat("worker", …) 自己
多轮循环，只能用名单里的工具（外加 submit_result），**只能通过 submit_result
交回**（summary、data、evidence）——不能自己宣布完成，由调用方（主模型 / feeds）验收。

0.4.0 起：
- **没有步数上限**：以调用 submit_result 结束；只说话不调工具催两次（_MAX_NUDGES），
  还不调就判失败。max_steps 参数保留兼容（None / 0 = 不限；调用方不再传 12 / 16）。
- 上下文压缩（compaction.py，共用）：估算超触发线先截旧 tool 结果（不调模型），
  仍超再调模型把最老一段总结成 8 节摘要；system 永不压缩、tool/assistant 成组切分；
  摘要失败原样继续；模型回「上下文超长」裁最旧一段重试一次。
- 大工具结果落盘：单个工具结果超约 5 万字时完整写到 `<workspace>/tool_spill/<任务ID>/`，
  对话里只放「头 + 尾 + 文件路径说明」；没工作区时退回头尾截断。
- 重复调用提醒：同一工具 + 规范化参数连用第 3 / 5 / 8 次往对话里加一句提醒
  （只提醒，不拦截）；有新的 user 消息进来计数清零。
- 模型调用 ModelError：ok=False，error 带原因。
- 任务安全网（app.schedule_task_net / Coordinator 侧，不是这个文件）：暂停时
  workers 下一步开头看到 paused 就停手返回。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clock, compaction
from .models import ModelError
from .tools import ToolContext, Tools

logger = logging.getLogger("maiwork.workers")

_TOOL_MSG_MAX = 6000    # 回给模型的单条 tool 消息截 6000 字（spill 之后一般远小于它）
_MAX_NUDGES = 2         # 最多催几次「请调用 submit_result 交回」

# 调研类子任务的报告框架（docs/02 §7.2 真实验收的配套）：主模型在计划里把子任务标成
# research 时，coordinator 把这段作为 system 提示的追加段传进来（system_extra）。
# 只给调研/对比/盘点这类要出结论的活；做东西（build）不加。文案给子 agent 看，不写实现细节。
RESEARCH_REPORT_FRAMEWORK = (
    "这是一件调研类的活，交给群里的成果按这个框架组织，别写成一堆链接：\n"
    "1. 一句话结论；\n"
    "2. 大家都同意的；\n"
    "3. 有分歧的（把分歧摆出来，不要取平均）；\n"
    "4. 吐槽最多的；\n"
    "5. 新冒头的；\n"
    "6. 没人提的（值得注意的空白）。\n"
    "每一点都要带链接，并标明「多源」（≥2 个不同站点支持）还是「单源」。"
)


@dataclass
class WorkerReport:
    ok: bool
    summary: str
    data: Any = None
    evidence: list[str] = field(default_factory=list)
    steps: int = 0
    error: str = ""


def _system_prompt(actor: str, group_id: str, output_schema: dict | None, skills_hint: str = "", identity: Any = None, extra_system: str = "") -> str:
    prefix = ""
    if identity is not None:
        try:
            prefix = identity.prompt_block("agents")
        except Exception:
            prefix = ""
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

    def _context_window(self) -> int:
        """上下文窗口（tokens）；取不到设置就用默认 128000。"""
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
    ) -> WorkerReport:
        specs = self._tools.specs("worker", list(tools) + ["submit_result"])
        messages: list[dict] = [
            {"role": "system", "content": _system_prompt(actor, group_id, output_schema, self._hint(skills_hint), identity=self._identity, extra_system=system_extra)},
            {"role": "user", "content": str(brief)},
        ]
        ctx = ToolContext(
            group_id=str(group_id),
            task_id=str(task_id),
            actor=actor,
            workspace=workspace,
            role="worker",
        )
        steps = 0
        nudges = 0
        progress: list[str] = []  # 已有进展（失败 / 交回不了时汇报用）
        nudger = compaction.RepeatCallNudger()
        context_window = self._context_window()
        wrapped_up = False  # 到期强制交回的标记（一次：append 提示 + 只给 submit_result）
        spill_dir = None
        if workspace is not None and task_id:
            try:
                spill_dir = Path(workspace) / "tool_spill" / str(task_id)
            except Exception:
                spill_dir = None

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
                messages.append(
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
                    )
                if status in ("paused", "waiting_input", "shelved"):
                    return WorkerReport(
                        ok=False,
                        summary=f"任务已「{status}」，子 agent 先停手；任务恢复后再来",
                        steps=steps,
                        error="任务已暂停" if status == "paused" else "任务已挂起",
                    )
            steps += 1
            # 上下文压缩：估算超触发线先截旧 tool 结果（不调模型），仍超再摘要最老一段。
            # 摘要失败原样继续（maybe_compact 内部吞掉，不抛）。
            try:
                messages = await compaction.maybe_compact(
                    messages,
                    models=self._models,
                    role="worker",
                    context_window=context_window,
                    output_reserve=compaction.DEFAULT_OUTPUT_RESERVE,
                    purpose="worker",
                    group_id=str(group_id),
                    task_id=str(task_id),
                    keep_recent_n=1,
                )
            except Exception:
                logger.exception("上下文压缩失败（%s），原样继续", actor)
            try:
                result = await compaction.chat_with_retry_on_long_context(
                    messages,
                    models=self._models,
                    role="worker",
                    tools=specs or None,
                    purpose="worker",
                    group_id=str(group_id),
                    task_id=str(task_id),
                    on_trim=lambda trimmed: messages.__setitem__(slice(None), list(trimmed)),
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
                    messages.append({"role": "assistant", "content": result.text})
                    messages.append(
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
            messages.append({"role": "assistant", "content": result.text or "", "tool_calls": tool_calls})
            submitted = await self._run_tool_calls(tool_calls, ctx, messages, nudger, spill_dir)
            if submitted is not None:
                report = submitted
                report.steps = steps
                return report

        # 给了 max_steps（>0）且用完：兼容的老失败路径
        return WorkerReport(
            ok=False,
            summary=f"步数用完（{max_steps} 步）：子 agent 没能交回。已有进展：{self._progress_text(progress)}",
            steps=steps,
            error="步数用完",
        )

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
        spill_dir: Path | None = None,
    ) -> WorkerReport | None:
        """顺序执行这一轮的工具调用并追加 tool 消息；遇到 submit_result 成功就构造报告返回。"""
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
                )

            # 其余（或 submit 失败）→ 把结果作为 role=tool 消息回给模型继续
            content = result.output if result.ok else f"出错了：{result.error or result.output}"
            # 大结果落盘：超出 spill 阈值的完整写到工作区，对话里放头 + 尾 + 路径说明
            if spill_dir is not None and result.ok and content:
                try:
                    content = compaction.spill_big_output(content, spill_dir)
                except Exception:
                    logger.exception("大结果落盘失败，原样走长度截断")
            if len(content) > _TOOL_MSG_MAX:
                content = content[:_TOOL_MSG_MAX] + " …（已截断）"
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
        return None
