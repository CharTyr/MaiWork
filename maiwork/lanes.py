"""任务双岗协作的 lane 存储（docs/20 §六；原始历史归档见 docs/27 §7/§8 P0）。

一个任务挂着几条 lane：`lead`（领队 = 主模型）和 `worker:<n>`（第 n 条活的干活
子 agent）。每条 lane 有两份东西：

- **工作视图**（`task_lanes.messages`）：压缩之后的对话（OpenAI messages，不含 system），
  被打回后同一条 lane 带着前情接着改；
- **原始历史**（`task_lane_raw.messages`）：这一轮压缩之前说过的原话，**追加式**单独存档。
  压缩只改工作视图，永远不动原始历史；摘要失败时工作视图原样保留（不退回旧提要、不清空）。
  原始历史由 `load_raw` 读回，供恢复 / 重读 / 排查用（不是模型可见工具）。

边界（docs/20 §6.1 / §七）：
- 只在**同一个任务内部**记前情：任务到终态 / 需求改版时 `Tasks.transition` / `revise`
  在同一事务里调 `close_task_lanes` 清空（工作视图 + 原始历史一起清）；不跨任务、不跨群。
- 保存是有条件的：任务已到终态 / 需求版本不对 / 群号不对 → 不写（取消、改版之后
  晚到的结果不复活对话）。`expect_rev` 再钉一道：读的时候看到的原始历史版本被别的
  写入者动过 → 这一版整体不写（晚到 / 并发的一方不许覆盖新的）。
- system 不存：规矩、本群做法、工具说明每轮现算，权限变了当轮生效。
- 续用前 `prepare_history` 把本轮不给的工具的调用记录改写成文字（不留工具调用格式，
  模型不会以为还能用；真调用时 Tools.call 的硬门照拦）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from . import clock
from .compaction import SUMMARY_MARKER

logger = logging.getLogger("maiwork.lanes")

# 不能再保存 lane 的任务状态（终态 + failed：failed 只能由人重开，重开就从零来）
_CLOSED_TASK_STATUSES = ("completed", "cancelled", "rejected", "failed")

_DANGLING_REPLY = "（这一步之后已经把结果交回给领队，等验收；领队的反馈在下面）"

# 工作视图里那条「前面对话的摘要」：它是**视图**不是原始历史，归档时不算 raw 内容
# （压缩只改视图，不改原始历史）。标记以 compaction 为准，不自己再写一个字面量。
SUMMARY_PREFIX = SUMMARY_MARKER


# 带 lane 的活（任务双岗协作，docs/20 §四 / 第三步）追加进子 agent 的 system：
# 可以对说明本身提异议，由领队决定；不许自己改需求。
LANE_WORKER_RULES = (
    "你在这个任务里有一条自己的对话：上面的前情是你之前在这个任务里做过的，接着干，别从头再来。"
    "觉得这次的说明本身有问题（做不到、前后矛盾、有明显更好的做法）：先把能做的部分做好，"
    "再在 submit_result 的 challenge 里写清理由、依据和建议，由领队决定；不要自己改需求，"
    "也不要因为有异议就什么都不交。"
)


def close_task_lanes(conn: Any, task_id: str, *, lanes: str = "all", now: float | None = None) -> None:
    """同一事务里把任务的 lane 清空并标 closed（Tasks.transition / revise 调）。

    lanes="all" 全部；"workers" 只关干活 lane（需求改版：旧活可能已经不对，领队留着）。
    工作视图和原始历史一起清（同一事务）：任务到终态 / 需求改版之后，这个任务的前情
    一份都不留（不跨任务、不跨群），row 和计数留着备查。
    """
    ts = clock.now() if now is None else now
    lane_filter = " AND lane LIKE 'worker:%'" if lanes == "workers" else ""
    for table in ("task_lanes", "task_lane_raw"):
        conn.execute(
            f"UPDATE {table} SET messages='[]', status='closed', updated=? WHERE task_id=?{lane_filter}",
            [ts, str(task_id)],
        )


def _as_messages(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict) and m.get("role")]


def _tool_names(msg: dict) -> list[str]:
    out = []
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            out.append(str((tc.get("function") or {}).get("name") or ""))
    return out


# ---------------------------------------------------------------------------
# 原始历史（task_lane_raw）：追加式合并，只增不减
# ---------------------------------------------------------------------------


def _is_summary(msg: dict) -> bool:
    return str(msg.get("content") or "").startswith(SUMMARY_PREFIX)


def _drop_summaries(messages: Any) -> list[dict]:
    return [m for m in _as_messages(messages) if not _is_summary(m)]


def _same_message(a: dict, b: dict) -> bool:
    """两条消息是不是同一条（整条 JSON 比，含工具调用与结果）。"""
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b, ensure_ascii=False, sort_keys=True
    )


def _tail_after(base: list[dict], add: list[dict]) -> list[dict]:
    """add 里「base 之后」的那一段（**比较时不过滤摘要**，先按原样比）。

    base 是 add 的整份前缀（含 base 为空）→ 只取尾巴；对不上（视图被摘要整段换掉 /
    换了岗 / 重开）→ 整份 add 都当新料。
    """
    if base and len(add) >= len(base) and all(
        _same_message(add[i], base[i]) for i in range(len(base))
    ):
        return list(add[len(base):])
    return list(add)


def raw_merge(
    old_raw: Any,
    baseline: Any,
    incoming: Any,
    original: Any = None,
) -> tuple[list[dict], int]:
    """算出这一轮之后的原始历史（追加式）与追加条数。

    - ``old_raw``：库里已有的原始历史；
    - ``baseline``：**上一份已落库的工作视图**（只认「整份是新的前缀」这一种安全重叠）；
    - ``incoming``：这一轮的工作视图（压缩后的视图也算）；
    - ``original``：调用方手里这一轮的完整原始对话（压缩把工作视图整段换掉时给）。
      给了它就**以它为准算一次增量**（压缩前那份原样归档），``incoming`` 不再额外追加
      同一批数据——压缩后的视图会在下一次保存时当 baseline，那时再追加新长出来的话。

    规则（docs/27 §8 P0）：
    1. 原始历史只增不减、不覆盖：摘要换掉工作视图时 raw 原样保留；
    2. 视图接得上上一版 → 只追加长出来的那一段（重复的原话也照样追加）；
    3. 接不上（换了岗 / 重开 / 视图被改写）→ 整份当新料追加——新内容一条不丢；
    4. 摘要消息不算原始历史：**先按原样比前缀，比出来的增量里再滤掉摘要**（基线里本来就
       带摘要时，这样才不会把同一段近期原文反复追加）
    5. 不按内容去重：一模一样的 user / tool 消息重复出现是正常证据。
    """
    raw = _as_messages(old_raw)
    base = _as_messages(baseline)
    source = _as_messages(original) if original is not None else _as_messages(incoming)
    delta = _drop_summaries(_tail_after(base, source))
    return raw + delta, len(delta)


def close_dangling_tool_calls(messages: Iterable[Any]) -> list[dict]:
    """给没收到回复的工具调用补一条 tool 回复（交回 submit_result 时就是这样悬着的）。

    严格的端点要求每个 assistant.tool_calls 后面都有对应 id 的 tool 消息，否则 400。
    """
    msgs = _as_messages(list(messages or []))
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        out.append(m)
        i += 1
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        want = [str(tc.get("id") or "") for tc in m["tool_calls"] if isinstance(tc, dict)]
        seen: set[str] = set()
        while i < len(msgs) and msgs[i].get("role") == "tool":
            seen.add(str(msgs[i].get("tool_call_id") or ""))
            out.append(msgs[i])
            i += 1
        for tc in m["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or "")
            if cid and cid not in seen:
                out.append({
                    "role": "tool", "tool_call_id": cid,
                    "name": str((tc.get("function") or {}).get("name") or ""),
                    "content": _DANGLING_REPLY,
                })
        del want
    return out


def prepare_history(messages: Iterable[Any], *, allowed: Iterable[str]) -> list[dict]:
    """续用前整理一条 lane 的历史（docs/20 §6.2）。

    - 去掉 system（每轮现算）和坏数据；
    - 补齐悬着的工具调用回复；
    - 一组工具调用里只要有本轮不给的工具，整组改写成一条 assistant 文字
      （「之前用过 X（参数），结果：…；这一轮不能再用」），不留工具调用格式。
    """
    allowed_set = {str(x) for x in (allowed or ())}
    msgs = close_dangling_tool_calls([m for m in _as_messages(list(messages or [])) if m.get("role") != "system"])
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        i += 1
        if m.get("role") == "tool":
            # 前面没有 assistant(tool_calls) 的孤儿回复：严格端点会拒，丢掉
            continue
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            out.append(m)
            continue
        group = [m]
        while i < len(msgs) and msgs[i].get("role") == "tool":
            group.append(msgs[i])
            i += 1
        names = [n for n in _tool_names(m) if n]
        if all(n in allowed_set for n in names):
            out.extend(group)
            continue
        results = {str(t.get("tool_call_id") or ""): t.get("content") for t in group[1:]}
        lines = []
        if str(m.get("content") or "").strip():
            lines.append(str(m["content"]).strip())
        for tc in m["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            res = results.get(str(tc.get("id") or ""), "")
            note = "" if name in allowed_set else "（这一轮不能再用这个工具）"
            # 原样带上参数和结果：旧结果里的「完整输出在 <工作区相对路径>」这类回读指针
            # 是事实，截断会把它悄悄弄丢（装不下交给整包预算闸明确失败，不静默丢）
            lines.append(f"之前用过 {name}{note}，参数：{args}；结果：{res}")
        out.append({"role": "assistant", "content": "\n".join(lines)})
    return out


class TaskLanes:
    """task_lanes 表的读写（Store 之上的一薄层）。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    @staticmethod
    def _row(row: Any) -> dict:
        return {
            "task_id": str(row["task_id"]),
            "lane": str(row["lane"]),
            "group_id": str(row["group_id"]),
            "kind": str(row["kind"] or ""),
            "model": str(row["model"] or ""),
            "req_version": int(row["req_version"] or 1),
            "messages": _as_messages(row["messages"]),
            "snapshot": str(row["snapshot"] or ""),
            "escalated": bool(row["escalated"]),
            "handoff_id": str(row["handoff_id"] or ""),
            "status": str(row["status"] or ""),
            "updated": float(row["updated"] or 0),
        }

    def load(self, task_id: str, lane: str, *, group_id: str) -> dict | None:
        row = self._store._conn.execute(
            "SELECT * FROM task_lanes WHERE task_id=? AND lane=? AND group_id=?",
            (str(task_id), str(lane), str(group_id)),
        ).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _row_raw(row: Any) -> dict:
        return {
            "task_id": str(row["task_id"]),
            "lane": str(row["lane"]),
            "group_id": str(row["group_id"]),
            "kind": str(row["kind"] or ""),
            "req_version": int(row["req_version"] or 1),
            "messages": _as_messages(row["messages"]),
            "rev": int(row["rev"] or 0),
            "covered_count": int(row["covered_count"] or 0),
            "covered_rev": int(row["covered_rev"] or 0),
            "summary": str(row["summary"] or ""),
            "status": str(row["status"] or ""),
            "created": float(row["created"] or 0),
            "updated": float(row["updated"] or 0),
        }

    def list(self, task_id: str, *, group_id: str) -> list[dict]:
        rows = self._store._conn.execute(
            "SELECT * FROM task_lanes WHERE task_id=? AND group_id=? ORDER BY lane",
            (str(task_id), str(group_id)),
        ).fetchall()
        return [self._row(r) for r in rows]

    def save(
        self,
        task_id: str,
        lane: str,
        *,
        group_id: str,
        kind: str,
        messages: list[dict],
        req_version: int,
        model: str | None = None,
        escalated: bool | None = None,
        snapshot: str | None = None,
        handoff_id: str | None = None,
        original_messages: list[dict] | None = None,
        expect_rev: int | None = None,
        summary: str | None = None,
        covered_count: int | None = None,
        covered_rev: int | None = None,
    ) -> bool:
        """有条件保存：任务属于这个群、没到终态、需求版本对得上才写。返回写没写。

        工作视图（``task_lanes.messages``）、原始历史（``task_lane_raw``）和压缩覆盖
        元数据（``summary`` / ``covered_count`` / ``covered_rev``）在同一条事务里写：
        任一道闸不过 → 整体不写（覆盖元数据也不会单独前进）。

        - ``original_messages``：这一轮的完整原始对话（压缩把工作视图整段换掉时给）；
        - ``expect_rev``：读的时候看到的原始历史版本（`rev`）。被别人改过 → 整体不写
          （晚到 / 并发的一方不许覆盖新的那份）。
        """
        now = clock.now()
        msgs = close_dangling_tool_calls([m for m in _as_messages(messages) if m.get("role") != "system"])
        body = json.dumps(msgs, ensure_ascii=False)
        with self._store.tx() as conn:
            t = conn.execute(
                "SELECT status, req_version, group_id FROM tasks WHERE id=?", (str(task_id),)
            ).fetchone()
            if t is None or str(t["group_id"]) != str(group_id):
                return False
            if str(t["status"]) in _CLOSED_TASK_STATUSES:
                return False
            if int(t["req_version"] or 1) != int(req_version):
                return False
            old = conn.execute(
                "SELECT model, escalated, snapshot, handoff_id, messages FROM task_lanes"
                " WHERE task_id=? AND lane=?",
                (str(task_id), str(lane)),
            ).fetchone()
            raw = conn.execute(
                "SELECT * FROM task_lane_raw WHERE task_id=? AND lane=?",
                (str(task_id), str(lane)),
            ).fetchone()
            # raw 行不在（被终态/改版清掉、或被别的写入者删过）时实际版本 = 0：
            # 读的时候看到 5 版、中途被清掉 → 这一版不许把 open 视图复活
            actual_rev = int(raw["rev"] or 0) if raw is not None else 0
            if expect_rev is not None and actual_rev != int(expect_rev):
                return False
            model_v = str(model) if model is not None else (str(old["model"]) if old is not None else "")
            esc_v = (1 if escalated else 0) if escalated is not None else (int(old["escalated"]) if old is not None else 0)
            snap_v = str(snapshot) if snapshot is not None else (str(old["snapshot"]) if old is not None else "")
            hid_v = str(handoff_id) if handoff_id is not None else (str(old["handoff_id"]) if old is not None else "")
            old_raw = raw["messages"] if raw is not None else "[]"
            baseline = old["messages"] if old is not None else "[]"
            merged, appended = raw_merge(old_raw, baseline, msgs, original_messages)
            rev = (int(raw["rev"] or 0) if raw is not None else 0) + (1 if appended else 0)
            sum_v = str(summary) if summary is not None else (str(raw["summary"] or "") if raw is not None else "")
            cov_v = int(covered_count) if covered_count is not None else (
                int(raw["covered_count"] or 0) if raw is not None else 0
            )
            cov_rev = int(covered_rev) if covered_rev is not None else (
                int(raw["covered_rev"] or 0) if raw is not None else 0
            )
            if covered_count is not None:
                cov_v = max(0, min(cov_v, len(merged)))
            conn.execute(
                "INSERT INTO task_lanes (task_id, lane, group_id, kind, model, req_version, messages,"
                " snapshot, escalated, handoff_id, status, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)"
                " ON CONFLICT(task_id, lane) DO UPDATE SET group_id=excluded.group_id, kind=excluded.kind,"
                " model=excluded.model, req_version=excluded.req_version, messages=excluded.messages,"
                " snapshot=excluded.snapshot, escalated=excluded.escalated,"
                " handoff_id=excluded.handoff_id, status='open',"
                " updated=excluded.updated",
                (str(task_id), str(lane), str(group_id), str(kind or ""), model_v, int(req_version),
                 body, snap_v, esc_v, hid_v, now, now),
            )
            conn.execute(
                "INSERT INTO task_lane_raw (task_id, lane, group_id, kind, req_version, messages,"
                " rev, covered_count, covered_rev, summary, status, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)"
                " ON CONFLICT(task_id, lane) DO UPDATE SET group_id=excluded.group_id,"
                " kind=excluded.kind, req_version=excluded.req_version, messages=excluded.messages,"
                " rev=excluded.rev, covered_count=excluded.covered_count,"
                " covered_rev=excluded.covered_rev, summary=excluded.summary, status='open',"
                " updated=excluded.updated",
                (str(task_id), str(lane), str(group_id), str(kind or ""), int(req_version),
                 json.dumps(merged, ensure_ascii=False), rev, cov_v, cov_rev, sum_v, now, now),
            )
        return True

    def load_raw(self, task_id: str, lane: str, *, group_id: str) -> dict | None:
        """读这条 lane 的**原始历史**（恢复 / 重读用；不是模型可见工具）。

        返回 messages = 这一轮轮压缩之前说过的原话（不含 system、不含摘要）；
        rev = 这份原始历史的版本（每次真追加 +1）；covered_count / covered_rev = 工作视图
        那次摘要覆盖到原始历史的第几条 / 当时的版本；summary = 最近一次成功的提要。
        """
        row = self._store._conn.execute(
            "SELECT * FROM task_lane_raw WHERE task_id=? AND lane=? AND group_id=?",
            (str(task_id), str(lane), str(group_id)),
        ).fetchone()
        return self._row_raw(row) if row is not None else None

    def raw_rev(self, task_id: str, lane: str, *, group_id: str) -> int | None:
        """这条 lane 原始历史现在的版本号（没有 → None）。保存时当 ``expect_rev`` 用。"""
        row = self._store._conn.execute(
            "SELECT rev FROM task_lane_raw WHERE task_id=? AND lane=? AND group_id=?",
            (str(task_id), str(lane), str(group_id)),
        ).fetchone()
        return int(row["rev"] or 0) if row is not None else None

    def reset(self, task_id: str, lane: str, *, group_id: str) -> None:
        """这条 lane 从零重开（压缩失败 / 换了岗位）：清对话，计数和提要保留备查。"""
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE task_lanes SET messages='[]', escalated=0, updated=? WHERE task_id=? AND lane=? AND group_id=?",
                (clock.now(), str(task_id), str(lane), str(group_id)),
            )
