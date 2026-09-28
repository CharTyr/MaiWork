"""验收时核对引用（coordinator）+ 调研类任务的报告框架（workers/coordinator）。

1. 验收前用代码（不是模型）抽交付物文本里的 http(s) 链接：summary / evidence +
   工作区 artifacts/<任务>/ 下的文本成品（md/html/json…；PDF 等二进制跳过）；
   和本任务 fetch_page 成功过的 URL 比对（规范化：去 fragment、末尾 /、utm_* 等跟踪
   参数；http/https 视同；主机名大小写不敏感）。只在搜索结果里出现过、没打开过的
   算「没打开过」。
2. 有没打开过的链接 → 把清单作为事实喂给验收模型的 prompt；没有 / 不是调研类任务
   → 不加这段，行为不变。
3. 留痕：验收意见里带「引用核对：N 条链接，M 条没打开过」，结构化结果存
   kv["task.link_check.<任务ID>"]。
4. 调研类子任务（计划里 type=research，缺省按 brief 关键词兜底）：子 agent 的
   system 提示带报告框架（一句话结论 / 大家都同意的 / 有分歧的 / 吐槽最多的 /
   新冒头的 / 没人提的；每条带链接并标「多源」「单源」）。非调研任务不加。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_coordinator import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    NOW,
    FakeDelivery,
    FakeOutbox,
    FakeWorkers,
    ModelsQueue,
    _Profiles,
    _Settings,
    _build,
    _create_task,
    _plan,
    _review,
)

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tools
from CharTyr_MaiWork.maiwork.workers import WorkerReport

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures（本地定义，不依赖别的测试模块的 fixture 收集）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


@pytest.fixture
def mem_store(tmp_path: Path):
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    yield store
    store.close()


@pytest.fixture
def settings(tmp_path: Path):
    return _Settings(tmp_path / "workspaces")


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def tools(mem_store: Store) -> Tools:
    return Tools(mem_store)


@pytest.fixture
def tasks(mem_store: Store, settings) -> Tasks:
    return Tasks(mem_store, lambda: settings)


@pytest.fixture
def goals(mem_store: Store, settings) -> Goals:
    return Goals(mem_store, lambda: settings)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _research_plan(deliver_kind: str = "text", *, jobs=None) -> str:
    if jobs is None:
        jobs = [{"brief": "调研一下大家怎么看这个方案", "type": "research", "tools": ["fetch_page"]}]
    return _plan(jobs=jobs, deliver_kind=deliver_kind)


def _build_job_plan() -> str:
    return _plan(jobs=[{"brief": "做一页总结", "tools": ["write_file"]}], deliver_kind="text")


def _coord(*, mem_store, settings, env, tools, tasks, goals, models, workers,
           delivery=None, outbox=None) -> Coordinator:
    return _build(
        mem_store=mem_store,
        settings=settings,
        env=env,
        tools=tools,
        tasks=tasks,
        goals=goals,
        models=models,
        workers=workers,
        delivery=delivery or FakeDelivery(),
        outbox=outbox or FakeOutbox(),
    )


def _write_artifact_hook(env, tasks, tid: str, files: dict[str, bytes | str]):
    """worker 交回前在工作区里写出成品文件。"""

    async def _hook() -> None:
        ws = env.workspace(tasks.get(tid)["workspace"])
        d = ws / "artifacts" / tid
        d.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            (d / name).write_bytes(data)

    return _hook


def _log_tool_call(mem_store: Store, *, tid: str, tool: str, inp: str, ok: bool = True,
                   output: str = "ok", actor: str = "子 agent #1") -> None:
    with mem_store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
            (NOW, GID, tid, actor, tool, inp, output, 4, 1 if ok else 0),
        )


def _review_prompt(models: ModelsQueue) -> str:
    """把验收回合（purpose=coordinator.review）的 messages 拼起来（含最后一轮的最终 prompt）。"""
    parts: list[str] = []
    for _role, msgs, kw in models.calls:
        if str(kw.get("purpose") or "") != "coordinator.review":
            continue
        for m in msgs:
            content = m.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "\n".join(parts)


def _purpose_calls(models: ModelsQueue, purpose: str) -> list[list[dict]]:
    return [msgs for _role, msgs, kw in models.calls if str(kw.get("purpose") or "") == purpose]


# ---------------------------------------------------------------------------
# 计划：子任务类型 + research 标记
# ---------------------------------------------------------------------------


async def test_plan_marks_research_job_from_type(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_research_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["research"] is True
    assert plan["jobs"][0]["type"] == "research"


async def test_plan_falls_back_to_keywords_for_research(mem_store, settings, env, tools, tasks, goals):
    """计划没给 type 时按 brief 关键词兜底：盘点 / 大家怎么看 这类算调研。"""
    tid = _create_task(tasks)
    raw = json.dumps({
        "criteria": ["有对比结论"],
        "deliver_kind": "text",
        "jobs": [{"brief": "帮我盘点一下大家在用的工具", "tools": ["fetch_page"]}],
        "question": None,
    }, ensure_ascii=False)
    models = ModelsQueue(replies=[raw])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["research"] is True
    assert plan["jobs"][0]["type"] == "research"


async def test_plan_build_job_is_not_research(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_build_job_plan()])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())
    plan = await coord._plan(tasks.get(tid))
    assert plan["research"] is False
    assert plan["jobs"][0]["type"] != "research"


# ---------------------------------------------------------------------------
# 调研类子任务：system 提示带报告框架
# ---------------------------------------------------------------------------


async def test_research_job_worker_gets_report_framework(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="还行")])
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)

    assert workers.calls
    extra = str(workers.calls[0].get("system_extra") or "")
    assert "一句话结论" in extra
    assert "有分歧" in extra
    assert "没人提" in extra
    assert "多源" in extra and "单源" in extra
    assert "链接" in extra


async def test_build_job_worker_gets_no_report_framework(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_build_job_plan(), _review(pass_=True, artifact="", review="还行")])
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=[])])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)
    await coord.run_task(tid)

    assert workers.calls
    assert str(workers.calls[0].get("system_extra") or "") == ""


# ---------------------------------------------------------------------------
# 验收：引用核对
# ---------------------------------------------------------------------------


async def test_review_flags_unopened_link_with_facts(mem_store, settings, env, tools, tasks, goals):
    """交付 md 里引用了两个链接：一个真打开过（大小写/尾斜杠/utm 不同也算打开过），
    一个只在搜索结果里出现过 → 后者作为事实进验收 prompt，并留痕。"""
    tid = _create_task(tasks)
    report = (
        "# 调研\n"
        "- 打开过的：https://opened.example/a\n"
        "- 没打开的：https://never.example/b?utm_source=x#frag\n"
    )
    _log_tool_call(mem_store, tid=tid, tool="fetch_page",
                   inp="HTTPS://Opened.Example/a/?utm_source=zz#top", ok=True)
    _log_tool_call(mem_store, tid=tid, tool="web_search", inp="搜索：never",
                   output="1. 没打开的 https://never.example/b")
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    workers.before_return = _write_artifact_hook(env, tasks, tid, {"report.md": report})
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    prompt = _review_prompt(models)
    assert "没有真正打开过" in prompt
    assert "never.example/b" in prompt
    assert "opened.example" not in prompt  # 打开过的链接不进「没打开」清单

    task = tasks.get(tid)
    assert "引用核对：2 条链接，1 条没打开过" in str(task["review"])
    saved = mem_store.kv_get(f"task.link_check.{tid}")
    assert isinstance(saved, dict)
    assert saved["links"] == 2
    assert saved["unopened"] == 1
    assert saved["unopened_urls"] == ["https://never.example/b?utm_source=x#frag"]


async def test_review_no_fact_block_when_all_links_opened(mem_store, settings, env, tools, tasks, goals):
    tid = _create_task(tasks)
    _log_tool_call(mem_store, tid=tid, tool="fetch_page", inp="https://a.example/x/", ok=True)
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    workers.before_return = _write_artifact_hook(
        env, tasks, tid, {"report.html": '<a href="HTTPS://A.Example/x#f">链接</a>'}
    )
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    prompt = _review_prompt(models)
    assert "没有真正打开过" not in prompt  # 全打开过 → 不加这段
    assert "引用核对：1 条链接，0 条没打开过" in str(tasks.get(tid)["review"])
    saved = mem_store.kv_get(f"task.link_check.{tid}")
    assert saved["links"] == 1 and saved["unopened"] == 0


async def test_review_counts_redirect_final_url_as_opened(mem_store, settings, env, tools, tasks, goals):
    """请求的是短链、跳到长链，交付里引用长链 → 不算「没打开过」。

    真的走 fetch_page（MockTransport 跟随跳转）落 tool_calls；交付引用最终地址。
    """
    import httpx

    from CharTyr_MaiWork.maiwork.tools import ToolContext
    from CharTyr_MaiWork.maiwork.tools_builtin import register_builtin

    tid = _create_task(tasks)

    def handler(request):
        if request.url.path == "/short":
            return httpx.Response(302, headers={"location": "https://long.example/very/long?a=1"})
        return httpx.Response(
            200, text="<html><body>长链正文</body></html>", headers={"content-type": "text/html"}
        )

    register_builtin(
        tools,
        search=None,
        profiles=None,
        http_transport=httpx.MockTransport(handler),
        resolver=lambda host: ["93.184.216.34"],
    )

    async def _open_short() -> None:
        ctx = ToolContext(group_id=GID, task_id=tid, actor="子 agent #1", role="worker")
        r = await tools.call("fetch_page", {"url": "http://example.com/short"}, ctx)
        assert r.ok

    report = "看这个：https://long.example/very/long?a=1\n"
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    workers.before_return = _write_artifact_hook(env, tasks, tid, {"report.md": report})
    original_hook = workers.before_return

    async def _hook() -> None:  # 先真的打开短链（落 tool_calls），再写成品
        await _open_short()
        await original_hook()

    workers.before_return = _hook
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    assert "没有真正打开过" not in _review_prompt(models)
    saved = mem_store.kv_get(f"task.link_check.{tid}")
    assert saved["links"] == 1 and saved["unopened"] == 0
    assert "引用核对：1 条链接，0 条没打开过" in str(tasks.get(tid)["review"])


async def test_review_checks_evidence_links_too(mem_store, settings, env, tools, tasks, goals):
    """summary / evidence 里的链接一样要核对（不只扫成品文件）。"""
    tid = _create_task(tasks)
    workers = FakeWorkers(reports=[WorkerReport(
        ok=True, summary="看了几个来源", evidence=["https://never.example/evid"],
    )])
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    prompt = _review_prompt(models)
    assert "never.example/evid" in prompt
    assert "引用核对：1 条链接，1 条没打开过" in str(tasks.get(tid)["review"])


async def test_review_skips_binary_artifact(mem_store, settings, env, tools, tasks, goals):
    """PDF 等二进制成品不扫：里面的「链接」不算引用。"""
    tid = _create_task(tasks)
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    workers.before_return = _write_artifact_hook(
        env, tasks, tid, {"data.pdf": b"%PDF-1.4 https://never.example/hidden"}
    )
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    assert "没有真正打开过" not in _review_prompt(models)
    saved = mem_store.kv_get(f"task.link_check.{tid}")
    assert saved["links"] == 0 and saved["unopened"] == 0


async def test_non_research_task_has_no_link_check(mem_store, settings, env, tools, tasks, goals):
    """非调研类任务：不抽链接、不加事实、不留 link_check（行为不变）。"""
    tid = _create_task(tasks)
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="做完了", evidence=[])])
    workers.before_return = _write_artifact_hook(
        env, tasks, tid, {"index.html": '<html><a href="https://never.example/x">x</a></html>'}
    )
    models = ModelsQueue(replies=[_build_job_plan(), _review(pass_=True, artifact="", review="看着不错")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    assert "没有真正打开过" not in _review_prompt(models)
    assert "引用核对" not in str(tasks.get(tid)["review"])
    assert mem_store.kv_get(f"task.link_check.{tid}") is None


# ---------------------------------------------------------------------------
# 纯函数：链接抽取 / 规范化
# ---------------------------------------------------------------------------


async def test_normalize_link_for_check_rules():
    from CharTyr_MaiWork.maiwork.coordinator import normalize_link_for_check as norm

    # http/https 视同、主机名大小写不敏感、去末尾 /、去 fragment、去 utm_*
    assert norm("HTTPS://Example.COM/a/?utm_source=x&keep=1#frag") == norm("http://example.com/a?keep=1")
    assert norm("https://a.com/x?fbclid=q") == "a.com/x"
    assert norm("https://a.com/x/") == "a.com/x"
    assert norm("不是链接") == "" or "不是链接" in norm("不是链接")  # 不崩就行；真链接才走这个函数
    assert norm("ftp://a.com/x") == ""


async def test_extract_http_links_dedup_and_trim():
    from CharTyr_MaiWork.maiwork.coordinator import extract_http_links

    text = (
        "看这两个：https://a.example/x。还有 (https://a.example/x/?utm_source=q#f)，"
        "重复的不算；末尾有点号 https://b.example/y."
    )
    assert extract_http_links(text) == ["https://a.example/x", "https://b.example/y"]
    assert extract_http_links("没有链接") == []
