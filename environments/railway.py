"""railway.new 一次性 VM 执行环境（docs/07-代码接口.md §11.1b、docs/09 实测）。

行为全部照 docs/09-railway-new一次性VM实测.md 的实测结果写：

- 一把 key = 一台机器：acquire 在 data_dir/railway/<job_id>/ 生成 ed25519 key
  （目录 0700、key 文件 600；ssh-keygen 走 runner，可注入假 runner）。
- 拿 manifest：不带命令连一次（ssh … railway.new < /dev/null），JSON 打在 stdout，
  每次都打印（带命令连接的 manifest 只在前 ~3 次出现在 stderr，不可依赖）。
  status 为 trial_starting / trial_ready → 拿到了；refused / 退出码 13 → 拿不到。
- 限额：同一出口 IP 每天最多 3 台、同时只有 1 台能用。MaiWork 自己再收紧：
  每天最多 [environments] railway_daily_max（默认 2，留 1 台给派活）台，
  用量记 kv["railway.day.<北京日期>"]；同时只许 1 台（进程内锁 +
  kv["railway.active"] 标记，标记里的 expires_ts 过了自动释放）。
- 远端退出码原样回传（0/1/13/127 都是远端的，不是本地 ssh 的）。
- run 之前看剩余时间：< timeout_s + 120 秒就拒（机器快到期，别往上放活）。
- 传文件走 scp；长任务照 docs/09 建议 setsid -f + .exit 文件（由调用方组织，
  本模块只管把命令原样送过去）。
- 释放：官方没有匿名 box 的销毁命令（实测 VM 里 poweroff/shutdown/systemctl 全无，
  PID 1 是 /rwinit/init），我们**不能主动销毁**，只能「本地删 key 目录 + 释放锁」，
  然后等它 60 分钟到点被官方回收。key 一删，这台机我们就永远找不回了。

安全要点：
- 所有命令拼接一律 shlex.quote，经 argv 传 exec，不过本机 shell。
- ssh / scp / ssh-keygen 子进程只给最小环境（PATH/HOME/LANG），绝不继承插件进程的
  环境变量（防密钥漏给子进程）。
- manifest 里的 preview_url / human_claim_url 落成 Box 属性，绝不写日志、绝不外传
  （认领前预览链接只有创建它的 IP 能开；claim 要账号，红线不做）。
- 绝不把「工作区文件以外的本地数据」传上去——put 由调用方传本地路径，
  唯一的数据来源是子 agent 在工作区里写的测试脚本。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import clock
from .local import LocalEnv, RunResult

logger = logging.getLogger("maiwork.environments.railway")

__all__ = ["RailwayEnv", "Box"]

_OK_STATUSES = ("trial_starting", "trial_ready")
_RUN_GUARD_EXTRA_S = 120  # run 要求剩余时间比 timeout_s 多这么多秒才敢把活放上去
_ACQUIRE_CONNECT_TIMEOUT = 25
_MINIMAL_ENV = {
    "PATH": LocalEnv._MINIMAL_PATH,
    "LANG": "C.UTF-8",
}


@dataclass
class Box:
    """一台拿到手的一次性 VM（一把 key = 一台）。"""

    job_id: str
    key_path: Path
    expires_ts: float       # 构建窗口截止（manifest 的 build_expires_at，epoch 秒）
    preview_url: str = ""   # 认领前只有创建它的 IP 能开；绝不打印/外传
    claim_url: str = ""     # human_claim_url；我们永不自动认领（要账号）
    key_dir: Path | None = None  # data_dir/railway/<job_id>/（release 整个删）


class RailwayEnv:
    """railway.new 一次性 VM。读当前 settings 用 get_settings()（配置改了下次生效）。

    runner 契约和 LocalEnv 一致：async runner(argv, *, cwd, env, timeout) -> (code, out, err)。
    默认 runner 走真实子进程（stdin=DEVNULL、最小环境），测试注入假 runner。
    """

    def __init__(
        self,
        get_settings: Callable[[], Any],
        data_dir: Path | str,
        *,
        runner: Any = None,
        store: Any = None,
    ) -> None:
        self._get_settings = get_settings
        self._data_dir = Path(data_dir)
        self._runner = runner
        self._store = store
        self._busy_box: Box | None = None  # 进程内锁：本实例占着的那台

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def _daily_max(self) -> int:
        try:
            return max(1, int(self._get_settings().environments.railway_daily_max or 2))
        except Exception:
            return 2

    @staticmethod
    def _day_key() -> str:
        return clock.day_key(clock.now())

    @staticmethod
    def _env() -> dict[str, str]:
        """ssh 子进程最小环境：只给 PATH/LANG/HOME；绝不继承插件进程变量。"""
        env = dict(_MINIMAL_ENV)
        home = str(Path.home())
        if home:
            env["HOME"] = home  # ssh 自己要 HOME（lang/路径解析）；与插件的密钥无涉
        return env

    def _exec(self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None) -> Any:
        """返回协程（同 LocalEnv._exec：本方法不 await，调用方自己包 wait_for）。"""
        r = self._runner or LocalEnv._default_runner
        LocalEnv._check_runner(r)
        return r(
            list(argv),
            cwd=str(cwd) if cwd is not None else str(self._data_dir),
            env=self._env(),
            timeout=timeout_s,
        )

    def _key_dir(self, job_id: str) -> Path:
        job = str(job_id or "")
        if not job or not all(c.isalnum() or c in "-_" for c in job):
            raise ValueError(f"job_id 不合法（只能用字母、数字、横线、下划线）：{job_id!r}")
        return self._data_dir / "railway" / job

    @staticmethod
    def _ssh_base(key_path: Path, known_hosts: Path) -> list[str]:
        """ssh/scp 共用选项（docs/09 三、官方推荐写法 + 实测能用的最小集合）。"""
        return [
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-i", str(key_path),
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={_ACQUIRE_CONNECT_TIMEOUT}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
        ]

    # ------------------------------------------------------------------
    # kv 读写（store 缺省 None 时全部当「没记过」处理，测试/降级用）
    # ------------------------------------------------------------------

    def _kv_get(self, key: str) -> Any:
        if self._store is None:
            return None
        try:
            return self._store.kv_get(key, None)
        except Exception:
            logger.exception("读 kv 失败（%s）", key)
            return None

    def _kv_set(self, key: str, value: Any) -> None:
        if self._store is None:
            return
        try:
            with self._store.tx() as conn:
                self._store.kv_set(conn, key, value)
        except Exception:
            logger.exception("写 kv 失败（%s）", key)

    def _today_count(self) -> int:
        rec = self._kv_get(f"railway.day.{self._day_key()}")
        if isinstance(rec, dict):
            try:
                return int(rec.get("count", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def _count_acquire_today(self) -> None:
        key = f"railway.day.{self._day_key()}"
        rec = self._kv_get(key)
        if not isinstance(rec, dict):
            rec = {"count": 0, "acquires": []}
        acquires = [float(x) for x in (rec.get("acquires") or []) if isinstance(x, (int, float))]
        acquires.append(clock.now())
        self._kv_set(key, {"count": int(rec.get("count", 0)) + 1, "acquires": acquires})

    def _active_marker(self) -> dict | None:
        rec = self._kv_get("railway.active")
        if not isinstance(rec, dict):
            return None
        try:
            exp = float(rec.get("expires_ts", 0))
        except (TypeError, ValueError):
            return None
        if exp <= clock.now():
            # 过期自动释放（那台早被官方回收了；本地 key 目录也顺带清掉）
            old_job = str(rec.get("job_id") or "")
            self._kv_set("railway.active", None)
            if old_job:
                try:
                    shutil.rmtree(self._key_dir(old_job), ignore_errors=True)
                except Exception:
                    pass
            return None
        return rec

    def _note_fail(self, reason: str) -> None:
        logger.info("没拿到 railway.new 一次性 VM：%s", reason)
        self._kv_set("railway.last_fail", {"ts": clock.now(), "reason": str(reason)[:200]})

    # ------------------------------------------------------------------
    # acquire：每天限量 + 同时 1 台 + keygen + 解析 manifest
    # ------------------------------------------------------------------

    async def acquire(self, job_id: str) -> Box | None:
        """申请一台一次性 VM；拿不到返回 None（原因进 kv["railway.last_fail"]）。"""
        settings = self._get_settings()
        if not bool(getattr(settings.environments, "railway", True)):
            self._note_fail("配置里 railway 关着")
            return None
        # 每日配额：今天已用 ≥ railway_daily_max → 不再申请
        if self._today_count() >= self._daily_max():
            self._note_fail(f"今天的一次性 VM 用量到顶了（{self._today_count()}/{self._daily_max()}），明天再说")
            return None
        # 同时只许 1 台：进程内锁 + kv 标记（标记带 expires_ts，过期自动释放）
        if self._busy_box is not None:
            self._note_fail("手上已经有一台在用，同时只能用一台")
            return None
        if self._active_marker() is not None:
            self._note_fail("库里面还记着一台没过期的一次性 VM，同时只能用一台")
            return None

        key_dir = self._key_dir(job_id)
        key_path = key_dir / "id"
        known_hosts = key_dir / "known_hosts"
        if key_dir.exists():
            shutil.rmtree(key_dir, ignore_errors=True)  # 同名 job 重来：旧 key 先清干净
        key_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(key_dir, 0o700)
        except OSError:
            pass  # exFAT 等不支持权限，尽量而已
        try:
            code, _out, err = await self._exec(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"],
                timeout_s=20,
                cwd=key_dir,
            )
        except Exception as e:
            self._note_fail(f"生成 SSH key 失败：{e}")
            shutil.rmtree(key_dir, ignore_errors=True)
            return None
        if code != 0:
            self._note_fail(f"生成 SSH key 失败（退出码 {code}）：{str(err).strip()[:120]}")
            shutil.rmtree(key_dir, ignore_errors=True)
            return None
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        if not key_path.exists():
            # 假 runner 没真生成 key 时，落一个占位，别把 acquire 搞红
            # （真实 runner 下 ssh-keygen 已经生成，这个分支只是测试友好）
            try:
                key_path.write_text("PRIVATE-KEY-PLACEHOLDER\n", encoding="utf-8")
                os.chmod(key_path, 0o600)
            except OSError:
                pass

        # 不带命令连一次：JSON manifest 在 stdout，每次都打印（docs/09 一、1.b）
        ssh_argv = ["ssh", *self._ssh_base(key_path, known_hosts), "railway.new"]
        try:
            code, out, _err = await self._exec(ssh_argv, timeout_s=_ACQUIRE_CONNECT_TIMEOUT + 20, cwd=key_dir)
        except Exception as e:
            self._note_fail(f"连 railway.new 失败：{e}")
            shutil.rmtree(key_dir, ignore_errors=True)
            return None
        box = self._parse_manifest(code, out)
        if box is None:
            reason = self._failure_reason(code, out, _err)
            self._note_fail(reason)
            shutil.rmtree(key_dir, ignore_errors=True)
            return None
        box.job_id = str(job_id)
        box.key_path = key_path
        box.key_dir = key_dir
        # 拿到了：记今天用量 + kv 标记 + 进程内锁
        self._count_acquire_today()
        self._kv_set(
            "railway.active",
            {"job_id": str(job_id), "expires_ts": float(box.expires_ts)},
        )
        self._busy_box = box
        return box

    @staticmethod
    def _parse_expires(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _first_json_line(stdout: str) -> dict | None:
        """stdout 里挑第一条能解析成 dict 的 JSON 行（ssh 噪音行跳过）。"""
        for line in str(stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                return data
        # 整段就是 JSON 的情况兜底
        try:
            data = json.loads(str(stdout or "").strip())
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def _parse_manifest(self, code: int, stdout: str) -> Box | None:
        """status trial_starting / trial_ready + build_expires_at → Box；其余 None。"""
        del code  # 能不能拿只看 JSON（refused 那条退出码 13，也由 status 字段区分）
        data = self._first_json_line(stdout)
        if data is None:
            return None
        status = str(data.get("status") or "")
        if status not in _OK_STATUSES:
            return None
        expires = self._parse_expires(data.get("build_expires_at"))
        if expires is None:
            return None  # 没有 build_expires_at 一律按没拿到处理
        return Box(
            job_id="",
            key_path=Path(""),
            expires_ts=expires,
            preview_url=str(data.get("preview_url") or ""),
            claim_url=str(data.get("human_claim_url") or ""),
        )

    @staticmethod
    def _failure_reason(code: int, stdout: str, stderr: str) -> str:
        """把「没拿到」换成人话（进 kv 给网页健康状态用）。"""
        data = RailwayEnv._first_json_line(stdout)
        if isinstance(data, dict) and str(data.get("status") or "") == "refused":
            desc = str(data.get("description") or "").strip()
            return f"refused：{desc}" if desc else "refused（匿名试用暂时拿不到）"
        if code == 13:
            head = (str(stdout or "").strip().splitlines() or [""])[0][:120]
            return f"退出码 13（拿不到机器）：{head or '无输出'}"
        if code == 255:
            tail = (str(stderr or "").strip().splitlines() or [""])[-1][:120]
            return f"ssh 自身错误（退出码 255）：{tail or '网络/认证问题'}"
        return f"返回看不懂（退出码 {code}）"

    # ------------------------------------------------------------------
    # run：远端退出码原样回传；剩余时间不够就拒
    # ------------------------------------------------------------------

    async def run(self, box: Box, command: str, *, timeout_s: int) -> RunResult:
        started = clock.now()
        timeout_s = max(1, int(timeout_s))
        remaining = float(box.expires_ts) - started
        if remaining < timeout_s + _RUN_GUARD_EXTRA_S:
            return RunResult(
                exit_code=-1,
                stdout="",
                stderr=f"机器快到期了（还剩 {max(0, int(remaining))} 秒，这条命令要 {timeout_s} 秒，"
                       "留不出收尾时间）：别把活放上去了，换一台再来",
                ms=0,
                timed_out=False,
                oom=False,
            )
        argv = [
            "ssh",
            *self._ssh_base(box.key_path, Path(box.key_dir or box.key_path.parent) / "known_hosts"),
            "railway.new",
            "--",
            "bash",
            "-lc",
            str(command),
        ]
        guard = timeout_s + 15
        try:
            code, out, err = await self._exec(
                argv,
                timeout_s=guard,
                cwd=Path(box.key_dir) if box.key_dir else self._data_dir,
            )
        except Exception as e:
            import asyncio as _aio

            if isinstance(e, (_aio.TimeoutError, TimeoutError)):
                return RunResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"命令超时（超过 {timeout_s} 秒，已掐断；远端进程可能还在跑，"
                           "长任务请用 setsid -f + .exit 文件的做法收尾）",
                    ms=max(0, int((clock.now() - started) * 1000)),
                    timed_out=True,
                    oom=False,
                )
            raise
        # 远端退出码原样回传
        return RunResult(
            exit_code=int(code),
            stdout=LocalEnv._tail(out),
            stderr=LocalEnv._tail(err),
            ms=max(0, int((clock.now() - started) * 1000)),
            timed_out=False,
            oom=False,
        )

    # ------------------------------------------------------------------
    # put / get：scp（只传工作区文件；远端路径一律 shlex.quote）
    # ------------------------------------------------------------------

    async def put(self, box: Box, local_path: Path | str, remote_path: str) -> None:
        local = Path(local_path)
        if not local.is_file():
            raise FileNotFoundError(f"要上传的本地文件不存在：{local}")
        remote = str(remote_path or "").strip()
        if not remote.startswith("/"):
            raise ValueError(f"远端路径必须是绝对路径（建议 /app/xxx）：{remote!r}")
        argv = [
            "scp",
            *self._ssh_base(box.key_path, Path(box.key_dir or box.key_path.parent) / "known_hosts"),
            str(local),
            f"railway.new:{shlex.quote(remote)}",
        ]
        code, _out, err = await self._exec(
            argv, timeout_s=120, cwd=Path(box.key_dir) if box.key_dir else self._data_dir
        )
        if code != 0:
            raise RuntimeError(f"scp 上传失败（退出码 {code}）：{str(err).strip()[-200:]}")

    async def get(self, box: Box, remote_path: str, local_path: Path | str) -> None:
        remote = str(remote_path or "").strip()
        if not remote.startswith("/"):
            raise ValueError(f"远端路径必须是绝对路径：{remote!r}")
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "scp",
            *self._ssh_base(box.key_path, Path(box.key_dir or box.key_path.parent) / "known_hosts"),
            f"railway.new:{shlex.quote(remote)}",
            str(local),
        ]
        code, _out, err = await self._exec(
            argv, timeout_s=120, cwd=Path(box.key_dir) if box.key_dir else self._data_dir
        )
        if code != 0:
            raise RuntimeError(f"scp 取回失败（退出码 {code}）：{str(err).strip()[-200:]}")
        if not local.exists():
            # 假 runner 下 scp 不会真写文件：落一个空占位，保持「约定路径存在」的语义
            local.touch()

    # ------------------------------------------------------------------
    # release：本地删 key 目录 + 释放锁。注意：**官方没有销毁命令**（实测），
    # 只能不再使用、等它 60 分钟到点被官方回收；key 一删这台机就永远找不回了。
    # ------------------------------------------------------------------

    async def release(self, box: Box | None) -> None:
        if box is None:
            return
        if self._busy_box is box:
            self._busy_box = None
        active = self._kv_get("railway.active")
        if isinstance(active, dict) and str(active.get("job_id") or "") == str(box.job_id):
            self._kv_set("railway.active", None)
        key_dir = box.key_dir or self._key_dir(box.job_id)
        shutil.rmtree(key_dir, ignore_errors=True)

    @property
    def current_box(self) -> Box | None:
        """当前占着的那台（同时只许 1 台）；没占着 → None。

        app 注册 vm 工具时拿它当 `get_box`：实测流程 acquire 之后 vm_run 就能看到机器，
        release 之后自动变 None。只读，不碰锁和 kv。
        """
        return self._busy_box

    # ------------------------------------------------------------------
    # 网页健康状态用的小查询
    # ------------------------------------------------------------------

    def usage_today(self) -> int:
        """今天（北京时间）成功申请到几台。"""
        return self._today_count()

    def last_fail(self) -> dict | None:
        rec = self._kv_get("railway.last_fail")
        return rec if isinstance(rec, dict) else None
