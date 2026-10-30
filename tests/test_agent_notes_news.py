"""资讯岗便条：这批资讯发出来才贴（docs/34 §五 4，用户 2026-10-30 定）。

- news 环节的 review（feeds._settle_stage_reports_local）只说明「该环节交回了结构有效的产物」，
  不代表这批资讯出了——这时不贴，便条留在交接单上；
- 这一批入库且有条目通过（news_batches.kept>0、skipped=0，条目 rejected=0 上资讯页）
  → 本轮所有通过检查的 news 交接单的暂存便条入板（同一道隐私闸、同一套上限/过期）；
- 没出（skipped / 全被筛掉 / 失败）→ 不贴，便条丢弃。
- Agents 层：review(post_notes=False) 先压着；post_held_notes 只贴 accepted 的单、不重复贴。

先写测试、确认能变红再实现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.specialists import Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import WorkerReport

G1 = "900000001"


class _Settings:
    def __init__(self, gid=G1):
        self.groups = {gid: object()}
        self.data_dir = Path("/tmp/maiwork-notes-news")
        self.feeds = type("Feeds", (), {
            "news_slots": None, "idea_slots": None,
            "lookback_days": 14, "web_min_avg": 3.0, "pool_min_avg": 4.0, "max_items": 10,
            "collect_minutes": 15, "guides": True, "blocked_domains": (), "verify_enabled": False,
        })()
        self.topics = type("Topics", (), {"candidate_ttl_hours": 12})()
        self.environments = type("Env", (), {"max_parallel": 2, "railway": False, "verify_minutes": 10})()
        self.models = type("Models", (), {"context_window": 128000})()

    def is_served(self, gid) -> bool:
        return str(gid) in self.groups

    def workspace_of(self, gid) -> str:
        return f"g{gid}"


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings():
    return _Settings()


@pytest.fixture
def agents(store, settings):
    return Agents(store, lambda: settings)


def _returned(agents, gid, kind, notes):
    hid = agents.begin(gid, kind, "干活")
    agents.running(gid, hid)
    agents.returned(gid, hid, "做完了", data={"x": 1}, notes=notes)
    return hid


def _board(store):
    return [(r["from_kind"], r["body"], r["handoff_id"]) for r in store.read().execute(
        "SELECT from_kind, body, handoff_id FROM agent_notes WHERE group_id=? ORDER BY id", (G1,))]


# ---------------------------------------------------------------- Agents 层


def test_review_post_notes_false_holds_notes(agents, store):
    hid = _returned(agents, G1, "news", [{"to": "idea", "text": "先压着的便条"}])
    agents.review(G1, hid, True, "ok", learn=False, post_notes=False)
    assert agents.handoff(G1, hid)["status"] == "accepted"
    assert _board(store) == []


def test_post_held_notes_posts_once(agents, store):
    hid = _returned(agents, G1, "news", [{"to": "idea", "text": "这批真出了"}])
    agents.review(G1, hid, True, "ok", learn=False, post_notes=False)
    assert agents.post_held_notes(G1, [hid]) == 1
    assert _board(store) == [("news", "这批真出了", hid)]
    # 幂等：再调一次 / 重复 id 不重复贴
    assert agents.post_held_notes(G1, [hid, hid]) == 0
    assert len(_board(store)) == 1


def test_post_held_notes_skips_already_posted_by_review(agents, store):
    hid = _returned(agents, G1, "news", [{"to": "*", "text": "检查时已经贴过"}])
    agents.review(G1, hid, True, "ok")
    assert agents.post_held_notes(G1, [hid]) == 0
    assert len(_board(store)) == 1


def test_post_held_notes_only_accepted(agents, store):
    rej = _returned(agents, G1, "news", [{"to": "*", "text": "被打回的"}])
    agents.review(G1, rej, False, "打回", post_notes=False)
    pending = _returned(agents, G1, "news", [{"to": "*", "text": "还没检查的"}])
    task = _returned(agents, G1, "task", [{"to": "*", "text": "通用任务"}])
    agents.review(G1, task, True, "ok", post_notes=False)
    assert agents.post_held_notes(G1, [rej, pending, task, "不存在的单"]) == 0
    assert _board(store) == []


def test_post_held_notes_privacy_gate(agents, store):
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed) VALUES (?,?,?,?,0)",
            (G1, "u1", "阿帆", "最近在准备考研很焦虑每天熬夜"),
        )
    hid = _returned(agents, G1, "news", [{"to": "*", "text": "阿帆最近在准备考研很焦虑，多推学习资料"},
                                         {"to": "*", "text": "干净的一条"}])
    agents.review(G1, hid, True, "ok", post_notes=False)
    assert agents.post_held_notes(G1, [hid]) == 1
    assert [b for _, b, _ in _board(store)] == ["干净的一条"]


def test_post_held_notes_other_group_handoff_ignored(store):
    s = _Settings()
    s.groups["123456789"] = object()
    a = Agents(store, lambda: s)
    hid = _returned(a, "123456789", "news", [{"to": "*", "text": "二群的"}])
    a.review("123456789", hid, True, "ok", post_notes=False)
    assert a.post_held_notes(G1, [hid]) == 0  # 不跨群
    with pytest.raises(ValueError):
        a.post_held_notes("999", [hid])


def test_specialists_review_passes_post_notes(agents, store):
    sp = Specialists(agents, None, None)
    hid = _returned(agents, G1, "news", [{"to": "*", "text": "经 Specialists 压着"}])
    sp.review(G1, hid, True, "ok", learn=False, post_notes=False)
    assert _board(store) == []
    assert agents.handoff(G1, hid)["status"] == "accepted"


# ---------------------------------------------------------------- 资讯一轮（feeds 接线）


class _NewsSp(Specialists):
    """真 Specialists.review + 真 Agents；run 不起子 agent，直接登记一张带便条的 news 交接单。"""

    def __init__(self, agents):
        super().__init__(agents, None, None)
        self.hids: list[str] = []

    async def run(self, kind, brief, *, group_id, phase="", task_id="", tools=None,
                  output_schema=None, actor="", deadline_ts=None, max_steps=0, **_kw):
        hid = self._agents.begin(str(group_id), str(kind), str(brief), task_id=str(task_id), phase=str(phase))
        self._agents.running(str(group_id), hid)
        notes = [{"to": "idea", "text": f"{phase} 环节留的便条"}]
        self._agents.returned(str(group_id), hid, f"{phase} 交回", data={"items": []}, notes=notes)
        self.hids.append(hid)
        return WorkerReport(ok=True, summary=f"{phase} 交回", data={"items": []}, handoff_id=hid)


class _Topics:
    def add_candidate(self, *a, **k):
        pass


def _round_feeds(store, settings, agents, monkeypatch, *, outcome: str):
    """把 _prepare_news_round 的重环节换成桩，只留：环节交回 → 收尾 review → 落批次 → 入板。

    outcome: published（有 1 条通过）/ all_rejected（全被筛掉，kept=0）/ no_candidates（撒网空，skipped）
    """
    from CharTyr_MaiWork.maiwork import news_recheck
    from CharTyr_MaiWork.maiwork.feeds import Feeds

    feeds = Feeds(store, None, None, None, _Topics(), lambda: settings, search=None, host=None)
    sp = _NewsSp(agents)
    feeds._specialists = sp  # noqa: SLF001

    async def _anoop(*a, **k):
        return None

    async def _focus(*a, **k):
        return [{"query": "显卡", "why": "", "angle": "", "source": ""}]

    async def _rss(*a, **k):
        return []

    async def _collect(gid, focus, s, *, collect_mark, stats_out):
        await feeds._run_stage(gid, phase="verify", brief="核验", task_id=f"{collect_mark}:v",
                               tools=["fetch_page"])
        if outcome == "no_candidates":
            return []
        return [{
            "title": "新显卡发布", "summary": "摘要", "url": "https://example.com/a",
            "url_key": "example.com/a", "kind": "news", "why": "群里在聊显卡",
            "published_ts": clock.now(),
        }]

    async def _recheck(st, runner, gid, candidates, **kw):
        await feeds._recheck_run(gid, kw["recheck_mark"], "重看这几条", group_id=gid)

    async def _score(gid, s, survivors, *, task_id=""):
        for it in survivors:
            it["scores"] = {"avg": 4.5, "relevance": 5, "chat": 4}
            if outcome == "all_rejected":
                it["reject"] = ("web", "不够好")

    monkeypatch.setattr(feeds, "news_precheck", lambda gid: "")
    monkeypatch.setattr(feeds, "_ensure_search", _anoop)
    monkeypatch.setattr(feeds, "_rss_subscribed", lambda gid: False)
    monkeypatch.setattr(feeds, "_plan_focus", _focus)
    monkeypatch.setattr(feeds, "_collect_rss", _rss)
    monkeypatch.setattr(feeds, "_collect_two_phase", _collect)
    monkeypatch.setattr(feeds, "_merge_rss_candidates", lambda *a, **k: 0)
    monkeypatch.setattr(news_recheck, "recheck", _recheck)
    monkeypatch.setattr(feeds, "_hard_reject_code", lambda gid, s, c: list(c))
    monkeypatch.setattr(feeds, "_score", _score)
    monkeypatch.setattr(feeds, "_hard_reject_model", lambda *a, **k: None)
    monkeypatch.setattr(feeds, "_resolve_dup_in_batch", lambda *a, **k: None)
    monkeypatch.setattr(feeds, "_limit_followups", lambda *a, **k: None)
    monkeypatch.setattr(feeds, "_web_gate", lambda *a, **k: None)
    monkeypatch.setattr(feeds, "_dedup_homogeneous", lambda *a, **k: None)
    monkeypatch.setattr(feeds, "_write_posts", _anoop)
    monkeypatch.setattr(feeds, "_stored_url_keys", lambda *a, **k: set())
    monkeypatch.setattr(feeds, "_round_stats", lambda *a, **k: {})
    return feeds, sp


@pytest.mark.asyncio
async def test_news_round_published_posts_notes(store, settings, agents, monkeypatch):
    feeds, sp = _round_feeds(store, settings, agents, monkeypatch, outcome="published")
    kept = await feeds.prepare_news(G1)
    assert kept == 1
    batch = store.read().execute("SELECT kept, skipped FROM news_batches WHERE group_id=?", (G1,)).fetchone()
    assert (batch["kept"], batch["skipped"]) == (1, 0)
    assert len(sp.hids) == 2  # 核验 + 补打开两张 news 交接单
    assert all(agents.handoff(G1, h)["status"] == "accepted" for h in sp.hids)
    board = _board(store)
    assert sorted(b for _, b, _ in board) == ["recheck 环节留的便条", "verify 环节留的便条"]
    assert {h for _, _, h in board} == set(sp.hids)
    assert all(f == "news" for f, _, _ in board)


@pytest.mark.asyncio
async def test_news_round_all_rejected_drops_notes(store, settings, agents, monkeypatch):
    feeds, sp = _round_feeds(store, settings, agents, monkeypatch, outcome="all_rejected")
    kept = await feeds.prepare_news(G1)
    assert kept == 0
    batch = store.read().execute("SELECT kept, skipped FROM news_batches WHERE group_id=?", (G1,)).fetchone()
    assert (batch["kept"], batch["skipped"]) == (0, 1)
    # 环节照样检查通过（交回了结构有效的产物），但这批没出 → 不贴
    assert sp.hids and all(agents.handoff(G1, h)["status"] == "accepted" for h in sp.hids)
    assert _board(store) == []


@pytest.mark.asyncio
async def test_news_round_skipped_drops_notes(store, settings, agents, monkeypatch):
    feeds, sp = _round_feeds(store, settings, agents, monkeypatch, outcome="no_candidates")
    kept = await feeds.prepare_news(G1)
    assert kept == 0
    batch = store.read().execute("SELECT kept, skipped FROM news_batches WHERE group_id=?", (G1,)).fetchone()
    assert batch["skipped"] == 1
    assert sp.hids
    assert _board(store) == []
