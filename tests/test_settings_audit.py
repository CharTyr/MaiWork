"""设置修改审计 + 一键撤销（settings_audit.py / console/backport_routes.py）测试。

- 数据层：record / list_changes / revert_change / audit_context / 密钥脱敏；
- 钩子：group_push / group_features / group_approval / group_rules / search_binding /
  专岗 profile / 常驻笔记写库时自动记账；
- HTTP：/api/settings/changes*（总管理员）+ /api/groups/{gid}/changes*（总管理员 /
  本群管理员，群管理员不能碰派活审批）；
- 撤销语义：404 / 403 / 400（密钥 / 已撤销 / 无撤销方式）/ 409 冲突（force 放行）/
  反锁死（撤销后主模型为空 → 拦）。
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import group_push, rules, settings_audit
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.console import backport_routes
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
from CharTyr_MaiWork.maiwork.group_approval import GroupApprovals
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"
G2 = "123456789"
PASSWORD = "测试密码-很显眼-12345"


class _Console:
    def __init__(self, password: str = ""):
        self.password = password


class _Settings:
    def __init__(self, served=(G1,), password: str = ""):
        self.served = set(served)
        self.console = _Console(password)
        self.model_list = ()
        self.endpoints = ()
        self.approval = None
        self.group_controls_seed_ready = True
        self.data_dir = ""

    def is_served(self, gid):
        return str(gid) in self.served


class _FakeSvc:
    def __init__(self, store: Store, settings: _Settings, agents) -> None:
        self.store = store
        self._settings = settings
        self.agents = agents
        self.group_approvals = GroupApprovals(store, get_settings=self.get_settings)

    def get_settings(self):
        return self._settings


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _Settings(served=(G1, G2), password=PASSWORD)
    agents = Agents(store, lambda: settings)
    svc = _FakeSvc(store, settings, agents)

    server = ConsoleServer(svc)
    backport_routes.register(server, server.app, svc)
    test_server = TestServer(server.app)
    client = TestClient(test_server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()

    class Env:
        pass

    e = Env()
    e.store = store
    e.settings = settings
    e.agents = agents
    e.svc = svc
    e.server = server
    e.client = client

    async def login(password: str = PASSWORD):
        return await client.post("/api/login", json={"password": password})

    e.login = login
    yield e
    await client.close()
    store.close()


@pytest.fixture
def dstore(tmp_path: Path):
    """纯数据层（不起 HTTP）。"""
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    yield store
    store.close()


# ----------------------------------------------------------------------
# 数据层：record / list_changes / redact
# ----------------------------------------------------------------------


class TestRecord:
    def test_record_and_list(self, dstore) -> None:
        cid = settings_audit.record(
            dstore, scope="global", target_key="search_binding",
            summary="修改联网搜索绑定",
            before={"mcp": "a"}, after={"mcp": "b"},
        )
        assert cid
        rows, has_more = settings_audit.list_changes(dstore)
        assert not has_more
        assert [r["id"] for r in rows] == [cid]
        r = rows[0]
        assert r["actor"] == "admin_web"
        assert r["target_key"] == "search_binding"
        assert r["before_json"] == {"mcp": "a"}
        assert r["after_json"] == {"mcp": "b"}
        assert r["revertible"] is True

    def test_noop_change_not_recorded(self, dstore) -> None:
        assert settings_audit.record(
            dstore, scope="global", target_key="x",
            summary="没改", before={"a": 1}, after={"a": 1},
        ) is None
        rows, _ = settings_audit.list_changes(dstore)
        assert rows == []

    def test_secret_payload_redacted_and_not_revertible(self, dstore) -> None:
        cid = settings_audit.record(
            dstore, scope="global", target_key="endpoint:ep1",
            summary="修改端点",
            before={"api_key": "SECRET-OLD", "name": "a"},
            after={"api_key": "SECRET-NEW", "name": "b"},
        )
        assert cid
        rows, _ = settings_audit.list_changes(dstore)
        assert rows[0]["revertible"] is False
        blob = str(rows[0])
        assert "SECRET-OLD" not in blob and "SECRET-NEW" not in blob
        assert rows[0]["before_json"]["api_key"] == settings_audit.REDACTED
        assert rows[0]["after_json"]["api_key"] == settings_audit.REDACTED

    def test_secret_fields_still_recorded_even_if_look_same(self, dstore) -> None:
        """密钥轮换：脱敏后前后都是 [REDACTED]，但必须落一条（revertible=0 不跳过）。"""
        cid = settings_audit.record(
            dstore, scope="global", target_key="endpoint:ep2",
            summary="换密钥",
            before={"api_key": "AAA"}, after={"api_key": "BBB"},
            revertible=False,
        )
        assert cid
        rows, _ = settings_audit.list_changes(dstore)
        assert rows[0]["revertible"] is False

    def test_audit_context_tags_actor(self, dstore) -> None:
        with settings_audit.audit_context("admin_chat", actor_detail="mcp_manage", reason="action=toggle"):
            cid = settings_audit.record(
                dstore, scope="global", target_key="mcp:x",
                summary="改 MCP", before={}, after={"enabled": True},
            )
        rows, _ = settings_audit.list_changes(dstore)
        assert rows[0]["id"] == cid
        assert rows[0]["actor"] == "admin_chat"
        assert rows[0]["actor_detail"] == "mcp_manage"
        assert rows[0]["reason"] == "action=toggle"

    def test_filters_and_pagination(self, dstore) -> None:
        for i in range(5):
            settings_audit.record(
                dstore, scope="group:%s" % G1 if i % 2 else "global",
                target_key=f"t{i}", summary=f"改{i}",
                before={}, after={"i": i},
                actor="group_admin" if i == 3 else "admin_web",
            )
        rows, _ = settings_audit.list_changes(dstore, actor="group_admin")
        assert len(rows) == 1 and rows[0]["target_key"] == "t3"
        rows, _ = settings_audit.list_changes(dstore, scope=f"group:{G1}")
        assert len(rows) == 2
        rows, has_more = settings_audit.list_changes(dstore, limit=2)
        assert has_more and len(rows) == 2
        rows2, _ = settings_audit.list_changes(dstore, before_id=rows[-1]["id"])
        assert all(r["id"] < rows[-1]["id"] for r in rows2)

    def test_record_survives_no_store(self) -> None:
        assert settings_audit.record(None, target_key="x", summary="s", before={}, after={"a": 1}) is None


# ----------------------------------------------------------------------
# 钩子（数据层直接调）
# ----------------------------------------------------------------------


class TestHooks:
    def test_group_push_hook(self, dstore) -> None:
        group_push.set_config(dstore, G1, {"daily_max": 8})
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 1
        r = rows[0]
        assert r["scope"] == f"group:{G1}" and r["target_key"] == "group_push"
        assert r["after_json"]["daily_max"] == 8

    def test_group_features_hook(self, dstore) -> None:
        settings = _Settings()
        from CharTyr_MaiWork.maiwork import group_features

        group_features.set_config(dstore, G1, {"news": False}, settings)
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 1 and rows[0]["target_key"] == "group_features"
        assert rows[0]["before_json"] == {"news": True}
        assert rows[0]["after_json"] == {"news": False}

    def test_group_approval_hook(self, dstore) -> None:
        ga = GroupApprovals(dstore)  # legacy 纯数据层：无 getter 放行
        ga.set(G1, {"approvers": ["qq:1"], "exempt_users": [], "exempt_group": False, "required": True})
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 1 and rows[0]["target_key"] == "group_approval"
        assert rows[0]["after_json"]["approvers"] == ["qq:1"]

    def test_group_rules_hook(self, dstore) -> None:
        settings = _Settings()
        ag = Agents(dstore, lambda: settings)
        ag.group_rules_set(G1, "别发链接", updated_by="admin_web")
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 1 and rows[0]["target_key"] == "group_rules"
        assert rows[0]["before_json"] == {"body": ""}
        assert rows[0]["after_json"] == {"body": "别发链接"}

    def test_search_binding_hooks(self, dstore) -> None:
        from CharTyr_MaiWork.maiwork import search_binding

        search_binding.set_binding(dstore, {"mcp": "exa", "tool": "web_search"})
        search_binding.clear_binding(dstore)
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 2
        assert all(r["target_key"] == "search_binding" for r in rows)

    def test_agent_profile_hook(self, dstore) -> None:
        settings = _Settings()
        ag = Agents(dstore, lambda: settings)
        ag.update_profile("news", {"title": "新名字"})
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 1 and rows[0]["target_key"] == "agent_profile:news"
        assert rows[0]["after_json"]["title"] == "新名字"

    def test_index_note_manual_write_hook(self, dstore) -> None:
        settings = _Settings()
        ag = Agents(dstore, lambda: settings)
        ag.set_index_note(G1, "news", content="索引：科技媒体", source="admin_web")
        rows, _ = settings_audit.list_changes(dstore)
        assert len(rows) == 1 and rows[0]["target_key"] == "agent_index_note:news"

    def test_index_note_specialist_write_not_audited(self, dstore) -> None:
        settings = _Settings()
        ag = Agents(dstore, lambda: settings)
        ag.set_index_note(G1, "news", content="自写索引", source="specialist")
        rows, _ = settings_audit.list_changes(dstore)
        assert rows == []

    def test_rules_patch_hook_and_redaction(self, dstore, tmp_path, monkeypatch) -> None:
        """config.toml 直写路径：普通字段可撤销记账；密钥字段脱敏 + 不可撤销。"""
        from CharTyr_MaiWork.maiwork import config_file

        monkeypatch.setattr(config_file, "write_fields", lambda *a, **k: None)
        monkeypatch.setattr(config_file, "delete_fields", lambda *a, **k: None)
        base = _Settings(password=PASSWORD)
        changed = rules.save_config_patch(
            dstore,
            {"focus.max_members": 9, "console.password": "NEW-SECRET",
             "current_password": PASSWORD},
            base=base,
        )
        assert set(changed) == {"focus.max_members", "console.password"}
        rows, _ = settings_audit.list_changes(dstore)
        by_key = {r["target_key"]: r for r in rows}
        assert by_key["config:focus.max_members"]["revertible"] is True
        assert by_key["config:focus.max_members"]["after_json"] == {"focus.max_members": 9}
        secret_row = by_key["config:console.password"]
        assert secret_row["revertible"] is False
        assert "NEW-SECRET" not in str(secret_row)
        assert secret_row["after_json"] == {"console.password": settings_audit.REDACTED}
        assert secret_row["before_json"] == {"console.password": settings_audit.REDACTED}


# ----------------------------------------------------------------------
# revert_change（数据层）
# ----------------------------------------------------------------------


class TestRevert:
    def _svc(self, dstore, settings) -> _FakeSvc:
        ag = Agents(dstore, lambda: settings)
        return _FakeSvc(dstore, settings, ag)

    def test_revert_group_push(self, dstore) -> None:
        settings = _Settings()
        svc = self._svc(dstore, settings)
        group_push.set_config(dstore, G1, {"daily_max": 8}, settings)
        cid = settings_audit.list_changes(dstore)[0][0]["id"]
        group_push.set_config(dstore, G1, {"daily_max": 5}, settings)
        # 这条之后又改过 → 冲突；force=True 放行
        with pytest.raises(settings_audit.RevertConflictError):
            settings_audit.revert_change(dstore, svc, cid, actor="admin_web")
        out = settings_audit.revert_change(dstore, svc, cid, actor="admin_web", force=True)
        assert out["ok"] is True
        cfg = group_push.get_config(dstore, G1, settings)
        assert cfg["daily_max"] == 3  # 回到改之前的默认
        row = settings_audit.get_change(dstore, cid)
        assert row["reverted_at"] is not None and row["reverted_by_id"]
        rows, _ = settings_audit.list_changes(dstore)
        assert any(r["actor"] == "revert" for r in rows)

    def test_revert_search_binding(self, dstore) -> None:
        from CharTyr_MaiWork.maiwork import search_binding

        settings = _Settings()
        svc = self._svc(dstore, settings)
        search_binding.set_binding(dstore, {"mcp": "exa", "tool": "web_search"})
        search_binding.clear_binding(dstore)
        rows, _ = settings_audit.list_changes(dstore)
        # 撤「解绑」那条：恢复原绑定（live 已是 {} 与 after 一致，不冲突）
        cid = rows[0]["id"]
        assert rows[0]["after_json"] == {}
        out = settings_audit.revert_change(dstore, svc, cid, actor="admin_web")
        assert out["ok"] is True
        b = search_binding.get_binding(dstore)
        assert b is not None and b["mcp"] == "exa"

    def test_revert_index_note(self, dstore) -> None:
        settings = _Settings()
        svc = self._svc(dstore, settings)
        ag = svc.agents
        ag.set_index_note(G1, "news", content="第一版", source="admin_web")
        cid = settings_audit.list_changes(dstore)[0][0]["id"]
        out = settings_audit.revert_change(dstore, svc, cid, actor="admin_web", force=True)
        assert out["ok"] is True
        assert ag.get_index_note(G1, "news")["content"] == ""
        row = settings_audit.get_change(dstore, cid)
        assert row["reverted_at"] is not None

    def test_revert_missing_404(self, dstore) -> None:
        svc = self._svc(dstore, _Settings())
        with pytest.raises(KeyError):
            settings_audit.revert_change(dstore, svc, 999, actor="admin_web")

    def test_revert_non_revertible_400(self, dstore) -> None:
        svc = self._svc(dstore, _Settings())
        cid = settings_audit.record(
            dstore, scope="global", target_key="endpoint:e1",
            summary="端点", before={}, after={"a": 1}, revertible=False,
        )
        with pytest.raises(ValueError, match="不支持撤销"):
            settings_audit.revert_change(dstore, svc, cid, actor="admin_web")

    def test_revert_already_reverted_400(self, dstore) -> None:
        settings = _Settings()
        svc = self._svc(dstore, settings)
        group_push.set_config(dstore, G1, {"daily_max": 8}, settings)
        cid = settings_audit.list_changes(dstore)[0][0]["id"]
        settings_audit.revert_change(dstore, svc, cid, actor="admin_web")
        with pytest.raises(ValueError, match="已撤销"):
            settings_audit.revert_change(dstore, svc, cid, actor="admin_web")

    def test_revert_scope_gate_and_group_admin_gate(self, dstore) -> None:
        settings = _Settings()
        svc = self._svc(dstore, settings)
        settings_audit.record(
            dstore, scope="global", target_key="search_binding",
            summary="绑定", before={}, after={"mcp": "x", "tool": "t"},
        )
        cid = settings_audit.list_changes(dstore)[0][0]["id"]
        # 群管理员视角（allow_scope=group:G1）：全局记录不让碰
        with pytest.raises(PermissionError):
            settings_audit.revert_change(
                dstore, svc, cid, actor="group_admin",
                allow_scope=f"group:{G1}", is_group_admin_only=True,
            )
        ga = GroupApprovals(dstore)
        ga.set(G1, {"approvers": [], "exempt_users": [], "exempt_group": True, "required": True})
        cid2 = settings_audit.list_changes(dstore)[0][0]["id"]
        with pytest.raises(PermissionError, match="仅总管理员"):
            settings_audit.revert_change(
                dstore, svc, cid2, actor="group_admin",
                allow_scope=f"group:{G1}", is_group_admin_only=True,
            )

    def test_revert_no_writer_400(self, dstore) -> None:
        svc = self._svc(dstore, _Settings())
        cid = settings_audit.record(
            dstore, scope="global", target_key="endpoint:e9",
            summary="端点", before={}, after={"enabled": True}, revertible=True,
        )
        with pytest.raises(ValueError):
            settings_audit.revert_change(dstore, svc, cid, actor="admin_web")


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


class TestHttp:
    @pytest.mark.asyncio
    async def test_global_list_requires_admin(self, env) -> None:
        r = await env.client.get("/api/settings/changes")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_global_list_after_login(self, env) -> None:
        await env.login()
        group_push.set_config(env.store, G1, {"daily_max": 9})
        r = await env.client.get("/api/settings/changes")
        assert r.status == 200
        data = await r.json()
        assert data["changes"][0]["target_key"] == "group_push"

    @pytest.mark.asyncio
    async def test_group_list_and_revert_via_http(self, env) -> None:
        await env.login()
        group_push.set_config(env.store, G1, {"daily_max": 7})
        r = await env.client.get(f"/api/groups/{G1}/changes")
        assert r.status == 200
        data = await r.json()
        cid = data["changes"][0]["id"]
        assert data["changes"][0]["revertible"] is True

        r = await env.client.post(
            f"/api/groups/{G1}/changes/{cid}/revert", json={"force": True},
        )
        assert r.status == 200, await r.text()
        assert group_push.get_config(env.store, G1)["daily_max"] == 3  # 回默认

        r = await env.client.post(
            f"/api/groups/{G1}/changes/{cid}/revert", json={},
        )
        assert r.status == 400  # 已撤销

    @pytest.mark.asyncio
    async def test_revert_404_and_conflict(self, env) -> None:
        await env.login()
        r = await env.client.post(f"/api/groups/{G1}/changes/999/revert", json={})
        assert r.status == 404
        group_push.set_config(env.store, G1, {"daily_max": 7})
        data = await (await env.client.get(f"/api/groups/{G1}/changes")).json()
        cid = data["changes"][0]["id"]
        group_push.set_config(env.store, G1, {"daily_max": 3})
        r = await env.client.post(f"/api/groups/{G1}/changes/{cid}/revert", json={})
        assert r.status == 409
        body = await r.json()
        assert body["conflict"] is True
        r = await env.client.post(
            f"/api/groups/{G1}/changes/{cid}/revert", json={"force": True},
        )
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_anon_cannot_see_group_changes(self, env) -> None:
        r = await env.client.get(f"/api/groups/{G1}/changes")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_group_routes_are_not_swallowed_by_catchall(self, env) -> None:
        """注册在 /api/{tail:.*} 兜底之后也要可达（register 会重排）。"""
        await env.login()
        r = await env.client.get(f"/api/groups/{G1}/agent-notes")
        assert r.status == 200
