"""三道能力边界刹车（2026-10-31 Trellis 10-31 任务 Step 3）。

第一道（auto_review.py）：自动审核的提示词里写清 MaiWork 真实能力边界 + 当前
看图 / 生图模型「已配置 / 未配置」的事实；请求明显依赖现在没配的能力（看图 /
生图）→ 连模型都不调，直接留人批（fail-closed，不让模型替没配的硬件拍板）。

第二道（coordinator.py）：`_plan` 支持 "unsupported" 字段——主模型开工前认出
超出能力边界（修图改表情 / 做动画视频 / 冷门图反查出处 / 登录外部账号）就直接
收尾：任务转 failed、发件箱 @ 发起人说明、零子 agent、不开远程机器、不重试。
配套接线：_plan 提示词写清能力边界与 unsupported 字段；_REVIEW_TOOLS 加
inspect_image；_INPUT_IMAGES_HINT 点名 read_image；开工前自检新增
need="vision"（任务带原图 / brief 要看图时补 read_image，没配看图模型就走
重排 → 暂停闭环）。

第三道（coordinator.py）：`_blocked_ask_items` 第 1 轮放行——本轮子 agent 有
真实工具调用证据（has_tool_evidence，只认服务器侧遥测）且全部未完成必须项都是
blocked 时，直接转 waiting_input 问发起人，不再强制烧一轮返工。

用真 Store / 真 Agents / 真 Tools / 真 LocalEnv / 真 Tasks，模型用队列桩、
Workers 用抓取桩，不调真模型、不碰网络、不碰宿主。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.approvals import Approvals
from CharTyr_MaiWork.maiwork.auto_review import AutoReviewer
from CharTyr_MaiWork.maiwork.coordinator import (
    Coordinator,
    input_images_hint,
    job_needs_vision_capability,
    main_review_tool_specs,
)
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.goals import Goals
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tasks import Tasks
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult

from fakes import FakeModelsQueue
from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    CaptureWorkers,
    FakeDelivery,
    FakeOutbox,
    FakeProfiles,
    ModelsQueue,
    _add_worker_tool,
    _events,
    _job,
    _kinds,
    _make_specialists,
    agents,
    env,
    fixed_clock,
    settings,
    store,
    tools,
)

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# 桩与 helper
# ----------------------------------------------------------------------


class VisionModels(ModelsQueue):
    """带 has_vision 的模型桩（Step 3 视觉能力检查用）。"""

    def __init__(self, replies=None, *, vision=True):
        super().__init__(replies)
        self._vision = vision

    def has_vision(self, kind="task"):  # noqa: ARG002
        return self._vision


def _coord(store, settings, env, tools, agents, workers, *,
           specialists=None, models=None):
    """和 test_exec_capability_gate 的 _coord 同形，但文件内自带（不跨文件引私有函数）。"""
    outbox, delivery = FakeOutbox(), FakeDelivery()
    coord = Coordinator(
        store, models, workers, tools, Tasks(store, lambda: settings),
        Goals(store, lambda: settings), delivery, outbox, env,
        FakeProfiles(), lambda: settings,
    )
    coord._specialists = specialists  # noqa: SLF001
    return coord, outbox


def _add_main_tool(tools, name: str) -> None:
    async def _h(ctx, args):  # noqa: ARG001
        return ToolResult(ok=True, output="x")

    tools.register(Tool(
        name=name, description="t", parameters={"type": "object"},
        roles=frozenset({"main"}), handler=_h,
    ))


# ----------------------------------------------------------------------
# auto_review 小 harness（与 test_auto_review.py 同形，只加能力回调参数）
# ----------------------------------------------------------------------


def _ar_settings(*, serve=(GID,)):
    cfg = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{g}"} for g in serve]},
        "approval": {"auto_review": True, "auto_review_daily": 5},
    }
    loaded, problems = load_settings(cfg)
    assert problems == [], problems
    return loaded


class _ArHarness:
    def __init__(self, tmp_path, *, replies=None, has_vision=None, has_imagegen=None):
        self.store = Store(tmp_path / "mw-ar.db")
        self.store.migrate()
        self.settings = _ar_settings()
        self.models = FakeModelsQueue(ready=True, replies=replies)
        self.tasks = Tasks(self.store, lambda: self.settings)
        self.goals = Goals(self.store, lambda: self.settings)
        self.approvals = Approvals(self.store, lambda: self.settings, self.tasks, self.goals)
        self.started: list[str] = []
        self.reviewer = AutoReviewer(
            self.store, self.models, self.approvals, lambda: self.settings,
            run_task_starter=self.started.append,
            has_vision=has_vision, has_imagegen=has_imagegen,
        )

    def create(self, **kw):
        base = dict(
            kind="task", title="帮我查一下有没有免费图床",
            quote="@MaiBot 帮我查一下免费图床", via="群里 @",
            requester_id="10001", requester_name="阿柒",
        )
        base.update(kw)
        return self.approvals.create(GID, **base)

    def status(self, rid):
        return dict(self.store.read().execute(
            "SELECT * FROM requests WHERE id=?", (rid,)
        ).fetchone())

    def close(self):
        self.store.close()


_OK = json.dumps({"approve": True, "reason": "低风险轻活"}, ensure_ascii=False)


# ----------------------------------------------------------------------
# 第一道刹车：auto_review 真实能力边界
# ----------------------------------------------------------------------


class TestAutoReviewCapabilityBoundary:
    async def test_prompt_injects_capability_boundary_and_readiness(self, tmp_path):
        h = _ArHarness(tmp_path, has_vision=lambda: False, has_imagegen=lambda: False)
        try:
            prompt = h.reviewer._build_prompt(  # noqa: SLF001
                {"title": "看看图", "quote": "", "via": ""}
            )
        finally:
            h.close()
        assert "能力边界" in prompt
        assert "看图模型：未配置" in prompt
        assert "生图模型：未配置" in prompt
        assert "read_image" in prompt
        # 超界清单要点名（改表情 / 动画视频 / 反查出处 / 登录验证码）
        assert "改表情" in prompt
        assert "动画" in prompt
        assert "验证码" in prompt

    async def test_prompt_shows_configured_readiness(self, tmp_path):
        h = _ArHarness(tmp_path, has_vision=lambda: True, has_imagegen=lambda: True)
        try:
            prompt = h.reviewer._build_prompt(  # noqa: SLF001
                {"title": "看看图", "quote": "", "via": ""}
            )
        finally:
            h.close()
        assert "看图模型：已配置" in prompt
        assert "生图模型：已配置" in prompt

    async def test_readiness_callback_error_counts_as_unconfigured(self, tmp_path):
        def _boom():
            raise RuntimeError("读配置炸了")

        h = _ArHarness(tmp_path, has_vision=_boom, has_imagegen=_boom)
        try:
            prompt = h.reviewer._build_prompt(  # noqa: SLF001
                {"title": "x", "quote": "", "via": ""}
            )
        finally:
            h.close()
        # fail-closed：读不出就绪状态就当没配，不吹一个可能跑不通的能力
        assert "看图模型：未配置" in prompt
        assert "生图模型：未配置" in prompt

    async def test_vision_request_without_model_never_calls_model(self, tmp_path):
        """要真看图的活、而看图模型没配 → 连模型都不调，直接留人批。"""
        h = _ArHarness(tmp_path, replies=[_OK], has_vision=lambda: False,
                       has_imagegen=lambda: True)
        try:
            r = h.create(title="帮我看看这张截图里写了什么",
                         quote="@MaiBot 帮我看看这张截图里写了什么")
            assert await h.reviewer.review(r["id"]) is None
            assert h.models.calls == []
            assert h.status(r["id"])["status"] == "pending"
            assert h.started == []
        finally:
            h.close()

    async def test_imagegen_request_without_model_never_calls_model(self, tmp_path):
        h = _ArHarness(tmp_path, replies=[_OK], has_vision=lambda: True,
                       has_imagegen=lambda: False)
        try:
            r = h.create(title="帮我画一张海报", quote="@MaiBot 帮我画一张海报")
            assert await h.reviewer.review(r["id"]) is None
            assert h.models.calls == []
            assert h.status(r["id"])["status"] == "pending"
        finally:
            h.close()

    async def test_vision_request_with_model_reaches_judge(self, tmp_path):
        """看图模型配好了：依赖看图的请求照常送模型判，提示词里带着就绪事实。"""
        h = _ArHarness(tmp_path, replies=[_OK], has_vision=lambda: True,
                       has_imagegen=lambda: False)
        try:
            r = h.create(title="帮我看看这张截图里写了什么",
                         quote="帮我看看这张截图里写了什么")
            res = await h.reviewer.review(r["id"])
            assert res is not None
            assert len(h.models.calls) == 1
            prompt = h.models.calls[0][1][0]["content"]
            assert "看图模型：已配置" in prompt
            assert "生图模型：未配置" in prompt
            assert "能力边界" in prompt
        finally:
            h.close()


# ----------------------------------------------------------------------
# 第二道刹车：_plan unsupported 直接收尾
# ----------------------------------------------------------------------


def _plan_json(**kw) -> str:
    data = {
        "criteria": ["每条要求都做到"],
        "deliver_kind": "text",
        "jobs": [],
        "question": None,
    }
    data.update(kw)
    return json.dumps(data, ensure_ascii=False)


class TestPlanUnsupported:
    async def test_unsupported_ends_task_without_workers_or_retry(
        self, store, settings, env, tools, agents
    ):
        """主模型开工前说超出能力边界：任务转 failed + @ 发起人说明，
        零子 agent、不再重排不再重试。"""
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(
            GID, title="把图做成动画", req="把这张图做成 10 秒会动的视频",
            criteria=["出一个 10 秒动画"], source="test",
        )
        workers = CaptureWorkers()
        models = ModelsQueue(replies=[
            _plan_json(unsupported="MaiWork 不会剪辑视频 / 动画，只会看图片内容"),
        ])
        coord, outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        task = tasks.get(tid)
        assert task["status"] == "failed"
        assert "能力不支持" in str(task.get("review") or task.get("reason") or "")
        assert workers.calls == [], "unsupported 不许派任何子 agent"
        # 发件箱里有一句给发起人的说明（不是「没做成」套话）
        assert outbox.enqueued
        text = str(outbox.enqueued[0]["payload"].get("text") or "")
        assert "目前做不到" in text
        assert "动画" in text or "视频" in text
        assert "task.plan.unsupported" in _kinds(store, tid)

    async def test_unsupported_does_not_open_remote_machine(
        self, store, settings, env, tools, agents
    ):
        """unsupported 在申请远程机器之前就收尾（env=ssh/railway 也不开）。"""
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(
            GID, title="反查表情包出处", req="把这个没字的表情包的原始出处找出来",
            criteria=["找出处"], source="test",
        )
        workers = CaptureWorkers()
        models = ModelsQueue(replies=[
            _plan_json(env="railway",
                       unsupported="没有文字的冷门图反查不到精确出处"),
        ])
        coord, outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "failed"
        assert workers.calls == []
        assert len(models.calls) == 1, "unsupported 后不该再有模型回合（不重排不重试）"

    async def test_plan_prompt_documents_unsupported_field(
        self, store, settings, env, tools, agents
    ):
        """排计划提示词要写明 unsupported 字段和能力边界，模型才知道能回这个值。"""
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(
            GID, title="查资料", req="帮我查一下明天天气", criteria=["查到"], source="test",
        )
        workers = CaptureWorkers()
        models = ModelsQueue(replies=[
            _plan_json(
                jobs=[{"brief": "查一下明天天气", "tools": ["web_search"], "type": "other"}],
            ),
            json.dumps({"pass": True, "review": "行", "missing": [], "artifact": "", "note": ""}),
        ])
        coord, _outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        plan_prompts = [
            "\n".join(str(m.get("content") or "") for m in msgs if isinstance(m.get("content"), str))
            for _r, msgs, kw in models.calls
            if str(kw.get("purpose") or "") == "coordinator.plan"
        ]
        assert plan_prompts
        assert "unsupported" in plan_prompts[0]
        # 提示词里写了「什么做不到」的边界（举的例子至少一个命中）
        assert "动画" in plan_prompts[0] or "验证码" in plan_prompts[0] or "出处" in plan_prompts[0]


# ----------------------------------------------------------------------
# 第二道刹车配套：read_image / inspect_image 接线 + vision 能力自检
# ----------------------------------------------------------------------


class TestVisionWiring:
    async def test_input_images_hint_names_read_image(self):
        hint = input_images_hint(["artifacts/T-1/input/a.png"])
        assert "read_image" in hint
        assert "input/a.png" in hint

    async def test_review_tool_specs_include_inspect_image(self, tools):
        """验收回合的工具表：inspect_image 注册后要真的出现（没注册不报错）。"""
        _add_main_tool(tools, "inspect_image")
        names = [
            str((s.get("function") or {}).get("name") or "")
            for s in main_review_tool_specs(tools)
        ]
        assert "inspect_image" in names
        assert "inspect_file" in names

    async def test_job_needs_vision_capability_keywords(self):
        assert job_needs_vision_capability("帮我看看这张截图里写了什么")
        assert job_needs_vision_capability("这张图里写了什么")
        assert job_needs_vision_capability("识别一下这张图片上的文字")
        assert job_needs_vision_capability("把图里的文字提取出来")
        # 画图 / 纯文字不算看图
        assert not job_needs_vision_capability("帮我画一张海报")
        assert not job_needs_vision_capability("写一页纯文字总结")


class TestVisionCapabilitySelfCheck:
    async def test_input_images_without_vision_model_blocks(
        self, store, settings, env, tools, agents
    ):
        """任务带原图、而没配看图模型 → need="vision" 的 unavailable（走重排→暂停）。"""
        _add_worker_tool(tools, "read_image")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=ModelsQueue(),
        )
        coord._input_images["T-1"] = ["artifacts/T-1/input/a.png"]  # noqa: SLF001
        plan = {"criteria": ["说清楚图上是什么"], "deliver_kind": "text",
                "jobs": [_job(["read_file"], brief="整理材料")]}  # brief 本身不提图

        report = coord._exec_capability_self_check(  # noqa: SLF001
            "T-1", GID, plan, on_remote=False, box=None
        )

        assert report.blocked
        vision = [f for f in report.blocked_findings if f.need == "vision"]
        assert vision, "要产出一条 need=vision 的不可用结论"
        assert "task.exec_unavailable" in _kinds(store, "T-1")
        assert "read_image" not in plan["jobs"][0]["tools"], "没配模型不许硬塞工具"

    async def test_vision_brief_without_model_blocks(
        self, store, settings, env, tools, agents
    ):
        """brief 要看图（没带原图也一样）、而没配看图模型 → unavailable。"""
        _add_worker_tool(tools, "read_image")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=ModelsQueue(),
        )
        plan = {"criteria": ["整理成一页"], "deliver_kind": "text",
                "jobs": [_job(["read_file"], brief="看看这张截图里写了什么")]}

        report = coord._exec_capability_self_check(  # noqa: SLF001
            "T-1", GID, plan, on_remote=False, box=None
        )

        assert report.blocked
        assert any(f.need == "vision" for f in report.blocked_findings)

    async def test_vision_need_fills_read_image_when_configured(
        self, store, settings, env, tools, agents
    ):
        """配了看图模型 + read_image 已注册 + 岗位放行 → 自动补上，不报警。"""
        _add_worker_tool(tools, "read_image")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=VisionModels(vision=True),
        )
        plan = {"criteria": ["说清图上内容"], "deliver_kind": "text",
                "jobs": [_job(["read_file"], brief="看看这张截图里写了什么")]}

        report = coord._exec_capability_self_check(  # noqa: SLF001
            "T-1", GID, plan, on_remote=False, box=None
        )

        assert not report.blocked
        assert "read_image" in plan["jobs"][0]["tools"]
        assert any(
            f.need == "vision" and f.status == "filled" for f in report.findings
        )

    async def test_input_images_with_vision_model_fills(
        self, store, settings, env, tools, agents
    ):
        """任务带原图 + 配了看图模型 → brief 不提图也给补 read_image。"""
        _add_worker_tool(tools, "read_image")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=VisionModels(vision=True),
        )
        coord._input_images["T-1"] = ["artifacts/T-1/input/a.png"]  # noqa: SLF001
        plan = {"criteria": ["整理成一页"], "deliver_kind": "text",
                "jobs": [_job(["read_file"], brief="整理材料")]}

        report = coord._exec_capability_self_check(  # noqa: SLF001
            "T-1", GID, plan, on_remote=False, box=None
        )

        assert not report.blocked
        assert "read_image" in plan["jobs"][0]["tools"]

    async def test_vision_tool_already_present_needs_no_action(
        self, store, settings, env, tools, agents
    ):
        """计划里本来就有 read_image 且配了模型 → 不补也不报警。"""
        _add_worker_tool(tools, "read_image")
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        coord, _outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=VisionModels(vision=True),
        )
        plan = {"criteria": ["说清图上内容"], "deliver_kind": "text",
                "jobs": [_job(["read_file", "read_image"], brief="看看这张截图里写了什么")]}

        report = coord._exec_capability_self_check(  # noqa: SLF001
            "T-1", GID, plan, on_remote=False, box=None
        )

        assert not report.blocked
        assert "read_image" in plan["jobs"][0]["tools"]
        # 「截图」命中执行类关键词，run_command 被既有 exec 闸补上——那是旧行为；
        # 这条断言只管 vision：read_image 在、没报 vision 不可用。
        assert not any(
            f.need == "vision" and f.status == "unavailable" for f in report.findings
        )


# ----------------------------------------------------------------------
# 第三道刹车：首轮有真实工具证据的 blocked 直接止损
# ----------------------------------------------------------------------


def _review_dict(*, unmet=("R1",), blocked=("R1",)) -> dict:
    return {
        "pass": False,
        "items_judgement": {
            "unmet_blocking": [{"id": i, "text": f"要求 {i}"} for i in unmet],
        },
        "blocked_items": [
            {"id": i, "text": f"要求 {i}", "reason": "客观做不到", "evidence": "试过三种方法"}
            for i in blocked
        ],
    }


class TestBlockedAskItemsFirstAttempt:
    async def test_attempt1_with_tool_evidence_asks(self, tools):
        coord = self._bare_coord(tools)
        items = coord._blocked_ask_items(  # noqa: SLF001
            _review_dict(), 1, has_tool_evidence=True,
        )
        assert [i["id"] for i in items] == ["R1"]

    async def test_attempt1_without_tool_evidence_reworks(self, tools):
        coord = self._bare_coord(tools)
        assert coord._blocked_ask_items(  # noqa: SLF001
            _review_dict(), 1, has_tool_evidence=False,
        ) == []

    async def test_attempt2_keeps_old_behavior(self, tools):
        coord = self._bare_coord(tools)
        items = coord._blocked_ask_items(  # noqa: SLF001
            _review_dict(), 2,
        )
        assert [i["id"] for i in items] == ["R1"]

    async def test_attempt1_evidence_but_partial_blocked_reworks(self, tools):
        """第 1 轮有证据、但不是全部未完成项都 blocked → 照旧返工。"""
        coord = self._bare_coord(tools)
        review = _review_dict(unmet=("R1", "R2"), blocked=("R1",))
        assert coord._blocked_ask_items(  # noqa: SLF001
            review, 1, has_tool_evidence=True,
        ) == []

    async def test_passed_review_never_asks(self, tools):
        coord = self._bare_coord(tools)
        review = _review_dict()
        review["pass"] = True
        assert coord._blocked_ask_items(  # noqa: SLF001
            review, 1, has_tool_evidence=True,
        ) == []

    @staticmethod
    def _bare_coord(tools):
        """只够调 _blocked_ask_items 的最小 Coordinator（不用跑库）。"""
        import types

        store = types.SimpleNamespace(read=lambda: None)
        return Coordinator(
            store, None, None, tools, None, None, FakeDelivery(), FakeOutbox(),
            None, FakeProfiles(), lambda: None,
        )


class TestToolEvidence:
    """has_tool_evidence 只认服务器侧遥测：tool_calls 表 / 交接单 used_tools。"""

    async def test_tool_calls_row_counts_as_evidence(self, store, settings, env, tools, agents):
        workers = CaptureWorkers()
        coord, _ = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=ModelsQueue(),
        )
        tid = "T-ev-1"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                " VALUES (?, ?, ?, ?, 'fetch_page', '', '', 0, 1, '')",
                (clock.now(), GID, tid, "子 agent #1"),
            )
        assert coord._round_has_tool_evidence(tid, []) is True  # noqa: SLF001

    async def test_submit_result_alone_is_not_evidence(self, store, settings, env, tools, agents):
        """只调过 submit_result（交回通道）不算真试过。"""
        workers = CaptureWorkers()
        coord, _ = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=ModelsQueue(),
        )
        tid = "T-ev-2"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                " VALUES (?, ?, ?, ?, 'submit_result', '', '', 0, 1, '')",
                (clock.now(), GID, tid, "子 agent #1"),
            )
        assert coord._round_has_tool_evidence(tid, []) is False  # noqa: SLF001

    async def test_main_actor_calls_are_not_evidence(self, store, settings, env, tools, agents):
        """主模型自己查资料的调用不算「子 agent 试过」。"""
        workers = CaptureWorkers()
        coord, _ = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=ModelsQueue(),
        )
        tid = "T-ev-3"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                " VALUES (?, ?, ?, ?, 'inspect_file', '', '', 0, 1, '')",
                (clock.now(), GID, tid, "主模型"),
            )
        assert coord._round_has_tool_evidence(tid, []) is False  # noqa: SLF001

    async def test_failed_calls_are_not_evidence(self, store, settings, env, tools, agents):
        workers = CaptureWorkers()
        coord, _ = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=ModelsQueue(),
        )
        tid = "T-ev-4"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                " VALUES (?, ?, ?, ?, 'run_command', '', '', 0, 0, '炸了')",
                (clock.now(), GID, tid, "子 agent #1"),
            )
        assert coord._round_has_tool_evidence(tid, []) is False  # noqa: SLF001


class TestBlockedAskEndToEnd:
    async def test_first_attempt_blocked_goes_waiting_input(
        self, store, settings, env, tools, agents
    ):
        """第 1 轮：子 agent 真试过（tool_calls 有成功调用）、唯一必须项 blocked
        → 转 waiting_input 问发起人，不重跑第 2 轮。"""
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(
            GID, title="拿内部文档", req="帮我把内部系统里那份文档拿下来",
            criteria=["拿到内部文档"], source="test",
        )
        workers = CaptureWorkers()

        async def _seed_tool_evidence():
            # 模拟子 agent 干活时真的调过工具（服务器侧遥测）
            with store.tx() as conn:
                conn.execute(
                    "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
                    " VALUES (?, ?, ?, ?, 'fetch_page', '', '页', 0, 1, '')",
                    (clock.now(), GID, tid, "子 agent #1"),
                )

        workers.before_return = _seed_tool_evidence
        models = ModelsQueue(replies=[
            _plan_json(
                requirements=[{
                    "text": "拿到需要登录的内部文档", "origin": "原话", "kind": "实做",
                }],
                jobs=[{
                    "brief": "拿到需要登录的内部文档",
                    "tools": ["fetch_page"], "type": "other",
                }],
            ),
            json.dumps({
                "pass": False,
                "items": [
                    {
                        "id": "R1", "met": False,
                        "blocked": "内部文档需要登录企业账号，外部访问拿不到权限",
                        "evidence": "fetch_page 打开官网被要求登录，网页快照也打不开，三种途径都试过",
                    },
                    # 底线项也要判（不判 = 没做到 → 全部 unmet 不都 blocked，走旧路）
                    {"id": "R0", "met": True, "evidence": "内容真实"},
                ],
                "review": "唯一必须项客观做不到", "missing": [], "artifact": "", "note": "",
            }, ensure_ascii=False),
        ])
        coord, outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        task = tasks.get(tid)
        assert task["status"] == "waiting_input"
        # 只派了一轮子 agent（没强制返工）
        assert len(workers.calls) == 1
        # 发件箱里有一句问发起人的话
        texts = [str(e["payload"].get("text") or "") for e in outbox.enqueued]
        assert any("做不到" in t or "换一个" in t or "还是" in t for t in texts), texts
        assert "task.blocked_ask" in _kinds(store, tid)

    async def test_first_attempt_blocked_without_evidence_reworks(
        self, store, settings, env, tools, agents
    ):
        """第 1 轮没真实工具证据 → 照旧返工（不止损）；最终会在第 2/3 轮走旧路。"""
        tasks = Tasks(store, lambda: settings)
        tid = tasks.create(
            GID, title="拿内部文档", req="帮我把内部系统里那份文档拿下来",
            criteria=["拿到内部文档"], source="test",
        )
        workers = CaptureWorkers()
        review_fail = json.dumps({
            "pass": False,
            "items": [
                {
                    "id": "R1", "met": False,
                    "blocked": "内部文档需要登录企业账号，外部访问拿不到权限",
                    "evidence": "fetch_page 打开官网被要求登录，网页快照也打不开，三种途径都试过",
                },
                {"id": "R0", "met": True, "evidence": "内容真实"},
            ],
            "review": "唯一必须项客观做不到", "missing": [], "artifact": "", "note": "",
        }, ensure_ascii=False)
        models = ModelsQueue(replies=[
            _plan_json(
                requirements=[{
                    "text": "拿到需要登录的内部文档", "origin": "原话", "kind": "实做",
                }],
                jobs=[{"brief": "拿到需要登录的内部文档",
                       "tools": ["fetch_page"], "type": "other"}],
            ),
            review_fail,          # 第 1 轮验收：blocked（但没证据 → 返工）
            _plan_json(            # 第 2 轮计划（locked 清单，只排 jobs）
                jobs=[{"brief": "拿到需要登录的内部文档",
                       "tools": ["fetch_page"], "type": "other"}],
            ),
            review_fail,          # 第 2 轮验收：还是 blocked → 这次旧路问发起人
        ])
        coord, outbox = _coord(
            store, settings, env, tools, agents, workers,
            specialists=None, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        task = tasks.get(tid)
        assert task["status"] == "waiting_input"
        # 关键：第 1 轮没证据 → 返工跑了第 2 轮（两次子 agent），第 2 轮 blocked 才停下
        assert len(workers.calls) == 2
