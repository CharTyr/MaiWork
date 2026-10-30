"""LocalEnv.ephemeral：自动回合的单独工作区（design §3）。

- 进入只起名字不建目录；run/start 用到才建；名字 <prefix>-<毫秒b36>-<随机> 过 _NAME_RE；
- run()/start() 的时长只对登记过的 ephemeral 名夹到本轮剩余时间；过点直接拒；
- 退出（正常/异常/取消）：停掉名下后台进程 + 按精确名删整个目录 + 去登记；
- 清理异常只记日志不抛；CancelledError 不被吞；
- 非 ephemeral 工作区（任务工作区）完全不受夹时长影响；
- systemd 模式只测 argv（本机没有 systemd）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments import local as local_mod
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv


def _settings(root: Path, *, mode: str = "direct", **env_over):
    values = {"local_mode": mode, "workspace_root": str(root), **env_over}
    s, _ = load_settings({"environments": values})
    return s


def _env(root: Path, *, mode: str = "direct", runner=None, **env_over) -> LocalEnv:
    settings = _settings(root, mode=mode, **env_over)
    return LocalEnv(lambda: settings, runner=runner)


def runner_with(exit_code: int = 0, out: str = "", err: str = ""):
    calls: list[dict] = []

    async def runner(argv, *, cwd, env, timeout):
        calls.append({"argv": list(argv), "cwd": cwd, "env": env, "timeout": timeout})
        return exit_code, out, err

    return runner, calls


class TestEnter:
    @pytest.mark.asyncio
    async def test_no_dir_on_enter_created_on_use(self, tmp_path):
        env = _env(tmp_path)
        async with env.ephemeral("rr-news") as ws:
            name = ws.name
            assert ws.parent == tmp_path.resolve()
            assert name.startswith("rr-news-")
            assert local_mod._NAME_RE.match(name)
            assert not ws.exists(), "进入时绝不建目录"
            r = await env.run(name, "echo hi", timeout_s=10)
            assert r.exit_code == 0 and r.stdout.strip() == "hi"
            assert ws.is_dir(), "用到才建"
        assert not ws.exists(), "退出后整个删掉"

    @pytest.mark.asyncio
    async def test_name_unique(self, tmp_path):
        env = _env(tmp_path)
        async with env.ephemeral("rr-idea") as ws1:
            async with env.ephemeral("rr-idea") as ws2:
                assert ws1.name != ws2.name
        assert not ws1.exists() and not ws2.exists()


class TestDeadlineClip:
    @pytest.mark.asyncio
    async def test_run_timeout_clipped_to_remaining(self, tmp_path):
        runner, calls = runner_with()
        env = _env(tmp_path, runner=runner, command_timeout_s=300, runtime_max_sec=1800)
        async with env.ephemeral("rr-goal", deadline_ts=clock.now() + 5) as ws:
            r = await env.run(ws.name, "x", timeout_s=300)
            assert r.exit_code == 0
            assert calls[0]["timeout"] <= 6, f"应夹到剩余秒：{calls[0]['timeout']}"

    @pytest.mark.asyncio
    async def test_run_after_deadline_refused_no_dir(self, tmp_path):
        runner, calls = runner_with()
        env = _env(tmp_path, runner=runner)
        async with env.ephemeral("rr-goal", deadline_ts=clock.now() - 1) as ws:
            r = await env.run(ws.name, "x", timeout_s=10)
            assert r.timed_out is True
            assert "时间到了" in r.stderr or "到点" in r.stderr or "截止" in r.stderr
            assert calls == [], "过点不真跑"
            assert not ws.exists(), "过点连目录都不建"

    @pytest.mark.asyncio
    async def test_start_after_deadline_raises(self, tmp_path):
        env = _env(tmp_path)
        async with env.ephemeral("rr-goal", deadline_ts=clock.now() - 1) as ws:
            with pytest.raises(ValueError, match="时间|截止"):
                await env.start(ws.name, "sleep 1", label="p1", timeout_s=60)

    @pytest.mark.asyncio
    async def test_start_timeout_clipped(self, tmp_path):
        env = _env(tmp_path, runtime_max_sec=1800)
        async with env.ephemeral("rr-idea", deadline_ts=clock.now() + 60) as ws:
            unit = await env.start(ws.name, "sleep 500", label="p1", timeout_s=1500)
            assert unit.startswith(f"maiwork-{ws.name}-")
            st = await env.status(unit)
            assert st["active"] is True
        # 退出后被停掉
        st = await env.status(unit)
        assert st["active"] is False

    @pytest.mark.asyncio
    async def test_task_workspace_unaffected(self, tmp_path):
        """非 ephemeral 名字（任务工作区）：deadline 登记管不到它，时长不夹。"""
        runner, calls = runner_with()
        env = _env(tmp_path, runner=runner, command_timeout_s=200)
        async with env.ephemeral("rr-goal", deadline_ts=clock.now() + 3):
            r = await env.run("T-7", "x", timeout_s=150)
            assert r.exit_code == 0
            assert calls[0]["timeout"] == 150.0, "任务工作区的时长绝不能被 ephemeral 夹"


class TestCleanup:
    @pytest.mark.asyncio
    async def test_exception_inside_still_cleans(self, tmp_path):
        env = _env(tmp_path)
        ws = None
        with pytest.raises(RuntimeError, match="boom"):
            async with env.ephemeral("rr-news") as w:
                ws = w
                await env.run(w.name, "touch f.txt", timeout_s=10)
                raise RuntimeError("boom")
        assert ws is not None and not ws.exists()
        assert ws.name not in env._deadlines  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_cancelled_still_cleans_and_reraises(self, tmp_path):
        env = _env(tmp_path)
        ws = None

        async def inside():
            nonlocal ws
            async with env.ephemeral("rr-news") as w:
                ws = w
                await env.run(w.name, "touch f.txt", timeout_s=10)
                await asyncio.sleep(60)

        task = asyncio.ensure_future(inside())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ws is not None and not ws.exists(), "取消也要删工作区"
        assert ws.name not in env._deadlines  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_background_process_stopped_on_exit(self, tmp_path):
        env = _env(tmp_path)
        async with env.ephemeral("rr-news") as ws:
            unit = await env.start(ws.name, "sleep 300", label="bg", timeout_s=300)
            assert (await env.status(unit))["active"] is True
        assert (await env.status(unit))["active"] is False

    @pytest.mark.asyncio
    async def test_cleanup_error_logged_not_raised(self, tmp_path, monkeypatch):
        env = _env(tmp_path)

        async def bad_stop_all(name):
            raise OSError("stop 全炸")

        monkeypatch.setattr(env, "stop_all", bad_stop_all)
        async with env.ephemeral("rr-news") as ws:
            await env.run(ws.name, "touch f.txt", timeout_s=10)
        assert not ws.exists()  # stop_all 炸了，目录删除照样做

    @pytest.mark.asyncio
    async def test_delete_missing_dir_no_error(self, tmp_path):
        env = _env(tmp_path)
        async with env.ephemeral("rr-news") as ws:
            assert not ws.exists()  # 全程没用到 → 退出时也不许报错


class TestSystemdArgv:
    """systemd 模式只测 argv；本机没有 systemd，全部走假 runner。"""

    @pytest.mark.asyncio
    async def test_run_clips_runtimemax(self, tmp_path):
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        async with env.ephemeral("rr-goal", deadline_ts=clock.now() + 7) as ws:
            await env.run(ws.name, "true", timeout_s=300)
            argv = calls[0]["argv"]
            rt = [v for i, v in enumerate(argv) if argv[i - 1] == "-p" and v.startswith("RuntimeMaxSec=")]
            assert rt and int(rt[0].split("=")[1]) <= 8

    @pytest.mark.asyncio
    async def test_stop_all_lists_and_stops_units(self, tmp_path):
        async def runner(argv, *, cwd, env, timeout):
            calls.append(list(argv))
            if argv[:2] == ["systemctl", "list-units"]:
                return 0, "maiwork-rr-news-x1-p1.service loaded active running\n", ""
            return 0, "", ""

        calls: list[list[str]] = []
        env = _env(tmp_path, mode="systemd", runner=runner)
        async with env.ephemeral("rr-news") as ws:
            await env.start(ws.name, "sleep 100", label="p1", timeout_s=300)
        list_argv = next(a for a in calls if a[:2] == ["systemctl", "list-units"])
        assert f"maiwork-{ws.name}-*" in list_argv
        assert "--plain" in list_argv and "--no-legend" in list_argv and "--all" in list_argv
        stop_argv = [a for a in calls if a[:2] == ["systemctl", "stop"]]
        assert ["systemctl", "stop", "maiwork-rr-news-x1-p1.service"] in stop_argv or \
               ["systemctl", "stop", "maiwork-rr-news-x1-p1"] in stop_argv

    @pytest.mark.asyncio
    async def test_start_unit_argv_uses_ephemeral_name(self, tmp_path):
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        async with env.ephemeral("rr-idea", deadline_ts=clock.now() + 60) as ws:
            await env.start(ws.name, "sleep 10", label="bg", timeout_s=500)
            argv = calls[0]["argv"]
            assert f"--unit=maiwork-{ws.name}-bg" in argv
            rt = [v for i, v in enumerate(argv) if argv[i - 1] == "-p" and v.startswith("RuntimeMaxSec=")]
            assert rt and int(rt[0].split("=")[1]) <= 61
