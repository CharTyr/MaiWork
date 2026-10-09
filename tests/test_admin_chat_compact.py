"""admin_chat 上下文压缩（0.4.0）：CONTEXT_LIMIT=40 换成「最新摘要 + 其后消息」；
手动压缩 POST /api/chat/{id}/compact；对话 running 时手动压缩 409。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import clock, compaction
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat, ChatBusy
from CharTyr_MaiWork.maiwork.models import ChatResult, ModelError
from CharTyr_MaiWork.maiwork.tools import ToolContext

from test_admin_chat import G1, SECRET, _Models, _Svc, _call, _msgs, _res, _seed_msgs


def _seed_msgs_big(svc: _Svc, cid: int, n: int) -> None:
    """种 n 条 user/assistant 交替的老消息，content 较长。"""
    now = clock.now()
    with svc.store.tx() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content) VALUES (?, ?, ?, ?)",
                (cid, now + i, "user" if i % 2 == 0 else "assistant", f"第{i}条：" + "x" * 600),
            )


class TestHistoryUsesSummary:
    def test_history_starts_from_latest_summary(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs_big(svc, cid, 60)  # 60 条老对话
        # 种一条「摘要」消息（meta.kind=summary，覆盖前 50 条）+ 之后 10 条新消息
        with svc.store.tx() as conn:
            covered = svc.store.read().execute(
                "SELECT id, ts FROM admin_chat_msgs WHERE chat_id=? ORDER BY id LIMIT 50", (cid,)
            ).fetchall()
            cur = conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content, meta) VALUES (?, ?, 'assistant', ?, ?)",
                (
                    cid,
                    float(covered[-1]["ts"]) + 0.5,
                    "【前面对话的摘要】……",
                    json.dumps({"kind": "summary", "covers": 50, "from_ts": float(covered[0]["ts"]), "to_ts": float(covered[-1]["ts"]), "msg_from": int(covered[0]["id"]), "msg_to": int(covered[-1]["id"])}, ensure_ascii=False),
                ),
            )
        hist = chat._history(cid)
        # 不再看 40：从最新的摘要往后拼；摘要本身作为第一条 user 消息在
        assert hist[0]["role"] == "user" and "摘要" in str(hist[0]["content"])
        assert any("第50条" in str(m.get("content") or "") or "第51条" in str(m.get("content") or "") for m in hist)
        assert not any("第0条" in str(m.get("content") or "") for m in hist)  # 老的已经压掉了
        # 拼进来的后续消息都还在（不像以前只留 40 条）
        assert len(hist) >= 11

    def test_detail_marks_summary_messages(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO admin_chat_msgs (chat_id, ts, role, content, meta) VALUES (?, ?, 'assistant', '【前面对话的摘要】X', ?)",
                (cid, clock.now(), json.dumps({"kind": "summary", "covers": 30, "from_ts": 1.0, "to_ts": 2.0, "msg_from": 1, "msg_to": 30}, ensure_ascii=False)),
            )
        detail = chat.detail(cid)
        msgs = detail["messages"]
        assert msgs[0]["meta"]["kind"] == "summary"
        assert msgs[0]["meta"]["covers"] == 30
        assert msgs[0]["meta"]["to_ts"] == 2.0


class TestManualCompact:
    @pytest.mark.asyncio
    async def test_compact_writes_summary_and_returns(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("Primary Request and Intent：……8 节摘要……")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs_big(svc, cid, 20)
        out = await chat.compact(cid)
        assert out["summary"]["meta"]["kind"] == "summary"
        assert out["summary"]["meta"]["covers"] == 20
        rows = svc.store.read().execute(
            "SELECT meta FROM admin_chat_msgs WHERE chat_id=?", (cid,)
        ).fetchall()
        rowcount = sum(1 for r in rows if json.loads(r["meta"] or "{}").get("kind") == "summary")
        assert rowcount == 1
        # 摘要调用走了模型，purpose 带 :compact
        assert any(str(k.get("purpose") or "").endswith(":compact") for _r, _m, k in svc.models.calls)

    @pytest.mark.asyncio
    async def test_compact_busy_rejected(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        chat._begin(cid)  # 模拟该对话正在跑
        with pytest.raises(ChatBusy):
            await chat.compact(cid)

    @pytest.mark.asyncio
    async def test_compact_when_models_not_configured(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path, models=_Models(ready=False))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs_big(svc, cid, 5)
        with pytest.raises(ValueError):
            await chat.compact(cid)


    @pytest.mark.asyncio
    async def test_compact_model_error_becomes_friendly_valueerror(self, tmp_path: Path) -> None:
        """整理时模型连不上：给网页一句能看懂的 400，而不是 500「服务器出错了」；也不留半截摘要。"""
        from CharTyr_MaiWork.maiwork.models import ModelError

        svc = _Svc(tmp_path, models=_Models(script=[ModelError("网络错误（ConnectError）")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs_big(svc, cid, 6)
        with pytest.raises(ValueError) as ei:
            await chat.compact(cid)
        assert "整理没成功" in str(ei.value)
        rows = svc.store.read().execute("SELECT meta FROM admin_chat_msgs WHERE chat_id=?", (cid,)).fetchall()
        assert not any(json.loads(r["meta"] or "{}").get("kind") == "summary" for r in rows)
        # 没卡在「正在忙」
        assert not chat.is_running(cid) if hasattr(chat, "is_running") else True


class TestAutoCompactOnTurn:
    def test_history_builder_no_longer_truncates_at_40(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs_big(svc, cid, 80)
        hist = chat._history(cid)
        # 上下文里全收（估算超线由 _turn 里先压缩再发，不再看条数丢）
        assert len(hist) == 80


class TestServerRoute:
    @pytest.mark.asyncio
    async def test_compact_route_and_busy_409(self, tmp_path: Path) -> None:
        """POST /api/chat/{id}/compact：没错返回 summary；忙时 409。"""
        from CharTyr_MaiWork.maiwork.console.server import AUTH_KEY, create_app
        from tests.test_admin_chat_routes import _FakeStore, _Settings
        from aiohttp.test_utils import TestClient, TestServer
        import aiohttp

        svc_models = _Models(script=[_res("Primary Request and Intent：……8 节……")])
        svc = _Svc(tmp_path, models=svc_models)
        chat = AdminChat(svc)
        svc.admin_chat = chat

        # 换掉 _Svc 的 settings / store 给 console 鉴权用（密码 x）
        class _ConsoleSettings:
            class console:
                password = "x"

            class models:
                api_key = SECRET

            def is_served(self, gid: str) -> bool:
                return str(gid) == G1

        class _SvcAdapter:
            def __init__(self, base):
                self._base = base
                self.admin_chat = base.admin_chat
                self.store = _FakeStore()

            def get_settings(self):
                return _ConsoleSettings()

            def __getattr__(self, name):
                return getattr(self._base, name)

        app = create_app(_SvcAdapter(svc))
        server = TestServer(app)
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        try:
            auth = app[AUTH_KEY]
            from CharTyr_MaiWork.maiwork.console.auth import COOKIE_NAME
            value, _max_age = auth.make_cookie()
            client.session.cookie_jar.update_cookies({COOKIE_NAME: value})

            cid = int(chat.create(group_id=G1)["id"])
            _seed_msgs_big(svc, cid, 8)
            r = await client.post(f"/api/chat/{cid}/compact")
            assert r.status == 200
            data = await r.json()
            assert data["summary"]["meta"]["kind"] == "summary"

            chat._begin(cid)  # 忙走 409
            r = await client.post(f"/api/chat/{cid}/compact")
            assert r.status == 409
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 0.4.1 复盘修复：摘要查得到、原文存得住、覆盖边界按库里的行算、最新要求原样保住
# ---------------------------------------------------------------------------


class TestReadHistoryPaging:
    """read_history：按消息 id 往后翻 + 每条按字符窗口切（全文存库里，模型分页回读）。"""

    def test_window_walks_and_bounds_are_clamped(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.admin_chat import (
            READ_HISTORY_MAX_CHARS,
            READ_HISTORY_MAX_LIMIT,
        )

        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        long_row = chat._add_msg(cid, "tool", "字" * 250, tool_call_id="c1", name="large_read")
        _seed_msgs(svc, cid, 25)

        # 超界一律夹到上限；offset 负数当 0
        page = chat.read_history(cid, after=0, limit=999, offset=-5, chars=10**9)
        assert page["limit"] == READ_HISTORY_MAX_LIMIT
        assert page["offset"] == 0 and page["chars"] == READ_HISTORY_MAX_CHARS
        assert len(page["messages"]) == READ_HISTORY_MAX_LIMIT
        assert page["has_more"] is True

        # 照着 next_after 一路翻：每条行正好出现一次、id 递增、翻到底 has_more=False
        seen: list[int] = []
        cursor = 0
        for _ in range(20):
            page = chat.read_history(cid, after=cursor, limit=7, chars=READ_HISTORY_MAX_CHARS)
            seen.extend(int(m["id"]) for m in page["messages"])
            if not page["has_more"]:
                break
            cursor = int(page["next_after"])
        rows = svc.store.read().execute(
            "SELECT id FROM admin_chat_msgs WHERE chat_id=? ORDER BY id", (cid,)
        ).fetchall()
        assert seen == [int(r["id"]) for r in rows]
        assert seen[-1] == int(rows[-1]["id"])

        # 字符窗口：一段一段接，拼起来就是原文
        got = ""
        offset = 0
        while True:
            one = chat.read_history(cid, after=long_row - 1, limit=1, offset=offset, chars=100)
            msg = one["messages"][0]
            assert int(msg["id"]) == long_row and msg["total_chars"] == 250
            got += str(msg["content"])
            if not msg["has_more_content"]:
                assert msg["next_offset"] == 0
                break
            offset = int(msg["next_offset"])
        assert got == "字" * 250

    def test_unknown_chat_is_rejected(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        with pytest.raises(ValueError):
            chat.read_history(4242)

    def test_secrets_are_masked_when_read_back(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.admin_chat import READ_HISTORY_MAX_CHARS

        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        chat._add_msg(cid, "tool", f"接口返回：Bearer {SECRET}", name="stub_read")
        page = chat.read_history(cid, after=0, limit=5, chars=READ_HISTORY_MAX_CHARS)
        joined = json.dumps(page["messages"], ensure_ascii=False)
        assert SECRET not in joined, "读原文也要先遮密钥"
        assert "接口返回" in joined


class TestReadAdminHistoryTool:
    """read_admin_history：只认当前对话（ctx.chat_id），不给模型传 chat_id 的口子。"""

    def _wire(self, svc: _Svc, chat: AdminChat) -> None:
        from CharTyr_MaiWork.maiwork.tools_admin import register_admin_tools

        svc.admin_chat = chat
        svc.admin_pending = register_admin_tools(svc.tools, svc)

    @staticmethod
    def _ctx(chat_id: int, role: str = "admin") -> ToolContext:
        ctx = ToolContext(group_id=G1, actor="主模型（管理员对话）", role=role)
        ctx.chat_id = chat_id
        return ctx

    @pytest.mark.asyncio
    async def test_only_current_chat_and_no_chat_id_parameter(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        self._wire(svc, chat)
        other = int(chat.create(group_id=G1)["id"])
        mine = int(chat.create(group_id=G1)["id"])
        chat._add_msg(other, "user", "别的对话里的机密_TEXT")
        chat._add_msg(mine, "user", "本段对话的可见_TEXT")

        specs = {s["function"]["name"]: s for s in svc.tools.specs("admin")}
        assert "read_admin_history" in specs
        props = (specs["read_admin_history"]["function"]["parameters"].get("properties") or {})
        assert "chat_id" not in props, "工具不接受模型给别的对话号"

        # 模型就算硬塞 chat_id，也只读当前这段对话
        r = await svc.tools.call(
            "read_admin_history", {"chat_id": other, "limit": 10}, self._ctx(mine)
        )
        assert r.ok, r.error
        assert "本段对话的可见_TEXT" in r.output
        assert "别的对话里的机密_TEXT" not in r.output

    @pytest.mark.asyncio
    async def test_role_and_binding_are_enforced(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        self._wire(svc, chat)
        cid = int(chat.create(group_id=G1)["id"])
        chat._add_msg(cid, "user", "可见_TEXT")
        names = {s["function"]["name"] for s in svc.tools.specs("admin")}
        assert "read_admin_history" in names, "工具要注册给 admin 角色"
        # 绑定好的管理员对话读得到
        ok = await svc.tools.call("read_admin_history", {}, self._ctx(cid))
        assert ok.ok and "可见_TEXT" in ok.output
        # 子 agent / 普通主模型角色调不到（roles 只认 admin）
        r = await svc.tools.call("read_admin_history", {}, self._ctx(cid, role="worker"))
        assert not r.ok
        # 没绑定对话（ctx 上没有 chat_id）→ 明确拒绝，不猜一段来读
        bare = ToolContext(group_id=G1, actor="x", role="admin")
        r = await svc.tools.call("read_admin_history", {}, bare)
        assert not r.ok and not r.output

    @pytest.mark.asyncio
    async def test_long_tool_output_is_archived_but_readable_via_tool(self, tmp_path: Path) -> None:
        """全文留在库里；模型看到的只是头 + 尾 + 回读指针，照指针能对回原文。"""
        from CharTyr_MaiWork.maiwork.admin_chat import TOOL_CONTENT_MAX

        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        self._wire(svc, chat)
        cid = int(chat.create(group_id=G1)["id"])
        body = "A" * TOOL_CONTENT_MAX + "MIDDLE_PIECE" + "B" * 3000
        chat._add_msg(cid, "assistant", tool_calls=[_call("large_read", {})])
        rid = chat._add_msg(cid, "tool", body, tool_call_id="c1", name="large_read")
        hist = chat._history(cid)
        assert [m["role"] for m in hist] == ["assistant", "tool"]
        projected = str(hist[1]["content"])
        assert len(projected) < len(body) and "read_admin_history" in projected

        r = await svc.tools.call(
            "read_admin_history",
            {"after": rid - 1, "limit": 1, "offset": TOOL_CONTENT_MAX, "chars": 60},
            self._ctx(cid),
        )
        assert r.ok, r.error
        assert "MIDDLE_PIECE" in r.output


class TestSummaryFailureKeepsEverything:
    """摘要失败：库里一条不动、不留半截摘要、这一轮照旧（绝不因为整理失败丢原文）。"""

    @pytest.mark.asyncio
    async def test_failed_manual_summary_leaves_db_untouched(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[ModelError("端点挂了（ConnectError）")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs(svc, cid, 6)
        before = _msgs(svc, cid)
        with pytest.raises(ValueError):
            await chat.compact(cid)
        assert _msgs(svc, cid) == before, "摘要失败不许动库里的行"
        assert chat._latest_summary_row(cid) is None, "不许留半截摘要"
        assert not chat.is_busy(cid), "失败后不能卡在「正在忙」"
        assert "旧消息0" in json.dumps(chat._history(cid), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_failed_auto_summary_returns_history_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[ModelError("端点挂了")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs(svc, cid, 6)
        raw = chat._history(cid)
        monkeypatch.setattr(compaction, "compact_threshold", lambda *args, **kwargs: 1)
        monkeypatch.setattr(
            compaction, "pick_cut_point", lambda messages, **kwargs: (messages[-2:], messages[:-2])
        )
        got = await chat._auto_compact_for_turn(cid, raw)
        assert got == raw, "整理失败这一轮照旧发，上下文一条不动"
        assert chat._latest_summary_row(cid) is None


class TestIncrementalSummaryRounds:
    """多轮压缩：累计覆盖 + 本轮增量、上一版摘要进输入、最新用户要求原文程序化保住。"""

    @pytest.mark.asyncio
    async def test_manual_rounds_accumulate_and_keep_requirements_verbatim(
        self, tmp_path: Path
    ) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("摘要一"), _res("摘要二")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        u1 = chat._add_msg(cid, "user", "R1：只许动 admin_chat.py")
        a1 = chat._add_msg(cid, "assistant", "好")
        first = await chat.compact(cid)
        m1 = first["summary"]["meta"]
        assert m1["covers"] == 2 and m1["delta_covers"] == 2
        assert m1["msg_from"] == u1 and m1["msg_to"] == a1
        assert m1["pinned_user"]["id"] == u1

        u2 = chat._add_msg(cid, "user", "R2：文件名只许 ASCII")
        a2 = chat._add_msg(cid, "assistant", "好")
        second = await chat.compact(cid)
        m2 = second["summary"]["meta"]
        assert m2["covers"] == 4 and m2["delta_covers"] == 2, "累计覆盖 = 上一版 + 本轮增量"
        assert m2["msg_from"] == u1, "起点还是这段对话的开头"
        assert m2["msg_to"] == a2
        assert m2["prev_summary_id"] == first["summary"]["id"]
        assert m2["pinned_user"]["id"] == u2
        assert {int(p["id"]) for p in m2["pinned_users"]} >= {u1, u2}

        # 上一版摘要 + 本轮增量都进了摘要输入
        _role, sent, _kw = svc.models.calls[-1]
        sent_text = json.dumps(sent, ensure_ascii=False)
        assert "摘要一" in sent_text and "R2：文件名只许 ASCII" in sent_text

        # 上下文：新摘要在前，两条要求原文一条不少、没被摘要改写
        hist = chat._history(cid)
        assert "摘要二" in str(hist[0]["content"])
        hist_text = json.dumps(hist, ensure_ascii=False)
        assert "R1：只许动 admin_chat.py" in hist_text
        assert "R2：文件名只许 ASCII" in hist_text
        assert "摘要一" not in hist_text, "旧摘要被新摘要替代"

    @pytest.mark.asyncio
    async def test_repeated_manual_summary_without_new_user_keeps_previous_pin(
        self, tmp_path: Path
    ) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("摘要一"), _res("摘要二")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        u1 = chat._add_msg(cid, "user", "只许三条列，文件名 ASCII")
        chat._add_msg(cid, "assistant", "好")
        await chat.compact(cid)
        # 第二轮只有 assistant 增量：没有新用户原话，上一版钉住的要求不许丢
        chat._add_msg(cid, "assistant", "补充说明")
        out = await chat.compact(cid)
        meta = out["summary"]["meta"]
        assert meta["covers"] == 3 and meta["delta_covers"] == 1
        assert meta["pinned_user"]["id"] == u1
        assert any(
            "只许三条列，文件名 ASCII" in str(m.get("content"))
            for m in chat._history(cid)
        )

    @pytest.mark.asyncio
    async def test_auto_rounds_are_incremental_over_source_rows(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("自动摘要一"), _res("自动摘要二")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        u1 = chat._add_msg(cid, "user", "第一段要求：三条列")
        a1 = chat._add_msg(cid, "assistant", "好")
        chat._add_msg(cid, "user", "第二段要求：文件名 ASCII")
        a2 = chat._add_msg(cid, "assistant", "好")
        monkeypatch.setattr(compaction, "compact_threshold", lambda *args, **kwargs: 1)
        monkeypatch.setattr(
            compaction, "pick_cut_point", lambda messages, **kwargs: (messages[-2:], messages[:-2])
        )

        await chat._auto_compact_for_turn(cid, chat._history(cid))
        row1, m1 = chat._latest_summary_row(cid)
        assert m1["covers"] == 2 and m1["delta_covers"] == 2
        assert m1["msg_from"] == u1 and m1["msg_to"] == a1
        assert m1["pinned_user"]["id"] == u1, "被盖住的最新用户原话要程序化留住"

        chat._add_msg(cid, "user", "第三段要求：别动宿主")
        chat._add_msg(cid, "assistant", "好")
        await chat._auto_compact_for_turn(cid, chat._history(cid))
        row2, m2 = chat._latest_summary_row(cid)
        assert m2["covers"] == 4 and m2["delta_covers"] == 2, "第二轮只盖住新增那两行"
        assert m2["msg_from"] == u1 and m2["msg_to"] == a2
        assert m2["prev_summary_id"] == int(row1["id"])
        assert int(row2["id"]) > int(row1["id"])

        hist_text = json.dumps(chat._history(cid), ensure_ascii=False)
        assert "第三段要求：别动宿主" in hist_text, "没被盖住的原文原样留在上下文里"
        assert "第一段要求：三条列" in hist_text, "被盖住的靠钉住的原文保住"


class TestReadHistoryStaysBounded:
    """一次回读的**整体**输出有上限：limit=20 × chars=20000 不能拼出一个 40 万字的大包。"""

    def test_whole_response_bounded_and_partial_row_resumes(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.admin_chat import (
            READ_HISTORY_MAX_CHARS,
            READ_HISTORY_MAX_LIMIT,
            READ_HISTORY_TOTAL_CHARS,
        )

        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        for i in range(12):
            chat._add_msg(cid, "assistant", f"第{i}条" + "长" * 5000)

        page = chat.read_history(
            cid, after=0, limit=READ_HISTORY_MAX_LIMIT, chars=READ_HISTORY_MAX_CHARS
        )
        total = sum(len(str(m["content"])) for m in page["messages"])
        assert total <= READ_HISTORY_TOTAL_CHARS, "整包返回的总字数要有上限"
        assert page["has_more"] is True

        last = page["messages"][-1]
        assert last["has_more_content"] is True, "这一条没读完"
        # 没读完的那一条要能接着读：next_after 指回它自己，next_offset 指到断点
        assert int(page["next_after"]) == int(last["id"]) - 1
        assert int(page["next_offset"]) == int(last["next_offset"])
        nxt = chat.read_history(
            cid, after=page["next_after"], limit=1,
            offset=page["next_offset"], chars=READ_HISTORY_MAX_CHARS,
        )
        assert int(nxt["messages"][0]["id"]) == int(last["id"])
        row_text = str(
            svc.store.read()
            .execute("SELECT content FROM admin_chat_msgs WHERE id=?", (int(last["id"]),))
            .fetchone()["content"]
        )
        assert row_text.startswith(str(last["content"]) + str(nxt["messages"][0]["content"]))


class TestToolCallArgsNeverLandInDbUnmasked:
    """模型自己抄进 tool_calls 参数里的密钥也不许落库（回读工具也只回 content）。"""

    def test_arguments_are_masked_before_storing(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        call = _call("stub_read", {"note": f"Bearer {SECRET}"})
        chat._add_msg(cid, "assistant", tool_calls=[call])
        row = svc.store.read().execute(
            "SELECT tool_calls FROM admin_chat_msgs WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (cid,),
        ).fetchone()
        assert SECRET not in str(row["tool_calls"]), "参数里的密钥要先遮再落库"

        chat._add_msg(cid, "tool", "结果在这里", tool_call_id="c1", name="stub_read")
        page = chat.read_history(cid, after=0, limit=5)
        assert SECRET not in json.dumps(page["messages"], ensure_ascii=False)
        assert SECRET not in json.dumps(chat._history(cid), ensure_ascii=False)


class TestCoverageBoundaryMapping:
    """覆盖边界按「回放块 ↔ 库里行」对齐：切点不连续（中间保护了别的块）也不会错盖。"""

    def test_cut_that_skips_a_group_covers_the_right_rows(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        u1 = chat._add_msg(cid, "user", "一")
        a1 = chat._add_msg(cid, "assistant", "二")
        u2 = chat._add_msg(cid, "user", "三")
        a2 = chat._add_msg(cid, "assistant", "四")
        rows = [dict(r) for r in svc.store.read().execute(
            "SELECT * FROM admin_chat_msgs WHERE chat_id=? ORDER BY id", (cid,)
        ).fetchall()]
        groups = chat._source_groups(rows)
        flat = [m for _rows, msgs in groups for m in msgs]
        assert len(flat) == 4

        # 非连续切：只切第 1 条和最后一条（中间两条被保护 / 跳过）
        covered_rows, covered_msgs = chat._covered_for_cut(groups, [flat[0], flat[3]])
        assert [int(r["id"]) for r in covered_rows] == [u1, a2]
        assert covered_msgs == [flat[0], flat[3]]

        # 连续前缀还是老样子（最新的 user 不被盖住）
        covered_rows, _msgs = chat._covered_for_cut(groups, flat[:3])
        assert [int(r["id"]) for r in covered_rows] == [u1, a1, u2]
        assert u2 < a2

    @pytest.mark.asyncio
    async def test_real_cut_point_never_covers_the_arriving_user_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("真摘要")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        for i in range(10):
            chat._add_msg(cid, "user", f"第{i}段要求：" + "x" * 4000)
            chat._add_msg(cid, "assistant", f"收到{i}" + "y" * 4000)
        latest_user = int(
            svc.store.read().execute(
                "SELECT id FROM admin_chat_msgs WHERE chat_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                (cid,),
            ).fetchone()["id"]
        )
        monkeypatch.setattr(compaction, "compact_threshold", lambda *args, **kwargs: 1)
        # 不 patch pick_cut_point：走真实切点（默认保护最新一条用户原话）
        await chat._auto_compact_for_turn(cid, chat._history(cid))
        _row, meta = chat._latest_summary_row(cid)
        assert meta["covers"] >= 2, "这么长的一段该切出一部分来"
        assert int(meta["msg_to"]) < latest_user, "正在等回复的那句话绝不能被盖住"
        assert int(meta["pinned_user"]["id"]) < latest_user
        hist = json.dumps(chat._history(cid), ensure_ascii=False)
        assert "第9段要求" in hist, "最新一句要求还在上下文里"
        assert "第" in hist and "段要求" in hist


class TestOutgoingMessagesStayClean:
    """送模型的每条消息只带 OpenAI 那几把键，内部元数据（摘要 / 覆盖 / 库行号）不上线。"""

    @pytest.mark.asyncio
    async def test_history_has_no_internal_keys(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("摘要一")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        chat._add_msg(cid, "user", "只许三条列")
        call = _call("stub_read", {})
        mid = chat._add_msg(cid, "assistant", tool_calls=[call])
        await chat._run_call(cid, call, {}, G1, mid)
        await chat.compact(cid)

        allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
        hist = chat._history(cid)
        assert hist, "摘要之后还有内容"
        for msg in hist:
            extra = set(msg) - allowed
            assert all(str(k).startswith("maiwork_") for k in extra), (
                f"消息里混进了非内部键：{sorted(extra)}"
            )
        # 内部键（maiwork_*）发请求前被 models 剥掉：真正上网的那份只剩 OpenAI 那几把键
        from CharTyr_MaiWork.maiwork.models import _strip_internal_keys

        for msg in _strip_internal_keys(hist):
            assert set(msg) <= allowed, f"剥完还有内部键：{sorted(set(msg) - allowed)}"
        # 钉住的原文 / 摘要都进上下文了（说明不是靠空列表蒙过去的）
        text = json.dumps(hist, ensure_ascii=False)
        assert "只许三条列" in text and "摘要一" in text


class TestCompactionObservability:
    """摘要这条要能被下游认出来（内部标记），成功 / 失败都要有清楚的结构化日志。"""

    def test_summary_message_is_marked_internal(self, tmp_path: Path) -> None:
        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        chat._add_msg(cid, "user", "老内容")
        chat._add_msg(
            cid, "assistant", "只有正文没有标记的老摘要",
            meta={"kind": "summary", "covers": 1, "msg_from": 1, "msg_to": 1},
        )
        hist = chat._history(cid)
        assert compaction.is_summary_message(hist[0]) is True, "摘要这条要认成摘要，不能当用户原话"
        assert compaction.is_typed_user_message(hist[0]) is False
        assert hist[0][compaction.SUMMARY_FLAG_KEY] is True, "走 compaction 的内部标记"

    @pytest.mark.asyncio
    async def test_auto_compaction_logs_before_after_budget(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        import logging

        svc = _Svc(tmp_path, models=_Models(script=[_res("摘要")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs(svc, cid, 6)
        monkeypatch.setattr(compaction, "compact_threshold", lambda *args, **kwargs: 1)
        monkeypatch.setattr(
            compaction, "pick_cut_point", lambda messages, **kwargs: (messages[-2:], messages[:-2])
        )
        with caplog.at_level(logging.INFO, logger="maiwork.admin_chat"):
            await chat._auto_compact_for_turn(cid, chat._history(cid))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "自动整理" in text and "触发线" in text and "输出预留" in text
        assert "输入估计" in text and "→" in text, "整理前后都要记预算"

    @pytest.mark.asyncio
    async def test_failure_logs_warning_and_claims_no_success(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        import logging

        svc = _Svc(tmp_path, models=_Models(script=[ModelError("端点挂了")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        _seed_msgs(svc, cid, 6)
        monkeypatch.setattr(compaction, "compact_threshold", lambda *args, **kwargs: 1)
        monkeypatch.setattr(
            compaction, "pick_cut_point", lambda messages, **kwargs: (messages[-2:], messages[:-2])
        )
        with caplog.at_level(logging.INFO, logger="maiwork.admin_chat"):
            await chat._auto_compact_for_turn(cid, chat._history(cid))
        assert any(r.levelno >= logging.WARNING and "失败" in r.getMessage() for r in caplog.records)
        assert not any("整理成摘要" in r.getMessage() for r in caplog.records), "失败不许报成功"
        assert chat._latest_summary_row(cid) is None


class TestLongRequirementVerbatim:
    """长要求（远超旧 2000 字上限）也要逐字整段保住，跨两轮压缩不许被截 / 被再摘要改写。"""

    @pytest.mark.asyncio
    async def test_8000_char_requirement_keeps_its_distinct_tail_two_rounds(
        self, tmp_path: Path
    ) -> None:
        svc = _Svc(tmp_path, models=_Models(script=[_res("摘要一"), _res("摘要二")]))
        chat = AdminChat(svc)
        cid = int(chat.create(group_id=G1)["id"])
        tail = "尾巴标记_必须逐字保留_END"
        long_req = "长要求开头：" + "规" * 8000 + "：" + tail
        assert len(long_req) > 8000

        u1 = chat._add_msg(cid, "user", long_req)
        chat._add_msg(cid, "assistant", "好")
        await chat.compact(cid)
        _r1, m1 = chat._latest_summary_row(cid)
        assert int(m1["pinned_user"]["id"]) == u1
        assert m1["pinned_user"]["content"] == long_req, "钉住的是原文整段，不许截断"
        hist1 = json.dumps(chat._history(cid), ensure_ascii=False)
        assert long_req in hist1 and tail in hist1

        # 第二轮：又有新内容被盖住 → 上一版钉住的原文照旧整条在上下文里
        chat._add_msg(cid, "user", "第二轮要求：只许改这个文件")
        chat._add_msg(cid, "assistant", "好")
        await chat.compact(cid)
        _r2, m2 = chat._latest_summary_row(cid)
        pinned_by_id = {int(p["id"]): str(p["content"]) for p in m2["pinned_users"]}
        assert pinned_by_id[u1] == long_req, "第二轮也不许把老要求截断 / 丢字"
        assert int(m2["pinned_user"]["id"]) != u1, "最新一条用户原话要排在最后、不被上限挤掉"

        hist = chat._history(cid)
        hist_text = json.dumps(hist, ensure_ascii=False)
        assert long_req in hist_text and tail in hist_text
        # 钉住的原文带 compaction 的 pinned 标记：共享压缩的切点规划绝不会把它再摘要一遍
        pins = [m for m in hist if m.get(compaction.PINNED_KEY)]
        assert pins and any(long_req in str(m.get("content")) for m in pins)
        assert hist[0].get(compaction.SUMMARY_FLAG_KEY) is True
        # 出站前内部标记会被剥掉（真正上网的那份只有 OpenAI 那几把键）
        from CharTyr_MaiWork.maiwork.models import _strip_internal_keys

        for msg in _strip_internal_keys(hist):
            assert not any(str(k).startswith("maiwork_") for k in msg)


class TestTurnBudgetUsesActualModelWindow:
    """整包预算不许拿本地兜底窗口顶掉「实际选中模型」的窗口。"""

    def test_no_explicit_context_window_is_forced(self, tmp_path: Path, monkeypatch) -> None:
        seen: list[dict] = []

        def fake_budget(models, **kwargs):
            seen.append(dict(kwargs))
            return {
                "context_window": 64000,
                "output_reserve": 4096,
                "trigger_threshold": 51200,
                "calibrate_factor": 1.0,
            }

        svc = _Svc(tmp_path)
        chat = AdminChat(svc)
        monkeypatch.setattr(compaction, "context_budget", fake_budget)
        got = chat._turn_budget([{"role": "user", "content": "hi"}], [{"type": "function"}])
        assert "context_window" not in seen[0], "不许把兜底窗口塞进去顶掉真实窗口"
        assert seen[0]["role"] == "main" and seen[0]["agent"] == "main"
        assert seen[0]["messages"] and seen[0]["tools"]
        assert got["context_window"] == 64000 and got["output_reserve"] == 4096
        assert got["trigger_threshold"] == 51200
        assert set(got) == {
            "output_reserve",
            "estimated_input_tokens",
            "trigger_threshold",
            "context_window",
        }
