"""card_push.IdeaMention：出了新构想，MaiWork 在群里提一嘴（每群开关默认关）。

个人向构想 @ 本人；话里绝不能露画像（「根据你的画像 / 我注意到你…」）也不能写 QQ 号。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from CharTyr_MaiWork.maiwork import card_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"
UID = "31415926"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
SLEEP = _ts(23, 30)


class Host:
    def __init__(self) -> None:
        self.texts: list[dict] = []

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({"session_id": session_id, "text": text, "at_user": at_user})
        return type("R", (), {"sent": True, "message_id": f"t{len(self.texts)}"})()


class Models:
    def __init__(self, replies=None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append({"role": role, "messages": messages, **kw})
        r = self.replies.pop(0) if self.replies else json.dumps({"text": "想到个点子，要不要一起试试？"})
        if isinstance(r, Exception):
            raise r
        return type("C", (), {"text": r})()


def _make(tmp_path, *, replies=None, public_url="https://mw.example"):
    store = Store(tmp_path / "t.db")
    store.migrate()
    cfg = {"groups": {"serve": [{"group": f"qq:{GID}"}]}}
    if public_url:
        cfg["console"] = {"public_url": public_url}
    settings, _ = load_settings(cfg)
    host = Host()
    models = Models(replies)
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    im = card_push.IdeaMention(store, host, models, pushes, mentions, lambda: settings)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
    return store, host, models, pushes, im


def _idea(store, gid=GID, *, created=NOON, target="", title="我可以帮群里做个番剧追更表", state="new"):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, target_user_id)"
            " VALUES (?, ?, '每周自动汇总更新', '画像里说他最近在考研', ?, ?, ?, ?)",
            (gid, title, state, created, created, target),
        )
        return int(cur.lastrowid)


def _enable(store, gid=GID, now=NOON - 3600, **kw):
    patch = {"idea_mention_enabled": True}
    patch.update(kw)
    return card_push.set_config(store, gid, patch, now=now)


def _rows(store):
    return store.read().execute("SELECT * FROM idea_mentions ORDER BY id").fetchall()


async def test_off_by_default(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _idea(store)
    assert im.scan(GID, NOON + 10) == 0
    await im.flush(GID, NOON + 10)
    assert host.texts == [] and models.calls == []


async def test_unserved_group_nothing(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _enable(store, OTHER)
    _idea(store, OTHER)
    assert im.scan(OTHER, NOON + 10) == 0
    await im.flush(OTHER, NOON + 10)
    assert host.texts == [] and models.calls == []


async def test_group_idea_mentioned_once_with_link(tmp_path):
    store, host, models, pushes, im = _make(tmp_path)
    _enable(store)
    iid = _idea(store)
    assert im.scan(GID, NOON + 10) == 1
    assert im.scan(GID, NOON + 11) == 0
    await im.flush(GID, NOON + 10)
    await im.flush(GID, NOON + 20)
    assert len(host.texts) == 1
    t = host.texts[0]
    assert t["at_user"] == ""
    assert "想到个点子" in t["text"]
    assert f"https://mw.example/#/tok{GID}/ideas/I-{iid}" in t["text"]
    assert _rows(store)[0]["status"] == "sent"
    n = store.read().execute("SELECT COUNT(*) c FROM pushes WHERE kind='idea_mention'").fetchone()["c"]
    assert n == 1
    ok, why = pushes.can_push(GID, "topic", NOON + 30)
    assert ok, why


async def test_prompt_never_gets_basis(tmp_path):
    """basis 里是「为什么适合（引画像）」——根本不给模型，从源头防泄露。"""
    store, host, models, _p, im = _make(tmp_path)
    _enable(store)
    _idea(store, target=UID)
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    prompt = json.dumps(models.calls[0]["messages"], ensure_ascii=False)
    assert "考研" not in prompt
    assert UID not in prompt
    assert "画像" in prompt  # 规则里写了「不许提画像」


async def test_personal_idea_ats_member(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _enable(store)
    _idea(store, target=UID)
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    assert host.texts[0]["at_user"] == UID
    assert UID not in host.texts[0]["text"]


@pytest.mark.parametrize("bad", [
    "根据你的画像，你可能会喜欢这个",
    "我注意到你最近在忙考研，要不要试试",
    "看你平时经常聊番剧，来试试",
    f"@{UID} 来看看",
])
async def test_leaky_text_replaced_by_safe_template(tmp_path, bad):
    store, host, models, _p, im = _make(tmp_path, replies=[json.dumps({"text": bad})])
    _enable(store)
    _idea(store, target=UID)
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    text = host.texts[0]["text"]
    for w in ("画像", "注意到", "平时", UID):
        assert w not in text
    assert "番剧追更表" in text  # 模板用标题


async def test_model_failure_uses_template(tmp_path):
    store, host, models, _p, im = _make(tmp_path, replies=[ModelError("down")])
    _enable(store)
    _idea(store)
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    assert len(host.texts) == 1 and "番剧追更表" in host.texts[0]["text"]


async def test_sleep_defer_and_daily_cap(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _enable(store, now=SLEEP - 7200, idea_mention_daily_max=1)
    _idea(store, created=SLEEP - 60)
    _idea(store, created=SLEEP - 30, title="我可以做个群聊周报")
    im.scan(GID, SLEEP)
    await im.flush(GID, SLEEP)
    assert host.texts == [] and models.calls == []
    await im.flush(GID, _ts(8, 5, day=16))
    assert len(host.texts) == 1
    assert [r["status"] for r in _rows(store)] == ["sent", "dropped"]


async def test_dismissed_idea_not_mentioned(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _enable(store)
    iid = _idea(store)
    im.scan(GID, NOON + 10)
    with store.tx() as conn:
        conn.execute("UPDATE ideas SET state='dismissed' WHERE id=?", (iid,))
    await im.flush(GID, NOON + 10)
    assert host.texts == []
    assert _rows(store)[0]["status"] == "dropped"


async def test_ideas_before_enable_ignored(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _idea(store, created=NOON - 7200)
    _enable(store, now=NOON - 3600)
    assert im.scan(GID, NOON) == 0


async def test_has_due_and_status(tmp_path):
    store, host, models, _p, im = _make(tmp_path)
    _enable(store)
    _idea(store)
    im.scan(GID, NOON + 10)
    assert im.has_due(GID, NOON + 10) is True
    await im.flush(GID, NOON + 10)
    assert im.has_due(GID, NOON + 20) is False
    st = im.status(GID, now=NOON + 20)
    assert st["sent_today"] == 1 and st["recent"][0]["status"] == "sent"
