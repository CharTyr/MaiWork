"""console 单元测试：鉴权、限流、同源检查、群友隔离、密钥不出现在响应里、路由行为。

用 aiohttp.test_utils 起真的服务器（本机回环），不需要 MaiBot 宿主。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.views import icon_for_group

SECRET = "sk-test-十分显眼的密钥AaBbCc123"

def _free_port() -> int:
    """挑一个本机空闲端口：测试别依赖 18650 空着（用户可能正开着 SSH 隧道看网页）。"""
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]



PASSWORD = "测试密码-非常显眼-不要出现在日志里"
G1 = "900000001"
G2 = "123456789"


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": PASSWORD, "public_url": ""},
        "models": {
            "base_url": "https://ep.test/v1",
            "api_key": SECRET,
            "main": "main-m",
            "worker": "worker-m",
        },
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001", "10002"]},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    """起好 app + 控制台，产出可用 client 发请求的测试环境。

    网页改配置会写插件目录下的 config.toml：给每个测试造一个临时插件目录
    （不碰仓库里的真插件目录）。
    """
    raw = _raw_config(tmp_path / "data", environments={"workspace_root": str(tmp_path / "workspaces")})
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    import tomlkit

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
    # unsafe=True：允许跨端口带 cookie（TestServer 随机端口，验证逻辑靠 request.host）
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield SimpleEnv(app=app, client=client, tmp_path=tmp_path)
    await client.close()
    await app.stop()


class SimpleEnv:
    def __init__(self, app: MaiWorkApp, client: TestClient, tmp_path: Path) -> None:
        self.app = app
        self.client = client
        self.tmp_path = tmp_path

    @property
    def base(self) -> str:
        srv = self.app.console
        return f"http://127.0.0.1:{srv.port}"

    async def login(self, password: str = PASSWORD):
        return await self.client.post("/api/login", json={"password": password})


# ----------------------------------------------------------------------
# 静态与杂项
# ----------------------------------------------------------------------


class TestStaticAndMisc:
    @pytest.mark.asyncio
    async def test_index_no_cache_header(self, env: SimpleEnv) -> None:
        r = await env.client.get("/")
        assert r.status == 200
        assert r.headers.get("Cache-Control") == "no-cache"
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("Referrer-Policy") == "no-referrer"
        assert r.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_index_versions_assets(self, env: SimpleEnv) -> None:
        """部署新版后浏览器不能还用缓存的旧脚本 / 样式：入口和样式带内容版本号。"""
        import hashlib
        import re as _re

        from CharTyr_MaiWork.maiwork.console import server as _srv

        r = await env.client.get("/")
        html = await r.text()
        for name in ("js/main.js", "style.css"):
            m = _re.search(r"/static/" + _re.escape(name) + r"\?v=([0-9a-f]{8,})", html)
            assert m, f"{name} 没带版本号"
            digest = hashlib.sha256((_srv._STATIC_DIR / name).read_bytes()).hexdigest()
            assert digest.startswith(m.group(1))
        assert '<script type="module" src="/static/js/main.js?v=' in html
        r2 = await env.client.get("/static/js/main.js?v=abc")
        assert r2.status == 200

    @pytest.mark.asyncio
    async def test_index_import_map_versions_every_module(self, env: SimpleEnv) -> None:
        """前端拆成多个模块后，被入口 import 的模块也要带版本号（写在 import map 里），
        否则部署后浏览器可能拿缓存里的旧模块，新旧代码混用。"""
        import hashlib
        import re as _re

        from CharTyr_MaiWork.maiwork.console import server as _srv

        r = await env.client.get("/")
        html = await r.text()
        m = _re.search(r'<script type="importmap">(.*?)</script>', html, _re.S)
        assert m, "首页没有 import map"
        # import map 必须在第一个模块脚本之前
        assert html.index('type="importmap"') < html.index('type="module"')
        imports = json.loads(m.group(1))["imports"]
        js_dir = _srv._STATIC_DIR / "js"
        mods = sorted(p for p in js_dir.rglob("*.js") if not p.name.startswith("._"))
        assert len(mods) > 5
        for p in mods:
            url = "/static/" + p.relative_to(_srv._STATIC_DIR).as_posix()
            assert url in imports, f"{url} 不在 import map 里"
            ver = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            assert imports[url] == f"{url}?v={ver}"

    @pytest.mark.asyncio
    async def test_static_files_served(self, env: SimpleEnv) -> None:
        r = await env.client.get("/static/js/api.js")
        assert r.status == 200
        assert "api(" in (await r.text())
        assert "javascript" in r.headers.get("Content-Type", "")
        # 旧的单文件已经拆掉，不留两份
        assert not (Path(__file__).resolve().parent.parent / "maiwork/console/static/app.js").exists()

    @pytest.mark.asyncio
    async def test_group_short_link_redirects(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        r = await env.client.get(f"/g/{token}", allow_redirects=False)
        assert r.status == 302
        assert r.headers["Location"] == f"/#/{token}/news"

    @pytest.mark.asyncio
    async def test_unknown_api_is_json_404(self, env: SimpleEnv) -> None:
        r = await env.client.get("/api/no-such-thing")
        assert r.status == 404
        assert (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_m3_routes_need_identity_anonymous_is_401(self, env: SimpleEnv) -> None:
        """M3 路由已实现（不再 501）：匿名一律 401。"""
        for path in (
            "/api/requests/R-1/approve",
            "/api/requests/R-1/reject",
            "/api/tasks/T-1/pause",
            "/api/tasks/T-1/resume",
            "/api/tasks/T-1/cancel",
            "/api/tasks/T-1/retry",
            "/api/tasks/T-1/redeliver",
            "/api/goals/G-1/pause",
            "/api/goals/G-1/resume",
            "/api/goals/G-1/cancel",
        ):
            r = await env.client.post(path, json={})
            assert r.status == 401, (path, r.status)
        r = await env.client.get("/api/tasks/T-1")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_m2_routes_503_when_modules_missing(self, env: SimpleEnv) -> None:
        """feeds/topics 没就位时 M2 路由给 503「这个功能还没开」。"""
        # 同事把 feeds.py 写好了也可能没写好：无论哪种，这里手动替成 None 模拟「还没开」
        real_feeds, real_topics = env.app.feeds, env.app.topics
        env.app.feeds, env.app.topics = None, None
        try:
            await env.login()
            token = env.app.token_of(G1)
            for ident_headers in ({}, {"X-MW-Group": token}):
                for path in (
                    "/api/news/1/feedback",
                    "/api/ideas/1/feedback",
                    "/api/ideas/1/do",
                    "/api/ideas/1/dismiss",
                ):
                    r = await env.client.post(path, json={"value": "up", "prev": None}, headers=ident_headers)
                    assert r.status == 503, (path, ident_headers, r.status)
                    assert (await r.json())["error"] == "这个功能还没开"
                r = await env.client.post("/api/topics/1/verdict", json={"value": "right"}, headers=ident_headers)
                # 已带管理员 cookie（_identify 先看 cookie），无论有没有 X-MW-Group 都视作管理员 → 503
                assert r.status == 503, (ident_headers, r.status)
                assert (await r.json())["error"] == "这个功能还没开"
        finally:
            env.app.feeds, env.app.topics = real_feeds, real_topics

    @pytest.mark.asyncio
    async def test_me_none_without_identity(self, env: SimpleEnv) -> None:
        r = await env.client.get("/api/me")
        assert r.status == 200
        data = await r.json()
        assert data["role"] == "none"
        assert data["group"] is None
        assert data["bot"]["name"]  # 拿不到时用 MaiBot
        assert data["bot"]["avatar"].startswith("/api/avatar/bot?v=")
        assert data["now"] > 0


# ----------------------------------------------------------------------
# 管理员登录 / cookie
# ----------------------------------------------------------------------


class TestAdminAuth:
    @pytest.mark.asyncio
    async def test_settings_requires_admin(self, env: SimpleEnv) -> None:
        r = await env.client.get("/api/settings")
        assert r.status == 401
        assert (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_login_wrong_password_401(self, env: SimpleEnv) -> None:
        r = await env.login("不对的密码")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_login_ok_cookie_works(self, env: SimpleEnv) -> None:
        r = await env.login()
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True
        # 同一个 client 带着 cookie 再请求
        r2 = await env.client.get("/api/settings")
        assert r2.status == 200
        me = await (await env.client.get("/api/me")).json()
        assert me["role"] == "admin"

    @pytest.mark.asyncio
    async def test_cookie_attributes(self, env: SimpleEnv) -> None:
        r = await env.login()
        assert r.status == 200
        cookies = env.client.session.cookie_jar.filter_cookies(__import__("yarl").URL(env.base))
        c = cookies.get("mw_admin")
        assert c is not None

    @pytest.mark.asyncio
    async def test_five_wrong_then_429(self, env: SimpleEnv) -> None:
        for _ in range(5):
            r = await env.login("bad")
            assert r.status == 401
        r = await env.login("bad")
        assert r.status == 429
        # 对的密码也被限流
        r = await env.login(PASSWORD)
        assert r.status == 429

    @pytest.mark.asyncio
    async def test_logout_clears(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.post("/api/logout", json={})
        assert r.status == 200
        r2 = await env.client.get("/api/settings")
        assert r2.status == 401

    @pytest.mark.asyncio
    async def test_change_password_invalidates_old_cookie(self, env: SimpleEnv, tmp_path: Path) -> None:
        await env.login()
        assert (await env.client.get("/api/settings")).status == 200
        raw2 = _raw_config(tmp_path / "data")
        raw2["console"]["password"] = "另一个新密码"
        await env.app.update_config(raw2)
        r = await env.client.get("/api/settings")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_generated_password_file(self, tmp_path: Path, caplog) -> None:
        raw = _raw_config(tmp_path / "data2")
        raw["console"]["password"] = ""
        ctx = FakeCtx({})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        with caplog.at_level(logging.INFO):
            await app.start()
        try:
            pw_file = tmp_path / "data2" / "console_password.txt"
            assert pw_file.is_file()
            password = pw_file.read_text(encoding="utf-8").strip()
            assert len(password) == 16
            # 明文不出现在任何日志里
            assert password not in caplog.text
            assert "console_password.txt" in caplog.text
            # 能用这个密码登录
            server = TestServer(app.console.app)
            tc = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
            await tc.start_server()
            try:
                r = await tc.post("/api/login", json={"password": password})
                assert r.status == 200
            finally:
                await tc.close()
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_origin_mismatch_403(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.post(
            "/api/logout",
            json={},
            headers={"Origin": "http://evil.example.com:9999"},
        )
        assert r.status == 403
        # 同源的 Origin 放行（客户端实际连的 host:port）
        await env.login()
        origin = str(env.client.make_url("/")).rstrip("/")
        r2 = await env.client.post("/api/logout", json={}, headers={"Origin": origin})
        assert r2.status == 200


# ----------------------------------------------------------------------
# 群友身份
# ----------------------------------------------------------------------


class TestMemberAccess:
    @pytest.mark.asyncio
    async def test_member_me(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        r = await env.client.get("/api/me", headers={"X-MW-Group": token})
        data = await r.json()
        assert data["role"] == "member"
        assert data["group"] == G1

    @pytest.mark.asyncio
    async def test_bad_token_is_none(self, env: SimpleEnv) -> None:
        r = await env.client.get("/api/me", headers={"X-MW-Group": "badtoken"})
        assert (await r.json())["role"] == "none"

    @pytest.mark.asyncio
    async def test_member_groups_only_own_and_no_token_focus(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        r = await env.client.get("/api/groups", headers={"X-MW-Group": token})
        assert r.status == 200
        groups = await r.json()
        assert len(groups) == 1
        assert groups[0]["id"] == G1
        assert "token" not in json.dumps(groups)
        assert "focus" not in json.dumps(groups)

    @pytest.mark.asyncio
    async def test_member_other_group_403(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        token2 = env.app.token_of(G2)
        # 自己的群可以
        r = await env.client.get(f"/api/groups/{token}", headers={"X-MW-Group": token})
        assert r.status == 200
        view = await r.json()
        assert view["id"] == G1
        assert "token" not in json.dumps(view)
        assert "focus" not in json.dumps(view)
        # 别人的群链接码 / 群号都不行
        r2 = await env.client.get(f"/api/groups/{token2}", headers={"X-MW-Group": token})
        assert r2.status == 403
        r3 = await env.client.get(f"/api/groups/{G2}", headers={"X-MW-Group": token})
        assert r3.status == 403
        # 群友用群号（不是链接码）也看不了——必须用链接码
        r4 = await env.client.get(f"/api/groups/{G1}", headers={"X-MW-Group": token})
        assert r4.status == 403

    @pytest.mark.asyncio
    async def test_member_admin_endpoints_403(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        checks = [
            ("GET", f"/api/settings"),
            ("GET", f"/api/settings/endpoints"),
            ("PUT", f"/api/settings/endpoints/e1"),
            ("DELETE", f"/api/settings/endpoints/e1"),
            ("POST", f"/api/settings/endpoints/e1/test"),
            ("PUT", f"/api/settings/model-list/m1"),
            ("DELETE", f"/api/settings/model-list/m1"),
            ("POST", f"/api/groups/{G1}/profile"),
            ("POST", f"/api/groups/{G1}/focus"),
            ("POST", f"/api/groups/{G1}/token"),
        ]
        for method, path in checks:
            r = await env.client.request(method, path, json={}, headers={"X-MW-Group": token})
            assert r.status == 403, (method, path, r.status)
        for method, path in (("PATCH", "/api/profile/1"), ("DELETE", "/api/profile/1")):
            r = await env.client.request(method, path, json={}, headers={"X-MW-Group": token})
            assert r.status == 403, (method, path)


# ----------------------------------------------------------------------
# 密钥不外泄
# ----------------------------------------------------------------------


class TestSecretNeverLeaves:
    @pytest.mark.asyncio
    async def test_secret_not_in_responses(self, env: SimpleEnv) -> None:
        await env.login()
        bodies = []
        for path in ("/api/settings", "/api/me", "/api/groups", f"/api/groups/{G1}"):
            r = await env.client.get(path)
            bodies.append(await r.text())
        # 2026-10 改版 1a：模型走端点+模型库；密钥只进不出
        r = await env.client.put(
            "/api/settings/endpoints/e1",
            json={"id": "e1", "base_url": "https://ep2.test/v1", "api_key": SECRET},
        )
        assert r.status == 200
        bodies.append(await r.text())
        r = await env.client.get("/api/settings/endpoints")
        assert r.status == 200
        bodies.append(await r.text())
        r = await env.client.post("/api/settings/endpoints/e1/test", json={"base_url": "https://ep2.test/v1"})
        bodies.append(await r.text())
        r = await env.client.put("/api/settings/model-list/m9", json={"endpoint": "e1", "model": "mm"})
        bodies.append(await r.text())
        for body in bodies:
            assert SECRET not in body
            assert "api_key" not in body


# ----------------------------------------------------------------------
# 管理员业务操作
# ----------------------------------------------------------------------


class TestAdminOps:
    @pytest.mark.asyncio
    async def test_groups_admin_has_token_and_focus(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.get("/api/groups")
        groups = await r.json()
        assert {g["id"] for g in groups} == {G1, G2}
        assert all(g.get("token") for g in groups)
        # GroupView 管理员有 focus（关注成员，只给管理员）
        view = await (await env.client.get(f"/api/groups/{G2}")).json()
        assert "focus" in view
        assert view["workspace"] == f"g{G2}"
        assert view["id"] == G2

    @pytest.mark.asyncio
    async def test_groups_not_in_config_not_listed(self, env: SimpleEnv) -> None:
        # 直接往 groups 表插一个不在配置里的群，不应出现在列表里
        with env.app.store.tx() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, workspace, token, created) VALUES ('555', 'g555', 'tok555', 1.0)"
            )
        await env.login()
        groups = await (await env.client.get("/api/groups")).json()
        assert {g["id"] for g in groups} == {G1, G2}

    @pytest.mark.asyncio
    async def test_token_reset_invalidates_old(self, env: SimpleEnv) -> None:
        await env.login()
        old = env.app.token_of(G1)
        r = await env.client.post(f"/api/groups/{G1}/token", json={})
        assert r.status == 200
        new = (await r.json())["token"]
        assert new and new != old
        # 旧链接码不再映射到群（匿名视角：角色变 none）
        from CharTyr_MaiWork.maiwork.console import views as _views

        assert _views.group_id_by_token(env.app, old) is None
        r2 = await env.client.get("/api/me", headers={"X-MW-Group": old})
        me2 = await r2.json()
        assert me2["group"] is None  # 旧 token 带不出群了
        # 登出后用旧链接连身份都进不去
        await env.client.post("/api/logout", json={})
        r3 = await env.client.get("/api/me", headers={"X-MW-Group": old})
        assert (await r3.json())["role"] == "none"
        # 新链接可用
        r4 = await env.client.get("/api/me", headers={"X-MW-Group": new})
        assert (await r4.json())["group"] == G1

    @pytest.mark.asyncio
    async def test_profile_add_edit_delete(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.post(f"/api/groups/{G1}/profile", json={"category": "recent", "text": "在聊键帽"})
        assert r.status == 200
        entry_id = (await r.json())["id"]
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        recent = next(s for s in view["profile"] if s["category"] == "recent")
        assert any(e["id"] == entry_id and e["text"] == "在聊键帽" for e in recent["entries"])
        r = await env.client.request("PATCH", f"/api/profile/{entry_id}", json={"text": "在聊铝坨坨", "locked": True})
        assert r.status == 200
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        text = json.dumps(view, ensure_ascii=False)
        assert "在聊铝坨坨" in text
        r = await env.client.delete(f"/api/profile/{entry_id}")
        assert r.status == 200
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        assert "在聊铝坨坨" not in json.dumps(view, ensure_ascii=False)
        # 非法类别
        r = await env.client.post(f"/api/groups/{G1}/profile", json={"category": "胡说", "text": "x"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_focus_add_remove(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.post(f"/api/groups/{G1}/focus", json={"user_id": "10001", "action": "add"})
        assert r.status == 200
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        assert any(p["user_id"] == "10001" for p in view["focus"])
        r = await env.client.post(f"/api/groups/{G1}/focus", json={"user_id": "10001", "action": "remove"})
        assert r.status == 200
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        assert not any(p["user_id"] == "10001" for p in view["focus"])
        # 非法 action
        r = await env.client.post(f"/api/groups/{G1}/focus", json={"user_id": "10001", "action": "fly"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_models_save_and_test_endpoint(self, env: SimpleEnv) -> None:
        """2026-10 改版 1a：网页建端点 + 模型条目 + 岗位选择；test 端点失败消息不带密钥。"""
        await env.login()
        r = await env.client.put(
            "/api/settings/endpoints/e1",
            json={"id": "e1", "base_url": "https://new.test/v1", "api_key": "k-1"},
        )
        assert r.status == 200
        data = await r.json()
        e1 = next(e for e in data["endpoints"] if e["id"] == "e1")
        assert e1["base_url"] == "https://new.test/v1"
        assert e1["key_set"] is True
        # 建模型条目并给「主模型」「任务」选上 → 整体就绪
        r = await env.client.put("/api/settings/model-list/m1", json={"endpoint": "e1", "model": "gpt-x"})
        assert r.status == 200
        r = await env.client.put("/api/agents/main", json={"model": "m1"})
        assert r.status == 200
        r = await env.client.put("/api/agents/task", json={"model": "m1"})
        assert r.status == 200
        s = await (await env.client.get("/api/settings")).json()
        assert s["models"]["ready"] is True
        # 校验失败
        r = await env.client.put("/api/settings/endpoints/bad", json={"id": "bad", "base_url": "notaurl"})
        assert r.status == 400
        assert (await r.json())["error"]
        # test 端点：端点连不上时 ok=False 且错误信息不带密钥
        r = await env.client.post("/api/settings/endpoints/e1/test", json={"base_url": "http://127.0.0.1:9/x"})
        assert r.status == 200
        t = await r.json()
        assert t["ok"] is False
        assert SECRET not in json.dumps(t)

    @pytest.mark.asyncio
    async def test_models_save_keeps_retry_rate_and_context(self, env: SimpleEnv) -> None:
        """网页端点表单里的重试、请求频率，模型条目里的上下文长度、最大输出：保存后要真的存下，
        不能被路由丢掉、再显示回原来的值（用户实测踩到旧表单同款问题）。"""
        await env.login()
        body = {"id": "e1", "base_url": "https://new.test/v1", "api_key": "k-1",
                "retries": 3, "retry_delay_s": 20, "max_concurrency": 5, "max_rpm": 30}
        r = await env.client.put("/api/settings/endpoints/e1", json=body)
        assert r.status == 200, await r.text()
        want = {k: body[k] for k in ("retries", "retry_delay_s", "max_concurrency", "max_rpm")}
        ep = next(e for e in (await r.json())["endpoints"] if e["id"] == "e1")
        assert {k: ep[k] for k in want} == want
        # 重新拉也是新值
        s = await (await env.client.get("/api/settings/endpoints")).json()
        ep = next(e for e in s["endpoints"] if e["id"] == "e1")
        assert {k: ep[k] for k in want} == want
        # 模型条目的上下文 / 最大输出同样存下
        r = await env.client.put(
            "/api/settings/model-list/m1",
            json={"endpoint": "e1", "model": "gpt-x", "context_window": 200000, "max_tokens": 8192},
        )
        assert r.status == 200
        m1 = next(m for m in (await r.json())["models"] if m["id"] == "m1")
        assert {k: m1[k] for k in ("context_window", "max_tokens")} == \
               {"context_window": 200000, "max_tokens": 8192}
        # 越界值要报错，不能悄悄吞掉
        r = await env.client.put("/api/settings/endpoints/e1", json={**body, "max_concurrency": 99})
        assert r.status == 400
        r = await env.client.put(
            "/api/settings/model-list/m1", json={"context_window": 200000, "max_tokens": 999}
        )
        assert r.status == 400
        # 不传这几项 = 保留当前值
        r = await env.client.put("/api/settings/endpoints/e1", json={"id": "e1", "base_url": "https://new.test/v1"})
        ep = next(e for e in (await r.json())["endpoints"] if e["id"] == "e1")
        assert {k: ep[k] for k in want} == want


# ----------------------------------------------------------------------
# Settings 视图结构
# ----------------------------------------------------------------------


class TestSettingsView:
    @pytest.mark.asyncio
    async def test_settings_shape(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.get("/api/settings")
        assert r.status == 200
        s = await r.json()
        assert s["models"]["base_url"] == "https://ep.test/v1"
        assert s["models"]["key_set"] is True
        assert s["models"]["ready"] is True
        assert s["usage"]["today"]["jev"] == 0
        assert s["usage"]["alert_daily_tokens"] == 0
        states = {h["key"]: h for h in s["health"]}
        assert states["models"]["state"] == "warn"
        assert "未验证" in states["models"]["text"]  # 配置已选好 ≠ 当前配置已经验证
        # 测试机没有 Jev 密钥文件 →「没找到密钥」warn
        assert states["jev"]["state"] == "warn"
        assert states["jev"]["text"] == "没找到密钥"
        assert states["search"]["state"] == "warn"
        assert "还没指定联网搜索" in states["search"]["text"]
        assert "内部代号" not in json.dumps(s, ensure_ascii=False)
        # M3：启动时判定成 fixed（conftest 的假探测）→ ok「隔离运行（固定账号 maiwork）」
        assert states["localenv"]["state"] == "ok"
        assert states["localenv"]["text"] == "隔离运行（固定账号 maiwork）"
        assert s["rules"]["seed_only"] is True
        assert s["rules"]["group_managed"] is True
        assert "每个群" in s["rules"]["note"]
        assert len(s["groups"]) == 2
        g = next(x for x in s["groups"] if x["id"] == G1)
        assert g["link"].startswith(f"/#/{g['token']}/news")
        assert g["token"]
        assert isinstance(s["problems"], list)

    @pytest.mark.asyncio
    async def test_public_url_prefixes_link(self, tmp_path: Path) -> None:
        raw = _raw_config(tmp_path / "d3")
        raw["console"]["public_url"] = "https://mw.example.com/"
        ctx = FakeCtx({})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        server, client = TestServer(app.console.app), None
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        try:
            await client.post("/api/login", json={"password": PASSWORD})
            s = await (await client.get("/api/settings")).json()
            for g in s["groups"]:
                assert g["link"].startswith("https://mw.example.com/#/")
                assert g["link"].endswith("/news")
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# 视图细节
# ----------------------------------------------------------------------


class TestViewDetails:
    @pytest.mark.asyncio
    async def test_missing_group_row_still_renders(self, env: SimpleEnv) -> None:
        # 删掉 G2 的 groups 行，视图照样出（fresh）
        with env.app.store.tx() as conn:
            conn.execute("DELETE FROM groups WHERE group_id=?", (G2,))
        await env.login()
        r = await env.client.get(f"/api/groups/{G2}")
        assert r.status == 200
        view = await r.json()
        assert view["id"] == G2
        assert view["fresh"] is True
        assert view["members"] == 0
        # 群名空 → 清洗回退「群 <群号>」（names.py，视图兜底）
        assert view["name"] == f"群 {G2}"

    @pytest.mark.asyncio
    async def test_icon_stable_and_from_icons_dir(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import group_icons

        icons = group_icons()
        assert icons
        assert "gear" not in icons and "lock" not in icons and "sleeping" not in icons
        a = icon_for_group("900000001")
        assert a == icon_for_group("900000001")
        assert a in icons

    @pytest.mark.asyncio
    async def test_quiet_last_msg_ts_uses_max(self, env: SimpleEnv) -> None:
        # 库里 last_msg_ts = 100，信号里更新到 200 → 视图取 200
        with env.app.store.tx() as conn:
            conn.execute("UPDATE groups SET last_msg_ts=100 WHERE group_id=?", (G1,))
        env.app.signals.mark(G1, "sess", 200.0)
        await env.login()
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        assert view["quiet"]["last_msg_ts"] == 200.0
        # 信号取走后回落到库里的值
        env.app.signals.take()
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        assert view["quiet"]["last_msg_ts"] == 100.0

    @pytest.mark.asyncio
    async def test_profile_only_undeleted(self, env: SimpleEnv) -> None:
        await env.login()
        await env.client.post(f"/api/groups/{G1}/profile", json={"category": "recent", "text": "活着的"})
        r = await env.client.post(f"/api/groups/{G1}/profile", json={"category": "recent", "text": "要删掉的"})
        entry_id = (await r.json())["id"]
        await env.client.delete(f"/api/profile/{entry_id}")
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        dumped = json.dumps(view["profile"], ensure_ascii=False)
        assert "活着的" in dumped
        assert "要删掉的" not in dumped
        # 五类齐、名字对
        names = {s["category"]: s["name"] for s in view["profile"]}
        assert names == {
            "recent": "最近在聊",
            "interest": "长期兴趣",
            "ongoing": "在做的事",
            "convention": "约定和说法",
            "resource": "常用资源",
        }

    @pytest.mark.asyncio
    async def test_pulse_shape(self, env: SimpleEnv) -> None:
        await env.login()
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        p = view["pulse"]
        assert p["step"] == 900
        assert len(p["bins"]) == 96
        assert p["sleep"] == "23:00-08:00"
        assert p["spells"] == [] and p["topics"] == []

    @pytest.mark.asyncio
    async def test_upcoming_fresh_and_read_interval(self, env: SimpleEnv) -> None:
        await env.login()
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        # fresh（群行没有 profile_ready_ts）：提示读够记录后整理画像
        up = view["upcoming"]
        assert any("画像" in u["text"] for u in up)
        # 模型已配好 + 有 read_since → 有「下一次读群」
        with env.app.store.tx() as conn:
            conn.execute("UPDATE groups SET read_since=?, profile_ready_ts=? WHERE group_id=?", (clock.now(), clock.now(), G1))
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        assert any("下一次读群" in u["text"] for u in view["upcoming"])


# ----------------------------------------------------------------------
# M2 路由 + GroupView 新字段（docs/07 §9.2/§9.3）
# ----------------------------------------------------------------------


class _FakeStore:
    """给路由测试用的极简 store：只支持 SELECT group_id FROM <table> WHERE id=?。"""

    def __init__(self, mapping: dict[str, dict[int, str]] | None = None):
        # mapping[table][item_id] = group_id
        self._mapping = mapping or {}
        self.read_calls: list[tuple[str, tuple]] = []
        self._secrets: dict[str, str] = {}
        self._kv: dict[str, Any] = {}

    def secret_get(self, name: str) -> str:
        return self._secrets.get(name, "")

    def secret_set(self, conn, name: str, value: str) -> None:
        self._secrets[name] = value

    def kv_get(self, key: str, default=None):
        return self._kv.get(key, default)

    def kv_set(self, conn, key: str, value) -> None:
        self._kv[key] = value

    def tx(self):
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            yield self

        return _tx()

    def read(self):
        outer = self

        class _Cursor:
            def __init__(self, sql, params):
                self._sql = sql
                self._params = params

            def fetchone(self):
                sql = self._sql
                for table in ("news_items", "ideas", "topic_log"):
                    if f"FROM {table}" in sql:
                        gid = outer._mapping.get(table, {}).get(int(self._params[0]))
                        if gid is None:
                            return None
                        return {"group_id": gid}
                return None

        class _Conn:
            def execute(self, sql, params):
                return _Cursor(sql, params)

        return _Conn()


class _FakeSvc:
    """M2 路由测试用的服务对象；server.py 里用到的属性都给齐。"""

    def __init__(self, *, feeds=None, topics=None, store=None, settings=None, host=None):
        self.feeds = feeds
        self.topics = topics
        self.store = store
        self.get_settings = lambda: settings
        self.host = host
        self.profiles = None
        self.models = None
        self.scheduler = None
        self.jev = None
        self.signals = None
        self.is_served = lambda gid: True


def _settings_stub():
    """够用来跑 _served_group_ids / is_served / delivery / profile 的最小 Settings。"""
    class _S:
        class delivery:
            quiet_hours = "23:00-08:00"
            push_per_day = 3

        class profile:
            read_interval_minutes = 10

        class topics:
            per_day = 2
            min_gap_hours = 3

        class approval:
            required = True
            admins = []

        class usage:
            alert_daily_tokens = 0

        class console:
            password = "x"
            public_url = ""

        class models:
            base_url = ""
            api_key = ""
            main = ""
            main_backup = ""
            worker = ""
            worker_backup = ""

        class search:
            provider = ""
            api_key = ""
            timeout_s = 20

        class jev:
            enabled = True
            timeout_ms = 1500
            key_file = ""
            api_url = ""
            model = ""

        problems: list = []
        groups: dict = {}

        def is_served(self, gid):
            return True

        def workspace_of(self, gid):
            return f"g{gid}"

    return _S()


class _FakeFeeds:
    """console 路由测试用：只实现 §10.4 的方法 + 记录。"""

    def __init__(self) -> None:
        self.feedback_calls = []
        self.idea_calls = []
        self.missing = set()
        self.feedback_result = {"up": 2, "down": 1}
        self.idea_result = {"id": 1, "state": "wanted"}

    def feedback(self, kind, item_id, value, prev):
        self.feedback_calls.append((kind, int(item_id), value, prev))
        if int(item_id) in self.missing:
            raise KeyError(item_id)
        return dict(self.feedback_result)

    def idea_action(self, idea_id, op, *, by="", item_nos=None):
        self.idea_calls.append((int(idea_id), str(op), str(by), item_nos))
        if int(idea_id) in self.missing:
            raise KeyError(idea_id)
        out = dict(self.idea_result)
        out["state"] = {"want": "wanted", "do": "pending", "dismiss": "dismissed"}.get(op, out.get("state"))
        return out

    def news_view(self, group_id, *, days=3):
        return []

    def ideas_view(self, group_id):
        return []

    def today_count(self, group_id):
        return 0


class _FakeTopics:
    def __init__(self) -> None:
        self.verdict_calls = []

    def log_view(self, group_id, *, days=3):
        return []

    def verdict(self, topic_id, value):
        self.verdict_calls.append((int(topic_id), value))


@pytest_asyncio.fixture
async def m2_client(tmp_path: Path):
    """纯 M2 路由的测试服务器：不带 app、不带 MaiBot；只测路由 + 鉴权。"""
    import json as _json

    from aiohttp.test_utils import TestClient, TestServer

    from CharTyr_MaiWork.maiwork.console.server import create_app

    feeds = _FakeFeeds()
    topics = _FakeTopics()
    # 条目归属：news_items 1→G1，2→G2；ideas 11→G1，22→G2；topic_log 5→G1
    store = _FakeStore(
        {
            "news_items": {1: G1, 2: G2},
            "ideas": {11: G1, 22: G2},
            "topic_log": {5: G1},
        }
    )
    svc = _FakeSvc(feeds=feeds, topics=topics, store=store, settings=_settings_stub())
    app = create_app(svc)

    # 拿一个「能让 _identify 返回 member@G1」的身份：直接伪造（不改 db）
    from CharTyr_MaiWork.maiwork.console import views as _views

    orig = _views.group_id_by_token
    _views.group_id_by_token = lambda _svc, token: (G1 if token == "tok-g1" else None)
    # create_app 内部调 views.group_id_by_token，是通过模块引用——monkey-patch 模块级函数
    try:
        server = TestServer(app)
        client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await client.start_server()
        yield SimpleEnv(app=svc, client=client, tmp_path=tmp_path)  # app=svc 不是真 app，但 SimpleEnv 只用 .console.port
    finally:
        _views.group_id_by_token = orig
        await client.close()


class TestM2RoutesAuth:
    """M2 路由鉴权（群友只能管本群的条目、管理员随便；verdict 只管理员）。"""

    @pytest.mark.asyncio
    async def test_anonymous_feedback_gives_401(self, m2_client) -> None:
        for path in ("/api/news/1/feedback", "/api/ideas/11/feedback"):
            r = await m2_client.client.post(path, json={"value": "up", "prev": None})
            assert r.status == 401, (path, r.status)

    @pytest.mark.asyncio
    async def test_member_can_only_feedback_own_group(self, m2_client) -> None:
        # 本群（G1）头条 → 200
        r = await m2_client.client.post(
            "/api/news/1/feedback",
            json={"value": "up", "prev": None},
            headers={"X-MW-Group": "tok-g1"},
        )
        assert r.status == 200, r.status
        # 别群（G2）头条 → 403
        r = await m2_client.client.post(
            "/api/news/2/feedback",
            json={"value": "up", "prev": None},
            headers={"X-MW-Group": "tok-g1"},
        )
        assert r.status == 403, r.status

    @pytest.mark.asyncio
    async def test_member_cannot_do_dismiss_verdict(self, m2_client) -> None:
        for path in ("/api/ideas/11/do", "/api/ideas/11/dismiss", "/api/topics/5/verdict"):
            r = await m2_client.client.post(path, json={"value": "right"}, headers={"X-MW-Group": "tok-g1"})
            assert r.status == 403, (path, r.status)

    @pytest.mark.asyncio
    async def test_member_want_route_removed(self, m2_client) -> None:
        # 「想要这个」已删（docs/18 §五）：/api/ideas/{id}/want 路由摘掉，member 也拿不到
        for iid in (11, 22):
            r = await m2_client.client.post(
                f"/api/ideas/{iid}/want", json={}, headers={"X-MW-Group": "tok-g1"}
            )
            assert r.status == 404, (iid, r.status)

    @pytest.mark.asyncio
    async def test_admin_can_do_dismiss(self, m2_client) -> None:
        # 模拟管理员：摸回到 aiohttp app 拿 ConsoleAuth，做合法 cookie 塞进 cookie jar
        from CharTyr_MaiWork.maiwork.console.auth import COOKIE_NAME
        from CharTyr_MaiWork.maiwork.console.server import AUTH_KEY

        auth = None
        for v in vars(m2_client.client._server).values():
            try:
                cand = v[AUTH_KEY]
                auth = cand
                break
            except Exception:
                continue
        assert auth is not None, "摸不到 ConsoleAuth"
        value, _max_age = auth.make_cookie()
        m2_client.client.session.cookie_jar.update_cookies({COOKIE_NAME: value})
        for path, want_op in (("/api/ideas/11/do", "do"), ("/api/ideas/11/dismiss", "dismiss")):
            r = await m2_client.client.post(path, json={})
            assert r.status == 200, (path, r.status)
            data = await r.json()
            assert data["state"] == ("pending" if want_op == "do" else "dismissed")
        # verdict
        r = await m2_client.client.post("/api/topics/5/verdict", json={"value": "wrong"})
        assert r.status == 200, r.status
        r = await m2_client.client.post("/api/topics/5/verdict", json={"value": "fly"})
        assert r.status == 400, r.status

    @pytest.mark.asyncio
    async def test_admin_do_passes_picked_items_to_feeds(self, m2_client) -> None:
        """「直接开工」body 里的 items（构想项目序号）要原样传给 feeds.idea_action。"""
        from CharTyr_MaiWork.maiwork.console.auth import COOKIE_NAME
        from CharTyr_MaiWork.maiwork.console.server import AUTH_KEY

        auth = None
        for v in vars(m2_client.client._server).values():
            try:
                auth = v[AUTH_KEY]
                break
            except Exception:
                continue
        assert auth is not None, "摸不到 ConsoleAuth"
        value, _max_age = auth.make_cookie()
        m2_client.client.session.cookie_jar.update_cookies({COOKIE_NAME: value})

        r = await m2_client.client.post("/api/ideas/11/do", json={"items": [1, 3]})
        assert r.status == 200, r.status
        assert m2_client.app.feeds.idea_calls[-1] == (11, "do", "管理员", [1, 3])

        # 不带 body / body 里没有 items → None（= 全部）
        r = await m2_client.client.post("/api/ideas/11/do")
        assert r.status == 200, r.status
        assert m2_client.app.feeds.idea_calls[-1] == (11, "do", "管理员", None)

    @pytest.mark.asyncio
    async def test_feedback_unknown_item_404(self, m2_client) -> None:
        r = await m2_client.client.post("/api/news/999/feedback", json={"value": "up"}, headers={"X-MW-Group": "tok-g1"})
        assert r.status == 404, r.status


# ----------------------------------------------------------------------
# M2 的 GroupView 新字段（news/ideas/topic_log/pulse/today/upcoming）
# ----------------------------------------------------------------------


class TestGroupViewM2Fields:
    @pytest.mark.asyncio
    async def test_member_view_topic_log_visible_but_no_verdict(self, m2_client) -> None:
        """群友看得到 topic_log，但没有 verdict 字段；也不含 focus 个人画像信息。"""
        from CharTyr_MaiWork.maiwork.console import views as _views

        # 这个测试需要 _views.group_id_by_token 已 patch（fixture 做了）
        # 用 topics 给一条「已开话题」和一条「忍住没开」
        from CharTyr_MaiWork.maiwork import clock as _clock

        now = _clock.now()
        svc = m2_client.app  # 实际是 _FakeSvc
        # 喂 topic_log
        svc.topics.log_view = lambda gid, *, days=3: [
            {
                "id": 5, "ts": now - 600, "quiet_s": 1800, "usual_gap_s": 600,
                "jev": {"ok": True, "reason": "可以开", "confidence": 0.9, "detail": ""},
                "pick": {"title": "铝坨坨新配色", "fit": 0.83},
                "opener": "今天看到铝坨坨新出的配色，挺戳我",
                "result": {"replies": 2, "followups": 1},
                "verdict": "right",
            },
            {
                "id": 6, "ts": now - 1200, "quiet_s": 3000, "usual_gap_s": 900,
                "jev": {"ok": False, "reason": "气氛不对", "confidence": 0.7, "detail": ""},
                "pick": None, "opener": "", "result": None, "verdict": None,
            },
        ]
        # 喂些 feeds
        svc.feeds.news_map = {}  # _FakeFeeds 没这个属性，但 news_view 不读
        # GroupView（admin=False）
        view = _views.group_view(svc, G1, admin=False)
        assert view["id"] == G1
        assert "focus" not in view
        assert "card_push" not in view  # 往群里发的开关只给管理员 / 本群群管理员
        # topic_log 群友也看得到，但不含 verdict
        assert isinstance(view["topic_log"], list) and len(view["topic_log"]) == 2
        assert all("verdict" not in e for e in view["topic_log"])
        assert view["topic_log"][0]["pick"]["title"] == "铝坨坨新配色"
        # 没有任何字段泄出 focus / personal 信息
        dump = json.dumps(view, ensure_ascii=False)
        assert "focus" not in dump
        assert "reasons" not in dump
        # pulse.topics：开了的那条会说话
        topics_pts = view["pulse"]["topics"]
        assert any(t["label"] == "铝坨坨新配色" and t["replies"] == 2 for t in topics_pts)
        # pulse.spells：两条记录都有冷场段
        spells = view["pulse"]["spells"]
        assert len(spells) == 2
        notes = [s["note"] for s in spells]
        assert any("冷场 30 分钟 · 开了话题" in n for n in notes)
        assert any("冷场 50 分钟 · 没开" in n for n in notes)
        # 时段排序
        starts = [s["from"] for s in spells]
        assert starts == sorted(starts)

    @pytest.mark.asyncio
    async def test_pulse_spells_merge_rejudges_of_one_quiet_stretch(self, m2_client) -> None:
        """同一段冷场（最近消息时间相同）判了好几次 → 群脉搏只画一段，写明判了几次。

        线上 2026-10-01：每 30 秒判一次，一段冷场 40 条记录，脉搏上叠了 40 段。
        新记录用 jev.stretch_ts 认段；旧记录没有就用 ts - quiet_s（相差 90 秒内算同一段）。
        """
        from CharTyr_MaiWork.maiwork import clock as _clock
        from CharTyr_MaiWork.maiwork.console import views as _views

        now = _clock.now()
        start = now - 3000  # 最近一条消息的时刻
        svc = m2_client.app
        rows = []
        for k in range(5):  # 旧记录：同一段里每 30 秒一条（没 stretch_ts）
            ts = now - 1500 + k * 30
            rows.append({"id": 10 + k, "ts": ts, "quiet_s": ts - start + (k % 2), "usual_gap_s": 84,
                         "jev": {"ok": False, "reason": "气氛不对", "confidence": 0.4, "detail": ""},
                         "pick": {}, "opener": "", "result": None, "verdict": None})
        other_start = now - 9000  # 另一段（新记录，带 stretch_ts）
        rows.append({"id": 3, "ts": other_start + 1300, "quiet_s": 1300, "usual_gap_s": 84,
                     "jev": {"ok": False, "reason": "可以开", "confidence": 0.2, "detail": "",
                             "ok_p": 0.5, "stretch_ts": other_start},
                     "pick": {}, "opener": "", "result": None, "verdict": None})
        rows.sort(key=lambda e: -e["ts"])
        svc.topics.log_view = lambda gid, *, days=3: rows
        view = _views.group_view(svc, G1, admin=True)
        spells = view["pulse"]["spells"]
        assert len(spells) == 2, spells
        merged = [s for s in spells if abs(s["from"] - start) < 2][0]
        assert merged["to"] == pytest.approx(now - 1500 + 4 * 30)
        assert "判了 5 次" in merged["note"] and "没开" in merged["note"]
        single = [s for s in spells if abs(s["from"] - other_start) < 2][0]
        assert "判了" not in single["note"]
        assert [s["from"] for s in spells] == sorted(s["from"] for s in spells)

    @pytest.mark.asyncio
    async def test_admin_view_keeps_verdict(self, m2_client) -> None:
        """管理员的 topic_log 保留 verdict。"""
        from CharTyr_MaiWork.maiwork import clock as _clock
        from CharTyr_MaiWork.maiwork.console import views as _views

        now = _clock.now()
        svc = m2_client.app
        svc.topics.log_view = lambda gid, *, days=3: [
            {"id": 5, "ts": now - 60, "quiet_s": 600, "usual_gap_s": 300, "jev": None,
             "pick": {"title": "测试话题", "fit": 0.7}, "opener": "hi", "result": None, "verdict": "wrong"},
        ]
        view = _views.group_view(svc, G1, admin=True)
        assert view["topic_log"][0]["verdict"] == "wrong"

    @pytest.mark.asyncio
    async def test_empty_m2_modules_gives_empty_lists_and_zero_today(self, m2_client) -> None:
        """feeds/topics 给空 → news/ideas/topic_log/pulse.spells/pulse.topics 全空，today.news/topics = 0。"""
        from CharTyr_MaiWork.maiwork.console import views as _views
        svc = m2_client.app
        view = _views.group_view(svc, G1, admin=True)
        assert view["news"] == [] and view["ideas"] == [] and view["topic_log"] == []
        assert view["pulse"]["spells"] == [] and view["pulse"]["topics"] == []
        assert view["today"]["news"] == 0 and view["today"]["topics"] == 0

    @pytest.mark.asyncio
    async def test_today_counts_come_from_feeds_and_topic_log(self, m2_client) -> None:
        """today.news = feeds.today_count；today.topics = topic_log 里今天开了话题的次数。"""
        from CharTyr_MaiWork.maiwork import clock as _clock
        from CharTyr_MaiWork.maiwork.console import views as _views

        now = _clock.now()
        svc = m2_client.app
        svc.feeds.today_count = lambda gid: 3
        svc.topics.log_view = lambda gid, *, days=3: [
            {"id": 1, "ts": now - 60, "quiet_s": 60, "usual_gap_s": None, "opener": "hi", "pick": None, "result": None, "verdict": None},
            {"id": 2, "ts": now - 120, "quiet_s": 60, "usual_gap_s": None, "opener": "", "pick": None, "result": None, "verdict": None},  # 忍住没开不算
            {"id": 3, "ts": now - 90000, "quiet_s": 60, "usual_gap_s": None, "opener": "昨天开的", "pick": None, "result": None, "verdict": None},  # 昨天不算
        ]
        view = _views.group_view(svc, G1, admin=True)
        assert view["today"]["news"] == 3
        assert view["today"]["topics"] == 1

    @pytest.mark.asyncio
    async def test_upcoming_includes_next_news_ts(self, m2_client) -> None:
        """upcoming 里有「下一批资讯备料」（scheduler.next_news_ts）。"""
        from fakes import FakeScheduler

        from CharTyr_MaiWork.maiwork.console import views as _views

        svc = m2_client.app
        sched = FakeScheduler()
        # 「群画像成形」需要 profile_ready_ts 非空——_FakeStore 返回全零，这里直接打补丁
        class _Row(_FakeStore):
            def __init__(self, base_store):
                super().__init__(base_store._mapping)

        # 打补丁：让 _group_row 看得见的行有 profile_ready_ts
        orig_group_row = _views._group_row
        _views._group_row = lambda svc_, gid: {
            "group_id": gid, "workspace": "", "session_id": "", "name": "", "member_count": 0,
            "token": "", "created": 0.0, "last_msg_ts": 0.0, "cursor_ts": 0.0, "cursor_ids": "[]",
            "read_since": 0.0, "profile_ready_ts": 1.0,  # 关键：画像已成形
            "last_refresh_ts": 0.0, "last_weekly_ts": 0.0, "fail_count": 0,
        }
        sched.next_news_map[G1] = 1_790_100_000.0
        svc.scheduler = sched
        # 模型也要就绪，否则 _upcoming 直接 return
        class _MS:
            def ready(self):
                return True

        class _Models:
            def settings(self):
                return _MS()

        svc.models = _Models()
        try:
            view = _views.group_view(svc, G1, admin=False)
            texts = [u["text"] for u in view["upcoming"]]
            assert any(t == "下一批资讯备料" for t in texts)
            entry = next(u for u in view["upcoming"] if u["text"] == "下一批资讯备料")
            assert entry["at"] == 1_790_100_000.0
            assert entry["icon"] == "newspaper"
        finally:
            _views._group_row = orig_group_row


# ----------------------------------------------------------------------
# M3 路由（docs/07 §9.2 M3 段）：任务详情、批准 / 拒绝、任务操作、目标操作
# ----------------------------------------------------------------------


class TestM3Routes:
    def _make_task(self, app: MaiWorkApp, gid: str = G1, *, status: str = "queued") -> str:
        tid = app.tasks.create(gid, title="整理资料", req="req", criteria=[], source="test", env="本机", status="queued")
        if status != "queued":
            if status == "failed":
                app.tasks.transition(tid, "running")
                app.tasks.transition(tid, "failed", review="验收没过")
            else:
                app.tasks.transition(tid, status)
        return tid

    def _make_request(self, app: MaiWorkApp, gid: str = G1) -> dict:
        return app.approvals.create(
            gid, kind="task", title="旧请求", quote="原话", via="群里 @",
            requester_id="20002", requester_name="阿柒",
        )

    # ---------- 任务详情 ----------

    @pytest.mark.asyncio
    async def test_task_detail_admin_has_env_and_timeline(self, env: SimpleEnv) -> None:
        await env.login()
        tid = self._make_task(env.app)
        r = await env.client.get(f"/api/tasks/{tid}")
        assert r.status == 200
        detail = await r.json()
        assert detail["id"] == tid
        assert "env" in detail
        assert "timeline" in detail
        assert detail["steps"] == len(detail["timeline"])
        assert "delivery" in detail and "undelivered" in detail

    @pytest.mark.asyncio
    async def test_task_detail_includes_link_check(self, env: SimpleEnv) -> None:
        """验收引用核对的结构化结果（coordinator 存 kv）要出现在任务详情里；没有就 null。"""
        await env.login()
        tid = self._make_task(env.app)
        r0 = await env.client.get(f"/api/tasks/{tid}")
        assert (await r0.json())["link_check"] is None
        with env.app.store.tx() as conn:
            env.app.store.kv_set(
                conn,
                f"task.link_check.{tid}",
                {
                    "ts": 123.0,
                    "attempt": 1,
                    "links": 3,
                    "unopened": 1,
                    "unopened_urls": ["https://never.example/x"],
                },
            )
        r = await env.client.get(f"/api/tasks/{tid}")
        assert r.status == 200
        detail = await r.json()
        assert detail["link_check"] == {
            "links": 3, "unopened": 1, "unopened_urls": ["https://never.example/x"],
        }
        # 群友版也给这个字段
        token = env.app.token_of(G1)
        r2 = await env.client.get(f"/api/tasks/{tid}", headers={"X-MW-Group": token})
        assert r2.status == 200
        member = await r2.json()
        assert member["link_check"]["unopened"] == 1

    @pytest.mark.asyncio
    async def test_split_tasks_all_get_approval_info(self, env: SimpleEnv) -> None:
        """构想拆成多个任务落地：任务列表和任务详情对**每个**任务都给批准人 / 自动审核理由。"""
        await env.login()
        app = env.app
        now = clock.now()
        with app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'bulb', '做两件事', '', ?, 'wanted', ?, ?)",
                (
                    G1,
                    json.dumps(
                        [
                            {"kind": "task", "title": "第一件", "desc": ""},
                            {"kind": "task", "title": "第二件", "desc": ""},
                        ],
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
            idea_id = int(cur.lastrowid or 0)
        req = app.approvals.create(
            G1, kind="task", title="做两件事", quote="", via="来自构想",
            requester_id="", requester_name="阿柒", idea_id=idea_id,
        )
        res = app.approvals.approve(req["id"], by="网页管理员", auto_reason="查资料的小活，低风险")
        tids = [str(t) for t in res["task_ids"]]
        assert len(tids) == 2
        for tid in tids:
            detail = await (await env.client.get(f"/api/tasks/{tid}")).json()
            assert detail["approved_by"] == "网页管理员"
            assert detail["auto_reason"] == "查资料的小活，低风险"
        view = await (await env.client.get(f"/api/groups/{G1}")).json()
        by_id = {str(t.get("id")): t for t in view["tasks"]["list"]}
        for tid in tids:
            assert by_id[tid]["approved_by"] == "网页管理员"
            assert by_id[tid]["auto_reason"] == "查资料的小活，低风险"

    @pytest.mark.asyncio
    async def test_task_detail_member_sees_no_env_no_timeline(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        tid = self._make_task(env.app, G1)
        r = await env.client.get(f"/api/tasks/{tid}", headers={"X-MW-Group": token})
        assert r.status == 200
        body = await r.text()
        assert '"env"' not in body
        assert '"timeline"' not in body
        detail = json.loads(body)
        assert detail["steps"] == 0  # 群友只看得到步数

    @pytest.mark.asyncio
    async def test_task_detail_member_other_group_403(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        tid = self._make_task(env.app, G2)
        r = await env.client.get(f"/api/tasks/{tid}", headers={"X-MW-Group": token})
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_task_detail_unknown_404(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.get("/api/tasks/T-9999")
        assert r.status == 404

    # ---------- 批准 / 拒绝 ----------

    @pytest.mark.asyncio
    async def test_request_approve_spawns_run_task(self, env: SimpleEnv) -> None:
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        env.app.coordinator = coord
        req = self._make_request(env.app)
        await env.login()
        r = await env.client.post(f"/api/requests/{req['id']}/approve", json={})
        assert r.status == 200
        res = await r.json()
        assert res["status"] == "approved"
        tid = str(res["task_id"])
        for _ in range(30):
            if tid in coord.run_calls:
                break
            await asyncio.sleep(0.02)
        assert tid in coord.run_calls

    @pytest.mark.asyncio
    async def test_request_approve_idea_items_spawns_all_tasks(self, env: SimpleEnv) -> None:
        """网页批准带项目的构想：逐个落任务，返回 task_ids，并把每个都开工。"""
        import json as _json

        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        env.app.coordinator = coord
        with env.app.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, items, state, created, updated)"
                " VALUES (?, 'books', '做铝价表', '整理铝价', ?, 'new', 1, 1)",
                (G1, _json.dumps([
                    {"kind": "task", "title": "抓铝价", "desc": "先抓一个月"},
                    {"kind": "task", "title": "做成表", "desc": ""},
                ], ensure_ascii=False)),
            )
            idea_id = int(cur.lastrowid or 0)
        req = env.app.approvals.create(
            G1, kind="task", title="做铝价表", quote="", via="来自构想",
            requester_id="20002", requester_name="阿柒", idea_id=idea_id,
        )
        await env.login()
        r = await env.client.post(f"/api/requests/{req['id']}/approve", json={})
        assert r.status == 200
        res = await r.json()
        assert res["task_ids"] == ["T-1", "T-2"]
        assert res["task_id"] == "T-1"
        for tid in ("T-1", "T-2"):
            for _ in range(30):
                if tid in coord.run_calls:
                    break
                await asyncio.sleep(0.02)
            assert tid in coord.run_calls

    @pytest.mark.asyncio
    async def test_request_reject_and_state_errors(self, env: SimpleEnv) -> None:
        req = self._make_request(env.app)
        await env.login()
        r = await env.client.post(f"/api/requests/{req['id']}/reject", json={})
        assert r.status == 200
        assert (await r.json())["status"] == "rejected"
        # 再拒一次 → 409；不存在 → 404
        r = await env.client.post(f"/api/requests/{req['id']}/reject", json={})
        assert r.status == 409
        r = await env.client.post("/api/requests/R-9999/approve", json={})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_request_member_forbidden(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        req = self._make_request(env.app)
        r = await env.client.post(
            f"/api/requests/{req['id']}/approve", json={}, headers={"X-MW-Group": token}
        )
        assert r.status == 403

    # ---------- 任务操作 ----------

    @pytest.mark.asyncio
    async def test_task_pause_resume_cancel(self, env: SimpleEnv) -> None:
        await env.login()
        tid = self._make_task(env.app)
        r = await env.client.post(f"/api/tasks/{tid}/pause", json={})
        assert r.status == 200
        assert (await r.json())["status"] == "paused"
        r = await env.client.post(f"/api/tasks/{tid}/resume", json={})
        assert r.status == 200
        assert (await r.json())["status"] == "queued"
        r = await env.client.post(f"/api/tasks/{tid}/cancel", json={})
        assert r.status == 200
        assert (await r.json())["status"] == "cancelled"
        # 取消是终态：再来一次 → 409
        r = await env.client.post(f"/api/tasks/{tid}/cancel", json={})
        assert r.status == 409

    @pytest.mark.asyncio
    async def test_task_retry_failed_goes_queued_and_spawns(self, env: SimpleEnv) -> None:
        from fakes import FakeCoordinator

        coord = FakeCoordinator()
        env.app.coordinator = coord
        await env.login()
        tid = self._make_task(env.app, status="failed")
        r = await env.client.post(f"/api/tasks/{tid}/retry", json={})
        assert r.status == 200
        assert (await r.json())["status"] == "queued"
        for _ in range(30):
            if tid in coord.run_calls:
                break
            await asyncio.sleep(0.02)
        assert tid in coord.run_calls
        # 没失败的任务不让 retry
        tid2 = self._make_task(env.app)
        r = await env.client.post(f"/api/tasks/{tid2}/retry", json={})
        assert r.status == 409

    @pytest.mark.asyncio
    async def test_task_redeliver_retries_failed_outbox(self, env: SimpleEnv) -> None:
        await env.login()
        tid = self._make_task(env.app)
        oid = env.app.outbox.enqueue(
            f"task:{tid}:deliver", G1, "file",
            {"path": "/tmp/x", "name": "x", "push_kind": "delivery"}, task_id=tid,
        )
        env.app.outbox.enqueue(
            f"task:{tid}:done", G1, "text",
            {"text": "做好了", "push_kind": "delivery"}, task_id=tid,
        )
        with env.app.store.tx() as conn:
            conn.execute("UPDATE outbox SET status='failed' WHERE id=?", (oid,))
            conn.execute("UPDATE outbox SET status='sent' WHERE key=?", (f"task:{tid}:done",))
        r = await env.client.post(f"/api/tasks/{tid}/redeliver", json={})
        assert r.status == 200
        row = env.app.store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "pending"  # failed 的重新排队
        row = env.app.store.read().execute("SELECT status FROM outbox WHERE key=?", (f"task:{tid}:done",)).fetchone()
        assert row["status"] == "sent"  # sent 的不动

    @pytest.mark.asyncio
    async def test_task_redeliver_does_not_requeue_removed_group(self, env: SimpleEnv) -> None:
        from dataclasses import replace

        await env.login()
        tid = self._make_task(env.app, G1)
        oid = env.app.outbox.enqueue(
            f"task:{tid}:deliver", G1, "file",
            {"path": "missing", "push_kind": "delivery"}, task_id=tid,
        )
        with env.app.store.tx() as conn:
            conn.execute("UPDATE outbox SET status='failed' WHERE id=?", (oid,))
        before = env.app.base_settings()
        env.app._settings = replace(before, groups={G2: before.groups[G2]})
        r = await env.client.post(f"/api/tasks/{tid}/redeliver", json={})
        assert r.status == 409
        row = env.app.store.read().execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_task_redeliver_recreates_missing_text_outbox(self, env: SimpleEnv) -> None:
        await env.login()
        tid = self._make_task(env.app)
        env.app.tasks.transition(tid, "running")
        env.app.tasks.transition(tid, "reviewing")
        env.app.tasks.transition(tid, "completed", delivery_kind="text")
        assert env.app.delivery.undelivered(tid)
        r = await env.client.post(f"/api/tasks/{tid}/redeliver", json={})
        assert r.status == 200
        rows = env.app.store.read().execute(
            "SELECT key, status, payload FROM outbox WHERE task_id=?", (tid,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["key"] == f"task:{tid}:deliver:text"
        assert rows[0]["status"] == "pending"
        assert "整理资料" in json.loads(rows[0]["payload"])["text"]
        # 重复点不额外入队，也不能因此重传已经发送的成品。
        assert (await env.client.post(f"/api/tasks/{tid}/redeliver", json={})).status == 200
        assert env.app.store.read().execute(
            "SELECT COUNT(*) c FROM outbox WHERE task_id=?", (tid,)
        ).fetchone()["c"] == 1

    @pytest.mark.asyncio
    async def test_task_redeliver_rechecks_missing_file_artifact(self, env: SimpleEnv) -> None:
        await env.login()
        tid = self._make_task(env.app)
        task = env.app.tasks.get(tid)
        ws = env.app.env.workspace(task["workspace"])
        artifact = ws / "artifacts" / tid / "report.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("成品", encoding="utf-8")
        env.app.tasks.transition(tid, "running")
        env.app.tasks.start_attempt(tid)
        env.app.tasks.finish_attempt(
            env.app.tasks.current_attempt_id(tid), status="passed",
            artifacts=[f"artifacts/{tid}/report.txt"],
        )
        env.app.tasks.transition(tid, "reviewing")
        env.app.tasks.transition(tid, "completed", delivery_kind="file")
        r = await env.client.post(f"/api/tasks/{tid}/redeliver", json={})
        assert r.status == 200
        row = env.app.store.read().execute(
            "SELECT status, payload FROM outbox WHERE key=?", (f"task:{tid}:deliver",)
        ).fetchone()
        assert row is not None and row["status"] == "pending"
        assert json.loads(row["payload"])["path"] == str(artifact)

    @pytest.mark.asyncio
    async def test_task_redeliver_rejects_missing_or_escaped_artifact(self, env: SimpleEnv, tmp_path: Path) -> None:
        await env.login()
        tid = self._make_task(env.app)
        outside = tmp_path / "private.txt"
        outside.write_text("不许发送", encoding="utf-8")
        env.app.tasks.transition(tid, "running")
        env.app.tasks.start_attempt(tid)
        env.app.tasks.finish_attempt(
            env.app.tasks.current_attempt_id(tid), status="passed", artifacts=[str(outside)],
        )
        env.app.tasks.transition(tid, "reviewing")
        env.app.tasks.transition(tid, "completed", delivery_kind="file")
        r = await env.client.post(f"/api/tasks/{tid}/redeliver", json={})
        assert r.status == 409
        assert env.app.store.read().execute(
            "SELECT COUNT(*) c FROM outbox WHERE task_id=?", (tid,)
        ).fetchone()["c"] == 0

    # ---------- 目标操作 ----------

    @pytest.mark.asyncio
    async def test_goal_pause_resume_cancel(self, env: SimpleEnv) -> None:
        await env.login()
        gid_goal = env.app.goals.create_agent(G1, title="盯着铝价", body="", criteria=[], by_text="管理员 发起")
        for op, want in (("pause", "paused"), ("resume", "active"), ("cancel", "cancelled")):
            r = await env.client.post(f"/api/goals/{gid_goal}/{op}", json={})
            assert r.status == 200, (op, r.status)
            assert env.app.goals.get(gid_goal)["state"] == want
        r = await env.client.post(f"/api/goals/{gid_goal}/cancel", json={})
        assert r.status == 200  # 重复 cancel 不炸（goals.cancel 是幂等的 _set_state）
        r = await env.client.post("/api/goals/G-9999/pause", json={})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_task_ops_member_forbidden(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        tid = self._make_task(env.app)
        for op in ("pause", "resume", "cancel", "retry", "redeliver"):
            r = await env.client.post(f"/api/tasks/{tid}/{op}", json={}, headers={"X-MW-Group": token})
            assert r.status == 403, (op, r.status)

    # ---------- GroupView / Settings 的 M3 填充 ----------

    @pytest.mark.asyncio
    async def test_group_view_has_m3_fields(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        req = self._make_request(env.app)
        tid = self._make_task(env.app)
        gid_goal = env.app.goals.create_agent(G1, title="盯着铝价", body="", criteria=[], by_text="管理员 发起")
        env.app.goals.create_member(
            G1, who_id="20002", who_name="阿柒", title="吃药", due_ts=None, remind_ts=clock.now() + 3600,
        )
        r = await env.client.get(f"/api/groups/{token}", headers={"X-MW-Group": token})
        assert r.status == 200
        view = await r.json()
        assert any(p["id"] == req["id"] for p in view["tasks"]["pending"])
        assert any(t["id"] == tid and "undelivered" in t for t in view["tasks"]["list"])
        assert any(g["id"] == gid_goal for g in view["goals"]["agent"])
        assert any(m["title"] == "吃药" for m in view["goals"]["member"])
        assert view["today"]["pending"] == 1
        # upcoming 里能看到成员提醒
        assert any("吃药" in u["text"] for u in view["upcoming"])

    @pytest.mark.asyncio
    async def test_settings_localenv_direct_warns(self, tmp_path: Path) -> None:
        raw = _raw_config(tmp_path / "d9")
        raw["environments"] = {"local_mode": "direct"}
        ctx = FakeCtx({})
        app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        server = TestServer(app.console.app)
        tc = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
        await tc.start_server()
        try:
            await tc.post("/api/login", json={"password": PASSWORD})
            s = await (await tc.get("/api/settings")).json()
            states = {h["key"]: h for h in s["health"]}
            assert states["localenv"]["state"] == "warn"
            assert states["localenv"]["text"] == "直跑模式没有隔离，只能本地测试用"
        finally:
            await tc.close()
            await app.stop()

    # ---------- 构想「想要这个」已删（docs/18 §五）：路由摘掉、不再落待批 ----------

    @pytest.mark.asyncio
    async def test_idea_want_route_removed(self, env: SimpleEnv) -> None:
        token = env.app.token_of(G1)
        r = await env.client.post("/api/ideas/1/want", json={}, headers={"X-MW-Group": token})
        assert r.status == 404, r.status
        assert env.app.approvals.pending_view(G1) == []


class TestGroupViewAgentFish:
    """群视图 agent_fish：管理员 / 群友都能看到各专岗小鱼种子；agents 缺位不炸视图。"""

    _FISH_KINDS = ("main", "news", "idea", "goal", "task")

    @pytest.mark.asyncio
    async def test_admin_view_has_agent_fish_with_seed(self, env: SimpleEnv) -> None:
        await env.login()
        # 通过 PUT API 给 news 岗位设一个小鱼种子
        r = await env.client.put("/api/agents/news", json={"fish_seed": "ab12cd34"})
        assert r.status == 200
        token = env.app.token_of(G1)
        r = await env.client.get(f"/api/groups/{token}")
        assert r.status == 200
        view = await r.json()
        assert "agent_fish" in view
        af = view["agent_fish"]
        assert isinstance(af, dict)
        assert sorted(af.keys()) == sorted(self._FISH_KINDS)
        assert af["news"] == "ab12cd34"
        assert af["idea"] == ""
        assert af["goal"] == ""
        assert af["task"] == ""

    @pytest.mark.asyncio
    async def test_member_view_has_agent_fish_with_same_seed(self, env: SimpleEnv) -> None:
        await env.login()
        r = await env.client.put("/api/agents/idea", json={"fish_seed": "fish-idea01"})
        assert r.status == 200
        token = env.app.token_of(G1)
        # 群友视角：用群链接码打开
        r = await env.client.get(f"/api/groups/{token}", headers={"X-MW-Group": token})
        assert r.status == 200
        view = await r.json()
        assert "agent_fish" in view
        af = view["agent_fish"]
        assert sorted(af.keys()) == sorted(self._FISH_KINDS)
        assert af["idea"] == "fish-idea01"
        assert af["news"] == ""
        # 只有种子，不是岗位详情
        assert "title" not in af
        assert "instructions" not in af

    @pytest.mark.asyncio
    async def test_agent_fish_all_empty_when_agents_missing(self, env: SimpleEnv) -> None:
        await env.login()
        env.app.agents = None
        token = env.app.token_of(G1)
        r = await env.client.get(f"/api/groups/{token}")
        assert r.status == 200
        view = await r.json()
        assert view["agent_fish"] == {"main": "", "news": "", "idea": "", "goal": "", "task": ""}

    @pytest.mark.asyncio
    async def test_agent_fish_all_empty_when_profiles_raises(self, env: SimpleEnv) -> None:
        await env.login()
        real = env.app.agents
        class BrokenAgents:
            def profiles(self):  # noqa: ANN001
                raise RuntimeError("boom")
        env.app.agents = BrokenAgents()
        token = env.app.token_of(G1)
        r = await env.client.get(f"/api/groups/{token}")
        assert r.status == 200
        view = await r.json()
        assert view["agent_fish"] == {"main": "", "news": "", "idea": "", "goal": "", "task": ""}
        env.app.agents = real


_PORT: list[int] = []


def _port() -> int:
    """整个测试文件只挑一次：同一测试里前后两份配置端口要一样，不然会被当成「改了网页地址」重启。"""
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]
