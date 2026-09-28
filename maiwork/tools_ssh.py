"""专用 SSH 机器上的子 agent 工具（worker 角色；环境见 environments/ssh.py）。

派活时主模型选了「专用机器」（env=ssh）并且分到了一台，这几个工具才有机器可用；
机器按任务分：get_box(task_id) 现读这个任务占着的那台（同时可能有几个任务各占一台）。

- machine_run：在这次的工作目录里跑一条命令（远端退出码原样回传；timeout_s 上限 1800 秒）。
- machine_put_file：把**本机工作区里的一个文件**传到机器工作目录下（相对路径）。
- machine_read_file：读机器工作目录下的一个文件（截 20000 字）。
- machine_fetch_file：把机器工作目录下的文件拷回本机工作区 artifacts/<任务ID>/ 下
  ——成品必须拿回本机工作区才能交付；单文件 ≤20MB。

安全要点：远端路径只收工作目录下的相对路径（SshEnv 按白名单校验）；上传只收工作区内
文件（绝对路径、..、符号链接逃逸一律拒）；拷回落点过 LocalEnv.resolve 校验。
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_ssh")

MACHINE_TOOLS = ("machine_run", "machine_put_file", "machine_read_file", "machine_fetch_file")

_RUN_TIMEOUT_MAX = 1800
_RUN_TIMEOUT_DEFAULT = 300
_READ_MAX = 20000
_FETCH_MAX_BYTES = 20 * 1024 * 1024
_LOCAL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def register_machine_tools(
    tools: Tools,
    *,
    get_box: Callable[[str], Any],
    env: Any,
    local_env: Any = None,
    tmp_dir: Path | None = None,
) -> None:
    """注册 machine_* 工具（逐个幂等）。get_box(task_id) -> SshBox | None。"""

    def _registered(name: str) -> bool:
        return name in {s["function"]["name"] for s in tools.specs("worker")}

    def _box(ctx: ToolContext) -> Any:
        try:
            return get_box(str(ctx.task_id or ""))
        except Exception:
            logger.exception("get_box 回调出错，当没机器处理")
            return None

    def _no_box() -> ToolResult:
        return ToolResult(ok=False, output="", error="这个任务没有分到专用机器（没配置、都连不上或都在忙）")

    async def machine_run(ctx: ToolContext, args: dict) -> ToolResult:
        box = _box(ctx)
        if box is None:
            return _no_box()
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 不能为空")
        try:
            timeout_s = int(args.get("timeout_s") or _RUN_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            timeout_s = _RUN_TIMEOUT_DEFAULT
        timeout_s = max(1, min(timeout_s, _RUN_TIMEOUT_MAX))
        try:
            r = await env.run(box, command, timeout_s=timeout_s)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"在专用机器上跑命令出错：{e}")
        head = f"退出码 {r.exit_code}"
        if r.timed_out:
            head += " · 超时被掐断"
        head += f" · 耗时 {r.ms / 1000:.1f} 秒"
        parts = [head, "--- 输出（stdout） ---", r.stdout or "（空）"]
        if r.stderr:
            parts += ["--- 错误输出（stderr） ---", r.stderr]
        return ToolResult(ok=True, output="\n".join(parts), data={
            "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr, "timed_out": r.timed_out,
        })

    def _workspace_file(ctx: ToolContext, rel: str) -> tuple[Path | None, ToolResult | None]:
        if ctx.workspace is None:
            return None, ToolResult(ok=False, output="", error="这次没带工作区，传不了文件")
        rel_str = str(rel or "").strip()
        cand = Path(rel_str)
        if not rel_str or cand.is_absolute():
            return None, ToolResult(ok=False, output="", error="只能传工作区里的文件（给相对路径）")
        parts = [p for p in cand.parts if p not in ("", ".")]
        if not parts or ".." in parts:
            return None, ToolResult(ok=False, output="", error="路径越界（不允许 ..）")
        ws = ctx.workspace.resolve()
        try:
            resolved = ws.joinpath(*parts).resolve()
        except OSError:
            return None, ToolResult(ok=False, output="", error=f"路径解析失败：{rel_str}")
        if resolved != ws and ws not in resolved.parents:
            return None, ToolResult(ok=False, output="", error="路径越界（符号链接指向工作区外）")
        if not resolved.is_file():
            return None, ToolResult(ok=False, output="", error=f"工作区内没有这个文件：{rel_str}")
        return resolved, None

    async def machine_put_file(ctx: ToolContext, args: dict) -> ToolResult:
        box = _box(ctx)
        if box is None:
            return _no_box()
        local, err = _workspace_file(ctx, str(args.get("path") or ""))
        if err is not None:
            return err
        remote = str(args.get("remote_path") or "").strip() or local.name  # type: ignore[union-attr]
        try:
            await env.put(box, local, remote)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"传到专用机器出错：{e}")
        return ToolResult(ok=True, output=f"已传到机器工作目录下的 {remote}", data={"remote": remote})

    async def machine_read_file(ctx: ToolContext, args: dict) -> ToolResult:
        box = _box(ctx)
        if box is None:
            return _no_box()
        remote = str(args.get("path") or "").strip()
        base = tmp_dir or Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        local = base / f"maiwork-machine-read-{id(box):x}-{Path(remote).name[:32] or 'f'}"
        try:
            try:
                await env.get(box, remote, local)
            except Exception as e:
                return ToolResult(ok=False, output="", error=f"从专用机器读文件出错：{e}")
            text = local.read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                local.unlink(missing_ok=True)
            except OSError:
                pass
        if len(text) > _READ_MAX:
            text = text[:_READ_MAX] + f"\n…（文件太长，截到前 {_READ_MAX} 字）"
        return ToolResult(ok=True, output=text or "（空文件）", data={"chars": len(text)})

    async def machine_fetch_file(ctx: ToolContext, args: dict) -> ToolResult:
        box = _box(ctx)
        if box is None:
            return _no_box()
        remote = str(args.get("remote_path") or "").strip()
        name = str(args.get("local_name") or "").strip() or Path(remote).name
        if not _LOCAL_NAME_RE.match(name):
            return ToolResult(ok=False, output="", error=f"local_name 不合法：{name!r}（字母数字 . _ -，不能带路径）")
        if local_env is None or ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次没带工作区，不能拷回文件")
        rel = f"artifacts/{ctx.task_id}/{name}"
        try:
            target = local_env.resolve(ctx.workspace.name, rel)
        except (PermissionError, ValueError) as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            await env.get(box, remote, target)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"从专用机器拷回文件出错：{e}")
        try:
            size = Path(target).stat().st_size
        except OSError:
            size = 0
        if size > _FETCH_MAX_BYTES:
            try:
                Path(target).unlink(missing_ok=True)
            except OSError:
                pass
            return ToolResult(ok=False, output="", error=f"文件太大（超过 20MB 上限），已删掉没收；请压缩或只取需要的部分")
        return ToolResult(ok=True, output=f"已拷回本机工作区 {rel}（{size} 字节）", data={"path": rel, "size": size})

    specs = [
        ("machine_run", machine_run,
         "在分给这个任务的专用机器（用户自己的 VPS / VM）上跑一条命令：装依赖、编译、跑程序都行。"
         "命令在这次的工作目录里执行，远端退出码原样回传；timeout_s 最多 1800 秒。",
         {"command": {"type": "string", "description": "要跑的命令（bash）"},
          "timeout_s": {"type": "integer", "description": "最多跑多少秒（上限 1800）"}},
         ["command"], float(_RUN_TIMEOUT_MAX) + 60.0),
        ("machine_put_file", machine_put_file,
         "把本机工作区里的一个文件传到专用机器的工作目录下。remote_path 是工作目录下的相对路径（不给就用原文件名）。",
         {"path": {"type": "string", "description": "本机工作区内的相对路径"},
          "remote_path": {"type": "string", "description": "机器工作目录下的相对路径（可选）"}},
         ["path"], 240.0),
        ("machine_read_file", machine_read_file,
         "读专用机器工作目录下的一个文件（相对路径；内容截 20000 字）。",
         {"path": {"type": "string", "description": "机器工作目录下的相对路径"}},
         ["path"], 240.0),
    ]
    if local_env is not None:
        specs.append((
            "machine_fetch_file", machine_fetch_file,
            "把专用机器工作目录下的一个文件拷回本机工作区 artifacts/<任务ID>/ 下。"
            "做好的成品最后一定要拷回来——只留在机器上是没法交付的（单文件 ≤20MB）。",
            {"remote_path": {"type": "string", "description": "机器工作目录下的相对路径"},
             "local_name": {"type": "string", "description": "拷回后叫什么名字（可选）"}},
            ["remote_path"], 240.0,
        ))
    for name, handler, desc, props, required, timeout in specs:
        if _registered(name):
            continue
        tools.register(Tool(
            name=name, description=desc,
            parameters={"type": "object", "properties": props, "required": required},
            roles=frozenset({"worker"}), handler=handler, timeout_s=timeout,
        ))
