"""有效模型链（docs/13 A01/A02，2026-10 修正）。

- A01：专岗保持「跟主模型一样」（profile.model 空）时，就绪判断和实际调用走同一条
  「有效候选链」——岗位自己没候选就用主模型兜底；只要主模型选好了，整体就绪。
- A02：岗位模型存在 kv["agents.profiles"]，改了它，Models.settings() 的就绪摘要缓存
  必须立刻跟上（即使 get_settings 回调一直返回同一个 Settings 对象）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store


def _settings():
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "endpoints": [{"id": "default", "base_url": "https://api.test/v1", "api_key": "sk-test"}],
            "model_list": [
                {"id": "m1", "endpoint": "default", "model": "gpt-main"},
                {"id": "w1", "endpoint": "default", "model": "gpt-worker"},
            ],
        }
    )
    assert problems == []
    return settings


def _ok(request: httpx.Request) -> httpx.Response:
    import json

    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "id": "x",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok:" + body["model"]}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _settings()  # 回调永远返回同一个对象（审查里能复现缓存过期的那条路径）
    agents = Agents(store, lambda: settings)
    models = Models(store, lambda: settings, agents=agents, transport=httpx.MockTransport(_ok))
    yield store, agents, models
    store.close()


class TestInheritMain:
    def test_only_main_chosen_is_ready(self, env) -> None:
        _, agents, models = env
        agents.update_profile("main", {"model": "m1"})
        for k in ("news", "idea", "goal", "task"):
            agents.update_profile(k, {"model": ""})  # 引导默认「跟主模型一样」写空串
        s = models.settings()
        assert s.ready() is True
        # 摘要里干活模型显示的是实际会用的（主模型兜底）
        assert s.worker == "gpt-main"

    @pytest.mark.asyncio
    async def test_inherit_chat_works_for_every_kind(self, env) -> None:
        _, agents, models = env
        agents.update_profile("main", {"model": "m1"})
        try:
            for kind in ("main", "news", "idea", "goal", "task"):
                r = await models.chat(agent=kind, messages=[{"role": "user", "content": "hi"}], retries=0)
                assert r.text == "ok:gpt-main"
        finally:
            await models.close()

    @pytest.mark.asyncio
    async def test_custom_agent_inherits(self, env) -> None:
        store, agents, models = env
        agents.update_profile("main", {"model": "m1"})
        kind = agents.create_custom("自建岗")["kind"]
        try:
            r = await models.chat(agent=kind, messages=[{"role": "user", "content": "hi"}], retries=0)
            assert r.text == "ok:gpt-main"
        finally:
            await models.close()

    def test_nothing_chosen_not_ready(self, env) -> None:
        _, _, models = env
        assert models.settings().ready() is False

    def test_own_model_still_wins(self, env) -> None:
        _, agents, models = env
        agents.update_profile("main", {"model": "m1"})
        agents.update_profile("task", {"model": "w1"})
        assert models.settings().worker == "gpt-worker"
        assert [c.service_model for c in models._current_candidates("task")] == ["gpt-worker"]


class TestProfileChangeInvalidates:
    def test_ready_follows_profile_change_without_manual_reset(self, env) -> None:
        _, agents, models = env
        assert models.settings().ready() is False  # 先读一次，进缓存
        agents.update_profile("main", {"model": "m1"})
        assert models.settings().ready() is True
        agents.update_profile("main", {"model": ""})
        assert models.settings().ready() is False

    def test_worker_label_follows_task_change(self, env) -> None:
        _, agents, models = env
        agents.update_profile("main", {"model": "m1"})
        assert models.settings().worker == "gpt-main"
        agents.update_profile("task", {"model": "w1"})
        assert models.settings().worker == "gpt-worker"
