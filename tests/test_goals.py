"""M3 数据层 goals.py：agent 目标 + 成员目标（docs/02 §4.3、01 R5、07 §9.3 goals / §11.2）。

规则对应：
- agent 目标：create_agent 落 G-n，criteria 存 [{"text","done":False}]，next_check_ts=now+3600；
- 成员目标：create_member 落 M-n；repeat="daily" 时 until_ts=now+30 天；
- due(now)：remind 到点、到期问进展、agent 目标到检查点、循环到期前 1 天问续期；
- mark(...) 之后推进 remind_ts / next_check_ts，错过的只补一次（不连发）；
- view(group_id) 按 §9.3 结构出 agent / member 两栏，只列 active / paused，done 的保留 3 天。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "900000001"


class _Settings:
    def workspace_of(self, group_id: str) -> str:
        return "ws-demo"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path):
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    yield store
    store.close()


@pytest.fixture
def goals(mem_store: Store) -> Goals:
    return Goals(mem_store, lambda: _Settings())


def _mk_agent(goals: Goals, **kw) -> str:
    kwargs = {"title": "盯着活动日历", "body": "每周更新", "criteria": ["不漏场", "提前一周发"]}
    kwargs.update(kw)
    return goals.create_agent(GID, **kwargs, by_text="麦麦")


def _mk_member(goals: Goals, **kw) -> str:
    kwargs = {
        "who_id": "10001", "who_name": "阿柒",
        "title": "交周报", "due_ts": NOW + 86400, "remind_ts": NOW + 3600,
    }
    kwargs.update(kw)
    return goals.create_member(GID, **kwargs)


class TestCreate:
    def test_agent_goal_structure(self, goals: Goals, mem_store: Store):
        gid = _mk_agent(goals)
        assert gid == "G-1"
        row = goals.get(gid)
        assert row["kind"] == "agent"
        assert row["state"] == "active"
        assert json.loads(row["criteria"]) == [
            {"text": "不漏场", "done": False},
            {"text": "提前一周发", "done": False},
        ]
        assert row["next_check_ts"] == NOW + 3600
        assert row["by_text"] == "麦麦"

    def test_member_goal_structure(self, goals: Goals):
        mid = _mk_member(goals)
        assert mid == "M-1"
        row = goals.get(mid)
        assert row["kind"] == "member"
        assert row["who_id"] == "10001"
        assert row["who_name"] == "阿柒"
        assert row["due_ts"] == NOW + 86400
        assert row["remind_ts"] == NOW + 3600
        assert row["repeat"] is None
        assert row["until_ts"] is None

    def test_member_goal_repeat_daily_sets_until_30d(self, goals: Goals):
        mid = _mk_member(goals, repeat="daily", due_ts=None)
        row = goals.get(mid)
        assert row["repeat"] == "daily"
        assert row["until_ts"] == NOW + 30 * 86400


class TestDueRemind:
    def test_remind_at_point(self, goals: Goals, fixed_clock):
        _mk_member(goals, remind_ts=NOW + 100)
        fixed_clock[0] = NOW + 200
        due = goals.due(fixed_clock[0])
        kinds = [d["type"] for d in due]
        assert "remind" in kinds
        item = next(d for d in due if d["type"] == "remind")
        assert item["goal"]["id"] == "M-1"
        assert item["goal"]["who_name"] == "阿柒"

    def test_no_remind_before_point(self, goals: Goals, fixed_clock):
        _mk_member(goals, remind_ts=NOW + 3600)
        assert goals.due(NOW) == []

    def test_mark_remind_nonrepeat_clears_remind_ts(self, goals: Goals, fixed_clock):
        _mk_member(goals, remind_ts=NOW + 100)
        fixed_clock[0] = NOW + 200
        goals.due(fixed_clock[0])
        goals.mark("M-1", "remind", fixed_clock[0])
        assert goals.get("M-1")["remind_ts"] is None
        assert goals.due(fixed_clock[0] + 3600) == []


class TestDueDailyRepeat:
    def test_daily_advances_by_one_day(self, goals: Goals, fixed_clock):
        _mk_member(goals, repeat="daily", due_ts=None, remind_ts=NOW + 100)
        fixed_clock[0] = NOW + 200
        goals.due(fixed_clock[0])
        goals.mark("M-1", "remind", fixed_clock[0])
        row = goals.get("M-1")
        # 推进约一天；如果「原时刻+1 天」已经过去了，就直接推到现在的明天（不连发）
        assert fixed_clock[0] + 1 < row["remind_ts"]
        assert abs(row["remind_ts"] - (fixed_clock[0] + 86400)) < 1e-6

    def test_downtime_missed_only_one(self, goals: Goals, fixed_clock):
        # 停机 3 天错过 3 次提醒，恢复后只补一次，下次直接跳到未来的下一天
        _mk_member(goals, repeat="daily", due_ts=None, remind_ts=NOW + 100)
        fixed_clock[0] = NOW + 3 * 86400 + 200
        due = goals.due(fixed_clock[0])
        reminds = [d for d in due if d["type"] == "remind"]
        assert len(reminds) == 1
        goals.mark("M-1", "remind", fixed_clock[0])
        row = goals.get("M-1")
        assert row["remind_ts"] > fixed_clock[0]
        assert abs(row["remind_ts"] - (fixed_clock[0] + 86400)) < 1e-6

    def test_repeat_mark_does_not_fire_again_immediately(self, goals: Goals, fixed_clock):
        _mk_member(goals, repeat="daily", due_ts=None, remind_ts=NOW + 100)
        fixed_clock[0] = NOW + 200
        goals.mark("M-1", "remind", fixed_clock[0])
        assert goals.due(fixed_clock[0]) == []

    def test_daily_mark_beyond_until_marks_done(self, goals: Goals, fixed_clock):
        mid = _mk_member(goals, repeat="daily", due_ts=None, remind_ts=NOW + 30 * 86400 - 60)
        row0 = goals.get(mid)
        until = row0["until_ts"]
        fixed_clock[0] = until + 10
        goals.due(fixed_clock[0])
        goals.mark(mid, "remind", fixed_clock[0])
        assert goals.get(mid)["state"] == "done"


class TestDueAskProgress:
    def test_due_ts_passed_and_not_asked(self, goals: Goals, fixed_clock):
        _mk_member(goals, due_ts=NOW + 100, remind_ts=None)
        fixed_clock[0] = NOW + 200
        types = [d["type"] for d in goals.due(fixed_clock[0])]
        assert "ask_progress" in types

    def test_not_due_before_due_ts(self, goals: Goals):
        _mk_member(goals, due_ts=NOW + 86400)
        assert goals.due(NOW) == []

    def test_mark_ask_progress_sets_last_ts(self, goals: Goals, fixed_clock):
        mid = _mk_member(goals, due_ts=NOW + 100, remind_ts=None)
        fixed_clock[0] = NOW + 200
        goals.due(fixed_clock[0])
        goals.mark(mid, "ask_progress", fixed_clock[0])
        assert goals.get(mid)["last_ts"] == fixed_clock[0]
        # 已问过的不再产生 ask_progress
        assert goals.due(fixed_clock[0] + 3600) == []


class TestDueAgentCheck:
    def test_next_check_at_point(self, goals: Goals):
        _mk_agent(goals)
        due = goals.due(NOW + 3601)
        assert any(d["type"] == "check" and d["goal"]["id"] == "G-1" for d in due)

    def test_not_due_before_check_point(self, goals: Goals):
        _mk_agent(goals)
        assert goals.due(NOW) == []

    def test_mark_check_updates_next_check_ts(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.mark(gid, "check", NOW + 3601, next_remind_ts=NOW + 7200)
        assert goals.get(gid)["next_check_ts"] == NOW + 7200
        assert goals.due(NOW + 3700) == []

    def test_paused_goal_not_due(self, goals: Goals, mem_store: Store):
        gid = _mk_agent(goals)
        mem_store.read().execute("UPDATE goals SET state='paused' WHERE id=?", (gid,))
        assert goals.due(NOW + 7200) == []


class TestDueRenew:
    def test_renew_fires_for_daily_near_until(self, goals: Goals, fixed_clock):
        mid = _mk_member(goals, repeat="daily", due_ts=None, remind_ts=None)
        until = goals.get(mid)["until_ts"]
        fixed_clock[0] = until - 0.5 * 86400
        types = [d["type"] for d in goals.due(fixed_clock[0])]
        assert "renew" in types

    def test_no_renew_before_until_minus_1d(self, goals: Goals):
        _mk_member(goals, repeat="daily", due_ts=None, remind_ts=None)
        assert goals.due(NOW) == []

    def test_mark_renew(self, goals: Goals, fixed_clock):
        mid = _mk_member(goals, repeat="daily", due_ts=None, remind_ts=None)
        until = goals.get(mid)["until_ts"]
        fixed_clock[0] = until - 0.5 * 86400
        goals.due(fixed_clock[0])
        goals.mark(mid, "renew", fixed_clock[0])
        assert goals.get(mid)["last_ts"] == fixed_clock[0]
        assert goals.due(fixed_clock[0] + 100) == []


class TestLifecycle:
    def test_pause_resume(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.pause(gid)
        assert goals.get(gid)["state"] == "paused"
        goals.resume(gid)
        assert goals.get(gid)["state"] == "active"

    def test_cancel(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.cancel(gid)
        assert goals.get(gid)["state"] == "cancelled"

    def test_repeated_cancel_is_idempotent_without_new_update(self, goals: Goals, fixed_clock):
        gid = _mk_agent(goals)
        goals.cancel(gid)
        updated = goals.get(gid)["updated"]
        fixed_clock[0] = NOW + 500
        goals.cancel(gid)
        assert goals.get(gid)["state"] == "cancelled"
        assert goals.get(gid)["updated"] == updated

    def test_done(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.done(gid)
        assert goals.get(gid)["state"] == "done"

    def test_late_completion_cannot_revive_cancelled_goal(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.cancel(gid)
        with pytest.raises(ValueError, match="cancelled"):
            goals.done(gid)
        assert goals.get(gid)["state"] == "cancelled"

    def test_late_completion_cannot_resume_paused_goal(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.pause(gid)
        with pytest.raises(ValueError, match="paused"):
            goals.done(gid)
        assert goals.get(gid)["state"] == "paused"

    def test_touch(self, goals: Goals, fixed_clock):
        gid = _mk_agent(goals)
        fixed_clock[0] = NOW + 500
        goals.touch(gid, "本周发了一次")
        row = goals.get(gid)
        assert row["last_ts"] == NOW + 500
        assert row["last_text"] == "本周发了一次"


class TestCriteria:
    def test_set_criterion_marks_done(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        crit = json.loads(goals.get(gid)["criteria"])
        assert crit[0] == {"text": "不漏场", "done": True}
        assert crit[1]["done"] is False

    def test_set_criterion_index_out_of_range_raises(self, goals: Goals):
        gid = _mk_agent(goals)
        with pytest.raises(IndexError):
            goals.set_criterion(gid, 99, True)


class TestView:
    def test_view_structure_agent_and_member(self, goals: Goals, mem_store: Store):
        agid = _mk_agent(goals)
        mem_store.read().execute("UPDATE goals SET task_id='T-7' WHERE id=?", (agid,))
        mid = _mk_member(goals)
        out = goals.view(GID)
        assert set(out.keys()) == {"agent", "member"}
        a = out["agent"][0]
        m = out["member"][0]
        assert a["id"] == agid
        assert a["title"] == "盯着活动日历"
        assert a["by"] == "麦麦"
        assert a["last"] is None
        assert a["task_id"] == "T-7"
        assert a["state"] == "active"
        assert a["fit"] == "" and a["fit_reason"] == ""   # R5：老库没这列时兜底空串
        assert isinstance(a["criteria"], list) and a["criteria"][0] == {
            "text": "不漏场", "done": False, "backed": False, "pending": False,
        }
        assert a["next_check_ts"] == NOW + 3600
        assert m["id"] == mid
        assert m["who"] == "阿柒"
        assert m["title"] == "交周报"
        assert m["due_ts"] == NOW + 86400
        assert m["remind_ts"] == NOW + 3600
        assert m["state"] == "active"

    def test_agent_last_after_touch(self, goals: Goals, fixed_clock):
        gid = _mk_agent(goals)
        fixed_clock[0] = NOW + 500
        goals.touch(gid, "交了一次")
        out = goals.view(GID)
        assert out["agent"][0]["last"] == {"ts": NOW + 500, "text": "交了一次"}

    def test_done_goal_hidden_after_3_days(self, goals: Goals, mem_store: Store, fixed_clock):
        gid = _mk_agent(goals)
        mem_store.read().execute("UPDATE goals SET state='done', updated=? WHERE id=?", (NOW, gid))
        out_now = goals.view(GID)
        assert len(out_now["agent"]) == 1  # 3 天内还看得到
        fixed_clock[0] = NOW + 4 * 86400
        out_later = goals.view(GID)
        assert out_later["agent"] == []

    def test_paused_goal_shown(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.pause(gid)
        out = goals.view(GID)
        assert out["agent"][0]["state"] == "paused"

    def test_cancelled_goal_hidden(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.cancel(gid)
        assert goals.view(GID)["agent"] == []


class TestCurrentNames:
    """成员目标的显示名按 who_id 查名册当前名；查不到回落 who_name 快照。"""

    def _record(self, mem_store: Store, uid: str, name: str) -> None:
        from CharTyr_MaiWork.maiwork import members

        with mem_store.tx() as conn:
            members.record(conn, GID, uid, name, 1e10)

    def test_view_who_uses_current_name(self, goals: Goals, mem_store: Store):
        _mk_member(goals, who_id="10001", who_name="阿柒")
        self._record(mem_store, "10001", "阿柒改了名")
        assert goals.view(GID)["member"][0]["who"] == "阿柒改了名"

    def test_view_who_unknown_id_keeps_snapshot(self, goals: Goals, mem_store: Store):
        _mk_member(goals, who_id="99999", who_name="老快照")
        assert goals.view(GID)["member"][0]["who"] == "老快照"

    def test_due_remind_who_name_is_current(self, goals: Goals, mem_store: Store, fixed_clock):
        _mk_member(goals, who_id="10001", who_name="阿柒", remind_ts=NOW + 100)
        self._record(mem_store, "10001", "阿柒改了名")
        fixed_clock[0] = NOW + 200
        item = next(d for d in goals.due(fixed_clock[0]) if d["type"] == "remind")
        assert item["goal"]["who_name"] == "阿柒改了名"
        assert item["goal"]["who_id"] == "10001"   # 调用方仍拿得到 id 自己再查


class TestSetCriteria:
    """2026-10：目标第一次检查补验收标准用（整组替换，done 一律 False）。"""

    def test_replaces_criteria(self, goals: Goals, mem_store: Store):
        gid = goals.create_agent(GID, title="盯着", body="", criteria=[], by_text="")
        got = goals.set_criteria(gid, ["第一条", "第二条", "  "])
        assert [c["text"] for c in got] == ["第一条", "第二条"]
        assert all(c["done"] is False for c in got)
        row = mem_store.read().execute("SELECT criteria FROM goals WHERE id=?", (gid,)).fetchone()
        assert [c["text"] for c in json.loads(row["criteria"])] == ["第一条", "第二条"]

    def test_empty_list_clears(self, goals: Goals):
        gid = goals.create_agent(GID, title="盯着", body="", criteria=["旧"], by_text="")
        assert goals.set_criteria(gid, []) == []
        assert json.loads(goals.get(gid)["criteria"]) == []

    def test_unknown_goal_raises(self, goals: Goals):
        with pytest.raises(KeyError):
            goals.set_criteria("G-999", ["x"])


def _events(mem_store: Store, kind: str, entity_id: str) -> list[dict]:
    rows = mem_store.read().execute(
        "SELECT payload FROM events WHERE kind=? AND entity_id=? ORDER BY id",
        (str(kind), str(entity_id)),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


# ---------------------------------------------------------------------------
# R1 / R6：完成标准「有没有依据」纯函数
# ---------------------------------------------------------------------------


class TestCriterionPureFunctions:
    """criterion_backed / criterion_pending：不写库、现算（R1 D5）。"""

    def test_backed_by_evidence(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        c = {"text": "群里说过", "done": True, "evidence": "阿柒：投票结果出来了"}
        assert goals_mod.criterion_backed(c) is True
        assert goals_mod.criterion_pending(c) is False

    def test_backed_by_task_id(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        c = {"text": "下级任务做完", "done": True, "task_id": "T-3"}
        assert goals_mod.criterion_backed(c) is True
        assert goals_mod.criterion_pending(c) is False

    def test_backed_by_confirmed_by(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        c = {"text": "管理员算数", "done": True, "confirmed_by": "10001"}
        assert goals_mod.criterion_backed(c) is True
        assert goals_mod.criterion_pending(c) is False

    def test_blank_evidence_is_not_backing(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        for c in (
            {"done": True, "evidence": "   "},
            {"done": True, "evidence": ""},
            {"done": True, "evidence": None},
            {"done": True, "task_id": ""},
            {"done": True, "task_id": None},
            {"done": True, "confirmed_by": "  "},
        ):
            assert goals_mod.criterion_backed(c) is False
            assert goals_mod.criterion_pending(c) is True

    def test_done_but_backed_nowhere_is_pending(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        c = {"text": "旧的无依据勾", "done": True}
        assert goals_mod.criterion_backed(c) is False
        assert goals_mod.criterion_pending(c) is True

    def test_not_done_is_neither(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        c = {"text": "还没做", "done": False, "evidence": "随便写"}
        assert goals_mod.criterion_backed(c) is False
        assert goals_mod.criterion_pending(c) is False

    def test_non_dict_always_false(self):
        from CharTyr_MaiWork.maiwork import goals as goals_mod

        for bad in (None, [], "done", 3, 0, True, ("a",)):
            assert goals_mod.criterion_backed(bad) is False
            assert goals_mod.criterion_pending(bad) is False


class TestChatLineBody:
    """R6：群聊行「- MM-DD HH:MM 谁：正文」先去时间前缀再取正文。"""

    def test_new_format_strips_time_prefix(self):
        """时间前缀（半角冒号）要先去掉，再按「谁：正文」取正文。"""
        from CharTyr_MaiWork.maiwork.goals import _chat_line_body

        assert _chat_line_body("- 10-11 14:05 阿柒:投票结果出来了") == "投票结果出来了"
        assert _chat_line_body("- 10-11 14:05 阿柒：投票结果出来了") == "投票结果出来了"
        assert _chat_line_body("- 10-11 14:05：投票结果出来了") == "投票结果出来了"

    def test_old_format_unchanged(self):
        from CharTyr_MaiWork.maiwork.goals import _chat_line_body

        assert _chat_line_body("- 阿柒：投票结果出来了") == "投票结果出来了"
        assert _chat_line_body("- 阿柒:投票结果出来了") == "投票结果出来了"
        assert _chat_line_body("- 裸一行没有冒号") == "裸一行没有冒号"


# ---------------------------------------------------------------------------
# R5：目标本身的补判结果（goals.fit / goals.fit_reason，只给网页）
# ---------------------------------------------------------------------------


class TestSetFit:
    def test_online_writes_fit_and_event(self, goals: Goals, mem_store: Store):
        gid = _mk_agent(goals)
        goals.set_fit(gid, "online", "  盯着  版本发布  ")
        row = goals.get(gid)
        assert row["fit"] == "online"
        assert row["fit_reason"] == "盯着 版本发布"
        assert _events(mem_store, "goal.fit", gid) == [
            {"kind": "online", "reason": "盯着 版本发布"}
        ]

    def test_human_kind_allowed(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_fit(gid, "human", "要群友自己动手")
        assert goals.get(gid)["fit"] == "human"

    def test_reason_truncated_to_80(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_fit(gid, "human", "很" * 200)
        assert len(goals.get(gid)["fit_reason"]) == 80

    def test_empty_reason_ok(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_fit(gid, "online", "   ")
        assert goals.get(gid)["fit_reason"] == ""

    def test_bad_kind_raises_and_writes_nothing(self, goals: Goals):
        gid = _mk_agent(goals)
        with pytest.raises(ValueError):
            goals.set_fit(gid, "kinda", "x")
        assert goals.get(gid)["fit"] == ""

    def test_unknown_goal_raises(self, goals: Goals):
        with pytest.raises(KeyError):
            goals.set_fit("G-999", "online", "x")


# ---------------------------------------------------------------------------
# R1：管理员对待确认项「算数 / 撤掉」
# ---------------------------------------------------------------------------


class TestConfirmRevokeCriterion:
    def test_confirm_pending_sets_admin_and_event(
        self, goals: Goals, mem_store: Store, fixed_clock
    ):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)          # 旧的无依据勾 → 待确认
        fixed_clock[0] = NOW + 60
        goals.confirm_criterion(gid, 0, "10001")

        item = json.loads(goals.get(gid)["criteria"])[0]
        assert item["done"] is True
        assert item["confirmed_by"] == "10001"
        assert item["confirmed_ts"] == NOW + 60
        assert _events(mem_store, "goal.criterion_confirmed", gid) == [
            {"index": 0, "text": "不漏场", "by": "10001"}
        ]

    def test_confirm_paused_goal_allowed(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        goals.pause(gid)
        goals.confirm_criterion(gid, 0, "10001")
        assert json.loads(goals.get(gid)["criteria"])[0]["confirmed_by"] == "10001"

    def test_confirm_requires_pending(self, goals: Goals, mem_store: Store):
        gid = _mk_agent(goals)
        # 没勾 → 不是待确认
        with pytest.raises(ValueError):
            goals.confirm_criterion(gid, 0, "10001")
        # 有证据的勾 → 也不是待确认
        goals.set_criterion(gid, 0, True, evidence="阿柒：投票结果出来了")
        with pytest.raises(ValueError):
            goals.confirm_criterion(gid, 0, "10001")
        assert _events(mem_store, "goal.criterion_confirmed", gid) == []

    def test_confirm_requires_live_state(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        goals.done(gid)
        with pytest.raises(ValueError):
            goals.confirm_criterion(gid, 0, "10001")
        assert "confirmed_by" not in json.loads(goals.get(gid)["criteria"])[0]

        gid2 = _mk_agent(goals, title="另一个")
        goals.set_criterion(gid2, 0, True)
        goals.cancel(gid2)
        with pytest.raises(ValueError):
            goals.confirm_criterion(gid2, 0, "10001")

    def test_confirm_index_out_of_range(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        with pytest.raises(IndexError):
            goals.confirm_criterion(gid, 99, "10001")

    def test_confirm_unknown_goal(self, goals: Goals):
        with pytest.raises(KeyError):
            goals.confirm_criterion("G-999", 0, "10001")

    def test_revoke_pending_clears_done_and_event(self, goals: Goals, mem_store: Store):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        goals.revoke_criterion(gid, 0, "10001")

        item = json.loads(goals.get(gid)["criteria"])[0]
        assert item["text"] == "不漏场"
        assert item["done"] is False
        assert "confirmed_by" not in item and "confirmed_ts" not in item
        assert _events(mem_store, "goal.criterion_revoked", gid) == [
            {"index": 0, "text": "不漏场", "by": "10001"}
        ]

    def test_revoke_requires_pending(self, goals: Goals):
        gid = _mk_agent(goals)
        with pytest.raises(ValueError):
            goals.revoke_criterion(gid, 0, "10001")
        goals.set_criterion(gid, 0, True, evidence="阿柒：投票结果出来了")
        with pytest.raises(ValueError):
            goals.revoke_criterion(gid, 0, "10001")
        assert json.loads(goals.get(gid)["criteria"])[0]["done"] is True

    def test_revoke_requires_live_state(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        goals.done(gid)
        with pytest.raises(ValueError):
            goals.revoke_criterion(gid, 0, "10001")

    def test_revoke_index_out_of_range(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        with pytest.raises(IndexError):
            goals.revoke_criterion(gid, 99, "10001")


# ---------------------------------------------------------------------------
# R1 / R5：网页视图字段
# ---------------------------------------------------------------------------


class TestViewFitAndBackedFlags:
    def test_view_exposes_fit_and_flags(self, goals: Goals, mem_store: Store):
        gid = _mk_agent(goals)
        mem_store.read().execute(
            "UPDATE goals SET fit='human', fit_reason='要群友自己动手' WHERE id=?", (gid,)
        )
        a = goals.view(GID)["agent"][0]
        assert a["fit"] == "human"
        assert a["fit_reason"] == "要群友自己动手"
        assert a["criteria"][0] == {
            "text": "不漏场", "done": False, "backed": False, "pending": False,
        }

    def test_view_marks_pending_and_backed(self, goals: Goals):
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)                              # 待确认
        goals.set_criterion(gid, 1, True, evidence="阿柒：提前一周发了")  # 有依据
        crit = goals.view(GID)["agent"][0]["criteria"]
        assert (crit[0]["done"], crit[0]["backed"], crit[0]["pending"]) == (True, False, True)
        assert (crit[1]["done"], crit[1]["backed"], crit[1]["pending"]) == (True, True, False)

    def test_view_does_not_persist_flags(self, goals: Goals):
        """backed / pending 是现算的，不写回库。"""
        gid = _mk_agent(goals)
        goals.set_criterion(gid, 0, True)
        goals.view(GID)
        assert json.loads(goals.get(gid)["criteria"])[0] == {"text": "不漏场", "done": True}

    def test_agent_dict_exposes_fit(self, goals: Goals, mem_store: Store, fixed_clock):
        """due() 走的 _agent_dict 也带 fit / fit_reason。"""
        gid = _mk_agent(goals)
        mem_store.read().execute(
            "UPDATE goals SET fit='human', fit_reason='盯不了' WHERE id=?", (gid,)
        )
        fixed_clock[0] = NOW + 7200
        item = next(d for d in goals.due(fixed_clock[0]) if d["type"] == "check")
        assert item["goal"]["fit"] == "human"
        assert item["goal"]["fit_reason"] == "盯不了"

    def test_missing_fit_columns_fall_back_empty(self, goals: Goals):
        """老库 dict 里没有 fit 列时用 g.get 兜底空串。"""
        gid = _mk_agent(goals)
        g = goals.get(gid)
        g.pop("fit", None)
        g.pop("fit_reason", None)
        assert goals._agent_view(g)["fit"] == ""
        assert goals._agent_view(g)["fit_reason"] == ""
        assert goals._agent_dict(g)["fit"] == ""
        assert goals._agent_dict(g)["fit_reason"] == ""
