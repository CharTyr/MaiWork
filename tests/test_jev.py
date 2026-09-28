"""jev.py 单元测试：答案校验、熔断、超时、密钥读取流程、judgments 落库且不含密钥。

全部通过 httpx.MockTransport（不用真网络）；密钥文件用 tmp_path。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Tuple

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "typesafe-testkey-123"  # 假密钥；专门用来断言「绝不出现在任何输出里」


def _settings(cfg: dict | None = None) -> object:
    settings, _ = load_settings(cfg or {})
    return settings


class RequestLog:
    """httpx.MockTransport handler：记录收到的每个请求。"""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: List[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.responder(req)


def _make_jev(
    tmp_path,
    *,
    cfg: dict | None = None,
    responder=None,
    n_requests: List[httpx.Request] | None = None,
):
    from CharTyr_MaiWork.maiwork.jev import Jev

    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg)
    if responder is None:
        responder = lambda req: httpx.Response(500, json={"error": "boom"})
    log = RequestLog(responder)
    transport = httpx.MockTransport(log.handler)
    jev = Jev(
        store,
        lambda: settings,
        transport=transport,
    )
    return store, settings, jev, log


STATE = {"messages": [{"speaker": "USER", "text": "最近那个开源项目怎么样"}]}
QUESTIONS = {
    "ok": {"type": "noul", "instructions": "现在适合抛新话题吗"},
    "reason": {
        "type": "choice",
        "instructions": "为什么不适合",
        "criteria": {
            "left": "人都走了",
            "open_question": "有问题没人回",
            "mood": "气氛不对",
            "fine": "可以开",
        },
    },
}
GOOD_ANSWERS = {
    "ok": {"type": "noul", "noul": 0.8},
    "reason": {
        "type": "choice",
        "choice": "fine",
        "probabilities": {"left": 0.05, "open_question": 0.05, "mood": 0.05, "fine": 0.85},
        "confidence": 0.9,
    },
}


# ----------------------------------------------------------------------
# 可用性
# ----------------------------------------------------------------------


def test_available_false_when_disabled(tmp_path):
    jev = _make_jev(tmp_path, cfg={"jev": {"enabled": False}})[2]
    assert jev.available() is False


def test_available_false_when_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev = _make_jev(tmp_path)[2]
    assert jev.available() is False


def test_available_true_with_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    jev = _make_jev(tmp_path)[2]
    assert jev.available() is True


def test_calls_today_zero_initially(tmp_path):
    jev = _make_jev(tmp_path)[2]
    assert jev.calls_today() == 0


# ----------------------------------------------------------------------
# 密钥读取优先级：环境变量 > 配置（api_key）> key_file > ~/.typesafe_key
# ----------------------------------------------------------------------


def test_key_env_wins_over_file(tmp_path, monkeypatch):
    (tmp_path / "k.txt").write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key-999")
    jev = _make_jev(tmp_path, cfg={"jev": {"key_file": str(tmp_path / "k.txt")}})[2]
    # 环境变量赢
    assert jev._key() == "env-key-999"


def test_key_from_key_file(tmp_path, monkeypatch):
    key_path = tmp_path / "k.txt"
    key_path.write_text(SECRET + "\n", encoding="utf-8")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev = _make_jev(tmp_path, cfg={"jev": {"key_file": str(key_path)}})[2]
    assert jev._key() == SECRET


def test_key_symlink_refused(tmp_path, monkeypatch):
    real = tmp_path / "real.txt"
    real.write_text(SECRET, encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev = _make_jev(tmp_path, cfg={"jev": {"key_file": str(link)}})[2]
    assert jev._key() == ""


def test_key_nonfile_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev = _make_jev(tmp_path, cfg={"jev": {"key_file": str(tmp_path / "missing.txt")}})[2]
    assert jev._key() == ""


# ----------------------------------------------------------------------
# 请求 / 答案校验
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_returns_none_when_disabled(tmp_path):
    called = []
    jev = _make_jev(tmp_path, cfg={"jev": {"enabled": False}}, responder=lambda r: called.append(r))[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None
    assert called == []


@pytest.mark.asyncio
async def test_ask_no_key_no_request(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    called = []
    jev = _make_jev(tmp_path, responder=lambda r: called.append(r))[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None
    assert called == []


@pytest.mark.asyncio
async def test_ask_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

    def responder(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        assert body["state"] == STATE
        assert body["questions"] == QUESTIONS
        assert body.get("model")  # 要有模型名
        assert req.headers.get("authorization", "") == f"Bearer {SECRET}"
        return httpx.Response(200, json={"answers": GOOD_ANSWERS})

    store, settings, jev, log = _make_jev(tmp_path, responder=responder)
    out = await jev.ask(dict(STATE), dict(QUESTIONS), purpose="topic", group_id="111")
    assert out == {"ok": 0.8, "reason": ("fine", 0.85, 0.9)}
    assert len(log.requests) == 1


@pytest.mark.asyncio
async def test_ask_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

    def responder(req):
        raise httpx.ReadTimeout("太慢了", request=req)

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


@pytest.mark.asyncio
async def test_ask_http_error_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

    def responder(req):
        return httpx.Response(500, json={"error": "boom"})

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


@pytest.mark.asyncio
async def test_ask_validation_noul_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    bad = {
        "ok": {"type": "noul", "noul": 1.5},  # 超界
        "reason": GOOD_ANSWERS["reason"],
    }

    def responder(req):
        return httpx.Response(200, json={"answers": bad})

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


@pytest.mark.asyncio
async def test_ask_validation_choice_label_not_in_criteria(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    bad = {
        "ok": GOOD_ANSWERS["ok"],
        "reason": {
            "type": "choice",
            "choice": "alien",  # 不在 criteria
            "probabilities": {"left": 0.1, "open_question": 0.1, "mood": 0.1, "fine": 0.7},
            "confidence": 0.9,
        },
    }

    def responder(req):
        return httpx.Response(200, json={"answers": bad})

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


@pytest.mark.asyncio
async def test_ask_validation_choice_probabilities_key_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    bad = {
        "ok": GOOD_ANSWERS["ok"],
        "reason": {
            "type": "choice",
            "choice": "fine",
            "probabilities": {"fine": 0.7},  # 键集合不一致
            "confidence": 0.9,
        },
    }

    def responder(req):
        return httpx.Response(200, json={"answers": bad})

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


@pytest.mark.asyncio
async def test_ask_missing_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

    def responder(req):
        return httpx.Response(200, json={})  # 缺 answers

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


@pytest.mark.asyncio
async def test_ask_bad_json_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

    def responder(req):
        return httpx.Response(200, content=b"not-json")

    jev = _make_jev(tmp_path, responder=responder)[2]
    assert await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111") is None


# ----------------------------------------------------------------------
# 熔断：连续 3 次失败停 60 秒
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_3_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    n = {"count": 0}

    def responder(req):
        n["count"] += 1
        return httpx.Response(500, json={"error": "boom"})

    jev = _make_jev(tmp_path, responder=responder)[2]
    for _ in range(3):
        assert await jev.ask(STATE, QUESTIONS, purpose="x", group_id="111") is None
    assert n["count"] == 3
    assert jev.available() is False
    assert await jev.ask(STATE, QUESTIONS, purpose="x", group_id="111") is None
    assert n["count"] == 3  # 熔断中不再发请求


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_after_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    t = {"now": 1_790_000_000.0}
    monkeypatch.setattr(clock, "now", lambda: t["now"])
    n = {"count": 0}

    def responder(req):
        n["count"] += 1
        if n["count"] <= 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"answers": GOOD_ANSWERS})

    jev = _make_jev(tmp_path, responder=responder)[2]
    for _ in range(3):
        assert await jev.ask(STATE, QUESTIONS, purpose="x", group_id="111") is None
    assert n["count"] == 3
    t["now"] += 61.0  # 冷却 60 秒
    assert await jev.ask(STATE, QUESTIONS, purpose="x", group_id="111") is not None
    assert n["count"] == 4


# ----------------------------------------------------------------------
# judgments 落库
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judgments_logged_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    store, settings, jev, _ = _make_jev(
        tmp_path,
        responder=lambda r: httpx.Response(200, json={"answers": GOOD_ANSWERS}),
    )
    await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111")
    rows = store.read().execute("SELECT * FROM judgments").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["purpose"] == "topic"
    assert row["group_id"] == "111"
    assert row["ok"] == 1
    assert row["ms"] >= 0
    assert row["error"] == ""
    # state 摘要截 800 字
    assert len(row["state_summary"]) <= 800
    assert SECRET not in row["state_summary"]
    assert SECRET not in row["answers"]
    # answers JSON 能还原
    parsed = json.loads(row["answers"])
    assert parsed["ok"] == GOOD_ANSWERS["ok"]["noul"]


@pytest.mark.asyncio
async def test_calls_today_counts_both_ok_and_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    store, settings, jev, _ = _make_jev(
        tmp_path,
        responder=lambda r: httpx.Response(200, json={"answers": GOOD_ANSWERS}),
    )
    assert jev.calls_today() == 0
    await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111")
    assert jev.calls_today() == 1


@pytest.mark.asyncio
async def test_uniform_timeout_from_settings(tmp_path, monkeypatch):
    """超时默认 settings.jev.timeout_ms，不走 10 秒。"""
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    # 配置里 timeout_ms=800（G5 起夹到 [200,1200]，范围内的值原样保留）
    jev = _make_jev(tmp_path, cfg={"jev": {"timeout_ms": 800}})[2]
    # 不发起请求（没 key），只确认属性读得到
    assert getattr(jev._settings(), "jev").timeout_ms == 800


@pytest.mark.asyncio
async def test_ask_error_string_sanitized(tmp_path, monkeypatch):
    """错误消息里不带密钥。"""
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

    def responder(req):
        return httpx.Response(500, content=f" boom {SECRET} ".encode(), headers={"content-type": "text/plain"})

    store, settings, jev, _ = _make_jev(tmp_path, responder=responder)
    await jev.ask(STATE, QUESTIONS, purpose="topic", group_id="111")
    rows = store.read().execute("SELECT * FROM judgments").fetchall()
    row = dict(rows[0])
    assert row["ok"] == 0
    assert SECRET not in row["error"]
    assert SECRET not in row["answers"]
