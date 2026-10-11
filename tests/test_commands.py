"""commands.py（/mw 指令）单元测试：权限、固定回复、去重、立刻 flush。

用真 Store + 真 Tasks/Goals/Approvals（数据层是同事写的、已测过的），
只把 Outbox / Host 换成假的发件箱和假的成员角色。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeCoordinator, FakeHost

from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.commands import (
    HELP_ITEMS,
    USAGE_TIPS,
    Commands,
    help_text,
    usage_text,
    usage_tips,
)
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

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


def _set_fit(env: SimpleEnv, rid: str, fit: str, *, reason: str = "", alt=None) -> None:
    """把「这件事 MaiWork 能不能盯」的判断写进请求行（判断本身在 test_goal_fit.py 测）。"""
    import json as _json

    with env.store.tx() as conn:
        conn.execute(
            "UPDATE requests SET goal_fit=?, goal_fit_reason=?, goal_fit_alt=? WHERE id=?",
            (fit, reason, (_json.dumps(alt, ensure_ascii=False) if alt is not None else ""), rid),
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

    @pytest.mark.asyncio
    async def test_status_ends_with_next_step_hint(self, tmp_path: Path) -> None:
        """状态最后一句告诉群友怎么派活、怎么找全部指令。"""
        env = _env(tmp_path)
        reply = await env.say("/mw")
        assert reply.splitlines()[-1] == "派活发 /mw do 要做的事；全部指令发 /mw 帮助"


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
        assert "发 /mw 看进度" in reply  # 顺手告诉批准人去哪看进展
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

    @pytest.mark.asyncio
    async def test_approve_human_goal_without_alt_replies_the_reason(self, tmp_path: Path) -> None:
        """R5：判断是「要群友自己动手」又没有替代任务 → 批准被拒，回复里写清原因。"""
        env = _env(tmp_path)
        req = env.approvals.create(
            G1, kind="goal", title="一起开档打卡", quote="一起开档打卡", via="群里 @",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}",
        )
        _set_fit(env, req["id"], "human", reason="要群友自己开档、截图")
        reply = await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        assert "群友自己动手" in reply and "盯不了" in reply
        assert "要群友自己开档、截图" in reply      # 原样带出理由
        assert "只能不批" in reply
        assert [p["id"] for p in env.approvals.pending_view(G1)] == [req["id"]]  # 还是待批
        assert env.store.read().execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
        assert env.store.read().execute("SELECT COUNT(*) AS n FROM goals").fetchone()["n"] == 0

    @pytest.mark.asyncio
    async def test_approve_human_goal_with_alt_starts_task(self, tmp_path: Path) -> None:
        """human + 替代任务 → 批准后按任务开工（不是目标）。"""
        started: list[str] = []
        env = _env(tmp_path, run_task_starter=started.append)
        req = env.approvals.create(
            G1, kind="goal", title="一起开档打卡", quote="一起开档打卡", via="群里 @",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}",
        )
        _set_fit(
            env, req["id"], "human", reason="要群友自己开档",
            alt={"title": "整理 mod 清单", "req": "把 mod 和分线整理成清单"},
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        assert "已开工" in reply
        rows = env.store.read().execute("SELECT title, request_id FROM tasks").fetchall()
        assert [r["title"] for r in rows] == ["整理 mod 清单"]
        assert started == ["T-1"]

    @pytest.mark.asyncio
    async def test_approve_skipped_idea_items_are_listed_in_reply(self, tmp_path: Path) -> None:
        """构想里「要群友自己动手」又没替代任务的项目被跳过，回复里要列出来。"""
        import json as _json

        env = _env(tmp_path)
        with env.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'books', '分线计划', '分线开档', ?, 'new', 1, 1)",
                (G1, _json.dumps([
                    {"kind": "goal", "title": "分线开档打卡", "desc": "群友自己开档"},
                    {"kind": "goal", "title": "盯新版本", "desc": "盯发布"},
                ], ensure_ascii=False)),
            )
            idea_id = int(cur.lastrowid or 0)
        req = env.approvals.create(
            G1, kind="task", title="分线计划", quote="分线计划", via="来自构想",
            requester_id=MEMBER, requester_name=f"名字{MEMBER}", idea_id=idea_id, source="idea",
        )
        _set_fit(
            env, req["id"], "human", reason="要群友自己开档",
            alt={"items": {
                "1": {"kind": "human", "reason": "要群友自己开档", "alt": None},
                "2": {"kind": "online", "reason": "盯发布", "alt": None},
            }},
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user=ADMIN)
        assert "目标" in reply                       # 第 2 项照旧立目标
        assert "1 项要群友自己动手" in reply
        assert "分线开档打卡" in reply


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
        assert reply.startswith("没有「拍脑袋」这个指令")
        assert "/mw 帮助" in reply and "/mw do" in reply

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


# ----------------------------------------------------------------------
# /mw 帮助：整段回复由 commands.HELP_ITEMS 这一张表生成
# ----------------------------------------------------------------------


class TestHelp:
    @pytest.mark.asyncio
    async def test_help_aliases_reply_the_same_text(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        for i, alias in enumerate(("帮助", "help", "?", "？")):
            reply = await env.say(f"/mw {alias}", message_id=f"m-help-{i}")
            assert reply == help_text()

    def test_help_text_is_grouped_and_covers_every_syntax(self) -> None:
        text = help_text()
        assert "大家都能用：" in text and "批准人：" in text
        for it in HELP_ITEMS:
            assert it["syntax"] in text, it
            assert it["desc"] in text, it
        # 例子照抄就能用
        assert "例：/mw do 整理一份开黑语音排障步骤" in text
        assert "例：/mw 批准 R-12" in text
        assert "例：/mw 取消 T-8" in text
        assert "例：/mw 领取 T-8" in text
        # 群消息，别太长（加了「直接 @ 我说」两条用法后放宽：正文 306 → 421 字）
        assert len(text) <= 460
        # 人人能用的排在前一组，批准人才用的排在后一组
        cut = text.index("批准人：")
        assert text.index("/mw do 要做的事") < cut
        assert text.index("/mw 领取 T-任务号") < cut
        assert text.index("/mw 网页") < cut
        assert text.index("/mw 帮助") < cut
        assert text.index("/mw 批准 [ID]") > cut
        assert text.index("/mw 拒绝 [ID]") > cut
        assert text.index("/mw 取消 [ID]") > cut

    @pytest.mark.asyncio
    async def test_short_usage_comes_from_the_same_table(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 随便")
        assert reply == usage_text("随便")
        assert "/mw 看进度" in reply and "/mw do 要做的事" in reply and "/mw 帮助" in reply


# ----------------------------------------------------------------------
# 「也可以直接 @ 我说」两条大白话用法（USAGE_TIPS / usage_tips / help_text 尾巴）
# ----------------------------------------------------------------------


def _table_head() -> str:
    """/mw 帮助 原有的那一段（从指令表生成）；用来钉住「前面一字不变」。"""
    def line(it: dict[str, str]) -> str:
        # 和 commands._help_line 同一个写法，冻结成期望
        return f"{it['syntax']} — {it['desc']}" + (
            f"，例：{it['example']}" if it.get("example") else ""
        )

    out = ["MaiWork 群指令：", "大家都能用："]
    out += [f"· {line(it)}" for it in HELP_ITEMS if it["who"] == "所有人"]
    out.append("批准人：")
    out += [f"· {line(it)}" for it in HELP_ITEMS if it["who"] != "所有人"]
    return "\n".join(out)


class TestUsageTips:
    def test_two_tips_are_plain_sayings_with_placeholder(self) -> None:
        assert len(USAGE_TIPS) == 2
        assert USAGE_TIPS[0] == {
            "say": "@{bot} 提醒我 周五晚八点 开黑",
            "desc": "到点在群里 @ 你提醒，不用批准",
        }
        assert USAGE_TIPS[1] == {
            "say": "@{bot} 帮我们盯着 暗潮新版本，出了说一声",
            "desc": "MaiWork 在网上盯着，盯到了在群里说一声；要管理员批准",
        }

    def test_usage_tips_replaces_placeholder_on_a_copy(self) -> None:
        tips = usage_tips("小笨")
        assert len(tips) == 2
        for got, src in zip(tips, USAGE_TIPS):
            assert "{bot}" not in got["say"]
            assert "小笨" in got["say"]
            assert got["desc"] == src["desc"]
        # 是副本：改返回值不能碰唯一来源
        tips[0]["say"] = "改坏了"
        assert "{bot}" in USAGE_TIPS[0]["say"]

    def test_usage_tips_blank_name_falls_back_to_maibot(self) -> None:
        for blank in ("", "   ", "\n"):
            tips = usage_tips(blank)
            assert all("MaiBot" in t["say"] for t in tips), blank
        assert usage_tips() == usage_tips("MaiBot")

    def test_help_text_appends_tips_after_the_unchanged_table(self) -> None:
        text = help_text("小笨")
        marker = "也可以直接 @ 我说："
        assert text.count(marker) == 1
        head, tail = text.split(marker)
        # 前面部分一字不变（末尾换行 + 两条用法）
        assert head == _table_head() + "\n"
        assert tail == (
            "\n· @小笨 提醒我 周五晚八点 开黑 — 到点在群里 @ 你提醒，不用批准"
            "\n· @小笨 帮我们盯着 暗潮新版本，出了说一声 —"
            " MaiWork 在网上盯着，盯到了在群里说一声；要管理员批准"
        )
        # 每条都在，且用了机器人名字
        for tip in USAGE_TIPS:
            assert tip["desc"] in text

    def test_help_text_empty_name_is_maibot(self) -> None:
        assert help_text("") == help_text("MaiBot")
        assert "@MaiBot 提醒我 周五晚八点 开黑" in help_text()

    @pytest.mark.asyncio
    async def test_help_reply_uses_the_bot_name_from_host(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.host.bot_name_cache = "阿笨"
        reply = await env.say("/mw 帮助", message_id="m-help-name")
        assert reply == help_text("阿笨")
        assert "@阿笨 提醒我 周五晚八点 开黑" in reply

    @pytest.mark.asyncio
    async def test_help_reply_falls_back_when_host_has_no_name(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 帮助", message_id="m-help-noname")
        assert "@MaiBot 提醒我 周五晚八点 开黑" in reply


# ----------------------------------------------------------------------
# 少了 do：看着像派活就回一行能照抄的 /mw do，绝不建请求
# ----------------------------------------------------------------------


class TestMissingDoHint:
    @pytest.mark.asyncio
    async def test_request_like_text_gets_copyable_line(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 帮我整理一份清单")
        assert reply == "要派活请这样发：\n/mw do 帮我整理一份清单"
        assert env.approvals.pending_view(G1) == []
        n = env.store.read().execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
        assert int(n) == 0, "提示归提示，不许真建请求"

    @pytest.mark.asyncio
    async def test_hint_caps_long_text(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 帮我" + "整" * 300)
        assert reply.splitlines()[1] == "/mw do 帮我" + "整" * 98

    @pytest.mark.asyncio
    async def test_chitchat_without_cue_gets_plain_usage(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 随便说说")
        assert reply.startswith("没有「随便说说」这个指令")
        assert "要派活请这样发" not in reply
