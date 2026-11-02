"""线上任务卡脖子问题回归测试（按需上云、远端拷回子路径/中文名、引用核对范围、随口一问减负、通才技能与客观查不到转问）。"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import test_coordinator_env as ce
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tool, ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools
from CharTyr_MaiWork.maiwork.tools_ssh import register_machine_tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport

pytestmark = pytest.mark.asyncio


def _make_settings(ws_root: Path, *, railway: bool = True):
    s = ce._Settings(ws_root, railway=railway)
    s.is_served = lambda _gid: True  # type: ignore[attr-defined]
    return s


def _register_research_stubs(tools: Tools) -> None:
    """给 worker 注册 web_search / fetch_page，满足调研活能力自检的注册表核验。"""
    for name in ("web_search", "fetch_page"):
        if tools.get(name, "worker") is not None:
            continue

        async def _noop(ctx: ToolContext, args: dict) -> ToolResult:
            del ctx, args
            return ToolResult(ok=True, output="ok")

        tools.register(
            Tool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                roles=frozenset({"worker"}),
                handler=_noop,
            )
        )


async def test_pure_non_exec_task_stays_local_even_when_vm_available(tmp_path: Path):
    """纯检索 + 写文字的任务不占一次性 VM、不塞 vm_fetch_file 警告。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks)
        plan_json = json.dumps(
            {
                "criteria": ["说清 BDS 运动是什么"],
                "scale": "brief",
                "deliver_kind": "text",
                "env": "railway",
                "env_reason": "默认选了机器",
                "jobs": [
                    {
                        "brief": f"搜集 BDS 运动资料并把回复写进 artifacts/{tid}/reply.md",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "write_file"],
                    }
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        review_json = ce._review(pass_=True, artifact=f"artifacts/{tid}/reply.md")
        models = ce.ModelsQueue(replies=[plan_json, review_json])
        workers = ce.FakeWorkers()
        railway = ce.FakeRailwayEnv(give_box=True)

        async def _write_reply():
            ws_name = tasks.get(tid)["workspace"]
            await env.write_file(ws_name, f"artifacts/{tid}/reply.md", "BDS 是抵制、撤资、制裁运动。")

        workers.before_return = _write_reply
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=workers, railway=railway,
        )
        await coord.run_task(tid)

        t = tasks.get(tid)
        assert t["status"] == "completed", t
        assert railway.acquire_calls == [], "纯检索写字任务不该申请一次性 VM"
        assert "本机" in str(t["env"])
        assert "railway" not in str(t["env"]).lower()
        call_tools = set(workers.calls[0]["tools"])
        assert "vm_run" not in call_tools and "vm_fetch_file" not in call_tools
        assert "vm_fetch_file" not in workers.calls[0]["brief"]
    finally:
        store.close()


async def test_pure_non_exec_task_stays_local_even_when_ssh_available(tmp_path: Path):
    """专用机器就位时，纯检索 + render_report 出网页也留在本机工作区，不占专用机器。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks)
        plan_json = json.dumps(
            {
                "criteria": ["做成单页对比"],
                "scale": "standard",
                "deliver_kind": "view",
                "env": "ssh",
                "env_reason": "顺手填了 ssh",
                "jobs": [
                    {
                        "brief": f"查清对比并用 render_report 输出 artifacts/{tid}/index.html",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "render_report", "write_file"],
                    }
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        models = ce.ModelsQueue(replies=[plan_json, ce._review(pass_=True, artifact=f"artifacts/{tid}/index.html")])
        workers = ce.FakeWorkers()
        ssh = ce.FakeSshEnv(box=ce._SshBox())
        register_machine_tools(tools, get_box=lambda _tid: ssh.box, env=types.SimpleNamespace(), local_env=env)

        async def _write_page():
            ws_name = tasks.get(tid)["workspace"]
            await env.write_file(ws_name, f"artifacts/{tid}/index.html", "<!doctype html><title>ok</title><p>内容</p>")

        workers.before_return = _write_page
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=workers, railway=None, ssh=ssh,
        )
        await coord.run_task(tid)

        assert tasks.get(tid)["status"] == "completed"
        assert ssh.acquire_calls == []
        call_tools = set(workers.calls[0]["tools"])
        assert "machine_run" not in call_tools and "machine_fetch_file" not in call_tools
        assert "machine_fetch_file" not in workers.calls[0]["brief"]
    finally:
        store.close()


async def test_mixed_two_job_plan_only_attaches_vm_to_exec_job(tmp_path: Path):
    """两步任务（第 1 步纯调研写资料、第 2 步跑命令制图）：申请 VM，但第 1 步不塞 vm_* / vm_fetch_file。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks)
        plan_json = json.dumps(
            {
                "criteria": ["按资料生成交互页"],
                "scale": "standard",
                "deliver_kind": "view",
                "env": "railway",
                "jobs": [
                    {
                        "brief": "先搜集资料写成调研底稿",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "write_file"],
                    },
                    {
                        "brief": f"跑脚本把底稿做成 artifacts/{tid}/index.html",
                        "type": "build",
                        "after": [1],
                        "tools": ["run_command", "read_file", "write_file"],
                    },
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        models = ce.ModelsQueue(replies=[plan_json, ce._review(pass_=True, artifact=f"artifacts/{tid}/index.html")])
        railway = ce.FakeRailwayEnv(give_box=True)

        class _StepWorkers(ce.FakeWorkers):
            async def run(self, brief, **kw):
                ws_name = tasks.get(tid)["workspace"]
                idx = len(self.calls) + 1
                if idx == 1:
                    await env.write_file(ws_name, f"artifacts/{tid}/steps/1/research.md", "# 调研底稿\n真实内容")
                    self.reports.append(
                        WorkerReport(ok=True, summary="底稿写好", evidence=[f"artifacts/{tid}/steps/1/research.md"])
                    )
                else:
                    await env.write_file(ws_name, f"artifacts/{tid}/index.html", "<!doctype html><p>成品</p>")
                    self.reports.append(
                        WorkerReport(ok=True, summary="网页做好", evidence=[f"artifacts/{tid}/index.html"])
                    )
                return await super().run(brief, **kw)

        workers = _StepWorkers()
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=workers, railway=railway,
        )
        await coord.run_task(tid)

        assert tasks.get(tid)["status"] == "completed"
        assert railway.acquire_calls == [tid]
        assert railway.release_calls == 1
        assert len(workers.calls) == 2
        step1_tools = set(workers.calls[0]["tools"])
        step2_tools = set(workers.calls[1]["tools"])
        assert "vm_run" not in step1_tools and "vm_fetch_file" not in step1_tools
        assert "vm_fetch_file" not in workers.calls[0]["brief"]
        assert "vm_run" in step2_tools and "vm_fetch_file" in step2_tools
        assert "vm_fetch_file" in workers.calls[1]["brief"]
    finally:
        store.close()


async def test_non_exec_task_succeeds_when_local_exec_restricted_and_no_vm(tmp_path: Path):
    """本机不能隔离跑命令、也没配远程机器时：纯检索写字照常完成；要跑命令的活照旧诚实失败。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)

        # 1) 纯非执行任务：成功
        tid_ok = ce._create_task(tasks)
        non_exec_plan = json.dumps(
            {
                "criteria": ["直接文字回答"],
                "scale": "brief",
                "deliver_kind": "text",
                "env": "local",
                "jobs": [
                    {
                        "brief": f"查一下写进 artifacts/{tid_ok}/reply.md",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "write_file"],
                    }
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        models_ok = ce.ModelsQueue(
            replies=[non_exec_plan, ce._review(pass_=True, artifact=f"artifacts/{tid_ok}/reply.md")]
        )
        workers_ok = ce.FakeWorkers()

        async def _write_ok():
            ws_name = tasks.get(tid_ok)["workspace"]
            await env.write_file(ws_name, f"artifacts/{tid_ok}/reply.md", "简短回答正文。")

        workers_ok.before_return = _write_ok
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models_ok, workers=workers_ok, railway=None,
        )
        coord._capability = types.SimpleNamespace(ok=False, reason="缺少隔离沙箱")  # noqa: SLF001
        await coord.run_task(tid_ok)
        assert tasks.get(tid_ok)["status"] == "completed"

        # 2) 要跑命令的任务：仍然诚实报「本机不能隔离跑命令」失败
        tid_exec = ce._create_task(tasks)
        exec_plan = ce._plan(env="local")  # 默认带 run_command
        tools_exec_only = Tools(store)
        coord_exec = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools_exec_only,
            tasks=tasks, goals=goals, models=ce.ModelsQueue(replies=[exec_plan]),
            workers=ce.FakeWorkers(), railway=None,
        )
        coord_exec._capability = types.SimpleNamespace(ok=False, reason="缺少隔离沙箱")  # noqa: SLF001
        await coord_exec.run_task(tid_exec)
        assert tasks.get(tid_exec)["status"] == "failed"
        assert "不能隔离跑命令" in str(tasks.get(tid_exec)["review"] or "")
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["vm", "ssh"])
async def test_fetch_file_allows_subpaths_chinese_names_and_enforces_guards(tmp_path: Path, kind: str):
    """vm_fetch_file / machine_fetch_file 支持安全子目录、中文文件名、冗余前缀去重，并守住越界与写范围。"""
    store = Store(tmp_path / f"{kind}.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws")
        local = LocalEnv(lambda: settings)
        ws = local.workspace("ws-demo")

        async def _get(_box, _remote, dst: Path):
            Path(dst).write_text("FETCHED_OK", encoding="utf-8")

        remote = types.SimpleNamespace(get=_get)
        tools = Tools(store)
        if kind == "vm":
            box = types.SimpleNamespace(remote_root="/app", expires_ts=0.0)
            register_vm_tools(tools, get_box=lambda: box, env=remote, local_env=local)
            tool_name = "vm_fetch_file"
            remote_file = "/app/research.md"
        else:
            box = types.SimpleNamespace(kind="ssh", name="vps-1", remote_dir="/tmp/mw")
            register_machine_tools(tools, get_box=lambda _tid: box, env=remote, local_env=local)
            tool_name = "machine_fetch_file"
            remote_file = "out/research.md"

        ctx = ToolContext(group_id="g1", task_id="T-1", actor="worker", role="worker", workspace=ws)

        # 1. 允许 steps/ 子路径、images/ 子路径、中文文件名、冗余 artifacts/T-1/ 前缀
        for local_name, expected_rel in [
            ("steps/1/research.md", "artifacts/T-1/steps/1/research.md"),
            ("images/海报1.png", "artifacts/T-1/images/海报1.png"),
            ("排障说明.md", "artifacts/T-1/排障说明.md"),
            ("artifacts/T-1/steps/2/summary.md", "artifacts/T-1/steps/2/summary.md"),
        ]:
            r = await tools.call(tool_name, {"remote_path": remote_file, "local_name": local_name}, ctx)
            assert r.ok, f"{local_name} 应通过，实际报错：{r.error}"
            assert local.resolve("ws-demo", expected_rel).read_text(encoding="utf-8") == "FETCHED_OK"

        # 2. 中间步骤带 write_scope=("artifacts/T-1/steps/1",)：
        #    不传 local_name 自动落进该步骤目录；传步骤外路径被拒
        step_ctx = ToolContext(
            group_id="g1", task_id="T-1", actor="worker", role="worker", workspace=ws,
            write_scope=("artifacts/T-1/steps/1",),
        )
        r_auto = await tools.call(tool_name, {"remote_path": remote_file}, step_ctx)
        assert r_auto.ok, r_auto.error
        assert local.resolve("ws-demo", "artifacts/T-1/steps/1/research.md").read_text(encoding="utf-8") == "FETCHED_OK"

        r_out_scope = await tools.call(tool_name, {"remote_path": remote_file, "local_name": "index.html"}, step_ctx)
        assert not r_out_scope.ok
        assert "steps/1" in r_out_scope.error

        # 3. 危险路径一律拒绝：..、绝对路径、别的任务目录、input/ 原图目录、隐藏文件、Windows 保留字
        for bad in [
            "../evil.html",
            "steps/../../evil.html",
            "/etc/passwd",
            "artifacts/T-2/steal.md",
            "input/原图1.jpg",
            ".bashrc",
            "CON.txt",
            "a:b.txt",
        ]:
            r_bad = await tools.call(tool_name, {"remote_path": remote_file, "local_name": bad}, ctx)
            assert not r_bad.ok, f"危险 local_name={bad!r} 不该通过"

        # 4. 目标或其父目录是符号链接指向外部：拒绝
        outside = tmp_path / "outside.txt"
        outside.write_text("SECRET", encoding="utf-8")
        sym_dir = ws / "artifacts" / "T-1" / "sym"
        sym_dir.parent.mkdir(parents=True, exist_ok=True)
        sym_dir.symlink_to(tmp_path)
        r_sym = await tools.call(tool_name, {"remote_path": remote_file, "local_name": "sym/pwn.txt"}, ctx)
        assert not r_sym.ok
        assert not (tmp_path / "pwn.txt").exists()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Slice 2：引用核对只扫真正交付成品 & 直链文件核验 + brief 随口一问减负
# ---------------------------------------------------------------------------


def _insert_tool_call(
    store: Store, *, tid: str, tool: str, inp: str, output: str = "取到正文 500 字", ok: int = 1
) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ok, ms) "
            "VALUES (1790000000, '900000001', ?, '子 agent #1', ?, ?, ?, ?, 12)",
            (tid, tool, inp, output, int(ok)),
        )


async def test_link_check_for_text_deliverable_only_scans_reply_md_and_ignores_scratch_notes(
    tmp_path: Path,
):
    """deliver_kind=text 有 reply.md 时，只核对 reply.md（+ summary/evidence），忽略 说明.md / notes.md / steps/ 等过程文件。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks, req="查一下 BDS 运动")
        _insert_tool_call(store, tid=tid, tool="fetch_page", inp="https://opened.example/1")
        _insert_tool_call(store, tid=tid, tool="fetch_page", inp="https://opened.example/2")

        plan_json = json.dumps(
            {
                "criteria": ["说清 BDS 运动并附核实出处"],
                "scale": "brief",
                "deliver_kind": "text",
                "env": "local",
                "jobs": [
                    {
                        "brief": f"查清写进 artifacts/{tid}/reply.md",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "write_file"],
                    }
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        models = ce.ModelsQueue(
            replies=[plan_json, ce._review(pass_=True, artifact=f"artifacts/{tid}/reply.md")]
        )
        workers = ce.FakeWorkers(reports=[WorkerReport(ok=True, summary="已查清写进回复", evidence=[])])

        async def _write_files():
            ws_name = tasks.get(tid)["workspace"]
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/reply.md",
                "BDS 是抵制、撤资、制裁运动。参考：https://opened.example/1 与 https://opened.example/2",
            )
            # 下面全是子 agent 顺手留下的过程排查文件（不会发给群友）
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/说明.md",
                "候选死链排查：https://dead1.example/404 https://dead2.example/403",
            )
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/notes.md",
                "搜索草稿：https://snippet1.example/a https://snippet2.example/b",
            )
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/steps/1/research.md",
                "初筛：https://step-cand.example/c",
            )
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/rawsearch/page.html",
                "<a href='https://raw.example/d'>raw</a>",
            )

        workers.before_return = _write_files
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=workers, railway=None,
        )
        await coord.run_task(tid)

        saved = store.kv_get(f"task.link_check.{tid}")
        assert isinstance(saved, dict), saved
        assert saved["links"] == 2
        assert saved["unopened"] == 0
        assert saved["unopened_urls"] == []
    finally:
        store.close()


async def test_link_check_for_view_deliverable_only_scans_root_html_and_ignores_steps(
    tmp_path: Path,
):
    """deliver_kind=view 有根级 .html 成品时，只核对根级 .html（+ summary/evidence），不扫 steps/ 与过程笔记。"""
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks, req="做一页调研汇总")
        _insert_tool_call(store, tid=tid, tool="fetch_page", inp="https://opened.example/ok")

        plan_json = json.dumps(
            {
                "criteria": ["做成单页汇总"],
                "scale": "standard",
                "deliver_kind": "view",
                "env": "local",
                "jobs": [
                    {
                        "brief": f"做成 artifacts/{tid}/index.html",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "render_report", "write_file"],
                    }
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        models = ce.ModelsQueue(
            replies=[plan_json, ce._review(pass_=True, artifact=f"artifacts/{tid}/index.html")]
        )
        workers = ce.FakeWorkers(reports=[WorkerReport(ok=True, summary="已生成单页", evidence=[])])

        async def _write_files():
            ws_name = tasks.get(tid)["workspace"]
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/index.html",
                '<!doctype html><a href="https://opened.example/ok">出处</a>',
            )
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/steps/1/research.md",
                "未采用候选：https://cand1.example/x https://cand2.example/y",
            )
            await env.write_file(
                ws_name,
                f"artifacts/{tid}/notes.md",
                "备忘：https://cand3.example/z",
            )

        workers.before_return = _write_files
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=workers, railway=None,
        )
        await coord.run_task(tid)

        saved = store.kv_get(f"task.link_check.{tid}")
        assert isinstance(saved, dict), saved
        assert saved["links"] == 1
        assert saved["unopened"] == 0
    finally:
        store.close()


async def test_fetch_page_http200_pdf_counts_as_verified_for_task_link_check_not_news(
    tmp_path: Path,
):
    """fetch_page 遇到 HTTP 200 的 PDF 直链：工具仍返 ok=False（资讯补打开不放水），但任务引用核对认该直链及跳转终址可访问。"""
    import httpx

    from CharTyr_MaiWork.maiwork.tools_builtin import opened_urls_from_rows, register_builtin

    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        def _handler(req: httpx.Request) -> httpx.Response:
            if str(req.url) == "https://short.example/pdf":
                return httpx.Response(302, headers={"location": "https://official.example/report.pdf"})
            if str(req.url) == "https://official.example/report.pdf":
                return httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=b"%PDF-1.4 fake pdf bytes",
                )
            return httpx.Response(404)

        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        register_builtin(
            tools,
            search=None,
            profiles=types.SimpleNamespace(entries=lambda _g: []),
            http_transport=httpx.MockTransport(_handler),
            resolver=lambda _h: ["93.184.216.34"],
        )
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks, req="找一下官方白皮书 PDF 链接")

        ctx = ToolContext(group_id=ce.GID, task_id=tid, actor="子 agent #1", role="worker")
        r = await tools.call("fetch_page", {"url": "https://short.example/pdf"}, ctx)
        assert r.ok is False
        assert "页面类型是 application/pdf" in r.error
        assert "最终地址：https://official.example/report.pdf" in r.error

        # 资讯核验用的 opened_urls_from_rows 仍然严格不认 ok=0
        rows = store.read().execute(
            "SELECT tool, input, output, ok FROM tool_calls WHERE task_id=?", (tid,)
        ).fetchall()
        assert opened_urls_from_rows(rows) == set()

        # 任务协调器 _opened_urls 认 HTTP 200 直链（含跳转前后的地址）
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=ce.ModelsQueue(), workers=ce.FakeWorkers(), railway=None,
        )
        opened = coord._opened_urls(tid)  # noqa: SLF001
        assert "short.example/pdf" in opened
        assert "official.example/report.pdf" in opened
    finally:
        store.close()


async def test_brief_scale_forbids_process_notes_and_separates_url_length(tmp_path: Path):
    """scale=brief：计划提示禁止加码补充项与额外写过程文件；验收提示声明链接 URL 长度另算；调研口径禁塞未打开链接。"""
    from CharTyr_MaiWork.maiwork import coordinator as co
    from CharTyr_MaiWork.maiwork.workers import research_chat_framework, research_report_framework

    tail = co.with_brief_job_tail("搜一下")
    assert "不做网页、文件或长报告" in tail
    assert "说明.md" in tail and "notes.md" in tail

    assert "搜索摘要" in research_chat_framework()
    assert "搜索摘要" in research_report_framework()

    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        tid = ce._create_task(tasks, req="搜搜今天啥情况")
        plan_json = json.dumps(
            {
                "criteria": ["几句话说清"],
                "scale": "brief",
                "deliver_kind": "text",
                "env": "local",
                "jobs": [{"brief": "搜一下", "type": "research", "tools": ["web_search", "fetch_page", "write_file"]}],
                "question": None,
            },
            ensure_ascii=False,
        )
        models = ce.ModelsQueue(
            replies=[plan_json, ce._review(pass_=True, artifact=f"artifacts/{tid}/reply.md")]
        )
        workers = ce.FakeWorkers()

        async def _write():
            ws_name = tasks.get(tid)["workspace"]
            await env.write_file(ws_name, f"artifacts/{tid}/reply.md", "一句话说清了。")

        workers.before_return = _write
        coord = ce._build(
            mem_store=store, settings=settings, env=env, tools=tools,
            tasks=tasks, goals=goals, models=models, workers=workers, railway=None,
        )
        await coord.run_task(tid)

        plan_prompt = "\n".join(str(m.get("content") or "") for m in models.calls[0][1])
        review_prompt = "\n".join(str(m.get("content") or "") for m in models.calls[1][1])
        assert "不要自己加码" in plan_prompt and "说明.md" in plan_prompt
        assert "链接" in review_prompt and "另算" in review_prompt
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Slice 3：通才内置技能保底 + 网页 task 保存不丢通才 + 按可读性点名 skill + 查证卡住转问线索
# ---------------------------------------------------------------------------


async def test_task_generalist_keeps_core_builtin_skills_even_with_stale_saved_profile(tmp_path: Path):
    """线上 task 岗位若存过旧的 skills 列表（或空列表），仍保底可读内置通用做法技能；手动停用时立刻生效。"""
    from CharTyr_MaiWork.maiwork.agents import Agents
    from CharTyr_MaiWork.maiwork.skills import Skills, set_disabled
    from CharTyr_MaiWork.maiwork.specialists import Specialists

    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        agents = Agents(store, lambda: settings)
        skills = Skills(tmp_path / "skills", store=store, settings=settings)
        workers = ce.FakeWorkers()
        sp = Specialists(agents, workers, skills)

        # 模拟旧版网页编辑过 task 模型后把 skills 写成了旧子集或空数组
        for stale_skills in (["search-you"], []):
            agents.update_profile("task", {"skills": stale_skills})
            allowed = sp._resolve_allowed_skills("task")  # noqa: SLF001
            assert allowed is not None
            assert "research-report" in allowed
            assert "general-task" in allowed
            assert "office-docs" in allowed

        # 管理员在扩展页手动停用某个内置 skill → 立刻从 allowed_skills 摘掉
        set_disabled(store, "research-report", True)
        allowed_after_disable = sp._resolve_allowed_skills("task")  # noqa: SLF001
        assert allowed_after_disable is not None
        assert "research-report" not in allowed_after_disable
        assert "general-task" in allowed_after_disable

        # 专岗（news）不注入通用任务技能
        set_disabled(store, "research-report", False)
        agents.update_profile("news", {"skills": []})
        assert sp._resolve_allowed_skills("news") == ()  # noqa: SLF001
    finally:
        store.close()


async def test_agents_js_task_null_skills_renders_all_checked_and_saves_null():
    """网页专岗设置：task / 自定义专岗 skills=null 时全选渲染，全勾保存仍发 null（不锁死未来新增内置技能）。"""
    import subprocess

    js_path = Path(__file__).resolve().parent.parent / "maiwork/console/static/js/settings/agents.js"
    src = "\n".join(
        line for line in js_path.read_text(encoding="utf-8").splitlines() if not line.startswith("import ")
    ).replace("export ", "")
    state = {
        "agents": {
            "profiles": [
                {"kind": "task", "title": "通用任务", "model": "m1", "skills": None,
                 "tools_view": {"mode": "per_job", "candidates": ["write_file"]}},
                {"kind": "news", "title": "资讯", "model": "m1", "skills": [],
                 "tools_view": {"mode": "fixed_plus_skills", "factory": ["web_search"], "skills": [], "phases": {}}},
            ]
        },
        "agentSkills": [
            {"name": "research-report", "enabled": True, "allowed_tools": []},
            {"name": "general-task", "enabled": True, "allowed_tools": []},
        ],
        "agentDocs": {
            "task": {"soul": {"text": ""}, "agents": {"text": ""}, "limits": {}},
            "news": {"soul": {"text": ""}, "agents": {"text": ""}, "limits": {}},
        },
        "mdl": {"models": [{"id": "m1", "name": "模型一", "endpoint": "ep1"}], "endpoints": [{"id": "ep1", "name": "端点"}]},
        "groups": [],
        "agentNew": False,
        "agentEdit": "task",
        "agentFishEdit": None,
        "agentError": "",
    }
    script = (
        'import assert from "node:assert/strict";\n'
        f"const state = {json.dumps(state, ensure_ascii=False)};\n"
        r"""
const esc = (s) => String(s ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const tip = () => "", toast = () => {}, SVG = { trash: "" }, $ = () => null, ico = () => "", dayWord = () => "今天";
const loading = () => "", avatarBlock = () => "", agentFish = () => "", agentSeed = () => "", fishSvg = () => "";
const fishTraits = () => ({}), parseCustom = () => null, customSeed = () => "";
const FISH_SPECIES = {}, FISH_EYES = {}, FISH_COLORS = [], FISH_COLOR_NAMES = [], FISH_BASE_RE = /x/;
const api = async () => ({}), repaintSheet = () => {}, EFFORT_NAMES = {};
const document = { addEventListener: () => {}, querySelector: () => null, querySelectorAll: () => [] };
"""
        + src
        + r"""
const taskHtml = profileForm(state.agents.profiles[0]);
assert.match(taskHtml, /value="research-report" checked/);
assert.match(taskHtml, /value="general-task" checked/);

const newsHtml = profileForm(state.agents.profiles[1]);
assert.doesNotMatch(newsHtml, /value="research-report" checked/);

assert.equal(
  resolveSkillsSelection("task", [
    { value: "research-report", checked: true },
    { value: "general-task", checked: true },
  ]),
  null,
);
assert.equal(
  resolveSkillsSelection("c_abc123", [
    { value: "research-report", checked: true },
    { value: "general-task", checked: true },
  ]),
  null,
);
assert.deepEqual(
  resolveSkillsSelection("task", [
    { value: "research-report", checked: true },
    { value: "general-task", checked: false },
  ]),
  ["research-report"],
);
assert.deepEqual(
  resolveSkillsSelection("news", [
    { value: "research-report", checked: true },
    { value: "general-task", checked: true },
  ]),
  ["research-report", "general-task"],
);
"""
    )
    res = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr


async def test_research_framework_only_mentions_research_report_skill_when_agent_can_read_it(
    tmp_path: Path,
):
    """调研活提示词只在当前执行岗位真被允许读 research-report 时才点名 read_skill('research-report')。"""
    from CharTyr_MaiWork.maiwork.agents import Agents
    from CharTyr_MaiWork.maiwork.coordinator import Coordinator
    from CharTyr_MaiWork.maiwork.skills import Skills
    from CharTyr_MaiWork.maiwork.skills_tools import register_skill_tools
    from CharTyr_MaiWork.maiwork.specialists import Specialists

    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        skills = Skills(tmp_path / "skills", store=store, settings=settings)
        register_skill_tools(tools, skills)
        agents = Agents(store, lambda: settings)
        agents.update_profile("news", {"skills": ["news-standard"]})

        captured_extra: list[tuple[str, str]] = []

        class _SpyWorkers(ce.FakeWorkers):
            async def run(self, brief, **kw):
                full_prompt = f"{brief}\n{kw.get('system_extra') or ''}"
                captured_extra.append((str(kw.get("agent_type") or ""), full_prompt))
                return WorkerReport(ok=True, summary="已查完", evidence=[])

        workers = _SpyWorkers()
        sp = Specialists(agents, workers, skills, tools=tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        coord = ce._build(
            mem_store=store,
            settings=settings,
            env=env,
            tools=tools,
            tasks=tasks,
            goals=goals,
            models=ce.ModelsQueue(),
            workers=workers,
            railway=None,
        )
        coord._specialists = sp  # noqa: SLF001

        # 1) 派给 news 专岗（白名单里没有 research-report）：不许空口点名 research-report
        await coord._run_job(  # noqa: SLF001
            brief="查一下近期动态", tools=["web_search", "fetch_page", "read_skill"],
            gid=ce.GID, tid="T-1", job_idx=1, ws_name="ws-1",
            job_type="research", agent="news", deliver_kind="view",
        )
        # 2) 派给 task 通才（能读 research-report）：正常点名 research-report
        await coord._run_job(  # noqa: SLF001
            brief="查一下近期动态", tools=["web_search", "fetch_page", "read_skill"],
            gid=ce.GID, tid="T-2", job_idx=1, ws_name="ws-1",
            job_type="research", agent="task", deliver_kind="view",
        )

        assert len(captured_extra) == 2
        assert "research-report" not in captured_extra[0][1]
        assert "research-report" in captured_extra[1][1]
    finally:
        store.close()


async def test_lookup_blocked_guidance_and_first_round_waiting_input(tmp_path: Path):
    """搜图/查出处等开放查证活：多渠道真调工具均无收录时，验收引导标 blocked 并首轮转 waiting_input 问发起人要线索。"""
    from CharTyr_MaiWork.maiwork.coordinator import Coordinator

    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    try:
        settings = _make_settings(tmp_path / "ws", railway=False)
        env = LocalEnv(lambda: settings)
        tools = Tools(store)
        _register_research_stubs(tools)
        tasks = Tasks(store, lambda: settings)
        goals = Goals(store, lambda: settings)
        outbox = ce.FakeOutbox()
        tid = ce._create_task(tasks, req="帮我查一下这张梗图最早出自哪条原帖")
        await env.write_file(tasks.get(tid)["workspace"], f"artifacts/{tid}/reply.md", "试了多处搜图暂未找到原帖。")

        # 子 agent 真调了多个搜索/核查工具
        _insert_tool_call(store, tid=tid, tool="web_search", inp='{"query": "梗图 台词 原帖"}')
        _insert_tool_call(store, tid=tid, tool="fetch_page", inp="https://forum.example/thread/1")

        plan_json = json.dumps(
            {
                "requirements": [{"text": "找到这张梗图的最早原帖链接", "origin": "原话", "kind": "文稿"}],
                "criteria": ["找到这张梗图的最早原帖链接"],
                "scale": "brief",
                "deliver_kind": "text",
                "env": "local",
                "jobs": [
                    {
                        "brief": f"检索原帖出处写进 artifacts/{tid}/reply.md",
                        "type": "research",
                        "tools": ["web_search", "fetch_page", "write_file"],
                        "covers": ["R1"],
                    }
                ],
                "question": None,
            },
            ensure_ascii=False,
        )
        review_json = json.dumps(
            {
                "pass": False,
                "review": "没过：公开搜图与关键词检索均未收录该原帖，需发起人补充线索。",
                "missing": ["原帖出处"],
                "artifact": f"artifacts/{tid}/reply.md",
                "note": "试了几个搜图渠道都没收录这张原帖",
                "items": [
                    {
                        "id": "R1",
                        "met": False,
                        "blocked": "公开索引与搜图渠道均未收录该图原帖，缺平台或上下文线索",
                        "evidence": "已调 web_search 多组关键词并核查论坛候选页，均无原帖记录",
                    },
                    {
                        "id": "R0",
                        "met": True,
                        "evidence": "如实说明已查渠道，未编造出处",
                    },
                ],
            },
            ensure_ascii=False,
        )
        models = ce.ModelsQueue(replies=[plan_json, review_json])
        workers = ce.FakeWorkers(
            reports=[WorkerReport(ok=True, summary="已查多渠道未见收录", evidence=["web_search + fetch_page"])]
        )
        coord = ce._build(
            mem_store=store,
            settings=settings,
            env=env,
            tools=tools,
            tasks=tasks,
            goals=goals,
            models=models,
            workers=workers,
            outbox=outbox,
            railway=None,
        )
        await coord.run_task(tid)

        review_prompt = "\n".join(str(m.get("content") or "") for m in models.calls[1][1])
        assert "搜图" in review_prompt and "未收录" in review_prompt and "补充" in review_prompt
        assert tasks.get(tid)["status"] == "waiting_input"
        assert "线索" in str(tasks.get(tid)["question"] or "")
        assert any(f"ask:{tid}:" in str(e.get("key") or "") for e in outbox.enqueued)
    finally:
        store.close()
