"""feeds.py（M2，docs/07-代码接口.md §10.4）：资讯和构想。

资讯流程（prepare_news，按 docs/02-设计.md §4.1「质量标准（2026-09-27 与用户定）」三道门槛）：
1. 群画像没成形（groups.profile_ready_ts == 0）→ 0，什么都不做；
2. 主模型没配好 → 0；搜索没配（SearchUnavailable）→ 记一条 skipped 批次；
3. 主模型（json_mode）按画像条目 + 最近 14 天反馈标题出 3–5 个关注点；
4. 交给 Workers.run（web_search / fetch_page）同时找「资讯」（最近几天的新闻/发布/动态）
   和「好文」（教程、好文章、工具介绍，不看新不新；[feeds] guides=false 就只找资讯）。
   每条候选必须真用 fetch_page 打开过（交回 fetched=true + quote≤200 字原文依据），
   要登录/付费的标 paywall=true；每条带 kind（news|guide）；
5. 第一道（硬性淘汰，不管分数）：
   - 代码侧：fetched 不真 / quote 空（「原文没打开过/打不开」）、paywall（「要登录/付费」）、
     URL 规范化 / 标题近似撞最近 lookback_days 天已出的（「重复」）、
     屏蔽名单（[feeds] blocked_domains + kv["feeds.blocked_domains"] 网页维护的，按域名及子域；
     被标「没用」净值 down-up 累计 ≥3 的域名自动屏蔽，「这个来源被标没用太多次」）；
   - 打分模型顺带判：grounded=false（「摘要在原文找不到依据」）、junk=true（「垃圾：原因」）、
     same_as_recent=true（「和最近出过的是同一件事（重复）」，参考最近 14 天已出标题）；
6. 第二道（上网页）：打分模型对每条出 info/source/relevance/timeliness/chat 五项 1–5 分
   （好文的 timeliness 解释成「现在还适用」，核对版本/价格/接口过时没）、
   profile（对应群画像条目的编号；对不上 → relevance 封顶 2）、topic（≤8 字话题标签）、
   sensitive（政治/争议）。avg=五项平均；avg ≥ [feeds] web_min_avg（默认 3）且 relevance ≥ 3
   才上网页，否则 rejected gate='web'。再按 avg 高者留去同质化：同一 topic ≤2、
   同一域名 ≤3、敏感 ≤1、总数 ≤ [feeds] max_items（默认 10），落选 gate='web'；
7. 第三道（进话题候选池）：kind=news、avg ≥ [feeds] pool_min_avg（默认 4）、
   relevance ≥4、chat ≥4、published 在 48 小时内（没有 published 不进）、非 sensitive。
   好文一律不进；chat_votes ≥ 2 的资讯可破格进池（48 小时、非争议等其他条件照旧），
   破格进池的候选过期时间 24 小时；

「有人味」（docs/02 §4.1，2026-09-27 与用户定）：打分之后、入库前，对过第二道门槛的
每条再调一次主模型 json_mode 写「帖子」（body/reason/refs/audience/keywords），
输入带候选标题/摘要/quote/来源、群画像、search_chat 查到的本群相关原话
（最多 6 条带时间和名字）、MaiBot 人设（host.config 读 bot.nickname /
personality.personality / personality.reply_style，读不到就略过）和
kv["feeds.pref.<群号>"] 资讯偏好。代码侧：refs 序号换真实 {ts, who, text(截 80),
message_id}；audience 只留确实出现在引用原话里的名字；body 链接只留 http(s) 最多 4 个；
reason/body/audience 过 privacy.scrub（按 note/persona 片段规则，名字本身放行）；
写帖子失败回落 body=summary、reason=why，不丢条目。好文同样处理。
定关注点可额外产出 0–1 个「不同角度 / 反方观点」方向（diverse），命中它的候选打
angle='diverse'，去同质化每轮最多留 2 条 diverse。
8. 被筛掉的也入库（rejected=1 + reject_gate + reject_reason + scores）；
   一个事务写 news_batches + news_items；过第三道的 topics.add_candidate(kind="news", …)。

任何一步模型 / 子 agent 失败 → 记 skipped 批次（note 中文原因），返回 0，不抛。

构想（make_idea）：画像没成形 / 模型没配好 → None；主模型写 0 或 1 条「我可以……」；
和最近 30 天构想标题 difflib ≥ 0.75 → None；chat_worthy 的才进话题候选池。

view 结构照 docs/07 §9.3（news 只含过线的 kind=news 条目；rejected 一栏只给管理员；
guides 走好文专栏），前端字段名一个都不能变。
"""

from __future__ import annotations

import difflib
import ipaddress
import json
import logging
import re as _re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import clock
from .config import Settings, normalize_domain as _normalize_domain
from .models import ModelError
from .search import SearchUnavailable
from .store import Store


_FOCUS_KEYS = ("focus", "focuses", "items", "queries", "关注点")


_STEP_PREFIX = _re.compile(r"^\s*(?:第一步\s*[:：]\s*)+")


def clean_step(raw: Any) -> str:
    """「第一步」字段去掉模型自带的「第一步：」前缀（页面标签已经写了「第一步」）。"""
    return _STEP_PREFIX.sub("", str(raw or "")).strip()


def focus_items(data: Any) -> list[dict]:
    """从模型回的 JSON 里拿出关注点列表，对格式宽容。

    约定是 {"focus": [{"query", "why"}]}，但有的模型会：直接回一个关注点对象
    （线上 step-5-preview 实测）、回裸列表、focus 给成单个对象、换键名、或给纯字符串。
    都认；认不出返回 []。顶层 "diverse" 不算 focus。
    """
    raw: Any = None
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        for k in _FOCUS_KEYS:
            if k in data:
                raw = data[k]
                break
        else:
            if str(data.get("query") or "").strip():
                raw = [data]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for f in raw:
        if isinstance(f, str) and f.strip():
            out.append({"query": f.strip(), "why": ""})
        elif isinstance(f, dict) and str(f.get("query") or "").strip():
            out.append({"query": str(f["query"]).strip(), "why": str(f.get("why") or "")})
    return out

logger = logging.getLogger("maiwork.feeds")

# 资讯实测（railway.new 一次性 VM，docs/09）
_VERIFY_BUDGET_S = 20 * 60.0        # 一轮实测的总时长上限 20 分钟
_VERIFY_SUMMARY_MAX = 200
_VERIFY_STEPS_MAX = 6
_VERIFY_STEP_MAX_LEN = 80

# 允许模型挑的图标（来自 console/static/assets/icons/ 的名单，固定写死）
_ICONS = (
    "robot", "newspaper", "bulb", "monitor", "chart", "rocket", "books", "testtube",
    "palette", "camera", "joystick", "sparkles", "magnifier", "floppy", "link",
    "calendar", "cloud", "tools", "package",
)

_CANDIDATE_CAP = 12         # 子 agent 最多交回多少条（brief 里也这么要求）
_FEEDBACK_SCAN_DAYS = 14    # 关注点提示 / 打分参考的最近反馈窗口
_IDEA_DEDUP_DAYS = 30       # 构想去重窗口
_IDEA_DEDUP_RATIO = 0.75
_TITLE_DEDUP_RATIO = 0.8
_NEWS_VIEW_DAYS = 3
_IDEAS_DISMISSED_KEEP_DAYS = 3
_IDEAS_VIEW_CAP = 20
_GUIDES_VIEW_DAYS = 30      # 好文专栏：最近 30 天
_GUIDES_VIEW_CAP = 20       # 好文专栏最多 20 条
_QUOTE_MAX = 200            # 子 agent 交回的原文依据最长（截断照收，只有空才淘汰）
_TOPIC_MAX_LEN = 8          # 话题标签最多几个字（超了截断）
_POOL_NEWS_MAX_AGE_H = 48   # 进话题候选池的资讯必须多少小时内
_AUTO_BLOCK_NET_DOWN_MIN = 3   # 「没用」净值（down-up）到多少自动屏蔽这个来源
_AUTO_BLOCK_LOOKBACK_DAYS = 90 # 净值只看最近多少天
_NORM_TOPIC_CAP = 2         # 去同质化：同一话题最多几条
_NORM_DOMAIN_CAP = 3        # 去同质化：同一域名最多几条
_NORM_SENSITIVE_CAP = 1     # 去同质化：敏感话题最多几条
_NORM_DIVERSE_CAP = 2       # 去同质化：同一轮「不同角度」最多几条
_CHAT_VOTE_MIN = 2          # 「想在群里聊」几票可以破格进候选池
_CHAT_VOTE_TTL_H = 24.0     # 破格进池的候选过期时间（小时）
_PREF_MAX = 300             # 资讯偏好一句话最长（字）
_BODY_LINK_MAX = 4          # body 里最多留几个嵌入链接
_REF_TEXT_MAX = 80          # refs 里每条原话最多留多少字
_REASON_MAX = 200           # reason 最多留多少字
_AUDIENCE_MAX = 8           # audience 最多几个名字
_KEYWORDS_MAX = 10          # keywords 最多几个
_SEARCH_QUOTES = 6          # 写帖子每条最多带几条群原话

_NEWS_OUTPUT_SCHEMA = {
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
                    "image_url": {"type": "string"},
                },
                "required": ["title", "url", "summary", "kind", "fetched", "quote", "paywall"],
            },
        }
    },
    "required": ["items"],
}


# ----------------------------------------------------------------------
# url 规范化
# ----------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """去 utm_*/fbclid/gclid、去结尾 /、小写 host、去 #、去默认端口；解析失败返回 ""。"""
    raw = str(url or "").strip()
    if not raw or " " in raw or any(ord(c) > 0x2FFF for c in raw):
        return ""  # 明显不是 URL（空格、CJK 表意文字段）
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    host = p.hostname.lower()
    try:
        port = p.port
    except ValueError:
        return ""
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = p.path.rstrip("/") or "/"
    pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in ("fbclid", "gclid"))
    ]
    query = urlencode(pairs)
    return f"{host}{path}{('?' + query) if query else ''}"


def _site_of(url: str) -> str:
    """来源站点名：host 小写、去 www. 前缀。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _public_http_url(url: str) -> str:
    """洗净的 http(s) 链接；内网 / 本机 IP、解析不了的一律返回 ""。只防常识，不做 DNS。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # 域名放行
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            return ""
    return p.geturl()


def _parse_published(value: Any) -> float | None:
    """published：epoch（int/float / 数字字符串）或 ISO 字符串；解析不了 None。"""
    from datetime import datetime

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


import re as _re

_MD_LINK_RE = _re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


_BARE_URL_RE = _re.compile(r"(?<!\()\b(?:https?://|javascript:|ftp:|data:|file:)[^\s)\]，。；!！?？\"']+")


def _clean_body_links(body: str, max_links: int = 4) -> str:
    """body 里嵌的链接只留 http(s)、最多 max_links 个；其他协议的脱掉外壳只留文字。

    - Markdown 形态 [文字](https://链)：http(s) 且没超上限 → 原样留；否则变回纯文字「文字」；
    - 裸 http(s) URL（前面不是 `(` 的）：超上限 → 删掉这条 URL；
    - javascript:/ftp:/data:/file: 等非 http(s) 的：一律洗掉（Markdown 的脱壳，裸的删）。
    """
    text = str(body or "")
    kept = 0

    def _md_sub(m: "_re.Match[str]") -> str:
        nonlocal kept
        label, url = m.group(1), m.group(2).strip()
        if kept < max_links and url.startswith(("http://", "https://")):
            kept += 1
            return m.group(0)
        return label or ""  # 超上限 / 非 http(s)：脱壳只留文字

    text = _MD_LINK_RE.sub(_md_sub, text)

    def _url_sub(m: "_re.Match[str]") -> str:
        nonlocal kept
        url = m.group(0)
        if not url.startswith(("http://", "https://")):
            return ""  # javascript:/ftp:/data:/file: 一律删
        if kept < max_links:
            kept += 1
            return url
        return ""  # 超上限的裸 URL 删掉

    return _BARE_URL_RE.sub(_url_sub, text)



def _split_query_words(query: str) -> list[str]:
    """把关注点拆成可比对的关键词片段：英文按空格/斜杠切，中文整段 + 常见分割。"""
    import re as _re

    raw = str(query or "")
    parts = _re.split(r"[\s/·,，、。：:；;（）()\[\]【】\"“”'‘’-]+", raw)
    return [p for p in parts if p]



def blocked_domains_effective(store: Any, config_blocked: tuple[str, ...] | list[str]) -> list[str]:
    """生效的屏蔽名单：kv["feeds.blocked_domains"] 存在 → 以它为准；否则用配置的。

    首次网页改动会把当时「配置 ∪ kv」的合并名单写回 kv，之后网页就是权威；
    之后再改配置文件里的名单不会生效（要在网页上加回来）。返回规范化去重的稳定排序。
    """
    kv_list: list[str] | None = None
    try:
        raw = store.kv_get("feeds.blocked_domains", None)
        if isinstance(raw, list):
            kv_list = [str(x) for x in raw]
    except Exception:
        kv_list = None
    if kv_list is not None:
        return sorted({_normalize_domain(d) for d in kv_list if _normalize_domain(d)})
    return sorted({_normalize_domain(d) for d in (config_blocked or ()) if _normalize_domain(d)})


def _domain_blocked(site: str, blocked: set[str] | list[str]) -> bool:
    """按域名及其子域匹配屏蔽名单：site == d 或 site 是 d 的子域。"""
    site = str(site or "").lower()
    if not site:
        return False
    for d in blocked:
        d = str(d or "").lower()
        if d and (site == d or site.endswith("." + d)):
            return True
    return False


# ----------------------------------------------------------------------
# Feeds
# ----------------------------------------------------------------------


class Feeds:
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
        on_start: Callable[[dict], None] | None = None,
        verify_runner: Any = None,
        identity: Any = None,  # identity.py（SOUL/工作记忆注入；None 走老的纯 host 逻辑）
    ) -> None:
        self._store = store
        self._models = models
        self._workers = workers
        self._profiles = profiles
        self._topics = topics
        self._get_settings = get_settings
        self._search = search
        # 「实测过再发是加分项」（docs/02 §4.1）：挑中的条目在一次性 VM 里实测。
        # 契约：async verify_runner(items, picks, gid, settings)；直接改 items[i]["verify"]。
        # None = 这轮没有实测能力（没接线 / railway=false），跳过实测，照常出资讯。
        self._verify_runner = verify_runner
        # 「有人味」：写帖子要按 MaiBot 人设口吻（host.config 读 bot.nickname 等），
        # 没有 host（老测试 / 特殊部署）就跳过人设，读不到单项就略过。
        # identity（identity.py）在时有 SOUL 就优先按 SOUL 的口吻（身份与工作记忆），
        # 没有 SOUL 内容仍回落 host 这套老逻辑。
        self._host = host
        self._identity = identity
        # 构想「做这个」的回调：app 接成 _on_idea_started（落成任务并开工）；None = 只记状态。
        self.on_start = on_start
        # RSS 客户端注入（httpx.MockTransport，tests 用；None = 真实网络，接口/备料自己起）
    async def _collect_rss(self, gid: str, settings: Settings) -> list[dict]:
        """把本群启用中的 RSS 源取回来，交回「优先看这些链接」的 RSS 条目候选。

        和搜索子 agent 的候选**走同一套质量门槛**（硬淘汰里「原文打开过」对 RSS：
        交给子 agent 的 brief 作「优先看这些链接」，由子 agent fetch_page 打开并按原流程
        交回时带 fetched/quote——不另开绿灯）。sources.site 用源标题（rss.title）代替域名。
        全 MockTransport；取失败只记 last_error，不拖垮这轮备料。
        """
        from . import rss as _rss

        try:
            feeds = [e for e in _rss.list_feeds(self._store, gid) if bool(e.get("enabled"))]
        except Exception:
            feeds = []
        if not feeds:
            return []
        lookback = int(getattr(settings.feeds, "lookback_days", 14) or 14)
        out: list[dict] = []
        for entry in feeds[:20]:
            fid = str(entry.get("id") or "")
            url = str(entry.get("url") or "")
            try:
                result = await _rss.fetch_feed_source(
                    url,
                    transport=self._rss_transport,
                    lookback_days=lookback,
                    now=clock.now(),
                    limit=_CANDIDATE_CAP,
                )
            except Exception:
                result = {"title": "", "items": [], "error": "意外错误"}
            ok_ts: float | None = None
            error = str(result.get("error") or "")
            if not error:
                ok_ts = clock.now()
            try:
                _rss.mark_checked(self._store, gid, fid, ok_ts=ok_ts, error=error)
            except Exception:
                pass
            if error:
                logger.info("RSS 源 %s（群 %s）取回失败：%s", url, gid, error[:120])
                continue
            for it in result.get("items") or []:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("title") or "").strip()
                url_i = _public_http_url(it.get("url"))
                if not title or not url_i:
                    continue
                summary = str(it.get("summary") or "").strip()
                out.append(
                    {
                        "title": title,
                        "url": url_i,
                        "summary": summary,
                        "kind": "news",
                        "published_raw": it.get("published"),
                        "published_ts": it.get("published"),
                        "fetched": True,  # RSS 源本身算「打开过」；quote 交给子 agent 补
                        "quote": summary[:_QUOTE_MAX] if summary else title[:_QUOTE_MAX],
                        "paywall": False,
                        "image_url": "",
                        "url_key": _normalize_url(url_i),
                        "site": str(entry.get("title") or _rss._site_of(url_i) or url_i.split("/", 3)[2]),
                        "_rss_feed": url,
                        "_rss_title": str(entry.get("title") or ""),
                    }
                )
        return out

    # ------------------------------------------------------------------
    # 资讯
    # ------------------------------------------------------------------

    async def prepare_news(self, group_id: str) -> int:
        gid = str(group_id)
        settings = self._get_settings()
        if not self._profile_ready(gid):
            return 0
        try:
            if not self._models_ready():
                return 0
        except Exception:
            return 0
        try:
            await self._ensure_search()
        except SearchUnavailable as e:
            self._skipped_batch(gid, str(e) or "搜索没配置")
            return 0

        # ① 关注点
        try:
            focus = await self._plan_focus(gid, settings)
        except (ModelError, ValueError) as e:
            logger.info("备资讯-定关注点失败（群 %s）：%s", gid, e)
            self._skipped_batch(gid, f"模型出关注点失败：{e}")
            return 0

        # ② 子 agent 找候选（资讯 + 好文；每条必须真打开过）
        try:
            candidates = await self._collect(gid, focus, settings)
        except (ModelError, ValueError) as e:
            logger.info("备资讯-子 agent 失败（群 %s）：%s", gid, e)
            self._skipped_batch(gid, f"子 agent 没找到东西：{e}")
            return 0
        except Exception as e:  # 兜底：任何意外都不能炸后台循环
            logger.exception("备资讯-子 agent 意外错误（群 %s）", gid)
            self._skipped_batch(gid, f"子 agent 出了意外：{e}")
            return 0

        # ③ 第一道（代码侧）：没打开过 / 付费 / 屏蔽来源 / URL·标题重复
        survivors = self._hard_reject_code(gid, settings, candidates)

        # ④ 打分（幸存者为空就不调模型，省额度）
        if survivors:
            try:
                await self._score(gid, settings, survivors)
            except (ModelError, ValueError) as e:
                logger.info("备资讯-打分失败（群 %s）：%s", gid, e)
                self._skipped_batch(gid, f"模型打分失败：{e}", found=len(candidates))
                return 0
            # 第一道（模型侧）：不扎实 / 垃圾 / 同一件事
            self._hard_reject_model(survivors)

        # ⑤ 第二道（上网页）：五项分门槛
        web_min_avg = float(getattr(settings.feeds, "web_min_avg", 3.0))
        for item in survivors:
            if "reject" in item:
                continue
            sc = item["scores"]
            if sc["relevance"] < 3.0:
                item["reject"] = ("web", f"相关度 {sc['relevance']:.1f} < 3.0，过不了上网页这道")
            elif sc["avg"] < web_min_avg:
                item["reject"] = ("web", f"平均分 {sc['avg']:.1f} < {web_min_avg:.1f}，过不了上网页这道")
        # G7 隐私闸：why 含关注成员注记 / 画像片段的整条丢弃（why 群友可见；名字本身放行）
        for item in survivors:
            if "reject" not in item and self._scrub_item_text(gid, str(item.get("why") or "")) is None:
                item["reject"] = ("web", "和群友相关的细节不宜公开，这条不上")

        # ⑤.5 「有人味」：对过第二道门槛的每条写帖子（失败回落不丢条目）
        posting = [item for item in survivors if "reject" not in item]
        if posting:
            try:
                await self._write_posts(gid, posting, settings)
            except Exception:
                logger.exception("写帖子意外出错（群 %s），全部回落原文", gid)
                for item in posting:
                    self._post_fallback(item)

        # ⑥ 第二道（去同质化）：同话题 ≤2、同域名 ≤3、敏感 ≤1、diverse ≤2、总数 ≤ max_items；avg 高者留
        max_items = max(1, int(getattr(settings.feeds, "max_items", 10)))
        self._dedup_homogeneous(survivors, max_items)

        # ⑥.5 「实测过再发是加分项」（docs/02 §4.1）：对最终入选的条目挑 ≤2 条，
        # 在 railway.new 一次性 VM 里真试一下（railway=false 整个关掉；拿不到机器照常入库）
        final_items = [item for item in survivors if "reject" not in item]
        if final_items and self._verify_runner is not None:
            try:
                await self._plan_and_verify(gid, final_items, settings)
            except Exception:
                # 实测是加分项不是门槛：这步炸了什么都不能拖累出资讯
                logger.exception("实测环节意外出错（群 %s），这轮跳过实测", gid)

        # ⑦ 第三道（进话题候选池）+ 落库（被筛掉的也入库）
        pool_min_avg = float(getattr(settings.feeds, "pool_min_avg", 4.0))
        ttl_h = float(getattr(settings.topics, "candidate_ttl_hours", 12))
        now = clock.now()
        # survivors / candidates 是同一份对象引用：被筛的直接看 candidates 里带 reject 的
        accepted = [item for item in survivors if "reject" not in item]
        rejected_items = [item for item in candidates if item.get("reject")]
        kept = len(accepted)
        note = "" if kept else ("没找到值得发的" if not survivors else "没有过门槛的")
        batch_id = self._insert_batch_and_items(
            gid,
            now,
            found=len(candidates),
            kept=kept,
            rejected_items=rejected_items,
            accepted_items=accepted,
            ttl_h=ttl_h,
            note=note,
        )
        del batch_id  # 目前不对外用
        # 第三道：kind=news、avg≥pool_min_avg、relevance≥4、chat≥4、48 小时内、非敏感；
        # chat_votes ≥2 可破格进池（其他条件照旧），候选过期 24 小时
        for item in accepted:
            eligible, voted = self._pool_eligible_check(item, pool_min_avg, now)
            if not eligible:
                continue
            try:
                self._topics.add_candidate(
                    gid, kind="news", ref_id=item["_news_id"], title=item["title"],
                    brief=self._pool_brief(item), link=item["url"],
                    ttl_h=_CHAT_VOTE_TTL_H if voted else None,
                )
            except Exception:
                logger.exception("资讯进话题候选池失败（群 %s 条 %s）", gid, item.get("_news_id"))
        return kept

    @staticmethod
    def _pool_brief(item: dict) -> str:
        """候选池的摘要：有人味的正文优先（没有就原 summary），截 120。"""
        text = str(item.get("body") or "") or str(item.get("summary") or "")
        return text[:120]

    # ------------------------------------------------------------------
    # 第一道（代码侧）
    # ------------------------------------------------------------------

    def _hard_reject_code(self, gid: str, settings: Settings, candidates: list[dict]) -> list[dict]:
        """第一道硬性淘汰里「不调模型」的部分；被筛的打上 item["reject"]=(gate, reason)。

        返回幸存列表（candidates 里没打 reject 的；顺序保持子 agent 交回的原序）。
        """
        blocked = self._blocked_domains(gid, settings)
        auto_blocked = set(self._auto_blocked_domains(gid))
        lookback_days = max(1, int(getattr(settings.feeds, "lookback_days", 14)))
        since = clock.now() - lookback_days * 86400.0
        rows = self._store.read().execute(
            "SELECT url_key, title FROM news_items WHERE group_id=? AND created>=? AND rejected=0",
            (gid, since),
        ).fetchall()
        seen_urls = {str(r["url_key"]) for r in rows if r["url_key"]}
        seen_titles = [str(r["title"]) for r in rows if r["title"]]
        survivors: list[dict] = []
        for item in candidates:
            url_key = item["url_key"]
            site = item.get("site") or _site_of(item["url"])
            item["site"] = site
            if not item.get("fetched") or not str(item.get("quote") or "").strip():
                item["reject"] = ("hard", "原文没打开过/打不开")
            elif item.get("paywall"):
                item["reject"] = ("hard", "原文要登录/付费")
            elif site and _domain_blocked(site, blocked):
                item["reject"] = ("hard", "来源在屏蔽名单里")
            elif site and _domain_blocked(site, auto_blocked):
                item["reject"] = ("hard", "这个来源被标没用太多次")
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

    def _blocked_domains(self, gid: str, settings: Settings) -> set[str]:
        """生效的屏蔽名单（域名 & 其子域在匹配处再展开）。

        语义：kv["feeds.blocked_domains"] 存在 = 网页改过一次，以它为准（首次改动时已把
        当时配置里的合并进来）；不存在 = 用 [feeds] blocked_domains。
        集中在一个模块函数里（console 的 settings / domains 接口也用同一份），
        返回规范化后的去重列表。
        """
        return set(blocked_domains_effective(self._store, tuple(getattr(settings.feeds, "blocked_domains", ()) or ())))

    def _auto_blocked_domains(self, gid: str) -> list[str]:
        """自动屏蔽：来源域名按「没用」净值（down-up）累计 ≥3（只看最近 90 天）。

        来源取入库的 sources[0].site（当时规范化过）；轮询来源缺失的用 url_key 的主机段兜底。
        稳定排序输出（网页设置里展示用）。
        """
        since = clock.now() - _AUTO_BLOCK_LOOKBACK_DAYS * 86400.0
        try:
            rows = self._store.read().execute(
                "SELECT sources, url_key, up, down FROM news_items"
                " WHERE group_id=? AND created>=? AND rejected=0",
                (gid, since),
            ).fetchall()
        except Exception:
            logger.debug("自动屏蔽统计失败（群 %s）", gid, exc_info=True)
            return []
        by_site: dict[str, int] = {}
        for r in rows:
            site = ""
            try:
                src = json.loads(r["sources"] or "[]")
                if isinstance(src, list) and src and isinstance(src[0], dict):
                    site = _normalize_domain(src[0].get("site") or "")
                    if not site:
                        site = _normalize_domain((src[0].get("url") or "").split("/")[2] if "://" in str(src[0].get("url") or "") else "")
            except (ValueError, TypeError):
                src = []
            if not site:
                site = _normalize_domain(str(r["url_key"] or "").split("/", 1)[0])
            if not site:
                continue
            by_site[site] = by_site.get(site, 0) + (int(r["down"] or 0) - int(r["up"] or 0))
        return sorted(d for d, net in by_site.items() if net >= _AUTO_BLOCK_NET_DOWN_MIN)

    # ------------------------------------------------------------------
    # 第一道（模型侧）
    # ------------------------------------------------------------------

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
                item["reject"] = ("hard", "和最近出过的是同一件事（重复）")

    # ------------------------------------------------------------------
    # 第二道（去同质化）
    # ------------------------------------------------------------------

    def _dedup_homogeneous(self, survivors: list[dict], max_items: int) -> None:
        """过线的条目按 topic / 域名 / 敏感 / 不同角度上限 + 总数上限去同质化，avg 高者留；
        落选的打 item["reject"]=("web", 中文原因)。"""
        web = [item for item in survivors if "reject" not in item and "scores" in item]
        # 按 avg 高到低稳定排（同分保持原序）
        ordered = sorted(web, key=lambda it: -float(it["scores"]["avg"]))
        topic_n: dict[str, int] = {}
        domain_n: dict[str, int] = {}
        sensitive_n = 0
        diverse_n = 0
        kept_n = 0
        for item in ordered:
            topic = str(item.get("topic") or "")
            site = str(item.get("site") or "")
            if kept_n >= max_items:
                item["reject"] = ("web", f"超出本轮上限（最多 {max_items} 条）")
                continue
            if topic and topic_n.get(topic, 0) >= _NORM_TOPIC_CAP:
                item["reject"] = ("web", "同一个话题这轮已经留了两条，留分高的")
                continue
            if site and domain_n.get(site, 0) >= _NORM_DOMAIN_CAP:
                item["reject"] = ("web", "同个来源这轮已经留了三条，留分高的")
                continue
            if item.get("sensitive") and sensitive_n >= _NORM_SENSITIVE_CAP:
                item["reject"] = ("web", "争议话题这轮已有一条，留分高的")
                continue
            if str(item.get("angle") or "") == "diverse" and diverse_n >= _NORM_DIVERSE_CAP:
                item["reject"] = ("web", "不同角度的这轮已经留了两条，留分高的")
                continue
            # 留下
            kept_n += 1
            if topic:
                topic_n[topic] = topic_n.get(topic, 0) + 1
            if site:
                domain_n[site] = domain_n.get(site, 0) + 1
            if item.get("sensitive"):
                sensitive_n += 1
            if str(item.get("angle") or "") == "diverse":
                diverse_n += 1

    # ------------------------------------------------------------------
    # 第三道（进话题候选池）资格
    # ------------------------------------------------------------------

    def _pool_eligible(self, item: dict, pool_min_avg: float, now: float) -> bool:
        if item.get("kind") != "news":
            return False
        if item.get("sensitive"):
            return False
        sc = item.get("scores") or {}
        if float(sc.get("avg") or 0.0) < pool_min_avg:
            return False
        if float(sc.get("relevance") or 0.0) < 4.0 or float(sc.get("chat") or 0.0) < 4.0:
            return False
        published = item.get("published_ts")
        if not isinstance(published, (int, float)):
            return False
        return now - float(published) <= _POOL_NEWS_MAX_AGE_H * 3600.0

    def _pool_eligible_check(self, item: dict, pool_min_avg: float, now: float) -> tuple[bool, bool]:
        """(能不能进池, 是不是投票破格进的)。

        chat_votes ≥ _CHAT_VOTE_MIN 的资讯：「pool_min_avg / relevance≥4 / chat≥4」这三条
        分线可以不用过，但「kind=news / 非争议 / 48 小时内」照旧。
        """
        if self._pool_eligible(item, pool_min_avg, now):
            return True, False
        if self._pool_eligible_with_votes(item, pool_min_avg, now):
            return True, True
        return False, False

    def _pool_eligible_with_votes(self, item: dict, pool_min_avg: float, now: float) -> bool:
        """投票破格的资格判断：chat_votes ≥2 +（kind=news、非争议、48 小时内）。"""
        del pool_min_avg  # 投票破格不看分数门槛（48 小时、非争议等照旧）
        if int(item.get("chat_votes") or 0) < _CHAT_VOTE_MIN:
            return False
        if item.get("kind") != "news":
            return False
        if item.get("sensitive"):
            return False
        published = item.get("published_ts")
        if not isinstance(published, (int, float)):
            return False
        return now - float(published) <= _POOL_NEWS_MAX_AGE_H * 3600.0

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _insert_batch_and_items(
        self,
        gid: str,
        now: float,
        *,
        found: int,
        kept: int,
        rejected_items: list[dict],
        accepted_items: list[dict],
        ttl_h: float,
        note: str,
    ) -> int:
        """一个事务写批次 + 全部条目（通过的和被筛的都在），返回批次 id。

        通过的：rejected=0、score=avg、status 'pool'、expires_ts 按候选 ttl；
        被筛的：rejected=1、reject_gate/reject_reason、score=avg（有分的话）、不进池。
        每条通过的条目的 news_items.id 回写在 item["_news_id"]（第三道要用）。
        """
        expires = now + ttl_h * 3600.0
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, now, int(found), int(kept), 1 if not kept else 0, str(note)[:300], now),
            )
            batch_id = int(cur.lastrowid or 0)
            for item in accepted_items:
                sc = item.get("scores") or {}
                post = item.get("post") or {}
                verify_raw = item.get("verify")
                verify_json = json.dumps(verify_raw, ensure_ascii=False) if isinstance(verify_raw, dict) else ""
                cur = conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                    " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                    " reject_gate, reject_reason, body, reason, refs, audience, image_url,"
                    " keywords, chat_votes, angle, verify)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pool', NULL, 0, ?, 0, 0, ?,"
                    " ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id, gid, str(item.get("icon") or "newspaper"), item["title"],
                        item["summary"], str(item.get("why") or ""),
                        json.dumps(
                            [{"url": item["url"], "site": str(item.get("site") or _site_of(item["url"])), "title": item["title"]}],
                            ensure_ascii=False,
                        ),
                        item["url_key"], item.get("published_ts"),
                        float(sc.get("avg") or 0.0), expires if item.get("kind") == "news" else None,
                        now, str(item.get("kind") or "news"),
                        json.dumps(sc, ensure_ascii=False),
                        str(item.get("topic") or ""), 1 if item.get("sensitive") else 0,
                        str(item.get("profile_ref") or ""),
                        str(post.get("body") or ""), str(post.get("reason") or ""),
                        json.dumps(post.get("refs") or [], ensure_ascii=False),
                        json.dumps(post.get("audience") or [], ensure_ascii=False),
                        str(item.get("image_url") or ""),
                        json.dumps(post.get("keywords") or [], ensure_ascii=False),
                        int(item.get("chat_votes") or 0),
                        str(item.get("angle") or ""),
                        verify_json,
                    ),
                )
                item["_news_id"] = int(cur.lastrowid or 0)
            for item in rejected_items:
                gate, reason = item.get("reject") or (None, None)
                sc = item.get("scores") or {}
                conn.execute(
                    "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
                    " url_key, published_ts, score, status_kind, status_at, replies, expires_ts,"
                    " up, down, created, kind, scores, topic, sensitive, profile_ref, rejected,"
                    " reject_gate, reject_reason, angle, image_url)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', NULL, 0, NULL, 0, 0, ?,"
                    " ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        batch_id, gid, str(item.get("icon") or "newspaper"), item["title"],
                        item["summary"], str(item.get("why") or ""),
                        json.dumps(
                            [{"url": item["url"], "site": str(item.get("site") or _site_of(item["url"])), "title": item["title"]}],
                            ensure_ascii=False,
                        ),
                        item["url_key"], item.get("published_ts"),
                        float(sc.get("avg") or 0.0),
                        now, str(item.get("kind") or "news"),
                        json.dumps(sc, ensure_ascii=False) if sc else "",
                        str(item.get("topic") or ""), 1 if item.get("sensitive") else 0,
                        str(item.get("profile_ref") or ""), str(gate) if gate else None,
                        str(reason) if reason else None,
                        str(item.get("angle") or ""), str(item.get("image_url") or ""),
                    ),
                )
        return batch_id

    async def _plan_focus(self, gid: str, settings: Settings) -> list[dict]:
        """定关注点。返回 [{"query", "why", "angle"}]；angle='diverse' 的是「不同角度/反方观点」。

        提示词里带：群画像 + 最近反馈 + 资讯偏好（kv["feeds.pref.<群号>"]）。
        模型可额外给 0–1 个不同角度关注点（顶层 "diverse" 键），进搜索列表，产出条目
        带 angle='diverse'；去同质化时每轮最多留 2 条。
        """
        entries = self._safe_entries(gid)
        feedback = self._recent_feedback_titles(gid)
        agents_block = self._prompt_block_safe("agents")
        mem_block = self._prompt_block_safe("memory", group_id=gid)
        # AGENTS.md（做事规矩，含管理员写的搜索要求）和工作记忆一起，在定关注点时就生效
        lines = ([agents_block.strip()] if agents_block else [])
        lines += ([mem_block.strip()] if mem_block else [])
        lines += ["这是一个 QQ 群的画像（分类整理）："]
        if entries:
            for e in entries[:40]:
                lines.append(f"- [{e.get('category', '')}] {e.get('text', '')}")
        else:
            lines.append("（画像还是空的，就按泛科技兴趣挑）")
        lines.append("")
        pref = self.pref(gid)
        if pref:
            lines.append(f"管理员对这个群的资讯偏好（每轮都要照着办）：{pref}")
            lines.append("")
        if feedback["up"]:
            lines.append("最近这些资讯群友觉得有用（可以多往这方向找）：")
            lines.extend(f"- {t}" for t in feedback["up"][:10])
        if feedback["down"]:
            lines.append("最近这些资讯群友觉得没用（避开这类）：")
            lines.extend(f"- {t}" for t in feedback["down"][:10])
        lines.append("")
        lines.append(
            "请给出 3–5 个接下来要去找的关注点，只回 JSON："
            '{"focus": [{"query": "拿去搜索的关键词（具体一点）", "why": "为什么这个群会在意"}],'
            ' "diverse": {"query": "…", "why": "…"} | null}'
            "。另外如果找得到一个「不同角度 / 反方观点」的方向（避免回音壁），就放进 diverse"
            "（最多 1 个，没有合适的就 null）。"
        )
        result = await self._models.chat(
            "main",
            [{"role": "user", "content": "\n".join(lines)}],
            json_mode=True,
            purpose="feeds.focus",
            group_id=gid,
        )
        data = json.loads(result.text)
        out = [{**f, "angle": ""} for f in focus_items(data)[:5]]
        if not out:
            raise ValueError("模型没给出能用的关注点（返回格式不对或是空的）")
        diverse = data.get("diverse") if isinstance(data, dict) else None
        if isinstance(diverse, dict) and str(diverse.get("query") or "").strip():
            out.append(
                {
                    "query": str(diverse["query"]).strip(),
                    "why": str(diverse.get("why") or ""),
                    "angle": "diverse",
                }
            )
        return out

    async def _collect(self, gid: str, focus: list[dict], settings: Settings) -> list[dict]:
        lines = []
        for f in focus:
            tag = "（不同角度，刻意找反方观点）" if f.get("angle") == "diverse" else ""
            lines.append(f"- {f['query']}{tag}（原因：{f['why']}）" if f.get("why") else f"- {f['query']}{tag}")
        guides = bool(getattr(settings.feeds, "guides", True))
        if guides:
            kind_req = "两类都要：「资讯」（新闻、发布、动态，要最近几天的新东西）和「好文」（教程、好文章、工具介绍，不看新不新，但要在正文里核对现在还适用——版本、价格、接口有没有过时）；每条用 kind 标明（news=资讯，guide=好文）；"
            kind_field = "kind（news 或 guide）、"
        else:
            kind_req = "只找「资讯」（新闻、发布、动态，要最近几天的新东西）；每条的 kind 一律填 news；"
            kind_field = "kind（一律 news）、"
        # RSS 源（rss.py）：取回后交子 agent 当「优先看这些链接」，由子 agent 打开并按原流程交回——
        # 和搜索候选走同一套质量门槛（不另开绿灯）。取失败只记 last_error，不拖垮这轮备料。
        rss_items = await self._collect_rss(gid, settings)
        rss_section = ""
        if rss_items:
            rss_lines = ["下面这些链接是群订阅的 RSS 源给的，**优先看这些链接**（也要用 fetch_page 打开核对，不行就跳过）："]
            for it in rss_items[:20]:
                rss_lines.append(f"- （RSS：{it.get('_rss_title') or 'rss'}）{it['title']} —— {it['url']}")
            rss_section = "\n".join(rss_lines) + "\n\n"
        brief = (
            "帮这个群找值得看的内容。关注点如下：\n"
            + "\n".join(lines)
            + "\n\n"
            + rss_section
            + "要求：\n"
            f"1. {kind_req}资讯用 web_search 搜最近几天（days 填 3–7）；\n"
            "2. 每条候选必须用 fetch_page 真打开过原文再看一遍，确实和关注点相关、有信息量才收；\n"
            "3. 凑数的、旧的、广告软文、营销号都不要；要登录或付费才能看的也别收，"
            "直接标出来（paywall: true）；\n"
            f"4. 最多交回 {_CANDIDATE_CAP} 条，宁缺毋滥；\n"
            "5. 每条：title（原标题或大意）、url（原文链接）、summary（2–4 句中文纯文本，别用 Markdown）、"
            f"{kind_field}published（发布时间，ISO 格式或 epoch 秒，实在拿不到就空字符串）、"
            f"fetched（确实用 fetch_page 打开过就 true）、quote（从原文里抄一小段能支撑摘要的依据，≤{_QUOTE_MAX} 字）、"
            "paywall（要登录/付费就 true）、"
            "image_url（fetch_page 说有封面图就把那个地址抄过来，没有就空字符串）；\n"
            "6. 最后用 submit_result 交回，data 按约定的 JSON Schema。"
        )
        report = await self._workers.run(
            brief,
            group_id=gid,
            tools=["web_search", "fetch_page"],
            output_schema=_NEWS_OUTPUT_SCHEMA,
        )
        if not getattr(report, "ok", False):
            raise ValueError(str(getattr(report, "error", "") or getattr(report, "summary", "") or "子 agent 没干成"))
        data = report.data
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("子 agent 交回的格式不对")
        items = []
        for raw in data["items"][:_CANDIDATE_CAP]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            url = _public_http_url(raw.get("url"))
            summary = str(raw.get("summary") or "").strip()
            if not title or not url or not summary:
                continue
            kind = str(raw.get("kind") or "news").strip().lower()
            if kind not in ("news", "guide"):
                kind = "news"
            items.append(
                {
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "kind": kind,
                    "published_raw": raw.get("published"),
                    "published_ts": _parse_published(raw.get("published")),
                    "fetched": bool(raw.get("fetched")),
                    "quote": str(raw.get("quote") or "").replace("\n", " ").strip()[:_QUOTE_MAX],
                    "paywall": bool(raw.get("paywall")),
                    "image_url": _public_http_url(raw.get("image_url")),
                    "url_key": _normalize_url(url),
                }
            )
        # 「不同角度」：这轮定关注点带了 diverse 的，凡是从这个方向找回来的都打上 angle。
        # 启发式：首先按子 agent 抄回的原话（query 写在标题或 summary 里难判），
        # 保守做法——diverse 只有一个方向时，「和常规关注点文本不重叠的关键词」命中的
        # 都算 diverse；判不了的一律不打（宁缺毋滥）。当前实现：标题或摘要里出现
        # diverse 查询词 ≥3 字符片段的算 diverse，全打不上时按顺序从后往前给一两名额外的，
        # 反正去同质化只留前两条。
        diverse_queries = [f["query"] for f in focus if f.get("angle") == "diverse"]
        if diverse_queries:
            q = diverse_queries[0]
            words = [w for w in _split_query_words(q) if len(w) >= 3]
            for item in items:
                haystack = f"{item['title']} {item['summary']}"
                if any(w in haystack for w in words):
                    item["angle"] = "diverse"
        return items

    async def _score(self, gid: str, settings: Settings, candidates: list[dict]) -> None:
        """给每条幸存者打五项分等信息（直接改 item）。

        模型给：info / source / relevance / timeliness / chat（1–5）、
        profile（群画像条目的编号，回文字由代码换）、topic（≤8 字）、sensitive、
        why、icon，外加三道「第一道」判断：grounded（摘要有原文依据吗）、
        junk（标题党/软文/营销号/纯情绪）、same_as_recent（和最近 14 天已出的是同一件事吗）。
        代码侧算 avg（五项平均）、profile 编号换文字（对不上 → relevance 封顶 2）、
        topic 截 8 字。模型漏给某条 → 五项记 0（第二道自然筛掉，理由走相关度）。

        编号：prompt 里按 candidates 的顺序写成 0..n-1；模型回 i 就按 i 对，
        回了 title 就对 title（编号对不上时的兜底）。
        """
        entries = self._safe_entries(gid)
        entry_texts = [str(e.get("text") or "") for e in entries[:20]]
        recent_titles = self._recent_news_titles(gid)
        mem_block = self._prompt_block_safe("memory", group_id=gid)
        lines = ([mem_block.strip()] if mem_block else []) + ["这是一个 QQ 群的画像条目（打分时要指出每条对应的条目）："]
        if entry_texts:
            for i, t in enumerate(entry_texts):
                lines.append(f"[{i}] {t}")
        else:
            lines.append("（画像还是空的；profile 就给 null）")
        lines.append("")
        if recent_titles:
            lines.append("最近 14 天已经给这个群出过的（判断「同一件事」用）：")
            lines.extend(f"- {t}" for t in recent_titles[:15])
            lines.append("")
        lines.append("下面是一批候选（按编号从 0 开始；kind=news 资讯 / guide 好文；quote 是子 agent 从原文抄的依据）：")
        for i, c in enumerate(candidates):
            kind_zh = "资讯" if c.get("kind") == "news" else "好文"
            quote = str(c.get("quote") or "")
            pub = c.get("published_raw")
            pub_text = f"，发布于 {pub}" if pub else ""
            lines.append(
                f"[{i}]（{kind_zh}）{c['title']} —— {c['summary'][:150]}（{c['url']}{pub_text}）"
                + (f" 原文依据：{quote[:150]}" if quote else "")
            )
        lines.append("")
        icon_list = "、".join(_ICONS)
        lines.append(
            "请给每条打分，只回 JSON："
            '{"scores": [{"i": 编号,'
            ' "info": 信息量 1到5（新事实、有料才高分，旧闻重炒低分）,'
            ' "source": 来源等级 1到5（官方/一手 > 权威媒体 > 个人博客 > 二手转述）,'
            ' "relevance": 和这个群的相关度 1到5（必须对着某条画像条目打）,'
            ' "timeliness": 资讯=新鲜度 1到5；好文=现在还适用吗 1到5（要核对版本/价格/接口过时没）,'
            ' "chat": 值不值得拿到群里聊 1到5,'
            ' "profile": 相关度对应的是上面哪一条画像（回它的编号；没有就说 null）,'
            ' "topic": 这条的话题标签（不超过 8 个字，同一类事给同一个标签）,'
            ' "sensitive": 政治/争议话题吗 true/false,'
            ' "grounded": 上面的摘要能在 quote/原文里找到依据吗 true/false,'
            ' "junk": 标题党/软文广告/营销号/纯情绪没事实吗 true/false,'
            ' "junk_reason": junk 是 true 的话写一个简短原因，否则空字符串,'
            ' "same_as_recent": 是不是和上面「最近出过的」某条讲的是同一件事 true/false,'
            ' "why": "为什么给这个群（一句话，只说群的事，不许点名任何群友）",'
            f' "icon": "从下面这些挑一个：{icon_list}"}}]}}'
        )
        result = await self._models.chat(
            "main",
            [{"role": "user", "content": "\n".join(lines)}],
            json_mode=True,
            purpose="feeds.score",
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
            profile_idx: int | None
            raw_profile = s.get("profile")
            try:
                profile_idx = int(raw_profile) if raw_profile is not None else None
            except (TypeError, ValueError):
                profile_idx = None
            relevance = _f15("relevance", s)
            profile_ref = ""
            if profile_idx is not None and 0 <= profile_idx < len(entry_texts) and entry_texts[profile_idx]:
                profile_ref = entry_texts[profile_idx]
            else:
                # 指不出对应画像条目 → relevance 封顶 2（这道标准写死的）
                relevance = min(relevance, 2.0)
            topic = str(s.get("topic") or "").strip().replace("\n", " ")[:_TOPIC_MAX_LEN]
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
                title = str(s.get("title") or "").strip()
                if title and title not in by_title:
                    by_title[title] = normed
                try:
                    i = int(s.get("i"))
                except (TypeError, ValueError):
                    continue
                if i in by_index:
                    continue
                by_index[i] = normed
        zero_five = {"info": 0.0, "source": 0.0, "relevance": 0.0, "timeliness": 0.0, "chat": 0.0, "avg": 0.0}
        for pos, item in enumerate(candidates):
            s = by_index.get(pos)
            s_titled = by_title.get(item["title"])
            if s_titled is not None:
                # 打分里附了标题：以标题为准（编号在「去重后跳号」时可能对不上）
                s = s_titled
            if not s:
                item["scores"] = dict(zero_five)
                item.setdefault("why", "")
                item.setdefault("icon", "newspaper")
                item.setdefault("topic", "")
                item.setdefault("sensitive", False)
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

    # ------------------------------------------------------------------
    # 身份与工作记忆（identity.py）
    # ------------------------------------------------------------------

    def _prompt_block_safe(self, kind: str, group_id: str | None = None) -> str:
        """identity.prompt_block；没有 identity / 它出错 / 内容空 → 都 ""。各注入点靠这个回落。"""
        identity = self._identity
        if identity is None:
            return ""
        try:
            out = identity.prompt_block(kind, group_id=group_id)
        except Exception:
            return ""
        return str(out or "")

    # ------------------------------------------------------------------
    # 「有人味」：写帖子（body / reason / refs / audience / keywords）
    # ------------------------------------------------------------------

    async def _bot_persona_lines(self) -> list[str]:
        """MaiBot 人设（host.config 读 bot.nickname / personality.*）；读不到就 []。"""
        if self._host is None:
            return []
        got: dict[str, str] = {}
        for key in ("bot.nickname", "personality.personality", "personality.reply_style"):
            try:
                val = await self._host.config(key)
            except Exception:
                val = None
            if val is not None and str(val).strip():
                got[key] = str(val).strip()
        if not got:
            return []
        lines = ["你（MaiBot）的人设，写正文和原因都按这个口吻："]
        name = got.get("bot.nickname")
        if name:
            lines.append(f"- 名字：{name}")
        personality = got.get("personality.personality")
        if personality:
            lines.append(f"- 人格：{personality}")
        style = got.get("personality.reply_style")
        if style:
            lines.append(f"- 说话风格：{style}")
        return lines

    def _quotes_for_item(self, gid: str, item: dict) -> list[dict]:
        """按候选关键词查本群相关原话（最多 _SEARCH_QUOTES 条，带时间和名字）。"""
        from .chatlog import search_chat

        queries = [item.get("title") or "", item.get("topic") or ""]
        queries = [str(q).strip() for q in queries if str(q or "").strip()]
        seen: set[str] = set()
        out: list[dict] = []
        for q in queries:
            for hit in search_chat(self._store, gid, q, days=14, limit=_SEARCH_QUOTES):
                mid = str(hit.get("message_id") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                out.append(hit)
                if len(out) >= _SEARCH_QUOTES:
                    return out
        return out

    async def _write_posts(self, gid: str, items: list[dict], settings: Settings) -> None:
        """对过第二道门槛的每条写帖子（一次模型调用写完全部；失败的条目回落原文）。

        直接改 item：item["post"] = {"body","reason","refs","audience","keywords"}。
        """
        if not items:
            return
        # 每条的素材：候选本身 + 群原话
        per_item: list[dict] = []
        for item in items:
            site = str(item.get("site") or _site_of(item["url"]))
            quotes = self._quotes_for_item(gid, item)
            per_item.append({"item": item, "site": site, "quotes": quotes})

        lines: list[str] = []
        # 有 SOUL（identity.py）→ 用「## MaiWork 的身份」这一份；没有才回落老的人设行
        soul_block = self._prompt_block_safe("soul")
        if soul_block:
            lines.append(soul_block.strip())
            lines.append(f"写正文和原因都按上面「MaiWork 的身份」的口吻。")
            lines.append("")
        else:
            persona = await self._bot_persona_lines()
            if persona:
                lines.extend(persona)
                lines.append("")
        pref = self.pref(gid)
        if pref:
            lines.append(f"管理员对这个群的资讯偏好：{pref}")
            lines.append("")
        entries = self._safe_entries(gid)
        if entries:
            lines.append("群画像条目（写「我发这条的原因」时对得上哪条就说哪条）：")
            for e in entries[:20]:
                lines.append(f"- [{e.get('category', '')}] {e.get('text', '')}")
            lines.append("")
        lines.append("下面是这轮要给这个群发的内容（每条附候选信息和本群里聊过的相关原话）：")
        for i, pack in enumerate(per_item):
            item = pack["item"]
            kind_zh = "资讯" if item.get("kind") == "news" else "好文"
            lines.append(
                f"[{i}]（{kind_zh}）{item['title']} —— {str(item['summary'] or '')[:150]}"
                f"（来源 {pack['site']}，{item['url']}）"
            )
            quote = str(item.get("quote") or "")[:150]
            if quote:
                lines.append(f"    原文依据：{quote}")
            if pack["quotes"]:
                lines.append("    本群聊过的相关原话（编号从 1 开始，写 reason 和 audience 只能用这些）：")
                for j, hit in enumerate(pack["quotes"], 1):
                    stamp = clock.bj(float(hit["ts"])).strftime("%m-%d %H:%M")
                    lines.append(f"      ({j}) {stamp} {hit['who']}: {str(hit['text'])[:80]}")
            else:
                lines.append("    （本群最近没搜到相关原话：reason 写泛一点，refs 给空，audience 给空）")
            lines.append("")
        lines.append(
            "给每条写帖子，只回 JSON："
            '{"posts": [{"i": 编号, "title": "对应条目标题",'
            ' "body": "按 MaiBot 口吻写的正文 2–5 句，像跟熟人讲；关键处可用 [文字](https://链接)'
            ' 嵌原文链接，只许 http(s) 链接,'
            ' "reason": "我发这条的原因，第一人称；落到群里真实聊过的事和时间'
            '（能从原话找到依据就点出来），找不到依据就写泛一点，不许编,'
            ' "refs": [引用到的群原话编号（整数，只能用上面给的那几条）],'
            ' "audience": ["可能需要的群友名字，只能来自上面给出的原话发言人"],'
            ' "keywords": ["5–10 个关键词，中英文、同义词都放点"]}]}'
        )
        try:
            result = await self._models.chat(
                "main",
                [{"role": "user", "content": "\n".join(lines)}],
                json_mode=True,
                purpose="feeds.post",
                group_id=gid,
            )
            data = json.loads(result.text)
        except (ModelError, ValueError) as e:
            logger.info("写帖子失败（群 %s）：%s；全部回落原文", gid, e)
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
            item["post"] = self._clean_post(gid, raw, pack["quotes"], item)

    def _post_fallback(self, item: dict) -> None:
        """写帖子失败 / 漏了这条的回落：body=summary、reason=why、refs/audience 空、
        keywords 用话题 + 标题粗拼的兜底，image_url 留着。"""
        keywords: list[str] = []
        for k in [str(item.get("topic") or ""), *_split_query_words(str(item.get("title") or ""))]:
            k = k.strip()
            if k and k not in keywords:
                keywords.append(k)
            if len(keywords) >= _KEYWORDS_MAX:
                break
        item["post"] = {
            "body": str(item.get("summary") or ""),
            "reason": str(item.get("why") or ""),
            "refs": [],
            "audience": [],
            "keywords": keywords[:_KEYWORDS_MAX],
        }

    def _clean_post(self, gid: str, raw: dict, quotes: list[dict], item: dict) -> dict:
        """把模型写的帖子洗净：refs 序号→真实原话、audience 只留白名单、链接只留 http(s) ≤4、
        reason/body/audience 过隐私闸（按片段规则）。闸掉整体回落原文。"""
        body = str(raw.get("body") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not body:
            body = str(item.get("summary") or "")
        if not reason:
            reason = str(item.get("why") or "")
        body = _clean_body_links(body, _BODY_LINK_MAX)
        # refs：序号（1..len(quotes)）换真实原话
        refs_out: list[dict] = []
        raw_refs = raw.get("refs")
        if isinstance(raw_refs, list):
            for x in raw_refs:
                try:
                    idx = int(x)
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(quotes):
                    hit = quotes[idx - 1]
                    entry = {
                        "ts": float(hit["ts"]),
                        "who": str(hit["who"]),
                        "text": str(hit["text"])[:_REF_TEXT_MAX],
                        "message_id": str(hit["message_id"]),
                    }
                    if entry["message_id"] not in {r["message_id"] for r in refs_out}:
                        refs_out.append(entry)
        # audience：只留确实出现在引用原话里的名字
        speakers = {str(h["who"]) for h in quotes}
        ref_speakers = {r["who"] for r in refs_out}
        audience: list[str] = []
        raw_aud = raw.get("audience")
        if isinstance(raw_aud, list):
            for name in raw_aud:
                n = str(name or "").strip()
                if n and n in ref_speakers and n not in audience:
                    audience.append(n)
                if len(audience) >= _AUDIENCE_MAX:
                    break
        # keywords
        keywords: list[str] = []
        raw_kw = raw.get("keywords")
        if isinstance(raw_kw, list):
            for k in raw_kw:
                kw = str(k or "").strip()
                if kw and kw not in keywords:
                    keywords.append(kw)
                if len(keywords) >= _KEYWORDS_MAX:
                    break
        # 隐私闸：reason / body / audience 每个名字（按 note/persona 片段规则；名字本身放行）
        if self._scrub_item_text(gid, body) is None or self._scrub_item_text(gid, reason) is None:
            logger.info("帖子含关注成员片段，回落原文（群 %s 条 %r）", gid, item.get("title", "")[:30])
            return self._fallback_post_dict(item)
        audience = [n for n in audience if self._scrub_item_text(gid, n) is not None]
        return {
            "body": body,
            "reason": reason[:_REASON_MAX],
            "refs": refs_out,
            "audience": audience,
            "keywords": keywords,
        }

    def _fallback_post_dict(self, item: dict) -> dict:
        out_item = dict(item)
        self._post_fallback(out_item)
        return out_item["post"]

    # ------------------------------------------------------------------
    # 实测（railway.new 一次性 VM，docs/02 §4.1「实测过再发是加分项，不是门槛」）
    # ------------------------------------------------------------------

    async def _plan_and_verify(self, gid: str, items: list[dict], settings: Settings) -> None:
        """挑值得实测的条目 → 交给注入的 verify_runner 去一次性 VM 里跑。

        - [environments] railway=false、没接 verify_runner → 整个跳过（不多调一次模型）。
        - 主模型挑中 0 条 → 跳过；挑多了夹回 [environments] verify_per_round（默认 2）。
        - 实测本身由 verify_runner 干（真机上接 run_railway_verify；测试注入假的），
          直接改写 items[i]["verify"]；runner 拿不到机器就什么都不写，照常入库。
        """
        env_cfg = getattr(settings, "environments", None)
        if env_cfg is None or not bool(getattr(env_cfg, "railway", True)):
            return
        runner = self._verify_runner
        if runner is None:
            return
        per_round = max(1, int(getattr(env_cfg, "verify_per_round", 2) or 2))
        try:
            picks = await self._plan_verify_picks(gid, items, per_round)
        except (ModelError, ValueError) as e:
            logger.info("挑实测条目失败（群 %s）：%s；这轮跳过实测", gid, e)
            return
        if not picks:
            return
        await runner(items, picks, gid, settings)

    async def _plan_verify_picks(self, gid: str, items: list[dict], cap: int) -> list[dict]:
        """主模型（json_mode）挑「值得实测」的条目，回 [{"index","title","what","expect"}]。

        值得实测 = 工具 / 命令 / 开源项目 / 一行命令就能复现的说法；
        要注册账号、填密钥、登录才能测的一律不挑。
        """
        lines = [
            "这轮要给群发的内容筛好了（已过质量关）。哪几条值得在一台一次性 Linux VM 里真试一下？",
            "只挑「工具 / 命令 / 开源项目 / 『一行命令就能……』」这种能当场复现验证的；",
            "要注册账号、填密钥、登录、花钱才能测的一律不挑；新闻事件、观点文章也没法测。",
            f"最多挑 {cap} 条。没有值得测的就回 {{\"verify\": null}}。",
            "",
        ]
        for i, item in enumerate(items):
            kind_zh = "资讯" if item.get("kind") == "news" else "好文"
            avg = float((item.get("scores") or {}).get("avg") or 0.0)
            lines.append(
                f"[{i}]（{kind_zh}，{avg:.1f} 分）{item['title']} —— {str(item.get('summary') or '')[:120]}"
                f"（{item['url']}）"
            )
        lines.append("")
        lines.append(
            "只回 JSON：{\"verify\": [{\"i\": 编号, \"title\": \"条目原标题\", "
            "\"what\": \"要验证什么（一句话）\", \"expect\": \"预期看到什么结果（一句话）\"}]}"
        )
        result = await self._models.chat(
            "main",
            [{"role": "user", "content": "\n".join(lines)}],
            json_mode=True,
            purpose="feeds.verify_plan",
            group_id=gid,
        )
        data = json.loads(result.text)
        raw = data.get("verify") if isinstance(data, dict) else None
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError("verify 不是列表")
        picks: list[dict] = []
        seen: set[int] = set()
        for p in raw:
            if not isinstance(p, dict):
                continue
            try:
                idx = int(p.get("i"))
            except (TypeError, ValueError):
                continue
            if idx in seen or not (0 <= idx < len(items)):
                continue
            what = str(p.get("what") or "").strip()
            if not what:
                continue  # 「要验证什么」都说不出来就不算计划
            seen.add(idx)
            picks.append(
                {
                    "index": idx,
                    "title": str(p.get("title") or items[idx]["title"]),
                    "what": what[:200],
                    "expect": str(p.get("expect") or "").strip()[:200],
                }
            )
            if len(picks) >= cap:
                break
        return picks

    # ------------------------------------------------------------------
    # 资讯偏好
    # ------------------------------------------------------------------

    @staticmethod
    def _pref_key(gid: str) -> str:
        return f"feeds.pref.{gid}"

    def pref(self, gid: str) -> str:
        """kv["feeds.pref.<群号>"] 的一句话偏好；没有 → ""。"""
        try:
            raw = self._store.kv_get(self._pref_key(gid), "")
        except Exception:
            return ""
        return str(raw or "")[:_PREF_MAX]

    def set_pref(self, gid: str, text: str) -> str:
        """管理员写偏好；截 300 字，返回存下去的值。"""
        text = str(text or "").strip()[:_PREF_MAX]
        with self._store.tx() as conn:
            self._store.kv_set(conn, self._pref_key(str(gid)), text)
        return text

    # ------------------------------------------------------------------
    # 「想在群里聊」投票
    # ------------------------------------------------------------------

    def admin_chat_vote(self, gid: str, item_id: int) -> dict:
        """本群条目 chat_votes +1；够 _CHAT_VOTE_MIN 票时补进话题候选池（候选 ttl 24 小时）。

        网页没有身份，谁点都只计数（前端自己防重复）。返回 {"chat_votes": n}。
        """
        gid = str(gid)
        iid = int(item_id)
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT id, group_id, chat_votes, kind, sensitive, published_ts, scores,"
                " title, body, summary, sources, rejected"
                " FROM news_items WHERE id=?",
                (iid,),
            ).fetchone()
            if row is None or str(row["group_id"]) != gid or int(row["rejected"] or 0):
                raise KeyError(f"找不到这条资讯：#{iid}")
            votes = int(row["chat_votes"] or 0) + 1
            conn.execute("UPDATE news_items SET chat_votes=? WHERE id=?", (votes, iid))
        # 够票 → 补进候选池（只补一次：mark 一条 event，下一次不重复进）
        if votes >= _CHAT_VOTE_MIN:
            self._maybe_pool_from_votes(gid, row, votes)
        return {"chat_votes": votes}

    def _maybe_pool_from_votes(self, gid: str, row: Any, votes: int) -> None:
        """够了票还没进过池的资讯，补进候选池（候选 ttl 24h；同一条只补一次）。"""
        iid = int(row["id"])
        marker = f"候选池补进（投票到 {votes}）"
        try:
            with self._store.tx() as conn:
                hit = conn.execute(
                    "SELECT id FROM topic_candidates WHERE group_id=? AND kind='news' AND ref_id=?",
                    (gid, iid),
                ).fetchone()
            if hit is not None:
                return  # 已经在池里（备料时自己进的也算）
            try:
                scores = json.loads(row["scores"] or "{}")
            except (ValueError, TypeError):
                scores = {}
            item = {
                "kind": str(row["kind"] or "news"),
                "sensitive": bool(row["sensitive"] or 0),
                "published_ts": row["published_ts"],
                "scores": scores,
                "chat_votes": votes,
                "title": str(row["title"] or ""),
                "body": str(row["body"] or ""),
                "summary": str(row["summary"] or ""),
            }
            ok, _voted = self._pool_eligible_check(item, 0.0, clock.now())
            if not ok:
                logger.info("投票够 %s 票但不满足进池的硬条件（48 小时/非争议），不进（群 %s 条 %s）",
                            votes, gid, iid)
                return
            url = ""
            try:
                src = json.loads(row["sources"] or "[]")
                if isinstance(src, list) and src and isinstance(src[0], dict):
                    url = str(src[0].get("url") or "")
            except (ValueError, TypeError):
                url = ""
            self._topics.add_candidate(
                gid, kind="news", ref_id=iid, title=item["title"],
                brief=self._pool_brief(item), link=url, ttl_h=_CHAT_VOTE_TTL_H,
            )
            logger.info("%s：群 %s 条 %s 进候选池", marker, gid, iid)
        except Exception:
            logger.info("投票补进候选池失败（群 %s 条 %s）", gid, iid, exc_info=True)

    def _skipped_batch(self, gid: str, note: str, *, found: int = 0) -> None:
        now = clock.now()
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                    " VALUES (?, ?, ?, 0, 1, ?, ?)",
                    (gid, now, int(found), str(note)[:300], now),
                )
        except Exception:
            logger.exception("记 skipped 批次失败（群 %s）", gid)

    def _recent_feedback_titles(self, gid: str) -> dict[str, list[str]]:
        since = clock.now() - _FEEDBACK_SCAN_DAYS * 86400.0
        rows = self._store.read().execute(
            "SELECT title, up, down FROM news_items WHERE group_id=? AND created>=?"
            " AND (up > 0 OR down > 0)",
            (gid, since),
        ).fetchall()
        up = [str(r["title"]) for r in rows if int(r["up"]) > int(r["down"])]
        down = [str(r["title"]) for r in rows if int(r["down"]) > int(r["up"])]
        return {"up": up, "down": down}

    # ------------------------------------------------------------------
    # 构想
    # ------------------------------------------------------------------

    async def make_idea(self, group_id: str) -> int | None:
        gid = str(group_id)
        if not self._profile_ready(gid):
            return None
        try:
            if not self._models_ready():
                return None
        except Exception:
            return None
        entries = self._safe_entries(gid)
        recent_ideas = self._recent_idea_titles(gid)
        recent_news = self._recent_news_titles(gid)

        # 构想写法（SOUL）+ 记忆（全局 + 本群）：有 SOUL/记忆就带进去
        prefix_parts: list[str] = []
        soul_block = self._prompt_block_safe("soul")
        if soul_block:
            prefix_parts.append(soul_block.strip())
        mem_block = self._prompt_block_safe("memory", group_id=gid)
        if mem_block:
            prefix_parts.append(mem_block.strip())
        lines = ([p for p in prefix_parts] if prefix_parts else []) + ["这是一个 QQ 群的画像要点："]
        for e in entries[:25]:
            lines.append(f"- [{e.get('category', '')}] {e.get('text', '')}")
        if recent_news:
            lines.append("")
            lines.append("最近给这个群找过的资讯（感受一下方向）：")
            lines.extend(f"- {t}" for t in recent_news[:10])
        if recent_ideas:
            lines.append("")
            lines.append("最近已经提过的构想（别再提类似的）：")
            lines.extend(f"- {t}" for t in recent_ideas[:20])
        lines.append("")
        icon_list = "、".join(_ICONS)
        lines.append(
            "想一个值得这个群试试的点子，想不到合适的就 null。只回 JSON："
            '{"idea": {"title": "我可以……（一句话）", "body": "想法是什么（两三句）",'
            ' "basis": "为什么适合这个群（引用画像，不点名群友）", "step": "第一步怎么做",'
            ' "effort": "大概要多少功夫",'
            f' "icon": "从下面这些挑一个：{icon_list}",'
            ' "chat_worthy": 适不适合拿到群里聊一聊 true/false,'
            ' "feasibility": {"level": "ok"|"maybe"|"need", "note": "一句话：'
            '能做 / 可能能做 / 需要你提供什么"},'
            ' "keywords": ["5–10 个关键词，中英文、同义词都放点"]} | null}'
            "。构想不吹牛：level 只许这三个——ok=我真能做，maybe=可能能做，need=还需要群里提供什么"
            "（note 里写清楚需要什么）。"
        )
        try:
            result = await self._models.chat(
                "main",
                [{"role": "user", "content": "\n".join(lines)}],
                json_mode=True,
                purpose="feeds.idea",
                group_id=gid,
            )
            data = json.loads(result.text)
        except (ModelError, ValueError) as e:
            logger.info("出构想失败（群 %s）：%s", gid, e)
            return None
        idea = data.get("idea") if isinstance(data, dict) else None
        if not isinstance(idea, dict):
            return None
        title = str(idea.get("title") or "").strip()
        if not title:
            return None
        if any(_similar(title, t) >= _IDEA_DEDUP_RATIO for t in recent_ideas):
            return None
        # G7 隐私闸：body / basis 含关注成员名字 / 注记 → 这条构想整条丢弃（群友可见）
        body_s = str(idea.get("body") or "").strip()
        basis_s = str(idea.get("basis") or "").strip()
        if self._scrub_item_text(gid, body_s) is None or self._scrub_item_text(gid, basis_s) is None:
            logger.info("构想含关注成员信息，整条丢弃（群 %s）", gid)
            return None
        icon = str(idea.get("icon") or "").strip()
        if icon not in _ICONS:
            icon = "bulb"
        # 可行性：level 只认 ok / maybe / need；没给或乱给 → maybe
        raw_feas = idea.get("feasibility")
        feas_level, feas_note = "maybe", ""
        if isinstance(raw_feas, dict):
            lv = str(raw_feas.get("level") or "").strip().lower()
            if lv in ("ok", "maybe", "need"):
                feas_level = lv
            feas_note = str(raw_feas.get("note") or "").strip()[:120]
        feasibility_json = json.dumps({"level": feas_level, "note": feas_note}, ensure_ascii=False)
        keywords: list[str] = []
        raw_kw = idea.get("keywords")
        if isinstance(raw_kw, list):
            for k in raw_kw:
                kw = str(k or "").strip()
                if kw and kw not in keywords:
                    keywords.append(kw)
                if len(keywords) >= _KEYWORDS_MAX:
                    break
        keywords_json = json.dumps(keywords, ensure_ascii=False)
        now = clock.now()
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
                " requested_by, task_id, up, down, created, updated, feasibility, keywords)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'new', NULL, NULL, 0, 0, ?, ?, ?, ?)",
                (
                    gid, icon, title,
                    str(idea.get("body") or "").strip(),
                    str(idea.get("basis") or "").strip(),
                    str(idea.get("step") or "").strip(),
                    str(idea.get("effort") or "").strip(),
                    now, now, feasibility_json, keywords_json,
                ),
            )
            idea_id = int(cur.lastrowid or 0)
        if bool(idea.get("chat_worthy")):
            body = str(idea.get("body") or "").strip()
            try:
                self._topics.add_candidate(
                    gid, kind="idea", ref_id=idea_id, title=title,
                    brief=(body or title)[:120], link="",
                )
            except Exception:
                logger.exception("构想进话题候选池失败（群 %s 条 %s）", gid, idea_id)
        return idea_id

    def _recent_idea_titles(self, gid: str) -> list[str]:
        since = clock.now() - _IDEA_DEDUP_DAYS * 86400.0
        rows = self._store.read().execute(
            "SELECT title FROM ideas WHERE group_id=? AND created>=? ORDER BY created DESC",
            (gid, since),
        ).fetchall()
        return [str(r["title"]) for r in rows if r["title"]]

    def _recent_news_titles(self, gid: str) -> list[str]:
        since = clock.now() - _FEEDBACK_SCAN_DAYS * 86400.0
        rows = self._store.read().execute(
            "SELECT title FROM news_items WHERE group_id=? AND created>=? ORDER BY created DESC LIMIT 10",
            (gid, since),
        ).fetchall()
        return [str(r["title"]) for r in rows if r["title"]]

    # ------------------------------------------------------------------
    # 反馈
    # ------------------------------------------------------------------

    def feedback(self, kind: str, item_id: int, value: str | None, prev: str | None) -> dict:
        if kind not in ("news", "ideas"):
            raise ValueError(f"feedback 的 kind 只认 news / ideas，收到 {kind!r}")
        for v in (value, prev):
            if v is not None and v not in ("up", "down"):
                raise ValueError(f"反馈值只认 up / down / 空，收到 {v!r}")
        table = "news_items" if kind == "news" else "ideas"
        iid = int(item_id)
        with self._store.tx() as conn:
            row = conn.execute(f"SELECT up, down FROM {table} WHERE id=?", (iid,)).fetchone()
            if row is None:
                raise KeyError(f"找不到这条：{kind} #{iid}")
            up, down = int(row["up"]), int(row["down"])
            if prev == "up":
                up = max(0, up - 1)
            elif prev == "down":
                down = max(0, down - 1)
            if value == "up":
                up += 1
            elif value == "down":
                down += 1
            conn.execute(f"UPDATE {table} SET up=?, down=? WHERE id=?", (up, down, iid))
        return {"up": up, "down": down}

    # ------------------------------------------------------------------
    # 构想操作
    # ------------------------------------------------------------------

    def idea_action(self, idea_id: int, op: str, *, by: str) -> dict:
        if op not in ("want", "do", "dismiss"):
            raise ValueError(f"构想操作只认 want / do / dismiss，收到 {op!r}")
        iid = int(idea_id)
        by = str(by or "").strip()
        allowed = {"new": ("want", "do", "dismiss"), "wanted": ("do", "dismiss")}
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT state, requested_by FROM ideas WHERE id=?", (iid,)
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到这个构想：#{iid}")
            state = str(row["state"])
            if op not in allowed.get(state, ()):  # started / dismissed 等不再接受任何操作
                state_zh = {
                    "wanted": "已经有人想要了", "started": "已经在做了",
                    "dismissed": "这条已经收起了", "pending": "这条在等批准",
                }.get(state, f"当前是 {state}")
                op_zh = {"want": "想要", "do": "开工", "dismiss": "收起"}[op]
                raise ValueError(f"{state_zh}，不能{op_zh}")
            now = clock.now()
            if op == "want":
                conn.execute(
                    "UPDATE ideas SET state='wanted', requested_by=?, updated=? WHERE id=?",
                    (by, now, iid),
                )
            elif op == "do":
                conn.execute(
                    "UPDATE ideas SET state='started', updated=?,"
                    " requested_by=COALESCE(requested_by, NULLIF(?, '')) WHERE id=?",
                    (now, by, iid),
                )
            else:
                conn.execute("UPDATE ideas SET state='dismissed', updated=? WHERE id=?", (now, iid))
        view = self._idea_one(iid)
        if op == "do" and self.on_start is not None:
            # 回调抛错不影响状态（只记日志）
            try:
                self.on_start(view)
            except Exception:
                logger.exception("on_start 回调出错（构想 %s）", iid)
        return view

    def _idea_one(self, idea_id: int) -> dict:
        row = self._store.read().execute(
            "SELECT * FROM ideas WHERE id=?", (int(idea_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"找不到这个构想：#{idea_id}")
        return self._idea_row_to_view(row)

    # ------------------------------------------------------------------
    # 网页视图
    # ------------------------------------------------------------------

    def _batches(self, group_id: str, days: int) -> list:
        """群资讯视图要看的批次：略过个人向批次（note 以 personal.py 的前缀开头）。"""
        gid = str(group_id)
        since = clock.now() - max(1, int(days)) * 86400.0
        return self._store.read().execute(
            "SELECT * FROM news_batches WHERE group_id=? AND created>=? AND note NOT LIKE 'personal:%'"
            " ORDER BY created DESC, id DESC",
            (gid, since),
        ).fetchall()

    def news_view(self, group_id: str, *, days: int = _NEWS_VIEW_DAYS, admin: bool = False) -> list[dict]:
        """资讯栏（§9.3）：只含通过三道门的 kind=news 条目（好文走 guides_view、被筛的只管理员可见）。

        批次带 rejected_count（所有人可见，被筛掉几条）；admin=True 时多一个
        rejected 列表：[{id,title,url,site,gate,reason,avg}]。
        群友视图（admin=False）去掉每条 item 的 keywords（只给管理员）。
        """
        gid = str(group_id)
        # 个人向批次的 note 前缀（personal.py）；群资讯视图一律略过（个人向不上群视图）
        batches = self._batches(group_id, days)
        out: list[dict] = []
        now = clock.now()
        for b in batches:
            rows = self._store.read().execute(
                "SELECT * FROM news_items WHERE batch_id=? AND rejected=0 AND kind='news'"
                " AND target_user_id=''"
                " ORDER BY score DESC, id ASC",
                (int(b["id"]),),
            ).fetchall()
            items = [self._news_row_to_view(r, now) for r in rows]
            if not admin:
                for it in items:
                    it.pop("keywords", None)
            rejected_rows = self._store.read().execute(
                "SELECT id, title, url_key, sources, reject_gate, reject_reason, score"
                " FROM news_items WHERE batch_id=? AND rejected=1 ORDER BY id ASC",
                (int(b["id"]),),
            ).fetchall()
            entry = {
                "id": int(b["id"]),
                "slot_ts": float(b["slot_ts"]),
                "found": int(b["found"]),
                "kept": int(b["kept"]),
                "skipped": bool(b["skipped"]),
                "note": str(b["note"] or ""),
                "rejected_count": len(rejected_rows),
                "items": items,
            }
            if admin:
                entry["rejected"] = [self._rejected_row_to_view(r) for r in rejected_rows]
            out.append(entry)
        return out

    def _rejected_row_to_view(self, r: Any) -> dict:
        url, site = "", ""
        try:
            src = json.loads(r["sources"] or "[]")
            if isinstance(src, list) and src and isinstance(src[0], dict):
                url = str(src[0].get("url") or "")
                site = str(src[0].get("site") or "")
        except (ValueError, TypeError):
            pass
        return {
            "id": int(r["id"]),
            "title": str(r["title"]),
            "url": url,
            "site": site,
            "gate": str(r["reject_gate"] or ""),
            "reason": str(r["reject_reason"] or ""),
            "avg": float(r["score"] or 0.0),
        }

    def _news_row_to_view(self, r: Any, now: float) -> dict:
        status_kind = str(r["status_kind"] or "new")
        expires_ts = r["expires_ts"]
        if status_kind == "pool" and expires_ts is not None and float(expires_ts) < now:
            status_kind = "expired"
        try:
            sources = json.loads(r["sources"] or "[]")
        except (ValueError, TypeError):
            sources = []
        sources = [s for s in sources if isinstance(s, dict)]
        published = r["published_ts"]
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
        refs_out: list[dict] = []
        try:
            refs_raw = json.loads(r["refs"] or "[]")
            if isinstance(refs_raw, list):
                for x in refs_raw:
                    if not isinstance(x, dict):
                        continue
                    refs_out.append(
                        {
                            "ts": float(x.get("ts") or 0.0),
                            "who": str(x.get("who") or ""),
                            "text": str(x.get("text") or "")[:_REF_TEXT_MAX],
                            "message_id": str(x.get("message_id") or ""),
                        }
                    )
        except (ValueError, TypeError):
            refs_out = []
        audience_out: list[str] = []
        try:
            aud_raw = json.loads(r["audience"] or "[]")
            if isinstance(aud_raw, list):
                audience_out = [str(x) for x in aud_raw if str(x or "").strip()]
        except (ValueError, TypeError):
            audience_out = []
        keywords_out: list[str] = []
        try:
            kw_raw = json.loads(r["keywords"] or "[]")
            if isinstance(kw_raw, list):
                keywords_out = [str(x) for x in kw_raw if str(x or "").strip()]
        except (ValueError, TypeError):
            keywords_out = []
        verify_out: Any = None
        raw_verify = str(r["verify"] or "").strip()
        if raw_verify:
            try:
                verify_out = json.loads(raw_verify)
            except (ValueError, TypeError):
                verify_out = None
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
            "sensitive": bool(r["sensitive"] or 0),
            "profile_ref": str(r["profile_ref"] or ""),
            "body": str(r["body"] or ""),
            "reason": str(r["reason"] or ""),
            "refs": refs_out,
            "audience": audience_out,
            "image_url": str(r["image_url"] or ""),
            "keywords": keywords_out,
            "verify": verify_out,
            "chat_votes": int(r["chat_votes"] or 0),
            "angle": str(r["angle"] or ""),
            "status": {
                "kind": status_kind,
                "at": float(r["status_at"]) if r["status_at"] is not None else None,
                "replies": int(r["replies"] or 0),
                "expires_ts": float(expires_ts) if expires_ts is not None else None,
            },
            "feedback": {"up": int(r["up"] or 0), "down": int(r["down"] or 0)},
        }

    def guides_view(self, group_id: str, *, days: int = _GUIDES_VIEW_DAYS, admin: bool = False) -> list[dict]:
        """好文专栏（§9.3）：最近 30 天通过三道门的 kind=guide 条目，最多 20 条，新的在前。

        结构和 news item 一致（scores/topic/sensitive/profile_ref/status/feedback 都有，
        也带 body/reason/refs/audience/image_url/keywords/verify/chat_votes/angle）。
        群友视图（admin=False）去掉每条的 keywords（只给管理员）。
        """
        gid = str(group_id)
        since = clock.now() - max(1, int(days)) * 86400.0
        rows = self._store.read().execute(
            "SELECT * FROM news_items WHERE group_id=? AND rejected=0 AND kind='guide'"
            " AND created>=? AND target_user_id='' ORDER BY created DESC, id DESC LIMIT ?",
            (gid, since, _GUIDES_VIEW_CAP),
        ).fetchall()
        now = clock.now()
        out = [self._news_row_to_view(r, now) for r in rows]
        if not admin:
            for it in out:
                it.pop("keywords", None)
        return out

    def ideas_view(self, group_id: str, *, admin: bool = False) -> list[dict]:
        """构想页（群友、管理员都看得到，含「给某个关注成员的」构想，每条带 target_user_id）。

        个人向构想的 basis 是个人画像摘要，只给管理员：admin=False 时清空。"""
        gid = str(group_id)
        cutoff = clock.now() - _IDEAS_DISMISSED_KEEP_DAYS * 86400.0
        rows = self._store.read().execute(
            "SELECT * FROM ideas WHERE group_id=?"
            " AND (state != 'dismissed' OR updated >= ?)"
            " ORDER BY created DESC, id DESC LIMIT ?",
            (gid, cutoff, _IDEAS_VIEW_CAP),
        ).fetchall()
        out = []
        for r in rows:
            v = self._idea_row_to_view(r)
            v["target_user_id"] = str(r["target_user_id"] or "")
            if v["target_user_id"] and not admin:
                v["basis"] = ""
            out.append(v)
        return out

    def _idea_row_to_view(self, r: Any) -> dict:
        feasibility: dict[str, str] = {"level": "maybe", "note": ""}
        try:
            raw = json.loads(r["feasibility"] or "")
            if isinstance(raw, dict):
                lv = str(raw.get("level") or "").strip().lower()
                if lv in ("ok", "maybe", "need"):
                    feasibility["level"] = lv
                feasibility["note"] = str(raw.get("note") or "")
        except (ValueError, TypeError):
            pass
        keywords: list[str] = []
        try:
            raw_kw = json.loads(r["keywords"] or "[]")
            if isinstance(raw_kw, list):
                keywords = [str(x) for x in raw_kw if str(x or "").strip()]
        except (ValueError, TypeError):
            keywords = []
        return {
            "id": int(r["id"]),
            "icon": str(r["icon"] or "bulb"),
            "title": str(r["title"]),
            "body": str(r["body"] or ""),
            "basis": str(r["basis"] or ""),
            "step": clean_step(r["step"]),
            "effort": str(r["effort"] or ""),
            "state": str(r["state"] or "new"),
            "requested_by": str(r["requested_by"]) if r["requested_by"] else None,
            "task_id": str(r["task_id"]) if r["task_id"] else None,
            "created_ts": float(r["created"] or 0),
            "feedback": {"up": int(r["up"] or 0), "down": int(r["down"] or 0)},
            "feasibility": feasibility,
            "keywords": keywords,
        }

    def today_count(self, group_id: str) -> int:
        """今天（北京时间）入选的资讯条数。"""
        gid = str(group_id)
        start = (
            clock.bj(clock.now()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM news_items WHERE group_id=? AND created>=?",
            (gid, start),
        ).fetchone()
        return int(row["c"] or 0)

    # ------------------------------------------------------------------
    # 前提检查
    # ------------------------------------------------------------------

    def _profile_ready(self, gid: str) -> bool:
        row = self._store.read().execute(
            "SELECT profile_ready_ts FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        return row is not None and float(row["profile_ready_ts"] or 0) > 0

    def _models_ready(self) -> bool:
        return bool(self._models.settings().ready())

    async def _ensure_search(self) -> None:
        """确认搜索可用（2026-10：只看绑定状态，不真发探活请求）。

        - self._search 为 Search（search.py）：用它的 `available()` 判——有绑定且扩展已启用、
          连得上、工具在才算可用；不行抛 SearchUnavailable（中文提示去 设置 → 扩展 绑定）；
        - 为 None（没注入）：当可用，由子 agent 的 web_search 工具自己兜底；
        - 注入了别的带 `search()` 的对象（测试假搜索）：照旧调一次 search() 自检
          （假搜索自己决定抛不抛 SearchUnavailable；现有测试桩依赖这个行为）。
        """
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

    def _scrub_item_text(self, gid: str, text: str) -> str | None:
        """G7 隐私闸：资讯 why / 构想 body·basis 要出库（群友可见），不能含关注成员信息。"""
        from .privacy import scrub

        return scrub(gid, text, self._store)

    def _safe_entries(self, gid: str) -> list[dict]:
        try:
            entries = self._profiles.entries(gid)
        except Exception:
            logger.exception("读画像条目失败（群 %s）", gid)
            return []
        return list(entries or [])


# ----------------------------------------------------------------------
# 实测编排：run_railway_verify（app 接线时注入给 Feeds.verify_runner）
# ----------------------------------------------------------------------

_VERIFY_TOOLS = ["vm_run", "vm_put_file", "vm_read_file"]


def _verify_dict(status: str, summary: str, steps: list[str], started_ts: float) -> dict:
    """news_items.verify 的统一结构：{"status","summary","steps"(≤6 条每条≤80字),"minutes","ts"}。"""
    elapsed = max(0.0, clock.now() - float(started_ts))
    return {
        "status": "passed" if status == "passed" else "failed",
        "summary": str(summary or "").strip()[:_VERIFY_SUMMARY_MAX],
        "steps": [str(s).strip()[:_VERIFY_STEP_MAX_LEN] for s in steps if str(s).strip()][:_VERIFY_STEPS_MAX],
        "minutes": max(1, int(round(elapsed / 60.0))),
        "ts": float(clock.now()),
    }


def _verify_from_report(report: Any, started_ts: float) -> dict:
    """子 agent 交回的 WorkerReport → verify dict。ok=False / status 不认识一律 failed。"""
    if not getattr(report, "ok", False):
        err = str(getattr(report, "error", "") or "").strip()
        summary = f"没测完：{err}" if err else "没测完：子 agent 没交回结果"
        return _verify_dict("failed", summary, [], started_ts)
    data = getattr(report, "data", None)
    data = data if isinstance(data, dict) else {}
    status = "passed" if str(data.get("status") or "") == "passed" else "failed"
    summary = str(data.get("summary") or getattr(report, "summary", "") or "")
    raw_steps = data.get("steps")
    steps = [str(s) for s in raw_steps] if isinstance(raw_steps, list) else []
    return _verify_dict(status, summary, steps, started_ts)


async def run_railway_verify(
    items: list[dict],
    plans: list[dict],
    gid: str,
    settings: Settings,
    *,
    workers: Any,
    tools: Any,
    env: Any,
) -> None:
    """一轮资讯实测（docs/09）：申请 → 逐条派子 agent 在一次性 VM 里测 → 释放。

    - 拿不到 VM（acquire → None）→ 这轮不测，items 一个不写 verify，照常入库；
    - 每条派一次 Workers.run（tools = vm_run/vm_put_file/vm_read_file，max_steps 12），
      brief 写清「只做只读/无副作用的验证」；
    - 实测总时长上限 20 分钟（_VERIFY_BUDGET_S）：到钟就不再派下一条，没测的不留半截；
    - 用完必 release（本地删 key 目录；远端没有销毁命令，只能等它过期）；
    - 直接改写 items[i]["verify"]（结构见 _verify_dict）；单条崩溃只算那一单 failed。
    """
    minutes = max(1, int(getattr(settings.environments, "verify_minutes", 10) or 10))
    start = clock.now()

    # vm 工具的 box 走闭包现读（拿到机器前先注册，get_box 读 cell）
    cell: dict[str, Any] = {"box": None}
    try:
        from .tools_railway import register_vm_tools

        register_vm_tools(tools, get_box=lambda: cell["box"], env=env)
    except Exception:
        logger.exception("注册 vm 工具出错（群 %s），这轮跳过实测", gid)
        return

    box = None
    try:
        import secrets as _secrets

        box = await env.acquire(f"verify-{_secrets.token_hex(4)}")
        if box is None:
            logger.info("这轮实测拿不到一次性 VM（群 %s），照常入库", gid)
            return
        cell["box"] = box
        for plan in plans:
            # 总时长上限：剩下的预算放不下一条（按 verify_minutes 估）就不开新的了
            if (clock.now() - start) + minutes * 60 > _VERIFY_BUDGET_S:
                logger.info("实测总时长到顶（%d 分钟，群 %s），剩下的条这轮不测", _VERIFY_BUDGET_S // 60, gid)
                break
            try:
                idx = int(plan.get("index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(items)):
                continue
            item = items[idx]
            t0 = clock.now()
            brief = _verify_brief(item, plan, minutes, gid)
            try:
                report = await workers.run(
                    brief,
                    group_id=str(gid),
                    tools=list(_VERIFY_TOOLS),
                    actor="实测子 agent",
                    max_steps=12,
                )
            except Exception as e:
                logger.exception("实测子 agent 崩溃（群 %s 条 %r）", gid, item.get("title", "")[:30])
                item["verify"] = _verify_dict("failed", f"实测子 agent 崩了：{e}", [], t0)
                continue
            item["verify"] = _verify_from_report(report, t0)
    finally:
        cell["box"] = None
        if box is not None:
            try:
                await env.release(box)
            except Exception:
                logger.exception("释放一次性 VM 出错（群 %s）", gid)


def _verify_brief(item: dict, plan: dict, minutes: int, gid: str) -> str:
    """实测子 agent 的 brief：只读/无副作用的规矩写死，10 分钟内做完。"""
    del gid
    lines = [
        "你的任务：在一台一次性 Linux VM（2 vCPU / 2G，60 分钟后自动销毁）里实测下面这条内容。",
        "",
        f"条目：{item.get('title', '')}",
        f"链接：{item.get('url', '')}",
        f"摘要：{str(item.get('summary') or '')[:300]}",
        f"要验证什么：{str(plan.get('what') or '')[:200]}",
        f"预期结果：{str(plan.get('expect') or '')[:200]}",
        "",
        "工具：vm_run（在 VM 里跑命令）、vm_put_file（把工作区里你写的测试脚本传到 VM /app 下）、"
        "vm_read_file（读 VM 上 /app 下的文件）。",
        "",
        "硬规矩：",
        "1. **只做只读 / 无副作用的验证**：安装、跑示例、看输出；",
        "2. 不注册账号、不填任何密钥、不访问要登录的服务、不联网拉来路不明的脚本直接跑；",
        "3. VM 是干净的临时机：别往里放任何隐私或密钥；",
        f"4. {minutes} 分钟内做完（timeout_s 别超过 600 秒）；做完用 submit_result 交回：",
        '   data = {"status": "passed" 或 "failed", "summary": "一句话结论", '
        '"steps": ["做了什么，≤6 条，每条一句话，写明关键输出/坑"]}。',
        "   验证结果和「预期结果」对得上才是 passed；对不上、装不上、跑不通都是 failed，如实交回。",
    ]
    return "\n".join(lines)
