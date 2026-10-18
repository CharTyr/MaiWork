"""R5 立目标前的判断（goal_fit.GoalFitJudge；.trellis/tasks/10-11-goals-fit-group-chat）。

规则对应：
- 只判「可能要立成目标」的请求：kind="goal"，或带 idea_id 且挑中的项目里有 goal；
  别的请求零模型调用；
- 非服务群 / 空群号 / 请求不是 pending → 零模型调用、零写入；
- 一次主模型调用（purpose="approvals.goal_fit"、json_mode、group_id）：
  `{"kind": "online"|"human", "reason": "一句话", "alt": {"title","req"}|null}`；
  构想时 `{"items": {"<序号>": {"kind","reason","alt"}}}`；
- 单目标写回 goal_fit / goal_fit_reason（折叠空白截 80 字）/ goal_fit_alt；
  构想写汇总（任一 human 则 human，否则 online）+ 每个项目的判断；
- 模型出错 / JSON 不合法 / kind 不认识 → goal_fit="error"（不重试），事件照记；
- 写回前重查仍是 pending，否则整个判断丢弃；
- 每次判断记一条 `request.goal_fit` 事件。
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeModelsQueue

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goal_fit import GoalFitJudge
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

G1 = "900000001"
G2 = "111222333"
NOW = 1_790_000_000.0

_ONLINE = json.dumps(
    {"kind": "online", "reason": "只是上网盯着版本更新，MaiWork 能做", "alt": None}, ensure_ascii=False
)
_HUMAN_ALT = json.dumps(
    {
        "kind": "human",
        "reason": "要群友自己开档、截图",
        "alt": {"title": "整理 mod 清单", "req": "把 mod 和分线整理成一份清单"},
    },
    ensure_ascii=False,
)
_HUMAN_NO_ALT = json.dumps({"kind": "human", "reason": "要大家到场才能做", "alt": None}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


def _settings(*, serve=(G1,)):
    cfg: dict = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
    }
    settings, problems = load_settings(cfg)
    assert problems == [], problems
    return settings


class _Harness:
    def __init__(self, store, settings, models, tasks, goals, approvals, judge):
        self.store = store
        self.settings = settings
        self.models = models
        self.tasks = tasks
        self.goals = goals
        self.approvals = approvals
        self.judge = judge

    def create(self, group_id=G1, **kw):
        base = dict(
            kind="goal",
            title="帮我们盯着暗潮新版本",
            quote="@MaiBot 帮我们盯着 暗潮新版本，出了说一声",
            via="群里 @",
            requester_id="10001",
            requester_name="阿柒",
        )
        base.update(kw)
        return self.approvals.create(group_id, **base)

    def row(self, rid):
        got = self.store.read().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(got) if got is not None else None

    def counts(self):
        n_task = self.store.read().execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        n_goal = self.store.read().execute("SELECT COUNT(*) AS n FROM goals").fetchone()["n"]
        return int(n_task), int(n_goal)


def _setup(tmp_path, *, replies=None, serve=(G1,), approvals=None):
    store = Store(tmp_path / "mw.db")
    store.migrate()
    settings = _settings(serve=serve)
    models = FakeModelsQueue(ready=True, replies=replies)
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    approvals = approvals if approvals is not None else Approvals(store, lambda: settings, tasks, goals)
    judge = GoalFitJudge(store, models, lambda: settings, approvals=approvals)
    return _Harness(store, settings, models, tasks, goals, approvals, judge)


def _seed_idea(store, items, *, gid=G1, state="new", body="把干货整理出来") -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
            " VALUES (?, 'bulb', '做铝价表', ?, ?, ?, ?, ?)",
            (gid, body, json.dumps(items, ensure_ascii=False), state, NOW, NOW),
        )
        return int(cur.lastrowid or 0)


def _fit_events(store, rid) -> list[dict]:
    rows = store.read().execute(
        "SELECT payload FROM events WHERE kind='request.goal_fit' AND entity_id=?", (rid,)
    ).fetchall()
    return [json.loads(str(r["payload"])) for r in rows]


_TASK_ITEM = {"kind": "task", "title": "抓铝价数据", "desc": "先把最近一个月的铝价抓下来"}
_GOAL_HUMAN = {"kind": "goal", "title": "分线开档打卡", "desc": "群友自己开档、截图打卡"}
_GOAL_ONLINE = {"kind": "goal", "title": "盯游戏新版本", "desc": "出了新版本在群里说一声"}


# ----------------------------------------------------------------------
# 单目标：online / human+替代 / human 没替代
# ----------------------------------------------------------------------


class TestSingleGoal:
    @pytest.mark.asyncio
    async def test_online_written_to_request(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        r = h.create()
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["status"] == "pending"
        assert row["goal_fit"] == "online"
        assert row["goal_fit_reason"] == "只是上网盯着版本更新，MaiWork 能做"
        assert row["goal_fit_alt"] == ""
        assert len(h.models.calls) == 1
        _role, messages, kwargs = h.models.calls[0]
        assert kwargs["purpose"] == "approvals.goal_fit"
        assert kwargs["json_mode"] is True
        assert kwargs["group_id"] == G1
        assert "暗潮新版本" in messages[-1]["content"]
        assert _fit_events(h.store, r["id"]) == [
            {"fit": "online", "reason": "只是上网盯着版本更新，MaiWork 能做", "alt": None}
        ]

    @pytest.mark.asyncio
    async def test_human_with_alt_saved_as_json(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_HUMAN_ALT])
        r = h.create()
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["goal_fit"] == "human"
        assert row["goal_fit_reason"] == "要群友自己开档、截图"
        assert json.loads(row["goal_fit_alt"]) == {
            "title": "整理 mod 清单",
            "req": "把 mod 和分线整理成一份清单",
        }

    @pytest.mark.asyncio
    async def test_human_without_alt_keeps_empty_alt(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_HUMAN_NO_ALT])
        r = h.create()
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["goal_fit"] == "human"
        assert row["goal_fit_reason"] == "要大家到场才能做"
        assert row["goal_fit_alt"] == ""

    @pytest.mark.asyncio
    async def test_reason_folded_and_capped_at_80(self, tmp_path) -> None:
        long_reason = "要群友\n自己动手" + "啊" * 200
        h = _setup(tmp_path, replies=[json.dumps({"kind": "human", "reason": long_reason, "alt": None}, ensure_ascii=False)])
        r = h.create()
        await h.judge.judge(r["id"])
        reason = h.row(r["id"])["goal_fit_reason"]
        assert "\n" not in reason and "  " not in reason     # 空白折叠成单空格
        assert len(reason) == 80
        assert reason.startswith("要群友 自己动手")

    @pytest.mark.asyncio
    async def test_alt_without_req_is_dropped(self, tmp_path) -> None:
        """替代任务必须 title + req 都有；缺一个当没给。"""
        h = _setup(tmp_path, replies=[json.dumps({"kind": "human", "reason": "要动手", "alt": {"title": "只有标题"}}, ensure_ascii=False)])
        r = h.create()
        await h.judge.judge(r["id"])
        assert h.row(r["id"])["goal_fit_alt"] == ""


# ----------------------------------------------------------------------
# 出错 / 坏 JSON / 不认识的 kind → error（不重试）
# ----------------------------------------------------------------------


class TestErrors:
    @pytest.mark.asyncio
    async def test_model_error_marks_error(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[ModelError("端点挂了")])
        r = h.create()
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["status"] == "pending"
        assert row["goal_fit"] == "error"
        assert row["goal_fit_reason"]              # 出错原因留在行上（截断过）
        assert len(h.models.calls) == 1            # 不重试
        assert _fit_events(h.store, r["id"])[0]["fit"] == "error"

    @pytest.mark.asyncio
    async def test_bad_json_marks_error(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=["这不是 JSON"])
        r = h.create()
        await h.judge.judge(r["id"])
        assert h.row(r["id"])["goal_fit"] == "error"
        assert len(h.models.calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_kind_marks_error(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[json.dumps({"kind": "maybe", "reason": "拿不准"}, ensure_ascii=False)])
        r = h.create()
        await h.judge.judge(r["id"])
        assert h.row(r["id"])["goal_fit"] == "error"

    @pytest.mark.asyncio
    async def test_not_a_dict_marks_error(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=['["online"]'])
        r = h.create()
        await h.judge.judge(r["id"])
        assert h.row(r["id"])["goal_fit"] == "error"


# ----------------------------------------------------------------------
# 不该判断的情形：零模型调用
# ----------------------------------------------------------------------


class TestNoJudgement:
    @pytest.mark.asyncio
    async def test_task_request_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        r = h.create(kind="task", title="帮我查免费图床")
        await h.judge.judge(r["id"])
        assert h.models.calls == []
        assert h.row(r["id"])["goal_fit"] == ""

    @pytest.mark.asyncio
    async def test_idea_without_goal_items_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        iid = _seed_idea(h.store, [_TASK_ITEM])
        r = h.create(kind="task", idea_id=iid, via="来自构想", source="idea")
        await h.judge.judge(r["id"])
        assert h.models.calls == []
        assert h.row(r["id"])["goal_fit"] == ""

    @pytest.mark.asyncio
    async def test_non_served_group_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE], serve=(G1,))
        with pytest.raises(ValueError, match="服务列表"):
            h.create(group_id=G2)
        assert h.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert h.models.calls == []
        assert h.counts() == (0, 0)

    @pytest.mark.asyncio
    async def test_empty_group_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        with pytest.raises(ValueError, match="服务列表"):
            h.create(group_id="")
        assert h.store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert h.models.calls == []
        assert h.counts() == (0, 0)

    @pytest.mark.asyncio
    async def test_missing_request_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        await h.judge.judge("R-404")
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_already_decided_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        r = h.create()
        h.approvals.approve(r["id"], by="42")
        await h.judge.judge(r["id"])
        assert h.models.calls == []
        assert h.row(r["id"])["goal_fit"] == ""

    @pytest.mark.asyncio
    async def test_no_model_client_is_ignored(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        judge = GoalFitJudge(h.store, None, lambda: h.settings, approvals=h.approvals)
        r = h.create()
        await judge.judge(r["id"])
        assert h.row(r["id"])["goal_fit"] == ""


# ----------------------------------------------------------------------
# 判断期间被批了 → 整个结果丢弃
# ----------------------------------------------------------------------


class TestRaceWithApproval:
    @pytest.mark.asyncio
    async def test_decided_while_judging_is_discarded(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_ONLINE])
        r = h.create()
        real = h.models.chat

        async def decide_first(*args, **kwargs):
            h.approvals.approve(r["id"], by="42")
            return await real(*args, **kwargs)

        h.models.chat = decide_first
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["status"] == "approved"
        assert row["goal_fit"] == ""            # 判断结果不再写回
        assert _fit_events(h.store, r["id"]) == []
        assert h.counts() == (0, 1)             # 按老口径建了目标


# ----------------------------------------------------------------------
# 构想：逐个 goal 项目判断
# ----------------------------------------------------------------------


def _idea_reply(*, human_alt=None, online_alt=None) -> str:
    return json.dumps(
        {
            "items": {
                "2": {
                    "kind": "human",
                    "reason": "要群友自己开档截图",
                    "alt": human_alt,
                },
                "3": {"kind": "online", "reason": "盯发布就行", "alt": online_alt},
            }
        },
        ensure_ascii=False,
    )


class TestIdeaItems:
    @pytest.mark.asyncio
    async def test_mixed_items_written_per_number(self, tmp_path) -> None:
        h = _setup(
            tmp_path,
            replies=[_idea_reply(human_alt={"title": "整理 mod 清单", "req": "把 mod 分线整理成清单"})],
        )
        iid = _seed_idea(h.store, [_TASK_ITEM, _GOAL_HUMAN, _GOAL_ONLINE])
        r = h.create(idea_id=iid, via="来自构想", source="idea", title="分线开档打卡")
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["goal_fit"] == "human"                     # 任一 human → 汇总 human
        assert row["goal_fit_reason"] == "要群友自己开档截图"   # 第一个 human 项的理由
        assert json.loads(row["goal_fit_alt"]) == {
            "items": {
                "2": {
                    "kind": "human",
                    "reason": "要群友自己开档截图",
                    "alt": {"title": "整理 mod 清单", "req": "把 mod 分线整理成清单"},
                },
                "3": {"kind": "online", "reason": "盯发布就行", "alt": None},
            }
        }
        prompt = h.models.calls[0][1][-1]["content"]
        assert "[2]" in prompt and "[3]" in prompt
        assert "分线开档" in prompt and "盯游戏新版本" in prompt
        # 只判 goal 项目：task 项目（序号 1）不问也不写
        assert "[1]" not in prompt

    @pytest.mark.asyncio
    async def test_all_online_summarises_online(self, tmp_path) -> None:
        reply = json.dumps(
            {"items": {"2": {"kind": "online", "reason": "盯发布", "alt": None}}}, ensure_ascii=False
        )
        h = _setup(tmp_path, replies=[reply])
        iid = _seed_idea(h.store, [_TASK_ITEM, _GOAL_ONLINE])
        r = h.create(idea_id=iid, via="来自构想", source="idea")
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["goal_fit"] == "online"
        assert json.loads(row["goal_fit_alt"])["items"]["2"]["kind"] == "online"

    @pytest.mark.asyncio
    async def test_missing_item_in_reply_marks_error(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[json.dumps({"items": {"2": {"kind": "human", "reason": "要动手"}}}, ensure_ascii=False)])
        iid = _seed_idea(h.store, [_GOAL_HUMAN, _GOAL_ONLINE])
        r = h.create(idea_id=iid, via="来自构想", source="idea")
        await h.judge.judge(r["id"])
        assert h.row(r["id"])["goal_fit"] == "error"

    @pytest.mark.asyncio
    async def test_only_picked_items_are_judged(self, tmp_path) -> None:
        """点名只做第 3 项 → 第 2 项（human）不问也不写。"""
        h = _setup(tmp_path, replies=[json.dumps({"items": {"3": {"kind": "online", "reason": "盯发布"}}}, ensure_ascii=False)])
        iid = _seed_idea(h.store, [_TASK_ITEM, _GOAL_HUMAN, _GOAL_ONLINE])
        r = h.create(idea_id=iid, via="来自构想", source="idea", items=[3])
        await h.judge.judge(r["id"])
        row = h.row(r["id"])
        assert row["goal_fit"] == "online"
        assert set(json.loads(row["goal_fit_alt"])["items"]) == {"3"}
