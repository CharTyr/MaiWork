"""垃圾候选粗筛 + 搜索词要短（2026-09-30 线上实测后调整）。

线上实录（群 900000001，2026-09-30 14:19 那批）：
- 搜「生化危机9」搜出维基百科「9 (2009 animated film)」和「9 (disambiguation)」；
- 搜出来「Archive for September 2026 - Page 24 | The Verge」（归档列表页）；
- 「Sony 388 港币定价」靠数字 388 撞出一条加密币被盗新闻。
- 关注点被整段拿来当搜索词（关键词串烧：「鬼武者 剑之道 首发解锁 豪华版提前游玩
  通关评价 战斗系统解析」）。

修两处：
C1. 粗筛（_prefilter，不调模型）：
    - 百科 / 词典 / 资料站（wikipedia、wiktionary、baike.baidu 等），以及标题看着就是
      消歧义 / disambiguation 的，丢——理由「百科词条或资料页」；
    - 归档 / 列表页：/archives/…、/archive/2026、/tag(s)/…、/category/…、/page/N、
      纯日期路径（/2026/09/），标题「Archive for …」开头；丢——理由「归档或列表页」。
    网站首页 / 栏目页那条老规则（_is_listing_url）原样不动。
C2. 「怎么搜」一段（_search_guide_section，撒网 / 老路 brief 都带它）：搜索词要短——
    一个核心事物 + 一个角度，大约 2–6 个词；绝不把整条关注点原样当搜索词。
    给一组清楚的好 / 坏例子。
"""

from __future__ import annotations

from test_feeds_quality import GID, NOW, _TimePatch
from test_feeds_two_phase import _candidate, _ready_two_phase_feeds


# ----------------------------------------------------------------------
# C1 粗筛：百科 / 资料页
# ----------------------------------------------------------------------


def test_prefilter_drops_encyclopedia_pages(tmp_path) -> None:
    """wikipedia（含各级语言子域）、wiktionary、baike.baidu 的候选一律丢。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://en.wikipedia.org/wiki/9_(2009_animated_film)",
                       title="9 (2009 animated film) - Wikipedia", published=NOW - 100),
            _candidate("https://zh.wikipedia.org/wiki/9", title="9 - 维基百科", published=NOW - 100),
            _candidate("https://en.wiktionary.org/wiki/nine", title="nine - Wiktionary", published=NOW - 100),
            _candidate("https://baike.baidu.com/item/%E7%94%9F%E5%8C%96%E5%8D%B1%E6%9C%BA",
                       title="生化危机_百度百科", published=NOW - 100),
            _candidate("https://www.gamesradar.com/2026/09/resident-evil-9-review",
                       title="Resident Evil 9 review: the real article", published=NOW - 100),
        ])
    assert [c["url"] for c in kept] == ["https://www.gamesradar.com/2026/09/resident-evil-9-review"]
    assert len(drops) == 4
    assert all("百科" in d[1] or "资料页" in d[1] for d in drops), drops


def test_prefilter_drops_disambiguation_titles(tmp_path) -> None:
    """标题看着就是消歧义页的（即使不是百科站）：丢。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://wiki.example.org/wiki/9_(disambiguation)",
                       title="9 (disambiguation)", published=NOW - 100),
            _candidate("https://wiki.example.org/wiki/9_(消歧义)",
                       title="9（消歧义）", published=NOW - 100),
            _candidate("https://wiki.example.org/wiki/resident-evil-9-review",
                       title="生化危机 9 深度拆解", published=NOW - 100),
        ])
    assert [c["url"] for c in kept] == ["https://wiki.example.org/wiki/resident-evil-9-review"]
    assert len(drops) == 2


# ----------------------------------------------------------------------
# C1 粗筛：归档 / 列表页
# ----------------------------------------------------------------------


def test_prefilter_drops_archive_and_listing_pages(tmp_path) -> None:
    """归档段路径 / 纯日期路径 / 「Archive for …」标题：丢。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://www.theverge.com/archives/2026/9",
                       title="Archive for September 2026 - Page 24 | The Verge", published=NOW - 100),
            _candidate("https://example.com/archive/2026/09/", title="九月归档", published=NOW - 100),
            _candidate("https://example.com/2026/09/", title="按月列表", published=NOW - 100),
            _candidate("https://example.com/page/24/", title="第 24 页", published=NOW - 100),
            _candidate("https://example.com/tags/switch", title="标签聚合", published=NOW - 100),
            # 首页 / 栏目页老规则保持不变
            _candidate("https://example.com/news/", title="新闻栏目", published=NOW - 100),
            # 真文章：路径里带日期段但最后是文章 slug，不能误伤
            _candidate("https://example.com/2026/09/switch-update-review",
                       title="任天堂九月更新实测", published=NOW - 100),
            _candidate("https://www.theverge.com/games/2026/9/30/re9-review",
                       title="Resident Evil 9 review roundup", published=NOW - 100),
        ])
    kept_urls = [c["url"] for c in kept]
    assert "https://example.com/2026/09/switch-update-review" in kept_urls
    assert "https://www.theverge.com/games/2026/9/30/re9-review" in kept_urls
    dropped_urls = {u for u, _r in drops}
    assert "https://www.theverge.com/archives/2026/9" in dropped_urls
    assert "https://example.com/archive/2026/09/" in dropped_urls
    assert "https://example.com/2026/09/" in dropped_urls
    assert "https://example.com/page/24/" in dropped_urls
    assert "https://example.com/tags/switch" in dropped_urls
    assert "https://example.com/news/" in dropped_urls
    for url, reason in drops:
        if "news/" in url or "tags/switch" in url:
            continue  # 老的 2 层短路径拦到的（栏目 / 标签页自己的理由）
        assert ("归档" in reason) or ("列表页" in reason), (url, reason)


# ----------------------------------------------------------------------
# C1 回归：单层数字路径（帖子 id）不能误伤
# ----------------------------------------------------------------------


def test_numeric_path_regression_not_mistaken_for_date_archive(tmp_path) -> None:
    """fresh.com/1、s.com/42 这类单层数字路径是帖子 id；只有多层纯数字或 4 位年份才算归档。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    with _TimePatch():
        kept, drops, _ = feeds._prefilter(GID, settings, [
            _candidate("https://fresh.com/1", title="量子芯片全新架构发布", published=NOW - 100),
            _candidate("https://fresh.com/42", title="编辑部最新圆桌探讨纪要", published=NOW - 100),
            _candidate("https://fresh.com/2026", title="站点年度汇总页", published=NOW - 100),      # 年归档：丢
            _candidate("https://fresh.com/2026/09", title="九月列表", published=NOW - 100),          # 月归档：丢
            _candidate("https://fresh.com/2026/09/real-article", title="九月热度实测的那一篇", published=NOW - 100),
        ])
    kept_urls = [c["url"] for c in kept]
    assert "https://fresh.com/1" in kept_urls
    assert "https://fresh.com/42" in kept_urls
    assert "https://fresh.com/2026/09/real-article" in kept_urls
    dropped_urls = {u for u, _r in drops}
    assert "https://fresh.com/2026" in dropped_urls
    assert "https://fresh.com/2026/09" in dropped_urls


def test_numeric_article_paths_with_file_extension_are_kept() -> None:
    """IT之家这类文章地址是 /0/843/123.htm：纯数字但最后是网页文件，是一篇文章不是日期归档。"""
    from maiwork.feeds import _is_archive_or_listing_page

    assert not _is_archive_or_listing_page("https://www.ithome.com/0/843/123.htm", "某新机发布")
    assert not _is_archive_or_listing_page("https://news.example.com/2026/0930/5566.html", "一篇报道")
    assert not _is_archive_or_listing_page("https://example.com/12/345", "帖子")  # 不像年份开头
    assert _is_archive_or_listing_page("https://example.com/2026/09/30/", "")
    assert _is_archive_or_listing_page("https://example.com/2026/09/index.html", "")


# ----------------------------------------------------------------------
# C2 「怎么搜」：搜索词要短，带好坏例子；定关注点的提示词也写明
# ----------------------------------------------------------------------


def test_search_guide_asks_for_short_queries_with_examples(tmp_path) -> None:
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    guide = feeds._search_guide_section(GID, settings)
    # 搜索词要短：一个核心事物 + 一个角度
    assert ("一个核心事物" in guide) or ("核心" in guide and "角度" in guide)
    # 长度口径写清（2–6 个词 / 只写不搜）
    assert "2–6" in guide or "2-6" in guide
    # 别把整条关注点原样当搜索词
    assert "别把整条关注点" in guide or "整条关注点" in guide
    # 好坏例子都在（坏例子就是线上那串关键词串烧的同类）
    assert "好：" in guide and "坏：" in guide
    assert "鬼武者" in guide  # 坏例子取自线上实录的关键词串烧


def test_discover_brief_carries_short_query_section(tmp_path) -> None:
    """撒网 brief（新找法）带的「怎么搜」就是同一段：核验它确实在。"""
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    brief = feeds._discover_brief(GID, [{"query": "方向一"}], settings)
    assert "2–6" in brief or "2-6" in brief
    assert "整条关注点" in brief


def test_focus_prompt_asks_for_short_direction_phrase(tmp_path) -> None:
    """定关注点（feeds.focus）的提示词要给模型写明：方向是一句短短的话，不是关键词串烧。"""
    from test_feeds_quality import _FOCUS_JSON, _run
    store, settings, feeds, models, workers, topics = _ready_two_phase_feeds(tmp_path)
    models.reply_queue = [_FOCUS_JSON, _FOCUS_JSON]
    with _TimePatch():
        _run(feeds._plan_focus(GID, settings))
    prompt = models.calls[0][1][0]["content"]
    assert ("关键词串" in prompt) or ("一两句" in prompt and "短" in prompt)
