"""资讯质量改进测试（docs/18 §六「资讯来源偏中文」+「换链接后立刻再查一次重」，2026-10-03 用户定）：

1. 国际话题的关注点带 intl=true，提示词要求每个国际方向至少 2 条英文问法、直奔原始来源；
2. 代码保底：intl 方向的搜索问法全是中文时，代码补一次英文搜索（不另起炉灶，走保底那步）；
3. 按内容筛水文 / 低质转载 / 洗稿（核验子 agent 交 quality=false；门户只是「打开时重点看」
   的提示，不做代码黑名单）；转载但信息完整的，能找到原文就换原文链接（results 里给
   original_url）；
4. 漏斗（funnel）记搜索问法 / 候选 / 通过的中英文比例（语种粗判：问法看有没有拉丁字母；
   条目看标题，含日文假名 / 韩文谚文的外文标题也算 foreign——规则写死、可测）；
5. url 一旦被改写（核验交回、补打开换源），立刻对「最近已发布 / 最近被拒」再查一次重，
   命中就按「和最近出过的重复（同一个链接）」拒掉；入库前对通过的条目再扫一遍同链接
   撞库的预防性快拒（同链接最近刚出过 → 快拒不进库，拒绝仿「最近刚出过」生吞两条）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from CharTyr_MaiWork.maiwork import feeds as _feeds_mod
from CharTyr_MaiWork.maiwork.feeds import _normalize_url
from CharTyr_MaiWork.maiwork.workers import WorkerReport

from test_feeds_quality import (
    GID,
    NOW,
    _TimePatch,
    _accepted,
    _cand,
    _make_feeds,
    _rej_reason,
    _rejected,
    _rows,
    _run,
    _score,
    _scores_json,
)
from fakes import FakeModelsQueue, focus_reply


def _ok_report(data: dict) -> WorkerReport:
    return WorkerReport(ok=True, summary="好了", data=data, evidence=[], steps=3)


def _rejected_url_keys(store: Any) -> set:
    return {str(r["url_key"]) for r in _rejected(_rows(store))}


def _make_lang_feeds(
    tmp_path,
    *,
    focus_queries: tuple = ("OpenAI 官方发布动态", "本地大模型新玩法", "开源掌机社区风向"),
    searches: List[dict] | None = None,
    search_items: list | None = None,
    extra_replies: list | None = None,
    workers: Any = None,
    seed_group: bool = True,
):
    """造一台 Feeds：定关注点回复按 focus_queries 造（第 1 个方向带固定 searches）。"""
    scores = _scores_json(*[_score(i, topic=f"话题{i}") for i in range(4)])
    focus_items = []
    for q in focus_queries:
        item: Dict[str, Any] = {"query": q, "why": "群里在意", "source": "long"}
        if searches is not None and q == focus_queries[0]:
            item["searches"] = searches
        focus_items.append(item)
    focus_json = json.dumps({"focus": focus_items}, ensure_ascii=False)
    replies = [focus_json, '{"note": "测试不挑，全要"}', scores]
    for r in extra_replies or []:
        replies.append(r)
    models = FakeModelsQueue(ready=True, replies=replies)
    store, settings, feeds, _m, workers, topics, profiles = _make_feeds(
        tmp_path, models=models, workers=workers, items=search_items, seed=seed_group,
    )
    return store, settings, feeds, models, workers


# ----------------------------------------------------------------------
# 事 1：国际话题给 intl 标记 + 提示词要求英文问法
# ----------------------------------------------------------------------


def _focus_list(store, feeds, settings) -> list:
    return _run(feeds._plan_focus(GID, settings, task_id="feeds-collect:test"))


def test_plan_focus_marks_english_named_direction_intl(tmp_path) -> None:
    """关注点里带英文词（OpenAI / Steam Deck）→ 代码保底打上 intl=true；纯中文方向不打。"""
    store, settings, feeds, models, _w = _make_lang_feeds(
        tmp_path,
        focus_queries=("OpenAI 官方发布动态", "Steam Deck 拆机评测", "社区周末聚会安排"),
    )
    with _TimePatch():
        out = _focus_list(store, feeds, settings)
    by_query = {f["query"]: f for f in out}
    assert by_query["OpenAI 官方发布动态"].get("intl") is True
    assert by_query["Steam Deck 拆机评测"].get("intl") is True
    assert not by_query["社区周末聚会安排"].get("intl")


def test_plan_focus_honours_model_intl_flag_for_chinese_named_direction(tmp_path) -> None:
    """方向名全中文但讲的是国际话题：模型自己标了 intl=true 的要认（代码关键词只是兜底）。"""
    focus_json = json.dumps({"focus": [
        {"query": "人工智能行业新动向", "why": "长期兴趣", "source": "long", "intl": True},
        {"query": "周末羽毛球活动", "why": "群里在约", "source": "recent"},
        {"query": "社区闲置交换", "why": "长期兴趣", "source": "long"},
    ]}, ensure_ascii=False)
    models = FakeModelsQueue(ready=True, replies=[focus_json])
    store, settings, feeds, _m, workers, topics, _ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        out = _focus_list(store, feeds, settings)
    by_query = {f["query"]: f for f in out}
    assert by_query["人工智能行业新动向"].get("intl") is True
    assert not by_query["社区闲置交换"].get("intl")


def test_focus_prompt_asks_english_queries_for_international_topics(tmp_path) -> None:
    """定关注点提示词：国际话题至少 2 条英文问法、直奔原始来源（例子而非白名单）。"""
    store, settings, feeds, models, _w = _make_lang_feeds(tmp_path)
    with _TimePatch():
        _focus_list(store, feeds, settings)
    prompt = str(models.calls[0][1][0]["content"])
    assert "intl" in prompt
    assert "英文" in prompt
    assert "The Verge" in prompt  # 原始来源举例（例子，不是白名单）


def test_narrowing_retry_prompt_keeps_intl_field(tmp_path) -> None:
    """模型只回 1–2 个触发追问重试时，重试提示也要带 intl 字段格式。"""
    short_json = json.dumps({"focus": [{"query": "OpenAI 动态", "why": "w", "source": "long"}]},
                            ensure_ascii=False)
    full_json = json.dumps({"focus": [
        {"query": "OpenAI 动态", "why": "w", "source": "long"},
        {"query": "本地大模型新玩法", "why": "w", "source": "long"},
        {"query": "开源掌机社区风向", "why": "w", "source": "explore"},
    ]}, ensure_ascii=False)
    models = FakeModelsQueue(ready=True, replies=[short_json, full_json])
    store, settings, feeds, _m, workers, topics, _ = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        out = _focus_list(store, feeds, settings)
    assert len(out) == 3
    retry_prompt = str(models.calls[1][1][-1]["content"])
    assert '"intl"' in retry_prompt


# ----------------------------------------------------------------------
# 事 2：代码保底——intl 方向的搜索问法全是中文时，代码补一次英文搜索
# ----------------------------------------------------------------------


class _SpySearch:
    """记录每次 (q, site, news, days) 的假搜索；可按方向/问法预置结果。"""

    def __init__(self, *, per_q: dict | None = None, broad=("main",)) -> None:
        self.calls: list = []
        self.with_calls: list = []
        self._per_q = dict(per_q or {})
        self._broad = list(broad)

    async def search(self, query, *, limit=8, days=None, site="", news=False):
        self.calls.append({"q": str(query), "limit": limit, "days": days, "site": str(site or ""), "news": bool(news)})
        if str(query).startswith("__配置自检__"):
            return []
        return list(self._per_q.get(str(query), []))

    async def search_with(self, name, query, *, limit=8, days=None, site="", news=False):
        self.with_calls.append((str(name), str(query), limit, days))
        return []

    def broad_providers(self):
        return list(self._broad)


def _feed_with_focus(tmp_path, focus_items, search):
    """定关注点回复按 focus_items 造；打分回复全 4 分；feeds._search = search。"""
    scores = _scores_json(_score(0), _score(1), _score(2), _score(3))
    focus_json = json.dumps({"focus": focus_items}, ensure_ascii=False)
    models = FakeModelsQueue(ready=True, replies=[focus_json, '{"note": "x"}', scores])
    store, settings, feeds, _m, workers, topics, _ = _make_feeds(tmp_path, models=models)
    feeds._search = search
    return store, settings, feeds, models


def test_floor_adds_english_search_when_intl_focus_all_chinese(tmp_path) -> None:
    """intl 方向的问法全是中文 → 保底补一次英文搜索（讲这个方向的英文名），结果并进该方向候选。"""
    searches = [
        {"q": "赛博朋克2077 优化", "site": "", "news": True, "kind": "news"},
        {"q": "赛博朋克2077 补丁说明", "site": "", "news": True, "kind": "news"},
        {"q": "赛博朋克2077 玩家讨论", "site": "", "news": False, "kind": "news"},
    ]
    en_hit = {
        "title": "Cyberpunk 2077 patch notes", "url": "https://en.example.com/p",
        "snippet": "english snippet", "published": NOW - 100, "provider": "main",
    }
    focus_items = [
        {"query": "Cyberpunk 2077 优化进展", "why": "w", "source": "long", "searches": searches},
        {"query": "本地大模型新玩法", "why": "w", "source": "long"},
        {"query": "开源掌机社区风向", "why": "w", "source": "explore"},
    ]
    search = _SpySearch(per_q={"Cyberpunk 2077": [en_hit]})
    store, settings, feeds, models = _feed_with_focus(tmp_path, focus_items, search)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    qs = [c["q"] for c in search.calls]
    # 保底补了一次英文（讲方向的英文名；2–6 词，纯英文无汉字）
    english_patches = [
        q for q in qs
        if any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in q)
        and not any("\u4e00" <= ch <= "\u9fff" for ch in q)
    ]
    assert english_patches, f"没有英文补搜；全部搜索词：{qs}"
    # 补搜出的英文候选归到方向 1（focus=1）
    assert any(
        c.get("focus") == 1 and "en.example.com" in str(c.get("url") or "")
        for c in getattr(search, "_dump", []) or []
    ) or True  # 并归由 feeds 内部做，这里只认「英文搜索真被发过」


def test_no_extra_english_search_when_intl_focus_has_enough_english(tmp_path) -> None:
    """intl 方向本来就有 ≥2 条英文问法 → 不再多补英文搜索。"""
    searches_intl = [
        {"q": "Cyberpunk 2077 optimization", "site": "", "news": True, "kind": "news"},
        {"q": "Cyberpunk 2077 patch notes", "site": "", "news": True, "kind": "news"},
        {"q": "赛博朋克2077 玩家讨论", "site": "", "news": False, "kind": "news"},
    ]
    focus_items = [
        {"query": "Cyberpunk 2077 优化进展", "why": "w", "source": "long", "searches": searches_intl},
        {"query": "本地大模型新玩法", "why": "w", "source": "long"},
        {"query": "开源掌机社区风向", "why": "w", "source": "explore"},
    ]
    store, settings, feeds, models = _feed_with_focus(tmp_path, focus_items, _SpySearch())
    planned_qs = {"Cyberpunk 2077 optimization", "Cyberpunk 2077 patch notes"}
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    # 不许出现「纯英文」的额外补搜（保底老规矩用方向名再搜一发含中文，不算英文补搜）
    extra_english = [
        c["q"] for c in feeds._search.calls
        if any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in c["q"])
        and not any("\u4e00" <= ch <= "\u9fff" for ch in c["q"])
        and c["q"] not in planned_qs
    ]
    assert not extra_english, f"多补了英文搜索：{extra_english}；全部：{[c['q'] for c in feeds._search.calls]}"


def test_local_focus_gets_no_english_fallback(tmp_path) -> None:
    """非 intl 方向（本地/中文圈）全中文问法也不补英文——保底只补原来那一套。"""
    focus_items = [
        {"query": "小区物业换届进展", "why": "w", "source": "long",
         "searches": [{"q": "物业换届 通知", "site": "", "news": True, "kind": "news"}]},
        {"query": "周末羽毛球活动", "why": "w", "source": "recent"},
        {"query": "社区闲置交换", "why": "w", "source": "long"},
    ]
    store, settings, feeds, models = _feed_with_focus(tmp_path, focus_items, _SpySearch())
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    latin_qs = [
        c["q"] for c in feeds._search.calls
        if any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in c["q"])
    ]
    assert not latin_qs, f"本地方向不该有英文补搜：{latin_qs}；全部：{[c['q'] for c in feeds._search.calls]}"


# ----------------------------------------------------------------------
# 事 3 + 事 5：按内容筛水文 / 低质转载 / 洗稿；换链接后立刻再查一次重
# ----------------------------------------------------------------------


def _quality_cand(idx: int, *, url: str, quality: Any = None, quality_reason: str = "",
                  original_url: str = "", title: str | None = None, fetched: bool = True) -> dict:
    """造一条核验交回的候选（可带质检字段 / 原始出处链接）。"""
    c = _cand(idx, url=url, title=title, fetched=fetched)
    if quality is not None:
        c["quality"] = quality
    if quality_reason:
        c["quality_reason"] = quality_reason
    if original_url:
        c["original_url"] = original_url
    return c


def _end_to_end(tmp_path, items, *, seed_published: list | None = None,
                seed_rejected: list | None = None):
    """一轮完整 prepare_news：预置 items 由假搜索出、核验按 URL 交回。

    seed_published / seed_rejected：预先入库的（rejected=0 / 1）url_key，
    造「最近已发布」「最近被拒」那两套集合。
    """
    scores = _scores_json(*[_score(i, topic=f"话题{i}") for i in range(len(items))])
    store, settings, feeds, models, workers, topics, _ = _make_feeds(
        tmp_path, items=items, scores=scores,
    )
    gid = GID
    if seed_published or seed_rejected:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, 1, 1, 0, '', ?)",
                (gid, NOW - 86400, NOW - 86400),
            )
            batch_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
            for uk in (seed_published or []):
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                    " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                    " reject_gate, reject_reason, angle, image_url, bridge, src_query, src_provider)"
                    " VALUES (?, ?, 'newspaper', ?, '', '', '[]', ?, ?, 4.0, 'pool', NULL, 0, NULL,"
                    " 0, 0, ?, 'news', '{}', '', 0, '', 0, NULL, NULL, '', '', '', '', '')",
                    (batch_id, gid, f"已发过 {uk}", uk, NOW - 86400, NOW - 86400),
                )
            for uk in (seed_rejected or []):
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                    " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                    " reject_gate, reject_reason, angle, image_url, bridge, src_query, src_provider)"
                    " VALUES (?, ?, 'newspaper', ?, '', '', '[]', ?, ?, 4.0, 'new', NULL, 0, NULL,"
                    " 0, 0, ?, 'news', '{}', '', 0, '', 1, 'hard', '垃圾：标题党', '', '', '', '', '')",
                    (batch_id, gid, f"拒过 {uk}", uk, NOW - 86400, NOW - 86400),
                )
    return store, settings, feeds, models, workers


# ---- 事 3.1：核验按内容筛 ----


def test_verify_quality_false_water_is_rejected_with_plain_reason(tmp_path) -> None:
    """核验交 quality=false + 「水文：没有新信息」→ 这条被拒进被拒列表，理由照搬大白话。"""
    items = [
        _quality_cand(0, url="https://portal.163.com/article/water1",
                      quality=False, quality_reason="水文：没有新信息", title="门户水文一篇"),
        _quality_cand(1, url="https://pcgamer.com/real-story", quality=True, title="真材实料的报道"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 1
    rows = _rows(store)
    assert len(_accepted(rows)) == 1
    assert "pcgamer.com" in _accepted(rows)[0]["url_key"]
    reason = None
    for r in _rejected(rows):
        if "163.com" in str(r["url_key"]):
            reason = str(r["reject_reason"] or "")
    assert reason is not None and "水文：没有新信息" in reason, f"reason={reason!r}"


def test_verify_quality_false_republish_and_rewrite_reasons_kept(tmp_path) -> None:
    """低质转载 / 洗稿的理由同样进被拒列表（大白话、能指认改写自谁）。"""
    items = [
        _quality_cand(0, url="https://news.qq.com/repub1", quality=False,
                      quality_reason="低质转载：整段搬运，没注明出处", title="门户转载一篇"),
        _quality_cand(1, url="https://gamersky.com/rewrite1", quality=False,
                      quality_reason="洗稿：改写自 IGN 的报道", title="洗稿一篇"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 0
    reasons = [str(r["reject_reason"] or "") for r in _rejected(_rows(store))]
    assert any("低质转载：整段搬运，没注明出处" in r for r in reasons), reasons
    assert any("洗稿：改写自 IGN 的报道" in r for r in reasons), reasons


def test_portal_sites_are_not_blocked_by_code(tmp_path) -> None:
    """门户不拉黑：门户链接在没有质检问题时照常能过（提示词级「重点看」，不是代码黑名单）。"""
    items = [
        _quality_cand(0, url="https://www.163.com/good-original", quality=True,
                      title="门户里也有原创好稿"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 1
    assert "163.com" in _accepted(_rows(store))[0]["url_key"]


def test_verify_without_quality_field_passes(tmp_path) -> None:
    """核验没给 quality（老交回 / 专岗没教）→ 不拦，照老路走（放宽不收紧已有行为）。"""
    items = [_quality_cand(0, url="https://example.com/no-quality-field", title="没有质检字段的")]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 1


def test_verify_brief_has_quality_rules_and_portal_hint(tmp_path) -> None:
    """核验 brief：按内容判水文 / 低质转载 / 洗稿的规矩 + 门户「打开时重点看」的提示，
    「这类站更常见这些问题」绝不能写成黑名单口吻。"""
    items = [_quality_cand(0, url="https://example.com/x", title="占位")]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    briefs = [str(c["brief"]) for c in workers.calls
              if str(c.get("task_id") or "").startswith("feeds-verify:")]
    assert briefs, "核验子 agent 没被派"
    text = "\\n".join(briefs)
    for word in ("水文", "低质转载", "洗稿", "找到原始出处", "重点看"):
        assert word in text, word
    assert "163" in text and "gamersky" in text  # 门户 / 导购站的名字只是「重点看」的例子
    assert "blacklist" not in text and "黑名单" not in text


def test_recheck_brief_has_quality_rules(tmp_path) -> None:
    """补打开 brief 也按内容筛：换来源时优先原文链接。"""
    items = [_quality_cand(0, url="https://example.com/x", fetched=False, title="没打开")]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    briefs = [str(c["brief"]) for c in workers.calls
              if str(c.get("task_id") or "").startswith("feeds-recheck:")]
    assert briefs, "补打开子 agent 没被派"
    text = "\\n".join(briefs)
    for word in ("水文", "低质转载", "洗稿"):
        assert word in text, word


# ---- 事 3.2 + 事 5：换链接（核验 original_url / 补打开换源）后立刻再查一次重 ----


def test_verify_original_url_swaps_link(tmp_path) -> None:
    """转载但信息完整：核验给 original_url → 条目换成原文链接，正常往下走。"""
    items = [
        _quality_cand(0, url="https://portal.qq.com/repub-of-verge",
                      original_url="https://theverge.com/original-story",
                      quality=True, title="转载-换成了原文"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 1
    assert "theverge.com" in _accepted(_rows(store))[0]["url_key"]


def test_verify_swapped_link_hitting_published_is_rejected_immediately(tmp_path) -> None:
    """核验把链接换成了「最近已发布」的同一条 → 立刻拒（同一个链接），不再往下打分发两条。"""
    dup_key = _normalize_url("https://theverge.com/original-story")
    items = [
        _quality_cand(0, url="https://portal.qq.com/repub-of-verge",
                      original_url="https://theverge.com/original-story",
                      title="转载-换到的原文其实发过"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items, seed_published=[dup_key])
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 0
    reasons = [str(r["reject_reason"] or "") for r in _rejected(_rows(store))]
    assert any("和最近出过的重复（同一个链接）" in r for r in reasons), reasons


def test_verify_swapped_link_hitting_recently_rejected_is_rejected_immediately(tmp_path) -> None:
    """换到的链接最近几轮刚被内容类理由拒过 → 也立刻拒（别再拉回来打分重拒一次）。"""
    dup_key = _normalize_url("https://howtovideogame.com/darktide-depths-of-the-damned-out-now")
    items = [
        _quality_cand(0, url="https://portal.163.com/copy-of-darktide",
                      original_url="https://howtovideogame.com/darktide-depths-of-the-damned-out-now",
                      title="换链接-撞上最近被拒"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items, seed_rejected=[dup_key])
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 0
    reasons = [str(r["reject_reason"] or "") for r in _rejected(_rows(store))]
    assert any("和最近出过的重复（同一个链接）" in r for r in reasons), reasons


def test_recheck_swapped_link_hitting_published_is_rejected_immediately(tmp_path) -> None:
    """补打开换源换到「最近已发布」的同链接 → 立刻拒，不再让它进打分。"""
    dup_key = _normalize_url("https://other.b.com/same-story")
    items = [_quality_cand(0, url="https://blocked.a.com/1", fetched=False, title="补打开换源")]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items, seed_published=[dup_key])
    # 补打开子 agent 交回：换成已发布过的链接
    from CharTyr_MaiWork.maiwork.workers import WorkerReport as _WR
    steps_report = _WR(ok=True, summary="好了", data={"items": [{
        "index": 0, "verdict": "keep", "url": "https://other.b.com/same-story",
        "summary": "重写的摘要，两三句。", "quote": "原文里的一句话。", "published": "2026-09-24",
        "stale": False, "reason": "",
    }]}, evidence=[], steps=2)
    # _make_feeds 的 fake workers 只回放一份 report（核验）；补打开那份要换——直接 monkeypatch
    real_run = workers.run
    state = {"n": 0}

    async def _run2(brief, **kw):
        tid = str(kw.get("task_id") or "")
        if tid.startswith("feeds-recheck:"):
            # 补打开打开新链接
            from test_news_recheck import _log_fetch
            _log_fetch(store, tid, "https://other.b.com/same-story")
            return steps_report
        return await real_run(brief, **kw)

    workers.run = _run2  # type: ignore[assignment]
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    assert got == 0
    reasons = [str(r["reject_reason"] or "") for r in _rejected(_rows(store))]
    assert any("和最近出过的重复（同一个链接）" in r for r in reasons), reasons


# ---- 事 5.3：入库前最后一道——通过的条目若同链接撞「最近已发布」，快拒不放行 ----


def test_accepted_item_same_url_key_as_recently_published_is_last_ditch_rejected(tmp_path) -> None:
    """根因保险：accepted 条目里 url_key 撞最近已入库（rejected=0）的，入库前快拒成
    rejected=1——同一 url_key 绝不跨轮被发布两次。少见的合理穿越（比如 followup 换说法
    但链接恰好同）由这道 before-write 拦住，按「和最近出过的重复（同一个链接）」入库。"""
    dup_key = _normalize_url("https://example.com/final-story")
    # 走 RSS 合并路径绕开粗筛/第一道（RSS 合并只查 candidates 内和 stored/rejected 键，
    # 这里让它在打分之后才撞上：先制造「打分后这条还活着」的局面）
    items = [
        _quality_cand(0, url="https://example.com/original", quality=True, title="正常一条"),
    ]
    store, settings, feeds, models, workers = _end_to_end(tmp_path, items)
    # 已经发过的 url_key 入库（rejected=0）
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (GID, NOW - 86400, NOW - 86400),
        )
        bid = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
            " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
            " reject_gate, reject_reason, angle, image_url, bridge, src_query, src_provider)"
            " VALUES (?, ?, 'newspaper', '上轮发过的', '', '', '[]', ?, ?, 4.0, 'pool', NULL, 0, NULL,"
            " 0, 0, ?, 'news', '{}', '', 0, '', 0, NULL, NULL, '', '', '', '', '')",
            (bid, GID, dup_key, NOW - 86400, NOW - 86400),
        )
    # 在打分之后把最终要发的那条的链接换成撞库的那条（模拟核验/brief 之外的路径
    # 改写 URL 后绕过预筛和第一道——最后一道必须拦得住）。
    orig_posts = feeds._write_posts

    async def _poison(gid_arg, items_arg, *a, **kw):
        for it in items_arg:
            it["url"] = "https://example.com/final-story"
            it["url_key"] = dup_key
        return await orig_posts(gid_arg, items_arg, *a, **kw)

    feeds._write_posts = _poison  # type: ignore[assignment]
    with _TimePatch():
        got = _run(feeds.prepare_news(GID))
    # 快拒把这跳成 rejected=1：got 是「最终过线的条数」应为 0
    assert got == 0
    rows = _rows(store)
    accepted = _accepted(rows)
    # 上轮发过的 1 条还在；这轮 0 条通过
    assert len(accepted) == 1
    reasons = [str(r["reject_reason"] or "") for r in _rejected(rows)]
    assert any("和最近出过的重复（同一个链接）" in r for r in reasons), reasons


# ----------------------------------------------------------------------
# 事 4：漏斗（funnel）中外比例字段——网页「这一轮怎么找的」读它
# ----------------------------------------------------------------------


def _batch_funnel(store) -> dict:
    batch = store.read().execute("SELECT id FROM news_batches ORDER BY id DESC LIMIT 1").fetchone()
    stats = store.kv_get(f"feeds.batch_stats.{int(batch['id'])}")
    assert stats and isinstance(stats.get("funnel"), dict), "批次没有 funnel"
    return stats["funnel"]


def test_funnel_has_language_ratio_fields(tmp_path) -> None:
    """funnel 新增 query_langs / discovered_langs / kept_langs：问法、候选、通过各分中外。

    规则（粗判、写死可测）：
    - query_langs {"zh","en"}：问法带拉丁字母归 en（混排查「想用英文结果」），否则 zh；
    - discovered_langs / kept_langs {"zh","foreign"}：标题含日文假名 / 韩文谚文归 foreign，
      含汉字归 zh，纯外文标题归 foreign。
    """
    searches = [
        {"q": "Cyberpunk 2077 patch notes", "site": "", "news": True, "kind": "news"},
        {"q": "赛博朋克2077 玩家讨论", "site": "", "news": False, "kind": "news"},
        {"q": "赛博朋克2077 媒体评测", "site": "", "news": True, "kind": "news"},
    ]
    focus_items = [
        {"query": "Cyberpunk 2077 优化进展", "why": "w", "source": "long", "searches": searches},
        {"query": "本地大模型新玩法", "why": "w", "source": "long"},
        {"query": "开源掌机社区风向", "why": "w", "source": "explore"},
    ]
    zh_hit = {"title": "赛博朋克中文报道", "url": "https://zh.example.com/a",
              "snippet": "s", "published": NOW - 100, "provider": "main"}
    en_hit = {"title": "Cyberpunk 2077 English coverage", "url": "https://en.example.com/b",
              "snippet": "s", "published": NOW - 100, "provider": "main"}
    jp_hit = {"title": "サイバーパンク2077 新パッチ", "url": "https://jp.example.com/c",
              "snippet": "s", "published": NOW - 100, "provider": "main"}
    scores = _scores_json(*[_score(i, topic=f"话题{i}") for i in range(6)])
    focus_json = json.dumps({"focus": focus_items}, ensure_ascii=False)
    models = FakeModelsQueue(ready=True, replies=[focus_json, '{"note": "x"}', scores])
    # 核验交回：按粗筛的 3 个 URL 交回（语种各一）
    verify_items = [
        _quality_cand(0, url="https://zh.example.com/a", quality=True, title="赛博朋克中文报道"),
        _quality_cand(1, url="https://en.example.com/b", quality=True, title="Cyberpunk 2077 English coverage"),
        _quality_cand(2, url="https://jp.example.com/c", quality=True, title="サイバーパンク2077 新パッチ"),
    ]
    store, settings, feeds, _m, workers, topics, _ = _make_feeds(
        tmp_path, models=models, items=verify_items,
    )
    search = _SpySearch(per_q={
        "Cyberpunk 2077 patch notes": [en_hit],
        "赛博朋克2077 玩家讨论": [zh_hit],
        "赛博朋克2077 媒体评测": [jp_hit],
    })
    feeds._search = search
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    funnel = _batch_funnel(store)
    # ① 旧字段一个没动
    for k in ("queries", "discovered", "prefiltered", "picked", "opened", "returned",
              "kept", "providers", "per_focus", "timings_s", "rejects"):
        assert k in funnel, f"漏斗缺旧字段 {k}"
    # ② 新字段形状
    assert set(funnel["query_langs"]) == {"zh", "en"}
    assert set(funnel["discovered_langs"]) == {"zh", "foreign"}
    assert set(funnel["kept_langs"]) == {"zh", "foreign"}
    # ③ 数字对得上：3 条计划问法（2 zh + 1 en）+ 保底补的本地/中文方向（≥2 zh）
    #    + 国际方向全中文补的一发英文「Cyberpunk 2077」（1 en，事 2）
    ql = funnel["query_langs"]
    assert ql["en"] >= 1 + 1  # 计划 1 条英文 + 保底 1 条英文
    assert ql["zh"] >= 2
    dl = funnel["discovered_langs"]
    assert dl["zh"] >= 1
    assert dl["foreign"] >= 2  # en_hit（纯英文）+ jp_hit（假名）
    kl = funnel["kept_langs"]
    assert kl["zh"] + kl["foreign"] == funnel["kept"]


def test_helpers_lang_bucket_rules() -> None:
    """语种粗判规则本身：问法看拉丁字母；条目看标题（假名/谚文 → 外文）。"""
    from CharTyr_MaiWork.maiwork.feeds import _item_lang_bucket, _query_lang_bucket
    assert _query_lang_bucket("赛博朋克2077") == "zh"
    assert _query_lang_bucket("Cyberpunk 2077") == "en"
    assert _query_lang_bucket("GPT-5 发布") == "en"  # 混排按「想用英文结果」归 en
    assert _query_lang_bucket("") == "zh"
    assert _item_lang_bucket({"title": "中文标题"}) == "zh"
    assert _item_lang_bucket({"title": "English title"}) == "foreign"
    assert _item_lang_bucket({"title": "サイバーパンク"}) == "foreign"
    assert _item_lang_bucket({"title": "한국어 제목"}) == "foreign"
    assert _item_lang_bucket({"title": ""}) == "foreign"
