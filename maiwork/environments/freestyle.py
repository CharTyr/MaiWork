"""Freestyle 一次性 VM 执行环境（2026-10-15，docs/02 §9、docs/07 §11.1d）。

事实来源：官方文档 https://www.freestyle.sh/docs/llms.txt 与 OpenAPI
（https://api.freestyle.sh/openapi.json，openapi 3.1.0）。**没有用真密钥实测过**：
所有端点行为都是规范推导，落地后必须用真 key 走一遍
acquire → run → put → get → delete 并把结果补进文档。

和 railway.py 的关系：对外操作逐字同形（acquire / run / put / get / release /
current_box / usage_today / last_fail），所以 coordinator 只按 `box.kind` 查表分派。
差别有四处：
- 要**用户自己的 API key**（Bearer；环境变量 FREESTYLE_API_KEY 优先，其次 config.toml）；
- **能主动销毁**（DELETE /v5/vms/{id}），不用等平台回收；
- 传文件走**文件系统 API**（fs/write、fs/read + Range），不再用 scp；
- 单条命令**平台硬上限 5 分钟**（无 background/native-exec 能力时），本模块夹到 280 秒，
  超长命令要么由调用方拆短、要么用 `setsid -f … ; echo $? > .exit` + 短 exec 轮询。

安全要点（和 railway 一套思路）：
- 密钥只进不出：不写日志、不进 kv、不进远端命令、不传给子进程；
- 远端根目录 `/root/maiwork`（默认 ubuntu 镜像用 `linuxUser: "root"` 跑，免得提权）；
- 传上去的只能是调用方给的本地文件（工作区里的脚本），本模块不自己找数据；
- 出网规则固定 `{destination: {public: true}}`（装依赖必需）；「绝不把工作区文件以外的
  本地数据传上去」这条红线由 tools_railway.py 的路径校验保证，本模块不放松它。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import clock
from . import vm_platforms
from .local import RunResult

logger = logging.getLogger("maiwork.environments.freestyle")

__all__ = ["FreestyleEnv", "FreestyleBox"]

_BASE_URL = "https://api.freestyle.sh"
_REMOTE_ROOT = "/root/maiwork"        # 远端固定根（和 railway 的 /app 一个意思）
_LINUX_USER = "root"                  # 默认镜像的 ubuntu 用户要 sudo；root 直接写
_SNAPSHOT_FALLBACK = "freestyle/ubuntu-sm"
_EXEC_TIMEOUT_MAX_MS = 280_000        # 平台硬上限 300000ms，留 20 秒余量
_HTTP_TIMEOUT_S = 30.0
_RUN_GUARD_EXTRA_S = 120              # run 要求剩余时间比 timeout_s 多这么多秒才敢放活
_FS_WRITE_MAX_BYTES = 8 * 1024 * 1024  # 超过就走分片上传（官方 SDK 也是 8MiB 切）
_FS_UPLOAD_CHUNK = 8 * 1024 * 1024
_FS_READ_CHUNK = 4 * 1024 * 1024       # 大文件分块读（带 Range，可续传）
_POLL_INTERVAL_S = 2.0
_POLL_TIMEOUT_S = 120.0
_RUNNING = "running"
_DEAD_STATES = ("stopped",)           # 建出来就到这儿 = 没成

# 按 code 分支（规范原文：message 会改，code 稳定）
_AUTH_CODES = frozenset({"MISSING_CREDENTIALS", "UNAUTHORIZED", "INVALID_API_KEY", "FORBIDDEN", "INVALID_TOKEN"})
_QUOTA_CODES = frozenset({"RATE_LIMITED", "TOO_MANY_REQUESTS", "QUOTA_EXCEEDED", "LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"})
_NOT_FOUND_CODES = frozenset({"NOT_FOUND", "VM_NOT_FOUND"})


@dataclass
class FreestyleBox:
    """一台拿到手的 Freestyle VM（同时只许 1 台）。"""

    job_id: str
    vm_id: str
    kind: str = "freestyle"
    expires_ts: float = 0.0          # createdAt + ttlSeconds（平台按创建时刻算，不是空闲窗口）
    remote_root: str = _REMOTE_ROOT
    slug: str = ""
    state: str = ""
    snapshot: str = ""
    cpu: int = 0
    memory_mib: int = 0
    created_ts: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_ts(value: Any) -> float | None:
    """ISO8601（可带 Z）→ epoch 秒；认不出 → None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _error_reason(status: int, code: str, message: str) -> str:
    """HTTP 错误换成人话（进 kv 给网页健康状态用）；按 code 分支，不照抄 message 当结论。

    只回中文结论：服务商的原始 code / message 是英文实现细节（还可能是英文报错原文），
    **不拼进给网页看的文字**，只写日志（2026-10-15 用户定）。
    """
    code_s = str(code or "").strip()
    msg = str(message or "").strip()[:200]
    if code_s or msg:
        logger.info("Freestyle 返回错误详情（HTTP %s）：%s %s", status, code_s, msg)
    if status in (401, 403) or code_s in _AUTH_CODES:
        return "Freestyle 密钥不正确或未填写（请到 https://dash.freestyle.sh 创建 API key 后填入设置）"
    if status == 429 or code_s in _QUOTA_CODES:
        return "Freestyle 当前无法提供机器（配额或限流已达上限）"
    if status == 404 or code_s in _NOT_FOUND_CODES:
        return "Freestyle 上找不到这台机器（可能已被回收）"
    if status == 409 or code_s == "CONFLICT":
        return "Freestyle 返回名称冲突（409）"
    return f"Freestyle 返回错误（HTTP {status}）"


class FreestyleEnv:
    """Freestyle 一次性 VM（https://api.freestyle.sh/v5）。

    读当前 settings 用 get_settings()（配置改了下次生效）。transport 是给测试用的
    httpx.MockTransport；sleep / poll_interval_s / poll_timeout_s 也是测试注入点
    （默认走 asyncio.sleep 与 2 秒 / 120 秒）。
    """

    def __init__(
        self,
        get_settings: Callable[[], Any],
        data_dir: Path | str,
        *,
        store: Any = None,
        transport: Any = None,
        sleep: Callable[[float], Any] | None = None,
        poll_interval_s: float = _POLL_INTERVAL_S,
        poll_timeout_s: float = _POLL_TIMEOUT_S,
    ) -> None:
        self._get_settings = get_settings
        self._data_dir = Path(data_dir)
        self._store = store
        self._transport = transport
        self._sleep = sleep or asyncio.sleep
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._poll_timeout_s = max(0.0, float(poll_timeout_s))
        self._client: Any = None
        self._busy_box: FreestyleBox | None = None

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    @property
    def max_run_timeout_s(self) -> int:
        """单条命令的实际上限（秒）。tools_railway 拿它夹 timeout_s，避免假能力。"""
        return _EXEC_TIMEOUT_MAX_MS // 1000

    def _client_or_new(self) -> Any:
        if self._client is None:
            import httpx

            kwargs: dict[str, Any] = {"base_url": _BASE_URL, "timeout": httpx.Timeout(_HTTP_TIMEOUT_S)}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.debug("关 Freestyle HTTP 客户端出错", exc_info=True)

    def _api_key(self, settings: Any = None) -> str:
        """密钥：环境变量优先，其次 config.toml。"" 表示没有；值绝不打印。"""
        s = settings if settings is not None else self._get_settings()
        return vm_platforms.api_key_of("freestyle", s)

    def _platform_selected(self, settings: Any) -> bool:
        """Freestyle 开关开着（2026-10-15 起和 Railway 各自开关，不再二选一）。"""
        try:
            preset = vm_platforms.get("freestyle")
            return bool(preset is not None and preset.enabled(settings))
        except Exception:
            return False

    def _daily_max(self) -> int:
        try:
            return max(1, int(self._get_settings().environments.freestyle_daily_max or 2))
        except Exception:
            return 2

    def _ttl_seconds(self) -> int:
        try:
            minutes = max(1, int(self._get_settings().environments.freestyle_ttl_minutes or 60))
        except Exception:
            minutes = 60
        return minutes * 60

    def _idle_seconds(self) -> int:
        try:
            minutes = max(1, int(self._get_settings().environments.freestyle_idle_minutes or 10))
        except Exception:
            minutes = 10
        return minutes * 60

    def _snapshot(self) -> str:
        try:
            value = str(self._get_settings().environments.freestyle_snapshot or "").strip()
        except Exception:
            value = ""
        return value or _SNAPSHOT_FALLBACK

    @staticmethod
    def _day_key() -> str:
        return clock.day_key(clock.now())

    # ------------------------------------------------------------------
    # kv（store 缺省 None 时全部当「没记过」处理）
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
        rec = self._kv_get(f"freestyle.day.{self._day_key()}")
        if isinstance(rec, dict):
            try:
                return int(rec.get("count", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def _count_acquire_today(self) -> None:
        key = f"freestyle.day.{self._day_key()}"
        rec = self._kv_get(key)
        if not isinstance(rec, dict):
            rec = {"count": 0, "acquires": []}
        acquires = [float(x) for x in (rec.get("acquires") or []) if isinstance(x, (int, float))]
        acquires.append(clock.now())
        self._kv_set(key, {"count": int(rec.get("count", 0)) + 1, "acquires": acquires})

    def _active_marker(self) -> dict | None:
        rec = self._kv_get("freestyle.active")
        if not isinstance(rec, dict):
            return None
        try:
            exp = float(rec.get("expires_ts", 0))
        except (TypeError, ValueError):
            return None
        if exp <= clock.now():
            # 过期自动释放：平台那边 TTL 到点会自己删，本地标记顺手清掉
            self._kv_set("freestyle.active", None)
            return None
        return rec

    def _note_fail(self, reason: str) -> None:
        logger.info("没拿到 Freestyle 一次性 VM：%s", reason)
        self._kv_set("freestyle.last_fail", {"ts": clock.now(), "reason": str(reason)[:200]})

    def _note_ok(self) -> None:
        self._kv_set("freestyle.last_acquire", {"ts": clock.now()})

    # ------------------------------------------------------------------
    # acquire
    # ------------------------------------------------------------------

    async def acquire(self, job_id: str) -> FreestyleBox | None:
        """申请一台 Freestyle VM；拿不到返回 None（原因进 kv["freestyle.last_fail"]）。"""
        settings = self._get_settings()
        if not self._platform_selected(settings):
            self._note_fail(
                "Freestyle 临时机器未启用（设置 → 全部设置 → 运行环境 → Freestyle 临时机器）"
            )
            return None
        key = self._api_key(settings)
        if not key:
            self._note_fail(
                "未填写 Freestyle 密钥（设置 → 全部设置 → 运行环境 → Freestyle 密钥）"
            )
            return None
        if self._today_count() >= self._daily_max():
            self._note_fail(
                f"今天的 Freestyle 机器用量已达上限（{self._today_count()}/{self._daily_max()}），"
                "请明天再试"
            )
            return None
        if self._busy_box is not None:
            self._note_fail("已有一台正在使用，同一时间只能用一台")
            return None
        if self._active_marker() is not None:
            self._note_fail("数据库里还记着一台未过期的 Freestyle 机器，同一时间只能用一台")
            return None

        ttl = self._ttl_seconds()
        payload: dict[str, Any] = {
            "firewall": {"rules": [{"action": "allow", "source": {}, "destination": {"public": True}}]},
            "snapshotId": self._snapshot(),
            "autoDeleteSeconds": 0,          # ephemeral：一停就删，不留垃圾
            "ttlSeconds": ttl,
            "idleTimeoutSeconds": self._idle_seconds(),
            "metadata": {"maiwork_job": str(job_id)[:63]},
        }
        created: dict[str, Any] | None = None
        last_reason = ""
        for attempt in range(2):  # 409 = slug 撞了，换一个后缀再试一次
            body = dict(payload)
            body["slug"] = self._slug(job_id, attempt)
            try:
                resp = await self._client_or_new().post(
                    "/v5/vms", json=body, headers={"Authorization": f"Bearer {key}"}
                )
            except Exception as e:
                logger.info("申请机器时连 Freestyle 失败：%s", e)
                self._note_fail("申请机器时连不上 Freestyle（详情见服务端日志）")
                return None
            if resp.status_code in (200, 201):
                try:
                    created = resp.json()
                except ValueError:
                    created = None
                if isinstance(created, dict) and created.get("id"):
                    break
                created = None
                last_reason = "Freestyle 已创建机器，但返回信息无法解析（缺少 id）"
                break
            last_reason = _error_reason(resp.status_code, *_error_code_message(resp))
            if resp.status_code != 409:
                break
        if created is None:
            self._note_fail(last_reason or "Freestyle 没给机器（原因不明）")
            return None

        vm_id = str(created.get("id") or "")
        state = str(created.get("state") or "")
        if state != _RUNNING:
            vm = await self._wait_running(vm_id, key)
            if vm is None:
                # 等不到 running：能删就删掉，别留一台半死不活的机器
                await self._delete(vm_id, key)
                return None
            created = {**created, **vm}
        box = self._box_from(created, job_id=str(job_id), ttl=ttl)
        if box is None:
            self._note_fail("Freestyle 返回的机器信息无法解析（缺少 id）")
            return None
        # 远端工作根先建出来（走文件 API，不花一次 exec）；建不出来不致命，后面 put 会再试
        await self._mkdir(box.remote_root, key, vm_id=box.vm_id, quiet=True)

        self._count_acquire_today()
        self._note_ok()
        self._kv_set(
            "freestyle.active",
            {"job_id": str(job_id), "vm_id": box.vm_id, "expires_ts": float(box.expires_ts)},
        )
        self._busy_box = box
        return box

    @staticmethod
    def _slug(job_id: str, attempt: int) -> str:
        """slug 只收 [a-z0-9-]、1~63 字符、账号内唯一 → 洗一遍再加随机后缀。

        attempt > 0 是 409（slug 撞了）后的重试，换一个后缀。
        """
        del attempt  # 每次调用都现抽随机后缀，重试天然换名字
        raw = "".join(c if (c.isalnum() or c == "-") else "-" for c in str(job_id or "").lower())
        raw = raw.strip("-") or "job"
        suffix = f"{random.randrange(16 ** 6):06x}"
        base = f"maiwork-{raw}"[: 63 - len(suffix) - 1]
        return f"{base}-{suffix}"

    def _box_from(self, data: dict[str, Any], *, job_id: str, ttl: int) -> FreestyleBox | None:
        vm_id = str(data.get("id") or "")
        if not vm_id:
            return None
        created_ts = _parse_ts(data.get("createdAt"))
        if created_ts is None:
            created_ts = clock.now()
        resources = data.get("resources") if isinstance(data.get("resources"), dict) else {}
        try:
            expires = created_ts + float(data.get("ttlSeconds") or ttl)
        except (TypeError, ValueError):
            expires = created_ts + float(ttl)
        return FreestyleBox(
            job_id=str(job_id),
            vm_id=vm_id,
            expires_ts=expires,
            remote_root=_REMOTE_ROOT,
            slug=str(data.get("slug") or ""),
            state=str(data.get("state") or ""),
            snapshot=str(data.get("snapshotId") or data.get("image") or self._snapshot()),
            cpu=_int_of(resources.get("cpu")),
            memory_mib=_int_of(resources.get("memory")),
            created_ts=created_ts,
        )

    async def _wait_running(self, vm_id: str, key: str) -> dict[str, Any] | None:
        """轮询到 state == running；到点 / 变成 stopped → None（原因进 last_fail）。"""
        deadline = clock.now() + self._poll_timeout_s
        last_state = ""
        while True:
            try:
                resp = await self._client_or_new().get(
                    f"/v5/vms/{vm_id}", headers={"Authorization": f"Bearer {key}"}
                )
            except Exception as e:
                logger.info("等待机器启动时连 Freestyle 失败：%s", e)
                self._note_fail("等待机器启动时连不上 Freestyle（详情见服务端日志）")
                return None
            if resp.status_code != 200:
                self._note_fail(_error_reason(resp.status_code, *_error_code_message(resp)))
                return None
            try:
                data = resp.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                self._note_fail("Freestyle 返回的机器信息无法解析（不是 JSON 对象）")
                return None
            last_state = str(data.get("state") or "")
            if last_state == _RUNNING:
                return data
            if last_state in _DEAD_STATES:
                self._note_fail(f"机器创建后即停止（state={last_state}），请重新申请一台")
                return None
            if clock.now() >= deadline:
                self._note_fail(f"等待机器启动超时（一直停在 state={last_state or '未知'}）")
                return None
            await self._sleep(self._poll_interval_s)

    async def _delete(self, vm_id: str, key: str) -> bool:
        """DELETE 幂等：404 也算成功。自身不抛。"""
        if not vm_id:
            return True
        try:
            resp = await self._client_or_new().delete(
                f"/v5/vms/{vm_id}", headers={"Authorization": f"Bearer {key}"}
            )
        except Exception as e:
            logger.info("销毁 Freestyle 机器失败（%s）：%s", vm_id, e)
            return False
        if resp.status_code in (200, 202, 204, 404):
            return True
        logger.info("销毁 Freestyle 机器被拒（%s）：%s", vm_id, _error_reason(resp.status_code, *_error_code_message(resp)))
        return False

    # ------------------------------------------------------------------
    # run：exec-await（远端退出码原样回传；statusCode=null = 被平台掐断）
    # ------------------------------------------------------------------

    async def run(self, box: FreestyleBox, command: str, *, timeout_s: int) -> RunResult:
        started = clock.now()
        timeout_s = max(1, int(timeout_s))
        remaining = float(box.expires_ts) - started
        if remaining < timeout_s + _RUN_GUARD_EXTRA_S:
            return RunResult(
                exit_code=-1,
                stdout="",
                stderr=f"机器即将到期（还剩 {max(0, int(remaining))} 秒，这条命令需要 {timeout_s} 秒，"
                       "不足以完成收尾）：请不要再提交任务，重新申请一台",
                ms=0,
                timed_out=False,
                oom=False,
            )
        timeout_ms = min(timeout_s * 1000, _EXEC_TIMEOUT_MAX_MS)
        key = self._api_key()
        if not key:
            return RunResult(
                exit_code=-1, stdout="",
                stderr="未填写 Freestyle 密钥，无法运行命令"
                       "（设置 → 全部设置 → 运行环境 → Freestyle 密钥）",
                ms=0, timed_out=False, oom=False,
            )
        body = {"command": str(command), "timeoutMs": timeout_ms, "linuxUser": _LINUX_USER}
        try:
            resp = await self._client_or_new().post(
                f"/v5/vms/{box.vm_id}/exec-await",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=httpx_timeout(timeout_ms / 1000.0 + 15),
            )
        except Exception as e:
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                return RunResult(
                    exit_code=-1, stdout="",
                    stderr=f"命令超时（超过 {timeout_s} 秒，已掐断；远端进程可能还在跑，"
                           "长任务请用 setsid -f + .exit 文件的做法收尾）",
                    ms=max(0, int((clock.now() - started) * 1000)), timed_out=True, oom=False,
                )
            raise
        if resp.status_code != 200:
            reason = _error_reason(resp.status_code, *_error_code_message(resp))
            return RunResult(
                exit_code=-1, stdout="", stderr=reason,
                ms=max(0, int((clock.now() - started) * 1000)), timed_out=False, oom=False,
            )
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return RunResult(
                exit_code=-1, stdout="", stderr="Freestyle 返回的命令结果无法解析（不是 JSON 对象）",
                ms=max(0, int((clock.now() - started) * 1000)), timed_out=False, oom=False,
            )
        status = data.get("statusCode")
        stdout = _tail(data.get("stdout") or "")
        stderr = _tail(data.get("stderr") or "")
        ms = max(0, int((clock.now() - started) * 1000))
        if status is None:
            # statusCode 为 null = 被平台按 timeoutMs 杀掉了
            note = f"命令超时（平台按 {timeout_ms // 1000} 秒掐断；远端进程可能还在跑，长任务请用 setsid -f + .exit 轮询）"
            return RunResult(
                exit_code=-1, stdout=stdout, stderr=(stderr + "\n" + note).strip(),
                ms=ms, timed_out=True, oom=False,
            )
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = -1
        return RunResult(exit_code=code, stdout=stdout, stderr=stderr, ms=ms, timed_out=False, oom=False)

    # ------------------------------------------------------------------
    # 文件：fs/write（小）/ fs/uploads（大）/ fs/read（带 Range）
    # ------------------------------------------------------------------

    async def put(self, box: FreestyleBox, local_path: Path | str, remote_path: str) -> None:
        local = Path(local_path)
        if not local.is_file():
            raise FileNotFoundError(f"要上传的本地文件不存在：{local}")
        remote = str(remote_path or "").strip()
        if not remote.startswith("/"):
            raise ValueError(f"远端路径必须是绝对路径（建议 {box.remote_root}/xxx）：{remote!r}")
        key = self._api_key()
        if not key:
            raise RuntimeError(
                "未填写 Freestyle 密钥，无法传文件"
                "（设置 → 全部设置 → 运行环境 → Freestyle 密钥）"
            )
        data = local.read_bytes()
        await self._mkdir(str(Path(remote).parent), key, vm_id=box.vm_id)
        if len(data) <= _FS_WRITE_MAX_BYTES:
            await self._write_small(box, remote, data, key)
            return
        await self._write_big(box, remote, data, key)

    async def _write_small(self, box: FreestyleBox, remote: str, data: bytes, key: str) -> None:
        body = {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
            "path": remote,
            "mode": 0o644,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        resp = await self._client_or_new().put(
            f"/v5/vms/{box.vm_id}/fs/write",
            params={"path": remote},
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"传到 Freestyle 失败：{_error_reason(resp.status_code, *_error_code_message(resp))}")

    async def _write_big(self, box: FreestyleBox, remote: str, data: bytes, key: str) -> None:
        digest = hashlib.sha256(data).hexdigest()
        begin = await self._client_or_new().post(
            f"/v5/vms/{box.vm_id}/fs/uploads",
            json={"path": remote, "size": len(data), "sha256": digest, "mode": 0o644},
            headers={"Authorization": f"Bearer {key}"},
        )
        if begin.status_code not in (200, 201):
            raise RuntimeError(f"分片上传开局失败：{_error_reason(begin.status_code, *_error_code_message(begin))}")
        try:
            upload_id = str((begin.json() or {}).get("uploadId") or "")
        except ValueError:
            upload_id = ""
        if not upload_id:
            raise RuntimeError("分片上传开局返回无法解析（缺少 uploadId）")
        offset = 0
        try:
            while offset < len(data):
                chunk = data[offset: offset + _FS_UPLOAD_CHUNK]
                resp = await self._client_or_new().put(
                    f"/v5/vms/{box.vm_id}/fs/uploads/{upload_id}",
                    params={"offset": offset},
                    content=chunk,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/octet-stream"},
                )
                if resp.status_code not in (200, 201, 204):
                    raise RuntimeError(
                        f"分片上传失败（offset={offset}）：{_error_reason(resp.status_code, *_error_code_message(resp))}"
                    )
                offset += len(chunk)
            done = await self._client_or_new().post(
                f"/v5/vms/{box.vm_id}/fs/uploads/{upload_id}/commit",
                json={},
                headers={"Authorization": f"Bearer {key}"},
            )
            if done.status_code not in (200, 201, 204):
                raise RuntimeError(f"分片上传收尾失败：{_error_reason(done.status_code, *_error_code_message(done))}")
        except Exception:
            try:
                await self._client_or_new().delete(
                    f"/v5/vms/{box.vm_id}/fs/uploads/{upload_id}",
                    headers={"Authorization": f"Bearer {key}"},
                )
            except Exception:
                logger.debug("中止分片上传失败（不影响结论）", exc_info=True)
            raise

    async def get(self, box: FreestyleBox, remote_path: str, local_path: Path | str) -> None:
        remote = str(remote_path or "").strip()
        if not remote.startswith("/"):
            raise ValueError(f"远端路径必须是绝对路径：{remote!r}")
        key = self._api_key()
        if not key:
            raise RuntimeError(
                "未填写 Freestyle 密钥，无法取回文件"
                "（设置 → 全部设置 → 运行环境 → Freestyle 密钥）"
            )
        size = await self._stat_size(box, remote, key)
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        if size is not None and size > _FS_READ_CHUNK:
            data = await self._read_ranged(box, remote, key, size)
        else:
            resp = await self._client_or_new().get(
                f"/v5/vms/{box.vm_id}/fs/read",
                params={"path": remote},
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code not in (200, 206):
                raise RuntimeError(f"从 Freestyle 取回失败：{_error_reason(resp.status_code, *_error_code_message(resp))}")
            data = resp.content
        local.write_bytes(data)

    async def _stat_size(self, box: FreestyleBox, remote: str, key: str) -> int | None:
        try:
            resp = await self._client_or_new().get(
                f"/v5/vms/{box.vm_id}/fs/stat",
                params={"path": remote},
                headers={"Authorization": f"Bearer {key}"},
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return int(data.get("size"))
        except (TypeError, ValueError):
            return None

    async def _read_ranged(self, box: FreestyleBox, remote: str, key: str, size: int) -> bytes:
        """大文件分块读（带 Range，官方支持 206 续传）。"""
        out = bytearray()
        while len(out) < size:
            start = len(out)
            end = min(start + _FS_READ_CHUNK, size) - 1
            resp = await self._client_or_new().get(
                f"/v5/vms/{box.vm_id}/fs/read",
                params={"path": remote},
                headers={"Authorization": f"Bearer {key}", "Range": f"bytes={start}-{end}"},
            )
            if resp.status_code not in (200, 206):
                raise RuntimeError(
                    f"从 Freestyle 分块取回失败（offset={start}）："
                    f"{_error_reason(resp.status_code, *_error_code_message(resp))}"
                )
            chunk = resp.content
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out)

    async def _mkdir(self, path: str, key: str, *, vm_id: str, quiet: bool = False) -> bool:
        """建目录（会建父目录）。quiet=True 时失败只记 debug。"""
        target = str(path or "").strip()
        if not target or not target.startswith("/"):
            return False
        try:
            resp = await self._client_or_new().post(
                f"/v5/vms/{vm_id}/fs/mkdir",
                json={"path": target},
                headers={"Authorization": f"Bearer {key}"},
            )
        except Exception:
            if quiet:
                logger.debug("建远端目录失败（%s）", target, exc_info=True)
                return False
            raise
        if resp.status_code in (200, 201, 204):
            return True
        if quiet:
            logger.debug("建远端目录被拒（%s）：HTTP %s", target, resp.status_code)
            return False
        raise RuntimeError(f"建远端目录失败：{_error_reason(resp.status_code, *_error_code_message(resp))}")

    # ------------------------------------------------------------------
    # release：真销毁（DELETE 幂等，自身不抛）
    # ------------------------------------------------------------------

    async def release(self, box: FreestyleBox | None) -> None:
        """结束一定调；DELETE 404 也算成功，绝不因此抛错（coordinator 的约定）。"""
        if box is None:
            return
        if self._busy_box is box:
            self._busy_box = None
        active = self._kv_get("freestyle.active")
        if isinstance(active, dict) and str(active.get("vm_id") or "") == str(box.vm_id):
            self._kv_set("freestyle.active", None)
        key = self._api_key()
        if key and box.vm_id:
            await self._delete(box.vm_id, key)

    @property
    def current_box(self) -> FreestyleBox | None:
        """当前占着的那台（同时只许 1 台）；没占着 → None。只读，不碰锁和 kv。"""
        return self._busy_box

    # ------------------------------------------------------------------
    # 网页健康状态用的小查询
    # ------------------------------------------------------------------

    def usage_today(self) -> int:
        """今天（北京时间）成功申请到几台。"""
        return self._today_count()

    def last_fail(self) -> dict | None:
        rec = self._kv_get("freestyle.last_fail")
        return rec if isinstance(rec, dict) else None

    async def check_key(self) -> tuple[bool, str]:
        """验密钥：GET /v5/vms?limit=1。返回 (能不能用, 中文说明)；不发请求就没有密钥。"""
        key = self._api_key()
        if not key:
            return False, ("未填写 Freestyle 密钥"
                           "（设置 → 全部设置 → 运行环境 → Freestyle 密钥）")
        try:
            resp = await self._client_or_new().get(
                "/v5/vms", params={"limit": "1"}, headers={"Authorization": f"Bearer {key}"}
            )
        except Exception:
            # 这条说明会直接显示在网页健康项上（views._freestyle_health → 「密钥验证失败：…」），
            # 不能把英文异常原文写给用户看；详情只进日志（2026-10-15 用户定）。
            logger.exception("验 Freestyle 密钥时连不上")
            return False, "连不上 Freestyle（详情见服务端日志）"
        if resp.status_code == 200:
            count = 0
            try:
                data = resp.json()
                if isinstance(data, dict):
                    count = _int_of(data.get("totalCount"))
            except ValueError:
                count = 0
            return True, f"密钥可用 · 账号里现在有 {count} 台机器"
        return False, _error_reason(resp.status_code, *_error_code_message(resp))


def _error_code_message(resp: Any) -> tuple[str, str]:
    """错误体 {"code","message"} → (code, message)；解析不出来给 ("","")。"""
    try:
        data = resp.json()
    except Exception:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("code") or ""), str(data.get("message") or "")


def _tail(text: Any, limit: int = 20000) -> str:
    """输出太长只留尾巴（和 LocalEnv._tail 一个口径）。"""
    out = str(text or "")
    if len(out) <= limit:
        return out
    return out[-limit:]


def _int_of(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def httpx_timeout(seconds: float) -> Any:
    """给单条 exec 用的 httpx 超时（要比 timeoutMs 大，别自己先掐断）。"""
    import httpx

    return httpx.Timeout(max(5.0, float(seconds)))
