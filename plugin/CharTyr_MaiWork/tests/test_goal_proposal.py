"""MaiWork 主动提目标（goal_proposal.GoalProposer；docs/02 §4.3 / §5.2，2026-10）。

规则对应：
- 值得做 → 写一条 kind=goal、source="maiwork" 的待批请求（requester_name=MaiWork）；
- **永远要管理员批准**：required=False、免批群、免批人都不让它自动落地；
- 每群每天最多一次（跑过一次哪怕结论是「没有」当天不再跑；模型出错不算跑过）；
- 没有值得做的（goal: null）→ 什么都不做；
- 和已有 agent 目标 / 本群 pending 的 MaiWork 提议重复 → 不提；
- 非服务群、[goals] propose=false → 零模型调用。
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeModelsQueue

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.approvals import Approvals
from CharTyr_MaiWork.goal_proposal import GoalProposer
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks

G1 = "900000001"
G2 = "111222333"

NOW = 1_790_000_000.0

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    from CharTyr_MaiWork import clock

    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


class FakeProfiles:
    def __init__(self, entries=None):
        self.entries_map = dict(entries or {})
        self.calls: list[str] = []

    def entries(self, group_id: str):
        self.calls.append(str(group_id))
        return list(self.entries_map.get(str(group_id), []))


def _settings(*, serve=(G1,), approval=None, goals=None):
    cfg = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
    }
    if approval is not None:
        cfg["approval"] = approval
    if goals is not None:
        cfg["goals"] = goals
    settings, problems = load_settings(cfg)
    assert problems == [], problems
    return settings


def _setup(tmp_path, *, replies=None, serve=(G1,), approval=None, goals=None, entries=None,
           ready=True):
    store = Store(tmp_path / "mw.db")
    store.migrate()
    settings = _settings(serve=serve, approval=approval, goals=goals)
    for gid in serve:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, workspace, profile_ready_ts, created)"
                " VALUES (?, ?, ?, ?)",
                (gid, f"g{gid}", NOW, NOW),
            )
    models = FakeModelsQueue(ready=ready, replies=replies)
    tasks = Tasks(store, lambda: settings)
    goals_obj = Goals(store, lambda: settings)
    approvals = Approvals(store, lambda: settings, tasks, goals_obj)
    profiles = FakeProfiles(entries)
    proposer = GoalProposer(
        store, models, goals_obj, approvals, lambda: settings,
        profiles=profiles, identity=None,
    )
    return store, settings, models, tasks, goals_obj, approvals, profiles, proposer


def _seed_chat(store: Store, gid: str, rows: list[tuple[str, str]]) -> None:
    with store.tx() as conn:
        for i, (who, text) in enumerate(rows):
            conn.execute(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (text, gid, f"m{i}", NOW - i * 60, f"u{i}", who),
            )


_GOAL_JSON = json.dumps(
    {"goal": {"title": "整理一份群里的开源项目清单并每月更新",
              "body": "把群里提过的开源项目整理成一页，每月更新一次",
              "why": "群里最近几天一直在聊自建服务，翻来覆去问同样几个项目"}},
    ensure_ascii=False,
)


class TestProposeHappyPath:
    async def test_creates_pending_request_with_source(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON],
            entries={G1: [{"category": "ongoing", "text": "在折腾自建服务"}]},
        )
        _seed_chat(store, G1, [("阿柒", "NAS 用什么系统好"), ("老李", "我那个项目又挂了")])
        res = await proposer.propose(G1)
        assert isinstance(res, dict)
        pending = approvals.pending_view(G1)
        assert len(pending) == 1
        p = pending[0]
        assert p["source"] == "maiwork"
        assert p["title"].startswith("整理一份")
        assert p["who"] == "MaiWork"
        assert "MaiWork 提议" in p["via"]
        # 提示词带上了群聊节选和画像
        prompt = models.calls[0][1][-1]["content"]
        assert "NAS 用什么系统好" in prompt
        assert "在折腾自建服务" in prompt
        assert "已经有的目标" in prompt and '{"goal"' in prompt

    async def test_approved_goal_keeps_maiwork_context(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON]
        )
        await proposer.propose(G1)
        rid = approvals.pending_view(G1)[0]["id"]
        out = approvals.approve(rid, by="42")
        goal = goals.get(out["goal_id"])
        assert goal["state"] == "active"
        assert goal["title"] == "整理一份群里的开源项目清单并每月更新"
        assert "为什么值得做" in goal["body"]
        assert "MaiWork" in goal["by_text"] and "来自" not in goal["by_text"]
        assert json.loads(goal["criteria"]) == []  # 验收标准留到第一次检查补（coordinator.check_goal）

    async def test_never_auto_approves_even_when_exempt(self, tmp_path) -> None:
        """红线：免批配置对它一律不生效（MaiWork 不能替管理员拍板立目标）。"""
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON],
            approval={"required": False, "exempt_groups": [f"qq:{G1}"], "exempt_users": ["10001"]},
        )
        res = await proposer.propose(G1)
        assert res["status"] == "pending" and res["auto"] is None
        assert goals.view(G1)["agent"] == []
        assert len(approvals.pending_view(G1)) == 1


class TestGuards:
    async def test_once_per_day_even_when_null(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=['{"goal": null}']
        )
        assert await proposer.propose(G1) is None
        assert len(models.calls) == 1
        assert await proposer.propose(G1) is None
        assert len(models.calls) == 1  # 当天不再调模型

    async def test_null_goal_creates_nothing(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=['{"goal": null}']
        )
        await proposer.propose(G1)
        assert approvals.pending_view(G1) == []

    async def test_dedup_against_existing_goal(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON]
        )
        goals.create_agent(
            G1, title="整理一份群里的开源项目清单并每月更新一次", body="", criteria=[],
            by_text="管理员 发起",
        )
        assert await proposer.propose(G1) is None
        assert approvals.pending_view(G1) == []

    async def test_dedup_against_pending_maiwork_proposal(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON]
        )
        approvals.create(
            G1, kind="goal", title="整理一份群里的开源项目清单，每月更新", quote="",
            via="MaiWork 提议", requester_id="", requester_name="MaiWork",
            source="maiwork", force_manual=True,
        )
        assert await proposer.propose(G1) is None
        assert len(approvals.pending_view(G1)) == 1  # 还是原来那条，没多出新的

    async def test_non_served_group_zero_calls(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON], serve=(G1,)
        )
        assert await proposer.propose(G2) is None
        assert models.calls == []
        assert approvals.pending_view(G2) == []

    async def test_switch_off_zero_calls(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON], goals={"propose": False}
        )
        assert await proposer.propose(G1) is None
        assert models.calls == []

    async def test_models_not_ready_zero_calls(self, tmp_path) -> None:
        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[_GOAL_JSON], ready=False
        )
        assert await proposer.propose(G1) is None
        assert models.calls == []

    async def test_model_error_does_not_mark_the_day(self, tmp_path) -> None:
        from CharTyr_MaiWork.models import ModelError

        store, settings, models, tasks, goals, approvals, profiles, proposer = _setup(
            tmp_path, replies=[ModelError("端点挂了"), _GOAL_JSON]
        )
        assert await proposer.propose(G1) is None
        # 出错不算「今天看过了」：下一轮还能再试
        res = await proposer.propose(G1)
        assert isinstance(res, dict)
        assert len(models.calls) == 2
