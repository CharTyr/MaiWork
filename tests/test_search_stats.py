"""search_stats.py：按成绩软倾斜搜索（docs/10 §「第二步剩下三项」第 1 项，2026-10-05 用户拍板）。

覆盖：
- provider_rates / weak_providers：近 30 天各搜索服务的群向候选与进网页率；某家候选 ≥20 条
  且进网页率不到主家一半 → 这轮算弱；主家永不弱、主家样本不够或一条没进时谁都不弱；
- allow_weak_retry：弱的那家每周仍放一次（kv["search.weak_retry.<群号>"] 记上次放行时间）；
- filter_extras：合起来给出这轮还能用的「其他家」+ 跳过的；任何一步出错都原样放行；
- style_rates / style_prompt_lines：近 14 天「中文 / 英文 × 资讯 / 文章」四类问法的成绩，
  样本 <10 条不列，按进网页率从高到低写进定关注点提示词；
- 接线：`_floor_searches` 不叫成绩差的搜索服务补搜（每周一次的机会除外）、漏斗记
  funnel["weak_providers"]；`_plan_focus` 提示词带上问法成绩那几行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import search_stats
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeModelsQueue, focus_reply
from test_feeds_quality import GID, NOW, _TimePatch, _make_feeds, _run

DAY = 86400.0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "s.db")
    s.migrate()
    yield s
    s.close()


def _insert(
    store: Any,
    *,
    provider: str = "main",
    query: str = "",
    kind: str = "news",
    rejected: int = 0,
    days_ago: float = 1.0,
    gid: str = GID,
    target: str = "",
    n: int = 1,
    kept: int | None = None,
) -> None:
    """插 n 条 news_items：kept 条 rejected=0，其余 rejected=1（kept=None 时全按 rejected 参数）。"""
    rows = []
    for i in range(n):
        rej = rejected if kept is None else (0 if i < kept else 1)
        rows.append((1, gid, "标题", kind, query, provider, target, rej, NOW - days_ago * DAY))
    with store.tx() as conn:
        conn.executemany(
            "INSERT INTO news_items (batch_id, group_id, title, kind, src_query, src_provider,"
            " target_user_id, rejected, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


class _FakeSearch:
    """保底撒网用的假搜索：search / search_with 各记调用，broad_providers 给多家。"""

    def __init__(self, broad: list[str]) -> None:
        self._broad = list(broad)
        self.calls: list[tuple] = []
        self.with_calls: list[tuple] = []

    async def search(self, query, *, limit=8, days=None, site="", news=False):
        self.calls.append((query, limit, days, site, news))
        return [{"title": "主家结果", "url": "https://main.example/a", "snippet": "s",
                 "published": None, "provider": self._broad[0] if self._broad else "main"}]

    async def search_with(self, name, query, *, limit=8, days=None, site="", news=False):
        self.with_calls.append((name, query, limit, days))
        return [{"title": "撒网结果", "url": f"https://{name}.example/b", "snippet": "s",
                 "published": None, "provider": name}]

    def broad_providers(self) -> list[str]:
        return list(self._broad)


class _ReadBroken:
    def read(self):
        raise RuntimeError("库坏了")


class _TxBroken:
    """读写能过，事务写不了（模拟 kv 写失败）。"""

    def __init__(self, inner: Store) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def tx(self):
        raise RuntimeError("写不了")


# ----------------------------------------------------------------------
# 各家搜索服务的成绩
# ----------------------------------------------------------------------


def test_provider_rates_counts_and_excludes(store: Store) -> None:
    """近 30 天群向候选：RSS 源 / 个人向 / 太老 / 别的群 / 没记服务名的都不算。"""
    _insert(store, provider="main", n=3, kept=2)
    _insert(store, provider="exa", n=2, kept=2)
    _insert(store, provider="rss:gcores", n=2, kept=2)  # RSS 源不是搜索服务
    _insert(store, provider="", n=2, kept=2)  # 没记服务名
    _insert(store, provider="exa", target="10001", n=2, kept=2)  # 个人向
    _insert(store, provider="exa", days_ago=40, n=2, kept=2)  # 超出 30 天窗
    _insert(store, provider="exa", gid="222", n=2, kept=2)  # 别的群
    assert search_stats.provider_rates(store, GID, NOW) == {
        "main": {"cand": 3, "kept": 2},
        "exa": {"cand": 2, "kept": 2},
    }


def test_provider_rates_broken_store_is_empty(store: Store) -> None:
    assert search_stats.provider_rates(_ReadBroken(), GID, NOW) == {}


def test_weak_providers_thresholds(store: Store) -> None:
    """候选 ≥20 且进网页率 < 主家一半才算弱；正好一半、候选只有 19 条、主家自己都不算。"""
    _insert(store, provider="main", n=20, kept=10)  # 0.5
    _insert(store, provider="weak", n=20, kept=2)  # 0.1 < 0.25
    _insert(store, provider="edge", n=20, kept=5)  # 0.25，正好一半 → 不弱
    _insert(store, provider="tiny", n=19, kept=0)  # 候选不到 20 → 不算弱
    assert search_stats.weak_providers(
        store, GID, NOW, "main", ["weak", "edge", "tiny", "main"]
    ) == {"weak"}


def test_weak_providers_needs_main_sample(store: Store) -> None:
    """主家候选不到 20 条：没有可比对象，谁都不弱。"""
    _insert(store, provider="main", n=19, kept=19)
    _insert(store, provider="bad", n=30, kept=0)
    assert search_stats.weak_providers(store, GID, NOW, "main", ["bad"]) == set()


def test_weak_providers_none_when_main_rate_zero(store: Store) -> None:
    """主家一条都没进网页（率 0）：无从比较，谁都不弱。"""
    _insert(store, provider="main", n=20, kept=0)
    _insert(store, provider="bad", n=25, kept=1)
    assert search_stats.weak_providers(store, GID, NOW, "main", ["bad"]) == set()


def test_weak_providers_unknown_extra_is_not_weak(store: Store) -> None:
    _insert(store, provider="main", n=20, kept=10)
    assert search_stats.weak_providers(store, GID, NOW, "main", ["没见过的家"]) == set()


# ----------------------------------------------------------------------
# 每周一次的机会
# ----------------------------------------------------------------------


def test_allow_weak_retry_weekly(store: Store) -> None:
    """第一次（没记过）放行并记下时间；7 天内不再放；满 7 天再放一次并更新记录。"""
    assert search_stats.allow_weak_retry(store, GID, "weak", NOW) is True
    assert store.kv_get(f"search.weak_retry.{GID}") == {"weak": NOW}
    assert search_stats.allow_weak_retry(store, GID, "weak", NOW + 6 * DAY) is False
    assert search_stats.allow_weak_retry(store, GID, "weak", NOW + 7 * DAY) is True
    assert store.kv_get(f"search.weak_retry.{GID}") == {"weak": NOW + 7 * DAY}
    # 另一家各记各的
    assert search_stats.allow_weak_retry(store, GID, "other", NOW + 7 * DAY) is True
    assert store.kv_get(f"search.weak_retry.{GID}") == {
        "weak": NOW + 7 * DAY,
        "other": NOW + 7 * DAY,
    }


# ----------------------------------------------------------------------
# 合起来：这轮还能用哪些「其他家」
# ----------------------------------------------------------------------


def test_filter_extras_keeps_weak_first_time_then_skips(store: Store) -> None:
    """弱的那家第一次遇到先给一次机会（并记时间），7 天内跳过，满 7 天再放。"""
    _insert(store, provider="main", n=20, kept=10)
    _insert(store, provider="weak", n=20, kept=2)
    _insert(store, provider="ok", n=20, kept=8)  # 0.4 > 0.25 → 不弱
    assert search_stats.filter_extras(store, GID, NOW, "main", ["weak", "ok"]) == (
        ["weak", "ok"], []
    )
    assert search_stats.filter_extras(store, GID, NOW + 6 * DAY, "main", ["weak", "ok"]) == (
        ["ok"], ["weak"]
    )
    assert search_stats.filter_extras(store, GID, NOW + 7 * DAY, "main", ["weak", "ok"]) == (
        ["weak", "ok"], []
    )


def test_filter_extras_blocks_weak_with_recent_retry(store: Store) -> None:
    """已经给过每周机会（记录还在 7 天内）→ 这轮跳过它，别的家照常。"""
    _insert(store, provider="main", n=20, kept=10)
    _insert(store, provider="weak", n=20, kept=2)
    with store.tx() as conn:
        store.kv_set(conn, f"search.weak_retry.{GID}", {"weak": NOW - 2 * DAY})
    assert search_stats.filter_extras(store, GID, NOW, "main", ["weak"]) == ([], ["weak"])
    assert search_stats.filter_extras(store, GID, NOW, "main", ["ok"]) == (["ok"], [])


def test_filter_extras_never_raises(store: Store) -> None:
    """读坏 / 事务写不了 / 完全不是 store：原样放行，从不出错。"""
    _insert(store, provider="main", n=20, kept=10)
    _insert(store, provider="weak", n=20, kept=2)
    assert search_stats.filter_extras(_ReadBroken(), GID, NOW, "main", ["weak"]) == (
        ["weak"], []
    )
    assert search_stats.filter_extras(object(), GID, NOW, "main", ["a", "b"]) == (
        ["a", "b"], []
    )
    # 弱家能认出来，但记「每周一次」时 kv 写不了 → 整步放弃、原样放行
    assert search_stats.filter_extras(_TxBroken(store), GID, NOW, "main", ["weak"]) == (
        ["weak"], []
    )


def test_filter_extras_empty_extras(store: Store) -> None:
    assert search_stats.filter_extras(store, GID, NOW, "main", []) == ([], [])


# ----------------------------------------------------------------------
# 各类问法的成绩
# ----------------------------------------------------------------------


def test_style_rates_language_and_kind(store: Store) -> None:
    """语种按问法粗判（含汉字的混排算中文）、kind 只分资讯 / 文章；RSS、没记问法、
    个人向、太老的都不算。"""
    _insert(store, provider="keenable", query="本地大模型新玩法", kind="news", n=3, kept=2)
    _insert(store, provider="exa", query="openai gpt news", kind="guide", n=2, kept=1)
    _insert(store, provider="keenable", query="GPT-5 发布进展", kind="news", n=1, kept=1)
    _insert(store, provider="keenable", query="别的类目", kind="other", n=1, kept=1)
    _insert(store, provider="rss:abc", query="机器之心", kind="news", n=2, kept=2)
    _insert(store, provider="keenable", query="", kind="news", n=2, kept=2)
    _insert(store, provider="keenable", query="私有问法", kind="news", n=2, kept=2, target="10001")
    _insert(store, provider="keenable", query="太老问法", kind="news", n=2, kept=2, days_ago=20)
    rates = {
        (r["lang"], r["kind"]): (r["cand"], r["kept"])
        for r in search_stats.style_rates(store, GID, NOW)
    }
    assert rates == {("zh", "news"): (5, 4), ("en", "guide"): (2, 1)}


def test_style_rates_sorted_by_rate_desc(store: Store) -> None:
    _insert(store, provider="a", query="中文资讯", kind="news", n=10, kept=2)
    _insert(store, provider="b", query="english guide", kind="guide", n=10, kept=9)
    got = search_stats.style_rates(store, GID, NOW)
    assert [(r["lang"], r["kind"]) for r in got] == [("en", "guide"), ("zh", "news")]


def test_style_prompt_lines_empty_below_min(store: Store) -> None:
    """每一类样本都不到 10 条 → 一个字都不带；读坏了也一样。"""
    _insert(store, provider="keenable", query="中文问法", kind="news", n=9, kept=9)
    assert search_stats.style_prompt_lines(store, GID, NOW) == []
    assert search_stats.style_prompt_lines(object(), GID, NOW) == []


def test_style_prompt_lines_format_and_sort(store: Store) -> None:
    _insert(store, provider="keenable", query="中文问法", kind="news", n=111, kept=61)
    _insert(store, provider="exa", query="english query", kind="guide", n=20, kept=15)
    _insert(store, provider="exa", query="english news", kind="news", n=9, kept=9)  # 样本不够
    assert search_stats.style_prompt_lines(store, GID, NOW) == [
        "近 14 天各类搜索问法的成绩（进网页 / 搜到的候选，只是参考，方向和口味优先）：",
        "- 英文问法找文章：15 / 20（75%）",
        "- 中文问法找资讯：61 / 111（55%）",
        "成绩明显好的那类问法可以多用一点；样本少的没列。",
    ]


# ----------------------------------------------------------------------
# 接线一：保底撒网不叫成绩差的搜索服务补搜
# ----------------------------------------------------------------------


def test_floor_searches_skips_weak_provider(tmp_path: Path) -> None:
    """弱家（近 30 天 20 条候选只进 2 条）在每周机会还没到时不被叫去补搜；
    主家照搜；漏斗记下跳过的家。满 7 天再放它一次。"""
    store, settings, feeds, models, workers, topics, profiles = _make_feeds(tmp_path)
    _insert(store, provider="main", n=20, kept=10)
    _insert(store, provider="weak", n=20, kept=2)
    search = _FakeSearch(["main", "weak"])
    feeds._search = search
    focus = [{"query": "开源掌机新动向"}]
    with _TimePatch():
        with store.tx() as conn:
            store.kv_set(conn, f"search.weak_retry.{GID}", {"weak": NOW - 2 * DAY})
        funnel: dict = {"queries": 0, "providers": {}, "per_focus": []}
        _run(feeds._floor_searches(GID, focus, [], funnel))
        assert search.calls, "主家照搜，不该被成绩倾斜挡住"
        assert search.with_calls == [], "弱家这轮不该被叫去补搜"
        assert funnel["weak_providers"] == ["weak"]
        # 满 7 天：放它一次
        with store.tx() as conn:
            store.kv_set(conn, f"search.weak_retry.{GID}", {"weak": NOW - 8 * DAY})
        funnel2: dict = {"queries": 0, "providers": {}, "per_focus": []}
        _run(feeds._floor_searches(GID, focus, [], funnel2))
        assert [name for name, *_ in search.with_calls] == ["weak"]
        assert "weak_providers" not in funnel2


# ----------------------------------------------------------------------
# 接线二：定关注点的提示词带上各类问法的成绩
# ----------------------------------------------------------------------


def test_plan_focus_prompt_contains_style_lines(tmp_path: Path) -> None:
    focus_json = focus_reply("开源掌机新动向", "本地大模型新玩法", "周末局域网聚会")
    models = FakeModelsQueue(ready=True, replies=[focus_json])
    store, settings, feeds, _m, workers, topics, profiles = _make_feeds(tmp_path, models=models)
    _insert(store, provider="keenable", query="中文问法", kind="news", n=12, kept=8)
    with _TimePatch():
        out = _run(feeds._plan_focus(GID, settings))
    assert out, "关注点该照常出"
    prompt = str(models.calls[0][1][0]["content"])
    assert "近 14 天各类搜索问法的成绩" in prompt
    assert "- 中文问法找资讯：8 / 12（67%）" in prompt
    # 就贴在「资讯标准」那段前面，中间隔一空行
    assert "成绩明显好的那类问法可以多用一点；样本少的没列。\n\n资讯标准（定关注点照这个来）：" in prompt


def test_plan_focus_prompt_omits_style_lines_when_sample_small(tmp_path: Path) -> None:
    focus_json = focus_reply("开源掌机新动向", "本地大模型新玩法", "周末局域网聚会")
    models = FakeModelsQueue(ready=True, replies=[focus_json])
    store, settings, feeds, _m, workers, topics, profiles = _make_feeds(tmp_path, models=models)
    _insert(store, provider="keenable", query="中文问法", kind="news", n=9, kept=9)
    with _TimePatch():
        out = _run(feeds._plan_focus(GID, settings))
    assert out
    prompt = str(models.calls[0][1][0]["content"])
    assert "近 14 天各类搜索问法的成绩" not in prompt
    assert "资讯标准（定关注点照这个来）：" in prompt


def test_plan_focus_prompt_style_lines_without_history(tmp_path: Path) -> None:
    """库里一条问法记录都没有：照常定关注点，不带成绩那几行。"""
    focus_json = focus_reply("开源掌机新动向", "本地大模型新玩法", "周末局域网聚会")
    models = FakeModelsQueue(ready=True, replies=[focus_json])
    store, settings, feeds, _m, workers, topics, profiles = _make_feeds(tmp_path, models=models)
    with _TimePatch():
        out = _run(feeds._plan_focus(GID, settings))
    assert out
    prompt = str(models.calls[0][1][0]["content"])
    assert "近 14 天各类搜索问法的成绩" not in prompt
