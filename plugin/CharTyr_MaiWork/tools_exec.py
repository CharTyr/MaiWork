"""M3 子 agent 工具（docs/02-设计 §10、docs/07 §10.2 + §11.1）：

- 子 agent（worker）：read_file / write_file / list_files / run_command /
  start_process / check_process / stop_process / read_chat_history / search_memory。
- 主模型（main）：inspect_file / inspect_files（只读，用来验收子 agent 的成品）。

安全要点：
- 一切路径过 env.resolve：越界（绝对路径、..、符号链接逃逸）→ PermissionError
  → Tools.call 记一条失败调用，子 agent 看到的是中文「不允许访问…」。
- 工作区名取自 ctx.workspace.name；不传 workspace 一律报错，不让模型自己填
  workspace 参数（填了它也不会经过群授权检查）。
- run_command timeout_s 用 [environments] command_timeout_s 夹住（配置里写死
  默认 300 秒），配置之外模型自己说了不算。
- read_chat_history 只读本群：session 从外部 session_of(group_id) 回调拿
  （由 tasks.py/wiring 层去查 store.groups.session_id），本模块不依赖 store。
- search_memory 只传 group_id 不传 person_id（docs/06：person_id 过滤是假的）。
- EXEC 环境 runner 由 LocalEnv 内部注入；tools 层只看到语义化的 RunResult。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import clock
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_exec")

_READ_LIMIT_HINT = 50_000  # read_file/inspect_file 最多回给模型 5 万字


def register_exec_tools(
    tools: Tools,
    *,
    env: Any,  # LocalEnv（environments/local.py）
    host: Any,  # Host（host.py），用它的 messages/knowledge
    get_settings: Callable[[], Any],
    session_of: Callable[[str], str] | None = None,
) -> None:
    """把 M3 工具注册进 tools。

    - session_of(group_id) -> session_id：由接线层提供（去 store.groups 查）；
      没给或查不到时 read_chat_history 直接报「没找到本群会话」。
    - env 的 local_mode / 各种上限从 get_settings() 现读（配置改了下次生效）。
    """
    session_of = session_of or (lambda _gid: "")

    # ------------------------------------------------------------------
    # 共用：resolve 包成中文 ToolResult
    # ------------------------------------------------------------------

    def _bound(ctx: ToolContext, rel: str) -> tuple[Path | None, ToolResult | None]:
        """返回 (path, None) 或 (None, 错误 ToolResult)。"""
        if ctx.workspace is None:
            return None, ToolResult(ok=False, output="", error="这次任务没带工作区，不能操作文件")
        try:
            return env.resolve(ctx.workspace.name, str(rel or "")), None
        except PermissionError as e:
            return None, ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        except ValueError as e:
            return None, ToolResult(ok=False, output="", error=str(e))

    def _ws_name(ctx: ToolContext) -> str | None:
        return ctx.workspace.name if ctx.workspace is not None else None

    # ------------------------------------------------------------------
    # 文件工具
    # ------------------------------------------------------------------

    async def read_file(ctx: ToolContext, args: dict) -> ToolResult:
        path, err = _bound(ctx, str(args.get("path") or ""))
        if err:
            return err
        try:
            text = await env.read_file(ctx.workspace.name, str(args.get("path") or ""))
        except FileNotFoundError:
            return ToolResult(ok=False, output="", error=f"工作区内没有这个文件：{args.get('path')}")
        except IsADirectoryError:
            return ToolResult(ok=False, output="", error=f"{args.get('path')} 是目录，不是文件")
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        if len(text) > _READ_LIMIT_HINT:
            text = text[:_READ_LIMIT_HINT] + f"\n…（文件太长了，只给你看前 {_READ_LIMIT_HINT} 字，剩下的分次读）"
        return ToolResult(ok=True, output=text, data={"chars": len(text)})

    async def write_file(ctx: ToolContext, args: dict) -> ToolResult:
        rel = str(args.get("path") or "").strip()
        if not rel:
            return ToolResult(ok=False, output="", error="path 不能为空")
        content = args.get("content")
        if content is None:
            return ToolResult(ok=False, output="", error="content 不能为空")
        append = bool(args.get("append"))
        try:
            await env.write_file(ctx.workspace.name, rel, str(content), append=append)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        verb = "追加" if append else "写入"
        return ToolResult(ok=True, output=f"已{verb} {rel}（{len(str(content))} 字）", data={"path": rel})

    async def list_files(ctx: ToolContext, args: dict) -> ToolResult:
        path, err = _bound(ctx, str(args.get("path") or ""))
        if err:
            return err
        try:
            depth = int(args.get("depth") or 2)
        except (TypeError, ValueError):
            depth = 2
        try:
            limit = max(1, min(500, int(args.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        try:
            entries = await env.list_files(
                ctx.workspace.name, str(args.get("path") or ""), depth=depth, limit=limit
            )
        except FileNotFoundError:
            return ToolResult(ok=False, output="", error=f"工作区内没有这个目录：{args.get('path')}")
        except IsADirectoryError:
            return ToolResult(ok=False, output="", error=f"{args.get('path')} 是文件，不是目录")
        lines = []
        for e in entries:
            suffix = "/" if e["is_dir"] else f"（{e['size']} 字节）"
            lines.append(f"{e['path']}{suffix}")
        if not lines:
            return ToolResult(ok=True, output="（空的）", data=[])
        return ToolResult(ok=True, output="\n".join(lines), data=entries)

    # ------------------------------------------------------------------
    # 命令工具
    # ------------------------------------------------------------------

    async def run_command(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能跑命令")
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 不能为空")
        cfg_timeout = max(1, int(get_settings().environments.command_timeout_s))
        try:
            timeout_s = int(args.get("timeout_s") or cfg_timeout)
        except (TypeError, ValueError):
            timeout_s = cfg_timeout
        timeout_s = max(1, min(timeout_s, cfg_timeout))  # 配置为准，模型说了不算
        try:
            result = await env.run(ctx.workspace.name, command, timeout_s=timeout_s)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        head = f"退出码 {result.exit_code}"
        if result.timed_out:
            head += " · 超时被强制停止"
        if result.oom:
            head += " · 内存超限被杀（推断）"
        head += f" · 耗时 {result.ms / 1000:.1f} 秒"
        parts = [head, "--- 输出（stdout） ---"]
        parts.append(result.stdout or "（空）")
        if result.stderr:
            parts.append("--- 错误输出（stderr） ---")
            parts.append(result.stderr)
        return ToolResult(
            ok=True,
            output="\n".join(parts),
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "ms": result.ms,
                "timed_out": result.timed_out,
                "oom": result.oom,
            },
        )

    # ------------------------------------------------------------------
    # 后台进程
    # ------------------------------------------------------------------

    async def start_process(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能起进程")
        command = str(args.get("command") or "").strip()
        label = str(args.get("label") or "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 不能为空")
        if not label:
            return ToolResult(ok=False, output="", error="label 不能为空（起个名字方便回头查）")
        try:
            timeout_s = int(args.get("timeout_s") or get_settings().environments.runtime_max_sec)
        except (TypeError, ValueError):
            timeout_s = int(get_settings().environments.runtime_max_sec)
        try:
            unit = await env.start(ctx.workspace.name, command, label=label, timeout_s=timeout_s)
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        return ToolResult(
            ok=True,
            output=f"已在后台跑起来（{label}），单元名 {unit}",
            data={"unit": unit, "label": label},
        )

    async def check_process(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能查进程")
        label = str(args.get("label") or "").strip()
        if not label:
            return ToolResult(ok=False, output="", error="label 不能为空")
        try:
            label = env._check_label(label)  # noqa: SLF001 — 同一条防线，和 start_process 共用
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        unit = f"maiwork-{ctx.workspace.name}-{label}"
        st = await env.status(unit)
        logs = await env.logs(ctx.workspace.name, unit, tail=50)
        if st["active"]:
            status_line = f"{label} 还在跑"
        else:
            code_text = f"，退出码 {st['exit_code']}" if st["exit_code"] is not None else ""
            status_line = f"{label} 没在跑了{code_text}"
        parts = [status_line]
        if logs:
            parts.append("--- 最近日志 ---")
            parts.append(logs)
        return ToolResult(ok=True, output="\n".join(parts), data={"active": st["active"], "exit_code": st["exit_code"], "logs": logs})

    async def stop_process(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能停进程")
        label = str(args.get("label") or "").strip()
        if not label:
            return ToolResult(ok=False, output="", error="label 不能为空")
        try:
            label = env._check_label(label)  # noqa: SLF001 — 同一条防线，和 start_process 共用
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        unit = f"maiwork-{ctx.workspace.name}-{label}"
        await env.stop(unit)
        return ToolResult(ok=True, output=f"{label} 已停（如果它本来就没在跑，现在也没在跑了）")

    # ------------------------------------------------------------------
    # 群聊和记忆
    # ------------------------------------------------------------------

    async def read_chat_history(ctx: ToolContext, args: dict) -> ToolResult:
        group_id = str(ctx.group_id or "").strip()
        if not group_id:
            return ToolResult(ok=False, output="", error="拿不到当前群号")
        session_id = str(session_of(group_id) or "").strip()
        if not session_id:
            return ToolResult(ok=False, output="", error="没找到本群的会话")
        try:
            hours = float(args.get("hours") or 24)
        except (TypeError, ValueError):
            hours = 24.0
        hours = max(0.1, min(hours, 24 * 30))  # 最多往回翻 30 天
        try:
            limit = max(1, min(500, int(args.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        keyword = str(args.get("keyword") or "").strip().lower()
        end = clock.now()
        start = end - hours * 3600
        try:
            msgs = await host.messages(session_id, start, end, limit)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"读群消息失败：{e}")
        lines: list[str] = []
        for m in msgs:
            if keyword and keyword not in str(getattr(m, "text", "") or "").lower():
                continue
            stamp = clock.bj(float(getattr(m, "ts", 0.0))).strftime("%H:%M")
            name = str(getattr(m, "user_name", "") or "") or str(getattr(m, "user_id", "") or "")
            text = str(getattr(m, "text", "") or "")
            lines.append(f"[{stamp}] {name}: {text}")
        if not lines:
            hint = f"（带「{args.get('keyword')}」的）" if keyword else ""
            return ToolResult(ok=True, output=f"最近 {hours:g} 小时没有{hint}消息", data=[])
        if len(lines) > limit:
            lines = lines[-limit:]
        return ToolResult(ok=True, output="\n".join(lines), data={"count": len(lines)})

    async def search_memory(ctx: ToolContext, args: dict) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="", error="query 不能为空")
        group_id = str(ctx.group_id or "").strip()
        if not group_id:
            return ToolResult(ok=False, output="", error="拿不到当前群号")
        try:
            text = await host.knowledge(query, group_id=group_id)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"查记忆失败：{e}")
        if not str(text or "").strip():
            return ToolResult(ok=True, output="记忆里没留下这方面的印象", data={"found": False})
        return ToolResult(ok=True, output=str(text), data={"found": True})

    # ------------------------------------------------------------------
    # 注册（角色严格区分）
    # ------------------------------------------------------------------

    tools.register(
        Tool(
            name="read_file",
            description="读工作区内一个文件（utf-8 文本，最多 20 万字节，太长的分次读开头）。只能在当前任务的工作区内读。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "工作区内的相对路径，如 tasks/t1/out.md"}},
                "required": ["path"],
            },
            roles=frozenset({"worker"}),
            handler=read_file,
            summarize=lambda args, res: (
                str(args.get("path", "")),
                (res.output[:200] + "…") if res.ok and len(res.output) > 200 else (res.output if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="write_file",
            description="写工作区内一个文件（单文件最多 5MB；append=true 追加；自动建父目录）。工作区外一律写不进去。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径"},
                    "content": {"type": "string", "description": "完整内容（文本）"},
                    "append": {"type": "boolean", "description": "true = 追加到文件末尾；false/缺省 = 整文件覆盖"},
                },
                "required": ["path", "content"],
            },
            roles=frozenset({"worker"}),
            handler=write_file,
            summarize=lambda args, res: (
                str(args.get("path", "")),
                f"{'追加' if args.get('append') else '写入'} {len(str(args.get('content', '')))} 字" if res.ok else res.error,
            ),
            timeout_s=15.0,
        )
    )
    tools.register(
        Tool(
            name="list_files",
            description="列工作区内一个目录下的文件和子目录（默认两层深、200 条），看看里面有什么。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径；空 = 工作区根"},
                    "depth": {"type": "integer", "description": "往下钻几层，默认 2"},
                    "limit": {"type": "integer", "description": "最多列多少条，默认 200，上限 500"},
                },
            },
            roles=frozenset({"worker"}),
            handler=list_files,
            summarize=lambda args, res: (
                f"列目录：{args.get('path', '') or '（根）'}",
                (f"{len(res.data or [])} 个条目" if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="run_command",
            description=f"在工作区里跑一条 bash 命令，拿回退出码、stdout、stderr、耗时。默认超时和上限以配置为准（单条命令最长 [environments] runtime_max_sec，再长的活要拆着跑）。命令里写 ~ 指工作区，不继承插件进程的环境变量。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "bash 命令（会按 bash -lc 跑）"},
                    "timeout_s": {"type": "integer", "description": "这条命令最多跑多少秒；不给 = 配置里的默认上限；给得比配置大也会被夹住"},
                },
                "required": ["command"],
            },
            roles=frozenset({"worker"}),
            handler=run_command,
            summarize=lambda args, res: (
                f"输入「{str(args.get('command', '')).splitlines()[0] if args.get('command') else ''}」",
                (
                    (f"退出码 {res.data['exit_code']} · 超时" if res.data and res.data.get("timed_out") else f"退出码 {res.data['exit_code']}")
                    + (f" · 内存超限" if res.data and res.data.get("oom") else "")
                    + (f" · {res.data['ms'] / 1000:.1f} 秒" if res.data else "")
                )
                if res.ok and isinstance(res.data, dict)
                else (res.error or "出错"),
            ),
            timeout_s=3600.0,  # 工具自身的兜底：比 runtime_max_sec + 兜底 15s 多一档
        )
    )
    tools.register(
        Tool(
            name="start_process",
            description="在工作区里起一个后台进程（不等你），起个 label 名字。之后用 check_process 看它跑得怎么样、stop_process 停它。输出会一直写到 runtime/logs/<label>.log。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "bash 命令"},
                    "label": {"type": "string", "description": "进程名，只能用字母数字下划线横线"},
                    "timeout_s": {"type": "integer", "description": "到点强杀；默认 = 配置里的 runtime_max_sec"},
                },
                "required": ["command", "label"],
            },
            roles=frozenset({"worker"}),
            handler=start_process,
            summarize=lambda args, res: (
                f"起进程 {args.get('label', '')}",
                res.output if res.ok else res.error,
            ),
            timeout_s=30.0,
        )
    )
    tools.register(
        Tool(
            name="check_process",
            description="看后台进程还在不在跑、退出码多少，顺带把最近几行日志捞出来。",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string", "description": "start_process 起的那个名字"}},
                "required": ["label"],
            },
            roles=frozenset({"worker"}),
            handler=check_process,
            summarize=lambda args, res: (
                f"查进程 {args.get('label', '')}",
                (res.output.splitlines()[0] if res.output else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="stop_process",
            description="停掉一个后台进程（SIGKILL）。名字写错了/本就没在跑不会报错。",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string", "description": "start_process 起的那个名字"}},
                "required": ["label"],
            },
            roles=frozenset({"worker"}),
            handler=stop_process,
            summarize=lambda args, res: (
                f"停进程 {args.get('label', '')}",
                res.output if res.ok else res.error,
            ),
            timeout_s=15.0,
        )
    )
    tools.register(
        Tool(
            name="read_chat_history",
            description="读本群最近一段时间的聊天记录（只会给你本群的，别群不给）。输出一行一条，「[HH:MM] 名字: 文本」。超过需求再带 keyword 筛。",
            parameters={
                "type": "object",
                "properties": {
                    "hours": {"type": "number", "description": "往回翻多少小时，默认 24；最多 30 天"},
                    "keyword": {"type": "string", "description": "只留文本里带这个词的消息（可选）"},
                    "limit": {"type": "integer", "description": "最多多少条，默认 200，上限 500"},
                },
            },
            roles=frozenset({"worker"}),
            handler=read_chat_history,
            summarize=lambda args, res: (
                f"读本群最近 {args.get('hours', 24)} 小时" + (f"（筛 {args.get('keyword')}）" if args.get("keyword") else ""),
                f"{(res.data or {}).get('count', 0)} 条" if res.ok else res.error,
            ),
            timeout_s=20.0,
        )
    )
    tools.register(
        Tool(
            name="search_memory",
            description="查 MaiBot 的长期记忆（按本群过滤；不是按人）。想起来要引用旧事时再用。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "想找的内容，用人话说"}},
                "required": ["query"],
            },
            roles=frozenset({"worker"}),
            handler=search_memory,
            summarize=lambda args, res: (
                f"查记忆：{args.get('query', '')}",
                ("找到了" if (res.data or {}).get("found") else "没印象") if res.ok else res.error,
            ),
            timeout_s=20.0,
        )
    )
    tools.register(
        Tool(
            name="inspect_file",
            description="（主模型验收用）读工作区内一个文件，和子 agent 的 read_file 是同一个东西的只读版。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "工作区内的相对路径"}},
                "required": ["path"],
            },
            roles=frozenset({"main"}),
            handler=read_file,
            summarize=lambda args, res: (
                f"（验收）{args.get('path', '')}",
                (res.output[:120] + "…") if res.ok and len(res.output) > 120 else (res.output if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="inspect_files",
            description="（主模型验收用）列工作区内一个目录下的文件，和子 agent 的 list_files 是同一个东西的只读版。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径；空 = 根"},
                    "depth": {"type": "integer", "description": "往下钻几层，默认 2"},
                    "limit": {"type": "integer", "description": "最多多少条，默认 200"},
                },
            },
            roles=frozenset({"main"}),
            handler=list_files,
            summarize=lambda args, res: (
                f"（验收）{args.get('path', '') or '根目录'}",
                (f"{len(res.data or [])} 个条目" if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )


__all__ = ["register_exec_tools"]
