"""代码从网页读发布日期（docs/10-资讯流水线改进计划.md §九 第一步 2，2026-10-05）。

线上现象：好文（kind=guide）因为「文章没有发布时间，宁缺毋滥不收」被硬拒
（222 篇好文里 6 篇，含机核长文 2 篇）——那是子 agent 在页面上没找到可见日期。
规则不放宽，改成代码自己读：

- 打开网页那一步（tools_builtin.fetch_page 拿到原始 HTML）解析 meta（article:published_time、
  og:published_time、datePublished、pubdate、date、DC.date……）、JSON-LD（datePublished /
  dateCreated）、`<time datetime="…">`；
- 读到就把日期写进 fetch_page 的 tool_calls 摘要（`（页面发布日期：YYYY-MM-DD）`），
  子 agent 也直接在正文里看得到；
- 下游（feeds 核验 / news_recheck 补打开）在**模型没给日期时**用它补 `published_ts`，
  「必须有日期」照旧不放宽。

日期统一归一成 `YYYY-MM-DD`（够判几天的时效，又不用纠缠时区）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

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


def dates_from_rows(rows: Any) -> dict[str, str]:
    """tool_calls 记录（dict 行，键 tool/input/output/ok）→ {规范化链接: YYYY-MM-DD}。

    只认真打开过原文的那两类调用（和 opened_urls_from_rows 同一份判定）：fetch_page
    （请求地址 + 摘要里的「最终地址」）和扩展抓正文工具（input 的 url / urls / link）。
    日期取摘要里的「页面发布日期」标记，其次纯文本里的日期行。
    """
    from .coordinator import normalize_link_for_check
    from .tools_builtin import _is_extract_like_mcp, _urls_from_json_input, final_url_from_summary

    out: dict[str, str] = {}
    for row in rows or []:
        try:
            tool = str(row["tool"] or "")
            ok = int(row["ok"] or 0)
            raw_input = str(row["input"] or "")
            output = str(row["output"] or "")
        except (KeyError, TypeError, ValueError):
            continue
        if not ok:
            continue
        urls: list[str] = []
        if tool == "fetch_page":
            urls.extend(_urls_from_json_input(raw_input))
            final = final_url_from_summary(output)
            if final:
                urls.append(final)
        elif _is_extract_like_mcp(tool):
            urls.extend(_urls_from_json_input(raw_input))
        else:
            continue
        date = date_from_summary(output) or published_from_text(output)
        if not date:
            continue
        for one in urls:
            key = normalize_link_for_check(one)
            if key:
                out.setdefault(key, date)
    return out


def page_dates(store: Any, task_id: str) -> dict[str, str]:
    """这个 task_id 下代码从网页读到的发布日期；读不到 / 出错 → {}（不挡流程）。"""
    try:
        rows = store.read().execute(
            "SELECT tool, input, output, ok FROM tool_calls WHERE task_id=?", (str(task_id),)
        ).fetchall()
    except Exception:
        logger.debug("读打开记录失败（%s）", task_id, exc_info=True)
        return {}
    return dates_from_rows(rows)
