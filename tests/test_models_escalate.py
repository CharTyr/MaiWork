"""Models 的升级模型（docs/20 §5.3）：干活 lane 第 2 次没过换升级模型接着干。

- 岗位没选「做不动时换用」（profile.escalate）→ 用主模型的链（用户 2026-10-05 定）；
- 选了 → 用它；
- 升级后的首选和岗位现在的首选是同一个条目 → 没有可升级的（escalation_target 为 None）；
- 记账仍在干活桶（是干活 lane 在用，不是主模型亲自干）；
- 上下文窗口跟着升级模型走。
"""

from __future__ import annotations

import pytest

from tests.test_models import FakeEndpoint, _FakeAgents, _make_new, _new_cfg, _usage_rows


def _cfg_with_windows():
    cfg = _new_cfg()
    cfg["model_list"] = [
        {"id": "m1", "endpoint": "default", "model": "m-main", "name": "主模型展示", "context_window": 300000},
        {"id": "m2", "endpoint": "default", "model": "m-bak"},
        {"id": "w1", "endpoint": "default", "model": "m-worker", "context_window": 100000},
    ]
    return cfg


@pytest.mark.asyncio
async def test_escalate_defaults_to_main_chain(tmp_path) -> None:
    ep = FakeEndpoint({"m-main": [{"kind": "ok", "content": "升级模型回了"}]})
    store, _holder, models = _make_new(tmp_path, ep)
    r = await models.chat("worker", [{"role": "user", "content": "x"}], agent="task", escalate=True)
    assert r.text == "升级模型回了"
    assert ep.calls[0]["model"] == "m-main"
    row = _usage_rows(store)[0]
    assert row["role"] == "worker"
    await models.close()


@pytest.mark.asyncio
async def test_escalate_uses_profile_choice(tmp_path) -> None:
    ep = FakeEndpoint({})
    agents = _FakeAgents({"main": {"model": "m1"}, "task": {"model": "w1", "escalate": "m2"}})
    _store, _holder, models = _make_new(tmp_path, ep, agents=agents)
    await models.chat("worker", [{"role": "user", "content": "x"}], agent="task", escalate=True)
    assert ep.calls[0]["model"] == "m-bak"
    assert models.escalation_target("task") == {"entry_id": "m2", "label": "m-bak"}
    await models.close()


@pytest.mark.asyncio
async def test_no_escalate_keeps_own_model(tmp_path) -> None:
    ep = FakeEndpoint({})
    _store, _holder, models = _make_new(tmp_path, ep)
    await models.chat("worker", [{"role": "user", "content": "x"}], agent="task")
    assert ep.calls[0]["model"] == "m-worker"
    await models.close()


def test_target_default_is_main(tmp_path) -> None:
    _store, _holder, models = _make_new(tmp_path)
    assert models.escalation_target("task") == {"entry_id": "m1", "label": "主模型展示"}


def test_target_none_when_same_as_current(tmp_path) -> None:
    agents = _FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}})
    _store, _holder, models = _make_new(tmp_path, agents=agents)
    assert models.escalation_target("task") is None


def test_target_none_when_task_falls_back_to_main(tmp_path) -> None:
    """任务岗没选模型 = 本来就在用主模型的链：没有更强的可换。"""
    agents = _FakeAgents({"main": {"model": "m1"}})
    _store, _holder, models = _make_new(tmp_path, agents=agents)
    assert models.escalation_target("task") is None


def test_limits_follow_escalation(tmp_path) -> None:
    _store, _holder, models = _make_new(tmp_path, cfg=_cfg_with_windows())
    assert models.limits_for("task")["context_window"] == 100000
    assert models.limits_for("task", escalate=True)["context_window"] == 300000


def test_model_label_now_and_after_escalation(tmp_path) -> None:
    """docs/20 第三步：排计划时告诉领队干活的是哪个模型。"""
    _store, _holder, models = _make_new(tmp_path)
    now = models.model_label("task")
    assert now and now != "主模型展示"
    assert models.model_label("task", escalate=True) == "主模型展示"


def test_model_label_no_target_keeps_own(tmp_path) -> None:
    agents = _FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}})
    _store, _holder, models = _make_new(tmp_path, agents=agents)
    assert models.model_label("task", escalate=True) == models.model_label("task") == "主模型展示"
