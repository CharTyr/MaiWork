"""本群的「优质来源」名单（2026-09-30，docs/10-资讯流水线改进计划.md 第七节第 6 步）。

手册「来源图谱与正负名单」：正名单要按群、会衰减、给新来源留位置，只影响「搜哪里 / 先打开谁」，
不能绕过核对。这里只算名单和一个 0–1 的先验分，不碰任何门槛。

- 统计口径：本群近 WINDOW_DAYS 天上了网页（rejected=0）、五项平均分 ≥ HIGH_AVG 的条目，
  按域名（去 www.）计数；每条按距今天数衰减（半衰期 HALF_LIFE_DAYS）；群友点「有用」净值每 1 票
  再加 UP_BONUS 条（同样衰减）。
- 上名单：至少 MIN_HIGH 条高分（不衰减的原始条数，样本少不算数）；管理员移出的（kv
  「feeds.trusted_removed.<群号>」）和屏蔽名单里的不上；按衰减后的分数从高到低，最多 limit 个。
- source_prior：在名单里 → 按名次给 1.0 往下递减（最低 0.3）；不在名单 → 0。
- view：给网页管理员看的名单明细 + 被移出的。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable
from urllib.parse import urlsplit

logger = logging.getLogger("maiwork.source_stats")

WINDOW_DAYS = 30
HALF_LIFE_DAYS = 15.0
HIGH_AVG = 4.0
MIN_HIGH = 2
UP_BONUS = 0.5
DEFAULT_LIMIT = 8


def _removed_key(gid: str) -> str:
    return f"feeds.trusted_removed.{gid}"


def _norm(domain: Any) -> str:
    d = str(domain or "").strip().lower().rstrip(".")
    if "://" in d:
        try:
            d = (urlsplit(d).hostname or "").lower()
        except ValueError:
            return ""
    d = d.split("/", 1)[0]
    return d[4:] if d.startswith("www.") else d


def _site_of_row(row: Any) -> str:
    try:
        src = json.loads(row["sources"] or "[]")
        if isinstance(src, list) and src and isinstance(src[0], dict):
            site = _norm(src[0].get("site") or "") or _norm(src[0].get("url") or "")
            if site:
                return site
    except (ValueError, TypeError):
        pass
    return _norm(str(row["url_key"] or ""))


def removed(store: Any, gid: str) -> list[str]:
    try:
        raw = store.kv_get(_removed_key(str(gid)), []) or []
    except Exception:
        return []
    return sorted({_norm(d) for d in raw if _norm(d)})


def set_removed(store: Any, gid: str, domain: str, is_removed: bool) -> list[str]:
    """管理员把某个域名移出 / 放回优质来源名单；返回移出清单。"""
    d = _norm(domain)
    if not d:
        raise ValueError("域名不合法")
    cur = set(removed(store, gid))
    if is_removed:
        cur.add(d)
    else:
        cur.discard(d)
    out = sorted(cur)
    with store.tx() as conn:
        store.kv_set(conn, _removed_key(str(gid)), out)
    return out


def _stats(store: Any, gid: str, now: float) -> dict[str, dict[str, float]]:
    since = now - WINDOW_DAYS * 86400.0
    try:
        rows = store.read().execute(
            "SELECT sources, url_key, scores, up, down, created FROM news_items"
            " WHERE group_id=? AND rejected=0 AND created>=?",
            (str(gid), since),
        ).fetchall()
    except Exception:
        logger.debug("优质来源统计失败（群 %s）", gid, exc_info=True)
        return {}
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        try:
            avg = float((json.loads(r["scores"] or "{}") or {}).get("avg") or 0.0)
        except (ValueError, TypeError, AttributeError):
            avg = 0.0
        if avg < HIGH_AVG:
            continue
        site = _site_of_row(r)
        if not site:
            continue
        age_days = max(0.0, (now - float(r["created"] or 0.0)) / 86400.0)
        decay = 0.5 ** (age_days / HALF_LIFE_DAYS)
        net_up = max(0, int(r["up"] or 0) - int(r["down"] or 0))
        s = out.setdefault(site, {"high": 0.0, "score": 0.0, "up": 0.0})
        s["high"] += 1
        s["up"] += net_up
        s["score"] += decay * (1.0 + UP_BONUS * net_up)
    return out


def trusted_domains(
    store: Any, gid: str, now: float, *, limit: int = DEFAULT_LIMIT, blocked: Iterable[str] = ()
) -> list[str]:
    """本群的优质来源（高到低）。"""
    return [d["domain"] for d in _ranked(store, gid, now, blocked=blocked)[: max(0, int(limit))]]


def _ranked(store: Any, gid: str, now: float, *, blocked: Iterable[str] = ()) -> list[dict[str, Any]]:
    skip = set(removed(store, gid)) | {_norm(b) for b in blocked if _norm(b)}
    items = [
        {"domain": d, "high": int(s["high"]), "up": int(s["up"]), "score": round(s["score"], 3)}
        for d, s in _stats(store, gid, now).items()
        if s["high"] >= MIN_HIGH and d not in skip
    ]
    items.sort(key=lambda x: (-x["score"], x["domain"]))
    return items


def source_prior(store: Any, gid: str, site: str, now: float, *, blocked: Iterable[str] = ()) -> float:
    """两段式预筛排序用：名单第 1 名 1.0，往下每名少 0.1，最低 0.3；不在名单 0。"""
    d = _norm(site)
    if not d:
        return 0.0
    ranked = trusted_domains(store, gid, now, blocked=blocked)
    if d not in ranked:
        return 0.0
    return max(0.3, 1.0 - 0.1 * ranked.index(d))


def view(store: Any, gid: str, now: float, *, blocked: Iterable[str] = ()) -> dict[str, Any]:
    """网页管理员看的：{trusted: [{domain, high, up, score}], removed: [...], rule: 一句话口径}。"""
    return {
        "trusted": _ranked(store, gid, now, blocked=blocked)[:DEFAULT_LIMIT],
        "removed": removed(store, gid),
        "rule": f"近 {WINDOW_DAYS} 天至少 {MIN_HIGH} 条 {HIGH_AVG:g} 分以上的来源；越近越算数，群友点「有用」加分",
    }
