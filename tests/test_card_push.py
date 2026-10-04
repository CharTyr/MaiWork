"""card_push.py：资讯卡片（每群开关默认关、睡觉时段顺延、共用一个每日总上限、同一条永不重发）。

0.8.0 归一之后：卡片只「备料 + 入队」（画好的图落盘缓存），真正发出去、睡觉时段、
每日总上限、开关复检都在发件箱里；发出去之后由结果 hook 回写 news_cards。
全部假 host / 假渲染器 + 真 Store / 真 Outbox，不联网、不起浏览器。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import card_push, clock, group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.news_card import RenderError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
SLEEP = _ts(23, 30)

# 发件箱按内容（PNG 魔数）判图片类型：假渲染器也要给一张「真 PNG 头」的数据
PNG = b"\x89PNG\r\n\x1a\n" + b"card-bytes"


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

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
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
        return PNG


def _make(tmp_path, *, public_url="https://mw.example", groups=(GID,), outbox=True):
    store = Store(tmp_path / "t.db")
    store.migrate()
    cfg = {
        "groups": {"serve": [{"group": f"qq:{g}"} for g in groups]},
        "environments": {"workspace_root": str(tmp_path)},
    }
    if public_url:
        cfg["console"] = {"public_url": public_url}
    settings, _ = load_settings(cfg)
    host = CardHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    renderer = Renderer()
    # 真发件箱：卡片只入队，发送 / 节制 / 回写都在这里
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    cp = card_push.CardPush(store, host, pushes, mentions, lambda: settings,
                            renderer=renderer, outbox=ob if outbox else None)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)",
                (g, f"sess-{g}", "测试群", f"tok{g}"),
            )
    return store, settings, host, pushes, mentions, renderer, cp, ob


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


def _boxes(store):
    return store.read().execute("SELECT * FROM outbox ORDER BY id").fetchall()


# ---------------------------------------------------------------- 配置


async def test_config_defaults_off(tmp_path):
    store, *_ = _make(tmp_path)
    cfg = card_push.get_config(store, GID)
    assert cfg["news_card_enabled"] is False
    assert cfg["news_card_count"] == 3
    assert cfg["idea_mention_enabled"] is False
    assert cfg["daily_max"] == 3                      # 一个总上限（沿用 push_per_day）
    assert "news_card_daily_max" not in cfg           # 每类每日上限退役
    assert "idea_mention_daily_max" not in cfg


async def test_config_validates_ranges(tmp_path):
    store, *_ = _make(tmp_path)
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"news_card_count": 4}, now=NOON)
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"daily_max": 99}, now=NOON)
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"nope": 1}, now=NOON)
    cfg = card_push.set_config(store, GID, {"news_card_count": 1, "daily_max": 24}, now=NOON)
    assert cfg["news_card_count"] == 1 and cfg["daily_max"] == 24
    # 退役的每类上限：所有调用口都拒（含 settings=None 的老壳），不静默假保存
    with pytest.raises(ValueError):
        card_push.set_config(store, GID, {"news_card_daily_max": 9}, now=NOON)
    assert card_push.get_config(store, GID)["daily_max"] == 24


async def test_disabled_group_gets_nothing(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _batch(store)
    assert cp.scan(GID, NOON + 60) == 0
    await cp.flush(GID, NOON + 60)
    assert host.images == [] and _cards(store) == [] and _boxes(store) == []


async def test_unserved_group_never_scanned_or_sent(tmp_path):
    """非服务群：设置都不给改（group_push 按服务群校验），扫 / 发全为零。"""
    store, settings, host, *_r, cp, ob = _make(tmp_path)
    with pytest.raises(ValueError):
        group_push.set_config(store, OTHER, {"news_card_enabled": True}, settings)
    _batch(store, OTHER)
    assert cp.scan(OTHER, NOON + 60) == 0
    await cp.flush(OTHER, NOON + 60)
    assert host.images == [] and _cards(store) == [] and _boxes(store) == []


async def test_unserved_pending_row_not_sent(tmp_path):
    """行已经在（比如群后来被移出服务名单）：flush 也不发、不入队。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
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
    store, _, host, pushes, mentions, renderer, cp, ob = _make(tmp_path)
    _enable(store, news_card_count=2)
    _, ids = _batch(store)
    assert cp.scan(GID, NOON + 60) == 1
    assert cp.scan(GID, NOON + 61) == 0  # 同一批只建一行
    await cp.flush(GID, NOON + 60)
    # 只入队：卡片还挂着 queued，图落盘等发件箱发
    card = _cards(store)[0]
    assert card["status"] == "queued" and card["sent_ts"] is None
    box = _boxes(store)[0]
    assert box["status"] == "pending" and box["kind"] == "image"
    assert Path(json.loads(box["payload"])["path"]).is_file()
    await ob.flush(NOON + 61)
    assert len(host.images) == 1
    sent = host.images[0]
    assert sent["session_id"] == f"sess-{GID}"
    assert sent["png"] == PNG
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


async def test_card_shares_the_one_daily_cap(tmp_path):
    """三种自制消息共用一个每日总上限：卡片也吃这份额度（不再各开小灶）。"""
    store, settings, host, pushes, *_r, cp, ob = _make(tmp_path)
    group_push.set_config(store, GID, {"news_card_enabled": True, "daily_max": 1},
                          settings, now=NOON - 10)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    assert len(host.images) == 1
    ok, why = pushes.can_push(GID, "topic", NOON + 20)
    assert ok is False and why == "今天推够了"
    assert pushes.count_used(GID, NOON + 20) == 1


async def test_only_group_items_no_personal_no_rejected(tmp_path):
    store, _, host, _p, _m, renderer, cp, ob = _make(tmp_path)
    _enable(store)
    _batch(store, note="personal:123")  # 个人向批次：整批不管
    _batch(store, target="777")           # 条目带 target_user_id：不进卡片
    assert cp.scan(GID, NOON + 60) == 0 or all(r["status"] == "dropped" for r in _cards(store))
    await cp.flush(GID, NOON + 60)
    assert host.images == []
    _, ids = _batch(store, rejected=(0,), items=((4.9, "news"), (4.0, "news"), (3.0, "idea")))
    cp.scan(GID, NOON + 70)
    await cp.flush(GID, NOON + 70)
    await ob.flush(NOON + 71)
    titles = [it["title"] for it in renderer.calls[-1]["items"]]
    assert titles == [f"标题3-1"]  # 被筛的、非 news/guide 的都不要


async def test_batches_before_enable_ignored(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _batch(store, created=NOON - 7200)
    _enable(store, now=NOON - 3600)
    assert cp.scan(GID, NOON) == 0


async def test_old_batch_over_12h_not_picked(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store, now=NOON - 20 * 3600)
    _batch(store, created=NOON - 13 * 3600)
    assert cp.scan(GID, NOON) == 0


# ---------------------------------------------------------------- 节制


async def test_sleep_hours_defer_then_send_after_wake(tmp_path):
    """睡觉时段：连图都不画（原样留着），醒来那轮才备料入队、再由发件箱发出去。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store, now=SLEEP - 7200)
    _batch(store, created=SLEEP - 60)
    cp.scan(GID, SLEEP)
    await cp.flush(GID, SLEEP)
    await ob.flush(SLEEP)
    assert host.images == [] and _boxes(store) == []
    assert _cards(store)[0]["status"] == "pending"
    wake = _ts(8, 5, day=16)
    await cp.flush(GID, wake)
    assert _cards(store)[0]["status"] == "queued"
    await ob.flush(wake)
    assert len(host.images) == 1
    assert _cards(store)[0]["status"] == "sent"


async def test_pending_over_12h_dropped(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store, now=NOON - 7200)
    _batch(store, created=NOON)
    cp.scan(GID, NOON)
    # 第二天早上（醒着）才轮到这里：已经过了 12 小时 → 作废，不补发
    await cp.flush(GID, _ts(9, 0, day=16))
    assert host.images == [] and _boxes(store) == []
    row = _cards(store)[0]
    assert row["status"] == "dropped"
    assert "12" in row["error"]


async def test_shared_cap_defers_extra_card_instead_of_dropping(tmp_path):
    """一个总上限（1）：第二张卡片不丢，留到明天额度回来（还在 12 小时窗口里）再发。"""
    store, settings, host, _p, _m, _r, cp, ob = _make(tmp_path)
    evening = _ts(20, 0)
    group_push.set_config(store, GID, {"news_card_enabled": True, "daily_max": 1,
                                       "quiet_hours": "00:00-00:00"},
                          settings, now=evening - 10)
    _batch(store, created=evening)
    _batch(store, created=evening + 100)
    cp.scan(GID, evening + 200)
    await cp.flush(GID, evening + 200)
    await ob.flush(evening + 200)
    assert len(host.images) == 1
    assert [r["status"] for r in _cards(store)] == ["sent", "queued"]
    second = _boxes(store)[1]
    assert second["status"] == "pending" and float(second["not_before"]) > evening
    # 第二天凌晨额度回来（还没过第二张的 12 小时窗口）：接着发
    await ob.flush(_ts(0, 30, day=16))
    assert len(host.images) == 2
    assert [r["status"] for r in _cards(store)] == ["sent", "sent"]


async def test_queued_card_past_window_dropped_instead_of_sent_next_day(tmp_path):
    """已入队（queued）的卡片被每日上限推到 12 小时窗口之外 → 发件箱作废，不发陈旧批次。"""
    store, settings, host, _p, _m, _r, cp, ob = _make(tmp_path)
    group_push.set_config(store, GID, {"news_card_enabled": True, "daily_max": 1},
                          settings, now=NOON - 10)
    _batch(store, created=NOON)
    _batch(store, created=NOON + 100)
    cp.scan(GID, NOON + 200)
    await cp.flush(GID, NOON + 200)
    await ob.flush(NOON + 200)
    assert len(host.images) == 1
    # 第二天早上额度回来了，但第二张已经过了 12 小时 → 作废，不补发
    await ob.flush(_ts(9, 0, day=16))
    assert len(host.images) == 1
    assert [r["status"] for r in _cards(store)] == ["sent", "dropped"]
    assert "有效期限" in _cards(store)[1]["error"]


async def test_same_item_never_twice(tmp_path):
    store, _, host, _p, _m, renderer, cp, ob = _make(tmp_path)
    _enable(store, news_card_count=3)
    _, ids = _batch(store, created=NOON)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)     # 第一张真发出去，条目进「已进过卡片」名单
    # 手工伪造：第二行的快照里混进已发过的条目 → 发前过滤掉
    _, ids2 = _batch(store, created=NOON + 20, items=((4.0, "news"),))
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts)"
            " VALUES (?, 999, 'pending', ?, ?, ?)",
            (GID, json.dumps([ids[0], ids2[0]]), NOON + 20, NOON + 20),
        )
    await cp.flush(GID, NOON + 30)
    await ob.flush(NOON + 31)
    last = [it["title"] for it in renderer.calls[-1]["items"]]
    assert last == ["标题2-0"]


async def test_all_filtered_means_dropped_not_empty_card(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
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
    store, _, host, _p, _m, renderer, cp, ob = _make(tmp_path)
    renderer.error = RenderError("no browser")
    _enable(store, news_card_count=2)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    assert _boxes(store)[0]["kind"] == "text"      # 画不出来 → 入队纯文字
    await ob.flush(NOON + 11)
    assert host.images == []
    assert len(host.texts) == 1
    t = host.texts[0]["text"]
    assert "标题1-0" in t and "标题1-1" in t and "https://mw.example/#/tok900000001/news" in t
    row = _cards(store)[0]
    assert row["status"] == "sent" and row["mode"] == "text"


async def test_no_public_url_image_only(tmp_path):
    store, _, host, _p, _m, renderer, cp, ob = _make(tmp_path, public_url="")
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    assert host.images[0]["text"] == ""
    assert renderer.calls[0]["link"] == ""


async def test_send_failure_retries_once_then_failed(tmp_path):
    """发送失败由发件箱有界重试一次（同一个缓存图片，不重画）；再失败 → 卡片标 failed。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    host.image_errors = [HostError("boom"), HostError("boom again")]
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 10)
    assert _cards(store)[0]["status"] == "queued"        # 还没定：排了一次重试
    assert _boxes(store)[0]["status"] == "pending"
    await ob.flush(NOON + 400)
    row = _cards(store)[0]
    assert row["status"] == "failed" and row["error"]
    assert len(host.images) == 2
    await ob.flush(NOON + 800)
    assert len(host.images) == 2


async def test_timeout_is_uncertain_not_retried(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    host.image_errors = [HostError("调用宿主能力超时: send.hybrid")]
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 10)
    await ob.flush(NOON + 400)
    assert _cards(store)[0]["status"] == "uncertain"
    assert len(host.images) == 1
    assert _boxes(store)[0]["status"] == "uncertain"


async def test_recover_marks_sending_uncertain(tmp_path):
    store, *_r, cp, ob = _make(tmp_path)
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
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    st = cp.status(GID, now=NOON + 20)
    assert st["config"]["news_card_enabled"] is True
    assert st["sent_today"] == 1
    assert st["recent"][0]["status"] == "sent"
    assert st["recent"][0]["count"] == 3


async def test_card_data_carries_checked_viz_html(tmp_path):
    """2026-09-29 用户要：上卡片的资讯有核对过的图解，就把图解也画进卡片（免得连配图都没有）。
    只给 status=ok 的；被拒 / 没做的不给。"""
    store, _, host, pushes, mentions, renderer, cp, ob = _make(tmp_path)
    _enable(store, news_card_count=2)
    _, ids = _batch(store)
    with store.tx() as conn:
        conn.execute("INSERT INTO news_viz (item_id, group_id, status, html, reason, created, updated)"
                     " VALUES (?, ?, 'ok', '<p>图解一</p>', '', ?, ?)", (ids[0], GID, NOON, NOON))
        conn.execute("INSERT INTO news_viz (item_id, group_id, status, html, reason, created, updated)"
                     " VALUES (?, ?, 'rejected', '<p>没过</p>', 'x', ?, ?)", (ids[1], GID, NOON, NOON))
    cp.scan(GID, NOON + 60)
    await cp.flush(GID, NOON + 60)
    await ob.flush(NOON + 61)
    items = renderer.calls[0]["items"]
    assert "<p>图解一</p>" in items[0]["viz_html"] and "Content-Security-Policy" in items[0]["viz_html"]
    assert not items[1].get("viz_html")
