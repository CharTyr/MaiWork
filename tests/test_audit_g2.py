"""G2 回归测试：resolve 与读写之间的 TOCTOU。

LocalEnv.read_file / write_file 在 resolve 成功之后才 open()，中间文件可能
被换成符号链接。修复约定：
- 用 os.open + O_NOFOLLOW 打开最后一段（写：O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW，
  追加用 O_APPEND）；最后一段是链接 → 打开失败，
  抛 PermissionError（和 resolve 的越界同类）；
- 打开后用 os.fstat + /proc/self/fd/<fd>（Linux）或回落「重 realpath 父目录 +
  st_dev/st_ino 对比」校验仍在工作区内；
- 父目录各段逐段 lstat 检查，父目录链上任何一段是符号链接 → PermissionError。
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
