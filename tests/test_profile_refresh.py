"""profile.py 第二部分测试：调主模型提炼群画像、ops 应用、失败重试、每周整理、PROFILE.md。

时钟钉在 T0（2026-09-26 14:05 北京时间，周六）。默认 settings：
batch_messages=3、backfill_days=30、workspace_root=tmp_path/wsroot、workspace=tinker。
FakeHost 记录每次 messages(session_id, start, end, limit)；FakeModelsQueue 记 chat 调用并弹预设回复。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.host import Msg
from CharTyr_MaiWork.maiwork.models import ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.profile import Profiles

from fakes import FakeHost, FakeModelsQueue

GID = "900000001"
# 2026-09-26 06:05 UTC = 北京时间 14:05（周六）
T0 = datetime(2026, 9, 26, 6, 5, tzinfo=timezone.utc).timestamp()
assert clock.bj(T0).weekday() == 5


def _settings(
    tmp_path: Path,
    *,
    personal_profile: bool = True,
    ws_name: str = "tinker",
    **profile_over,
):
    profile = {"backfill_days": 30, "backfill_max_messages": 1500, "batch_messages": 3}
    profile.update(profile_over)
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": ws_name}]},
        "focus": {"personal_profile": personal_profile},
        "environments": {"workspace_root": str(tmp_path / "wsroot")},
        "profile": profile,
    }
    settings, problems = load_settings(raw)
    assert not problems
    return settings


def _msg(
    mid: str,
    ts: float,
    user: str = "u1",
    name: str = "阿一",
    *,
    bot: bool = False,
    text: str = "你好",
) -> Msg:
    return Msg(
        id=mid,
        ts=ts,
        user_id=user,
        user_name=name,
        text=text,
        is_bot=bot,
        is_at=False,
        is_picture=False,
        reply_to="",
    )


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(clock, "now", lambda: T0)
    return T0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "maiwork.db")
    s.migrate()
    yield s
    s.close()


def _make(
    store: Store,
    host: FakeHost,
    models: FakeModelsQueue,
    settings,
) -> Profiles:
    return Profiles(store, host, models, lambda: settings)


def _group(store: Store, gid: str = GID):
    return store.read().execute(
        "SELECT * FROM groups WHERE group_id=?", (gid,)
    ).fetchone()


def _events(store: Store, kind: str, gid: str = GID) -> list:
    return store.read().execute(
        "SELECT * FROM events WHERE kind=? AND group_id=? ORDER BY id", (kind, gid)
    ).fetchall()


# ----------------------------------------------------------------------
# 门控：模型没配好 / 没攒够不提炼
# ----------------------------------------------------------------------


class TestGate:
    @pytest.mark.asyncio
    async def test_not_ready_no_model_call_but_pending_keeps(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue(ready=False, replies=['{"ops": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"消息{i}") for i in range(5)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.read == 5
        assert r.refreshed is False
        assert models.calls == []  # 没调模型
        row = _group(store)
        assert row["pending_count"] == 5  # pending 留着
        assert row["profile_ready_ts"] == 0

    @pytest.mark.asyncio
    async def test_below_batch_no_refresh_after_ready(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        # 画像已成形（非首次）时，pending < batch 不触发
        settings = _settings(tmp_path)  # batch=3
        models = FakeModelsQueue(replies=['{"ops": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(2)])  # 只 2 条
        p = _make(store, host, models, settings)
        p.ensure_group(GID)
        with store.tx() as conn:
            conn.execute(
                "UPDATE groups SET profile_ready_ts=?, last_refresh_ts=? WHERE group_id=?",
                (T0 - 3600, T0 - 3600, GID),
            )
        r = await p.tick(GID)
        assert r.refreshed is False
        assert models.calls == []
        assert _group(store)["pending_count"] == 2

    @pytest.mark.asyncio
    async def test_first_time_any_pending_triggers(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        # 首次（profile_ready_ts==0）哪怕只有 1 条也提炼
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg("m1", T0 - 10)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert len(models.calls) == 1
        assert r.refreshed is True

    @pytest.mark.asyncio
    async def test_interval_alone_with_few_pending_no_refresh(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        # last_refresh 很久以前、但 pending=4 < 5 → 不触发
        settings = _settings(tmp_path, batch_messages=1000)
        models = FakeModelsQueue(replies=['{"ops": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 10 + i) for i in range(4)])
        p = _make(store, host, models, settings)
        p.ensure_group(GID)
        with store.tx() as conn:
            conn.execute(
                "UPDATE groups SET last_refresh_ts=?, profile_ready_ts=? WHERE group_id=?",
                (T0 - 10 * 3600, T0 - 86400, GID),
            )
        r = await p.tick(GID)
        assert r.refreshed is False
        assert models.calls == []

    @pytest.mark.asyncio
    async def test_interval_and_five_pending_triggers(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        # last_refresh 4 小时前（≥3 小时）、pending=5 → 触发（batch 设巨大，排除数量触发）
        settings = _settings(tmp_path, batch_messages=1000)
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(5)])
        p = _make(store, host, models, settings)
        p.ensure_group(GID)
        with store.tx() as conn:
            conn.execute(
                "UPDATE groups SET last_refresh_ts=?, profile_ready_ts=? WHERE group_id=?",
                (T0 - 4 * 3600, T0 - 86400, GID),
            )
        r = await p.tick(GID)
        assert len(models.calls) == 1
        assert r.refreshed is True

    @pytest.mark.asyncio
    async def test_force_triggers_on_empty_pending(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        # 强制 → 哪怕 pending=0 也提炼（回读库里的消息）
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg("m1", T0 - 60), _msg("m2", T0 - 30), _msg("m3", T0 - 10)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID, force=True)
        assert len(models.calls) == 1
        assert r.refreshed is True

    @pytest.mark.asyncio
    async def test_batch_trigger_and_pending_cleared(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)  # batch=3
        models = FakeModelsQueue(replies=['{"ops": [], "people": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert len(models.calls) == 1
        assert r.refreshed is True
        row = _group(store)
        assert row["pending_count"] == 0
        assert row["profile_ready_ts"] > 0
        assert row["fail_count"] == 0
        assert row["last_refresh_ts"] > 0


# ----------------------------------------------------------------------
# 分批与回读窗口
# ----------------------------------------------------------------------


class TestBatches:
    @pytest.mark.asyncio
    async def test_first_refresh_splits_into_120_per_call(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue()  # 队列空 → 回空 ops
        host = FakeHost([_msg(f"m{i}", T0 - 1500 + i, text=f"第{i}条") for i in range(1500)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.read == 1500
        assert r.refreshed is True
        assert len(models.calls) == 13  # 120 × 12 + 60
        row = _group(store)
        assert row["pending_count"] == 0

    @pytest.mark.asyncio
    async def test_second_refresh_reads_only_new_window(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue()
        first = [_msg(f"a{i}", T0 - 5000 + i, text=f"旧{i}") for i in range(3)]
        host = FakeHost(first)
        p = _make(store, host, models, settings)
        await p.tick(GID)
        assert len(models.calls) == 1
        # tick 分页读结束后，提炼读的窗口都在后面
        paginate_calls = len(host.msg_calls)

        host.msgs.extend([_msg(f"b{i}", T0 - 100 + i, text=f"新{i}") for i in range(3)])
        await p.tick(GID)
        assert len(models.calls) == 2
        # 第二次 tick：1 次分页读 + 1 次提炼读
        refresh_call = host.msg_calls[paginate_calls + 1]
        # 提炼读的 start 不小于第一次读到的最大 ts（不重读旧消息）
        assert refresh_call[1] >= first[-1].ts


# ----------------------------------------------------------------------
# ops：add / update / remove / touch
# ----------------------------------------------------------------------


class TestOps:
    _ENTRY_JSON = (
        '{"ops": [{"op": "add", "category": "recent", "text": "在聊新出的掌机", "evidence": [1, 3]}], "people": []}'
    )

    @pytest.mark.asyncio
    async def test_add_creates_model_entry_with_evidence_ids(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=[self._ENTRY_JSON])
        msgs = [_msg(f"m{i}", T0 - 200 + i * 10, text=f"掌机{i}") for i in range(3)]
        host = FakeHost(msgs)
        p = _make(store, host, models, settings)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT * FROM profile_entries WHERE group_id=?", (GID,)
        ).fetchone()
        assert row is not None
        assert row["category"] == "recent"
        assert row["text"] == "在聊新出的掌机"
        assert row["source"] == "model"
        assert row["deleted"] == 0 and row["locked"] == 0
        # evidence 序号 1、3 → 消息 id m0、m2；first/last 用证据消息 ts
        import json as _j
        assert _j.loads(row["evidence"]) == ["m0", "m2"]
        assert row["evidence_count"] == 2
        assert row["first_ts"] == msgs[0].ts
        assert row["last_ts"] == msgs[2].ts
        # 事件记录了各类计数
        ev = _events(store, "profile.refresh")
        assert len(ev) == 1
        assert '"add": 1' in (ev[0]["payload"] or "")

    @pytest.mark.asyncio
    async def test_add_accepts_chinese_category_names(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        """线上实测（2026-09-27）：真模型把 category 写成「话题」「兴趣」，
        全被当成非法类别丢掉，群画像一直是空的。中文叫法也要认。"""
        import json as _j
        reply = _j.dumps({"ops": [
            {"op": "add", "id": "topic-x", "category": "话题", "text": "在聊黎明之血像巫师三", "evidence": [1]},
            {"op": "add", "category": "兴趣", "text": "在意 miniled 屏", "evidence": [2]},
            {"op": "add", "category": "在做的事", "text": "有人在装 NAS", "evidence": [1]},
            {"op": "add", "category": "约定和说法", "text": "周五是分享夜", "evidence": [2]},
            {"op": "add", "category": "常用资源", "text": "常发 B 站链接", "evidence": [3]},
            {"op": "add", "category": "乱写的", "text": "这条不认", "evidence": [3]},
        ]}, ensure_ascii=False)
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 200 + i * 10, text=f"消息{i}") for i in range(3)])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        cats = sorted(r["category"] for r in store.read().execute(
            "SELECT category FROM profile_entries WHERE group_id=? AND deleted=0", (GID,)))
        assert cats == sorted(["recent", "interest", "ongoing", "convention", "resource"])

    @pytest.mark.asyncio
    async def test_prompt_lists_exact_category_codes(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=['{"ops": []}'])
        host = FakeHost([_msg(f"m{i}", T0 - 200 + i * 10, text=f"消息{i}") for i in range(3)])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        text = str(models.calls[0][1])
        for code in ("recent", "interest", "ongoing", "convention", "resource"):
            assert code in text

    @pytest.mark.asyncio
    async def test_add_rejects_bad_category_and_empty_text(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        reply = (
            '{"ops": ['
            '{"op": "add", "category": "瞎编", "text": "不接受"},'
            '{"op": "add", "category": "recent", "text": "  "},'
            '{"op": "add", "category": "interest", "text": "合法的"}'
            "]}"
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        rows = store.read().execute(
            "SELECT category, text FROM profile_entries WHERE group_id=?", (GID,)
        ).fetchall()
        assert [(r["category"], r["text"]) for r in rows] == [("interest", "合法的")]

    @pytest.mark.asyncio
    async def test_update_changes_text(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "旧说法")
        with store.tx() as conn:  # 管理员条目默认锁，这里解锁模拟模型条目
            conn.execute("UPDATE profile_entries SET locked=0 WHERE id=?", (eid,))
        models = FakeModelsQueue(
            replies=[f'{{"ops": [{{"op": "update", "id": {eid}, "text": "新说法"}}]}}']
        )
        p = _make(store, host, models, settings)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT text FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["text"] == "新说法"

    @pytest.mark.asyncio
    async def test_locked_entry_immune_to_update_and_remove(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "锁住的")  # add_entry 自带 locked=1
        models = FakeModelsQueue(
            replies=[
                f'{{"ops": [{{"op": "update", "id": {eid}, "text": "想改"}},'
                f'{{"op": "remove", "id": {eid}}}]}}'
            ]
        )
        p = _make(store, host, models, settings)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT text, deleted FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["text"] == "锁住的"
        assert row["deleted"] == 0  # 锁定条目 remove 不生效

    @pytest.mark.asyncio
    async def test_model_remove_is_not_tombstone_and_readdable(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "周三开黑")
        with store.tx() as conn:
            conn.execute("UPDATE profile_entries SET locked=0 WHERE id=?", (eid,))
        models = FakeModelsQueue(
            replies=[
                f'{{"ops": [{{"op": "remove", "id": {eid}}}]}}',
                '{"ops": [{"op": "add", "category": "recent", "text": "周三开黑", "evidence": [1]}]}',
            ]
        )
        p = _make(store, host, models, settings)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT deleted FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["deleted"] == 2  # 模型删 = 2；不是管理员墓碑 1
        assert p.entries(GID) == []  # 管理员视图看不见
        # 之后同文字照样能再加（deleted=2 不当墓碑）
        host.msgs.extend([_msg(f"n{i}", T0 - 50 + i) for i in range(3)])
        await p.tick(GID)
        live = p.entries(GID)
        assert any(e["text"] == "周三开黑" for e in live)

    @pytest.mark.asyncio
    async def test_tombstone_blocks_readd_of_similar_text(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text="装修") for i in range(3)])
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "最近在聊装修报价")
        p.delete_entry(eid)  # 管理员删 → 墓碑
        models = FakeModelsQueue(
            replies=[
                '{"ops": ['
                '{"op": "add", "category": "recent", "text": "最近在聊装修的报价", "evidence": [1]},'  # 相似 → 拒
                '{"op": "add", "category": "interest", "text": "完全不同的主题", "evidence": [2]}'
                "]}"
            ]
        )
        p = _make(store, host, models, settings)
        await p.tick(GID)
        live = p.entries(GID)
        assert [e["text"] for e in live] == ["完全不同的主题"]
        # 原墓碑还是 deleted=1
        row = store.read().execute(
            "SELECT deleted, text FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["deleted"] == 1

    @pytest.mark.asyncio
    async def test_add_duplicate_of_live_entry_becomes_touch(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "群里都在折腾 NAS 部署的事")
        with store.tx() as conn:
            conn.execute(
                "UPDATE profile_entries SET locked=0, source='model' WHERE id=?", (eid,)
            )
        models = FakeModelsQueue(
            replies=[
                '{"ops": [{"op": "add", "category": "recent",'
                ' "text": "群里都在折腾 NAS 部署的事情", "evidence": [1, 2]}]}'
            ]
        )
        p = _make(store, host, models, settings)
        await p.tick(GID)
        rows = p.entries(GID)
        assert len(rows) == 1  # 没新开条目
        row = store.read().execute(
            "SELECT evidence_count, last_ts, evidence FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        assert row["evidence_count"] == 2  # touch 加了引用数
        assert row["last_ts"] == T0
        import json as _j
        assert _j.loads(row["evidence"]) == ["m0", "m1"]

    @pytest.mark.asyncio
    async def test_touch_refreshes_and_evidence_merges(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "长期话题")
        with store.tx() as conn:
            conn.execute(
                "UPDATE profile_entries SET locked=0, source='model', evidence='[\"old1\"]' WHERE id=?",
                (eid,),
            )
        models = FakeModelsQueue(
            replies=[f'{{"ops": [{{"op": "touch", "id": {eid}, "evidence": [2]}}]}}']
        )
        p = _make(store, host, models, settings)
        await p.tick(GID)
        row = store.read().execute(
            "SELECT evidence, evidence_count, last_ts FROM profile_entries WHERE id=?", (eid,)
        ).fetchone()
        import json as _j
        assert _j.loads(row["evidence"]) == ["old1", "m1"]
        assert row["evidence_count"] == 1  # 原 0 + 这次 1 条
        assert row["last_ts"] == T0

    @pytest.mark.asyncio
    async def test_ops_on_unknown_id_are_ignored(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        reply = '{"ops": [{"op": "update", "id": 999, "text": "幽灵"}, {"op": "touch", "id": 999}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        assert p.entries(GID) == []


# ----------------------------------------------------------------------
# 解析容错与失败重试
# ----------------------------------------------------------------------


class TestParsingAndFailure:
    @pytest.mark.asyncio
    async def test_json_fence_is_tolerated(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        fenced = '```json\n{"ops": [{"op": "add", "category": "recent", "text": "围栏里的", "evidence": [1]}]}\n```'
        models = FakeModelsQueue(replies=[fenced])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.refreshed is True
        assert [e["text"] for e in p.entries(GID)] == ["围栏里的"]

    @pytest.mark.asyncio
    async def test_garbage_then_next_tick_retries_without_losing_pending(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue(
            # 读不懂会当场带「格式不对」再问一次（2026-09-29）：两次都读不懂才算这次失败
            replies=["不是 JSON", "还是看不懂", '{"ops": [{"op": "add", "category": "recent", "text": "重试成功"}]}']
        )
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.refreshed is False
        row = _group(store)
        assert row["fail_count"] == 1
        assert row["pending_count"] == 3  # 没推进，下次重试
        # 下一次 tick（含新消息）重试成功
        host.msgs.append(_msg("m3", T0 - 10))
        r = await p.tick(GID)
        assert r.refreshed is True
        row = _group(store)
        assert row["fail_count"] == 0
        assert row["pending_count"] == 0
        assert [e["text"] for e in p.entries(GID)] == ["重试成功"]

    @pytest.mark.asyncio
    async def test_model_error_counts_as_failure(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=[ModelError("端点 500")])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.refreshed is False
        assert _group(store)["fail_count"] == 1

    @pytest.mark.asyncio
    async def test_three_failures_skip_batch_and_advance(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path, batch_messages=3)
        models = FakeModelsQueue(
            # 每次 tick：读不懂的会再问一次，所以一次失败 = 两句垃圾；模型报错不再问
            replies=["垃圾1", "垃圾2", ModelError("炸了"), "垃圾3", "垃圾4", '{"ops": []}']
        )
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        for _ in range(3):  # 三次 tick 都失败
            await p.tick(GID)
        row = _group(store)
        assert row["fail_count"] == 0  # 跳过这批后清零
        assert row["pending_count"] == 0
        assert row["last_refresh_ts"] > 0  # 游标推进了
        skips = _events(store, "profile.skip")
        assert len(skips) == 1
        # 第 4 次 tick：窗口前移，旧消息不再喂给模型
        host.msgs.append(_msg("n1", T0 - 5))
        await p.tick(GID)
        # 最后一通调用的消息里不应该有 m0（已跳过的批）
        last_messages = models.calls[-1][1]
        assert "m0" not in str(last_messages)

    @pytest.mark.asyncio
    async def test_bad_structure_counts_as_failure(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        models = FakeModelsQueue(replies=['{"ops": "不是列表"}', '{"ops": "还不是列表"}'])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.refreshed is False
        assert _group(store)["fail_count"] == 1


# ----------------------------------------------------------------------
# people 注记与提示词内容
# ----------------------------------------------------------------------


class TestPeopleAndPrompt:
    def _seed_focus(self, store: Store, gid: str = GID) -> None:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, reasons, note,"
                " pinned, removed, updated) VALUES (?, 'ufan', '阿帆', '[]', '', 0, 0, 0),"
                " (?, 'urem', '路人', '[]', '', 0, 1, 0)",
                (gid, gid),
            )

    @pytest.mark.asyncio
    async def test_people_note_written_and_outsiders_ignored(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        self._seed_focus(store)
        long_note = "爱聊硬件，最近在折腾风扇调速，常给新人解答问题" * 10
        reply = (
            '{"ops": [], "people": ['
            '{"user_id": "ufan", "note": "' + long_note + '"},'
            '{"user_id": "urem", "note": "已移除的不该写"},'
            '{"user_id": "ustranger", "note": "不在名单不该写"}'
            "]}"
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        rows = store.read().execute(
            "SELECT user_id, note FROM focus_members WHERE group_id=? ORDER BY user_id", (GID,)
        ).fetchall()
        notes = {r["user_id"]: r["note"] for r in rows}
        assert notes["ufan"] == long_note[:120]  # 截 120 字
        assert notes["urem"] == ""  # removed=1 不写
        # 提示词里带了关注成员名单
        prompt = models.calls[0][1][1]["content"]
        assert "ufan" in prompt and "阿帆" in prompt

    @pytest.mark.asyncio
    async def test_personal_profile_off_hides_members_and_skips_notes(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path, personal_profile=False)
        self._seed_focus(store)
        reply = '{"ops": [], "people": [{"user_id": "ufan", "note": "不该写"}]}'
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg("m0", T0 - 100, user="ufan", name="阿帆", text="在吗")]
                        + [_msg(f"m{i}", T0 - 99 + i) for i in range(1, 3)])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        # 提示词里没有成员名单段（名单段落的特征行；规则说明文字可以有）
        prompt = models.calls[0][1][1]["content"]
        assert "只给他们写 people 注记" not in prompt
        assert "user_id=ufan" not in prompt
        # 也不写 note
        row = store.read().execute(
            "SELECT note FROM focus_members WHERE group_id=? AND user_id='ufan'", (GID,)
        ).fetchone()
        assert row["note"] == ""

    @pytest.mark.asyncio
    async def test_prompt_includes_entries_tombstones_and_message_format(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        host = FakeHost(
            [
                _msg("m0", T0 - 100, text="短的"),
                _msg("m1", T0 - 90, user="botqq", bot=True, text="机器人的话"),
                _msg("m2", T0 - 80, text="长" * 500),
            ]
        )
        p = _make(store, host, FakeModelsQueue(), settings)
        eid = p.add_entry(GID, "recent", "已有条目")
        with store.tx() as conn:
            conn.execute("UPDATE profile_entries SET locked=0 WHERE id=?", (eid,))
        p.add_entry(GID, "convention", "锁定示例")
        tomb = p.add_entry(GID, "recent", "管理员讨厌的")
        p.delete_entry(tomb)
        models = FakeModelsQueue(replies=['{"ops": []}'])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        prompt = models.calls[0][1][1]["content"]
        # 当前条目（带 id、类别、锁定标记）
        assert f"#{eid}" in prompt and "[recent]" in prompt and "已有条目" in prompt
        assert "锁定示例" in prompt and "（锁定）" in prompt
        # 墓碑文字给出且声明不许加回
        assert "管理员讨厌的" in prompt and "不许再加回来" in prompt
        # 消息格式 + 机器人叫 MaiBot + 200 字截断
        assert "[1]" in prompt and "阿一: 短的" in prompt
        assert "MaiBot: 机器人的话" in prompt
        long_line = [ln for ln in prompt.splitlines() if "长" in ln and ": " in ln][0]
        assert len(long_line.split(": ", 1)[1]) == 200


# ----------------------------------------------------------------------
# 每周整理（weekly）
# ----------------------------------------------------------------------


class TestWeekly:
    # T0 是北京周六（weekday()==5），weekly_day=5 才对得上
    def _make_ready(
        self,
        store: Store,
        host: FakeHost,
        settings,
        *,
        weekly_day: int = 5,
        last_weekly_ts: float = 0.0,
    ):
        p = _make(store, host, FakeModelsQueue(), settings)
        p.ensure_group(GID)
        with store.tx() as conn:
            conn.execute(
                "UPDATE groups SET profile_ready_ts=?, last_refresh_ts=?,"
                " last_weekly_ts=? WHERE group_id=?",
                (T0 - 86400, T0, last_weekly_ts, GID),
            )
        return p

    @pytest.mark.asyncio
    async def test_weekly_runs_on_right_day_and_applies_remove(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path, weekly_day=5, batch_messages=1000)
        host = FakeHost()  # 无新消息；weekly 不读消息
        p = self._make_ready(store, host, settings)
        stale = p.add_entry(GID, "recent", "过时快讯")
        keep = p.add_entry(GID, "interest", "常青话题")
        with store.tx() as conn:
            conn.execute(
                "UPDATE profile_entries SET locked=0, last_ts=? WHERE id=?",
                (T0 - 20 * 86400, stale),
            )
            conn.execute("UPDATE profile_entries SET locked=0 WHERE id=?", (keep,))
        chat = FakeModelsQueue(
            replies=[f'{{"ops": [{{"op": "add", "category": "recent", "text": "不该有"}},'
                     f'{{"op": "touch", "id": {keep}}},'
                     f'{{"op": "remove", "id": {stale}}}]}}']
        )
        models = chat
        p = _make(store, host, models, settings)
        res = await p.tick(GID)
        assert len(models.calls) == 1  # 只有 weekly 这一次调用（没有提炼）
        purpose = models.calls[0][2].get("purpose", "")
        assert "weekly" in purpose
        prompt = models.calls[0][1][1]["content"]
        assert "过时快讯" in prompt and "常青话题" in prompt
        # add/touch 被过滤，只剩 remove 生效
        row = store.read().execute(
            "SELECT deleted FROM profile_entries WHERE id=?", (stale,)
        ).fetchone()
        assert row["deleted"] == 2
        assert p.entries(GID) == [e for e in p.entries(GID) if e["text"] == "常青话题"]
        assert _group(store)["last_weekly_ts"] > 0

    @pytest.mark.asyncio
    async def test_weekly_skips_wrong_day_and_recent_gap(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path, weekly_day=1)  # 周二 ≠ 周六
        host = FakeHost()
        p = self._make_ready(store, host, settings)
        models = FakeModelsQueue(replies=['{"ops": []}'])
        p = _make(store, host, models, settings)
        await p.tick(GID)
        assert models.calls == []
        # 日期对但间隔不够（3 天前才整过）
        settings2 = _settings(tmp_path, weekly_day=5)
        p2 = self._make_ready(store, host, settings2, last_weekly_ts=T0 - 3 * 86400)
        models2 = FakeModelsQueue(replies=['{"ops": []}'])
        p2 = _make(store, host, models2, settings2)
        await p2.tick(GID)
        assert models2.calls == []
        # 画像没成形（profile_ready_ts==0）不整
        settings3 = _settings(tmp_path, weekly_day=5)
        models3 = FakeModelsQueue(replies=['{"ops": []}'])
        p3 = _make(store, host, models3, settings3)
        p3.ensure_group(GID)
        await p3.tick(GID)
        assert models3.calls == []


# ----------------------------------------------------------------------
# PROFILE.md
# ----------------------------------------------------------------------


class TestProfileMd:
    @pytest.mark.asyncio
    async def test_profile_md_written_with_sections_without_member_info(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        ws_dir = tmp_path / "wsroot" / "tinker"
        ws_dir.mkdir(parents=True)
        self_note = {"user_id": "ufan", "note": "只给管理员看的注记"}
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, pinned,"
                " removed, updated) VALUES (?, 'ufan', '阿帆', '', 0, 0, 0)",
                (GID,),
            )
        reply = (
            '{"ops": ['
            '{"op": "add", "category": "ongoing", "text": "在给服务迁移数据库"},'
            '{"op": "add", "category": "resource", "text": "官方 wiki 地址"}'
            '], "people": [' + '{"user_id": "ufan", "note": "只给管理员看的注记"}' + "]}"
        )
        models = FakeModelsQueue(replies=[reply])
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i, text=f"x{i}") for i in range(3)])
        p = _make(store, host, models, settings)
        p.ensure_group(GID)
        with store.tx() as conn:
            conn.execute("UPDATE groups SET name='折腾研究所' WHERE group_id=?", (GID,))
        r = await p.tick(GID)
        assert r.refreshed is True
        # G6 起文件名带群号：PROFILE-<群号>.md（共享工作区不再互相覆盖）
        md = (ws_dir / f"PROFILE-{GID}.md").read_text(encoding="utf-8")
        assert "折腾研究所 群画像" in md
        for name in ("最近在聊", "长期兴趣", "在做的事", "约定和说法", "常用资源"):
            assert name in md
        assert "在给服务迁移数据库" in md
        assert "官方 wiki 地址" in md
        # 不含任何关注成员信息
        assert "ufan" not in md
        assert "阿帆" not in md
        assert "只给管理员看的注记" not in md
        # 但关注成员的 note 确实写进了库（个人画像照常记录，只是不进 PROFILE.md）
        row = store.read().execute(
            "SELECT note FROM focus_members WHERE group_id=? AND user_id='ufan'", (GID,)
        ).fetchone()
        assert row["note"] == "只给管理员看的注记"

    @pytest.mark.asyncio
    async def test_profile_md_missing_dir_skips_silently(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        # wsroot/tinker 不存在：不创建、不报错
        models = FakeModelsQueue(
            replies=['{"ops": [{"op": "add", "category": "recent", "text": "有变化"}]}']
        )
        host = FakeHost([_msg(f"m{i}", T0 - 100 + i) for i in range(3)])
        p = _make(store, host, models, settings)
        r = await p.tick(GID)
        assert r.refreshed is True  # 提炼照样成功
        assert not (tmp_path / "wsroot" / "tinker").exists()  # 没有顺手建目录
        assert p.entries(GID)[0]["text"] == "有变化"  # 条目正常入库


# ----------------------------------------------------------------------
# 2026-09-29：一行一件事的输出格式；读不懂不算成功；超时 / 失败拆半
# 线上实测：step-5-preview 开 JSON 模式时 29 次里 26 次没按 {"ops":[…]} 回（只给一条 / 压扁 / 回空 {}），
# 老代码把「没有 ops 键」当成「没变化」照样收账，群画像一整天几乎没更新；超时同一批原样重试 18 次后整批跳过。
# ----------------------------------------------------------------------


class TestLineFormat:
    def _host(self, n=3):
        return FakeHost([_msg(f"m{i}", T0 - 500 + i * 5, user=f"u{i % 3}", text=f"消息{i}") for i in range(n)])

    @pytest.mark.asyncio
    async def test_lines_applied(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        import json as _j

        settings = _settings(tmp_path)
        host = self._host()
        models = FakeModelsQueue(replies=["新增 | 长期兴趣 | 喜欢开源硬件 | 1\n新增 | 最近在聊 | 在聊旧话题 | 2"])
        p = _make(store, host, models, settings)
        assert (await p.tick(GID)).refreshed is True
        ids = {e["text"]: e["id"] for e in p.entries(GID)}
        old, gone = ids["喜欢开源硬件"], ids["在聊旧话题"]
        # 第二批：新 3 条消息
        host.msgs.extend(_msg(f"n{i}", T0 - 100 + i * 5, text=f"新消息{i}") for i in range(3))
        reply = "\n".join([
            "新增 | 最近在聊 | 在聊新出的掌机 | 1,3",
            f"修改 | #{old} | 喜欢开源硬件和 FPGA",
            f"删除 | #{gone}",
            "新增 | 约定和说法 | A|B 两个方案都叫「快板」 | 2",
        ])
        models.reply_queue.append(reply)
        r = await p.tick(GID)
        assert r.refreshed is True
        texts = {e["text"]: e for e in p.entries(GID)}
        assert "在聊新出的掌机" in texts and texts["在聊新出的掌机"]["category"] == "recent"
        assert "喜欢开源硬件和 FPGA" in texts
        assert "在聊旧话题" not in texts
        assert "A|B 两个方案都叫「快板」" in texts
        row = store.read().execute(
            "SELECT evidence FROM profile_entries WHERE text=?", ("在聊新出的掌机",)).fetchone()
        # 第二批窗口含上一批最后那条（m2），序号 1、3 → m2、n1
        assert _j.loads(row["evidence"]) == ["m2", "n1"]
        # 调用：不开 JSON 模式、等待上限 420 秒（给到 16000 token）、超时只重试 1 次
        kw = models.calls[0][2]
        assert kw.get("json_mode") is False
        assert kw.get("timeout") == 420
        # 网关约 129 秒就断连接（流式也一样）：同一批原样重试没用，失败直接拆半
        assert kw.get("retries") == 0

    @pytest.mark.asyncio
    async def test_prompt_describes_line_format(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        models = FakeModelsQueue(replies=["没有变化"])
        p = _make(store, self._host(), models, _settings(tmp_path))
        await p.tick(GID)
        text = str(models.calls[0][1])
        assert "新增 |" in text and "没有变化" in text and "一行一件事" in text

    @pytest.mark.asyncio
    async def test_no_change_line_is_success(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        models = FakeModelsQueue(replies=["没有变化"])
        p = _make(store, self._host(), models, _settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        assert _group(store)["pending_count"] == 0
        assert len(models.calls) == 1

    @pytest.mark.asyncio
    async def test_bad_lines_skipped_good_lines_kept(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        reply = "好的，结果如下：\n- 新增 | 最近在聊 | 在聊巫师三 | 1\n乱七八糟的一行\n新增 | 不存在的类别 | 这条不认 | 2"
        models = FakeModelsQueue(replies=[reply])
        p = _make(store, self._host(), models, _settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        assert [e["text"] for e in p.entries(GID)] == ["在聊巫师三"]

    @pytest.mark.asyncio
    async def test_people_and_asks_lines(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id) VALUES (?) ON CONFLICT(group_id) DO NOTHING", (GID,))
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, removed) VALUES (?, 'u1', '阿一', 0)", (GID,))
        reply = "关注成员 | u1 | 最近在做开源掌机项目\n没有变化"
        models = FakeModelsQueue(replies=[reply])
        p = _make(store, self._host(), models, _settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        note = store.read().execute(
            "SELECT note FROM focus_members WHERE group_id=? AND user_id='u1'", (GID,)).fetchone()["note"]
        assert "开源掌机" in note

    @pytest.mark.asyncio
    async def test_old_json_single_op_is_salvaged(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        """线上 step-5-preview 常见回法：只给一条 op、没套 {"ops":[…]}——内容是对的，收下。"""
        reply = '{"op":"add","id":"#38","category":"recent","text":"群友分享战锤40K新武器视频","evidence":[1]}'
        models = FakeModelsQueue(replies=[reply])
        p = _make(store, self._host(), models, _settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        assert [e["text"] for e in p.entries(GID)] == ["群友分享战锤40K新武器视频"]

    @pytest.mark.asyncio
    async def test_unreadable_asks_again_then_applies(self, store: Store, frozen_now: float, tmp_path: Path) -> None:
        """回空 {} / 压扁成重复键：读不懂 → 带着「格式不对」再问一次。"""
        flattened = '{"op":"add","category":"recent","text":"甲","op":"add","category":"recent","text":"乙"}'
        models = FakeModelsQueue(replies=[flattened, "新增 | 最近在聊 | 重问后读懂了 | 1"])
        p = _make(store, self._host(), models, _settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is True
        assert [e["text"] for e in p.entries(GID)] == ["重问后读懂了"]
        assert len(models.calls) == 2
        second = str(models.calls[1][1])
        assert "格式" in second and "没有变化" in second

    @pytest.mark.asyncio
    async def test_empty_object_twice_is_failure_not_success(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        models = FakeModelsQueue(replies=["{}", "{}"])
        p = _make(store, self._host(), models, _settings(tmp_path))
        r = await p.tick(GID)
        assert r.refreshed is False
        row = _group(store)
        assert row["fail_count"] == 1
        assert row["pending_count"] == 3  # 没收账，下次还能再整理

    @pytest.mark.asyncio
    async def test_big_batch_split_in_half_after_failure(
        self, store: Store, frozen_now: float, tmp_path: Path
    ) -> None:
        """一批（≥40 条）整批失败：拆成两半各试一次，而不是原样重复。"""
        models = FakeModelsQueue(replies=[
            ModelError("网络错误（ReadTimeout）"),
            "新增 | 最近在聊 | 前半段的话题 | 1",
            "新增 | 最近在聊 | 后半段的话题 | 1",
        ])
        p = _make(store, self._host(60), models, _settings(tmp_path, batch_messages=3))
        r = await p.tick(GID)
        assert r.refreshed is True
        assert sorted(e["text"] for e in p.entries(GID)) == ["前半段的话题", "后半段的话题"]
        assert len(models.calls) == 3
        assert "[31]" not in str(models.calls[1][1]) and "[30]" in str(models.calls[1][1])
        assert _group(store)["pending_count"] == 0 and _group(store)["fail_count"] == 0


class TestReadWindowPaging:
    """2026-09-29 线上补整理：宿主一页 200 条里有通知/缺 id 被过滤，拿回来不满 200 条
    就被当成「读完了」，每轮只整理约一页，后面几千条卡住等新消息。"""

    def test_short_page_after_filtering_keeps_paging(self, store, tmp_path) -> None:
        import asyncio
        from CharTyr_MaiWork.maiwork import profile as profile_mod

        msgs = [_msg(f"m{i}", T0 - 5000 + i) for i in range(500)]

        class FilteringHost(FakeHost):
            async def messages(self, session_id, start, end, limit, *, limit_mode="latest"):
                page = await super().messages(session_id, start, end, limit, limit_mode=limit_mode)
                # 模拟 host.messages 过滤掉通知：每页少几条
                return [m for m in page if not m.id.endswith("7")]

        host = FilteringHost(msgs)
        p = _make(store, host, FakeModelsQueue(ready=True), _settings(tmp_path))
        got = asyncio.run(p._read_window("sess-1", T0 - 6000, T0))
        want = [m.id for m in msgs if not m.id.endswith("7")]
        assert [m.id for m in got] == want
        assert len(host.msg_calls) <= profile_mod._MAX_PAGES


class TestTruncatedAnswer:
    """2026-09-29 线上：glm-5.3-flash 把思考写在正文里，8192 token 截断；截断的草稿里
    夹着「新增 | …」草稿行，被当成功读进了画像。截断的一律不读，再问一次。"""

    def _prep(self, store, tmp_path, replies):
        msgs = [_msg(f"m{i}", T0 - 100 + i, text=f"聊游戏{i}") for i in range(3)]
        host = FakeHost(msgs)
        models = FakeModelsQueue(ready=True, replies=replies)
        p = _make(store, host, models, _settings(tmp_path))
        return p, models

    def _entries(self, store):
        return store.read().execute(
            "SELECT text FROM profile_entries WHERE group_id=?", (GID,)
        ).fetchall()

    def test_truncated_draft_not_applied_then_retry_used(self, store, tmp_path, frozen_now) -> None:
        import asyncio
        draft = "Let me think...\n新增 | 最近在聊 | 草稿里的半截话 | 1\nhmm, maybe"
        p, models = self._prep(store, tmp_path, [
            {"text": draft, "finish_reason": "length"},
            "新增 | 最近在聊 | 群友在聊新游戏 | 1,2",
        ])
        asyncio.run(p.tick(GID, refresh=False))
        asyncio.run(p.refresh(GID, force=True))
        texts = [r["text"] for r in self._entries(store)]
        assert not any("草稿" in t for t in texts), texts
        assert any("新游戏" in t for t in texts), texts
        assert len(models.calls) == 2

    def test_truncated_twice_is_failure(self, store, tmp_path, frozen_now) -> None:
        import asyncio
        draft = {"text": "新增 | 最近在聊 | 草稿 | 1", "finish_reason": "length"}
        p, models = self._prep(store, tmp_path, [draft, draft])
        asyncio.run(p.tick(GID, refresh=False))
        asyncio.run(p.refresh(GID, force=True))
        assert self._entries(store) == []
        assert int(_group(store)["fail_count"]) == 1

    def test_refresh_asks_for_larger_output_budget(self, store, tmp_path, frozen_now) -> None:
        import asyncio
        p, models = self._prep(store, tmp_path, ["没有变化"])
        asyncio.run(p.tick(GID, refresh=False))
        asyncio.run(p.refresh(GID, force=True))
        kw = models.calls[0][2]
        assert 8000 <= kw.get("max_tokens", 0) <= 12000
        assert kw.get("timeout", 0) >= 120


def test_refresh_batch_small_enough_for_gateway_limit() -> None:
    """2026-09-29 线上：网关约 129 秒断连接；300 条一批 glm 要想 100~140 秒，常被断。"""
    assert Profiles._REFRESH_BATCH <= 120
