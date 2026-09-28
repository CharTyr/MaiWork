"""commands.py（/mw 指令）单元测试：权限、固定回复、去重、立刻 flush。

用真 Store + 真 Tasks/Goals/Approvals（数据层是同事写的、已测过的），
只把 Outbox / Host 换成假的发件箱和假的成员角色。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeCoordinator, FakeHost

from CharTyr_MaiWork.approvals import Approvals
from CharTyr_MaiWork.commands import Commands
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks

G1 = "900000001"
ADMIN = "10001"
MEMBER = "20002"
OTHER = "40004"


class FakeOutbox:
    """假的发件箱：和 Outbox.enqueue 同语义（同 key 去重），flush 只记录。"""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, str, dict]] = []
        self._keys: set[str] = set()
        self.flushed: list[float] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0) -> int:
        key = str(key)
        if key in self._keys:
            return -1  # 同 key 已有 → 返回旧 id，不重复
        self._keys.add(key)
        self.enqueued.append((key, str(group_id), str(kind), dict(payload)))
        return len(self.enqueued)

    async def flush(self, now: float) -> None:
        self.flushed.append(float(now))


def _env(tmp_path: Path, *, public_url: str = "", coordinator=None, run_task_starter=None):
    store = Store(tmp_path / "mw.db")
    store.migrate()
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "approval": {"required": True, "admins": [ADMIN]},
            "console": {"public_url": public_url},
        }
    )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, workspace, token, created) VALUES (?, ?, ?, ?)",
            (G1, f"g{G1}", "kTok1234", 1000.0),
        )
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    approvals = Approvals(store, lambda: settings, tasks, goals)
    outbox = FakeOutbox()
    host = FakeHost(session_id="sess-1")
    host.member_roles = {}
    cmd = Commands(
        store, approvals, tasks, goals, outbox, host,
        lambda: settings,
        coordinator=coordinator,
        run_task_starter=run_task_starter,
    )
    return SimpleEnv(store=store, settings=settings, tasks=tasks, goals=goals,
                     approvals=approvals, outbox=outbox, host=host, cmd=cmd)


class SimpleEnv:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)

    async def say(self, text: str, *, user: str = MEMBER, message_id: str = "m-1") -> str:
        await self.cmd.handle(G1, user, f"名字{user}", text, message_id)
        assert self.outbox.enqueued, f"应该有一条回复入队（{text}）"
        return self.outbox.enqueued[-1][3]["text"]


def _request(env: SimpleEnv, *, requester: str = MEMBER, title: str = "整理资料", quote: str = "") -> dict:
    return env.approvals.create(
        G1, kind="task", title=title, quote=quote or title,
        via="群里 @ · Jev 判断是「准备东西」（把握 0.90）",
        requester_id=requester, requester_name=f"名字{requester}", message_id="m-req",
    )


# ----------------------------------------------------------------------
# /mw 总览
# ----------------------------------------------------------------------


class TestBareMw:
    @pytest.mark.asyncio
    async def test_status_empty(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw")
        assert "目标" in reply and "任务" in reply and "待批" in reply
        assert reply.count("（没有）") == 3

    @pytest.mark.asyncio
    async def test_status_lists_goal_task_pending(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        gid_goal = env.goals.create_agent(G1, title="盯着铝价", body="", criteria=[], by_text="管理员 发起")
        mid = env.goals.create_member(
            G1, who_id=MEMBER, who_name=f"名字{MEMBER}", title="吃药", due_ts=None, remind_ts=None
        )
        tid = env.tasks.create(G1, title="整理资料", req="", criteria=[], source="request", status="queued")
        req = _request(env, title="做个对比表")
        reply = await env.say("/mw")
        assert gid_goal in reply and "盯着铝价" in reply
        assert mid in reply and "吃药" in reply
        assert tid in reply and "整理资料" in reply
        assert req["id"] in reply and "做个对比表" in reply

    @pytest.mark.asyncio
    async def test_status_caps_at_five_per_section(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        for i in range(7):
            env.tasks.create(G1, title=f"任务{i}", req="", criteria=[], source="test", status="queued")
        reply = await env.say("/mw")
        assert "共 7 件" in reply


# ----------------------------------------------------------------------
# /mw 网页
# ----------------------------------------------------------------------


class TestWeb:
    @pytest.mark.asyncio
    async def test_web_no_public_url(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 网页")
        assert reply == "网页还没公开，找管理员要"

    @pytest.mark.asyncio
    async def test_web_with_public_url(self, tmp_path: Path) -> None:
        env = _env(tmp_path, public_url="https://mw.example.com/")
        reply = await env.say("/mw 网页")
        assert reply == "本群网页：https://mw.example.com/#/kTok1234/news"


# ----------------------------------------------------------------------
# /mw 批准 / 拒绝：权限与「不带 ID」
# ----------------------------------------------------------------------


class TestDecide:
    @pytest.mark.asyncio
    async def test_approve_requires_admin(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        req = _request(env)
        reply = await env.say(f"/mw 批准 {req['id']}", user=OTHER)
        assert "只有 bot 管理员" in reply
        pending = env.approvals.pending_view(G1)
        assert [p["id"] for p in pending] == [req["id"]]

    @pytest.mark.asyncio
    async def test_reject_requires_admin(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        req = _request(env)
        reply = await env.say(f"/mw 拒绝 {req['id']}", user=OTHER)
        assert "只有 bot 管理员" in reply
        assert len(env.approvals.pending_view(G1)) == 1

    @pytest.mark.asyncio
    async def test_approve_single_pending_without_id(self, tmp_path: Path) -> None:
        started: list[str] = []
        env = _env(tmp_path, run_task_starter=started.append)
        req = _request(env)
        reply = await env.say("/mw 批准", user=ADMIN)
        assert "开工" in reply
        task_id = started[0] if started else None
        assert task_id is not None and task_id.startswith("T-")
        assert task_id in reply
        assert env.approvals.pending_view(G1) == []
        # 任务落地、排队中
        task = env.tasks.get(task_id)
        assert task["status"] == "queued"

    @pytest.mark.asyncio
    async def test_multiple_pending_without_id_lists_them(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        r1 = _request(env, title="第一件")
        r2 = _request(env, title="第二件")
        reply = await env.say("/mw 批准", user=ADMIN)
        assert r1["id"] in reply and r2["id"] in reply
        # 一个都没动
        assert len(env.approvals.pending_view(G1)) == 2

    @pytest.mark.asyncio
    async def test_no_pending_reports_empty(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 批准", user=ADMIN)
        assert "现在没有待批的请求" in reply

    @pytest.mark.asyncio
    async def test_approve_unknown_id(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 批准 R-99", user=ADMIN)
        assert "没找到请求 R-99" in reply

    @pytest.mark.asyncio
    async def test_approve_twice_is_explained(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        req = _request(env)
        first = await env.say(f"/mw 批准 {req['id']}", user=ADMIN, message_id="m-a")
        assert "开工" in first or "已批准" in first
        second = await env.say(f"/mw 批准 {req['id']}", user=ADMIN, message_id="m-b")
        assert "不能" in second or "已经" in second

    @pytest.mark.asyncio
    async def test_reject_with_id(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        req = _request(env)
        reply = await env.say(f"/mw 拒绝 {req['id']}", user=ADMIN)
        assert "已拒绝" in reply
        assert env.approvals.pending_view(G1) == []

    @pytest.mark.asyncio
    async def test_approve_goal_kind_does_not_start_task(self, tmp_path: Path) -> None:
        started: list[str] = []
        env = _env(tmp_path, run_task_starter=started.append)
        req = env.approvals.create(
            G1, kind="goal", title="帮我盯着", quote="帮我盯着", via="群里 @",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}",
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        assert "已批准" in reply
        assert started == []  # goal 不开任务


# ----------------------------------------------------------------------
# /mw 取消：四种身份
# ----------------------------------------------------------------------


class TestCancel:
    def _make_task(self, env: SimpleEnv) -> str:
        return env.tasks.create(
            G1, title="整理资料", req="", criteria=[], source="request",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}", status="queued",
        )

    @pytest.mark.asyncio
    async def test_requester_can_cancel_own_task(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        tid = self._make_task(env)
        reply = await env.say(f"/mw 取消 {tid}", user=MEMBER)
        assert "已取消" in reply
        assert env.tasks.get(tid)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_group_admin_can_cancel(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.host.member_roles[(G1, OTHER)] = "admin"
        tid = self._make_task(env)
        reply = await env.say(f"/mw 取消 {tid}", user=OTHER)
        assert "已取消" in reply
        assert env.tasks.get(tid)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_bot_admin_can_cancel(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        tid = self._make_task(env)
        reply = await env.say(f"/mw 取消 {tid}", user=ADMIN)
        assert "已取消" in reply
        assert env.tasks.get(tid)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_random_member_cannot_cancel(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        tid = self._make_task(env)
        reply = await env.say(f"/mw 取消 {tid}", user=OTHER)
        assert "发起人、群管理或 bot 管理员" in reply
        assert env.tasks.get(tid)["status"] == "queued"

    @pytest.mark.asyncio
    async def test_cancel_member_goal_by_self(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        mid = env.goals.create_member(
            G1, who_id=MEMBER, who_name=f"名字{MEMBER}", title="吃药提醒", due_ts=None, remind_ts=None
        )
        reply = await env.say(f"/mw 取消 {mid}", user=MEMBER)
        assert "已取消" in reply
        assert env.goals.get(mid)["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_agent_goal_by_admin(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        gid_goal = env.goals.create_agent(G1, title="盯着铝价", body="", criteria=[], by_text="管理员 发起")
        reply = await env.say(f"/mw 取消 {gid_goal}", user=ADMIN)
        assert "已取消" in reply
        assert env.goals.get(gid_goal)["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_unknown_id(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 取消 T-99", user=ADMIN)
        assert "没找到 T-99" in reply

    @pytest.mark.asyncio
    async def test_cancel_without_id_shows_usage(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 取消", user=ADMIN)
        assert "把要取消的 ID 带上" in reply

    @pytest.mark.asyncio
    async def test_cancel_other_group_not_allowed(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        tid = env.tasks.create(
            "999999", title="别群的活", req="", criteria=[], source="test",
            requester_id=MEMBER, status="queued",
        )
        reply = await env.say(f"/mw 取消 {tid}", user=ADMIN)
        assert "不是本群" in reply
        assert env.tasks.get(tid)["status"] == "queued"


# ----------------------------------------------------------------------
# 未知子命令、回复形态（reply_to、key 去重、立刻 flush）
# ----------------------------------------------------------------------


class TestReplyShape:
    @pytest.mark.asyncio
    async def test_unknown_subcommand_shows_usage(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 拍脑袋")
        assert "MaiWork 指令" in reply

    @pytest.mark.asyncio
    async def test_reply_has_reply_to_and_command_push_kind(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.cmd.handle(G1, MEMBER, f"名字{MEMBER}", "/mw", "m-42")
        key, gid, kind, payload = env.outbox.enqueued[0]
        assert key == "cmd:m-42"
        assert gid == G1
        assert kind == "text"
        assert payload["reply_to"] == "m-42"
        assert payload["push_kind"] == "command"
        # handle 里立刻 flush 过一次
        assert env.outbox.flushed, "回复后应该立刻 flush"

    @pytest.mark.asyncio
    async def test_same_message_id_deduped(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.cmd.handle(G1, MEMBER, f"名字{MEMBER}", "/mw", "m-dup")
        await env.cmd.handle(G1, MEMBER, f"名字{MEMBER}", "/mw", "m-dup")
        assert len(env.outbox.enqueued) == 1, "同一条消息的回复只入队一次（key 去重）"
        assert len(env.outbox.flushed) == 2


# ----------------------------------------------------------------------
# coordinator 兜底（没传 run_task_starter 时用 spawn 兜底）
# ----------------------------------------------------------------------


class TestCoordinatorFallback:
    @pytest.mark.asyncio
    async def test_approve_spawns_coordinator_run_task(self, tmp_path: Path) -> None:
        coord = FakeCoordinator()
        env = _env(tmp_path, coordinator=coord)
        req = _request(env)
        await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        # 兜底是 create_task：事件循环里跑一会儿就被调到
        for _ in range(20):
            if coord.run_calls:
                break
            import asyncio

            await asyncio.sleep(0.01)
        assert len(coord.run_calls) == 1
        assert coord.run_calls[0].startswith("T-")


class TestApproveIdeaItems:
    """/mw 批准走的是 approvals.approve：带项目的构想要逐个开工（和网页批准同一条路）。"""

    def _idea_with_items(self, env: SimpleEnv, items: list) -> int:
        import json as _json

        with env.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'books', '做铝价表', '整理铝价', ?, 'new', 1, 1)",
                (G1, _json.dumps(items, ensure_ascii=False)),
            )
            return int(cur.lastrowid or 0)

    @pytest.mark.asyncio
    async def test_approve_multi_item_starts_all_tasks(self, tmp_path: Path) -> None:
        started: list[str] = []
        env = _env(tmp_path, run_task_starter=started.append)
        idea_id = self._idea_with_items(env, [
            {"kind": "task", "title": "抓铝价数据", "desc": "先抓一个月"},
            {"kind": "task", "title": "做成表", "desc": ""},
        ])
        req = env.approvals.create(
            G1, kind="task", title="做铝价表", quote="", via="来自构想",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}", idea_id=idea_id,
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        assert "开工" in reply
        assert started == ["T-1", "T-2"]
        assert "T-1" in reply and "T-2" in reply
        assert env.tasks.get("T-1")["title"] == "抓铝价数据"

    @pytest.mark.asyncio
    async def test_approve_goal_item_creates_goal_not_task(self, tmp_path: Path) -> None:
        started: list[str] = []
        env = _env(tmp_path, run_task_starter=started.append)
        idea_id = self._idea_with_items(env, [
            {"kind": "goal", "title": "每周更新铝价", "desc": "每周更新一次"},
        ])
        req = env.approvals.create(
            G1, kind="task", title="做铝价表", quote="", via="来自构想",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}", idea_id=idea_id,
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        assert "目标 G-1" in reply
        assert started == []
        assert env.goals.get("G-1")["title"] == "每周更新铝价"
