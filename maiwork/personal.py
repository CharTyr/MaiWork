"""personal.py（关注成员的个人向产出，docs/02-设计.md §3.2、§4.1 —「关注成员可以得到
个人向的资讯和构想，交付方式同样受 §6 约束，在群里提起时不暴露画像细节」）。

- prepare_personal(group_id, user_id)：只对**当前关注成员**、[focus] personal_profile
  为真、且 persona 已存在（focus_members.persona 非空 JSON）的人做。
  - 每人每天（北京时间）最多 1 次；每次最多 [focus] personal_per_day（默认 3）条。
  - 用他的 persona（doing/cares/asked）定 1–2 个关注点，复用 feeds 的搜索子 agent
    与质量门槛（第一道硬性淘汰 + 第二道打分）；相关度改成「和这个人在做的/关心的事
    相关」，必须能指出对应 persona 条目（指不出 → relevance 封顶 2）。
  - 写法是「写给他本人」的第二人称（「你在弄 X，这个可能用得上……」）；reason 可以说
    「你前几天在群里提到……」，但**只能引用他本人在群里公开说过的话**
    （chatlog.search_chat 只取他本人 user_id 的发言）。
  - 个人向资讯入 news_items.target_user_id（非空）、个人向构想入 ideas.target_user_id。
    **个人向条目绝不出现在群资讯视图、群友视图、话题候选池、TopicMatcher 候选里**
    （那些查询统一按「target_user_id 为空」过滤，见 feeds.py / delivery.py）。
- mention_to_member：管理员在网页点「在群里提给他」→ 往本群可提起清单
  （delivery.Mentions.add）加一句（ttl 6 小时）：「@<名字> 可能会对这个感兴趣：<标题>（<链接>）。
  提的时候别说 MaiWork 怎么知道他关心这个。」——**不含画像细节**、过 privacy.scrub、
  **不直接发群消息**。返回 {"ok": True}。
- focus_personal_view：GroupView.focus[].personal 的数据源（只给管理员）。

调度辅助：due（每群每轮最多 1 个到期的人）、mark_done、in_personal_window（北京时间
9:00–22:00，含 9:00 不含 22:00）。模型没配好 / 搜索没配（SearchUnavailable）→ 返回 0，不抛。

红线（AGENTS.md、docs/02 §3.2）：个人向内容只给本人 + 管理员看，绝不出现在群消息、
群画像、任何发给群友的文字里；不跨群；不跨人（focus_personal_view 按 target_user_id 分人）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import clock, members
from .config import Settings
from .feeds import _adopt_title_zh, _localize_title, _titles_with_originals, clean_step, focus_items
from .models import ModelError
from .search import SearchUnavailable
from .store import Store

logger = logging.getLogger("maiwork.personal")

# 调度时间窗：北京时间 9:00–22:00（含 9:00，不含 22:00）
_WINDOW_START_H = 9
_WINDOW_END_H = 22
# focus_personal_view 的条数上限
_VIEW_NEWS_CAP = 5
_VIEW_IDEAS_CAP = 2
# 一轮个人向备料子 agent 最多交回几条
_COLLECT_CAP = 6
# 写帖子每条最多带几条他自己的原话
_SEARCH_QUOTES = 6
# 入网门槛（第二道）：相关度 ≥3 且平均分 ≥3
_WEB_MIN_AVG = 3.0


def in_personal_window(ts: float) -> bool:
    """这个时间戳（北京时间）是不是在 9:00–22:00 之间（含 9:00、不含 22:00）。"""
    hour = clock.bj(float(ts)).hour
    return _WINDOW_START_H <= hour < _WINDOW_END_H


class Personal:
    """关注成员的个人向资讯 + 构想。构造：store, models, workers, profiles, topics, get_settings。"""

    def __init__(
        self,
        store: Store,
        models: Any,
        workers: Any,
        profiles: Any,
        topics: Any,
        get_settings: Callable[[], Settings],
        *,
        search: Any = None,
        host: Any = None,
        identity: Any = None,  # identity.py（AGENTS 规矩注入定关注点；None 自动跳过）
    ) -> None:
        self._store = store
        self._models = models
        self._workers = workers
        self._profiles = profiles
        self._topics = topics
        self._get_settings = get_settings
        self._search = search
        self._host = host
        self._identity = identity

    # ------------------------------------------------------------------
    # 调度（app 后台循环）：到期的人（每群每轮最多 1 个）、记做过
    # ------------------------------------------------------------------

    def due(self, group_id: str, now: float) -> list[str]:
        """本轮该给谁跑个人向产出（最多 1 个 user_id；没有 → []）。

        挑人条件：personal_feeds 开、personal_profile 开、在 9:00–22:00 时间窗里、
        是当前关注成员（removed=0）、persona 已存在、今天（北京时间）还没跑过。
        """
        gid = str(group_id)
        try:
            settings = self._get_settings()
        except Exception:
            return []
        focus = getattr(settings, "focus", None)
        if focus is None:
            return []
        if not bool(getattr(focus, "personal_feeds", True)):
            return []
        if not bool(getattr(focus, "personal_profile", True)):
            return []
        if not in_personal_window(float(now)):
            return []
        day = clock.day_key(float(now))
        rows = self._store.read().execute(
            "SELECT user_id, persona FROM focus_members WHERE group_id=? AND removed=0 ORDER BY user_id",
            (gid,),
        ).fetchall()
        for r in rows:
            uid = str(r["user_id"] or "")
            if not uid:
                continue
            if not str(r["persona"] or "").strip():
                continue  # 画像还没建出来
            if self._done_today(gid, uid, day):
                continue  # 今天已跑过
            return [uid]  # 每群每轮最多 1 个
        return []

    def mark_done(self, group_id: str, user_id: str, now: float) -> None:
        """记下「这个人今天（北京时间）跑过了」。"""
        gid = str(group_id)
        uid = str(user_id)
        with self._store.tx() as conn:
            self._store.kv_set(conn, self._done_key(gid, uid), clock.day_key(float(now)))

    @staticmethod
    def _done_key(gid: str, uid: str) -> str:
        return f"personal.done.{gid}.{uid}"

    def _done_today(self, gid: str, uid: str, day: str) -> bool:
        try:
            return str(self._store.kv_get(self._done_key(gid, uid), "") or "") == day
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 个人向资讯 + 构想
    # ------------------------------------------------------------------

    async def prepare_personal(self, group_id: str, user_id: str) -> int:
        """给一位关注成员出一轮个人向资讯（+0–1 条构想）。返回入选条数。任何门不过 → 0，不抛。"""
        gid = str(group_id)
        uid = str(user_id).strip()
        settings = self._get_settings()
        focus = getattr(settings, "focus", None)
        if not uid or focus is None:
            return 0
        if not bool(getattr(focus, "personal_profile", True)):
            return 0
        try:
            if not self._models.settings().ready():
                return 0
        except Exception:
            return 0
        member = self._member(gid, uid)
        if member is None:
            return 0
        persona = member["persona"]
        name = member["name"]
        now = clock.now()
        if self._done_today(gid, uid, clock.day_key(now)):
            return 0
        try:
            await self._ensure_search()
        except SearchUnavailable as e:
            logger.info("个人向备料-搜索没配（群 %s 人 %s）：%s", gid, uid[:8], e)
            return 0

        # ① 用他的 persona 定 1–2 个关注点（顺带 0–1 条构想）
        try:
            plan = await self._plan_focus(gid, name, persona)
        except (ModelError, ValueError) as e:
            logger.info("个人向备料-定关注点失败（群 %s 人 %s）：%s", gid, uid[:8], e)
            return 0
        focus_pts = plan["focus"]
        idea = plan["idea"]

        # ② 子 agent 找候选
        try:
            candidates = await self._collect(gid, focus_pts)
        except (ModelError, ValueError) as e:
            logger.info("个人向备料-子 agent 失败（群 %s 人 %s）：%s", gid, uid[:8], e)
            return 0
        except Exception as e:
            logger.exception("个人向备料-子 agent 意外（群 %s）", gid)
            return 0

        # ③ 第一道（代码侧）+ 打分（相关度对着 persona 条目）+ 第一道（模型侧）
        survivors = self._hard_reject_code(gid, candidates)
        if survivors:
            try:
                await self._score(gid, name, persona, survivors)
            except (ModelError, ValueError) as e:
                logger.info("个人向备料-打分失败（群 %s 人 %s）：%s", gid, uid[:8], e)
                return 0
            self._hard_reject_model(survivors)

        # ④ 第二道（relevance≥3 且 avg≥[feeds] web_min_avg，和群向资讯同一道闸）
        web_min_avg = float(getattr(settings.feeds, "web_min_avg", _WEB_MIN_AVG))
        for item in survivors:
            if "reject" in item:
                continue
            sc = item["scores"]
            if sc["relevance"] < 3.0:
                item["reject"] = ("web", f"相关度 {sc['relevance']:.1f} < 3.0，过不了这道")
            elif sc["avg"] < web_min_avg:
                item["reject"] = ("web", f"平均分 {sc['avg']:.1f} < {web_min_avg:.1f}，过不了这道")

        # ⑥ 每次最多 personal_per_day 条（2026-10，C01：从写帖之后挪到写帖之前——
        # 确定会被截掉的条目不再花写帖的钱；被截的照落库、带「超出这次上限」的原因）
        per_day = max(1, int(getattr(focus, "personal_per_day", 3)))
        kept = self._keep_top(survivors, per_day)

        # ⑤ 写帖子（写给他本人；原话只取他自己的）——只写最终留下的这几条
        posting = [item for item in survivors if "reject" not in item]
        if posting:
            try:
                await self._write_posts(gid, uid, name, persona, posting)
            except Exception:
                logger.exception("个人向写帖子意外出错（群 %s），全部回落原文", gid)
                for item in posting:
                    self._post_fallback(item)

        # ⑦ 落库（含被筛的）+ 标今天做过
        kept_n = self._insert_items(gid, uid, now, candidates)
        self.mark_done(gid, uid, now)

        # ⑧ 顺带 0–1 条个人向构想
        if idea is not None:
            try:
                self._insert_idea(gid, uid, idea, persona)
            except Exception:
                logger.exception("个人向构想入库失败（群 %s 人 %s）", gid, uid[:8])
        return kept_n

    # ------------------------------------------------------------------
    # 定关注点（+顺带构想）
    # ------------------------------------------------------------------

    def _persona_lines(self, persona: dict) -> list[str]:
        lines: list[str] = []
        if persona.get("summary"):
            lines.append(f"一句话：{persona['summary']}")
        if persona.get("doing"):
            lines.append("在做的事：")
            lines.extend(f"- {x}" for x in persona["doing"][:5])
        if persona.get("cares"):
            lines.append("关心的话题：")
            lines.extend(f"- {x}" for x in persona["cares"][:5])
        if persona.get("asked"):
            lines.append("提过的需求：")
            lines.extend(f"- {x}" for x in persona["asked"][:5])
        return lines

    async def _plan_focus(self, gid: str, name: str, persona: dict) -> dict:
        """用他的 persona 定 1–2 个关注点 + 0–1 条「我可以帮你……」构想。

        AGENTS.md（做事规矩，含管理员写的搜索要求）在定关注点时就生效。
        """
        agents_block = self._prompt_block_safe("agents")
        lines = ([agents_block] if agents_block else [])
        lines += [f"要给 {name}（就这个人，不是整个群）挑他个人会感兴趣的资讯。他最近的画像："]
        p_lines = self._persona_lines(persona)
        if p_lines:
            lines.extend(p_lines)
        else:
            lines.append("（画像还少，按他最近在群里做的事挑）")
        lines.append("")
        lines.append(
            '请给出 1–2 个拿去搜索的关注点（具体一点，对着他做的/关心的），'
            '顺带想 0–1 个「我可以帮他……」的小忙（想不出就 idea 给 null）。只回 JSON：'
            '{"focus": [{"query": "搜索关键词", "why": "对着他哪件事"}],'
            ' "idea": {"title": "我可以帮你……", "body": "帮什么（一两句）", "step": "第一步", '
            '"effort": "大概多久"} | null}'
        )
        result = await self._models.chat(
            agent="main",
            messages=[{"role": "user", "content": "\n".join(lines)}],
            json_mode=True,
            purpose="personal.focus",
            group_id=gid,
        )
        data = json.loads(result.text)
        out_focus: list[dict] = focus_items(data)[:2]
        if not out_focus:
            raise ValueError("模型没给出能用的关注点（返回格式不对或是空的）")
        idea = data.get("idea") if isinstance(data, dict) else None
        out_idea: dict | None = None
        if isinstance(idea, dict) and str(idea.get("title") or "").strip():
            out_idea = {
                "title": str(idea.get("title") or "").strip()[:60],
                "body": str(idea.get("body") or "").strip()[:200],
                "step": str(idea.get("step") or "").strip()[:120],
                "effort": str(idea.get("effort") or "").strip()[:60],
            }
        return {"focus": out_focus, "idea": out_idea}

    # ------------------------------------------------------------------
    # 子 agent 找候选
    # ------------------------------------------------------------------

    async def _collect(self, gid: str, focus_list: list[dict]) -> list[dict]:
        # 时间盒：和群资讯同一个 [feeds] collect_minutes（2026-09-29 线上一轮跑了一个多小时）
        settings = self._get_settings()
        collect_minutes = max(1, int(getattr(settings.feeds, "collect_minutes", 15) or 15))
        lines = []
        for f in focus_list:
            lines.append(f"- {f['query']}" + (f"（原因：{f['why']}）" if f.get("why") else ""))
        brief = (
            "给一位群成员挑他个人会感兴趣的「资讯」（最近几天的新闻/发布/动态）。关注点：\n"
            + "\n".join(lines)
            + "\n\n要求：\n"
            "1. 用 web_search 搜最近几天（days 填 3–7）；\n"
            "2. 每条候选必须用 fetch_page 真打开过原文再看一遍，确实和关注点相关、有信息量才收；\n"
            "3. 凑数的、旧的、广告软文、营销号都不要；要登录或付费才能看的也别收，标 paywall: true；\n"
            f"4. 最多交回 {_COLLECT_CAP} 条，宁缺毋滥；\n"
            "5. 每条：title、url、summary（2–4 句中文纯文本，别用 Markdown）、kind（一律 news）、"
            "published（ISO 或 epoch，拿不到就空字符串）、fetched（真打开过就 true）、"
            "quote（从原文抄一小段能支撑摘要的依据，≤200 字）、paywall；\n"
            "6. 最后用 submit_result 交回，data 按约定的 JSON Schema；"
            f"你只有大约 {collect_minutes} 分钟，到点前记得把已经找到的交回来（部分结果也算，不会丢）。"
        )
        report = await self._workers.run(
            brief,
            group_id=gid,
            tools=["web_search", "fetch_page"],
            deadline_ts=clock.now() + collect_minutes * 60,
            output_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "summary": {"type": "string"},
                                "kind": {"type": "string"},
                                "published": {"type": "string"},
                                "fetched": {"type": "boolean"},
                                "quote": {"type": "string"},
                                "paywall": {"type": "boolean"},
                            },
                            "required": ["title", "url", "summary", "kind", "fetched", "quote", "paywall"],
                        },
                    }
                },
                "required": ["items"],
            },
        )
        if not getattr(report, "ok", False):
            raise ValueError(str(getattr(report, "error", "") or getattr(report, "summary", "") or "子 agent 没干成"))
        data = report.data
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("子 agent 交回的格式不对")
        from .feeds import _normalize_url, _parse_published, _public_http_url, _QUOTE_MAX

        items = []
        for raw in data["items"][:_COLLECT_CAP]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            url = _public_http_url(raw.get("url"))
            summary = str(raw.get("summary") or "").strip()
            if not title or not url or not summary:
                continue
            items.append(
                {
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "kind": "news",
                    "published_raw": raw.get("published"),
                    "published_ts": _parse_published(raw.get("published")),
                    "fetched": bool(raw.get("fetched")),
                    "quote": str(raw.get("quote") or "").replace("\n", " ").strip()[:_QUOTE_MAX],
                    "paywall": bool(raw.get("paywall")),
                    "url_key": _normalize_url(url),
                }
            )
        return items

    # ------------------------------------------------------------------
    # 第一道（代码侧）：和「给他本人的最近」对重
    # ------------------------------------------------------------------

    def _hard_reject_code(self, gid: str, candidates: list[dict]) -> list[dict]:
        """不调模型的第一道硬性淘汰；返回幸存列表（原序）。"""
        from .feeds import _similar, _site_of, _TITLE_DEDUP_RATIO, _domain_blocked, blocked_domains_effective

        settings = self._get_settings()
        blocked = set(blocked_domains_effective(
            self._store, tuple(getattr(settings.feeds, "blocked_domains", ()) or ())
        ))
        # 对重窗口看「本群个人向 + 群向」最近 lookback_days 天（个人向别和群向出题撞，也别和自己撞）
        since = clock.now() - max(1, int(getattr(settings.feeds, "lookback_days", 14))) * 86400.0
        rows = self._store.read().execute(
            "SELECT url_key, title, sources FROM news_items WHERE group_id=? AND created>=? AND rejected=0",
            (gid, since),
        ).fetchall()
        seen_urls = {str(r["url_key"]) for r in rows if r["url_key"]}
        seen_titles = _titles_with_originals(rows)
        survivors: list[dict] = []
        for item in candidates:
            url_key = item["url_key"]
            site = _site_of(item["url"])
            item["site"] = site
            if not item.get("fetched") or not str(item.get("quote") or "").strip():
                item["reject"] = ("hard", "原文没打开过/打不开")
            elif item.get("paywall"):
                item["reject"] = ("hard", "原文要登录/付费")
            elif site and _domain_blocked(site, blocked):
                item["reject"] = ("hard", "来源在屏蔽名单里")
            elif url_key and url_key in seen_urls:
                item["reject"] = ("hard", "和最近出过的重复（同一个链接）")
            elif any(_similar(item["title"], t) >= _TITLE_DEDUP_RATIO for t in seen_titles):
                item["reject"] = ("hard", "和最近出过的重复（标题高度相似）")
            if "reject" not in item:
                survivors.append(item)
                if url_key:
                    seen_urls.add(url_key)
                seen_titles.append(item["title"])
        return survivors

    def _hard_reject_model(self, survivors: list[dict]) -> None:
        for item in survivors:
            if "reject" in item:
                continue
            flags = item.get("_flags") or {}
            if not flags.get("grounded", True):
                item["reject"] = ("hard", "摘要在原文找不到依据（不扎实）")
            elif flags.get("junk"):
                reason = str(flags.get("junk_reason") or "").strip()
                item["reject"] = ("hard", f"垃圾：{reason}" if reason else "垃圾：标题党/软文/营销号/纯情绪")
            elif flags.get("same_as_recent"):
                item["reject"] = ("hard", "和最近给过的是同一件事（重复）")

    def _keep_top(self, survivors: list[dict], per_day: int) -> list[dict]:
        """每次最多 per_day 条：avg 高者留，其余标 reject。返回留下的。"""
        ok = [it for it in survivors if "reject" not in it and "scores" in it]
        ordered = sorted(ok, key=lambda x: -float(x["scores"]["avg"]))
        for it in ok[per_day:]:
            it["reject"] = ("web", f"超出这次上限（每人每次最多 {per_day} 条）")
        return ordered[:per_day]

    # ------------------------------------------------------------------
    # 打分（相关度对着 persona 条目）
    # ------------------------------------------------------------------

    async def _score(self, gid: str, name: str, persona: dict, candidates: list[dict]) -> None:
        """打五项分 + 第一道模型侧判断；相关度必须对着 persona 条目（指不出 → 封顶 2）。"""
        from .feeds import _ICONS

        # persona 条目（编号 = 模型要指的）：doing / cares / asked
        entry_texts: list[str] = []
        for k in ("doing", "cares", "asked"):
            for x in persona.get(k, [])[:5]:
                entry_texts.append(str(x))
        entry_texts = entry_texts[:20]
        lines = [f"这是 {name}（一个人，不是群）在做/关心/提过的事，相关度对着它们打："]
        if entry_texts:
            for i, t in enumerate(entry_texts):
                lines.append(f"[{i}] {t}")
        else:
            lines.append("（还少，按一句话简介：%s；profile 给 null）" % (persona.get("summary") or "泛科技兴趣"))
        lines.append("")
        if persona.get("summary"):
            lines.append(f"他的简介：{persona['summary']}")
            lines.append("")
        recent = self._recent_personal_titles(gid)
        if recent:
            lines.append("最近给过他的（判「同一件事」用）：")
            lines.extend(f"- {t}" for t in recent[:15])
            lines.append("")
        lines.append("下面是一批候选（编号从 0 开始；quote 是子 agent 从原文抄的依据）：")
        for i, c in enumerate(candidates):
            quote = str(c.get("quote") or "")
            pub = c.get("published_raw")
            pub_text = f"，发布于 {pub}" if pub else ""
            lines.append(
                f"[{i}] {c['title']} —— {c['summary'][:150]}（{c['url']}{pub_text}）"
                + (f" 原文依据：{quote[:150]}" if quote else "")
            )
        lines.append("")
        icon_list = "、".join(_ICONS)
        lines.append(
            "请给每条打分，只回 JSON："
            '{"scores": [{"i": 编号,'
            ' "info": 信息量 1到5,'
            ' "source": 来源等级 1到5（官方/一手 > 权威媒体 > 个人博客 > 二手转述）,'
            ' "relevance": 和「这个人」做的/关心的事的相关度 1到5（必须对着上面某一条打）,'
            ' "timeliness": 新鲜度 1到5,'
            ' "chat": 他会不会觉得有用 1到5,'
            ' "profile": 相关度对应的是上面哪一条（回它的编号；没有就说 null）,'
            ' "topic": 话题标签（不超过 8 个字）,'
            ' "sensitive": 政治/争议话题吗 true/false,'
            ' "grounded": 摘要能在 quote/原文里找到依据吗 true/false,'
            ' "junk": 标题党/软文广告/营销号/纯情绪没事实吗 true/false,'
            ' "junk_reason": junk 是 true 的话写一个简短原因，否则空字符串,'
            ' "same_as_recent": 是不是和「最近给过他的」某条讲的是同一件事 true/false,'
            ' "why": "为什么给他（一句话，对着他哪件事）",'
            f' "icon": "从下面这些挑一个：{icon_list}"}}]}}'
        )
        result = await self._models.chat(
            agent="main",
            messages=[{"role": "user", "content": "\n".join(lines)}],
            json_mode=True,
            purpose="personal.score",
            group_id=gid,
        )
        data = json.loads(result.text)
        scores_raw = data.get("scores") if isinstance(data, dict) else None

        def _f15(key: str, s: dict) -> float:
            try:
                v = float(s.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0
            return max(0.0, min(5.0, v))

        def _norm(s: dict) -> dict:
            icon = str(s.get("icon") or "").strip()
            if icon not in _ICONS:
                icon = "newspaper"
            flags = {
                "grounded": bool(s.get("grounded", True)),
                "junk": bool(s.get("junk", False)),
                "junk_reason": str(s.get("junk_reason") or "")[:80],
                "same_as_recent": bool(s.get("same_as_recent", False)),
            }
            relevance = _f15("relevance", s)
            raw_profile = s.get("profile")
            try:
                pidx = int(raw_profile) if raw_profile is not None else None
            except (TypeError, ValueError):
                pidx = None
            profile_ref = ""
            if pidx is not None and 0 <= pidx < len(entry_texts) and entry_texts[pidx]:
                profile_ref = entry_texts[pidx]
            else:
                relevance = min(relevance, 2.0)  # 指不出对应条目 → relevance 封顶 2
            topic = str(s.get("topic") or "").strip().replace("\n", " ")[:8]
            five = {
                "info": _f15("info", s),
                "source": _f15("source", s),
                "relevance": relevance,
                "timeliness": _f15("timeliness", s),
                "chat": _f15("chat", s),
            }
            five["avg"] = round(sum(five.values()) / 5.0, 3)
            return {
                "scores": five,
                "why": str(s.get("why") or "").strip()[:200],
                "icon": icon,
                "topic": topic,
                "sensitive": bool(s.get("sensitive", False)),
                "profile_ref": profile_ref,
                "flags": flags,
            }

        by_index: dict[int, dict] = {}
        by_title: dict[str, dict] = {}
        if isinstance(scores_raw, list):
            for s in scores_raw:
                if not isinstance(s, dict):
                    continue
                normed = _norm(s)
                t = str(s.get("title") or "").strip()
                if t and t not in by_title:
                    by_title[t] = normed
                try:
                    i = int(s.get("i"))
                except (TypeError, ValueError):
                    continue
                if i in by_index:
                    continue
                by_index[i] = normed
        zero = {"info": 0.0, "source": 0.0, "relevance": 0.0, "timeliness": 0.0, "chat": 0.0, "avg": 0.0}
        for pos, item in enumerate(candidates):
            s = by_index.get(pos)
            if not s:
                s = by_title.get(item["title"])
            if not s:
                item["scores"] = dict(zero)
                item.setdefault("why", "")
                item.setdefault("icon", "newspaper")
                item.setdefault("topic", "")
                item.setdefault("profile_ref", "")
                item.setdefault("_flags", {"grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False})
                continue
            item["scores"] = s["scores"]
            item["why"] = s["why"]
            item["icon"] = s["icon"]
            item["topic"] = s["topic"]
            item["sensitive"] = s["sensitive"]
            item["profile_ref"] = s["profile_ref"]
            item["_flags"] = s["flags"]

    def _recent_personal_titles(self, gid: str) -> list[str]:
        since = clock.now() - 14 * 86400.0
        rows = self._store.read().execute(
            "SELECT title FROM news_items WHERE group_id=? AND target_user_id<>''"
            " AND created>=? ORDER BY created DESC LIMIT 15",
            (gid, since),
        ).fetchall()
        return [str(r["title"]) for r in rows if r["title"]]

    # ------------------------------------------------------------------
    # 写帖子（写给他本人；原话只取他自己的）
    # ------------------------------------------------------------------

    async def _write_posts(self, gid: str, uid: str, name: str, persona: dict, items: list[dict]) -> None:
        from .chatlog import search_chat

        per_item: list[dict] = []
        for item in items:
            quotes: list[dict] = []
            seen: set[str] = set()
            for q in (str(item.get("title") or ""), str(item.get("topic") or "")):
                q = q.strip()
                if not q:
                    continue
                for hit in search_chat(self._store, gid, q, days=14, limit=_SEARCH_QUOTES, uid=uid):
                    mid = str(hit.get("message_id") or "")
                    if mid and mid not in seen:
                        seen.add(mid)
                        quotes.append(hit)
                        if len(quotes) >= _SEARCH_QUOTES:
                            break
                if len(quotes) >= _SEARCH_QUOTES:
                    break
            per_item.append({"item": item, "quotes": quotes})

        lines = [f"下面这轮内容只给 {name} 一个人看。写正文要用「写给他本人」的第二人称，"
                 "像当面跟他说，别用第三人称称呼他；reason 想说他自己提过的事，只能用给出的他本人的原话。"]
        p_lines = self._persona_lines(persona)
        if p_lines:
            lines.append("")
            lines.append("他最近的情况（写「为什么给他」时对得上就提一句）：")
            lines.extend(p_lines)
        lines.append("")
        for i, pack in enumerate(per_item):
            item = pack["item"]
            lines.append(
                f"[{i}] {item['title']} —— {str(item['summary'] or '')[:150]}"
                f"（来源 {str(item.get('site') or '')}，{item['url']}）"
            )
            quote = str(item.get("quote") or "")[:150]
            if quote:
                lines.append(f"    原文依据：{quote}")
            if pack["quotes"]:
                lines.append("    他本人在群里说过的相关原话（编号从 1 开始，reason 要引用只能用这些；"
                             "可以说「你前几天在群里提到……」并引用编号）：")
                for j, hit in enumerate(pack["quotes"], 1):
                    stamp = clock.bj(float(hit["ts"])).strftime("%m-%d %H:%M")
                    lines.append(f"      ({j}) {stamp} {hit['who']}: {str(hit['text'])[:80]}")
            else:
                lines.append("    （没搜到他自己的相关原话：reason 就事论事，不编他说过的，refs 给空）")
            lines.append("")
        lines.append(
            "给每条写帖子，只回 JSON："
            '{"posts": [{"i": 编号, "title": "对应条目标题（原样照抄）",'
            ' "title_zh": "原标题不是中文时译成简洁自然的中文标题（专有名词、产品名可保留原文），'
            '不许加原文没有的信息；原标题已是中文就原样照抄",'
            ' "body": "写给他的正文 2–4 句，第二人称，像「你在弄 X，这个可能用得上……」；'
            ' 关键处可用 [文字](https://链接) 嵌原文链接，只许 http(s),'
            ' "reason": "为什么给他；说他提过的事只能用上面引用列表里他本人的原话，别的不编,'
            ' "refs": [引用到的他本人原话编号（只能用上面给的）],'
            ' "audience": [],'
            ' "keywords": ["3–6 个关键词"]}]}'
        )
        try:
            result = await self._models.chat(
                agent="main",
                messages=[{"role": "user", "content": "\n".join(lines)}],
                json_mode=True,
                purpose="personal.post",
                group_id=gid,
            )
            data = json.loads(result.text)
        except (ModelError, ValueError) as e:
            logger.info("个人向写帖子失败（群 %s）：%s；全部回落原文", gid, e)
            for item in items:
                self._post_fallback(item)
            return
        posts = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(posts, list):
            posts = []
        by_index: dict[int, dict] = {}
        by_title: dict[str, dict] = {}
        for p in posts:
            if not isinstance(p, dict):
                continue
            try:
                idx = int(p.get("i"))
            except (TypeError, ValueError):
                idx = None
            if idx is not None and idx not in by_index:
                by_index[idx] = p
            t = str(p.get("title") or "").strip()
            if t and t not in by_title:
                by_title[t] = p
        for i, pack in enumerate(per_item):
            item = pack["item"]
            raw = by_title.get(item["title"]) or by_index.get(i)
            if raw is None:
                self._post_fallback(item)
                continue
            item["post"] = self._clean_post(raw, pack["quotes"], item)
            _adopt_title_zh(item, raw)

    def _post_fallback(self, item: dict) -> None:
        keywords: list[str] = []
        for k in [str(item.get("topic") or ""), str(item.get("title") or "")]:
            k = k.strip()
            if k and k not in keywords:
                keywords.append(k)
        item["post"] = {
            "body": str(item.get("summary") or ""),
            "reason": str(item.get("why") or ""),
            "refs": [],
            "audience": [],
            "keywords": keywords[:6],
        }

    def _clean_post(self, raw: dict, quotes: list[dict], item: dict) -> dict:
        """refs 只保留「他本人」的原话（quotes 已经是按 uid 过滤的）；链接只留 http(s)≤4。"""
        from .feeds import _clean_body_links, _BODY_LINK_MAX, _REF_TEXT_MAX, _REASON_MAX, _KEYWORDS_MAX

        body = str(raw.get("body") or "").strip() or str(item.get("summary") or "")
        reason = str(raw.get("reason") or "").strip() or str(item.get("why") or "")
        body = _clean_body_links(body, _BODY_LINK_MAX)
        refs_out: list[dict] = []
        raw_refs = raw.get("refs")
        if isinstance(raw_refs, list):
            for x in raw_refs:
                try:
                    idx = int(x)
                except (TypeError, ValueError):
                    continue
                if not (1 <= idx <= len(quotes)):
                    continue
                hit = quotes[idx - 1]
                entry = {
                    "ts": float(hit["ts"]),
                    "who": str(hit["who"]),
                    # 认人靠平台 id：显示时按它查当前名（这里引用的都是他本人的原话）
                    "user_id": str(hit.get("user_id") or ""),
                    "text": str(hit["text"])[:_REF_TEXT_MAX],
                    "message_id": str(hit["message_id"]),
                }
                if entry["message_id"] not in {r["message_id"] for r in refs_out}:
                    refs_out.append(entry)
        keywords: list[str] = []
        raw_kw = raw.get("keywords")
        if isinstance(raw_kw, list):
            for k in raw_kw:
                kw = str(k or "").strip()
                if kw and kw not in keywords:
                    keywords.append(kw)
                if len(keywords) >= _KEYWORDS_MAX:
                    break
        return {
            "body": body,
            "reason": reason[:_REASON_MAX],
            "refs": refs_out,
            "audience": [],
            "keywords": keywords,
        }

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _insert_items(self, gid: str, uid: str, now: float, candidates: list[dict]) -> int:
        """一个事务写批次 + 全部条目（target_user_id=uid）。返回入选条数。"""
        from .privacy import scrub

        accepted = [it for it in candidates if "reject" not in it]
        rejected = [it for it in candidates if it.get("reject")]
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, now, len(candidates), len(accepted), 1 if not accepted else 0,
                 "personal:" if accepted else "personal: 没有过门槛的", now),
            )
            batch_id = int(cur.lastrowid or 0)
            for item in accepted:
                _localize_title(item)
                sc = item.get("scores") or {}
                post = item.get("post") or {}
                body = scrub(gid, str(post.get("body") or ""), self._store) or ""
                reason = scrub(gid, str(post.get("reason") or ""), self._store) or ""
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                    " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                    " reject_gate, reject_reason, body, reason, refs, audience, image_url,"
                    " keywords, chat_votes, angle, verify, target_user_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', NULL, 0, NULL, 0, 0, ?,"
                    " 'news', ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?, '', ?, 0, '', ?, ?)",
                    (
                        batch_id, gid, str(item.get("icon") or "newspaper"), item["title"],
                        item["summary"], str(item.get("why") or ""),
                        json.dumps(
                            [{"url": item["url"], "site": str(item.get("site") or ""),
                              "title": item.get("title_orig") or item["title"]}],
                            ensure_ascii=False,
                        ),
                        item["url_key"], item.get("published_ts"),
                        float(sc.get("avg") or 0.0), now,
                        json.dumps(sc, ensure_ascii=False),
                        str(item.get("topic") or ""), 1 if item.get("sensitive") else 0,
                        str(item.get("profile_ref") or ""),
                        body, reason,
                        json.dumps(post.get("refs") or [], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                        json.dumps(post.get("keywords") or [], ensure_ascii=False),
                        json.dumps(post.get("verify"), ensure_ascii=False) if isinstance(post.get("verify"), dict) else "",
                        uid,
                    ),
                )
            for item in rejected:
                gate, reason = item.get("reject") or (None, None)
                sc = item.get("scores") or {}
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                    " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                    " reject_gate, reject_reason, angle, image_url, target_user_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', NULL, 0, NULL, 0, 0, ?,"
                    " 'news', ?, ?, ?, ?, 1, ?, ?, '', '', ?)",
                    (
                        batch_id, gid, str(item.get("icon") or "newspaper"), item["title"],
                        item["summary"], str(item.get("why") or ""),
                        json.dumps(
                            [{"url": item["url"], "site": str(item.get("site") or ""), "title": item["title"]}],
                            ensure_ascii=False,
                        ),
                        item["url_key"], item.get("published_ts"),
                        float(sc.get("avg") or 0.0), now,
                        json.dumps(sc, ensure_ascii=False) if sc else "",
                        str(item.get("topic") or ""), 1 if item.get("sensitive") else 0,
                        str(item.get("profile_ref") or ""),
                        str(gate) if gate else None, str(reason) if reason else None,
                        uid,
                    ),
                )
        return len(accepted)

    def _insert_idea(self, gid: str, uid: str, idea: dict, persona: dict) -> None:
        """个人向构想（0–1 条「我可以帮你……」）→ ideas.target_user_id；不进候选池。"""
        from .feeds import _similar, _IDEA_DEDUP_RATIO
        from .privacy import scrub

        title = str(idea.get("title") or "").strip()
        if not title:
            return
        since = clock.now() - 30 * 86400.0
        rows = self._store.read().execute(
            "SELECT title FROM ideas WHERE group_id=? AND target_user_id=? AND created>=?",
            (gid, uid, since),
        ).fetchall()
        if any(_similar(title, str(r["title"])) >= _IDEA_DEDUP_RATIO for r in rows):
            return
        body = scrub(gid, str(idea.get("body") or ""), self._store) or ""
        now = clock.now()
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
                " requested_by, task_id, up, down, created, updated, feasibility, keywords,"
                " target_user_id)"
                " VALUES (?, 'bulb', ?, ?, ?, ?, ?, 'new', NULL, NULL, 0, 0, ?, ?, '', '[]', ?)",
                (
                    gid, title, body,
                    str(persona.get("summary") or "")[:120],  # basis：只给管理员参考，不进任何群文字
                    str(idea.get("step") or "").strip(),
                    str(idea.get("effort") or "").strip(),
                    now, now, uid,
                ),
            )

    # ------------------------------------------------------------------
    # mention_to_member（管理员在网页点「在群里提给他」）
    # ------------------------------------------------------------------

    def mention_to_member(self, group_id: str, news_id: int, *, mentions: Any) -> dict:
        """往本群可提起清单加一句（ttl 6 小时）。不含画像细节、过 privacy.scrub、不发群消息。

        返回 {"ok": True}；找不到这条 / 不是个人向 → KeyError。
        """
        gid = str(group_id)
        nid = int(news_id)
        row = self._store.read().execute(
            "SELECT group_id, title, sources, target_user_id FROM news_items WHERE id=?",
            (nid,),
        ).fetchone()
        if row is None or str(row["group_id"]) != gid:
            raise KeyError(f"找不到这条资讯：#{nid}")
        uid = str(row["target_user_id"] or "").strip()
        if not uid:
            raise KeyError("这条不是个人向资讯")
        name_row = self._store.read().execute(
            "SELECT name FROM focus_members WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        name = str(name_row["name"] or "").strip() if name_row else ""
        if not name:
            name = uid
        url = ""
        try:
            src = json.loads(row["sources"] or "[]")
            if isinstance(src, list) and src and isinstance(src[0], dict):
                url = str(src[0].get("url") or "")
        except (ValueError, TypeError):
            url = ""
        link_part = f"（{url}）" if url else ""
        text = (
            f"@{name} 可能会对这个感兴趣：{str(row['title'] or '')}{link_part}。"
            "提的时候顺其自然就好，别说 MaiWork 怎么知道 ta 关心这个。"
        )[:280]
        # 文字过隐私闸：标题里万一带出画像片段（≥8 字连续）就整条拦下——只含画像细节的
        # 文本绝不允许进 MaiBot 的备忘。
        from .privacy import scrub

        cleaned = scrub(gid, text, self._store)
        if cleaned is None:
            logger.info("mention-to-member 文本含画像片段，已拦下（群 %s 条 %s）", gid, nid)
            return {"ok": False}
        mentions.add(gid, cleaned, key=f"personal-mention:{nid}", ttl_s=6 * 3600.0)
        return {"ok": True}

    # ------------------------------------------------------------------
    # focus_personal_view（GroupView.focus[].personal；只给管理员）
    # ------------------------------------------------------------------

    def focus_personal_view(self, group_id: str, user_id: str, *, days: int = 7) -> dict:
        """最近 days 天的个人向资讯（≤5，结构同 news item）+ 个人向构想（≤2）+ last_ts。"""
        gid = str(group_id)
        uid = str(user_id)
        now = clock.now()
        since = now - max(1, int(days)) * 86400.0
        news_rows = self._store.read().execute(
            "SELECT * FROM news_items WHERE group_id=? AND target_user_id=? AND rejected=0"
            " AND created>=? ORDER BY created DESC, id DESC LIMIT ?",
            (gid, uid, since, _VIEW_NEWS_CAP),
        ).fetchall()
        idea_rows = self._store.read().execute(
            "SELECT * FROM ideas WHERE group_id=? AND target_user_id=? AND created>=?"
            " ORDER BY created DESC, id DESC LIMIT ?",
            (gid, uid, since, _VIEW_IDEAS_CAP),
        ).fetchall()
        news = [self._news_personal_view(r) for r in news_rows]
        ideas = [self._idea_personal_view(r) for r in idea_rows]
        stamps: list[float] = []
        if news_rows:
            stamps.append(float(news_rows[0]["created"] or 0.0))
        if idea_rows:
            stamps.append(float(idea_rows[0]["created"] or 0.0))
        last_ts: float | None = max(stamps) if stamps else None
        return {"news": news, "ideas": ideas, "last_ts": last_ts}

    def _news_personal_view(self, r: Any) -> dict:
        """个人向资讯条目结构（参 §9.3 news item 的子集，带个人字段）。"""
        try:
            sources = json.loads(r["sources"] or "[]")
        except (ValueError, TypeError):
            sources = []
        sources = [s for s in sources if isinstance(s, dict)]
        try:
            scores = json.loads(r["scores"] or "")
            if not isinstance(scores, dict):
                scores = {}
        except (ValueError, TypeError):
            scores = {}
        scores_out = {
            k: float(scores[k]) if isinstance(scores.get(k), (int, float)) else 0.0
            for k in ("info", "source", "relevance", "timeliness", "chat", "avg")
        }
        published = r["published_ts"]
        try:
            refs = json.loads(r["refs"] or "[]")
        except (ValueError, TypeError):
            refs = []
        ref_items = [x for x in (refs if isinstance(refs, list) else []) if isinstance(x, dict)]
        # refs 的 who 换成名册当前名（按 user_id）；查不到回落快照；输出不带 user_id
        gid = str(r["group_id"] or "")
        names = members.names_of(
            self._store, gid, [str(x.get("user_id") or "") for x in ref_items]
        )
        refs_out = [
            {
                "ts": float(x.get("ts") or 0.0),
                "who": names.get(str(x.get("user_id") or "")) or str(x.get("who") or ""),
                "text": str(x.get("text") or "")[:80],
                "message_id": str(x.get("message_id") or ""),
            }
            for x in ref_items
        ]
        try:
            keywords = json.loads(r["keywords"] or "[]")
        except (ValueError, TypeError):
            keywords = []
        keywords_out = [str(x) for x in keywords if str(x or "").strip()] if isinstance(keywords, list) else []
        return {
            "id": int(r["id"]),
            "icon": str(r["icon"] or "newspaper"),
            "kind": str(r["kind"] or "news"),
            "title": str(r["title"]),
            "summary": str(r["summary"] or ""),
            "why": str(r["why"] or ""),
            "sources": sources,
            "published_ts": float(published) if published is not None else None,
            "scores": scores_out,
            "topic": str(r["topic"] or ""),
            "profile_ref": str(r["profile_ref"] or ""),
            "body": str(r["body"] or ""),
            "reason": str(r["reason"] or ""),
            "refs": refs_out,
            "keywords": keywords_out,
            "target_user_id": str(r["target_user_id"] or ""),
            "created_ts": float(r["created"] or 0.0),
        }

    def _idea_personal_view(self, r: Any) -> dict:
        return {
            "id": int(r["id"]),
            "icon": str(r["icon"] or "bulb"),
            "title": str(r["title"]),
            "body": str(r["body"] or ""),
            "basis": str(r["basis"] or ""),
            "step": clean_step(r["step"]),
            "effort": str(r["effort"] or ""),
            "target_user_id": str(r["target_user_id"] or ""),
            "created_ts": float(r["created"] or 0.0),
            # 个人向构想没有项目列表（还是老的单任务形态）；给个空列表让网页形状一致。
            "items": [],
        }

    # ------------------------------------------------------------------
    # 前提
    # ------------------------------------------------------------------

    def _member(self, gid: str, uid: str) -> dict | None:
        """当前关注成员（removed=0）且 persona 是合法 JSON → {"name","persona"}；否则 None。"""
        row = self._store.read().execute(
            "SELECT name, persona FROM focus_members WHERE group_id=? AND user_id=? AND removed=0",
            (gid, uid),
        ).fetchone()
        if row is None:
            return None
        raw = str(row["persona"] or "").strip()
        if not raw:
            return None
        try:
            persona = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(persona, dict):
            return None

        def _lst(v: Any) -> list[str]:
            return [str(x).strip() for x in v if str(x or "").strip()] if isinstance(v, list) else []

        persona = {
            "summary": str(persona.get("summary") or ""),
            "doing": _lst(persona.get("doing")),
            "cares": _lst(persona.get("cares")),
            "asked": _lst(persona.get("asked")),
        }
        name = str(row["name"] or "").strip() or uid
        return {"name": name, "persona": persona}

    def _safe_entries(self, gid: str) -> list[dict]:
        try:
            entries = self._profiles.entries(gid)
        except Exception:
            logger.exception("读画像条目失败（群 %s）", gid)
            return []
        return list(entries or [])

    async def _ensure_search(self) -> None:
        """确认搜索可用（同 feeds 的绑定自检：不发探活请求；没绑好抛 SearchUnavailable）。"""
        if self._search is None:
            return
        available_probe = getattr(self._search, "available", None)
        if callable(available_probe):
            if not available_probe():
                _, text = self._search.status()
                raise SearchUnavailable(text or "还没指定联网搜索：去 设置 → 扩展 里选一个 MCP 用作联网搜索")
            return
        probe = getattr(self._search, "search", None)
        if callable(probe):
            await probe("__配置自检__", limit=1)

    def _prompt_block_safe(self, kind: str, group_id: str | None = None) -> str:
        """identity.prompt_block；没有 identity / 它出错 / 内容空 → 都 ""。各注入点靠这个回落。"""
        identity = self._identity
        if identity is None:
            return ""
        try:
            out = identity.prompt_block(kind, group_id=group_id)
        except Exception:
            return ""
        return str(out or "").strip()
