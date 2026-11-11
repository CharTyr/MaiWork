"""用量提醒（docs/02 §7.2 最后一条）：token 不设硬上限，超阈值只提醒、不暂停。

取舍（2026-09 定）：提醒**只在网页设置页醒目标出**，不往群里发——
群里发用量既抢 MaiBot 的话筒，也未必只该管理员看；网页 settings 视图的
usage.alerts 就是它的展示位。任务照跑，不暂停任何东西。

后台循环每轮调 check()：
- alert_daily_tokens > 0：按服务群（usage 表的 group_id）算今天（北京日）
  主模型 + 子 agent 的 token 合计；超了且今天还没提醒过这个群 →
  记一条 kv「usage.alerted.<群号>.<日期>」防重复，同一天不再提醒；
- alert_task_tokens > 0：tasks 表 tokens 超线的任务，没提醒过
  （kv「usage.alerted.task.<任务号>」）→ 记一条。

kv 的 value 就是提醒本体（group_id / kind / text / ts）：同一个键既当防重复
标记、又当网页要读的数据，不另开表。check() 只写 kv，不碰发件箱、不动任务状态。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import clock

# 提醒记录都挂在这个前缀下，今天产生的靠 value.ts 过滤
ALERTED_PREFIX = "usage.alerted."

# 主模型 + 子 agent；别的 role（比如固定话术之类）不算用量提醒
_ROLES = ("main", "worker")


def daily_key(group_id: str, day: str) -> str:
    """日提醒的防重复键：按群 + 北京日。"""
    return f"{ALERTED_PREFIX}{group_id}.{day}"


def task_key(task_id: str) -> str:
    """单任务提醒的防重复键：一个任务只提醒一次。"""
    return f"{ALERTED_PREFIX}task.{task_id}"


def _wan(tokens: int) -> str:
    """token 数折成「万」的短写法：30000 → "3"，35000 → "3.5"。"""
    v = round(int(tokens) / 10000.0, 1)
    return str(int(v)) if v == int(v) else f"{v:.1f}"


def daily_text(tokens: int, threshold: int) -> str:
    return (
        f"今天 MaiWork 用了 {_wan(tokens)} 万 tokens，超过提醒线 {_wan(threshold)} 万了。"
        "只是提醒，不会停。"
    )


def task_text(task_id: str, title: str, tokens: int) -> str:
    return f"任务 {task_id}《{title}》已经用了 {_wan(tokens)} 万 tokens，超过单任务提醒线"


def _served_groups(settings: Any) -> list[str]:
    groups = getattr(settings, "groups", None)
    if not groups:
        return []
    try:
        return [str(g) for g in groups.keys()]
    except Exception:
        return []


def hard_key(day: str) -> str:
    """硬上限提醒的防重复键：全站一天一次。"""
    return f"{ALERTED_PREFIX}hard.{day}"


def daily_hard_limit_status(store: Any, settings: Any, now: float | None = None) -> dict:
    """今天（北京日）全站 token 是否撞了每日硬上限。

    返回 {"tripped", "limit", "used", "day"}；limit<=0 = 不设上限（tripped 恒 False、不查库）。
    读库失败按没撞（不因统计出错停掉后台）。
    """
    ts = clock.now() if now is None else float(now)
    day = clock.day_key(ts)
    try:
        limit = int(getattr(getattr(settings, "usage", None), "hard_limit_daily_tokens", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0 or store is None:
        return {"tripped": False, "limit": max(0, limit), "used": 0, "day": day}
    placeholders = ",".join("?" for _ in _ROLES)
    try:
        row = store.read().execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS n"
            f" FROM usage WHERE day = ? AND role IN ({placeholders})",
            (day, *_ROLES),
        ).fetchone()
        used = int(row["n"] or 0) if row is not None else 0
    except Exception:
        used = 0
    return {"tripped": used >= limit, "limit": limit, "used": used, "day": day}


def hard_limit_tripped(store: Any, get_settings: Any) -> dict | None:
    """后台自动活入口用：撞了返回状态 dict，没撞 / 出错返回 None。"""
    try:
        settings = get_settings() if callable(get_settings) else get_settings
        st = daily_hard_limit_status(store, settings)
    except Exception:
        return None
    return st if st.get("tripped") else None


def _remember(store: Any, key: str, alert: dict, now: float) -> None:
    """把提醒写进 kv（键防重复，值给网页读）。"""
    record = dict(alert)
    record["ts"] = float(now)
    with store.tx() as conn:
        store.kv_set(conn, key, record)


def _daily_alerts(store: Any, served: list[str], limit: int, now: float) -> list[dict]:
    day = clock.day_key(now)
    out: list[dict] = []
    placeholders = ",".join("?" for _ in _ROLES)
    for gid in served:
        key = daily_key(gid, day)
        if store.kv_get(key) is not None:
            continue  # 今天已经提醒过这个群了
        row = store.read().execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS n"
            f" FROM usage WHERE day = ? AND group_id = ? AND role IN ({placeholders})",
            (day, gid, *_ROLES),
        ).fetchone()
        tokens = int(row["n"] or 0) if row is not None else 0
        if tokens <= limit:  # 正好等于阈值不算「超」
            continue
        alert = {"group_id": gid, "kind": "daily", "text": daily_text(tokens, limit)}
        _remember(store, key, alert, now)
        out.append(alert)
    return out


def _task_alerts(store: Any, served: list[str], limit: int, now: float) -> list[dict]:
    if not served:
        return []
    in_clause = ",".join("?" for _ in served)
    rows = store.read().execute(
        "SELECT id, group_id, title, tokens FROM tasks"
        f" WHERE tokens > ? AND group_id IN ({in_clause}) ORDER BY tokens DESC, id",
        (limit, *served),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        tid = str(row["id"])
        key = task_key(tid)
        if store.kv_get(key) is not None:
            continue  # 这个任务提醒过了
        alert = {
            "group_id": str(row["group_id"]),
            "kind": "task",
            "text": task_text(tid, str(row["title"] or ""), int(row["tokens"] or 0)),
        }
        _remember(store, key, alert, now)
        out.append(alert)
    return out


def check(store: Any, get_settings: Callable[[], Any] | Any, now: float) -> list[dict]:
    """跑一轮用量检查：返回本轮新产生的提醒（已写 kv 防重复）。

    阈值 0 就是不提醒。返回 [{"group_id", "kind": "daily"|"task", "text"}]；
    只记 kv，不往群里发、不暂停任务。
    """
    if store is None:
        return []
    settings = get_settings() if callable(get_settings) else get_settings
    if settings is None:
        return []
    usage = getattr(settings, "usage", None)
    if usage is None:
        return []
    daily_limit = int(getattr(usage, "alert_daily_tokens", 0) or 0)
    task_limit = int(getattr(usage, "alert_task_tokens", 0) or 0)
    out: list[dict] = []
    hard = daily_hard_limit_status(store, settings, now)
    if hard["tripped"] and store.kv_get(hard_key(hard["day"])) is None:
        alert = {
            "group_id": "", "kind": "hard_limit",
            "text": (
                f"今天 MaiWork 已用 {_wan(hard['used'])} 万 tokens，达到每日硬上限 {_wan(hard['limit'])} 万，"
                "后台自动搜集已暂停，明早零点自动恢复。"
            ),
        }
        _remember(store, hard_key(hard["day"]), alert, now)
        out.append(alert)
    if daily_limit <= 0 and task_limit <= 0:
        return out
    served = _served_groups(settings)
    if daily_limit > 0:
        out.extend(_daily_alerts(store, served, daily_limit, now))
    if task_limit > 0:
        out.extend(_task_alerts(store, served, task_limit, now))
    return out


def today_alerts(store: Any, now: float) -> list[dict]:
    """今天（北京时间）产生的提醒，按时间排序——settings 视图的 usage.alerts 用它。

    返回 [{"group_id", "kind", "text", "ts"}]；读 kv 失败当没有，不影响页面其它部分。
    """
    if store is None:
        return []
    try:
        rows = store.read().execute(
            "SELECT value FROM kv WHERE key LIKE ?", (ALERTED_PREFIX + "%",)
        ).fetchall()
    except Exception:
        return []
    today = clock.day_key(now)
    out: list[dict] = []
    for row in rows:
        raw = row["value"]
        try:
            record = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError):
            record = None
        if not isinstance(record, dict):
            continue
        try:
            ts = float(record.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if clock.day_key(ts) != today:
            continue
        out.append(
            {
                "group_id": str(record.get("group_id") or ""),
                "kind": str(record.get("kind") or ""),
                "text": str(record.get("text") or ""),
                "ts": ts,
            }
        )
    out.sort(key=lambda a: a["ts"])
    return out
