"""profile.py：群画像、关注成员、活跃度统计。

第一部分（本文件目前范围）：不调模型的部分——
- 增量读群消息（游标 + 分页 + 首次回读），维护 member_activity / activity_bins /
  bot_messages / member_interactions 统计；
- 群画像条目的管理员操作（entries / add_entry / edit_entry / delete_entry）；
- 关注成员（focus / set_focus）；
- 群脉搏（pulse）和平时发言间隔（usual_gap）。

第二部分（本文件）：_maybe_refresh 里调主模型提炼画像变更（攒批/到点/强制/首次触发，
[上次提炼, 游标] 区间从 host 重新读消息、300 条一批依次提炼）、失败后重试（连续 3 次跳过）、
每周整理（合并重复、删过时）、PROFILE-<群号>.md 同步（不含关注成员信息；G6 起
文件名带群号，共享工作区的群各写各的不再互相覆盖）。

接口约定见 docs/07-代码接口.md §8。
"""

from __future__ import annotations

import json
import re
import logging
import math
import random
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from . import clock
from .privacy import scrub

if TYPE_CHECKING:  # 只做类型提示，运行时不需要
    from .config import Settings
    from .host import Host
    from .models import Models
    from .store import Store

logger = logging.getLogger("maiwork.profile")

# 五类画像条目（docs/07 §4）
CATEGORIES = ("recent", "interest", "ongoing", "convention", "resource")

# 真模型常把类别写成中文（线上实测：「话题」「兴趣」），这些叫法都认
_CATEGORY_ALIASES = {
    "recent": "recent", "最近在聊": "recent", "最近": "recent", "话题": "recent", "近况": "recent", "近期话题": "recent",
    "interest": "interest", "长期兴趣": "interest", "兴趣": "interest", "爱好": "interest",
    "ongoing": "ongoing", "doing": "ongoing", "在做的事": "ongoing", "在做": "ongoing", "项目": "ongoing", "进行中": "ongoing",
    "convention": "convention", "约定和说法": "convention", "约定": "convention", "说法": "convention",
    "约定术语": "convention", "术语": "convention", "梗": "convention", "黑话": "convention",
    "resource": "resource", "常用资源": "resource", "资源": "resource", "工具": "resource", "链接": "resource",
}


def normalize_category(raw: object) -> str:
    """模型给的类别 → 五类代码之一；认不出返回 ""。"""
    key = str(raw or "").strip().lower()
    return _CATEGORY_ALIASES.get(key, "")

# 分页读消息：每页上限、最多翻多少页
_PAGE_LIMIT = 200
_MAX_PAGES = 20
# 15 分钟一个活跃度桶
_BIN_SECONDS = 900
# groups.info_ts 超过一天就重新取群名
_INFO_STALE_SECONDS = 86400
# bot_messages 只保留 7 天（判断「回复了 MaiBot」用）
_BOT_MSG_KEEP_SECONDS = 7 * 86400
# 关注成员候选：看近 30 天发言
_FOCUS_WINDOW_SECONDS = 30 * 86400
# 关注成员发言留存（个人画像素材）：每人只留最近 14 天、最多 300 条
_FOCUS_MSG_KEEP_SECONDS = 14 * 86400
_FOCUS_MSG_KEEP_MAX = 300
# 存进 focus_messages 的单条发言截断
_FOCUS_MSG_TEXT_MAX = 300
# usual_gap：看最近多少天（不含今天）
_USUAL_DAYS = 21
# usual_gap：有数据的天数低于这个数就认为样本不足
_USUAL_MIN_SAMPLES = 5

_SKIP_NOT_SERVED = "不是服务群"
_SKIP_NO_SESSION = "拿不到会话 ID"
_SKIP_THROTTLED = "距上次读取不足读消息间隔"
# 本轮有新消息信号时，最多每隔这么久才去宿主读一次（没信号按 [profile] read_interval_minutes）
_READ_WITH_SIGNAL_SECONDS = 60.0


@dataclass
class RefreshResult:
    """tick 的结果。refreshed=True 表示这一轮调了主模型提炼画像（第二部分才真做）。"""

    read: int = 0  # 本轮读到的新消息条数（去掉重复后）
    new_messages: int = 0  # 本轮新读到、且还没交给模型提炼的累计条数（pending_count）
    refreshed: bool = False
    skipped_reason: str = ""  # 非空表示这一轮没读消息，原因的中文说明
    # refresh=False 模式下：这轮该后台跑一次 refresh（提炼和/或每周整理），由 app 用长任务去做
    needs_refresh: bool = False


class Profiles:
    """群画像与活跃统计。构造参数：store, host, models, get_settings。"""

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
        # 上次真去宿主读消息的时刻（群 → epoch）：只有 tick 传 has_signal 时才做频率限制
        self._last_read: dict[str, float] = {}
        # 主模型读群发现请求（asks）的落地依赖：profiles 比 approvals/goals/outbox 先建，
        # app 启动时用 set_request_deps 接上；缺哪个，那一种 asks 就不落地（不报错）
        self._approvals: Any = None
        self._goals: Any = None
        self._outbox: Any = None

    def set_request_deps(self, *, approvals: Any = None, goals: Any = None, outbox: Any = None) -> None:
        """接上 asks 的落地依赖：prepare/goal → approvals.create；reminder → goals + outbox 回一句。"""
        if approvals is not None:
            self._approvals = approvals
        if goals is not None:
            self._goals = goals
        if outbox is not None:
            self._outbox = outbox

    # ------------------------------------------------------------------
    # 第二部分：攒够一批就调主模型提炼画像
    # ------------------------------------------------------------------

    # 每批交给主模型的消息条数上限（多批依次提炼，首次回读最多 1500 → 5 批）
    # 2026-09-29：网关约 129 秒断连接（流式也一样），300 条一批 glm 常要想 100~140 秒 → 120
    _REFRESH_BATCH = 120
    # 连续解析/调用失败多少次就跳过这批（推进游标、记事件，避免卡死）
    _MAX_FAILS = 3
    # 传给单条消息的文本上限（字）
    _MSG_TEXT_MAX = 200

    def _refresh_due(self, group_id: str, now: float, force: bool = False) -> bool:
        """提炼触发条件是不是满足了（纯查库 + 配置，不调模型、不读宿主）。

        触发：force；或 pending_count ≥ batch_messages；或距上次提炼 ≥ max_interval_hours
        且 pending ≥ 5；或首次（profile_ready_ts==0 且 pending>0）。模型没配好永远 False。
        """
        settings = self._get_settings()
        if not self._models.settings().ready():
            return False
        gid = str(group_id)
        row = self._store.read().execute(
            "SELECT pending_count, last_refresh_ts, profile_ready_ts FROM groups WHERE group_id=?",
            (gid,),
        ).fetchone()
        if row is None:
            return False
        pending = int(row["pending_count"] or 0)
        last_refresh = float(row["last_refresh_ts"] or 0)
        ready_ts = float(row["profile_ready_ts"] or 0)
        batch = max(1, int(settings.profile.batch_messages))
        return bool(
            force
            or pending >= batch
            or (
                last_refresh > 0
                and pending >= 5
                and now - last_refresh >= float(settings.profile.max_interval_hours) * 3600
            )
            or (ready_ts == 0 and pending > 0)
        )

    async def refresh(self, group_id: str, *, force: bool = False) -> bool:
        """后台慢活：提炼画像 + 每周整理（原来在 tick 里串行做，会卡住整个后台循环）。

        app 用长任务机制（_spawn_long_job，同群同种同时只跑一个）在后台调；
        直接调也行（测试 / 手动）。非服务群零调用。返回是否真调了模型。
        """
        gid = str(group_id)
        settings = self._get_settings()
        if not settings.is_served(gid):
            return False
        now = clock.now()
        did = False
        if await self._maybe_refresh(gid, now, force):
            did = True
        if await self._maybe_weekly(gid, now):
            did = True
        return did

    async def _maybe_refresh(self, group_id: str, now: float, force: bool) -> bool:
        """触发条件满足就调主模型提炼画像变更入库。模型没配好立刻 False（不调模型）。

        触发：force；或 pending_count ≥ batch_messages；或距上次提炼 ≥ max_interval_hours
        且 pending ≥ 5；或首次（profile_ready_ts==0 且 pending>0）。
        消息不在库里存原文，提炼前从 host 重新读 [last_refresh（首次用 read_since）, cursor_ts]。
        """
        settings = self._get_settings()
        if not self._models.settings().ready():
            return False
        gid = str(group_id)
        if not self._refresh_due(gid, now, force):
            return False
        row = self._store.read().execute(
            "SELECT pending_count, last_refresh_ts, profile_ready_ts, read_since,"
            " cursor_ts, session_id FROM groups WHERE group_id=?",
            (gid,),
        ).fetchone()
        if row is None:
            return False
        last_refresh = float(row["last_refresh_ts"] or 0)
        # 重读要提炼的消息区间：last_refresh 之后到 cursor；首次 last_refresh=0 用 read_since
        start = last_refresh if last_refresh > 0 else float(row["read_since"] or 0)
        msgs = await self._read_window(str(row["session_id"]), start, float(row["cursor_ts"]))
        if not msgs:
            return False
        for i in range(0, len(msgs), self._REFRESH_BATCH):
            batch_msgs = msgs[i : i + self._REFRESH_BATCH]
            ok = await self._refine_with_split(gid, batch_msgs, now)
            if not ok:
                return False
        return True

    async def _read_window(self, session_id: str, start: float, end: float) -> list:
        """按区间翻页读消息（不含 start 之前、含两端边界），最多 _PAGE_LIMIT*_MAX_PAGES 条。"""
        out: list = []
        cursor = start
        seen_ids: set = set()
        for _ in range(_MAX_PAGES):
            page = await self._host.messages(session_id, cursor, end, _PAGE_LIMIT, limit_mode="earliest")
            if not page:
                break
            # 不能按「这页不满 200 条」判读完：host.messages 会滤掉通知 / 缺 id 的记录，
            # 宿主给了满页也常常拿回来不满（2026-09-29 线上补整理每轮只读一页就停了）。
            # 改成：这页一条新消息都没有、或时间没往前走，才算读完。
            fresh = [m for m in page if str(m.id) not in seen_ids]
            if not fresh:
                break
            seen_ids.update(str(m.id) for m in fresh)
            out.extend(fresh)
            if page[-1].ts <= cursor and len(page) < _PAGE_LIMIT:
                break
            cursor = page[-1].ts
        # 分页重叠的同 ts 消息可能重复，按 id 去重
        seen: set = set()
        uniq: list = []
        for m in out:
            if str(m.id) in seen:
                continue
            seen.add(str(m.id))
            uniq.append(m)
        return uniq

    # 一批整批失败时，拆成两半各试一次的门槛（太小的批就不拆了）
    _SPLIT_MIN = 40
    # 整理画像的单次等待上限（秒）：step-5-preview 先在后台「思考」2k–9.5k token（约 70 token/秒），
    # 120 秒不够；超时只重试 1 次（同一批原样重复多次没用，线上 18 次全超时）
    # 2026-09-29：glm-5.3-flash 把思考写在正文里，端点默认 8192 token 就截断（半数回答被截）；
    # 给到 16000，流式约 55 token/秒，总时长放到 420 秒
    # 网关约 129 秒断连接：同一批原样重试没用，失败直接交给拆半（retries=0）；
    # 输出上限 12000（够写结果，想太久就截断 → 不带草稿再问一次，比被断强）
    _REFRESH_TIMEOUT_S = 420
    _REFRESH_RETRIES = 0
    _REFRESH_MAX_TOKENS = 12000
    _FORMAT_RETRY_TEXT = (
        "上面的回答读不懂，格式不对。请严格按「一行一件事」的格式重新输出，每行用「|」分段，"
        "例如：新增 | 最近在聊 | 一句话 | 3,5。不要 JSON、不要别的话；"
        "这批消息没有任何新变化就只输出一行：没有变化"
    )

    _TRUNCATED_RETRY_TEXT = (
        "刚才的回答太长被截断了。这次不要写分析过程，直接按「一行一件事」的格式输出结果，"
        "每行用「|」分段；这批消息没有任何新变化就只输出一行：没有变化"
    )

    @staticmethod
    def _truncated(result) -> bool:
        return str(getattr(result, "finish_reason", "") or "") == "length"

    def _read_answer(self, result):
        """解析一次回答；被 max_tokens 截断的一律当读不懂（草稿里夹着「新增 | …」半成品行，
        2026-09-29 线上这样读进过画像）。"""
        if self._truncated(result):
            return None
        return self._parse_output(result.text)

    async def _refine_with_split(self, gid: str, batch_msgs: list, now: float) -> bool:
        """提炼一批；整批失败且够大 → 拆两半各试一次（前半失败就停）；最终失败才记 fail_count。"""
        if await self._refine_batch(gid, batch_msgs, now, record_failure=False):
            return True
        if len(batch_msgs) >= self._SPLIT_MIN:
            half = len(batch_msgs) // 2
            for part in (batch_msgs[:half], batch_msgs[half:]):
                if not await self._refine_batch(gid, part, now, record_failure=False):
                    return self._batch_failed(gid, now, part)
            return True
        return self._batch_failed(gid, now, batch_msgs)

    async def _ask_model(self, gid: str, messages: list):
        return await self._models.chat(
            "main", messages, json_mode=False, purpose="profile.refresh", group_id=gid,
            timeout=self._REFRESH_TIMEOUT_S, retries=self._REFRESH_RETRIES,
            max_tokens=self._REFRESH_MAX_TOKENS,
        )

    async def _refine_batch(self, gid: str, batch_msgs: list, now: float, *, record_failure: bool = True) -> bool:
        """调主模型提炼一批消息。成功返回 True。

        读不懂（格式不对、回空 {}）不算成功：带着「格式不对」再问一次，还读不懂才算失败。
        失败时 record_failure=True 记 fail_count；False 交给调用方（拆半重试）决定。
        """
        from .models import ModelError  # 局部导入，避免和 TYPE_CHECKING 重复

        def failed() -> bool:
            return self._batch_failed(gid, now, batch_msgs) if record_failure else False

        messages = self._build_prompt(gid, batch_msgs)
        try:
            result = await self._ask_model(gid, messages)
        except ModelError:
            return failed()
        parsed = self._read_answer(result)
        if parsed is None:
            truncated = self._truncated(result)
            logger.info(
                "整理群画像：回答%s，再问一次（群 %s）：%s",
                "被截断" if truncated else "读不懂", gid, str(result.text or "")[:80],
            )
            if truncated:
                # 截断的多半是思考写太长的草稿：不把草稿塞回去（又长又会被照抄），直接要结果
                retry = [*messages, {"role": "user", "content": self._TRUNCATED_RETRY_TEXT}]
            else:
                retry = [
                    *messages,
                    {"role": "assistant", "content": str(result.text or "")[:2000]},
                    {"role": "user", "content": self._FORMAT_RETRY_TEXT},
                ]
            try:
                result = await self._ask_model(gid, retry)
            except ModelError:
                return failed()
            parsed = self._read_answer(result)
        if parsed is None:
            return failed()
        ops, people, asks = parsed
        op_counts: dict
        changed = False
        with self._store.tx() as conn:
            op_counts = self._apply_ops(conn, gid, ops, batch_msgs, now)
            changed = self._apply_people(conn, gid, people, now) or bool(
                sum(op_counts.values())
            )
            self._finish_batch_tx(conn, gid, batch_msgs, now, op_counts)
        if changed or sum(op_counts.values()):
            self._write_profile_md(gid)
        # 读群时顺带发现的请求（docs/02 §3.1/§5.1）：在 ops 事务之后落地，
        # 各自的 create 再开自己的事务（Store 不支持套事务）。落地出错只记日志，
        # 不影响这一批提炼的收账。
        try:
            self._apply_asks(gid, batch_msgs, asks)
        except Exception:
            logger.exception("落地读群发现的请求出错（群 %s）", gid)
        return True

    # ------------------------------------------------------------------
    # ops 应用
    # ------------------------------------------------------------------

    # add 和已有/墓碑文字相似度达到这个值就算同一条（已有的变 touch、墓碑的不许加回）
    _SIM_THRESHOLD = 0.85
    # 条目最多保留的证据消息数
    _EVIDENCE_MAX = 20
    # 单条文本一句话上限（字）；超了截断（不拒整条，模型啰嗦不至于废掉整批）
    _TEXT_MAX = 40

    def _apply_ops(self, conn, gid: str, ops: list, batch_msgs: list, now: float) -> dict:
        """整批在一个事务里应用 add/update/remove/touch；返回各类实际生效的计数。

        - add：类别必须五类之一、文本非空；和墓碑（deleted=1）相似 → 跳过；和已有未删
          条目相似 → 那条变 touch（不新开）。
        - update/remove：锁定、墓碑（deleted 非 0）无效；remove 打 deleted=2（和管理员
          墓碑 deleted=1 区分，之后同文字照样能再加回）。
        - touch：last_ts=now，evidence_count += 引用条数；证据按本批消息序号换成消息 id，
          与已有合并去重最多 _EVIDENCE_MAX 条。
        """
        from difflib import SequenceMatcher

        def similar(a: str, b: str) -> bool:
            return SequenceMatcher(None, a, b).ratio() >= self._SIM_THRESHOLD

        counts = {"add": 0, "update": 0, "remove": 0, "touch": 0}
        rows = conn.execute(
            "SELECT id, category, text, evidence, locked, deleted FROM profile_entries"
            " WHERE group_id=?",
            (gid,),
        ).fetchall()
        live = [dict(r) for r in rows if int(r["deleted"]) == 0]
        tombs = [dict(r) for r in rows if int(r["deleted"]) == 1]
        by_id = {int(r["id"]): r for r in live}

        def evidence_ids(op: dict) -> list[str]:
            """evidence 序号 → 本批消息 id（越界序号丢掉）。"""
            out: list[str] = []
            raw = op.get("evidence")
            if isinstance(raw, list):
                for x in raw:
                    try:
                        idx = int(x)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= idx <= len(batch_msgs):
                        out.append(str(batch_msgs[idx - 1].id))
            return out

        def evidence_ts(op: dict) -> list[float]:
            raw = op.get("evidence")
            out: list[float] = []
            if isinstance(raw, list):
                for x in raw:
                    try:
                        idx = int(x)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= idx <= len(batch_msgs):
                        out.append(float(batch_msgs[idx - 1].ts))
            return out

        def do_touch(entry_id: int, ids: list[str], add_count: int) -> None:
            row = conn.execute(
                "SELECT evidence, evidence_count FROM profile_entries WHERE id=?",
                (int(entry_id),),
            ).fetchone()
            if row is None:
                return
            try:
                old = [str(v) for v in json.loads(row["evidence"] or "[]")]
            except (TypeError, ValueError):
                old = []
            merged = old + [i for i in ids if i not in old]
            merged = merged[-self._EVIDENCE_MAX :]
            conn.execute(
                "UPDATE profile_entries SET last_ts=?,"
                " evidence_count=evidence_count+?, evidence=?, updated=?"
                " WHERE id=?",
                (now, max(0, add_count), json.dumps(merged, ensure_ascii=False), now, int(entry_id)),
            )
            counts["touch"] += 1

        for op in ops:
            kind = str(op.get("op") or "").strip().lower()
            if kind == "add":
                text = str(op.get("text") or "").strip()
                category = normalize_category(op.get("category") or op.get("cat"))
                if not text or category not in CATEGORIES:
                    if text:
                        logger.info("群 %s 画像条目类别认不出（%r），这条跳过", gid, op.get("category"))
                    continue
                # G7：关注成员的名字 / 注记不能进群画像（群友能看到画像）——整条丢弃
                if scrub(gid, text, self._store) is None:
                    continue
                text = text[: self._TEXT_MAX]
                if any(similar(text, str(t["text"])) for t in tombs):
                    continue  # 管理员删过的不许加回
                dup = next((r for r in live if similar(text, str(r["text"])) and str(r["category"]) == category), None)
                if dup is not None:
                    ids = evidence_ids(op)
                    if int(dup["locked"]) == 0:
                        do_touch(int(dup["id"]), ids, len(ids))
                    continue
                ts_list = evidence_ts(op)
                first_ts = min(ts_list) if ts_list else now
                last_ts = max(ts_list) if ts_list else now
                ids = evidence_ids(op)
                cur = conn.execute(
                    "INSERT INTO profile_entries"
                    " (group_id, category, text, evidence_count, evidence,"
                    " first_ts, last_ts, locked, deleted, source, updated)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'model', ?)",
                    (
                        gid,
                        category,
                        text,
                        len(ids),
                        json.dumps(ids[: self._EVIDENCE_MAX], ensure_ascii=False),
                        first_ts,
                        last_ts,
                        now,
                    ),
                )
                counts["add"] += 1
                live.append(
                    {"id": int(cur.lastrowid or 0), "category": category, "text": text,
                     "locked": 0, "deleted": 0, "evidence": "[]"}
                )
                by_id[int(cur.lastrowid or 0)] = live[-1]
            elif kind == "update":
                try:
                    eid = int(op.get("id"))
                except (TypeError, ValueError):
                    continue
                row0 = by_id.get(eid)
                if row0 is None or int(row0["locked"]):
                    continue
                text = str(op.get("text") or "").strip()
                if not text:
                    continue
                # G7：关注成员的名字 / 注记不能进群画像——更新作废，原文保留
                if scrub(gid, text, self._store) is None:
                    continue
                conn.execute(
                    "UPDATE profile_entries SET text=?, updated=? WHERE id=?",
                    (text[: self._TEXT_MAX], now, eid),
                )
                row0["text"] = text[: self._TEXT_MAX]
                counts["update"] += 1
            elif kind == "remove":
                try:
                    eid = int(op.get("id"))
                except (TypeError, ValueError):
                    continue
                row0 = by_id.get(eid)
                if row0 is None or int(row0["locked"]):
                    continue
                conn.execute(
                    "UPDATE profile_entries SET deleted=2, updated=? WHERE id=?",
                    (now, eid),
                )
                live = [r for r in live if int(r["id"]) != eid]
                counts["remove"] += 1
            elif kind == "touch":
                try:
                    eid = int(op.get("id"))
                except (TypeError, ValueError):
                    continue
                row0 = by_id.get(eid)
                if row0 is None:
                    continue
                ids = evidence_ids(op)
                do_touch(eid, ids, len(ids))
        return counts

    def _apply_people(self, conn, gid: str, people: list, now: float) -> bool:
        """people 只写当前关注成员（且 removed=0）；note 截 120 字；名单外的忽略。

        persona 已存在的成员不覆盖 note（persona 的 summary 优先，persona.py 入库时
        已同步成 note）。
        返回是否真的写了至少一条注记。
        """
        if not self._get_settings().focus.personal_profile:
            return False
        current = {
            str(r["user_id"])
            for r in conn.execute(
                "SELECT user_id FROM focus_members WHERE group_id=? AND removed=0",
                (gid,),
            )
        }
        with_persona = {
            str(r["user_id"])
            for r in conn.execute(
                "SELECT user_id FROM focus_members"
                " WHERE group_id=? AND removed=0 AND persona<>''",
                (gid,),
            )
        }
        wrote = False
        for p in people:
            uid = str(p.get("user_id") or "").strip()
            note = str(p.get("note") or "").strip()
            if not uid or not note or uid not in current or uid in with_persona:
                continue
            conn.execute(
                "UPDATE focus_members SET note=?, updated=? WHERE group_id=? AND user_id=?",
                (note[:120], now, gid, uid),
            )
            wrote = True
        return wrote

    # ------------------------------------------------------------------
    # asks：主模型读群时发现的请求（docs/02 §3.1 / §5.1）
    # ------------------------------------------------------------------

    _ASK_TITLE_MAX = 30
    _ASK_QUOTE_MAX = 500

    def _apply_asks(self, gid: str, batch_msgs: list, asks: list) -> None:
        """把这一批里发现的请求落地；pending_asks 中被判过的标 handled。

        - prepare → approvals.create(kind=task)；goal → approvals.create(kind=goal)；
          via 都写「主模型读群时发现」，requester 是那条消息的发言人；
          同一 message_id 已有请求（任意状态）就跳过，不重复建。
        - reminder 且 when 解析得出 → goals.create_member（who=发言人，remind_ts=due_ts=when），
          并经 outbox 在群里回一句固定话「记下了，<时间> 提醒你」（push_kind=status，
          reply_to 原消息）；同一 message_id 已经回过（发件箱 key 在）就跳过。
        - 机器人自己的消息、非服务群的消息绝不处理；依赖没接线（set_request_deps 没给）
          对应 kind 跳过且不标 handled，下次接上还能再判。
        """
        if not asks:
            return
        settings = self._get_settings()
        if not settings.is_served(gid):
            return
        approvals = self._approvals
        goals = self._goals
        outbox = self._outbox
        handled: list[str] = []
        for a in asks:
            try:
                idx = int(a.get("i"))
            except (TypeError, ValueError):
                continue
            if not (1 <= idx <= len(batch_msgs)):
                continue
            m = batch_msgs[idx - 1]
            if bool(getattr(m, "is_bot", False)):
                continue  # 机器人自己的消息绝不处理
            if is_command(getattr(m, "text", "")):
                continue  # 别的插件的指令，不是给 MaiWork 的活
            kind = str(a.get("kind") or "").strip()
            title = str(a.get("title") or "").strip()[: self._ASK_TITLE_MAX]
            if not title:
                title = str(getattr(m, "text", "") or "").strip()[: self._ASK_TITLE_MAX]
            mid = str(getattr(m, "id", "") or "")
            if kind in ("prepare", "goal"):
                if approvals is None:
                    continue
                if mid and self._request_exists(gid, mid):
                    handled.append(mid)  # 同一 message_id 已有请求：不重复建，但也算判过了
                    continue
                try:
                    approvals.create(
                        gid,
                        kind="task" if kind == "prepare" else "goal",
                        title=title or "群里的请求",
                        quote=str(getattr(m, "text", "") or "")[: self._ASK_QUOTE_MAX],
                        via="主模型读群时发现",
                        requester_id=str(getattr(m, "user_id", "") or ""),
                        requester_name=str(getattr(m, "user_name", "") or ""),
                        message_id=mid,
                    )
                except Exception:
                    logger.exception("读群发现的请求建待批出错（群 %s，消息 %s）", gid, mid)
                    continue
                handled.append(mid)
            elif kind == "reminder":
                if goals is None or outbox is None:
                    continue
                remind_ts = _parse_bj_when(str(a.get("when") or ""))
                if remind_ts is None:
                    # 模型判了是提醒但没给得出时间：不建目标，算判过（避免每轮重复问）
                    handled.append(mid)
                    continue
                key = f"ask-remind:{gid}:{mid}"
                if mid and self._outbox_key_exists(key):
                    handled.append(mid)  # 同一 message_id 只回一次
                    continue
                title = title or "提醒"
                who_name = str(getattr(m, "user_name", "") or "")
                goals.create_member(
                    gid,
                    who_id=str(getattr(m, "user_id", "") or ""),
                    who_name=who_name,
                    title=title,
                    due_ts=remind_ts,
                    remind_ts=remind_ts,
                )
                when_txt = clock.bj(remind_ts).strftime("%Y-%m-%d %H:%M")
                outbox.enqueue(
                    key,
                    gid,
                    "text",
                    {
                        "text": f"记下了，{when_txt} 提醒你",
                        "reply_to": mid,
                        "push_kind": "status",
                    },
                )
                handled.append(mid)
        # pending_asks 里被判过的标 handled（只有确实走到落地这一步的才标）
        if handled:
            try:
                with self._store.tx() as conn:
                    for mid in handled:
                        conn.execute(
                            "UPDATE pending_asks SET handled=1 WHERE group_id=? AND message_id=?",
                            (gid, mid),
                        )
            except Exception:
                logger.info("标 pending_asks handled 失败（群 %s）", gid, exc_info=True)

    def _request_exists(self, gid: str, message_id: str) -> bool:
        """同一 message_id 已有请求（intake 快路径或以往读群建的都算，任意状态）。"""
        row = self._store.read().execute(
            "SELECT 1 FROM requests WHERE group_id=? AND message_id=? LIMIT 1",
            (gid, str(message_id)),
        ).fetchone()
        return row is not None

    def _outbox_key_exists(self, key: str) -> bool:
        row = self._store.read().execute(
            "SELECT 1 FROM outbox WHERE key=? LIMIT 1", (str(key),)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 每周整理：合并重复、删掉过时的（recent 类 14 天没更新的优先）
    # ------------------------------------------------------------------

    # recent 类超过这么久没动过的条目视为「过时」，提示模型优先考虑删掉
    _WEEKLY_STALE_SECONDS = 14 * 86400
    # 两次整理的最小间隔（「每周」= 到星期且间隔够）
    _WEEKLY_MIN_GAP = 6 * 86400

    def _weekly_due(self, gid: str, now: float) -> bool:
        """每周整理到点了吗（纯查库 + 配置，不调模型）。模型没配好永远 False。"""
        settings = self._get_settings()
        if not self._models.settings().ready():
            return False
        row = self._store.read().execute(
            "SELECT profile_ready_ts, last_weekly_ts FROM groups WHERE group_id=?",
            (gid,),
        ).fetchone()
        if row is None:
            return False
        if float(row["profile_ready_ts"] or 0) == 0:
            return False
        if clock.bj(now).weekday() != int(settings.profile.weekly_day):
            return False
        if now - float(row["last_weekly_ts"] or 0) < self._WEEKLY_MIN_GAP:
            return False
        return True

    async def _maybe_weekly(self, gid: str, now: float) -> bool:
        """到点了（北京星期几 == weekly_day、距上次 ≥ 6 天、画像已成形）就整体整理一次。

        只带当前未删除条目，不许加新的：ops 里只保留 update/remove。
        失败不推进 last_weekly_ts（下次还可以补整）。
        """
        settings = self._get_settings()
        if not self._models.settings().ready():
            return False
        if not self._weekly_due(gid, now):
            return False

        conn = self._store.read()
        entries_rows = conn.execute(
            "SELECT id, category, text, locked, last_ts FROM profile_entries"
            " WHERE group_id=? AND deleted=0 ORDER BY category, last_ts DESC",
            (gid,),
        ).fetchall()
        if not entries_rows:
            return False
        lines = []
        for r in entries_rows:
            stale = "（过时）" if (
                str(r["category"]) == "recent"
                and now - float(r["last_ts"] or 0) >= self._WEEKLY_STALE_SECONDS
            ) else ""
            lines.append(
                f"- #{int(r['id'])} [{r['category']}] {r['text']}"
                + ("（锁定）" if int(r["locked"]) else "")
                + stale
            )
        prompt = [
            {"role": "system", "content": "你在为 MaiWork 整理一个 QQ 群的画像。只输出 JSON，不要其他话。"},
            {"role": "user", "content": (
                "下面是这个群当前的画像条目（锁定 = 不能改不能删；过时 = recent 类 14 天没更新）。\n\n"
                + "\n".join(lines) + "\n\n"
                "请整理：合并意思重复/重叠的条目（保留的用 update 改成合并后的一句话，"
                "多余的用 remove 删掉）；删掉已经过时、不再代表群现状的条目（标了「过时」的优先）。\n"
                "规则：只允许 update 和 remove，不许加新条目；锁定条目不能动；每条一句话不超过 40 字。\n"
                '输出格式：{"ops":[{"op":"update|remove","id":条目id,"text":"update 时必填"}]}'
                "（不需要整理就给空 ops）"
            )},
        ]
        from .models import ModelError as _ME
        try:
            result = await self._models.chat(
                "main", prompt, json_mode=True, purpose="profile.weekly", group_id=gid
            )
        except _ME:
            logger.info("群 %s 每周整理调模型失败，下周再整", gid)
            return False
        parsed = self._parse_output(result.text)
        if parsed is None:
            logger.info("群 %s 每周整理解析模型输出失败", gid)
            return False
        ops, _people, _asks = parsed
        kept_ops = [
            o for o in ops if str(o.get("op") or "").strip().lower() in ("update", "remove")
        ]
        now2 = clock.now()
        with self._store.tx() as tx_conn:
            counts = (
                self._apply_ops(tx_conn, gid, kept_ops, [], now)
                if kept_ops
                else {"add": 0, "update": 0, "remove": 0, "touch": 0}
            )
            tx_conn.execute(
                "UPDATE groups SET last_weekly_ts=? WHERE group_id=?", (now2, gid)
            )
            self._store.event(
                tx_conn,
                "profile.weekly",
                group_id=gid,
                entity="profile",
                payload={"update": counts.get("update", 0), "remove": counts.get("remove", 0)},
            )
        if counts.get("update") or counts.get("remove"):
            self._write_profile_md(gid)
        return True

    # ------------------------------------------------------------------
    # PROFILE-<群号>.md（G6：共享工作区的群各写各的，不再互相覆盖）
    # ------------------------------------------------------------------

    def _write_profile_md(self, gid: str) -> None:
        """条目有变化后同步 workspace_root/<workspace>/PROFILE-<群号>.md；
        目录不存在跳过（不创建、不报错）。

        内容只含群画像五类分节；不写任何关注成员信息。
        文件名带群号（G6）：多个群共享一个工作区时，原来统一的 PROFILE.md 会被
        互相覆盖；现在每群一份，共享工作区里的子 agent 能看到所有这些画像
        （设计允许的共享，配置问题清单里有提示）。
        """
        settings = self._get_settings()
        workspace = settings.workspace_of(gid)
        folder = settings.workspace_root / workspace
        if not folder.is_dir():
            logger.debug("工作区目录 %s 不存在，PROFILE-%s.md 跳过", folder, gid)
            return
        row = self._store.read().execute(
            "SELECT name FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        title = str(row["name"] or "") if row is not None else ""
        if not title:
            title = gid
        entries = self.entries(gid)
        cat_names = {
            "recent": "最近在聊",
            "interest": "长期兴趣",
            "ongoing": "在做的事",
            "convention": "约定和说法",
            "resource": "常用资源",
        }
        lines = [f"# {title} 群画像", ""]
        for cat in CATEGORIES:
            lines.append(f"## {cat_names[cat]}")
            items = [e for e in entries if e["category"] == cat]
            if items:
                for e in items:
                    lines.append(f"- {e['text']}")
            else:
                lines.append("（暂无）")
            lines.append("")
        try:
            (folder / f"PROFILE-{gid}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except OSError as e:
            logger.info("写 PROFILE-%s.md 失败（%s）：%s", gid, folder, type(e).__name__)

    def _batch_failed(self, gid: str, now: float, batch_msgs: list) -> bool:
        """这批提炼失败：fail_count+1；连续失败 3 次就推进游标跳过这批（记 profile.skip）。"""
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT fail_count FROM groups WHERE group_id=?", (gid,)
            ).fetchone()
            fails = int(row["fail_count"] or 0) + 1
            if fails >= self._MAX_FAILS:
                last_ts = float(batch_msgs[-1].ts)
                conn.execute(
                    "UPDATE groups SET fail_count=0, pending_count=0,"
                    " last_refresh_ts=MAX(last_refresh_ts, ?) WHERE group_id=?",
                    (last_ts, gid),
                )
                self._store.event(
                    conn,
                    "profile.skip",
                    group_id=gid,
                    entity="profile",
                    payload={"reason": "连续失败跳过", "failed": fails},
                )
            else:
                conn.execute(
                    "UPDATE groups SET fail_count=? WHERE group_id=?", (fails, gid)
                )
        return False

    def _finish_batch_tx(
        self, conn, gid: str, batch_msgs: list, now: float, op_counts: dict
    ) -> None:
        """整批成功的收账（在调用方事务里）：pending 清空、last_refresh 推到本批最后消息、
        首次写 profile_ready、fail_count 清零、记 profile.refresh 事件（各类 op 计数）。"""
        last_ts = float(batch_msgs[-1].ts)
        conn.execute(
            "UPDATE groups SET pending_count=0, fail_count=0,"
            " last_refresh_ts=MAX(last_refresh_ts, ?),"
            " profile_ready_ts=CASE WHEN profile_ready_ts=0 THEN ? ELSE profile_ready_ts END"
            " WHERE group_id=?",
            (last_ts, now, gid),
        )
        self._store.event(
            conn,
            "profile.refresh",
            group_id=gid,
            entity="profile",
            payload=dict(op_counts),
        )

    # -- 提示词 --

    _PROMPT_SYSTEM = (
        "你在为 MaiWork（QQ 群的后台助手）维护一个群的画像。你的任务：读一段群聊记录，"
        "更新这个群的画像条目和关注成员注记。按要求的「一行一件事」格式输出，不要写别的话。"
    )

    _PROMPT_RULES = (
        "规则：\n"
        "1. 只记群「整体」的话题、兴趣、在做的事、约定术语、常用资源这五类；"
        "不记某个人的隐私（手机号、住址、感情私事、不愿公开的个人信息等）。\n"
        "2. 每条一句话，不超过 40 字；每类最多约 8 条。\n"
        "3. 这批消息没有带来新变化就不动，只输出一行「没有变化」。\n"
        "4. 锁定的条目不能改、不能删。\n"
        "5. 管理员删除过的条目（墓碑）不许再加回来（换个说法也不行）。\n"
        "6. evidence 填支持这条判断的消息序号（方括号里那个数）。\n"
        "7. 关注成员只写关注成员的注记（他们是群里的关键人物，给管理员一个人看的），"
        "一句话不超过 120 字；没有新观察就空列表。\n"
        "8. 请求：这批消息里**明确对 MaiBot / 机器人说的请求**（@ 了它、点名它、或明确说"
        "「帮我整理 / 帮我准备 / 提醒我」这类），记一条；"
        "消息序号后面标了「这条 @ 了 MaiBot，Jev 没判出来，请你判断」的，请重点判断那条。"
        "kind 三选一：prepare=请它准备 / 整理 / 调研 / 做一个东西；goal=请它帮忙盯着某件事"
        "或把某件事做成；reminder=请它到某个时间提醒说话的人。\n"
        "   title 用不超过 30 字说清要什么；kind=reminder 且话里有明确时间时，when 填提醒"
        "时间（北京时间 YYYY-MM-DD HH:MM），没有明确时间就留空字符串。\n"
        "   泛泛的「谁来整理一下」这种没指明对机器人说的，不要收；拿不准是不是请求的不收。"
        "没有就空列表。机器人自己说的话（名字 MaiBot 的行）永远不收。"
        "以 / ! # 开头的是**别的插件的指令**（比如 /pic 画图），不是请 MaiWork 做事，永远不收。\n"
        "输出格式：一行一件事，每行用「|」分成几段，不要编号、不要 JSON、不要别的话。\n"
        "类别只能写这五个之一：最近在聊（recent）、长期兴趣（interest）、在做的事（ongoing）、"
        "约定和说法（convention）、常用资源（resource）。\n"
        "新增 | 类别 | 一句话 | 证据消息序号（逗号分开）\n"
        "修改 | #条目id | 改成的一句话\n"
        "删除 | #条目id\n"
        "还在聊 | #条目id | 证据消息序号（这条又被聊到了）\n"
        "关注成员 | QQ号 | 注记\n"
        "请求 | 消息序号 | prepare 或 goal 或 reminder | 不超过 30 字说清要什么 | 提醒时间（没有就空着）\n"
        "这批消息没有任何新变化，就只输出一行：没有变化"
    )

    def _build_prompt(self, gid: str, batch_msgs: list) -> list:
        """拼 system + user：当前条目 / 墓碑 / 关注成员（仅 personal_profile 开时）/ 消息。"""
        settings = self._get_settings()
        conn = self._store.read()
        current = conn.execute(
            "SELECT id, category, text, locked FROM profile_entries"
            " WHERE group_id=? AND deleted=0 ORDER BY category, last_ts DESC",
            (gid,),
        ).fetchall()
        tombs = conn.execute(
            "SELECT text FROM profile_entries WHERE group_id=? AND deleted=1",
            (gid,),
        ).fetchall()
        parts = [
            "当前画像条目（未删除）：",
            "\n".join(
                f"- #{int(r['id'])} [{r['category']}] {r['text']}"
                + ("（锁定）" if int(r["locked"]) else "")
                for r in current
            )
            or "（空）",
        ]
        parts.append(
            "管理员删除过的条目（墓碑，不许再加回来，换说法也不行）：\n"
            + ("\n".join(f"- {r['text']}" for r in tombs) if tombs else "（无）")
        )
        if settings.focus.personal_profile:
            members = conn.execute(
                "SELECT user_id, name, note FROM focus_members"
                " WHERE group_id=? AND removed=0",
                (gid,),
            ).fetchall()
            parts.append(
                "关注成员（只给他们写 people 注记；名单以外的人不要写）：\n"
                + (
                    "\n".join(
                        f"- user_id={r['user_id']} 名字={r['name'] or '?'}"
                        + (f" 现有注记：{r['note']}" if str(r["note"] or "") else "")
                        for r in members
                    )
                    if members
                    else "（无）"
                )
            )
        # pending_asks 里还没判的消息（@ 了 MaiBot 但 Jev 没判出来的）在消息行尾标出来
        pending_ask_ids: set[str] = set()
        try:
            pending_ask_ids = {
                str(r["message_id"])
                for r in conn.execute(
                    "SELECT message_id FROM pending_asks WHERE group_id=? AND handled=0",
                    (gid,),
                )
            }
        except Exception:
            logger.debug("读 pending_asks 标记失败（群 %s）", gid, exc_info=True)
        lines = []
        for i, m in enumerate(batch_msgs, 1):
            hhmm = clock.bj(float(m.ts)).strftime("%H:%M")
            name = "MaiBot" if m.is_bot else str(m.user_name)
            text = str(m.text)[: self._MSG_TEXT_MAX]
            flag = ""
            if str(m.id) in pending_ask_ids:
                flag = "（这条 @ 了 MaiBot，Jev 没判出来，请你判断）"
                pending_ask_ids.discard(str(m.id))
            lines.append(f"[{i}] {hhmm} {name}: {text}{flag}")
        parts.append("这批消息：\n" + "\n".join(lines))
        parts.append(self._PROMPT_RULES)
        return [
            {"role": "system", "content": self._PROMPT_SYSTEM},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    # -- 解析 --

    def _parse_output(self, text: str) -> tuple[list, list, list] | None:
        """解析模型输出，返回 (ops, people, asks)；读不懂返回 None（**不许**当成「没变化」）。

        2026-09-29 起主格式是「一行一件事」（见 _PROMPT_RULES）：每行单独读，坏行跳过，好行照收；
        一行都读不出、也没写「没有变化」→ None。
        兼容 JSON（老格式 / 别的模型）：{"ops":[…],"people":[…],"asks":[…]}；只给一条 op 的对象、
        顶层就是 op 列表也收（线上 step-5-preview 常这么回，内容是对的）；回空 {}、压扁成重复键 → None。
        """
        s = str(text or "").strip()
        if s.startswith("```"):
            s = s.strip("`").strip()
            if s.lower().startswith("json"):
                s = s[4:].strip()
        if s[:1] in "{[":
            return self._parse_json_output(s)
        return self._parse_lines(s)

    _LINE_KINDS = {
        "新增": "add", "add": "add", "修改": "update", "update": "update",
        "删除": "remove", "remove": "remove", "还在聊": "touch", "touch": "touch",
        "关注成员": "people", "people": "people", "请求": "ask", "ask": "ask",
    }

    @staticmethod
    def _evidence_of(field: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", str(field or ""))]

    @staticmethod
    def _id_of(field: str) -> int | None:
        m = re.search(r"\d+", str(field or ""))
        return int(m.group(0)) if m else None

    def _parse_lines(self, s: str) -> tuple[list, list, list] | None:
        ops: list[dict] = []
        people: list[dict] = []
        asks: list[dict] = []
        no_change = False
        for raw in s.splitlines():
            line = raw.strip().strip("`").strip()
            line = re.sub(r"^(?:[-*•]|\d+[.、)）])\s*", "", line)
            if not line:
                continue
            if line.replace("。", "").strip() in ("没有变化", "无变化", "没变化"):
                no_change = True
                continue
            parts = [x.strip() for x in re.split(r"[|｜]", line)]
            kind = self._LINE_KINDS.get(parts[0].lower() if parts else "")
            if kind is None:
                continue
            if kind == "add" and len(parts) >= 3:
                # 文本里可能自带「|」：最后一段像序号列表就当证据，中间都算文本
                tail_is_ev = len(parts) >= 4 and bool(re.fullmatch(r"[\d,，、\s]*", parts[-1]))
                body = parts[2:-1] if tail_is_ev else parts[2:]
                text_ = "|".join(body).strip()
                if text_:
                    ops.append({"op": "add", "category": parts[1], "text": text_,
                                "evidence": self._evidence_of(parts[-1]) if tail_is_ev else []})
            elif kind == "update" and len(parts) >= 3:
                eid = self._id_of(parts[1])
                text_ = "|".join(parts[2:]).strip()
                if eid is not None and text_:
                    ops.append({"op": "update", "id": eid, "text": text_})
            elif kind == "remove" and len(parts) >= 2:
                eid = self._id_of(parts[1])
                if eid is not None:
                    ops.append({"op": "remove", "id": eid})
            elif kind == "touch" and len(parts) >= 2:
                eid = self._id_of(parts[1])
                if eid is not None:
                    ops.append({"op": "touch", "id": eid,
                                "evidence": self._evidence_of(parts[2]) if len(parts) >= 3 else []})
            elif kind == "people" and len(parts) >= 3:
                uid = parts[1].strip()
                note = "|".join(parts[2:]).strip()
                if uid and note:
                    people.append({"user_id": uid, "note": note})
            elif kind == "ask" and len(parts) >= 4:
                idx = self._id_of(parts[1])
                if idx is not None:
                    asks.append({"i": idx, "kind": parts[2].strip().lower(), "title": parts[3].strip(),
                                 "when": parts[4].strip() if len(parts) >= 5 else ""})
        if not (ops or people or asks or no_change):
            return None
        return ops, people, asks

    def _parse_json_output(self, s: str) -> tuple[list, list, list] | None:
        def no_dup(pairs):
            keys = [k for k, _ in pairs]
            if len(keys) != len(set(keys)):
                raise ValueError("重复键（被压扁的多条）")
            return dict(pairs)

        start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
        end = max(s.rfind("}"), s.rfind("]"))
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(s[start : end + 1], object_pairs_hook=no_dup)
        except (ValueError, TypeError):
            return None
        if isinstance(data, list):
            data = {"ops": data}
        if not isinstance(data, dict):
            return None
        if "op" in data and not any(k in data for k in ("ops", "people", "asks")):
            data = {"ops": [data]}  # 只给了一条 op
        if not any(k in data for k in ("ops", "people", "asks")):
            return None  # 回空 {} / 结构对不上：读不懂，不当成「没变化」
        ops = data.get("ops")
        people = data.get("people")
        asks = data.get("asks")
        if ops is None:
            ops = []
        if people is None:
            people = []
        if asks is None:
            asks = []
        if not isinstance(ops, list) or not isinstance(people, list) or not isinstance(asks, list):
            return None
        if not all(isinstance(o, dict) for o in ops):
            return None
        if not all(isinstance(p, dict) for p in people):
            return None
        if not all(isinstance(a, dict) for a in asks):
            return None
        return [dict(o) for o in ops], [dict(p) for p in people], [dict(a) for a in asks]

    # ------------------------------------------------------------------
    # groups 行
    # ------------------------------------------------------------------

    @staticmethod
    def _new_token() -> str:
        """8 位字母数字随机的群链接码。"""
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.SystemRandom().choice(alphabet) for _ in range(8))

    def ensure_group(self, group_id: str) -> dict:
        """确保 groups 里有这群：没有就建（workspace 取配置、随机 token、created=now）。

        返回该行的 dict（与库里一致）。
        """
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM groups WHERE group_id=?", (str(group_id),)
            ).fetchone()
            if row is None:
                settings = self._get_settings()
                conn.execute(
                    "INSERT INTO groups (group_id, workspace, token, created)"
                    " VALUES (?, ?, ?, ?)",
                    (str(group_id), settings.workspace_of(str(group_id)), self._new_token(), now),
                )
                row = conn.execute(
                    "SELECT * FROM groups WHERE group_id=?", (str(group_id),)
                ).fetchone()
        return dict(row)

    def remember_session(self, group_id: str, session_id: str, last_msg_ts: float) -> None:
        """记下这群收消息的会话 ID 和最新消息时间（收消息钩子里调）。

        行不存在时按 ensure_group 的规则建；last_msg_ts 只增不减。
        """
        gid = str(group_id)
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT last_msg_ts FROM groups WHERE group_id=?", (gid,)
            ).fetchone()
            if row is None:
                settings = self._get_settings()
                conn.execute(
                    "INSERT INTO groups (group_id, workspace, session_id, token, created, last_msg_ts)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        gid,
                        settings.workspace_of(gid),
                        str(session_id),
                        self._new_token(),
                        clock.now(),
                        float(last_msg_ts),
                    ),
                )
                return
            new_ts = max(float(last_msg_ts), float(row["last_msg_ts"]))
            conn.execute(
                "UPDATE groups SET session_id=?, last_msg_ts=? WHERE group_id=?",
                (str(session_id), new_ts, gid),
            )

    # ------------------------------------------------------------------
    # tick：增量读消息 + 统计
    # ------------------------------------------------------------------

    async def tick(
        self,
        group_id: str,
        *,
        force: bool = False,
        refresh: bool = True,
        has_signal: bool | None = None,
    ) -> RefreshResult:
        """后台循环对每个服务群调：增量读消息 + 统计（快）；提炼不在这里等。

        参数：
        - refresh=True（默认，向后兼容）：tick 末尾照旧自己等 _maybe_refresh / _maybe_weekly
          （老测试 / 手动调用用这个）。
        - refresh=False（后台循环用）：只读消息 + 统计；要不要提炼 / 每周整理放在返回值的
          needs_refresh 里，由 app 用长任务机制后台跑 refresh()，不卡这一轮。
        - has_signal=None（默认）：不限制读消息频率（老的直接调用语义）；
          True=本轮有新消息信号，距上次读不足 60 秒就跳过读宿主；False=没信号，按
          [profile] read_interval_minutes 节流。跳过也只跳「读宿主」这一步。

        非服务群直接返回、不碰 host。拿到 session_id 后按游标增量读：
        - 首次（read_since==0）：只回读 profile.backfill_days 天、最多
          backfill_max_messages 条（取最新的），read_since 记成实际最早消息的 ts；
        - 之后：从 cursor_ts 读到 now，limit=200 分页（满 200 条就从最后一条的 ts
          接着读，最多 20 页），cursor_ids 里记过的同 ts 消息 id 直接丢。
        统计（同一事务和游标一起提交）：
        - 非机器人消息 → member_activity（北京日）+ activity_bins（15 分钟桶）；
        - 机器人消息只计 activity_bins，并记进 bot_messages（保留 7 天，顺手清旧）；
        - is_at 或 reply_to 在 bot_messages 里 → member_interactions（北京日）+1；
        - 本轮新消息数累加进 groups.pending_count。
        """
        settings = self._get_settings()
        gid = str(group_id)
        if not settings.is_served(gid):
            return RefreshResult(skipped_reason=_SKIP_NOT_SERVED)

        now = clock.now()
        # 读消息的频率（read_interval_minutes 生效）：
        # 本轮有信号（来了新消息）最多每 60 秒读一次宿主；没信号按 read_interval_minutes。
        if has_signal is not None:
            interval = _READ_WITH_SIGNAL_SECONDS if has_signal else (
                max(1, int(settings.profile.read_interval_minutes)) * 60.0
            )
            last = float(self._last_read.get(gid, 0.0))
            if last > 0.0 and now - last < interval:
                row0 = self._store.read().execute(
                    "SELECT pending_count FROM groups WHERE group_id=?", (gid,)
                ).fetchone()
                return RefreshResult(
                    read=0,
                    new_messages=int(row0["pending_count"] or 0) if row0 is not None else 0,
                    skipped_reason=_SKIP_THROTTLED,
                )

        # session_id：先用 groups 表里记的，没有再问 host
        row = self._store.read().execute(
            "SELECT session_id, cursor_ts, cursor_ids, read_since FROM groups WHERE group_id=?",
            (gid,),
        ).fetchone()
        session_id = str(row["session_id"]) if row is not None else ""
        # 还没读到过任何消息（read_since==0）的群不信表里存的：线上实测第一次可能拿到
        # 同群号的旧空会话，存下来就一直读空。每次都重新问 host（host 端按群号缓存）。
        never_read = row is not None and float(row["read_since"] or 0) == 0
        if not session_id or never_read:
            saved = session_id
            try:
                session_id = await self._host.session_for_group(gid)
            except Exception as e:
                logger.info("群 %s 拿不到会话 ID：%s", gid, type(e).__name__)
                session_id = ""
            if not session_id:
                if not saved:
                    return RefreshResult(skipped_reason=_SKIP_NO_SESSION)
                session_id = saved  # 问不到就先用表里存的
            self.remember_session(gid, session_id, 0.0)
            row = self._store.read().execute(
                "SELECT session_id, cursor_ts, cursor_ids, read_since FROM groups WHERE group_id=?",
                (gid,),
            ).fetchone()

        # 真要读宿主这一轮：记下读取时刻（节流只挡下一次）
        self._last_read[gid] = clock.now()
        now = clock.now()
        fresh = row is None or float(row["read_since"] or 0) == 0
        if fresh:
            start = now - float(settings.profile.backfill_days) * 86400
            read_ids: set[str] = set()
        else:
            start = float(row["cursor_ts"])
            try:
                read_ids = {str(x) for x in json.loads(row["cursor_ids"] or "[]")}
            except (TypeError, ValueError):
                read_ids = set()

        # 分页读新消息（游标之外的）。read_msgs 记本轮碰到过的每一条
        # （含被游标丢掉的），最后存 cursor_ids 时要用它——只看过滤后的 new_msgs
        # 会把「以前读过、这次又被丢掉」的同 ts 消息漏掉，下次又读一遍。
        new_msgs: list[Any] = []
        read_msgs: list[Any] = []
        cursor = start
        cursor_ids: set[str] = set(read_ids)  # 在 cursor 这个 ts 上已读过的 id
        if fresh:
            # 首次回读：一次取 backfill_days 天里最新的 backfill_max_messages 条（宿主 "latest"）
            first = await self._host.messages(
                session_id, start, now, int(settings.profile.backfill_max_messages), limit_mode="latest"
            )
            read_msgs.extend(first)
            new_msgs.extend(first)
        for _ in range(0 if fresh else _MAX_PAGES):
            page = await self._host.messages(session_id, cursor, now, _PAGE_LIMIT, limit_mode="earliest")
            read_msgs.extend(page)
            batch = [m for m in page if not (m.ts == cursor and m.id in cursor_ids)]
            new_msgs.extend(batch)
            if len(page) < _PAGE_LIMIT or not page:
                break
            cursor = page[-1].ts
            cursor_ids = {m.id for m in page if m.ts == cursor}

        if fresh and len(new_msgs) > int(settings.profile.backfill_max_messages):
            # 首次回读超量：只保留最新的 N 条
            new_msgs = new_msgs[-int(settings.profile.backfill_max_messages):]

        if new_msgs:
            new_cursor_ts = new_msgs[-1].ts
            new_cursor_ids = [m.id for m in read_msgs if m.ts == new_cursor_ts]
        elif row is not None and not fresh:
            new_cursor_ts = float(row["cursor_ts"])
            try:
                new_cursor_ids = [str(x) for x in json.loads(row["cursor_ids"] or "[]")]
            except (TypeError, ValueError):
                new_cursor_ids = []
        else:
            new_cursor_ts = start if fresh else 0.0
            new_cursor_ids = []

        # 统计 + 游标推进，同一事务
        with self._store.tx() as conn:
            # bot_messages 清掉 7 天前的（判断「回复了 MaiBot」只看近的）
            conn.execute(
                "DELETE FROM bot_messages WHERE group_id=? AND ts<?",
                (gid, now - _BOT_MSG_KEEP_SECONDS),
            )
            # 个人画像发言留存：只存当前关注成员（removed=0）、且开着 personal_profile；
            # 顺手清掉 14 天前的、每人超过 300 条只留最新（表是 _m_persona 之后才有的，
            # 旧库第一次 migrate 完就有，这里直接怼）
            focus_msg_targets: set[str] = set()
            if settings.focus.personal_profile:
                focus_msg_targets = {
                    str(r["user_id"])
                    for r in conn.execute(
                        "SELECT user_id FROM focus_members WHERE group_id=? AND removed=0",
                        (gid,),
                    )
                }
                conn.execute(
                    "DELETE FROM focus_messages WHERE group_id=? AND ts<?",
                    (gid, now - _FOCUS_MSG_KEEP_SECONDS),
                )
            known_bot_ids = {
                str(r["message_id"])
                for r in conn.execute(
                    "SELECT message_id FROM bot_messages WHERE group_id=?", (gid,)
                )
            }
            for m in new_msgs:
                conn.execute(
                    "INSERT INTO activity_bins (group_id, bin_ts, count) VALUES (?, ?, 1)"
                    " ON CONFLICT(group_id, bin_ts) DO UPDATE SET count=count+1",
                    (gid, math.floor(m.ts / _BIN_SECONDS) * _BIN_SECONDS),
                )
                if m.is_bot:
                    conn.execute(
                        "INSERT OR IGNORE INTO bot_messages (group_id, message_id, ts)"
                        " VALUES (?, ?, ?)",
                        (gid, str(m.id), m.ts),
                    )
                    known_bot_ids.add(str(m.id))
                    continue
                # 关注成员的发言留一份做个人画像素材（personal_profile 开时）
                if focus_msg_targets and str(m.user_id) in focus_msg_targets:
                    conn.execute(
                        "INSERT OR IGNORE INTO focus_messages"
                        " (group_id, user_id, ts, message_id, text)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (gid, str(m.user_id), m.ts, str(m.id),
                         str(m.text or "")[:_FOCUS_MSG_TEXT_MAX]),
                    )
                    # 每人最多 300 条：删掉超出部分的旧行
                    conn.execute(
                        "DELETE FROM focus_messages WHERE group_id=? AND user_id=?"
                        " AND message_id NOT IN ("
                        " SELECT message_id FROM focus_messages"
                        " WHERE group_id=? AND user_id=? ORDER BY ts DESC, message_id DESC"
                        " LIMIT ?)",
                        (gid, str(m.user_id), gid, str(m.user_id), _FOCUS_MSG_KEEP_MAX),
                    )
                day = clock.day_key(m.ts)
                conn.execute(
                    "INSERT INTO member_activity (group_id, user_id, day, count, name)"
                    " VALUES (?, ?, ?, 1, ?)"
                    " ON CONFLICT(group_id, user_id, day) DO UPDATE SET"
                    " count=count+1, name=excluded.name",
                    (gid, str(m.user_id), day, str(m.user_name)),
                )
                is_reply_to_bot = bool(m.reply_to) and str(m.reply_to) in known_bot_ids
                if m.is_at or is_reply_to_bot:
                    conn.execute(
                        "INSERT INTO member_interactions (group_id, user_id, day, count)"
                        " VALUES (?, ?, ?, 1)"
                        " ON CONFLICT(group_id, user_id, day) DO UPDATE SET count=count+1",
                        (gid, str(m.user_id), day),
                    )
            read_since = (
                min(m.ts for m in new_msgs)
                if (fresh and new_msgs)
                else (float(row["read_since"]) if row is not None else 0.0)
            )
            conn.execute(
                "INSERT INTO groups (group_id, workspace, session_id, token, created,"
                " cursor_ts, cursor_ids, read_since, pending_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(group_id) DO UPDATE SET"
                " session_id=excluded.session_id, cursor_ts=excluded.cursor_ts,"
                " cursor_ids=excluded.cursor_ids, read_since=excluded.read_since,"
                " pending_count=groups.pending_count+excluded.pending_count",
                (
                    gid,
                    settings.workspace_of(gid),
                    session_id,
                    self._new_token(),
                    now,
                    new_cursor_ts,
                    json.dumps(new_cursor_ids, ensure_ascii=False),
                    read_since,
                    len(new_msgs),
                ),
            )
            if new_msgs:
                # 网页「安静多久」看 last_msg_ts：推进到本轮新消息的最大 ts（只增不减）
                conn.execute(
                    "UPDATE groups SET last_msg_ts=MAX(last_msg_ts, ?) WHERE group_id=?",
                    (new_cursor_ts, gid),
                )

        # 「有人味」的 chat_log（§4.1）：统计之后，本轮新消息进全文检索副本 + 每天清 14 天前的。
        # 出错记日志，绝不影响画像统计。
        try:
            from . import chatlog

            chatlog.record_messages(self._store, gid, new_msgs, now=now)
            chatlog.prune_old(self._store, gid, now=now)
        except Exception:
            logger.info("chat_log 维护出错（群 %s）", gid, exc_info=True)

        row2 = self._store.read().execute(
            "SELECT pending_count FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        pending = int(row2["pending_count"]) if row2 is not None else len(new_msgs)

        refreshed = False
        needs_refresh = False
        if refresh:
            # 老语义（默认）：tick 里直接等提炼 / 每周整理（手动调用、老测试用这个）
            refreshed = await self._maybe_refresh(gid, now, force)
            await self._maybe_weekly(gid, now)
        else:
            # 后台循环用：慢活不在 tick 里等；要不要后台跑 refresh 由返回值带去
            needs_refresh = self._refresh_due(gid, now, force) or self._weekly_due(gid, now)

        # 群信息：名字空或超过一天没更新就问宿主（拿不到跳过）
        await self._refresh_group_info(gid, now)

        return RefreshResult(
            read=len(new_msgs),
            new_messages=pending,
            refreshed=refreshed,
            skipped_reason="",
            needs_refresh=needs_refresh,
        )

    async def _refresh_group_info(self, group_id: str, now: float) -> None:
        """群名空或 info_ts 超过一天 → host.group_info；拿不到就跳过（保持原样）。"""
        row = self._store.read().execute(
            "SELECT name, info_ts FROM groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if row is None:
            return
        if str(row["name"] or "") and now - float(row["info_ts"] or 0) < _INFO_STALE_SECONDS:
            return
        try:
            info = await self._host.group_info(group_id)
        except Exception as e:
            logger.info("群 %s 取群信息失败：%s", group_id, type(e).__name__)
            return
        if not isinstance(info, dict):
            return
        # 群名进库就洗（names.py：QQ 特殊标记的错误解码、控制字符），视图直接拿干净值。
        # 「群里本来就没名 / 拿到个空」→ 洗出来是回退名「群 <群号>」，
        # 只有它 + 没人数时按「没拿到」跳过重试（别把回退名当真名钉死在库里）。
        from .names import clean_group_name

        raw_name = info.get("group_name") or info.get("name") or ""
        name = clean_group_name(raw_name, str(group_id))
        member_count = int(info.get("member_count") or info.get("member_num") or 0)
        if not str(raw_name or "").strip() and member_count <= 0:
            return
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE groups SET name=?, member_count=?, info_ts=? WHERE group_id=?",
                (name, member_count, now, group_id),
            )

    # ------------------------------------------------------------------
    # 画像条目的管理员操作
    # ------------------------------------------------------------------

    def entries(self, group_id: str) -> list[dict]:
        """未删除（deleted=0）的条目，按 category、last_ts 倒序。"""
        rows = self._store.read().execute(
            "SELECT id, group_id, category, text, evidence_count, evidence,"
            " first_ts, last_ts, confidence, locked, deleted, source, updated"
            " FROM profile_entries WHERE group_id=? AND deleted=0"
            " ORDER BY category ASC, last_ts DESC, id DESC",
            (str(group_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_entry(self, group_id: str, category: str, text: str) -> int:
        """管理员新增条目：source=admin、locked=1；类别必须是五类之一。"""
        gid = str(group_id)
        category = str(category).strip()
        text = str(text).strip()
        if category not in CATEGORIES:
            raise ValueError(f"画像类别必须是 {CATEGORIES} 之一，收到: {category!r}")
        if not text:
            raise ValueError("画像条目文本不能为空")
        now = clock.now()
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO profile_entries"
                " (group_id, category, text, evidence_count, first_ts, last_ts,"
                "  locked, deleted, source, updated)"
                " VALUES (?, ?, ?, 0, ?, ?, 1, 0, 'admin', ?)",
                (gid, category, text, now, now, now),
            )
            entry_id = int(cur.lastrowid or 0)
            self._store.event(
                conn,
                "profile_entry.add",
                group_id=gid,
                entity="profile_entry",
                entity_id=str(entry_id),
                payload={"category": category, "text": text},
            )
        return entry_id

    def edit_entry(
        self,
        entry_id: int,
        *,
        text: str | None = None,
        locked: bool | None = None,
    ) -> None:
        """管理员改条目：改了文字就自动锁定（locked=1）；只调整锁定状态也行。"""
        row = self._store.read().execute(
            "SELECT id, group_id, text, locked, deleted FROM profile_entries WHERE id=?",
            (int(entry_id),),
        ).fetchone()
        if row is None or int(row["deleted"]):
            raise ValueError(f"画像条目不存在: {entry_id}")
        new_text: str | None = None
        if text is not None:
            new_text = str(text).strip()
            if not new_text:
                raise ValueError("画像条目文本不能为空")
        if text is None and locked is None:
            return
        # 改文字自动锁；只改 locked 时用传入值
        new_locked = 1 if text is not None else (1 if locked else 0)
        now = clock.now()
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE profile_entries SET text=COALESCE(?, text), locked=?, updated=?"
                " WHERE id=?",
                (new_text, new_locked, now, int(entry_id)),
            )
            self._store.event(
                conn,
                "profile_entry.edit",
                group_id=str(row["group_id"]),
                entity="profile_entry",
                entity_id=str(entry_id),
                payload={"text_changed": text is not None, "locked": bool(new_locked)},
            )

    def delete_entry(self, entry_id: int) -> None:
        """管理员删条目：打墓碑（deleted=1），不真删行。"""
        row = self._store.read().execute(
            "SELECT id, group_id, deleted FROM profile_entries WHERE id=?",
            (int(entry_id),),
        ).fetchone()
        if row is None or int(row["deleted"]):
            raise ValueError(f"画像条目不存在: {entry_id}")
        now = clock.now()
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE profile_entries SET deleted=1, updated=? WHERE id=?",
                (now, int(entry_id)),
            )
            self._store.event(
                conn,
                "profile_entry.delete",
                group_id=str(row["group_id"]),
                entity="profile_entry",
                entity_id=str(entry_id),
            )

    # ------------------------------------------------------------------
    # 群脉搏 / 平时发言间隔
    # ------------------------------------------------------------------

    def pulse(self, group_id: str, *, end: float, hours: int = 24) -> list[int]:
        """hours*4 个 15 分钟桶；最后一桶覆盖 end 所在 bin（floor 对齐），缺的补 0。"""
        n = max(1, int(hours)) * 4
        last_bin = math.floor(float(end) / _BIN_SECONDS) * _BIN_SECONDS
        first_bin = last_bin - (n - 1) * _BIN_SECONDS
        rows = self._store.read().execute(
            "SELECT bin_ts, count FROM activity_bins"
            " WHERE group_id=? AND bin_ts>=? AND bin_ts<=?",
            (str(group_id), first_bin, last_bin),
        ).fetchall()
        by_bin = {int(r["bin_ts"]): int(r["count"]) for r in rows}
        return [by_bin.get(first_bin + i * _BIN_SECONDS, 0) for i in range(n)]

    def usual_gap(self, group_id: str, ts: float) -> float | None:
        """最近 21 天（不含今天）、北京时间和 ts 同一钟点，平均每隔多少秒一条消息。

        有数据（>0）的天数 < 5 视为样本不足，返回 None；否则平均每钟点条数
        n = 总条数 / 有数据天数，返回 3600/n。
        """
        t = clock.bj(float(ts))
        start_of_today = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        start = start_of_today - _USUAL_DAYS * 86400
        hour = t.hour
        rows = self._store.read().execute(
            "SELECT bin_ts, count FROM activity_bins"
            " WHERE group_id=? AND bin_ts>=? AND bin_ts<?",
            (str(group_id), start, start_of_today),
        ).fetchall()
        per_day: dict[str, int] = {}
        for r in rows:
            if clock.bj(float(r["bin_ts"])).hour != hour:
                continue
            day = clock.day_key(float(r["bin_ts"]))
            per_day[day] = per_day.get(day, 0) + int(r["count"])
        days_with_data = sum(1 for v in per_day.values() if v > 0)
        if days_with_data < _USUAL_MIN_SAMPLES:
            return None
        per_hour = sum(per_day.values()) / days_with_data
        if per_hour <= 0:
            return None
        return 3600.0 / per_hour

    # ------------------------------------------------------------------
    # 关注成员
    # ------------------------------------------------------------------

    _REASON_ACTIVE = "最活跃"
    _REASON_CHAT = "和 MaiBot 聊得多"  # 已退役：不再用来挑人；member_interactions 数据保留
    _REASON_REQ = "提过请求"
    _REASON_PIN = "管理员加的"

    def focus(
        self,
        group_id: str,
        requesters: list[str] | None = None,
    ) -> list[dict]:
        """重算关注成员并同步 focus_members 表。

        候选：近 30 天发言前 3（最活跃）、requesters 里的（提过请求）、管理员
        pinned 的（管理员加的）。removed 排除。member_interactions 的互动计数
        数据保留（不删），但不再用来挑人（2026-09-27 起）。
        最多 settings.focus.max_members 个；落选且非 pinned 的删行（个人画像
        persona 和发言留存 focus_messages 随之删除）。
        personal_profile=False 时清空所有 note / persona、删掉所有 focus_messages。
        """
        settings = self._get_settings()
        gid = str(group_id)
        now = clock.now()
        cutoff = now - _FOCUS_WINDOW_SECONDS
        max_members = max(1, int(settings.focus.max_members))
        conn = self._store.read()

        removed = {
            str(r["user_id"])
            for r in conn.execute(
                "SELECT user_id FROM focus_members WHERE group_id=? AND removed=1",
                (gid,),
            )
        }
        pinned_rows = conn.execute(
            "SELECT user_id, name FROM focus_members WHERE group_id=? AND pinned=1",
            (gid,),
        ).fetchall()

        # 近 30 天发言数前 3
        active = [
            str(r["user_id"])
            for r in conn.execute(
                "SELECT user_id FROM member_activity"
                " WHERE group_id=? AND day >= ?"
                " GROUP BY user_id ORDER BY SUM(count) DESC, user_id ASC LIMIT 3",
                (gid, clock.day_key(cutoff)),
            )
        ]

        reasons_user: dict[str, list[str]] = {}

        def _add(user_id: str, reason: str) -> None:
            if user_id and user_id not in removed:
                rs = reasons_user.setdefault(user_id, [])
                if reason not in rs:
                    rs.append(reason)

        for u in active:
            _add(u, self._REASON_ACTIVE)
        for u in (str(x) for x in (requesters or [])):
            _add(u, self._REASON_REQ)
        pinned_ids = [str(r["user_id"]) for r in pinned_rows]
        for u in pinned_ids:
            _add(u, self._REASON_PIN)

        # 排序：pinned 优先，其次理由多的，其次 user_id，保证稳定
        ranked = sorted(
            reasons_user.items(),
            key=lambda kv: (0 if kv[0] in pinned_ids else 1, -len(kv[1]), kv[0]),
        )
        if len(ranked) > max_members:
            pinned_out = [kv for kv in ranked[max_members:] if kv[0] in pinned_ids]
            keep_n = max_members - len(pinned_out)
            ranked = ranked[:keep_n] + pinned_out
        selected = [u for u, _ in ranked]

        # 名字：member_activity 里最近一天的名字
        def _latest_name(user_id: str) -> str:
            row = conn.execute(
                "SELECT name FROM member_activity WHERE group_id=? AND user_id=?"
                " ORDER BY day DESC LIMIT 1",
                (gid, user_id),
            ).fetchone()
            if row is not None and str(row["name"] or ""):
                return str(row["name"])
            return user_id

        with self._store.tx() as tx_conn:
            # 落选且非 pinned 的删行（个人画像 persona 和发言留存 focus_messages 随之删除）
            existing = tx_conn.execute(
                "SELECT user_id, pinned FROM focus_members WHERE group_id=? AND removed=0",
                (gid,),
            ).fetchall()
            for r in existing:
                u = str(r["user_id"])
                if u not in selected and not int(r["pinned"]):
                    tx_conn.execute(
                        "DELETE FROM focus_members WHERE group_id=? AND user_id=?",
                        (gid, u),
                    )
                    tx_conn.execute(
                        "DELETE FROM focus_messages WHERE group_id=? AND user_id=?",
                        (gid, u),
                    )
            # 入选的 upsert
            for u in selected:
                name = _latest_name(u)
                rs = reasons_user[u]
                tx_conn.execute(
                    "INSERT INTO focus_members"
                    " (group_id, user_id, name, reasons, pinned, removed, updated)"
                    " VALUES (?, ?, ?, ?, 0, 0, ?)"
                    " ON CONFLICT(group_id, user_id) DO UPDATE SET"
                    " name=excluded.name, reasons=excluded.reasons,"
                    " removed=0, updated=excluded.updated",
                    (gid, u, name, json.dumps(rs, ensure_ascii=False), now),
                )
            if not settings.focus.personal_profile:
                # 关掉个人画像：note / persona 全清、发言留存全删
                tx_conn.execute(
                    "UPDATE focus_members SET note='' WHERE group_id=? AND note<>''",
                    (gid,),
                )
                tx_conn.execute(
                    "UPDATE focus_members SET persona='', persona_ts=0"
                    " WHERE group_id=? AND (persona<>'' OR COALESCE(persona_ts,0)>0)",
                    (gid,),
                )
                tx_conn.execute(
                    "DELETE FROM focus_messages WHERE group_id=?",
                    (gid,),
                )

        rows = conn.execute(
            "SELECT user_id, name, card, nickname, reasons, note, pinned, persona, persona_ts"
            " FROM focus_members WHERE group_id=? AND removed=0",
            (gid,),
        ).fetchall()
        by_user: dict[str, dict] = {}
        for r in rows:
            try:
                rs = [str(x) for x in json.loads(r["reasons"] or "[]")]
            except (TypeError, ValueError):
                rs = []
            by_user[str(r["user_id"])] = {
                "user_id": str(r["user_id"]),
                "name": str(r["name"] or ""),
                # 群名片 / QQ 昵称（adapter 查到后缓存；视图拼 display_name 用）
                "card": str(r["card"] or ""),
                "nickname": str(r["nickname"] or ""),
                "reasons": rs,
                "note": str(r["note"] or ""),
                "pinned": bool(r["pinned"]),
                "persona": str(r["persona"] or ""),
                "persona_ts": float(r["persona_ts"] or 0),
            }
        return [by_user[u] for u in selected if u in by_user]

    def set_focus(self, group_id: str, user_id: str, action: str) -> None:
        """管理员手动调整关注：add→pinned=1,removed=0；remove→removed=1,pinned=0,note=''；
        auto→两个都 0。行不存在就建。"""
        gid = str(group_id)
        uid = str(user_id).strip()
        action = str(action).strip()
        if not uid:
            raise ValueError("user_id 不能为空")
        if action not in ("add", "remove", "auto"):
            raise ValueError(f"action 必须是 add/remove/auto，收到: {action!r}")
        now = clock.now()
        if action == "add":
            pinned, removed, note_sql = 1, 0, None
        elif action == "remove":
            pinned, removed, note_sql = 0, 1, ""
        else:
            pinned, removed, note_sql = 0, 0, None
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members"
                " (group_id, user_id, pinned, removed, updated)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(group_id, user_id) DO UPDATE SET"
                " pinned=excluded.pinned, removed=excluded.removed,"
                " updated=excluded.updated",
                (gid, uid, pinned, removed, now),
            )
            if note_sql is not None:
                conn.execute(
                    "UPDATE focus_members SET note='' WHERE group_id=? AND user_id=?",
                    (gid, uid),
                )
                # 移除关注：个人画像和发言留存一起删
                conn.execute(
                    "UPDATE focus_members SET persona='', persona_ts=0"
                    " WHERE group_id=? AND user_id=?",
                    (gid, uid),
                )
                conn.execute(
                    "DELETE FROM focus_messages WHERE group_id=? AND user_id=?",
                    (gid, uid),
                )
            self._store.event(
                conn,
                "focus_member.set",
                group_id=gid,
                entity="focus_member",
                entity_id=uid,
                payload={"action": action},
            )


_COMMAND_PREFIXES = ("/", "／", "!", "！", "#")


def is_command(text: str) -> bool:
    """别的插件的指令（/pic、!remind、#xxx 之类）：不是请 MaiWork 做事。
    线上实测群友发 `/pic …` 被当成了请求。/mw 在收消息钩子里另有专门处理，不走这里。"""
    t = str(text or "").lstrip()
    return bool(t) and t.startswith(_COMMAND_PREFIXES)


_WHEN_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S")


def _parse_bj_when(text: str) -> float | None:
    """asks 的 when 字段：「北京时间 YYYY-MM-DD HH:MM」→ epoch 秒；解析不出返回 None。"""
    from datetime import datetime

    s = str(text or "").strip()
    if not s:
        return None
    for fmt in _WHEN_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=clock.BJ).timestamp()
        except ValueError:
            continue
    return None
