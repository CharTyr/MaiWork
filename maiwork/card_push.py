"""card_push.py：MaiWork 自己往群里发的两种「主动小推送」（每群开关，默认都关）。

1. 资讯卡片（CardPush）：一批群资讯出来后，挑分数最高的 1~3 条（每群可配）画成一张卡片图
   （news_card.render_png）发进群，附本群 MaiWork 网页链接。
2. 构想提一嘴（IdeaMention，见下半部分）：出了新构想，用 MaiWork 的口吻说一两句 + 链接。

共同的节制（2026-10-03 docs/18 第三步归一之后）：
- **设置只有一份**：group_push.py（kv["group_push.<群号>"]）。本模块的 get_config /
  set_config 是给老调用口（网页 PUT / GET）留的薄壳，数据还是那一份；「资讯卡片 / 提一嘴
  各自的每日上限」退役，三种自制消息共用一个 daily_max（在 group_push，由 Pushes 数）。
  每群开关默认关；开开关那一刻之前的批次 / 构想不补发（since 存在 group_push 里）。
- **发送只有发件箱一条路**：scan 建待发行；flush 只做「备料 + 入队」——卡片画好图**落盘
  缓存**（重试用同一个文件，不重画）、提一嘴按 SOUL 写好话，然后交给 Outbox。真正的发送、
  以及发送前再查一遍（服务群 / 开关 / 睡觉时段 / 额度）都在 Outbox.flush：待发期间关掉
  开关就作废，不发陈旧的卡片 / 话。
- 结果回写走发件箱的结果 hook（on_result）：sent 才写 sent / 备忘；超时写 uncertain
  （可能已发出，不重发）；失败 / 作废照抄。**只入队不算发过。**
- 超过 12 小时还没发出去就作废（dropped）；入队载荷带 `expires_ts`（= 批次 / 构想时间
  + 12 小时），发件箱在发送前按它作废——被睡觉时段 / 每日上限推到窗口之外也不会发陈旧内容。
  同一条资讯永远只进一次卡片；同一个构想只提一次。
  发过的东西 MaiBot 照样可以再提（发完另记一条 6 小时备忘）。
- 只发服务群：建行和入队两处都挡。
- 卡片图缓存（<workspace_root>/.web/.push/card-<id>.png）由 `prune_card_cache` 每天清一次
  （app._prune_round 调）：只删发件箱里已经是 sent / failed / dropped 且过了 7 天的自己那一份。
- 插件重启：recover() 跟着发件箱的真实状态对账（没入过队的回 pending，发出去的补记 sent，
  结果不明的照抄），绝不重复发。

后台循环每轮对每个服务群调 scan(gid, now) 建待发行、flush(gid, now) 备料入队；
真正发出去由发件箱那一轮 flush 做。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import clock, group_push, members, voice
from .store import Store

logger = logging.getLogger("maiwork.card_push")

# 设置只有一份（group_push.py）；这两个常量是老调用口用的旧键名
KV_PREFIX = group_push.LEGACY_KV_PREFIX
LEGACY_KV_PREFIX = group_push.LEGACY_KV_PREFIX
GROUP_PUSH_KV_PREFIX = group_push.KV_PREFIX

EXPIRE_S = 12 * 3600.0
# 卡片图落盘缓存的保留期：发完（或失败 / 作废）之后这么久可以删（app._prune_round 每天跑）
PRUNE_AGE_S = 7 * 86400.0
_MEMO_TTL_S = 6 * 3600
_ERR_MAX = 300
_CARD_KINDS = ("news", "guide")
# 卡片图落盘缓存的位置（相对 workspace_root）：发送失败重试用同一个文件，不重画
_IMAGE_DIR = (".web", ".push")


# ----------------------------------------------------------------------
# 配置（薄壳：数据只有一份，在 group_push）
# ----------------------------------------------------------------------


def get_config(store: Store, group_id: Any, settings: Any = None) -> dict:
    """这个群的推送设置（= group_push 那一份）；settings 给了就顺带查服务名单。"""
    return group_push.get_config(store, group_id, settings)


def set_config(store: Store, group_id: Any, patch: dict, *, now: Optional[float] = None,
               settings: Any = None) -> dict:
    """改设置（= group_push.set_config）；字段 / 范围不对抛 ValueError（中文）。"""
    return group_push.set_config(store, group_id, patch, settings, now=now)


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
    """网页「往群里发」那块要的数据（老形状 + 新的归一视图）。

    - `config` / `sent_today` / `quota_used`：来自 group_push 那一份真源（今天真发出几条、
      占掉多少额度）。
    - `recent`（卡片记录）/ `mention`（提一嘴记录）：本模块自己两张表的最近记录。
    - `group_push`：group_push.view 的原样输出（父会话换前端时直接读它，别再加第二处）。
    """
    gid = str(group_id)
    try:
        settings = svc.get_settings()
    except Exception:
        logger.debug("读配置失败（群 %s 的推送视图按保守默认）", gid, exc_info=True)
        settings = group_push.UNAVAILABLE
    if settings is None:
        settings = group_push.UNAVAILABLE
    gp = group_push.view(svc.store, gid, settings)
    cp = getattr(svc, "card_push", None)
    view = cp.status(gid) if cp is not None else {"config": gp["config"], "sent_today": 0, "recent": []}
    im = getattr(svc, "idea_mention", None)
    view["mention"] = im.status(gid) if im is not None else {"sent_today": 0, "recent": []}
    view["config"] = dict(gp["config"])       # 设置只有一份真源
    view["sent_today"] = gp["sent_today"]     # 今天真发出去几条（含开场白 / 提一嘴）
    view["quota_used"] = gp["quota_used"]
    view["daily_max"] = gp["daily_max"]
    view["has_link"] = bool(group_link(svc.store, settings, gid))
    view["group_push"] = gp
    return view


def _served(get_settings: Callable[[], Any], gid: str) -> bool:
    try:
        return bool(get_settings().is_served(gid))
    except Exception:
        return False


def _slot_label(ts: float) -> str:
    from .news_card import slot_word

    t = clock.bj(float(ts))
    return f"{t.month}月{t.day}日 · {slot_word(ts, '')}"


def _day_bounds(ts: float) -> tuple[float, float]:
    t = clock.bj(float(ts))
    start = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start, start + 86400.0


def _card_id_of_key(key: Any) -> Optional[int]:
    """`news_card:<正整数>` → 编号；别的形状一律 None（不猜、不碰）。"""
    text = str(key or "")
    prefix = "news_card:"
    if not text.startswith(prefix):
        return None
    digits = text[len(prefix):]
    if not digits.isdigit():
        return None
    return int(digits)


def prune_card_cache(store: Any, get_settings: Callable[[], Any], now: float, *,
                     age_s: float = PRUNE_AGE_S) -> int:
    """清掉早就落地的卡片图缓存；返回真删了几张。

    只碰**自己那一份**：路径必须正好是 `<workspace_root>/.web/.push/card-<id>.png`
    （解析后仍在该目录里、不是符号链接），且发件箱里有一条 `key=news_card:<id>`、
    `kind='image'`、`status ∈ sent/failed/dropped` 且 `updated <= now-7 天` 的记录。

    `pending` / `sending` / `uncertain` 绝不删（结果没定，图还得留着重发）；
    符号链接、解析到目录外的、名字 / 编号对不上的、没有对应记录的、artifact 或别处的
    文件一律不碰。单张出问题只记日志，不拖垮整轮。
    """
    moment = float(now)
    cutoff = moment - float(age_s)
    rows = store.read().execute(
        "SELECT key FROM outbox"
        " WHERE kind='image' AND status IN ('sent','failed','dropped')"
        " AND updated<=? AND key LIKE 'news_card:%'",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    try:
        settings = get_settings()
        root = Path(getattr(settings, "workspace_root", Path("data/workspaces")))
    except Exception:
        logger.debug("读配置失败，卡片图缓存这次不清理", exc_info=True)
        return 0
    try:
        root_resolved = root.resolve()
        push_dir = root.joinpath(*_IMAGE_DIR)
        if push_dir.is_symlink():
            logger.warning("卡片图目录是符号链接，不清理：%s", push_dir)
            return 0
        push_resolved = push_dir.resolve()
    except OSError:
        return 0
    if push_resolved != root_resolved and root_resolved not in push_resolved.parents:
        logger.warning("卡片图目录不在工作区根目录下，不清理：%s", push_dir)
        return 0
    removed = 0
    for row in rows:
        try:
            cid = _card_id_of_key(row["key"])
            if cid is None:
                continue
            target = push_resolved / f"card-{cid}.png"
            if target.is_symlink() or not target.exists():
                continue
            resolved = target.resolve()
            # 精确 basename + 就在这个目录里（resolve 后仍在 containment 内）
            if resolved.name != f"card-{cid}.png" or resolved.parent != push_resolved:
                continue
            if root_resolved not in resolved.parents:
                continue
            if not resolved.is_file():
                continue
            resolved.unlink()
            removed += 1
        except OSError as e:
            logger.warning("删卡片图缓存失败（key=%s）：%s", row["key"], e)
        except Exception:
            logger.exception("删卡片图缓存出错（key=%s），跳过这一张", row["key"])
    return removed


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
        outbox: Any = None,
    ) -> None:
        self._store = store
        self._host = host
        self._pushes = pushes
        self._mentions = mentions
        self._get_settings = get_settings
        self._render = renderer or _default_renderer
        self._outbox: Any = None
        if outbox is not None:
            self.attach_outbox(outbox)

    def attach_outbox(self, outbox: Any) -> None:
        """接上发件箱（app 建好 Outbox 之后调一次）：卡片只入队，发出去之后回写。"""
        self._outbox = outbox
        add_hook = getattr(outbox, "add_result_hook", None)
        if callable(add_hook):
            add_hook(self.on_result)
        else:
            logger.warning("发件箱没有 add_result_hook，卡片发出后回写不了")

    def _cfg(self, gid: str) -> dict:
        """这个群的「往群里发」设置（group_push 那一份）。

        失败关闭：读配置失败 / 拿不到 → 保守默认（开关全关、额度有限、默认睡觉时段），
        零读零写；绝不落进 legacy `settings=None` 放行口径去 seed 一份「默认开着」的配置。
        """
        try:
            settings = self._get_settings()
        except Exception:
            logger.debug("读配置失败（群 %s 的卡片设置按保守默认）", gid, exc_info=True)
            settings = None
        if settings is None:
            return group_push.conservative_defaults()
        return group_push.get_config(self._store, gid, settings)

    # -------------------------------------------------------------- 建待发行

    def _carded_item_ids(self, gid: str) -> set[int]:
        """这个群已经进过卡片（已发 / 排队待发 / 发送中 / 不确定）的条目 id。"""
        rows = self._store.read().execute(
            "SELECT item_ids FROM news_cards WHERE group_id=?"
            " AND status IN ('sent','queued','sending','uncertain')",
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
        cfg = self._cfg(gid)
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
                # 核对过的图解整页（没封面时画进卡片，免得连配图都没有；news_card 截成图）
                "viz_html": self._viz_html(gid, r["id"]),
            })
        return {
            "group_name": str(grow["name"] or "") if grow is not None else "",
            "slot_label": _slot_label(slot_ts),
            "slot_ts": slot_ts,
            "link": link,
            "total": int(batch["kept"]) if batch is not None else len(items),
            "items": items,
        }

    def _viz_html(self, gid: str, item_id: Any) -> str:
        try:
            from . import news_viz

            return news_viz.html_for(self._store, gid, int(item_id)) or ""
        except Exception:  # noqa: BLE001  图解是锦上添花，读不到就不画
            logger.debug("读图解失败（群 %s 条 %s）", gid, item_id, exc_info=True)
            return ""

    @staticmethod
    def _text_fallback(data: dict) -> str:
        lines = [f"【资讯精选 · {data['slot_label']}】"]
        for i, it in enumerate(data["items"], 1):
            site = f" — {it['site']}" if it.get("site") else ""
            lines.append(f"{i}. {it['title']}{site}")
        if data.get("link"):
            lines.append(f"看全部资讯：{data['link']}")
        return "\n".join(lines)

    def _image_path(self, cid: int) -> Path:
        """卡片图的落盘位置（workspace_root/.web/.push/card-<id>.png）。

        画一次就落盘：发送失败重试时发件箱用的是同一个文件，不重画（省一次起浏览器）。
        """
        settings = self._get_settings()
        root = Path(getattr(settings, "workspace_root", Path("data/workspaces")))
        d = root.joinpath(*_IMAGE_DIR)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"card-{int(cid)}.png"

    async def flush(self, group_id: Any, now: Optional[float] = None) -> None:
        """把到点的待发卡片「备料 + 入队」（真正发出去由发件箱那一轮做）。

        - 开关关了 / 过期了 / 条目都被撤下了 → 作废，不发陈旧的。
        - 画图失败退回纯文字列表。
        - 没接发件箱：什么都不发（也什么都不标），等 app 接线。
        """
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return
        moment = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM news_cards WHERE group_id=? AND status='pending' AND due_ts<=? ORDER BY id",
            (gid, moment),
        ).fetchall()
        if not rows:
            return
        if self._pushes.in_quiet(moment, gid):
            return  # 睡觉时段：原样留着，醒来那轮再备料（不白画一张图）
        cfg = self._cfg(gid)
        if not cfg["news_card_enabled"]:
            for row in rows:
                self._set(int(row["id"]), status="dropped", error="开关已关")
            return
        outbox = self._outbox
        if outbox is None:
            logger.warning("资讯卡片还没接到发件箱（app 未接线），这批卡片先不发（群 %s）", gid)
            return
        for row in rows:
            cid = int(row["id"])
            if moment - float(row["created"]) > EXPIRE_S:
                self._set(cid, status="dropped", error="超过 12 小时没发出去，作废")
                logger.info("资讯卡片作废（群 %s 批次 %s）：超过 12 小时", gid, row["batch_id"])
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
            link = group_link(self._store, self._get_settings(), gid)
            data = self._card_data(gid, live, int(row["batch_id"]), link)
            live_ids = [int(r["id"]) for r in live]
            png: bytes = b""
            try:
                png = await self._render(data)
            except Exception as e:  # 渲染失败（没浏览器 / 超时 / 意外）→ 纯文字列表兜底
                logger.warning("资讯卡片渲染失败，改发文字（群 %s）：%s", gid, e)
            payload: dict[str, Any] = {
                "push_kind": "news_card",
                # 自动卡片的期限沿用现有 12 小时窗口：被睡觉时段 / 每日上限推迟过期后，
                # 发件箱直接作废，绝不把陈旧批次发进群。
                "expires_ts": float(row["created"]) + EXPIRE_S,
            }
            kind = "text"
            mode = "text"
            if png:
                try:
                    path = self._image_path(cid)
                    path.write_bytes(bytes(png))
                    payload.update({"path": str(path), "text": f"看全部资讯：{link}" if link else ""})
                    kind, mode = "image", "image"
                except OSError as e:
                    logger.warning("资讯卡片图片落盘失败，改发文字（群 %s）：%s", gid, e)
                    png = b""
            if not png:
                payload["text"] = self._text_fallback(data)
            try:
                outbox.enqueue(f"news_card:{cid}", gid, kind, payload)
            except Exception as e:
                logger.exception("资讯卡片入队失败（群 %s 卡片 %s）", gid, cid)
                self._set(cid, status="failed", error=f"入队失败：{str(e)[:_ERR_MAX]}")
                continue
            # 只入队：sent / 备忘都等发件箱真发出去之后由 on_result 写
            self._set(cid, status="queued", item_ids=json.dumps(live_ids), mode=mode, due_ts=moment)

    def _settle(self, gid: str, cid: int, outcome: str, *, result: Optional[dict] = None,
                error: str = "", moment: Optional[float] = None) -> None:
        """把发件箱的结果写回 news_cards；只有 sent 才算真发出去。"""
        now = float(moment) if moment is not None else clock.now()
        if outcome == "sent":
            mode = "image" if str((result or {}).get("mode") or "") == "image" else ""
            row = self._store.read().execute("SELECT mode FROM news_cards WHERE id=?", (int(cid),)).fetchone()
            mode = mode or (str(row["mode"] or "") if row is not None else "")
            self._set(cid, status="sent", sent_ts=now, error="",
                      mode=mode, message_id=str((result or {}).get("message_id") or ""))
            self._memo(gid, cid)
        elif outcome == "uncertain":
            self._set(cid, status="uncertain", sent_ts=now,
                      error=f"发送超时，可能已发出，不重发：{error}"[:_ERR_MAX])
        elif outcome == "failed":
            self._set(cid, status="failed", error=(error or "发送失败")[:_ERR_MAX])
        elif outcome == "dropped":
            self._set(cid, status="dropped", error=(error or "作废")[:_ERR_MAX])

    def on_result(self, info: dict) -> None:
        """发件箱的结果回调（attach_outbox 时自动挂上）。"""
        key = str((info or {}).get("key") or "")
        if not key.startswith("news_card:"):
            return
        try:
            cid = int(key.split(":", 1)[1])
        except (TypeError, ValueError):
            return
        gid = str(info.get("group_id") or "")
        outcome = str(info.get("outcome") or "")
        if outcome == "retrying":
            return
        try:
            self._settle(gid, cid, outcome, result=info.get("result") or {},
                         error=str(info.get("error") or ""),
                         moment=info.get("ts"))
        except Exception:
            logger.exception("资讯卡片结果回写失败（卡片 %s，outcome=%s）", cid, outcome)

    def _memo(self, gid: str, cid: int) -> None:
        """发完给 MaiBot 加一条 6 小时备忘（有人问起能接上）。"""
        try:
            row = self._store.read().execute(
                "SELECT item_ids FROM news_cards WHERE id=?", (int(cid),)
            ).fetchone()
            ids: list[int] = []
            if row is not None:
                try:
                    ids = [int(x) for x in json.loads(row["item_ids"] or "[]")]
                except (TypeError, ValueError):
                    ids = []
            titles = ""
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                rows = self._store.read().execute(
                    f"SELECT title FROM news_items WHERE id IN ({placeholders})", ids
                ).fetchall()
                titles = "；".join(str(r["title"] or "") for r in rows)
            link = group_link(self._store, self._get_settings(), gid)
            self._mentions.add(
                gid,
                f"MaiWork 刚在群里发了资讯卡片：{titles[:120]}。有人问起可以接着聊，"
                + (f"全部资讯在 {link}" if link else "全部资讯在 MaiWork 网页里"),
                key=f"news_card:{int(cid)}",
                ttl_s=_MEMO_TTL_S,
            )
        except Exception:
            logger.exception("资讯卡片备忘失败（群 %s，卡片 %s）", gid, cid)

    def recover(self, outbox: Any = None) -> int:
        """插件重启之后和发件箱对账；返回改了几条。

        - 本地 queued、发件箱里**没有**对应行（还没入队就崩了）→ 回到 pending，可以重建。
        - 本地 sending（老版本留下的、发到一半崩了）→ 不确定，绝不重发。
        - 发件箱 sent → 补记 sent（含备忘），不重发。
        - 发件箱 uncertain / failed / dropped → 照抄状态。
        - 发件箱还 pending / sending → 保持 queued，等下一轮发。
        没接发件箱：老库里的 sending 一律标不确定，不重发（老行为）。
        """
        ob = outbox if outbox is not None else self._outbox
        rows = self._store.read().execute(
            "SELECT id, group_id, status FROM news_cards WHERE status IN ('queued','sending') ORDER BY id"
        ).fetchall()
        if not rows:
            return 0
        if ob is None:
            changed = 0
            with self._store.tx() as conn:
                cur = conn.execute(
                    "UPDATE news_cards SET status='uncertain',"
                    " error='插件重启时发送中断，标为不确定，不自动重发' WHERE status='sending'"
                )
                changed = int(cur.rowcount or 0)
            return changed
        changed = 0
        for r in rows:
            cid = int(r["id"])
            gid = str(r["group_id"])
            legacy_sending = str(r["status"] or "") == "sending"
            box = self._store.read().execute(
                "SELECT status, result, error FROM outbox WHERE key=?", (f"news_card:{cid}",)
            ).fetchone()
            if box is None:
                if legacy_sending:
                    self._set(cid, status="uncertain",
                              error="插件重启时发送中断，标为不确定，不自动重发")
                else:
                    self._set(cid, status="pending", error="")
                changed += 1
                continue
            status = str(box["status"] or "")
            if status in ("pending", "sending"):
                continue
            try:
                result = json.loads(box["result"] or "{}")
            except (TypeError, ValueError):
                result = {}
            self._settle(gid, cid, status, result=result if isinstance(result, dict) else {},
                         error=str(box["error"] or ""))
            changed += 1
        return changed

    # -------------------------------------------------------------- 其他

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
        return {"config": self._cfg(gid), "sent_today": self.sent_today(gid, now), "recent": recent}


# ----------------------------------------------------------------------
# 构想提一嘴
# ----------------------------------------------------------------------

# 话里不许出现的（会露出「我在分析你」或画像内容）：命中就换安全模板
_LEAK_WORDS = (
    "画像", "注意到", "根据你", "观察", "了解到", "记得你", "平时", "经常", "一直在", "总是",
    "你最近", "看你", "听说你", "据说",
)
# 推销腔（2026-10 用户定：构想提一嘴要关心式问法，不许推销）：命中就换模板
_PITCH_WORDS = (
    "我可以帮", "给大家带来", "推荐给大家", "安利", "感兴趣的话", "点进去看看",
)
_MENTION_MAX = 90
_IDEA_LIVE_STATES = ("new", "wanted")
# 没由头时从标题里剥掉的开头（标题是「我可以……」式的推销句，群里说的话不要这个头）
_PITCH_HEADS = ("帮你们", "帮大家", "帮你", "帮群里", "帮")
_IDEA_TITLE_HEAD = "我可以"
# 通用兜底问句（模板最后一道保险：不带任何具体内容，也就不可能泄漏）
_GENERIC_GROUP = "突然想到一件事，要不要我来弄？"
_GENERIC_PERSONAL = "突然想到一件事，要不要我帮你搭把手？"


def _leaky(text: str, at_user: str) -> bool:
    t = str(text or "")
    if any(w in t for w in _LEAK_WORDS):
        return True
    if at_user and at_user in t:
        return True  # 绝不把 QQ 号写进话里
    return "{@" in t or "@" in t


def _pitchy(text: str) -> bool:
    """推销腔：命中任一个就换模板（「我可以帮」「感兴趣的话」这类）。"""
    t = str(text or "")
    return any(w in t for w in _PITCH_WORDS)


def _idea_rest(title: str) -> str:
    """标题去掉「我可以（帮你们/帮大家/帮你/帮群里/帮）」后的内容；剥不出 → ""。"""
    t = str(title or "").strip().rstrip("。.")
    if not t.startswith(_IDEA_TITLE_HEAD):
        return ""
    rest = t[len(_IDEA_TITLE_HEAD):].lstrip("，,、:： ")
    for head in _PITCH_HEADS:
        if rest.startswith(head):
            rest = rest[len(head):]
            break
    return rest.strip("，,、:：。. ")


def _template(title: str, personal: bool, at_user: str, origin: str = "") -> str:
    """固定模板：关心式问法（一两句、以问句结尾，不推销、不露画像）。

    - 有由头（origin，已过隐私闸）→「话说之前大家聊的那个 X 后来怎么样了？要我帮忙吗？」
      （个人向：「话说你之前想弄的那个 X 怎么样了？要我搭把手吗？」）
    - 没由头 → 标题剥掉「我可以帮…」的头，拼成「突然想到，X 这事要不要我来弄？」
    - 由头/标题本身命中词表 → 用不带具体内容的通用问句。
    """
    o = str(origin or "").strip()
    text = ""
    if o and not _leaky(o, at_user):
        if personal:
            text = f"话说你之前想弄的那个{o}怎么样了？要我搭把手吗？"
        else:
            text = f"话说之前大家聊的那个{o}后来怎么样了？要我帮忙吗？"
    if not text:
        rest = _idea_rest(title)
        if rest and not _leaky(rest, at_user):
            text = f"突然想到，{rest}这事要不要我来弄？"
    if not text or _leaky(text, at_user):
        text = _GENERIC_PERSONAL if personal else _GENERIC_GROUP
    return text


class IdeaMention:
    """出了新构想（群构想 / 个人向构想）→ 按人设关心地问一句 + 构想链接。

    个人向构想（ideas.target_user_id 非空）会 @ 本人。给模型的材料只有标题 + 正文 + 可行性
    + 由头（origin），**不给 basis**（「为什么适合」那句会引画像）；写出来的话再过一遍
    _leaky / _pitchy，命中就用模板。flush 里要调模型（可能几十秒），app 把它当后台长活跑，
    不卡主循环。
    """

    def __init__(
        self,
        store: Store,
        host: Any,
        models: Any,
        pushes: Any,
        mentions: Any,
        get_settings: Callable[[], Any],
        *,
        identity: Any = None,  # identity.py（读 SOUL）；None 就是没人设（不回退读 MaiBot 人格）
        outbox: Any = None,
    ) -> None:
        self._store = store
        self._host = host
        self._models = models
        self._pushes = pushes
        self._mentions = mentions
        self._get_settings = get_settings
        self._identity = identity
        # 每群三份统一注入（docs/17 §八.2）：app._wire_specialists 挂上后生效
        self._agents: Any = None
        self._outbox: Any = None
        if outbox is not None:
            self.attach_outbox(outbox)

    def attach_outbox(self, outbox: Any) -> None:
        """接上发件箱（app 建好 Outbox 之后调一次）：提一嘴只入队，发出去之后回写。"""
        self._outbox = outbox
        add_hook = getattr(outbox, "add_result_hook", None)
        if callable(add_hook):
            add_hook(self.on_result)
        else:
            logger.warning("发件箱没有 add_result_hook，提一嘴发出后回写不了")

    def _cfg(self, gid: str) -> dict:
        """这个群的「往群里发」设置（group_push 那一份）。

        失败关闭：读配置失败 / 拿不到 → 保守默认（开关全关、额度有限、默认睡觉时段），
        零读零写；绝不落进 legacy `settings=None` 放行口径去 seed 一份「默认开着」的配置。
        """
        try:
            settings = self._get_settings()
        except Exception:
            logger.debug("读配置失败（群 %s 的提一嘴设置按保守默认）", gid, exc_info=True)
            settings = None
        if settings is None:
            return group_push.conservative_defaults()
        return group_push.get_config(self._store, gid, settings)

    def scan(self, group_id: Any, now: float) -> int:
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return 0
        cfg = self._cfg(gid)
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
        origin = ""
        try:
            origin = str(idea["origin"] or "").strip() if "origin" in idea.keys() else ""
        except (KeyError, IndexError, TypeError):
            origin = ""
        feas = ""
        try:
            f = json.loads(idea["feasibility"] or "{}") if "feasibility" in idea.keys() else {}
            feas = str(f.get("note") or "") if isinstance(f, dict) else ""
        except (TypeError, ValueError):
            feas = ""
        # 人设只认 SOUL（2026-10-01 用户定）：不读 MaiBot 人格、不拿它的发言当样例
        persona = voice.persona(self._identity)
        # 提一嘴只要规矩（注入表里「做法」一栏是 —）：kind=main 只出规矩段，不注 learned skill
        gc_text = self._group_context_safe(gid, "main")
        rules = [
            persona.section(),
            "",
            "你刚想到一个构想，要在群里顺口关心地问一句（链接由程序附在后面，你不用写）。",
            "",
            f"构想标题：{title}",
            f"构想内容：{body[:300]}",
        ]
        if origin:
            who = ("这件事接的是**他自己之前在群里说过想做的那件事**"
                   if personal else "这件事接的是**群里之前聊过的那件事**")
            rules.append(f"{who}（由头）：{origin}")
        if feas:
            rules.append(f"能不能做：{feas[:120]}")
        if gc_text:
            rules.append(gc_text)
        rules += [
            "",
            "要求：",
            "- 用关心、顺口问一句的口吻，像「话说之前大家聊的那个 X 后来怎么样了？要我帮忙吗？」"
            "「话说你之前想弄的那个 X 怎么样了？要我搭把手吗？」这种问法。",
            "- 一两句，总共不超过 60 个字；口语、自然，结尾必须是问句。",
            "- 别推销、别邀功：不许写「我可以帮」「给大家带来」「推荐给大家」「安利」"
            "「感兴趣的话」「点进去看看」这类话。",
            "- 有由头就顺着由头问；没有就按构想内容自然地问一句要不要帮忙。别说「我刚想到」。",
            "- 别复述标题全文，不写链接、不写 QQ 号、不写 @，不用表情符号堆砌。",
        ]
        if personal:
            rules.append(
                "- 这个构想是给某一位群友的（程序会在前面 @ 他）。直接对他说「你」就行；"
                "只许提他自己在群里说过想做的那件事本身，**绝对不许**提他的画像、习惯、经历，"
                "不许出现「根据你的画像」「我注意到你」「看你平时」「你最近在…」「记得你」这类话。"
            )
        else:
            rules.append("- 是给全群的，不要点名任何人。")
        rules += [
            "- 不许出现「画像」这个词，不许写任何 QQ 号、@、链接。",
            '只回 JSON：{"text": "你要说的话"}',
        ]
        try:
            res = await self._models.chat(
                agent="idea",
                messages=[{"role": "user", "content": "\n".join(rules)}],
                json_mode=True,
                purpose="card_push.idea_mention",
                group_id=gid,
            )
            data = json.loads(str(getattr(res, "text", "") or ""))
            text = str(data.get("text") or "").strip() if isinstance(data, dict) else ""
        except Exception as e:  # 模型出错 / JSON 坏了 → 模板
            logger.info("构想提一嘴写话失败，用模板（群 %s）：%s", gid, e)
            text = ""
        if (not text or len(text) > _MENTION_MAX or _leaky(text, at_user)
                or _pitchy(text) or voice.is_self_intro(text)):
            if text:
                logger.info("构想提一嘴的话不合规（泄漏 / 推销腔 / 自我介绍 / 太长），换模板（群 %s）", gid)
            text = _template(title, personal, at_user, origin)
        return text

    def _group_context_safe(self, gid: str, kind: str) -> str:
        """统一注入（docs/17 §八.2）：本群规矩 + 本群<岗>的做法；没接线 / 出错 → ""。

        构想提一嘴只要规矩（注入表里「做法」一栏是 —），所以调用方传 kind="main"：
        group_context 对 main 只出规矩段，不注 learned skill。
        """
        agents = getattr(self, "_agents", None)
        if agents is None:
            return ""
        try:
            from . import group_context as _gc

            return str(_gc.group_context(agents, str(gid), kind) or "").strip()
        except Exception:
            logger.warning("读本群规矩 / 做法出错（群 %s 岗 %s），这次不注入", gid, kind, exc_info=True)
            return ""

    async def flush(self, group_id: Any, now: Optional[float] = None) -> None:
        """把到点的待发提一嘴「写好话 + 入队」（真正发出去由发件箱那一轮做）。

        - 开关关了 / 过期 / 构想被划掉 → 作废，不发陈旧的话。
        - 每日上限、睡觉时段、服务群都在发件箱发送前再查一遍（三种自制消息共用一个总数）。
        - 没接发件箱：什么都不发（也什么都不标），等 app 接线。
        """
        gid = str(group_id)
        if not _served(self._get_settings, gid):
            return
        moment = clock.now() if now is None else float(now)
        rows = self._store.read().execute(
            "SELECT * FROM idea_mentions WHERE group_id=? AND status='pending' AND due_ts<=? ORDER BY id",
            (gid, moment),
        ).fetchall()
        if not rows:
            return
        if self._pushes.in_quiet(moment, gid):
            return  # 睡觉时段：原样留着，醒来那轮再写话（不白叫一次模型）
        cfg = self._cfg(gid)
        if not cfg["idea_mention_enabled"]:
            for row in rows:
                self._set(int(row["id"]), status="dropped", error="开关已关")
            return
        outbox = self._outbox
        if outbox is None:
            logger.warning("构想提一嘴还没接到发件箱（app 未接线），这几条先不发（群 %s）", gid)
            return
        for row in rows:
            mid = int(row["id"])
            if moment - float(row["created"]) > EXPIRE_S:
                self._set(mid, status="dropped", error="超过 12 小时没发出去，作废")
                continue
            idea = self._store.read().execute(
                "SELECT * FROM ideas WHERE id=? AND group_id=?", (int(row["idea_id"]), gid)
            ).fetchone()
            if idea is None or str(idea["state"] or "") not in _IDEA_LIVE_STATES:
                self._set(mid, status="dropped", error="构想已经不在了或被划掉了")
                continue
            at_user = str(row["at_user"] or "")
            text = await self._write(gid, idea, bool(at_user), at_user)
            link = group_link(self._store, self._get_settings(), gid, tab="ideas", item=f"I-{int(idea['id'])}")
            full = f"{text}\n{link}" if link else text
            # Telegram 没有真正的 @ 段：Host 会把 @ 退成正文「@名字 」，名字这里给
            at_name = members.name_of(self._store, gid, at_user) if at_user else ""
            try:
                outbox.enqueue(
                    f"idea_mention:{mid}",
                    gid,
                    "text",
                    {"text": full, "push_kind": "idea_mention",
                     "at_user": at_user, "at_name": at_name,
                     # 提一嘴的期限沿用现有 12 小时窗口：过期就作废，不发陈旧的话
                     "expires_ts": float(row["created"]) + EXPIRE_S},
                )
            except Exception as e:
                logger.exception("构想提一嘴入队失败（群 %s 条目 %s）", gid, mid)
                self._set(mid, status="failed", error=f"入队失败：{str(e)[:_ERR_MAX]}")
                continue
            # 只入队：sent / 留痕 / 备忘都等发件箱真发出去之后由 on_result 写
            self._set(mid, status="queued", text=full)

    def on_result(self, info: dict) -> None:
        """发件箱的结果回调（attach_outbox 时自动挂上）。"""
        key = str((info or {}).get("key") or "")
        if not key.startswith("idea_mention:"):
            return
        try:
            mid = int(key.split(":", 1)[1])
        except (TypeError, ValueError):
            return
        gid = str(info.get("group_id") or "")
        outcome = str(info.get("outcome") or "")
        if outcome == "retrying":
            return
        try:
            self._settle(gid, mid, outcome, result=info.get("result") or {},
                         error=str(info.get("error") or ""),
                         moment=info.get("ts"))
        except Exception:
            logger.exception("构想提一嘴结果回写失败（条目 %s，outcome=%s）", mid, outcome)

    def _settle(self, gid: str, mid: int, outcome: str, *, result: Optional[dict] = None,
                error: str = "", moment: Optional[float] = None) -> None:
        """把发件箱的结果写回 idea_mentions；只有 sent 才算真发出去。"""
        now = float(moment) if moment is not None else clock.now()
        if outcome == "sent":
            self._set(mid, status="sent", sent_ts=now, error="",
                      message_id=str((result or {}).get("message_id") or ""))
            self._memo(gid, mid)
        elif outcome == "uncertain":
            self._set(mid, status="uncertain", sent_ts=now,
                      error=f"发送超时，可能已发出，不重发：{error}"[:_ERR_MAX])
        elif outcome == "failed":
            self._set(mid, status="failed", error=(error or "发送失败")[:_ERR_MAX])
        elif outcome == "dropped":
            self._set(mid, status="dropped", error=(error or "作废")[:_ERR_MAX])

    def _memo(self, gid: str, mid: int) -> None:
        """发完给 MaiBot 加一条 6 小时备忘。"""
        try:
            row = self._store.read().execute(
                "SELECT m.at_user, i.title, i.id FROM idea_mentions m"
                " LEFT JOIN ideas i ON i.id=m.idea_id WHERE m.id=?", (int(mid),)
            ).fetchone()
            title = str(row["title"] or "")[:60] if row is not None else ""
            who = "给一位群友的" if row is not None and str(row["at_user"] or "") else ""
            link = ""
            if row is not None and row["id"] is not None:
                link = group_link(self._store, self._get_settings(), gid,
                                  tab="ideas", item=f"I-{int(row['id'])}")
            self._mentions.add(
                gid,
                f"MaiWork 刚在群里提了一个{who}构想：{title}。有人问起可以接着聊"
                + (f"，详情在 {link}" if link else ""),
                key=f"idea_mention:{int(mid)}",
                ttl_s=_MEMO_TTL_S,
            )
        except Exception:
            logger.exception("构想提一嘴备忘失败（群 %s，条目 %s）", gid, mid)

    def recover(self, outbox: Any = None) -> int:
        """插件重启之后和发件箱对账（同 CardPush.recover）：没入过队的回 pending，
        发出去的补记 sent + 备忘，结果不明的照抄；绝不重复发。
        """
        ob = outbox if outbox is not None else self._outbox
        rows = self._store.read().execute(
            "SELECT id, group_id FROM idea_mentions WHERE status IN ('queued','sending') ORDER BY id"
        ).fetchall()
        if not rows:
            return 0
        if ob is None:
            with self._store.tx() as conn:
                cur = conn.execute(
                    "UPDATE idea_mentions SET status='uncertain',"
                    " error='插件重启时发送中断，标为不确定，不自动重发' WHERE status='sending'"
                )
                return int(cur.rowcount or 0)
        changed = 0
        for r in rows:
            mid = int(r["id"])
            gid = str(r["group_id"])
            legacy_sending = str(r["status"] or "") == "sending"
            box = self._store.read().execute(
                "SELECT status, result, error FROM outbox WHERE key=?", (f"idea_mention:{mid}",)
            ).fetchone()
            if box is None:
                if legacy_sending:
                    self._set(mid, status="uncertain",
                              error="插件重启时发送中断，标为不确定，不自动重发")
                else:
                    self._set(mid, status="pending", error="")
                changed += 1
                continue
            status = str(box["status"] or "")
            if status in ("pending", "sending"):
                continue
            try:
                result = json.loads(box["result"] or "{}")
            except (TypeError, ValueError):
                result = {}
            self._settle(gid, mid, status, result=result if isinstance(result, dict) else {},
                         error=str(box["error"] or ""))
            changed += 1
        return changed

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
        return {"config": self._cfg(gid), "sent_today": self.sent_today(gid, now), "recent": recent}
