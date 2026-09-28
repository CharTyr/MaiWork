"""admin_chat.py 测试：管理员对话的核心循环（网页 → 主模型 → 管理员专属工具 → 回话）。

覆盖的红线（AGENTS + 02 设计）：
- 只有服务群能当对话焦点（未授权群号一律 ValueError，零写入）；
- 模型只拿到 `tools.specs("admin")`（工具层注册的 admin 角色），别的角色工具调不动；
- assistant(tool_calls) → tool 的消息顺序严格按 OpenAI 规矩落库、按同样顺序回放；
- tool 行 meta={ok, tool, label}，tool_call_id / name 齐全；
- 同对话并发 → ChatBusy（ValueError 子类，网页回 409）；
- 模型没配好 / 调用失败 / 轮数用尽 → 留 system_note；
- 密钥（已知密钥、Bearer、sk- 形式）不进对话、不进库；
- 待确认小票用 app.admin_pending.execute(...) 执行/作废，结果记 system_note 并继续模型回答；
- 后台跑：send 立刻返回用户消息 id，关网页不影响这轮。

测试里的假对象都自己造（真 Store / 真 Tools / 真 Identity），不依赖宿主与网络。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat, ChatBusy
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.models import ChatResult, ModelError
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolContext, ToolResult, Tools

G1 = "900000001"
G2 = "123456789"
SECRET = "sk-live-secret-9527"


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class _Ready:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    def ready(self) -> bool:
        return self._ready


def _res(text: str = "", tool_calls: list[dict] | None = None) -> ChatResult:
    return ChatResult(
        text=text,
        tool_calls=list(tool_calls or []),
        model="fake-main",
        prompt_tokens=1,
        completion_tokens=1,
        raw_message={},
    )


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class _Models:
    """假 Models：脚本队列（ChatResult / 异常 / 可调用），记录每次 chat 的入参。"""

    def __init__(self, ready: bool = True, script: list[Any] | None = None) -> None:
        self._ready = ready
        self.script: list[Any] = list(script or [])
        self.calls: list[tuple[str, list[dict], dict]] = []
        self.gate: asyncio.Event | None = None

    def settings(self) -> Any:
        return _Ready(self._ready)

    async def chat(self, role: str, messages: list[dict], **kwargs: Any) -> ChatResult:
        self.calls.append((role, [dict(m) for m in messages], dict(kwargs)))
        if self.gate is not None:
            await self.gate.wait()
        item: Any = self.script.pop(0) if self.script else _res("（默认回答）")
        if callable(item) and not isinstance(item, ChatResult):
            item = item()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return _res(item)
        return item


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": "127.0.0.1:18699", "password": "x", "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": SECRET, "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


class _Outbox:
    def __init__(self) -> None:
        self.items: list[tuple] = []

    def enqueue(self, key: str, group_id: str, kind: str, payload: dict, task_id: Any = None) -> int:
        self.items.append((key, group_id, kind, payload, task_id))
        return len(self.items)

    async def flush(self, now: float = 0.0) -> None:
        return None


class _Gate:
    """假待确认门闸（签名与 tools_admin.PendingGate 一致）。"""

    def __init__(self, result: ToolResult | None = None) -> None:
        self.result = result or ToolResult(ok=True, output=f"已把这句话发给群 {G1}")
        self.calls: list[tuple[int, bool]] = []
        self.bound: list[tuple[int, int]] = []

    def bind_chat(self, chat_id: Any, msg_id: Any = 0) -> None:
        self.bound.append((int(chat_id or 0), int(msg_id or 0)))

    def pending(self, chat_id: Any = None) -> list[dict]:
        rows = self._store.read().execute(
            "SELECT * FROM admin_chat_pending WHERE status='pending' AND chat_id=? ORDER BY id",
            (int(chat_id or 0),),
        ).fetchall()
        return [dict(r) for r in rows]

    async def execute(self, pending_id: Any, approve: bool = True) -> ToolResult:
        self.calls.append((int(pending_id), bool(approve)))
        return self.result


class _Svc:
    """最小「app」：真 Store / 真 Tools / 真 Identity + 假模型、假门闸、假发件箱。"""

    def __init__(self, tmp_path: Path, models: _Models | None = None, *, spawn_bg: bool = True) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._settings, _ = load_settings(_raw_config(data_dir))
        self.store = Store(data_dir / "maiwork.db")
        self.store.migrate()
        self.models = models or _Models()
        self.tools = Tools(self.store)
        self.identity: Any = Identity(data_dir, self.store, self.get_settings)
        self.outbox = _Outbox()
        self.admin_pending: Any = _Gate()
        self.spawned: list[str] = []
        self.bg_jobs: set[asyncio.Task] = set()
        self.tool_ctx: list[dict] = []
        self.boom_called = False
        self.main_only_called = False
        if not spawn_bg:
            self._spawn_bg = None  # type: ignore[assignment]
        now = clock.now()
        with self.store.tx() as conn:
            for gid in (G1, G2):
                conn.execute(
                    "INSERT INTO groups (group_id, name, workspace, created) VALUES (?, ?, 'tinker', ?)"
                    " ON CONFLICT(group_id) DO NOTHING",
                    (gid, f"测试群{gid}", now),
                )
        self._register_stub_tools()

    def get_settings(self) -> Any:
        return self._settings

    def _spawn_bg(self, awaitable: Any, *, name: str = "maiwork-bg") -> None:
        self.spawned.append(name)
        task = asyncio.ensure_future(awaitable)
        self.bg_jobs.add(task)
        task.add_done_callback(self.bg_jobs.discard)

    def _register_stub_tools(self) -> None:
        svc = self

        async def read_it(ctx: ToolContext, args: dict) -> ToolResult:
            svc.tool_ctx.append(
                {
                    "group_id": str(getattr(ctx, "group_id", "")),
                    "chat_id": int(getattr(ctx, "chat_id", 0) or 0),
                    "msg_id": int(getattr(ctx, "msg_id", 0) or 0),
                    "role": str(getattr(ctx, "role", "")),
                    "args": dict(args),
                }
            )
            return ToolResult(ok=True, output="读到了：三条画像", data={"n": 3})

        async def boom(_ctx: ToolContext, _args: dict) -> ToolResult:
            svc.boom_called = True
            raise RuntimeError(f"内部炸了，密钥 {SECRET}")

        async def main_only(_ctx: ToolContext, _args: dict) -> ToolResult:
            svc.main_only_called = True
            return ToolResult(ok=True, output="主模型专用工具跑了")

        self.tools.register(
            Tool(
                name="stub_read",
                description="读一段服务群的东西（带编号）。",
                parameters={"type": "object", "properties": {"group_id": {"type": "string"}}},
                roles=frozenset({"admin"}),
                handler=read_it,
            )
        )
        self.tools.register(
            Tool(
                name="stub_boom",
                description="故意炸的工具。",
                parameters={"type": "object", "properties": {}},
                roles=frozenset({"admin"}),
                handler=boom,
            )
        )
        self.tools.register(
            Tool(
                name="stub_main_only",
                description="只有主模型能用的工具。",
                parameters={"type": "object", "properties": {}},
                roles=frozenset({"main"}),
                handler=main_only,
            )
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path: Path) -> _Svc:
    return _Svc(tmp_path)


def _chat(svc: _Svc, group_id: str = G1) -> tuple[AdminChat, int]:
    chat = AdminChat(svc)
    cid = int(chat.create(group_id=group_id)["id"])
    return chat, cid


def _msgs(svc: _Svc, cid: int) -> list[dict]:
    rows = svc.store.read().execute(
        "SELECT * FROM admin_chat_msgs WHERE chat_id=? ORDER BY id", (cid,)
    ).fetchall()
    return [dict(r) for r in rows]


def _seed_pending(svc: _Svc, cid: int, *, tool: str = "send_group_message", summary: str = "发到群：你好") -> int:
    with svc.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO admin_chat_pending (chat_id, msg_id, tool, args, summary, status, created)"
            " VALUES (?, 0, ?, ?, ?, 'pending', ?)",
            (cid, tool, json.dumps({"text": "你好"}, ensure_ascii=False), summary, clock.now()),
        )
        return int(cur.lastrowid or 0)


def _seed_msgs(svc: _Svc, cid: int, n: int) -> None:
    now = clock.now()
    with svc.store.tx() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content) VALUES (?, ?, ?, ?)",
                (cid, now + i, "user" if i % 2 == 0 else "assistant", f"旧消息{i}"),
            )


# ---------------------------------------------------------------------------
# 对话的增删改查
# ---------------------------------------------------------------------------


class TestChats:
    def test_list_chats_empty(self, svc: _Svc) -> None:
        assert AdminChat(svc).list_chats() == []

    def test_create_defaults_and_listed(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        got = chat.create()
        assert int(got["id"]) > 0
        assert got["group_id"] == ""
        assert got["archived"] is False
        assert got["title"]
        listed = chat.list_chats()
        assert [int(c["id"]) for c in listed] == [int(got["id"])]

    def test_create_focus_served_group(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        got = chat.create(group_id=G1)
        assert got["group_id"] == G1
        assert got["group_name"] == f"测试群{G1}"

    def test_create_rejects_non_served_group(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        with pytest.raises(ValueError):
            chat.create(group_id="99999")
        assert chat.list_chats() == []

    def test_update_title_group_archived(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        got = chat.update(cid, title="  改过的标题  ")
        assert got["title"] == "改过的标题"
        assert got["group_id"] == G1  # 没传的不动
        got = chat.update(cid, group_id=G2)
        assert got["group_id"] == G2
        got = chat.update(cid, group_id="")
        assert got["group_id"] == ""
        got = chat.update(cid, archived=True)
        assert got["archived"] is True
        assert chat.list_chats() == []
        got = chat.update(cid, archived=False)
        assert got["archived"] is False
        assert len(chat.list_chats()) == 1

    def test_update_rejects_non_served_group(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        with pytest.raises(ValueError):
            chat.update(cid, group_id="99999")
        assert chat.detail(cid)["chat"]["group_id"] == G1

    def test_update_rejects_empty_title(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        cid = int(chat.create()["id"])
        with pytest.raises(ValueError):
            chat.update(cid, title="   ")

    def test_update_unknown_chat(self, svc: _Svc) -> None:
        with pytest.raises(ValueError):
            AdminChat(svc).update(4242, title="x")

    def test_detail_unknown_chat(self, svc: _Svc) -> None:
        with pytest.raises(ValueError):
            AdminChat(svc).detail(4242)

    def test_detail_messages_and_after_paging(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        _seed_msgs(svc, cid, 5)
        got = chat.detail(cid)
        assert len(got["messages"]) == 5
        assert got["chat"]["id"] == cid
        first_id = int(got["messages"][0]["id"])
        later = chat.detail(cid, after=first_id)
        assert [int(m["id"]) for m in later["messages"]] == [
            int(m["id"]) for m in got["messages"][1:]
        ]
        assert chat.detail(cid, after=int(got["messages"][-1]["id"]))["messages"] == []

    def test_detail_message_view_fields(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content, tool_calls, tool_call_id, name, meta)"
                " VALUES (?, ?, 'tool', '读到了', '', 'c1', 'stub_read', ?)",
                (
                    cid,
                    clock.now(),
                    json.dumps({"ok": True, "tool": "stub_read", "label": "读东西"}, ensure_ascii=False),
                ),
            )
        msg = chat.detail(cid)["messages"][0]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "c1"
        assert msg["name"] == "stub_read"
        assert msg["meta"]["ok"] is True
        assert msg["meta"]["label"] == "读东西"
        assert msg["tool_calls"] == []

    def test_detail_includes_pending(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        pid = _seed_pending(svc, cid)
        got = chat.detail(cid)
        assert [int(p["id"]) for p in got["pending"]] == [pid]
        assert got["pending"][0]["tool"] == "send_group_message"


# ---------------------------------------------------------------------------
# send：后台跑、并发 409、模型入参
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_send_returns_user_msg_id_and_stores(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        mid = await chat.send(cid, "帮我看下群里最近聊了啥")
        assert isinstance(mid, int) and mid > 0
        rows = _msgs(svc, cid)
        assert [(r["role"], r["content"]) for r in rows[:1]] == [("user", "帮我看下群里最近聊了啥")]
        assert int(rows[0]["id"]) == mid
        assert await chat.wait_idle(cid)
        assert [r["role"] for r in _msgs(svc, cid)] == ["user", "assistant"]
        assert _msgs(svc, cid)[1]["content"] == "（默认回答）"

    @pytest.mark.asyncio
    async def test_first_message_becomes_title_unless_renamed(self, svc: _Svc) -> None:
        """侧栏里好几段对话不能都叫「xx｜管理员对话」：第一句话自动当标题；手改过的不动。"""
        chat, cid = _chat(svc)
        await chat.send(cid, "  帮我看下这周群里\n都聊了些什么，顺便列一下有没有人提到 NAS 的事情  ")
        assert await chat.wait_idle(cid)
        title = chat.detail(cid)["chat"]["title"]
        assert title.startswith("帮我看下这周群里 都聊了些什么")
        assert "\n" not in title and len(title) <= 24
        await chat.send(cid, "第二句不改标题")
        assert await chat.wait_idle(cid)
        assert chat.detail(cid)["chat"]["title"] == title
        chat2, cid2 = _chat(svc)
        chat2.update(cid2, title="我起的名字")
        await chat2.send(cid2, "这句不该变成标题")
        assert await chat2.wait_idle(cid2)
        assert chat2.detail(cid2)["chat"]["title"] == "我起的名字"

    @pytest.mark.asyncio
    async def test_send_empty_and_unknown_rejected(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        with pytest.raises(ValueError):
            await chat.send(cid, "   ")
        with pytest.raises(ValueError):
            await chat.send(4242, "你好")
        with pytest.raises(ValueError):
            await chat.send(cid, "长" * 20001)
        assert _msgs(svc, cid) == []
        assert svc.models.calls == []

    @pytest.mark.asyncio
    async def test_send_returns_before_model_answers(self, svc: _Svc) -> None:
        """send 立刻返回：模型那一轮在后台，网页关了也照跑。"""
        gate = asyncio.Event()
        svc.models.gate = gate
        chat, cid = _chat(svc)
        mid = await chat.send(cid, "慢慢答，不着急")
        assert isinstance(mid, int)
        assert chat.is_busy(cid) is True
        assert [r["role"] for r in _msgs(svc, cid)] == ["user"]  # 回答还没落库
        gate.set()
        assert await chat.wait_idle(cid)
        assert chat.is_busy(cid) is False
        assert [r["role"] for r in _msgs(svc, cid)] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_concurrent_send_is_chat_busy_409(self, svc: _Svc) -> None:
        gate = asyncio.Event()
        svc.models.gate = gate
        chat, cid = _chat(svc)
        await chat.send(cid, "第一句")
        with pytest.raises(ChatBusy) as e:
            await chat.send(cid, "第二句")
        assert isinstance(e.value, ValueError)  # 网页按 ValueError 回 409
        assert [r["content"] for r in _msgs(svc, cid)] == ["第一句"]  # 被拒的那句没落库
        gate.set()
        assert await chat.wait_idle(cid)
        # 忙完能接着发
        await chat.send(cid, "第三句")
        assert await chat.wait_idle(cid)

    @pytest.mark.asyncio
    async def test_send_uses_admin_specs_and_purpose(self, svc: _Svc) -> None:
        chat, cid = _chat(svc, G1)
        await chat.send(cid, "你好")
        assert await chat.wait_idle(cid)
        role, _messages, kwargs = svc.models.calls[0]
        assert role == "main"
        assert kwargs["purpose"] == "admin_chat"
        assert kwargs["group_id"] == G1
        offered = {t["function"]["name"] for t in kwargs["tools"]}
        admin_specs = {t["function"]["name"] for t in svc.tools.specs("admin")}
        assert offered == admin_specs
        assert "stub_main_only" not in offered

    @pytest.mark.asyncio
    async def test_send_without_focus_passes_empty_group(self, svc: _Svc) -> None:
        chat = AdminChat(svc)
        cid = int(chat.create()["id"])
        await chat.send(cid, "没聚焦群")
        assert await chat.wait_idle(cid)
        assert svc.models.calls[0][2]["group_id"] == ""

    @pytest.mark.asyncio
    async def test_spawn_registered_for_app_shutdown(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        await chat.send(cid, "你好")
        assert await chat.wait_idle(cid)
        assert svc.spawned and f"maiwork-admin-chat-{cid}" in svc.spawned[0]

    @pytest.mark.asyncio
    async def test_works_without_spawn_bg(self, tmp_path: Path) -> None:
        """app 没有 _spawn_bg（接线还没到）时自己起任务，功能不丢。"""
        svc = _Svc(tmp_path, spawn_bg=False)
        chat, cid = _chat(svc)
        await chat.send(cid, "你好")
        assert await chat.wait_idle(cid)
        assert [r["role"] for r in _msgs(svc, cid)] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_close_cancels_running_turn(self, svc: _Svc) -> None:
        """插件 stop 时 close()：把在跑的轮次取消掉、忙标记清干净，不留野任务。"""
        gate = asyncio.Event()
        svc.models.gate = gate
        chat, cid = _chat(svc)
        await chat.send(cid, "跑着")
        assert chat.is_busy(cid) is True
        await chat.close()
        assert chat.is_busy(cid) is False
        assert chat._jobs == set()
        await chat.close()  # 重复调不炸

    @pytest.mark.asyncio
    async def test_cancel_all_is_synchronous(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path, spawn_bg=False)
        gate = asyncio.Event()
        svc.models.gate = gate
        chat, cid = _chat(svc)
        await chat.send(cid, "跑着")
        chat.cancel_all()
        assert chat.is_busy(cid) is False
        await asyncio.sleep(0)  # 让取消落地，别留 pending task 警告


# ---------------------------------------------------------------------------
# 模型没配好 / 调用失败 / 轮数上限
# ---------------------------------------------------------------------------


class TestFailures:
    @pytest.mark.asyncio
    async def test_model_not_ready_writes_system_note(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path, _Models(ready=False))
        chat, cid = _chat(svc)
        await chat.send(cid, "在吗")
        assert await chat.wait_idle(cid)
        rows = _msgs(svc, cid)
        assert [r["role"] for r in rows] == ["user", "system_note"]
        assert "模型" in rows[1]["content"]
        assert svc.models.calls == []  # 没配好就不叫模型

    @pytest.mark.asyncio
    async def test_model_error_writes_masked_system_note(self, svc: _Svc) -> None:
        svc.models.script = [ModelError(f"调用被拒：key={SECRET} Bearer abcdefghijklmn")]
        chat, cid = _chat(svc)
        await chat.send(cid, "在吗")
        assert await chat.wait_idle(cid)
        note = _msgs(svc, cid)[-1]
        assert note["role"] == "system_note"
        assert SECRET not in note["content"]
        assert "Bearer ***" in note["content"]
        rows = svc.store.read().execute("SELECT content FROM admin_chat_msgs").fetchall()
        assert all(SECRET not in str(r["content"]) for r in rows)

    @pytest.mark.asyncio
    async def test_unexpected_model_crash_writes_note(self, svc: _Svc) -> None:
        svc.models.script = [RuntimeError("连接断了")]
        chat, cid = _chat(svc)
        await chat.send(cid, "在吗")
        assert await chat.wait_idle(cid)
        note = _msgs(svc, cid)[-1]
        assert note["role"] == "system_note"
        assert "连接断了" in note["content"]

    @pytest.mark.asyncio
    async def test_round_limit_is_12(self, svc: _Svc) -> None:
        svc.models.script = [_res("", [_call("stub_read", {}, f"c{i}")]) for i in range(20)]
        chat, cid = _chat(svc)
        await chat.send(cid, "一直调工具")
        assert await chat.wait_idle(cid)
        assert len(svc.models.calls) == 12
        note = _msgs(svc, cid)[-1]
        assert note["role"] == "system_note"
        assert "12" in note["content"]

    @pytest.mark.asyncio
    async def test_context_window_keeps_last_40(self, svc: _Svc) -> None:
        """0.4.0 起不再按条数丢：60 条都送（还没到 128000 触发线；压缩由
        _turn 里估算超线时自动整理，test_admin_chat_compact.py 另测）。"""
        chat, cid = _chat(svc)
        _seed_msgs(svc, cid, 60)
        await chat.send(cid, "最新一句")
        assert await chat.wait_idle(cid)
        _role, messages, _kw = svc.models.calls[0]
        assert messages[0]["role"] == "system"
        assert len(messages) == 62  # 1 条 system + 60 条旧消息 + 新发的这一句（0.4.0 起不按条数丢）
        assert messages[-1] == {"role": "user", "content": "最新一句"}


# ---------------------------------------------------------------------------
# 工具轮：顺序、meta、角色门、动态 chat_id
# ---------------------------------------------------------------------------


class TestToolLoop:
    @pytest.mark.asyncio
    async def test_message_order_and_meta(self, svc: _Svc) -> None:
        svc.models.script = [
            _res("", [_call("stub_read", {"group_id": G1}, "c1"), _call("stub_read", {}, "c2")]),
            _res("两件事都看完了"),
        ]
        chat, cid = _chat(svc)
        await chat.send(cid, "看两样东西")
        assert await chat.wait_idle(cid)
        rows = _msgs(svc, cid)
        assert [r["role"] for r in rows] == ["user", "assistant", "tool", "tool", "assistant"]
        calls = json.loads(rows[1]["tool_calls"])
        assert [c["id"] for c in calls] == ["c1", "c2"]
        assert rows[1]["content"] == ""
        for row, cid_ in zip(rows[2:4], ("c1", "c2")):
            assert row["tool_call_id"] == cid_
            assert row["name"] == "stub_read"
            meta = json.loads(row["meta"])
            assert meta["ok"] is True
            assert meta["tool"] == "stub_read"
            assert meta["label"]
            assert "读" in meta["label"]
        assert rows[4]["content"] == "两件事都看完了"

    @pytest.mark.asyncio
    async def test_model_sees_openai_ordered_history(self, svc: _Svc) -> None:
        svc.models.script = [
            _res("", [_call("stub_read", {}, "c1"), _call("stub_read", {}, "c2")]),
            _res("好了"),
        ]
        chat, cid = _chat(svc)
        await chat.send(cid, "看东西")
        assert await chat.wait_idle(cid)
        _role, messages, _kw = svc.models.calls[1]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool"]
        assert messages[2]["tool_calls"][0]["id"] == "c1"
        assert messages[2]["content"] == ""
        assert [m["tool_call_id"] for m in messages[3:5]] == ["c1", "c2"]
        assert [m["name"] for m in messages[3:5]] == ["stub_read", "stub_read"]

    @pytest.mark.asyncio
    async def test_ctx_carries_chat_and_msg_id(self, svc: _Svc) -> None:
        """ToolContext 没 chat_id 字段 → 动态挂上（工具层/门闸要靠它归属对话）。"""
        svc.models.script = [_res("", [_call("stub_read", {}, "c1")]), _res("好")]
        chat, cid = _chat(svc, G1)
        await chat.send(cid, "看东西")
        assert await chat.wait_idle(cid)
        assert len(svc.tool_ctx) == 1
        got = svc.tool_ctx[0]
        assert got["chat_id"] == cid
        assert got["role"] == "admin"
        assert got["group_id"] == G1
        assert got["msg_id"] == int(_msgs(svc, cid)[1]["id"])  # 就是那条 assistant(tool_calls)

    @pytest.mark.asyncio
    async def test_non_admin_tool_call_blocked_by_tool_layer(self, svc: _Svc) -> None:
        svc.models.script = [_res("", [_call("stub_main_only", {}, "c1")]), _res("知道了")]
        chat, cid = _chat(svc)
        await chat.send(cid, "调个别的角色的工具")
        assert await chat.wait_idle(cid)
        assert svc.main_only_called is False  # 工具层挡住，handler 没跑
        tool_row = _msgs(svc, cid)[2]
        assert tool_row["role"] == "tool"
        assert json.loads(tool_row["meta"])["ok"] is False
        assert "不允许" in tool_row["content"]

    @pytest.mark.asyncio
    async def test_tool_error_masked_in_conversation(self, svc: _Svc) -> None:
        svc.models.script = [_res("", [_call("stub_boom", {}, "c1")]), _res("知道了")]
        chat, cid = _chat(svc)
        await chat.send(cid, "让它炸")
        assert await chat.wait_idle(cid)
        assert svc.boom_called is True
        tool_row = _msgs(svc, cid)[2]
        assert json.loads(tool_row["meta"])["ok"] is False
        assert SECRET not in tool_row["content"]
        assert "***" in tool_row["content"]
        # 进模型的那份也遮了
        _role, messages, _kw = svc.models.calls[1]
        assert SECRET not in json.dumps(messages, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_unknown_tool_name_gets_error_tool_message(self, svc: _Svc) -> None:
        svc.models.script = [_res("", [_call("no_such_tool", {}, "c1")]), _res("哦")]
        chat, cid = _chat(svc)
        await chat.send(cid, "调个不存在的")
        assert await chat.wait_idle(cid)
        tool_row = _msgs(svc, cid)[2]
        assert json.loads(tool_row["meta"])["ok"] is False
        assert tool_row["content"]


# ---------------------------------------------------------------------------
# system 提示：身份 / 权限边界
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    def _seed_identity(self, svc: _Svc) -> None:
        assert svc.identity is not None
        svc.identity.write("soul", "SOUL：我是 MaiWork 的脑子。")
        svc.identity.write("agents", "AGENTS：先查证据再动手。")
        svc.identity.write("memory", "全局记忆：管理员喜欢短句。")
        svc.identity.group_write(G1, f"本群记忆：{G1} 的人在折腾小工具。")

    @pytest.mark.asyncio
    async def test_prompt_has_identity_and_boundaries(self, svc: _Svc) -> None:
        self._seed_identity(svc)
        chat, cid = _chat(svc, G1)
        await chat.send(cid, "你好")
        assert await chat.wait_idle(cid)
        system = svc.models.calls[0][1][0]
        assert system["role"] == "system"
        for frag in ("SOUL：我是 MaiWork 的脑子。", "AGENTS：先查证据再动手。", "全局记忆", "本群记忆"):
            assert frag in system["content"], frag
        for frag in ("服务群", "管理员", "确认"):
            assert frag in system["content"], frag
        assert G1 in system["content"]

    @pytest.mark.asyncio
    async def test_prompt_without_focus_has_only_global_memory(self, svc: _Svc) -> None:
        self._seed_identity(svc)
        chat = AdminChat(svc)
        cid = int(chat.create()["id"])
        await chat.send(cid, "你好")
        assert await chat.wait_idle(cid)
        system = svc.models.calls[0][1][0]["content"]
        assert "全局记忆" in system
        assert "本群记忆" not in system

    @pytest.mark.asyncio
    async def test_prompt_works_without_identity(self, svc: _Svc) -> None:
        svc.identity = None
        chat, cid = _chat(svc)
        await chat.send(cid, "你好")
        assert await chat.wait_idle(cid)
        assert "服务群" in svc.models.calls[0][1][0]["content"]


# ---------------------------------------------------------------------------
# confirm：小票的同意 / 拒绝
# ---------------------------------------------------------------------------


class TestConfirm:
    @pytest.mark.asyncio
    async def test_approve_executes_then_notes_and_continues(self, svc: _Svc) -> None:
        chat, cid = _chat(svc, G1)
        pid = _seed_pending(svc, cid)
        out = await chat.confirm(pid, True)
        assert svc.admin_pending.calls == [(pid, True)]
        assert out["approved"] is True
        assert out["ok"] is True
        assert out["result"] == out["output"]  # console 读 result / output 都认
        assert await chat.wait_idle(cid)
        rows = _msgs(svc, cid)
        assert [r["role"] for r in rows] == ["system_note", "assistant"]
        assert "已确认" in rows[0]["content"]
        assert f"已把这句话发给群 {G1}" in rows[0]["content"]
        # 继续问了模型，而且模型看到了这条系统提示
        assert len(svc.models.calls) == 1
        assert "已确认" in json.dumps(svc.models.calls[0][1], ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_note_uses_friendly_label_not_tool_name(self, svc: _Svc) -> None:
        """网页上的系统提示给人看：用工具描述的短标签，不露英文工具名。"""
        chat, cid = _chat(svc, G1)
        pid = _seed_pending(svc, cid, tool="stub_read")
        await chat.confirm(pid, True)
        assert await chat.wait_idle(cid)
        note = _msgs(svc, cid)[0]["content"]
        assert "读一段服务群的东西" in note
        assert "stub_read" not in note

    @pytest.mark.asyncio
    async def test_decided_pending_raises_pending_decided(self, svc: _Svc) -> None:
        from CharTyr_MaiWork.maiwork.admin_chat import PendingDecided

        chat, cid = _chat(svc, G1)
        pid = _seed_pending(svc, cid)
        with svc.store.tx() as conn:
            conn.execute("UPDATE admin_chat_pending SET status='approved' WHERE id=?", (pid,))
        with pytest.raises(PendingDecided):
            await chat.confirm(pid, True)

    @pytest.mark.asyncio
    async def test_reject_notes_and_continues(self, svc: _Svc) -> None:
        svc.admin_pending.result = ToolResult(ok=True, output="管理员没有同意，这次动作已取消")
        chat, cid = _chat(svc)
        pid = _seed_pending(svc, cid)
        out = await chat.confirm(pid, False)
        assert svc.admin_pending.calls == [(pid, False)]
        assert out["approved"] is False
        assert await chat.wait_idle(cid)
        note = _msgs(svc, cid)[0]
        assert note["role"] == "system_note"
        assert "拒绝" in note["content"] or "没有同意" in note["content"]
        assert svc.models.calls  # 也继续回答

    @pytest.mark.asyncio
    async def test_confirm_unknown_pending(self, svc: _Svc) -> None:
        chat, _cid = _chat(svc)
        with pytest.raises(ValueError):
            await chat.confirm(9999, True)
        assert svc.admin_pending.calls == []

    @pytest.mark.asyncio
    async def test_confirm_twice_rejected(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        pid = _seed_pending(svc, cid)
        with svc.store.tx() as conn:
            conn.execute("UPDATE admin_chat_pending SET status='rejected' WHERE id=?", (pid,))
        with pytest.raises(ValueError):
            await chat.confirm(pid, True)
        assert svc.admin_pending.calls == []

    @pytest.mark.asyncio
    async def test_confirm_without_gate(self, svc: _Svc) -> None:
        svc.admin_pending = None
        chat, cid = _chat(svc)
        pid = _seed_pending(svc, cid)
        with pytest.raises(ValueError):
            await chat.confirm(pid, True)
        assert chat.is_busy(cid) is False  # 出错也要把忙标记清掉

    @pytest.mark.asyncio
    async def test_confirm_busy_raises(self, svc: _Svc) -> None:
        gate = asyncio.Event()
        svc.models.gate = gate
        chat, cid = _chat(svc)
        pid = _seed_pending(svc, cid)
        await chat.send(cid, "先说一句")
        with pytest.raises(ChatBusy):
            await chat.confirm(pid, True)
        assert svc.admin_pending.calls == []
        gate.set()
        assert await chat.wait_idle(cid)


# ---------------------------------------------------------------------------
# 和真 tools_admin.PendingGate 对接（签名 + 越界 + 真执行）
# ---------------------------------------------------------------------------


class TestRealGate:
    def _wire(self, svc: _Svc) -> None:
        from CharTyr_MaiWork.maiwork.tools_admin import register_admin_tools

        svc.admin_pending = register_admin_tools(svc.tools, svc)

    @pytest.mark.asyncio
    async def test_real_gate_binds_chat_and_sends_after_confirm(self, svc: _Svc) -> None:
        self._wire(svc)
        svc.models.script = [
            _res("", [_call("send_group_message", {"group_id": G1, "text": "大家好"}, "c1")]),
            _res("已经请管理员确认了"),
        ]
        chat, cid = _chat(svc, G1)
        await chat.send(cid, f"跟群 {G1} 说句话")
        assert await chat.wait_idle(cid)
        row = svc.store.read().execute("SELECT * FROM admin_chat_pending").fetchone()
        assert row is not None
        assert int(row["chat_id"]) == cid  # bind_chat 生效
        assert int(row["msg_id"]) == int(_msgs(svc, cid)[1]["id"])
        assert str(row["status"]) == "pending"
        assert svc.outbox.items == []  # 没确认前不发
        assert "_approved" not in str(row["args"])  # 主循环绝不自己盖「已批准」的章
        tool_row = _msgs(svc, cid)[2]
        assert json.loads(tool_row["meta"])["ok"] is True
        assert "已请求管理员确认" in tool_row["content"]

        out = await chat.confirm(int(row["id"]), True)
        assert out["ok"] is True
        assert await chat.wait_idle(cid)
        assert len(svc.outbox.items) == 1
        _key, gid, kind, payload, _task = svc.outbox.items[0]
        assert gid == G1 and kind == "text" and payload["text"] == "大家好"
        row2 = svc.store.read().execute("SELECT status FROM admin_chat_pending").fetchone()
        assert str(row2["status"]) == "approved"
        assert "已确认" in _msgs(svc, cid)[-2]["content"]

    @pytest.mark.asyncio
    async def test_real_gate_reject_keeps_outbox_empty(self, svc: _Svc) -> None:
        self._wire(svc)
        svc.models.script = [
            _res("", [_call("send_group_message", {"group_id": G1, "text": "别发"}, "c1")]),
            _res("好"),
        ]
        chat, cid = _chat(svc, G1)
        await chat.send(cid, "说句话")
        assert await chat.wait_idle(cid)
        pid = int(svc.store.read().execute("SELECT id FROM admin_chat_pending").fetchone()["id"])
        await chat.confirm(pid, False)
        assert await chat.wait_idle(cid)
        assert svc.outbox.items == []
        row = svc.store.read().execute("SELECT status FROM admin_chat_pending").fetchone()
        assert str(row["status"]) == "rejected"

    @pytest.mark.asyncio
    async def test_real_gate_blocks_non_served_group(self, svc: _Svc) -> None:
        self._wire(svc)
        svc.models.script = [
            _res("", [_call("send_group_message", {"group_id": "99999", "text": "越界"}, "c1")]),
            _res("知道了"),
        ]
        chat, cid = _chat(svc, G1)
        await chat.send(cid, "发到 99999")
        assert await chat.wait_idle(cid)
        tool_row = _msgs(svc, cid)[2]
        assert json.loads(tool_row["meta"])["ok"] is False
        assert "服务群" in tool_row["content"]
        assert svc.outbox.items == []
        count = svc.store.read().execute(
            "SELECT COUNT(*) AS c FROM admin_chat_pending"
        ).fetchone()["c"]
        assert int(count) == 0


# ---------------------------------------------------------------------------
# 回放修洞：助手调了一半就断了，也要能按 OpenAI 顺序重放
# ---------------------------------------------------------------------------


class TestHistoryRepair:
    @pytest.mark.asyncio
    async def test_crashed_tool_block_is_repaired(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content, tool_calls)"
                " VALUES (?, ?, 'assistant', '我在看', ?)",
                (cid, clock.now(), json.dumps([_call("stub_read", {}, "c1")], ensure_ascii=False)),
            )
        await chat.send(cid, "接着来")
        assert await chat.wait_idle(cid)
        _role, messages, _kw = svc.models.calls[0]
        assert [m["role"] for m in messages] == ["system", "assistant", "tool", "user"]
        assert messages[2]["tool_call_id"] == "c1"
        assert messages[2]["content"]

    @pytest.mark.asyncio
    async def test_system_note_never_splits_tool_block(self, svc: _Svc) -> None:
        chat, cid = _chat(svc)
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content, tool_calls)"
                " VALUES (?, ?, 'assistant', '', ?)",
                (cid, clock.now(), json.dumps([_call("stub_read", {}, "c1")], ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content)"
                " VALUES (?, ?, 'system_note', '中途的提示')",
                (cid, clock.now()),
            )
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content, tool_call_id, name)"
                " VALUES (?, ?, 'tool', '结果', 'c1', 'stub_read')",
                (cid, clock.now()),
            )
        await chat.send(cid, "接着来")
        assert await chat.wait_idle(cid)
        _role, messages, _kw = svc.models.calls[0]
        roles = [m["role"] for m in messages]
        ai = roles.index("assistant")
        assert roles[ai + 1] == "tool"  # 提示不能插在 assistant(tool_calls) 和 tool 中间
        assert "中途的提示" in json.dumps(messages, ensure_ascii=False)
