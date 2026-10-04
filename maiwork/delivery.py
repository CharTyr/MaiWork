"""delivery.py（M2 部分）：可提起清单 Mentions、按话题接上 TopicMatcher、推送节制 Pushes。

- Mentions：往 mentions 表存「可以顺口提起」的素材；render(session_id) 经
  groups.session_id 找群，把未过期且 turns_left>0 的拼成 ≤300 字备忘；
  inject(kwargs) 往 maisaka.planner.before_request 的 items 里追加。
- TopicMatcher：群友自己聊起某个话题时，用最近几条群消息去对资讯/构想的关键词
  （docs/02 §4.1「群友自己聊起时接得上」）。纯代码匹配，不调模型、不做网络，
  候选每群一份缓存 60 秒，钩子里不做重查询。
- Pushes：每群一个每日总上限 + 一份睡觉时段（都从 group_push 读，三种自制消息
  topic / news_card / idea_mention 一起数）；kind="error" / "command" / "admin" /
  "awaited_delivery" 不受限。结果不明（sending / uncertain）的发送安全保留额度。
  非服务群（或认不出服务名单）：`can_push` 直接不可推，**零 SQL**——不读每群设置、
  不数额度、不看睡觉时段；非服务群零读取是红线。
"""

from __future__ import annotations

import copy
import html
import json
import logging
import re
from typing import Any, Callable, Optional

from . import clock, group_push
from .config import Settings
from .store import Store

logger = logging.getLogger("maiwork.delivery")


# ----------------------------------------------------------------------
# 关键词助手（原 chat_feed.py 里「关键词修准 / 聊天文本清洗 / 新鲜事问话识别」；
# 2026-10 喂给 MaiBot 的那条「记账 / 聊到了」路径已删，这些助手就放在 TopicMatcher 自家文件里）
# ----------------------------------------------------------------------

_STOP_ASCII = frozenset(
    """the and for with you are was not but all new now how why what this that from have has its can get got
    one two out use via pro max mini plus well yes lol app web day man let see too off way who did our may
    any her his him she they them then than just like more most some such only also into over very will
    your about after again""".split()
)
_DIGITS_PUNCT = re.compile(r"^[\d\W_]+$")
_ASCII = re.compile(r"^[\x00-\x7f]+$")
_HAS_ALPHA = re.compile(r"[a-z]")
_HAS_DIGIT = re.compile(r"\d")
_CJK = re.compile(r"[\u3400-\u9fff]")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EVENT = re.compile(r"\[事件-[^\]]*\][^\n]*")
# 表情包 / 图片的自动描述不算群友在聊什么（线上 2026-10-02 回放：图片描述带来的命中
# 大多是误撞，「探索」+「switch」撞上不相干的游戏，甚至撞上群友转发的 MaiWork 自己的资讯卡截图）
_EMOJI = re.compile(r"\[(?:表情包|图片)[:：][^\]]*(?:\]|$)")
# 问「最近有什么新鲜事」：时间词 + 新闻词，或 有什么/有啥/来点 + 新闻词
_ASK_NOUN = r"(新闻|新鲜事|资讯|大事|热点|瓜|好玩的事)"
_ASK = (
    re.compile(r"(最近|今天|今日|这两天|这几天|近期|这周)[^。！？\n]{0,8}" + _ASK_NOUN),
    re.compile(r"(有什么|有啥|有没有|来点|来些)[^。！？\n]{0,4}" + _ASK_NOUN),
)


def usable_kw(kw: str) -> bool:
    s = str(kw or "").strip().lower()
    if len(s) < 2 or _DIGITS_PUNCT.match(s):
        return False
    if _ASCII.match(s):
        if _HAS_ALPHA.search(s) and _HAS_DIGIT.search(s):
            return True
        return len(s) >= 3 and s not in _STOP_ASCII
    return True


def specific_kw(kw: str) -> bool:
    s = str(kw or "").strip().lower()
    if not usable_kw(s):
        return False
    if _ASCII.match(s):
        return len(s) >= 4 or bool(_HAS_ALPHA.search(s) and _HAS_DIGIT.search(s))
    return len(_CJK.findall(s)) >= 3 or bool(re.search(r"[a-z0-9]", s))


def _kw_usable_walk(data: Any) -> list[str]:
    out: list[str] = []
    for kw in data:
        s = str(kw or "").strip().lower()
        if s and s not in out and usable_kw(s):
            out.append(s)
    return out


def usable_keywords(raw: Any) -> list[str]:
    """JSON 关键词列表 → 小写、去重、只留能用的。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return _kw_usable_walk(data)


def clean_chat_text(text: str) -> str:
    return _URL.sub(" ", _EMOJI.sub(" ", _EVENT.sub(" ", str(text or "")))).lower()


def kw_contains(kw: str, text_lower: str) -> bool:
    if _ASCII.match(kw):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text_lower) is not None
    return kw in text_lower


def is_fresh_news_ask(text: str) -> bool:
    s = str(text or "")
    return any(pat.search(s) for pat in _ASK)


HEADER = "【MaiWork 备忘】话题相关时可以自然提起，不必每条都说："
MAX_TEXT_LENGTH = 300  # 备忘总文字 ≤300 字
MARKER = "【MaiWork 备忘】"  # 已注入标记：inject 排重用（不含「话题相关时可以自然提起」整段）
# 普通交付算主动推送；只有群友用 /mw 领取 <任务号> 当场索取时，
# 才把待发的本任务成品升级为 awaited_delivery。它与故障及指令回执一样不受
# 睡觉时段/每日额度限制，仍记发送审计；不要把所有 delivery 都豁免。
PUSH_EXEMPT_KINDS = frozenset(("error", "command", "admin", "awaited_delivery"))
# 读不到可信的每群配置时的理由：**不是**「开关已关」（那条会被作废），
# 调用方（Outbox.flush）据此只推迟、不发、也不作废。
UNREADABLE_REASON = "读不到设置"
# 这个群根本不在服务名单里（或认不出在不在）：直接不可推，且**零 SQL**——
# 连每群设置 / 每日额度 / 睡觉时段都不查。非服务群零读取是红线。
UNSERVED_REASON = "非服务群"
# 2026-10-03（docs/18 第三步）：「自带每群每日上限、不占开场白额度」的那一套退役。
# 现在每群只有一份设置（group_push.py）：一个 daily_max 把 topic / news_card /
# idea_mention 三种自制消息一起数，睡觉时段也只有一份；豁免清单保持原样，不扩也不缩。

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
        英文常用词不要，见本文件 usable_kw；原 chat_feed.py 账路径已删，助手就留这里）。"""
        return usable_keywords(raw)

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
            c["spec"] = {kw for kw in c["keywords"] if specific_kw(kw) and df.get(kw, 0) < self.GENERIC_DF}
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
        text = clean_chat_text(recent_text)
        last = clean_chat_text(latest) if latest is not None else None
        scored: list[tuple[int, float, dict, list[str]]] = []
        for cand in candidates:
            hits = [kw for kw in cand.get("keywords") or () if kw_contains(kw, text)]
            if len(hits) < 2:
                continue
            spec = cand.get("spec")
            if spec is None:
                spec = {kw for kw in cand.get("keywords") or () if specific_kw(kw)}
            if not any(kw in spec for kw in hits):
                continue
            if last is not None and not any(kw_contains(kw, last) for kw in hits):
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
            if is_fresh_news_ask(latest):
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

    def _memo_with_topics(self, group_id: str, kwargs: Any) -> Optional[str]:
        """话题接龙行排前面 + 已存备忘，共用同一个 300 字总上限；没货 → None。

        2026-10：只递不记账（chat_feeds 表和 record/check_said 已删；「聊到了」状态也删）。
        """
        topic_entries = self._topics.memo_entries(group_id, kwargs)
        lines: list[str] = []
        used_ids: list[int] = []
        used_keys: list[str] = []
        remaining = MAX_TEXT_LENGTH - len(HEADER)
        for entry in topic_entries:
            line = f"- {entry['text']}"
            if len(line) > remaining:
                continue
            lines.append(line)
            used_keys.append(entry["key"])
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
    """推送节制：每群一个每日总上限 + 一份睡觉时段（都从 group_push 读）。

    - 豁免清单不动：error / command / admin / awaited_delivery 永远放行，也不占额度。
    - 受限的推送共用一个每日上限（group_push `daily_max`，首次 seed 时沿用原
      `delivery.push_per_day` 默认 3，不加大）；三种自制消息（topic / news_card /
      idea_mention）也在同一个口径里，不再各自开小灶。
    - 结果不明（sending / uncertain）的发送**安全保留**一份额度：可能已经发出去了，
      不能当成没发过再放行别的推送（`count_used`，Outbox.flush 和网页视图共用）。
    - 失败的发送不留痕、不占额度：只有真发出去（或结果不明）才占。
    """

    def __init__(self, store: Store, get_settings: Callable[[], Settings]) -> None:
        self._store = store
        self._get_settings = get_settings

    def _settings(self) -> Any:
        """读配置；拿不到（抛异常 / 返回 None）→ None（调用方按失败关闭处理）。"""
        try:
            return self._get_settings()
        except Exception:
            logger.debug("读配置失败（推送节制按保守默认）", exc_info=True)
            return None

    def _config(self, group_id: str) -> tuple[dict, bool]:
        """这个群的推送设置 + 「读到了可信的一份」标志。

        失败关闭：生产路径拿不到配置 → 零读零写 + 保守默认（开关全关、额度有限、
        默认睡觉时段）；**不**落进 group_push 的 legacy `settings=None` 放行口径，
        也就不会拿旧全局种子把管理员关掉的开关重新 seed 成开。
        """
        settings = self._settings()
        if settings is None:
            return group_push.conservative_defaults(), False
        try:
            return group_push.get_config(self._store, group_id, settings), True
        except Exception:
            logger.exception("读每群推送设置失败（群 %s），按保守默认节制", group_id)
            return group_push.conservative_defaults(settings), False

    def can_push(self, group_id: str, kind: str, now: float) -> tuple[bool, str]:
        """决定能不能推。故障、指令回执、管理员当场让发的、明确领取的成品即时发送。

        now 决定「用哪一天的额度」和「是不是在睡觉时段」。三种自制消息的每群开关
        也在这里管：关了就返回「开关已关」，调用方据此把这条待发的作废（不发陈旧的）。

        非服务群（或认不出在不在服务名单）：直接 `(False, UNSERVED_REASON)`，**零 SQL**
        ——不读每群设置、不数额度、不看睡觉时段。只有明确的服务群才继续往下查。
        """
        kind_s = str(kind or "")
        if kind_s in PUSH_EXEMPT_KINDS:
            return True, ""
        gid = str(group_id)
        settings = self._settings()
        if settings is None:
            return False, UNREADABLE_REASON
        if not group_push.served(settings, gid):
            return False, UNSERVED_REASON
        try:
            cfg = group_push.get_config(self._store, gid, settings)
        except Exception:
            logger.exception("读每群推送设置失败（群 %s），按保守默认节制", gid)
            return False, UNREADABLE_REASON
        if not group_push.kind_enabled(cfg, kind_s):
            return False, "开关已关"
        if self.in_quiet(now, gid):
            return False, "睡觉时段"
        limit = int(cfg.get("daily_max") or 0)
        if limit > 0 and self.count_used(gid, float(now)) >= limit:
            return False, "今天推够了"
        return True, ""

    def _quiet_range(self, group_id: str = "") -> tuple[int, int]:
        if group_id:
            cfg, readable = self._config(str(group_id))
            quiet = str(cfg.get("quiet_hours") or "") if readable else ""
        else:
            settings = self._settings()
            quiet = str(getattr(getattr(settings, "delivery", None), "quiet_hours", "") or "23:00-08:00")
        try:
            return clock.parse_hhmm_range(quiet)
        except (ValueError, AttributeError):
            return 0, 0  # 配错按不限制

    def in_quiet(self, now: float, group_id: str = "") -> bool:
        """now 在不在这个群的睡觉时段；不给群号就用 settings 里那份（老调用口）。"""
        s, e = self._quiet_range(str(group_id or ""))
        return s != e and clock.in_range(float(now), (s, e))

    def record(self, group_id: str, kind: str, text: str, now: float) -> None:
        """任何 kind 都留发送记录；豁免种类不占每日额度。"""
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO pushes (group_id, ts, day, kind, text) VALUES (?, ?, ?, ?, ?)",
                (str(group_id), float(now), clock.day_key(float(now)), str(kind or ""), str(text or "")[:500]),
            )

    def _count_for_day(self, group_id: str, ts: float, kind: Optional[str]) -> int:
        """ts 所在北京那一天推过几条**原始留痕**（不含结果不明的保留）。kind=None 只算受限的。

        额度判定请用 count_used（它把结果不明的安全保留也算进去）；这个口是给统计 / 老调用
        （网页、审计测试）看留痕用的。
        """
        day = clock.day_key(float(ts))
        if kind is None:
            exempt = tuple(sorted(PUSH_EXEMPT_KINDS))
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

    def count_used(self, group_id: str, now: float) -> int:
        """今天占掉的额度：已发出的受限推送 + 结果不明的安全保留（group_push.used_today）。"""
        return group_push.used_today(
            self._store, str(group_id), now=float(now), exempt_kinds=PUSH_EXEMPT_KINDS
        )

    def count_today(self, group_id: str, kind: Optional[str] = None) -> int:
        """今天（按北京时间）推过几条原始记录。kind=None 算所有推送总数（含豁免的记录）。"""
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
