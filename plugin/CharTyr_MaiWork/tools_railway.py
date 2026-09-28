"""railway.new 一次性 VM 上的子 agent 工具（worker 角色，docs/07-代码接口.md §11.1b）。

这些工具在「资讯实测」和「派活选了 railway」两种流程里用，拿到一次性 VM 之后才
有意义；box 通过闭包 `get_box()` 现读（box 变了/没了时不必重新注册）：

- vm_run：在 VM 里跑一条命令（远端退出码原样回传；timeout_s 上限 600 秒）。
- vm_put_file：把**当前工作区里的一个文件**传到 VM 的 /app/ 下
  （红线：绝不把工作区以外的任何本地文件/数据传上去——唯一的数据来源是
  子 agent 自己在工作区里写的测试脚本）。
- vm_read_file：读 VM 上 /app 下的一个文件（截 20000 字回给模型）。
- vm_fetch_file（给了 local_env 才注册，派活用）：把 VM 上的文件拷回本机工作区
  artifacts/<task_id>/ 下——成品必须拿回本机工作区才能交付。

安全要点（和 tools_exec 一套思路）：
- 没 box（拿不到机器 / 已 release）一律中文失败，不让模型瞎试。
- 传文件只收工作区内相对路径：绝对路径、..、符号链接逃出去一律拒。
- 远端文件名白名单（字母数字 ._-），远端路径固定在 /app/ 下，不许到别处写。
- vm_fetch_file 只收 /app/ 下的远端文件、单个 ≤20MB；落到 artifacts/<task_id>/<文件名>，
  目标路径过 LocalEnv.resolve 校验（越界一律拒）；落点文件名同样走白名单（不含路径符）。
- 到期护栏：build_expires_at 距现在 <10 分钟时，每个工具结果后面附「机器快到期了，
  赶紧把成品拿回来」；已经到点（剩余 ≤0）的工具一律失败，让模型收手。
- 注册幂等：同一份 Tools 上重复 register 不炸（app 重启/测试重复搭）；逐个工具判，
  已注册过的跳过、只补缺的（先注册 vm_*、后来补 vm_fetch_file 不冲突）。
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import clock
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_railway")

_VM_RUN_TIMEOUT_MAX = 600     # vm_run 的 timeout_s 上限（秒）
_VM_RUN_TIMEOUT_DEFAULT = 120
_VM_READ_MAX = 20000          # vm_read_file 回给模型最多多少字
_VM_FETCH_MAX_BYTES = 20 * 1024 * 1024  # vm_fetch_file 单文件上限 20MB
_EXPIRE_WARN_S = 600          # 剩 <10 分钟，就在每个工具结果后附「快到期」提醒
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOCAL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # 拷回本机用的文件名（不含路径符）


def register_vm_tools(
    tools: Tools,
    *,
    get_box: Callable[[], Any],
    env: Any,
    tmp_dir: Path | None = None,
    local_env: Any = None,
) -> None:
    """把 vm 工具注册进 tools（逐个幂等：已注册过同名的跳过，缺的补上）。

    - get_box() -> Box | None：实测流程现读当前 box（拿不到机器时是 None）。
    - env：RailwayEnv（或仿它的假对象，async run/put/get）。
    - tmp_dir：vm_read_file 取回文件的临时落点；None 用系统临时目录。
    - local_env：本机 LocalEnv，给 vm_fetch_file 用；没给就不注册 vm_fetch_file
      ——资讯实测那一挂不需要把成品拷回本机工作区。
    """

    def _registered(name: str) -> bool:
        existing = {s["function"]["name"] for s in tools.specs("worker")}
        return name in existing

    def _box() -> Any:
        try:
            return get_box()
        except Exception:
            logger.exception("get_box 回调出错，当没机器处理")
            return None

    def _no_box() -> ToolResult:
        return ToolResult(ok=False, output="", error="这轮没有分到一次性 VM（拿不到机器，或已经用完释放）")

    # ------------------------------------------------------------------
    # 到期护栏：快到期 → 每个工具结果后附提醒；已到点 → 直接失败
    # ------------------------------------------------------------------

    def _expiry_state(box: Any) -> tuple[str, float]:
        """返回 ('ok'|'warn'|'expired', 剩余秒)。box 没带 expires_ts 一律当 ok。"""
        try:
            expires = float(getattr(box, "expires_ts", 0) or 0)
        except (TypeError, ValueError):
            return "ok", 0.0
        if expires <= 0:
            return "ok", 0.0
        remaining = expires - clock.now()
        if remaining <= 0:
            return "expired", remaining
        if remaining < _EXPIRE_WARN_S:
            return "warn", remaining
        return "ok", remaining

    def _expired_result(remaining: float) -> ToolResult:
        return ToolResult(
            ok=False, output="",
            error="这台一次性机器到点了（已经过了 60 分钟构建窗口，不能再用）："
                  "别再往上放活了。已经在本机工作区的成品不受影响。",
        )

    def _with_expiry_warning(box: Any, result: ToolResult) -> ToolResult:
        """剩 <10 分钟时，在这个工具结果后面附一句「机器快到期了，赶紧把成品拿回来」。"""
        state, remaining = _expiry_state(box)
        if state != "warn" or not result.ok:
            return result
        mins = max(1, int(remaining // 60))
        note = (
            f"\n\n【提醒】这台一次性机器快到期了（大约还剩 {mins} 分钟）："
            "赶紧用 vm_fetch_file 把做好的成品拷回本机工作区，拿到本机工作区才算数，"
            "机器一到点上边的东西就全没了。"
        )
        return ToolResult(ok=result.ok, output=str(result.output) + note,
                          data=result.data, error=result.error)

    # ------------------------------------------------------------------
    # vm_run
    # ------------------------------------------------------------------

    async def vm_run(ctx: ToolContext, args: dict) -> ToolResult:
        del ctx
        box = _box()
        if box is None:
            return _no_box()
        state, remaining = _expiry_state(box)
        if state == "expired":
            return _expired_result(remaining)
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 不能为空")
        try:
            timeout_s = int(args.get("timeout_s") or _VM_RUN_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            timeout_s = _VM_RUN_TIMEOUT_DEFAULT
        timeout_s = max(1, min(timeout_s, _VM_RUN_TIMEOUT_MAX))  # 上限写死，模型说了不算
        try:
            result = await env.run(box, command, timeout_s=timeout_s)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"在 VM 上跑命令出错：{e}")
        head = f"退出码 {result.exit_code}"
        if result.timed_out:
            head += " · 超时被掐断（远端进程可能还在跑，可用 .exit 文件轮询）"
        head += f" · 耗时 {result.ms / 1000:.1f} 秒"
        parts = [head, "--- 输出（stdout） ---"]
        parts.append(result.stdout or "（空）")
        if result.stderr:
            parts.append("--- 错误输出（stderr） ---")
            parts.append(result.stderr)
        out = ToolResult(
            ok=True,
            output="\n".join(parts),
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
        )
        return _with_expiry_warning(box, out)

    # ------------------------------------------------------------------
    # vm_put_file
    # ------------------------------------------------------------------

    def _workspace_file(ctx: ToolContext, rel: str) -> tuple[Path | None, ToolResult | None]:
        """工作区内相对路径 → 真实文件（拒绝对路径、..、符号链接逃逸）。"""
        if ctx.workspace is None:
            return None, ToolResult(ok=False, output="", error="这次没带工作区，传不了文件")
        rel_str = str(rel or "").strip()
        if not rel_str:
            return None, ToolResult(ok=False, output="", error="path 不能为空")
        cand = Path(rel_str)
        if cand.is_absolute():
            return None, ToolResult(ok=False, output="", error="只能传工作区里的文件（给相对路径）")
        parts = [p for p in cand.parts if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            return None, ToolResult(ok=False, output="", error="路径越界（不允许 ..）")
        ws = ctx.workspace.resolve()
        target = ws
        for part in parts:
            target = target / part
        try:
            resolved = target.resolve()
        except OSError:
            return None, ToolResult(ok=False, output="", error=f"路径解析失败：{rel_str}")
        if resolved != ws and ws not in resolved.parents:
            return None, ToolResult(ok=False, output="", error="路径越界（符号链接指向工作区外）")
        if not resolved.is_file():
            return None, ToolResult(ok=False, output="", error=f"工作区内没有这个文件：{rel_str}")
        return resolved, None

    async def vm_put_file(ctx: ToolContext, args: dict) -> ToolResult:
        box = _box()
        if box is None:
            return _no_box()
        state, remaining = _expiry_state(box)
        if state == "expired":
            return _expired_result(remaining)
        local, err_result = _workspace_file(ctx, str(args.get("path") or ""))
        if err_result is not None:
            return err_result
        remote_name = str(args.get("remote_name") or "").strip() or local.name  # type: ignore[union-attr]
        if not _REMOTE_NAME_RE.match(remote_name):
            return ToolResult(
                ok=False, output="",
                error=f"remote_name 不合法：{remote_name!r}（只能用字母、数字、点、下划线、横线，字母或数字开头，1~64 个字符）",
            )
        remote = f"/app/{remote_name}"
        try:
            await env.put(box, local, remote)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"传到 VM 出错：{e}")
        out = ToolResult(ok=True, output=f"已传到 VM 的 {remote}（{local.name}）", data={"remote": remote})  # type: ignore[union-attr]
        return _with_expiry_warning(box, out)

    # ------------------------------------------------------------------
    # vm_read_file
    # ------------------------------------------------------------------

    async def vm_read_file(ctx: ToolContext, args: dict) -> ToolResult:
        del ctx
        box = _box()
        if box is None:
            return _no_box()
        state, remaining = _expiry_state(box)
        if state == "expired":
            return _expired_result(remaining)
        remote = str(args.get("path") or "").strip()
        if not remote.startswith("/app/"):
            return ToolResult(ok=False, output="", error="只能读 VM 上 /app/ 下的文件（实测的产出都在那里）")
        base = tmp_dir or Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        local = base / f"maiwork-vmread-{id(box):x}-{Path(remote).name[:32]}"
        try:
            try:
                await env.get(box, remote, local)
            except Exception as e:
                return ToolResult(ok=False, output="", error=f"从 VM 取回文件出错：{e}")
            try:
                text = local.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return ToolResult(ok=False, output="", error=f"读回的文件打不开：{e}")
        finally:
            try:
                local.unlink(missing_ok=True)
            except OSError:
                pass
        if len(text) > _VM_READ_MAX:
            text = text[:_VM_READ_MAX] + f"\n…（文件太长，截到前 {_VM_READ_MAX} 字）"
        out = ToolResult(ok=True, output=text or "（空文件）", data={"chars": len(text)})
        return _with_expiry_warning(box, out)

    # ------------------------------------------------------------------
    # vm_fetch_file（给了 local_env 才注册）：把 VM 上的成品文件拷回本机工作区
    #   artifacts/<task_id>/ 下——成品必须拿回本机工作区才能交付。
    # ------------------------------------------------------------------

    async def vm_fetch_file(ctx: ToolContext, args: dict) -> ToolResult:
        box = _box()
        if box is None:
            return _no_box()
        state, remaining = _expiry_state(box)
        if state == "expired":
            return _expired_result(remaining)
        # 远端只收 /app/ 下的文件（成品都在那里）
        remote = str(args.get("remote_path") or "").strip()
        if not remote.startswith("/app/"):
            return ToolResult(ok=False, output="", error="只能从 VM 的 /app/ 下拷回文件")
        # 落点文件名：白名单（不含路径符），拒 .. / 子目录
        name = str(args.get("local_name") or "").strip() or Path(remote).name
        if not _LOCAL_NAME_RE.match(name):
            return ToolResult(
                ok=False, output="",
                error=f"local_name 不合法：{name!r}（只能用字母、数字、点、下划线、横线，字母或数字开头，1~64 个字符；不能带路径）",
            )
        if local_env is None or ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次没带工作区，不能拷回文件")
        ws_name = ctx.workspace.name
        rel = f"artifacts/{ctx.task_id}/{name}"
        try:
            target = local_env.resolve(ws_name, rel)  # 越界（..、符号链接）在这里就拒
        except (PermissionError, ValueError) as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            await env.get(box, remote, target)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"从 VM 拷回文件出错：{e}")
        # 单文件 ≤20MB：拷回来发现太大就删掉不收
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        if size > _VM_FETCH_MAX_BYTES:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return ToolResult(
                ok=False, output="",
                error=f"文件太大（{size // (1024 * 1024)}MB，超过 20MB 上限），已在工作区删掉没收；"
                      "请压缩 / 只取需要的部分再 vm_fetch_file。",
            )
        out = ToolResult(
            ok=True, output=f"已拷回本机工作区 {rel}（{size} 字节）", data={"path": rel, "size": size},
        )
        return _with_expiry_warning(box, out)

    # ------------------------------------------------------------------
    # 注册（逐个幂等）
    # ------------------------------------------------------------------

    if not _registered("vm_run"):
        tools.register(
            Tool(
                name="vm_run",
                description=(
                    "在一次性 VM（railway.new）里跑一条命令：用来装依赖、跑示例、看输出。"
                    "远端退出码原样回传。只读/无副作用的验证优先；timeout_s 最多 600 秒。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要在 VM 里跑的命令（bash）"},
                        "timeout_s": {"type": "integer", "description": "这条命令最多跑多少秒（上限 600）"},
                    },
                    "required": ["command"],
                },
                roles=frozenset({"worker"}),
                handler=vm_run,
                timeout_s=float(_VM_RUN_TIMEOUT_MAX) + 60.0,  # 工具层兜底 > 内部 run 的上限
            )
        )
    if not _registered("vm_put_file"):
        tools.register(
            Tool(
                name="vm_put_file",
                description=(
                    "把当前工作区里的一个文件（比如你自己写的测试脚本）传到 VM 的 /app/ 下。"
                    "只能传工作区里的文件；remote_name 不给就用原文件名。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "工作区内的相对路径"},
                        "remote_name": {"type": "string", "description": "在 VM /app/ 下叫什么名字（可选）"},
                    },
                    "required": ["path"],
                },
                roles=frozenset({"worker"}),
                handler=vm_put_file,
                timeout_s=180.0,
            )
        )
    if not _registered("vm_read_file"):
        tools.register(
            Tool(
                name="vm_read_file",
                description="读 VM 上 /app/ 下的一个文件（内容截 20000 字）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "VM 上的绝对路径，必须 /app/ 开头"},
                    },
                    "required": ["path"],
                },
                roles=frozenset({"worker"}),
                handler=vm_read_file,
                timeout_s=180.0,
            )
        )
    if local_env is not None and not _registered("vm_fetch_file"):
        tools.register(
            Tool(
                name="vm_fetch_file",
                description=(
                    "把 VM 上 /app/ 下的一个文件拷回本机工作区 artifacts/<任务ID>/ 下。"
                    "做好的成品最后一定要用本工具拷回本机工作区——成品只留在 VM 上是没法交付的"
                    "（单文件 ≤20MB；local_name 不给就用远端文件名）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "remote_path": {"type": "string", "description": "VM 上的绝对路径，必须 /app/ 开头"},
                        "local_name": {"type": "string", "description": "拷回本机工作区 artifacts/<任务ID>/ 下叫什么名字（可选）"},
                    },
                    "required": ["remote_path"],
                },
                roles=frozenset({"worker"}),
                handler=vm_fetch_file,
                timeout_s=180.0,
            )
        )
