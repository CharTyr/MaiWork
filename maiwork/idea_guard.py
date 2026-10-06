"""idea_guard.py：个人向构想 / 提一嘴的「堆积闸 + 打扰闸」（纯查询；不调模型、不写库、不发消息）。

2026-10 用户定（个人向构想不占群面构想的位子：feeds._unhandled_idea_count 只数
`target_user_id=''` 的行），所以个人向那一路以前会无限堆积、也会反复追着同一个人提。
这里定两道**减法**，别处照旧：

1. **堆积闸**（生成那一刻，personal._insert_idea）：
   - 同一人同时只留 1 条没处理的个人向构想（state ∈ new / wanted / pending 且没落任务）；
   - 每群 7 天新鲜期里最多 3 条；
   - 7 天以前的旧行不占位子（绝不永久锁死）；
   - **只读**：正在等批准（pending）等老行一行都不改，不替谁「腾位子」。

2. **打扰闸**（提一嘴那一刻，card_push.IdeaMention.scan）：
   - 同一人 3 天内只提一次——sent / uncertain，以及还没落地的 pending / queued / sending
     全算（沉默不等于不高兴，不追着问）；
   - 每群 7 天新鲜期里最多 3 条个人提一嘴在途；
   - 7 天以前的旧行不占位子。

另外给「发送前复核」提供只读依据：他本人在**本群**、某个时刻之后的原话（有界、只按
user_id 精确匹配），依据还在不在（同一个群、还是他本人、还在新鲜窗口里），以及**材料指纹**
（他本人的聊天行位 / 他的活 / 给他的构想：复核时存一份，发送前再取一次，只比「规模变多」
不比时间戳——群聊是异步补读入库的，一条发言的时间可能早于复核时刻、却在复核之后才进库）。

「沉默不是差评」：这里只做减法，不写任何负面经验 / 规矩，也不因为没人回话就降低谁的权重。
"""

from __future__ import annotations

import hashlib
import json

from typing import Any, Optional

# 「没处理」的口径和 feeds._IDEAS_UNHANDLED_STATES 一致（新想法 / 有人点过想要 / 等批准）。
# 不从 feeds 导入：feeds 很重，personal 只在这里用一次；测试里有对齐断言钉住两边一样。
UNHANDLED_IDEA_STATES = ("new", "wanted", "pending")
# 还没落地的提一嘴（建了行但还没真发出去）
OPEN_MENTION_STATUSES = ("pending", "queued", "sending")
# 已经提过的提一嘴（uncertain = 可能发出去了，也按「提过」算，绝不重发 / 不追问）
SENT_MENTION_STATUSES = ("sent", "uncertain")

# 新鲜期：过了这么久的老行不再占位子（绝不永久锁死）
PERSONAL_HORIZON_S = 7 * 86400.0
# 同一个人的冷却期（未落地 / 已提 / 不确定全算）
PERSONAL_COOLDOWN_S = 3 * 86400.0
# 每群新鲜期里最多几条个人向构想 / 个人提一嘴在途
PERSONAL_GROUP_CAP = 3
# 复核材料：只看他本人最近这么多条原话，每条截断，别把提示词撑爆
PERSONAL_CHAT_LIMIT = 20
_CHAT_TEXT_MAX = 200


def _states(values: tuple[str, ...]) -> str:
    return "', '".join(values)


def personal_idea_block(store: Any, gid: str, uid: str, now: float) -> str:
    """现在能不能再给这个人生成一条个人向构想。不能 → 固定的中文原因；可以 → ""。

    只读；不新建、不改任何已有行。
    """
    gid, uid = str(gid), str(uid or "")
    if not uid:
        return ""
    now = float(now)
    horizon = now - PERSONAL_HORIZON_S
    states = _states(UNHANDLED_IDEA_STATES)
    row = store.read().execute(
        "SELECT 1 FROM ideas WHERE group_id=? AND target_user_id=? AND task_id IS NULL"
        f" AND state IN ('{states}') AND created>=? LIMIT 1",
        (gid, uid, horizon),
    ).fetchone()
    if row is not None:
        return "这个人还有一条没处理的个人向构想，先不另开一条"
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM ideas WHERE group_id=? AND COALESCE(target_user_id,'')<>''"
        " AND task_id IS NULL"
        f" AND state IN ('{states}') AND created>=?",
        (gid, horizon),
    ).fetchone()
    if row is not None and int(row["c"] or 0) >= PERSONAL_GROUP_CAP:
        return f"本群 7 天内的个人向构想已经攒到 {PERSONAL_GROUP_CAP} 条没处理，先不出新的"
    return ""


def personal_mention_block(store: Any, gid: str, uid: str, now: float) -> str:
    """现在能不能给这个人建一行「提一嘴」待发。不能 → 固定原因；可以 → ""。"""
    gid, uid = str(gid), str(uid or "")
    if not uid:
        return ""
    now = float(now)
    horizon = now - PERSONAL_HORIZON_S
    cooldown = now - PERSONAL_COOLDOWN_S
    open_states = _states(OPEN_MENTION_STATUSES)
    row = store.read().execute(
        "SELECT 1 FROM idea_mentions WHERE group_id=? AND at_user=?"
        f" AND status IN ('{open_states}') AND created>=? LIMIT 1",
        (gid, uid, horizon),
    ).fetchone()
    if row is not None:
        return "这个人已经有一条还没落地的个人提一嘴，先不重复建"
    sent_states = _states(SENT_MENTION_STATUSES)
    row = store.read().execute(
        "SELECT 1 FROM idea_mentions WHERE group_id=? AND at_user=?"
        f" AND ((status IN ('{sent_states}') AND COALESCE(sent_ts, created)>=?)"
        f"   OR (status IN ('{open_states}') AND created>=?))"
        " LIMIT 1",
        (gid, uid, cooldown, cooldown),
    ).fetchone()
    if row is not None:
        return "这个人 3 天内已经提过一次（沉默不是差评，不追着问）"
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM idea_mentions WHERE group_id=? AND at_user<>''"
        f" AND status IN ('{open_states}') AND created>=?",
        (gid, horizon),
    ).fetchone()
    if row is not None and int(row["c"] or 0) >= PERSONAL_GROUP_CAP:
        return f"本群 7 天内的个人提一嘴已经堆到 {PERSONAL_GROUP_CAP} 条没落地，先不堆新的"
    return ""


def last_sent_mention_ts(store: Any, gid: str, uid: str, *, exclude_id: int = 0) -> float:
    """这个人最近一次「已经提过」的时刻（sent / uncertain）；从没提过 → 0.0。

    这是再提一嘴的锚点：只有**这之后**他本人明确表示需要，才允许再提。
    """
    row = store.read().execute(
        "SELECT MAX(COALESCE(sent_ts, created)) AS ts FROM idea_mentions"
        " WHERE group_id=? AND at_user=? AND id<>?"
        f" AND status IN ('{_states(SENT_MENTION_STATUSES)}')",
        (str(gid), str(uid), int(exclude_id or 0)),
    ).fetchone()
    return float(row["ts"] or 0.0) if row is not None else 0.0


def mentioned_since(store: Any, gid: str, uid: str, since: float) -> bool:
    """这个人在 `since` 之后已经被提过一次了吗（发送前复核用：排队期间别人先提了）。"""
    row = store.read().execute(
        "SELECT 1 FROM idea_mentions WHERE group_id=? AND at_user=?"
        f" AND status IN ('{_states(SENT_MENTION_STATUSES)}')"
        " AND COALESCE(sent_ts, created)>=? LIMIT 1",
        (str(gid), str(uid), float(since)),
    ).fetchone()
    return row is not None


def target_chat_since(store: Any, gid: str, uid: str, since: float, now: float, *,
                      limit: int = PERSONAL_CHAT_LIMIT) -> list[dict]:
    """他本人**在本群**、(`since`, `now`] 之间的原话（最新在前；最多 limit 条，每条截断）。

    只按 user_id 精确匹配：别人的话、别的群的话一律不掺（认人靠平台 id）。
    读不到库 → []（调用方按「没有材料」失败关闭）。
    """
    try:
        rows = store.read().execute(
            "SELECT text, ts, message_id FROM chat_log"
            " WHERE group_id=? AND user_id=? AND ts>? AND ts<=?"
            " ORDER BY ts DESC, rowid DESC LIMIT ?",
            (str(gid), str(uid), float(since), float(now), max(1, int(limit))),
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "message_id": str(r["message_id"] or ""),
            "ts": float(r["ts"] or 0.0),
            "text": str(r["text"] or "")[:_CHAT_TEXT_MAX],
        }
        for r in rows
    ]


def material_fingerprint(store: Any, gid: str, uid: str) -> Optional[dict]:
    """复核所依据的「材料指纹」：他本人的聊天行、他的活、给他的构想。

    判断复核有没有过期**不能比时间戳**：群聊是异步补读入库的（`profile.tick`），一条
    发言的时间可能早于复核时刻、却是在复核之后才进库（2026-10 用户指出的边界）。所以记
    的是「材料的规模」——聊天表里他本人最大的 rowid、他的任务条数 / 最新更新时间、给他的
    构想最大 id。复核时存进 guard，发送前再取一次：**只要变多了**就说明复核看到的不是
    全部材料，作废、宁可少发一条。

    读不到库 → None（调用方按「复核没做成」失败关闭）。
    """
    gid, uid = str(gid), str(uid or "")
    try:
        chat = store.read().execute(
            "SELECT MAX(rowid) AS m FROM chat_log WHERE group_id=? AND user_id=?", (gid, uid)
        ).fetchone()
        task = store.read().execute(
            "SELECT COUNT(*) AS c, MAX(updated) AS u FROM tasks"
            " WHERE group_id=? AND requester_id=?", (gid, uid)
        ).fetchone()
        idea = store.read().execute(
            "SELECT MAX(id) AS m FROM ideas WHERE group_id=? AND target_user_id=?", (gid, uid)
        ).fetchone()
    except Exception:
        return None
    return {
        "chat_max_rowid": int(chat["m"] or 0) if chat is not None else 0,
        "task_count": int(task["c"] or 0) if task is not None else 0,
        "task_max_updated": float(task["u"] or 0.0) if task is not None else 0.0,
        "idea_max_id": int(idea["m"] or 0) if idea is not None else 0,
    }



def material_snapshot(store: Any, gid: str, uid: str, iid: int, since: float, now: float) -> str:
    """有界材料的内容摘要；捕获原位修改/rowid复用和网页直接关联任务。异常抛给失败关闭入口。

    只返回散列，不把群聊或画像写进队列。since 固定为复核窗口起点，避免时间滑动产生假变化。
    """
    c = store.read()
    chat = c.execute(
        "SELECT message_id,text,ts FROM chat_log WHERE group_id=? AND user_id=?"
        " AND typeof(ts) IN ('integer','real') AND ts>? AND ts<=?"
        " ORDER BY ts DESC,rowid DESC LIMIT ?",
        (gid, uid, since, now, PERSONAL_CHAT_LIMIT),
    ).fetchall()
    ideas = c.execute(
        "SELECT id,title,body,origin,state,task_id,updated FROM ideas"
        " WHERE group_id=? AND target_user_id=? AND created>=? ORDER BY id DESC LIMIT 5",
        (gid, uid, since),
    ).fetchall()
    idea = c.execute(
        "SELECT id,title,body,origin,state,task_id,updated FROM ideas"
        " WHERE id=? AND group_id=? AND target_user_id=?", (iid, gid, uid),
    ).fetchall()
    tasks = c.execute(
        "SELECT id,title,status,updated FROM tasks WHERE group_id=?"
        " AND (requester_id=? OR id IN (SELECT task_id FROM ideas WHERE group_id=?"
        " AND target_user_id=? AND task_id IS NOT NULL)) ORDER BY updated DESC,id DESC LIMIT 5",
        (gid, uid, gid, uid),
    ).fetchall()
    data = [[list(r) for r in rows] for rows in (chat, ideas, idea, tasks)]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def material_grew(before: Any, after: Any) -> bool:
    """材料比复核时多了吗（只比「变多」，不比时间戳）。

    任何一项拿不到 / 形状不对 → True（失败关闭：宁可少发一条）。行被清理变少不算「多了」。
    """
    keys = ("chat_max_rowid", "task_count", "task_max_updated", "idea_max_id")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return True
    if any(k not in before or k not in after for k in keys):
        return True  # 指纹缺项：不猜，按「多了」失败关闭
    try:
        for key in ("chat_max_rowid", "task_count", "idea_max_id", "task_max_updated"):
            if float(after.get(key) or 0) > float(before.get(key) or 0):
                return True
    except (TypeError, ValueError):
        return True
    return False


def evidence_ok(store: Any, gid: str, uid: str, refs: Any, *, anchor: float,
                now: float) -> bool:
    """复核用的每一条依据都还站得住吗：同一个群、正好是他本人的话、还在新鲜窗口里。

    依据为空（本来就不需要依据）→ True。读不到库 → False（失败关闭）。
    """
    items = [r for r in (refs if isinstance(refs, list) else []) if isinstance(r, dict)]
    if not items:
        return True
    try:
        for r in items:
            mid = str(r.get("message_id") or "")
            if not mid:
                return False
            row = store.read().execute(
                "SELECT 1 FROM chat_log WHERE group_id=? AND message_id=? AND user_id=?"
                " AND ts>? AND ts<=? LIMIT 1",
                (str(gid), mid, str(uid), float(anchor), float(now)),
            ).fetchone()
            if row is None:
                return False
    except Exception:
        return False
    return True
