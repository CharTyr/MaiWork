"""来源名统一（docs/10-资讯流水线改进计划.md §九 第一步 1，2026-10-05）。

现象（线上只读核查 2026-10-05）：`news_items.sources[0].site` 有时是中文站名
（「机核」18 条、「游研社」3 条、「触乐」1 条）。查代码：site 是代码从 url 算的
（`_site_of`），**唯一例外**是 RSS 候选——`_collect_rss` 拿 RSS 源标题（`entry.title`）
当 site，所以「机核」这类源标题进了库。同一网站因此被拆成两份，站名也没法拿去
site 搜 / 找 RSS。

做法：一个函数 `source_name.site_of_url`（写入和统计共用）：
- 一律用真实域名（从 url 算、去 www.）；
- 大平台按作者 / 仓库 / 子域名算：`github.com/<owner>`、`<name>.substack.com`、
  `medium.com/@<作者>`、`dev.to/<作者>`；
- YouTube / B 站的视频地址（/watch?v=、/video/BV…）里拿不到频道，不做按频道切分
  （同一频道会一会拆成 youtube.com/@x、一会只剩 youtube.com，比不拆更乱）。
旧库里已有的中文 site 不改数据，统计（source_stats）按 url 重算归到域名。
"""

from __future__ import annotations

import asyncio
import json
from email.utils import formatdate

import httpx
import pytest

from CharTyr_MaiWork.maiwork import rss, source_name, source_stats
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds, _domain_blocked, _normalize_url
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"


def _run(coro):
    return asyncio.run(coro)


class _TimePatch:
    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class _NoopWorkers:
    async def run(self, brief, **kwargs):  # pragma: no cover - 这些用例不该派子 agent
        raise AssertionError("这个用例不该派子 agent")


class _Models:
    def settings(self):
        class _S:
            def ready(self) -> bool:
                return True

        return _S()


class _Profiles:
    def entries(self, gid):
        return []


class _Topics:
    def add_candidate(self, *a, **kw):
        return None


def _feeds(tmp_path, name: str = "t.db") -> tuple:
    store = Store(tmp_path / name)
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (GID, 1_700_000_000.0),
        )
    settings, _ = load_settings({})
    feeds = Feeds(store, _Models(), _NoopWorkers(), _Profiles(), _Topics(), lambda: settings)
    return store, settings, feeds


def _rss_xml(items: list[tuple[str, str]], *, title: str = "机核") -> str:
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<pubDate>{formatdate(NOW - 3600, usegmt=True)}</pubDate>"
        f"<description>源里的摘要。</description></item>"
        for t, u in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{body}</channel></rss>'


# ----------------------------------------------------------------------
# 单元：真实域名 + 大平台按作者 / 仓库 / 子域名
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,want",
    [
        ("https://www.gcores.com/articles/123", "gcores.com"),
        ("http://gcores.com/", "gcores.com"),
        ("https://www.yystv.cn/p/12345", "yystv.cn"),
        ("https://chuapp.com/2026/x", "chuapp.com"),
        ("https://www.nintendolife.com/news/2026/09/x", "nintendolife.com"),
    ],
)
def test_site_of_url_uses_real_domain(url: str, want: str) -> None:
    assert source_name.site_of_url(url) == want


@pytest.mark.parametrize(
    "url,want",
    [
        # GitHub 按仓库主人
        ("https://github.com/openai/whisper", "github.com/openai"),
        ("https://github.com/openai", "github.com/openai"),
        ("https://www.github.com/openai/whisper/tree/main/x", "github.com/openai"),
        # GitHub 自己的栏目页不是某个作者
        ("https://github.com/features/copilot", "github.com"),
        ("https://github.com/trending", "github.com"),
        # Substack：作者子域名本身就是一站
        ("https://foo.substack.com/p/some-post", "foo.substack.com"),
        ("https://www.foo.substack.com/p/some-post", "foo.substack.com"),
        ("https://substack.com/home", "substack.com"),
        # Medium 按 @作者（专栏页不拆）
        ("https://medium.com/@someone/some-post-1a2b3c", "medium.com/@someone"),
        ("https://medium.com/towards-data-science/some-post", "medium.com"),
        # dev.to 按作者（栏目页不拆）
        ("https://dev.to/ben/some-post-4a2b", "dev.to/ben"),
        ("https://dev.to/top/week", "dev.to"),
        # YouTube / B 站：视频地址里没有频道，不按频道切
        ("https://www.youtube.com/watch?v=abcdefg", "youtube.com"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili.com"),
    ],
)
def test_site_of_url_big_platforms(url: str, want: str) -> None:
    assert source_name.site_of_url(url) == want


@pytest.mark.parametrize("raw", ["机核", "游研社", "触乐", "", "   ", "机核 https://gcores.com"])
def test_site_of_url_rejects_non_url(raw: str) -> None:
    assert source_name.site_of_url(raw) == ""


def test_domain_of_strips_path_and_www() -> None:
    assert source_name.domain_of("github.com/openai") == "github.com"
    assert source_name.domain_of("www.gcores.com") == "gcores.com"
    assert source_name.domain_of("https://foo.substack.com/p/x") == "foo.substack.com"
    assert source_name.domain_of("机核") == ""


def test_normalize_site_takes_url_or_label() -> None:
    assert source_name.normalize_site("https://github.com/openai/whisper") == "github.com/openai"
    assert source_name.normalize_site("github.com/openai") == "github.com/openai"
    assert source_name.normalize_site("www.gcores.com") == "gcores.com"
    assert source_name.normalize_site("") == ""


# ----------------------------------------------------------------------
# 写入路径：RSS 条目用域名，不再拿源标题当站名
# ----------------------------------------------------------------------


def test_collect_rss_uses_domain_not_feed_title(tmp_path) -> None:
    """RSS 源标题「机核」不再当 site：site 从条目链接算（gcores.com）。"""
    store, settings, feeds = _feeds(tmp_path)
    xml = _rss_xml([("机核长文一篇", "https://www.gcores.com/articles/1")], title="机核")
    feeds._rss_transport = httpx.MockTransport(lambda req: httpx.Response(200, text=xml))
    rss.add_feed(store, GID, url="https://www.gcores.com/feed", title="机核", feed_id="rG", now=NOW)
    with _TimePatch():
        items = _run(feeds._collect_rss(GID, settings))
    assert [it["site"] for it in items] == ["gcores.com"]
    assert items[0]["_rss_title"] == "机核", "源标题仍留着排查用"
    assert items[0]["from_rss"] is True
    store.close()


def test_collect_rss_github_feed_uses_owner(tmp_path) -> None:
    store, settings, feeds = _feeds(tmp_path)
    xml = _rss_xml([("Release v1", "https://github.com/openai/whisper/releases/tag/v1")], title="Releases")
    feeds._rss_transport = httpx.MockTransport(lambda req: httpx.Response(200, text=xml))
    rss.add_feed(store, GID, url="https://github.com/openai/whisper/releases.atom",
                 title="whisper releases", feed_id="rG", now=NOW)
    with _TimePatch():
        items = _run(feeds._collect_rss(GID, settings))
    assert [it["site"] for it in items] == ["github.com/openai"]
    store.close()


# ----------------------------------------------------------------------
# 旧库：中文 site 不改数据，统计按 url 重算归到域名
# ----------------------------------------------------------------------


def _legacy_row(store, url: str, *, site: str, gid: str = GID, avg: float = 4.2, days_ago: float = 1.0) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, scores, rejected, up, down, created)"
            " VALUES (1, ?, ?, ?, ?, ?, 0, 0, 0, ?)",
            (gid, url, _normalize_url(url), json.dumps([{"url": url, "site": site}]),
             json.dumps({"avg": avg}), NOW - days_ago * 86400),
        )


def test_source_stats_recomputes_legacy_chinese_site_from_url(tmp_path) -> None:
    """库里 site=「机核」的老行：统计按 url 归到 gcores.com（和别的 gcores 行合一份）。"""
    store, _settings, _feeds_obj = _feeds(tmp_path)
    _legacy_row(store, "https://www.gcores.com/articles/1", site="机核")
    _legacy_row(store, "https://gcores.com/articles/2", site="机核")
    assert source_stats.trusted_domains(store, GID, NOW) == ["gcores.com"]
    store.close()


def test_source_stats_counts_platform_by_author(tmp_path) -> None:
    """大平台（github）按仓库主人分开计数，source_prior 也用同一个名字对得上。"""
    store, _settings, _feeds_obj = _feeds(tmp_path)
    _legacy_row(store, "https://github.com/openai/whisper", site="github.com")
    _legacy_row(store, "https://github.com/openai/whisper/releases", site="github.com")
    _legacy_row(store, "https://github.com/other/repo", site="github.com")
    assert source_stats.trusted_domains(store, GID, NOW) == ["github.com/openai"]
    assert source_stats.source_prior(store, GID, "github.com/openai", NOW) > 0.0
    assert source_stats.source_prior(store, GID, "github.com/other", NOW) == 0.0
    store.close()


def test_stats_removed_domain_covers_platform_labels(tmp_path) -> None:
    """管理员移出整个域名（github.com）→ 该域名下的作者标签一起移出。"""
    store, _settings, _feeds_obj = _feeds(tmp_path)
    _legacy_row(store, "https://github.com/openai/whisper", site="github.com")
    _legacy_row(store, "https://github.com/openai/whisper/wiki", site="github.com")
    source_stats.set_removed(store, GID, "github.com", True)
    assert source_stats.trusted_domains(store, GID, NOW) == []
    store.close()


# ----------------------------------------------------------------------
# 域名级判断（屏蔽名单 / 同域名配额）仍按域名算，不被作者标签拆开
# ----------------------------------------------------------------------


def test_domain_blocked_covers_platform_label() -> None:
    assert _domain_blocked("github.com/openai", {"github.com"}) is True
    assert _domain_blocked("foo.substack.com", {"substack.com"}) is True
    assert _domain_blocked("medium.com/@someone", {"medium.com"}) is True
    assert _domain_blocked("gcores.com", {"github.com"}) is False


def test_blocked_domains_accepts_platform_label(tmp_path) -> None:
    """网页「屏蔽 <来源名>」按钮传过来的可能是 github.com/<作者>：屏蔽按域名算，存 github.com。"""
    from CharTyr_MaiWork.maiwork.feeds import blocked_domains_set

    store, _settings, _feeds_obj = _feeds(tmp_path)
    assert blocked_domains_set(store, GID, ["github.com/openai"]) == ["github.com"]
    assert blocked_domains_set(store, GID, ["github.com/openai", "medium.com/@someone", "Gcores.com"]) == [
        "gcores.com", "github.com", "medium.com",
    ]
    store.close()


def test_trusted_removed_keeps_author_label(tmp_path) -> None:
    """网页「移出」传的是作者标签：只移出这个作者，不能把整个 github.com 连坐。"""
    store, _settings, _feeds_obj = _feeds(tmp_path)
    _legacy_row(store, "https://github.com/openai/whisper", site="github.com")
    _legacy_row(store, "https://github.com/openai/whisper/wiki", site="github.com")
    _legacy_row(store, "https://github.com/other/repo", site="github.com")
    _legacy_row(store, "https://github.com/other/repo/wiki", site="github.com")
    source_stats.set_removed(store, GID, "github.com/openai", True)
    assert source_stats.removed(store, GID) == ["github.com/openai"]
    assert source_stats.trusted_domains(store, GID, NOW) == ["github.com/other"]
    store.close()


def test_prefilter_blocks_platform_label_by_domain(tmp_path) -> None:
    """屏蔽名单里写 github.com：github.com/<owner> 的候选照旧被第一道挡下。"""
    store, settings, feeds = _feeds(tmp_path)
    from CharTyr_MaiWork.maiwork.feeds import blocked_domains_set

    blocked_domains_set(store, GID, ["github.com"])

    def _cand(url: str) -> dict:
        return {"title": "新发布", "url": url, "summary": "摘要", "kind": "news",
                "published": NOW - 3600, "fetched": True, "quote": "原文依据"}

    with _TimePatch():
        kept, drops, _ = feeds._prefilter(GID, settings, [_cand("https://github.com/openai/whisper")])
    assert kept == []
    assert drops and "屏蔽" in drops[0][1]
    store.close()


def test_domain_cap_counts_by_domain_not_owner(tmp_path) -> None:
    """同域名 ≤3 仍按域名算：github.com 下 4 个不同作者，也只有 3 条能留。"""
    store, _settings, feeds = _feeds(tmp_path)
    survivors = [
        {"title": f"仓库{i}", "url": f"https://github.com/o{i}/repo", "summary": "s",
         "kind": "news", "scores": {"avg": 4.0 - i * 0.1}, "topic": f"话题{i}",
         "site": f"github.com/o{i}"}
        for i in range(4)
    ]
    feeds._dedup_homogeneous(survivors, 10)
    dropped = [it for it in survivors if it.get("reject")]
    assert len(dropped) == 1 and "同个来源" in dropped[0]["reject"][1]
    store.close()
