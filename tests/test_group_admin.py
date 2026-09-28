"""群管理员（group_admin）测试：密码存储、登录 cookie、逐类路由权限、/mw 批准。

先写先红（2026-10）：group_admins.py 还没建、网页身份还只有 admin / member 时，
这一整个文件应当失败（导入错误 / 401 / 403 不对），再写实现让它变绿。

分三块：
1. GroupAdmins 数据层（真 Store + 真 Settings + 真 ConsoleAuth）
2. 网页：登录与 cookie、逐类路由权限、总管理员管理群管理员的接口
3. /mw 批准 / 拒绝：本群管理员可批本群、不能批他群、非名单拒绝
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.app import MaiWorkApp
from CharTyr_MaiWork.console.auth import COOKIE_NAME
from CharTyr_MaiWork.store import Store

G1 = "900000001"
G2 = "123456789"
ADMIN_PW = "总管理员密码-不要外传-1234"
G1_PW = "群一管理员密码-abcd"
G2_PW = "群二管理员密码-efgh"
NEW_PW = "换过的新密码-wxyz"
G1_ADMIN_QQ = "30003"
MEMBER = "20002"


# ----------------------------------------------------------------------
# 小工具：数据层
# ----------------------------------------------------------------------


def _make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "ga.db")
    store.migrate()
    return store


def _make_settings(gids: tuple[str, ...] = (G1, G2)):
    from CharTyr_MaiWork.config import load_settings

    settings, problems = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{g}"} for g in gids]},
            "console": {"password": ADMIN_PW},
        }
    )
    assert not problems, problems
    return settings


def _ga(store: Store, settings=None, auth=None):
    from CharTyr_MaiWork.group_admins import GroupAdmins

    return GroupAdmins(
        store,
        get_settings=(lambda: settings) if settings is not None else None,
        console_auth=auth,
    )


# ----------------------------------------------------------------------
# 1. 数据层
# ----------------------------------------------------------------------


class TestGroupAdminsStore:
    def test_set_password_too_short(self, tmp_path: Path) -> None:
        ga = _ga(_make_store(tmp_path), _make_settings())
        with pytest.raises(ValueError) as e:
            ga.set_password(G1, "1234567")
        assert "8" in str(e.value)

    def test_set_has_clear_password(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ga = _ga(store, _make_settings())
        assert ga.has_password(G1) is False
        ga.set_password(G1, G1_PW)
        assert ga.has_password(G1) is True
        # 存的是 sha256$salt$digest 格式，明文不落库
        raw = store.secret_get(f"group_admin_pw.{G1}")
        assert raw.startswith("sha256$")
        assert raw.count("$") == 2
        assert G1_PW not in raw
        ga.clear_password(G1)
        assert ga.has_password(G1) is False

    def test_duplicate_password_other_group_rejected(self, tmp_path: Path) -> None:
        ga = _ga(_make_store(tmp_path), _make_settings())
        ga.set_password(G1, G1_PW)
        with pytest.raises(ValueError) as e:
            ga.set_password(G2, G1_PW)
        assert "和别的群管理员密码重复了" in str(e.value)
        assert ga.has_password(G2) is False

    def test_duplicate_password_with_total_admin_rejected(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.console.auth import ConsoleAuth

        store = _make_store(tmp_path)
        settings = _make_settings()
        auth = ConsoleAuth(store, lambda: settings)
        ga = _ga(store, settings, auth)
        with pytest.raises(ValueError) as e:
            ga.set_password(G1, ADMIN_PW)
        assert "和别的群管理员密码重复了" in str(e.value)
        assert ga.has_password(G1) is False

    def test_match_only_served_groups(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        settings = _make_settings()
        ga = _ga(store, settings)
        ga.set_password(G1, G1_PW)
        ga.set_password(G2, G2_PW)
        assert ga.match(G1_PW) == G1
        assert ga.match(G2_PW) == G2
        assert ga.match("完全不对的密码-zzzz") is None
        assert ga.match("") is None
        # 群不再服务 → 视为 none
        only_g1 = _make_settings((G1,))
        ga2 = _ga(store, only_g1)
        assert ga2.match(G2_PW) is None
        assert ga2.match(G1_PW) == G1

    def test_accounts_normalized(self, tmp_path: Path) -> None:
        ga = _ga(_make_store(tmp_path), _make_settings())
        assert ga.accounts(G1) == []
        out = ga.set_accounts(G1, ["30003", "qq:40004", "30003"])
        assert out == ["qq:30003", "qq:40004"]
        assert ga.accounts(G1) == ["qq:30003", "qq:40004"]
        assert ga.is_group_admin(G1, "30003") is True
        assert ga.is_group_admin(G1, "40004") is True
        assert ga.is_group_admin(G1, "99999") is False
        assert ga.is_group_admin(G2, "30003") is False
        assert ga.is_group_admin(G1, "") is False

    def test_accounts_reject_garbage(self, tmp_path: Path) -> None:
        ga = _ga(_make_store(tmp_path), _make_settings())
        with pytest.raises(ValueError):
            ga.set_accounts(G1, ["这不是账号"])

    def test_fingerprint_tracks_password(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ga = _ga(store, _make_settings())
        assert ga.fingerprint(G1) == ""
        ga.set_password(G1, G1_PW)
        fp1 = ga.fingerprint(G1)
        assert fp1
        ga.set_password(G1, NEW_PW)
        fp2 = ga.fingerprint(G1)
        assert fp2 and fp2 != fp1
        ga.clear_password(G1)
        assert ga.fingerprint(G1) == ""
        assert fp2 != ga.fingerprint(G1)


# ----------------------------------------------------------------------
# 2. 网页
# ----------------------------------------------------------------------


def _port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": ADMIN_PW, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-x", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


class _FakeFeeds:
    """只实现 server.py 用到的几个方法。"""

    def __init__(self) -> None:
        self.feedback_calls: list[tuple] = []
        self.idea_calls: list[tuple] = []
        self.prefs: dict[str, str] = {}

    def feedback(self, kind, item_id, value, prev):
        self.feedback_calls.append((kind, int(item_id), value, prev))
        return {"up": 1, "down": 0}

    def idea_action(self, idea_id, op, *, by="", item_nos=None):
        self.idea_calls.append((int(idea_id), str(op), str(by), item_nos))
        return {"id": int(idea_id), "state": {"want": "wanted", "do": "pending", "dismiss": "dismissed"}[op]}

    def ideas_view(self, group_id):
        return []

    def pref(self, group_id):
        return self.prefs.get(str(group_id), "")

    def set_pref(self, group_id, text):
        self.prefs[str(group_id)] = str(text)
        return self.prefs[str(group_id)]

    def admin_chat_vote(self, group_id, item_id):
        return {"chat_votes": 1}

    def news_view(self, group_id, *, days=3):
        return []

    def today_count(self, group_id):
        return 0


class _FakeTopics:
    def __init__(self) -> None:
        self.verdict_calls: list[tuple] = []

    def verdict(self, topic_id, value):
        self.verdict_calls.append((int(topic_id), value))

    def log_view(self, group_id, *, days=3):
        return []

    async def check(self, group_id, now):
        return {}


class SimpleEnv:
    def __init__(self, app: MaiWorkApp, client: TestClient, tmp_path: Path) -> None:
        self.app = app
        self.client = client
        self.tmp_path = tmp_path

    async def login(self, password: str = ADMIN_PW):
        return await self.client.post("/api/login", json={"password": password})

    async def me(self) -> dict:
        r = await self.client.get("/api/me")
        assert r.status == 200, r.status
        return await r.json()

    def set_group_password(self, gid: str, pw: str) -> None:
        self.app.group_admins.set_password(gid, pw)

    def add_news(self, gid: str, *, personal: bool = False, title: str = "一条资讯") -> int:
        with self.app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, created, target_user_id)"
                " VALUES (1, ?, ?, ?, ?)",
                (gid, title, clock.now(), MEMBER if personal else ""),
            )
            return int(cur.lastrowid or 0)

    def add_idea(self, gid: str, *, personal: bool = False, title: str = "一个构想") -> int:
        with self.app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, title, created, updated, target_user_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (gid, title, clock.now(), clock.now(), MEMBER if personal else ""),
            )
            return int(cur.lastrowid or 0)

    def add_topic(self, gid: str) -> int:
        with self.app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_log (group_id, ts, opener) VALUES (?, ?, '聊过')",
                (gid, clock.now()),
            )
            return int(cur.lastrowid or 0)

    def add_request(self, gid: str, *, title: str = "整理资料") -> dict:
        return self.app.approvals.create(
            gid, kind="task", title=title, quote=title, via="群里 @",
            requester_id=MEMBER, requester_name="阿柒",
        )

    def add_task(self, gid: str, *, status: str = "queued") -> str:
        tid = self.app.tasks.create(
            gid, title="整理资料", req="req", criteria=[], source="test", status="queued"
        )
        if status != "queued":
            self.app.tasks.transition(tid, status)
        return tid

    def add_goal(self, gid: str) -> str:
        return self.app.goals.create_agent(
            gid, title="追一个目标", body="", criteria=[], by_text="测试"
        )


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    import tomlkit

    raw = _raw_config(tmp_path / "data")
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
    # M2 模块在纯测试环境里可能没就位：塞假的，专测权限，不碰模型
    app.feeds = _FakeFeeds()
    app.topics = _FakeTopics()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield SimpleEnv(app=app, client=client, tmp_path=tmp_path)
    finally:
        await client.close()
        await app.stop()


class TestLoginAndCookie:
    @pytest.mark.asyncio
    async def test_group_password_login_and_me(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        r = await env.login(G1_PW)
        assert r.status == 200, r.status
        me = await env.me()
        assert me["role"] == "group_admin"
        assert me["group"] == G1

    @pytest.mark.asyncio
    async def test_total_admin_login_unchanged(self, env: SimpleEnv) -> None:
        r = await env.login(ADMIN_PW)
        assert r.status == 200
        me = await env.me()
        assert me["role"] == "admin"
        assert me["group"] is None

    @pytest.mark.asyncio
    async def test_wrong_password_still_401(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        r = await env.login("谁都不对的密码-0000")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_old_cookie_dies_when_password_changes(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        assert (await env.login(G1_PW)).status == 200
        assert (await env.me())["role"] == "group_admin"
        env.set_group_password(G1, NEW_PW)
        me = await env.me()
        assert me["role"] == "none"
        assert me["group"] is None

    @pytest.mark.asyncio
    async def test_old_cookie_dies_when_password_cleared(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        assert (await env.login(G1_PW)).status == 200
        env.app.group_admins.clear_password(G1)
        assert (await env.me())["role"] == "none"

    @pytest.mark.asyncio
    async def test_group_no_longer_served_is_none(self, env: SimpleEnv) -> None:
        env.set_group_password(G2, G2_PW)
        assert (await env.login(G2_PW)).status == 200
        assert (await env.me())["group"] == G2
        # 群被移出服务名单 → 旧 cookie 视为 none
        from CharTyr_MaiWork.config import load_settings

        settings = env.app.get_settings()
        only_g1, _ = load_settings(
            {
                "plugin": {"enabled": True},
                "groups": {"serve": [{"group": f"qq:{G1}"}]},
                "console": {"password": ADMIN_PW, "listen": settings.console.listen},
            }
        )
        env.app._settings = only_g1
        me = await env.me()
        assert me["role"] == "none"
        assert me["group"] is None

    @pytest.mark.asyncio
    async def test_login_response_does_not_leak_password(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        r = await env.login(G1_PW)
        text = await r.text()
        assert G1_PW not in text


class TestGroupsAndGroupView:
    @pytest.mark.asyncio
    async def test_groups_list_only_own(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.get("/api/groups")
        assert r.status == 200
        groups = await r.json()
        assert [g["id"] for g in groups] == [G1]

    @pytest.mark.asyncio
    async def test_group_view_own_is_admin_view(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.get(f"/api/groups/{G1}")
        assert r.status == 200
        view = await r.json()
        assert view["id"] == G1
        assert "focus" in view  # 管理员版才有的关注成员
        assert view.get("token")
        # 别的群一律 403
        for ref in (G2, env.app.token_of(G2)):
            r2 = await env.client.get(f"/api/groups/{ref}")
            assert r2.status == 403, (ref, r2.status)

    @pytest.mark.asyncio
    async def test_group_view_own_by_token_ok(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.get(f"/api/groups/{env.app.token_of(G1)}")
        assert r.status == 200


class TestGroupScopedRoutes:
    """按群的管理动作：本群可（非 403），他群 403。"""

    @pytest.mark.asyncio
    async def test_profile_add_own_group_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.post(
            f"/api/groups/{G1}/profile", json={"category": "interest", "text": "爱聊技术"}
        )
        assert r.status == 200, r.status
        r2 = await env.client.post(
            f"/api/groups/{G2}/profile", json={"category": "interest", "text": "别的群"}
        )
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_profile_entry_id_other_group_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        # FakeProfiles：给 G2 造一条画像
        eid = env.app.profiles.add_entry(G2, "interest", "别的群的画像")
        r = await env.client.patch(f"/api/profile/{eid}", json={"text": "改一下"})
        assert r.status == 403, r.status
        # 本群的能改；条目不存在 404
        own = env.app.profiles.add_entry(G1, "interest", "本群画像")
        r2 = await env.client.patch(f"/api/profile/{own}", json={"text": "改一下"})
        assert r2.status == 200, r2.status
        r3 = await env.client.delete("/api/profile/999999")
        assert r3.status == 404, r3.status

    @pytest.mark.asyncio
    async def test_focus_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.post(f"/api/groups/{G1}/focus", json={"user_id": "50005", "action": "add"})
        assert r.status == 200, r.status
        r2 = await env.client.post(f"/api/groups/{G2}/focus", json={"user_id": "50005", "action": "add"})
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_focus_remove_forbidden_for_group_admin(self, env: SimpleEnv) -> None:
        # 移除关注会删掉这个人的个人画像；个人画像群管理员只读，所以不给移除
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.post(f"/api/groups/{G1}/focus", json={"user_id": "50005", "action": "remove"})
        assert r.status == 403, r.status

    @pytest.mark.asyncio
    async def test_token_reset_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        old = env.app.token_of(G1)
        r = await env.client.post(f"/api/groups/{G1}/token", json={})
        assert r.status == 200, r.status
        assert (await r.json())["token"] != old
        r2 = await env.client.post(f"/api/groups/{G2}/token", json={})
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_feeds_pref_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.put(f"/api/groups/{G1}/feeds-pref", json={"text": "想看开源"})
        assert r.status == 200, r.status
        assert (await r.json())["text"] == "想看开源"
        r2 = await env.client.get(f"/api/groups/{G1}/feeds-pref")
        assert r2.status == 200
        r3 = await env.client.put(f"/api/groups/{G2}/feeds-pref", json={"text": "别的群"})
        assert r3.status == 403, r3.status
        r4 = await env.client.get(f"/api/groups/{G2}/feeds-pref")
        assert r4.status == 403, r4.status

    @pytest.mark.asyncio
    async def test_rss_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.get(f"/api/groups/{G1}/rss")
        assert r.status == 200, r.status
        r2 = await env.client.delete("/api/groups/999999/rss/nope")
        assert r2.status == 404, r2.status
        r3 = await env.client.get(f"/api/groups/{G2}/rss")
        assert r3.status == 403, r3.status

    @pytest.mark.asyncio
    async def test_identity_group_memory_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.put(f"/api/identity/group-memory/{G1}", json={"text": "这个群爱聊开源"})
        assert r.status != 403, r.status
        r2 = await env.client.put(f"/api/identity/group-memory/{G2}", json={"text": "别的群"})
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_ideas_run_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.post(f"/api/groups/{G1}/ideas/run", json={})
        assert r.status != 403, r.status
        r2 = await env.client.post(f"/api/groups/{G2}/ideas/run", json={})
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_identity_group_memory_read_write(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.put(f"/api/identity/group-memory/{G1}", json={"text": "这个群爱聊开源"})
        assert r.status != 403, r.status
        r2 = await env.client.get(f"/api/identity/group-memory/{G1}")
        assert r2.status == 200, r2.status
        assert (await r2.json())["text"] == "这个群爱聊开源"
        r3 = await env.client.get(f"/api/identity/group-memory/{G2}")
        assert r3.status == 403, r3.status

    @pytest.mark.asyncio
    async def test_news_run_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        r = await env.client.post(f"/api/groups/{G1}/news/run", json={})
        assert r.status != 403, r.status
        r2 = await env.client.post(f"/api/groups/{G2}/news/run", json={})
        assert r2.status == 403, r2.status


class TestItemIdRoutes:
    """参数是条目 id 的路由：先查它属于哪个群，再判。"""

    @pytest.mark.asyncio
    async def test_news_feedback_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_news(G1)
        other = env.add_news(G2)
        r = await env.client.post(f"/api/news/{own}/feedback", json={"value": "up", "prev": None})
        assert r.status == 200, r.status
        r2 = await env.client.post(f"/api/news/{other}/feedback", json={"value": "up", "prev": None})
        assert r2.status == 403, r2.status
        r3 = await env.client.post("/api/news/999999/feedback", json={"value": "up"})
        assert r3.status == 404, r3.status

    @pytest.mark.asyncio
    async def test_ideas_do_dismiss_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_idea(G1)
        other = env.add_idea(G2)
        r = await env.client.post(f"/api/ideas/{own}/do", json={})
        assert r.status == 200, r.status
        assert env.app.feeds.idea_calls[-1][2] == "群管理员（网页）"
        r2 = await env.client.post(f"/api/ideas/{own}/dismiss", json={})
        assert r2.status == 200, r2.status
        r3 = await env.client.post(f"/api/ideas/{other}/do", json={})
        assert r3.status == 403, r3.status
        r4 = await env.client.post(f"/api/ideas/{other}/dismiss", json={})
        assert r4.status == 403, r4.status

    @pytest.mark.asyncio
    async def test_topics_verdict_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_topic(G1)
        other = env.add_topic(G2)
        r = await env.client.post(f"/api/topics/{own}/verdict", json={"value": "right"})
        assert r.status == 200, r.status
        r2 = await env.client.post(f"/api/topics/{other}/verdict", json={"value": "right"})
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_personal_news_read_ok_write_403(self, env: SimpleEnv) -> None:
        """个人向资讯：群管理员看得见（本群），但改不了。"""
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        # 看得见：群视图里关注成员个人向内容不报错
        r = await env.client.get(f"/api/groups/{G1}")
        assert r.status == 200
        pid = env.add_news(G1, personal=True)
        r2 = await env.client.post(f"/api/news/{pid}/feedback", json={"value": "up", "prev": None})
        assert r2.status == 403, r2.status
        assert "个人画像" in (await r2.json())["error"]
        r3 = await env.client.post(f"/api/news/{pid}/chat-vote", json={})
        assert r3.status == 403, r3.status
        r4 = await env.client.post(f"/api/news/{pid}/mention-to-member", json={})
        assert r4.status == 403, r4.status
        assert "个人画像" in (await r4.json())["error"]

    @pytest.mark.asyncio
    async def test_personal_ideas_write_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        pid = env.add_idea(G1, personal=True)
        r = await env.client.post(f"/api/ideas/{pid}/feedback", json={"value": "up"})
        assert r.status == 403, r.status
        assert "个人画像" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_personal_news_member_still_404(self, env: SimpleEnv) -> None:
        """群友看个人向资讯照样当不存在。"""
        pid = env.add_news(G1, personal=True)
        token = env.app.token_of(G1)
        r = await env.client.post(
            f"/api/news/{pid}/feedback",
            json={"value": "up"},
            headers={"X-MW-Group": token},
        )
        assert r.status == 404, r.status


class TestTaskAndApprovalRoutes:
    @pytest.mark.asyncio
    async def test_task_detail_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_task(G1)
        other = env.add_task(G2)
        r = await env.client.get(f"/api/tasks/{own}")
        assert r.status == 200, r.status
        # 服务器上的工作区 / token 数这类实现细节不给群管理员看
        detail = await r.json()
        assert "workspace" not in detail
        assert "env" not in detail
        r2 = await env.client.get(f"/api/tasks/{other}")
        assert r2.status == 403, r2.status

    @pytest.mark.asyncio
    async def test_task_ops_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_task(G1)
        other = env.add_task(G2)
        r = await env.client.post(f"/api/tasks/{own}/cancel", json={})
        assert r.status == 200, r.status
        for op in ("pause", "resume", "retry", "redeliver"):
            r2 = await env.client.post(f"/api/tasks/{other}/{op}", json={})
            assert r2.status == 403, (op, r2.status)

    @pytest.mark.asyncio
    async def test_goal_ops_own_ok_other_403(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_goal(G1)
        other = env.add_goal(G2)
        r = await env.client.post(f"/api/goals/{own}/pause", json={})
        assert r.status == 200, r.status
        for op in ("resume", "cancel"):
            r2 = await env.client.post(f"/api/goals/{other}/{op}", json={})
            assert r2.status == 403, (op, r2.status)

    @pytest.mark.asyncio
    async def test_approve_own_ok_other_403_and_by_text(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        own = env.add_request(G1)
        other = env.add_request(G2)
        r = await env.client.post(f"/api/requests/{other['id']}/approve", json={})
        assert r.status == 403, r.status
        r2 = await env.client.post(f"/api/requests/{own['id']}/reject", json={})
        assert r2.status == 200, r2.status
        row = env.app.store.read().execute(
            "SELECT decided_by FROM requests WHERE id=?", (own["id"],)
        ).fetchone()
        assert str(row["decided_by"]) == "群管理员（网页）"
        # 批准走同一套（by 文本）
        own2 = env.add_request(G1, title="第二件")
        r3 = await env.client.post(f"/api/requests/{own2['id']}/approve", json={})
        assert r3.status == 200, r3.status
        row2 = env.app.store.read().execute(
            "SELECT decided_by FROM requests WHERE id=?", (own2["id"],)
        ).fetchone()
        assert str(row2["decided_by"]) == "群管理员（网页）"

    @pytest.mark.asyncio
    async def test_admin_approve_by_text_unchanged(self, env: SimpleEnv) -> None:
        await env.login()
        req = env.add_request(G1)
        r = await env.client.post(f"/api/requests/{req['id']}/approve", json={})
        assert r.status == 200, r.status
        row = env.app.store.read().execute(
            "SELECT decided_by FROM requests WHERE id=?", (req["id"],)
        ).fetchone()
        assert str(row["decided_by"]) == "网页管理员"


class TestGlobalRoutesDenied:
    """全局的一律只给 admin。"""

    GLOBAL_GET = (
        "/api/settings",
        "/api/settings/rules",
        "/api/settings/config",
        "/api/onboarding",
        "/api/logs/model-calls",
        "/api/logs/tool-calls",
        "/api/logs/summary",
        "/api/usage/history",
        "/api/identity",
        "/api/extensions",
        "/api/extensions/search",
        "/api/chat",
        "/api/settings/avatar",
        "/api/avatar/m/abc",
        "/api/groups/" + G1 + "/group-admin",
    )
    GLOBAL_WRITE = (
        ("PUT", "/api/settings/models", {"base_url": "x"}),
        ("POST", "/api/settings/models/test", {"base_url": "https://x"}),
        ("PUT", "/api/settings/rules", {}),
        ("POST", "/api/settings/rules/reset", {"field": "quiet_hours"}),
        ("PUT", "/api/settings/config", {}),
        ("POST", "/api/settings/config/reset", {"field": "x"}),
        ("POST", "/api/onboarding", {"action": "next"}),
        ("POST", "/api/feeds/domains", {"domain": "example.com", "blocked": True}),
        ("POST", "/api/extensions/mcp/test", {"url": "https://x"}),
        ("POST", "/api/extensions/mcp", {"name": "x", "url": "https://x"}),
        ("POST", "/api/extensions/skills", {"name": "x"}),
        ("POST", "/api/identity/soul/sync", {}),
        ("PUT", "/api/identity/soul", {"text": "x"}),
        ("POST", "/api/chat", {}),
        ("POST", "/api/settings/avatar", {}),
        ("DELETE", "/api/settings/avatar", None),
        ("PUT", f"/api/groups/{G1}/group-admin", {"password": G1_PW}),
        ("DELETE", f"/api/groups/{G1}/group-admin/password", None),
    )

    @pytest.mark.asyncio
    async def test_global_get_403_with_message(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        for path in self.GLOBAL_GET:
            r = await env.client.get(path)
            assert r.status == 403, (path, r.status)
            assert "群管理员只能管本群的事" in (await r.json())["error"], path

    @pytest.mark.asyncio
    async def test_global_write_403_with_message(self, env: SimpleEnv) -> None:
        env.set_group_password(G1, G1_PW)
        await env.login(G1_PW)
        for method, path, body in self.GLOBAL_WRITE:
            r = await env.client.request(method, path, json=body)
            assert r.status == 403, (method, path, r.status)
            assert "群管理员只能管本群的事" in (await r.json())["error"], path

    @pytest.mark.asyncio
    async def test_member_still_403(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        r = await env.client.get("/api/settings", headers={"X-MW-Group": token})
        assert r.status == 403
        r2 = await env.client.get("/api/groups/" + G1 + "/group-admin", headers={"X-MW-Group": token})
        assert r2.status == 403


class TestGroupAdminManageApi:
    """总管理员管理群管理员的接口（只 admin）。"""

    @pytest.mark.asyncio
    async def test_get_put_delete(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/group-admin")
        assert r.status == 200
        assert await r.json() == {"password_set": False, "accounts": []}
        r2 = await env.client.put(
            f"/api/groups/{G1}/group-admin",
            json={"password": G1_PW, "accounts": [G1_ADMIN_QQ, "qq:40004"]},
        )
        assert r2.status == 200, r2.status
        data = await r2.json()
        assert data["password_set"] is True
        assert data["accounts"] == ["qq:30003", "qq:40004"]
        assert "password" not in data
        assert G1_PW not in await r2.text()
        # password 空字符串 = 不改
        r3 = await env.client.put(f"/api/groups/{G1}/group-admin", json={"password": ""})
        assert (await r3.json())["password_set"] is True
        # 只改名单
        r4 = await env.client.put(f"/api/groups/{G1}/group-admin", json={"accounts": ["50005"]})
        data4 = await r4.json()
        assert data4["accounts"] == ["qq:50005"]
        assert data4["password_set"] is True
        # 清密码
        r5 = await env.client.delete(f"/api/groups/{G1}/group-admin/password")
        assert r5.status == 200
        assert (await r5.json())["password_set"] is False
        # 名单还在
        assert (await (await env.client.get(f"/api/groups/{G1}/group-admin")).json())["accounts"] == ["qq:50005"]

    @pytest.mark.asyncio
    async def test_password_too_short_400(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.put(f"/api/groups/{G1}/group-admin", json={"password": "1234567"})
        assert r.status == 400
        assert "8" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_duplicate_password_400(self, env: SimpleEnv) -> None:
        await env.login()
        assert (
            await env.client.put(f"/api/groups/{G1}/group-admin", json={"password": G1_PW})
        ).status == 200
        r = await env.client.put(f"/api/groups/{G2}/group-admin", json={"password": G1_PW})
        assert r.status == 400
        assert "和别的群管理员密码重复了" in (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_unknown_group_404(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.get("/api/groups/999999/group-admin")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_group_admin_cookie_dies_after_clear(self, env: SimpleEnv) -> None:
        await env.login()
        await env.client.put(
            f"/api/groups/{G1}/group-admin", json={"password": G1_PW, "accounts": [G1_ADMIN_QQ]}
        )
        await env.client.post("/api/logout")
        await env.login(G1_PW)
        assert (await env.me())["role"] == "group_admin"
        # 管理员在另一个 client 上清密码
        _, _ = env.app, env.app.group_admins.clear_password(G1)
        assert (await env.me())["role"] == "none"


# ----------------------------------------------------------------------
# 3. /mw 批准 / 拒绝
# ----------------------------------------------------------------------


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


class CmdEnv:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)

    async def say(self, text: str, *, user: str = MEMBER, gid: str = G1, message_id: str = "m-1") -> str:
        await self.cmd.handle(gid, user, f"名字{user}", text, message_id)
        assert self.outbox.enqueued, f"应该有一条回复入队（{text}）"
        return self.outbox.enqueued[-1][3]["text"]


def _cmd_env(tmp_path: Path) -> CmdEnv:
    from fakes import FakeCoordinator, FakeHost

    from CharTyr_MaiWork.approvals import Approvals
    from CharTyr_MaiWork.commands import Commands
    from CharTyr_MaiWork.goals import Goals
    from CharTyr_MaiWork.tasks import Tasks

    from CharTyr_MaiWork.config import load_settings

    store = Store(tmp_path / "cmd.db")
    store.migrate()
    settings, _ = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
            "approval": {"required": True, "admins": ["10001"]},
        }
    )
    for gid in (G1, G2):
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, workspace, token, created) VALUES (?, ?, ?, ?)",
                (gid, f"g{gid}", f"tok{gid}", 1000.0),
            )
    tasks = Tasks(store, lambda: settings)
    goals = Goals(store, lambda: settings)
    approvals = Approvals(store, lambda: settings, tasks, goals)
    group_admins = _ga(store, settings)
    outbox = _FakeOutbox()
    host = FakeHost(session_id="sess-1")
    host.member_roles = {}
    cmd = Commands(
        store, approvals, tasks, goals, outbox, host, lambda: settings,
        coordinator=FakeCoordinator(), group_admins=group_admins,
    )
    return CmdEnv(store=store, settings=settings, tasks=tasks, goals=goals,
                  approvals=approvals, outbox=outbox, host=host, cmd=cmd,
                  group_admins=group_admins)


class TestMwDecideGroupAdmin:
    @pytest.mark.asyncio
    async def test_listed_group_admin_can_approve_own_group(self, tmp_path: Path) -> None:
        env = _cmd_env(tmp_path)
        env.group_admins.set_accounts(G1, [G1_ADMIN_QQ])
        req = env.approvals.create(
            G1, kind="task", title="整理资料", quote="q", via="群里 @",
            requester_id=MEMBER, requester_name="阿柒",
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user=G1_ADMIN_QQ)
        assert "已批准" in reply, reply
        row = env.store.read().execute("SELECT status FROM requests WHERE id=?", (req["id"],)).fetchone()
        assert str(row["status"]) == "approved"

    @pytest.mark.asyncio
    async def test_listed_group_admin_can_reject_own_group(self, tmp_path: Path) -> None:
        env = _cmd_env(tmp_path)
        env.group_admins.set_accounts(G2, [G1_ADMIN_QQ])
        req = env.approvals.create(
            G2, kind="task", title="整理资料", quote="q", via="群里 @",
            requester_id=MEMBER, requester_name="阿柒",
        )
        reply = await env.say(f"/mw 拒绝 {req['id']}", user=G1_ADMIN_QQ, gid=G2)
        assert "已拒绝" in reply, reply

    @pytest.mark.asyncio
    async def test_group_admin_cannot_decide_other_group(self, tmp_path: Path) -> None:
        env = _cmd_env(tmp_path)
        env.group_admins.set_accounts(G1, [G1_ADMIN_QQ])
        other = env.approvals.create(
            G2, kind="task", title="别的群的活", quote="q", via="群里 @",
            requester_id=MEMBER, requester_name="阿柒",
        )
        reply = await env.say(f"/mw 批准 {other['id']}", user=G1_ADMIN_QQ, gid=G1)
        assert "不在本群" in reply, reply
        row = env.store.read().execute("SELECT status FROM requests WHERE id=?", (other["id"],)).fetchone()
        assert str(row["status"]) == "pending"

    @pytest.mark.asyncio
    async def test_non_listed_member_denied(self, tmp_path: Path) -> None:
        env = _cmd_env(tmp_path)
        env.group_admins.set_accounts(G1, [G1_ADMIN_QQ])
        req = env.approvals.create(
            G1, kind="task", title="整理资料", quote="q", via="群里 @",
            requester_id=MEMBER, requester_name="阿柒",
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user="77777")
        assert "只有 bot 管理员或本群管理员能批准 / 拒绝" in reply, reply
        row = env.store.read().execute("SELECT status FROM requests WHERE id=?", (req["id"],)).fetchone()
        assert str(row["status"]) == "pending"

    @pytest.mark.asyncio
    async def test_bot_admin_still_can(self, tmp_path: Path) -> None:
        env = _cmd_env(tmp_path)
        req = env.approvals.create(
            G1, kind="task", title="整理资料", quote="q", via="群里 @",
            requester_id=MEMBER, requester_name="阿柒",
        )
        reply = await env.say(f"/mw 批准 {req['id']}", user="10001")
        assert "已批准" in reply, reply
