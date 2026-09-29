"""personal.py（关注成员的个人向产出）测试 + 全链路的「个人向不外泄」测试。

覆盖（对应设计 docs/02 §3.2、§4.1；代码接口 docs/07 §8.2/§9.3）：
- prepare_personal：只给「当前关注成员 + personal_profile 开 + persona 已存在」的人做；
  每人每天（北京时间）最多 1 次、每次最多 personal_per_day 条（默认 3）；模型/搜索没配不做；
  个人向资讯写 news_items.target_user_id、个人向构想写 ideas.target_user_id。
- 写法：第二人称「写给他本人」；reason 里引用的原话只来自他本人（chatlog.search_chat 限定
  该 user_id 的发言）。
- 个人向不外泄：news_view（管理员/群友）、guides_view、ideas_view、话题候选池
  （topics.add_candidate）、TopicMatcher 候选，全都不含个人向条目。
- mention-to-member：只往本群可提起清单加一句（ttl 6 小时），不直接发群消息；
  文字过 privacy.scrub、不含 persona 片段；返回 {"ok": true}。
- 调度：北京时间 9:00–22:00 之外不做；同一群同一轮最多给 1 个到期的人跑；
  personal_feeds=false 不做。
- focus_personal_view（喂给 GroupView.focus[].personal）：
  {"news":≤5, "ideas":≤2, "last_ts": ts|null}；按 target_user_id 分到各人；不跨人、不跨群。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeCtx, FakeModelsQueue, FakeProfiles, focus_reply
from test_feeds import FakeSearch, UnavailableSearch, FakeWorkers, _ok_report  # noqa: F401

BJ = timezone(timedelta(hours=8))


def _bj_ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=BJ).timestamp()


NOW = _bj_ts(2026, 8, 31, 15)  # 北京时间 2026-08-31 15:00，落在 9:00–22:00 调度窗里
GID = "111"
UID = "10001"
UID2 = "10002"
UNAME = "阿帆"


def _settings(cfg: dict | None = None) -> Any:
    settings, _problems = load_settings(cfg or {})
    return settings


def _seed_group(store: Store, gid: str = GID, *, ready: bool = True) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (gid, 1_700_000_000.0 if ready else 0.0),
        )


def _persona_json(**over) -> str:
    p = {
        "summary": "在折腾 FPGA 的小项目",
        "doing": ["在写 FPGA 学习板", "在搭本地小模型"],
        "cares": ["开源硬件", "本地大模型"],
        "asked": ["想找一块便宜的学习板"],
        "style": "话不多但爱折腾",
    }
    p.update(over)
    return json.dumps(p, ensure_ascii=False)


def _add_focus_member(
    store: Store,
    uid: str = UID,
    gid: str = GID,
    *,
    name: str = UNAME,
    persona: str | None = None,
    removed: int = 0,
    persona_ts: float = NOW - 3600,
) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, reasons, note, pinned,"
            " removed, updated, persona, persona_ts) VALUES (?, ?, ?, '[]', ?, 1, ?, ?, ?, ?)",
            (
                gid, uid, name,
                "在折腾 FPGA 的小项目" if persona else "",
                removed, NOW - 7200,
                persona if persona is not None else _persona_json(),
                persona_ts,
            ),
        )


class FakeTopics:
    """只记 add_candidate 的假 Topics。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 候选 / 打分素材（和 feeds 的质量标准同构）
# ----------------------------------------------------------------------

_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"

_WORKER_ITEMS = {
    "items": [
        {"title": "新开源 FPGA 学习板上架", "url": "https://Example.COM/board",
         "summary": "一块新的 FPGA 学习板。便宜。资料全。", "kind": "news",
         "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
        {"title": "本地小模型量化教程", "url": "https://news.com/quant",
         "summary": "教你在小机器上跑量化模型。步骤清楚。", "kind": "news",
         "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
        {"title": "吃桃子的十种方法", "url": "https://food.com/peach",
         "summary": "生活小窍门。", "kind": "news",
         "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False},
    ]
}


def _personal_scores_json() -> str:
    """打分（个人向版）：第三条相关度栽掉（过不了第二道），前两条过线。"""
    return json.dumps(
        {
            "scores": [
                {"i": 0, "title": "新开源 FPGA 学习板上架", "info": 5, "source": 4, "relevance": 5,
                 "timeliness": 4, "chat": 4, "profile": 0, "topic": "FPGA", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "他在做 FPGA 学习板", "icon": "tools"},
                {"i": 1, "title": "本地小模型量化教程", "info": 4, "source": 5, "relevance": 4,
                 "timeliness": 4, "chat": 4, "profile": 1, "topic": "本地模型", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "他在搭本地小模型", "icon": "robot"},
                {"i": 2, "title": "吃桃子的十种方法", "info": 4, "source": 4, "relevance": 1,
                 "timeliness": 4, "chat": 1, "profile": 0, "topic": "生活", "sensitive": False,
                 "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
                 "why": "和他没关系", "icon": "newspaper"},
            ]
        },
        ensure_ascii=False,
    )


# ----------------------------------------------------------------------
# Personal 单元测试
# ----------------------------------------------------------------------


class _time_patch:
    """临时把 personal.clock.now 钉到 NOW（数据都围 NOW 造）。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.personal as mod

        self._mod = mod
        self._orig = mod.clock.now
        mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


def _make_personal(
    tmp_path,
    *,
    models=None,
    workers=None,
    search=None,
    topics=None,
    cfg: dict | None = None,
    persona: str | None = None,
    removed: int = 0,
    focus_on: bool = True,
    seed_persona: bool = True,
):
    """装好个人向模块 + 一个带画像的关注成员（默认阿帆 / UID）。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed_group(store, GID, ready=True)
    if seed_persona:
        _add_focus_member(store, UID, GID, persona=persona, removed=removed)
    raw = {"plugin": {"enabled": True}, "groups": {"serve": [{"group": f"qq:{GID}"}]}}
    if not focus_on:
        raw["focus"] = {"personal_profile": False}
    if cfg:
        for k, v in cfg.items():
            raw.setdefault(k, {}).update(v)
    settings = _settings(raw)
    if models is None:
        models = FakeModelsQueue(ready=True)
    if workers is None:
        workers = FakeWorkers(_ok_report(_WORKER_ITEMS))
    if topics is None:
        topics = FakeTopics()
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [
        {"category": "ongoing", "text": "在做开源硬件项目"},
        {"category": "interest", "text": "本地大模型"},
    ]
    from CharTyr_MaiWork.maiwork.personal import Personal

    personal = Personal(
        store, models, workers, profiles, topics, lambda: settings, search=search
    )
    return store, settings, personal, models, workers, topics, profiles


class TestPersonalPrepare:
    def test_requires_persona_existing(self, tmp_path) -> None:
        """persona 还没建出来的人不做（不派子 agent、不入库）。"""
        store, settings, personal, models, workers, topics, _ = _make_personal(tmp_path)
        with store.tx() as conn:
            conn.execute("UPDATE focus_members SET persona='' WHERE group_id=? AND user_id=?", (GID, UID))
        got = _run(personal.prepare_personal(GID, UID))
        assert got == 0
        assert workers.calls == []
        assert store.read().execute(
            "SELECT COUNT(*) c FROM news_items WHERE target_user_id=?", (UID,)
        ).fetchone()["c"] == 0

    def test_requires_current_focus_member(self, tmp_path) -> None:
        """已被移出关注的（removed=1）不做。"""
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, removed=1
        )
        got = _run(personal.prepare_personal(GID, UID))
        assert got == 0
        assert workers.calls == []

    def test_requires_personal_profile_on(self, tmp_path) -> None:
        """[focus] personal_profile 关掉就不做。"""
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, focus_on=False
        )
        got = _run(personal.prepare_personal(GID, UID))
        assert got == 0
        assert workers.calls == []

    def test_models_not_ready(self, tmp_path) -> None:
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=FakeModelsQueue(ready=False)
        )
        got = _run(personal.prepare_personal(GID, UID))
        assert got == 0
        assert workers.calls == []

    def test_search_unavailable(self, tmp_path) -> None:
        """搜索没配（SearchUnavailable）→ 0，不派子 agent。"""
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, search=UnavailableSearch()
        )
        got = _run(personal.prepare_personal(GID, UID))
        assert got == 0
        assert workers.calls == []

    def test_collect_has_time_box(self, tmp_path) -> None:
        """子 agent 找资讯要有时间盒（和群资讯同一个 [feeds] collect_minutes）。
        2026-09-29 线上：个人资讯子 agent 没有时间盒，一轮跑了一个多小时、反复压缩对话。"""
        from CharTyr_MaiWork.maiwork import clock

        models = FakeModelsQueue(
            ready=True,
            replies=[
                json.dumps({"focus": [{"query": "FPGA 学习板 新出", "why": "在做 FPGA 学习板"}],
                            "idea": None}, ensure_ascii=False),
                _personal_scores_json(),
                json.dumps({"posts": []}, ensure_ascii=False),
            ],
        )
        store, settings, personal, models, workers, topics, _ = _make_personal(tmp_path, models=models)
        before = clock.now()
        _run(personal.prepare_personal(GID, UID))
        assert workers.calls, "应该派了子 agent"
        dl = workers.calls[0].get("deadline_ts")
        assert dl is not None, "个人资讯子 agent 必须带 deadline_ts"
        minutes = int(getattr(settings.feeds, "collect_minutes", 15) or 15)
        assert before + minutes * 60 - 5 <= dl <= clock.now() + minutes * 60 + 5
        assert f"{minutes} 分钟" in workers.calls[0]["brief"]

    def test_happy_path_writes_target_user_id(self, tmp_path) -> None:
        """全流程：过线的两条入 news_items 且 target_user_id=UID；被筛的一条也入库。"""
        models = FakeModelsQueue(
            ready=True,
            replies=[
                json.dumps({"focus": [{"query": "FPGA 学习板 新出", "why": "在做 FPGA 学习板"}],
                            "idea": None}, ensure_ascii=False),
                _personal_scores_json(),
                json.dumps({"posts": [
                    {"i": 0, "title": "新开源 FPGA 学习板上架",
                     "body": "你在弄 FPGA 学习板，这块新板可能用得上……",
                     "reason": "你前几天在群里提到想找一块便宜的学习板",
                     "refs": [], "audience": [], "keywords": ["FPGA"]},
                    {"i": 1, "title": "本地小模型量化教程",
                     "body": "你在搭本地小模型，这个量化教程可能用得上……",
                     "reason": "你之前在弄本地模型",
                     "refs": [], "audience": [], "keywords": ["模型"]},
                ]}, ensure_ascii=False),
            ],
        )
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            got = _run(personal.prepare_personal(GID, UID))
        assert got == 2  # 第三条 relevance=1<3 没过第二道
        rows = store.read().execute(
            "SELECT * FROM news_items WHERE target_user_id=? ORDER BY id", (UID,)
        ).fetchall()
        assert len(rows) == 3  # 含被筛的
        accepted = [r for r in rows if not r["rejected"]]
        assert len(accepted) == 2
        assert all(str(r["target_user_id"]) == UID for r in rows)
        # 个人向条目绝不进话题候选池
        assert topics.calls == []

    def test_per_day_limit_default_3(self, tmp_path) -> None:
        """每次最多 personal_per_day（默认 3）条：4 条过线也只留 3。"""
        titles = ["国产 FPGA 学习板社区发布新品", "本地大模型量化部署教程", "开源硬件周报第两百期", "单板机新手引导系统镜像"]
        items4 = {
            "items": [
                {"title": titles[i], "url": f"https://site{i}.com/{i}",
                 "summary": f"{titles[i]}。内容不错。值得看。", "kind": "news",
                 "published": NOW - 86400, "fetched": True, "quote": _QUOTE, "paywall": False}
                for i in range(4)
            ]
        }
        scores = {"scores": [
            {"i": i, "title": titles[i], "info": 5, "source": 5, "relevance": 5,
             "timeliness": 5, "chat": 5, "profile": 0, "topic": f"T{i}", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "他在做", "icon": "tools"}
            for i in range(4)
        ]}
        posts = {"posts": [
            {"i": i, "title": titles[i], "body": "你可能用得上……",
             "reason": "你在弄这个", "refs": [], "audience": [], "keywords": ["FPGA"]}
            for i in range(4)
        ]}
        models = FakeModelsQueue(ready=True, replies=[
            json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False),
            json.dumps(scores, ensure_ascii=False),
            json.dumps(posts, ensure_ascii=False),
        ])
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models, workers=FakeWorkers(_ok_report(items4))
        )
        with _time_patch():
            got = _run(personal.prepare_personal(GID, UID))
        assert got == 3
        n = store.read().execute(
            "SELECT COUNT(*) c FROM news_items WHERE target_user_id=? AND rejected=0", (UID,)
        ).fetchone()["c"]
        assert n == 3  # 默认 personal_per_day=3

    def test_per_day_configurable(self, tmp_path) -> None:
        """personal_per_day=1 时只留 1 条。"""
        models = FakeModelsQueue(ready=True, replies=[
            json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False),
            _personal_scores_json(),
            json.dumps({"posts": [
                {"i": 0, "title": "新开源 FPGA 学习板上架", "body": "……", "reason": "……",
                 "refs": [], "audience": [], "keywords": ["x"]},
                {"i": 1, "title": "本地小模型量化教程", "body": "……", "reason": "……",
                 "refs": [], "audience": [], "keywords": ["x"]},
            ]}, ensure_ascii=False),
        ])
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models, cfg={"focus": {"personal_per_day": 1}}
        )
        assert settings.focus.personal_per_day == 1
        with _time_patch():
            _run(personal.prepare_personal(GID, UID))
        n = store.read().execute(
            "SELECT COUNT(*) c FROM news_items WHERE target_user_id=? AND rejected=0", (UID,)
        ).fetchone()["c"]
        assert n == 1

    def test_once_per_day(self, tmp_path) -> None:
        """每人每天（北京时间）最多 1 次：当天跑过一次，再跑直接 0、不调模型。"""
        models = FakeModelsQueue(ready=True, replies=[
            json.dumps({"focus": [{"query": "FPGA", "why": "在做"}], "idea": None}, ensure_ascii=False),
            _personal_scores_json(),
            json.dumps({"posts": []}, ensure_ascii=False),
        ])
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _run(personal.prepare_personal(GID, UID))
        n_calls_after_first = len(models.calls)
        with _time_patch():
            again = _run(personal.prepare_personal(GID, UID))
        assert again == 0
        assert len(models.calls) == n_calls_after_first
        assert len(workers.calls) == 1  # 只跑过一轮子 agent

    def test_idea_optional_with_target_user_id(self, tmp_path) -> None:
        """可顺带产出 0–1 条「我可以帮你……」构想，落 ideas.target_user_id，不进候选池。"""
        models = FakeModelsQueue(ready=True, replies=[
            json.dumps({
                "focus": [{"query": "FPGA", "why": "在做"}],
                "idea": {"title": "我可以帮你把这块板的上手例程跑一遍",
                         "body": "我可以帮你把这板子的例程整理成一页", "step": "先列出要跑的例程",
                         "effort": "半天"},
            }, ensure_ascii=False),
            _personal_scores_json(),
            json.dumps({"posts": []}, ensure_ascii=False),
        ])
        store, settings, personal, models, workers, topics, _ = _make_personal(
            tmp_path, models=models
        )
        with _time_patch():
            _run(personal.prepare_personal(GID, UID))
        rows = store.read().execute(
            "SELECT * FROM ideas WHERE target_user_id=?", (UID,)
        ).fetchall()
        assert len(rows) == 1
        assert "帮你" in rows[0]["title"] or "帮你" in rows[0]["body"]
        assert all(c.get("kind") != "idea" for c in topics.calls)

    def test_reason_only_quotes_himself(self, tmp_path) -> None:
        """reason 里能引用的原话只来自他本人（chatlog.search_chat 只取该 user_id 的发言）。"""
        from CharTyr_MaiWork.maiwork import chatlog

        class _Msg:
            def __init__(self, mid, ts, uid, name, text):
                self.id = mid
                self.ts = ts
                self.user_id = uid
                self.user_name = name
                self.text = text
                self.is_bot = False

        store, settings, personal, models, workers, topics, _ = _make_personal(tmp_path)
        # 本人说过「想找个便宜的学习板」，别人说过「这块板真不错」
        chatlog.record_messages(store, GID, [
            _Msg("m1", NOW - 1000, UID, UNAME, "想找个便宜的学习板来练手"),
            _Msg("m2", NOW - 900, "99999", "路人甲", "新开源 FPGA 学习板真不错"),
        ], now=NOW)

        models2 = FakeModelsQueue(ready=True, replies=[
            json.dumps({"focus": [{"query": "FPGA 学习板", "why": "在做"}], "idea": None}, ensure_ascii=False),
            _personal_scores_json(),
            json.dumps({"posts": [
                {"i": 0, "title": "新开源 FPGA 学习板上架",
                 "body": "你在弄 FPGA，这块新板可能用得上……",
                 "reason": "你前几天在群里提到想找个便宜的学习板",
                 "refs": [1, 2],   # 代码侧必须只保留「本人」的那条
                 "audience": [], "keywords": ["FPGA"]},
            ]}, ensure_ascii=False),
        ])
        personal._models = models2
        with _time_patch():
            _run(personal.prepare_personal(GID, UID))
        # 至少有一条过线，且它的 refs 里（若有）只能是他本人的原话
        rows = store.read().execute(
            "SELECT refs FROM news_items WHERE target_user_id=? AND rejected=0 ORDER BY id",
            (UID,),
        ).fetchall()
        assert rows, "至少一条过线"
        for row in rows:
            for r in json.loads(row["refs"] or "[]"):
                assert str(r.get("who")) == UNAME  # 引用的原话只能是他本人的


# ----------------------------------------------------------------------
# 个人向不外泄：群资讯视图 / 群友视图 / 候选池 / TopicMatcher
# ----------------------------------------------------------------------


def _seed_personal_news(store: Store, uid: str = UID, gid: str = GID, *, created: float = NOW) -> int:
    """直接种一条个人向资讯 + 一条个人向构想到库里，返回 news_id。"""
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (gid, created, created),
        )
        batch_id = int(cur.lastrowid or 0)
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, created, kind, rejected, target_user_id)"
            " VALUES (?, ?, 'tools', '给他个人的FPGA资讯', '只给他看的摘要', '', '[]',"
            " 'e.com/x', ?, 4.0, 'pool', ?, 'news', 0, ?)",
            (batch_id, gid, created - 100, created, uid),
        )
        news_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
            " created, updated, target_user_id) VALUES (?, 'bulb', '我可以帮他……', '帮他整理',"
            " '', '第一步', '半天', 'new', ?, ?, ?)",
            (gid, created, created, uid),
        )
    return news_id


class _feeds_time_patch:
    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as mod

        self._mod = mod
        self._orig = mod.clock.now
        mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class _delivery_time_patch:
    def __enter__(self):
        import CharTyr_MaiWork.maiwork.delivery as mod

        self._mod = mod
        self._orig = mod.clock.now
        mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class TestPersonalIsolation:
    def test_news_view_excludes_personal_all_roles(self, tmp_path) -> None:
        """群资讯视图（管理员和群友）都不含个人向条目。"""
        from CharTyr_MaiWork.maiwork.feeds import Feeds

        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store, GID, ready=True)
        # 同时种一条普通（群向）资讯，确认群向的还在、个人向的不在
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)", (GID, NOW, NOW),
            )
            bid = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, created, kind, rejected,"
                " status_kind, target_user_id) VALUES (?, ?, '群向资讯', ?, 'news', 0, 'pool', '')",
                (bid, GID, NOW),
            )
        _seed_personal_news(store, UID, GID)
        settings = _settings()
        feeds = Feeds(store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: settings)
        with _feeds_time_patch():
            admin_view = feeds.news_view(GID, days=3, admin=True)
            member_view = feeds.news_view(GID, days=3, admin=False)
        for view in (admin_view, member_view):
            titles = [it["title"] for b in view for it in b.get("items", [])]
            assert "给他个人的FPGA资讯" not in titles
            assert "群向资讯" in titles  # 群向的还在

    def test_guides_ideas_views_exclude_personal(self, tmp_path) -> None:
        """好文专栏不含个人向条目。构想页（2026-09-28 起）含个人向构想、带 target_user_id，
        群友版（admin=False）清掉个人向构想的 basis（画像摘要）。"""
        from CharTyr_MaiWork.maiwork.feeds import Feeds

        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store, GID, ready=True)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, state, created, updated, target_user_id)"
                " VALUES (?, '群向构想', '给群的', 'new', ?, ?, '')",
                (GID, NOW, NOW),
            )
        _seed_personal_news(store, UID, GID)
        settings = _settings()
        feeds = Feeds(store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: settings)
        with _feeds_time_patch():
            guides = feeds.guides_view(GID, admin=True)
            ideas = feeds.ideas_view(GID)
        assert all("个人" not in g.get("title", "") for g in guides)
        mine = [i for i in ideas if i.get("target_user_id") == UID]
        assert len(mine) == 1 and mine[0]["basis"] == ""
        assert any("群向构想" in i.get("title", "") and i["target_user_id"] == "" for i in ideas)

    def test_topic_matcher_excludes_personal(self, tmp_path) -> None:
        """TopicMatcher 候选不含个人向条目（MaiBot 不会顺着个人向去接话）。"""
        from CharTyr_MaiWork.maiwork.delivery import TopicMatcher

        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store, GID, ready=True)
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW, NOW),
            )
            batch_id = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, keywords, sources,"
                " created, kind, rejected, target_user_id)"
                " VALUES (?, ?, '给他个人的FPGA资讯', '摘', '[\"fpga\"]', '[]', ?, 'news', 0, ?)",
                (batch_id, GID, NOW, UID),
            )
            # 群向的带上同关键词，确认还在
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, keywords, sources,"
                " created, kind, rejected, target_user_id)"
                " VALUES (?, ?, '群向FPGA', '摘', '[\"fpga\"]', '[]', ?, 'news', 0, '')",
                (batch_id, GID, NOW),
            )
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, keywords, state, created, updated,"
                " target_user_id) VALUES (?, '我可以帮他弄FPGA', '帮他', '[\"fpga\"]', 'new', ?, ?, ?)",
                (GID, NOW, NOW, UID),
            )
        tm = TopicMatcher(store)
        with _delivery_time_patch():
            cands = tm.candidates_for(GID, NOW)
        texts = " ".join(str(c.get("title") or "") + str(c.get("body") or "") for c in cands)
        assert "帮他" not in texts and "给他个人" not in texts
        assert "群向FPGA" in texts  # 群向的还在


# ----------------------------------------------------------------------
# mention_to_member：只加备忘，不发群消息、文字干净
# ----------------------------------------------------------------------


class TestMentionToMember:
    def _mentions(self, store, settings):
        from CharTyr_MaiWork.maiwork.delivery import Mentions

        return Mentions(store, lambda: settings)

    def test_adds_memo_and_no_send(self, tmp_path) -> None:
        """往可提起清单加一句；不产生任何群消息；文字不含 persona 片段；返回 ok。"""
        store, settings, personal, models, workers, topics, _ = _make_personal(tmp_path)
        news_id = _seed_personal_news(store, UID, GID)
        mentions = self._mentions(store, settings)
        with _time_patch(), _delivery_time_patch():
            out = personal.mention_to_member(GID, news_id, mentions=mentions)
        assert out == {"ok": True}
        rows = store.read().execute(
            "SELECT text, expires_ts FROM mentions WHERE group_id=?", (GID,)
        ).fetchall()
        assert len(rows) == 1
        text = str(rows[0]["text"])
        assert UNAME in text  # @名字
        assert "可能会对这个感兴趣" in text
        assert "怎么知道" in text or "别说" in text  # 嘱咐不说画像细节
        # 不含 persona 片段（「在折腾 FPGA 的小项目」是 persona summary，绝不能出现）
        assert "在折腾 FPGA 的小项目" not in text
        # 6 小时 ttl
        assert abs((float(rows[0]["expires_ts"]) - NOW) - 6 * 3600) < 60
        # 不发群消息：没有 outbox 行
        n_outbox = store.read().execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
        assert n_outbox == 0

    def test_no_persona_fragment_in_memo(self, tmp_path) -> None:
        """persona 里任何 8 字以上片段都不能进备忘文字。"""
        store, settings, personal, *_ = _make_personal(tmp_path)
        news_id = _seed_personal_news(store, UID, GID)
        mentions = self._mentions(store, settings)
        with _time_patch(), _delivery_time_patch():
            personal.mention_to_member(GID, news_id, mentions=mentions)
        text = store.read().execute("SELECT text FROM mentions WHERE group_id=?", (GID,)).fetchone()["text"]
        persona = json.loads(_persona_json())
        for field in (
            persona.get("summary") or "",
            *persona.get("doing"),
            *persona.get("cares"),
            *persona.get("asked"),
        ):
            f = str(field)
            if len(f) >= 8:
                assert f not in text


# ----------------------------------------------------------------------
# 调度：时间窗 / 每群每轮 1 个 / personal_feeds=false
# ----------------------------------------------------------------------


class TestSchedule:
    def test_window_9_to_22(self, tmp_path) -> None:
        """北京时间 9:00–22:00 之外不给到期的人跑。"""
        from CharTyr_MaiWork.maiwork.personal import in_personal_window

        with _time_patch():
            assert in_personal_window(_bj_ts(2026, 8, 31, 15)) is True   # 15 点（窗内）
            assert in_personal_window(_bj_ts(2026, 8, 31, 9)) is True    # 9 点（边界起，含）
            assert in_personal_window(_bj_ts(2026, 8, 31, 8)) is False   # 8 点
            assert in_personal_window(_bj_ts(2026, 8, 31, 23)) is False  # 23 点
            assert in_personal_window(_bj_ts(2026, 8, 31, 22)) is False  # 22 点（边界止，不含）

    def test_personal_feeds_off_no_due(self, tmp_path) -> None:
        """personal_feeds=false：due 给空，没有人被挑上。"""
        store, settings, personal, *_ = _make_personal(
            tmp_path, cfg={"focus": {"personal_feeds": False}}
        )
        assert settings.focus.personal_feeds is False
        assert personal.due(GID, NOW) == []

    def test_due_one_per_group_per_round(self, tmp_path) -> None:
        """每群每轮最多给 1 个：due 只回一个人；当天已跑过的不回。"""
        store, settings, personal, *_ = _make_personal(tmp_path)
        _add_focus_member(store, UID2, GID, name="阿二")  # 第二个有画像的人
        with _time_patch():
            due = personal.due(GID, NOW)
        assert len(due) == 1
        personal.mark_done(GID, due[0], NOW)
        with _time_patch():
            due2 = personal.due(GID, NOW)
        assert due[0] not in due2


# ----------------------------------------------------------------------
# focus_personal_view（GroupView.focus[].personal 的数据源）
# ----------------------------------------------------------------------


class TestFocusPersonalView:
    def test_personal_structure(self, tmp_path) -> None:
        """{news:≤5, ideas:≤2, last_ts}；按 target_user_id 分到各人；不混入别人的。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store, GID, ready=True)
        _add_focus_member(store, UID, GID, name=UNAME)
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 0, 0, 0, '', ?)", (GID, NOW, NOW),
            )
            bid = int(cur.lastrowid or 0)
            for i in range(6):
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, created, kind, rejected,"
                    " target_user_id, status_kind) VALUES (?, ?, ?, ?, 'news', 0, ?, 'pool')",
                    (bid, GID, f"个人资讯{i}", NOW - i * 1000, UID),
                )
            for i in range(3):
                conn.execute(
                    "INSERT INTO ideas (group_id, title, state, created, updated, target_user_id)"
                    " VALUES (?, ?, 'new', ?, ?, ?)",
                    (GID, f"个人构想{i}", NOW - i * 1000, NOW - i * 1000, UID),
                )
            # 别人的
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, created, kind, rejected,"
                " target_user_id, status_kind) VALUES (?, ?, '给阿二的', ?, 'news', 0, ?, 'pool')",
                (bid, GID, NOW, UID2),
            )
        from CharTyr_MaiWork.maiwork.personal import Personal

        settings = _settings()
        personal = Personal(
            store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: settings
        )
        with _time_patch():
            out = personal.focus_personal_view(GID, UID, days=7)
        assert set(out.keys()) == {"news", "ideas", "last_ts"}
        assert len(out["news"]) == 5   # 最多 5 条
        assert len(out["ideas"]) == 2  # 最多 2 条
        assert all("id" in n and "title" in n for n in out["news"])
        assert out["last_ts"] is not None and float(out["last_ts"]) > 0
        assert all("给阿二" not in str(n.get("title") or "") for n in out["news"])

    def test_personal_refs_use_current_name(self, tmp_path) -> None:
        """个人向 refs 的 who 按 user_id 查当前名；视图不带 user_id。"""
        from CharTyr_MaiWork.maiwork import members

        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store, GID, ready=True)
        _add_focus_member(store, UID, GID, name=UNAME)
        nid = _seed_personal_news(store, UID, GID)
        with store.tx() as conn:
            conn.execute(
                "UPDATE news_items SET refs=? WHERE id=?",
                (json.dumps([{"ts": NOW - 100, "who": UNAME, "user_id": UID,
                              "text": "想找个便宜的学习板", "message_id": "m1"}], ensure_ascii=False), nid),
            )
            members.record(conn, GID, UID, "阿帆改了名", 1e10)
        from CharTyr_MaiWork.maiwork.personal import Personal

        personal = Personal(
            store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: _settings()
        )
        with _time_patch():
            out = personal.focus_personal_view(GID, UID, days=7)
        item = next(n for n in out["news"] if n["id"] == nid)
        assert item["refs"][0]["who"] == "阿帆改了名"
        assert "user_id" not in item["refs"][0]

    def test_personal_empty_for_unknown(self, tmp_path) -> None:
        """没做过画像 / 不是关注成员的人：personal 是空结构，last_ts 为 null。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store, GID, ready=True)
        from CharTyr_MaiWork.maiwork.personal import Personal

        settings = _settings()
        personal = Personal(
            store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: settings
        )
        with _time_patch():
            out = personal.focus_personal_view(GID, "nobody", days=7)
        assert out == {"news": [], "ideas": [], "last_ts": None}


# ----------------------------------------------------------------------
# POST /api/news/{id}/mention-to-member（console/server 路由 + 鉴权）
# ----------------------------------------------------------------------


def _seed_personal_store(store: Store, gid: str = GID, uid: str = UID) -> int:
    """起好了 app 的库：种一条个人向资讯 + 一个带画像的关注成员。返回 news_items.id。"""
    _add_focus_member(store, uid, gid)
    return _seed_personal_news(store, uid, gid)


def _app_config(tmp_path: Path, listen: str = "") -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "console": {"listen": listen or f"127.0.0.1:{_port()}", "password": "pw-测试", "public_url": ""},
        "storage": {"data_dir": str(tmp_path)},
    }


class _fake_clock:
    """把 app / clock 里读到的「现在」钉在指定时刻（调度时间窗测试用）。"""

    def __init__(self, ts: float):
        self._ts = ts

    def __enter__(self):
        clock.now = lambda: self._ts
        import CharTyr_MaiWork.maiwork.app as app_mod

        self._app_mod = app_mod
        self._app_orig = app_mod._now
        app_mod._now = lambda: self._ts
        return self

    def __exit__(self, *exc):
        self._app_mod._now = self._app_orig


@pytest.mark.asyncio
class TestMentionRoute:
    async def _setup(self, tmp_path: Path):
        import aiohttp
        from aiohttp.test_utils import TestClient, TestServer

        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), _app_config(tmp_path / "data-mention"), plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        assert app.started and app.personal is not None
        news_id = _seed_personal_store(app.store)
        server = TestServer(app.console.app)
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        return app, client, news_id

    async def _teardown(self, app, client) -> None:
        await client.close()
        await app.stop()

    async def test_admin_ok_and_no_group_send(self, tmp_path) -> None:
        """管理员点「在群里提给他」：{"ok": true}；可提起清单多一句；不发任何群消息。"""
        app, client, news_id = await self._setup(tmp_path)
        try:
            await client.post("/api/login", json={"password": "pw-测试"})
            r = await client.post(f"/api/news/{news_id}/mention-to-member", json={})
            assert r.status == 200
            data = await r.json()
            assert data == {"ok": True}
            rows = app.store.read().execute(
                "SELECT text FROM mentions WHERE group_id=?", (GID,)
            ).fetchall()
            assert len(rows) == 1
            assert UNAME in rows[0]["text"]
            n_outbox = app.store.read().execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
            assert n_outbox == 0  # 不直接发群消息
        finally:
            await self._teardown(app, client)

    async def test_member_forbidden(self, tmp_path) -> None:
        """群友调 mention-to-member → 403；匿名 → 401。"""
        app, client, news_id = await self._setup(tmp_path)
        try:
            token = app.token_of(GID)
            r = await client.post(
                f"/api/news/{news_id}/mention-to-member",
                json={},
                headers={"X-MW-Group": token},
            )
            assert r.status == 403
            r2 = await client.post(f"/api/news/{news_id}/mention-to-member", json={})
            assert r2.status == 401
        finally:
            await self._teardown(app, client)

    async def test_group_news_not_found(self, tmp_path) -> None:
        """拿群向资讯（target_user_id 为空）来点 → 404。"""
        app, client, news_id = await self._setup(tmp_path)
        try:
            with app.store.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, 1, 1, 0, '', ?)", (GID, NOW, NOW),
                )
                bid = int(cur.lastrowid or 0)
                cur = conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, title, created, kind, rejected,"
                    " target_user_id) VALUES (?, ?, '群向的', ?, 'news', 0, '')",
                    (bid, GID, NOW),
                )
                group_news_id = int(cur.lastrowid or 0)
            await client.post("/api/login", json={"password": "pw-测试"})
            r = await client.post(f"/api/news/{group_news_id}/mention-to-member", json={})
            assert r.status == 404
            r2 = await client.post("/api/news/99999/mention-to-member", json={})
            assert r2.status == 404
        finally:
            await self._teardown(app, client)


# ----------------------------------------------------------------------
# app 后台调度：时间窗 / 每群每轮 1 个 / 不重复 spawn
# ----------------------------------------------------------------------


class _FakePersonal:
    """假的 Personal（app 用）：记 prepare / mark_done 调用。"""

    def __init__(self, due_list: list) -> None:
        self._due = due_list
        self.calls: List[str] = []
        self.done_calls: List[str] = []

    def due(self, group_id: str, now: float) -> list:
        return list(self._due)

    def mark_done(self, group_id: str, user_id: str, now: float) -> None:
        self.done_calls.append(str(user_id))

    async def prepare_personal(self, group_id: str, user_id: str) -> int:
        self.calls.append(str(user_id))
        return 1


@pytest.mark.asyncio
class TestAppScheduling:
    async def _app(self, tmp_path: Path, personal):
        from CharTyr_MaiWork.maiwork.app import MaiWorkApp

        raw = _app_config(tmp_path / "data-sched")
        raw["models"] = {"base_url": "http://127.0.0.1:9/v1", "api_key": "test-key", "main": "m1"}
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        app.personal_factory = lambda *a, **k: personal
        await app.start()
        assert app.started
        return app

    async def _drain(self, app) -> None:
        for t in list(app._bg_jobs):
            try:
                await t
            except Exception:
                pass

    async def test_window_outside_no_spawn(self, tmp_path) -> None:
        """北京时间 23 点（窗外）：due 该给空 → 不 spawn、不准备。"""
        # 真 Personal 对象走真 due：窗在 9–22 点；23 点没人被挑上
        store = Store(tmp_path / "w.db")
        store.migrate()
        _seed_group(store)
        _add_focus_member(store, UID)
        settings = _settings()
        from CharTyr_MaiWork.maiwork.personal import Personal

        personal = Personal(store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: settings)
        with _fake_clock(_bj_ts(2026, 8, 31, 23)):
            assert personal.due(GID, clock.now()) == []

    async def test_one_per_group_per_round_and_no_duplicate(self, tmp_path) -> None:
        """due 只给一个人；在跑时不再重复 spawn；跑完记 mark_done。"""
        personal = _FakePersonal([UID, UID2])  # 即便 due 塞俩，app 也只挑第一个
        app = await self._app(tmp_path, personal)
        try:
            with _fake_clock(NOW):
                await app.run_loop_once()
            await self._drain(app)
            assert len(personal.calls) == 1  # 每群每轮最多 1 个
            assert personal.calls[0] == UID
            assert personal.done_calls == [UID]  # 跑完记了「今天做过」
        finally:
            await app.stop()

    async def test_running_job_not_duplicated(self, tmp_path) -> None:
        """同一群同一时刻只开一个：长活在跑时再来一轮不再 spawn。"""
        personal = _FakePersonal([UID])

        async def slow_prepare(group_id: str, user_id: str) -> int:
            personal.calls.append(str(user_id))
            await asyncio.sleep(0.5)  # 慢活：下一轮来时还在跑
            return 1

        personal.prepare_personal = slow_prepare
        app = await self._app(tmp_path, personal)
        try:
            with _fake_clock(NOW):
                await app.run_loop_once()
                await asyncio.sleep(0.05)  # 让它跑起来
                await app.run_loop_once()  # 还在跑：这一轮不应再 spawn
            await self._drain(app)
            assert personal.calls == [UID]  # 只开过一次
        finally:
            await app.stop()

    async def test_personal_feeds_off_app_no_spawn(self, tmp_path) -> None:
        """personal_feeds=false：真 due 给空 → app 不 spawn。"""
        store = Store(tmp_path / "f.db")
        store.migrate()
        _seed_group(store)
        _add_focus_member(store, UID)
        settings = _settings({"focus": {"personal_feeds": False}})
        from CharTyr_MaiWork.maiwork.personal import Personal

        personal = Personal(store, FakeModelsQueue(), FakeWorkers(), FakeProfiles(), FakeTopics(), lambda: settings)
        with _fake_clock(NOW):
            assert personal.due(GID, clock.now()) == []


def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_PORT: list[int] = []


def _port() -> int:
    """整个测试文件只挑一次：同一测试里前后两份配置端口要一样，不然会被当成「改了网页地址」重启。"""
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]
