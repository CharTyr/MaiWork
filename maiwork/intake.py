"""收消息钩子逻辑（M1 记信号；M3 @ 识别 + /mw 指令）。

红线（AGENTS.md / docs/02 §2）：
- 非服务群立刻返回 continue，**在读取任何其他字段之前**；零 Jev、零调用、零 Store、零发送。
- 钩子里唯一的慢事：服务群里被 @ 时等 Jev（≤ [jev] timeout_ms，外面再用
  asyncio.wait_for 兜一层）；批准、指令处理、提醒解析这些慢活全部 spawn 到后台。
- 收消息的钩子永不中止：任何异常吞掉，永远返回 {"action": "continue"}。
- 机器人自己的消息不当成请求（只记信号）。
- 群友派的活走 Approvals（要批准的未批准不开工）。

M3 决定的出入（和 docs/07 §11.5 对齐）：
- @ 之后 Jev 判 kind=prepare/goal 且把握 ≥0.6 → 后台 approvals.create，并往可提起清单
  加一句「<名字> 刚才 @ 你派的活已由 MaiWork 接手（等管理员批准 / 已开工）；你不用答应，
  不要自己做」（ttl 30 分钟，key 前缀 request: → delivery 把它排在接龙行之前、优先分配
  预算；放不下按句截断，预算耗尽停止——条目多时不保证这一轮完整注入）。「已开工」是
  免批 / 当场获准的口径，不是子 agent 已在跑。
- kind=reminder 且把握 ≥0.6 → 后台回调 on_reminder(group_id, msg)（app 提供，
  由主模型解析时间后建成员目标）。
- Jev 明确判 none 且把握 ≥0.6 → 什么都不记（这是正常闲聊）。
- Jev 不可用 / 超时 / 失败 / 答案无效 / 把握不够 → 写进 pending_asks 表（不再攒内存、
  不写 intake.slow 事件）；主模型读群提炼时把这批消息标出来逐条判（asks），判过标
  handled。slow_queue 属性返回未处理的行（没接库的极简构造退回内存攒着，兼容老测试）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from . import clock
from .config import Settings, _norm_platform, host_platform
from .host import _parse_reply_to
from .quick_judge import has_request_cue

logger = logging.getLogger("maiwork.intake")

_CONTINUE: Dict[str, Any] = {"action": "continue"}

# kind → 中文说法（Jev 那条路和快速判断兜底共用的同一份口径）
_KIND_ZH: Dict[str, str] = {"prepare": "准备东西", "goal": "帮忙盯着或做成"}

# 内存环：每个服务群留最近几条消息当快速判断的上下文
_RING_MAX = 8
_RING_TEXT_MAX = 120


def _is_command(text: str) -> bool:
    """别的插件的指令（/pic、!remind、#xxx）：不是请 MaiWork 做事（/mw 在前面已单独处理）。"""
    t = str(text or "").lstrip()
    return bool(t) and t.startswith(("/", "／", "!", "！", "#"))

# Jev 选择题（选项文字按任务书原文，别随手改——判准是拿它喂的）
_AT_QUESTIONS: Dict[str, Any] = {
    "kind": {
        "type": "choice",
        "instructions": "群里有人 @ MaiBot，这条消息是在请 MaiBot 做什么？",
        "criteria": {
            "prepare": "请 MaiBot 准备/整理/调研/做一个能在网上做出来的东西（资料、清单、文档等）",
            "goal": "请 MaiBot 在网上帮忙盯着某件事，或把一件它在线上能做成的事做成",
            "reminder": "请 MaiBot 到时间提醒自己",
            "none": "都不是（闲聊、提问、玩笑、整人，或要它办现实里办不到的事，比如改别人的假期、管人）",
        },
    }
}

_CONFIDENCE_MIN = 0.6
# 「准备 / 盯着」直接建待批要更有把握：线上有条玩笑请求以 0.61 混了进去（2026-09-30）。
# 0.6~0.7 之间的进慢路径，让主模型读群时再判（和 Jev 没判出来同一条路）。
_REQUEST_CONFIDENCE_MIN = 0.7
_STATE_TEXT_MAX = 500
_TITLE_MAX = 30
# 可提起清单的存活期
_COMMAND_MENTION_TTL_S = 120        # /mw 指令说明只留 2 分钟
_REQUEST_MENTION_TTL_S = 30 * 60    # 「已由 MaiWork 接手（等批准 / 已开工）」留 30 分钟
_SLOW_QUEUE_MAX = 200               # 慢路径内存队列上限（满了丢最旧的）
# 网页构想详情「复制要求」带的编号（「（构想 #12）」）：认出来就按这条构想建请求，不问 Jev。
# 2026-10：还认「（构想 #12，要做：1、3）」这种挑了项目的写法（尾部不跨右括号 / 换行）。
_IDEA_REF = re.compile(r"构想\s*[#＃]\s*(\d{1,9})([^\n）)]{0,80})")
# 尾部里的「要做：1、3 / 1,3 / 1 3」；分隔符中英文逗号、顿号、空格、点、斜杠都认
_IDEA_WANT = re.compile(r"要做\s*[:：]?\s*([0-9０-９][0-9０-９\s、,，.．/／-]*)")
_IDEA_ITEM_NO_MAX = 24                     # 一次最多认这么多序号（防刷屏）
_IDEA_OPEN_STATES = ("new", "wanted", "dismissed")
_IDEA_BUSY_TEXT = {"pending": "已经在等管理员批准了", "started": "已经在做了"}
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_idea_wanted(text: Any) -> list[int] | None:
    """从「构想 #N」后面的尾巴里读出「要做：1、3」的序号。

    - 没写「要做」→ None（= 这条构想的全部项目）；
    - 写了但一个数字都没有 → None（同样当「全部」，别因为写错就什么都不做）；
    - 序号去重、只留正数、最多 24 个。
    """
    m = _IDEA_WANT.search(str(text or ""))
    if not m:
        return None
    nums: list[int] = []
    for chunk in re.findall(r"[0-9０-９]+", m.group(1).translate(_FULLWIDTH_DIGITS)):
        try:
            n = int(chunk)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in nums:
            nums.append(n)
        if len(nums) >= _IDEA_ITEM_NO_MAX:
            break
    return nums or None


def _ts_of(value: Any) -> float:
    """钩子载荷里的消息时间 → 秒；认不出来给 0.0。

    宿主给的是字符串（message_utils.py `timestamp=str(...timestamp())`，docs/06）；
    以前只认数字，结果每条都记成 0.0，「最近一条消息」只能等后台读消息才更新（最多晚 10 分钟）。
    """
    if isinstance(value, bool):
        return 0.0
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    return ts if math.isfinite(ts) and ts > 0 else 0.0


@dataclass
class Signal:
    """一个服务群自上次取走信号以来的新消息情况。"""

    session_id: str
    last_ts: float
    count: int


class Signals:
    """内存里的「有新消息」信号，给后台循环用。"""

    def __init__(self) -> None:
        self._map: dict[str, Signal] = {}

    def mark(self, group_id: str, session_id: str, ts: float) -> None:
        s = self._map.get(group_id)
        if s is None:
            self._map[group_id] = Signal(session_id=session_id, last_ts=ts, count=1)
        else:
            s.session_id = session_id
            s.last_ts = max(s.last_ts, ts)
            s.count += 1
            # 后到的消息 session_id 以最新为准（同一群一般不变）

    def take(self) -> dict[str, Signal]:
        """取走并清空：群号 -> Signal。"""
        out = self._map
        self._map = {}
        return out

    def last_ts(self, group_id: str) -> float:
        s = self._map.get(group_id)
        return s.last_ts if s is not None else 0.0


class Intake:
    def __init__(
        self,
        get_settings: Callable[[], Settings],
        signals: Signals,
        *,
        jev: Any = None,
        approvals: Any = None,
        mentions: Any = None,
        commands: Any = None,
        on_reminder: Callable[[str, dict], Any] | None = None,
        bot_qq: Callable[[], str] | str | None = None,
        spawn: Callable[[Awaitable[Any]], Any] | None = None,
        store: Any = None,
        on_answer: Callable[[str, str, str], Awaitable[Any]] | None = None,
        waiting_tasks: Callable[[str], list] | None = None,
        bot_account: Callable[[str], str] | None = None,
        models: Any = None,
        quick_judge: Any = None,
        quick_judge_sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._get_settings = get_settings
        self._signals = signals
        self._jev = jev
        self._approvals = approvals
        self._mentions = mentions
        self._commands = commands
        self._on_reminder = on_reminder
        self._bot_qq_source = bot_qq
        # 非 qq 平台的机器人自己账号（平台 → 账号；app 给 Host 缓存的 bot.platforms）
        self._bot_account_source = bot_account
        self._spawn = spawn if spawn is not None else _default_spawn
        self._store = store
        from .news_feedback import CardIndex

        self._card_index = CardIndex()
        # 提问的回答恢复（docs/02 §7.2）：waiting_tasks(群号) 给缓存的
        # [(task_id, question_msg_id, requester_id)]（app 提供，30 秒一刷）；
        # 命中后 spawn on_answer(group_id, task_id, text)（app 挂 coordinator.resume）
        self._on_answer = on_answer
        self._waiting_tasks = waiting_tasks
        # 同一条消息只触发一次：最近触发过的消息 ID（message_id → ts，顺手清旧）
        self._answered_ids: dict[str, float] = {}
        # 慢路径在库里的表是 pending_asks；这个内存队列只给「没接库」的极简构造兜底
        self._slow_queue: list[dict] = []
        # 通过 @ 判断的最近几条消息（内存环，零 I/O）：快速判断兜底要用「这条 @ 之前的
        # 6 条」当上下文；每个服务群一份，只留最近 8 条（机器人自己的也记，标 MaiBot）
        self._recent: dict[str, list[dict]] = {}
        # 派活判断兜底（quick_judge.QuickJudge）：Jev 不在 / 拿不准时用模型快速判一次。
        # 显式注入（测试）优先；只给了 models 就在这里自己建一个。
        self._quick_judge = quick_judge
        if self._quick_judge is None and models is not None:
            try:
                from .quick_judge import QuickJudge

                self._quick_judge = QuickJudge(
                    get_settings,
                    models,
                    store=store,
                    on_request=self._qj_request,
                    on_reminder=on_reminder,
                    on_slow=self._slow,
                    sleep=quick_judge_sleep,
                )
            except Exception:
                logger.exception("建派活判断兜底出错，这次退回慢路径")
                self._quick_judge = None

    # ------------------------------------------------------------------
    # 钩子本体
    # ------------------------------------------------------------------

    async def handle(self, kwargs: dict) -> dict:
        """钩子本体；async；永远返回 {"action": "continue"}。"""
        try:
            group_id = self._group_id_of(kwargs)
            if not group_id:
                return _CONTINUE
            settings = self._get_settings()
            if settings is None or not settings.is_served(group_id):
                return _CONTINUE
            # 群号相同但平台不同（比如 qq 群号撞上别的平台的 ID）不算服务群
            # （qqbot 在宿主里也叫 qq：比宿主平台名；QQ 官方群号是 openid，不会和 SnowLuma 的数字群号撞）
            platform = settings.platform_of(group_id)
            if self._platform_of(kwargs) != host_platform(platform):
                return _CONTINUE
            # 到这一步才允许读消息的其他字段
            if self._is_ignorable(kwargs):
                return _CONTINUE
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return _CONTINUE
            session_id = str(message.get("session_id") or "")
            self._signals.mark(group_id, session_id, _ts_of(message.get("timestamp")))
            await self._m3(settings, group_id, message, platform)
            return _CONTINUE
        except Exception:  # 任何异常都吞掉，钩子永不中止消息
            logger.debug("收消息钩子异常，已吞掉", exc_info=True)
            return _CONTINUE

    # ------------------------------------------------------------------
    # M3：机器人自己 / /mw / @ 识别
    # ------------------------------------------------------------------

    async def _m3(self, settings: Settings, group_id: str, message: dict, platform: str = "qq") -> None:
        user_id, user_name = self._speaker(message)
        message_id = str(message.get("message_id") or "")
        text = str(message.get("processed_plain_text") or "")
        # 0) 内存环：这条之前的那几条（给快速判断兜底当上下文）；当前这条随后再进环，
        #    所以上下文里永远不会出现它自己。机器人自己的消息一样记（标 MaiBot）。
        bot_id = self._bot_id(platform)
        is_bot = bool(bot_id and user_id and user_id == bot_id)
        context = self._ring_context(group_id)
        self._ring_add(group_id, "MaiBot" if is_bot else (user_name or user_id), text, message_id, is_bot)
        # 1) 机器人自己发的：只记信号，不当请求
        if is_bot:
            return
        # 2) /mw 指令：交 commands（spawn 后台），给 MaiBot 留个说明
        parts = text.strip().split(maxsplit=1)
        if parts and parts[0] == "/mw":
            self._add_mention(
                group_id,
                "刚才那条是 MaiWork 指令，已经处理，不用回",
                key=f"cmd-note:{message_id}",
                ttl_s=_COMMAND_MENTION_TTL_S,
            )
            _spawn_safely(self._spawn, self._run_command(group_id, user_id, user_name, text, message_id))
            return
        # 2.5) 回复 / 引用了 MaiWork 发的资讯卡片 → 记一次自动好评（news_feedback；卡片清单 60 秒缓存）
        if self._store is not None:
            reply_to = str(message.get("reply_to") or "").strip() or _parse_reply_to(message.get("raw_message"))
            if reply_to:
                at = _ts_of(message.get("timestamp")) or clock.now()
                self._card_index.on_message(self._store, group_id, user_id, message_id, reply_to, at)
        # 3) 提问的回答（docs/02 §7.2）：回复了 waiting_input/shelved 任务那条提问，
        #    或发起人 @ 机器人而且这个群里只有这一个等待任务 → 后台恢复任务。
        #    纯内存查（app 给的 30 秒缓存），不做重查询。
        self._maybe_answer(group_id, user_id, message_id, text, message)
        # 4) @ MaiBot：钩子里只问 Jev（带超时），其余全部后台
        #    别的插件的指令（/pic 之类）不是 MaiWork 的活，连 Jev 都不问
        if (bool(message.get("is_at")) or bool(message.get("is_mentioned"))) and not _is_command(text):
            await self._handle_at(settings, group_id, user_id, user_name, message_id, text, context)

    async def _run_command(self, group_id: str, user_id: str, user_name: str, text: str, message_id: str) -> None:
        commands = self._commands
        if commands is None:
            return
        try:
            await commands.handle(group_id, user_id, user_name, text, message_id)
        except Exception:
            logger.exception("/mw 指令处理协程出错（群 %s）", group_id)

    def _maybe_answer(
        self, group_id: str, user_id: str, message_id: str, text: str, message: dict
    ) -> None:
        """认出「这是对某条提问的回答」→ spawn on_answer(group_id, task_id, 文本)。

        两条规则（docs/02 §7.2）：
        - 消息的 reply_to（原始消息里的引用，host._parse_reply_to 解析）等于某个
          waiting_input / shelved 任务的 question_msg_id；
        - 发言人就是该任务的请求人（requester_id）、这个群里此人只有一个等待任务、
          且这条消息 @ 了机器人。
        同一条消息只触发一次；拿不到回复字段时只用第二条规则。
        """
        provider = self._waiting_tasks
        cb = self._on_answer
        if provider is None or cb is None:
            return
        text_s = str(text or "").strip()
        if not text_s:
            return
        mid = str(message_id or "")
        if mid and mid in self._answered_ids:
            return
        try:
            waiting = list(provider(group_id) or [])
        except Exception:
            logger.debug("等待任务缓存查询出错（群 %s）", group_id, exc_info=True)
            return
        if not waiting:
            return
        task_id = ""
        # 规则 1：回复了那条提问（消息里有引用段时）
        reply_to = _parse_reply_to(message.get("raw_message"))
        if reply_to:
            for tid, qmid, _rid in waiting:
                qmid_s = str(qmid or "").strip()
                if qmid_s and reply_to == qmid_s:
                    task_id = str(tid)
                    break
        # 规则 2：请求人 @ 机器人 + 这个群里只有一个等待任务
        if not task_id and (bool(message.get("is_at")) or bool(message.get("is_mentioned"))):
            # 按任务数，不按提问条数：同一个任务可能有原提问 + 提醒两条（2026-10-10）
            mine = {str(t[0]) for t in waiting if str(t[2] or "") == str(user_id)}
            if len(mine) == 1:
                task_id = next(iter(mine))
        if not task_id:
            return
        if mid:
            self._answered_ids[mid] = clock.now()
            # 顺手清一清 10 分钟前的（这个表只会按群消息量慢慢长）
            cutoff = clock.now() - 600.0
            stale = [k for k, ts in self._answered_ids.items() if ts < cutoff]
            for k in stale:
                del self._answered_ids[k]
        _spawn_safely(self._spawn, self._run_answer(cb, group_id, task_id, text_s))

    async def _run_answer(
        self,
        cb: Callable[[str, str, str], Awaitable[Any]],
        group_id: str,
        task_id: str,
        text: str,
    ) -> None:
        try:
            result = cb(group_id, task_id, text)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception:
            logger.exception("回答恢复协程出错（群 %s，任务 %s）", group_id, task_id)

    async def _handle_at(
        self,
        settings: Settings,
        group_id: str,
        user_id: str,
        user_name: str,
        message_id: str,
        text: str,
        context: list[dict] | None = None,
    ) -> None:
        # G5：钩子阻塞宿主管线，等 Jev 的时间绝不超 1200 毫秒（配置解析时已夹到
        # [200,1200]，这里 min 再兜一层，防止构造时绕过了 load_settings）
        idea = self._idea_of(group_id, text)
        if idea is not None:
            _spawn_safely(self._spawn, self._create_idea_request(group_id, idea, user_id, user_name, message_id, text))
            return
        timeout_ms = min(1200, max(100, int(getattr(getattr(settings, "jev", None), "timeout_ms", 1500) or 1500)))
        answers: Optional[dict] = None
        jev = self._jev
        if jev is not None:
            try:
                available = bool(jev.available())
            except Exception:
                available = False
            if available:
                state = {"messages": [{"speaker": "USER", "text": text[:_STATE_TEXT_MAX]}]}
                try:
                    answers = await asyncio.wait_for(
                        jev.ask(state, _AT_QUESTIONS, purpose="intake", group_id=group_id, timeout_ms=timeout_ms),
                        timeout=timeout_ms / 1000.0,
                    )
                except Exception:  # 超时 / 客户端异常都走慢路径
                    answers = None
        ctx = {"context": list(context or [])}
        if not isinstance(answers, dict):
            self._fallback("jev_无答案", group_id, user_id, user_name, message_id, text, **ctx)
            return
        raw_kind = answers.get("kind")
        if isinstance(raw_kind, (tuple, list)) and len(raw_kind) >= 3:
            label, prob, confidence = str(raw_kind[0]), raw_kind[1], raw_kind[2]
            try:
                confidence_f = float(confidence)
            except (TypeError, ValueError):
                confidence_f = 0.0
        else:
            label, confidence_f = "", 0.0
        if label in ("prepare", "goal") and confidence_f >= _REQUEST_CONFIDENCE_MIN:
            _spawn_safely(self._spawn, self._create_request(group_id, label, confidence_f, user_id, user_name, message_id, text))
            return
        if label == "reminder" and confidence_f >= _CONFIDENCE_MIN:
            cb = self._on_reminder
            if cb is not None:
                _spawn_safely(self._spawn, self._run_reminder(cb, group_id, user_id, user_name, message_id, text))
            return
        if label == "none" and confidence_f >= _CONFIDENCE_MIN:
            # Jev 明确判成闲聊且把握够：这是正常闲聊，什么都不记（不进慢路径）
            return
        self._fallback(f"jev_判成「{label or '无效'}」（把握 {confidence_f:.2f}）",
                       group_id, user_id, user_name, message_id, text, **ctx)

    # ------------------------------------------------------------------
    # 派活判断兜底（quick_judge）：Jev 不在 / 拿不准时的第二条快路
    # ------------------------------------------------------------------

    @property
    def quick_judge(self) -> Any:
        """派活判断兜底（没接 / 建不起来 = None）；app 停机时收它。"""
        return self._quick_judge

    def _fallback(
        self,
        reason: str,
        group_id: str,
        user_id: str,
        user_name: str,
        message_id: str,
        text: str,
        *,
        context: list[dict] | None = None,
    ) -> None:
        """Jev 判不了的一条 @：先试快速判断兜底，不行再走老慢路径（pending_asks）。

        请求词过滤（keyword_filter）开着、这条 @ 里一个请求词都没有 → 直接当闲聊：
        不调模型，也不写 pending_asks（主模型读群时照样能看见这条消息）。
        """
        qj = self._quick_judge
        if qj is not None and qj.enabled():
            try:
                qjs = getattr(self._get_settings(), "quick_judge", None)
            except Exception:
                qjs = None
            if bool(getattr(qjs, "keyword_filter", False)) and not has_request_cue(text):
                return
            if qj.enqueue(
                group_id,
                {
                    "group_id": str(group_id),
                    "user_id": str(user_id),
                    "user_name": str(user_name),
                    "message_id": str(message_id),
                    "text": str(text or ""),
                },
                context=list(context or []),
            ):
                return
        self._slow(reason, group_id, user_id, user_name, message_id, text)

    def _qj_request(self, group_id: str, label: str, sure: float, item: dict, title: str) -> Awaitable[None]:
        """快速判断判成 prepare / goal：和 Jev 那条路一样建待批，只是 via 说明出处。"""
        zh = _KIND_ZH.get(str(label), str(label))
        return self._create_request(
            group_id,
            str(label),
            float(sure),
            str(item.get("user_id") or ""),
            str(item.get("user_name") or ""),
            str(item.get("message_id") or ""),
            str(item.get("text") or ""),
            via=f"群里 @ · 快速判断是「{zh}」（把握 {float(sure):.2f}）",
            title=str(title or ""),
        )

    async def _run_reminder(
        self,
        cb: Callable[[str, dict], Any],
        group_id: str,
        user_id: str,
        user_name: str,
        message_id: str,
        text: str,
    ) -> None:
        try:
            msg = {
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "message_id": message_id,
                "text": text,
            }
            result = cb(group_id, msg)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception:
            logger.exception("提醒慢路径出错（群 %s）", group_id)

    async def _create_request(
        self,
        group_id: str,
        label: str,
        confidence: float,
        user_id: str,
        user_name: str,
        message_id: str,
        text: str,
        *,
        via: str | None = None,
        title: str | None = None,
    ) -> None:
        approvals = self._approvals
        if approvals is None:
            self._slow("approvals_没就位", group_id, user_id, user_name, message_id, text)
            return
        kind = "task" if label == "prepare" else "goal"
        zh = _KIND_ZH.get(label, label)
        title = (str(title or "").strip()[:_TITLE_MAX]) or (text.strip()[:_TITLE_MAX]) or zh
        via = str(via or "") or f"群里 @ · Jev 判断是「{zh}」（把握 {confidence:.2f}）"
        try:
            res = approvals.create(
                group_id,
                kind=kind,
                title=title,
                quote=text,
                via=via,
                requester_id=user_id,
                requester_name=user_name,
                message_id=message_id,
                screen=True,
            )
        except Exception:
            logger.exception("记待批请求出错（群 %s，消息 %s）", group_id, message_id)
            return
        if not isinstance(res, dict) or not res.get("id"):
            return
        name = user_name or user_id or "有人"
        # 有 id 的返回路径只有 pending / approved 两种（approvals.create）。
        # approved = 免批 / 当场获准：口径是「已获准、交给 MaiWork」，不是子 agent 已在跑。
        state_zh = "等管理员批准" if str(res.get("status") or "") == "pending" else "已开工"
        self._add_mention(
            group_id,
            f"{name} 刚才 @ 你派的活已由 MaiWork 接手（{state_zh}）。"
            f"你不用答应，不要自己做、不要说你来做或给出成品；"
            f"可以只简短说一句「好，交给它了」，也可以不回。",
            key=f"request:{res['id']}",
            ttl_s=_REQUEST_MENTION_TTL_S,
        )

    def _idea_of(self, group_id: str, text: str) -> dict | None:
        """消息里带「构想 #N」且这条构想属于本群 → 构想行（dict，带 wanted）；否则 None。

        `wanted` 是「（构想 #12，要做：1、3）」里点名的项目序号；没写 / 写不出数字 → None
        （= 这条构想的全部项目）。
        """
        if self._store is None:
            return None
        m = _IDEA_REF.search(str(text or ""))
        if not m:
            return None
        try:
            row = self._store.read().execute(
                "SELECT id, title, icon, state FROM ideas WHERE id=? AND group_id=?",
                (int(m.group(1)), str(group_id)),
            ).fetchone()
        except Exception:
            logger.debug("查构想编号失败（群 %s）", group_id, exc_info=True)
            return None
        if row is None:
            return None
        idea = dict(row)
        idea["wanted"] = parse_idea_wanted(m.group(2))
        return idea

    async def _create_idea_request(
        self, group_id: str, idea: dict, user_id: str, user_name: str, message_id: str, text: str
    ) -> None:
        """群里 @ 着发了构想的要求：按这条构想建待批请求（带 idea_id）；在等批准 / 在做的只回一句。"""
        iid = int(idea["id"])
        title = str(idea.get("title") or "构想")
        state = str(idea.get("state") or "new")
        name = user_name or user_id or "有人"
        if state not in _IDEA_OPEN_STATES:
            busy = _IDEA_BUSY_TEXT.get(state, "已经处理过了")
            self._add_mention(
                group_id, f"{name}提的构想「{title[:40]}」{busy}，不用再建", key=f"idea-busy:{iid}:{message_id}",
                ttl_s=_REQUEST_MENTION_TTL_S,
            )
            return
        approvals = self._approvals
        if approvals is None:
            self._slow("approvals_没就位", group_id, user_id, user_name, message_id, text)
            return
        try:
            res = approvals.create(
                group_id,
                kind="task",
                title=title,
                quote=text,
                via=f"群里 @ · 来自构想 #{iid}",
                requester_id=user_id,
                requester_name=user_name,
                message_id=message_id,
                idea_id=iid,
                icon=str(idea.get("icon") or "package"),
                items=idea.get("wanted"),
                source="idea",
            )
        except Exception:
            logger.exception("按构想记待批请求出错（群 %s，构想 %s）", group_id, iid)
            return
        if not isinstance(res, dict) or not res.get("id"):
            return
        if str(res.get("status") or "") == "pending" and self._store is not None:
            try:
                with self._store.tx() as conn:
                    conn.execute(
                        "UPDATE ideas SET state='pending', requested_by=?, updated=?"
                        " WHERE id=? AND state IN ('new', 'wanted', 'dismissed')",
                        (str(name), clock.now(), iid),
                    )
            except Exception:
                logger.exception("构想 #%s 标「等批准」失败", iid)
        state_zh = "等管理员批准" if str(res.get("status") or "") == "pending" else "已开工"
        self._add_mention(
            group_id,
            f"{name} 刚才 @ 你派的活（构想「{title[:40]}」）已由 MaiWork 接手（{state_zh}）。"
            f"你不用答应，不要自己做、不要说你来做或给出成品；"
            f"可以只简短说一句「好，交给它了」，也可以不回。",
            key=f"request:{res['id']}",
            ttl_s=_REQUEST_MENTION_TTL_S,
        )

    # ------------------------------------------------------------------
    # 慢路径 / 小工具
    # ------------------------------------------------------------------

    def _ring_add(self, group_id: str, speaker: str, text: str, message_id: str, is_bot: bool) -> None:
        """记一条进内存环（零 I/O、不落库）；每个群只留最近 _RING_MAX 条。"""
        try:
            ring = self._recent.setdefault(str(group_id), [])
            ring.append(
                {
                    "speaker": str(speaker or "有人"),
                    "text": str(text or "")[:_RING_TEXT_MAX],
                    "message_id": str(message_id or ""),
                    "bot": bool(is_bot),
                }
            )
            if len(ring) > _RING_MAX:
                del ring[: len(ring) - _RING_MAX]
        except Exception:
            logger.debug("记最近消息环出错（群 %s）", group_id, exc_info=True)

    def _ring_context(self, group_id: str) -> list[dict]:
        """这条消息之前的那几条（最多 6 条），给快速判断兜底当上下文。"""
        try:
            return list(self._recent.get(str(group_id), []))[-6:]
        except Exception:
            return []

    def _slow(self, reason: str, group_id: str, user_id: str, user_name: str, message_id: str, text: str) -> None:
        """Jev 判不了的 @ 走慢路径：写进 pending_asks 表（docs/02 §5.1），主模型下次
        读群提炼时逐条判（asks），判过标 handled。不写 intake.slow 事件、不攒内存队列
        （没接库的极简构造才退回内存攒着，兼容老测试）。"""
        gid = str(group_id)
        mid = str(message_id)
        if self._store is None:
            # 没接库：退回内存队列（老行为；上限内丢最旧的）
            entry = {
                "reason": str(reason),
                "ts": clock.now(),
                "group_id": gid,
                "user_id": str(user_id),
                "user_name": str(user_name),
                "message_id": mid,
                "text": str(text or "")[:_STATE_TEXT_MAX],
            }
            self._slow_queue.append(entry)
            if len(self._slow_queue) > _SLOW_QUEUE_MAX:
                del self._slow_queue[: len(self._slow_queue) - _SLOW_QUEUE_MAX]
            return
        try:
            with self._store.tx() as conn:
                conn.execute(
                    "INSERT INTO pending_asks"
                    " (group_id, message_id, user_id, user_name, text, ts, reason, handled)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 0)"
                    " ON CONFLICT(group_id, message_id) DO UPDATE SET"
                    " user_id=excluded.user_id, user_name=excluded.user_name,"
                    " text=excluded.text, ts=excluded.ts, reason=excluded.reason,"
                    " handled=0",
                    (
                        gid, mid, str(user_id), str(user_name),
                        str(text or "")[:_STATE_TEXT_MAX], clock.now(), str(reason),
                    ),
                )
        except Exception:
            logger.debug("写 pending_asks 失败（群 %s）", gid, exc_info=True)

    @property
    def slow_queue(self) -> list[dict]:
        """慢路径队列（只读视图）：接库时是 pending_asks 里还没判的行；没接库是内存攒的。"""
        if self._store is None:
            return list(self._slow_queue)
        try:
            rows = self._store.read().execute(
                "SELECT group_id, message_id, user_id, user_name, text, ts, reason"
                " FROM pending_asks WHERE handled=0 ORDER BY ts ASC, message_id ASC"
            ).fetchall()
            return [
                {
                    "reason": str(r["reason"]),
                    "ts": float(r["ts"] or 0),
                    "group_id": str(r["group_id"]),
                    "user_id": str(r["user_id"]),
                    "user_name": str(r["user_name"]),
                    "message_id": str(r["message_id"]),
                    "text": str(r["text"]),
                }
                for r in rows
            ]
        except Exception:
            logger.debug("读 pending_asks 失败", exc_info=True)
            return []

    def _add_mention(self, group_id: str, text: str, *, key: str, ttl_s: float) -> None:
        mentions = self._mentions
        if mentions is None:
            return
        try:
            mentions.add(group_id, text, key=key, ttl_s=ttl_s)
        except Exception:
            logger.exception("写可提起清单出错（群 %s，key %s）", group_id, key)

    def _bot_id(self, platform: str) -> str:
        """这个平台上机器人自己的账号：qq 用 bot_qq；其它平台问 bot_account，拿不到退回 bot_qq。"""
        if platform != "qq" and self._bot_account_source is not None:
            try:
                acc = str(self._bot_account_source(platform) or "").strip()
            except Exception:
                acc = ""
            if acc:
                return acc
        return self._bot_qq()

    @staticmethod
    def _platform_of(kwargs: Any) -> str:
        """消息的平台（message.platform，退回 message_info.platform）；缺了当 qq（老消息）。"""
        try:
            message = kwargs.get("message") or {}
            p = message.get("platform")
            if not p:
                info = message.get("message_info") or {}
                p = info.get("platform") if isinstance(info, dict) else ""
            return _norm_platform(str(p or "")) or "qq"
        except Exception:
            return "qq"

    def _bot_qq(self) -> str:
        src = self._bot_qq_source
        try:
            if callable(src):
                return str(src() or "").strip()
            return str(src or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _speaker(message: dict) -> tuple[str, str]:
        info = message.get("message_info")
        if not isinstance(info, dict):
            return "", ""
        user_info = info.get("user_info")
        if not isinstance(user_info, dict):
            return "", ""
        uid_raw = user_info.get("user_id")
        user_id = str(uid_raw).strip() if uid_raw is not None else ""
        for key in ("user_nickname", "nickname", "card", "user_name"):
            val = user_info.get(key)
            if isinstance(val, str) and val.strip():
                return user_id, val.strip()
        return user_id, ""

    @staticmethod
    def _group_id_of(kwargs: Any) -> str:
        """只读群号。拿不到/不是服务群格式就返回 ""。"""
        if not isinstance(kwargs, dict):
            return ""
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return ""
        info = message.get("message_info")
        if not isinstance(info, dict):
            return ""
        group = info.get("group_info")
        if not isinstance(group, dict):
            return ""
        gid = group.get("group_id")
        if isinstance(gid, (int, float)):
            gid = str(int(gid))
        if not isinstance(gid, str):
            return ""
        return gid.strip()

    @staticmethod
    def _is_ignorable(kwargs: Any) -> bool:
        try:
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return True
            if bool(message.get("is_notify")):
                return True
            mid = message.get("message_id")
            mid_s = str(mid) if mid is not None else ""
            if mid_s.startswith("notice:"):
                return True
            return False
        except Exception:
            return True  # 判不了就当不用记，反正不能炸


def _spawn_safely(spawner: Callable[[Awaitable[Any]], Any], coro: Awaitable[Any]) -> None:
    """调用注入的 spawn；spawn 本身出错（一般是没事件循环）→ 兜底 / 关协程，绝不外抛。"""
    try:
        spawner(coro)
    except Exception:
        logger.debug("spawn 回调出错", exc_info=True)
        _default_spawn(coro)


def _default_spawn(coro: Awaitable[Any]) -> None:
    """没注入 spawn 时的兜底：丢进当前事件循环；没有循环就关掉协程防告警。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _close_quietly(coro)
        return
    try:
        task = loop.create_task(coro)
        # M9：后台协程的异常要有处看
        task.add_done_callback(_log_silent_task_exception)
    except Exception:
        logger.exception("兜底 spawn 出错，协程丢弃")
        _close_quietly(coro)


def _log_silent_task_exception(task: asyncio.Task) -> None:
    """M9：这条链路的 create_task 没有人收结果，cancelled 之外的异常记日志。"""
    try:
        if task.cancelled():
            return
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        exc = None
    if exc is not None:
        logger.exception("收消息链路的后台协程出错：%s", exc, exc_info=exc)


def _close_quietly(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
