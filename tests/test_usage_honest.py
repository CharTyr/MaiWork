"""C03（docs/13-0.7.0整体审查.md）：用量如实显示——未知不是零，缓存不是免费。

- ChatResult 带 usage_known / cache_read_tokens / cache_write_tokens：
  端点没报 usage、usage 不是对象、输入输出两个计数都缺 → usage_known=False；
  openai 的 cached_tokens / anthropic 的 cache_read_input_tokens → cache_read，
  anthropic 的 cache_creation_input_tokens → cache_write（prompt_tokens 保持
  input_tokens 原值，缓存另记，不混进输入）。
- usage 表新列 usage_src（reported 实报 / unknown 不知道 / none 确定没生成）、
  cache_read、cache_write、attempt；旧行 usage_src='' 当老数据。
  - 成功且端点报了用量 → reported；
  - 成功但没报 / 网络错误、超时、流中断、5xx、408（请求可能已在服务端处理）→ unknown；
  - 429 和其他 4xx（请求被拒，确定没生成）→ none。
- usage_today() 多给 unknown_calls / retries（attempt>1 的尝试数）/ cache_read / cache_write；
  main/worker 口径不变（照旧按 role 加 token，未知不算进去也不当 0 冒充）。
- 一轮资讯的模型调用尽量带本轮标记（task_id=feeds-collect:… 落进 usage 表），
  批次统计 usage 段按它点数。
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.models import (
    ChatResult,
    ModelError,
    Models,
    _parse_anthropic_response,
    _parse_responses_response,
)
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "honestsecret123"


def _make_models(tmp_path, handler, *, protocol: str = "openai", retries: int = 0):
    """最小可用的 Models：一个端点 + 一个主模型条目；handler 是 MockTransport 的处理函数。"""
    store = Store(tmp_path / "honest.db")
    store.migrate()
    cfg = {
        "endpoints": [
            {"id": "e1", "protocol": protocol, "base_url": "https://h.test/v1",
             "api_key": SECRET, "retries": retries, "retry_delay_s": 1},
        ],
        "model_list": [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
    }
    from CharTyr_MaiWork.maiwork.config import load_settings

    settings, _ = load_settings(cfg)
    models = Models(
        store, lambda: settings, transport=httpx.MockTransport(handler),
        agents=_FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}}),
    )
    return store, models


class _FakeAgents:
    def __init__(self, mapping: dict) -> None:
        self._m = mapping

    def profile(self, kind: str) -> dict:
        d = {"kind": kind, "title": kind, "model": "", "effort": "", "backup": "", "enabled": True}
        d.update(self._m.get(kind, {}))
        return dict(d)


def _usage_rows(store: Store) -> list[dict]:
    return [dict(r) for r in store.read().execute("SELECT * FROM usage ORDER BY id").fetchall()]


def _sse_lines(*payloads: str) -> bytes:
    return ("".join(f"data: {p}\n\n" for p in payloads) + "data: [DONE]\n\n").encode()


def _openai_sse_chunk(content: str, *, usage: dict | None = ..., finish: str = "stop") -> str:
    obj: dict = {
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }
    if usage is not ...:
        obj["usage"] = usage
    return json.dumps(obj, ensure_ascii=False)


def _json_handler(payload: dict | list, *, status: int = 200, content_type: str = "application/json"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers={"content-type": content_type})

    return handler


# ----------------------------------------------------------------------
# 解析层：三个协议的 usage_known / 缓存列
# ----------------------------------------------------------------------


class TestParseUsageHonest:
    def test_openai_parse_reported_with_cache(self) -> None:
        data = {
            "choices": [{"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 9,
                      "prompt_tokens_details": {"cached_tokens": 40}},
        }
        r = Models._parse_chat(data, "m")
        assert r.usage_known is True
        assert r.prompt_tokens == 100 and r.completion_tokens == 9
        assert r.cache_read_tokens == 40 and r.cache_write_tokens == 0

    def test_openai_parse_no_usage_is_unknown_not_zero(self) -> None:
        data = {"choices": [{"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}]}
        r = Models._parse_chat(data, "m")
        assert r.usage_known is False
        assert r.prompt_tokens == 0 and r.completion_tokens == 0

    def test_openai_parse_usage_not_dict_or_empty_counts(self) -> None:
        base = {"choices": [{"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}]}
        # 不是对象 / 空 dict / 两个计数都缺 → unknown
        for usage in ("x", {}, {"total_tokens": 9}):
            r = Models._parse_chat({**base, "usage": usage}, "m")
            assert r.usage_known is False, usage
        # 只报了一个计数也算「报了」（规格是「两个计数都缺」才算没报）
        r = Models._parse_chat({**base, "usage": {"prompt_tokens": 3}}, "m")
        assert r.usage_known is True and r.prompt_tokens == 3 and r.completion_tokens == 0

    def test_anthropic_parse_cache_columns_and_prompt_semantics(self) -> None:
        """anthropic 的 input_tokens 不含缓存部分：prompt_tokens 保持 input_tokens 原值，
        缓存读/写另记两列，不许偷偷并进输入。"""
        data = {
            "content": [{"type": "text", "text": "好"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 7,
                      "cache_read_input_tokens": 900, "cache_creation_input_tokens": 300},
        }
        r = _parse_anthropic_response(data, "claude-x")
        assert r.usage_known is True
        assert r.prompt_tokens == 50
        assert r.completion_tokens == 7
        assert r.cache_read_tokens == 900
        assert r.cache_write_tokens == 300

    def test_anthropic_parse_no_usage_unknown(self) -> None:
        r = _parse_anthropic_response({"content": [{"type": "text", "text": "好"}]}, "m")
        assert r.usage_known is False

    def test_responses_parse_cached_tokens(self) -> None:
        data = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "好"}]}],
            "usage": {"input_tokens": 66, "output_tokens": 4,
                      "input_tokens_details": {"cached_tokens": 20}},
        }
        r = _parse_responses_response(data, "m")
        assert r.usage_known is True
        assert r.prompt_tokens == 66 and r.completion_tokens == 4
        assert r.cache_read_tokens == 20 and r.cache_write_tokens == 0

    def test_responses_parse_no_usage_unknown(self) -> None:
        r = _parse_responses_response({"output": []}, "m")
        assert r.usage_known is False


# ----------------------------------------------------------------------
# 记账层：usage_src 规则（reported / unknown / none）+ 缓存列 + attempt
# ----------------------------------------------------------------------


class TestUsageSrc:
    @pytest.mark.asyncio
    async def test_success_without_usage_is_unknown(self, tmp_path) -> None:
        """成功但端流里没报 usage：usage_src=unknown，token 是 0 但不能冒充「真没花」。"""
        handler = _json_handler({
            "choices": [{"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
        })
        store, models = _make_models(tmp_path, handler)
        try:
            r = await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            assert r.usage_known is False
            rows = _usage_rows(store)
            assert len(rows) == 1
            row = rows[0]
            assert row["ok"] == 1
            assert row["usage_src"] == "unknown"
            assert row["prompt_tokens"] == 0 and row["completion_tokens"] == 0
            assert row["attempt"] == 1
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_stream_broken_after_content_is_unknown(self, tmp_path) -> None:
        """生成后断流（有 content 但既没 [DONE] 也没 finish_reason）：请求可能已被处理 → unknown。"""
        body = _sse_lines(_openai_sse_chunk("半截", usage=None, finish=None)).replace(b"[DONE]\n\n", b"")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        store, models = _make_models(tmp_path, handler)
        try:
            with pytest.raises(ModelError):
                await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            rows = _usage_rows(store)
            assert len(rows) == 1
            assert rows[0]["ok"] == 0
            assert rows[0]["usage_src"] == "unknown"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_429_is_none(self, tmp_path) -> None:
        """429 = 被限流拒了，确定没生成 → none（不是 unknown）。"""
        store, models = _make_models(tmp_path, _json_handler({"error": {"message": "太快了"}}, status=429))
        try:
            with pytest.raises(ModelError):
                await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            rows = _usage_rows(store)
            assert len(rows) == 1
            assert rows[0]["ok"] == 0 and rows[0]["usage_src"] == "none"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_400_is_none(self, tmp_path) -> None:
        store, models = _make_models(tmp_path, _json_handler({"error": {"message": "请求不对"}}, status=400))
        try:
            with pytest.raises(ModelError):
                await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            rows = _usage_rows(store)
            assert len(rows) == 1
            assert rows[0]["ok"] == 0 and rows[0]["usage_src"] == "none"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_500_is_unknown(self, tmp_path) -> None:
        """5xx：请求可能已经在服务端生成了才出错 → unknown。"""
        store, models = _make_models(tmp_path, _json_handler({"error": {"message": "服务端错误"}}, status=500))
        try:
            with pytest.raises(ModelError):
                await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            rows = _usage_rows(store)
            assert len(rows) == 1
            assert rows[0]["ok"] == 0 and rows[0]["usage_src"] == "unknown"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_retry_then_success_has_attempt2_reported(self, tmp_path) -> None:
        """先 500 后成功（重试第 2 次）：attempt=2 那行是 reported；attempt=1 那行 unknown。"""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, json={"error": {"message": "服务端错误"}})
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            })

        store, models = _make_models(tmp_path, handler, retries=1)
        try:
            r = await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            assert r.prompt_tokens == 12
            rows = _usage_rows(store)
            assert len(rows) == 2
            assert rows[0]["ok"] == 0 and rows[0]["usage_src"] == "unknown" and rows[0]["attempt"] == 1
            assert rows[1]["ok"] == 1 and rows[1]["usage_src"] == "reported" and rows[1]["attempt"] == 2
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_openai_cached_tokens_logged(self, tmp_path) -> None:
        """openai 端点报 cached_tokens → 记进 cache_read 列（usage 和 model_calls 都记）。"""
        store, models = _make_models(tmp_path, _json_handler({
            "choices": [{"message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 9,
                      "prompt_tokens_details": {"cached_tokens": 40}},
        }))
        try:
            await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            row = _usage_rows(store)[0]
            assert row["usage_src"] == "reported"
            assert row["cache_read"] == 40 and row["cache_write"] == 0
            mc = store.read().execute("SELECT * FROM model_calls ORDER BY id").fetchall()
            assert mc and mc[0]["usage_src"] == "reported"
            assert mc[0]["cache_read"] == 40 and mc[0]["cache_write"] == 0
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_openai_stream_empty_usage_dict_is_unknown(self, tmp_path) -> None:
        """openai 流式路径：拼好的 data 里 usage 是空 dict（端点没报）→ unknown。"""
        body = _sse_lines(_openai_sse_chunk("好", usage=None))
        store, models = _make_models(
            tmp_path,
            lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}),
        )
        try:
            r = await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            assert r.text == "好" and r.usage_known is False
            assert _usage_rows(store)[0]["usage_src"] == "unknown"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_openai_stream_cached_tokens(self, tmp_path) -> None:
        """openai 流式路径：末块带 usage.cached_tokens → reported + cache_read。"""
        body = _sse_lines(
            _openai_sse_chunk("好", usage=None, finish=None),
            _openai_sse_chunk("", usage={"prompt_tokens": 30, "completion_tokens": 2,
                                         "prompt_tokens_details": {"cached_tokens": 10}}),
        )
        store, models = _make_models(
            tmp_path,
            lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}),
        )
        try:
            r = await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            assert r.usage_known is True and r.cache_read_tokens == 10
            row = _usage_rows(store)[0]
            assert row["usage_src"] == "reported" and row["cache_read"] == 10
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_anthropic_cache_logged(self, tmp_path) -> None:
        """anthropic 端点：cache_read/cache_creation_input_tokens 记进列，prompt 不动。"""
        store, models = _make_models(tmp_path, _json_handler({
            "content": [{"type": "text", "text": "好"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 7,
                      "cache_read_input_tokens": 900, "cache_creation_input_tokens": 300},
        }), protocol="anthropic")
        try:
            await models.chat(role="main", messages=[{"role": "user", "content": "hi"}])
            row = _usage_rows(store)[0]
            assert row["usage_src"] == "reported"
            assert row["prompt_tokens"] == 50 and row["completion_tokens"] == 7
            assert row["cache_read"] == 900 and row["cache_write"] == 300
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_usage_columns_in_both_tables(self, tmp_path) -> None:
        """usage / model_calls 两表都有新列。"""
        store, models = _make_models(tmp_path, _json_handler({"choices": []}))
        try:
            ucols = {r["name"] for r in store.read().execute("PRAGMA table_info(usage)")}
            assert {"usage_src", "cache_read", "cache_write", "attempt"} <= ucols
            mcols = {r["name"] for r in store.read().execute("PRAGMA table_info(model_calls)")}
            assert {"usage_src", "cache_read", "cache_write"} <= mcols
        finally:
            store.close()


# ----------------------------------------------------------------------
# 汇总：usage_today 多出来的四样
# ----------------------------------------------------------------------


class TestUsageTodayHonest:
    def _insert(self, store: Store, **kw) -> None:
        row = {
            "ts": clock.now(), "day": clock.day_key(clock.now()), "role": "worker",
            "model": "m", "purpose": "", "group_id": "", "task_id": "",
            "prompt_tokens": 0, "completion_tokens": 0, "ok": 1, "ms": 1, "error": "",
            "usage_src": "reported", "cache_read": 0, "cache_write": 0, "attempt": 1,
        }
        row.update(kw)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
                " prompt_tokens, completion_tokens, ok, ms, error, usage_src, cache_read, cache_write, attempt)"
                " VALUES (:ts, :day, :role, :model, :purpose, :group_id, :task_id,"
                " :prompt_tokens, :completion_tokens, :ok, :ms, :error, :usage_src,"
                " :cache_read, :cache_write, :attempt)",
                row,
            )

    def test_today_counts_unknown_retries_cache(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        try:
            self._insert(store, role="main", prompt_tokens=10, completion_tokens=5)
            self._insert(store, role="worker", prompt_tokens=20, completion_tokens=5,
                         cache_read=30, cache_write=8)
            self._insert(store, ok=0, usage_src="unknown")          # 断流
            self._insert(store, ok=1, usage_src="unknown")          # 成功但没报
            self._insert(store, ok=0, usage_src="none")             # 429
            self._insert(store, prompt_tokens=3, completion_tokens=2, attempt=2)  # 重试后成功
            models = Models.__new__(Models)
            models._store = store
            t = models.usage_today()
            assert t["main"] == 15
            assert t["worker"] == 30  # 25 + 3 + 2（attempt=2 的也照旧进桶）
            assert t["calls"] == 6 and t["errors"] == 2
            assert t["unknown_calls"] == 2
            assert t["retries"] == 1
            assert t["cache_read"] == 30 and t["cache_write"] == 8
        finally:
            store.close()

    def test_today_defaults_present_when_empty(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        try:
            t = Models.__new__(Models)
            t._store = store
            out = t.usage_today()
            assert out["unknown_calls"] == 0 and out["retries"] == 0
            assert out["cache_read"] == 0 and out["cache_write"] == 0
            assert out["main"] == 0 and out["worker"] == 0
        finally:
            store.close()

    def test_today_ignores_old_rows_with_empty_src(self, tmp_path) -> None:
        """老行（usage_src=''，这功能之前落的）：不计 unknown_calls，main/worker 照旧算。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        try:
            self._insert(store, role="main", prompt_tokens=7, completion_tokens=3, usage_src="")
            t = Models.__new__(Models)
            t._store = store
            out = t.usage_today()
            assert out["main"] == 10
            assert out["unknown_calls"] == 0 and out["retries"] == 0
        finally:
            store.close()


# ----------------------------------------------------------------------
# 迁移：旧库（没有这个迁移的版本）再迁移不炸、列补上、再跑一次幂等
# ----------------------------------------------------------------------


class TestUsageMigration:
    def test_old_db_gets_columns_and_is_idempotent(self, tmp_path) -> None:
        db = tmp_path / "old.db"
        from CharTyr_MaiWork.maiwork import store as store_mod

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(store_mod._M1_SQL)  # v1 建库（只有最初那批表）
        # 老数据一行（没有新列时插入的）
        conn.execute(
            "INSERT INTO usage (ts, day, role, model, prompt_tokens, completion_tokens, ok)"
            " VALUES (1, '2026-01-01', 'main', 'old-m', 5, 3, 1)"
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

        store = Store(db)
        v1 = store.migrate()
        try:
            ucols = {r["name"] for r in store.read().execute("PRAGMA table_info(usage)")}
            assert {"usage_src", "cache_read", "cache_write", "attempt"} <= ucols
            row = store.read().execute("SELECT * FROM usage WHERE model='old-m'").fetchone()
            assert row["usage_src"] == ""          # 老行 = 老数据，别乱猜
            assert row["cache_read"] == 0 and row["cache_write"] == 0 and row["attempt"] == 0
            v2 = store.migrate()                    # 再跑一次不炸（幂等）
            assert v1 == v2
        finally:
            store.close()


# ----------------------------------------------------------------------
# 一轮资讯花多少：feeds 按本轮标记（task_id=feeds-collect:…）点数
# ----------------------------------------------------------------------


class TestRoundUsageQuery:
    def _mk_feeds(self, tmp_path) -> tuple[Store, object]:
        """Feeds.__new__ 绕过 __init__（不拉起一堆依赖）；_round_usage 只用 self._store。"""
        from CharTyr_MaiWork.maiwork.feeds import Feeds

        store = Store(tmp_path / "f.db")
        store.migrate()
        feeds = Feeds.__new__(Feeds)
        feeds._store = store
        return store, feeds

    def _insert_usage(self, store: Store, task_id: str, **kw) -> None:
        row = {
            "ts": 1.0, "day": "2026-01-01", "role": "worker", "model": "m",
            "purpose": "feeds.score", "group_id": "g1", "task_id": task_id,
            "prompt_tokens": 100, "completion_tokens": 20, "ok": 1, "ms": 1, "error": "",
            "usage_src": "reported", "cache_read": 0, "cache_write": 0, "attempt": 1,
        }
        row.update(kw)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
                " prompt_tokens, completion_tokens, ok, ms, error, usage_src, cache_read, cache_write, attempt)"
                " VALUES (:ts, :day, :role, :model, :purpose, :group_id, :task_id,"
                " :prompt_tokens, :completion_tokens, :ok, :ms, :error, :usage_src,"
                " :cache_read, :cache_write, :attempt)",
                row,
            )

    def test_round_usage_sums_reported_and_unknown_separately(self, tmp_path) -> None:
        store, feeds = self._mk_feeds(tmp_path)
        try:
            mark = "feeds-collect:g1:1700000000000:1"
            self._insert_usage(store, mark, prompt_tokens=100, completion_tokens=20, purpose="feeds.focus")
            self._insert_usage(store, mark, prompt_tokens=200, completion_tokens=30,
                               purpose="feeds.score", cache_read=50, cache_write=6)
            # 本轮的重看 / 核验子 agent 标记：不同前缀、同一个 base，也算这轮的
            self._insert_usage(store, mark.replace("feeds-collect:", "feeds-recheck:", 1),
                               prompt_tokens=10, completion_tokens=2)
            self._insert_usage(store, f"feeds-verify:g1:1700000000000:1:0",
                               prompt_tokens=40, completion_tokens=8)
            self._insert_usage(store, mark, ok=0, usage_src="unknown")   # 没报用量
            self._insert_usage(store, mark, ok=0, usage_src="none")      # 429，确定没生成
            self._insert_usage(store, mark, usage_src="", prompt_tokens=999)  # 老行：不进统计
            # 别的轮 / 别的群：不许串进来
            self._insert_usage(store, "feeds-collect:g1:1699999999999:9", prompt_tokens=777)
            self._insert_usage(store, mark.replace(":g1:", ":g2:"), group_id="g2", prompt_tokens=555)
            u = feeds._round_usage(mark)
            assert u["reported"] == 410          # 120 + 230 + 12 + 48（不含老行 999）
            assert u["calls"] == 4               # 实报的尝试数
            assert u["cache_read"] == 50 and u["cache_write"] == 6
            assert u["unknown_calls"] == 1       # none 不算 unknown
        finally:
            store.close()

    def test_round_usage_empty_mark_is_none(self, tmp_path) -> None:
        store, feeds = self._mk_feeds(tmp_path)
        try:
            assert feeds._round_usage("") is None
            assert feeds._round_usage("feeds-collect:g1:1:1") is None  # 没数据 → None（不显示）
        finally:
            store.close()

    def test_batch_stats_roundtrip_with_usage(self, tmp_path) -> None:
        store, feeds = self._mk_feeds(tmp_path)
        try:
            usage = {"reported": 410, "calls": 4, "cache_read": 50, "cache_write": 6, "unknown_calls": 1}
            with store.tx() as conn:
                feeds._write_batch_stats(conn, 42, searches=3, pages=5, kept=2, usage=usage)
            st = feeds._batch_stats(42)
            assert st["searches"] == 3 and st["pages"] == 5 and st["kept"] == 2
            assert st["usage"] == usage
        finally:
            store.close()

    def test_batch_stats_old_batch_without_usage(self, tmp_path) -> None:
        """老批次（没有 usage 键）读出来不带 usage → 前端显示老样子，不崩。"""
        store, feeds = self._mk_feeds(tmp_path)
        try:
            with store.tx() as conn:
                feeds._write_batch_stats(conn, 43, searches=1, pages=2, kept=1)
            st = feeds._batch_stats(43)
            assert "usage" not in st
        finally:
            store.close()
