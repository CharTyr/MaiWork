"""构想「先手小样」（构想先手小样，Item 5）后端测试。

- 专岗调查那轮**真用 fetch_page 打开成功**的 URL（tool_calls 里 ok=1、output 非空）
  才进得去 preview.sources 白名单；只搜过 / 打开失败 / 返回空 / 纯编出来的链接一律剥掉；
- 小样随构想同事务落 kv["idea.preview.<id>"]（零表迁移）；kind=talk 永不落小样；
- 落库前 summary / points / sources.title 全过隐私闸（_scrub_item_text），
  一条不过整块小样丢，构想本身照走；
- ideas_view 的 "preview" 只给 help 构想带（kv 里那份已过完两道闸）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from CharTyr_MaiWork.maiwork import clock  # noqa: E402
from CharTyr_MaiWork.maiwork.feeds import (  # noqa: E402
    IDEA_PREVIEW_KV_PREFIX,
    Feeds,
    get_idea_preview,
)
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402
from CharTyr_MaiWork.maiwork.workers import WorkerReport  # noqa: E402
from fakes import IDEA_BASIS, seed_idea_chat  # noqa: E402
from mafakes_specialists import FakeAgents, FakeSpecialists  # noqa: E402

G1 = "900000001"
OPENED_URL = "https://docs.example.com/guide"
UNOPENED_URL = "https://unopened.example.com"
EVIDENCE = "这手柄放半年了，还能救活吗"


class _Settings:
    def __init__(self, gid=G1):
        self.groups = {gid: object()}
        self.feeds = type("F", (), {
            "news_slots": None, "idea_slots": None,
            "lookback_days": 14, "web_min_avg": 3.0, "pool_min_avg": 4.0,
            "max_items": 10, "collect_minutes": 15, "guides": True,
            "blocked_domains": (), "verify_enabled": False,
        })()

    def is_served(self, gid) -> bool:
        return str(gid) in self.groups

    def workspace_of(self, gid) -> str:
        return f"g{gid}"


class _Models:
    """按 purpose 回 JSON 的假模型（make_idea 只调一次 feeds.idea）。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def chat(self, role=None, messages=None, **kwargs):
        purpose = str(kwargs.get("purpose") or "")
        self.calls.append({"role": role, "messages": messages, "purpose": purpose})
        text = self.payload if purpose == "feeds.idea" else "{}"
        return type("R", (), {"text": text})()

    def settings(self):
        return type("S", (), {"ready": lambda self: True})()


class _Profiles:
    def entries(self, gid):
        return []


class _Topics:
    def __init__(self):
        self.calls: list[dict] = []

    def add_candidate(self, gid, **kw):
        self.calls.append({"gid": str(gid), **kw})


class TelemetrySpecialists(FakeSpecialists):
    """给 FakeSpecialists 接上「工具遥测」：按预设把这轮的 fetch_page 结果
    写进 tool_calls（task_id = 调用方给的 idea-inv 标记）。"""

    def __init__(self, agents, store, results=None, fetch_calls=None):
        super().__init__(agents, results=results)
        self._store = store
        self._fetch_calls = list(fetch_calls or [])

    async def run_auto(self, kind, brief, **kwargs):
        report = await super().run_auto(kind, brief, **kwargs)
        if str(kind) == "idea" and str(kwargs.get("phase") or "") == "investigate":
            task_id = str(kwargs.get("task_id") or "")
            for call in self._fetch_calls:
                with self._store.tx() as conn:
                    conn.execute(
                        "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool,"
                        " input, output, ms, ok, error) VALUES (?, ?, ?, ?, 'fetch_page', ?, ?, 0, ?, '')",
                        (
                            clock.now(), str(kwargs.get("group_id") or ""), task_id, "构想调查",
                            json.dumps({"url": call["url"]}, ensure_ascii=False),
                            str(call.get("output") or ""), int(call.get("ok", 1)),
                        ),
                    )
        return report


def _mk(tmp_path, *, results, fetch_calls=None, main_payload, idea_enabled=True):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _Settings()
    agents = FakeAgents(enabled_kinds={"idea": bool(idea_enabled)})
    sp = TelemetrySpecialists(agents, store, results=results, fetch_calls=fetch_calls)
    models = _Models(main_payload)
    feeds = Feeds(store, models, None, _Profiles(), _Topics(), lambda: settings)
    feeds._specialists = sp
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (G1, clock.now()),
        )
    seed_idea_chat(store, G1, now=clock.now())
    return store, feeds, sp, models


def _specialist_candidate(**over):
    """idea 专岗调查交回的候选（data["idea"]）。"""
    cand = {
        "kind": "help",
        "title": "查清手柄常见故障",
        "body": "查这款手柄的常见故障并给出复活步骤",
        "evidence": EVIDENCE,
        "basis": dict(IDEA_BASIS),
        "origin": "手柄维修",
    }
    cand.update(over)
    return cand


def _idea_payload(**idea_over):
    """主模型的 feeds.idea 回包。"""
    idea = {
        "kind": "help",
        "title": "我可以帮群把手柄救活",
        "body": "查常见故障再出复活步骤",
        "evidence": EVIDENCE,
        "basis": dict(IDEA_BASIS),
        "icon": "gamepad",
        "chat_worthy": False,
        "keywords": ["手柄"],
        "items": [{"kind": "task", "title": "查清故障原因", "desc": "先出一版"}],
        "feasibility": {
            "level": "ok", "note": "联网查资料整理成步骤",
            "uses": ["search", "write"], "deliver": "doc", "needs_members": False,
        },
    }
    idea.update(idea_over)
    return json.dumps({"idea": idea}, ensure_ascii=False)


def _report(cand) -> WorkerReport:
    return WorkerReport(ok=True, summary="调查交回", data={"idea": cand})


def _kv_rows(store) -> dict:
    rows = store.read().execute("SELECT key, value FROM kv").fetchall()
    return {str(r["key"]): r["value"] for r in rows}


# ----------------------------------------------------------------------
# get_idea_preview 形状清洗（kv 读侧，永远不许抛）
# ----------------------------------------------------------------------


def test_get_idea_preview_missing_and_bad_shape(tmp_path) -> None:
    store = Store(tmp_path / "t.db")
    store.migrate()
    assert get_idea_preview(store, 1) is None
    assert get_idea_preview(store, "不是数字") is None

    with store.tx() as conn:
        store.kv_set(conn, f"{IDEA_PREVIEW_KV_PREFIX}7", {"points": "不是列表", "sources": []})
        store.kv_set(conn, f"{IDEA_PREVIEW_KV_PREFIX}8", "字符串不是表")
        store.kv_set(conn, f"{IDEA_PREVIEW_KV_PREFIX}9", {
            "summary": "",  # 空 → 回退 points[0][:80]
            "points": ["要点一"],
            "sources": [{"title": "", "url": "https://a.example.com/x"}],  # 空标题 → hostname
        })
    assert get_idea_preview(store, 7) is None
    assert get_idea_preview(store, 8) is None
    pv = get_idea_preview(store, 9)
    assert pv == {
        "summary": "要点一",
        "points": ["要点一"],
        "sources": [{"title": "a.example.com", "url": "https://a.example.com/x"}],
    }

    class _BadStore:
        def kv_get(self, key, default=None):
            raise RuntimeError("boom")

    assert get_idea_preview(_BadStore(), 1) is None


# ----------------------------------------------------------------------
# 端到端：专岗 fetch_page 打开成功 → 主模型采用小样 → kv + ideas_view
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_e2e_saved_and_surfaced(tmp_path) -> None:
    store, feeds, sp, models = _mk(
        tmp_path,
        results=[_report(_specialist_candidate(preview={
            "summary": "九成是电池触点氧化",
            "points": ["先测电池触点", "再查排线"],
            "sources": [
                {"title": "维修指南", "url": OPENED_URL},
                {"title": "没打开过的", "url": UNOPENED_URL},  # 幻觉链接 → 剥掉
            ],
        }))],
        fetch_calls=[
            {"url": OPENED_URL, "ok": 1, "output": "页面正文……"},
            {"url": "https://searched.example.com", "ok": 0, "output": ""},  # 打开失败不算
        ],
        main_payload=_idea_payload(preview={
            "summary": "先查电池触点氧化",
            "points": ["测触点", "查排线"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }),
    )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0

    # 调查标记真传给专岗了（工具记录按它归拢）
    inv_marks = [r["task_id"] for r in sp.runs_of("idea")]
    assert inv_marks and all(str(m).startswith(f"idea-inv:{G1}:") for m in inv_marks)

    saved = store.kv_get(f"{IDEA_PREVIEW_KV_PREFIX}{idea_id}")
    assert saved is not None
    assert saved["summary"] == "先查电池触点氧化"
    assert saved["points"] == ["测触点", "查排线"]
    assert saved["sources"] == [{"title": "维修指南", "url": OPENED_URL}]

    # 主模型提示词里带的是**核验后**的小样（幻觉链接已被剥掉，进不了白名单提示）
    prompt = models.calls[0]["messages"][0]["content"]
    assert OPENED_URL in prompt
    assert UNOPENED_URL not in prompt

    for admin in (False, True):
        view = [v for v in feeds.ideas_view(G1, admin=admin) if v["id"] == idea_id]
        assert view and view[0]["kind"] == "help"
        assert view[0]["preview"] == saved


@pytest.mark.asyncio
async def test_preview_all_sources_stripped_idea_still_saves(tmp_path) -> None:
    """专岗 / 主模型给的 URL 全没真打开过 → 小样整体丢（preview=None），构想照样入库。"""
    store, feeds, _sp, _m = _mk(
        tmp_path,
        results=[_report(_specialist_candidate(preview={
            "summary": "结论",
            "points": ["要点"],
            "sources": [{"title": "假来源", "url": UNOPENED_URL}],
        }))],
        fetch_calls=[
            # 这轮唯一的 fetch_page 还失败了（ok=0 / output 空）→ 一个能用的都没有
            {"url": UNOPENED_URL, "ok": 0, "output": ""},
        ],
        main_payload=_idea_payload(preview={
            "summary": "结论", "points": ["要点"],
            "sources": [{"title": "假来源", "url": UNOPENED_URL}],
        }),
    )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0
    assert store.kv_get(f"{IDEA_PREVIEW_KV_PREFIX}{idea_id}") is None
    row = [v for v in feeds.ideas_view(G1) if v["id"] == idea_id]
    assert row and row[0]["preview"] is None


@pytest.mark.asyncio
async def test_preview_main_model_cannot_hallucinate_without_fetch(tmp_path) -> None:
    """专岗这轮一个页面都没打开（候选连 preview 都没给）→ 主模型自己报的 preview 必丢。"""
    store, feeds, _sp, _m = _mk(
        tmp_path,
        results=[_report(_specialist_candidate())],  # 没 preview、没 fetch_page 记录
        main_payload=_idea_payload(preview={
            "summary": "凭空编的结论",
            "points": ["凭空编的要点"],
            "sources": [{"title": "编的", "url": UNOPENED_URL}],
        }),
    )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0
    assert store.kv_get(f"{IDEA_PREVIEW_KV_PREFIX}{idea_id}") is None
    row = [v for v in feeds.ideas_view(G1) if v["id"] == idea_id]
    assert row and row[0]["preview"] is None


@pytest.mark.asyncio
async def test_preview_role_disabled_never_saved(tmp_path) -> None:
    """idea 岗停用：专岗根本没跑（绝无 _opened_urls），主模型报 preview 也必丢。"""
    store, feeds, sp, _m = _mk(
        tmp_path,
        results=[_report(_specialist_candidate(preview={
            "summary": "结论", "points": ["要点"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }))],
        idea_enabled=False,
        main_payload=_idea_payload(preview={
            "summary": "结论", "points": ["要点"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }),
    )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0
    assert sp.runs_of("idea") == []
    assert store.kv_get(f"{IDEA_PREVIEW_KV_PREFIX}{idea_id}") is None
    row = [v for v in feeds.ideas_view(G1) if v["id"] == idea_id]
    assert row and row[0]["preview"] is None


@pytest.mark.asyncio
async def test_preview_talk_kind_never_saved(tmp_path) -> None:
    """kind=talk：哪怕专岗开了页、主模型报了 preview，也永不落小样。"""
    store, feeds, _sp, _m = _mk(
        tmp_path,
        results=[_report(_specialist_candidate(preview={
            "summary": "结论", "points": ["要点"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }))],
        fetch_calls=[{"url": OPENED_URL, "ok": 1, "output": "正文"}],
        main_payload=_idea_payload(
            kind="talk", title="聊聊手柄怎么救", items=None, feasibility=None,
            preview={
                "summary": "结论", "points": ["要点"],
                "sources": [{"title": "维修指南", "url": OPENED_URL}],
            },
        ),
    )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0
    assert all(not str(k).startswith(IDEA_PREVIEW_KV_PREFIX) for k in _kv_rows(store))
    row = [v for v in feeds.ideas_view(G1) if v["id"] == idea_id]
    assert row and row[0]["kind"] == "talk" and row[0].get("preview") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leak_at", ["summary", "point", "source_title"])
async def test_preview_privacy_scrub_drops(tmp_path, leak_at: str) -> None:
    """小样任何一段含关注成员注记 / 画像摘要的 ≥8 字片段 → 整块丢，构想照走。"""
    secret = "他最近在偷偷准备考研二战"
    store, feeds, _sp, _m = _mk(
        tmp_path,
        results=[_report(_specialist_candidate(preview={
            "summary": "结论", "points": ["要点"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }))],
        fetch_calls=[{"url": OPENED_URL, "ok": 1, "output": "正文"}],
        main_payload=_idea_payload(preview={
            "summary": secret if leak_at == "summary" else "正常结论",
            "points": [secret if leak_at == "point" else "正常要点"],
            "sources": [{
                "title": secret if leak_at == "source_title" else "维修指南",
                "url": OPENED_URL,
            }],
        }),
    )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed, updated)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (G1, "u1", "阿帆", secret, clock.now()),
        )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0  # 构想本身不受影响
    assert store.kv_get(f"{IDEA_PREVIEW_KV_PREFIX}{idea_id}") is None
    row = [v for v in feeds.ideas_view(G1) if v["id"] == idea_id]
    assert row and row[0]["preview"] is None
    # 私密片段绝不能经小样落到任何 kv / 视图里
    assert secret not in json.dumps(list(_kv_rows(store).values()), ensure_ascii=False)
    assert secret not in json.dumps(row, ensure_ascii=False)


@pytest.mark.asyncio
async def test_preview_persona_summary_scrub_drops(tmp_path) -> None:
    """persona.summary（JSON 里那份）里的片段同样拦。"""
    secret = "私下说要攒钱给女朋友买手柄当生日礼物"
    store, feeds, _sp, _m = _mk(
        tmp_path,
        results=[_report(_specialist_candidate(preview={
            "summary": "结论", "points": ["要点"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }))],
        fetch_calls=[{"url": OPENED_URL, "ok": 1, "output": "正文"}],
        main_payload=_idea_payload(preview={
            "summary": "正常结论",
            "points": [f"要点：{secret}"],
            "sources": [{"title": "维修指南", "url": OPENED_URL}],
        }),
    )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, persona, removed, updated)"
            " VALUES (?, ?, ?, '', ?, 0, ?)",
            (G1, "u1", "阿帆", json.dumps({"summary": secret}, ensure_ascii=False), clock.now()),
        )
    idea_id = await feeds.make_idea(G1)
    assert isinstance(idea_id, int) and idea_id > 0
    assert store.kv_get(f"{IDEA_PREVIEW_KV_PREFIX}{idea_id}") is None
    row = [v for v in feeds.ideas_view(G1) if v["id"] == idea_id]
    assert row and row[0]["preview"] is None
