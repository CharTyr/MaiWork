"""skill 扩展（docs/02-设计.md §10「插件（skill + MCP）」、docs/07 §10.9）。

skill = <数据目录>/skills/<skill 名>/ 目录里的一份 SKILL.md（可附其他文件），
由管理员（root）放置，子 agent 只读。SKILL.md 开头可以是 YAML front matter
（--- 包起来的若干 key: value 行），只认三个键：name、description、roles。

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


class Skills:
    """<数据目录>/skills/ 的只读视图。data_dir 传 settings.data_dir 即可。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir) / "skills"

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list(self, role: str | None = None) -> list[dict[str, Any]]:
        """[{name, description, roles, path}]，按名字排序，最多 20 个。

        role 给了就只要 roles 含这个角色的（主模型 role="main"、子 agent role="worker"）；
        不给（None）返回全部——网页 / 调试要看全量。
        """
        out: list[dict[str, Any]] = []
        for name, path in self._skill_dirs():
            text = self._read_head(path / "SKILL.md")
            if text is None:
                continue
            front = parse_front_matter(text)
            description = str(front.get("description") or "")
            roles = _normalize_roles(front.get("roles"))
            if role is not None and role not in roles:
                continue
            out.append(
                {
                    "name": name,
                    "description": description,
                    "roles": roles,
                    "path": str(path),
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
        """某个 skill 的 roles（没这个 skill / 读不到 → None）；坏值回落 worker。"""
        path = self._skill_path(name)
        if path is None:
            return None
        text = self._read_head(path / "SKILL.md")
        if text is None:
            return None
        return _normalize_roles(parse_front_matter(text).get("roles"))

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read(self, name: str) -> str | None:
        """SKILL.md 全文（≤40KB）；不存在 / 名字不合法 / 符号链接 → None。"""
        path = self._skill_path(name)
        if path is None:
            return None
        return self._read_head(path / "SKILL.md", full=True)

    def read_file(self, name: str, rel: str) -> str | None:
        """skill 目录内的附属文件（≤40KB）；越界 / 绝对路径 / 符号链接 → None。"""
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

    def _skill_dirs(self) -> list[tuple[str, Path]]:
        """(名字, 目录) 列表：只看普通目录（符号链接不算），名字按目录名。"""
        try:
            entries = sorted(self._root.iterdir(), key=lambda p: p.name)
        except (OSError, FileNotFoundError):
            return []
        out: list[tuple[str, Path]] = []
        for p in entries:
            try:
                if not p.is_dir() or p.is_symlink():
                    continue
            except OSError:
                continue
            out.append((p.name, p))
        return out

    def _skill_path(self, name: str) -> Path | None:
        """名字 → skill 目录；符号链接 / 不是目录 → None。"""
        n = str(name or "").strip()
        if not n or "/" in n or "\\" in n or n in (".", ".."):
            return None
        p = self._root / n
        try:
            if p.is_symlink() or not p.is_dir():
                return None
        except OSError:
            return None
        return p

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
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if key not in _FRONT_KEYS:
            continue  # 只认三个键
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


def _normalize_roles(raw: Any) -> list[str]:
    """front matter 的 roles → ["worker"] / ["main"] / 两者；坏值回落 worker。"""
    items: list[str] = []
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    elif isinstance(raw, str):
        items = _parse_roles_value(raw)
    roles = [r for r in items if r in ("worker", "main")]
    return roles or ["worker"]
