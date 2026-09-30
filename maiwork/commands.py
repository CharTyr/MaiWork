"""commands.py（M3）：/mw 群指令（docs/02 §5.3、docs/07 §11.5）。

- 纯代码固定回复，**不调模型**。
- 只在服务群被调用（intake 保证）；任何异常吞掉，不能让后台协程炸出声响。
- 回复一律进 Outbox：key=f"cmd:{message_id}"（按消息 ID 去重，同一条消息只回一次），
  kind="text"，payload 带 reply_to=消息 ID、push_kind="command"（不受推送上限和睡觉时段限制，
  见 delivery.py / outbox.py 的约定）；随后立刻 flush 一次，发不出去留在发件箱等后台补。

指令表（02 §5.3）：
- /mw            任何人：本群进行中的目标、任务、待批请求（简短，各最多 5 条）
- /mw 网页       任何人：本群网页链接（没配 public_url 就回「网页还没公开，找管理员要」）
- /mw 批准 [ID]  bot 管理员 或**本群**群管理员（kv["group_admins.<群号>"] 名单里的人）：
  批准待批请求；不带 ID 时本群只有一个待批直接处理，多个列出让人选；
  本群管理员只能批本群的请求（别的群的请求 ID 直接拒）
- /mw 拒绝 [ID]  bot 管理员 或本群群管理员：同上
- /mw 取消 [ID]  发起人 / 群主 / 群管理 / bot 管理员：取消任务（T-）、agent 目标（G-）、提醒（M-）
- /mw 领取 T-x  本群任何人：明确当场索取已完成且待发的成品（不重复传已发/不确定的文件）
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

from . import clock

logger = logging.getLogger("maiwork.commands")

_USAGE = (
    "MaiWork 指令：/mw 看进度；/mw 网页 拿本群网页链接；"
    "/mw 领取 T-任务号 当场取成品；/mw 批准 [ID]、/mw 拒绝 [ID]（bot 管理员）；"
    "/mw 取消 [ID]（发起人 / 群管理 / bot 管理员）"
)

_ID_PREFIX_KIND = {"T": "task", "G": "goal", "M": "goal"}

_TASK_STATUS_ZH = {
    "queued": "排队中",
    "running": "在做",
    "waiting_input": "等回答",
    "shelved": "已搁置",
    "reviewing": "验收中",
    "completed": "已完成",
    "failed": "失败",
    "paused": "已暂停",
    "cancelled": "已取消",
    "rejected": "未批准",
}

_GOAL_STATE_ZH = {"active": "进行中", "paused": "已暂停", "done": "已完成", "cancelled": "已取消"}


class Commands:
    def __init__(
        self,
        store: Any,
        approvals: Any,
        tasks: Any,
        goals: Any,
        outbox: Any,
        host: Any,
        get_settings: Callable[[], Any],
        *,
        coordinator: Any = None,
        run_task_starter: Callable[[str], None] | None = None,
        run_task_stopper: Callable[[str], None] | None = None,
        group_admins: Any = None,
    ) -> None:
        self._store = store
        self._approvals = approvals
        self._tasks = tasks
        self._goals = goals
        self._outbox = outbox
        self._host = host
        self._get_settings = get_settings
        self._coordinator = coordinator
        # 按群的管理员（group_admins.py）；没接上就只有 bot 管理员能批
        self._group_admins = group_admins
        # 开工任务的回调（app 提供，保证同一任务不并发；没有就用兜底 spawn）
        self._run_task_starter = run_task_starter
        # 停掉正在跑的任务的回调（app.cancel_task_run；取消后子 agent 尽快停）
        self._run_task_stopper = run_task_stopper

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def handle(self, group_id: str, user_id: str, user_name: str, text: str, message_id: str) -> None:
        gid = str(group_id)
        message_id_s = str(message_id or "")
        try:
            if not self._get_settings().is_served(gid):
                return  # 不能只依赖 intake；非服务群不读库也不发消息
        except Exception:
            return
        try:
            parts = (str(text or "")).strip().split()
            reply = await self._dispatch(gid, str(user_id or ""), str(user_name or ""), parts)
        except Exception:
            logger.exception("/mw 指令处理出错（群 %s，消息 %s）", gid, message_id_s)
            reply = "这条指令处理出错了，日志里有；admin 可以到网页上看"
        try:
            self._outbox.enqueue(
                f"cmd:{message_id_s}",
                gid,
                "text",
                {"text": reply, "reply_to": message_id_s, "push_kind": "command"},
            )
        except Exception:
            logger.exception("/mw 回复入队失败（群 %s，消息 %s）", gid, message_id_s)
            return
        try:
            await self._outbox.flush(clock.now())
        except Exception:
            logger.exception("/mw 回复立刻发送失败，留在发件箱等后台补发（群 %s）", gid)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    async def _dispatch(self, gid: str, user_id: str, user_name: str, parts: list[str]) -> str:
        if len(parts) <= 1:
            return self._status(gid)
        sub = parts[1]
        arg = parts[2] if len(parts) > 2 else ""
        if sub == "网页":
            return self._web(gid)
        if sub in ("批准", "通过"):
            return await self._decide(gid, user_id, arg, op="approve")
        if sub == "拒绝":
            return await self._decide(gid, user_id, arg, op="reject")
        if sub == "取消":
            return await self._cancel(gid, user_id, arg)
        if sub == "领取":
            return self._claim_delivery(gid, arg)
        return _USAGE

    def _claim_delivery(self, gid: str, task_id: str) -> str:
        """只有明确索取本群指定任务的这条指令能升级待发交付。"""
        if not re.fullmatch(r"T-\d+", str(task_id or "")):
            return "把本群任务号带上，例如 /mw 领取 T-3"
        outcome = self._outbox.claim_delivery(gid, task_id)
        if outcome == "queued":
            return f"收到，{task_id} 的成品现在交付；如果发件失败请到本群网页核对"
        if outcome == "sent":
            return f"{task_id} 已交付过，请在群文件或本群网页查收，不会重复上传"
        if outcome == "sending":
            return f"{task_id} 正在发送中，请稍等，不会重复上传"
        if outcome == "not_ready":
            return f"{task_id} 还没完成，请完成后再领"
        if outcome in ("uncertain", "failed", "broken"):
            return f"{task_id} 的交付需要管理员到网页核对，不会盲目重复上传"
        return "本群没有可领取的这份成品，请核对任务号或找管理员查看发件状态"

    # ------------------------------------------------------------------
    # /mw：本群进行中的目标、任务、待批（各最多 5 条）
    # ------------------------------------------------------------------

    def _status(self, gid: str) -> str:
        lines: list[str] = ["MaiWork 本群情况："]
        goals_view = {"agent": [], "member": []}
        try:
            if self._goals is not None:
                goals_view = self._goals.view(gid) or goals_view
        except Exception:
            logger.exception("/mw 读目标失败（群 %s）", gid)
        goal_items: list[str] = []
        for g in (goals_view.get("agent") or []):
            if str(g.get("state")) in ("active", "paused"):
                goal_items.append(
                    f"{g.get('id')} {str(g.get('title') or '')[:20]}（{_GOAL_STATE_ZH.get(str(g.get('state')), str(g.get('state')))}）"
                )
        for g in (goals_view.get("member") or []):
            if str(g.get("state")) == "active":
                goal_items.append(f"{g.get('id')} {str(g.get('title') or '')[:20]}（{g.get('who') or ''} 的提醒）")

        task_items: list[str] = []
        try:
            if self._tasks is not None:
                for t in self._tasks.list_view(gid) or []:
                    if str(t.get("status")) in ("cancelled", "rejected"):
                        continue
                    task_items.append(
                        f"{t.get('id')} {str(t.get('title') or '')[:20]}（{_TASK_STATUS_ZH.get(str(t.get('status')), str(t.get('status')))}）"
                    )
        except Exception:
            logger.exception("/mw 读任务失败（群 %s）", gid)

        pending_items: list[str] = []
        try:
            if self._approvals is not None:
                for p in self._approvals.pending_view(gid) or []:
                    pending_items.append(f"{p.get('id')} {str(p.get('title') or '')[:20]}（{p.get('who') or ''} 发起）")
        except Exception:
            logger.exception("/mw 读待批失败（群 %s）", gid)

        lines += self._section("进行中的目标", goal_items)
        lines += self._section("任务", task_items)
        lines += self._section("待批", pending_items)
        return "\n".join(lines)

    @staticmethod
    def _section(name: str, items: list[str], limit: int = 5) -> list[str]:
        out = [f"{name}："]
        if not items:
            out.append("· （没有）")
            return out
        for it in items[:limit]:
            out.append(f"· {it}")
        if len(items) > limit:
            out.append(f"· …共 {len(items)} 件")
        return out

    # ------------------------------------------------------------------
    # /mw 网页：本群链接
    # ------------------------------------------------------------------

    def _web(self, gid: str) -> str:
        from .card_push import group_link

        try:
            settings = self._get_settings()
        except Exception:
            settings = None
        link = group_link(self._store, settings, gid) if settings is not None else ""
        if not link:
            return "网页还没公开，找管理员要"
        return f"本群网页：{link}"

    # ------------------------------------------------------------------
    # /mw 批准 / 拒绝 [ID]：只限 bot 管理员
    # ------------------------------------------------------------------

    def _platform_of(self, gid: str) -> str:
        """群所在平台（qq / telegram）：管理员名单按「平台:账号」比对。"""
        try:
            settings = self._get_settings()
            return settings.platform_of(str(gid)) if settings is not None else "qq"
        except Exception:
            return "qq"

    def _is_group_admin(self, gid: str, user_id: str) -> bool:
        """本群群管理员名单里的人（group_admins 没接上 / 读失败都当不是）。"""
        if self._group_admins is None:
            return False
        try:
            return bool(self._group_admins.is_group_admin(gid, user_id, platform=self._platform_of(gid)))
        except Exception:
            logger.exception("/mw 判本群管理员失败（群 %s）", gid)
            return False

    async def _decide(self, gid: str, user_id: str, rid: str, *, op: str) -> str:
        verb = "批准" if op == "approve" else "拒绝"
        try:
            is_bot_admin = bool(self._approvals.is_admin(user_id, platform=self._platform_of(gid)))
        except Exception:
            logger.exception("/mw 判管理员失败")
            return "这条指令处理出错了，日志里有"
        is_group_admin = (not is_bot_admin) and self._is_group_admin(gid, user_id)
        if not (is_bot_admin or is_group_admin):
            return "只有 bot 管理员或本群管理员能批准 / 拒绝"
        rid_s = str(rid or "").strip()
        if not rid_s:
            try:
                pending = self._approvals.pending_view(gid) or []
            except Exception:
                logger.exception("/mw 读待批失败（群 %s）", gid)
                return "这条指令处理出错了，日志里有"
            if not pending:
                return "现在没有待批的请求"
            if len(pending) > 1:
                listing = "；".join(
                    f"{p.get('id')} {str(p.get('title') or '')[:16]}（{p.get('who') or ''}）" for p in pending[:5]
                )
                extra = f"等共 {len(pending)} 件" if len(pending) > 5 else ""
                return f"有好几件等着处理{extra}，把 ID 带上，比如 /mw {verb} {pending[0].get('id')}：{listing}"
            rid_s = str(pending[0].get("id") or "")
        if not rid_s:
            return "现在没有待批的请求"
        if is_group_admin and not is_bot_admin:
            # 本群管理员只能批本群的请求：先查出这条请求属于哪个群
            try:
                req_gid = self._approvals.group_of(rid_s)
            except Exception:
                logger.exception("/mw 查请求归属失败（%s）", rid_s)
                return "这条指令处理出错了，日志里有"
            if req_gid is None:
                return f"没找到请求 {rid_s}"
            if str(req_gid) != str(gid):
                return f"请求 {rid_s} 不在本群，管不了"
        try:
            if op == "approve":
                res = self._approvals.approve(rid_s, by=user_id)
            else:
                res = self._approvals.reject(rid_s, by=user_id)
        except KeyError:
            return f"没找到请求 {rid_s}"
        except ValueError as e:
            return str(e)
        if op == "reject":
            return f"已拒绝 {rid_s}"
        if not isinstance(res, dict):
            return f"已批准 {rid_s}"
        tids = [str(t) for t in (res.get("task_ids") or []) if str(t)]
        tid = str(res.get("task_id") or "")
        if tid and tid not in tids:
            tids.insert(0, tid)
        goal_id = str(res.get("goal_id") or "")
        if tids:
            for one in tids:
                self._start_task(one)
            return f"已批准 {rid_s}，任务 {'、'.join(tids)} 这就开工"
        if goal_id:
            return f"已批准 {rid_s}，目标 {goal_id} 记下了，会定期检查"
        return f"已批准 {rid_s}"

    def _start_task(self, task_id: str) -> None:
        try:
            if self._run_task_starter is not None:
                self._run_task_starter(task_id)
                return
        except Exception:
            logger.exception("run_task_starter 出错（任务 %s）", task_id)
        if self._coordinator is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._coordinator.run_task(task_id))
            # M9：后台协程的异常要有处看，不能「never retrieved」草草了事
            task.add_done_callback(self._log_task_exception)
        except RuntimeError:
            logger.warning("没有运行中的事件循环，任务 %s 等后台循环捞起来再开工", task_id)
        except Exception:
            logger.exception("spawn run_task 出错（任务 %s）", task_id)

    @staticmethod
    def _log_task_exception(task: "asyncio.Task") -> None:
        """M9：后台 run_task 协程的 done callback——cancelled 之外的异常记日志。"""
        try:
            if task.cancelled():
                return
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            exc = None
        if exc is not None:
            logger.exception("后台开工任务出错：%s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # /mw 取消 [ID]：发起人 / 群主 / 群管理 / bot 管理员
    # ------------------------------------------------------------------

    async def _cancel(self, gid: str, user_id: str, ident: str) -> str:
        ident_s = str(ident or "").strip().upper()
        if not ident_s or "-" not in ident_s:
            return "把要取消的 ID 带上（任务 T-x、目标 G-x、提醒 M-x），比如 /mw 取消 T-3"
        prefix = ident_s.split("-", 1)[0]
        kind = _ID_PREFIX_KIND.get(prefix)
        if kind is None:
            return "ID 只认 T-x（任务）、G-x（目标）、M-x（提醒）"
        obj = None
        try:
            if prefix == "T":
                obj = self._tasks.get(ident_s) if self._tasks is not None else None
            else:
                obj = self._goals.get(ident_s) if self._goals is not None else None
        except Exception:
            logger.exception("/mw 取消读对象失败（%s）", ident_s)
        if obj is None:
            return f"没找到 {ident_s}"
        if str(obj.get("group_id") or "") != gid:
            return "这条不是本群的，到它自己的群里取消"
        role = ""
        try:
            if self._host is not None:
                role = await self._host.group_member_role(gid, user_id)
        except Exception:
            logger.exception("/mw 取消读群角色失败（群 %s，人 %s）", gid, user_id)
            role = ""
        try:
            allowed = self._approvals.can_cancel(kind, ident_s, user_id, group_role=str(role or ""))
        except Exception:
            logger.exception("/mw 取消判权限失败")
            allowed = False
        if not allowed:
            return "只有发起人、群管理或 bot 管理员能取消"
        try:
            if prefix == "T":
                self._tasks.transition(ident_s, "cancelled", reason=f"群里 {user_id} 取消")
                # 取消要把正在跑的子 agent 也停掉（线上踩过：取消了还跑了 40 秒）
                if self._run_task_stopper is not None:
                    try:
                        self._run_task_stopper(ident_s)
                    except Exception:
                        logger.exception("停任务 %s 的后台协程出错", ident_s)
            else:
                self._goals.cancel(ident_s)
        except KeyError:
            return f"没找到 {ident_s}"
        except ValueError as e:
            return str(e)
        return f"已取消 {ident_s}"
