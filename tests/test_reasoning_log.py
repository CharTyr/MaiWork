"""思考链记录（2026-10 看板需求）：三协议的 reasoning 只进 model_calls.response 日志。

规矩：
- reasoning 文字只写 model_calls.response 的新键 `reasoning`（截断 ≤8000，超长带「…（已截断）」）；
- redacted / 加密的 thinking 不存内容，记 `reasoning_redacted: true`；
- 业务结果（ChatResult.text / tool_calls）绝不含思考文字；思考绝不回传给模型、不改请求体；
- 写库前过和 response 其它字段一样的密钥遮罩；
- 看板 think 步带 `reasoning`（≤4000）和 `reasoning_redacted`。

全部用 httpx.MockTransport 假端点，不碰真网络。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.board import item_view
from CharTyr_MaiWork.maiwork.models import (
    Models,
    _parse_anthropic_response,
    _parse_responses_response,
)
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "sk-fake-reasoning-do-not-leak-0001"
REASON = "先把候选人分成三组，再逐个核对他说的和事实对不对得上"


def _settings(settings_dict: dict) -> object:
    settings, _ = load_settings(settings_dict)
    return settings


class _FakeAgents:
    def __init__(self, mapping: dict | None = None) -> None:
        self._m = mapping or {}

    def profile(self, kind: str) -> dict:
        d = {"kind": kind, "title": kind, "model": "", "effort": "", "backup": "", "enabled": True,
             "skills": None, "tools": None}
        d.update(self._m.get(kind, {}))
        return dict(d)


def _cfg(proto: str, *, base: str = "https://p.test/v1") -> dict:
    return {
        "endpoints": [{"id": "e1", "protocol": proto, "base_url": base, "api_key": SECRET, "retries": 0}],
        "model_list": [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
    }


def _make(tmp_path, cfg: dict, handler):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg)
    models = Models(
        store, lambda: settings, transport=httpx.MockTransport(handler),
        agents=_FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}}),
    )
    return store, models


def _last_response(store) -> dict:
    row = store.read().execute("SELECT response FROM model_calls ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "model_calls 里没有行"
    return json.loads(row["response"])


def _sse(lines: list[str]) -> httpx.Response:
    payload = "".join(lines)
    payload += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())


def _oai_chunk(delta: dict, finish: str | None = None) -> str:
    obj = {"id": "x", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ----------------------------------------------------------------------
# OpenAI 兼容：reasoning_content（非流式 JSON + 流式 delta）
# ----------------------------------------------------------------------


class TestOpenAIReasoning:
    @pytest.mark.asyncio
    async def test_nonstream_reasoning_logged_not_in_result(self, tmp_path) -> None:
        """端点回普通 JSON（不支持流）：reasoning_content 进日志，不进结果文本。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "id": "x", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "正式答案", "reasoning_content": REASON}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })

        store, models = _make(tmp_path, _cfg("openai"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "正式答案"
        assert REASON not in r.text
        resp = _last_response(store)
        assert resp["reasoning"] == REASON
        assert resp.get("reasoning_redacted") is False
        await models.close()

    @pytest.mark.asyncio
    async def test_stream_reasoning_accumulated(self, tmp_path) -> None:
        """流式 delta 里的 reasoning_content 分段拼起来进日志；结果文本只有 content。"""

        def handler(request: httpx.Request) -> httpx.Response:
            half = len(REASON) // 2
            return _sse([
                _oai_chunk({"role": "assistant"}),
                _oai_chunk({"reasoning_content": REASON[:half]}),
                _oai_chunk({"reasoning_content": REASON[half:]}),
                _oai_chunk({"content": "答"}, finish="stop"),
            ])

        store, models = _make(tmp_path, _cfg("openai"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "答"
        resp = _last_response(store)
        assert resp["reasoning"] == REASON
        await models.close()

    @pytest.mark.asyncio
    async def test_no_reasoning_keys_present(self, tmp_path) -> None:
        """模型没给思考：reasoning 为空串、reasoning_redacted 为 false。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return _sse([_oai_chunk({"content": "好"}, finish="stop")])

        store, models = _make(tmp_path, _cfg("openai"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "好"
        resp = _last_response(store)
        assert resp["reasoning"] == ""
        assert resp["reasoning_redacted"] is False
        await models.close()

    @pytest.mark.asyncio
    async def test_reasoning_truncated_at_8000(self, tmp_path) -> None:
        long_reason = "想" * 9000

        def handler(request: httpx.Request) -> httpx.Response:
            return _sse([
                _oai_chunk({"reasoning_content": long_reason}),
                _oai_chunk({"content": "答"}, finish="stop"),
            ])

        store, models = _make(tmp_path, _cfg("openai"), handler)
        await models.chat("main", [{"role": "user", "content": "x"}])
        resp = _last_response(store)
        assert resp["reasoning"].endswith("…（已截断）")
        assert len(resp["reasoning"]) <= 8010  # 8000 + 「…（已截断）」6 字
        assert resp["reasoning"].startswith("想")
        await models.close()

    @pytest.mark.asyncio
    async def test_reasoning_secret_masked(self, tmp_path) -> None:
        """思考里带密钥也要遮掉再走库。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return _sse([
                _oai_chunk({"reasoning_content": f"密钥 {SECRET} 要用上"}),
                _oai_chunk({"content": "答"}, finish="stop"),
            ])

        store, models = _make(tmp_path, _cfg("openai"), handler)
        await models.chat("main", [{"role": "user", "content": "x"}])
        resp = _last_response(store)
        assert SECRET not in resp["reasoning"]
        assert "***" in resp["reasoning"]
        await models.close()


# ----------------------------------------------------------------------
# Anthropic：thinking 块 / redacted_thinking
# ----------------------------------------------------------------------


class TestAnthropicReasoning:
    def test_thinking_block_parsed_into_log_field(self) -> None:
        data = {
            "content": [
                {"type": "thinking", "thinking": REASON},
                {"type": "text", "text": "结论"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        r = _parse_anthropic_response(data, "claude-x")
        assert r.text == "结论"
        assert REASON not in r.text
        assert r.reasoning == REASON
        assert r.reasoning_redacted is False

    def test_redacted_thinking_marks_flag_no_content(self) -> None:
        data = {
            "content": [
                {"type": "redacted_thinking", "data": "加密后的一坨密文Base64"},
                {"type": "text", "text": "结论"},
            ],
            "stop_reason": "end_turn",
            "usage": {},
        }
        r = _parse_anthropic_response(data, "m")
        assert r.reasoning == ""
        assert r.reasoning_redacted is True
        assert "加密后的一坨密文" not in (r.reasoning or "")
        assert "密文" not in r.text

    def test_mixed_thinking_and_redacted(self) -> None:
        data = {
            "content": [
                {"type": "thinking", "thinking": "前半想"},
                {"type": "redacted_thinking", "data": "密文"},
                {"type": "thinking", "thinking": "后半想"},
                {"type": "text", "text": "结论"},
            ],
            "stop_reason": "end_turn",
            "usage": {},
        }
        r = _parse_anthropic_response(data, "m")
        assert "前半想" in r.reasoning and "后半想" in r.reasoning
        assert r.reasoning_redacted is True

    @pytest.mark.asyncio
    async def test_full_roundtrip_logs_reasoning(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "content": [
                    {"type": "thinking", "thinking": REASON},
                    {"type": "redacted_thinking", "data": "密文"},
                    {"type": "text", "text": "结论"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        store, models = _make(tmp_path, _cfg("anthropic", base="https://p.test"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "结论"
        resp = _last_response(store)
        assert resp["reasoning"] == REASON
        assert resp["reasoning_redacted"] is True
        await models.close()


# ----------------------------------------------------------------------
# Responses：reasoning item（summary 文字）
# ----------------------------------------------------------------------


class TestResponsesReasoning:
    def test_reasoning_item_summary_text(self) -> None:
        data = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": REASON}]},
                {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
            ],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }
        r = _parse_responses_response(data, "gpt-x")
        assert r.text == "答案"
        assert REASON not in r.text
        assert r.reasoning == REASON
        assert r.reasoning_redacted is False

    def test_empty_summary_no_reasoning(self) -> None:
        data = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
            ],
        }
        r = _parse_responses_response(data, "m")
        assert r.reasoning == ""
        assert r.reasoning_redacted is False

    @pytest.mark.asyncio
    async def test_sse_completed_reasoning_logged(self, tmp_path) -> None:
        """Responses SSE：completed 事件整份里的 reasoning item 进日志。"""

        def handler(request: httpx.Request) -> httpx.Response:
            events = [
                {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}},
                {"type": "response.completed", "response": {
                    "id": "r1", "status": "completed",
                    "output": [
                        {"type": "reasoning",
                         "summary": [{"type": "summary_text", "text": REASON}]},
                        {"type": "message", "content": [{"type": "output_text", "text": "流式答案"}]},
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 2},
                }},
            ]
            payload = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
            payload += "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                content=payload.encode())

        store, models = _make(tmp_path, _cfg("responses"), handler)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "流式答案"
        resp = _last_response(store)
        assert resp["reasoning"] == REASON
        await models.close()


# ----------------------------------------------------------------------
# 看板：think 步带 reasoning / reasoning_redacted（≤4000）
# ----------------------------------------------------------------------


class _BoardSvc:
    """item_view 需要的最小 svc：store + get_settings + agents（懒建表）。"""

    def __init__(self, store: Store, gid: str) -> None:
        self.store = store
        self._gid = gid
        from CharTyr_MaiWork.maiwork.agents import Agents
        self.agents = Agents(store, self.get_settings)
        self.agents._ensure_schema()

    def get_settings(self):
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{self._gid}"}]}})
        return settings


def _make_board_svc(store: Store, gid: str) -> "_BoardSvc":
    return _BoardSvc(store, gid)


def _seed_handoff_with_reasoning(store: Store, gid: str, hid: str, mark: str,
                                 reasoning: str, redacted: bool = False,
                                 *, ts: float | None = None) -> None:
    ts = ts or clock.now()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO agent_handoffs"
            " (id, group_id, kind, task_id, phase, parent_id, status, brief,"
            "  criteria, tools, skills, summary, data, evidence, review, error, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?, '[]','[]','[]','','','[]','','',?,?)",
            (hid, gid, "news", mark, "", "", "accepted", "核验", ts - 100, ts),
        )
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt, ok,"
            " status, ms, prompt_tokens, completion_tokens, error, request, response,"
            " usage_src, cache_read, cache_write)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts - 50, "worker", "worker", "news", "worker-m", gid, mark, 1, 1,
             200, 50, 10, 20, "", "{}", json.dumps(
                 {"text": "这条像软文", "reasoning": reasoning,
                  "reasoning_redacted": redacted, "tool_calls": [], "finish_reason": "stop"},
                 ensure_ascii=False), "reported", 0, 0),
        )


class TestBoardReasoningSteps:
    def test_think_step_carries_reasoning(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        gid = "900000001"
        mark = "feeds-verify:900000001:1:0"
        svc = _BoardSvc(store, gid)  # 先建表
        _seed_handoff_with_reasoning(store, gid, "h-r1", mark, REASON)
        data = item_view(svc, "handoff", "h-r1")
        assert data is not None
        think = next(s for s in data["steps"] if s["type"] == "think")
        assert think["reasoning"] == REASON
        assert think["reasoning_redacted"] is False
        assert think["text"] == "这条像软文"
        # tool / status 步也有这两个字段（统一形状，值为空串/false）
        for s in data["steps"]:
            assert "reasoning" in s and "reasoning_redacted" in s
        store.close()

    def test_reasoning_clipped_at_4000(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        gid = "900000001"
        mark = "feeds-verify:900000001:2:0"
        svc = _BoardSvc(store, gid)
        _seed_handoff_with_reasoning(store, gid, "h-r2", mark, "长" * 5000)
        data = item_view(svc, "handoff", "h-r2")
        think = next(s for s in data["steps"] if s["type"] == "think")
        assert len(think["reasoning"]) <= 4000
        assert think["reasoning"].endswith("…")
        store.close()

    def test_reasoning_redacted_flag(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        gid = "900000001"
        mark = "feeds-verify:900000001:3:0"
        svc = _BoardSvc(store, gid)
        _seed_handoff_with_reasoning(store, gid, "h-r3", mark, "", redacted=True)
        data = item_view(svc, "handoff", "h-r3")
        think = next(s for s in data["steps"] if s["type"] == "think")
        assert think["reasoning"] == ""
        assert think["reasoning_redacted"] is True
        store.close()

    def test_no_reasoning_defaults_empty(self, tmp_path) -> None:
        """老行（response 里没有 reasoning 键）：reasoning 空串、redacted false。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        gid = "900000001"
        svc = _BoardSvc(store, gid)
        ts = clock.now()
        mark = "feeds-verify:900000001:4:0"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO agent_handoffs"
                " (id, group_id, kind, task_id, phase, parent_id, status, brief,"
                "  criteria, tools, skills, summary, data, evidence, review, error, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?, '[]','[]','[]','','','[]','','',?,?)",
                ("h-r4", gid, "news", mark, "", "", "accepted", "核验", ts - 100, ts),
            )
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt, ok,"
                " status, ms, prompt_tokens, completion_tokens, error, request, response,"
                " usage_src, cache_read, cache_write)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts - 50, "worker", "worker", "news", "worker-m", gid, mark, 1, 1,
                 200, 50, 10, 20, "", "{}", json.dumps(
                     {"text": "旧行没思考", "tool_calls": [], "finish_reason": "stop"},
                     ensure_ascii=False), "reported", 0, 0),
            )
        data = item_view(svc, "handoff", "h-r4")
        think = next(s for s in data["steps"] if s["type"] == "think")
        assert think["reasoning"] == ""
        assert think["reasoning_redacted"] is False
        store.close()
