"""MaiWork M0 探针：只作用于测试群，由 /tmp/maiwork-probe/cmd/*.json 驱动，结果写 out.jsonl。

测完即删。不注册任何工具或指令，不向 planner 暴露任何东西。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode

logger = logging.getLogger("maiwork_probe")

GROUP_ID = "900000001"
BASE = Path("/tmp/maiwork-probe")
CMD_DIR = BASE / "cmd"
OUT = BASE / "out.jsonl"


def _now() -> float:
    return time.time()


class MaiWorkProbePlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task | None = None
        self._session_id: str = ""
        self._orig_freq: float | None = None
        self._freq_touched = False
        self._armed = False
        self._arm_delay = 0.5
        self._stats = {"all": 0, "group": 0, "max_ms": 0.0, "sum_ms": 0.0}
        self._keys_dumped = False
        self._guard_until = 0.0
        self._guard_s = 180.0

    # ---------- 输出 ----------
    def _out(self, kind: str, **data: Any) -> None:
        try:
            BASE.mkdir(parents=True, exist_ok=True)
            rec = {"t": round(_now(), 3), "kind": kind, **data}
            with OUT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("probe out 失败: %s", e)

    # ---------- 生命周期 ----------
    async def on_load(self) -> None:
        CMD_DIR.mkdir(parents=True, exist_ok=True)
        self._out("loaded", pid=os.getpid())
        self._task = asyncio.create_task(self._loop())
        logger.info("MaiWork 探针已加载，仅作用于群 %s", GROUP_ID)

    async def on_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._restore_freq("unload")
        self._out("unloaded", stats=self._stats)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        return None

    # ---------- 频率 ----------
    async def _get_freq(self, sid: str) -> Any:
        return await self.ctx.call_capability("frequency.get_adjust", chat_id=sid)

    async def _set_freq(self, sid: str, value: float) -> Any:
        return await self.ctx.call_capability("frequency.set_adjust", chat_id=sid, value=value)

    async def _restore_freq(self, why: str) -> None:
        if not self._freq_touched or not self._session_id:
            return
        orig = 1.0 if self._orig_freq is None else float(self._orig_freq)
        try:
            r = await self._set_freq(self._session_id, orig)
            back = await self._get_freq(self._session_id)
            self._freq_touched = False
            self._out("freq_restored", why=why, set_result=r, readback=back, orig=orig)
        except Exception as e:  # noqa: BLE001
            self._out("freq_restore_error", why=why, error=repr(e))

    async def _silence(self, sid: str, why: str) -> Dict[str, Any]:
        t0 = _now()
        if not self._freq_touched:
            self._orig_freq = await self._get_freq(sid)
        r = await self._set_freq(sid, 0.0)
        back = await self._get_freq(sid)
        self._freq_touched = True
        self._session_id = sid
        return {"why": why, "orig": self._orig_freq, "set": r, "readback": back, "ms": round((_now() - t0) * 1000, 1)}

    # ---------- 钩子 ----------
    @HookHandler(
        "chat.receive.after_process",
        name="maiwork_probe_after_process",
        description="MaiWork M0 探针：测量钩子开销与进入 WORK 时的静默时序（仅测试群）",
        mode=HookMode.BLOCKING,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_process(self, **kwargs: Any) -> Dict[str, Any]:
        t0 = _now()
        self._stats["all"] += 1
        try:
            msg = kwargs.get("message") or {}
            info = msg.get("message_info") or {}
            ginfo = info.get("group_info") or {}
            gid = str(ginfo.get("group_id") or "")
            if gid != GROUP_ID:
                return {"action": "continue"}
            self._stats["group"] += 1
            sid = str(msg.get("session_id") or "")
            if sid:
                self._session_id = sid
            if not self._keys_dumped:
                self._keys_dumped = True
                self._out("msg_keys", keys=sorted(msg.keys()), info_keys=sorted(info.keys()))
            uinfo = info.get("user_info") or {}
            rec = {
                "mid": msg.get("message_id"),
                "sid": sid,
                "uid": uinfo.get("user_id"),
                "is_at": msg.get("is_at"),
                "is_mentioned": msg.get("is_mentioned"),
                "text": str(msg.get("processed_plain_text") or "")[:60],
                "msg_ts": msg.get("timestamp"),
                "lag_s": None,
            }
            try:
                rec["lag_s"] = round(t0 - float(msg.get("timestamp")), 3)
            except Exception:  # noqa: BLE001
                pass
            if self._armed and (msg.get("is_at") or msg.get("is_mentioned")) and sid:
                self._armed = False
                await asyncio.sleep(self._arm_delay)  # 模拟 Jev 判断耗时
                rec["enter"] = await self._silence(sid, "hook_enter")
                self._guard_until = _now() + self._guard_s
            rec["hook_ms"] = round((_now() - t0) * 1000, 1)
            self._out("hook", **rec)
            return {"action": "continue"}
        finally:
            ms = (_now() - t0) * 1000
            self._stats["sum_ms"] += ms
            self._stats["max_ms"] = max(self._stats["max_ms"], round(ms, 1))

    @HookHandler(
        "send_service.before_send",
        name="maiwork_probe_before_send",
        description="MaiWork M0 探针：WORK 窗口内拦截 MaiBot 在测试群的发送（仅测试群）",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_send(self, **kwargs: Any) -> Dict[str, Any]:
        msg = kwargs.get("message") or {}
        sid = str(msg.get("session_id") or "")
        if not sid or sid != self._session_id:
            return {"action": "continue"}
        text = str(msg.get("processed_plain_text") or "")
        own = text.startswith("（MaiWork")
        active = _now() < self._guard_until
        self._out("before_send", sid=sid, text=text[:60], own=own, guard=active, keys=sorted(msg.keys())[:30])
        if active and not own:
            self._out("blocked", text=text[:80])
            return {"action": "abort"}
        return {"action": "continue"}

    # ---------- 指令循环 ----------
    async def _loop(self) -> None:
        while True:
            try:
                for p in sorted(CMD_DIR.glob("*.json")):
                    try:
                        cmd = json.loads(p.read_text(encoding="utf-8"))
                    except Exception as e:  # noqa: BLE001
                        cmd = {"op": "bad", "error": repr(e)}
                    p.unlink(missing_ok=True)
                    try:
                        res = await self._run(cmd)
                        self._out("cmd", cmd=cmd, ok=True, result=res)
                    except Exception as e:  # noqa: BLE001
                        self._out("cmd", cmd=cmd, ok=False, error=repr(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._out("loop_error", error=repr(e))
            await asyncio.sleep(1.0)
            if self._guard_until and _now() > self._guard_until:
                self._guard_until = 0.0
                await self._restore_freq("guard_expired")

    async def _run(self, cmd: Dict[str, Any]) -> Any:
        op = cmd.get("op")
        sid = cmd.get("sid") or self._session_id
        cap = self.ctx.call_capability
        if op == "stream":
            return {
                "by_group": await cap("chat.get_stream_by_group_id", group_id=GROUP_ID, platform="qq"),
                "hook_sid": self._session_id,
            }
        if op == "get_freq":
            return {"sid": sid, "value": await self._get_freq(sid), "talk": await cap("frequency.get_current_talk_value", chat_id=sid)}
        if op == "silence":
            return await self._silence(sid, "cmd")
        if op == "restore":
            await self._restore_freq("cmd")
            return {"sid": sid, "value": await self._get_freq(sid)}
        if op == "arm":
            self._armed = True
            self._arm_delay = float(cmd.get("delay", 0.5))
            return {"armed": True, "delay": self._arm_delay}
        if op == "unguard":
            self._guard_until = 0.0
            await self._restore_freq("unguard")
            return {"guard": False}
        if op == "disarm":
            self._armed = False
            return {"armed": False}
        if op == "stats":
            return self._stats
        if op == "send_reply":
            segs = []
            if cmd.get("reply_to"):
                segs.append({"type": "reply", "data": {"target_message_id": str(cmd["reply_to"])}})
            segs.append({"type": "text", "content": str(cmd.get("text") or "")})
            t0 = _now()
            r = await cap(
                "send.hybrid",
                segments=segs,
                stream_id=sid,
                return_details=True,
                sync_to_maisaka_history=True,
                storage_message=True,
                processed_plain_text=str(cmd.get("text") or ""),
            )
            return {"ms": round((_now() - t0) * 1000, 1), "result": r}
        if op == "upload":
            t0 = _now()
            r = await cap(
                "api.call",
                api_name="adapter.napcat.file.upload_group_file",
                version="1",
                args={"group_id": GROUP_ID, "file": cmd["file"], "name": cmd.get("name", "")},
                timeout_ms=60000,
            )
            return {"ms": round((_now() - t0) * 1000, 1), "result": r}
        if op == "api":
            return await cap("api.call", api_name=cmd["name"], version=cmd.get("version", "1"), args=cmd.get("args", {}), timeout_ms=30000)
        if op == "api_list":
            return await cap("api.list")
        if op == "readable":
            end = _now()
            start = end - float(cmd.get("minutes", 30)) * 60
            return await cap(
                "message.build_readable",
                chat_id=sid,
                start_time=start,
                end_time=end,
                limit=int(cmd.get("limit", 40)),
                replace_bot_name=True,
                timestamp_mode="relative",
            )
        if op == "by_time":
            end = _now()
            start = end - float(cmd.get("minutes", 30)) * 60
            r = await cap(
                "message.get_by_time_in_chat",
                chat_id=sid,
                start_time=start,
                end_time=end,
                limit=int(cmd.get("limit", 40)),
                filter_mai=False,
            )
            return r
        if op == "bot_account":
            return await cap("config.get", key="bot.qq_account")
        return {"error": f"unknown op {op}"}


def create_plugin() -> MaiWorkProbePlugin:
    return MaiWorkProbePlugin()
