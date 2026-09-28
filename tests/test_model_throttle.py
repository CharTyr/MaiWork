"""端点级限流（models.py 内部）：并发上限、429 冷却、每分钟上限。

线上实测（2026-10，step-5-preview + api.stepfun.ai）：09:00–09:12 之间 46 次调用里
22 次 429，而请求速率只有每分钟 3~6 次。原来的重试拿到 429 后很快又打过去，越打越被限。
新行为（全部在 models.py 内部，状态只在进程内存、不落库）：

- 同一 base_url（规范化：去空格、小写、去末尾斜杠）同时最多 max_concurrency 个在途请求
  （设置 [models] max_concurrency，缺省 2）；超出的排队等，不报错。
- 任何一次 429 → 整个端点进冷却：有 Retry-After（秒数或 HTTP 日期）就用它（封顶 120 秒）；
  没有就按连续 429 次数退避 10/20/40/60 秒（封顶 60）+ ±20% 抖动。
  冷却期间同端点的新请求和重试都先等到冷却结束；成功一次后连续 429 计数清零。
- chat() 的 429 重试不再用固定 retry_delay_s，改走端点冷却；5xx / 网络错误维持原逻辑。
- 可选 max_rpm > 0 时按端点做 60 秒滑动窗口限速（缺省 0 = 关）。

测试用假时钟 + 假 sleep（FakeClock）按秒推进，不等真时间。
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import CharTyr_MaiWork.maiwork.models as mm
from CharTyr_MaiWork.maiwork.models import ModelError, Models
from CharTyr_MaiWork.maiwork.store import Store

from test_models import SECRET, _chat_payload, _settings

# ----------------------------------------------------------------------
# 假时钟 / 假 sleep：等待可断言，且立刻推进时间
# ----------------------------------------------------------------------


class FakeClock:
    """假时钟：sleep 只记录秒数并推进自己的时间，不等真秒数。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
        self.slept: list[float] = []
        self.on_sleep: Any = None  # 每次 sleep 前回调（记「睡的时候发了几个请求」）

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """把 models.py 限流用的时钟和 sleep 都换成假的（conftest 的零 sleep 会被覆盖）。"""
    fake = FakeClock()
    monkeypatch.setattr(mm, "_NOW", fake.now)
    monkeypatch.setattr(mm, "_SLEEP", fake.sleep)
    return fake


# ----------------------------------------------------------------------
# 假设置 / 假端点
# ----------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "base_url": "https://api.test/v1",
    "api_key": SECRET,
    "main": "m1",
    "main_backup": "",
    "worker": "w1",
    "worker_backup": "",
    "retries": 0,
    "retry_delay_s": 10,
}


def _settings_ns(**over: Any) -> SimpleNamespace:
    """假 Settings：models 上可以带 max_concurrency / max_rpm（config.py 现在还没有这两个字段）。

    用 SimpleNamespace 直接给属性，等价于「config 之后加上字段」的情形；
    真配置（没有这两个字段）走 getattr 缺省，另有测试覆盖。
    """
    fields = dict(_DEFAULTS)
    fields.update(over)
    return SimpleNamespace(models=SimpleNamespace(**fields))


def _make(tmp_path, models_fields: dict | None = None, handler=None):
    store = Store(tmp_path / "test.db")
    store.migrate()
    holder = {"settings": _settings_ns(**(models_fields or {}))}
    models = Models(store, lambda: holder["settings"], transport=httpx.MockTransport(handler))
    return store, holder, models


def _ok_response(model: str, content: str = "好") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def _rate_limited(retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, json={"error": {"message": "请求太快"}}, headers=headers)


async def _chat(models: Models, text: str = "x"):
    return await models.chat("main", [{"role": "user", "content": text}])


# ----------------------------------------------------------------------
# 并发上限
# ----------------------------------------------------------------------


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_limit_one_serializes(self, tmp_path) -> None:
        """max_concurrency=1：同一时刻只有 1 个在途；其它排队，不报错。"""
        entered: asyncio.Queue = asyncio.Queue()
        release = asyncio.Event()
        inflight = {"n": 0, "max": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            entered.put_nowait(1)
            await release.wait()
            inflight["n"] -= 1
            return _ok_response("m1")

        store, holder, models = _make(tmp_path, {"max_concurrency": 1}, handler)
        tasks = [asyncio.create_task(_chat(models, str(i))) for i in range(3)]
        await asyncio.wait_for(entered.get(), 2)
        for _ in range(3):
            await asyncio.sleep(0)
        assert inflight["n"] == 1, "并发上限 1：同一时刻只有一个请求在途"
        assert entered.qsize() == 0, "第 2 个请求要排队等名额，不能同时进端点"
        release.set()
        results = await asyncio.gather(*tasks)
        assert [r.text for r in results] == ["好", "好", "好"]
        assert inflight["max"] == 1
        await models.close()

    @pytest.mark.asyncio
    async def test_default_limit_is_two(self, tmp_path) -> None:
        """真配置里没有 max_concurrency 字段：缺省并发上限 2。"""
        entered: asyncio.Queue = asyncio.Queue()
        release = asyncio.Event()
        inflight = {"n": 0, "max": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            entered.put_nowait(1)
            await release.wait()
            inflight["n"] -= 1
            return _ok_response("m1")

        store = Store(tmp_path / "test.db")
        store.migrate()
        settings = _settings({"models": _chat_payload(api_key=SECRET, main="m1", worker="w1")})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(handler))
        tasks = [asyncio.create_task(_chat(models, str(i))) for i in range(4)]
        await asyncio.wait_for(entered.get(), 2)
        await asyncio.wait_for(entered.get(), 2)
        for _ in range(3):
            await asyncio.sleep(0)
        assert inflight["n"] == 2, "缺省并发上限 2：两个在途、其它排队"
        assert entered.qsize() == 0
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(r.text == "好" for r in results)
        assert inflight["max"] == 2
        await models.close()


# ----------------------------------------------------------------------
# 429 冷却
# ----------------------------------------------------------------------


class TestCooldownAfter429:
    @pytest.mark.asyncio
    async def test_retry_after_seconds_delays_next_request(self, tmp_path, fake_time) -> None:
        """Retry-After: 30 → 同端点下一个请求等满 30 秒再发（等完才发，不是发了再等）。"""
        calls: list[str] = []
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content or b"{}").get("model", ""))
            state["n"] += 1
            return _rate_limited("30") if state["n"] == 1 else _ok_response("m1", "过了")

        store, holder, models = _make(tmp_path, None, handler)
        with pytest.raises(ModelError) as ei:
            await _chat(models, "1")
        assert ei.value.status == 429
        assert fake_time.slept == [], "第一次 429 之前没有冷却，不该等"

        seen_at_sleep: list[int] = []
        fake_time.on_sleep = lambda _s: seen_at_sleep.append(len(calls))
        r = await _chat(models, "2")
        assert r.text == "过了"
        assert fake_time.slept == [30], "同端点新请求要等满 Retry-After 30 秒"
        assert seen_at_sleep == [1], "等完冷却才发第二个请求（不是先发再等）"
        assert len(calls) == 2
        await models.close()

    @pytest.mark.asyncio
    async def test_retry_after_http_date(self, tmp_path, fake_time) -> None:
        """Retry-After 是 HTTP 日期：按「还差几秒」算冷却。"""
        when = email.utils.formatdate(fake_time.t + 45, usegmt=True)

        def handler(request: httpx.Request) -> httpx.Response:
            return _rate_limited(when)

        store, holder, models = _make(tmp_path, None, handler)
        with pytest.raises(ModelError):
            await _chat(models, "1")
        with pytest.raises(ModelError):
            await _chat(models, "2")
        assert fake_time.slept == [45], "HTTP 日期形式的 Retry-After 要换算成秒数"
        await models.close()

    @pytest.mark.asyncio
    async def test_retry_after_capped_120(self, tmp_path, fake_time) -> None:
        """Retry-After 再大也封顶 120 秒（防端点胡来把请求卡死）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _rate_limited("3600")

        store, holder, models = _make(tmp_path, None, handler)
        with pytest.raises(ModelError):
            await _chat(models, "1")
        with pytest.raises(ModelError):
            await _chat(models, "2")
        assert fake_time.slept == [120]
        await models.close()

    @pytest.mark.asyncio
    async def test_backoff_sequence_without_retry_after(self, tmp_path, fake_time, monkeypatch) -> None:
        """没有 Retry-After：连续 429 按 10/20/40/60 秒退避，封顶 60。"""
        monkeypatch.setattr(mm, "_jitter", lambda s: s)  # 去掉抖动看纯序列

        def handler(request: httpx.Request) -> httpx.Response:
            return _rate_limited()

        store, holder, models = _make(tmp_path, None, handler)
        for i in range(6):
            with pytest.raises(ModelError):
                await _chat(models, str(i))
        assert fake_time.slept == [10, 20, 40, 60, 60], "连续 429 退避 10/20/40/60 封顶 60"
        await models.close()

    @pytest.mark.asyncio
    async def test_success_resets_429_streak(self, tmp_path, fake_time, monkeypatch) -> None:
        """成功一次后连续 429 计数清零：下一次 429 又从 10 秒开始。"""
        monkeypatch.setattr(mm, "_jitter", lambda s: s)
        seq = [_rate_limited("10"), _ok_response("m1"), _rate_limited("10"), _ok_response("m1")]
        step = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            resp = seq[min(step["i"], len(seq) - 1)]
            step["i"] += 1
            return resp

        store, holder, models = _make(tmp_path, {"retries": 1}, handler)
        assert (await _chat(models, "1")).text == "好"
        assert (await _chat(models, "2")).text == "好"
        assert fake_time.slept == [10, 10], "计数清零后第二次 429 还是 10 秒（不是 20）"
        await models.close()

    @pytest.mark.asyncio
    async def test_other_endpoint_not_delayed(self, tmp_path, fake_time) -> None:
        """不同端点互不影响：a.test 冷却中，b.test 照发不等。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if "a.test" in str(request.url):
                return _rate_limited("60")
            return _ok_response("m1", "b 端点不用等")

        store, holder, models = _make(tmp_path, {"base_url": "https://a.test/v1"}, handler)
        with pytest.raises(ModelError):
            await _chat(models, "1")
        assert fake_time.slept == []
        holder["settings"] = _settings_ns(base_url="https://b.test/v1")
        r = await _chat(models, "2")
        assert r.text == "b 端点不用等"
        assert fake_time.slept == [], "别的端点不受 a.test 冷却影响"
        await models.close()

    @pytest.mark.asyncio
    async def test_endpoint_key_ignores_case_and_trailing_slash(self, tmp_path, fake_time) -> None:
        """端点按 base_url 规范化分组：大小写 / 末尾斜杠不同也算同一个端点。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _rate_limited("60")

        store, holder, models = _make(tmp_path, {"base_url": "https://A.test/v1/"}, handler)
        with pytest.raises(ModelError):
            await _chat(models, "1")
        holder["settings"] = _settings_ns(base_url="https://a.test/v1")
        with pytest.raises(ModelError):
            await _chat(models, "2")
        assert fake_time.slept == [60], "两种写法是同一个端点，第二个请求要等冷却"
        await models.close()


class TestCooldownLog:
    @pytest.mark.asyncio
    async def test_logs_host_and_seconds_without_key(self, tmp_path, fake_time, caplog) -> None:
        """进冷却时 info 一行：端点 host + 冷却秒数，不打密钥。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _rate_limited("45")

        store, holder, models = _make(tmp_path, {"base_url": "https://api.stepfun.ai/v1"}, handler)
        with caplog.at_level(logging.INFO, logger="maiwork.models"):
            with pytest.raises(ModelError):
                await _chat(models, "1")
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "api.stepfun.ai" in text, "日志要有端点 host"
        assert "45" in text, "日志要有冷却秒数"
        assert SECRET not in text, "日志不打密钥"
        await models.close()


# ----------------------------------------------------------------------
# 每分钟上限（max_rpm）
# ----------------------------------------------------------------------


class TestMaxRpm:
    @pytest.mark.asyncio
    async def test_window_waits_when_full(self, tmp_path, fake_time) -> None:
        """max_rpm=2：60 秒窗口内第 3 个请求要等到窗口空出位置。"""
        sent: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(1)
            return _ok_response("m1")

        store, holder, models = _make(tmp_path, {"max_rpm": 2}, handler)
        for i in range(3):
            await _chat(models, str(i))
        assert len(sent) == 3, "排队等的请求最终都要发出去（不报错）"
        assert fake_time.slept == [60], "第 3 个请求要等满 60 秒窗口"
        await models.close()

    @pytest.mark.asyncio
    async def test_zero_means_off(self, tmp_path, fake_time) -> None:
        """max_rpm=0 表示关闭。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _ok_response("m1")

        store, holder, models = _make(tmp_path, {"max_rpm": 0}, handler)
        for i in range(3):
            await _chat(models, str(i))
        assert fake_time.slept == []
        await models.close()

    @pytest.mark.asyncio
    async def test_missing_field_is_off(self, tmp_path, fake_time) -> None:
        """真配置里没有 max_rpm 字段：按 0 = 关闭。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _ok_response("m1")

        store = Store(tmp_path / "test.db")
        store.migrate()
        settings = _settings({"models": _chat_payload(api_key=SECRET, main="m1", worker="w1")})
        models = Models(store, lambda: settings, transport=httpx.MockTransport(handler))
        for i in range(3):
            await _chat(models, str(i))
        assert fake_time.slept == []
        await models.close()


# ----------------------------------------------------------------------
# 取消
# ----------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_propagates_while_waiting(self, tmp_path, monkeypatch) -> None:
        """冷却等待中取消：CancelledError 原样抛出（不吞、不发出请求）。"""
        fake = FakeClock()
        monkeypatch.setattr(mm, "_NOW", fake.now)
        started = asyncio.Event()
        sent = {"n": 0}

        async def slow_sleep(seconds: float) -> None:
            started.set()
            await asyncio.sleep(30)  # 真等，但马上会被取消

        monkeypatch.setattr(mm, "_SLEEP", slow_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            sent["n"] += 1
            return _rate_limited("60")

        store, holder, models = _make(tmp_path, None, handler)
        with pytest.raises(ModelError):
            await _chat(models, "1")
        assert sent["n"] == 1
        task = asyncio.create_task(_chat(models, "2"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sent["n"] == 1, "取消发生在等待中：第二个请求根本不该发出去"
        await models.close()


# ----------------------------------------------------------------------
# Retry-After 解析 / 抖动（纯函数）
# ----------------------------------------------------------------------


class TestRetryAfterParse:
    @pytest.mark.parametrize(
        ("raw", "expect"),
        [("30", 30.0), ("0", 0.0), ("", 0.0), (None, 0.0), ("abc", 0.0), ("-5", 0.0), ("2.5", 2.5)],
    )
    def test_seconds_or_garbage(self, raw, expect) -> None:
        assert mm._parse_retry_after(raw, 1000.0) == expect

    def test_http_date_future(self) -> None:
        raw = email.utils.formatdate(1045.0, usegmt=True)
        assert mm._parse_retry_after(raw, 1000.0) == pytest.approx(45.0, abs=1.0)

    def test_http_date_past_is_zero(self) -> None:
        raw = email.utils.formatdate(500.0, usegmt=True)
        assert mm._parse_retry_after(raw, 1000.0) == 0.0


class TestJitter:
    def test_jitter_is_within_20_percent(self) -> None:
        values = [mm._jitter(10.0) for _ in range(200)]
        assert all(8.0 <= v <= 12.0 for v in values), "抖动 ±20%"
        assert len(set(values)) > 1, "不是固定值（真的有抖动）"

    def test_jitter_zero(self) -> None:
        assert mm._jitter(0.0) == 0.0
