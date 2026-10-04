"""两阶段找资讯（「广撒网再挑着打开」）测试：撒网、保底、粗筛、挑着打开、验收、漏斗统计。

2026-09-30 用户决定：新找法对**所有服务群**恒生效，不再有每群开关
（kv feeds.two_phase 的旧行留着不管，但任何代码都不再读它；Feeds 上没有
two_phase_on / set_two_phase；GET/PUT /api/groups/{gid}/feeds-two-phase 已删）。

prepare_news 的第 ② 步固定为：
1. 撒网（2026-10-01 起：主模型在定关注点时一并给出每个方向的搜索计划 searches；
   代码照单并发搜——没有子 agent、没有工具调用；候选照旧落进撒网登记簿 discovery）；
2. 保底（代码）：某个方向搜不够（<2 次问 / <6 条候选）→ 代码直接补搜；
3. 粗筛（代码，不调模型）：已入库链接 / 屏蔽来源 / 太旧 / 标题近似撞掉，再按方向均衡（40% 上限）留 ≤24 条；
4. 挑（主模型一次 json_mode）：挑 8–12 条真去打开，每条带一句话理由（hook），失败回落前 10 条；
5. 核验（最多 3 个子 agent 并发，只用 fetch_page，task_id feeds-verify: 开头）：真打开原文按老格式交回；
6. 之后完全走老路（第一道 / 打分 / 帖子 / 入库）。

漏斗统计（kv feeds.batch_stats.<批次> 的 "funnel"）+ 可追溯：news_items.src_query / src_provider，
管理员视图的 "src" 字段。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from test_feeds_quality import (
    _FOCUS_JSON,
    GID,
    NOW,
    _make_feeds,
    _run,
    _scores_json,
    _score,
    _settings,
    _TimePatch,
)

from fakes import FakeCtx, FakeModelsQueue, FakeProfiles
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork import discovery
from CharTyr_MaiWork.maiwork.feeds import _normalize_url
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

PASSWORD = "测试密码-非常显眼-不要出现在日志里"
SECRET = "sk-test-十分显眼的密钥AaBbCc123"
G1 = "900000001"


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


class SeqWorkers:
    """按 task_id 分派的假 workers：每个核验子 agent 各回各的，全程记录调用。
    2026-10-01 起撒网不再派子 agent（代码按计划搜）——还收到 feeds-discover: 派工算 bug。"""

    def __init__(self, verify: list | None = None, others: Any = None) -> None:
        self.verify = list(verify or [])
        self.others = others  # 传给老路调用（一般用不到）
        self.calls: List[Dict[str, Any]] = []
        self.max_parallel = 0
        self._cur = 0

    async def run(self, brief: str, **kwargs: Any) -> Any:
        from CharTyr_MaiWork.maiwork.workers import WorkerReport

        task_id = str(kwargs.get("task_id") or "")
        call = {"brief": brief, **kwargs}
        self.calls.append(call)

        assert not task_id.startswith("feeds-discover:"), "撒网子 agent 已删除（2026-10-01）"
        if task_id.startswith("feeds-verify:"):
            # 并发度记录：进一个 +1，出来 -1
            self._cur += 1
            self.max_parallel = max(self.max_parallel, self._cur)
            try:
                await asyncio.sleep(0)  # 让 gather 里其他子 agent 有机会并发进来
                rep = self.verify.pop(0) if self.verify else None
                if isinstance(rep, BaseException):
                    raise rep
                return rep
            finally:
                self._cur -= 1
        rep = self.others
        if isinstance(rep, BaseException):
            raise rep
        return rep if rep is not None else WorkerReport(ok=True, summary="好", data={"items": []}, evidence=[], steps=1)


class FakeBroadSearch:
    """撒网保底用的假搜索：search / search_with / broad_providers 回放预设结果。"""

    def __init__(self, results=None, broad=None, with_results=None) -> None:
        self.results = list(results or [])
        self._broad = list(broad or [])
        self.with_results = with_results if with_results is not None else {}
        self.calls: List[tuple] = []
        self.with_calls: List[tuple] = []

    def _tag(self, results, provider):
        out = []
        for r in results:
            d = dict(r)
            d.setdefault("provider", provider)  # search.py 的真实口径：每条带 provider
            out.append(d)
        return out

    async def search(self, query, *, limit=8, days=None, site="", news=False):
        self.calls.append((query, limit, days, site, news))
        main = self._broad[0] if self._broad else "main"
        return self._tag(self.results, main)

    async def search_with(self, name, query, *, limit=8, days=None, site="", news=False):
        self.with_calls.append((name, query, limit, days))
        return self._tag(self.with_results.get(name, []), name)

    def broad_providers(self):
        return list(self._broad)


class ToolSearch:
    """给 web_search 工具用的假搜索：结果带 provider 字段（search.py 的真实口径）。
    calls 记 (query, limit, days, site, news)——2026-10-01 起代码撒网也用它（五元组）。"""

    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.calls: List[tuple] = []

    async def search(self, query, *, limit=8, days=None, site="", news=False):
        self.calls.append((query, limit, days, site, news))
        return [dict(r) for r in self.results]


def _ctx(**over):
    base = dict(group_id=GID, task_id="feeds-discover:1:2:3", actor="子 agent #1", role="worker")
    base.update(over)
    return ToolContext(**base)


def _candidate(url: str, *, title: str = "", focus: int | None = 1, query: str = "q",
               provider: str = "main", published=None) -> dict:
    """造一条撒网登记簿口径的候选（close_run 的返回形状手动拼）。"""
    return {
        "title": title or f"标题-{url}",
        "url": url,
        "snippet": f"摘要-{url}",
        "published": published,
        "query": query,
        "focus": focus,
        "provider": provider,
        "queries": [query],
    }


def _ok_report(data: dict, *, ok: bool = True, error: str = "") -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    rep = WorkerReport(ok=ok, summary="核验好" if ok else "", data=data, evidence=[], steps=2)
    if error:
        rep.error = error
    return rep


def _verify_item(url: str, *, title: str = "", kind: str = "news", published=NOW - 3600) -> dict:
    return {
        "title": title or f"核验过的-{url}",
        "url": url,
        "summary": "核验过的摘要：原文确实讲这件事。",
        "kind": kind,
        "published": published,
        "fetched": True,
        "quote": "原文里确实写着这件事。",
        "paywall": False,
        "image_url": "",
    }


_PLANNED_SEARCHES_FOR_FOCUS = (
    lambda q, i: [
        {"q": f"{q} 新进展", "site": "", "news": True, "kind": "news"},
        {"q": f"{q} 分析", "site": "", "news": False, "kind": "news"},
    ]
)


def _add_plans_to_focus_reply(reply: str) -> str:
    """给一份定关注点回复补上每个方向 2 条 searches（2026-10-01 起撒网照计划搜，
    让饿不着的方向保底不插手；解析不出的回复原样返回）。"""
    try:
        parsed = json.loads(reply)
    except Exception:
        return reply
    focus = parsed.get("focus") if isinstance(parsed, dict) else None
    if not isinstance(focus, list):
        return reply
    for i, f in enumerate(focus, 1):
        if isinstance(f, dict) and f.get("query"):
            q = str(f["query"])
            f.setdefault("searches", _PLANNED_SEARCHES_FOR_FOCUS(q, i))
    return json.dumps(parsed, ensure_ascii=False)


def _ready_two_phase_feeds(tmp_path, *, discover=None, verify=None, search=None, models=None):
    """假 workers（按 task_id 分派）+ 假搜索；返回常用几个对象。

    2026-09-30 起两阶段恒生效，不需要也不允许再拨开关；这里特意不碰 kv。
    2026-10-01 起撒网是代码按计划搜，不再派子 agent：discover 参数已废弃。
    定关注点回复就地补上 searches 计划（让饿不着的方向保底不插手）。
    """
    assert discover is None, "撒网子 agent 已删除：改用 search= 预设结果（代码按计划搜）"
    workers = SeqWorkers(verify=verify)
    kw: Dict[str, Any] = {"workers": workers}
    if models is not None:
        kw["models"] = models
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, **kw)
    if isinstance(models.reply_queue, list):
        models.reply_queue = [
            _add_plans_to_focus_reply(r) if isinstance(r, str) and '"focus"' in r else r
            for r in models.reply_queue
        ]
    if search is None:
        search = FakeBroadSearch()
    feeds._search = search
    return store, settings, feeds, models, workers, topics


# ----------------------------------------------------------------------
# 恒生效（2026-09-30 用户决定：不再有每群开关）
# ----------------------------------------------------------------------


def test_feeds_no_longer_has_two_phase_switch(tmp_path) -> None:
    """开关整体删除：Feeds 上没有 two_phase_on / set_two_phase / _two_phase_key。"""
    _store, _settings, feeds, _m, _w, _t, _p = _make_feeds(tmp_path)
    assert not hasattr(feeds, "two_phase_on")
    assert not hasattr(feeds, "set_two_phase")
    assert not hasattr(feeds, "_two_phase_key")


def test_two_phase_always_on_even_with_stale_kv(tmp_path) -> None:
    """不设 kv（以及留了旧 kv 行）都一样：prepare_news 恒走两阶段，不再有老单子 agent、
    也没有 feeds-discover: 的撒网子 agent（2026-10-01 起撒网是代码按计划搜）。"""
    for write_stale_kv in (False, True):
        store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
            tmp_path / f"case{int(write_stale_kv)}",
            verify=[_ok_report({"items": [_verify_item("https://a.com/1")]})],
            search=FakeBroadSearch(results=[
                {"title": "A", "url": "https://a.com/1", "snippet": "s", "published": None},
            ]),
        )
        if write_stale_kv:
            with store.tx() as conn:
                store.kv_set(conn, "feeds.two_phase", ["999"])  # 旧行残留：不许再有人读
        models.reply_queue = [
            _FOCUS_JSON,
            json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得打开"}]}, ensure_ascii=False),
            _scores_json(_score(0)),
        ]
        with _TimePatch():
            kept = _run(feeds.prepare_news(GID))
        assert kept == 1, write_stale_kv
        discovers = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-discover:")]
        assert discovers == [], workers.calls  # 撒网不再派子 agent
        verifies = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-verify:")]
        assert verifies and all(v["tools"] == ["fetch_page"] for v in verifies), workers.calls
        # 老路的标志：一个 web_search+fetch_page 一把梭、task_id feeds-collect: 的子 agent——不再存在
        collects = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-collect:")]
        assert collects == [], workers.calls
    assert not hasattr(feeds, "two_phase_on")


# ----------------------------------------------------------------------
# 撒网登记簿（discovery.py）
# ----------------------------------------------------------------------


def test_discovery_registry_records_and_dedupes():
    """登记簿：同一链接（规范化后）只留先见的那条，queries 攒齐碰过的问法；没开着的 run 直接忽略。"""
    tid = "feeds-discover:unit:1:1"
    discovery.open_run(tid)
    discovery.record(tid, query="q1", focus=1, provider="main", results=[
        {"title": "甲", "url": "https://a.com/x/?utm_source=zz", "snippet": "s1", "published": 1700000000.0},
        {"title": "乙", "url": "https://b.com/y", "snippet": "s2", "published": None},
    ])
    discovery.record(tid, query="q2", focus=2, provider="other", results=[
        {"title": "甲-另一个名", "url": "https://a.com/x/", "snippet": "s1b", "published": 1700000000.0},
    ])
    discovery.record("feeds-discover:别的", query="q3", focus=1, provider="main", results=[
        {"title": "丙", "url": "https://c.com/z", "snippet": "s3", "published": None},
    ])  # 没开过的 run：直接忽略，不炸
    out = discovery.close_run(tid)
    assert [c["url"] for c in out] == ["https://a.com/x/?utm_source=zz", "https://b.com/y"]
    first, second = out
    assert first["query"] == "q1" and first["focus"] == 1 and first["provider"] == "main"
    assert first["queries"] == ["q1", "q2"]
    assert second["focus"] == 1 and second["provider"] == "main"
    assert second["queries"] == ["q1"]  # b.com 只被 q1 碰到过
    assert first["published"] == 1700000000.0 and second["published"] is None
    # 关掉后再记没用，也没残留
    discovery.record(tid, query="q4", focus=1, provider="main", results=[
        {"title": "丁", "url": "https://d.com/w", "snippet": "s4", "published": None},
    ])
    assert discovery.close_run(tid) == []


@pytest.mark.asyncio
async def test_web_search_tool_no_registry_recording_anymore(tmp_path) -> None:
    """2026-10-01 起撒网登记由代码撒网自己做（feeds._run_planned_searches），
    web_search 工具不再往登记簿记；focus 参数也随之从工具声明里拿掉。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
    tools = Tools(store)
    search = ToolSearch([
        {"title": "甲", "url": "https://a.com/1", "snippet": "s", "published": None, "provider": "keenable"},
    ])
    register_builtin(tools, search=search, profiles=FakeProfiles(), get_settings=lambda: settings)
    tid = "feeds-discover:tool:1:1"  # 同名的 run 开着也不记（登记只剩代码撒网一个写口）
    discovery.open_run(tid)
    try:
        r = await tools.call("web_search", {"query": "测试词", "focus": 3}, _ctx(task_id=tid))
        assert r.ok
    finally:
        out = discovery.close_run(tid)
    assert out == []
    # focus 参数已不在工具声明里（模型不再被要求标 focus=<编号>）
    tool = tools._tools["web_search"]
    assert "focus" not in (tool.parameters.get("properties") or {})
    # 任何 task_id 传 focus 也不炸
    r2 = await tools.call("web_search", {"query": "别的", "focus": "不是数字"}, _ctx(task_id="T-9"))
    assert r2.ok


# ----------------------------------------------------------------------
# 撒网（discover）+ 保底（floor）
# ----------------------------------------------------------------------


def test_planned_searches_run_exactly_as_planned(tmp_path) -> None:
    """定关注点给的 searches 就照单执行：q/site/news/days（news 7 天、guide 180 天）映射，
    同一方向的结果带 1 起的 focus 编号落登记簿；不再有任何 feeds-discover: 派工。"""

    class ByQuerySearch:
        """和线上一致的回放：每一次搜索出的链接都不同（真的这么散）。"""

        def __init__(self) -> None:
            self.calls: List[tuple] = []

        async def search(self, query, *, limit=8, days=None, site="", news=False):
            self.calls.append((query, limit, days, site, news))
            return [{"title": query, "url": f"https://a.com/{len(self.calls)}",
                     "snippet": "s", "published": None, "provider": "keenable"}]

    search = ByQuerySearch()
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path, search=search)
    focus = [
        {"query": "生化危机 9", "why": "", "angle": "", "source": "recent", "searches": [
            {"q": "生化危机9 战斗系统", "site": "", "news": True, "kind": "news"},
            {"q": "生化危机9 豪华版提前解锁", "site": "", "news": False, "kind": "news"},
            {"q": "生化危机9 官方公告", "site": "capcom.com", "news": False, "kind": "news"},
            {"q": "生化危机9 通关评测", "site": "", "news": False, "kind": "guide"},
        ]},
        {"query": "开源掌机", "why": "", "angle": "", "source": "long", "searches": [
            {"q": "开源掌机 新品", "site": "", "news": True, "kind": "news"},
        ]},
    ]
    funnel: dict = {"queries": 0, "providers": {}, "per_focus": [], "timings_s": {}}
    discovery.open_run("feeds-discover:t:1:1")
    err = _run(feeds._run_planned_searches(GID, focus, settings, "feeds-discover:t:1:1", funnel))
    assert err is None
    got = discovery.close_run("feeds-discover:t:1:1")
    by_q = {c[0]: c for c in search.calls}
    # 照计划各搜一次：site / news / days 映射（guide 放宽到 180 天）
    assert set(by_q) == {"生化危机9 战斗系统", "生化危机9 豪华版提前解锁", "生化危机9 官方公告", "生化危机9 通关评测", "开源掌机 新品"}
    assert by_q["生化危机9 战斗系统"] == ("生化危机9 战斗系统", 10, 7, "", True)
    assert by_q["生化危机9 豪华版提前解锁"] == ("生化危机9 豪华版提前解锁", 10, 7, "", False)
    assert by_q["生化危机9 官方公告"] == ("生化危机9 官方公告", 10, 7, "capcom.com", False)
    assert by_q["生化危机9 通关评测"] == ("生化危机9 通关评测", 10, 180, "", False)
    # 方向 1 的四搜出的 4 条链接都在登记簿（focus=1）；方向 2 的在 focus=2
    f1 = [c for c in got if c["focus"] == 1]
    f2 = [c for c in got if c["focus"] == 2]
    assert len(f1) == 4 and len(f2) == 1
    # 同一条链接被同方向多搜碰到时，问法全攒进先见那条的 queries（登记簿按链接去重）
    assert {q for c in f1 for q in c["queries"]} == {
        "生化危机9 战斗系统", "生化危机9 豪华版提前解锁", "生化危机9 官方公告", "生化危机9 通关评测",
    }
    assert funnel["queries"] == 5


def test_planned_searches_fall_back_to_focus_query(tmp_path) -> None:
    """searches 没了 / 给得不对（不是列表 / 空列表 / 全不合法）→ 回退成 focus["query"] 一搜。"""
    search = ToolSearch(results=[
        {"title": "甲", "url": "https://a.com/1", "snippet": "s", "published": None, "provider": "m"},
    ])
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path, search=search)
    focus = [
        {"query": "方向名一句", "why": "", "angle": "", "source": "", "searches": []},
        {"query": "另一个方向", "why": "", "angle": "", "source": "",
         "searches": [{"q": "  "}, {"q": ""}, "不是对象"]},
    ]
    funnel: dict = {"queries": 0, "providers": {}, "per_focus": [], "timings_s": {}}
    discovery.open_run("feeds-discover:t:2:2")
    err = _run(feeds._run_planned_searches(GID, focus, settings, "feeds-discover:t:2:2", funnel))
    out = discovery.close_run("feeds-discover:t:2:2")
    assert err is None
    assert [c[0] for c in search.calls] == ["方向名一句", "另一个方向"]
    assert all(c[2] == 7 for c in search.calls)  # 回退一律按资讯 7 天
    # 两条都进了登记簿（同链接去重留先见），focus 各记各的
    assert out and out[0]["focus"] == 1


def test_planned_searches_capped_and_deduped(tmp_path) -> None:
    """一轮计划总数封顶 + 同 (q, site, news, kind) 去重；方向里给多了也最多 5 条。"""
    search = ToolSearch(results=[])
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path, search=search)
    many = [
        {"q": f"词{i}", "site": "", "news": False, "kind": "news"} for i in range(9)
    ]
    dup = [
        {"q": "同一句", "site": "", "news": True, "kind": "news"},
        {"q": "同一句", "site": "", "news": True, "kind": "news"},
        {"q": "同一句", "site": "x.com", "news": True, "kind": "news"},
    ]
    focus = [
        {"query": "A", "why": "", "angle": "", "source": "", "searches": many},
        {"query": "B", "why": "", "angle": "", "source": "", "searches": dup},
        *[
            # 方向名前缀防「同一搜索词去重」跨方向不生效（不同方向的同一句各搜各的）
            {"query": f"C{i}", "why": "", "angle": "", "source": "",
             "searches": [dict(s, q=f"C{i}-{s['q']}") for s in many]}
            for i in range(4)
        ],
    ]
    funnel: dict = {"queries": 0}
    discovery.open_run("feeds-discover:t:3:3")
    err = _run(feeds._run_planned_searches(GID, focus, settings, "feeds-discover:t:3:3", funnel))
    assert err is None
    # 方向 A 给 9 条砍到 5；方向 B 去重后 2 条；再加 4 个方向各 5 条 = 27 ≤ 30 的帽
    assert funnel["queries"] == len(search.calls) == 5 + 2 + 20


def test_planned_searches_concurrency_bounded(tmp_path) -> None:
    """代码撒网并发受信号量限流：峰值 <= 并发上限常数。"""
    import asyncio as _asyncio

    from CharTyr_MaiWork.maiwork import feeds as feeds_mod

    search = ToolSearch(
        results=[{"title": "t", "url": "https://a.com/1", "snippet": "s", "published": None, "provider": "m"}],
    )
    cur, peak = 0, 0
    lock = _asyncio.Lock()

    async def slow_search(query, *, limit=8, days=None, site="", news=False):
        nonlocal cur, peak
        async with lock:
            cur += 1
            peak = max(peak, cur)
        await _asyncio.sleep(0.01)
        async with lock:
            cur -= 1
        search.calls.append((query, limit, days, site, news))
        return [dict(r) for r in search.results]

    search.search = slow_search
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path, search=search)
    # 7 个方向、各 5 条互不相同的计划 = 35 条 → 一轮 30 条的帽会封顶
    focus = [
        {"query": f"方向{n}", "why": "", "angle": "", "source": "",
         "searches": [{"q": f"方向{n}-词{i}", "site": "", "news": False, "kind": "news"} for i in range(5)]}
        for n in range(7)
    ]
    funnel: dict = {"queries": 0}
    discovery.open_run("feeds-discover:t:4:4")
    err = _run(feeds._run_planned_searches(GID, focus, settings, "feeds-discover:t:4:4", funnel))
    assert err is None
    assert funnel["queries"] == 30  # 一轮 30 条的帽
    assert 1 < peak <= feeds_mod.PLANNED_SEARCH_CONCURRENCY, peak


def test_planned_searches_all_fail_raises_like_before(tmp_path) -> None:
    """每一搜都挂（含保底）+ 一条都没搜出 = 撒网垮：照老句式「子 agent 没找到东西」跳过这轮。"""

    class FailingSearch:
        async def search(self, query, **kw):
            if str(query).startswith("__配置自检__"):
                return []  # 配自检放行（走到撒网那步才算全挂）
            raise RuntimeError("搜索全挂")

        def broad_providers(self):
            return []

    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, search=FailingSearch(),
    )
    models.reply_queue = [_FOCUS_JSON]
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 0
    row = store.read().execute("SELECT note FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert "子 agent 没找到东西" in str(row["note"]) or "子 agent 出了意外" in str(row["note"])


def test_planned_no_search_object_means_skip(tmp_path) -> None:
    """self._search 是 None（测试没注入）：一搜不发、不算垮（候选空照旧按老路回落）。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    feeds._search = None
    funnel: dict = {"queries": 0}
    discovery.open_run("feeds-discover:t:5:5")
    err = _run(feeds._run_planned_searches(
        GID, [{"query": "方向", "why": "", "angle": "", "source": ""}], settings,
        "feeds-discover:t:5:5", funnel,
    ))
    discovery.close_run("feeds-discover:t:5:5")
    assert err is None
    assert funnel["queries"] == 0


def test_per_focus_floor_triggers_code_searches(tmp_path) -> None:
    """计划里没有几条独特的问法（或者计划都搜不出东西）→ 饿着（问 <2 次 / 候选 <6 条）
    的方向由保底代码补搜：主家一次 + 撒网多一家各一次。"""
    search = FakeBroadSearch(
        results=[{"title": "X", "url": "https://x.com/1", "snippet": "s", "published": None}],
        broad=["main", "extra"],
        with_results={"extra": [{"title": "Y", "url": "https://y.com/2", "snippet": "s", "published": None}]},
    )
    verify = [_ok_report({"items": [_verify_item("https://x.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=search,
    )
    models.reply_queue = [
        _FOCUS_JSON,  # 3 个方向、不带 searches → 每方向只搜 1 条（饿着） → 保底补 1 主家 + 1 extra
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得打开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    # 三个方向各回退搜 1 次（days=7），再各被保底补 1 次主家（days=7；配自检探测那条不算）；
    # 「FPGA 新动态」按代码兜底算国际方向（名字带英文词）且问法全中文 → 额外补 1 次英文（=7 主家搜）。
    real_calls = [c for c in search.calls if c[0] != "__配置自检__"]
    assert len(real_calls) == 3 + 3 + 1, search.calls
    assert all(call[2] == 7 for call in real_calls)
    # 补的英文搜索用的是方向的英文名（纯英文、无汉字）
    qs = [str(c[0]) for c in real_calls]
    english_patches = [q for q in qs if any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in q)
                       and not any("一" <= ch <= "鿿" for ch in q)]
    assert english_patches == ["FPGA"], qs
    # 撒网多一家：每个饿着的方向也用 search_with 各补一次
    assert len(search.with_calls) >= 3
    assert all(name == "extra" for name, *_ in search.with_calls)


def test_floor_errors_ignored(tmp_path) -> None:
    """保底补搜全部出错也不拖累这轮：还靠计划搜到的那几条往下走。"""

    class BadSearch:
        async def search(self, query, **kw):
            if str(query).startswith("__配置自检__"):
                return []  # 自检探测放行
            if "FPGA" in str(query):  # 计划搜那条放行（登记簿能收一条往下走）
                return [{"title": "好", "url": "https://good.com/1", "snippet": "s", "published": None}]
            raise RuntimeError("保底补搜全挂")

        def broad_providers(self):
            return []

    verify = [_ok_report({"items": [_verify_item("https://good.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=BadSearch(),
    )
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得打开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1


# ----------------------------------------------------------------------
# 粗筛（prefilter）
# ----------------------------------------------------------------------


def test_prefilter_drops_stored_blocked_old_and_near_dup(tmp_path) -> None:
    """已入库链接、屏蔽来源、超 180 天（有日期的）、标题近似（候选之间 & 撞已发过的）都被筛掉。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        with store.tx() as conn:
            store.kv_set(conn, f"feeds.blocked.{GID}", ["bad.example"])
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (GID, NOW - 86400, NOW - 86400),
            )
            conn.execute(
                "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                " reject_gate, reject_reason, angle, image_url, bridge)"
                " VALUES (?, ?, 'newspaper', '开源掌机周报火热出炉', 's', '', '[]', ?, ?, 4, 'pool', NULL, 0,"
                " NULL, 0, 0, ?, 'news', '', '', 0, '', 0, NULL, NULL, '', '', '')",
                (int(cur.lastrowid), GID, _normalize_url("https://seen.com/post"),
                 NOW - 86400, NOW - 86400),
            )
        candidates = [
            _candidate("https://seen.com/post", title="这个链接已经出过"),              # 撞已入库链接
            _candidate("https://bad.example/news/1", title="屏蔽来源的"),                   # 屏蔽域名
            _candidate("https://old.com/1", title="太旧的", published=NOW - 200 * 86400),  # 超 180 天
            _candidate("https://fresh.com/1", title="量子芯片全新架构发布", published=NOW - 100),
            _candidate("https://fresh.com/2", title="量子芯片全新架构发布", published=NOW - 100),  # 标题和候选撞
            _candidate("https://fresh.com/3", title="开源掌机周报火热出炉", published=NOW - 100),  # 标题撞已发过的
            _candidate("https://fresh.com/4", title="家用路由折腾史记录", published=NOW - 100),   # 活口
        ]
        kept, drops, _stats = feeds._prefilter(GID, settings, candidates)
    # 标题近似的两条留先见的那条（fresh.com/1），撞已发过的丢（fresh.com/3）
    assert [c["url"] for c in kept] == ["https://fresh.com/1", "https://fresh.com/4"], kept
    reasons = [d[1] for d in drops]
    assert any("重复" in r or "出过" in r for r in reasons)
    assert any("屏蔽" in r for r in reasons)
    assert any("旧" in r or "天" in r for r in reasons)


def test_prefilter_caps_focus_share(tmp_path) -> None:
    """均衡：一个方向最多占粗筛结果的 40%（数量够多时），小方向不被挤光。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        candidates = [
            _candidate(f"https://f1.com/{i}", title=f"方向一的料第{i}号", focus=1, published=NOW - 100)
            for i in range(30)
        ] + [
            _candidate("https://f2.com/a", title="无线电通联小知识", focus=2, published=NOW - 100),
            _candidate("https://f2.com/b", title="云端成本控制心得", focus=2, published=NOW - 100),
            _candidate("https://f3.com/a", title="数据库索引漫谈记", focus=3, published=NOW - 100),
            _candidate("https://f3.com/b", title="相机传感器科普篇", focus=3, published=NOW - 100),
        ]
        kept, _drops, _stats = feeds._prefilter(GID, settings, candidates)
    n_f1 = sum(1 for c in kept if c.get("focus") == 1)
    n_f2 = sum(1 for c in kept if c.get("focus") == 2)
    n_f3 = sum(1 for c in kept if c.get("focus") == 3)
    assert len(kept) <= 24
    # 方向一 30 条候选也最多吃 40% 的名额（24×0.4 ≈ 10），小方向全留不被挤光
    assert n_f1 <= 10, (n_f1, n_f2, n_f3)
    assert n_f2 == 2 and n_f3 == 2, (n_f2, n_f3, n_f1)


# ----------------------------------------------------------------------
# 挑（pick）
# ----------------------------------------------------------------------


def test_pick_model_failure_falls_back(tmp_path) -> None:
    """挑的模型调炸了 → 前 10 条粗筛结果直接进核验，不报错。"""
    search = FakeBroadSearch(results=[
        {"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(12)
    ])
    verify = [_ok_report({"items": [_verify_item("https://s.com/0")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=search,
    )
    from CharTyr_MaiWork.maiwork.models import ModelError
    models.reply_queue = [_FOCUS_JSON, ModelError("模型炸了"), _scores_json(_score(0))]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    verifies = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-verify:")]
    assert verifies
    brief_all = "\n".join(v["brief"] for v in verifies)
    assert "https://s.com/0" in brief_all


# ----------------------------------------------------------------------
# 核验（verify）
# ----------------------------------------------------------------------


def test_verify_workers_concurrent_and_fail_isolated(tmp_path) -> None:
    """核验子 agent 并发跑（max ≥2）；一个炸了只丢它自己那份，别人的照收。"""
    search = FakeBroadSearch(results=[
        {"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(12)
    ])
    verify = [
        _ok_report(None, ok=False, error="炸了"),  # 第 1 组炸
        _ok_report({"items": [_verify_item("https://s.com/4")]}),  # 第 2 组回 1 条
    ]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=search,
    )
    picks = [{"i": i, "kind": "news", "hook": f"理由{i}"} for i in range(12)]
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": picks}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1  # 炸的那组不拖累好组：好组回来 1 条入库
    assert workers.max_parallel >= 2, workers.max_parallel
    verifies = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-verify:")]
    assert len(verifies) >= 2
    assert all(v["tools"] == ["fetch_page"] for v in verifies)
    rows = store.read().execute("SELECT * FROM news_items WHERE rejected=0").fetchall()
    assert len(rows) == 1
    assert rows[0]["src_provider"] != ""


def test_verify_output_order_follows_picks(tmp_path) -> None:
    """并发核验按轮循分组会把候选顺序打散；交回入库前按挑的顺序排回去，
    让「打分的 i ↔ 候选的 i」在老用例口径下稳定（顺序就是子 agent 看到的顺序）。"""
    picks = [
        (_candidate(f"https://o.com/{i}", title=f"顺序{i}"), "news", f"理由{i}")
        for i in range(6)
    ]
    r0 = _ok_report({"items": [_verify_item("https://o.com/3"), _verify_item("https://o.com/0")]})
    r1 = _ok_report({"items": [_verify_item("https://o.com/4"), _verify_item("https://o.com/1")]})
    r2 = _ok_report({"items": [_verify_item("https://o.com/5"), _verify_item("https://o.com/2")]})
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=[r0, r1, r2],
    )
    items, opened = _run(feeds._verify_batch(
        GID, picks, verify_mark="feeds-verify:order:1", deadline_ts=NOW + 60,
    ))
    assert [it["url"] for it in items] == [f"https://o.com/{i}" for i in range(6)]
    assert opened == 6


def test_verify_picks_without_hook_dropped(tmp_path) -> None:
    """模型挑的条目没有具体 hook（或编号越界）→ 丢掉不打开。"""
    search = FakeBroadSearch(results=[
        {"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(6)
    ])
    verify = [_ok_report({"items": [_verify_item("https://s.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=search,
    )
    picks = [
        {"i": 0, "kind": "news", "hook": "   "},  # 空 hook → 丢
        {"i": 1, "kind": "news", "hook": "真理由"},
        {"i": 99, "kind": "news", "hook": "越界编号也丢"},
    ]
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": picks}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    verifies = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-verify:")]
    brief_all = "\n".join(v["brief"] for v in verifies)
    assert "https://s.com/1" in brief_all
    assert "https://s.com/0" not in brief_all


# ----------------------------------------------------------------------
# 可追溯：src_query / src_provider 进库 + 管理员视图带它
# ----------------------------------------------------------------------


def test_items_carry_src_into_news_items(tmp_path) -> None:
    """核验回来到入库这一段：src_query/src_provider 写进 news_items；老库迁移也有这两列。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    cols = {r["name"] for r in store.read().execute("PRAGMA table_info(news_items)")}
    assert "src_query" in cols and "src_provider" in cols
    store.close()

    # 模拟线上：每条计划各自出的结果不同，保底补搜什么都搜不到
    # （否则多条计划/保底碰到同一条链接会把 src 抢去）
    class PlannedOnly:
        def __init__(self) -> None:
            self.calls: List[tuple] = []

        async def search(self, query, *, limit=8, days=None, site="", news=False):
            self.calls.append((query, limit, days, site, news))
            if str(query) == "FPGA 新动态 新进展":  # 方向 1 计划的第一条（见 _add_plans_to_focus_reply）
                return [{"title": "开源掌机周报", "url": "https://src.com/a", "snippet": "s",
                         "published": None, "provider": "keenable"}]
            return []

    verify = [_ok_report({"items": [_verify_item("https://src.com/a")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=PlannedOnly(),
    )
    # 定关注点回复换成「带搜索计划」的版本：方向 1 计划第一条 q="FPGA 新动态 新进展"
    models.reply_queue = [
        _add_plans_to_focus_reply(_FOCUS_JSON),
        # 方向列表里只有 1 条候选（粗筛留它的那条） → 挑第 0 条即那条
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得一开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    row = store.read().execute("SELECT src_query, src_provider FROM news_items WHERE rejected=0").fetchone()
    assert row is not None
    # 这条候选被每一搜都碰到：登记簿留先见的那条——方向 1 计划的第一条搜索词
    assert row["src_query"] == "FPGA 新动态 新进展"
    assert row["src_provider"] == "keenable"
    # 管理员视图带 src；群友视图不带（时间冻结在入库那一刻的 3 天窗口里读）
    with _TimePatch():
        view_admin = feeds.news_view(GID, days=3, admin=True)
        view_member = feeds.news_view(GID, days=3, admin=False)
    assert view_admin and view_admin[0]["items"], view_admin
    item = view_admin[0]["items"][0]
    assert item.get("src") == {"query": "FPGA 新动态 新进展", "provider": "keenable"}
    assert "src" not in view_member[0]["items"][0]


# ----------------------------------------------------------------------
# 漏斗统计
# ----------------------------------------------------------------------


def test_funnel_stats_written_and_readable(tmp_path) -> None:
    """批次统计带 funnel：各环节计数 + 每方向的计数，读的回来；老字段还在。"""
    search = FakeBroadSearch(
        results=[{"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(6)],
        broad=["main", "extra"],
        with_results={"extra": [{"title": f"E{i}", "url": f"https://e.com/{i}", "snippet": "s", "published": None} for i in range(3)]},
    )
    verify = [_ok_report({"items": [_verify_item("https://s.com/0")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, verify=verify, search=search,
    )
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得一开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    batch = store.read().execute("SELECT id FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    stats = feeds._batch_stats(int(batch["id"]))
    assert stats is not None
    funnel = stats.get("funnel")
    assert funnel is not None, stats
    assert funnel["discovered"] >= 3
    assert funnel["queries"] >= 3  # 计划（回退）的 3 次搜索计数
    assert funnel["prefiltered"] >= 1
    assert funnel["picked"] >= 1
    assert funnel["returned"] >= 1
    assert funnel["kept"] == 1
    assert "timings_s" in funnel and "discover" in funnel["timings_s"]
    assert "per_focus" in funnel and isinstance(funnel["per_focus"], list)
    assert "searches" in stats and "pages" in stats


def test_old_batch_stats_without_funnel_still_read(tmp_path) -> None:
    """老批次（两阶段恒生效之前跑的、统计里没有 funnel 的）照读：funnel 键不存在、不炸。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (GID, NOW, NOW),
        )
        store.kv_set(conn, f"feeds.batch_stats.{int(cur.lastrowid)}", {"searches": 2, "pages": 1, "kept": 1})
    batch = store.read().execute("SELECT id FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    stats = feeds._batch_stats(int(batch["id"]))
    assert stats is not None and "funnel" not in stats
    assert stats["kept"] == 1


# ----------------------------------------------------------------------
# 接口：feeds-two-phase 两个路由已删（2026-09-30 用户决定新找法恒生效，不再开关）
# ----------------------------------------------------------------------


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = _raw_config(tmp_path / "data")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield type("SimpleEnv", (), {"app": app, "client": client, "tmp_path": tmp_path})()
    finally:
        await client.close()
        await app.stop()


@pytest.mark.asyncio
async def test_two_phase_api_routes_gone_admin(env) -> None:
    """路由删掉后：管理员 GET/PUT 都是 404（不再是能拨的开关）。"""
    await env.client.post("/api/login", json={"password": PASSWORD})
    r = await env.client.get(f"/api/groups/{G1}/feeds-two-phase")
    assert r.status == 404, r.status
    r = await env.client.put(f"/api/groups/{G1}/feeds-two-phase", json={"on": True})
    assert r.status == 404, r.status


@pytest.mark.asyncio
async def test_two_phase_api_routes_gone_member_and_anon(env) -> None:
    """群友 / 匿名同样 404——任何身份都拨不了这个不存在的开关。"""
    token = env.app.token_of(G1)
    r = await env.client.put(
        f"/api/groups/{G1}/feeds-two-phase", json={"on": True},
        headers={"X-MW-Group": token},
    )
    assert r.status == 404, r.status
    r = await env.client.get(f"/api/groups/{G1}/feeds-two-phase")
    assert r.status == 404, r.status


# ----------------------------------------------------------------------
# 2026-09-30 线上第一批实测后的修正
# ----------------------------------------------------------------------


def test_prefilter_drops_homepages_and_listing_pages(tmp_path) -> None:
    """网站首页 / 栏目页 / 标签页不是一篇内容，挑了也打不开有用的东西（线上实测挑中了 nintendolife.com 首页）。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    candidates = [
        _candidate("http://nintendolife.com", title="Nintendo Life"),
        _candidate("https://example.com/", title="首页"),
        _candidate("https://example.com/news/", title="新闻栏目"),
        _candidate("https://example.com/tag/switch-2", title="标签页"),
        _candidate("https://example.com/category/games/", title="分类页"),
        _candidate("https://example.com/news/2026/09/switch-update", title="真正的一篇"),
    ]
    with _TimePatch():
        kept, drops, _ = feeds._prefilter(GID, settings, candidates)
    assert [c["url"] for c in kept] == ["https://example.com/news/2026/09/switch-update"]
    assert all("首页" in d[1] or "栏目" in d[1] for d in drops)


def test_pick_prompt_asks_for_source_quality(tmp_path) -> None:
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    models.reply_queue = [json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "官方公告，群里在等这个"}]}, ensure_ascii=False)]
    kept = [_candidate("https://www.nintendo.com/news/2026/update", title="官方更新说明")]
    _run(feeds._pick(GID, [{"query": "Switch 2 更新"}], kept))
    prompt = models.calls[-1][1][0]["content"]
    for word in ("SEO", "采购", "聚合", "首页", "nintendo.com/news/2026/update"):
        assert word in prompt, word


def test_funnel_counts_focus_and_providers_without_floor_search(tmp_path) -> None:
    """没有可用的 search 对象（保底补搜跳过）时，漏斗的「各个关注点」「各家搜索服务」也要从撒网结果里数出来。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    feeds._search = None
    cands = [_candidate(f"https://a.com/{i}", focus=1, query="q1", provider="keenable") for i in range(3)]
    cands += [_candidate(f"https://b.com/{i}", focus=2, query="q2", provider="exa") for i in range(2)]
    funnel: dict = {"queries": 0, "providers": {}, "per_focus": []}
    added = _run(feeds._floor_searches(GID, [{"query": "方向一"}, {"query": "方向二"}], cands, funnel))
    assert added == []
    assert funnel["providers"] == {"keenable": 3, "exa": 2}
    assert [(x["query"], x["queries"], x["cands"]) for x in funnel["per_focus"]] == [("方向一", 1, 3), ("方向二", 1, 2)]


@pytest.mark.asyncio
async def test_app_feeds_gets_search(tmp_path) -> None:
    """线上实测：app 建 Feeds 时没把 Search 传进去，保底补搜和「搜索没配好就跳过」都没生效。"""
    from test_app import _app

    app = _app(tmp_path)
    await app.start()
    try:
        assert app.search is not None
        assert app.feeds is not None and app.feeds._search is app.search
    finally:
        await app.stop()


def test_focus_prompt_plans_searches_with_all_guidance(tmp_path) -> None:
    """2026-10-01 起撒网是代码按计划搜：搜索的「怎么搜」全套规矩都进定关注点（feeds.focus）
    的提示词（原撒网 brief 的口径搬走）；提示词不带 fetch_page / 工具调用那套。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        _run(feeds._plan_focus(GID, settings))
    prompt = models.calls[0][1][0]["content"]
    for word in ("一手来源", "社区", "反面", "后续进展", "site"):
        assert word in prompt, word
    assert "同义" in prompt  # 禁止同义改写刷搜索
    assert "2–6" in prompt  # 搜索词要短的口径
    assert "7 天" in prompt and "180" in prompt  # 时间由程序管
    assert "search" in prompt and "searches" in prompt  # 要让模型给搜索计划
    assert "news" in prompt and "kind" in prompt
    assert "fetch_page" not in prompt and "web_search" not in prompt  # 工具话术不落在这
    # guides 开着（默认）：kind 允许 guide
    assert "guide" in prompt


def test_focus_prompt_guides_off_says_news_only(tmp_path) -> None:
    """[feeds] guides=false：定关注点提示词写明这轮只找资讯（别给 guide 计划/kind）。"""
    cfg = {"feeds": {"guides": False}}
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    # 重配 settings：_make_feeds 的 _settings 只接受 cfg 参数，这里直接改 feeds 的设置闭包
    from test_feeds_quality import _settings as _mk_settings

    settings2 = _mk_settings(cfg)
    feeds._get_settings = lambda: settings2
    focus_json = json.dumps({
        "focus": [
            {"query": "A", "why": "", "source": "recent",
             "searches": [{"q": "a", "site": "", "news": True, "kind": "guide"},
                          {"q": "b", "site": "", "news": True, "kind": "news"}]},
            {"query": "B", "why": "", "source": "long"},
            {"query": "C", "why": "", "source": "explore"},
        ]
    }, ensure_ascii=False)
    models.reply_queue = [focus_json]
    with _TimePatch():
        focus = _run(feeds._plan_focus(GID, settings2))
    prompt = models.calls[0][1][0]["content"]
    assert "只找资讯" in prompt
    # guides=false 时计划的 kind=guide 一律按 news 归一
    assert focus[0]["searches"][0]["kind"] == "news"


def test_focus_prompt_saturated_topics_and_blocked_listed(tmp_path) -> None:
    """话题饱和提示 + 屏蔽名单（手动+自动）都进定关注点提示词。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with store.tx() as conn:
        store.kv_set(conn, f"feeds.blocked.{GID}", ["spam-news.example"])
    with _TimePatch():
        _run(feeds._plan_focus(GID, settings))
    prompt = models.calls[0][1][0]["content"]
    assert "spam-news.example" in prompt
    # 直接发够 2 条的饱和话题会列出来（库里没数据时这块没有，只验手动屏蔽名单）
