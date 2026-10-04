"""能力闸暂停原因（paused_reason kind=capability）：三处视图都说人话，且不泄密（2026-10 收口）。

背景（父会话收口口径）：`_pause_for_capability` 以前把「为什么停」借 `Tasks.set_env` 的
note 塞进任务详情「在哪里做」，`paused_reason` 只认安全网的 tokens / time，于是：
- 群管理员 / 群友在列表和详情里只看到「已暂停」，看不出是能力对不上；
- 能力闸自身出意外时 run_attempt 外层 except 是 fail-open（记一笔照老行为继续开工），
  等于闸形同不存在，最坏照旧白烧 token。

本次口径：

1. `paused_reason` 新增 `kind="capability"`：
   `{"kind": "capability", "text": "安全中文一句", "jobs": [出问题的活序号]}`；
   写入严格校验（kind 白名单、text 限长、jobs 只留正整数且限个数），脏数据一律不显示
   （宁可不显示，也不编一个「时长 0」的假原因）；
2. `list_view` / `detail_view(admin=True)` / `detail_view(admin=False)` 三处都给出同一句
   安全中文 `text`；里面不出现内部绝对路径、密钥样式、QQ 号这类账号；
3. 能力闸暂停写真实 `paused_reason`（不再借 set_env 挂理由）；「继续 / 取消」清干净；
4. 能力检查本身抛错 → fail-closed：按「能力检查没完成」暂停、Workers 零执行、一次性 /
   专用机器先释放、不记 failed、绝不把任务卡在 running；
5. `_unconsume_attempt` 回退尝试计数只在任务确实还停在 paused 时做：晚到的取消不复活。

用真 Store / 真 Tasks / 真 Agents / 真 Tools / 真 LocalEnv，模型用队列桩、Workers 用抓取桩，
不调真模型、不碰网络、不碰宿主。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from CharTyr_MaiWork.maiwork.tasks import Tasks
from test_effective_capability_review_fixes import (  # noqa: F401  （同目录测试模块互相导入是仓库惯例）
    GID,
    CaptureWorkers,
    ModelsQueue,
    _job,
    _make_specialists,
    _railway_box,
    agents,
    env,
    fixed_clock,
    settings,
    store,
    tools,
)
from test_console import (  # noqa: F401  （起真控制台，验群友 HTTP 拿到的就是安全那句）
    SimpleEnv,
    env as console_env,
)
from test_exec_capability_gate import (  # noqa: F401
    FakeRailway,
    _coord,
    _failed_messages,
    _kinds,
    _new_task,
    _plan_json,
)

pytestmark = pytest.mark.asyncio

TEXT_MAX = 400


def _mk(tasks: Tasks) -> str:
    return tasks.create(GID, title="找图", req="找图并存到本地", criteria=["存到本地"], source="test")


def _paused_item(tasks: Tasks, tid: str) -> dict:
    for item in tasks.list_view(GID):
        if item["id"] == tid:
            return item
    raise AssertionError(f"list_view 里找不到 {tid}")


# ----------------------------------------------------------------------
# 1. 写入 schema：kind 白名单 / text 限长 / jobs 最小化
# ----------------------------------------------------------------------


class TestPausedReasonSchema:
    def test_capability_reason_is_cleaned_and_bounded(self, store, settings):
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(
            tid, "paused", reason="能力闸",
            paused_reason={
                "kind": "capability",
                "text": "开工前对不上：" + "长" * 900,
                "jobs": [3, 1, 1, 0, -2, "bad", 4, 5, 6, 7, 8, 9, 10],
                "secret": "不该留下的字段",
            },
        )
        pr = json.loads(tasks.get(tid)["paused_reason"])
        assert pr["kind"] == "capability"
        assert len(pr["text"]) <= TEXT_MAX, "超长原因必须截断，不能原样进库 / 进网页"
        assert "secret" not in pr, "只留最小字段"
        assert pr["jobs"] == [3, 1, 4, 5, 6, 7, 8, 9], "jobs 只留正整数、去重、限个数"

    @pytest.mark.parametrize(
        "bad",
        [
            {"kind": "bogus", "text": "不认识的种类"},
            {"kind": "capability"},                     # 没有 text
            {"kind": "capability", "text": "   "},      # 空白 text
            {"kind": "time", "limit": 0, "used": 0},    # 假的「时长 0」
            {"kind": "tokens", "limit": -1, "used": 3},
            ["不是", "dict"],
        ],
    )
    def test_invalid_reason_is_rejected_without_dirty_write(self, store, settings, bad):
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        tasks.transition(tid, "running", reason="开工")
        with pytest.raises(ValueError):
            tasks.transition(tid, "paused", reason="能力闸", paused_reason=bad)
        row = tasks.get(tid)
        assert row["status"] == "running", "原因不合法：宁可这次不改状态，也不写脏数据"
        assert not row["paused_reason"]

    def test_dirty_legacy_reason_shows_nothing(self, store, settings):
        """老库里可能躺着脏数据（时长 0 / 不是 JSON）：视图宁可不显示，也不编原因。"""
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(tid, "paused", reason="网页暂停")
        for dirty in (json.dumps({"kind": "time", "limit": 0, "used": 0}), "not json", ""):
            with store.tx() as conn:
                conn.execute("UPDATE tasks SET paused_reason=? WHERE id=?", (dirty, tid))
            item = _paused_item(tasks, tid)
            assert item["paused_reason"] is None
            assert item["meta"] == "已暂停"

    def test_manual_pause_has_no_reason(self, store, settings):
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(tid, "paused", reason="网页暂停")
        assert not tasks.get(tid)["paused_reason"]
        item = _paused_item(tasks, tid)
        assert item["paused_reason"] is None, "手动暂停没有自动暂停原因"
        assert item["meta"] == "已暂停"


# ----------------------------------------------------------------------
# 2. 三处视图：安全中文一句，且不泄密
# ----------------------------------------------------------------------


class TestPausedReasonViews:
    def test_all_views_carry_safe_chinese_text(self, store, settings):
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        tasks.transition(tid, "running", reason="开工")
        tasks.transition(
            tid, "paused", reason="安全网",
            paused_reason={"kind": "tokens", "limit": 2000000, "used": 2100000},
        )
        item = _paused_item(tasks, tid)
        assert item["paused_reason"]["kind"] == "tokens"
        assert "自动暂停" in item["paused_reason"]["text"]
        assert item["meta"] == "自动暂停：用量到上限了，等你决定"
        for admin in (True, False):
            d = tasks.detail_view(tid, admin=admin)
            assert d["paused_reason"]["kind"] == "tokens"
            assert d["paused_reason"]["text"] == item["paused_reason"]["text"]

    def test_capability_text_scrubs_paths_secrets_accounts(self, store, settings):
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        tasks.transition(tid, "running", reason="开工")
        dirty = (
            "开工前对不上：在 /root/.typesafe_key 里读到 api_key=sk-live-abc123，"
            "工作区 ~/.dsh/secrets/pw 也在，账号 13800138000 同样对不上"
        )
        tasks.transition(
            tid, "paused", reason="能力闸",
            paused_reason={"kind": "capability", "text": dirty, "jobs": [1]},
        )
        stored = json.loads(tasks.get(tid)["paused_reason"])["text"]
        for leak in ("/root/.typesafe_key", "api_key=sk-live-abc123", "sk-live-abc123",
                     "~/.dsh", "13800138000"):
            assert leak not in stored, f"暂停原因不能带出 {leak!r}"
        assert "[路径]" in stored and "[已隐去]" in stored
        # 群友版拿到的是同一句（已经洗过），不会因为群友视图再漏一层
        member = tasks.detail_view(tid, admin=False)
        assert member["paused_reason"]["text"] == stored
        # 群友版仍然不给实现细节
        for key in ("env", "timeline", "tokens", "workspace", "source", "request_id", "requester_id"):
            assert key not in member

    def test_resume_and_cancel_clear_reason(self, store, settings):
        tasks = Tasks(store, lambda: settings)
        # 继续：原因清掉 + 记安全网基线
        t1 = _mk(tasks)
        tasks.transition(t1, "running", reason="开工")
        tasks.transition(
            t1, "paused", reason="能力闸",
            paused_reason={"kind": "capability", "text": "开工前对不上：先停下等你决定"},
        )
        tasks.transition(t1, "queued", reason="管理员继续")
        assert not tasks.get(t1)["paused_reason"]
        assert _paused_item(tasks, t1)["paused_reason"] is None
        assert store.kv_get(f"task.net_base.{t1}") is not None
        # 取消：原因也清干净，不留过期的「为什么停」
        t2 = _mk(tasks)
        tasks.transition(t2, "running", reason="开工")
        tasks.transition(
            t2, "paused", reason="能力闸",
            paused_reason={"kind": "capability", "text": "开工前对不上：先停下等你决定"},
        )
        tasks.transition(t2, "cancelled", reason="用户取消")
        assert not tasks.get(t2)["paused_reason"]


# ----------------------------------------------------------------------
# 3. 能力闸接线：真跑一遍，paused_reason 进 get / 列表 / 群友详情
# ----------------------------------------------------------------------


class TestCapabilityPauseWiring:
    async def test_pause_reason_visible_in_get_list_and_member_detail(
        self, store, settings, env, tools, agents
    ):
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        # 两次一样的计划：重排一次还是做不到 → 暂停
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
        ])
        coord, outbox, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        row = tasks.get(tid)
        assert row["status"] == "paused"
        pr = json.loads(row["paused_reason"])
        assert pr["kind"] == "capability"
        assert pr["jobs"] == [1]
        assert "执行工具" in pr["text"] and "继续" in pr["text"]

        item = _paused_item(tasks, tid)
        assert item["paused_reason"]["kind"] == "capability"
        assert item["paused_reason"]["text"] == pr["text"]
        assert item["meta"] == "自动暂停：开工前对不上，等你决定"

        member = tasks.detail_view(tid, admin=False)
        assert member["paused_reason"]["text"] == pr["text"], "群友详情也要能说清为什么停"
        admin = tasks.detail_view(tid, admin=True)
        assert admin["paused_reason"]["text"] == pr["text"]

        # 不再借 set_env 把理由挂进「在哪里做」
        assert "开工前对不上" not in str(row.get("env") or "")
        # 也不往群里发「没做成」
        assert _failed_messages(outbox) == []

        # 「继续」：原因清空，重新排队
        tasks.transition(tid, "queued", reason="管理员继续")
        assert not tasks.get(tid)["paused_reason"]
        assert _paused_item(tasks, tid)["paused_reason"] is None

    async def test_check_exception_fails_closed_and_releases_remote(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        """能力检查本身抛错：不许 fail-open 照常开工，也不许把任务卡在 running。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        box = _railway_box()
        railway = FakeRailway(box)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["read_file", "write_file"], agent="task")], env="railway"),
        ])
        coord, outbox, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models, railway=railway,
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("自检炸了")

        monkeypatch.setattr(coord, "_exec_capability_self_check", _boom)

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        row = tasks.get(tid)
        assert row["status"] == "paused", "检查没完成 → 安全暂停，不许照常开工"
        assert workers.calls == [], "一个子 agent 都不许跑"
        assert int(row["attempts"]) == 0, "零执行不扣尝试资源"
        pr = json.loads(row["paused_reason"])
        assert pr["kind"] == "capability"
        assert "能力检查没完成" in pr["text"]
        kinds = _kinds(store, tid)
        assert "task.failed" not in kinds
        assert "task.exec_check_incomplete" in kinds
        assert railway.released == [box], "先释放一次性机器再暂停"
        assert _failed_messages(outbox) == []
        assert _paused_item(tasks, tid)["paused_reason"]["kind"] == "capability"

    async def test_check_exception_does_not_revive_cancelled(
        self, store, settings, env, tools, agents, monkeypatch
    ):
        """检查抛错时任务已经被取消：安全暂停也不许把取消复活成 paused。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["write_file", "run_command"], agent="task")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        def _cancel_then_boom(*args, **kwargs):
            tasks.transition(tid, "cancelled", reason="用户取消")
            raise RuntimeError("自检炸了")

        monkeypatch.setattr(coord, "_exec_capability_self_check", _cancel_then_boom)

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        assert tasks.get(tid)["status"] == "cancelled", "取消优先：不许被安全暂停复活"
        assert "task.paused" not in _kinds(store, tid)
        assert workers.calls == []


# ----------------------------------------------------------------------
# 4. 真控制台 HTTP：群友 / 管理员拿到的就是安全那句
# ----------------------------------------------------------------------


class TestConsolePausedReason:
    async def test_member_http_detail_and_group_view_carry_safe_reason(self, console_env):
        app = console_env.app
        tid = app.tasks.create(
            GID, title="找图", req="找 3 张图存到本地", criteria=["存到本地"], source="test",
        )
        app.tasks.transition(tid, "running", reason="开工")
        app.tasks.transition(
            tid, "paused", reason="能力闸",
            paused_reason={
                "kind": "capability",
                "text": (
                    "开工前对不上：在 /root/.typesafe_key 里读到 api_key=sk-live-abc123，"
                    "账号 13800138000 也对不上"
                ),
                "jobs": [1],
            },
        )
        token = app.token_of(GID)

        r = await console_env.client.get(f"/api/tasks/{tid}", headers={"X-MW-Group": token})
        assert r.status == 200
        body = await r.text()
        assert "/root/.typesafe_key" not in body
        assert "sk-live-abc123" not in body
        assert "13800138000" not in body
        assert '"env"' not in body and '"timeline"' not in body
        detail = json.loads(body)
        assert detail["paused_reason"]["kind"] == "capability"
        assert "[路径]" in detail["paused_reason"]["text"]

        view = await (await console_env.client.get(
            f"/api/groups/{token}", headers={"X-MW-Group": token}
        )).json()
        listed = {str(x.get("id")): x for x in view["tasks"]["list"]}
        assert listed[tid]["paused_reason"]["kind"] == "capability"
        assert listed[tid]["paused_reason"]["text"] == detail["paused_reason"]["text"]
        assert listed[tid]["meta"] == "自动暂停：开工前对不上，等你决定"
        assert "/root/.typesafe_key" not in json.dumps(view, ensure_ascii=False)


# ----------------------------------------------------------------------
# 5. 尝试计数回退不复活取消 / 终态
# ----------------------------------------------------------------------


class TestUnconsumeAttemptDoesNotRevive:
    def test_cancelled_task_is_not_touched(self, store, settings, env, tools, agents):
        tasks = Tasks(store, lambda: settings)
        tid = _mk(tasks)
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, CaptureWorkers(), models=ModelsQueue(),
        )
        tasks.transition(tid, "running", reason="开工")
        aid = tasks.start_attempt(tid)
        tasks.transition(tid, "cancelled", reason="用户取消")
        before = tasks.get(tid)

        coord._unconsume_attempt(tid, aid)  # noqa: SLF001

        after = tasks.get(tid)
        assert after["status"] == "cancelled", "晚到的能力闸回退不许复活取消"
        assert int(after["attempts"]) == int(before["attempts"]) == 1, "取消后的任务不再退计数"
        row = store.read().execute(
            "SELECT status FROM attempts WHERE id=?", (int(aid),)
        ).fetchone()
        assert row is not None and str(row["status"]) == "stale"

    async def test_paused_task_gets_attempt_refunded(self, store, settings, env, tools, agents):
        """对照：真的停在 paused 时才退计数（现有行为不许被上面的防护弄丢）。"""
        tasks = Tasks(store, lambda: settings)
        tid = _new_task(tasks)
        workers = CaptureWorkers()
        specialists = _make_specialists(agents, workers)
        models = ModelsQueue(replies=[
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
            _plan_json(jobs=[_job(["web_search"], agent="news")]),
        ])
        coord, _o, _d = _coord(
            store, settings, env, tools, agents, workers,
            specialists=specialists, models=models,
        )

        await asyncio.wait_for(coord.run_task(tid), timeout=10)

        row = tasks.get(tid)
        assert row["status"] == "paused"
        assert int(row["attempts"]) == 0
