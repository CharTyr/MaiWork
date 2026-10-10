"""发件箱延期与 TTL 不相容的本地修复（2026-10-06，未部署）。

线上实测的形状（#58 / #59）：

- 当天 `daily_max` 用满 → 19:00 那条排到**次日 00:00**；
- 次日 00:00 又落在本群睡觉时段（00:00-07:00）→ 再排到 07:00（**第二趟**）；
- 可那条资讯卡片的 TTL 是 06:59 —— 到 07:00 才「发现」它过期、才作废。

要锁死的三条（最小修法，不动额度 / 不动 TTL / 不改旧记录）：

1. 推迟时**一次**算出「下一次能发」的时刻：额度原因 = 次日 00:00，若那一刻仍落在
   本群 quiet 里就接着移到那段 quiet 结束（跨夜、同日区间都要对）；睡觉时段原因 =
   quiet 结束；读不到那份设置 / 认不出原因 → 只延 5 分钟再试，绝不猜次日钟点。
2. 这个时刻**严格晚于** `payload.expires_ts`（`now > expires` 同口径，取等号仍算有效）
   → 当场作废（dropped）并把结果交给生产者；没有有效期的（任务交付等）不受影响。
3. 拿不到可信设置时**不**用猜出来的次日时刻提前作废；5 分钟这一步只做有界检查
   （下一次 flush 最早也只在这之后，那时已经过期 → 现在作废不白等）。

另：延期 / 作废要留安全日志或 events（只说原因和目标时间，绝不写载荷 / 链接 / token / 正文）。

全部用真 Store / 真 Outbox / 假 Host，不联网、不调模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import UNREADABLE_REASON, Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import (
    _POSTPONE_RETRY_S,
    _TTL_EXPIRED_REASON,
    Outbox,
)
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    """北京时间 2026-10-{day} 某时刻的 epoch。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOW_19 = _ts(19, 0)               # 当天额度已经用完的那一刻（还醒着）
NEXT_MIDNIGHT = _ts(0, day=16)
QUIET_07 = _ts(7, day=16)
QUIET_08 = _ts(8, day=16)
FAR_FUTURE = _ts(23, day=16)      # 期限很远：不该因为延期被作废


class Host:
    """假的宿主发送口：只记文本。"""

    def __init__(self) -> None:
        self.texts: list[dict] = []

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name="") -> Any:
        self.texts.append({"session_id": session_id, "text": text})
        return SimpleNamespace(sent=True, message_id="m1")


def _world(tmp_path, *, cfg: dict | None = None, broken: bool = False):
    """真 Store + 真 Outbox + 假 Host；broken=True 时装一个读设置就炸的口。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    merged: dict[str, Any] = {
        "environments": {"workspace_root": str(tmp_path)},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
    }
    if cfg:
        merged.update(cfg)
    settings, problems = load_settings(merged)
    assert not problems, problems
    host = Host()

    def _boom():
        raise RuntimeError("读配置炸了")

    get_settings = _boom if broken else (lambda: settings)
    pushes = Pushes(store, get_settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, get_settings)
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "sess-1"))
    return SimpleNamespace(store=store, settings=settings, host=host, pushes=pushes,
                           mentions=mentions, ob=ob)


def _exhaust_quota(world, *, quiet: str = "00:00-07:00", now: float = NOW_19) -> None:
    """把当天额度用满（daily_max=1）+ 定好这个群的睡觉时段。"""
    group_push.set_config(world.store, GID, {"daily_max": 1, "quiet_hours": quiet},
                          world.settings, now=now)
    world.pushes.record(GID, "status", "当天已经推过一条", now)


def _rows(store: Store):
    return store.read().execute(
        "SELECT id, key, kind, status, payload, error, attempts, not_before FROM outbox ORDER BY id"
    ).fetchall()


def _events(store: Store, kind: str) -> list[dict]:
    rows = store.read().execute(
        "SELECT kind, group_id, entity, entity_id, payload FROM events WHERE kind=? ORDER BY id",
        (str(kind),),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "kind": str(r["kind"]),
            "group_id": str(r["group_id"]),
            "entity": str(r["entity"]),
            "entity_id": str(r["entity_id"]),
            "payload": json.loads(r["payload"] or "{}"),
        })
    return out


# ======================================================================
# 1. 额度原因：一次算到 quiet 结束（不再排第二趟）
# ======================================================================


@pytest.mark.parametrize("quiet,expected", [
    ("00:00-07:00", QUIET_07),
    ("23:00-08:00", QUIET_08),
    ("22:00-14:30", _ts(14, 30, day=16)),
    # 同日晚间静默（19 点还醒着，所以走的是额度原因）：次日 00:00 已经不在静默里
    ("20:00-23:30", NEXT_MIDNIGHT),
    # 起止相同 = 不限制（老口径）：停在次日 00:00
    ("00:00-00:00", NEXT_MIDNIGHT),
])
async def test_quota_target_is_next_midnight_then_group_quiet_end(tmp_path, quiet, expected):
    """额度用满 → 一次算到「次日 00:00；那一刻还在本群 quiet 里就移到 quiet 结束」。"""
    world = _world(tmp_path)
    _exhaust_quota(world, quiet=quiet)
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "额度用满的这条", "push_kind": "status", "expires_ts": FAR_FUTURE})
    await world.ob.flush(NOW_19)
    row = _rows(world.store)[0]
    assert row["status"] == "pending", row["error"]
    assert float(row["not_before"]) == expected, float(row["not_before"])
    assert float(row["not_before"]) > NOW_19
    assert not world.host.texts
    # 留痕：延期原因 + 目标时刻（不带载荷内容）
    notes = _events(world.store, "outbox.postpone")
    assert len(notes) == 1
    assert notes[0]["payload"]["reason"] == "今天推够了"
    assert notes[0]["payload"]["target_ts"] == expected
    assert notes[0]["entity"] == "outbox"


async def test_quota_target_is_actually_sendable_next_day(tmp_path):
    """一步算出来的时刻到点真能发（次日额度已恢复）。"""
    world = _world(tmp_path)
    _exhaust_quota(world)
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "额度用满的这条", "push_kind": "status", "expires_ts": FAR_FUTURE})
    await world.ob.flush(NOW_19)
    assert float(_rows(world.store)[0]["not_before"]) == QUIET_07
    # 静默结束前不发
    await world.ob.flush(QUIET_07 - 60)
    assert not world.host.texts
    await world.ob.flush(QUIET_07 + 60)
    assert _rows(world.store)[0]["status"] == "sent"
    assert [t["text"] for t in world.host.texts] == ["额度用满的这条"]


async def test_quota_plus_quiet_single_step_drops_past_expiry(tmp_path):
    """#58 正例：19 点额度用满、quiet 到 07:00，而 TTL 是 06:59 → 当场作废，不白等一晚。"""
    world = _world(tmp_path)
    seen: list[dict] = []
    world.ob.add_result_hook(seen.append)
    _exhaust_quota(world)
    world.ob.enqueue("news_card:58", GID, "text",
                     {"text": "秘密正文不该进日志", "push_kind": "status",
                      "expires_ts": _ts(6, 59, day=16),
                      "link": "https://example.com/secret", "token": "tok-1234567890"})
    await world.ob.flush(NOW_19)
    row = _rows(world.store)[0]
    assert row["status"] == "dropped"
    assert "有效期限" in row["error"]
    assert "2026-10-16 07:00" in row["error"]          # 原定推迟到哪也说清楚
    assert not world.host.texts
    # 生产者只收一次结果（dropped），不重复回写
    assert len(seen) == 1 and seen[0]["outcome"] == "dropped"
    assert seen[0]["key"] == "news_card:58"
    # 之后两轮 flush 都不再碰它、也不再触发 hook
    await world.ob.flush(_ts(6, 58, day=16))
    await world.ob.flush(_ts(7, 5, day=16))
    assert len(seen) == 1
    assert not world.host.texts
    # 作废留痕：原因 + 目标时刻 + 期限；**不带**载荷 / 链接 / token / 正文
    drops = _events(world.store, "outbox.ttl_drop")
    assert len(drops) == 1
    payload = drops[0]["payload"]
    assert payload["postpone_reason"] == "今天推够了"
    assert payload["target_ts"] == QUIET_07
    assert payload["expires_ts"] == _ts(6, 59, day=16)
    blob = json.dumps(drops, ensure_ascii=False)
    for leak in ("秘密正文不该进日志", "example.com", "tok-1234567890"):
        assert leak not in blob, leak


# ======================================================================
# 2. 睡觉时段：quiet 结束；跨夜 / 同日都要对，且同样盯 TTL
# ======================================================================


async def test_quiet_only_postpone_cross_midnight_window(tmp_path):
    """23:30 在 23:00-08:00 里 → 次日 08:00（跨夜区间）。"""
    world = _world(tmp_path)
    group_push.set_config(world.store, GID, {"quiet_hours": "23:00-08:00"}, world.settings,
                          now=NOW_19)
    world.ob.enqueue("topic:1", GID, "text",
                     {"text": "冷场开场白", "push_kind": "status", "expires_ts": FAR_FUTURE})
    await world.ob.flush(_ts(23, 30))
    row = _rows(world.store)[0]
    assert row["status"] == "pending"
    assert float(row["not_before"]) == QUIET_08
    await world.ob.flush(QUIET_08 + 60)
    assert _rows(world.store)[0]["status"] == "sent"


async def test_quiet_only_postpone_same_day_window(tmp_path):
    """14:00 在 13:00-15:00 里 → 当天 15:00（同日区间）。"""
    world = _world(tmp_path)
    group_push.set_config(world.store, GID, {"quiet_hours": "13:00-15:00"}, world.settings,
                          now=NOW_19)
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "白天也别出声", "push_kind": "status", "expires_ts": FAR_FUTURE})
    await world.ob.flush(_ts(14, 0))
    row = _rows(world.store)[0]
    assert row["status"] == "pending"
    assert float(row["not_before"]) == _ts(15, 0)
    await world.ob.flush(_ts(15, 1))
    assert _rows(world.store)[0]["status"] == "sent"


async def test_quiet_postpone_drops_when_quiet_end_past_expiry(tmp_path):
    """23:30 排队、quiet 到 08:00，可 TTL 只到次日 02:00 → 当场作废。"""
    world = _world(tmp_path)
    world.ob.enqueue("topic:2", GID, "text",
                     {"text": "过期前的开场白", "push_kind": "status",
                      "expires_ts": _ts(2, 0, day=16)})
    await world.ob.flush(_ts(23, 30))
    row = _rows(world.store)[0]
    assert row["status"] == "dropped"
    assert "有效期限" in row["error"]
    assert not world.host.texts


# ======================================================================
# 3. TTL 边界：now > expires 才算过期（取等号仍有效），没有有效期不误伤
# ======================================================================


async def test_expiry_boundary_equal_target_is_kept_and_sent(tmp_path):
    """目标时刻 == 期限 → 不算「严格晚于」：留着，到点还能发。"""
    world = _world(tmp_path)
    _exhaust_quota(world, quiet="00:00-00:00")
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "卡在边界上的一条", "push_kind": "status",
                      "expires_ts": NEXT_MIDNIGHT})
    await world.ob.flush(NOW_19)
    row = _rows(world.store)[0]
    assert row["status"] == "pending", row["error"]
    assert float(row["not_before"]) == NEXT_MIDNIGHT
    await world.ob.flush(NEXT_MIDNIGHT)
    assert _rows(world.store)[0]["status"] == "sent"
    assert len(world.host.texts) == 1


async def test_expiry_equal_now_is_not_expired(tmp_path):
    """发送前判定同口径：expires_ts == now 不算过期（now > expires 才算）。"""
    world = _world(tmp_path)
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "刚好到期的那一刻", "push_kind": "status",
                      "expires_ts": NOW_19})
    await world.ob.flush(NOW_19)
    assert _rows(world.store)[0]["status"] == "sent"


async def test_task_delivery_without_ttl_is_never_dropped_by_postpone(tmp_path):
    """没有有效期的任务交付：推迟照旧排队，绝不因为「推到下一段能发的时间」被作废。

    2026-10-10 起任务自己的消息（task_id 非空 + delivery/status）不吃每日额度了
    （新口径与用例见 tests/test_task_push_quota.py），所以这里改用**睡觉时段**造成推迟
    ——顺带证明改口径之后睡觉时段仍然照旧推迟、且没有有效期就绝不提前作废。
    """
    world = _world(tmp_path)
    seen: list[dict] = []
    world.ob.add_result_hook(seen.append)
    group_push.set_config(world.store, GID, {"quiet_hours": "19:00-20:00", "daily_max": 1},
                          world.settings, now=NOW_19 - 10)
    world.ob.enqueue("task:T-9:deliver:text", GID, "text",
                     {"text": "任务成品", "push_kind": "delivery"}, task_id="T-9")
    await world.ob.flush(NOW_19)
    row = _rows(world.store)[0]
    assert row["status"] == "pending"
    assert "推迟" in row["error"]
    assert float(row["not_before"]) == _ts(20, 0)   # 这段 quiet 结束的时刻
    assert seen == []                     # 没作废、没结果 hook
    await world.ob.flush(_ts(20, 1))
    assert _rows(world.store)[0]["status"] == "sent"
    assert [t["text"] for t in world.host.texts] == ["任务成品"]


# ======================================================================
# 4. 读不到设置：只延 5 分钟，不猜次日钟点；5 分钟这一步只做有界检查
# ======================================================================


async def test_unreadable_settings_defers_five_minutes_without_guessing(tmp_path):
    """读不到设置 → 延 5 分钟再试；没有有效期就一直这样等着，绝不提前作废。"""
    world = _world(tmp_path, broken=True)
    seen: list[dict] = []
    world.ob.add_result_hook(seen.append)
    now = _ts(3, 0)
    world.ob.enqueue("status:1", GID, "text", {"text": "读不到设置时的这条", "push_kind": "status"})
    await world.ob.flush(now, allowed_groups={GID})
    row = _rows(world.store)[0]
    assert row["status"] == "pending"
    assert UNREADABLE_REASON in row["error"]
    assert float(row["not_before"]) == now + _POSTPONE_RETRY_S
    assert not world.host.texts and seen == []
    await world.ob.flush(now + _POSTPONE_RETRY_S, allowed_groups={GID})
    row = _rows(world.store)[0]
    assert row["status"] == "pending"       # 还是不发，但也不作废
    assert float(row["not_before"]) == now + 2 * _POSTPONE_RETRY_S


async def test_unreadable_settings_bounded_check_drops_only_when_certain(tmp_path):
    """有界检查：期限在 5 分钟窗口之内（下次 flush 最早也已过期）→ 现在作废。"""
    world = _world(tmp_path, broken=True)
    seen: list[dict] = []
    world.ob.add_result_hook(seen.append)
    now = _ts(3, 0)
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "等不到下次 flush", "push_kind": "status",
                      "expires_ts": now + _POSTPONE_RETRY_S - 100})
    await world.ob.flush(now, allowed_groups={GID})
    row = _rows(world.store)[0]
    assert row["status"] == "dropped"
    assert _TTL_EXPIRED_REASON in row["error"]
    assert not world.host.texts
    assert len(seen) == 1 and seen[0]["outcome"] == "dropped"


async def test_unreadable_settings_does_not_drop_when_a_retry_could_still_send(tmp_path):
    """期限还在 5 分钟窗口之外 → 先延 5 分钟；到那一刻确认来不及了才作废。"""
    world = _world(tmp_path, broken=True)
    seen: list[dict] = []
    world.ob.add_result_hook(seen.append)
    now = _ts(3, 0)
    world.ob.enqueue("status:1", GID, "text",
                     {"text": "还能撑过一个窗口", "push_kind": "status",
                      "expires_ts": now + _POSTPONE_RETRY_S + 100})
    await world.ob.flush(now, allowed_groups={GID})
    row = _rows(world.store)[0]
    assert row["status"] == "pending"                      # 不猜、不提前作废
    assert float(row["not_before"]) == now + _POSTPONE_RETRY_S
    assert seen == []
    await world.ob.flush(now + _POSTPONE_RETRY_S, allowed_groups={GID})
    assert _rows(world.store)[0]["status"] == "dropped"
    assert len(seen) == 1 and seen[0]["outcome"] == "dropped"
