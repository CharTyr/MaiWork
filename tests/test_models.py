"""models.py 单元测试：模型设置来源优先级、密钥只进不出、主备回落、用量记录。

全部通过 httpx.MockTransport（不用真网络）。
假端点服务 FakeEndpoint 按模型名/序号决定响应，记录每个模型收到的请求体。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.models import ModelError, Models
from CharTyr_MaiWork.store import Store

# 测试里用的假密钥；专门用来断言「绝不出现在任何输出里」
# SECRET 用不带 sk- 前缀的形式，确保遮罩靠的是「已知密钥精确替换」，
# 而不只是 sk- / Bearer 这两个正则碰巧盖掉。
SECRET = "testsecret123"
OTHER_SECRET = "sk-othersecret456"
BEARER_SECRET = "zz-bearer-secret"

OK_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def _settings(settings_dict: dict) -> object:
    settings, _ = load_settings(settings_dict)
    return settings


# ----------------------------------------------------------------------
# 假 OpenAI 兼容端点
# ----------------------------------------------------------------------


class FakeEndpoint:
    """按「第 N 次收到某模型 → 怎么回应」配置行为的假端点。

    behavior: 模型名 -> 响应描述列表（按该模型被调用的顺序消耗；耗尽用最后一个）。
    响应描述：
      {"kind": "ok"}                       正常回答
      {"kind": "status", "status": 500, "body": ...}   指定状态码
      {"kind": "timeout"}                  不回应（触发 httpx 超时）
      {"kind": "ok", "tool": {...}}        带 tool_calls 的回答
    """

    def __init__(self, behavior: dict, *, models: list[str] | None = None) -> None:
        self.behavior = {k: list(v) for k, v in behavior.items()}
        self.model_ids = list(models or [])
        self.calls: list[dict] = []  # {"model","body","auth"}
        self.chat_count = 0

    def _behavior_for(self, model: str, index: int) -> dict:
        steps = self.behavior.get(model)
        if not steps:
            return {"kind": "ok"}
        return steps[min(index, len(steps) - 1)]

    def _finish(self, model: str, step: dict, body: dict) -> httpx.Response:
        kind = step.get("kind", "ok")
        self.calls.append({"model": model, "body": body, "auth": None})
        self.chat_count += 1
        if kind == "status":
            payload = step.get("body", {"error": {"message": "服务端错误"}})
            if isinstance(payload, str):
                return httpx.Response(step["status"], content=payload)
            return httpx.Response(step["status"], json=payload)
        message: dict = {"role": "assistant", "content": step.get("content", f"来自{model}的回答")}
        if step.get("tool"):
            message["tool_calls"] = [step["tool"]]
        usage = step.get("usage", OK_USAGE)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        # 未授权也要能看到 Authorization 头（记录用），但这里统一按路径分
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in self.model_ids]})
        if request.url.path.endswith("/chat/completions"):
            try:
                body = json.loads(request.content or b"{}")
            except ValueError:
                body = {}
            model = body.get("model", "")
            index = sum(1 for c in self.calls if c["model"] == model)
            step = self._behavior_for(model, index)
            if step.get("kind") == "timeout":
                raise httpx.ReadTimeout("读取超时", request=request)
            # 这里对 status/ok 补记 authorization
            before = self.chat_count
            resp = self._finish(model, step, body)
            if self.chat_count > before:
                self.calls[-1]["auth"] = request.headers.get("authorization", "")
            return resp
        return httpx.Response(404, json={"error": {"message": "路径不存在"}})


def _transport(ep: FakeEndpoint) -> httpx.MockTransport:
    return httpx.MockTransport(ep.handler)


def _make_models(tmp_path, cfg: dict | None = None, ep: FakeEndpoint | None = None):
    """config_writer 假钩子：把 models.save 要写的键并进 holder 的 settings（模拟
    真链路「写 config.toml → 热更新」），返回写的键清单方便断言。"""
    store = Store(tmp_path / "test.db")
    store.migrate()
    holder = {"settings": _settings(cfg or {})}
    writes_log: list[dict] = []

    def _fake_writer(flat: dict):
        writes_log.append(dict(flat))
        models_kv = {}
        for full_key, value in flat.items():
            section, _, field = full_key.partition(".")
            assert section == "models"
            models_kv[field] = value
        merged = dict(holder["settings"].models.__dict__)
        merged.update(models_kv)
        holder["settings"] = _settings({"models": merged})
        return "written"

    models = Models(
        store, lambda: holder["settings"], transport=_transport(ep) if ep else None,
        config_writer=_fake_writer,
    )
    return store, holder, models


def _usage_rows(store: Store) -> list[dict]:
    rows = store.read().execute("SELECT * FROM usage ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _chat_payload(**over) -> dict:
    data = {"base_url": "https://api.test/v1", "main": "main-a", "main_backup": "", "worker": "w-a", "worker_backup": ""}
    data.update(over)
    return data


def _patch(**over) -> dict:
    data = _chat_payload(main="m1", main_backup="m2", worker="w1", worker_backup="w2")
    data.update(over)
    return data


# ----------------------------------------------------------------------
# 设置来源：网页优先、回落 config、save 后生效
# ----------------------------------------------------------------------


class TestSourcePriority:
    def test_config_only(self, tmp_path) -> None:
        cfg = {
            "models": {
                "base_url": "https://cfg.test/v1",
                "api_key": SECRET,
                "main": "cfg-main",
                "worker": "cfg-worker",
            }
        }
        store, holder, models = _make_models(tmp_path, cfg)
        s = models.settings()
        assert s.source == "config"
        assert s.base_url == "https://cfg.test/v1"
        assert s.main == "cfg-main" and s.worker == "cfg-worker"
        assert s.key_set is True
        assert s.ready() is True

    def test_none_when_nothing(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        s = models.settings()
        assert s.source == "none"
        assert s.key_set is False
        assert s.ready() is False
        assert s.public()["source"] == "none"

    def test_save_replaces_config(self, tmp_path) -> None:
        """网页保存 = 整组写进 config.toml（假 writer 直接并进 settings），source 恒 config。"""
        cfg = {
            "models": {
                "base_url": "https://cfg.test/v1",
                "api_key": SECRET,
                "main": "cfg-main",
                "worker": "cfg-worker",
            }
        }
        store, holder, models = _make_models(tmp_path, cfg)
        patch = _patch(base_url="https://web.test/v1", api_key=OTHER_SECRET)
        models.save(patch)
        s = models.settings()
        assert s.source == "config"
        assert s.base_url == "https://web.test/v1"
        assert s.main == "m1" and s.worker == "w1"
        assert s.key_set is True
        assert "cfg.test" not in json.dumps(s.public())

    def test_config_file_change_refreshes(self, tmp_path) -> None:
        """config.toml 被改（热更新换了 Settings 对象）→ settings() 跟着变。"""
        cfg = {
            "models": {
                "base_url": "https://cfg.test/v1",
                "api_key": SECRET,
                "main": "cfg-main",
                "worker": "cfg-worker",
            }
        }
        store, holder, models = _make_models(tmp_path, cfg)
        assert models.settings().base_url == "https://cfg.test/v1"
        holder["settings"] = _settings(
            {"models": {"base_url": "https://b.test/v1", "api_key": SECRET, "main": "m-b", "worker": "w-b"}}
        )
        assert models.settings().base_url == "https://b.test/v1"
        assert models.settings().source == "config"

    def test_config_hot_reload_refreshes_cache(self, tmp_path) -> None:
        cfg1 = {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m-a", "worker": "w-a"}}
        store, holder, models = _make_models(tmp_path, cfg1)
        s1 = models.settings()
        assert s1.base_url == "https://a.test/v1"
        holder["settings"] = _settings(
            {"models": {"base_url": "https://b.test/v1", "api_key": SECRET, "main": "m-b", "worker": "w-b"}}
        )
        s2 = models.settings()
        assert s2.base_url == "https://b.test/v1"

    @pytest.mark.asyncio
    async def test_chat_uses_saved_settings(self, tmp_path) -> None:
        ep = FakeEndpoint({"web-main": [{"kind": "ok", "content": "ok-web"}]})
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://cfg.test/v1", "api_key": SECRET, "main": "cfg-main", "worker": "cfg-worker"}},
            ep,
        )
        models.save(_patch(base_url="https://web.test/v1", main="web-main", worker="web-worker", api_key=OTHER_SECRET))
        r = await models.chat("main", [{"role": "user", "content": "hi"}])
        assert ep.calls[0]["model"] == "web-main"
        assert r.text == "ok-web"
        assert r.model == "web-main"
        await models.close()


# ----------------------------------------------------------------------
# 密钥只进不出
# ----------------------------------------------------------------------


class TestSecretPrivacy:
    def test_public_never_contains_key(self, tmp_path) -> None:
        cfg = {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m", "worker": "w"}}
        store, holder, models = _make_models(tmp_path, cfg)
        assert SECRET not in json.dumps(models.settings().public(), ensure_ascii=False)
        models.save(_patch(api_key=OTHER_SECRET))
        dumped = json.dumps(models.settings().public(), ensure_ascii=False)
        assert SECRET not in dumped and OTHER_SECRET not in dumped

    def test_save_without_key_keeps_old(self, tmp_path) -> None:
        """api_key 空 / 不传 = 不改密钥（写进 config.toml 的那把还在）。"""
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(api_key=SECRET))
        assert holder["settings"].models.api_key == SECRET
        models.save(_patch(main="m-changed", api_key=""))
        assert holder["settings"].models.api_key == SECRET
        models.save(_patch(main="m-changed2"))  # 整个字段都不传
        assert holder["settings"].models.api_key == SECRET
        assert models.settings().main == "m-changed2"

    def test_save_with_none_key_keeps_old(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(api_key=SECRET))
        models.save(_patch(main="m3", api_key=None))
        assert holder["settings"].models.api_key == SECRET

    @pytest.mark.asyncio
    async def test_error_message_redacts_secret(self, tmp_path) -> None:
        body = {"error": {"message": f"鉴权失败 key={SECRET} Bearer {SECRET}"}}
        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 500, "body": body}]})
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w", "retries": 0}},
            ep,
        )
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert SECRET not in ei.value.message
        assert ei.value.status == 500
        rows = _usage_rows(store)
        assert len(rows) == 1 and rows[0]["ok"] == 0
        assert SECRET not in rows[0]["error"]
        await models.close()

    @pytest.mark.asyncio
    async def test_saved_event_has_no_secret(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(api_key=SECRET))
        row = store.read().execute(
            "SELECT payload FROM events WHERE kind='models.saved' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert SECRET not in row["payload"]

    def test_redact_function_itself(self) -> None:
        from CharTyr_MaiWork.models import _redact

        out = _redact(f"key=abc 再去 Bearer {BEARER_SECRET} 和 sk-realkey999", ["abc"])
        for bad in ("abc", BEARER_SECRET, "sk-realkey999"):
            assert bad not in out
        assert "sk-***" in out and "Bearer ***" in out
        long = _redact("x" * 500, [])
        assert len(long) == 300


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


class TestValidation:
    def test_bad_base_url(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        with pytest.raises(ValueError):
            models.save(_patch(base_url="ftp://x.test/v1", api_key=SECRET))
        with pytest.raises(ValueError):
            models.save(_patch(base_url="", api_key=SECRET))

    def test_missing_main_or_worker(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        with pytest.raises(ValueError):
            models.save(_patch(main="", api_key=SECRET))
        with pytest.raises(ValueError):
            models.save(_patch(worker="", api_key=SECRET))

    def test_backup_same_as_original(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        with pytest.raises(ValueError):
            models.save(_patch(main_backup="m1", api_key=SECRET))
        with pytest.raises(ValueError):
            models.save(_patch(worker_backup="w1", api_key=SECRET))


# ----------------------------------------------------------------------
# 主备回落
# ----------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_500_then_backup(self, tmp_path) -> None:
        ep = FakeEndpoint(
            {
                "m1": [{"kind": "status", "status": 500, "body": {"error": {"message": "内部错误"}}}],
                "m-bak": [{"kind": "ok", "content": "备用救场"}],
            }
        )
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "main_backup": "m-bak", "worker": "w", "retries": 0}},
            ep,
        )
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用救场"
        assert r.model == "m-bak"
        assert [c["model"] for c in ep.calls] == ["m1", "m-bak"]
        rows = _usage_rows(store)
        assert len(rows) == 2
        assert rows[0]["model"] == "m1" and rows[0]["ok"] == 0 and rows[0]["error"]
        assert rows[1]["model"] == "m-bak" and rows[1]["ok"] == 1
        await models.close()

    @pytest.mark.asyncio
    async def test_429_falls_back(self, tmp_path) -> None:
        ep = FakeEndpoint(
            {
                "m1": [{"kind": "status", "status": 429, "body": {"error": {"message": "太快了"}}}],
                "m-bak": [{"kind": "ok", "content": "过了"}],
            }
        )
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "main_backup": "m-bak", "worker": "w"}},
            ep,
        )
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "过了"
        await models.close()

    @pytest.mark.asyncio
    async def test_timeout_falls_back(self, tmp_path) -> None:
        ep = FakeEndpoint(
            {
                "m1": [{"kind": "timeout"}],
                "m-bak": [{"kind": "ok", "content": "超时后备用成功"}],
            }
        )
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "main_backup": "m-bak", "worker": "w", "retries": 0}},
            ep,
        )
        r = await models.chat("main", [{"role": "user", "content": "x"}], timeout=5)
        assert r.text == "超时后备用成功"
        rows = _usage_rows(store)
        # retries=0：超时失败后不原地重试，直接换备用
        assert [(x["model"], x["ok"]) for x in rows] == [("m1", 0), ("m-bak", 1)]
        await models.close()

    @pytest.mark.asyncio
    async def test_network_error_falls_back(self, tmp_path) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                body = json.loads(request.content or b"{}")
                if body.get("model") == "m1":
                    raise httpx.ConnectError("连不上", request=request)
                message = {"role": "assistant", "content": "网错后备用成功"}
                return httpx.Response(
                    200,
                    json={
                        "id": "x", "object": "chat.completion", "created": 1, "model": "m-bak",
                        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                    },
                )
            return httpx.Response(404)

        store = Store(tmp_path / "test.db")
        store.migrate()
        settings = _settings(
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "main_backup": "m-bak", "worker": "w"}}
        )
        models = Models(store, lambda: settings, transport=httpx.MockTransport(boom))
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "网错后备用成功"
        await models.close()

    @pytest.mark.asyncio
    async def test_network_error_retry_then_success(self, tmp_path) -> None:
        """线上实测（2026-09-27）：没配备用模型时，一次网络抖动就让整批资讯作废。
        网络错误同一个模型要重试；错误里要写清是哪种网络错误（原来是空的）。"""
        n = {"c": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            n["c"] += 1
            if n["c"] == 1:
                raise httpx.ReadTimeout("", request=request)
            message = {"role": "assistant", "content": "重试成功"}
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 1, "model": "w",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

        store = Store(tmp_path / "t.db"); store.migrate()
        settings = _settings({"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w"}})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(flaky))
        r = await models.chat("worker", [{"role": "user", "content": "x"}])
        assert r.text == "重试成功" and n["c"] == 2
        rows = _usage_rows(store)
        assert rows[0]["ok"] == 0 and "ReadTimeout" in rows[0]["error"]
        await models.close()

    @pytest.mark.asyncio
    async def test_network_error_twice_raises_with_type(self, tmp_path) -> None:
        def dead(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("", request=request)

        store = Store(tmp_path / "t.db"); store.migrate()
        settings = _settings({"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w", "retries": 0}})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(dead))
        with pytest.raises(ModelError) as ei:
            await models.chat("worker", [{"role": "user", "content": "x"}])
        assert "RemoteProtocolError" in ei.value.message
        assert len(_usage_rows(store)) == 1
        await models.close()

    @pytest.mark.asyncio
    async def test_4xx_no_fallback(self, tmp_path) -> None:
        for status in (400, 401, 403, 404):
            store = Store(tmp_path / f"t{status}.db")
            store.migrate()
            ep = FakeEndpoint(
                {"m1": [{"kind": "status", "status": status, "body": {"error": {"message": f"错误{status}"}}}]}
            )
            settings = _settings(
                {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "main_backup": "m-bak", "worker": "w"}}
            )
            models = Models(store, lambda: settings, transport=_transport(ep))
            with pytest.raises(ModelError) as ei:
                await models.chat("main", [{"role": "user", "content": "x"}])
            assert ei.value.status == status
            assert ep.chat_count == 1  # 没换备用
            await models.close()
            store.close()

    @pytest.mark.asyncio
    async def test_no_backup_raises(self, tmp_path) -> None:
        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 500, "body": {"error": {"message": "挂了"}}}]})
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w", "retries": 0}},
            ep,
        )
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert ei.value.status == 500
        rows = _usage_rows(store)
        assert len(rows) == 1
        await models.close()

    @pytest.mark.asyncio
    async def test_both_fail_raises_last(self, tmp_path) -> None:
        ep = FakeEndpoint(
            {
                "m1": [{"kind": "status", "status": 500, "body": {"error": {"message": "主挂"}}}],
                "m-bak": [{"kind": "status", "status": 503, "body": {"error": {"message": "备也挂"}}}],
            }
        )
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "main_backup": "m-bak", "worker": "w", "retries": 0}},
            ep,
        )
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert ei.value.status == 503  # 最后一个失败
        assert len(_usage_rows(store)) == 2
        await models.close()

    @pytest.mark.asyncio
    async def test_worker_role_uses_worker_models(self, tmp_path) -> None:
        ep = FakeEndpoint(
            {
                "w1": [{"kind": "status", "status": 500, "body": {"error": {"message": "干活模型挂"}}}],
                "w-bak": [{"kind": "ok", "content": "worker 备用成功"}],
            }
        )
        store, holder, models = _make_models(
            tmp_path,
            {
                "models": {
                    "base_url": "https://a.test/v1",
                    "api_key": SECRET,
                    "main": "m1",
                    "main_backup": "m-bak",
                    "worker": "w1",
                    "worker_backup": "w-bak",
                    "retries": 0,
                }
            },
            ep,
        )
        r = await models.chat("worker", [{"role": "user", "content": "x"}])
        assert [c["model"] for c in ep.calls] == ["w1", "w-bak"]
        assert r.text == "worker 备用成功"
        rows = _usage_rows(store)
        assert all(row["role"] == "worker" for row in rows)
        await models.close()


# ----------------------------------------------------------------------
# 请求细节
# ----------------------------------------------------------------------


class TestRequestDetails:
    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_and_auth_header(self, tmp_path) -> None:
        ep = FakeEndpoint({"m1": [{"kind": "ok"}]})
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1/", "api_key": SECRET, "main": "m1", "worker": "w"}},
            ep,
        )
        await models.chat("main", [{"role": "user", "content": "x"}])
        req_path = ep.calls[0]
        assert req_path["auth"] == f"Bearer {SECRET}"
        # 路径没有重复斜杠导致的问题——MockTransport 看不到 URL，
        # 但 handler 能匹配到 /chat/completions 就说明拼对了
        await models.close()

    @pytest.mark.asyncio
    async def test_json_mode_and_tools_and_usage(self, tmp_path) -> None:
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_cmd", "arguments": '{"cmd": "ls"}'},
        }
        ep = FakeEndpoint(
            {"m1": [{"kind": "ok", "content": None, "tool": tool_call, "usage": {"prompt_tokens": 21, "completion_tokens": 5}}]}
        )
        store, holder, models = _make_models(
            tmp_path,
            {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w"}},
            ep,
        )
        tools = [{"type": "function", "function": {"name": "run_cmd", "parameters": {"type": "object"}}}]
        r = await models.chat(
            "main", [{"role": "user", "content": "跑个命令"}], tools=tools, json_mode=True, purpose="测试用途"
        )
        body = ep.calls[0]["body"]
        assert body["response_format"] == {"type": "json_object"}
        assert body["tools"] == tools
        assert r.text == ""
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0]["function"]["name"] == "run_cmd"
        assert r.prompt_tokens == 21 and r.completion_tokens == 5
        assert isinstance(r.raw_message, dict)
        rows = _usage_rows(store)
        assert rows[0]["purpose"] == "测试用途"
        assert rows[0]["prompt_tokens"] == 21 and rows[0]["completion_tokens"] == 5
        await models.close()

    @pytest.mark.asyncio
    async def test_missing_usage_defaults_zero(self, tmp_path) -> None:
        def no_usage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion", "created": 1, "model": "m1",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
                },
            )

        store = Store(tmp_path / "test.db")
        store.migrate()
        settings = _settings({"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w"}})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(no_usage))
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.prompt_tokens == 0 and r.completion_tokens == 0
        await models.close()


# ----------------------------------------------------------------------
# 未配好
# ----------------------------------------------------------------------


class TestNotReady:
    @pytest.mark.asyncio
    async def test_chat_raises_without_request(self, tmp_path) -> None:
        ep = FakeEndpoint({})
        store, holder, models = _make_models(tmp_path, {}, ep)
        with pytest.raises(ModelError, match="模型还没配好"):
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert ep.chat_count == 0
        assert _usage_rows(store) == []

    @pytest.mark.asyncio
    async def test_missing_key_not_ready(self, tmp_path) -> None:
        cfg = {"models": {"base_url": "https://a.test/v1", "main": "m1", "worker": "w"}}
        store, holder, models = _make_models(tmp_path, cfg)
        assert models.settings().ready() is False
        with pytest.raises(ModelError, match="模型还没配好"):
            await models.chat("worker", [{"role": "user", "content": "x"}])


# ----------------------------------------------------------------------
# list_models / 测试连接
# ----------------------------------------------------------------------


class TestListModels:
    @pytest.mark.asyncio
    async def test_parse_data_ids(self, tmp_path) -> None:
        ep = FakeEndpoint({}, models=["gpt-x", "claude-y"])
        store = Store(tmp_path / "test.db")
        store.migrate()
        settings = _settings({})
        models = Models(store, lambda: settings, transport=_transport(ep))
        out = await models.list_models("https://api.test/v1/", api_key=SECRET)
        assert out == ["gpt-x", "claude-y"]
        await models.close()

    @pytest.mark.asyncio
    async def test_uses_config_secret_when_key_empty(self, tmp_path) -> None:
        """list_models 不给密钥时用 config.toml 里的 [models] api_key。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json={"object": "list", "data": [{"id": "a"}]})

        store, holder, models = _make_models(
            tmp_path, {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m", "worker": "w"}}, None
        )
        # 关掉懒创建的默认 client，换成本测试的假传输
        models._transport = httpx.MockTransport(handler)
        out = await models.list_models("https://api.test/v1")
        assert out == ["a"]
        assert seen == [f"Bearer {SECRET}"]
        await models.close()

    @pytest.mark.asyncio
    async def test_falls_back_to_config_key(self, tmp_path) -> None:
        ep = FakeEndpoint({}, models=["a"])
        cfg = {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m", "worker": "w"}}
        store, holder, models = _make_models(tmp_path, cfg, ep)
        out = await models.list_models("https://api.test/v1")
        assert out == ["a"]
        await models.close()

    @pytest.mark.asyncio
    async def test_no_key_anywhere_raises(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        with pytest.raises(ModelError):
            await models.list_models("https://api.test/v1")

    @pytest.mark.asyncio
    async def test_success_does_not_save(self, tmp_path) -> None:
        ep = FakeEndpoint({}, models=["a", "b"])
        store, holder, models = _make_models(tmp_path, {}, ep)
        out = await models.list_models("https://api.test/v1", api_key=SECRET)
        assert out == ["a", "b"]
        assert store.kv_get("models.checked") is None  # 没自动保存（连回写都没有）
        assert holder["settings"].models.api_key == ""  # config 也没被动过
        await models.close()


# ----------------------------------------------------------------------
# 用量统计
# ----------------------------------------------------------------------


class TestUsage:
    @pytest.mark.asyncio
    async def test_usage_today(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(clock, "now", lambda: 1_790_000_000.0)
        day = clock.day_key(1_790_000_000.0)
        ep = FakeEndpoint(
            {
                "m1": [
                    {"kind": "ok", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
                    {"kind": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                ],
                "w1": [{"kind": "status", "status": 500, "body": {"error": {"message": "挂"}}}],
            }
        )
        cfg = {
            "models": {
                "base_url": "https://a.test/v1",
                "api_key": SECRET,
                "main": "m1",
                "worker": "w1",
                "retries": 0,
            }
        }
        store, holder, models = _make_models(tmp_path, cfg, ep)
        await models.chat("main", [{"role": "user", "content": "1"}], group_id="900000001", task_id="T-1")
        await models.chat("main", [{"role": "user", "content": "2"}])
        with pytest.raises(ModelError):
            await models.chat("worker", [{"role": "user", "content": "3"}])
        u = models.usage_today()
        assert u["main"] == 17  # 15 + 2
        assert u["worker"] == 0
        assert u["calls"] == 3
        assert u["errors"] == 1
        rows = _usage_rows(store)
        assert rows[0]["group_id"] == "900000001" and rows[0]["task_id"] == "T-1"
        assert rows[0]["day"] == day
        await models.close()

    @pytest.mark.asyncio
    async def test_usage_fields(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(clock, "now", lambda: 1_790_000_000.0)
        ep = FakeEndpoint({"m1": [{"kind": "ok"}]})
        cfg = {"models": {"base_url": "https://a.test/v1", "api_key": SECRET, "main": "m1", "worker": "w"}}
        store, holder, models = _make_models(tmp_path, cfg, ep)
        await models.chat("main", [{"role": "user", "content": "x"}], purpose="测字段", group_id="1", task_id="T-9")
        row = _usage_rows(store)[0]
        assert row["ts"] == 1_790_000_000.0
        assert row["day"] == clock.day_key(1_790_000_000.0)
        assert row["role"] == "main"
        assert row["model"] == "m1"
        assert row["purpose"] == "测字段"
        assert row["group_id"] == "1" and row["task_id"] == "T-9"
        assert row["ok"] == 1
        assert row["ms"] >= 0
        assert row["error"] == ""
        await models.close()


# ----------------------------------------------------------------------
# save 细节：保留 checked_at / available
# ----------------------------------------------------------------------


class TestSaveDetails:
    def test_checked_at_and_available_preserved_or_set(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(clock, "now", lambda: 1_790_000_000.0)
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(api_key=SECRET, available=["m1", "m2", "w1"], checked_at=1_789_999_000.0))
        s = models.settings()
        assert s.available == ["m1", "m2", "w1"]
        assert s.checked_at == 1_789_999_000.0
        # 再保存不带 available：保留旧值
        monkeypatch.setattr(clock, "now", lambda: 1_790_000_100.0)
        models.save(_patch(api_key=""))
        s2 = models.settings()
        assert s2.available == ["m1", "m2", "w1"]
        assert s2.checked_at == 1_789_999_000.0

    def test_base_url_trailing_slash_stripped_on_save(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(base_url="https://x.test/v1/", api_key=SECRET))
        assert models.settings().base_url == "https://x.test/v1"

    def test_save_writes_checked_kv_and_event_in_one_tx(self, tmp_path) -> None:
        """保存：config 键交给 writer（假钩子并进 settings），kv 只放 checked_at/available，
        密钥绝不进 kv / 事件。"""
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(api_key=SECRET))
        kv = store.kv_get("models.checked")
        assert kv is not None and kv["base_url"] == "https://api.test/v1"
        assert "api_key" not in kv
        # 密钥写进了配置（假 writer 并进 settings 的 [models] api_key）
        assert holder["settings"].models.api_key == SECRET
        ev = store.read().execute("SELECT * FROM events WHERE kind='models.saved'").fetchall()
        assert len(ev) == 1
        assert SECRET not in ev[0]["payload"]

    def test_public_shape(self, tmp_path) -> None:
        store, holder, models = _make_models(tmp_path)
        models.save(_patch(api_key=SECRET))
        pub = models.settings().public()
        for key in ("base_url", "key_set", "main", "main_backup", "worker", "worker_backup", "source", "checked_at", "available"):
            assert key in pub
        assert pub["key_set"] is True
        assert "api_key" not in pub
