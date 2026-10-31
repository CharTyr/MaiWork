"""Public artifact paths: retain the lexical path until symlink/input checks finish."""
from pathlib import Path
import re


def _no_symlinks_under(anchor: Path, *parts: str) -> Path:
    cur = anchor
    for p in parts:
        if not p or p == ".":
            continue
        cur = cur / p
        if cur.is_symlink():
            raise ValueError("路径里有符号链接")
    return cur


def artifact_base(settings, task: dict) -> Path:
    """Use the task's saved workspace; reject links below trusted workspace_root."""
    tid = str(task.get("id") or "")
    if not re.fullmatch(r"T-\d+", tid):
        raise ValueError("任务号不合法")
    root = getattr(settings, "workspace_root", None)
    if root is None:
        root = getattr(getattr(settings, "environments", None), "workspace_root", None)
    if root is None:
        raise ValueError("工作区未配置")
    workspace = str(task.get("workspace") or "").strip()
    if not workspace:
        workspace = str(settings.workspace_of(str(task.get("group_id") or "")) or "")
    rel = Path(workspace)
    if not workspace or rel.is_absolute() or ".." in rel.parts or any(p.lower() == "input" for p in rel.parts):
        raise ValueError("工作区不合法")
    trusted_root = Path(root).resolve()
    return _no_symlinks_under(trusted_root, *rel.parts, "artifacts", tid)


def artifact_file(base: Path, relative: str) -> Path:
    """Reject every input segment and lexical symlink, then check resolved containment."""
    rel = Path(str(relative).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or any(p.lower() == "input" for p in rel.parts):
        raise ValueError("路径不合法")
    if base.is_symlink():
        raise ValueError("路径里有符号链接")
    candidate = _no_symlinks_under(base, *rel.parts)
    resolved = candidate.resolve()
    parts = resolved.relative_to(base.resolve()).parts
    if not parts or any(p.lower() == "input" for p in parts):
        raise ValueError("路径不合法")
    return resolved


def artifact_entry(base: Path, candidates=()) -> Path | None:
    """Find the actual delivered entry, with safe index/unique-file fallbacks."""
    for value in candidates:
        text = str(value or "").replace("\\", "/")
        prefix = f"artifacts/{base.name}/"
        if text.startswith(prefix):
            text = text[len(prefix):]
        path = Path(text)
        try:
            if path.is_absolute():
                text = path.relative_to(base).as_posix()
            selected = artifact_file(base, text)
            if selected.is_file():
                return selected
        except (ValueError, OSError):
            continue
    try:
        index = artifact_file(base, "index.html")
        if index.is_file():
            return index
        files = []
        for path in sorted(base.iterdir()):
            try:
                selected = artifact_file(base, path.name)
                if selected.is_file():
                    files.append(selected)
            except (ValueError, OSError):
                continue
        pages = [p for p in files if p.suffix.lower() in (".html", ".htm")]
        if len(pages) == 1:
            return pages[0]
        return files[0] if len(files) == 1 else None
    except (ValueError, OSError):
        return None


def stage_public_artifacts(base: Path, destination: Path, entry: Path | None = None) -> int:
    """Copy a publishable snapshot; original inputs and links never leave the workspace."""
    import os
    import shutil
    files = []
    for folder, dirs, names in os.walk(base, followlinks=False):
        parent = Path(folder)
        dirs[:] = [d for d in dirs if d.lower() != "input" and not d.startswith(".") and not (parent / d).is_symlink()]
        for name in names:
            if name.startswith("."):
                continue
            source = parent / name
            try:
                relative = source.relative_to(base)
                checked = artifact_file(base, relative.as_posix())
                if checked.is_file():
                    files.append((checked, relative))
            except (ValueError, OSError):
                continue
    # The original main page must be the site's index.html. `entry` is a path
    # already validated inside `base` (typically from artifact_entry); a nested
    # main page also ships at its own path so its sibling assets still resolve.
    main: Path | None = None
    if entry is not None and entry.suffix.lower() in (".html", ".htm"):
        try:
            if entry.is_file() and base.resolve() in entry.resolve().parents:
                main = entry.resolve()
        except OSError:
            main = None
    if main is None and len(files) == 1 and files[0][0].suffix.lower() in (".html", ".htm"):
        main = files[0][0].resolve()
    for source, relative in files:
        is_main = main is not None and source.resolve() == main
        target_rel = Path("index.html") if is_main else relative
        target = destination / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        # Only a *nested* main also stays reachable at its own relative path so
        # its sibling ./assets still resolve; a root-level main ships once.
        if is_main and len(relative.parts) > 1:
            extra = destination / relative
            extra.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, extra)
    return len(files)
