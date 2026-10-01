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
    def __init__(self, persona=None, msgs=None) -> None:
        self.texts: list[dict] = []
        self.persona = dict(persona or {})
        self.msgs = list(msgs or [])

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({"session_id": session_id, "text": text, "at_user": at_user})
        return type("R", (), {"sent": True, "message_id": f"t{len(self.texts)}"})()

    async def config(self, key, default=None):
        return self.persona.get(key, default)

    async def messages(self, session_id, start, end, limit, **kw):
        return list(self.msgs)


class BotMsg:
    def __init__(self, text: str) -> None:
        self.text = text
        self.is_bot = True
        self.user_name = "机器人"


class Identity:
    """假的 identity（voice.persona 用它读 SOUL）。"""

    def __init__(self, blocks=None) -> None:
        self.blocks = dict(blocks or {})

    def prompt_block(self, kind, **kw):
        return self.blocks.get(kind, "")


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


def _make(tmp_path, *, replies=None, public_url="https://mw.example", persona=None, msgs=None,
          identity=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    cfg = {"groups": {"serve": [{"group": f"qq:{GID}"}]}}
    if public_url:
        cfg["console"] = {"public_url": public_url}
    settings, _ = load_settings(cfg)
    host = Host(persona, msgs)
    models = Models(replies)
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    im = card_push.IdeaMention(store, host, models, pushes, mentions, lambda: settings,
                               identity=identity)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
    return store, host, models, pushes, im


def _idea(store, gid=GID, *, created=NOON, target="", title="我可以帮群里做个番剧追更表", state="new",
          origin=""):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, basis, state, created, updated,"
            " target_user_id, origin)"
            " VALUES (?, ?, '每周自动汇总更新', '画像里说他最近在考研', ?, ?, ?, ?, ?)",
            (gid, title, state, created, created, target, origin),
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


# ----------------------------------------------------------------------
# 关心式问法 + 由头（2026-10 与用户定）
# ----------------------------------------------------------------------


async def test_prompt_carries_persona_and_origin(tmp_path):
    """提示词只按 SOUL 说话（不读 MaiBot 人格、不拿它的发言当样例），并带上由头。"""
    store, host, models, _p, im = _make(
        tmp_path,
        persona={
            "bot.nickname": "小麦",
            "personality.personality": "热心肠",
            "personality.reply_style": "随口一聊",
        },
        msgs=[BotMsg("这块板子的事我记着呢")],
        identity=Identity({"soul": "## MaiWork 的身份\n你是这群的老熟人。"}),
    )
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    prompt = json.dumps(models.calls[0]["messages"], ensure_ascii=False)
    assert "小麦" not in prompt and "热心肠" not in prompt and "随口一聊" not in prompt
    assert "这块板子的事我记着呢" not in prompt  # 不拿 MaiBot 的发言当样例
    assert "老熟人" in prompt                    # SOUL
    assert "不自我介绍" in prompt and "不寒暄" in prompt
    # 不给它塞「助手」身份（会引出「作为助手…」）
    assert "的助手。" not in prompt and "群里的 AI 助手" not in prompt
    assert "涂击队百层挑战" in prompt            # 由头（origin）
    assert "关心" in prompt                      # 要求关心式问法
    assert "问句" in prompt                      # 结尾要是问句
    assert "别推销" in prompt and "感兴趣的话" in prompt  # 推销腔被点名禁止


async def test_caring_reply_is_kept(tmp_path):
    """模型写出的关心式问句照用（不被当成推销腔换掉）。"""
    good = json.dumps(
        {"text": "话说之前大家聊的那个涂击队百层挑战后来怎么样了？要我帮忙吗？"},
        ensure_ascii=False,
    )
    store, host, models, _p, im = _make(tmp_path, replies=[good])
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    first_line = host.texts[0]["text"].split("\n")[0]
    assert first_line == "话说之前大家聊的那个涂击队百层挑战后来怎么样了？要我帮忙吗？"


@pytest.mark.parametrize("bad", [
    "我可以帮你们搓一份极乐迪斯科防翻车手册，感兴趣的话点进去看看",
    "给大家带来一个好东西，安利一下",
    "推荐给大家一个小工具，点进去看看",
])
async def test_pitch_talk_replaced_by_template(tmp_path, bad):
    """推销腔（我可以帮 / 给大家带来 / 推荐给大家 / 安利 / 感兴趣的话 / 点进去看看）→ 换模板。"""
    store, host, models, _p, im = _make(
        tmp_path, replies=[json.dumps({"text": bad}, ensure_ascii=False)]
    )
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    text = host.texts[0]["text"]
    for w in card_push._PITCH_WORDS:
        assert w not in text, w
    assert "涂击队百层挑战" in text
    assert "要我帮忙吗" in text


@pytest.mark.parametrize("bad", [
    "我是小麦，话说之前那个涂击队百层挑战后来怎么样了？",
    "大家好，之前那个涂击队百层挑战还在搞吗？",
    "作为群助手，想问问百层挑战后来怎么样了？",
])
async def test_self_intro_replaced_by_template(tmp_path, bad):
    """自我介绍 / 寒暄（2026-10-01 用户定：不要自我介绍和废话）→ 换模板。"""
    store, host, models, _p, im = _make(
        tmp_path, replies=[json.dumps({"text": bad}, ensure_ascii=False)]
    )
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    first_line = host.texts[0]["text"].split("\n")[0]
    assert first_line == "话说之前大家聊的那个涂击队百层挑战后来怎么样了？要我帮忙吗？"

