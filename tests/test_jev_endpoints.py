"""多判断端点：内置预设、[[jev_endpoints]] 配置、两个协议适配器、Jev 客户端。

覆盖：
- jev_presets 内置预设表（顺序 / 协议 / 必填字段）；
- [jev] use + [[jev_endpoints]] 解析规则（id / 保留 id / 协议 / 地址 / 模型名 / 名字 /
  重复 / 占位地址 / 本机 http / 预设补全）；
- resolve_target（内置 / 自己加的 / 选了不存在的 / 密钥来源）；
- build_body + normalize_response（systemone 与 openai_decisions；Cloudflare 包一层 result）；
- Jev.ask 按目标走（url / model / key / body / 错误带目标 id / 熔断按目标各算）；
- Jev.test_endpoint（真发一次、不落库、不动熔断、错误遮密钥、状态码带短正文）；
- onboarding 的 jev 检查跟当前目标；console 健康行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "jev-endpoint-testkey-123"  # 假密钥；专门用来断言「绝不出现在任何输出里」

STATE = {"messages": [{"speaker": "USER", "text": "最近那个开源项目怎么样"}]}
QUESTIONS = {
    "ok": {"type": "noul", "instructions": "现在适合抛新话题吗"},
    "reason": {
        "type": "choice",
        "instructions": "为什么不适合",
        "criteria": {"left": "人都走了", "fine": "可以开"},
    },
}
GOOD_ANSWERS = {
    "ok": {"type": "noul", "noul": 0.8},
    "reason": {
        "type": "choice",
        "choice": "fine",
        "probabilities": {"left": 0.2, "fine": 0.8},
        "confidence": 0.9,
    },
}
SYSTEMONE_BODY = {"model": "typesafe/jev-1.13", "state": STATE, "questions": QUESTIONS}


def _settings(cfg: dict | None = None) -> Any:
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


def _make_jev(tmp_path, *, cfg: dict | None = None, responder=None):
    """返回 (store, holder, jev, log)；holder["settings"] 可以中途换（测熔断按目标重置）。"""
    from CharTyr_MaiWork.maiwork.jev import Jev

    store = Store(tmp_path / "t.db")
    store.migrate()
    holder = {"settings": _settings(cfg)}
    if responder is None:
        responder = lambda req: httpx.Response(500, json={"error": "boom"})
    log = RequestLog(responder)
    transport = httpx.MockTransport(log.handler)
    jev = Jev(store, lambda: holder["settings"], transport=transport)
    return store, holder, jev, log


# ----------------------------------------------------------------------
# 预设表
# ----------------------------------------------------------------------


class TestPresets:
    def test_order_and_protocols(self) -> None:
        from CharTyr_MaiWork.maiwork.jev_presets import PRESETS, PROTOCOLS

        assert list(PRESETS.keys()) == [
            "typesafe", "openrouter", "opencode", "commandcode", "vercel",
            "upstage", "inception", "liquid", "cloudflare", "openai",
        ]
        assert PROTOCOLS == ("systemone", "openai_decisions")
        for pid, p in PRESETS.items():
            assert p.id == pid
            assert p.name and p.url and p.model
            assert p.protocol in PROTOCOLS
            assert p.model in p.models, pid
            assert p.docs_url.startswith("https://"), pid
            assert isinstance(p.note, str)

    def test_known_facts(self) -> None:
        from CharTyr_MaiWork.maiwork.jev_presets import PRESETS

        assert PRESETS["typesafe"].url == "https://api.typesafe.ai/v1/systemone"
        assert PRESETS["typesafe"].model == "jev-1.13.0"
        assert "jev-latest" in PRESETS["typesafe"].models
        assert PRESETS["openrouter"].url == "https://openrouter.ai/api/v1/systemone"
        assert PRESETS["openrouter"].model == "typesafe/jev-1.13"
        assert PRESETS["openrouter"].key_url == "https://openrouter.ai/keys"
        assert "~typesafe/jev-latest" in PRESETS["openrouter"].models
        assert PRESETS["opencode"].url == "https://opencode.ai/zen/v1/systemone"
        assert "jev-1.13-free" in PRESETS["opencode"].models
        assert PRESETS["commandcode"].url == "https://api.commandcode.ai/provider/v1/systemone"
        assert PRESETS["vercel"].url == "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
        assert PRESETS["vercel"].model == "typesafe-ai/jev"
        assert PRESETS["openai"].url == "https://api.openai.com/v1/decisions"
        assert PRESETS["openai"].model == "gpt-6-luna"
        assert PRESETS["openai"].protocol == "openai_decisions"

    def test_added_providers(self) -> None:
        from CharTyr_MaiWork.maiwork.jev_presets import PRESETS

        up = PRESETS["upstage"]
        assert (up.name, up.protocol, up.url, up.model) == (
            "Upstage Solar Decide", "systemone",
            "https://api.upstage.ai/v1/systemone", "solar-decide",
        )
        assert up.models == ("solar-decide",)
        assert up.note == "公测；choice 最多 26 个选项"
        inc = PRESETS["inception"]
        assert (inc.name, inc.url, inc.model) == (
            "Inception Mercury Decide", "https://api.inceptionlabs.ai/v1/decisions", "mercury-decide",
        )
        assert inc.note == ""
        liq = PRESETS["liquid"]
        assert (liq.url, liq.model) == ("https://api.liquid.ai/decisions/v1/systemone", "d1")
        assert liq.models == ("d1", "d1:free")
        assert liq.note == "d1:free 免费档"
        cf = PRESETS["cloudflare"]
        assert cf.name == "Cloudflare Clef"
        assert cf.url == (
            "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/cloudflare/clef-flash"
        )
        assert cf.models == ("clef-flash", "clef")
        assert "account_id" in cf.note

    def test_frozen(self) -> None:
        from CharTyr_MaiWork.maiwork.jev_presets import PRESETS

        with pytest.raises(Exception):
            PRESETS["typesafe"].model = "x"  # type: ignore[misc]


# ----------------------------------------------------------------------
# 配置解析
# ----------------------------------------------------------------------


class TestJevConfig:
    def test_use_default_and_value(self) -> None:
        assert _settings().jev.use == "typesafe"
        assert _settings({"jev": {"use": "or"}}).jev.use == "or"
        assert _settings({"jev": {"use": "  "}}).jev.use == "typesafe"

    def test_endpoint_from_preset_fills_fields(self) -> None:
        s = _settings({"jev_endpoints": [{"id": "or", "preset": "openrouter", "api_key": "k"}]})
        assert s.problems == ()
        e = s.jev_endpoints[0]
        assert (e.id, e.preset, e.name, e.protocol, e.url, e.model, e.api_key) == (
            "or", "openrouter", "OpenRouter", "systemone",
            "https://openrouter.ai/api/v1/systemone", "typesafe/jev-1.13", "k",
        )

    def test_endpoint_custom_no_preset(self) -> None:
        s = _settings({"jev_endpoints": [{
            "id": "mine", "name": "自家判断", "protocol": "openai_decisions",
            "url": "https://x.test/v1/decisions/", "model": "gpt-6-luna", "api_key": "k",
        }]})
        assert s.problems == ()
        e = s.jev_endpoints[0]
        assert (e.preset, e.name, e.protocol, e.model) == ("", "自家判断", "openai_decisions", "gpt-6-luna")
        assert e.url == "https://x.test/v1/decisions"

    def test_endpoint_name_defaults_to_id(self) -> None:
        s = _settings({"jev_endpoints": [{
            "id": "mine", "url": "https://x.test/v1/systemone", "model": "m",
        }]})
        assert s.jev_endpoints[0].name == "mine"

    def test_explicit_values_beat_preset(self) -> None:
        s = _settings({"jev_endpoints": [{
            "id": "or", "preset": "openrouter", "url": "https://proxy.test/v1/systemone", "model": "my-jev",
        }]})
        e = s.jev_endpoints[0]
        assert e.url == "https://proxy.test/v1/systemone" and e.model == "my-jev"

    def test_unknown_preset_kept_as_custom(self) -> None:
        s = _settings({"jev_endpoints": [{
            "id": "x", "preset": "nope", "url": "https://x.test/v1/systemone", "model": "m",
        }]})
        assert s.jev_endpoints[0].preset == ""
        assert any("预设" in p for p in s.problems)

    @pytest.mark.parametrize(
        ("entry", "needle"),
        [
            ({"id": "Bad", "url": "https://x.test/v1/systemone", "model": "m"}, "id"),
            ({"id": "x" * 40, "url": "https://x.test/v1/systemone", "model": "m"}, "id"),
            ({"id": "typesafe", "url": "https://x.test/v1/systemone", "model": "m"}, "内置"),
            ({"id": "x", "url": "https://x.test/v1/systemone", "model": "m", "protocol": "nope"}, "协议"),
            ({"id": "x", "url": "http://api.test/v1/systemone", "model": "m"}, "https"),
            ({"id": "x", "url": "https://x.test/{account_id}/systemone", "model": "m"}, "占位"),
            ({"id": "x", "url": "https://x.test/v1/systemone", "model": ""}, "模型名"),
            ({"id": "x", "url": "https://x.test/v1/systemone", "model": "m" * 201}, "模型名"),
            ({"id": "x", "url": "https://x.test/v1/systemone", "model": "m", "name": "名" * 41}, "名字"),
        ],
    )
    def test_bad_entries_dropped(self, entry: dict, needle: str) -> None:
        s = _settings({"jev_endpoints": [entry]})
        assert s.jev_endpoints == ()
        assert any(needle in p for p in s.problems), s.problems

    def test_local_http_allowed(self) -> None:
        for url in (
            "http://localhost:8080/v1/systemone",
            "http://127.0.0.1:8080/v1/systemone",
            "http://[::1]:8080/v1/systemone",
        ):
            s = _settings({"jev_endpoints": [{"id": "local", "url": url, "model": "m"}]})
            assert s.jev_endpoints and s.jev_endpoints[0].url == url, (url, s.problems)

    def test_duplicate_id_dropped(self) -> None:
        s = _settings({"jev_endpoints": [
            {"id": "a", "url": "https://a.test/v1/systemone", "model": "m"},
            {"id": "a", "url": "https://b.test/v1/systemone", "model": "m"},
        ]})
        assert [e.id for e in s.jev_endpoints] == ["a"]
        assert any("重复" in p for p in s.problems)

    def test_not_a_list_ignored(self) -> None:
        s = _settings({"jev_endpoints": {"id": "a"}})
        assert s.jev_endpoints == ()
        assert any("表数组" in p for p in s.problems)

    def test_cloudflare_preset_needs_account_id(self) -> None:
        s = _settings({"jev_endpoints": [{"id": "cf", "preset": "cloudflare", "api_key": "k"}]})
        assert s.jev_endpoints == ()
        assert any("account_id" in p for p in s.problems)


# ----------------------------------------------------------------------
# resolve_target
# ----------------------------------------------------------------------


class TestResolveTarget:
    def test_default_is_builtin_typesafe(self, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        from CharTyr_MaiWork.maiwork.jev import resolve_target

        t = resolve_target(_settings())
        assert t is not None
        assert (t.id, t.name, t.protocol) == ("typesafe", "TypeSafe 官方", "systemone")
        assert t.url == "https://api.typesafe.ai/v1/systemone" and t.model == "jev-1.13.0"
        assert t.key == ""

    def test_builtin_key_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
        from CharTyr_MaiWork.maiwork.jev import resolve_target

        assert resolve_target(_settings()).key == SECRET

    def test_builtin_key_from_config(self, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        from CharTyr_MaiWork.maiwork.jev import resolve_target

        s = _settings({"jev": {"api_key": "cfg-key", "model": "jev-x", "api_url": "https://my.test/v1/systemone"}})
        t = resolve_target(s)
        assert (t.key, t.model, t.url) == ("cfg-key", "jev-x", "https://my.test/v1/systemone")

    def test_custom_target(self, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        from CharTyr_MaiWork.maiwork.jev import resolve_target

        s = _settings({
            "jev": {"use": "or"},
            "jev_endpoints": [{"id": "or", "preset": "openrouter", "api_key": SECRET}],
        })
        t = resolve_target(s)
        assert (t.id, t.name, t.protocol, t.model, t.key) == (
            "or", "OpenRouter", "systemone", "typesafe/jev-1.13", SECRET,
        )
        assert t.url == "https://openrouter.ai/api/v1/systemone"

    def test_missing_endpoint_returns_none(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import resolve_target

        assert resolve_target(_settings({"jev": {"use": "ghost"}})) is None


# ----------------------------------------------------------------------
# build_body
# ----------------------------------------------------------------------


class TestBuildBody:
    def test_systemone_shape(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        assert build_body("systemone", "jev-1.13.0", STATE, QUESTIONS) == {
            "model": "jev-1.13.0", "state": STATE, "questions": QUESTIONS,
        }

    def test_openai_noul_is_predicate(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        body = build_body(
            "openai_decisions", "gpt-6-luna", "群里有 3 条消息",
            {"greeting": {"type": "noul", "instructions": "这条消息是不是在打招呼？"}},
        )
        assert body == {
            "model": "gpt-6-luna",
            "input": "群里有 3 条消息",
            "questions": [{"type": "predicate", "name": "greeting", "instructions": "这条消息是不是在打招呼？"}],
        }

    def test_openai_noul_with_true_false_criteria(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        body = build_body(
            "openai_decisions", "m", "x",
            {"greeting": {"type": "noul", "instructions": "在打招呼吗",
                          "criteria": {"true": "是", "false": "否"}}},
        )
        assert body["questions"][0]["instructions"] == "在打招呼吗（是：是；否：否）"
        assert "criteria" not in body["questions"][0]

    def test_openai_choice_uses_criteria_order(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        body = build_body(
            "openai_decisions", "m", "x",
            {"lang": {"type": "choice", "instructions": "什么语言",
                      "criteria": {"zh": "中文", "en": "英文"}}},
        )
        assert body["questions"][0] == {
            "type": "choice", "name": "lang", "instructions": "什么语言",
            "choices": [{"value": "zh", "description": "中文"}, {"value": "en", "description": "英文"}],
        }

    def test_openai_score_levels(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        body = build_body(
            "openai_decisions", "m", "x",
            {"s": {"type": "score", "instructions": "打分", "criteria": ["高", "低"]}},
        )
        assert body["questions"][0] == {
            "type": "score", "name": "s", "instructions": "打分",
            "levels": [{"label": "高"}, {"label": "低"}],
        }

    def test_openai_state_dict_serialized(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        body = build_body("openai_decisions", "m", {"a": "中文"}, {"k": {"type": "noul", "instructions": "?"}})
        assert body["input"] == json.dumps({"a": "中文"}, ensure_ascii=False)

    def test_openai_non_str_instructions_json(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        body = build_body(
            "openai_decisions", "m", "x",
            {"k": {"type": "noul", "instructions": {"zh": "说中文"}}},
        )
        assert body["questions"][0]["instructions"] == json.dumps({"zh": "说中文"}, ensure_ascii=False)

    def test_unknown_type_and_protocol_raise(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import build_body

        with pytest.raises(ValueError):
            build_body("openai_decisions", "m", "x", {"k": {"type": "nope", "instructions": "?"}})
        with pytest.raises(ValueError):
            build_body("nope", "m", "x", {"k": {"type": "noul", "instructions": "?"}})


# ----------------------------------------------------------------------
# normalize_response
# ----------------------------------------------------------------------


class TestNormalizeResponse:
    def test_systemone_passthrough(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        raw = {"answers": GOOD_ANSWERS, "model": "jev-1.13.0"}
        assert normalize_response("systemone", raw, QUESTIONS) == raw

    def test_systemone_unwraps_cloudflare_result(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        raw = {"result": {"answers": GOOD_ANSWERS}, "success": True, "errors": [], "messages": []}
        out = normalize_response("systemone", raw, QUESTIONS)
        assert out == {"answers": GOOD_ANSWERS}

    def test_systemone_cloudflare_error_message(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        raw = {"result": None, "success": False,
               "errors": [{"code": 7000, "message": "no such model"}]}
        with pytest.raises(ValueError) as e:
            normalize_response("systemone", raw, QUESTIONS)
        assert "no such model" in str(e.value)

    def test_systemone_cloudflare_error_message_sanitized(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        raw = {"success": False, "errors": [{"message": f"bad Bearer {SECRET} token"}]}
        with pytest.raises(ValueError) as e:
            normalize_response("systemone", raw, QUESTIONS)
        assert SECRET not in str(e.value)

    def test_systemone_invalid(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        with pytest.raises(ValueError):
            normalize_response("systemone", {"nope": 1}, QUESTIONS)

    def test_openai_maps_to_systemone_shape(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response, validate_answers

        raw = {"answers": [
            {"type": "predicate", "name": "ok", "probability": 0.8},
            {"type": "choice", "name": "reason", "choice": "fine",
             "probabilities": [{"value": "left", "probability": 0.2}, {"value": "fine", "probability": 0.8}],
             "confidence": 0.9},
        ]}
        out = normalize_response("openai_decisions", raw, QUESTIONS)
        assert out == {"answers": {
            "ok": {"type": "noul", "noul": 0.8},
            "reason": {"type": "choice", "choice": "fine",
                       "probabilities": {"left": 0.2, "fine": 0.8}, "confidence": 0.9},
        }}
        assert validate_answers(out, QUESTIONS) == {"ok": 0.8, "reason": ("fine", 0.8, 0.9)}

    def test_openai_score(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        raw = {"answers": [{
            "type": "score", "name": "s", "score": 0.7,
            "probabilities": [{"label": "高", "probability": 0.7}, {"label": "低", "probability": 0.3}],
            "confidence": 0.5,
        }]}
        out = normalize_response("openai_decisions", raw, {})
        assert out["answers"]["s"] == {
            "type": "score", "score": 0.7, "probabilities": {"高": 0.7, "低": 0.3}, "confidence": 0.5,
        }

    def test_openai_introspection_order(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        raw = {"answers": [{"type": "choice", "name": "c", "choice": "b",
                            "probabilities": [{"value": "a", "probability": 0.4},
                                              {"value": "b", "probability": 0.6}],
                            "confidence": 0.3}]}
        out = normalize_response("openai_decisions", raw, {})
        assert list(out["answers"]["c"]["probabilities"].keys()) == ["a", "b"]

    def test_openai_refusal_raises(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        with pytest.raises(ValueError) as e:
            normalize_response("openai_decisions", {"answers": [{"type": "refusal", "name": "ok"}]}, QUESTIONS)
        assert "refusal" in str(e.value) and "ok" in str(e.value)

    def test_openai_unknown_type_raises(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        with pytest.raises(ValueError):
            normalize_response("openai_decisions", {"answers": [{"type": "nope", "name": "ok"}]}, QUESTIONS)

    def test_openai_answers_must_be_list(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        with pytest.raises(ValueError):
            normalize_response("openai_decisions", {"answers": {"ok": 1}}, QUESTIONS)

    def test_unknown_protocol_raises(self) -> None:
        from CharTyr_MaiWork.maiwork.jev import normalize_response

        with pytest.raises(ValueError):
            normalize_response("nope", {"answers": {}}, QUESTIONS)


# ----------------------------------------------------------------------
# 客户端：按目标走
# ----------------------------------------------------------------------

CFG_TWO = {
    "jev": {"use": "or"},
    "jev_endpoints": [
        {"id": "or", "preset": "openrouter", "api_key": SECRET},
        {"id": "oa", "preset": "openai", "api_key": "oa-key"},
    ],
}


class TestAskWithTarget:
    @pytest.mark.asyncio
    async def test_ask_uses_custom_url_model_key(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def responder(req: httpx.Request) -> httpx.Response:
            assert str(req.url) == "https://openrouter.ai/api/v1/systemone"
            assert req.headers.get("authorization") == f"Bearer {SECRET}"
            assert json.loads(req.content) == SYSTEMONE_BODY
            return httpx.Response(200, json={"answers": GOOD_ANSWERS})

        store, holder, jev, log = _make_jev(tmp_path, cfg=CFG_TWO, responder=responder)
        out = await jev.ask(dict(STATE), dict(QUESTIONS), purpose="topic", group_id="111")
        assert out == {"ok": 0.8, "reason": ("fine", 0.8, 0.9)}
        assert len(log.requests) == 1

    @pytest.mark.asyncio
    async def test_ask_openai_decisions_endpoint(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        cfg = {
            "jev": {"use": "oa"},
            "jev_endpoints": [{"id": "oa", "preset": "openai", "api_key": "oa-key"}],
        }

        def responder(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert str(req.url) == "https://api.openai.com/v1/decisions"
            assert req.headers.get("authorization") == "Bearer oa-key"
            assert body["model"] == "gpt-6-luna" and body["input"] == json.dumps(STATE, ensure_ascii=False)
            assert body["questions"][0]["type"] == "predicate"
            assert body["questions"][1]["choices"] == [
                {"value": "left", "description": "人都走了"}, {"value": "fine", "description": "可以开"},
            ]
            return httpx.Response(200, json={"answers": [
                {"type": "predicate", "name": "ok", "probability": 0.8},
                {"type": "choice", "name": "reason", "choice": "fine",
                 "probabilities": [{"value": "left", "probability": 0.2},
                                   {"value": "fine", "probability": 0.8}],
                 "confidence": 0.9},
            ]})

        store, holder, jev, log = _make_jev(tmp_path, cfg=cfg, responder=responder)
        out = await jev.ask(dict(STATE), dict(QUESTIONS), purpose="topic", group_id="111")
        assert out == {"ok": 0.8, "reason": ("fine", 0.8, 0.9)}

    @pytest.mark.asyncio
    async def test_refusal_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        cfg = {
            "jev": {"use": "oa"},
            "jev_endpoints": [{"id": "oa", "preset": "openai", "api_key": "oa-key"}],
        }
        store, holder, jev, log = _make_jev(
            tmp_path, cfg=cfg,
            responder=lambda r: httpx.Response(200, json={"answers": [{"type": "refusal", "name": "ok"}]}),
        )
        assert await jev.ask(dict(STATE), dict(QUESTIONS), purpose="topic") is None
        row = dict(store.read().execute("SELECT * FROM judgments").fetchall()[0])
        assert row["ok"] == 0 and "refusal" in row["error"]

    @pytest.mark.asyncio
    async def test_failure_error_carries_target_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        store, holder, jev, log = _make_jev(
            tmp_path, cfg=CFG_TWO,
            responder=lambda r: httpx.Response(500, content=b"boom"),
        )
        assert await jev.ask(dict(STATE), dict(QUESTIONS), purpose="topic") is None
        row = dict(store.read().execute("SELECT * FROM judgments").fetchall()[0])
        assert row["error"].startswith("[or] "), row["error"]

    @pytest.mark.asyncio
    async def test_breaker_is_per_target(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def responder(req: httpx.Request) -> httpx.Response:
            if "openrouter.ai" in str(req.url):
                return httpx.Response(500, content=b"boom")
            return httpx.Response(200, json={"answers": GOOD_ANSWERS})

        cfg_two_systemone = {
            "jev": {"use": "or"},
            "jev_endpoints": [
                {"id": "or", "preset": "openrouter", "api_key": SECRET},
                {"id": "op", "preset": "opencode", "api_key": "op-key"},
            ],
        }
        store, holder, jev, log = _make_jev(tmp_path, cfg=cfg_two_systemone, responder=responder)
        for _ in range(3):
            assert await jev.ask(dict(STATE), dict(QUESTIONS), purpose="x") is None
        assert jev.available() is False
        # 换成另一个端点：失败计数和熔断要跟着目标重置
        holder["settings"] = _settings({
            "jev": {"use": "op"},
            "jev_endpoints": [{"id": "op", "preset": "opencode", "api_key": "op-key"}],
        })
        assert jev.available() is True
        assert await jev.ask(dict(STATE), dict(QUESTIONS), purpose="x") is not None

    @pytest.mark.asyncio
    async def test_available_requires_key_for_custom(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        cfg = {"jev": {"use": "or"}, "jev_endpoints": [{"id": "or", "preset": "openrouter"}]}
        store, holder, jev, log = _make_jev(tmp_path, cfg=cfg)
        assert jev.available() is False
        assert await jev.ask(dict(STATE), dict(QUESTIONS), purpose="x") is None
        assert log.requests == []

    @pytest.mark.asyncio
    async def test_available_false_when_target_missing(self, tmp_path) -> None:
        store, holder, jev, log = _make_jev(tmp_path, cfg={"jev": {"use": "ghost"}})
        assert jev.available() is False
        assert jev.current_target_info()["id"] == ""

    @pytest.mark.asyncio
    async def test_current_target_info(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        store, holder, jev, log = _make_jev(tmp_path, cfg=CFG_TWO)
        info = jev.current_target_info()
        assert info == {
            "id": "or", "name": "OpenRouter", "protocol": "systemone",
            "model": "typesafe/jev-1.13", "key_set": True,
        }
        assert SECRET not in json.dumps(info, ensure_ascii=False)


# ----------------------------------------------------------------------
# test_endpoint（网页「测试」按钮）
# ----------------------------------------------------------------------


class TestTestEndpoint:
    @pytest.mark.asyncio
    async def test_ok_systemone(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def responder(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert str(req.url) == "https://probe.test/v1/systemone"
            assert req.headers.get("authorization") == f"Bearer {SECRET}"
            assert body["state"] == {"message": "你好，今天天气不错"}
            return httpx.Response(200, json={"answers": {
                "greeting": {"type": "noul", "noul": 0.92},
                "lang": {"type": "choice", "choice": "zh",
                         "probabilities": {"zh": 0.9, "en": 0.1}, "confidence": 0.93},
            }})

        store, holder, jev, log = _make_jev(tmp_path, responder=responder)
        res = await jev.test_endpoint(
            protocol="systemone", url="https://probe.test/v1/systemone", model="jev-1.13.0", key=SECRET,
        )
        assert res["ok"] is True and isinstance(res["ms"], int) and res["error"] == ""
        assert res["answers"] == {"greeting": 0.92, "lang": ["zh", 0.9, 0.93]}
        # 不落库、不动熔断、不动「今天判断几次」
        assert store.read().execute("SELECT COUNT(*) AS c FROM judgments").fetchone()["c"] == 0
        assert jev.calls_today() == 0

    @pytest.mark.asyncio
    async def test_ok_openai_decisions(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def responder(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert str(req.url) == "https://api.openai.com/v1/decisions"
            assert body["questions"][0]["type"] == "predicate"
            assert body["questions"][1]["type"] == "choice"
            return httpx.Response(200, json={"answers": [
                {"type": "predicate", "name": "greeting", "probability": 0.9},
                {"type": "choice", "name": "lang", "choice": "zh",
                 "probabilities": [{"value": "zh", "probability": 0.9}, {"value": "en", "probability": 0.1}],
                 "confidence": 0.8},
            ]})

        store, holder, jev, log = _make_jev(tmp_path, responder=responder)
        res = await jev.test_endpoint(
            protocol="openai_decisions", url="https://api.openai.com/v1/decisions",
            model="gpt-6-luna", key="oa-key",
        )
        assert res["ok"] is True
        assert res["answers"] == {"greeting": 0.9, "lang": ["zh", 0.9, 0.8]}

    @pytest.mark.asyncio
    async def test_http_error_body_sanitized_and_short(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        long_body = f"boom {SECRET} " + "x" * 400

        store, holder, jev, log = _make_jev(
            tmp_path, responder=lambda r: httpx.Response(503, content=long_body.encode()),
        )
        res = await jev.test_endpoint(
            protocol="systemone", url="https://probe.test/v1/systemone", model="m", key=SECRET,
        )
        assert res["ok"] is False
        assert "http_503" in res["error"]
        assert SECRET not in res["error"] and "***" in res["error"]
        assert len(res["error"]) <= 300

    @pytest.mark.asyncio
    async def test_bad_answers_reported_not_raised(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        store, holder, jev, log = _make_jev(
            tmp_path, responder=lambda r: httpx.Response(200, content=b"not-json"),
        )
        res = await jev.test_endpoint(protocol="systemone", url="https://probe.test/v1/systemone", model="m", key="k")
        assert res["ok"] is False and res["error"]
        assert res["answers"] == {}

    @pytest.mark.asyncio
    async def test_network_error_reported(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def responder(req):
            raise httpx.ConnectError("boom", request=req)

        store, holder, jev, log = _make_jev(tmp_path, responder=responder)
        res = await jev.test_endpoint(protocol="systemone", url="https://probe.test/v1/systemone", model="m", key="k")
        assert res["ok"] is False and res["error"]

    @pytest.mark.asyncio
    async def test_does_not_open_breaker(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)

        def responder(req):
            if "probe.test" in str(req.url):
                return httpx.Response(500, content=b"boom")
            return httpx.Response(200, json={"answers": GOOD_ANSWERS})

        store, holder, jev, log = _make_jev(tmp_path, responder=responder)
        res = await jev.test_endpoint(protocol="systemone", url="https://probe.test/v1/systemone", model="m", key="k")
        assert res["ok"] is False
        assert jev.available() is True

    @pytest.mark.asyncio
    async def test_bad_protocol_rejected(self, tmp_path) -> None:
        store, holder, jev, log = _make_jev(tmp_path)
        res = await jev.test_endpoint(protocol="nope", url="https://probe.test/v1/systemone", model="m", key="k")
        assert res["ok"] is False and res["error"]
        assert log.requests == []


# ----------------------------------------------------------------------
# onboarding / 健康行
# ----------------------------------------------------------------------


class _FakeSvc:
    def __init__(self, settings: Any, jev: Any = None) -> None:
        self._settings = settings
        self.jev = jev
        self.models = None
        self.search = None

    def get_settings(self) -> Any:
        return self._settings


class TestOnboardingCheck:
    def test_check_follows_active_target(self, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        from CharTyr_MaiWork.maiwork.onboarding import _checks

        assert _checks(_FakeSvc(_settings()))["jev"] is False
        custom = _settings({
            "jev": {"use": "or"},
            "jev_endpoints": [{"id": "or", "preset": "openrouter", "api_key": SECRET}],
        })
        assert _checks(_FakeSvc(custom))["jev"] is True
        ghost = _settings({"jev": {"use": "ghost"}})
        assert _checks(_FakeSvc(ghost))["jev"] is False
        no_key = _settings({"jev": {"use": "or"}, "jev_endpoints": [{"id": "or", "preset": "openrouter"}]})
        assert _checks(_FakeSvc(no_key))["jev"] is False


class TestHealthLine:
    def test_ok_shows_target_name(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
        from CharTyr_MaiWork.maiwork.console import views
        from CharTyr_MaiWork.maiwork.jev import Jev

        settings = _settings({
            "jev": {"use": "or"},
            "jev_endpoints": [{"id": "or", "preset": "openrouter", "api_key": SECRET}],
        })
        store = Store(tmp_path / "h.db")
        store.migrate()
        jev = Jev(store, lambda: settings, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        monkeypatch.setattr(jev, "calls_today", lambda: 7)
        item = views._jev_health(_FakeSvc(settings, jev))
        assert item["state"] == "ok" and item["text"] == "能用 · OpenRouter · 今天判断 7 次"

    def test_missing_endpoint_warns(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        from CharTyr_MaiWork.maiwork.console import views
        from CharTyr_MaiWork.maiwork.jev import Jev

        settings = _settings({"jev": {"use": "ghost"}})
        store = Store(tmp_path / "h.db")
        store.migrate()
        jev = Jev(store, lambda: settings, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        item = views._jev_health(_FakeSvc(settings, jev))
        assert item["state"] == "warn" and "不见了" in item["text"]

    def test_no_key_warns(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        from CharTyr_MaiWork.maiwork.console import views
        from CharTyr_MaiWork.maiwork.jev import Jev

        settings = _settings({})
        store = Store(tmp_path / "h.db")
        store.migrate()
        jev = Jev(store, lambda: settings, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        item = views._jev_health(_FakeSvc(settings, jev))
        assert item["state"] == "warn" and item["text"] == "没找到密钥"


class TestResolveKeyStillWorks:
    def test_resolve_key_exported(self, monkeypatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
        from CharTyr_MaiWork.maiwork.jev import _resolve_key

        assert _resolve_key(_settings()) == SECRET
        assert os.environ.get("TYPESAFE_API_KEY") == SECRET
