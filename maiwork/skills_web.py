"""网页管理 skill 的数据层（docs/02 §10、docs/07 §10.9）。

- 网页新增 / 修改的 skill 落到 <data_dir>/skills/<名字>/SKILL.md：
  内容 = front matter（name、description、roles）+ 正文；单个 SKILL.md ≤ 40KB；
  roles 必须是列表、只能含 worker/main、不能为空（不合法 → 400，不静默回落；
  没传时新增默认 worker、修改保持原值；SKILL.md 里手写的坏值仍容错回落 worker）；
  名字只允许 [A-Za-z0-9_-]{1,64}；目录尽量 0700（exFAT chmod 失败不报错，同 store._chmod）。
- 网页加的记进 kv["extensions.skills.web"] = [名字]；目录里手动放的标 source="file"。
- 删除：拒绝符号链接；realpath 必须在 skills 根目录之内（绝不越界删外面的东西）。
- 附属文件本期不支持网页上传，GET 时只读列出文件名。
- exFAT：新建文件先 touch 再写（原子写 tempfile+link 在 exFAT 上会 ENOTSUP）。
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Any

from . import clock
from .skills import (
    BUILTIN_ROOT,
    KV_SKILLS_DISABLED,
    _roles_of,
    _search_preset_for,
    effective_status,
    parse_front_matter,
    set_disabled,
)

logger = logging.getLogger("maiwork.skills_web")

KV_SKILLS_WEB = "extensions.skills.web"   # 网页加过的 skill 名单
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIZE_MAX_BYTES = 40 * 1024               # 单个 SKILL.md ≤ 40KB
_FILES_LIST_MAX = 50


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "skills"


def _builtin_dir(name: str) -> Path | None:
    """内置 skill（maiwork/builtin_skills/<名字>/，随插件发布、只读）；没有 → None。"""
    n = str(name or "").strip()
    if not _NAME_RE.match(n):
        return None
    p = BUILTIN_ROOT / n
    try:
        if p.is_symlink() or not p.is_dir() or not (p / "SKILL.md").is_file():
            return None
    except OSError:
        return None
    return p


def _builtin_names() -> list[str]:
    try:
        return sorted(p.name for p in BUILTIN_ROOT.iterdir() if _builtin_dir(p.name) is not None)
    except OSError:
        return []


_BUILTIN_READONLY = "内置 skill 随插件发布，网页上不能改或删（要改标准得发新版本）"


def _chmod(p: Path, mode: int) -> None:
    try:
        os.chmod(p, stat.S_IMODE(mode))
    except OSError:
        pass  # exFAT 等不支持权限的文件系统上 chmod 可能失败，不报错


def _web_names(store: Any) -> list[str]:
    raw = store.kv_get(KV_SKILLS_WEB)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for n in raw:
        n_s = str(n or "").strip()
        if _NAME_RE.match(n_s) and n_s not in out:
            out.append(n_s)
    return out


def _mark_web(store: Any, name: str) -> None:
    names = _web_names(store)
    if name in names:
        return
    names.append(name)
    with store.tx() as conn:
        store.kv_set(conn, KV_SKILLS_WEB, names)


def _unmark_web(store: Any, name: str) -> None:
    names = [n for n in _web_names(store) if n != name]
    with store.tx() as conn:
        store.kv_set(conn, KV_SKILLS_WEB, names)


def _safe_skill_dir(root: Path, name: str) -> Path:
    """名字 → skills 根下的目录；不合法 / 越界 → ValueError。只看路径形态，不碰盘。"""
    n = str(name or "").strip()
    if not _NAME_RE.match(n):
        raise ValueError("skill 名字不合法（只能用字母、数字、下划线、横线，1~64 个字符）")
    return root / n


def _render(name: str, description: str, roles: list[str], body: str) -> str:
    """front matter + 正文。description 单行化；roles 逗号分隔（解析端两种写法都认）。"""
    desc = " ".join(str(description or "").split())
    roles_s = ", ".join(roles) if roles else "worker"
    text = f"---\nname: {name}\ndescription: {desc}\nroles: {roles_s}\n---\n\n{body}"
    if not text.endswith("\n"):
        text += "\n"
    return text


def _check_size(text: str) -> None:
    size = len(text.encode("utf-8"))
    if size > _SIZE_MAX_BYTES:
        raise ValueError(f"SKILL.md 太大了（{size} 字节），单个最多 {_SIZE_MAX_BYTES} 字节（40KB）")


def _write_skill_md(skill_dir: Path, text: str) -> None:
    """exFAT 规矩：新建文件先 touch 再写。"""
    target = skill_dir / "SKILL.md"
    if not target.exists():
        target.touch()
    target.write_text(text, encoding="utf-8")


def _split_body(text: str) -> str:
    """从 SKILL.md 全文里拆出正文（front matter 之后的东西）。"""
    lines = str(text or "").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return str(text or "")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            rest = "".join(lines[i + 1:])
            return rest.lstrip("\n")
    return str(text or "")


def _stat_item(
    skill_dir: Path,
    name: str,
    text: str,
    source: str,
    settings: Any = None,
    store: Any = None,
) -> dict[str, Any]:
    front = parse_front_matter(text)
    st = os.stat(skill_dir / "SKILL.md")
    # effective 开关态（manual / service gate）现查；settings/store 没给按全开兜。
    # 服务闸只给内置那份 search-<preset>（source==builtin 且 preset 认得出）。
    preset = _search_preset_for(name) if source == "builtin" else None
    status = effective_status(name, store, settings, search_preset=preset)
    return {
        "name": name,
        "description": str(front.get("description") or ""),
        "roles": _roles_of(front),
        "source": source,
        "size": int(st.st_size),
        "updated_ts": float(st.st_mtime),
        "enabled": bool(status.get("enabled", True)),
        "manual_enabled": bool(status.get("manual_enabled", True)),
        "disabled_reason": str(status.get("disabled_reason") or ""),
    }


def _list_files(skill_dir: Path) -> list[str]:
    """附属文件相对路径（不含 SKILL.md；不跟符号链接，最多 _FILES_LIST_MAX 条）。"""
    out: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
            # 符号链接目录不进去（followlinks=False 已把链接当叶子，这里再把它们移出 dirnames 保险）
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if p.is_symlink() or fn.startswith("._"):  # macOS 在 exFAT 上生成的 AppleDouble 不算
                    continue
                rel = p.relative_to(skill_dir).as_posix()
                if rel == "SKILL.md":
                    continue
                out.append(rel)
                if len(out) >= _FILES_LIST_MAX:
                    return out
    except OSError:
        return out
    return out


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------


def list_view(data_dir: Path | str, store: Any, settings: Any = None) -> list[dict[str, Any]]:
    """GET /api/extensions 的 skills 段：[{name, description, roles, source, size, updated_ts,
    enabled, manual_enabled, disabled_reason}]。

    每次现读目录（没有缓存，改完下一次就是新的）。
    """
    root = _root(data_dir)
    web = set(_web_names(store))
    out: list[dict[str, Any]] = []
    builtin = _builtin_names()
    for n in builtin:
        view = get_view(data_dir, store, n, settings)
        if view is not None:
            view.pop("body", None)
            view.pop("files", None)
            out.append(view)
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except (OSError, FileNotFoundError):
        return out
    for p in entries:
        try:
            if not p.is_dir() or p.is_symlink() or p.name in builtin:
                continue
            target = p / "SKILL.md"
            if target.is_symlink() or not target.is_file():
                continue
            text = target.read_bytes()[: _SIZE_MAX_BYTES + 1].decode("utf-8", errors="replace")
            out.append(_stat_item(p, p.name, text, "web" if p.name in web else "file", settings=settings, store=store))
        except OSError:
            continue
    return out


def get_view(
    data_dir: Path | str,
    store: Any,
    name: str,
    settings: Any = None,
) -> dict[str, Any] | None:
    """GET /api/extensions/skills/{name}：{name, description, roles, body, source, files,
    enabled, manual_enabled, disabled_reason}；
    不存在 / 不合法 / 符号链接 → None。被停用的也照常返回（管理员要看）。"""
    root = _root(data_dir)
    builtin = _builtin_dir(name)
    try:
        skill_dir = builtin or _safe_skill_dir(root, name)
    except ValueError:
        return None
    try:
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            return None
        target = skill_dir / "SKILL.md"
        if target.is_symlink() or not target.is_file():
            return None
        text = target.read_bytes()[: _SIZE_MAX_BYTES + 1].decode("utf-8", errors="replace")
        source = "builtin" if builtin else ("web" if skill_dir.name in set(_web_names(store)) else "file")
        item = _stat_item(skill_dir, skill_dir.name, text, source, settings=settings, store=store)
        item["body"] = _split_body(text)[:_SIZE_MAX_BYTES]
        item["files"] = _list_files(skill_dir)
        return item
    except OSError:
        return None


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------


def _normalize_input_roles(raw: Any, problems: list[str]) -> list[str] | None:
    """网页提交的 roles：没传（None）→ None，由调用方决定默认还是保持原值。

    必须是列表、只能含 "worker" / "main"、**不能为空**；不合法往 problems 里记
    （接口层统一 → 400），不再静默回落 worker。SKILL.md front matter 那侧的容错在
    skills.py（管理员手写文件，坏值回落 worker 不拖垮）。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        problems.append("roles 要是列表")
        return None
    values = [str(r).strip() for r in raw]
    if [v for v in values if v and v not in ("worker", "main")]:
        problems.append('roles 只认 "worker" / "main"')
    roles = sorted({v for v in values if v in ("worker", "main")})
    if not roles:
        problems.append("roles 不能为空（至少选一个：worker / main）")
    return roles or None


def create(data_dir: Path | str, store: Any, body: dict[str, Any]) -> dict[str, Any]:
    """网页新增 skill。ValueError 校验失败；FileExistsError 重名（含目录里已有的）。"""
    root = _root(data_dir)
    name = str((body or {}).get("name") or "").strip()
    skill_dir = _safe_skill_dir(root, name)  # ValueError：名字不合法
    problems: list[str] = []
    description = str((body or {}).get("description") or "").strip()
    roles = _normalize_input_roles((body or {}).get("roles"), problems)
    text_body = str((body or {}).get("body") or "")
    if problems:
        raise ValueError("；".join(problems))
    text = _render(name, description, roles if roles is not None else ["worker"], text_body)
    _check_size(text)
    if _builtin_dir(name) is not None:
        raise FileExistsError(f"「{name}」是内置 skill 的名字，换一个")
    if skill_dir.exists() or skill_dir.is_symlink():
        raise FileExistsError(f"已经有叫「{name}」的 skill 了（重名）")
    root.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir()
    _chmod(skill_dir, 0o700)
    _write_skill_md(skill_dir, text)
    _mark_web(store, name)
    logger.info("网页新增 skill %s", name)
    view = get_view(data_dir, store, name)
    assert view is not None
    return view


def update(data_dir: Path | str, store: Any, name: str, body: dict[str, Any]) -> dict[str, Any]:
    """网页修改 skill（description / roles / body 传了才改）。KeyError 不存在。"""
    root = _root(data_dir)
    name = str(name or "").strip()
    if _builtin_dir(name) is not None:
        raise PermissionError(_BUILTIN_READONLY)
    skill_dir = _safe_skill_dir(root, name)  # ValueError：名字不合法
    try:
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            raise KeyError(name)
    except OSError:
        raise KeyError(name) from None
    current = get_view(data_dir, store, name)
    if current is None:
        raise KeyError(name)
    problems: list[str] = []
    if (body or {}).get("description") is not None:
        description = str((body or {}).get("description") or "").strip()
    else:
        description = str(current.get("description") or "")
    if (body or {}).get("roles") is not None:
        roles = _normalize_input_roles((body or {}).get("roles"), problems)
    else:
        roles = None
    if roles is None:  # 没传 / 坏值：先把原值守住（坏值的 problems 下面会 raise）
        roles = [str(r) for r in (current.get("roles") or ["worker"])]
    if (body or {}).get("body") is not None:
        text_body = str((body or {}).get("body") or "")
    else:
        text_body = str(current.get("body") or "")
    if problems:
        raise ValueError("；".join(problems))
    text = _render(name, description, roles, text_body)
    _check_size(text)
    _write_skill_md(skill_dir, text)
    logger.info("网页修改 skill %s", name)
    view = get_view(data_dir, store, name)
    assert view is not None
    return view


def delete(data_dir: Path | str, store: Any, name: str) -> None:
    """删掉整个 skill 目录。KeyError 不存在；PermissionError 符号链接（拒不碰目标）；
    ValueError 名字不合法（含越界形态）。realpath 必须在 skills 根之内。
    """
    root = _root(data_dir)
    name = str(name or "").strip()
    if _builtin_dir(name) is not None:
        raise PermissionError(_BUILTIN_READONLY)
    # 名字白名单本身就挡掉了 .. / 斜杠等越界形态（_safe_skill_dir 抛 ValueError）
    skill_dir = _safe_skill_dir(root, name)
    try:
        is_link = skill_dir.is_symlink()
        is_dir = skill_dir.is_dir()
        # realpath 双保险：必须在 skills 根的 realpath 之内
        root_real = os.path.realpath(str(root))
        dir_real = os.path.realpath(str(skill_dir))
    except OSError:
        raise KeyError(name) from None
    # 注意：PermissionError 是 OSError 的子类，这个判断必须在 try 外面，别被吞成 KeyError
    if is_link:
        raise PermissionError(f"skill「{name}」是符号链接，拒绝删除（不碰它指向的东西）")
    if not is_dir:
        raise KeyError(name)
    if dir_real == root_real or not dir_real.startswith(root_real + os.sep):
        raise PermissionError(f"skill「{name}」的路径越出 skills 目录，拒绝删除")
    shutil.rmtree(dir_real)  # 不跟符号链接：目录里的链接只删链接本身
    _unmark_web(store, name)
    logger.info("网页删除 skill %s", name)


def _now_ts() -> float:
    return clock.now()


# ----------------------------------------------------------------------
# 开关（POST /api/extensions/skills/{name}/toggle）
# ----------------------------------------------------------------------


def toggle(data_dir: Path | str, store: Any, name: str, enabled: bool) -> None:
    """网页把 skill 开关拨成 开/关：存 kv["extensions.skills.disabled"] 名单。

    任何来源（builtin / web / file）都能拨——builtin 那份不改文件，只改开关 kv。
    KeyError 没有这个 skill；ValueError 名字不合法。
    """
    name = str(name or "").strip()
    if get_view(data_dir, store, name) is None:
        raise KeyError(name)
    set_disabled(store, name, not enabled)
    logger.info("网页把 skill %s 开关拨成 %s", name, "开" if enabled else "关")


# ----------------------------------------------------------------------
# zip 安装（POST /api/extensions/skills/upload）
# ----------------------------------------------------------------------
#
# 规则（照通用 skill 安装规范）：
# - zip 里要有且只有一份 SKILL.md（根目录，或唯一顶层目录里）；
# - 名字取 front matter 的 name，没有就取顶层目录名 / zip 文件名（multipart 的 filename，
#   或请求头 X-Filename，前端 encodeURIComponent 过）；
# - 必须 ^[A-Za-z0-9_-]{1,64}$；重名 409 除非 ?replace=1；
# - 拒绝：绝对路径、..、符号链接（zip 外部属性的 symlink 位）、>200 个文件、
#   解压总量 >20MB、单份 SKILL.md >40KB、zip 本身 >5MB；
# - 解压到 <data_dir>/skills/<名>/（先解到临时目录再原子替换），目录 0700，记 kv 标 source=web。

_ZIP_MAX_BYTES = 5 * 1024 * 1024        # zip 本身 ≤5MB
_ZIP_FILES_MAX = 200                     # 最多 200 个文件（不含目录）
_ZIP_UNPACK_MAX = 20 * 1024 * 1024      # 解压后总量 >20MB 拒
_ZIP_SKILL_MD_MAX = 40 * 1024            # 单份 SKILL.md >40KB 拒


def _is_symlink_info(info: Any) -> bool:
    """zip 外部属性里的 symlink 位（*nix：高 16 位是 st_mode，S_IFLNK = 0o120xxx）。"""
    mode = (int(getattr(info, "external_attr", 0)) >> 16) & 0o170000
    return mode == 0o120000


def _safe_zip_member(name: str) -> str:
    """拒绝绝对路径 / .. / 空段；返回 POSIX 化的相对路径；不合格抛 ValueError（中文）。"""
    n = str(name or "").replace("\\", "/")
    if not n or n.endswith("/"):
        raise ValueError(f"zip 里的路径 {name!r} 不合法（空或目录）")
    if n.startswith("/"):
        raise ValueError(f"zip 里有绝对路径 {name!r}，拒绝（越界）")
    parts = n.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"zip 里有越界路径 {name!r}（含 ..），拒绝")
    return n


def _zipfilename_to_name(filename: str) -> str:
    """zip 文件名（可能带 URL 编码 / .zip 尾） → 备选 skill 名。"""
    import urllib.parse

    base = Path(urllib.parse.unquote(str(filename or "")).split("/")[-1]).stem
    return base.strip()


def _pick_skill_name(infos: list[Any], skill_md_rel: str, fallback_names: list[str]) -> str:
    """优先级：front matter name > 顶层目录名 > zip 文件名。非法抛 ValueError。"""
    # front matter
    for info in infos:
        if info.filename == skill_md_rel:
            text = info  # placeholder replaced below
            break
    return ""  # 由 install_zip 内联做完（zip 读内容要用 zf.read，不在这一层）


def _normalize_uploaded_name(infos: Any, skill_md_rel: str, filename: str, zip_file: Any) -> str:
    """front matter name → 顶层目录 → zip 文件名。"""
    raw_text = zip_file.read(skill_md_rel)[:_ZIP_SKILL_MD_MAX].decode("utf-8", errors="replace")
    front = parse_front_matter(raw_text)
    cand = str(front.get("name") or "").strip()
    if cand:
        return cand
    rel = _safe_zip_member(skill_md_rel)
    parts = rel.split("/")
    if len(parts) > 1 and parts[0]:
        return parts[0].strip()
    return _zipfilename_to_name(filename)


def _atomic_replace_dir(src: Path, dst: Path) -> None:
    """src 整体挪到 dst（同文件系统）；dst 已存在先删。exFAT 上 rename 目录可用。"""
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(os.path.realpath(str(dst)))
    src.rename(dst)


def _zip_common_top(infos: list[Any]) -> str:
    """所有文件都挂在同一个顶层目录下 → 返回那个目录名；否则 ""（根/SKILL.md）。"""
    tops: set[str] = set()
    for info in infos:
        rel = _safe_zip_member(info.filename)
        first = rel.split("/", 1)[0]
        tops.add(first)
    if len(tops) == 1:
        return next(iter(tops))
    return ""


def install_zip(store: Any, data_dir: Path | str, blob: bytes, *, filename: str = "", replace: bool = False) -> dict[str, Any]:
    """把 zip 包装进 skills 根。各项拒绝抛 ValueError（中文）；重名不带 replace 抛 FileExistsError。"""
    if len(blob) > _ZIP_MAX_BYTES:
        raise ValueError(f"zip 最多 {_ZIP_MAX_BYTES // 1024 // 1024}MB，这份 {len(blob)} 字节")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("这不是一份合法的 zip 包") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise ValueError("zip 里没有文件")
        if len(infos) > _ZIP_FILES_MAX:
            raise ValueError(f"zip 最多 {_ZIP_FILES_MAX} 个文件，这份 {len(infos)} 个")
        # 符号链接（zip 外部属性的 symlink 位）在总量检查前先杀
        for info in infos:
            if _is_symlink_info(info):
                raise ValueError(f"zip 里有符号链接 {info.filename!r}，拒绝（要文件本体）")
        total = 0
        skill_md_candidates: list[str] = []
        for info in infos:
            rel = _safe_zip_member(info.filename)
            size = int(getattr(info, "file_size", 0) or 0)
            if size < 0 or size > _ZIP_UNPACK_MAX:
                raise ValueError(f"zip 里 {rel!r} 太大了")
            total += size
            if total > _ZIP_UNPACK_MAX:
                raise ValueError(f"解压后总量超过 {(_ZIP_UNPACK_MAX // 1024 // 1024)}MB，拒绝")
            if Path(rel).name == "SKILL.md":
                skill_md_candidates.append(rel)
        if not skill_md_candidates:
            raise ValueError("zip 里没有 SKILL.md（要有且只有一份：根目录或唯一顶层目录里）")
        if len(skill_md_candidates) > 1:
            raise ValueError(f"zip 里只能有一份 SKILL.md，找到 {len(skill_md_candidates)} 份")
        skill_md_rel = skill_md_candidates[0]
        # SKILL.md 大小（压缩包标的）
        md_size = next(int(getattr(i, "file_size", 0) or 0) for i in infos if _safe_zip_member(i.filename) == skill_md_rel)
        if md_size > _ZIP_SKILL_MD_MAX:
            raise ValueError(f"SKILL.md 太大了（{md_size} 字节），单个最多 {_ZIP_SKILL_MD_MAX} 字节（40KB）")
        # 名字优先级：front matter name > 顶层目录 > zip 文件名
        name = _normalize_uploaded_name(infos, skill_md_rel, filename, zf)
        if not _NAME_RE.match(name):
            raise ValueError(f"skill 名字 {name!r} 不合法（只能用字母、数字、下划线、横线，1~64 个字符）")
        if _builtin_dir(name) is not None:
            raise FileExistsError(f"「{name}」是内置 skill 的名字，不能用上传覆盖")
        root = _root(data_dir)
        target = _safe_skill_dir(root, name)  # ValueError：路径不合法
        if (target.exists() or target.is_symlink()) and not replace:
            raise FileExistsError(f"已经有叫「{name}」的 skill 了（重名）")
        # 所有文件都在同一个顶层目录下（且那个顶层目录不是单份 SKILL.md 之类的根文件） →
        # 剥掉这层（skill 内容 = 顶层目录里的东西）；否则按原相对路径放（SKILL.md 就在根）。
        tops = {_safe_zip_member(i.filename).split("/", 1)[0] for i in infos}
        common_top = next(iter(tops)) if (len(tops) == 1 and Path(next(iter(tops))).name != "SKILL.md") else ""
        # 先解到临时目录，校验都过才原子替换
        root.mkdir(parents=True, exist_ok=True)
        tmp_dir = target.parent / (target.name + ".uploading")
        try:
            if tmp_dir.exists() or tmp_dir.is_symlink():
                shutil.rmtree(os.path.realpath(str(tmp_dir)))
            tmp_dir.mkdir(parents=True)
            _chmod(tmp_dir, 0o700)
            for info in infos:
                rel = _safe_zip_member(info.filename)
                if _is_symlink_info(info):
                    raise ValueError(f"zip 里有符号链接 {rel!r}，拒绝")
                if common_top:
                    parts = rel.split("/", 1)
                    if len(parts) < 2 or not parts[1]:
                        continue  # 顶层目录本身的目录项（防御）
                    rel = parts[1]
                dest = tmp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    dest.touch()  # exFAT：新建文件先 touch 再写
                dest.write_bytes(zf.read(info.filename))
            _atomic_replace_dir(tmp_dir, target)
            _chmod(target, 0o700)
        except Exception:
            try:
                if tmp_dir.exists():
                    shutil.rmtree(os.path.realpath(str(tmp_dir)))
            except OSError:
                pass
            raise
    _mark_web(store, name)
    logger.info("zip 安装 skill %s（replace=%s）", name, replace)
    view = get_view(data_dir, store, name)
    if view is None:
        raise RuntimeError("装完读不回视图")
    return view
