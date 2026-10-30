"""目标受阻提醒和管理员调整的正式回归测试；全部使用临时库与假发件箱。"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.goals import STALE_MODEL_REASON, STALE_TIMEOUT_REASON
from test_goal_heartbeat import FakeModels, _build_coordinator
from test_tools_admin import G1, _bind_chat, _ctx, svc  # noqa: F401


@pytest.fixture
def goal_id(svc):
    return svc.goals.create_agent(G1, title="盯公告", body="检查发布页", criteria=["第一条", "第二条", "第三条"], by_text="")


def criteria(svc, goal_id):
    return json.loads(svc.goals.get(goal_id)["criteria"])


def seed_evidence(svc, goal_id):
    items = [
        {"text": "第一条", "done": True, "evidence": "第一条的原始证据", "task_id": "T-1", "ts": 123.0},
        {"text": "第二条", "done": True, "confirmed_by": "admin", "confirmed_ts": 456.0},
        {"text": "第三条", "done": False},
    ]
    with svc.store.tx() as conn:
        conn.execute("UPDATE goals SET criteria=? WHERE id=?", (json.dumps(items), goal_id))
    return items


class Outbox:
    def __init__(self):
        self.items = []

    def enqueue(self, key, group_id, kind, payload, **kwargs):
        self.items.append({"key": key, "group_id": group_id, "payload": payload})
        return len(self.items)


def round_app(svc, ready=True):
    return SimpleNamespace(
        goals=svc.goals, store=svc.store, _settings=svc.get_settings(),
        coordinator=None, outbox=Outbox(), _models_ready=lambda: ready,
        _get_group_approvers=lambda gid: ["123456"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_kind", ["timeout", "errors", "capability"])
async def test_goals_round_repeats_each_beijing_day(svc, goal_id, reason_kind):
    """持续超时 / 三连错 / 缺能力，均跨北京时间午夜重报，日内去重。"""
    now = datetime(2026, 10, 20, 23, 59, tzinfo=clock.BJ).timestamp()
    if reason_kind == "errors":
        for _ in range(3):
            svc.goals.fail(goal_id, "无权读取来源", now)
    with svc.store.tx() as conn:
        conn.execute(
            "UPDATE goals SET created=?, heartbeat_ts=NULL, next_check_ts=? WHERE id=?",
            (now - 20000, now - 16400, goal_id),
        )
        if reason_kind == "capability":
            conn.execute("UPDATE goals SET stale_reason=? WHERE id=?", ("缺少读取该来源的能力", goal_id))
    app = round_app(svc)
    await MaiWorkApp._goals_round(app, now)
    await MaiWorkApp._goals_round(app, now + 30)
    assert len(app.outbox.items) == 1
    await MaiWorkApp._goals_round(app, now + 120)
    assert len(app.outbox.items) == 2
    assert [item["key"] for item in app.outbox.items] == [
        f"goal-stale:{goal_id}:2026-10-20", f"goal-stale:{goal_id}:2026-10-21",
    ]
    assert all(item["payload"]["at_user"] == "123456" for item in app.outbox.items)
    if reason_kind == "capability":
        assert "缺少读取该来源的能力" in app.outbox.items[-1]["payload"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "done", "cancelled"])
async def test_inactive_goal_never_notifies_even_directly(svc, goal_id, state):
    svc.goals._set_state(goal_id, state)
    with svc.store.tx() as conn:
        conn.execute("UPDATE goals SET stale_reason=? WHERE id=?", (STALE_TIMEOUT_REASON, goal_id))
    outbox = Outbox()
    assert not svc.goals.notify_stale_if_needed(goal_id, clock.now(), outbox=outbox, get_admins=lambda gid: ["123456"])
    assert outbox.items == []


@pytest.mark.asyncio
async def test_round_skips_nonserved_and_model_only(svc, goal_id):
    now = clock.now()
    with svc.store.tx() as conn:
        conn.execute("UPDATE goals SET created=?, next_check_ts=? WHERE id=?", (now - 20000, now - 16400, goal_id))
    app = round_app(svc, ready=False)
    await MaiWorkApp._goals_round(app, now)
    assert svc.goals.get(goal_id)["stale_reason"] == STALE_MODEL_REASON
    assert app.outbox.items == []
    with svc.store.tx() as conn:
        conn.execute("UPDATE goals SET group_id='99999' WHERE id=?", (goal_id,))
    app._models_ready = lambda: True
    app._get_group_approvers = lambda gid: pytest.fail("不应读取非服务群管理员")
    await MaiWorkApp._goals_round(app, now + 1)
    assert app.outbox.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action,args,expected", [
    ("add_criterion", {"text": "新增标准"}, "add"),
    ("remove_criterion", {"index": 2}, "remove"),
    ("edit_criterion", {"index": 0, "text": "修改后标准"}, "edit"),
])
async def test_criteria_changes_preserve_other_evidence(svc, goal_id, action, args, expected):
    before = seed_evidence(svc, goal_id)
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": action, **args}, _ctx())
    assert result.ok, result.error
    after = criteria(svc, goal_id)
    if expected == "add":
        assert after == before + [{"text": "新增标准", "done": False}]
    elif expected == "remove":
        assert after == before[:2]
    else:
        assert after == [{"text": "修改后标准", "done": False}, *before[1:]]
    event = svc.store.read().execute("SELECT kind FROM events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (goal_id,)).fetchone()
    assert event["kind"] == "goal.criteria_adjusted"


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [-1, 100, "bad"])
async def test_criteria_invalid_index_does_not_change_evidence(svc, goal_id, index):
    before = seed_evidence(svc, goal_id)
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": "edit_criterion", "index": index, "text": "修改"}, _ctx())
    assert not result.ok
    assert criteria(svc, goal_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action,initial,target", [("pause", "active", "paused"), ("resume", "paused", "active"), ("cancel", "active", "cancelled")])
async def test_state_change_waits_for_registered_confirmation(svc, goal_id, action, initial, target):
    if initial == "paused":
        svc.goals._set_state(goal_id, initial)
    _bind_chat(svc)
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": action}, _ctx())
    assert result.ok and result.data.get("needs_confirm")
    assert svc.goals.get(goal_id)["state"] == initial
    pending = svc.admin_pending.pending()
    assert len(pending) == 1 and pending[0]["tool"] == "goal_manage"
    confirmed = await svc.admin_pending.execute(result.data["pending_id"], True)
    assert confirmed.ok, confirmed.error
    assert svc.goals.get(goal_id)["state"] == target
    assert not (await svc.admin_pending.execute(result.data["pending_id"], True)).ok


@pytest.mark.asyncio
async def test_rejected_cancel_keeps_goal_active(svc, goal_id):
    _bind_chat(svc)
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": "cancel"}, _ctx())
    assert result.ok and result.data.get("needs_confirm")
    assert (await svc.admin_pending.execute(result.data["pending_id"], False)).ok
    assert svc.goals.get(goal_id)["state"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [1, 6, 72])
async def test_interval_saved_in_kv_and_reused_in_check(svc, goal_id, hours, monkeypatch):
    now = clock.now()
    monkeypatch.setattr(clock, "now", lambda: now)
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": "set_interval", "hours": hours}, _ctx())
    assert result.ok, result.error
    assert svc.store.kv_get(f"goal.check_interval.{goal_id}") == hours
    assert svc.goals.get(goal_id)["next_check_ts"] == now + hours * 3600
    models = FakeModels([json.dumps({"done_criteria": [], "next_check_hours": 24})])
    coordinator = _build_coordinator(svc.store, svc.get_settings(), svc.goals, models)
    await coordinator.check_goal(goal_id)
    assert svc.goals.get(goal_id)["next_check_ts"] == now + hours * 3600


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [0, -1, 73, 720, "bad", float("inf"), float("nan"), True])
async def test_invalid_interval_never_persisted(svc, goal_id, hours):
    before = svc.goals.get(goal_id)["next_check_ts"]
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": "set_interval", "hours": hours}, _ctx())
    assert not result.ok
    assert svc.store.kv_get(f"goal.check_interval.{goal_id}") is None
    assert svc.goals.get(goal_id)["next_check_ts"] == before


@pytest.mark.asyncio
async def test_goal_context_saved_and_injected_into_check_prompt(svc, goal_id):
    for action, key, value in [("set_url", "url", "https://example.test/releases"), ("set_guidance", "guidance", "只核实正式发布，忽略预览版")]:
        result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": action, key: value}, _ctx())
        assert result.ok, result.error
    context = svc.store.kv_get(f"goal.context.{goal_id}")
    assert context == {"url": "https://example.test/releases", "guidance": "只核实正式发布，忽略预览版"}
    class CaptureModels(FakeModels):
        async def chat(self, role=None, messages=None, **kwargs):
            self.messages = messages
            return await super().chat(role=role, messages=messages, **kwargs)
    models = CaptureModels(["{}"])
    await _build_coordinator(svc.store, svc.get_settings(), svc.goals, models).check_goal(goal_id)
    prompt = models.messages[-1]["content"]
    assert context["url"] in prompt
    assert context["guidance"] in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("action,key,value", [
    ("set_url", "url", ["https://example.test"]),
    ("set_url", "url", "https://example.test/" + "a" * 2048),
    ("set_guidance", "guidance", {"text": "bad"}),
    ("set_guidance", "guidance", "a" * 4001),
])
async def test_context_rejects_nonstring_and_oversized_values(svc, goal_id, action, key, value):
    result = await svc.tools.call("goal_manage", {"goal_id": goal_id, "action": action, key: value}, _ctx())
    assert not result.ok
    assert svc.store.kv_get(f"goal.context.{goal_id}") is None
