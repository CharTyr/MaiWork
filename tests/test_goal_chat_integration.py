import pytest
from test_console import env, G1
from test_goal_chat_repair import Chat, goal

@pytest.mark.asyncio
async def test_group_admin_cannot_enter_global_tool_conversation(env):
    env.app.group_admins.set_password(G1, "group-test-password")
    await env.login("group-test-password")
    gid = goal(env.app)
    env.app.admin_chat = Chat()
    for method in ("get", "post"):
        response = await getattr(env.client, method)(f"/api/goals/{gid}/chat", **({"json": {"text": "暂停"}} if method == "post" else {}))
        assert response.status == 403
    assert env.app.admin_chat.created == 0


@pytest.mark.asyncio
async def test_goal_conversation_confirmation_runs_registered_action(env):
    from test_admin_chat import _Models, _res, _call
    await env.login()
    gid = goal(env.app)
    env.app.models = _Models(script=[
        _res(tool_calls=[_call("goal_adjust", {"group_id": G1, "goal_id": gid, "action": "pause"})]),
        _res("请确认暂停这个目标"), _res("已暂停"),
    ])
    response = await env.client.post(f"/api/goals/{gid}/chat", json={"text": "暂停这个目标"})
    assert response.status == 202
    cid = env.app.goals.get_admin_chat(gid)
    await env.app.admin_chat.wait_idle(cid)
    result = await (await env.client.get(f"/api/goals/{gid}/chat")).json()
    assert env.app.goals.get(gid)["state"] == "active"
    assert result["pending"]
    pid = result["pending"][0]["id"]
    response = await env.client.post(f"/api/chat/pending/{pid}", json={"approve": True})
    assert response.status == 200
    await env.app.admin_chat.wait_idle(cid)
    assert env.app.goals.get(gid)["state"] == "paused"
    assert gid in str(env.app.models.calls[0][1])
