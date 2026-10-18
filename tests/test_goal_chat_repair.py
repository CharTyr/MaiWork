"""目标对话使用真实 HTTP 路由验证授权、绑定和持久消息协议。"""
import pytest

from test_console import env, G1, G2, SECRET


class Chat:
    def __init__(self):
        self.messages = []
        self.created = 0
        self.group = G1

    async def close(self):
        pass

    def create(self, group_id):
        self.created += 1
        self.group = group_id
        return {"id": 91, "group_id": group_id}

    def detail(self, cid, after=0):
        return {"chat": {"id": cid, "group_id": self.group},
                "messages": self.messages, "pending": [], "running": True}

    def is_busy(self, cid):
        return True

    async def send(self, cid, text):
        self.messages.append({"id": 12, "role": "user", "content": text})
        return 12


def goal(app):
    return app.goals.create_agent(G1, title="检查版本更新", body="每天观察发布页", criteria=["核实更新"], by_text="管理员")


@pytest.mark.asyncio
async def test_goal_chat_requires_global_admin_before_lookup(env):
    env.app.admin_chat = Chat()
    for method in ("get", "post"):
        response = await getattr(env.client, method)("/api/goals/G-missing/chat", **({"json": {"text": "hello"}} if method == "post" else {}))
        assert response.status == 401
    assert env.app.admin_chat.created == 0


@pytest.mark.asyncio
async def test_goal_chat_async_accept_and_persistent_history(env):
    await env.login()
    gid = goal(env.app)
    chat = Chat()
    env.app.admin_chat = chat
    initial = await env.client.get(f"/api/goals/{gid}/chat")
    assert initial.status == 200
    assert (await initial.json())["messages"] == []
    assert chat.created == 0
    sent = await env.client.post(f"/api/goals/{gid}/chat", json={"text": "换个网址"})
    assert sent.status == 202
    assert (await sent.json())["user_message_id"] == 12
    result = await env.client.get(f"/api/goals/{gid}/chat")
    data = await result.json()
    assert data["running"] is True
    assert data["messages"][0]["content"] == "换个网址"
    assert chat.created == 1


@pytest.mark.asyncio
async def test_goal_chat_rejects_mismatched_group_and_masks_history(env):
    await env.login()
    gid = goal(env.app)
    env.app.goals.set_admin_chat(gid, 91)
    chat = Chat()
    env.app.admin_chat = chat
    chat.group = G2
    response = await env.client.get(f"/api/goals/{gid}/chat")
    assert response.status == 409
    chat.group = G1
    chat.messages = [{"id": 1, "role": "assistant", "content": SECRET}]
    response = await env.client.get(f"/api/goals/{gid}/chat")
    assert response.status == 200
    assert SECRET not in await response.text()


@pytest.mark.asyncio
async def test_goal_chat_bad_body_and_unserved_goal(env):
    await env.login()
    gid = goal(env.app)
    env.app.admin_chat = Chat()
    response = await env.client.post(f"/api/goals/{gid}/chat", data="invalid")
    assert response.status == 400
    with env.app.store.tx() as conn:
        conn.execute("UPDATE goals SET group_id='not-served' WHERE id=?", (gid,))
    response = await env.client.get(f"/api/goals/{gid}/chat")
    assert response.status == 404
