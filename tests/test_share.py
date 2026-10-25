"""share.py：没填公开网址时，把群友要看的那一页发布到 here.now（假客户端，不联网）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock, share
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.herenow import HereNowError
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

GID = "900000001"
TOKEN = "tokSECRET9999"


class FakeHereNow:
    def __init__(self) -> None:
        self.pages: list[str] = []
        self.error: Exception | None = None

    async def publish(self, directory: Path) -> dict:
        files = sorted(p.name for p in Path(directory).iterdir())
        assert files == ["index.html"]
        self.pages.append((Path(directory) / "index.html").read_text(encoding="utf-8"))
        if self.error is not None:
            raise self.error
        return {"url": f"https://x{len(self.pages)}.here.now/", "slug": f"x{len(self.pages)}"}


def _view(idea_title: str = "做个铝价看板") -> dict:
    """模拟 group_view(admin=False) 的输出，故意塞进不该外泄的字段。"""
    return {
        "id": GID,
        "name": "测试群<b>",
        "token": TOKEN,
        "view": {
            "workspace": "/root/ws/secret_ws",
            "profile": {"persona": "画像：张三爱喝酒"},
            "news": [{"id": 1, "items": [
                {"id": 11, "title": "铝价大涨", "summary": "<script>alert(1)</script>摘要",
                 "why": "群里聊过", "profile_ref": "画像引用-机密",
                 "refs": [{"who": "张三", "user_id": "u777", "text": "原话-机密"}],
                 "sources": [{"url": "https://news.example/a", "site": "示例网"},
                             {"url": "javascript:alert(1)", "site": "坏链接"}]},
                {"id": 14, "title": "敏感资讯", "summary": "s", "sensitive": True},
            ]}],
            "ideas": [{"id": 12, "title": idea_title, "body": "构想正文", "basis": "依据-机密",
                       "requested_by": "u888", "keywords": ["关键词-机密"], "step": "第一步"},
                      {"id": 13, "title": "另一条构想", "body": "别的"}],
            "goals": {"agent": [{"id": "G-5", "title": "盯铝价", "body": "目标说明",
                                 "state": "active", "stale_reason": "内部原因-机密",
                                 "criteria": [{"text": "价格过两万", "backed": True,
                                               "evidence": "群友原话-机密", "confirmed_by": "admin-机密"}],
                                 "last": {"ts": 0, "text": "最近进展"}}],
                      "member": [{"id": "M-1", "who": "李四", "title": "吃药"}]},
            "tasks": {"list": [{"id": "T-1", "question": "整理资料", "state": "done",
                                "workspace": "/root/ws/secret_ws", "req": "需求原文-机密"}],
                      "review": [{"text": "待复核-机密"}]},
        },
    }


def _settings(tmp_path: Path, *, public_url: str = "", herenow_share=None):
    console: dict = {}
    if public_url:
        console["public_url"] = public_url
    if herenow_share is not None:
        console["herenow_share"] = herenow_share
    cfg = {"groups": {"serve": [{"group": f"qq:{GID}"}]},
           "environments": {"workspace_root": str(tmp_path)}}
    if console:
        cfg["console"] = console
    return load_settings(cfg)[0]


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute("INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)", (GID, "s", "x", TOKEN))
    return store


def _make(tmp_path, *, view=None, **kw):
    store = _store(tmp_path)
    settings = _settings(tmp_path, **kw)
    hn = FakeHereNow()
    holder = {"view": view or _view()}
    sh = share.Share(store, hn, lambda: settings, view_fn=lambda gid: holder["view"])
    return store, settings, hn, sh, holder


async def test_page_does_not_leak(tmp_path):
    _store_, _s, hn, sh, _h = _make(tmp_path)
    for tab, item in (("news", ""), ("ideas", "I-12"), ("tasks", "G-5"), ("tasks", ""), ("ideas", "")):
        url = await sh.publish(GID, tab=tab, item=item)
        assert url.startswith("https://")
    assert len(hn.pages) == 5
    for page in hn.pages:
        for bad in (TOKEN, "画像", "/root/ws", "secret_ws", "u777", "u888",
                    "<script>", "javascript:", "<b>", "内部原因-机密", "admin-机密",
                    "待复核-机密", "关键词-机密"):
            assert bad not in page, (bad, page[:200])
        assert "由 MaiWork 生成 · 24 小时内有效" in page
        assert "测试群&lt;b&gt;" in page  # 和群友网页一样写群名（转义）
        assert "<link" not in page and "src=\"http" not in page  # 无外部依赖
    news, idea, goal = hn.pages[0], hn.pages[1], hn.pages[2]
    assert "铝价大涨" in news and 'href="https://news.example/a"' in news
    assert "做个铝价看板" in idea and "另一条构想" not in idea and "构想正文" in idea
    assert "盯铝价" in goal and "价格过两万" in goal and "最近进展" in goal
    # 2026-10-25 用户定：公开页不比群友网页保守——群友原话、构想依据、任务需求、敏感资讯都放
    assert "张三" in news and "原话-机密" in news
    assert "敏感资讯" in news
    assert "依据-机密" in idea
    assert "需求原文-机密" in hn.pages[3]
    for page in hn.pages:
        assert "群友原话-机密" not in page  # 群友网页本来就不给目标证据


async def test_cache_reuse_and_expire(tmp_path, monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store, _s, hn, sh, holder = _make(tmp_path)
    u1 = await sh.publish(GID, tab="ideas", item="I-12")
    u2 = await sh.publish(GID, tab="ideas", item="I-12")
    assert u1 == u2 and len(hn.pages) == 1
    rec = store.kv_get(f"share.{GID}.ideas.I-12")
    assert rec["url"] == u1 and rec["digest"]
    assert share.cached_link(store, GID, tab="ideas", item="I-12") == u1
    holder["view"] = _view("改过的标题")          # 内容变了 → 重发
    u3 = await sh.publish(GID, tab="ideas", item="I-12")
    assert u3 != u1 and len(hn.pages) == 2
    t[0] += 13 * 3600                            # 超过 12 小时 → 重发
    u4 = await sh.publish(GID, tab="ideas", item="I-12")
    assert u4 != u3 and len(hn.pages) == 3
    t[0] += 13 * 3600
    assert share.cached_link(store, GID, tab="ideas", item="I-12") == ""


async def test_publish_failure_returns_empty(tmp_path, caplog):
    _st, _s, hn, sh, _h = _make(tmp_path)
    hn.error = HereNowError("太频繁", retry_after=30)
    assert await sh.publish(GID, tab="news") == ""
    hn.error = RuntimeError("网络断了")
    assert await sh.publish(GID, tab="news") == ""
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_link_prefers_public_url(tmp_path):
    store, settings, hn, sh, _h = _make(tmp_path, public_url="https://mw.example")
    link = await share.group_link_async(store, settings, GID, tab="ideas", item="I-12", share=sh)
    assert link == f"https://mw.example/#/{TOKEN}/ideas/I-12"
    assert hn.pages == []


async def test_link_uses_herenow_by_default(tmp_path):
    store, settings, hn, sh, _h = _make(tmp_path)
    assert settings.console.herenow_share is True
    link = await share.group_link_async(store, settings, GID, tab="news", share=sh)
    assert link == "https://x1.here.now/"
    assert share.link_available(settings, sh) is True


async def test_link_switch_off(tmp_path):
    store, settings, hn, sh, _h = _make(tmp_path, herenow_share=False)
    assert await share.group_link_async(store, settings, GID, tab="news", share=sh) == ""
    assert hn.pages == []
    assert share.link_available(settings, sh) is False
    # 没有 here.now 客户端也是 ""
    s2 = _settings(tmp_path)
    assert await share.group_link_async(store, s2, GID, tab="news", share=None) == ""
    assert share.link_available(s2, None) is False
