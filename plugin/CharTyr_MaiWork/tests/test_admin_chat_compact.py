"""admin_chat 上下文压缩（0.4.0）：CONTEXT_LIMIT=40 换成「最新摘要 + 其后消息」；
手动压缩 POST /api/chat/{id}/compact；对话 running 时手动压缩 409。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.admin_chat import AdminChat, ChatBusy
from CharTyr_MaiWork.models import ChatResult

from test_admin_chat import G1, SECRET, _Models, _Svc, _res


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
        from CharTyr_MaiWork.models import ModelError

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
        from CharTyr_MaiWork.console.server import AUTH_KEY, create_app
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
            from CharTyr_MaiWork.console.auth import COOKIE_NAME
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
