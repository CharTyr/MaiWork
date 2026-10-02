"""delivery.py（M2 部分）：可提起清单 Mentions、按话题接上 TopicMatcher、推送节制 Pushes。

- Mentions：往 mentions 表存「可以顺口提起」的素材；render(session_id) 经
  groups.session_id 找群，把未过期且 turns_left>0 的拼成 ≤300 字备忘；
  inject(kwargs) 往 maisaka.planner.before_request 的 items 里追加。
- TopicMatcher：群友自己聊起某个话题时，用最近几条群消息去对资讯/构想的关键词
  （docs/02 §4.1「群友自己聊起时接得上」）。纯代码匹配，不调模型、不做网络，
  候选每群一份缓存 60 秒，钩子里不做重查询。
- Pushes：每日上限 + 睡觉时段；kind="error" / "command" 不受限。
"""

from __future__ import annotations

import copy
import html
import json
import logging
import re
from typing import Any, Callable, Optional

from . import chat_feed, clock
from .config import Settings
from .store import Store

logger = logging.getLogger("maiwork.delivery")

HEADER = "【MaiWork 备忘】话题相关时可以自然提起，不必每条都说："
MAX_TEXT_LENGTH = 300  # 备忘总文字 ≤300 字
MARKER = "【MaiWork 备忘】"  # 已注入标记：inject 排重用（不含「话题相关时可以自然提起」整段）
# 普通交付算主动推送；只有群友用 /mw 领取 <任务号> 当场索取时，
# 才把待发的本任务成品升级为 awaited_delivery。它与故障及指令回执一样不受
# 睡觉时段/每日额度限制，仍记发送审计；不要把所有 delivery 都豁免。
PUSH_EXEMPT_KINDS = frozenset(("error", "command", "admin", "awaited_delivery"))
# 自带每日上限的推送（card_push.py：资讯卡片 / 构想提一嘴）：留 pushes 记录，但不占
# delivery.push_per_day（开场白那份额度），节制由它们自己的每群上限管
SELF_QUOTA_KINDS = frozenset(("news_card", "idea_mention"))

# MaiBot 上下文里聊天消息形如 `<message msg_id="..">文本</message>` 的 text part
# （出处：reference/jev/processor.py 的 _parse_chat_message）；
# is_self_message="true" 是 MaiBot 自己说的，不算群友在聊。
_MESSAGE_BLOCK = re.compile(r"<message\s+([^>]*)>(.*?)</message>", re.DOTALL)
_MESSAGE_PREFIX = re.compile(r"^<message\s+([^>]*)>", re.DOTALL)
_MESSAGE_ATTR = re.compile(r'([a-zA-Z_]+)="([^"]*)"')


class TopicMatcher:
    """按话题接上（docs/02 §4.1「群友自己聊起时接得上」）。

    - recent_chat_text(kwargs)：从 planner 载荷 items 里抽出最近 6 条群友发言
      （`<message …>文本</message>` 的 text part；is_self_message="true" 跳过），
      拼成小写文本。
    - candidates_for(group_id)：本群候选内容——最近 3 天通过的 news_items
      （rejected=0，kind news/guide）+ 最近 7 天未收起的 ideas，带 keywords。
      每群一份内存缓存，60 秒刷新一次；钩子里不做重查询。
    - match(candidates, text)：关键词（≥2 字符，小写）出现在最近消息文本里算命中；
      同一条命中 ≥2 个不同关键词 → 接得上；按命中数、新旧排，调用方取前几条。
      （原来「想在群里聊」够 2 票时命中 1 个也算，2026-09-29 随气泡按钮删掉。）
    - memo_lines(group_id, kwargs)：命中 → [(文本, key)]，同一条同群 30 分钟内
      最多给 3 轮（record_injected 由调用方在真的注入后记账）。
    全程不调模型、不做网络，候选只有资讯/构想（没有关注成员个人画像）。
    """

    CACHE_TTL_S = 60.0
    JAB_WINDOW_S = 30 * 60
    JAB_MAX = 3
    RECENT_LIMIT = 6
    NEWS_DAYS = 3
    IDEA_DAYS = 7
    SNIPPET_LEN = 60
    TAKE = 2
    GENERIC_DF = 3   # 本群这么多条候选都有的词算泛词
    ASK_DAYS = 2     # 有人问「最近有啥新鲜事」时只递近 2 天的

    def __init__(self, store: Store) -> None:
        self._store = store
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._jabs: dict[tuple[str, str], list[float]] = {}

    # ------------------------------------------------------------------
    # 解析：items 里的群聊消息
    # ------------------------------------------------------------------

    @staticmethod
    def _item_text(item: Any) -> str:
        """一个 item 里的 text part 拼成一段（线上 2026-10-02：带图 / 表情 / @ 的消息，
        第一个 part 只有 `<message …>` 前缀或「(@了你)」，正文在后面的 part；
        只看第一个 part 会丢掉约四分之一群友消息的正文）。"""
        parts = item.get("parts") if isinstance(item, dict) else None
        texts = [
            p["text"] for p in (parts if isinstance(parts, list) else ())
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        ]
        return "\n".join(texts)

    @staticmethod
    def _parse_chat_bodies(text: str) -> list[tuple[bool, str]]:
        """一段 text part → [(是否机器人自己, 正文)]。兼容整块和一个 part 只有一条。"""
        out: list[tuple[bool, str]] = []
        matches = list(_MESSAGE_BLOCK.finditer(text))
        if matches:
            for m in matches:
                attrs = dict(_MESSAGE_ATTR.findall(m.group(1)))
                body = html.unescape(m.group(2).strip())
                if body:
                    out.append((attrs.get("is_self_message", "").lower() == "true", body))
            return out
        prefix = _MESSAGE_PREFIX.match(text)
        if prefix is not None:
            attrs = dict(_MESSAGE_ATTR.findall(prefix.group(1)))
            body = text[prefix.end():]
            if body.endswith("</message>"):
                body = body[: -len("</message>")]
            body = html.unescape(body.strip())
            if body:
                out.append((attrs.get("is_self_message", "").lower() == "true", body))
        return out

    def recent_chat_text(self, kwargs: Any) -> str:
        """kwargs["items"] 里最近 N 条群友发言拼成小写文本；没有 → ""。"""
        try:
            if not isinstance(kwargs, dict):
                return ""
            items = kwargs.get("items")
            if not isinstance(items, list):
                return ""
            bodies: list[str] = []
            for item in items:
                text = self._item_text(item)
                if not text:
                    continue
                for is_self, body in self._parse_chat_bodies(text):
                    if is_self:
                        continue
                    bodies.append(body[:1200])
            bodies = bodies[-self.RECENT_LIMIT:]
            return "\n".join(bodies).lower()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 候选（每群一份缓存，60 秒刷新）
    # ------------------------------------------------------------------

    @staticmethod
    def _keywords_of(raw: Any) -> list[str]:
        """JSON 关键词列表 → 小写、去重、只留能用的（2026-10-01：纯数字 / 日期、两个字母的英文、
        英文常用词不要，见 chat_feed.usable）。"""
        return chat_feed.keywords(raw)

    @staticmethod
    def _first_link(raw: Any) -> str:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return ""
        if not isinstance(data, list):
            return ""
        for s in data:
            if isinstance(s, dict):
                url = str(s.get("url") or "")
                if url:
                    return url
        return ""

    def _load_candidates(self, group_id: str, now: float) -> list[dict]:
        gid = str(group_id)
        out: list[dict] = []
        rows = self._store.read().execute(
            "SELECT id, title, summary, body, keywords, sources, created, score, up, down"
            " FROM news_items"
            " WHERE group_id=? AND rejected=0 AND kind IN ('news','guide') AND created>=?"
            " AND target_user_id=''"
            " ORDER BY created DESC",
            (gid, float(now) - self.NEWS_DAYS * 86400),
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "key": f"news:{int(r['id'])}",
                    "title": str(r["title"] or ""),
                    "body": str(r["body"] or "") or str(r["summary"] or ""),
                    "link": self._first_link(r["sources"]),
                    "keywords": self._keywords_of(r["keywords"]),
                    "created": float(r["created"] or 0.0),
                    "score": float(r["score"] or 0.0),
                    "liked": int(r["down"] or 0) <= int(r["up"] or 0),
                }
            )
        rows = self._store.read().execute(
            "SELECT id, title, body, keywords, created FROM ideas"
            " WHERE group_id=? AND state!='dismissed' AND created>=?"
            " AND target_user_id=''"
            " ORDER BY created DESC",
            (gid, float(now) - self.IDEA_DAYS * 86400),
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "key": f"idea:{int(r['id'])}",
                    "title": str(r["title"] or ""),
                    "body": str(r["body"] or ""),
                    "link": "",
                    "keywords": self._keywords_of(r["keywords"]),
                    "created": float(r["created"] or 0.0),
                }
            )
        # 本群 ≥GENERIC_DF 条候选都有的词是泛词（935 群的 llm / prompt 这种），不能当「具体词」
        df: dict[str, int] = {}
        for c in out:
            for kw in c["keywords"]:
                df[kw] = df.get(kw, 0) + 1
        for c in out:
            c["spec"] = {kw for kw in c["keywords"] if chat_feed.specific(kw) and df.get(kw, 0) < self.GENERIC_DF}
        return out

    def candidates_for(self, group_id: str, now: Optional[float] = None) -> list[dict]:
        """本群候选；内存缓存每群一份，60 秒内直接用。"""
        gid = str(group_id)
        ts = clock.now() if now is None else float(now)
        hit = self._cache.get(gid)
        if hit is not None and ts - hit[0] < self.CACHE_TTL_S:
            return hit[1]
        cands = self._load_candidates(gid, ts)
        self._cache[gid] = (ts, cands)
        return cands

    # ------------------------------------------------------------------
    # 匹配（纯代码）
    # ------------------------------------------------------------------

    @staticmethod
    def match_hits(candidates: list[dict], recent_text: str, latest: Optional[str] = None) -> list[tuple[dict, list[str]]]:
        """接得上 = 命中 ≥2 个不同关键词，其中至少一个是本条的「具体词」（spec），
        且至少一个出现在最新两条群友消息里（latest；不给就不卡）。英文按整词对；
        网址、[事件-…] 系统提示先洗掉。按命中数、新旧排。"""
        text = chat_feed.clean(recent_text)
        last = chat_feed.clean(latest) if latest is not None else None
        scored: list[tuple[int, float, dict, list[str]]] = []
        for cand in candidates:
            hits = [kw for kw in cand.get("keywords") or () if chat_feed.contains(kw, text)]
            if len(hits) < 2:
                continue
            spec = cand.get("spec")
            if spec is None:
                spec = {kw for kw in cand.get("keywords") or () if chat_feed.specific(kw)}
            if not any(kw in spec for kw in hits):
                continue
            if last is not None and not any(chat_feed.contains(kw, last) for kw in hits):
                continue
            scored.append((len(hits), float(cand.get("created") or 0.0), cand, hits))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [(c, h) for _, _, c, h in scored]

    @staticmethod
    def match(candidates: list[dict], recent_text_lower: str) -> list[dict]:
        return [c for c, _ in TopicMatcher.match_hits(candidates, recent_text_lower)]

    # ------------------------------------------------------------------
    # 30 分钟 3 轮节制
    # ------------------------------------------------------------------

    def _jab_count(self, gid: str, key: str, now: float) -> int:
        stamps = self._jabs.get((gid, key))
        if not stamps:
            return 0
        cutoff = now - self.JAB_WINDOW_S
        fresh = [s for s in stamps if s > cutoff]
        if len(fresh) != len(stamps):
            self._jabs[(gid, key)] = fresh
        return len(fresh)

    def record_injected(self, group_id: str, keys: list[str], now: Optional[float] = None) -> None:
        """真的注入过之后记账；同一条同群 30 分钟内最多 3 轮。"""
        gid = str(group_id)
        ts = clock.now() if now is None else float(now)
        for key in keys:
            k = (gid, str(key))
            stamps = self._jabs.get(k)
            if stamps is None:
                stamps = []
                self._jabs[k] = stamps
            stamps.append(ts)

    # ------------------------------------------------------------------
    # 组合：给 inject 用的话题行
    # ------------------------------------------------------------------

    @staticmethod
    def _format_line(cand: dict, mode: str = "topic") -> str:
        """2026-10-01：写成 MaiBot 自己看到过的事，不提 MaiWork；链接只在有人追问出处时给。"""
        snippet = str(cand.get("body") or "")[: TopicMatcher.SNIPPET_LEN]
        link = str(cand.get("link") or "")
        link_part = f"（有人问出处再给：{link}）" if link else ""
        what = f"{cand.get('title') or ''}——{snippet}{link_part}"
        if str(cand.get("key") or "").startswith("idea:"):
            return f"群里在聊的和你之前想到的一个点子有关，想接就自然接一句：{what}"
        if mode == "ask":
            return f"群里有人在问最近有什么新鲜事，你最近看到过这条，想说就挑着说、别照念：{what}"
        return f"你最近看到过这条，和群里在聊的有关，想接就自然接一句、别照念：{what}"

    def chat_bodies(self, kwargs: Any) -> tuple[list[str], list[str]]:
        """kwargs["items"] → (群友发言, MaiBot 自己的发言)，都按时间从旧到新。"""
        users: list[str] = []
        selfs: list[str] = []
        try:
            items = kwargs.get("items") if isinstance(kwargs, dict) else None
            for item in items if isinstance(items, list) else ():
                text = self._item_text(item)
                if not text:
                    continue
                for is_self, body in self._parse_chat_bodies(text):
                    (selfs if is_self else users).append(body[:1200])
        except Exception:
            return [], []
        return users, selfs

    def _ask_candidates(self, cands: list[dict], ts: float) -> list[dict]:
        news = [
            c for c in cands
            if str(c["key"]).startswith("news:") and c.get("liked", True)
            and float(c.get("created") or 0.0) >= ts - self.ASK_DAYS * 86400
        ]
        news.sort(key=lambda c: (-float(c.get("score") or 0.0), -float(c.get("created") or 0.0)))
        return news

    def memo_entries(self, group_id: str, kwargs: Any, now: Optional[float] = None) -> list[dict]:
        """命中 → [{text, key, mode, title, hit, words, link}]，最多 TAKE 条（话题在前、问新鲜事在后）。"""
        gid = str(group_id)
        ts = clock.now() if now is None else float(now)
        try:
            users, _ = self.chat_bodies(kwargs)
            users = users[-self.RECENT_LIMIT:]
            if not users:
                return []
            cands = self.candidates_for(gid, ts)
            if not cands:
                return []
            latest = "\n".join(users[-2:])
            picked: list[tuple[dict, str, list[str]]] = [
                (c, "topic", h) for c, h in self.match_hits(cands, "\n".join(users), latest)
            ]
            if chat_feed.is_ask(latest):
                picked += [(c, "ask", []) for c in self._ask_candidates(cands, ts)]
            out: list[dict] = []
            seen: set[str] = set()
            for cand, mode, hits in picked:
                key = str(cand["key"])
                if len(out) >= self.TAKE:
                    break
                if key in seen or self._jab_count(gid, key, ts) >= self.JAB_MAX:
                    continue
                seen.add(key)
                out.append({
                    "text": self._format_line(cand, mode), "key": key, "mode": mode,
                    "title": str(cand.get("title") or ""), "hit": hits,
                    "words": list(cand.get("keywords") or ()), "link": str(cand.get("link") or ""),
                })
            return out
        except Exception:
            logger.exception("TopicMatcher.memo_entries 异常，已吞掉（群 %s）", gid)
            return []

    def memo_lines(self, group_id: str, kwargs: Any, now: Optional[float] = None) -> list[tuple[str, str]]:
        """命中 → [(行文本, 候选 key)]，最多 TAKE 条。调用方注入后要 record_injected。"""
        return [(e["text"], e["key"]) for e in self.memo_entries(group_id, kwargs, now)]


class Mentions:
    """可提起清单：往 MaiBot 的 planner 上下文追加一小段备忘。

    - add()：存素材，同 key 覆盖。
    - render(session_id)：拼出备忘文本；不调模型、纯查库；非服务群或没货 → None。
    - inject(kwargs)：钩子本体；追加到第一个 SystemMessageItem 最后一个 text part。
      话题接龙（TopicMatcher）命中的行排在已存备忘前面，共用 300 字总上限。
    """

    def __init__(self, store: Store, get_settings: Callable[[], Settings]) -> None:
        self._store = store
        self._get_settings = get_settings
        self._topics = TopicMatcher(store)
        # 每群见过的 MaiBot 发言（判「聊到了」只看新发言；重载后第一次只当底）
        self._seen_self: dict[str, set[str]] = {}

    @staticmethod
    def _served_group_id_for_session(store: Store, session_id: str) -> str:
        """groups 表里查这个 session_id 对应的服务群；找不到就 ""。"""
        if not session_id:
            return ""
        row = store.read().execute(
            "SELECT group_id FROM groups WHERE session_id=? LIMIT 1",
            (str(session_id),),
        ).fetchone()
        if row is None:
            return ""
        return str(row["group_id"]) if row["group_id"] else ""

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5) -> None:
        """存一条备忘；同 key 覆盖。"""
        gid = str(group_id)
        now = clock.now()
        # 先删后插，避免 UNIQUE 冲突报错；同事务
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO mentions (group_id, key, text, expires_ts, turns_left, created)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(group_id, key) DO UPDATE SET"
                " text=excluded.text, expires_ts=excluded.expires_ts,"
                " turns_left=excluded.turns_left, created=excluded.created",
                (gid, str(key), str(text), now + float(ttl_s), int(turns), now),
            )

    def _mention_rows(self, group_id: str) -> list[Any]:
        now = clock.now()
        return self._store.read().execute(
            "SELECT id, text, turns_left FROM mentions"
            " WHERE group_id=? AND expires_ts>? AND turns_left>0"
            " ORDER BY created DESC",
            (str(group_id), now),
        ).fetchall()

    def _decrement_turns(self, used_ids: list[int], group_id: str) -> None:
        """turn 递减：只扣这次真的放进渲染的那些。"""
        if not used_ids:
            return
        try:
            placeholders = ",".join("?" for _ in used_ids)
            with self._store.tx() as conn:
                conn.execute(
                    f"UPDATE mentions SET turns_left=turns_left-1 WHERE id IN ({placeholders})",
                    used_ids,
                )
        except Exception:
            logger.exception("mentions turn 递减失败（group=%s）", group_id)

    def _render_for_group(self, group_id: str) -> Optional[str]:
        """给某个群拼备忘（不含话题接龙）；没货 → None。"""
        lines: list[str] = []
        used_ids: list[int] = []
        remaining = MAX_TEXT_LENGTH - len(HEADER)
        for r in self._mention_rows(group_id):
            line = f"- {r['text']}"
            if len(line) > remaining:
                continue  # 跳过过长的，继续看后面短的
            lines.append(line)
            used_ids.append(int(r["id"]))
            remaining -= len(line) + 1  # +1 是换行
        if not lines:
            return None
        self._decrement_turns(used_ids, group_id)
        return HEADER + "\n" + "\n".join(lines)

    def _check_said(self, group_id: str, kwargs: Any) -> None:
        """MaiBot 的新发言里提到了刚递过的资讯 → chat_feeds 记「聊到了」（chat_feed.check_said）。"""
        gid = str(group_id)
        try:
            _, selfs = self._topics.chat_bodies(kwargs)
            seen = self._seen_self.get(gid)
            if seen is None:
                self._seen_self[gid] = set(selfs)
                return
            new = [b for b in selfs if b not in seen]
            if not new:
                return
            seen.update(new)
            if len(seen) > 500:
                self._seen_self[gid] = set(selfs)
            with self._store.tx() as conn:
                chat_feed.check_said(conn, gid, new, clock.now())
        except Exception:
            logger.exception("资讯反哺：判「聊到了」失败（group=%s）", gid)

    def _memo_with_topics(self, group_id: str, kwargs: Any) -> Optional[str]:
        """话题接龙行排前面 + 已存备忘，共用同一个 300 字总上限；没货 → None。"""
        self._check_said(group_id, kwargs)
        topic_entries = self._topics.memo_entries(group_id, kwargs)
        lines: list[str] = []
        used_ids: list[int] = []
        used_keys: list[str] = []
        used_entries: list[dict] = []
        remaining = MAX_TEXT_LENGTH - len(HEADER)
        for entry in topic_entries:
            line = f"- {entry['text']}"
            if len(line) > remaining:
                continue
            lines.append(line)
            used_keys.append(entry["key"])
            used_entries.append(entry)
            remaining -= len(line) + 1
        for r in self._mention_rows(group_id):
            line = f"- {r['text']}"
            if len(line) > remaining:
                continue
            lines.append(line)
            used_ids.append(int(r["id"]))
            remaining -= len(line) + 1
        if not lines:
            return None
        self._decrement_turns(used_ids, group_id)
        if used_keys:
            try:
                self._topics.record_injected(group_id, used_keys)
            except Exception:
                logger.exception("话题接龙记账失败（group=%s）", group_id)
            try:
                with self._store.tx() as conn:
                    chat_feed.record(conn, str(group_id), used_entries, clock.now())
            except Exception:
                logger.exception("资讯反哺记账失败（group=%s）", group_id)
        return HEADER + "\n" + "\n".join(lines)

    def _resolve_group(self, session_id: str) -> str:
        """session_id → 服务群号；找不到或不再服务 → ""（G4：is_served 复核）。"""
        gid = self._served_group_id_for_session(self._store, session_id)
        if not gid:
            return ""
        try:
            settings = self._get_settings()
            if settings is not None and not settings.is_served(gid):
                return ""
        except Exception:
            return ""
        return gid

    def render(self, session_id: str) -> Optional[str]:
        """拼本群的备忘；非服务群 / 找不到 → None。

        G4：groups 表里的 session_id 映射是历史残留（热更新删群后还在），
        所以查到群后必须再用 settings.is_served 复核；不再服务 → None。
        """
        gid = self._resolve_group(session_id)
        if not gid:
            return None
        return self._render_for_group(gid)

    # ------------------------------------------------------------------
    # maisaka.planner.before_request 钩子
    # ------------------------------------------------------------------

    def inject(self, kwargs: dict, *, group_id: str = "") -> Optional[dict]:
        """追加到第一个 SystemMessageItem 最后一个 text part。

        group_id 由已验证服务群的 planner 钩子传入；不再按同一个 session_id
        二次查库决定群，避免历史/歧义会话映射把隔壁群备忘注入当前群。

        - 取 kwargs["items"]（list）；没有 / 不是 list → None。
        - 找第一个 item_type=="SystemMessageItem" 的 item，它最后一个 type=="text" 的 part。
        - 已经含固定标题（本插件或其他实例加过）→ None。
        - 追加 "\n\n" + 备忘文本；保留其他所有键（item_schema_version 等）。
        - 深拷贝后返回整个新 kwargs；任何异常/格式不符 → None。
        """
        try:
            if not isinstance(kwargs, dict):
                return None
            items = kwargs.get("items")
            if not isinstance(items, list):
                return None
            # 已经加过了就不重复
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("item_type") != "SystemMessageItem":
                    continue
                parts = item.get("parts")
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") != "text":
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and MARKER in text:
                        return None

            # 取 session_id 决定加哪个群的备忘；非服务群连候选都不查
            session_id = str(kwargs.get("session_id") or "")
            if not session_id:
                return None
            if group_id:
                gid = str(group_id)
                try:
                    if not self._get_settings().is_served(gid):
                        return None
                except Exception:
                    return None
            else:
                gid = self._resolve_group(session_id)
                if not gid:
                    return None
            memo = self._memo_with_topics(gid, kwargs)
            if memo is None:
                return None

            # 深拷贝整个 kwargs（不动原 dict），在第一个 SystemMessageItem 里追加
            out = copy.deepcopy(kwargs)
            for item in out["items"]:
                if not isinstance(item, dict):
                    continue
                if item.get("item_type") != "SystemMessageItem":
                    continue
                parts = item.get("parts")
                if not isinstance(parts, list):
                    return None
                # 找最后一个 type=="text" 的 part
                target = None
                for part in reversed(parts):
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        target = part
                        break
                if target is None:
                    return None
                target["text"] = target["text"] + "\n\n" + memo
                return out
            return None
        except Exception:
            logger.exception("Mentions.inject 异常，已吞掉")
            return None


class Pushes:
    """推送节制：每日上限 + 睡觉时段。"""

    def __init__(self, store: Store, get_settings: Callable[[], Settings]) -> None:
        self._store = store
        self._get_settings = get_settings

    def can_push(self, group_id: str, kind: str, now: float) -> tuple[bool, str]:
        """决定能不能推。故障、指令回执和明确领取的成品即时发送。

        now 决定「用哪一天的额度」和「是不是在睡觉时段」。
        """
        kind = str(kind or "")
        if kind in PUSH_EXEMPT_KINDS:
            return True, ""
        settings = self._get_settings()
        if self.in_quiet(now):
            return False, "睡觉时段"
        # 每日上限
        limit = int(getattr(settings.delivery, "push_per_day", 3))
        if limit > 0 and self._count_for_day(group_id, float(now), kind=None) >= limit:
            return False, "今天推够了"
        return True, ""

    def _quiet_range(self) -> tuple[int, int]:
        quiet = getattr(self._get_settings().delivery, "quiet_hours", "") or "23:00-08:00"
        try:
            return clock.parse_hhmm_range(quiet)
        except (ValueError, AttributeError):
            return 0, 0  # 配错按不限制

    def in_quiet(self, now: float) -> bool:
        """now 在不在睡觉时段（delivery.quiet_hours）。"""
        s, e = self._quiet_range()
        return s != e and clock.in_range(float(now), (s, e))

    def record(self, group_id: str, kind: str, text: str, now: float) -> None:
        """任何 kind 都留发送记录；豁免种类不占每日额度。"""
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO pushes (group_id, ts, day, kind, text) VALUES (?, ?, ?, ?, ?)",
                (str(group_id), float(now), clock.day_key(float(now)), str(kind or ""), str(text or "")[:500]),
            )

    def _count_for_day(self, group_id: str, ts: float, kind: Optional[str]) -> int:
        """ts 所在北京那一天推过几条。kind=None 只算需要节制的推送。"""
        day = clock.day_key(float(ts))
        if kind is None:
            exempt = tuple(sorted(PUSH_EXEMPT_KINDS | SELF_QUOTA_KINDS))
            placeholders = ", ".join("?" for _ in exempt)
            row = self._store.read().execute(
                "SELECT COUNT(*) AS c FROM pushes"
                f" WHERE group_id=? AND day=? AND kind NOT IN ({placeholders})",
                (str(group_id), day, *exempt),
            ).fetchone()
        else:
            row = self._store.read().execute(
                "SELECT COUNT(*) AS c FROM pushes WHERE group_id=? AND day=? AND kind=?",
                (str(group_id), day, str(kind)),
            ).fetchone()
        return int(row["c"]) if row else 0

    def count_today(self, group_id: str, kind: Optional[str] = None) -> int:
        """今天（按北京时间）推过几条。kind=None 算所有推送总数（含 error / command 的记录）。"""
        day = clock.day_key(clock.now())
        if kind is None:
            row = self._store.read().execute(
                "SELECT COUNT(*) AS c FROM pushes WHERE group_id=? AND day=?",
                (str(group_id), day),
            ).fetchone()
        else:
            row = self._store.read().execute(
                "SELECT COUNT(*) AS c FROM pushes WHERE group_id=? AND day=? AND kind=?",
                (str(group_id), day, str(kind)),
            ).fetchone()
        return int(row["c"]) if row else 0
