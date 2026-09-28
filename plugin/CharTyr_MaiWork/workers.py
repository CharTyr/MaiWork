"""子 agent 执行器（M2，docs/07-代码接口.md §10.3、需求 R9）。

主模型不亲自干长活：派给子 agent，子 agent 用 models.chat("worker", …) 自己
多轮循环，只能用名单里的工具（外加 submit_result），**只能通过 submit_result
交回**（summary、data、evidence）——不能自己宣布完成，由调用方（主模型 / feeds）验收。

- 步数超 max_steps：ok=False，summary 带「步数用完」+已有进展。
- ModelError：ok=False，error 带原因。
- 模型有文本但不调工具：提示「请调用 submit_result 交回」再试，最多催 2 次。
- tool_calls 的 arguments 解析不了：经 tools.call 落一条失败记录，并回给模型
  一条错误 tool 消息继续（不中断循环）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .models import ModelError
from .tools import ToolContext, Tools

logger = logging.getLogger("maiwork.workers")

_TOOL_MSG_MAX = 6000    # 回给模型的单条 tool 消息截 6000 字
_MAX_NUDGES = 2         # 最多催几次「请调用 submit_result 交回」


@dataclass
class WorkerReport:
    ok: bool
    summary: str
    data: Any = None
    evidence: list[str] = field(default_factory=list)
    steps: int = 0
    error: str = ""


def _system_prompt(actor: str, group_id: str, output_schema: dict | None, skills_hint: str = "", identity: Any = None) -> str:
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
    def __init__(self, models: Any, tools: Tools, skills_hint_fn: Any = None, identity: Any = None, tasks: Any = None) -> None:
        self._models = models
        self._tools = tools
        # skills_hint_fn：无参返回「名字：一句描述」清单文本（app 给默认的；
        # 调 run 时传 skills_hint 可覆盖本次）。没有 / 返回空 → system 提示不加 skill 段。
        self._skills_hint_fn = skills_hint_fn
        # identity（identity.py；做事规矩 AGENTS.md 注入子 agent system；None 就跳过）
        self._identity = identity
        # tasks（Tasks；给了就会在每一步开头查任务状态——已是 cancelled 等终态立刻停，
        # 不再调模型、不再交付。没有就只按 max_steps 收尾。）
        self._tasks = tasks

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

    async def run(
        self,
        brief: str,
        *,
        group_id: str,
        tools: list[str],
        task_id: str = "",
        actor: str = "子 agent #1",
        max_steps: int = 12,
        output_schema: dict | None = None,
        workspace: Any = None,
        skills_hint: Any = None,
    ) -> WorkerReport:
        specs = self._tools.specs("worker", list(tools) + ["submit_result"])
        messages: list[dict] = [
            {"role": "system", "content": _system_prompt(actor, group_id, output_schema, self._hint(skills_hint), identity=self._identity)},
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
        progress: list[str] = []  # 已有进展（步数用完时汇报用）

        while steps < max_steps:
            # 每一步开头查一次任务状态：已是 cancelled（或其它终态）立刻停——
            # 不再调模型、不再交付（线上踩过「取消了子 agent 还跑 40 秒」）
            if self._tasks is not None and task_id:
                try:
                    row = self._tasks.get(task_id)
                except Exception:
                    row = None
                if row is not None and str(row.get("status") or "") in (
                    "cancelled", "completed", "failed", "rejected",
                ):
                    return WorkerReport(
                        ok=False,
                        summary=f"任务已是「{row.get('status')}」，子 agent 停手，不再调模型",
                        steps=steps,
                        error="任务已取消或结束",
                    )
            steps += 1
            left = max_steps - steps + 1  # 含这一步
            step_specs = specs
            if max_steps >= 3 and left == 2:
                messages.append({"role": "user", "content": (
                    "只剩 2 步了。下一步就用 submit_result 把已经找到的内容交回（部分结果也行），不要再开新的搜索。"
                )})
            elif max_steps >= 2 and left == 1:
                # 最后一步：只给交回工具，防止一直搜到步数用完、整批作废（线上实测踩到）
                messages.append({"role": "user", "content": (
                    "这是最后一步，只能调用 submit_result 交回已经找到的内容；没找到合格的就如实交回空结果并说明原因。"
                )})
                step_specs = [x for x in (specs or []) if (x.get("function") or {}).get("name") == "submit_result"] or specs
            try:
                result = await self._models.chat(
                    "worker",
                    messages,
                    tools=step_specs or None,
                    purpose="worker",
                    group_id=str(group_id),
                    task_id=str(task_id),
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
                    continue
                # 没调工具也没东西可催了 → 步数到头按失败处理
                return WorkerReport(
                    ok=False,
                    summary=f"步数用完：子 agent 一直没调用 submit_result 交回。已有进展：{self._progress_text(progress)}",
                    steps=steps,
                    error="子 agent 没有通过 submit_result 交回",
                )

            # OpenAI 规范：tool 结果前面必须先有这条 assistant(tool_calls)，否则严格的端点直接 400
            # （线上实测踩到：子 agent 一步都走不下去，资讯一批都出不来）
            messages.append({"role": "assistant", "content": result.text or "", "tool_calls": tool_calls})
            submitted = await self._run_tool_calls(tool_calls, ctx, messages)
            if submitted is not None:
                report = submitted
                report.steps = steps
                return report

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
        self, tool_calls: list[dict], ctx: ToolContext, messages: list[dict]
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
            if len(content) > _TOOL_MSG_MAX:
                content = content[:_TOOL_MSG_MAX] + " …（已截断）"
            msg: dict[str, Any] = {"role": "tool", "content": content}
            if tc.get("id"):
                msg["tool_call_id"] = str(tc["id"])
            if name:
                msg["name"] = name
            messages.append(msg)
        return None
