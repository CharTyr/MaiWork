"""runtime_bus.py：进程内运行事件总线（看板「正在做什么」的实时流）。

- 纯内存：发布订阅 + 有界最近事件环（deque maxlen=80）+ 在途任务表
  （active_flights；start/first_token 登记、done/error 摘除，>180s 没动静自动过期）。
- 隐私红线：事件只允许白名单字段（kind/stage/status/group_id/role/model/
  flight_id/ms/first_token_ms/ok/badge），绝不带群友原话 / 提示词 / 成员资料；
  badge 是 ≤24 字的正式中文短标签。
- 服务群闸门：is_served 判定给了且 group_id 非空、不是 "admin" 时，非服务群
  的事件直接丢掉（一个字段都不进内存）。
- 订阅者拿 asyncio.Queue(maxsize=64)；从别的线程 emit 时经
  call_soon_threadsafe 转发；队列满时丢最旧那条，慢客户端绝不反压生产者。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("maiwork.runtime_bus")

# 最近事件环大小 / 订阅者队列大小 / 在途任务过期秒数 / 快照里带的最近条数
RECENT_MAX = 80
SUB_QUEUE_MAX = 64
ACTIVE_STALE_S = 180.0
SNAPSHOT_RECENT = 30

_EVENT_FIELDS = (
    "seq", "ts", "kind", "stage", "status", "group_id", "role",
    "model", "flight_id", "ms", "first_token_ms", "ok", "badge",
)
_BADGE_MAX = 24


class RuntimeBus:
    """线程安全 / asyncio 安全的运行事件总线。没接线时各模块不建它（None）。"""

    def __init__(self, is_served: Callable[[str], bool] | None = None) -> None:
        self._is_served = is_served
        self._lock = threading.Lock()
        self._seq = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT_MAX)
        self._active: dict[str, dict[str, Any]] = {}
        self._subs: dict[int, asyncio.Queue] = {}
        self._sub_loops: dict[int, Any] = {}
        self._next_sub = 0

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def set_served_checker(self, fn: Callable[[str], bool] | None) -> None:
        """app 在 settings 就绪后补挂（构造时给了就不用手动调）。"""
        self._is_served = fn

    def _served(self, group_id: str) -> bool:
        """服务群闸门：没给 group_id（""）或 "admin" → 放行；没接判定 → 放行。"""
        gid = str(group_id or "")
        if not gid or gid == "admin":
            return True
        fn = self._is_served
        if fn is None:
            return True
        try:
            return bool(fn(gid))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 发
    # ------------------------------------------------------------------

    def emit(
        self,
        kind: str,
        *,
        stage: str,
        status: str = "done",
        group_id: str = "",
        role: str = "",
        model: str = "",
        flight_id: str = "",
        ms: int = 0,
        first_token_ms: int = 0,
        ok: bool = True,
        badge: str = "",
    ) -> dict[str, Any] | None:
        """发一条运行事件；被服务群闸门拦下 → None，否则回事件 dict。"""
        if not self._served(group_id):
            return None
        now = time.time()
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": now,
                "kind": str(kind or ""),
                "stage": str(stage or ""),
                "status": str(status or "done"),
                "group_id": str(group_id or ""),
                "role": str(role or ""),
                "model": str(model or ""),
                "flight_id": str(flight_id or ""),
                "ms": int(ms or 0),
                "first_token_ms": int(first_token_ms or 0),
                "ok": bool(ok),
                "badge": str(badge or "")[:_BADGE_MAX],
            }
            # 只留白名单字段（防上游误塞原话 / 提示词进内存）
            event = {k: event[k] for k in _EVENT_FIELDS}
            self._recent.append(event)
            fid = event["flight_id"]
            if fid:
                if event["status"] in ("start", "first_token"):
                    flight = self._active.get(fid)
                    if flight is None:
                        flight = {"started_ts": now}
                        self._active[fid] = flight
                    flight.update(
                        stage=event["stage"], group_id=event["group_id"],
                        role=event["role"], model=event["model"],
                        badge=event["badge"],
                    )
                    # started_ts 只在 start 写；first_token 更新 first_token_ms、不动 started_ts
                    if event["status"] == "first_token":
                        flight["first_token_ms"] = event["first_token_ms"]
                    else:
                        flight["first_token_ms"] = event["first_token_ms"] or flight.get("first_token_ms", 0)
                elif event["status"] in ("done", "error"):
                    self._active.pop(fid, None)
            self._expire_active(now)
            subs = list(self._subs.items())
        for sub_id, queue in subs:
            self._deliver(sub_id, queue, event)
        return event

    def _deliver(self, sub_id: int, queue: asyncio.Queue, event: dict[str, Any]) -> None:
        """把事件塞给订阅者队列；跨线程 emit 经 call_soon_threadsafe 转发。"""
        loop = self._sub_loops.get(sub_id)

        def _put() -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 慢客户端不反压生产者：丢最旧一条，塞最新的
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        if loop is not None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                _put()
            else:
                try:
                    loop.call_soon_threadsafe(_put)
                except RuntimeError:
                    pass
            return
        _put()

    def _expire_active(self, now: float) -> None:
        """清掉 >180s 没动静的在途任务（防 done 没发到把卡片永远钉在「正在跑」）。"""
        stale = [
            fid for fid, f in self._active.items()
            if now - float(f.get("started_ts") or now) > ACTIVE_STALE_S
        ]
        for fid in stale:
            self._active.pop(fid, None)

    # ------------------------------------------------------------------
    # 收
    # ------------------------------------------------------------------

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        """登记一个订阅者 → (sub_id, 队列)。队列绑当前事件循环（跨线程 emit 靠它转发）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
        with self._lock:
            self._next_sub += 1
            sub_id = self._next_sub
            self._subs[sub_id] = queue
            self._sub_loops[sub_id] = loop
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)
            self._sub_loops.pop(sub_id, None)

    # ------------------------------------------------------------------
    # 快照（SSE 起手 + 看板 live 块）
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._expire_active(now)
            active = [
                {"flight_id": fid, **dict(f)}
                for fid, f in self._active.items()
            ]
            recent = list(self._recent)[-SNAPSHOT_RECENT:]
            seq = self._seq
        return {"seq": seq, "ts": now, "active": active, "recent": recent}
