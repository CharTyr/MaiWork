"""M3 数据层 approvals.py：派活待批请求（docs/02 §5、01 R6、07 §9.3 tasks.pending）。

规则对应：
- 默认要批（required=True 且不在免批名单）→ create 落下是 pending；
- 免批（required=False、群在 exempt_groups、requester 在 exempt_users）→ 直接落地：
  kind="task" → Tasks.create(status="queued", source="request", request_id=…)；
  kind="goal" → Goals.create_agent(...)by_text 用「…发起 · 自动批准」；
- approve / reject 只能处理 pending，重复操作要报错；
- 超 24 小时没人处理 → 提醒一次（且只一次）；超 7 天 → expired；
- 取消 / 指令批准要看身份：发起人、群主/群管理、bot 管理员能行，其余不行。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

NOW = 1_790_000_000.0
GID = "900000001"


class _ApprovalSetting:
    def __init__(self, required=True, admins=(), exempt_groups=(), exempt_users=()):
        self.required = required
        self.admins = tuple(admins)
        self.exempt_groups = tuple(exempt_groups)
        self.exempt_users = tuple(exempt_users)


class _Settings:
    def __init__(self, approval):
        self.approval = approval

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
def tasks(mem_store: Store) -> Tasks:
    return Tasks(mem_store, lambda: _Settings(_ApprovalSetting()))


@pytest.fixture
def goals(mem_store: Store) -> Goals:
    return Goals(mem_store, lambda: _Settings(_ApprovalSetting()))


def _approvals(mem_store: Store, tasks: Tasks, goals: Goals, approval: _ApprovalSetting) -> Approvals:
    return Approvals(mem_store, lambda: _Settings(approval), tasks, goals)


_DEFAULTS = dict(
    kind="task", title="整理上周干货", quote="@麦麦 帮我整理一下", via="群里 @",
    requester_id="10001", requester_name="阿柒",
)


class TestCreateRequiresApprovalByDefault:
    def test_default_is_pending(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        r = ap.create(GID, **_DEFAULTS)
        assert r["id"] == "R-1"
        assert r["status"] == "pending"
        assert r["auto"] is None

    def test_event_written(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        r = ap.create(GID, **_DEFAULTS)
        rows = mem_store.read().execute(
            "SELECT kind, entity_id FROM events WHERE entity_id=?", (r["id"],)
        ).fetchall()
        assert any(e["kind"] == "request.pending" for e in rows)


class TestAutoApprovedPaths:
    def test_required_false_lands_task_queued(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=False))
        r = ap.create(GID, **_DEFAULTS)
        assert r["status"] == "approved"
        assert r["auto"] is True
        assert r["task_id"] == "T-1"
        row = tasks.get(r["task_id"])
        assert row["status"] == "queued"
        assert row["source"] == "request"
        assert row["request_id"] == r["id"]

    def test_exempt_group_with_prefix_matches(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(exempt_groups=(f"qq:{GID}",)))
        r = ap.create(GID, **_DEFAULTS)
        assert r["auto"] is True

    def test_exempt_group_without_prefix_matches(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(exempt_groups=(GID,)))
        r = ap.create(GID, **_DEFAULTS)
        assert r["auto"] is True

    def test_exempt_user_matches(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(exempt_users=("10001",)))
        r = ap.create(GID, **_DEFAULTS)
        assert r["auto"] is True

    def test_auto_approved_goal_kind_calls_goals_create_agent(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=False))
        kw = dict(_DEFAULTS)
        kw.update(kind="goal", title="帮我们盯着活动日历")
        r = ap.create(GID, **kw)
        assert r["status"] == "approved" and r["auto"] is True
        assert r["goal_id"] == "G-1"
        g = goals.get(r["goal_id"])
        assert g is not None
        assert g["kind"] == "agent"
        assert "自动批准" in str(g["by_text"])

    def test_idea_converted_request_by_text(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=False))
        kw = dict(_DEFAULTS)
        kw.update(kind="goal", idea_id=5)
        r = ap.create(GID, **kw)
        g = goals.get(r["goal_id"])
        assert "来自构想" in str(g["by_text"])


class TestApproveReject:
    def _mk_pending(self, mem_store, tasks, goals) -> Approvals:
        return _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))

    def test_approve_pending_lands_task(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        r = ap.create(GID, **_DEFAULTS)
        out = ap.approve(r["id"], by="42")
        assert out["status"] == "approved"
        assert out["task_id"] == "T-1"
        assert tasks.get(out["task_id"])["status"] == "queued"

    def test_approve_goal_kind_lands_goal(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        kw = dict(_DEFAULTS); kw.update(kind="goal")
        r = ap.create(GID, **kw)
        out = ap.approve(r["id"], by="42")
        assert out["goal_id"] == "G-1"
        assert "管理" in str(goals.get(out["goal_id"])["by_text"]) or "批准" in str(goals.get(out["goal_id"])["by_text"])

    def _insert_idea(self, mem_store, gid: str = GID, state: str = "pending") -> int:
        with mem_store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state, created, updated)"
                " VALUES (?, 'bulb', '做个铝价表', '把最近铝价整理成表', '群里天天聊铝', '先抓数据', '半天', ?, 1, 1)",
                (gid, state),
            )
            return int(cur.lastrowid or 0)

    def _idea_row(self, mem_store, idea_id: int):
        return mem_store.read().execute("SELECT state, task_id FROM ideas WHERE id=?", (idea_id,)).fetchone()

    def test_reject_request_from_idea_puts_idea_back(self, mem_store, tasks, goals):
        """来自构想的请求被拒：构想回到 new（不一直挂「等批准」）。"""
        ap = self._mk_pending(mem_store, tasks, goals)
        idea_id = self._insert_idea(mem_store)
        kw = dict(_DEFAULTS)
        kw.update(via="来自构想", idea_id=idea_id)
        r = ap.create(GID, **kw)
        ap.reject(r["id"], by="42")
        assert self._idea_row(mem_store, idea_id)["state"] == "new"

    def test_approve_request_from_idea_marks_idea_started(self, mem_store, tasks, goals):
        """构想「想要」（idea_id 非空）走到手动批准：构想要变 started 并回写 task_id。"""
        ap = self._mk_pending(mem_store, tasks, goals)
        idea_id = self._insert_idea(mem_store)
        kw = dict(_DEFAULTS)
        kw.update(via="来自构想", idea_id=idea_id)
        r = ap.create(GID, **kw)
        out = ap.approve(r["id"], by="42")
        assert out["task_id"] is not None
        row = self._idea_row(mem_store, idea_id)
        assert row["state"] == "started"
        assert row["task_id"] == out["task_id"]

    def test_approve_does_not_overwrite_already_started_idea(self, mem_store, tasks, goals):
        """构想已经 started（比如「做这个」先落过）：手动批准另一条请求不许把它乱指。"""
        ap = self._mk_pending(mem_store, tasks, goals)
        idea_id = self._insert_idea(mem_store, state="started")
        with mem_store.tx() as conn:
            conn.execute("UPDATE ideas SET task_id='T-77' WHERE id=?", (idea_id,))
        kw = dict(_DEFAULTS)
        kw.update(via="来自构想", idea_id=idea_id)
        r = ap.create(GID, **kw)
        ap.approve(r["id"], by="42")
        row = self._idea_row(mem_store, idea_id)
        assert row["state"] == "started"
        assert row["task_id"] == "T-77"  # 没被覆盖

    def test_reject_pending(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        r = ap.create(GID, **_DEFAULTS)
        out = ap.reject(r["id"], by="42")
        assert out["status"] == "rejected"
        assert out.get("task_id") is None

    def test_approve_non_pending_raises(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        r = ap.create(GID, **_DEFAULTS)
        ap.approve(r["id"], by="42")
        with pytest.raises(ValueError):
            ap.approve(r["id"], by="42")

    def test_reject_after_reject_raises(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        r = ap.create(GID, **_DEFAULTS)
        ap.reject(r["id"], by="42")
        with pytest.raises(ValueError):
            ap.reject(r["id"], by="42")

    def test_reject_after_approve_raises(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        r = ap.create(GID, **_DEFAULTS)
        ap.approve(r["id"], by="42")
        with pytest.raises(ValueError):
            ap.reject(r["id"], by="42")

    def test_approve_missing_raises(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        with pytest.raises(KeyError):
            ap.approve("R-99", by="42")

    def test_events_written(self, mem_store, tasks, goals):
        ap = self._mk_pending(mem_store, tasks, goals)
        r = ap.create(GID, **_DEFAULTS)
        ap.approve(r["id"], by="42")
        rows = mem_store.read().execute(
            "SELECT kind FROM events WHERE entity_id=? ORDER BY id", (r["id"],)
        ).fetchall()
        kinds = [e["kind"] for e in rows]
        assert "request.pending" in kinds and "request.approved" in kinds


class TestPendingView:
    def test_structure_and_order(self, mem_store, tasks, goals, fixed_clock):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting())
        fixed_clock[0] = NOW - 4000
        ap.create(GID, **dict(_DEFAULTS, title="老的", requester_name="甲"))
        fixed_clock[0] = NOW - 1000
        ap.create(GID, **dict(_DEFAULTS, title="新的", requester_name="乙"))
        fixed_clock[0] = NOW
        view = ap.pending_view(GID)
        assert len(view) == 2
        assert view[0]["title"] == "老的"          # 旧的在前
        assert view[0]["who"] == "甲"
        assert view[0]["ts"] == NOW - 4000
        assert view[0]["age_s"] == 4000
        assert view[0]["quote"] == _DEFAULTS["quote"]
        assert "id" in view[0] and "via" in view[0] and "icon" in view[0]

    def test_other_group_excluded(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting())
        ap.create(GID, **_DEFAULTS)
        assert ap.pending_view("别的群") == []


class TestRemindersAndExpiry:
    def _mk_pending(self, mem_store, tasks, goals, fixed_clock, age_s) -> Approvals:
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting())
        fixed_clock[0] = NOW - age_s
        rid = ap.create(GID, **_DEFAULTS)["id"]
        fixed_clock[0] = NOW
        return ap

    def test_due_after_24h(self, mem_store, tasks, goals, fixed_clock):
        ap = self._mk_pending(mem_store, tasks, goals, fixed_clock, 25 * 3600)
        due = ap.due_reminders(NOW)
        assert len(due) == 1
        assert due[0]["id"] == "R-1"
        assert due[0]["who"] == _DEFAULTS["requester_name"]
        assert due[0]["age_s"] == 25 * 3600

    def test_not_due_before_24h(self, mem_store, tasks, goals, fixed_clock):
        ap = self._mk_pending(mem_store, tasks, goals, fixed_clock, 23 * 3600)
        assert ap.due_reminders(NOW) == []

    def test_remind_only_once(self, mem_store, tasks, goals, fixed_clock):
        ap = self._mk_pending(mem_store, tasks, goals, fixed_clock, 30 * 3600)
        assert len(ap.due_reminders(NOW)) == 1
        ap.mark_reminded("R-1", NOW)
        assert ap.due_reminders(NOW + 3600) == []

    def test_expired_after_7days(self, mem_store, tasks, goals, fixed_clock):
        ap = self._mk_pending(mem_store, tasks, goals, fixed_clock, 8 * 86400)
        ids = ap.expire(NOW)
        assert ids == ["R-1"]
        assert ap.pending_view(GID) == []

    def test_not_expired_before_7days(self, mem_store, tasks, goals, fixed_clock):
        ap = self._mk_pending(mem_store, tasks, goals, fixed_clock, 6 * 86400)
        assert ap.expire(NOW) == []
        assert [r["id"] for r in ap.pending_view(GID)] == ["R-1"]


class TestIsAdmin:
    def test_admin_list_matches_by_string(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(admins=("42", "7")))
        assert ap.is_admin("42") is True
        assert ap.is_admin(42) is True      # 数字也能对
        assert ap.is_admin(" 42 ") is True  # 去空格
        assert ap.is_admin("43") is False

    def test_empty_admins(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting())
        assert ap.is_admin("42") is False


class TestCanCancel:
    @pytest.fixture
    def ap(self, mem_store, tasks, goals) -> Approvals:
        return _approvals(mem_store, tasks, goals, _ApprovalSetting(admins=("42",)))

    def _mk_task(self, mem_store, gid: str, requester: str = "10001") -> str:
        with mem_store.tx() as conn:
            from CharTyr_MaiWork.maiwork.store import next_id
            tid = next_id(conn, "T")
            conn.execute(
                "INSERT INTO tasks (id, group_id, workspace, source, requester_id, requester_name,"
                " icon, title, req, criteria, status, env, created, updated)"
                " VALUES (?, ?, 'w', 'request', ?, '甲', 'package', 't', 'r', '[]', 'queued', '', 0, 0)",
                (tid, gid, requester),
            )
        return tid

    def _mk_goal(self, mem_store, gid: str, who: str = "10001") -> str:
        with mem_store.tx() as conn:
            from CharTyr_MaiWork.maiwork.store import next_id
            gid_ = next_id(conn, "G")
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, icon, title, who_id, who_name,"
                " by_text, criteria, state, created, updated)"
                " VALUES (?, ?, 'agent', 'bullseye', 't', ?, '甲', '', '[]', 'active', 0, 0)",
                (gid_, gid, who),
            )
        return gid_

    @pytest.mark.parametrize("kind", ["task", "goal"])
    def test_requester_can_cancel(self, ap, mem_store, kind):
        obj = self._mk_task(mem_store, GID) if kind == "task" else self._mk_goal(mem_store, GID)
        assert ap.can_cancel(kind, obj, "10001", group_role="member") is True

    @pytest.mark.parametrize("kind", ["task", "goal"])
    def test_group_owner_and_admin_can_cancel(self, ap, mem_store, kind):
        obj = self._mk_task(mem_store, GID) if kind == "task" else self._mk_goal(mem_store, GID)
        assert ap.can_cancel(kind, obj, "99999", group_role="owner") is True
        assert ap.can_cancel(kind, obj, "99999", group_role="admin") is True

    @pytest.mark.parametrize("kind", ["task", "goal"])
    def test_bot_admin_can_cancel(self, ap, mem_store, kind):
        obj = self._mk_task(mem_store, GID) if kind == "task" else self._mk_goal(mem_store, GID)
        assert ap.can_cancel(kind, obj, "42", group_role="member") is True

    @pytest.mark.parametrize("kind", ["task", "goal"])
    def test_random_member_cannot_cancel(self, ap, mem_store, kind):
        obj = self._mk_task(mem_store, GID) if kind == "task" else self._mk_goal(mem_store, GID)
        assert ap.can_cancel(kind, obj, "99999", group_role="member") is False


class TestIdeaItemsLanding:
    """2026-10：批准「带项目的构想」→ 按选中的项目逐个建任务 / agent 目标。"""

    ITEMS = [
        {"kind": "task", "title": "抓铝价数据", "desc": "先把最近一个月的铝价抓下来"},
        {"kind": "goal", "title": "每周更新铝价表", "desc": "盯着这件事，每周更新一次"},
        {"kind": "task", "title": "把表发给群友", "desc": ""},
    ]

    def _insert_idea_with_items(self, mem_store, items=None, gid: str = GID) -> int:
        with mem_store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'books', '做个铝价表', '把铝价整理成表', ?, 'new', 1, 1)",
                (gid, json.dumps(self.ITEMS if items is None else items, ensure_ascii=False)),
            )
            return int(cur.lastrowid or 0)

    def test_approve_all_items_creates_tasks_and_goal(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        idea_id = self._insert_idea_with_items(mem_store)
        r = ap.create(GID, **dict(_DEFAULTS, idea_id=idea_id, via="来自构想"))
        out = ap.approve(r["id"], by="42")
        assert out["task_ids"] == ["T-1", "T-2"]     # 两个 task 项目
        assert out["goal_ids"] == ["G-1"]            # 一个 goal 项目
        assert out["task_id"] == "T-1" and out["goal_id"] == "G-1"
        t1 = tasks.get("T-1")
        assert t1["title"] == "抓铝价数据"
        assert "先把最近一个月的铝价抓下来" in t1["req"]
        assert "来自构想" in t1["req"]
        g1 = goals.get("G-1")
        assert g1["title"] == "每周更新铝价表"
        assert g1["body"].startswith("盯着这件事")
        assert "来自构想" in g1["by_text"]
        # 构想标 started 并回写第一个任务
        row = mem_store.read().execute("SELECT state, task_id FROM ideas WHERE id=?", (idea_id,)).fetchone()
        assert row["state"] == "started" and row["task_id"] == "T-1"

    def test_approve_selected_items_only(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        idea_id = self._insert_idea_with_items(mem_store)
        r = ap.create(GID, **dict(_DEFAULTS, idea_id=idea_id, items=[2]))
        out = ap.approve(r["id"], by="42")
        assert out["task_ids"] == [] and out["goal_ids"] == ["G-1"]  # 只做了第 2 项
        assert out["task_id"] is None
        row = mem_store.read().execute("SELECT state FROM ideas WHERE id=?", (idea_id,)).fetchone()
        assert row["state"] == "started"

    def test_out_of_range_selection_falls_back_to_all(self, mem_store, tasks, goals):
        """写了不存在的序号 → 当「全部」处理（别什么都不做）。"""
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        idea_id = self._insert_idea_with_items(mem_store)
        r = ap.create(GID, **dict(_DEFAULTS, idea_id=idea_id, items=[9, 99]))
        out = ap.approve(r["id"], by="42")
        assert out["task_ids"] == ["T-1", "T-2"] and out["goal_ids"] == ["G-1"]

    def test_idea_without_items_keeps_old_single_task(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        with mem_store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'bulb', '老构想', '老正文', '[]', 'pending', 1, 1)",
                (GID,),
            )
            idea_id = int(cur.lastrowid or 0)
        r = ap.create(GID, **dict(_DEFAULTS, idea_id=idea_id, title="老请求"))
        out = ap.approve(r["id"], by="42")
        assert out["task_ids"] == ["T-1"] and out["goal_ids"] == []
        assert tasks.get("T-1")["title"] == "老请求"   # 用请求的标题，不按项目拆

    def test_auto_approved_idea_with_items_also_expands(self, mem_store, tasks, goals):
        """免批路径（required=False）也走同一套拆项目逻辑。"""
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=False))
        idea_id = self._insert_idea_with_items(mem_store)
        r = ap.create(GID, **dict(_DEFAULTS, idea_id=idea_id))
        assert r["status"] == "approved" and r["auto"] is True
        assert r["task_ids"] == ["T-1", "T-2"] and r["goal_ids"] == ["G-1"]

    def test_maiwork_source_never_auto_approves(self, mem_store, tasks, goals):
        """红线：MaiWork 主动提的目标永远要管理员批准，免批群 / required=False 都不生效。"""
        ap = _approvals(
            mem_store, tasks, goals,
            _ApprovalSetting(required=False, exempt_groups=(GID,), exempt_users=("10001",)),
        )
        r = ap.create(
            GID, kind="goal", title="盯着群里的开源项目", quote="看群里最近在聊的",
            via="MaiWork 提议", requester_id="10001", requester_name="MaiWork",
            source="maiwork", force_manual=True,
        )
        assert r["status"] == "pending" and r["auto"] is None
        assert goals.get("G-1") is None            # 没批准就不立目标
        view = ap.pending_view(GID)
        assert len(view) == 1
        assert view[0]["source"] == "maiwork"

    def test_pending_view_exposes_source_and_items(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        ap.create(GID, **dict(_DEFAULTS, source="idea", items=[1, 3]))
        view = ap.pending_view(GID)
        assert view[0]["source"] == "idea"
        assert view[0]["items"] == [1, 3]
        assert view[0]["idea_id"] is None

    def test_create_drops_bad_item_nos(self, mem_store, tasks, goals):
        ap = _approvals(mem_store, tasks, goals, _ApprovalSetting(required=True))
        r = ap.create(GID, **dict(_DEFAULTS, items=[0, -1, "x", 2, 2, 3]))
        view = ap.pending_view(GID)
        assert view[0]["items"] == [2, 3]
        assert r["status"] == "pending"
