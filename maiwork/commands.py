"""commands.py（M3）：/mw 群指令（docs/02 §5.3、docs/07 §11.5）。

- 纯代码固定回复，**不调模型**。
- 只在服务群被调用（intake 保证）；任何异常吞掉，不能让后台协程炸出声响。
- 回复一律进 Outbox：key=f"cmd:{message_id}"（按消息 ID 去重，同一条消息只回一次），
  kind="text"，payload 带 reply_to=消息 ID、push_kind="command"（不受推送上限和睡觉时段限制，
  见 delivery.py / outbox.py 的约定）；随后立刻 flush 一次，发不出去留在发件箱等后台补。

指令表（02 §5.3）：
- /mw            任何人：本群进行中的目标、任务、待批请求（简短，各最多 5 条）
- /mw do 要做的事 任何人（别名 /mw 做）：**不问 Jev、不调模型**，直接记一条待批请求，之后
  走正常批准流程（免批直接开工）。引用一条消息再发就用被引用的正文当要做的事，后面的话算
  发起人的补充说明；请求的 message_id 就是这条 /mw do 自己的消息（收图靠它，见 coordinator）。
- /mw 领取 T-x  本群任何人：明确当场索取已完成且待发的成品（不重复传已发/不确定的文件）
- /mw 网页       任何人：本群网页链接（public_url 优先；没填就按开关发 here.now 公开页；都没有回「网页还没公开…」）
- /mw 帮助       任何人（别名 help / ? / ？）：全部指令一张表（`HELP_ITEMS`，唯一来源）
- /mw 批准 [ID]  本群批准人（`kv["group_approval.<群号>"].approvers`，含全局管理员从旧
  名单种进来的那份）：批准**本群**的待批请求；不带 ID 时本群只有一个待批直接处理，多个列出
  让人选；请求属于别的群一律拒（群命令只管本群，bot 管理员也不例外）
- /mw 拒绝 [ID]  同上
- /mw 取消 [ID]  发起人 / 群主 / 群管理 / 本群批准人：取消任务（T-）、agent 目标（G-）、提醒（M-）

群友不会用就给提示：回复里顺手带下一步（状态末句、do 回批准 / 取消怎么发、批准回「发 /mw 看进度」）；
未知子命令回一句短提示；那截文字看着像派活（和 @ 派活同一份请求词表）就给一行能直接照抄的
「/mw do …」，**不建请求**。全部固定文本，不调模型。
另外 `/mw 帮助` 末尾附两条大白话用法（`USAGE_TIPS`）：「直接 @ 机器人就能让它提醒 / 帮忙盯着」，
**不是新指令**；`{bot}` 换成机器人在群里的名字，网页群视图的 `commands.tips` 和它同源。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

from . import clock
from .intake import _TITLE_MAX  # 标题上限和 @ 派活同一个口径
from .quick_judge import has_request_cue  # 「看着像派活」的请求词表只有这一份

logger = logging.getLogger("maiwork.commands")

# ----------------------------------------------------------------------
# 指令表（唯一来源）：/mw 帮助、未知子命令的短提示、网页群视图的 commands 都从它来
# ----------------------------------------------------------------------

HELP_ITEMS: tuple[dict[str, str], ...] = (
    {"who": "所有人", "syntax": "/mw", "example": "", "desc": "看本群进度（目标、任务、待批）"},
    {
        "who": "所有人",
        "syntax": "/mw do 要做的事",
        "example": "/mw do 整理一份开黑语音排障步骤",
        "desc": "派活；也可以引用一条消息再发 /mw do",
    },
    {"who": "所有人", "syntax": "/mw 领取 T-任务号", "example": "/mw 领取 T-8", "desc": "取做好的成品"},
    {"who": "所有人", "syntax": "/mw 网页", "example": "", "desc": "拿本群网页链接"},
    {"who": "所有人", "syntax": "/mw 帮助", "example": "", "desc": "看这份说明"},
    {"who": "批准人", "syntax": "/mw 批准 [ID]", "example": "/mw 批准 R-12", "desc": "批准开工"},
    {"who": "批准人", "syntax": "/mw 拒绝 [ID]", "example": "/mw 拒绝 R-12", "desc": "不批这条"},
    {
        "who": "发起人 / 群管理 / 批准人",
        "syntax": "/mw 取消 [ID]",
        "example": "/mw 取消 T-8",
        "desc": "取消任务 / 提醒（发起人、群管理、批准人都能发）",
    },
)

# `/mw 帮助` 的别名（中文、英文、问号两种写法）
_HELP_ALIASES = ("帮助", "help", "?", "？")
_HELP_TITLE = "MaiWork 群指令："
_HELP_GROUP_ALL = "大家都能用："
_HELP_GROUP_APPROVER = "批准人："
# 末尾两条大白话用法的小标题（不是指令，是「能这么跟机器人说话」）
_TIPS_TITLE = "也可以直接 @ 我说："
# 拿不到机器人在群里的名字时的回落（app.py 启动时把 bot.nickname 写进 host.bot_name_cache）
_BOT_NAME_FALLBACK = "MaiBot"
# 短提示里点名的三条常用：(表里的写法, 一句话)——写法都从表里取，不另写一份指令名
_HELP_BY_SYNTAX = {it["syntax"]: it for it in HELP_ITEMS}
_USAGE_HEADS = (("/mw", "看进度"), ("/mw do 要做的事", "派活"))
_USAGE_TAIL = "/mw 帮助"
# 「要派活请这样发」里原话最多留这么多字（QQ 消息，别太长）
_HINT_MAX = 100

# ----------------------------------------------------------------------
# 两条大白话用法（唯一来源）：群友不知道可以直接 @ 机器人让它提醒 / 帮忙盯着。
# 只是帮人看懂「能怎么跟机器人说话」，**不是新指令**；`{bot}` 用机器人在群里的
# 名字替换（见 usage_tips）。`/mw 帮助` 末尾和网页群视图的 commands.tips 都从这里来。
# ----------------------------------------------------------------------

USAGE_TIPS: tuple[dict[str, str], ...] = (
    {"say": "@{bot} 提醒我 周五晚八点 开黑", "desc": "到点在群里 @ 你提醒，不用批准"},
    {
        "say": "@{bot} 帮我们盯着 暗潮新版本，出了说一声",
        "desc": "MaiWork 在网上盯着，盯到了在群里说一声；要管理员批准",
    },
)


def _help_line(item: dict[str, str]) -> str:
    line = f"{item['syntax']} — {item['desc']}"
    return f"{line}，例：{item['example']}" if item.get("example") else line


def usage_tips(bot_name: str = "") -> list[dict[str, str]]:
    """USAGE_TIPS 的副本，把 `{bot}` 换成机器人在群里的名字（名字空着回落 MaiBot）。"""
    name = str(bot_name or "").strip() or _BOT_NAME_FALLBACK
    return [{k: str(v).replace("{bot}", name) for k, v in tip.items()} for tip in USAGE_TIPS]


def help_text(bot_name: str = "") -> str:
    """`/mw 帮助` 的整段回复：先是指令表（按「谁能用」分两组），末尾两条大白话用法。"""
    out = [_HELP_TITLE, _HELP_GROUP_ALL]
    out += [f"· {_help_line(it)}" for it in HELP_ITEMS if it["who"] == "所有人"]
    out.append(_HELP_GROUP_APPROVER)
    out += [f"· {_help_line(it)}" for it in HELP_ITEMS if it["who"] != "所有人"]
    out.append(_TIPS_TITLE)
    out += [f"· {t['say']} — {t['desc']}" for t in usage_tips(bot_name)]
    return "\n".join(out)


def usage_text(sub: str) -> str:
    """未知子命令的短提示：三条常用（写法取自指令表），完整清单在 /mw 帮助。"""
    heads = "；".join(f"{_HELP_BY_SYNTAX[s]['syntax']} {word}" for s, word in _USAGE_HEADS)
    return f"没有「{sub}」这个指令。常用：{heads}；全部指令发 {_HELP_BY_SYNTAX[_USAGE_TAIL]['syntax']}"


def do_hint(rest: str) -> str:
    """看着像派活但没写 do：给一行能直接照抄的 /mw do（只提示，不建请求）。"""
    return f"要派活请这样发：\n/mw do {str(rest or '').strip()[:_HINT_MAX]}"


def _rest_after_head(raw: str) -> str:
    """`/mw` 后面那截原文（保留原有空格和换行，只去首尾空白）。"""
    parts = str(raw or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _pending_reply(rid: str) -> str:
    """待批的统一回法：告诉批准人怎么一句话开工。"""
    return f"收到，{rid} 等管理员批准；批准人发 /mw 批准 {rid} 就开工"


def _started_reply(task_ids: str) -> str:
    """开工的统一回法：去哪看进度、怎么取消（用第一个任务号举例）。"""
    first = str(task_ids).split("、")[0]
    return f"收到，已开工（{task_ids}）；发 /mw 看进度，/mw 取消 {first} 可以取消"


# `/mw do` 的两种写法（别名「做」只在代码里认，文档只写 do）
_DO_SUBS = ("do", "做")
_DO_RE = re.compile(r"^\s*/mw\s+(?:do|做)\s*")
_DO_EMPTY = (
    "把要做的事写在后面，例如 /mw do 整理一份开黑语音排障步骤；"
    "也可以引用一条消息再发 /mw do；全部指令发 /mw 帮助"
)
_STATUS_HINT = "派活发 /mw do 要做的事；全部指令发 /mw 帮助"

_DO_VIA = "群里 /mw do 指令"
_QUOTE_MAX = 2000          # 引用来的正文最多留这么多字
_QUOTE_TIMEOUT_S = 5.0     # 取被引用消息的超时（宿主慢就当没引用）

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


def _do_text(raw: str) -> str:
    """「/mw do 要做的事」后面那截原文（保留原有空格 / 换行，只去首尾空白）。"""
    m = _DO_RE.match(str(raw or ""))
    return str(raw)[m.end():].strip() if m else ""


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
        share: Any = None,  # share.Share：没填公开网址时 /mw 网页发 here.now 公开页
    ) -> None:
        self._share = share
        self._store = store
        self._approvals = approvals
        self._tasks = tasks
        self._goals = goals
        self._outbox = outbox
        self._host = host
        self._get_settings = get_settings
        self._coordinator = coordinator
        # 旧接线残留：名单已并进 approvals 的每群批准名单（group_approval），这里只留个引用，
        # 不再作为第二个鉴权来源（单来源，避免两套名单各说各话）
        self._group_admins = group_admins
        # 开工任务的回调（app 提供，保证同一任务不并发；没有就用兜底 spawn）
        self._run_task_starter = run_task_starter
        # 停掉正在跑的任务的回调（app.cancel_task_run；取消后子 agent 尽快停）
        self._run_task_stopper = run_task_stopper

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def handle(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: str,
        reply_to: str = "",
    ) -> None:
        gid = str(group_id)
        message_id_s = str(message_id or "")
        try:
            if not self._get_settings().is_served(gid):
                return  # 不能只依赖 intake；非服务群不读库也不发消息
        except Exception:
            return
        try:
            raw = str(text or "").strip()
            parts = raw.split()
            reply = await self._dispatch(
                gid,
                str(user_id or ""),
                str(user_name or ""),
                parts,
                raw=raw,
                message_id=message_id_s,
                reply_to=str(reply_to or ""),
            )
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

    def _bot_name(self) -> str:
        """机器人在群里的名字：app.py 启动时预热进 host.bot_name_cache；拿不到给空串（回落 MaiBot）。"""
        try:
            return str(getattr(self._host, "bot_name_cache", "") or "")
        except Exception:
            logger.debug("取 host.bot_name_cache 失败，帮助文本回落 MaiBot", exc_info=True)
            return ""

    async def _dispatch(
        self,
        gid: str,
        user_id: str,
        user_name: str,
        parts: list[str],
        *,
        raw: str = "",
        message_id: str = "",
        reply_to: str = "",
    ) -> str:
        if len(parts) <= 1:
            return self._status(gid)
        sub = parts[1]
        arg = parts[2] if len(parts) > 2 else ""
        if sub in _DO_SUBS:
            return await self._do(gid, user_id, user_name, raw, message_id, reply_to)
        if sub == "网页":
            return await self._web(gid)
        if sub in _HELP_ALIASES:
            return help_text(self._bot_name())
        if sub in ("批准", "通过"):
            return await self._decide(gid, user_id, arg, op="approve")
        if sub == "拒绝":
            return await self._decide(gid, user_id, arg, op="reject")
        if sub == "取消":
            return await self._cancel(gid, user_id, arg)
        if sub == "领取":
            return await self._claim_delivery(gid, arg)
        # 不认的子命令：看着像派活（和 @ 派活同一份请求词表）就给一行能照抄的 /mw do
        rest = _rest_after_head(raw)
        return do_hint(rest) if has_request_cue(rest) else usage_text(sub)

    # ------------------------------------------------------------------
    # /mw do 要做的事：直接派活（不问 Jev、不调模型），引用一条消息就用它的正文
    # ------------------------------------------------------------------

    async def _do(
        self, gid: str, user_id: str, user_name: str, raw: str, message_id: str, reply_to: str
    ) -> str:
        try:
            existing = self._approvals.by_message(gid, message_id)
        except Exception:
            logger.exception("/mw do 查重失败（群 %s，消息 %s）", gid, message_id)
            existing = None
        if existing:
            return self._do_existing(existing)
        extra = _do_text(raw)
        quoted = await self._quote_text(reply_to)
        if not extra and not quoted:
            return _DO_EMPTY
        job = quoted or extra
        body = f"{quoted}\n\n{extra}" if (quoted and extra) else job
        res = self._approvals.create(
            gid,
            kind="task",
            title=" ".join(job.split())[:_TITLE_MAX],
            quote=body,
            via=_DO_VIA,
            requester_id=user_id,
            requester_name=user_name,
            message_id=message_id,   # 这条 /mw do 自己的消息：收图时靠它拿到引用的那条
            screen=False,            # 明确指令，不过「类似的被拒多了就不再收」
        )
        if not isinstance(res, dict) or not str(res.get("id") or ""):
            raise RuntimeError("approvals.create 没给出请求 id")
        if str(res.get("status") or "") == "pending":
            return _pending_reply(str(res["id"]))
        tids = [str(t) for t in (res.get("task_ids") or []) if str(t)]
        tid = str(res.get("task_id") or "")
        if tid and tid not in tids:
            tids.insert(0, tid)
        for one in tids:
            self._start_task(one)  # 和 /mw 批准 同一条开工路
        if tids:
            return _started_reply("、".join(tids))
        return f"收到，{res['id']} 已批准"

    @staticmethod
    def _do_existing(row: dict) -> str:
        """同一条消息已经派过（热重载重放）：报已有那条，不再建第二条。"""
        rid = str(row.get("id") or "")
        status = str(row.get("status") or "")
        if status == "pending":
            return _pending_reply(rid)
        if status == "approved":
            tid = str(row.get("task_id") or "")
            return _started_reply(tid) if tid else f"收到，{rid} 已批准"
        return f"这条消息已经派过一次（{rid}），不重复建"

    async def _quote_text(self, reply_to: str) -> str:
        """被引用那条消息的正文；宿主取不到 / 出错 / 超时 → 空串（当没引用）。"""
        mid = str(reply_to or "").strip()
        if not mid or self._host is None:
            return ""
        try:
            msg = await asyncio.wait_for(
                self._host.message_by_id(mid, _timeout_s=_QUOTE_TIMEOUT_S),
                timeout=_QUOTE_TIMEOUT_S,
            )
        except Exception:
            logger.warning("/mw do 取被引用消息失败（%s），当作没写正文", mid)
            return ""
        if not isinstance(msg, dict):
            return ""
        return str(msg.get("processed_plain_text") or "").strip()[:_QUOTE_MAX]

    async def _claim_delivery(self, gid: str, task_id: str) -> str:
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
        if outcome == "expired":
            try:
                result = await self._outbox.republish_if_expired(task_id)
            except Exception:
                logger.exception("领取成品时重发链接失败（%s）", task_id)
                result = {}
            local = ""
            public = str(self._get_settings().console.public_url or "").rstrip("/")
            if public:
                row = self._store.read().execute("SELECT token FROM groups WHERE group_id=?", (gid,)).fetchone()
                if row is not None and row["token"]:
                    local = f"；本地副本：{public}/g/{row['token']}/artifacts/{task_id}/"
            if result.get("ok") and result.get("url"):
                return f"{task_id} 的新链接：{result['url']}" + local
            return f"{task_id} 的外链已过期；本地副本在本群网页任务详情里" + local
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
        lines.append(_STATUS_HINT)  # 顺手告诉群友怎么派活、去哪看全部指令
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

    async def _web(self, gid: str) -> str:
        from .share import group_link_async

        try:
            settings = self._get_settings()
        except Exception:
            settings = None
        link = await group_link_async(self._store, settings, gid, tab="news", share=self._share)
        if not link:
            return "网页还没公开：管理员可以打开 here.now 自动公开或填公开网址"
        if not str(getattr(getattr(settings, "console", None), "public_url", "") or ""):
            return f"本群网页：{link}（24 小时内有效）"
        return f"本群网页：{link}"

    # ------------------------------------------------------------------
    # /mw 批准 / 拒绝 [ID]：只限本群批准人
    # ------------------------------------------------------------------

    def _platform_of(self, gid: str) -> str:
        """群所在平台（qq / telegram）：管理员名单按「平台:账号」比对。"""
        try:
            settings = self._get_settings()
            return settings.platform_of(str(gid)) if settings is not None else "qq"
        except Exception:
            return "qq"

    async def _decide(self, gid: str, user_id: str, rid: str, *, op: str) -> str:
        verb = "批准" if op == "approve" else "拒绝"
        try:
            # 只认本群那份名单（全局管理员是种进来的，删了就批不了；不再回全局并集）
            is_approver = bool(
                self._approvals.is_admin(user_id, platform=self._platform_of(gid), group_id=gid)
            )
        except Exception:
            logger.exception("/mw 判批准人失败")
            return "这条指令处理出错了，日志里有"
        if not is_approver:
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
        # 群命令只管本群：先查出这条请求属于哪个群（bot 管理员跨群批也不许）
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
        # 目标贴合（R5）：构想的项目里「要群友自己动手」又没替代任务的会被跳过，回执里说清
        skipped = [s for s in (res.get("skipped") or []) if isinstance(s, dict)]
        skip_note = ""
        if skipped:
            names = "、".join(str(s.get("title") or "")[:12] for s in skipped[:3])
            skip_note = f"；另有 {len(skipped)} 项要群友自己动手，这次没做（{names}）"
        if tids:
            for one in tids:
                self._start_task(one)
            return f"已批准 {rid_s}，已开工（{'、'.join(tids)}）；发 /mw 看进度{skip_note}"
        if goal_id:
            return f"已批准 {rid_s}，目标 {goal_id} 记下了，会定期检查{skip_note}"
        return f"已批准 {rid_s}{skip_note}"

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
            allowed = self._approvals.can_cancel(
                kind, ident_s, user_id, group_role=str(role or ""), group_id=gid
            )
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
