"""「这个模型在这个端点不可用」快速换备用 + 短期熔断（线上 2026-10-08）。

线上 ling-3.1-flash 回 HTTP 503 {"error":{"code":"model_not_found","message":"No available
channel for model ... under group code (distributor)"}}：旧规则当普通 5xx 连试 1+retries 次、
每次等 retry_delay_s，下一轮又从头撞。现在：
- 认出「模型不可用」（503/404/400/200 内嵌错误都认）→ 只试这一次、不等，立刻换下一个候选；
- 内存熔断 `端点|模型` 一段时间（_UNAVAILABLE_TTL_S），期内的调用直接跳过它让备用上；
- 全部候选都在熔断期 → 仍试熔断最早到期的那个（探测恢复）；
- 到期自动恢复；普通 503 过载 / 429 不误判，照旧重试。
"""

from __future__ import annotations

import pytest

import CharTyr_MaiWork.maiwork.models as mm
from CharTyr_MaiWork.maiwork.models import ModelError, _model_unavailable

from test_model_retry import _make
from test_models import SECRET, FakeEndpoint, _chat_payload

NOT_FOUND_503 = {
    "error": {
        "code": "model_not_found",
        "message": "No available channel for model ling-3.1-flash under group code (distributor)",
    }
}


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,text",
    [
        (503, '{"error":{"code":"model_not_found","message":"No available channel for model x under group code"}}'),
        (503, "No available channel for model ling-3.1-flash under group default"),
        (404, '{"error":{"message":"The model `gpt-9` does not exist or you do not have access to it.","code":"model_not_found"}}'),
        (404, "model gpt-9 not found"),
        (400, '{"error":{"message":"Model Not Exist"}}'),
        (400, "模型不存在"),
        (502, "model_not_found"),
    ],
)
def test_detects_unavailable(status, text) -> None:
    assert _model_unavailable(status, text) is True


@pytest.mark.parametrize(
    "status,text",
    [
        (503, '{"error":{"message":"Service temporarily overloaded, please retry"}}'),
        (503, "upstream connect error"),
        (429, '{"error":{"message":"rate limit for model x exceeded","code":"model_not_found"}}'),
        (408, "model not found"),
        (500, "internal error"),
        (404, '{"error":{"message":"路径不存在"}}'),
        (400, '{"error":{"message":"max_tokens is too large for model gpt-4"}}'),
        (200, ""),
    ],
)
def test_not_unavailable(status, text) -> None:
    assert _model_unavailable(status, text) is False


# ----------------------------------------------------------------------
# chat 行为
# ----------------------------------------------------------------------


@pytest.fixture
def slept(monkeypatch):
    got: list[float] = []

    async def _rec(s: float) -> None:
        got.append(s)

    monkeypatch.setattr(mm, "_SLEEP", _rec)
    return got


@pytest.fixture
def now(monkeypatch):
    t = {"v": 1_000_000.0}
    monkeypatch.setattr(mm, "_NOW", lambda: t["v"])
    return t


def _cfg(**over):
    return {"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=2, **over)}


class TestUnavailableFallback:
    @pytest.mark.asyncio
    async def test_503_model_not_found_tries_once_no_sleep(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 503, "body": NOT_FOUND_503}],
            "m-bak": [{"kind": "ok", "content": "备用救回"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用救回"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak"]
        assert slept == []
        rows = store.read().execute("SELECT model, ok, status FROM model_calls ORDER BY id").fetchall()
        assert [(x["model"], x["ok"], x["status"]) for x in rows] == [("m1", 0, 503), ("m-bak", 1, 200)]
        await models.close()

    @pytest.mark.asyncio
    async def test_404_model_not_found_falls_back(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 404,
                    "body": {"error": {"message": "The model `m1` does not exist", "code": "model_not_found"}}}],
            "m-bak": [{"kind": "ok", "content": "备用"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak"]
        await models.close()

    @pytest.mark.asyncio
    async def test_200_embedded_model_not_found_falls_back(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 200, "body": NOT_FOUND_503}],
            "m-bak": [{"kind": "ok", "content": "备用"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak"]
        assert slept == []
        await models.close()

    @pytest.mark.asyncio
    async def test_breaker_skips_primary_next_call(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 503, "body": NOT_FOUND_503}],
            "m-bak": [{"kind": "ok", "content": "备用"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        await models.chat("main", [{"role": "user", "content": "x"}])
        now["v"] += 60
        r = await models.chat("main", [{"role": "user", "content": "y"}])
        assert r.model == "m-bak"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak", "m-bak"], "熔断期内不再打首选"
        n = store.read().execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        assert n == 3, "跳过不写 model_calls"
        await models.close()

    @pytest.mark.asyncio
    async def test_all_broken_still_tries_one(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 503, "body": NOT_FOUND_503}],
            "m-bak": [{"kind": "status", "status": 503, "body": NOT_FOUND_503},
                      {"kind": "ok", "content": "备用恢复了"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert ei.value.status == 503
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak"]
        # 第二次：两个都在熔断期 → 不是什么都不试，而是探测熔断最早到期的那个（并列取靠前的 m1）
        now["v"] += 30
        with pytest.raises(ModelError):
            await models.chat("main", [{"role": "user", "content": "y"}])
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak", "m1"]
        # 第三次：m1 刚被重新熔断（到期更晚）→ 这回探测 m-bak，它恢复了
        now["v"] += 30
        r = await models.chat("main", [{"role": "user", "content": "z"}])
        assert r.text == "备用恢复了"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak", "m1", "m-bak"]
        await models.close()

    @pytest.mark.asyncio
    async def test_breaker_expires_restores_primary(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 503, "body": NOT_FOUND_503},
                   {"kind": "ok", "content": "首选回来了"}],
            "m-bak": [{"kind": "ok", "content": "备用"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        await models.chat("main", [{"role": "user", "content": "x"}])
        now["v"] += mm._UNAVAILABLE_TTL_S + 1
        r = await models.chat("main", [{"role": "user", "content": "y"}])
        assert r.text == "首选回来了"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak", "m1"]
        await models.close()

    @pytest.mark.asyncio
    async def test_plain_503_still_retries(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 503, "body": {"error": {"message": "overloaded"}}}],
            "m-bak": [{"kind": "ok", "content": "备用"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用"
        assert [c["model"] for c in ep.calls] == ["m1", "m1", "m1", "m-bak"]
        assert slept == [10, 10]
        # 普通 503 不熔断：下一次仍先打首选
        await models.chat("main", [{"role": "user", "content": "y"}])
        assert ep.calls[4]["model"] == "m1"
        await models.close()

    @pytest.mark.asyncio
    async def test_other_4xx_still_raises(self, tmp_path, slept, now) -> None:
        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 404, "body": {"error": {"message": "路径不存在"}}}]})
        store, models = _make(tmp_path, _cfg(), ep)
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert ei.value.status == 404
        assert [c["model"] for c in ep.calls] == ["m1"]
        await models.close()

    @pytest.mark.asyncio
    async def test_fallback_rescue_logged(self, tmp_path, slept, now, caplog) -> None:
        ep = FakeEndpoint({
            "m1": [{"kind": "status", "status": 503, "body": NOT_FOUND_503}],
            "m-bak": [{"kind": "ok", "content": "备用"}],
        })
        store, models = _make(tmp_path, _cfg(), ep)
        with caplog.at_level("INFO", logger="maiwork.models"):
            await models.chat("main", [{"role": "user", "content": "x"}])
        msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
        assert any("m1" in m and "m-bak" in m and "备用救回" in m for m in msgs), msgs
        await models.close()
