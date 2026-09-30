"""测试用假对象。各模块的测试可以往这里加，但不要改已有对象的行为。"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Tuple


class FakeCtx:
    """假的插件上下文：记录每次 call_capability，按能力名返回预设值。

    responses[name] 可以是：值（直接返回）、可调用对象（fn(**kw) -> 值，可以是协程函数）、
    Exception 实例（抛出）。没预设的能力返回 None。
    """

    def __init__(self, responses: Dict[str, Any] | None = None) -> None:
        self.responses: Dict[str, Any] = dict(responses or {})
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def call_capability(self, name: str, **kw: Any) -> Any:
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
            "timestamp": ts,
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

    def set_request_deps(self, *, approvals: Any = None, goals: Any = None, outbox: Any = None) -> None:
        self.request_deps = (approvals, goals, outbox)

    def remember_session(self, group_id: str, session_id: str, last_msg_ts: float) -> None:
        self.remembered.append((group_id, session_id, last_msg_ts))

    def entries(self, group_id: str) -> List[dict]:
        return list(self.entries_map.get(group_id, []))

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
    """

    def __init__(
        self,
        msgs: List[Any] | None = None,
        *,
        session_id: str = "sess-1",
        info: Dict[str, Any] | None = None,
        session_error: Exception | None = None,
        info_error: Exception | None = None,
    ) -> None:
        self.msgs: List[Any] = sorted(list(msgs or []), key=lambda m: m.ts)
        self.session_id = session_id
        self.info: Dict[str, Any] = dict(info or {})
        self.session_error = session_error
        self.info_error = info_error
        self.msg_calls: List[Tuple[str, float, float, int]] = []
        self.session_calls: List[str] = []
        self.info_calls: List[str] = []

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


class FakeModelsQueue:
    """假的 Models（第二部分提炼画像用）：预设回复队列 + 记录每次调用。

    - reply_queue: 先进先出；元素是 str（chat 的返回文本）或 Exception 实例（抛出）。
      队列空了默认回 "{\"ops\": [], \"people\": []}"。
    - calls: 每次 chat 记 (role, messages, kwargs)；kwargs 含 json_mode/purpose/group_id 等。
    - 其他 FakeModels 的测试不受影响（两个类并存）。
    """

    def __init__(self, ready: bool = True, replies: List[Any] | None = None) -> None:
        self._ready = ready
        self.reply_queue: List[Any] = list(replies or [])
        self.calls: List[Tuple[str, List[dict], Dict[str, Any]]] = []

    def settings(self) -> Any:
        ready = self._ready

        class _S:
            def ready(self) -> bool:
                return ready

        return _S()

    async def chat(self, role: str, messages: List[dict], **kwargs: Any) -> Any:
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
            text = '{"ops": [], "people": []}'
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


async def two_phase_workers_run(report: Any, brief: str, kwargs: Dict[str, Any]) -> Any:
    """「预置一份老路 report」的假 workers 接上两阶段流水线的通用派发。

    - task_id 以 feeds-discover: 开头：把预置 report.data["items"] 当搜索结果
      记进撒网登记簿（discovery.record），回一条 ok 的备注 report；
    - task_id 以 feeds-verify: 开头：只把 brief 里列出的链接（「链接：<url>」行）
      对应的预置条目回给这一组核验（几组并发各回各的，不会重复交全量）；
    - 其他 task_id（feeds-recheck: 等）：老行为——预置是异常就抛，否则原样回。
    """
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    task_id = str(kwargs.get("task_id") or "")
    if isinstance(report, BaseException):
        raise report
    data = getattr(report, "data", None)
    preset_items = list(data.get("items") or []) if isinstance(data, dict) else []
    if task_id.startswith("feeds-discover:"):
        from CharTyr_MaiWork.maiwork import discovery

        if getattr(report, "ok", True) is False:
            # 预置一份「撒网子 agent 坏了」的：登记簿空着，照坏原样报回去
            return report
        results = []
        for it in preset_items:
            if not isinstance(it, dict) or not it.get("url"):
                continue
            pub = it.get("published")
            results.append(
                {
                    "title": str(it.get("title") or ""),
                    "url": str(it.get("url") or ""),
                    "snippet": str(it.get("summary") or ""),
                    "published": float(pub) if isinstance(pub, (int, float)) and pub else None,
                }
            )
        if results:
            # 预置可选 "seed_focus"：这批候选假装是从第 N 个关注点搜出来的
            # （diverse/explore 末尾方向打 angle 用）。缺省 1。
            seed_focus = data.get("seed_focus", 1) if isinstance(data, dict) else 1
            try:
                seed_focus = int(seed_focus)
            except (TypeError, ValueError):
                seed_focus = 1
            discovery.record(task_id, query="测试关注点", focus=seed_focus, provider="main", results=results)
        return WorkerReport(ok=True, summary="撒好了", data={"note": "撒好了"}, evidence=[], steps=1)
    if task_id.startswith("feeds-verify:"):
        kept = [
            dict(it)
            for it in preset_items
            if isinstance(it, dict) and str(it.get("url") or "") and str(it.get("url") or "") in str(brief)
        ]
        return WorkerReport(ok=True, summary="核验好", data={"items": kept}, evidence=[], steps=2)
    return report
