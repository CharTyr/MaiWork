"""验证记录绑定真实候选与配置签名（docs/14 R03，2026-10）。

老规矩：kv["models.verified.<条目id>"] 按「服务端模型名」找最新记录就算已验证——
同名模型换端点地址、换协议、换密钥、换条目 id 都会串记录，错误地显示「已验证」。

新规矩（本文件约束的对外口）：
- Models.verification_stamp(entry_id, *, max_tokens=None)：把条目 id、端点 id、服务模型、
  协议、规范化 base_url、密钥（哈希）、上下文窗口/最大输出、能力字段（efforts/vision）
  做成稳定签名（sha256 十六进制）；绝不输出密钥原文。
- Models.verification_for(kind='main')：按岗位真实候选链的首选候选条目 id 读记录，
  只有记录签名和当前配置签名一致才返回；记录缺签名、旧格式（按名字）、换过配置 → None。
- verify_entry 结果带 fingerprint（实际验证通过的输出上限的签名）、
  requested_fingerprint（请求开始时的配置签名）和原字段；降档通过但配置没跟上时
  verification_for 返回派生的「建议最大输出尚未保存」失败记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

SECRET_A = "sk-super-secret-alpha-0001"
SECRET_B = "sk-super-secret-bravo-0002"


def _settings(max_tokens: int = 32768, base_url: str = "https://api.test/v1",
              api_key: str = SECRET_A, entry_id: str = "m1", endpoint_id: str = "default",
              service_model: str = "gpt-x"):
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "endpoints": [{"id": endpoint_id, "base_url": base_url, "api_key": api_key}],
            "model_list": [
                {"id": entry_id, "endpoint": endpoint_id, "model": service_model,
                 "max_tokens": max_tokens}
            ],
        }
    )
    assert problems == []
    return settings


def _reply(content: str = "OK", tool_calls=None) -> httpx.Response:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": msg}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 1}})


def _ping_reply(word: str = "hi") -> httpx.Response:
    return _reply("", [{"id": "c1", "type": "function",
                        "function": {"name": "maiwork_ping", "arguments": json.dumps({"word": word})}}])


def _mk(tmp_path: Path, handler, settings=None):
    """建好 store/settings/agents/models，main 岗位指向 settings 里的第一条模型条目。"""
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = settings or _settings()
    agents = Agents(store, lambda: settings)
    agents.update_profile("main", {"model": "m1"})
    return store, agents, Models(store, lambda: settings, agents=agents,
                                 transport=httpx.MockTransport(handler))


def _seed_ok_record(store, entry_id: str, *, fp: str, req_fp: str, model: str = "gpt-x",
                    endpoint: str = "default", tools_ok: bool = True,
                    suggested_max_tokens: int = 0, ok: bool = True) -> None:
    """模拟主会话 server 存一条验证记录（含两个签名）。"""
    with store.tx() as conn:
        store.kv_set(conn, f"models.verified.{entry_id}", {
            "model": model, "endpoint": endpoint,
            "ok": ok, "tools_ok": tools_ok,
            "note": "", "error": "" if ok else "模拟失败",
            "fingerprint": fp, "requested_fingerprint": req_fp,
            "suggested_max_tokens": suggested_max_tokens,
            "ts": 1234567.0,
        })


async def test_stamp_stable_and_hides_secret(tmp_path: Path) -> None:
    """同一份配置两次取签名一致；签名本身不含密钥、地址明文只占位长度。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        a = models.verification_stamp("m1")
        b = models.verification_stamp("m1")
        assert a and a == b
        assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
        assert SECRET_A not in a and "api.test" not in a
        assert models.verification_stamp("nope") == ""
    finally:
        await models.close(); store.close()


async def test_stamp_changes_with_capability_fields(tmp_path: Path) -> None:
    """地址（含大小写）、密钥、协议、模型名、输出上限、上下文窗口、能力勾选都进签名。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        base = models.verification_stamp("m1")
        # 换密钥 → 变
        other_key = _settings(api_key=SECRET_B)
        models._get_settings = lambda: other_key
        assert models.verification_stamp("m1") != base
        # 换地址 → 变；仅大小写差 → 不变（规范化）
        up = _settings(base_url="https://API.TEST/v1")
        models._get_settings = lambda: up
        assert models.verification_stamp("m1") == base
        other_url = _settings(base_url="https://api.other/v1")
        models._get_settings = lambda: other_url
        assert models.verification_stamp("m1") != base
        # 换输出上限（默认读条目值；调用方显式传以调用方为准）→ 变
        assert models.verification_stamp("m1", max_tokens=8192) != base
        lowered = _settings(max_tokens=8192)
        models._get_settings = lambda: lowered
        assert models.verification_stamp("m1") == models.verification_stamp("m1", max_tokens=8192)
        assert models.verification_stamp("m1") != base
    finally:
        await models.close(); store.close()


async def test_verification_for_none_without_record(tmp_path: Path) -> None:
    """没记录 → None（未验证，不声明已验证）。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_verification_for_matches_current_candidate(tmp_path: Path) -> None:
    """签名相符 → 返回记录本体（dict 拷贝），含签名与建议值。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        fp = models.verification_stamp("m1")
        _seed_ok_record(store, "m1", fp=fp, req_fp=fp)
        rec = models.verification_for("main")
        assert rec is not None and rec["ok"] is True and rec["fingerprint"] == fp
        rec["ok"] = "tampered"
        assert models.verification_for("main")["ok"] is True  # 返回的是拷贝
    finally:
        await models.close(); store.close()


async def test_identity_change_same_name_different_endpoint(tmp_path: Path) -> None:
    """R03 主场景：模型名不变，端点地址换了 → 旧记录不再算已验证。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        fp = models.verification_stamp("m1")
        _seed_ok_record(store, "m1", fp=fp, req_fp=fp)
        assert models.verification_for("main") is not None
        # 同一端点 id、同一模型名，只换地址（热更新后 get_settings 返回新对象）
        changed = _settings(base_url="https://api.changed/v1")
        models._get_settings = lambda: changed
        agents._get_settings = lambda: changed
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_same_endpoint_same_model_different_entry_id(tmp_path: Path) -> None:
    """同名模型、同端点，但换了一个条目 id 在用它 → 旧记录不认（记录按条目 id 存）。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        fp = models.verification_stamp("m1")
        _seed_ok_record(store, "m1", fp=fp, req_fp=fp)
        assert models.verification_for("main") is not None
        changed = _settings(entry_id="m2")  # 新条目 id，模型名/端点完全一样
        models._get_settings = lambda: changed
        agents._get_settings = lambda: changed
        agents.update_profile("main", {"model": "m2"})
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_legacy_record_without_stamp_is_unknown(tmp_path: Path) -> None:
    """旧安装留下的按名字记录（没有签名）→ None，既不算已验证也不算明确失败。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        with store.tx() as conn:
            store.kv_set(conn, "models.verified.m1", {
                "model": "gpt-x", "endpoint": "default",
                "ok": True, "tools_ok": True, "note": "", "error": "", "ts": 1.0,
            })
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_candidate_fallback_uses_main_chain(tmp_path: Path) -> None:
    """岗位没选模型 → 兜底用主模型链：news 岗位读到的是主模型条目的验证记录。"""
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        fp = models.verification_stamp("m1")
        _seed_ok_record(store, "m1", fp=fp, req_fp=fp)
        assert models.verification_for("news") is not None
        assert models.verification_for("news")["fingerprint"] == fp
        # 什么候选都没有的岗位体系 → None
        agents.update_profile("main", {"model": ""})
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_role_slots_are_separate(tmp_path: Path) -> None:
    """不同岗位用不同条目时各看各的记录：主模型已验证不代表任务岗已验证。"""
    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "endpoints": [{"id": "default", "base_url": "https://api.test/v1", "api_key": SECRET_A}],
            "model_list": [
                {"id": "m1", "endpoint": "default", "model": "gpt-main"},
                {"id": "w1", "endpoint": "default", "model": "gpt-worker"},
            ],
        }
    )
    assert problems == []
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    agents = Agents(store, lambda: settings)
    agents.update_profile("main", {"model": "m1"})
    agents.update_profile("task", {"model": "w1"})
    models = Models(store, lambda: settings, agents=agents, transport=httpx.MockTransport(lambda req: _reply()))
    try:
        fp_m1 = models.verification_stamp("m1")
        _seed_ok_record(store, "m1", fp=fp_m1, req_fp=fp_m1, model="gpt-main")
        assert models.verification_for("main") is not None
        assert models.verification_for("task") is None  # 任务岗的条目没记录
        fp_w1 = models.verification_stamp("w1")
        _seed_ok_record(store, "w1", fp=fp_w1, req_fp=fp_w1, model="gpt-worker")
        assert models.verification_for("task") is not None
    finally:
        await models.close(); store.close()


async def test_failed_record_binds_to_identity(tmp_path: Path) -> None:
    """明确失败的记录也绑定身份：签名相符就如实返回失败；换了地址后旧失败不算数。"""
    store, agents, models = _mk(tmp_path, lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))
    try:
        r = await models.verify_entry("m1")
        assert r["ok"] is False and r["fingerprint"] and r["requested_fingerprint"] == r["fingerprint"]
        _seed_ok_record(store, "m1", fp=r["fingerprint"], req_fp=r["requested_fingerprint"], ok=False)
        rec = models.verification_for("main")
        assert rec is not None and rec["ok"] is False
        # 换了地址：旧失败记录不跟着走（那是旧配置的事）
        changed = _settings(base_url="https://api.changed/v1")
        models._get_settings = lambda: changed
        agents._get_settings = lambda: changed
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_verify_result_fingerprints_and_capture(tmp_path: Path) -> None:
    """并发换代：请求进行中 get_settings 换成新对象，结果里的签名仍是开始时的配置。"""
    settings_v1 = _settings()
    settings_v2 = _settings(base_url="https://api.changed/v1")
    box = {"settings": settings_v1}
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    agents = Agents(store, lambda: box["settings"])
    agents.update_profile("main", {"model": "m1"})

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tools"):
            return _ping_reply()
        return _reply("OK")

    models = Models(store, lambda: box["settings"], agents=agents,
                    transport=httpx.MockTransport(handler))
    try:
        expected = models.verification_stamp("m1")
        # 模拟「验证请求已发出、配置此刻被热更新」：打完第一枪就换代
        orig_chat = models.chat

        async def swap_then_chat(*a, **k):
            r = await orig_chat(*a, **k)
            box["settings"] = settings_v2
            agents._get_settings = lambda: settings_v2
            return r

        models.chat = swap_then_chat
        r = await models.verify_entry("m1")
        assert r["ok"] is True
        assert r["requested_fingerprint"] == expected == r["fingerprint"]
        # 当前配置已经是 v2：旧签名对不上 → 未验证
        assert models.verification_for("main") is None
    finally:
        await models.close(); store.close()


async def test_derived_pending_save_then_saved(tmp_path: Path) -> None:
    """降档 8192 通过但配置还是 32768：verification_for 给派生的「建议最大输出尚未保存」
    失败记录（不声称当前配置已通过）；真保存成 8192 后同一条记录自动生效。"""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        mt = body.get("max_tokens") or body.get("max_completion_tokens") or 0
        if mt > 8192:
            return httpx.Response(400, json={"error": {"message": "max_tokens must be <= 8192"}})
        if body.get("tools"):
            return _ping_reply()
        return _reply("OK")

    store, agents, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
        assert r["ok"] is True and r["suggested_max_tokens"] == 8192
        assert r["fingerprint"] != r["requested_fingerprint"]
        assert r["fingerprint"] == models.verification_stamp("m1", max_tokens=8192)
        # 主会话按约定把两个签名和建议值存下来
        _seed_ok_record(store, "m1", fp=r["fingerprint"], req_fp=r["requested_fingerprint"],
                        suggested_max_tokens=8192)
        rec = models.verification_for("main")
        assert rec is not None and rec["ok"] is False
        assert "8192" in rec["error"] and "尚未保存" in rec["error"]
        # 把建议值真存进配置 → 记录自动有效
        saved = _settings(max_tokens=8192)
        models._get_settings = lambda: saved
        agents._get_settings = lambda: saved
        rec2 = models.verification_for("main")
        cur = models.verification_stamp("m1")
        assert cur == r["fingerprint"], (cur, r["fingerprint"], r["requested_fingerprint"])
        assert rec2 is not None and rec2["ok"] is True and rec2["tools_ok"] is True
    finally:
        await models.close(); store.close()


async def test_no_secret_leak_in_results_and_records(tmp_path: Path) -> None:
    """结果、派生记录、签名、数据库里的 kv 值都不出现密钥原文。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {SECRET_A}"}})

    store, agents, models = _mk(tmp_path, handler)
    try:
        r = await models.verify_entry("m1")
        assert SECRET_A not in json.dumps(r, ensure_ascii=False)
        _seed_ok_record(store, "m1", fp=r["fingerprint"], req_fp=r["requested_fingerprint"], ok=False)
        rec = models.verification_for("main")
        assert rec is not None and SECRET_A not in json.dumps(rec, ensure_ascii=False)
        raw = store.read().execute("SELECT value FROM kv WHERE key='models.verified.m1'").fetchone()[0]
        assert SECRET_A not in raw
    finally:
        await models.close(); store.close()


async def test_stamp_keeps_case_sensitive_url_path(tmp_path):
    store, agents, models = _mk(tmp_path, lambda req: _reply())
    try:
        first = models.verification_stamp('m1')
        models._get_settings = lambda: _settings(base_url='https://API.TEST/v1/')
        assert models.verification_stamp('m1') == first
        models._get_settings = lambda: _settings(base_url='https://api.test/V1')
        assert models.verification_stamp('m1') != first
    finally:
        await models.close(); store.close()


async def test_empty_key_has_no_verification_stamp(tmp_path):
    store, agents, models = _mk(tmp_path, lambda req: _reply(), settings=_settings(api_key=''))
    try:
        assert models.verification_stamp('m1') == ''
    finally:
        await models.close(); store.close()
