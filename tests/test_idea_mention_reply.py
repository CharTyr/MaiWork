"""引用回复「构想提一嘴」直接转派活（design §2.1 子项1，2026-10-31）。

契约：
- `Intake._IdeaMentionIndex`：`(group_id, str(message_id)) -> {idea_id, title, target_user_id, items}`，
  钩子只读内存表；由 outbox 发送成功的 `on_delivery_result` 增量登记 + `refresh_reply_indexes` 周期预热（R8）。
- `_m3` 里这条分支在 `_handle_at` 之前（QQ 引用机器人消息会自动带 @）：reply_to 命中 index →
  后台 `_handle_idea_mention_reply`。
- 隐私门禁：个人向构想（target_user_id 非空）只有本人或本群/全局管理员能触发，
  别人回复 → 静默忽略（不在群里点破）。
- 后台：显式拒绝词（算了/不用/别做）→ 退出；短肯定句或 Jev 判「采纳执行」→
  `parse_idea_wanted` 解析子条目序号 → 复用 `_create_idea_request`；闲聊评价 →
  不建单，写 30 秒纯内存备忘让 MaiBot 顺聊。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "900000001"


def _settings(serve=(GID,)):
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
            "jev": {"timeout_ms": 1500},
        }
    )
    return settings


class _FakeJev:
    def __init__(self, answers=None, *, available: bool = True) -> None:
        self.answers = answers
        self._available = available
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    async def ask(self, state, questions, *, purpose: str, group_id: str = "", timeout_ms=None):
        self.calls.append(
            {"state": state, "questions": questions, "purpose": purpose, "group_id": group_id}
        )
        return self.answers


class _FakeApprovals:
    def __init__(self, *, admins: set[str] | None = None) -> None:
        self.created: list[tuple[str, dict]] = []
        self._admins = admins or set()

    def create(self, group_id: str, **kw):
        self.created.append((str(group_id), dict(kw)))
        return {"id": "R-9", "status": "pending", "auto": False, "task_id": None, "goal_id": None}

    def is_admin(self, user_id, platform: str = "qq", *, group_id: str | None = None) -> bool:
        return str(user_id) in self._admins


class _FakeMentions:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.ephemeral: list[dict] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5,
            target_user_id: str = "", keywords="") -> None:
        self.items.append(
            {"group_id": str(group_id), "text": str(text), "key": str(key), "ttl_s": float(ttl_s)}
        )

    def add_ephemeral(self, group_id: str, text: str, *, key: str, ttl_s: float = 15.0,
                      turns: int = 1, target_user_id: str = "", keywords="") -> None:
        self.ephemeral.append(
            {
                "group_id": str(group_id), "text": str(text), "key": str(key),
                "ttl_s": float(ttl_s), "turns": int(turns),
                "target_user_id": str(target_user_id),
            }
        )


def _seed_idea_and_mention(
    store: Store,
    intake: Intake | None = None,
    *,
    gid: str = GID,
    mention_mid: str = "im-1",
    target_user_id: str = "",
    idea_state: str = "new",
    mention_status: str = "sent",
    items: list[dict] | None = None,
) -> int:
    """种一条构想 + 一条「构想提一嘴」发出记录（idea_mentions）。返回构想 id。

    接收钩子只读内存索引（R8）；传 intake 时按真实链路（outbox 发送成功 →
    on_delivery_result）把刚种的提一嘴登记进索引。"""
    items_json = json.dumps(
        items
        or [
            {"kind": "task", "title": "查清故障原因", "desc": "先出一版"},
            {"kind": "task", "title": "整理复活步骤", "desc": ""},
        ],
        ensure_ascii=False,
    )
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
            " requested_by, task_id, up, down, created, updated, items, target_user_id)"
            " VALUES (?, 'bulb', '把手柄救活', 'b', 's', 'st', 'e', ?, NULL, NULL, 0, 0, ?, ?, ?, ?)",
            (gid, idea_state, NOW - 3600, NOW - 3600, items_json, target_user_id),
        )
        iid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO idea_mentions (group_id, idea_id, status, text, at_user, created, due_ts,"
            " sent_ts, attempts, message_id, error)"
            " VALUES (?, ?, ?, '提一嘴', '', ?, ?, ?, 0, ?, '')",
            (gid, iid, mention_status, NOW - 3000, NOW - 3000, NOW - 3000, mention_mid),
        )
    if intake is not None and mention_mid and mention_status == "sent":
        mid_row = store.read().execute(
            "SELECT id FROM idea_mentions WHERE group_id=? AND message_id=?", (gid, mention_mid)
        ).fetchone()
        if mid_row is not None:
            intake.on_delivery_result({
                "outcome": "sent", "group_id": gid,
                "key": f"idea_mention:{int(mid_row['id'])}",
                "result": {"message_id": mention_mid}, "ts": NOW,
            })
    return iid


def _make(tmp_path, *, jev=None, approvals=None, mentions=None, on_spark=None):
    store = Store(tmp_path / "i.db")
    store.migrate()
    spawned: list = []
    intake = Intake(
        lambda: _settings(),
        Signals(),
        jev=jev,
        approvals=approvals if approvals is not None else _FakeApprovals(),
        mentions=mentions,
        store=store,
        spawn=lambda c: spawned.append(c),
        on_spark=on_spark,
    )
    return intake, store, spawned


async def _drain(spawned: list) -> None:
    while spawned:
        await spawned.pop(0)


def _reply_msg(*, text: str, reply_to: str, user_id: str = "20002", mid: str = "r-1", is_at: bool = True):
    msg = hook_message(group_id=GID, user_id=user_id, text=text, message_id=mid, is_at=is_at)
    msg["message"]["reply_to"] = reply_to
    return msg


# ----------------------------------------------------------------------
# 命中 → 建请求
# ----------------------------------------------------------------------


class TestIdeaMentionReplyCreate:
    @pytest.mark.asyncio
    async def test_affirmative_reply_creates_request(self, tmp_path) -> None:
        """群友引用提一嘴回「做」→ 后台按这条构想建待批请求（idea_id 对上）。"""
        approvals = _FakeApprovals()
        intake, store, spawned = _make(tmp_path, approvals=approvals)
        iid = _seed_idea_and_mention(store, intake)
        out = await intake.handle(_reply_msg(text="做", reply_to="im-1"))
        assert out == {"action": "continue"}
        await _drain(spawned)
        assert len(approvals.created) == 1
        kw = approvals.created[0][1]
        assert kw["idea_id"] == iid
        assert kw["requester_id"] == "20002"

    @pytest.mark.asyncio
    async def test_reply_does_not_fall_through_to_at_path(self, tmp_path) -> None:
        """引用回复带 @ 也不能再走 _handle_at（否则会被当成普通派活判一遍）。"""
        jev = _FakeJev({"kind": ("none", 0.9, 0.9)})
        approvals = _FakeApprovals()
        intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals)
        _seed_idea_and_mention(store, intake)
        await intake.handle(_reply_msg(text="做", reply_to="im-1", is_at=True))
        await _drain(spawned)
        # 只建了一次（构想路径），Jev 的 @ 判词一次都没问
        assert len(approvals.created) == 1
        assert approvals.created[0][1].get("idea_id") is not None
        assert all(c["purpose"] != "intake" for c in jev.calls)

    @pytest.mark.asyncio
    async def test_item_no_parsed_from_reply(self, tmp_path) -> None:
        """「做第 2 个」→ 只挑子条目 2（wanted=[2]）。"""
        approvals = _FakeApprovals()
        intake, store, spawned = _make(tmp_path, approvals=approvals)
        _seed_idea_and_mention(store, intake)
        await intake.handle(_reply_msg(text="做第 2 个", reply_to="im-1"))
        await _drain(spawned)
        assert len(approvals.created) == 1
        assert approvals.created[0][1].get("items") == [2]

    @pytest.mark.asyncio
    async def test_pending_idea_not_duplicated(self, tmp_path) -> None:
        """构想已经在等批准（state=pending）：不建第二个请求，只回一句备忘。"""
        approvals = _FakeApprovals()
        mentions = _FakeMentions()
        intake, store, spawned = _make(tmp_path, approvals=approvals, mentions=mentions)
        _seed_idea_and_mention(store, intake, idea_state="pending")
        await intake.handle(_reply_msg(text="做", reply_to="im-1"))
        await _drain(spawned)
        assert approvals.created == []
        assert any("已经在等管理员批准" in it["text"] or "不用再建" in it["text"] for it in mentions.items)


# ----------------------------------------------------------------------
# 隐私门禁（个人向构想）
# ----------------------------------------------------------------------


class TestIdeaMentionReplyPrivacy:
    @pytest.mark.asyncio
    async def test_other_user_reply_to_personal_idea_ignored(self, tmp_path) -> None:
        """个人向构想（target_user_id=10001）：非本人非管理员回复 → 静默忽略。"""
        approvals = _FakeApprovals()
        mentions = _FakeMentions()
        jev = _FakeJev({"kind": ("prepare", 0.9, 0.9)})
        intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals, mentions=mentions)
        _seed_idea_and_mention(store, intake, target_user_id="10001")
        # 20002 不是本人、也不是管理员
        await intake.handle(_reply_msg(text="做", reply_to="im-1", user_id="20002"))
        await _drain(spawned)
        assert approvals.created == []
        # 不留任何会点破的痕迹（不提构想名、不问 Jev）
        assert mentions.items == [] and mentions.ephemeral == []
        assert jev.calls == []

    @pytest.mark.asyncio
    async def test_owner_reply_to_personal_idea_works(self, tmp_path) -> None:
        """本人回复自己的个人向构想 → 正常建请求。"""
        approvals = _FakeApprovals()
        intake, store, spawned = _make(tmp_path, approvals=approvals)
        iid = _seed_idea_and_mention(store, intake, target_user_id="10001")
        await intake.handle(_reply_msg(text="做", reply_to="im-1", user_id="10001"))
        await _drain(spawned)
        assert len(approvals.created) == 1
        assert approvals.created[0][1]["idea_id"] == iid

    @pytest.mark.asyncio
    async def test_group_admin_reply_to_personal_idea_works(self, tmp_path) -> None:
        """本群管理员回复别人的个人向构想 → 放行。"""
        approvals = _FakeApprovals(admins={"30003"})
        intake, store, spawned = _make(tmp_path, approvals=approvals)
        iid = _seed_idea_and_mention(store, intake, target_user_id="10001")
        await intake.handle(_reply_msg(text="做", reply_to="im-1", user_id="30003"))
        await _drain(spawned)
        assert len(approvals.created) == 1
        assert approvals.created[0][1]["idea_id"] == iid


# ----------------------------------------------------------------------
# 拒绝 / 闲聊
# ----------------------------------------------------------------------


class TestIdeaMentionReplyRefuseOrChat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["算了", "不用了", "别做了"])
    async def test_reject_words_do_nothing(self, tmp_path, text: str) -> None:
        approvals = _FakeApprovals()
        mentions = _FakeMentions()
        jev = _FakeJev({"kind": ("yes", 0.9, 0.9)})
        intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals, mentions=mentions)
        _seed_idea_and_mention(store, intake)
        await intake.handle(_reply_msg(text=text, reply_to="im-1"))
        await _drain(spawned)
        assert approvals.created == []
        assert jev.calls == []  # 拒绝词直接退出，不问 Jev

    @pytest.mark.asyncio
    async def test_chitchat_writes_30s_memo_no_request(self, tmp_path) -> None:
        """「哈哈有意思」：不建单，塞一条 30 秒纯内存备忘让 MaiBot 顺聊。"""
        approvals = _FakeApprovals()
        mentions = _FakeMentions()
        jev = _FakeJev({"verdict": ("chat", 0.9, 0.9)})
        intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals, mentions=mentions)
        _seed_idea_and_mention(store, intake)
        await intake.handle(_reply_msg(text="哈哈有意思", reply_to="im-1"))
        await _drain(spawned)
        assert approvals.created == []
        assert len(mentions.ephemeral) == 1
        eph = mentions.ephemeral[0]
        assert eph["ttl_s"] == pytest.approx(30.0)
        assert eph["target_user_id"] == "20002"

    @pytest.mark.asyncio
    async def test_jev_confirmed_intent_creates_request(self, tmp_path) -> None:
        """不是短肯定、Jev 判「是采纳执行」→ 建请求。"""
        approvals = _FakeApprovals()
        jev = _FakeJev({"verdict": ("yes", 0.9, 0.9)})
        intake, store, spawned = _make(tmp_path, jev=jev, approvals=approvals)
        iid = _seed_idea_and_mention(store, intake)
        await intake.handle(_reply_msg(text="这个方案听起来靠谱，就按这个来吧", reply_to="im-1"))
        await _drain(spawned)
        assert len(approvals.created) == 1
        assert approvals.created[0][1]["idea_id"] == iid

    @pytest.mark.asyncio
    async def test_jev_unavailable_ambiguous_text_is_chat(self, tmp_path) -> None:
        """Jev 不在、又不是短肯定句 → 当闲聊处理（不建单，只留备忘）。"""
        approvals = _FakeApprovals()
        mentions = _FakeMentions()
        intake, store, spawned = _make(tmp_path, jev=None, approvals=approvals, mentions=mentions)
        _seed_idea_and_mention(store, intake)
        await intake.handle(_reply_msg(text="这个方案听起来靠谱，就按这个来吧", reply_to="im-1"))
        await _drain(spawned)
        assert approvals.created == []
        assert len(mentions.ephemeral) == 1


# ----------------------------------------------------------------------
# 索引本身（_IdeaMentionIndex）
# ----------------------------------------------------------------------


class TestIdeaMentionIndex:
    """索引的 DB→内存预热（refresh）：R8 后 lookup 只读内存、钩子不查库。"""

    def test_lookup_only_sent_with_mid(self, tmp_path) -> None:
        """只有 status='sent' 且 message_id 非空的行会被命中。"""
        intake, store, _ = _make(tmp_path)
        _seed_idea_and_mention(store, mention_mid="im-1", mention_status="sent")
        _seed_idea_and_mention(store, mention_mid="", mention_status="sent")
        _seed_idea_and_mention(store, mention_mid="im-x", mention_status="pending")
        intake._idea_mention_index.refresh(store, GID, NOW)
        hit = intake._idea_mention_index.lookup(store, GID, "im-1", clock.now())
        assert hit is not None and hit["idea_id"] is not None
        assert intake._idea_mention_index.lookup(store, GID, "", clock.now()) is None
        assert intake._idea_mention_index.lookup(store, GID, "im-x", clock.now()) is None

    def test_lookup_hit_carries_fields(self, tmp_path) -> None:
        intake, store, _ = _make(tmp_path)
        iid = _seed_idea_and_mention(store, target_user_id="10001")
        intake._idea_mention_index.refresh(store, GID, NOW)
        hit = intake._idea_mention_index.lookup(store, GID, "im-1", clock.now())
        assert hit == {
            "idea_id": iid,
            "title": "把手柄救活",
            "target_user_id": "10001",
            "items": [
                {"kind": "task", "title": "查清故障原因", "desc": "先出一版"},
                {"kind": "task", "title": "整理复活步骤", "desc": ""},
            ],
        }

    def test_lookup_other_group_misses(self, tmp_path) -> None:
        intake, store, _ = _make(tmp_path)
        _seed_idea_and_mention(store)
        intake._idea_mention_index.refresh(store, GID, NOW)
        assert intake._idea_mention_index.lookup(store, "222", "im-1", clock.now()) is None
