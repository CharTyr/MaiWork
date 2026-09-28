"""专用 SSH 机器执行环境（用户自己的 VPS / VM；配置 [environments] ssh）。

怎么连：
- 插件自己生成一把 ed25519 key：<data_dir>/ssh/id_ed25519（目录 0700、私钥 600）。
  公钥在网页「运行状态」里给用户，用户把它加进那台机器的 ~/.ssh/authorized_keys。
  插件不读用户的 ~/.ssh，也不需要密码。
- 主机指纹记在 <data_dir>/ssh/known_hosts（第一次连 accept-new 记住，之后变了就连不上）。
- ssh/scp 一律 BatchMode=yes（不交互）、IdentitiesOnly=yes、连接 15 秒超时。

怎么用：
- acquire(job_id, prefer)：主模型点名的那台先试，然后按配置顺序挑第一台「连得上、这会儿没活」的机器（一台机器同时只接
  一个任务），在上面建 ~/maiwork/<job_id>/ 当这次的工作目录；拿不到返回 None，
  原因进 last_fail()。
- run(box, 命令)：在工作目录里 `bash -lc` 跑（远端有 timeout 就用它限时），远端退出码原样回传。
- put / get：只收工作目录下的相对路径，字符白名单（字母数字 . _ - /），
  不许绝对路径、不许 ..——所以远端路径不用引号也不会被解释成别的东西（scp 新旧协议都安全）。
- release(box)：只释放「忙」标记；远端目录留着（那是用户自己的机器，成品已拷回本机工作区）。

安全要点：
- host 写法严格校验（user@地址:端口），不许 - 开头、不许空白和 shell 字符（防参数注入）。
- 子进程只给最小环境（PATH/LANG/HOME），绝不继承插件进程的环境变量。
- 这台机器上的隔离由用户自己负责（它就是给 MaiWork 专用的机器）；网页和 README 写明。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import clock
from .local import LocalEnv, RunResult

logger = logging.getLogger("maiwork.environments.ssh")

__all__ = ["SshEnv", "SshBox", "parse_host"]

_CONNECT_TIMEOUT = 15
_USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*|\[[0-9A-Fa-f:]+\])$")
_JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,127}){0,15}$")
_ABS_SAFE_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,400}$")
_MINIMAL_ENV = {"PATH": LocalEnv._MINIMAL_PATH, "LANG": "C.UTF-8"}


def parse_host(raw: Any) -> tuple[str, str, int]:
    """「user@地址:端口」→ (user, host, port)；user 可省（用本机默认用户），端口默认 22。

    不合法一律 ValueError（中文）：- 开头、空白、shell 字符、端口越界……
    """
    text = str(raw or "").strip()
    if not text or text.startswith("-") or any(c.isspace() for c in text):
        raise ValueError(f"机器地址写法不对：{raw!r}（应为 user@地址 或 user@地址:端口）")
    user = ""
    rest = text
    if "@" in text:
        user, rest = text.split("@", 1)
        if not _USER_RE.match(user):
            raise ValueError(f"用户名不合法：{user!r}")
    port = 22
    if rest.startswith("["):
        host, _, tail = rest.partition("]")
        host += "]"
        if tail:
            if not tail.startswith(":"):
                raise ValueError(f"机器地址写法不对：{raw!r}")
            port_s = tail[1:]
        else:
            port_s = ""
    else:
        if rest.count(":") > 1:
            raise ValueError(f"机器地址写法不对：{raw!r}（端口只能写一个）")
        host, _, port_s = rest.partition(":")
    if port_s:
        if not port_s.isdigit():
            raise ValueError(f"端口不对：{port_s!r}")
        port = int(port_s)
        if not 1 <= port <= 65535:
            raise ValueError(f"端口超出范围：{port}")
    if not host or host.startswith("-") or not _HOST_RE.match(host):
        raise ValueError(f"机器地址不合法：{host!r}")
    return user, host, port


@dataclass
class SshBox:
    """这次任务分到的一台专用机器。"""

    job_id: str
    name: str
    dest: str          # user@host（或 host）
    port: int
    workdir: str       # 远端绝对路径 ~/maiwork/<job_id>（已 pwd -P 解开）
    kind: str = "ssh"
    expires_ts: float = 0.0  # 专用机器没有到期；vm 工具的到期护栏按 0 当「不到期」


class SshEnv:
    """用户自己的 VPS / VM。runner 契约同 LocalEnv：async runner(argv, *, cwd, env, timeout)。"""

    def __init__(self, get_settings: Callable[[], Any], data_dir: Path | str, *, runner: Any = None) -> None:
        self._get_settings = get_settings
        self._data_dir = Path(data_dir)
        self._runner = runner
        self._busy: dict[str, str] = {}      # 机器名 -> 占着它的 job_id
        self._status: dict[str, dict] = {}   # 机器名 -> {"ok", "error", "ts"}
        self._last_fail: dict | None = None
        self._boxes: dict[str, SshBox] = {}  # job_id -> 分到的机器（工具按任务现读）

    # ------------------------------------------------------------------
    # 配置 / 小工具
    # ------------------------------------------------------------------

    def machines(self) -> list[dict]:
        """配置里的机器（顺序即优先级）；地址写错的带 error，不参与挑选。"""
        out: list[dict] = []
        try:
            items = list(getattr(self._get_settings().environments, "ssh", ()) or ())
        except Exception:
            items = []
        for it in items:
            name = str(getattr(it, "name", "") or (it.get("name") if isinstance(it, dict) else "") or "")
            host = str(getattr(it, "host", "") or (it.get("host") if isinstance(it, dict) else "") or "")
            note = str(getattr(it, "note", "") or (it.get("note") if isinstance(it, dict) else "") or "")
            entry: dict[str, Any] = {"name": name or host, "host": host, "note": note}
            try:
                user, h, port = parse_host(host)
                entry.update(dest=f"{user}@{h}" if user else h, port=port)
            except ValueError as e:
                entry["error"] = str(e)
            out.append(entry)
        return out

    def available(self) -> bool:
        return any("error" not in m for m in self.machines())

    @property
    def _key_dir(self) -> Path:
        return self._data_dir / "ssh"

    @property
    def key_path(self) -> Path:
        return self._key_dir / "id_ed25519"

    @staticmethod
    def _env() -> dict[str, str]:
        env = dict(_MINIMAL_ENV)
        home = str(Path.home())
        if home:
            env["HOME"] = home
        return env

    def _exec(self, argv: Sequence[str], *, timeout_s: float) -> Any:
        r = self._runner or LocalEnv._default_runner
        LocalEnv._check_runner(r)
        self._key_dir.mkdir(parents=True, exist_ok=True)
        return r(list(argv), cwd=str(self._key_dir), env=self._env(), timeout=timeout_s)

    def _opts(self) -> list[str]:
        return [
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-i", str(self.key_path),
            "-o", f"UserKnownHostsFile={self._key_dir / 'known_hosts'}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
        ]

    def _ssh_argv(self, dest: str, port: int, remote_cmd: str) -> list[str]:
        return ["ssh", *self._opts(), "-p", str(port), dest, remote_cmd]

    # ------------------------------------------------------------------
    # key
    # ------------------------------------------------------------------

    async def ensure_key(self) -> str:
        """没有 key 就生成一把；返回公钥一行。"""
        pub = self.key_path.with_suffix(".pub")
        if not (self.key_path.exists() and pub.exists()):
            self._key_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self._key_dir, 0o700)
            except OSError:
                pass
            code, _out, err = await self._exec(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "maiwork", "-f", str(self.key_path)],
                timeout_s=30,
            )
            if code != 0:
                raise RuntimeError(f"生成 SSH key 失败：{str(err).strip()[-200:]}")
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return self.public_key()

    def public_key(self) -> str:
        try:
            return self.key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # 连通检查
    # ------------------------------------------------------------------

    @staticmethod
    def _explain(code: int, err: str) -> str:
        e = str(err or "").strip()
        low = e.lower()
        if "permission denied" in low:
            return "登录被拒：把网页上的 MaiWork 公钥加进这台机器的 ~/.ssh/authorized_keys"
        if "host key verification failed" in low or "remote host identification has changed" in low:
            return "主机指纹变了（机器重装过？），为安全起见不连；确认无误后删掉数据目录 ssh/known_hosts 里那一行"
        if "timed out" in low or "no route" in low or "refused" in low or "could not resolve" in low:
            return f"连不上：{e[-120:]}"
        return f"连不上（退出码 {code}）：{e[-120:] or '没有输出'}"

    async def _probe(self, m: dict) -> tuple[bool, str]:
        try:
            code, out, err = await self._exec(
                self._ssh_argv(m["dest"], m["port"], "echo ok"), timeout_s=_CONNECT_TIMEOUT + 10
            )
        except Exception as e:  # 超时 / 起不了 ssh
            return False, f"连不上：{type(e).__name__}"
        if code == 0 and "ok" in str(out):
            return True, ""
        return False, self._explain(code, err)

    def _record(self, name: str, ok: bool, error: str) -> None:
        self._status[name] = {"ok": ok, "error": error, "ts": clock.now()}

    async def check_all(self) -> list[dict]:
        """把每台机器连一次（网页健康项用）；没 key 先生成。"""
        try:
            await self.ensure_key()
        except Exception:
            logger.exception("生成 SSH key 出错")
        for m in self.machines():
            if "error" in m:
                self._record(m["name"], False, m["error"])
                continue
            ok, error = await self._probe(m)
            self._record(m["name"], ok, error)
        return self.status()

    def status(self) -> list[dict]:
        out = []
        for m in self.machines():
            st = self._status.get(m["name"]) or {}
            out.append({
                "name": m["name"], "host": m["host"],
                "ok": bool(st.get("ok")) if st else None,
                "error": m.get("error") or str(st.get("error") or ""),
                "ts": st.get("ts"),
                "busy": m["name"] in self._busy,
            })
        return out

    def last_fail(self) -> dict | None:
        return self._last_fail

    # ------------------------------------------------------------------
    # acquire / release
    # ------------------------------------------------------------------

    async def acquire(self, job_id: str, prefer: str = "") -> SshBox | None:
        job = str(job_id or "")
        if not _JOB_RE.match(job):
            raise ValueError(f"job_id 不合法（只能用字母、数字、横线、下划线）：{job_id!r}")
        machines = self.machines()
        # 主模型点了名的那台先试（按 AGENTS.md 里写的用途挑的），不行再按配置顺序换别的
        pref = str(prefer or "").strip()
        if pref:
            machines.sort(key=lambda m: 0 if m["name"] == pref else 1)
        if not machines:
            self._last_fail = {"ts": clock.now(), "reason": "没有配置专用机器"}
            return None
        try:
            await self.ensure_key()
        except Exception as e:
            self._last_fail = {"ts": clock.now(), "reason": f"生成 SSH key 失败：{e}"}
            return None
        reasons: list[str] = []
        for m in machines:
            name = m["name"]
            if "error" in m:
                reasons.append(f"{name}：{m['error']}")
                continue
            if name in self._busy:
                reasons.append(f"{name}：正在做别的任务")
                continue
            self._busy[name] = job  # 先占上，别让并发的另一个任务同时挑中
            try:
                code, out, err = await self._exec(
                    self._ssh_argv(m["dest"], m["port"], f"mkdir -p \"$HOME/maiwork/{job}\" && cd \"$HOME/maiwork/{job}\" && pwd -P"),
                    timeout_s=_CONNECT_TIMEOUT + 15,
                )
            except Exception as e:
                code, out, err = -1, "", f"{type(e).__name__}"
            if code != 0:
                self._busy.pop(name, None)
                why = self._explain(code, err)
                self._record(name, False, why)
                reasons.append(f"{name}：{why}")
                continue
            workdir = (str(out).strip().splitlines() or [""])[-1].strip()
            if not _ABS_SAFE_RE.match(workdir) or ".." in workdir.split("/"):
                self._busy.pop(name, None)
                reasons.append(f"{name}：远端工作目录路径里有特殊字符（{workdir[:60]!r}），为安全起见不用")
                continue
            self._record(name, True, "")
            logger.info("任务 %s 分到专用机器 %s", job, name)
            box = SshBox(job_id=job, name=name, dest=m["dest"], port=int(m["port"]), workdir=workdir)
            self._boxes[job] = box
            return box
        self._last_fail = {"ts": clock.now(), "reason": "；".join(reasons)[:300]}
        logger.info("没拿到专用机器（任务 %s）：%s", job, self._last_fail["reason"])
        return None

    async def release(self, box: SshBox | None) -> None:
        if box is None:
            return
        if self._busy.get(box.name) == box.job_id:
            self._busy.pop(box.name, None)
        if self._boxes.get(box.job_id) is box:
            self._boxes.pop(box.job_id, None)

    def box_for(self, job_id: str) -> SshBox | None:
        """这个任务现在占着的机器（没有 → None）；machine_* 工具靠它找机器。"""
        return self._boxes.get(str(job_id or ""))

    # ------------------------------------------------------------------
    # run / put / get
    # ------------------------------------------------------------------

    def _remote_path(self, box: SshBox, rel: str) -> str:
        rel_s = str(rel or "").strip()
        if not _REL_RE.match(rel_s) or any(p in ("..", ".") for p in rel_s.split("/")):
            raise ValueError(
                f"路径不合法：{rel!r}（只能是这次工作目录下的相对路径，字母数字和 . _ - /，不许 ..）"
            )
        return f"{box.workdir}/{rel_s}"

    async def run(self, box: SshBox, command: str, *, timeout_s: int) -> RunResult:
        started = clock.now()
        t = max(1, int(timeout_s))
        inner = shlex.quote(str(command))
        remote = (
            f"cd {box.workdir} && "
            f"if command -v timeout >/dev/null 2>&1; then timeout -k 5 {t} bash -lc {inner}; "
            f"else bash -lc {inner}; fi"
        )
        try:
            code, out, err = await self._exec(self._ssh_argv(box.dest, box.port, remote), timeout_s=t + 30)
        except Exception as e:
            import asyncio as _aio

            if isinstance(e, (_aio.TimeoutError, TimeoutError)):
                return RunResult(exit_code=-1, stdout="", stderr=f"命令超时（超过 {t} 秒，已掐断）",
                                 ms=max(0, int((clock.now() - started) * 1000)), timed_out=True, oom=False)
            raise
        timed_out = int(code) == 124
        return RunResult(
            exit_code=int(code), stdout=LocalEnv._tail(out), stderr=LocalEnv._tail(err),
            ms=max(0, int((clock.now() - started) * 1000)), timed_out=timed_out, oom=False,
        )

    async def put(self, box: SshBox, local_path: Path | str, rel: str) -> None:
        local = Path(local_path)
        remote = self._remote_path(box, rel)
        if not local.is_file():
            raise FileNotFoundError(f"要上传的本地文件不存在：{local}")
        parent = remote.rsplit("/", 1)[0]
        code, _o, err = await self._exec(self._ssh_argv(box.dest, box.port, f"mkdir -p {parent}"), timeout_s=40)
        if code != 0:
            raise RuntimeError(f"在机器上建目录失败：{str(err).strip()[-200:]}")
        argv = ["scp", *self._opts(), "-P", str(box.port), str(local), f"{box.dest}:{remote}"]
        code, _o, err = await self._exec(argv, timeout_s=180)
        if code != 0:
            raise RuntimeError(f"上传失败（退出码 {code}）：{str(err).strip()[-200:]}")

    async def get(self, box: SshBox, rel: str, local_path: Path | str) -> None:
        remote = self._remote_path(box, rel)
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        argv = ["scp", *self._opts(), "-P", str(box.port), f"{box.dest}:{remote}", str(local)]
        code, _o, err = await self._exec(argv, timeout_s=180)
        if code != 0:
            raise RuntimeError(f"取回失败（退出码 {code}）：{str(err).strip()[-200:]}")
        if not local.exists():
            local.touch()  # 假 runner 下 scp 不真写文件：保持「约定路径存在」
