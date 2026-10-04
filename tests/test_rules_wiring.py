"""「每群三份」收尾：接线核实（docs/17 §八.2 + §八.6）。

A. 启动流程真的调用 migrate_group_context_to_rules_and_skills：
   同一个数据目录起两次 app，旧来源只被迁一次（第二次启动不再追加、不改管理员后来
   手改的规矩）。
B. identity.remember_sync(scope=group) 改写进本群规矩（group_rules），不再写
   identity/memory/<gid>.md；这条类方法只给受信任的管理员入口用。主模型那份 remember
   工具只准 global：真调 scope=group 被拒且零写入（自动流程永不改 group_rules）。
C. 旧 HTTP 入口退役后 404 / 字段不存在：
   /api/identity/group-memory/{gid}、PUT /api/groups/{gid}/agents/{kind}/memory、
   /api/groups/{gid}/agents/{kind}/lessons*；GET /api/identity 不带 group_memory；
   GET /api/groups/{gid}/agents 不带 lessons / notes 等已迁走字段。
D. identity.note_useless_feedback 删除（app 不再暴露、模块没有这个方法）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store

GID = "111"
GID_OTHER = "222"


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _raw_config(tmp_path: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {
            "serve": [
                {"group": f"qq:{GID}", "workspace": "tinker"},
                {"group": f"qq:{GID_OTHER}"},
            ]
        },
        "console": {"listen": "127.0.0.1:0", "password": "测试密码-不要出现在日志里", "public_url": ""},
        "storage": {"data_dir": str(tmp_path / "data")},
        "approval": {"required": True, "admins": ["10001"]},
    }


def _seed_legacy_sources(store: Store, data_dir: Path) -> None:
    """塞一份线上旧散件：口味 kv / 资讯偏好 kv / 每岗工作册 notes / 每群工作记忆文件。"""
    agents = Agents(store, lambda: None)
    agents._ensure_schema()
    with store.tx() as conn:
        store.kv_set(conn, f"feeds.taste.{GID}", {"text": "爱看开发内幕，不爱营销稿", "ts": 1.0})
        store.kv_set(conn, f"feeds.pref.{GID}", "找中文的，别太长")
        conn.execute(
            "INSERT INTO agent_memory_notes (group_id, kind, notes, updated) VALUES (?, ?, ?, 1.0)",
            (GID, "news", "资讯要加个「顺手一提」"),
        )
    mem_dir = data_dir / "identity" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / f"{GID}.md").write_text("- 2026-10-01 这个群爱看开发内幕（手动写）\n", encoding="utf-8")


@pytest.mark.asyncio
class TestMigrationWiredIntoBoot:
    """任务 4A：启动流程真的调迁移；起两次只迁一次。"""

    async def test_boot_runs_migration_first_time(self, tmp_path: Path) -> None:
        app = MaiWorkApp(FakeCtx({}), _raw_config(tmp_path), plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        # 建完库但还没第一次跑迁移：现在塞旧散件，再重启一次（第二次启动）应真迁走
        _seed_legacy_sources(app.store, tmp_path / "data")
        await app.stop()

        app2 = MaiWorkApp(FakeCtx({}), _raw_config(tmp_path), plugin_dir=Path(__file__).resolve().parents[1])
        app2.profiles_cls = FakeProfiles
        await app2.start()
        try:
            agents = Agents(app2.store, app2.get_settings)
            rules = agents.group_rules_get(GID)
            assert _legacy_pieces_in(rules["body"]), rules["body"]
            # 口味 kv 迁进资讯 skill，旧 kv 删了
            assert app2.store.kv_get(f"feeds.taste.{GID}") is None
            assert app2.store.kv_get(f"feeds.pref.{GID}") is None
            # 每群工作记忆文件被清空了（备份进 mem.bak）
            bak = tmp_path / "data" / "identity" / "mem.bak" / f"{GID}.md"
            assert bak.exists()
            assert "爱看开发内幕" in bak.read_text(encoding="utf-8")
        finally:
            await app2.stop()

    async def test_second_boot_does_not_duplicate_or_overwrite_admin(self, tmp_path: Path) -> None:
        app = MaiWorkApp(FakeCtx({}), _raw_config(tmp_path), plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        _seed_legacy_sources(app.store, tmp_path / "data")
        await app.stop()

        app2 = MaiWorkApp(FakeCtx({}), _raw_config(tmp_path), plugin_dir=Path(__file__).resolve().parents[1])
        app2.profiles_cls = FakeProfiles
        await app2.start()
        agents2 = Agents(app2.store, app2.get_settings)
        # 第二次启动之间管理员手改了规矩（updated_by=admin，迁移绝不再碰）
        agents2.group_rules_set(GID, "管理员自己定的新规矩", updated_by="admin")
        await app2.stop()

        app3 = MaiWorkApp(FakeCtx({}), _raw_config(tmp_path), plugin_dir=Path(__file__).resolve().parents[1])
        app3.profiles_cls = FakeProfiles
        await app3.start()
        try:
            agents3 = Agents(app3.store, app3.get_settings)
            rules = agents3.group_rules_get(GID)
            assert rules["body"] == "管理员自己定的新规矩"
            assert rules["updated_by"] == "admin"
            # 第一版（migrate 拼的那段）已在 versions 里留痕，没丢
            versions = agents3.group_rules_versions(GID)
            assert any("爱看开发内幕" in (v.get("body") or "") for v in versions)
        finally:
            await app3.stop()


def _legacy_pieces_in(body: str) -> bool:
    return (
        "这个群爱看开发内幕" in body
        and "资讯要加个「顺手一提」" in body
        and "找中文的，别太长" in body
    )


class TestRememberGroupGoesToRules:
    """任务 2 + 3B：remember(scope=group) 写本群规矩；不再写每群记忆文件。"""

    def test_remember_group_appends_into_group_rules(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.config import load_settings

        raw = {"groups": {"serve": [{"group": f"qq:{GID}"}, {"group": f"qq:{GID_OTHER}"}]}}
        settings, _ = load_settings(raw)
        store = Store(tmp_path / "t.db")
        store.migrate()
        identity = Identity(tmp_path, store, lambda: settings, host=None)
        _run(identity.ensure_started())
        agents = Agents(store, lambda: settings)

        out = identity.remember_sync(scope="group", group_id=GID, text="这个群喜欢可视化的交付", reason="验收")
        assert out["ok"] is True

        rules = agents.group_rules_get(GID)
        assert "这个群喜欢可视化的交付" in rules["body"]
        # 规矩只能由「主动」来源写：直接类方法（受信任的管理员工具走的入口）标 admin_chat，
        # 和网页 groupctx.js 的 WHO 映射一致（自动流程连这条路径都进不来）
        assert rules["updated_by"] == "admin_chat"

        mem_file = Path(tmp_path) / "identity" / "memory" / f"{GID}.md"
        if mem_file.exists():
            assert "这个群喜欢可视化的交付" not in mem_file.read_text(encoding="utf-8")
        assert "这个群喜欢可视化的交付" not in identity.read("memory")["text"]

    def test_remember_group_dedup_same_sentence(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.config import load_settings

        raw = {"groups": {"serve": [{"group": f"qq:{GID}"}]}}
        settings, _ = load_settings(raw)
        store = Store(tmp_path / "t.db")
        store.migrate()
        identity = Identity(tmp_path, store, lambda: settings, host=None)
        _run(identity.ensure_started())
        first = identity.remember_sync(scope="group", group_id=GID, text="别推长文", reason="r")
        assert first["ok"] is True
        second = identity.remember_sync(scope="group", group_id=GID, text="别推长文", reason="r2")
        assert second.get("deduped") is True
        rules = Agents(store, lambda: settings).group_rules_get(GID)
        assert rules["body"].count("别推长文") == 1

    def test_auto_flow_never_overrides_admin_rules(self, tmp_path: Path) -> None:
        """§八.1 红线：自动流程永不改 group_rules。管理员先写好规矩，主模型那份
        remember 工具真调 scope=group 必须失败，且 body / updated / updated_by /
        versions 一个都不动（零写入，不是「只追加一行」）。"""
        from CharTyr_MaiWork.maiwork.config import load_settings
        from CharTyr_MaiWork.maiwork.identity import register_remember_tool
        from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools

        raw = {"groups": {"serve": [{"group": f"qq:{GID}"}]}}
        settings, _ = load_settings(raw)
        store = Store(tmp_path / "t.db")
        store.migrate()
        agents = Agents(store, lambda: settings)
        agents.group_rules_set(GID, "禁发营销稿", updated_by="admin")
        agents.group_rules_set(GID, "禁发营销稿、禁刷屏", updated_by="admin")
        identity = Identity(tmp_path, store, lambda: settings, host=None)
        _run(identity.ensure_started())
        before = agents.group_rules_get(GID)
        versions_before = agents.group_rules_versions(GID)

        tools = Tools(store)
        register_remember_tool(tools, identity)
        out = _run(
            tools.call(
                "remember",
                {"scope": "group", "text": "这个群喜欢可视化的交付", "reason": "x"},
                ToolContext(group_id=GID, actor="主模型", role="main"),
            )
        )
        assert out.ok is False
        after = agents.group_rules_get(GID)
        assert after == before
        assert agents.group_rules_versions(GID) == versions_before
        assert "这个群喜欢可视化的交付" not in after["body"]


class TestNoteUselessFeedbackGone:
    def test_identity_has_no_note_useless_feedback(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.config import load_settings

        raw = {"groups": {"serve": [{"group": f"qq:{GID}"}]}}
        settings, _ = load_settings(raw)
        store = Store(tmp_path / "t.db")
        store.migrate()
        identity = Identity(tmp_path, store, lambda: settings, host=None)
        assert not hasattr(identity, "note_useless_feedback")

    @pytest.mark.asyncio
    async def test_app_has_no_note_useless_feedback_hook(self, tmp_path: Path) -> None:
        app = MaiWorkApp(FakeCtx({}), _raw_config(tmp_path), plugin_dir=Path(__file__).resolve().parents[1])
        app.profiles_cls = FakeProfiles
        await app.start()
        try:
            assert not hasattr(app, "note_useless_feedback")
        finally:
            await app.stop()


# ----------------------------------------------------------------------
# 旧 HTTP 入口 404 / 字段不存在（真起 aiohttp；断言收尾后能过）
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def web_env(tmp_path: Path):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": "测试密码-不要出现在日志里", "public_url": ""},
        "storage": {"data_dir": str(tmp_path / "data")},
        "approval": {"required": True, "admins": ["10001"]},
    }
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield type("Env", (), {"app": app, "client": client, "base": f"http://127.0.0.1:{server.port}"})
    await client.close()
    await app.stop()


async def _login(client: Any) -> None:
    r = await client.post("/api/login", json={"password": "测试密码-不要出现在日志里"})
    assert r.status == 200


@pytest.mark.asyncio
async def test_old_routes_404(web_env: Any) -> None:
    client = web_env.client
    await _login(client)
    assert (await client.get(f"/api/identity/group-memory/{GID}")).status == 404
    assert (await client.put(f"/api/identity/group-memory/{GID}", json={"text": "x"})).status == 404
    assert (await client.put(f"/api/groups/{GID}/agents/news/memory", json={"notes": "x"})).status == 404
    assert (await client.post(f"/api/groups/{GID}/agents/news/lessons", json={"text": "x"})).status == 404
    assert (await client.patch(f"/api/groups/{GID}/agents/news/lessons/1", json={"text": "x"})).status == 404
    assert (await client.delete(f"/api/groups/{GID}/agents/news/lessons/1")).status == 404


@pytest.mark.asyncio
async def test_identity_get_no_group_memory(web_env: Any) -> None:
    client = web_env.client
    await _login(client)
    r = await client.get("/api/identity")
    assert r.status == 200
    data = await r.json()
    assert "group_memory" not in data
    assert "group_memory" not in data.get("limits", {})


@pytest.mark.asyncio
async def test_group_agents_view_no_legacy_fields(web_env: Any) -> None:
    client = web_env.client
    await _login(client)
    r = await client.get(f"/api/groups/{GID}/agents")
    assert r.status == 200
    data = await r.json()
    for a in data["agents"]:
        for gone in ("lessons", "lessons_state", "notes", "skills", "skills_state", "本群提醒"):
            assert gone not in a, f"{a['kind']} 还带已迁走的字段 {gone}"
