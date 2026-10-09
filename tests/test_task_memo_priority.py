"""派活 / 任务状态类备忘的优先级与措辞（2026-10 线上实测整改）。

背景（线上 10-09 只读排查）：
1) 群友 @ 机器人派活时，`delivery.Mentions._memo_with_topics` 先放「话题接龙」行、
   再放其它备忘行，共用 300 字总上限。两条接龙（每条 130~160 字）吃光预算，
   派活那行被判「放不下」整行跳过，MaiBot 那一轮根本没看到。
2) 那句备忘（intake.py `_create_request`）措辞太软：「<名字>刚才请你……，
   MaiWork 已经记下，等管理员批准后开工」，没说「你别自己做」。MaiBot 被 @ 必回，
   于是自己答应、自己写脚本、重复干。

本文件守住的规则（方案 1，只改备忘）：
- 派活 / 任务状态行（key 前缀 request: / idea-busy:）排在接龙行之前、优先分配预算；
  单条太长按句截断，不整行丢；预算耗尽就停止（备忘条目多时后面的行这一轮可能整条
  不进上下文，不保证每条每轮都完整）。
- 话题接龙行仍按原规则：派活行之后、预算不够就少放。
- 派活备忘改成明确指令（不用答应 / 不要自己做），只写发起人显示名，不带画像、不带内部 id。
- 总上限仍是 300（宿主提示预算），不新增群消息、钩子返回不改。

备忘本身还有两个边界（不属于本文件断言、但不要读成「一定注入」）：每条备忘默认
5 轮，turns_left 用完或过期（request 备忘 ttl 30 分钟）后就不再进上下文。
"""

from __future__ import annotations

import copy
import json

import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import HEADER, Mentions
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"
G1 = "900000001"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """冻住时钟：候选 3 天窗口、备忘有效期、turns 判定都可复现。"""
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


# ----------------------------------------------------------------------
# delivery 侧：派活行优先级
# ----------------------------------------------------------------------


def _settings(serve=(GID,)) -> object:
    merged = {"groups": {"serve": [{"group": f"qq:{g}"} for g in serve]}}
    settings, _ = load_settings(merged)
    return settings


def _make_mentions(tmp_path, serve=(GID,)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(serve)
    return store, settings, Mentions(store, lambda: settings)


def _seed_group(store: Store, gid: str = GID, session_id: str = "sess-1") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id) VALUES (?, ?)",
            (gid, session_id),
        )


def _seed_news(
    store: Store,
    *,
    gid: str = GID,
    title: str,
    keywords,
    body: str = "文" * 60,
    created: float | None = None,
    url: str = "",
) -> int:
    with store.tx() as conn:
        conn.execute("INSERT INTO news_batches (group_id, slot_ts, created) VALUES (?, 0, ?)", (gid, NOW))
        bid = int(conn.execute("SELECT MAX(id) AS m FROM news_batches").fetchone()["m"])
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, body, keywords,"
            " sources, rejected, kind, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'news', ?)",
            (
                bid, gid, title, "摘要", body,
                json.dumps(list(keywords), ensure_ascii=False),
                json.dumps([{"url": url}]) if url else "[]",
                NOW if created is None else float(created),
            ),
        )
        return int(cur.lastrowid)


def _msg_part(text: str) -> dict:
    attrs = ' msg_id="m1" user_name="群友"'
    return {
        "item_type": "UserMessageItem",
        "meta": {},
        "parts": [{"type": "text", "text": f"<message{attrs}>{text}</message>"}],
    }


def _kwargs(chat: list[str], session_id: str = "sess-1") -> dict:
    items = [
        {
            "item_type": "SystemMessageItem",
            "meta": {},
            "parts": [{"type": "text", "text": "系统指令"}],
        }
    ]
    for text in chat:
        items.append(_msg_part(text))
    return {
        "item_schema_version": 1,
        "tool_definitions": [],
        "session_id": session_id,
        "items": items,
    }


def _memo_of(out: dict) -> str:
    text = out["items"][0]["parts"][0]["text"]
    assert "【MaiWork 备忘】" in text
    return text[text.index("【MaiWork 备忘】"):]


def test_two_long_topic_lines_do_not_push_out_assign_memo(tmp_path):
    """两条长话题接龙 + 一条派活备忘：本用例的预算下派活行在、且排在接龙行之前，总长 ≤300。

    真实尺寸（线上 10-09）：接龙行 ≈130~160 字，两条就吃光 300 预算，
    旧代码把派活行整行跳过 → MaiBot 那轮看不到。
    """
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    # 两条接龙行：33（固定前缀）+ 标题 + 60（正文片段）= 120 / 140 字
    _seed_news(store, title="话题甲" + "甲" * 24, keywords=("m7", "芯片"), created=NOW)
    _seed_news(store, title="话题乙" + "乙" * 44, keywords=("m8", "屏幕"), created=NOW - 10)
    # 派活行（intake 写的 key 前缀 request:）
    m.add(
        GID,
        "阿柒 刚才 @ 你派的活已由 MaiWork 接手（等管理员批准）。"
        "你不用答应，不要自己做、不要说你来做或给出成品；"
        "可以只简短说一句「好，交给它了」，也可以不回。",
        key="request:R-1",
        ttl_s=30 * 60,
    )
    out = m.inject(copy.deepcopy(_kwargs(["m7 芯片 怎么样", "m8 屏幕 呢"])))
    assert out is not None
    memo = _memo_of(out)
    assert "阿柒" in memo, "派活行被话题接龙挤掉了"
    assert "话题甲" in memo, "接龙行仍应在派活行之后留一条"
    assert memo.index("阿柒") < memo.index("话题甲"), "派活行必须排在话题接龙行之前"
    assert len(memo) <= 300


def test_two_assign_memos_both_appear(tmp_path):
    """两条派活 / 状态行：本用例预算下两条都出现（各截到约一半预算；预算更少时会截得更短、后面的整条进不来）。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "阿柒 的派活甲" + "甲" * 200, key="request:R-1", ttl_s=30 * 60)
    m.add(GID, "阿柒 的派活乙" + "乙" * 200, key="request:R-2", ttl_s=30 * 60)
    out = m.inject(copy.deepcopy(_kwargs([])))
    assert out is not None
    memo = _memo_of(out)
    assert "派活甲" in memo and "派活乙" in memo
    assert len(memo) <= 300


def test_overlong_assign_line_truncated_not_dropped(tmp_path):
    """单条派活行自身超长 → 截断但仍出现，不整行丢。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "阿柒" + "派活说明" * 80, key="request:R-9", ttl_s=30 * 60)
    out = m.inject(copy.deepcopy(_kwargs([])))
    assert out is not None, "超长派活行被整行丢掉了（旧行为）"
    memo = _memo_of(out)
    assert "阿柒" in memo
    assert len(memo) <= 300
    # 真的截断过（不是原样塞进去）
    assert "派活说明" * 80 not in memo


def test_overlong_assign_line_cut_at_sentence_end(tmp_path):
    """按句截断：句末标点落在预算内时在标点处断，不硬切半句话。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    m.add(GID, "阿柒" + "甲" * 197 + "。" + "乙" * 200, key="request:R-3", ttl_s=30 * 60)
    out = m.inject(copy.deepcopy(_kwargs([])))
    assert out is not None
    memo = _memo_of(out)
    line = [ln for ln in memo.splitlines() if ln.startswith("- ")][0]
    assert line.endswith("。")
    assert len(memo) <= 300


def test_idea_busy_status_line_is_priority(tmp_path):
    """同类任务状态行（构想已在等批准 / 已在做）也排在接龙行之前（本用例预算下仍进得来；预算更少时会截短或整条进不来）。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    _seed_news(store, title="话题甲" + "甲" * 24, keywords=("m7", "芯片"), created=NOW)
    _seed_news(store, title="话题乙" + "乙" * 44, keywords=("m8", "屏幕"), created=NOW - 10)
    m.add(GID, "阿柒提的构想「做个抽签」已经在做了，不用再建", key="idea-busy:1:m-1", ttl_s=30 * 60)
    out = m.inject(copy.deepcopy(_kwargs(["m7 芯片 怎么样", "m8 屏幕 呢"])))
    assert out is not None
    memo = _memo_of(out)
    assert "不用再建" in memo
    assert len(memo) <= 300


def test_no_assign_line_topic_before_memo_unchanged(tmp_path):
    """回归：没有派活行时，接龙行仍在普通备忘行之前（旧行为不变）。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    _seed_news(store, title="话题甲", keywords=("m7", "芯片"), created=NOW)
    m.add(GID, "普通备忘一条", key="news:1", ttl_s=30 * 60)
    out = m.inject(copy.deepcopy(_kwargs(["m7 芯片 怎么样"])))
    assert out is not None
    memo = _memo_of(out)
    assert "话题甲" in memo and "普通备忘一条" in memo
    assert memo.index("话题甲") < memo.index("普通备忘一条")


def test_turn_decrement_covers_priority_line(tmp_path):
    """派活行真的注入了 → turns_left 照常递减；放不下没进备忘的行不动。"""
    store, settings, m = _make_mentions(tmp_path)
    _seed_group(store)
    with store.tx() as conn:
        # 普通行更新（旧代码先处理它）：260 字，正好把旧预算吃光
        conn.execute(
            "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GID, "news:9", "普通备忘" + "备" * 254, NOW + 3600, 2, NOW + 10),
        )
        # 派活行
        conn.execute(
            "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (GID, "request:R-1", "阿柒 的派活", NOW + 3600, 2, NOW + 5),
        )
    out = m.inject(copy.deepcopy(_kwargs([])))
    assert out is not None
    rows = store.read().execute("SELECT key, turns_left FROM mentions ORDER BY key").fetchall()
    assert [(r["key"], r["turns_left"]) for r in rows] == [("news:9", 2), ("request:R-1", 1)]


# ----------------------------------------------------------------------
# intake 侧：派活备忘措辞
# ----------------------------------------------------------------------


def _intake_settings(serve=(G1,)) -> object:
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
            "jev": {"timeout_ms": 1500},
        }
    )
    return settings


class _FakeJev:
    def __init__(self, answers) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    async def ask(self, state, questions, *, purpose: str, group_id: str = "", timeout_ms=None):
        self.calls.append({"purpose": purpose, "group_id": group_id})
        return self.answers


class _FakeApprovals:
    def __init__(self, *, status: str = "pending") -> None:
        self.status = status
        self.created: list[tuple[str, dict]] = []

    def create(self, group_id: str, **kw):
        self.created.append((str(group_id), dict(kw)))
        return {"id": "R-1", "status": self.status, "auto": self.status != "pending",
                "task_id": "T-1" if self.status != "pending" else None, "goal_id": None}


class _FakeMentions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5) -> None:
        self.items.append(
            {"group_id": str(group_id), "text": str(text), "key": str(key), "ttl_s": float(ttl_s)}
        )


def _make_intake(*, jev, approvals, mentions):
    settings = _intake_settings()
    signals = Signals()
    spawned: list = []
    intake = Intake(
        lambda: settings,
        signals,
        jev=jev,
        approvals=approvals,
        mentions=mentions,
        bot_qq="987654321",
        spawn=lambda coro: spawned.append(coro),
    )
    return intake, spawned


async def _drain(spawned: list) -> None:
    for coro in list(spawned):
        await coro


@pytest.mark.asyncio
async def test_assign_memo_wording_is_a_command_not_a_note(tmp_path) -> None:
    """派活备忘要明确让 MaiBot 别自己做；只写发起人显示名，不带画像 / 内部 id。"""
    mention = _FakeMentions()
    intake, spawned = _make_intake(
        jev=_FakeJev({"kind": ("prepare", 0.95, 0.9)}),
        approvals=_FakeApprovals(status="pending"),
        mentions=mention,
    )
    await intake.handle(
        hook_message(
            group_id=G1, is_at=True, user_id="20002", nickname="阿柒",
            message_id="m-1", text="帮我整理一份铝坨坨的资料",
        )
    )
    await _drain(spawned)
    assert len(mention.items) == 1
    text = mention.items[0]["text"]
    # 明确指令
    assert "不要自己做" in text
    assert "不用答应" in text
    assert "已由 MaiWork 接手" in text
    # 只写发起人显示名（不是 user_id）
    assert "阿柒" in text
    assert "20002" not in text
    # 不带画像 / 隐私字段
    assert "画像" not in text
    assert "关注" not in text
    # 状态按实际写：还在等管理员批准
    assert "等管理员批准" in text
    # key 仍是「派活」前缀（delivery 靠它排优先级），有效期没变
    assert mention.items[0]["key"] == "request:R-1"
    assert mention.items[0]["ttl_s"] == 30 * 60


@pytest.mark.asyncio
async def test_assign_memo_wording_says_started_when_already_started(tmp_path) -> None:
    """免批 / 直接开工时同一句改成「已开工」口径。"""
    mention = _FakeMentions()
    intake, spawned = _make_intake(
        jev=_FakeJev({"kind": ("prepare", 0.95, 0.9)}),
        approvals=_FakeApprovals(status="approved"),
        mentions=mention,
    )
    await intake.handle(
        hook_message(group_id=G1, is_at=True, user_id="20002", nickname="阿柒", text="帮我整理一份资料")
    )
    await _drain(spawned)
    assert len(mention.items) == 1
    text = mention.items[0]["text"]
    assert "已开工" in text
    assert "不要自己做" in text
    assert "等管理员批准" not in text
