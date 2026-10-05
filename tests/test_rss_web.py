"""RSS 资讯源接口 + 备料合并（起真 app + MockTransport，不碰真实网络）。

- 接口：GET/POST/DELETE/toggle 只管理员；POST 先试取成功才保存；
- 备料：RSS 条目和搜索子 agent 的候选合并后走同一套质量门槛（硬性淘汰、打分、去重）；
  sources.site 是条目链接算出来的真实域名（2026-10-05 docs/10 §九 第一步 1：以前是 RSS 源标题），
  fetched/quote 交给补打开子 agent 再核对（同上 第一步 3）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeModelsQueue, FakeProfiles, focus_reply

from CharTyr_MaiWork.maiwork import clock, rss
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "sk-rss-web-test-显眼Cc3"
PASSWORD = "RSS 测试密码-显眼-456"
G1 = "900000001"
NOW = 1_790_000_000.0


def _raw_config(data_dir: Path, **over: Any) -> dict:
    raw: dict[str, Any] = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:18652", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class _Env:
    def __init__(self, app: MaiWorkApp, client: TestClient) -> None:
        self.app = app
        self.client = client


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = _raw_config(tmp_path / "data")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    # 注入 RSS MockTransport：不让接口 / 备料碰真实网络
    def _handler(request: httpx.Request) -> httpx.Response:
        if "good" in str(request.url):
            xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>好博客</title>
            <item><title>t1</title><link>https://ok.example.com/1</link></item></channel></rss>"""
            return httpx.Response(200, text=xml)
        if "dead" in str(request.url):
            return httpx.Response(404, text="nope")
        return httpx.Response(200, text="<html>not rss</html>")

    app.rss_transport = httpx.MockTransport(_handler)
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield _Env(app=app, client=client)
    await client.close()
    await app.stop()


async def _login(env: _Env) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


class TestRssApi:
    @pytest.mark.asyncio
    async def test_requires_admin(self, env: _Env) -> None:
        r = await env.client.get(f"/api/groups/{G1}/rss")
        assert r.status == 401
        r = await env.client.post(f"/api/groups/{G1}/rss", json={"url": "https://good.example.com/feed"})
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_add_list_delete(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.post(f"/api/groups/{G1}/rss", json={"url": "https://good.example.com/feed"})
        assert r.status == 200
        data = await r.json()
        assert data["id"]
        assert data["url"] == "https://good.example.com/feed"
        assert data["title"] == "好博客"
        assert data["items_count"] == 1
        # list
        r = await env.client.get(f"/api/groups/{G1}/rss")
        assert r.status == 200
        listed = await r.json()
        assert len(listed["rss"]) == 1
        assert listed["rss"][0]["enabled"] is True
        # toggle
        r = await env.client.post(f"/api/groups/{G1}/rss/{data['id']}/toggle", json={"enabled": False})
        assert r.status == 200
        toggled = await r.json()
        assert toggled["enabled"] is False
        # delete
        r = await env.client.delete(f"/api/groups/{G1}/rss/{data['id']}")
        assert r.status == 200
        r = await env.client.get(f"/api/groups/{G1}/rss")
        assert (await r.json())["rss"] == []

    @pytest.mark.asyncio
    async def test_add_validates_fetch(self, env: _Env) -> None:
        await _login(env)
        for bad, frag in (
            ("not-a-url", "http"),
            ("ftp://x/feed", "http"),
            ("https://dead.example.com/feed", "404"),
        ):
            r = await env.client.post(f"/api/groups/{G1}/rss", json={"url": bad})
            assert r.status == 400, bad
            body = await r.json()
            assert frag in body["error"]

    @pytest.mark.asyncio
    async def test_add_private_url_returns_400_without_fetching_or_saving(self, env: _Env) -> None:
        await _login(env)
        def must_not_request(_: httpx.Request) -> httpx.Response:
            pytest.fail("Private RSS source reached the network transport")
        env.app.rss_transport = httpx.MockTransport(must_not_request)
        for bad in ("http://127.0.0.1/good", "http://169.254.169.254/good", "http://user@public.example/good"):
            r = await env.client.post(f"/api/groups/{G1}/rss", json={"url": bad})
            assert r.status == 400, bad
        assert env.app.store.kv_get(rss._key(G1)) is None

    @pytest.mark.asyncio
    async def test_toggle_not_found(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.post(f"/api/groups/{G1}/rss/rX/toggle", json={"enabled": True})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_add_origin_mismatch_403(self, env: _Env) -> None:
        await _login(env)
        r = await env.client.post(
            f"/api/groups/{G1}/rss",
            json={"url": "https://good.example.com/feed"},
            headers={"Origin": "http://evil.example.com:2"},
        )
        assert r.status == 403
        assert env.app.store.kv_get(rss._key(G1)) is None



# ----------------------------------------------------------------------
# 备料：RSS 条目和搜索候选合并后走同一套质量门槛
# ----------------------------------------------------------------------


class _FakeWorkers:
    def __init__(self, report):
        self.report = report
        self.calls: list[dict] = []

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": brief, **kwargs})
        from fakes import two_phase_workers_run

        return await two_phase_workers_run(self.report, brief, kwargs)


def _worker_report(items: list[dict]) -> Any:
    from CharTyr_MaiWork.maiwork.workers import WorkerReport

    return WorkerReport(ok=True, summary="找好了", data={"items": items}, evidence=[], steps=1)


class _EmptySearch:
    async def search(self, q, **kw):
        return []


class _FakeTopics:
    def __init__(self):
        self.calls: list = []

    def add_candidate(self, *a, **kw) -> None:
        self.calls.append((a, kw))


def _make_feeds(store: Store, workers: Any, models: Any = None) -> Feeds:
    settings, _ = load_settings({})
    profiles = FakeProfiles()
    profiles.entries_map[G1] = [{"category": "interest", "text": "FPGA"}]
    if models is None:
        focus_json = '{"focus": [{"query": "FPGA", "why": "群里在做"}]}'
        models = FakeModelsQueue(ready=True, replies=[focus_json])
    return Feeds(store, models, workers, profiles, _FakeTopics(), lambda: settings, search=_EmptySearch())


def _good_xml() -> str:
    """一条新鲜的 RSS 条目（pubDate = 现在前一小时；新鲜度门槛按真实时钟算）。"""
    import time as _time
    from email.utils import formatdate as _fmt

    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>好博客</title>'
        '<item><title>RSS 好文</title><link>https://ok.example.com/rss1</link>'
        f'<pubDate>{_fmt(_time.time() - 3600, usegmt=True)}</pubDate>'
        '<description>内容</description></item></channel></rss>'
    )


def _rss_handler(request: httpx.Request) -> httpx.Response:
    if "good" in str(request.url):
        return httpx.Response(200, text=_good_xml())
    return httpx.Response(404, text="no")


class TestRssPipeline:
    @pytest.mark.asyncio
    async def test_rss_items_flow_when_search_finds_nothing(self, tmp_path: Path):
        """两阶段恒生效后：撒网一无所获时，RSS 订阅的条目照常并进候选池、
        走完同一套硬门槛 + 落库（老路本来就支持 RSS-only 的轮）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)", (G1, 1_700_000_000.0))
        rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客", feed_id="rGood", now=NOW)

        score = json.dumps({"scores": [{
            "i": 0, "title": "RSS 好文", "info": 5, "source": 5, "relevance": 5,
            "timeliness": 5, "chat": 5, "profile": 0, "topic": "RSS", "sensitive": False,
            "grounded": True, "junk": False, "junk_reason": "", "relation": "unrelated",
            "same_as_recent": False, "novelty": 5, "surprise": 3, "why": "正好对口", "icon": "newspaper",
        }]}, ensure_ascii=False)
        models = FakeModelsQueue(ready=True, replies=[
            '{"focus": [{"query": "FPGA", "why": "群里在做"}, {"query": "本地大模型", "why": "长期"}, {"query": "掌机", "why": "拓展"}]}',
            '{"note": "测试不挑"}', score, '{"posts": []}',
        ])
        workers = _FakeWorkers(_worker_report([]))  # 撒网一无所获（搜索也空）
        feeds = _make_feeds(store, workers, models)
        feeds._rss_transport = httpx.MockTransport(_rss_handler)  # 见 feeds.py：_collect_rss 注入点

        kept = await feeds.prepare_news(G1)
        assert kept == 1
        rows = store.read().execute("SELECT * FROM news_items").fetchall()
        assert [r["url_key"] for r in rows] == ["ok.example.com/rss1"]
        # 来源名按条目链接算（2026-10-05 §九 第一步 1）：以前是 RSS 源标题「好博客」
        assert json.loads(rows[0]["sources"])[0]["site"] == "ok.example.com"
        store.close()

    @pytest.mark.asyncio
    async def test_rss_fetch_error_marks_last_error(self, tmp_path: Path):
        """RSS 源取回失败：备料照常走，last_error 落 kv（下一轮还试，不拖垮）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)", (G1, 1_700_000_000.0))
        rss.add_feed(store, G1, url="https://dead.example.com/feed", title="坏死站", feed_id="rD", now=NOW)

        workers = _FakeWorkers(_worker_report([]))
        feeds = _make_feeds(store, workers)
        feeds._rss_transport = httpx.MockTransport(_rss_handler)  # dead → 404

        await feeds.prepare_news(G1)
        row = rss.list_feeds(store, G1)[0]
        assert row["last_error"] != ""
        assert row["last_ok_ts"] == 0.0
        store.close()
