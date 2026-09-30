"""source_stats.py：本群的「优质来源」名单（docs/10 第七节第 6 步，2026-09-30）。

- 按域名统计近 30 天上了网页、平均分 ≥4 的条目（越近越重，半衰期 15 天），群友点「有用」的加分；
- 至少 2 条高分才上名单（样本少不算数），被管理员移出的不再上；屏蔽名单里的不上；
- source_prior：给两段式预筛排序用的 0–1 分（名单里的越靠前越高，不在名单 = 0）；
- 名单只影响「先搜哪里 / 先开谁」，不绕过任何核对（这里只出名单，不改门槛）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import source_stats
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _item(store, url, *, avg=4.2, rejected=0, days_ago=1.0, up=0, down=0, gid=GID, site=None):
    host = site or url.split("/")[2]
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, url_key, sources, scores, rejected, up, down, created)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, url, url.split("://", 1)[-1], json.dumps([{"url": url, "site": host}]),
             json.dumps({"avg": avg}), rejected, up, down, NOW - days_ago * 86400),
        )


def test_needs_two_high_items(store):
    _item(store, "https://gcores.com/a")
    assert source_stats.trusted_domains(store, GID, NOW) == []
    _item(store, "https://gcores.com/b")
    assert source_stats.trusted_domains(store, GID, NOW) == ["gcores.com"]


def test_low_score_rejected_old_and_other_group_ignored(store):
    for i in range(3):
        _item(store, f"https://low.example/{i}", avg=3.4)
        _item(store, f"https://rej.example/{i}", rejected=1)
        _item(store, f"https://old.example/{i}", days_ago=40)
        _item(store, f"https://other.example/{i}", gid="222")
    assert source_stats.trusted_domains(store, GID, NOW) == []


def test_recent_and_upvoted_rank_higher(store):
    for i in range(2):
        _item(store, f"https://older.example/{i}", days_ago=25)
        _item(store, f"https://fresh.example/{i}", days_ago=1)
        _item(store, f"https://liked.example/{i}", days_ago=25, up=2)
    ranked = source_stats.trusted_domains(store, GID, NOW)
    assert ranked[0] in ("fresh.example", "liked.example")
    assert ranked[-1] == "older.example"


def test_www_prefix_merged(store):
    _item(store, "https://www.nintendolife.com/a", site="www.nintendolife.com")
    _item(store, "https://nintendolife.com/b", site="nintendolife.com")
    assert source_stats.trusted_domains(store, GID, NOW) == ["nintendolife.com"]


def test_removed_and_blocked_excluded(store):
    for i in range(2):
        _item(store, f"https://a.example/{i}")
        _item(store, f"https://b.example/{i}")
    source_stats.set_removed(store, GID, "a.example", True)
    assert source_stats.trusted_domains(store, GID, NOW) == ["b.example"]
    assert source_stats.trusted_domains(store, GID, NOW, blocked={"b.example"}) == []
    source_stats.set_removed(store, GID, "a.example", False)
    assert "a.example" in source_stats.trusted_domains(store, GID, NOW)


def test_limit(store):
    for d in range(12):
        for i in range(2):
            _item(store, f"https://s{d}.example/{i}")
    assert len(source_stats.trusted_domains(store, GID, NOW)) == 8
    assert len(source_stats.trusted_domains(store, GID, NOW, limit=3)) == 3


def test_source_prior(store):
    for i in range(3):
        _item(store, f"https://top.example/{i}")
    for i in range(2):
        _item(store, f"https://second.example/{i}", days_ago=20)
    top = source_stats.source_prior(store, GID, "top.example", NOW)
    second = source_stats.source_prior(store, GID, "www.second.example", NOW)
    assert 0 < second < top <= 1.0
    assert source_stats.source_prior(store, GID, "nobody.example", NOW) == 0.0


def test_view_for_admin(store):
    for i in range(2):
        _item(store, f"https://a.example/{i}")
    source_stats.set_removed(store, GID, "zzz.example", True)
    v = source_stats.view(store, GID, NOW)
    assert v["trusted"][0]["domain"] == "a.example" and v["trusted"][0]["high"] == 2
    assert v["removed"] == ["zzz.example"]
