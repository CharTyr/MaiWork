"""构想 0.8.0：少而精（make_idea）+ 7 天没人理自动收起（shelve_ignored_ideas）。

两份契约（docs/18「构想少而精：把握不大就不出；7 天没人理自动收起」）：

A. make_idea 少而精
   - 提示词明说：想不到值得做的 / 把握不大就给 null，不要为了凑数硬出一条；
     提之前自己过三关：群里真的有人要它、我真做得到、风险和代价说得出。
   - 「值得做的把握」是**可选**字段 worth（high / medium / low，可省略）：
     low = 模型自己都觉得可做可不做 → 这轮不出；老格式没有它照常入库（不破坏现有形状）。
   - 输出 {"idea": null} / 只有 skip / 整个是 null / idea 不是表 → 安静返回 None，
     不抛错、不落库、不进话题候选池。
   - 堆积闸：本群还没处理掉的（new / wanted / pending）已经有 3 条 → 这轮不再造新的
     （一条模型调用都不发）；已开工 / 已收起 / 已落任务的不占位子；个人向的不算；
     闸只读不改，pending 绝不被顺手覆盖。

B. shelve_ignored_ideas(group_id, now?)
   - 只服务配置里的群；非服务群一行都不写。
   - 7 天按 created 算（喜欢点击只动 up / down，不算「有人处理」；updated 不参与判断）。
   - 只收 state=new / wanted，且没有 task_id、没有 pending / approved 的关联待批请求；
     等批准（state=pending）、已开工（started）、已收起（dismissed）、个人向的一律不动。
   - 只改状态不删行、幂等；每条记 idea.auto_shelved 事件；不碰专岗学习、不跨群。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds
from CharTyr_MaiWork.maiwork.store import Store

from fakes import FakeModelsQueue, FakeProfiles

NOW = 1_790_000_000.0
DAY = 86400.0
GID = "111"
GID_OTHER = "222"
_SERVED = {"groups": {"serve": [{"group": "qq:111"}, {"group": "qq:222"}]}}
_ONLY_OTHER = {"groups": {"serve": [{"group": "qq:222"}]}}


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _settings(cfg: dict | None = None) -> Any:
    settings, _problems = load_settings(cfg or {})
    return settings


def _run(coro):
    return asyncio.run(coro)


class _TimePatch:
    """临时把 feeds.clock.now 固定成 NOW。"""

    def __enter__(self):
        import CharTyr_MaiWork.maiwork.feeds as feeds_mod

        self._mod = feeds_mod
        self._orig = feeds_mod.clock.now
        feeds_mod.clock.now = lambda: NOW
        return self

    def __exit__(self, *exc):
        self._mod.clock.now = self._orig


class FakeTopics:
    """只记 add_candidate 的假 Topics。"""

    def __init__(self) -> None:
        self.calls: List[dict] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


class _RecordingSpecialists:
    """自动收起绝不该碰专岗：一碰就记名字 + 炸，顺带守住「不借沉默写负面规矩」。"""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"这条路径不该碰专岗（{name}）")


def _feeds(
    tmp_path,
    *,
    models: Any = None,
    cfg: dict | None = None,
    ready_groups: tuple = (GID,),
    get_settings: Any = None,
) -> tuple:
    """一份带服务群配置的 Feeds（make_idea 要看画像是否成形 → groups 行要有 profile_ready_ts）。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    for gid in ready_groups:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
                (gid, 1_700_000_000.0),
            )
    settings = _settings(cfg)
    models = models if models is not None else FakeModelsQueue(ready=True)
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [
        {"category": "ongoing", "text": "在做开源硬件项目"},
        {"category": "interest", "text": "本地大模型"},
    ]
    topics = FakeTopics()
    feeds = Feeds(
        store, models, None, profiles, topics,
        get_settings if get_settings is not None else (lambda: settings),
    )
    return store, feeds, models, topics


def _plain_idea() -> dict:
    return {
        "title": "我可以帮群把每周讨论整理成一页",
        "body": "每周自动汇总一次",
        "basis": "群里每周都在复盘",
        "icon": "books",
        "chat_worthy": False,
        # 合格样例（docs/18 §八；_feeds() 不接 workers → 基本能力 search/chat/write/watch）
        "feasibility": {"level": "ok", "note": "能做",
                        "uses": ["search", "write"], "deliver": "doc", "needs_members": False},
    }


_IDEA_PLAIN = json.dumps({"idea": _plain_idea()}, ensure_ascii=False)


def _seed_idea(
    store: Store,
    *,
    gid: str = GID,
    title: str = "我可以做个小工具",
    state: str = "new",
    created: float = NOW,
    updated: float | None = None,
    task_id: str | None = None,
    target_user_id: str = "",
    requested_by: str | None = None,
) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
            " requested_by, task_id, up, down, created, updated, target_user_id)"
            " VALUES (?, 'bulb', ?, 'b', 's', '', '', ?, ?, ?, 0, 0, ?, ?, ?)",
            (
                gid, title, state, requested_by, task_id,
                created, created if updated is None else updated, target_user_id,
            ),
        )
        return int(cur.lastrowid or 0)


def _seed_request(store: Store, idea_id: int, status: str, *, gid: str = GID, rid: str = "R-1") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO requests (id, group_id, kind, title, quote, via, icon, requester_id,"
            " requester_name, message_id, idea_id, status, created, updated)"
            " VALUES (?, ?, 'task', '标题', '', '群里 @ · 来自构想', 'package', 'u1', '阿柒', '', ?, ?, ?, ?)",
            (rid, gid, int(idea_id), status, NOW, NOW),
        )


def _row(store: Store, idea_id: int) -> Any:
    return store.read().execute("SELECT * FROM ideas WHERE id=?", (int(idea_id),)).fetchone()


def _events(store: Store, kind: str) -> list:
    return store.read().execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()


# ----------------------------------------------------------------------
# B. shelve_ignored_ideas：7 天没人理自动收起
# ----------------------------------------------------------------------


class TestShelveIgnoredIdeas:
    def test_seven_day_boundary_old_wanted_and_updated_untouched(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        at_cutoff = _seed_idea(store, title="刚好七天", created=NOW - 7 * DAY)
        just_inside = _seed_idea(
            store, title="还差一分钟", state="wanted", created=NOW - 7 * DAY + 60
        )
        older = _seed_idea(store, title="八天没人理", created=NOW - 8 * DAY)

        got = feeds.shelve_ignored_ideas(GID, NOW)

        assert got == [at_cutoff, older]
        assert _row(store, at_cutoff)["state"] == "dismissed"
        assert _row(store, older)["state"] == "dismissed"
        assert float(_row(store, at_cutoff)["updated"]) == pytest.approx(NOW)
        # 没到 7 天的（含存量 wanted）不动，updated 也不许被顺手重写
        assert _row(store, just_inside)["state"] == "wanted"
        assert float(_row(store, just_inside)["updated"]) == pytest.approx(NOW - 7 * DAY + 60)

    def test_default_now_and_never_cross_group(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED, ready_groups=(GID, GID_OTHER))
        mine = _seed_idea(store, title="本群老构想", created=NOW - 8 * DAY)
        other = _seed_idea(store, gid=GID_OTHER, title="别群老构想", created=NOW - 8 * DAY)

        with _TimePatch():
            got = feeds.shelve_ignored_ideas(GID)  # 不传 now：用 clock.now

        assert got == [mine]
        assert _row(store, other)["state"] == "new"  # 不跨群
        assert feeds.shelve_ignored_ideas(GID_OTHER, NOW) == [other]

    def test_unserved_group_writes_nothing(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=None)  # 配置里一个服务群都没有
        iid = _seed_idea(store, title="没服务的群", created=NOW - 30 * DAY)

        assert feeds.shelve_ignored_ideas(GID, NOW) == []

        row = _row(store, iid)
        assert row["state"] == "new"
        assert float(row["updated"]) == pytest.approx(NOW - 30 * DAY)
        assert _events(store, "idea.auto_shelved") == []

    def test_not_served_but_exists_in_config_is_untouched(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_ONLY_OTHER)  # 只服务 222
        iid = _seed_idea(store, title="111 没被服务", created=NOW - 30 * DAY)

        assert feeds.shelve_ignored_ideas(GID, NOW) == []

        assert _row(store, iid)["state"] == "new"
        assert _events(store, "idea.auto_shelved") == []

    def test_idempotent_second_run_changes_nothing(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        iid = _seed_idea(store, title="八天没人理", created=NOW - 8 * DAY)

        assert feeds.shelve_ignored_ideas(GID, NOW) == [iid]
        first_updated = float(_row(store, iid)["updated"])
        assert len(_events(store, "idea.auto_shelved")) == 1

        assert feeds.shelve_ignored_ideas(GID, NOW) == []
        assert float(_row(store, iid)["updated"]) == pytest.approx(first_updated)
        assert len(_events(store, "idea.auto_shelved")) == 1
        # 只归档不删除：行还在
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 1

    def test_pending_or_approved_request_blocks_shelving(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        waiting = _seed_idea(store, title="有人想要、在等管理员批", state="wanted", created=NOW - 20 * DAY)
        _seed_request(store, waiting, "pending", rid="R-1")
        inflight = _seed_idea(store, title="批了、在落地", created=NOW - 20 * DAY)
        _seed_request(store, inflight, "approved", rid="R-2")
        pending_only = _seed_idea(store, title="等批准", state="pending", created=NOW - 20 * DAY)
        rejected = _seed_idea(store, title="请求被拒过", created=NOW - 20 * DAY)
        _seed_request(store, rejected, "rejected", rid="R-3")
        expired = _seed_idea(store, title="请求过期了", created=NOW - 20 * DAY)
        _seed_request(store, expired, "expired", rid="R-4")

        got = feeds.shelve_ignored_ideas(GID, NOW)

        # 终态请求（被拒 / 过期）不保护；pending / approved 保护
        assert got == [rejected, expired]
        assert _row(store, waiting)["state"] == "wanted"
        assert _row(store, inflight)["state"] == "new"
        assert _row(store, pending_only)["state"] == "pending"

    def test_started_task_and_already_dismissed_untouched(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        started = _seed_idea(store, title="开工了", state="started", created=NOW - 30 * DAY, task_id="T-1")
        started_no_task = _seed_idea(store, title="标了开工", state="started", created=NOW - 30 * DAY)
        new_with_task = _seed_idea(
            store, title="新想法但落了任务", created=NOW - 30 * DAY, task_id="T-2"
        )
        done = _seed_idea(
            store, title="早收起了", state="dismissed", created=NOW - 30 * DAY, updated=NOW - 20 * DAY
        )
        old_wanted = _seed_idea(store, title="老想要", state="wanted", created=NOW - 30 * DAY)

        assert feeds.shelve_ignored_ideas(GID, NOW) == [old_wanted]

        assert _row(store, started)["state"] == "started"
        assert _row(store, started_no_task)["state"] == "started"
        assert _row(store, new_with_task)["state"] == "new"
        assert _row(store, done)["state"] == "dismissed"
        # 早收起的：不重写它的 updated（幂等语义）
        assert float(_row(store, done)["updated"]) == pytest.approx(NOW - 20 * DAY)

    def test_personal_ideas_are_not_shelved(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        personal = _seed_idea(
            store, title="给阿帆整理的清单", created=NOW - 30 * DAY, target_user_id="u1"
        )
        group_idea = _seed_idea(store, title="给全群的", created=NOW - 30 * DAY)

        assert feeds.shelve_ignored_ideas(GID, NOW) == [group_idea]

        assert _row(store, personal)["state"] == "new"

    def test_likes_do_not_count_as_handled_and_created_wins(self, tmp_path) -> None:
        """喜欢点击（up）只动计数、不算开工；7 天一律看 created，不看 updated。"""
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        liked = _seed_idea(store, title="点过喜欢的", created=NOW - 8 * DAY)
        assert feeds.feedback("ideas", liked, "up", None) == {"up": 1, "down": 0}
        touched_recently = _seed_idea(
            store, title="最近被改过但想法很老", created=NOW - 9 * DAY, updated=NOW - 600
        )
        fresh = _seed_idea(store, title="刚提的", created=NOW - 3600, updated=NOW - 9 * DAY)

        got = feeds.shelve_ignored_ideas(GID, NOW)

        assert got == [liked, touched_recently]
        assert _row(store, liked)["state"] == "dismissed"
        assert _row(store, fresh)["state"] == "new"

    def test_records_event_and_never_touches_specialists(self, tmp_path) -> None:
        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED)
        a = _seed_idea(store, title="甲", created=NOW - 10 * DAY)
        b = _seed_idea(store, title="乙", state="wanted", created=NOW - 10 * DAY)
        loud = _RecordingSpecialists()
        feeds._specialists = loud  # 一碰就炸：守住「不借沉默学负面规矩」

        assert feeds.shelve_ignored_ideas(GID, NOW) == [a, b]

        evs = _events(store, "idea.auto_shelved")
        assert [int(e["entity_id"]) for e in evs] == [a, b]
        assert all(e["entity"] == "idea" and e["group_id"] == GID for e in evs)
        payload = json.loads(evs[0]["payload"])
        assert payload.get("title") == "甲"
        assert payload.get("from") == "new"
        assert loud.calls == []

    def test_settings_error_does_not_raise_or_write(self, tmp_path) -> None:
        def _boom() -> Any:
            raise RuntimeError("配置读不出来")

        store, feeds, *_ = _feeds(tmp_path, cfg=_SERVED, get_settings=_boom)
        iid = _seed_idea(store, title="八天没人理", created=NOW - 8 * DAY)

        assert feeds.shelve_ignored_ideas(GID, NOW) == []

        assert _row(store, iid)["state"] == "new"
        assert _events(store, "idea.auto_shelved") == []


# ----------------------------------------------------------------------
# A. make_idea：少而精 + 堆积闸
# ----------------------------------------------------------------------


class TestMakeIdeaFewAndGood:
    def test_prompt_demands_no_padding_and_optional_worth(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_IDEA_PLAIN])
        store, feeds, models, topics = _feeds(tmp_path, models=models)

        assert isinstance(_run(feeds.make_idea(GID)), int)

        prompt = models.calls[0][1][-1]["content"]
        # 少而精：想不到 / 把握不大就给 null，别凑数
        assert "少而精" in prompt
        assert "null" in prompt
        assert "凑数" in prompt or "不值得" in prompt
        assert "把握不大" in prompt
        # 信心字段是可选建议，不是硬要求
        assert '"worth"' in prompt and "可省略" in prompt
        # 提之前要能说清真实群需求 / 做不做得到 / 风险代价
        assert "风险" in prompt
        # 老要求没被挤掉
        assert '"origin"' in prompt and "不点名群友" in prompt
        assert "最多 5 个" in prompt
        assert '"step"' not in prompt and '"effort"' not in prompt

    def test_worth_low_skips_this_round(self, tmp_path) -> None:
        low = json.dumps({"idea": {**_plain_idea(), "worth": "low"}}, ensure_ascii=False)
        models = FakeModelsQueue(ready=True, replies=[low])
        store, feeds, models, topics = _feeds(tmp_path, models=models)

        assert _run(feeds.make_idea(GID)) is None

        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        assert topics.calls == []

    def test_worth_present_keeps_existing_shape(self, tmp_path) -> None:
        for worth in ("high", "medium", "说不准"):
            models = FakeModelsQueue(
                ready=True,
                replies=[json.dumps({"idea": {**_plain_idea(), "worth": worth}}, ensure_ascii=False)],
            )
            store, feeds, models, topics = _feeds(tmp_path / f"w-{worth}", models=models)
            got = _run(feeds.make_idea(GID))
            assert isinstance(got, int), worth
            row = store.read().execute("SELECT title, body, basis FROM ideas WHERE id=?", (got,)).fetchone()
            assert row["title"] and row["body"] and row["basis"]

    @pytest.mark.parametrize(
        "reply",
        [
            '{"idea": null}',
            '{"idea": null, "skip": "这轮没有值得提的"}',
            '{"skip": true}',
            "null",
            '{"idea": "none"}',
            '{"idea": []}',
        ],
    )
    def test_skip_outputs_are_quiet(self, tmp_path, reply: str) -> None:
        models = FakeModelsQueue(ready=True, replies=[reply])
        store, feeds, models, topics = _feeds(tmp_path, models=models)

        assert _run(feeds.make_idea(GID)) is None

        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 0
        assert topics.calls == []

    def test_pile_gate_blocks_at_three_unhandled(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_IDEA_PLAIN])
        store, feeds, models, topics = _feeds(tmp_path, models=models, cfg=_SERVED)
        _seed_idea(store, title="新的", created=NOW - 3600)
        _seed_idea(store, title="有人想要", state="wanted", created=NOW - 7200)
        pending = _seed_idea(store, title="等批准", state="pending", created=NOW - 100)

        assert _run(feeds.make_idea(GID)) is None

        assert models.calls == []  # 堆积了就不发模型调用
        assert topics.calls == []
        assert store.read().execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"] == 3
        # pending 安全：只读不改，没被顺手覆盖
        row = _row(store, pending)
        assert row["state"] == "pending"
        assert float(row["updated"]) == pytest.approx(NOW - 100)

    def test_pile_gate_boundary_two_unhandled_still_creates(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_IDEA_PLAIN])
        store, feeds, models, topics = _feeds(tmp_path, models=models, cfg=_SERVED)
        _seed_idea(store, title="甲", created=NOW - 3600)
        _seed_idea(store, title="乙", state="wanted", created=NOW - 3600)

        got = _run(feeds.make_idea(GID))

        assert isinstance(got, int) and got > 0
        assert len(models.calls) == 1

    def test_pile_gate_counts_only_this_group(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_IDEA_PLAIN])
        store, feeds, models, topics = _feeds(
            tmp_path, models=models, cfg=_SERVED, ready_groups=(GID, GID_OTHER)
        )
        for i in range(3):
            _seed_idea(store, gid=GID_OTHER, title=f"别群的{i}", created=NOW - 60)

        assert isinstance(_run(feeds.make_idea(GID)), int)

    def test_pile_gate_ignores_handled_and_personal(self, tmp_path) -> None:
        models = FakeModelsQueue(ready=True, replies=[_IDEA_PLAIN])
        store, feeds, models, topics = _feeds(tmp_path, models=models, cfg=_SERVED)
        for i in range(3):
            _seed_idea(store, title=f"开工{i}", state="started", created=NOW - 60, task_id=f"T-{i}")
            _seed_idea(store, title=f"收起{i}", state="dismissed", created=NOW - 60)
            _seed_idea(store, title=f"个人{i}", created=NOW - 60, target_user_id="u1")

        assert isinstance(_run(feeds.make_idea(GID)), int)
