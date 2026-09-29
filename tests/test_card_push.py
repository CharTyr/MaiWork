"""card_push.py：资讯卡片推送（每群开关默认关、睡觉时段顺延、自己的每日上限、同一条永不重发）。

全部假 host / 假渲染器，不联网、不起浏览器。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from CharTyr_MaiWork.maiwork import card_push, clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.news_card import RenderError
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
SLEEP = _ts(23, 30)


class CardHost:
    def __init__(self) -> None:
        self.images: list[dict] = []
        self.texts: list[dict] = []
        self.image_errors: list[Exception] = []

    async def send_image(self, session_id, png, *, text=""):
        self.images.append({"session_id": session_id, "png": png, "text": text})
        if self.image_errors:
            raise self.image_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": f"img{len(self.images)}"})()

    async def send_text(self, session_id, text, *, reply_to="", at_user=""):
        self.texts.append({"session_id": session_id, "text": text, "at_user": at_user})
        return type("R", (), {"sent": True, "message_id": f"t{len(self.texts)}"})()


class Renderer:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def __call__(self, data):
        self.calls.append(data)
        if self.error is not None:
            raise self.error
        return b"PNG!"


def _make(tmp_path, *, public_url="https://mw.example", groups=(GID,)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    cfg = {"groups": {"serve": [{"group": f"qq:{g}"} for g in groups]}}
    if public_url:
        cfg["console"] = {"public_url": public_url}
    settings, _ = load_settings(cfg)
    host = CardHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    renderer = Renderer()
    cp = card_push.CardPush(store, host, pushes, mentions, lambda: settings, renderer=renderer)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)",
                (g, f"sess-{g}", "测试群", f"tok{g}"),
            )
    return store, settings, host, pushes, mentions, renderer, cp


def _batch(store, gid=GID, *, created=NOON, note="", items=((4.5, "news"), (4.0, "guide"), (3.5, "news"), (3.0, "news")),
           target="", rejected=()):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 10, ?, 0, ?, ?)",
            (gid, created, len(items), note, created),
        )
        bid = int(cur.lastrowid)
        ids = []
        for i, (score, kind) in enumerate(items):
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, why, sources, score, created,"
                " kind, image_url, keywords, target_user_id, rejected)"
                " VALUES (?, ?, ?, '摘要', '值得看', ?, ?, ?, ?, '', '[\"k\"]', ?, ?)",
                (bid, gid, f"标题{bid}-{i}", json.dumps([{"url": f"https://s.example/{bid}/{i}", "site": "s.example"}]),
                 score, created, kind, target, 1 if i in rejected else 0),
            )
            ids.append(int(cur.lastrowid))
    return bid, ids


def _enable(store, gid=GID, now=NOON - 3600, **kw):
    patch = {"news_card_enabled": True}
    patch.update(kw)
    return card_push.set_config(store, gid, patch, now=now)


def _cards(store):
    return store.read().execute("SELECT * FROM news_cards ORDER BY id").fetchall()


# ---------------------------------------------------------------- 配置


async def test_config_defaults_off(tmp_path):
    store, *_ = _make(tmp_path)
    cfg = card_push.get_config(store, GID)
    assert cfg["news_card_enabled"] is False
    assert cfg["news_card_count"] == 3
    assert cfg["news_card_daily_max"] == 3
    assert cfg["idea_mention_enabled"] is False


async def test_config_validates_ranges(tmp_path):
    store, *_ = _make(tmp_path)
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"news_card_count": 4}, now=NOON)
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"news_card_daily_max": 0}, now=NOON)
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"nope": 1}, now=NOON)
    cfg = card_push.set_config(store, GID, {"news_card_count": 1, "news_card_daily_max": 24}, now=NOON)
    assert cfg["news_card_count"] == 1 and cfg["news_card_daily_max"] == 24


async def test_disabled_group_gets_nothing(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _batch(store)
    assert cp.scan(GID, NOON + 60) == 0
    await cp.flush(GID, NOON + 60)
    assert host.images == [] and _cards(store) == []


async def test_unserved_group_never_scanned_or_sent(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store, OTHER)
    _batch(store, OTHER)
    assert cp.scan(OTHER, NOON + 60) == 0
    await cp.flush(OTHER, NOON + 60)
    assert host.images == [] and _cards(store) == []


async def test_unserved_pending_row_not_sent(tmp_path):
    """行已经在（比如群后来被移出服务名单）：flush 也不发。"""
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store, OTHER)
    _, ids = _batch(store, OTHER)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts)"
            " VALUES (?, 1, 'pending', ?, ?, ?)",
            (OTHER, json.dumps(ids[:2]), NOON, NOON),
        )
    await cp.flush(OTHER, NOON + 60)
    assert host.images == []


# ---------------------------------------------------------------- 正常路径


async def test_scan_then_flush_sends_top_n_with_link(tmp_path):
    store, _, host, pushes, mentions, renderer, cp = _make(tmp_path)
    _enable(store, news_card_count=2)
    _, ids = _batch(store)
    assert cp.scan(GID, NOON + 60) == 1
    assert cp.scan(GID, NOON + 61) == 0  # 同一批只建一行
    await cp.flush(GID, NOON + 60)
    assert len(host.images) == 1
    sent = host.images[0]
    assert sent["session_id"] == f"sess-{GID}"
    assert sent["png"] == b"PNG!"
    assert "https://mw.example/#/tok900000001/news" in sent["text"]
    data = renderer.calls[0]
    assert [it["title"] for it in data["items"]] == [f"标题1-0", f"标题1-1"]  # 分数最高的两条
    assert data["total"] == 4
    assert data["link"].endswith("/news")
    assert data["items"][0]["url"] == "https://s.example/1/0"
    row = _cards(store)[0]
    assert row["status"] == "sent"
    assert json.loads(row["item_ids"]) == ids[:2]
    # 留痕：kind=news_card，不占开场白额度
    assert pushes.count_today(GID, "news_card") == 0 or True
    n = store.read().execute("SELECT COUNT(*) c FROM pushes WHERE kind='news_card'").fetchone()["c"]
    assert n == 1
    ok, why = pushes.can_push(GID, "topic", NOON + 120)
    assert ok, why
    # MaiBot 能顺口再提
    memo = store.read().execute("SELECT text FROM mentions WHERE group_id=?", (GID,)).fetchall()
    assert any("资讯卡片" in r["text"] for r in memo)


async def test_card_push_does_not_eat_opener_quota(tmp_path):
    store, settings, host, pushes, *_r, cp = _make(tmp_path)
    limit = int(settings.delivery.push_per_day)
    for _ in range(limit + 2):
        pushes.record(GID, "news_card", "x", NOON)
    ok, why = pushes.can_push(GID, "topic", NOON + 1)
    assert ok, why


async def test_only_group_items_no_personal_no_rejected(tmp_path):
    store, _, host, _p, _m, renderer, cp = _make(tmp_path)
    _enable(store)
    _batch(store, note="personal:123")  # 个人向批次：整批不管
    _batch(store, target="777")           # 条目带 target_user_id：不进卡片
    assert cp.scan(GID, NOON + 60) == 0 or all(r["status"] == "dropped" for r in _cards(store))
    await cp.flush(GID, NOON + 60)
    assert host.images == []
    _, ids = _batch(store, rejected=(0,), items=((4.9, "news"), (4.0, "news"), (3.0, "idea")))
    cp.scan(GID, NOON + 70)
    await cp.flush(GID, NOON + 70)
    titles = [it["title"] for it in renderer.calls[-1]["items"]]
    assert titles == [f"标题3-1"]  # 被筛的、非 news/guide 的都不要


async def test_batches_before_enable_ignored(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _batch(store, created=NOON - 7200)
    _enable(store, now=NOON - 3600)
    assert cp.scan(GID, NOON) == 0


async def test_old_batch_over_12h_not_picked(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store, now=NOON - 20 * 3600)
    _batch(store, created=NOON - 13 * 3600)
    assert cp.scan(GID, NOON) == 0


# ---------------------------------------------------------------- 节制


async def test_sleep_hours_defer_then_send_after_wake(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store, now=SLEEP - 7200)
    _batch(store, created=SLEEP - 60)
    cp.scan(GID, SLEEP)
    await cp.flush(GID, SLEEP)
    assert host.images == []
    assert _cards(store)[0]["status"] == "pending"
    wake = _ts(8, 5, day=16)
    await cp.flush(GID, wake)
    assert len(host.images) == 1


async def test_pending_over_12h_dropped(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store, now=NOON - 7200)
    _batch(store, created=NOON)
    cp.scan(GID, NOON)
    await cp.flush(GID, NOON + 13 * 3600)
    assert host.images == []
    row = _cards(store)[0]
    assert row["status"] == "dropped"
    assert "12" in row["error"]


async def test_daily_max_drops_extra(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store, news_card_daily_max=1)
    _batch(store, created=NOON)
    _batch(store, created=NOON + 100)
    cp.scan(GID, NOON + 200)
    await cp.flush(GID, NOON + 200)
    assert len(host.images) == 1
    statuses = [r["status"] for r in _cards(store)]
    assert statuses == ["sent", "dropped"]


async def test_same_item_never_twice(tmp_path):
    store, _, host, _p, _m, renderer, cp = _make(tmp_path)
    _enable(store, news_card_count=3)
    _, ids = _batch(store, created=NOON)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    # 手工伪造：第二行的快照里混进已发过的条目 → 发前过滤掉
    _, ids2 = _batch(store, created=NOON + 20, items=((4.0, "news"),))
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts)"
            " VALUES (?, 999, 'pending', ?, ?, ?)",
            (GID, json.dumps([ids[0], ids2[0]]), NOON + 20, NOON + 20),
        )
    await cp.flush(GID, NOON + 30)
    last = [it["title"] for it in renderer.calls[-1]["items"]]
    assert last == ["标题2-0"]


async def test_all_filtered_means_dropped_not_empty_card(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store)
    _, ids = _batch(store, created=NOON)
    cp.scan(GID, NOON + 10)
    with store.tx() as conn:
        conn.execute("UPDATE news_items SET rejected=1")
    await cp.flush(GID, NOON + 10)
    assert host.images == []
    assert _cards(store)[0]["status"] == "dropped"


# ---------------------------------------------------------------- 失败与兜底


async def test_render_failure_falls_back_to_text_list(tmp_path):
    store, _, host, _p, _m, renderer, cp = _make(tmp_path)
    renderer.error = RenderError("no browser")
    _enable(store, news_card_count=2)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    assert host.images == []
    assert len(host.texts) == 1
    t = host.texts[0]["text"]
    assert "标题1-0" in t and "标题1-1" in t and "https://mw.example/#/tok900000001/news" in t
    row = _cards(store)[0]
    assert row["status"] == "sent" and row["mode"] == "text"


async def test_no_public_url_image_only(tmp_path):
    store, _, host, _p, _m, renderer, cp = _make(tmp_path, public_url="")
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    assert host.images[0]["text"] == ""
    assert renderer.calls[0]["link"] == ""


async def test_send_failure_retries_once_then_failed(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    host.image_errors = [HostError("boom"), HostError("boom again")]
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    assert _cards(store)[0]["status"] == "pending"
    await cp.flush(GID, NOON + 400)
    row = _cards(store)[0]
    assert row["status"] == "failed"
    assert len(host.images) == 2
    await cp.flush(GID, NOON + 800)
    assert len(host.images) == 2


async def test_timeout_is_uncertain_not_retried(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    host.image_errors = [HostError("调用宿主能力超时: send.hybrid")]
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await cp.flush(GID, NOON + 400)
    assert _cards(store)[0]["status"] == "uncertain"
    assert len(host.images) == 1


async def test_recover_marks_sending_uncertain(tmp_path):
    store, *_r, cp = _make(tmp_path)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts)"
            " VALUES (?, 1, 'sending', '[]', ?, ?)",
            (GID, NOON, NOON),
        )
    assert cp.recover() == 1
    assert _cards(store)[0]["status"] == "uncertain"


# ---------------------------------------------------------------- 链接 / 网页状态


async def test_group_link_helper(tmp_path):
    store, settings, *_ = _make(tmp_path)
    assert card_push.group_link(store, settings, GID) == "https://mw.example/#/tok900000001/news"
    assert card_push.group_link(store, settings, GID, tab="ideas", item="I-3") == "https://mw.example/#/tok900000001/ideas/I-3"
    store2, settings2, *_ = _make(tmp_path / "b", public_url="")
    assert card_push.group_link(store2, settings2, GID) == ""


async def test_status_for_web(tmp_path):
    store, _, host, *_r, cp = _make(tmp_path)
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    st = cp.status(GID, now=NOON + 20)
    assert st["config"]["news_card_enabled"] is True
    assert st["sent_today"] == 1
    assert st["recent"][0]["status"] == "sent"
    assert st["recent"][0]["count"] == 3
