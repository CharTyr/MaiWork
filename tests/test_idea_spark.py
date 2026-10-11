"""话头火花（docs/18 §十一，2026-10-11 用户拍板）：群里明显有人为难 / 想要时，不等排程也出一次构想。

契约：
- `feeds.idea_spark(text)` 纯函数：短于 6 字、指令（/…）不算；命中线索词（怎么办 / 搞不定 /
  求推荐 / 在等…补货 / 谁懂……）算。机器人的消息、@ 机器人的请求由调用方挡掉。
- intake：服务群里普通发言命中线索 → 记一笔内存火花（零库写）；非服务群 / 指令 / @ 机器人不记。
- app 排程：2 小时内有火花、今天还没火花过、离上一条群面构想 ≥6 小时、画像成形、有位子、
  没有在跑的 idea 长活 → spawn 一次 make_idea 并认领（清火花 + 记当天）；认领过当天不再触发。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import idea_spark
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeFeeds, FakeScheduler, hook_message
from test_feeds import GID, _make_feeds, _seed_idea

NOW = 1_790_000_000.0
G1 = "900000001"
G2 = "123456789"


# ----------------------------------------------------------------------
# 线索函数（纯函数）
# ----------------------------------------------------------------------


class TestIdeaSpark:
    @pytest.mark.parametrize(
        "text",
        [
            "这手柄还能救活吗，怎么办",
            "板子接线搞不定，谁有图",
            "编译一直报错，卡在这一步了",
            "求推荐一款便宜的烙铁",
            "有没有人用过这块板子",
            "想买一个热风枪",
            "在等下一批补货",
            "到底哪里买靠谱",
            "这两个型号值不值这个价",
            "选哪个版本好",
            "每次接线都好麻烦",
            "这软件装不上烦死了",
            "这个驱动不会弄",
            "示波器怎么弄才能测出来",
            "这步到底怎么搞",
            "有人吗求助一下",
            "有啥办法能省点钱",
            "谁懂这个错误码",
        ],
    )
    def test_cue_hits(self, text: str) -> None:
        assert idea_spark(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "在吗",
            "怎么办",          # 少于 6 字
            "哈哈哈哈哈",
            "今天天气不错，出去走走",
            "/mw 怎么办才好",   # 指令
            "  卡住  ",         # 去掉空白后不到 6 字
        ],
    )
    def test_no_cue(self, text) -> None:
        assert idea_spark(text) is False


# ----------------------------------------------------------------------
# intake：记内存火花（零库写）
# ----------------------------------------------------------------------


def _settings(serve=(G1,)):
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
        }
    )
    return settings


def _intake(tmp_path, *, serve=(G1,), on_spark=None):
    store = Store(tmp_path / "i.db")
    store.migrate()
    intake = Intake(
        lambda: _settings(serve),
        Signals(),
        store=store,
        spawn=lambda c: c.close(),  # 后台协程不跑，但别留下没 await 的告警
        on_spark=on_spark,
    )
    return intake, store


class TestIntakeSpark:
    @pytest.mark.asyncio
    async def test_served_group_plain_message_is_noted(self, tmp_path) -> None:
        noted: list[tuple[str, float]] = []
        intake, _store = _intake(tmp_path, on_spark=lambda gid, ts: noted.append((gid, ts)))
        await intake.handle(
            hook_message(group_id=G1, text="这手柄还能救活吗，怎么办", message_id="m-1")
        )
        assert noted == [(G1, NOW)]

    @pytest.mark.asyncio
    async def test_not_served_group_not_noted(self, tmp_path) -> None:
        noted: list = []
        intake, _store = _intake(tmp_path, serve=(), on_spark=lambda gid, ts: noted.append((gid, ts)))
        await intake.handle(hook_message(group_id=G1, text="这手柄怎么办啊", message_id="m-2"))
        assert noted == []

    @pytest.mark.asyncio
    async def test_command_not_noted(self, tmp_path) -> None:
        noted: list = []
        intake, _store = _intake(tmp_path, on_spark=lambda gid, ts: noted.append((gid, ts)))
        await intake.handle(hook_message(group_id=G1, text="/mw 这手柄怎么办", message_id="m-3"))
        assert noted == []

    @pytest.mark.asyncio
    async def test_at_bot_not_noted(self, tmp_path) -> None:
        """@ 机器人的话是派活请求，走 intake 自己的路，不算话头。"""
        noted: list = []
        intake, _store = _intake(tmp_path, on_spark=lambda gid, ts: noted.append((gid, ts)))
        await intake.handle(
            hook_message(group_id=G1, text="这手柄怎么办啊", is_at=True, message_id="m-4")
        )
        assert noted == []

    @pytest.mark.asyncio
    async def test_plain_message_without_cue_not_noted(self, tmp_path) -> None:
        noted: list = []
        intake, _store = _intake(tmp_path, on_spark=lambda gid, ts: noted.append((gid, ts)))
        await intake.handle(
            hook_message(group_id=G1, text="今天大家都挺好啊", message_id="m-5")
        )
        assert noted == []

    @pytest.mark.asyncio
    async def test_without_callback_nothing_breaks(self, tmp_path) -> None:
        intake, _store = _intake(tmp_path)
        await intake.handle(hook_message(group_id=G1, text="这手柄怎么办啊", message_id="m-6"))


# ----------------------------------------------------------------------
# feeds 侧：2 小时窗口 / 每天一次 / 6 小时间隔 / 画像成形
# ----------------------------------------------------------------------


class TestSparkGate:
    def _real(self, tmp_path):
        store, settings, feeds, *_r = _make_feeds(tmp_path)
        return store, feeds

    def test_no_spark_or_too_old(self, tmp_path) -> None:
        _store, feeds = self._real(tmp_path)
        assert feeds.spark_ready(GID, NOW) is False
        feeds.note_spark(GID, NOW - 10)
        assert feeds.spark_ready(GID, NOW) is True
        feeds.note_spark(GID, NOW - 3 * 3600)
        assert feeds.spark_ready(GID, NOW) is False

    def test_one_spark_run_per_day(self, tmp_path) -> None:
        store, feeds = self._real(tmp_path)
        feeds.note_spark(GID, NOW)
        assert feeds.spark_ready(GID, NOW) is True
        feeds.spark_taken(GID, NOW)
        # 内存火花清掉了
        assert feeds.spark_ready(GID, NOW) is False
        # 同一天再来一条火花也不出（kv 记了当天）
        feeds.note_spark(GID, NOW + 600)
        assert feeds.spark_ready(GID, NOW + 600) is False
        assert store.kv_get(f"ideas.spark_day.{GID}") == clock.day_key(NOW)
        # 第二天（同一条火花 2 小时内）：可以
        tomorrow = NOW + 86400.0
        feeds.note_spark(GID, tomorrow - 60)
        assert feeds.spark_ready(GID, tomorrow) is True

    def test_six_hour_gap_since_last_group_idea(self, tmp_path) -> None:
        store, feeds = self._real(tmp_path)
        feeds.note_spark(GID, NOW)
        iid = _seed_idea(store, GID, title="一小时前出的", created=NOW - 3600)
        assert feeds.spark_ready(GID, NOW) is False
        with store.tx() as conn:
            conn.execute("UPDATE ideas SET created=? WHERE id=?", (NOW - 7 * 3600, iid))
        assert feeds.spark_ready(GID, NOW) is True

    def test_personal_idea_does_not_block_spark(self, tmp_path) -> None:
        """个人向构想只给管理员看，不占群里的构想节奏。"""
        store, feeds = self._real(tmp_path)
        feeds.note_spark(GID, NOW)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, created, updated, target_user_id)"
                " VALUES (?, '给他的点子', 'b', ?, ?, '10001')",
                (GID, NOW - 600, NOW - 600),
            )
        assert feeds.spark_ready(GID, NOW) is True

    def test_profile_not_ready_no_spark(self, tmp_path) -> None:
        store, feeds = self._real(tmp_path)
        feeds.note_spark(GID, NOW)
        with store.tx() as conn:
            conn.execute("UPDATE groups SET profile_ready_ts=0 WHERE group_id=?", (GID,))
        assert feeds.spark_ready(GID, NOW) is False

    def test_spark_state_is_per_group(self, tmp_path) -> None:
        _store, feeds = self._real(tmp_path)
        feeds.note_spark(GID, NOW)
        assert feeds.spark_ready(GID, NOW) is True
        assert feeds.spark_ready("999", NOW) is False


# ----------------------------------------------------------------------
# app 排程：一次触发、认领一次、有位子 / 没在跑
# ----------------------------------------------------------------------


class _SparkFeeds(FakeFeeds):
    """火花那几口转发给真 Feeds（真库 / 真 kv 日记），make_idea 还是假的。"""

    def __init__(self, real) -> None:
        super().__init__()
        self._real = real

    def note_spark(self, group_id: str, ts=None) -> None:
        self._real.note_spark(group_id, ts)

    def spark_ready(self, group_id: str, now: float) -> bool:
        return bool(self._real.spark_ready(group_id, now))

    def spark_taken(self, group_id: str, now: float) -> None:
        self.spark_taken_calls.append(str(group_id))
        self._real.spark_taken(group_id, now)


def _spark_app(tmp_path, feeds, scheduler):
    from test_app import _app

    app = _app(tmp_path)
    app.feeds_factory = lambda *a, **kw: feeds
    app.scheduler_factory = lambda *a, **kw: scheduler
    return app


async def _wait_until(pred, tries: int = 60) -> None:
    import asyncio

    for _ in range(tries):
        if pred():
            break
        await asyncio.sleep(0.02)


class TestSchedulerSpark:
    @pytest.mark.asyncio
    async def test_spark_spawns_idea_and_claims_once_per_day(self, tmp_path) -> None:
        import asyncio

        _store, _settings, real, *_r = _make_feeds(tmp_path)
        # 火花的群（G2）在真 Feeds 那个库里也要是「画像已成形」的服务群
        with _store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
                (G2, 1_700_000_000.0),
            )
        feeds = _SparkFeeds(real)
        scheduler = FakeScheduler()
        app = _spark_app(tmp_path, feeds, scheduler)
        await app.start()
        try:
            feeds.note_spark(G2, clock.now())
            await app.run_loop_once()
            await _wait_until(lambda: G2 in feeds.make_idea_calls)
            assert feeds.make_idea_calls == [G2]
            assert feeds.spark_taken_calls == [G2]
            # 同一天再蹦一条火花：不再出（真 Feeds 的当天标记挡住了）
            await asyncio.sleep(0.05)
            feeds.note_spark(G2, clock.now())
            await app.run_loop_once()
            await asyncio.sleep(0.1)
            assert feeds.make_idea_calls == [G2]
            assert feeds.spark_taken_calls == [G2]
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_no_room_leaves_spark_for_later(self, tmp_path) -> None:
        feeds = FakeFeeds()
        feeds.spark_ready_map[G2] = True
        checks: list[str] = []

        def _room(gid: str) -> bool:
            checks.append(gid)
            return False

        feeds.idea_room = _room  # type: ignore[attr-defined]
        scheduler = FakeScheduler()
        app = _spark_app(tmp_path, feeds, scheduler)
        await app.start()
        try:
            await app.run_loop_once()
            await _wait_until(lambda: checks, tries=5)
            assert feeds.make_idea_calls == []
            # 没位子不算「火花用掉了」：没认领，火花留着等腾位子
            assert feeds.spark_taken_calls == []
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_running_idea_job_blocks_a_second_spark(self, tmp_path) -> None:
        import asyncio

        feeds = FakeFeeds()
        feeds.spark_ready_map[G2] = True
        feeds.delay_s = 0.3
        scheduler = FakeScheduler()
        scheduler.due_map[G2] = [["idea"]]
        app = _spark_app(tmp_path, feeds, scheduler)
        await app.start()
        try:
            await app.run_loop_once()
            await _wait_until(lambda: G2 in feeds.make_idea_calls)
            assert feeds.spark_taken_calls == []  # 火花这次没轮到（idea 长活正占着）
            await app.run_loop_once()
            await asyncio.sleep(0.05)
            assert feeds.make_idea_calls == [G2]
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_manual_idea_in_flight_blocks_spark(self, tmp_path) -> None:
        """管理员手动点的那一轮还在跑：火花也不该再叠一轮（和到点排程同一口径）。"""
        import asyncio

        feeds = FakeFeeds()
        feeds.spark_ready_map[G2] = True
        scheduler = FakeScheduler()
        app = _spark_app(tmp_path, feeds, scheduler)
        await app.start()
        try:
            app._running_jobs.add((G2, "idea_manual"))
            await app.run_loop_once()
            await asyncio.sleep(0.05)
            assert feeds.make_idea_calls == []
            assert feeds.spark_taken_calls == []  # 没认领，火花留着
        finally:
            app._running_jobs.discard((G2, "idea_manual"))
            await app.stop()
