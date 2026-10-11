"""`/mw do 要做的事`（群里直接派活）单元测试。

用真 Store + 真 Tasks/Goals/Approvals（数据层已单独测过），只换掉 Outbox / Host。
覆盖：文本 / 别名「做」/ 引用一条消息 / 引用 + 补充 / 空内容回用法 / 免批当场开工 /
同一条消息不重复建 / 非服务群零动作 / 不经「被拒多了不再收」筛。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeHost

from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.commands import Commands
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

G1 = "900000001"
ADMIN = "10001"
MEMBER = "20002"

JOB = "帮我整理一份开黑语音排障步骤"


class FakeOutbox:
    """假的发件箱：和 Outbox.enqueue 同语义（同 key 去重），flush 只记录。"""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, str, dict]] = []
        self._keys: set[str] = set()
        self.flushed: list[float] = []

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0) -> int:
        key = str(key)
        if key in self._keys:
            return -1
        self._keys.add(key)
        self.enqueued.append((key, str(group_id), str(kind), dict(payload)))
        return len(self.enqueued)

    async def flush(self, now: float) -> None:
        self.flushed.append(float(now))


class Env:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)

    async def say(
        self,
        text: str,
        *,
        user: str = MEMBER,
        message_id: str = "m-do",
        reply_to: str = "",
        gid: str = G1,
    ) -> str:
        await self.cmd.handle(gid, user, f"名字{user}", text, message_id, reply_to=reply_to)
        assert self.outbox.enqueued, f"应该有一条回复入队（{text}）"
        return self.outbox.enqueued[-1][3]["text"]

    def requests(self) -> list[dict]:
        rows = self.store.read().execute(
            "SELECT * FROM requests ORDER BY created ASC, id ASC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


def _env(
    tmp_path: Path,
    *,
    approval: dict | None = None,
    run_task_starter=None,
    by_id: dict | None = None,
    message_error: Exception | None = None,
) -> Env:
    store = Store(tmp_path / "mw.db")
    store.migrate()
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}]},
            "approval": approval if approval is not None else {"required": True, "admins": [ADMIN]},
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
    host = FakeHost(session_id="sess-1", by_id=by_id or {}, message_error=message_error)
    cmd = Commands(
        store, approvals, tasks, goals, outbox, host,
        lambda: settings,
        run_task_starter=run_task_starter,
    )
    return Env(store=store, settings=settings, tasks=tasks, goals=goals,
               approvals=approvals, outbox=outbox, host=host, cmd=cmd)


# ----------------------------------------------------------------------
# 文本形式
# ----------------------------------------------------------------------


class TestTextForm:
    @pytest.mark.asyncio
    async def test_creates_pending_request(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say(f"/mw do {JOB}", message_id="m-9")
        assert reply == "收到，R-1 等管理员批准；批准人发 /mw 批准 R-1 就开工"
        rows = env.requests()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"
        assert row["kind"] == "task"
        assert row["title"] == JOB
        assert row["quote"] == JOB
        assert row["via"] == "群里 /mw do 指令"
        assert row["requester_id"] == MEMBER
        assert row["requester_name"] == f"名字{MEMBER}"
        assert row["message_id"] == "m-9"
        assert row["source"] == ""
        assert env.approvals.pending_view(G1)[0]["id"] == "R-1"

    @pytest.mark.asyncio
    async def test_keeps_newlines_and_caps_title(self, tmp_path: Path) -> None:
        """/mw do 后面那截保留原有换行（任务正文要完整）；标题压成一行、截 30 字。"""
        env = _env(tmp_path)
        job = "第一步：查日志\n第二步：看麦克风权限"
        await env.say(f"/mw do {job}")
        row = env.requests()[0]
        assert row["quote"] == job
        assert "\n" in row["quote"]
        assert row["title"] == " ".join(job.split())
        assert len(row["title"]) <= 30

    @pytest.mark.asyncio
    async def test_alias_zuo(self, tmp_path: Path) -> None:
        """`/mw 做 …` 是同一个处理（只是不在文档里写）。"""
        env = _env(tmp_path)
        reply = await env.say(f"/mw 做 {JOB}")
        assert "R-1" in reply
        assert env.requests()[0]["quote"] == JOB

    @pytest.mark.asyncio
    async def test_no_screening_for_explicit_command(self, tmp_path: Path) -> None:
        """明确指令不经「类似的被拒多了就不再收」：写明要做的事就一定记下来。"""
        env = _env(tmp_path)
        for i in range(3):
            env.approvals.create(
                G1, kind="task", title=JOB, quote=JOB, via="群里 @ · Jev",
                requester_id=MEMBER, requester_name=f"名字{MEMBER}", message_id=f"m-old{i}",
            )
            env.approvals.reject(f"R-{i + 1}", by=ADMIN)
        reply = await env.say(f"/mw do {JOB}", message_id="m-new")
        assert "R-4 等管理员批准" in reply
        assert env.requests()[-1]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_empty_without_quote_shows_usage(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw do")
        assert reply == (
            "把要做的事写在后面，例如 /mw do 整理一份开黑语音排障步骤；"
            "也可以引用一条消息再发 /mw do；全部指令发 /mw 帮助"
        )
        assert env.requests() == []


# ----------------------------------------------------------------------
# 引用一条消息
# ----------------------------------------------------------------------


class TestQuoteForm:
    @pytest.mark.asyncio
    async def test_quoted_text_is_the_job(self, tmp_path: Path) -> None:
        env = _env(tmp_path, by_id={"m-quoted": {"processed_plain_text": "这是要做的原文"}})
        reply = await env.say("/mw do", message_id="m-do-1", reply_to="m-quoted")
        assert "R-1 等管理员批准" in reply
        row = env.requests()[0]
        assert row["quote"] == "这是要做的原文"
        assert row["title"] == "这是要做的原文"
        assert row["via"] == "群里 /mw do 指令"
        # 关键：message_id 必须是 /mw do 自己那条——coordinator 靠它去取这条消息
        # 引用的那段（图片等）就要经它的 reply 段拿到（见 coordinator._collect_request_images）
        assert row["message_id"] == "m-do-1"

    @pytest.mark.asyncio
    async def test_quoted_plus_extra_words(self, tmp_path: Path) -> None:
        env = _env(tmp_path, by_id={"m-quoted": {"processed_plain_text": "原文：语音排队"}})
        await env.say("/mw do 按上周的格式来", message_id="m-do-2", reply_to="m-quoted")
        row = env.requests()[0]
        assert row["title"] == "原文：语音排队"
        assert "原文：语音排队" in row["quote"]
        assert "按上周的格式来" in row["quote"]  # 后面那句算发起人的补充说明

    @pytest.mark.asyncio
    async def test_quote_lookup_failure_without_text_shows_usage(self, tmp_path: Path) -> None:
        env = _env(tmp_path, message_error=RuntimeError("宿主炸了"))
        reply = await env.say("/mw do", message_id="m-do-3", reply_to="m-quoted")
        assert reply.startswith("把要做的事写在后面")
        assert env.requests() == []

    @pytest.mark.asyncio
    async def test_quote_missing_message_without_text_shows_usage(self, tmp_path: Path) -> None:
        env = _env(tmp_path)  # by_id 空 → message_by_id 返回 None
        reply = await env.say("/mw do", message_id="m-do-4", reply_to="m-nope")
        assert reply.startswith("把要做的事写在后面")
        assert env.requests() == []

    @pytest.mark.asyncio
    async def test_quote_capped(self, tmp_path: Path) -> None:
        env = _env(tmp_path, by_id={"m-quoted": {"processed_plain_text": "长" * 3000}})
        await env.say("/mw do", message_id="m-do-5", reply_to="m-quoted")
        assert len(env.requests()[0]["quote"]) == 2000


# ----------------------------------------------------------------------
# 免批（自动批准）：和 /mw 批准 同一条开工路
# ----------------------------------------------------------------------


class TestAutoApproved:
    @pytest.mark.asyncio
    async def test_lands_and_starts_task(self, tmp_path: Path) -> None:
        started: list[str] = []
        env = _env(tmp_path, approval={"required": False, "admins": [ADMIN]}, run_task_starter=started.append)
        reply = await env.say(f"/mw do {JOB}")
        assert reply == "收到，已开工（T-1）；发 /mw 看进度，/mw 取消 T-1 可以取消"
        assert started == ["T-1"]
        assert env.approvals.pending_view(G1) == []
        assert env.tasks.get("T-1")["status"] == "queued"
        assert env.tasks.get("T-1")["title"] == JOB


# ----------------------------------------------------------------------
# 去重 / 非服务群
# ----------------------------------------------------------------------


class TestGuards:
    @pytest.mark.asyncio
    async def test_same_message_creates_one_request(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        first = await env.say(f"/mw do {JOB}", message_id="m-same")
        assert "R-1" in first
        env.outbox._keys.clear()  # 热重载重放：发件箱的去重键没了，请求也不能再来一条
        second = await env.say(f"/mw do {JOB}", message_id="m-same")
        assert second == "收到，R-1 等管理员批准；批准人发 /mw 批准 R-1 就开工"
        assert len(env.requests()) == 1

    @pytest.mark.asyncio
    async def test_same_message_after_landing_reports_started(self, tmp_path: Path) -> None:
        env = _env(tmp_path, approval={"required": False, "admins": [ADMIN]}, run_task_starter=lambda t: None)
        await env.say(f"/mw do {JOB}", message_id="m-same2")
        env.outbox._keys.clear()
        reply = await env.say(f"/mw do {JOB}", message_id="m-same2")
        assert reply == "收到，已开工（T-1）；发 /mw 看进度，/mw 取消 T-1 可以取消"
        assert len(env.requests()) == 1

    @pytest.mark.asyncio
    async def test_non_served_group_does_nothing(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.cmd.handle("999999", MEMBER, f"名字{MEMBER}", f"/mw do {JOB}", "m-x")
        assert env.requests() == []
        assert env.outbox.enqueued == []

    @pytest.mark.asyncio
    async def test_create_error_goes_to_generic_error_reply(self, tmp_path: Path) -> None:
        """建请求出错 → 走 handle 原有的通用报错，绝不把异常抛给后台协程。"""
        env = _env(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("库炸了")

        env.cmd._approvals.create = boom  # type: ignore[method-assign]
        reply = await env.say(f"/mw do {JOB}")
        assert reply == "这条指令处理出错了，日志里有；admin 可以到网页上看"
        assert env.requests() == []


# ----------------------------------------------------------------------
# 用法提示
# ----------------------------------------------------------------------


class TestUsage:
    @pytest.mark.asyncio
    async def test_usage_mentions_do(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say("/mw 拍脑袋")
        assert "/mw do" in reply
        assert "/mw 帮助" in reply  # 短提示里带上哪里看全部指令

    @pytest.mark.asyncio
    async def test_pending_reply_says_who_can_approve(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        reply = await env.say(f"/mw do {JOB}", message_id="m-hint-1")
        assert "等管理员批准" in reply and "批准人发 /mw 批准 R-1 就开工" in reply

    @pytest.mark.asyncio
    async def test_started_reply_says_where_to_look_and_cancel(self, tmp_path: Path) -> None:
        env = _env(tmp_path, approval={"required": False, "admins": [ADMIN]}, run_task_starter=lambda t: None)
        reply = await env.say(f"/mw do {JOB}", message_id="m-hint-2")
        assert "发 /mw 看进度" in reply and "/mw 取消 T-1 可以取消" in reply
