"""待复核 @ 请求的汇总提醒 + 超时归档（2026-10-15 线上实测）。

线上 pending_asks 有 9 条 state='review'（判断服务把握 0.21~0.54 拿不准被搁置），
最老一条等了 17 天；日志只有一句 warning，管理员根本不知道。这个文件盯四件事：

1. `PendingAsks.archive_expired`：等了超过 14 天还没人处理的待复核 → 终态 `expired`
   （保留记录、记一条中文 info 日志），阈值是模块常量 `REVIEW_EXPIRED_S`；
   归档挂在已有后台巡检（`Profiles.recover_pending_asks` 里挨着 `mark_stale`）。
2. `views.asks_review_summary`：全部服务群的待复核汇总（数量 / 最老等了多久 / 每条明细），
   不把异常原文送出去。
3. 网页接口：`GET /api/asks/review`（只总管理员）与 `GET /api/groups/{gid}/pending-asks`
   （总管理员 / 本群群管理员）的鉴权与形状。
4. 首页/导航汇总：`today.asks_review` 计数（照着 today.pending 加一项，前端直接显示）。

先写先红：archive_expired / asks_review_summary / 两个 GET 路由 / today.asks_review
在实现之前都不存在，这一整个文件应当失败，再写实现让它变绿。
"""

from __future__ import annotations

import json
import logging
import types
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

import pytest

import test_group_admin as group_admin
from test_group_admin import ADMIN_PW, G1, G1_PW, G2, G2_PW

env = group_admin.env  # 复用 test_group_admin 的 aiohttp 环境（真 MaiWorkApp + 真控制台）

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console import views
from CharTyr_MaiWork.maiwork.pending_asks import REVIEW_EXPIRED_S, PendingAsks
from CharTyr_MaiWork.maiwork.store import Store

T0 = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0


# ----------------------------------------------------------------------
# 共用小工具
# ----------------------------------------------------------------------


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch):
    box = {"t": T0}
    monkeypatch.setattr(clock, "now", lambda: box["t"])
    return box


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


def _settings(gids: tuple[str, ...] = (G1, G2)):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in gids]},
    }
    settings, problems = load_settings(raw)
    assert not problems, problems
    return settings


def _review(pa: PendingAsks, gid: str, mid: str, *, days_ago: float, name: str = "某成员",
            text: str = "帮我整理资料") -> dict:
    """造一条「等了 days_ago 天的待复核」（>48 小时才会被标 review）。"""
    pa.record_pending(
        gid,
        {"message_id": mid, "user_id": "u1", "user_name": name, "text": text,
         "timestamp": T0 - days_ago * DAY},
        "jev_超时",
    )
    pa.mark_stale(gid, now=T0)
    row = pa.get(gid, mid)
    assert row is not None and row["state"] == "review", row
    return row


class _Svc:
    """只给 views 用的最小服务包（真 Store + 真 Settings + 真 PendingAsks）。"""

    def __init__(self, store: Store, settings: Any, pa: Any) -> None:
        self.store = store
        self._settings = settings
        self.profiles = types.SimpleNamespace(pending_asks=pa, usual_gap=lambda gid, at: None)

    def get_settings(self):
        return self._settings


class _FakeAsks:
    """精简假对象：真值计数和列表长度故意不一致（250 条真值 / 只列 100 条）。

    真实 PendingAsks 造 250 行太慢；这个替身只实现 views 用到的两个方法，
    盯的是「群计数必须走 review_count，不许拿 items 数出来凑」。
    """

    def __init__(self, counts: dict[str, int], rows: dict[str, list[dict]]) -> None:
        self._counts = {str(k): int(v) for k, v in counts.items()}
        self._rows = {str(k): list(v) for k, v in rows.items()}

    def review_count(self, gid: str) -> int:
        return self._counts.get(str(gid), 0)

    def review_view(self, gid: str, *, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._rows.get(str(gid), [])][: min(100, max(1, int(limit)))]


def _fake_row(mid: str, *, ts: float, text: str = "帮我整理资料") -> dict:
    return {"message_id": mid, "user_id": "u1", "user_name": "某成员", "text": text,
            "ts": ts, "first_seen": ts, "attempts": 0, "last_error": "", "decision": None}


# ----------------------------------------------------------------------
# 1. 超时归档
# ----------------------------------------------------------------------


class TestArchiveExpired:
    def test_threshold_is_a_module_constant(self) -> None:
        assert REVIEW_EXPIRED_S == 14 * 24 * 3600

    def test_older_than_14_days_becomes_expired(self, store: Store, now, caplog) -> None:
        pa = PendingAsks(store, _settings)
        _review(pa, G1, "old", days_ago=17, name="阿柒")
        with caplog.at_level(logging.INFO, logger="maiwork.pending_asks"):
            assert pa.archive_expired(G1, now=T0) == 1
        row = pa.get(G1, "old")
        assert row["state"] == "expired"
        assert row["handled"] == 1 and row["review"] == 0
        assert row["updated"] == T0
        logs = [r.getMessage() for r in caplog.records]
        assert any("过期" in m and G1 in m for m in logs), logs

    def test_within_14_days_stays_for_review(self, store: Store, now) -> None:
        pa = PendingAsks(store, _settings)
        _review(pa, G1, "fresh", days_ago=13)
        assert pa.archive_expired(G1, now=T0) == 0
        assert pa.get(G1, "fresh")["state"] == "review"

    def test_expired_is_terminal_and_keeps_row(self, store: Store, now) -> None:
        pa = PendingAsks(store, _settings)
        _review(pa, G1, "old", days_ago=20)
        assert pa.archive_expired(G1, now=T0) == 1
        # 归档之后不许被重新打开 / 复活（记录留着，只是不再自动重试）
        assert pa.decide(G1, "old", {"kind": "prepare", "title": "x"}) is False
        assert pa.fail(G1, "old", "再来一次") is False
        assert pa.complete(G1, "old", result_kind="request", result_id="R-1") is False
        assert pa.mark_stale(G1, now=T0) == 0
        row = pa.get(G1, "old")
        assert row is not None and row["state"] == "expired" and row["handled"] == 1
        assert pa.due(G1, now=T0 + 999999) == []
        assert pa.undecided(G1, before=T0, now=T0) == []

    def test_per_group_and_idempotent(self, store: Store, now) -> None:
        pa = PendingAsks(store, _settings)
        _review(pa, G1, "a", days_ago=15)
        _review(pa, G2, "b", days_ago=15)
        assert pa.archive_expired(G1, now=T0) == 1
        assert pa.get(G2, "b")["state"] == "review"  # 别的群不动
        assert pa.archive_expired(G1, now=T0) == 0   # 幂等
        assert pa.get(G1, "a")["state"] == "expired"

    def test_non_served_group_is_zero_work(self, store: Store, now) -> None:
        pa = PendingAsks(store, lambda: _settings(gids=(G1,)))
        assert pa.archive_expired("999999", now=T0) == 0

    @pytest.mark.asyncio
    async def test_recover_pending_asks_archives_expired(self, store: Store, now) -> None:
        """归档接在已有后台巡检里（recover_pending_asks 挨着 mark_stale）。"""
        import test_pending_ask_recovery as rec
        from fakes import FakeHost, FakeModelsQueue

        g = rec._Graph(store, FakeHost([]), FakeModelsQueue(replies=[]))
        g.asks.record_pending(
            rec.GID,
            {"message_id": "ancient", "user_id": "u1", "user_name": "阿一",
             "text": "帮我整理资料", "timestamp": T0 - 20 * DAY},
            "jev_超时",
        )
        g.asks.mark_stale(rec.GID, now=T0)
        assert rec._row(store, "ancient")["state"] == "review"
        await g.profiles.recover_pending_asks(rec.GID)
        assert rec._row(store, "ancient")["state"] == "expired"


# ----------------------------------------------------------------------
# 2. 汇总函数
# ----------------------------------------------------------------------


class TestAsksReviewSummary:
    def test_counts_oldest_and_items(self, store: Store, now) -> None:
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        _review(pa, G1, "m-old", days_ago=17, name="阿柒", text="帮我整理资料" * 20)
        _review(pa, G1, "m-mid", days_ago=5, name="阿八", text="查一下铝价")
        _review(pa, G2, "m-new", days_ago=3, name="阿九", text="提醒我交周报")
        out = views.asks_review_summary(_Svc(store, settings, pa))
        assert out["total"] == 3
        assert out["group_count"] == 2
        assert out["oldest_wait_s"] == pytest.approx(17 * DAY)
        assert out["oldest_wait_text"] == "17 天"
        assert [i["message_id"] for i in out["items"]] == ["m-old", "m-mid", "m-new"]
        first = out["items"][0]
        assert first["group_id"] == G1
        assert first["user_name"] == "阿柒"
        assert first["wait_text"] == "17 天"
        assert first["text"].startswith("帮我整理资料")
        assert len(first["text"]) <= views.ASKS_REVIEW_TEXT_MAX
        assert first["text"].endswith("…")
        assert first["why"] == "超过 48 小时仍未办成"

    def test_only_served_groups_and_optional_single_group(self, store: Store, now) -> None:
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        _review(pa, G1, "a", days_ago=3)
        _review(pa, G2, "b", days_ago=4)
        one = views.asks_review_summary(_Svc(store, settings, pa), only_group_id=G2)
        assert one["total"] == 1 and one["items"][0]["group_id"] == G2
        assert one["group_counts"] == {G2: 1}
        # 只剩 G2 服务时，G1 的行不算（不跨出服务群名单）
        only_g2 = _settings(gids=(G2,))
        pa2 = PendingAsks(store, lambda: only_g2)
        out = views.asks_review_summary(_Svc(store, only_g2, pa2))
        assert out["total"] == 1 and out["items"][0]["group_id"] == G2
        assert out["group_counts"] == {G2: 1}

    def test_empty_and_missing_module(self, store: Store, now) -> None:
        settings = _settings()
        out = views.asks_review_summary(_Svc(store, settings, PendingAsks(store, lambda: settings)))
        assert out == {"total": 0, "shown": 0, "group_count": 0, "oldest_wait_s": 0.0,
                       "oldest_wait_text": "", "items": [], "group_counts": {}}
        out2 = views.asks_review_summary(_Svc(store, settings, None))
        assert out2["total"] == 0 and out2["items"] == [] and out2["group_counts"] == {}

    def test_group_counts_is_true_per_group_count_over_the_cap(self, store: Store, now) -> None:
        """同群 101 条：items 只列最老 100 条，前端照 items 数会显示「100 条」。

        `group_counts` 给每群真值，前端不用再自己数 items（2026-10-15 发版前修复）。
        """
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        for i in range(101):
            _review(pa, G1, f"m{i:03d}", days_ago=3 + i / 1000.0, text=f"第 {i} 条")
        for i in range(3):
            _review(pa, G2, f"n{i:03d}", days_ago=2.5 + i / 1000.0, text=f"群二第 {i} 条")
        out = views.asks_review_summary(_Svc(store, settings, pa))
        assert out["total"] == 104
        assert out["shown"] == 103 and len(out["items"]) == 103  # G1 只列 100 条
        assert out["group_counts"] == {G1: 101, G2: 3}
        # 旧算法（照 items 数）确实会少 1 条 —— 这就是要修的那个显示 bug
        naive: dict[str, int] = {}
        for it in out["items"]:
            naive[it["group_id"]] = naive.get(it["group_id"], 0) + 1
        assert naive == {G1: 100, G2: 3}
        assert out["group_counts"][G1] == 101 > naive[G1]

    def test_group_counts_comes_from_review_count_not_the_list(self, store: Store, now) -> None:
        """计数走 `review_count`：列表只给 100 条，计数也必须是 250（精简假对象）。"""
        settings = _settings()
        rows = {G1: [_fake_row(f"m{i:03d}", ts=T0 - (3 + i / 1000.0) * DAY) for i in range(100)]}
        pa = _FakeAsks({G1: 250, G2: 0}, rows)
        out = views.asks_review_summary(_Svc(store, settings, pa))
        assert out["group_counts"] == {G1: 250}
        assert out["total"] == 250
        assert out["shown"] == 100 and len(out["items"]) == 100
        # 没有待复核的群不进 group_counts（省流量；前端按「缺省」回落）
        assert G2 not in out["group_counts"]

    def test_group_counts_skips_unserved_groups(self, store: Store, now) -> None:
        """非服务群零读取：计数再大也不进 group_counts。"""
        settings = _settings(gids=(G1,))
        rows = {G1: [_fake_row("a", ts=T0 - 3 * DAY)], G2: [_fake_row("b", ts=T0 - 3 * DAY)]}
        out = views.asks_review_summary(_Svc(store, settings, _FakeAsks({G1: 2, G2: 9}, rows)))
        assert out["group_counts"] == {G1: 2}
        assert out["total"] == 2 and [i["group_id"] for i in out["items"]] == [G1]

    def test_total_follows_the_same_fallback_as_group_counts(self, store: Store, now) -> None:
        """计数读成 0 却有明细（替身 / 旧模块）时，total 也必须跟着 max 兜底。

        不然会出现 total=0 / shown=1 的自相矛盾：前端 `!a.total` 会把这一块整个藏起来，
        真正躺在待复核里的那条就没人看见了。
        """
        settings = _settings(gids=(G1,))
        rows = {G1: [_fake_row("a", ts=T0 - 3 * DAY)]}
        out = views.asks_review_summary(_Svc(store, settings, _FakeAsks({G1: 0}, rows)))
        assert out["group_counts"] == {G1: 1}
        assert out["total"] == 1
        assert out["shown"] == 1 and out["group_count"] == 1
        assert out["oldest_wait_s"] == pytest.approx(3 * DAY)

    def test_total_is_true_count_even_over_the_list_cap(self, store: Store, now) -> None:
        """列表每群最多列最老 100 条，但数量必须是真的（管理员看的是「还有多少条」）。"""
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        for i in range(105):
            _review(pa, G1, f"m{i:03d}", days_ago=3 + i / 1000.0, text=f"第 {i} 条")
        out = views.asks_review_summary(_Svc(store, settings, pa))
        assert out["total"] == 105
        assert out["shown"] == 100 and len(out["items"]) == 100
        assert out["items"][0]["message_id"] == "m104"  # 最老的先
        assert out["group_counts"] == {G1: 105}  # 群计数同样是真值，不受 100 条上限影响
        assert views.group_summary(_Svc(store, settings, pa), G1, admin=True)["today"]["asks_review"] == 105

    def test_does_not_leak_error_text(self, store: Store, now) -> None:
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        _review(pa, G1, "m1", days_ago=3)
        with store.tx() as conn:
            conn.execute(
                "UPDATE pending_asks SET last_error=?, attempts=2 WHERE group_id=? AND message_id=?",
                ("private-error-credential-value", G1, "m1"),
            )
        out = views.asks_review_summary(_Svc(store, settings, pa))
        assert "private-error-credential-value" not in json.dumps(out, ensure_ascii=False)
        assert out["items"][0]["why"] == "处理失败，重试后仍未办成"
        assert out["items"][0]["attempts"] == 2


# ----------------------------------------------------------------------
# 3. 网页接口
# ----------------------------------------------------------------------


def _seed_review(svc, gid: str, mid: str, *, days_ago: float = 17) -> PendingAsks:
    pa = PendingAsks(svc.store, svc.get_settings)
    svc.profiles.pending_asks = pa
    pa.record_pending(
        gid,
        {"message_id": mid, "user_id": "u1", "user_name": "某成员", "text": "帮我整理资料",
         "timestamp": clock.now() - days_ago * DAY},
        "jev_超时",
    )
    pa.mark_stale(gid, now=clock.now())
    return pa


class TestAsksReviewApi:
    @pytest.mark.asyncio
    async def test_global_summary_is_admin_only(self, env) -> None:
        _seed_review(env.app, G1, "old")
        assert (await env.client.get("/api/asks/review")).status == 401
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        assert (await env.client.get("/api/asks/review")).status == 403
        token = env.app.token_of(G1)
        assert (await env.client.get("/api/asks/review", headers={"X-MW-Group": token})).status == 403
        await env.login(ADMIN_PW)
        r = await env.client.get("/api/asks/review")
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["total"] == 1
        assert data["oldest_wait_s"] > 16 * DAY
        item = data["items"][0]
        assert item["group_id"] == G1 and item["message_id"] == "old"
        assert item["user_name"] == "某成员" and item["text"] == "帮我整理资料"
        assert item["wait_text"] == "17 天"
        assert "last_error" not in json.dumps(data, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_group_route_permissions_and_shape(self, env) -> None:
        _seed_review(env.app, G1, "old")
        url = f"/api/groups/{G1}/pending-asks"
        assert (await env.client.get(url)).status == 401
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.get(url)
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["total"] == 1 and data["items"][0]["message_id"] == "old"
        assert data["group_counts"] == {G1: 1}  # 单群版也带群计数，形状和全局版一致
        assert (await env.client.get(f"/api/groups/{G2}/pending-asks")).status == 403
        await env.login(ADMIN_PW)
        assert (await env.client.get(f"/api/groups/{G2}/pending-asks")).status == 200
        assert (await env.client.get("/api/groups/999999/pending-asks")).status == 404

    @pytest.mark.asyncio
    async def test_group_route_accepts_link_token(self, env) -> None:
        _seed_review(env.app, G1, "old")
        await env.login(ADMIN_PW)
        token = env.app.token_of(G1)
        r = await env.client.get(f"/api/groups/{token}/pending-asks")
        assert r.status == 200, await r.text()
        assert (await r.json())["items"][0]["message_id"] == "old"


# ----------------------------------------------------------------------
# 4. 首页/导航汇总计数
# ----------------------------------------------------------------------


class TestTodayAsksReviewCount:
    def test_group_summary_carries_asks_review(self, store: Store, now) -> None:
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        _review(pa, G1, "m1", days_ago=3)
        svc = _Svc(store, settings, pa)
        assert views.group_summary(svc, G1, admin=True)["today"]["asks_review"] == 1
        assert views.group_summary(svc, G2, admin=True)["today"]["asks_review"] == 0
        lst = {g["id"]: g for g in views.list_summaries(svc, admin=True)}
        assert lst[G1]["today"]["asks_review"] == 1
        assert lst[G2]["today"]["asks_review"] == 0
        # 待复核和待批是两码事：不并进 today.pending
        assert lst[G1]["today"]["pending"] == 0

    def test_expired_rows_stop_counting(self, store: Store, now) -> None:
        settings = _settings()
        pa = PendingAsks(store, lambda: settings)
        _review(pa, G1, "old", days_ago=20)
        assert views.group_summary(_Svc(store, settings, pa), G1, admin=True)["today"]["asks_review"] == 1
        pa.archive_expired(G1, now=T0)
        assert views.group_summary(_Svc(store, settings, pa), G1, admin=True)["today"]["asks_review"] == 0

    def test_missing_module_is_zero(self, store: Store, now) -> None:
        settings = _settings()
        svc = _Svc(store, settings, None)
        assert views.group_summary(svc, G1, admin=True)["today"]["asks_review"] == 0
        assert views.list_summaries(svc, admin=True)[0]["today"]["asks_review"] == 0
