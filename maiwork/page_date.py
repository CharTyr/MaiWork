"""代码从网页读发布日期（docs/10-资讯流水线改进计划.md §九 第一步 2，2026-10-05）。

线上现象：好文（kind=guide）因为「文章没有发布时间，宁缺毋滥不收」被硬拒
（222 篇好文里 6 篇，含机核长文 2 篇）——那是子 agent 在页面上没找到可见日期。
规则不放宽，改成代码自己读：

- 打开网页那一步（tools_builtin.fetch_page 拿到原始 HTML）解析 meta（article:published_time、
  og:published_time、datePublished、pubdate、date、DC.date……）、JSON-LD（datePublished /
  dateCreated）、`<time datetime="…">`；
- 读到就把日期写进 fetch_page 的 tool_calls 摘要（`（页面发布日期：YYYY-MM-DD）`），
  子 agent 也直接在正文里看得到；
- 下游（feeds 核验 / news_recheck 补打开）用它定 `published_ts`，「必须有日期」照旧不放宽。

2026-10-08（同一篇机核播客页相隔 9 分钟核验两次存成 10-06 / 10-07）：`resolve_dates` 改成
代码读到的页面日期**覆盖**模型给的；模型只看到相对时间（「3 小时前」）就原样抄回，代码按这次
打开的时间（tool_calls.ts）换算（`relative_ts`）；依据记在 item["date_basis"]
（page / relative / model / search / ""）。

2026-10-08 第三轮线上巡检（`.local-checks/audit-round3-news`，线上 4 个真实页面本地复现）
查出两处缺口，本模块一并收口，**都只改读取结构、不放宽规则**：

1. 读得到的面太窄：触乐把发布日期挂在
   `<span class="fn-right friendly_time" data-time="1791453657">2026年10月08日 18时00分</span>`、
   机核挂在 `<span class="me-2 u_color-gray-info" title="2026-10-08 17:52:36">5 小时前</span>`，
   两站都没有 meta / JSON-LD / `<time datetime>`，于是这四个样本全读成空，只能退回相对时间估算。
   现在加一条排在 meta → JSON-LD → `<time>` **之后**的读法（`_attr_date`）：只认
   ① 属性名本身就是日期语义的（data-time / data-date / data-published…），
   ② `title` 而元素的可见文字就是相对时间标签（「5 小时前」——「相对文字 + 绝对属性」是
   发布时间展示位的常见写法）；两者都还要过一道「发布上下文」闸（自己 / 祖先容器的类名像
   发布时间，或可见文字是相对时间），评论、推荐、作者档案、页脚、以及任意 span 的 title 都不认。
   **四个样本里三个**（chuapp 291676、gcores 220636 / 220638）是「属性上有绝对日期、以前没被
   读到」，新读法各读出 2026-10-08；**第四个 yystv 14479 是另一码事**：页面上只有
   「22小时前 发布」这类相对标签，没有任何绝对日期字段（无 meta / 无 data-time / 无带日期的
   title），按设计照旧返回 ""——不硬造日期，下游按相对标签换算（合理回落，不是漏读）。
2. 相对时间不许覆盖更精确的时间：相对标签是向下取整的粗粒度估计（「1 小时前」= 1h00m–1h59m）。
   已有**可信**时间（依据是 page / search，不是模型自己猜的）落在它自己那个粒度区间里时，
   `resolve_dates` 保留精确值（`relative_window` / `relative_contains`：数字标签是滚动窗口、
   左开右闭；「昨天 / 前天」说的是那一整天、左闭右开）——线上 gcores 220638 的页面 title
   写着 17:52:36，被「1 小时前」按打开时刻 19:19:41 覆盖成 18:19:41，晚了 27 分钟
   （同批 220636 留住了页面精确值，两条口径不一致）；
   模型自己猜的时间（basis 是 model / 空）不算证据，照旧被相对标签覆盖。
   页面读到的只是「哪一天」：它和已有秒级可信时间同一天时同样保留秒级值（依据记 page）。
   搜索结果给的日期一样只是外部证据（可能是抓取 / 索引日期），所以只在粒度对得上时作数。

日期统一归一成 `YYYY-MM-DD`（够判几天的时效，又不用纠缠时区）：`published_from_html` /
`note` / `dates_from_rows` 这条线上只有天；item 里已有的秒级 `published_ts` 在上述保留分支里
不会被降成当天 00:00。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

logger = logging.getLogger("maiwork.page_date")

PAGE_DATE_LABEL = "页面发布日期"

_DATE_RE = re.compile(r"((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_LABEL_RE = re.compile(PAGE_DATE_LABEL + r"[:：]\s*(\d{4}-\d{2}-\d{2})")
# 抓正文工具（Jina / MCP）的纯文本里常见的日期行
_TEXT_DATE_RE = re.compile(
    r"(?:published[_\s-]?time|publish(?:ed)?[_\s-]?date|发布时间|发布日期|datePublished)"
    r"\s*[:：=]\s*\"?((?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}[^\s\"<>]*)",
    re.IGNORECASE,
)

# meta 的键 → 优先级（数字小优先）：<meta property/name/itemprop> 都算
_META_DATE_KEYS = {
    "article:published_time": 0,
    "og:published_time": 1,
    "og:article:published_time": 1,
    "article:published": 1,
    "article.published": 1,
    "datepublished": 2,
    "publishdate": 3,
    "publish_date": 3,
    "pubdate": 3,
    "parsely-pub-date": 4,
    "bytedance:published_time": 4,
    "weibo:article:create_at": 4,
    "dc.date.issued": 4,
    "dc.date": 4,
    "date": 5,
    "sailthru.date": 5,
}

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))"
)
_LD_JSON_RE = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_TIME_RE = re.compile(r"<time\b([^>]*)>", re.IGNORECASE)
_ARTICLE_RE = re.compile(r"<article\b[^>]*>.*?</article>", re.IGNORECASE | re.DOTALL)

# ----------------------------------------------------------------------
# 元素属性上的发布日期（2026-10-08 第三轮线上巡检 §4.4 缺陷 2；只做结构保守的扩展）
#
# 只认两种结构：
#   ① 属性名本身就是日期语义的（data-time / data-date / data-published / …）；
#   ② `title` 而元素的可见文字就是相对时间标签（「5 小时前」）——「相对文字 + 绝对属性」
#      是发布时间展示位的常见写法（机核）。
# 两者都还要过「发布上下文」闸：自己或祖先容器的类名像发布时间，或可见文字是相对时间。
# 评论 / 推荐 / 作者档案 / 页脚 / 任意 span 的 title 一律不认（不抓页面上随便一个日期）。
# ----------------------------------------------------------------------

# 属性名 → 优先级（数字小优先；只列名字本身说清楚是时间/日期的）
_ATTR_DATE_KEYS = {
    "data-time": 0,
    "data-datetime": 0,
    "data-published": 1,
    "data-published-time": 1,
    "data-pubtime": 1,
    "data-pub-time": 1,
    "data-post-time": 1,
    "data-post-date": 1,
    "data-date": 2,
    "data-timestamp": 2,
}
_TITLE_RANK = 3  # title 上的日期优先级最低（只在可见文字是相对时间时才算）
_ATTR_DATE_ORDER = tuple(sorted(_ATTR_DATE_KEYS.items(), key=lambda kv: kv[1]))

# 类名 / id 里出现这些字样 = 这个元素（或它的容器）是「文章发布时间」的展示位
_PUBLISH_HINT_RE = re.compile(
    r"publish|pubdate|pub[_-]?time|post[_-]?(time|date)|article[_-]?(time|date)|entry[_-]?date|"
    r"friendly[_-]?time|author[_-]?time|meta[_-]?time|date[_-]?time|release[_-]?time|time[_-]?info",
    re.IGNORECASE,
)
# 这些容器里的日期是评论 / 推荐 / 作者档案 / 页脚……不是这篇文章的发布时间
# （类名 / id 里的字样：正文 <article> 里也不认——正文里的评论、相关阅读模块同样带日期）
_OTHER_DATE_HINT_RE = re.compile(
    r"comment|reply|replies|recommend|related|hot[_-]?list|rank|sidebar|aside|footer|nav|"
    r"breadcrumb|avatar|profile|original[_-]?created|tag[_-]?list|share|copyright",
    re.IGNORECASE,
)
# 这些**标签**在文章外面时是站点头尾（导航 / 版权区），里面的日期不算正文发布时间；
# 在 <article> 里面则是文章自己的署名区（`<article><header>` / `<article><footer>`），照认。
_OTHER_DATE_TAGS = frozenset({"aside", "footer", "nav", "header", "form"})

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][-a-zA-Z0-9]*)([^>]*)>")
# 剥掉脚本 / 样式 / 注释：内联模板串（'<span data-time="…">'）不是页面上的日期
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL)
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
})


def note(date: str) -> str:
    """fetch_page 摘要里的日期标记（下游 dates_from_rows 按它取）。"""
    return f"（{PAGE_DATE_LABEL}：{date}）"


def normalize_date(value: Any) -> str:
    """各种日期写法 → `YYYY-MM-DD`；认不出 → ""。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit() and len(raw) >= 10:  # epoch 秒
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    m = _DATE_RE.search(raw)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return ""
    try:  # RFC 2822：Mon, 28 Sep 2026 10:00:00 GMT
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return ""
    return dt.strftime("%Y-%m-%d") if dt else ""


def date_from_summary(text: str) -> str:
    """从 fetch_page 的摘要里取日期标记；没有 → ""。"""
    m = _LABEL_RE.search(str(text or ""))
    return m.group(1) if m else ""


def published_from_text(text: str) -> str:
    """从抓正文工具的纯文本里找日期行（Published Time: … / 发布时间：…）。"""
    m = _TEXT_DATE_RE.search(str(text or ""))
    return normalize_date(m.group(1)) if m else ""


def _attrs(tag_body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag_body or ""):
        name = m.group(1).lower()
        value = m.group(2) or m.group(3) or m.group(4) or ""
        out.setdefault(name, value.strip())
    return out


def _meta_date(html: str) -> str:
    best: tuple[int, str] | None = None
    for m in _META_TAG_RE.finditer(html):
        attrs = _attrs(m.group(0))
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").strip().lower()
        rank = _META_DATE_KEYS.get(key)
        if rank is None:
            continue
        date = normalize_date(attrs.get("content") or attrs.get("value") or "")
        if not date:
            continue
        if best is None or rank < best[0]:
            best = (rank, date)
    return best[1] if best else ""


def _walk_ld(node: Any, keys: tuple[str, ...]) -> str:
    if isinstance(node, dict):
        for key in keys:
            for k, v in node.items():
                if str(k).lower() == key and isinstance(v, str):
                    date = normalize_date(v)
                    if date:
                        return date
        for v in node.values():
            date = _walk_ld(v, keys)
            if date:
                return date
    elif isinstance(node, list):
        for v in node:
            date = _walk_ld(v, keys)
            if date:
                return date
    return ""


def _json_ld_date(html: str) -> str:
    for m in _LD_JSON_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            date = _walk_ld(parsed, ("datepublished", "datecreated"))
            if date:
                return date
            continue
        for key in ("datePublished", "dateCreated"):  # JSON 坏了也 regex 捞一把
            mm = re.search(r"\"%s\"\s*:\s*\"([^\"]+)\"" % key, raw)
            if mm:
                date = normalize_date(mm.group(1))
                if date:
                    return date
    return ""


def _article_bounds(text: str) -> tuple[int, int] | None:
    m = _ARTICLE_RE.search(text)
    return (m.start(), m.end()) if m else None


def _time_date(html: str) -> str:
    span = _article_bounds(html)
    scored: list[tuple[int, int, str]] = []
    for i, m in enumerate(_TIME_RE.finditer(html)):
        attrs = _attrs(m.group(1))
        date = normalize_date(attrs.get("datetime") or attrs.get("content") or "")
        if not date:
            continue
        raw_low = m.group(1).lower()
        if "pubdate" in raw_low or (attrs.get("itemprop") or "").lower() == "datepublished":
            rank = 0
        elif span and span[0] <= m.start() <= span[1]:
            rank = 1
        else:
            rank = 2
        scored.append((rank, i, date))
    if not scored:
        return ""
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def _element_text(text: str, pos: int) -> str:
    """开标签后面的可见文字（到下一个标签为止）。"""
    end = text.find("<", pos)
    return text[pos:end if end >= 0 else len(text)].strip()


def _is_relative_label(text: str) -> bool:
    """这段文字本身就是一个相对时间标签（「5 小时前」/「3 hours ago」）。"""
    return bool(text) and relative_ts(text, 0.0) is not None


def _tag_parents(text: str) -> list[tuple[int, int, str, dict[str, str], int]]:
    """顺序扫开 / 闭标签 → [(标签起点, 标签终点, 名字, 属性, 父下标)]。

    HTML 不严格闭合也能用：闭标签只往回找同名开标签（找不到就忽略）。
    有了父链才知道一个元素**真正**在哪个块里，不会被前面已经闭合的兄弟块（机核的
    `<a class="avatar">`）带偏。
    """
    out: list[tuple[int, int, str, dict[str, str], int]] = []
    stack: list[int] = []
    for m in _TAG_RE.finditer(text):
        name = m.group(2).lower()
        body = m.group(3)
        if m.group(1) == "/":
            for i in range(len(stack) - 1, -1, -1):
                if out[stack[i]][2] == name:
                    del stack[i:]
                    break
            continue
        out.append((m.start(), m.end(), name, _attrs(body), stack[-1] if stack else -1))
        if name not in _VOID_TAGS and not body.rstrip().endswith("/"):
            stack.append(len(out) - 1)
    return out


def _ancestors(tags: list, index: int, depth: int = 4) -> list[tuple[str, str]]:
    """这个标签往上 4 层祖先的 (标签名, "class id")（判断「它在哪个块里」）。"""
    out: list[tuple[str, str]] = []
    parent = tags[index][4]
    while parent >= 0 and depth > 0:
        _start, _end, name, attrs, _p = tags[parent]
        out.append((name, f"{attrs.get('class', '')} {attrs.get('id', '')}"))
        parent = tags[parent][4]
        depth -= 1
    return out


def _attrs_date_value(attrs: dict[str, str], text: str, pos: int) -> tuple[str, int]:
    """这个标签的属性里有没有明确的发布日期 → (YYYY-MM-DD, 优先级)；没有 → ("", 优先级)。"""
    for name, rank in _ATTR_DATE_ORDER:
        raw = attrs.get(name)
        if not raw:
            continue
        date = normalize_date(raw)
        if date:
            return date, rank
    title = attrs.get("title")
    if title and _is_relative_label(_element_text(text, pos)):
        date = normalize_date(title)
        if date:
            return date, _TITLE_RANK
    return "", _TITLE_RANK


def _attr_date(html: str) -> str:
    """元素属性上的发布日期（data-time / data-date… / 可见文字是相对时间的 title）。

    排在 meta → JSON-LD → `<time datetime>` 之后：那三样是更明确的结构化字段。
    读不到 / 只有评论、推荐、作者档案、页脚里的日期 → ""（宁可退回相对时间，也不乱认）。
    """
    text = _SCRIPT_RE.sub(" ", str(html or ""))
    if not text:
        return ""
    span = _article_bounds(text)
    tags = _tag_parents(text)
    scored: list[tuple[int, int, int, str]] = []
    for i, (start, end, _name, attrs, _parent) in enumerate(tags):
        date, rank = _attrs_date_value(attrs, text, end)
        if not date:
            continue
        own = f"{attrs.get('class', '')} {attrs.get('id', '')}"
        if _OTHER_DATE_HINT_RE.search(own):
            continue
        in_article = bool(span and span[0] <= start <= span[1])
        anc = _ancestors(tags, i)
        anc_marks = " ".join(m for _n, m in anc)
        if _OTHER_DATE_HINT_RE.search(anc_marks):
            continue  # 评论 / 相关阅读 / 作者档案这类容器里的日期，正文 <article> 里也不认
        if not in_article and any(name in _OTHER_DATE_TAGS for name, _m in anc):
            continue  # 站点头尾（导航 / 版权区）里的日期不算这篇文章的发布时间
        marks = f"{own} {anc_marks}"
        if not (_PUBLISH_HINT_RE.search(marks) or _is_relative_label(_element_text(text, end))):
            continue
        scored.append((rank, 0 if in_article else 1, i, date))
    if not scored:
        return ""
    scored.sort()
    return scored[0][3]


def published_from_html(html: str) -> str:
    """从 HTML 里读发布日期（meta → JSON-LD → `<time>` → 元素属性）；没有 → ""。"""
    text = str(html or "")
    if not text:
        return ""
    return _meta_date(text) or _json_ld_date(text) or _time_date(text) or _attr_date(text)


def _opened_row(row: Any) -> tuple[list[str], str] | None:
    """一条 tool_calls 记录若是「真打开过原文」的成功调用 → (规范化链接们, 摘要)；否则 None。

    只认 fetch_page（请求地址 + 摘要里的「最终地址」）和扩展抓正文工具（input 的 url / urls / link），
    和 opened_urls_from_rows 同一份判定。
    """
    from .coordinator import normalize_link_for_check
    from .tools_builtin import _is_extract_like_mcp, _urls_from_json_input, final_url_from_summary

    try:
        tool = str(row["tool"] or "")
        ok = int(row["ok"] or 0)
        raw_input = str(row["input"] or "")
        output = str(row["output"] or "")
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not ok:
        return None
    urls: list[str] = []
    if tool == "fetch_page":
        urls.extend(_urls_from_json_input(raw_input))
        final = final_url_from_summary(output)
        if final:
            urls.append(final)
    elif _is_extract_like_mcp(tool):
        urls.extend(_urls_from_json_input(raw_input))
    else:
        return None
    keys = [k for k in (normalize_link_for_check(u) for u in urls) if k]
    return keys, output


def dates_from_rows(rows: Any) -> dict[str, str]:
    """tool_calls 记录（dict 行，键 tool/input/output/ok）→ {规范化链接: YYYY-MM-DD}。

    日期取摘要里的「页面发布日期」标记，其次纯文本里的日期行。
    """
    out: dict[str, str] = {}
    for row in rows or []:
        got = _opened_row(row)
        if got is None:
            continue
        keys, output = got
        date = date_from_summary(output) or published_from_text(output)
        if not date:
            continue
        for key in keys:
            out.setdefault(key, date)
    return out


def fetch_times_from_rows(rows: Any) -> dict[str, float]:
    """tool_calls 记录（还要 ts 键）→ {规范化链接: 这轮最后一次成功打开它的时间}。

    相对时间（「3 小时前」）按这个时间换算：模型看到的是打开那一刻的页面。
    """
    out: dict[str, float] = {}
    for row in rows or []:
        got = _opened_row(row)
        if got is None:
            continue
        try:
            ts = float(row["ts"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        for key in got[0]:
            if ts > out.get(key, 0.0):
                out[key] = ts
    return out


def _rows(store: Any, task_id: str) -> list:
    return store.read().execute(
        "SELECT ts, tool, input, output, ok FROM tool_calls WHERE task_id=?", (str(task_id),)
    ).fetchall()


def page_dates(store: Any, task_id: str) -> dict[str, str]:
    """这个 task_id 下代码从网页读到的发布日期；读不到 / 出错 → {}（不挡流程）。"""
    try:
        rows = _rows(store, task_id)
    except Exception:
        logger.debug("读打开记录失败（%s）", task_id, exc_info=True)
        return {}
    return dates_from_rows(rows)


# ----------------------------------------------------------------------
# 相对时间（2026-10-08 线上问题：同一篇机核播客页相隔 9 分钟核验两次，
# 模型看到「3 小时前」分别自己换算成 10-06 和 10-07）——改成模型原样抄、代码换算。
# ----------------------------------------------------------------------

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "几": 3}
_UNIT_SECONDS = {
    "秒": 1, "分钟": 60, "分": 60, "小时": 3600, "个小时": 3600, "钟头": 3600, "个钟头": 3600,
    "天": 86400, "日": 86400, "周": 7 * 86400, "星期": 7 * 86400, "个星期": 7 * 86400,
    "个月": 30 * 86400, "月": 30 * 86400, "年": 365 * 86400,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 7 * 86400, "wk": 7 * 86400, "wks": 7 * 86400, "week": 7 * 86400, "weeks": 7 * 86400,
    "mo": 30 * 86400, "month": 30 * 86400, "months": 30 * 86400,
    "y": 365 * 86400, "yr": 365 * 86400, "yrs": 365 * 86400, "year": 365 * 86400, "years": 365 * 86400,
}
_CN_REL_RE = re.compile(
    r"(\d+|[一两二三四五六七八九十几]+|半)\s*(个小时|个钟头|小时|钟头|分钟|分|秒|天|日|个星期|星期|周|个月|月|年)\s*(?:以?前|之前)"
)
_EN_REL_RE = re.compile(
    r"\b(\d+|an?|one)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|years?|yrs?|"
    r"[smhdwy]|mo)\s+ago\b",
    re.IGNORECASE,
)
_WORDS: tuple[tuple[re.Pattern, int, str], ...] = (
    (re.compile(r"大前天"), 3 * 86400, "day"),
    (re.compile(r"前天|day before yesterday", re.IGNORECASE), 2 * 86400, "day"),
    (re.compile(r"昨天|昨日|\byesterday\b", re.IGNORECASE), 86400, "day"),
    (re.compile(r"刚刚|刚才|\bjust now\b|\bmoments? ago\b", re.IGNORECASE), 0, "now"),
    (re.compile(r"今天|今日|\btoday\b", re.IGNORECASE), 0, "now"),
)


def _cn_number(text: str) -> float | None:
    if text == "半":
        return 0.5
    if text.isdigit():
        return float(text)
    if text in _CN_NUM:
        return float(_CN_NUM[text])
    if text.startswith("十") and len(text) == 2 and text[1] in _CN_NUM:  # 十二
        return 10.0 + _CN_NUM[text[1]]
    if len(text) in (2, 3) and text[0] in _CN_NUM and text[1] == "十":  # 二十 / 二十三
        return _CN_NUM[text[0]] * 10.0 + (_CN_NUM.get(text[2], 0) if len(text) == 3 else 0)
    return None


def _relative_offset(text: Any) -> tuple[float, float, str] | None:
    """相对时间文字 → (要往前推的秒数, 这种写法的粒度秒数, 种类)；不是相对时间 → None。

    种类：
    - "span" —— 数字标签（「1 小时前」「3 hours ago」）：向下取整，真值落在
      base−(n+1)·单位 到 base−n·单位 之间；
    - "day" —— 日历词（「昨天 / 前天 / 大前天」）：按北京时间的「天」解释，粒度一天；
    - "now" —— 「刚刚 / 今天」：粒度 0（说不出具体时刻，只能当「就是那时」）。
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw or len(raw) > 40 or _DATE_RE.search(raw):
        return None
    m = _CN_REL_RE.search(raw)
    if m:
        n = _cn_number(m.group(1))
        unit = _UNIT_SECONDS.get(m.group(2))
        if n is not None and unit:
            return n * unit, float(unit), "span"
    m = _EN_REL_RE.search(raw)
    if m:
        word = m.group(1).lower()
        n = 1.0 if word in ("a", "an", "one") else float(word)
        unit = _UNIT_SECONDS.get(m.group(2).lower())
        if unit:
            return n * unit, float(unit), "span"
    for pat, seconds, kind in _WORDS:
        if pat.search(raw):
            return float(seconds), float(seconds), kind
    return None


def relative_ts(text: Any, base_ts: float) -> float | None:
    """页面上的相对时间文字 → 时间戳（以 base_ts 为「那一刻」）；不是相对时间 → None。

    认：「3 小时前」「三小时前」「半小时前」「2 天前」「1 周前」「昨天」「前天」「刚刚」「今天」、
    「3 hours ago」「an hour ago」「2d ago」「yesterday」「just now」「today」。
    带绝对日期的、太长的（不像一个日期字段）、「…后」都不认。
    """
    offset = _relative_offset(text)
    return float(base_ts) - offset[0] if offset is not None else None


def relative_window(text: Any, base_ts: float) -> tuple[float, float] | None:
    """相对时间文字 → 它隐含的发布时间区间（端点约定见 `relative_contains`）；不是相对时间 → None。

    - 数字标签是向下取整：「base 时刻写 1 小时前」= 发布在 (base−2h, base−1h]；
    - 日历词按北京时间的「天」：「昨天」= [昨天 00:00, 今天 00:00)；
    - 「刚刚 / 今天」说不出具体时刻（粒度 0）→ None。

    `relative_ts` 仍旧是「base 减 n 个单位」的粗估计，不按日历改——按日历只管证据取舍那一步。
    """
    from . import clock

    parsed = _relative_offset(text)
    if parsed is None:
        return None
    seconds, unit, kind = parsed
    if kind == "day":
        days = max(1, int(round(seconds / 86400.0)))
        midnight = clock.bj(base_ts).replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight - timedelta(days=days)).timestamp(), (midnight - timedelta(days=days - 1)).timestamp()
    if unit <= 0:
        return None
    return float(base_ts) - seconds - unit, float(base_ts) - seconds


def relative_contains(text: Any, ts: float, base_ts: float) -> bool:
    """这个相对时间标签隐含的区间包不包含 ts？（相对标签的端点约定都在这里）

    - 数字标签（「1 小时前」）是滚动窗口，**右闭左开**：`base−(n+1)·单位 < ts ≤ base−n·单位`；
    - 日历词（「昨天 / 前天」）说的是「那一整天」，**左闭右开**：`[昨天 00:00, 今天 00:00)`。
      页面 / 搜索结果给的日期常归一成当天 00:00，左开会把「昨天 00:00」排掉，右闭又会把
      「今天 00:00」当成昨天——两个都错。
    - 「刚刚 / 今天」以及认不出的文字 → False。
    """
    parsed = _relative_offset(text)
    window = relative_window(text, base_ts)
    if parsed is None or window is None:
        return False
    lo, hi = window
    if parsed[2] == "day":
        return lo <= float(ts) < hi
    return lo < float(ts) <= hi


def relative_date(text: Any, base_ts: float) -> str:
    """相对时间 → 北京时间日期 `YYYY-MM-DD`；不是相对时间 → ""。"""
    from . import clock

    ts = relative_ts(text, base_ts)
    return clock.bj(ts).strftime("%Y-%m-%d") if ts is not None else ""


# 只有代码 / 搜索结果给的精确时间才算「精确证据」；模型自己填的（model / 空）不算
_PRECISE_BASES = ("page", "search")


def _precise_ts_kept(item: dict, old_ts: Any, has_ts: bool, raw: Any, anchor: float) -> bool:
    """相对文字该不该让位给已有的精确时间（True = 保留精确值，不覆盖）。

    相对标签是**向下取整的粗粒度**估计（「1 小时前」= 1h00m–1h59m），所以已有精确时间
    （依据是 page 页面读到 / search 撒网搜索结果）**落在它自己的粒度区间里**时，两者说的是
    同一个时刻，保留更精确的那个——线上 gcores 220638 页面 title 写着 17:52:36，被
    「1 小时前」按打开时刻 19:19:41 覆盖成 18:19:41，晚了 27 分钟（同批 220636 留住了页面
    精确值，两条口径不一致）。

    区间由 `relative_window` / `relative_contains` 给（数字标签是滚动窗口、右闭；日历词按
    北京时间的「天」、左闭右开）：比标签**新**（页面说至少 1 小时前，候选却只有 9 分钟）
    或**旧出一个粒度**（那是「2 小时前」）都不保留。

    模型自己猜的时间（依据是 model / 空）不算证据：页面上活着的相对标签照旧覆盖它，
    免得旧错误日期赢过相对换算。
    """
    if not has_ts or str(item.get("date_basis") or "") not in _PRECISE_BASES:
        return False
    return relative_contains(raw, float(old_ts), anchor)


def _page_date_keeps_precise_ts(item: dict, old_ts: Any, has_ts: bool, page_date: str) -> bool:
    """页面读到的是「哪一天」；已有精确到秒的可信时间正好同一天时保留它（别降成当天 00:00）。

    依据同样是 page / search——模型自己猜的时间不算证据，照旧被页面日期覆盖。
    """
    if not has_ts or str(item.get("date_basis") or "") not in _PRECISE_BASES:
        return False
    from . import clock

    return clock.day_key(float(old_ts)) == str(page_date or "")


def resolve_dates(
    store: Any,
    task_id: str,
    items: list[dict],
    parse_published: Callable[[Any], Any],
    *,
    now_ts: float | None = None,
) -> int:
    """按依据定每条的发布日期（原地改 items），返回被代码改过 / 补上的条数。

    优先级：
    1. page —— 这轮打开该链接时代码从网页 HTML 读到的日期（tool_calls 摘要里的标记，
       含 meta / JSON-LD / `<time>` / data-time / title 那几种读法），覆盖模型给的（不同就记日志）；
    2. relative —— 模型把页面上的相对时间原样填进 published（「3 小时前」），按这轮打开该链接的
       fetch_page 时间（tool_calls.ts）换算；没有打开记录就按 now_ts（缺省 clock.now()）；
       但已有**精确**时间（依据是 page / search，不是模型自己猜的）且落在相对标签一个粒度内时，
       保留精确值、不覆盖（`_precise_ts_kept`）；
    3. 已有 published_ts 的保留原依据（核验搜索兜底标的 search），没标就是 model；
    4. 都没有 → ""，不硬造日期（「必须有日期」规则照旧）。
    published_raw 保留原文（只有它本来空着、用了页面日期时才填页面日期）。
    """
    from . import clock
    from .coordinator import normalize_link_for_check

    try:
        rows = _rows(store, task_id)
    except Exception:
        logger.debug("读打开记录失败（%s）", task_id, exc_info=True)
        rows = []
    dates = dates_from_rows(rows)
    times = fetch_times_from_rows(rows)
    changed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        key = normalize_link_for_check(str(item.get("url") or ""))
        raw = item.get("published_raw")
        old_ts = item.get("published_ts")
        has_ts = isinstance(old_ts, (int, float)) and not isinstance(old_ts, bool)
        date = dates.get(key, "") if key else ""
        ts = parse_published(date) if date else None
        if ts:
            if _page_date_keeps_precise_ts(item, old_ts, has_ts, date):
                logger.info(
                    "页面读到 %s 与已有精确时间 %s 同一天，保留秒级值（%s）",
                    date, clock.bj(float(old_ts)).strftime("%Y-%m-%d %H:%M:%S"), key,
                )
            else:
                model_date = normalize_date(raw) if isinstance(raw, str) else ""
                if model_date and model_date != date:
                    logger.info("发布日期以页面为准：%s 模型填 %r，网页读到 %s", key, raw, date)
                elif raw not in (None, "") and not model_date:
                    logger.debug("发布日期以页面为准：%s 模型填 %r，网页读到 %s", key, raw, date)
                item["published_ts"] = ts
            item["date_basis"] = "page"
            if raw in (None, ""):
                item["published_raw"] = date
            changed += 1
            continue
        base = times.get(key) if key else None
        anchor = base if base else (now_ts if now_ts is not None else clock.now())
        rel = relative_ts(raw, anchor)
        if rel is not None:
            if _precise_ts_kept(item, old_ts, has_ts, raw, anchor):
                logger.info(
                    "相对发布时间 %r 是粗粒度标签（单位内向下取整），已有精确时间 %s（依据 %s）"
                    "落在同一粒度里，保留精确值（%s）",
                    raw, clock.bj(float(old_ts)).strftime("%Y-%m-%d %H:%M:%S"),
                    item.get("date_basis"), key,
                )
                continue
            item["published_ts"] = rel
            item["date_basis"] = "relative"
            logger.info("相对发布时间 %r 按%s换算成 %s（%s）", raw, "打开时间" if base else "当前时间",
                        clock.bj(rel).strftime("%Y-%m-%d %H:%M"), key)
            changed += 1
            continue
        if has_ts:
            item["date_basis"] = item.get("date_basis") or "model"
        else:
            item["date_basis"] = ""
    return changed
