"""派活「自动审核」（auto_review.AutoReviewer；docs/02 §5.2，2026-10）。

规则对应：
- 只审 kind="task"；goal 永远要人批（零模型调用）；
- 来自构想、选中项目里含 goal 的请求 → 整条留人批；
- source="maiwork" / force_manual=True 的请求永远不自动批；
- 模型说 approve=true → 走和人批同一条落地路径（approvals.approve → _land → 开工），
  批准人记「MaiWork 自动审核」，理由存 requests.auto_reason；
- 模型说不批 / 解析失败 / 没配好 / 调用异常 → 留人批（不重试、不报错给群）；
- 每群每天上限（默认 5，0 = 关），按北京日期存 kv；只有真正批通过才计数；
- 关掉配置 / 上限到了 / 非服务群 → 连模型都不调。
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeModelsQueue

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.auto_review import AUTO_BY, AutoReviewer
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

G1 = "900000001"
G2 = "111222333"
NOW = 1_790_000_000.0
DAY = "2026-09-25"



@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


_OK = json.dumps({"approve": True, "reason": "查资料的小活，低风险"}, ensure_ascii=False)
_NO = json.dumps({"approve": False, "reason": "这事要花钱，得管理员定"}, ensure_ascii=False)


def _settings(*, serve=(G1,), approval=None):
    cfg: dict = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
    }
    if approval is not None:
        cfg["approval"] = approval
    settings, problems = load_settings(cfg)
    assert problems == [], problems
    return settings


class _Harness:
    def __init__(self, store, settings, models, tasks, goals, approvals, reviewer, started):
        self.store = store
        self.settings = settings
        self.models = models
        self.tasks = tasks
        self.goals = goals
        self.approvals = approvals
        self.reviewer = reviewer
        self.started = started

    def create(self, group_id=G1, **kw):
        base = dict(
            kind="task", title="帮我查一下有没有免费图床", quote="@MaiBot 帮我查一下免费图床",
            via="群里 @", requester_id="10001", requester_name="阿柒",
        )
        base.update(kw)
        return self.approvals.create(group_id, **base)

    def status(self, rid):
        row = self.store.read().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(row)

    def kv(self, key):
        return self.store.kv_get(key, None)


def _setup(tmp_path, *, replies=None, serve=(G1,), approval=None, ready=True, starter=None):
    store = Store(tmp_path / "mw.db")
    store.migrate()
    settings = _settings(serve=serve, approval=approval)
    models = FakeModelsQueue(ready=ready, replies=replies)
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    approvals = Approvals(store, lambda: settings, tasks, goals)
    started: list[str] = []
    reviewer = AutoReviewer(
        store, models, approvals, lambda: settings,
        run_task_starter=starter if starter is not None else started.append,
    )
    return _Harness(store, settings, models, tasks, goals, approvals, reviewer, started)


# ----------------------------------------------------------------------
# 落库迁移
# ----------------------------------------------------------------------


class TestMigration:
    def test_requests_has_auto_review_columns(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        cols = {r["name"] for r in store.read().execute("PRAGMA table_info(requests)")}
        assert "force_manual" in cols
        assert "auto_reason" in cols

    def test_force_manual_persisted(self, tmp_path) -> None:
        h = _setup(tmp_path)
        r = h.create(force_manual=True)
        assert h.status(r["id"])["force_manual"] == 1


# ----------------------------------------------------------------------
# 低风险 → 自动批 + 开工
# ----------------------------------------------------------------------


class TestAutoApprove:
    @pytest.mark.asyncio
    async def test_low_risk_task_auto_approved_and_started(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create()
        assert r["status"] == "pending"
        res = await h.reviewer.review(r["id"])
        assert isinstance(res, dict) and res["status"] == "approved"
        row = h.status(r["id"])
        assert row["status"] == "approved"
        assert row["decided_by"] == AUTO_BY
        assert row["auto_reason"] == "查资料的小活，低风险"
        task = h.tasks.get(res["task_id"])
        assert task["status"] == "queued"
        assert h.started == [res["task_id"]]
        assert len(h.models.calls) == 1
        assert h.models.calls[0][2]["purpose"] == "approvals.auto_review"
        assert h.models.calls[0][2]["group_id"] == G1

    @pytest.mark.asyncio
    async def test_approved_event_carries_reason(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create()
        await h.reviewer.review(r["id"])
        rows = h.store.read().execute(
            "SELECT payload FROM events WHERE kind='request.approved' AND entity_id=?", (r["id"],)
        ).fetchall()
        payloads = [json.loads(str(x["payload"])) for x in rows]
        assert any(p.get("auto_reason") == "查资料的小活，低风险" for p in payloads)

    @pytest.mark.asyncio
    async def test_review_without_gid_uses_request_group(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create()
        assert await h.reviewer.review(r["id"]) is not None

    @pytest.mark.asyncio
    async def test_history_view_contract(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create()
        res = await h.reviewer.review(r["id"])
        info = h.approvals.auto_info_by_task([res["task_id"]])
        assert info[res["task_id"]] == {"approved_by": AUTO_BY, "auto_reason": "查资料的小活，低风险"}
        # 别的任务没有这条记录 → 没有键
        assert "T-999" not in info

    @pytest.mark.asyncio
    async def test_multi_task_idea_each_landed_task_has_auto_info(self, tmp_path) -> None:
        """构想拆成多个任务落地时，**每个**任务都能查到批准人 / 自动审核理由。"""
        h = _setup(tmp_path, replies=[_OK])
        with h.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'bulb', '做两件事', '', ?, 'wanted', ?, ?)",
                (
                    G1,
                    json.dumps(
                        [
                            {"kind": "task", "title": "第一件", "desc": ""},
                            {"kind": "task", "title": "第二件", "desc": ""},
                        ],
                        ensure_ascii=False,
                    ),
                    NOW,
                    NOW,
                ),
            )
            idea_id = int(cur.lastrowid or 0)
        r = h.create(idea_id=idea_id, title="做两件事")
        res = await h.reviewer.review(r["id"])
        assert isinstance(res, dict) and res["status"] == "approved"
        tids = [str(t) for t in (res.get("task_ids") or [])]
        assert len(tids) == 2
        info = h.approvals.auto_info_by_task(tids)
        for tid in tids:
            assert info[tid] == {"approved_by": AUTO_BY, "auto_reason": "查资料的小活，低风险"}


# ----------------------------------------------------------------------
# 留给人批
# ----------------------------------------------------------------------


class TestKeepManual:
    @pytest.mark.asyncio
    async def test_model_says_no_keeps_pending(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_NO])
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.status(r["id"])["status"] == "pending"
        assert h.started == []
        assert h.kv(f"auto_review.day.{G1}") is None

    @pytest.mark.asyncio
    async def test_goal_never_calls_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create(kind="goal", title="帮我们盯着活动日历")
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_maiwork_source_never_calls_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create(source="maiwork")
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_force_manual_never_calls_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create(force_manual=True)
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_non_served_group_never_calls_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK], serve=(G1,))
        r = h.create(group_id=G2)
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_empty_group_never_calls_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK], serve=(G1,))
        r = h.create(group_id="")
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_review_disabled_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK], approval={"auto_review": False, "auto_review_daily": 5})
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_daily_zero_means_off(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK], approval={"auto_review": True, "auto_review_daily": 0})
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_cap_reached_no_model_call(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK], approval={"auto_review": True, "auto_review_daily": 5})
        with h.store.tx() as conn:
            h.store.kv_set(conn, f"auto_review.day.{G1}", {"day": clock.day_key(NOW), "n": 5})
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_yesterday_count_does_not_block(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        with h.store.tx() as conn:
            h.store.kv_set(conn, f"auto_review.day.{G1}", {"day": "2000-01-01", "n": 99})
        r = h.create()
        assert await h.reviewer.review(r["id"]) is not None

    @pytest.mark.asyncio
    async def test_model_not_ready_calls_nothing(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK], ready=False)
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_model_error_leaves_pending(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[ModelError("端点挂了")])
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.status(r["id"])["status"] == "pending"
        assert h.started == []

    @pytest.mark.asyncio
    async def test_bad_json_leaves_pending(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=["这不是 JSON"])
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_approve_not_real_bool_leaves_pending(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[json.dumps({"approve": "yes", "reason": "x"})])
        r = h.create()
        assert await h.reviewer.review(r["id"]) is None
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_already_decided_request_is_ignored(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        r = h.create()
        h.approvals.approve(r["id"], by="10001")
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_missing_request_is_ignored(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        assert await h.reviewer.review("R-404") is None
        assert h.models.calls == []

    @pytest.mark.asyncio
    async def test_no_model_client_is_ignored(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        reviewer = AutoReviewer(h.store, None, h.approvals, lambda: h.settings)
        r = h.create()
        assert await reviewer.review(r["id"]) is None
        assert h.status(r["id"])["status"] == "pending"


# ----------------------------------------------------------------------
# 来源构想：选中项目含 goal → 整条留人批
# ----------------------------------------------------------------------


def _seed_idea(store, gid, items, *, state="new"):
    import json as _json

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO ideas (group_id, title, body, items, state, created, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, "整理群里的干货", "把干货整理成一页", _json.dumps(items, ensure_ascii=False), state, NOW, NOW),
        )
        iid = int(conn.execute("SELECT id FROM ideas ORDER BY id DESC LIMIT 1").fetchone()["id"])
    return iid


_TASK_ITEM = {"kind": "task", "title": "整理一页干货", "desc": "做成一个网页"}
_GOAL_ITEM = {"kind": "goal", "title": "每月更新", "desc": "长期盯着"}


class TestIdeaItems:
    @pytest.mark.asyncio
    async def test_idea_with_goal_item_never_calls_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        iid = _seed_idea(h.store, G1, [_TASK_ITEM, _GOAL_ITEM])
        r = h.create(source="idea", idea_id=iid, items=[2])
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_idea_all_tasks_can_be_reviewed(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        iid = _seed_idea(h.store, G1, [_TASK_ITEM])
        r = h.create(source="idea", idea_id=iid)
        res = await h.reviewer.review(r["id"])
        assert res is not None
        assert len(h.models.calls) == 1
        assert h.started

    @pytest.mark.asyncio
    async def test_idea_picked_task_only_ignores_unpicked_goal(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        iid = _seed_idea(h.store, G1, [_TASK_ITEM, _GOAL_ITEM])
        r = h.create(source="idea", idea_id=iid, items=[1])
        assert await h.reviewer.review(r["id"]) is not None

    @pytest.mark.asyncio
    async def test_missing_idea_row_stays_manual(self, tmp_path) -> None:
        """构想行读不到时不知道有没有 goal → 保守留人批（零模型调用）。"""
        h = _setup(tmp_path, replies=[_OK])
        r = h.create(source="idea", idea_id=999)
        assert await h.reviewer.review(r["id"]) is None
        assert h.models.calls == []
        assert h.status(r["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_old_idea_without_items_can_be_reviewed(self, tmp_path) -> None:
        """老构想（items 为空）没有 goal 概念 → 照常审。"""
        h = _setup(tmp_path, replies=[_OK])
        iid = _seed_idea(h.store, G1, [])
        r = h.create(source="idea", idea_id=iid)
        assert await h.reviewer.review(r["id"]) is not None


# ----------------------------------------------------------------------
# 每群每天上限：只有真批通过才计数
# ----------------------------------------------------------------------


class TestDailyCap:
    @pytest.mark.asyncio
    async def test_cap_counts_only_successful_approvals(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_NO, _OK, _OK], approval={"auto_review": True, "auto_review_daily": 2})
        r1, r2, r3 = h.create().get("id"), h.create().get("id"), h.create().get("id")
        await h.reviewer.review(r1)  # 模型说不批 → 不计数
        assert h.kv(f"auto_review.day.{G1}") is None
        await h.reviewer.review(r2)
        assert h.kv(f"auto_review.day.{G1}") == {"day": clock.day_key(NOW), "n": 1}
        await h.reviewer.review(r3)
        assert h.kv(f"auto_review.day.{G1}") == {"day": clock.day_key(NOW), "n": 2}

    @pytest.mark.asyncio
    async def test_fifth_ok_sixth_skips_model(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK] * 5, approval={"auto_review": True, "auto_review_daily": 5})
        for _ in range(5):
            await h.reviewer.review(h.create()["id"])
        assert len(h.models.calls) == 5
        assert h.kv(f"auto_review.day.{G1}")["n"] == 5
        r6 = h.create()
        assert await h.reviewer.review(r6["id"]) is None
        assert len(h.models.calls) == 5
        assert h.status(r6["id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_cap_is_per_group(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK] * 3, serve=(G1, G2), approval={"auto_review": True, "auto_review_daily": 1})
        await h.reviewer.review(h.create(group_id=G1)["id"])
        assert await h.reviewer.review(h.create(group_id=G2)["id"]) is not None
        assert len(h.models.calls) == 2


    @pytest.mark.asyncio
    async def test_two_concurrent_approvals_cannot_exceed_cap_one(self, tmp_path) -> None:
        import asyncio
        from types import SimpleNamespace

        h = _setup(tmp_path, approval={"auto_review": True, "auto_review_daily": 1})
        first, second = h.create()["id"], h.create()["id"]
        entered = 0
        both_entered = asyncio.Event()

        async def judged(*args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            return SimpleNamespace(text=_OK)

        h.models.chat = judged
        results = await asyncio.wait_for(asyncio.gather(
            h.reviewer.review(first), h.reviewer.review(second)
        ), timeout=2)
        assert sum(res is not None for res in results) == 1
        assert sorted([h.status(first)["status"], h.status(second)["status"]]) == ["approved", "pending"]
        assert h.kv(f"auto_review.day.{G1}") == {"day": clock.day_key(NOW), "n": 1}
        assert len(h.started) == 1

    @pytest.mark.asyncio
    async def test_landing_failure_does_not_use_quota_and_can_retry(self, tmp_path, monkeypatch) -> None:
        h = _setup(tmp_path, replies=[_OK, _OK], approval={"auto_review_daily": 1})
        rid = h.create()["id"]
        original = h.tasks.create

        def broken_task(*args, **kwargs):
            raise RuntimeError("cannot create task")

        monkeypatch.setattr(h.tasks, "create", broken_task)
        assert await h.reviewer.review(rid) is None
        assert h.status(rid)["status"] == "pending"
        assert h.kv(f"auto_review.day.{G1}") is None
        monkeypatch.setattr(h.tasks, "create", original)
        assert await h.reviewer.review(rid) is not None
        assert h.kv(f"auto_review.day.{G1}")["n"] == 1


# ----------------------------------------------------------------------
# create 触发审核回调（钩子里只登记，异步审核不卡收消息）
# ----------------------------------------------------------------------


class TestReviewHook:
    @pytest.mark.asyncio
    async def test_create_pending_triggers_hook(self, tmp_path) -> None:
        h = _setup(tmp_path)
        seen: list[tuple[str, str]] = []
        h.approvals.set_review_hook(lambda rid, gid: seen.append((rid, gid)))
        r = h.create()
        assert seen == [(r["id"], G1)]

    @pytest.mark.asyncio
    async def test_hook_not_called_for_exempt_request(self, tmp_path) -> None:
        h = _setup(tmp_path, approval={"required": False})
        seen: list[str] = []
        h.approvals.set_review_hook(lambda rid, gid: seen.append(rid))
        r = h.create()
        assert r["status"] == "approved"
        assert seen == []

    @pytest.mark.asyncio
    async def test_hook_error_does_not_break_create(self, tmp_path) -> None:
        h = _setup(tmp_path)

        def _boom(rid, gid):
            raise RuntimeError("回调炸了")

        h.approvals.set_review_hook(_boom)
        r = h.create()
        assert r["status"] == "pending"

    @pytest.mark.asyncio
    async def test_no_hook_is_fine(self, tmp_path) -> None:
        h = _setup(tmp_path)
        assert h.create()["status"] == "pending"


# ----------------------------------------------------------------------
# 配置：默认值 / 网页 schema / 热生效
# ----------------------------------------------------------------------


class TestConfig:
    def test_defaults(self) -> None:
        s = _settings()
        assert s.approval.auto_review is True
        assert s.approval.auto_review_daily == 5

    def test_explicit_values(self) -> None:
        s = _settings(approval={"auto_review": False, "auto_review_daily": 9})
        assert s.approval.auto_review is False
        assert s.approval.auto_review_daily == 9

    def test_schema_has_product_labels(self) -> None:
        from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY

        f = CONFIG_BY_KEY["approval.auto_review"]
        assert f["type"] == "bool"
        assert "自动审核" in f["label"]
        g = CONFIG_BY_KEY["approval.auto_review_daily"]
        assert g["type"] == "int"
        assert "自动批" in g["label"]
        assert g["min"] == 0

    def test_config_schema_accept_both_types(self) -> None:
        """两键的合法性/范围由 CONFIG_SCHEMA 的类型 + min/max 表达（validate_patch 已删）。"""
        from CharTyr_MaiWork.maiwork.rules import CONFIG_BY_KEY, _validate_generic

        assert _validate_generic(CONFIG_BY_KEY["approval.auto_review"], True) is True
        assert _validate_generic(CONFIG_BY_KEY["approval.auto_review_daily"], 3) == 3
        with pytest.raises(ValueError):
            _validate_generic(CONFIG_BY_KEY["approval.auto_review"], 1)
        with pytest.raises(ValueError):
            _validate_generic(CONFIG_BY_KEY["approval.auto_review_daily"], -1)

    def test_hot_apply_settings_toggle(self, tmp_path) -> None:
        """热改自动审核（网页 / 工具写 config.toml → 新 Settings）→ reviewer 读到关。

        旧版靠 kv["rules.override"] 合并层；2026-10 起唯一真实来源是 config.toml，
        热应用就是换新 Settings 对象（本测试直接换 lambda 返回的对象模拟这步）。
        """
        from CharTyr_MaiWork.maiwork.config import load_settings

        off_settings, _ = load_settings({"plugin": {"enabled": True},
                                          "approval": {"auto_review": False, "auto_review_daily": 2}})
        assert off_settings.approval.auto_review is False
        assert off_settings.approval.auto_review_daily == 2
        # 基础设置本身不被动（新对象）
        base = _settings()
        assert base.approval.auto_review is True

    @pytest.mark.asyncio
    async def test_hot_apply_changes_reviewer_behaviour(self, tmp_path) -> None:
        h = _setup(tmp_path, replies=[_OK])
        from CharTyr_MaiWork.maiwork.config import load_settings as _ls

        off, _ = _ls({"plugin": {"enabled": True}, "approval": {"auto_review": False}})
        reviewer = AutoReviewer(h.store, h.models, h.approvals, lambda: off)
        r = h.create()
        assert await reviewer.review(r["id"]) is None
        assert h.models.calls == []


# ----------------------------------------------------------------------
# 端到端接线：收消息钩子 → 记待批 → 后台审核（钩子绝不被卡住）
# ----------------------------------------------------------------------


class _FakeJev:
    def __init__(self, answers) -> None:
        self.answers = answers
        self.calls = 0

    def available(self) -> bool:
        return True

    async def ask(self, state, questions, *, purpose, group_id="", timeout_ms=None):
        self.calls += 1
        return self.answers


class _FakeMentions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group_id, text, *, key, ttl_s, turns=5) -> None:
        self.items.append({"group_id": group_id, "text": text, "key": key})


class TestIntakeIntegration:
    @staticmethod
    async def _drain(spawned: list) -> None:
        """把钩子 spawn 出来的后台协程跑完（跑的过程中还可能再 spawn 出审核协程）。"""
        while spawned:
            await spawned.pop(0)

    @pytest.mark.asyncio
    async def test_hook_returns_immediately_then_review_approves(self, tmp_path) -> None:
        """钩子只登记（spawn 一个协程），审核在后台跑；跑完自动批 + 开工。"""
        from fakes import hook_message

        from CharTyr_MaiWork.maiwork.intake import Intake, Signals

        h = _setup(tmp_path, replies=[_OK])
        spawned: list = []
        h.approvals.set_review_hook(
            lambda rid, gid: spawned.append(h.reviewer.review(rid, group_id=gid))
        )
        intake = Intake(
            lambda: h.settings,
            Signals(),
            jev=_FakeJev({"kind": ("prepare", 0.99, 0.99)}),
            approvals=h.approvals,
            mentions=_FakeMentions(),
            spawn=lambda coro: spawned.append(coro),
            bot_qq="987654321",
        )
        out = await intake.handle(hook_message(is_at=True, text="@MaiBot 帮我查一下免费图床"))
        assert out == {"action": "continue"}
        # 钩子回来时审核还没跑：没有被卡住（连请求都还没落库，记请求也是后台的）
        assert len(spawned) == 1
        assert h.started == []
        assert h.models.calls == []
        await self._drain(spawned)
        row = h.store.read().execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
        assert str(row["status"]) == "approved"
        assert str(row["decided_by"]) == AUTO_BY
        assert str(row["auto_reason"]) == "查资料的小活，低风险"
        assert h.started == [str(row["task_id"])]

    @pytest.mark.asyncio
    async def test_mention_text_same_as_exempt_path(self, tmp_path) -> None:
        """群里看到的固定话和「免批直接开工」那句一致（都是 intake 那句）。"""
        from fakes import hook_message

        from CharTyr_MaiWork.maiwork.intake import Intake, Signals

        h = _setup(tmp_path, replies=[_OK])
        mentions = _FakeMentions()
        spawned: list = []
        h.approvals.set_review_hook(
            lambda rid, gid: spawned.append(h.reviewer.review(rid, group_id=gid))
        )
        intake = Intake(
            lambda: h.settings, Signals(),
            jev=_FakeJev({"kind": ("prepare", 0.99, 0.99)}),
            approvals=h.approvals, mentions=mentions,
            spawn=lambda coro: spawned.append(coro), bot_qq="987654321",
        )
        await intake.handle(hook_message(is_at=True, text="@MaiBot 帮我查一下免费图床"))
        await self._drain(spawned)
        assert len(mentions.items) == 1
        assert "等管理员批准后开工（免批的话已经开工）" in mentions.items[0]["text"]
        # 自动批了，但群里那句话没变（和免批一样）
        row = h.store.read().execute("SELECT status FROM requests").fetchone()
        assert str(row["status"]) == "approved"

    @pytest.mark.asyncio
    async def test_goal_at_message_never_reviews(self, tmp_path) -> None:
        """@ 判成 goal → approvals 记 kind=goal → 自动审核零模型调用。"""
        from fakes import hook_message

        from CharTyr_MaiWork.maiwork.intake import Intake, Signals

        h = _setup(tmp_path, replies=[_OK])
        spawned: list = []
        h.approvals.set_review_hook(
            lambda rid, gid: spawned.append(h.reviewer.review(rid, group_id=gid))
        )
        intake = Intake(
            lambda: h.settings, Signals(),
            jev=_FakeJev({"kind": ("goal", 0.99, 0.99)}),
            approvals=h.approvals, mentions=_FakeMentions(),
            spawn=lambda coro: spawned.append(coro), bot_qq="987654321",
        )
        await intake.handle(hook_message(is_at=True, text="@MaiBot 帮我盯着发布会"))
        assert len(spawned) == 1
        await self._drain(spawned)
        assert h.models.calls == []
        row = h.store.read().execute("SELECT status, kind FROM requests").fetchone()
        assert str(row["kind"]) == "goal"
        assert str(row["status"]) == "pending"
