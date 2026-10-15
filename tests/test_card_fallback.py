"""资讯卡片「最终失败 → 纯文字兜底」的行为（card_push.py + outbox.py）。

用户 2026-10-15 定：卡片图发不出去（发件箱有界重试一次之后仍然 failed）时，
自动再发一条**纯文字列表**（就是现有「画图失败退回纯文字」那一套），
同样走发件箱、同样受每日额度 / 睡觉时段 / 12 小时有效期约束；

- 兜底只发一次：兜底文字自己再失败也不兜底第二次；
- uncertain（超时，可能已经发出去了）**不兜底**，避免重复刷屏；
- 兜底的结果写回卡片行 / kv（不改表结构）。

用假 host + 假渲染器 + 真 Store / 真 Outbox，不联网、不起浏览器。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import card_push, clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
# 发件箱按内容判图片类型：假渲染器给一张「真 JPEG 头」的数据（2026-10-15 起卡片图是 JPEG）
JPEG = b"\xff\xd8\xff\xe0" + b"card-bytes"
FALLBACK_KEY = "news_card:1:fallback"


class CardHost:
    def __init__(self) -> None:
        self.images: list[dict] = []
        self.texts: list[dict] = []
        self.image_errors: list[Exception] = []
        self.text_errors: list[Exception] = []

    async def send_image(self, session_id, data, *, text=""):
        self.images.append({"session_id": session_id, "data": data, "text": text})
        if self.image_errors:
            raise self.image_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": f"img{len(self.images)}"})()

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({"session_id": session_id, "text": text, "at_user": at_user})
        if self.text_errors:
            raise self.text_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": f"t{len(self.texts)}"})()


class Renderer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, data):
        self.calls.append(data)
        return JPEG


def _make(tmp_path, *, public_url="https://mw.example", groups=(GID,)):
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
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    cp = card_push.CardPush(store, host, pushes, mentions, lambda: settings,
                            renderer=renderer, outbox=ob)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)",
                (g, f"sess-{g}", "测试群", f"tok{g}"),
            )
    return store, settings, host, pushes, mentions, renderer, cp, ob


def _batch(store, gid=GID, *, created=NOON, items=((4.5, "news"), (4.0, "guide"))):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 10, ?, 0, '', ?)",
            (gid, created, len(items), created),
        )
        bid = int(cur.lastrowid)
        ids = []
        for i, (score, kind) in enumerate(items):
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, why, sources, score, created,"
                " kind, image_url, keywords, target_user_id, rejected)"
                " VALUES (?, ?, ?, '摘要', '值得看', ?, ?, ?, ?, '', '[\"k\"]', '', 0)",
                (bid, gid, f"标题{bid}-{i}",
                 json.dumps([{"url": f"https://s.example/{bid}/{i}", "site": "s.example"}]),
                 score, created, kind),
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


def _fallback_boxes(store):
    return [b for b in _boxes(store) if str(b["key"]).endswith(":fallback")]


async def _fail_card(store, host, cp, ob, *, moment=NOON + 10):
    """让一张卡片走完「重试一次 → 最终 failed」这条路。"""
    host.image_errors = [HostError("图发不出去"), HostError("图还是发不出去")]
    _enable(store)
    _batch(store)
    cp.scan(GID, moment)
    await cp.flush(GID, moment)
    await ob.flush(moment)              # 第一次失败 → 排一次重试
    await ob.flush(moment + 400)        # 第二次失败 → 最终 failed（这里会入队纯文字兜底）
    return moment


# ---------------------------------------------------------------- 兜底


async def test_failed_card_enqueues_text_fallback_once(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    await _fail_card(store, host, cp, ob)
    assert _cards(store)[0]["status"] == "failed"
    boxes = _fallback_boxes(store)
    assert len(boxes) == 1
    assert str(boxes[0]["key"]) == FALLBACK_KEY
    assert str(boxes[0]["kind"]) == "text"
    payload = json.loads(boxes[0]["payload"])
    # 就是「画图失败退回纯文字列表」那一套：标题 + 本群链接
    assert "标题1-0" in payload["text"] and "标题1-1" in payload["text"]
    assert "https://mw.example/#/tok900000001/news" in payload["text"]
    # 和卡片同一份节制口径（开关 / 额度都按 news_card 算）
    assert payload["push_kind"] == "news_card"


async def test_fallback_carries_same_12h_deadline(tmp_path):
    """兜底也受 12 小时有效期约束：expires_ts 和卡片本身同一时刻。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    await _fail_card(store, host, cp, ob)
    row = _cards(store)[0]
    payload = json.loads(_fallback_boxes(store)[0]["payload"])
    assert float(payload["expires_ts"]) == float(row["created"]) + card_push.EXPIRE_S


async def test_uncertain_card_has_no_fallback(tmp_path):
    """超时 = 可能已经发出去了：不兜底，绝不重复刷屏。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    host.image_errors = [HostError("调用宿主能力超时: send.hybrid")]
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)
    await ob.flush(NOON + 10)
    await ob.flush(NOON + 400)
    assert _cards(store)[0]["status"] == "uncertain"
    assert _fallback_boxes(store) == []


async def test_fallback_failure_is_recorded_and_not_retried_again(tmp_path):
    """兜底文字自己也失败 → 只记结果，绝不兜底第二次。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    host.text_errors = [HostError("文字也发不出去"), HostError("文字还是发不出去")]
    moment = await _fail_card(store, host, cp, ob)
    await ob.flush(moment + 800)        # 兜底文字的第二次尝试也失败
    assert len(_fallback_boxes(store)) == 1
    assert str(_fallback_boxes(store)[0]["status"]) == "failed"
    assert host.texts, "兜底文字至少真的试发过"
    # 卡片行记下了兜底的结果（不加迁移：写在已有 error 文本里）
    err = str(_cards(store)[0]["error"])
    assert "兜底" in err
    # 也没生成「兜底的兜底」
    keys = [str(b["key"]) for b in _boxes(store)]
    assert len(keys) == len(set(keys))


async def test_fallback_sent_records_success_on_card(tmp_path):
    """兜底文字真发出去了：卡片行标出来（error 里加一句），且不再有别的兜底。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    await _fail_card(store, host, cp, ob)
    assert str(_fallback_boxes(store)[0]["status"]) == "sent"
    assert len(host.texts) == 1
    assert "标题1-0" in host.texts[0]["text"]
    assert "兜底" in str(_cards(store)[0]["error"])
    # 卡片本身仍是 failed（只有 sent 才算真发出去），不复活
    assert _cards(store)[0]["status"] == "failed"


async def test_fallback_not_duplicated_by_second_result_hook(tmp_path):
    """结果 hook 被重复触发（比如重启 recover 再对账一次）也不会多入队一条。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    await _fail_card(store, host, cp, ob)
    assert len(_fallback_boxes(store)) == 1
    # 再对账一次：卡片已是 failed，outbox 那条也是 failed → 不该再加一条兜底
    cp.on_result({"key": "news_card:1", "group_id": GID, "outcome": "failed", "error": "又报一次"})
    assert len(_fallback_boxes(store)) == 1


async def test_no_fallback_when_items_all_gone(tmp_path):
    """卡片条目在发送失败之后被撤下 → 没有可发的文字，不建空兜底。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    host.image_errors = [HostError("图发不出去"), HostError("图还是发不出去")]
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    await cp.flush(GID, NOON + 10)      # 已入队，卡片挂着 queued
    with store.tx() as conn:
        conn.execute("UPDATE news_items SET rejected=1")
    await ob.flush(NOON + 10)
    await ob.flush(NOON + 400)
    assert _cards(store)[0]["status"] == "failed"
    assert _fallback_boxes(store) == []


async def test_queued_fallback_wording_says_enqueued_not_sent(tmp_path):
    """兜底只是入队时，卡片行 / kv 不许写成「已补发」——发件箱还可能推迟或作废它。

    2026-10-15 复核：`_enqueue_text_fallback` 在 `outbox.enqueue` 之后就把卡片行写成
    「已自动补发纯文字兜底」，可这一轮根本没发（开关 / 睡觉时段 / 每日额度会推迟，
    12 小时有效期会作废）。这里走真实入口（发件箱的结果回调）但不 flush，
    钉住「只入队」的措辞。
    """
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _batch(store)
    cp.scan(GID, NOON + 10)
    cp.on_result({"key": "news_card:1", "group_id": GID, "outcome": "failed", "error": "图发不出去"})
    boxes = _fallback_boxes(store)
    assert len(boxes) == 1 and str(boxes[0]["status"]) == "pending", "兜底只入队，还没发"
    assert str(store.kv_get("news_card.fallback.1")["status"]) == "queued"
    err = str(_cards(store)[0]["error"])
    assert "入队" in err
    assert "已补发" not in err and "已发出" not in err, err
    assert host.texts == [], "这一轮不该真发出去"
