"""两家一次性 VM 同时开（2026-10-15 用户定：Freestyle 是额外添加，不是二选一）。

- Railway、Freestyle 各有开关；`vm_platform` 改成「优先用哪家」；
- 排计划只给主模型一个「一次性机器」选项（优先那家），不让模型挑平台；
- 优先那家拿不到 → 自动换另一家，再不行才专用机器 / 本机；
- 同一时间只占一台一次性机器（两家合计），子 agent 的 vm_* 工具按 box.kind 分派到对的那家；
- 老配置（没有 freestyle 开关）读出来不变：只开 Railway。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_coordinator_env import (  # noqa: F401  (fixtures)
    FakeFreestyleEnv,
    FakeRailwayEnv,
    FakeSshEnv,
    FakeWorkers,
    ModelsQueue,
    _SshBox,
    _Settings,
    _create_task,
    _plan,
    _review,
    env,
    fixed_clock,
    goals,
    mem_store,
    settings,
    tasks,
    tools,
)

from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.environments import vm_platforms
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools

pytestmark = pytest.mark.asyncio


def _both(tmp_path: Path, *, prefer: str = "railway", freestyle_key: str = "fs-test-key") -> _Settings:
    s = _Settings(tmp_path / "ws", vm_platform=prefer, freestyle_api_key=freestyle_key)
    s.environments.freestyle = True
    return s


# ---------------------------------------------------------------------------
# 注册表：开关 + 优先顺序
# ---------------------------------------------------------------------------


async def test_enabled_kinds_preferred_first(tmp_path: Path) -> None:
    assert vm_platforms.enabled_kinds(_both(tmp_path)) == ("railway", "freestyle")
    assert vm_platforms.enabled_kinds(_both(tmp_path, prefer="freestyle")) == ("freestyle", "railway")
    only_rw = _Settings(tmp_path / "ws")
    only_rw.environments.freestyle = False
    assert vm_platforms.enabled_kinds(only_rw) == ("railway",)
    assert vm_platforms.get("freestyle").enabled(_both(tmp_path))
    off = _both(tmp_path)
    off.environments.railway = False
    assert vm_platforms.enabled_kinds(off) == ("freestyle",)


async def test_config_freestyle_switch_default_off() -> None:
    from CharTyr_MaiWork.maiwork.config import load_settings
    from test_rules_web import _raw_config  # 现成的最小配置

    def parse_settings(r):
        return load_settings(r)[0]

    raw = _raw_config(Path("/tmp/mw-vm-both"))
    s = parse_settings(raw)
    assert s.environments.freestyle is False          # 老配置：只开 Railway
    assert vm_platforms.enabled_kinds(s) == ("railway",)
    raw2 = dict(raw)
    raw2["environments"] = {**dict(raw.get("environments") or {}), "freestyle": True,
                            "freestyle_api_key": "k", "vm_platform": "freestyle"}
    s2 = parse_settings(raw2)
    assert s2.environments.freestyle is True
    assert vm_platforms.enabled_kinds(s2) == ("freestyle", "railway")
    # 二选一那版存下的「选 Freestyle」（没有 freestyle 开关）→ 只开 Freestyle，行为不变
    raw3 = dict(raw)
    raw3["environments"] = {**dict(raw.get("environments") or {}), "vm_platform": "freestyle"}
    raw3["environments"].pop("freestyle", None)
    raw3["environments"].pop("railway", None)
    assert vm_platforms.enabled_kinds(parse_settings(raw3)) == ("freestyle",)


# ---------------------------------------------------------------------------
# 分派器：一个 vm_* 工具面对两家
# ---------------------------------------------------------------------------


async def test_router_routes_by_box_kind_and_single_box(tmp_path: Path) -> None:
    rw, fs = FakeRailwayEnv(), FakeFreestyleEnv()
    router = vm_platforms.VMRouter({"railway": rw, "freestyle": fs})
    assert router.current_box is None
    fbox = await fs.acquire("T-1")
    assert router.current_box is fbox
    assert router.busy_kind() == "freestyle"
    await router.run(fbox, "echo hi", timeout_s=5)
    assert rw.run_calls == []                       # 没跑到 railway 上去
    # 单条命令上限：跟着当前机器那家走（Freestyle 280 < railway 600）
    fs.max_run_timeout_s = 280
    rw.max_run_timeout_s = 600
    assert router.max_run_timeout_s == 280


# ---------------------------------------------------------------------------
# coordinator：只给一个选项、拿不到换另一家、同一时间只占一台
# ---------------------------------------------------------------------------


def _build_both(mem_store, settings, env, tools, tasks, goals, models, workers, rw, fs, ssh=None):
    register_exec_tools(tools, env=env, host=None, get_settings=lambda: settings, session_of=lambda _g: "s")
    router = vm_platforms.VMRouter({"railway": rw, "freestyle": fs})
    register_vm_tools(tools, get_box=lambda: router.current_box, env=router, local_env=env)
    from test_coordinator_env import FakeDelivery, FakeOutbox, _Profiles

    return Coordinator(
        mem_store, models, workers, tools, tasks, goals, FakeDelivery(), FakeOutbox(), env,
        _Profiles(), lambda: settings, railway=rw, ssh=ssh, vms={"freestyle": fs}, vm_router=router,
    )


async def _run(mem_store, env, tools, tasks, goals, settings, rw, fs, *, plan_env, ssh=None):
    tid = _create_task(tasks)
    models = ModelsQueue(replies=[_plan(env=plan_env), _review(pass_=True)])
    workers = FakeWorkers()

    async def _write():
        ws = env.workspace(tasks.get(tid)["workspace"])
        (ws / "artifacts" / tid).mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / tid / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    workers.before_return = _write
    c = _build_both(mem_store, settings, env, tools, tasks, goals, models, workers, rw, fs, ssh=ssh)
    await c.run_task(tid)
    return tid, models, c


async def test_plan_offers_only_preferred_platform(mem_store, tmp_path, env, tools, tasks, goals) -> None:
    settings = _both(tmp_path, prefer="freestyle")
    rw, fs = FakeRailwayEnv(), FakeFreestyleEnv()
    _tid, models, c = await _run(mem_store, env, tools, tasks, goals, settings, rw, fs, plan_env="freestyle")
    prompt = "\n".join(str(m.get("content")) for m in models.calls[0][1])
    assert '"env": "local|freestyle"' in prompt and "railway" not in prompt
    # 模型硬写另一家 → 规范成优先那家
    assert c._normalize_env_choice("railway") == "freestyle"  # noqa: SLF001
    assert len(fs.acquire_calls) == 1 and rw.acquire_calls == []


async def test_preferred_unavailable_falls_back_to_other_platform(
    mem_store, tmp_path, env, tools, tasks, goals
) -> None:
    settings = _both(tmp_path, prefer="freestyle")
    rw, fs = FakeRailwayEnv(give_box=True), FakeFreestyleEnv(give_box=False)
    ssh = FakeSshEnv(box=_SshBox())
    tid, _m, _c = await _run(mem_store, env, tools, tasks, goals, settings, rw, fs, plan_env="freestyle", ssh=ssh)
    assert tasks.get(tid)["status"] == "completed"
    assert len(fs.acquire_calls) == 1 and len(rw.acquire_calls) == 1
    assert ssh.acquire_calls == []                  # 另一家一次性机器先于专用机器
    assert rw.release_calls == 1
    row = tasks.get(tid)
    assert "railway" in str(row["env"])


async def test_only_one_oneshot_box_at_a_time(mem_store, tmp_path, env, tools, tasks, goals) -> None:
    settings = _both(tmp_path, prefer="railway")
    rw, fs = FakeRailwayEnv(give_box=True), FakeFreestyleEnv(give_box=True)
    await rw.acquire("T-other")                      # 别的任务正占着一台 Railway
    c = _build_both(mem_store, settings, env, tools, tasks, goals, ModelsQueue(), FakeWorkers(), rw, fs)
    box, reason = await c._acquire_remote("freestyle", "T-9")  # noqa: SLF001
    assert box is None and "占" in reason
    assert fs.acquire_calls == []                    # 没再去另一家申请第二台


async def test_freestyle_without_key_not_offered(mem_store, tmp_path, env, tools, tasks, goals) -> None:
    settings = _both(tmp_path, prefer="freestyle", freestyle_key="")
    rw, fs = FakeRailwayEnv(), FakeFreestyleEnv()
    c = _build_both(mem_store, settings, env, tools, tasks, goals, ModelsQueue(), FakeWorkers(), rw, fs)
    assert c._oneshot_kinds() == ("railway",)  # noqa: SLF001


async def test_concurrent_acquire_never_takes_two_boxes(tmp_path: Path) -> None:
    """两个任务同时申请（一个要 railway、一个要 freestyle）：分派器那把锁保证只拿到一台。"""
    import asyncio

    class Slow(FakeRailwayEnv):
        async def acquire(self, job_id):
            await asyncio.sleep(0.05)
            return await super().acquire(job_id)

    class SlowFs(FakeFreestyleEnv):
        async def acquire(self, job_id):
            await asyncio.sleep(0.05)
            return await super().acquire(job_id)

    rw, fs = Slow(), SlowFs()
    router = vm_platforms.VMRouter({"railway": rw, "freestyle": fs})
    (b1, _), (b2, busy2) = await asyncio.gather(router.acquire("railway", "A"), router.acquire("freestyle", "B"))
    assert b1 is not None and b2 is None and busy2 == "railway"
    # 实测用的「守规矩」版本：acquire 也走那把锁，其余照原样委托
    ex = router.exclusive("freestyle")
    assert await ex.acquire("V") is None
    assert ex.last_fail()["reason"]


async def test_freestyle_without_key_reason_says_key_missing(mem_store, tmp_path, env, tools, tasks, goals) -> None:
    """M6（2026-10-15 巡检）：开着 Freestyle 但没填密钥，告诉主模型 / 管理员的原因要是「没填密钥」，不是「配置关了」。"""
    settings = _both(tmp_path, prefer="freestyle", freestyle_key="")
    rw, fs = FakeRailwayEnv(), FakeFreestyleEnv()
    c = _build_both(mem_store, settings, env, tools, tasks, goals, ModelsQueue(), FakeWorkers(), rw, fs)
    box, reason = await c._acquire_remote("freestyle", "T-9")  # noqa: SLF001
    assert box is None and "密钥" in reason and "配置关了" not in reason
    assert fs.acquire_calls == []
