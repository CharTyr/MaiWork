"""资讯反哺 MaiBot 闲聊（2026-10-01 用户同意的 4 条）：

1. 匹配修准：纯数字 / 日期、两个字母的英文、英文常用词不算关键词；英文按整词对；
   至少要对上一个「具体」的词（≥3 个汉字、≥4 个字母、或字母数字混合的型号）；
   本群 ≥3 条候选都有的词算泛词，不能当那个具体词；最新两条群友消息里至少要有一个；
   网址、[事件-…] 系统提示不参与匹配。
2. 措辞：「你最近看到过……」，链接只在有人追问出处时给。
3. 有人问「最近有啥新鲜事」→ 递本群近 2 天评分最高的 1~2 条。
4. 记账：递了什么记进 chat_feeds；之后 MaiBot 自己的新发言里出现了这条的其他关键词 / 链接
   → 记「聊到了」，资讯状态标 mentioned（网页显示「MaiBot 在聊天里提过」）。
"""

from __future__ import annotations

import json

import pytest

from CharTyr_MaiWork.maiwork import chat_feed, clock
from test_topic_match import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例；fixed_clock 是 autouse）
    GID, NOW, _kwargs, _make, _seed_group, _seed_news, fixed_clock,
)


def _lines(tm, chat):
    return tm.memo_lines(GID, _kwargs(chat))


class TestKeywordFilter:
    def test_digits_and_dates_dont_count(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("09", "29", "2026-09-29", "5.5"))
        assert _lines(tm, [("09 月 29 号 2026-09-29 5.5", False)]) == []

    def test_short_or_common_english_dont_count(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("is", "and", "ii", "the"))
        assert _lines(tm, [("this is ii and the end", False)]) == []

    def test_english_whole_word_only(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="Opus 发布", keywords=("opus", "claude"))
        assert _lines(tm, [("corpus 和 claudette", False)]) == []
        assert len(_lines(tm, [("新的 opus 比 claude 老版强", False)])) == 1

    def test_needs_one_specific_keyword(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="双节表情包", keywords=("国庆", "中秋"))
        assert _lines(tm, [("国庆中秋快乐", False)]) == []

    def test_group_generic_word_cannot_be_the_specific_one(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        for i in range(3):
            _seed_news(store, title=f"第{i}条", keywords=("prompt", "llm", f"专属词{i}号"))
        # prompt / llm 在本群 3 条里都有 → 泛词，光它俩对上不算
        assert _lines(tm, [("prompt 和 llm 怎么写", False)]) == []
        lines = _lines(tm, [("专属词1号 的 prompt 怎么写", False)])
        assert len(lines) == 1 and "第1条" in lines[0][0]

    def test_hit_must_touch_latest_two_messages(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"))
        chat = [("m7 芯片好强", False), ("吃饭了吗", False), ("吃了", False), ("今天好热", False)]
        assert _lines(tm, chat) == []
        assert len(_lines(tm, chat + [("说回 m7", False)])) == 1

    def test_urls_and_system_events_ignored(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("github", "agent"))
        assert _lines(tm, [("https://github.com/x/agent-kit", False)]) == []
        assert _lines(tm, [("[事件-群消息撤回] github agent 撤回了一条消息", False)]) == []

    def test_product_code_counts_as_specific(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"))
        assert len(_lines(tm, [("m7 芯片", False)])) == 1


class TestWording:
    def test_line_reads_as_own_knowledge_and_link_on_ask(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, keywords=("m7", "芯片"), url="https://example.com/m7")
        line = _lines(tm, [("m7 芯片", False)])[0][0]
        assert "你最近看到过" in line
        assert "MaiWork" not in line
        assert "https://example.com/m7" in line and "出处" in line


def _set(store, nid, **cols):
    with store.tx() as conn:
        for k, v in cols.items():
            conn.execute(f"UPDATE news_items SET {k}=? WHERE id=?", (v, nid))


class TestAskForNews:
    def test_question_gets_top_two_by_score(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        a = _seed_news(store, title="甲新闻", keywords=("甲甲甲",))
        b = _seed_news(store, title="乙新闻", keywords=("乙乙乙",))
        c = _seed_news(store, title="丙新闻", keywords=("丙丙丙",))
        _set(store, a, score=4.5)
        _set(store, b, score=2.0)
        _set(store, c, score=3.5)
        lines = _lines(tm, [("最近有啥新鲜事吗", False)])
        texts = [t for t, _ in lines]
        assert len(texts) == 2
        assert "甲新闻" in texts[0] and "丙新闻" in texts[1]
        assert "有人在问" in texts[0]

    @pytest.mark.parametrize("q", ["今天有什么新闻", "有啥瓜", "这两天有什么大事", "来点新鲜事"])
    def test_question_variants(self, tmp_path, q):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="甲新闻", keywords=("甲甲甲",))
        assert len(_lines(tm, [(q, False)])) == 1

    def test_not_a_question_or_bot_asking(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="甲新闻", keywords=("甲甲甲",))
        assert _lines(tm, [("我看新闻说要降温", False)]) == []
        assert _lines(tm, [("最近有啥新鲜事吗", True)]) == []

    def test_excludes_old_personal_and_disliked(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        _seed_news(store, title="三天前", keywords=("甲甲甲",), created=NOW - 3 * 86400)
        p = _seed_news(store, title="个人向", keywords=("乙乙乙",))
        _set(store, p, target_user_id="10001")
        d = _seed_news(store, title="被踩的", keywords=("丙丙丙",))
        _set(store, d, down=3, up=1)
        assert _lines(tm, [("最近有什么新闻", False)]) == []

    def test_topic_and_ask_share_cap_without_duplicates(self, tmp_path):
        store, settings, m, tm = _make(tmp_path)
        a = _seed_news(store, title="M7 芯片发布", keywords=("m7", "芯片"))
        _set(store, a, score=5)
        b = _seed_news(store, title="乙新闻", keywords=("乙乙乙",))
        _set(store, b, score=4)
        _seed_news(store, title="丙新闻", keywords=("丙丙丙",))
        lines = _lines(tm, [("m7 芯片最近有啥新鲜事", False)])
        keys = [k for _, k in lines]
        assert len(keys) == 2 and len(set(keys)) == 2
        assert "M7 芯片发布" in lines[0][0]


def _feeds(store):
    return [dict(r) for r in store.read().execute("SELECT * FROM chat_feeds ORDER BY id")]


class TestLedger:
    def test_inject_records_and_merges_rounds(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        nid = _seed_news(store, keywords=("m7", "芯片"))
        assert m.inject(_kwargs([("m7 芯片", False)]), group_id=GID) is not None
        fixed_clock[0] = NOW + 60
        assert m.inject(_kwargs([("m7 芯片", False)]), group_id=GID) is not None
        rows = _feeds(store)
        assert len(rows) == 1
        r = rows[0]
        assert r["key"] == f"news:{nid}" and r["mode"] == "topic" and r["rounds"] == 2
        assert set(json.loads(r["hit"])) == {"m7", "芯片"}
        assert r["said_ts"] is None

    def test_bot_mentions_new_detail_marks_said(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        nid = _seed_news(store, keywords=("m7", "芯片", "台积电"))
        m.inject(_kwargs([("m7 芯片", False)]), group_id=GID)
        fixed_clock[0] = NOW + 30
        m.inject(_kwargs([("m7 芯片", False), ("听说 M7 是台积电代工的", True), ("真的假的", False)]), group_id=GID)
        r = _feeds(store)[0]
        assert r["said_ts"] == NOW + 30 and "台积电" in r["said_text"]
        row = store.read().execute("SELECT status_kind, status_at FROM news_items WHERE id=?", (nid,)).fetchone()
        assert row["status_kind"] == "mentioned" and row["status_at"] == NOW + 30

    def test_bot_echoing_group_words_is_not_said(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片", "台积电"))
        m.inject(_kwargs([("m7 芯片", False)]), group_id=GID)
        m.inject(_kwargs([("m7 芯片", False), ("m7 芯片确实", True)]), group_id=GID)
        assert _feeds(store)[0]["said_ts"] is None

    def test_old_bot_message_before_feed_not_counted(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片", "台积电"))
        old_bot = ("台积电今天开会", True)
        m.inject(_kwargs([old_bot, ("m7 芯片", False)]), group_id=GID)  # 这一轮才递
        m.inject(_kwargs([old_bot, ("m7 芯片", False)]), group_id=GID)  # 机器人没新发言
        assert _feeds(store)[0]["said_ts"] is None

    def test_fresh_process_seeds_before_judging(self, tmp_path, fixed_clock):
        """插件重载后第一次看到的机器人发言只当底，不拿来判「聊到了」。"""
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        _seed_news(store, keywords=("m7", "芯片", "台积电"))
        m.inject(_kwargs([("m7 芯片", False)]), group_id=GID)
        from CharTyr_MaiWork.maiwork.delivery import Mentions
        m2 = Mentions(store, lambda: settings)
        m2.inject(_kwargs([("台积电早就说了", True), ("m7 芯片", False)]), group_id=GID)
        assert _feeds(store)[0]["said_ts"] is None
        m2.inject(_kwargs([("台积电早就说了", True), ("台积电代工的", True), ("m7 芯片", False)]), group_id=GID)
        assert _feeds(store)[0]["said_ts"] is not None

    def test_used_status_not_overwritten(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        nid = _seed_news(store, keywords=("m7", "芯片", "台积电"))
        _set(store, nid, status_kind="used", status_at=1.0)
        m.inject(_kwargs([("m7 芯片", False)]), group_id=GID)
        m.inject(_kwargs([("m7 芯片", False), ("台积电代工", True)]), group_id=GID)
        assert _feeds(store)[0]["said_ts"] is not None
        row = store.read().execute("SELECT status_kind FROM news_items WHERE id=?", (nid,)).fetchone()
        assert row["status_kind"] == "used"

    def test_item_stats(self, tmp_path, fixed_clock):
        store, settings, m, tm = _make(tmp_path)
        _seed_group(store)
        nid = _seed_news(store, keywords=("m7", "芯片", "台积电"))
        assert chat_feed.item_stats(store.read(), GID, f"news:{nid}") == {"times": 0, "said_ts": None}
        m.inject(_kwargs([("m7 芯片", False)]), group_id=GID)
        fixed_clock[0] = NOW + 40 * 60  # 超过 30 分钟：新的一次
        m.inject(_kwargs([("又说 m7 芯片", False)]), group_id=GID)
        st = chat_feed.item_stats(store.read(), GID, f"news:{nid}")
        assert st["times"] == 2 and st["said_ts"] is None
