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

日期统一归一成 `YYYY-MM-DD`（够判几天的时效，又不用纠缠时区）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
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


def _time_date(html: str) -> str:
    article = _ARTICLE_RE.search(html)
    span = (article.start(), article.end()) if article else None
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


def published_from_html(html: str) -> str:
    """从 HTML 里读发布日期（meta → JSON-LD → <time>）；没有 → ""。"""
    text = str(html or "")
    if not text:
        return ""
    return _meta_date(text) or _json_ld_date(text) or _time_date(text)


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
_WORDS: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"大前天"), 3 * 86400),
    (re.compile(r"前天|day before yesterday", re.IGNORECASE), 2 * 86400),
    (re.compile(r"昨天|昨日|\byesterday\b", re.IGNORECASE), 86400),
    (re.compile(r"刚刚|刚才|\bjust now\b|\bmoments? ago\b", re.IGNORECASE), 0),
    (re.compile(r"今天|今日|\btoday\b", re.IGNORECASE), 0),
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


def relative_ts(text: Any, base_ts: float) -> float | None:
    """页面上的相对时间文字 → 时间戳（以 base_ts 为「那一刻」）；不是相对时间 → None。

    认：「3 小时前」「三小时前」「半小时前」「2 天前」「1 周前」「昨天」「前天」「刚刚」「今天」、
    「3 hours ago」「an hour ago」「2d ago」「yesterday」「just now」「today」。
    带绝对日期的、太长的（不像一个日期字段）、「…后」都不认。
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
            return float(base_ts) - n * unit
    m = _EN_REL_RE.search(raw)
    if m:
        word = m.group(1).lower()
        n = 1.0 if word in ("a", "an", "one") else float(word)
        unit = _UNIT_SECONDS.get(m.group(2).lower())
        if unit:
            return float(base_ts) - n * unit
    for pat, seconds in _WORDS:
        if pat.search(raw):
            return float(base_ts) - seconds
    return None


def relative_date(text: Any, base_ts: float) -> str:
    """相对时间 → 北京时间日期 `YYYY-MM-DD`；不是相对时间 → ""。"""
    from . import clock

    ts = relative_ts(text, base_ts)
    return clock.bj(ts).strftime("%Y-%m-%d") if ts is not None else ""


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
    1. page —— 这轮打开该链接时代码从网页 HTML 读到的日期（tool_calls 摘要里的标记），
       覆盖模型给的（不同就记日志）；
    2. relative —— 模型把页面上的相对时间原样填进 published（「3 小时前」），按这轮打开该链接的
       fetch_page 时间（tool_calls.ts）换算；没有打开记录就按 now_ts（缺省 clock.now()）；
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
        rel = relative_ts(raw, base if base else (now_ts if now_ts is not None else clock.now()))
        if rel is not None:
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
