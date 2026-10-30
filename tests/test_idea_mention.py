"""card_push.IdeaMention：出了新构想，MaiWork 在群里提一嘴（每群开关默认关）。

个人向构想 @ 本人；话里绝不能露画像（「根据你的画像 / 我注意到你…」）也不能写 QQ 号。

0.8.0 归一之后：提一嘴只「写好话 + 入队」（默认模板 / SOUL 人设那套没变），
真正发出去、睡觉时段、共用的每日总上限都在发件箱；发出之后由结果 hook 回写 idea_mentions。

个人向的那一份更严（2026-10 用户定，见 idea_guard.py）：
- 只给「还是当前关注成员 + 个人向开关还开着」的群；同一人 3 天冷却（未落地 / 已提 /
  不确定全算）、每群 3 条在途（7 天新鲜期）。
- 发送前复核：先把**他本人在本群**最近说的话（有界、只按 user_id 精确取）交给模型严格
  JSON 判读，失败关闭；上次提过之后再提必须有「新的明确需要」的原文依据，沉默不算需要。
- 发件箱发送前还有一道纯代码复核（挂 preflight hook）：人/开关变了、构想被划掉、
  依据对不上 → 作废，绝不照着旧决定硬发。
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from CharTyr_MaiWork.maiwork import card_push, group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "555000"
UID = "31415926"
UID2 = "27182818"
UID3 = "16180339"
UID4 = "14142135"
UID5 = "17320508"


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
        if self.replies:
            r = self.replies.pop(0)
        elif str(kw.get("purpose") or "") == "card_push.idea_guard":
            # 个人提一嘴的发送前复核：默认「没结果、没拒绝、也没有新的明确需要」
            r = _guard()
        else:
            # 默认假模型也给一句「有具体事 + 问句」的话（2026-10 第二步的措辞口径）
            r = json.dumps({"text": "我可以帮群里做个番剧追更表，要不要我来弄？"}, ensure_ascii=False)
        if isinstance(r, Exception):
            raise r
        return type("C", (), {"text": r})()


def _guard(*, resolved: bool = False, declined: bool = False, need: bool = False,
           evidence=(), raw: str | None = None) -> str:
    """模型复核的严格 JSON（没给 raw 就按三个布尔拼）。"""
    if raw is not None:
        return raw
    return json.dumps({"resolved": bool(resolved), "declined": bool(declined),
                       "need": bool(need), "evidence": list(evidence)})


def _guard_call(models) -> dict:
    calls = [c for c in models.calls if str(c.get("purpose") or "") == "card_push.idea_guard"]
    assert calls, "应该叫过一次「发送前复核」"
    return calls[-1]


def _write_calls(models) -> list[dict]:
    return [c for c in models.calls if str(c.get("purpose") or "") == "card_push.idea_mention"]


_CHAT_SEQ = itertools.count(1)


def _chat(store, text, *, uid=UID, ts=NOON, gid=GID, mid=""):
    """往 chat_log 塞一条群聊原话；返回 message_id（复核依据就是它）。"""
    mid = mid or f"m{next(_CHAT_SEQ)}"
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
            " VALUES (?, ?, ?, ?, ?, '群友')",
            (text, gid, mid, float(ts), uid),
        )
    return mid


def _swap_focus(box: dict, **fields) -> None:
    """中途改个人向开关（settings 是 frozen dataclass，换一份重新塞进盒子里）。"""
    s = box["settings"]
    box["settings"] = dataclasses.replace(s, focus=dataclasses.replace(s.focus, **fields))


def _make(tmp_path, *, replies=None, public_url="https://mw.example", persona=None, msgs=None,
          identity=None, outbox=True, box=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    cfg = {"groups": {"serve": [{"group": f"qq:{GID}"}]},
           "environments": {"workspace_root": str(tmp_path)}}
    if public_url:
        cfg["console"] = {"public_url": public_url}
    settings, _ = load_settings(cfg)
    box = box if box is not None else {}
    box["settings"] = settings
    get_settings = lambda: box["settings"]          # noqa: E731
    host = Host(persona, msgs)
    models = Models(replies)
    pushes = Pushes(store, get_settings)
    mentions = Mentions(store, get_settings)
    # 真发件箱：提一嘴只入队，发送 / 节制 / 回写都在这里
    ob = Outbox(store, host, pushes, mentions, get_settings)
    im = card_push.IdeaMention(store, host, models, pushes, mentions, get_settings,
                               identity=identity, outbox=ob if outbox else None)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
                (g, f"sess-{g}", f"tok{g}"),
            )
        # 个人向提一嘴只提「当前关注成员」：测试里默认这几个人都在关注名单里
        for u in (UID, UID2, UID3, UID4, UID5):
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, reasons, note, pinned,"
                " removed, updated) VALUES (?, ?, ?, '[]', '', 1, 0, ?)",
                (GID, u, f"群友{u[-4:]}", NOON - 7200),
            )
    return store, host, models, pushes, im, ob


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


def _boxes(store):
    return store.read().execute("SELECT * FROM outbox ORDER BY id").fetchall()


async def test_off_by_default(tmp_path):
    store, host, models, _p, im, ob = _make(tmp_path)
    _idea(store)
    assert im.scan(GID, NOON + 10) == 0
    await im.flush(GID, NOON + 10)
    assert host.texts == [] and models.calls == []


async def test_unserved_group_nothing(tmp_path):
    """非服务群：设置都不给改（group_push 按服务群校验）；就算有残留配置也扫 / 发全零。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    settings = im._get_settings()
    with pytest.raises(ValueError):
        group_push.set_config(store, OTHER, {"idea_mention_enabled": True}, settings)
    _enable(store, OTHER)                      # 老调用口（settings=None）仍能写，但发不出去
    _idea(store, OTHER)
    assert im.scan(OTHER, NOON + 10) == 0
    await im.flush(OTHER, NOON + 10)
    assert host.texts == [] and models.calls == [] and _boxes(store) == []


async def test_personal_scan_keeps_only_one_pending_per_member(tmp_path):
    """同一人同时只留一条还没落地的个人提一嘴（不堆）。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _idea(store, target=UID, created=NOON + 1, title="我可以帮你做第二份清单")
    assert im.scan(GID, NOON + 10) == 1
    rows = _rows(store)
    assert sum(r["status"] == "pending" for r in rows) == 1


async def test_personal_scan_group_cap_within_horizon(tmp_path):
    """每群 7 天新鲜期里最多 3 条个人提一嘴在途；第 4 个人这轮先不建。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    for i, uid in enumerate((UID, UID2, UID3, UID4)):
        _idea(store, target=uid, created=NOON + i, title=f"我可以帮你做第 {i} 份清单")
    assert im.scan(GID, NOON + 10) == 3
    assert {str(r["at_user"]) for r in _rows(store)} == {UID, UID2, UID3}


async def test_personal_scan_old_rows_do_not_lock_and_stay_untouched(tmp_path):
    """8 天前的旧待发行不占位子（绝不永久锁死），也一行都不改。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    old = NOON - 8 * 86400.0
    with store.tx() as conn:
        for i, uid in enumerate((UID, UID2, UID3)):
            conn.execute(
                "INSERT INTO idea_mentions (group_id, idea_id, status, text, at_user, created,"
                " due_ts, error) VALUES (?, ?, 'pending', '老的待发行', ?, ?, ?, '老的待发行')",
                (GID, 9000 + i, uid, old, old),
            )
    _idea(store, target=UID4, created=NOON)
    assert im.scan(GID, NOON + 10) == 1
    rows = _rows(store)
    old_rows = [r for r in rows if int(r["idea_id"]) >= 9000]
    assert [str(r["status"]) for r in old_rows] == ["pending"] * 3
    assert all(str(r["error"]) == "老的待发行" for r in old_rows)


async def test_personal_scan_old_pending_does_not_lock_same_member(tmp_path):
    """同一个人 8 天前那条还没落地，也不该把新构想永久锁死。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    old = NOON - 8 * 86400.0
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO idea_mentions (group_id, idea_id, status, at_user, created, due_ts)"
            " VALUES (?, 9001, 'pending', ?, ?, ?)",
            (GID, UID, old, old),
        )
    _idea(store, target=UID, created=NOON)
    assert im.scan(GID, NOON + 10) == 1
    assert [str(r["status"]) for r in _rows(store)] == ["pending", "pending"]


async def test_personal_scan_skips_when_focus_member_gone(tmp_path):
    """已移出关注名单的人：一条都不建。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    with store.tx() as conn:
        conn.execute("UPDATE focus_members SET removed=1 WHERE group_id=? AND user_id=?",
                     (GID, UID))
    _idea(store, target=UID, created=NOON)
    assert im.scan(GID, NOON + 10) == 0
    assert _rows(store) == []


async def test_personal_scan_skips_when_personal_switch_off(tmp_path):
    """个人向产出开关关了：个人向不建，群向照旧（只关个人那一份）。"""
    box = {}
    store, host, models, _p, im, ob = _make(tmp_path, box=box)
    _enable(store)
    _swap_focus(box, personal_feeds=False)
    _idea(store, target=UID, created=NOON)
    assert im.scan(GID, NOON + 10) == 0
    assert _rows(store) == []
    _idea(store, created=NOON + 2, title="我可以给全群做份周报")
    assert im.scan(GID, NOON + 20) == 1


# ----------------------------------------------------------------------
# 个人提一嘴：发送前复核（模型严格 JSON，失败关闭）
# ----------------------------------------------------------------------


async def test_personal_flush_ignores_unrelated_resolved_chat_without_regex(tmp_path):
    """聊天里出现「已经搞定了」但说的是别的事：不再靠正则判成「已解决」，交给模型判读。

    这一条没有任何「他本人明确要这件事」的原话 → 复核按「没有明确需要」作废；关键是不能
    被判成「已解决 / 明确不需要」（那正是老正则的错法）。
    """
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "上周那个报表我已经搞定了，不用整理了", ts=NOON + 100)
    assert im.scan(GID, NOON + 200) == 1
    await im.flush(GID, NOON + 200)
    row = _rows(store)[0]
    assert row["status"] == "dropped"
    assert "明确需要" in row["error"]                      # 只是没有明确需要
    for w in ("有结果", "不需要", "已解决"):
        assert w not in row["error"]                      # 不是被判成已解决 / 被拒
    content = _guard_call(models)["messages"][0]["content"]
    assert "不可信" in content                             # 群聊原文标了不可信
    assert "报表我已经搞定了" in content
    assert _write_calls(models) == []
    assert _boxes(store) == [] and host.texts == []


async def test_personal_flush_drops_when_review_says_resolved(tmp_path):
    """复核判定这件事已经有结果 → 作废，不再写话、不入队。"""
    store, host, models, _p, im, ob = _make(tmp_path, replies=[_guard(resolved=True)])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那块板子的例程我已经跑通了", ts=NOON + 100)
    assert im.scan(GID, NOON + 200) == 1
    await im.flush(GID, NOON + 200)
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "有结果" in row["error"]
    assert _boxes(store) == [] and host.texts == []
    assert _write_calls(models) == []


async def test_personal_flush_drops_when_review_says_declined(tmp_path):
    """复核判定他本人明确说不需要 → 作废（不写话、不入队）。"""
    store, host, models, _p, im, ob = _make(tmp_path, replies=[_guard(declined=True)])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个不用你弄了，我自己来", ts=NOON + 100)
    assert im.scan(GID, NOON + 200) == 1
    await im.flush(GID, NOON + 200)
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "不需要" in row["error"]
    assert _boxes(store) == [] and host.texts == [] and _write_calls(models) == []


@pytest.mark.parametrize("bad", [
    ModelError("down"),
    "这不是 JSON",
    json.dumps({"resolved": "yes"}),
    json.dumps({"declined": False}),
])
async def test_personal_flush_review_fails_closed(tmp_path, bad):
    """复核没做成 / JSON 不严格 → 宁可少发一条；不写话、不入队、不发。"""
    store, host, models, _p, im, ob = _make(tmp_path, replies=[bad])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    assert im.scan(GID, NOON + 100) == 1
    await im.flush(GID, NOON + 100)
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "复核" in row["error"]
    assert _boxes(store) == [] and host.texts == []
    assert _write_calls(models) == []


async def test_personal_reproposal_after_silence_needs_fresh_need(tmp_path):
    """提过一次、对方一直没回应：3 天冷却过去也不再追（沉默不是需要）。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    assert im.scan(GID, NOON + 1) == 1
    await im.flush(GID, NOON + 1)
    await ob.flush(NOON + 2)
    assert _rows(store)[0]["status"] == "sent"
    models.calls.clear()
    later = NOON + 4 * 86400.0
    _idea(store, target=UID, created=later - 60, title="我可以帮你把这些表再理一遍")
    assert im.scan(GID, later) == 1                     # 冷却过了，可以复核
    await im.flush(GID, later)
    row = _rows(store)[-1]
    assert row["status"] == "dropped" and "明确需要" in row["error"]
    assert _write_calls(models) == []
    assert len(host.texts) == 1                         # 只有第一次那条


async def test_personal_reproposal_with_fresh_explicit_need_sends(tmp_path):
    """上次提过之后他本人明确说要 → 带原文依据复核通过，可以再提。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 1)
    await im.flush(GID, NOON + 1)
    await ob.flush(NOON + 2)
    later = NOON + 4 * 86400.0
    _chat(store, "那个追更表你帮我弄一下吧", uid=UID, ts=later - 120, mid="fresh-1")
    _chat(store, "这块板子我要不要买", uid=UID, ts=NOON - 500, mid="old-1")   # 上次提之前的老话
    _idea(store, target=UID, created=later - 60, title="我可以帮你把这些表再理一遍")
    assert im.scan(GID, later) == 1
    models.replies = [
        _guard(need=True, evidence=["fresh-1"]),
        json.dumps({"text": "我可以帮你把追更表理一遍，要不要这两天弄？"}, ensure_ascii=False),
    ]
    await im.flush(GID, later)
    assert _rows(store)[-1]["status"] == "queued"
    content = _guard_call(models)["messages"][0]["content"]
    assert "fresh-1" in content and "你帮我弄一下吧" in content
    assert "old-1" not in content and "我要不要买" not in content   # 只收上次提之后的话
    await ob.flush(later + 1)
    assert "要不要这两天弄" in host.texts[-1]["text"]


async def test_personal_reproposal_ignores_other_people_and_other_groups(tmp_path):
    """复核材料只取「他本人在本群」的话：别人的、别的群的一律不算。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 1)
    await im.flush(GID, NOON + 1)
    await ob.flush(NOON + 2)
    later = NOON + 4 * 86400.0
    _chat(store, "那个表你帮我弄一下", uid=UID2, ts=later - 100, mid="other-user")
    _chat(store, "那个表你帮我弄一下", uid=UID, ts=later - 90, gid=OTHER, mid="other-group")
    _idea(store, target=UID, created=later - 60, title="我可以帮你把这些表再理一遍")
    assert im.scan(GID, later) == 1
    await im.flush(GID, later)
    row = _rows(store)[-1]
    assert row["status"] == "dropped" and "明确需要" in row["error"]
    assert "你帮我弄一下" not in _guard_call(models)["messages"][0]["content"]
    assert len(host.texts) == 1 and len(_boxes(store)) == 1


async def test_personal_review_rejects_evidence_not_from_target(tmp_path):
    """模型引用了别人的原话当依据 → 依据对不上，失败关闭（不发）。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 1)
    await im.flush(GID, NOON + 1)
    await ob.flush(NOON + 2)
    later = NOON + 4 * 86400.0
    _chat(store, "帮我弄一下那个表", uid=UID2, ts=later - 100, mid="u2-1")
    _idea(store, target=UID, created=later - 60, title="我可以帮你把这些表再理一遍")
    assert im.scan(GID, later) == 1
    models.calls.clear()
    models.replies = [_guard(need=True, evidence=["u2-1"])]
    await im.flush(GID, later)
    row = _rows(store)[-1]
    assert row["status"] == "dropped" and "依据对不上" in row["error"]
    assert _write_calls(models) == []
    assert len(host.texts) == 1


# ----------------------------------------------------------------------
# 个人提一嘴：发件箱发送前的纯代码复核（排队期间状态变了就作废）
# ----------------------------------------------------------------------


async def test_personal_preflight_drops_when_focus_member_removed(tmp_path):
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    assert im.scan(GID, NOON + 1) == 1
    await im.flush(GID, NOON + 1)
    assert _boxes(store)[0]["status"] == "pending"
    with store.tx() as conn:
        conn.execute("UPDATE focus_members SET removed=1 WHERE group_id=? AND user_id=?",
                     (GID, UID))
    calls_before = len(models.calls)
    await ob.flush(NOON + 2)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "关注成员" in row["error"]
    assert len(models.calls) == calls_before      # 发送前复核是纯代码，不再叫模型


async def test_personal_preflight_drops_when_personal_switch_turns_off(tmp_path):
    box = {}
    store, host, models, _p, im, ob = _make(
        tmp_path, box=box, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 1)
    await im.flush(GID, NOON + 1)
    _swap_focus(box, personal_feeds=False)
    await ob.flush(NOON + 2)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "开关" in row["error"]


async def test_personal_preflight_drops_when_idea_dismissed_while_queued(tmp_path):
    """排队期间这条构想被划掉了 → 不发陈旧的话。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    iid = _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 1)
    await im.flush(GID, NOON + 1)
    with store.tx() as conn:
        conn.execute("UPDATE ideas SET state='dismissed' WHERE id=?", (iid,))
    await ob.flush(NOON + 2)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "构想" in row["error"]


async def test_personal_preflight_drops_legacy_pending_without_guard(tmp_path):
    """升级前就排在发件箱里的个人提一嘴（老载荷没有复核材料）：宁可少发一条。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    iid = _idea(store, target=UID, created=NOON)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO idea_mentions (group_id, idea_id, status, text, at_user, created, due_ts)"
            " VALUES (?, ?, 'queued', '老版本排的话', ?, ?, ?)",
            (GID, iid, UID, NOON, NOON),
        )
    ob.enqueue("idea_mention:1", GID, "text",
               {"text": "老版本排的话", "push_kind": "idea_mention", "at_user": UID,
                "at_name": "群友", "expires_ts": NOON + 3600})
    await ob.flush(NOON + 10)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "复核材料" in row["error"]


async def test_personal_preflight_drops_when_evidence_gone(tmp_path):
    """依据的那条原话没了（库被清理 / 对不上）→ 作废，不照旧决定硬发。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 1)
    await im.flush(GID, NOON + 1)
    await ob.flush(NOON + 2)
    assert _rows(store)[0]["status"] == "sent"
    later = NOON + 4 * 86400.0
    _chat(store, "那个追更表你帮我弄一下吧", ts=later - 120, mid="fresh-1")
    _idea(store, target=UID, created=later - 60, title="我可以帮你把这些表再理一遍")
    assert im.scan(GID, later) == 1
    models.replies = [
        _guard(need=True, evidence=["fresh-1"]),
        json.dumps({"text": "我可以帮你把追更表理一遍，要不要这两天弄？"}, ensure_ascii=False),
    ]
    await im.flush(GID, later)
    assert _rows(store)[-1]["status"] == "queued"
    with store.tx() as conn:
        conn.execute("DELETE FROM chat_log WHERE message_id=?", ("fresh-1",))
    await ob.flush(later + 1)
    assert len(host.texts) == 1                     # 只有第一次那条发出去
    row = _rows(store)[-1]
    assert row["status"] == "dropped" and "依据" in row["error"]


async def test_personal_preflight_drops_when_target_talks_after_review(tmp_path):
    """复核之后他本人又说话了：复核过期，作废（不拿旧依据硬发）；发件箱里不叫模型。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    assert _boxes(store)[0]["status"] == "pending"
    _chat(store, "算了先不用了", uid=UID, ts=NOON + 20, mid="newer-1")
    before = len(models.calls)
    await ob.flush(NOON + 30)
    assert host.texts == []
    assert len(models.calls) == before            # 发送前复核是纯代码，不在发件箱锁里叫模型
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "材料又多了" in row["error"]


async def test_personal_preflight_drops_when_task_appears_while_queued(tmp_path):
    """排队期间他这边落了新任务：复核看不到这条材料，作废（不拿旧复核继续邀约）。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    assert _boxes(store)[0]["status"] == "pending"
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, requester_id, title, status, created,"
            " updated) VALUES ('T-x', ?, 'test', ?, '番剧追更表', 'completed', ?, ?)",
            (GID, UID, NOON + 15, NOON + 15),
        )
    await ob.flush(NOON + 20)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "材料又多了" in row["error"]


async def test_personal_preflight_drops_when_chat_arrives_late(tmp_path):
    """补读入库的发言（时间戳早于复核、入库在复核之后）：材料变多，旧复核作废。

    这正是「不能只比时间戳」的边界：`ts` 比 checked_ts 早，但复核当时没看到它。
    """
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID, created=NOON)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    assert _boxes(store)[0]["status"] == "pending"
    _chat(store, "追更表我已经做完了不用再做", ts=NOON + 5, mid="late-solved")   # 早于复核时刻
    await ob.flush(NOON + 20)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "dropped" and "材料又多了" in row["error"]


async def test_personal_outbox_refuses_unguarded_payload_without_producer(tmp_path):
    """生产者没挂上（重启 / 构造失败）时，发件箱自己也不放行没有复核材料的个人提一嘴。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    _idea(store, target=UID, created=NOON)
    im.scan(GID, NOON + 1)
    mid = int(_rows(store)[0]["id"])
    unguarded = Outbox(store, host, im._pushes, im._mentions, im._get_settings)
    unguarded.enqueue(f"idea_mention:{mid}", GID, "text",
                      {"text": "旧的提议", "push_kind": "idea_mention", "at_user": UID,
                       "expires_ts": NOON + 3600})
    await unguarded.flush(NOON + 2)
    assert host.texts == []
    assert _boxes(store)[0]["status"] == "dropped"
    assert "复核材料" in str(_boxes(store)[0]["error"])


async def test_group_ideas_are_not_subject_to_personal_gates(tmp_path):
    """群向构想（没指定人）不受个人向的「每人 1 条 / 每群 3 条」约束。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    for i in range(5):
        _idea(store, created=NOON + i, title=f"我可以给全群做第 {i} 份周报")
    assert im.scan(GID, NOON + 10) == 5


async def test_group_idea_mentioned_once_with_link(tmp_path):
    """只入队（不标 sent），发件箱真发出去之后才算提过一次 + 留痕。"""
    store, host, models, pushes, im, ob = _make(tmp_path)
    _enable(store)
    iid = _idea(store)
    assert im.scan(GID, NOON + 10) == 1
    assert im.scan(GID, NOON + 11) == 0
    await im.flush(GID, NOON + 10)
    await im.flush(GID, NOON + 20)
    assert host.texts == []
    row = _rows(store)[0]
    assert row["status"] == "queued" and row["sent_ts"] is None
    assert "做个番剧追更表" in row["text"]      # 写好话落库，等发件箱发
    box = _boxes(store)[0]
    assert box["status"] == "pending" and str(box["key"]) == f"idea_mention:{int(row['id'])}"
    await ob.flush(NOON + 30)
    assert len(host.texts) == 1
    t = host.texts[0]
    assert t["at_user"] == ""
    assert "做个番剧追更表" in t["text"]
    assert f"https://mw.example/#/tok{GID}/ideas/I-{iid}" in t["text"]
    row = _rows(store)[0]
    assert row["status"] == "sent" and row["message_id"]
    n = store.read().execute("SELECT COUNT(*) c FROM pushes WHERE kind='idea_mention'").fetchone()["c"]
    assert n == 1


async def test_prompt_never_gets_basis(tmp_path):
    """basis 里是「为什么适合（引画像）」——根本不给模型，从源头防泄露（复核那一次也不给）。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    every = json.dumps(models.calls, ensure_ascii=False)
    assert "考研" not in every
    assert UID not in every
    write = json.dumps(_write_calls(models)[0]["messages"], ensure_ascii=False)
    assert "画像" in write  # 规则里写了「不许提画像」


async def test_personal_idea_ats_member(tmp_path):
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[_guard(need=True, evidence=["need-1"])])
    _enable(store)
    _idea(store, target=UID)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    payload = json.loads(_boxes(store)[0]["payload"])
    assert payload["at_user"] == UID and UID not in payload["text"]
    await ob.flush(NOON + 11)
    assert host.texts[0]["at_user"] == UID
    assert UID not in host.texts[0]["text"]


@pytest.mark.parametrize("bad", [
    "根据你的画像，你可能会喜欢这个",
    "我注意到你最近在忙考研，要不要试试",
    "看你平时经常聊番剧，来试试",
    f"@{UID} 来看看",
])
async def test_leaky_text_replaced_by_safe_template(tmp_path, bad):
    store, host, models, _p, im, ob = _make(
        tmp_path,
        replies=[_guard(need=True, evidence=["need-1"]),
                 json.dumps({"text": bad}, ensure_ascii=False)],
    )
    _enable(store)
    _idea(store, target=UID)
    _chat(store, "那个追更表能帮我做吗", ts=NOON - 10, mid="need-1")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    text = host.texts[0]["text"]
    for w in ("画像", "注意到", "平时", UID):
        assert w not in text
    assert "番剧追更表" in text  # 模板用标题


async def test_model_failure_uses_template(tmp_path):
    store, host, models, _p, im, ob = _make(tmp_path, replies=[ModelError("down")])
    _enable(store)
    _idea(store)
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    assert len(host.texts) == 1 and "番剧追更表" in host.texts[0]["text"]


async def test_sleep_hours_no_prepare_no_model(tmp_path):
    """睡觉时段不备料不发（连模型都不叫）；醒来后备料入队再发。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store, now=SLEEP - 7200)
    _idea(store, created=SLEEP - 60)
    im.scan(GID, SLEEP)
    await im.flush(GID, SLEEP)
    await ob.flush(SLEEP)
    assert host.texts == [] and models.calls == []      # 睡觉时段连模型都不叫
    assert _boxes(store) == []                          # 也还没备料入队
    wake = _ts(8, 5, day=16)
    await im.flush(GID, wake)
    await ob.flush(wake)
    assert len(host.texts) == 1
    assert [r["status"] for r in _rows(store)] == ["sent"]


async def test_shared_cap_defers_extra_idea_instead_of_dropping(tmp_path):
    """每日总上限（1）：第二条留到次日额度回来（还在 12 小时窗口里）再发。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    afternoon = _ts(13, 0, day=16)
    _enable(store, now=afternoon - 600)
    group_push.set_config(store, GID, {"daily_max": 1, "quiet_hours": "00:00-00:00"})
    _idea(store, created=afternoon - 60)
    _idea(store, created=afternoon - 30, title="我可以做个群聊周报")
    im.scan(GID, afternoon)
    await im.flush(GID, afternoon)
    await ob.flush(afternoon + 60)
    assert len(host.texts) == 1
    assert [r["status"] for r in _rows(store)] == ["sent", "queued"]
    second = _boxes(store)[1]
    assert second["status"] == "pending" and float(second["not_before"]) > afternoon
    # 次日凌晨额度回来（离构想出来还没到 12 小时）：第二条接着发
    await im.flush(GID, _ts(0, 30, day=17))
    await ob.flush(_ts(0, 35, day=17))
    assert len(host.texts) == 2
    assert [r["status"] for r in _rows(store)] == ["sent", "sent"]


async def test_queued_idea_past_window_dropped_instead_of_sent_next_day(tmp_path):
    """已入队（queued）的提一嘴被推到 12 小时窗口之外 → 发件箱作废，不发陈旧的话。"""
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store, now=NOON - 600)
    group_push.set_config(store, GID, {"daily_max": 1})
    _idea(store, created=NOON - 60)
    _idea(store, created=NOON - 30, title="我可以做个群聊周报")
    im.scan(GID, NOON)
    await im.flush(GID, NOON)
    await ob.flush(NOON + 60)
    assert len(host.texts) == 1
    # 第二天早上额度回来了，但第二条已经过了 12 小时 → 作废，不补发
    await ob.flush(_ts(9, 0, day=16))
    assert len(host.texts) == 1
    assert [r["status"] for r in _rows(store)] == ["sent", "dropped"]
    assert "有效期限" in str(_rows(store)[1]["error"])


async def test_dismissed_idea_not_mentioned(tmp_path):
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    iid = _idea(store)
    im.scan(GID, NOON + 10)
    with store.tx() as conn:
        conn.execute("UPDATE ideas SET state='dismissed' WHERE id=?", (iid,))
    await im.flush(GID, NOON + 10)
    assert host.texts == [] and _boxes(store) == []
    assert _rows(store)[0]["status"] == "dropped"


async def test_ideas_before_enable_ignored(tmp_path):
    store, host, models, _p, im, ob = _make(tmp_path)
    _idea(store, created=NOON - 7200)
    _enable(store, now=NOON - 3600)
    assert im.scan(GID, NOON) == 0


async def test_has_due_and_status(tmp_path):
    store, host, models, _p, im, ob = _make(tmp_path)
    _enable(store)
    _idea(store)
    im.scan(GID, NOON + 10)
    assert im.has_due(GID, NOON + 10) is True
    await im.flush(GID, NOON + 10)
    assert im.has_due(GID, NOON + 20) is False
    st = im.status(GID, now=NOON + 20)
    assert st["sent_today"] == 0 and st["recent"][0]["status"] == "queued"
    await ob.flush(NOON + 21)
    st = im.status(GID, now=NOON + 22)
    assert st["sent_today"] == 1 and st["recent"][0]["status"] == "sent"


# ----------------------------------------------------------------------
# 关心式问法 + 由头（2026-10 与用户定）
# ----------------------------------------------------------------------


async def test_prompt_carries_persona_and_origin(tmp_path):
    """提示词只按 SOUL 说话（不读 MaiBot 人格、不拿它的发言当样例），并带上由头。"""
    store, host, models, _p, im, ob = _make(
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
    """模型写出的「有具体事 + 问句」照用（不再被当成推销腔换掉）。"""
    good = json.dumps(
        {"text": "我可以帮你们做个涂击队百层挑战进度表，要不要我来弄？"},
        ensure_ascii=False,
    )
    store, host, models, _p, im, ob = _make(tmp_path, replies=[good])
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    first_line = host.texts[0]["text"].split("\n")[0]
    assert first_line == "我可以帮你们做个涂击队百层挑战进度表，要不要我来弄？"


@pytest.mark.parametrize("bad", [
    "我可以帮你们搓一份极乐迪斯科防翻车手册，感兴趣的话点进去看看",
    "给大家带来一个好东西，安利一下",
    "推荐给大家一个小工具，点进去看看",
])
async def test_pitch_talk_replaced_by_template(tmp_path, bad):
    """广告话术（给大家带来 / 推荐给大家 / 安利 / 感兴趣的话 / 点进去看看）→ 换具体兜底。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[json.dumps({"text": bad}, ensure_ascii=False)]
    )
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    text = host.texts[0]["text"]
    for w in card_push._PITCH_WORDS:
        assert w not in text, w
    assert "做个番剧追更表" in text
    assert "要不要我来弄" in text


@pytest.mark.parametrize("bad", [
    "我是小麦，话说之前那个涂击队百层挑战后来怎么样了？",
    "大家好，之前那个涂击队百层挑战还在搞吗？",
    "作为群助手，想问问百层挑战后来怎么样了？",
])
async def test_self_intro_replaced_by_template(tmp_path, bad):
    """自我介绍 / 寒暄（2026-10-01 用户定：不要自我介绍和废话）→ 换模板。"""
    store, host, models, _p, im, ob = _make(
        tmp_path, replies=[json.dumps({"text": bad}, ensure_ascii=False)]
    )
    _enable(store)
    _idea(store, origin="涂击队百层挑战")
    im.scan(GID, NOON + 10)
    await im.flush(GID, NOON + 10)
    await ob.flush(NOON + 11)
    first_line = host.texts[0]["text"].split("\n")[0]
    assert first_line == "我可以帮群里做个番剧追更表，要不要我来弄？"

