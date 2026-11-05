"""看板精修第一步：main_runs 分栏（lane）、交接单挂 run_ref、轮次「正在跑」实时上板、闲时探索归通用任务栏。

规矩（实现要点见 maiwork/store.py 第 50 步迁移、run_ref.py 的 active_rounds、
console/board/sources/round.py、main_run.py 的 lane）：

- `main_runs` 多一列 `lane`（默认 'main'）：同一类条目可以归到别的栏（比如
  闲时探索归 task 栏）；`idx_main_runs_lane_started` 索引跟着建。
- `agent_handoffs` 多一列 `run_ref`：交接单记下自己那一轮的 ref；无外圈 ref 时
  给它一个 `handoff:<交接单号>` 自己的 ref，worker 那轮的工具/模型调用挂得上。
- 轮在跑就显示在对应栏：资讯/构想/开话题/目标检查过完前置检查后（settle /
  skip 不算），登记进 run_ref 的 active 名册 → board 的 round 源把它显示成
  「正在跑」；item_view('round', ref) 活的时候给实时步骤，轮结束了回落到
  已落库的条目（news_batches / ideas / topic_log / goals 里 run_ref 对上的行）。
- 闲时探索 `skill_explore.run` 整轮包在 MainRuns.scope(lane="task") 里：
  跑的时候 task 栏有条「闲时探索做法」；跑完标 done / failed（标题按结果改写），
  main 栏不会多出孤儿「skill_explore.pick」条目。
- 非服务群：名册里的轮不上板，item_view 也拒绝看（零暴露）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import asyncio
import aiohttp
import httpx
import pytest
import pytest_asyncio
import tomlkit
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import clock, run_ref, skill_explore
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.board import board_view, item_view
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.jev import Jev
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import _MIGRATIONS, Store
from CharTyr_MaiWork.maiwork.topics import Topics

import fakes
from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    NOW,
    CaptureWorkers,
    FakeProfiles,
    FakeSkills,
    ModelsQueue,
    _add_worker_tool,
    agents,
    env,
    fixed_clock,
    settings,
    store,
    tools,
)
from test_board import PASSWORD as _BOARD_PW
from test_board import _login as _http_login
from test_board import _raw_config as _http_raw_config
from test_feeds import FakeTopics, FakeWorkers
from test_goal_heartbeat import _build_coordinator
from test_skill_explore import _PICK, _PICK_SKIP, _VALID_MD, _explore_tid, _report, _seed_calls
from CharTyr_MaiWork.maiwork.workers import WorkerReport


# ----------------------------------------------------------------------
# 最小服务对象 / helpers
# ----------------------------------------------------------------------


class _Svc:
    """board_view / item_view 需要的最小服务对象（和 test_board_golden 同款）。"""

    def __init__(self, store, settings, *, agents=None):
        self.store = store
        self._settings = settings
        self.agents = agents

    def get_settings(self):
        return self._settings


def _loaded_settings(groups=(GID,)):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}", "workspace": "g1"} for g in groups]},
        "console": {"board_hours": 24},
        "models": {"base_url": "https://ep.test/v1", "api_key": "k",
                   "main": "m", "worker": "w"},
    }
    loaded, problems = load_settings(raw)
    assert loaded is not None, problems
    return loaded


def _columns(store: Store, table: str) -> set:
    return {str(r["name"]) for r in store.read().execute(f"PRAGMA table_info({table})")}


def _index_names(store: Store, table: str) -> set:
    rows = store.read().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    )
    return {str(r["name"]) for r in rows}


def _run_steps(store: Store, upto: int) -> None:
    """顺序跑迁移前 upto 步（模仿存量库），user_version 落成 upto。"""
    for fn in _MIGRATIONS[:upto]:
        fn(store.read())
    store.read().execute(f"PRAGMA user_version={upto}")


def _lane(data: dict, kind: str) -> dict:
    return next((x for x in data["lanes"] if x["kind"] == kind), None) or {
        "running": [], "history": [], "counts": {"running": 0},
    }


# ----------------------------------------------------------------------
# 库号 50：main_runs.lane / agent_handoffs.run_ref
# ----------------------------------------------------------------------


class TestSchema50:
    def test_fresh_migrate_reaches_50(self, tmp_path: Path) -> None:
        s = Store(tmp_path / "t.db")
        try:
            assert s.migrate() == 50
            assert int(s.read().execute("PRAGMA user_version").fetchone()[0]) == 50
            assert len(_MIGRATIONS) == 50
            assert "lane" in _columns(s, "main_runs")
            assert "idx_main_runs_lane_started" in _index_names(s, "main_runs")
        finally:
            s.close()

    def test_upgrade_49_adds_lane_and_run_ref(self, tmp_path: Path) -> None:
        """49 存量库升 50：main_runs 补 lane（旧行默认 'main'）+ 索引；
        agent_handoffs（第 49 步还不存在的懒建表）手工补出来后补 run_ref + 索引。"""
        s = Store(tmp_path / "v49.db")
        try:
            _run_steps(s, 49)
            conn = s.read()
            # 模拟老库：49 的 main_runs 没有 lane 列（第 50 步前真实如此）
            cols49 = _columns(s, "main_runs")
            assert "lane" not in cols49
            conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started,"
                " finished, ok, error, ref) VALUES (?,?,?,?,?,?,?,?,?)",
                (GID, "admin_chat", "回答管理员", "done", NOW - 100, NOW - 90, 1, "",
                 "main:admin_chat:g:1:0"),
            )
            # 懒建表：49 时可能已被 Agents._ensure_schema 建过（当时没有 run_ref 列）
            conn.executescript("""
CREATE TABLE IF NOT EXISTS agent_handoffs (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    brief TEXT NOT NULL DEFAULT '',
    criteria TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    review TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_handoffs_group ON agent_handoffs(group_id, created);
CREATE INDEX IF NOT EXISTS idx_agent_handoffs_task ON agent_handoffs(task_id, created);
CREATE INDEX IF NOT EXISTS idx_agent_handoffs_status ON agent_handoffs(status, updated);
""")
            conn.execute(
                "INSERT INTO agent_handoffs (id, group_id, kind, task_id, status,"
                " brief, created, updated) VALUES ('h1', ?, 'task', 'T-1', 'done',"
                " '做个网页', ?, ?)",
                (GID, NOW - 50, NOW - 40),
            )
            assert "run_ref" not in _columns(s, "agent_handoffs")

            assert s.migrate() == 50
            assert "lane" in _columns(s, "main_runs")
            assert "idx_main_runs_lane_started" in _index_names(s, "main_runs")
            row = conn.execute("SELECT lane FROM main_runs WHERE ref=?",
                               ("main:admin_chat:g:1:0",)).fetchone()
            assert str(row["lane"]) == "main"
            assert "run_ref" in _columns(s, "agent_handoffs")
            assert "idx_agent_handoffs_run_ref" in _index_names(s, "agent_handoffs")
            h = conn.execute("SELECT run_ref FROM agent_handoffs WHERE id='h1'").fetchone()
            assert str(h["run_ref"]) == ""
        finally:
            s.close()

    def test_upgrade_49_without_handoffs_table(self, tmp_path: Path) -> None:
        """agent_handoffs 还没懒建出来时升 50：迁移不炸、不越权建表；
        之后 Agents._ensure_schema 建出的表直接带 run_ref 列。"""
        s = Store(tmp_path / "v49b.db")
        try:
            _run_steps(s, 49)
            assert "agent_handoffs" not in {
                str(r["name"]) for r in s.read().execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert s.migrate() == 50
            # Agents._ensure_schema 建的懒表带上 run_ref
            settings = _loaded_settings()
            a = Agents(s, lambda: settings)
            a._ensure_schema()  # noqa: SLF001
            cols = _columns(s, "agent_handoffs")
            assert "run_ref" in cols
            assert "idx_agent_handoffs_run_ref" in _index_names(s, "agent_handoffs")
        finally:
            s.close()


# ----------------------------------------------------------------------
# 交接单 run_ref：外圈 ref 优先，没有就用自己的 handoff:<id>
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandoffRunRef:
    def _specialists(self, agents_obj, workers):
        from CharTyr_MaiWork.maiwork.specialists import Specialists
        return Specialists(agents_obj, workers, FakeSkills())

    def _handoff_row(self, store_obj, hid: str) -> dict:
        r = store_obj.read().execute(
            "SELECT * FROM agent_handoffs WHERE id=?", (hid,)).fetchone()
        assert r is not None
        return dict(r)

    async def test_outer_ref_stored_on_handoff(self, store, agents):
        workers = CaptureWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=[])])
        sp = self._specialists(agents, workers)
        outer = "news:outer:test:1"
        async with run_ref.tagged(outer):
            rep = await sp.run("news", "找两条资讯", group_id=GID, task_id="T-1")
        assert rep.ok, rep
        hid = str(rep.handoff_id or "")
        row = self._handoff_row(store, hid)
        assert row["run_ref"] == outer

    async def test_no_outer_ref_gets_own_handoff_ref(self, store, agents):
        """run_ref.current() 为空时：workers.run 包进 handoff:<id> 自己的 ref，
        交接单存上它；它轮里的工具调用按 ref 精确挂，task_id 保持 T-1 不变。"""
        seen: dict = {}

        async def _peek():
            seen["ref"] = run_ref.current()
            # 模拟子 agent 那轮写的一条工具调用（tools._persist 同款挂法）
            with store.tx() as conn:
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool,"
                    " input, output, ms, ok, error, run_ref)"
                    " VALUES (?, ?, ?, '子 agent', 'fetch_page', '', '正文', 0, 1, '', ?)",
                    (clock.now(), GID, "T-1", run_ref.current()),
                )

        workers = CaptureWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=[])])
        workers.before_return = _peek
        sp = self._specialists(agents, workers)
        rep = await sp.run("task", "做个网页", group_id=GID, task_id="T-1")
        assert rep.ok, rep
        hid = str(rep.handoff_id or "")
        own_ref = f"handoff:{hid}"
        row = self._handoff_row(store, hid)
        assert row["run_ref"] == own_ref
        assert seen["ref"] == own_ref
        tc = store.read().execute(
            "SELECT task_id, run_ref FROM tool_calls WHERE task_id='T-1'").fetchone()
        assert str(tc["task_id"]) == "T-1" and str(tc["run_ref"]) == own_ref
        # workers.run 收到的 task_id 不变（还是 T-1，不是 ref）
        assert workers.calls[0]["task_id"] == "T-1"

    async def test_handoff_row_view_exposes_run_ref(self, store, agents):
        workers = CaptureWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=[])])
        sp = self._specialists(agents, workers)
        async with run_ref.tagged("idea:outer:test:2"):
            rep = await sp.run("idea", "出个构想", group_id=GID)
        hid = str(rep.handoff_id or "")
        row = agents.handoff(GID, hid)
        assert row is not None and str(row.get("run_ref") or "") == "idea:outer:test:2"


# ----------------------------------------------------------------------
# 轮次「正在跑」：active_rounds 名册 + board 的 round 源
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestLiveRoundsRegistry:
    async def test_tagged_registers_and_cleans_up(self) -> None:
        ref = run_ref.new("news", GID)
        assert run_ref.get_active_round(ref) is None
        async with run_ref.tagged(ref, lane="news", group_id=GID,
                                  title="正在采集本群资讯", task_id="") as sc:
            rounds = {r["ref"]: r for r in run_ref.active_rounds()}
            assert ref in rounds
            assert rounds[ref]["lane"] == "news"
            assert rounds[ref]["group_id"] == GID
            assert rounds[ref]["title"] == "正在采集本群资讯"
            assert run_ref.get_active_round(ref) is not None
        assert run_ref.get_active_round(ref) is None

    async def test_activate_after_prechecks(self) -> None:
        """预检没过之前 scope 不登记（skip 不上板）；activate() 之后才上板。"""
        ref = run_ref.new("topic", GID)
        async with run_ref.tagged(ref) as sc:
            assert run_ref.active_rounds() == []
            sc.activate(lane="topic", group_id=GID, title="正在判断是否开启话题")
            rounds = {r["ref"]: r for r in run_ref.active_rounds()}
            assert ref in rounds and rounds[ref]["lane"] == "topic"
        assert run_ref.active_rounds() == []

    async def test_board_shows_running_round_item(self, store, settings) -> None:
        ref = run_ref.new("news", GID)
        svc = _Svc(store, settings)
        async with run_ref.tagged(ref, lane="news", group_id=GID,
                                  title="正在采集本群资讯"):
            # 轮里落下一条工具调用：live 详情要能直接看到它
            with store.tx() as conn:
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool,"
                    " input, output, ms, ok, error, run_ref)"
                    " VALUES (?, ?, '', '子 agent', 'fetch_page', '', '正文', 0, 1, '', ?)",
                    (clock.now(), GID, ref),
                )
            data = board_view(svc)
            lane = _lane(data, "news")
            item = next((x for x in lane["running"] if x["id"] == ref), None)
            assert item is not None, f"news 栏 running 里要有这一轮：{lane['running']}"
            assert item["source"] == "round"
            assert item["status"] == "running"
            assert item["title"] == "正在采集本群资讯"
            view = item_view(svc, "round", ref)
            assert view is not None
            assert view["link"] == "exact"
            assert any(s["type"] == "tool" and s["tool"] == "fetch_page"
                       for s in view["steps"])
        # 出 scope 后下板
        lane = _lane(board_view(svc), "news")
        assert all(x["id"] != ref for x in lane["running"])

    async def test_non_served_group_round_not_shown(self, store, settings) -> None:
        ref = run_ref.new("idea", "999999999")  # 不在服务名单的群
        svc = _Svc(store, settings)
        async with run_ref.tagged(ref, lane="idea", group_id="999999999",
                                  title="正在构思"):
            data = board_view(svc)
            blob = json.dumps(data, ensure_ascii=False)
            assert ref not in blob
            assert item_view(svc, "round", ref) is None

    async def test_finished_round_falls_back_to_persisted_row(
        self, store, settings
    ) -> None:
        """轮结束后 item_view('round', ref) 回落到落库条目：news_batches /
        ideas / topic_log / goals 里 run_ref 对上的行。"""
        ref = run_ref.new("idea", GID)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, state, created, updated, run_ref)"
                " VALUES (?, '活过的一轮', '', 'new', ?, ?, ?)",
                (GID, NOW - 300, NOW - 300, ref),
            )
        svc = _Svc(store, settings)
        view = item_view(svc, "round", ref)
        assert view is not None
        assert view["link"] == "exact"
        assert view["item"]["title"] == "活过的一轮"

        news_ref = run_ref.new("news", GID)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped,"
                " note, created, run_ref) VALUES (?, ?, 3, 1, 0, '', ?, ?)",
                (GID, NOW - 200, NOW - 200, news_ref),
            )
        view = item_view(svc, "round", news_ref)
        assert view is not None and view["item"]["source"] == "news_batch"

        topic_ref = run_ref.new("topic", GID)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev,"
                " pick, candidate_id, opener, message_id, followup_due_ts, result,"
                " verdict, run_ref) VALUES (?, ?, 300.0, NULL, '{}', '{}', NULL,"
                " '', '', NULL, '{\"rejected\":\"没有合适候选\"}', 'bad', ?)",
                (GID, NOW - 460, topic_ref),
            )
        view = item_view(svc, "round", topic_ref)
        assert view is not None and view["item"]["source"] == "topic"


# ----------------------------------------------------------------------
# prepare_news / make_idea / topics.check / coordinator.check_goal 实时上板
# ----------------------------------------------------------------------


class _SvcFeeds:
    """feeds / topics / coordinator 用：接上 settings（含服务群名单）。"""

    def __init__(self, store, settings_obj):
        self.store = store
        self._s = settings_obj

    def get_settings(self):
        return self._s


def _oai(text: str = "好的") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    })


def _models_settings(cfg: dict | None = None):
    merged = {
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "endpoints": [{"id": "e1", "protocol": "openai",
                       "base_url": "https://e.test/v1", "api_key": "k-test"}],
        "model_list": [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
    }
    if cfg:
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
    loaded, _ = load_settings(merged)
    return loaded


def _real_models(store, settings_obj, handler, agent_map=None):
    from tests.test_main_runs import _FakeAgents
    return Models(
        store, lambda: settings_obj,
        transport=httpx.MockTransport(handler),
        agents=_FakeAgents(agent_map or {"main": {"model": "m1"},
                                       "task": {"model": "m1"},
                                       "news": {"model": "m1"},
                                       "idea": {"model": "m1"}}),
    )


@pytest.mark.asyncio
class TestRoundOnBoard:
    async def test_prepare_news_running_shows_on_board(self, tmp_path: Path) -> None:
        """prepare_news 真跑一轮：第一次模型调用时看板 news 栏有「正在跑」条目
        （source=round、id=轮 ref、status=running）；轮结束批次落库能按 ref 回看。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, profile_ready_ts,"
                " session_id, last_msg_ts) VALUES (?, ?, ?, ?)",
                (GID, 1_700_000_000.0, "sess-1", NOW - 3600.0),
            )
        settings_obj = _models_settings()
        captured: dict = {}
        items = [{"title": "测试资讯一", "url": "https://a.test/1",
                  "summary": "摘要", "kind": "news", "published": NOW - 3600,
                  "fetched": True, "quote": "原文确有", "paywall": False}]
        scores = json.dumps({"scores": [
            {"i": 0, "title": "测试资讯一", "info": 5, "source": 4, "relevance": 5,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "硬件",
             "sensitive": False, "grounded": True, "junk": False, "junk_reason": "",
             "same_as_recent": False, "why": "群里用得上", "icon": "tools"},
        ]}, ensure_ascii=False)
        posts = json.dumps({"posts": [
            {"i": 0, "title": "测试资讯一", "body": "这个新硬件挺值得看",
             "brief": "硬件短摘要", "verified": True},
        ]}, ensure_ascii=False)
        svc = _SvcFeeds(store, settings_obj)

        def handler(req: httpx.Request) -> httpx.Response:
            body = req.content.decode("utf-8", "replace")
            ref_now = run_ref.current()
            if ref_now and "board" not in captured:
                captured["board"] = board_view(svc)
                captured["ref"] = ref_now
            for key, text in {
                '"focus"': fakes.focus_reply("开源硬件", "本地大模型", "掌机社区"),
                '"note"': '{"note":"全要"}',
                '"scores"': scores,
                '"posts"': posts,
                '"unsupported"': '{"unsupported":[]}',
            }.items():
                if key in body:
                    return _oai(text)
            return _oai('{"note":"默认"}')

        models = _real_models(store, settings_obj, handler)
        from CharTyr_MaiWork.maiwork.feeds import Feeds
        feeds = Feeds(store, models, FakeWorkers(store), fakes.FakeProfiles(),
                      FakeTopics(), lambda: settings_obj)
        fakes.patch_two_phase_feeds(feeds, None, items)
        await feeds.prepare_news(GID)

        assert "board" in captured, "轮里该有模型调用（顺便抓到看板快照）"
        ref = captured["ref"]
        lane = _lane(captured["board"], "news")
        running = [x for x in lane["running"]
                   if x["source"] == "round" and x["id"] == ref]
        assert running, f"news 栏没显示这一轮：{lane['running']}"
        assert running[0]["status"] == "running"

        # 轮结束：批次落库存了同一 ref；item_view('round', ref) 回落到它
        batch = store.read().execute(
            "SELECT id, run_ref FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
        assert batch is not None and str(batch["run_ref"]) == ref
        view = item_view(svc, "round", ref)
        assert view is not None and view["item"]["id"] == int(batch["id"])
        assert view["link"] == "exact"
        store.close()

    async def test_make_idea_running_shows_on_board(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, profile_ready_ts,"
                " session_id, last_msg_ts) VALUES (?, ?, ?, ?)",
                (GID, 1_700_000_000.0, "sess-1", NOW - 3600.0),
            )
        settings_obj = _models_settings()
        fakes.seed_idea_chat(store, GID, now=clock.now())
        captured: dict = {}
        svc = _SvcFeeds(store, settings_obj)

        def handler(req: httpx.Request) -> httpx.Response:
            ref_now = run_ref.current()
            if ref_now and "board" not in captured:
                captured["board"] = board_view(svc)
                captured["ref"] = ref_now
            return _oai(fakes.idea_reply())

        models = _real_models(store, settings_obj, handler)
        from CharTyr_MaiWork.maiwork.feeds import Feeds
        feeds = Feeds(store, models, FakeWorkers(store), fakes.FakeProfiles(),
                      FakeTopics(), lambda: settings_obj)
        idea_id = await feeds.make_idea(GID)
        assert idea_id
        assert "board" in captured
        ref = captured["ref"]
        lane = _lane(captured["board"], "idea")
        assert any(x["source"] == "round" and x["id"] == ref
                   for x in lane["running"]), lane["running"]

        row = store.read().execute(
            "SELECT run_ref FROM ideas WHERE id=?", (int(idea_id),)).fetchone()
        assert str(row["run_ref"]) == ref
        view = item_view(svc, "round", ref)
        assert view is not None and view["item"]["id"] == int(idea_id)
        assert view["link"] == "exact"
        store.close()

    async def test_topics_check_running_shows_on_board(self, tmp_path: Path) -> None:
        """topics.check 过了前置检查、真问 Jev 时：topic 栏有「正在跑」条目。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings_obj = _models_settings({"topics": {"enabled": True, "min_gap_hours": 0},
                                         "jev": {"api_key": "k-test"}})
        captured: dict = {}
        svc = _SvcFeeds(store, settings_obj)
        jev_answers = {"answers": {
            "ok": {"type": "noul", "noul": 0.9},
            "reason": {"type": "choice", "choice": "fine",
                       "probabilities": {"fine": 0.9, "left": 0.04,
                                         "open_question": 0.03, "mood": 0.03},
                       "confidence": 0.9},
            "fit_0": {"type": "noul", "noul": 0.8},
        }}

        def jev_handler(req: httpx.Request) -> httpx.Response:
            ref_now = run_ref.current()
            if ref_now and "board" not in captured:
                captured["board"] = board_view(svc)
                captured["ref"] = ref_now
            return httpx.Response(200, json=jev_answers)

        jev = Jev(store, lambda: settings_obj,
                  transport=httpx.MockTransport(jev_handler))
        host = fakes.FakeHost(msgs=[], session_id="sess-1")
        profiles = fakes.FakeProfiles()
        profiles.usual_gap_value = 300.0
        signals = fakes.SignalsStub()
        signals.mark(GID, "sess-1", NOW - 3600.0)
        mentions = Mentions(store, lambda: settings_obj)
        pushes = Pushes(store, lambda: settings_obj)
        outbox = Outbox(store, host, pushes, mentions, lambda: settings_obj)
        topics = Topics(store, host,
                        fakes.FakeModelsQueue(ready=True, replies=["好话题，来聊聊？"]),
                        jev, profiles, mentions, pushes, lambda: settings_obj, signals,
                        outbox=outbox)
        expires = NOW + 12 * 3600.0
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief,"
                " link, expires_ts, used_ts, created)"
                " VALUES (?, 'news', 7, '测试候选', '简报', 'http://x', ?, NULL, ?)",
                (GID, expires, NOW - 60),
            )
        out = await topics.check(GID, NOW)
        assert out
        assert "board" in captured, "Jev 判断阶段看板该抓到这一轮"
        ref = captured["ref"]
        lane = _lane(captured["board"], "topic")
        assert any(x["source"] == "round" and x["id"] == ref
                   for x in lane["running"]), lane["running"]

        row = store.read().execute(
            "SELECT id, run_ref FROM topic_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None and str(row["run_ref"]) == ref
        view = item_view(svc, "round", ref)
        assert view is not None and view["item"]["id"] == int(row["id"])
        assert view["link"] == "exact"
        store.close()

    async def test_check_goal_running_shows_on_board(
        self, store, settings
    ) -> None:
        """coordinator.check_goal 真跑到主模型调用时：goal 栏有「正在跑」条目；
        结束后 item_view('round', ref) 回落到 goals 行。"""
        goals = Goals(store, lambda: settings)
        goal_id = goals.create_agent(
            GID, title="盯着公告", body="看发布页有没有新东西",
            criteria=["不漏场"], by_text="",
        )
        captured: dict = {}
        svc = _SvcFeeds(store, settings)

        class _PeekModels:
            def __init__(self):
                self.calls = []

            def settings(self):
                return SimpleNamespace(ready=lambda: True)

            async def chat(self, role=None, messages=None, **kwargs):
                self.calls.append(kwargs)
                captured["ref"] = run_ref.current()
                captured["board"] = board_view(svc)
                return SimpleNamespace(
                    text=json.dumps({"done_criteria": [], "next_check_hours": 2,
                                     "progress": None, "new_task": None,
                                     "report": None}, ensure_ascii=False),
                    tool_calls=[],
                )

        coord = _build_coordinator(store, settings, goals, _PeekModels())
        await coord.check_goal(goal_id)

        assert "board" in captured, "check_goal 该有一次主模型调用"
        ref = captured["ref"]
        assert ref.startswith(f"goal:{goal_id}:")
        lane = _lane(captured["board"], "goal")
        hits = [x for x in lane["running"] if x["source"] == "round" and x["id"] == ref]
        assert hits, f"goal 栏没显示这一轮：{lane['running']}"
        assert "盯" in hits[0]["title"]
        # 轮结束：回落到 goals 行
        view = item_view(svc, "round", ref)
        assert view is not None
        assert view["item"]["id"] == goal_id
        assert view["link"] == "exact"


# ----------------------------------------------------------------------
# 闲时探索：整轮 main_runs(lane='task')
# ----------------------------------------------------------------------


class _ExploreSearch:
    """search 适配层桩：接线条件要它非 None。"""


def _explore_kwargs(store, settings, env, tools, agents, workers, *, models=None):
    if models is None:
        models = ModelsQueue(replies=[_PICK])
    from test_skill_explore import _register_explore_tools
    _register_explore_tools(tools)
    return dict(
        store=store, models=models, workers=workers, env=env, agents=agents,
        gid=GID, now=NOW, profiles=FakeProfiles(),
        tools=tools, get_known_secrets=lambda: [], search=_ExploreSearch(),
    )


def _pick_models(store):
    """真 Models（带 MockTransport）：skill_explore.pick 这一调真落 model_calls。"""
    settings_obj = _models_settings()
    return _real_models(store, settings_obj, lambda req: _oai(_PICK))


def _main_run_rows(store):
    return [dict(r) for r in store.read().execute(
        "SELECT * FROM main_runs ORDER BY id ASC").fetchall()]


@pytest.mark.asyncio
class TestSkillExploreBoard:
    async def test_run_lands_in_task_lane_done(
        self, store, settings, env, tools, agents
    ):
        """跑通一轮：main_runs 留一条 lane='task' 的 done 条目，标题带方向名和
        「已沉淀」；main 栏不会出现孤儿 skill_explore.pick 条目。"""
        captured: dict = {}
        svc = _Svc(store, settings)

        async def _peek():
            captured["board"] = board_view(svc)
            captured["ref"] = run_ref.current()

        workers = CaptureWorkers(reports=[_report(data={
            "skill_markdown": _VALID_MD,
            "source_url": "skills.sh/csv-quick-preview",
            "smoke_test": "python3 -c 'import csv' 退出码 0",
        })])
        tid = _explore_tid(GID, NOW)

        async def _hook():
            await _peek()
            # 沙箱那轮的工具调用按轮 ref 落库（tools._persist 同款挂法）→
            # 看板详情按条目 ref 精确捞到它们。
            with store.tx() as conn:
                for i, (tool, ok, output) in enumerate((
                        ("fetch_page", 1, "外部文档正文"),
                        ("run_command", 1, "header printed"))):
                    conn.execute(
                        "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool,"
                        " input, output, ms, ok, error, run_ref)"
                        " VALUES (?, ?, ?, ?, ?, '', ?, 0, ?, '', ?)",
                        (clock.now() + i * 0.001, GID, tid,
                         "子 agent（闲时探索）", tool, output, ok,
                         run_ref.current()),
                    )
        workers.before_return = _hook
        models = _pick_models(store)
        out = await skill_explore.run(**_explore_kwargs(
            store, settings, env, tools, agents, workers, models=models))
        assert out["ran"] is True and out.get("skill_id")

        rows = _main_run_rows(store)
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["purpose"] == "skill_explore"
        assert str(row["lane"]) == "task"
        assert row["status"] == "done"
        assert "CSV 快速预览" in row["title"] and "已沉淀" in row["title"]
        ref = str(row["ref"])

        # 跑的时候 task 栏在板上有「闲时探索做法」的 running 条目
        lane = _lane(captured["board"], "task")
        running = [x for x in lane["running"]
                   if x["source"] == "main" and x["id"] == int(row["id"])]
        assert running, lane["running"]
        assert running[0]["title"] == "闲时探索做法"
        assert captured["ref"] == ref

        # main 栏零孤儿：整个看板没有 skill_explore.pick 的 main 条目
        data = board_view(svc)
        main_lane = _lane(data, "main")
        for sec in ("running", "history"):
            assert all("skill_explore" not in str(x.get("title") or "")
                       for x in main_lane[sec])

        # 详情：选题那一步 + 沙箱那几条 tool 都能按 ref 捞到
        view = item_view(svc, "main", row["id"])
        assert view is not None and view["link"] == "exact"
        mc = store.read().execute(
            "SELECT purpose, run_ref FROM model_calls WHERE run_ref=?",
            (ref,)).fetchall()
        assert any(str(r["purpose"]) == "skill_explore.pick" for r in mc), mc
        assert any(s["type"] == "tool" and s["tool"] == "fetch_page"
                   for s in view["steps"])
        assert any(s["type"] == "think" for s in view["steps"])

    async def test_skip_pick_marks_done_no_direction(
        self, store, settings, env, tools, agents
    ):
        workers = CaptureWorkers()
        models = ModelsQueue(replies=[_PICK_SKIP])
        out = await skill_explore.run(**_explore_kwargs(
            store, settings, env, tools, agents, workers, models=models))
        assert out == {"ran": False, "skill_id": None, "reason": "主模型说没方向"}
        rows = _main_run_rows(store)
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["title"] == "闲时探索：本轮暂无合适方向"
        assert str(rows[0]["lane"]) == "task"

    async def test_pick_failure_marks_failed(
        self, store, settings, env, tools, agents
    ):
        workers = CaptureWorkers()
        models = ModelsQueue(replies=[RuntimeError("模型挂了")])
        out = await skill_explore.run(**_explore_kwargs(
            store, settings, env, tools, agents, workers, models=models))
        assert out["ran"] is False
        rows = _main_run_rows(store)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "选题失败"

    async def test_gate_failure_marks_failed_with_reason(
        self, store, settings, env, tools, agents
    ):
        """门槛没过（没真打开过外部文档）：条目 failed，error = 门槛原话。"""
        workers = CaptureWorkers(reports=[_report(data={
            "skill_markdown": _VALID_MD, "source_url": "src", "smoke_test": "ok",
        })])
        workers.before_return = _seed_calls(store, GID, _explore_tid(GID, NOW), [
            ("web_search", 1, "搜索结果"), ("run_command", 1, "ok"),
        ])
        models = ModelsQueue(replies=[_PICK])
        out = await skill_explore.run(**_explore_kwargs(
            store, settings, env, tools, agents, workers, models=models))
        assert out["ran"] is True and not out.get("skill_id")
        rows = _main_run_rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "failed"
        assert row["error"] == "没真打开过外部文档"
        assert row["title"].startswith("闲时探索：CSV 快速预览")
        assert str(row["lane"]) == "task"

    async def test_sandbox_failure_marks_failed(
        self, store, settings, env, tools, agents
    ):
        workers = CaptureWorkers(reports=[RuntimeError("沙箱炸了")])
        models = ModelsQueue(replies=[_PICK])
        out = await skill_explore.run(**_explore_kwargs(
            store, settings, env, tools, agents, workers, models=models))
        assert out["ran"] is True and not out.get("skill_id")
        rows = _main_run_rows(store)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "沙箱探索出错"


# ======================================================================
# 第二步：交接单折进父卡 + 每单过程切片 + 轮次 / 便条 + 错误简报 + jump + uid
# ======================================================================


def _svc2(store: Store, *, agents_obj=None):
    """只服务 GID 的最小 board 服务对象（不带群名 → group_name 兜群号）。"""
    return _Svc(store, _loaded_settings(), agents=agents_obj)


class _FakeCAgents:
    """注册一个自定义专岗 c_ab12cd（kind_title / lane 名单用）。"""

    def profiles(self):
        return [{"kind": "c_ab12cd", "title": "调研岗", "fish_seed": "seed-c"}]


def _seed_groups(store: Store, names: dict | None = None) -> None:
    with store.tx() as conn:
        for gid, name in (names or {GID: "黄金群一"}).items():
            conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, name, created) VALUES (?,?,?)",
                (gid, name, 1_700_000_000.0),
            )


def _seed_task2(store: Store, tid: str, *, gid: str = GID,
                status: str = "done", created: float | None = None,
                updated: float | None = None, review: str = "") -> None:
    created = created if created is not None else NOW - 100
    updated = updated if updated is not None else NOW - 50
    finished_ts = updated if status == "done" else None
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, group_id, workspace, source, request_id, goal_id,"
            " title, req, status, created, updated, started_ts, finished_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, gid, "g1", "user", "", "", f"任务 {tid}", "req",
             status, created, updated, created, finished_ts),
        )
        if review:
            conn.execute("UPDATE tasks SET review=? WHERE id=?", (review, tid))


def _seed_goal2(store: Store, goal_id: str, *, gid: str = GID,
                state: str = "done", created: float | None = None,
                updated: float | None = None, title: str = "") -> None:
    created = created if created is not None else NOW - 100
    updated = updated if updated is not None else NOW - 50
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO goals (id, group_id, kind, title, state, created, updated)"
            " VALUES (?,?,?,?,?,?,?)",
            (goal_id, gid, "agent", title or f"目标 {goal_id}", state, created, updated),
        )


def _seed_news_batch2(store: Store, *, ref: str, created: float | None = None) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (GID, created or NOW, 12, 3, 0, "", created or NOW, ref),
        )
        return int(cur.lastrowid)


def _seed_idea2(store: Store, *, ref: str = "", target_user_id: str = "",
                title: str = "公开构想", created: float | None = None) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, state, created, updated,"
            " run_ref, target_user_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (GID, "", title, "构想正文", "new",
             created or NOW - 20, created or NOW - 20, ref, target_user_id),
        )
        return int(cur.lastrowid)


def _seed_main_run2(store: Store, *, gid: str = GID, title: str = "主模型判断",
                    status: str = "done", error: str = "", ref: str = "main:m:g:1:1",
                    started: float | None = None) -> int:
    started = started if started is not None else NOW - 60
    finished = started + 10 if status in ("done", "failed", "cancelled") else 0
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (gid, "admin_chat", title, status, started, finished,
             1 if status == "done" else 0, error, ref),
        )
        return int(cur.lastrowid)


def _seed_handoff2(store: Store, hid: str, *, gid: str = GID, kind: str = "task",
                   task_id: str = "", run_ref: str = "", status: str = "accepted",
                   created: float | None = None, updated: float | None = None,
                   brief: str = "", error: str = "", review: str = "",
                   summary: str = "") -> None:
    Agents(store, lambda: _loaded_settings())._ensure_schema()
    created = created if created is not None else NOW - 200
    updated = updated if updated is not None else created + 10
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO agent_handoffs (id, group_id, kind, task_id, phase, status,"
            " brief, summary, review, error, created, updated, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hid, gid, kind, task_id, "exec", status,
             brief or f"交接单 {hid}", summary, review, error,
             created, updated, run_ref),
        )


def _seed_note2(store: Store, gid: str, hid: str, body: str, *,
                created: float | None = None, frm: str = "news",
                to: str = "*") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO agent_notes (group_id, from_kind, to_kind, body, handoff_id, created)"
            " VALUES (?,?,?,?,?,?)",
            (gid, frm, to, body, hid, created if created is not None else NOW - 30),
        )


def _seed_tc2(store: Store, *, gid: str = GID, ts: float, tool: str,
              task_id: str = "", run_ref: str = "", group_id: str | None = None) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, group_id or gid, task_id, "news", tool, "in", "out", 5, 1, "", run_ref),
        )


def _seed_mc2(store: Store, *, gid: str = GID, ts: float, text: str = "想了一下",
              task_id: str = "", run_ref: str = "") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, model, group_id, task_id, attempt,"
            " ok, status, ms, prompt_tokens, completion_tokens, error, request, response,"
            " agent, usage_src, cache_read, cache_write, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "handoff", "assistant", "m", gid, task_id, 1, 1, 200, 5, 3, 5, "",
             "{}", json.dumps({"text": text}), "news", "", 0, 0, run_ref),
        )


def _lane(data: dict, kind: str) -> dict:
    """按 kind 找 lanes 里那栏；没有就报错（比逐字段 diff 直观）。"""
    for lane in data["lanes"]:
        if lane["kind"] == kind:
            return lane
    raise AssertionError(f"lanes 里没有 {kind} 栏：{[l['kind'] for l in data['lanes']]}")


def _lane_item(lane: dict, iid) -> dict | None:
    for it in lane["running"] + lane["history"]:
        if str(it["id"]) == str(iid):
            return it
    return None


@pytest.fixture
def board_store(store):
    """种了群行 + agent_handoffs 表的库（多数 step2 用例的底座）。"""
    Agents(store, lambda: _loaded_settings())._ensure_schema()
    _seed_groups(store)
    return store


class TestStep2Folding:
    """交接单折进父卡：同栏才折、跨栏 / 没爸的留着、活的交接单不藏进已结束的爸。"""

    def test_same_lane_task_child_folds(self, board_store):
        _seed_task2(board_store, "T-1", created=NOW - 300, updated=NOW - 200)
        _seed_handoff2(board_store, "h-t1", kind="task", task_id="T-1",
                       status="accepted", created=NOW - 280, updated=NOW - 250)
        _seed_note2(board_store, GID, "h-t1", "给大家：结果都在 artifacts 里")
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "task")
        assert [x["id"] for x in lane["history"]] == ["T-1"]
        assert "h-t1" not in json.dumps(data["lanes"], ensure_ascii=False)
        parent = lane["history"][0]
        assert parent["handoff_count"] == 1
        assert parent["note_count"] == 1
        # 详情同形：rounds / notes 聚合
        view = item_view(svc, "task", "T-1", now=NOW)
        assert view["item"]["handoff_count"] == 1
        assert view["item"]["note_count"] == 1
        assert view["item"]["jump"] == {"tab": "tasks", "detail_type": "task", "detail_id": "T-1"}
        assert len(view["rounds"]) == 1
        rd = view["rounds"][0]
        assert rd["id"] == "h-t1" and rd["no"] == 1
        assert rd["kind"] == "task" and rd["kind_title"] == "通用任务"
        assert rd["status"] == "accepted" and rd["ok"] is True
        assert set(view["notes"][0]) == {"from_kind", "to_kind", "body", "created"}
        assert [n["body"] for n in view["notes"]] == ["给大家：结果都在 artifacts 里"]

    def test_cross_lane_handoff_stays_visible(self, board_store):
        """kind=c_* 的交接单挂 task_id=T-x：它住在自己那栏，不折进 task 栏。"""
        _seed_task2(board_store, "T-2", created=NOW - 300, updated=NOW - 200)
        _seed_handoff2(board_store, "h-c1", kind="c_ab12cd", task_id="T-2",
                       status="accepted", created=NOW - 280, updated=NOW - 250)
        svc = _svc2(board_store, agents_obj=_FakeCAgents())
        data = board_view(svc, hours=24, now=NOW)
        assert _lane_item(_lane(data, "task"), "h-c1") is None
        c = _lane_item(_lane(data, "c_ab12cd"), "h-c1")
        assert c is not None and c["title"] == "交接单 h-c1"
        assert c["handoff_count"] == 0 and c["note_count"] == 0
        # 跨栏也折不得：父卡 counts 仍算它（child 归属按库键，不看栏）
        assert _lane_item(_lane(data, "task"), "T-2")["handoff_count"] == 1

    def test_news_batch_parent_folds_and_live_round(self, board_store):
        """run_ref 精确折：news_batch 吃掉 news 子交接单；正在跑的轮也折。"""
        ref = "news:900000001:1799999000000:9"
        bid = _seed_news_batch2(board_store, ref=ref, created=NOW - 100)
        _seed_handoff2(board_store, "h-nb", kind="news", run_ref=ref,
                       status="accepted", created=NOW - 150, updated=NOW - 130)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "news")
        assert _lane_item(lane, "h-nb") is None
        nb = _lane_item(lane, bid)
        assert nb is not None and nb["source"] == "news_batch"
        assert nb["handoff_count"] == 1
        assert nb["jump"] == {"tab": "news", "detail_type": "", "detail_id": ""}
        view = item_view(svc, "news_batch", str(nb["id"]), now=NOW)
        assert view["item"]["handoff_count"] == 1
        assert [r["id"] for r in view["rounds"]] == ["h-nb"]

        # 正在跑的轮（还没落库条目）当父卡：h-rr 折进去
        ref2 = "news:900000001:1799999900000:10"
        _seed_handoff2(board_store, "h-rr", kind="news", run_ref=ref2,
                       status="accepted", created=NOW - 20, updated=NOW - 10)

        async def _hold_round():
            async with run_ref.tagged(ref2, lane="news", group_id=GID,
                                      title="跑一轮资讯", task_id=""):
                data2 = board_view(svc, hours=24, now=NOW)
                lane2 = _lane(data2, "news")
                round_item = _lane_item(lane2, ref2)
                assert round_item is not None and round_item["source"] == "round"
                assert _lane_item(lane2, "h-rr") is None
                assert round_item["handoff_count"] == 1
                assert round_item["jump"] == {"tab": "news", "detail_type": "", "detail_id": ""}

        asyncio.run(_hold_round())
        # 轮收尾后：父卡消失（还没落库），h-rr 重新独立可见
        data3 = board_view(svc, hours=24, now=NOW)
        assert _lane_item(_lane(data3, "news"), "h-rr") is not None

    def test_goal_parent_folds_by_goal_check_task_id(self, board_store):
        _seed_goal2(board_store, "G-f", state="done",
                    created=NOW - 300, updated=NOW - 200)
        _seed_handoff2(board_store, "h-g", kind="goal", task_id="goal-check:G-f",
                       status="accepted", created=NOW - 280, updated=NOW - 250)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "goal")
        assert _lane_item(lane, "h-g") is None
        g = _lane_item(lane, "G-f")
        assert g["handoff_count"] == 1
        assert g["jump"] == {"tab": "tasks", "detail_type": "goal", "detail_id": "G-f"}
        view = item_view(svc, "goal", "G-f", now=NOW)
        assert [r["id"] for r in view["rounds"]] == ["h-g"]
        assert view["item"]["jump"]["detail_id"] == "G-f"

    def test_running_handoff_not_hidden_in_finished_parent(self, board_store):
        """爸已经结束了但交接单还在跑 → 交接单不折叠，单独可见。"""
        _seed_task2(board_store, "T-3", status="done",
                    created=NOW - 300, updated=NOW - 200)
        _seed_handoff2(board_store, "h-live", kind="task", task_id="T-3",
                       status="running", created=NOW - 100, updated=NOW - 30)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "task")
        assert _lane_item(lane, "h-live") is not None
        assert _lane_item(lane, "h-live") in lane["running"]

    def test_unparented_handoff_stays_visible(self, board_store):
        """run_ref 空 / 父卡不在板上的交接单保留自己那张卡。"""
        _seed_handoff2(board_store, "h-solo", kind="news", task_id="", run_ref="",
                       status="accepted", created=NOW - 280, updated=NOW - 250)
        _seed_handoff2(board_store, "h-orphan", kind="news",
                       run_ref="news:900000001:1111111111000:1",
                       status="accepted", created=NOW - 280, updated=NOW - 250)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "news")
        assert _lane_item(lane, "h-solo") is not None
        assert _lane_item(lane, "h-orphan") is not None
        for x in (("handoff", "h-solo"), ("handoff", "h-orphan")):
            assert item_view(svc, *x, now=NOW) is not None


class TestStep2HandoffTrace:
    """每张交接单的过程切片：handoff:<id> 精确单、共享 ref 切片、老 T-* 窗口。"""

    def test_handoff_ref_is_exact_single_round(self, board_store):
        _seed_handoff2(board_store, "h-self", kind="news",
                       run_ref="handoff:h-self", status="accepted",
                       created=NOW - 300, updated=NOW - 200)
        _seed_tc2(board_store, ts=NOW - 250, tool="self_tool", run_ref="handoff:h-self")
        _seed_tc2(board_store, ts=NOW - 240, tool="other_tool", run_ref="news:x:y:1:1")
        _seed_tc2(board_store, ts=NOW - 230, tool="plain_tool")
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-self", now=NOW)
        assert view["link"] == "exact"
        tools = [s["tool"] for s in view["steps"] if s["type"] == "tool"]
        assert tools == ["self_tool"]
        assert view["steps"][1]["round_id"] == "h-self"
        assert view["steps"][1]["uid"].startswith("t:")

    def test_outer_ref_time_sliced(self, board_store):
        """run_ref=共享轮 ref：切片 [created, updated+1]，轮里别家的步不进来。"""
        ref = "news:900000001:1799999800000:2"
        _seed_news_batch2(board_store, ref=ref, created=NOW - 90)
        _seed_handoff2(board_store, "h-slice", kind="news", run_ref=ref,
                       status="accepted", created=NOW - 100, updated=NOW - 50)
        _seed_tc2(board_store, ts=NOW - 80, tool="inside", run_ref=ref)
        _seed_tc2(board_store, ts=NOW - 40, tool="outside", run_ref=ref)
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-slice", now=NOW)
        assert view["link"] == "exact"
        tools = [s["tool"] for s in view["steps"] if s["type"] == "tool"]
        assert tools == ["inside"]
        # 列表 counts 与详情切片一致（batch 也按切片数）
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "news")
        nb = next(x for x in lane["history"] if x["source"] == "news_batch")
        assert nb["counts"]["tools"] == 2       # 父卡整轮两条都数
        assert nb["handoff_count"] == 1

    def test_legacy_T_handoff_windowed(self, board_store):
        """老 T-* 交接单（没 run_ref）：只取 [created, updated+1] 窗内的步。"""
        _seed_handoff2(board_store, "h-legacy", kind="task", task_id="T-L",
                       status="accepted", created=NOW - 300, updated=NOW - 200)
        _seed_tc2(board_store, ts=NOW - 250, tool="in_window", task_id="T-L")
        _seed_tc2(board_store, ts=NOW - 100, tool="after_window", task_id="T-L")
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-legacy", now=NOW)
        tools = [s["tool"] for s in view["steps"] if s["type"] == "tool"]
        assert tools == ["in_window"]

    def test_synthetic_tid_unwindowed(self, board_store):
        """非 T-* 的 tid（feeds-verify:* 等老形状）：全程拉，不切窗。"""
        _seed_handoff2(board_store, "h-syn", kind="news", task_id="feeds-verify:g:9:0",
                       status="accepted", created=NOW - 300, updated=NOW - 200)
        _seed_tc2(board_store, ts=NOW - 500, tool="before", task_id="feeds-verify:g:9:0")
        _seed_tc2(board_store, ts=NOW - 100, tool="after", task_id="feeds-verify:g:9:0")
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-syn", now=NOW)
        assert view["link"] == "exact"
        tools = [s["tool"] for s in view["steps"] if s["type"] == "tool"]
        assert tools == ["before", "after"]

    def test_no_keys_window_fallback(self, board_store):
        _seed_handoff2(board_store, "h-win", kind="news", task_id="", run_ref="",
                       status="accepted", created=NOW - 300, updated=NOW - 200)
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-win", now=NOW)
        assert view["link"] == "time_window"


class TestStep2BoardNotes:
    """顶层 notes：服务群 + 窗口内的便条按 created DESC, id DESC（至多 50）。"""

    def test_top_level_notes(self, board_store):
        _seed_handoff2(board_store, "h-n1", kind="news", status="accepted",
                       created=NOW - 100, updated=NOW - 90)
        _seed_note2(board_store, GID, "h-n1", "新便条", created=NOW - 10)
        _seed_note2(board_store, GID, "h-n1", "旧便条", created=NOW - 20)
        _seed_note2(board_store, GID, "h-n1", "窗外便条", created=NOW - 90000)
        _seed_note2(board_store, "777000777", "h-n1", "别群便条", created=NOW - 10)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        assert [n["body"] for n in data["notes"]] == ["新便条", "旧便条"]
        n0 = data["notes"][0]
        assert n0["group_id"] == GID and n0["handoff_id"] == "h-n1"
        assert n0["from_kind"] == "news" and n0["to_kind"] == "*"
        assert isinstance(n0["id"], int) and isinstance(n0["created"], float)
        assert n0["group_name"]  # 有名字时给名字，没名字兜群号

    def test_notes_empty_without_table_rows(self, board_store):
        data = board_view(_svc2(board_store), hours=24, now=NOW)
        assert data["notes"] == []


class TestStep2ErrorBriefsAndJump:
    """error_brief 全卡、error 只详情、jump 逐来源（个人构想不露 detail）。"""

    def test_failed_task_error_fields(self, board_store):
        _seed_task2(board_store, "T-e", status="failed",
                    created=NOW - 300, updated=NOW - 200,
                    review="失败原因：磁盘满 " + "x" * 200)
        _seed_task2(board_store, "T-ok", status="done",
                    created=NOW - 300, updated=NOW - 200)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        lane = _lane(data, "task")
        bad = _lane_item(lane, "T-e")
        assert bad["error_brief"].startswith("失败原因：磁盘满")
        assert len(bad["error_brief"]) <= 80
        assert "error" not in bad        # lanes 不放完整 error
        good = _lane_item(lane, "T-ok")
        assert good["error_brief"] == ""
        assert "error" not in good
        view = item_view(svc, "task", "T-e", now=NOW)
        assert view["item"]["error"].startswith("失败原因：磁盘满")
        assert len(view["item"]["error"]) <= 400
        assert view["item"]["error_brief"] == bad["error_brief"]

    def test_handoff_error_falls_back_to_review(self, board_store):
        _seed_handoff2(board_store, "h-rej", kind="news", status="rejected",
                       created=NOW - 300, updated=NOW - 200,
                       review="验收没通过：证据不够")
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        h = _lane_item(_lane(data, "news"), "h-rej")
        assert h["error_brief"] == "验收没通过：证据不够"
        view = item_view(svc, "handoff", "h-rej", now=NOW)
        assert view["item"]["error"] == "验收没通过：证据不够"

    def test_main_and_neutral_error_fields(self, board_store):
        mid = _seed_main_run2(board_store, status="failed",
                              error="模型超时 " + "e" * 300, started=NOW - 60)
        _seed_goal2(board_store, "G-bad", state="cancelled",
                    created=NOW - 300, updated=NOW - 200)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        m = _lane_item(_lane(data, "main"), mid)
        assert m["error_brief"].startswith("模型超时")
        assert len(m["error_brief"]) <= 80
        assert "error" not in m
        g = _lane_item(_lane(data, "goal"), "G-bad")
        assert g["error_brief"] == ""    # goal 不带 error 字段
        view = item_view(svc, "main", str(mid), now=NOW)
        assert len(view["item"]["error"]) <= 400
        assert view["item"]["error"].startswith("模型超时")

    def test_jump_per_source(self, board_store):
        _seed_task2(board_store, "T-j")
        _seed_goal2(board_store, "G-j", state="active",
                    created=NOW - 300, updated=NOW - 200)
        iid_pub = _seed_idea2(board_store, title="公开构想")
        iid_priv = _seed_idea2(board_store, title="私密构想", target_user_id="u9")
        mid = _seed_main_run2(board_store)
        _seed_handoff2(board_store, "h-tj", kind="news", task_id="T-77",
                       status="running", created=NOW - 30, updated=NOW - 10)
        _seed_handoff2(board_store, "h-gj", kind="goal", task_id="goal-check:G-9",
                       status="running", created=NOW - 30, updated=NOW - 10)
        _seed_handoff2(board_store, "h-gx", kind="news", task_id="",
                       status="running", created=NOW - 30, updated=NOW - 10)
        svc = _svc2(board_store)
        data = board_view(svc, hours=24, now=NOW)
        assert _lane_item(_lane(data, "task"), "T-j")["jump"] == {
            "tab": "tasks", "detail_type": "task", "detail_id": "T-j"}
        assert _lane_item(_lane(data, "goal"), "G-j")["jump"] == {
            "tab": "tasks", "detail_type": "goal", "detail_id": "G-j"}
        assert _lane_item(_lane(data, "idea"), iid_pub)["jump"] == {
            "tab": "ideas", "detail_type": "idea", "detail_id": f"I-{iid_pub}"}
        # 个人构想：group 页跳，绝不露 detail id
        assert _lane_item(_lane(data, "idea"), iid_priv)["jump"] == {
            "tab": "group", "detail_type": "", "detail_id": ""}
        assert _lane_item(_lane(data, "main"), mid)["jump"] == {
            "tab": "group", "detail_type": "", "detail_id": ""}
        news_lane = _lane(data, "news")
        assert _lane_item(news_lane, "h-tj")["jump"] == {
            "tab": "tasks", "detail_type": "task", "detail_id": "T-77"}
        goal_lane = _lane(data, "goal")
        assert _lane_item(goal_lane, "h-gj")["jump"] == {
            "tab": "tasks", "detail_type": "goal", "detail_id": "G-9"}
        assert _lane_item(news_lane, "h-gx")["jump"] == {
            "tab": "group", "detail_type": "", "detail_id": ""}

    def test_round_jump_variants(self, board_store):
        """round 的 jump 按 lane 走：idea→ideas / goal-check→tasks / topic→group。"""
        svc = _svc2(board_store)

        async def _check():
            async with run_ref.tagged("idea:g:1:1", lane="idea", group_id=GID,
                                      title="跑构想"), \
                    run_ref.tagged("goal:g:1:1", lane="goal", group_id=GID,
                                   title="目标检查", task_id="goal-check:G-7"), \
                    run_ref.tagged("topic:g:1:1", lane="topic", group_id=GID,
                                   title="开话题"):
                data = board_view(svc, hours=24, now=NOW)
                assert _lane_item(_lane(data, "idea"), "idea:g:1:1")["jump"] == {
                    "tab": "ideas", "detail_type": "", "detail_id": ""}
                assert _lane_item(_lane(data, "goal"), "goal:g:1:1")["jump"] == {
                    "tab": "tasks", "detail_type": "goal", "detail_id": "G-7"}
                assert _lane_item(_lane(data, "topic"), "topic:g:1:1")["jump"] == {
                    "tab": "group", "detail_type": "", "detail_id": ""}

        import asyncio
        asyncio.run(_check())


class TestStep2ItemViewExtras:
    """详情：rounds / notes / uid / round_id / ?after= / 截断标记。"""

    def test_rounds_empty_for_leaf_and_unmatched(self, board_store):
        _seed_handoff2(board_store, "h-leaf", kind="news",
                       run_ref="handoff:h-leaf", status="accepted",
                       created=NOW - 300, updated=NOW - 200)
        mid = _seed_main_run2(board_store)
        svc = _svc2(board_store)
        for source, iid in (("handoff", "h-leaf"), ("main", str(mid))):
            view = item_view(svc, source, iid, now=NOW)
            assert view["rounds"] == []
            assert view["notes"] == []
        view = item_view(svc, "task", "T-none", now=NOW)
        assert view is None

    def test_parent_notes_aggregation(self, board_store):
        _seed_task2(board_store, "T-n", created=NOW - 300, updated=NOW - 200)
        _seed_handoff2(board_store, "h-a", kind="task", task_id="T-n",
                       status="accepted", created=NOW - 280, updated=NOW - 250)
        _seed_handoff2(board_store, "h-b", kind="task", task_id="T-n",
                       status="failed", created=NOW - 240, updated=NOW - 230)
        _seed_note2(board_store, GID, "h-a", "便条A")
        _seed_note2(board_store, GID, "h-b", "便条B")
        _seed_note2(board_store, GID, "h-x", "别家的")
        svc = _svc2(board_store)
        view = item_view(svc, "task", "T-n", now=NOW)
        assert [n["body"] for n in view["notes"]] == ["便条A", "便条B"]
        assert view["item"]["note_count"] == 2
        assert view["item"]["handoff_count"] == 2
        rounds = view["rounds"]
        assert [r["no"] for r in rounds] == [1, 2]
        assert rounds[1]["status"] == "failed" and rounds[1]["ok"] is False

    def test_step_uid_and_after_dedup(self, board_store):
        _seed_handoff2(board_store, "h-u", kind="news",
                       run_ref="handoff:h-u", status="accepted",
                       created=NOW - 300, updated=NOW - 200)
        _seed_tc2(board_store, ts=NOW - 250, tool="t1", run_ref="handoff:h-u")
        _seed_tc2(board_store, ts=NOW - 200, tool="t2", run_ref="handoff:h-u")
        _seed_mc2(board_store, ts=NOW - 100, text="收尾", run_ref="handoff:h-u")
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-u", now=NOW)
        steps = view["steps"]
        uids = [s["uid"] for s in steps]
        assert uids[0] == "s:start"
        assert any(u.startswith("t:") for u in uids)
        assert any(u.startswith("m:") for u in uids)
        assert "s:end:accepted" in uids
        for s in steps:
            if s["type"] in ("tool", "think"):
                assert s["round_id"] == "h-u"
            else:
                assert s["round_id"] == ""
        # ?after=<上一条 ts>：同 ts 的步不丢（前端按 uid 去重）
        last_ts = steps[-1]["ts"]
        view2 = item_view(svc, "handoff", "h-u", after=last_ts, now=NOW)
        assert view2["steps"], "after 同 ts 不应全丢"
        assert all(s["ts"] >= last_ts for s in view2["steps"])
        assert view2["total_steps"] == len(view2["steps"])
        assert view2["truncated"] is False

    def test_truncated_over_500(self, board_store):
        _seed_handoff2(board_store, "h-big", kind="news", task_id="feeds-verify:g:9:1",
                       status="accepted", created=NOW - 100000, updated=NOW - 90000)
        with board_store.tx() as conn:
            for i in range(520):
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool,"
                    " input, output, ms, ok, error, run_ref)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (NOW - 80000 + i, GID, "feeds-verify:g:9:1", "news",
                     f"tool{i}", "in", "out", 5, 1, "", ""),
                )
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-big", now=NOW)
        assert view["total_steps"] > 500
        assert view["truncated"] is True
        assert len(view["steps"]) == 501  # 头尾 + 占位
        placeholder = next(s for s in view["steps"] if "省略" in s["text"])
        assert placeholder["uid"].startswith("s:omit:")


@pytest.mark.asyncio
class TestStep2BoardHttpRedaction:
    """/api/board 整包过密钥遮罩（和 /api/board/items 同款）。"""

    @pytest_asyncio.fixture
    async def board_env(self, tmp_path):
        raw = _http_raw_config(
            tmp_path / "data",
            environments={"workspace_root": str(tmp_path / "workspaces")},
        )
        plug_dir = tmp_path / "plug"
        plug_dir.mkdir()
        doc = tomlkit.document()
        for section, values in raw.items():
            if isinstance(values, dict):
                t = tomlkit.table()
                for k, v in values.items():
                    t[k] = v
                doc[section] = t
        (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
        ctx = fakes.FakeCtx({"config.get": "987654321"})
        app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
        app.profiles_cls = fakes.FakeProfiles
        await app.start()
        server = TestServer(app.console.app)
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        yield SimpleNamespace(app=app, client=client, store=app.store)
        await client.close()
        await app.stop()

    async def test_board_response_is_secret_redacted(self, board_env):
        secret = "sk-board-do-not-leak-zzz-9x9"
        with board_env.store.tx() as conn:
            conn.execute(
                "INSERT INTO secrets (name, value, updated) VALUES (?,?,?)",
                ("t1", secret, 1.0),
            )
        _seed_note2(board_env.store, GID, "h-s",
                    f"token {secret} tail", created=NOW - 10)
        _seed_handoff2(board_env.store, "h-s", kind="news", status="accepted",
                       created=NOW - 100, updated=NOW - 90,
                       brief=f"brief {secret} mid")
        await _http_login(board_env)
        r = await board_env.client.get("/api/board?hours=24")
        assert r.status == 200
        body = await r.text()
        assert secret not in body
        data = json.loads(body)
        assert data["notes"][0]["body"] == "token *** tail"
        news = next(l for l in data["lanes"] if l["kind"] == "news")
        h = next(x for x in news["history"] if x["id"] == "h-s")
        assert h["title"] == "brief *** mid"


# ======================================================================
# 第二步评审回归：多父卡同键 / accepted 不吃 review / 切片 T-* 卡面计数
# ======================================================================


class TestStep2ReviewRegressions:

    def test_running_handoff_folds_into_active_parent_same_key(self, board_store):
        """同键两张父卡（已结束的 news_batch + 正在跑的 round 同 ref）：
        lane_map 是「爸全走没」，还有活爸 → 跑着的单照常折进去。"""
        ref = "news:900000001:1799999800000:11"
        _seed_news_batch2(board_store, ref=ref, created=NOW - 300)   # 已结束的旧轮
        _seed_handoff2(board_store, "h-mix", kind="news", run_ref=ref,
                       status="running", created=NOW - 50, updated=NOW - 5)
        svc = _svc2(board_store)

        async def _hold():
            async with run_ref.tagged(ref, lane="news", group_id=GID,
                                      title="跑第二轮资讯", task_id=""):
                data = board_view(svc, hours=24, now=NOW)
                lane = _lane(data, "news")
                assert _lane_item(lane, "h-mix") is None   # 折进活爸，不露独立卡
                rnd = _lane_item(lane, ref)
                assert rnd is not None and rnd["source"] == "round"
                assert rnd["handoff_count"] == 1

        asyncio.run(_hold())

    def test_accepted_handoff_review_not_in_error(self, board_store):
        """accepted 的 review 是「核验通过」：error / error_brief 必须空，
        别把验收意见画进红色错误横幅。"""
        _seed_handoff2(board_store, "h-ok", kind="news", status="accepted",
                       created=NOW - 200, updated=NOW - 100,
                       review="核验通过")
        svc = _svc2(board_store)
        view = item_view(svc, "handoff", "h-ok", now=NOW)
        assert view["item"]["error"] == ""
        assert view["item"]["error_brief"] == ""
        # lanes 卡片同形
        data = board_view(svc, hours=24, now=NOW)
        card = _lane_item(_lane(data, "news"), "h-ok")
        assert card is not None and card["error_brief"] == ""

    def test_legacy_sliced_T_handoff_card_counts_match_steps(self, board_store):
        """跨栏 c_* 老 T-* 交接单（run_ref=""，updated>created → 时窗切片）：
        卡面 counts / last_step 跟 steps() 同一套窗——窗外的不算数。"""
        _seed_handoff2(board_store, "h-cx", kind="c_ab12cd", task_id="T-9",
                       status="accepted", created=NOW - 300, updated=NOW - 200)
        # 窗内（created .. updated+1）：两刀一想
        _seed_tc2(board_store, ts=NOW - 290, tool="t_in1", task_id="T-9")
        _seed_tc2(board_store, ts=NOW - 280, tool="t_in2", task_id="T-9")
        _seed_mc2(board_store, ts=NOW - 270, task_id="T-9", text="窗内想")
        # 窗外：同一 task_id 但不在交接单时窗里，不许计入卡面
        _seed_tc2(board_store, ts=NOW - 150, tool="t_late", task_id="T-9")
        svc = _svc2(board_store, agents_obj=_FakeCAgents())
        data = board_view(svc, hours=24, now=NOW)
        card = _lane_item(_lane(data, "c_ab12cd"), "h-cx")
        assert card is not None
        assert card["counts"] == {"tools": 2, "thinks": 1}
        assert card["last_step"]["text"].startswith("窗内想")
        # 详情同一套窗：步骤只含窗内的
        view = item_view(svc, "handoff", "h-cx", now=NOW)
        assert view["item"]["counts"] == {"tools": 2, "thinks": 1}
        tools = [s for s in view["steps"] if s["type"] == "tool"]
        thinks = [s for s in view["steps"] if s["type"] == "think"]
        assert len(tools) == 2 and len(thinks) == 1
