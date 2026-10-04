"""0.8.0 最后一轮边界：推送排期 / 发件新鲜度 / 非服务群零读取。

锁死这几条（父会话复审确认仍然存在的真 bug）：

1. `Outbox._postpone` 用的是**本群**那份睡觉时段（group_push 真源），不是全局
   `delivery.quiet_hours`：某个群早上还在 quiet 里，就推到**这个群**醒来钟点，
   不误推到默认 8 点（那会变成明天 8 点）。
2. 拿不到每群那份设置时只延 5 分钟再试，不误停到明天；这一轮零宿主发送。
3. 非服务群：`group_push.get_config` 给保守默认（三个自动开关全关、零库读取）；
   `Topics.check` 第一道 served 闸拦在**任何 SQL / 画像 / Jev / 模型之前**（连设置证据
   都缺也拒）；`Pushes.can_push` 非服务群直接不可推、零 SQL。
4. 自动消息 payload 带 `expires_ts`：到点还没发出去的那条在发件箱里作废
   （零宿主发送 + dropped 结果 hook），绝不把陈旧内容发进群。
5. 开场白排队 / 重试期间本群又有人说话 → 作废（不再冷场就不开口）；没有新消息照发。
   入队时没留冷场快照 → 失败关闭，作废。

全部用真 Store / 真 Outbox / 真 Topics / 假 Host，不联网、不调真模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from CharTyr_MaiWork.maiwork import group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import UNSERVED_REASON, Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"
SID = "sess-g1"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    """北京时间 2026-10-{day} 某时刻的 epoch。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)


def _settings(*, serve=(GID,), cfg: dict | None = None):
    merged: dict[str, Any] = {
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
        "environments": {"workspace_root": "data/workspaces"},
    }
    if cfg:
        merged.update(cfg)
    settings, problems = load_settings(merged)
    assert not problems, problems
    return settings


class Host:
    """假的宿主发送口：只记文本，也给 Topics.check 一个空的 messages 口。"""

    def __init__(self) -> None:
        self.texts: list[dict] = []

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({"session_id": session_id, "text": text})
        return SimpleNamespace(sent=True, message_id="m1")

    async def messages(self, session_id, start, end, limit, *, limit_mode="latest"):
        return []


class Jev:
    """预置「可以开」回答的 Jev 假对象。"""

    def __init__(self, *, ok: float = 0.9, reason: str = "fine", fit: float = 0.8,
                 available: bool = True) -> None:
        self._ok, self._reason, self._fit = ok, reason, fit
        self._available = available
        self.calls: list[tuple] = []

    def available(self) -> bool:
        return self._available

    async def ask(self, state, questions, *, purpose, group_id, timeout_ms=None):
        self.calls.append((state, questions, purpose, group_id))
        if not self._available:
            return None
        out: dict[str, Any] = {}
        for key, q in questions.items():
            if not isinstance(q, dict):
                continue
            if q.get("type") == "noul":
                out[key] = float(self._fit) if key.startswith("fit_") else float(self._ok)
            elif q.get("type") == "choice":
                out[key] = (self._reason, 0.9, 0.9)
        return out


class Profiles:
    def __init__(self, gap: Optional[float] = 300.0) -> None:
        self._gap = gap
        self.calls: list[tuple] = []

    def usual_gap(self, gid: str, now: float) -> Optional[float]:
        self.calls.append((gid, now))
        return self._gap

    def entries(self, gid: str) -> list:
        return []


class Models:
    """假主模型：ready 可控，chat 回一句干净开场白。"""

    def __init__(self, *, ready: bool = True, opener: str = "话说那个新板子你们看了没？") -> None:
        self._ready = ready
        self._opener = opener
        self.calls: list[dict] = []

    def settings(self):
        ready = self._ready

        class _S:
            def ready(self_inner) -> bool:
                return ready

        return _S()

    async def chat(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=self._opener)


class Signals:
    """假的 intake.Signals：手动 mark；last_ts 一直可读（不受 take 影响）。"""

    def __init__(self) -> None:
        self._map: dict[str, float] = {}

    def mark(self, group_id: str, ts: float) -> None:
        self._map[str(group_id)] = float(ts)

    def last_ts(self, group_id: str) -> float:
        return float(self._map.get(str(group_id), 0.0))

    def session_id(self, group_id: str) -> str:
        return SID


class CountingStore:
    """包一层真 Store：数「读了几次 / 开了几个事务 / 读了哪些 kv」。"""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.kv_get_calls: list[str] = []
        self.read_calls = 0
        self.tx_count = 0

    def kv_get(self, key: str, default: Any = None) -> Any:
        self.kv_get_calls.append(str(key))
        return self._inner.kv_get(key, default)

    def read(self):
        self.read_calls += 1
        return self._inner.read()

    def tx(self):
        self.tx_count += 1
        return self._inner.tx()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _rows(store: Store):
    return store.read().execute(
        "SELECT id, key, kind, status, payload, error, not_before FROM outbox ORDER BY id"
    ).fetchall()


def _seed_group(store: Store, gid: str, *, last_msg_ts: float = 0.0) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, session_id, last_msg_ts) VALUES (?, ?, ?)",
            (str(gid), f"sess-{gid}", float(last_msg_ts)),
        )


def _make_outbox(tmp_path, *, serve=(GID,), settings=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = settings or _settings(serve=serve)
    host = Host()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    for g in set(serve) | {GID, OTHER}:
        _seed_group(store, g)
    return store, settings, host, pushes, mentions, ob


def _make_topics(tmp_path, *, signals=None, jev=None, profiles=None, models=None, host=None,
                 serve=(GID,)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(serve=serve)
    signals = signals if signals is not None else Signals()
    host = host if host is not None else Host()
    jev = jev if jev is not None else Jev()
    profiles = profiles if profiles is not None else Profiles()
    models = models if models is not None else Models()
    mentions = Mentions(store, lambda: settings)
    pushes = Pushes(store, lambda: settings)
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    with store.tx() as conn:
        for g in set(serve) | {GID, OTHER}:
            conn.execute(
                "INSERT INTO groups (group_id, session_id, last_msg_ts) VALUES (?, ?, ?)",
                (g, f"sess-{g}", NOON - 1800.0),
            )
    topics = Topics(store, host, models, jev, profiles, mentions, pushes,
                    lambda: settings, signals, outbox=ob)
    return SimpleNamespace(store=store, settings=settings, host=host, pushes=pushes,
                           mentions=mentions, outbox=ob, topics=topics, signals=signals,
                           jev=jev, profiles=profiles, models=models)


def _seed_candidate(store: Store, *, now: float, ref_id: int = 7) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link, expires_ts, used_ts, created)"
            " VALUES (?, 'news', ?, '一条候选', '看点', 'http://x', ?, NULL, ?)",
            (GID, int(ref_id), float(now) + 12 * 3600.0, float(now) - 60.0),
        )
        return int(cur.lastrowid or 0)


def _enable_topics(store: Store, settings, *, now: float = NOON, **patch: Any) -> dict:
    body = {"topics_enabled": True, "quiet_hours": "00:00-00:00"}
    body.update(patch)
    return group_push.set_config(store, GID, body, settings, now=now)


# ======================================================================
# 1. 推迟用本群那份睡觉时段
# ======================================================================


async def test_postpone_uses_this_group_quiet_hours_not_global(tmp_path):
    """本群 quiet 到 14:30、全局那份是 23:00-08:00：早上推迟 → 本群 14:30，不是明天 8 点。"""
    store, settings, host, _, _, ob = _make_outbox(
        tmp_path, settings=_settings(cfg={"delivery": {"quiet_hours": "23:00-08:00"}})
    )
    group_push.set_config(store, GID, {"quiet_hours": "22:00-14:30"}, settings, now=NOON)
    morning = _ts(9, 0)
    ob.enqueue("k1", GID, "text", {"text": "随便聊聊", "push_kind": "topic"})
    await ob.flush(morning)
    row = _rows(store)[0]
    assert row["status"] == "pending"
    assert not host.texts
    assert row["not_before"] == _ts(14, 30), row["not_before"]
    # 到点就发得出去（不是卡到明天）
    await ob.flush(_ts(14, 31))
    assert _rows(store)[0]["status"] == "sent"
    assert len(host.texts) == 1


async def test_postpone_does_not_wait_until_tomorrow_when_read_fails(tmp_path):
    """读不到设置 → 只延 5 分钟再试（不按默认钟点误停到明天），这一轮零宿主发送。"""
    store, _, host, _, _, _ob = _make_outbox(tmp_path)

    def _boom():
        raise RuntimeError("读配置炸了")

    ob2 = Outbox(store, host, Pushes(store, _boom), Mentions(store, lambda: _settings()),
                 _boom)
    now = _ts(3, 0)  # 默认 23:00-08:00 的睡觉时段里
    ob2.enqueue("k1", GID, "text", {"text": "陈旧推送", "push_kind": "topic"})
    await ob2.flush(now, allowed_groups={GID})
    row = _rows(store)[0]
    assert row["status"] == "pending"
    assert "读不到设置" in row["error"]
    assert now < row["not_before"] <= now + 600.0, row["not_before"]
    assert not host.texts


# ======================================================================
# 2. 非服务群：零 SQL / 零 Jev / 零模型
# ======================================================================


async def test_unserved_topic_check_zero_sql_zero_model(tmp_path):
    """上游误传非服务群：Topics.check 第一道闸就拒，任何库 / 画像 / Jev / 模型都不碰。"""
    settings = _settings(serve=(GID,))
    inner = Store(tmp_path / "t.db")
    inner.migrate()
    counting = CountingStore(inner)
    jev, profiles, models, host = Jev(), Profiles(), Models(), Host()
    topics = Topics(counting, host, models, jev, profiles, None, None,
                    lambda: settings, Signals())
    out = await topics.check(OTHER, NOON)
    assert out == "skip:unserved", out
    assert counting.read_calls == 0 and counting.kv_get_calls == [] and counting.tx_count == 0
    assert jev.calls == [] and profiles.calls == [] and models.calls == []
    assert host.texts == [] and not getattr(host, "msg_calls", [])


async def test_topic_check_without_served_evidence_is_rejected(tmp_path):
    """连「这个群在不在服务名单」都认不出 → 也拒（失败关闭），零 SQL。"""

    class Bare:
        pass

    inner = Store(tmp_path / "t.db")
    inner.migrate()
    counting = CountingStore(inner)
    topics = Topics(counting, Host(), Models(), Jev(), Profiles(), None, None,
                    lambda: Bare(), Signals())
    assert (await topics.check(GID, NOON)) == "skip:unserved"
    assert counting.read_calls == 0 and counting.tx_count == 0


async def test_unserved_group_config_is_conservative_off(tmp_path):
    """非服务群那份设置：三个自动开关都关（不是 legacy「话题默认开」），零读写。"""
    inner = Store(tmp_path / "t.db")
    inner.migrate()
    counting = CountingStore(inner)
    cfg = group_push.get_config(counting, OTHER, _settings(serve=(GID,)))
    assert cfg["topics_enabled"] is False
    assert cfg["news_card_enabled"] is False and cfg["idea_mention_enabled"] is False
    assert counting.kv_get_calls == [] and counting.tx_count == 0


async def test_pushes_unserved_cannot_push_without_sql(tmp_path):
    """Pushes 对非服务群直接不可推：连额度 / 睡觉时段都不查（零 SQL）。"""
    inner = Store(tmp_path / "t.db")
    inner.migrate()
    counting = CountingStore(inner)
    pushes = Pushes(counting, lambda: _settings(serve=(GID,)))
    ok, why = pushes.can_push(OTHER, "topic", NOON)
    assert ok is False and why == UNSERVED_REASON, (ok, why)
    ok2, why2 = pushes.can_push(OTHER, "delivery", NOON)
    assert ok2 is False and why2 == UNSERVED_REASON
    assert counting.read_calls == 0 and counting.kv_get_calls == [] and counting.tx_count == 0
    # 故障回执照旧豁免（不受服务名单管）
    assert pushes.can_push(OTHER, "error", NOON) == (True, "")


# ======================================================================
# 3. TTL：自动消息过期 → 作废（零宿主发送 + dropped 结果 hook）
# ======================================================================


async def test_expired_auto_message_dropped_before_send(tmp_path):
    store, settings, host, pushes, mentions, ob = _make_outbox(tmp_path)
    results: list[dict] = []
    ob.add_result_hook(lambda info: results.append(info))
    ob.enqueue("topic:1", GID, "text",
               {"text": "陈旧开场白", "push_kind": "topic", "expires_ts": NOON - 1})
    ob.enqueue("topic:2", GID, "text",
               {"text": "还没过期", "push_kind": "topic", "expires_ts": NOON + 60})
    await ob.flush(NOON)
    rows = _rows(store)
    assert [r["status"] for r in rows] == ["dropped", "sent"]
    assert "超过有效期限" in str(rows[0]["error"])
    assert [t["text"] for t in host.texts] == ["还没过期"]
    dropped = [r for r in results if r["outcome"] == "dropped"]
    assert [r["key"] for r in dropped] == ["topic:1"]
    assert [r["outcome"] for r in results] == ["dropped", "sent"]


async def test_topic_opener_payload_carries_short_ttl_and_cold_snapshot(tmp_path):
    """开场白入队带保守 ≤30 分钟的期限 + 冷场快照，供发送前复核。"""
    signals = Signals()
    signals.mark(GID, NOON - 1800.0)
    world = _make_topics(tmp_path, signals=signals)
    _enable_topics(world.store, world.settings)
    _seed_candidate(world.store, now=NOON)
    out = await world.topics.check(GID, NOON)
    assert out.startswith("queued:topic_id="), out
    row = _rows(world.store)[0]
    payload = json.loads(row["payload"])
    assert payload["push_kind"] == "topic"
    assert NOON < float(payload["expires_ts"]) <= NOON + 30 * 60
    assert float(payload["cold_since_ts"]) == NOON - 1800.0


# ======================================================================
# 4. 开场白新鲜度：排队期间又有人说话 → 作废；没有 → 照发
# ======================================================================


async def test_queued_opener_dropped_when_group_talks_again(tmp_path):
    signals = Signals()
    signals.mark(GID, NOON - 1800.0)
    world = _make_topics(tmp_path, signals=signals)
    _enable_topics(world.store, world.settings)
    _seed_candidate(world.store, now=NOON)
    assert (await world.topics.check(GID, NOON)).startswith("queued:")
    # 排队期间群里又有人说话（信号 + groups 两个来源都推进）
    signals.mark(GID, NOON + 30.0)
    with world.store.tx() as conn:
        conn.execute("UPDATE groups SET last_msg_ts=? WHERE group_id=?", (NOON + 30.0, GID))
    await world.outbox.flush(NOON + 60.0)
    row = _rows(world.store)[0]
    assert row["status"] == "dropped", row["status"]
    assert "新消息" in str(row["error"])
    assert world.host.texts == []
    log = world.store.read().execute("SELECT opener, result FROM topic_log").fetchone()
    assert log["opener"] == ""
    assert "dropped" in str(log["result"])


async def test_queued_opener_sent_when_group_still_quiet(tmp_path):
    signals = Signals()
    signals.mark(GID, NOON - 1800.0)
    world = _make_topics(tmp_path, signals=signals)
    _enable_topics(world.store, world.settings)
    _seed_candidate(world.store, now=NOON)
    assert (await world.topics.check(GID, NOON)).startswith("queued:")
    await world.outbox.flush(NOON + 60.0)
    row = _rows(world.store)[0]
    assert row["status"] == "sent", row["status"]
    assert len(world.host.texts) == 1
    log = world.store.read().execute("SELECT opener FROM topic_log").fetchone()
    assert log["opener"] == "话说那个新板子你们看了没？"


async def test_opener_without_cold_snapshot_fails_closed(tmp_path):
    """入队时没留冷场快照（拿不到安全证据）→ 失败关闭，作废不发。"""
    signals = Signals()
    signals.mark(GID, NOON - 1800.0)
    world = _make_topics(tmp_path, signals=signals)
    _enable_topics(world.store, world.settings)
    # 手工塞一条没有 cold_since_ts 的 topic 行
    world.outbox.enqueue("topic:9999", GID, "text", {"text": "没快照的开场白", "push_kind": "topic"})
    await world.outbox.flush(NOON)
    row = _rows(world.store)[0]
    assert row["status"] == "dropped"
    assert world.host.texts == []


# ======================================================================
# 5. 退役字段：所有调用口都拒（legacy settings=None 同样拒）
# ======================================================================


async def test_retired_fields_rejected_even_on_legacy_none_path(tmp_path):
    store = Store(tmp_path / "t.db")
    store.migrate()
    for field in sorted(group_push.RETIRED_FIELDS):
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {field: 1})          # 老调用口（settings=None）
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {field: 1}, _settings())
    # 合法字段仍然走得通（只是不能假保存退役字段）
    cfg = group_push.set_config(store, GID, {"daily_max": 2})
    assert cfg["daily_max"] == 2
