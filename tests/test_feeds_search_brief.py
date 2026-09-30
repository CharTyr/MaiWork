"""找资讯子 agent 的「怎么搜」要求（docs/10 第七节第 2 步，2026-09-30）。

- 每个方向换几种问法（一手来源 / 技术细节 / 社区讨论 / 反面意见 / 本地语言 / 后续进展），
  用 site 直奔一手来源，禁止同义改写刷搜索；
- 时间由程序管：资讯默认只搜 7 天内（web_search 不填 days 程序也会补），找文章才放宽到 180 天；
- 屏蔽名单（手动 + 自动）写进 brief，别在注定被筛掉的来源上浪费打开次数；
- 绑定的搜索服务是预设的一家 → 把那家的 skill（官方用法）附在 brief 后面。
"""

from __future__ import annotations

from test_feeds_quality import _TimePatch, _brief, _make_feeds, _run, GID

from CharTyr_MaiWork.maiwork import extensions_web
from CharTyr_MaiWork.maiwork.search_binding import set_binding


def test_brief_asks_multiple_angles_and_primary_sources(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    brief = _brief(workers)
    for word in ("一手来源", "社区", "反面", "后续进展", "site"):
        assert word in brief, word
    assert "同义" in brief  # 禁止同义改写刷搜索


def test_brief_says_time_is_handled_by_code(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    brief = _brief(workers)
    assert "7 天" in brief and "180" in brief
    assert "别在搜索词里" in brief  # 不要往搜索词里塞年份月份求新


def test_brief_lists_blocked_domains(tmp_path) -> None:
    cfg = {"feeds": {"blocked_domains": ["spam-news.example"]}}
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path, cfg=cfg)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert "spam-news.example" in _brief(workers)


def test_brief_no_blocked_section_when_empty(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert "会被直接筛掉" not in _brief(workers)


def test_brief_includes_bound_preset_skill(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    extensions_web.create(store, settings, {
        "name": "keenable", "url": "https://api.keenable.ai/mcp", "headers": {},
        "roles": ["worker"], "enabled": True, "tools": [], "timeout_s": 30,
    })
    set_binding(store, {"mcp": "keenable", "tool": "search_web_pages"})
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    brief = _brief(workers)
    assert "Keenable" in brief
    assert "Describe the ideal page" in brief
    assert "name: search-keenable" not in brief  # 不带 frontmatter


def test_brief_no_skill_for_unknown_provider(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    extensions_web.create(store, settings, {
        "name": "mysearch", "url": "https://search.example/mcp", "headers": {},
        "roles": ["worker"], "enabled": True, "tools": [], "timeout_s": 30,
    })
    set_binding(store, {"mcp": "mysearch", "tool": "search"})
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert "搜索服务的用法" not in _brief(workers)
