"""发布日期的依据（2026-10-08 线上问题）：

同一篇机核播客页（gcores.com/radios/220558）两次相隔 9 分钟的核验，分别存成 10-06 和 10-07——
抓取工具没读到结构化日期，模型看到「3 小时前」自己瞎换算。改成：

1. 代码从网页 HTML 读到的日期（tool_calls 里的「页面发布日期」标记）覆盖模型给的；
2. 模型只看到相对时间就原样填进 published，代码按这次 fetch_page 的时间（tool_calls.ts）换算；
3. 记依据 item["date_basis"] ∈ page / relative / model / search / ""，published_raw 保留原文。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import clock, news_recheck, page_date
from CharTyr_MaiWork.maiwork.feeds import Feeds, _parse_published
from CharTyr_MaiWork.maiwork.store import Store

# 北京时间 2026-10-07 10:00
BASE = datetime(2026, 10, 7, 10, 0, tzinfo=clock.BJ).timestamp()
URL = "https://www.gcores.com/radios/220558"


def _bj_date(ts: float) -> str:
    return clock.bj(ts).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# 相对时间换算（纯函数）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("3 小时前", 3 * 3600),
        ("3小时前", 3 * 3600),
        ("三小时前", 3 * 3600),
        ("两小时前", 2 * 3600),
        ("半小时前", 1800),
        ("15 分钟前", 15 * 60),
        ("2 天前", 2 * 86400),
        ("2天前", 2 * 86400),
        ("1 周前", 7 * 86400),
        ("昨天", 86400),
        ("昨天 21:30", 86400),
        ("前天", 2 * 86400),
        ("刚刚", 0),
        ("今天", 0),
        ("页面显示 3 小时前", 3 * 3600),
        ("3 hours ago", 3 * 3600),
        ("an hour ago", 3600),
        ("1 hour ago", 3600),
        ("5 mins ago", 300),
        ("2 days ago", 2 * 86400),
        ("a day ago", 86400),
        ("Yesterday", 86400),
        ("yesterday at 3:00 PM", 86400),
        ("just now", 0),
        ("Today", 0),
        ("3h ago", 3 * 3600),
        ("2d ago", 2 * 86400),
    ],
)
def test_relative_ts_variants(text: str, seconds: int) -> None:
    assert page_date.relative_ts(text, BASE) == pytest.approx(BASE - seconds)


@pytest.mark.parametrize(
    "text",
    ["", None, "2026-10-06", "2026-10-06T08:00:00+08:00", "1759800000", "不知道", "Mon, 05 Oct 2026 10:00:00 GMT",
     "3 小时后"],
)
def test_relative_ts_not_relative(text) -> None:
    assert page_date.relative_ts(text, BASE) is None


def test_relative_date_uses_beijing_day() -> None:
    # 北京时间 10-07 01:00 的「3 小时前」是 10-06
    base = datetime(2026, 10, 7, 1, 0, tzinfo=clock.BJ).timestamp()
    assert page_date.relative_date("3 小时前", base) == "2026-10-06"
    assert page_date.relative_date("3 hours ago", BASE) == "2026-10-07"
    assert page_date.relative_date("2026-10-01", BASE) == ""


def test_fetch_times_from_rows_takes_latest_open() -> None:
    rows = [
        {"tool": "fetch_page", "ok": 1, "ts": BASE - 60, "input": URL, "output": "取到正文"},
        {"tool": "fetch_page", "ok": 1, "ts": BASE, "input": URL, "output": "取到正文"},
        {"tool": "fetch_page", "ok": 0, "ts": BASE + 999, "input": URL, "output": ""},
        {"tool": "web_search", "ok": 1, "ts": BASE + 999, "input": URL, "output": ""},
    ]
    assert page_date.fetch_times_from_rows(rows) == {"www.gcores.com/radios/220558": BASE}


# ----------------------------------------------------------------------
# 接线：feeds 核验（_fill_dates_from_pages）
# ----------------------------------------------------------------------


def _log_fetch(store: Store, task_id: str, url: str, ts: float, *, date: str = "") -> None:
    out = "取到正文 500 字（直接打开）" + (page_date.note(date) if date else "")
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, '900000001', ?, '子 agent #1', 'fetch_page', ?, ?, 1)",
            (ts, task_id, url, out),
        )


def _item(published) -> dict:
    return {
        "title": "机核播客",
        "url": URL,
        "published_raw": published,
        "published_ts": _parse_published(published),
    }


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _fill(store: Store, task_id: str, items: list[dict]) -> int:
    return Feeds._fill_dates_from_pages(SimpleNamespace(_store=store), task_id, items)


def test_page_date_overrides_model_date(store) -> None:
    _log_fetch(store, "feeds-verify:x:0", URL, BASE, date="2026-10-07")
    item = _item("2026-10-06")
    _fill(store, "feeds-verify:x:0", [item])
    assert _bj_date(item["published_ts"]) == "2026-10-07"
    assert item["date_basis"] == "page"
    assert item["published_raw"] == "2026-10-06", "模型原文要保留"


def test_relative_text_converted_by_fetch_time_same_day_9_minutes_apart(store) -> None:
    """线上那两次：相隔 9 分钟的两次核验，模型都写「3 小时前」→ 同一天。"""
    _log_fetch(store, "feeds-verify:a:0", URL, BASE)
    _log_fetch(store, "feeds-verify:b:0", URL, BASE + 9 * 60)
    first, second = _item("3 小时前"), _item("3 小时前")
    _fill(store, "feeds-verify:a:0", [first])
    _fill(store, "feeds-verify:b:0", [second])
    assert first["published_ts"] == pytest.approx(BASE - 3 * 3600)
    assert second["published_ts"] == pytest.approx(BASE + 540 - 3 * 3600)
    assert _bj_date(first["published_ts"]) == _bj_date(second["published_ts"]) == "2026-10-07"
    assert first["date_basis"] == second["date_basis"] == "relative"
    assert first["published_raw"] == "3 小时前"


def test_relative_text_overrides_search_fallback_date(store) -> None:
    """核验把相对时间填进 published、日期先被搜索结果日期兜底补上 → 相对时间换算优先。"""
    _log_fetch(store, "feeds-verify:a:0", URL, BASE)
    item = _item("3 hours ago")
    item["published_ts"] = BASE - 5 * 86400
    item["date_basis"] = "search"
    _fill(store, "feeds-verify:a:0", [item])
    assert item["published_ts"] == pytest.approx(BASE - 3 * 3600)
    assert item["date_basis"] == "relative"


def test_relative_without_fetch_record_uses_now(store, monkeypatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: BASE)
    item = _item("昨天")
    _fill(store, "feeds-verify:none:0", [item])
    assert item["published_ts"] == pytest.approx(BASE - 86400)
    assert item["date_basis"] == "relative"


def test_no_evidence_keeps_model_date(store) -> None:
    _log_fetch(store, "feeds-verify:a:0", URL, BASE)  # 打开过，但没读到日期
    item = _item("2026-10-06")
    before = item["published_ts"]
    _fill(store, "feeds-verify:a:0", [item])
    assert item["published_ts"] == before
    assert item["date_basis"] == "model"
    assert item["published_raw"] == "2026-10-06"


def test_unparseable_stays_without_date(store) -> None:
    _log_fetch(store, "feeds-verify:a:0", URL, BASE)
    item = _item("看不出来")
    _fill(store, "feeds-verify:a:0", [item])
    assert item["published_ts"] is None
    assert item["date_basis"] == ""


# ----------------------------------------------------------------------
# 接线：补打开（news_recheck._fill_code_dates）
# ----------------------------------------------------------------------


def test_recheck_page_date_overrides_and_relative(store) -> None:
    other = "https://example.com/news/1"
    _log_fetch(store, "feeds-recheck:x", URL, BASE, date="2026-10-07")
    _log_fetch(store, "feeds-recheck:x", other, BASE + 60)
    a = _item("2026-10-06")
    b = {"title": "t", "url": other, "published_raw": "2 days ago", "published_ts": None}
    news_recheck._fill_code_dates(store, "feeds-recheck:x", [a, b], _parse_published)
    assert _bj_date(a["published_ts"]) == "2026-10-07" and a["date_basis"] == "page"
    assert b["published_ts"] == pytest.approx(BASE + 60 - 2 * 86400)
    assert b["date_basis"] == "relative"
