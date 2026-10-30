"""每群一个「谁能批本群的活（含免批）」（docs/18 §五，0.8.0）。

先写先红：group_approval.py 还没建、approvals/group_admins/commands 还各读各的名单时，
这一整个文件应当失败（导入错误 / 行为不对），再写实现让它变绿。

归一后的契约（一句话）：**一个群一份 kv["group_approval.<群号>"]**，字段
`{approvers, exempt_users, exempt_group, required}`；首次访问从旧来源（全局
`approval.admins` ∪ 旧 `kv["group_admins.<群号>"]`，免批按本群平台从旧
`exempt_users` / `exempt_groups` / `required` 种下）惰性幂等迁移一次，并删掉旧按群名单；
**种下以后只认这一份**，不再回头和全局名单动态并集。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeHost, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.auto_review import AutoReviewer
from CharTyr_MaiWork.maiwork.commands import Commands
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.group_admins import GroupAdmins
from CharTyr_MaiWork.maiwork.group_approval import GroupApprovals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks

G1 = "900000001"
G2 = "123456789"
TG = "-1001234567890"
QQ_ADMIN = "10001"
LEGACY_ADMIN = "30003"
MEMBER = "20002"

ADMIN_PW = "总管理员密码-不要外传-1234"
G1_PW = "群一管理员密码-abcd"


# ======================================================================
# 数据层脚手架
# ======================================================================


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "unified.db")
    store.migrate()
    return store


def _settings(serve=("qq:" + G1, "qq:" + G2), *, approval=None):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": g} for g in serve]},
    }
    if approval is not None:
        raw["approval"] = approval
    settings, problems = load_settings(raw)
    assert not problems, problems
    return settings


def _world(tmp_path: Path, *, serve=("qq:" + G1, "qq:" + G2), approval=None, store=None):
    store = store or _store(tmp_path)
    settings = _settings(serve, approval=approval)
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    approvals = Approvals(store, lambda: settings, tasks, goals)
    gv = GroupApprovals(store, get_settings=lambda: settings)
    ga = GroupAdmins(store, get_settings=lambda: settings)
    return SimpleNamespace(
        store=store, settings=settings, tasks=tasks, goals=goals,
        approvals=approvals, gv=gv, ga=ga,
    )


def _create(world, gid=G1, **over):
    kw = dict(
        kind="task", title="整理资料", quote="q", via="群里 @",
        requester_id=MEMBER, requester_name="阿柒",
    )
    kw.update(over)
    return world.approvals.create(gid, **kw)


# ======================================================================
# 1. 迁移种子：旧名单不丢、幂等、删旧键
# ======================================================================


class TestSeed:
    def test_seed_merges_global_and_legacy_group_admins(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": [QQ_ADMIN]})
        with world.store.tx() as conn:
            world.store.kv_set(conn, f"group_admins.{G1}", ["30003", "qq:40004"])
        rec = world.gv.get(G1)
        assert rec.approvers == ("qq:10001", "qq:30003", "qq:40004")
        # 旧按群名单已并入 → 删掉旧键（惰性迁移）
        assert world.store.kv_get(f"group_admins.{G1}", None) is None

    def test_seed_keeps_legacy_only_list(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": []})
        with world.store.tx() as conn:
            world.store.kv_set(conn, f"group_admins.{G1}", ["30003"])
        assert world.gv.get(G1).approvers == ("qq:30003",)

    def test_seed_is_idempotent_and_existing_record_wins(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": [QQ_ADMIN]})
        first = world.gv.get(G1)
        assert first.approvers == ("qq:10001",)
        # 手工改成只有 9 号能批
        with world.store.tx() as conn:
            world.store.kv_set(conn, f"group_approval.{G1}", {
                "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": True,
            })
        again = world.gv.get(G1)
        assert again.approvers == ("qq:9",)  # 不重新种回全局管理员
        assert again.approvers == world.gv.get(G1).approvers

    def test_seed_does_not_create_record_for_unserved_group(self, tmp_path: Path) -> None:
        world = _world(tmp_path, serve=("qq:" + G1,), approval={"admins": [QQ_ADMIN]})
        assert world.gv.get("999999").approvers == ()
        assert world.store.kv_get("group_approval.999999", None) is None

    def test_seed_platform_filters_exempt(self, tmp_path: Path) -> None:
        world = _world(
            tmp_path,
            serve=("qq:" + G1, "telegram:" + TG),
            approval={
                "required": True,
                "admins": ["qq:" + QQ_ADMIN, "telegram:1000000003"],
                "exempt_users": ["qq:10001", "telegram:1000000003"],
                "exempt_groups": ["telegram:" + TG],
            },
        )
        tg = world.gv.get(TG)
        assert tg.exempt_users == ("telegram:1000000003",)  # 只种本群平台的免批人
        assert tg.exempt_group is True                       # exempt_groups 按本群平台种
        qq = world.gv.get(G1)
        assert qq.exempt_users == ("qq:10001",)
        assert qq.exempt_group is False

    def test_seed_required_flag(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": False, "admins": [QQ_ADMIN]})
        assert world.gv.get(G1).required is False


# ======================================================================
# 2. 种下以后只认新记录（不回全局并集）
# ======================================================================


class TestNoGlobalBypass:
    def test_removing_admin_from_record_kills_global_admin(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": [QQ_ADMIN]})
        assert world.approvals.is_admin(QQ_ADMIN, group_id=G1) is True
        world.gv.set(G1, {
            "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": True,
        })
        assert world.approvals.is_admin(QQ_ADMIN, group_id=G1) is False
        assert world.approvals.is_admin("9", group_id=G1) is True
        # 别的群没被动过
        assert world.approvals.is_admin(QQ_ADMIN, group_id=G2) is True

    def test_global_exempt_user_edit_does_not_leak_after_seed(self, tmp_path: Path) -> None:
        holder = {"required": True, "admins": [QQ_ADMIN], "exempt_users": ["qq:" + MEMBER]}
        world = _world(tmp_path, approval=holder)
        assert world.approvals._is_auto(G1, MEMBER) is True  # 种子里的免批人
        world.gv.set(G1, {
            "approvers": ["qq:" + QQ_ADMIN], "exempt_users": [],
            "exempt_group": False, "required": True,
        })
        assert world.approvals._is_auto(G1, MEMBER) is False  # 不再回全局并集

    def test_no_gid_legacy_path_still_reads_global(self, tmp_path: Path) -> None:
        # 兼容老纯数据层测试的缺省路径：不传 gid → 退回全局 approval.admins
        world = _world(tmp_path, approval={"required": True, "admins": [QQ_ADMIN]})
        assert world.approvals.is_admin(QQ_ADMIN) is True
        assert world.approvals.is_admin("99999") is False

    def test_can_cancel_without_resolvable_group_never_uses_global(self, tmp_path: Path) -> None:
        # 取消权限必须落到具体群；对象找不到、群也认不出时，不许用全局名单放行
        world = _world(tmp_path, approval={"required": True, "admins": [QQ_ADMIN]})
        assert world.approvals.can_cancel("task", "T-404", QQ_ADMIN) is False
        assert world.approvals.can_cancel("goal", "G-404", QQ_ADMIN) is False

    def test_group_admins_and_approvals_share_one_source(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": []})
        world.ga.set_accounts(G1, [LEGACY_ADMIN])
        assert world.gv.get(G1).approvers == ("qq:30003",)
        assert world.ga.accounts(G1) == ["qq:30003"]
        assert world.approvals.is_admin(LEGACY_ADMIN, group_id=G1) is True
        world.gv.set(G1, {
            "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": True,
        })
        assert world.ga.accounts(G1) == ["qq:9"]
        assert world.ga.is_group_admin(G1, "9") is True
        assert world.ga.is_group_admin(G1, LEGACY_ADMIN) is False


# ======================================================================
# 3. 免批按本群记录生效 + force_manual 闸
# ======================================================================


class TestPerGroupExempt:
    def test_record_required_false_makes_requests_auto(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": []})
        world.gv.set(G1, {
            "approvers": [], "exempt_users": [], "exempt_group": False, "required": False,
        })
        assert _create(world, G1)["status"] == "approved"
        assert _create(world, G2)["status"] == "pending"  # 只放开本群

    def test_record_exempt_group_and_user(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": []})
        world.gv.set(G1, {
            "approvers": [], "exempt_users": ["qq:" + MEMBER],
            "exempt_group": False, "required": True,
        })
        assert _create(world, G1)["status"] == "approved"
        assert _create(world, G1, requester_id="99999")["status"] == "pending"
        world.gv.set(G1, {
            "approvers": [], "exempt_users": [], "exempt_group": True, "required": True,
        })
        assert _create(world, G1, requester_id="99999")["status"] == "approved"

    def test_force_manual_beats_exempt_group(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": False, "admins": []})
        r = _create(world, G1, force_manual=True)
        assert r["status"] == "pending"

    def test_review_hook_not_called_for_exempt_group(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": False, "admins": []})
        seen: list[tuple[str, str]] = []
        world.approvals.set_review_hook(lambda rid, gid: seen.append((rid, gid)))
        _create(world, G1)
        assert seen == []  # 免批直接落地，不该触发自动审核

    def test_auto_review_gates_kept(self, tmp_path: Path) -> None:
        world = _world(tmp_path, approval={"required": True, "admins": [], "auto_review": True})

        class _Ready:
            def ready(self) -> bool:
                return True

        class _Models:
            def settings(self):
                return _Ready()

        ar = AutoReviewer(world.store, _Models(), world.approvals, lambda: world.settings)
        day = clock.day_key(clock.now())

        normal = _create(world, G1, title="查个资料")
        row = world.store.read().execute("SELECT * FROM requests WHERE id=?", (normal["id"],)).fetchone()
        assert ar._eligible(G1, {k: row[k] for k in row.keys()}, day) is True

        forced = _create(world, G1, title="对外发帖", force_manual=True)
        frow = world.store.read().execute("SELECT * FROM requests WHERE id=?", (forced["id"],)).fetchone()
        assert ar._eligible(G1, {k: frow[k] for k in frow.keys()}, day) is False

        goal = _create(world, G1, kind="goal", title="长期盯着")
        grow = world.store.read().execute("SELECT * FROM requests WHERE id=?", (goal["id"],)).fetchone()
        assert ar._eligible(G1, {k: grow[k] for k in grow.keys()}, day) is False

        mine = _create(world, G1, title="我自己提的", source="maiwork")
        mrow = world.store.read().execute("SELECT * FROM requests WHERE id=?", (mine["id"],)).fetchone()
        assert ar._eligible(G1, {k: mrow[k] for k in mrow.keys()}, day) is False


# ======================================================================
# 4. /mw 批准：同群归属 + 统一名单
# ======================================================================


class _FakeOutbox:
    def __init__(self) -> None:
        self.enqueued: list[tuple] = []
        self._keys: set[str] = set()

    def enqueue(self, key, group_id, kind, payload, *, task_id=None, not_before=0) -> int:
        if str(key) in self._keys:
            return -1
        self._keys.add(str(key))
        self.enqueued.append((key, group_id, kind, dict(payload)))
        return len(self.enqueued)

    async def flush(self, now: float) -> None:
        return None


def _cmd_env(tmp_path: Path, *, admins=("10001",)):
    world = _world(
        tmp_path,
        approval={"required": True, "admins": list(admins)},
    )
    host = FakeHost(session_id="sess-1")
    host.member_roles = {}
    outbox = _FakeOutbox()
    cmd = Commands(
        world.store, world.approvals, world.tasks, world.goals, outbox, host,
        lambda: world.settings,
        group_admins=world.ga,
    )
    world.host = host
    world.outbox = outbox
    world.cmd = cmd
    return world


async def _say(world, text: str, *, user: str = MEMBER, gid: str = G1) -> str:
    await world.cmd.handle(gid, user, f"名字{user}", text, "m-1")
    assert world.outbox.enqueued, f"应该有一条回复入队（{text}）"
    return world.outbox.enqueued[-1][3]["text"]


class TestMwDecideUnified:
    @pytest.mark.asyncio
    async def test_bot_admin_cannot_approve_other_group_request(self, tmp_path: Path) -> None:
        world = _cmd_env(tmp_path)
        other = _create(world, G2, title="别的群的活")
        reply = await _say(world, f"/mw 批准 {other['id']}", user=QQ_ADMIN, gid=G1)
        assert "不在本群" in reply, reply
        row = world.store.read().execute(
            "SELECT status FROM requests WHERE id=?", (other["id"],)
        ).fetchone()
        assert str(row["status"]) == "pending"

    @pytest.mark.asyncio
    async def test_bot_admin_can_approve_own_group(self, tmp_path: Path) -> None:
        world = _cmd_env(tmp_path)
        req = _create(world, G1)
        reply = await _say(world, f"/mw 批准 {req['id']}", user=QQ_ADMIN, gid=G1)
        assert "已批准" in reply or "开工" in reply

    @pytest.mark.asyncio
    async def test_group_admin_from_record_can_approve(self, tmp_path: Path) -> None:
        world = _cmd_env(tmp_path, admins=())
        world.ga.set_accounts(G1, [LEGACY_ADMIN])
        req = _create(world, G1)
        reply = await _say(world, f"/mw 批准 {req['id']}", user=LEGACY_ADMIN, gid=G1)
        assert "已批准" in reply or "开工" in reply

    @pytest.mark.asyncio
    async def test_removed_approver_cannot_approve(self, tmp_path: Path) -> None:
        world = _cmd_env(tmp_path)
        world.gv.set(G1, {
            "approvers": ["qq:9"], "exempt_users": [], "exempt_group": False, "required": True,
        })
        req = _create(world, G1)
        reply = await _say(world, f"/mw 批准 {req['id']}", user=QQ_ADMIN, gid=G1)
        assert "只有 bot 管理员或本群管理员能批准 / 拒绝" in reply, reply

    @pytest.mark.asyncio
    async def test_cancel_passes_gid_and_stays_within_group(self, tmp_path: Path) -> None:
        world = _cmd_env(tmp_path)
        tid = world.tasks.create(
            G1, title="整理资料", req="", criteria=[], source="request",
            requester_id=MEMBER, requester_name="阿柒", status="queued",
        )
        reply = await _say(world, f"/mw 取消 {tid}", user=MEMBER, gid=G1)
        assert "已取消" in reply
        assert world.tasks.get(tid)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_other_group_approver_list_does_not_grant(self, tmp_path: Path) -> None:
        world = _cmd_env(tmp_path, admins=())
        world.ga.set_accounts(G2, [LEGACY_ADMIN])  # 只在别的群是批准人
        req = _create(world, G1)
        reply = await _say(world, f"/mw 批准 {req['id']}", user=LEGACY_ADMIN, gid=G1)
        assert "只有 bot 管理员或本群管理员能批准 / 拒绝" in reply, reply


# ======================================================================
# 5. 网页 API：GET | PUT /api/groups/{gid}/approval
# ======================================================================


def _port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _app_raw(data_dir: Path, *, admins=("10001",)):
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": ADMIN_PW, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": list(admins)},
    }


@pytest_asyncio.fixture
async def api_env(tmp_path: Path):
    import tomlkit

    raw = _app_raw(tmp_path / "data")
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            t = tomlkit.table()
            for k, v in values.items():
                t[k] = v
            doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    env = SimpleNamespace(app=app, client=client, tmp_path=tmp_path)
    try:
        yield env
    finally:
        await client.close()
        await app.stop()


async def _login(client, password=ADMIN_PW):
    return await client.post("/api/login", json={"password": password})


class TestApprovalApi:
    _KEYS = {"approvers", "exempt_users", "exempt_group", "required"}

    @pytest.mark.asyncio
    async def test_get_anonymous_401(self, api_env) -> None:
        r = await api_env.client.get(f"/api/groups/{G1}/approval")
        assert r.status == 401, r.status

    @pytest.mark.asyncio
    async def test_get_member_403(self, api_env) -> None:
        token = api_env.app.token_of(G1)
        r = await api_env.client.get(
            f"/api/groups/{G1}/approval", headers={"X-MW-Group": token}
        )
        assert r.status == 403, r.status

    @pytest.mark.asyncio
    async def test_get_group_admin_own_ok_other_403(self, api_env) -> None:
        api_env.app.group_admins.set_password(G1, G1_PW)
        assert (await _login(api_env.client, G1_PW)).status == 200
        r = await api_env.client.get(f"/api/groups/{G1}/approval")
        assert r.status == 200, r.status
        data = await r.json()
        assert set(data.keys()) == self._KEYS
        assert data["approvers"] == ["qq:10001"]
        r2 = await api_env.client.get(f"/api/groups/{G2}/approval")
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_get_admin_ok_unknown_group_404(self, api_env) -> None:
        await _login(api_env.client)
        r = await api_env.client.get(f"/api/groups/{G1}/approval")
        assert r.status == 200
        assert (await api_env.client.get("/api/groups/999999/approval")).status == 404

    @pytest.mark.asyncio
    async def test_put_requires_total_admin(self, api_env) -> None:
        body = {"approvers": ["qq:10001"], "exempt_users": [], "exempt_group": False, "required": True}
        assert (await api_env.client.put(f"/api/groups/{G1}/approval", json=body)).status == 401
        token = api_env.app.token_of(G1)
        r = await api_env.client.put(
            f"/api/groups/{G1}/approval", json=body, headers={"X-MW-Group": token}
        )
        assert r.status == 403, r.status
        api_env.app.group_admins.set_password(G1, G1_PW)
        await _login(api_env.client, G1_PW)
        r2 = await api_env.client.put(f"/api/groups/{G1}/approval", json=body)
        assert r2.status == 403, r2.status
        assert "群管理员只能管本群的事" in (await r2.json())["error"]

    @pytest.mark.asyncio
    async def test_put_then_get_roundtrip(self, api_env) -> None:
        await _login(api_env.client)
        body = {
            "approvers": ["30003", "qq:40004"],
            "exempt_users": ["qq:20002"],
            "exempt_group": False,
            "required": True,
        }
        r = await api_env.client.put(f"/api/groups/{G1}/approval", json=body)
        assert r.status == 200, r.status
        data = await r.json()
        assert set(data.keys()) == self._KEYS
        assert data == {
            "approvers": ["qq:30003", "qq:40004"],
            "exempt_users": ["qq:20002"],
            "exempt_group": False,
            "required": True,
        }
        r2 = await api_env.client.get(f"/api/groups/{G1}/approval")
        assert await r2.json() == data

    @pytest.mark.asyncio
    async def test_put_rejects_before_any_write(self, api_env) -> None:
        await _login(api_env.client)
        good = {
            "approvers": ["qq:10001"], "exempt_users": [],
            "exempt_group": False, "required": True,
        }
        assert (await api_env.client.put(f"/api/groups/{G1}/approval", json=good)).status == 200
        before = await (await api_env.client.get(f"/api/groups/{G1}/approval")).json()
        bad_bodies = [
            {"approvers": ["qq:10001"], "exempt_users": [], "exempt_group": False,
             "required": True, "unknown": 1},                                    # 未知键
            {"approvers": ["不是账号"], "exempt_users": [], "exempt_group": False,
             "required": True},                                                   # 坏账号
            {"approvers": "qq:10001", "exempt_users": [], "exempt_group": False,
             "required": True},                                                   # 类型不对
            {"approvers": ["qq:10001"], "exempt_users": [], "exempt_group": "yes",
             "required": True},                                                   # 类型不对
            {"approvers": ["qq:10001"], "exempt_users": [], "exempt_group": False},  # 缺字段
        ]
        for body in bad_bodies:
            r = await api_env.client.put(f"/api/groups/{G1}/approval", json=body)
            assert r.status == 400, (body, r.status)
            assert (await r.json())["error"]
        after = await (await api_env.client.get(f"/api/groups/{G1}/approval")).json()
        assert after == before, "校验不过时一个字段都不许写"

    @pytest.mark.asyncio
    async def test_get_never_leaks_password(self, api_env) -> None:
        api_env.app.group_admins.set_password(G1, G1_PW)
        await _login(api_env.client)
        r = await api_env.client.get(f"/api/groups/{G1}/approval")
        assert G1_PW not in await r.text()

    @pytest.mark.asyncio
    async def test_group_admin_password_login_unchanged(self, api_env) -> None:
        api_env.app.group_admins.set_password(G1, G1_PW)
        assert (await _login(api_env.client, G1_PW)).status == 200
        me = await (await api_env.client.get("/api/me")).json()
        assert me["role"] == "group_admin" and me["group"] == G1

    @pytest.mark.asyncio
    async def test_group_admin_compat_api_same_source(self, api_env) -> None:
        """旧 /group-admin 账户操作落到同一份 approvers；单来源、不新增一条名单。"""
        await _login(api_env.client)
        r = await api_env.client.put(
            f"/api/groups/{G1}/group-admin", json={"accounts": ["30003"]}
        )
        assert r.status == 200, r.status
        assert (await r.json())["accounts"] == ["qq:30003"]
        approval = await (await api_env.client.get(f"/api/groups/{G1}/approval")).json()
        assert approval["approvers"] == ["qq:30003"]
        # 新 API 改成只剩 9 号 → 旧接口读到的也是同一份
        body = {
            "approvers": ["qq:9"], "exempt_users": [],
            "exempt_group": False, "required": True,
        }
        assert (await api_env.client.put(f"/api/groups/{G1}/approval", json=body)).status == 200
        ga = await (await api_env.client.get(f"/api/groups/{G1}/group-admin")).json()
        assert ga["accounts"] == ["qq:9"]
        # 旧按群名单键不再被写
        assert api_env.app.store.kv_get(f"group_admins.{G1}", None) is None


# ======================================================================
# 6. 死接口：/api/ideas/{id}/want 已摘（do / dismiss 保留）
# ======================================================================


class TestIdeaWantRemoved:
    @pytest.mark.asyncio
    async def test_want_route_gone(self, api_env) -> None:
        await _login(api_env.client)
        r = await api_env.client.post("/api/ideas/1/want", json={})
        assert r.status == 404, r.status
