"""tools_admin.py 测试：角色门、非服务群拒绝、密钥工具不存在、危险动作请确认、写工具效果。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from CharTyr_MaiWork import clock, rules
from CharTyr_MaiWork.approvals import Approvals
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.goals import Goals
from CharTyr_MaiWork.models import Models
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tasks import Tasks
from CharTyr_MaiWork.tools import ToolContext, Tools
from CharTyr_MaiWork.tools_admin import _CHAT_CTX

G1 = "900000001"
G2 = "123456789"


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": "127.0.0.1:0", "password": "x", "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }
    for section, values in over.items():
        if isinstance(values, dict) and isinstance(raw.get(section), dict):
            raw[section] = {**raw[section], **values}
        else:
            raw[section] = values
    return raw


class _Svc:
    """管理员对话工具要的那个「服务包」的最小实现（真 Store + 真数据层模块）。"""

    def __init__(self, tmp_path: Path, *, workspace_root: Path | None = None) -> None:
        over: dict = {}
        if workspace_root is not None:
            over["environments"] = {"workspace_root": str(workspace_root)}
        self._raw = _raw_config(tmp_path / "data", **over)
        self._settings, _ = load_settings(self._raw)
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "maiwork.db")
        self.store.migrate()
        self.models = Models(self.store, self.get_settings)
        self.tools = Tools(self.store)
        self.tasks = Tasks(self.store, self.get_settings, self.tools)
        self.goals = Goals(self.store, self.get_settings)
        self.approvals = Approvals(self.store, self.get_settings, self.tasks, self.goals)
        self.spawns: list[str] = []
        self.started_calls = 0

    def get_settings(self):
        return self._settings

    def base_settings(self):
        return self._settings

    def spawn_run_task(self, tid: str) -> None:
        self.spawns.append(str(tid))

    def _models_ready(self) -> bool:
        return True


def _ctx(gid: str = G1, role: str = "admin") -> ToolContext:
    return ToolContext(group_id=gid, actor="主模型（管理员对话）", role=role)


def _pending_rows(store: Store) -> list[dict]:
    rows = store.read().execute("SELECT * FROM admin_chat_pending ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def svc(tmp_path: Path) -> _Svc:
    # 门闸的「当前对话 / 已批准指纹」是模块级 ContextVar：每个测试开始时复位，
    # 防止上一个测试任务里的绑定 / 批准泄漏到这个测试（pytest-asyncio 可能复用主任务）
    from CharTyr_MaiWork.tools_admin import _APPROVED_CTX, _CHAT_CTX, register_admin_tools

    _CHAT_CTX.set((0, 0))
    _APPROVED_CTX.set(None)
    s = _Svc(tmp_path, workspace_root=tmp_path / "ws")
    now = clock.now()
    with s.store.tx() as conn:
        for gid in (G1, G2):
            conn.execute(
                "INSERT INTO groups (group_id, workspace, created) VALUES (?, ?, ?)"
                " ON CONFLICT(group_id) DO NOTHING",
                (gid, "tinker", now),
            )
    pending = register_admin_tools(s.tools, s)
    s.admin_pending = pending
    return s


class TestRolesAndGuards:
    @pytest.mark.asyncio
    async def test_admin_tool_rejects_worker(self, svc: _Svc) -> None:
        r = await svc.tools.call("list_groups", {}, _ctx(role="worker"))
        assert not r.ok
        assert "不允许" in r.error

    @pytest.mark.asyncio
    async def test_admin_tool_rejects_main(self, svc: _Svc) -> None:
        r = await svc.tools.call("list_groups", {}, _ctx(role="main"))
        assert not r.ok

    @pytest.mark.asyncio
    async def test_specs_only_admin(self, svc: _Svc) -> None:
        admin_specs = {s["function"]["name"] for s in svc.tools.specs("admin")}
        assert "list_groups" in admin_specs
        assert "send_group_message" in admin_specs
        worker_specs = {s["function"]["name"] for s in svc.tools.specs("worker")}
        main_specs = {s["function"]["name"] for s in svc.tools.specs("main")}
        assert not (admin_specs & worker_specs)
        assert not (admin_specs & main_specs)

    def test_no_secret_tools(self, svc: _Svc) -> None:
        """任何名字像密钥/端点/宿主管理的工具都不许存在。"""
        names = {s["function"]["name"] for s in svc.tools.specs("admin")}
        bad = ("secret", "api_key", "apikey", "token", "password", "endpoint",
               "base_url", "restart", "maibot", "planner", "plugin")
        for n in names:
            low = n.lower()
            for frag in bad:
                assert frag not in low, f"工具 {n} 名字里不该出现 {frag}"

    @pytest.mark.asyncio
    async def test_non_served_group_rejected(self, svc: _Svc) -> None:
        for tool, args in (
            ("group_overview", {"group_id": "99999"}),
            ("read_chat", {"group_id": "99999"}),
            ("profile_edit", {"group_id": "99999", "action": "add", "category": "recent", "text": "x"}),
            ("set_feeds_pref", {"group_id": "99999", "text": "x"}),
            ("send_group_message", {"group_id": "99999", "text": "hi"}),
            ("create_task", {"group_id": "99999", "title": "t", "request": "r"}),
        ):
            r = await svc.tools.call(tool, args, _ctx())
            assert not r.ok, tool
            assert "服务群" in r.error, (tool, r.error)

    @pytest.mark.asyncio
    async def test_list_groups(self, svc: _Svc) -> None:
        r = await svc.tools.call("list_groups", {}, _ctx())
        assert r.ok
        assert G1 in r.output and G2 in r.output

    @pytest.mark.asyncio
    async def test_every_call_logged(self, svc: _Svc) -> None:
        await svc.tools.call("list_groups", {}, _ctx())
        rows = svc.store.read().execute(
            "SELECT actor, tool FROM tool_calls WHERE tool='list_groups'"
        ).fetchall()
        assert len(rows) == 1
        assert "管理员" in str(rows[0]["actor"])


class TestReadTools:
    @pytest.mark.asyncio
    async def test_read_logs(self, svc: _Svc) -> None:
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, model, group_id, ok, status, ms, error)"
                " VALUES (?, 'feeds.score', 'main', 'm', ?, 0, 500, 10, '服务端错误')",
                (clock.now(), G1),
            )
            conn.execute(
                "INSERT INTO model_calls (ts, purpose, role, model, group_id, ok, status, ms)"
                " VALUES (?, 'worker', 'worker', 'w', ?, 1, 200, 10)",
                (clock.now(), G1),
            )
        r = await svc.tools.call("read_logs", {"failed_only": True, "limit": 10}, _ctx())
        assert r.ok
        assert "服务端错误" in r.output
        assert "worker" not in r.output

    @pytest.mark.asyncio
    async def test_list_requests(self, svc: _Svc) -> None:
        svc.approvals.create(
            G1, kind="task", title="做个东西", quote="要个东西", via="消息",
            requester_id="20002", requester_name="群友甲",
        )
        r = await svc.tools.call("list_requests", {"status": "pending"}, _ctx())
        assert r.ok
        assert "做个东西" in r.output
        assert isinstance(r.data, list) and r.data[0]["title"] == "做个东西"

    @pytest.mark.asyncio
    async def test_get_rules_and_identity(self, svc: _Svc) -> None:
        r = await svc.tools.call("get_rules", {}, _ctx())
        assert r.ok
        assert "push_per_day" in r.output
        r2 = await svc.tools.call("get_identity", {}, _ctx())
        assert r2.ok  # identity 没建 → 中文兜底，不炸


class TestWriteTools:
    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)  # set_rules 放宽时要写小票；小票必须有归属的对话

    def _wire_profiles(self, svc: _Svc) -> None:
        from fakes import FakeHost

        from CharTyr_MaiWork.profile import Profiles

        svc.profiles = Profiles(svc.store, FakeHost(), svc.models, svc.get_settings)

    @pytest.mark.asyncio
    async def test_set_feeds_pref(self, svc: _Svc) -> None:
        r = await svc.tools.call("set_feeds_pref", {"group_id": G1, "text": "多看本地生活"}, _ctx())
        assert r.ok
        assert svc.store.kv_get(f"feeds.pref.{G1}") == "多看本地生活"

    @pytest.mark.asyncio
    async def test_profile_edit_add_and_lock(self, svc: _Svc) -> None:
        self._wire_profiles(svc)
        r = await svc.tools.call(
            "profile_edit", {"group_id": G1, "action": "add", "category": "interest", "text": "喜欢折腾小工具"}, _ctx()
        )
        assert r.ok, r.error
        entries = svc.profiles.entries(G1)
        assert any(e["text"] == "喜欢折腾小工具" for e in entries)
        eid = int(r.data["id"])
        r2 = await svc.tools.call("profile_edit", {"group_id": G1, "action": "unlock", "entry_id": eid}, _ctx())
        assert r2.ok
        e = [x for x in svc.profiles.entries(G1) if x["id"] == eid][0]
        assert int(e["locked"]) == 0
        # 别的群的条目不许动
        r3 = await svc.tools.call("profile_edit", {"group_id": G2, "action": "delete", "entry_id": eid}, _ctx())
        assert not r3.ok

    @pytest.mark.asyncio
    async def test_block_domain(self, svc: _Svc) -> None:
        r = await svc.tools.call("block_domain", {"domain": "Bad-Site.COM", "blocked": True}, _ctx())
        assert r.ok, r.error
        assert "bad-site.com" in (svc.store.kv_get("feeds.blocked_domains") or [])
        r2 = await svc.tools.call("block_domain", {"domain": "bad-site.com", "blocked": False}, _ctx())
        assert r2.ok
        assert "bad-site.com" not in (svc.store.kv_get("feeds.blocked_domains") or [])

    @pytest.mark.asyncio
    async def test_set_rules_apply(self, svc: _Svc) -> None:
        # 收紧类的改动不用确认（默认 3 → 2），直接生效
        r = await svc.tools.call("set_rules", {"patch": {"delivery": {"push_per_day": 2}}}, _ctx())
        assert r.ok, r.error
        assert svc.get_settings().delivery.push_per_day == 2
        assert "push_per_day=2" in r.output

    @pytest.mark.asyncio
    async def test_set_rules_bad_value(self, svc: _Svc) -> None:
        r = await svc.tools.call("set_rules", {"patch": {"delivery": {"push_per_day": 99}}}, _ctx())
        assert not r.ok
        assert r.error

    @pytest.mark.asyncio
    async def test_set_rules_needs_confirm_when_loosening_approval(self, svc: _Svc) -> None:
        r = await svc.tools.call("set_rules", {"patch": {"approval": {"required": False}}}, _ctx())
        assert r.ok
        assert "已请求管理员确认" in r.output
        rows = _pending_rows(svc.store)
        assert len(rows) == 1
        assert rows[0]["tool"] == "set_rules"
        assert rules.read_override(svc.store) == {}  # 没落库

    @pytest.mark.asyncio
    async def test_create_task_needs_confirm_then_creates(self, svc: _Svc) -> None:
        """create_task 进了 CONFIRM_TOOLS（任务开工后 coordinator 会自动往群里发消息）：
        同意前只写小票不建任务，同意后才建并开工。真模块端到端版本在 test_tools_admin_real.py。"""
        from CharTyr_MaiWork.tools_admin import _CHAT_CTX

        _bind_chat(svc, cid=900)
        try:
            r = await svc.tools.call(
                "create_task", {"group_id": G1, "title": "整理一份周报", "request": "把这周群里聊的事整理成周报"}, _ctx()
            )
            assert r.ok, r.error
            assert "已请求管理员确认" in r.output
            assert svc.tasks.list_view(G1) == [], "同意前不能建任务"
            rows = _pending_rows(svc.store)
            assert len(rows) == 1 and rows[0]["tool"] == "create_task"
            assert "子 agent" in rows[0]["summary"] and G1 in rows[0]["summary"]
            done = await svc.admin_pending.execute(int(r.data["pending_id"]), True)
        finally:
            _CHAT_CTX.set((0, 0))
        assert done.ok, done.error
        tid = str(done.data["task_id"])
        task = svc.tasks.get(tid)
        assert task is not None
        assert task["status"] == "queued"
        assert task["source"] == "admin_chat"
        assert "管理员" in str(task["requester_name"])
        assert svc.spawns == [tid]

    @pytest.mark.asyncio
    async def test_create_goal_needs_confirm_then_creates(self, svc: _Svc) -> None:
        """create_goal 进了 CONFIRM_TOOLS（agent 目标后台检查会往群里发汇报 / 完成话）。"""
        from CharTyr_MaiWork.tools_admin import _CHAT_CTX

        _bind_chat(svc, cid=901)
        try:
            r = await svc.tools.call(
                "create_goal",
                {"group_id": G1, "title": "盯着服务器状态", "criteria": ["每天能出一次状态摘要"], "check_hours": 12},
                _ctx(),
            )
            assert r.ok, r.error
            assert "已请求管理员确认" in r.output
            assert svc.store.read().execute("SELECT COUNT(*) AS c FROM goals").fetchone()["c"] == 0
            rows = _pending_rows(svc.store)
            assert len(rows) == 1 and rows[0]["tool"] == "create_goal"
            assert G1 in rows[0]["summary"]
            done = await svc.admin_pending.execute(int(r.data["pending_id"]), True)
        finally:
            _CHAT_CTX.set((0, 0))
        assert done.ok, done.error
        rows = svc.store.read().execute("SELECT id, kind, state, by_text FROM goals").fetchall()
        assert len(rows) == 1
        assert str(rows[0]["kind"]) == "agent"
        assert "管理员" in str(rows[0]["by_text"])

    @pytest.mark.asyncio
    async def test_approve_and_reject_request(self, svc: _Svc) -> None:
        res = svc.approvals.create(
            G1, kind="task", title="做个 A", quote="要 A", via="消息",
            requester_id="20002", requester_name="群友甲",
        )
        rid = str(res["id"])
        # 批准群友派的活要写小票（M6：模型不能替管理员拍板）；点同意后才真批
        r = await svc.tools.call("approve_request", {"request_id": rid}, _ctx())
        assert r.ok, r.error
        assert "已请求管理员确认" in r.output
        pid = int(_pending_rows(svc.store)[0]["id"])
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok, out.error
        row = svc.store.read().execute("SELECT status, task_id FROM requests WHERE id=?", (rid,)).fetchone()
        assert str(row["status"]) == "approved"
        assert row["task_id"]
        r2 = await svc.tools.call("reject_request", {"request_id": "R-999", "reason": "不合适"}, _ctx())
        assert not r2.ok

    @pytest.mark.asyncio
    async def test_cancel_task(self, svc: _Svc) -> None:
        tid = svc.tasks.create(
            G1, title="t", req="r", criteria=[], source="request", requester_id="1", requester_name="n", status="queued"
        )
        r = await svc.tools.call("cancel_task", {"task_id": tid, "reason": "管理员说算了"}, _ctx())
        assert r.ok, r.error
        assert svc.tasks.get(tid)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_run_news_now(self, svc: _Svc) -> None:
        class _Feeds:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def prepare_news(self, gid: str) -> int:
                self.calls.append(str(gid))
                return 1

        svc.feeds = _Feeds()
        r = await svc.tools.call("run_news_now", {"group_id": G1}, _ctx())
        assert r.ok, r.error
        await asyncio.sleep(0.3)
        assert svc.feeds.calls == [G1]

    @pytest.mark.asyncio
    async def test_mcp_toggle_not_ready(self, svc: _Svc) -> None:
        r = await svc.tools.call("mcp_toggle", {"name": "xxx", "enabled": False}, _ctx())
        assert not r.ok
        assert r.error


class TestConfirmGate:
    """要确认的动作：当时不执行，写 admin_chat_pending，返回固定话术。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        if _CHAT_CTX.get()[0] <= 0:  # 并发绑定测试自己管 ContextVar，别抢
            _bind_chat(svc)

    @pytest.mark.asyncio
    async def test_parallel_chats_pending_belongs_to_correct_chat(self, svc: _Svc) -> None:
        """两个管理员对话同时调用工具，待确认项不能串到另一段对话。"""
        gate = svc.admin_pending
        with svc.store.tx() as conn:
            for cid in (101, 202):
                conn.execute(
                    "INSERT INTO admin_chats(id,title,group_id,created,updated) VALUES (?, 't', ?, 1, 1)",
                    (cid, G1),
                )
        barrier = asyncio.Event()

        async def go(cid: int) -> None:
            gate.bind_chat(cid, cid + 1)
            if cid == 101:
                barrier.set()
                await asyncio.sleep(0.01)
            else:
                await barrier.wait()
            gate.queue("send_group_message", {"group_id": G1, "text": str(cid)})

        await asyncio.gather(go(101), go(202))
        rows = _pending_rows(svc.store)
        assert {(r["chat_id"], json.loads(r["args"])["text"]) for r in rows} == {(101, "101"), (202, "202")}

    @pytest.mark.asyncio
    async def test_model_cannot_forge_approved_arg(self, svc: _Svc) -> None:
        """模型参数不可信：伪造 _approved=true 绝不能跳过管理员确认。"""
        r = await svc.tools.call(
            "set_rules", {"patch": {"approval": {"required": False}}, "_approved": True}, _ctx()
        )
        assert r.ok and "已请求管理员确认" in r.output
        assert rules.read_override(svc.store) == {}
        assert len(_pending_rows(svc.store)) == 1

    @pytest.mark.asyncio
    async def test_send_group_message_queued_not_sent(self, svc: _Svc) -> None:
        r = await svc.tools.call(
            "send_group_message", {"group_id": G1, "text": "大家好，这是管理员让我说的一句话"}, _ctx()
        )
        assert r.ok
        assert "已请求管理员确认" in r.output
        rows = _pending_rows(svc.store)
        assert len(rows) == 1
        assert rows[0]["tool"] == "send_group_message"
        assert rows[0]["status"] == "pending"
        args = json.loads(rows[0]["args"])
        assert args["group_id"] == G1
        n = svc.store.read().execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"]
        assert int(n) == 0

    @pytest.mark.asyncio
    async def test_groupspace_write_queued(self, svc: _Svc) -> None:
        r = await svc.tools.call("group_notice_send", {"group_id": G1, "content": "这周周报已整理"}, _ctx())
        assert r.ok
        assert "已请求管理员确认" in r.output

    @pytest.mark.asyncio
    async def test_rss_remove_queued(self, svc: _Svc) -> None:
        from CharTyr_MaiWork import rss as _rss

        _rss.add_feed(svc.store, G1, url="https://a.com/feed", title="A", feed_id="f1", now=clock.now())
        r = await svc.tools.call("rss_remove", {"group_id": G1, "feed_id": "f1"}, _ctx())
        assert r.ok
        assert "已请求管理员确认" in r.output
        assert any(f["id"] == "f1" for f in _rss.list_feeds(svc.store, G1))

    @pytest.mark.asyncio
    async def test_profile_bulk_delete_needs_confirm(self, svc: _Svc) -> None:
        r = await svc.tools.call(
            "profile_bulk_delete", {"group_id": G1, "entry_ids": [1, 2, 3, 4]}, _ctx()
        )
        assert r.ok
        assert "已请求管理员确认" in r.output


# ---------------------------------------------------------------------------
# 安全补洞（群内容可能注入主模型，以下每条都有 PoC 对应）
# ---------------------------------------------------------------------------


class _Space:
    """假群空间：只记调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_notice(self, gid, content, announce=None):
        self.calls.append(("notice", gid, content))

    async def delete_file(self, gid, fid):
        self.calls.append(("delete", gid, fid))

    async def rename_file(self, gid, fid, name):
        self.calls.append(("rename", gid, fid, name))

    async def move_file(self, gid, fid, folder):
        self.calls.append(("move", gid, fid, folder))

    async def create_folder(self, gid, name, folder=None):
        self.calls.append(("mkdir", gid, name, folder))

    async def upload_to_album(self, gid, album, path):
        self.calls.append(("album", gid, album, path))


class _OutboxRec:
    def __init__(self) -> None:
        self.items: list[tuple] = []

    def enqueue(self, key, gid, kind, payload, task_id=None):
        self.items.append((key, gid, kind, payload))
        return len(self.items)

    async def flush(self, now=0.0):
        return None


def _bind_chat(svc: _Svc, cid: int = 1, gid: str = G1) -> int:
    """建一段真实对话并把门闸绑上去（小票必须有归属，没绑定不写票）。"""
    from CharTyr_MaiWork.tools_admin import _CHAT_CTX

    with svc.store.tx() as conn:
        conn.execute(
            "INSERT INTO admin_chats (id, title, group_id, created, updated, archived)"
            " VALUES (?, 'x', ?, 0, 0, 0) ON CONFLICT(id) DO NOTHING",
            (cid, gid),
        )
    if _CHAT_CTX.get()[0] != cid:  # 只动没绑对的部分，别盖住并发子任务的绑定
        _CHAT_CTX.set((cid, 1))
    return cid


class TestH1SetRulesLoosen:
    """H1：patch 里任何 approval.* 键、推送变多、睡觉变窄、开话题变勤，都要先写小票。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "patch,frag",
        (
            ({"approval": {"admins": ["qq:10001", "qq:666666"]}}, "admins"),
            ({"approval": {"exempt_groups": [f"qq:{G1}"]}}, "exempt_groups"),
            ({"approval": {"exempt_users": ["qq:666666"]}}, "exempt_users"),
        ),
    )
    async def test_approval_lists_need_confirm(self, svc: _Svc, patch: dict, frag: str) -> None:
        r = await svc.tools.call("set_rules", {"patch": patch}, _ctx())
        assert r.ok, r.error
        assert "已请求管理员确认" in r.output
        rows = _pending_rows(svc.store)
        assert len(rows) == 1 and rows[0]["tool"] == "set_rules"
        assert frag in rows[0]["summary"]
        assert rules.read_override(svc.store) == {}  # 没同意前不落库

    @pytest.mark.asyncio
    async def test_delivery_topics_loosen_need_confirm(self, svc: _Svc) -> None:
        # 推送上限「变大」才要确认（默认 3；>5 无条件要，<=5 但比当前大也要）
        cases = [
            ({"delivery": {"push_per_day": 4}}, True),
            ({"delivery": {"quiet_hours": "23:30-07:30"}}, True),  # 睡觉变窄
            ({"delivery": {"quiet_hours": "22:00-08:00"}}, False),  # 变长不算放宽
            ({"topics": {"per_day": 3}}, True),
            ({"topics": {"min_gap_hours": 2}}, True),
            ({"topics": {"enabled": True}}, False),  # 和默认值一样，不算改
        ]
        for patch, want_ticket in cases:
            with svc.store.tx() as conn:
                conn.execute("DELETE FROM admin_chat_pending")
                svc.store.kv_set(conn, "rules.override", {})  # 每个用例从默认规则起算
            r = await svc.tools.call("set_rules", {"patch": patch}, _ctx())
            assert r.ok, (patch, r.error)
            rows = _pending_rows(svc.store)
            if want_ticket:
                assert len(rows) == 1, patch
                assert rules.read_override(svc.store) == {}
            else:
                assert rows == [], patch

    @pytest.mark.asyncio
    async def test_approved_ticket_applies(self, svc: _Svc) -> None:
        patch = {"approval": {"admins": ["qq:10001", "qq:666666"]}}
        r = await svc.tools.call("set_rules", {"patch": patch}, _ctx())
        assert r.ok
        pid = int(_pending_rows(svc.store)[0]["id"])
        assert svc.approvals.is_admin("666666") is False
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok, out.error
        assert rules.read_override(svc.store).get("approval", {}).get("admins") == ["qq:10001", "qq:666666"]
        assert svc.approvals.is_admin("666666") is True

    @pytest.mark.asyncio
    async def test_rejected_ticket_leaves_rules_untouched(self, svc: _Svc) -> None:
        patch = {"approval": {"exempt_users": ["qq:666666"]}}
        await svc.tools.call("set_rules", {"patch": patch}, _ctx())
        pid = int(_pending_rows(svc.store)[0]["id"])
        out = await svc.admin_pending.execute(pid, False)
        assert out.ok
        assert rules.read_override(svc.store) == {}
        assert "666666" not in svc.get_settings().approval.exempt_users


class TestM1SummaryListsAllFields:
    """M1：混合 patch 的小票摘要要列出全部字段，不能只写放宽那一项。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    @pytest.mark.asyncio
    async def test_mixed_patch_summary_shows_every_field(self, svc: _Svc) -> None:
        patch = {"delivery": {"push_per_day": 6}, "approval": {"admins": ["qq:10001", "qq:666666"]}}
        r = await svc.tools.call("set_rules", {"patch": patch}, _ctx())
        assert r.ok
        rows = _pending_rows(svc.store)
        assert len(rows) == 1
        summary = rows[0]["summary"]
        assert "approval.admins" in summary
        assert "qq:666666" in summary
        assert "delivery.push_per_day" in summary

    @pytest.mark.asyncio
    async def test_long_summary_truncated_with_count(self, svc: _Svc) -> None:
        patch = {
            "delivery": {"push_per_day": 6, "quiet_hours": "23:30-07:30"},
            "topics": {"per_day": 9, "min_gap_hours": 1},
            "approval": {
                "required": False,
                "remind": False,
                "admins": ["qq:10001", "qq:222222", "qq:333333"],
                "exempt_groups": [f"qq:{G1}", f"qq:{G2}"],
                "exempt_users": ["qq:444444"],
            },
        }
        r = await svc.tools.call("set_rules", {"patch": patch}, _ctx())
        assert r.ok, r.error
        summary = _pending_rows(svc.store)[0]["summary"]
        assert len(summary) <= 200
        assert "共改 9 项" in summary


class TestM2ApprovalFingerprint:
    """M2：「已批准」是本次动作的指纹，不是一面大家都能用的旗子。"""

    @staticmethod
    def _seed_ticket(svc: _Svc, tool: str, args: dict, cid: int = 1) -> int:
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO admin_chats (id, title, group_id, created, updated, archived)"
                " VALUES (?, 'x', ?, 0, 0, 0) ON CONFLICT(id) DO NOTHING",
                (cid, G1),
            )
            cur = conn.execute(
                "INSERT INTO admin_chat_pending (chat_id, msg_id, tool, args, summary, status, created)"
                " VALUES (?, 0, ?, ?, ?, 'pending', ?)",
                (cid, tool, json.dumps(args, ensure_ascii=False), f"手工票 {tool}", clock.now()),
            )
            return int(cur.lastrowid)

    @pytest.mark.asyncio
    async def test_spawned_child_does_not_inherit_approval(self, svc: _Svc) -> None:
        """execute 期间 handler 派出的子任务再调确认类工具：必须写新票，不能免确认。"""
        from CharTyr_MaiWork.tools import Tool, ToolResult

        _bind_chat(svc)
        svc.outbox = _OutboxRec()
        seen: dict = {}

        async def spawner(ctx_, args):
            async def bg():
                r = await svc.tools.call(
                    "send_group_message", {"group_id": G1, "text": "后台自己发的"}, ctx_
                )
                seen["bg_out"] = r.output or r.error

            asyncio.get_running_loop().create_task(bg())
            return ToolResult(ok=True, output="已派后台")

        svc.tools.register(Tool(
            name="spawner", description="派个后台任务。",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"admin"}), handler=spawner,
        ))
        pid = self._seed_ticket(svc, "spawner", {})
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok
        await asyncio.sleep(0.1)
        assert "已请求管理员确认" in str(seen.get("bg_out"))
        assert svc.outbox.items == []  # 子任务那句没真发出去
        new_tickets = [p for p in _pending_rows(svc.store) if p["tool"] == "send_group_message"]
        assert len(new_tickets) == 1

    @pytest.mark.asyncio
    async def test_same_tool_different_args_still_needs_confirm(self, svc: _Svc) -> None:
        """同一张票执行时，同工具但参数不一样的再调用也要重新写票。"""
        from CharTyr_MaiWork.tools import Tool, ToolResult

        _bind_chat(svc)
        svc.outbox = _OutboxRec()
        seen: dict = {}

        async def resend(ctx_, args):
            r = await svc.tools.call(
                "send_group_message", {"group_id": G1, "text": "换了个内容"}, ctx_
            )
            seen["out"] = r.output or r.error
            return ToolResult(ok=True, output="done")

        svc.tools.register(Tool(
            name="resend", description="里面再发一句别的。",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"admin"}), handler=resend,
        ))
        pid = self._seed_ticket(svc, "resend", {})
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok
        assert "已请求管理员确认" in str(seen.get("out"))
        assert svc.outbox.items == []

    @pytest.mark.asyncio
    async def test_exact_replay_inside_handler_passes(self, svc: _Svc) -> None:
        """票里就是 send_group_message，handler 按同样参数再走一遍 _need_confirm → 放行。"""
        svc.outbox = _OutboxRec()
        _bind_chat(svc)
        r = await svc.tools.call("send_group_message", {"group_id": G1, "text": "大家好"}, _ctx())
        assert r.ok and "已请求管理员确认" in r.output
        pid = int(_pending_rows(svc.store)[0]["id"])
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok, out.error
        assert len(svc.outbox.items) == 1
        assert svc.outbox.items[0][1] == G1

    @pytest.mark.asyncio
    async def test_plain_call_still_needs_confirm(self, svc: _Svc) -> None:
        """普通调用（不在任何 execute 窗口里）照旧写票。"""
        svc.outbox = _OutboxRec()
        _bind_chat(svc)
        r = await svc.tools.call("send_group_message", {"group_id": G1, "text": "hi"}, _ctx())
        assert "已请求管理员确认" in r.output
        assert svc.outbox.items == []


class TestM3AlbumUploadPath:
    """M3：传群相册的 path 必须是本群工作区里的真实文件（拒符号链接 / 穿越 / 区外）。"""

    @pytest.fixture(autouse=True)
    def _space(self, svc: _Svc) -> None:
        svc.group_space = _Space()

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    def _workspace_file(self, svc: _Svc, name: str = "pic.png") -> Path:
        root = Path(svc.get_settings().workspace_root)
        d = root / svc.get_settings().workspace_of(G1)
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_bytes(b"\x89PNG fake")
        return f

    @pytest.mark.asyncio
    async def test_outside_workspace_rejected_no_ticket(self, svc: _Svc, tmp_path: Path) -> None:
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"x")
        r = await svc.tools.call(
            "group_album_upload", {"group_id": G1, "path": str(outside), "album_id": "A1"}, _ctx()
        )
        assert not r.ok
        assert "工作区" in r.error
        assert _pending_rows(svc.store) == []
        assert svc.group_space.calls == []

    @pytest.mark.asyncio
    async def test_dotdot_traversal_rejected(self, svc: _Svc) -> None:
        self._workspace_file(svc)
        root = Path(svc.get_settings().workspace_root)
        evil = str(root / "tinker" / ".." / ".." / "etc" / "passwd")
        r = await svc.tools.call(
            "group_album_upload", {"group_id": G1, "path": evil, "album_id": "A1"}, _ctx()
        )
        assert not r.ok
        assert _pending_rows(svc.store) == []

    @pytest.mark.asyncio
    async def test_symlink_rejected(self, svc: _Svc, tmp_path: Path) -> None:
        good = self._workspace_file(svc)
        target = tmp_path / "real.png"
        target.write_bytes(b"x")
        link = good.parent / "link.png"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("这个文件系统建不了符号链接")
        r = await svc.tools.call(
            "group_album_upload", {"group_id": G1, "path": str(link), "album_id": "A1"}, _ctx()
        )
        assert not r.ok
        assert "符号链接" in r.error
        assert _pending_rows(svc.store) == []

    @pytest.mark.asyncio
    async def test_other_group_workspace_rejected(self, svc: _Svc) -> None:
        """在服务群根目录下、但属于别的群的工作区，也不许传。"""
        root = Path(svc.get_settings().workspace_root)
        other = root / svc.get_settings().workspace_of(G2)
        other.mkdir(parents=True, exist_ok=True)
        f = other / "pic.png"
        f.write_bytes(b"x")
        r = await svc.tools.call(
            "group_album_upload", {"group_id": G1, "path": str(f), "album_id": "A1"}, _ctx()
        )
        assert not r.ok
        assert _pending_rows(svc.store) == []

    @pytest.mark.asyncio
    async def test_valid_path_writes_ticket_then_uploads(self, svc: _Svc) -> None:
        good = self._workspace_file(svc)
        r = await svc.tools.call(
            "group_album_upload", {"group_id": G1, "path": str(good), "album_id": "A1"}, _ctx()
        )
        assert r.ok, r.error
        assert "已请求管理员确认" in r.output
        assert svc.group_space.calls == []
        pid = int(_pending_rows(svc.store)[0]["id"])
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok, out.error
        assert svc.group_space.calls == [("album", G1, "A1", str(good))]


class TestM4MakeIdeaMutex:
    """M4：make_idea_now 同一群同时只跑一个（优先走 app 方法，回落也要互斥）。"""

    class _Feeds:
        def __init__(self) -> None:
            self.runs: list[str] = []
            self.inflight = 0
            self.max_inflight = 0

        async def make_idea(self, gid: str) -> int:
            self.runs.append(str(gid))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0.2)
            self.inflight -= 1
            return 1

    @pytest.mark.asyncio
    async def test_fallback_mutex_same_group(self, svc: _Svc) -> None:
        feeds = self._Feeds()
        svc.feeds = feeds
        rs = [await svc.tools.call("make_idea_now", {"group_id": G1}, _ctx()) for _ in range(8)]
        assert sum(bool(r.ok) for r in rs) == 1  # 只有第一个起得来
        assert sum("已经在想" in (r.error or "") for r in rs if not r.ok) == 7
        await asyncio.sleep(0.4)
        assert feeds.runs == [G1]
        assert feeds.max_inflight == 1

    @pytest.mark.asyncio
    async def test_app_method_preferred(self, svc: _Svc) -> None:
        calls: list[str] = []

        def make_idea_now(gid: str) -> dict:
            calls.append(str(gid))
            return {"started": False, "reason": "这个群已经在想构想了，等它想完"}

        svc.make_idea_now = make_idea_now
        svc.feeds = self._Feeds()
        r = await svc.tools.call("make_idea_now", {"group_id": G1}, _ctx())
        assert not r.ok
        assert "已经在想" in (r.error or "")
        assert calls == [G1]
        assert svc.feeds.runs == []  # 没走回落

    @pytest.mark.asyncio
    async def test_real_app_make_idea_now_mutex(self, tmp_path: Path) -> None:
        """真 app 的 make_idea_now：同群互斥、登记进 _running_jobs、和定时 idea 互斥。"""
        from fakes import FakeCtx

        from CharTyr_MaiWork.app import MaiWorkApp

        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
            "console": {"listen": "127.0.0.1:0", "password": "x", "public_url": ""},
            "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])
        gate = asyncio.Event()

        class _Feeds:
            def __init__(self) -> None:
                self.runs: list[str] = []

            async def make_idea(self, gid: str) -> int:
                self.runs.append(str(gid))
                await gate.wait()
                return 1

        await app.start()
        try:
            app.feeds = _Feeds()
            r1 = app.make_idea_now(G1)
            assert r1["started"] is True
            assert (G1, "idea_manual") in app._running_jobs
            r2 = app.make_idea_now(G1)
            assert r2["started"] is False and "已经在想" in r2["reason"]
            gate.set()
            await asyncio.sleep(0.1)
            assert app.feeds.runs == [G1]
            # 跑完（算做过）之后能再开
            r3 = app.make_idea_now(G1)
            assert r3["started"] is True
            gate.set()
            await asyncio.sleep(0.1)
        finally:
            gate.set()
            await app.stop()

    @pytest.mark.asyncio
    async def test_real_app_make_idea_now_blocks_scheduled(self, tmp_path: Path) -> None:
        """手动的还在跑时，定时巡检到点的 idea 这一轮先不开。"""
        from fakes import FakeCtx

        from CharTyr_MaiWork.app import MaiWorkApp

        raw = {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}]},
            "console": {"listen": "127.0.0.1:0", "password": "x", "public_url": ""},
            "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
        app = MaiWorkApp(FakeCtx({}), raw, plugin_dir=Path(__file__).resolve().parents[1])

        class _Scheduler:
            def due(self, gid, now, last_msg_ts=0.0):
                return ["idea"]

        class _Feeds:
            async def make_idea(self, gid: str) -> int:
                return 1

        await app.start()
        try:
            app.feeds = _Feeds()
            app.scheduler = _Scheduler()
            app._running_jobs.add((G1, "idea_manual"))
            before = set(app._bg_jobs)
            await app._schedule_round(G1, clock.now(), None)
            assert set(app._bg_jobs) == before  # 没开新活
            assert (G1, "idea") not in app._running_jobs
        finally:
            await app.stop()


class TestM5FocusGroupTicketArgs:
    """M5：焦点群里没给 group_id 的票，群号要补进票的 args，同意后能真执行。"""

    @pytest.mark.asyncio
    async def test_focus_group_filled_into_ticket_args(self, svc: _Svc) -> None:
        svc.outbox = _OutboxRec()
        _bind_chat(svc, cid=7, gid=G1)
        r = await svc.tools.call("send_group_message", {"text": "没给群号的一句"}, _ctx())
        assert r.ok, r.error
        assert "已请求管理员确认" in r.output
        row = _pending_rows(svc.store)[0]
        args = json.loads(row["args"])
        assert args.get("group_id") == G1
        out = await svc.admin_pending.execute(int(row["id"]), True)
        assert out.ok, out.error
        assert len(svc.outbox.items) == 1
        assert svc.outbox.items[0][1] == G1

    @pytest.mark.asyncio
    async def test_notice_focus_group_filled(self, svc: _Svc) -> None:
        svc.group_space = _Space()
        _bind_chat(svc, cid=8, gid=G1)
        r = await svc.tools.call("group_notice_send", {"content": "公告"}, _ctx())
        assert r.ok and "已请求管理员确认" in r.output
        row = _pending_rows(svc.store)[0]
        assert json.loads(row["args"]).get("group_id") == G1
        out = await svc.admin_pending.execute(int(row["id"]), True)
        assert out.ok, out.error
        assert svc.group_space.calls == [("notice", G1, "公告")]


class TestM6ApproveRequestConfirm:
    """M6：批准群友派的活必须管理员点第二次头；拒绝不用。"""

    @pytest.fixture(autouse=True)
    def _chat(self, svc: _Svc) -> None:
        _bind_chat(svc)

    def _new_request(self, svc: _Svc, gid: str = G1) -> str:
        res = svc.approvals.create(
            gid, kind="task", title="群友的活", quote="要做", via="消息",
            requester_id="20002", requester_name="群友甲",
        )
        return str(res["id"])

    @pytest.mark.asyncio
    async def test_approve_queues_ticket_not_approved(self, svc: _Svc) -> None:
        rid = self._new_request(svc)
        r = await svc.tools.call("approve_request", {"request_id": rid}, _ctx())
        assert r.ok, r.error
        assert "已请求管理员确认" in r.output
        row = svc.store.read().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        assert str(row["status"]) == "pending"
        ticket = _pending_rows(svc.store)[0]
        assert ticket["tool"] == "approve_request"
        assert rid in ticket["summary"]

    @pytest.mark.asyncio
    async def test_approve_after_confirm_lands_task(self, svc: _Svc) -> None:
        rid = self._new_request(svc)
        await svc.tools.call("approve_request", {"request_id": rid}, _ctx())
        pid = int(_pending_rows(svc.store)[0]["id"])
        out = await svc.admin_pending.execute(pid, True)
        assert out.ok, out.error
        row = svc.store.read().execute("SELECT status, task_id FROM requests WHERE id=?", (rid,)).fetchone()
        assert str(row["status"]) == "approved"
        assert row["task_id"]
        assert svc.spawns == [str(row["task_id"])]

    @pytest.mark.asyncio
    async def test_approve_rejected_ticket_keeps_pending(self, svc: _Svc) -> None:
        rid = self._new_request(svc)
        await svc.tools.call("approve_request", {"request_id": rid}, _ctx())
        pid = int(_pending_rows(svc.store)[0]["id"])
        await svc.admin_pending.execute(pid, False)
        row = svc.store.read().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        assert str(row["status"]) == "pending"

    @pytest.mark.asyncio
    async def test_reject_request_no_confirm_needed(self, svc: _Svc) -> None:
        rid = self._new_request(svc)
        r = await svc.tools.call("reject_request", {"request_id": rid, "reason": "不合适"}, _ctx())
        assert r.ok, r.error
        assert "已请求管理员确认" not in r.output
        assert _pending_rows(svc.store) == []
        row = svc.store.read().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        assert str(row["status"]) == "rejected"


class TestL1RejectServed:
    """L1：reject_request 也要查服务群。"""

    @pytest.mark.asyncio
    async def test_reject_non_served_group_rejected(self, svc: _Svc) -> None:
        now = clock.now()
        with svc.store.tx() as conn:
            conn.execute(
                "INSERT INTO requests (id, group_id, kind, title, quote, via, icon, requester_id,"
                " requester_name, message_id, status, created, updated)"
                " VALUES ('R-X', '555000111', 'task', '别的群的活', 'q', 'group', 'magnifier',"
                " '666', '路人', '', 'pending', ?, ?)",
                (now, now),
            )
        r = await svc.tools.call("reject_request", {"request_id": "R-X"}, _ctx())
        assert not r.ok
        assert "服务群" in r.error
        row = svc.store.read().execute("SELECT status FROM requests WHERE id='R-X'").fetchone()
        assert str(row["status"]) == "pending"


class TestL2ServedOnlyReads:
    """L2：不带群号时，list_requests / read_logs 只给服务群的数据。"""

    G3 = "555000111"

    def _seed(self, svc: _Svc) -> None:
        now = clock.now()
        with svc.store.tx() as conn:
            for rid, gid in (("R1", G1), ("R3", self.G3)):
                conn.execute(
                    "INSERT INTO requests (id, group_id, kind, title, quote, via, icon, requester_id,"
                    " requester_name, message_id, status, created, updated)"
                    " VALUES (?, ?, 'task', ?, 'q', 'group', 'magnifier', '666', '路人', '', 'pending', ?, ?)",
                    (rid, gid, f"{gid} 的活", now, now),
                )
            for gid, ok in ((G1, 0), (self.G3, 0), ("", 0)):
                conn.execute(
                    "INSERT INTO model_calls (ts, purpose, role, model, group_id, ok, status, ms, error)"
                    " VALUES (?, 'feeds.score', 'main', 'm', ?, ?, 500, 10, ?)",
                    (now, gid, ok, f"{gid or '无群'} 的报错"),
                )

    @pytest.mark.asyncio
    async def test_list_requests_without_group_only_served(self, svc: _Svc) -> None:
        self._seed(svc)
        r = await svc.tools.call("list_requests", {}, _ctx(gid=""))
        assert r.ok
        assert f"{G1} 的活" in r.output
        assert f"{self.G3} 的活" not in r.output
        gids = {str(d["group_id"]) for d in (r.data or [])}
        assert gids == {G1}

    @pytest.mark.asyncio
    async def test_read_logs_only_served_groups(self, svc: _Svc) -> None:
        self._seed(svc)
        r = await svc.tools.call("read_logs", {"limit": 10}, _ctx(gid=""))
        assert r.ok
        assert f"{G1} 的报错" in r.output
        assert f"{self.G3} 的报错" not in r.output
        assert "无群 的报错" in r.output  # 全局（没群号）的调用是管理员自己的，照样看得到
        gids = {str(d["group_id"]) for d in (r.data or [])}
        assert self.G3 not in gids


class TestL5ExecuteTimeout:
    """L5：门闸真执行时有超时；卡死的工具标 failed，回中文错误。"""

    @pytest.mark.asyncio
    async def test_slow_handler_marks_failed(self, svc: _Svc) -> None:
        from CharTyr_MaiWork.tools import Tool, ToolResult

        async def slow(ctx_, args):
            await asyncio.sleep(5)
            return ToolResult(ok=True, output="跑完了")

        svc.tools.register(Tool(
            name="slow_tool", description="卡住的工具。",
            parameters={"type": "object", "properties": {}},
            roles=frozenset({"admin"}), handler=slow, timeout_s=0.1,
        ))
        pid = TestM2ApprovalFingerprint._seed_ticket(svc, "slow_tool", {})
        out = await svc.admin_pending.execute(pid, True)
        assert not out.ok
        assert "超时" in out.error
        row = svc.store.read().execute(
            "SELECT status, result FROM admin_chat_pending WHERE id=?", (pid,)
        ).fetchone()
        assert str(row["status"]) == "failed"
        assert "超时" in str(row["result"])


class TestQueueNeedsBoundChat:
    """没绑定对话时不偷偷新建：直接回中文错误，不落票、不建对话。"""

    @pytest.mark.asyncio
    async def test_queue_without_bind_chat_refused(self, svc: _Svc) -> None:
        gate = svc.admin_pending
        gate.chat_id = 0
        gate.msg_id = 0
        r = await svc.tools.call("send_group_message", {"group_id": G1, "text": "无主的票"}, _ctx())
        assert not r.ok
        assert "对话" in r.error
        assert _pending_rows(svc.store) == []
        n = svc.store.read().execute("SELECT COUNT(*) AS c FROM admin_chats").fetchone()["c"]
        assert int(n) == 0


class TestGroupSpaceAlbumPath:
    """tools_groupspace 的 group_album_upload 也限本群工作区。"""

    @pytest.mark.asyncio
    async def test_groupspace_upload_path_checked(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.tools_groupspace import register_groupspace_tools

        store = Store(tmp_path / "g.db")
        store.migrate()
        tools = Tools(store)
        space = _Space()
        ws_root = tmp_path / "ws"
        (ws_root / "tinker").mkdir(parents=True)
        good = ws_root / "tinker" / "pic.png"
        good.write_bytes(b"x")
        settings = _gs_settings(tmp_path)
        register_groupspace_tools(
            tools, space, announce=lambda gid, text: None, get_settings=lambda: settings
        )
        ctx = ToolContext(group_id=G1, actor="主模型", role="main")
        bad = await tools.call(
            "group_album_upload", {"path": str(tmp_path / "outside.png"), "album_id": "A"}, ctx
        )
        assert not bad.ok
        assert space.calls == []
        ok = await tools.call("group_album_upload", {"path": str(good), "album_id": "A"}, ctx)
        assert ok.ok, ok.error
        assert space.calls == [("album", G1, "A", str(good))]


def _gs_settings(tmp_path: Path):
    """群空间工具要的最小设置：workspace_root + workspace_of。"""
    raw = _raw_config(tmp_path / "data")
    raw["environments"] = {"workspace_root": str(tmp_path / "ws")}
    s, _problems = load_settings(raw)  # 端口 0 的提示不算问题（测试不真开网页）
    return s
