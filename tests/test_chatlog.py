"""chatlog.py 测试（docs/02-设计.md §4.1「有人味」：群友版「能在资讯里点名群友」的基础）。

chat_log = 服务群最近 14 天发言的只读副本（FTS5 trigram，支持中文全文检索）：
- 写入只收：非机器人、文本非空、且不是「[图片]」占位的消息（profile.tick 统计之后调
  chatlog.record_messages；本文件直接测 record_messages / search_chat / prune_old）。
- 清理：每天清一次 14 天前的（kv 记上次清的日子，同一天第二次调用跳过）。
- search_chat(store, group_id, query, *, days=14, limit=8)：
  - trigram 需要查询词 ≥3 个字符；短词自动换 LIKE 兜底（照样能命中）；
  - 只查本群（别的群的原话绝不混进来）；
  - 返回 [{ts, who, text, message_id}]，新的在前；
  - 空查询词 / 没命中 → []。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.chatlog import prune_old, record_messages, search_chat
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"
GID_B = "555666777"
NOW = 1_790_000_000.0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


class M:
    """最小的消息对象（host.Msg 同名字段；tick 要用 reply_to / is_at）。"""

    def __init__(self, mid: str, ts: float, text: str, *, user: str = "u1",
                 name: str = "阿一", bot: bool = False) -> None:
        self.id = mid
        self.ts = ts
        self.text = text
        self.user_id = user
        self.user_name = name
        self.is_bot = bot
        self.is_at = False
        self.is_picture = False
        self.reply_to = ""


def _seed_chat(store: Store) -> None:
    msgs = [
        M("m1", NOW - 100, "之前聊过重装系统的事，ventoy 真香", user="u1", name="阿一"),
        M("m2", NOW - 90, "FPGA 开发板选型纠结中，荔枝派还是 Tang", user="u2", name="老王"),
        M("m3", NOW - 80, "重装系统之前记得先备份 home 目录", user="u1", name="阿一"),
        M("m4", NOW - 70, "[图片]", user="u3", name="图哥"),          # 图片占位：不进
        M("m5", NOW - 60, "", user="u3", name="空哥"),                # 空文本：不进
        M("m6", NOW - 50, "   ", user="u3", name="白哥"),             # 全空白：不进
        M("m7", NOW - 40, "我机器人自己说一句", bot=True),            # 机器人：不进
    ]
    record_messages(store, GID, msgs, now=NOW)


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------


class TestRecord:
    def test_writes_text_messages_only(self, store: Store) -> None:
        _seed_chat(store)
        rows = store.read().execute(
            "SELECT text, message_id, user_name FROM chat_log WHERE group_id=?", (GID,)
        ).fetchall()
        texts = {r["text"] for r in rows}
        assert any("ventoy" in t for t in texts)
        assert "[图片]" not in texts
        assert "" not in texts
        assert not any("机器人" in t for t in texts)
        assert len(rows) == 3

    def test_stores_columns(self, store: Store) -> None:
        _seed_chat(store)
        row = store.read().execute(
            "SELECT * FROM chat_log WHERE message_id='m2'"
        ).fetchone()
        assert row is not None
        assert row["group_id"] == GID
        assert row["user_id"] == "u2"
        assert row["user_name"] == "老王"
        assert row["ts"] == pytest.approx(NOW - 90)

    def test_duplicate_message_id_ignored(self, store: Store) -> None:
        _seed_chat(store)
        record_messages(store, GID, [M("m1", NOW - 100, "重复插入同一 id")], now=NOW)
        rows = store.read().execute(
            "SELECT COUNT(*) c FROM chat_log WHERE message_id='m1'"
        ).fetchone()
        assert int(rows["c"]) == 1
        # 原文不动
        row = store.read().execute(
            "SELECT text FROM chat_log WHERE message_id='m1'"
        ).fetchone()
        assert "ventoy" in row["text"]

    def test_empty_batch_is_noop(self, store: Store) -> None:
        record_messages(store, GID, [], now=NOW)
        rows = store.read().execute("SELECT COUNT(*) c FROM chat_log").fetchone()
        assert int(rows["c"]) == 0


# ----------------------------------------------------------------------
# 清理（14 天）
# ----------------------------------------------------------------------


class TestPrune:
    def test_prunes_older_than_14_days(self, store: Store) -> None:
        old = NOW - 15 * 86400
        recent = NOW - 3 * 86400
        record_messages(
            store, GID,
            [M("old1", old, "十五天前的老消息"), M("new1", recent, "三天前的新消息")],
            now=NOW,
        )
        # prune_old 每天最多做一次；第一次一定真清
        prune_old(store, GID, now=NOW)
        texts = [r["text"] for r in store.read().execute("SELECT text FROM chat_log").fetchall()]
        assert any("新消息" in t for t in texts)
        assert not any("老消息" in t for t in texts)

    def test_prune_only_once_per_day(self, store: Store) -> None:
        """同一天（北京时间）第二次调用直接跳过：塞一条旧消息验证第二次调用没动库。"""
        record_messages(store, GID, [M("a1", NOW - 100, "今天的正常消息")], now=NOW)
        prune_old(store, GID, now=NOW)
        # 同日再调一次：不动库。塞一条 15 天前的，第二次调用不该清它
        record_messages(store, GID, [M("a2", NOW - 15 * 86400, "手动塞的旧消息")], now=NOW)
        prune_old(store, GID, now=NOW)
        row = store.read().execute(
            "SELECT COUNT(*) c FROM chat_log WHERE message_id='a2'"
        ).fetchone()
        assert int(row["c"]) == 1
        # 第二天再调：清掉
        prune_old(store, GID, now=NOW + 86400)
        row = store.read().execute(
            "SELECT COUNT(*) c FROM chat_log WHERE message_id='a2'"
        ).fetchone()
        assert int(row["c"]) == 0

    def test_prune_is_per_group(self, store: Store) -> None:
        record_messages(store, GID_B, [M("b1", NOW - 15 * 86400, "另一个群的旧消息")], now=NOW)
        prune_old(store, GID, now=NOW)  # 清 GID 不影响 GID_B（同一天各记各的）
        row = store.read().execute(
            "SELECT COUNT(*) c FROM chat_log WHERE group_id=?", (GID_B,)
        ).fetchone()
        assert int(row["c"]) == 1


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------


class TestSearch:
    def test_chinese_trigram_hit(self, store: Store) -> None:
        _seed_chat(store)
        hits = search_chat(store, GID, "重装系统", days=14, limit=8, now=NOW)
        assert len(hits) >= 1
        assert all(isinstance(h["ts"], float) for h in hits)
        assert all(h["who"] for h in hits)
        assert all(h["message_id"] for h in hits)
        # 两条「重装系统」都该命中；新的在前
        assert hits[0]["message_id"] == "m3"
        assert hits[1]["message_id"] == "m1"

    def test_short_query_falls_back_to_like(self, store: Store) -> None:
        """查询词 <3 个字符：trigram 匹配不了，走 LIKE 兜底照样命中。"""
        _seed_chat(store)
        hits = search_chat(store, GID, "备份", days=14, limit=8, now=NOW)
        assert len(hits) == 1
        assert hits[0]["who"] == "阿一"
        assert "备份" in hits[0]["text"]
        # 单字符也要能查
        hits = search_chat(store, GID, "香", days=14, limit=8, now=NOW)
        assert len(hits) == 1
        assert hits[0]["message_id"] == "m1"

    def test_only_own_group(self, store: Store) -> None:
        _seed_chat(store)
        record_messages(store, GID_B, [M("x1", NOW - 30, "别的群也聊过重装系统")], now=NOW)
        hits = search_chat(store, GID, "重装系统", days=14, limit=8, now=NOW)
        assert {h["message_id"] for h in hits} == {"m1", "m3"}
        hits_b = search_chat(store, GID_B, "重装系统", days=14, limit=8, now=NOW)
        assert {h["message_id"] for h in hits_b} == {"x1"}

    def test_respects_days_window(self, store: Store) -> None:
        old = NOW - 20 * 86400
        record_messages(store, GID, [M("o1", old, "二十天前聊过重装系统")], now=NOW)
        record_messages(store, GID, [M("n1", NOW - 100, "今天聊重装系统")], now=NOW)
        hits = search_chat(store, GID, "重装系统", days=14, limit=8, now=NOW)
        assert {h["message_id"] for h in hits} == {"n1"}
        # 放宽到 25 天两条都回来
        hits = search_chat(store, GID, "重装系统", days=25, limit=8, now=NOW)
        assert {h["message_id"] for h in hits} == {"n1", "o1"}

    def test_limit_and_order(self, store: Store) -> None:
        msgs = [M(f"s{i}", NOW - i, f"重装系统第{i}条") for i in range(10)]
        record_messages(store, GID, msgs, now=NOW)
        hits = search_chat(store, GID, "重装系统", days=14, limit=3, now=NOW)
        assert len(hits) == 3
        assert [h["message_id"] for h in hits] == ["s0", "s1", "s2"]  # 新的在前

    def test_empty_query_returns_empty(self, store: Store) -> None:
        _seed_chat(store)
        assert search_chat(store, GID, "", days=14, now=NOW) == []
        assert search_chat(store, GID, "   ", days=14, now=NOW) == []
        assert search_chat(store, GID, "完全不沾边的词组", days=14, now=NOW) == []

    def test_result_shape(self, store: Store) -> None:
        _seed_chat(store)
        hits = search_chat(store, GID, "FPGA", days=14, now=NOW)
        assert len(hits) == 1
        h = hits[0]
        assert set(h.keys()) == {"ts", "who", "text", "message_id"}
        assert h["who"] == "老王"
        assert h["message_id"] == "m2"
        assert "FPGA" in h["text"]


# ----------------------------------------------------------------------
# profile.tick 接线：统计之后的同一批新消息也进 chat_log，同时每天清旧
# ----------------------------------------------------------------------


class TestTickWiring:
    """tick 里统计之后：本轮新消息（非机器人、非 [图片]）进 chat_log；14 天旧的清掉。"""

    def _profiles(self, store: Store, msgs: list):
        from CharTyr_MaiWork.maiwork.config import load_settings
        from CharTyr_MaiWork.maiwork.profile import Profiles
        from fakes import FakeHost, FakeModels

        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        host = FakeHost(msgs, session_id="sess-1")
        return Profiles(store, host, FakeModels(ready=False), lambda: settings)

    def test_tick_writes_chat_log(self, store: Store, monkeypatch) -> None:
        from CharTyr_MaiWork.maiwork import clock as _clock
        from CharTyr_MaiWork.maiwork.profile import _BIN_SECONDS

        monkeypatch.setattr(_clock, "now", lambda: NOW)
        msgs = [
            M("t1", NOW - 100, "大家聊聊重装系统的经验", user="u1", name="阿一"),
            M("t2", NOW - 90, "[图片]", user="u2", name="图哥"),
            M("t3", NOW - 80, "", user="u2", name="空哥"),
            M("t4", NOW - 70, "机器人插话", bot=True),
        ]
        profiles = self._profiles(store, msgs)
        import asyncio

        res = asyncio.run(profiles.tick(GID))
        assert res.read == 4
        rows = store.read().execute(
            "SELECT text FROM chat_log WHERE group_id=?", (GID,)
        ).fetchall()
        texts = {r["text"] for r in rows}
        assert "大家聊聊重装系统的经验" in texts
        assert "[图片]" not in texts
        assert "" not in texts
        assert not any("机器人" in t for t in texts)

        # 第二天再 tick：14 天前的被清
        old = NOW - 15 * 86400
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO chat_log (text, group_id, message_id, ts, user_id, user_name)"
                " VALUES ('很早以前聊过重装系统', ?, 'old1', ?, 'u9', '古人')",
                (GID, old),
            )
        import asyncio as _aio

        monkeypatch.setattr(_clock, "now", lambda: NOW + 86400)
        _aio.run(profiles.tick(GID))  # 第二天：没有新消息也照样做清理那一步
        row = store.read().execute(
            "SELECT COUNT(*) c FROM chat_log WHERE message_id='old1'"
        ).fetchone()
        assert int(row["c"]) == 0
