"""skill 扩展（docs/02-设计.md §10「插件（skill + MCP）」、docs/07 §10.9）。

skill = <数据目录>/skills/<skill 名>/ 目录里的一份 SKILL.md（可附其他文件），
由管理员（root）放置，子 agent 只读。格式照 Agent Skills 规范（agentskills.io/specification）：
SKILL.md 开头 YAML front matter，认 name、description、metadata（一层映射），
另认 MaiWork 旧写法的顶层 roles。给谁用写在 metadata 的 maiwork-roles（「worker main」）或旧的 roles。

内置 skill：maiwork/builtin_skills/<名字>/（随插件发布，只读；如资讯标准 news-standard）。
和数据目录里同名的，以内置为准（数据目录那份不列、不读）；列表项带 builtin: true。

**不引入 yaml 库**：标准库手写极简解析，只认「key: value」行；roles 写成
「worker, main」或「[worker, main]」都行。front matter 里的 name 只当参考，
对外一律以目录名为准（防两份 skill 自报同一个名字）。

roles = 这个 skill 给谁用：["worker"]（默认，子 agent）/ ["main"]（主模型）/ 两者。
front matter 里的坏值容错回落 worker；list(role=…) / hint() 按角色过滤，
list_skills / read_skill 工具也照这个过滤（见 skills_tools.py）。

安全规则：
- read_file(name, rel) 只给 skill 目录内的文件：绝对路径、.. 越界一律拒；
- 符号链接一律拒（包括 skill 目录本身、目录内的文件）；线上 skill 目录 root 所有，
  子 agent 要用里面的脚本，得 read_skill 出来后 write_file 到自己工作区再跑，
  绝不直接执行数据目录里的东西。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("maiwork.skills")

_READ_MAX_BYTES = 40 * 1024          # SKILL.md / 附属文件一次最多读 40KB
_FRONT_KEYS = ("name", "description", "roles")
BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin_skills"   # 插件自带的 skill（只读）

KV_SKILLS_DISABLED = "extensions.skills.disabled"  # 手动停用的 skill 名单（网页 toggle 写入）


def _disabled_names(store: Any) -> list[str]:
    """kv 里手动停用的 skill 名单。store 没给（None）→ 空。"""
    if store is None:
        return []
    raw = store.kv_get(KV_SKILLS_DISABLED)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for n in raw:
        n_s = str(n or "").strip()
        if n_s and n_s not in out:
            out.append(n_s)
    return out


def set_disabled(store: Any, name: str, disabled: bool) -> None:
    """把 skill 名写进 / 摘出手动停用名单（不分来源；先幂等归一，再落库）。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("skill 名字不能为空")
    if store is None:
        raise ValueError("skill 开关需要 Store（没接库时改不了）")
    names = _disabled_names(store)
    if disabled and name not in names:
        names.append(name)
    elif not disabled:
        names = [n for n in names if n != name]
    with store.tx() as conn:
        store.kv_set(conn, KV_SKILLS_DISABLED, names)


def _search_preset_for(name: str) -> Any:
    """名字对应哪家搜索预设 → Preset；不认得 → None。只看名字，不管这名字是不是真内置。"""
    n = str(name or "").strip()
    if not n:
        return None
    try:
        from .search_presets import PRESETS
    except Exception:
        return None
    for p in PRESETS.values():
        if getattr(p, "skill", "") == n:
            return p
    return None


def _search_service_on(preset: Any, store: Any, settings: Any) -> bool:
    """「这家搜索服务开着吗」：merged MCP 里至少一条 preset 认得出 + enabled；
    store/settings 没给齐 → False（判不了就当没开，宁愿不注入/不泄露也不当开着）。
    判定中出错一律按关（保守，防 store 假故障时把停用 skill 漏给模型）。"""
    if store is None or settings is None:
        return False
    try:
        from .extensions_web import merged_entries
        from .search_presets import preset_of_url
    except Exception:
        return False
    try:
        for entry in merged_entries(settings, store):
            if not getattr(entry, "enabled", False):
                continue
            if preset_of_url(getattr(entry, "url", "")) is preset:
                return True
    except Exception:
        logger.exception("判定搜索服务 %s 的开关出错，按关", getattr(preset, "id", "?"))
        return False
    return False


def effective_status(
    name: str,
    store: Any,
    settings: Any,
    *,
    search_preset: Any = None,
) -> dict[str, Any]:
    """一个 skill 的「effective 生效态」+ 三个网页字段。

    - manual_enabled：没手动停用就是 True（store=None 的旧构造按 True 兜——不接库
      时保持旧的全开行为；**store 给了但读坏了**按“保守停用”，防假故障时泄露）。
    - enabled：通才 skill = manual_enabled；search skill（调用方按 search_preset 传入
      对应预设，且已核过这名字真是内置那份）= manual_enabled AND 对应搜索服务开着。
    - disabled_reason：enabled=False 时的中文一句；空串 = 开着。

    名字本身不检查「存在不存在」——那是 get_view/list_view 的事；这里只管策略。
    """
    if store is None:
        manual = True
    else:
        try:
            manual = str(name or "").strip() not in set(_disabled_names(store))
        except Exception:
            logger.exception("读 skill 手动停名单出错，按停用（保守，不泄露）")
            manual = False
    if not manual:
        return {"enabled": False, "manual_enabled": False, "disabled_reason": "已手动停用"}
    if search_preset is not None and not _search_service_on(search_preset, store, settings):
        return {"enabled": False, "manual_enabled": True, "disabled_reason": "对应的搜索服务未开启"}
    return {"enabled": True, "manual_enabled": True, "disabled_reason": ""}


class Skills:
    """<数据目录>/skills/ 的只读视图。data_dir 传 settings.data_dir 即可。

    store / settings 给了就接「skill 开关」（kv + MCP 扩展开关）：
    - 手动停用的 skill 不再列 / 读 / 给 hint；
    - search-<preset> 的还看「对应搜索服务开没开」（MCP 扩展 enabled）；
    没给保持旧行为（全部看得见，不引入依赖）。
    """

    def __init__(
        self,
        data_dir: Path | str,
        builtin_root: Path | str | None = BUILTIN_ROOT,
        store: Any = None,
        settings: Any = None,
    ) -> None:
        self._root = Path(data_dir) / "skills"
        self._builtin_root = Path(builtin_root) if builtin_root else None
        self._store = store
        self._settings = settings

    def _settings_now(self) -> Any:
        """settings 可以是 Settings 对象或一个无参返回 Settings 的可调用（
        MaiWorkApp 的 get_settings 会跟着规则覆盖变化；每次都现判，不许缓存）。"""
        s = self._settings
        if s is None:
            return None
        if callable(s):
            try:
                return s()
            except Exception:
                logger.exception("取 settings 出错，按没配（search skill 当服务未开）")
                return None
        return s

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list(self, role: str | None = None) -> list[dict[str, Any]]:
        """[{name, description, roles, path}]，按名字排序，最多 20 个；被停用的不出现。

        role 给了就只要 roles 含这个角色的（主模型 role="main"、子 agent role="worker"）；
        不给（None）返回全部角色——但这是**模型/工具视角的全量**：手动停用或服务未开启
        的 skill 照样不出现（要「管理视角看全部 + 开关字段」走 skills_web.list_view，
        它带 enabled / manual_enabled / disabled_reason，不过滤）。
        """
        out: list[dict[str, Any]] = []
        for name, path, builtin in self._skill_dirs():
            if not self.is_effectively_active(name):
                continue
            text = self._read_head(path / "SKILL.md")
            if text is None:
                continue
            front = parse_front_matter(text)
            description = str(front.get("description") or "")
            roles = _roles_of(front)
            if role is not None and role not in roles:
                continue
            out.append(
                {
                    "name": name,
                    "description": description,
                    "roles": roles,
                    "path": str(path),
                    "builtin": builtin,
                }
            )
        out.sort(key=lambda i: str(i["name"]))
        return out[:20]

    def hint(self, role: str = "worker") -> str:
        """「名字：一句描述」清单（每行一个，最多 20 条）；没有 skill 返回空串。

        默认只列 roles 含 worker 的——这个清单是挂进子 agent system 提示用的，
        不能把只给主模型的 skill 名字漏给子 agent。
        """
        lines: list[str] = []
        for item in self.list(role):
            name = str(item.get("name") or "")
            desc = str(item.get("description") or "").strip()
            lines.append(f"- {name}：{desc}" if desc else f"- {name}")
        return "\n".join(lines)

    def roles(self, name: str) -> list[str] | None:
        """某个 skill 的 roles（没这个 skill / 读不到 / 被停用 → None）；坏值回落 worker。"""
        if not self.is_effectively_active(name):
            return None
        path = self._skill_path(name)
        if path is None:
            return None
        text = self._read_head(path / "SKILL.md")
        if text is None:
            return None
        return _roles_of(parse_front_matter(text))

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 开关状态（effective / manual / disabled_reason）
    # ------------------------------------------------------------------

    def status_of(self, name: str) -> dict[str, Any]:
        """一个 skill 的开关状态：{enabled, manual_enabled, disabled_reason}。
        只查策略不读盘；name 不存在也照算（存在性由调用方判）。
        没接 store/settings 时按「全开着」兜（旧行为）。
        服务闸只给「内置那份 search-<preset>」上——用户在数据目录起个同名 skill
        （内置 优先规则下根本轮不到它）不吃服务闸，和普通通才 skill 一样默认开。
        """
        n = str(name or "")
        preset = None
        if _search_preset_for(n) is not None and self._skill_path_in(self._builtin_root, n) is not None:
            preset = _search_preset_for(n)
        return effective_status(n, self._store, self._settings_now(), search_preset=preset)

    def is_effectively_active(self, name: str) -> bool:
        """effective 生效态：模型能不能看到 / 读到这个 skill。

        异常路径（store/settings 假故障）fail-closed 返回 False：宁岂这轮不给模型用，
        也不要把「真被停了」的 skill 漏出去。
        """
        try:
            return bool(self.status_of(name).get("enabled", True))
        except Exception:
            logger.exception("判 skill %s 开关状态出错，按停用", name)
            return False

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read(self, name: str) -> str | None:
        """SKILL.md 全文（≤40KB）；不存在 / 名字不合法 / 符号链接 / 被停用 → None。"""
        if not self.is_effectively_active(name):
            return None
        path = self._skill_path(name)
        if path is None:
            return None
        return self._read_head(path / "SKILL.md", full=True)

    def read_file(self, name: str, rel: str) -> str | None:
        """skill 目录内的附属文件（≤40KB）；越界 / 绝对路径 / 符号链接 / 被停用 → None。"""
        if not self.is_effectively_active(name):
            return None
        path = self._skill_path(name)
        if path is None:
            return None
        rel_s = str(rel or "").strip()
        if not rel_s or rel_s.startswith("/") or rel_s.startswith("\\"):
            return None
        # 逐段防 .. 越界（拼完还会用 realpath 复核）
        parts = rel_s.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
        target = path.joinpath(*parts)
        # 符号链复核：realpath 必须在 skill 目录的 realpath 之内；
        # 且原路径每一段都不是符号链接（防「中间目录是指向外面的链接」）
        try:
            root_real = os.path.realpath(str(path))
            target_real = os.path.realpath(str(target))
        except OSError:
            return None
        if target_real == root_real or not target_real.startswith(root_real + os.sep):
            return None
        if _has_symlink_in_chain(path, parts):
            return None
        return self._read_head(Path(target_real), full=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _dirs_in(root: Path | None) -> list[tuple[str, Path]]:
        if root is None:
            return []
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except (OSError, FileNotFoundError):
            return []
        out: list[tuple[str, Path]] = []
        for p in entries:
            try:
                if not p.is_dir() or p.is_symlink() or p.name.startswith("."):
                    continue
            except OSError:
                continue
            out.append((p.name, p))
        return out

    def _skill_dirs(self) -> list[tuple[str, Path, bool]]:
        """(名字, 目录, 是否内置) 列表：只看普通目录（符号链接不算），名字按目录名；
        数据目录里和内置同名的跳过（以内置为准）。"""
        builtin = self._dirs_in(self._builtin_root)
        names = {n for n, _p in builtin}
        out = [(n, p, True) for n, p in builtin]
        out += [(n, p, False) for n, p in self._dirs_in(self._root) if n not in names]
        return out

    def is_builtin(self, name: str) -> bool:
        return self._skill_path_in(self._builtin_root, name) is not None

    @staticmethod
    def _skill_path_in(root: Path | None, name: str) -> Path | None:
        n = str(name or "").strip()
        if root is None or not n or "/" in n or "\\" in n or n in (".", "..") or n.startswith("."):
            return None
        p = root / n
        try:
            if p.is_symlink() or not p.is_dir():
                return None
        except OSError:
            return None
        return p

    def _skill_path(self, name: str) -> Path | None:
        """名字 → skill 目录（内置优先）；符号链接 / 不是目录 → None。"""
        return self._skill_path_in(self._builtin_root, name) or self._skill_path_in(self._root, name)

    @staticmethod
    def _read_head(path: Path, *, full: bool = False) -> str | None:
        """读文本文件；full=True 截 40KB，否则只读前 4KB（够 front matter）。"""
        try:
            if path.is_symlink() or not path.is_file():
                return None
            limit = (_READ_MAX_BYTES if full else 4096) + 1
            with open(path, "rb") as f:
                raw = f.read(limit)
        except (OSError, ValueError):
            return None
        cap = _READ_MAX_BYTES if full else 4096
        text = raw[:cap].decode("utf-8", errors="replace")
        return text


def _has_symlink_in_chain(base: Path, parts: list[str]) -> bool:
    """从 base 往下逐段查，任何一段是符号链接 → True。"""
    cur = base
    for part in parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                return True
        except OSError:
            return True
    return False


# ----------------------------------------------------------------------
# front matter 极简解析（标准库手写；只认 key: value 行，不引入 yaml 库）
# ----------------------------------------------------------------------


def parse_front_matter(text: str) -> dict[str, Any]:
    """SKILL.md 开头的 front matter → {name, description, roles}（都是 str，roles 例外见下）。

    - 第一行必须恰是 "---"；找到下一个恰是 "---" 的行结束；找不到/没有 → {}。
    - 中间只认「key: value」行（key 字母/数字/下划线；# 开头和空行跳过）；
      值去掉首尾空白，去掉成对的单/双引号。
    - roles 额外解析：「worker, main」或「[worker, main]」→ ["worker", "main"]（原样 str 列表）。
    """
    lines = str(text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    out: dict[str, Any] = {}
    in_metadata = False
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        indented = line[:1] in (" ", "\t")
        key, _, value = stripped.partition(":")
        key = key.strip()
        if indented:
            # metadata 下面的一层「key: value」（Agent Skills 规范：字符串到字符串的映射）
            if in_metadata:
                v = value.strip()
                if len(v) >= 2 and v.startswith(("'", '"')) and v.endswith(v[0]):
                    v = v[1:-1]
                out.setdefault("metadata", {})[key] = v
            continue
        in_metadata = key == "metadata" and not value.strip()
        if in_metadata:
            out.setdefault("metadata", {})
            continue
        if key not in _FRONT_KEYS:
            continue  # 顶层只认 name / description / roles（license 等规范字段不影响行为）
        value = value.strip()
        if len(value) >= 2 and value.startswith(("'", '"')) and value.endswith(value[0]):
            value = value[1:-1]
        if key == "roles":
            out[key] = _parse_roles_value(value)
        else:
            out[key] = value
    return out


def _parse_roles_value(value: str) -> list[str]:
    """「worker, main」/「[worker, main]」→ ["worker", "main"]。"""
    v = str(value or "").strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    items: list[str] = []
    for part in v.split(","):
        p = part.strip().strip("'\"")
        if p:
            items.append(p)
    return items


def _roles_of(front: dict[str, Any]) -> list[str]:
    """front matter → roles：旧的顶层 roles 优先；否则 metadata.maiwork-roles（空格或逗号分隔）。"""
    if front.get("roles"):
        return _normalize_roles(front.get("roles"))
    meta = front.get("metadata") or {}
    raw = str(meta.get("maiwork-roles") or "") if isinstance(meta, dict) else ""
    return _normalize_roles(raw.replace(" ", ","))


def _normalize_roles(raw: Any) -> list[str]:
    """front matter 的 roles → ["worker"] / ["main"] / 两者；坏值回落 worker。"""
    items: list[str] = []
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    elif isinstance(raw, str):
        items = _parse_roles_value(raw)
    roles = [r for r in items if r in ("worker", "main")]
    return roles or ["worker"]
