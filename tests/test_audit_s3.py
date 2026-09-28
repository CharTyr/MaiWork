"""S3 回归测试：交付发布/打包不得跟随符号链接。

审计发现：herenow.py _iter_files 用 is_file()（跟随链接）、read_bytes()（跟随链接），
插件线上是 root——工作区里一个指向 /root/.typesafe_key 的符号链接会被发到公开
here.now / 群文件。修复约定：
- 遍历时任何符号链接（文件或目录）一律跳过并记日志；
- 每个文件 resolve 后必须 is_relative_to(目录.resolve())；
- 发布前单文件按 stat 大小先判断，超限不读（M3 顺带）；
- coordinator 交付前对 artifact（文件或目录）递归检查：含指向外面的符号链接 →
  视为验收不通过（review 写明）；
- outbox 执行 file 上传前检查 path 在 workspace_root 下且不是符号链接。
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.herenow import HereNow, HereNowError, _iter_files
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Delivery, Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

GID = "900000001"
SECRET = "super-secret-key-material"


def _outside(tmp_path: Path) -> Path:
    """工作区之外的「敏感文件」。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.key"
    secret.write_text(SECRET, encoding="utf-8")
    return secret


class TestIterFilesSkipsSymlinks:
    def test_symlink_file_skipped(self, tmp_path: Path) -> None:
        secret = _outside(tmp_path)
        pub = tmp_path / "pub"
        pub.mkdir()
        (pub / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        (pub / "leak.html").symlink_to(secret)
        found = [rel for rel, _p, _s in _iter_files(pub)]
        assert found == ["index.html"]

    def test_symlink_dir_skipped(self, tmp_path: Path) -> None:
        secret = _outside(tmp_path)
        pub = tmp_path / "pub"
        pub.mkdir()
        (pub / "ok.txt").write_text("ok", encoding="utf-8")
        (pub / "linked").symlink_to(secret.parent, target_is_directory=True)
        found = [rel for rel, _p, _s in _iter_files(pub)]
        assert found == ["ok.txt"]

    def test_symlink_to_inside_also_skipped(self, tmp_path: Path) -> None:
        """指向目录内部的链接同样跳过（规则简单一致：符号链接一律不打包）。"""
        pub = tmp_path / "pub"
        pub.mkdir()
        (pub / "real.txt").write_text("real", encoding="utf-8")
        (pub / "alias.txt").symlink_to(pub / "real.txt")
        found = sorted(rel for rel, _p, _s in _iter_files(pub))
        assert found == ["real.txt"]


class TestPublishNeverReadsSymlink:
    async def test_manifest_excludes_secret(self, tmp_path: Path) -> None:
        """发布目录里的符号链接不进 manifest、内容不被读。"""
        import httpx

        secret = _outside(tmp_path)
        pub = tmp_path / "pub"
        pub.mkdir()
        (pub / "index.html").write_text("<html>safe</html>", encoding="utf-8")
        (pub / "evil.html").symlink_to(secret)

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            if request.method == "POST" and request.url.path == "/api/v1/publish":
                captured.append(_json.loads(request.content))
                return httpx.Response(
                    200,
                    json={
                        "slug": "s",
                        "siteUrl": "https://s.here.now",
                        "upload": {"versionId": "v", "uploads": [], "finalizeUrl": "https://here.now/f"},
                        "expiresAt": "2026-10-16T12:00:00Z",
                    },
                )
            if request.method == "POST" and request.url.path == "/f":
                return httpx.Response(200, json={"siteUrl": "https://s.here.now"})
            return httpx.Response(200, json={})

        hn = HereNow(transport=httpx.MockTransport(handler))
        await hn.publish(pub)
        assert captured, "publish 应该调了 here.now"
        paths = [f["path"] for f in captured[0]["files"]]
        assert paths == ["index.html"]  # 符号链接不在 manifest 里


class TestZipFallbackSkipsSymlinks:
    async def test_zip_fallback_excludes_symlink(self, tmp_path: Path) -> None:
        """view 回落群文件打 zip：符号链接不进 zip。"""
        secret = _outside(tmp_path)
        store = Store(tmp_path / "t.db")
        store.migrate()
        ws_root = tmp_path / "ws"
        art = ws_root / "g1" / "artifacts" / "T-9"
        art.mkdir(parents=True)
        (art / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        (art / "evil.html").symlink_to(secret)
        settings, _ = load_settings(
            {
                "groups": {"serve": [{"group": f"qq:{GID}"}]},
                "environments": {"workspace_root": str(ws_root)},
            }
        )

        class _Host:
            def __init__(self) -> None:
                self.text_errors: list[Exception] = []
                self.uploads: list[dict] = []

            async def send_text(self, session_id, text, *, reply_to=""):
                raise self.text_errors.pop(0) if self.text_errors else None

            async def upload_group_file(self, group_id, path, name):
                self.uploads.append({"group_id": group_id, "path": path, "name": name})
                return "fid"

        host = _Host()
        ob = Outbox(store, host, Pushes(store, lambda: settings), Mentions(store, lambda: settings), lambda: settings)
        d = Delivery(store, ob)
        with store.tx() as conn:
            conn.execute("INSERT INTO groups (group_id, session_id) VALUES (?, ?)", (GID, "s1"))
            conn.execute(
                "INSERT INTO tasks (id, group_id, workspace, title, status, created, updated)"
                " VALUES ('T-9', ?, 'g1', 'T', 'completed', 0, 0)",
                (GID,),
            )
        # 没配 here.now → 首选渠道发布失败 → flush 时回落群文件 zip
        await d.deliver_task("T-9", kind="view", path=art, name="页面", note="看页面")
        await ob.flush(4_000_000_000.0)
        rows = store.read().execute("SELECT kind, payload FROM outbox ORDER BY id").fetchall()
        kinds = [r["kind"] for r in rows]
        assert kinds[0] == "herenow"  # 首选渠道（没配 here.now → failed）
        assert "file" in kinds  # 回落 zip
        fb_row = rows[kinds.index("file")]
        import json as _json

        fb = _json.loads(fb_row["payload"])
        zpath = Path(fb["path"])
        assert zpath.suffix == ".zip"
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            assert "index.html" in names
            assert "evil.html" not in names
            assert SECRET not in zf.read("index.html").decode("utf-8", errors="replace")


class TestFileUploadGuards:
    async def test_upload_symlink_rejected(self, tmp_path: Path) -> None:
        """outbox 执行 file 上传：路径是符号链接 → failed，不调宿主。"""
        secret = _outside(tmp_path)
        store = Store(tmp_path / "t.db")
        store.migrate()
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        link = ws_root / "evil.zip"
        link.symlink_to(secret)
        settings, _ = load_settings(
            {
                "groups": {"serve": [{"group": f"qq:{GID}"}]},
                "environments": {"workspace_root": str(ws_root)},
            }
        )

        class _Host:
            def __init__(self) -> None:
                self.uploads: list[dict] = []

            async def upload_group_file(self, group_id, path, name):
                self.uploads.append(path)
                return "fid"

            async def send_text(self, session_id, text, *, reply_to=""):
                return type("R", (), {"message_id": "m"})()

        host = _Host()
        ob = Outbox(store, host, Pushes(store, lambda: settings), Mentions(store, lambda: settings), lambda: settings)
        ob.enqueue("f-evil", GID, "file", {"path": str(link), "name": "evil.zip", "note": "", "push_kind": "delivery"})
        await ob.flush(1000000000.0)
        row = store.read().execute("SELECT status, error FROM outbox WHERE key='f-evil'").fetchone()
        assert row["status"] == "failed"
        assert host.uploads == []

    async def test_upload_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """outbox 执行 file 上传：resolve 后不在 workspace_root 下 → failed，不调宿主。"""
        secret = _outside(tmp_path)
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings, _ = load_settings(
            {
                "groups": {"serve": [{"group": f"qq:{GID}"}]},
                "environments": {"workspace_root": str(tmp_path / "ws")},
            }
        )

        class _Host:
            def __init__(self) -> None:
                self.uploads: list[dict] = []

            async def upload_group_file(self, group_id, path, name):
                self.uploads.append(path)
                return "fid"

            async def send_text(self, session_id, text, *, reply_to=""):
                return type("R", (), {"message_id": "m"})()

        host = _Host()
        ob = Outbox(store, host, Pushes(store, lambda: settings), Mentions(store, lambda: settings), lambda: settings)
        ob.enqueue(
            "f-out", GID, "file",
            {"path": str(secret), "name": "secret.key", "note": "", "push_kind": "delivery"},
        )
        await ob.flush(1000000000.0)
        row = store.read().execute("SELECT status, error FROM outbox WHERE key='f-out'").fetchone()
        assert row["status"] == "failed"
        assert host.uploads == []

    async def test_legit_workspace_file_uploads(self, tmp_path: Path) -> None:
        """工作区内的正常文件照常上传（守卫不误伤）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        ws_root = tmp_path / "ws"
        (ws_root / "g1").mkdir(parents=True)
        good = ws_root / "g1" / "报告.csv"
        good.write_text("a,b\n", encoding="utf-8")
        settings, _ = load_settings(
            {
                "groups": {"serve": [{"group": f"qq:{GID}"}]},
                "environments": {"workspace_root": str(ws_root)},
            }
        )

        class _Host:
            def __init__(self) -> None:
                self.uploads: list[dict] = []

            async def upload_group_file(self, group_id, path, name):
                self.uploads.append(path)
                return "fid"

            async def send_text(self, session_id, text, *, reply_to=""):
                return type("R", (), {"message_id": "m"})()

        host = _Host()
        ob = Outbox(store, host, Pushes(store, lambda: settings), Mentions(store, lambda: settings), lambda: settings)
        ob.enqueue("f-ok", GID, "file", {"path": str(good), "name": "报告.csv", "note": "n", "push_kind": "delivery"})
        await ob.flush(1000000000.0)
        row = store.read().execute("SELECT status FROM outbox WHERE key='f-ok'").fetchone()
        assert row["status"] == "sent"
        assert host.uploads == [str(good)]


class TestCoordinatorArtifactSymlinkCheck:
    def test_check_function_flags_outside_symlink(self, tmp_path: Path) -> None:
        """交付前递归检查：目录里含指向工作区外的符号链接 → 返回问题说明（验收按不通过）。"""
        from CharTyr_MaiWork.maiwork.coordinator import Coordinator

        secret = _outside(tmp_path)
        art = tmp_path / "art"
        art.mkdir()
        (art / "index.html").write_text("ok", encoding="utf-8")
        (art / "evil.html").symlink_to(secret)
        problem = Coordinator._find_artifact_symlink_escape(art)
        assert problem is not None
        assert "evil.html" in str(problem)

    def test_check_function_clean_dir_ok(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.coordinator import Coordinator

        art = tmp_path / "art"
        art.mkdir()
        (art / "a.txt").write_text("ok", encoding="utf-8")
        (art / "sub").mkdir()
        (art / "sub" / "b.txt").write_text("ok", encoding="utf-8")
        assert Coordinator._find_artifact_symlink_escape(art) is None
        # 单个常规文件也没问题
        assert Coordinator._find_artifact_symlink_escape(art / "a.txt") is None
