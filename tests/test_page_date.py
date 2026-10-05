"""代码从网页读发布日期（docs/10-资讯流水线改进计划.md §九 第一步 2，2026-10-05）。

线上现象：「文章没有发布时间」只拒了 222 篇好文里的 6 篇（含机核长文 2 篇）——
好文因为子 agent 没在页面上看到日期，被 `_news_freshness_reject` 的
「文章没有发布时间，宁缺毋滥不收」硬拒。规则不放宽，改成代码在打开网页那一步
（tools_builtin.fetch_page 拿到原始 HTML）自己读日期：

- meta：article:published_time / og:published_time / datePublished / pubdate / date / DC.date；
- JSON-LD：datePublished / dateCreated；
- `<time datetime="…">`（含 pubdate / itemprop=datePublished）。

读到就随 fetch_page 的摘要写进 tool_calls（`（页面发布日期：YYYY-MM-DD）`），
下游（feeds 核验 / news_recheck 补打开）在模型没给日期时用它补上 `published_ts`。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork import page_date
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin
from CharTyr_MaiWork.maiwork.workers import WorkerReport

from fakes import FakeProfiles

from test_feeds_quality import (
    GID,
    NOW,
    _TimePatch,
    _accepted,
    _cand,
    _make_feeds,
    _rej_reason,
    _rows,
    _run,
    _score,
    _scores_json,
)

_DATE = "2026-09-28"


def _run_async(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 单元：从 HTML 里读日期
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "head,want",
    [
        (f'<meta property="article:published_time" content="{_DATE}T10:00:00+08:00">', _DATE),
        (f'<meta property="og:published_time" content="{_DATE}T02:00:00Z">', _DATE),
        (f'<meta name="pubdate" content="{_DATE}">', _DATE),
        (f'<meta name="date" content="{_DATE}T10:00:00+08:00">', _DATE),
        (f'<meta itemprop="datePublished" content="{_DATE}">', _DATE),
        (f'<meta name="DC.date.issued" content="{_DATE}">', _DATE),
        (f'<meta name="parsely-pub-date" content="{_DATE}T10:00:00+08:00">', _DATE),
        ('<meta name="pubdate" content="Mon, 28 Sep 2026 10:00:00 GMT">', _DATE),
    ],
)
def test_meta_tags(head: str, want: str) -> None:
    html = f"<html><head>{head}</head><body>正文</body></html>"
    assert page_date.published_from_html(html) == want


@pytest.mark.parametrize(
    "script",
    [
        '{"@type": "NewsArticle", "datePublished": "2026-09-28T10:00:00+08:00"}',
        '{"@context": "https://schema.org", "dateCreated": "2026-09-28T02:00:00Z"}',
        '[{"@type": "BlogPosting", "datePublished": "2026-09-28"}]',
    ],
)
def test_json_ld(script: str) -> None:
    html = (
        "<html><head><script type=\"application/ld+json\">" + script + "</script></head>"
        "<body>正文</body></html>"
    )
    assert page_date.published_from_html(html) == _DATE


@pytest.mark.parametrize(
    "body",
    [
        f'<article><time datetime="{_DATE}T10:00:00+08:00">9 月 28 日</time><p>正文</p></article>',
        f'<article><time pubdate datetime="{_DATE}">9 月 28 日</time></article>',
        f'<article><time itemprop="datePublished" datetime="{_DATE}"></time></article>',
        f'<time datetime="{_DATE}T10:00:00+08:00"></time>',  # 没有 <article> 也一样认
    ],
)
def test_time_tag(body: str) -> None:
    assert page_date.published_from_html(f"<html><body>{body}</body></html>") == _DATE


def test_meta_wins_over_time_tag() -> None:
    html = (
        '<html><head><meta property="article:published_time" content="2026-09-28T10:00:00+08:00"></head>'
        '<body><time datetime="2026-01-01"></time></body></html>'
    )
    assert page_date.published_from_html(html) == _DATE


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>没有日期</body></html>",
        '<html><head><meta name="pubdate" content="刚刚"></head><body>x</body></html>',
        '<html><head><script type="application/ld+json">{不是 JSON</script></head><body>x</body></html>',
        '<html><body><time datetime="not-a-date"></time></body></html>',
    ],
)
def test_no_date_returns_empty(html: str) -> None:
    assert page_date.published_from_html(html) == ""


@pytest.mark.parametrize(
    "raw,want",
    [
        ("2026-09-28T10:00:00+08:00", "2026-09-28"),
        ("2026-09-28T02:00:00Z", "2026-09-28"),
        ("2026-09-28", "2026-09-28"),
        ("2026/9/8", "2026-09-08"),
        ("Mon, 28 Sep 2026 10:00:00 GMT", "2026-09-28"),
        ("", ""),
        ("刚刚更新", ""),
    ],
)
def test_normalize_date(raw: str, want: str) -> None:
    assert page_date.normalize_date(raw) == want


def test_note_and_summary_roundtrip() -> None:
    assert page_date.note(_DATE) == "（页面发布日期：2026-09-28）"
    assert page_date.date_from_summary(f"取到正文 500 字{page_date.note(_DATE)}") == _DATE
    assert page_date.date_from_summary("取到正文 500 字") == ""


# ----------------------------------------------------------------------
# 真 fetch_page：读 HTML → 写进 tool_calls 摘要 → 下游能取出来
# ----------------------------------------------------------------------


class _FakeSearch:
    async def search(self, query, **kw):  # pragma: no cover - 这些用例不搜
        return []

    async def extract(self, url):  # pragma: no cover - 不走抓正文工具
        raise RuntimeError("不该走抓正文工具")


def _tools(store: Store, transport) -> Tools:
    settings, _ = load_settings({"groups": {"serve": [{"group": "qq:900000001"}]}})
    tools = Tools(store)
    register_builtin(
        tools,
        search=_FakeSearch(),
        profiles=FakeProfiles(),
        http_transport=transport,
        get_settings=lambda: settings,
        resolver=lambda host: ["93.184.216.34"],
    )
    return tools


def _ctx(task_id: str = "T-1") -> ToolContext:
    return ToolContext(group_id="900000001", task_id=task_id, actor="子 agent #1", role="worker")


def test_fetch_page_reads_date_from_html(tmp_path) -> None:
    html = (
        "<html><head><title>机核长文</title>"
        '<meta property="article:published_time" content="2026-09-28T10:00:00+08:00">'
        "</head><body><p>正文内容，够长。</p></body></html>"
    )
    store = Store(tmp_path / "t.db")
    store.migrate()
    try:
        tools = _tools(store, httpx.MockTransport(
            lambda req: httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ))
        r = _run_async(tools.call("fetch_page", {"url": "http://example.com/post"}, _ctx()))
        assert r.ok
        # 子 agent 直接看得到（提示词里要它填 published）
        assert _DATE in r.output
        assert (r.data or {}).get("published") == _DATE
        # 落进 tool_calls 的摘要（下游按 task_id 取）
        rows = store.read().execute(
            "SELECT tool, input, output, ok FROM tool_calls WHERE task_id='T-1'").fetchall()
        assert page_date.note(_DATE) in str(rows[0]["output"])
        dates = page_date.dates_from_rows(rows)
        assert dates == {"example.com/post": _DATE}
    finally:
        store.close()


def test_fetch_page_without_date_adds_nothing(tmp_path) -> None:
    html = "<html><head><title>无日期</title></head><body><p>正文</p></body></html>"
    store = Store(tmp_path / "t.db")
    store.migrate()
    try:
        tools = _tools(store, httpx.MockTransport(
            lambda req: httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ))
        r = _run_async(tools.call("fetch_page", {"url": "http://example.com/post"}, _ctx()))
        assert r.ok
        assert "页面发布日期" not in r.output
        rows = store.read().execute(
            "SELECT tool, input, output, ok FROM tool_calls WHERE task_id='T-1'").fetchall()
        assert page_date.dates_from_rows(rows) == {}
    finally:
        store.close()


def test_dates_from_rows_keeps_final_url_and_extract_rows() -> None:
    rows = [
        {"tool": "fetch_page", "ok": 1, "input": "https://short.example/x",
         "output": f"取到正文 500 字（Jina）（最终地址：https://news.example/final）{page_date.note(_DATE)}"},
        {"tool": "mcp_keenable_fetch_page_content", "ok": 1, "input": json.dumps({"url": "https://mcp.example/a"}),
         "output": f"Title: t\nURL: https://mcp.example/a\nPublished Time: {_DATE}T10:00:00Z\n\n正文"},
        {"tool": "web_search", "ok": 1, "input": "搜", "output": f"8 条{page_date.note(_DATE)}"},
        {"tool": "fetch_page", "ok": 0, "input": "https://bad.example/x", "output": page_date.note(_DATE)},
    ]
    dates = page_date.dates_from_rows(rows)
    assert dates["news.example/final"] == _DATE
    assert dates["short.example/x"] == _DATE
    assert dates["mcp.example/a"] == _DATE
    assert "bad.example/x" not in dates
    assert "mcp.example/a" in dates


# ----------------------------------------------------------------------
# 接线：核验子 agent 没给日期，代码读到的补上 → 好文不再被「没有发布时间」拒
# ----------------------------------------------------------------------


def _log_fetch(store: Store, task_id: str, url: str, *, date: str = "") -> None:
    out = "取到正文 500 字（直接打开）" + (page_date.note(date) if date else "")
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, ?, ?, '子 agent #1', 'fetch_page', ?, ?, 1)",
            (NOW, GID, task_id, url, out),
        )


class _CodeDateWorkers:
    """核验子 agent 的替身：按 brief 交回预置候选，并把这轮的 fetch_page 记录写成
    「代码读到的发布日期」那一行（真机上由 fetch_page 的摘要写）。"""

    def __init__(self, store_ref: list, items: list[dict], code_dates: dict[str, str]) -> None:
        self.store_ref = store_ref
        self.items = list(items)
        self.code_dates = dict(code_dates)
        self.calls: list[dict] = []
        self.report = WorkerReport(ok=True, summary="找好了", data={"items": self.items}, evidence=[], steps=3)

    async def run(self, brief: str, **kwargs):
        tid = str(kwargs.get("task_id") or "")
        self.calls.append({"brief": brief, "task_id": tid, **kwargs})
        assert tid.startswith("feeds-verify:"), f"这轮只该派核验：{tid}"
        store = self.store_ref[0]
        for url, date in self.code_dates.items():
            _log_fetch(store, tid, url, date=date)
        kept = [
            dict(it) for it in self.items
            if str(it.get("url") or "") and str(it.get("url") or "") in brief
        ]
        return WorkerReport(ok=True, summary="核验好", data={"items": kept}, evidence=[], steps=2)


def _guide_items(url: str = "https://gcores.com/deep") -> list[dict]:
    return [{
        "title": "一篇没有发布日期的长文",
        "url": url,
        "summary": "摘要：两三句话讲清楚这篇长文。",
        "kind": "guide",
        "published": None,
        "fetched": True,
        "quote": "原文里确实写着这件事。",
        "paywall": False,
    }]


def _setup_guide(tmp_path, *, code_date: str) -> tuple:
    ref: list = [None]
    items = _guide_items()
    workers = _CodeDateWorkers(ref, items, {"https://gcores.com/deep": code_date})
    store, settings, feeds, models, _w, _topics, _profiles = _make_feeds(
        tmp_path, items=items, scores=_scores_json(_score(0, topic="长文")), workers=workers
    )
    ref[0] = store
    return store, feeds, workers, models


def test_guide_without_model_date_rejected_without_code_date(tmp_path) -> None:
    """先把老行为钉住：代码没读到日期时，好文照旧被「文章没有发布时间」拒。"""
    store, feeds, workers, _models = _setup_guide(tmp_path, code_date="")
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 0
    assert "没有发布时间" in (_rej_reason(_rows(store), "gcores.com") or "")
    store.close()


def test_guide_saved_by_code_read_date(tmp_path) -> None:
    """核验没给日期、代码从网页读到 → 补上 published_ts，好文不再被误杀。"""
    from CharTyr_MaiWork.maiwork import clock

    code_date = clock.bj(NOW - 3 * 86400.0).strftime("%Y-%m-%d")
    store, feeds, workers, models = _setup_guide(tmp_path, code_date=code_date)
    with _TimePatch():
        assert _run(feeds.prepare_news(GID)) == 1
    row = _accepted(_rows(store))[0]
    assert row["title"] == "一篇没有发布日期的长文"
    assert row["published_ts"] is not None, "代码读到的日期要补进 published_ts"
    assert abs(float(row["published_ts"]) - (NOW - 3 * 86400.0)) < 1.5 * 86400.0
    store.close()
