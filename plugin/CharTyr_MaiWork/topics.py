"""topics.py（M2）：冷场开话题。

第 1 层（代码，不调模型）：睡觉时段、开关、候选池、每日上限、推送额度、
最小间隔（含退避倍数）、安静时长（≥ max(3×usual_gap, 20 分钟)）、
这个钟点平时有人（usual_gap ≤ 30 分钟）。

第 2 层（Jev）：ok ≥ 0.6 且 reason == fine 且至少一条 fit ≥ 0.5 → 开。
Jev 不可用 → 这次不开（不写 topic_log）。

开：主模型按人设写开场白，speaker="maiwork" 走 send_text，
speaker="maibot" 走 proactive_trigger。10 分钟后 follow_up。
播报腔整条作废。每发出一次压缩候选、写 news_items.replies、
加进 mentions、写 topic_log。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import clock
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

# Jev 判定阈值
_OK_THRESHOLD = 0.6
_FIT_THRESHOLD = 0.5

# 判断态（topic_log.jev 字段 JSON）的 reason 中文化映射
_REASON_ZH = {
    "fine": "可以开",
    "left": "人都走了",
    "open_question": "有问题还没人回",
    "mood": "气氛不对",
}


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
        identity: Any = None,  # identity.py（开场白注入 SOUL；None 走老的人设逻辑）
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

        # 9) 读最近 20 条消息 + 候选（最多 5 条）
        msgs: List[Msg] = []
        sid = self._session_id_for(gid)
        if sid:
            try:
                msgs = await self._host.messages(sid, now - 3600.0 * 6, now, 20)
            except HostError:
                msgs = []  # 读不到也照样判
        candidates = self._list_candidates(gid, now, limit=5)
        if not candidates:
            return "skip:no_candidate"

        # 10) 问 Jev
        questions: Dict[str, Any] = {
            "ok": {"type": "noul", "instructions": "群里现在适合抛出一个新话题吗？"},
            "reason": {
                "type": "choice",
                "instructions": "现在不适合开新话题的原因？选一个最贴切的。",
                "criteria": {
                    "fine": "可以开",
                    "left": "人都走了，没人会接",
                    "open_question": "有问题还没人回，不适合岔开",
                    "mood": "气氛不对（争执、严肃事），再说就不合时宜",
                },
            },
        }
        for i, cand in enumerate(candidates):
            questions[f"fit_{i}"] = {
                "type": "noul",
                "instructions": f"候选{i}能不能自然接上最近的聊天、这个群会不会感兴趣？",
            }
        state = {
            "messages": [
                {"speaker": "BOT" if m.is_bot else m.user_name, "text": m.text}
                for m in msgs[-20:]
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
                "main",
                opener_prompt,
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
        rows = self._store.read().execute(
            "SELECT id, kind, ref_id, title, brief, link, expires_ts"
            " FROM topic_candidates"
            " WHERE group_id=? AND used_ts IS NULL AND expires_ts>?"
            " ORDER BY created DESC LIMIT ?",
            (gid, now, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

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
    ) -> int:
        jev_obj = {
            "ok": bool(answers.get("ok") is not None and float(answers.get("ok", 0.0)) >= _OK_THRESHOLD and reason == "fine"),
            "reason": _REASON_ZH.get(reason, reason),
            "confidence": float(reason_conf),
            "detail": "",
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
        """按 docs/02-设计.md §4.4 第 1 条组 prompt。"""
        # 人设：读 host.config；读不到略过
        nickname = ""
        personality = ""
        reply_style = ""
        try:
            v = await self._host.config("bot.nickname")
            nickname = str(v or "").strip() if v is not None else ""
        except Exception:
            pass
        try:
            v = await self._host.config("personality.personality")
            personality = str(v or "").strip() if v is not None else ""
        except Exception:
            pass
        try:
            v = await self._host.config("personality.reply_style")
            reply_style = str(v or "").strip() if v is not None else ""
        except Exception:
            pass
        # MaiBot 最近在本群的 5 条发言（找语气）
        maibot_recent: List[str] = []
        if sid:
            try:
                # 近 24 小时限 30 条，反向找 is_bot
                msgs = await self._host.messages(sid, clock.now() - 86400.0, clock.now(), 30)
                for m in reversed(msgs):
                    if m.is_bot and m.text.strip():
                        maibot_recent.append(m.text.strip()[:80])
                        if len(maibot_recent) >= 5:
                            break
            except HostError:
                pass
        # 最近 15 条群消息
        recent_lines: List[str] = []
        for m in recent_msgs[-15:]:
            who = "机器人" if m.is_bot else (m.user_name or "群友")
            txt = m.text.strip()[:80]
            if txt:
                recent_lines.append(f"{who}: {txt}")
        # 人设
        persona_lines: List[str] = []
        if nickname:
            persona_lines.append(f"名字：{nickname}")
        if personality:
            persona_lines.append(f"性格：{personality}")
        if reply_style:
            persona_lines.append(f"回复风格：{reply_style}")
        persona = ("\n".join(persona_lines)) if persona_lines else "（没有）"
        # 样例
        examples = "\n".join(f"- {t}" for t in maibot_recent) if maibot_recent else "（没有）"
        # 最近群消息
        recent_text = "\n".join(recent_lines) if recent_lines else "（没有）"
        link = str(candidate.get("link") or "")
        # 身份与工作记忆（identity.py）：有 SOUL 就按 SOUL 的口吻（身份块在最前面）
        soul_block = ""
        if self._identity is not None:
            try:
                soul_block = str(self._identity.prompt_block("soul") or "")
            except Exception:
                soul_block = ""
        user_prompt = f"""你扮演 {nickname or "群里的 AI 助手"}。群里冷场了一段时间，你随手起个话头。

# 人设
{persona}

# 你最近在群里说过的话（找语气）
{examples}

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
            {"role": "system", "content": (soul_block + f"你是 {nickname or '群里的 AI 助手'}。") if soul_block else f"你是 {nickname or '群里的 AI 助手'}。"},
            {"role": "user", "content": user_prompt},
        ]

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
