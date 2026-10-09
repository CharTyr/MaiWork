"""卡片图解时序（2026-10 线上问题，方案 A+B+C）：发到群里的资讯卡片从来没带上图解。

线上实测确认的根因（三处，代码与线上一致）：
- R1 时序：同一批资讯同时触发「卡片长活」和「图解长活」，互不等待；卡片画图（几秒）时
  图解（47 秒 ~ 13 分钟）还没好，所以卡片永远没有图解。
- R2 优先级：条目没配图时 `news_card.prepare_covers` 会打开原文页抓封面，`_viz_todo`
  看到有封面就不画图解 → 已经在库里的 ok 图解被临时抓来的封面顶掉。
- R3 口径：图解每轮只挑 1 条、范围是全群 12 小时前 8 条，和卡片条目（该批 top N）
  不是同一批。

本文件测的就是修完之后的硬约束：
1. flush 先图解、后画图（renderer 拿到的 data 里该条 viz_html 非空）。
2. 图解超时 / 抛错 → 卡片照常入队、不抛异常（等待有上限，远小于 12 小时 TTL）。
3. 有 ok 图解且没自带配图 → 画图解，不再去原文页抓封面；有真 image_url 仍用配图。
4. ensure_for_items 只为传进来的条目建行；超 viz_per_day 不再做；已有记录不重做；
   和 _viz_round 不重复同一条。
5. app 调度层：有到点的卡片时，独立的 _viz_round 让位（名额先给卡片条目）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import card_push, news_card
from CharTyr_MaiWork.maiwork import news_viz as nv
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
NOW = datetime(2026, 10, 15, 12, tzinfo=BJ).timestamp()
SOURCE = (
    "GPT-6 Sol 输入 $2 / 输出 $10 每百万 token，比 5.6 降价 50%。Terminal-Bench 43%（原 37%）。"
)
GOOD = """<style>.bar{height:8px;background:var(--accent)}</style>
<div class="card"><h3>GPT-6 Sol</h3><p>43%（原 37%）<b>+6</b></p><p>价格 −50%</p></div>"""

# 发件箱按内容（PNG 魔数）判图片类型：假渲染器也要给一张「真 PNG 头」的数据
PNG = b"\x89PNG\r\n\x1a\n" + b"card-bytes"


# ----------------------------------------------------------------------
# 假件（卡片路径）
# ----------------------------------------------------------------------


class CardHost:
    def __init__(self) -> None:
        self.images: list[dict] = []

    async def send_image(self, session_id, png, *, text=""):
        self.images.append({"session_id": session_id, "png": png, "text": text})
        return SimpleNamespace(sent=True, message_id=f"img{len(self.images)}")

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        return SimpleNamespace(sent=True, message_id="t1")


class Renderer:
    """假渲染器：记录调用顺序（要断言 viz → render）。"""

    def __init__(self, order: list[str]) -> None:
        self.calls: list[dict] = []
        self.order = order

    async def __call__(self, data):
        self.order.append("render")
        self.calls.append(data)
        return PNG


class StubViz:
    """假图解模块：能落 ok 行、能睡过头、能抛错。"""

    def __init__(self, store: Store, *, order: list[str] | None = None, html: str = "<p>43%</p>",
                 sleep: float = 0.0, error: BaseException | None = None) -> None:
        self.store = store
        self.order = order if order is not None else []
        self.html = html
        self.sleep = sleep
        self.error = error
        self.calls: list[dict] = []
        self.reserved_getter = None

    def set_reserved_getter(self, fn) -> None:
        self.reserved_getter = fn

    async def ensure_for_items(self, gid, item_ids, *, now=None, deadline=None):
        self.order.append("viz")
        self.calls.append({"gid": gid, "ids": [int(i) for i in item_ids], "now": now, "deadline": deadline})
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.error is not None:
            raise self.error
        for i in item_ids:
            with self.store.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO news_viz (item_id, group_id, status, html, reason, created, updated)"
                    " VALUES (?, ?, 'ok', ?, '', ?, ?)",
                    (int(i), str(gid), self.html, float(now or 0), float(now or 0)),
                )
        return len(list(item_ids))


def _make(tmp_path, *, viz=None, viz_wait_s=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    cfg = {
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "environments": {"workspace_root": str(tmp_path)},
        "console": {"public_url": "https://mw.example"},
    }
    settings, _ = load_settings(cfg)
    host = CardHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    order: list[str] = []
    renderer = Renderer(order)
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    kw: dict = {}
    if viz_wait_s is not None:
        kw["viz_wait_s"] = viz_wait_s
    cp = card_push.CardPush(store, host, pushes, mentions, lambda: settings,
                            renderer=renderer, outbox=ob, viz=viz, **kw)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)",
            (GID, f"sess-{GID}", "测试群", f"tok{GID}"),
        )
    return store, settings, host, pushes, mentions, renderer, cp, ob


def _batch(store, *, gid=GID, created=NOW, items=None):
    items = items if items is not None else [
        {"score": 4.5, "kind": "news", "image_url": ""},
        {"score": 4.0, "kind": "news", "image_url": "https://img.example/a.jpg"},
    ]
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 10, ?, 0, '', ?)",
            (gid, created, len(items), created),
        )
        bid = int(cur.lastrowid)
        ids = []
        for i, it in enumerate(items):
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, why, sources, score, created,"
                " kind, image_url, keywords, target_user_id, rejected, body)"
                " VALUES (?, ?, ?, '摘要', '值得看', ?, ?, ?, ?, ?, '[\"k\"]', '', 0, '正文')",
                (bid, gid, it.get("title", f"标题{bid}-{i}"),
                 json.dumps([{"url": it.get("url", f"https://s.example/{bid}/{i}"), "site": "s.example"}]),
                 it["score"], created, it["kind"], it.get("image_url", "")),
            )
            ids.append(int(cur.lastrowid))
    return bid, ids


def _enable(store, **kw):
    patch = {"news_card_enabled": True}
    patch.update(kw)
    return card_push.set_config(store, GID, patch, now=NOW - 3600)


def _boxes(store):
    return store.read().execute("SELECT * FROM outbox ORDER BY id").fetchall()


# ----------------------------------------------------------------------
# A. 时序：画卡前先图解，有上限地等
# ----------------------------------------------------------------------


async def test_flush_makes_viz_before_render(tmp_path):
    store, _s, host, _p, _m, renderer, cp, ob = _make(tmp_path)
    viz = StubViz(store, order=renderer.order)
    cp.attach_viz(viz)
    _enable(store, news_card_count=1)
    _batch(store, items=[{"score": 4.5, "kind": "news", "image_url": ""}])
    assert cp.scan(GID, NOW + 60) == 1
    await cp.flush(GID, NOW + 60)
    assert viz.calls and viz.calls[0]["ids"]           # 画卡前先为卡片条目补图解
    data = renderer.calls[0]
    assert str(data["items"][0]["viz_html"]).strip()   # 画图时 data 里已经带上图解
    assert "Content-Security-Policy" in data["items"][0]["viz_html"]
    assert renderer.order == ["viz", "render"]         # 顺序：viz → render
    await ob.flush(NOW + 61)
    assert len(host.images) == 1


async def test_viz_timeout_still_enqueues_card(tmp_path):
    """图解睡过头（超过上限）→ 不阻塞、不抛异常，卡片照旧入队发出。"""
    store, _s, host, _p, _m, renderer, cp, ob = _make(tmp_path, viz_wait_s=0.05)
    viz = StubViz(store, sleep=5.0)
    cp.attach_viz(viz)
    _enable(store, news_card_count=1)
    _batch(store, items=[{"score": 4.5, "kind": "news", "image_url": ""}])
    cp.scan(GID, NOW + 60)
    await cp.flush(GID, NOW + 60)
    assert renderer.calls and renderer.calls[0]["items"][0]["viz_html"] == ""
    boxes = _boxes(store)
    assert boxes and boxes[0]["status"] == "pending" and boxes[0]["kind"] == "image"
    await ob.flush(NOW + 61)
    assert len(host.images) == 1


async def test_viz_error_still_enqueues_card(tmp_path):
    store, _s, host, _p, _m, renderer, cp, ob = _make(tmp_path)
    cp.attach_viz(StubViz(store, error=RuntimeError("图解炸了")))
    _enable(store, news_card_count=1)
    _batch(store, items=[{"score": 4.5, "kind": "news", "image_url": ""}])
    cp.scan(GID, NOW + 60)
    await cp.flush(GID, NOW + 60)  # 不能把异常抛进后台循环
    assert renderer.calls and _boxes(store)[0]["status"] == "pending"
    await ob.flush(NOW + 61)
    assert len(host.images) == 1


async def test_viz_wait_budget_leaves_room_before_ttl(tmp_path):
    """等待有上限：默认 150 秒的常量，且给发件箱留余量，绝不把卡片等到过期。"""
    assert card_push.CARD_VIZ_WAIT_S == 150.0
    store, _s, _h, _p, _m, _r, cp, _ob = _make(tmp_path)
    assert cp._viz_budget(NOW, NOW) <= card_push.CARD_VIZ_WAIT_S
    # 快到 12 小时 TTL：宁可不等图解，也不能把卡片等过期
    assert cp._viz_budget(NOW, NOW + 12 * 3600 - 10) == 0.0


# ----------------------------------------------------------------------
# B. 优先级：有 ok 图解就不再去原文页抓封面
# ----------------------------------------------------------------------


async def test_viz_wins_over_scraped_cover():
    calls: list[str] = []

    async def fake(img, url):
        calls.append(url)
        return ("data:image/jpeg;base64,/9j/AAAA", 900)

    d = {"items": [
        {"title": "有图解", "url": "https://a.com/x", "viz_html": "<p>43%</p>"},
        {"title": "真配图", "image_url": "https://img.example/a.jpg", "url": "https://a.com/y",
         "viz_html": "<p>1</p>"},
        {"title": "没图解", "url": "https://a.com/z"},
    ]}
    await news_card.prepare_covers(d, fetch=fake)
    assert calls == ["https://a.com/y", "https://a.com/z"]     # 有图解的没去抓封面
    assert not str(d["items"][0].get("cover") or "")           # → 画图解
    assert str(d["items"][1]["cover"]).startswith("data:image/")  # 真 image_url 仍用配图
    assert str(d["items"][2]["cover"]).startswith("data:image/")  # 没图解照旧抓封面
    # 最后真的要画的：有图解的那条（没封面的走截图解；有封面的仍用封面）
    assert [it["title"] for it in news_card._viz_todo(d)] == ["有图解"]


# ----------------------------------------------------------------------
# C. 口径：ensure_for_items 只为传入条目做，名额 / 去重都管住
# ----------------------------------------------------------------------


class VizModels:
    def __init__(self, reply: dict | None = None) -> None:
        self.reply = reply if reply is not None else {"pick": 0, "why": "有数字"}
        self.calls = []

    def settings(self):
        return SimpleNamespace(ready=lambda: True)

    async def chat(self, role=None, messages=None, **kw):
        self.calls.append((role, messages, kw))
        return SimpleNamespace(text=json.dumps(self.reply))


class VizWorkers:
    def __init__(self, html: str = GOOD, *, sleep: float = 0.0) -> None:
        self.html = html
        self.sleep = sleep
        self.briefs = []

    async def run(self, brief, **kw):
        self.briefs.append((brief, kw))
        if self.sleep:
            await asyncio.sleep(self.sleep)
        return SimpleNamespace(ok=True, data={"html": self.html}, error="", summary="")


class VizTools:
    def __init__(self, text: str = SOURCE, *, sleep: float = 0.0) -> None:
        self.text = text
        self.sleep = sleep
        self.urls: list[str] = []

    async def call(self, name, args, ctx):
        assert name == "fetch_page"
        if self.sleep:
            await asyncio.sleep(self.sleep)
        self.urls.append(args["url"])
        return SimpleNamespace(ok=True, output=self.text, error="")


def _vstore(tmp_path) -> Store:
    s = Store(tmp_path / "v.db")
    s.migrate()
    return s


def _vitem(s, *, title="GPT-6 降价", image_url="", created=NOW - 3600) -> int:
    with s.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)", (GID, created, created))
        bid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, sources, url_key, score, created,"
            " kind, rejected, target_user_id, image_url, body)"
            " VALUES (?, ?, ?, '摘要', ?, 'a.com/x', 4.0, ?, 'news', 0, '', ?, '正文')",
            (bid, GID, title, json.dumps([{"url": "https://a.com/x", "site": "a.com", "title": title}]),
             created, image_url))
        return int(cur.lastrowid)


def _vizmod(s, *, per_day=3, workers=None, tools=None, models=None):
    settings = SimpleNamespace(feeds=SimpleNamespace(viz_per_day=per_day), is_served=lambda g: True)
    models = models or VizModels()
    workers = workers or VizWorkers()
    tools = tools or VizTools()
    return nv.NewsViz(s, models, workers, tools, lambda: settings), models, workers, tools


async def test_ensure_only_touches_given_items(tmp_path):
    s = _vstore(tmp_path)
    a = _vitem(s)
    b = _vitem(s, title="另一条")
    v, models, workers, tools = _vizmod(s)
    made = await v.ensure_for_items(GID, [a], now=NOW, deadline=NOW + 60)
    assert made == 1
    assert nv.status_of(s, [a]) == {a: "ok"}
    assert nv.status_of(s, [b]) == {}          # 没传进来的条目一行都不建
    assert tools.urls == ["https://a.com/x"]
    assert workers.briefs and workers.briefs[0][1]["tools"] == []
    assert models.calls == []                  # 卡片条目已经定了，不再让主模型挑一条


async def test_ensure_no_redo_and_respects_daily_cap(tmp_path):
    s = _vstore(tmp_path)
    a = _vitem(s)
    v, _m, workers, tools = _vizmod(s)
    assert await v.ensure_for_items(GID, [a], now=NOW, deadline=NOW + 60) == 1
    # 已有 ok 记录：不重做（不重复调原文页 / 子 agent）
    tools.urls.clear()
    workers.briefs.clear()
    assert await v.ensure_for_items(GID, [a], now=NOW, deadline=NOW + 60) == 0
    assert tools.urls == [] and workers.briefs == []
    # 今天已经 ok 一张、名额定 1：不再做
    v2, _m2, workers2, tools2 = _vizmod(s, per_day=1)
    d = _vitem(s, title="第三条")
    assert await v2.ensure_for_items(GID, [d], now=NOW, deadline=NOW + 60) == 0
    assert tools2.urls == [] and workers2.briefs == []
    assert nv.status_of(s, [d]) == {}


async def test_ensure_skips_items_with_real_image(tmp_path):
    """资讯自带配图（真 image_url）仍用配图，不为它做图解。"""
    s = _vstore(tmp_path)
    a = _vitem(s, image_url="https://img.example/a.jpg")
    v, _m, workers, tools = _vizmod(s)
    assert await v.ensure_for_items(GID, [a], now=NOW, deadline=NOW + 60) == 0
    assert tools.urls == [] and workers.briefs == []
    assert nv.status_of(s, [a]) == {}


async def test_ensure_stops_at_deadline(tmp_path):
    """到截止时间就停：不越线、不抛异常；这张不建行，交给 _viz_round 用剩余名额。"""
    s = _vstore(tmp_path)
    a = _vitem(s)
    v, _m, _w, tools = _vizmod(s, workers=VizWorkers(sleep=5.0))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    made = await v.ensure_for_items(GID, [a], now=NOW, deadline=NOW + 0.05)
    elapsed = loop.time() - t0
    assert made == 0 and elapsed < 1.0
    assert nv.status_of(s, [a]) == {}


async def test_ensure_does_not_double_with_viz_round(tmp_path):
    """卡片路径正在做这条时，_viz_round 不再抢同一条（也不浪费一次挑的模型调用）。"""
    s = _vstore(tmp_path)
    a = _vitem(s)
    v, models, workers, tools = _vizmod(s, tools=VizTools(sleep=0.05))
    task = asyncio.create_task(v.ensure_for_items(GID, [a], now=NOW, deadline=NOW + 60))
    await asyncio.sleep(0)                      # 让卡片路径先占住这条
    assert not v.has_work(GID, NOW)             # 在做 / 已做的条目不算候选
    await v.run(GID, now=NOW)                   # 原 _viz_round 这条路：什么都不做
    assert await task == 1
    assert tools.urls == ["https://a.com/x"]
    assert len(workers.briefs) == 1
    assert models.calls == []
    assert nv.status_of(s, [a]) == {a: "ok"}


async def test_viz_round_leaves_pending_card_items_alone(tmp_path):
    """等卡片的条目归卡片路径：_viz_round 既不当候选，也不把它记成 skip。"""
    s = _vstore(tmp_path)
    a = _vitem(s, title="卡片条目")
    b = _vitem(s, title="另一条")
    with s.tx() as conn:
        conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts)"
            " VALUES (?, 1, 'pending', ?, ?, ?)",
            (GID, json.dumps([a]), NOW, NOW),
        )
    # 候选里只剩 b（a 被保留给卡片路径），挑中的是 b；a 一行都不记
    v, models, workers, tools = _vizmod(s, models=VizModels({"pick": 0, "why": "只剩这条"}))
    v.set_reserved_getter(lambda gid: {a})
    await v.run(GID, now=NOW)
    assert nv.status_of(s, [a, b]) == {b: "ok"}   # a 不记 skip，留给卡片路径
    assert [it["title"] for it in v._candidates(GID, NOW)] == []


async def test_attach_viz_wires_pending_items(tmp_path):
    """attach_viz 把「等卡片的条目」告诉图解模块（app 每轮补接一次）。"""
    store, _s, _h, _p, _m, _r, cp, _ob = _make(tmp_path)
    _enable(store, news_card_count=1)
    _, ids = _batch(store, items=[{"score": 4.5, "kind": "news", "image_url": ""}])
    cp.scan(GID, NOW + 60)
    assert cp.pending_item_ids(GID) == {ids[0]}
    viz = StubViz(store)
    cp.attach_viz(viz)
    assert viz.reserved_getter(GID) == {ids[0]}


async def test_app_viz_round_yields_to_due_card(tmp_path):
    """app 调度层：有到点的卡片时 _viz_round 让位（图解名额先给卡片条目）。"""
    from pathlib import Path

    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
    ran: list[str] = []

    class _V:
        def has_work(self, gid, now):
            return True

        async def run(self, gid):
            ran.append(gid)

    class _CP:
        due = True

        def has_due(self, gid, now):
            return bool(self.due)

    app.news_viz = _V()
    app.card_push = _CP()
    app._models_ready = lambda: True
    app._viz_round(GID, NOW)
    await asyncio.sleep(0.05)
    assert ran == []                            # 卡片到点：这轮不让图解抢名额
    app.card_push.due = False
    app._viz_round(GID, NOW)
    for _ in range(50):
        if (GID, "viz") not in app._running_jobs:
            break
        await asyncio.sleep(0.02)
    assert ran == [GID]


async def test_app_viz_round_runs_during_quiet_hours(tmp_path):
    """睡觉时段卡片发不出去：这轮图解照跑（提前做好，早上卡片能直接用上），不干等。"""
    from pathlib import Path

    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    app = MaiWorkApp(ctx=None, raw_config={}, plugin_dir=Path(__file__).resolve().parents[1])
    ran: list[str] = []

    class _V:
        def has_work(self, gid, now):
            return True

        async def run(self, gid):
            ran.append(gid)

    class _CP:
        def has_due(self, gid, now):
            return True

    class _P:
        def in_quiet(self, now, gid):
            return True

    app.news_viz = _V()
    app.card_push = _CP()
    app.pushes = _P()
    app._models_ready = lambda: True
    app._viz_round(GID, NOW)
    for _ in range(50):
        if (GID, "viz") not in app._running_jobs:
            break
        await asyncio.sleep(0.02)
    assert ran == [GID]
