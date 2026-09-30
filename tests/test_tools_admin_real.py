"""tools_admin 真模块集成测试：起真 MaiWorkApp + 真数据层模块，把 39+1 个管理员工具
每一个都真调一遍（docs/02-设计.md「管理员对话」的回归保险）。

为什么单开一个文件：tests/test_tools_admin.py 用的是「最小服务包」，模块是拼出来的
简版，线上就踩过「单测绿、真模块上坏」的坑（list_news 把批次当条目、task_detail
拿不到 group_id）。这里所有被工具读的模块都是真实现（真 Store/Feeds/Tasks/Goals/
Approvals/Profiles/Identity/Outbox/Rules/Rss），只有模型相关是假的。

覆盖清单写死在 ALL_ADMIN_TOOLS：tools.specs("admin") 里新增 / 少了工具都会报错，
不许漏测。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from fakes import FakeCtx

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat
from CharTyr_MaiWork.maiwork.tools import ToolContext

G1 = "900000001"
G2 = "123456789"
G_BAD = "99999"

# 管理员对话全部工具的覆盖清单（含本批新增的 refresh_profile / create_task 确认化）。
# tools.specs("admin") 的实际名单必须和它一模一样：新增工具漏测会在这里报错。
ALL_ADMIN_TOOLS = {
    # 读类
    "list_groups", "group_overview", "read_profile", "list_focus", "list_news",
    "list_ideas", "list_tasks", "task_detail", "list_goals", "list_requests",
    "read_chat", "read_logs", "get_rules", "get_identity", "list_extensions",
    # 写类
    "profile_edit", "profile_bulk_delete", "focus_edit", "set_feeds_pref",
    "rss_add", "rss_remove", "block_domain", "set_rules", "identity_edit", "remember",
    # 危险 / 对外
    "send_group_message", "group_notice_send", "group_file_manage", "group_album_upload",
    "skill_delete", "mcp_toggle", "mcp_delete",
    # 现在动手（后台类）
    "run_news_now", "make_idea_now", "refresh_profile",
    # 任务 / 目标 / 请求
    "create_task", "create_goal", "approve_request", "reject_request", "cancel_task",
}

NOW = time.time()


def _free_port() -> int:
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw(data_dir: Path, port: int) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{port}", "password": "pw-测试"},
        "models": {"base_url": "http://127.0.0.1:9/v1", "api_key": "test-key", "main": "m1", "worker": "w1"},
        "storage": {"data_dir": str(data_dir)},
        "environments": {"workspace_root": ""},
    }


def _ctx(gid: str = G1, role: str = "admin") -> ToolContext:
    return ToolContext(group_id=gid, actor="bot 管理员", role=role)


def _seed(app: MaiWorkApp) -> None:
    """用各模块自己的公开方法 / 线上同款造数（参考 $TMPDIR/mw-dev/real.py）种真实形状的数据。"""
    p = app.profiles
    p.add_entry(G1, "convention", "周五晚上是「分享夜」，大家贴本周的折腾成果")
    p.add_entry(G1, "interest", "自部署服务和家里的服务器")
    p.set_focus(G1, "10003", "add")

    # 群聊记录（chatlog.record_messages 是真写入入口，走真路径）
    from CharTyr_MaiWork.maiwork import chatlog

    msgs = [
        type("M", (), {"is_bot": False, "text": "有没有地方能白嫖一台临时机器跑下备份迁移",
                       "id": "m1", "ts": NOW - 3600, "user_id": "10001", "user_name": "阿柒"})(),
        type("M", (), {"is_bot": False, "text": "railway.new 一行 ssh 就能拿一台",
                       "id": "m2", "ts": NOW - 3500, "user_id": "10002", "user_name": "蓝莓山竹"})(),
    ]
    chatlog.record_messages(app.store, G1, msgs, now=NOW)

    st = app.store
    with st.tx() as c:
        # 资讯批次 + 条目（含 1 条被筛掉的，list_news 要把被筛数说出来）
        c.execute(
            "INSERT INTO news_batches(group_id,slot_ts,found,kept,skipped,created) VALUES(?,?,?,?,?,?)",
            (G1, NOW - 3600, 18, 2, 0, NOW - 3600),
        )
        b = c.execute("SELECT max(id) FROM news_batches").fetchone()[0]
        c.execute(
            "INSERT INTO news_items(batch_id,group_id,icon,title,summary,why,sources,score,status_kind,expires_ts,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                b, G1, "rocket", "railway.new：一行 ssh 拿到一台临时 Linux",
                "大约 1.4 秒给你一台 2 核 2G 的机器。", "群里最近在找能随手用的临时机器。",
                json.dumps([{"url": "https://railway.new", "site": "railway.new", "title": "官方说明"}]),
                0.8, "pool", NOW + 10 * 3600, NOW - 3600,
            ),
        )
        c.execute(
            "INSERT INTO news_items(batch_id,group_id,icon,title,summary,why,sources,score,status_kind,expires_ts,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                b, G1, "chart", "一篇讲 SQLite 当消息队列的长文",
                "从单写入者讲到 WAL 检查点。", "群里在讨论发件箱放哪。",
                json.dumps([{"url": "https://example.com/a", "site": "example.com", "title": "SQLite as a queue"}]),
                0.7, "new", NOW + 10 * 3600, NOW - 3600,
            ),
        )
        c.execute(
            "INSERT INTO news_items(batch_id,group_id,icon,title,sources,score,status_kind,rejected,reject_gate,reject_reason,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                b, G1, "newspaper", "某营销稿：手机跑分又破纪录",
                json.dumps([{"url": "https://spam.example.com/x", "site": "spam.example.com", "title": "营销稿"}]),
                0.2, "new", 1, "web", "像营销稿", NOW - 3600,
            ),
        )
        c.execute(
            "INSERT INTO ideas(group_id,icon,title,body,basis,step,effort,state,created,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                G1, "monitor", "我可以每天巡检一次群里的服务器",
                "每天早上看各容器状态和磁盘水位，只在真出问题时提一句。",
                "这周两次有人说服务挂了，最后是误报。", "先列出要看的容器，发给你确认。",
                "半天", "new", NOW - 7200, NOW - 7200,
            ),
        )
        c.execute(
            "INSERT INTO model_calls(ts,purpose,role,model,group_id,ok,status,ms,error)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (NOW - 60, "worker", "worker", "w1", G1, 0, 429, 1200, "限流了（测试记录）"),
        )
        st.kv_set(c, f"feeds.pref.{G1}", "多找自部署和开源硬件，少一点手机评测")

    # 任务 / 目标 / 待批请求（都用真模块的公开方法）
    tid = app.tasks.create(
        G1, title="插件踩坑文档：写成网页",
        req="把整理好的坑按主题写成一页网页，手机上能看。",
        criteria=["至少覆盖 6 个主题"], source="admin_chat",
        requester_id="", requester_name="bot 管理员（管理员对话）", icon="books",
    )
    app.tasks.transition(tid, "running", env="本机 · 测试")
    app.tasks.transition(tid, "reviewing")
    app.tasks.transition(tid, "completed", review="3 个样例都导出正常。")
    t2 = app.tasks.create(G1, title="Aseprite 批量导出脚本", req="把 .ase 批量导出成 PNG。",
                          criteria=[], source="idea", icon="palette")
    app.tasks.transition(t2, "running")
    t3 = app.tasks.create(G1, title="排队中的任务", req="等着开工。", criteria=[], source="admin_chat")

    gid_agent = app.goals.create_agent(
        G1, title="把 MaiBot 插件开发的坑整理成群文档", body="两个月踩过的坑整理成一页能查的网页。",
        criteria=["收集群里相关讨论", "写成网页草稿"], by_text="zzh 发起 · 管理员批准",
    )
    app.goals.create_member(G1, who_id="10002", who_name="蓝莓山竹",
                            title="今晚把 Jev 对照的 case 发到群里", due_ts=NOW + 5400, remind_ts=NOW + 1800)

    app.approvals.create(
        G1, kind="task", title="汇总这周群里提到的 NAS 方案",
        quote="@东雪莲 帮我把这周大家提到的 NAS 方案汇总一下",
        via="群里 @ · Jev 判断是「请求准备东西」", requester_id="10003", requester_name="Kiriko",
    )
    app.identity.remember_sync(scope="global", text="测试要记住的事：回读验证", reason="测试")
    app.identity.group_write(G1, "这个群最近爱在周五晚上分享折腾成果。")

    app._seeded = {"tid": tid, "tid_running": t2, "tid_queued": t3, "goal_id": gid_agent}


@pytest.fixture
def app(tmp_path: Path):
    """真 MaiWorkApp（同步 fixture：pytest-asyncio 标记模式下 async fixture 不能被同步测试用）。

    app.start/stop 都是协程：这里起一条专属事件循环跑完它们，测试体里的
    tools.call / gate.execute 仍由 pytest-asyncio 自己的循环跑（后台派工在测试体
    里发生，不会跨循环）。
    """
    from CharTyr_MaiWork.maiwork.tools_admin import _APPROVED_CTX, _CHAT_CTX

    _CHAT_CTX.set((0, 0))
    _APPROVED_CTX.set(None)
    a = MaiWorkApp(FakeCtx({}), _raw(tmp_path / "data", _free_port()), plugin_dir=Path(__file__).resolve().parents[1])
    # app 内部把 asyncio.get_event_loop() 记在对象上，start/stop 必须在同一条
    # 事件循环上跑；用 set_event_loop 让它落到这条专属循环（stop 时内部也靠它）。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(a.start())
    except Exception:
        asyncio.set_event_loop(None)
        loop.close()
        raise
    try:
        assert a.admin_pending is not None
        assert a.feeds is not None and a.profiles is not None and a.identity is not None
        _seed(a)
        yield a
    finally:
        try:
            loop.run_until_complete(a.stop())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def _chat_id(app: MaiWorkApp) -> int:
    row = app.store.read().execute("SELECT id FROM admin_chats ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return int(row["id"])


async def _call(app: MaiWorkApp, name: str, args: dict, gid: str = G1):
    """直接调一个工具（admin 角色）。需要先确认的返回 ok + 「已请求管理员确认」。"""
    return await app.tools.call(name, args, _ctx(gid))


async def _call_confirmed(app: MaiWorkApp, name: str, args: dict, gid: str = G1):
    """调一个要确认的工具：先写小票 → 管理员点同意 → 返回真执行的结果。"""
    gate = app.admin_pending
    chat = AdminChat(app).create(G1)
    cid = int(chat["id"])
    gate.bind_chat(cid, 0)
    try:
        first = await app.tools.call(name, args, _ctx(gid))
        assert first.ok, f"{name} 第一次调用失败：{first.error}"
        assert "已请求管理员确认" in first.output, f"{name} 应该先写小票，实际：{first.output}"
        pid = int(first.data["pending_id"])
        ticket = gate._row(pid)
        assert ticket is not None and ticket["tool"] == name
        assert int(ticket["chat_id"]) == cid, f"{name} 的小票没记在这段对话上"
        return await gate.execute(pid, True), ticket
    finally:
        gate.bind_chat(0, 0)


class TestCoverageComplete:
    def test_all_admin_tools_covered(self, app) -> None:
        """specs('admin') 的名单必须和覆盖清单一致：新增工具漏测就报错。"""
        names = {(s.get("function") or {}).get("name") for s in app.tools.specs("admin")}
        assert names == ALL_ADMIN_TOOLS, (
            f"缺测：{sorted(names - ALL_ADMIN_TOOLS)}；名单里多出来的：{sorted(ALL_ADMIN_TOOLS - names)}"
        )


class TestReadTools:
    @pytest.mark.asyncio
    async def test_list_groups(self, app) -> None:
        r = await _call(app, "list_groups", {})
        assert r.ok and G1 in r.output and "tinker" in r.output

    @pytest.mark.asyncio
    async def test_group_overview(self, app) -> None:
        r = await _call(app, "group_overview", {"group_id": G1})
        assert r.ok, r.error
        assert "画像条目 2 条" in r.output
        assert "工作区 tinker" in r.output
        assert "任务" in r.output and "在盯的事" in r.output

    @pytest.mark.asyncio
    async def test_read_profile(self, app) -> None:
        r = await _call(app, "read_profile", {"group_id": G1})
        assert r.ok and "分享夜" in r.output and "自部署" in r.output

    @pytest.mark.asyncio
    async def test_list_focus(self, app) -> None:
        r = await _call(app, "list_focus", {"group_id": G1})
        assert r.ok and "10003" in r.output

    @pytest.mark.asyncio
    async def test_list_news_real_batches(self, app) -> None:
        """线上踩过的坑：news_view(admin=True) 返回的是批次列表，必须展开批次里的 items。"""
        r = await _call(app, "list_news", {"group_id": G1})
        assert r.ok, r.error
        assert "railway.new" in r.output and "SQLite" in r.output
        assert "https://spam.example.com/x" in r.output, f"条目要带出链接 / 站点：{r.output}"
        assert "筛掉 1 条" in r.output, f"要把被筛掉的条数说出来：{r.output}"
        assert not re.search(r"- \[(?:new|pool|被筛)\]\s*$", r.output, re.M), f"还有空壳条目：{r.output}"

    @pytest.mark.asyncio
    async def test_list_ideas(self, app) -> None:
        r = await _call(app, "list_ideas", {"group_id": G1})
        assert r.ok and "巡检" in r.output

    @pytest.mark.asyncio
    async def test_list_tasks(self, app) -> None:
        r = await _call(app, "list_tasks", {"group_id": G1})
        assert r.ok and app._seeded["tid"] in r.output and "插件踩坑文档" in r.output

    @pytest.mark.asyncio
    async def test_task_detail_real(self, app) -> None:
        """线上踩过的坑：detail_view 没有 group_id 键，服务群检查永远失败。"""
        tid = app._seeded["tid"]
        r = await _call(app, "task_detail", {"task_id": tid})
        assert r.ok, r.error
        assert tid in r.output and "插件踩坑文档" in r.output
        assert "完成" in r.output or "completed" in r.output
        assert "至少覆盖 6 个主题" in r.output

    @pytest.mark.asyncio
    async def test_task_detail_wrong_group_refused(self, app) -> None:
        """detail_view 没有 group_id 键（线上踩过：view.get('group_id') 永远是空），
        必须从 Tasks.get 的行上拿群号做服务群检查——别群的任务要拒。"""
        bad = app.tasks.create("555444333", title="别群的任务", req="x", criteria=[], source="admin_chat")
        r = await _call(app, "task_detail", {"task_id": str(bad)})
        assert not r.ok and "不是服务群" in r.error

    @pytest.mark.asyncio
    async def test_list_goals(self, app) -> None:
        r = await _call(app, "list_goals", {"group_id": G1})
        assert r.ok and "插件开发的坑" in r.output and "Jev 对照" in r.output

    @pytest.mark.asyncio
    async def test_list_requests(self, app) -> None:
        r = await _call(app, "list_requests", {"group_id": G1})
        assert r.ok and "NAS 方案" in r.output and "Kiriko" in r.output

    @pytest.mark.asyncio
    async def test_read_chat(self, app) -> None:
        r = await _call(app, "read_chat", {"group_id": G1})
        assert r.ok, r.error
        assert "白嫖一台临时机器" in r.output and "阿柒" in r.output

    @pytest.mark.asyncio
    async def test_read_logs(self, app) -> None:
        r = await _call(app, "read_logs", {"failed_only": True})
        assert r.ok, r.error
        assert "限流" in r.output and "429" in r.output

    @pytest.mark.asyncio
    async def test_get_rules(self, app) -> None:
        r = await _call(app, "get_rules", {})
        assert r.ok and "delivery.push_per_day" in r.output

    @pytest.mark.asyncio
    async def test_get_identity(self, app) -> None:
        r = await _call(app, "get_identity", {})
        assert r.ok, r.error
        assert "SOUL.md" in r.output and "MEMORY.md" in r.output

    @pytest.mark.asyncio
    async def test_list_extensions(self, app) -> None:
        r = await _call(app, "list_extensions", {})
        assert r.ok, r.error
        assert r.output  # 有或没有扩展都要有一句像样的话


class TestWriteTools:
    @pytest.mark.asyncio
    async def test_profile_edit_add_and_lock(self, app) -> None:
        r = await _call(app, "profile_edit", {"group_id": G1, "action": "add", "category": "ongoing", "text": "在做：给群文档收尾"})
        assert r.ok, r.error
        eid = int(r.data["id"])
        texts = [e["text"] for e in app.profiles.entries(G1)]
        assert "在做：给群文档收尾" in texts
        r2 = await _call(app, "profile_edit", {"group_id": G1, "action": "lock", "entry_id": eid})
        assert r2.ok, r2.error

    @pytest.mark.asyncio
    async def test_profile_bulk_delete_confirmed(self, app) -> None:
        eid = app.profiles.add_entry(G1, "recent", "一条马上要被删的条目")
        r, ticket = await _call_confirmed(app, "profile_bulk_delete", {"group_id": G1, "entry_ids": [eid]})
        assert r.ok, r.error
        assert "已删 1 条" in r.output
        assert "1 条画像" in ticket["summary"]

    @pytest.mark.asyncio
    async def test_focus_edit(self, app) -> None:
        r = await _call(app, "focus_edit", {"group_id": G1, "user_id": "10005", "action": "add"})
        assert r.ok and "一直关注" in r.output
        rows = app.store.read().execute(
            "SELECT user_id FROM focus_members WHERE group_id=? AND removed=0 AND user_id='10005'", (G1,)
        ).fetchall()
        assert rows, "10005 要被设成关注"

    @pytest.mark.asyncio
    async def test_set_feeds_pref(self, app) -> None:
        r = await _call(app, "set_feeds_pref", {"group_id": G1, "text": "只要硬件相关"})
        assert r.ok and "只要硬件相关" in r.output
        assert app.feeds.pref(G1) == "只要硬件相关"

    @pytest.mark.asyncio
    async def test_rss_add_then_remove_confirmed(self, app) -> None:
        r = await _call(app, "rss_add", {"group_id": G1, "url": "https://example.com/feed.xml", "title": "测试源"})
        assert r.ok, r.error
        from CharTyr_MaiWork.maiwork import rss as _rss

        rows = _rss.list_feeds(app.store, G1)
        assert len(rows) == 1
        fid = str(rows[0]["id"])
        r2, ticket = await _call_confirmed(app, "rss_remove", {"group_id": G1, "feed_id": fid})
        assert r2.ok, r2.error
        assert _rss.list_feeds(app.store, G1) == []
        assert "RSS 源" in ticket["summary"]

    @pytest.mark.asyncio
    async def test_block_domain(self, app) -> None:
        r = await _call(app, "block_domain", {"domain": "spam.example.com", "blocked": True})
        assert r.ok and "已屏蔽 spam.example.com" in r.output
        r2 = await _call(app, "block_domain", {"domain": "spam.example.com", "blocked": False})
        assert r2.ok and "取消屏蔽" in r2.output

    @pytest.mark.asyncio
    async def test_set_rules_safe_and_loosen(self, app) -> None:
        # 收紧：不确认直接生效
        r = await _call(app, "set_rules", {"patch": {"delivery": {"push_per_day": 2}}})
        assert r.ok, r.error
        assert "已请求管理员确认" not in r.output
        # 放宽：要确认，同意后才生效
        r2, ticket = await _call_confirmed(app, "set_rules", {"patch": {"delivery": {"push_per_day": 9}}})
        assert r2.ok, r2.error
        assert "每天推送上限 → 9" in ticket["summary"] and "push_per_day" not in ticket["summary"]
        assert app.get_settings().delivery.push_per_day == 9

    @pytest.mark.asyncio
    async def test_identity_edit_group(self, app) -> None:
        r = await _call(app, "identity_edit", {"kind": "group", "group_id": G1, "text": "新的群工作记忆"})
        assert r.ok, r.error
        got = app.identity.group_read(G1)
        assert "新的群工作记忆" in str(got or "")

    @pytest.mark.asyncio
    async def test_remember(self, app) -> None:
        r = await _call(app, "remember", {"text": "管理员说：记住这个真模块集成测试", "group_id": G1})
        assert r.ok, r.error
        assert "已记住" in r.output


class TestDangerTools:
    @pytest.mark.asyncio
    async def test_send_group_message_confirmed(self, app) -> None:
        r, ticket = await _call_confirmed(app, "send_group_message", {"group_id": G1, "text": "大家好，这是测试一句"})
        assert r.ok, r.error
        assert "发件箱" in r.output
        row = app.store.read().execute("SELECT * FROM outbox WHERE group_id=?", (G1,)).fetchone()
        assert row is not None and "大家好" in row["payload"]
        assert G1 in ticket["summary"] and "大家好" in ticket["summary"]

    @pytest.mark.asyncio
    async def test_group_notice_send_confirmed(self, app) -> None:
        calls: list[tuple[str, str]] = []

        async def _notice(gid: str, content: str, announce: object = None) -> None:
            calls.append((gid, content))

        app.group_space = type("FakeSpace", (), {"send_notice": staticmethod(_notice)})()
        r, ticket = await _call_confirmed(app, "group_notice_send", {"group_id": G1, "content": "这周周报已整理"})
        assert r.ok, r.error
        assert calls == [(G1, "这周周报已整理")]
        assert "公告" in ticket["summary"]

    @pytest.mark.asyncio
    async def test_group_file_manage_confirmed(self, app) -> None:
        calls: list[tuple] = []

        async def _mkdir(gid: str, name: str, folder: object = None) -> None:
            calls.append(("mkdir", gid, name))

        app.group_space = type("FakeSpace", (), {"create_folder": staticmethod(_mkdir)})()
        r, _ticket = await _call_confirmed(app, "group_file_manage", {"group_id": G1, "action": "mkdir", "name": "成品"})
        assert r.ok, r.error
        assert calls == [("mkdir", G1, "成品")]

    @pytest.mark.asyncio
    async def test_group_album_upload_confirmed(self, app, tmp_path: Path) -> None:
        # 拿一个绝对存在的目录当 workspace_root 注入（config 的默认根在开发机上建不出来）
        from CharTyr_MaiWork.maiwork.config import load_settings

        ws_root = tmp_path / "wsroot"
        (ws_root / "tinker").mkdir(parents=True)
        raw = _raw(tmp_path / "data2", _free_port())
        raw["environments"] = {"workspace_root": str(ws_root)}
        settings, _ = load_settings(raw)
        app._settings = settings
        # 换了 settings 就重判一次执行方式（生产走 start/update_config，两条都会重判）；
        # 不然 get_settings 会把工作区根按上一次判定的结果修正，注入的 ws_root 不生效
        app._detect_local_capability()
        assert "mw_effective" not in app.__dict__ or app.__dict__.pop("_mw_effective", None) is None
        pic = ws_root / "tinker" / "pic.png"
        pic.write_bytes(b"\x89PNG\r\n\x1a\n")
        calls: list[tuple] = []

        async def _upload(gid: str, album: str, path: str) -> None:
            calls.append((gid, album, path))

        app.group_space = type("FakeSpace", (), {"upload_to_album": staticmethod(_upload)})()
        r, _t = await _call_confirmed(app, "group_album_upload", {"group_id": G1, "path": str(pic), "album_id": "A1"})
        assert r.ok, r.error
        assert calls and calls[0][2] == str(pic)

    @pytest.mark.asyncio
    async def test_skill_delete_confirmed(self, app) -> None:
        from CharTyr_MaiWork.maiwork.skills import Skills

        skills = Skills(app.get_settings().data_dir)
        root = Path(app.get_settings().data_dir) / "skills" / "demo-skill"
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 测试用\n---\n正文", encoding="utf-8")
        assert skills.read("demo-skill") is not None
        r, _t = await _call_confirmed(app, "skill_delete", {"name": "demo-skill"})
        assert r.ok, r.error
        assert skills.read("demo-skill") is None

    @pytest.mark.asyncio
    async def test_mcp_toggle_and_delete_no_ext(self, app) -> None:
        """没接扩展管理时要说清「现在开不了也关不了」，而不是报错崩掉。"""
        if app.extensions is None:
            r = await _call(app, "mcp_toggle", {"name": "x", "enabled": True})
            assert not r.ok and "没接 MCP 扩展" in r.error
            r2 = await _call(app, "mcp_delete", {"name": "x"})
            assert not r2.ok
            return
        pytest.skip("这个环境接了扩展管理")


class TestNowTools:
    @pytest.mark.asyncio
    async def test_run_news_now_starts_bg_job(self, app) -> None:
        called: list[str] = []

        async def _fake_prepare(gid: str) -> int:
            called.append(str(gid))
            await asyncio.sleep(0.05)  # 慢一点，互斥断言时这批还在跑
            return 0

        app.feeds.prepare_news = _fake_prepare
        r = await _call(app, "run_news_now", {"group_id": G1})
        assert r.ok, r.error
        assert "备一批资讯" in r.output and "后台" in r.output
        assert (G1, "news_manual") in app._running_jobs
        for _ in range(50):
            if called:
                break
            await asyncio.sleep(0.02)
        assert called == [G1], "真的要调到 Feeds.prepare_news"
        # 同群互斥：这批还没结，第二次直接回「已经在备料了」
        r2 = await _call(app, "run_news_now", {"group_id": G1})
        assert not r2.ok and "已经在备料" in r2.error

    @pytest.mark.asyncio
    async def test_make_idea_now_starts_bg_job(self, app) -> None:
        called: list[str] = []

        async def _fake_make(gid: str):
            called.append(str(gid))
            return None

        app.feeds.make_idea = _fake_make
        r = await _call(app, "make_idea_now", {"group_id": G1})
        assert r.ok, r.error
        assert (G1, "idea_manual") in app._running_jobs
        for _ in range(50):
            if called:
                break
            await asyncio.sleep(0.02)
        assert called == [G1]

    @pytest.mark.asyncio
    async def test_refresh_profile_starts_bg_job(self, app) -> None:
        """新工具：现在重新整理一个群的画像。后台跑、同群互斥、只限服务群。"""
        called: list[str] = []

        async def _fake_refresh(gid: str, *, force: bool = False) -> bool:
            called.append(str(gid))
            assert force is True, "管理员点名的要强制刷"
            await asyncio.sleep(0.05)
            return True

        app.profiles.refresh = _fake_refresh
        r = await _call(app, "refresh_profile", {"group_id": G1})
        assert r.ok, r.error
        assert "已经开始重新整理" in r.output and G1 in r.output
        assert "网页群画像" in r.output
        assert (G1, "profile_manual") in app._running_jobs
        # 同群互斥：连调只起一个
        r2 = await _call(app, "refresh_profile", {"group_id": G1})
        assert not r2.ok and "已经在整理" in r2.error
        for _ in range(100):
            if called:
                break
            await asyncio.sleep(0.02)
        assert called == [G1], "真的要调到画像刷新入口（profiles.refresh）"

    @pytest.mark.asyncio
    async def test_refresh_profile_rejects_non_served(self, app) -> None:
        r = await _call(app, "refresh_profile", {"group_id": G_BAD})
        assert not r.ok and "不是服务群" in r.error

    @pytest.mark.asyncio
    async def test_refresh_profile_no_confirm(self, app) -> None:
        """refresh_profile 不对外发东西，不该写小票。"""
        async def _fake_refresh(gid: str, *, force: bool = False) -> bool:
            return True

        app.profiles.refresh = _fake_refresh
        gate = app.admin_pending
        chat = AdminChat(app).create(G1)
        gate.bind_chat(int(chat["id"]), 0)
        try:
            r = await _call(app, "refresh_profile", {"group_id": G1})
        finally:
            gate.bind_chat(0, 0)
        assert r.ok, r.error
        assert "已请求管理员确认" not in r.output
        assert gate.pending() == []


class TestTaskGoalRequestTools:
    @pytest.mark.asyncio
    async def test_create_task_needs_confirm_then_creates(self, app) -> None:
        """create_task 进了确认清单：同意前只写票不建任务，同意后才建并开工。"""
        title = "把本周聊的部署脚本整理成一页"
        r, ticket = await _call_confirmed(
            app, "create_task", {"group_id": G1, "title": title, "request": "整理成一页网页，手机上能看"}
        )
        assert "子 agent" in ticket["summary"] and G1 in ticket["summary"] and title[:10] in ticket["summary"]
        assert r.ok, r.error
        tid = str(r.data["task_id"])
        task = app.tasks.get(tid)
        assert task is not None and task["status"] == "queued"
        assert task["source"] == "admin_chat"

    @pytest.mark.asyncio
    async def test_create_task_ticket_only_before_approve(self, app) -> None:
        gate = app.admin_pending
        chat = AdminChat(app).create(G1)
        gate.bind_chat(int(chat["id"]), 0)
        try:
            before = app.store.read().execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
            r = await _call(app, "create_task", {"group_id": G1, "title": "先不建", "request": "先不建"})
            after = app.store.read().execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        finally:
            gate.bind_chat(0, 0)
        assert r.ok and "已请求管理员确认" in r.output
        assert before == after, "同意前不能建任务"

    @pytest.mark.asyncio
    async def test_create_goal_needs_confirm_then_creates(self, app) -> None:
        """create_goal 进了确认清单：agent 目标后台检查时会往群里发汇报 / 完成话
        （coordinator 的 goal:{id}:report / goal:{id}:done），等于间接对外说话。"""
        r, ticket = await _call_confirmed(app, "create_goal", {"group_id": G1, "title": "盯着服务器状态",
                                                               "criteria": ["每天出一次状态摘要"], "check_hours": 12})
        assert G1 in ticket["summary"] and "盯着服务器状态" in ticket["summary"]
        assert r.ok, r.error
        row = app.store.read().execute(
            "SELECT id, kind, state, title FROM goals WHERE kind='agent' AND title='盯着服务器状态'"
        ).fetchone()
        assert row is not None and row["state"] == "active"

    @pytest.mark.asyncio
    async def test_create_goal_ticket_only_before_approve(self, app) -> None:
        gate = app.admin_pending
        chat = AdminChat(app).create(G1)
        gate.bind_chat(int(chat["id"]), 0)
        try:
            r = await _call(app, "create_goal", {"group_id": G1, "title": "先不立", "criteria": ["x"]})
        finally:
            gate.bind_chat(0, 0)
        assert r.ok and "已请求管理员确认" in r.output
        row = app.store.read().execute("SELECT COUNT(*) AS c FROM goals WHERE title='先不立'").fetchone()
        assert row["c"] == 0, "同意前不能立目标"

    @pytest.mark.asyncio
    async def test_approve_request_confirmed(self, app) -> None:
        out = app.approvals.create(
            G1, kind="task", title="整理一份周报", quote="@bot 整周报",
            via="测试", requester_id="10004", requester_name="zzh",
        )
        rid = str(out["id"])
        r, ticket = await _call_confirmed(app, "approve_request", {"request_id": rid})
        assert r.ok, r.error
        assert "已批准" in r.output
        row = app.store.read().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "approved"
        assert rid in ticket["summary"]

    @pytest.mark.asyncio
    async def test_reject_request_direct(self, app) -> None:
        out = app.approvals.create(
            G1, kind="task", title="帮我把群文件都删了", quote="@bot 删群文件",
            via="测试", requester_id="10005", requester_name="捣蛋鬼",
        )
        rid = str(out["id"])
        r = await _call(app, "reject_request", {"request_id": rid, "reason": "不合适"})
        assert r.ok, r.error
        row = app.store.read().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_cancel_task_direct_and_stops_runner(self, app) -> None:
        tid = app._seeded["tid_running"]
        cancelled: list[str] = []
        app.cancel_task_run = lambda tid_: cancelled.append(str(tid_))
        r = await _call(app, "cancel_task", {"task_id": tid, "reason": "不要了"})
        assert r.ok, r.error
        assert app.tasks.get(tid)["status"] == "cancelled"
        assert tid in cancelled, "取消要顺着统一入口把正在跑的子 agent 停掉"


class TestCancelStopsSubAgent:
    """取消任务要真的把正在跑的子 agent 停掉（线上踩过：网页取消后还跑了 40 秒）。"""

    @pytest.mark.asyncio
    async def test_cancel_stops_running_worker_no_new_model_call(self, app) -> None:
        from CharTyr_MaiWork.maiwork.workers import Workers

        calls = {"n": 0}

        class _ScriptedModels:
            """假模型：前 3 步各回一个工具调用（不 submit），记调用次数。"""

            def settings(self):
                return type("S", (), {"ready": lambda self: True})()

            async def chat(self, role=None, messages=None, **kw):
                calls["n"] += 1
                return type(
                    "R",
                    (),
                    {
                        "text": "",
                        "tool_calls": [
                            {"id": f"c{calls['n']}", "function": {"name": "noop_tool", "arguments": "{}"}}
                        ],
                    },
                )()

        tools = app.tools

        async def _noop(ctx, args):
            return type("TR", (), {"ok": True, "output": "好", "error": "", "data": None})()

        from CharTyr_MaiWork.maiwork.tools import Tool

        tools.register(Tool(name="noop_tool", description="占位", parameters={"type": "object", "properties": {}},
                            roles=frozenset({"worker"}), handler=_noop))
        workers = Workers(_ScriptedModels(), tools, tasks=app.tasks)
        tid = app.tasks.create(G1, title="跑到一半被取消", req="多步任务", criteria=[], source="admin_chat")
        app.tasks.transition(tid, "running")

        async def _run() -> None:
            await workers.run("干活", group_id=G1, tools=["noop_tool"], task_id=tid, max_steps=10)

        runner = asyncio.get_running_loop().create_task(_run())
        # 让它先跑 2 步
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert calls["n"] >= 2, "先让子 agent 真的调了几次模型"
        # 取消（走统一入口）
        app.tasks.transition(tid, "cancelled", reason="管理员取消")
        app.cancel_task_run(tid)
        before = calls["n"]
        await asyncio.wait_for(runner, timeout=5)
        await asyncio.sleep(0.05)
        assert calls["n"] == before, "取消后不能再有新的模型调用"
        # 没有交付、没有往群里发任何东西
        rows = app.store.read().execute(
            "SELECT COUNT(*) AS c FROM outbox WHERE task_id=?", (tid,)
        ).fetchone()
        assert rows["c"] == 0, "取消后 outbox 不能有这条任务的消息"

    @pytest.mark.asyncio
    async def test_cancel_task_run_cancels_bg_task(self, app) -> None:
        """app.cancel_task_run 把登记在 _bg_jobs 的 maiwork-task-<tid> 协程停掉。"""
        tid = "T-停我"
        started = asyncio.Event()
        release = asyncio.Event()

        async def _run() -> None:
            started.set()
            await release.wait()

        app._running_tasks.add(tid)
        t = asyncio.get_running_loop().create_task(_run(), name=f"maiwork-task-{tid}")
        app._bg_jobs.add(t)
        await asyncio.wait_for(started.wait(), timeout=2)
        stopped = app.cancel_task_run(tid)
        assert stopped is True
        await asyncio.wait_for(asyncio.shield(asyncio.gather(t, return_exceptions=True)), timeout=2)
        assert t.cancelled() or t.done()
        assert tid not in app._running_tasks


class TestGuardsOnRealApp:
    @pytest.mark.asyncio
    async def test_non_admin_role_rejected(self, app) -> None:
        r = await app.tools.call("list_groups", {}, _ctx(role="worker"))
        assert not r.ok

    @pytest.mark.asyncio
    async def test_non_served_group_rejected(self, app) -> None:
        for name, args in (
            ("group_overview", {"group_id": G_BAD}),
            ("list_news", {"group_id": G_BAD}),
            ("read_chat", {"group_id": G_BAD}),
            ("run_news_now", {"group_id": G_BAD}),
            ("make_idea_now", {"group_id": G_BAD}),
            ("refresh_profile", {"group_id": G_BAD}),
        ):
            r = await _call(app, name, args)
            assert not r.ok and "不是服务群" in r.error, f"{name} 没挡住非服务群：{r.ok} {r.output}{r.error}"
