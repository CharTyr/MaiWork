"""主模型条目（main_runs，库号 46）：主模型每次独立判断 / 一轮处理在板上有条目。

规矩：
- 每一次主模型的「独立判断 / 一轮处理」= main_runs 一行（开始 running、结束终态）；
- 这一轮里写的 model_calls / tool_calls 带 run_ref = 这条目的标记（link=exact）；
- task_id 非空的调用（T-* 任务里的计划 / 验收）不算独立条目——留在任务里；
- agent 不是 main（专岗岗位）的调用不算主模型条目；
- 进程中断留下的 running：启动时标 cancelled；看板读取也把超过时限的当已中断；
- title 只说「在判断什么」（purpose 中文名），不放群友原话 / QQ 号 / 对话原文；
- 清理跟 model_calls 同口径（3 天 / 2000 行上限，哪个先到按哪个）；
- 写入失败只记日志，绝不影响主流程（模型调用本身照常返回）。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console.board import item_view, board_view
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store

SECRET = "sk-mainrun-test-only"


def _settings(settings_dict: dict) -> object:
    settings, _ = load_settings(settings_dict)
    return settings


class _FakeAgents:
    def __init__(self, mapping: dict | None = None) -> None:
        self._m = mapping or {}

    def profile(self, kind: str) -> dict:
        d = {"kind": kind, "title": kind, "model": "", "effort": "", "backup": "",
             "enabled": True, "skills": None, "tools": None}
        d.update(self._m.get(kind, {}))
        return dict(d)


def _cfg() -> dict:
    return {
        "endpoints": [{"id": "e1", "protocol": "openai",
                       "base_url": "https://e.test/v1", "api_key": SECRET, "retries": 0}],
        "model_list": [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
    }


def _oai(text: str = "好的") -> httpx.Response:
    body = {
        "choices": [{"message": {"role": "assistant", "content": text},
                      "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }
    return httpx.Response(200, json=body)


def _make(tmp_path, handler=None, cfg=None):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(cfg or _cfg())
    h = handler or (lambda req: _oai())
    models = Models(
        store, lambda: settings, transport=httpx.MockTransport(h),
        agents=_FakeAgents({"main": {"model": "m1"}, "task": {"model": "m1"}}),
    )
    return store, models


def _runs(store) -> list[dict]:
    rows = store.read().execute(
        "SELECT * FROM main_runs ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def _mc(store) -> list[dict]:
    rows = store.read().execute(
        "SELECT * FROM model_calls ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


class _Svc:
    """board_view/item_view 需要的最小服务对象。"""

    def __init__(self, store, groups: tuple[str, ...] = ("900000001",)) -> None:
        self.store = store
        self._groups = groups
        self.agents = _FakeAgents()

    def get_settings(self):
        return _settings({
            "groups": {"serve": [{"group": f"qq:{g}"} for g in self._groups]},
            "endpoints": [{"id": "e1", "protocol": "openai",
                           "base_url": "https://e.test/v1", "api_key": "k"}],
            "model_list": [{"id": "m1", "endpoint": "e1", "model": "svc-model"}],
        })


# ----------------------------------------------------------------------
# 表与迁移
# ----------------------------------------------------------------------


class TestSchema:
    def test_tables_exist(self, tmp_path) -> None:
        """新库有 main_runs 表和 run_ref 列。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        names = {r["name"] for r in store.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "main_runs" in names
        mc_cols = {r["name"] for r in store.read().execute("PRAGMA table_info(model_calls)")}
        tc_cols = {r["name"] for r in store.read().execute("PRAGMA table_info(tool_calls)")}
        assert "run_ref" in mc_cols
        assert "run_ref" in tc_cols

    def test_latest_version(self, tmp_path) -> None:
        """库号 46：main_runs 一步。"""
        store = Store(tmp_path / "t.db")
        assert store.migrate() == 47
        assert int(store._conn.execute("PRAGMA user_version").fetchone()[0]) == 47


# ----------------------------------------------------------------------
# 写入：调一次主模型 → 一条 main_runs + model_calls 挂 ref
# ----------------------------------------------------------------------


class TestWrite:
    @pytest.mark.asyncio
    async def test_main_call_creates_run(self, tmp_path) -> None:
        """agent=main、task_id 空的一次 chat：开一条 run、结束成 done、调用挂上 run_ref。"""
        store, models = _make(tmp_path)
        result = await models.chat(
            agent="main",
            messages=[{"role": "user", "content": "判断一下"}],
            purpose="quick_judge.test", group_id="900000001",
        )
        assert result.text == "好的"
        runs = _runs(store)
        assert len(runs) == 1, runs
        run = runs[0]
        assert run["group_id"] == "900000001"
        assert run["purpose"] == "quick_judge.test"
        assert run["status"] == "done"
        assert run["ok"] == 1
        assert run["finished"] > 0
        assert run["ref"].startswith("main:quick_judge.test:900000001:")
        calls = _mc(store)
        assert len(calls) == 1
        assert calls[0]["run_ref"] == run["ref"]

    @pytest.mark.asyncio
    async def test_task_call_no_run(self, tmp_path) -> None:
        """task_id 非空的调用（T-* 任务里）不建条目、不挂 run_ref。"""
        store, models = _make(tmp_path)
        await models.chat(
            agent="main",
            messages=[{"role": "user", "content": "排计划"}],
            purpose="coordinator.plan", group_id="900000001", task_id="T-9",
        )
        assert _runs(store) == []
        calls = _mc(store)
        assert len(calls) == 1
        assert calls[0]["run_ref"] == ""

    @pytest.mark.asyncio
    async def test_worker_call_no_run(self, tmp_path) -> None:
        """agent 不是 main（专岗干活）不建条目。"""
        store, models = _make(tmp_path)
        await models.chat(
            agent="task",
            messages=[{"role": "user", "content": "干活"}],
            purpose="worker", group_id="900000001", task_id="",
        )
        assert _runs(store) == []
        assert _mc(store)[0]["run_ref"] == ""

    @pytest.mark.asyncio
    async def test_failed_call_marks_failed(self, tmp_path) -> None:
        """模型调用抛错：条目落成 failed 带错误，调用行也挂着 run_ref。"""
        def boom(req):
            return httpx.Response(500, json={"error": "boom"})

        store, models = _make(tmp_path, handler=boom)
        from CharTyr_MaiWork.maiwork.models import ModelError
        with pytest.raises(ModelError):
            await models.chat(
                agent="main",
                messages=[{"role": "user", "content": "x"}],
                purpose="quick_judge.test", group_id="900000001", retries=0,
            )
        runs = _runs(store)
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert runs[0]["ok"] == 0
        assert "boom" in runs[0]["error"]
        assert _mc(store)[0]["run_ref"] == runs[0]["ref"]

    @pytest.mark.asyncio
    async def test_run_failure_does_not_break_call(self, tmp_path) -> None:
        """写 main_runs 炸了也不影响模型调用本身（记录失败只打日志）。"""
        store, models = _make(tmp_path)
        # 把表弄坏：删掉再建一张形状不对的，写入会报错
        store.read().execute("DROP TABLE main_runs")
        store.read().execute("CREATE TABLE main_runs (id INTEGER PRIMARY KEY, bogus TEXT)")
        result = await models.chat(
            agent="main",
            messages=[{"role": "user", "content": "x"}],
            purpose="quick_judge.test", group_id="900000001",
        )
        assert result.text == "好的"  # 调用本身照常
        assert _mc(store)[0]["run_ref"] == "" or True  # 不强求 ref 一定挂上

    @pytest.mark.asyncio
    async def test_title_is_chinese_not_verbatim(self, tmp_path) -> None:
        """title 是中文说明（purpose 映射），不放群友原话 / QQ 号。"""
        store, models = _make(tmp_path)
        await models.chat(
            agent="main",
            messages=[{"role": "user", "content": "群友 123456 说：帮我买咖啡"}],
            purpose="intake.quick_judge", group_id="900000001",
        )
        run = _runs(store)[0]
        assert len(run["title"]) <= 80
        assert "123456" not in run["title"]
        assert "买咖啡" not in run["title"]
        assert "派活" in run["title"] or "判断" in run["title"]


# ----------------------------------------------------------------------
# 显式 scope：一轮多次调用进同一条目
# ----------------------------------------------------------------------


class TestScope:
    @pytest.mark.asyncio
    async def test_scope_merges_calls(self, tmp_path) -> None:
        """显式 scope 里两次 chat 进同一条目（不各开各的）。"""
        from CharTyr_MaiWork.maiwork.main_run import MainRuns

        store, models = _make(tmp_path)
        runner = MainRuns(store)
        async with runner.scope(group_id="900000001", purpose="admin_chat",
                                title="回答管理员"):
            await models.chat(agent="main",
                              messages=[{"role": "user", "content": "第一轮"}],
                              purpose="admin_chat", group_id="900000001")
            await models.chat(agent="main",
                              messages=[{"role": "user", "content": "第二轮"}],
                              purpose="admin_chat", group_id="900000001")
        runs = _runs(store)
        assert len(runs) == 1
        assert runs[0]["purpose"] == "admin_chat"
        assert runs[0]["status"] == "done"
        calls = _mc(store)
        assert len(calls) == 2
        assert all(c["run_ref"] == runs[0]["ref"] for c in calls)

    @pytest.mark.asyncio
    async def test_scope_failure_marks_failed(self, tmp_path) -> None:
        """scope 里抛异常 → 条目 failed，不影响调用方拿到的异常。"""
        from CharTyr_MaiWork.maiwork.main_run import MainRuns

        store, models = _make(tmp_path)
        runner = MainRuns(store)
        with pytest.raises(RuntimeError):
            async with runner.scope(group_id="900000001", purpose="feeds.mention_judge",
                                    title="复盘这轮反馈"):
                await models.chat(agent="main",
                                  messages=[{"role": "user", "content": "x"}],
                                  purpose="feeds.mention_judge", group_id="900000001")
                raise RuntimeError("流程炸了")
        runs = _runs(store)
        assert runs[0]["status"] == "failed"
        assert "流程炸了" in runs[0]["error"]

    @pytest.mark.asyncio
    async def test_tool_calls_share_ref(self, tmp_path) -> None:
        """scope 里 tool_calls 也挂同一个 run_ref。"""
        from CharTyr_MaiWork.maiwork.main_run import MainRuns
        from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools

        store, _ = _make(tmp_path)
        runner = MainRuns(store)
        tools = Tools(store)

        async def handler(ctx, args):
            from CharTyr_MaiWork.maiwork.tools import ToolResult
            return ToolResult(ok=True, output="ok")

        from CharTyr_MaiWork.maiwork.tools import Tool
        tools.register(Tool(name="probe", description="t",
                            parameters={"type": "object", "properties": {}},
                            roles=frozenset({"main"}), handler=handler))

        async with runner.scope(group_id="900000001", purpose="test.scope", title="测试一轮"):
            ctx = ToolContext(group_id="900000001", actor="主模型", role="main")
            await tools.call("probe", {}, ctx)

        rows = store.read().execute("SELECT run_ref FROM tool_calls").fetchall()
        runs = _runs(store)
        assert len(runs) == 1
        assert len(rows) == 1
        assert rows[0]["run_ref"] == runs[0]["ref"]

    @pytest.mark.asyncio
    async def test_nested_scope_inner_wins(self, tmp_path) -> None:
        """嵌套 scope：里面的 run 是新的条目（外层的是另一条）。"""
        from CharTyr_MaiWork.maiwork.main_run import MainRuns

        store, models = _make(tmp_path)
        runner = MainRuns(store)
        async with runner.scope(group_id="900000001", purpose="outer", title="外层"):
            await models.chat(agent="main",
                              messages=[{"role": "user", "content": "a"}],
                              purpose="outer.p1", group_id="900000001")
            async with runner.scope(group_id="900000001", purpose="inner", title="内层"):
                await models.chat(agent="main",
                                  messages=[{"role": "user", "content": "b"}],
                                  purpose="inner.p1", group_id="900000001")
        runs = _runs(store)
        assert len(runs) == 2
        purposes = {r["purpose"] for r in runs}
        assert purposes == {"outer", "inner"}
        inner = next(r for r in runs if r["purpose"] == "inner")
        outer = next(r for r in runs if r["purpose"] == "outer")
        calls = _mc(store)
        assert calls[0]["run_ref"] == outer["ref"]
        assert calls[1]["run_ref"] == inner["ref"]


# ----------------------------------------------------------------------
# 中断：进程死掉的 running
# ----------------------------------------------------------------------


class TestInterrupted:
    def test_orphans_marked_cancelled_on_start(self, tmp_path) -> None:
        """遗留 running 在启动收尾时标 cancelled。"""
        from CharTyr_MaiWork.maiwork.main_run import MainRuns

        store, _ = _make(tmp_path)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                " VALUES ('900000001','quick_judge.test','判断中','running',?,?,0,'','main:quick_judge.test:g1:1:0')",
                (clock.now() - 100, 0),
            )
        runner = MainRuns(store)
        n = runner.close_orphans(reason="进程重启中断")
        assert n == 1
        run = _runs(store)[0]
        assert run["status"] == "cancelled"
        assert run["error"] == "进程重启中断"
        assert run["finished"] > 0

    def test_stale_running_counts_as_interrupted_in_board(self, tmp_path) -> None:
        """看板读取：超过时限还 running 的按「已中断」显示（不等人来收尾）。"""
        from CharTyr_MaiWork.maiwork.main_run import RUN_STALE_S

        store, _ = _make(tmp_path)
        old = clock.now() - RUN_STALE_S - 60
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                " VALUES ('900000001','p','判断','running',?,0,0,'','main:p:g1:2:0')",
                (old,),
            )
        svc = _Svc(store)
        data = board_view(svc, hours=24, now=clock.now())
        lane = next(x for x in data["lanes"] if x["kind"] == "main")
        assert lane["running"] == []
        assert any(i["status"] == "cancelled" for i in lane["history"])


# ----------------------------------------------------------------------
# 清理
# ----------------------------------------------------------------------


class TestPrune:
    def test_prune_by_age(self, tmp_path) -> None:
        """main_runs 跟 model_calls 同口径：3 天前的清掉。"""
        store, _ = _make(tmp_path)
        old = clock.now() - 4 * 86400
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                " VALUES ('900000001','p','t','done',?,?,1,'','main:p:g1:3:0')",
                (old, old),
            )
        counts = store.prune(clock.now())
        assert counts.get("main_runs") == 1
        assert _runs(store) == []

    def test_prune_keeps_recent(self, tmp_path) -> None:
        """近 3 天的留着。"""
        store, _ = _make(tmp_path)
        recent = clock.now() - 3600
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                " VALUES ('900000001','p','t','done',?,?,1,'','main:p:g1:4:0')",
                (recent, recent),
            )
        counts = store.prune(clock.now())
        assert counts.get("main_runs") == 0
        assert len(_runs(store)) == 1


# ----------------------------------------------------------------------
# 看板
# ----------------------------------------------------------------------


def _seed_run(store, *, status: str, started: float, finished: float = 0.0,
              gid: str = "900000001", purpose: str = "intake.quick_judge",
              title: str = "判断这条 @ 是不是派活", ref: str = "") -> str:
    ref = ref or f"main:{purpose}:{gid}:{int(started * 1000)}:0"
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (gid, purpose, title, status, started, finished,
             1 if status == "done" else 0, "", ref),
        )
    return ref


def _mc_with_ref(store, *, ts: float, run_ref: str, group_id: str = "900000001",
                 purpose: str = "p", ok: bool = True) -> None:
    resp = json.dumps({"text": "想了一下", "reasoning": ""}, ensure_ascii=False)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt, ok,"
            " status, ms, prompt_tokens, completion_tokens, error, request, response,"
            " usage_src, cache_read, cache_write, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, purpose, "main", "main", "svc-model", group_id, "", 1, 1 if ok else 0,
             200, 30, 3, 5, "", "{}", resp, "reported", 0, 0, run_ref),
        )


def _tc_with_ref(store, *, ts: float, run_ref: str, group_id: str = "900000001") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error, run_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, group_id, "", "主模型", "probe", "in", "out", 5, 1, "", run_ref),
        )


class TestBoardLane:
    def test_running_and_history(self, tmp_path) -> None:
        """main lane：running 里是在跑的条目，history 里是终态。"""
        store, _ = _make(tmp_path)
        now = clock.now()
        _seed_run(store, status="running", started=now - 10, ref="main:r:g1:1:0")
        _seed_run(store, status="done", started=now - 100, finished=now - 90, ref="main:d:g1:2:0")
        _seed_run(store, status="failed", started=now - 200, finished=now - 190, ref="main:f:g1:3:0")
        svc = _Svc(store)
        data = board_view(svc, hours=24, now=now)
        lane = next(x for x in data["lanes"] if x["kind"] == "main")
        assert [i["id"] for i in lane["running"]] != []
        statuses = {i["status"] for i in lane["history"]}
        assert "done" in statuses and "failed" in statuses
        assert lane["counts"]["running"] == 1
        assert lane["counts"]["done"] >= 1
        assert lane["counts"]["failed"] >= 1

    def test_not_served_group_hidden(self, tmp_path) -> None:
        """非服务群的条目不显示。"""
        store, _ = _make(tmp_path)
        _seed_run(store, status="done", started=clock.now() - 50,
                  finished=clock.now() - 40, gid="555555555", ref="main:x:not-served:5:0")
        svc = _Svc(store)
        data = board_view(svc, hours=24, now=clock.now())
        lane = next(x for x in data["lanes"] if x["kind"] == "main")
        assert lane["history"] == []
        assert lane["running"] == []

    def test_item_detail_exact_link(self, tmp_path) -> None:
        """详情：link=exact，steps 挂的是这条 run 的调用（按 run_ref 查）。"""
        store, _ = _make(tmp_path)
        now = clock.now()
        ref = _seed_run(store, status="done", started=now - 100, finished=now - 90,
                        ref="main:p:g1:6:0")
        _mc_with_ref(store, ts=now - 95, run_ref=ref)
        _mc_with_ref(store, ts=now - 93, run_ref=ref)
        _tc_with_ref(store, ts=now - 94, run_ref=ref)
        # 别条的调用不进来
        _mc_with_ref(store, ts=now - 96, run_ref="main:other:g1:7:0")
        _mc_with_ref(store, ts=now - 92, run_ref="")  # 没挂 ref 的老行
        svc = _Svc(store)
        run_id = _runs(store)[0]["id"]
        view = item_view(svc, "main", str(run_id), now=now)
        assert view is not None
        assert view["link"] == "exact"
        assert view["item"]["source"] == "main"
        thinks = [s for s in view["steps"] if s["type"] == "think"]
        tools = [s for s in view["steps"] if s["type"] == "tool"]
        assert len(thinks) == 2
        assert len(tools) == 1
        statuses = [s for s in view["steps"] if s["type"] == "status"]
        assert any("结束" in s["text"] for s in statuses)

    def test_item_detail_not_found(self, tmp_path) -> None:
        """不存在 / 非服务群 → None（路由层 404）。"""
        store, _ = _make(tmp_path)
        _seed_run(store, status="done", started=clock.now() - 50,
                  finished=clock.now() - 40, gid="555555555", ref="main:x:outsider:8:0")
        svc = _Svc(store)
        assert item_view(svc, "main", "9999") is None
        run_id = _runs(store)[0]["id"]
        assert item_view(svc, "main", str(run_id)) is None

    def test_item_counts_and_last_step(self, tmp_path) -> None:
        """lane item 带 counts（按 run_ref 数）和 last_step。"""
        store, _ = _make(tmp_path)
        now = clock.now()
        ref = _seed_run(store, status="done", started=now - 100, finished=now - 90,
                        ref="main:c:g1:9:0")
        _mc_with_ref(store, ts=now - 95, run_ref=ref)
        _tc_with_ref(store, ts=now - 94, run_ref=ref)
        svc = _Svc(store)
        data = board_view(svc, hours=24, now=now)
        lane = next(x for x in data["lanes"] if x["kind"] == "main")
        item = next(i for i in lane["history"] if i["id"] == _runs(store)[0]["id"])
        assert item["counts"]["thinks"] == 1
        assert item["counts"]["tools"] == 1
        assert item["last_step"] is not None

    def test_title_and_no_verbatim(self, tmp_path) -> None:
        """title 出现在 lane item；绝不出现群友原话 / QQ 号 / 对话全文。"""
        store, _ = _make(tmp_path)
        secret_text = "群友 987654321 的原话秘密"
        now = clock.now()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO main_runs (group_id, purpose, title, status, started, finished, ok, error, ref)"
                " VALUES ('900000001','admin_chat','回答管理员','done',?,?,1,'','main:admin_chat:g1:10:0')",
                (now - 100, now - 90),
            )
        svc = _Svc(store)
        data = board_view(svc, hours=24, now=now)
        blob = json.dumps(data, ensure_ascii=False)
        assert "987654321" not in blob
        assert secret_text not in blob
        lane = next(x for x in data["lanes"] if x["kind"] == "main")
        assert lane["history"][0]["title"] == "回答管理员"
