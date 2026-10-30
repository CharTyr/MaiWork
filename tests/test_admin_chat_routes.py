"""console 的「和 MaiWork 聊」路由（/api/chat*）：鉴权、状态码、轮询 after、同源、密钥不外泄。

用 aiohttp.test_utils 起真的回环服务器；svc 是假的（admin_chat 用假模块，
同步/异步方法各来一个，逼路由两边都认），不碰 MaiBot 宿主、不碰线上。
响应字段按 console/static/js/chat.js 的「和 MaiWork 聊」页来断言。
"""

from __future__ import annotations

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.console.auth import COOKIE_NAME
from CharTyr_MaiWork.maiwork.console.server import AUTH_KEY, create_app

SECRET = "sk-管理员对话测试密钥-绝不该出现在响应里"
SK_LIKE = "sk-abcdef123456"
G1 = "900000001"
G2 = "123456789"
G3 = "555000111"  # 不在服务群名单里
TOKEN_G1 = "tok-g1"
MAX_TITLE = 100  # 和 admin_chat.MAX_TITLE 一致
MAX_TEXT = 20000  # 和 admin_chat.MAX_TEXT 一致


class ChatBusy(ValueError):
    """照 admin_chat.ChatBusy 的样子造：ValueError 子类，网页要认成 409。"""

# 六条路由 + 各自的请求体（匿名 401 / 群友 403 / 模块没开 503 都用它跑一遍）
ROUTES = [
    ("GET", "/api/chat", None),
    ("POST", "/api/chat", {"group_id": G1}),
    ("PATCH", "/api/chat/1", {"title": "改个名"}),
    ("GET", "/api/chat/1", None),
    ("POST", "/api/chat/1/messages", {"text": "你好"}),
    ("POST", "/api/chat/pending/7", {"approve": True}),
]


class _FakeCursor:
    """只认两条查询：群链接码查表（鉴权）、secrets 表（密钥遮罩）。"""

    def __init__(self, sql: str, params: tuple, secrets: list[str]) -> None:
        self._sql = sql
        self._params = params
        self._secrets = secrets

    def fetchone(self):
        if "FROM groups" in self._sql and self._params[:1] == (TOKEN_G1,):
            return {"group_id": G1}
        return None

    def fetchall(self):
        if "FROM secrets" in self._sql:
            return [{"value": s} for s in self._secrets]
        return []


class _FakeStore:
    """够 ConsoleAuth（cookie 签名）+ 密钥遮罩用；不做真库。"""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = list(secrets or [])

    def secret_get(self, key: str) -> str:
        return "fixed-console-secret"  # 有值 → ConsoleAuth 不再写库

    def read(self):
        return self

    def execute(self, sql: str, params: tuple = ()):
        return _FakeCursor(str(sql), tuple(params), self.secrets)


class _FakeAdminChat:
    """假 admin_chat：照真模块的接口和异常来。

    - 读/写查对话是同步；send / confirm 是 async（真模块就是这样）。
    - 找不到对话、群不合法一律 ValueError；忙是 ChatBusy（ValueError 子类）。
    - confirm 回 {approved, ok, result}（真模块的形状），网页那层再翻成 {status, result}。
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.chats: dict[int, dict] = {
            1: {
                "id": 1,
                "title": "第一段对话",
                "group_id": G1,
                "group_name": "测试群",
                "updated": 1000.0,
                "running": False,
            }
        }
        self.messages: list[dict] = [
            {"id": 1, "role": "user", "content": "你好", "tool_calls": [], "name": "", "meta": {}},
            {"id": 2, "role": "assistant", "content": "在的", "tool_calls": [], "name": "", "meta": {}},
        ]
        self.pending: list[dict] = [
            {"id": 7, "status": "pending", "tool": "send_group_message", "args": {"text": "早安"}, "summary": "发到群里"}
        ]
        self.running = False
        self.busy: set[int] = set()  # is_busy 认这个（详情里没带 running 时的兜底）
        self.detail_afters: list[tuple[int, int]] = []
        self.send_result: object = 42
        self.send_error: Exception | None = None
        self.confirm_result: dict = {"approved": True, "ok": True, "result": "发出去了"}
        self.confirm_error: Exception | None = None

    # ---- 同步读写 ----

    def list_chats(self) -> list[dict]:
        self.calls.append(("list",))
        return [dict(c) for c in self.chats.values()]

    def create(self, group_id: str = "") -> dict:
        self.calls.append(("create", group_id))
        if group_id and group_id not in (G1, G2):
            raise ValueError(f"不是服务群：{group_id}")
        cid = max(self.chats) + 1
        chat = {"id": cid, "title": "", "group_id": group_id, "group_name": "", "updated": 2000.0, "running": False}
        self.chats[cid] = chat
        return dict(chat)

    def update(self, chat_id: int, *, title=None, group_id=None, archived=None) -> dict:
        self.calls.append(("update", int(chat_id), title, group_id, archived))
        chat = self.chats.get(int(chat_id))
        if chat is None:
            raise ValueError(f"找不到对话 {chat_id}")
        if group_id is not None and group_id and group_id not in (G1, G2):
            raise ValueError(f"不是服务群：{group_id}")
        if title is not None:
            if not str(title).strip():
                raise ValueError("标题不能是空的")
            if len(str(title)) > MAX_TITLE:
                raise ValueError(f"标题太长了（最多 {MAX_TITLE} 个字）")
            chat["title"] = str(title)
        if group_id is not None:
            chat["group_id"] = group_id
        if archived is not None:
            chat["archived"] = archived
        return dict(chat)

    def detail(self, chat_id: int, after: int = 0) -> dict:
        self.detail_afters.append((int(chat_id), int(after)))
        if int(chat_id) not in self.chats:
            raise ValueError(f"找不到对话 {chat_id}")
        return {
            "chat": dict(self.chats[int(chat_id)]),
            "running": bool(self.running),
            "messages": [dict(m) for m in self.messages if int(m["id"]) > int(after)],
            "pending": [dict(p) for p in self.pending],
        }

    def is_busy(self, chat_id: int) -> bool:
        return int(chat_id) in self.busy

    # ---- 异步（真模块的 send / confirm 就是 async） ----

    async def send(self, chat_id: int, text: str) -> int:
        self.calls.append(("send", int(chat_id), text))
        if self.send_error is not None:
            raise self.send_error
        if int(chat_id) not in self.chats:
            raise ValueError(f"找不到对话 {chat_id}")
        if len(text) > MAX_TEXT:
            raise ValueError(f"消息太长了（最多 {MAX_TEXT} 个字）")
        return self.send_result  # type: ignore[return-value]

    async def confirm(self, pending_id: int, approve: bool) -> dict:
        self.calls.append(("confirm", int(pending_id), bool(approve)))
        if self.confirm_error is not None:
            raise self.confirm_error
        if int(pending_id) == 999:
            raise ValueError(f"找不到待确认项 {pending_id}")
        return dict(self.confirm_result)


class _Settings:
    class console:
        password = "测试密码"

    class models:
        api_key = ""

    def is_served(self, gid: str) -> bool:
        return str(gid) in (G1, G2)


class _FakeSvc:
    def __init__(self, *, admin_chat: object, store: object, settings: object) -> None:
        self.admin_chat = admin_chat
        self.store = store
        self.get_settings = lambda: settings


class _Env:
    def __init__(self, svc: _FakeSvc, chat: _FakeAdminChat, client: TestClient, app) -> None:
        self.svc = svc
        self.chat = chat
        self.client = client
        self.app = app

    @property
    def member_headers(self) -> dict[str, str]:
        return {"X-MW-Group": TOKEN_G1}

    async def login_admin(self) -> None:
        auth = self.app[AUTH_KEY]
        value, _max_age = auth.make_cookie()
        self.client.session.cookie_jar.update_cookies({COOKIE_NAME: value})


@pytest_asyncio.fixture
async def env():
    chat = _FakeAdminChat()
    store = _FakeStore()
    svc = _FakeSvc(admin_chat=chat, store=store, settings=_Settings())
    app = create_app(svc)
    server = TestServer(app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    wrapper = _Env(svc=svc, chat=chat, client=client, app=app)
    try:
        yield wrapper
    finally:
        await client.close()


class _SettingsWithKey(_Settings):
    class models:
        api_key = SECRET


# ----------------------------------------------------------------------
# 鉴权 / 模块没开
# ----------------------------------------------------------------------


class TestAuth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path,body", ROUTES)
    async def test_anonymous_401(self, env: _Env, method: str, path: str, body) -> None:
        r = await env.client.request(method, path, json=body)
        assert r.status == 401, (method, path, r.status)
        assert (await r.json())["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path,body", ROUTES)
    async def test_member_403(self, env: _Env, method: str, path: str, body) -> None:
        r = await env.client.request(method, path, json=body, headers=env.member_headers)
        assert r.status == 403, (method, path, r.status)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path,body", ROUTES)
    async def test_module_missing_503(self, env: _Env, method: str, path: str, body) -> None:
        await env.login_admin()
        env.svc.admin_chat = None
        r = await env.client.request(method, path, json=body)
        assert r.status == 503, (method, path, r.status)
        assert (await r.json())["error"] == "这个功能还没开"


# ----------------------------------------------------------------------
# 列表 / 新建 / 修改
# ----------------------------------------------------------------------


class TestListCreateUpdate:
    @pytest.mark.asyncio
    async def test_list_wraps_chats(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.get("/api/chat")
        assert r.status == 200
        data = await r.json()
        assert set(data) == {"chats"}
        assert len(data["chats"]) == 1
        for key in ("id", "title", "group_id", "group_name", "updated", "running"):
            assert key in data["chats"][0], key

    @pytest.mark.asyncio
    async def test_create_passes_group_and_returns_chat(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat", json={"group_id": G1})
        assert r.status == 200
        data = await r.json()
        assert data["id"] not in (None, 0)
        assert data["group_id"] == G1
        assert ("create", G1) in env.chat.calls

    @pytest.mark.asyncio
    async def test_create_without_group_is_unscoped(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat", json={})
        assert r.status == 200
        assert (await r.json())["group_id"] == ""
        assert ("create", "") in env.chat.calls

    @pytest.mark.asyncio
    async def test_create_unserved_group_404(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat", json={"group_id": G3})
        assert r.status == 404
        assert (await r.json())["error"] != "没有这个接口"  # 路由真在，不是兜底 404
        assert not [c for c in env.chat.calls if c[0] == "create"]

    @pytest.mark.asyncio
    async def test_create_non_json_body_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat", json=["not", "a", "dict"])
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_patch_title_strips_and_forwards(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={"title": "  换个名字  "})
        assert r.status == 200
        assert (await r.json())["title"] == "换个名字"
        assert ("update", 1, "换个名字", None, None) in env.chat.calls

    @pytest.mark.asyncio
    async def test_patch_group_and_archived(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={"group_id": G2, "archived": True})
        assert r.status == 200
        data = await r.json()
        assert data["group_id"] == G2
        assert data["archived"] is True
        assert ("update", 1, None, G2, True) in env.chat.calls
        # 清空聚焦群：空字符串放行
        r2 = await env.client.patch("/api/chat/1", json={"group_id": ""})
        assert r2.status == 200
        assert ("update", 1, None, "", None) in env.chat.calls

    @pytest.mark.asyncio
    async def test_patch_unserved_group_404(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={"group_id": G3})
        assert r.status == 404
        assert (await r.json())["error"] != "没有这个接口"
        assert not [c for c in env.chat.calls if c[0] == "update"]

    @pytest.mark.asyncio
    async def test_patch_bad_archived_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={"archived": "yes"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_patch_unknown_chat_404(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/5", json={"title": "x"})
        assert r.status == 404
        assert (await r.json())["error"] != "没有这个接口"
        assert [c for c in env.chat.calls if c[0] == "update"]  # 真问了 admin_chat

    @pytest.mark.asyncio
    async def test_patch_bad_id_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/abc", json={"title": "x"})
        assert r.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("title", ["", "   "])
    async def test_patch_empty_title_400(self, env: _Env, title: str) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={"title": title})
        assert r.status == 400
        assert not [c for c in env.chat.calls if c[0] == "update"]

    @pytest.mark.asyncio
    async def test_patch_too_long_title_400(self, env: _Env) -> None:
        """对话在、只是标题太长（admin_chat 抛 ValueError）→ 400，不是 404。"""
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={"title": "长" * (MAX_TITLE + 1)})
        assert r.status == 400
        assert "太长" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_patch_no_field_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.patch("/api/chat/1", json={})
        assert r.status == 400
        assert not [c for c in env.chat.calls if c[0] == "update"]

    @pytest.mark.asyncio
    async def test_list_fills_running_from_is_busy(self, env: _Env) -> None:
        """admin_chat 的列表没带 running 时，用 is_busy 补上（侧栏小圆点靠它）。"""
        await env.login_admin()
        del env.chat.chats[1]["running"]
        env.chat.busy.add(1)
        data = await (await env.client.get("/api/chat")).json()
        assert data["chats"][0]["running"] is True


# ----------------------------------------------------------------------
# 详情 / 轮询
# ----------------------------------------------------------------------


class TestDetailAndPolling:
    @pytest.mark.asyncio
    async def test_detail_shape(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.get("/api/chat/1")
        assert r.status == 200
        data = await r.json()
        assert set(data) == {"chat", "running", "messages", "pending"}
        assert data["chat"]["id"] == 1
        assert data["running"] is False
        assert [m["id"] for m in data["messages"]] == [1, 2]
        assert data["pending"][0]["id"] == 7
        assert data["pending"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_detail_running_true(self, env: _Env) -> None:
        await env.login_admin()
        env.chat.running = True
        data = await (await env.client.get("/api/chat/1")).json()
        assert data["running"] is True

    @pytest.mark.asyncio
    async def test_detail_after_is_forwarded_and_filters(self, env: _Env) -> None:
        """轮询：带 ?after=最后一条 id → 只回新消息，after 原样给 admin_chat。"""
        await env.login_admin()
        first = await (await env.client.get("/api/chat/1")).json()
        last = first["messages"][-1]["id"]
        env.chat.messages.append({"id": last + 1, "role": "assistant", "content": "新的一句"})
        r = await env.client.get(f"/api/chat/1?after={last}")
        assert r.status == 200
        data = await r.json()
        assert env.chat.detail_afters[-1] == (1, last)
        assert [m["id"] for m in data["messages"]] == [last + 1]
        assert data["chat"]["id"] == 1  # 轮询也要带 chat（前端刷 info）

    @pytest.mark.asyncio
    async def test_detail_bad_after_falls_back_to_zero(self, env: _Env) -> None:
        await env.login_admin()
        for raw in ("abc", "-5", ""):
            r = await env.client.get(f"/api/chat/1?after={raw}")
            assert r.status == 200, raw
            assert env.chat.detail_afters[-1] == (1, 0), raw

    @pytest.mark.asyncio
    async def test_detail_flat_dict_still_gets_all_keys(self, env: _Env) -> None:
        """admin_chat 把 chat 字段铺在顶层也认：四个键一定齐。"""
        await env.login_admin()
        env.chat.detail = lambda chat_id, after=0: {
            "id": 1,
            "title": "第一段对话",
            "group_id": G1,
            "messages": [],
            "pending": [],
        }
        data = await (await env.client.get("/api/chat/1")).json()
        assert set(data) == {"chat", "running", "messages", "pending"}
        assert data["chat"]["id"] == 1
        assert data["chat"]["title"] == "第一段对话"

    @pytest.mark.asyncio
    async def test_detail_unknown_chat_404(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.get("/api/chat/5")
        assert r.status == 404
        assert (await r.json())["error"] != "没有这个接口"
        assert env.chat.detail_afters[-1] == (5, 0)

    @pytest.mark.asyncio
    async def test_detail_bad_id_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.get("/api/chat/abc")
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_detail_running_falls_back_to_is_busy(self, env: _Env) -> None:
        """admin_chat 的 detail 没带 running 时，用 is_busy 补上（前端靠它轮询）。"""
        await env.login_admin()
        env.chat.detail = lambda chat_id, after=0: {"chat": {"id": 1}, "messages": [], "pending": []}
        env.chat.busy.add(1)
        data = await (await env.client.get("/api/chat/1")).json()
        assert data["running"] is True


# ----------------------------------------------------------------------
# 发消息 / 确认
# ----------------------------------------------------------------------


class TestSendAndConfirm:
    @pytest.mark.asyncio
    async def test_send_202_with_user_message_id(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/1/messages", json={"text": "  在吗  "})
        assert r.status == 202
        assert await r.json() == {"accepted": True, "user_message_id": 42}
        assert ("send", 1, "在吗") in env.chat.calls  # 前后空白去掉

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    async def test_send_empty_text_400(self, env: _Env, text: str) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/1/messages", json={"text": text})
        assert r.status == 400
        assert not [c for c in env.chat.calls if c[0] == "send"]

    @pytest.mark.asyncio
    async def test_send_non_json_body_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/1/messages", json=["nope"])
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_send_busy_409(self, env: _Env) -> None:
        """ChatBusy 是 ValueError 子类，但网页要认成 409（不是 404）。"""
        await env.login_admin()
        env.chat.send_error = ChatBusy("上一句还在处理，等一下")
        r = await env.client.post("/api/chat/1/messages", json={"text": "喂"})
        assert r.status == 409
        assert "处理" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_send_too_long_text_400(self, env: _Env) -> None:
        """对话在、只是话太长（admin_chat 抛 ValueError）→ 400，不是 404。"""
        await env.login_admin()
        r = await env.client.post("/api/chat/1/messages", json={"text": "长" * (MAX_TEXT + 1)})
        assert r.status == 400
        assert "太长" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_send_unknown_chat_404(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/5/messages", json={"text": "喂"})
        assert r.status == 404
        assert (await r.json())["error"] != "没有这个接口"
        assert ("send", 5, "喂") in env.chat.calls

    @pytest.mark.asyncio
    async def test_send_bad_id_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/abc/messages", json={"text": "喂"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_confirm_approve_ok_maps_to_done(self, env: _Env) -> None:
        """admin_chat.confirm 回 {approved, ok, result}；网页那层翻成 {status, result}。"""
        await env.login_admin()
        r = await env.client.post("/api/chat/pending/7", json={"approve": True})
        assert r.status == 200
        assert await r.json() == {"status": "done", "result": "发出去了"}
        assert ("confirm", 7, True) in env.chat.calls

    @pytest.mark.asyncio
    async def test_confirm_approve_not_ok_maps_to_failed(self, env: _Env) -> None:
        await env.login_admin()
        env.chat.confirm_result = {"approved": True, "ok": False, "result": "群里发失败了"}
        r = await env.client.post("/api/chat/pending/7", json={"approve": True})
        assert r.status == 200
        assert await r.json() == {"status": "failed", "result": "群里发失败了"}

    @pytest.mark.asyncio
    async def test_confirm_reject_maps_to_rejected(self, env: _Env) -> None:
        await env.login_admin()
        env.chat.confirm_result = {"approved": False, "ok": True, "result": ""}
        r = await env.client.post("/api/chat/pending/7", json={"approve": False})
        assert r.status == 200
        assert await r.json() == {"status": "rejected", "result": ""}
        assert ("confirm", 7, False) in env.chat.calls

    @pytest.mark.asyncio
    async def test_confirm_standard_status_passes_through(self, env: _Env) -> None:
        await env.login_admin()
        env.chat.confirm_result = {"status": "done", "result": "已经做了"}
        assert await (await env.client.post("/api/chat/pending/7", json={"approve": True})).json() == {
            "status": "done",
            "result": "已经做了",
        }

    @pytest.mark.asyncio
    async def test_confirm_busy_409(self, env: _Env) -> None:
        await env.login_admin()
        env.chat.confirm_error = ChatBusy("上一句还在处理，等一下")
        r = await env.client.post("/api/chat/pending/7", json={"approve": True})
        assert r.status == 409

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"approve": "1"}, {"approve": None}, {"approve": 1}])
    async def test_confirm_non_bool_400(self, env: _Env, body: dict) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/pending/7", json=body)
        assert r.status == 400
        assert not [c for c in env.chat.calls if c[0] == "confirm"]

    @pytest.mark.asyncio
    async def test_confirm_already_decided_409_with_reason(self, env: _Env) -> None:
        """在两个页面各点一次同意：第二次要说「已经处理过了」，不是「不存在」。"""
        from CharTyr_MaiWork.maiwork.admin_chat import PendingDecided

        await env.login_admin()
        env.chat.confirm_error = PendingDecided("这条待确认动作已经处理过了（approved）")
        r = await env.client.post("/api/chat/pending/7", json={"approve": True})
        assert r.status == 409
        assert "已经处理过了" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_confirm_unknown_pending_404(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/pending/999", json={"approve": True})
        assert r.status == 404
        assert (await r.json())["error"] != "没有这个接口"
        assert ("confirm", 999, True) in env.chat.calls

    @pytest.mark.asyncio
    async def test_confirm_bad_id_400(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.post("/api/chat/pending/abc", json={"approve": True})
        assert r.status == 400


# ----------------------------------------------------------------------
# 同源（写接口）
# ----------------------------------------------------------------------


class TestOriginGuard:
    @pytest.mark.asyncio
    async def test_cross_origin_writes_403(self, env: _Env) -> None:
        await env.login_admin()
        evil = {"Origin": "http://evil.example.com:9999"}
        r = await env.client.post("/api/chat", json={"group_id": G1}, headers=evil)
        assert r.status == 403
        r2 = await env.client.patch("/api/chat/1", json={"title": "x"}, headers=evil)
        assert r2.status == 403
        r3 = await env.client.post("/api/chat/1/messages", json={"text": "hi"}, headers=evil)
        assert r3.status == 403
        r4 = await env.client.post("/api/chat/pending/7", json={"approve": True}, headers=evil)
        assert r4.status == 403
        # 一个写都没落到 admin_chat
        assert not [c for c in env.chat.calls if c[0] in ("create", "update", "send", "confirm")]

    @pytest.mark.asyncio
    async def test_same_origin_writes_pass(self, env: _Env) -> None:
        await env.login_admin()
        origin = str(env.client.make_url("/")).rstrip("/")
        r = await env.client.post("/api/chat/1/messages", json={"text": "hi"}, headers={"Origin": origin})
        assert r.status == 202
        r2 = await env.client.post("/api/chat/pending/7", json={"approve": True}, headers={"Origin": origin})
        assert r2.status == 200

    @pytest.mark.asyncio
    async def test_get_is_exempt_from_origin(self, env: _Env) -> None:
        await env.login_admin()
        r = await env.client.get("/api/chat/1", headers={"Origin": "http://evil.example.com:9999"})
        assert r.status == 200


# ----------------------------------------------------------------------
# 密钥不外泄
# ----------------------------------------------------------------------


class TestSecretsStayInside:
    @pytest.mark.asyncio
    async def test_secret_redacted_in_list_detail_and_confirm(self, env: _Env) -> None:
        await env.login_admin()
        env.svc.store.secrets = [SECRET, SK_LIKE]
        env.chat.chats[1]["title"] = f"关于 {SECRET}"
        env.chat.messages[0]["content"] = f"我记下了 {SECRET} 这个密钥"
        env.chat.messages[1]["content"] = f"还有 {SK_LIKE} 这种样子"
        env.chat.pending[0]["args"] = {"text": SECRET}
        env.chat.pending[0]["summary"] = f"要发：{SECRET}"
        env.chat.confirm_result = {"status": "failed", "result": f"没发出去：{SECRET}"}

        bodies = [
            await (await env.client.get("/api/chat")).text(),
            await (await env.client.get("/api/chat/1")).text(),
            await (await env.client.post("/api/chat/pending/7", json={"approve": True})).text(),
        ]
        for body in bodies:
            assert SECRET not in body
            assert SK_LIKE not in body
            assert "***" in body  # 遮了，不是整条丢掉

    @pytest.mark.asyncio
    async def test_api_key_never_in_chat_payload(self, env: _Env) -> None:
        await env.login_admin()
        env.svc.get_settings = lambda: _SettingsWithKey()
        r = await env.client.get("/api/chat")
        assert r.status == 200
        assert SECRET not in (await r.text())
