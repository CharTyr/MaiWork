"""本机执行环境（docs/07-代码接口.md §11.1）。

两种模式（config [environments] local_mode）：
- systemd（线上）：命令用 systemd-run 以独立用户 maiwork 跑，带内存（MemoryMax）、
  时长（RuntimeMaxSec）上限；工作区建好 chown 给 maiwork；maiwork 进不了 /root，
  碰不到 MaiBot 的任何数据。本机没 systemd，线上行为只能保证「argv 拼对」。
  环境变量用 --setenv 给最小集（PATH 最小集、HOME=工作区、LANG=C.UTF-8）——
  线上实测（2026-09-27）：不设的话单元里 HOME 是 /home/maiwork（ProtectHome 只读），
  子 agent 往 ~ 写东西会直接炸。
- direct（只给本机开发测试）：直接开子进程，不隔离。环境变量也只给最小集合
  （PATH、HOME=工作区、LANG=C.UTF-8），绝不继承插件进程的环境（防密钥漏给子进程）。

后台进程：
- systemd：systemd-run（--collect，不带 --wait --pipe）--unit=maiwork-<工作区>-<标签>，
  输出重定向到工作区 runtime/logs/<标签>.log，进程收尾把自己的退出码写进
  runtime/logs/<标签>.exit（单元被 --collect 收掉后 systemctl 里查不到真实退出码，
  线上实测 ExecMainStatus 恒 0）；status 看 systemctl show 的 ActiveState，
  不活跃时退出码读 <标签>.exit；stop 用 systemctl stop（幂等，没该单元也不报错）。
- direct：子进程登记在内存表里（同一 LocalEnv 实例内），日志和 <标签>.exit 同样落在
  runtime/logs 下；登记里查不到的单元（比如插件重启过）也从 <标签>.exit 读退出码，
  语义与 systemd 模式一致，给测试用。

防御要点（红线：子 agent 只能写自己的工作区）：
- 工作区名只允许 [A-Za-z0-9_-]{1,64}；
- resolve() 拒绝对路径、拒绝 ..、realpath 之后必须仍在工作区内（防符号链接逃逸）；
- 命令、子目录名一律拼进 argv 传 exec，不经过本机 shell 二次解释；
- run() 在 RuntimeMaxSec 之外再用 asyncio.wait_for(+15s) 兜底，超时要 kill
  systemd-run 进程并 systemctl stop 兜底单元，所以 run 也带 --unit=maiwork-run-<随机>。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import pwd
import re
import secrets
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .. import clock

logger = logging.getLogger("maiwork.environments.local")

__all__ = ["LocalEnv", "RunResult", "Runner", "RunOutcome"]

_MAX_OUTPUT_CHARS = 20_000          # stdout / stderr 各截 20000 字（保留末尾）
_MAX_FILE_BYTES = 5 * 1024 * 1024   # 工作区单文件上限 5MB
_READ_DEFAULT_BYTES = 200_000       # read_file 默认最多读多少字节
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_WS_SUBDIRS = ("tasks", "artifacts", "tools", "runtime")
_OOM_WORDS = ("oom", "memory")
_SYSTEMD_TRAILER_PREFIXES = ("Running as unit:", "Main processes terminated", "Service runtime:", "CPU time consumed:", "Memory peak:", "IP traffic", "IO bytes")      # stderr 出现其一（小写）就推断为内存超限

RunOutcome = tuple[int, str, str]  # (exit_code, stdout, stderr)
Runner = Callable[[Sequence[str]], Awaitable[RunOutcome]]  # 示意；真实签名见 _exec


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    ms: int
    timed_out: bool
    oom: bool


class LocalEnv:
    """本机执行环境。读当前 settings 用 get_settings()（配置改了下次调用生效）。"""

    def __init__(self, get_settings: Callable[[], Any], *, runner: Any = None) -> None:
        self._get_settings = get_settings
        self._runner = runner
        # direct 模式的后台进程登记：unit -> {"label","proc","log","exit_code"}
        self._procs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def _mode(self) -> str:
        env = self._get_settings().environments
        mode = env.local_mode if env.local_mode in ("systemd", "direct") else "systemd"
        if env.local_mode not in ("systemd", "direct"):
            logger.warning("local_mode=%r 不认识，按 systemd 处理", env.local_mode)
        return mode

    def _run_as(self) -> str:
        return str(self._get_settings().environments.run_as or "maiwork")

    def _memory(self) -> str:
        return str(self._get_settings().environments.memory_max or "512M")

    def _runtime_max(self) -> int:
        return max(1, int(self._get_settings().environments.runtime_max_sec))

    def _default_command_timeout(self) -> int:
        """不传 timeout_s 时的默认命令上限：command_timeout_s，同时不超过 runtime_max_sec。"""
        env = self._get_settings().environments
        return max(1, min(int(env.command_timeout_s), self._runtime_max()))

    _MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

    @staticmethod
    def _minimal_env(ws: Path) -> dict[str, str]:
        """子进程只给最小环境；绝不传插件进程的环境（密钥不落子进程）。"""
        return {
            "PATH": LocalEnv._MINIMAL_PATH,
            "HOME": str(ws),
            "LANG": "C.UTF-8",
        }

    def _systemd_setenv(self, ws: Path) -> list[str]:
        """--setenv 把最小环境直接给单元（线上实测：不设的话单元里 HOME 是
        只读的 /home/maiwork 而不是工作区，子 agent 往 ~ 写东西直接炸）。"""
        return [f"--setenv={k}={v}" for k, v in self._minimal_env(ws).items()]

    @staticmethod
    def _chown_tree(path: Path, uid: int, gid: int) -> None:
        """chown 单个路径；目录则连内容一起。只动工作区内的东西。"""
        try:
            os.chown(path, uid, gid)
        except PermissionError:
            logger.warning("chown %s 失败（非属主？），跳过", path)
            return
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    try:
                        os.chown(os.path.join(root, d), uid, gid)
                    except PermissionError:
                        continue
                for f in files:
                    try:
                        os.chown(os.path.join(root, f), uid, gid)
                    except PermissionError:
                        continue

    def _chown_for_run_as(self, path: Path) -> None:
        """仅 systemd 模式且当前是 root 时 chown 给 run_as 用户（本机非 root 直接跳过）。"""
        if self._mode() != "systemd" or os.geteuid() != 0:
            return
        try:
            pw = pwd.getpwnam(self._run_as())
        except KeyError:
            logger.warning("系统里没有用户 %s，chown 跳过", self._run_as())
            return
        self._chown_tree(path, pw.pw_uid, pw.pw_gid)

    # ------------------------------------------------------------------
    # workspace / resolve
    # ------------------------------------------------------------------

    def workspace(self, name: str) -> Path:
        """工作区目录，没有就建；systemd 模式且 root 时 chown 给 run_as 用户。

        name 只允许 [A-Za-z0-9_-]{1,64}，别的抛 ValueError（中文）。
        """
        name = str(name or "")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"工作区名不合法：{name!r}（只能用字母、数字、下划线、横线，1~64 个字符）"
            )
        root = Path(self._get_settings().environments.workspace_root)
        ws = root / name
        ws.mkdir(parents=True, exist_ok=True)
        for sub in _WS_SUBDIRS:
            (ws / sub).mkdir(parents=True, exist_ok=True)
        self._chown_for_run_as(ws)
        return ws.resolve()

    def resolve(self, name: str, rel: str) -> Path:
        """工作区内相对路径 → 绝对路径。越界（绝对路径、..、符号链接指出去）→ PermissionError。"""
        ws = self.workspace(name)
        rel_str = str(rel or "")
        cand = Path(rel_str)
        if cand.is_absolute():
            raise PermissionError(f"路径必须是工作区内的相对路径：{rel_str!r}")
        parts = [p for p in cand.parts if p not in ("", ".")]
        if not parts:
            return ws
        # 前缀逐级拼并逐段 realpath：已存在的符号链接段在这一步就会被解出来
        current = ws
        for i, part in enumerate(parts):
            if part == "..":
                raise PermissionError(f"路径越界（不允许 ..）：{rel_str!r}")
            current = current / part
            if i < len(parts) - 1 or current.exists():
                resolved = current.resolve()
                if resolved != ws and ws not in resolved.parents:
                    raise PermissionError(f"路径越界（符号链接指向工作区外）：{rel_str!r}")
                current = resolved
        return current

    # ------------------------------------------------------------------
    # runner：唯一真正起进程的地方
    # ------------------------------------------------------------------

    @staticmethod
    async def _default_runner(argv: Sequence[str], *, cwd: Any, env: Any, timeout: float) -> RunOutcome:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise
        out = (out_b or b"").decode("utf-8", errors="replace")
        err = (err_b or b"").decode("utf-8", errors="replace")
        return proc.returncode if proc.returncode is not None else -1, out, err

    @staticmethod
    def _check_runner(r: Any) -> None:
        """runner 契约：async runner(argv, *, cwd, env, timeout) -> (code, out, err)。

        签名不对当场 ValueError（fail fast：注入错了要在测试里立刻红）。
        """
        if not inspect.iscoroutinefunction(r):
            raise ValueError("runner 必须是协程函数：async runner(argv, *, cwd, env, timeout)")
        params = inspect.signature(r).parameters
        for keyw in ("cwd", "env", "timeout"):
            if keyw not in params:
                raise ValueError(f"runner 签名缺关键参数 {keyw}（应为 async runner(argv, *, cwd, env, timeout)）")

    def _exec(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        runner: Any = None,
    ) -> Any:
        """校验 runner 契约并返回协程（本方法不做 await，方便调用方包 wait_for）。"""
        r = runner if runner is not None else (self._runner or self._default_runner)
        self._check_runner(r)
        return r(list(argv), cwd=cwd, env=dict(env), timeout=timeout_s)

    @staticmethod
    def _tail(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
        return text if len(text) <= limit else text[-limit:]

    @staticmethod
    def _split_systemd_trailer(err: str) -> tuple[str, str]:
        """去掉 systemd-run（不带 --quiet）写到 stderr 的状态行，返回 (干净的 stderr, 结果)。

        线上实测（2026-09-27，systemd 257）：超内存 → 「Finished with result: oom-kill」、
        超时 → 「Finished with result: timeout」，两者 systemd-run 退出码都是 1（不是 137）；
        命令自己失败 → 「exit-code」，退出码透传。所以只能靠这一行判断。
        """
        result = ""
        keep: list[str] = []
        for line in err.splitlines():
            s = line.strip()
            if s.startswith("Finished with result:"):
                result = s.split(":", 1)[1].strip()
                continue
            if s.startswith(_SYSTEMD_TRAILER_PREFIXES):
                continue
            keep.append(line)
        return "\n".join(keep), result

    @staticmethod
    def _make_result(code: int, out: str, err: str, started: float, timed_out: bool, *, systemd_result: str = "") -> RunResult:
        ms = max(0, int((clock.now() - started) * 1000))
        if systemd_result:
            oom = systemd_result == "oom-kill"
            timed_out = timed_out or systemd_result == "timeout"
        else:
            # direct 模式只能推断：退出码 137 或 stderr 里有 oom/memory 字样
            err_l = err.lower()
            oom = code == 137 or any(w in err_l for w in _OOM_WORDS)
        return RunResult(
            exit_code=code,
            stdout=LocalEnv._tail(out),
            stderr=LocalEnv._tail(err),
            ms=ms,
            timed_out=timed_out,
            oom=oom,
        )

    # ------------------------------------------------------------------
    # run：跑一条命令，等它结束
    # ------------------------------------------------------------------

    async def run(
        self,
        name: str,
        command: str,
        *,
        timeout_s: int | None = None,
        memory: str | None = None,
        cwd: str = "",
    ) -> RunResult:
        ws = self.workspace(name)
        cmd = str(command)
        if cwd:
            cwd_abs = self.resolve(name, cwd)
            if not cwd_abs.is_dir():
                raise FileNotFoundError(f"工作区内没有目录：{cwd!r}")
            # M12：cwd 虽已限定在工作区内，但目录名可以带空格/特殊字符——quote 兜底
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        limit = (
            self._default_command_timeout()
            if timeout_s is None
            else max(1, min(int(timeout_s), self._runtime_max()))
        )
        guard = limit + 15  # 外层兜底：比 RuntimeMaxSec 多 15 秒，宁可多等不可漏杀
        started = clock.now()

        if self._mode() == "systemd":
            unit = f"maiwork-run-{secrets.token_hex(4)}"
            run_as = self._run_as()
            argv = [
                "systemd-run",
                "--wait", "--collect", "--pipe",  # 不加 --quiet：要靠「Finished with result」判断超内存/超时
                f"--unit={unit}",
                f"--uid={run_as}", f"--gid={run_as}",
                *self._systemd_setenv(ws),  # 单元里 HOME=工作区、LANG=C.UTF-8、最小 PATH
                "-p", f"MemoryMax={memory or self._memory()}",
                "-p", "MemorySwapMax=0",
                "-p", f"RuntimeMaxSec={limit}",
                *self._hardening(ws),
                "-p", f"WorkingDirectory={ws}",
                "--", "/bin/bash", "-lc", cmd,
            ]
            try:
                code, out, err = await asyncio.wait_for(
                    self._exec(argv, cwd=ws, env=self._minimal_env(ws), timeout_s=guard),
                    timeout=guard,
                )
            except asyncio.TimeoutError:
                # systemd-run 本身被 wait_for 掐了（runner 里已 kill 进程）；
                # 单元可能还活着，兜底停掉（后台任务，不等）。
                logger.warning("命令兜底超时，停止单元 %s", unit)
                asyncio.get_running_loop().create_task(self._systemctl_stop(unit, ws))
                return self._make_result(-1, "", f"命令超时（超过 {limit} 秒，已强制停止）", started, True)
            clean_err, result = self._split_systemd_trailer(err)
            return self._make_result(code, out, clean_err, started, False, systemd_result=result)

        # direct：不经过 systemd，直接开子进程（本机测试用）
        argv_d = ["/bin/bash", "-lc", cmd]
        try:
            code, out, err = await self._exec(argv_d, cwd=ws, env=self._minimal_env(ws), timeout_s=limit)
        except asyncio.TimeoutError:
            return self._make_result(-1, "", f"命令超时（超过 {limit} 秒，已强制停止）", started, True)
        return self._make_result(code, out, err, started, False)

    def _hardening(self, ws: Path) -> list[str]:
        """隔离：整个系统只读、家目录只读、/tmp 私有、不能提权；
        工作区根被换成空的临时目录，只把本工作区挂回来——看不到别的群的工作区。
        线上实测（2026-09-27）：别的工作区「No such file」、/home/maiwork 只读、本工作区可写、能出网。"""
        root = Path(ws).parent
        return [
            "-p", "ProtectSystem=strict",
            "-p", "ProtectHome=read-only",
            "-p", "PrivateTmp=yes",
            "-p", "NoNewPrivileges=yes",
            "-p", f"TemporaryFileSystem={root}",
            "-p", f"BindPaths={ws}",
        ]

    async def _systemctl_stop(self, unit: str, ws: Path) -> None:
        try:
            await self._exec(["systemctl", "stop", unit], cwd=ws, env=self._minimal_env(ws), timeout_s=15)
        except Exception:
            logger.exception("systemctl stop %s 失败", unit)

    # ------------------------------------------------------------------
    # start / status / logs / stop：后台进程
    # ------------------------------------------------------------------

    @staticmethod
    def _check_label(label: str) -> str:
        label = str(label or "")
        if not _LABEL_RE.match(label):
            raise ValueError("标签只能用字母、数字、下划线、横线（不允许空格和斜线）")
        return label

    async def start(self, name: str, command: str, *, label: str, timeout_s: int) -> str:
        """后台进程，返回单元名 maiwork-<工作区>-<标签>。

        日志落 runtime/logs/<标签>.log；进程收尾把真实退出码写进 runtime/logs/<标签>.exit
        （systemd 模式起的是 --collect 单元，跑完被回收，systemctl 里查不到退出码——
        线上实测 ExecMainStatus 恒 0；status 只能从 .exit 文件拿真退出码）。
        """
        label = self._check_label(label)
        ws = self.workspace(name)
        unit = f"maiwork-{name}-{label}"
        log_dir = ws / "runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._chown_for_run_as(log_dir)
        log_path = log_dir / f"{label}.log"
        exit_path = log_dir / f"{label}.exit"
        # 同标签的旧 .exit 先抹掉：新进程还没跑完前，status 不该读到上一轮的退出码
        try:
            exit_path.unlink(missing_ok=True)
        except OSError:
            pass
        limit = min(max(1, int(timeout_s)), self._runtime_max())
        # M12：ws / log_path / exit_path 拼进 bash -lc 前一律 quote（根目录走配置，可以带空格）
        inner = (
            f"cd {shlex.quote(str(ws))} && ( {command} ) >> {shlex.quote(str(log_path))} 2>&1; "
            f"rc=$?; echo $rc > {shlex.quote(str(exit_path))}; exit $rc"
        )

        if self._mode() == "systemd":
            run_as = self._run_as()
            argv = [
                "systemd-run",
                "--collect", "--quiet",
                f"--unit={unit}",
                f"--uid={run_as}", f"--gid={run_as}",
                *self._systemd_setenv(ws),  # 单元里 HOME=工作区、LANG=C.UTF-8、最小 PATH
                "-p", f"MemoryMax={self._memory()}",
                "-p", "MemorySwapMax=0",
                "-p", f"RuntimeMaxSec={limit}",
                *self._hardening(ws),
                "-p", f"WorkingDirectory={ws}",
                "--", "/bin/bash", "-lc", inner,
            ]
            await self._exec(argv, cwd=ws, env=self._minimal_env(ws), timeout_s=20)
            return unit

        # direct：同名标签已存在就先杀干净的旧的再起新的（避免两个进程同时写一份日志）
        old = self._procs.get(unit)
        if old is not None:
            await self._kill_entry(old)
        log_fh = open(log_path, "a", encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", inner,
            cwd=str(ws),
            env=self._minimal_env(ws),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log_fh,
            stderr=asyncio.subprocess.STDOUT,
        )
        timer = asyncio.get_running_loop().call_later(limit, self._kill_proc_quietly, proc)
        self._procs[unit] = {"label": label, "proc": proc, "log": log_path, "fh": log_fh, "timer": timer}
        return unit

    @staticmethod
    def _kill_proc_quietly(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def status(self, unit: str) -> dict:
        """{"active": bool, "exit_code": int|None}

        活跃时 exit_code 一律 None；不活跃时读 runtime/logs/<标签>.exit——进程收尾时
        自己写的真实退出码（systemd 的 --collect 单元跑完被回收，ExecMainStatus 查不到；
        读不到 .exit 就 None，不猜）。direct 模式登记在册的进程优先用内存里的 returncode，
        登记查不到（插件重启/close 后）同样落回 .exit 文件，两种模式语义一致。
        """
        unit = str(unit or "")
        if self._mode() == "systemd":
            ws = Path(self._get_settings().environments.workspace_root)
            argv = ["systemctl", "show", "-p", "ActiveState", unit]
            try:
                code, out, _err = await self._exec(argv, cwd=ws, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, timeout_s=10)
            except Exception:
                return {"active": False, "exit_code": self._read_exit_code(unit)}
            if code != 0:
                return {"active": False, "exit_code": self._read_exit_code(unit)}
            fields: dict[str, str] = {}
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    fields[k.strip()] = v.strip()
            if not fields:
                return {"active": False, "exit_code": self._read_exit_code(unit)}
            if fields.get("ActiveState") == "active":
                return {"active": True, "exit_code": None}
            return {"active": False, "exit_code": self._read_exit_code(unit)}

        info = self._procs.get(unit)
        if info is not None:
            proc: asyncio.subprocess.Process = info["proc"]
            if proc.returncode is None:
                return {"active": True, "exit_code": None}
            return {"active": False, "exit_code": int(proc.returncode)}
        return {"active": False, "exit_code": self._read_exit_code(unit)}

    def _unit_candidates(self, unit: str) -> list[tuple[Path, str]]:
        """单元名 maiwork-<工作区>-<标签> → [(工作区目录, 标签)]。

        工作区名自己允许带 '-'，光从单元名拆不开；只能拿 workspace_root 下真实存在的
        目录去对前缀（长名优先，「a」和「a-b」都沾边时先吃下歧义少的那个）。
        """
        if not unit.startswith("maiwork-"):
            return []
        rest = unit[len("maiwork-"):]
        root = Path(self._get_settings().environments.workspace_root)
        try:
            dirs = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            return []
        out: list[tuple[Path, str]] = []
        for d in sorted(dirs, key=lambda p: len(p.name), reverse=True):
            prefix = d.name + "-"
            if not rest.startswith(prefix):
                continue
            label = rest[len(prefix):]
            if _LABEL_RE.match(label):  # 标签字符集白名单，顺带挡掉 ".."
                out.append((d, label))
        return out

    def _read_exit_code(self, unit: str) -> int | None:
        """不活跃单元的真实退出码：读 runtime/logs/<标签>.exit；读不到/读不懂返回 None。"""
        for ws, label in self._unit_candidates(unit):
            exit_path = ws / "runtime" / "logs" / f"{label}.exit"
            try:
                text = exit_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            try:
                return int(text)
            except ValueError:
                continue
        return None

    async def logs(self, name: str, unit: str, *, tail: int = 200) -> str:
        """后台进程日志的最后 tail 行；没日志文件返回空串。"""
        ws = self.workspace(name)
        label = ""
        prefix = f"maiwork-{name}-"
        if unit.startswith(prefix):
            label = unit[len(prefix):]
        elif self._mode() == "direct" and unit in self._procs:
            label = str(self._procs[unit]["label"])
        if not label or "/" in label or "\\" in label or label in (".", ".."):
            return ""
        log_path = ws / "runtime" / "logs" / f"{label}.log"
        base = (ws / "runtime" / "logs").resolve()
        if log_path.resolve().parent != base:  # 防御：绝不读 logs 目录外的东西
            return ""
        if not log_path.exists():
            return ""
        data = log_path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        tail = max(1, int(tail))
        return "\n".join(lines[-tail:])

    async def stop(self, unit: str) -> None:
        """停止后台进程。没这个单元/进程不报错（systemctl stop 对未知 unit 幂等）。"""
        unit = str(unit or "")
        if self._mode() == "systemd":
            ws = Path(self._get_settings().environments.workspace_root)
            try:
                await self._exec(["systemctl", "stop", unit], cwd=ws, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, timeout_s=20)
            except Exception:
                logger.warning("systemctl stop %s 失败（可能本就不存在）", unit)
            return
        info = self._procs.get(unit)
        if info is None:
            return
        await self._kill_entry(info)

    async def close(self) -> None:
        """M4：收摊子——杀掉所有 direct 后台进程、关日志句柄、取消 kill 定时器。

        插件停 / 热重载时由 app._stop_stack 调用；不做的话配置热重载会留孤儿进程
        （线上是 systemd 模式，_procs 本来就是空的，调了也没事）。幂等。
        """
        entries = list(self._procs.items())
        self._procs = {}
        for unit, info in entries:
            try:
                await self._kill_entry(info)
            except Exception:
                logger.exception("收尾后台进程失败（%s）", unit)

    async def _kill_entry(self, info: dict) -> None:
        """杀掉登记的后台进程并收尾（定时器取消、日志句柄关闭）。

        SIGKILL 后子进程要经事件循环收尸才算真死；连着 kill 的话（比如同标签
        重复 start）给一小段收尸时间，避免旧进程残留和日志写到一半被截。
        """
        proc: asyncio.subprocess.Process = info["proc"]
        self._kill_proc_quietly(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        timer = info.get("timer")
        if timer is not None:
            timer.cancel()
        fh = info.get("fh")
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 文件读写：插件（root）直接读写工作区，写完 systemd 模式 chown
    #
    # TOCTOU（G2）：resolve 与 open 之间文件可能被换成符号链接。防线：
    # 1. 父目录各段逐段 lstat——父目录链上任何一段是符号链接 → PermissionError；
    # 2. os.open 带 O_NOFOLLOW 打开最后一段（末段是链接 → OSError(ELOOP) → PermissionError）；
    # 3. 打开后校验 fd 指回工作区内：Linux 用 /proc/self/fd/<fd> 的 realpath；
    #    其他平台回落「重新 realpath 父目录 + fstat 与 os.stat 的 st_dev/st_ino 对比」。
    # ------------------------------------------------------------------

    @staticmethod
    def _nofollow_flag() -> int:
        return getattr(os, "O_NOFOLLOW", 0)

    def _check_parents_no_symlink(self, ws: Path, path: Path) -> None:
        """path 相对工作区根的父目录链，逐段 lstat；任何一段是符号链接 → PermissionError。"""
        try:
            rel = path.relative_to(ws)
        except ValueError:
            raise PermissionError(f"路径不在工作区内：{path}") from None
        current = ws
        for part in rel.parts[:-1]:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                # 还不存在的目录段（写新文件时是常态）——存在的才需要查
                break
            if stat.S_ISLNK(st.st_mode):
                raise PermissionError(f"父目录段是符号链接，不允许：{current}")

    @staticmethod
    def _open_nofollow(path: Path, flags: int) -> int:
        """os.open 带 O_NOFOLLOW；末段是链接 → PermissionError（ELOOP 等一律按越界拒）。"""
        try:
            return os.open(str(path), flags | LocalEnv._nofollow_flag(), 0o644)
        except PermissionError:
            raise PermissionError(f"不允许访问（符号链接不允许读写）：{path}") from None
        except FileNotFoundError:
            raise
        except OSError:
            raise PermissionError(f"不允许访问（符号链接不允许读写）：{path}") from None

    def _verify_fd_in_workspace(self, fd: int, path: Path, ws: Path) -> None:
        """打开之后再校验一次 fd 指向的对象仍在工作区内（挡 resolve→open 之间的替换）。"""
        procfd = f"/proc/self/fd/{fd}"
        if os.path.isdir("/proc/self/fd"):
            # Linux：直接看 fd 指去哪
            real = os.path.realpath(procfd)
            ws_real = os.path.realpath(str(ws))
            if real != ws_real and not real.startswith(ws_real + os.sep):
                raise PermissionError(f"打开的文件不在工作区内（fd 指向 {real}）")
            return
        # 回落：fstat 和重新 stat 路径比对 st_dev/st_ino，再确认 realpath 在区内
        try:
            fst = os.fstat(fd)
            pst = os.stat(path)
        except OSError:
            raise PermissionError(f"打开的文件校验失败：{path}") from None
        if (fst.st_dev, fst.st_ino) != (pst.st_dev, pst.st_ino):
            raise PermissionError(f"打开期间文件被替换：{path}")
        ws_real = os.path.realpath(str(ws))
        parent_real = os.path.realpath(str(path.parent))
        if parent_real != ws_real and not parent_real.startswith(ws_real + os.sep):
            raise PermissionError(f"打开的文件不在工作区内：{path}")

    async def read_file(self, name: str, rel: str, *, max_bytes: int = _READ_DEFAULT_BYTES) -> str:
        path = self.resolve(name, rel)
        ws = Path(self.workspace(name))
        if not path.exists():
            raise FileNotFoundError(f"工作区内没有这个文件：{rel!r}")
        if path.is_dir():
            raise IsADirectoryError(f"{rel!r} 是目录，不是文件")
        self._check_parents_no_symlink(ws, path)
        max_bytes = max(1, int(max_bytes))
        fd = self._open_nofollow(path, os.O_RDONLY)
        try:
            self._verify_fd_in_workspace(fd, path, ws)
            with os.fdopen(fd, "rb") as f:
                data = f.read(max_bytes + 1)
        finally:
            # fdopen 成功会接管 fd；只有校验阶段抛错时才仍归这里管
            try:
                os.close(fd)
            except OSError:
                pass
        if len(data) > max_bytes:
            data = data[:max_bytes]
        return data.decode("utf-8", errors="replace")

    async def write_file(self, name: str, rel: str, content: str, *, append: bool = False) -> None:
        data = str(content).encode("utf-8")
        path = self.resolve(name, rel)
        ws = Path(self.workspace(name))
        if path.is_dir():
            raise IsADirectoryError(f"{rel!r} 已存在且是目录")
        parent = path.parent
        parent_existed = parent.exists()
        if not parent_existed:
            parent.mkdir(parents=True, exist_ok=True)
        self._check_parents_no_symlink(ws, path)
        old_total = 0
        if append and path.exists():
            old_total = path.stat().st_size
        if old_total + len(data) > _MAX_FILE_BYTES:
            raise ValueError(
                f"单个文件不能超过 {_MAX_FILE_BYTES // (1024 * 1024)}MB：{rel!r} 写入后会超限"
            )
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        fd = self._open_nofollow(path, flags)
        try:
            self._verify_fd_in_workspace(fd, path, ws)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        # systemd 模式且 root 时 chown 给 run_as：新建的父目录链从工作区往下补一次
        self._chown_for_run_as(self.workspace(name) if not parent_existed else path)

    async def list_files(self, name: str, rel: str = "", *, depth: int = 2, limit: int = 200) -> list[dict]:
        """列出工作区（或子目录 rel）下的条目：[{path, size, is_dir}]，path 相对工作区。"""
        ws = self.workspace(name)
        base = self.resolve(name, rel)
        if not base.exists():
            raise FileNotFoundError(f"工作区内没有这个目录：{rel!r}")
        if not base.is_dir():
            raise IsADirectoryError(f"{rel!r} 是文件，不是目录")
        depth = max(0, int(depth))
        limit = max(1, int(limit))
        entries: list[dict] = []

        def walk(d: Path, level: int) -> None:
            if level >= depth or len(entries) >= limit:
                return
            try:
                children = sorted(d.iterdir(), key=lambda p: p.name)
            except PermissionError:
                return
            for child in children:
                if len(entries) >= limit:
                    return
                is_dir = child.is_dir()
                try:
                    size = 0 if is_dir else child.stat().st_size
                except OSError:
                    size = 0
                entries.append(
                    {
                        "path": str(child.relative_to(ws)),
                        "size": int(size),
                        "is_dir": bool(is_dir),
                    }
                )
                if is_dir:
                    walk(child, level + 1)

        walk(base, 0)
        return entries
