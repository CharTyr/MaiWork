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

