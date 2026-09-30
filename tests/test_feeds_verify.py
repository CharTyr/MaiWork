"""feeds.py「railway.new 一次性 VM 实测资讯」测试（docs/07-代码接口.md §10.4、docs/09）。

覆盖：
- 主模型没挑中（返回 verify=null / 空 / 没 verify 键）→ 根本不申请 VM，照常入库；
- 拿不到 VM（verify_runner 什么都不做就回来）→ 照常入库，verify 列为空；
- passed / failed 都写进 news_items.verify（前端如实显示 verify 块）；
- 每轮最多 [environments] verify_per_round（默认 2）条，挑多了夹回；
- [environments] railway=false → 整个实测关掉（不挑、不调 runner）；
- [environments] verify_enabled=false（默认）→ 整个挑实测跳过：不调模型挑条、不申请 VM；
- 实测总时长上限：run_railway_verify 到钟就收尾、没测的条绝不留半截 verify；
- verify 字段最终结构：{"status","summary","steps"(≤6 条每条≤80字),"minutes","ts"}。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.feeds import Feeds, run_railway_verify
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.workers import WorkerReport

from fakes import PICK_FALLBACK_REPLY, FakeModelsQueue, FakeProfiles, focus_reply, two_phase_workers_run

BJ = timezone(timedelta(hours=8))
NOW = 1_790_000_000.0
GID = "111"

_QUOTE = "原文里确实写着这件事，摘要能在正文找到依据。"


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    """本文件的候选发布时间都围着 NOW 造；不钉住时钟的话，真实时间一过 NOW+7 天，
    「资讯超过 7 天算旧闻」的硬规则就把它们拒掉（2026-09-28 12:13 UTC 实际踩到）。
    个别用例自己再 monkeypatch clock.now 的，以它的为准（后设覆盖前设）。"""
    monkeypatch.setattr(clock, "now", lambda: NOW)

_WORKER_ITEMS = {
    "items": [
        {"title": "一行命令把 JSON 转成 CSV 的新工具", "url": "https://tools.example.com/j2c",
         "summary": "一个开源命令行小工具。装完就能用。", "kind": "news",
         "published": NOW - 3600, "fetched": True, "quote": _QUOTE, "paywall": False},
        {"title": "新的硬件板子发布", "url": "https://news.example.com/board",
         "summary": "一块新板子。配置不错。", "kind": "news",
         "published": NOW - 7200, "fetched": True, "quote": _QUOTE, "paywall": False},
    ]
}

_SCORES_JSON = json.dumps(
    {
        "scores": [
            {"i": 0, "title": "一行命令把 JSON 转成 CSV 的新工具", "info": 4, "source": 4, "relevance": 4,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "工具", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "群友喜欢顺手的命令行工具", "icon": "tools"},
            {"i": 1, "title": "新的硬件板子发布", "info": 4, "source": 4, "relevance": 4,
             "timeliness": 4, "chat": 4, "profile": 0, "topic": "硬件", "sensitive": False,
             "grounded": True, "junk": False, "junk_reason": "", "same_as_recent": False,
             "why": "群里在做硬件", "icon": "rocket"},
        ]
    },
    ensure_ascii=False,
)

# 定关注点要求 3–5 个（少了会触发追问重试），统一用 fakes.focus_reply 造
_FOCUS_JSON = focus_reply("顺手的开源小工具", "本地大模型新玩法", "开源掌机社区风向")

_VERIFY_PICK_JSON = json.dumps(
    {"verify": [{"i": 0, "title": "一行命令把 JSON 转成 CSV 的新工具",
                 "what": "装上跑示例，看能不能真把 JSON 转成 CSV",
                 "expect": "示例跑通，输出 CSV"}]},
    ensure_ascii=False,
)


class FakeWorkers:
    """假 workers.run：索引第几次调用，按队列回 WorkerReport。

    两阶段恒生效：feeds-discover 时弹一条（这条当作「老路收上来的候选」记住，
    种进撒网登记簿）；feeds-verify 时按 brief 里列出的链接交回这份的子集。
    """

    def __init__(self, reports: list[Any] | None = None, default: Any = None) -> None:
        self.reports = list(reports or [])
        self.default = default
        self.calls: list[dict] = []
        self._collect_report: Any = None

    async def run(self, brief: str, **kwargs: Any) -> Any:
        self.calls.append({"brief": str(brief), **kwargs})
        task_id = str(kwargs.get("task_id") or "")
        if task_id.startswith(("feeds-discover:", "feeds-verify:")):
            if task_id.startswith("feeds-discover:"):
                r = self.reports.pop(0) if self.reports else self.default
                if isinstance(r, BaseException):
                    raise r
                self._collect_report = r
            return await two_phase_workers_run(self._collect_report, brief, kwargs)
        if self.reports:
            r = self.reports.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        return self.default


def _worker_report_items() -> WorkerReport:
    return WorkerReport(ok=True, summary="找好了", data=_WORKER_ITEMS, evidence=[], steps=3)


class FakeTopics:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add_candidate(self, group_id: str, **kw: Any) -> None:
        self.calls.append({"group_id": group_id, **kw})


class RecordingVerifyRunner:
    """假实测执行：记录被叫；可预设「给这些 index 写 verify dict」。"""

    def __init__(self, results: dict[int, dict] | None = None) -> None:
        self.calls: list[dict] = []
        self.results = dict(results or {})

    async def __call__(self, items: list[dict], plans: list[dict], gid: str, settings: Any) -> None:
        self.calls.append({"items": items, "plans": plans, "gid": gid})
        for plan in plans:
            idx = int(plan.get("index", -1))
            if idx in self.results:
                items[idx]["verify"] = dict(self.results[idx])


def _settings(cfg: dict | None = None) -> Any:
    settings, _ = load_settings(cfg or {})
    return settings


def _seed_group(store: Store, gid: str = GID) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, profile_ready_ts) VALUES (?, ?)",
            (gid, 1_700_000_000.0),
        )


def _make_feeds(
    tmp_path: Path,
    *,
    models: Any,
    workers: Any,
    verify_runner: Any = None,
    cfg: dict | None = None,
) -> tuple:
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed_group(store, GID)
    settings = _settings(cfg)
    profiles = FakeProfiles()
    profiles.entries_map[GID] = [{"category": "interest", "text": "喜欢顺手的开源工具"}]
    topics = FakeTopics()
    feeds = Feeds(store, models, workers, profiles, topics, lambda: settings,
                  search=None, verify_runner=verify_runner)
    return store, settings, feeds


def _accepted_verify(tmp_path: Path, store: Store) -> list[dict]:
    rows = store.read().execute(
        "SELECT title, verify FROM news_items WHERE rejected=0 ORDER BY id"
    ).fetchall()
    return [{"title": str(r["title"]), "verify": str(r["verify"] or "")} for r in rows]


# ----------------------------------------------------------------------
# 挑条 + verify_runner 注入路径（假 runner）
# ----------------------------------------------------------------------


def test_no_pick_means_no_vm_and_normal_insert(tmp_path: Path) -> None:
    """主模型说没有值得实测的（verify 键缺失/null）→ 不申请 VM，照常入库。"""
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON,
        '{"posts": []}',
        '{"verify": null}',
    ])
    runner = RecordingVerifyRunner()
    store, settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    assert runner.calls == []  # 没挑中：一次都没申请
    for row in _accepted_verify(tmp_path, store):
        assert row["verify"] == ""  # verify 列空着


def test_railway_off_means_whole_verify_disabled(tmp_path: Path) -> None:
    """[environments] railway=false → 实测整个关掉：不挑条、不调 runner、甚至不多调一次模型。"""
    models = FakeModelsQueue(ready=True, replies=[_FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}'])
    runner = RecordingVerifyRunner({0: {"status": "passed"}})
    store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"railway": False, "verify_enabled": True}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    assert runner.calls == []
    # 主模型被问 4 次（focus/挑候选/score/post），没有第 5 次挑实测的调用
    assert len(models.calls) == 4
    for row in _accepted_verify(tmp_path, store):
        assert row["verify"] == ""


def test_verify_enabled_off_skips_pick_and_runner(tmp_path: Path) -> None:
    """[environments] verify_enabled=false（默认）→ 整个挑实测跳过：
    不调模型挑条、不调 runner、不多一次模型调用；出资讯照常。"""
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', _VERIFY_PICK_JSON,
    ])
    runner = RecordingVerifyRunner({0: {"status": "passed"}})
    store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": False}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    assert runner.calls == []  # 一次都没申请 VM
    # 主模型被问 4 次（focus/挑候选/score/post），没有第 5 次挑实测的调用
    assert len(models.calls) == 4
    for row in _accepted_verify(tmp_path, store):
        assert row["verify"] == ""


def test_verify_enabled_on_keeps_existing_behavior(tmp_path: Path) -> None:
    """[environments] verify_enabled=true → 老实测：先调模型挑条，再交给 runner。"""
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', _VERIFY_PICK_JSON,
    ])
    runner = RecordingVerifyRunner({0: {"status": "passed"}})
    store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    assert len(models.calls) == 5  # 第 2 次是挑候选、第 5 次是挑实测
    assert len(runner.calls) == 1
    assert [int(p["index"]) for p in runner.calls[0]["plans"]] == [0]
    rows = _accepted_verify(tmp_path, store)
    assert json.loads(rows[0]["verify"])["status"] == "passed"


def test_vm_unavailable_still_inserts_normally(tmp_path: Path) -> None:
    """runner 表示拿不到 VM（什么都不写就回来）→ 照常入库、返回入选数不变。"""
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', _VERIFY_PICK_JSON,
    ])
    runner = RecordingVerifyRunner()  # 不写任何 verify = 没拿到机器
    store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    assert len(runner.calls) == 1  # 确实去尝试申请了（只是没拿到）
    for row in _accepted_verify(tmp_path, store):
        assert row["verify"] == ""


def test_passed_and_failed_both_written_to_verify(tmp_path: Path) -> None:
    """passed / failed 都写进 news_items.verify（前端如实显示），字段结构齐。"""
    v_pass = {"status": "passed", "summary": "装上跑通了", "steps": ["装包", "跑示例"],
              "minutes": 3, "ts": NOW}
    v_fail = {"status": "failed", "summary": "装了跑不起来", "steps": ["装包失败"],
              "minutes": 2, "ts": NOW}
    pick_two = json.dumps(
        {"verify": [
            {"i": 0, "title": "一行命令把 JSON 转成 CSV 的新工具", "what": "a", "expect": "b"},
            {"i": 1, "title": "新的硬件板子发布", "what": "c", "expect": "d"},
        ]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', pick_two,
    ])
    runner = RecordingVerifyRunner({0: v_pass, 1: v_fail})
    store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    rows = _accepted_verify(tmp_path, store)
    assert len(rows) == 2
    v0 = json.loads(rows[0]["verify"])
    v1 = json.loads(rows[1]["verify"])
    assert v0["status"] == "passed"
    assert set(v0.keys()) == {"status", "summary", "steps", "minutes", "ts"}
    assert v1["status"] == "failed"
    # 测不通照样上网页（rejected=0 的行就有这两条）
    batch = store.read().execute("SELECT kept FROM news_batches").fetchone()
    assert int(batch["kept"]) == 2


def test_per_round_cap_is_two(tmp_path: Path) -> None:
    """主模型挑了 3 条，夹回 verify_per_round（默认 2）。"""
    pick_three = json.dumps(
        {"verify": [
            {"i": 0, "title": "一行命令把 JSON 转成 CSV 的新工具", "what": "a", "expect": "b"},
            {"i": 1, "title": "新的硬件板子发布", "what": "c", "expect": "d"},
            {"i": 0, "title": "重复挑", "what": "x", "expect": "y"},
        ]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', pick_three,
    ])
    runner = RecordingVerifyRunner()
    _store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    asyncio.run(feeds.prepare_news(GID))
    assert len(runner.calls) == 1
    plans = runner.calls[0]["plans"]
    assert len(plans) == 2  # 默认每轮最多 2 条（且同一条不重复挑）


def test_pick_out_of_range_dropped(tmp_path: Path) -> None:
    """模型挑了不存在的编号 → 丢掉，剩下合法的那条才进 runner。"""
    pick_bad = json.dumps(
        {"verify": [
            {"i": 9, "title": "不存在", "what": "a", "expect": "b"},
            {"i": 1, "title": "新的硬件板子发布", "what": "c", "expect": "d"},
        ]},
        ensure_ascii=False,
    )
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', pick_bad,
    ])
    runner = RecordingVerifyRunner()
    _store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    asyncio.run(feeds.prepare_news(GID))
    assert len(runner.calls) == 1
    plans = runner.calls[0]["plans"]
    assert [int(p["index"]) for p in plans] == [1]


# ----------------------------------------------------------------------
# run_railway_verify：真实编排，假 env / 假 workers
# ----------------------------------------------------------------------


class _FakeRailEnv:
    """假 RailwayEnv：acquire 回（或不回）一台 Box；release 记录。"""

    def __init__(self, give_box: bool = True) -> None:
        self.give_box = give_box
        self.release_calls = 0

    async def acquire(self, job_id: str) -> Any:
        if not self.give_box:
            return None
        from CharTyr_MaiWork.maiwork.environments.railway import Box

        return Box(job_id=str(job_id), key_path=Path("/tmp/fake-id"),
                   expires_ts=NOW + 3600, preview_url="", key_dir=Path("/tmp"))

    async def release(self, box: Any) -> None:
        self.release_calls += 1


class _FakeVerifyWorkers:
    """假 Workers（run_railway_verify 用）：按队列回 report，记 brief。"""

    def __init__(self, reports: list[WorkerReport]) -> None:
        self.reports = list(reports)
        self.calls: list[dict] = []

    async def run(self, brief: str, **kwargs: Any) -> WorkerReport:
        self.calls.append({"brief": str(brief), **kwargs})
        if self.reports:
            return self.reports.pop(0)
        return WorkerReport(ok=False, summary="没回", error="空队列")


def _dummy_tools() -> Any:
    """装作「vm 工具已注册过」的 Tools（run_railway_verify 幂等跳过的路径）。"""

    class _T:
        def specs(self, role: str) -> list:
            return [
                {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
                for n in ("vm_run", "vm_put_file", "vm_read_file")
            ]

        def register(self, tool: Any) -> None:
            raise AssertionError("测试里不该真的注册")

    return _T()


def _plans_two() -> list[dict]:
    return [
        {"index": 0, "what": "a", "expect": "b"},
        {"index": 1, "what": "c", "expect": "d"},
    ]


def test_runner_acquire_fail_leaves_no_verify(tmp_path: Path) -> None:
    items = [{"title": "t1"}, {"title": "t2"}]
    env = _FakeRailEnv(give_box=False)
    workers = _FakeVerifyWorkers([])
    settings = _settings({})
    asyncio.run(
        run_railway_verify(items, _plans_two(), GID, settings,
                           workers=workers, tools=_dummy_tools(), env=env)
    )
    assert workers.calls == []  # 没机器就不派子 agent
    assert env.release_calls == 0
    assert all("verify" not in it for it in items)


def test_runner_passed_and_failed_normalized(tmp_path: Path) -> None:
    """子 agent 交回 passed/failed → 落成统一结构；超长截断。"""
    items = [{"title": "t1"}, {"title": "t2"}]
    env = _FakeRailEnv(give_box=True)
    reports = [
        WorkerReport(ok=True, summary="跑通了：" + "很" * 300,
                     data={"status": "passed", "summary": "跑通了：" + "很" * 300,
                           "steps": [f"第{i}步：" + "细" * 200 for i in range(10)]},
                     evidence=[], steps=4),
        WorkerReport(ok=True, summary="没跑通", data={"status": "failed", "summary": "装不上"},
                     evidence=[], steps=2),
    ]
    workers = _FakeVerifyWorkers(reports)
    settings = _settings({})
    asyncio.run(
        run_railway_verify(items, _plans_two(), GID, settings,
                           workers=workers, tools=_dummy_tools(), env=env)
    )
    v0 = items[0]["verify"]
    assert v0["status"] == "passed"
    assert len(v0["summary"]) <= 200
    assert len(v0["steps"]) <= 6  # ≤6 条
    assert all(len(s) <= 80 for s in v0["steps"])  # 每条 ≤80 字
    assert isinstance(v0["minutes"], (int, float)) and v0["minutes"] > 0
    assert isinstance(v0["ts"], float)
    v1 = items[1]["verify"]
    assert v1["status"] == "failed"
    # brief 里写清「只读/无副作用」的规矩 + 单条时长来自 verify_minutes
    brief = workers.calls[0]["brief"]
    assert "不注册账号" in brief and "不填任何密钥" in brief and "只读" in brief
    assert workers.calls[0]["tools"] == ["vm_run", "vm_put_file", "vm_read_file"]
    assert env.release_calls == 1  # 用完释放


def test_runner_report_not_ok_marks_failed(tmp_path: Path) -> None:
    """子 agent 没交回（ok=False）→ 照 failed 落。"""
    items = [{"title": "t1"}]
    env = _FakeRailEnv(give_box=True)
    workers = _FakeVerifyWorkers([WorkerReport(ok=False, summary="", error="步数用完")])
    settings = _settings({})
    asyncio.run(
        run_railway_verify(items, [ {"index": 0, "what": "a", "expect": "b"} ], GID, settings,
                           workers=workers, tools=_dummy_tools(), env=env)
    )
    assert items[0]["verify"]["status"] == "failed"


def test_runner_unknown_status_marks_failed(tmp_path: Path) -> None:
    items = [{"title": "t1"}]
    env = _FakeRailEnv(give_box=True)
    workers = _FakeVerifyWorkers([
        WorkerReport(ok=True, summary="x", data={"status": "weird"}, evidence=[], steps=1)
    ])
    settings = _settings({})
    asyncio.run(
        run_railway_verify(items, [ {"index": 0, "what": "a", "expect": "b"} ], GID, settings,
                           workers=workers, tools=_dummy_tools(), env=env)
    )
    assert items[0]["verify"]["status"] == "failed"


def test_runner_total_time_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """实测总时长上限 20 分钟：第一条把钟用完了，第二条直接不测、不留半截 verify。"""
    items = [{"title": "t1"}, {"title": "t2"}]
    env = _FakeRailEnv(give_box=True)
    reports = [
        WorkerReport(ok=True, summary="好",
                     data={"status": "passed", "summary": "好"}, evidence=[], steps=3),
        WorkerReport(ok=True, summary="不该被用", data={"status": "passed"}, evidence=[], steps=1),
    ]
    workers = _FakeVerifyWorkers(reports)
    settings = _settings({})

    real_now = {"t": NOW}

    def fake_now() -> float:
        # 每次查钟往后跳 8 分钟：第一条测完（8 分钟）还能放进预算，第二条直接超 20 分钟总预算
        real_now["t"] += 8 * 60
        return real_now["t"]

    monkeypatch.setattr(clock, "now", fake_now)
    asyncio.run(
        run_railway_verify(items, _plans_two(), GID, settings,
                           workers=workers, tools=_dummy_tools(), env=env)
    )
    assert len(workers.calls) == 1  # 总时长到顶：第二条根本没被派
    assert "verify" in items[0]  # 第一条测完有 verify
    assert "verify" not in items[1]  # 第二条绝不允许有（半截也不行）
    assert env.release_calls == 1  # 到钟也照样释放机器


def test_verify_full_chain_end_to_end(tmp_path: Path) -> None:
    """全链路：prepare_news（假 runner）里挑中 → runner 写 verify → 入库 → view 原样出。"""
    v = {"status": "passed", "summary": "装上跑通了", "steps": ["装", "跑"],
         "minutes": 4, "ts": NOW}
    models = FakeModelsQueue(ready=True, replies=[
        _FOCUS_JSON, PICK_FALLBACK_REPLY, _SCORES_JSON, '{"posts": []}', _VERIFY_PICK_JSON,
    ])
    runner = RecordingVerifyRunner({0: v})
    store, _settings, feeds = _make_feeds(
        tmp_path, models=models, workers=FakeWorkers([_worker_report_items()]),
        verify_runner=runner, cfg={"environments": {"verify_enabled": True}},
    )
    got = asyncio.run(feeds.prepare_news(GID))
    assert got == 2
    view = feeds.news_view(GID)
    assert view
    items = [it for b in view for it in b["items"]]
    hit = [it for it in items if it["verify"] is not None]
    assert len(hit) == 1
    assert hit[0]["verify"]["status"] == "passed"
    assert hit[0]["verify"]["summary"] == "装上跑通了"
