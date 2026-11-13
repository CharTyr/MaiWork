"""测试用假对象。各模块的测试可以往这里加，但不要改已有对象的行为。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable, Dict, List, Tuple


class FakeCtx:
    """假的插件上下文：记录每次 call_capability，按能力名返回预设值。

    responses[name] 可以是：值（直接返回）、可调用对象（fn(**kw) -> 值，可以是协程函数）、
    Exception 实例（抛出）。没预设的能力返回 None。
    """

    def __init__(self, responses: Dict[str, Any] | None = None) -> None:
        self.responses: Dict[str, Any] = dict(responses or {})
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def call_capability(self, capability: str, **kw: Any) -> Any:
        # 第一个参数名与真 SDK 一致（capability），能力本身也可以带 name= 参数。
        name = capability
        self.calls.append((name, kw))
        r = self.responses.get(name)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            out = r(**kw)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        return r

    def names(self) -> List[str]:
        return [n for n, _ in self.calls]


def hook_message(
    group_id: str = "900000001",
    user_id: str = "10001",
    text: str = "你好",
    *,
    message_id: str = "123",
    session_id: str = "sess-1",
    ts: float = 1_790_000_000.0,
    is_at: bool = False,
    nickname: str = "群友",
) -> Dict[str, Any]:
    """按 docs/06 记录的真实键名造一条 chat.receive.after_process 的 kwargs。"""
    info: Dict[str, Any] = {"user_info": {"user_id": user_id, "user_nickname": nickname}}
    if group_id:
        info["group_info"] = {"group_id": group_id, "group_name": "测试群"}
    return {
        "message": {
            "message_id": message_id,
            # 宿主真载荷是字符串：message_utils.py `timestamp=str(...timestamp())`（docs/06）
            "timestamp": str(ts),
            "platform": "qq",
            "message_info": info,
            "raw_message": text,
            "is_at": is_at,
            "is_mentioned": is_at,
            "is_command": text.startswith("/"),
            "is_emoji": False,
            "is_picture": False,
            "is_notify": False,
            "session_id": session_id,
            "processed_plain_text": text,
        }
    }


Factory = Callable[..., Any]


class FakeProfiles:
    """假的 Profiles（docs/07 §8 的接口）：记录调用，不做事。

    - tick(gid)：追加到 ticks；tick_errors 里有这个群号则抛异常。
    - remember_session(group_id, session_id, last_msg_ts)：追加到 remembered。
    - 其余读方法返回可预设的空值。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # 接受 Profiles(store, host, models, get_settings) 的构造参数，直接丢掉
        self.ticks: List[str] = []
        self.tick_errors: set[str] = set()
        self.remembered: List[Tuple[str, str, float]] = []
        self.entries_map: Dict[str, List[dict]] = {}
        self.focus_map: Dict[str, List[dict]] = {}
        self.pulse_bins: List[int] | None = None
        self.usual_gap_value: float | None = None
        # 问题 A 回归测试用：next_needs_refresh=True 时 tick 返回带 needs_refresh 的对象；
        # refresh 记录调用，refresh_block（asyncio.Event）设了就等到被 set（模拟模型很慢）；
        # request_deps 记录 set_request_deps 接进来的 (approvals, goals, outbox)
        self.next_needs_refresh: bool = False
        self.refresh_calls: List[str] = []
        self.refresh_block: Any = None
        self.request_deps: Tuple[Any, Any, Any] | None = None

    async def tick(
        self,
        group_id: str,
        *,
        force: bool = False,
        refresh: bool = True,
        has_signal: Any = None,
    ) -> Any:
        self.ticks.append(group_id)
        if group_id in self.tick_errors:
            raise RuntimeError(f"FakeProfiles.tick 被要求失败: {group_id}")
        if self.next_needs_refresh:
            import types

            return types.SimpleNamespace(group_id=group_id, needs_refresh=True)
        return {"group_id": group_id}

    async def refresh(self, group_id: str, *, force: bool = False) -> bool:
        self.refresh_calls.append(group_id)
        ev = self.refresh_block
        if ev is not None:
            await ev.wait()
        return True

    def set_group_space(self, group_space: Any) -> None:
        self.group_space = group_space

    def set_request_deps(self, *, approvals: Any = None, goals: Any = None, outbox: Any = None) -> None:
        self.request_deps = (approvals, goals, outbox)

    def remember_session(self, group_id: str, session_id: str, last_msg_ts: float) -> None:
        self.remembered.append((group_id, session_id, last_msg_ts))

    def entries(self, group_id: str) -> List[dict]:
        return list(self.entries_map.get(group_id, []))

    def entries_for_prompt(self, group_id: str, limit: int = 20) -> List[dict]:
        return list(self.entries_map.get(group_id, []))[: int(limit)]

    def add_entry(self, group_id: str, category: str, text: str) -> int:
        entries = self.entries_map.setdefault(group_id, [])
        entry_id = max((int(e.get("id", 0)) for e in entries), default=0) + 1
        now = 1_790_000_000.0
        entries.append(
            {
                "id": entry_id,
                "group_id": group_id,
                "category": category,
                "text": text,
                "evidence_count": 0,
                "first_ts": now,
                "last_ts": now,
                "locked": True,
                "deleted": False,
                "source": "admin",
            }
        )
        return entry_id

    def edit_entry(self, entry_id: int, *, text: str | None = None, locked: bool | None = None) -> None:
        for entries in self.entries_map.values():
            for e in entries:
                if int(e.get("id", 0)) == int(entry_id):
                    if text is not None:
                        e["text"] = text
                    if locked is not None:
                        e["locked"] = bool(locked)
                    return
        raise KeyError(f"条目不存在: {entry_id}")

    def delete_entry(self, entry_id: int) -> None:
        for entries in self.entries_map.values():
            for e in entries:
                if int(e.get("id", 0)) == int(entry_id):
                    e["deleted"] = True
                    return
        raise KeyError(f"条目不存在: {entry_id}")

    def focus(self, group_id: str) -> List[dict]:
        return list(self.focus_map.get(group_id, []))

    def set_focus(self, group_id: str, user_id: str, action: str) -> None:
        members = self.focus_map.setdefault(group_id, [])
        if action == "add":
            if not any(m.get("user_id") == user_id for m in members):
                members.append(
                    {"user_id": user_id, "name": "", "reasons": ["管理员添加"], "note": "", "pinned": True}
                )
        elif action == "remove":
            self.focus_map[group_id] = [m for m in members if m.get("user_id") != user_id]

    def pulse(self, group_id: str, *, end: float, hours: int = 24) -> List[int]:
        if self.pulse_bins is not None:
            return list(self.pulse_bins)
        return [0] * (hours * 4)

    def usual_gap(self, group_id: str, ts: float) -> float | None:
        return self.usual_gap_value


# ---------------------------------------------------------------------------
# profile.py 测试用（2026-09 追加）：FakeHost / FakeModels
# ---------------------------------------------------------------------------


class FakeHost:
    """假的 Host：预置 Msg 列表，按 [start, end] 过滤返回升序前 limit 条。

    - msgs: list；元素是 CharTyr_MaiWork.maiwork.host.Msg 或有同名属性的对象。
    - session_id: session_for_group 的固定返回值；session_error 非空时抛它。
    - info: group_info 的固定返回 dict；info_error 非空时抛它。
    - 记录：msg_calls=[(session_id, start, end, limit)]、session_calls=[gid]、info_calls=[gid]。
    - 线上 T-14（2026-10-10）追加（只加不改）：by_id 是 {message_id: 消息 dict}，
      供 `message_by_id`（取原图用）回放；message_error 非空时抛它；记录
      by_id_calls=[(message_id, session_id, include_binary_data)]。
    """

    def __init__(
        self,
        msgs: List[Any] | None = None,
        *,
        session_id: str = "sess-1",
        info: Dict[str, Any] | None = None,
        session_error: Exception | None = None,
        info_error: Exception | None = None,
        by_id: Dict[str, Any] | None = None,
        message_error: Exception | None = None,
    ) -> None:
        self.msgs: List[Any] = sorted(list(msgs or []), key=lambda m: m.ts)
        self.session_id = session_id
        self.info: Dict[str, Any] = dict(info or {})
        self.session_error = session_error
        self.info_error = info_error
        self.msg_calls: List[Tuple[str, float, float, int]] = []
        self.session_calls: List[str] = []
        self.info_calls: List[str] = []
        self.by_id: Dict[str, Any] = dict(by_id or {})
        self.message_error = message_error
        self.by_id_calls: List[Tuple[str, str, bool]] = []

    async def messages(
        self, session_id: str, start: float, end: float, limit: int, *, limit_mode: str = "latest"
    ) -> List[Any]:
        # 和线上 MaiBot 一样（message_repository.find_messages，2026-09-27 实读）：
        # limit_mode 默认 "latest" = 取区间里最新的 N 条；"earliest" = 最早的 N 条；结果都按时间正序。
        self.msg_calls.append((session_id, start, end, limit))
        self.msg_modes = getattr(self, "msg_modes", []) + [limit_mode]
        got = [m for m in self.msgs if start <= m.ts <= end]
        if limit and limit > 0:
            got = got[:limit] if limit_mode == "earliest" else got[-limit:]
        return got

    async def session_for_group(self, group_id: str) -> str:
        self.session_calls.append(group_id)
        if self.session_error is not None:
            raise self.session_error
        return self.session_id

    async def group_info(self, group_id: str) -> Dict[str, Any]:
        self.info_calls.append(group_id)
        if self.info_error is not None:
            raise self.info_error
        return dict(self.info)

    async def message_by_id(
        self, message_id: str, session_id: str = "", *, include_binary_data: bool = False, **kwargs: Any
    ) -> Any:
        """假 `Host.message_by_id`（线上 T-14）：从 by_id 里回放一条消息 dict。

        深拷贝一份再给出去，免得调用方（把原图解码写盘那套）改到预置数据。
        预置值是异常实例时抛它（测「宿主炸了」）；对不上 message_error 也一样。
        """
        import copy

        self.by_id_calls.append((str(message_id), str(session_id or ""), bool(include_binary_data)))
        if self.message_error is not None:
            raise self.message_error
        msg = self.by_id.get(str(message_id))
        if isinstance(msg, BaseException):
            raise msg
        if msg is None:
            return None
        return copy.deepcopy(msg)


class FakeModels:
    """假的 Models：settings().ready() 可控。"""

    def __init__(self, ready: bool = True) -> None:
        self._ready = ready

    def settings(self) -> Any:
        ready = self._ready

        class _S:
            def ready(self) -> bool:
                return ready

        return _S()


# ---------------------------------------------------------------------------
# profile.py 第二部分测试用（2026-09 追加）：FakeModelsQueue
# ---------------------------------------------------------------------------


DISPLAY_CHECK_PASSES = {"feeds.post_check": '{"unsupported": []}', "personal.post_check": '{"unsupported": []}'}


class FakeModelsQueue:
    """假的 Models（第二部分提炼画像用）：预设回复队列 + 记录每次调用。

    - reply_queue: 先进先出；元素是 str（chat 的返回文本）或 Exception 实例（抛出）。
      队列空了默认回 "{\"ops\": [], \"people\": []}"；按 purpose 给了 defaults 的用那份
      （比如端到端测试给展示自检一个明确的 {"unsupported": []}，不靠不合格式的兜底回复）。
    - calls: 每次 chat 记 (role, messages, kwargs)；kwargs 含 json_mode/purpose/group_id 等。
    - 其他 FakeModels 的测试不受影响（两个类并存）。
    """

    # 整个测试文件共用的按 purpose 默认回复（用 monkeypatch 设，测完自动还原）
    DEFAULTS: Dict[str, str] = {}

    def __init__(
        self, ready: bool = True, replies: List[Any] | None = None, defaults: Dict[str, str] | None = None
    ) -> None:
        self._ready = ready
        self.reply_queue: List[Any] = list(replies or [])
        self.defaults: Dict[str, str] = dict(defaults or {})
        self.calls: List[Tuple[str, List[dict], Dict[str, Any]]] = []

    def settings(self) -> Any:
        ready = self._ready

        class _S:
            def ready(self) -> bool:
                return ready

        return _S()

    async def chat(self, role: str | None = None, messages: List[dict] | None = None, **kwargs: Any) -> Any:
        self.calls.append((role, messages, kwargs))
        if self.reply_queue:
            item = self.reply_queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, dict) and "text" in item:
                # {"text": ..., "finish_reason": "length"}：模拟回答被截断
                text = str(item["text"])
                finish = str(item.get("finish_reason") or "stop")
            else:
                text = str(item)
                finish = "stop"
        else:
            purpose = str(kwargs.get("purpose") or "")
            text = self.defaults.get(purpose, type(self).DEFAULTS.get(purpose, '{"ops": [], "people": []}'))
            finish = "stop"
        return type("ChatResult", (), {"text": text, "finish_reason": finish})()


# ---------------------------------------------------------------------------
# topics.py 测试用（2026-09 追加）
# ---------------------------------------------------------------------------


class SignalsStub:
    """假的 intake.Signals：手动调 mark 塞「最近消息」。有这些就够了。"""

    def __init__(self) -> None:
        self._map: Dict[str, Any] = {}

    def mark(self, group_id: str, session_id: str, ts: float) -> None:
        self._map[group_id] = type("Signal", (), {"session_id": session_id, "last_ts": ts, "count": 1})()

    def last_ts(self, group_id: str) -> float:
        s = self._map.get(group_id)
        return float(getattr(s, "last_ts", 0.0)) if s is not None else 0.0

    def session_id(self, group_id: str) -> str:
        s = self._map.get(group_id)
        return str(getattr(s, "session_id", "")) if s is not None else ""

    def take(self) -> Dict[str, Any]:
        out = self._map
        self._map = {}
        return out


# FakeHost 补 send_text / proactive_trigger 记录


class _SendResult:
    def __init__(self, sent: bool, message_id: str = "") -> None:
        self.sent = sent
        self.message_id = message_id


async def _fake_host_send_text(self, session_id: str, text: str, *, reply_to: str = "", **kwargs: Any) -> Any:
    """FakeHost 补丁方法；被赋到类上。"""
    rec = {"session_id": session_id, "text": text, "reply_to": reply_to, "kwargs": kwargs}
    if not hasattr(self, "send_text_calls"):
        self.send_text_calls = []
    self.send_text_calls.append(rec)
    return _SendResult(sent=True, message_id="fake-msg-id")


async def _fake_host_proactive_trigger(
    self, session_id: str, intent: str, *, reason: str = "", priority: str = "normal", metadata: Any = None, **kwargs: Any
) -> dict:
    rec = {"session_id": session_id, "intent": intent, "reason": reason, "priority": priority, "metadata": metadata, "kwargs": kwargs}
    if not hasattr(self, "proactive_trigger_calls"):
        self.proactive_trigger_calls = []
    self.proactive_trigger_calls.append(rec)
    return {"success": True, "stream_id": session_id, "task_id": "fake-task", "queued": True}


async def _fake_host_config(self, key: str, default: Any = None) -> Any:
    if key == "bot.nickname":
        return "测试AI"
    if key == "personality.personality":
        return "乐于助人"
    if key == "personality.reply_style":
        return "简短口语"
    return default


FakeHost.send_text = _fake_host_send_text
FakeHost.proactive_trigger = _fake_host_proactive_trigger
FakeHost.config = _fake_host_config


def write_text_reply(tasks: Any, tid: str, text: str = "（正文）") -> bool:
    """给任务写 text 活的成品正文 `artifacts/<任务>/reply.md`（线上 T-13，2026-10-10）。

    `deliver_kind="text"` 的成品固定是这个文件（`coordinator.TEXT_REPLY_NAME`），验收只认它。
    测试建任务时顺手写上，等价于子 agent 交回了正文；需要「没有 reply.md / 空 reply.md」
    的用例不要调它（或自己覆盖 / 删掉）。写不出来返回 False，不抛。
    """
    from pathlib import Path

    try:
        settings = tasks._get_settings()
        root = Path(settings.environments.workspace_root)
        ws_name = str(tasks.get(tid)["workspace"])
        path = root / ws_name / "artifacts" / str(tid) / "reply.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding="utf-8")
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# app.py / console M2 接线测试用（2026-10 追加，只加不改）：FakeFeeds / FakeScheduler
# ---------------------------------------------------------------------------


class FakeFeeds:
    """假的 Feeds（docs/07 §10.4 接口 + next-news 计数）。

    - prepare_news / make_idea：记录调用；errors 里有这个群就抛；delay_s 可拖时间。
    - feedback(kind, item_id, value, prev)：记录并按预设表返回 {"up","down"}；
      missing_feedback 里的 id 抛 KeyError。
    - idea_action(idea_id, op, by=…)：记录（含 by）、返回预设的 idea dict；missing_idea 抛 KeyError。
    - news_view / ideas_view / today_count：返回预置值。
    """

    def __init__(self) -> None:
        self.prepare_calls: List[str] = []
        self.make_idea_calls: List[str] = []
        self.errors: set[str] = set()
        self.delay_s: float = 0.0
        self.feedback_calls: List[Tuple[str, int, Any, Any]] = []
        self.feedback_result: Dict[str, Any] = {"up": 1, "down": 0}
        self.missing_feedback: set[int] = set()
        self.idea_calls: List[Tuple[int, str, str]] = []
        self.idea_result: Dict[str, Any] = {"id": 1, "state": "wanted"}
        self.missing_idea: set[int] = set()
        self.news_map: Dict[str, List[dict]] = {}
        self.ideas_map: Dict[str, List[dict]] = {}
        self.today_map: Dict[str, int] = {}
        self.spark: Dict[str, float] = {}
        self.spark_ready_map: Dict[str, bool] = {}
        self.spark_taken_calls: List[str] = []

    async def prepare_news(self, group_id: str) -> int:
        self.prepare_calls.append(group_id)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if group_id in self.errors:
            raise RuntimeError(f"FakeFeeds.prepare_news 被要求失败: {group_id}")
        return 2

    async def make_idea(self, group_id: str) -> Any:
        self.make_idea_calls.append(group_id)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if group_id in self.errors:
            raise RuntimeError(f"FakeFeeds.make_idea 被要求失败: {group_id}")
        return 1

    def feedback(self, kind: str, item_id: int, value: Any, prev: Any) -> Dict[str, Any]:
        self.feedback_calls.append((str(kind), int(item_id), value, prev))
        if int(item_id) in self.missing_feedback:
            raise KeyError(item_id)
        return dict(self.feedback_result)

    def idea_action(self, idea_id: int, op: str, *, by: str = "", item_nos: Any = None) -> Dict[str, Any]:
        self.idea_calls.append((int(idea_id), str(op), str(by), item_nos))
        if int(idea_id) in self.missing_idea:
            raise KeyError(idea_id)
        out = dict(self.idea_result)
        out.setdefault("id", int(idea_id))
        out["state"] = {"want": "wanted", "do": "pending", "dismiss": "dismissed"}.get(op, out.get("state"))
        return out

    def news_view(self, group_id: str, *, days: int = 3) -> List[dict]:
        return list(self.news_map.get(group_id, []))

    def ideas_view(self, group_id: str) -> List[dict]:
        return list(self.ideas_map.get(group_id, []))

    def today_count(self, group_id: str) -> int:
        return int(self.today_map.get(group_id, 0))

    # 话头火花（docs/18 §十一）：app 排程要问的几口；够不够格由 spark_ready_map 说了算，
    # 认领过一次记在 spark_taken_calls（真 Feeds 里对应清内存火花 + 记当天）。
    # **kwargs 兼容真实签名（text/user_id/message_id 等脱敏原话入参，FakeFeeds 不记原话）。
    def note_spark(self, group_id: str, ts: Any = None, **kwargs: Any) -> None:
        self.spark[str(group_id)] = float(ts if ts is not None else 0.0)

    def spark_ready(self, group_id: str, now: float) -> bool:
        return bool(self.spark_ready_map.get(str(group_id), False))

    def spark_taken(self, group_id: str, now: float) -> None:
        self.spark_taken_calls.append(str(group_id))
        self.spark.pop(str(group_id), None)


class FakeScheduler:
    """假的 Scheduler（docs/07 §10.5 接口 + next_news_ts）。

    due_map[gid] 是队列：每次 due(gid,…) 弹一个；列表最后一次循环给（方便断言「还在跑时不重开」）。
    next_news_map[gid] 给 next_news_ts 的返回值。
    """

    def __init__(self) -> None:
        self.due_map: Dict[str, List[List[str]]] = {}
        self.due_calls: List[Tuple[str, float, float]] = []
        self.done_calls: List[Tuple[str, str, float]] = []
        self.next_news_map: Dict[str, Any] = {}
        self.next_news_calls: List[Tuple[str, float]] = []

    def due(self, group_id: str, now: float, *, last_msg_ts: float = 0.0) -> List[str]:
        self.due_calls.append((group_id, float(now), float(last_msg_ts)))
        q = self.due_map.get(group_id) or []
        if len(q) > 1:
            return list(q.pop(0))
        if q:
            return list(q[0])
        return []

    def done(self, group_id: str, job: str, now: float) -> None:
        self.done_calls.append((group_id, str(job), float(now)))

    def next_news_ts(self, group_id: str, now: float) -> Any:
        self.next_news_calls.append((group_id, float(now)))
        return self.next_news_map.get(group_id)


# ---------------------------------------------------------------------------
# M3 测试用（2026-10 追加，只加不改）：FakeHost.group_member_role / FakeCoordinator
# ---------------------------------------------------------------------------


async def _fake_host_group_member_role(self, group_id: str, user_id: str, **kwargs: Any) -> str:
    """FakeHost 补丁方法；从实例属性 member_roles={(group_id, user_id): role} 取，缺省 "member"。"""
    roles = getattr(self, "member_roles", None) or {}
    return str(roles.get((str(group_id), str(user_id)), "member"))


FakeHost.group_member_role = _fake_host_group_member_role


class FakeCoordinator:
    """假的 Coordinator（M3，和 coordinator.py 的接口一致）：记录调用，可拖时间。

    - run_calls / check_calls / resume_calls：按顺序记录入参。
    - delay_s：run_task 拖这么久（测「同一任务不重复开工」）；delay 期间可被 cancel。
    - errors：集合里的 task_id 让 run_task 抛异常。
    """

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.run_calls: List[str] = []
        self.check_calls: List[str] = []
        self.resume_calls: List[Tuple[str, str]] = []
        self.delay_s = delay_s
        self.errors: set[str] = set()

    async def run_task(self, task_id: str) -> None:
        tid = str(task_id)
        self.run_calls.append(tid)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if tid in self.errors:
            raise RuntimeError(f"FakeCoordinator.run_task 被要求失败: {tid}")

    async def check_goal(self, goal_id: str) -> None:
        gid = str(goal_id)
        self.check_calls.append(gid)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)

    async def resume(self, task_id: str, answer_text: str) -> None:
        self.resume_calls.append((str(task_id), str(answer_text)))



# ---------------------------------------------------------------------------
# feeds 定关注点回复（2026-11 追加，只加不改）：定关注点现在要求 3–5 个，
# 少于 3 个会带一句追问重试一次——所以绝大多数「走到定关注点」的用例都该喂 ≥3 个。
# ---------------------------------------------------------------------------


def focus_reply(*queries: str) -> str:
    """造一条定关注点的模型回复 JSON：{"focus":[{"query","why","source"},…]}。

    不传参数时给 3 个互不相同的默认方向（recent/long/explore 各一）；
    要测「少于 3 个触发重试」的用例显式传 1–2 个 query。
    """
    import json as _json

    srcs = ["recent", "long", "explore"]
    qs = list(queries) or ["FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向"]
    whys = ["群里最近在聊", "长期兴趣", "拓展方向"]
    return _json.dumps(
        {"focus": [
            {"query": q, "why": whys[i % len(whys)], "source": srcs[i % len(srcs)]}
            for i, q in enumerate(qs)
        ]},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 撒网改成「代码按计划搜」（2026-10-01）后：预置「discover」候选改由假搜索服务供给
# ---------------------------------------------------------------------------


class FakePlannedSearch:
    """代码撒网（Feeds._run_planned_searches）用的假搜索：按计划种预置候选、记录调用。

    - items: {focus_index(1 起): [item...]}；代码撒网搜到这个方向的计划时，
      把这些预置 item 当 search.py 归一化后的结果交回（title/url/snippet=summmary/published/provider 字段）。
      方向也有 planned_searches 预算时按计划逐条回放；没给计划的（缺省回退成 focus["query"]
      那一搜）整份交回。兜底：没给 items / 没对上的方向，整份交回 items_flat（缺省=全部）。
    - planned_searches: {focus_index: [{"q","site","news","kind"}, …]}；
      set_planned(True) 后（Feeds 不再派 feeds-discover: 子 agent 就算撒网按计划跑了）
      自动按老习惯给 3 个关注点各造 2 条 ordinary 计划。
    - fail_all=True：每搜都抛（测「撒网全挂 → 整轮照老句式跳过」）。
    - broad_providers(): 保底补搜用（缺省 ["main"]）：第一个算主家，其余走 search_with。
    """

    def __init__(
        self,
        items: Any = None,
        *,
        broad: List[str] | None = None,
        with_results: Any = None,
        fail_all: bool = False,
        delay_s: float = 0.0,
    ) -> None:
        # items 可以是 callable（跑的时候再取——有些用例在 _feeds 之后才改 workers.report）
        self._items_callable: Any = items if callable(items) else None
        items0 = None if callable(items) else items
        if isinstance(items0, dict) and all(isinstance(k, int) for k in items0):
            self.items_by_focus: Dict[int, List[dict]] = {int(k): list(v or []) for k, v in items0.items()}
        elif isinstance(items0, list):
            self.items_by_focus = {1: list(items0)}
        else:
            self.items_by_focus = {}
        # 兜底那份：没标方向时每搜都整份给；有方向标时空（日志/兜底都走 items_by_focus）
        self._flat_explicit: List[dict] = (
            [it for _k in sorted(self.items_by_focus) for it in self.items_by_focus[_k]]
            if self._items_callable is None
            else []
        )
        self._broad = list(broad) if broad is not None else ["main"]
        self.with_results: Dict[str, List[dict]] = dict(with_results or {})
        self.fail_all = fail_all
        self.delay_s = delay_s
        self.calls: List[Dict[str, Any]] = []
        self.with_calls: List[tuple] = []
        self.planned_searches: Dict[int, List[dict]] = {}
        self._planned_on = False
        self.max_parallel = 0
        self._cur = 0

    def _current_items(self) -> Any:
        """items 的最新货：callable（比如懒取 workers.report.data 的那份）就跑一下再归一。"""
        if self._items_callable is None:
            return self.items_by_focus
        try:
            return self._items_callable()
        except Exception as exc:  # 预置的是「垮」就让这次搜索抛（老用例语义）
            raise RuntimeError(str(exc)) from exc

    def _as_focus_map(self, items: Any) -> Dict[int, List[dict]]:
        if isinstance(items, dict) and all(isinstance(k, int) for k in items):
            return {int(k): list(v or []) for k, v in items.items()}
        if isinstance(items, list):
            return {1: list(items)}
        return {}

    def set_planned(self, on: bool = True) -> "FakePlannedSearch":
        self._planned_on = bool(on)
        if on and not self.planned_searches:
            known = list(self.items_by_focus) if self.items_by_focus else [1, 2, 3]
            for i in known:
                if self.items_by_focus and not self.items_by_focus.get(i):
                    continue
                self.planned_searches[int(i)] = [
                    {"q": f"方向{i} 新进展", "site": "", "news": True, "kind": "news"},
                    {"q": f"方向{i} 分析", "site": "", "news": False, "kind": "news"},
                ]
        return self

    def _seed_focus(self) -> int:
        if self._items_callable is None:
            return min(self.items_by_focus) if self.items_by_focus else 1
        return 1

    def planned_focus_reply(self, *queries: str) -> str:
        """造定关注点回复：focus 的 searches 按 planned_searches 填；diverse/其他方向整份兜底。

        方向次序（focus_reply 缺省 explore 在第三）：全都给 planned_searches 里有的计划；
        多出的方向（diverse 常是第 4 个）不带 searches——代码会回退成那条 query 单搜，
        这时兜底把整份 items 记在它名下（模拟线上「最后看到这条链接」），保老用例口径。
        """
        import json as _json

        qs = list(queries) or ["FPGA 新动态", "本地大模型新玩法", "开源掌机社区风向"]
        srcs = ["recent", "long", "explore"]
        whys = ["群里最近在聊", "长期兴趣", "拓展方向"]
        focus = []
        for i, q in enumerate(qs, 1):
            item = {"query": q, "why": whys[(i - 1) % len(whys)], "source": srcs[(i - 1) % len(srcs)]}
            planned = self.planned_searches.get(i)
            if planned:
                item["searches"] = [dict(p) for p in planned]
            focus.append(item)
        return _json.dumps({"focus": focus}, ensure_ascii=False)

    def _main_provider(self) -> str:
        return str(self._broad[0]) if self._broad else "main"

    def _plans_for(self, query: str) -> tuple:
        """这个搜索词属于哪份计划的第几搜（(focus_index, ordinal)）；对不上 → (None, None)。"""
        for fi, planned in self.planned_searches.items():
            for j, p in enumerate(planned):
                if str(p.get("q") or "") == str(query):
                    return int(fi), int(j)
        return None, None

    def _results_for_entry(self, fi: int | None, _map: Any = None) -> List[dict]:
        m = _map if _map is not None else self._as_focus_map(self._current_items())
        focus_no = self._seed_focus() if fi is None else int(fi)
        return list(m.get(focus_no) or [])

    def _norm(self, results: List[dict], provider: str) -> List[dict]:
        out: List[dict] = []
        for it in results:
            if not isinstance(it, dict) or not it.get("url"):
                continue
            pub = it.get("published")
            row = {
                "title": str(it.get("title") or ""),
                "url": str(it.get("url") or ""),
                "snippet": str(it.get("summary") or it.get("snippet") or ""),
                "published": float(pub) if isinstance(pub, (int, float)) and pub else None,
                "provider": str(it.get("provider") or provider or ""),
            }
            # 预置标了 kind（guide 好文）/ explore（拓展）就原样带过去——真搜索结果没这字段，
            # 候选里带上了 _pick 回落才不至于把「挑出来的好文」降格成资讯。
            if it.get("kind"):
                row["kind"] = str(it.get("kind"))
            if it.get("explore"):
                row["explore"] = True
            out.append(row)
        return out

    async def search(self, query: str, *, limit: int = 8, days: Any = None, site: str = "", news: bool = False) -> List[dict]:
        if self.delay_s > 0:
            self._cur += 1
            self.max_parallel = max(self.max_parallel, self._cur)
            try:
                await asyncio.sleep(self.delay_s)
            finally:
                self._cur -= 1
        self.calls.append({"q": str(query), "limit": limit, "days": days, "site": str(site or ""), "news": bool(news)})
        if str(query).startswith("__配置自检__"):
            return []  # feeds._ensure_search 的探测，一律放行
        if self.fail_all:
            raise RuntimeError("FakePlannedSearch 被要求全挂")
        provider = self._main_provider()
        items_map = self._as_focus_map(self._current_items())
        flat = [it for _k in sorted(items_map) for it in items_map[_k]]
        fi, j = self._plans_for(query)
        if fi is not None:
            # 同方向第一条计划：整份（保持预置顺序，登记簿按见序留）；
            # 后面的换法想象成「没搜出新东西」
            share = self._results_for_entry(fi, items_map) if int(j) == 0 else []
            return self._norm(share, provider)
        # 计划外的搜索（缺省回退的 focus["query"]、保底补搜）：整份兜底
        return self._norm(flat, provider)

    async def search_with(self, name: str, query: str, *, limit: int = 8, days: Any = None, site: str = "", news: bool = False) -> List[dict]:
        self.with_calls.append((name, query, limit, days))
        return self._norm(list(self.with_results.get(str(name), [])), str(name))

    def broad_providers(self) -> List[str]:
        return list(self._broad)


# ---------------------------------------------------------------------------
# feeds 两阶段恒生效（2026-09-30 用户决定）后的通用假 workers 派发
# ---------------------------------------------------------------------------

# 挑（feeds.pick）那一次的模型回复：不真挑，让 _pick 走「回落前 10 条」。
# 插在各测试模型队列里「定关注点」之后，防止挑把打分回复吃掉（队列错位）。
PICK_FALLBACK_REPLY = '{"note": "测试不挑，全要"}'


def ensure_pick_fallback(models: Any) -> Any:
    """两阶段恒生效后，老路用例的模型队列 ([focus, score, post, …]) 需要在
    focus 之后插一条「挑」的占位回复，否则挑会把打分回复吃掉。幂等：
    已是占位回复就不重复插。返回原对象（就地改 reply_queue）。"""
    q = getattr(models, "reply_queue", None)
    if not isinstance(q, list) or not q:
        return models
    first = q[0]
    if not isinstance(first, str) or '"focus"' not in first:
        return models
    if len(q) >= 2 and q[1] == PICK_FALLBACK_REPLY:
        return models
    # 队列本身就是给「定关注点（含追问重试）」用的：第二条还是 focus 回复时别插队
    if len(q) >= 2 and isinstance(q[1], str) and '"focus"' in q[1]:
        return models
    q.insert(1, PICK_FALLBACK_REPLY)
    return models


async def two_phase_workers_run(report: Any, brief: str, kwargs: Dict[str, Any], *, store: Any = None) -> Any:
    """「预置一份老路 report」的假 workers 接上两阶段流水线的通用派发。

    2026-11 零子 agent 改版：资讯线（撒网 / 核验 / 补打开）和个人向找料一律不派
    子 agent——代码直接搜、直接 fetch_page（fixture 工具见 make_fixture_page_tools）。
    还按老路子派工的标记在这里直接断言失败，逼着用例改到新路：
    feeds-discover: / feeds-verify: / feeds-recheck: / personal-collect:。
    """
    task_id = str(kwargs.get("task_id") or "")
    if isinstance(report, BaseException):
        raise report
    for dead in ("feeds-discover:", "feeds-verify:", "feeds-recheck:", "personal-collect:"):
        assert not task_id.startswith(dead), (
            f"{dead[:-1]} 子 agent 已删除（2026-11 零子 agent 改版："
            "主流程代码直接 fetch_page）；这个用例还在指望派工——"
            "改用 patch_two_phase_feeds / patch_personal_collect 接 fixture 工具"
        )
    return report


FIXTURE_RECHECK_QUOTE = "原文正文里确实写着这件事的细节。"


def fixture_recheck_keep(store: Any, brief: str, task_id: str, quote: str) -> Any:
    """Fake recheck sub-agent that really "opens" every listed link: writes the
    fetch_page tool record and the page body (containing ``quote``), then keeps each."""
    import re

    from CharTyr_MaiWork.maiwork import clock
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    verdicts = []
    for n, url in re.findall(r"^\[(\d+)\] .*?\n\s*链接：(\S+)", str(brief), flags=re.M):
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
                " VALUES (?, '', ?, '子 agent', 'fetch_page', ?, '取到正文', 1)",
                (clock.now(), task_id, json.dumps({"url": url})),
            )
        item = {"index": int(n), "verdict": "keep", "url": url, "quote": quote}
        record_fixture_pages(task_id, [dict(item, fetched=True, summary="")])
        verdicts.append(item)
    return WorkerReport(ok=True, summary="补打开好了", data={"items": verdicts}, evidence=[], steps=2)


def fixture_article(item: dict) -> str:
    """Fixture page body for a preset item: an article paragraph that contains its quote."""
    return (f"这篇文章的正文这样写道：{item.get('quote') or ''}。"
            f"正文接着介绍：{item.get('summary') or ''}")


def record_fixture_pages(task_id: str, items: list) -> None:
    """Test-only stand-in for fetch_page: the fake sub-agent "opened" each item it
    reports as fetched=true, and the page body really contains its quote.

    Production never does this — evidence there only comes from fetch_page handlers.
    Items whose preset says fetched is not literally True get no page (they were not opened).
    """
    from CharTyr_MaiWork.maiwork import verify_budget

    for it in items:
        if not isinstance(it, dict) or it.get("fetched") is not True:
            continue
        url = str(it.get("original_url") or it.get("url") or "")
        if url:
            verify_budget.record_page(task_id, url, url, fixture_article(it), title=str(it.get("title") or ""))


def patch_two_phase_feeds(
    feeds: Any,
    models: Any = None,
    items: Any = None,
    *,
    fail_all: bool = False,
    **search_kw: Any,
) -> "FakePlannedSearch":
    """把 feeds 接到「代码撒网」的假搜索上：返回建好的 FakePlannedSearch（feeds._search 已换）。

    - items：FakePlannedSearch 同款（{focus: [item]} 或 [item]）。
    - models 给了且它的 reply_queue 第一条是定关注点回复 → 就地换成带 searches 计划的版本
      （用回复里原本的方向名）；该方向没 prefill 计划就不带 searches——走缺省回退。
    - fail_all=True → 每搜都抛（测 skipped 句式用），不 prefill 计划也不动回复。
    """
    fake = FakePlannedSearch(items, fail_all=fail_all, **search_kw)
    feeds._search = fake
    if fail_all:
        return fake
    fake.set_planned(True)
    # 2026-11 零子 agent 改版：打开原文由主流程代码自己 fetch_page（不走子 agent）。
    # 测试侧接上 fixture 版 fetch_page——真走 _open_candidates 的预算/绑定/查重，
    # 页面内容按预置条目造（正文含 quote，published 带回页面日期）。
    _wire_fixture_page_tools(feeds, items)
    if models is not None:
        models._fixture_tools = getattr(feeds, "_tools", None)
        _wrap_score_enrichment(models, items)
        q = getattr(models, "reply_queue", None)
        if isinstance(q, list) and q and isinstance(q[0], str) and '"focus"' in q[0]:
            import json as _json

            try:
                parsed = _json.loads(q[0])
                focus = parsed.get("focus")
                if isinstance(focus, list):
                    # 原样保留 query / why / source（含乱填的 source——有测试专测归一化），
                    # 只给 prefill 了计划的方向补 searches；没计划的方向不带 → 走缺省回退。
                    changed = False
                    for i, f in enumerate(focus, 1):
                        if isinstance(f, dict) and f.get("query") and not f.get("searches"):
                            planned = fake.planned_searches.get(i)
                            if planned:
                                f["searches"] = [dict(p) for p in planned]
                                changed = True
                    if changed:
                        q[0] = _json.dumps(parsed, ensure_ascii=False)
            except Exception:
                pass
    return fake


# ---------------------------------------------------------------------------
# 2026-11 零子 agent 改版：fixture 版 fetch_page（核验/补打开子 agent 全删后，
# 主流程代码自己调 self._tools.call("fetch_page")——测试给它接这一份）
# ---------------------------------------------------------------------------


def _preset_list(items: Any) -> List[dict]:
    """patch 传进来的 items（{focus: [..]} / [item] / callable 同种）拍平成条目列表。"""
    if callable(items):
        try:
            items = items()
        except Exception:
            return []
    if isinstance(items, dict) and all(isinstance(k, int) for k in items):
        return [it for _k in sorted(items) for it in (items[_k] or []) if isinstance(it, dict)]
    if isinstance(items, list):
        return [it for it in items if isinstance(it, dict)]
    return []


def make_fixture_page_tools(
    store: Any,
    items: Any = None,
    *,
    default_rss_quote: str = FIXTURE_RECHECK_QUOTE,
) -> Any:
    """造一只装着 fixture 版 fetch_page 的 Tools（真走预算 / 记录 / 绑定，不碰网络）。

    - 预算：verify_budget.consume(ctx.task_id)——没开账本的照旧放行，超上限照账本的话拒；
    - 命中预置条目（url / original_url 规范化后对得上）：
        * fetched is False → ToolResult(ok=False)（模拟打不开；RSS 类预置没 fetched 键=打得开）；
        * 否则 record_page 进账本 + 回 ok（body=fixture_article，published 按预置转 YYYY-MM-DD）；
    - 没命中（RSS 源直接塞进来的链接等）：回 ok，正文里带 default_rss_quote——
      打分补的 quote 对得上，evidence 闸门照常认；
    - summarize 照真 fetch_page 的形状吐（取到正文 N 字 + 最终地址 + 页面发布日期行），
      page_date.dates_from_rows 靠工具调用行读页面日期。
    """
    from CharTyr_MaiWork.maiwork import clock as _clock
    from CharTyr_MaiWork.maiwork import page_date as _page_date
    from CharTyr_MaiWork.maiwork import verify_budget as _vb
    from CharTyr_MaiWork.maiwork.feeds import _normalize_url as _norm_url, _parse_published
    from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
    from CharTyr_MaiWork.maiwork.tools_builtin import final_url_note

    tools = Tools(store)
    tools._is_fixture_page_tools = True
    # （测试侧台账，不算生产数据）_open_candidates 读到的「页面正文」按规范化 URL 索引，
    # 打分补写（_wrap_score_enrichment）拿这里的内容填 summary/quote，保证逐字对得上。
    pages: Dict[str, dict] = {}
    tools.fixture_pages = pages

    def _find(url: str) -> dict | None:
        key = _norm_url(str(url or ""))
        if not key:
            return None
        for it in _preset_list(items):
            if key in {_norm_url(str(it.get("url") or "")), _norm_url(str(it.get("original_url") or ""))}:
                return it
        return None

    async def _handler(ctx: Any, args: dict) -> ToolResult:
        url = str(args.get("url") or "").strip()
        ok_budget, note = _vb.consume(str(getattr(ctx, "task_id", "") or ""))
        if not ok_budget:
            return ToolResult(ok=False, output="", error=note)
        it = _find(url)
        if it is not None and it.get("fetched") is False:
            # 预置明说打不开（老「fetched=False」用例）→ 真 fetch_page 失败的样子
            return ToolResult(ok=False, output="", error="打不开这个页面")
        if it is not None:
            body = str(it.get("body") or "") if "body" in it else fixture_article(it)
            title = str(it.get("title") or "")
            final_url = str(it.get("original_url") or it.get("url") or url)
            if "page_published" in it:
                published = str(it.get("page_published") or "")
                pub = None
            else:
                pub = it.get("published")
                published = ""
        else:
            body = f"这篇文章的正文这样写道：{default_rss_quote}。正文接着介绍页面要点和相关背景。"
            title = ""
            final_url = url
            pub = None
            published = ""
        if isinstance(pub, str):
            try:
                pub = _parse_published(pub)
            except Exception:
                pub = None
        if not published and isinstance(pub, (int, float)) and pub:
            published = _clock.bj(float(pub)).strftime("%Y-%m-%d")
        _vb.record_page(str(getattr(ctx, "task_id", "") or ""), url, final_url, body, title=title)
        pages[_norm_url(final_url)] = {"body": body, "title": title, "final_url": final_url}
        return ToolResult(
            ok=True, output=body,
            data={
                "url": url,
                "final_url": final_url,
                "title": title,
                "image_url": str(it.get("image_url") or "") if it is not None else "",
                "published": published,
                "via": "fixture",
            },
        )

    def _summarize(args: dict, res: ToolResult) -> tuple:
        inp = str((args or {}).get("url") or "")
        if not res.ok:
            return inp, res.error or "失败了"
        data = res.data or {}
        out = f"取到正文 {len(str(res.output or ''))} 字"
        out += final_url_note(str(data.get("final_url") or ""))
        out += _page_date.note(str(data.get("published") or ""))
        return inp, out

    tools.register(Tool(
        name="fetch_page",
        description="打开网页读正文（fixture：不联网，按预置条目回正文）",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "要打开的网页地址"}},
            "required": ["url"],
        },
        roles=frozenset({"worker", "main"}),
        handler=_handler,
        summarize=_summarize,
        timeout_s=45.0,
    ))
    return tools


def _wire_fixture_page_tools(owner: Any, items: Any = None) -> None:
    """给 Feeds / Personal 接上 fixture fetch_page（已接了自定义 Tools 的跳过；旧 fixture 可刷新）。"""
    cur = getattr(owner, "_tools", None)
    if cur is not None and not getattr(cur, "_is_fixture_page_tools", False):
        return
    store = getattr(owner, "_store", None)
    if store is None:
        return
    owner.set_tools(make_fixture_page_tools(store, items))


# 打分提示词里每条候选的 URL（「[i]（…）标题 —— 线索（URL，发布于 …）」行）：
# [^\n]*? 非贪婪——同行右边再出现「（http…）」（比如线索里引了另一链接）也不抢这个位置的 URL。
_SCORE_I_URL_RE = re.compile(r"^\s*\[(\d+)\][^\n]*?（(https?://[^\s，、；;)）]+)", re.M)
# 同一行把标题也抓出来（「[i]（资讯）标题 ——」/「[i] 标题 ——」两种形状），
# 生产 apply-back 是「回了 title 就按 title 对」——补写也照这条规则定到条目，
# 不然第一道筛掉中间一条后编号错位，会把别条的摘要 / 依据补到这条上。
_SCORE_I_TITLE_RE = re.compile(r"^\s*\[(\d+)\][^\n]*?([^\n]*?)\s*——\s*", re.M)
_SCORE_LEAD_PAREN_RE = re.compile(r"^（[^）]*）\s*")


def _wrap_score_enrichment(models: Any, items: Any = None) -> None:
    """包一层 models.chat：打分（feeds.score / personal.score）回复里**没给**
    summary / quote / conditions 的条目，按「本轮真读到的正文」（fixture 页面台账 +
    预置条目）补上——模拟真模型照正文写回抽取字段。

    老测试造的打分回复只含分数 / 标帜（子 agent 时代 summary/quote 是核验子 agent 写的）；
    零子 agent 改版后打分要吃这些字段写回——不给就当模型漏写，照正文补，显式给了
    （含 "summary": "" / "paywall": true 这类）一字不动。解析不出 {"scores": [...]}
    或提示词对不上 URL 的回复原样放过。
    """
    models._fixture_items = items
    if getattr(models, "_fixture_score_wrap", False):
        return
    had_inst_chat = "chat" in getattr(models, "__dict__", {})
    orig_inst_chat = models.chat if had_inst_chat else None

    async def _chat(*a: Any, **kw: Any) -> Any:
        result = await (orig_inst_chat(*a, **kw) if orig_inst_chat is not None else type(models).chat(models, *a, **kw))
        try:
            purpose = str(kw.get("purpose") or "")
            if purpose not in ("feeds.score", "personal.score"):
                return result
            from CharTyr_MaiWork.maiwork.feeds import (
                _normalize_url as _norm_url,
                _parse_model_json,
            )

            raw_text = str(getattr(result, "text", "") or "")
            data = _parse_model_json(raw_text)
            scores = data.get("scores") if isinstance(data, dict) else None
            if not isinstance(scores, list):
                return result
            prompt = ""
            msgs = kw.get("messages")
            if isinstance(msgs, list) and msgs:
                prompt = "\n".join(str(m.get("content") or "") for m in msgs if isinstance(m, dict))
            url_by_i = {int(m.group(1)): m.group(2) for m in _SCORE_I_URL_RE.finditer(prompt)}
            # 编号 → 标题（提示词里的候选行）；title→url 再从这里推
            title_by_i: Dict[int, str] = {}
            for m in _SCORE_I_TITLE_RE.finditer(prompt):
                t = _SCORE_LEAD_PAREN_RE.sub("", str(m.group(2) or "")).strip()
                if t:
                    title_by_i[int(m.group(1))] = t
            url_by_title = {t: url_by_i[i] for i, t in title_by_i.items() if i in url_by_i}

            cur_items = getattr(models, "_fixture_items", items)
            by_url = {}
            for it in _preset_list(cur_items):
                # setdefault：预置两条撞同一个规范化链接时先见的赢——
                # 和 _find 的回查口径一致，不然后到的预置会抢走前条的正文依据。
                for raw in (it.get("url"), it.get("original_url")):
                    key = _norm_url(str(raw or ""))
                    if key:
                        by_url.setdefault(key, it)
            pages = getattr(getattr(models, "_fixture_tools", None), "fixture_pages", None) or {}
            for s in scores:
                if not isinstance(s, dict):
                    continue
                url = ""
                # 和生产 apply-back 同口径：回了 title 先按 title 定条目（编号在
                # 「第一道筛掉中间候选跳号」时对不上），对不上再退回编号
                s_title = str(s.get("title") or "").strip()
                if s_title:
                    url = str(url_by_title.get(s_title) or "")
                if not url:
                    try:
                        url = url_by_i.get(int(s.get("i")), "")
                    except (TypeError, ValueError):
                        url = ""
                it = by_url.get(_norm_url(url))
                page = pages.get(_norm_url(url)) if url else None
                body = str((page or {}).get("body") or "")
                if it is not None and not body:
                    body = fixture_article(it)
                if "summary" not in s:
                    s["summary"] = str(it.get("summary") or "") if it else ""
                    if not s["summary"]:
                        s["summary"] = body[:200]
                if "quote" not in s:
                    s["quote"] = str(it.get("quote") or "") if it else ""
                    if not s["quote"]:
                        s["quote"] = body[:150]
                if it is not None and "conditions" not in s and it.get("conditions"):
                    s["conditions"] = list(it.get("conditions") or [])
            enriched = json.dumps(data, ensure_ascii=False)
            result.text = f"```json\n{enriched}\n```" if raw_text.lstrip().startswith("```") else enriched
        except Exception:
            pass  # 补写失败原样回，别影响这条 chat
        return result

    models.chat = _chat
    models._fixture_score_wrap = True


def patch_personal_collect(
    personal: Any,
    models: Any = None,
    items: Any = None,
    *,
    fail_all: bool = False,
    **search_kw: Any,
) -> "FakePlannedSearch":
    """把 Personal 接到「代码找料」的假搜索 + fixture fetch_page 上（2026-11 零子 agent 改版）。

    老 personal-collect: 子 agent 已删：Phase A 代码按计划搜（FakePlannedSearch 供结果），
    Phase C 代码自己 fetch_page（fixture 版，预算 PERSONAL_PAGE_CAP 照走）。
    items 同 FakePlannedSearch（预置条目带 quote/summary/fetched/published；fetched=False → 打不开）。
    """
    fake = FakePlannedSearch(items, fail_all=fail_all, **search_kw)
    personal._search = fake
    _wire_fixture_page_tools(personal, items)
    if models is not None:
        _wrap_score_enrichment(models, items)
        models._fixture_tools = getattr(personal, "_tools", None)
    return fake


# ---------------------------------------------------------------------------
# 构想「先有真实需要，再有构想」（docs/18 §十，2026-10-11 用户拍板）：
# 种本群真发言（evidence 要和它对得上）+ 拼一份合格的群向模型回包
# ---------------------------------------------------------------------------

IDEA_CHAT_LINES: List[str] = [
    "- 阿强：这手柄放半年了，还能救活吗",
    "- 小林：有什么办法能看清这块板子怎么接线",
]
IDEA_EVIDENCE = "这手柄放半年了，还能救活吗"
IDEA_BASIS: Dict[str, str] = {
    "pain": "手柄坏了就扔，没人知道还能不能修",
    "value": "修好能省下买新的钱",
    "interest": "群里一直在折腾手柄和板子",
}
IDEA_FEAS_OK: Dict[str, Any] = {
    "level": "ok", "note": "能做：联网查公开资料整理成步骤",
    "uses": ["search", "write"], "deliver": "doc", "needs_members": False,
}


def seed_idea_chat(store: Any, gid: str, lines: Any = None, *, now: float = 1_790_000_000.0) -> None:
    """往 chat_log 种几条本群真发言：make_idea 的 evidence 要和这里对得上。"""
    for i, line in enumerate(IDEA_CHAT_LINES if lines is None else lines):
        text = str(line).strip()
        if text.startswith("-"):
            text = text[1:].strip()
        if "：" in text:
            text = text.split("：", 1)[1]
        elif ":" in text:
            text = text.split(":", 1)[1]
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " VALUES (?, ?, ?, ?, '10001', '群友')",
                (text, str(gid), f"seed-chat-{i}", float(now)),
            )


def idea_reply(**over: Any) -> str:
    """群向构想的合格模型回包：evidence 照抄群里一句 + 三段 basis + kind（docs/18 §十）。"""
    idea: Dict[str, Any] = {
        "kind": "help",
        "title": "把手柄救活",
        "body": "先查这款手柄的常见故障，再出一份照着做的复活步骤。",
        "evidence": IDEA_EVIDENCE,
        "basis": dict(IDEA_BASIS),
        "icon": "gamepad",
        "chat_worthy": False,
        "keywords": ["手柄"],
        "items": [{"kind": "task", "title": "查清故障原因", "desc": "先出一版"}],
        "feasibility": dict(IDEA_FEAS_OK),
    }
    idea.update(over)
    return json.dumps({"drafts": ["查手柄", "修板子"], "idea": idea}, ensure_ascii=False)
