"""派活「自动审核」：低风险的轻活，主模型看一眼就自动批（docs/02 §5.2，2026-10）。

背景：群友派的活默认要 bot 管理员批准（approvals.py）。管理员不想为「帮我查个东西」
这种轻活天天点一下，所以加了这一层：请求落到待批队列后，主模型判断它是不是
**低风险、短、清楚**的活；是就自动批准开工，不是就照旧等人批。

红线（和 approvals.py 同一套）：
- **只审 `kind="task"`**：agent 目标永远要人批。
- 来自构想、且点名选中的项目里含 `goal` 的请求 → **整条**留人批。
- `source="maiwork"`（MaiWork 主动提的目标）和 `force_manual=True` 的请求永远不自动批。
- 模型没配好 / 调用异常 / 输出解析不了 / `approve` 不是布尔真 → 一律留人批，
  **不重试、不报错给群**（只在日志里留一句）。
- 非服务群零模型调用。
- 每群每天上限（`approval.auto_review_daily`，默认 5，`0` = 关），按北京日期存
  kv（`auto_review.day.<群号>`）；**只有真正自动批通过才计数**；上限到了直接留人批
  （连模型都不调）。
- 自动批通过走和人批**完全同一条**落地路径：`Approvals.approve` → `_land` → 开工
  spawn（app.spawn_run_task；`/mw 批准` 和网页批准也是这条），批准人记
  「MaiWork 自动审核」，理由存 `requests.auto_reason`。

调用方（app.py）在 `Approvals.create` 落 pending 时收到回调，把 `review()` spawn 到
后台：判断是异步的，绝不卡住收消息钩子。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import clock
from .models import ModelError

logger = logging.getLogger("maiwork.auto_review")

# 批准人（写进 requests.decided_by；网页任务里显示「自动审核通过：<理由>」）
AUTO_BY = "MaiWork 自动审核"

_TITLE_MAX = 60
_QUOTE_MAX = 500
_VIA_MAX = 120
_REASON_MAX = 200
_KV_PREFIX = "auto_review.day."
_DAILY_DEFAULT = 5
_DAILY_MAX = 50
_PURPOSE = "approvals.auto_review"


def _norm(s: Any) -> str:
    return str(s or "").strip()


class AutoReviewer:
    def __init__(
        self,
        store: Any,
        models: Any,
        approvals: Any,
        get_settings: Callable[[], Any],
        *,
        run_task_starter: Callable[[str], Any] | None = None,
    ) -> None:
        self._store = store
        self._models = models
        self._approvals = approvals
        self._get_settings = get_settings
        # 开工入口（app.spawn_run_task）；和人批 / 网页批准复用同一个，不另起一份
        self._run_task_starter = run_task_starter

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    async def review(self, request_id: str, *, group_id: str = "") -> dict | None:
        """看一眼这条待批请求该不该自动批。

        返回落地的结果（`status="approved"`）；不该自动批 / 已经被人处理了 / 出错
        → None（请求原样留给人批）。
        """
        rid = _norm(request_id)
        if not rid or self._store is None:
            return None
        try:
            row = self._pending_row(rid)
        except Exception:
            logger.debug("读待批请求失败（%s）", rid, exc_info=True)
            return None
        if row is None:
            return None
        gid = _norm(row.get("group_id")) or _norm(group_id)
        day = clock.day_key(clock.now())
        if not self._eligible(gid, row, day):
            return None
        reason = await self._judge(gid, row)
        if reason is None:
            return None
        # 模型调用期间，人批、群服务列表、配置和北京日期都可能已经变了。
        try:
            fresh = self._pending_row(rid)
            day = clock.day_key(clock.now())
            if fresh is None or not self._eligible(gid, fresh, day):
                return None
        except Exception:
            logger.debug("自动审核落地前重查失败（%s），留给人批", rid, exc_info=True)
            return None
        return self._approve(rid, gid, day, reason)

    # ------------------------------------------------------------------
    # 前置条件（任何一条不过 → 留人批；调用前就把上限卡掉）
    # ------------------------------------------------------------------

    def _eligible(self, gid: str, row: dict, day: str) -> bool:
        if str(row.get("status") or "") != "pending":
            return False
        if str(row.get("kind") or "") != "task":
            return False  # goal 永远要人批
        if str(row.get("source") or "") == "maiwork":
            return False  # MaiWork 主动提的目标：红线，永远人批
        if bool(row.get("force_manual") or 0):
            return False
        try:
            settings = self._get_settings()
        except Exception:
            return False
        if settings is None:
            return False
        approval = getattr(settings, "approval", None)
        if approval is None:
            return False
        if not bool(getattr(approval, "auto_review", True)):
            return False
        cap = self._daily_cap(approval)
        if cap <= 0:
            return False  # 0 = 关
        if not gid:
            return False  # 空群号不是服务群，也不能调模型
        try:
            if not settings.is_served(gid):
                return False  # 非服务群零模型调用
        except Exception:
            return False
        if self._count(gid, day) >= cap:
            return False  # 今天这个群自动批的次数到上限了：连模型都不调
        if self._idea_has_goal(row):
            return False  # 来自构想、选中的项目里有 goal：整条留人批
        if self._models is None:
            return False
        try:
            if not bool(self._models.settings().ready()):
                return False
        except Exception:
            return False
        return True

    @staticmethod
    def _daily_cap(approval: Any) -> int:
        raw = getattr(approval, "auto_review_daily", _DAILY_DEFAULT)
        if isinstance(raw, bool):
            return _DAILY_DEFAULT
        try:
            return max(0, min(_DAILY_MAX, int(raw)))
        except (TypeError, ValueError):
            return _DAILY_DEFAULT

    def _idea_has_goal(self, row: dict) -> bool:
        """来自构想的请求：点名的项目里只要有 goal → True。

        保守口径：`idea_id` 有值但构想行读不到 / 查不出来 → 也算 True（不知道有没有 goal
        就留给人批）。老构想（items 为空）或群友直接派活 → False（照常审）。
        """
        iid = row.get("idea_id")
        if iid is None:
            return False
        try:
            got = self._store.read().execute("SELECT 1 FROM ideas WHERE id=?", (int(iid),)).fetchone()
        except Exception:
            logger.debug("查构想失败（%s），这条留给人批", iid, exc_info=True)
            return True
        if got is None:
            return True  # 构想行不见了：不知道有没有 goal，别替管理员拍板
        try:
            picked = self._approvals.picked_idea_items(row)
        except Exception:
            logger.debug("读构想的项目失败（%s），这条留给人批", iid, exc_info=True)
            return True
        return any(str(it.get("kind") or "").lower() == "goal" for it in picked)

    # ------------------------------------------------------------------
    # 每日上限（kv；北京日期 + 计数）
    # ------------------------------------------------------------------

    @staticmethod
    def _day_key(gid: str) -> str:
        return f"{_KV_PREFIX}{gid}"

    def _count(self, gid: str, day: str) -> int:
        if not gid:
            return 0
        try:
            data = self._store.kv_get(self._day_key(gid), None)
        except Exception:
            return 0
        if not isinstance(data, dict) or str(data.get("day") or "") != day:
            return 0
        try:
            return max(0, int(data.get("n") or 0))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # 问主模型
    # ------------------------------------------------------------------

    async def _judge(self, gid: str, row: dict) -> str | None:
        """主模型判一下；能批就给理由，不能批 / 任何意外 → None。"""
        prompt = self._build_prompt(row)
        try:
            result = await self._models.chat(
                "main",
                [{"role": "user", "content": prompt}],
                json_mode=True,
                purpose=_PURPOSE,
                group_id=gid,
            )
            data = json.loads(result.text)
        except (ModelError, ValueError, TypeError) as e:
            logger.info("自动审核放弃（请求 %s）：%s", row.get("id"), e)
            return None
        except Exception:
            logger.exception("自动审核出错（请求 %s），留给人批", row.get("id"))
            return None
        if not isinstance(data, dict) or data.get("approve") is not True:
            return None
        reason = _norm(data.get("reason"))[:_REASON_MAX]
        if not reason:
            reason = "低风险的轻活"
        return reason

    @staticmethod
    def _build_prompt(row: dict) -> str:
        title = _norm(row.get("title"))[:_TITLE_MAX]
        quote = _norm(row.get("quote"))[:_QUOTE_MAX]
        via = _norm(row.get("via"))[:_VIA_MAX]
        lines = [
            "有人在群里派了一件活，现在要你替管理员初筛一下：它是不是「低风险、短、清楚」的轻活，",
            "可以直接开工？拿不准就留给人批。",
            "",
            f"请求标题：{title}",
        ]
        if quote:
            lines.append(f"请求原话：{quote}")
        if via:
            lines.append(f"来源：{via}")
        lines.extend([
            "",
            "可以直接批（approve=true）的例子：调研、对比、找资料、做个简单小网页、出一份 PDF / 文档，"
            "这类一次能做完、看得清要什么、不碰外面的事。",
            "必须留给人批（approve=false）：",
            "- 高风险：要花钱、要对外发消息 / 发帖 / 联系别人、要改线上的东西、"
            "涉及个人隐私或别人的账号、违法或者灰色的事；",
            "- 长 / 复杂：要好几天、是大工程、要长期盯着；",
            "- 需求说不清：不知道到底要什么、做到什么程度算完。",
            "",
            '只回 JSON：{"approve": true 或 false, "reason": "一句话中文理由（给管理员看的，别写实现细节）"}',
        ])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 落地（和人批同一条路）
    # ------------------------------------------------------------------

    def _approve(self, rid: str, gid: str, day: str, reason: str) -> dict | None:
        try:
            cap = self._daily_cap(self._get_settings().approval)
            res = self._approvals.approve(
                rid, by=AUTO_BY, auto_reason=reason, auto_daily=(day, cap),
            )
        except (KeyError, ValueError):
            # 已被人处理，或模型等待时额度用完：留给人批，零计数。
            return None
        except Exception:
            logger.exception("自动批准落地出错（请求 %s），留给人批", rid)
            return None
        tids = [str(t) for t in ((res or {}).get("task_ids") or []) if str(t)]
        tid = str((res or {}).get("task_id") or "")
        if tid and tid not in tids:
            tids.insert(0, tid)
        for one in tids:
            self._start(one)
        logger.info("自动审核通过（请求 %s，群 %s）：%s", rid, gid, reason)
        return {
            "id": rid, "status": "approved", "auto": True, "auto_reason": reason,
            "approved_by": AUTO_BY,
            "task_id": (tids[0] if tids else None),
            "task_ids": tids,
            "goal_id": str((res or {}).get("goal_id") or "") or None,
        }

    def _start(self, task_id: str) -> None:
        """复用 app 的开工入口（和人批 / 网页批准同一个，不另起一份）。"""
        starter = self._run_task_starter
        if not callable(starter) or not task_id:
            return
        try:
            starter(str(task_id))
        except Exception:
            logger.exception("自动批后开工出错（任务 %s）", task_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _pending_row(self, rid: str) -> dict | None:
        row = self._store.read().execute(
            "SELECT * FROM requests WHERE id=?", (rid,)
        ).fetchone()
        if row is None:
            return None
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return None
