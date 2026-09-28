"""扩展 roles（谁能用）的校验与生效测试（docs/02 §10、docs/07 §10.9）。

roles 语义：`["worker"]`（默认，只给子 agent）/ `["main"]`（只给主模型）/ 两者都行。

- 网页新增 / 修改（MCP + skill 两边一致）：roles 必须是列表、只能含 worker / main、
  **不能为空**；不合法走 problems → 400，不再静默回落。没传 roles 时新增默认
  `["worker"]`、修改保持原值。
- config.toml 的 `[[extensions.mcp]]` / SKILL.md front matter 里的坏值仍然容错
  回落 worker（不拖垮）。
- 生效：`Tools.specs(role)` 按 roles 过滤；coordinator 给子 agent 挑活的工具名单
  只含 roles 含 worker 的（main-only 的 MCP 工具不在里面）；主模型验收回合的工具表
  带上 roles 含 main 的 MCP 工具；`list_skills` / `read_skill` / skill 提示按调用者
  角色过滤（主模型只看 roles 含 main 的，子 agent 只看 roles 含 worker 的）。

密钥一律假值；HTTP 一律 httpx.MockTransport，不碰真实网络。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork import coordinator, extensions_web
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.skills import Skills, _normalize_roles, parse_front_matter
from CharTyr_MaiWork.skills_tools import register_skill_tools
from CharTyr_MaiWork.store import Store
from CharTyr_MaiWork.tools import Tool, ToolContext, ToolResult, Tools
from test_extensions import MCP_URL, _McpServer, _make_skill, _tool_spec
from test_extensions_web import _login, _make

G1 = "900000001"
WEB_URL = "https://web-ext.example/mcp"


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


def _settings(mcp: list[dict] | None = None):
    raw: dict = {}
    if mcp is not None:
        raw["extensions"] = {"mcp": mcp}
    s, problems = load_settings(raw)
    assert problems == [], problems
    return s


def _reg(tools: Tools, name: str, roles: set[str]) -> None:
    async def _handler(ctx: ToolContext, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="ok")

    tools.register(
        Tool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            roles=frozenset(roles),
            handler=_handler,
        )
    )


# ----------------------------------------------------------------------
# 1. 网页校验：MCP（extensions_web）
# ----------------------------------------------------------------------


class TestMcpRolesValidation:
    def test_create_default_and_explicit(self, store: Store) -> None:
        s = _settings()
        entry = extensions_web.create(store, s, {"name": "a", "url": WEB_URL})
        assert entry["roles"] == ["worker"]  # 没传 → 默认 worker
        entry = extensions_web.create(store, s, {"name": "b", "url": WEB_URL, "roles": ["main"]})
        assert entry["roles"] == ["main"]
        entry = extensions_web.create(
            store, s, {"name": "c", "url": WEB_URL, "roles": ["worker", "main"]}
        )
        assert entry["roles"] == ["main", "worker"]  # 去重 + 排序

    @pytest.mark.parametrize("bad", [[], [""], ["  "], ["boss"], ["worker", "boss"]])
    def test_create_bad_roles_400(self, store: Store, bad) -> None:
        s = _settings()
        with pytest.raises(ValueError) as e:
            extensions_web.create(store, s, {"name": "a", "url": WEB_URL, "roles": bad})
        assert "roles" in str(e.value)
        assert store.kv_get("extensions.mcp") is None  # 一个字节都没落库

    @pytest.mark.parametrize("bad", ["worker", 3, {"worker": True}])
    def test_create_roles_not_list_400(self, store: Store, bad) -> None:
        s = _settings()
        with pytest.raises(ValueError) as e:
            extensions_web.create(store, s, {"name": "a", "url": WEB_URL, "roles": bad})
        assert "roles" in str(e.value)

    def test_update_keeps_roles_when_absent(self, store: Store) -> None:
        s = _settings()
        extensions_web.create(store, s, {"name": "a", "url": WEB_URL, "roles": ["main"]})
        cur = extensions_web.update(store, "a", {"timeout_s": 5})
        assert cur["roles"] == ["main"]  # 没传 → 保持原值
        cur = extensions_web.update(store, "a", {"roles": ["worker", "main"]})
        assert cur["roles"] == ["main", "worker"]
        cur = extensions_web.update(store, "a", {"roles": None})
        assert cur["roles"] == ["main", "worker"]  # None 也算没传

    @pytest.mark.parametrize("bad", [[], [""], ["boss"], ["worker", "boss"]])
    def test_update_bad_roles_400_and_keeps_old(self, store: Store, bad) -> None:
        s = _settings()
        extensions_web.create(store, s, {"name": "a", "url": WEB_URL, "roles": ["main"]})
        with pytest.raises(ValueError) as e:
            extensions_web.update(store, "a", {"roles": bad})
        assert "roles" in str(e.value)
        # 库里那条没被改坏
        current = extensions_web._web_raw_entries(store)
        assert current[0]["roles"] == ["main"]

    @pytest.mark.parametrize("bad", ["worker", 3])
    def test_update_roles_not_list_400(self, store: Store, bad) -> None:
        s = _settings()
        extensions_web.create(store, s, {"name": "a", "url": WEB_URL})
        with pytest.raises(ValueError) as e:
            extensions_web.update(store, "a", {"roles": bad})
        assert "roles" in str(e.value)

    def test_config_toml_bad_roles_still_falls_back_worker(self) -> None:
        """config.toml 是管理员手写的：坏值容错回落 worker，不拖垮这一节。"""
        s, _ = load_settings(
            {"extensions": {"mcp": [{"name": "a", "url": MCP_URL, "roles": ["boss"]}]}}
        )
        assert s.extensions.mcp[0].roles == ("worker",)
        s2, _ = load_settings(
            {"extensions": {"mcp": [{"name": "a", "url": MCP_URL, "roles": "worker"}]}}
        )
        assert s2.extensions.mcp[0].roles == ("worker",)


# ----------------------------------------------------------------------
# 2. 网页校验：skill（skills_web）
# ----------------------------------------------------------------------


class TestSkillRolesValidation:
    def test_create_default_and_explicit(self, tmp_path: Path, store: Store) -> None:
        from CharTyr_MaiWork import skills_web

        view = skills_web.create(tmp_path, store, {"name": "s1", "body": "x"})
        assert view["roles"] == ["worker"]
        view = skills_web.create(
            tmp_path, store, {"name": "s2", "body": "x", "roles": ["main"]}
        )
        assert view["roles"] == ["main"]
        view = skills_web.create(
            tmp_path, store, {"name": "s3", "body": "x", "roles": ["worker", "main"]}
        )
        assert view["roles"] == ["main", "worker"]

    @pytest.mark.parametrize("bad", [[], [""], ["  "], ["boss"], ["worker", "boss"]])
    def test_create_bad_roles_400(self, tmp_path: Path, store: Store, bad) -> None:
        from CharTyr_MaiWork import skills_web

        with pytest.raises(ValueError) as e:
            skills_web.create(tmp_path, store, {"name": "s", "body": "x", "roles": bad})
        assert "roles" in str(e.value)
        assert not (tmp_path / "skills" / "s").exists()  # 目录都没建

    @pytest.mark.parametrize("bad", ["worker", 3])
    def test_create_roles_not_list_400(self, tmp_path: Path, store: Store, bad) -> None:
        from CharTyr_MaiWork import skills_web

        with pytest.raises(ValueError) as e:
            skills_web.create(tmp_path, store, {"name": "s", "body": "x", "roles": bad})
        assert "roles" in str(e.value)

    def test_update_keeps_roles_when_absent(self, tmp_path: Path, store: Store) -> None:
        from CharTyr_MaiWork import skills_web

        skills_web.create(tmp_path, store, {"name": "s", "body": "x", "roles": ["main"]})
        view = skills_web.update(tmp_path, store, "s", {"description": "改描述"})
        assert view["roles"] == ["main"]
        view = skills_web.update(tmp_path, store, "s", {"roles": ["worker", "main"]})
        assert view["roles"] == ["main", "worker"]
        view = skills_web.update(tmp_path, store, "s", {"roles": None})
        assert view["roles"] == ["main", "worker"]  # None 也算没传

    @pytest.mark.parametrize("bad", [[], [""], ["boss"], ["worker", "boss"]])
    def test_update_bad_roles_400_and_keeps_old(
        self, tmp_path: Path, store: Store, bad
    ) -> None:
        from CharTyr_MaiWork import skills_web

        skills_web.create(tmp_path, store, {"name": "s", "body": "x", "roles": ["main"]})
        with pytest.raises(ValueError) as e:
            skills_web.update(tmp_path, store, "s", {"roles": bad})
        assert "roles" in str(e.value)
        assert skills_web.get_view(tmp_path, store, "s")["roles"] == ["main"]  # 没被改坏


# ----------------------------------------------------------------------
# 3. HTTP 层：problems → 400（MCP 和 skill 两边一致）
# ----------------------------------------------------------------------


class TestHttp400:
    @pytest.mark.asyncio
    async def test_bad_roles_400_both_sides(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path)
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp", json={"name": "x", "url": WEB_URL, "roles": []}
            )
            assert r.status == 400
            assert "roles" in (await r.text())
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": "x", "url": WEB_URL, "roles": ["worker", "boss"]},
            )
            assert r.status == 400
            r = await client.post(
                "/api/extensions/skills", json={"name": "s", "body": "x", "roles": []}
            )
            assert r.status == 400
            assert "roles" in (await r.text())
            # 两边都没落库
            assert app.store.kv_get("extensions.mcp") is None
            assert not (app.get_settings().data_dir / "skills" / "s").exists()
        finally:
            await client.close()
            await app.stop()


# ----------------------------------------------------------------------
# 4. SKILL.md front matter：坏值容错回落 worker
# ----------------------------------------------------------------------


class TestFrontMatterTolerance:
    def test_bad_roles_fall_back_worker(self, tmp_path: Path) -> None:
        _make_skill(tmp_path, "bad", front="---\nname: bad\nroles: boss\n---\n")
        _make_skill(tmp_path, "bare", body="没 front matter\n")
        _make_skill(tmp_path, "both", front="---\nname: both\nroles: [worker, main]\n---\n")
        sk = Skills(tmp_path)
        by_name = {i["name"]: i for i in sk.list()}
        assert by_name["bad"]["roles"] == ["worker"]
        assert by_name["bare"]["roles"] == ["worker"]
        assert set(by_name["both"]["roles"]) == {"worker", "main"}
        # 解析函数本身也容错
        assert _normalize_roles(parse_front_matter("---\nroles: boss\n---\n").get("roles")) == ["worker"]
        assert _normalize_roles([]) == ["worker"]


# ----------------------------------------------------------------------
# 5. MCP：Tools 注册表按 roles 分流（真实 Extensions + MockTransport）
# ----------------------------------------------------------------------


class TestMcpRoleRouting:
    @pytest.mark.asyncio
    async def test_specs_split_by_roles(self, store: Store) -> None:
        from CharTyr_MaiWork.extensions import Extensions

        server = _McpServer([_tool_spec("t")])
        settings = _settings(
            [
                {"name": "mainsrv", "url": MCP_URL, "roles": ["main"]},
                {"name": "worksrv", "url": MCP_URL, "roles": ["worker"]},
                {"name": "bothsrv", "url": MCP_URL, "roles": ["main", "worker"]},
            ]
        )
        tools = Tools(store)
        ext = Extensions(lambda: settings, transport=httpx.MockTransport(server))
        await ext.start(tools)
        try:
            main_names = [s["function"]["name"] for s in tools.specs("main")]
            worker_names = [s["function"]["name"] for s in tools.specs("worker")]
            assert "mcp_mainsrv_t" in main_names and "mcp_mainsrv_t" not in worker_names
            assert "mcp_worksrv_t" in worker_names and "mcp_worksrv_t" not in main_names
            assert "mcp_bothsrv_t" in main_names and "mcp_bothsrv_t" in worker_names
            # 角色不符的调用被拒
            res = await tools.call(
                "mcp_mainsrv_t", {}, ToolContext(group_id=G1, actor="子 agent #1", role="worker")
            )
            assert res.ok is False
        finally:
            await ext.aclose()


# ----------------------------------------------------------------------
# 6. coordinator：子 agent 候选名单 / 主模型工具表
# ----------------------------------------------------------------------


class TestCoordinatorRoleRouting:
    def test_worker_candidate_list_only_worker_roles(self, store: Store) -> None:
        tools = Tools(store)
        _reg(tools, "read_file", {"worker"})
        _reg(tools, "vm_run", {"worker"})  # railway 专用：选中 railway 时才换上，不进候选名单
        _reg(tools, "mcp_main_only", {"main"})
        _reg(tools, "mcp_worker_only", {"worker"})
        _reg(tools, "mcp_both", {"main", "worker"})
        _reg(tools, "submit_result", {"worker"})
        names = coordinator.worker_job_tool_names(tools)
        assert names == ["read_file", "mcp_worker_only", "mcp_both"]
        assert "mcp_main_only" not in names
        assert "vm_run" not in names  # railway 专用工具：选中 railway 时才换上
        assert "submit_result" not in names  # 交回工具每次自动补，不进候选名单

    def test_main_review_tool_table_carries_main_mcp(self, store: Store) -> None:
        tools = Tools(store)
        _reg(tools, "inspect_file", {"main"})
        _reg(tools, "inspect_files", {"main"})
        _reg(tools, "remember", {"main"})
        _reg(tools, "mcp_main_only", {"main"})
        _reg(tools, "mcp_worker_only", {"worker"})
        names = [s["function"]["name"] for s in coordinator.main_review_tool_specs(tools)]
        assert names == ["inspect_file", "inspect_files", "mcp_main_only"]
        assert "mcp_worker_only" not in names
        assert "remember" not in names  # 「记经验」有自己的回合，不混进验收

    @pytest.mark.asyncio
    async def test_plan_prompt_and_review_round_wired(self, tmp_path: Path) -> None:
        """端到端：计划提示里列 worker 工具、不列 main-only；验收回合 tools= 带 main MCP。"""
        from test_coordinator import (
            FakeDelivery,
            FakeOutbox,
            FakeWorkers,
            ModelsQueue,
            _Profiles,
            _Settings,
            _create_task,
            _plan,
            _review,
        )

        from CharTyr_MaiWork.coordinator import Coordinator
        from CharTyr_MaiWork.environments.local import LocalEnv
        from CharTyr_MaiWork.goals import Goals
        from CharTyr_MaiWork.tasks import Tasks
        from CharTyr_MaiWork.tools_exec import register_exec_tools

        store = Store(tmp_path / "m.db")
        store.migrate()
        try:
            settings = _Settings(tmp_path / "ws")
            env = LocalEnv(lambda: settings)
            tools = Tools(store)
            register_exec_tools(
                tools, env=env, host=None, get_settings=lambda: settings, session_of=lambda gid: "s"
            )
            _reg(tools, "mcp_gh_main", {"main"})
            _reg(tools, "mcp_gh_worker", {"worker"})
            tasks = Tasks(store, lambda: settings)
            goals = Goals(store, lambda: settings)
            models = ModelsQueue(replies=[_plan(), _review(pass_=True)])
            workers = FakeWorkers()
            coordinator_ = Coordinator(
                store,
                models,
                workers,
                tools,
                tasks,
                goals,
                FakeDelivery(),
                FakeOutbox(),
                env,
                _Profiles(),
                lambda: settings,
            )
            tid = _create_task(tasks)

            async def _write_artifact() -> None:
                ws = env.workspace(tasks.get(tid)["workspace"])
                (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
                (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

            workers.before_return = _write_artifact
            await coordinator_.run_task(tid)

            # 计划回合（第一次 chat）：候选名单列了 worker 工具，没列 main-only
            plan_prompt = str(models.calls[0][1][0]["content"])
            assert "mcp_gh_worker" in plan_prompt
            assert "mcp_gh_main" not in plan_prompt
            # 验收回合：tools= 带 roles 含 main 的 MCP 工具，不带 worker-only 的
            review_calls = [c for c in models.calls if c[2].get("purpose") == "coordinator.review"]
            assert len(review_calls) == 1
            review_names = [t["function"]["name"] for t in (review_calls[0][2].get("tools") or [])]
            assert "mcp_gh_main" in review_names
            assert "inspect_file" in review_names
            assert "mcp_gh_worker" not in review_names
        finally:
            store.close()


# ----------------------------------------------------------------------
# 7. skill 工具 / 提示按调用者角色过滤
# ----------------------------------------------------------------------


def _role_skills(tmp_path: Path) -> Skills:
    _make_skill(
        tmp_path,
        "main_only",
        front="---\nname: main_only\ndescription: 主模型的\nroles: main\n---\n",
        body="主模型正文\n",
    )
    _make_skill(
        tmp_path,
        "worker_only",
        front="---\nname: worker_only\ndescription: 子 agent 的\nroles: worker\n---\n",
        body="子 agent 正文\n",
        files={"x.txt": "附属\n"},
    )
    _make_skill(
        tmp_path,
        "both",
        front="---\nname: both\ndescription: 两边都行\nroles: worker, main\n---\n",
        body="通用正文\n",
    )
    _make_skill(tmp_path, "bare", body="没 front matter → 默认 worker\n")
    return Skills(tmp_path)


def _worker_ctx() -> ToolContext:
    return ToolContext(group_id=G1, task_id="T-1", actor="子 agent #1", role="worker")


def _main_ctx() -> ToolContext:
    return ToolContext(group_id=G1, task_id="T-1", actor="主模型", role="main")


class TestSkillToolFiltering:
    def test_effective_role_from_ctx(self) -> None:
        assert _worker_ctx().effective_role() == "worker"
        assert _main_ctx().effective_role() == "main"
        # role 没显式给 → 按 actor 引导式判断
        assert ToolContext(group_id=G1, actor="子 agent #2").effective_role() == "worker"
        assert ToolContext(group_id=G1, actor="主模型").effective_role() == "main"

    @pytest.mark.asyncio
    async def test_list_skills_filtered_by_role(self, store: Store, tmp_path: Path) -> None:
        tools = Tools(store)
        register_skill_tools(tools, _role_skills(tmp_path))

        res = await tools.call("list_skills", {}, _worker_ctx())
        assert res.ok is True
        names = {i["name"] for i in res.data}
        assert names == {"worker_only", "both", "bare"}
        assert "main_only" not in res.output

        res = await tools.call("list_skills", {}, _main_ctx())
        assert res.ok is True
        names = {i["name"] for i in res.data}
        assert names == {"main_only", "both"}
        assert "worker_only" not in res.output
        assert "bare" not in res.output

    @pytest.mark.asyncio
    async def test_read_skill_filtered_by_role(self, store: Store, tmp_path: Path) -> None:
        tools = Tools(store)
        register_skill_tools(tools, _role_skills(tmp_path))

        # 子 agent 读不到 main-only，主模型读得到
        res = await tools.call("read_skill", {"name": "main_only"}, _worker_ctx())
        assert res.ok is False
        res = await tools.call("read_skill", {"name": "main_only"}, _main_ctx())
        assert res.ok is True and "主模型正文" in res.output
        # 主模型读不到 worker-only（连附属文件也不给）
        res = await tools.call("read_skill", {"name": "worker_only"}, _main_ctx())
        assert res.ok is False
        res = await tools.call("read_skill", {"name": "worker_only", "file": "x.txt"}, _main_ctx())
        assert res.ok is False
        res = await tools.call("read_skill", {"name": "worker_only", "file": "x.txt"}, _worker_ctx())
        assert res.ok is True and "附属" in res.output
        # 两边都行的：都能读
        assert (await tools.call("read_skill", {"name": "both"}, _worker_ctx())).ok is True
        assert (await tools.call("read_skill", {"name": "both"}, _main_ctx())).ok is True

    @pytest.mark.asyncio
    async def test_role_from_actor_when_ctx_role_empty(
        self, store: Store, tmp_path: Path
    ) -> None:
        tools = Tools(store)
        register_skill_tools(tools, _role_skills(tmp_path))
        res = await tools.call(
            "list_skills", {}, ToolContext(group_id=G1, actor="子 agent #1")
        )
        assert {i["name"] for i in res.data} == {"worker_only", "both", "bare"}
        res = await tools.call("list_skills", {}, ToolContext(group_id=G1, actor="主模型"))
        assert {i["name"] for i in res.data} == {"main_only", "both"}

    def test_hint_only_worker_skills(self, tmp_path: Path) -> None:
        """子 agent 的 system 提示（hint）只挂 roles 含 worker 的 skill。"""
        hint = _role_skills(tmp_path).hint()
        assert "worker_only" in hint and "both" in hint and "bare" in hint
        assert "main_only" not in hint

    def test_skills_list_role_argument(self, tmp_path: Path) -> None:
        sk = _role_skills(tmp_path)
        assert {i["name"] for i in sk.list("worker")} == {"worker_only", "both", "bare"}
        assert {i["name"] for i in sk.list("main")} == {"main_only", "both"}
        assert {i["name"] for i in sk.list()} == {"main_only", "worker_only", "both", "bare"}
        assert sk.roles("main_only") == ["main"]
        assert sk.roles("nope") is None


# ----------------------------------------------------------------------
# 8. 网页视图：roles 按原样回显（GET /api/extensions）
# ----------------------------------------------------------------------


class TestViewRoles:
    @pytest.mark.asyncio
    async def test_mcp_and_skill_roles_round_trip(self, tmp_path: Path) -> None:
        app, client = await _make(tmp_path, server=_McpServer([_tool_spec("t")]))
        try:
            await _login(client)
            r = await client.post(
                "/api/extensions/mcp",
                json={"name": "m", "url": WEB_URL, "roles": ["main"]},
            )
            assert r.status == 200
            r = await client.post(
                "/api/extensions/skills",
                json={"name": "s", "body": "x", "roles": ["main", "worker"]},
            )
            assert r.status == 200
            r = await client.get("/api/extensions")
            data = await r.json()
            mcp_item = next(m for m in data["mcp"] if m["name"] == "m")
            skill_item = next(s for s in data["skills"] if s["name"] == "s")
            assert mcp_item["roles"] == ["main"]
            assert skill_item["roles"] == ["main", "worker"]
            # 主模型 / 子 agent 的工具表：m 只给主模型，skills 工具两边都有
            main_names = [t["function"]["name"] for t in app.tools.specs("main")]
            worker_names = [t["function"]["name"] for t in app.tools.specs("worker")]
            assert "mcp_m_t" in main_names and "mcp_m_t" not in worker_names
            assert "list_skills" in main_names and "list_skills" in worker_names
        finally:
            await client.close()
            await app.stop()
