"""库号 51 可观测性回填测试（runtime_bus / SSE / TTFT / context_map / Jev 概率）。

覆盖：
- 迁移 51：新列 / 新表齐了，重跑幂等；settings_changes 按「90 天 + 最新 500」清。
- RuntimeBus：白名单字段 / 服务群闸门 / 有界最近环 / 订阅队列满丢最旧 / 在途生命周期。
- build_context_map：分组归类、线性校准 = 实测、绝不抛。
- Jev：judgments.probs 落库、last_detail（ContextVar 并发不串台）、jev_gate 事件。
- 待判 @：decision.jev 白名单存库（views 的 jev 键同源）。
- 看板：live 快照形状 / think 步的 first_token_ms·context_map / 详情 context_map。
- SSE /api/board/events：鉴权、起手 snapshot、runtime 推送、stop() 不被流拖住。
- models.chat 真链路：流式 TTFT 进 model_calls.first_token_ms、context_map 校准入库、
  总线收到 start/first_token/done 三段。
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console import views
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.console.board import _latest_context_map, _live_view
from CharTyr_MaiWork.maiwork.console.board.trace import Trace, _think_step
from CharTyr_MaiWork.maiwork.jev import Jev
from CharTyr_MaiWork.maiwork.models import (
    Models,
    _calibrate_context_map,
    build_context_map,
)
from CharTyr_MaiWork.maiwork.pending_asks import PendingAsks
from CharTyr_MaiWork.maiwork.runtime_bus import RECENT_MAX, SUB_QUEUE_MAX, RuntimeBus
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
SECRET = "typesafe-testkey-123"     # 假密钥：只用来断言「绝不外泄」
JEV_SECRET = "typesafe-testkey-123"


def _settings(cfg: dict | None = None) -> object:
    merged = {"groups": {"serve": [{"group": f"qq:{G1}"}]}}
    if cfg:
        merged.update(cfg)
    settings, _ = load_settings(merged)
    return settings


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _cols(store: Store, table: str) -> set:
    return {str(r["name"]) for r in store.read().execute(f"PRAGMA table_info({table})")}


def _tables(store: Store) -> set:
    return {
        str(r["name"])
        for r in store.read().execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


# ----------------------------------------------------------------------
# 迁移 51：表 / 列 / 幂等 / settings_changes 清理
# ----------------------------------------------------------------------


class TestMigration51:
    def test_columns_and_tables_exist(self, store: Store) -> None:
        assert {"first_token_ms", "context_map"} <= _cols(store, "model_calls")
        assert "probs" in _cols(store, "judgments")
        assert {"settings_changes", "agent_index_notes", "agent_index_note_versions"} <= _tables(store)

    def test_migrate_rerun_is_idempotent(self, store: Store) -> None:
        assert store.migrate() == 51
        assert store.migrate() == 51

    def test_prune_settings_changes_90d_and_cap(self, store: Store) -> None:
        now = clock.now()
        with store.tx() as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO settings_changes (ts, actor, scope, target_key, summary)"
                    " VALUES (?, 'admin_web', 'global', ?, '改')",
                    (now - 91 * 86400 - i, f"k_old_{i}"),
                )
            for i in range(505):
                conn.execute(
                    "INSERT INTO settings_changes (ts, actor, scope, target_key, summary)"
                    " VALUES (?, 'admin_web', 'global', ?, '改')",
                    (now - 100 - i * 0.001, f"k_{i}"),
                )
        counts = store.prune(now)
        assert counts["settings_changes"] == 3 + 5
        left = store.read().execute("SELECT COUNT(*) AS c FROM settings_changes").fetchone()
        assert int(left["c"]) == 500
        # 留的是最新的 500 条（k_0..k_499），最老的 k_504 顶掉
        keys = {
            str(r["target_key"])
            for r in store.read().execute("SELECT target_key FROM settings_changes")
        }
        assert "k_0" in keys and "k_504" not in keys and "k_old_0" not in keys


# ----------------------------------------------------------------------
# RuntimeBus：闸门 / 有界环 / 订阅 / 在途
# ----------------------------------------------------------------------


class TestRuntimeBus:
    def test_emit_whitelist_and_badge_clip(self) -> None:
        bus = RuntimeBus()
        e = bus.emit(
            "model_call", stage="main", status="done", group_id=G1,
            role="main", model="m", ms=12, badge="长" * 40,
        )
        assert e is not None
        assert set(e) == {
            "seq", "ts", "kind", "stage", "status", "group_id", "role",
            "model", "flight_id", "ms", "first_token_ms", "ok", "badge",
        }
        assert len(e["badge"]) <= 24
        assert e["seq"] == 1

    def test_unserved_group_dropped_admin_passes(self) -> None:
        bus = RuntimeBus(is_served=lambda gid: gid == G1)
        assert bus.emit("signal", stage="signal", group_id="999") is None
        assert bus.emit("signal", stage="signal", group_id=G1) is not None
        assert bus.emit("signal", stage="signal", group_id="admin") is not None
        snap = bus.snapshot()
        assert len(snap["recent"]) == 2
        assert all(e["group_id"] in (G1, "admin") for e in snap["recent"])

    def test_recent_ring_is_bounded(self) -> None:
        bus = RuntimeBus()
        for i in range(RECENT_MAX + 10):
            bus.emit("signal", stage="signal", status="done", ms=i)
        snap = bus.snapshot()
        assert len(bus._recent) == RECENT_MAX
        assert len(snap["recent"]) == 30
        # 最旧的被挤出去了
        assert snap["recent"][0]["seq"] == RECENT_MAX + 10 - 29

    def test_active_flight_lifecycle(self) -> None:
        bus = RuntimeBus()
        bus.emit("model_call", stage="main", status="start",
                 group_id=G1, flight_id="call-1", model="m")
        snap = bus.snapshot()
        assert [f["flight_id"] for f in snap["active"]] == ["call-1"]
        bus.emit("model_call", stage="main", status="first_token",
                 group_id=G1, flight_id="call-1", first_token_ms=321)
        snap = bus.snapshot()
        assert snap["active"][0]["first_token_ms"] == 321
        bus.emit("model_call", stage="main", status="done",
                 group_id=G1, flight_id="call-1", ms=999)
        assert bus.snapshot()["active"] == []

    @pytest.mark.asyncio
    async def test_subscribe_gets_events_and_full_queue_drops_oldest(self) -> None:
        bus = RuntimeBus()
        sub_id, queue = bus.subscribe()
        try:
            bus.emit("signal", stage="signal", status="done", ms=1)
            e = await asyncio.wait_for(queue.get(), timeout=1)
            assert e["ms"] == 1
            # 灌满队列：容量 SUB_QUEUE_MAX，再发 5 条 → 最旧的 5 条被挤掉
            for i in range(SUB_QUEUE_MAX + 5):
                bus.emit("signal", stage="signal", status="done", ms=100 + i)
            got = []
            while not queue.empty():
                got.append(queue.get_nowait())
            assert len(got) == SUB_QUEUE_MAX
            assert got[-1]["ms"] == 100 + SUB_QUEUE_MAX + 5 - 1
        finally:
            bus.unsubscribe(sub_id)
        # 退订后不再收
        bus.emit("signal", stage="signal", status="done", ms=7)
        assert queue.empty()


# ----------------------------------------------------------------------
# build_context_map：分组 / 校准 / 不炸
# ----------------------------------------------------------------------


class TestContextMap:
    def test_grouping_system_rules_and_tool_io(self) -> None:
        msgs = [
            {"role": "system", "content":
             "你是 MaiWork。【本群规矩】别乱@人【本群做法】先想后说"},
            {"role": "user", "content": "今天怎么样"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "content": "工具结果"},
            {"role": "user", "content": "【前情摘要】之前聊了 A"},
        ]
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        m = build_context_map(msgs, tools, context_window=128000)
        assert m["limit_tokens"] == 128000
        groups = {g["group"] for g in m["groups"]}
        assert {"system", "rules", "skills", "tool_io", "summary", "history", "tools"} <= groups
        assert m["used_tokens"] == sum(p["tokens"] for p in m["parts"]) + 16
        assert m["calibrated"] is False
        # summary 标记的那条归 summary
        summ = [p for p in m["parts"] if p["group"] == "summary"]
        assert len(summ) == 1

    def test_calibration_scales_to_reported(self) -> None:
        msgs = [
            {"role": "system", "content": "短提示词"},
            {"role": "user", "content": "长" * 500},
        ]
        m = build_context_map(msgs, prompt_tokens=1000)
        assert m["calibrated"] is True
        assert m["used_tokens"] == 1000
        # parts 之和 + wrapper = used（舍入差并进最大段）
        assert sum(p["tokens"] for p in m["parts"]) + 16 == 1000

    def test_calibrate_no_reported_keeps_estimated(self) -> None:
        m = {"used_tokens": 50, "parts": [{"tokens": 30, "group": "history"}],
             "groups": [], "calibrated": False, "limit_tokens": 1}
        out = _calibrate_context_map(m, 0)
        assert out["calibrated"] is False and out["used_tokens"] == 50

    def test_never_throws_on_garbage(self) -> None:
        m = build_context_map(None)  # type: ignore[arg-type]
        assert m["parts"] == [] and m["used_tokens"] == 16  # 只剩请求壳
        m2 = build_context_map([{"role": 1, "content": None}], "x")  # type: ignore[arg-type]
        assert isinstance(m2, dict)


# ----------------------------------------------------------------------
# Jev：probs 落库 / last_detail / 闸门事件 / ContextVar 隔离
# ----------------------------------------------------------------------

_GOOD = {
    "ok": {"type": "noul", "noul": 0.8},
    "reason": {
        "type": "choice", "choice": "fine", "confidence": 0.9,
        "probabilities": {"left": 0.05, "open_question": 0.05, "mood": 0.05, "fine": 0.85},
    },
}
_STATE = {"messages": [{"speaker": "USER", "text": "最近那个项目怎么样"}]}
_QUESTIONS = {
    "ok": {"type": "noul", "instructions": "适合开话题吗"},
    "reason": {"type": "choice", "instructions": "为什么",
               "criteria": {"fine": "可以开", "left": "人都走了",
                            "open_question": "有问题没人回", "mood": "气氛不对"}},
}


def _make_jev(tmp_path, *, bus=None, responder=None):
    store = Store(tmp_path / "jev.db")
    store.migrate()
    settings = _settings()
    resp = responder or (lambda req: httpx.Response(200, json={"answers": _GOOD}))
    jev = Jev(store, lambda: settings, transport=httpx.MockTransport(resp),
              runtime_bus=bus)
    return store, jev


class TestJevObservability:
    @pytest.mark.asyncio
    async def test_probs_saved_and_last_detail(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", JEV_SECRET)
        store, jev = _make_jev(tmp_path)
        out = await jev.ask(_STATE, _QUESTIONS, purpose="topic_check", group_id=G1)
        assert out is not None
        row = store.read().execute(
            "SELECT probs, ms, ok FROM judgments ORDER BY id DESC LIMIT 1"
        ).fetchone()
        probs = json.loads(str(row["probs"]))
        assert probs["reason"]["fine"] == 0.85
        assert probs["ok"]["yes"] == 0.8 and round(probs["ok"]["no"], 4) == 0.2
        detail = jev.last_detail()
        assert detail is not None and detail["ok"] is True
        assert detail["probs"]["reason"]["fine"] == 0.85
        assert detail["ms"] >= 0

    @pytest.mark.asyncio
    async def test_last_detail_is_context_local(self, tmp_path, monkeypatch) -> None:
        """另一个 task（新 context）里 ask，不污染本 context 的 last_detail。"""
        monkeypatch.setenv("TYPESAFE_API_KEY", JEV_SECRET)
        store, jev = _make_jev(tmp_path)
        assert jev.last_detail() is None
        await asyncio.create_task(
            jev.ask(_STATE, _QUESTIONS, purpose="topic_check", group_id=G1)
        )
        # create_task 拷贝了 context：里面的 set 不回写这里
        assert jev.last_detail() is None
        await jev.ask(_STATE, _QUESTIONS, purpose="topic_check", group_id=G1)
        assert jev.last_detail() is not None

    @pytest.mark.asyncio
    async def test_gate_events_on_bus(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", JEV_SECRET)
        bus = RuntimeBus()
        store, jev = _make_jev(tmp_path, bus=bus)
        await jev.ask(_STATE, _QUESTIONS, purpose="topic_check", group_id=G1)
        events = [e for e in bus.snapshot()["recent"] if e["kind"] == "jev_gate"]
        statuses = [e["status"] for e in events]
        assert statuses[:1] == ["start"] and statuses[-1] == "done"
        assert events[-1]["badge"] == "冷场判断"
        assert events[-1]["stage"] == "gate"

    @pytest.mark.asyncio
    async def test_failure_marks_detail_not_ok(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", JEV_SECRET)
        store, jev = _make_jev(
            tmp_path, responder=lambda r: httpx.Response(500, json={"e": "x"}))
        assert await jev.ask(_STATE, _QUESTIONS, purpose="topic_check", group_id=G1) is None
        detail = jev.last_detail()
        assert detail is not None and detail["ok"] is False


# ----------------------------------------------------------------------
# 待判 @：decision.jev 白名单存库
# ----------------------------------------------------------------------


class TestPendingAsksJev:
    def test_decide_stores_jev_block(self, store: Store) -> None:
        pa = PendingAsks(store, lambda: _settings())
        pa.record_pending(
            G1, {"message_id": "m1", "user_id": "u1", "user_name": "阿一",
                 "text": "@bot 帮我整理", "timestamp": clock.now()}, "at")
        ok = pa.decide(G1, "m1", {
            "kind": "prepare", "title": "整理资料",
            "jev": {"probs": {"prepare": 0.8, "none": 0.2}, "ms": 120, "threshold": 0.7},
        })
        assert ok is True
        row = pa.get(G1, "m1")
        assert row["decision"]["jev"]["probs"]["prepare"] == 0.8
        assert row["decision"]["jev"]["ms"] == 120
        assert row["decision"]["jev"]["threshold"] == 0.7
        # views 的待复核条目同源给 jev 键
        item = views._ask_review_item(row, clock.now(), "群一")
        assert item["jev"]["probs"]["prepare"] == 0.8

    def test_decide_without_jev_keeps_old_shape(self, store: Store) -> None:
        pa = PendingAsks(store, lambda: _settings())
        pa.record_pending(
            G1, {"message_id": "m2", "user_id": "u1", "text": "x"}, "at")
        assert pa.decide(G1, "m2", {"kind": "none", "why": "闲聊"}) is True
        row = pa.get(G1, "m2")
        assert "jev" not in row["decision"]
        assert views._ask_review_item(row, clock.now(), "g")["jev"] == {}

    def test_jev_garbage_is_sanitized(self, store: Store) -> None:
        pa = PendingAsks(store, lambda: _settings())
        pa.record_pending(
            G1, {"message_id": "m3", "user_id": "u1", "text": "x"}, "at")
        assert pa.decide(G1, "m3", {
            "kind": "prepare", "jev": {"probs": {"p": "不是数字", "ok": 2.5},
                                       "ms": -5, "text": "群友原话不该进"},
        }) is True
        jev = pa.get(G1, "m3")["decision"]["jev"]
        assert "text" not in jev  # 白名单外一律丢
        assert jev["probs"] == {"ok": 1.0}  # 夹到 0..1，非法值丢


# ----------------------------------------------------------------------
# 看板：live 快照 / think 步 / 详情 context_map
# ----------------------------------------------------------------------


def _fake_ctx(store: Store | None = None) -> Any:
    conn = store.read() if store is not None else None

    def fetch_one(sql: str, args: tuple = ()) -> Any:
        if conn is None:
            return None
        try:
            return conn.execute(sql, tuple(args)).fetchone()
        except Exception:
            return None

    return types.SimpleNamespace(
        now=1000.0, conn=conn, fetch_one=fetch_one,
        group_name=lambda gid: f"群{gid}",
    )


class TestBoardLive:
    def test_live_view_without_bus_is_empty_shell(self) -> None:
        out = _live_view(_fake_ctx(), types.SimpleNamespace())
        assert out == {"seq": 0, "ts": 1000.0, "active": [], "recent": []}

    def test_live_view_enriches_group_name(self) -> None:
        bus = RuntimeBus()
        bus.emit("signal", stage="signal", status="done", group_id=G1)
        bus.emit("model_call", stage="main", status="start", group_id="admin",
                 flight_id="f1")
        out = _live_view(_fake_ctx(), types.SimpleNamespace(runtime_bus=bus))
        assert out["seq"] == 2
        assert out["recent"][0]["group_name"] == f"群{G1}"
        assert len(out["active"]) == 1
        assert "group_name" not in out["active"][0]  # admin 不查群名
        # 事件字段仍然只有白名单
        assert set(out["recent"][0]) <= {
            "seq", "ts", "kind", "stage", "status", "group_id", "role",
            "model", "flight_id", "ms", "first_token_ms", "ok", "badge",
            "group_name",
        }


class TestThinkStep:
    def _row(self, **kw) -> dict:
        base = {
            "id": 5, "ts": 1.0, "agent": "main", "model": "m", "ok": 1,
            "ms": 10, "error": "", "response": json.dumps({"text": "答"}),
            "run_ref": "", "first_token_ms": 0, "context_map": "",
        }
        base.update(kw)
        return base

    def test_no_observability_data_keeps_old_shape(self) -> None:
        step = _think_step(self._row())
        assert "first_token_ms" not in step
        assert "context_map" not in step
        assert "model_call_id" not in step
        assert step["text"] == "答"

    def test_first_token_and_map_appear_when_present(self) -> None:
        cmap = {"used_tokens": 10, "limit_tokens": 128000, "parts": [{"tokens": 10}]}
        step = _think_step(self._row(first_token_ms=250,
                                     context_map=json.dumps(cmap)))
        assert step["first_token_ms"] == 250
        assert step["context_map"]["used_tokens"] == 10
        assert step["model_call_id"] == 5

    def test_bad_map_json_ignored(self) -> None:
        step = _think_step(self._row(context_map="{坏 JSON"))
        assert "context_map" not in step


class TestItemContextMap:
    def test_latest_map_lookup(self, store: Store) -> None:
        ctx = _fake_ctx(store)
        trace = Trace(kind="window", group_id=G1, start=0.0, end=9999.0)
        assert _latest_context_map(ctx, trace) is None
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id,"
                " request, response, ms, ok, status, prompt_tokens, completion_tokens,"
                " error, task_id, run_ref, first_token_ms, context_map)"
                " VALUES (5.0,'p','main','a','m',?,'{}','{}',5,1,200,10,3,'','t','',"
                " 0, '')",
                (G1,),
            )
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id,"
                " request, response, ms, ok, status, prompt_tokens, completion_tokens,"
                " error, task_id, run_ref, first_token_ms, context_map)"
                " VALUES (6.0,'p','main','a','m',?,'{}','{}',5,1,200,10,3,'','t','',"
                " 200, ?)",
                (G1, json.dumps({"used_tokens": 10, "parts": []})),
            )
        m = _latest_context_map(ctx, trace)
        assert m == {"used_tokens": 10, "parts": []}
        # 精确钥匙：task_id=t 的行
        t2 = Trace(kind="task_id", value="t", group_id=G1)
        assert _latest_context_map(ctx, t2) == {"used_tokens": 10, "parts": []}


# ----------------------------------------------------------------------
# models.chat 真链路：TTFT + context_map + 总线三段事件
# ----------------------------------------------------------------------


def _sse_openai_response(text: str = "你好") -> httpx.Response:
    body = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":100,"completion_tokens":5,"total_tokens":105}}\n\n'
        "data: [DONE]\n\n"
    )
    return httpx.Response(200, content=body.encode(),
                          headers={"Content-Type": "text/event-stream"})


def _model_cfg() -> dict:
    return {
        "endpoints": [{"id": "default", "name": "d", "protocol": "openai",
                       "base_url": "https://a.test/v1", "api_key": SECRET}],
        "model_list": [{"id": "m1", "endpoint": "default", "model": "m-main"}],
    }


class _Agents:
    def profile(self, kind: str) -> dict:
        return {"kind": kind, "model": "m1", "backup": "", "effort": "", "enabled": True}


class TestModelCallObservability:
    @pytest.mark.asyncio
    async def test_ttft_and_context_map_logged_and_bus_events(self, tmp_path) -> None:
        store = Store(tmp_path / "m.db")
        store.migrate()
        settings = _settings(_model_cfg())
        bus = RuntimeBus()
        calls = []

        def responder(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            if req.url.path.endswith("/chat/completions"):
                return _sse_openai_response()
            return httpx.Response(404)

        models = Models(store, lambda: settings,
                        transport=httpx.MockTransport(responder),
                        agents=_Agents(), runtime_bus=bus)
        r = await models.chat("main", [{"role": "user", "content": "hi"}],
                              group_id=G1)
        assert getattr(r, "text", "") or r

        row = store.read().execute(
            "SELECT first_token_ms, context_map, ok FROM model_calls"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert int(row["ok"]) == 1
        # 流式：首 token 计时 > 0（MockTransport 本地也是真的耗时）
        assert int(row["first_token_ms"]) > 0
        cmap = json.loads(str(row["context_map"]))
        assert cmap["calibrated"] is True
        assert cmap["used_tokens"] == 100  # 端点实报 prompt_tokens 校准
        # 总线：start → first_token → done 三段
        events = [e for e in bus.snapshot()["recent"] if e["kind"] == "model_call"]
        statuses = [e["status"] for e in events]
        assert statuses[0] == "start" and "first_token" in statuses and statuses[-1] == "done"
        done = events[-1]
        assert done["first_token_ms"] > 0 and done["badge"] == "主模型思考"
        assert done["stage"] == "main"
        # 在途表清干净了
        assert bus.snapshot()["active"] == []

    @pytest.mark.asyncio
    async def test_error_attempt_emits_error_event(self, tmp_path) -> None:
        store = Store(tmp_path / "m2.db")
        store.migrate()
        settings = _settings(_model_cfg())
        bus = RuntimeBus()

        def boom(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/chat/completions"):
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(404)

        models = Models(store, lambda: settings,
                        transport=httpx.MockTransport(boom),
                        agents=_Agents(), runtime_bus=bus)
        try:
            await models.chat("main", [{"role": "user", "content": "x"}],
                              group_id=G1, timeout=3)
        except Exception:
            pass
        events = [e for e in bus.snapshot()["recent"] if e["kind"] == "model_call"]
        assert events and events[-1]["status"] == "error"
        assert bus.snapshot()["active"] == []


# ----------------------------------------------------------------------
# SSE：/api/board/events
# ----------------------------------------------------------------------


class _Console:
    def __init__(self, password: str = "") -> None:
        self.password = password


class _Settings:
    def __init__(self, served=(G1,), password: str = "") -> None:
        self.served = set(served)
        self.console = _Console(password)
        self.model_list = ()

    def is_served(self, gid) -> bool:
        return str(gid) in self.served


class _FakeSvc:
    def __init__(self, store: Store, settings: _Settings, bus: RuntimeBus) -> None:
        self.store = store
        self._settings = settings
        self.runtime_bus = bus

    def get_settings(self):
        return self._settings


PASSWORD = "测试密码-很显眼-12345"


@pytest_asyncio.fixture
async def sse_env(tmp_path: Path):
    store = Store(tmp_path / "sse.db")
    store.migrate()
    bus = RuntimeBus(is_served=lambda gid: gid == G1)
    svc = _FakeSvc(store, _Settings(password=PASSWORD), bus)
    server = ConsoleServer(svc)
    test_server = TestServer(server.app)
    client = TestClient(test_server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield types.SimpleNamespace(store=store, svc=svc, bus=bus,
                                server=server, client=client)
    await client.close()
    store.close()


class TestBoardEventsSse:
    @pytest.mark.asyncio
    async def test_requires_admin(self, sse_env) -> None:
        r = await sse_env.client.get("/api/board/events")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_snapshot_then_runtime_events(self, sse_env) -> None:
        r = await sse_env.client.post("/api/login", json={"password": PASSWORD})
        assert r.status == 200
        resp = await sse_env.client.get("/api/board/events")
        assert resp.status == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")
        # 第一帧：snapshot
        ev_name = (await asyncio.wait_for(resp.content.readline(), 5)).decode()
        data = (await asyncio.wait_for(resp.content.readline(), 5)).decode()
        assert "event: snapshot" in ev_name
        snap = json.loads(data.split("data:", 1)[1])
        assert {"seq", "ts", "active", "recent"} <= set(snap)
        # 总线发一条 → 收到 runtime 事件
        sse_env.bus.emit("signal", stage="signal", status="done",
                         group_id=G1, badge="群消息信号")
        got = ""
        for _ in range(6):
            line = (await asyncio.wait_for(resp.content.readline(), 5)).decode()
            got += line
            if "event: runtime" in line:
                payload = json.loads(
                    (await asyncio.wait_for(resp.content.readline(), 5))
                    .decode().split("data:", 1)[1])
                assert payload["kind"] == "signal"
                assert payload["badge"] == "群消息信号"
                break
        else:
            pytest.fail(f"没等到 runtime 事件，收到：{got!r}")
        resp.close()

    @pytest.mark.asyncio
    async def test_stop_does_not_hang_on_open_stream(self, sse_env) -> None:
        r = await sse_env.client.post("/api/login", json={"password": PASSWORD})
        assert r.status == 200
        resp = await sse_env.client.get("/api/board/events")
        await asyncio.wait_for(resp.content.readline(), 5)  # 流已经挂上
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(sse_env.server.stop(), timeout=2.0)
        assert asyncio.get_running_loop().time() - t0 < 2.0
