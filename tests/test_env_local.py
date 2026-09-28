"""environments/local.py 单元测试（M3，docs/07-代码接口.md §11.1）。

本机是 macOS：没有 systemd、没有 maiwork 用户。所以：
- systemd 模式只测「拼出来的 argv 对不对」（注入假 runner 录参数，不真跑）；
- direct 模式真跑（echo、超时、截断、后台进程）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.environments.local import LocalEnv

ON_MACOS = sys.platform == "darwin"


def _settings(root: Path, *, mode: str = "direct"):
    s, _ = load_settings(
        {"environments": {"local_mode": mode, "workspace_root": str(root)}}
    )
    return s


def _env(root: Path, *, mode: str = "direct", runner=None) -> LocalEnv:
    settings = _settings(root, mode=mode)
    return LocalEnv(lambda: settings, runner=runner)


def runner_with(exit_code: int = 0, out: str = "", err: str = ""):
    """假 runner：录下 argv/cwd/env/timeout，返回预定结果。"""
    calls: list[dict] = []

    async def runner(argv, *, cwd, env, timeout):
        calls.append({"argv": list(argv), "cwd": cwd, "env": env, "timeout": timeout})
        return exit_code, out, err

    return runner, calls


class TestRunnerPlumbing:
    @pytest.mark.asyncio
    async def test_default_runner_direct_real_echo(self, tmp_path: Path) -> None:
        """不注入 runner 时（direct）真跑子进程：证明默认管道本身是通的。"""
        env = _env(tmp_path)
        r = await env.run("ws", "echo ok", timeout_s=10)
        assert r.exit_code == 0
        assert r.stdout.strip() == "ok"

    def test_runner_type_mismatch_raises_valueerror(self, tmp_path: Path) -> None:
        env = _env(tmp_path)

        async def wrong_sig(argv):  # 缺 cwd / env / timeout 三个关键参数
            return 0, "", ""  # pragma: no cover - 不会被调用

        def sync_runner(argv, *, cwd, env, timeout):  # 不是协程函数
            return 0, "", ""  # pragma: no cover

        with pytest.raises(ValueError):
            env._check_runner(wrong_sig)
        with pytest.raises(ValueError):
            env._check_runner(sync_runner)


class TestWorkspace:
    def test_creates_workspace_and_subdirs(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("team-a")
        assert ws == (tmp_path / "team-a").resolve()
        for sub in ("tasks", "artifacts", "tools", "runtime"):
            assert (ws / sub).is_dir(), f"缺子目录 {sub}"

    def test_idempotent_on_existing(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("team-a")
        (tmp_path / "team-a" / "runtime" / "x.txt").write_text("hi")
        ws = env.workspace("team-a")
        assert (ws / "runtime" / "x.txt").read_text() == "hi"

    @pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "中文名", "含 空格", "a;b", "x" * 65, "./x"])
    def test_bad_name_raises_valueerror(self, tmp_path: Path, bad: str) -> None:
        env = _env(tmp_path)
        with pytest.raises(ValueError):
            env.workspace(bad)

    @pytest.mark.parametrize("good", ["a", "A0_-z", "g900000001", "x" * 64])
    def test_legal_names(self, tmp_path: Path, good: str) -> None:
        env = _env(tmp_path)
        assert env.workspace(good).name == good

    @pytest.mark.skipif(not ON_MACOS, reason="本机 macOS 非 root 下验证不 chown 也不炸；线上 chown 留待实测")
    def test_systemd_mode_skips_chown_when_not_root(self, tmp_path: Path) -> None:
        """systemd 模式、非 root（本机开发机）时不 chown，也不报错。"""
        env = _env(tmp_path, mode="systemd")
        ws = env.workspace("team-a")
        assert ws.is_dir()


class TestResolve:
    def test_normal_subpath(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        assert env.resolve("ws", "tasks/t1/a.txt") == ws / "tasks" / "t1" / "a.txt"

    def test_empty_rel_is_workspace_itself(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        assert env.resolve("ws", "") == env.workspace("ws")

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("ws")
        with pytest.raises(PermissionError):
            env.resolve("ws", "/etc/passwd")

    def test_dotdot_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("ws")
        for rel in ("../etc", "a/../../b", ".."):
            with pytest.raises(PermissionError):
                env.resolve("ws", rel)

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """工作区里的符号链接指向外面 → 拒绝（真实符号链接验证，不依赖 systemd）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("shh")
        env = _env(tmp_path)
        ws = env.workspace("ws")
        (ws / "links").mkdir()
        (ws / "links" / "evil.txt").symlink_to(outside / "secret.txt")
        with pytest.raises(PermissionError):
            env.resolve("ws", "links/evil.txt")

    def test_symlink_inside_workspace_ok(self, tmp_path: Path) -> None:
        """指向工作区内部的链接不算逃逸。"""
        env = _env(tmp_path)
        ws = env.workspace("ws")
        (ws / "real").mkdir()
        (ws / "real" / "a.txt").write_text("x")
        (ws / "alias.txt").symlink_to(ws / "real" / "a.txt")
        assert env.resolve("ws", "alias.txt") == ws / "real" / "a.txt"

    def test_bad_workspace_name_raises_valueerror(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(ValueError):
            env.resolve("..", "x")


class TestRunSystemdArgv:
    """systemd 模式只测 argv/环境/超时配置对不对；本机没有 systemd，不真跑。"""

    @pytest.mark.asyncio
    async def test_argv_exact_and_min_env(self, tmp_path: Path) -> None:
        runner, calls = runner_with(exit_code=0, out="done")
        env = _env(tmp_path, mode="systemd", runner=runner)
        r = await env.run("ws", "echo hello", timeout_s=60)
        assert r.exit_code == 0 and r.stdout == "done"
        assert len(calls) == 1
        argv = calls[0]["argv"]
        assert argv[0] == "systemd-run"
        assert "--wait" in argv and "--collect" in argv and "--pipe" in argv
        assert "--quiet" not in argv  # 要靠 stderr 的「Finished with result」判断超内存 / 超时
        # 隔离：系统只读、家目录只读、私有 /tmp、不提权、只挂本工作区（线上 2026-09-27 实测）
        root = str((tmp_path).resolve())
        for prop in ("ProtectSystem=strict", "ProtectHome=read-only", "PrivateTmp=yes", "NoNewPrivileges=yes",
                     f"TemporaryFileSystem={root}", f"BindPaths={(tmp_path / 'ws').resolve()}"):
            assert prop in argv, prop
        assert "--uid=maiwork" in argv and "--gid=maiwork" in argv
        # --setenv：单元内 HOME=工作区、LANG=C.UTF-8、最小 PATH（线上实测不设的话 HOME 是只读的 /home/maiwork）
        ws_line = str((tmp_path / "ws").resolve())
        assert f"--setenv=HOME={ws_line}" in argv
        assert "--setenv=LANG=C.UTF-8" in argv
        assert f"--setenv=PATH={LocalEnv._MINIMAL_PATH}" in argv
        assert argv.index(f"--setenv=HOME={ws_line}") < argv.index("--")  # setenv 必须在 -- 之前
        assert "MemoryMax=512M" in argv and "MemorySwapMax=0" in argv
        assert "RuntimeMaxSec=60" in argv
        ws = str((tmp_path / "ws").resolve())
        assert f"WorkingDirectory={ws}" in argv
        unit_opt = next(a for a in argv if a.startswith("--unit="))
        assert unit_opt.startswith("--unit=maiwork-run-")
        assert len(unit_opt) > len("--unit=maiwork-run-")
        assert argv[-3:] == ["/bin/bash", "-lc", "echo hello"]
        p_idx = argv.index("-p")
        assert argv[p_idx + 1].startswith("MemoryMax=")  # -p 后面紧跟属性名
        # 兜底超时 = 命令上限 + 15 秒
        assert calls[0]["timeout"] == 75.0
        # 环境只有最小集合，不继承插件进程的环境变量（防密钥泄漏）
        env_vars = calls[0]["env"]
        assert env_vars["PATH"] and " " not in env_vars["PATH"]
        assert env_vars["HOME"] == ws
        assert env_vars["LANG"] == "C.UTF-8"
        assert len(env_vars) == 3

    @pytest.mark.asyncio
    async def test_systemd_trailer_oom_and_timeout(self, tmp_path: Path) -> None:
        """线上实测：超内存 / 超时 systemd-run 都退 1，只能看「Finished with result」。"""
        trailer = ("Running as unit: maiwork-run-x.service\nFinished with result: {r}\n"
                   "Main processes terminated with: code=killed, status=9/KILL\nService runtime: 461ms\n"
                   "CPU time consumed: 416ms\nMemory peak: 64M (swap: 0B)\n")
        runner, _ = runner_with(exit_code=1, out="", err="boom\n" + trailer.format(r="oom-kill"))
        r = await _env(tmp_path, mode="systemd", runner=runner).run("ws", "x", timeout_s=10)
        assert r.oom and not r.timed_out and r.stderr.strip() == "boom"
        runner, _ = runner_with(exit_code=1, err=trailer.format(r="timeout"))
        r = await _env(tmp_path, mode="systemd", runner=runner).run("ws", "x", timeout_s=10)
        assert r.timed_out and not r.oom and r.stderr == ""
        runner, _ = runner_with(exit_code=3, err="err\n" + trailer.format(r="exit-code"))
        r = await _env(tmp_path, mode="systemd", runner=runner).run("ws", "x", timeout_s=10)
        assert r.exit_code == 3 and not r.oom and not r.timed_out and r.stderr.strip() == "err"

    @pytest.mark.asyncio
    async def test_run_memory_override(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        await env.run("ws", "true", timeout_s=10, memory="1G")
        assert "MemoryMax=1G" in calls[0]["argv"]

    @pytest.mark.asyncio
    async def test_run_default_timeout_uses_command_timeout_s(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        await env.run("ws", "true")
        assert "RuntimeMaxSec=300" in calls[0]["argv"]
        assert calls[0]["timeout"] == 315.0

    @pytest.mark.asyncio
    async def test_run_timeout_clamped_to_runtime_max(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        await env.run("ws", "true", timeout_s=99999)
        assert "RuntimeMaxSec=1800" in calls[0]["argv"]
        assert calls[0]["timeout"] == 1815.0

    @pytest.mark.asyncio
    async def test_run_cwd_appended_in_bash(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        (tmp_path / "ws" / "tasks").mkdir(parents=True)
        await env.run("ws", "make", timeout_s=10, cwd="tasks")
        assert calls[0]["argv"][-1] == "cd tasks && make"

    @pytest.mark.asyncio
    async def test_run_cwd_missing_dir_raises(self, tmp_path: Path) -> None:
        """cwd 指到工作区内不存在的目录 → FileNotFoundError，不跑命令。"""
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        with pytest.raises(FileNotFoundError):
            await env.run("ws", "make", timeout_s=10, cwd="tasks/t1")
        assert calls == []

    @pytest.mark.asyncio
    async def test_run_cwd_escape_rejected(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        with pytest.raises(PermissionError):
            await env.run("ws", "true", timeout_s=10, cwd="../outside")
        assert calls == []

    @pytest.mark.asyncio
    async def test_run_bad_workspace_name(self, tmp_path: Path) -> None:
        env = _env(tmp_path, mode="systemd", runner=runner_with()[0])
        with pytest.raises(ValueError):
            await env.run("a/b", "true", timeout_s=10)

    @pytest.mark.asyncio
    async def test_oom_flag_from_exit_137(self, tmp_path: Path) -> None:
        runner, _ = runner_with(exit_code=137)
        env = _env(tmp_path, mode="systemd", runner=runner)
        r = await env.run("ws", "eat-memory", timeout_s=10)
        assert r.oom is True

    @pytest.mark.asyncio
    async def test_oom_flag_from_stderr_keywords(self, tmp_path: Path) -> None:
        runner, _ = runner_with(exit_code=1, err="Killed (systemd MemoryMax 触发，进程被 OOM killer 干掉)")
        env = _env(tmp_path, mode="systemd", runner=runner)
        r = await env.run("ws", "x", timeout_s=10)
        assert r.oom is True

    @pytest.mark.asyncio
    async def test_no_oom_on_ordinary_failure(self, tmp_path: Path) -> None:
        runner, _ = runner_with(exit_code=2, err="普通报错")
        env = _env(tmp_path, mode="systemd", runner=runner)
        r = await env.run("ws", "x", timeout_s=10)
        assert r.oom is False

    @pytest.mark.asyncio
    async def test_output_truncated_to_20000(self, tmp_path: Path) -> None:
        """输出保留末尾：20049 字 → 前 49 个字符（含部分「头」）被截掉。"""
        head = "头" * 145
        tail = "尾" * 19900
        runner, _ = runner_with(out=head + tail + "多出来的", err=head + tail + "多出来的")
        env = _env(tmp_path, mode="systemd", runner=runner)
        r = await env.run("ws", "x", timeout_s=10)
        assert len(r.stdout) == 20000 and len(r.stderr) == 20000
        assert r.stdout.endswith("多出来的")  # 末尾完整保留
        # 前 49 个字符被截：头部原本 145 个「头」只剩 96 个
        assert r.stdout.startswith("头" * 96)
        assert not r.stdout.startswith("头" * 97)

    @pytest.mark.asyncio
    async def test_runner_timeout_marks_timed_out_and_kills_unit(self, tmp_path: Path) -> None:
        """外层 asyncio.wait_for 兜底超时：timed_out=True，并开任务 systemctl stop 兜底单元。"""
        import asyncio
        import unittest.mock as mock

        async def slow_runner(argv, *, cwd, env, timeout):
            await asyncio.sleep(30)
            return 0, "", ""

        env = _env(tmp_path, mode="systemd", runner=slow_runner)

        async def wait_for_times_out(coro, timeout):
            coro.close()  # 别留给 GC：mock 的 wait_for 不会真的去 await 它
            raise asyncio.TimeoutError

        def create_task_swallow(coro):
            coro.close()  # 同上：mock 的 create_task 不会调度它
            return mock.MagicMock()

        with mock.patch("CharTyr_MaiWork.environments.local.asyncio.wait_for", side_effect=wait_for_times_out):
            with mock.patch("CharTyr_MaiWork.environments.local.asyncio.get_running_loop") as get_loop:
                created: list[mock.MagicMock] = []

                def create_task_spy(coro):
                    created.append(coro)
                    return mock.MagicMock()

                get_loop.return_value.create_task = create_task_spy
                r = await env.run("ws", "sleep 100", timeout_s=1)
        assert r.timed_out is True
        assert len(created) == 1
        stop_coro = created[0]
        # stop_coro 是 LocalEnv._systemctl_stop(unit, ws) 协程；单元名藏在闭包里
        cells = stop_coro.cr_frame.f_locals
        assert str(cells.get("unit", "")).startswith("maiwork-run-")
        assert stop_coro.cr_code.co_name == "_systemctl_stop"
        stop_coro.close()


class TestRunDirect:
    """direct 模式在 macOS 上真跑（这就是 direct 存在的意义）。"""

    @pytest.mark.asyncio
    async def test_echo_and_cwd(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        with pytest.raises(FileNotFoundError):
            await env.run("ws", "cat ok.txt", timeout_s=10, cwd="sub")  # sub 还不存在
        (ws / "sub").mkdir()
        (ws / "sub" / "ok.txt").write_text("内容")
        r = await env.run("ws", "cat ok.txt", timeout_s=10, cwd="sub")
        assert r.exit_code == 0
        assert r.stdout.strip() == "内容"
        assert r.ms >= 0
        assert r.timed_out is False and r.oom is False

    @pytest.mark.asyncio
    async def test_minimal_env_not_inherited(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAIWORK_SECRET_TEST", "绝密值")
        env = _env(tmp_path)
        r = await env.run("ws", 'echo "[$MAIWORK_SECRET_TEST][$HOME]"', timeout_s=10)
        ws = str((tmp_path / "ws").resolve())
        assert f"[][{ws}]" in r.stdout  # 宿主机变量没漏进去，HOME 是工作区

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        r = await env.run("ws", "sleep 30", timeout_s=1)
        assert r.timed_out is True
        assert r.ms < 15000

    @pytest.mark.asyncio
    async def test_output_truncated_real(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        r = await env.run("ws", "head -c 30000 /dev/zero | tr '\\0' A", timeout_s=20)
        assert r.exit_code == 0
        assert len(r.stdout) == 20000

    @pytest.mark.asyncio
    async def test_stderr_captured(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        r = await env.run("ws", "echo oops >&2; exit 3", timeout_s=10)
        assert r.exit_code == 3
        assert "oops" in r.stderr


class TestReadWriteList:
    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.write_file("ws", "notes/hello.txt", "你好，世界")
        assert await env.read_file("ws", "notes/hello.txt") == "你好，世界"

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.write_file("ws", "deep/deeper/file.txt", "x")
        assert (tmp_path / "ws" / "deep" / "deeper" / "file.txt").is_file()

    @pytest.mark.asyncio
    async def test_append(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.write_file("ws", "a.txt", "1")
        await env.write_file("ws", "a.txt", "2", append=True)
        assert await env.read_file("ws", "a.txt") == "12"

    @pytest.mark.asyncio
    async def test_append_over_5mb_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.write_file("ws", "big.txt", "x" * (5 * 1024 * 1024))  # 恰好 5MB 行得通
        with pytest.raises(ValueError):
            await env.write_file("ws", "big.txt", "y", append=True)  # 再补 1 字节就超了

    @pytest.mark.asyncio
    async def test_write_over_5mb_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(ValueError):
            await env.write_file("ws", "big.txt", "x" * (5 * 1024 * 1024 + 1))

    @pytest.mark.asyncio
    async def test_write_escape_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(PermissionError):
            await env.write_file("ws", "../evil.txt", "x")

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(FileNotFoundError):
            await env.read_file("ws", "nope.txt")

    @pytest.mark.asyncio
    async def test_read_is_dir(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        (ws / "adir").mkdir()
        with pytest.raises(IsADirectoryError):
            await env.read_file("ws", "adir")

    @pytest.mark.asyncio
    async def test_read_max_bytes_and_replace_errors(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        # 前 100 字节是合法文本，后面塞非法 utf-8 字节
        (ws / "mixed.bin").write_bytes(b"a" * 100 + b"\xff\xfe" + b"b" * 400)
        text = await env.read_file("ws", "mixed.bin", max_bytes=120)
        assert len(text.encode("utf-8", errors="replace")) <= 120 + 8
        assert "�" in text  # 非法字节被替换，不炸

    @pytest.mark.asyncio
    async def test_list_files_tree_and_flags(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        (ws / "tasks" / "t1").mkdir(parents=True)
        (ws / "tasks" / "t1" / "out.txt").write_text("1234")
        entries = await env.list_files("ws", depth=3)
        by_path = {e["path"]: e for e in entries}
        assert by_path["tasks"]["is_dir"] is True
        assert by_path["tasks/t1"]["is_dir"] is True
        assert by_path["tasks/t1/out.txt"]["is_dir"] is False
        assert by_path["tasks/t1/out.txt"]["size"] == 4

    @pytest.mark.asyncio
    async def test_list_files_depth_limit(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        deep = ws / "a" / "b" / "c"
        deep.mkdir(parents=True)
        entries = await env.list_files("ws", depth=2)
        paths = {e["path"] for e in entries}
        assert "a/b" in paths
        assert "a/b/c" not in paths
        entries = await env.list_files("ws", depth=5)
        assert "a/b/c" in {e["path"] for e in entries}

    @pytest.mark.asyncio
    async def test_list_files_count_limit(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws")
        for i in range(10):
            (ws / f"f{i}.txt").write_text("x")
        entries = await env.list_files("ws", rel="", depth=1, limit=5)
        assert len(entries) == 5

    @pytest.mark.asyncio
    async def test_list_files_outside_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(PermissionError):
            await env.list_files("ws", rel="..")


class TestDirectProcessRegistry:
    """direct 模式的 start/status/logs/stop：内存登记 + 真子进程。"""

    @pytest.mark.asyncio
    async def test_start_status_logs_stop(self, tmp_path: Path) -> None:
        """长命进程：启动 → active → stop 后 inactive；写日志由独立用例覆盖。"""
        env = _env(tmp_path)
        unit = await env.start("ws", "sleep 60", label="watcher", timeout_s=120)
        assert unit == "maiwork-ws-watcher"
        st = await env.status(unit)
        assert st["active"] is True
        await env.stop(unit)
        st = await env.status(unit)
        assert st["active"] is False
        # 日志文件真落在工作区 runtime/logs 下（内容为空也算：这个进程没写东西）
        assert (tmp_path / "ws" / "runtime" / "logs" / "watcher.log").is_file()

    @pytest.mark.asyncio
    async def test_logs_from_finished_process(self, tmp_path: Path) -> None:
        """跑完就立刻退出的进程：等收尸后读日志，stdout 和 stderr 都在。"""
        import asyncio

        env = _env(tmp_path)
        unit = await env.start("ws", "echo 第一行; echo 第二行 >&2", label="done", timeout_s=60)
        st = await env.status(unit)
        for _ in range(100):
            if not st["active"]:
                break
            await asyncio.sleep(0.05)
            st = await env.status(unit)
        assert st["active"] is False
        text = await env.logs("ws", unit)
        assert "第一行" in text and "第二行" in text

    @pytest.mark.asyncio
    async def test_stop_running_process_is_kill_not_wait(self, tmp_path: Path) -> None:
        """stop 是「立刻停下」（SIGKILL），不是「等它写完」：运行中的进程 stop 后
        日志可能还没来得及落盘，这是预期，不能为此把 stop 改成等待。"""
        env = _env(tmp_path)
        unit = await env.start("ws", "printf 数据; sleep 60", label="busy", timeout_s=120)
        assert (await env.status(unit))["active"] is True
        await env.stop(unit)
        assert (await env.status(unit))["active"] is False
        _ = await env.logs("ws", unit)  # 有/没有内容都行，但绝不能炸

    @pytest.mark.asyncio
    async def test_repeated_start_same_label_kills_old(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.start("ws", "sleep 60", label="p", timeout_s=60)
        unit2 = await env.start("ws", "sleep 60", label="p", timeout_s=60)
        assert (await env.status(unit2))["active"] is True
        await env.stop(unit2)

    @pytest.mark.asyncio
    async def test_exit_code_collected(self, tmp_path: Path) -> None:
        import asyncio

        env = _env(tmp_path)
        unit = await env.start("ws", "exit 7", label="quick", timeout_s=60)
        st: dict = {}
        for _ in range(100):
            st = await env.status(unit)
            if not st["active"]:
                break
            await asyncio.sleep(0.05)
        assert st["active"] is False
        assert st["exit_code"] == 7

    @pytest.mark.asyncio
    async def test_status_running_exit_code_is_none(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        unit = await env.start("ws", "sleep 60", label="p", timeout_s=60)
        assert (await env.status(unit))["exit_code"] is None
        await env.stop(unit)

    @pytest.mark.asyncio
    async def test_bad_label_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(ValueError):
            await env.start("ws", "true", label="坏 标签", timeout_s=10)
        with pytest.raises(ValueError):
            await env.start("ws", "true", label="", timeout_s=10)

    @pytest.mark.asyncio
    async def test_stop_unknown_unit_is_noop(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        await env.stop("maiwork-ws-never-started")  # 不炸

    @pytest.mark.asyncio
    async def test_logs_tail(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        unit = await env.start("ws", "seq 1 50", label="many", timeout_s=60)
        for _ in range(100):
            if not (await env.status(unit))["active"]:
                break
            import asyncio

            await asyncio.sleep(0.05)
        text = await env.logs("ws", unit, tail=5)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 5
        assert lines[-1].strip() == "50"


class TestSystemdProcessArgv:
    """systemd 模式的 start/status/stop 也只测 argv。"""

    @pytest.mark.asyncio
    async def test_start_unit_name_and_redirect(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        unit = await env.start("ws", "python bot.py", label="watcher", timeout_s=600)
        assert unit == "maiwork-ws-watcher"
        argv = calls[0]["argv"]
        assert argv[0] == "systemd-run"
        assert "--wait" not in argv and "--pipe" not in argv
        assert "--unit=maiwork-ws-watcher" in argv
        assert "RuntimeMaxSec=600" in argv
        assert "--uid=maiwork" in argv and "--gid=maiwork" in argv
        assert "MemoryMax=512M" in argv
        log_path = str((tmp_path / "ws" / "runtime" / "logs" / "watcher.log").resolve())
        exit_path = str((tmp_path / "ws" / "runtime" / "logs" / "watcher.exit").resolve())
        assert f">> {log_path} 2>&1; rc=$?; echo $rc > {exit_path}; exit $rc" in argv[-1]
        assert argv[-1].startswith("cd ")
        # 单元里给最小环境：HOME=工作区（线上实测不设时是只读的 /home/maiwork）
        ws_abs = str((tmp_path / "ws").resolve())
        assert f"--setenv=HOME={ws_abs}" in argv and "--setenv=LANG=C.UTF-8" in argv
        # start 也要先建 runtime/logs 目录（插件侧准备好，重定向才不会失败）
        assert (tmp_path / "ws" / "runtime" / "logs").is_dir()

    @pytest.mark.asyncio
    async def test_status_parses_systemctl_show(self, tmp_path: Path) -> None:
        runner, calls = runner_with(exit_code=0, out="ActiveState=active\nExecMainStatus=0\n")
        env = _env(tmp_path, mode="systemd", runner=runner)
        st = await env.status("maiwork-ws-x")
        assert st == {"active": True, "exit_code": None}  # 还在跑，没有退出码
        argv = calls[0]["argv"]
        assert argv[:3] == ["systemctl", "show", "-p"] and argv[-1] == "maiwork-ws-x"

    @pytest.mark.asyncio
    async def test_status_failed_unit(self, tmp_path: Path) -> None:
        """线上实测：--collect 单元回收后 ExecMainStatus 恒 0，真实退出码只能读进程自己写的 .exit。"""
        runner, _ = runner_with(exit_code=0, out="ActiveState=inactive\nExecMainStatus=0\n")
        env = _env(tmp_path, mode="systemd", runner=runner)
        env.workspace("ws")
        assert await env.status("maiwork-ws-x") == {"active": False, "exit_code": None}  # 没有 .exit 不猜
        logs = tmp_path / "ws" / "runtime" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "x.exit").write_text("3\n")
        assert await env.status("maiwork-ws-x") == {"active": False, "exit_code": 3}

    @pytest.mark.asyncio
    async def test_status_bad_output_falls_back_inactive(self, tmp_path: Path) -> None:
        runner, _ = runner_with(exit_code=0, out="看不懂的输出")
        env = _env(tmp_path, mode="systemd", runner=runner)
        assert await env.status("maiwork-ws-x") == {"active": False, "exit_code": None}

    @pytest.mark.asyncio
    async def test_stop_argv(self, tmp_path: Path) -> None:
        runner, calls = runner_with()
        env = _env(tmp_path, mode="systemd", runner=runner)
        await env.stop("maiwork-ws-x")
        assert calls[0]["argv"] == ["systemctl", "stop", "maiwork-ws-x"]

    @pytest.mark.asyncio
    async def test_logs_reads_workspace_file(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        log_dir = ws / "runtime" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "watcher.log").write_text("\n".join(f"行{i}" for i in range(1, 11)))
        env = _env(tmp_path, mode="systemd")
        text = await env.logs("ws", "maiwork-ws-watcher", tail=3)
        assert text.splitlines() == ["行8", "行9", "行10"]

    @pytest.mark.asyncio
    async def test_logs_missing_file_returns_empty(self, tmp_path: Path) -> None:
        env = _env(tmp_path, mode="systemd")
        env.workspace("ws")
        assert await env.logs("ws", "maiwork-ws-nothing") == ""
