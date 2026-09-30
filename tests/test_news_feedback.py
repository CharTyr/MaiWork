"""news_feedback.py：自动收的资讯反馈（docs/10 第七节第 4 步，2026-09-30）。

- 回复 / 引用资讯卡片 → reply（+3，按卡片上的条数平分）；网页点开原文 → click（+2，同一浏览器同一条一天算一次）；
  群里接着聊 → mention（+2，关键词在发出后 6 小时内命中、再由判断确认）；没人理不记负分；
- 不存群友原始账号：actor 一律是「群号:账号」的哈希；
- 同一事件重复记只算一次（幂等）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import chatlog, news_feedback as nf
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    with s.tx() as conn:
        nf.ensure_schema(conn)
    yield s
    s.close()


def _run(c):
    return asyncio.run(c)


def _item(store, title="甲", *, gid=GID, keywords=("开放世界", "地下城"), created=NOW - 3600, url="https://a.example/1", rejected=0):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, keywords, rejected, created)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (gid, title, url.split("://")[1], json.dumps([{"url": url, "site": "a.example"}]),
             json.dumps(list(keywords), ensure_ascii=False), rejected, created),
        )
        return int(cur.lastrowid)


def _card(store, item_ids, mid="9001", *, gid=GID, status="sent"):
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, sent_ts, message_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, len(item_ids) + int(mid), status, json.dumps(item_ids), NOW - 3000, NOW - 3000, mid),
        )


def _rows(store):
    return store.read().execute("SELECT * FROM news_feedback ORDER BY id").fetchall()


def test_schema_idempotent(store):
    with store.tx() as conn:
        nf.ensure_schema(conn)
        nf.ensure_schema(conn)


def test_actor_hash_hides_raw_id():
    h = nf.actor_hash(GID, "10001")
    assert h and "10001" not in h and len(h) == 16
    assert h == nf.actor_hash(GID, "10001") and h != nf.actor_hash("222", "10001")


def test_record_idempotent(store):
    iid = _item(store)
    assert nf.record(store, GID, iid, "reply", actor="a", message_id="m1", now=NOW) is True
    assert nf.record(store, GID, iid, "reply", actor="a", message_id="m1", now=NOW) is False
    assert len(_rows(store)) == 1 and _rows(store)[0]["weight"] == 3.0


def test_reply_to_card_records_each_item(store):
    a, b = _item(store, "甲"), _item(store, "乙", url="https://a.example/2")
    _card(store, [a, b], "9001")
    idx = nf.CardIndex()
    assert idx.on_message(store, GID, "10001", "m5", "9001", NOW) == 2
    rows = _rows(store)
    assert {r["item_id"] for r in rows} == {a, b}
    assert all(r["kind"] == "reply" and r["weight"] == 1.5 for r in rows)
    assert all("10001" not in r["actor"] for r in rows)


def test_reply_other_group_or_unknown_ignored(store):
    a = _item(store)
    _card(store, [a], "9001")
    idx = nf.CardIndex()
    assert idx.on_message(store, "222", "10001", "m5", "9001", NOW) == 0
    assert idx.on_message(store, GID, "10001", "m6", "4242", NOW) == 0
    assert idx.on_message(store, GID, "10001", "m7", "", NOW) == 0
    assert _rows(store) == []


def test_reply_hook_never_raises():
    idx = nf.CardIndex()
    assert idx.on_message(SimpleNamespace(), GID, "1", "m", "9001", NOW) == 0  # 坏 store 也不抛


def test_card_index_caches(store):
    a = _item(store)
    idx = nf.CardIndex()
    assert idx.on_message(store, GID, "1", "m1", "9001", NOW) == 0  # 卡片还没发
    _card(store, [a], "9001")
    assert idx.on_message(store, GID, "1", "m2", "9001", NOW + 10) == 0  # 60 秒内用缓存
    assert idx.on_message(store, GID, "1", "m3", "9001", NOW + 61) == 1


def test_click_returns_stored_url_and_dedupes_per_day(store):
    iid = _item(store, url="https://a.example/deep/path?x=1")
    url = nf.click(store, GID, iid, client="browser-1", now=NOW)
    assert url == "https://a.example/deep/path?x=1"
    assert nf.click(store, GID, iid, client="browser-1", now=NOW + 60) == url
    assert len(_rows(store)) == 1
    nf.click(store, GID, iid, client="browser-1", now=NOW + 86400 * 2)
    assert len(_rows(store)) == 2


def test_click_rejects_other_group_unknown_and_rejected(store):
    iid = _item(store)
    bad = _item(store, "被筛", url="https://b.example/1", rejected=1)
    assert nf.click(store, "222", iid, client="c", now=NOW) is None
    assert nf.click(store, GID, 99999, client="c", now=NOW) is None
    assert nf.click(store, GID, bad, client="c", now=NOW) is None


def _chat(store, text, *, mid, ts, uid="20001"):
    chatlog.record_messages(store, GID, [SimpleNamespace(is_bot=False, text=text, id=mid, ts=ts, user_id=uid, user_name="n")], now=ts)


def test_mention_round_judges_hits_in_window(store):
    iid = _item(store, "《地下城2》开放世界", keywords=["开放世界", "地下城"], created=NOW - 3600)
    other = _item(store, "无人提", keywords=["量子芯片"], url="https://a.example/3", created=NOW - 3600)
    _chat(store, "地下城2 改成开放世界了？有点意思", mid="c1", ts=NOW - 1800)
    _chat(store, "开放世界的地下城我喜欢", mid="c2", ts=NOW - 1700, uid="20002")
    _chat(store, "今天吃什么", mid="c3", ts=NOW - 1600)
    seen = {}

    async def judge(cands):
        seen["cands"] = cands
        return {c["item_id"] for c in cands}

    n = _run(nf.mention_round(store, GID, NOW, judge=judge))
    assert n == 1
    assert [c["item_id"] for c in seen["cands"]] == [iid]
    assert len(seen["cands"][0]["messages"]) == 2
    rows = _rows(store)
    assert {r["kind"] for r in rows} == {"mention"} and len(rows) == 2
    assert other not in {r["item_id"] for r in rows}
    # 判过的不再判
    n2 = _run(nf.mention_round(store, GID, NOW + 600, judge=judge))
    assert n2 == 0


def test_mention_round_no_record_on_no(store):
    _item(store, "《地下城2》开放世界", keywords=["开放世界", "地下城"])
    _chat(store, "地下城2 开放世界来了", mid="c1", ts=NOW - 1800)

    async def judge(cands):
        return set()

    assert _run(nf.mention_round(store, GID, NOW, judge=judge)) == 0
    assert _rows(store) == []


def test_mention_outside_window_ignored(store):
    _item(store, "旧的", keywords=["开放世界", "地下城"], created=NOW - 3600 * 10)
    _chat(store, "地下城 开放世界", mid="c1", ts=NOW - 60)  # 发出后 10 小时才聊，超出 6 小时

    async def judge(cands):
        raise AssertionError("不该调")

    assert _run(nf.mention_round(store, GID, NOW, judge=judge)) == 0


def test_summary(store):
    a = _item(store)
    nf.record(store, GID, a, "reply", actor="x", message_id="m1", now=NOW)
    nf.record(store, GID, a, "click", actor="y", message_id="d1", now=NOW)
    s = nf.summary(store, GID, NOW)
    assert s["items"][a]["reply"] == 1 and s["items"][a]["click"] == 1
    assert s["items"][a]["score"] == 5.0


# ----------------------------------------------------------------------
# 接线：收消息钩子认出「回复了资讯卡片」
# ----------------------------------------------------------------------


def test_intake_hook_records_reply_to_card(store):
    from fakes import hook_message
    from CharTyr_MaiWork.maiwork.config import load_settings
    from CharTyr_MaiWork.maiwork.intake import Intake, Signals

    settings, _ = load_settings({"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{GID}"}]}})
    a = _item(store)
    _card(store, [a], "9001")
    intake = Intake(lambda: settings, Signals(), store=store, spawn=lambda c: c.close())
    msg = hook_message(group_id=GID, text="[CQ:reply,id=9001]这个好", message_id="m77")
    out = _run(intake.handle(msg))
    assert out == {"action": "continue"}
    rows = _rows(store)
    assert len(rows) == 1 and rows[0]["kind"] == "reply" and rows[0]["message_id"] == "m77"


def test_record_creates_table_lazily(tmp_path):
    s = Store(tmp_path / "fresh.db")
    s.migrate()
    iid = _item(s)
    assert nf.record(s, GID, iid, "click", actor="a", message_id="d", now=NOW) is True
    s.close()
