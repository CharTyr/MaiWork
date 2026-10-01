"""topics.py（M2）：冷场开话题。

第 1 层（代码，不调模型）：睡觉时段、开关、候选池、每日上限、推送额度、
最小间隔（含退避倍数）、安静时长（≥ max(3×usual_gap, 20 分钟)）、
这个钟点平时有人（usual_gap ≤ 30 分钟）、夜里的安静不算冷场（醒来后要有人说过话）、
安静超过 90 分钟不开。

第 2 层（Jev）：state 带时间（安静了多少分钟、平时多久一条、每条消息几分钟前），
过滤掉图片/事件/空合并转发这些杂音；ok ≥ 0.6 且 reason == fine 且至少一条 fit ≥ 0.5 → 开。
Jev 不可用 → 这次不开（不写 topic_log）。

同一段冷场（最近一条消息时刻 + 候选 id 列表算指纹）判过一次后 30 分钟内不重复问，
指纹变了或满 30 分钟才重问（2026-10-01 线上实测：一天判 343 次、开 0 次）。

开：主模型按人设写开场白，speaker="maiwork" 走 send_text，
speaker="maibot" 走 proactive_trigger。10 分钟后 follow_up。
播报腔整条作废。每发出一次压缩候选、写 news_items.replies、
加进 mentions、写 topic_log（jev 里带真实分数和卡在哪一层）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import clock, voice
from .config import Settings
from .delivery import Mentions, Pushes
from .host import Host, HostError, Msg
from .intake import Signals
from .jev import Jev
from .models import Models, ModelError
from .store import Store

# 不直接 import Profiles/Hosts 的具体类型——它们由别的同事在改，这里 duck-typing；类型签名做文档参考。
Profiles = Any  # docs/07 §8 规定的接口，fake 时继承 FakeProfiles

logger = logging.getLogger("maiwork.topics")

# 播报腔：命中任一词就整条作废（不发）
_BROADCAST_WORDS = ("据报道", "以下是", "今日资讯", "今日热点", "新闻速报")

# 开场白最长（清洗后）
_OPENER_MAX_CHARS = 120

# 退避倍数上限（min_gap_hours × 2^n，n 最多 3 → ×8）
_BACKOFF_MAX = 8

# follow_up：开场白发出多少秒后数 replies / followups
_FOLLOW_UP_DELAY_S = 600

# 安静时长下限（秒）：max(3×usual_gap, MIN_QUIET)
_MIN_QUIET_S = 20 * 60  # 20 分钟

# usual_gap 上限（秒）：>30 分钟 视为「这个钟点平时没人」
_USUAL_GAP_MAX_S = 30 * 60

# 安静太久（秒）：超过这个时长不开（群已经凉透了，这时候开对不上节奏；
# 也避免一小时前的判断被当成本刻的情况反复问 Jev）
_MAX_QUIET_S = 90 * 60

# 同一段冷场重复判断的最小间隔（秒）：指纹没变时，判过一次后这么久内不重问 Jev
_REJUDGE_S = 30 * 60

# Jev 判定阈值
_OK_THRESHOLD = 0.6
_FIT_THRESHOLD = 0.5

# 读给 Jev 看的群消息：多读一些（可能有图片/事件/合并转发占位），过滤后留最近 20 条
_MSG_SCAN_LIMIT = 60
_MSG_KEEP = 20

# 候选：多取一些按画像兴趣排序，再取前 5 条
_CANDIDATE_SCAN = 30

# 算「和群画像兴趣对得上」时看的画像类别（兴趣、最近在聊、在做的事）
_INTEREST_CATEGORIES = ("interest", "recent", "ongoing")

# 无描述的图片 / 表情包：这些不是「群里在说话」
_PLAIN_NOISE = frozenset(("[图片]", "[image]", "[表情包]"))

# 合并转发的格式骨架：包装词、方括号、名字、冒号、破折号、空白
_FORWARD_SKELETON = re.compile(r"合并转发消息|【[^】]*】|[\[\]【】:：\-—–\s]")

# 判断态（topic_log.jev 字段 JSON）的 reason 中文化映射
_REASON_ZH = {
    "fine": "可以开",
    "left": "人都走了",
    "open_question": "有问题还没人回",
    "mood": "气氛不对",
}


# ---------------------------------------------------------------------------
# 纯函数辅助（不读库、不调模型，方便单测）
# ---------------------------------------------------------------------------


def _is_noise(text: Any) -> bool:
    """这条群消息值不值得给 Jev 看（纯函数）。

    2026-10-01 线上实测：给 Jev 的 20 条里很多是这些占位，模型把「刷了个表情包」
    当成群里正在聊天，判出「气氛不对」：
    - 去空白后为空；
    - 恰好是无描述的图片 / 表情包（"[图片]" / "[image]" / "[表情包]"）；
    - 事件行（"[事件-群消息撤回] …" / "[事件-群消息表情回应] …"）；
    - 合并转发，但去掉格式骨架（"【合并转发消息:"、"-- 【名字】:"、"】"）后没剩实际文字。
    带描述的表情包（"[表情包: 无语,呆滞]"）和 "[视频]" 保留——那是真实内容。
    """
    s = str(text or "").strip()
    if not s:
        return True
    if s.lower() in _PLAIN_NOISE:
        return True
    if s.startswith("[事件-"):
        return True
    if "【合并转发消息" in s:
        return not _FORWARD_SKELETON.sub("", s)
    return False


def _last_wakeup_ts(now: float, end_min: int) -> float:
    """最近一次睡觉时段结束的时刻（北京时间今天或昨天那个钟点）。

    跨午夜（"23:00-08:00"）和当天（"00:00-07:00"）都适用：结束钟点每天固定，
    取「不晚于 now 的那一个」。北京时间固定 UTC+8、没有夏令时，直接算即可。
    """
    em = int(end_min) % 1440
    t = clock.bj(float(now)).replace(hour=em // 60, minute=em % 60, second=0, microsecond=0)
    if t.timestamp() > float(now):
        t = t - timedelta(days=1)
    return float(t.timestamp())


def _fingerprint(quiet_ts: float, candidates: List[dict]) -> dict:
    """同一段冷场的指纹：最近一条群消息的时刻 + 候选 id 排序后的列表。

    两个都没变才算「情况没变」（候选的排序是算出来的，不算变化，所以 id 先排序）。
    """
    return {
        "quiet_ts": float(quiet_ts),
        "cands": sorted(int(c["id"]) for c in candidates),
    }


def _bigrams(text: str) -> set:
    """去空白、转小写后的字符二元组（中文也适用；太短就整串当一个）。"""
    s = re.sub(r"\s+", "", str(text or "").lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _interest_score(cand: dict, interests: List[str]) -> float:
    """候选 title+brief 的二元组有多大比例出现在画像兴趣条目里（0~1，纯代码）。

    和 delivery.TopicMatcher 的关键词命中思路一致（都是「≥2 字符的重合」），
    只是方向反过来：那边是群友聊起来时找候选，这里是给候选找画像兴趣。
    """
    blob = "".join(interests)
    grams = _bigrams(f"{cand.get('title') or ''}{cand.get('brief') or ''}")
    if not grams:
        return 0.0
    hit = sum(1 for g in grams if g in blob)
    return hit / float(len(grams))


class Topics:
    """冷场开话题。"""

    def __init__(
        self,
        store: Store,
        host: Host,
        models: Models,
        jev: Jev,
        profiles: Profiles,
        mentions: Mentions,
        pushes: Pushes,
        get_settings: Callable[[], Settings],
        signals: Any,  # intake.Signals，测试里用 SignalsStub（含 last_ts / session_id）
        identity: Any = None,  # identity.py（开场白人设只认 SOUL；None 就是没人设，不回退读 MaiBot 人格）
    ) -> None:
        self._store = store
        self._host = host
        self._models = models
        self._jev = jev
        self._profiles = profiles
        self._mentions = mentions
        self._pushes = pushes
        self._get_settings = get_settings
        self._signals = signals
        self._identity = identity

    # ------------------------------------------------------------------
    # 候选池
    # ------------------------------------------------------------------

    def add_candidate_at(  # 测试用：用显式 now，便于造「未过期/已过期」的候选
        self,
        group_id: str,
        *,
        kind: str,
        ref_id: int,
        title: str,
        brief: str,
        link: str = "",
        ttl_h: float,
        now: float,
    ) -> int:
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link, expires_ts, used_ts, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    str(group_id), str(kind), int(ref_id), str(title), str(brief), str(link),
                    float(now) + ttl_h * 3600.0, float(now),
                ),
            )
            return int(cur.lastrowid or 0)

    def add_candidate(
        self,
        group_id: str,
        *,
        kind: str,
        ref_id: int,
        title: str,
        brief: str,
        link: str = "",
        ttl_h: float | None = None,
    ) -> None:
        settings = self._get_settings()
        if ttl_h is None:
            ttl_h = float(getattr(settings.topics, "candidate_ttl_hours", 12))
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO topic_candidates (group_id, kind, ref_id, title, brief, link, expires_ts, used_ts, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    str(group_id), str(kind), int(ref_id), str(title), str(brief), str(link),
                    clock.now() + ttl_h * 3600.0, clock.now(),
                ),
            )

    # ------------------------------------------------------------------
    # 后台循环每分钟调一次
    # ------------------------------------------------------------------

    async def check(self, group_id: str, now: float) -> str:
        """cold-start 判断；返回做了什么（给日志 / 测试）。"""
        gid = str(group_id)
        settings = self._get_settings()

        # 1) 睡觉时段第一个判断，直接返回（连库都尽量少读）
        quiet = getattr(settings.delivery, "quiet_hours", "") or "23:00-08:00"
        try:
            s, e = clock.parse_hhmm_range(quiet)
        except (ValueError, AttributeError):
            s, e = 0, 0
        if s != e and clock.in_range(now, (s, e)):
            return "skip:quiet_hours"

        # 2) topics.enabled 关
        if not getattr(settings.topics, "enabled", True):
            return "skip:disabled"

        # 3) 候选池非空（只看未过期、未用过的）
        row = self._store.read().execute(
            "SELECT id FROM topic_candidates"
            " WHERE group_id=? AND used_ts IS NULL AND expires_ts>?"
            " LIMIT 1",
            (gid, now),
        ).fetchone()
        if row is None:
            return "skip:no_candidate"

        # 4) 今日已开话题数 < per_day
        # 「已开」= topic_log 写过 judgment（opener 空也是一次「已判定」；
        #  但每日上限关心的是真的发出去的——按 opener!='' 算）
        per_day = int(getattr(settings.topics, "per_day", 2))
        day = clock.day_key(float(now))
        rows = self._store.read().execute(
            "SELECT ts FROM topic_log WHERE group_id=? AND opener!=''",
            (gid,),
        ).fetchall()
        opened_today = sum(1 for r in rows if clock.day_key(float(r["ts"])) == day)
        if per_day > 0 and opened_today >= per_day:
            return "skip:per_day_limit"

        # 5) 推送额度够
        ok, why = self._pushes.can_push(gid, "topic", now)
        if not ok:
            return f"skip:push:{why}"

        # 6) 最小间隔 × 退避倍数（上次没人接 → 倍数 ×2，最多 ×8）
        last_open = self._store.read().execute(
            "SELECT ts FROM topic_log"
            " WHERE group_id=? AND opener!=''"
            " ORDER BY ts DESC LIMIT 1",
            (gid,),
        ).fetchone()
        if last_open is not None:
            min_gap = float(getattr(settings.topics, "min_gap_hours", 3)) * 3600.0
            backoff = self._get_backoff(gid)
            gap_needed = min_gap * backoff
            if now - float(last_open["ts"]) < gap_needed:
                return f"skip:min_gap(backoff={backoff})"

        # 7) 安静时长 ≥ max(3×usual_gap, 1200s)
        usual_gap = self._profiles.usual_gap(gid, now)  # 秒 / None
        signal_ts = float(getattr(self._signals, "last_ts", lambda _g: 0.0)(gid))
        group_row = self._store.read().execute(
            "SELECT last_msg_ts FROM groups WHERE group_id=?",
            (gid,),
        ).fetchone()
        group_ts = float(group_row["last_msg_ts"]) if group_row is not None else 0.0
        quiet_ts = max(signal_ts, group_ts)
        quiet_s = float(now) - quiet_ts if quiet_ts > 0 else 0.0

        # 7a) 夜里的安静不算冷场：最近一条消息早于最近一次睡觉时段结束
        #     （今天或昨天那个结束钟点）→ 醒来后还没人说话，不判。
        #     线上实测：07:00 一到就把前一晚的安静算成冷场，立刻开始判。
        if quiet_ts > 0 and s != e and quiet_ts < _last_wakeup_ts(float(now), e):
            return "skip:no_activity_since_wakeup"

        # 7b) 安静太久（>90 分钟）→ 不开（群已经凉透了，这时候开对不上节奏）
        if quiet_s > _MAX_QUIET_S:
            return "skip:too_long_quiet"

        if usual_gap is None:
            # 这个钟点平时没人 → 不开
            return "skip:no_usual_audience"
        if float(usual_gap) > _USUAL_GAP_MAX_S:
            return "skip:no_usual_audience"
        need_quiet_s = max(3.0 * float(usual_gap), float(_MIN_QUIET_S))
        if quiet_s < need_quiet_s:
            return f"skip:not_quiet_enough(quiet={quiet_s:.0f}s, need={need_quiet_s:.0f}s)"

        # 8) Jev 不可用 → 不开（不写 topic_log）
        if not self._jev.available():
            return "skip:jev_unavailable"

        # 9) 候选（未过期未用；按和群画像兴趣的对得上程度排序后取 5 条）
        candidates = self._list_candidates(gid, now, limit=5)
        if not candidates:
            return "skip:no_candidate"

        # 9a) 同一段冷场不重复问 Jev：指纹 =（最近一条消息时刻, 候选 id 列表）。
        #     指纹没变且上次真判过不到 _REJUDGE_S → 输入几乎一样，问了也是同一个答案，
        #     白花 Jev 调用、白写重复日志（线上实测：一段冷场里每 30 秒问一次）。
        #     满了 _REJUDGE_S 也重问，让「有问题还没人回」这类判断随时间过期。
        fp = _fingerprint(quiet_ts, candidates)
        judged = self._store.kv_get(f"topics.judged.{gid}", default=None)
        if isinstance(judged, dict) and judged.get("fingerprint") == fp:
            age = float(now) - float(judged.get("ts") or 0.0)
            if 0.0 <= age < _REJUDGE_S:
                return "skip:same_as_last"

        # 9b) 读最近消息：多读一些（60 条），滤掉杂音后留最近 20 条给 Jev
        msgs: List[Msg] = []
        sid = self._session_id_for(gid)
        if sid:
            try:
                raw_msgs = await self._host.messages(sid, now - 3600.0 * 6, now, _MSG_SCAN_LIMIT)
            except HostError:
                raw_msgs = []  # 读不到也照样判
            msgs = [m for m in raw_msgs if not _is_noise(m.text)][-_MSG_KEEP:]

        # 10) 问 Jev（把「安静了多久、平时多久一条、每条消息几分钟前」写进 state / 问题里，
        #     不然 Jev 只看到一堆没时间的消息，会把 50 分钟前的问题当刚提的）
        quiet_minutes = int(quiet_s // 60)
        usual_gap_minutes = round(float(usual_gap) / 60.0, 1)
        questions: Dict[str, Any] = {
            "ok": {
                "type": "noul",
                "instructions": (
                    f"群里已经安静了 {quiet_minutes} 分钟"
                    f"（平时这个点大约 {usual_gap_minutes} 分钟一条）。现在抛出一个新话题合适吗？"
                ),
            },
            "reason": {
                "type": "choice",
                "instructions": "现在不适合开新话题的原因？选一个最贴切的。",
                "criteria": {
                    "fine": "可以开",
                    "left": "人都走了，没人会接",
                    "open_question": (
                        "最近有人提的问题还没人回、而且问题还不算太久"
                        "（看每条消息的 minutes_ago，太久的就不算「还没人回」了），不适合岔开"
                    ),
                    "mood": "气氛不对（争执、严肃事），再说就不合时宜",
                },
            },
        }
        for i, cand in enumerate(candidates):
            if str(cand.get("kind") or "") == "idea":
                # 构想类候选是「回头问一句群里之前聊过的事、问大家要不要帮忙」
                instr = (
                    f"候选{i} 是回头问一句群里之前聊过的事、问大家要不要帮忙："
                    "能自然接上最近的聊天吗、大家会愿意接吗？"
                )
            else:
                instr = f"候选{i}能不能自然接上最近的聊天、这个群会不会感兴趣？"
            questions[f"fit_{i}"] = {"type": "noul", "instructions": instr}
        state = {
            "quiet_minutes": quiet_minutes,
            "usual_gap_minutes": usual_gap_minutes,
            "messages": [
                {
                    "speaker": "BOT" if m.is_bot else m.user_name,
                    "text": m.text,
                    "minutes_ago": int(max(0.0, float(now) - float(m.ts)) // 60),
                }
                for m in msgs
            ],
            "candidates": [
                {"title": cand["title"], "brief": cand["brief"], "link": cand["link"]}
                for cand in candidates
            ],
        }
        answers = await self._jev.ask(
            state, questions, purpose="topic_check", group_id=gid, timeout_ms=None
        )
        if answers is None:
            return "skip:jev_unavailable"

        # Jev 真答了才记指纹：没答（超时/熔断）下一轮照问。
        with self._store.tx() as conn:
            self._store.kv_set(
                conn,
                f"topics.judged.{gid}",
                {"fingerprint": fp, "ts": float(now)},
            )

        # 11) 判定（Jev 判了，不管开不开都写 topic_log）
        ok_p = float(answers.get("ok", 0.0))
        reason_t = answers.get("reason")
        reason_label, reason_prob, reason_conf = reason_t if reason_t else ("open_question", 0.0, 0.0)
        fit_arr: List[Tuple[int, float]] = []  # (candidate_index, fit)
        for i in range(len(candidates)):
            v = answers.get(f"fit_{i}")
            if isinstance(v, (int, float)):
                fit_arr.append((i, float(v)))
        # 排序：fit 高的优先
        fit_arr.sort(key=lambda t: t[1], reverse=True)
        pass_judge = ok_p >= _OK_THRESHOLD and reason_label == "fine"
        fit_best = float(fit_arr[0][1]) if fit_arr else None
        fit_title = str(candidates[fit_arr[0][0]]["title"]) if fit_arr else ""

        pick: Optional[Dict[str, Any]] = None
        candidate_idx: Optional[int] = None
        candidate: Optional[Dict[str, Any]] = None
        if pass_judge and fit_arr and fit_arr[0][1] >= _FIT_THRESHOLD:
            candidate_idx = fit_arr[0][0]
            candidate = candidates[candidate_idx]
            pick = {
                "title": candidate["title"],
                "fit": float(fit_arr[0][1]),
            }

        # 写 topic_log（jev 字段 JSON / pick / 后续 fill）
        topic_id = self._write_judgment(
            gid,
            now,
            quiet_s,
            usual_gap,
            ok_p=float(ok_p),
            reason=reason_label,
            reason_conf=reason_conf,
            candidate_id=int(candidate["id"]) if candidate is not None else None,
            pick=pick,
            answers=answers,
            questions=questions,
            quiet_ts=quiet_ts,
            fit_best=fit_best,
            fit_title=fit_title,
            opened=candidate is not None,
        )

        if candidate is None:
            return f"skip:judged_no(ok={ok_p:.2f},reason={reason_label},fit={fit_arr[0][1] if fit_arr else 0:.2f})"

        # 12) 决定 speaker / 生成开场白
        speaker = str(getattr(settings.topics, "speaker", "maiwork") or "maiwork").lower()
        if speaker not in ("maiwork", "maibot"):
            speaker = "maiwork"

        # 生成开场白（models.chat main / purpose="opener"）
        # 模型没配好 → 不开
        model_settings = self._models.settings()
        if not model_settings.ready():
            self._update_log(topic_id, {"opener": "", "result": json.dumps({"rejected": "模型没配好"}, ensure_ascii=False)})
            return "skip:model_not_ready"

        opener_text: Optional[str] = None
        try:
            opener_prompt = await self._build_opener_prompt(gid, sid, candidate, msgs)
            # 开场白在主循环（run_loop_once → _topics_round → topics.check）里直接 await：
            # 不能按设置里默认的 5 次 ×10 秒重试把循环卡几分钟→ retries=1（现实两秒内出结果，不行就下轮）
            chat_result = await self._models.chat(
                agent="main",
                messages=opener_prompt,
                json_mode=False,
                purpose="opener",
                group_id=gid,
                timeout=30,
                retries=1,
            )
            opener_text = str(getattr(chat_result, "text", "") or "").strip()
        except Exception as exc:
            logger.exception("生成开场白失败（group=%s）", gid)
            self._update_log(topic_id, {"opener": "", "result": json.dumps({"rejected": f"模型错误: {type(exc).__name__}"}, ensure_ascii=False)})
            return "skip:model_error"

        if not opener_text:
            self._update_log(topic_id, {"opener": "", "result": json.dumps({"rejected": "开场白为空"}, ensure_ascii=False)})
            return "skip:empty_opener"

        # 自我介绍 / 寒暄（2026-10-01 用户定：不要自我介绍和废话）→ 这次不开
        if voice.is_self_intro(opener_text):
            self._update_log(topic_id, {"opener": "", "result": json.dumps({"rejected": "开场白在自我介绍 / 寒暄，不发"}, ensure_ascii=False)})
            return "rejected:self_intro"

        # 清洗
        cleaned = self._clean_opener(opener_text)
        if cleaned is None:
            self._update_log(topic_id, {"opener": "", "result": json.dumps({"rejected": "播报腔作废"}, ensure_ascii=False)})
            return "rejected:broadcast_tone"

        # G7 隐私闸：开场白含关注成员名字/注记 → 整个不发
        cleaned = self._scrub_group_text(gid, cleaned)
        if cleaned is None:
            self._update_log(topic_id, {"opener": "", "result": json.dumps({"rejected": "开场白含关注成员信息，不发"}, ensure_ascii=False)})
            return "rejected:privacy"

        # 13) 发送
        reject_reason = ""
        message_id = ""
        try:
            if speaker == "maiwork":
                send_result = await self._host.send_text(
                    sid, cleaned,
                    # send.hybrid 内部带 sync_to_maisaka_history=True, storage_message=True
                )
                message_id = str(getattr(send_result, "message_id", "") or "")
            else:  # maibot
                resp = await self._host.proactive_trigger(
                    sid,
                    intent=f"开场白：{cleaned}",
                    reason=f"群冷场 {_insight(quiet_s)}，话题 {candidate['title']}",
                    priority="normal",
                    metadata={"candidate_id": int(candidate["id"]), "pick_fit": fit_arr[0][1]},
                )
                message_id = ""  # proactive_trigger 不知道 message_id
                if not (isinstance(resp, dict) and resp.get("success")):
                    raise HostError("proactive_trigger 返回 success=False")
        except Exception as exc:
            logger.exception("开话题发送失败（group=%s speaker=%s）", gid, speaker)
            reject_reason = str(type(exc).__name__)
            self._update_log(
                topic_id,
                {
                    "opener": cleaned,
                    "message_id": "",
                    "result": json.dumps({"error": reject_reason}, ensure_ascii=False),
                },
            )
            return f"send_failed:{reject_reason}"

        # 14) 记发送成功：候选 used_ts / news_items.replies=0 / mentions / topic_log
        now_send = clock.now()
        self._mark_candidate_used(candidate, now_send)
        # news_items（kind=="news" 时 ref_id）status_kind="used", status_at
        if str(candidate.get("kind") or "") == "news" and candidate.get("ref_id"):
            try:
                with self._store.tx() as conn:
                    conn.execute(
                        "UPDATE news_items SET status_kind='used', status_at=? WHERE id=?",
                        (now_send, int(candidate["ref_id"])),
                    )
            except Exception:
                logger.exception("news_items 状态更新失败")
        # 加进 mentions（可提起清单，4 小时）
        try:
            brief = str(candidate.get("brief") or candidate["title"])
            link = str(candidate.get("link") or "")
            mention_text = brief
            if link:
                mention_text += f" {link}"
            self._mentions.add(
                gid, mention_text,
                key=f"topic:{topic_id}",
                ttl_s=4 * 3600.0,
                turns=5,
            )
        except Exception:
            logger.exception("mentions.add 失败")
        # 推送记账
        self._pushes.record(gid, "topic", f"开场白：{cleaned[:80]}", now_send)
        # 更新 topic_log
        self._update_log(
            topic_id,
            {
                "opener": cleaned,
                "message_id": message_id,
                "followup_due_ts": now_send + _FOLLOW_UP_DELAY_S,
            },
        )
        return f"opened:topic_id={topic_id}"

    # ------------------------------------------------------------------
    # follow_up：10 分钟后数 replies / followups
    # ------------------------------------------------------------------

    async def follow_up(self, group_id: str, now: float) -> None:
        """对到期的 topic_log：读 [开场白 ts, ts+600] 消息，写 result。"""
        gid = str(group_id)
        rows = self._store.read().execute(
            "SELECT id, ts, message_id, opener FROM topic_log"
            " WHERE group_id=? AND opener!='' AND followup_due_ts IS NOT NULL"
            " AND followup_due_ts<=? AND result IS NULL",
            (gid, now),
        ).fetchall()
        if not rows:
            return
        sid = self._session_id_for(gid)
        if not sid:
            return
        for row in rows:
            topic_id = int(row["id"])
            topic_ts = float(row["ts"])
            opener_message_id = str(row["message_id"] or "")
            try:
                msgs = await self._host.messages(sid, topic_ts, topic_ts + _FOLLOW_UP_DELAY_S, 50, limit_mode="earliest")
            except HostError:
                msgs = []
            # 非机器人发言的不同人数 / 机器人后续条数（不含开场白本身）
            replies: set[str] = set()
            followups = 0
            for m in msgs:
                if not isinstance(m, Msg):
                    continue
                if float(m.ts) < topic_ts or float(m.ts) > topic_ts + _FOLLOW_UP_DELAY_S:
                    continue
                if m.is_bot:
                    # 排除开场白自己（按 message_id 识别）
                    if opener_message_id and str(m.id) == opener_message_id:
                        continue
                    followups += 1
                else:
                    if m.user_id:
                        replies.add(str(m.user_id))
            result = {"replies": len(replies), "followups": int(followups)}
            self._update_log(
                topic_id,
                {"result": json.dumps(result, ensure_ascii=False)},
            )
            # news_items.replies 同步（如果这条话题是从资讯里来的）
            self._sync_news_replies(topic_id, int(result["replies"]))
            # 退避更新
            if result["replies"] > 0:
                self._reset_backoff(gid)
            else:
                self._bump_backoff(gid)

    # ------------------------------------------------------------------
    # 退避
    # ------------------------------------------------------------------

    def _get_backoff(self, group_id: str) -> int:
        v = self._store.kv_get(f"topics.backoff.{group_id}", default=1)
        try:
            n = int(v)
            return max(1, min(_BACKOFF_MAX, n))
        except (TypeError, ValueError):
            return 1

    def _bump_backoff(self, group_id: str) -> None:
        cur = self._get_backoff(group_id)
        with self._store.tx() as conn:
            self._store.kv_set(conn, f"topics.backoff.{group_id}", min(_BACKOFF_MAX, cur * 2))

    def _reset_backoff(self, group_id: str) -> None:
        with self._store.tx() as conn:
            self._store.kv_set(conn, f"topics.backoff.{group_id}", 1)

    # ------------------------------------------------------------------
    # log_view / verdict
    # ------------------------------------------------------------------

    def log_view(self, group_id: str, *, days: int = 3) -> list[dict]:
        """§9.3 topic_log 结构。"""
        gid = str(group_id)
        since = clock.now() - days * 86400.0
        rows = self._store.read().execute(
            "SELECT id, ts, quiet_s, usual_gap_s, jev, pick, opener, result, verdict"
            " FROM topic_log WHERE group_id=? AND ts>=? ORDER BY ts DESC",
            (gid, since),
        ).fetchall()
        out: List[dict] = []
        for r in rows:
            import json as _json
            jev = None
            if r["jev"]:
                try:
                    jev = _json.loads(r["jev"])
                except (ValueError, TypeError):
                    jev = None
            pick = None
            if r["pick"]:
                try:
                    pick = _json.loads(r["pick"])
                except (ValueError, TypeError):
                    pick = None
            result = None
            if r["result"]:
                try:
                    result = _json.loads(r["result"])
                except (ValueError, TypeError):
                    result = None
            out.append({
                "id": int(r["id"]),
                "ts": float(r["ts"]),
                "quiet_s": float(r["quiet_s"]),
                "usual_gap_s": float(r["usual_gap_s"]) if r["usual_gap_s"] is not None else None,
                "jev": jev,
                "pick": pick,
                "opener": str(r["opener"] or ""),
                "result": result,
                "verdict": r["verdict"],
            })
        return out

    def verdict(self, topic_id: int, value: str | None) -> None:
        """管理员在网页上标 right / wrong / None。"""
        v = value if value in ("right", "wrong", None) else None
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE topic_log SET verdict=? WHERE id=?",
                (v, int(topic_id)),
            )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _list_candidates(self, gid: str, now: float, limit: int = 5) -> List[dict]:
        """未过期未用的候选；按和群画像兴趣的对得上程度排序后取 limit 条。

        2026-10-01 起不再单纯按 created DESC：线上实测候选池里「和群里长期兴趣贴边」
        和「完全不沾边」的混在一起，只按时间取 5 条会让 Jev 看到的全是后者。
        做法：先多取 _CANDIDATE_SCAN 条，用「候选 title+brief 与画像兴趣类条目文本的
        字符二元组重合度」排序（纯代码，不调模型、不多打一次网络）；拿不到画像条目
        （没有 / 读失败）就退回按 created DESC。同分保持 created DESC（稳定排序）。
        """
        rows = self._store.read().execute(
            "SELECT id, kind, ref_id, title, brief, link, expires_ts, created"
            " FROM topic_candidates"
            " WHERE group_id=? AND used_ts IS NULL AND expires_ts>?"
            " ORDER BY created DESC LIMIT ?",
            (gid, now, int(_CANDIDATE_SCAN)),
        ).fetchall()
        cands = [dict(r) for r in rows]
        if not cands:
            return []
        interests = self._interest_texts(gid)
        if not interests:
            return cands[: int(limit)]
        return sorted(cands, key=lambda c: -_interest_score(c, interests))[: int(limit)]

    def _interest_texts(self, gid: str) -> List[str]:
        """群画像里「兴趣类」条目的文字（兴趣 / 最近在聊 / 在做的事）。

        读不到就返回空表——调用方退回按时间排，绝不因为画像出问题就不开话题。
        """
        try:
            entries = self._profiles.entries(gid)
        except Exception:
            logger.info("读画像条目失败，候选退回按时间排（group=%s）", gid, exc_info=True)
            return []
        out: List[str] = []
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            if str(e.get("category") or "") not in _INTEREST_CATEGORIES:
                continue
            text = str(e.get("text") or "").strip()
            if text:
                out.append(text)
        return out

    def _session_id_for(self, gid: str) -> str:
        # 优先 SignalsStub / intake.Signals 里的（收过新消息）
        sid_fn = getattr(self._signals, "session_id", None)
        if callable(sid_fn):
            sid = sid_fn(gid)
            if sid:
                return str(sid)
        # 回落 groups.session_id
        row = self._store.read().execute(
            "SELECT session_id FROM groups WHERE group_id=?",
            (gid,),
        ).fetchone()
        return str(row["session_id"]) if row is not None and row["session_id"] else ""

    def _write_judgment(
        self,
        gid: str,
        now: float,
        quiet_s: float,
        usual_gap: Any,
        *,
        ok_p: float,
        reason: str,
        reason_conf: float,
        candidate_id: int | None,
        pick: dict | None,
        answers: dict,
        questions: dict,
        quiet_ts: float = 0.0,
        fit_best: float | None = None,
        fit_title: str = "",
        opened: bool = False,
    ) -> int:
        ok = bool(answers.get("ok") is not None and float(answers.get("ok", 0.0)) >= _OK_THRESHOLD and reason == "fine")
        # stuck：这轮没开是卡在哪一层——时机不过关（ok 分不够或 reason 不是 fine）
        # 还是时机过了但候选都不够贴；真开了就是 null。
        # （开场白之后被打回（播报腔/隐私/发送失败）记在 result 里，不算这两层。）
        if opened:
            stuck: Optional[str] = None
        elif float(ok_p) < _OK_THRESHOLD or reason != "fine":
            stuck = "timing"
        else:
            stuck = "candidate"
        jev_obj = {
            "ok": ok,
            "reason": _REASON_ZH.get(reason, reason),
            "confidence": float(reason_conf),
            "detail": "",
            # 2026-10-01 起把真实分数也落库：原来只存了 reason 那道选择题的把握，
            # 网页上看不出「没开」是卡在时机、还是候选都不够贴。
            "ok_p": float(ok_p),
            "ok_need": float(_OK_THRESHOLD),
            "fit_best": (float(fit_best) if fit_best is not None else None),
            "fit_need": float(_FIT_THRESHOLD),
            "fit_title": str(fit_title or ""),
            "stuck": stuck,
            "stretch_ts": float(quiet_ts),
        }

        jev_json = json.dumps(jev_obj, ensure_ascii=False)
        pick_json = json.dumps(pick or {}, ensure_ascii=False)
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id, opener, message_id, followup_due_ts, result, verdict)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '', '', NULL, NULL, NULL)",
                (gid, float(now), float(quiet_s), float(usual_gap) if usual_gap is not None else None, jev_json, pick_json, candidate_id),
            )
            return int(cur.lastrowid or 0)

    def _update_log(self, topic_id: int, fields: dict) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        for k in ("opener", "message_id", "result"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        if "followup_due_ts" in fields:
            sets.append("followup_due_ts=?")
            vals.append(float(fields["followup_due_ts"]))
        if not sets:
            return
        vals.append(int(topic_id))
        with self._store.tx() as conn:
            conn.execute(f"UPDATE topic_log SET {', '.join(sets)} WHERE id=?", vals)

    def _mark_candidate_used(self, candidate: dict, now: float) -> None:
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE topic_candidates SET used_ts=? WHERE id=?",
                (float(now), int(candidate["id"])),
            )

    def _sync_news_replies(self, topic_id: int, replies: int) -> None:
        """从 topic_log.candidate_id 找回 news_items.replies 同步。"""
        row = self._store.read().execute(
            "SELECT candidate_id FROM topic_log WHERE id=?",
            (int(topic_id),),
        ).fetchone()
        if row is None or row["candidate_id"] is None:
            return
        cand = self._store.read().execute(
            "SELECT kind, ref_id FROM topic_candidates WHERE id=?",
            (int(row["candidate_id"]),),
        ).fetchone()
        if cand is None or str(cand["kind"]) != "news":
            return
        ref_id = cand["ref_id"]
        if not ref_id:
            return
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "UPDATE news_items SET replies=? WHERE id=?",
                    (int(replies), int(ref_id)),
                )
        except Exception:
            logger.exception("news_items.replies 同步失败")

    # ------------------------------------------------------------------
    # 开场白
    # ------------------------------------------------------------------

    async def _build_opener_prompt(
        self,
        gid: str,
        sid: str,
        candidate: dict,
        recent_msgs: List[Msg],
    ) -> List[dict]:
        """按 docs/02-设计.md §4.4 第 1 条组 prompt；人设只认 SOUL（voice.persona）。

        kind == "idea" 的候选按「关心式问法」写（有由头就顺着由头问一句要不要帮忙）；
        news 候选保持原来的写法。不自我介绍、不寒暄（规矩在 persona.section() 里）。
        """
        del sid  # 不再读 MaiBot 最近发言当语气样例（人设只认 SOUL）
        persona = voice.persona(self._identity)
        # 最近 15 条群消息
        recent_lines: List[str] = []
        for m in recent_msgs[-15:]:
            who = "机器人" if m.is_bot else (m.user_name or "群友")
            txt = m.text.strip()[:80]
            if txt:
                recent_lines.append(f"{who}: {txt}")
        recent_text = "\n".join(recent_lines) if recent_lines else "（没有）"
        if str(candidate.get("kind") or "") == "idea":
            user_prompt = self._idea_opener_prompt(gid, candidate, persona, recent_text)
        else:
            link = str(candidate.get("link") or "")
            user_prompt = f"""群里冷场了一段时间，你随手起个话头。

{persona.section()}

# 群里最近的对话
{recent_text}

# 想聊的话题（候选素材）
标题：{candidate.get("title", "")}
简报：{candidate.get("brief", "")}
链接：{link}

写一两句口语化的开场白，把上面的话题自然带出来：
- 像随口一提，别端着
- 最多一个链接（也可以不带，自然就行）
- 别写「据报道」「以下是」「今日资讯」「新闻速报」这种播报腔
- 别超过 120 字

只回开场白本身，不要别的解释。"""
        return [
            {"role": "system", "content": persona.system()},
            {"role": "user", "content": user_prompt},
        ]

    def _idea_material(self, gid: str, candidate: dict) -> dict:
        """idea 候选的素材：按 ref_id 从 ideas 表读 origin / title / body。

        读不到（老库没补列 / 行没了 / 假行）就用候选自己的标题和简报，绝不抛。
        **不读 basis**——那句是「为什么适合」的画像依据，不能进要发进群的话。
        """
        out = {
            "origin": "",
            "title": str(candidate.get("title") or "").strip(),
            "body": str(candidate.get("brief") or "").strip(),
        }
        ref_id = candidate.get("ref_id")
        if not ref_id:
            return out
        try:
            row = self._store.read().execute(
                "SELECT origin, title, body FROM ideas WHERE id=? AND group_id=?",
                (int(ref_id), str(gid)),
            ).fetchone()
        except Exception:
            logger.debug("读构想由头失败（群 %s 条 %s）", gid, ref_id, exc_info=True)
            return out
        if row is None:
            return out
        out["origin"] = str(row["origin"] or "").strip()
        out["title"] = str(row["title"] or "").strip() or out["title"]
        out["body"] = str(row["body"] or "").strip() or out["body"]
        return out

    def _idea_opener_prompt(
        self, gid: str, candidate: dict, persona: Any, recent_text: str,
    ) -> str:
        """构想候选的开场白：关心式问一句（有由头顺着由头问），不推销、不点名任何人。"""
        material = self._idea_material(gid, candidate)
        origin = material["origin"]
        if origin:
            head = (
                f"由头（群里之前聊过的那件事）：{origin}\n"
                f"写成「话说之前大家聊的那个{origin}后来怎么样了？要我帮忙吗？」这种问法。"
            )
        else:
            head = "这件事没有由头（想不起接的是哪件事），就按下面的标题 / 想法自然地问一句要不要帮忙。"
        return f"""群里冷场了一段时间，你想顺着之前聊过的一件事问一句。

{persona.section()}

# 群里最近的对话
{recent_text}

# 想聊的事（群里之前有人提过想做）
{head}
标题：{material["title"]}
想法：{material["body"]}

写一两句口语化的开场白：
- 用关心、顺口问一句的口吻（「……后来怎么样了？要我帮忙吗？」），结尾是问句
- 只提这件事本身，不点名任何群友，不提任何人的个人情况、习惯、经历
- 别推销：不许写「我可以帮」「给大家带来」「推荐给大家」「安利」「感兴趣的话」「点进去看看」
- 别写「据报道」「以下是」「今日资讯」「新闻速报」这种播报腔
- 最多一个链接（也可以不带，自然就行）；别超过 120 字

只回开场白本身，不要别的解释。"""

    def _scrub_group_text(self, gid: str, text: str) -> Optional[str]:
        """G7 隐私闸：开场白要发进群，不能含关注成员的名字 / 注记。"""
        from .privacy import scrub

        return scrub(gid, text, self._store)

    @staticmethod
    def _clean_opener(text: str) -> Optional[str]:
        """去除首尾引号、限 120 字、链接最多保留 1 个、播报腔作废。"""
        s = str(text or "").strip().strip('"').strip("'").strip("“”").strip("‘’").strip()
        if not s:
            return None
        for w in _BROADCAST_WORDS:
            if w in s:
                return None
        # 链接最多 1 个
        import re
        url_pat = re.compile(r"https?://\S+")
        urls = url_pat.findall(s)
        if len(urls) > 1:
            # 保留第一个，其余去掉
            first = urls[0]
            # 把除了第一个外的 URL 换成空
            count = 0

            def _replace(m):
                nonlocal count
                count += 1
                return m.group(0) if count == 1 else ""

            s = url_pat.sub(_replace, s)
        if len(s) > _OPENER_MAX_CHARS:
            s = s[:_OPENER_MAX_CHARS]
        return s


def _insight(quiet_s: float) -> str:
    """把安静秒数翻成「几分钟 / 几小时」。"""
    m = int(quiet_s / 60)
    if m < 60:
        return f"{m} 分钟"
    return f"{m // 60} 小时 {m % 60} 分钟"
