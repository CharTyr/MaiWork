"""验收引用统计的两个已核实缺口（2026-10 本地修复，未部署）。

缺口 1：`_link_check` 以前对 summary / evidence / 每个成品文件**分别** `extract_http_links`
再 `extend`——单个文本内部去重了，跨文本重复出现的同一个链接却全部计数（线上 T8 历史记
「66 条链接 / 39 条没打开过」，按规范化唯一算实际是 41 条 / 28 条）。现在跨文本按
`normalize_link_for_check` 统一去重，保留首见的那份展示 URL；计数（links / unopened）
按唯一 URL 算，结构键 `{links, unopened, unopened_urls}` 不变。

缺口 2：`_UNOPENED_URLS_IN_PROMPT`=10 只限制清单条数，`_review` 提示以前不告诉模型总数，
模型误以为只有列出来的少数几条。现在提示里明确写：本次交付唯一引用总数、已打开数、
没打开总数，以及「只列了前 N 条、还有多少没列」；即便一条没打开也给摘要（不强制全列）。

边界（这轮**不做**的）：只修喂料与统计，不动 `plan["research"]` 开关和验收协议，不加任何
「没打开比例超过多少就打回」的硬阈值，一条没打开不等于没过；语义验收仍不保证。运行期
默认不把完整链接清单写日志（这里只断言提示文本和结构化结果）。不同 query 的链接按已有
`normalize_link_for_check` 规则算不同链接，不许人为合并。
"""

from __future__ import annotations

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
from test_review_link_check import (  # noqa: F401
    _log_tool_call,
    _review_prompt,
    _write_artifact_hook,
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


def _research_plan(deliver_kind: str = "text") -> str:
    jobs = [{"brief": "调研一下大家怎么看这个方案", "type": "research", "tools": ["fetch_page"]}]
    return _plan(jobs=jobs, deliver_kind=deliver_kind)


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


def _write_files(env, tasks, tid: str, files: dict[str, str]) -> None:
    """直接往工作区成品目录写文本文件（不起 worker 钩子）。"""
    ws = env.workspace(tasks.get(tid)["workspace"])
    d = ws / "artifacts" / tid
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (d / name).write_bytes(content.encode("utf-8"))


def _listing(tid: str, names: list[str]) -> list[dict]:
    return [
        {"path": f"artifacts/{tid}/{name}", "is_dir": False, "size": 10} for name in names
    ]


# ---------------------------------------------------------------------------
# 缺口 1：跨文本按规范化去重 + 首见展示 URL
# ---------------------------------------------------------------------------


async def test_link_check_dedupes_across_texts_and_keeps_first_display_url(
    mem_store, settings, env, tools, tasks, goals
):
    """同一个链接以三种写法（首见带 utm/fragment、末尾斜杠、裸地址）散在
    summary / evidence / 成品文件里 → 只算 1 条唯一链接，清单里保留首见的展示 URL。"""
    tid = _create_task(tasks)
    _write_files(env, tasks, tid, {"report.md": "两份都引了 https://dup.example/p\n"})
    models = ModelsQueue(replies=[])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=FakeWorkers())

    out = await coord._link_check(
        tid=tid,
        ws_name=tasks.get(tid)["workspace"],
        listing=_listing(tid, ["report.md"]),
        summary="小结：https://dup.example/p?utm_source=x#frag 讲得挺细",
        evidence=["原始出处：https://dup.example/p/"],
    )

    assert out["links"] == 1
    assert out["unopened"] == 1
    # 首见（summary）那份展示 URL 原样保留，不是被后见写法顶掉、也不重写
    assert out["unopened_urls"] == ["https://dup.example/p?utm_source=x#frag"]


async def test_link_check_dedupes_slash_tracking_and_query_encoding(
    mem_store, settings, env, tools, tasks, goals
):
    """末斜杠 / utm_* / fragment / 同一 query 的 %20 与 + 写法，按已有规范化规则算同一个链接。"""
    tid = _create_task(tasks)
    _write_files(
        env, tasks, tid,
        {"a.md": "https://enc.example/x/?utm_source=q#f\n", "b.md": "https://enc.example/x?a=%20b\n"},
    )
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(replies=[]),
                   workers=FakeWorkers())

    out = await coord._link_check(
        tid=tid,
        ws_name=tasks.get(tid)["workspace"],
        listing=_listing(tid, ["a.md", "b.md"]),
        summary="https://enc.example/x?a=+b",
        evidence=[],
    )

    assert out["links"] == 2        # enc.example/x（末斜杠+utm）算 1 条；?a=… 算另一条
    assert out["unopened"] == 2


async def test_link_check_keeps_distinct_query_links(mem_store, settings, env, tools, tasks, goals):
    """query 值不同的链接是不同链接：不许为了「去重」把它们合并或删掉。"""
    tid = _create_task(tasks)
    _write_files(env, tasks, tid, {"q.md": "https://q.example/1?a=1\nhttps://q.example/1?a=2\n"})
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(replies=[]),
                   workers=FakeWorkers())

    out = await coord._link_check(
        tid=tid,
        ws_name=tasks.get(tid)["workspace"],
        listing=_listing(tid, ["q.md"]),
        summary="https://q.example/1?a=1",
        evidence=[],
    )

    assert out["links"] == 2
    assert sorted(out["unopened_urls"]) == ["https://q.example/1?a=1", "https://q.example/1?a=2"]


async def test_link_check_duplicate_of_opened_link_is_not_unopened(
    mem_store, settings, env, tools, tasks, goals
):
    """重复出现的那个链接只要打开过，去重后不算没打开（T8 误判的同类）。"""
    tid = _create_task(tasks)
    _write_files(env, tasks, tid, {"r.md": "https://seen.example/a/ 和 https://seen.example/a\n"})
    _log_tool_call(mem_store, tid=tid, tool="fetch_page",
                   inp="HTTPS://Seen.Example/a/?utm_source=z#top", ok=True)
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=ModelsQueue(replies=[]),
                   workers=FakeWorkers())

    out = await coord._link_check(
        tid=tid,
        ws_name=tasks.get(tid)["workspace"],
        listing=_listing(tid, ["r.md"]),
        summary="https://seen.example/a",
        evidence=["https://seen.example/a/"],
    )

    assert out["links"] == 1
    assert out["unopened"] == 0
    assert out["unopened_urls"] == []


# ---------------------------------------------------------------------------
# 缺口 2：验收提示给全量计数（唯一链接 / 已打开 / 没打开总数 / 未列条数）
# ---------------------------------------------------------------------------


async def test_review_prompt_states_unique_totals_across_files(
    mem_store, settings, env, tools, tasks, goals
):
    """跨 summary / evidence / 成品重复的链接：留痕计数和提示计数都按唯一链接算。"""
    tid = _create_task(tasks)
    workers = FakeWorkers(reports=[WorkerReport(
        ok=True,
        summary="调研完了：https://dup.example/p?utm_source=x",
        evidence=["出处 https://dup.example/p/"],
    )])
    workers.before_return = _write_artifact_hook(
        env, tasks, tid, {"report.md": "https://dup.example/p\nhttps://other.example/q\n"}
    )
    _log_tool_call(mem_store, tid=tid, tool="fetch_page",
                   inp="HTTPS://Other.Example/q/#f", ok=True)
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    prompt = _review_prompt(models)
    assert "共引用 2 条链接" in prompt
    assert "已打开 1 条" in prompt
    assert "没打开过 1 条" in prompt
    assert "没有真正打开过" in prompt            # 兼容旧的「没打开」事实段口径
    assert prompt.count("- https://dup.example/p") == 1   # 「没打开」清单里只列一次
    assert "other.example" not in prompt          # 打开过的不进清单

    saved = mem_store.kv_get(f"task.link_check.{tid}")
    assert saved["links"] == 2 and saved["unopened"] == 1
    assert "引用核对：2 条链接，1 条没打开过" in str(tasks.get(tid)["review"])


async def test_review_prompt_states_total_when_more_than_prompt_cap(
    mem_store, settings, env, tools, tasks, goals
):
    """没打开过 13 条（> _UNOPENED_URLS_IN_PROMPT=10）：提示给全量 13，明说只列前 10、还有 3 条没列。"""
    tid = _create_task(tasks)
    links = "".join(f"https://many.example/n{i}\n" for i in range(1, 14))
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    workers.before_return = _write_artifact_hook(env, tasks, tid, {"report.md": links})
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers)

    await coord.run_task(tid)

    prompt = _review_prompt(models)
    assert "共引用 13 条链接" in prompt
    assert "已打开 0 条" in prompt
    assert "没打开过 13 条" in prompt
    assert "还有 3 条没列出来" in prompt
    assert prompt.count("https://many.example/") == 10   # 清单只列前 10 条

    saved = mem_store.kv_get(f"task.link_check.{tid}")
    assert saved["links"] == 13 and saved["unopened"] == 13
    assert len(saved["unopened_urls"]) == 10


async def test_review_prompt_shows_summary_when_nothing_unopened(
    mem_store, settings, env, tools, tasks, goals
):
    """一条没打开：也给唯一链接总数 + 已打开数的摘要，但不强制列全链接。"""
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
    assert "共引用 1 条链接" in prompt
    assert "已打开 1 条" in prompt
    assert "没打开过 0 条" in prompt
    assert "没有真正打开过" not in prompt   # 旧口径：全打开时不加「没打开」事实段
    assert "a.example" not in prompt        # 不强制列已打开链接


async def test_review_few_unopened_links_can_still_pass(
    mem_store, settings, env, tools, tasks, goals
):
    """少量、非关键结论的没打开链接：模型判 pass 就 pass，不设任何比例阈值。"""
    tid = _create_task(tasks)
    workers = FakeWorkers(reports=[WorkerReport(ok=True, summary="调研完了", evidence=[])])
    workers.before_return = _write_artifact_hook(
        env, tasks, tid, {"report.md": "背景参考（非关键）：https://aside.example/note\n"}
    )
    models = ModelsQueue(replies=[_research_plan(), _review(pass_=True, artifact="", review="过了")])
    outbox = FakeOutbox()
    coord = _coord(mem_store=mem_store, settings=settings, env=env, tools=tools,
                   tasks=tasks, goals=goals, models=models, workers=workers, outbox=outbox)

    await coord.run_task(tid)

    assert tasks.get(tid)["status"] == "completed"
    assert [e for e in outbox.enqueued if e["task_id"] == tid and e["kind"] == "text"]
    assert "引用核对：1 条链接，1 条没打开过" in str(tasks.get(tid)["review"])
    prompt = _review_prompt(models)
    assert "没有「没打开过的比例超过多少就必须打回」的硬线" in prompt
