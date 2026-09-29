"""资讯候选「补打开」（2026-09-29 用户定）：找资讯的子 agent 没真打开过原文的候选，
主流程再派一个子 agent（子 agent 模型）一批打开、抓正文、对照原文核对，而不是直接扔掉。

为什么（线上实测 2026-09-28 23:39）：找资讯的子 agent 15 分钟时间盒到点，只看过搜索摘要的 3 条
交了回来（fetched=false），第一道「原文没打开过/打不开」全扔，那轮一条没发；其中一条打开原文会发现
是旧闻（正文「争取 9 月 20 日上线」，9 月 26 日转发）。

流程（feeds.prepare_news 第一道之前）：
1. 挑出要补的：fetched=false 的，以及说 fetched=true 但这轮 fetch_page 记录里没有它（请求地址 / 跳转后的
   最终地址都算）的。这轮一条工具记录都没有时（记录读不到）不凭空怀疑「说打开过」的。
2. 一批交给补打开子 agent（工具 fetch_page + web_search，时间盒 _RECHECK_MINUTES 分钟，最多 _RECHECK_CAP 条）：
   打开原文（打不开可以找同一件事的别的来源并打开，换了就改 url）→ 对照原文：摘要站不站得住（drop）、
   按原文重写摘要和原文依据、填发布时间、判旧闻（stale：正文说的事已经过去 / 发布超过 7 天）。
3. 代码只认它**这次真打开过**的链接（查补打开这轮的 fetch_page 记录）；说 keep 但没打开 → 不动，
   后面第一道照旧按「原文没打开过」淘汰。drop / stale → 直接打上第一道淘汰理由。
补打开出任何错都不拖累这轮：记日志，候选原样往下走。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import clock

logger = logging.getLogger("maiwork.news_recheck")

_RECHECK_MINUTES = 6
_RECHECK_CAP = 8
_QUOTE_MAX = 200

RECHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["keep", "drop"]},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                    "quote": {"type": "string"},
                    "published": {"type": "string"},
                    "stale": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "url"],
            },
        }
    },
    "required": ["items"],
}


def opened_links(store: Any, task_id: str) -> tuple[set[str], int]:
    """这个 task_id 下成功打开过的链接（规范化）和工具记录总数。

    fetch_page 和扩展的抓正文工具（mcp_ 开头、名字像抓正文、不像搜索）成功过的都算；
    和 coordinator 验收引用核对共用一份解析（tools_builtin.opened_urls_from_rows）。
    """
    from .tools_builtin import opened_urls_from_rows

    try:
        rows = store.read().execute(
            "SELECT tool, input, output, ok FROM tool_calls WHERE task_id=?", (str(task_id),)
        ).fetchall()
    except Exception:
        logger.exception("读打开记录失败（%s）", task_id)
        return set(), 0
    return opened_urls_from_rows(rows), len(rows)


def pick_unverified(candidates: list[dict], opened: set[str], records: int) -> list[int]:
    """要补打开的候选下标（按原序）：没打开过的；说打开过但记录里没有的（有记录时才查）。"""
    from .coordinator import normalize_link_for_check

    out: list[int] = []
    for i, item in enumerate(candidates):
        if item.get("reject") or item.get("from_rss"):
            continue
        if not item.get("fetched") or not str(item.get("quote") or "").strip():
            out.append(i)
        elif records and normalize_link_for_check(str(item.get("url") or "")) not in opened:
            out.append(i)
    return out


def _brief(candidates: list[dict], picked: list[int]) -> str:
    today = clock.bj(clock.now()).strftime("%Y-%m-%d")
    lines = []
    for n, i in enumerate(picked):
        it = candidates[i]
        lines.append(
            f"[{n}] 标题：{it.get('title', '')}\n    链接：{it.get('url', '')}\n"
            f"    同事写的摘要：{it.get('summary', '')}\n    同事填的发布时间：{it.get('published_raw') or '没填'}"
        )
    return (
        f"今天是 {today}（北京时间）。下面 {len(picked)} 条是同事找资讯时交回、但原文还没核对过的候选"
        "（多半只看过搜索摘要）。请逐条：\n"
        "1. 用 fetch_page 打开链接读原文；打不开的，可以用 web_search 找同一件事的别的可靠来源并用 fetch_page 打开，"
        "换了来源就把 url 改成新链接。只打开文章本身，别去开网站首页、新闻列表页；\n"
        "2. 对照原文核对：原文支持不支持同事的摘要——不支持、对不上、是营销软文就 verdict=drop，reason 写原因；\n"
        "3. 支持的 verdict=keep：按原文重写 summary（2–4 句中文纯文本）；quote 从原文里抄一小段能支撑摘要的原话"
        f"（≤{_QUOTE_MAX} 字）；published 填原文的发布时间（ISO 日期，拿不到就空字符串）；\n"
        "4. 判旧闻：原文说的事已经过去了（比如写「争取 9 月 20 日上线」而今天已经过了那天），或原文发布超过 7 天，"
        "stale=true，reason 写清楚；\n"
        "5. 每条都要给结论（index 用方括号里的编号）；打不开也找不到别的来源的，verdict=drop、reason 写「打不开」；\n"
        f"6. 最后用 submit_result 交回，data 按约定的 JSON Schema。你只有大约 {_RECHECK_MINUTES} 分钟，"
        "到点前把已经核对完的交回来。\n\n"
        + "\n".join(lines)
    )


async def recheck(
    store: Any,
    workers: Any,
    gid: str,
    candidates: list[dict],
    *,
    collect_mark: str,
    recheck_mark: str,
    parse_published: Callable[[Any], Any],
    normalize_url: Callable[[str], str],
    site_of: Callable[[str], str],
) -> int:
    """补打开 + 核对；原地改 candidates。返回核对通过（keep 且真打开过）的条数。出错不抛。"""
    from .coordinator import normalize_link_for_check

    opened, records = opened_links(store, collect_mark)
    picked = pick_unverified(candidates, opened, records)[:_RECHECK_CAP]
    if not picked:
        return 0
    try:
        report = await workers.run(
            _brief(candidates, picked),
            group_id=gid,
            tools=["fetch_page", "web_search"],
            output_schema=RECHECK_SCHEMA,
            task_id=recheck_mark,
            deadline_ts=clock.now() + _RECHECK_MINUTES * 60,
        )
    except Exception:
        logger.exception("资讯补打开子 agent 出错（群 %s），这些候选照旧按没打开处理", gid)
        return 0
    data = getattr(report, "data", None)
    if not getattr(report, "ok", False) or not isinstance(data, dict) or not isinstance(data.get("items"), list):
        logger.info("资讯补打开没交回结果（群 %s）：%s", gid, getattr(report, "error", "") or getattr(report, "summary", ""))
        return 0
    reopened, _n = opened_links(store, recheck_mark)
    kept = 0
    for res in data["items"]:
        if not isinstance(res, dict):
            continue
        try:
            n = int(res.get("index"))
        except (TypeError, ValueError):
            continue
        if n < 0 or n >= len(picked):
            continue
        item = candidates[picked[n]]
        reason = str(res.get("reason") or "").strip()[:120]
        verdict = str(res.get("verdict") or "").strip().lower()
        url = str(res.get("url") or "").strip() or str(item.get("url") or "")
        was_opened = normalize_link_for_check(url) in reopened
        if verdict == "drop":
            if was_opened or "打不开" in reason:
                item["reject"] = ("hard", f"补打开核对：{reason or '原文不支持这条'}")
            continue
        if verdict != "keep" or not was_opened:
            continue  # 说 keep 却没真打开：不认，第一道照旧按没打开处理
        if bool(res.get("stale")):
            item["reject"] = ("hard", f"旧闻（补打开核对）：{reason or '原文说的事已经过去了'}")
            continue
        quote = str(res.get("quote") or "").replace("\n", " ").strip()[:_QUOTE_MAX]
        if not quote:
            continue
        if url != item.get("url"):
            item["url"] = url
            item["url_key"] = normalize_url(url)
            item["site"] = site_of(url)
        summary = str(res.get("summary") or "").strip()
        if summary:
            item["summary"] = summary
        item["quote"] = quote
        published = str(res.get("published") or "").strip()
        if published:
            item["published_raw"] = published
            ts = parse_published(published)
            if ts:
                item["published_ts"] = ts
        item["fetched"] = True
        item["rechecked"] = True
        kept += 1
    logger.info("资讯补打开（群 %s）：送去 %d 条，核对通过 %d 条", gid, len(picked), kept)
    return kept


def merge_stats(a: dict, b: dict) -> dict:
    return {"searches": int(a.get("searches", 0)) + int(b.get("searches", 0)),
            "pages": int(a.get("pages", 0)) + int(b.get("pages", 0))}
