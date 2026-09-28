"""persona.py：关注成员的额外人物画像（只给管理员看；docs/07 §8.2）。

- refresh(group_id, user_id)：素材 = 该人自己最近 150 条发言（focus_messages，
  profile.tick 只在 personal_profile 开着时、对当前关注成员存）+ MaiBot 长期记忆
  检索文本（knowledge.search 只传 query / chat_id / limit——**不传 person_id**：
  person_id 只是后置过滤，匹配不到还退回全部结果，容易误信，见 docs/06 阶段 0）。
  主模型 json_mode 提炼成 {summary, doing, cares, asked, style}，存
  focus_members.persona（JSON）/ persona_ts，并把 note 同步成 summary。
- due(group_id, now)：到期要刷新的人——personal_profile 开着、是当前关注成员
  （removed=0）、距 persona_ts ≥ 24 小时（或从没做过且库里有发言）、且上次刷新以来
  至少有 5 条新发言。personal_profile 关掉 → 顺手清掉这一群残留的
  focus_messages / persona。
- 模型没配好：refresh 立刻 False（不查记忆、不调模型）；due 里的系统性判断
  （开关 / 资格 / 清理）照常做，但时间档筛选用「没做过也算到期」保守兜底（app 里
  每群每轮最多刷一个，刷新本身因模型没配好不做，不会浪费模型调用）。

红线：persona 内容只进管理员的网页接口；永不出现在群消息、群画像、
PROFILE-*.md、资讯 / 构想 / 开场白的提示词里。privacy.scrub 的片段来源里
包含了 persona 的 summary 和 note。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from . import clock

if TYPE_CHECKING:
    from .config import Settings
    from .host import Host
    from .models import Models
    from .store import Store

logger = logging.getLogger("maiwork.persona")

# 刷新素材：取该人最近多少条自己的发言
_REFRESH_MSG_LIMIT = 150
# 到期间隔（秒）：距上次刷新至少一天
_DUE_AFTER_SECONDS = 24 * 3600
# 上次刷新以来至少攒够这么多条新发言才值得刷新
_DUE_MIN_NEW_MESSAGES = 5
# 每类最多 5 条、每条最多 40 字（提示词里也这么要求，这里再夹一遍）
_LIST_ITEM_MAX = 5
_ITEM_TEXT_MAX = 40
# summary 同步进 note（兼容旧显示），note 本身限 120 字（profile._apply_people 同）
_SUMMARY_MAX = 120


class Personas:
    """关注成员个人画像的提炼与到期判断。构造参数：store, host, models, get_settings。"""

    def __init__(
        self,
        store: "Store",
        host: "Host",
        models: "Models",
        get_settings: Callable[[], "Settings"],
    ) -> None:
        self._store = store
        self._host = host
        self._models = models
        self._get_settings = get_settings

    # ------------------------------------------------------------------
    # 到期判断 / 残留清理
    # ------------------------------------------------------------------

    def due(self, group_id: str, now: float) -> list[str]:
        """本群当前到期、该刷新的关注成员（user_id 列表，只含 removed=0 的）。

        - personal_profile 关掉：把这一群残留的 focus_messages / persona 清掉，返回 []；
        - removed=1 的已移除成员：顺带删掉他们的 focus_messages / persona（防御，正常
          流程 set_focus / focus() 已删过）；
        - 到期 = 距 persona_ts ≥ 24 小时（persona_ts 空 = 从没做过也算到期），且
          上次刷新以来该人至少有 5 条新发言（从没做过的按 0 起算）。
        """
        gid = str(group_id)
        settings = self._get_settings()
        with self._store.tx() as conn:
            if not settings.focus.personal_profile:
                cur = conn.execute("DELETE FROM focus_messages WHERE group_id=?", (gid,))
                conn.execute(
                    "UPDATE focus_members SET persona='', persona_ts=0"
                    " WHERE group_id=? AND (persona<>'' OR COALESCE(persona_ts,0)>0)",
                    (gid,),
                )
                if cur.rowcount:
                    logger.info("personal_profile 已关：清掉群 %s 的 %d 条成员发言留存", gid, cur.rowcount)
                return []
            conn.execute(
                "DELETE FROM focus_messages WHERE group_id=? AND user_id IN"
                " (SELECT user_id FROM focus_members WHERE group_id=? AND removed=1)",
                (gid, gid),
            )
            conn.execute(
                "UPDATE focus_members SET persona='', persona_ts=0"
                " WHERE group_id=? AND removed=1 AND (persona<>'' OR COALESCE(persona_ts,0)>0)",
                (gid,),
            )
            rows = conn.execute(
                "SELECT user_id, persona_ts FROM focus_members WHERE group_id=? AND removed=0",
                (gid,),
            ).fetchall()
            out: list[str] = []
            for r in rows:
                uid = str(r["user_id"])
                try:
                    p_ts = float(r["persona_ts"] or 0)
                except (TypeError, ValueError):
                    p_ts = 0.0
                if p_ts > 0 and float(now) - p_ts < _DUE_AFTER_SECONDS:
                    continue
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM focus_messages"
                    " WHERE group_id=? AND user_id=? AND ts>=?",
                    (gid, uid, p_ts),
                ).fetchone()
                if int(n["c"] if n else 0) >= _DUE_MIN_NEW_MESSAGES:
                    out.append(uid)
        return sorted(out)

    # ------------------------------------------------------------------
    # 刷新个人画像
    # ------------------------------------------------------------------

    async def refresh(self, group_id: str, user_id: str) -> bool:
        """刷新一个人的个人画像。成功入库返回 True；任何一步不行返回 False。

        门：模型没配好立刻 False；personal_profile 必须开着且本人是当前关注成员。
        """
        gid = str(group_id)
        uid = str(user_id).strip()
        if not self._models.settings().ready():
            return False
        settings = self._get_settings()
        if not uid or not settings.focus.personal_profile:
            return False
        member = self._store.read().execute(
            "SELECT name, removed FROM focus_members WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        if member is None or int(member["removed"]):
            return False
        name = str(member["name"] or "").strip() or uid

        msgs_rows = self._store.read().execute(
            "SELECT ts, text FROM focus_messages WHERE group_id=? AND user_id=?"
            " ORDER BY ts DESC LIMIT ?",
            (gid, uid, _REFRESH_MSG_LIMIT),
        ).fetchall()
        msgs = [(float(r["ts"]), str(r["text"] or "")) for r in reversed(msgs_rows)]

        memory = ""
        sess_row = self._store.read().execute(
            "SELECT session_id FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        session_id = str(sess_row["session_id"] or "") if sess_row else ""
        if session_id:
            try:
                # 不传 person_id：那只是后置过滤、匹配不到会退回全部结果（docs/06）
                memory = await self._host.knowledge(
                    f"{name} 最近在做什么 关心什么",
                    chat_id=session_id,
                    limit=6,
                )
            except Exception:
                logger.info("群 %s 查 MaiBot 长期记忆失败（%s），这轮画像不用它", gid, uid[:8])
                memory = ""

        messages = self._build_prompt(gid, uid, name, msgs, memory)
        from .models import ModelError

        try:
            result = await self._models.chat(
                "main", messages, json_mode=True, purpose="persona.refresh", group_id=gid
            )
        except ModelError:
            logger.info("群 %s 成员 %s 的画像调主模型失败，下轮再说", gid, uid[:8])
            return False
        try:
            data = json.loads(str(result.text or "").strip())
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            logger.info("群 %s 成员 %s 的画像输出不是 JSON，下轮再说", gid, uid[:8])
            return False

        summary = self._clip_text(data.get("summary"), _SUMMARY_MAX)
        persona = {
            "summary": summary,
            "doing": self._clip_list(data.get("doing")),
            "cares": self._clip_list(data.get("cares")),
            "asked": self._clip_list(data.get("asked")),
            "style": self._clip_text(data.get("style"), _ITEM_TEXT_MAX),
        }
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT removed FROM focus_members WHERE group_id=? AND user_id=?",
                (gid, uid),
            ).fetchone()
            if row is None or int(row["removed"]):
                # 模型跑着的时候管理员把人移了：不落库
                return False
            persona_json = json.dumps(persona, ensure_ascii=False)
            conn.execute(
                "UPDATE focus_members SET persona=?, persona_ts=?, note=?, updated=?"
                " WHERE group_id=? AND user_id=?",
                (persona_json, now, summary, now, gid, uid),
            )
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated) VALUES (?, ?, ?)",
                (f"persona.last_refresh:{gid}:{uid}", json.dumps(now), now),
            )
            self._store.event(
                conn,
                "persona.refresh",
                group_id=gid,
                entity="focus_member",
                entity_id=uid,
                payload={"messages": len(msgs), "memory": bool(memory.strip())},
            )
        return True

    # ------------------------------------------------------------------
    # 提示词与解析
    # ------------------------------------------------------------------

    _PROMPT_SYSTEM = (
        "你在为 MaiWork（QQ 群的后台助手）整理一位群成员的几句话情况，只给管理员一个人看，"
        "方便管理员判断 MaiWork 能不能帮到这个人。只输出 JSON，不要任何解释和别的话。"
    )

    @staticmethod
    def _clip_text(raw: Any, limit: int) -> str:
        return str(raw or "").strip()[:limit]

    @staticmethod
    def _clip_list(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for x in raw:
            s = str(x or "").strip()[:_ITEM_TEXT_MAX]
            if s:
                out.append(s)
            if len(out) >= _LIST_ITEM_MAX:
                break
        return out

    def _build_prompt(
        self,
        gid: str,
        uid: str,
        name: str,
        msgs: list[tuple[float, str]],
        memory: str,
    ) -> list[dict]:
        parts: list[str] = [
            f"要整理的人：{name}（QQ号 {uid}）。",
        ]
        if msgs:
            lines = []
            for ts, text in msgs:
                hhmm = clock.bj(ts).strftime("%m-%d %H:%M")
                lines.append(f"- [{hhmm}] {text}")
            parts.append("ta 最近在群里的发言：\n" + "\n".join(lines))
        else:
            parts.append("ta 最近没有发言留存。")
        if memory.strip():
            parts.append(
                "下面是 MaiBot 的长期记忆里可能和这个人相关的内容——可能不准、"
                "也可能混进了别人的事，只当参考，核对不上的别信：\n" + memory.strip()
            )
        parts.append(
            "规则：\n"
            "1. 只记和「MaiWork 能帮他什么」有关的部分：在做什么、关心什么话题、提过什么需求/请求。\n"
            "2. 不记隐私：住址、电话、身份证、感情、健康、收入、家庭等，一概不记，别出现。\n"
            "3. 证据不足就少写：该空的给空列表、空字符串，不编。\n"
            "4. 每类最多 5 条，每条不超过 40 字；summary 一两句话不超过 120 字。\n"
            '输出格式（JSON，就这几个键）：\n'
            '{"summary":"一两句话","doing":["在做的事"],"cares":["关心的话题"],'
            '"asked":["提过的需求/请求"],"style":"说话风格一句话（可空）"}'
        )
        return [
            {"role": "system", "content": self._PROMPT_SYSTEM},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
