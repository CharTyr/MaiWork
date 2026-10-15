"""quick_judge.py 单元测试：请求词筛、一条一次判断、阈值、上限、上下文、挑模型、思考强度。

红线：
- 请求词过滤开着、这条 @ 里一个请求词都没有 → 不调模型、也不写 pending_asks（当闲聊）；
- 一条 @ 一条模型调用（不攒批、不等待）：`enqueue` 同步零 I/O，后台立刻判这一条；
- 拿不准 / 坏 JSON / 模型出错 / 当天次数用完 → 回落 pending_asks（老慢路径照旧）；
- 收消息钩子绝不 await 模型调用，永远快速返回 continue。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.quick_judge import QuickJudge, has_request_cue
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"


# ----------------------------------------------------------------------
# 公共假对象 / 小工具
# ----------------------------------------------------------------------


def _settings(**qj):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}]},
        "jev": {"timeout_ms": 300},
        "quick_judge": qj,
    }
    settings, problems = load_settings(raw)
    assert problems == []
    return settings


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "qj.db")
    store.migrate()
    return store


class _Res:
    def __init__(self, text: str) -> None:
        self.text = text
        self.finish_reason = "stop"
        self.tool_calls: list = []


class _FakeModels:
    """假模型客户端：candidates_for_entry 可控，chat 记录全部入参。"""

    def __init__(self, replies=None, *, entry_cands=None, delay_s: float = 0.0, ready: bool = True) -> None:
        self.replies = list(replies or [])
        self.entry_cands = dict(entry_cands or {})
        self.delay_s = delay_s
        self._ready = ready
        self.calls: list[dict] = []
        self.entry_queries: list[str] = []

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self) -> bool:
                return ready

        return _S()

    def candidates_for_entry(self, entry_id):
        self.entry_queries.append(str(entry_id))
        return list(self.entry_cands.get(str(entry_id), []))

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append({"role": role, "messages": messages, **kw})
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.replies:
            item = self.replies.pop(0)
            if isinstance(item, BaseException):
                raise item
            return _Res(str(item))
        return _Res("{}")


class _BlockingModels(_FakeModels):
    """第一次 chat 卡在事件上（模拟「模型还在回」），后面的调用照常走假回复。"""

    def __init__(self, replies=None) -> None:
        super().__init__(replies)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._blocked = False

    async def chat(self, role=None, messages=None, **kw):
        if not self._blocked:
            self._blocked = True
            self.calls.append({"role": role, "messages": messages, **kw})
            self.started.set()
            await self.release.wait()
            return _Res(str(self.replies.pop(0)) if self.replies else "{}")
        return await super().chat(role=role, messages=messages, **kw)


class _Rec:
    """同步记录回调（同步回调不产生 await 点，测试时序确定）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reminders: list[tuple] = []
        self.slows: list[dict] = []

    def on_request(self, group_id, label, sure, item, title):
        self.requests.append(
            {"group_id": group_id, "label": label, "sure": sure, "item": dict(item), "title": title}
        )

    def on_reminder(self, group_id, msg):
        self.reminders.append((group_id, dict(msg)))

    def on_slow(self, reason, group_id, user_id, user_name, message_id, text):
        self.slows.append(
            {
                "reason": reason,
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "message_id": message_id,
                "text": text,
            }
        )


def _one(kind: str = "none", sure: float = 0.9, title: str = "", why: str = "") -> str:
    """新版单条形状：{"kind":…,"sure":…,"title":…,"why":…}。"""
    return json.dumps({"kind": kind, "sure": sure, "title": title, "why": why}, ensure_ascii=False)


def _reply(items) -> str:
    """旧版攒批形状（{"items":[...]}）：现在只认第一条，用来测容错。"""
    return json.dumps({"items": items}, ensure_ascii=False)


def _item(mid: str = "m1", text: str = "帮我整理一下", *, user: str = "10001", name: str = "群友", context=None) -> dict:
    return {
        "group_id": G1,
        "user_id": user,
        "user_name": name,
        "message_id": mid,
        "text": text,
        "context": list(context or []),
    }


def _make_qj(*, replies=None, store=None, rec=None, entry_cands=None, models=None, **qj_cfg):
    settings = _settings(**qj_cfg)
    rec = rec or _Rec()
    models = models if models is not None else _FakeModels(replies, entry_cands=entry_cands)
    qj = QuickJudge(
        lambda: settings,
        models,
        store=store,
        on_request=rec.on_request,
        on_reminder=rec.on_reminder,
        on_slow=rec.on_slow,
    )
    return qj, models, rec


class _Jev:
    def __init__(self, answers=None, *, available: bool = True) -> None:
        self.answers = answers
        self._available = available
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    async def ask(self, state, questions, *, purpose: str, group_id: str = "", timeout_ms=None):
        self.calls.append({"state": state, "questions": questions, "purpose": purpose, "group_id": group_id})
        return self.answers


class _Approvals:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create(self, group_id: str, **kw):
        self.created.append((str(group_id), dict(kw)))
        return {"id": "R-1", "status": "pending", "auto": False, "task_id": None, "goal_id": None}


class _Mentions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5) -> None:
        self.items.append({"group_id": str(group_id), "text": str(text), "key": str(key), "ttl_s": float(ttl_s)})


def _at_kwargs(text: str = "帮我整理一下", *, mid: str = "123", **kw):
    return hook_message(G1, text=text, is_at=True, message_id=mid, **kw)


def _make_intake(settings, *, models=None, jev=None, store=None, approvals=None, mentions=None, spawned=None, **kw):
    spawned = spawned if spawned is not None else []
    intake = Intake(
        lambda: settings,
        Signals(),
        jev=jev,
        approvals=approvals,
        mentions=mentions,
        store=store,
        models=models,
        spawn=lambda coro: spawned.append(coro),
        **kw,
    )
    return intake, spawned


async def _drain(spawned) -> None:
    """把钩子 spawn 出来的后台协程跑完（后台里再 spawn 出来的也跑，直到清空）。

    2026-10 起 Jev 判词在后台：钩子只登记 `_judge_at`，判完才回落快速判断 /
    慢路径——所以这里要抽干，不能只看第一层。
    """
    while spawned:
        await spawned.pop(0)


# ----------------------------------------------------------------------
# 1. 请求词（keyword_filter 用；纯函数）
# ----------------------------------------------------------------------


class TestRequestCue:
    @pytest.mark.parametrize(
        "text",
        [
            "宝宝你看我给你买的鞋",
            "请和我交往",
            "是不是你干的",
            "[image] 分析一下图中的发言",
            "来点图图",
            "画来看看",
            "",  # 空
        ],
    )
    def test_banter_and_questions_have_no_cue(self, text: str) -> None:
        assert has_request_cue(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            "帮我搞个不要钱的k3过来",
            "我可以做张开黑语音排障卡，把…对照步骤整理成文档",
            "提醒我明天下午三点开会",
            "@小明 查一下这个工具的用法",
            "麻烦看看这份材料，总结成一份表",
            "记得到时候通知我一声",
        ],
    )
    def test_request_texts_have_a_cue(self, text: str) -> None:
        assert has_request_cue(text) is True

    def test_at_name_is_stripped_before_matching(self) -> None:
        """「@名字」先去掉再找请求词（不然名字里带个「查」字就白花钱）。"""
        assert has_request_cue("@帮我 你好啊") is False

    def test_name_glued_to_cue_still_counts(self) -> None:
        """名字和请求词黏在一起（没有空格）时不删 —— 宁可多花一次模型钱，也不漏派活。"""
        assert has_request_cue("@小明帮我查一下") is True


# ----------------------------------------------------------------------
# 2. 一条一次判断（不攒批）/ 上下文 / 去重
# ----------------------------------------------------------------------


class TestOneShot:
    @pytest.mark.asyncio
    async def test_one_message_one_call_without_waiting(self) -> None:
        """排进来就判：不用等窗口，一条消息一次调用。"""
        qj, models, rec = _make_qj(replies=[_one("none", 0.9)])
        assert qj.enqueue(G1, _item("m1", "帮我整理一下")) is True
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 1
        text = models.calls[0]["messages"][-1]["content"]
        assert "帮我整理一下" in text
        assert rec.requests == [] and rec.reminders == [] and rec.slows == []
        await qj.close()

    @pytest.mark.asyncio
    async def test_two_at_two_calls(self) -> None:
        qj, models, rec = _make_qj(replies=[_one("none", 0.9), _one("none", 0.9)])
        qj.enqueue(G1, _item("m1", "帮我整理一下"))
        qj.enqueue(G1, _item("m2", "帮我查一下"))
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 2
        texts = [c["messages"][-1]["content"] for c in models.calls]
        assert any("帮我整理一下" in t for t in texts) and any("帮我查一下" in t for t in texts)
        # 一次调用只看一条消息，不把两条混在一起
        assert not any("帮我整理一下" in t and "帮我查一下" in t for t in texts)
        await qj.close()

    @pytest.mark.asyncio
    async def test_context_lines_in_prompt(self) -> None:
        qj, models, _rec = _make_qj(replies=[_one("none", 0.9)])
        ctx = [
            {"speaker": "MaiBot", "text": "昨晚那个活动结束了", "message_id": "c1", "bot": True},
            {"speaker": "小明", "text": "那我们周末干什么", "message_id": "c2", "bot": False},
        ]
        qj.enqueue(G1, _item("m1", "帮我把这两天的聊天整理成一份计划", context=ctx))
        await asyncio.wait_for(qj.join(), 2)
        text = models.calls[0]["messages"][-1]["content"]
        assert "MaiBot: 昨晚那个活动结束了" in text
        assert "小明: 那我们周末干什么" in text
        await qj.close()

    @pytest.mark.asyncio
    async def test_same_message_id_never_judged_twice(self) -> None:
        """同一条 @ 排两次（钩子重复触发）只调一次模型。"""
        qj, models, _rec = _make_qj(replies=[_one("none", 0.9)])
        qj.enqueue(G1, _item("m1"))
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 1
        assert qj.enqueue(G1, _item("m1")) is True   # 判过了：收下但不再花钱
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 1
        await qj.close()

    @pytest.mark.asyncio
    async def test_message_arriving_during_the_model_call_is_judged(self) -> None:
        """第一条还在调模型时又来了 @：各自一条，不互相等。"""
        models = _BlockingModels(replies=[_one("none", 0.9), _one("none", 0.9)])
        qj, _models, _rec = _make_qj(models=models)
        qj.enqueue(G1, _item("m1", "帮我整理一下"))
        await asyncio.wait_for(models.started.wait(), 1)   # 第一次调用已经在路上
        qj.enqueue(G1, _item("m2", "帮我查一下"))           # 调用过程中又来一条
        models.release.set()
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 2
        assert "帮我整理一下" in models.calls[0]["messages"][-1]["content"]
        assert "帮我查一下" in models.calls[1]["messages"][-1]["content"]
        await qj.close()

    @pytest.mark.asyncio
    async def test_old_items_shape_still_parsed(self) -> None:
        """旧版 {"items":[…]} 形状（只有一条）照样认，别因为提示词换了就读不出来。"""
        qj, _models, rec = _make_qj(
            replies=[_reply([{"i": 1, "kind": "prepare", "sure": 0.9, "title": "整理成文档"}])]
        )
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert len(rec.requests) == 1 and rec.requests[0]["title"] == "整理成文档"
        await qj.close()

    @pytest.mark.asyncio
    async def test_empty_items_shape_goes_slow(self) -> None:
        qj, _models, rec = _make_qj(replies=[_reply([])])
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert rec.requests == [] and len(rec.slows) == 1
        await qj.close()

    @pytest.mark.asyncio
    async def test_close_hands_inflight_back_to_slow(self) -> None:
        """停机：在跑的调用被取消，还没判的交回慢路径，一条都不丢。"""
        models = _BlockingModels(replies=[_one("none", 0.9)])
        qj, _models, rec = _make_qj(models=models)
        qj.enqueue(G1, _item("m1", "帮我整理一下"))
        await asyncio.wait_for(models.started.wait(), 1)
        qj.enqueue(G1, _item("m2", "帮我查一下"))   # 还没开始调
        await qj.close()
        assert {s["message_id"] for s in rec.slows} == {"m1", "m2"}
        assert all("打断" in s["reason"] for s in rec.slows)
        assert [t for t in qj._tasks if not t.done()] == []

    def test_enqueue_returns_false_without_event_loop(self) -> None:
        """没有事件循环（钩子外直接调）：不收，让调用方走慢路径，别把这条吞了。"""
        qj, models, rec = _make_qj(replies=[_one("none", 0.9)])
        assert qj.enqueue(G1, _item("m1")) is False
        assert models.calls == [] and rec.slows == [] and qj._judged == {}


# ----------------------------------------------------------------------
# 3. 每种结论的门槛
# ----------------------------------------------------------------------


class TestDecisions:
    async def _decide(self, reply, item=None, **qj_cfg):
        qj, models, rec = _make_qj(replies=[reply], **qj_cfg)
        qj.enqueue(G1, item or _item())
        await asyncio.wait_for(qj.join(), 2)
        await qj.close()
        return rec, models

    @pytest.mark.asyncio
    async def test_prepare_high_sure_creates_request(self) -> None:
        rec, _ = await self._decide(_one("prepare", 0.85, "整理成一份文档", why="他把记录整理成文档"))
        assert len(rec.requests) == 1
        assert rec.requests[0]["label"] == "prepare"
        assert rec.requests[0]["title"] == "整理成一份文档"
        assert rec.requests[0]["sure"] == pytest.approx(0.85)
        assert rec.requests[0]["item"]["_why"] == "他把记录整理成文档"
        assert rec.slows == []

    @pytest.mark.asyncio
    async def test_goal_high_sure_creates_request(self) -> None:
        rec, _ = await self._decide(_one("goal", 0.9, "盯着降价"))
        assert len(rec.requests) == 1 and rec.requests[0]["label"] == "goal"

    @pytest.mark.asyncio
    async def test_prepare_low_sure_goes_slow(self) -> None:
        rec, _ = await self._decide(_one("prepare", 0.79, "x"))
        assert rec.requests == []
        assert len(rec.slows) == 1 and "拿不准" in rec.slows[0]["reason"]

    @pytest.mark.asyncio
    async def test_reminder_sure_7_invokes_callback(self) -> None:
        rec, _ = await self._decide(_one("reminder", 0.7, "提醒开会"))
        assert len(rec.reminders) == 1
        gid, msg = rec.reminders[0]
        assert gid == G1 and msg["message_id"] == "m1" and msg["text"] == "帮我整理一下"
        assert rec.requests == [] and rec.slows == []

    @pytest.mark.asyncio
    async def test_reminder_low_sure_goes_slow(self) -> None:
        rec, _ = await self._decide(_one("reminder", 0.69, "x"))
        assert rec.reminders == [] and len(rec.slows) == 1

    @pytest.mark.asyncio
    async def test_none_confident_records_nothing(self) -> None:
        rec, _ = await self._decide(_one("none", 0.7))
        assert rec.requests == [] and rec.reminders == [] and rec.slows == []

    @pytest.mark.asyncio
    async def test_none_low_sure_goes_slow(self) -> None:
        rec, _ = await self._decide(_one("none", 0.5))
        assert len(rec.slows) == 1

    @pytest.mark.asyncio
    async def test_bad_json_goes_slow(self) -> None:
        rec, _ = await self._decide("这不是 JSON")
        assert rec.requests == [] and len(rec.slows) == 1
        assert "拿不准" in rec.slows[0]["reason"]

    @pytest.mark.asyncio
    async def test_bad_sure_goes_slow(self) -> None:
        rec, _ = await self._decide(_one("prepare", "很高", "x"))
        assert rec.requests == [] and len(rec.slows) == 1

    @pytest.mark.asyncio
    async def test_model_error_goes_slow(self) -> None:
        rec, _ = await self._decide(RuntimeError("模型炸了"))
        assert rec.requests == [] and len(rec.slows) == 1

    @pytest.mark.asyncio
    async def test_title_truncated_to_20(self) -> None:
        rec, _ = await self._decide(_one("prepare", 0.9, "一二三四五六七八九十一二三四五六七八九十十一"))
        assert len(rec.requests[0]["title"]) <= 20

    @pytest.mark.asyncio
    async def test_json_fence_stripped(self) -> None:
        rec, _ = await self._decide("```json\n" + _one("prepare", 0.9, "整理") + "\n```")
        assert len(rec.requests) == 1


# ----------------------------------------------------------------------
# 4. 每天次数上限（按群按北京日期，跨重启不重置）
# ----------------------------------------------------------------------


class TestDailyCap:
    @pytest.mark.asyncio
    async def test_cap_reached_no_more_calls_and_slow_path(self, tmp_path) -> None:
        store = _store(tmp_path)
        day = clock.day_key(clock.now())
        qj, models, rec = _make_qj(store=store, daily_max=1, replies=[_one("none", 0.9), _one("none", 0.9)])
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 1
        assert store.kv_get(f"quick_judge.calls.{G1}.{day}") == 1
        qj.enqueue(G1, _item("m2"))
        await asyncio.wait_for(qj.join(), 2)
        assert len(models.calls) == 1
        assert [s["message_id"] for s in rec.slows] == ["m2"]
        assert rec.slows[0]["reason"] == "快速判断今天次数用完"
        await qj.close()

    @pytest.mark.asyncio
    async def test_cap_survives_restart(self, tmp_path) -> None:
        store = _store(tmp_path)
        qj, _models, _rec = _make_qj(store=store, daily_max=1, replies=[_one("none", 0.9)])
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        await qj.close()
        # 新进程（新实例、同一个库）：今天的次数不清零
        qj2, models2, rec2 = _make_qj(store=store, daily_max=1, replies=[_one("none", 0.9)])
        qj2.enqueue(G1, _item("m2"))
        await asyncio.wait_for(qj2.join(), 2)
        assert models2.calls == []
        assert rec2.slows and rec2.slows[0]["reason"] == "快速判断今天次数用完"
        await qj2.close()

    @pytest.mark.asyncio
    async def test_daily_max_zero_never_calls(self, tmp_path) -> None:
        store = _store(tmp_path)
        qj, models, rec = _make_qj(store=store, daily_max=0, replies=[_one("none", 0.9)])
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert models.calls == []
        assert rec.slows and rec.slows[0]["reason"] == "快速判断今天次数用完"
        await qj.close()


# ----------------------------------------------------------------------
# 5. 挑模型 / 强度
# ----------------------------------------------------------------------


class TestModelChoice:
    @pytest.mark.asyncio
    async def test_configured_entry_candidates_passed(self) -> None:
        sentinel = [object()]
        models = _FakeModels(replies=[_one("none", 0.9)], entry_cands={"q1": sentinel})
        qj, _models, _rec = _make_qj(models=models, model="q1")
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert models.entry_queries == ["q1"]
        assert models.calls[0]["_candidates"] == sentinel
        await qj.close()

    @pytest.mark.asyncio
    async def test_unknown_entry_falls_back_to_main_chain(self) -> None:
        models = _FakeModels(replies=[_one("none", 0.9)], entry_cands={})
        qj, _models, _rec = _make_qj(models=models, model="没有这条")
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert models.entry_queries == ["没有这条"]
        assert models.calls[0].get("_candidates") is None  # 用主模型的链
        await qj.close()

    @pytest.mark.asyncio
    async def test_empty_model_uses_main_chain_without_lookup(self) -> None:
        models = _FakeModels(replies=[_one("none", 0.9)])
        qj, _models, _rec = _make_qj(models=models, model="")
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        assert models.entry_queries == []
        assert models.calls[0].get("_candidates") is None
        await qj.close()

    @pytest.mark.asyncio
    async def test_call_shape(self) -> None:
        """一条消息就是一次 chat：主模型、JSON、低强度、便宜、短超时。"""
        qj, models, _rec = _make_qj(replies=[_one("none", 0.9)])
        qj.enqueue(G1, _item("m1"))
        await asyncio.wait_for(qj.join(), 2)
        kw = models.calls[0]
        assert kw["role"] is None and kw["agent"] == "main"
        assert kw["json_mode"] is True
        assert kw["purpose"] == "intake.quick_judge"
        assert kw["group_id"] == G1
        assert kw["retries"] == 1
        assert "max_tokens" not in kw  # 思考和答案共用额度，沿用模型条目配置
        assert kw["timeout"] == 60
        assert kw["effort"] == "low"
        assert isinstance(kw["messages"], list) and len(kw["messages"]) >= 2
        await qj.close()


# ----------------------------------------------------------------------
# 6. 收消息钩子的接线（回落 / 快速返回 / via / Jev 上下文）
# ----------------------------------------------------------------------


class TestIntakeRouting:
    @pytest.mark.asyncio
    async def test_jev_confident_path_unchanged(self) -> None:
        settings = _settings()
        ap, mn = _Approvals(), _Mentions()
        models = _FakeModels()
        jev = _Jev({"kind": ("prepare", 0.9, 0.9)})
        intake, spawned = _make_intake(settings, models=models, jev=jev, approvals=ap, mentions=mn)
        await intake.handle(_at_kwargs())
        await _drain(spawned)
        assert len(ap.created) == 1
        assert ap.created[0][1]["via"] == "群里 @ · Jev 判断是「准备东西」（把握 0.90）"
        assert models.calls == []          # 没走快速判断
        assert intake.slow_queue == []     # 也没写慢路径
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_jev_unsure_goes_to_quick_judge(self) -> None:
        settings = _settings()
        ap, mn = _Approvals(), _Mentions()
        reply = _one("prepare", 0.9, "整理成一份文档", why="他把这几天的记录要整理成文档")
        models = _FakeModels(replies=[reply])
        jev = _Jev({"kind": ("prepare", 0.5, 0.65)})
        intake, spawned = _make_intake(settings, models=models, jev=jev, approvals=ap, mentions=mn)
        await intake.handle(_at_kwargs("帮我把这几天的记录整理成一份文档", mid="555"))
        assert ap.created == []            # 钩子里不建（后台还没跑）
        assert intake.slow_queue == []     # 也没写慢路径
        await _drain(spawned)              # 后台问 Jev：判不准 → 回落快速判断
        await intake.quick_judge.join()
        assert len(ap.created) == 1
        assert ap.created[0][1]["via"] == "群里 @ · 快速判断：他把这几天的记录要整理成文档"
        assert ap.created[0][1]["title"] == "整理成一份文档"
        assert ap.created[0][1]["message_id"] == "555"
        assert len(models.calls) == 1
        assert models.calls[0]["purpose"] == "intake.quick_judge"
        assert models.calls[0]["effort"] == "low"
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_via_truncated_to_60(self) -> None:
        settings = _settings()
        ap = _Approvals()
        models = _FakeModels(replies=[_one("prepare", 0.9, "整理成一份文档", why="很" * 100)])
        jev = _Jev({"kind": ("prepare", 0.5, 0.65)})
        intake, spawned = _make_intake(settings, models=models, jev=jev, approvals=ap)
        await intake.handle(_at_kwargs("帮我把这几天的记录整理成一份文档", mid="556"))
        await _drain(spawned)
        await intake.quick_judge.join()
        via = ap.created[0][1]["via"]
        assert len(via) <= 60
        assert via.startswith("群里 @ · 快速判断：")
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_via_falls_back_when_why_empty(self) -> None:
        settings = _settings()
        ap = _Approvals()
        models = _FakeModels(replies=[_one("goal", 0.9, "", why="")])
        jev = _Jev({"kind": ("goal", 0.5, 0.5)})
        intake, spawned = _make_intake(settings, models=models, jev=jev, approvals=ap)
        await intake.handle(_at_kwargs("帮我把这几天的记录整理成一份文档", mid="557"))
        await _drain(spawned)
        await intake.quick_judge.join()
        assert ap.created[0][1]["via"] == "群里 @ · 快速判断是「帮忙盯着或做成」"
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_jev_unavailable_goes_to_quick_judge(self) -> None:
        settings = _settings()
        ap = _Approvals()
        models = _FakeModels(replies=[_one("none", 0.9)])
        jev = _Jev(None, available=False)
        intake, spawned = _make_intake(settings, models=models, jev=jev, approvals=ap)
        await intake.handle(_at_kwargs())
        assert jev.calls == []
        await _drain(spawned)
        await intake.quick_judge.join()
        assert len(models.calls) == 1 and ap.created == []
        assert intake.slow_queue == []
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_quick_judge_disabled_goes_slow(self) -> None:
        settings = _settings(enabled=False)
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models)
        await intake.handle(_at_kwargs("帮我把记录整理成文档", mid="556"))
        await _drain(spawned)
        assert models.calls == []
        rows = intake.slow_queue
        assert len(rows) == 1 and rows[0]["message_id"] == "556"

    @pytest.mark.asyncio
    async def test_keyword_filter_on_banter_skipped_entirely(self) -> None:
        settings = _settings(keyword_filter=True)
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models)
        await intake.handle(_at_kwargs("宝宝你看我给你买的鞋", mid="557"))
        await _drain(spawned)              # 后台判词 → 没有请求词 → 当闲聊
        assert models.calls == []
        assert intake.slow_queue == []     # 不写 pending_asks：读群时照样能看见这条
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_keyword_filter_on_with_cue_still_judged(self) -> None:
        settings = _settings(keyword_filter=True)
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models)
        await intake.handle(_at_kwargs("帮我搞个不要钱的k3过来", mid="558"))
        await _drain(spawned)
        await intake.quick_judge.join()
        assert len(models.calls) == 1
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_keyword_filter_off_banter_still_judged(self) -> None:
        settings = _settings(keyword_filter=False)
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models)
        await intake.handle(_at_kwargs("宝宝你看我给你买的鞋", mid="559"))
        await _drain(spawned)
        await intake.quick_judge.join()
        assert len(models.calls) == 1
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_bad_json_lands_in_pending_asks(self, tmp_path) -> None:
        store = _store(tmp_path)
        settings = _settings()
        models = _FakeModels(replies=["这不是 JSON"])
        intake, spawned = _make_intake(settings, models=models, store=store)
        await intake.handle(_at_kwargs("帮我整理成文档", mid="600"))
        await _drain(spawned)
        await intake.quick_judge.join()
        rows = intake.slow_queue
        assert len(rows) == 1
        assert rows[0]["message_id"] == "600"
        assert rows[0]["reason"].startswith("快速判断拿不准（")
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_cap_reached_lands_in_pending_asks(self, tmp_path) -> None:
        store = _store(tmp_path)
        settings = _settings(daily_max=1)
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models, store=store)
        await intake.handle(_at_kwargs("帮我整理成文档", mid="601"))
        await _drain(spawned)
        await intake.quick_judge.join()
        assert len(models.calls) == 1
        await intake.handle(_at_kwargs("帮我查一下这个", mid="602"))
        await _drain(spawned)
        await intake.quick_judge.join()
        assert len(models.calls) == 1
        rows = {r["message_id"]: r for r in intake.slow_queue}
        assert rows["602"]["reason"] == "快速判断今天次数用完"
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_hook_returns_quickly_and_never_awaits_model(self) -> None:
        settings = _settings()
        models = _FakeModels(replies=[_one("none", 0.9)], delay_s=0.5)
        intake, spawned = _make_intake(settings, models=models)
        start = time.monotonic()
        out = await intake.handle(_at_kwargs("帮我整理成文档", mid="603"))
        elapsed = time.monotonic() - start
        assert out == {"action": "continue"}
        assert elapsed < 0.3
        assert models.calls == []          # 模型调用在后台，钩子不等
        await _drain(spawned)
        await intake.quick_judge.join()
        assert len(models.calls) == 1
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_ring_records_bot_and_plain_messages_as_context(self) -> None:
        settings = _settings()
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models, bot_qq="424242")
        # 机器人自己发的一条 + 群友的一句闲聊，都该出现在给快速判断的上下文里
        await intake.handle(hook_message(G1, user_id="424242", text="我昨天说过活动结束了", message_id="c1"))
        await intake.handle(hook_message(G1, text="那我们周末干什么", message_id="c2"))
        await intake.handle(_at_kwargs("帮我把这两天的安排整理成一份计划", mid="604"))
        await _drain(spawned)
        await intake.quick_judge.join()
        text = models.calls[0]["messages"][-1]["content"]
        assert "MaiBot: 我昨天说过活动结束了" in text
        assert "群友: 那我们周末干什么" in text
        await intake.quick_judge.close()

    @pytest.mark.asyncio
    async def test_ring_keeps_only_last_eight(self) -> None:
        settings = _settings()
        models = _FakeModels(replies=[_one("none", 0.9)])
        intake, spawned = _make_intake(settings, models=models)
        for i in range(10):
            await intake.handle(hook_message(G1, text=f"闲聊第{i}条", message_id=f"r{i}"))
        await intake.handle(_at_kwargs("帮我整理成文档", mid="605"))
        await _drain(spawned)
        await intake.quick_judge.join()
        text = models.calls[0]["messages"][-1]["content"]
        assert "闲聊第9条" in text
        assert "闲聊第0条" not in text      # 只留最近 8 条
        await intake.quick_judge.close()


# ----------------------------------------------------------------------
# 6b. Jev 也带上下文（最多 4 条，USER 在最后）
# ----------------------------------------------------------------------


class TestJevContext:
    @pytest.mark.asyncio
    async def test_state_has_context_with_user_last(self) -> None:
        settings = _settings()
        jev = _Jev({"kind": ("none", 0.9, 0.9)})
        intake, spawned = _make_intake(settings, jev=jev, bot_qq="424242")
        await intake.handle(hook_message(G1, user_id="424242", text="机器人说过的话", message_id="c1"))
        await intake.handle(hook_message(G1, text="群友闲聊一句", message_id="c2"))
        await intake.handle(_at_kwargs("帮我整理一下", mid="c3"))
        await _drain(spawned)               # 判词在后台，抽干后才看得到 Jev 请求
        msgs = jev.calls[0]["state"]["messages"]
        assert [m["speaker"] for m in msgs] == ["MaiBot", "群友", "USER"]
        assert msgs[-1]["text"] == "帮我整理一下"
        assert msgs[0]["text"] == "机器人说过的话"
        # 只带上下文文字，不把内部字段（message_id / bot）塞给 Jev
        assert set(msgs[0]) == {"speaker", "text"}

    @pytest.mark.asyncio
    async def test_state_keeps_only_last_four_context_messages(self) -> None:
        settings = _settings()
        jev = _Jev({"kind": ("none", 0.9, 0.9)})
        intake, spawned = _make_intake(settings, jev=jev)
        for i in range(6):
            await intake.handle(hook_message(G1, text=f"第{i}句", message_id=f"c{i}"))
        await intake.handle(_at_kwargs("帮我整理一下", mid="c9"))
        await _drain(spawned)
        msgs = jev.calls[0]["state"]["messages"]
        assert [m["speaker"] for m in msgs] == ["群友"] * 4 + ["USER"]
        assert msgs[0]["text"] == "第2句" and msgs[3]["text"] == "第5句"

    @pytest.mark.asyncio
    async def test_state_drops_oldest_context_over_800_chars(self) -> None:
        settings = _settings()
        jev = _Jev({"kind": ("none", 0.9, 0.9)})
        intake, spawned = _make_intake(settings, jev=jev)
        for i in range(4):
            await intake.handle(hook_message(G1, text="甲" * 120, message_id=f"c{i}"))
        await intake.handle(_at_kwargs("乙" * 500, mid="c9"))
        await _drain(spawned)
        msgs = jev.calls[0]["state"]["messages"]
        assert msgs[-1]["speaker"] == "USER" and len(msgs[-1]["text"]) == 500
        assert sum(len(m["text"]) for m in msgs) <= 800
        assert len(msgs) == 3          # 4 条上下文太长了，从最老的开始丢

    @pytest.mark.asyncio
    async def test_state_text_truncated_to_500(self) -> None:
        settings = _settings()
        jev = _Jev({"kind": ("none", 0.9, 0.9)})
        intake, spawned = _make_intake(settings, jev=jev)
        await intake.handle(_at_kwargs("长" * 800, mid="c1"))
        await _drain(spawned)
        msgs = jev.calls[0]["state"]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["speaker"] == "USER" and len(msgs[0]["text"]) == 500

    def test_instructions_text_points_at_the_last_message(self) -> None:
        from CharTyr_MaiWork.maiwork.intake import _AT_QUESTIONS

        assert _AT_QUESTIONS["kind"]["instructions"] == (
            "群里有人 @ MaiBot。最后一条（USER）是这条 @ 的消息，前面几条只是上下文。这条 @ 是在请 MaiBot 做什么？"
        )
        # 判准（criteria）不动
        c = _AT_QUESTIONS["kind"]["criteria"]
        assert c["prepare"].startswith("请 MaiBot 准备/整理/调研")
        assert c["goal"].startswith("请 MaiBot 在网上帮忙盯着")
        assert c["reminder"].startswith("请 MaiBot 到时间提醒")
        assert c["none"].startswith("都不是")

    def test_openai_decisions_multi_message_state(self) -> None:
        """openai_decisions 协议：多条消息的状态要进 input，不能丢也不能炸。"""
        from CharTyr_MaiWork.maiwork.jev import build_body

        state = {
            "messages": [
                {"speaker": "群友", "text": "前面说的是活动"},
                {"speaker": "USER", "text": "帮我整理一下"},
            ]
        }
        body = build_body(
            "openai_decisions", "m", state,
            {"kind": {"type": "choice", "instructions": "在请 MaiBot 做什么？", "criteria": {"prepare": "准备", "none": "都不是"}}},
        )
        assert isinstance(body["input"], str)
        assert "帮我整理一下" in body["input"] and "前面说的是活动" in body["input"]
        assert body["questions"][0]["type"] == "choice"
        # systemone 原样带走 messages
        assert build_body("systemone", "m", state, {})["state"] == state


# ----------------------------------------------------------------------
# 7. models.chat 的 effort 口 + candidates_for_entry（真 Models + 假端点）
# ----------------------------------------------------------------------


class _Agents:
    def __init__(self, mapping: dict | None = None) -> None:
        self._m = mapping or {}

    def profile(self, kind: str) -> dict:
        d = {"kind": kind, "title": kind, "model": "", "effort": "", "backup": "", "enabled": True}
        d.update(self._m.get(kind, {}))
        return dict(d)


def _real_models(tmp_path, cfg: dict, handler, agents=None) -> Models:
    store = Store(tmp_path / "models.db")
    store.migrate()
    settings, _ = load_settings(cfg)
    return Models(
        store,
        lambda: settings,
        transport=httpx.MockTransport(handler),
        agents=agents if agents is not None else _Agents({"main": {"model": "m1"}, "task": {"model": "m1"}}),
    )


def _ok_handler(bodies: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            },
        )

    return handler


def _models_cfg(*entries) -> dict:
    return {
        "endpoints": [
            {"id": "e1", "protocol": "openai", "base_url": "https://q.test/v1", "api_key": "qjsecret", "retries": 0},
        ],
        "model_list": [dict(e) for e in entries],
    }


class TestModelsEffortAndEntry:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["main", "explicit", "backup"])
    async def test_quick_judge_uses_each_model_output_limit(self, tmp_path, route) -> None:
        bodies: list[dict] = []
        cfg = _models_cfg(
            {"id": "m1", "endpoint": "e1", "model": "m-main", "max_tokens": 8192},
            {"id": "m2", "endpoint": "e1", "model": "m-backup", "max_tokens": 16384},
            {"id": "q1", "endpoint": "e1", "model": "m-quick", "max_tokens": 32768},
        )
        cfg["endpoints"][0]["retry_delay_s"] = 1

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            # 模拟思考模型：额度太小只思考不出答案；备用场景让主模型始终为空。
            empty = body["max_tokens"] <= 600 or (route == "backup" and body["model"] == "m-main")
            return httpx.Response(200, json={
                "choices": [{"index": 0, "message": {
                    "role": "assistant",
                    "content": "" if empty else _one("prepare", 0.95, "整理记录"),
                }, "finish_reason": "length" if empty else "stop"}],
                "usage": {},
            })

        models = _real_models(tmp_path, cfg, handler, agents=_Agents({
            "main": {"model": "m1", "backup": "m2"},
        }))
        qj, _, rec = _make_qj(models=models, model="q1" if route == "explicit" else "")
        try:
            qj.enqueue(G1, _item())
            await asyncio.wait_for(qj.join(), 5)
            expected = {
                "main": [("m-main", 8192)],
                "explicit": [("m-quick", 32768)],
                "backup": [("m-main", 8192), ("m-main", 8192), ("m-backup", 16384)],
            }[route]
            assert [(b["model"], b["max_tokens"]) for b in bodies] == expected
            assert len(rec.requests) == 1
            assert rec.slows == []
        finally:
            await qj.close()
            await models.close()

    def test_candidates_for_entry(self, tmp_path) -> None:
        cfg = _models_cfg({"id": "q1", "endpoint": "e1", "model": "quick-model", "efforts": ["low"]})
        models = _real_models(tmp_path, cfg, _ok_handler([]))
        cands = models.candidates_for_entry("q1")
        assert [c.service_model for c in cands] == ["quick-model"]
        assert models.candidates_for_entry("没有这条") == []
        assert models.candidates_for_entry("") == []

    @pytest.mark.asyncio
    async def test_effort_kwarg_overrides_profile(self, tmp_path) -> None:
        bodies: list[dict] = []
        cfg = _models_cfg({"id": "m1", "endpoint": "e1", "model": "m-main", "efforts": ["low", "high"]})
        models = _real_models(tmp_path, cfg, _ok_handler(bodies), agents=_Agents({"main": {"model": "m1", "effort": "high"}}))
        await models.chat(agent="main", messages=[{"role": "user", "content": "x"}])
        assert bodies[-1]["reasoning_effort"] == "high"     # 老行为：读岗位 profile
        await models.chat(agent="main", messages=[{"role": "user", "content": "x"}], effort="low")
        assert bodies[-1]["reasoning_effort"] == "low"      # 传了就以传入的为准
        await models.close()

    @pytest.mark.asyncio
    async def test_effort_not_sent_when_entry_has_no_such_level(self, tmp_path) -> None:
        bodies: list[dict] = []
        cfg = _models_cfg({"id": "m1", "endpoint": "e1", "model": "m-main", "efforts": ["high"]})
        models = _real_models(tmp_path, cfg, _ok_handler(bodies), agents=_Agents({"main": {"model": "m1", "effort": "high"}}))
        await models.chat(agent="main", messages=[{"role": "user", "content": "x"}], effort="low")
        assert "reasoning_effort" not in bodies[-1]         # 条目没勾 low → 不发
        await models.close()

    @pytest.mark.asyncio
    async def test_entry_candidates_used_for_the_call(self, tmp_path) -> None:
        bodies: list[dict] = []
        cfg = _models_cfg({"id": "q1", "endpoint": "e1", "model": "quick-model", "efforts": ["low"]})
        models = _real_models(tmp_path, cfg, _ok_handler(bodies), agents=_Agents({}))
        cands = models.candidates_for_entry("q1")
        await models.chat(
            agent="main", messages=[{"role": "user", "content": "x"}], _candidates=cands, effort="low"
        )
        assert bodies[-1]["model"] == "quick-model"
        assert bodies[-1]["reasoning_effort"] == "low"
        await models.close()


# ----------------------------------------------------------------------
# 8. 配置规范化 + 全部设置的高级项 + 网页健康行
# ----------------------------------------------------------------------


class TestConfigAndWeb:
    def test_defaults(self) -> None:
        s = _settings()
        qj = s.quick_judge
        assert qj.enabled is True
        assert qj.model == ""
        assert qj.keyword_filter is False
        assert qj.daily_max == 30
        assert not hasattr(qj, "batch_wait_s")   # 攒批参数已删

    def test_daily_max_clamped_with_chinese_problems(self) -> None:
        settings, problems = load_settings(
            {
                "plugin": {"enabled": True},
                "groups": {"serve": [{"group": f"qq:{G1}"}]},
                "quick_judge": {"daily_max": 999},
            }
        )
        assert settings.quick_judge.daily_max == 500
        assert any("daily_max" in p for p in problems)

    def test_old_config_with_batch_wait_s_loads_clean(self) -> None:
        """老 config.toml 还留着 batch_wait_s：能读、不报问题。"""
        settings, problems = load_settings(
            {
                "plugin": {"enabled": True, "config_version": "0.4.8"},
                "groups": {"serve": [{"group": f"qq:{G1}"}]},
                "quick_judge": {"enabled": True, "keyword_filter": True, "daily_max": 12, "batch_wait_s": 15},
            }
        )
        assert problems == []
        qj = settings.quick_judge
        assert qj.daily_max == 12 and qj.keyword_filter is True
        assert not hasattr(qj, "batch_wait_s")

    def test_old_config_with_batch_wait_s_survives_host_normalize(self) -> None:
        """宿主那层（PluginConfigBase，extra="ignore"）也认：老的 batch_wait_s 被忽略、不报错。"""
        from CharTyr_MaiWork.plugin import create_plugin

        cfg, _changed = create_plugin().normalize_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "0.4.8"},
                "groups": {"serve": [{"group": f"qq:{G1}"}]},
                "quick_judge": {"enabled": True, "keyword_filter": True, "daily_max": 12, "batch_wait_s": 15},
            }
        )
        assert cfg["plugin"]["enabled"] is False
        assert "groups" in cfg
        assert "batch_wait_s" not in cfg["quick_judge"]
        assert cfg["quick_judge"]["daily_max"] == 12 and cfg["quick_judge"]["keyword_filter"] is True

    def test_daily_max_is_advanced_and_batch_wait_gone(self, tmp_path) -> None:
        from CharTyr_MaiWork.maiwork import rules

        store = _store(tmp_path)
        try:
            fields = {
                f["key"]: f
                for s in rules.config_view(_settings(), store)["sections"]
                for f in s["fields"]
            }
            assert fields["quick_judge.daily_max"]["advanced"] is True
            assert fields["quick_judge.enabled"]["advanced"] is False
            assert "quick_judge.keyword_filter" in fields
            assert "quick_judge.batch_wait_s" not in fields
        finally:
            store.close()

    def test_health_line(self, tmp_path) -> None:
        import types

        from CharTyr_MaiWork.maiwork.console.views import _quick_judge_health
        from CharTyr_MaiWork.maiwork.quick_judge import calls_key

        store = _store(tmp_path)
        day = clock.day_key(clock.now())
        with store.tx() as conn:
            store.kv_set(conn, calls_key("111", day), 2)
            store.kv_set(conn, calls_key("222", day), 1)
        svc = types.SimpleNamespace(store=store, get_settings=lambda: _settings())
        h = _quick_judge_health(svc)
        assert h["key"] == "quick_judge"
        assert h["state"] == "ok"
        assert h["text"] == "开 · 今天判断 3 次"
        # 关着 → off
        svc_off = types.SimpleNamespace(store=store, get_settings=lambda: _settings(enabled=False))
        assert _quick_judge_health(svc_off)["state"] == "off"
