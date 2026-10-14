"""核验子 agent（feeds-verify:）的打开页数账本：代码硬上限，防「找日期打转」。

为什么要有它（2026-09-30 线上实测）：一个核验子 agent 为了给一条 YouTube 视频找
发布时间，8 分钟里调了 23 次 fetch_page（invidious / piped 镜像、web.archive、
oEmbed……），提示词劝不住。所以上限做进代码：

- feeds._verify_batch 按组 open_run（上限 = cap_for(组内条数)）；
- tools_builtin 的 fetch_page handler 看到 task_id 在账本就 consume 一次，
  用完直接回拒绝话（中文，告诉子 agent 别再开、用已经打开的内容交回）；
- 任务结束 close_run，不残留；
- 没开账本的 task_id（普通任务、老模式 feeds-collect: 找资讯）一律放行。

2026-10-03 起个人向找料（personal-collect:）也用这本账：打开页数 + 搜索次数各一条上限
（线上实测贵的一次搜了 44 次，每步重发整段对话，一次就 78 万 token）。
搜索上限只在 open_run 给了 cap_search_calls 时生效；核验组不设，照旧只管打开页数。

账本只活在本进程里（内存 + 锁）：核验组几分钟就结束，重启后旧标记本来也不会再用。
"""

from __future__ import annotations

import html
import re
import threading
import unicodedata
from dataclasses import dataclass

# task_id -> {"cap": int, "used": int, "search_cap": int(0=不限), "search_used": int}
_RUNS: dict[str, dict[str, int]] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class FetchedPage:
    """Code-owned evidence from the exact text shown by a successful fetch_page."""
    url: str
    final_url: str
    title: str
    body: str


_PAGES: dict[str, list[FetchedPage]] = {}
_EVIDENCE_MAX_CHARS = 20000  # extraction fallback's existing tool-visible limit
# A quote must carry at least this many letters/digits/CJK chars (one 4-char CJK
# phrase); 1–3 characters match almost any page and prove nothing.
_QUOTE_MIN_ALNUM = 4
_METADATA = re.compile(r"^(?:title|url source|published time|markdown content|发布时间|发布日期|作者|来源)\s*[:：]", re.I)
_NAV_WORDS = re.compile(r"^(?:首页|登录|注册|商店|社区|支持|关于|联系我们|隐私政策|版权|目录|分享|上一页|下一页|home|store|community|support|sign in|log in|login|register|menu|search|privacy|terms|share|next|previous|about)[\s|/·:：-]*$", re.I)
_INSUFFICIENT = re.compile(r"仅(?:根据|依据|凭|从).{0,8}标题|只(?:根据|依据|凭|看(?:到|了)?).{0,8}标题|未(?:读取|读到|获得|能读取).{0,4}正文|正文(?:无法|不可|未能)(?:读取|获得|访问)|(?:based|relying)\s+(?:only\s+)?on\s+(?:the\s+)?(?:title|headline)|title[- ]only|body\s+(?:was\s+)?(?:unavailable|not\s+(?:read|fetched))", re.I)
_ACCESS_WALL = re.compile(r"sign in to (?:read|continue)|subscribe to (?:read|continue)|log in to (?:read|continue)|登录后(?:阅读|查看)|订阅后(?:阅读|查看)|付费(?:后|才能)(?:阅读|查看)", re.I)


def _match_text(text: str) -> str:
    # Keep punctuation and digits: a quote with different limits/prices must not match.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", html.unescape(str(text or "")))).casefold()


def _quote_text(text: str) -> str:
    """引文比对口径（2026-10-14 用户同意放宽）：在 `_match_text` 之上再去掉标点。

    文字、数字、货币和百分号这些符号一字不差；只忽略标点、空格、全半角差别。
    数字中间的 . , : 保留（「1.5 万」不能被「15 万」冒充）。
    """
    base = _match_text(text)
    out = []
    for i, ch in enumerate(base):
        if unicodedata.category(ch).startswith("P"):
            if (ch in "%‰‱" or
                (ch in "-‐‑‒–—" and i + 1 < len(base) and base[i + 1].isdigit()) or
                (ch in ".,:/" and 0 < i < len(base) - 1 and base[i - 1].isdigit() and base[i + 1].isdigit())):
                out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


def article_body(text: str, title: str = "") -> str:
    """Remove standalone headings, metadata and navigation; retain concise prose.

    This is a structural screen, not a claim that every summary sentence is supported.
    A short sentence can qualify; hundreds of menu labels cannot.
    """
    headline = _match_text(title).strip("《》#")
    paragraphs = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "![", "（代码从页面", "（发布时间", "（这个链接", "（这页有封面", "（这是经")):
            continue
        if _METADATA.match(line) or _NAV_WORDS.fullmatch(line):
            continue
        if _match_text(line).strip("《》#") == headline and headline:
            continue
        # Menu entries and footer link lists are not article paragraphs.
        if re.fullmatch(r"(?:[-*+]\s*)?\[[^\]]+\]\([^)]*\)(?:\s*\|?\s*\[[^\]]+\]\([^)]*\))*", line):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        letters = sum(ch.isalnum() for ch in line)
        if letters < 8 or (letters < 40 and not re.search(r"[。！？.!?；;]", line)):
            continue
        paragraphs.append(line)
    body = "\n".join(paragraphs)
    if len(body) < 1500 and _ACCESS_WALL.search(body):
        return ""
    return body


def record_page(task_id: str, url: str, final_url: str, body: str, *, title: str = "") -> None:
    """Capture successful fetches only inside an active, bounded run."""
    tid = str(task_id or "")
    page = FetchedPage(str(url), str(final_url or url), str(title)[:300], str(body)[:_EVIDENCE_MAX_CHARS])
    with _LOCK:
        run = _RUNS.get(tid)
        if run is not None and len(_PAGES.get(tid, [])) < min(run["cap"], 32):
            _PAGES.setdefault(tid, []).append(page)


def bind_pages(task_id: str, items: list[dict]) -> None:
    """Bind this run's actual source to parsed candidates before closing the run.

    Model-supplied original_url receives no evidence from the old source unless the
    new URL was itself fetched (or was the final URL of a real redirect).
    """
    from .feeds import _normalize_url

    with _LOCK:
        pages = tuple(_PAGES.get(str(task_id or ""), ()))
    for item in items:
        item.pop("_fetched_evidence", None)
        item.pop("_source_body", None)
        key = _normalize_url(str(item.get("url") or ""))
        matching = [p for p in pages if key and key in {_normalize_url(p.url), _normalize_url(p.final_url)}]
        if not matching:
            continue
        # Multiple fetches of one URL: use a body actually containing this quote.
        quote = _quote_text(str(item.get("quote") or ""))
        page = next((p for p in matching if quote and quote in _quote_text(article_body(p.body, p.title))), matching[-1])
        item["_fetched_evidence"] = page
        item["_source_body"] = article_body(page.body, page.title)


def evidence_problem(item: dict) -> str:
    """Shared fail-closed gate; strings/numbers cannot assert fetched/grounded."""
    from .feeds import _normalize_url

    quote = str(item.get("quote") or "").strip()
    if item.get("fetched") is not True or not quote:
        return "原文没打开过/打不开"
    if item.get("evidence_sufficient") is False or _INSUFFICIENT.search(str(item.get("summary") or "")):
        return "摘要明确缺少正文依据"
    page = item.get("_fetched_evidence")
    if not isinstance(page, FetchedPage):
        return "本轮没有这条原文的正文记录"
    key = _normalize_url(str(item.get("url") or ""))
    if not key or key not in {_normalize_url(page.url), _normalize_url(page.final_url)}:
        return "原文依据与本轮打开的链接不一致"
    body = article_body(page.body, page.title)
    if not body:
        return "原文只有标题/导航，正文依据不足"
    evidence = _quote_text(quote)
    headlines = [_quote_text(page.title), _quote_text(str(item.get("title") or ""))]
    if any(headline and evidence in headline for headline in headlines):
        return "原文依据只有标题，缺少正文依据"
    if sum(ch.isalnum() for ch in evidence) < _QUOTE_MIN_ALNUM or evidence not in _quote_text(body):
        return "原文依据不在本轮读到的正文中"
    return ""

# 绝对上限（防「组内条数多就无限开」；一组最多先开这么多页）
_CAP_MAX = 6

# 拒绝时回给子 agent 的话（中文、一句话；让它直接收尾交回，别再挣扎）
_REFUSE_NOTE = (
    "这组核验的打开次数已经用完，别再打开新页面了——"
    "用已经打开到的内容按格式交回；没打开的条目跳过，"
    "页面上找不到发布日期的用搜索结果自带的日期（没有就留空）。"
)


def cap_for(items_in_group: int) -> int:
    """一组任务的打开页数上限：约（组内条数 × 2）+ 1，最多 _CAP_MAX 次，最少 1 次。

    一倍的关系：每条候选开一次候选链接本身，打不开最多再换一个备用地址，
    再多一次容错 —— 超出基本就是在打转了（找镜像 / 翻存档站）。
    """
    try:
        n = int(items_in_group)
    except (TypeError, ValueError):
        n = 0
    return max(1, min(n * 2 + 1, _CAP_MAX))


_SEARCH_REFUSE_NOTE = (
    "这次的搜索次数已经用完，别再搜了——"
    "用已经搜到、打开过的内容按格式交回（部分结果也算）。"
)

_PERSONAL_PAGE_REFUSE_NOTE = (
    "这次的打开页面次数已经用完，别再打开新页面了——"
    "用已经打开到的内容按格式交回（部分结果也算）。"
)


def open_run(task_id: str, *, cap_page_calls: int, cap_search_calls: int = 0) -> None:
    """给一个任务开账本（同名再开 = 清掉重来）。打开页数 cap≤0 当 1；搜索上限 0 = 不限。"""
    tid = str(task_id or "")
    if not tid:
        return
    try:
        cap = max(1, int(cap_page_calls))
    except (TypeError, ValueError):
        cap = 1
    try:
        search_cap = max(0, int(cap_search_calls or 0))
    except (TypeError, ValueError):
        search_cap = 0
    with _LOCK:
        _RUNS[tid] = {"cap": cap, "used": 0, "search_cap": search_cap, "search_used": 0}
        _PAGES[tid] = []


def close_run(task_id: str) -> None:
    """任务收尾，关掉账本（不关的话旧标记残留，理论上同名复用会受影响）。"""
    tid = str(task_id or "")
    with _LOCK:
        _RUNS.pop(tid, None)
        _PAGES.pop(tid, None)


def consume(task_id: str) -> tuple[bool, str]:
    """记一次打开页数。允许 → (True, "")；超上限 → (False, 拒绝话)。

    没开账本的 task_id 一律放行（别的任务不受影响）。
    """
    tid = str(task_id or "")
    if not tid:
        return True, ""
    with _LOCK:
        run = _RUNS.get(tid)
        if run is None:
            return True, ""
        if run["used"] >= run["cap"]:
            return False, (_PERSONAL_PAGE_REFUSE_NOTE if tid.startswith("personal-collect:") else _REFUSE_NOTE)
        run["used"] += 1
        return True, ""


def consume_search(task_id: str) -> tuple[bool, str]:
    """记一次搜索。没开账本 / 没设搜索上限 → 放行；超上限 → (False, 拒绝话)。"""
    tid = str(task_id or "")
    if not tid:
        return True, ""
    with _LOCK:
        run = _RUNS.get(tid)
        if run is None or not run.get("search_cap"):
            return True, ""
        if run["search_used"] >= run["search_cap"]:
            return False, _SEARCH_REFUSE_NOTE
        run["search_used"] += 1
        return True, ""


def _status(task_id: str) -> dict[str, int] | None:
    """（调试 / 测试用）这组账本现况；没开过 → None。"""
    tid = str(task_id or "")
    with _LOCK:
        run = _RUNS.get(tid)
        return dict(run) if run is not None else None
