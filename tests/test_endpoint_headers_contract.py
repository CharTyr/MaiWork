"""主会话验收：前后端空值约定、HTTP token、默认兼容及凭据回显防护。"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_endpoints_api import env  # noqa: F401 — 复用隔离的端点 API 环境
from CharTyr_MaiWork.maiwork import config as cfg
from CharTyr_MaiWork.maiwork.models import Models


@pytest.mark.parametrize("blank", ["", None])
@pytest.mark.asyncio
async def test_saved_blank_header_test_keeps_value_without_writing(env, blank):
    r=await env.client.put("/api/settings/endpoints/e1", json={
        "base_url":"https://a.test/v1", "api_key":"example-api-key",
        "headers":{"X-Keep":"existing-private-value", "X-Remove":"remove-private-value"},
    })
    assert r.status==200
    before=(env.plug_dir/"config.toml").read_text()
    r=await env.client.post("/api/settings/endpoints/e1/test", json={"headers":{"x-keep":blank}})
    assert r.status==200, await r.text()
    assert (await r.json())["ok"] is True
    heads=env.app.models._transport.handler.seen[-1]["headers"]
    assert heads["x-keep"]=="existing-private-value"
    assert "x-remove" not in heads
    assert (env.plug_dir/"config.toml").read_text()==before


@pytest.mark.parametrize("name", ['X(Thing)', 'X/Thing', 'X=Thing', 'X,Thing', 'X;Thing', 'X"Thing', 'X[Thing]', 'X{Thing}', 'X?Thing', 'X\\Thing'])
def test_only_rfc_token_header_names_allowed(name):
    assert cfg.validate_endpoint_header_name(name)[0], "HTTP 头名不是任意可打印 ASCII"


def test_endpoint_direct_default_is_readonly():
    ep=cfg.EndpointSetting(id="e1",name="one",protocol="openai",base_url="https://a.test/v1",api_key="example-key")
    with pytest.raises(TypeError):
        ep.headers["X-Mutate"]="no"


def test_empty_headers_preserve_legacy_verification_stamp():
    models=Models.__new__(Models)
    ep=SimpleNamespace(id="e1",base_url="https://a.test/v1",api_key="example-key",protocol="openai",headers={})
    entry=SimpleNamespace(model="svc",context_window=128000,max_tokens=32768,efforts=(),vision=False)
    material=models._stamp_material("m1",ep,entry)
    material.pop("headers_fp",None)
    expected=hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    assert models.verification_stamp("m1",_caps=(entry,ep))==expected


@pytest.mark.asyncio
async def test_header_auth_token_without_scheme_is_masked(env):
    token="opaque-auth-credential-9"
    await env.client.put("/api/settings/endpoints/e1",json={
        "base_url":"https://a.test/v1","api_key":"example-api-key","headers":{"Authorization":"Bearer "+token},
    })
    tr=env.app.models._transport.handler
    tr.status=401;tr.payload={"error":"invalid token "+token}
    r=await env.client.post("/api/settings/endpoints/e1/test",json={})
    assert token not in await r.text()
    assert token in env.app._known_secrets()


@pytest.mark.asyncio
async def test_successful_list_cannot_store_echoed_header_credentials(env):
    secret="private-list-value-987"
    await env.client.put("/api/settings/endpoints/e1",json={"base_url":"https://a.test/v1","api_key":"example-api-key"})
    tr=env.app.models._transport.handler
    tr.payload={"data":[{"id":"provider-debug-"+secret}]}
    r=await env.client.post("/api/settings/endpoints/e1/test",json={"headers":{"X-Token":secret}})
    assert r.status==200
    assert secret not in await r.text()
    checked=env.app.store.kv_get("endpoints.checked.e1")
    assert secret not in json.dumps(checked)


def test_headers_survive_host_typed_config_roundtrip():
    typed = cfg.MaiWorkConfig.model_validate({"endpoints": [{
        "id": "e1", "base_url": "https://a.test/v1", "api_key": "example-key",
        "headers": {"X-Route": "retained-after-reload"},
    }]})
    settings, problems = cfg.load_settings(typed)
    assert not problems
    assert dict(settings.endpoints[0].headers) == {"X-Route": "retained-after-reload"}


@pytest.mark.parametrize("bad", ["\r\n", "\v", " " * 8193])
@pytest.mark.parametrize("operation", ["save", "test"])
@pytest.mark.asyncio
async def test_blank_looking_invalid_value_is_not_a_keep_placeholder(env, bad, operation):
    await env.client.put("/api/settings/endpoints/e1", json={
        "base_url": "https://a.test/v1", "api_key": "example-key", "headers": {"X-Route": "old-private-value"},
    })
    if operation == "save":
        r = await env.client.put("/api/settings/endpoints/e1", json={"headers": {"X-Route": bad}})
    else:
        r = await env.client.post("/api/settings/endpoints/e1/test", json={"headers": {"X-Route": bad}})
    assert r.status == 400, "换行/控制字符/超长空白不能被当成留空保留"


@pytest.mark.parametrize("secret_kind", ["bearer", "other_endpoint"])
@pytest.mark.asyncio
async def test_chat_errors_and_usage_mask_all_endpoint_credentials(env, secret_kind):
    from CharTyr_MaiWork.maiwork.models import ModelError
    token = "private-bare-auth-token-321"
    other = "private-other-endpoint-token-654"
    await env.client.put("/api/settings/endpoints/e1", json={
        "base_url": "https://a.test/v1", "api_key": "example-key", "headers": {"Authorization": "Bearer " + token},
    })
    await env.client.put("/api/settings/endpoints/e2", json={
        "base_url": "https://b.test/v1", "api_key": "example-key2", "headers": {"X-Other": other},
    })
    await env.client.put("/api/settings/model-list/m1", json={"endpoint": "e1", "model": "test-model"})
    env.app.agents.update_profile("main", {"model": "m1"})
    tr = env.app.models._transport.handler
    secret = token if secret_kind == "bearer" else other
    tr.status = 401
    tr.payload = {"error": "invalid credential " + secret}
    with pytest.raises(ModelError) as exc:
        await env.app.models.chat(agent="main", messages=[{"role": "user", "content": "hi"}], retries=0)
    assert secret not in str(exc.value)
    row = env.app.store.read().execute("SELECT error FROM usage ORDER BY id DESC LIMIT 1").fetchone()
    assert secret not in row["error"]


@pytest.mark.asyncio
async def test_logs_api_masks_endpoint_headers_even_for_old_raw_record(env):
    secret = "private-archived-header-token-986"
    await env.client.put("/api/settings/endpoints/e1", json={
        "base_url": "https://a.test/v1", "api_key": "example-key", "headers": {"X-Route": secret},
    })
    with env.app.store.tx() as conn:
        cursor = conn.execute("INSERT INTO model_calls(ts,error) VALUES(1,?)", ("gateway echo " + secret,))
        call_id = cursor.lastrowid
    for path in ("/api/logs/model-calls", f"/api/logs/model-calls/{call_id}"):
        r = await env.client.get(path)
        assert r.status == 200
        assert secret not in await r.text()


@pytest.mark.asyncio
async def test_listing_same_address_endpoints_uses_matching_key_not_first_headers(env):
    for endpoint, key, head_value in (("e1", "example-key1", "first-value"), ("e2", "example-key2", "second-value")):
        r = await env.client.put(f"/api/settings/endpoints/{endpoint}", json={
            "base_url": "https://a.test/v1", "api_key": key, "headers": {"X-Route": head_value},
        })
        assert r.status == 200
    await env.app.models.list_models("https://a.test/v1", "example-key2")
    heads = env.app.models._transport.handler.seen[-1]["headers"]
    assert heads["x-route"] == "second-value", "同一地址的独立端点不能串请求头"


@pytest.mark.asyncio
async def test_unsaved_new_endpoint_does_not_inherit_same_address_headers(env):
    await env.client.put("/api/settings/endpoints/e1", json={
        "base_url": "https://a.test/v1", "api_key": "example-key", "headers": {"X-Private": "existing-endpoint-private-value"},
    })
    r = await env.client.post("/api/settings/endpoints/new-endpoint/test", json={
        "base_url": "https://a.test/v1", "api_key": "example-key", "protocol": "openai",
    })
    assert r.status == 200
    assert (await r.json())["ok"] is True
    heads = env.app.models._transport.handler.seen[-1]["headers"]
    assert "x-private" not in heads, "未保存的新端点不得按地址偷偷继承其他端点的头"
