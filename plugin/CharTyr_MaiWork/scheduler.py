"""scheduler.py（M2，docs/07-代码接口.md §10.5）：什么时辰该干什么事。

- 资讯：`[feeds] news_slots` 每个时段加固定随机偏移（按「群号+日期+时段」播种，可复现），
  范围 ±`news_jitter_minutes`；实际时刻起 **45 分钟内**算数，错过不补。
- 构想：今天没出过、北京时间 10:00–21:00、群安静 ≥ 10 分钟（last_msg_ts 不知道就当安静）。
- 主动提目标（goal）：今天没跑过、北京时间 19:00–21:00、`[goals] propose` 开着才报
  （跑不跑、提不提由 GoalProposer 自己决定）。
- 睡觉时段（`[delivery] quiet_hours`）什么都不返回。
- 群画像没成形（groups.profile_ready_ts == 0，含库里没这个群）→ 永远空。

状态存 kv：
- `sched.<群号>.news` = 已做过的 "日期|slot" 列表，只留（按 done 时刻算的）今天和昨天；
- `sched.<群号>.idea` = 做过的日期（北京时间 YYYY-MM-DD）；
- `sched.<群号>.goal` = 做过的日期（同 idea）。

全部只读 kv / groups，不碰模型、不发消息；所有时间参数由调用方给（方便测试）。
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable, List

from . import clock
from .config import Settings
from .store import Store

logger = logging.getLogger("maiwork.scheduler")

_NEWS_WINDOW_S = 45 * 60          # 实际时刻后 45 分钟内有效，错过不补
_IDEA_QUIET_S = 10 * 60           # 群安静至少 10 分钟才出构想
_IDEA_WINDOW = (10 * 60, 21 * 60)  # 构想只在北京时间 [10:00, 21:00) 出
_GOAL_WINDOW = (19 * 60, 21 * 60)  # 主动提目标只在北京时间 [19:00, 21:00)（一天一次）
_DONE_KEY_FMT = "{}|{}"           # done 标记：「日期|slot」


class Scheduler:
    def __init__(self, store: Store, get_settings: Callable[[], Settings]) -> None:
        self._store = store
        self._get_settings = get_settings

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    def due(self, group_id: str, now: float, *, last_msg_ts: float = 0.0) -> List[str]:
        """这个时刻该做的事：返回子集 ["news", "idea", "goal"]；不排序要求时按 news 在前。"""
        gid = str(group_id)
        now = float(now)
        if not self._ready(gid):
            return []
        settings = self._get_settings()
        if self._in_quiet(settings, now):
            return []
        out: list[str] = []
        if self._news_due(gid, settings, now):
            out.append("news")
        if self._idea_due(gid, now, float(last_msg_ts or 0.0)):
            out.append("idea")
        if self._goal_due(gid, settings, now):
            out.append("goal")
        return out

    def done(self, group_id: str, job: str, now: float) -> None:
        """记一笔「做过了」；未知的 job 直接忽略。"""
        gid = str(group_id)
        now = float(now)
        day = clock.day_key(now)
        with self._store.tx() as conn:
            if job == "news":
                slot = self._nearest_slot(gid, now)
                key = f"sched.{gid}.news"
                marks = self._store.kv_get(key, [])
                if not isinstance(marks, list):
                    marks = []
                marker = _DONE_KEY_FMT.format(day, slot)
                today_key = self._day_key_offset(now, 0)
                yesterday_key = self._day_key_offset(now, -1)
                marks = [str(m) for m in marks if str(m).split("|", 1)[0] in (today_key, yesterday_key)]
                if marker not in marks:
                    marks.append(marker)
                self._store.kv_set(conn, key, marks)
            elif job == "idea":
                self._store.kv_set(conn, f"sched.{gid}.idea", day)
            elif job == "goal":
                self._store.kv_set(conn, f"sched.{gid}.goal", day)
            else:
                logger.warning("Scheduler.done 收到不认识的事：%r（群 %s）", job, gid)

    def next_news_ts(self, group_id: str, now: float) -> float | None:
        """下一个资讯时段的**实际**时刻（含固定偏移）；没配时段返回 None。"""
        gid = str(group_id)
        settings = self._get_settings()
        slots = self._valid_slots(settings)
        if not slots:
            return None
        now = float(now)
        future: list[float] = []
        for delta_days in range(3):  # 今天、明天、后天兜底（时段整天都配在已经很晚也想得出下一个）
            probe = now + delta_days * 86400.0
            for slot in slots:
                actual = self.slot_ts(gid, probe, slot)
                if actual > now:
                    future.append(actual)
            if future:
                break
        return min(future) if future else None

    def slot_ts(self, group_id: str, now: float, slot: str) -> float:
        """某群、now 那一天、某时段的**实际**备料时刻（时段 + 固定偏移）。

        偏移 = random.Random(f"{群号}:{日期}:{slot}").uniform(-jitter, +jitter) 分钟。
        同一个 (群号, 日期, slot) 无论传哪个 now 进来，算出来都是同一个时刻。
        """
        gid = str(group_id)
        settings = self._get_settings()
        jitter = max(0, int(getattr(settings.feeds, "news_jitter_minutes", 30)))
        day = clock.day_key(float(now))
        rng = random.Random(f"{gid}:{day}:{slot}")
        offset_min = rng.uniform(-jitter, jitter)
        base_dt = clock.bj(float(now)).replace(
            hour=int(slot.split(":", 1)[0]),
            minute=int(slot.split(":", 1)[1]),
            second=0,
            microsecond=0,
        )
        return base_dt.timestamp() + offset_min * 60.0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ready(self, gid: str) -> bool:
        row = self._store.read().execute(
            "SELECT profile_ready_ts FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        return row is not None and float(row["profile_ready_ts"] or 0) > 0

    def _in_quiet(self, settings: Settings, now: float) -> bool:
        try:
            rng = clock.parse_hhmm_range(str(settings.delivery.quiet_hours))
        except (ValueError, AttributeError):
            return False
        return clock.in_range(now, rng)

    def _valid_slots(self, settings: Settings) -> list[str]:
        out: list[str] = []
        for raw in getattr(settings.feeds, "news_slots", ()) or ():
            s = str(raw).strip()
            try:
                hh_s, mm_s = s.split(":", 1)
                hh, mm = int(hh_s), int(mm_s)
            except (ValueError, AttributeError):
                continue
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                out.append(f"{hh:02d}:{mm:02d}")
        return out

    def _news_due(self, gid: str, settings: Settings, now: float) -> bool:
        marks = self._store.kv_get(f"sched.{gid}.news", [])
        if not isinstance(marks, list):
            marks = []
        done_today = {str(m) for m in marks}
        today_key = clock.day_key(now)
        for slot in self._valid_slots(settings):
            actual = self.slot_ts(gid, now, slot)
            if not (actual <= now < actual + _NEWS_WINDOW_S):
                continue
            if _DONE_KEY_FMT.format(today_key, slot) in done_today:
                continue
            return True
        return False

    def _idea_due(self, gid: str, now: float, last_msg_ts: float) -> bool:
        t = clock.bj(now)
        minute = t.hour * 60 + t.minute
        if not (_IDEA_WINDOW[0] <= minute < _IDEA_WINDOW[1]):
            return False
        if last_msg_ts > 0 and now - last_msg_ts < _IDEA_QUIET_S:
            return False
        done_day = self._store.kv_get(f"sched.{gid}.idea")
        return done_day != clock.day_key(now)

    def _goal_due(self, gid: str, settings: Settings, now: float) -> bool:
        """主动提目标：只在北京时间 [19:00, 21:00)、当天还没跑过、开关开着才报。

        开关读合并后的有效值（网页规则覆盖，rules.py）：关掉起下一轮就不提。
        """
        goals_cfg = getattr(settings, "goals", None)
        if goals_cfg is not None and not bool(getattr(goals_cfg, "propose", True)):
            return False
        t = clock.bj(now)
        minute = t.hour * 60 + t.minute
        if not (_GOAL_WINDOW[0] <= minute < _GOAL_WINDOW[1]):
            return False
        done_day = self._store.kv_get(f"sched.{gid}.goal")
        return done_day != clock.day_key(now)

    def _nearest_slot(self, gid: str, now: float) -> str:
        """done 时不知道具体是哪个 slot，就记在「实际时刻离 now 最近」的那个时段上。"""
        settings = self._get_settings()
        slots = self._valid_slots(settings)
        if not slots:
            return ""
        best = slots[0]
        best_dist = float("inf")
        for slot in slots:
            dist = abs(self.slot_ts(gid, now, slot) - now)
            if dist < best_dist:
                best, best_dist = slot, dist
        return best

    @staticmethod
    def _day_key_offset(now: float, days: int) -> str:
        return clock.day_key(clock.bj(now).timestamp() + days * 86400.0)
