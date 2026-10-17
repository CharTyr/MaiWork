"""网页音效包与主题设置（任务 10-16-web-theme-sound，design.md §2~§4）。

- 音效包装在 <data_dir>/console/sound_packs/<pack_id>/：规范化后的 pack.json + 音频文件；
  pack_id 由服务端生成（[a-z0-9-]{1,40}），用户输入不直接成为路径；重名加后缀，不覆盖。
- 来源两种：总管理员上传 zip；填 GitHub 公开仓库地址下载（codeload zip）。两者走同一套校验：
  zip ≤10MB、成员 ≤200、单个音频 ≤1MB、音频合计 ≤8MB；拒绝绝对路径、..、符号链接；
  按文件头校验 ogg / mp3 / wav；只解出被引用的音频；一个有效声音都没有就拒绝。
  先写临时目录，校验通过后 rename 到位，失败清理。
- 主题设置存 kv：ui.theme.default（缺省 default）、ui.theme.sounds（{主题: pack_id}）；
  默认主题不可绑定音效。get_theme 是 GET /api/theme 的返回体，不含任何文件系统路径。
错误一律抛 ValueError（中文具体原因）；删除不存在的包抛 KeyError。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("maiwork.sound_packs")

KINDS = ("hover", "click", "open", "close", "success", "error")
THEMES = ("default", "retro")
THEME_NAMES = {"default": "默认", "retro": "复古终端"}

ZIP_MAX = 10 * 1024 * 1024
MEMBERS_MAX = 200
AUDIO_MAX = 1024 * 1024
UNPACK_MAX = 8 * 1024 * 1024
PACK_JSON_MAX = 64 * 1024
NAME_MAX = 60
HTTP_TIMEOUT_S = 30

PACK_ID_RE = re.compile(r"[a-z0-9-]{1,40}")
_GH_PART_RE = re.compile(r"[A-Za-z0-9_.-]+")
_EXTS = ("ogg", "mp3", "wav")
CONTENT_TYPES = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}
_GH_HOSTS = ("github.com", "api.github.com", "codeload.github.com")

KV_DEFAULT = "ui.theme.default"
KV_SOUNDS = "ui.theme.sounds"

_install_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def _root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "console" / "sound_packs"


def _valid_id(pack_id: Any) -> bool:
    return isinstance(pack_id, str) and PACK_ID_RE.fullmatch(pack_id) is not None


def _pack_dir(data_dir: Path | str, pack_id: str) -> Path | None:
    if not _valid_id(pack_id):
        return None
    d = _root(data_dir) / pack_id
    if d.is_symlink() or not d.is_dir():
        return None
    return d


# ---------------------------------------------------------------------------
# 校验小件
# ---------------------------------------------------------------------------


def _is_symlink_info(info: zipfile.ZipInfo) -> bool:
    """zip 外部属性里的 symlink 位（*nix：高 16 位是 st_mode）。"""
    return ((int(info.external_attr) >> 16) & 0o170000) == 0o120000


def _safe_member(name: str) -> str:
    """zip 成员名 → POSIX 相对路径；绝对路径 / .. / 空段抛 ValueError。"""
    n = str(name or "").replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        raise ValueError(f"压缩包里有绝对路径 {name}，已拒绝")
    stripped = n[:-1] if n.endswith("/") else n
    parts = stripped.split("/")
    if not stripped or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"压缩包里有越界路径 {name}，已拒绝")
    return n


def _sniff(data: bytes) -> str | None:
    """按文件头认格式：ogg / wav / mp3；都不像返回 None。"""
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    if not s:
        s = "pack-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return s


def _clean_name(name: Any) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    return s[:NAME_MAX]


# ---------------------------------------------------------------------------
# 安装核心
# ---------------------------------------------------------------------------


def _install_from_zip(
    data_dir: Path | str,
    zf: zipfile.ZipFile,
    *,
    base: str,
    name_fallback: str,
    source: str,
) -> dict[str, Any]:
    """从已打开的 zip 里 base 目录（"" = 根）装一个包。"""
    prefix = f"{base}/" if base else ""
    members: dict[str, zipfile.ZipInfo] = {}
    count = 0
    for info in zf.infolist():
        n = _safe_member(info.filename)
        if prefix and not n.startswith(prefix):
            continue
        count += 1
        if count > MEMBERS_MAX:
            raise ValueError(f"压缩包里的文件超过 {MEMBERS_MAX} 个")
        if _is_symlink_info(info):
            raise ValueError(f"压缩包里有符号链接 {n[len(prefix):]}，已拒绝")
        if info.is_dir():
            continue
        members[n[len(prefix):]] = info
    if not members:
        raise ValueError("压缩包里没有文件")

    # pack.json（可选）
    name = ""
    wanted: dict[str, str] = {}
    meta_info = members.get("pack.json")
    if meta_info is not None:
        if meta_info.file_size > PACK_JSON_MAX:
            raise ValueError("pack.json 过大")
        try:
            meta = json.loads(zf.read(meta_info).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("pack.json 不是有效的 JSON") from None
        if not isinstance(meta, dict):
            raise ValueError("pack.json 格式不正确，应为对象")
        name = _clean_name(meta.get("name")) if isinstance(meta.get("name"), str) else ""
        sounds = meta.get("sounds")
        if sounds is not None and not isinstance(sounds, dict):
            raise ValueError("pack.json 中的 sounds 应为对象")
        for kind, rel in (sounds or {}).items():
            if kind not in KINDS:
                continue
            if not isinstance(rel, str) or not rel.strip():
                raise ValueError(f"pack.json 中 {kind} 的文件路径不正确")
            rel_n = rel.strip().replace("\\", "/")
            try:
                rel_n = _safe_member(rel_n)
            except ValueError:
                raise ValueError(f"pack.json 中 {kind} 的路径 {rel} 越界，已拒绝") from None
            if rel_n not in members:
                raise ValueError(f"pack.json 指向的 {rel} 在压缩包里不存在")
            wanted[kind] = rel_n
    if not wanted:
        lower = {k.lower(): k for k in members if "/" not in k}
        for kind in KINDS:
            for ext in _EXTS:
                hit = lower.get(f"{kind}.{ext}")
                if hit is not None:
                    wanted[kind] = hit
                    break
    if not wanted:
        raise ValueError("压缩包里没有可识别的声音（需要以 hover / click / open / close / success / error 命名的 ogg、mp3 或 wav 文件）")

    # 读取并校验被引用的音频（只读这些，其余成员不解出）
    blobs: dict[str, tuple[bytes, str]] = {}
    total = 0
    for kind, rel in wanted.items():
        info = members[rel]
        label = rel.rsplit("/", 1)[-1]
        ext = label.rsplit(".", 1)[-1].lower() if "." in label else ""
        if ext not in _EXTS:
            raise ValueError(f"{label} 的格式不受支持（只支持 ogg、mp3、wav）")
        if info.file_size > AUDIO_MAX:
            raise ValueError(f"{label} 超过 1MB")
        with zf.open(info) as fh:
            data = fh.read(AUDIO_MAX + 1)
        if len(data) > AUDIO_MAX:
            raise ValueError(f"{label} 超过 1MB")
        if _sniff(data) != ext:
            raise ValueError(f"{label} 不是有效的音频文件")
        total += len(data)
        if total > UNPACK_MAX:
            raise ValueError(f"音频文件合计超过 {UNPACK_MAX // 1024 // 1024}MB")
        blobs[kind] = (data, ext)

    name = name or _clean_name(name_fallback) or "音效包"
    root = _root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{uuid.uuid4().hex}"
    try:
        tmp.mkdir()
        sounds_meta: dict[str, str] = {}
        for kind in KINDS:
            if kind not in blobs:
                continue
            data, ext = blobs[kind]
            fname = f"{kind}.{ext}"
            (tmp / fname).write_bytes(data)
            sounds_meta[kind] = fname
        meta_out = {
            "name": name,
            "source": source,
            "sounds": sounds_meta,
            "size": total,
            "installed_at": time.time(),
        }
        (tmp / "pack.json").write_text(json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8")
        with _install_lock:
            base_id = _slug(name)
            pack_id = base_id
            n = 2
            while (root / pack_id).exists() or (root / pack_id).is_symlink():
                suffix = f"-{n}"
                pack_id = base_id[: 40 - len(suffix)].rstrip("-") + suffix
                n += 1
            os.rename(tmp, root / pack_id)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    logger.info("已安装音效包 %s（%s，来源 %s）", pack_id, "、".join(sounds_meta), source)
    return _view(pack_id, meta_out)


def _view(pack_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    sounds = meta.get("sounds") if isinstance(meta.get("sounds"), dict) else {}
    return {
        "id": pack_id,
        "name": str(meta.get("name") or pack_id),
        "source": str(meta.get("source") or ""),
        "sounds": [k for k in KINDS if k in sounds],
        "size": int(meta.get("size") or 0),
        "installed_at": meta.get("installed_at") or 0,
    }


def _read_meta(d: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads((d / "pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _open_zip(blob: bytes) -> zipfile.ZipFile:
    if len(blob) > ZIP_MAX:
        raise ValueError(f"压缩包超过 {ZIP_MAX // 1024 // 1024}MB")
    try:
        return zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("上传的文件不是有效的 zip 压缩包") from None


def _single_top(zf: zipfile.ZipFile) -> str:
    """所有成员都在同一个顶层目录下 → 返回它；否则 ""。"""
    tops: set[str] = set()
    for info in zf.infolist():
        n = _safe_member(info.filename)
        head, sep, _ = n.partition("/")
        if not sep:
            return ""
        tops.add(head)
    return next(iter(tops)) if len(tops) == 1 else ""


# ---------------------------------------------------------------------------
# 对外：安装 / 列表 / 删除 / 取文件
# ---------------------------------------------------------------------------


def install_zip(
    store: Any, data_dir: Path | str, blob: bytes, filename: str | None = None, source: str = "upload"
) -> dict[str, Any]:
    """装一份 zip 音效包，返回包视图（不含路径）。不合格抛 ValueError。"""
    with _open_zip(blob) as zf:
        top = _single_top(zf)
        stem = Path(urllib.parse.unquote(str(filename or "")).replace("\\", "/").split("/")[-1]).stem
        return _install_from_zip(data_dir, zf, base=top, name_fallback=top or stem, source=source)


def list_packs(data_dir: Path | str) -> list[dict[str, Any]]:
    root = _root(data_dir)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not _valid_id(d.name) or d.is_symlink() or not d.is_dir():
            continue
        meta = _read_meta(d)
        if meta is None:
            continue
        out.append(_view(d.name, meta))
    out.sort(key=lambda v: (v["installed_at"] or 0, v["id"]))
    return out


def delete_pack(store: Any, data_dir: Path | str, pack_id: str) -> None:
    if not _valid_id(pack_id):
        raise ValueError("音效包编号不正确")
    d = _pack_dir(data_dir, pack_id)
    if d is None:
        raise KeyError(pack_id)
    shutil.rmtree(d)
    bindings = _bindings(store)
    left = {t: p for t, p in bindings.items() if p != pack_id}
    if left != bindings:
        with store.tx() as conn:
            store.kv_set(conn, KV_SOUNDS, left)
    logger.info("已删除音效包 %s", pack_id)


def pack_file(data_dir: Path | str, pack_id: str, kind: str) -> tuple[Path, str] | None:
    if kind not in KINDS:
        return None
    d = _pack_dir(data_dir, pack_id)
    if d is None:
        return None
    meta = _read_meta(d)
    sounds = meta.get("sounds") if meta else None
    fname = sounds.get(kind) if isinstance(sounds, dict) else None
    if not isinstance(fname, str) or fname not in {f"{kind}.{e}" for e in _EXTS}:
        return None
    p = d / fname
    if p.is_symlink() or not p.is_file():
        return None
    return p, CONTENT_TYPES[fname.rsplit(".", 1)[1]]


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------


def _bindings(store: Any) -> dict[str, str]:
    raw = store.kv_get(KV_SOUNDS, {})
    if not isinstance(raw, dict):
        return {}
    return {str(t): str(p) for t, p in raw.items() if isinstance(p, str)}


def get_theme(store: Any, data_dir: Path | str) -> dict[str, Any]:
    default = store.kv_get(KV_DEFAULT, "default")
    if default not in THEMES:
        default = "default"
    sounds: dict[str, Any] = {}
    for theme, pack_id in _bindings(store).items():
        if theme not in THEMES or theme == "default":
            continue
        d = _pack_dir(data_dir, pack_id)
        meta = _read_meta(d) if d is not None else None
        if meta is None:
            continue
        view = _view(pack_id, meta)
        sounds[theme] = {
            "pack": pack_id,
            "name": view["name"],
            "files": {k: f"/api/sound-packs/{pack_id}/{k}" for k in view["sounds"]},
        }
    return {
        "default": default,
        "themes": [{"id": t, "name": THEME_NAMES[t]} for t in THEMES],
        "sounds": sounds,
    }


def set_theme(store: Any, data_dir: Path | str, body: Any) -> dict[str, Any]:
    """body：{default?, sounds?: {主题: pack_id | null}}。不合格抛 ValueError。"""
    if not isinstance(body, dict):
        raise ValueError("请求内容应为对象")
    new_default = None
    if "default" in body:
        new_default = body.get("default")
        if new_default not in THEMES:
            raise ValueError(f"未知主题：{new_default}")
    bindings = _bindings(store)
    changed = False
    if "sounds" in body:
        sounds = body.get("sounds")
        if not isinstance(sounds, dict):
            raise ValueError("sounds 应为对象")
        for theme, pack_id in sounds.items():
            if theme not in THEMES:
                raise ValueError(f"未知主题：{theme}")
            if pack_id in (None, ""):
                if theme in bindings:
                    bindings.pop(theme)
                    changed = True
                continue
            if theme == "default":
                raise ValueError("默认主题不能绑定音效包")
            if not _valid_id(pack_id) or _pack_dir(data_dir, pack_id) is None:
                raise ValueError(f"音效包 {pack_id} 不存在")
            bindings[theme] = pack_id
            changed = True
    with store.tx() as conn:
        if new_default is not None:
            store.kv_set(conn, KV_DEFAULT, new_default)
        if changed:
            store.kv_set(conn, KV_SOUNDS, bindings)
    return get_theme(store, data_dir)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def parse_github_url(url: Any) -> tuple[str, str, str | None, str]:
    """https://github.com/<owner>/<repo>[.git][/tree/<branch>[/<子目录>]] → (owner, repo, branch, 子目录)。"""
    raw = str(url or "").strip()
    bad = ValueError("只支持 https://github.com/<用户>/<仓库>[/tree/<分支>[/<子目录>]] 形式的公开仓库地址")
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        raise bad from None
    if parts.scheme != "https" or parts.netloc.lower() != "github.com":
        raise bad
    segs = [urllib.parse.unquote(s) for s in parts.path.rstrip("/").split("/")[1:]]
    if len(segs) < 2:
        raise bad
    owner, repo = segs[0], segs[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    for p in (owner, repo):
        if not _GH_PART_RE.fullmatch(p) or p in (".", ".."):
            raise bad
    if len(segs) == 2:
        return owner, repo, None, ""
    if segs[2] != "tree" or len(segs) < 4:
        raise bad
    branch = segs[3]
    if not _GH_PART_RE.fullmatch(branch) or branch in (".", ".."):
        raise bad
    sub = segs[4:]
    for s in sub:
        if s in ("", ".", "..") or "\\" in s or any(ord(c) < 32 for c in s):
            raise bad
    return owner, repo, branch, "/".join(sub)


async def _get(client: Any, url: str, *, stream: bool) -> Any:
    """手动跟随重定向，只允许 github 的几个主机。返回已发送的 response（stream=True 时未读体）。"""
    import httpx

    for _ in range(4):
        req = client.build_request("GET", url, headers={"User-Agent": "MaiWork", "Accept": "*/*"})
        resp = await client.send(req, stream=stream)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            await resp.aclose()
            nxt = httpx.URL(url).join(loc)
            if nxt.scheme != "https" or nxt.host not in _GH_HOSTS:
                raise ValueError("下载失败：GitHub 返回了指向其他网站的跳转，已拒绝")
            url = str(nxt)
            continue
        return resp
    raise ValueError("下载失败：跳转次数过多")


async def _download(owner: str, repo: str, branch: str | None, transport: Any) -> tuple[bytes, str]:
    import httpx

    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(HTTP_TIMEOUT_S), "follow_redirects": False}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        if branch is None:
            resp = await _get(client, f"https://api.github.com/repos/{owner}/{repo}", stream=False)
            if resp.status_code == 404:
                raise ValueError(f"找不到仓库 {owner}/{repo}（只支持公开仓库）")
            if resp.status_code != 200:
                raise ValueError(f"查询仓库信息失败（HTTP {resp.status_code}）")
            try:
                branch = resp.json().get("default_branch")
            except Exception:
                branch = None
            if not isinstance(branch, str) or not _GH_PART_RE.fullmatch(branch):
                raise ValueError("查询仓库信息失败：无法确定默认分支")
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
        resp = await _get(client, url, stream=True)
        try:
            if resp.status_code != 200:
                raise ValueError(f"下载失败：找不到分支 {branch} 或仓库不可访问（HTTP {resp.status_code}）")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > ZIP_MAX:
                raise ValueError(f"压缩包超过 {ZIP_MAX // 1024 // 1024}MB，已中止下载")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > ZIP_MAX:
                    raise ValueError(f"压缩包超过 {ZIP_MAX // 1024 // 1024}MB，已中止下载")
        finally:
            await resp.aclose()
    return bytes(buf), branch


async def install_github(store: Any, data_dir: Path | str, url: str, *, transport: Any = None) -> dict[str, Any]:
    """从 GitHub 公开仓库（可带分支 / 子目录）安装音效包。不合格或下载失败抛 ValueError。"""
    import httpx

    owner, repo, branch, sub = parse_github_url(url)
    source = f"https://github.com/{owner}/{repo}"
    if branch is not None:
        source += f"/tree/{branch}" + (f"/{sub}" if sub else "")
    try:
        blob, _branch = await asyncio.wait_for(_download(owner, repo, branch, transport), timeout=HTTP_TIMEOUT_S * 2)
    except asyncio.TimeoutError:
        raise ValueError("下载失败：连接 GitHub 超时") from None
    except httpx.HTTPError as e:
        logger.info("音效包 GitHub 下载失败：%s", type(e).__name__)
        raise ValueError("下载失败：无法连接 GitHub") from None

    def _work() -> dict[str, Any]:
        with _open_zip(blob) as zf:
            top = _single_top(zf)
            base = "/".join(p for p in (top, sub) if p)
            if sub and not any(_safe_member(i.filename).startswith(base + "/") for i in zf.infolist()):
                raise ValueError(f"仓库里没有子目录 {sub}")
            fallback = sub.rsplit("/", 1)[-1] if sub else repo
            return _install_from_zip(data_dir, zf, base=base, name_fallback=fallback, source=source)

    return await asyncio.to_thread(_work)
