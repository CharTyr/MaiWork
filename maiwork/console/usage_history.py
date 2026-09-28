"""用量历史（GET /api/usage/history）：按天 / 按模型 / 按用途 / 按群 / 按小时。

只读 MaiWork 自己的库，两张表：
- `usage`：models.py 每次模型尝试一行（role main/worker、model、purpose、group_id、
  prompt_tokens、completion_tokens、ok、ms）；
- `judgments`：jev.py 每次 Jev 调用一行（purpose、group_id、ms、ok）。
不读 MaiBot 的消息库；返回里只有群号、模型名、purpose、次数和 token 数——
不带密钥，也不带 QQ 号以外的个人信息。

「天」一律是北京时间的 day_key（clock.day_key）。两种模式：
- `days=N`（默认 7，夹在 1..30）：最近 N 天含今天，范围内每天一条，没数据的天补 0；
- `date=YYYY-MM-DD`：只看那一天（格式错抛 ValueError，server 转 400 中文错误）。
date 给了就优先 date。落在范围内的天按日期升序。

这个模块是纯函数（输入 svc，输出按 docs/07 §9.2 的结构），server.py 只接线。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from .. import clock
from ..names import clean_group_name
from . import views

logger = logging.getLogger("maiwork.console.usage_history")

MAX_DAYS = 30
DEFAULT_DAYS = 7
HOURS = 24
# 北京时间固定 UTC+8（clock.BJ）；SQLite 的 unixepoch 是 UTC，加这个偏移再取小时
_BJ_OFFSET_SECONDS = 8 * 3600
_OTHER_GROUP_NAME = "其他"
_DAY_TEXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY_FMT = "%Y-%m-%d"


# ----------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------


def normalize_days(raw: Any) -> int:
    """days 参数：认不出（空 / 不是整数）用默认 7；大了夹到 30，小了夹到 1。"""
    text = str(raw).strip() if raw is not None else ""
    try:
        n = int(text)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, n))


def parse_date(raw: Any) -> date | None:
    """date 参数：空 = 没给（None）；不是严格的 YYYY-MM-DD 或不是真日期 → ValueError（中文）。"""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    if not _DAY_TEXT_RE.match(text):
        raise ValueError("日期要写成 2026-09-28 这样（YYYY-MM-DD）")
    try:
        return datetime.strptime(text, _DAY_FMT).date()
    except ValueError:
        raise ValueError(f"没有 {text} 这一天，日期写错了") from None


def resolve_range(
    days: Any = None, date_raw: Any = None, *, today: date
) -> tuple[date, date, date | None]:
    """算出范围（含两头）和单日模式的那一天；date 优先于 days。

    格式错由 parse_date 抛 ValueError（server 转 400）。
    """
    one = parse_date(date_raw)
    if one is not None:
        return one, one, one
    to_day = today
    from_day = today - timedelta(days=normalize_days(days) - 1)
    return from_day, to_day, None


def _day_list(from_day: date, to_day: date) -> list[str]:
    """范围内每天一个 day_key，按日期升序（补 0 天靠它）。"""
    return [(from_day + timedelta(days=i)).isoformat() for i in range((to_day - from_day).days + 1)]


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------


def history_view(svc: Any, *, days: Any = None, date: Any = None) -> dict[str, Any]:
    """拼 /api/usage/history 的返回。date 格式不对抛 ValueError（server 转 400）。"""
    today = clock.bj(clock.now()).date()
    from_day, to_day, one = resolve_range(days, date, today=today)
    day_list = _day_list(from_day, to_day)
    from_key, to_key = from_day.isoformat(), to_day.isoformat()
    conn = _conn(svc)

    per_day: dict[str, dict[str, int]] = {
        d: {"main": 0, "worker": 0, "calls": 0, "errors": 0, "jev": 0} for d in day_list
    }
    prompt_total = completion_total = 0
    for r in _fetch(
        conn,
        "SELECT day, role, COUNT(*) AS calls,"
        " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) AS errors,"
        " COALESCE(SUM(prompt_tokens), 0) AS pt,"
        " COALESCE(SUM(completion_tokens), 0) AS ct"
        " FROM usage WHERE day >= ? AND day <= ? GROUP BY day, role",
        (from_key, to_key),
    ):
        bucket = per_day.get(str(r["day"]))
        if bucket is None:  # 库里存了范围外的天（不该有，认不出就跳过）
            continue
        pt, ct = int(r["pt"] or 0), int(r["ct"] or 0)
        bucket["calls"] += int(r["calls"] or 0)
        bucket["errors"] += int(r["errors"] or 0)
        role = str(r["role"] or "")
        if role in ("main", "worker"):
            bucket[role] += pt + ct
        prompt_total += pt
        completion_total += ct
    for r in _fetch(
        conn,
        "SELECT day, COUNT(*) AS c FROM judgments WHERE day >= ? AND day <= ? GROUP BY day",
        (from_key, to_key),
    ):
        bucket = per_day.get(str(r["day"]))
        if bucket is not None:
            bucket["jev"] += int(r["c"] or 0)

    day_items = [{"day": d, **per_day[d]} for d in day_list]
    totals = {
        "main": sum(b["main"] for b in per_day.values()),
        "worker": sum(b["worker"] for b in per_day.values()),
        "calls": sum(b["calls"] for b in per_day.values()),
        "errors": sum(b["errors"] for b in per_day.values()),
        "jev": sum(b["jev"] for b in per_day.values()),
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
    }

    out: dict[str, Any] = {}
    if one is not None:
        out["day"] = from_key  # 单日模式给顶层 day，前端「某天的明细」直接用
    out["range"] = {"from": from_key, "to": to_key}
    out["days"] = day_items
    out["totals"] = totals
    out["by_model"] = _by_model(conn, from_key, to_key)
    out["by_purpose"] = _by_purpose(conn, from_key, to_key)
    out["by_group"] = _by_group(svc, conn, from_key, to_key)
    out["by_hour"] = _by_hour(conn, from_key, to_key)
    jev_stats = _jev_stats(conn, from_key, to_key)
    # 顶层 jev 统一是整数（范围内的 Jev 次数），明细汇总放 jev_stats
    out["jev"] = jev_stats["calls"]
    out["jev_stats"] = jev_stats
    return out


def _by_model(conn: Any, from_key: str, to_key: str) -> list[dict[str, Any]]:
    """按 (model, role) 分组；tokens（输入+输出）降序。"""
    items: list[dict[str, Any]] = []
    for r in _fetch(
        conn,
        "SELECT model, role, COUNT(*) AS calls,"
        " COALESCE(SUM(prompt_tokens), 0) AS pt,"
        " COALESCE(SUM(completion_tokens), 0) AS ct,"
        " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) AS errors,"
        " COALESCE(AVG(ms), 0) AS avg_ms"
        " FROM usage WHERE day >= ? AND day <= ? GROUP BY model, role",
        (from_key, to_key),
    ):
        pt, ct = int(r["pt"] or 0), int(r["ct"] or 0)
        items.append(
            {
                "model": str(r["model"] or ""),
                "role": str(r["role"] or ""),
                "calls": int(r["calls"] or 0),
                "prompt": pt,
                "completion": ct,
                "tokens": pt + ct,
                "errors": int(r["errors"] or 0),
                "avg_ms": _avg_ms(r["avg_ms"]),
            }
        )
    items.sort(key=lambda m: (-m["tokens"], m["model"], m["role"]))
    return items


def _by_purpose(conn: Any, from_key: str, to_key: str) -> list[dict[str, Any]]:
    """按 purpose 分组；tokens 降序。purpose 给网页显示的中文名（views.purpose_name）。"""
    items: list[dict[str, Any]] = []
    for r in _fetch(
        conn,
        "SELECT purpose, COUNT(*) AS calls,"
        " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens"
        " FROM usage WHERE day >= ? AND day <= ? GROUP BY purpose",
        (from_key, to_key),
    ):
        items.append(
            {
                "purpose": views.purpose_name(r["purpose"]),
                "calls": int(r["calls"] or 0),
                "tokens": int(r["tokens"] or 0),
            }
        )
    items.sort(key=lambda p: (-p["tokens"], p["purpose"]))
    return items


def _by_group(svc: Any, conn: Any, from_key: str, to_key: str) -> list[dict[str, Any]]:
    """按群分组：只列配置里的服务群；非服务群和空 group_id 合并成一条「其他」。

    范围内没有用量的服务群不列。tokens 降序。
    """
    served = set(views._served_group_ids(svc))
    groups: dict[str, dict[str, Any]] = {}
    for r in _fetch(
        conn,
        "SELECT group_id, COUNT(*) AS calls,"
        " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens"
        " FROM usage WHERE day >= ? AND day <= ? GROUP BY group_id",
        (from_key, to_key),
    ):
        gid = str(r["group_id"] or "")
        calls, tokens = int(r["calls"] or 0), int(r["tokens"] or 0)
        if gid and gid in served:
            groups[gid] = {
                "group_id": gid,
                "name": _group_name(svc, gid),
                "calls": calls,
                "tokens": tokens,
            }
            continue
        other = groups.setdefault(
            "", {"group_id": "", "name": _OTHER_GROUP_NAME, "calls": 0, "tokens": 0}
        )
        other["calls"] += calls
        other["tokens"] += tokens
    return sorted(groups.values(), key=lambda g: (-g["tokens"], g["group_id"]))


def _by_hour(conn: Any, from_key: str, to_key: str) -> list[int]:
    """24 个整数：按北京时间小时的 token 数（范围内合计）。"""
    hours = [0] * HOURS
    for r in _fetch(
        conn,
        "SELECT CAST(strftime('%H', CAST(ts AS INTEGER) + ?, 'unixepoch') AS INTEGER) AS h,"
        " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens"
        " FROM usage WHERE day >= ? AND day <= ? GROUP BY h",
        (_BJ_OFFSET_SECONDS, from_key, to_key),
    ):
        try:
            h = int(r["h"])
        except (TypeError, ValueError):
            continue
        if 0 <= h < HOURS:
            hours[h] += int(r["tokens"] or 0)
    return hours


def _jev_stats(conn: Any, from_key: str, to_key: str) -> dict[str, int]:
    """范围内 Jev：calls / errors / avg_ms（整数毫秒）。"""
    rows = _fetch(
        conn,
        "SELECT COUNT(*) AS calls,"
        " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) AS errors,"
        " COALESCE(AVG(ms), 0) AS avg_ms"
        " FROM judgments WHERE day >= ? AND day <= ?",
        (from_key, to_key),
    )
    row = rows[0] if rows else None
    return {
        "calls": int(row["calls"] or 0) if row is not None else 0,
        "errors": int(row["errors"] or 0) if row is not None else 0,
        "avg_ms": _avg_ms(row["avg_ms"]) if row is not None else 0,
    }


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _conn(svc: Any) -> Any:
    store = getattr(svc, "store", None)
    try:
        return store.read() if store is not None else None
    except Exception:
        logger.exception("用量历史拿不到库连接")
        return None


def _fetch(conn: Any, sql: str, args: tuple) -> list[Any]:
    """查一把；表还没建（旧数据目录）等任何异常 → 空列表，绝不 500。"""
    if conn is None:
        return []
    try:
        return list(conn.execute(sql, args).fetchall())
    except Exception as e:
        logger.warning("用量历史查询失败（当空算）：%s", e)
        return []


def _group_name(svc: Any, group_id: str) -> str:
    """群名走 views 现成那套：读 groups 表 + names.clean_group_name 清洗。"""
    try:
        row = views._group_row(svc, group_id)
        name = row.get("name") if isinstance(row, dict) else None
    except Exception:
        name = None
    return clean_group_name(name or "", group_id)


def _avg_ms(value: Any) -> int:
    """平均毫秒取整数（四舍五入）。"""
    try:
        return int(float(value or 0) + 0.5)
    except (TypeError, ValueError):
        return 0
