"""模型重试规则（2026-10 用户要求：默认 5 次重试，每次间隔 10 秒）。

可重试的错误 = 网络错误（httpx.HTTPError）、HTTP 5xx、429、408；
同一个模型最多「1 + retries」次；非 429 的两次之间等 retry_delay_s 秒；
429 不再用固定 delay，改走端点级冷却（细测见 tests/test_model_throttle.py）：
Retry-After 秒数 / HTTP 日期封顶 120 秒，没有就按连续 429 次数 10/20/40/60 秒
退避（封顶 60、±20% 抖动）；其他 4xx 不重试、不换备用，直接抛。
测试里 conftest 把 models._SLEEP 换成立即返回的假函数，不等真秒数。
"""

from __future__ import annotations

import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import ModelError, Models
from CharTyr_MaiWork.maiwork.store import Store

from test_models import SECRET, FakeEndpoint, _chat_payload, _patch, _settings, _transport, _usage_rows


def _make(tmp_path, cfg: dict, ep: FakeEndpoint):
    """config_writer 假钩子：把 save 要写的键并进 settings（模拟「写 config.toml → 热更新」）。"""
    store = Store(tmp_path / "test.db")
    store.migrate()
    holder = {"settings": _settings(cfg)}

    def _fake_writer(flat: dict):
        merged = dict(holder["settings"].models.__dict__)
        for full_key, value in flat.items():
            section, _, field = full_key.partition(".")
            merged[field] = value
        holder["settings"] = _settings({"models": merged})
        return "written"

    models = Models(store, lambda: holder["settings"], transport=_transport(ep), config_writer=_fake_writer)
    return store, models


# ----------------------------------------------------------------------
# 设置：retries / retry_delay_s 默认值、来源优先级、save 校验
# ----------------------------------------------------------------------


class TestRetrySettings:
    def test_defaults(self, tmp_path) -> None:
        cfg = {"models": _chat_payload(api_key=SECRET)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        s = models.settings()
        assert s.retries == 5
        assert s.retry_delay_s == 10

    @pytest.mark.parametrize("bad", [-1, 11, 0, -3])
    def test_config_out_of_range_clamped(self, tmp_path, bad) -> None:
        """config.toml 里写越界：load_settings 夹回边（重了也不让插件挂）。"""
        cfg = {"models": _chat_payload(api_key=SECRET, retries=bad)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        assert 0 <= models.settings().retries <= 10

    def test_config_values(self, tmp_path) -> None:
        cfg = {"models": _chat_payload(api_key=SECRET, retries=2, retry_delay_s=3)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        s = models.settings()
        assert s.retries == 2
        assert s.retry_delay_s == 3

    def test_public_contains_retry_fields(self, tmp_path) -> None:
        cfg = {"models": _chat_payload(api_key=SECRET, retries=7, retry_delay_s=22)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        pub = models.settings().public()
        assert pub["retries"] == 7
        assert pub["retry_delay_s"] == 22

    def test_save_accepts_retry_fields(self, tmp_path) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        models.save(_patch(api_key=SECRET, retries=0, retry_delay_s=60))
        s = models.settings()
        assert s.retries == 0
        assert s.retry_delay_s == 60
        # 写进 config（假 writer 并进了 settings）
        assert s.source == "config"

    @pytest.mark.parametrize("bad", [-1, 11, "x", 1.5])
    def test_save_retries_invalid(self, tmp_path, bad) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        with pytest.raises(ValueError, match="重试次数"):
            models.save(_patch(api_key=SECRET, retries=bad))

    @pytest.mark.parametrize("bad", [0, 61, "x", 1.5])
    def test_save_retry_delay_invalid(self, tmp_path, bad) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        with pytest.raises(ValueError, match="重试间隔"):
            models.save(_patch(api_key=SECRET, retry_delay_s=bad))

    @pytest.mark.parametrize("good", [0, 5, 10])
    def test_save_retries_valid(self, tmp_path, good) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        models.save(_patch(api_key=SECRET, retries=good))
        assert models.settings().retries == good

    def test_save_replaces_config_retry_values(self, tmp_path) -> None:
        cfg = {"models": _chat_payload(api_key=SECRET, retries=1, retry_delay_s=2)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        models.save(_patch(api_key=SECRET, retries=8, retry_delay_s=9))
        s = models.settings()
        assert s.source == "config"
        assert s.retries == 8 and s.retry_delay_s == 9

    def test_save_without_retry_fields_keeps_current(self, tmp_path) -> None:
        """网页保存时没带 retries/retry_delay_s：保留当前有效值（不顶掉 config 里的）。"""
        cfg = {"models": _chat_payload(api_key=SECRET, retries=4, retry_delay_s=44)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        models.save(_patch(api_key=SECRET))
        s = models.settings()
        assert s.retries == 4 and s.retry_delay_s == 44


# ----------------------------------------------------------------------
# 重试行为
# ----------------------------------------------------------------------


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_network_error_retries_delay_then_success(self, tmp_path, monkeypatch) -> None:
        """网络错误：同一模型最多 1+retries 次，两次之间等 retry_delay_s。

        FakeEndpoint 的 timeout 是「耗尽之前按顺序超一次，之后才用下一个行为」，
        但超时本身不落 calls → 行为列表的计数索引永远停在 0 → 后面也都超时。
        所以超时一次后要成功，要用自定义 handler（不能依赖 FakeEndpoint 的 calls 索引）。
        """
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        n = {"c": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n["c"] += 1
            if n["c"] == 1:
                raise httpx.ReadTimeout("读取超时", request=request)
            return httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion", "created": 1, "model": "m1",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "重来好了"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings({"models": _chat_payload(api_key=SECRET, main="m1", retries=2, retry_delay_s=7)})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(handler))
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "重来好了"
        assert n["c"] == 2, "超时一次 + 重试一次成功"
        assert slept == [7], "网络错误重试前等 retry_delay_s 秒"
        await models.close()

    @pytest.mark.asyncio
    async def test_network_error_exhausts_then_raise(self, tmp_path) -> None:
        """retries=1 时同一模型最多 2 次，用完抛；usage 每次尝试一行。"""
        ep = FakeEndpoint({"w": [{"kind": "timeout"}]})  # 枯竭后一直走最后一个=超时
        cfg = {"models": _chat_payload(api_key=SECRET, worker="w", retries=1)}
        store, models = _make(tmp_path, cfg, ep)
        with pytest.raises(ModelError):
            await models.chat("worker", [{"role": "user", "content": "x"}])
        assert ep.chat_count == 0, "两次都是超时（未落 calls）"
        rows = _usage_rows(store)
        assert len(rows) == 2 and all(r["ok"] == 0 for r in rows)
        mc = store.read().execute("SELECT attempt, ok, status FROM model_calls ORDER BY id").fetchall()
        assert [(r["attempt"], r["ok"], r["status"]) for r in mc] == [(1, 0, 0), (2, 0, 0)]
        await models.close()

    @pytest.mark.asyncio
    async def test_5xx_retries_same_model(self, tmp_path, monkeypatch) -> None:
        """5xx 也是可重试错误：同一模型重试，不立刻换备用。"""
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        ep = FakeEndpoint(
            {
                "m1": [
                    {"kind": "status", "status": 500, "body": {"error": {"message": "挂"}}},
                    {"kind": "ok", "content": "缓过来了"},
                ],
                "m-bak": [{"kind": "ok", "content": "不该轮到备用"}],
            }
        )
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=2)}
        store, models = _make(tmp_path, cfg, ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "缓过来了"
        assert [c["model"] for c in ep.calls] == ["m1", "m1"]
        await models.close()

    @pytest.mark.asyncio
    async def test_429_honors_retry_after(self, tmp_path, monkeypatch) -> None:
        """429 带 Retry-After=30：整个端点冷却 30 秒后才重试（不再用 retry_delay_s=5）。"""
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            n = sum(1 for c in calls if c == body.get("model"))
            calls.append(body.get("model", ""))
            if n == 0:
                return httpx.Response(429, json={"error": {"message": "太快"}}, headers={"Retry-After": "30"})
            return httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion", "created": 1, "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "过了"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        calls: list[str] = []
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings({"models": _chat_payload(api_key=SECRET, main="m1", retries=1, retry_delay_s=5)})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(handler))
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "过了"
        assert len(slept) == 1 and 29.9 < slept[0] <= 30, "429 带 Retry-After=30 要等 30 秒（大过 retry_delay_s）"
        await models.close()

    @pytest.mark.asyncio
    async def test_429_retry_after_capped_120(self, tmp_path, monkeypatch) -> None:
        """Retry-After 再大也最多 120 秒（防端点胡来把请求卡死）。"""
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "limit"}}, headers={"Retry-After": "3600"})

        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings({"models": _chat_payload(api_key=SECRET, main="m1", retries=1, retry_delay_s=10)})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(handler))
        with pytest.raises(ModelError):
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert len(slept) == 1 and 119.9 < slept[0] <= 120
        await models.close()

    @pytest.mark.asyncio
    async def test_429_without_header_uses_endpoint_backoff(self, tmp_path, monkeypatch) -> None:
        """429 没有 Retry-After：按端点退避 10 秒（±20% 抖动），不再用 retry_delay_s=8。"""
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 429, "body": {"error": {"message": "limit"}}}]})
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", retries=1, retry_delay_s=8)}
        store, models = _make(tmp_path, cfg, ep)
        with pytest.raises(ModelError):
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert len(slept) == 1 and 8 <= slept[0] <= 12, "无 Retry-After：端点退避 10 秒 ±20% 抖动"
        assert ep.chat_count == 2  # 1 + retries（重试总次数不变）
        await models.close()

    @pytest.mark.asyncio
    async def test_408_retryable(self, tmp_path) -> None:
        """408 是可重试错误（其他 4xx 不是）。"""
        ep = FakeEndpoint(
            {
                "m1": [
                    {"kind": "status", "status": 408, "body": {"error": {"message": "超时"}}},
                    {"kind": "ok", "content": "缓过来"},
                ]
            }
        )
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", retries=1)}
        store, models = _make(tmp_path, cfg, ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "缓过来"
        assert ep.chat_count == 2
        await models.close()

    @pytest.mark.asyncio
    async def test_400_not_retried_no_fallback(self, tmp_path) -> None:
        """400 等其他 4xx：不重试、不换备用，直接抛。"""
        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 400, "body": {"error": {"message": "请求坏"}}}]})
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=5)}
        store, models = _make(tmp_path, cfg, ep)
        with pytest.raises(ModelError) as ei:
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert ei.value.status == 400
        assert ep.chat_count == 1
        assert [c["model"] for c in ep.calls] == ["m1"]
        rows = _usage_rows(store)
        assert len(rows) == 1
        await models.close()

    @pytest.mark.asyncio
    async def test_exhausted_falls_back_same_rules(self, tmp_path) -> None:
        """主模型 1+retries 次用完 → 换备用模型，备用同样 1+retries 次。"""
        ep = FakeEndpoint(
            {
                "m1": [{"kind": "status", "status": 500, "body": {"error": {"message": "挂"}}}],
                "m-bak": [{"kind": "status", "status": 500, "body": {"error": {"message": "也挂"}}}],
            }
        )
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=1)}
        store, models = _make(tmp_path, cfg, ep)
        with pytest.raises(ModelError):
            await models.chat("main", [{"role": "user", "content": "x"}])
        assert [c["model"] for c in ep.calls] == ["m1", "m1", "m-bak", "m-bak"]
        await models.close()

    @pytest.mark.asyncio
    async def test_switches_backup_immediately_outside_backoff(self, tmp_path, monkeypatch) -> None:
        """换备用模型之间没有重试等待（等待只发生在同一模型的两次尝试之间）。"""
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        ep = FakeEndpoint(
            {
                "m1": [{"kind": "status", "status": 500, "body": {"error": {"message": "挂"}}}],
                "m-bak": [{"kind": "ok", "content": "备用"}],
            }
        )
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=2)}
        store, models = _make(tmp_path, cfg, ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用"
        assert [c["model"] for c in ep.calls] == ["m1", "m1", "m1", "m-bak"]
        assert slept == [10, 10]  # 同模型两次重试等待（默认 retry_delay_s=10）；换备用不等
        await models.close()

    @pytest.mark.asyncio
    async def test_retries_param_overrides_settings(self, tmp_path, monkeypatch) -> None:
        """chat(retries=1)：后台主循环里直接 await 的调用不能默认 5 次把循环卡住。"""
        slept: list[float] = []
        import CharTyr_MaiWork.maiwork.models as mm

        async def _rec(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _rec)

        ep = FakeEndpoint({"m1": [{"kind": "timeout"}]})
        cfg = {"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=5, retry_delay_s=6)}
        store, models = _make(tmp_path, cfg, ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}], retries=1)
        assert r.model == "m-bak"
        assert slept == [6], "retries=1：只主模型这两次尝试之间等一次（换备用不等）"
        rows = store.read().execute("SELECT model, ok, attempt FROM model_calls ORDER BY id").fetchall()
        assert [(r["model"], r["ok"], r["attempt"]) for r in rows] == [
            ("m1", 0, 1), ("m1", 0, 2), ("m-bak", 1, 3),
        ], "retries=1：主 2 次失败 + 备 1 次成功（attempt 递增）"
        await models.close()

    @pytest.mark.asyncio
    async def test_retries_zero_means_try_once_each(self, tmp_path) -> None:
        n = {"c": 0}

        def one_shot(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            n["c"] += 1
            if body.get("model") == "m1":
                raise httpx.ConnectError("连不上", request=request)
            return httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion", "created": 1, "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "备用一次过"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings({"models": _chat_payload(api_key=SECRET, main="m1", main_backup="m-bak", retries=0)})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(one_shot))
        r = await models.chat("main", [{"role": "user", "content": "x"}])
        assert r.text == "备用一次过"
        assert n["c"] == 2, "retries=0：主备各只试 1 次（m1 连不上 1 次 + m-bak 1 次）"
        await models.close()

    @pytest.mark.asyncio
    async def test_default_retries_is_five(self, tmp_path) -> None:
        """没说 retries 时按设置默认 5：模型最多 6 次尝试。"""
        ep = FakeEndpoint({"w": [{"kind": "status", "status": 500, "body": {"error": {"message": "挂"}}}]})
        cfg = {"models": _chat_payload(api_key=SECRET, worker="w")}
        store, models = _make(tmp_path, cfg, ep)
        with pytest.raises(ModelError):
            await models.chat("worker", [{"role": "user", "content": "x"}])
        assert ep.chat_count == 6
        rows = _usage_rows(store)
        assert len(rows) == 6 and all(r["ok"] == 0 for r in rows)
        await models.close()
