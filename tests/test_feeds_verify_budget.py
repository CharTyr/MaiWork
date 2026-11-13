"""核验子 agent（feeds-verify:）防打转（2026-09-30 线上实测后调整）。

线上实录：一个核验子 agent 为了给一条 YouTube 视频找发布时间，8 分钟里试了 23 次
fetch_page（invidious / piped 镜像、web.archive、oembed……），而搜索结果本身就带着发布
日期（2026-09-27），最后还因为「文章没有发布时间」被硬拒。修三件事：

A1. 代码硬上限（不是只写在提示词里）：一个注明核验任务的 fetch_page 次数 =
    min(组内条数 × 2 + 1, 6)（verify_budget.py，feeds._verify_batch 按组开账）；
    用完之后工具直接回一句「别再开了，用已经打开的内容交回」。
A2. 核验 brief 写明：只开候选链接本身（打不开最多再换一个备用地址），不许搜
    镜像 / 存档站 / oEmbed API；页面里没有可见发布日期，就用搜索结果自带的那条日期
    （brief 里把它列出来）。
A3. 日期兜底：核验交回的条目没拿到发布时间时，用撒网候选自带的搜索结果日期补上
    （published_ts），再进硬淘汰那道。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from test_feeds_quality import (
    _FOCUS_JSON,
    GID,
    NOW,
    _make_feeds,
    _run,
    _scores_json,
    _score,
    _TimePatch,
)

from fakes import FakeProfiles
from CharTyr_MaiWork.maiwork import discovery, verify_budget
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

from test_feeds_two_phase import (
    FakeBroadSearch,
    SeqWorkers,
    _candidate,
    _ok_report,
    _ready_two_phase_feeds,
    _verify_item,
)


# ----------------------------------------------------------------------
# A1 代码硬上限：verify_budget 账本本体
# ----------------------------------------------------------------------


def test_cap_for_is_items_times_two_plus_one_capped() -> None:
    """上限公式：约（组内条数 × 2）+ 1，最多 6。"""
    assert verify_budget.cap_for(1) == 3
    assert verify_budget.cap_for(2) == 5
    assert verify_budget.cap_for(3) == 6
    assert verify_budget.cap_for(12) == 6
    # 空组 / 怪值按最小 1 次算（这条候选至少开一次）
    assert verify_budget.cap_for(0) == 1


def test_budget_allows_up_to_cap_then_refuses() -> None:
    tid = "feeds-verify:a:b:0"
    verify_budget.open_run(tid, cap_page_calls=3)
    try:
        assert verify_budget.consume(tid)[0] is True   # 1
        assert verify_budget.consume(tid)[0] is True   # 2
        assert verify_budget.consume(tid)[0] is True   # 3 —— 到顶
        allowed, note = verify_budget.consume(tid)     # 4 —— 拒
        assert allowed is False
        assert note  # 拒绝时给一句面向子 agent 的中文原因
        assert "别再打开" in note and "交回" in note
        # 拒绝不继续累加：多试几次还是拒、一句话稳定返回
        allowed2, note2 = verify_budget.consume(tid)
        assert allowed2 is False and note2 == note
    finally:
        verify_budget.close_run(tid)
    # 关掉账本就恢复不受限
    assert verify_budget.consume(tid)[0] is True


def test_budget_unknown_task_is_unlimited() -> None:
    """没开账本的 task_id（普通任务、老模式找资讯）一律放行，不受影响。"""
    for _ in range(20):
        assert verify_budget.consume("feeds-collect:x:y:1")[0] is True
        assert verify_budget.consume("T-9")[0] is True


def test_budget_is_per_task_not_shared() -> None:
    """两组核验（:0 / :1 后缀）各算各的账，互不挤占。"""
    verify_budget.open_run("feeds-verify:a:b:0", cap_page_calls=1)
    verify_budget.open_run("feeds-verify:a:b:1", cap_page_calls=3)
    try:
        assert verify_budget.consume("feeds-verify:a:b:0")[0] is True
        assert verify_budget.consume("feeds-verify:a:b:0")[0] is False  # 这组 1 次就用完
        assert verify_budget.consume("feeds-verify:a:b:1")[0] is True   # 另一组照开
        assert verify_budget.consume("feeds-verify:a:b:1")[0] is True
    finally:
        verify_budget.close_run("feeds-verify:a:b:0")
        verify_budget.close_run("feeds-verify:a:b:1")


# ----------------------------------------------------------------------
# A1 落进工具：fetch_page handler 在 feeds-verify: 任务里被硬上限拦住
# ----------------------------------------------------------------------


def _public_dns(host: str):
    return ["93.184.216.34"]


def _tools_for_fetch(store: Store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"},
            text="<html><head><title>t</title></head><body>p</body></html>",
        )

    settings, _ = load_settings({})
    tools = Tools(store)
    register_builtin(
        tools, search=None, profiles=FakeProfiles(),
        http_transport=httpx.MockTransport(handler),
        get_settings=lambda: settings,
        resolver=_public_dns,
    )
    return tools


@pytest.mark.asyncio
async def test_fetch_page_refuses_after_verify_cap(tmp_path) -> None:
    """feeds-verify: 任务开了 1 次的账本：第 1 次真打开，第 2 次直接被工具拦——
    不再发请求，错误消息告诉子 agent 别再开、用已经打开的内容交回。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    tools = _tools_for_fetch(store)
    tid = "feeds-verify:g:1:2:0"
    verify_budget.open_run(tid, cap_page_calls=1)
    ctx = ToolContext(group_id=GID, task_id=tid, actor="子 agent #1", role="worker")
    try:
        r1 = await tools.call("fetch_page", {"url": "http://example.com/1"}, ctx)
        assert r1.ok
        r2 = await tools.call("fetch_page", {"url": "http://example.com/2"}, ctx)
        assert r2.ok is False
        assert "别再打开" in (r2.error or "")
        assert "交回" in (r2.error or "")
    finally:
        verify_budget.close_run(tid)
    # 请求数：被拦的那次根本没发出去（账本记录过几次打开有账可查）
    rows = store.read().execute(
        "SELECT ok FROM tool_calls WHERE task_id=? AND tool='fetch_page' ORDER BY id", (tid,)
    ).fetchall()
    assert len(rows) == 2  # 都落了库（可追溯），但第二次 ok=0 且没有真的网络请求
    assert [int(r["ok"]) for r in rows] == [1, 0]
    store.close()


@pytest.mark.asyncio
async def test_fetch_page_unlimited_outside_verify(tmp_path) -> None:
    """老模式找资讯（feeds-collect:）和普通任务：没有账本，不受核验上限影响。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    tools = _tools_for_fetch(store)
    for tid in ("feeds-collect:g:1:2", "T-9"):
        ctx = ToolContext(group_id=GID, task_id=tid, actor="子 agent #1", role="worker")
        for i in range(7):
            r = await tools.call("fetch_page", {"url": f"http://example.com/{i}"}, ctx)
            assert r.ok, (tid, i, r.error)
    store.close()


# ----------------------------------------------------------------------
# A1 wiring：feeds._verify_batch 给每一组开账本（上限随组内条数）、跑完关账
# ----------------------------------------------------------------------


def test_verify_batch_opens_budget_per_group(tmp_path) -> None:
    """2026-11 零子 agent 改版：挑 12 条后由 _open_candidates 开一份 feeds-collect 账本
    （上限=要开条数 12），跑完关账不残留，不派 feeds-verify 子 agent。"""
    from fakes import patch_two_phase_feeds

    preset = [
        {"title": f"S{i}", "url": f"https://s.com/{i}", "summary": f"摘要{i}",
         "kind": "news", "published": None, "fetched": True,
         "quote": f"这是第 {i} 条的正文依据。", "paywall": False}
        for i in range(12)
    ]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=[],
    )
    picks = [{"i": i, "kind": "news", "hook": f"理由{i}"} for i in range(12)]
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": picks}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    patch_two_phase_feeds(feeds, models, {1: preset[:4], 2: preset[4:8], 3: preset[8:]})
    models.reply_queue[1] = json.dumps({"picks": picks}, ensure_ascii=False)
    seen: Dict[str, int] = {}
    orig_open = verify_budget.open_run

    def spy_open(task_id: str, **kwargs) -> None:
        seen[task_id] = int(kwargs.get("cap_page_calls") or 0)
        orig_open(task_id, **kwargs)

    verify_budget.open_run = spy_open
    try:
        with _TimePatch():
            _run(feeds.prepare_news(GID))
    finally:
        verify_budget.open_run = orig_open
    col = [t for t in seen if t.startswith("feeds-collect:")]
    assert len(col) == 1, seen
    assert seen[col[0]] == 12, seen
    assert not any(t.startswith("feeds-verify:") for t in seen), seen
    assert workers.calls == []
    # 收尾关账：老账不残留（同账本已关，复用时应重新开账；不残留= consume 直接放行）
    assert verify_budget.consume(col[0])[0] is True


# ----------------------------------------------------------------------
# A2 零子 agent 直接打开：只开候选链接本身、保留搜索日期、未读到日期不编
# ----------------------------------------------------------------------


def test_verify_brief_forbids_mirror_rabbit_hole_and_keeps_search_date(tmp_path) -> None:
    """代码直接调用 fetch_page 只开候选链接本身（1 次），不找镜像/存档，保留自带搜索日期。"""
    from fakes import make_fixture_page_tools

    pub = NOW - 3 * 86400
    c = _candidate("https://youtu.be/abc123", title="某个视频", published=pub)
    c["published_ts"] = pub
    c["date_basis"] = "search"
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    tools = make_fixture_page_tools(store, [{
        "title": "某个视频", "url": "https://youtu.be/abc123",
        "summary": "视频介绍了新品细节。", "quote": "视频展示了最新细节。",
        "fetched": True, "page_published": "",
    }])
    called_urls: List[str] = []
    orig_call = tools.call

    async def _spy_call(name: str, args: dict, ctx: Any) -> Any:
        called_urls.append(str((args or {}).get("url") or ""))
        return await orig_call(name, args, ctx)

    tools.call = _spy_call  # type: ignore[method-assign]
    feeds.set_tools(tools)
    with _TimePatch():
        _run(feeds._open_candidates(GID, [c], task_id="feeds-collect:t1"))
    assert called_urls == ["https://youtu.be/abc123"]
    assert c["published_ts"] == pub
    assert workers.calls == []


def test_verify_brief_shows_candidate_search_date(tmp_path) -> None:
    """候选自带搜索日期的，收集阶段留住 published_ts，打分提示词里带出该日期。"""
    from fakes import FakeModelsQueue

    pub = NOW - 3 * 86400
    c = _candidate("https://youtu.be/abc123", title="某个视频", published=pub)
    c["published_raw"] = pub
    c["published_ts"] = pub
    c["date_basis"] = "search"
    c["_source_body"] = "视频正文里展示了很多新细节。"
    score_reply = json.dumps({"scores": [{
        "i": 0, "title": "某个视频", "summary": "摘要", "quote": "展示了很多新细节",
        "info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4,
        "profile": None, "topic": "视频", "grounded": True, "junk": False, "why": "w", "icon": "video",
    }]}, ensure_ascii=False)
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    feeds._models = FakeModelsQueue(ready=True, replies=[score_reply])
    with _TimePatch():
        _run(feeds._score(GID, settings, [c]))
    score_prompt = str(feeds._models.calls[-1][1][0]["content"])
    assert str(pub) in score_prompt or "发布于" in score_prompt


def test_verify_brief_without_search_date_still_mentions_fallback_rule(tmp_path) -> None:
    """候选本身没日期时，打分提示词明确告诉模型读不到日期就留空、别编。"""
    from fakes import FakeModelsQueue

    c = _candidate("https://example.com/a", title="没日期的", published=None)
    c["_source_body"] = "一篇没有写出发布时间的文章正文内容。"
    score_reply = json.dumps({"scores": [{
        "i": 0, "title": "没日期的", "summary": "摘要", "quote": "没有写出发布时间的文章",
        "published": "",
        "info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4,
        "profile": None, "topic": "话题", "grounded": True, "junk": False, "why": "w", "icon": "newspaper",
    }]}, ensure_ascii=False)
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    feeds._models = FakeModelsQueue(ready=True, replies=[score_reply])
    with _TimePatch():
        _run(feeds._score(GID, settings, [c]))
    score_prompt = str(feeds._models.calls[-1][1][0]["content"])
    assert "读不到给空字符串，别编" in score_prompt


# ----------------------------------------------------------------------
# A3 日期兜底：页面没拿到日期 → 保留撒网候选自带的搜索结果日期
# ----------------------------------------------------------------------


def test_verify_fills_published_from_discovery_candidate(tmp_path) -> None:
    """页面里找不到日期时，保留撒网候选自带的搜索日期；页面自带日期则以页面为准，不被覆盖。"""
    from CharTyr_MaiWork.maiwork import clock as _clock
    from fakes import _wrap_score_enrichment, make_fixture_page_tools

    page_items = [
        {
            "title": "生化危机 9 评测", "url": "https://v.com/no-date",
            "summary": "视频里详细评测了生化危机 9。", "kind": "guide",
            "page_published": "",  # 页面上找不到日期
            "fetched": True, "quote": "原文确实评测了生化危机 9。", "paywall": False,
        },
        {
            "title": "另一篇自带日期", "url": "https://v.com/with-date",
            "summary": "这篇自己在页面上标了日期。", "kind": "guide",
            "page_published": _clock.bj(NOW - 10 * 86400).strftime("%Y-%m-%d"),  # 页面自己的日期
            "fetched": True, "quote": "原文确实在。", "paywall": False,
        },
    ]
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    # 撒网候选自带搜索日期
    feeds._search = FakeBroadSearch(results=[
        {"title": "生化危机 9 评测", "url": "https://v.com/no-date", "snippet": "s",
         "published": NOW - 3 * 86400},
        {"title": "另一篇自带日期", "url": "https://v.com/with-date", "snippet": "s",
         "published": NOW - 5 * 86400},
    ])
    feeds.set_tools(make_fixture_page_tools(store, page_items))
    models._fixture_tools = feeds._tools
    _wrap_score_enrichment(models, page_items)

    seen_items: list[dict] = []
    orig_open_candidates = feeds._open_candidates

    async def spy_open(gid_, candidates_, *, task_id, funnel=None):
        await orig_open_candidates(gid_, candidates_, task_id=task_id, funnel=funnel)
        seen_items.extend(candidates_)

    feeds._open_candidates = spy_open  # type: ignore[method-assign]
    picks = [{"i": 0, "kind": "guide", "hook": "群里在等这个"}, {"i": 1, "kind": "guide", "hook": "也想看这个"}]
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": picks}, ensure_ascii=False),
        _scores_json(_score(0), _score(1)),
    ]
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    by_item = {str(it.get("url_key")): it for it in seen_items}
    item_no_date = by_item.get("v.com/no-date")
    item_with_date = by_item.get("v.com/with-date")
    # 页面没日期保留搜索结果日期；页面自带日期覆写成页面日期
    assert item_no_date is not None and item_with_date is not None
    assert float(item_no_date["published_ts"] or 0) == NOW - 3 * 86400
    assert abs(float(item_with_date["published_ts"] or 0) - (NOW - 10 * 86400)) < 86400.0
    rows = store.read().execute(
        "SELECT url_key, published_ts, rejected, reject_reason FROM news_items ORDER BY id"
    ).fetchall()
    by_key = {str(r["url_key"]): r for r in rows}
    no_date = by_key.get("v.com/no-date")
    with_date = by_key.get("v.com/with-date")
    assert no_date is not None and with_date is not None
    # 搜索结果自带的日期留了下来 → 「文章没有发布时间」这道硬拒没触发
    assert float(no_date["published_ts"] or 0) == NOW - 3 * 86400
    assert abs(float(with_date["published_ts"] or 0) - (NOW - 10 * 86400)) < 86400.0
    assert "没有发布时间" not in str(no_date["reject_reason"] or "")


# ----------------------------------------------------------------------
# A1 附带：零子 agent 打开原文不派 feeds-verify: 子 agent
# ----------------------------------------------------------------------


def test_verify_deadline_is_tight_box(tmp_path) -> None:
    """2026-11 零子 agent 改版：不再派 feeds-verify: 子 agent，workers.calls 为空。"""
    from fakes import _wire_fixture_page_tools, _wrap_score_enrichment

    preset = [
        {"title": "S0", "url": "https://s.com/0", "summary": "摘要",
         "kind": "news", "published": None, "fetched": True,
         "quote": "原文确实在。", "paywall": False},
    ]
    search = FakeBroadSearch(results=[
        {"title": "S0", "url": "https://s.com/0", "snippet": "s", "published": None},
    ])
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=[], search=search,
    )
    _wire_fixture_page_tools(feeds, preset)
    models._fixture_tools = feeds._tools
    _wrap_score_enrichment(models, preset)
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert workers.calls == []
