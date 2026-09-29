"""console 的视图拼装：GroupSummary / GroupView / Settings。

纯函数，输入 services（store / models / profiles / get_settings / host / signals），
输出按 docs/07-代码接口.md §9.3 的结构；这里不写库（建行、生成 token 是 app 的事）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .. import clock, members
from ..names import clean_group_name

logger = logging.getLogger("maiwork.console.views")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 画像五类：key -> 显示名（和前端 CATS 一致）
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("recent", "最近在聊"),
    ("interest", "长期兴趣"),
    ("ongoing", "在做的事"),
    ("convention", "约定和说法"),
    ("resource", "常用资源"),
)
CATEGORY_KEYS = {k for k, _ in CATEGORIES}

# 不适合当群图标的：设置、锁、睡觉
_ICON_EXCLUDE = {"gear", "lock", "sleeping"}

# 模型调用目的（models.chat 的 purpose）→ 网页显示的中文名（docs/07 §9.4）
# 代码里实际出现的全列上；没认出来的 purpose 在接口里原样返回（前端拿原名当备用）。
PURPOSE_NAMES: dict[str, str] = {
    "profile.refresh": "整理群画像",
    "profile.weekly": "每周整理群画像",
    "feeds.focus": "资讯定关注点",
    "feeds.score": "资讯打分",
    "feeds.post": "写资讯帖子",
    "feeds.idea": "想构想",
    "feeds.verify_plan": "挑实测",
    "worker": "子 agent",
    "coordinator.plan": "派活计划",
    "coordinator.review": "验收",
    "coordinator.groupspace": "群空间探活",
    "coordinator.remember": "记下任务经验",
    "coordinator.check_goal": "目标检查点",
    "persona.refresh": "人物画像",
    "personal.focus": "个人向关注点",
    "personal.score": "个人向资讯打分",
    "personal.post": "写个人向帖子",
    "opener": "开场白",
    "reminder_parse": "解析提醒",
}


def purpose_name(purpose: Any) -> str:
    """purpose 的中文显示名；没认出来的原样返回原串。"""
    p = str(purpose or "").strip()
    if not p:
        return ""
    return PURPOSE_NAMES.get(p, p)

_icons_cache: list[str] | None = None


def group_icons() -> list[str]:
    """static/assets/icons 里可用作群图标的名字（稳定排序，去掉不合适的）。"""
    global _icons_cache
    if _icons_cache is None:
        icons_dir = _STATIC_DIR / "assets" / "icons"
        try:
            names = sorted(p.stem for p in icons_dir.glob("*.png"))
        except OSError:
            names = []
        _icons_cache = [n for n in names if n and n not in _ICON_EXCLUDE]
    return _icons_cache


def icon_for_group(group_id: str) -> str:
    """按群号稳定挑一个图标（md5 取模）。"""
    icons = group_icons()
    if not icons:
        return "sparkles"
    digest = hashlib.md5(str(group_id).encode("utf-8")).digest()
    return icons[int.from_bytes(digest[:4], "big") % len(icons)]


# ----------------------------------------------------------------------
# 群行读取
# ----------------------------------------------------------------------


def _group_row(svc: Any, group_id: str) -> dict[str, Any]:
    """读 groups 表的一行；没有则返回全零行（不写库）。"""
    try:
        row = svc.store.read().execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    except Exception:  # 库还没开 / 表还没建
        row = None
    if row is None:
        return {
            "group_id": group_id,
            "workspace": "",
            "session_id": "",
            "name": "",
            "member_count": 0,
            "token": "",
            "created": 0.0,
            "last_msg_ts": 0.0,
            "cursor_ts": 0.0,
            "cursor_ids": "[]",
            "read_since": 0.0,
            "profile_ready_ts": 0.0,
            "last_refresh_ts": 0.0,
            "last_weekly_ts": 0.0,
            "fail_count": 0,
        }
    return dict(row)


def _served_group_ids(svc: Any) -> list[str]:
    """只列 settings.groups 里的服务群（groups 表里有但配置里没有的不显示）。"""
    settings = svc.get_settings()
    if settings is None:
        return []
    return list(settings.groups.keys())


def _group_avatar_path(svc: Any, group_id: str) -> str:
    """群对象的 avatar 字段（/api/avatar/g/<token>）；非 QQ 形态的群号给空串，建不起服务也给空串。"""
    try:
        from .avatar import service_of

        av = service_of(svc)
        if av is None:
            return ""
        return str(av.group_avatar_path(group_id) or "")
    except Exception:
        return ""


# ----------------------------------------------------------------------
# GroupSummary / GroupView
# ----------------------------------------------------------------------


def _m3_today_counts(svc: Any, group_id: str) -> dict[str, int]:
    """today.pending = 本群待批件数；today.running = 本群 running 任务数（真值）。"""
    pending = 0
    try:
        appr = getattr(svc, "approvals", None)
        if appr is not None:
            pending = len(appr.pending_view(group_id) or [])
    except Exception:
        pending = 0
    running = 0
    try:
        tasks = getattr(svc, "tasks", None)
        if tasks is not None:
            running = int(tasks.running_count(group_id) or 0)
    except Exception:
        running = 0
    return {"pending": pending, "running": running}


def _base_summary(svc: Any, group_id: str, row: dict[str, Any], now: float, *, m2: dict[str, int] | None = None) -> dict[str, Any]:
    last_msg = float(row.get("last_msg_ts") or 0.0)
    try:
        signal_ts = float(svc.signals.last_ts(group_id) or 0.0)
    except Exception:
        signal_ts = 0.0
    last_msg = max(last_msg, signal_ts)
    try:
        usual_gap = svc.profiles.usual_gap(group_id, now)
    except Exception:
        usual_gap = None
    m3 = _m3_today_counts(svc, group_id)
    return {
        "id": group_id,
        # 群名统一清洗（names.py：库里可能还有旧脏行，视图层兜底再洗一遍，老库不用迁移）
        "name": clean_group_name(row.get("name") or "", group_id),
        "icon": icon_for_group(group_id),
        "avatar": _group_avatar_path(svc, group_id),
        "members": int(row.get("member_count") or 0),
        "token": str(row.get("token") or ""),
        "fresh": not bool(row.get("profile_ready_ts")),
        "quiet": {
            "last_msg_ts": last_msg,
            "usual_gap_s": float(usual_gap) if isinstance(usual_gap, (int, float)) else None,
        },
        "today": {
            "news": int((m2 or {}).get("news", 0)),
            "topics": int((m2 or {}).get("topics", 0)),
            "pending": m3["pending"],
            "running": m3["running"],
        },
    }


def group_summary(svc: Any, group_id: str, *, admin: bool) -> dict[str, Any]:
    now = clock.now()
    row = _group_row(svc, group_id)
    out = _base_summary(svc, group_id, row, now)
    if not admin:
        out.pop("token", None)
    return out


def list_summaries(svc: Any, *, admin: bool, only_group_id: str | None = None) -> list[dict[str, Any]]:
    ids = _served_group_ids(svc)
    if only_group_id is not None:
        ids = [g for g in ids if g == only_group_id]
    now = clock.now()
    out = []
    for gid in ids:
        row = _group_row(svc, gid)
        item = _base_summary(svc, gid, row, now, m2=_today_counts(svc, gid, now))
        if not admin:
            item.pop("token", None)
        out.append(item)
    return out


def _profile_sections(svc: Any, group_id: str) -> list[dict[str, Any]]:
    try:
        entries = svc.profiles.entries(group_id)
    except Exception:
        entries = []
    by_cat: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in CATEGORIES}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if e.get("deleted"):
            continue
        cat = str(e.get("category") or "")
        if cat not in by_cat:
            continue
        by_cat[cat].append(
            {
                "id": int(e.get("id") or 0),
                "text": str(e.get("text") or ""),
                "evidence_count": int(e.get("evidence_count") or 0),
                "first_ts": float(e.get("first_ts") or 0.0),
                "last_ts": float(e.get("last_ts") or 0.0),
                "locked": bool(e.get("locked")),
                "source": str(e.get("source") or "model"),
            }
        )
    return [{"category": k, "name": name, "entries": by_cat[k]} for k, name in CATEGORIES]


# ----------------------------------------------------------------------
# M2 视图（docs/07 §9.3）：news / ideas / topic_log / 冷场段 / 今日计数
# ----------------------------------------------------------------------


def _start_of_today(now: float) -> float:
    """北京时间今天 0 点的 epoch 秒。"""
    dt = clock.bj(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return float(dt.timestamp())


def _safe_jsonable(value: Any) -> Any:
    """只保留能 JSON 序列化的基本结构；兜底保视图不 500。"""
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v) for k, v in value.items() if isinstance(k, (str, int))}
    if isinstance(value, (list, tuple)):
        return [_safe_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _today_counts(svc: Any, group_id: str, now: float) -> dict[str, int]:
    """today.news = feeds.today_count；today.topics = topic_log 里今天开了话题的次数。"""
    news_n = 0
    try:
        if getattr(svc, "feeds", None) is not None:
            news_n = int(svc.feeds.today_count(group_id) or 0)
    except Exception:
        news_n = 0
    topics_n = 0
    try:
        if getattr(svc, "topics", None) is not None:
            start = _start_of_today(now)
            for e in svc.topics.log_view(group_id, days=3) or []:
                if not isinstance(e, dict):
                    continue
                try:
                    ts = float(e.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ts >= start and str(e.get("opener") or ""):
                    topics_n += 1
    except Exception:
        topics_n = 0
    return {"news": news_n, "topics": topics_n}


def _pulse_topics(topic_log: list[Any], now: float) -> tuple[list[dict[str, Any]], dict[float, bool]]:
    """从开了话题的 topic_log 拼 pulse.topics；顺手把每条记录划进时段桶。

    返回 (topics, buckets)：buckets 记 900 秒桶起点 → 这个桶里开过话题没有。
    """
    topics: list[dict[str, Any]] = []
    buckets: dict[float, bool] = {}
    step = 900.0
    span = 96 * step
    for e in topic_log or []:
        if not isinstance(e, dict):
            continue
        try:
            ts = float(e.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts <= now - span or ts > now:
            continue
        b = ts - (ts % step)
        pick = e.get("pick")
        title = ""
        if isinstance(pick, dict):
            title = str(pick.get("title") or "")
        opener = str(e.get("opener") or "")
        result = e.get("result")
        replies = 0
        if isinstance(result, dict):
            try:
                replies = int(result.get("replies") or 0)
            except (TypeError, ValueError):
                replies = 0
        if opener and title:
            buckets[b] = True
            topics.append({"at": ts, "label": title, "replies": replies})
        else:
            buckets.setdefault(b, False)
    topics.sort(key=lambda x: float(x["at"]))
    return topics, buckets


def _pulse_spells(topic_log: list[Any], now: float) -> list[dict[str, Any]]:
    """topic_log 每条记录生成一个冷场段：{from: ts-quiet_s, to: ts, note}。"""
    out: list[dict[str, Any]] = []
    step = 900.0
    span = 96 * step
    for e in topic_log or []:
        if not isinstance(e, dict):
            continue
        try:
            ts = float(e.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts <= now - span or ts > now:
            continue
        try:
            quiet_s = max(0.0, float(e.get("quiet_s") or 0.0))
        except (TypeError, ValueError):
            quiet_s = 0.0
        minutes = int(quiet_s // 60)
        opened = bool(str(e.get("opener") or ""))
        note = f"冷场 {minutes} 分钟 · {'开了话题' if opened else '没开'}"
        out.append({"from": ts - quiet_s, "to": ts, "note": note})
    out.sort(key=lambda x: float(x["from"]))
    return out


def _m2_lists(svc: Any, group_id: str, now: float, *, admin: bool) -> tuple[list, list, list, list, list, list]:
    """一次取齐 news / guides / ideas / topic_log / pulse.topics / pulse.spells；模块没开全空。

    news 的 rejected 一栏只给管理员（群友版 batch 里没有 rejected 键）。
    """
    news: list = []
    guides: list = []
    ideas: list = []
    topic_log: list = []
    try:
        if getattr(svc, "feeds", None) is not None:
            news = [_safe_jsonable(x) for x in (svc.feeds.news_view(group_id, admin=admin) or []) if isinstance(x, dict)]
            guides = [_safe_jsonable(x) for x in (svc.feeds.guides_view(group_id, admin=admin) or []) if isinstance(x, dict)]
            ideas = [_safe_jsonable(x) for x in (_ideas_of(svc.feeds, group_id, admin) or []) if isinstance(x, dict)]
    except Exception:
        news, guides, ideas = [], [], []
    try:
        if getattr(svc, "topics", None) is not None:
            topic_log = [_safe_jsonable(x) for x in (svc.topics.log_view(group_id) or []) if isinstance(x, dict)]
    except Exception:
        topic_log = []
    topics_opened, _buckets = _pulse_topics(topic_log, now)
    spells = _pulse_spells(topic_log, now)
    return news, guides, ideas, topic_log, topics_opened, spells


def _ideas_of(feeds: Any, group_id: str, admin: bool) -> list:
    """构想列表（含「给某人的」）；群友版里个人向构想的 basis 由 feeds 清空。"""
    try:
        return feeds.ideas_view(group_id, admin=admin)
    except TypeError:  # 老接口 / 测试替身不认 admin
        return feeds.ideas_view(group_id)


def _attach_idea_targets(svc: Any, group_id: str, ideas: list, focus: list, *, admin: bool = True) -> None:
    """给「给某人的」构想挂 for_member {user_id, name, avatar}（卡片底部画头像 + 名字）。

    名字先用关注名单里的显示名；不在名单里 → 用库里存量名字；绝不把 QQ 号当名字。
    群友版不给头像地址（关注成员头像接口只给管理员）。
    """
    known = {str(p.get("user_id") or ""): p for p in focus or [] if isinstance(p, dict)}
    for it in ideas:
        if not isinstance(it, dict):
            continue
        # basis / reason / why 可能带 {@QQ号} 记号（个人向构想的「因为他是…」）——渲染成当前名字
        for key in ("basis", "reason", "why"):
            v = str(it.get(key) or "")
            if "{@" in v:
                try:
                    it[key] = members.render(svc.store, group_id, v)
                except Exception:
                    pass
        uid = str(it.pop("target_user_id", "") or "")
        if not uid:
            it["for_member"] = None
            continue
        p = known.get(uid)
        if p is not None:
            name, avatar = str(p.get("display_name") or p.get("name") or ""), str(p.get("avatar") or "")
        else:
            name = ""
            try:
                row = svc.store.read().execute(
                    "SELECT name FROM focus_members WHERE group_id=? AND user_id=?", (group_id, uid)
                ).fetchone()
                if row is not None:
                    name = _member_display_name({"name": row["name"]}, uid)
            except Exception:
                name = ""
            avatar = _member_avatar_path(svc, group_id, uid) if admin else ""
        if not admin:
            avatar = ""
        if name == uid:
            name = ""
        it["for_member"] = {"user_id": uid, "name": name, "avatar": avatar}


def _persona_view(raw_persona: Any, raw_ts: Any, *, gid: str = "", svc: Any = None) -> dict[str, Any] | None:
    """把 focus_members.persona（JSON）转成 GroupView.focus 每项的 persona 字段。

    只给管理员看；结构固定五键加 updated_ts；库里没有 / JSON 拿不出来 → None。
    正文里的 {@QQ号} 一律渲染成名册里的当前名字（QQ 号不当名字给人看）。
    """
    s = str(raw_persona or "").strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    def _r(text: Any) -> str:
        if svc is None:
            return str(text or "")
        try:
            return members.render(svc.store, gid, text) or str(text or "")
        except Exception:
            return str(text or "")

    def _str_list(v: Any) -> list[str]:
        return [_r(x) for x in v] if isinstance(v, list) else []
    persona = {
        "summary": _r(parsed.get("summary")),
        "doing": _str_list(parsed.get("doing")),
        "cares": _str_list(parsed.get("cares")),
        "asked": _str_list(parsed.get("asked")),
        "style": _r(parsed.get("style")),
        "updated_ts": float(raw_ts or 0.0),
    }
    return persona


def _member_display_name(member: dict[str, Any], user_id: str) -> str:
    """关注成员的显示名：群名片（QQ 昵称）> 群名片 / 昵称 > 存量 name。

    - card 空或与 nickname 相同 → 只留一个；
    - 两个都没有 → 用 focus_members.name（存量）；
    - 结果为空、或恰好是 QQ 号 / 平台号（== user_id）→ 留空，绝不把账号当显示名。
    """
    card = str(member.get("card") or "").strip()
    nick = str(member.get("nickname") or "").strip()
    name = ""
    if card and nick and card != nick:
        name = f"{card}（{nick}）"
    else:
        name = card or nick
    if not name:
        name = str(member.get("name") or "").strip()
    if not name or name == str(user_id or "").strip():
        return ""
    return name


def _member_avatar_path(svc: Any, group_id: str, user_id: str) -> str:
    """关注成员头像（/api/avatar/m/<token>）；只有 QQ（user_id 纯数字）才有，其余留空。"""
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return ""
    try:
        from .avatar import service_of

        av = service_of(svc)
        if av is None:
            return ""
        return av.member_avatar_path(av.member_token(str(group_id), uid))
    except Exception:
        return ""


def _focus_list(svc: Any, group_id: str) -> list[dict[str, Any]]:
    try:
        people = svc.profiles.focus(group_id)
    except Exception:
        people = []
    out = []
    for m in people or []:
        if not isinstance(m, dict) or m.get("removed"):
            continue
        reasons = m.get("reasons")
        persona_view = _persona_view(m.get("persona"), m.get("persona_ts"), gid=group_id, svc=svc)
        user_id = str(m.get("user_id") or "")
        display_name = _member_display_name(m, user_id)
        try:
            note = members.render(svc.store, group_id, m.get("note") or "")
        except Exception:
            note = str(m.get("note") or "")
        out.append(
            {
                "user_id": user_id,
                # name 只放安全的显示名（存量 name 就是 QQ 号时留空，账号只留在 user_id 里）
                "name": display_name,
                "display_name": display_name,
                "avatar": _member_avatar_path(svc, group_id, user_id),
                "reasons": [str(r) for r in reasons] if isinstance(reasons, list) else [],
                # 注记里的 {@QQ号} 渲染成当前名字（管理员看的是人，不是数字）
                "note": note,
                "pinned": bool(m.get("pinned")),
                "persona": persona_view,
                "personal": _personal_view(svc, group_id, str(m.get("user_id") or "")),
            }
        )
    return out


def _personal_view(svc: Any, group_id: str, user_id: str) -> dict[str, Any]:
    """GroupView.focus[].personal（只给管理员）：最近 7 天个人向资讯（≤5）/ 构想（≤2）+ last_ts。

    personal.py 没就位（老部署）→ 空结构；任何异常都回落空，不拖垮 focus。
    """
    empty = {"news": [], "ideas": [], "last_ts": None}
    personal = getattr(svc, "personal", None)
    if personal is None:
        return empty
    try:
        out = personal.focus_personal_view(group_id, user_id, days=7)
    except Exception:
        return empty
    if not isinstance(out, dict):
        return empty
    return {
        "news": [_safe_jsonable(x) for x in (out.get("news") or []) if isinstance(x, dict)][:5],
        "ideas": [_safe_jsonable(x) for x in (out.get("ideas") or []) if isinstance(x, dict)][:2],
        "last_ts": out.get("last_ts"),
    }


def _goals_view(svc: Any, group_id: str) -> dict[str, Any]:
    """GroupView.goals：Goals.view（{"agent": [...], "member": [...]}）；模块没开全空。"""
    goals = getattr(svc, "goals", None)
    if goals is None:
        return {"agent": [], "member": []}
    try:
        v = goals.view(group_id) or {}
        return {
            "agent": [_safe_jsonable(x) for x in (v.get("agent") or []) if isinstance(x, dict)],
            "member": [_safe_jsonable(x) for x in (v.get("member") or []) if isinstance(x, dict)],
        }
    except Exception:
        return {"agent": [], "member": []}


def _tasks_view(svc: Any, group_id: str) -> dict[str, Any]:
    """GroupView.tasks：pending = approvals.pending_view；list = tasks.list_view（每条带 undelivered）。"""
    pending: list = []
    try:
        appr = getattr(svc, "approvals", None)
        if appr is not None:
            pending = [_safe_jsonable(x) for x in (appr.pending_view(group_id) or []) if isinstance(x, dict)]
    except Exception:
        pending = []
    lst: list = []
    try:
        tasks = getattr(svc, "tasks", None)
        if tasks is not None:
            lst = [_safe_jsonable(x) for x in (tasks.list_view(group_id) or []) if isinstance(x, dict)]
    except Exception:
        lst = []
    try:
        delivery = getattr(svc, "delivery", None)
        if delivery is not None:
            for item in lst:
                try:
                    item["undelivered"] = bool(delivery.undelivered(item.get("id")))
                except Exception:
                    pass
    except Exception:
        pass
    # 批准信息（自动审核 / 人批）：approved_by + auto_reason，任务里能看到
    # 「自动审核通过：<理由>」。一次查完，不逐条查。
    try:
        appr = getattr(svc, "approvals", None)
        if appr is not None and lst:
            info = appr.auto_info_by_task([str(x.get("id") or "") for x in lst])
            for item in lst:
                got = info.get(str(item.get("id") or ""))
                if got:
                    item.update(got)
    except Exception:
        pass
    return {"pending": pending, "list": lst}


def _upcoming(svc: Any, group_id: str, row: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """M1 很简单：群画像还没成形 → 读够记录后整理；否则模型配好了给「下一次读群」。
    M2：scheduler 给了「下一批资讯备料」的时间就也列上。
    M3：追加最近的成员提醒和目标检查（最多 3 条）。"""
    out: list[dict[str, Any]] = []
    if not row.get("profile_ready_ts"):
        out.append({"icon": "seedling", "at": None, "text": "读够聊天记录后整理群画像"})
    else:
        try:
            ready = svc.models.settings().ready()
        except Exception:
            ready = False
        if ready:
            settings = svc.get_settings()
            interval_min = int(settings.profile.read_interval_minutes or 0) if settings is not None else 0
            last_read = max(float(row.get("last_refresh_ts") or 0.0), float(row.get("last_msg_ts") or 0.0))
            if interval_min > 0:
                at = last_read + interval_min * 60 if last_read > 0 else now + interval_min * 60
                out.append({"icon": "books", "at": at, "text": "下一次读群"})
        try:
            scheduler = getattr(svc, "scheduler", None)
            if scheduler is not None:
                at = scheduler.next_news_ts(group_id, now)
                if isinstance(at, (int, float)) and float(at) > 0:
                    out.append({"icon": "newspaper", "at": float(at), "text": "下一批资讯备料"})
        except Exception:
            pass
    # M3：成员提醒 + agent 目标检查（最近的在前，最多 3 条）
    try:
        goals = getattr(svc, "goals", None)
        if goals is not None:
            v = goals.view(group_id) or {}
            extra: list[dict[str, Any]] = []
            for m in v.get("member") or []:
                if not isinstance(m, dict) or str(m.get("state")) != "active":
                    continue
                at = m.get("remind_ts") or m.get("due_ts")
                if not at:
                    continue
                who = str(m.get("who") or "")
                title = str(m.get("title") or "")[:20]
                text = f"提醒 {who}：{title}" if who else f"成员提醒：{title}"
                extra.append({"icon": "alarm", "at": float(at), "text": text})
            for g in v.get("agent") or []:
                if not isinstance(g, dict) or str(g.get("state")) != "active":
                    continue
                at = g.get("next_check_ts")
                if not at:
                    continue
                extra.append({"icon": "bullseye", "at": float(at), "text": f"检查目标「{str(g.get('title') or '')[:20]}」"})
            extra.sort(key=lambda e: float(e["at"]))
            out.extend(extra[:3])
    except Exception:
        pass
    return out


def group_view(svc: Any, group_id: str, *, admin: bool) -> dict[str, Any]:
    settings = svc.get_settings()
    now = clock.now()
    row = _group_row(svc, group_id)
    out = _base_summary(svc, group_id, row, now, m2=_today_counts(svc, group_id, now))
    workspace = settings.workspace_of(group_id) if settings is not None else str(row.get("workspace") or "")
    try:
        bins = svc.profiles.pulse(group_id, end=now)
    except Exception:
        bins = []
    bins_list = [int(x) for x in bins] if isinstance(bins, (list, tuple)) else []
    sleep = settings.delivery.quiet_hours if settings is not None else "23:00-08:00"
    read_since = float(row.get("read_since") or 0.0)
    news, guides, ideas, topic_log, pulse_topics, pulse_spells = _m2_lists(svc, group_id, now, admin=admin)
    feeds_pref = ""
    try:
        if getattr(svc, "feeds", None) is not None:
            feeds_pref = str(svc.feeds.pref(group_id) or "")
    except Exception:
        feeds_pref = ""
    view: dict[str, Any] = {
        "workspace": workspace,
        "read_since": read_since if read_since > 0 else None,
        "feeds_pref": feeds_pref,
        "pulse": {
            "end": now,
            "step": 900,
            "bins": bins_list,
            "sleep": sleep,
            "spells": pulse_spells,
            "topics": pulse_topics,
        },
        "profile": _profile_sections(svc, group_id),
        "news": news,
        "guides": guides,
        "ideas": ideas,
        "goals": _goals_view(svc, group_id),
        "tasks": _tasks_view(svc, group_id),
        "topic_log": topic_log,
        "upcoming": _upcoming(svc, group_id, row, now),
    }
    if admin:
        out["focus"] = _focus_list(svc, group_id)
        _attach_idea_targets(svc, group_id, ideas, out["focus"])
        # 群空间（docs/02 §10）：仅管理员；模块没建起来给全 False + 空身份
        gs = getattr(svc, "group_space", None)
        try:
            caps = gs.capabilities(group_id) if gs is not None else {}
            role = gs.role_of(group_id) if gs is not None else ""
        except Exception:
            caps, role = {}, ""
        view["group_space"] = {
            "files_list": bool(caps.get("files_list")),
            "files_manage": bool(caps.get("files_manage")),
            "notice_send": bool(caps.get("notice_send")),
            "album_upload": bool(caps.get("album_upload")),
            "role": str(role or ""),
        }
        # 往群里发（资讯卡片 / 构想提一嘴）的开关：仅管理员 / 本群群管理员
        try:
            from .. import card_push as _cp

            view["card_push"] = _cp.web_view(svc, group_id)
        except Exception:
            logger.exception("读卡片推送设置失败（群 %s）", group_id)
    else:
        _attach_idea_targets(svc, group_id, ideas, [], admin=False)
        out.pop("token", None)
        out.pop("focus", None)
        # G3：群友不提供工作区名（实现细节）；sleep 保留（前端要画睡觉时段）
        view.pop("workspace", None)
        # 群友也看得到开话题记录（设计允许），但不带管理员的判定字段
        view["topic_log"] = [
            {k: v for k, v in e.items() if k != "verdict"} for e in topic_log
        ]
    out.update(view)
    return out


# ----------------------------------------------------------------------
# Settings 视图
# ----------------------------------------------------------------------


def _recent_model_errors(store: Any, now: float) -> bool:
    """近 1 小时 usage 有没有错误。"""
    try:
        row = store.read().execute(
            "SELECT COUNT(*) AS n FROM usage WHERE ok = 0 AND ts >= ?", (now - 3600,)
        ).fetchone()
        return bool(row and int(row["n"]) > 0)
    except Exception:
        return False


def _jev_health(svc: Any) -> dict[str, Any]:
    """Jev 健康项：available → ok「能用 · 今天判断 N 次」；没密钥 → warn；关了 → off。"""
    jev = getattr(svc, "jev", None)
    if jev is None:
        return {"key": "jev", "icon": "sparkles", "name": "快速判断", "state": "off", "text": "还没启用"}
    settings = svc.get_settings()
    if settings is not None and not bool(getattr(settings.jev, "enabled", True)):
        return {"key": "jev", "icon": "sparkles", "name": "快速判断", "state": "off", "text": "已关闭"}
    try:
        if jev.available():
            try:
                n = int(jev.calls_today())
            except Exception:
                n = 0
            return {"key": "jev", "icon": "sparkles", "name": "快速判断", "state": "ok", "text": f"能用 · 今天判断 {n} 次"}
    except Exception:
        pass
    key = ""
    try:
        key = jev._key()  # noqa: SLF001 —— 网页要分清「没密钥」和「熔断」，只能问它
    except Exception:
        key = ""
    if not key:
        return {"key": "jev", "icon": "sparkles", "name": "快速判断", "state": "warn", "text": "没找到密钥"}
    return {"key": "jev", "icon": "sparkles", "name": "快速判断", "state": "warn", "text": "连续失败在熔断，过会儿自己恢复"}


def _search_health(svc: Any) -> dict[str, Any]:
    """搜索健康项（2026-10：搜索只走「扩展」里绑定的那个 MCP，绑定存数据库）。

    ok：「用 <扩展名> 的 <工具名>」；warn：没绑定 / 扩展不在了 / 没启用 / 没连上 / 工具不在
    （中文说明来自 search.status()，和 GET /api/extensions/search 的 status 同一份）。
    """
    search = getattr(svc, "search", None)
    if search is None:
        return {"key": "search", "icon": "magnifier", "name": "搜索", "state": "warn",
                "text": "还没选：去「设置 → 扩展」选一个"}
    try:
        ok, text = search.status()
    except Exception:
        ok, text = False, "搜索状态读不出来（扩展还没就位？）"
    return {"key": "search", "icon": "magnifier", "name": "搜索", "state": "ok" if ok else "warn", "text": text}


def _reader_health(svc: Any) -> dict[str, Any]:
    """「打开网页」健康项：三条路现在的样子（Jina Reader → 抓网页正文工具 → 直接打开）。

    ok：Jina 能用（或关着、走老顺序）；warn：Jina 在冷却（被限流 / 密钥不对 / 额度用完）。
    """
    search = getattr(svc, "search", None)
    ex_ok, ex_text = False, "没选抓正文工具"
    avail = getattr(search, "extract_available", None)
    if callable(avail):
        try:
            ex_ok, ex_text = avail()
        except Exception:
            ex_ok, ex_text = False, "抓正文工具状态读不出来"
    extract_part = f"抓正文工具（{ex_text}）"
    reader = getattr(svc, "reader", None)
    enabled = False
    if reader is not None:
        try:
            enabled = bool(reader.enabled())
        except Exception:
            enabled = False
    if not enabled:
        text = f"Jina Reader 关着：先直接打开，被网站拦了再用{extract_part}"
        return {"key": "reader", "icon": "books", "name": "打开网页", "state": "ok", "text": text}
    try:
        r_ok, r_text = reader.status()
    except Exception:
        r_ok, r_text = False, "Jina Reader 状态读不出来"
    text = f"{r_text} → {extract_part} → 直接打开"
    return {"key": "reader", "icon": "books", "name": "打开网页", "state": "ok" if r_ok else "warn", "text": text}


def _ssh_health(svc: Any) -> dict[str, Any] | None:
    """专用机器健康项：每台连不连得上；带上 MaiWork 的公钥（copy 字段，前端给复制按钮）。

    - SshEnv 没就位 → None（不放进列表）；
    - 没配机器 → off，告诉用户怎么加（公钥照样给，可以先加上）；
    - 都连得上 → ok「甲、乙 都连得上」；有连不上的 → warn，逐台写原因；还没检查过 → 「正在检查」。
    """
    ssh = getattr(svc, "ssh", None)
    if ssh is None:
        return None
    base = {"key": "ssh", "icon": "monitor", "name": "专用机器"}
    try:
        pub = str(ssh.public_key() or "")
    except Exception:
        pub = ""
    if pub:
        base["copy"] = pub
    try:
        st = list(ssh.status() or [])
    except Exception:
        st = []
    if not st:
        return {**base, "state": "off",
                "text": "没配置。要用自己的 VPS / VM：在「专用 SSH 机器」里加上，并把下面的公钥加进那台机器的 ~/.ssh/authorized_keys"}
    bad = [m for m in st if m.get("ok") is False]
    unknown = [m for m in st if m.get("ok") is None]
    if bad:
        text = "；".join(f"{m.get('name')}：{m.get('error') or '连不上'}" for m in bad)
        return {**base, "state": "warn", "text": text}
    if unknown:
        return {**base, "state": "warn", "text": "正在检查：" + "、".join(str(m.get("name")) for m in unknown)}
    busy = [str(m.get("name")) for m in st if m.get("busy")]
    text = "、".join(str(m.get("name")) for m in st) + " 都连得上"
    if busy:
        text += f"（{'、'.join(busy)} 正在干活）"
    return {**base, "state": "ok", "text": text}


def _localenv_health(svc: Any) -> dict[str, Any]:
    """本机干活健康项（M3）：按启动时的执行方式判定写大白话。

    - fixed（有 run_as 固定账号）→ ok「隔离运行（固定账号 maiwork）」；
    - dynamic（没这个账号，systemd 自动分配临时账号）→ ok「隔离运行（自动分配临时账号，不用建用户）」；
    - stopped（macOS / Windows / 不是 root / 没 systemd）→ off「没开（原因…）；跑命令的活交给 Railway 或做不了」；
    - 受限时不管 local_mode 写的是什么，都按「没开」说（受限下命令工具根本没注册，
      写 direct 也只是文件工具用得上）；
    - 没判定过（老 stub / 没启动）→ 按 local_mode 回落成老文案（direct 仍 warn）。
    """
    settings = svc.get_settings()
    env = getattr(settings, "environments", None) if settings is not None else None
    mode = str(getattr(env, "local_mode", "") or "").strip() if env is not None else ""
    base = {"key": "localenv", "icon": "monitor", "name": "本机干活"}
    cap = getattr(svc, "capability", None)
    cap_mode = str(getattr(cap, "mode", "") or "")
    if cap_mode == "stopped":
        why = str(getattr(cap, "reason", "") or "这台机器不能隔离跑命令")
        return {**base, "state": "off", "text": f"没开（{why}）；跑命令的活交给专用机器或 Railway，都没有就做不了"}
    if mode == "direct":
        return {**base, "state": "warn", "text": "直跑模式没有隔离，只能本地测试用"}
    if cap_mode == "fixed":
        run_as = str(getattr(env, "run_as", "") or "maiwork") if env is not None else "maiwork"
        return {**base, "state": "ok", "text": f"隔离运行（固定账号 {run_as}）"}
    if cap_mode == "dynamic":
        return {**base, "state": "ok", "text": "隔离运行（自动分配临时账号，不用建用户）"}
    if mode == "systemd":
        n = int(getattr(env, "max_parallel", 2) or 2) if env is not None else 2
        return {**base, "state": "ok", "text": f"systemd 隔离 · 同时最多 {n} 个子 agent"}
    return {**base, "state": "off", "text": "还没启用"}


def _rss_by_group(svc: Any) -> dict[str, list[dict[str, Any]]]:
    """settings.feeds.rss：{群号: [{id,url,title,enabled,added_ts,last_ok_ts,last_error}]}；模块没装全 / 群号错 → 空。

    取数据的 canonical 实现是 rss.list_feeds；出问题静默空（设置页不能因为 RSS 坏掉）。
    """
    try:
        from .. import rss as _rss  # noqa: SLF001  # 借用 _key/list_feeds 的公共路径

        out: dict[str, list[dict[str, Any]]] = {}
        for gid in _served_group_ids(svc):
            out[gid] = [
                {k: v for k, v in e.items()}
                for e in _rss.list_feeds(svc.store, gid)
            ]
        return out
    except Exception:
        return {gid: [] for gid in _served_group_ids(svc)}


def _groupspace_health(svc: Any) -> dict[str, Any]:
    """群空间健康项（docs/02 §10）：
    - [group_space] enabled=false 或模块没建起来 → off；
    - 适配器开放了（探测到新版接口）→ ok「能管群文件 / 公告 / 相册（按群看身份）」；
    - 没开放（旧版 0.8.5）→ warn「QQ 适配器是旧版…升级到 v1.0.1 后自动开放」。
    """
    base = {"key": "group_space", "icon": "filebox", "name": "群空间"}
    gs = getattr(svc, "group_space", None)
    if gs is None:
        return {**base, "state": "off", "text": "已关闭"}
    try:
        opened = bool(gs.adapter_open())
    except Exception:
        opened = False
    if opened:
        return {**base, "state": "ok", "text": "能管群文件、公告和相册"}
    return {
        **base, "state": "warn",
        "text": "暂时用不了：QQ 适配器是旧版，升级到 v1.0.1 就行",
    }


def _railway_health(svc: Any) -> dict[str, Any] | None:
    """一次性 VM（railway.new）健康项（docs/09 实测）。

    - [environments] 里没有 environments 节（老 stub）→ None，不出现在健康列表里；
    - railway=false → off；
    - 开着 → ok「今天用了 N/<railway_daily_max> 台」（N = 今天成功拿到的台数）；
    - 最近一次申请没拿到（railway.last_fail 在最近 24 小时内、且之后没再成功拿到过）
      → warn「最近一次没拿到机器：<原因>」。
    状态全部从 kv 读（railway.day.<北京日期> / railway.last_fail / railway.last_acquire），
    网页不用有 RailwayEnv 实例也能显示。
    """
    base = {"key": "railway", "icon": "cloud", "name": "一次性 VM（railway.new）"}
    settings = svc.get_settings()
    env = getattr(settings, "environments", None) if settings is not None else None
    if env is None:
        return None
    if not bool(getattr(env, "railway", True)):
        return {**base, "state": "off", "text": "已关闭"}
    daily_max = max(1, int(getattr(env, "railway_daily_max", 2) or 2))
    used = 0
    last_fail: dict | None = None
    last_acquire_ts = 0.0
    store = getattr(svc, "store", None)
    if store is not None:
        try:
            rec = store.kv_get(f"railway.day.{clock.day_key(clock.now())}", None)
            if isinstance(rec, dict):
                acquires = rec.get("acquires")
                if isinstance(acquires, list):
                    used = len([x for x in acquires if isinstance(x, (int, float))])
                else:
                    used = int(rec.get("count", 0) or 0)
        except Exception:
            used = 0
        try:
            lf = store.kv_get("railway.last_fail", None)
            if isinstance(lf, dict):
                last_fail = lf
        except Exception:
            last_fail = None
        try:
            la = store.kv_get("railway.last_acquire", None)
            if isinstance(la, dict):
                last_acquire_ts = float(la.get("ts", 0) or 0)
        except Exception:
            last_acquire_ts = 0.0
    if last_fail is not None:
        try:
            fail_ts = float(last_fail.get("ts", 0) or 0)
        except (TypeError, ValueError):
            fail_ts = 0.0
        if fail_ts > last_acquire_ts and clock.now() - fail_ts < 86400.0:
            reason = str(last_fail.get("reason") or "").strip() or "原因不明"
            return {**base, "state": "warn", "text": f"最近一次没拿到机器：{reason}"}
    return {**base, "state": "ok", "text": f"可用 · 今天用了 {used}/{daily_max} 台"}


def _bot_info(svc: Any) -> dict[str, str]:
    """bot 名字从 host 的预热缓存拿，拿不到用 MaiBot。

    avatar 统一指到 /api/avatar/bot?v=<版本>（console/avatar.py）：自定义上传 /
    换网址 / 删除、平台头像缓存更新时版本号变，浏览器不会一直用旧缓存。
    头像服务没就位（老数据目录 / 没启动）时回落默认图。"""
    name = ""
    try:
        name = str(getattr(svc.host, "bot_name_cache", "") or "")
    except Exception:
        name = ""
    avatar = "/static/assets/logo.png"
    try:
        av = getattr(svc, "avatar", None)
        if av is not None:
            avatar = av.bot_avatar_url()
    except Exception:
        avatar = "/static/assets/logo.png"
    return {"name": name or "MaiBot", "avatar": avatar}


def settings_view(svc: Any) -> dict[str, Any]:
    settings = svc.get_settings()
    now = clock.now()
    models = svc.models.settings()
    model_state = "ok" if models.ready() else "off"
    model_text = "配好了，能用" if models.ready() else "还没配好，MaiWork 暂时不会工作"
    if models.ready() and _recent_model_errors(svc.store, now):
        model_state = "warn"
        model_text = "最近 1 小时有调用出错"
    usage_today = svc.models.usage_today()
    jev_today = 0
    try:
        if getattr(svc, "jev", None) is not None:
            jev_today = int(svc.jev.calls_today())
    except Exception:
        jev_today = 0
    usage_today["jev"] = jev_today
    public_url = (settings.console.public_url or "").rstrip("/") if settings is not None else ""
    groups = []
    for gid in _served_group_ids(svc):
        row = _group_row(svc, gid)
        token = str(row.get("token") or "")
        path = f"/#/{token}/news"
        groups.append(
            {
                "id": gid,
                # 群名统一清洗（names.py）：settings.groups 同样用洗过的
                "name": clean_group_name(row.get("name") or "", gid),
                "icon": icon_for_group(gid),
                "avatar": _group_avatar_path(svc, gid),
                "token": token,
                "link": public_url + path if public_url else path,
            }
        )
    feeds_config: tuple[str, ...] = ()
    if settings is not None:
        feeds_config = tuple(getattr(settings.feeds, "blocked_domains", ()) or ())
        rules = {
            "quiet_hours": settings.delivery.quiet_hours,
            "topics_per_day": settings.topics.per_day,
            "topics_min_gap_hours": settings.topics.min_gap_hours,
            "push_per_day": settings.delivery.push_per_day,
            "approval_required": settings.approval.required,
            "admins": len(settings.approval.admins),
        }
        alert = int(settings.usage.alert_daily_tokens or 0)
        problems = list(settings.problems)
    else:
        rules = {
            "quiet_hours": "23:00-08:00",
            "topics_per_day": 2,
            "topics_min_gap_hours": 3,
            "push_per_day": 3,
            "approval_required": True,
            "admins": 0,
        }
        alert = 0
        problems = []
    # feeds 段（2026-09-27 质量标准）：生效屏蔽名单（网页改过一次就以网页为准）+ 自动屏蔽的
    try:
        from ..feeds import blocked_domains_effective

        merged_blocked = blocked_domains_effective(svc.store, feeds_config)
    except Exception:
        merged_blocked = sorted(feeds_config)
    auto_blocked_all: set[str] = set()
    try:
        feeds_mod = getattr(svc, "feeds", None)
        if feeds_mod is not None:
            for gid in _served_group_ids(svc):
                auto_blocked_all.update(feeds_mod._auto_blocked_domains(gid) or [])
    except Exception:
        auto_blocked_all = set()
    # 健康项：railway 那条老 stub 里没有 environments 节时返回 None，不放进列表
    health = [
        {"key": "models", "icon": "robot", "name": "模型端点", "state": model_state, "text": model_text},
        _jev_health(svc),
        _search_health(svc),
        _reader_health(svc),
        _localenv_health(svc),
        _groupspace_health(svc),
    ]
    ssh_health = _ssh_health(svc)
    if ssh_health is not None:
        health.append(ssh_health)
    railway_health = _railway_health(svc)
    if railway_health is not None:
        health.append(railway_health)
    # 用量提醒（docs/02 §7.2）：超阈值只在网页标出，不往群里发；只列今天产生的
    try:
        from ..usage_alerts import today_alerts

        usage_alerts_today = today_alerts(svc.store, now)
    except Exception:
        usage_alerts_today = []
    return {
        "models": models.public(),
        "usage": {
            "today": usage_today,
            "alert_daily_tokens": alert,
            "alerts": usage_alerts_today,
        },
        "health": health,
        "rules": rules,
        "groups": groups,
        "feeds": {
            "blocked_domains": merged_blocked,
            "auto_blocked": sorted(auto_blocked_all),
            # 每群 RSS 源列表（settings 视图共用；结构 = rss.list_feeds 的单条）
            "rss": _rss_by_group(svc),
        },
        "extensions": _extensions_view(svc),
        "problems": problems,
    }


def _extensions_view(svc: Any) -> dict[str, Any]:
    """settings 的扩展段（docs/02 §10）：mcp 各条只说名字/host/开关/健康/工具数/错误——
    headers（密钥）绝不回显，url 只到 host（extensions.info 已裁掉路径和 query）。
    skills 给名字和一句描述。manage=true：扩展可以在网页上管理（/api/extensions 系列接口，
    网页加的东西存数据库，不写回 config.toml）。"""
    out: dict[str, Any] = {"mcp": [], "skills": [], "manage": True}
    try:
        ext = getattr(svc, "extensions", None)
        if ext is not None:
            info = ext.info()
            if isinstance(info, list):
                out["mcp"] = [
                    {
                        "name": str(m.get("name") or ""),
                        "url": str(m.get("url") or ""),
                        "enabled": bool(m.get("enabled")),
                        "ok": bool(m.get("ok")),
                        "tools": int(m.get("tools") or 0),
                        "error": str(m.get("error") or ""),
                    }
                    for m in info
                    if isinstance(m, dict)
                ]
    except Exception:
        logger.exception("拼 MCP 扩展信息出错")
    try:
        skills = getattr(svc, "skills", None)
        if skills is not None:
            out["skills"] = [
                {"name": str(s.get("name") or ""), "description": str(s.get("description") or "")}
                for s in skills.list()
            ]
    except Exception:
        logger.exception("拼 skill 清单出错")
    return out


# ----------------------------------------------------------------------
# token 查询
# ----------------------------------------------------------------------


def group_id_by_token(svc: Any, token: str) -> str | None:
    """群链接码 → 群号；只对仍在配置里的服务群生效。"""
    token = str(token or "").strip()
    if not token:
        return None
    settings = svc.get_settings()
    try:
        row = svc.store.read().execute("SELECT group_id FROM groups WHERE token = ?", (token,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    gid = str(row["group_id"])
    if settings is None or not settings.is_served(gid):
        return None
    return gid


def token_of(svc: Any, group_id: str) -> str:
    return str(_group_row(svc, group_id).get("token") or "")
