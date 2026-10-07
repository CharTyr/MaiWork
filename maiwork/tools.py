"""工具注册与调用（M2，docs/07-代码接口.md §10.2）。

- register 校验工具名（只能 [A-Za-z0-9_-]{1,64}）和重名，坏名字抛 ValueError。
- call() 是子 agent / 主模型调工具的唯一入口：查角色、查必填参数、限时执行，
  每次调用（无论成败）都写 tool_calls 表，供网页时间线展示。
- 出入参摘要各截 500 字；键名含 key/token/password/secret 的值一律替换成 ***，
  密钥绝不落库。
- recent_calls() 给网页任务时间线用。

与 §10.2 的出入：ToolContext 增加了可选字段 role（"worker"/"main"）和
effective_role()，用来判定角色限制——§10.2 里 ToolContext 只有
group_id/task_id/actor/workspace，没处放角色；role 不传时按引导式判断
（actor 含「子 agent」当 worker，否则当 main），正好兼容文档给的默认
actor="主模型"。工具 handler 里判角色统一用模块函数 role_of(ctx)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import clock
from .store import Store

logger = logging.getLogger("maiwork.tools")

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 键名里出现这些词（小写后包含即算），值落库前替换成 ***
_SENSITIVE_WORDS = ("key", "token", "password", "secret")
_SUMMARY_MAX = 500
# 管理员对话的角色名（tools_admin.ROLE 同值）；这个角色的工具单独注册表
ADMIN_ROLE = "admin"
# 群绑工具（worker 角色时 args.group_id 不允许越 ctx.group_id）：这些工具读「本群的资料」
# （画像 / 群聊消息 / 语义搜群 / 群记忆）。worker 拿 args 伪造 group_id 越群读别群资料，
# 在 handler 之前就被拦；admin / main 不变（主模型/管理员可指定群——比如验收时正群查资料）。
_GROUP_BOUND_TOOLS = frozenset({
    "read_profile", "read_chat_history", "search_chat", "search_memory",
})
# M5：摘要文本统一再过一遍的密钥形式
_FINAL_BEARER_RE = re.compile(r"(?i)Bearer\s+\S+")
_FINAL_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{3,}")
_FINAL_APIKEY_RE = re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+")

Handler = Callable[["ToolContext", dict], Awaitable["ToolResult"]]
Summarizer = Callable[[dict, "ToolResult"], tuple[str, str]]


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: Any = None
    error: str = ""


def role_of(ctx: Any) -> str:
    """从 ToolContext 判角色：role 显式给了就用；空则按 actor 引导式判断。

    引导式判断（§10.2 的 ToolContext 没 role 字段）：actor 名里带「子 agent」当
    worker，其余当 main——正好兼容文档给的默认 actor="主模型"。
    """
    role = str(getattr(ctx, "role", "") or "")
    if role:
        return role
    return "worker" if "子 agent" in str(getattr(ctx, "actor", "")) else "main"


@dataclass
class ToolContext:
    group_id: str
    task_id: str = ""
    actor: str = "主模型"
    workspace: Path | None = None
    role: str = ""  # "worker" | "main"；空则按 actor 引导式判断（§10.2 没这个字段，见模块 docstring）
    # 成品目录隔离（2026-10，线上 T-4 串文件整改）：允许碰的 artifacts 目录
    # （工作区相对路径，不含结尾 /，如 ("artifacts/T-4", "artifacts/T-2")）。
    # None / 空 = 不限制（管理员对话、资讯等老调用方行为不变）。tools_exec 的
    # 文件工具（read/write/list）只在它非空时拦「scope 外、artifacts/ 下」的路径。
    artifact_scope: tuple[str, ...] | None = None
    # 专岗（specialists.py）注入的执行身份与硬权限（2026-09-30 专岗契约 B 部分）：
    # - agent_type：这次 worker 回合的执行身份（"task" 默认；"news"/"idea"/"goal" 专岗）。
    #   只读标识，工具 handler 可以据它调行为；绝不能用工具 args 伪造（args 进不了 ctx）。
    # - allowed_tools：本轮**硬权限**工具名单（tuple of 工具名）。给了（非 None）就只许
    #   调用名单内的工具——模型回传未提供的工具名直接拒绝执行（不是只给 spec 提示）。
    #   None = 不加这层名单（老调用方行为不变，仅角色门控）。
    # - allowed_skills：本轮允许 read_skill/list_skills 的 skill 白名单（tuple of 名）。
    #   None = 通才（不加岗位滤网，仅 roles/全局开关过滤）。
    agent_type: str = "task"
    allowed_tools: tuple[str, ...] | None = None
    allowed_skills: tuple[str, ...] | None = None
    # 各步骤分文件夹（docs/22 §4 C，2026-10-07 本地）：**写**文件的范围（工作区相对目录，
    # 如 ("artifacts/T-4/steps/1",)）。None / 空 = 不限制写（管理员对话、资讯等老调用方）。
    # 只管 artifacts/ 下的写：路径要落在写范围里，且 `artifacts/<任务>/steps/<步号>/`
    # 只许对应那一步写（交付步骤写不了别人的步骤目录）。读不受它管（读仍按 artifact_scope）。
    write_scope: tuple[str, ...] | None = None
    # 每次执行独有的可信遥测，不从模型参数读取；只记真正派发的工具。
    used_tools: set[str] | None = None
    # 调用前的闸（docs/22 §4 F 止损用，workers 注入）：callable(name, args) -> ToolResult | None。
    # 返回 ToolResult 就短路（不调 handler），但仍然照常落一条 tool_calls（可审计）；
    # 返回 None / 不是 ToolResult → 正常派发。别的作用域（主模型 / 管理员）不传 = 不启用。
    call_gate: Any = None

    def effective_role(self) -> str:
        """这个上下文实际算哪个角色（role 优先，空则看 actor）。工具 handler 里也用它。"""
        return role_of(self)


@dataclass
class Tool:
    name: str                    # 只能 [A-Za-z0-9_-]，register 时校验
    description: str
    parameters: dict             # JSON Schema（object + properties + required）
    roles: frozenset[str]        # {"main"} / {"worker"} / 两者
    handler: Handler
    summarize: Summarizer | None = None  # 网页显示的「输入 → 输出」一句话
    timeout_s: float = 60.0      # 单个工具默认 60 秒


class Tools:
    """工具注册表 + 唯一调用入口。每次调用都落 tool_calls 表。"""

    def __init__(self, store: Store, *, get_known_secrets: Callable[[], list[str]] | None = None) -> None:
        # M5：落库摘要最后还要统一遮一次密钥。除了 Bearer / sk- / api_key= 这些形式，
        # 「已知具体密钥」由调用方回调给（app 传模型 / 搜索密钥），防 summarize 写漏。
        self._store = store
        self._get_known_secrets = get_known_secrets
        self._tools: dict[str, Tool] = {}
        # 管理员对话（role="admin"）的工具单独一格：可以和主模型/子 agent 的工具同名
        # （例如都有 read_profile），但各查各的，谁也调不到对方那一份。
        self._admin_tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def _table(self, role: str) -> dict[str, Tool]:
        return self._admin_tools if role == ADMIN_ROLE else self._tools

    def register(self, tool: Tool) -> None:
        name = str(tool.name or "")
        if not _NAME_RE.match(name):
            raise ValueError(f"工具名字不合法：{name!r}（只能用字母、数字、下划线、横线，1~64 个字符）")
        roles = frozenset(str(r) for r in tool.roles)
        if ADMIN_ROLE in roles and roles != {ADMIN_ROLE}:
            raise ValueError(f"工具 {name} 的角色不对：管理员工具只能是 admin 一个角色，不能和别的角色混用")
        table = self._admin_tools if ADMIN_ROLE in roles else self._tools
        if name in table:
            raise ValueError(f"工具名字重复：{name} 已注册过")
        tool.roles = roles
        table[name] = tool
        logger.debug("注册工具 %s（角色：%s）", name, ",".join(sorted(tool.roles)))

    def get(self, name: str, role: str) -> Tool | None:
        """按角色取已注册的工具（管理员一格、其余一格）；角色不允许就当没有。"""
        tool = self._table(str(role)).get(str(name))
        if tool is None or str(role) not in tool.roles:
            return None
        return tool

    def unregister(self, name: str) -> bool:
        """摘掉一个已注册的工具（MCP 扩展 reload 时用）。不存在返回 False。"""
        if not isinstance(name, str) or not name:
            return False
        if self._tools.pop(name, None) is None:
            return False
        logger.debug("摘掉工具 %s", name)
        return True

    # ------------------------------------------------------------------
    # specs：OpenAI tools 格式
    # ------------------------------------------------------------------

    def specs(self, role: str, names: list[str] | None = None) -> list[dict]:
        """某角色可用的工具规格。names 给了就只要这几个（按给定顺序，忽略未注册的）。"""
        table = self._table(role)
        wanted = list(names) if names is not None else list(table)
        out: list[dict] = []
        for name in wanted:
            tool = table.get(name)
            if tool is None or role not in tool.roles:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return out

    def catalog(self, role: str) -> list[tuple[str, str]]:
        """某角色**现在**能用的工具清单 [(名字, 描述)]（按名字排序）。

        给「构想可行性评估」（idea_feasibility.inventory）算能力清单用：只列这个角色
        真正注册着、且允许这个角色的工具。工具被摘掉（app._drop_command_tools_if_stopped
        摘 run_command / start_process 等）后这里自然没有它——清单就那么算出来的。
        """
        table = self._table(str(role))
        out: list[tuple[str, str]] = []
        for name in sorted(table):
            tool = table[name]
            if str(role) in tool.roles:
                out.append((tool.name, tool.description))
        return out

    # ------------------------------------------------------------------
    # call：唯一入口
    # ------------------------------------------------------------------

    @staticmethod
    def _role_of(ctx: ToolContext) -> str:
        return role_of(ctx)

    async def call(self, name: str, args: dict | str, ctx: ToolContext) -> ToolResult:
        """查名字、查角色、查必填参数，限时执行；结果落库后返回。

        args 可以是 dict（正常）或 str（模型给的 arguments 原文；这是 §10.2 之外的
        容错扩展：workers 遇到解析不了的 arguments 会把原串透传进来，统一在这里
        判定、落库，错误消息面向模型）。
        """
        start = clock.now()
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                parsed = None
            if not isinstance(parsed, dict):
                # 解析不了的参数原文不落库（红线：密钥不落库，坏串里指不定有什么）
                result = ToolResult(ok=False, output="", error="工具参数不是合法的 JSON 对象，请检查格式后重试")
                self._persist(name, {"_error": "arguments 不是合法 JSON"}, result, ctx, start)
                return result
            args = parsed
        role = self._role_of(ctx)
        # 硬权限：本轮工具名单（专岗/时间盒收尾强约束）。模型捏造未提供的工具名
        # 到这里直接拒绝——落库保留痕迹，不调 handler。args 伪造 role/group/agent_type
        # 没用：这些只由 ToolContext 提供，进不了调用参数。
        allowed_tools = getattr(ctx, "allowed_tools", None)
        if allowed_tools is not None and str(name) not in allowed_tools:
            result = ToolResult(
                ok=False, output="",
                error=f"工具「{name}」不在本轮允许使用的名单里，不能调用",
            )
            self._persist(name, args, result, ctx, start)
            return result
        tool = self._table(role).get(name)
        if tool is None:
            # 另一格里有同名工具：说清楚是「角色不允许」，而不是「不认识」
            other = self._tools if role == ADMIN_ROLE else self._admin_tools
            if name in other:
                who = "主模型或子 agent" if role == ADMIN_ROLE else "bot 管理员"
                result = ToolResult(ok=False, output="", error=f"工具「{name}」只有{who}能用，当前角色不允许")
                self._persist(name, args, result, ctx, start)
                return result
        if tool is None:
            result = ToolResult(ok=False, output="", error=f"不认识名为「{name}」的工具")
            self._persist(name, args, result, ctx, start)
            return result
        if role not in tool.roles:
            allowed = "、".join(
                {"main": "主模型", "admin": "bot 管理员"}.get(r, "子 agent") for r in sorted(tool.roles)
            )
            result = ToolResult(ok=False, output="", error=f"工具「{name}」只有{allowed}能用，当前角色不允许")
            self._persist(name, args, result, ctx, start)
            return result
        # 群绑闸（worker 专属）：读「本群资料」的工具不许用 args.group_id 越群。
        # handler 自己也可能再用 ctx.group_id 兜底，这一层是它的防线——模型用 args 伪造
        # group_id 时 handler 根本没机会被调。main / admin 不在闸内（主模型/管理员可跨群）。
        if (
            role == "worker"
            and str(name) in _GROUP_BOUND_TOOLS
            and isinstance(args, dict)
        ):
            requested_gid = str(args.get("group_id") or "").strip()
            if requested_gid and requested_gid != str(ctx.group_id or ""):
                result = ToolResult(
                    ok=False, output="",
                    error=f"工具「{name}」只能读当前群的资料，不能跨群（你给了别的群号）",
                )
                self._persist(name, args, result, ctx, start)
                return result
        if not isinstance(args, dict):
            result = ToolResult(ok=False, output="", error="工具参数必须是一个 JSON 对象")
            self._persist(name, args, result, ctx, start)
            return result
        required = tool.parameters.get("required") or []
        missing = [k for k in required if k not in args]
        if missing:
            result = ToolResult(ok=False, output="", error=f"缺必填参数：{('、'.join(str(m) for m in missing))}")
            self._persist(name, args, result, ctx, start)
            return result
        timeout = tool.timeout_s if tool.timeout_s and tool.timeout_s > 0 else 60.0
        used_tools = getattr(ctx, "used_tools", None)
        if isinstance(used_tools, set) and name != "submit_result":
            used_tools.add(name)
        # 调用前的闸（可选，workers 止损用）：短路也照常落一条 tool_calls（可审计）。
        gate = getattr(ctx, "call_gate", None)
        if callable(gate):
            try:
                blocked = gate(name, args)
            except Exception:
                logger.exception("调用前闸出错（%s），按不拦处理", name)
                blocked = None
            if isinstance(blocked, ToolResult):
                self._persist(name, args, blocked, ctx, start)
                return blocked
        try:
            result = await asyncio.wait_for(tool.handler(ctx, args), timeout=timeout)
        except asyncio.TimeoutError:
            result = ToolResult(ok=False, output="", error=f"工具「{name}」执行超时（超过 {timeout:g} 秒）")
        except Exception as e:  # handler 炸了也不能把子 agent 搞死
            logger.exception("工具 %s 执行出错", name)
            result = ToolResult(ok=False, output="", error=f"工具「{name}」出错：{e}")
        if not isinstance(result, ToolResult):
            result = ToolResult(ok=bool(result), output=str(result))
        self._persist(name, args, result, ctx, start)
        return result

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    @staticmethod
    def _mask_sensitive(value: Any) -> Any:
        """递归遮密钥：键名含 key/token/password/secret 的值换成 ***。"""
        if isinstance(value, dict):
            masked: dict[str, Any] = {}
            for k, v in value.items():
                key_l = str(k).lower()
                if any(w in key_l for w in _SENSITIVE_WORDS):
                    masked[k] = "***"
                else:
                    masked[k] = Tools._mask_sensitive(v)
            return masked
        if isinstance(value, list):
            return [Tools._mask_sensitive(v) for v in value]
        return value

    @staticmethod
    def _truncate(text: str, limit: int = _SUMMARY_MAX) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _summarize(self, tool: Tool | None, name: str, args: Any, result: ToolResult) -> tuple[str, str]:
        """输入/输出摘要：工具有 summarize 用它（各截 500），否则默认 JSON 截断（先遮密钥）。"""
        if tool is not None and tool.summarize is not None:
            try:
                inp, outp = tool.summarize(args if isinstance(args, dict) else {}, result)
                return self._truncate(str(inp)), self._truncate(str(outp))
            except Exception:
                logger.exception("工具 %s 的 summarize 出错，退回默认摘要", name)
        safe_args = self._mask_sensitive(args if isinstance(args, dict) else {})
        try:
            inp = json.dumps(safe_args, ensure_ascii=False)
        except (TypeError, ValueError):
            inp = str(safe_args)
        outp = result.output if result.ok else (result.error or result.output)
        return self._truncate(inp), self._truncate(str(outp))

    def _mask_final(self, text: str) -> str:
        """M5：最终摘要统一遮罩——Bearer / sk- / api_key= / 已知密钥。"""
        out = str(text or "")
        for getter_secret in self._known_secrets_safely():
            if getter_secret:
                out = out.replace(getter_secret, "***")
        out = _FINAL_BEARER_RE.sub("Bearer ***", out)
        out = _FINAL_SK_RE.sub("sk-***", out)
        out = _FINAL_APIKEY_RE.sub(lambda m: m.group(1) + "***", out)
        return out

    def _known_secrets_safely(self) -> list[str]:
        getter = self._get_known_secrets
        if getter is None:
            return []
        try:
            values = getter()
        except Exception:
            logger.exception("get_known_secrets 回调出错，这次的已知密钥不遮")
            return []
        if not isinstance(values, (list, tuple)):
            return []
        return [str(v) for v in values if str(v or "")]

    def _persist(self, name: str, args: Any, result: ToolResult, ctx: ToolContext, start: float) -> None:
        tool = self._table(self._role_of(ctx)).get(name)
        inp, outp = self._summarize(tool, name, args, result)
        inp = self._mask_final(inp)[:_SUMMARY_MAX]
        outp = self._mask_final(outp)[:_SUMMARY_MAX]
        ms = int((clock.now() - start) * 1000)
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clock.now(),
                        str(ctx.group_id or ""),
                        str(ctx.task_id or ""),
                        str(ctx.actor or ""),
                        str(name),
                        inp,
                        outp,
                        ms,
                        1 if result.ok else 0,
                        self._mask_final(str(result.error or ""))[:_SUMMARY_MAX],
                    ),
                )
        except Exception:
            logger.exception("写 tool_calls 失败（tool=%s ok=%s）", name, result.ok)

    # ------------------------------------------------------------------
    # 网页时间线
    # ------------------------------------------------------------------

    def recent_calls(
        self,
        *,
        task_id: str | None = None,
        group_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """最近的工具调用（按时间升序），给任务时间线用。task_id / group_id 至少给一个。"""
        where: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            where.append("task_id=?")
            params.append(str(task_id))
        if group_id is not None:
            where.append("group_id=?")
            params.append(str(group_id))
        if not where:
            return []
        # 先取最近 limit 条（倒序），再翻回升序方便时间线展示
        sql = (
            "SELECT ts, actor, tool, input, output, ms, ok FROM tool_calls"
            f" WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        rows = self._store.read().execute(sql, params).fetchall()
        return [
            {
                "ts": float(r["ts"]),
                "actor": str(r["actor"]),
                "tool": str(r["tool"]),
                "input": str(r["input"]),
                "output": str(r["output"]),
                "ms": int(r["ms"]),
                "ok": bool(r["ok"]),
            }
            for r in reversed(rows)
        ]

