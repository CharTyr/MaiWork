"""两阶段找资讯（「广撒网再挑着打开」）测试：开关、撒网、保底、粗筛、挑着打开、验收、漏斗统计。

开关：kv key feeds.two_phase = 用新路的群号列表；Feeds.two_phase_on(gid) / set_two_phase(gid, on)；
管理员接口 GET/PUT /api/groups/{gid}/feeds-two-phase。

开关开着时 prepare_news 的第 ② 步换成：
1. 撒网（子 agent，只用 web_search，task_id feeds-discover: 开头）——候选从**程序侧的
   撒网登记簿**（discovery.py：web_search 工具 handler 记进去的搜索结果）拿，不信子 agent 交回；
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
from CharTyr_MaiWork.maiwork.feeds import Feeds, _normalize_url
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
    """按 task_id 分派的假 workers：撒网 / 每个核验子 agent 各回各的，全程记录调用。"""

    def __init__(self, discover: Any = None, verify: list | None = None, others: Any = None) -> None:
        self.discover = discover
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

        if task_id.startswith("feeds-discover:"):
            rep = self.discover
            if isinstance(rep, BaseException):
                raise rep
            return rep
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
    """给 web_search 工具用的假搜索：结果带 provider 字段（search.py 的真实口径）。"""

    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.calls: List[tuple] = []

    async def search(self, query, *, limit=8, days=None, site="", news=False):
        self.calls.append((query, limit, days))
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


def _ready_two_phase_feeds(tmp_path, *, discover=None, verify=None, search=None, models=None):
    """开关打开 + 假 workers（按 task_id 分派）；返回常用几个对象。"""
    workers = SeqWorkers(discover=discover, verify=verify)
    kw: Dict[str, Any] = {"workers": workers}
    if models is not None:
        kw["models"] = models
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, **kw)
    if search is None:
        search = FakeBroadSearch()
    feeds._search = search
    feeds.set_two_phase(GID, True)
    return store, settings, feeds, models, workers, topics


# ----------------------------------------------------------------------
# 开关本身
# ----------------------------------------------------------------------


def test_two_phase_switch_default_off(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    assert feeds.two_phase_on(GID) is False
    feeds.set_two_phase(GID, True)
    assert feeds.two_phase_on(GID) is True
    feeds.set_two_phase(GID, False)
    assert feeds.two_phase_on(GID) is False
    # 关掉后不留痕迹：另起一个实例读同一份库也是关
    feeds2 = Feeds(store, models, workers, FakeProfiles(), topics, lambda: settings)
    assert feeds2.two_phase_on(GID) is False


def test_switch_off_uses_classic_collect_worker(tmp_path) -> None:
    """开关关着 = 老路不变：一个子 agent，web_search + fetch_page 一起，task_id feeds-collect:。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    collects = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-collect:")]
    assert len(collects) == 1, workers.calls
    assert sorted(collects[0]["tools"]) == ["fetch_page", "web_search"]
    assert not any(str(c.get("task_id") or "").startswith("feeds-discover:") for c in workers.calls)


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
async def test_web_search_tool_records_into_registry(tmp_path) -> None:
    """真实 web_search 工具 handler：task_id 有开着的撒网 run → 结果顺手记进登记簿；
    另外接受 focus 参数（整数，撒网之外的上下文里忽略）。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
    tools = Tools(store)
    search = ToolSearch([
        {"title": "甲", "url": "https://a.com/1", "snippet": "s", "published": None, "provider": "keenable"},
    ])
    register_builtin(tools, search=search, profiles=FakeProfiles(), get_settings=lambda: settings)
    tid = "feeds-discover:tool:1:1"
    discovery.open_run(tid)
    try:
        r = await tools.call("web_search", {"query": "测试词", "focus": 3}, _ctx(task_id=tid))
        assert r.ok
    finally:
        out = discovery.close_run(tid)
    assert len(out) == 1
    assert out[0]["query"] == "测试词" and out[0]["focus"] == 3 and out[0]["provider"] == "keenable"
    # focus 参数进了工具声明（模型看得见）
    tool = tools._tools["web_search"]
    assert "focus" in (tool.parameters.get("properties") or {})
    # 撒网之外的 task_id 传 focus 也不炸、不记
    r2 = await tools.call("web_search", {"query": "别的", "focus": "不是数字"}, _ctx(task_id="T-9"))
    assert r2.ok


# ----------------------------------------------------------------------
# 撒网（discover）+ 保底（floor）
# ----------------------------------------------------------------------


def test_discover_worker_searches_only(tmp_path) -> None:
    """撒网子 agent：工具只有 web_search；brief 带编号关注点 / 「只搜不开」/ focus=<编号>。"""
    discover = _ok_report({"note": "撒好了"})
    # 登记簿里零候选 → 保底会补；假搜索给 2 条，让流程能开到核验
    search = FakeBroadSearch(results=[
        {"title": "A", "url": "https://a.com/1", "snippet": "s", "published": None},
        {"title": "B", "url": "https://b.com/2", "snippet": "s", "published": None},
    ])
    verify = [_ok_report({"items": [_verify_item("https://a.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
    )
    # 模型队列：定关注点 → 挑（就挑第 0 条）→ 打分
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "和群画像直接对得上"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    discovers = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-discover:")]
    assert len(discovers) == 1
    assert discovers[0]["tools"] == ["web_search"]
    assert str(discovers[0]["task_id"]).startswith("feeds-discover:")
    assert discovers[0]["output_schema"] == {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
    }
    brief = discovers[0]["brief"]
    assert "1." in brief and "FPGA 新动态" in brief  # 编号关注点
    assert "不要打开" in brief or "别打开" in brief
    assert "focus=" in brief
    # 核验子 agent 只用 fetch_page
    verifies = [c for c in workers.calls if str(c.get("task_id") or "").startswith("feeds-verify:")]
    assert verifies, workers.calls
    assert all(v["tools"] == ["fetch_page"] for v in verifies)


def test_per_focus_floor_triggers_code_searches(tmp_path) -> None:
    """某个方向没被撒网搜到（0 次问 / 0 条）→ 代码直接补搜：主家一次 + 撒网多一家各一次。"""
    discover = _ok_report({"note": "ok"})
    search = FakeBroadSearch(
        results=[{"title": "X", "url": "https://x.com/1", "snippet": "s", "published": None}],
        broad=["main", "extra"],
        with_results={"extra": [{"title": "Y", "url": "https://y.com/2", "snippet": "s", "published": None}]},
    )
    verify = [_ok_report({"items": [_verify_item("https://x.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
    )
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得打开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    # 三个关注点每个都饿着：每个主家补一次（days=7；配自检探测那条不算）
    real_calls = [c for c in search.calls if c[0] != "__配置自检__"]
    assert len(real_calls) >= 3, search.calls
    assert all(call[2] == 7 for call in real_calls)
    # 撒网多一家：每个饿着的方向也用 search_with 各补一次
    assert len(search.with_calls) >= 3
    assert all(name == "extra" for name, *_ in search.with_calls)


def test_floor_errors_ignored(tmp_path) -> None:
    """保底补搜全部出错也不拖累这轮：还靠撒网登记簿那几条往下走。"""

    class BadSearch:
        async def search(self, query, **kw):
            if str(query).startswith("__配置自检__"):
                return []  # 自检探测放行（让它走进保底，保底里才全挂）
            raise RuntimeError("搜索全挂")

        def broad_providers(self):
            return []

    discover = _ok_report({"note": "ok"})
    verify = [_ok_report({"items": [_verify_item("https://good.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=BadSearch(),
    )
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得打开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]

    orig_run = workers.run

    async def run_and_record(brief, **kwargs):
        tid = str(kwargs.get("task_id") or "")
        if tid.startswith("feeds-discover:"):
            discovery.record(tid, query="q", focus=1, provider="main", results=[
                {"title": "好", "url": "https://good.com/1", "snippet": "s", "published": None},
            ])
        return await orig_run(brief, **kwargs)

    workers.run = run_and_record
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
            store.kv_set(conn, "feeds.blocked_domains", ["bad.example"])
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
    discover = _ok_report({"note": "ok"})
    search = FakeBroadSearch(results=[
        {"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(12)
    ])
    verify = [_ok_report({"items": [_verify_item("https://s.com/0")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
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
    discover = _ok_report({"note": "ok"})
    search = FakeBroadSearch(results=[
        {"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(12)
    ])
    verify = [
        _ok_report(None, ok=False, error="炸了"),  # 第 1 组炸
        _ok_report({"items": [_verify_item("https://s.com/4")]}),  # 第 2 组回 1 条
    ]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
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


def test_verify_picks_without_hook_dropped(tmp_path) -> None:
    """模型挑的条目没有具体 hook（或编号越界）→ 丢掉不打开。"""
    discover = _ok_report({"note": "ok"})
    search = FakeBroadSearch(results=[
        {"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(6)
    ])
    verify = [_ok_report({"items": [_verify_item("https://s.com/1")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
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

    discover = _ok_report({"note": "ok"})
    search = FakeBroadSearch()  # 保底给空：候选只从登记簿来
    verify = [_ok_report({"items": [_verify_item("https://src.com/a")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
    )
    models.reply_queue = [
        _FOCUS_JSON,
        json.dumps({"picks": [{"i": 0, "kind": "news", "hook": "值得一开"}]}, ensure_ascii=False),
        _scores_json(_score(0)),
    ]
    orig_run = workers.run

    async def run_and_record(brief, **kwargs):
        tid = str(kwargs.get("task_id") or "")
        if tid.startswith("feeds-discover:"):
            discovery.record(tid, query="开源掌机新进展", focus=1, provider="keenable", results=[
                {"title": "开源掌机周报", "url": "https://src.com/a", "snippet": "s", "published": None},
            ])
        return await orig_run(brief, **kwargs)

    workers.run = run_and_record
    with _TimePatch():
        kept = _run(feeds.prepare_news(GID))
    assert kept == 1
    row = store.read().execute("SELECT src_query, src_provider FROM news_items WHERE rejected=0").fetchone()
    assert row is not None
    assert row["src_query"] == "开源掌机新进展"
    assert row["src_provider"] == "keenable"
    # 管理员视图带 src；群友视图不带（时间冻结在入库那一刻的 3 天窗口里读）
    with _TimePatch():
        view_admin = feeds.news_view(GID, days=3, admin=True)
        view_member = feeds.news_view(GID, days=3, admin=False)
    assert view_admin and view_admin[0]["items"], view_admin
    item = view_admin[0]["items"][0]
    assert item.get("src") == {"query": "开源掌机新进展", "provider": "keenable"}
    assert "src" not in view_member[0]["items"][0]


# ----------------------------------------------------------------------
# 漏斗统计
# ----------------------------------------------------------------------


def test_funnel_stats_written_and_readable(tmp_path) -> None:
    """批次统计带 funnel：各环节计数 + 每方向的计数，读的回来；老字段还在。"""
    discover = _ok_report({"note": "ok"})
    search = FakeBroadSearch(
        results=[{"title": f"S{i}", "url": f"https://s.com/{i}", "snippet": "s", "published": None} for i in range(6)],
        broad=["main", "extra"],
        with_results={"extra": [{"title": f"E{i}", "url": f"https://e.com/{i}", "snippet": "s", "published": None} for i in range(3)]},
    )
    verify = [_ok_report({"items": [_verify_item("https://s.com/0")]})]
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(
        tmp_path, discover=discover, verify=verify, search=search,
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
    assert funnel["discovered"] >= 6
    assert funnel["prefiltered"] >= 1
    assert funnel["picked"] >= 1
    assert funnel["returned"] >= 1
    assert funnel["kept"] == 1
    assert "timings_s" in funnel and "discover" in funnel["timings_s"]
    assert "per_focus" in funnel and isinstance(funnel["per_focus"], list)
    assert "searches" in stats and "pages" in stats


def test_old_batch_stats_without_funnel_still_read(tmp_path) -> None:
    """老批次（没有 funnel 的）照读：funnel 键不存在、不炸。"""
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    batch = store.read().execute("SELECT id FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    stats = feeds._batch_stats(int(batch["id"]))
    assert stats is not None and "funnel" not in stats
    assert stats["kept"] == 1


# ----------------------------------------------------------------------
# 接口：GET/PUT /api/groups/{gid}/feeds-two-phase（管理员）
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
async def test_two_phase_api_admin_rw(env) -> None:
    await env.client.post("/api/login", json={"password": PASSWORD})
    r = await env.client.get(f"/api/groups/{G1}/feeds-two-phase")
    assert r.status == 200
    assert (await r.json()) == {"on": False}
    r = await env.client.put(f"/api/groups/{G1}/feeds-two-phase", json={"on": True})
    assert r.status == 200
    assert (await r.json()) == {"on": True}
    r = await env.client.get(f"/api/groups/{G1}/feeds-two-phase")
    assert (await r.json()) == {"on": True}
    r = await env.client.put(f"/api/groups/{G1}/feeds-two-phase", json={"on": False})
    assert (await r.json()) == {"on": False}


@pytest.mark.asyncio
async def test_two_phase_api_member_403_anon_401(env) -> None:
    token = env.app.token_of(G1)
    r = await env.client.put(
        f"/api/groups/{G1}/feeds-two-phase", json={"on": True},
        headers={"X-MW-Group": token},
    )
    assert r.status == 403
    r = await env.client.put(f"/api/groups/{G1}/feeds-two-phase", json={"on": True})
    assert r.status == 401
    # 群友 GET 也读不到（开关是管理员的事）
    r = await env.client.get(f"/api/groups/{G1}/feeds-two-phase", headers={"X-MW-Group": token})
    assert r.status == 403


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


def test_discover_brief_says_call_web_search_directly(tmp_path) -> None:
    """线上实测：撒网子 agent 开头 4 次调了不存在的「invoke」工具。brief 里写明直接调 web_search。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    brief = feeds._discover_brief(GID, [{"query": "方向一"}], settings)
    assert "直接调用 web_search" in brief
