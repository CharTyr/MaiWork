"""原生 Windows（没有 POSIX 目录句柄）上的工作区安全读写（docs/07 §11.1）。

真机上 Windows 版 Python 没有 O_NOFOLLOW / O_DIRECTORY，`os.supports_dir_fd` 为空——
老实现 `LocalEnv._dir_flags` 当场抛「平台不支持安全的工作区目录打开方式」，所以 Windows 上
任何派活（连纯查资料写网页、借云端机器取回成品）都卡死。

这里用 conftest 的 `simulate_no_dir_fd` 在 Linux / macOS 上**如实模拟**那套 API：带 dir_fd
的调用一律 NotImplementedError。被测代码只要还偷偷走目录句柄那条路就会当场红；
GitHub 上的 windows-latest 再跑一遍真系统。

Linux / macOS 原来的目录句柄防线一字不动：最后一组测试证明真平台上根本不进 Windows 分支。
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import compaction
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments import pathsafe
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv, write_new_file_verified

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="真 Windows 上没有目录句柄，这条只在 Linux / macOS 上有意义")


def _symlink_or_skip(link: Path, target: Path, *, is_dir: bool = False) -> None:
    """Windows 上建符号链接要开发者模式 / 管理员；建不了就如实跳过（不假装测过）。"""
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:  # pragma: no cover - 只在没权限的 Windows 上走
        pytest.skip(f"这台机器建不了符号链接：{e}")


def _env(root: Path) -> LocalEnv:
    settings, _ = load_settings({"environments": {"local_mode": "direct", "workspace_root": str(root)}})
    return LocalEnv(lambda: settings)


@pytest.fixture
def no_dir_fd(simulate_no_dir_fd):
    return None


# ----------------------------------------------------------------------
# 1. 正常读写全链路
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestWorksWithoutDirFd:
    async def test_workspace_and_subdirs(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        assert ws == (tmp_path / "ws").resolve() / "g1"
        for sub in ("tasks", "artifacts", "tools", "runtime"):
            assert (ws / sub).is_dir()
        assert env.workspace("g1") == ws  # 幂等

    async def test_write_read_append_overwrite(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        await env.write_file("g1", "artifacts/T-1/report.md", "你好")
        assert await env.read_file("g1", "artifacts/T-1/report.md") == "你好"
        await env.write_file("g1", "artifacts/T-1/report.md", "世界", append=True)
        assert await env.read_file("g1", "artifacts/T-1/report.md") == "你好世界"
        await env.write_file("g1", "artifacts/T-1/report.md", "新")
        assert await env.read_file("g1", "artifacts/T-1/report.md") == "新"
        # 反斜杠写法也认（Windows 用户 / 模型都可能这么写）
        assert await env.read_file("g1", "artifacts\\T-1\\report.md") == "新"
        # 不留临时文件
        names = os.listdir(env.workspace("g1") / "artifacts" / "T-1")
        assert names == ["report.md"]

    async def test_overwrite_is_new_file_not_truncate(self, tmp_path, no_dir_fd):
        """原子替换：落位的是新文件，旧文件对象本身没被截断改写。"""
        env = _env(tmp_path / "ws")
        await env.write_file("g1", "a.txt", "OLD-CONTENT")
        target = env.workspace("g1") / "a.txt"
        before = os.lstat(target)
        if os.name == "nt":
            # 真 Windows：别人开着的文件不让替换（系统规矩），这里只核对落位的是新文件
            await env.write_file("g1", "a.txt", "NEW")
        else:
            fh = open(target, "rb")  # noqa: SIM115 — 拿着旧文件，看它的内容会不会被改
            try:
                await env.write_file("g1", "a.txt", "NEW")
                assert fh.read() == b"OLD-CONTENT"
            finally:
                fh.close()
        assert target.read_text(encoding="utf-8") == "NEW"
        assert os.lstat(target).st_ino != before.st_ino or os.name == "nt"

    async def test_limits_and_missing(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        env.workspace("g1")
        with pytest.raises(ValueError):
            await env.write_file("g1", "big.txt", "x" * (5 * 1024 * 1024 + 1))
        with pytest.raises(FileNotFoundError):
            await env.read_file("g1", "nope.txt")
        with pytest.raises(IsADirectoryError):
            await env.read_file("g1", "artifacts")

    async def test_read_file_page(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        body = "".join(f"第{i}行。" for i in range(3000))
        await env.write_file("g1", "notes/long.md", body)
        page = await env.read_file_page("g1", "notes/long.md", offset=100, limit=50)
        assert page.text == body[100:150]
        tail = await env.read_file_page("g1", "notes/long.md", offset=len(body) - 10, limit=50)
        assert tail.text == body[-10:] and tail.eof and tail.complete

    async def test_list_files(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        await env.write_file("g1", "artifacts/T-1/index.html", "<p>hi</p>")
        entries = await env.list_files("g1", "artifacts", depth=3)
        paths = {e["path"]: e for e in entries}
        assert paths["artifacts/T-1"]["is_dir"] is True
        assert paths["artifacts/T-1/index.html"]["size"] == len("<p>hi</p>")
        with pytest.raises(FileNotFoundError):
            await env.list_files("g1", "nope")

    async def test_resolve(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        assert env.resolve("g1", "") == ws
        assert env.resolve("g1", "artifacts/T-7/result.html") == ws / "artifacts" / "T-7" / "result.html"

    async def test_ephemeral(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        async with env.ephemeral("rr-news") as path:
            name = path.name
            await env.write_file(name, "notes.md", "临时")
            assert await env.read_file(name, "notes.md") == "临时"
        assert not path.exists()

    async def test_write_new_file_verified(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        assert write_new_file_verified(ws, "tool_spill/T-1", "spill-1.txt", "正文".encode()) is True
        assert (ws / "tool_spill" / "T-1" / "spill-1.txt").read_bytes() == "正文".encode()
        with pytest.raises(FileExistsError):
            write_new_file_verified(ws, "tool_spill/T-1", "spill-1.txt", b"again")

    async def test_secure_write_text(self, tmp_path, no_dir_fd):
        base = tmp_path.resolve() / "ws" / "g1" / "tool_spill" / "T-1"
        path = compaction.secure_write_text(base, "spill.txt", "完整正文")
        assert path is not None and path.parent == base
        assert path.read_text(encoding="utf-8") == "完整正文"
        assert path.name.startswith("spill-") and path.name.endswith(".txt")


# ----------------------------------------------------------------------
# 2. 防逃逸
# ----------------------------------------------------------------------


_BAD_RELS = (
    "../evil.txt",
    "a/../../evil.txt",
    "/etc/passwd",
    "\\evil.txt",
    "C:evil.txt",
    "C:\\Windows\\win.ini",
    "report.txt:hidden",       # NTFS 备用数据流
    "CON",
    "nul.txt",
    "artifacts/COM1",
    "lpt9.log",
    "trailing.",
    "trailing ",
    "a<b.txt",
    "q?.txt",
)


@pytest.mark.asyncio
class TestRejectsEscapes:
    @pytest.mark.parametrize("rel", _BAD_RELS)
    async def test_bad_paths_rejected(self, tmp_path, no_dir_fd, rel):
        env = _env(tmp_path / "ws")
        env.workspace("g1")
        with pytest.raises(PermissionError):
            await env.write_file("g1", rel, "nope")
        with pytest.raises(PermissionError):
            await env.read_file("g1", rel)
        with pytest.raises(PermissionError):
            env.resolve("g1", rel)

    @pytest.mark.parametrize("name", ["CON", "aux.md", "spill:ads", "..", "x/y"])
    async def test_new_file_bad_names_rejected(self, tmp_path, no_dir_fd, name):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        with pytest.raises(PermissionError):
            write_new_file_verified(ws, "tool_spill/T-1", name, b"x")

    async def test_final_symlink_rejected(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("TOP-SECRET", encoding="utf-8")
        _symlink_or_skip(ws / "evil.txt", secret)
        with pytest.raises(PermissionError):
            await env.read_file("g1", "evil.txt")
        with pytest.raises(PermissionError):
            await env.read_file_page("g1", "evil.txt")
        with pytest.raises(PermissionError):
            await env.write_file("g1", "evil.txt", "OVERWRITTEN")
        with pytest.raises(PermissionError):
            await env.write_file("g1", "evil.txt", "MORE", append=True)
        with pytest.raises(PermissionError):
            env.resolve("g1", "evil.txt")
        assert secret.read_text(encoding="utf-8") == "TOP-SECRET"
        assert [e["path"] for e in await env.list_files("g1", "")].count("evil.txt") == 0

    async def test_parent_symlink_rejected(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data.txt").write_text("x", encoding="utf-8")
        _symlink_or_skip(ws / "linkdir", outside, is_dir=True)
        with pytest.raises(PermissionError):
            await env.read_file("g1", "linkdir/data.txt")
        with pytest.raises(PermissionError):
            await env.write_file("g1", "linkdir/new.txt", "nope")
        with pytest.raises(PermissionError):
            await env.list_files("g1", "linkdir")
        with pytest.raises(PermissionError):
            env.resolve("g1", "linkdir/new.txt")
        with pytest.raises(PermissionError):
            write_new_file_verified(ws, "linkdir/sub", "x.txt", b"x")
        assert sorted(os.listdir(outside)) == ["data.txt"]

    async def test_workspace_symlink_rejected(self, tmp_path, no_dir_fd):
        root = tmp_path / "ws"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_or_skip(root / "g1", outside, is_dir=True)
        env = _env(root)
        with pytest.raises(PermissionError):
            env.workspace("g1")
        assert not (outside / "tasks").exists()

    async def test_reparse_point_rejected(self, tmp_path, no_dir_fd, monkeypatch):
        """NTFS 目录联接（junction）在 lstat 里不是 S_IFLNK，只有 st_reparse_tag 非零——也得拒。"""
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        junction = ws / "junction"
        junction.mkdir()
        (junction / "f.txt").write_text("x", encoding="utf-8")
        real_lstat = os.lstat

        def fake_lstat(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if Path(path) == junction:
                fields = {k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
                fields["st_reparse_tag"] = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT
                return types.SimpleNamespace(**fields)
            return st

        monkeypatch.setattr(os, "lstat", fake_lstat)
        with pytest.raises(PermissionError):
            await env.read_file("g1", "junction/f.txt")
        with pytest.raises(PermissionError):
            await env.write_file("g1", "junction/g.txt", "nope")
        with pytest.raises(PermissionError):
            env.resolve("g1", "junction/g.txt")
        listed = [e["path"] for e in await env.list_files("g1", "", depth=3)]
        assert "junction" not in listed and "junction/f.txt" not in listed
        base = ws / "junction" / "spill"
        assert compaction.secure_write_text(base, "spill.txt", "x") is None
        assert not (junction / "g.txt").exists() and not base.exists()

    async def test_hardlink_rejected(self, tmp_path, no_dir_fd):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        outside = tmp_path / "victim.txt"
        outside.write_text("KEEP", encoding="utf-8")
        try:
            os.link(outside, ws / "victim.txt")
        except OSError as e:  # pragma: no cover
            pytest.skip(f"这台机器建不了硬链接：{e}")
        with pytest.raises(PermissionError):
            await env.write_file("g1", "victim.txt", "REPLACED")
        with pytest.raises(PermissionError):
            await env.read_file("g1", "victim.txt")
        with pytest.raises(PermissionError):
            env.resolve("g1", "victim.txt")
        assert outside.read_text(encoding="utf-8") == "KEEP"

    async def test_target_swapped_during_write_fails_closed(self, tmp_path, no_dir_fd, monkeypatch):
        """写临时文件的空档里目标被换成指向外面的链接：替换前复核必须拒，外面的文件不动。"""
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        await env.write_file("g1", "a.txt", "inside")
        outside = tmp_path / "outside.txt"
        outside.write_text("KEEP", encoding="utf-8")
        real_write = pathsafe._write_all
        swapped = []

        def swap_then_write(fd, data, **kw):
            real_write(fd, data, **kw)
            if not swapped:
                (ws / "a.txt").unlink()
                _symlink_or_skip(ws / "a.txt", outside)
                swapped.append(True)

        monkeypatch.setattr(pathsafe, "_write_all", swap_then_write)
        with pytest.raises(PermissionError):
            await env.write_file("g1", "a.txt", "REPLACED")
        assert swapped
        assert outside.read_text(encoding="utf-8") == "KEEP"
        assert not [n for n in os.listdir(ws) if n.startswith(".maiwork-write-")]

    async def test_parent_swapped_during_write_fails_closed(self, tmp_path, no_dir_fd, monkeypatch):
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        await env.write_file("g1", "sub/a.txt", "inside")
        outside = tmp_path / "outside"
        outside.mkdir()
        real_write = pathsafe._write_all
        swapped = []

        def swap_parent(fd, data, **kw):
            real_write(fd, data, **kw)
            if not swapped:
                (ws / "sub").rename(ws / "old-sub")
                _symlink_or_skip(ws / "sub", outside, is_dir=True)
                swapped.append(True)

        monkeypatch.setattr(pathsafe, "_write_all", swap_parent)
        with pytest.raises(PermissionError):
            await env.write_file("g1", "sub/a.txt", "REPLACED")
        assert swapped and os.listdir(outside) == []


# ----------------------------------------------------------------------
# 3. 派活全链路（借云端临时机器取回成品）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestCoordinatorWithoutDirFd:
    @pytest.mark.parametrize("kind", ["railway", "freestyle", "ssh"])
    async def test_remote_fetch_roundtrip(self, tmp_path, no_dir_fd, kind):
        from CharTyr_MaiWork.maiwork import clock
        from CharTyr_MaiWork.maiwork.store import Store
        from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
        from CharTyr_MaiWork.maiwork.tools_railway import register_vm_tools
        from CharTyr_MaiWork.maiwork.tools_ssh import register_machine_tools

        payload = b"remote\r\n\x1a\x00\xff"
        local = _env(tmp_path / "ws")
        ws = local.workspace("g1")
        box = types.SimpleNamespace(kind=kind, remote_root="/app", expires_ts=clock.now() + 3600)

        async def get(_box, _remote, target):
            Path(target).write_bytes(payload)

        remote = types.SimpleNamespace(get=get)
        store = Store(tmp_path / "fetch.db")
        store.migrate()
        try:
            tools = Tools(store)
            if kind == "ssh":
                register_machine_tools(tools, get_box=lambda _tid: box, env=remote, local_env=local)
                tool_name = "machine_fetch_file"
            else:
                register_vm_tools(tools, get_box=lambda: box, env=remote, local_env=local)
                tool_name = "vm_fetch_file"
            ctx = ToolContext(group_id="g1", task_id="T-1", actor="worker", role="worker", workspace=ws)
            result = await tools.call(tool_name, {"remote_path": "/app/result.bin", "local_name": "result.bin"}, ctx)
            assert result.ok, result.error
            assert local.resolve("g1", "artifacts/T-1/result.bin").read_bytes() == payload
        finally:
            store.close()

    async def test_railway_task_completes_and_fetches(self, tmp_path, no_dir_fd):
        import test_coordinator_env as ce
        from CharTyr_MaiWork.maiwork.goals import Goals
        from CharTyr_MaiWork.maiwork.store import Store
        from CharTyr_MaiWork.maiwork.tasks import Tasks
        from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools

        store = Store(tmp_path / "maiwork.db")
        store.migrate()
        try:
            settings = ce._Settings(tmp_path / "workspaces")
            env = LocalEnv(lambda: settings)
            tools = Tools(store)
            tasks = Tasks(store, lambda: settings)
            goals = Goals(store, lambda: settings)
            tid = ce._create_task(tasks)
            models = ce.ModelsQueue(
                replies=[ce._plan(env="railway", env_reason="要装系统依赖"), ce._review(pass_=True)]
            )
            workers = ce.FakeWorkers()
            railway = ce.FakeRailwayEnv(give_box=True)

            async def _write():
                ws_name = tasks.get(tid)["workspace"]
                await env.write_file(ws_name, f"artifacts/{tid}/index.html", "<html>ok</html>")
                ctx = ToolContext(group_id=ce.GID, task_id=tid, actor="子 agent", role="worker",
                                  workspace=env.workspace(ws_name))
                r = await tools.call("vm_fetch_file", {"remote_path": "/app/result.html",
                                                       "local_name": "result.html"}, ctx)
                assert r.ok, r.error

            workers.before_return = _write
            coordinator = ce._build(
                mem_store=store, settings=settings, env=env, tools=tools, tasks=tasks,
                goals=goals, models=models, workers=workers, railway=railway,
            )
            await coordinator.run_task(tid)
            task = tasks.get(tid)
            assert task["status"] == "completed", task
            fetched = env.resolve(task["workspace"], f"artifacts/{tid}/result.html")
            assert fetched.read_text(encoding="utf-8") == "REMOTE-FETCHED"
        finally:
            store.close()


# ----------------------------------------------------------------------
# 4. Linux / macOS：原防线一字不动，根本不进 Windows 分支
# ----------------------------------------------------------------------


@POSIX_ONLY
@pytest.mark.asyncio
class TestPosixKeepsDirFdPath:
    async def test_real_platform_has_dir_fd(self):
        assert pathsafe.has_dir_fd() is True

    async def test_posix_never_uses_plain_branch(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("Linux / macOS 不许走 Windows 分支")

        for fn in ("ensure_dirs", "split_rel", "open_read_fd", "replace_atomic", "create_new_verified",
                   "create_exclusive", "lstat_regular"):
            monkeypatch.setattr(pathsafe, fn, boom)
        env = _env(tmp_path / "ws")
        ws = env.workspace("g1")
        await env.write_file("g1", "a/b.txt", "x")
        await env.write_file("g1", "a/b.txt", "y", append=True)
        assert await env.read_file("g1", "a/b.txt") == "xy"
        assert (await env.read_file_page("g1", "a/b.txt")).text == "xy"
        assert [e["path"] for e in await env.list_files("g1", "a")] == ["a/b.txt"]
        assert env.resolve("g1", "a/b.txt") == ws / "a" / "b.txt"
        assert write_new_file_verified(ws, "tool_spill/T-1", "s.txt", b"z") is True
        assert compaction.secure_write_text(ws / "spill", "s.txt", "z") is not None


# ----------------------------------------------------------------------
# 5. 段名规则本身
# ----------------------------------------------------------------------


class TestFileIdentity:
    @pytest.mark.parametrize("left,right", [(0, 0), (0, 7), (7, 0)])
    def test_missing_file_id_is_not_proof_of_identity(self, left, right):
        assert not pathsafe._same_file(
            types.SimpleNamespace(st_ino=left, st_dev=1),
            types.SimpleNamespace(st_ino=right, st_dev=1),
        )


class TestCheckPart:
    @pytest.mark.parametrize("part", ["report.md", "T-1", "index.html", "中文名.txt", "CONFIG", "nullable", "com10"])
    def test_ok(self, part):
        assert pathsafe.check_part(part) == part

    @pytest.mark.parametrize(
        "part", ["", ".", "..", "a/b", "a\\b", "c:", "x:y", "CON", "con.txt", "Nul", "PRN.md", "aux",
                 "COM1", "com9.log", "LPT1", "conin$", "end.", "end ", "a*b", "a\x01b"],
    )
    def test_bad(self, part):
        with pytest.raises(PermissionError):
            pathsafe.check_part(part)

    def test_split_rel(self):
        assert pathsafe.split_rel("a/./b\\c//d") == ["a", "b", "c", "d"]
        assert pathsafe.split_rel("") == []
        for bad in ("/a", "\\a", "C:a", "c:\\a", "a/../b"):
            with pytest.raises(PermissionError):
                pathsafe.split_rel(bad)


if sys.platform == "win32":  # pragma: no cover - 只在真 Windows（GitHub windows-latest）上跑

    @pytest.mark.asyncio
    class TestRealWindows:
        async def test_real_windows_has_no_dir_fd(self):
            assert pathsafe.has_dir_fd() is False

        async def test_roundtrip_on_real_windows(self, tmp_path):
            env = _env(tmp_path / "ws")
            await env.write_file("g1", "artifacts/T-1/a.md", "你好")
            assert await env.read_file("g1", "artifacts/T-1/a.md") == "你好"
            assert [e["path"] for e in await env.list_files("g1", "artifacts/T-1")] == ["artifacts/T-1/a.md"]

        async def test_real_junction_rejected(self, tmp_path):
            import _winapi

            env = _env(tmp_path / "ws")
            ws = env.workspace("g1")
            outside = tmp_path / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("KEEP", encoding="utf-8")
            junction = ws / "junction"
            _winapi.CreateJunction(str(outside), str(junction))
            try:
                assert os.lstat(junction).st_reparse_tag != 0
                with pytest.raises(PermissionError):
                    await env.read_file("g1", "junction/secret.txt")
                with pytest.raises(PermissionError):
                    await env.write_file("g1", "junction/new.txt", "nope")
                assert "junction" not in {e["path"] for e in await env.list_files("g1")}
                assert sorted(os.listdir(outside)) == ["secret.txt"]
            finally:
                os.rmdir(junction)
