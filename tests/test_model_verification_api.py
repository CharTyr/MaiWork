"""R01/R02/R03：真实网页接口接线、只改建议参数、过期保护与密钥遮罩。"""
from __future__ import annotations
import json
import httpx
import pytest
from test_endpoints_api import env  # noqa: F401

pytestmark=pytest.mark.asyncio

async def prepare(env,handler):
    await env.app.models.close()
    env.app.models._transport=httpx.MockTransport(handler)
    assert (await env.client.put('/api/settings/endpoints/e1',json={'base_url':'https://fake.test/v1','api_key':'sk-only-fake-secret'})).status==200
    assert (await env.client.put('/api/settings/model-list/m1',json={'endpoint':'e1','model':'fake-model'})).status==200
    assert (await env.client.put('/api/agents/main',json={'model':'m1'})).status==200


def reply(req):
    body=json.loads(req.content)
    msg={'role':'assistant','content':'OK'}
    if body.get('tools') and not any(m.get('role')=='tool' for m in body['messages']):
        msg={'role':'assistant','content':'','tool_calls':[{'id':'c1','type':'function','function':{'name':'maiwork_ping','arguments':'{"word":"hi"}'}}]}
    return httpx.Response(200,json={'choices':[{'message':msg}],'usage':{'prompt_tokens':1,'completion_tokens':1}})

async def test_route_saves_actual_fingerprints_and_current_record(env):
    await prepare(env,reply)
    response=await env.client.post('/api/settings/model-list/m1/verify',json={}); assert response.status==200
    result=await response.json(); assert result['ok'] and result['tools_ok'] and not result['stale']
    record=env.app.models.verification_for('main')
    assert record['fingerprint']==result['fingerprint']==env.app.models.verification_stamp('m1')
    assert record['requested_fingerprint']==result['requested_fingerprint']
    assert 'sk-only-fake-secret' not in json.dumps(record)

async def test_known_failure_is_off_in_completion_and_settings_health(env):
    await prepare(env,lambda req:httpx.Response(401,json={'error':{'message':'fake bad key'}}))
    response=await env.client.post('/api/settings/model-list/m1/verify',json={}); assert response.status==200
    info=await (await env.client.get('/api/onboarding')).json(); assert not info['usable']
    settings=await (await env.client.get('/api/settings')).json()
    health=next(x for x in settings['health'] if x['key']=='models')
    assert health['state']=='off' and '401' in health['text']

async def test_lowered_probe_is_pending_until_save_and_does_not_overwrite_changes(env):
    def endpoint(req):
        if json.loads(req.content).get('max_tokens',0)>8192:
            return httpx.Response(400,json={'error':{'message':'max_tokens must be <= 8192'}})
        return reply(req)
    await prepare(env,endpoint)
    result=await (await env.client.post('/api/settings/model-list/m1/verify',json={})).json()
    assert result['suggested_max_tokens']==8192
    assert env.app.models.verification_for('main')['ok'] is False
    saved=await env.client.put('/api/settings/model-list/m1',json={'max_tokens':8192,'if_fingerprint':result['requested_fingerprint']})
    assert saved.status==200
    assert env.app.models.verification_for('main')['ok'] is True
    # 旧建议不能覆盖后来的配置；原 API 支持只传改的字段，不要整条覆盖。
    conflict=await env.client.put('/api/settings/model-list/m1',json={'max_tokens':4096,'if_fingerprint':result['requested_fingerprint']})
    assert conflict.status==409 and env.app.models.limits_for('main')['max_tokens']==8192

async def test_unexpected_probe_error_never_echoes_key(env,monkeypatch):
    await prepare(env,reply)
    async def broken(_id): raise RuntimeError('fake failure sk-only-fake-secret')
    monkeypatch.setattr(env.app.models,'verify_entry',broken)
    response=await env.client.post('/api/settings/model-list/m1/verify',json={})
    body=await response.json()
    assert not body['ok'] and 'sk-only-fake-secret' not in json.dumps(body)
    assert 'sk-only-fake-secret' not in json.dumps(env.app.models.verification_for('main'))
