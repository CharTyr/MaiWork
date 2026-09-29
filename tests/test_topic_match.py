"""delivery.py 的 TopicMatcher（按话题接上，docs/02 §4.1「群友自己聊起时接得上」）。

红线对应：
- planner 钩子里纯代码匹配，不调模型、不做网络；
- 非服务群零处理（is_served 复核后候选连查都不查）；
- 候选只有资讯 / 构想，不注入关注成员个人画像内容；
- 同样有 300 字总上限；同一条内容同群 30 分钟内最多注入 3 轮；
- 匹配耗时：1000 条候选 < 50ms（粗测）。

MaiBot 上下文里的聊天消息是 `<message ...>文本</message>` 形式的 text part，
`is_self_message="true"` 的是 MaiBot 自己说的，跳过（参考 reference/jev/processor.py）。
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, TopicMatcher
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path):
    store = Store(tmp_path / "t.db")
    store.migrate()
    yield store
    store.close()


def _settings(serve: tuple[str, ...] = (GID,)):
    merged = {"groups": {"serve": [{"group": f"qq:{g}"} for g in serve]}}
    settings, _ = load_settings(merged)
    return settings


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
    title: str = "M7 芯片发布",
    keywords=("m7", "芯片"),
    votes: int = 0,
    created: float | None = None,
    rejected: int = 0,
    kind: str = "news",
    body: str = "正文：新芯片性能翻倍",
    summary: str = "摘要：新芯片",
    url: str = "https://example.com/m7",
) -> int:
    with store.tx() as conn:
        conn.execute("INSERT INTO news_batches (group_id, slot_ts, created) VALUES (?, 0, 0)", (gid,))
        bid = int(conn.execute("SELECT MAX(id) AS m FROM news_batches").fetchone()["m"])
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, body, keywords,"
            " chat_votes, sources, rejected, kind, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bid, gid, title, summary, body,
                json.dumps(list(keywords), ensure_ascii=False),
                int(votes),
                json.dumps([{"url": url}]) if url else "[]",
                int(rejected), kind,
                (float(created) if created is not None else clock.now()),
            ),
        )
        return int(cur.lastrowid)


def _seed_idea(
    store: Store,
    *,
    gid: str = GID,
    title: str = "做个抽签小程序",
    keywords=("抽签", "小程序"),
    state: str = "new",
    created: float | None = None,
    body: str = "构想正文：帮群里快速抽签",
) -> int:
    now = clock.now()
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, keywords, state, created, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                gid, title, body,
                json.dumps(list(keywords), ensure_ascii=False),
                state,
                (float(created) if created is not None else now), now,
            ),
        )
        return int(cur.lastrowid)


def _msg_part(text: str, *, is_self: bool = False) -> dict:
    attrs = ' msg_id="m1" user_name="群友"'
    if is_self:
        attrs = ' msg_id="m2" is_self_message="true"'
    return {
        "item_type": "UserMessageItem",
        "meta": {},
        "parts": [{"type": "text", "text": f"<message{attrs}>{text}</message>"}],
    }


def _kwargs(chat: list, session_id: str = "sess-1", system_text: str = "系统指令") -> dict:
    """chat: [("文本", 是否机器人自己)] 按时间从旧到新。"""
    items = [
        {
            "item_type": "SystemMessageItem",
            "meta": {},
            "parts": [{"type": "text", "text": system_text}],
        }
    ]
    for text, is_self in chat:
        items.append(_msg_part(text, is_self=is_self))
    return {
        "item_schema_version": 1,
        "tool_definitions": [],
        "session_id": session_id,
        "items": items,
    }


def _make(tmp_path, serve=(GID,)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(serve)
    return store, settings, Mentions(store, lambda: settings), TopicMatcher(store)


# ----------------------------------------------------------------------
# 解析：items 里的聊天消息
# ----------------------------------------------------------------------


class TestParseChatMessages:
    def test_parse_message_text_parts(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        kwargs = _kwargs([("大家在聊 M7 芯片", False), ("对啊", False)])
        text = tm.recent_chat_text(kwargs)
        assert "m7 芯片" in text  # 小写化
        assert "对啊" in text

    def test_skip_self_messages(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        kwargs = _kwargs([("有人在吗", False), ("我在～（机器人自己说的，提到 m8）", True)])
        text = tm.recent_chat_text(kwargs)
        assert "有人在吗" in text
        assert "m8" not in text  # 机器人自己说的不算

    def test_only_last_6_user_messages(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        kwargs = _kwargs([(f"消息{i}", False) for i in range(8)])
        text = tm.recent_chat_text(kwargs)
        assert "消息7" in text
        assert "消息2" in text
        assert "消息0" not in text  # 最旧的两条超出窗口
        assert "消息1" not in text

    def test_non_message_text_ignored(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        kwargs = _kwargs([("正常消息", False)])
        kwargs["items"].append(
            {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "纯文本不带外壳"}]}
        )
        text = tm.recent_chat_text(kwargs)
        assert "纯文本" not in text

    def test_empty_or_bad_kwargs_returns_empty(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        assert tm.recent_chat_text(None) == ""
        assert tm.recent_chat_text({}) == ""
        assert tm.recent_chat_text({"items": "no"}) == ""


# ----------------------------------------------------------------------
# 命中规则
# ----------------------------------------------------------------------


class TestMatchRules:
    def test_two_keywords_hit_matches(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"))
        kwargs = _kwargs([("新 m7 芯片看着不错", False)])
        lines = tm.memo_lines(GID, kwargs)
        assert len(lines) == 1
        assert "M7 芯片发布" in lines[0][0]

    def test_single_keyword_no_votes_no_match(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"), votes=0)
        kwargs = _kwargs([("大家聊 m7 了吗", False)])  # 只命中一个关键词
        assert tm.memo_lines(GID, kwargs) == []

    def test_single_keyword_votes_no_longer_relax(self, tmp_path):
        """2026-09-29 删掉「想在群里聊」：老数据里的 chat_votes 不再让单个关键词算接得上。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"), votes=5)
        kwargs = _kwargs([("大家聊 m7 了吗", False)])
        assert tm.memo_lines(GID, kwargs) == []

    def test_keyword_min_len_2(self, tmp_path):
        """单字符关键词不算命中。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("x", "xy"), votes=9)
        kwargs = _kwargs([("x 到处都是 x", False)])
        assert tm.memo_lines(GID, kwargs) == []

    def test_match_case_insensitive(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("M7", "芯片"))
        kwargs = _kwargs([("m7 芯片", False)])
        assert len(tm.memo_lines(GID, kwargs)) == 1

    def test_top_score_max_2(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        for i in range(4):
            _seed_news(store, title=f"第{i}篇", keywords=("m7", "芯片"))
        kwargs = _kwargs([("m7 芯片", False)])
        lines = tm.memo_lines(GID, kwargs)
        assert len(lines) == 2  # 最多 2 条

    def test_ideas_are_candidates(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_idea(store, keywords=("抽签", "小程序"))
        kwargs = _kwargs([("谁能写个抽签 小程序", False)])
        lines = tm.memo_lines(GID, kwargs)
        assert len(lines) == 1
        assert "做个抽签小程序" in lines[0][0]
        # 构想没有链接：行尾不带（…）
        assert "（" not in lines[0][0]

    def test_dismissed_idea_excluded(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_idea(store, keywords=("抽签", "小程序"), state="dismissed")
        kwargs = _kwargs([("抽签 小程序", False)])
        assert tm.memo_lines(GID, kwargs) == []

    def test_rejected_and_old_news_excluded(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="被筛掉的", keywords=("m7", "芯片"), rejected=1)
        _seed_news(store, title="四天前的", keywords=("m7", "芯片"), created=NOW - 4 * 86400)
        kwargs = _kwargs([("m7 芯片", False)])
        assert tm.memo_lines(GID, kwargs) == []

    def test_old_idea_excluded(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_idea(store, keywords=("抽签", "小程序"), created=NOW - 8 * 86400)
        kwargs = _kwargs([("抽签 小程序", False)])
        assert tm.memo_lines(GID, kwargs) == []

    def test_no_chat_text_no_match(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"))
        kwargs = _kwargs([])  # 只有 system item
        assert tm.memo_lines(GID, kwargs) == []

    def test_news_link_in_line(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"), url="https://example.com/m7")
        kwargs = _kwargs([("m7 芯片", False)])
        lines = tm.memo_lines(GID, kwargs)
        assert "（https://example.com/m7）" in lines[0][0]

    def test_snippet_capped_60_chars(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"), body="长" * 200)
        kwargs = _kwargs([("m7 芯片", False)])
        lines = tm.memo_lines(GID, kwargs)
        # 正文或摘要只取前 60 字
        assert "长" * 61 not in lines[0][0]
        assert "长" * 60 in lines[0][0]


# ----------------------------------------------------------------------
# 30 分钟 3 轮上限 / 缓存
# ----------------------------------------------------------------------


class TestRateLimitAndCache:
    def test_same_item_max_3_rounds_in_30min(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"))
        kwargs = _kwargs([("m7 芯片", False)])
        for _ in range(3):
            lines = tm.memo_lines(GID, kwargs)
            assert len(lines) == 1
            tm.record_injected(GID, [key for _, key in lines])
        # 第 4 轮：被节制
        assert tm.memo_lines(GID, kwargs) == []
        # 31 分钟后又能提了
        fixed_clock[0] = NOW + 31 * 60
        assert len(tm.memo_lines(GID, kwargs)) == 1

    def test_candidates_cached_60s(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="旧的", keywords=("m7", "芯片"))
        first = tm.candidates_for(GID)
        assert [c["title"] for c in first] == ["旧的"]
        # 库里加了新的，但缓存还没到期 → 看不到
        _seed_news(store, title="新的", keywords=("m7", "芯片"))
        assert [c["title"] for c in tm.candidates_for(GID)] == ["旧的"]
        # 61 秒后刷新 → 看得到
        fixed_clock[0] = NOW + 61
        titles = [c["title"] for c in tm.candidates_for(GID)]
        assert "新的" in titles and "旧的" in titles

    def test_other_group_isolated(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, gid="222", keywords=("m7", "芯片"))
        assert tm.candidates_for(GID) == []
        assert len(tm.candidates_for("222")) == 1


# ----------------------------------------------------------------------
# inject 整合：排在备忘前面、300 字上限、非服务群零处理、无个人画像
# ----------------------------------------------------------------------


class TestInjectIntegration:
    def test_topic_line_before_mentions(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, title="M7 芯片发布", keywords=("m7", "芯片"))
        m.add(GID, "普通备忘条目", key="k1", ttl_s=3600, turns=2)
        out = m.inject(_kwargs([("m7 芯片", False)]))
        assert out is not None
        text = out["items"][0]["parts"][0]["text"]
        assert "【MaiWork 备忘】" in text
        assert "可以自然接一句" in text
        assert "M7 芯片发布" in text
        assert "普通备忘条目" in text
        # 话题接龙排在已有备忘前面
        assert text.index("M7 芯片发布") < text.index("普通备忘条目")

    def test_topic_only_no_mentions_still_injects(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片"))
        out = m.inject(_kwargs([("m7 芯片", False)]))
        assert out is not None
        text = out["items"][0]["parts"][0]["text"]
        assert "M7 芯片发布" in text

    def test_total_cap_300_chars(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, title="超长" * 40, keywords=("m7", "芯片"), body="文" * 100)
        m.add(GID, "很长的备忘" + "备" * 200, key="k1", ttl_s=3600, turns=2)
        out = m.inject(_kwargs([("m7 芯片", False)]))
        assert out is not None
        text = out["items"][0]["parts"][0]["text"]
        memo = text[text.index("【MaiWork 备忘】"):]
        assert len(memo) <= 300

    def test_non_served_group_zero_processing(self, tmp_path):
        """非服务群：返回 None，候选查询都不做（缓存为空）。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store, gid="222", session_id="sess-other")  # 库里有群但不在服务列表
        _seed_news(store, gid="222", keywords=("m7", "芯片"))
        out = m.inject(_kwargs([("m7 芯片", False)], session_id="sess-other"))
        assert out is None
        assert m._topics._cache == {}  # 一次候选查询都没发生

    def test_no_personal_profile_in_output(self, tmp_path):
        """注入内容里没有关注成员私下画像（候选本来只有资讯/构想）。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片"))
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_messages (group_id, user_id, ts, message_id, text)"
                " VALUES (?, ?, 0, 'fm1', ?)",
                (GID, "10001", "阿花的私下画像注记 m7 芯片"),
            )
        out = m.inject(_kwargs([("m7 芯片", False)]))
        assert out is not None
        text = out["items"][0]["parts"][0]["text"]
        assert "阿花" not in text
        assert "私下画像" not in text

    def test_inject_rate_limited_after_3_rounds(self, tmp_path):
        """走完整 inject：同一条 30 分钟内最多 3 轮。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片"))
        for _ in range(3):
            out = m.inject(copy.deepcopy(_kwargs([("m7 芯片", False)])))
            assert out is not None
            assert "M7 芯片发布" in out["items"][0]["parts"][0]["text"]
        # 第 4 轮不再注入（也没有别的备忘 → None）
        assert m.inject(copy.deepcopy(_kwargs([("m7 芯片", False)]))) is None

    def test_mention_turns_still_decremented_with_topics(self, tmp_path):
        """话题行挤进来了，原有备忘的 turns 递减行为不变。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片"))
        m.add(GID, "普通备忘", key="k1", ttl_s=3600, turns=2)
        out = m.inject(_kwargs([("m7 芯片", False)]))
        assert out is not None
        row = store.read().execute("SELECT turns_left FROM mentions WHERE key='k1'").fetchone()
        assert int(row["turns_left"]) == 1


# ----------------------------------------------------------------------
# 性能粗测
# ----------------------------------------------------------------------


class TestPerformance:
    def test_match_1000_candidates_under_50ms(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        with store.tx() as conn:
            conn.execute("INSERT INTO news_batches (group_id, slot_ts, created) VALUES (?, 0, 0)", (GID,))
            bid = int(conn.execute("SELECT MAX(id) AS m FROM news_batches").fetchone()["m"])
            for i in range(1000):
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, keywords, sources, created)"
                    " VALUES (?, ?, ?, ?, '[]', ?)",
                    (bid, GID, f"第{i}条", json.dumps([f"关键词{i}", "芯片"]), NOW),
                )
        cands = tm.candidates_for(GID)
        assert len(cands) == 1000
        text = "大家在聊 m7 芯片和别的事情"
        t0 = time.perf_counter()
        for _ in range(10):
            tm.match(cands, text)
        dt = (time.perf_counter() - t0) / 10
        assert dt < 0.05, f"1000 条候选匹配耗时 {dt * 1000:.1f}ms，超过 50ms"
