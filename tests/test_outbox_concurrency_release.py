"""发件箱并发的最后一道闸（2026-10 复审）。

背景：`Outbox.flush` 原来没有任何并发保护，「同一 key 只发一次」只靠
`_due_rows`（status='pending'）和 `_claim`（`UPDATE … WHERE id=?`，**没有**
status / not_before 条件）两步之间**没有 await** 这个巧合。一旦

- 两个 flush 协程并发（同一实例或同进程两个实例），或
- 以后有人在读行和 claim 之间插一个 await，

两个 flush 都会把同一行当成待发：`_claim` 照样写 sending、attempts 各 +1、
两边都真的把同一条消息发进群，安全失败的那次自动重试也可能被算成两次首发
（群里看到两条一样的开场白 / 卡片）。

修：`flush` 走一把 asyncio 锁（同一实例串行），`_claim` 变成数据库 CAS
（`WHERE id=? AND status='pending' AND not_before<=?`），抢不到（0 行）就
**明确跳过**——不发、不记账、不触发结果 hook。不同 Outbox 实例共用同一个
Store 时靠 CAS + 「sending 已占额度」兜底，全场仍然只发一次。

额度顺序保持原样：**先 can_push（此刻自己还是 pending，不把自己算满）→ 再
claim（转 sending，从此占住一份额度）→ 再 await 发送**。所以两个并发、
不同 key 的推送也超不过当群总额度。

这些用例先写先红：旧实现下并发路径会发两次 / attempts 被多加。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=BJ).timestamp()
NEXT_DAY = datetime(2026, 10, 16, 0, 0, tzinfo=BJ).timestamp()


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------


class GatedHost:
    """假宿主：把 `send_text` 卡在一道闸上，好让「另一个 flush」在它 await 时插进来。

    只有测试放开闸（`gate.set()`）之后才真的返回；`error` 非空时抛那个错。
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.error = error

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.calls.append({"session_id": session_id, "text": text})
        self.entered.set()
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(sent=True, message_id=f"m{len(self.calls)}")

    async def send_image(self, session_id, png, *, text=""):
        raise AssertionError("并发用例只发文本")

    async def upload_group_file(self, group_id, path, name):
        raise AssertionError("并发用例只发文本")


class World:
    def __init__(self, store, settings, host, pushes, mentions, root) -> None:
        self.store = store
        self.settings = settings
        self.host = host
        self.pushes = pushes
        self.mentions = mentions
        self.root = root

    def outbox(self, *, host=None, cls=Outbox, **kwargs) -> Outbox:
        return cls(
            self.store,
            host if host is not None else self.host,
            self.pushes,
            self.mentions,
            lambda: self.settings,
            **kwargs,
        )


def _make(tmp_path, *, daily_max: int = 3, host: GatedHost | None = None) -> World:
    root = tmp_path / "w"
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "t.db")
    store.migrate()
    settings, problems = load_settings({
        "environments": {"workspace_root": str(root)},
        "groups": {"serve": [{"group": f"qq:{GID}"}]},
        "delivery": {"push_per_day": daily_max, "quiet_hours": "00:00-00:00"},
    })
    assert not problems, problems
    host = host if host is not None else GatedHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, '测试群', ?)",
            (GID, f"sess-{GID}", f"tok{GID}"),
        )
    group_push.set_config(store, GID, {"daily_max": daily_max, "quiet_hours": "00:00-00:00"},
                          settings, now=NOON - 3600)
    return World(store, settings, host, pushes, mentions, root)


def _rows(store: Store) -> list:
    return store.read().execute(
        "SELECT id, key, status, attempts, not_before, error FROM outbox ORDER BY id"
    ).fetchall()


def _pushes(store: Store) -> list:
    return store.read().execute("SELECT kind FROM pushes ORDER BY id").fetchall()


class SnapshotOutbox(Outbox):
    """故意让两个实例读到同一份**过期快照**，逼出数据库 CAS 的必要性。

    `_due_rows` 第一次被调用时把结果记进共享列表，之后永远返回那份列表——
    模拟「两个 flush 都在对方 claim 之前读到了同一行 pending」。真实情况下
    读行和 claim 之间没有 await 才侥幸不出事；这里把巧合去掉，只有 CAS
    能挡住第二次发送。
    """

    def __init__(self, *args, snapshot: list, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._snapshot = snapshot

    def _due_rows(self, now: float) -> list:
        rows = super()._due_rows(now)
        if not self._snapshot:
            self._snapshot.extend(rows)
        return list(self._snapshot)


# ----------------------------------------------------------------------
# 1. 同一实例：两把并发 flush，同 key 只发一次
# ----------------------------------------------------------------------


async def test_concurrent_flushes_same_instance_send_same_key_once(tmp_path):
    world = _make(tmp_path)
    outbox = world.outbox()
    seen: list[dict] = []
    outbox.add_result_hook(seen.append)
    outbox.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})

    a = asyncio.create_task(outbox.flush(NOON))
    await asyncio.wait_for(world.host.entered.wait(), 2)   # A 已 claim 并卡在发送里
    b = asyncio.create_task(outbox.flush(NOON))            # 第二把并发 flush
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(world.host.calls) == 1, "第二把 flush 不许再发一次"

    world.host.gate.set()
    await asyncio.wait_for(asyncio.gather(a, b), 2)

    assert len(world.host.calls) == 1
    assert [h["text"] for h in world.host.calls] == ["开场白"]
    row = _rows(world.store)[0]
    assert row["status"] == "sent" and int(row["attempts"]) == 1
    assert len(_pushes(world.store)) == 1
    assert [i["outcome"] for i in seen] == ["sent"], "跳过的 flush 不许触发结果 hook"


# ----------------------------------------------------------------------
# 2. 不同实例 + 同一 Store：同 key 只发一次（数据库那一层挡住）
# ----------------------------------------------------------------------


async def test_concurrent_flushes_two_instances_send_same_key_once(tmp_path):
    world = _make(tmp_path)
    ob1 = world.outbox()
    ob2 = world.outbox()                       # 另一个实例，共用同一个 Store
    ob1.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})

    a = asyncio.create_task(ob1.flush(NOON))
    await asyncio.wait_for(world.host.entered.wait(), 2)
    b = asyncio.create_task(ob2.flush(NOON))
    await asyncio.wait_for(b, 2)               # ob2 现在查不到 pending，直接收工
    assert len(world.host.calls) == 1

    world.host.gate.set()
    await asyncio.wait_for(a, 2)
    assert len(world.host.calls) == 1
    row = _rows(world.store)[0]
    assert row["status"] == "sent" and int(row["attempts"]) == 1


async def test_stale_snapshot_second_claim_is_skipped_by_cas(tmp_path):
    """两个实例都拿着「claim 之前读到的 pending 快照」：CAS 只能让一个过去。

    旧实现 `UPDATE … WHERE id=?` 不带条件，第二个实例照样把 sending 再写一遍、
    attempts+1、再发一条；0 行命中必须明确跳过（不发 / 不记账 / 不 hook）。
    """
    world = _make(tmp_path)
    snapshot: list = []
    ob1 = world.outbox(cls=SnapshotOutbox, snapshot=snapshot)
    ob2 = world.outbox(cls=SnapshotOutbox, snapshot=snapshot)
    seen: list[dict] = []
    ob1.add_result_hook(seen.append)
    ob2.add_result_hook(seen.append)
    ob1.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    assert ob2._due_rows(NOON), "先把两个实例共用的那份「过期」快照填好"

    a = asyncio.create_task(ob1.flush(NOON))
    await asyncio.wait_for(world.host.entered.wait(), 2)
    b = asyncio.create_task(ob2.flush(NOON))
    await asyncio.wait_for(b, 2)               # 快照里还有那一行，但 CAS 抢不到

    assert len(world.host.calls) == 1, "第二个实例不许重发"
    world.host.gate.set()
    await asyncio.wait_for(a, 2)

    assert len(world.host.calls) == 1
    row = _rows(world.store)[0]
    assert row["status"] == "sent" and int(row["attempts"]) == 1, "attempts 不许被多加"
    assert len(_pushes(world.store)) == 1
    assert [i["outcome"] for i in seen] == ["sent"], "抢不到的那次不许触发结果 hook"


async def test_claim_is_compare_and_swap_with_not_before_guard(tmp_path):
    """`_claim` 的数据库契约：只有 pending 且到点的行才能抢到，抢不到返回 0。"""
    world = _make(tmp_path)
    ob1 = world.outbox()
    ob2 = world.outbox()
    oid = ob1.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})

    assert ob1._claim(oid, moment=NOON) == 1          # 第一次抢到
    assert ob2._claim(oid, moment=NOON) == 0          # 已经是 sending：抢不到
    assert int(_rows(world.store)[0]["attempts"]) == 1

    with world.store.tx() as conn:                     # 回到 pending，但还没到点
        conn.execute("UPDATE outbox SET status='pending', not_before=? WHERE id=?",
                     (NOON + 100, oid))
    assert ob2._claim(oid, moment=NOON) == 0, "没到点不许抢"
    assert int(_rows(world.store)[0]["attempts"]) == 1

    with world.store.tx() as conn:
        conn.execute("UPDATE outbox SET not_before=0 WHERE id=?", (oid,))
    assert ob2._claim(oid, moment=NOON) == 2
    assert int(_rows(world.store)[0]["attempts"]) == 2


# ----------------------------------------------------------------------
# 3. 并发下的失败 / 重试：首发 + 一次，绝不多
# ----------------------------------------------------------------------


async def test_concurrent_flushes_never_exceed_two_attempts(tmp_path):
    host = GatedHost(error=HostError("发送失败"))
    world = _make(tmp_path, host=host)
    ob1 = world.outbox()
    ob2 = world.outbox()
    ob1.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})

    a = asyncio.create_task(ob1.flush(NOON))
    await asyncio.wait_for(host.entered.wait(), 2)
    b = asyncio.create_task(ob2.flush(NOON))
    await asyncio.wait_for(b, 2)
    assert len(host.calls) == 1, "并发的那把不许把同一条当成第二次首发"

    host.gate.set()
    await asyncio.wait_for(a, 2)
    row = _rows(world.store)[0]
    assert row["status"] == "pending" and int(row["attempts"]) == 1
    assert float(row["not_before"]) == NOON + 300

    await ob1.flush(NOON + 400)
    row = _rows(world.store)[0]
    assert row["status"] == "failed" and int(row["attempts"]) == 2
    assert len(host.calls) == 2                     # 首发 + 安全重试一次，就两次

    await ob1.flush(NOON + 900)
    assert len(host.calls) == 2 and _pushes(world.store) == []


# ----------------------------------------------------------------------
# 4. 并发的两个不同 key：当群总额度 1 不许超额
# ----------------------------------------------------------------------


async def test_concurrent_flushes_two_keys_never_exceed_group_cap(tmp_path):
    world = _make(tmp_path, daily_max=1)
    ob1 = world.outbox()
    ob2 = world.outbox()
    ob1.enqueue("k:a", GID, "text", {"text": "第一条", "push_kind": "topic"})
    ob1.enqueue("k:b", GID, "text", {"text": "第二条", "push_kind": "topic"})

    a = asyncio.create_task(ob1.flush(NOON))
    await asyncio.wait_for(world.host.entered.wait(), 2)   # 第一条已经 sending（占住额度）
    b = asyncio.create_task(ob2.flush(NOON))               # 另一个实例抢第二条
    await asyncio.wait_for(b, 2)

    assert len(world.host.calls) == 1, "额度 1 时两个并发 flush 也只能发一条"
    assert [h["text"] for h in world.host.calls] == ["第一条"]
    world.host.gate.set()
    await asyncio.wait_for(a, 2)

    rows = {str(r["key"]): r for r in _rows(world.store)}
    assert rows["k:a"]["status"] == "sent"
    assert rows["k:b"]["status"] == "pending"
    assert float(rows["k:b"]["not_before"]) >= NEXT_DAY
    assert "推够" in str(rows["k:b"]["error"]) or "上限" in str(rows["k:b"]["error"])
    assert len(_pushes(world.store)) == 1
    assert world.pushes.count_used(GID, NOON) == 1


async def test_concurrent_flushes_two_keys_cap_one_same_instance(tmp_path):
    """同一实例（有锁串行）也一样：cap 1 时两条不同 key 只发一条。"""
    world = _make(tmp_path, daily_max=1)
    outbox = world.outbox()
    outbox.enqueue("k:a", GID, "text", {"text": "第一条", "push_kind": "topic"})
    outbox.enqueue("k:b", GID, "text", {"text": "第二条", "push_kind": "topic"})

    a = asyncio.create_task(outbox.flush(NOON))
    await asyncio.wait_for(world.host.entered.wait(), 2)
    b = asyncio.create_task(outbox.flush(NOON))
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(world.host.calls) == 1

    world.host.gate.set()
    await asyncio.wait_for(asyncio.gather(a, b), 2)
    assert len(world.host.calls) == 1
    rows = {str(r["key"]): r for r in _rows(world.store)}
    assert rows["k:a"]["status"] == "sent" and rows["k:b"]["status"] == "pending"
    assert len(_pushes(world.store)) == 1


# ----------------------------------------------------------------------
# 5. 崩溃 / 超时语义不变
# ----------------------------------------------------------------------


async def test_uncertain_and_recover_semantics_unchanged(tmp_path):
    """超时 → uncertain（不重发、保留额度）；重启留下的 sending 由 recover 收口。"""
    host = GatedHost(error=asyncio.TimeoutError())
    world = _make(tmp_path, daily_max=1, host=host)
    ob1 = world.outbox()
    ob2 = world.outbox()
    ob1.enqueue("k:a", GID, "text", {"text": "第一条", "push_kind": "topic"})
    ob1.enqueue("k:b", GID, "text", {"text": "第二条", "push_kind": "topic"})

    a = asyncio.create_task(ob1.flush(NOON))
    await asyncio.wait_for(host.entered.wait(), 2)
    b = asyncio.create_task(ob2.flush(NOON))
    await asyncio.wait_for(b, 2)
    host.gate.set()
    await asyncio.wait_for(a, 2)

    rows = {str(r["key"]): r for r in _rows(world.store)}
    assert rows["k:a"]["status"] == "uncertain"
    assert rows["k:b"]["status"] == "pending"
    assert len(host.calls) == 1
    await ob1.flush(NOON + 600)
    assert len(host.calls) == 1, "不确定的绝不重发"
    # 重启恢复：把发送中留下的行标不确定，就一条，不再重放
    with world.store.tx() as conn:
        conn.execute("UPDATE outbox SET status='sending' WHERE key='k:a'")
    assert ob1.recover() == 1
    assert _rows(world.store)[0]["status"] == "uncertain"


async def test_stale_snapshot_same_instance_second_claim_is_skipped(tmp_path):
    """同一实例也一样：光「读行 → claim 之间没 await」不够，claim 本身必须是 CAS。"""
    world = _make(tmp_path)
    snapshot: list = []
    outbox = world.outbox(cls=SnapshotOutbox, snapshot=snapshot)
    seen: list[dict] = []
    outbox.add_result_hook(seen.append)
    outbox.enqueue("k:topic", GID, "text", {"text": "开场白", "push_kind": "topic"})
    assert outbox._due_rows(NOON), "先填好那份「读到时就已过期」的快照"

    a = asyncio.create_task(outbox.flush(NOON))
    await asyncio.wait_for(world.host.entered.wait(), 2)
    b = asyncio.create_task(outbox.flush(NOON))
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(world.host.calls) == 1, "拿过期快照的第二次 flush 不许重发"

    world.host.gate.set()
    await asyncio.wait_for(asyncio.gather(a, b), 2)
    assert len(world.host.calls) == 1
    row = _rows(world.store)[0]
    assert row["status"] == "sent" and int(row["attempts"]) == 1
    assert [i["outcome"] for i in seen] == ["sent"]
