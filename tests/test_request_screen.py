"""群友 @ 的请求：类似的请求被管理员拒绝多次后，不再往待批里放。

线上实测（2026-09-30）：「@東雪蓮 帮我取消群友的国庆七天假期」被 Jev 判成「帮忙盯着或做成」
（把握 0.61）进了待批——这是玩笑 / 办不到的事，不该当任务。

规则：
- 只数本群、近 60 天、群友 @ 来的（source 空）、状态 rejected 的请求；
- 新来的这条和其中 ≥3 条「像」（difflib ≥0.6，去掉 @人名 和「帮我」这类客套话再比）→ 不建请求，记一条事件；
- 构想 / MaiWork 自己提的被拒，不算；别的群被拒，不算；默认（不传 screen）不筛；
- Jev 把握不到 0.7 的「准备 / 盯着」不直接建请求，进慢路径让主模型再判。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals, _screen_text
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

NOW = 1_790_000_000.0
GID = "900000001"
OTHER = "111222333"


class _Approval:
    required = True
    admins = ()
    exempt_groups = ()
    exempt_users = ()


class _Settings:
    approval = _Approval()

    def workspace_of(self, group_id: str) -> str:
        return "ws-demo"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def ap(store: Store) -> Approvals:
    return Approvals(store, lambda: _Settings(), Tasks(store, lambda: _Settings()), Goals(store, lambda: _Settings()))


def _new(ap: Approvals, text: str, *, gid: str = GID, source: str = "", screen: bool = True, mid: str = "m") -> dict:
    return ap.create(
        gid, kind="goal", title=text[:30], quote=text, via="群里 @ · Jev 判断是「帮忙盯着或做成」（把握 0.61）",
        requester_id="10001", requester_name="阿柒", message_id=mid, source=source, screen=screen,
    )


def _reject(ap: Approvals, text: str, **kw) -> str:
    r = _new(ap, text, screen=False, **kw)
    ap.reject(r["id"], by="42")
    return r["id"]


def _count(store: Store) -> int:
    return int(store.read().execute("SELECT COUNT(*) FROM requests").fetchone()[0])


HOLIDAYS = [
    "@東雪蓮 帮我取消群友的国庆七天假期",
    "@東雪蓮 帮我取消群友的春节假期",
    "取消群友的中秋假期",
]


class TestScreenText:
    def test_strips_mentions_polite_words_and_punct(self):
        assert _screen_text("@東雪蓮 帮我取消群友的国庆七天假期！") == "取消群友的国庆七天假期"

    def test_empty(self):
        assert _screen_text("  @a  ") == ""


class TestScreenByRejections:
    def test_three_similar_rejections_block_the_fourth(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, mid=f"r{i}")
        before = _count(store)
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        assert out["status"] == "screened"
        assert not out.get("id")
        assert _count(store) == before  # 没建请求

    def test_screening_leaves_an_event(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, mid=f"r{i}")
        _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        kinds = [r["kind"] for r in store.read().execute("SELECT kind FROM events").fetchall()]
        assert "request.screened" in kinds

    def test_two_rejections_are_not_enough(self, ap, store):
        for i, t in enumerate(HOLIDAYS[:2]):
            _reject(ap, t, mid=f"r{i}")
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        assert out["status"] == "pending" and out["id"]

    def test_different_request_is_not_blocked(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, mid=f"r{i}")
        out = _new(ap, "@東雪蓮 帮我整理一份铝价周报", mid="new")
        assert out["status"] == "pending"

    def test_shared_words_alone_are_not_similar(self, ap, store):
        """只共享「国庆假期」四个字、动作不同（整理 vs 取消）：不算同类。"""
        for i in range(3):
            _reject(ap, "取消国庆假期", mid=f"r{i}")
        out = _new(ap, "整理国庆假期攻略", mid="new")
        assert out["status"] == "pending"

    def test_other_group_rejections_do_not_count(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, gid=OTHER, mid=f"r{i}")
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        assert out["status"] == "pending"

    def test_idea_and_maiwork_sourced_rejections_do_not_count(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, source="idea" if i % 2 else "maiwork", mid=f"r{i}")
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        assert out["status"] == "pending"

    def test_old_rejections_expire(self, ap, store, fixed_clock):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, mid=f"r{i}")
        fixed_clock[0] = NOW + 61 * 86400
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        assert out["status"] == "pending"

    def test_approved_or_pending_similar_do_not_count(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            r = _new(ap, t, screen=False, mid=f"r{i}")
            if i == 0:
                ap.approve(r["id"], by="42")
            # 其余留 pending
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", mid="new")
        assert out["status"] == "pending"

    def test_default_does_not_screen(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, mid=f"r{i}")
        out = _new(ap, "@東雪蓮 帮我取消群友的元旦假期", screen=False, mid="new")
        assert out["status"] == "pending"

    def test_empty_text_never_screened(self, ap, store):
        for i, t in enumerate(HOLIDAYS):
            _reject(ap, t, mid=f"r{i}")
        out = _new(ap, "@東雪蓮", mid="new")
        assert out["status"] == "pending"


class TestIntake:
    @pytest.mark.asyncio
    async def test_intake_passes_screen_true_and_says_nothing_when_screened(self):
        from CharTyr_MaiWork.maiwork.config import load_settings
        from CharTyr_MaiWork.maiwork.intake import Intake, Signals

        seen: list[dict] = []
        added: list = []

        class _Ap:
            def create(self, gid, **kw):
                seen.append(kw)
                return {"id": "", "status": "screened", "auto": None}

        class _Mentions:
            def add(self, *a, **k):
                added.append((a, k))

        settings, _ = load_settings({"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{GID}"}]}})
        intake = Intake(lambda: settings, Signals(), approvals=_Ap(), mentions=_Mentions(), spawn=lambda c: None)
        await intake._create_request(GID, "goal", 0.9, "10001", "阿柒", "m1", "帮我取消群友的假期")
        assert seen and seen[0]["screen"] is True
        assert added == []  # 被挡掉的不往「可提起」里写「已记下」

    @pytest.mark.asyncio
    async def test_borderline_confidence_goes_to_slow_path_not_request(self):
        """把握 0.61（线上那条）进慢路径让主模型再判，不直接建请求；0.7 起才直接建。"""
        from fakes import hook_message
        from test_intake_m3 import _FakeApprovals, _FakeJev, _drain, _make

        for conf, want_created in ((0.61, 0), (0.69, 0), (0.7, 1), (0.9, 1)):
            jev = _FakeJev({"kind": ("goal", 0.8, conf)})
            approvals = _FakeApprovals()
            intake, _signals, spawned = _make(jev=jev, approvals=approvals)
            await intake.handle(hook_message(is_at=True, text="帮我取消群友的国庆七天假期"))
            await _drain(spawned)
            assert len(approvals.created) == want_created, conf
            if not want_created:
                assert len(intake.slow_queue) == 1, conf

    @pytest.mark.asyncio
    async def test_reminder_and_none_keep_the_old_0_6_line(self):
        from fakes import hook_message
        from test_intake_m3 import _FakeApprovals, _FakeJev, _drain, _make

        jev = _FakeJev({"kind": ("none", 0.9, 0.62)})
        intake, _s, spawned = _make(jev=jev, approvals=_FakeApprovals())
        await intake.handle(hook_message(is_at=True, text="哈哈"))
        await _drain(spawned)
        assert intake.slow_queue == []  # none 0.62 仍算「明确闲聊」

    def test_jev_choices_rule_out_real_world_and_prank_asks(self):
        from CharTyr_MaiWork.maiwork.intake import _AT_QUESTIONS

        c = _AT_QUESTIONS["kind"]["criteria"]
        assert "现实" in c["none"] and "玩笑" in c["none"]
        assert "网上" in c["goal"] or "线上" in c["goal"]
