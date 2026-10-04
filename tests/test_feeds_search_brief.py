"""找资讯的「怎么搜」要求（docs/10 第七节第 2 步，2026-09-30；2026-10-01 起挪进定关注点提示词）。

- 每个方向换几种问法（一手来源 / 技术细节 / 社区讨论 / 反面意见 / 本地语言 / 后续进展），
  用 site 直奔一手来源，禁止同义改写刷搜索；
- 时间由程序管：资讯默认只搜 7 天内，找文章才放宽到 180 天；
- 屏蔽名单（手动 + 自动）写进提示词；
- 绑定的搜索服务是预设的一家 → 把那家的 skill（官方用法）附在提示词后面。

2026-10-01 起撒网改代码按计划搜：这些要求全进**定关注点（feeds.focus）**的提示词，
让模型一次就把每个方向的搜索计划 searches 给出来。
"""

from __future__ import annotations

from test_feeds_quality import _TimePatch, _make_feeds, _run, GID

from CharTyr_MaiWork.maiwork import extensions_web
from CharTyr_MaiWork.maiwork.search_binding import set_binding


def _focus_prompt(models) -> str:
    return str(models.calls[0][1][0]["content"])


def test_brief_asks_multiple_angles_and_primary_sources(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    prompt = _focus_prompt(models)
    for word in ("一手来源", "社区", "反面", "后续进展", "site"):
        assert word in prompt, word
    assert "同义" in prompt  # 禁止同义改写刷搜索


def test_brief_says_time_is_handled_by_code(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    prompt = _focus_prompt(models)
    assert "7 天" in prompt and "180" in prompt
    assert "别在搜索词里" in prompt  # 不要往搜索词里塞年份月份求新


def test_brief_lists_blocked_domains(tmp_path) -> None:
    """屏蔽名单进定关注点提示词（2026-10 起从 kv["feeds.blocked.<gid>"] 按群读）。"""
    from CharTyr_MaiWork.maiwork import feeds as _feeds

    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    _feeds.blocked_domains_set(store, GID, ["spam-news.example"])
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert "spam-news.example" in _focus_prompt(models)


def test_brief_no_blocked_section_when_empty(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert "会被直接筛掉" not in _focus_prompt(models)


def test_brief_includes_bound_preset_skill(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    extensions_web.create(store, settings, {
        "name": "keenable", "url": "https://api.keenable.ai/mcp", "headers": {},
        "roles": ["worker"], "enabled": True, "tools": [], "timeout_s": 30,
    })
    set_binding(store, {"mcp": "keenable", "tool": "search_web_pages"})
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    prompt = _focus_prompt(models)
    assert "Keenable" in prompt
    assert "Describe the ideal page" in prompt
    assert "name: search-keenable" not in prompt  # 不带 frontmatter


def test_brief_no_skill_for_unknown_provider(tmp_path) -> None:
    store, settings, feeds, models, workers, topics, _ = _make_feeds(tmp_path)
    extensions_web.create(store, settings, {
        "name": "mysearch", "url": "https://search.example/mcp", "headers": {},
        "roles": ["worker"], "enabled": True, "tools": [], "timeout_s": 30,
    })
    set_binding(store, {"mcp": "mysearch", "tool": "search"})
    with _TimePatch():
        _run(feeds.prepare_news(GID))
    assert "搜索服务的用法" not in _focus_prompt(models)
