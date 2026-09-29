"""card_push.py：MaiWork 自己往群里发的两种「主动小推送」（每群开关，默认都关）。

1. 资讯卡片（CardPush）：一批群资讯出来后，挑分数最高的 1~3 条（每群可配）画成一张卡片图
   （news_card.render_png）发进群，附本群 MaiWork 网页链接。
2. 构想提一嘴（IdeaMention，见下半部分）：出了新构想，用 MaiWork 的口吻说一两句 + 链接。

共同的节制（2026-09-29 与用户定）：
- 每群开关，默认关；配置存 kv["cardpush.<群号>"]（不进 config.toml）。开开关那一刻之前的
  批次 / 构想不补发。
- 睡觉时段（delivery.quiet_hours）不发，醒来再发；超过 12 小时还没发出去就作废（dropped）。
- 各自的每日上限（按北京时间自然日数「已发」），不占开场白的 push_per_day；超额的直接作废，
  不拖到第二天。
- 同一条资讯永远只进一次卡片；同一个构想只提一次。发过的东西 MaiBot 照样可以再提
  （MaiBot 可提起清单不受影响；发完另记一条备忘）。
- 只发服务群：建行和投递两处都挡。
- 发送失败重试 1 次（隔 5 分钟）；超时算「不确定」，不重发（可能已经发出去了）；
  插件重启时 sending → uncertain。

后台循环每轮对每个服务群调 scan(gid, now) 建待发行、flush(gid, now) 投递。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from . import clock, members
from .store import Store

logger = logging.getLogger("maiwork.card_push")

KV_PREFIX = "cardpush."
DEFAULTS: dict[str, Any] = {
    "news_card_enabled": False,
    "news_card_count": 3,
    "news_card_daily_max": 3,
    "idea_mention_enabled": False,
    "idea_mention_daily_max": 2,
}
_RANGES = {
    "news_card_count": (1, 3),
    "news_card_daily_max": (1, 24),
    "idea_mention_daily_max": (1, 24),
}
_BOOLS = ("news_card_enabled", "idea_mention_enabled")
# 开关打开的时刻：只管这之后出来的批次 / 构想
_SINCE = {"news_card_enabled": "news_card_since", "idea_mention_enabled": "idea_mention_since"}

EXPIRE_S = 12 * 3600.0
RETRY_DELAY_S = 300.0
MAX_ATTEMPTS = 2  # 首发 + 重试 1 次
_MEMO_TTL_S = 6 * 3600
_ERR_MAX = 300
_CARD_KINDS = ("news", "guide")


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def get_config(store: Store, group_id: Any) -> dict:
    raw = store.kv_get(f"{KV_PREFIX}{group_id}", {}) or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in list(DEFAULTS) + list(_SINCE.values()):
            if k in raw:
                out[k] = raw[k]
    for k in _BOOLS:
        out[k] = bool(out[k])
    for k, (lo, hi) in _RANGES.items():
        try:
            v = int(out[k])
        except (TypeError, ValueError):
            v = DEFAULTS[k]
        out[k] = min(hi, max(lo, v))
    for k in _SINCE.values():
        try:
            out[k] = float(out.get(k) or 0.0)
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


def set_config(store: Store, group_id: Any, patch: dict, *, now: Optional[float] = None) -> dict:
    """改一个群的推送配置；字段 / 范围不对抛 ValueError（中文）。返回改完的完整配置。"""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("要给要改的字段")
    now = clock.now() if now is None else float(now)
    cur = get_config(store, group_id)
    for k, v in patch.items():
        if k in _BOOLS:
            if not isinstance(v, bool):
                raise ValueError(f"{k} 要是 true / false")
            if v and not cur[k]:
                cur[_SINCE[k]] = now
            cur[k] = v
        elif k in _RANGES:
            lo, hi = _RANGES[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
                raise ValueError(f"{k} 要是整数")
            if not lo <= int(v) <= hi:
                raise ValueError(f"{k} 要在 {lo}~{hi} 之间")
            cur[k] = int(v)
        else:
            raise ValueError(f"不认识的字段：{k}")
    with store.tx() as conn:
        store.kv_set(conn, f"{KV_PREFIX}{group_id}", cur)
    return cur


def group_link(store: Store, settings: Any, group_id: Any, *, tab: str = "news", item: str = "") -> str:
    """本群 MaiWork 网页链接（console.public_url + 群链接码）；没公开 / 没链接码 → ""。"""
    public_url = str(getattr(getattr(settings, "console", None), "public_url", "") or "").rstrip("/")
    if not public_url:
        return ""
    try:
        row = store.read().execute("SELECT token FROM groups WHERE group_id=?", (str(group_id),)).fetchone()
    except Exception:
        logger.exception("读群链接码失败（群 %s）", group_id)
        return ""
    token = str(row["token"] or "") if row is not None else ""
    if not token:
        return ""
    link = f"{public_url}/#/{token}/{tab}"
    return f"{link}/{item}" if item else link


def web_view(svc: Any, group_id: Any) -> dict:
    """网页「往群里发」那块要的数据：两个开关的配置 + 今天发了几次 + 最近几条记录。"""
    gid = str(group_id)
    cp = getattr(svc, "card_push", None)
    view = cp.status(gid) if cp is not None else {
        "config": get_config(svc.store, gid), "sent_today": 0, "recent": []}
    im = getattr(svc, "idea_mention", None)
    view["mention"] = im.status(gid) if im is not None else {"sent_today": 0, "recent": []}
    view["has_link"] = bool(group_link(svc.store, svc.get_settings(), gid))
    return view


def _served(get_settings: Callable[[], Any], gid: str) -> bool:
    try:
        return bool(get_settings().is_served(gid))
    except Exception:
        return False


def _is_timeout(exc: BaseException) -> bool:
    return "超时" in str(exc) or exc.__class__.__name__ == "TimeoutError"


def _slot_label(ts: float) -> str:
    from .news_card import slot_word

    t = clock.bj(float(ts))
    return f"{t.month}月{t.day}日 · {slot_word(ts, '')}"


def _day_bounds(ts: float) -> tuple[float, float]:
    t = clock.bj(float(ts))
    start = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start, start + 86400.0


# ----------------------------------------------------------------------
# 资讯卡片
# ----------------------------------------------------------------------


Renderer = Callable[[dict], Awaitable[bytes]]


async def _default_renderer(data: dict) -> bytes:
    from . import news_card

    return await news_card.render_png(data)


class CardPush:
    def __init__(
        self,
        store: Store,
        host: Any,
        pushes: Any,
        mentions: Any,
        get_settings: Callable[[], Any],
        *,
        renderer: Optional[Renderer] = None,
    ) -> None:
        self._store = store
        self._host = host
        self._pushes = pushes
        self._mentions = mentions
        self._get_settings = get_settings
        self._render = renderer or _default_renderer

    # -------------------------------------------------------------- 建待发行

    def _carded_item_ids(self, gid: str) -> set[int]:
        """这个群已经进过卡片（已发 / 发送中 / 不确定）的条目 id。"""
        rows = self._store.read().execute(
            "SELECT item_ids FROM news_cards WHERE group_id=? AND status IN ('sent','sending','uncertain')",
            (gid,),
        ).fetchall()
        out: set[int] = set()
        for r in rows:
            try:
                out.update(int(x) for x in json.loads(r["item_ids"] or "[]"))
            except (TypeError, ValueError):
                continue
        return out

    def _eligible_items(self, gid: str, batch_id: int, exclude: set[int]) -> list[Any]:
        rows = self._store.read().execute(
            "SELECT * FROM news_items WHERE group_id=? AND batch_id=? AND rejected=0"
            " AND kind IN ('news','guide') AND COALESCE(target_user_id,'')=''"
            " ORDER BY score DESC, id ASC",
            (gid, int(batch_id)),
        ).fetchall()
        return [r for r in rows if int(r["id"]) not in exclude]

    def scan(self, group_id: Any, now: float) -> int:
        """把开关打开之后、12 小时内出来的群资讯批次各建一行待发（每批至多一行）。返回新建的待发行数。"""
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return 0
        cfg = get_config(self._store, gid)
        if not cfg["news_card_enabled"]:
            return 0
        since = max(float(cfg["news_card_since"]), float(now) - EXPIRE_S)
        batches = self._store.read().execute(
            "SELECT b.id, b.created FROM news_batches b"
            " WHERE b.group_id=? AND b.created>=? AND b.kept>0 AND b.note NOT LIKE 'personal:%'"
            " AND NOT EXISTS (SELECT 1 FROM news_cards c WHERE c.batch_id=b.id)"
            " ORDER BY b.id",
            (gid, since),
        ).fetchall()
        made = 0
        for b in batches:
            picked = self._eligible_items(gid, int(b["id"]), self._carded_item_ids(gid))
            ids = [int(r["id"]) for r in picked[: cfg["news_card_count"]]]
            status, error = ("pending", "") if ids else ("dropped", "这批没有能上卡片的群资讯")
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (gid, int(b["id"]), status, json.dumps(ids), float(b["created"]), float(b["created"]), error),
                )
            if ids:
                made += 1
        return made

    # -------------------------------------------------------------- 投递

    def has_due(self, group_id: Any, now: float) -> bool:
        row = self._store.read().execute(
            "SELECT 1 FROM news_cards WHERE group_id=? AND status='pending' AND due_ts<=? LIMIT 1",
            (str(group_id), float(now)),
        ).fetchone()
        return row is not None

    def _set(self, cid: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._store.tx() as conn:
            conn.execute(f"UPDATE news_cards SET {cols} WHERE id=?", (*fields.values(), int(cid)))

    def sent_today(self, gid: str, now: float) -> int:
        start, end = _day_bounds(now)
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM news_cards WHERE group_id=? AND status IN ('sent','uncertain')"
            " AND sent_ts>=? AND sent_ts<?",
            (gid, start, end),
        ).fetchone()
        return int(row["c"]) if row else 0

    def _session(self, gid: str) -> str:
        row = self._store.read().execute(
            "SELECT session_id FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        return str(row["session_id"] or "") if row is not None else ""

    def _card_data(self, gid: str, rows: list[Any], batch_id: int, link: str) -> dict:
        batch = self._store.read().execute(
            "SELECT slot_ts, kept FROM news_batches WHERE id=?", (int(batch_id),)
        ).fetchone()
        grow = self._store.read().execute("SELECT name FROM groups WHERE group_id=?", (gid,)).fetchone()
        slot_ts = float(batch["slot_ts"]) if batch is not None else clock.now()
        items = []
        for r in rows:
            try:
                sources = json.loads(r["sources"] or "[]")
            except (TypeError, ValueError):
                sources = []
            src = sources[0] if sources and isinstance(sources[0], dict) else {}
            try:
                kws = json.loads(r["keywords"] or "[]")
            except (TypeError, ValueError):
                kws = []
            items.append({
                "title": members.render(self._store, gid, r["title"]),
                "summary": members.render(self._store, gid, r["summary"]),
                "why": members.render(self._store, gid, r["why"]),
                "site": str(src.get("site") or ""),
                "url": str(src.get("url") or ""),
                "published_ts": r["published_ts"],
                "icon": str(r["icon"] or "newspaper"),
                "keywords": [str(k) for k in kws if str(k).strip()] if isinstance(kws, list) else [],
                "topic": str(r["topic"] or ""),
                "kind": str(r["kind"] or "news"),
                "image_url": str(r["image_url"] or ""),
            })
        return {
            "group_name": str(grow["name"] or "") if grow is not None else "",
            "slot_label": _slot_label(slot_ts),
            "slot_ts": slot_ts,
            "link": link,
            "total": int(batch["kept"]) if batch is not None else len(items),
            "items": items,
        }

    @staticmethod
    def _text_fallback(data: dict) -> str:
        lines = [f"【资讯精选 · {data['slot_label']}】"]
        for i, it in enumerate(data["items"], 1):
            site = f" — {it['site']}" if it.get("site") else ""
            lines.append(f"{i}. {it['title']}{site}")
        if data.get("link"):
            lines.append(f"看全部资讯：{data['link']}")
        return "\n".join(lines)

    async def flush(self, group_id: Any, now: Optional[float] = None) -> None:
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return
        now = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM news_cards WHERE group_id=? AND status='pending' AND due_ts<=? ORDER BY id",
            (gid, now),
        ).fetchall()
        if not rows:
            return
        cfg = get_config(self._store, gid)
        for row in rows:
            cid = int(row["id"])
            if not cfg["news_card_enabled"]:
                self._set(cid, status="dropped", error="开关已关")
                continue
            if now - float(row["created"]) > EXPIRE_S:
                self._set(cid, status="dropped", error="超过 12 小时没发出去，作废")
                logger.info("资讯卡片作废（群 %s 批次 %s）：超过 12 小时", gid, row["batch_id"])
                continue
            if self._pushes.in_quiet(now):
                return  # 睡觉时段：原样留着，醒来那轮再发
            if self.sent_today(gid, now) >= cfg["news_card_daily_max"]:
                self._set(cid, status="dropped", error="今天的卡片发够了")
                logger.info("资讯卡片作废（群 %s 批次 %s）：今天发够了", gid, row["batch_id"])
                continue
            try:
                ids = [int(x) for x in json.loads(row["item_ids"] or "[]")]
            except (TypeError, ValueError):
                ids = []
            done = self._carded_item_ids(gid)
            live: list[Any] = []
            for i in ids:
                if i in done:
                    continue
                r = self._store.read().execute(
                    "SELECT * FROM news_items WHERE id=? AND group_id=? AND rejected=0"
                    " AND kind IN ('news','guide') AND COALESCE(target_user_id,'')=''",
                    (i, gid),
                ).fetchone()
                if r is not None:
                    live.append(r)
            if not live:
                self._set(cid, status="dropped", error="要发的条目都发过或被撤下了")
                continue
            session_id = self._session(gid)
            if not session_id:
                self._set(cid, status="failed", error="还不知道这个群的会话，发不了")
                continue
            await self._send_one(gid, row, live, session_id, now)

    async def _send_one(self, gid: str, row: Any, live: list[Any], session_id: str, now: float) -> None:
        cid = int(row["id"])
        link = group_link(self._store, self._get_settings(), gid)
        data = self._card_data(gid, live, int(row["batch_id"]), link)
        ids = [int(r["id"]) for r in live]
        png: bytes = b""
        try:
            png = await self._render(data)
        except Exception as e:  # 渲染失败（没浏览器 / 超时 / 意外）→ 纯文字列表兜底
            logger.warning("资讯卡片渲染失败，改发文字（群 %s）：%s", gid, e)
        attempts = int(row["attempts"] or 0) + 1
        self._set(cid, status="sending", attempts=attempts, item_ids=json.dumps(ids))
        try:
            if png:
                res = await self._host.send_image(session_id, png, text=f"看全部资讯：{link}" if link else "")
                mode = "image"
            else:
                res = await self._host.send_text(session_id, self._text_fallback(data))
                mode = "text"
        except Exception as e:
            err = str(e)[:_ERR_MAX]
            if _is_timeout(e):
                self._set(cid, status="uncertain", sent_ts=now, error=f"发送超时，可能已发出，不重发：{err}")
            elif attempts < MAX_ATTEMPTS:
                self._set(cid, status="pending", due_ts=now + RETRY_DELAY_S, error=f"发送失败，稍后重试一次：{err}")
            else:
                self._set(cid, status="failed", error=f"发送失败：{err}")
            logger.warning("资讯卡片发送失败（群 %s）：%s", gid, err)
            return
        self._set(cid, status="sent", sent_ts=now, mode=mode, error="",
                  message_id=str(getattr(res, "message_id", "") or ""))
        titles = "；".join(it["title"] for it in data["items"])
        try:
            self._pushes.record(gid, "news_card", titles, now)
        except Exception:
            logger.exception("资讯卡片留痕失败（群 %s）", gid)
        try:
            self._mentions.add(
                gid,
                f"MaiWork 刚在群里发了资讯卡片：{titles[:120]}。有人问起可以接着聊，"
                + (f"全部资讯在 {link}" if link else "全部资讯在 MaiWork 网页里"),
                key=f"news_card:{cid}",
                ttl_s=_MEMO_TTL_S,
            )
        except Exception:
            logger.exception("资讯卡片备忘失败（群 %s）", gid)

    # -------------------------------------------------------------- 其他

    def recover(self) -> int:
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE news_cards SET status='uncertain', error='插件重启时发送中断，标为不确定，不自动重发'"
                " WHERE status='sending'"
            )
            return int(cur.rowcount or 0)

    def status(self, group_id: Any, *, now: Optional[float] = None) -> dict:
        gid = str(group_id)
        now = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT id, status, item_ids, created, sent_ts, mode, error FROM news_cards"
            " WHERE group_id=? ORDER BY id DESC LIMIT 5",
            (gid,),
        ).fetchall()
        recent = []
        for r in rows:
            try:
                n = len(json.loads(r["item_ids"] or "[]"))
            except (TypeError, ValueError):
                n = 0
            recent.append({
                "id": int(r["id"]), "status": str(r["status"]), "count": n,
                "created": float(r["created"]), "sent_ts": r["sent_ts"],
                "mode": str(r["mode"] or ""), "error": str(r["error"] or ""),
            })
        return {"config": get_config(self._store, gid), "sent_today": self.sent_today(gid, now), "recent": recent}


# ----------------------------------------------------------------------
# 构想提一嘴
# ----------------------------------------------------------------------

# 话里不许出现的（会露出「我在分析你」或画像内容）：命中就换安全模板
_LEAK_WORDS = (
    "画像", "注意到", "根据你", "观察", "了解到", "记得你", "平时", "经常", "一直在", "总是",
    "你最近", "看你", "听说你", "据说",
)
_MENTION_MAX = 90
_IDEA_LIVE_STATES = ("new", "wanted")


def _leaky(text: str, at_user: str) -> bool:
    t = str(text or "")
    if any(w in t for w in _LEAK_WORDS):
        return True
    if at_user and at_user in t:
        return True  # 绝不把 QQ 号写进话里
    return "{@" in t or "@" in t


def _template(title: str, personal: bool, at_user: str) -> str:
    title = str(title or "").strip().rstrip("。.")
    if title and not _leaky(title, at_user):
        head = "想到一个可能适合你的点子" if personal else "刚想到一个点子"
        return f"{head}——{title}。感兴趣的话点进去看看"
    return "想到一个可能适合你的点子，点进去看看" if personal else "刚想到一个点子，点进去看看"


class IdeaMention:
    """出了新构想（群构想 / 个人向构想）→ 用 MaiWork 的口吻说一两句 + 构想链接。

    个人向构想（ideas.target_user_id 非空）会 @ 本人。给模型的材料只有标题 + 正文 + 可行性，
    **不给 basis**（「为什么适合」那句会引画像）；写出来的话再过一遍 _leaky，命中就用模板。
    flush 里要调模型（可能几十秒），app 把它当后台长活跑，不卡主循环。
    """

    def __init__(
        self,
        store: Store,
        host: Any,
        models: Any,
        pushes: Any,
        mentions: Any,
        get_settings: Callable[[], Any],
    ) -> None:
        self._store = store
        self._host = host
        self._models = models
        self._pushes = pushes
        self._mentions = mentions
        self._get_settings = get_settings

    def scan(self, group_id: Any, now: float) -> int:
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return 0
        cfg = get_config(self._store, gid)
        if not cfg["idea_mention_enabled"]:
            return 0
        since = max(float(cfg["idea_mention_since"]), float(now) - EXPIRE_S)
        rows = self._store.read().execute(
            "SELECT i.id, i.created, COALESCE(i.target_user_id,'') AS uid FROM ideas i"
            " WHERE i.group_id=? AND i.created>=?"
            " AND NOT EXISTS (SELECT 1 FROM idea_mentions m WHERE m.idea_id=i.id)"
            " ORDER BY i.id",
            (gid, since),
        ).fetchall()
        for r in rows:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO idea_mentions (group_id, idea_id, status, at_user, created, due_ts)"
                    " VALUES (?, ?, 'pending', ?, ?, ?)",
                    (gid, int(r["id"]), str(r["uid"]), float(r["created"]), float(r["created"])),
                )
        return len(rows)

    def has_due(self, group_id: Any, now: float) -> bool:
        row = self._store.read().execute(
            "SELECT 1 FROM idea_mentions WHERE group_id=? AND status='pending' AND due_ts<=? LIMIT 1",
            (str(group_id), float(now)),
        ).fetchone()
        return row is not None

    def _set(self, mid: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._store.tx() as conn:
            conn.execute(f"UPDATE idea_mentions SET {cols} WHERE id=?", (*fields.values(), int(mid)))

    def sent_today(self, gid: str, now: float) -> int:
        start, end = _day_bounds(now)
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM idea_mentions WHERE group_id=? AND status IN ('sent','uncertain')"
            " AND sent_ts>=? AND sent_ts<?",
            (gid, start, end),
        ).fetchone()
        return int(row["c"]) if row else 0

    async def _write(self, gid: str, idea: Any, personal: bool, at_user: str) -> str:
        title = str(idea["title"] or "").strip()
        body = members.render(self._store, gid, idea["body"] or "")
        feas = ""
        try:
            f = json.loads(idea["feasibility"] or "{}") if "feasibility" in idea.keys() else {}
            feas = str(f.get("note") or "") if isinstance(f, dict) else ""
        except (TypeError, ValueError):
            feas = ""
        rules = [
            "你是 MaiWork，这个 QQ 群里帮大家找资讯、想点子、干活的助手。你刚想到一个构想，"
            "要在群里顺口提一句，让感兴趣的人点链接去看（链接由程序附在后面，你不用写）。",
            "",
            f"构想标题：{title}",
            f"构想内容：{body[:300]}",
        ]
        if feas:
            rules.append(f"能不能做：{feas[:120]}")
        rules += [
            "",
            "要求：",
            "- 一到两句，总共不超过 60 个字，口语、自然、不做作，不用表情符号堆砌。",
            "- 说清楚这是个什么点子、为什么可能有意思；别复述标题全文。",
        ]
        if personal:
            rules.append(
                "- 这个点子是想给某一位群友的（程序会在前面 @ 他）。直接对他说「你」就行，"
                "但绝对不能说你怎么知道他的情况：不许出现「根据你的画像」「我注意到你」「看你平时」"
                "「你最近在…」这类话，不许提到他的任何个人情况、经历、习惯。"
            )
        else:
            rules.append("- 是给全群的，不要点名任何人。")
        rules += [
            "- 不许出现「画像」这个词，不许写任何 QQ 号、@、链接。",
            '只回 JSON：{"text": "你要说的话"}',
        ]
        try:
            res = await self._models.chat(
                "main",
                [{"role": "user", "content": "\n".join(rules)}],
                json_mode=True,
                purpose="card_push.idea_mention",
                group_id=gid,
            )
            data = json.loads(str(getattr(res, "text", "") or ""))
            text = str(data.get("text") or "").strip() if isinstance(data, dict) else ""
        except Exception as e:  # 模型出错 / JSON 坏了 → 模板
            logger.info("构想提一嘴写话失败，用模板（群 %s）：%s", gid, e)
            text = ""
        if not text or len(text) > _MENTION_MAX or _leaky(text, at_user):
            if text:
                logger.info("构想提一嘴的话不合规，换模板（群 %s）", gid)
            text = _template(title, personal, at_user)
        return text

    async def flush(self, group_id: Any, now: Optional[float] = None) -> None:
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return
        now = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM idea_mentions WHERE group_id=? AND status='pending' AND due_ts<=? ORDER BY id",
            (gid, now),
        ).fetchall()
        if not rows:
            return
        cfg = get_config(self._store, gid)
        for row in rows:
            mid = int(row["id"])
            if not cfg["idea_mention_enabled"]:
                self._set(mid, status="dropped", error="开关已关")
                continue
            if now - float(row["created"]) > EXPIRE_S:
                self._set(mid, status="dropped", error="超过 12 小时没发出去，作废")
                continue
            if self._pushes.in_quiet(now):
                return
            idea = self._store.read().execute(
                "SELECT * FROM ideas WHERE id=? AND group_id=?", (int(row["idea_id"]), gid)
            ).fetchone()
            if idea is None or str(idea["state"] or "") not in _IDEA_LIVE_STATES:
                self._set(mid, status="dropped", error="构想已经不在了或被划掉了")
                continue
            if self.sent_today(gid, now) >= cfg["idea_mention_daily_max"]:
                self._set(mid, status="dropped", error="今天提够了")
                continue
            srow = self._store.read().execute(
                "SELECT session_id FROM groups WHERE group_id=?", (gid,)
            ).fetchone()
            session_id = str(srow["session_id"] or "") if srow is not None else ""
            if not session_id:
                self._set(mid, status="failed", error="还不知道这个群的会话，发不了")
                continue
            at_user = str(row["at_user"] or "")
            text = await self._write(gid, idea, bool(at_user), at_user)
            link = group_link(self._store, self._get_settings(), gid, tab="ideas", item=f"I-{int(idea['id'])}")
            full = f"{text}\n{link}" if link else text
            attempts = int(row["attempts"] or 0) + 1
            self._set(mid, status="sending", attempts=attempts, text=full)
            try:
                res = await self._host.send_text(session_id, full, at_user=at_user)
            except Exception as e:
                err = str(e)[:_ERR_MAX]
                if _is_timeout(e):
                    self._set(mid, status="uncertain", sent_ts=now, error=f"发送超时，可能已发出，不重发：{err}")
                elif attempts < MAX_ATTEMPTS:
                    self._set(mid, status="pending", due_ts=now + RETRY_DELAY_S, error=f"发送失败，稍后重试一次：{err}")
                else:
                    self._set(mid, status="failed", error=f"发送失败：{err}")
                logger.warning("构想提一嘴发送失败（群 %s）：%s", gid, err)
                continue
            self._set(mid, status="sent", sent_ts=now, error="",
                      message_id=str(getattr(res, "message_id", "") or ""))
            try:
                self._pushes.record(gid, "idea_mention", full, now)
            except Exception:
                logger.exception("构想提一嘴留痕失败（群 %s）", gid)
            try:
                who = "给一位群友的" if at_user else ""
                self._mentions.add(
                    gid,
                    f"MaiWork 刚在群里提了一个{who}构想：{str(idea['title'] or '')[:60]}。有人问起可以接着聊"
                    + (f"，详情在 {link}" if link else ""),
                    key=f"idea_mention:{mid}",
                    ttl_s=_MEMO_TTL_S,
                )
            except Exception:
                logger.exception("构想提一嘴备忘失败（群 %s）", gid)

    def recover(self) -> int:
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE idea_mentions SET status='uncertain', error='插件重启时发送中断，标为不确定，不自动重发'"
                " WHERE status='sending'"
            )
            return int(cur.rowcount or 0)

    def status(self, group_id: Any, *, now: Optional[float] = None) -> dict:
        gid = str(group_id)
        now = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT id, idea_id, status, created, sent_ts, at_user, error FROM idea_mentions"
            " WHERE group_id=? ORDER BY id DESC LIMIT 5",
            (gid,),
        ).fetchall()
        recent = [{
            "id": int(r["id"]), "idea_id": int(r["idea_id"]), "status": str(r["status"]),
            "created": float(r["created"]), "sent_ts": r["sent_ts"],
            "personal": bool(r["at_user"]), "error": str(r["error"] or ""),
        } for r in rows]
        return {"sent_today": self.sent_today(gid, now), "recent": recent}
