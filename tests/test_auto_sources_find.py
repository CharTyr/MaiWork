"""auto_sources.py：每周一次专门「找来源」的搜索（docs/10 §九 第二步剩下三项 第 2 项）。

跟来源地图同一次模型调用：模型除了列一手来源，顺带回 1~2 条「找来源」的短搜索词（`queries`），
代码拿它去搜（不占资讯搜索名额），搜到的网站当来源地图候选，照样体检（能找到 RSS、14 天内有更新、
是真文章、不是大平台）才订。

覆盖：
- `parse_map_queries`：`{"queries": […]}` → ≤FIND_QUERY_MAX 条干净搜索词（空的 / 太长的 / 重复的丢掉），
  同一个回复里 `parse_source_map` 行为不变；
- `find_source_candidates`：大平台整站不要、本群已订过的不要、进过拒绝 / 屏蔽 / 移出名单的不要、
  同一站去重、最多 FIND_CAND_MAX 条；某个词搜失败只记日志、接着搜下一个；search 没接或没有搜索词 → 空；
  作者标签（medium.com/@x、someone.substack.com、dev.to/x）用标签当名字；
- `map_sources`：搜索来的候选进来源地图（origin="search"）、验证通过的按 map 名额订上（origin="map"）、
  自己一份小验证预算（不吃模型候选那份 MAP_VALIDATE_MAX）；
- `auto_sources.run` 把 search 透传给 `map_sources`；`feedback_jobs.run` 把 search 透传给 `auto_sources.run`。

网络一律 httpx.MockTransport，不碰真实网络；搜索是假的（真搜索要 MCP，这里只验代码怎么用它）。
"""

from __future__ import annotations

import json
import logging

import pytest

from CharTyr_MaiWork.maiwork import auto_sources, rss, source_name, source_stats

# 复用 test_auto_sources.py 里的造数据帮手和 `store` fixture（仓库惯例：同目录测试模块互相导入）
from test_auto_sources import (  # noqa: F401
    GID,
    NOW,
    _Models,
    _ProfilesWithPoints,
    _profile_ready,
    _rss_doc,
    _transport,
    store,
)


class _Search:
    """假搜索：q → 结果列表（`{"url","title",…}` 形状），或异常（那个词这次搜失败）。"""

    def __init__(self, mapping: dict | None = None) -> None:
        self.mapping = dict(mapping or {})
        self.calls: list[tuple] = []

    async def search(self, q, **kw):
        self.calls.append((q, kw))
        got = self.mapping.get(q, [])
        if isinstance(got, BaseException):
            raise got
        return got


def _hit(url: str, title: str = "某篇") -> dict:
    return {"url": url, "title": title, "snippet": "", "published": None}


# ----------------------------------------------------------------------
# 一、parse_map_queries
# ----------------------------------------------------------------------


class TestParseMapQueries:
    def test_reads_queries_next_to_sources(self):
        text = json.dumps(
            {
                "sources": [{"name": "A", "url": "https://a.example/", "why": "x"}],
                "queries": ["掌机 blog", "独立游戏 newsletter"],
            },
            ensure_ascii=False,
        )
        assert auto_sources.parse_map_queries(text) == ["掌机 blog", "独立游戏 newsletter"]
        # 同一个回复里 sources 照旧解析（parse_source_map 行为不变）
        assert auto_sources.parse_source_map(text)[0]["name"] == "A"

    def test_strips_and_caps_at_two(self):
        assert auto_sources.parse_map_queries(json.dumps({"queries": [" a ", "b", "c"]})) == ["a", "b"]
        assert auto_sources.FIND_QUERY_MAX == 2

    def test_drops_empty_too_long_and_duplicates(self):
        too_long = "长" * 81
        text = json.dumps({"queries": ["", "   ", too_long, "x", "x", " y "]})
        assert auto_sources.parse_map_queries(text) == ["x", "y"]

    def test_bad_shape_gives_empty(self):
        assert auto_sources.parse_map_queries("模型胡说") == []
        assert auto_sources.parse_map_queries(json.dumps({"sources": []})) == []
        assert auto_sources.parse_map_queries(json.dumps(["q1"])) == []
        assert auto_sources.parse_map_queries(json.dumps({"queries": "不是列表"})) == []


# ----------------------------------------------------------------------
# 二、find_source_candidates
# ----------------------------------------------------------------------


class TestFindSourceCandidates:
    @pytest.mark.asyncio
    async def test_none_search_or_empty_queries_gives_empty(self, store):
        assert await auto_sources.find_source_candidates(store, GID, ["q"], search=None, now=NOW) == []
        s = _Search({})
        assert await auto_sources.find_source_candidates(store, GID, [], search=s, now=NOW) == []
        assert await auto_sources.find_source_candidates(store, GID, ["", "  "], search=s, now=NOW) == []
        assert s.calls == []

    @pytest.mark.asyncio
    async def test_entry_shape_and_search_call(self, store):
        s = _Search({"掌机 blog": [_hit("https://newblog.example.com/p/1")]})
        out = await auto_sources.find_source_candidates(store, GID, ["掌机 blog"], search=s, now=NOW)
        assert out == [
            {
                "name": "newblog.example.com",
                "url": "https://newblog.example.com/",
                "why": "找来源搜索「掌机 blog」搜到",
                "origin": "search",
            }
        ]
        # 不带天数限制（找来源不是找资讯）
        assert s.calls == [("掌机 blog", {"limit": 10})]

    @pytest.mark.asyncio
    async def test_big_platforms_are_skipped(self, store):
        s = _Search(
            {
                "q": [
                    _hit("https://www.youtube.com/watch?v=1"),
                    _hit("https://github.com/openai/x"),
                    _hit("https://www.bilibili.com/video/BV1"),
                    _hit("https://zhihu.com/question/1"),
                ]
            }
        )
        assert await auto_sources.find_source_candidates(store, GID, ["q"], search=s, now=NOW) == []

    @pytest.mark.asyncio
    async def test_already_subscribed_is_skipped(self, store):
        rss.add_feed(store, GID, url="https://known.example.com/feed", title="已知", feed_id="r1",
                     now=NOW, label="known.example.com")
        s = _Search({"q": [_hit("https://known.example.com/p/1")]})
        assert await auto_sources.find_source_candidates(store, GID, ["q"], search=s, now=NOW) == []

    @pytest.mark.asyncio
    async def test_rejected_blocked_and_removed_are_skipped(self, store):
        auto_sources.note_rejected(store, GID, label="seen.example.com", reason="删过", now=NOW)
        with store.tx() as conn:
            store.kv_set(conn, "feeds.blocked." + GID, ["blocked.example"])
        source_stats.set_removed(store, GID, "removed.example", True)
        s = _Search(
            {
                "q": [
                    _hit("https://seen.example.com/p/1"),
                    _hit("https://blocked.example/p/1"),
                    _hit("https://sub.removed.example/p/1"),
                    _hit("https://fine.example.com/p/1"),
                ]
            }
        )
        out = await auto_sources.find_source_candidates(store, GID, ["q"], search=s, now=NOW)
        assert [e["name"] for e in out] == ["fine.example.com"]

    @pytest.mark.asyncio
    async def test_dedupes_same_site_and_caps(self, store):
        hits = [_hit(f"https://s{i}.example.com/p/1") for i in range(8)]
        hits.append(_hit("https://s0.example.com/p/2", "同站另一篇"))
        s = _Search({"q": hits})
        out = await auto_sources.find_source_candidates(store, GID, ["q"], search=s, now=NOW)
        assert len(out) == auto_sources.FIND_CAND_MAX == 5
        assert [e["name"] for e in out] == [f"s{i}.example.com" for i in range(5)]

    @pytest.mark.asyncio
    async def test_one_failing_query_does_not_stop_the_others(self, store):
        s = _Search({"bad": RuntimeError("搜索炸了"), "good": [_hit("https://ok.example.com/p/1")]})
        out = await auto_sources.find_source_candidates(store, GID, ["bad", "good"], search=s, now=NOW)
        assert [e["name"] for e in out] == ["ok.example.com"]
        assert [q for q, _ in s.calls] == ["bad", "good"]

    @pytest.mark.asyncio
    async def test_author_labels_keep_the_label_as_name(self, store):
        s = _Search(
            {
                "q": [
                    _hit("https://medium.com/@someone/p/1"),
                    _hit("https://someone.substack.com/p/1"),
                    _hit("https://dev.to/somebody/x"),
                ]
            }
        )
        out = await auto_sources.find_source_candidates(store, GID, ["q"], search=s, now=NOW)
        assert [(e["name"], e["url"], e["origin"]) for e in out] == [
            ("medium.com/@someone", "https://medium.com/@someone", "search"),
            ("someone.substack.com", "https://someone.substack.com/", "search"),
            ("dev.to/somebody", "https://dev.to/somebody", "search"),
        ]
        # 作者标签是 site_of_url 认出来的（跟订阅地址那条线同一口径）
        assert source_name.site_of_url("https://medium.com/@someone/p/1") == "medium.com/@someone"

    @pytest.mark.asyncio
    async def test_results_without_url_or_unknown_host_are_skipped(self, store):
        s = _Search({"q": [_hit(""), {"title": "没有 url"}, "不是字典", None,
                           _hit("https://www.example.com/p/1")]})  # 带 www. → 域名去 www.
        out = await auto_sources.find_source_candidates(store, GID, ["q"], search=s, now=NOW)
        assert [(e["name"], e["url"]) for e in out] == [("example.com", "https://example.com/")]


# ----------------------------------------------------------------------
# 三、map_sources 接线（模型候选 + 搜索候选）
# ----------------------------------------------------------------------


class TestMapSourcesFindSearch:
    @pytest.mark.asyncio
    async def test_search_candidate_saved_with_origin_search_and_subscribed_as_map(self, store):
        _profile_ready(store)
        pages = {
            "https://good.example.com/feed": _rss_doc([("真文章", "https://good.example.com/p/1", 1)]),
            "https://found.example.com/feed": _rss_doc(
                [("搜到的文章", "https://found.example.com/post/1", 1)]
            ),
        }
        search = _Search({"掌机 博客 RSS": [_hit("https://found.example.com/post/7", "某篇")]})
        models = _Models(
            [
                json.dumps(
                    {
                        "sources": [{"name": "模型来源", "url": "https://good.example.com/", "why": "一手"}],
                        "queries": ["掌机 博客 RSS"],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        n = await auto_sources.map_sources(
            store, GID, NOW, models=models, profiles=_ProfilesWithPoints(),
            transport=_transport(pages), search=search,
        )
        assert n == 2
        saved = {s["label"]: s for s in auto_sources.load_map(store, GID)["sources"]}
        assert saved["good.example.com"]["status"] == "verified"
        assert saved["good.example.com"]["origin"] == "model"
        assert saved["found.example.com"]["status"] == "verified"
        assert saved["found.example.com"]["origin"] == "search"
        assert "找来源搜索" in saved["found.example.com"]["why"]
        feeds = {e["label"]: e for e in rss.list_feeds(store, GID)}
        assert feeds["found.example.com"]["origin"] == "map"
        assert feeds["found.example.com"]["auto"] is True
        # 搜索词是在同一次模型调用里要的
        assert '"queries"' in models.calls[0][1][0]["content"]

    @pytest.mark.asyncio
    async def test_search_candidates_have_their_own_validate_budget(self, store, monkeypatch):
        _profile_ready(store)
        # 模型候选正好把 MAP_VALIDATE_MAX 这份预算用满：MAP_MAX 条死站 + 把预算调成同一个数
        srcs = [
            {"name": f"死站{i}", "url": f"https://dead{i}.example.com/", "why": "打不开"}
            for i in range(auto_sources.MAP_MAX)
        ]
        monkeypatch.setattr(auto_sources, "MAP_VALIDATE_MAX", len(srcs))
        pages = {
            "https://found.example.com/feed": _rss_doc(
                [("搜到的文章", "https://found.example.com/post/1", 1)]
            )
        }
        search = _Search({"找来源": [_hit("https://found.example.com/post/7")]})
        models = _Models([json.dumps({"sources": srcs, "queries": ["找来源"]}, ensure_ascii=False)])
        await auto_sources.map_sources(
            store, GID, NOW, models=models, profiles=_ProfilesWithPoints(),
            transport=_transport(pages), search=search,
        )
        saved = {s["label"]: s for s in auto_sources.load_map(store, GID)["sources"]}
        # 模型候选把 MAP_VALIDATE_MAX 用满（全 rejected）
        assert len(srcs) == auto_sources.MAP_MAX
        assert all(saved[f"dead{i}.example.com"]["status"] == "rejected" for i in range(len(srcs)))
        # 搜索候选另有自己的预算：照样验到、订上
        assert saved["found.example.com"]["status"] == "verified"
        assert any(e["label"] == "found.example.com" for e in rss.list_feeds(store, GID))

    @pytest.mark.asyncio
    async def test_one_info_log_line_per_run(self, store, caplog):
        _profile_ready(store)
        pages = {
            "https://found.example.com/feed": _rss_doc(
                [("搜到的文章", "https://found.example.com/post/1", 1)]
            )
        }
        search = _Search({"找来源": [_hit("https://found.example.com/post/7")]})
        models = _Models([json.dumps({"sources": [], "queries": ["找来源"]})])
        with caplog.at_level(logging.INFO, logger="maiwork.auto_sources"):
            await auto_sources.map_sources(
                store, GID, NOW, models=models, profiles=_ProfilesWithPoints(),
                transport=_transport(pages), search=search,
            )
        lines = [r.getMessage() for r in caplog.records if "找来源搜索" in r.getMessage()]
        assert len(lines) == 1
        assert "搜 1 次" in lines[0] and "新网站 1 个" in lines[0] and "验证通过 1 个" in lines[0]

    @pytest.mark.asyncio
    async def test_search_not_given_still_maps_model_entries(self, store):
        """search=None（老调用方 / 没配搜索）：模型那条线照旧，不报错、不发搜索。"""
        _profile_ready(store)
        pages = {
            "https://good.example.com/feed": _rss_doc([("真文章", "https://good.example.com/p/1", 1)])
        }
        models = _Models(
            [json.dumps({"sources": [{"name": "A", "url": "https://good.example.com/", "why": "x"}],
                         "queries": ["掌机 blog"]}, ensure_ascii=False)]
        )
        n = await auto_sources.map_sources(
            store, GID, NOW, models=models, profiles=_ProfilesWithPoints(),
            transport=_transport(pages),
        )
        assert n == 1
        saved = auto_sources.load_map(store, GID)["sources"]
        assert [s["label"] for s in saved] == ["good.example.com"]


# ----------------------------------------------------------------------
# 四、search 透传：auto_sources.run → map_sources；feedback_jobs.run → auto_sources.run
# ----------------------------------------------------------------------


class TestSearchPlumbing:
    @pytest.mark.asyncio
    async def test_run_passes_search_to_map_sources(self, store, monkeypatch):
        _profile_ready(store)
        seen: dict = {}

        async def fake_map(store_, gid_, now_, *, models=None, profiles=None, transport=None, search=None):
            seen["search"] = search
            return 0

        monkeypatch.setattr(auto_sources, "map_sources", fake_map)
        sentinel = object()
        models = _Models([json.dumps({"fit": []})])
        await auto_sources.run(
            store, models, GID, NOW, profiles=_ProfilesWithPoints(),
            transport=_transport({}), search=sentinel,
        )
        assert seen["search"] is sentinel

    @pytest.mark.asyncio
    async def test_feedback_jobs_forwards_search_to_auto_sources(self, store, monkeypatch):
        from CharTyr_MaiWork.maiwork import feedback_jobs

        seen: dict = {}

        async def fake_auto(store_, models_, gid_, now_, *, profiles=None, transport=None, search=None):
            seen["search"] = search
            return {"subscribed": 0, "unsubscribed": 0, "map": 0, "push": 0, "skipped": ""}

        monkeypatch.setattr(auto_sources, "run", fake_auto)
        sentinel = object()
        await feedback_jobs.run(
            store, _Models([], ready=False), GID, NOW,
            profiles=_ProfilesWithPoints(), search=sentinel,
        )
        assert seen["search"] is sentinel
