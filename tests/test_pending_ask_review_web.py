import pytest

import test_group_admin as group_admin
from test_group_admin import G1, G2, G1_PW, ADMIN_PW

env = group_admin.env
from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.pending_asks import PendingAsks
from CharTyr_MaiWork.maiwork.console import views


def seed(env):
    pa = PendingAsks(env.app.store, env.app.get_settings)
    env.app.profiles.pending_asks = pa
    for gid in (G1, G2):
        pa.record_pending(gid, {"message_id": "old", "user_id": "u1", "user_name": "某成员",
                               "text": "整理资料", "ts": clock.now() - 49 * 3600}, "jev_超时")
        pa.mark_stale(gid)
    return pa


@pytest.mark.asyncio
async def test_review_view_admin_only(env):
    seed(env)
    admin = views.group_view(env.app, G1, admin=True)
    assert admin["tasks"]["review"][0]["message_id"] == "old"
    assert "review" not in views.group_view(env.app, G1, admin=False)["tasks"]


@pytest.mark.asyncio
async def test_review_view_does_not_expose_exception_text(env):
    seed(env)
    with env.app.store.tx() as conn:
        conn.execute("UPDATE pending_asks SET last_error=? WHERE group_id=?",
                     ("private-error-credential-value", G1))
    admin = views.group_view(env.app, G1, admin=True)
    assert "private-error-credential-value" not in str(admin["tasks"]["review"])


@pytest.mark.asyncio
async def test_review_permissions_and_origin(env):
    pa = seed(env)
    url = f"/api/groups/{G1}/pending-asks/old"
    assert (await env.client.post(url, json={"action": "ignore"})).status == 401
    env.set_group_password(G1, G1_PW)
    await env.login(G1_PW)
    assert (await env.client.post(f"/api/groups/{G2}/pending-asks/old", json={"action": "ignore"})).status == 403
    assert (await env.client.post(url, json={"action": "ignore"}, headers={"Origin": "https://evil.test"})).status == 403
    assert pa.get(G1, "old")["state"] == "review"
    r = await env.client.post(url, json={"action": "ignore"})
    assert r.status == 200, await r.text()
    assert pa.get(G1, "old")["state"] == "none"
    assert pa.get(G2, "old")["state"] == "review"


@pytest.mark.asyncio
async def test_review_convert_and_invalid_payload(env):
    seed(env)
    await env.login(ADMIN_PW)
    url = f"/api/groups/{G1}/pending-asks/old"
    assert (await env.client.post(url, json={"action": "request", "kind": "reminder"})).status == 400
    assert (await env.client.post(url, json=[])).status == 400
    r = await env.client.post(url, json={"action": "request", "kind": "task"})
    assert r.status == 200, await r.text()
    rid = (await r.json())["request_id"]
    assert env.app.approvals.pending_view(G1)[0]["id"] == rid
    assert (await env.client.post(url, json={"action": "request", "kind": "task"})).status == 200
    assert (await env.client.post(url, json={"action": "ignore"})).status == 409
    assert (await env.client.post(f"/api/groups/{G1}/pending-asks/missing", json={"action": "ignore"})).status == 404
