"""开话题适配器（topic_log）：一轮开话题 = 一件活，归 topic 栏。

- active：没有（一轮是一次性的）。
- finished：ts 在窗口内；verdict good → ok=True / bad → ok=False /
  没有 verdict → None；result JSON 里 rejected → 「开话题没开成」。
- trace：库号 47 起 topic_log 带 run_ref → exact，并且同轮 Jev 判断
  （judgments.run_ref 相同）并进一条 think（只放结论摘要，不放群友原话）；
  老行 → 同群 + 窗口（ts 前 2 分钟到后 10 分钟）best-effort，如实报 time_window。
"""

from __future__ import annotations

from typing import Any

from ..context import Ctx, _clip, _f
from ..trace import STEP_TEXT_MAX, Trace

SOURCE = "topic"


def _title_of(r: Any) -> tuple[str, str]:
    """(title, status_text)；result.rejected 非空 → 「没开成」。"""
    result = str(r["result"] or "")
    rejected = ""
    try:
        import json as _j
        rej = _j.loads(result) if result else None
        rejected = str(rej.get("rejected") or "") if isinstance(rej, dict) else ""
    except Exception:
        pass
    if rejected:
        return (f"开话题没开成：{rejected[:40]}", "没开成")
    return ("在群里开了个话题", "开了个话题")


def _row_to_item(r: Any, now: float) -> dict[str, Any]:
    ts = _f(r["ts"])
    verdict = str(r["verdict"] or "")
    ok = True if verdict == "good" else (False if verdict == "bad" else None)
    title, status_text = _title_of(r)
    item = {
        "source": SOURCE, "id": int(r["id"]), "lane": "topic",
        "group_id": str(r["group_id"] or ""),
        "title": title,
        "status": verdict or "opened", "status_text": status_text,
        "phase": "",
        "started": ts, "finished": ts, "updated": ts,
        "ok": ok, "stuck": False, "task_id": "",
        "_run_ref": str(r["run_ref"] or ""),
    }
    item["trace"] = _trace_of(item, now)
    return item


def _trace_of(item: dict[str, Any], now: float) -> Trace:
    run_ref = str(item.get("_run_ref") or "")
    ts = item["started"]
    if run_ref:
        return Trace(kind="run_ref", value=run_ref, group_id=item["group_id"],
                     start=ts - 120, end=ts + 600)
    return Trace(kind="window", group_id=item["group_id"],
                 start=ts - 120, end=ts + 600)


def active(ctx: Ctx) -> list[dict[str, Any]]:
    return []


def finished(ctx: Ctx, since: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in ctx.fetch(
        "SELECT id, group_id, ts, verdict, result, run_ref FROM topic_log"
        " WHERE ts >= ? ORDER BY ts DESC LIMIT 4000",
        (since,),
    ):
        if str(r["group_id"] or "") not in ctx.served:
            continue
        out.append(_row_to_item(r, ctx.now))
    return out


def _topic_judgment_text(jrow: Any, item: dict[str, Any], topic_jev_json: str) -> str:
    """同轮 Jev 判断折成一句「结果摘要」给 steps 的 think（不放群友原话）。

    topic_log.jev 里本来就有结论（ok / reason 中文 / ok_p / fit），直接折它最准；
    拿不到再回退到条目开没开。
    """
    error = str(jrow["error"] or "") if jrow is not None else ""
    if error:
        return f"Jev 判断失败：{_clip(error, 120)}"
    try:
        import json as _json
        jev = _json.loads(str(topic_jev_json or "{}"))
    except Exception:
        jev = {}
    if isinstance(jev, dict) and jev:
        ok_p = jev.get("ok_p")
        reason_zh = str(jev.get("reason") or "")
        fit_t = str(jev.get("fit_title") or "")
        opened = bool(jev.get("ok"))
        stuck = str(jev.get("stuck") or "")
        score = f"ok={float(ok_p):.2f}" if ok_p is not None else ""
        if opened:
            extra = f"，候选「{fit_t}」对得上" if fit_t else ""
            return f"Jev 判断：可以开{('（' + score + '）') if score else ''}，{reason_zh or '时机合适'}{extra}"
        if stuck == "timing":
            return f"Jev 判断：这轮不适合开（时机不合适{('，' + score) if score else ''}）"
        if stuck == "candidate":
            return f"Jev 判断：时机可以但没有够贴的候选{('（' + score + '）') if score else ''}"
        return f"Jev 判断：这轮不开{('，' + reason_zh) if reason_zh else ''}"
    opened = bool(item.get("ok") is True) or str(item.get("status") or "") in ("good", "opened")
    return "Jev 判断：可以开话题" if opened else "Jev 判断：这轮不适合开"


def get(ctx: Ctx, item_id: Any) -> dict[str, Any] | None:
    try:
        tid = int(item_id)
    except (TypeError, ValueError):
        return None
    r = ctx.fetch_one(
        "SELECT id, group_id, ts, verdict, result, opener, jev, run_ref FROM topic_log WHERE id=?",
        (tid,),
    )
    if r is None:
        return None
    if str(r["group_id"] or "") not in ctx.served:
        return None
    item = _row_to_item(r, ctx.now)
    # 同轮 Jev 判断并进一条 think（只放结论摘要，不放群友原话 / state_summary）
    run_ref = str(r["run_ref"] or "")
    jev_json = str(r["jev"] or "")
    extra: list[dict[str, Any]] = []
    if run_ref:
        try:
            jrows = ctx.fetch(
                "SELECT id, ts, ok, error, answers, verdict FROM judgments"
                " WHERE run_ref=? ORDER BY ts ASC, id ASC",
                (run_ref,),
            )
        except Exception:
            jrows = []
        for jrow in jrows or []:
            extra.append({
                "ts": _f(jrow["ts"]), "type": "think",
                "actor": "Jev", "model": "",
                "tool": "", "input": "", "output": "",
                "text": _topic_judgment_text(jrow, item, jev_json),
                "reasoning": "", "reasoning_redacted": False,
                "ms": 0,
                "ok": bool(jrow["ok"]),
                "error": str(jrow["error"] or ""),
            })
    item["_extra_think"] = extra
    return item
