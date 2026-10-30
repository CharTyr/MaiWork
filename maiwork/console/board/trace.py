"""Trace：「这一步属于哪一件」只在这一处决定。

`Trace(kind, value, group_id, start, end)` 是三把钥匙的统一体：

- kind="task_id"：任务 T-*、专岗交接单、目标检查（精确）；
- kind="run_ref"：资讯轮 / 构想轮 / 开话题 / 主模型独立判断（精确，库号 46/47 起）；
- kind="window"：上面几类的**老记录**（没标记）→ 同群 + 时间窗，网页提示 best-effort。

`link`（exact / time_window）也由钥匙决定：task_id / run_ref 有非空 value
→ exact；window → time_window。适配器只负责给钥匙，查库全在这里。

另两个批量活也在这里（列表阶段要，一件一件查太贵）：
- `batch_counts(traces)` → {key: {tools, thinks}}
- `batch_last_step(traces)` → {key: {ts, text}}
key 是 `trace.key`（"task_id:T-1" / "run_ref:news:g:.." / "window:..."），
window 的 Trace 不参与批量（单件 get 时才取窗口）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import Ctx, _clip, _f

LANE_DETAIL_STEPS_MAX = 500  # 详情最多回多少步
STEP_TEXT_MAX = 4000         # 详情单条 step 的 input/output/text 截断上限


@dataclass(frozen=True)
class Trace:
    """去哪找这件活的过程。kind: "task_id" | "run_ref" | "window"。

    window 用 group_id + (start, end)；前两种 value 是 task_id / run_ref
    的字符串值，start/end 只用来限窗口外的步骤（老行 best-effort 不设置，
    精确钥匙下 ts 过滤用 start/end 收紧）。
    """

    kind: str
    value: str = ""
    group_id: str = ""
    start: float = 0.0
    end: float = 0.0            # 0 = 还开着（到 now）
    windowed: bool = True       # task_id 才用：False = 不按 ts 过滤（handoff 的宽窗）

    @property
    def link(self) -> str:
        """网页看的「这步是怎么挂上的」：exact / time_window，只由钥匙决定——
        window（同群+时间窗查）→ time_window，task_id / run_ref → exact。"""
        return "time_window" if self.kind == "window" else "exact"

    @property
    def key(self) -> str:
        """批量计数的键。"""
        return f"{self.kind}:{self.value or self.group_id}"


def _tool_step(r: Any, clip_n: int = STEP_TEXT_MAX) -> dict[str, Any]:
    return {
        "ts": _f(r["ts"]), "type": "tool",
        "actor": str(r["actor"] or ""), "model": "",
        "tool": str(r["tool"] or ""),
        "input": _clip(str(r["input"] or ""), clip_n),
        "output": _clip(str(r["output"] or ""), clip_n),
        "text": "", "reasoning": "", "reasoning_redacted": False,
        "ms": int(r["ms"] or 0),
        "ok": bool(r["ok"]),
        "error": str(r["error"] or ""),
    }


def _think_step(r: Any, clip_n: int = STEP_TEXT_MAX) -> dict[str, Any]:
    text = ""
    reasoning = ""
    reasoning_redacted = False
    try:
        import json as _json
        data = _json.loads(r["response"] or "{}")
        if isinstance(data, dict):
            text = str(data.get("text") or "")
            reasoning = str(data.get("reasoning") or "")
            reasoning_redacted = bool(data.get("reasoning_redacted"))
    except Exception:
        pass
    return {
        "ts": _f(r["ts"]), "type": "think",
        "actor": str(r["agent"] or ""), "model": str(r["model"] or ""),
        "tool": "", "input": "", "output": "",
        "text": _clip(text, clip_n),
        "reasoning": _clip(reasoning, clip_n),
        "reasoning_redacted": reasoning_redacted,
        "ms": int(r["ms"] or 0),
        "ok": bool(r["ok"]),
        "error": str(r["error"] or ""),
    }


def batch_counts(ctx: Ctx, traces: list[Trace]) -> dict[str, dict[str, int]]:
    """按 Trace 批量数「几步工具 / 几步思考」。task_id / run_ref 才参加。"""
    out: dict[str, dict[str, int]] = {}
    if ctx.conn is None:
        return out
    task_ids = [t.value for t in traces if t.kind == "task_id" and t.value]
    refs = [t.value for t in traces if t.kind == "run_ref" and t.value]
    _counts_into(ctx, out, "task_id", task_ids)
    _counts_into(ctx, out, "run_ref", refs)
    return out


def _counts_into(ctx: Ctx, out: dict[str, dict[str, int]], kind: str, values: list[str]) -> None:
    if not values:
        return
    marks = ",".join("?" * len(values))
    try:
        for r in ctx.conn.execute(
            f"SELECT {kind}, COUNT(*) AS n FROM tool_calls WHERE {kind} IN ({marks}) GROUP BY {kind}",
            tuple(values),
        ).fetchall():
            key = f"{kind}:{r[kind]}"
            out.setdefault(key, {"tools": 0, "thinks": 0})["tools"] = int(r["n"] or 0)
    except Exception:
        pass
    try:
        for r in ctx.conn.execute(
            f"SELECT {kind}, COUNT(*) AS n FROM model_calls WHERE {kind} IN ({marks}) GROUP BY {kind}",
            tuple(values),
        ).fetchall():
            key = f"{kind}:{r[kind]}"
            out.setdefault(key, {"tools": 0, "thinks": 0})["thinks"] = int(r["n"] or 0)
    except Exception:
        pass


def batch_last_step(ctx: Ctx, traces: list[Trace]) -> dict[str, dict[str, Any]]:
    """每个 Trace 的最后一步（tools + thinks 里 ts 最大的）。"""
    out: dict[str, dict[str, Any]] = {}
    if ctx.conn is None:
        return out
    task_ids = [t.value for t in traces if t.kind == "task_id" and t.value]
    refs = [t.value for t in traces if t.kind == "run_ref" and t.value]
    _last_into(ctx, out, "task_id", task_ids)
    _last_into(ctx, out, "run_ref", refs)
    return out


def _last_into(ctx: Ctx, out: dict[str, dict[str, Any]], kind: str, values: list[str]) -> None:
    if not values:
        return
    marks = ",".join("?" * len(values))
    # 每件只要最后一条：SQLite 的 MAX() 聚合会让同一行的其它列取 ts 最大那行的值，
    # 不用把每件的全部行（model_calls.response 一行就几十 KB）拉回来再挑——列表每 10 秒刷一次
    try:
        for r in ctx.conn.execute(
            f"SELECT {kind}, MAX(ts) AS ts, tool, output FROM tool_calls"
            f" WHERE {kind} IN ({marks}) GROUP BY {kind}",
            tuple(values),
        ).fetchall():
            key = f"{kind}:{r[kind]}"
            cur = out.get(key)
            if cur is None or _f(r["ts"]) > cur["ts"]:
                out[key] = {"ts": _f(r["ts"]), "text": _clip(f"{r['tool']}：{r['output']}", 60)}
    except Exception:
        pass
    try:
        for r in ctx.conn.execute(
            f"SELECT {kind}, MAX(ts) AS ts, response FROM model_calls"
            f" WHERE {kind} IN ({marks}) GROUP BY {kind}",
            tuple(values),
        ).fetchall():
            key = f"{kind}:{r[kind]}"
            cur = out.get(key)
            if cur is None or _f(r["ts"]) > cur["ts"]:
                text = ""
                try:
                    import json as _j
                    data = _j.loads(r["response"] or "{}")
                    text = str(data.get("text") or "")
                except Exception:
                    pass
                out[key] = {"ts": _f(r["ts"]), "text": _clip(text or "想了一下", 60)}
    except Exception:
        pass


def steps(ctx: Ctx, trace: Trace, item: dict[str, Any], *, after: float = 0.0,
          extra_think: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """按 Trace 把这件活的全部步骤拉出来。

    extra_think：适配器自己的「想」（topic 同轮 Jev 判断）并进来，和库行
    一起按 ts 升序排；限 LANE_DETAIL_STEPS_MAX 条（头尾保、中间省成占位）。
    """
    tool_rows: list[Any] = []
    mc_rows: list[Any] = []
    start = trace.start
    end = trace.end if trace.end else ctx.now
    try:
        if trace.kind == "task_id" and trace.value:
            if trace.windowed:
                tool_rows = ctx.fetch(
                    "SELECT id, ts, actor, tool, input, output, ms, ok, error FROM tool_calls"
                    " WHERE task_id=? AND ts>=? AND ts<=? ORDER BY ts ASC, id ASC",
                    (trace.value, start, end),
                )
                mc_rows = ctx.fetch(
                    "SELECT id, ts, agent, model, ok, ms, error, response FROM model_calls"
                    " WHERE task_id=? AND ts>=? AND ts<=? ORDER BY ts ASC, id ASC",
                    (trace.value, start, end),
                )
            else:
                # handoff：updated 可能等于 created（立即失败 / 测试）且 reviewed 晚于
                # 执行，窗太紧反而漏；task_id 本来就精确，不按 ts 过滤
                tool_rows = ctx.fetch(
                    "SELECT id, ts, actor, tool, input, output, ms, ok, error FROM tool_calls"
                    " WHERE task_id=? ORDER BY ts ASC, id ASC",
                    (trace.value,),
                )
                mc_rows = ctx.fetch(
                    "SELECT id, ts, agent, model, ok, ms, error, response FROM model_calls"
                    " WHERE task_id=? ORDER BY ts ASC, id ASC",
                    (trace.value,),
                )
        elif trace.kind == "run_ref" and trace.value:
            # run_ref 历来精确：挂上 ref 的行就是这一轮的，不按 ts 过滤
            tool_rows = ctx.fetch(
                "SELECT id, ts, actor, tool, input, output, ms, ok, error FROM tool_calls"
                " WHERE run_ref=? ORDER BY ts ASC, id ASC",
                (trace.value,),
            )
            mc_rows = ctx.fetch(
                "SELECT id, ts, agent, model, ok, ms, error, response FROM model_calls"
                " WHERE run_ref=? ORDER BY ts ASC, id ASC",
                (trace.value,),
            )
        else:
            tool_rows = ctx.fetch(
                "SELECT id, ts, actor, tool, input, output, ms, ok, error FROM tool_calls"
                " WHERE group_id=? AND ts>=? AND ts<=? ORDER BY ts ASC, id ASC",
                (trace.group_id, start, end),
            )
            mc_rows = ctx.fetch(
                "SELECT id, ts, agent, model, ok, ms, error, response FROM model_calls"
                " WHERE group_id=? AND ts>=? AND ts<=? ORDER BY ts ASC, id ASC",
                (trace.group_id, start, end),
            )
    except Exception:
        tool_rows, mc_rows = [], []

    steps_list: list[dict[str, Any]] = []
    for r in tool_rows:
        ts = _f(r["ts"])
        if ts <= after:
            continue
        steps_list.append(_tool_step(r))
    for r in mc_rows:
        ts = _f(r["ts"])
        if ts <= after:
            continue
        steps_list.append(_think_step(r))
    for extra in extra_think or []:
        if _f(extra.get("ts")) > after:
            steps_list.append(extra)

    # status 节点：条目自己的状态收尾（started 一步 + finished 一步，终态才有）
    if _f(item.get("started")) > after:
        steps_list.append({
            "ts": _f(item.get("started")), "type": "status",
            "actor": "", "model": "", "tool": "",
            "input": "", "output": "",
            "reasoning": "", "reasoning_redacted": False,
            "text": f"开始：{item.get('status_text')}",
            "ms": 0, "ok": None, "error": "",
        })
    if item.get("finished") is not None and _f(item.get("finished")) > after:
        steps_list.append({
            "ts": _f(item.get("finished")), "type": "status",
            "actor": "", "model": "", "tool": "",
            "input": "", "output": "",
            "reasoning": "", "reasoning_redacted": False,
            "text": f"结束：{item.get('status_text')}",
            "ms": 0, "ok": item.get("ok"), "error": "",
        })

    steps_list.sort(key=lambda s: (_f(s["ts"]), 0 if s["type"] == "status" else 1 if s["type"] == "think" else 2))
    if len(steps_list) > LANE_DETAIL_STEPS_MAX:
        head = steps_list[: LANE_DETAIL_STEPS_MAX // 2]
        tail = steps_list[-(LANE_DETAIL_STEPS_MAX - len(head)):]
        steps_list = head + [{
            "ts": tail[0]["ts"] if tail else ctx.now, "type": "status",
            "actor": "", "model": "", "tool": "", "input": "", "output": "",
            "reasoning": "", "reasoning_redacted": False,
            "text": f"……中间省略 {len(steps_list) - len(head) - len(tail)} 步……",
            "ms": 0, "ok": None, "error": "",
        }] + tail
    return {"link": trace.link, "steps": steps_list}
