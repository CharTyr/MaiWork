"""看板过程明细的「精确关联」（库号 47，2026-10-29）。

三类条目（资讯轮 news_batch / 构想 idea / 开话题 topic）的过程以前靠「同群 + 时间窗」
猜（link="time_window"），同群同时段别的活也会混进来。现在这几轮开工时在外层挂一个
round ref（`news:<标记>` / `idea:<标记>` / `topic:<标记>`，contextvars，和 main_runs
的 run_ref 同一个机制），轮里的 model_calls / tool_calls（含子 agent / 专岗交接单里
的调用）自动带上；条目自己也存一份 ref（news_batches / ideas / topic_log / judgments
各加 run_ref 列），看板 item 详情按它直连 → link="exact"。老记录（run_ref 空）照旧走
时间窗。

这些轮里的调用不是「主模型独立判断」：挂 round ref 的调用不许自己开 main_runs 条目
（models.chat 的自动开只看 main_runs 的 CURRENT_REF，round scope 不碰它——资讯 / 构想
的调用本来 agent 不是 main 或带 task_id 也不会开；开话题的开场白 agent=main 无 task_id，
是真可能开条目的那一路）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.board import item_view
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.jev import Jev
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.topics import Topics

from fakes import (
    FakeHost,
    FakeModelsQueue,
    FakeProfiles,
    SignalsStub,
    focus_reply,
    patch_two_phase_feeds,
    seed_idea_chat,
)
from tests.test_feeds import FakeTopics, FakeWorkers

BJ = timezone(timedelta(hours=8))
GID = "900000001"
SID = "sess-1"
SECRET = "sk-exact-link-test"
NOW = 1_790_000_000.0


def _settings(cfg: dict | None = None):
    merged = {
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "endpoints": [{"id": "e1", "protocol": "openai",
                       "base_url": "https://e.test/v1", "api_key": SECRET}],
        "model_list": [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
    }
    if cfg:
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
    settings, _ = load_settings(merged)
    return settings


class _Svc:
    """item_view 需要的最小服务对象。"""

    def __init__(self, store, groups=(GID,)):
        self.store = store
        self._groups = groups

    def get_settings(self):
        return _settings()


def _routing_models(store, settings, by_key):
    """真 Models + MockTransport，按请求体里的特征键路由到不同回复（模拟各 purpose）。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode("utf-8", "replace")
        for key, text in by_key.items():
            if key in body:
                return _oai(text)
        return _oai('{"note":"默认"}')
    return _real_models(store, settings, handler=handler)


def _oai(text: str = "好的") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    })


def _real_models(store, settings, handler=None, agent_map=None):
    from tests.test_main_runs import _FakeAgents
    return Models(
        store, lambda: settings,
        transport=httpx.MockTransport(handler or (lambda req: _oai())),
        agents=_FakeAgents(agent_map or {"main": {"model": "m1"}, "task": {"model": "m1"},
                                         "news": {"model": "m1"}, "idea": {"model": "m1"}}),
    )


def _seed_group(store, gid=GID):
    with store.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, profile_ready_ts, session_id, last_msg_ts)"
            " VALUES (?, ?, ?, ?)",
            (gid, 1_700_000_000.0, SID, NOW - 3600.0),
        )


def _fake_feeds(store, settings, models=None, profiles=None):
    """建一个接线最少的 Feeds：FakeWorkers（不派子 agent）、FakeTopics、FakeProfiles。"""
    from CharTyr_MaiWork.maiwork.feeds import Feeds
    return Feeds(
        store, models or FakeModelsQueue(), FakeWorkers(store),
        profiles or FakeProfiles(), FakeTopics(), lambda: settings,
    )


# ----------------------------------------------------------------------
# 库号 47：列就位
# ----------------------------------------------------------------------


class TestSchema:
    def test_run_ref_columns_exist(self, tmp_path):
        store = Store(tmp_path / "t.db")
        assert store.migrate() == 47
        for table in ("news_batches", "ideas", "topic_log", "judgments"):
            cols = {r["name"] for r in store.read().execute(f"PRAGMA table_info({table})")}
            assert "run_ref" in cols, table

    def test_migration_count(self):
        from CharTyr_MaiWork.maiwork.store import _MIGRATIONS
        assert len(_MIGRATIONS) == 47


# ----------------------------------------------------------------------
# 资讯轮：run_ref 精确挂 + 条目存 ref + 不混同群别的活
# ----------------------------------------------------------------------


class TestNewsExactLink:
    @pytest.mark.asyncio
    async def test_round_ref_attached_and_batch_stores_it(self, tmp_path):
        """prepare_news 走真代码：轮里的 model_calls / tool_calls 带同一个 run_ref，
        news_batches 行存上这个 ref；detail link=exact，同群同时段别的活不混进来。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store)
        settings = _settings()

        items = [{"title": "测试资讯一", "url": "https://a.test/1",
                  "summary": "摘要", "kind": "news", "published": NOW - 3600,
                  "fetched": True, "quote": "原文确有", "paywall": False}]
        # 真 Models：打分 / 写帖 / 自检的调用真落 model_calls，run_ref 真写进库里
        scores = json.dumps({"scores": [
            {"i": 0, "title": "测试资讯一", "info": 5, "source": 4, "relevance": 5,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "硬件", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "群里用得上的硬件", "icon": "tools"},
        ]}, ensure_ascii=False)
        posts = json.dumps({"posts": [
            {"i": 0, "title": "测试资讯一", "body": "这个新硬件挺值得看",
              "brief": "硬件短摘要", "verified": True},
        ]}, ensure_ascii=False)
        models = _routing_models(store, settings, {
            '"focus"': focus_reply("开源硬件", "本地大模型", "掌机社区"),
            '"note"': '{"note":"全要"}',       # 挑（两阶段的 pick）
            '"scores"': scores,                 # 打分
            '"posts"': posts,                   # 写帖
            '"unsupported"': '{"unsupported":[]}',  # 展示自检
        })
        feeds = _fake_feeds(store, settings, models=models)
        patch_two_phase_feeds(feeds, models, items)

        await feeds.prepare_news(GID)
        batches = store.read().execute(
            "SELECT id, run_ref FROM news_batches ORDER BY id").fetchall()
        assert batches, "一轮资讯应该留一条批次（哪怕 skipped）"
        ref = str(batches[-1]["run_ref"] or "")
        assert ref.startswith("news:"), f"批次要存本轮的 run_ref，实际 {ref!r}"
        # 轮里的模型调用都挂上它（打分的 agent=news、写帖的 agent=news）
        rows = store.read().execute(
            "SELECT run_ref FROM model_calls WHERE group_id=?", (GID,)).fetchall()
        assert rows, "这一轮应该有模型调用落库"
        assert all(str(r["run_ref"] or "") == ref for r in rows), (
            "这一轮的全部模型调用都要挂同一个 run_ref")
        # main_runs 一条都不该开（这些调用 agent 不是 main / 带 task_id）
        assert store.read().execute("SELECT COUNT(*) c FROM main_runs").fetchone()["c"] == 0
        # detail：exact，且步骤就是这一轮挂 ref 的
        view = item_view(_Svc(store), "news_batch", int(batches[-1]["id"]))
        assert view is not None
        assert view["link"] == "exact"
        assert view["steps"], "挂 ref 的调用应该出现在 steps 里"

        # 同群同时段别的活（别的 run_ref）不混进来
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id,"
                " attempt, ok, status, ms, prompt_tokens, completion_tokens, error, request,"
                " response, usage_src, cache_read, cache_write, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (clock.now(), "chat", "main", "main", "m1", GID, "", 1, 1, 200, 10, 1, 1, "",
                 "{}", json.dumps({"text": "同群别的事在判", "tool_calls": [],
                                    "finish_reason": "stop"}), "reported", 0, 0, "main:x:1:1:1"),
            )
        view2 = item_view(_Svc(store), "news_batch", int(batches[-1]["id"]))
        assert view2["link"] == "exact"
        texts = [s.get("text") or "" for s in view2["steps"]]
        assert not any("同群别的事" in t for t in texts), "别的 run_ref 的调用不许混进来"

    def test_old_batch_falls_back_time_window(self, tmp_path):
        """老记录（run_ref 空）照旧 time_window。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        now = clock.now()
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?,?,?,?,?,?,?)", (GID, now - 200, 5, 2, 0, "", now - 200))
            bid = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id,"
                " attempt, ok, status, ms, prompt_tokens, completion_tokens, error, request,"
                " response, usage_src, cache_read, cache_write, run_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now - 210, "feeds.score", "worker", "news", "m1", GID, "feeds-collect:g:1:1",
                 1, 1, 200, 10, 1, 1, "", "{}",
                 json.dumps({"text": "打分", "tool_calls": [], "finish_reason": "stop"}),
                 "reported", 0, 0, ""),
            )
        view = item_view(_Svc(store), "news_batch", bid)
        assert view is not None
        assert view["link"] == "time_window"
        assert any(s["type"] == "think" for s in view["steps"])


# ----------------------------------------------------------------------
# 构想轮：一轮可能出多条，都指向同一个 run_ref
# ----------------------------------------------------------------------


class TestIdeaExactLink:
    @pytest.mark.asyncio
    async def test_idea_row_stores_round_ref(self, tmp_path):
        """make_idea 走完一轮：ideas 行带本轮 run_ref，detail link=exact。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        _seed_group(store)
        settings = _settings()
        seed_idea_chat(store, GID, now=clock.now())

        from fakes import idea_reply
        reply = idea_reply()
        # 真 Models：feeds.idea 的调用真落 model_calls，run_ref 真写进库里
        models = _real_models(store, settings, handler=lambda req: _oai(reply))
        feeds = _fake_feeds(store, settings, models=models)
        idea_id = await feeds.make_idea(GID)
        assert idea_id, "应该出一条构想"
        row = store.read().execute(
            "SELECT run_ref FROM ideas WHERE id=?", (int(idea_id),)).fetchone()
        ref = str(row["run_ref"] or "")
        assert ref.startswith("idea:"), f"构想行要存本轮 run_ref，实际 {ref!r}"
        calls = store.read().execute(
            "SELECT run_ref FROM model_calls WHERE group_id=?", (GID,)).fetchall()
        assert calls and all(str(r["run_ref"]) == ref for r in calls)
        view = item_view(_Svc(store), "idea", int(idea_id))
        assert view is not None and view["link"] == "exact"
        assert view["steps"]

    def test_old_idea_falls_back_time_window(self, tmp_path):
        store = Store(tmp_path / "t.db")
        store.migrate()
        now = clock.now()
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, title, body, state, created, updated)"
                " VALUES (?,?,?,?,?,?)", (GID, "老构想", "", "new", now - 300, now - 300))
            iid = int(cur.lastrowid or 0)
        view = item_view(_Svc(store), "idea", iid)
        assert view is not None and view["link"] == "time_window"


# ----------------------------------------------------------------------
# 开话题：Jev 判断（judgments）也挂同轮 run_ref，steps 里出 think 摘要
# ----------------------------------------------------------------------


def _seed_candidate(store, *, check_now, title="测试候选", ref_id=7):
    expires = float(check_now) + 12 * 3600.0
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link,"
            " expires_ts, used_ts, created) VALUES (?,?,?,?,?,?,?,NULL,?)",
            (GID, "news", ref_id, title, "简报", "http://x", expires, check_now - 60),
        )


class TestTopicExactLink:
    @pytest.mark.asyncio
    async def test_topic_log_and_judgment_share_round_ref(self, tmp_path):
        """一次开话题判断：Jev 的 judgments 行和 topic_log 行挂同一个 topic:<标记>；
        detail link=exact，Jev 判断结果作为 think 步出现（不放群友原话）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        # Jev 应答（systemone 形状）：可以开 + 候选 0 合适
        jev_answers = {"answers": {
            "ok": {"type": "noul", "noul": 0.9},
            "reason": {"type": "choice", "choice": "fine",
                        "probabilities": {"fine": 0.9, "left": 0.04,
                                           "open_question": 0.03, "mood": 0.03},
                        "confidence": 0.9},
            "fit_0": {"type": "noul", "noul": 0.8},
        }}
        jev = Jev(store, lambda: _settings({"jev": {"api_key": "k-test"}}),
                  transport=httpx.MockTransport(lambda req: httpx.Response(200, json=jev_answers)))
        settings = _settings({"topics": {"enabled": True, "min_gap_hours": 0},
                              "jev": {"api_key": "k-test"}})
        host = FakeHost(msgs=[], session_id=SID)
        profiles = FakeProfiles()
        profiles.usual_gap_value = 300.0
        signals = SignalsStub()
        signals.mark(GID, SID, NOW - 3600.0)
        mentions = Mentions(store, lambda: settings)
        pushes = Pushes(store, lambda: settings)
        outbox = Outbox(store, host, pushes, mentions, lambda: settings)
        topics = Topics(store, host, FakeModelsQueue(ready=True, replies=["好话题，来聊聊？"]),
                        jev, profiles, mentions, pushes, lambda: settings, signals,
                        outbox=outbox)
        T = NOW
        _seed_candidate(store, check_now=T)
        out = await topics.check(GID, T)
        assert out, "check 得给个说法"
        topic_row = store.read().execute(
            "SELECT id, run_ref FROM topic_log ORDER BY id DESC LIMIT 1").fetchone()
        assert topic_row is not None, "判过就该有 topic_log（不管开没开）"
        ref = str(topic_row["run_ref"] or "")
        assert ref.startswith("topic:"), f"topic_log 要存本轮 run_ref，实际 {ref!r}"
        judg = store.read().execute(
            "SELECT run_ref FROM judgments WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (GID,)).fetchone()
        assert judg is not None and str(judg["run_ref"]) == ref, (
            "Jev 判断要挂同一轮的 run_ref")
        view = item_view(_Svc(store), "topic", int(topic_row["id"]))
        assert view is not None and view["link"] == "exact"
        thinks = [s for s in view["steps"] if s["type"] == "think"]
        assert thinks, "steps 里要有 Jev 判断的 think 步"
        # think 文本是「判断结果摘要」（有没有开成、原因），不是群友原话
        joined = " ".join(s["text"] for s in thinks)
        assert "判断" in joined or "开" in joined or "可以开" in joined
        assert "群友" not in joined

    def test_old_topic_falls_back_time_window(self, tmp_path):
        store = Store(tmp_path / "t.db")
        store.migrate()
        now = clock.now()
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick,"
                " candidate_id, opener, message_id, followup_due_ts, result, verdict)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (GID, now - 60, 300.0, None, "{}", "{}", None, "x", "", None, None, None))
            tid = int(cur.lastrowid or 0)
        view = item_view(_Svc(store), "topic", tid)
        assert view is not None and view["link"] == "time_window"


# ----------------------------------------------------------------------
# 这些轮里的主模型调用不进主模型栏（main_runs）
# ----------------------------------------------------------------------


class TestNoAutoMainRun:
    @pytest.mark.asyncio
    async def test_main_chat_inside_round_scope_does_not_open_main_run(self, tmp_path):
        """round scope 里 agent=main、无 task_id 的调用（开话题开场白就是这种）
        挂本轮的 ref，不自己开 main_runs 条目。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        models = _real_models(store, settings)
        from CharTyr_MaiWork.maiwork import run_ref
        async with run_ref.tagged(run_ref.new("topic", GID)):
            await models.chat(agent="main", messages=[{"role": "user", "content": "hi"}],
                              purpose="opener", group_id=GID)
        assert store.read().execute("SELECT COUNT(*) c FROM main_runs").fetchone()["c"] == 0
        row = store.read().execute("SELECT run_ref FROM model_calls").fetchone()
        assert row is not None and str(row["run_ref"]).startswith("topic:")

    @pytest.mark.asyncio
    async def test_unrelated_main_call_still_opens_main_run(self, tmp_path):
        """没在 round scope 里的 main 调用照旧自动开 main_runs（不退化老行为）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        models = _real_models(store, settings)
        await models.chat(agent="main", messages=[{"role": "user", "content": "hi"}],
                          purpose="admin_chat", group_id=GID)
        assert store.read().execute("SELECT COUNT(*) c FROM main_runs").fetchone()["c"] == 1
