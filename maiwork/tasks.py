"""M3 数据层：任务（docs/02 §7.2 状态机、docs/07 §9.3 / §11.2）。

只放数据和规则：不调模型、不调宿主、不发消息。

要点：
- 合法转移严格按 02 §7.2 的表；非法转移抛 ValueError（中文），状态不变。
- transition 在一个事务里完成：改状态 + 更新 updated + 写事件（kind "task.<to>"）。
- 到 running 第一次记 started_ts（重进不清）；到任一终态记 finished_ts。
- revise 存新需求版本；任务在 running/reviewing 时排回 queued（旧尝试作废）。
- 取消 / 失败 / 完成 / 暂停后，晚到的结果一律不接（accept_result=False），attempt 标 stale，
  历史保留，任务状态绝不复活（红线：「取消 → …晚到的结果只记历史，不会让任务复活或再次交付」）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import nullcontext
from typing import Any, Callable, Iterable

from . import clock, members, requirements
from .lanes import close_task_lanes
from .store import Store, next_id

_STATUS_KINDS = (
    "pending_approval", "queued", "running", "waiting_input", "shelved",
    "reviewing", "completed", "failed", "paused", "cancelled", "rejected",
)

# 02 §7.2 的合法转移表：任何不在表里的转移都是非法的
_LEGAL: dict[str, frozenset[str]] = {
    "pending_approval": frozenset({"queued", "rejected", "cancelled"}),
    "queued": frozenset({"running", "paused", "cancelled"}),
    "running": frozenset({"reviewing", "waiting_input", "failed", "paused", "cancelled", "queued"}),
    "reviewing": frozenset({"completed", "running", "queued", "failed", "waiting_input", "paused", "cancelled"}),
    "waiting_input": frozenset({"queued", "running", "shelved", "cancelled"}),
    "shelved": frozenset({"queued", "cancelled"}),
    "paused": frozenset({"queued", "cancelled"}),
    "failed": frozenset({"queued"}),   # 失败只允许重试（回 queued）
    "completed": frozenset(),           # 终态；只允许重新交付，不改状态
    "cancelled": frozenset(),
    "rejected": frozenset(),
}

# transition(**fields) 允许顺带更新的列（delivery 给 list 会序列化成 JSON）
_MUTABLE_FIELDS = frozenset({
    "review", "question", "question_ts", "question_msg_id",
    "env", "delivery_kind", "delivery", "undelivered", "tokens",
    "paused_reason",  # 安全网（0.4.0）：自动暂停原因 JSON；queued 时自动清空
})

_TERMINAL = frozenset({"completed", "cancelled", "rejected"})
# 等发起人回答的提问（缺信息 / 做不到 / 需要有人参与 / 6 小时「还在等回答」）发进群时
# 末尾统一加这句（2026-10-10，线上 T-14）：只有引用回复提问、或发起人 @ 机器人才认得出
# 是回答，直接在群里说一句认不出来。只加在群消息上，任务里存的 question 不带。
ANSWER_HINT = "（引用这条消息回复我就行）"


def _load_json(s: Any, default: Any) -> Any:
    try:
        return json.loads(s) if s else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 暂停原因（paused_reason）结构化 schema（0.4.0 安全网 + 2026-10 能力闸收口）
#
# 为什么要有 schema：暂停原因会进网页的任务列表和详情（总管理员 / 群管理员 / 群友
# 三种身份都看得到），所以只留「说得清 + 不泄密」的最小结构，写入时严格校验、限长：
#   {"kind": "tokens"|"time", "limit": int>0, "used": int>=0}   安全网自动暂停
#   {"kind": "capability", "text": "中文一句", "jobs": [int, ...]} 开工前能力闸暂停
# 三处视图（list_view / 管理员详情 / 群友详情）都从这里派生一句安全中文 `text`。
# 手动暂停（网页 / /mw 点「暂停」）没有原因：paused_reason 为空，视图不编造
# 「时长 0」这类假原因。
# ---------------------------------------------------------------------------
_PAUSED_KINDS = frozenset({"tokens", "time", "capability"})
_PAUSED_TEXT_MAX = 400   # 中文一句话的硬上限（能力闸原因本来就写成大白话，超长一律截断）
_PAUSED_JOBS_MAX = 8     # 最多记 8 条出问题的活（只用来指路，不记 job 详情）
_SECRET_IN_TEXT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|token|secret|password|passwd|credential)"
    r"\s*[=:：]\s*[^\s，。；、）】\"']+"
)
_ABSPATH_IN_TEXT = re.compile(r"(?<![\w.])~(?:/[\w.\-]+)+|(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]+")
_ACCOUNT_IN_TEXT = re.compile(r"\d{6,}")

# 「为什么停」的一句话（列表 meta 用；详情直接用带数字/原话的 text）
_PAUSED_META = {
    "tokens": "自动暂停：用量到上限了，等你决定",
    "time": "自动暂停：做得太久了，等你决定",
    "capability": "自动暂停：开工前对不上，等你决定",
}


def _safe_paused_text(text: Any, *, limit: int = _PAUSED_TEXT_MAX) -> str:
    """把一句暂停原因洗成能公开给群友看的中文。

    - 折叠空白、截断到 limit；
    - 隐去明显的密钥 / 口令（`api_key=xxx`）、内部绝对路径（`/root/.typesafe_key`）
      和长数字（QQ 号这类账号）：暂停原因里只该有「哪条活、为什么、下一步怎么办」。
    """
    s = " ".join(str(text or "").split())
    s = _SECRET_IN_TEXT.sub(lambda m: f"{m.group(1)}=[已隐去]", s)
    s = _ABSPATH_IN_TEXT.sub("[路径]", s)
    s = _ACCOUNT_IN_TEXT.sub("[已隐去]", s)
    return s[: max(1, int(limit))]


def _clean_paused_reason(value: Any) -> dict | None:
    """校验并清洗一条暂停原因；不合法（kind 不认识 / 缺关键信息）返回 None。"""
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip()
    if kind not in _PAUSED_KINDS:
        return None
    if kind in ("tokens", "time"):
        try:
            limit = int(value.get("limit") or 0)
            used = int(value.get("used") or 0)
        except (TypeError, ValueError):
            return None
        if limit <= 0 or used < 0:
            # 没上限 / 负数的「暂停原因」是脏数据：宁可不显示，也不编一个 0 时长
            return None
        return {"kind": kind, "limit": limit, "used": used}
    text = _safe_paused_text(value.get("text") or value.get("reason") or "")
    if not text:
        return None
    out: dict[str, Any] = {"kind": "capability", "text": text}
    jobs = value.get("jobs")
    clean_jobs: list[int] = []
    if isinstance(jobs, (list, tuple)):
        for item in jobs:
            if len(clean_jobs) >= _PAUSED_JOBS_MAX:
                break
            try:
                n = int(item)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in clean_jobs:
                clean_jobs.append(n)
    if clean_jobs:
        out["jobs"] = clean_jobs
    return out


def _paused_reason_for_store(value: Any) -> Any:
    """transition 用：dict → 清洗后的 dict；None / 空串 → ""；其它 / 不合法 → ValueError。"""
    if value is None or value == "":
        return ""
    cleaned = _clean_paused_reason(value)
    if cleaned is None:
        raise ValueError(
            "暂停原因不合法：kind 只认 tokens / time / capability，"
            "且要带够最小信息（tokens/time 要有正的 limit，capability 要有 text）"
        )
    return cleaned


def _row_paused_reason(row: Any) -> dict | None:
    """从任务行里读暂停原因：老库没有这列、脏 JSON、脏结构一律当「没有」。"""
    try:
        raw = row["paused_reason"] if "paused_reason" in row.keys() else None
    except Exception:
        try:
            raw = row["paused_reason"]
        except Exception:
            raw = None
    return _clean_paused_reason(_load_json(raw, None))


def paused_reason_text(reason: dict | None) -> str:
    """给网页（列表 / 详情、管理员 / 群友）看的一句安全中文。"""
    if not isinstance(reason, dict):
        return ""
    kind = str(reason.get("kind") or "")
    if kind == "capability":
        return _safe_paused_text(reason.get("text") or "")
    if kind == "tokens":
        return (
            f"自动暂停：用量到上限了（用了约 {int(reason.get('used') or 0)} token，"
            f"上限 {int(reason.get('limit') or 0)}），先停下等你决定"
        )
    if kind == "time":
        return (
            f"自动暂停：做得太久了（已跑 {int(reason.get('used') or 0)} 秒，"
            f"上限 {int(reason.get('limit') or 0)} 秒），先停下等你决定"
        )
    return ""


class Tasks:
    def __init__(self, store: Store, get_settings: Callable[[], Any], tools: Any = None) -> None:
        self._store = store
        self._get_settings = get_settings
        self._tools = tools

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> dict | None:
        row = self._store.read().execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    # ------------------------------------------------------------------
    # create / revise
    # ------------------------------------------------------------------

    def create(
        self,
        group_id: str,
        *,
        title: str,
        req: str,
        criteria: Iterable[str],
        source: str,
        requester_id: str = "",
        requester_name: str = "",
        request_id: str | None = None,
        goal_id: str | None = None,
        icon: str = "package",
        status: str = "queued",
        env: str = "",
        delivery_kind: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Create a task; an optional caller-owned connection joins its transaction."""
        gid = str(group_id)
        now = clock.now()
        criteria_l = [str(c) for c in criteria]
        workspace = self._workspace_for(gid)
        with (self._store.tx() if conn is None else nullcontext(conn)) as conn:
            tid = next_id(conn, "T")
            conn.execute(
                "INSERT INTO tasks (id, group_id, workspace, source, request_id, goal_id,"
                " requester_id, requester_name, icon, title, req, req_version, criteria,"
                " status, env, delivery_kind, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    tid, gid, workspace, str(source or ""), request_id, goal_id,
                    str(requester_id or ""), str(requester_name or ""), str(icon or "package"),
                    str(title), str(req or ""), json.dumps(criteria_l, ensure_ascii=False),
                    str(status), str(env or ""), str(delivery_kind or ""), now, now,
                ),
            )
            conn.execute(
                "INSERT INTO task_versions (task_id, version, req, criteria, ts) VALUES (?, 1, ?, ?, ?)",
                (tid, str(req or ""), json.dumps(criteria_l, ensure_ascii=False), now),
            )
            self._store.event(
                conn, f"task.{status}", group_id=gid, entity="task", entity_id=tid,
                payload={"title": str(title), "source": str(source or "")},
            )
        return tid

    def revise(self, task_id: str, *, req: str, criteria: Iterable[str]) -> int:
        """存一个新需求版本；任务在 running / reviewing 时排回 queued（旧尝试作废）。"""
        now = clock.now()
        criteria_l = [str(c) for c in criteria]
        with self._store.tx() as conn:
            row = conn.execute("SELECT status, req_version, group_id FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            v = int(row["req_version"]) + 1
            conn.execute(
                "UPDATE tasks SET req=?, criteria=?, req_version=?, updated=? WHERE id=?",
                (str(req or ""), json.dumps(criteria_l, ensure_ascii=False), v, now, str(task_id)),
            )
            conn.execute(
                "INSERT INTO task_versions (task_id, version, req, criteria, ts) VALUES (?, ?, ?, ?, ?)",
                (str(task_id), v, str(req or ""), json.dumps(criteria_l, ensure_ascii=False), now),
            )
            # 需求改版：干活 lane 的前情可能已经不对，全部关掉重开（docs/20 §6.1）
            close_task_lanes(conn, str(task_id), lanes="workers", now=now)
            if str(row["status"]) in ("running", "reviewing"):
                conn.execute("UPDATE tasks SET status='queued', updated=? WHERE id=?", (now, str(task_id)))
                self._store.event(
                    conn, "task.queued", group_id=str(row["group_id"]), entity="task", entity_id=str(task_id),
                    payload={"reason": "需求改版，重新排队", "version": v},
                )
        return v

    # ------------------------------------------------------------------
    # transition
    # ------------------------------------------------------------------

    def transition(self, task_id: str, to: str, *, reason: str = "", **fields: Any) -> dict:
        to_s = str(to)
        unknown = set(fields) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"不能更新的字段: {sorted(unknown)}")
        if to_s not in _LEGAL:
            raise ValueError(f"不认识的任务状态「{to_s}」")
        if "paused_reason" in fields:
            # 严格校验 + 清洗（限长 / 只留最小字段）：脏原因当场报错，绝不写进库给人看
            fields["paused_reason"] = _paused_reason_for_store(fields["paused_reason"])
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            src = str(row["status"])
            if to_s not in _LEGAL.get(src, frozenset()):
                raise ValueError(f"任务 {task_id} 不能从「{src}」改成「{to_s}」")

            updates: dict[str, Any] = {"status": to_s, "updated": now}
            if to_s == "running" and row["started_ts"] is None:
                updates["started_ts"] = now
            if to_s in _TERMINAL:
                updates["finished_ts"] = now
            for key, value in fields.items():
                if key == "delivery" and isinstance(value, (list, tuple)):
                    updates[key] = json.dumps(list(value), ensure_ascii=False)
                elif key == "undelivered":
                    updates[key] = 1 if value else 0
                elif key == "paused_reason":
                    # 已经过 _paused_reason_for_store 清洗：dict 序列化成 JSON；"" 表示清空
                    if isinstance(value, dict):
                        updates[key] = json.dumps(value, ensure_ascii=False)
                    else:
                        updates[key] = str(value) if value else ""
                else:
                    updates[key] = value

            # 从 paused 出去（继续 / 取消）：暂停原因一并清掉，不留过期的「为什么停」
            #（继续那条在下面的恢复分支里本来就会清，这里补上取消等其它出口）
            if src == "paused" and to_s != "paused":
                updates["paused_reason"] = ""

            # 从 paused/shelved 恢复（→ queued）：清掉暂停原因 + 记安全网基线
            # （tokens 基线 = 当前 usage 总量；时长基线 = resume 时刻）。本轮继续后，
            # 安全网从这条基线重新算——这就是「继续后重新计时 / 重新起算」的口径。
            if to_s == "queued" and src in ("paused", "shelved"):
                updates["paused_reason"] = ""
                try:
                    usage_row = conn.execute(
                        "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t FROM usage WHERE task_id=?",
                        (str(task_id),),
                    ).fetchone()
                    used_so_far = int(usage_row["t"]) if usage_row is not None else 0
                except Exception:
                    used_so_far = 0
                conn.execute(
                    "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                    (
                        f"task.net_base.{task_id}",
                        json.dumps({"tokens": used_so_far, "resume_ts": now}, ensure_ascii=False),
                        now,
                    ),
                )

            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", [*updates.values(), str(task_id)])
            if to_s in _TERMINAL or to_s == "failed":
                # 任务双岗协作（docs/20 §6.1）：lane 前情只在任务内部有效，到终态同一事务清掉
                close_task_lanes(conn, str(task_id), now=now)
            if to_s == "cancelled":
                # 取消后没人再收这一轮（协程被直接停掉）：还开着的尝试同一事务里作废，
                # 不留「任务取消了、尝试还在跑」的残留（外部审查 2026-10-02，线上 T-3）
                conn.execute(
                    "UPDATE attempts SET status='stale', finished=COALESCE(finished, ?)"
                    " WHERE task_id=? AND status IN ('running', 'waiting')",
                    (now, str(task_id)),
                )
            payload: dict[str, Any] = {}
            if reason:
                payload["reason"] = str(reason)
            if fields:
                payload["fields"] = {
                    k: (json.loads(v) if k == "delivery" else v)
                    for k, v in updates.items() if k in fields
                }
            self._store.event(
                conn, f"task.{to_s}", group_id=str(row["group_id"]), entity="task", entity_id=str(task_id),
                payload=payload or None,
            )
        return self.get(task_id)  # type: ignore[return-value]

    def interrupt_orphaned(self, *, reason: str = "插件重启中断，保留已有产物；核对后手动继续") -> int:
        """停机或冷启动时，把失去执行协程的任务暂停，不盲目重跑有副作用的操作。

        状态、正在执行的尝试和时间线在同一事务里修改；queued/waiting_input 不受影响。
        这时不可能有协程还在跑，所以别的任务上残留的 running 尝试（修复前取消留下的）也一并作废。
        """
        now = clock.now()
        with self._store.tx() as conn:
            rows = conn.execute(
                "SELECT id, group_id FROM tasks WHERE status IN ('running', 'reviewing')"
            ).fetchall()
            for row in rows:
                tid = str(row["id"])
                conn.execute(
                    "UPDATE tasks SET status='paused', updated=? WHERE id=? AND status IN ('running', 'reviewing')",
                    (now, tid),
                )
                conn.execute(
                    "UPDATE attempts SET status='stale', finished=COALESCE(finished, ?)"
                    " WHERE task_id=? AND status='running'",
                    (now, tid),
                )
                self._store.event(
                    conn, "task.paused", group_id=str(row["group_id"]),
                    entity="task", entity_id=tid, payload={"reason": str(reason)},
                )
            conn.execute(
                "UPDATE attempts SET status='stale', finished=COALESCE(finished, ?) WHERE status='running'",
                (now,),
            )
        return len(rows)

    def set_env(self, task_id: str, env_text: str, *, note: str = "") -> None:
        """把任务在哪做的（本机 / 一次性机器）写进 env 字段；事件的 payload 同时进网页时间线。

        - env_text 是给人看的一句（如「本机 · maiwork 用户 · 内存上限 512M」或
          「railway.new 一次性机器 · 2 核 2G · 到期 14:32」）；
        - note 非空时（如「一次性机器拿不到，改在本机做：原因」）追加进 env 字段，
          并以 task.env 事件落一条，管理员在任务详情的时间线里能看到。
        """
        text = str(env_text or "").strip()
        note_s = str(note or "").strip()
        if note_s and note_s not in text:
            text = f"{text}（{note_s}）" if text else note_s
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute("SELECT group_id FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            conn.execute("UPDATE tasks SET env=?, updated=? WHERE id=?", (text, now, str(task_id)))
            payload: dict[str, Any] = {"env": text}
            if note_s:
                payload["note"] = note_s
            self._store.event(
                conn, "task.env", group_id=str(row["group_id"]), entity="task",
                entity_id=str(task_id), payload=payload,
            )

    # ------------------------------------------------------------------
    # attempts
    # ------------------------------------------------------------------

    def start_attempt(self, task_id: str) -> int:
        """开一次新尝试：n = 当前尝试数 + 1，钉住当前 req_version；tasks.attempts 同步 +1。返回 n。"""
        now = clock.now()
        with self._store.tx() as conn:
            row = conn.execute("SELECT req_version, attempts FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {task_id}")
            n = int(row["attempts"]) + 1
            conn.execute(
                "INSERT INTO attempts (task_id, n, req_version, status, started) VALUES (?, ?, ?, 'running', ?)",
                (str(task_id), n, int(row["req_version"]), now),
            )
            conn.execute("UPDATE tasks SET attempts=?, updated=? WHERE id=?", (n, now, str(task_id)))
        return n

    def current_attempt_id(self, task_id: str) -> int | None:
        """当前那次尝试（n 最大）的行 id；还没有尝试则 None。"""
        row = self._store.read().execute(
            "SELECT id FROM attempts WHERE task_id=? ORDER BY n DESC LIMIT 1", (str(task_id),)
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        summary: str = "",
        evidence: Iterable[str] = (),
        artifacts: Iterable[str] = (),
        review: str = "",
    ) -> None:
        """回写一次尝试的结果。只更新尝试本身；愿不愿接收走 accept_result。"""
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE attempts SET status=?, summary=?, evidence=?, artifacts=?, review=?, finished=?"
                " WHERE id=?",
                (
                    str(status), str(summary or ""),
                    json.dumps([str(x) for x in evidence], ensure_ascii=False),
                    json.dumps([str(x) for x in artifacts], ensure_ascii=False),
                    str(review or ""), clock.now(), int(attempt_id),
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"尝试不存在: {attempt_id}")

    def accept_result(self, task_id: str, attempt_id: int, req_version: int) -> bool:
        """这次交回能不能算数。

        任务已取消 / 失败 / 完成（终态）、被拒、暂停，或者交回对应的 req_version
        和任务当前版本对不上 → False；同时把该 attempt 标 stale
        （历史保留，任务不复活、不再交付）。
        """
        with self._store.tx() as conn:
            row = conn.execute("SELECT status, req_version FROM tasks WHERE id=?", (str(task_id),)).fetchone()
            if row is None:
                return False
            status = str(row["status"])
            current_version = int(row["req_version"])
            if (
                status in _TERMINAL
                or status in ("paused", "failed")
                or int(req_version) != current_version
            ):
                conn.execute(
                    "UPDATE attempts SET status='stale', finished=COALESCE(finished, ?) WHERE id=?",
                    (clock.now(), int(attempt_id)),
                )
                return False
            return True

    # ------------------------------------------------------------------
    # 网页视图（docs/07 §9.3；字段名以前端为准）
    # ------------------------------------------------------------------

    def list_view(self, group_id: str) -> list[dict]:
        """tasks.list：不含 pending_approval（那些走待批列表）。按 updated 倒序，最多 50 条。"""
        rows = self._store.read().execute(
            "SELECT * FROM tasks WHERE group_id=? AND status != 'pending_approval'"
            " ORDER BY updated DESC LIMIT 50",
            (str(group_id),),
        ).fetchall()
        return [self._list_item(r) for r in rows]

    def detail_view(self, task_id: str, *, admin: bool) -> dict:
        """任务详情。群友版不含 env / timeline，只看得到 steps（步数）。

        G3：群友版还不含 tokens / workspace / source / request_id / requester_id——
        这些是实现细节（花了多少 token、工作区名、从哪来的），群友不需要；
        server 层群友分支会再剥一层兜底。
        """
        row = self._store.read().execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        item = self._list_item(row)
        item.update(
            {
                "req": str(row["req"]),
                "criteria": _load_json(row["criteria"], []),
                "review": str(row["review"] or ""),
                "delivery": _load_json(row["delivery"], []),
                "question": (str(row["question"]) if row["question"] is not None else None),
                "delivery_kind": str(row["delivery_kind"] or ""),
                "attempts": int(row["attempts"]),
                "tokens": int(row["tokens"]),
                "workspace": str(row["workspace"]),
                "source": str(row["source"] or ""),
                "request_id": (str(row["request_id"]) if row["request_id"] else None),
                "started_ts": row["started_ts"],
                "finished_ts": row["finished_ts"],
            }
        )
        # docs/22 §3.1：网页「怎样算完成」显示这一版的需求清单（没锁定 / 版本变了 → []）
        item["requirements"] = (
            requirements.load(self._store, task_id, int(row["req_version"])) or []
        )
        calls = self._calls(str(task_id))
        item["steps"] = len(calls)
        if admin:
            item["env"] = str(row["env"] or "")
            item["timeline"] = calls
            # 任务双岗协作（docs/20 §八）：返工 / 换模型 / 重开的大白话记录
            item["lane_notes"] = self._lane_notes(str(task_id))
        else:
            # G3：群友看不到 token 数 / 工作区 / 来源 / 请求 ID / 发起人 QQ
            for key in ("tokens", "workspace", "source", "request_id", "requester_id"):
                item.pop(key, None)
        return item

    def _lane_notes(self, task_id: str) -> list[dict]:
        rows = self._store.read().execute(
            "SELECT ts, kind, payload FROM events WHERE entity='task' AND entity_id=?"
            " AND kind LIKE 'task.lane_%' ORDER BY id LIMIT 50",
            (str(task_id),),
        ).fetchall()
        out = []
        for r in rows:
            data = _load_json(r["payload"], {})
            if not isinstance(data, dict):
                data = {}
            out.append({
                "ts": r["ts"],
                "kind": str(r["kind"]).removeprefix("task.lane_"),
                "note": str(data.get("note") or "")[:300],
            })
        return out

    def running_count(self, group_id: str) -> int:
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE group_id=? AND status='running'",
            (str(group_id),),
        ).fetchone()
        return int(row["c"]) if row is not None else 0

    # ------------------------------------------------------------------
    # 安全网（0.4.0，[tasks] token_limit / run_seconds）
    # ------------------------------------------------------------------

    def net_check(self, task_id: str) -> dict | None:
        """安全网巡检：任务超限（token / 时长）就自动 paused 并写 paused_reason，返回原因。

        口径：
        - tokens：usage 里这个任务的累计（减去继续那时刻的基线 task.net_base 的 tokens）；
        - 时长：现在 − resume_ts（没有基线 = started_ts / created）；
        - 没在 running 的任务不碰；限值 0 = 不限；失败返回 None（不抛）。
        网页 / /mw / admin 把它 queued 回来时 transition 自动清原因、按那时刻重记基线
        （恢复后 token 从恢复那时刻重新累计、时长重新开始跑）。
        """
        try:
            settings = self._get_settings()
            cfg = getattr(settings, "tasks", None)
        except Exception:
            cfg = None
        token_limit = int(getattr(cfg, "token_limit", 0) or 0) if cfg is not None else 0
        run_seconds = int(getattr(cfg, "run_seconds", 0) or 0) if cfg is not None else 0
        if token_limit <= 0 and run_seconds <= 0:
            return None
        tid = str(task_id or "")
        try:
            row = self.get(tid)
        except Exception:
            row = None
        if row is None or str(row.get("status") or "") != "running":
            return None
        now = clock.now()
        try:
            base = self._store.kv_get(f"task.net_base.{tid}")
        except Exception:
            base = None
        base_tokens = int((base or {}).get("tokens") or 0) if isinstance(base, dict) else 0
        base_ts = float((base or {}).get("resume_ts") or 0.0) if isinstance(base, dict) else 0.0
        total_tokens = 0
        try:
            r = self._store.read().execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t FROM usage WHERE task_id=?",
                (tid,),
            ).fetchone()
            total_tokens = int(r["t"]) if r is not None else 0
        except Exception:
            total_tokens = 0
        used_tokens = max(0, total_tokens - base_tokens)
        started = base_ts or float(row.get("started_ts") or row.get("created") or now)
        elapsed = max(0.0, now - started)
        reason: dict | None = None
        if token_limit > 0 and used_tokens >= token_limit:
            reason = {"kind": "tokens", "limit": token_limit, "used": used_tokens}
        elif run_seconds > 0 and elapsed >= run_seconds:
            reason = {"kind": "time", "limit": run_seconds, "used": int(elapsed)}
        if reason is None:
            return None
        note = (
            f"安全网：token 超线（{used_tokens} / {token_limit}），自动暂停"
            if reason["kind"] == "tokens"
            else f"安全网：跑满 {run_seconds} 秒，自动暂停"
        )
        try:
            self.transition(tid, "paused", reason=note, paused_reason=reason)
        except (KeyError, ValueError):
            pass
        except Exception:
            return None
        return reason

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _workspace_for(self, gid: str) -> str:
        try:
            settings = self._get_settings()
            fn = getattr(settings, "workspace_of", None)
            if callable(fn):
                return str(fn(gid))
        except Exception:
            pass
        return f"g{gid}"

    def _calls(self, task_id: str) -> list[dict]:
        if self._tools is None:
            return []
        try:
            calls = self._tools.recent_calls(task_id=task_id)
        except Exception:
            return []
        return [
            {
                "ts": float(c["ts"]),
                "actor": str(c["actor"]),
                "tool": str(c["tool"]),
                "input": str(c["input"]),
                "output": str(c["output"]),
                "ms": int(c["ms"]),
                "ok": bool(c["ok"]),
            }
            for c in calls
        ]

    @staticmethod
    def _meta(row: sqlite3.Row) -> str:
        """§9.3 的 meta：服务端拼好的一句概况。"""
        status = str(row["status"])
        now = clock.now()
        attempts = int(row["attempts"] or 0)
        if status == "running":
            mins = max(0, int((now - float(row["started_ts"] or now)) / 60))
            if mins < 1:
                return f"刚开始 · {attempts} 次尝试"
            return f"已跑 {mins} 分钟 · {attempts} 次尝试"
        if status == "waiting_input":
            question = str(row["question"] or "")[:30]
            qts = float(row["question_ts"] or now)
            hours = max(0, int((now - qts) / 3600))
            return f"等回答：{question} · 已问 {hours} 小时"
        if status == "shelved":
            return "已搁置，有人回复那条提问就恢复"
        if status == "reviewing":
            return "验收中"
        if status == "queued":
            return "排队中"
        if status == "paused":
            # 自动暂停（安全网 / 开工前能力闸）：原因进 meta，网页直接显示；
            # 手动暂停没有原因，只说「已暂停」，不编造时长 / 用量
            pr = _row_paused_reason(row)
            return _PAUSED_META.get(str((pr or {}).get("kind") or ""), "") or "已暂停"
        if status == "cancelled":
            return "已取消"
        if status == "rejected":
            return "未批准"
        if status == "failed":
            reason = str(row["review"] or "")[:40]
            return f"失败：{reason}" if reason else "失败"
        if status == "completed":
            finished = row["finished_ts"]
            when = clock.bj(float(finished)).strftime("%m-%d %H:%M") if finished else ""
            base = f"完成于 {when}"
            if int(row["undelivered"] or 0):
                return f"{base} · 做完了但还没发出去"
            return base
        return ""

    def _list_item(self, row: sqlite3.Row) -> dict:
        # 自动暂停原因（0.4.0 安全网 / 2026-10 能力闸）：老库没这列、脏数据一律当没有；
        # 手动暂停 / 正常运行都是 None。三种身份（总管理员 / 群管理员 / 群友）共用这条，
        # 所以这里只给最小结构 + 一句安全中文 text（能力闸那句在写入时已洗过，读时再洗一遍）。
        paused_reason = None
        pr = _row_paused_reason(row)
        if pr is not None:
            paused_reason = dict(pr)
            text = paused_reason_text(pr)
            if text:
                paused_reason["text"] = text
        # 发起人只给显示名：按 requester_id 查名册当前名，查不到回落 requester_name 快照。
        # 绝不把 requester_id（QQ 号）放进列表（群友视图也用 list_view）。
        requester_name = members.name_of(
            self._store, row["group_id"], row["requester_id"], fallback=row["requester_name"]
        )
        return {
            "id": str(row["id"]),
            "icon": str(row["icon"] or "package"),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "meta": self._meta(row),
            "goal_id": (str(row["goal_id"]) if row["goal_id"] else None),
            "updated_ts": float(row["updated"]),
            "undelivered": bool(row["undelivered"]),
            "paused_reason": paused_reason,
            "requester_name": requester_name,
        }
