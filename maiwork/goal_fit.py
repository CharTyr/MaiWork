"""R5 立目标前的判断：这件事 MaiWork 自己能不能在网上盯 / 做成。

背景（.trellis/tasks/10-11-goals-fit-group-chat/prd.md R5）：目标只接 MaiWork 自己能在
网上盯 / 做成的事（例：盯游戏新版本、盯某个 mod 发布）；要群友自己动手的集体计划
（投票、开档、截图、到场）MaiWork 盯不了。所以一条可能立成目标的请求落进待批队列后，
后台让主模型判一次，结果写在请求行上（requests.goal_fit / goal_fit_reason / goal_fit_alt）：

- `online` → 批准后照旧立目标；
- `human` → 批准后改成一次性任务（主模型给了替代任务时）/ 只能不批（没给替代任务时）；
- `error` / 没判（`''`）→ 照旧立目标（判断失败不比现在差，网页标「没判断」）。

红线：
- 非服务群、空群号、请求不是 pending、不需要判断（不是 goal 请求，构想里也没有 goal 项目）
  → **零模型调用、零写入**；
- 一次请求最多一次模型调用，出错写 `error` 不重试、不挡审批；
- 写回前重查请求仍是 pending，否则整个结果丢弃（人批优先）；
- 每次判断记一条 `request.goal_fit` 事件（出错也记）。

调用方（app.py）在 `Approvals.create` 落 pending 时收到回调，把 `judge(rid)` spawn 到后台
（和自动审核同一个回调），绝不卡住收消息钩子。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import clock
from .approvals import _norm_alt   # 替代任务的形状由 approvals（pending_view / _land）读，共用一处校验
from .models import ModelError

logger = logging.getLogger("maiwork.goal_fit")

# 模型调用用途（网页 / 用量统计里显示；docs/07）
PURPOSE = "approvals.goal_fit"

FIT_ONLINE = "online"
FIT_HUMAN = "human"
FIT_ERROR = "error"
_FIT_KINDS = (FIT_ONLINE, FIT_HUMAN)

_TITLE_MAX = 60
_QUOTE_MAX = 500
_REASON_MAX = 80
_ITEM_DESC_MAX = 200
_IDEA_BODY_MAX = 300


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _fold(s: Any, limit: int) -> str:
    """折叠空白 + 截断（写库前统一走这里）。"""
    return " ".join(str(s or "").split())[:limit]


class GoalFitJudge:
    """判「这件目标 MaiWork 自己能不能在网上盯 / 做成」（goal_fit.py 顶部有完整说明）。"""

    def __init__(
        self,
        store: Any,
        models: Any,
        get_settings: Callable[[], Any],
        approvals: Any = None,
    ) -> None:
        self._store = store
        self._models = models
        self._get_settings = get_settings
        # 复用 Approvals.picked_idea_items_numbered（构想项目 + 序号）；没有就判不了构想请求
        self._approvals = approvals

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    async def judge(self, request_id: str) -> None:
        """判一次并把结果写在请求行上；任何意外都不抛（后台协程，出错只记日志）。"""
        rid = _norm(request_id)
        if not rid or self._store is None or self._models is None:
            return
        row = self._read(rid)
        if row is None or _norm(row.get("status")) != "pending":
            return
        gid = _norm(row.get("group_id"))
        if not gid or not self._served(gid):
            return  # 非服务群零模型调用
        targets = self._goal_targets(row)
        if targets is None:
            return  # 这条请求不会立目标（或不来自构想也拿不准）：不判
        mode, items = targets
        fit, reason, alt = await self._judge(gid, row, mode, items)
        # 模型调用期间可能已经被人批 / 拒了：那不是 pending 就不写了
        fresh = self._read(rid)
        if fresh is None or _norm(fresh.get("status")) != "pending":
            return
        self._save(rid, gid, fit, reason, alt)

    # ------------------------------------------------------------------
    # 要不要判 / 判哪些项目
    # ------------------------------------------------------------------

    def _goal_targets(self, row: dict) -> tuple[str, list[tuple[int, dict]]] | None:
        """`("single", [])` / `("idea", [(序号, 项目)])` / None（不用判）。

        口径和 `Approvals._land` 一致：构想请求里点名选中的项目优先；没有项目可拆时按
        请求自己的 kind 走。
        """
        numbered = self._numbered_items(row)
        if numbered:
            goals_only = [
                (no, it) for no, it in numbered if _norm(it.get("kind")).lower() == "goal"
            ]
            return ("idea", goals_only) if goals_only else None
        if _norm(row.get("kind")) == "goal":
            return ("single", [])
        return None

    def _numbered_items(self, row: dict) -> list[tuple[int, dict]]:
        if row.get("idea_id") is None or self._approvals is None:
            return []
        try:
            return list(self._approvals.picked_idea_items_numbered(row))
        except Exception:
            logger.debug("读构想的项目失败（请求 %s）", row.get("id"), exc_info=True)
            return []

    def _served(self, gid: str) -> bool:
        try:
            settings = self._get_settings()
            return bool(settings is not None and settings.is_served(gid))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 问主模型
    # ------------------------------------------------------------------

    async def _judge(
        self, gid: str, row: dict, mode: str, items: list[tuple[int, dict]]
    ) -> tuple[str, str, dict | None]:
        """返回 `(kind, reason, alt)`；任何意外 → `("error", 出错原因, None)`，不重试。"""
        try:
            prompt = self._build_prompt(row, mode, items)
            result = await self._models.chat(
                agent="main",
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
                purpose=PURPOSE,
                group_id=gid,
            )
            data = json.loads(result.text)
            if not isinstance(data, dict):
                raise ValueError("模型没给 JSON 对象")
            if mode == "idea":
                return self._parse_idea(data, items)
            return self._parse_single(data)
        except (ModelError, ValueError, TypeError, KeyError) as e:
            logger.info("目标判断放弃（请求 %s），照旧按目标接：%s", row.get("id"), e)
            return FIT_ERROR, _fold(e, _REASON_MAX) or "判断失败", None
        except Exception as e:  # 端点的稀奇古怪错误也不许冒到后台任务里
            logger.exception("目标判断出错（请求 %s），照旧按目标接", row.get("id"))
            return FIT_ERROR, _fold(e, _REASON_MAX) or "判断失败", None

    @staticmethod
    def _parse_single(data: dict) -> tuple[str, str, dict | None]:
        kind = _norm(data.get("kind")).lower()
        if kind not in _FIT_KINDS:
            raise ValueError(f"不认识的判断「{_norm(data.get('kind'))}」")
        reason = _fold(data.get("reason"), _REASON_MAX)
        alt = None if kind == FIT_ONLINE else _norm_alt(data.get("alt"))
        return kind, reason, alt

    @staticmethod
    def _parse_idea(data: dict, items: list[tuple[int, dict]]) -> tuple[str, str, dict | None]:
        raw = data.get("items")
        if not isinstance(raw, dict):
            raise ValueError("构想判断缺 items")
        out: dict[str, dict] = {}
        for no, _it in items:
            one = raw.get(str(no))
            if not isinstance(one, dict):
                one = raw.get(no)
            if not isinstance(one, dict):
                raise ValueError(f"构想的第 {no} 项没有判断")
            kind = _norm(one.get("kind")).lower()
            if kind not in _FIT_KINDS:
                raise ValueError(f"构想的第 {no} 项判断不认识「{_norm(one.get('kind'))}」")
            out[str(no)] = {
                "kind": kind,
                "reason": _fold(one.get("reason"), _REASON_MAX),
                "alt": None if kind == FIT_ONLINE else _norm_alt(one.get("alt")),
            }
        kinds = [out[str(no)]["kind"] for no, _ in items]
        fit = FIT_HUMAN if FIT_HUMAN in kinds else FIT_ONLINE
        reason = ""
        if fit == FIT_HUMAN:
            # 汇总理由取第一个（序号最小的）human 项的理由
            reason = next(out[str(no)]["reason"] for no, _ in items if out[str(no)]["kind"] == FIT_HUMAN)
        return fit, reason, {"items": out}

    def _build_prompt(self, row: dict, mode: str, items: list[tuple[int, dict]]) -> str:
        title = _fold(row.get("title"), _TITLE_MAX)
        quote = _fold(row.get("quote"), _QUOTE_MAX)
        lines: list[str] = []
        if mode == "idea":
            idea = self._idea_of(row)
            lines.append("有人从构想里挑了要做的事，其中几件是想让 MaiWork 长期盯着的。")
            lines.append("现在逐个判断：**办成它要不要群友自己动手？**")
            if idea is not None:
                lines.append("")
                lines.append(f"构想标题：{_fold(idea.get('title'), _TITLE_MAX)}")
                body = _fold(idea.get("body"), _IDEA_BODY_MAX)
                if body:
                    lines.append(f"构想说明：{body}")
            lines.append("")
            lines.append("要判断的项目（方括号里是序号）：")
            for no, it in items:
                desc = _fold(it.get("desc"), _ITEM_DESC_MAX) or "（没写说明）"
                lines.append(f"[{no}] {_fold(it.get('title'), _TITLE_MAX)}：{desc}")
        else:
            lines.append("有人想在群里立一件「目标」（要长期盯着、慢慢推进的事）。")
            lines.append("现在只判断一件事：**办成它要不要群友自己动手？**")
            lines.append("")
            lines.append(f"标题：{title}")
            if quote:
                lines.append(f"原话：{quote}")
        lines.extend([
            "",
            "判断标准（只看「完成它要不要群友自己动手」）：",
            "- online：完成它只需要 MaiWork 上网查、看、整理、做东西"
            "（盯某个版本更新、盯某个发布、查资料、整理成清单 / 网页）。",
            "- human：要群友自己去投票、开档、截图、到场、动手"
            "（集体计划、约着一起玩、要大家参与的活动）。",
            "",
        ])
        if mode == "idea":
            lines.extend([
                "只回 JSON：",
                '{"items": {"<项目序号>": {"kind": "online" 或 "human",'
                ' "reason": "一句话中文理由（给管理员看的）",'
                ' "alt": {"title": "替代任务标题（≤30 字）", "req": "这个任务要做什么（≤200 字）"} 或 null}}}',
                "每个序号都要给一条，键就用方括号里的数字（字符串）。",
                "human 的项目尽量给一个 MaiWork 自己一次能做完的替代任务"
                "（例：整理一份 mod 清单和分线说明）；给不出就把 alt 写成 null。online 的项目 alt 一律 null。",
            ])
        else:
            lines.extend([
                "只回 JSON：",
                '{"kind": "online" 或 "human", "reason": "一句话中文理由（给管理员看的）",'
                ' "alt": {"title": "替代任务标题（≤30 字）", "req": "这个任务要做什么（≤200 字）"} 或 null}',
                "human 时尽量给一个 MaiWork 自己一次能做完的替代任务"
                "（例：整理一份 mod 清单和分线说明）；给不出就把 alt 写成 null。online 时 alt 一律 null。",
            ])
        return "\n".join(lines)

    def _idea_of(self, row: dict) -> dict | None:
        if row.get("idea_id") is None:
            return None
        try:
            got = self._store.read().execute(
                "SELECT id, title, body FROM ideas WHERE id=?", (int(row["idea_id"]),)
            ).fetchone()
        except Exception:
            logger.debug("读构想失败（请求 %s）", row.get("id"), exc_info=True)
            return None
        if got is None:
            return None
        try:
            return {k: got[k] for k in got.keys()}
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 写回
    # ------------------------------------------------------------------

    def _save(self, rid: str, gid: str, fit: str, reason: str, alt: dict | None) -> None:
        try:
            with self._store.tx() as conn:
                cur = conn.execute(
                    "UPDATE requests SET goal_fit=?, goal_fit_reason=?, goal_fit_alt=?, updated=?"
                    " WHERE id=? AND status='pending'",
                    (fit, reason, (json.dumps(alt, ensure_ascii=False) if alt else ""), clock.now(), rid),
                )
                if int(cur.rowcount or 0) <= 0:
                    return  # 就在写回这一刻被人处理了：连事件都不记
                self._store.event(
                    conn, "request.goal_fit", group_id=gid, entity="request", entity_id=rid,
                    payload={"fit": fit, "reason": reason, "alt": alt},
                )
        except Exception:
            logger.exception("写目标判断失败（请求 %s），照旧按目标接", rid)

    def _read(self, rid: str) -> dict | None:
        try:
            got = self._store.read().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        except Exception:
            logger.debug("读待批请求失败（%s）", rid, exc_info=True)
            return None
        if got is None:
            return None
        try:
            return {k: got[k] for k in got.keys()}
        except Exception:
            return None
