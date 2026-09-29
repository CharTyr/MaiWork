"""资讯图解（news_viz.py）：没配图、数据多的资讯，子 agent 写一张只用 HTML+CSS 的小图，
程序核对：数字都来自原文、不引外部资源、没有脚本；网页里沙箱隔离显示。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import news_viz as nv
from CharTyr_MaiWork.maiwork.store import Store

G = "900000001"
NOW = 1_800_000_000.0

SOURCE = (
    "GPT-6 Sol 输入 $2 / 输出 $10 每百万 token，比 5.6 降价 50%。Terminal-Bench 43%（原 37%），"
    "SWE-Atlas-QnA 58%（原 54%）。Luna 输入 $0.10，SWE-Atlas-QnA 44%（原 49%）。发布于 2026 年 9 月 22 日。"
)
GOOD = """<style>.bar{height:8px;background:var(--accent)}</style>
<div class="card"><h3>GPT-6 Sol ☀️</h3>
<input type="radio" name="t" id="a" checked><label for="a">Terminal-Bench</label>
<p>43%（原 37%）<b>+6</b></p><p title="SWE-Atlas-QnA 58%">58%</p>
<p>输入 / 输出 $2 / $10</p><p>价格 −50%</p><p>9 月 22 日</p>
<div class="bar" style="width:43%"></div></div>"""


# ----------------------------------------------------------------------
# 核对（纯函数）
# ----------------------------------------------------------------------


def test_check_accepts_numbers_from_source_and_derived() -> None:
    ok, why = nv.check_html(GOOD, SOURCE)
    assert ok, why


def test_check_rejects_made_up_number() -> None:
    ok, why = nv.check_html(GOOD.replace("58%", "61%"), SOURCE)
    assert not ok and "61" in why


def test_signed_derived_numbers_only_with_sign() -> None:
    # 58 - 37 = 21（差）；(43 - 37) / 37 ≈ 16.2%（变化百分比）
    assert nv.check_html("<p>领先 +21 分</p><p>涨了 +16%</p>", SOURCE)[0]
    ok, why = nv.check_html("<p>领先 21 分</p>", SOURCE)
    assert not ok and "21" in why
    ok, why = nv.check_html("<p>涨了 16%</p>", SOURCE)
    assert not ok and "16" in why
    # 带号但对不上
    ok, why = nv.check_html("<p>+29%</p>", SOURCE)
    assert not ok and "29" in why


def test_check_rejects_number_in_data_attribute() -> None:
    ok, why = nv.check_html('<div data-value="77">x</div>', SOURCE)
    assert not ok and "77" in why


@pytest.mark.parametrize("bad", [
    "<script>alert(1)</script>",
    '<div onclick="x()">a</div>',
    '<img src="https://evil.example/p.png">',
    '<div style="background:url(//evil.example/a.png)">a</div>',
    "<style>@import 'x.css';</style>",
    '<iframe srcdoc="x"></iframe>',
    '<a href="https://example.com">原文</a>',
    '<link rel="stylesheet" href="a.css">',
    '<meta http-equiv="refresh" content="0">',
    '<form action="/x"></form>',
])
def test_check_rejects_scripts_and_external(bad) -> None:
    ok, why = nv.check_html("<p>43%</p>" + bad, SOURCE)
    assert not ok, bad


def test_check_rejects_too_big_and_empty() -> None:
    assert not nv.check_html("", SOURCE)[0]
    assert not nv.check_html("<p>43%</p>" + "<i></i>" * 8000, SOURCE)[0]


def test_wrap_has_csp_and_only_our_script() -> None:
    doc = nv.wrap(GOOD)
    assert "Content-Security-Policy" in doc
    assert "default-src 'none'" in doc
    assert "script-src 'sha256-" in doc
    assert doc.count("<script") == 1
    assert GOOD in doc


# ----------------------------------------------------------------------
# 流程
# ----------------------------------------------------------------------


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "v.db")
    s.migrate()
    return s


def _item(s, *, title="GPT-6 降价", image_url="", created=NOW - 3600, target="", gid=G) -> int:
    with s.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)", (gid, created, created))
        bid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key, score, created,"
            " kind, rejected, target_user_id, image_url, body) VALUES (?, ?, ?, '摘要', ?, 'a.com/x', 4.0, ?, 'news', 0, ?, ?, '正文')",
            (bid, gid, title, json.dumps([{"url": "https://a.com/x", "site": "a.com", "title": title}]),
             created, target, image_url))
        return int(cur.lastrowid)


class FakeModels:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def settings(self):
        return SimpleNamespace(ready=lambda: True)

    async def chat(self, role, messages, **kw):
        self.calls.append((role, messages, kw))
        return SimpleNamespace(text=json.dumps(self.reply))


class FakeWorkers:
    def __init__(self, html, ok=True):
        self.html = html
        self.ok = ok
        self.briefs = []

    async def run(self, brief, **kw):
        self.briefs.append((brief, kw))
        return SimpleNamespace(ok=self.ok, data={"html": self.html}, error="", summary="")


class FakeTools:
    def __init__(self, text=SOURCE, ok=True):
        self.text = text
        self.ok = ok
        self.urls = []

    async def call(self, name, args, ctx):
        assert name == "fetch_page"
        self.urls.append(args["url"])
        return SimpleNamespace(ok=self.ok, output=self.text, error="" if self.ok else "打不开")


def _viz(s, *, pick=0, html=GOOD, per_day=3, tools=None, workers=None):
    settings = SimpleNamespace(feeds=SimpleNamespace(viz_per_day=per_day), is_served=lambda g: True)
    models = FakeModels({"pick": pick, "why": "有对比数字"})
    workers = workers or FakeWorkers(html)
    tools = tools or FakeTools()
    return nv.NewsViz(s, models, workers, tools, lambda: settings), models, workers, tools


def test_happy_path_makes_viz_and_view_flag(tmp_path) -> None:
    s = _store(tmp_path)
    a = _item(s, title="有数字的")
    b = _item(s, title="另一条")
    v, models, workers, tools = _viz(s, pick=0)
    assert v.has_work(G, NOW)
    asyncio.run(v.run(G, now=NOW))
    assert tools.urls == ["https://a.com/x"]
    brief, kw = workers.briefs[0]
    assert SOURCE in brief and kw["tools"] == []
    assert nv.status_of(s, [a, b]) == {a: "ok", b: "skip"}
    doc = nv.html_for(s, G, a)
    assert doc and "Content-Security-Policy" in doc
    assert nv.html_for(s, "999", a) is None  # 别的群拿不到
    assert not v.has_work(G, NOW)  # 都处理过了


def test_model_picks_none_marks_all_skip(tmp_path) -> None:
    s = _store(tmp_path)
    a = _item(s)
    v, models, workers, tools = _viz(s, pick=-1)
    asyncio.run(v.run(G, now=NOW))
    assert nv.status_of(s, [a]) == {a: "skip"}
    assert workers.briefs == []


def test_bad_html_rejected_with_reason(tmp_path) -> None:
    s = _store(tmp_path)
    a = _item(s)
    v, *_ = _viz(s, html=GOOD.replace("58%", "61%"))
    asyncio.run(v.run(G, now=NOW))
    assert nv.status_of(s, [a]) == {a: "rejected"}
    assert nv.html_for(s, G, a) is None


def test_source_unreachable_fails(tmp_path) -> None:
    s = _store(tmp_path)
    a = _item(s)
    v, _m, workers, _t = _viz(s, tools=FakeTools(ok=False))
    asyncio.run(v.run(G, now=NOW))
    assert nv.status_of(s, [a]) == {a: "failed"}
    assert workers.briefs == []


def test_candidates_exclude_images_personal_old_and_other_groups(tmp_path) -> None:
    s = _store(tmp_path)
    _item(s, title="有图的", image_url="https://img/x.jpg")
    _item(s, title="个人向", target="111")
    _item(s, title="太早的", created=NOW - 13 * 3600)
    _item(s, title="别的群", gid="999")
    v, *_ = _viz(s)
    assert not v.has_work(G, NOW)


def test_daily_cap_and_off(tmp_path) -> None:
    s = _store(tmp_path)
    _item(s)
    v, *_ = _viz(s, per_day=0)
    assert not v.has_work(G, NOW)
    v2, *_ = _viz(s, per_day=1)
    with s.tx() as conn:
        conn.execute(
            "INSERT INTO news_viz (item_id, group_id, status, html, reason, created, updated)"
            " VALUES (99999, ?, 'ok', '<p>1</p>', '', ?, ?)", (G, NOW - 60, NOW - 60))
    assert not v2.has_work(G, NOW)


def test_config_viz_per_day_default_and_clamp() -> None:
    from CharTyr_MaiWork.maiwork.config import load_settings

    s, _ = load_settings({})
    assert s.feeds.viz_per_day == 3
    s, _ = load_settings({"feeds": {"viz_per_day": 0}})
    assert s.feeds.viz_per_day == 0
    s, _ = load_settings({"feeds": {"viz_per_day": 99}})
    assert s.feeds.viz_per_day == 10


# ----------------------------------------------------------------------
# app 接线 / 视图 / 接口
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_viz_round_spawns_once(tmp_path) -> None:
    from pathlib import Path

    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
    ran: list[str] = []

    class _V:
        def has_work(self, gid, now):
            return True

        async def run(self, gid):
            ran.append(gid)
            await asyncio.sleep(0.05)

    app.news_viz = _V()
    app._models_ready = lambda: True
    app._viz_round(G, NOW)
    app._viz_round(G, NOW)  # 在跑就不重复开
    for _ in range(50):
        if (G, "viz") not in app._running_jobs:
            break
        await asyncio.sleep(0.02)
    assert ran == [G]
    app._models_ready = lambda: False
    app._viz_round(G, NOW)
    await asyncio.sleep(0.05)
    assert ran == [G]


def test_feeds_view_flags_viz(tmp_path) -> None:
    from CharTyr_MaiWork.maiwork.feeds import Feeds

    s = _store(tmp_path)
    a = _item(s)
    b = _item(s, title="没图解的")
    with s.tx() as conn:
        conn.execute(
            "INSERT INTO news_viz (item_id, group_id, status, html, reason, created, updated)"
            " VALUES (?, ?, 'ok', '<p>43%</p>', '', ?, ?)", (a, G, NOW, NOW))
    feeds = Feeds(s, None, None, None, None, lambda: None)
    import CharTyr_MaiWork.maiwork.feeds as fm

    orig = fm.clock.now
    fm.clock.now = lambda: NOW
    try:
        items = {it["id"]: it for bt in feeds.news_view(G) for it in bt["items"]}
    finally:
        fm.clock.now = orig
    assert items[a]["viz"] is True
    assert items[b]["viz"] is False
