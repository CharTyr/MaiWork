"""模型限流的两个设置（[models] max_concurrency / max_rpm）接到配置、网页，以及测试连接不干等。

- config.toml 能写：max_concurrency 1~8（缺省 2），max_rpm 0~600（缺省 0 = 不限）；越界夹回。
- 设置 → 模型 的 public() 带这两项；save() 能改、非法值给中文错；没传保留当前值。
- 端点刚被 429 冷却时，「测试连接 / 列模型」直接报错说还要等几秒，不让网页转圈两分钟。
"""

from __future__ import annotations

import pytest

import CharTyr_MaiWork.models as mm
from CharTyr_MaiWork.models import ModelError

from test_models import SECRET, FakeEndpoint, _chat_payload, _patch
from test_model_retry import _make


class TestLimitSettings:
    def test_defaults(self, tmp_path) -> None:
        store, models = _make(tmp_path, {"models": _chat_payload(api_key=SECRET)}, FakeEndpoint({}))
        s = models.settings()
        assert s.max_concurrency == 2
        assert s.max_rpm == 0

    def test_config_values_reach_settings(self, tmp_path) -> None:
        cfg = {"models": _chat_payload(api_key=SECRET, max_concurrency=1, max_rpm=12)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        pub = models.settings().public()
        assert pub["max_concurrency"] == 1
        assert pub["max_rpm"] == 12

    @pytest.mark.parametrize("bad_conc,bad_rpm", [(0, -1), (99, 9999)])
    def test_config_out_of_range_clamped(self, tmp_path, bad_conc, bad_rpm) -> None:
        cfg = {"models": _chat_payload(api_key=SECRET, max_concurrency=bad_conc, max_rpm=bad_rpm)}
        store, models = _make(tmp_path, cfg, FakeEndpoint({}))
        s = models.settings()
        assert 1 <= s.max_concurrency <= 8
        assert 0 <= s.max_rpm <= 600

    def test_save_accepts_limits(self, tmp_path) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        models.save(_patch(api_key=SECRET, max_concurrency=1, max_rpm=20))
        s = models.settings()
        assert (s.max_concurrency, s.max_rpm) == (1, 20)

    def test_save_keeps_limits_when_missing(self, tmp_path) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        models.save(_patch(api_key=SECRET, max_concurrency=3, max_rpm=6))
        models.save(_patch())
        s = models.settings()
        assert (s.max_concurrency, s.max_rpm) == (3, 6)

    @pytest.mark.parametrize("field,bad", [("max_concurrency", 0), ("max_concurrency", 9), ("max_rpm", -1), ("max_rpm", "x")])
    def test_save_invalid(self, tmp_path, field, bad) -> None:
        store, models = _make(tmp_path, {}, FakeEndpoint({}))
        with pytest.raises(ValueError):
            models.save(_patch(api_key=SECRET, **{field: bad}))


class TestListModelsDuringCooldown:
    @pytest.mark.asyncio
    async def test_fails_fast_instead_of_waiting(self, tmp_path, monkeypatch) -> None:
        slept: list[float] = []

        async def _no_sleep(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(mm, "_SLEEP", _no_sleep)
        store, models = _make(tmp_path, {"models": _chat_payload(api_key=SECRET)}, FakeEndpoint({}))
        base = models.settings().base_url
        models._throttle.note_429(base, 90)
        with pytest.raises(ModelError) as ei:
            await models.list_models(base)
        assert "秒" in str(ei.value)
        assert SECRET not in str(ei.value)
        assert slept == []
