"""可信的文章发布时间定位（第三轮线上巡检 2026-10-08 §4.4 缺陷 2 与建议 3）。

线上实测（本地用仓库里的页面副本复现）：chuapp 291676、gcores 220636、gcores 220638、
yystv 14479 四个样本 `page_date.published_from_html` **全部返回 ""**。原因分两种：

- **三个是真正的「属性上有绝对日期、以前没被读到」**（meta / JSON-LD / `<time datetime>` 都没有）：
  - 触乐：`<span class="fn-right friendly_time" data-time="1791453657">2026年10月08日 18时00分</span>`
  - 机核：`<span class="me-2 u_color-gray-info" title="2026-10-08 17:52:36">5 小时前</span>`
- **第四个 yystv 14479 是另一种情况**：页面上只有「22小时前 发布」这类相对标签，没有任何绝对
  日期字段（无 meta / 无 data-time / 无带日期的 title）→ 按设计照旧返回 ""，不许硬造日期，
  下游按相对标签换算。**它返回空是对的，不是这次要修的漏读**（`test_relative_only_page_...` 钉住）。

读不到就只剩「模型抄回的相对文字 → 按打开时刻减 n 个整单位」，而相对标签是向下取整的
（「1 小时前」= 1h00m–1h59m），日期会跟着舍入漂。

这里钉住两件事：

1. `published_from_html` 只做**结构保守**的扩展：属性名本身是日期语义
   （data-time / data-date / data-published…），或 `title` 而元素的可见文字就是相对时间标签
   （「相对文字 + 绝对属性」是发布时间展示位的常见写法）；两者都还要过一道「发布上下文」闸
   （自身 / 附近容器的类名像发布时间，或可见文字是相对时间）。
   评论、推荐文章、作者档案、任意 span 的 title 一律不认——不抓「页面上随便一个日期」当发布时间。
   已有 meta → JSON-LD → `<time>` 的优先顺序不变，新读法排在它们后面。
2. `resolve_dates` 里相对标签不许覆盖**更精确**的发布时间：已有页面 / 搜索结果给的精确时间、
   且落在相对标签自己的粒度区间内时保留精确值（线上 gcores 220638 被覆盖成
   18:19:41，比页面 title 的 17:52:36 晚 27 分钟）；但模型自己猜的旧时间不算证据，
   照旧让页面上活着的相对标签赢。

这些用例都是**代码层**的：页面片段取自线上真实页面（去掉了无关部分），
`feeds.py` 那一段（撒网候选日期兜底 → `_fill_dates_from_pages`）按源码路径照抄在测试里。
本地不调模型，所以**不测真实模型交回的日期文字**——那部分只能靠线上日志验证。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock, page_date
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import _parse_published
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

from fakes import FakeProfiles

URL = "https://www.gcores.com/articles/220638"

# 线上页面里真实出现过的两段标记（只截了发布日期那一段）
GCORES_MARKUP = (
    '<div class="article-info"><a href="/users/1">'
    '<div class="avatar_text"><span class="me-2">YT17</span></div></a>'
    '<span class="me-2 u_color-gray-info" title="2026-10-08 17:52:36">5 小时前</span>'
    '<span><span class="me-1">发布于</span>'
    '<a class="u_color-category" href="/categories/2" target="_blank">资讯</a></span></div>'
)
CHUAPP_MARKUP = (
    '<div class="author-time fn-clear"><span class="fn-left"><em>编辑</em>陈静</span>'
    '<span class="fn-right friendly_time" data-time="1791453657">2026年10月08日 18时00分</span></div>'
)


def _bj(*args: int) -> float:
    return datetime(*args, tzinfo=clock.BJ).timestamp()


def _page(body: str) -> str:
    return f"<html><head><title>标题</title></head><body>{body}</body></html>"


# ----------------------------------------------------------------------
# 一、属性上的发布日期（线上两个站的真实写法）
# ----------------------------------------------------------------------


@pytest.mark.parametrize("markup", [CHUAPP_MARKUP, GCORES_MARKUP])
def test_publish_markup_attrs_read(markup: str) -> None:
    """触乐的 data-time、机核的 title（文字是相对时间）都要读出来。"""
    assert page_date.published_from_html(_page(markup)) == "2026-10-08"


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="post-time" data-time="2026-10-08T17:52:36+08:00">2026年10月08日</div>',
        '<div class="article-date" data-date="2026-10-08">发布于 2026 年 10 月 8 日</div>',
        '<div class="publish-time" data-published="2026/10/8">2026 年 10 月 8 日</div>',
        '<div class="friendly_time" data-time="1791453657"></div>',  # 文字被样式藏起来也算
    ],
)
def test_other_date_attribute_names(markup: str) -> None:
    assert page_date.published_from_html(_page(markup)) == "2026-10-08"


def test_markup_attr_inside_article_wins() -> None:
    """页面边上还有别的发布时间（栏目标题之类）时，正文 <article> 里的优先。"""
    html = _page(
        '<aside><span class="publish-time" data-time="1790848857">2026年10月01日</span></aside>'
        "<article><h1>正文</h1>"
        '<span class="friendly_time" data-time="1791453657">2026年10月08日 18时00分</span>'
        "<p>正文内容</p></article>"
    )
    assert page_date.published_from_html(html) == "2026-10-08"


@pytest.mark.parametrize(
    "head,body",
    [
        (
            '<meta property="article:published_time" content="2026-09-28T10:00:00+08:00">',
            GCORES_MARKUP,
        ),
        (
            '<script type="application/ld+json">{"@type":"NewsArticle",'
            '"datePublished":"2026-09-28T10:00:00+08:00"}</script>',
            CHUAPP_MARKUP,
        ),
    ],
)
def test_meta_and_json_ld_keep_priority(head: str, body: str) -> None:
    html = f"<html><head>{head}</head><body>{body}</body></html>"
    assert page_date.published_from_html(html) == "2026-09-28"


def test_time_tag_keeps_priority_over_markup_attrs() -> None:
    html = _page(f'<article><time datetime="2026-09-28">9 月 28 日</time>{GCORES_MARKUP}</article>')
    assert page_date.published_from_html(html) == "2026-09-28"


@pytest.mark.parametrize(
    "markup",
    [
        # 任意 span 的 title 里有日期：不认（标题里的日期、作者注册日期都不算发布时间）
        '<span title="2025-12-03">作者 2025 年注册</span>',
        '<span class="u_color-gray-info" title="2025-12-03">简介</span>',
        # 作者档案里的日期
        '<div class="original_createdDate avatar_sub">2025-12-03</div>',
        # 评论
        '<div class="comment-list"><div class="comment-item">'
        '<span class="comment-time" data-time="1790848857">2026年10月01日 18时00分</span>'
        "</div></div>",
        # 相关 / 推荐文章列表
        '<div class="related-list"><a href="/x">'
        '<span class="item-time" data-time="1790848857">2026年10月01日</span></a></div>',
        # 页脚
        '<footer><span class="publish-time" data-time="1790848857">2026年10月01日</span></footer>',
        # 站点头部（导航、栏目日期）
        '<header><span class="publish-time" data-time="1790848857">2026年10月01日</span></header>',
        # 正文 <article> 里的评论 / 相关 / 推荐模块：也有「相对文字 + 绝对属性」，但不认
        "<article><p>正文</p>"
        '<div class="comment-list"><span title="2026-10-01 10:00:00">3 天前</span></div></article>',
        "<article><p>正文</p>"
        '<div class="related-list"><span class="item-time" title="2026-10-01 10:00:00">3 天前</span>'
        "</div></article>",
        "<article><p>正文</p>"
        '<section class="recommend-box"><span title="2026-10-01 10:00:00">3 天前</span></section></article>',
        # 没有发布上下文、文字也不是相对时间：不认
        '<span data-time="1790848857">2026年10月01日</span>',
        '<div data-time="1790848857">场次 1790848857</div>',
        # 内联脚本里的模板串不算页面上的日期
        "<script>var t = '<span class=\"friendly_time\" data-time=\"1791453657\">"
        "2026年10月08日</span>';</script>",
        # 不是日期
        '<div class="publish-time" data-time="刚刚">刚刚</div>',
    ],
)
def test_other_page_dates_not_taken(markup: str) -> None:
    assert page_date.published_from_html(_page(markup)) == ""


def test_article_internal_comment_does_not_beat_the_real_publish_span() -> None:
    """正文里先出现一条评论的相对时间、后面才是发布时间位 → 取发布时间位。"""
    html = _page(
        "<article><p>正文</p>"
        '<div class="comment-list"><span title="2026-10-01 10:00:00">3 天前</span></div>'
        f"{CHUAPP_MARKUP}</article>"
    )
    assert page_date.published_from_html(html) == "2026-10-08"


def test_article_header_and_footer_are_publish_places() -> None:
    """`<article>` 自己的 header / footer（正文署名区）算发布时间位——负向只看标签名时放行。"""
    for body in (
        '<article><header><span class="publish-time" data-time="1791453657">2026年10月08日</span>'
        "</header><p>正文</p></article>",
        '<article><p>正文</p><footer><span class="publish-time" data-time="1791453657">'
        "2026年10月08日</span></footer></article>",
    ):
        assert page_date.published_from_html(_page(body)) == "2026-10-08"


def test_no_markup_still_empty() -> None:
    assert page_date.published_from_html(_page("<p>没有日期</p>")) == ""


def test_relative_only_page_reads_no_date_and_falls_back() -> None:
    """yystv 14479 那种页面（线上真实标记）：只有相对标签、没有任何绝对日期 → 返回 ""（不硬造）。

    这是边界样例，不是漏读：published_from_html 说「这天是哪天」读不到，下游按相对标签 +
    打开时刻换算（`relative_ts`），日期照样定得下来。
    """
    html = _page(
        '<div class="d-flex"><div class="c-999 f-12 shrink-0">22小时前 发布</div>'
        '<div class="doc-author"><span>22小时前</span></div></div><div class="doc-content">正文</div>'
    )
    assert page_date.published_from_html(html) == ""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    assert page_date.relative_ts("22小时前", opened) == pytest.approx(opened - 22 * 3600)
    assert page_date.relative_date("22小时前", opened) == "2026-10-07"


# ----------------------------------------------------------------------
# 一之补：真 fetch_page 走一遍（HTML → 摘要里的「页面发布日期」 → 下游按 task_id 取）
# ----------------------------------------------------------------------


class _FakeSearch:
    async def search(self, query, **kw):  # pragma: no cover - 这些用例不搜
        return []

    async def extract(self, url):  # pragma: no cover - 不走抓正文工具
        raise RuntimeError("不该走抓正文工具")


def _tools(store: Store, transport) -> Tools:
    settings, _ = load_settings({"groups": {"serve": [{"group": "qq:900000001"}]}})
    tools = Tools(store)
    register_builtin(
        tools,
        search=_FakeSearch(),
        profiles=FakeProfiles(),
        http_transport=transport,
        get_settings=lambda: settings,
        resolver=lambda host: ["93.184.216.34"],
    )
    return tools


def test_fetch_page_writes_markup_date_into_summary(tmp_path) -> None:
    """机核那种页面：代码在打开那一步就把属性上的发布日期读出来，落进 tool_calls 摘要。"""
    html = _page(f"<p>正文内容，够长。</p>{GCORES_MARKUP}")
    store = Store(tmp_path / "t.db")
    store.migrate()
    try:
        tools = _tools(store, httpx.MockTransport(
            lambda req: httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ))
        ctx = ToolContext(group_id="900000001", task_id="T-1", actor="子 agent #1", role="worker")
        r = asyncio.run(tools.call("fetch_page", {"url": URL}, ctx))
        assert r.ok
        assert (r.data or {}).get("published") == "2026-10-08"
        # 子 agent 在正文里也看得到（tools_builtin 用的措辞）
        assert "2026-10-08" in r.output and "发布时间" in r.output
        rows = store.read().execute(
            "SELECT tool, input, output, ok FROM tool_calls WHERE task_id='T-1'").fetchall()
        assert page_date.note("2026-10-08") in str(rows[0]["output"]), "落进摘要的标记"
        assert page_date.dates_from_rows(rows) == {"www.gcores.com/articles/220638": "2026-10-08"}
    finally:
        store.close()


# ----------------------------------------------------------------------
# 二、相对文字不许覆盖更精确的发布时间（resolve_dates 覆盖顺序）
# ----------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _log_fetch(store: Store, task_id: str, url: str, ts: float, *, date: str = "") -> None:
    out = "取到正文 500 字（直接打开）" + (page_date.note(date) if date else "")
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok)"
            " VALUES (?, '900000001', ?, '子 agent #1', 'fetch_page', ?, ?, 1)",
            (ts, task_id, url, out),
        )


def _resolve(store: Store, task_id: str, items: list[dict]) -> int:
    return page_date.resolve_dates(store, task_id, items, _parse_published)


def test_relative_window_units() -> None:
    """相对标签隐含的区间：数字标签按单位向下取整，日历词按北京时间的「天」。"""
    base = _bj(2026, 10, 8, 19, 19, 41)
    assert page_date.relative_window("1 小时前", base) == (base - 7200, base - 3600)
    assert page_date.relative_window("2 天前", base) == (base - 3 * 86400, base - 2 * 86400)
    assert page_date.relative_window("30 分钟前", base) == (base - 1860, base - 1800)
    # 「昨天」是日历概念，不是「base − 24h ± 一天」
    assert page_date.relative_window("昨天", base) == (_bj(2026, 10, 7, 0, 0), _bj(2026, 10, 8, 0, 0))
    assert page_date.relative_window("前天", base) == (_bj(2026, 10, 6, 0, 0), _bj(2026, 10, 7, 0, 0))
    assert page_date.relative_window("yesterday", base) == (_bj(2026, 10, 7, 0, 0), _bj(2026, 10, 8, 0, 0))
    # 说不出具体时刻的、以及绝对日期 → 没有区间
    assert page_date.relative_window("刚刚", base) is None
    assert page_date.relative_window("今天", base) is None
    assert page_date.relative_window("2026-10-01", base) is None


def test_calendar_window_endpoints_are_half_open() -> None:
    """日历词覆盖的是「那一整天」：`[昨天 00:00, 今天 00:00)`。

    页面 / 搜索结果给的日期常归一成当天 00:00，所以日历词的左端必须闭、右端必须开
    （数字标签是滚动区间，右闭）。
    """
    base = _bj(2026, 10, 8, 19, 19, 41)
    assert page_date.relative_contains("昨天", _bj(2026, 10, 7, 0, 0), base)
    assert page_date.relative_contains("昨天", _bj(2026, 10, 7, 23, 59, 59), base)
    assert not page_date.relative_contains("昨天", _bj(2026, 10, 8, 0, 0), base)
    assert not page_date.relative_contains("昨天", _bj(2026, 10, 6, 23, 59, 59), base)
    assert page_date.relative_contains("前天", _bj(2026, 10, 6, 0, 0), base)
    assert not page_date.relative_contains("前天", _bj(2026, 10, 7, 0, 0), base)
    assert page_date.relative_contains("1 小时前", base - 3600, base)
    assert not page_date.relative_contains("1 小时前", base - 7200, base)
    assert not page_date.relative_contains("刚刚", base, base)


def test_yesterday_midnight_evidence_is_kept(store) -> None:
    """归一成 00:00 的「昨天」证据要保留（不能被左开端点排掉）。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:y:0", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": "昨天",
            "published_ts": _bj(2026, 10, 7, 0, 0), "date_basis": "search"}
    _resolve(store, "feeds-verify:y:0", [item])
    assert item["published_ts"] == _bj(2026, 10, 7, 0, 0)
    assert item["date_basis"] == "search"


def test_today_midnight_evidence_is_not_yesterday(store) -> None:
    """今天 00:00 不是「昨天」，不许被右闭端点留下。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:y:1", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": "昨天",
            "published_ts": _bj(2026, 10, 8, 0, 0), "date_basis": "search"}
    _resolve(store, "feeds-verify:y:1", [item])
    assert item["published_ts"] == page_date.relative_ts("昨天", opened)
    assert item["date_basis"] == "relative"


def test_relative_ts_unchanged_for_calendar_words() -> None:
    """加了区间之后，换算本身不变（昨天 = 减一天、刚刚/今天 = 不减）。"""
    base = _bj(2026, 10, 8, 19, 19, 41)
    assert page_date.relative_ts("昨天", base) == base - 86400
    assert page_date.relative_ts("前天", base) == base - 2 * 86400
    assert page_date.relative_ts("刚刚", base) == base
    assert page_date.relative_ts("今天", base) == base


def test_relative_does_not_clobber_precise_search_ts_in_its_bucket(store) -> None:
    """线上 gcores 220638：页面 title 的 17:52:36 被「1 小时前」按打开时刻 19:19:41 覆盖成 18:19:41。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    precise = _bj(2026, 10, 8, 17, 52, 36)
    _log_fetch(store, "feeds-verify:a:1", URL, opened)  # 这轮打开过，但代码没读到页面日期
    item = {"title": "太吾异世劫", "url": URL, "published_raw": "1 小时前",
            "published_ts": precise, "date_basis": "search"}
    _resolve(store, "feeds-verify:a:1", [item])
    assert item["published_ts"] == precise, "相对标签是粗粒度估计，不许覆盖同一区间里更精确的时间"
    assert item["date_basis"] == "search"
    assert item["published_raw"] == "1 小时前"


def test_page_html_date_beats_relative(store) -> None:
    """页面 HTML 读到的日期（新读法：data-time / title）优先于相对文字。

    这条同时钉住「页面读到的只是哪一天」：已有精确到秒的可信时间又正好同一天时，
    保留秒级值、别降到当天 00:00（依据仍记 page，表示这一天被页面确认过）。
    """
    opened = _bj(2026, 10, 8, 19, 19, 41)
    precise = _bj(2026, 10, 8, 17, 52, 36)
    _log_fetch(store, "feeds-verify:a:2", URL, opened, date="2026-10-08")
    item = {"title": "太吾异世劫", "url": URL, "published_raw": "1 小时前",
            "published_ts": precise, "date_basis": "search"}
    _resolve(store, "feeds-verify:a:2", [item])
    assert item["date_basis"] == "page"
    assert item["published_ts"] == precise, "同一天的精确秒级时间别被页面日期降成 00:00"


def test_page_date_overrides_precise_ts_on_another_day(store) -> None:
    """精确时间与页面日期不是同一天（搜索结果常给成抓取/索引日）→ 以页面为准。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:21", URL, opened, date="2026-10-08")
    item = {"title": "t", "url": URL, "published_raw": "2026-10-01",
            "published_ts": _bj(2026, 10, 1, 9, 0), "date_basis": "search"}
    _resolve(store, "feeds-verify:a:21", [item])
    assert item["date_basis"] == "page"
    assert page_date.normalize_date(clock.bj(item["published_ts"])) == "2026-10-08"


def test_page_date_overrides_wrong_model_ts_even_same_day(store) -> None:
    """模型自己猜的时间不是证据：同一天也照旧被页面日期覆盖（不许拿它保精度）。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:22", URL, opened, date="2026-10-08")
    item = {"title": "t", "url": URL, "published_raw": "2026-10-08",
            "published_ts": _bj(2026, 10, 8, 8, 0), "date_basis": "model"}
    _resolve(store, "feeds-verify:a:22", [item])
    assert item["date_basis"] == "page"
    assert item["published_ts"] == _parse_published("2026-10-08")


@pytest.mark.parametrize(
    "label,offset_min,kept",
    [
        # 线上那一条：19:19:41 打开、页面写「1 小时前」→ 区间 (18:19:41−1h, 18:19:41]
        ("1 小时前", -87, True),   # 17:52:36 在区间里 → 保留精确值
        ("1 小时前", -60, True),   # 正好是区间上界（右闭）
        ("1 小时前", -120, False),  # 正好是区间下界（左开）：那是「2 小时前」
        ("1 小时前", -59, False),  # 比标签新 1 分钟：页面说至少 1 小时前，不可能
        ("1 小时前", -9, False),   # 比标签新 51 分钟：不能保留
        ("1 小时前", -119, True),  # 1h59m 前，还在 (2h, 1h] 里
        ("1 小时前", -121, False),  # 2h01m 前，超出下界：那是「2 小时前」
        ("3 小时前", -200, True),  # 3h20m 前在 (4h, 3h] 里
        ("3 小时前", -170, False),  # 2h50m 前比标签新
        ("3 小时前", -300, False),  # 正好是下界（左开）：那是「4 小时前」
    ],
)
def test_precise_ts_kept_only_inside_the_label_window(store, label, offset_min, kept) -> None:
    """保留精确值的条件是「落在相对标签自己的粒度区间」——向下取整，左开右闭。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    ts = opened + offset_min * 60
    _log_fetch(store, "feeds-verify:w:0", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": label, "published_ts": ts, "date_basis": "search"}
    _resolve(store, "feeds-verify:w:0", [item])
    if kept:
        assert item["published_ts"] == ts and item["date_basis"] == "search"
    else:
        assert item["published_ts"] == page_date.relative_ts(label, opened)
        assert item["date_basis"] == "relative"


def test_yesterday_uses_calendar_window(store) -> None:
    """「昨天」按日历解释：北京时间的昨天那一整天，不是「anchor − 24h ± 一天」。"""
    opened = _bj(2026, 10, 8, 10, 0)
    url_y, url_o = "https://a.example/y", "https://a.example/o"
    _log_fetch(store, "feeds-verify:c:0", url_y, opened)
    _log_fetch(store, "feeds-verify:c:0", url_o, opened)
    inside = {"title": "t", "url": url_y, "published_raw": "昨天",
              "published_ts": _bj(2026, 10, 7, 9, 0), "date_basis": "search"}
    outside = {"title": "t", "url": url_o, "published_raw": "昨天",
               "published_ts": _bj(2026, 10, 6, 20, 0), "date_basis": "search"}
    _resolve(store, "feeds-verify:c:0", [inside, outside])
    assert inside["published_ts"] == _bj(2026, 10, 7, 9, 0), "昨天 09:00 在「昨天」区间里"
    assert page_date.normalize_date(clock.bj(outside["published_ts"])) == "2026-10-07"
    assert outside["date_basis"] == "relative", "前天 20:00 不是「昨天」，不许当精确值留着"


def test_feeds_search_fallback_precise_ts_survives(store) -> None:
    """照 feeds.py:3707-3717 的真实接线走一遍：核验交回的 published 是相对文字（`_parse_published`
    认不出 → published_ts 不是数字），撒网候选自带的秒级 src.published 补回来并标 date_basis="search"，
    published_raw 原样留着相对文字；随后 `_fill_dates_from_pages` → resolve_dates。

    搜索结果给的日期也只是**外部证据**（可能是抓取/索引日），所以它只在相对标签自己的粒度内作数；
    粒度对不上时仍旧让页面上活着的相对标签覆盖它（见上一条用例）。
    """
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:7", URL, opened)
    item = {"title": "太吾异世劫", "url": URL, "published_raw": "1 小时前",
            "published_ts": _parse_published("1 小时前")}
    assert item["published_ts"] is None, "相对文字解析不出时间戳"
    src_published = _bj(2026, 10, 8, 17, 52, 36)  # 撒网候选自带的秒级时间（= 页面 title 的 17:52:36）
    if not isinstance(item.get("published_ts"), (int, float)):
        item["published_ts"] = float(src_published)
        item["date_basis"] = "search"
    _resolve(store, "feeds-verify:a:7", [item])
    assert item["published_ts"] == src_published
    assert item["date_basis"] == "search"
    assert item["published_raw"] == "1 小时前"


def test_model_guessed_ts_still_loses_to_relative(store) -> None:
    """模型自己猜的时间（basis 是 model）不算精确证据，页面上活着的相对标签照旧赢。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:3", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": "1 小时前",
            "published_ts": opened - 3600 + 600, "date_basis": "model"}
    _resolve(store, "feeds-verify:a:3", [item])
    assert item["published_ts"] == pytest.approx(opened - 3600)
    assert item["date_basis"] == "relative"


def test_unbased_ts_still_loses_to_relative(store) -> None:
    """没有依据的旧时间（核验刚交回、basis 空）同样不算证据。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:4", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": "3 小时前",
            "published_ts": opened - 3 * 3600 + 120}
    _resolve(store, "feeds-verify:a:4", [item])
    assert item["published_ts"] == pytest.approx(opened - 3 * 3600)
    assert item["date_basis"] == "relative"


def test_search_ts_far_from_relative_still_loses(store) -> None:
    """搜索结果日期和相对标签差出粒度（几天）时，以页面上活着的相对标签为准。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:5", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": "2 天前",
            "published_ts": opened - 5 * 86400, "date_basis": "search"}
    _resolve(store, "feeds-verify:a:5", [item])
    assert item["published_ts"] == pytest.approx(opened - 2 * 86400)
    assert item["date_basis"] == "relative"


def test_precise_ts_kept_counts_as_unchanged(store) -> None:
    """保留精确值时不算「代码改了日期」（count 语义：被代码改过 / 补上的条数）。"""
    opened = _bj(2026, 10, 8, 19, 19, 41)
    _log_fetch(store, "feeds-verify:a:6", URL, opened)
    item = {"title": "t", "url": URL, "published_raw": "1 小时前",
            "published_ts": _bj(2026, 10, 8, 17, 52, 36), "date_basis": "search"}
    assert _resolve(store, "feeds-verify:a:6", [item]) == 0
