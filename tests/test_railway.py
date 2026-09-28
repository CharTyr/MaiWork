"""environments/railway.py + tools_railway.py 单元测试（docs/07-代码接口.md §11.1b）。

railway.new 那边只有真连过才算实测；这里全部注入假 runner 录 argv、喂预定输出
（参照 test_env_local.py 的 systemd 模式做法），只验证：
manifest 解析、每日配额、同时只 1 台、argv 拼法、退出码透传、剩余时间不够拒绝、
put/get、release 清理、子进程环境最小、拒绝理由落 kv。

时间用 monkeypatch clock.now 钉住（NOW）；绝不放行任何真实 ssh。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.environments.railway import Box, RailwayEnv
from CharTyr_MaiWork.store import Store

BJ = timezone(timedelta(hours=8))
NOW = 1_790_000_000.0
# 一份「还剩 58 分钟」的 manifest（UTC ISO 整秒）
_BASE_UTC = clock.bj(NOW).astimezone(timezone.utc).replace(microsecond=0)
EXPIRES_ISO = (_BASE_UTC + timedelta(minutes=58)).strftime("%Y-%m-%dT%H:%M:%SZ")
EXPIRES_TS = (_BASE_UTC + timedelta(minutes=58)).timestamp()


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: NOW)


def _manifest(status: str = "trial_ready", **over: Any) -> str:
    data = {
        "status": status,
        "state": "running",
        "description": "Railway VM",
        "usage": {"run": "ssh railway.new COMMAND_HERE"},
        "preview_url": "https://preview-abcdef.up.railway.app",
        "human_claim_url": "https://railway.com/ssh-signup?code=XYZ_secret",
        "build_expires_at": EXPIRES_ISO,
    }
    data.update(over)
    return json.dumps(data)


def _settings(tmp_path: Path, cfg: dict | None = None) -> Any:
    raw = {"environments": {"railway": True, "workspace_root": str(tmp_path / "ws")}}
    if cfg:
        raw["environments"].update(cfg)
    s, _ = load_settings(raw)
    return s


def _make_env(
    tmp_path: Path,
    *,
    runner: Any = None,
    store: Store | None = None,
    cfg: dict | None = None,
) -> tuple[RailwayEnv, Store, Any]:
    settings = _settings(tmp_path, cfg)
    st = store or Store(tmp_path / "t.db")
    st.migrate()
    return RailwayEnv(lambda: settings, data_dir=tmp_path / "data", store=st, runner=runner), st, settings


def runner_scripted(responses: list[tuple[int, str, str]]):
    """假 runner：按次序返回 (exit_code, stdout, stderr)，录每次调用。"""
    calls: list[dict] = []
    queue = list(responses)

    async def runner(argv, *, cwd, env, timeout):
        calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout": timeout})
        if queue:
            return queue.pop(0)
        return 0, "", ""

    return runner, calls


def _keygen_and_manifest(manifest: str, *, code: int = 0) -> list[tuple[int, str, str]]:
    """acquire 常规走的两步：ssh-keygen + 不带命令连一次。"""
    return [(0, "", ""), (code, manifest, "")]


def _argv_ssh(calls: list[dict]) -> list[str]:
    """找出那次「不带命令连一次」的 ssh argv。"""
    for c in calls:
        argv = c["argv"]
        if argv and Path(str(argv[0])).name == "ssh":
            return argv
    raise AssertionError(f"没有 ssh 调用：{[c['argv'] for c in calls]}")


# ----------------------------------------------------------------------
# acquire：manifest 解析
# ----------------------------------------------------------------------


class TestAcquireManifest:
    @pytest.mark.asyncio
    async def test_trial_ready_ok(self, tmp_path: Path) -> None:
        runner, _calls = runner_scripted(_keygen_and_manifest(_manifest("trial_ready")))
        env, _store, _settings = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-a")
        assert box is not None
        assert box.job_id == "job-a"
        assert box.key_path.name == "id"
        assert box.expires_ts == pytest.approx(EXPIRES_TS)
        # preview_url 落成对象属性（绝不打印/外传），以后可能要用
        assert box.preview_url == "https://preview-abcdef.up.railway.app"

    @pytest.mark.asyncio
    async def test_trial_starting_ok(self, tmp_path: Path) -> None:
        runner, _calls = runner_scripted(_keygen_and_manifest(_manifest("trial_starting")))
        env, _s, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is not None

    @pytest.mark.asyncio
    async def test_refused_exit13_none_and_reason(self, tmp_path: Path) -> None:
        refused = json.dumps(
            {
                "status": "refused",
                "human_signup_url": "https://railway.com/ssh-signup?code=abc",
                "poll_url": "https://backboard.railway.com/ssh-signup/poll?code=abc",
                "description": "Anonymous visitors are limited. Sign up to keep building.",
            }
        )
        runner, _calls = runner_scripted(_keygen_and_manifest(refused, code=13))
        env, store, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is None
        fail = store.kv_get("railway.last_fail")
        assert isinstance(fail, dict)
        assert fail["reason"] != ""
        assert "refused" in fail["reason"] or "拒绝" in fail["reason"]

    @pytest.mark.asyncio
    async def test_exit13_non_json_stdout_none(self, tmp_path: Path) -> None:
        runner, _calls = runner_scripted(_keygen_and_manifest("网关抖了一下", code=13))
        env, store, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is None
        fail = store.kv_get("railway.last_fail")
        assert fail and fail["reason"]

    @pytest.mark.asyncio
    async def test_bad_json_exit0_none(self, tmp_path: Path) -> None:
        runner, _calls = runner_scripted(_keygen_and_manifest("no json at all", code=0))
        env, store, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is None
        fail = store.kv_get("railway.last_fail")
        assert fail and fail["reason"]

    @pytest.mark.asyncio
    async def test_ssh_error_exit255_none(self, tmp_path: Path) -> None:
        runner, _calls = runner_scripted(_keygen_and_manifest("", code=255))
        env, store, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is None
        fail = store.kv_get("railway.last_fail")
        assert fail and fail["reason"]

    @pytest.mark.asyncio
    async def test_missing_expires_field_none(self, tmp_path: Path) -> None:
        # 没有 build_expires_at 一律按没拿到处理（文档：用它判断是否成功）
        data = json.loads(_manifest("trial_ready"))
        del data["build_expires_at"]
        runner, _calls = runner_scripted(_keygen_and_manifest(json.dumps(data)))
        env, _s, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is None

    @pytest.mark.asyncio
    async def test_key_dir_and_ssh_options(self, tmp_path: Path) -> None:
        runner, calls = runner_scripted(_keygen_and_manifest(_manifest()))
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-k")
        assert box is not None
        # 每个 job 一个目录：data_dir/railway/<job_id>/
        keydir = tmp_path / "data" / "railway" / "job-k"
        assert keydir.is_dir()
        assert (keydir / "id").exists()  # key 文件已由 acquire 落盘
        # 第一步确实去 keygen（记录里第一条）
        assert "ssh-keygen" in " ".join(calls[0]["argv"])
        assert "-t" in calls[0]["argv"] and "ed25519" in calls[0]["argv"]
        # 连一次的 argv：固定的安全选项一个不少
        argv = _argv_ssh(calls)
        joined = " ".join(argv)
        assert "BatchMode=yes" in argv
        assert "IdentitiesOnly=yes" in argv
        assert "-i" in argv and str(keydir / "id") in argv
        assert f"UserKnownHostsFile={keydir / 'known_hosts'}" in joined
        assert "StrictHostKeyChecking=accept-new" in joined
        assert "ConnectTimeout=25" in joined
        assert "ServerAliveInterval=15" in joined
        assert "ServerAliveCountMax=4" in joined
        assert argv[-1] == "railway.new"


# ----------------------------------------------------------------------
# acquire：每日配额 / 同时只 1 台（进程内锁 + kv 标记）
# ----------------------------------------------------------------------


class TestAcquireGuards:
    @pytest.mark.asyncio
    async def test_daily_max_default_2_blocks_third(self, tmp_path: Path) -> None:
        runner, calls = runner_scripted([])
        env, store, _ = _make_env(tmp_path, runner=runner)
        day = clock.day_key(NOW)
        with store.tx() as conn:
            store.kv_set(conn, f"railway.day.{day}", {"count": 2, "acquires": [NOW, NOW]})
        assert (await env.acquire("job-a")) is None
        assert calls == []  # 配额到了直接不申请，一次 ssh 都不许打

    @pytest.mark.asyncio
    async def test_daily_max_configurable(self, tmp_path: Path) -> None:
        runner, calls = runner_scripted(_keygen_and_manifest(_manifest()))
        env, store, _ = _make_env(tmp_path, runner=runner, cfg={"railway_daily_max": 3})
        day = clock.day_key(NOW)
        with store.tx() as conn:
            store.kv_set(conn, f"railway.day.{day}", {"count": 2, "acquires": [NOW, NOW]})
        assert (await env.acquire("job-a")) is not None  # max=3，2 台还能再要
        assert calls  # 这次真的走了 ssh

    @pytest.mark.asyncio
    async def test_successful_acquire_counts_today(self, tmp_path: Path) -> None:
        runner, _ = runner_scripted(_keygen_and_manifest(_manifest()))
        env, store, _ = _make_env(tmp_path, runner=runner)
        assert (await env.acquire("job-a")) is not None
        day = clock.day_key(NOW)
        rec = store.kv_get(f"railway.day.{day}")
        assert isinstance(rec, dict) and int(rec.get("count", 0)) == 1
        assert len(rec.get("acquires") or []) == 1

    @pytest.mark.asyncio
    async def test_busy_marker_blocks_second_box(self, tmp_path: Path) -> None:
        """kv 里已有没过期的一台 → 不再申请。"""
        runner, calls = runner_scripted(_keygen_and_manifest(_manifest()))
        env, store, _ = _make_env(tmp_path, runner=runner)
        with store.tx() as conn:
            store.kv_set(conn, "railway.active", {"job_id": "other", "expires_ts": NOW + 600})
        assert (await env.acquire("job-a")) is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_stale_busy_marker_released(self, tmp_path: Path) -> None:
        """kv 里那台过期了 → 自动释放，可以继续申请。"""
        runner, calls = runner_scripted(_keygen_and_manifest(_manifest()))
        env, store, _ = _make_env(tmp_path, runner=runner)
        with store.tx() as conn:
            store.kv_set(conn, "railway.active", {"job_id": "old", "expires_ts": NOW - 5})
        assert (await env.acquire("job-a")) is not None
        assert calls

    @pytest.mark.asyncio
    async def test_process_lock_blocks_concurrent_acquire(self, tmp_path: Path) -> None:
        """同一进程同时只许 1 台：占着期间，第二个 acquire 直接 None（不再打 ssh）。"""
        runner, _ = runner_scripted(_keygen_and_manifest(_manifest()) * 2)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-1")
        assert box is not None
        # 还没 release：同一 RailwayEnv 实例再来一个 job 也拿不到
        assert (await env.acquire("job-2")) is None
        await env.release(box)
        # release 之后同实例能再要（还剩 1 台配额）
        box2 = await env.acquire("job-3")
        assert box2 is not None


# ----------------------------------------------------------------------
# run：argv 拼法、退出码透传、剩余时间不够拒绝、子进程环境最小
# ----------------------------------------------------------------------


class TestRun:
    @pytest.mark.asyncio
    async def test_run_argv_and_exit_code(self, tmp_path: Path) -> None:
        responses = _keygen_and_manifest(_manifest()) + [(42, "hello", "warn")]
        runner, calls = runner_scripted(responses)
        env, _s, _settings = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-r")
        assert box is not None
        result = await env.run(box, "echo 'a b'; uname -a", timeout_s=60)
        assert result.exit_code == 42  # 远端退出码原样回传
        assert result.stdout == "hello"
        assert result.stderr == "warn"
        assert result.timed_out is False
        argv = calls[-1]["argv"]
        assert argv[0] == "ssh"
        assert "railway.new" in argv
        assert "--" in argv
        idx = argv.index("--")
        # ssh … railway.new -- bash -lc <command>
        assert argv[idx - 1] == "railway.new"
        assert argv[idx + 1 : idx + 3] == ["bash", "-lc"]
        assert argv[idx + 3] == "echo 'a b'; uname -a"  # 命令原样一段，不经本地 shell

    @pytest.mark.asyncio
    async def test_run_rejects_when_remaining_insufficient(self, tmp_path: Path) -> None:
        """剩余时间 < timeout_s + 120 秒就拒（解释「机器快到期了」）、不打 ssh。"""
        soon = (_BASE_UTC + timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        responses = _keygen_and_manifest(_manifest(build_expires_at=soon))
        runner, calls = runner_scripted(responses)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-e")
        assert box is not None
        before = len(calls)
        result = await env.run(box, "echo hi", timeout_s=60)  # 60+120=180 > 100
        assert result.exit_code != 0
        assert "到期" in result.stderr or "剩余" in result.stderr
        assert len(calls) == before  # 没再打 ssh

    @pytest.mark.asyncio
    async def test_run_timeout_guard(self, tmp_path: Path) -> None:
        """runner 自己抛 TimeoutError → RunResult.timed_out。"""
        responses = _keygen_and_manifest(_manifest())
        runner, _calls = runner_scripted(responses)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-t")
        assert box is not None

        async def stuck(argv, *, cwd, env, timeout):
            raise asyncio.TimeoutError()

        env._runner = stuck
        result = await env.run(box, "sleep 999", timeout_s=300)
        assert result.timed_out is True
        assert result.exit_code == -1

    @pytest.mark.asyncio
    async def test_run_minimal_env_no_leak(self, tmp_path: Path) -> None:
        """ssh 子进程只给最小环境：不继承插件进程的变量（防密钥外泄）。"""
        responses = _keygen_and_manifest(_manifest()) + [(0, "ok", "")]
        runner, calls = runner_scripted(responses)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-env")
        assert box is not None
        await env.run(box, "true", timeout_s=30)
        run_env = calls[-1]["env"]
        # 只放白名单小集合；密钥相关一律不在
        assert set(run_env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TERM"}
        for k in run_env:
            assert not any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASS")), f"环境变量泄了 {k}"
        # acquire 那次连一次同样最小
        acq_env = calls[1]["env"]
        assert set(acq_env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TERM"}


# ----------------------------------------------------------------------
# put / get / release
# ----------------------------------------------------------------------


class TestFileOps:
    @pytest.mark.asyncio
    async def test_put_get_argv(self, tmp_path: Path) -> None:
        responses = _keygen_and_manifest(_manifest()) + [(0, "", ""), (0, "", "")]
        runner, calls = runner_scripted(responses)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-f")
        assert box is not None
        local_up = tmp_path / "up.txt"
        local_up.write_text("hi")
        local_dn = tmp_path / "dn.txt"
        await env.put(box, local_up, "/app/up.txt")
        await env.get(box, "/app/job.log", local_dn)
        put_argv, get_argv = calls[-2]["argv"], calls[-1]["argv"]
        assert put_argv[0] == "scp" and get_argv[0] == "scp"
        assert str(local_up) in put_argv
        assert "railway.new:/app/up.txt" in put_argv
        assert "railway.new:/app/job.log" in get_argv
        assert str(local_dn) in get_argv
        for argv in (put_argv, get_argv):
            assert "BatchMode=yes" in argv
            assert "IdentitiesOnly=yes" in argv
        assert local_dn.exists()  # 本地目标按约定路径落盘

    @pytest.mark.asyncio
    async def test_put_quotes_remote_path(self, tmp_path: Path) -> None:
        responses = _keygen_and_manifest(_manifest()) + [(0, "", "")]
        runner, calls = runner_scripted(responses)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-q")
        assert box is not None
        nasty = tmp_path / "a file;rm -rf.txt"
        nasty.write_text("x")
        await env.put(box, nasty, "/app/a file.txt")
        argv = calls[-1]["argv"]
        assert str(nasty) in argv  # 本地路径原样一段（不经 shell）
        remote_seg = [a for a in argv if str(a).startswith("railway.new:")][0]
        assert "'" in remote_seg or '"' in remote_seg  # 远端路径含空格必须被 quote

    @pytest.mark.asyncio
    async def test_release_removes_keydir_and_frees_lock(self, tmp_path: Path) -> None:
        responses = _keygen_and_manifest(_manifest())
        runner, _ = runner_scripted(responses)
        env, store, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-d")
        assert box is not None
        keydir = tmp_path / "data" / "railway" / "job-d"
        assert keydir.is_dir()
        await env.release(box)
        assert not keydir.exists()  # 本地删 key 目录
        assert store.kv_get("railway.active", None) in (None, {}, "")

    @pytest.mark.asyncio
    async def test_release_idempotent(self, tmp_path: Path) -> None:
        responses = _keygen_and_manifest(_manifest())
        runner, _ = runner_scripted(responses)
        env, _s, _ = _make_env(tmp_path, runner=runner)
        box = await env.acquire("job-i")
        assert box is not None
        await env.release(box)
        await env.release(box)  # 第二次也不炸


# ----------------------------------------------------------------------
# tools_railway.py：vm_run / vm_put_file / vm_read_file（worker 角色）
# ----------------------------------------------------------------------


async def _tools_with_box(tmp_path: Path, box: Box | None):
    from CharTyr_MaiWork.tools import Tools
    from CharTyr_MaiWork.tools_railway import register_vm_tools

    store = Store(tmp_path / "tools.db")
    store.migrate()
    tools = Tools(store)
    cell: dict[str, Any] = {"box": box}
    env_calls: list[dict] = []

    class FakeRailEnv:
        async def run(self, _box, command, *, timeout_s):
            env_calls.append({"cmd": command, "timeout_s": timeout_s})
            from CharTyr_MaiWork.environments.local import RunResult

            return RunResult(exit_code=0, stdout="OUT", stderr="ERR", ms=5, timed_out=False, oom=False)

        async def put(self, _box, local_path, remote_path):
            env_calls.append({"put": (str(local_path), str(remote_path))})

        async def get(self, _box, remote_path, local_path):
            env_calls.append({"get": (str(remote_path), str(local_path))})
            Path(local_path).write_text("REMOTE CONTENT")

    register_vm_tools(tools, get_box=lambda: cell["box"], env=FakeRailEnv())
    return tools, store, env_calls


class TestVmTools:
    @pytest.mark.asyncio
    async def test_vm_run_passes_command_and_caps_timeout(self, tmp_path: Path) -> None:
        box = Box(job_id="j", key_path=tmp_path / "id", expires_ts=NOW + 3600, preview_url="")
        tools, _store, env_calls = await _tools_with_box(tmp_path, box)
        from CharTyr_MaiWork.tools import ToolContext

        ctx = ToolContext(group_id="111", actor="子 agent", role="worker")
        r = await tools.call("vm_run", {"command": "ls -la /app", "timeout_s": 600}, ctx)
        assert r.ok
        assert env_calls[0]["cmd"] == "ls -la /app"
        assert env_calls[0]["timeout_s"] == 600
        assert "OUT" in r.output
        # 超时上限 600：超了夹回 600
        r2 = await tools.call("vm_run", {"command": "sleep 1", "timeout_s": 99999}, ctx)
        assert r2.ok
        assert env_calls[-1]["timeout_s"] == 600

    @pytest.mark.asyncio
    async def test_vm_run_without_box_fails_chinese(self, tmp_path: Path) -> None:
        tools, _store, _ = await _tools_with_box(tmp_path, None)
        from CharTyr_MaiWork.tools import ToolContext

        ctx = ToolContext(group_id="111", actor="子 agent", role="worker")
        r = await tools.call("vm_run", {"command": "ls"}, ctx)
        assert not r.ok
        assert "虚拟机" in r.error or "VM" in r.error or "机器" in r.error

    @pytest.mark.asyncio
    async def test_vm_roles_worker_only(self, tmp_path: Path) -> None:
        box = Box(job_id="j", key_path=tmp_path / "id", expires_ts=NOW + 3600, preview_url="")
        tools, _store, _ = await _tools_with_box(tmp_path, box)
        from CharTyr_MaiWork.tools import ToolContext

        main_ctx = ToolContext(group_id="111", actor="主模型", role="main")
        r = await tools.call("vm_run", {"command": "ls"}, main_ctx)
        assert not r.ok and "子 agent" in r.error

    @pytest.mark.asyncio
    async def test_vm_put_file_only_workspace_file(self, tmp_path: Path) -> None:
        """只许传工作区里的一个文件，落 VM 的 /app 下；越界（..、绝对路径）拒。"""
        box = Box(job_id="j", key_path=tmp_path / "id", expires_ts=NOW + 3600, preview_url="")
        tools, _store, env_calls = await _tools_with_box(tmp_path, box)
        from CharTyr_MaiWork.tools import ToolContext

        ws = tmp_path / "wsa"
        ws.mkdir()
        (ws / "script.py").write_text("print(1)")
        ctx = ToolContext(group_id="111", actor="子 agent", role="worker", workspace=ws)
        r = await tools.call("vm_put_file", {"path": "script.py", "remote_name": "s.py"}, ctx)
        assert r.ok
        assert env_calls[-1]["put"][1] == "/app/s.py"
        # 越界拒收
        r2 = await tools.call("vm_put_file", {"path": "../../etc/passwd"}, ctx)
        assert not r2.ok

    @pytest.mark.asyncio
    async def test_vm_read_file(self, tmp_path: Path) -> None:
        box = Box(job_id="j", key_path=tmp_path / "id", expires_ts=NOW + 3600, preview_url="")
        tools, _store, env_calls = await _tools_with_box(tmp_path, box)
        from CharTyr_MaiWork.tools import ToolContext

        ctx = ToolContext(group_id="111", actor="子 agent", role="worker")
        r = await tools.call("vm_read_file", {"path": "/app/job.log"}, ctx)
        assert r.ok
        assert "REMOTE CONTENT" in r.output
        assert env_calls[-1]["get"][0] == "/app/job.log"

    @pytest.mark.asyncio
    async def test_vm_read_file_truncates_at_20000(self, tmp_path: Path) -> None:
        """读 VM 文件超过 20000 字截断（带提示）。"""
        from CharTyr_MaiWork.tools import ToolContext, Tools
        from CharTyr_MaiWork.tools_railway import register_vm_tools

        store = Store(tmp_path / "t2.db")
        store.migrate()
        tools = Tools(store)

        class FakeRailEnv2:
            async def get(self, _box, remote_path, local_path):
                Path(local_path).write_text("x" * 25000)

            async def run(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
                raise AssertionError

            async def put(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
                raise AssertionError

        real_box = Box(job_id="j", key_path=tmp_path / "id", expires_ts=NOW + 3600, preview_url="")
        register_vm_tools(tools, get_box=lambda: real_box, env=FakeRailEnv2())
        ctx = ToolContext(group_id="111", actor="子 agent", role="worker")
        r = await tools.call("vm_read_file", {"path": "/app/big.log"}, ctx)
        assert r.ok
        assert len(r.output) <= 20000 + 200  # 截 20000 字 + 一句截断提示
        assert "截" in r.output
