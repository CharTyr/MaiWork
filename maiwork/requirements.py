"""requirements.py（docs/22 §3.1–§3.2 第一期）：需求清单 + 逐条判定算结果。

代码管流程、模型干活。这里只有纯函数和 kv 读写，不调模型、不碰宿主、不发消息：

- `normalize_requirements(raw, req_text)`：把主模型给的 `requirements` 列表收拾干净——
  去空白截 60 字、空的丢掉、最多 6 条（不含底线）、`origin` / `kind` 只认固定几个值
  （写错 / 没写一律当**严**的那一档，防止模型把原话要求偷偷标成可选）；
  一条「原话」都没有 → 代码自动补一条「按原话完成：<原话前 50 字>」；最后永远追加底线 R0。
  id 由代码按顺序编 R1..Rn（底线固定 R0），不信模型给的 id。
- `is_blocking(item)`：必须项 = `原话` + `底线`；加分项 = `补充`。
- `criteria_texts(items)`：给网页 / 任务 criteria 用的文本（去掉底线，补充项标「（加分项）」）。
- `judge(items, verdicts)`：**代码算过没过**。一条算「做到」必须 `met is True` 且给了证据
  （去空白后 ≥2 字）；模型漏判某条必须项 = 没做到。**原话**必须项的 `met=true` 但证据里是
  「未覆盖 / 不另扩搜 / 待补充 / 需另开一次调研」这类「没去做」的写法 → 也算没做到
  （`WHY_PLACEHOLDER`，线上 T-11；「查不到」本身不是标记词，写清查过哪里是合法的）；
  底线 R0 和补充项不受这条规则影响。模型自己给的 `pass` 只作参考。
- `human_unmet(judgement, verdicts)`（第三期 §5 A）：没做到的必须项里带 `needs_human`
  （去空白后 ≥ `NEEDS_HUMAN_MIN`=6 字）的那些——「需要真人参与、材料已备好」的出口。
- `with_brief_scale(items)`（docs/26 问题 C）：规模档 brief 时由代码往已锁定的清单里加一条
  「篇幅：几句话回答清楚，不做网页/文件」（`补充`=加分项，不参与通过公式；条数满了就挂在
  底线 R0 文字后面，不占条数）。
- `load` / `save`：清单锁在 kv `task.requirements.<任务ID>`，带需求版本号——同一版需求
  之后的每轮计划都不许改它，只有群友改了需求（版本号变）才重新拆。不加表、不迁库。
"""

from __future__ import annotations

import logging
from typing import Any

from . import clock

logger = logging.getLogger("maiwork.requirements")

MAX_ITEMS = 6          # 最多 6 条（不含底线 R0）
TEXT_MAX = 60          # 每条要求 ≤60 字
REQ_TEXT_MAX = 50      # 自动补的「按原话完成：」取原话前 50 字
EVIDENCE_MIN = 2       # 证据去空白后至少这么长才算给了证据
NEEDS_HUMAN_MIN = 6    # needs_human 去空白后至少这么长才算「写清了要谁做什么」（docs/22 §5 A）
FLOOR_ID = "R0"
FLOOR_TEXT = "内容真实、不编造；交付物能正常打开"
# 规模档 brief（docs/26 问题 C 收口）：群友只是随口一问时，代码要把「别做网页/文件、
# 几句话答清楚」锁进清单，免得下一轮又派制作网页的活。由 `with_brief_scale` 写进去。
BRIEF_SCALE_TEXT = "篇幅：几句话回答清楚，不做网页/文件"
ORIGIN_ORIGINAL = "原话"
ORIGIN_BONUS = "补充"
ORIGIN_FLOOR = "底线"
_ORIGINS = (ORIGIN_ORIGINAL, ORIGIN_BONUS)
KIND_STRICT = "按原话"
_KINDS = ("实做", "文稿", "真人")

WHY_NOT_MET = "判定没做到"
WHY_NO_EVIDENCE = "没给证据"
WHY_NOT_JUDGED = "验收没判这一条"
# 线上 T-11：原话必须项写成「这块没覆盖 / 本次不另扩搜 / 待补充：需另开一次调研」——
# 明说了「没去做」，不得算做到（「查过但查不到、并写清查过哪里」是合法的，不算留空）。
WHY_PLACEHOLDER = "只写了没覆盖/待补，内容没做"
PLACEHOLDER_MARKERS = (
    "未覆盖", "不另扩搜", "不扩搜", "不展开", "此处不写", "待补充",
    "需另开", "另开一次", "未收录", "留待后续", "占位",
)

# 计划覆盖（线上 T-11）：job 用 `covers` 声明自己负责哪几条需求；代码把没被任何 job
# 覆盖到的**原话**必须项补成一行指令，追加进合适的 job brief。
COVERS_HINT_PREFIX = "另外必须为这些要求取材/产出："


def _clean_text(value: Any, limit: int) -> str:
    """折叠空白后截断（模型爱写换行 / 连续空格）。"""
    return " ".join(str(value or "").split())[:limit]


def normalize_requirements(raw: Any, req_text: Any) -> list[dict]:
    """把模型给的需求列表收拾成锁定的清单（纯函数，任何输入都不抛）。"""
    items: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            text = _clean_text(entry.get("text"), TEXT_MAX)
            if not text:
                continue
            origin = str(entry.get("origin") or "").strip()
            if origin not in _ORIGINS:
                # 写错 / 没写 → 当「原话」（宁严勿松）
                origin = ORIGIN_ORIGINAL
            kind = str(entry.get("kind") or "").strip()
            if kind not in _KINDS:
                kind = KIND_STRICT
            items.append({"text": text, "origin": origin, "kind": kind})
            if len(items) >= MAX_ITEMS:
                break
    if not any(i["origin"] == ORIGIN_ORIGINAL for i in items):
        # 模型一条原话都没标（或没给）→ 代码自动补，防「原话要求被整份漏掉」
        fallback = _clean_text(req_text, REQ_TEXT_MAX)
        items.insert(0, {
            "text": "按原话完成：" + fallback,
            "origin": ORIGIN_ORIGINAL,
            "kind": KIND_STRICT,
        })
        items = items[:MAX_ITEMS]
    out: list[dict] = []
    for i, item in enumerate(items, start=1):
        out.append({
            "id": f"R{i}",
            "text": item["text"],
            "origin": item["origin"],
            "kind": item["kind"],
        })
    out.append({
        "id": FLOOR_ID,
        "text": FLOOR_TEXT,
        "origin": ORIGIN_FLOOR,
        "kind": ORIGIN_FLOOR,
    })
    return out


def is_blocking(item: Any) -> bool:
    """必须项 = 原话 + 底线；补充是加分项。"""
    if not isinstance(item, dict):
        return False
    return str(item.get("origin") or "") in (ORIGIN_ORIGINAL, ORIGIN_FLOOR)


def prompt_line(item: Any) -> str:
    """提示词里的一行：`R1【原话·实做】把投票发起来`；补充项标「（加分项）」。"""
    if not isinstance(item, dict):
        return ""
    text = str(item.get("text") or "")
    if str(item.get("origin") or "") == ORIGIN_BONUS:
        text += "（加分项）"
    return f"{item.get('id')}【{item.get('origin')}·{item.get('kind')}】{text}"


def criteria_texts(items: Any) -> list[str]:
    """给网页 / tasks.criteria 的文本：去掉底线，补充项标「（加分项）」。"""
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == FLOOR_ID or str(item.get("origin") or "") == ORIGIN_FLOOR:
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if str(item.get("origin") or "") == ORIGIN_BONUS:
            text += "（加分项）"
        out.append(text)
    return out


def placeholder_marker(text: Any) -> str:
    """证据里有没有「没去做」的标记词（去空白后找）；返回命中的第一个，没有 → ''。

    只认 `PLACEHOLDER_MARKERS`（未覆盖 / 不另扩搜 / 待补充 / 需另开一次调研 / 占位…）。
    **「查不到」「未确认」本身不是标记词**：查过之后如实写清「查过哪些地方、没查到」是
    合法的证据，不许当留空。
    """
    folded = " ".join(str(text or "").split())
    for marker in PLACEHOLDER_MARKERS:
        if marker in folded:
            return marker
    return ""


def judge(items: Any, verdicts: Any) -> dict:
    """代码按规则算过没过（模型的 pass 不参与）。

    返回 {"pass", "unmet_blocking", "unmet_bonus", "met"}；
    `why` ∈ {"判定没做到", "没给证据", "验收没判这一条", "只写了没覆盖/待补，内容没做"}。

    线上 T-11：**原话**必须项 `met=true` 但证据里是「这块没覆盖 / 本次不另扩搜 / 待补充」
    这类「没去做」的写法 → 判没做到。底线 R0 和补充项不受这条规则影响（`is_blocking`
    之外的项不进通过公式；底线只要求「内容真实、能打开」，不查留空措辞）。
    """
    by_id: dict[str, dict] = {}
    if isinstance(verdicts, list):
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            vid = v.get("id")
            if isinstance(vid, bool) or not isinstance(vid, str):
                continue
            key = vid.strip()
            if not key or key in by_id:
                continue
            by_id[key] = v
    met: list[str] = []
    unmet_blocking: list[dict] = []
    unmet_bonus: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("id") or "")
        text = str(item.get("text") or "")
        verdict = by_id.get(iid)
        if verdict is None:
            ok, why = False, WHY_NOT_JUDGED
        elif verdict.get("met") is not True:
            ok, why = False, WHY_NOT_MET
        elif len(str(verdict.get("evidence") or "").strip()) < EVIDENCE_MIN:
            # met=true 但没给证据 = 没做到（只拿一句「做了」不算数）
            ok, why = False, WHY_NO_EVIDENCE
        elif (
            str(item.get("origin") or "") == ORIGIN_ORIGINAL
            and placeholder_marker(verdict.get("evidence"))
        ):
            # 线上 T-11：原话必须项只写「没覆盖 / 不另扩搜 / 待补充」= 内容没做
            ok, why = False, WHY_PLACEHOLDER
        else:
            ok, why = True, ""
        if ok:
            met.append(iid)
        elif is_blocking(item):
            unmet_blocking.append({"id": iid, "text": text, "why": why})
        else:
            unmet_bonus.append({"id": iid, "text": text, "why": why})
    return {
        "pass": not unmet_blocking,
        "unmet_blocking": unmet_blocking,
        "unmet_bonus": unmet_bonus,
        "met": met,
    }


def human_unmet(judgement: Any, verdicts: Any) -> list[dict]:
    """docs/22 §5 A：没做到的必须项里，哪些带 `needs_human`（需要真人参与）且写清了。

    和 `judge` / coordinator 的 `_blocked_items` 同构：只认**没做到的必须项**；`needs_human`
    去空白后至少 `NEEDS_HUMAN_MIN` 个字才算（写清了「要谁做什么、做完怎么告诉我」）。
    返回 `[{"id", "text", "needs_human"}]`。
    """
    by_id: dict[str, dict] = {}
    for v in verdicts if isinstance(verdicts, list) else []:
        if not isinstance(v, dict):
            continue
        key = str(v.get("id") or "").strip()
        if key and key not in by_id:
            by_id[key] = v
    out: list[dict] = []
    for item in (judgement or {}).get("unmet_blocking") or []:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("id") or "")
        verdict = by_id.get(iid) or {}
        need = " ".join(str(verdict.get("needs_human") or "").split())
        if len(need) < NEEDS_HUMAN_MIN:
            continue
        out.append({
            "id": iid,
            "text": str(item.get("text") or ""),
            "needs_human": need[:200],
        })
    return out


def with_brief_scale(items: Any) -> list[dict]:
    """规模档 brief（docs/26 问题 C）：把 `BRIEF_SCALE_TEXT` 写进已锁定的清单（纯函数）。

    只有 `Coordinator._plan` 在 scale=brief 时调用；模型给的条目一个字都不改。

    - 已经有这条 → 原样返回（幂等：多轮计划不会越加越多）；
    - 去掉底线后还有位置（少于 `MAX_ITEMS`）→ 插在底线前面，id 按现有最大编号接着编；
    - 条数已经满了 → **不占条数**，把这句挂到底线 R0 的文字后面（R0 永远要判，
      不会被漏掉；这样不会把模型给的第 6 条原话挤出去、也不破坏「最多 6 条」的上限）。

    这条永远标 `补充`（加分项），不是 `原话`：它不是群友原话里明确说的，是 MaiWork
    自己按「口头一问别升级成全景报告」加的口径——本模块的规矩是「为了做好自己加的
    一律标补充」。所以它进清单、进计划/验收提示，但不参与 `judge` 的通过公式
    （brief 的硬闸在 coordinator：deliver_kind 一定是 text、最多 1 条活）。
    """
    rows = [dict(i) for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if not rows:
        return [{
            "id": FLOOR_ID, "text": FLOOR_TEXT, "origin": ORIGIN_FLOOR, "kind": ORIGIN_FLOOR,
        }]
    if any(BRIEF_SCALE_TEXT in str(i.get("text") or "") for i in rows):
        return rows
    floor_idx = next(
        (n for n, i in enumerate(rows) if str(i.get("id") or "") == FLOOR_ID), len(rows)
    )
    body = [i for i in rows if str(i.get("id") or "") != FLOOR_ID]
    if len(body) < MAX_ITEMS:
        top = 0
        for i in body:
            iid = str(i.get("id") or "")
            if iid.startswith("R") and iid[1:].isdigit():
                top = max(top, int(iid[1:]))
        rows.insert(floor_idx, {
            "id": f"R{top + 1}", "text": BRIEF_SCALE_TEXT,
            "origin": ORIGIN_BONUS, "kind": KIND_STRICT,
        })
    else:
        floor = rows[floor_idx]
        base = str(floor.get("text") or "").strip() or FLOOR_TEXT
        floor["text"] = f"{base}；{BRIEF_SCALE_TEXT}"
    return rows


def covered_ids(jobs: Any) -> set[str]:
    """所有 job 的 `covers` 声明的需求 id（去空白、统一大写，便于对上 R1/r1）。"""
    out: set[str] = set()
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        covers = job.get("covers")
        for raw in covers if isinstance(covers, list) else []:
            key = str(raw or "").strip().upper()
            if key:
                out.add(key)
    return out


def uncovered_required(items: Any, jobs: Any) -> list[dict]:
    """计划里没有任何 job 的 `covers` 覆盖到的**原话**必须项（不含底线 R0、不含补充）。

    纯函数，任何输入都不抛。**所有 job 都没给 covers → 返回 []**（兼容旧模型 / 旧行为：
    没有声明就没有「没覆盖」这回事，不许凭空往 brief 里塞指令）。
    """
    covered = covered_ids(jobs)
    if not covered:
        return []
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("origin") or "") != ORIGIN_ORIGINAL:
            continue
        iid = str(item.get("id") or "").strip()
        if not iid or iid.upper() in covered:
            continue
        out.append({"id": iid, "text": str(item.get("text") or "")})
    return out


def covers_instruction(missing: Any) -> str:
    """没被覆盖的原话必须项 → 一行补充指令（`COVERS_HINT_PREFIX` + `R2 文本；R3 文本`）。"""
    parts: list[str] = []
    for item in missing if isinstance(missing, list) else []:
        if not isinstance(item, dict):
            continue
        line = f"{item.get('id')} {item.get('text')}".strip()
        if line:
            parts.append(line)
    if not parts:
        return ""
    return COVERS_HINT_PREFIX + "；".join(parts)


def kv_key(task_id: Any) -> str:
    return f"task.requirements.{task_id}"


def load(store: Any, task_id: Any, req_version: Any) -> list[dict] | None:
    """读当前版本锁定的清单；没存过 / 版本对不上 / 坏数据 → None（= 还没锁）。"""
    try:
        saved = store.kv_get(kv_key(task_id))
    except Exception:
        logger.exception("读需求清单失败（任务 %s）", task_id)
        return None
    if not isinstance(saved, dict):
        return None
    try:
        if int(saved.get("req_version")) != int(req_version):
            return None
    except (TypeError, ValueError):
        return None
    items = saved.get("items")
    if not isinstance(items, list) or not items:
        return None
    got = [i for i in items if isinstance(i, dict)]
    return got or None  # 整份都是坏数据 → 当没锁（下一轮重新拆）


def save(store: Any, task_id: Any, req_version: Any, items: list[dict]) -> None:
    """锁定这一版需求的清单（kv，带版本号）；出错只记日志，不打断任务。"""
    try:
        with store.tx() as conn:
            store.kv_set(
                conn,
                kv_key(task_id),
                {
                    "req_version": int(req_version),
                    "items": list(items or []),
                    "ts": clock.now(),
                },
            )
    except Exception:
        logger.exception("存需求清单失败（任务 %s）", task_id)
