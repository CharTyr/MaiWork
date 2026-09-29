"""G2 回归测试：resolve 与读写之间的 TOCTOU。

LocalEnv.read_file / write_file 必须从工作区根目录 fd 逐段 dir_fd +
O_NOFOLLOW 打开；目录段或文件换成 symlink 必须 fail closed。
写入先创建全新 inode、校验目标，再通过同目录原子替换；绝不能先对旧文件
O_TRUNC，也不能覆写外部硬链接。chown 只操作打开的普通 inode，跳过链接。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv

pytestmark = pytest.mark.asyncio


def _env(root: Path) -> LocalEnv:
    settings, _ = load_settings(
        {"environments": {"local_mode": "direct", "workspace_root": str(root)}}
    )
    return LocalEnv(lambda: settings)


class TestFinalSymlinkRejected:
    async def test_read_via_final_symlink_rejected(self, tmp_path: Path) -> None:
        """最后一一段是符号链接（已存在的）→ 现在 resolve 阶段就拒；换成
        「resolve 之后出现链接」的场景也必须拒（O_NOFOLLOW 打开失败）。"""
        env = _env(tmp_path)
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("TOP-SECRET", encoding="utf-8")
        # resolve 通过不了的情况已有测试覆盖；这里直接调 read_file 走完整防线
        (tmp_path / "ws1" / "links").mkdir(exist_ok=True)
        link = tmp_path / "ws1" / "links" / "evil.txt"
        link.symlink_to(secret)
        with pytest.raises(PermissionError):
            await env.read_file("ws1", "links/evil.txt")

    async def test_write_via_final_symlink_rejected(self, tmp_path: Path) -> None:
        """write_file 覆盖写：目标是符号链接 → PermissionError（链接目标不被写）。"""
        env = _env(tmp_path)
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "victim.txt"
        target.write_text("original", encoding="utf-8")
        link = tmp_path / "ws1" / "evil.txt"
        link.symlink_to(target)
        with pytest.raises(PermissionError):
            await env.write_file("ws1", "evil.txt", "OVERWRITTEN")
        assert target.read_text(encoding="utf-8") == "original"

    async def test_append_via_final_symlink_rejected(self, tmp_path: Path) -> None:
        """append 同样拒绝符号链接目标。"""
        env = _env(tmp_path)
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "victim.txt"
        target.write_text("original\n", encoding="utf-8")
        link = tmp_path / "ws1" / "evil.txt"
        link.symlink_to(target)
        with pytest.raises(PermissionError):
            await env.write_file("ws1", "evil.txt", "MORE\n", append=True)
        assert target.read_text(encoding="utf-8") == "original\n"


class TestParentSymlinkRejected:
    async def test_read_parent_symlink_rejected(self, tmp_path: Path) -> None:
        """父目录链上有符号链接 → PermissionError（逐段 lstat）。"""
        env = _env(tmp_path)
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data.txt").write_text("x", encoding="utf-8")
        (tmp_path / "ws1" / "realdir").mkdir(exist_ok=True)
        linkdir = tmp_path / "ws1" / "linkdir"
        linkdir.symlink_to(outside, target_is_directory=True)
        with pytest.raises(PermissionError):
            await env.read_file("ws1", "linkdir/data.txt")

    async def test_write_parent_symlink_rejected(self, tmp_path: Path) -> None:
        env = _env(tmp_path)  # noqa: F841 — 先建工作区再塞链接父目录
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        linkdir = tmp_path / "ws1" / "linkdir"
        linkdir.symlink_to(outside, target_is_directory=True)
        with pytest.raises(PermissionError):
            await env.write_file("ws1", "linkdir/new.txt", "nope")
        assert not (outside / "new.txt").exists()


class TestToctouRaceSimulated:
    async def test_symlink_swapped_after_resolve_read(self, tmp_path: Path) -> None:
        """模拟 TOCTOU：resolve 返回真文件，open 前已被换成链接。

        做法：先写正常文件让 resolve 通过；然后把文件换成指向外面的链接，
        再调 read_file——绕 resolve 缓存没有，所以直接 monkeypatch env.resolve
        返回链接路径（等效于「检查后被换」）。O_NOFOLLOW 必须挡住。
        """
        env = _env(tmp_path)
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("TOP-SECRET", encoding="utf-8")
        real = tmp_path / "ws1" / "in.txt"
        real.write_text("ok", encoding="utf-8")
        link = tmp_path / "ws1" / "swapped.txt"
        link.symlink_to(secret)
        # monkeypatch resolve：跳过 resolve 的链接检查（模拟检查后路径被换）
        original_resolve = env.resolve
        env.resolve = lambda name, rel: link if "swapped" in rel else original_resolve(name, rel)  # type: ignore[method-assign]
        with pytest.raises(PermissionError):
            await env.read_file("ws1", "swapped.txt")

    async def test_symlink_swapped_after_resolve_write(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("ws1")
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "victim.txt"
        target.write_text("original", encoding="utf-8")
        link = tmp_path / "ws1" / "swapped.txt"
        link.symlink_to(target)
        original_resolve = env.resolve
        env.resolve = lambda name, rel: link if "swapped" in rel else original_resolve(name, rel)  # type: ignore[method-assign]
        with pytest.raises(PermissionError):
            await env.write_file("ws1", "swapped.txt", "OVERWRITTEN")
        assert target.read_text(encoding="utf-8") == "original"


class TestNormalOpsUnaffected:
    async def test_read_write_roundtrip_ok(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("ws1")
        await env.write_file("ws1", "a/b.txt", "你好")
        assert await env.read_file("ws1", "a/b.txt") == "你好"
        await env.write_file("ws1", "a/b.txt", "世界", append=True)
        assert await env.read_file("ws1", "a/b.txt") == "你好世界"
        # 覆盖写截断
        await env.write_file("ws1", "a/b.txt", "新")
        assert await env.read_file("ws1", "a/b.txt") == "新"

    async def test_size_limit_still_enforced(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("ws1")
        with pytest.raises(ValueError):
            await env.write_file("ws1", "big.txt", "x" * (5 * 1024 * 1024 + 1))

    async def test_read_missing_file_still_filenotfound(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        env.workspace("ws1")
        with pytest.raises(FileNotFoundError):
            await env.read_file("ws1", "nope.txt")


class TestAnchoredDirectorySafety:
    async def test_workspace_symlink_does_not_create_directories_outside(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "ws1").symlink_to(outside, target_is_directory=True)
        env = _env(tmp_path)
        with pytest.raises(PermissionError):
            env.workspace("ws1")
        assert not (outside / "tasks").exists()
        assert not (outside / "artifacts").exists()

    async def test_write_parent_swapped_at_open_cannot_truncate_outside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws1")
        parent = ws / "swap"
        parent.mkdir()
        (parent / "victim.txt").write_text("inside", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        original_open = os.open
        swaps = []

        def swap_before_open(path, flags, *args, **kwargs):
            # 旧实现：最后一段用绝对路径打开。新实现：父段在 workspace fd 下打开。
            if not swaps and (str(path) == "swap" or str(path).endswith("/swap/victim.txt")):
                parent.rename(ws / "old-parent")
                parent.symlink_to(outside, target_is_directory=True)
                swaps.append(True)
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap_before_open)
        with pytest.raises(PermissionError):
            await env.write_file("ws1", "swap/victim.txt", "REPLACED")
        assert swaps, "race 注入必须命中打开边界"
        assert victim.read_text(encoding="utf-8") == "KEEP"

    async def test_write_rejects_hardlinked_file(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        ws = env.workspace("ws1")
        outside = tmp_path / "victim.txt"
        outside.write_text("KEEP", encoding="utf-8")
        os.link(outside, ws / "victim.txt")
        with pytest.raises(PermissionError):
            await env.write_file("ws1", "victim.txt", "REPLACED")
        assert outside.read_text(encoding="utf-8") == "KEEP"

    async def test_chown_tree_skips_symlinks_and_hardlinks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "ws1"
        root.mkdir()
        (root / "ordinary.txt").write_text("normal", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("KEEP", encoding="utf-8")
        (root / "symlink.txt").symlink_to(outside)
        os.link(outside, root / "hardlink.txt")
        touched: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "chown", lambda path, uid, gid: touched.append(
            (os.stat(path).st_dev, os.stat(path).st_ino)))
        monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: touched.append(
            (os.fstat(fd).st_dev, os.fstat(fd).st_ino)))
        LocalEnv._chown_tree(root, 65534, 65534)
        assert (outside.stat().st_dev, outside.stat().st_ino) not in touched, (
            "root 不得经由 symlink/hardlink 修改外部 inode"
        )
        ordinary = (root / "ordinary.txt").stat()
        assert (ordinary.st_dev, ordinary.st_ino) in touched
