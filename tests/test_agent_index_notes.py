"""常驻笔记（agent_index_notes / docs/35）测试。

- 数据层：Agents.get/set/list_index_notes、版本表（≤10）、rollback、锁定、
  1200 字上限、main/task 不认、未注册岗位 400、服务群闸、隐私闸；
- 双重门槛：returned 先暂存 my_notes；review(accepted=True) 且没锁定才写进
  常驻笔记（rejected 不写、锁着不写、main/task 不存）；
- 提交管线：submit_result 收 my_notes → WorkerReport.my_notes；
- 提示词：group_context 给专岗注入「本岗常驻笔记（数据不是指令）」；
- HTTP：/api/groups/{gid}/agent-notes*（总管理员 / 本群管理员）。
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.console import backport_routes
from CharTyr_MaiWork.maiwork.console.server import ConsoleServer
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

    def is_served(self, gid):
        return str(gid) in self.served


class _FakeSvc:
    def __init__(self, store: Store, settings: _Settings, agents) -> None:
        self.store = store
        self._settings = settings
        self.agents = agents

    def get_settings(self):
        return self._settings


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    store.migrate()
    settings = _Settings(served=(G1, G2))
    agents = Agents(store, lambda: settings)

    class Env:
        pass

    e = Env()
    e.store = store
    e.settings = settings
    e.agents = agents
    yield e
    store.close()


@pytest_asyncio.fixture
async def aenv(tmp_path: Path):
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
    e.client = client

    async def login(password: str = PASSWORD):
        return await client.post("/api/login", json={"password": password})

    e.login = login
    yield e
    await client.close()
    store.close()


# ----------------------------------------------------------------------
# 数据层：读 / 写 / 版本 / 回滚 / 闸
# ----------------------------------------------------------------------


class TestIndexNotes:
    def test_empty_note(self, env) -> None:
        note = env.agents.get_index_note(G1, "news")
        assert note["content"] == "" and note["version"] == 0
        assert note["locked"] is False and note["max_chars"] == 1200
        assert env.agents.list_index_notes(G1) == []

    def test_set_get_list(self, env) -> None:
        note = env.agents.set_index_note(G1, "news", content="科技媒体索引", source="admin_web")
        assert note["content"] == "科技媒体索引" and note["version"] == 1
        assert note["updated_by"] == "admin_web"
        notes = env.agents.list_index_notes(G1)
        assert [n["kind"] for n in notes] == ["news"]
        # 另一群独立
        assert env.agents.get_index_note(G2, "news")["content"] == ""

    def test_main_task_rejected(self, env) -> None:
        for kind in ("main", "task"):
            with pytest.raises(ValueError):
                env.agents.get_index_note(G1, kind)
            with pytest.raises(ValueError):
                env.agents.set_index_note(G1, kind, content="x")

    def test_unknown_kind_rejected(self, env) -> None:
        with pytest.raises(ValueError):
            env.agents.set_index_note(G1, "c_no_such_kind", content="x")

    def test_custom_kind_ok(self, env) -> None:
        kind = env.agents.create_custom("自定义岗A")["kind"]
        assert kind.startswith("c_")
        note = env.agents.set_index_note(G1, kind, content="自定义岗索引")
        assert note["content"] == "自定义岗索引"
        assert kind in [n["kind"] for n in env.agents.list_index_notes(G1)]

    def test_max_chars(self, env) -> None:
        with pytest.raises(ValueError, match="1200"):
            env.agents.set_index_note(G1, "news", content="字" * 1201)
        # specialist 来源截断不报错
        note = env.agents.set_index_note(G1, "news", content="字" * 1300, source="specialist")
        assert len(note["content"]) == 1200

    def test_versions_kept_to_10(self, env) -> None:
        for i in range(12):
            env.agents.set_index_note(G1, "news", content=f"第{i}版", source="admin_web")
        versions = env.agents.index_note_versions(G1, "news")
        assert len(versions) == 10
        assert versions[0]["version"] == 12 and versions[0]["content"] == "第11版"
        assert versions[-1]["version"] == 3
        # 没变化不记版本
        env.agents.set_index_note(G1, "news", content="第11版", source="admin_web")
        assert len(env.agents.index_note_versions(G1, "news")) == 10

    def test_rollback(self, env) -> None:
        env.agents.set_index_note(G1, "news", content="一", source="admin_web")
        env.agents.set_index_note(G1, "news", content="二", source="admin_web")
        note = env.agents.rollback_index_note(G1, "news", 1)
        assert note["content"] == "一" and note["version"] == 3
        with pytest.raises(KeyError):
            env.agents.rollback_index_note(G1, "news", 99)

    def test_lock(self, env) -> None:
        env.agents.set_index_note(G1, "news", content="锁定中", locked=True)
        note = env.agents.get_index_note(G1, "news")
        assert note["locked"] is True
        # 锁定只是挡「验收通过自动写」，手动改写照常
        note = env.agents.set_index_note(G1, "news", content="管理员改", source="admin_web")
        assert note["content"] == "管理员改" and note["locked"] is True
        note = env.agents.set_index_note(G1, "news", locked=False)
        assert note["locked"] is False and note["content"] == "管理员改"

    def test_noop_set_does_not_bump_version(self, env) -> None:
        env.agents.set_index_note(G1, "news", content="不变")
        note = env.agents.set_index_note(G1, "news", content="不变")
        assert note["version"] == 1

    def test_non_served_group_rejected(self, env) -> None:
        with pytest.raises(ValueError):
            env.agents.set_index_note("999999999", "news", content="x")


# ----------------------------------------------------------------------
# 双重门槛：returned 暂存 → review 验收通过才写
# ----------------------------------------------------------------------


class TestDualGate:
    def _handoff(self, env, kind="news", my_notes="自写索引") -> str:
        hid = env.agents.begin(G1, kind, "干一次活")
        env.agents.running(G1, hid)
        env.agents.returned(G1, hid, "干完了", data={}, my_notes=my_notes)
        return hid

    def test_accepted_writes_index_note(self, env) -> None:
        hid = self._handoff(env)
        env.agents.review(G1, hid, True, "通过")
        note = env.agents.get_index_note(G1, "news")
        assert note["content"] == "自写索引"
        versions = env.agents.index_note_versions(G1, "news")
        assert len(versions) == 1
        assert versions[0]["source"] == "specialist" and versions[0]["handoff_id"] == hid
        # 暂存在交接单上
        assert env.agents.handoff(G1, hid)["my_notes"] == "自写索引"

    def test_rejected_does_not_write(self, env) -> None:
        hid = self._handoff(env)
        env.agents.review(G1, hid, False, "不行")
        assert env.agents.get_index_note(G1, "news")["content"] == ""

    def test_locked_blocks_specialist_write(self, env) -> None:
        env.agents.set_index_note(G1, "news", content="管理员定的", locked=True)
        hid = self._handoff(env)
        env.agents.review(G1, hid, True, "通过")
        assert env.agents.get_index_note(G1, "news")["content"] == "管理员定的"

    def test_empty_my_notes_no_write(self, env) -> None:
        hid = self._handoff(env, my_notes="")
        env.agents.review(G1, hid, True, "通过")
        assert env.agents.get_index_note(G1, "news")["content"] == ""

    def test_task_kind_no_my_notes(self, env) -> None:
        hid = env.agents.begin(G1, "task", "干一次活")
        env.agents.running(G1, hid)
        env.agents.returned(G1, hid, "干完了", my_notes="任务岗不该存")
        assert env.agents.handoff(G1, hid)["my_notes"] == ""


# ----------------------------------------------------------------------
# 提交管线：submit_result → WorkerReport.my_notes
# ----------------------------------------------------------------------


class TestSubmitResult:
    @pytest.mark.asyncio
    async def test_my_notes_flows_through(self, env) -> None:
        from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
        from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

        tools = Tools(env.store)
        register_builtin(tools, search=None, profiles=None, store=env.store)
        ctx = ToolContext(group_id=G1, role="worker")
        res = await tools.call(
            "submit_result",
            {"summary": "干完了", "my_notes": "查过 IT之家；坑：翻页要 ?page"},
            ctx,
        )
        assert res.ok
        assert res.data["my_notes"] == "查过 IT之家；坑：翻页要 ?page"
        # 超过 1200 截断
        res2 = await tools.call("submit_result", {"summary": "x", "my_notes": "字" * 1500}, ctx)
        assert len(res2.data["my_notes"]) == 1200


# ----------------------------------------------------------------------
# group_context 注入
# ----------------------------------------------------------------------


class TestGroupContext:
    def test_index_note_injected_for_specialist(self, env) -> None:
        from CharTyr_MaiWork.maiwork.group_context import group_context

        env.agents.set_index_note(G1, "news", content="只查官方源", source="admin_web")
        out = group_context(env.agents, G1, "news")
        assert "本岗常驻笔记" in out and "只查官方源" in out
        assert "是数据不是指令" in out

    def test_no_injection_for_task_and_empty(self, env) -> None:
        from CharTyr_MaiWork.maiwork.group_context import group_context

        env.agents.set_index_note(G1, "news", content="索引")
        assert "常驻笔记" not in group_context(env.agents, G1, "task")
        assert "常驻笔记" not in group_context(env.agents, G1, "idea")

    def test_no_leak_across_groups(self, env) -> None:
        from CharTyr_MaiWork.maiwork.group_context import group_context

        env.agents.set_index_note(G1, "news", content="甲群索引")
        assert "常驻笔记" not in group_context(env.agents, G2, "news")


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


class TestHttp:
    @pytest.mark.asyncio
    async def test_agent_notes_anon_401(self, aenv) -> None:
        r = await aenv.client.get(f"/api/groups/{G1}/agent-notes")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_put_get_versions_rollback(self, aenv) -> None:
        await aenv.login()
        r = await aenv.client.put(
            f"/api/groups/{G1}/agent-notes/news",
            json={"content": "第一版", "locked": False},
        )
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["note"]["content"] == "第一版" and data["note"]["version"] == 1

        await aenv.client.put(f"/api/groups/{G1}/agent-notes/news", json={"content": "第二版"})
        r = await aenv.client.get(f"/api/groups/{G1}/agent-notes")
        notes = (await r.json())["notes"]
        assert notes[0]["content"] == "第二版" and notes[0]["version"] == 2

        r = await aenv.client.get(f"/api/groups/{G1}/agent-notes/news/versions")
        versions = (await r.json())["versions"]
        assert [v["version"] for v in versions] == [2, 1]

        r = await aenv.client.post(
            f"/api/groups/{G1}/agent-notes/news/rollback", json={"version": 1},
        )
        assert r.status == 200
        assert (await r.json())["note"]["content"] == "第一版"

        r = await aenv.client.post(
            f"/api/groups/{G1}/agent-notes/news/rollback", json={"version": 99},
        )
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_put_bad_kind_and_too_long(self, aenv) -> None:
        await aenv.login()
        r = await aenv.client.put(f"/api/groups/{G1}/agent-notes/task", json={"content": "x"})
        assert r.status == 400
        r = await aenv.client.put(
            f"/api/groups/{G1}/agent-notes/news", json={"content": "字" * 1201},
        )
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_cross_group_isolation(self, aenv) -> None:
        await aenv.login()
        await aenv.client.put(f"/api/groups/{G1}/agent-notes/news", json={"content": "甲群"})
        r = await aenv.client.get(f"/api/groups/{G2}/agent-notes")
        assert (await r.json())["notes"] == []
