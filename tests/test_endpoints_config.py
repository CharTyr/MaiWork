"""[[endpoints]] / [[model_list]] 两段配置的规范化测试（2026-10 改版阶段 1a）。

config.toml 是唯一真实来源：MaiWork 的模型改成「端点 + 模型库」。
- [[endpoints]]：id / name / protocol(openai|anthropic|responses) / base_url / api_key /
  retries / retry_delay_s / max_concurrency / max_rpm。
- [[model_list]]：id / endpoint（必须指向存在的端点）/ model（服务商模型名）/ name /
  efforts（low..max 有序子集，可空）/ vision / context_window / max_tokens（< context_window）。
坏条目逐条丢弃、记中文问题，绝不抛异常。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.config import CONFIG_VERSION, load_settings


def _endpoint(**over):
    d = {
        "id": "default",
        "name": "默认端点",
        "protocol": "openai",
        "base_url": "https://api.test/v1",
        "api_key": "sk-test",
    }
    d.update(over)
    return d


def _model(**over):
    d = {"id": "m1", "endpoint": "default", "model": "gpt-x"}
    d.update(over)
    return d


class TestEndpointsOK:
    def test_defaults_filled(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint()]})
        assert settings.endpoints
        e = settings.endpoints[0]
        assert e.id == "default"
        assert e.name == "默认端点"
        assert e.protocol == "openai"
        assert e.base_url == "https://api.test/v1"
        assert e.api_key == "sk-test"
        assert e.retries == 5
        assert e.retry_delay_s == 10
        assert e.max_concurrency == 2
        assert e.max_rpm == 0
        assert problems == []

    def test_bounds(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint(retries=0, retry_delay_s=60, max_concurrency=8, max_rpm=600)]
        })
        e = settings.endpoints[0]
        assert (e.retries, e.retry_delay_s, e.max_concurrency, e.max_rpm) == (0, 60, 8, 600)

    def test_protocols_all_ok(self) -> None:
        settings, _ = load_settings({
            "endpoints": [
                _endpoint(id="a", protocol="anthropic"),
                _endpoint(id="b", protocol="responses"),
                _endpoint(id="c", protocol="openai"),
            ]
        })
        assert [e.protocol for e in settings.endpoints] == ["anthropic", "responses", "openai"]

    def test_model_defaults(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model()],
        })
        assert problems == []
        m = settings.model_list[0]
        assert m.id == "m1"
        assert m.endpoint == "default"
        assert m.model == "gpt-x"
        assert m.name == "gpt-x"  # 默认显示名 = 模型名
        assert m.efforts == ()
        assert m.vision is False
        assert m.context_window == 128000
        assert m.max_tokens == 32768

    def test_model_full(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(name="小主模型", efforts=["high", "low"], vision=True,
                                   context_window=1_000_000, max_tokens=999_999)],
        })
        m = settings.model_list[0]
        assert m.name == "小主模型"
        assert m.efforts == ("high", "low")  # 保序
        assert m.vision is True
        assert m.context_window == 1_000_000
        assert m.max_tokens == 999_999

    def test_empty_by_default(self) -> None:
        settings, problems = load_settings({})
        assert settings.endpoints == ()
        assert settings.model_list == ()
        assert problems == []


class TestEndpointProblems:
    def test_bad_id(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint(id="BAD ID!")]})
        assert settings.endpoints == ()
        assert any("不合法" in p and "default" not in p for p in problems) or any("BAD ID" in p for p in problems)

    def test_duplicate_id_keeps_first(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint(id="a", name="一"), _endpoint(id="a", name="二")]
        })
        assert len(settings.endpoints) == 1
        assert settings.endpoints[0].name == "一"
        assert any("重复" in p for p in problems)

    def test_bad_protocol(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint(protocol="grpc")]})
        assert settings.endpoints == ()
        assert any("协议" in p for p in problems)

    def test_bad_base_url(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint(base_url="ftp://x") ]})
        assert settings.endpoints == ()
        assert any("http" in p for p in problems)

    def test_http_base_url_ok(self) -> None:
        settings, _ = load_settings({"endpoints": [_endpoint(base_url="http://127.0.0.1:8300/v1/")]})
        assert settings.endpoints[0].base_url == "http://127.0.0.1:8300/v1"

    def test_name_too_long(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint(name="长" * 41)]})
        assert settings.endpoints == ()
        assert any("名字" in p for p in problems)

    def test_bad_number_type(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint(retries="五")]})
        assert settings.endpoints == ()
        assert problems

    def test_out_of_range(self) -> None:
        settings, problems = load_settings({"endpoints": [_endpoint(retries=99)]})
        assert settings.endpoints == ()
        assert any("retries" in p or "重试" in p for p in problems)

    def test_bad_single_entry_does_not_kill_others(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint(id="good"), {"id": "BAD ID"}, _endpoint(id="good2", name="")]
        })
        assert [e.id for e in settings.endpoints] == ["good", "good2"]

    def test_name_defaults_to_id(self) -> None:
        settings, _ = load_settings({"endpoints": [_endpoint(name="")]})
        assert settings.endpoints[0].name == "default"


class TestModelListProblems:
    def test_unknown_endpoint(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(endpoint="ghost")],
        })
        assert settings.model_list == ()
        assert any("端点" in p and "ghost" in p for p in problems)

    def test_duplicate_model_id(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(id="m1", model="a"), _model(id="m1", model="b")],
        })
        assert len(settings.model_list) == 1
        assert settings.model_list[0].model == "a"
        assert any("重复" in p for p in problems)

    def test_empty_model_rejected(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(model="  ")],
        })
        assert settings.model_list == ()
        assert problems

    def test_malformed_id(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(id="空格 不行")],
        })
        assert settings.model_list == ()

    def test_bad_effort_dropped(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(efforts=["low", "turbo", "max"])],
        })
        assert settings.model_list[0].efforts == ("low", "max")
        assert any("turbo" in p for p in problems)

    def test_max_tokens_must_be_below_context_window(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(context_window=100000, max_tokens=200000)],
        })
        assert settings.model_list == ()
        assert any("小于" in p or "必须" in p for p in problems)

    def test_vision_type(self) -> None:
        settings, problems = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(vision="yes")],
        })
        assert settings.model_list == ()
        assert problems

    def test_model_name_too_long(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(name="长" * 61)],
        })
        assert settings.model_list == ()

    def test_model_id_too_long(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint(id="x" * 25)],
            "model_list": [_model(id="y" * 25)],
        })
        assert settings.endpoints == ()
        assert settings.model_list == ()

    def test_numbers_out_of_range_rejected(self) -> None:
        settings, _ = load_settings({
            "endpoints": [_endpoint()],
            "model_list": [_model(context_window=8999999)],
        })
        assert settings.model_list == ()


class TestOldModelsSectionStillParses:
    def test_old_models_keys_still_read(self) -> None:
        """阶段迁移前旧 [models] 还要能读出来（迁移用，别在过渡期内读崩）。"""
        settings, _ = load_settings({
            "models": {"base_url": "https://old.test/v1", "api_key": "sk-old", "main": "m", "worker": "w"}
        })
        assert settings.models.base_url == "https://old.test/v1"
        assert settings.models.main == "m"

    def test_config_version_bumped(self) -> None:
        assert CONFIG_VERSION == "0.4.8"
