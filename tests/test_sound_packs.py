"""sound_packs 单元测试（不走路由）：音效包 zip 校验、安装、删除、主题绑定、GitHub 下载。

测试里的音频都是几字节的假文件头（OggS / RIFF..WAVE / ID3 / 帧同步），仓库不放真实音频。
GitHub 下载走 httpx.MockTransport，不碰真实网络。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from CharTyr_MaiWork.maiwork import sound_packs as sp
from CharTyr_MaiWork.maiwork.store import Store

OGG = b"OggS" + b"\x00" * 60
WAV = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"\x00" * 40
MP3_ID3 = b"ID3" + b"\x03\x00" + b"\x00" * 40
MP3_SYNC = b"\xff\xfb" + b"\x90\x00" + b"\x00" * 40
NOT_AUDIO = b"this is not audio at all"


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _zip(entries: dict[str, bytes | str], *, symlinks: list[tuple[str, str]] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
        for link_name, target in symlinks or []:
            info = zipfile.ZipInfo(link_name)
            info.external_attr = 0o120777 << 16
            info.create_system = 3
            zf.writestr(info, target)
    return buf.getvalue()


def _packs_root(data_dir: Path) -> Path:
    return data_dir / "console" / "sound_packs"


def _dirs(data_dir: Path) -> list[str]:
    root = _packs_root(data_dir)
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# ---------------------------------------------------------------------------
# zip 安装：成功
# ---------------------------------------------------------------------------


class TestInstallZipOk:
    def test_with_pack_json(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({
            "pack.json": json.dumps({"name": "Retro Clicks", "sounds": {"click": "audio/c.ogg", "error": "audio/e.wav"}}),
            "audio/c.ogg": OGG,
            "audio/e.wav": WAV,
            "audio/unused.ogg": OGG,
            "readme.txt": "说明",
        })
        view = sp.install_zip(store, tmp_path, blob, filename="x.zip")
        assert view["name"] == "Retro Clicks"
        assert view["id"] == "retro-clicks"
        assert view["source"] == "upload"
        assert sorted(view["sounds"]) == ["click", "error"]
        d = _packs_root(tmp_path) / "retro-clicks"
        meta = json.loads((d / "pack.json").read_text(encoding="utf-8"))
        assert meta["name"] == "Retro Clicks"
        assert meta["source"] == "upload"
        assert set(meta["sounds"]) == {"click", "error"}
        assert meta["size"] == len(OGG) + len(WAV)
        assert meta["installed_at"]
        # 只解出被引用的音频；其余（readme、未引用的音频）不落盘
        files = sorted(p.name for p in d.iterdir())
        assert files == sorted(["pack.json", meta["sounds"]["click"], meta["sounds"]["error"]])
        assert (d / meta["sounds"]["click"]).read_bytes() == OGG
        # 视图不含文件系统路径
        assert str(tmp_path) not in json.dumps(view, ensure_ascii=False)

    def test_without_pack_json_root(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"hover.ogg": OGG, "click.mp3": MP3_ID3, "open.wav": WAV, "close.mp3": MP3_SYNC, "notes.md": "x"})
        view = sp.install_zip(store, tmp_path, blob, filename="My Pack.zip")
        assert sorted(view["sounds"]) == ["click", "close", "hover", "open"]
        assert view["name"] == "My Pack"
        assert view["id"] == "my-pack"

    def test_single_top_dir(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"tarkov-ui/click.ogg": OGG, "tarkov-ui/success.wav": WAV})
        view = sp.install_zip(store, tmp_path, blob)
        assert sorted(view["sounds"]) == ["click", "success"]
        assert view["name"] == "tarkov-ui"
        assert view["id"] == "tarkov-ui"

    def test_chinese_name_gets_pack_prefix(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"pack.json": json.dumps({"name": "塔科夫界面"}), "click.ogg": OGG})
        view = sp.install_zip(store, tmp_path, blob)
        assert view["name"] == "塔科夫界面"
        assert view["id"].startswith("pack-")
        assert sp.PACK_ID_RE.fullmatch(view["id"])

    def test_duplicate_name_gets_suffix(self, tmp_path):
        store = _store(tmp_path)
        blob = _zip({"pack.json": json.dumps({"name": "demo"}), "click.ogg": OGG})
        a = sp.install_zip(store, tmp_path, blob)
        b = sp.install_zip(store, tmp_path, blob)
        assert a["id"] == "demo"
        assert b["id"] != "demo" and b["id"].startswith("demo-")
        assert sorted(_dirs(tmp_path)) == sorted([a["id"], b["id"]])

    def test_list_packs(self, tmp_path):
        store = _store(tmp_path)
        sp.install_zip(store, tmp_path, _zip({"pack.json": json.dumps({"name": "a"}), "click.ogg": OGG}))
        sp.install_zip(store, tmp_path, _zip({"pack.json": json.dumps({"name": "b"}), "error.wav": WAV}))
        packs = sp.list_packs(tmp_path)
        assert sorted(p["id"] for p in packs) == ["a", "b"]
        for p in packs:
            assert set(p) >= {"id", "name", "source", "sounds", "size", "installed_at"}

    def test_pack_file(self, tmp_path):
        store = _store(tmp_path)
        sp.install_zip(store, tmp_path, _zip({"pack.json": json.dumps({"name": "a"}), "click.mp3": MP3_ID3, "open.wav": WAV}))
        path, ctype = sp.pack_file(tmp_path, "a", "click")
        assert ctype == "audio/mpeg"
        assert path.read_bytes() == MP3_ID3
        assert sp.pack_file(tmp_path, "a", "open")[1] == "audio/wav"
        assert sp.pack_file(tmp_path, "a", "hover") is None  # 包里没有这一类
        assert sp.pack_file(tmp_path, "a", "boom") is None  # 不认识的种类
        assert sp.pack_file(tmp_path, "../a", "click") is None  # 非法 id
        assert sp.pack_file(tmp_path, "nope", "click") is None


# ---------------------------------------------------------------------------
# zip 安装：拒绝
# ---------------------------------------------------------------------------


class TestInstallZipReject:
    def test_not_audio(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="click.mp3 不是有效的音频文件"):
            sp.install_zip(store, tmp_path, _zip({"click.mp3": NOT_AUDIO, "hover.ogg": OGG}))
        assert _dirs(tmp_path) == []

    def test_not_zip(self, tmp_path):
        with pytest.raises(ValueError, match="zip"):
            sp.install_zip(_store(tmp_path), tmp_path, b"not a zip")

    def test_zip_too_big(self, tmp_path):
        with pytest.raises(ValueError, match="压缩包超过 10MB"):
            sp.install_zip(_store(tmp_path), tmp_path, b"\x00" * (10 * 1024 * 1024 + 1))

    def test_too_many_members(self, tmp_path):
        entries = {f"f{i}.txt": "x" for i in range(201)}
        entries["click.ogg"] = OGG
        with pytest.raises(ValueError, match="200"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip(entries))

    def test_single_audio_too_big(self, tmp_path):
        big = b"OggS" + b"\x00" * (1024 * 1024)
        with pytest.raises(ValueError, match="1MB"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip({"click.ogg": big}))

    def test_six_near_limit_files_ok(self, tmp_path):
        # 6 类各近 1MB ≈ 6MB，没超合计 8MB，应当通过
        chunk = b"OggS" + b"\x00" * (1024 * 1024 - 100)
        sp.install_zip(_store(tmp_path), tmp_path, _zip({f"{k}.ogg": chunk for k in sp.KINDS}))
        assert sp.UNPACK_MAX == 8 * 1024 * 1024

    def test_total_too_big(self, tmp_path, monkeypatch):
        # 6 类 × 单个 1MB 到不了 8MB，所以把合计上限调小来验证这条检查本身

        monkeypatch.setattr(sp, "UNPACK_MAX", 100)
        with pytest.raises(ValueError, match="合计"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip({"click.ogg": OGG, "hover.ogg": OGG}))
        assert _dirs(tmp_path) == []

    def test_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="越界"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip({"../click.ogg": OGG}))

    def test_absolute_path(self, tmp_path):
        with pytest.raises(ValueError, match="绝对路径"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip({"/etc/click.ogg": OGG}))

    def test_pack_json_points_outside(self, tmp_path):
        blob = _zip({"pack.json": json.dumps({"sounds": {"click": "../x.ogg"}}), "click.ogg": OGG})
        with pytest.raises(ValueError):
            sp.install_zip(_store(tmp_path), tmp_path, blob)

    def test_symlink(self, tmp_path):
        blob = _zip({"hover.ogg": OGG}, symlinks=[("click.ogg", "/etc/passwd")])
        with pytest.raises(ValueError, match="符号链接"):
            sp.install_zip(_store(tmp_path), tmp_path, blob)

    def test_empty_pack(self, tmp_path):
        with pytest.raises(ValueError, match="没有可识别的声音"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip({"readme.txt": "x", "beep.ogg": OGG}))
        assert _dirs(tmp_path) == []

    def test_bad_pack_json(self, tmp_path):
        with pytest.raises(ValueError, match="pack.json"):
            sp.install_zip(_store(tmp_path), tmp_path, _zip({"pack.json": "{not json", "click.ogg": OGG}))

    def test_pack_json_missing_file(self, tmp_path):
        blob = _zip({"pack.json": json.dumps({"sounds": {"click": "nope.ogg"}}), "hover.ogg": OGG})
        with pytest.raises(ValueError, match="nope.ogg"):
            sp.install_zip(_store(tmp_path), tmp_path, blob)

    def test_failure_leaves_no_temp_dir(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            sp.install_zip(store, tmp_path, _zip({"click.ogg": OGG, "error.wav": NOT_AUDIO}))
        assert _dirs(tmp_path) == []


# ---------------------------------------------------------------------------
# 删除 / 主题
# ---------------------------------------------------------------------------


class TestThemeAndDelete:
    def test_default_theme_view(self, tmp_path):
        store = _store(tmp_path)
        t = sp.get_theme(store, tmp_path)
        assert t["default"] == "default"
        assert t["themes"] == [{"id": "default", "name": "默认"}, {"id": "retro", "name": "复古终端"}]
        assert t["sounds"] == {}

    def test_bind_and_view(self, tmp_path):
        store = _store(tmp_path)
        v = sp.install_zip(store, tmp_path, _zip({"pack.json": json.dumps({"name": "a"}), "click.ogg": OGG, "error.wav": WAV}))
        sp.set_theme(store, tmp_path, {"default": "retro", "sounds": {"retro": v["id"]}})
        t = sp.get_theme(store, tmp_path)
        assert t["default"] == "retro"
        assert t["sounds"]["retro"]["pack"] == "a"
        assert t["sounds"]["retro"]["name"] == "a"
        assert t["sounds"]["retro"]["files"] == {"click": "/api/sound-packs/a/click", "error": "/api/sound-packs/a/error"}
        assert str(tmp_path) not in json.dumps(t, ensure_ascii=False)
        # 解绑
        sp.set_theme(store, tmp_path, {"sounds": {"retro": None}})
        assert sp.get_theme(store, tmp_path)["sounds"] == {}
        assert sp.get_theme(store, tmp_path)["default"] == "retro"

    def test_default_theme_cannot_bind(self, tmp_path):
        store = _store(tmp_path)
        v = sp.install_zip(store, tmp_path, _zip({"pack.json": json.dumps({"name": "a"}), "click.ogg": OGG}))
        with pytest.raises(ValueError, match="默认主题"):
            sp.set_theme(store, tmp_path, {"sounds": {"default": v["id"]}})

    def test_unknown_theme_and_pack(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="主题"):
            sp.set_theme(store, tmp_path, {"default": "neon"})
        with pytest.raises(ValueError, match="主题"):
            sp.set_theme(store, tmp_path, {"sounds": {"neon": None}})
        with pytest.raises(ValueError, match="音效包"):
            sp.set_theme(store, tmp_path, {"sounds": {"retro": "missing"}})

    def test_delete_clears_binding(self, tmp_path):
        store = _store(tmp_path)
        v = sp.install_zip(store, tmp_path, _zip({"pack.json": json.dumps({"name": "a"}), "click.ogg": OGG}))
        sp.set_theme(store, tmp_path, {"sounds": {"retro": v["id"]}})
        sp.delete_pack(store, tmp_path, v["id"])
        assert _dirs(tmp_path) == []
        assert store.kv_get("ui.theme.sounds", {}) == {}
        assert sp.get_theme(store, tmp_path)["sounds"] == {}

    def test_delete_missing_or_bad_id(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            sp.delete_pack(store, tmp_path, "../etc")
        with pytest.raises(KeyError):
            sp.delete_pack(store, tmp_path, "nope")

    def test_binding_to_vanished_pack_is_unbound(self, tmp_path):
        store = _store(tmp_path)
        with store.tx() as conn:
            store.kv_set(conn, "ui.theme.sounds", {"retro": "gone"})
        assert sp.get_theme(store, tmp_path)["sounds"] == {}


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class TestGithubUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/foo/bar", ("foo", "bar", None, "")),
            ("https://github.com/foo/bar.git", ("foo", "bar", None, "")),
            ("https://github.com/foo/bar/", ("foo", "bar", None, "")),
            ("https://github.com/Fo-o_1/b.a-r/tree/dev", ("Fo-o_1", "b.a-r", "dev", "")),
            ("https://github.com/foo/bar/tree/main/packs/tarkov", ("foo", "bar", "main", "packs/tarkov")),
        ],
    )
    def test_parse_ok(self, url, expected):
        assert sp.parse_github_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "http://github.com/foo/bar",
            "https://gitlab.com/foo/bar",
            "https://github.com.evil.com/foo/bar",
            "https://evil.com/github.com/foo/bar",
            "https://github.com/foo",
            "https://github.com/fo o/bar",
            "https://github.com/foo/b$r",
            "https://github.com/foo/bar/blob/main/x",
            "https://github.com/foo/bar/tree/main/../x",
            "https://user@github.com/foo/bar",
            "https://github.com:8443/foo/bar",
            "",
        ],
    )
    def test_parse_reject(self, url):
        with pytest.raises(ValueError):
            sp.parse_github_url(url)


def _repo_zip(top: str, entries: dict[str, bytes | str]) -> bytes:
    return _zip({f"{top}/{k}": v for k, v in entries.items()})


class TestGithubInstall:
    @pytest.mark.asyncio
    async def test_default_branch_and_install(self, tmp_path):
        seen: list[str] = []
        blob = _repo_zip("bar-main", {"click.ogg": OGG, "README.md": "x"})

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            if req.url.host == "api.github.com":
                return httpx.Response(200, json={"default_branch": "main"})
            if req.url.host == "codeload.github.com":
                return httpx.Response(200, content=blob)
            return httpx.Response(404)

        store = _store(tmp_path)
        view = await sp.install_github(store, tmp_path, "https://github.com/foo/bar", transport=httpx.MockTransport(handler))
        assert seen == [
            "https://api.github.com/repos/foo/bar",
            "https://codeload.github.com/foo/bar/zip/refs/heads/main",
        ]
        assert view["source"] == "https://github.com/foo/bar"
        assert view["sounds"] == ["click"]
        assert view["name"] == "bar"

    @pytest.mark.asyncio
    async def test_branch_and_subdir(self, tmp_path):
        seen: list[str] = []
        blob = _repo_zip("bar-dev", {
            "click.ogg": OGG,  # 根目录的不算
            "packs/tarkov/pack.json": json.dumps({"name": "塔科夫"}),
            "packs/tarkov/error.wav": WAV,
            "packs/tarkov/hover.mp3": MP3_SYNC,
        })

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, content=blob)

        store = _store(tmp_path)
        view = await sp.install_github(
            store, tmp_path, "https://github.com/foo/bar/tree/dev/packs/tarkov", transport=httpx.MockTransport(handler)
        )
        assert seen == ["https://codeload.github.com/foo/bar/zip/refs/heads/dev"]
        assert sorted(view["sounds"]) == ["error", "hover"]
        assert view["name"] == "塔科夫"
        assert view["source"] == "https://github.com/foo/bar/tree/dev/packs/tarkov"

    @pytest.mark.asyncio
    async def test_subdir_missing(self, tmp_path):
        blob = _repo_zip("bar-dev", {"click.ogg": OGG})
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=blob))
        with pytest.raises(ValueError, match="子目录"):
            await sp.install_github(_store(tmp_path), tmp_path, "https://github.com/foo/bar/tree/dev/nope", transport=transport)

    @pytest.mark.asyncio
    async def test_download_too_big_aborts(self, tmp_path):
        served = {"bytes": 0}

        class Big(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(40):
                    served["bytes"] += 512 * 1024
                    yield b"\x00" * (512 * 1024)

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=Big())

        with pytest.raises(ValueError, match="10MB"):
            await sp.install_github(
                _store(tmp_path), tmp_path, "https://github.com/foo/bar/tree/main", transport=httpx.MockTransport(handler)
            )
        assert served["bytes"] < 12 * 1024 * 1024  # 超过后立刻停读
        assert _dirs(tmp_path) == []

    @pytest.mark.asyncio
    async def test_repo_not_found(self, tmp_path):
        transport = httpx.MockTransport(lambda req: httpx.Response(404))
        with pytest.raises(ValueError, match="仓库"):
            await sp.install_github(_store(tmp_path), tmp_path, "https://github.com/foo/bar", transport=transport)

    @pytest.mark.asyncio
    async def test_branch_download_404(self, tmp_path):
        transport = httpx.MockTransport(lambda req: httpx.Response(404))
        with pytest.raises(ValueError, match="下载失败"):
            await sp.install_github(_store(tmp_path), tmp_path, "https://github.com/foo/bar/tree/x", transport=transport)

    @pytest.mark.asyncio
    async def test_redirect_to_other_host_refused(self, tmp_path):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "codeload.github.com":
                return httpx.Response(302, headers={"Location": "https://evil.example.com/x.zip"})
            return httpx.Response(200, content=_repo_zip("t", {"click.ogg": OGG}))

        with pytest.raises(ValueError):
            await sp.install_github(
                _store(tmp_path), tmp_path, "https://github.com/foo/bar/tree/main", transport=httpx.MockTransport(handler)
            )

    @pytest.mark.asyncio
    async def test_bad_url_no_network(self, tmp_path):
        called = []
        transport = httpx.MockTransport(lambda req: called.append(1) or httpx.Response(200))
        with pytest.raises(ValueError):
            await sp.install_github(_store(tmp_path), tmp_path, "https://evil.com/foo/bar", transport=transport)
        assert called == []
