"""scheduler.py（M2，docs/07-代码接口.md §10.5）：什么时辰该干什么事。

- 资讯：`[feeds] news_slots` 每个时段加固定随机偏移（按「群号+日期+时段」播种，可复现），
  范围 ±`news_jitter_minutes`；实际时刻起 **45 分钟内**算数，错过不补。
- 构想：今天没出过、北京时间 10:00–21:00、群安静 ≥ 10 分钟（last_msg_ts 不知道就当安静）。
-（主动提目标 2026-10 docs/18 第一步删了：没讲过几次、想由成员自己提或由专岗
   调查后手动派——kv["sched.<群号>.goal"] 历史标记留着不读）。
- 睡觉时段（每群一份 `kv["group_push.<群号>"].quiet_hours`；`[delivery] quiet_hours`
  只作新群的迁移种子，不是第二来源）什么都不返回。
- 群画像没成形（groups.profile_ready_ts == 0，含库里没这个群）→ 永远空。

状态存 kv：
- `sched.<群号>.news` = 已做过的 "日期|slot" 列表，只留（按 done 时刻算的）今天和昨天；
- `sched.<群号>.idea` = 做过的日期（北京时间 YYYY-MM-DD）。构想位子满时 app 不开长活、
  也不记这一笔（暂时受阻 ≠ 做过），10 分钟后重查（app._idea_has_room）。

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
_DONE_KEY_FMT = "{}|{}"           # done 标记：「日期|slot」


class Scheduler:
    def __init__(self, store: Store, get_settings: Callable[[], Settings]) -> None:
        self._store = store
        self._get_settings = get_settings

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    def due(self, group_id: str, now: float, *, last_msg_ts: float = 0.0) -> List[str]:
        """这个时刻该做的事：返回子集 ["news", "idea"]（主动提目标 2026-10 已删）。"""
        gid = str(group_id)
        now = float(now)
        if not self._ready(gid):
            return []
        settings = self._get_settings()
        if self._in_quiet(gid, now):
            return []
        out: list[str] = []
        if self._news_due(gid, settings, now):
            out.append("news")
        if self._idea_due(gid, now, float(last_msg_ts or 0.0)):
            out.append("idea")
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

    def _in_quiet(self, gid: str, now: float) -> bool:
        """now 在不在**这个群**的睡觉时段（唯一真源：group_push.<群号>.quiet_hours）。

        0.8.0：`[delivery] quiet_hours` 只是新群第一次的迁移种子，不再当第二来源——
        一个群改了睡觉时段，不许连带按住别的群。读不到那份设置就按「没限制」走
        （老行为；真正的节制在发件箱 / 模块里还有一层，别在配置抖动时把所有群静音）。
        """
        from . import group_push

        try:
            settings = self._get_settings()
            cfg = group_push.get_config(self._store, str(gid), settings)
            quiet = str(cfg.get("quiet_hours") or "")
        except Exception:
            logger.exception("读每群睡觉时段出错（群 %s），这轮按不限制处理", gid)
            return False
        try:
            rng = clock.parse_hhmm_range(quiet)
        except (ValueError, AttributeError):
            return False
        s, e = rng
        return s != e and clock.in_range(now, rng)

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
