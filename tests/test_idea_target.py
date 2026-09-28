"""构想可以指明「给谁」（2026-09-27 用户要求）。

- 个人向构想（ideas.target_user_id 非空）放进「构想」页，只给管理员看；
  每条带 for_member {user_id, name, avatar}，前端在卡片底部画头像 + 名字。
- 群友视图、MaiBot 可提起清单仍然不含个人向构想（关注成员相关内容只给管理员）。
- 群友不能对个人向构想 / 个人向资讯做任何操作（按不存在处理）。
- 「第一步」字段去掉模型自己带的「第一步：」前缀，避免页面上出现「第一步：第一步：」。
"""

from __future__ import annotations

import pytest

from CharTyr_MaiWork.maiwork import clock

from test_console import G1, PASSWORD, SimpleEnv, env  # noqa: F401  (env 是 fixture)

UID = "10001"
UID_GONE = "10002"


def _idea(app, *, title: str, uid: str = "", step: str = "") -> int:
    now = clock.now()
    with app.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, basis, step, effort, state,"
            " created, updated, target_user_id) VALUES (?, 'bulb', ?, 'b', '', ?, '', 'new', ?, ?, ?)",
            (G1, title, step, now, now, uid),
        )
        return int(cur.lastrowid or 0)


def _personal_news(app, uid: str = UID) -> int:
    now = clock.now()
    with app.store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 1, 1, 0, '', ?)",
            (G1, now, now),
        )
        bid = int(cur.lastrowid or 0)
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, icon, title, summary, why, sources,"
            " url_key, published_ts, score, status_kind, created, kind, rejected, target_user_id)"
            " VALUES (?, ?, 'tools', '只给他', 's', '', '[]', 'e.com/y', ?, 4.0, 'pool', ?, 'news', 0, ?)",
            (bid, G1, now, now, uid),
        )
        return int(cur.lastrowid or 0)


def _focus(app) -> None:
    app.profiles.focus_map[G1] = [
        {"user_id": UID, "name": "阿帆", "card": "阿帆", "reasons": [], "note": "", "pinned": True}
    ]
    with app.store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, reasons, note, pinned, removed, updated)"
            " VALUES (?, ?, '阿二', '[]', '', 0, 1, ?)",
            (G1, UID_GONE, clock.now()),
        )


class TestIdeaTarget:
    @pytest.mark.asyncio
    async def test_admin_sees_targeted_ideas_with_member(self, env: SimpleEnv) -> None:
        _focus(env.app)
        g = _idea(env.app, title="群向构想")
        t = _idea(env.app, title="帮阿帆整理清单", uid=UID)
        gone = _idea(env.app, title="帮阿二", uid=UID_GONE)
        await env.login(PASSWORD)
        token = env.app.token_of(G1)
        view = await (await env.client.get(f"/api/groups/{token}")).json()
        by_id = {i["id"]: i for i in view["ideas"]}
        assert set(by_id) >= {g, t, gone}
        assert by_id[g].get("for_member") is None
        fm = by_id[t]["for_member"]
        assert fm["user_id"] == UID and fm["name"] == "阿帆"
        assert "avatar" in fm
        # 已不在关注名单里的人：从库里的存量名字取
        assert by_id[gone]["for_member"]["name"] == "阿二"

    @pytest.mark.asyncio
    async def test_member_view_shows_targeted_ideas_without_basis(self, env: SimpleEnv) -> None:
        _focus(env.app)
        g = _idea(env.app, title="群向构想")
        t = _idea(env.app, title="帮阿帆整理清单", uid=UID)
        with env.app.store.tx() as conn:
            conn.execute("UPDATE ideas SET basis='画像摘要：在折腾FPGA' WHERE id=?", (t,))
            conn.execute("UPDATE ideas SET basis='群里在聊' WHERE id=?", (g,))
        token = env.app.token_of(G1)
        view = await (await env.client.get(f"/api/groups/{token}", headers={"X-MW-Group": token})).json()
        by_id = {i["id"]: i for i in view["ideas"]}
        assert set(by_id) == {g, t}
        assert by_id[t]["for_member"]["user_id"] == UID
        assert by_id[t]["basis"] == ""
        assert by_id[g]["basis"] == "群里在聊"
        assert by_id[g]["for_member"] is None
        assert "target_user_id" not in by_id[t]

    @pytest.mark.asyncio
    async def test_member_can_want_targeted_idea_but_not_personal_news(self, env: SimpleEnv) -> None:
        t = _idea(env.app, title="帮阿帆整理清单", uid=UID)
        nid = _personal_news(env.app)
        token = env.app.token_of(G1)
        h = {"X-MW-Group": token}
        r = await env.client.post(f"/api/ideas/{t}/feedback", json={"value": "up"}, headers=h)
        assert r.status == 200
        r = await env.client.post(f"/api/ideas/{t}/want", json={}, headers=h)
        assert r.status == 200
        r = await env.client.post(f"/api/news/{nid}/feedback", json={"value": "up"}, headers=h)
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_admin_can_do_targeted_idea(self, env: SimpleEnv) -> None:
        t = _idea(env.app, title="帮阿帆整理清单", uid=UID)
        await env.login(PASSWORD)
        r = await env.client.post(f"/api/ideas/{t}/dismiss", json={})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_step_prefix_not_doubled(self, env: SimpleEnv) -> None:
        a = _idea(env.app, title="甲", step="第一步：你先告诉我预算")
        b = _idea(env.app, title="乙", step="第一步: 列清单")
        await env.login(PASSWORD)
        token = env.app.token_of(G1)
        view = await (await env.client.get(f"/api/groups/{token}")).json()
        by_id = {i["id"]: i for i in view["ideas"]}
        assert by_id[a]["step"] == "你先告诉我预算"
        assert by_id[b]["step"] == "列清单"


# ----------------------------------------------------------------------
# 群里 @MaiBot 带「构想 #N」→ 按构想建待批请求
# ----------------------------------------------------------------------

from CharTyr_MaiWork.maiwork.intake import Intake, Signals  # noqa: E402
from CharTyr_MaiWork.maiwork.store import Store  # noqa: E402
from fakes import hook_message  # noqa: E402
from test_intake_m3 import _FakeApprovals, _FakeJev, _FakeMentions, _settings  # noqa: E402


def _intake(tmp_path, approvals=None):
    store = Store(tmp_path / "i.db")
    store.migrate()
    settings = _settings()
    spawned: list = []
    jev = _FakeJev({"kind": ("none", 0.9, 0.9)})
    mentions = _FakeMentions()
    intake = Intake(
        lambda: settings, Signals(), jev=jev, approvals=approvals or _FakeApprovals(),
        mentions=mentions, spawn=lambda c: spawned.append(c), store=store,
    )
    return intake, store, jev, mentions, spawned


def _store_idea(store, *, state="new", gid=G1) -> int:
    now = clock.now()
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, icon, title, body, state, created, updated, target_user_id)"
            " VALUES (?, 'gamepad', '帮你查清该买哪个版本', 'b', ?, ?, ?, '')",
            (gid, state, now, now),
        )
        return int(cur.lastrowid or 0)


class TestIdeaRefFromGroup:
    @pytest.mark.asyncio
    async def test_at_with_idea_ref_creates_request(self, tmp_path) -> None:
        intake, store, jev, mentions, spawned = _intake(tmp_path)
        iid = _store_idea(store)
        text = f"帮我做这个构想：帮你查清该买哪个版本\nb\n（构想 #{iid}）"
        await intake.handle(hook_message(is_at=True, user_id="20002", nickname="阿柒", message_id="m-1", text=text))
        for c in spawned:
            await c
        assert jev.calls == []  # 认出构想编号，不问 Jev
        created = intake._approvals.created
        assert len(created) == 1
        gid, kw = created[0]
        assert gid == G1 and kw["idea_id"] == iid and kw["kind"] == "task"
        assert kw["title"] == "帮你查清该买哪个版本"
        assert kw["requester_id"] == "20002" and kw["message_id"] == "m-1"
        assert "构想" in kw["via"]
        row = store.read().execute("SELECT state, requested_by FROM ideas WHERE id=?", (iid,)).fetchone()
        assert row["state"] == "pending" and row["requested_by"] == "阿柒"
        assert any("MaiWork 已经记下" in m["text"] for m in mentions.items)

    @pytest.mark.asyncio
    async def test_already_started_idea_not_duplicated(self, tmp_path) -> None:
        intake, store, jev, mentions, spawned = _intake(tmp_path)
        iid = _store_idea(store, state="started")
        await intake.handle(hook_message(is_at=True, text=f"帮我做（构想 #{iid}）"))
        for c in spawned:
            await c
        assert intake._approvals.created == []
        assert jev.calls == []
        assert any("已经在做" in m["text"] for m in mentions.items)

    @pytest.mark.asyncio
    async def test_other_group_idea_ref_falls_back_to_jev(self, tmp_path) -> None:
        intake, store, jev, mentions, spawned = _intake(tmp_path)
        iid = _store_idea(store, gid="555")
        await intake.handle(hook_message(is_at=True, text=f"帮我做（构想 #{iid}）"))
        for c in spawned:
            await c
        assert intake._approvals.created == []
        assert len(jev.calls) == 1
