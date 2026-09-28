"""skills_web.install_zip 单元测试（不走路由）：zip 包安全规则全 Mock。

- 成功：根目录 SKILL.md / 单顶层目录里 SKILL.md；名字取 front matter name /
  顶层目录名 / zip 文件名；解压到 <data_dir>/skills/<名>/，0700，kv 标 source=web；
- 拒绝：.. 穿越、绝对路径、zip 外部属性里的符号链接位、多份 SKILL.md、没 SKILL.md、
  超 200 个文件、解压总量 >20MB、单份 SKILL.md >40KB、zip 本身 >5MB；
- 重名 409 除非 ?replace=1（replace 时先落到临时目录再原子替换）。
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from CharTyr_MaiWork import skills_web
from CharTyr_MaiWork.store import Store


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _zip(entries: dict[str, str | bytes], *, symlinks: list[tuple[str, str]] | None = None) -> bytes:
    """entries: {路径: 文本内容}；symlinks: [(链接名, 目标)]（按 *nix 的 symlink 外部属性写）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            payload = data.encode("utf-8") if isinstance(data, str) else data
            zf.writestr(name, payload)
        for link_name, target in symlinks or []:
            info = zipfile.ZipInfo(link_name)
            info.external_attr = 0o120777 << 16
            info.create_system = 3  # Unix
            zf.writestr(info, target)
    return buf.getvalue()


SKILL_MD = "---\nname: demo\ndescription: 演示 skill\nroles: worker\n---\n\n正文照抄。\n"


class TestInstallZipSuccess:
    def test_root_single_skill_md(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": SKILL_MD, "notes.txt": "附属"})
        view = skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")
        assert view["name"] == "demo"
        assert view["description"] == "演示 skill"
        assert view["source"] == "web"
        assert (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
        assert (tmp_path / "skills" / "demo" / "notes.txt").is_file()
        # kv 标 source=web
        assert "demo" in skills_web._web_names(store)
        # 新装的 skill 立刻能被 skills_web.get_view 看到
        view2 = skills_web.get_view(tmp_path, store, "demo")
        assert view2 is not None and view2["name"] == "demo"

    def test_single_top_dir(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"aa-tool/SKILL.md": SKILL_MD, "aa-tool/readme.txt": "hi"})
        # 名字取 front matter 的 name（demo），不是顶层目录名
        view = skills_web.install_zip(store, tmp_path, blob, filename="aa.zip")
        assert view["name"] == "demo"
        # 解压到 <data_dir>/skills/<front matter name>/
        assert (tmp_path / "skills" / "demo" / "readme.txt").is_file()

    def test_name_from_top_dir(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"aa-tool/SKILL.md": "正文没 front matter\n"})
        view = skills_web.install_zip(store, tmp_path, blob, filename="aa.zip")
        assert view["name"] == "aa-tool"

    def test_name_from_filename(self, tmp_path):
        store = _store(tmp_path)
        # 没 front matter、没顶层目录 → zip 文件名当名字
        blob = _zip({"SKILL.md": "正文没 front matter\n"})
        view = skills_web.install_zip(store, tmp_path, blob, filename="my-skill.zip")
        assert view["name"] == "my-skill"

    def test_replace(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": SKILL_MD})
        skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")
        # 再传同名不同内容，不带 replace → 409
        with pytest.raises(FileExistsError):
            skills_web.install_zip(store, tmp_path, _zip({"SKILL.md": SKILL_MD}), filename="demo.zip")
        # 带 replace → 替换成功，内容换
        skills_web.install_zip(store, tmp_path, _zip({"SKILL.md": "---\nname: demo\ndescription: 换过\n---\n\n换过的正文。\n"}), filename="demo.zip", replace=True)
        text = (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
        assert "换过的正文" in text

    def test_zip_over_5mb_rejected(self, tmp_path):
        store = _store(tmp_path)
        # random 内容基本不可压，zip 体量 ≈ 原始体量 >5MB
        blob = _zip({"SKILL.md": SKILL_MD, "big.bin": os.urandom(5 * 1024 * 1024 + 100)})
        with pytest.raises(ValueError, match="最多 5MB"):
            skills_web.install_zip(store, tmp_path, blob, filename="big.zip")


class TestInstallZipRejections:
    def test_dotdot_traversal(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": SKILL_MD, "../evil.txt": "越界"})
        with pytest.raises(ValueError, match="越界|.."):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")
        assert not (tmp_path / "evil.txt").exists()

    def test_absolute_path(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": SKILL_MD, "/abs/evil.txt": "abs"})
        with pytest.raises(ValueError, match="绝对路径|越界"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_symlink_external_attr(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": SKILL_MD}, symlinks=[("link.sh", "/etc/passwd")])
        with pytest.raises(ValueError, match="符号链接"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_multiple_skill_md(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": SKILL_MD, "extra/SKILL.md": SKILL_MD})
        with pytest.raises(ValueError, match="有一份 SKILL.md"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_no_skill_md(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"readme.txt": "没有 skill"})
        with pytest.raises(ValueError, match="SKILL.md"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_too_many_files(self, tmp_path):
        store = _store(tmp_path)
        entries = {"SKILL.md": SKILL_MD}
        for i in range(201):
            entries[f"f{i}.txt"] = "x"
        blob = _zip(entries)
        with pytest.raises(ValueError, match="200"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_unpacked_too_big(self, tmp_path):
        store = _store(tmp_path)
        # 解压后总量 >20MB：每条 1MB 共 21 条（还低于 200 条限制）
        entries = {"SKILL.md": SKILL_MD}
        for i in range(21):
            entries[f"blob{i}.bin"] = b"y" * (1024 * 1024)
        blob = _zip(entries)
        with pytest.raises(ValueError, match="20MB"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_skill_md_too_big(self, tmp_path):
        store = _store(tmp_path)
        big = SKILL_MD + ("长" * 40000) + "\n"  # 远超 40KB
        blob = _zip({"SKILL.md": big})
        with pytest.raises(ValueError, match="40KB"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")

    def test_not_zip(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="zip"):
            skills_web.install_zip(store, tmp_path, b"not a zip at all", filename="demo.zip")

    def test_bad_skill_name_rejected(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"SKILL.md": "---\nname: 乱 name!!\n---\n\n正文\n"})
        with pytest.raises(ValueError, match="名字"):
            skills_web.install_zip(store, tmp_path, blob, filename="demo.zip")


# ----------------------------------------------------------------------
# 路由层（POST /api/extensions/skills/upload）：raw / multipart / 鉴权 / 同源 / X-Filename
# ----------------------------------------------------------------------

import aiohttp
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.app import MaiWorkApp


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": "qq:900000001", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:18653", "password": "skill-zip-密码-显眼-789", "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-skill-zip-显眼Dd4", "main": "m", "worker": "w"},
        "storage": {"data_dir": str(data_dir)},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class _Env:
    def __init__(self, app, client):
        self.app = app
        self.client = client


@pytest_asyncio.fixture
async def env(tmp_path):
    raw = _raw_config(tmp_path / "data")
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield _Env(app, client)
    await client.close()
    await app.stop()


async def _login(env) -> None:
    r = await env.client.post("/api/login", json={"password": "skill-zip-密码-显眼-789"})
    assert r.status == 200


class TestUploadRoute:
    @pytest.mark.asyncio
    async def test_upload_raw_zip(self, env):
        await _login(env)
        blob = _zip({"SKILL.md": SKILL_MD, "aa.txt": "附属"})
        r = await env.client.post(
            "/api/extensions/skills/upload",
            data=blob,
            headers={"Content-Type": "application/zip", "X-Filename": "demo.zip"},
        )
        assert r.status == 200, await r.text()
        view = await r.json()
        assert view["name"] == "demo"
        assert view["source"] == "web"
        assert view["files"] == ["aa.txt"]
        # 重名 → 409
        r2 = await env.client.post(
            "/api/extensions/skills/upload",
            data=blob,
            headers={"Content-Type": "application/zip", "X-Filename": "demo.zip"},
        )
        assert r2.status == 409
        # ?replace=1 → 200
        r3 = await env.client.post(
            "/api/extensions/skills/upload?replace=1",
            data=blob,
            headers={"Content-Type": "application/zip", "X-Filename": "demo.zip"},
        )
        assert r3.status == 200

    @pytest.mark.asyncio
    async def test_upload_multipart(self, env):
        await _login(env)
        blob = _zip({"SKILL.md": SKILL_MD})
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(blob), filename="multi.zip", content_type="application/zip")
        r = await env.client.post("/api/extensions/skills/upload", data=form)
        assert r.status == 200
        view = await r.json()
        assert view["name"] == "demo"  # front matter 优先

    @pytest.mark.asyncio
    async def test_x_filename_used_when_no_front_matter(self, env):
        await _login(env)
        # 没 front matter、根 SKILL.md（只有这个一个文件 → 顶目录是文件本身）
        blob = _zip({"SKILL.md": "没 front matter\n"})
        r = await env.client.post(
            "/api/extensions/skills/upload",
            data=blob,
            headers={"Content-Type": "application/zip", "X-Filename": "my-skill.zip"},
        )
        view = await r.json()
        assert view["name"] == "my-skill"

    @pytest.mark.asyncio
    async def test_upload_validation_400(self, env):
        await _login(env)
        r = await env.client.post(
            "/api/extensions/skills/upload", data=b"not zip",
            headers={"Content-Type": "application/zip", "X-Filename": "x.zip"},
        )
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_upload_requires_admin(self, env):
        blob = _zip({"SKILL.md": SKILL_MD})
        r = await env.client.post(
            "/api/extensions/skills/upload", data=blob,
            headers={"Content-Type": "application/zip", "X-Filename": "demo.zip"},
        )
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_upload_origin_guard(self, env):
        await _login(env)
        blob = _zip({"SKILL.md": SKILL_MD})
        r = await env.client.post(
            "/api/extensions/skills/upload", data=blob,
            headers={"Content-Type": "application/zip", "X-Filename": "demo.zip", "Origin": "http://evil.example.com:3"},
        )
        assert r.status == 403
