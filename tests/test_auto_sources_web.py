"""自动订阅的接口字段 + 入库标记（docs/10 §九 第二步）。

- rss.py：条目带 auto / origin / reason / label / trial_until，_plain / list_feeds / add_feed
  都保留；老条目（kv 里没有这些键）照常工作、list_feeds 给默认值；
- GET /api/groups/{gid}/rss：把上面这些字段带给网页，并带上 rss_auto_log（按群）、
  来源地图（feeds.source_map.<群号>）和名额 / push 清单视图；
- DELETE 自动源 → 记进 feeds.rss_auto_rejected.<群号>（以后不再推荐）+ 自动操作日志；
- 备料：RSS 候选入库时 src_provider 写成 "rss:<feed_id>"（第二步靠它认出 RSS 条目：
  门槛统计里 RSS 条目不计高分也不计反应），并在入库后把「这轮给了几条候选 / 哪几条
  进了资讯」记进 feeds.rss_stats.<群号>。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeModelsQueue, FakeProfiles

from CharTyr_MaiWork.maiwork import auto_sources, rss
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "sk-auto-sources-显眼Aa1"
PASSWORD = "自动订阅测试密码-显眼-789"
G1 = "900000001"
NOW = 1_790_000_000.0


def _raw_config(data_dir: Path, **over: Any) -> dict:
    raw: dict[str, Any] = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:18655", "password": PASSWORD, "public_url": ""},
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
    app.rss_transport = httpx.MockTransport(lambda req: httpx.Response(404, text="no"))
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


# ----------------------------------------------------------------------
# rss.py：字段
# ----------------------------------------------------------------------


class TestRssFields:
    def _store(self, tmp_path: Path) -> Store:
        s = Store(tmp_path / "t.db")
        s.migrate()
        return s

    def test_add_feed_keeps_auto_fields(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        entry = rss.add_feed(
            s, G1, url="https://a.example.com/feed", title="A", feed_id="rA", now=NOW,
            auto=True, origin="trusted", reason="近 30 天高分多", label="a.example.com",
            trial_until=NOW + 14 * 86400.0,
        )
        assert entry["auto"] is True and entry["origin"] == "trusted"
        assert entry["label"] == "a.example.com"
        assert entry["reason"] == "近 30 天高分多"
        assert entry["trial_until"] == NOW + 14 * 86400.0
        listed = rss.list_feeds(s, G1)[0]
        for key in ("auto", "origin", "reason", "label", "trial_until"):
            assert key in listed
        assert listed["origin"] == "trusted"
        s.close()

    def test_manual_feed_defaults(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        entry = rss.add_feed(s, G1, url="https://m.example.com/feed", title="M", feed_id="rM", now=NOW)
        assert entry["auto"] is False and entry["origin"] == ""
        assert entry["reason"] == "" and entry["label"] == "" and entry["trial_until"] == 0.0
        s.close()

    def test_old_entries_without_fields_still_work(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        with s.tx() as conn:
            s.kv_set(conn, rss._key(G1), [
                {"id": "old", "url": "https://old.example.com/feed", "title": "老源", "enabled": True,
                 "added_ts": NOW, "last_ok_ts": 0.0, "last_error": ""}
            ])
        listed = rss.list_feeds(s, G1)[0]
        assert listed["id"] == "old" and listed["enabled"] is True
        assert listed["auto"] is False and listed["origin"] == "" and listed["trial_until"] == 0.0
        rss.toggle_feed(s, G1, "old", enabled=False)
        assert rss.list_feeds(s, G1)[0]["enabled"] is False
        s.close()


# ----------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------


class TestRssApiFields:
    @pytest.mark.asyncio
    async def test_requires_admin(self, env: _Env) -> None:
        r = await env.client.get(f"/api/groups/{G1}/rss")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_list_carries_auto_fields_log_map_and_quota(self, env: _Env) -> None:
        await _login(env)
        store = env.app.store
        rss.add_feed(store, G1, url="https://m.example.com/feed", title="手动", feed_id="rM", now=NOW)
        rss.add_feed(
            store, G1, url="https://a.example.com/feed", title="自动", feed_id="rA", now=NOW,
            auto=True, origin="trusted", reason="近 30 天高分多", label="a.example.com",
            trial_until=NOW + 14 * 86400.0,
        )
        auto_sources.note_log(store, G1, action="subscribed", label="a.example.com",
                              url="https://a.example.com/feed", origin="trusted", reason="高分多", now=NOW)
        auto_sources.save_map(store, G1, [
            {"name": "好来源", "url": "https://good.example.com/", "why": "一手",
             "label": "good.example.com", "feed_url": "https://good.example.com/feed",
             "status": "verified", "reason": "", "origin": "model"},
        ], NOW)
        r = await env.client.get(f"/api/groups/{G1}/rss")
        assert r.status == 200
        body = await r.json()
        assert len(body["rss"]) == 2
        auto = next(e for e in body["rss"] if e["id"] == "rA")
        assert auto["auto"] is True and auto["origin"] == "trusted"
        assert auto["label"] == "a.example.com" and auto["reason"] == "近 30 天高分多"
        assert auto["trial_until"] == NOW + 14 * 86400.0
        manual = next(e for e in body["rss"] if e["id"] == "rM")
        assert manual["auto"] is False and manual["origin"] == ""
        # 自动操作日志（按群）
        assert body["auto_log"][0]["action"] == "subscribed"
        assert body["auto_log"][0]["label"] == "a.example.com"
        # 来源地图（按群）
        assert body["source_map"]["sources"][0]["status"] == "verified"
        # 名额 / push 清单
        assert body["auto"]["quota"]["trusted"]["used"] == 1
        assert body["auto"]["quota"]["trusted"]["max"] == auto_sources.ORIGIN_QUOTA["trusted"]
        assert any(p["id"] == "hn" for p in body["auto"]["push"])
        assert body["auto"]["rule"]

    @pytest.mark.asyncio
    async def test_delete_auto_feed_records_rejection(self, env: _Env) -> None:
        await _login(env)
        store = env.app.store
        rss.add_feed(
            store, G1, url="https://a.example.com/feed", title="自动", feed_id="rA", now=NOW,
            auto=True, origin="trusted", label="a.example.com",
        )
        r = await env.client.delete(f"/api/groups/{G1}/rss/rA")
        assert r.status == 200
        assert rss.list_feeds(store, G1) == []
        assert "不再推荐" in auto_sources.gate_reason(store, G1, label="a.example.com", url="")
        assert auto_sources.load_log(store, G1)[0]["action"] == "rejected"

    @pytest.mark.asyncio
    async def test_delete_manual_feed_does_not_block_it(self, env: _Env) -> None:
        await _login(env)
        store = env.app.store
        rss.add_feed(store, G1, url="https://m.example.com/feed", title="手动", feed_id="rM", now=NOW)
        r = await env.client.delete(f"/api/groups/{G1}/rss/rM")
        assert r.status == 200
        assert auto_sources.rejected_list(store, G1) == []
        assert auto_sources.load_log(store, G1) == []


# ----------------------------------------------------------------------
# 备料：src_provider + 每源统计
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


def _good_xml() -> str:
    from email.utils import formatdate

    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>好博客</title>'
        '<item><title>RSS 好文</title><link>https://ok.example.com/rss1</link>'
        f'<pubDate>{formatdate(time.time() - 3600, usegmt=True)}</pubDate>'
        '<description>内容</description></item></channel></rss>'
    )


class TestRssPipelineMarking:
    @pytest.mark.asyncio
    async def test_rss_item_carries_src_provider_and_round_stats(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)", (G1, 1_700_000_000.0))
        rss.add_feed(store, G1, url="https://good.example.com/feed", title="好博客",
                     feed_id="rGood", now=NOW)
        settings, _ = load_settings({})
        profiles = FakeProfiles()
        profiles.entries_map[G1] = [{"category": "interest", "text": "FPGA"}]
        score = json.dumps({"scores": [{
            "i": 0, "title": "RSS 好文", "info": 5, "source": 5, "relevance": 5,
            "timeliness": 5, "chat": 5, "profile": 0, "topic": "RSS", "sensitive": False,
            "grounded": True, "junk": False, "junk_reason": "", "relation": "unrelated",
            "same_as_recent": False, "novelty": 5, "surprise": 3, "why": "正好对口", "icon": "newspaper",
        }]}, ensure_ascii=False)
        models = FakeModelsQueue(ready=True, replies=[
            '{"focus": [{"query": "FPGA", "why": "群里在做"}]}', '{"note": "测试不挑"}', score,
            '{"posts": []}',
        ])
        feeds = Feeds(store, models, _FakeWorkers(_worker_report([])), profiles, _FakeTopics(),
                      lambda: settings, search=_EmptySearch())
        feeds._rss_transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=_good_xml())
            if "good" in str(req.url) else httpx.Response(404, text="no")
        )
        kept = await feeds.prepare_news(G1)
        assert kept == 1
        row = store.read().execute(
            "SELECT id, src_provider FROM news_items WHERE rejected=0"
        ).fetchone()
        assert row["src_provider"] == "rss:rGood"
        stats = store.kv_get(auto_sources.stats_key(G1)) or {}
        assert stats["rGood"][0]["cand"] == 1
        assert stats["rGood"][0]["kept"] == [int(row["id"])]
        store.close()
