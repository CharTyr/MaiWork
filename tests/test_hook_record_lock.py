"""收消息钩子**不许等 SQLite 写锁**，而且这条 @ 一条都不能丢（2026-10-15 复核，发版前根治）。

线上实测（2026-10，4 天）：intake 钩子超时 9 次、mentions 钩子超时 3 次；宿主钩子超时
1500ms，连续 3 次失败就熔断 60 秒，期间**所有群**的 MaiWork 钩子都被跳过。这次改动把
Jev 判词挪到后台、给钩子套了 800ms 预算，但**同步写库**这条路没被覆盖：钩子里的
`pending_asks.record_pending` 走 Store 的共享连接（`busy_timeout=5000`），另一个写入者
拿着写锁时，钩子在 `BEGIN IMMEDIATE` 上等满 5 秒（同步代码没有 await 点，预算拦不住）。

本机复现（持锁的第二个连接 + 真 Store，`/tmp/mwprobe/probe1.py`）：

    hook returned {'action': 'continue'} after 5.36s (budget is 0.8s)

而且这次写失败 = 这条 @ **丢了**（后台恢复 `profile.recover_pending_asks` 只看库里的行）。

改法（本文件钉住的行为）：
- 钩子里落库走 `PendingAsks.record_pending_bounded`：**独立短连接 + 有界等锁**
  （`_HOOK_RECORD_WAIT_S` 150ms），拿不到写锁立刻返回 None，绝不拖住钩子；
- 拿不到写锁就把这条 @ **原子写进磁盘 spool**（`pending_spool/`，0700 目录 / 0600 文件 /
  先写 .tmp 再 os.replace / 同名幂等 / 限长载荷，不碰主库写锁）——请求此时已经持久；
- 后台任务用独立连接（`asyncio.to_thread`，绝不在线程里共用 Store 连接）把它写进库，
  **写进库才删 spool 文件**；写不进就退避重试、**永不丢弃**；停机 / 热重载时条目留在
  磁盘上，新实例启动时立刻恢复（`kick_spool_recover`，没有事件循环就留给第一条消息 /
  巡检的 `mark_stale`）；
- 判词（`_judge_at`）等这条 @ 真写进库再走原来的后续路径（等不到就不判，留给独立重试），
  且 `_decide` 说这条不归它管时**不重复建请求**——一条 @ 只判一次。

测试用真 Store / 真 SQLite 锁（不是 sleep 假锁）、假 Jev、假批准，不联网、不碰宿主。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote, quote

import pytest

from fakes import hook_message

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.intake import Intake, Signals
from CharTyr_MaiWork.maiwork.pending_asks import SPOOL_DIRNAME, PendingAsks
from CharTyr_MaiWork.maiwork.store import Store

GID = "900000001"
# 钩子预算 800ms；这里连 spool 落盘（含 fsync）一起量，留点余量给慢 CI
_HOOK_TOTAL_MAX_S = 0.6


def _settings():
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{GID}"}]},
            "jev": {"timeout_ms": 1200},
        }
    )
    assert not problems
    return settings


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "lock.db")
    store.migrate()
    return store


class _WriteLock:
    """另一个写入者：用第二条连接拿住 SQLite 写锁（模拟后台线程 / 另一实例正在写）。

    WAL 下写锁仍然只有一个：`BEGIN IMMEDIATE` + 一次写就占住，别的写入者的
    `BEGIN IMMEDIATE` 要按自己的 busy_timeout 等。
    """

    def __init__(self, store: Store) -> None:
        self._conn = sqlite3.connect(str(store.path), isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout=0")
        self._conn.execute("BEGIN IMMEDIATE")
        self._conn.execute("INSERT INTO kv (key, value, updated) VALUES ('lock-probe', '1', 0)")

    def release(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


class _Jev:
    """立刻回「prepare」的假 Jev（钩子不等它，判词在后台）。"""

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    async def ask(self, state, questions, *, purpose: str, group_id: str = "", timeout_ms=None):
        self.calls += 1
        return {"kind": ("prepare", 0.92, 0.9)}


class _Approvals:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create(self, group_id: str, **kw):
        self.created.append((str(group_id), dict(kw)))
        return {"id": "R-1", "status": "pending"}


class _Mentions:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group_id: str, text: str, *, key: str, ttl_s: float, turns: int = 5,
            target_user_id: str = "", keywords="") -> None:
        self.items.append({"group_id": str(group_id), "text": str(text), "key": str(key)})


def _make_intake(tmp_path: Path, *, jev=None, store: Store | None = None):
    """装好 intake：spawn 只登记协程（后台活由测试自己 drain，确定性强）。"""
    settings = _settings()
    store = store if store is not None else _store(tmp_path)
    spawned: list = []
    approvals = _Approvals()
    intake = Intake(
        lambda: settings,
        Signals(),
        jev=jev,
        approvals=approvals,
        mentions=_Mentions(),
        store=store,
        spawn=lambda coro: spawned.append(coro),
    )
    return intake, store, spawned, approvals


async def _drain(spawned: list) -> None:
    """把钩子 spawn 出来的后台协程跑完（后台里再 spawn 出来的也跑，直到清空）。"""
    while spawned:
        await spawned.pop(0)


def _spool_files(tmp_path: Path) -> list[Path]:
    d = tmp_path / SPOOL_DIRNAME
    return sorted(p for p in d.iterdir() if p.suffix == ".json") if d.is_dir() else []


def _rows(store: Store) -> list:
    return store.read().execute(
        "SELECT message_id, state, reason, result_id FROM pending_asks ORDER BY message_id"
    ).fetchall()


# ----------------------------------------------------------------------
# 1) 落库入口本身：独立连接 + 有界等锁
# ----------------------------------------------------------------------


def test_record_pending_bounded_gives_up_within_budget(tmp_path) -> None:
    """锁被占着：`record_pending_bounded` 在预算内放弃（不跟 Store 的 5 秒 busy_timeout 走）。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    lock = _WriteLock(store)
    try:
        start = time.monotonic()
        row = pa.record_pending_bounded(
            GID, {"message_id": "m-locked", "text": "帮我整理资料"}, "等判断", timeout_s=0.15
        )
        took = time.monotonic() - start
        assert row is None
        assert took < 0.7, f"等了 {took:.2f}s（Store 的 busy_timeout 是 5 秒）"
        # 锁还在：库里不该有半条
        assert store.read().execute(
            "SELECT 1 FROM pending_asks WHERE message_id='m-locked'"
        ).fetchone() is None
    finally:
        lock.release()
    # 锁放开之后同一入口照常落库
    row = pa.record_pending_bounded(
        GID, {"message_id": "m-locked", "text": "帮我整理资料"}, "等判断", timeout_s=1.0
    )
    assert row is not None and row["state"] == "pending" and row["handled"] == 0
    store.close()


def test_record_pending_bounded_does_not_touch_store_connection(tmp_path) -> None:
    """独立连接：写完之后 Store 自己的连接照常读写（没有留下悬挂事务）。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    assert pa.record_pending_bounded(
        GID, {"message_id": "m-ok", "text": "整理资料"}, "等判断", timeout_s=1.0
    ) is not None
    with store.tx() as conn:
        conn.execute("INSERT INTO kv (key, value, updated) VALUES ('after', '1', 0)")
    assert store.kv_get("after") == 1
    store.close()


# ----------------------------------------------------------------------
# 2) 磁盘 spool：落盘、权限、限长、写进库才删
# ----------------------------------------------------------------------


def test_spool_write_is_atomic_private_and_bounded(tmp_path) -> None:
    """spool 文件：0600、目录 0700、载荷限长、同名幂等（同一条 @ 只留一份）。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    long_text = "整" * 5000
    assert pa.spool_write(
        GID, {"message_id": "m-spool", "user_id": "u1", "user_name": "小" * 500,
              "text": long_text}, "等判断" + "理" * 1000,
    ) is True
    files = _spool_files(tmp_path)
    assert len(files) == 1 and files[0].name.endswith(".json")
    if os.name != "nt":  # Windows 的 os.chmod 只认只读位，0600/0700 语义不成立
        mode = os.stat(files[0]).st_mode & 0o777
        assert mode == 0o600, oct(mode)
        assert (os.stat(files[0].parent).st_mode & 0o777) == 0o700
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(data["text"]) == 500 and len(data["reason"]) == 200 and len(data["user_name"]) == 100
    assert data["group_id"] == GID and data["message_id"] == "m-spool"
    assert not any(x.suffix == ".tmp" for x in (tmp_path / SPOOL_DIRNAME).iterdir())
    # 同名幂等：同一条 @ 再写一次只留一份（内容以后写的为准）
    assert pa.spool_write(GID, {"message_id": "m-spool", "text": "改一下"}, "等判断") is True
    files = _spool_files(tmp_path)
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["text"] == "改一下"
    store.close()


def test_flush_spool_once_writes_db_then_deletes_file(tmp_path) -> None:
    """补落库：写进库之后才删 spool 文件；写不进（锁还占着）文件留着。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    assert pa.spool_write(GID, {"message_id": "m-flush", "text": "帮我整理资料"}, "等判断")
    lock = _WriteLock(store)
    try:
        landed, waiting = pa.flush_spool_once(timeout_s=0.05)
        assert landed == [] and waiting == 1
        assert len(_spool_files(tmp_path)) == 1, "写不进库就不许删文件"
    finally:
        lock.release()
    landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert landed == [(GID, "m-flush")] and waiting == 0
    assert _spool_files(tmp_path) == [], "写进库才删"
    row = store.read().execute(
        "SELECT state, reason FROM pending_asks WHERE message_id='m-flush'"
    ).fetchone()
    assert row is not None and row["state"] == "pending" and row["reason"] == "等判断"
    store.close()


def test_flush_spool_skips_unserved_group(tmp_path) -> None:
    """非服务群不写（红线）；那条留着不丢。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    assert pa.spool_write("555000", {"message_id": "m-other", "text": "别的群"}, "等判断")
    assert pa.spool_write(GID, {"message_id": "m-served", "text": "服务群"}, "等判断")
    landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert landed == [(GID, "m-served")] and waiting == 0
    left = [json.loads(x.read_text(encoding="utf-8"))["message_id"] for x in _spool_files(tmp_path)]
    assert left == ["m-other"], "非服务群那条留着（不写、也不丢）"
    store.close()


def test_unserved_payload_is_never_read(tmp_path, monkeypatch) -> None:
    """曾服务后被移除的群：**连我们自己的 spool 载荷都不读**（非服务群零读取）。

    服务名单只从文件名里的群标识判；只要代码去打开那个群的文件，这条测试就 raise。
    """
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    assert pa.spool_write("555000", {"message_id": "m-gone", "text": "这个群后来被移除了"}, "等判断")
    assert pa.spool_write(GID, {"message_id": "m-here", "text": "还在服务"}, "等判断")
    spool = pa._spool
    original = type(spool)._load

    def guarded(self, path):
        if "555000" in unquote(path.name):
            raise AssertionError(f"非服务群的载荷被读了：{path.name}")
        return original(self, path)

    monkeypatch.setattr(type(spool), "_load", guarded)
    landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert landed == [(GID, "m-here")] and waiting == 0
    rows = store.read().execute("SELECT group_id, message_id FROM pending_asks").fetchall()
    assert [str(r["group_id"]) for r in rows] == [GID], "非服务群不许写库"
    left = _spool_files(tmp_path)
    assert len(left) == 1 and "555000" in unquote(left[0].name), "非服务群那条原样留着"
    assert json.loads(left[0].read_text(encoding="utf-8"))["message_id"] == "m-gone"
    store.close()


def test_spool_name_carries_gid_without_traversal(tmp_path) -> None:
    """文件名带 URL 转义的群标识（不读内容就能判服务名单），且拼不出路径穿越。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    weird = "../.."
    assert pa.spool_write(weird, {"message_id": "m-weird", "text": "x"}, "等判断")
    files = _spool_files(tmp_path)
    assert len(files) == 1
    name = files[0].name
    assert "/" not in name and "\\" not in name and name not in (".", "..")
    assert name.startswith(quote(weird, safe="") + "-") and name.endswith(".json")
    assert pa._spool._gid_of_name(name) == weird, "文件名里的群标识要能原样还原"
    # 载荷群号和文件名对不上 → 跳过不处理（防串群）
    with store.tx() as conn:
        conn.execute("UPDATE kv SET value=value WHERE 0")  # no-op，只是别动库
    store.close()


def test_unknown_old_spool_file_is_kept_and_warned(tmp_path, caplog) -> None:
    """认不出名字的老格式：不读内容、只 warning 一次、文件留着。"""
    import logging as _logging

    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    d = tmp_path / SPOOL_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    old = d / ("a" * 40 + ".json")
    old.write_text(json.dumps({"group_id": GID, "message_id": "m-old"}), encoding="utf-8")
    with caplog.at_level(_logging.WARNING, logger="maiwork.pending_asks"):
        assert pa.spool_count() == 0, "认不出的老格式不算「能处理的条目」"
        landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert landed == [] and waiting == 0
    assert old.exists(), "老格式文件留着不动"
    assert any("认不出名字" in r.getMessage() for r in caplog.records)
    store.close()


# ----------------------------------------------------------------------
# 3) 钩子：持锁时照样秒回，@ 不丢、判词照样落地
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_does_not_wait_for_write_lock_and_at_still_lands(tmp_path) -> None:
    """真持锁：钩子 < 0.9s 返回、这条 @ 已经写进 spool；放锁后补落库，判词照常建待批请求。"""
    jev = _Jev()
    intake, store, spawned, approvals = _make_intake(tmp_path, jev=jev)
    lock = _WriteLock(store)
    try:
        start = time.monotonic()
        out = await intake.handle(
            hook_message(GID, text="帮我整理一份铝坨坨的资料", is_at=True, message_id="m-lock")
        )
        took = time.monotonic() - start
        assert out == {"action": "continue"}
        # 这条断言量的是**钩子总耗时**：150ms 有界等锁 + 一次 spool 原子落盘（含 fsync）。
        # 只要哪一步变成跟 Store 的 5 秒 busy_timeout 走，这里立刻红（旧实现 5.36s）。
        assert took < _HOOK_TOTAL_MAX_S, f"钩子被拖住了（{took:.2f}s）"
        # 钩子回来时锁还没放：这条 @ 在磁盘 spool 里（已经持久），库里还没有
        assert store.read().execute(
            "SELECT 1 FROM pending_asks WHERE message_id='m-lock'"
        ).fetchone() is None
        names = [json.loads(p.read_text(encoding="utf-8"))["message_id"] for p in _spool_files(tmp_path)]
        assert names == ["m-lock"]
        # 后台先试一次（这时还锁着），再放锁
        drain = asyncio.ensure_future(_drain(spawned))
        await asyncio.sleep(0.15)
        lock.release()
        await asyncio.wait_for(drain, timeout=15)
    finally:
        lock.release()
    row = store.read().execute(
        "SELECT state, handled, result_id FROM pending_asks WHERE message_id='m-lock'"
    ).fetchone()
    assert row is not None, "锁放开后这条 @ 必须补落库（不能丢）"
    assert row["state"] == "completed" and row["handled"] == 1 and row["result_id"] == "R-1"
    assert len(approvals.created) == 1 and approvals.created[0][1]["message_id"] == "m-lock"
    assert jev.calls == 1
    assert _spool_files(tmp_path) == [], "写进库之后 spool 文件要删掉"
    store.close()


@pytest.mark.asyncio
async def test_spool_survives_hot_reload(tmp_path) -> None:
    """模拟热重载：旧实例的后台任务被丢掉（协程没跑），新实例启动时把 spool 里的 @ 落库。"""
    store = _store(tmp_path)
    lock = _WriteLock(store)
    try:
        old, _s, old_spawned, _a = _make_intake(tmp_path, jev=_Jev(), store=store)
        out = await old.handle(
            hook_message(GID, text="帮我整理资料", is_at=True, message_id="m-reload")
        )
        assert out == {"action": "continue"}
        assert len(_spool_files(tmp_path)) == 1
        for coro in old_spawned:      # 热重载：旧实例的后台协程没跑就被丢掉
            coro.close()
    finally:
        lock.release()
    # 新实例（同一个库、同一个 spool 目录）：启动时立刻恢复
    new, _s2, new_spawned, approvals = _make_intake(tmp_path, jev=_Jev(), store=store)
    new.kick_spool_recover()
    await asyncio.wait_for(_drain(new_spawned), timeout=15)
    rows = _rows(store)
    assert [str(r["message_id"]) for r in rows] == ["m-reload"], "重载后这条 @ 必须补上"
    assert _spool_files(tmp_path) == []
    assert len(approvals.created) == 0, "恢复只负责落库，判词归独立重试路径（不重复判）"
    store.close()


@pytest.mark.asyncio
async def test_two_ats_under_lock_both_land(tmp_path) -> None:
    """持锁期间连来两条 @：都进 spool（一条一份文件），放锁后两条都落库、都判到。"""
    intake, store, spawned, approvals = _make_intake(tmp_path, jev=_Jev())
    lock = _WriteLock(store)
    try:
        for i in (1, 2):
            out = await intake.handle(
                hook_message(GID, text=f"帮我整理资料 {i}", is_at=True, message_id=f"m-{i}")
            )
            assert out == {"action": "continue"}
        assert len(_spool_files(tmp_path)) == 2
        drain = asyncio.ensure_future(_drain(spawned))
        await asyncio.sleep(0.1)
        lock.release()
        await asyncio.wait_for(drain, timeout=15)
    finally:
        lock.release()
    rows = _rows(store)
    assert [str(r["message_id"]) for r in rows] == ["m-1", "m-2"]
    assert all(str(r["state"]) == "completed" for r in rows)
    assert len(approvals.created) == 2
    assert _spool_files(tmp_path) == []
    store.close()


@pytest.mark.asyncio
async def test_judge_defers_when_record_never_lands(tmp_path, monkeypatch) -> None:
    """锁一直占着：判词**不判**（不调 Jev、不建请求），条目留在 spool 等恢复——一条 @ 只判一次。"""
    from CharTyr_MaiWork.maiwork import intake as intake_mod

    monkeypatch.setattr(intake_mod, "_RECORD_WAIT_MAX_S", 0.3)
    jev = _Jev()
    intake, store, spawned, approvals = _make_intake(tmp_path, jev=jev)
    lock = _WriteLock(store)
    try:
        await intake.handle(
            hook_message(GID, text="帮我整理资料", is_at=True, message_id="m-stuck")
        )
        # spawn 顺序 = [补落库任务, 判词]：补落库会一直重试（锁占着），只等判词那条
        drainer = spawned.pop(0)
        judge = spawned.pop(0)
        await asyncio.wait_for(judge, timeout=15)
        drainer.close()
    finally:
        lock.release()
    assert jev.calls == 0, "还没落库就不许判"
    assert approvals.created == []
    assert len(_spool_files(tmp_path)) == 1, "条目还在磁盘上，等恢复"
    assert store.read().execute("SELECT 1 FROM pending_asks").fetchone() is None
    # 放锁之后巡检（mark_stale，每群 ~5 分钟一次）兜底补写
    pa = PendingAsks(store, _settings)
    pa.mark_stale(GID, now=time.time())
    assert [str(r["message_id"]) for r in _rows(store)] == ["m-stuck"]
    assert _spool_files(tmp_path) == []
    store.close()


@pytest.mark.asyncio
async def test_landed_marks_are_cleaned_up(tmp_path) -> None:
    """landed 标记：判词取走即清（不无限涨内存）。"""
    intake, store, spawned, _approvals = _make_intake(tmp_path, jev=_Jev())
    lock = _WriteLock(store)
    try:
        await intake.handle(
            hook_message(GID, text="帮我整理资料", is_at=True, message_id="m-clean")
        )
        drain = asyncio.ensure_future(_drain(spawned))
        await asyncio.sleep(0.1)
        lock.release()
        await asyncio.wait_for(drain, timeout=15)
    finally:
        lock.release()
    assert list(intake._landed_set) == [] and list(intake._landed_order) == []
    store.close()


@pytest.mark.asyncio
async def test_decide_false_does_not_build_request(tmp_path) -> None:
    """`_decide` 说这条不归它管（已经终态 / 已被独立重试接手）→ 不重复建请求。

    断言落在**有没有 spawn `_create_request`**（假 spawn 只登记协程，`approvals.created`
    是空的，只看它会假绿）。
    """
    jev = _Jev()
    intake, store, spawned, approvals = _make_intake(tmp_path, jev=jev)
    pa = intake._pending
    assert pa.record_pending(GID, {"message_id": "m-done", "text": "帮我整理资料"}, "收到 @，等判断")
    assert pa.decide(GID, "m-done", {"kind": "none", "why": "独立重试判闲聊"}) is True
    await intake._judge_at(_settings(), GID, "u1", "小", "m-done", "帮我整理资料")
    assert spawned == [], "终态行不许再 spawn 建请求（旧实现会重复建）"
    assert approvals.created == []
    assert jev.calls == 1, "判词照旧问一次（只是结论不落地）"
    store.close()


@pytest.mark.asyncio
async def test_hook_total_time_under_lock_stays_bounded(tmp_path) -> None:
    """锁一直被占：连来 5 条 @，每一条钩子总耗时（有界等锁 + spool 落盘 fsync）都在上限内。"""
    intake, store, spawned, _approvals = _make_intake(tmp_path, jev=_Jev())
    lock = _WriteLock(store)
    times: list[float] = []
    try:
        for i in range(5):
            start = time.monotonic()
            out = await intake.handle(
                hook_message(GID, text=f"帮我整理资料 {i}", is_at=True, message_id=f"m-t{i}")
            )
            times.append(time.monotonic() - start)
            assert out == {"action": "continue"}
        assert len(_spool_files(tmp_path)) == 5, "每条都要落进 spool（一条都不能丢）"
    finally:
        lock.release()
        for coro in spawned:
            coro.close()
    assert max(times) < _HOOK_TOTAL_MAX_S, f"最慢一条 {max(times):.2f}s（旧实现会被拖 5 秒）"
    store.close()


@pytest.mark.asyncio
async def test_judge_chain_writes_do_not_block_the_loop(tmp_path) -> None:
    """判词链（decide / complete / fail）写库也不许在事件循环上等 5 秒写锁。

    锁一直被占：判词跑完（问过 Jev）但不建请求、不写结论——整段耗时必须是「毫秒级 +
    0.2s 有界等锁」，绝不能是 Store 的 5 秒 busy_timeout；行留在库里等独立重试。
    """
    jev = _Jev()
    intake, store, spawned, approvals = _make_intake(tmp_path, jev=jev)
    lock = _WriteLock(store)
    try:
        start = time.monotonic()
        await asyncio.wait_for(
            intake._judge_at(_settings(), GID, "u1", "小", "m-chain", "帮我整理资料"),
            timeout=10,
        )
        took = time.monotonic() - start
    finally:
        lock.release()
        for coro in spawned:
            coro.close()
    assert took < _HOOK_TOTAL_MAX_S, f"判词链在事件循环上等了写锁（{took:.2f}s）"
    assert jev.calls == 1
    assert approvals.created == [], "结论没写进库就不许建请求（留给独立重试，只判一次）"
    store.close()


@pytest.mark.asyncio
async def test_decide_bounded_returns_none_when_locked(tmp_path) -> None:
    """`decide_bounded` / `complete_bounded` / `fail_bounded`：拿不到写锁返回 None（不是 False）。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    assert pa.record_pending(GID, {"message_id": "m-b", "text": "帮我整理"}, "等判断") is not None
    lock = _WriteLock(store)
    try:
        assert pa.decide_bounded(GID, "m-b", {"kind": "prepare", "title": "整理"}, timeout_s=0.05) is None
        assert pa.complete_bounded(GID, "m-b", result_kind="request", result_id="R-9", timeout_s=0.05) is None
        assert pa.fail_bounded(GID, "m-b", "试一下", timeout_s=0.05) is None
    finally:
        lock.release()
    # 放锁之后同一入口照常工作
    assert pa.decide_bounded(GID, "m-b", {"kind": "prepare", "title": "整理"}, timeout_s=1.0) is True
    assert pa.complete_bounded(GID, "m-b", result_kind="request", result_id="R-9", timeout_s=1.0) is True
    row = store.read().execute(
        "SELECT state, result_id FROM pending_asks WHERE message_id='m-b'"
    ).fetchone()
    assert row["state"] == "completed" and row["result_id"] == "R-9"
    store.close()


@pytest.mark.asyncio
async def test_reminder_without_time_still_builds(tmp_path) -> None:
    """reminder 还没解析出时间时 `_decide` 返回 False 是**正常**的（结论先存、行还挂着），
    提醒照样要建——别把「False」当成「不归我管」而不建（2026-10-15 回归：e2e 提醒全弧）。"""
    class _RemJev(_Jev):
        async def ask(self, state, questions, *, purpose, group_id="", timeout_ms=None):
            self.calls += 1
            return {"kind": ("reminder", 0.91, 0.91)}

    jev = _RemJev()
    settings = _settings()
    store = _store(tmp_path)
    spawned: list = []
    seen: list = []
    intake = Intake(
        lambda: settings, Signals(), jev=jev, approvals=_Approvals(), mentions=_Mentions(),
        store=store, spawn=lambda coro: spawned.append(coro),
        on_reminder=lambda gid, msg: seen.append((gid, msg["message_id"])),
    )
    intake._record(GID, "u1", "小", "m-rem", "明晚 8 点提醒我交稿", "收到 @，等判断")
    await intake._judge_at(settings, GID, "u1", "小", "m-rem", "明晚 8 点提醒我交稿")
    assert len(spawned) == 1, "提醒那条后台任务必须建"
    await spawned.pop(0)
    assert seen == [(GID, "m-rem")]
    row = store.read().execute(
        "SELECT state, decision FROM pending_asks WHERE message_id='m-rem'"
    ).fetchone()
    assert row is not None and str(row["state"]) == "pending", "时间还没解析，行继续挂着"
    store.close()


# ----------------------------------------------------------------------
# 4) 提交语义与整轮有界（2026-10-15 收尾）
# ----------------------------------------------------------------------


class _CommitFails:
    """代理连接：COMMIT 抛错（模拟提交失败），其余照旧——用来钉住「提交成功才删 spool」。"""

    def __init__(self, conn) -> None:
        object.__setattr__(self, "_conn", conn)

    def __setattr__(self, name, value):
        # 属性设置（row_factory 等）要落到真连接上，代理必须完全透明
        setattr(self._conn, name, value)

    def execute(self, sql, *args):
        if str(sql).strip().upper().startswith("COMMIT"):
            raise sqlite3.OperationalError("disk I/O error（测试注入的提交失败）")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_commit_failure_keeps_spool_and_can_replay(tmp_path, monkeypatch) -> None:
    """提交失败 = 没写成功：不许删 spool 文件、不许留下半条；放开之后重放能正常提交。"""
    from CharTyr_MaiWork.maiwork import pending_asks as pa_mod

    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    assert pa.spool_write(GID, {"message_id": "m-commit", "text": "帮我整理资料"}, "等判断")
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        pa_mod.sqlite3, "connect",
        lambda *a, **kw: _CommitFails(real_connect(*a, **kw)),
    )
    landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert landed == [] and waiting == 1, "提交没成功就不算落地"
    assert len(_spool_files(tmp_path)) == 1, "spool 文件不许删（删了就是真丢请求）"
    assert store.read().execute(
        "SELECT 1 FROM pending_asks WHERE message_id='m-commit'"
    ).fetchone() is None, "提交失败必须回滚，不许留半条"
    monkeypatch.undo()
    landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert landed == [(GID, "m-commit")] and waiting == 0
    assert _spool_files(tmp_path) == []
    assert store.read().execute(
        "SELECT 1 FROM pending_asks WHERE message_id='m-commit'"
    ).fetchone() is not None
    store.close()


def test_isolated_write_yields_none_when_locked(tmp_path) -> None:
    """拿不到写锁必须 `yield None`（不是抛异常、也不是「generator didn't yield」）。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    lock = _WriteLock(store)
    try:
        with pa.isolated_write(0.05) as conn:
            assert conn is None
    finally:
        lock.release()
    with pa.isolated_write(1.0) as conn:
        assert conn is not None
    store.close()


def test_flush_spool_round_stops_after_first_lock_failure(tmp_path, monkeypatch) -> None:
    """整轮有界：锁被占时一轮只等一次锁（不是 4 条 × timeout），4 条全留着等退避重试。

    不变量用「锁尝试次数」来钉，不是墙钟——CI 高负载下一次 busy_timeout 等待本身
    就可能被拖到 0.4s+，按时间断言会把「没做整轮有界」和「机器慢」混为一谈。
    """
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    for i in range(4):
        assert pa.spool_write(GID, {"message_id": f"m-r{i}", "text": "帮我整理"}, "等判断")
    attempts = 0
    real_iso = pa.isolated_write

    def _counting_iso(timeout_s):
        nonlocal attempts
        attempts += 1
        return real_iso(timeout_s)

    monkeypatch.setattr(pa, "isolated_write", _counting_iso)
    lock = _WriteLock(store)
    try:
        landed, waiting = pa.flush_spool_once(timeout_s=0.2)
    finally:
        lock.release()
    assert landed == [] and waiting == 4
    assert attempts == 1, f"一轮试了 {attempts} 次写锁（4 条 × timeout 就是没做整轮有界）"
    assert len(_spool_files(tmp_path)) == 4, "一条都不许丢"
    # 放锁之后一轮全部落地
    landed, waiting = pa.flush_spool_once(timeout_s=1.0)
    assert len(landed) == 4 and waiting == 0
    assert _spool_files(tmp_path) == []
    store.close()


def test_flush_spool_round_budget_zero_writes_nothing(tmp_path) -> None:
    """整轮墙钟预算到点：这一轮一条都不写（全算「还剩」），spool 原样留着。"""
    store = _store(tmp_path)
    pa = PendingAsks(store, _settings)
    for i in range(3):
        assert pa.spool_write(GID, {"message_id": f"m-b{i}", "text": "帮我整理"}, "等判断")
    landed, waiting = pa.flush_spool_once(timeout_s=1.0, budget_s=0.0)
    assert landed == [] and waiting == 3
    assert len(_spool_files(tmp_path)) == 3
    store.close()


# ----------------------------------------------------------------------
# 5) 持久失败收口：没持久就绝不判（2026-10-15 最后一条）
# ----------------------------------------------------------------------


class _QuickJudge:
    """假的派活判断兜底：只记有没有被喂过。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def enabled(self) -> bool:
        return True

    def enqueue(self, gid, item, context=None) -> bool:
        self.items.append({"gid": str(gid), "item": dict(item)})
        return True


@pytest.mark.asyncio
async def test_persist_failure_never_judges(tmp_path, monkeypatch, caplog) -> None:
    """DB 和 spool 都写不进：钩子照常 continue，但**零 Jev / 零快速判断 / 零请求 / 零提醒**。

    没持久就往下判，等于拿一条查不到的 @ 去调模型、还可能建出请求或提醒——违反
    「先持久、再判」（2026-10-15 复核收口）。
    """
    import logging as _logging

    jev = _Jev()
    quick = _QuickJudge()
    settings = _settings()
    store = _store(tmp_path)
    spawned: list = []
    approvals = _Approvals()
    reminded: list = []
    intake = Intake(
        lambda: settings, Signals(), jev=jev, approvals=approvals, mentions=_Mentions(),
        store=store, spawn=lambda coro: spawned.append(coro), quick_judge=quick,
        on_reminder=lambda gid, msg: reminded.append((gid, msg)),
    )
    # 库写不进（None）+ spool 也写不进（False）
    monkeypatch.setattr(
        PendingAsks, "record_pending_bounded",
        lambda self, gid, msg, reason, *, timeout_s=0.15: None,
    )
    monkeypatch.setattr(PendingAsks, "spool_write", lambda self, gid, msg, reason: False)
    with caplog.at_level(_logging.WARNING, logger="maiwork.intake"):
        out = await intake.handle(
            hook_message(GID, text="帮我整理资料", is_at=True, message_id="m-lost")
        )
    assert out == {"action": "continue"}, "钩子永不中止宿主消息"
    assert spawned == [], "没持久就不许起任何判词 / 模型 / 提醒"
    assert jev.calls == 0 and quick.items == [] and approvals.created == [] and reminded == []
    assert store.read().execute("SELECT 1 FROM pending_asks").fetchone() is None
    assert _spool_files(tmp_path) == [], "也没写进 spool"
    assert any("保不住" in r.getMessage() for r in caplog.records), "要留一条能查的失败日志"
    assert any("没持久" in r.getMessage() for r in caplog.records)
    store.close()


@pytest.mark.asyncio
async def test_slow_path_does_not_claim_persisted_on_failure(tmp_path, monkeypatch, caplog) -> None:
    """`_slow` 那条路同样不许把持久失败当落盘成功（不进慢路径）。"""
    import logging as _logging

    settings = _settings()
    store = _store(tmp_path)
    intake = Intake(
        lambda: settings, Signals(), jev=None, approvals=_Approvals(), mentions=_Mentions(),
        store=store, spawn=lambda coro: None,
    )
    monkeypatch.setattr(
        PendingAsks, "record_pending_bounded",
        lambda self, gid, msg, reason, *, timeout_s=0.15: None,
    )
    monkeypatch.setattr(PendingAsks, "spool_write", lambda self, gid, msg, reason: False)
    with caplog.at_level(_logging.WARNING, logger="maiwork.intake"):
        intake._slow("jev_无答案", GID, "u1", "小", "m-slow", "帮我整理资料")
    assert store.read().execute("SELECT 1 FROM pending_asks").fetchone() is None
    assert intake.slow_queue == [], "库里没有这条，就不该出现在慢路径视图里"
    assert any("不进慢路径" in r.getMessage() for r in caplog.records)
    store.close()
