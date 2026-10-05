"""按成绩软倾斜搜索（2026-10-05 用户拍板，docs/10 §「第二步剩下三项」第 1 项）。

不硬改名额，只按各搜索服务 / 各类问法最近的成绩做软倾斜：

- 搜索服务：近 30 天本群群向候选里，保底撒网用的「其他家」候选 ≥ PROVIDER_MIN_CANDS 条
  且进网页率不到主家 WEAK_RATIO 倍 → 这轮不让它补搜；每周仍放它一次（试试有没有变好，
  记在 kv["search.weak_retry.<群号>"]）。主家永远照搜、永不算弱；线上只接一家搜索服务时
  这条不起作用。
- 问法：近 14 天「中文 / 英文 × 资讯 / 文章」四类问法的进网页率写进定关注点的提示词，
  让模型多用成绩好的那类问法；样本 < STYLE_MIN 条的不列。只是参考，方向和口味优先。

只读统计；读坏了 / 写不了一律不抛（返回空 / 原样放行），不拖累找资讯那轮。
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any

logger = logging.getLogger("maiwork.search_stats")

PROVIDER_WINDOW_DAYS = 30
PROVIDER_MIN_CANDS = 20
WEAK_RATIO = 0.5
RETRY_DAYS = 7
STYLE_WINDOW_DAYS = 14
STYLE_MIN = 10

_DAY = 86400.0
_RSS_PREFIX = "rss:"

# 问法语种粗判用的两条规则：有拉丁字母、无汉字 → 英文（跟 feeds._query_is_english 同一套口径；
# 这里本地实现，避免 search_stats ↔ feeds 循环导入）。
_LATIN_RE = _re.compile(r"[A-Za-z]")
_HAN_RE = _re.compile(r"[\u4e00-\u9fff]")

_STYLE_HEAD = "近 14 天各类搜索问法的成绩（进网页 / 搜到的候选，只是参考，方向和口味优先）："
_STYLE_TAIL = "成绩明显好的那类问法可以多用一点；样本少的没列。"
_LANG_LABEL = {"zh": "中文", "en": "英文"}
_KIND_LABEL = {"news": "找资讯", "guide": "找文章"}


def _retry_key(gid: Any) -> str:
    return f"search.weak_retry.{gid}"


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rate(kept: Any, cand: Any) -> float:
    """进网页率（kept / cand）；候选 0 条 → 0。"""
    n = int(cand or 0)
    return (float(kept or 0) / n) if n > 0 else 0.0


def _lang_of_query(q: Any) -> str:
    """问法语种粗判：带拉丁字母且不含汉字 → 英文，其余（纯中文、中日混排、纯数字符号）→ 中文。"""
    text = str(q or "")
    if _LATIN_RE.search(text) and not _HAN_RE.search(text):
        return "en"
    return "zh"


def _kind_of_row(kind: Any) -> str:
    """只分两类：kind=guide 是好文（文章），其余都算资讯。"""
    return "guide" if str(kind or "") == "guide" else "news"


def _is_rss(name: Any) -> bool:
    return str(name or "").startswith(_RSS_PREFIX)


def provider_rates(store: Any, gid: str, now: float) -> dict[str, dict[str, int]]:
    """近 PROVIDER_WINDOW_DAYS 天各搜索服务的群向候选 / 进网页数：{provider: {"cand", "kept"}}。

    只算群向（COALESCE(target_user_id,'')=''）、src_provider 非空且不是 RSS（"rss:<源>"）的行
    ——RSS 是订阅源，不是搜索服务。读坏了 → {}。
    """
    since = _as_float(now) - PROVIDER_WINDOW_DAYS * _DAY
    try:
        rows = store.read().execute(
            "SELECT src_provider, rejected FROM news_items"
            " WHERE group_id=? AND COALESCE(target_user_id,'')='' AND created>=?",
            (str(gid), since),
        ).fetchall()
    except Exception:
        logger.debug("搜索服务成绩统计失败（群 %s）", gid, exc_info=True)
        return {}
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        name = str(r["src_provider"] or "")
        if not name or _is_rss(name):
            continue
        st = out.setdefault(name, {"cand": 0, "kept": 0})
        st["cand"] += 1
        if not int(r["rejected"] or 0):
            st["kept"] += 1
    return out


def weak_providers(store: Any, gid: str, now: float, main: str, extras: list[str]) -> set[str]:
    """这批「其他家」里哪些这轮算成绩差：候选 ≥ PROVIDER_MIN_CANDS 且进网页率 < 主家一半。

    主家自己永不弱；主家样本不够（候选 < PROVIDER_MIN_CANDS）或一条没进（率 0）时谁都不弱
    ——没有可比对象就不倾斜。没见过的家不算弱。
    """
    rates = provider_rates(store, gid, now)
    main_name = str(main or "")
    main_st = rates.get(main_name)
    if not main_st or int(main_st["cand"]) < PROVIDER_MIN_CANDS:
        return set()
    main_rate = _rate(main_st["kept"], main_st["cand"])
    if main_rate <= 0:
        return set()
    weak: set[str] = set()
    for raw in extras or []:
        name = str(raw or "")
        if not name or name == main_name or name in weak:
            continue
        st = rates.get(name)
        if not st or int(st["cand"]) < PROVIDER_MIN_CANDS:
            continue
        if _rate(st["kept"], st["cand"]) < WEAK_RATIO * main_rate:
            weak.add(name)
    return weak


def allow_weak_retry(store: Any, gid: str, provider: str, now: float) -> bool:
    """成绩差的服务每周放一次：距上次放行 ≥ RETRY_DAYS 天（或从没记过）→ 记下这次、放行。

    记录在 kv["search.weak_retry.<群号>"]：{服务名: 上次放行时间}。没到时间的返回 False，不记账。
    """
    key = _retry_key(gid)
    raw = store.kv_get(key, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    last = _as_float(raw.get(str(provider)))
    if _as_float(now) - last < RETRY_DAYS * _DAY:
        return False
    raw[str(provider)] = float(_as_float(now))
    with store.tx() as conn:
        store.kv_set(conn, key, raw)
    return True


def filter_extras(
    store: Any, gid: str, now: float, main: str, extras: list[str]
) -> tuple[list[str], list[str]]:
    """保底撒网这轮用哪些「其他家」：成绩差的先按每周一次的机会放行，其余这轮跳过。

    返回 (还能用的, 这轮跳过的)。任何一步出错都原样放行（返回全部 extras、没有跳过），
    只记 debug，不拖累找资讯那轮。
    """
    names = [str(x) for x in (extras or [])]
    try:
        weak = weak_providers(store, gid, now, main, names)
        kept: list[str] = []
        skipped: list[str] = []
        for name in names:
            if name in weak and not allow_weak_retry(store, gid, name, now):
                skipped.append(name)
            else:
                kept.append(name)
        return kept, skipped
    except Exception:
        logger.debug("搜索服务成绩倾斜失败（群 %s），这轮照常撒网", gid, exc_info=True)
        return list(names), []


def style_rates(store: Any, gid: str, now: float) -> list[dict[str, Any]]:
    """近 STYLE_WINDOW_DAYS 天各类问法的群向成绩，按进网页率从高到低。

    返回 [{"lang": "zh"|"en", "kind": "news"|"guide", "cand": n, "kept": k}]。
    只算记了问法（src_query 非空）的群向行；RSS 源不是搜索问法，不算。读坏了 → []。
    """
    since = _as_float(now) - STYLE_WINDOW_DAYS * _DAY
    try:
        rows = store.read().execute(
            "SELECT src_query, src_provider, kind, rejected FROM news_items"
            " WHERE group_id=? AND COALESCE(target_user_id,'')='' AND created>=?"
            " AND COALESCE(src_query,'')<>''",
            (str(gid), since),
        ).fetchall()
    except Exception:
        logger.debug("搜索问法成绩统计失败（群 %s）", gid, exc_info=True)
        return []
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        if _is_rss(r["src_provider"]):
            continue
        key = (_lang_of_query(r["src_query"]), _kind_of_row(r["kind"]))
        st = buckets.setdefault(key, {"cand": 0, "kept": 0})
        st["cand"] += 1
        if not int(r["rejected"] or 0):
            st["kept"] += 1
    out = [
        {"lang": lang, "kind": kind, "cand": st["cand"], "kept": st["kept"]}
        for (lang, kind), st in buckets.items()
    ]
    out.sort(key=lambda d: (-_rate(d["kept"], d["cand"]), d["lang"], d["kind"]))
    return out


def style_prompt_lines(store: Any, gid: str, now: float) -> list[str]:
    """定关注点提示词里的「各类问法成绩」几行；没有一类样本够（cand ≥ STYLE_MIN）→ []。

    格式：一行表头 + 每类一行「- 中文问法找资讯：61 / 111（55%）」+ 一行尾注；按进网页率从高到低。
    任何出错都 → []（提示词少几行不影响找资讯）。
    """
    try:
        listed = [r for r in style_rates(store, gid, now) if int(r["cand"]) >= STYLE_MIN]
        if not listed:
            return []
        lines = [_STYLE_HEAD]
        for r in listed:
            lang = _LANG_LABEL.get(str(r["lang"]), str(r["lang"]))
            kind = _KIND_LABEL.get(str(r["kind"]), str(r["kind"]))
            pct = int(round(100.0 * _rate(r["kept"], r["cand"])))
            lines.append(f"- {lang}问法{kind}：{int(r['kept'])} / {int(r['cand'])}（{pct}%）")
        lines.append(_STYLE_TAIL)
        return lines
    except Exception:
        logger.debug("拼搜索问法成绩提示失败（群 %s）", gid, exc_info=True)
        return []
