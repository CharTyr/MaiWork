"""ZIP exports and administrator suggestions: roundtrip, isolation and masking."""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import skills_web
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "test.db")
    result.migrate()
    yield result
    result.close()


def _skill(root, name, files=None):
    target = root / name
    target.mkdir(parents=True)
    md = f"---\nname: {name}\ndescription: Export test\nroles: worker\n---\n\nOriginal body.\n".encode()
    expected = {"SKILL.md": md, **(files or {})}
    for rel, blob in expected.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    return target, expected


@pytest.mark.parametrize("builtin", [False, True])
def test_export_all_regular_files_and_roundtrip(tmp_path, store, monkeypatch, builtin):
    root = tmp_path / ("builtin" if builtin else "skills")
    monkeypatch.setattr(skills_web, "BUILTIN_ROOT", tmp_path / "builtin")
    target, expected = _skill(root, "export-demo", {
        **{f"refs/file-{i}.bin": bytes([i]) for i in range(60)},
        "scripts/run.py": b"print('hello')\r\n", "._metadata": b"regular file",
    })
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"private")
    (target / "linked-file").symlink_to(outside / "private.txt")
    (target / "linked-dir").symlink_to(outside, target_is_directory=True)
    if hasattr(os, "mkfifo"):
        os.mkfifo(target / "pipe")
    blob = skills_web.export_zip(tmp_path, store, "export-demo")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entries = {info.filename.split("/", 1)[1] if info.filename.startswith("export-demo/") else info.filename:
                   archive.read(info) for info in archive.infolist() if not info.is_dir()}
    assert entries == expected
    # Builtin packages can be reinstalled under a different declared name.
    if builtin:
        entries["SKILL.md"] = entries["SKILL.md"].replace(b"name: export-demo", b"name: imported-demo")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        blob = output.getvalue()
    view = skills_web.install_zip(store, tmp_path, blob, filename="export-demo.zip", replace=True)
    installed = tmp_path / "skills" / view["name"]
    assert {rel: (installed / rel).read_bytes() for rel in entries} == entries


def test_export_rejects_bad_missing_and_symlink_skills(tmp_path, store, monkeypatch):
    monkeypatch.setattr(skills_web, "BUILTIN_ROOT", tmp_path / "builtin")
    for name in ("../escape", "bad/name", ""):
        with pytest.raises(ValueError):
            skills_web.export_zip(tmp_path, store, name)
    with pytest.raises(KeyError):
        skills_web.export_zip(tmp_path, store, "missing")
    target, _ = _skill(tmp_path / "outside", "secret")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(KeyError):
        skills_web.export_zip(tmp_path, store, "linked")
    local, _ = _skill(tmp_path / "skills", "linked-md")
    (local / "SKILL.md").unlink()
    (local / "SKILL.md").symlink_to(target / "SKILL.md")
    with pytest.raises(KeyError):
        skills_web.export_zip(tmp_path, store, "linked-md")


def _svc(tmp_path, store, rows):
    settings, problems = load_settings({
        "groups": {"serve": [{"group": "qq:101", "workspace": "a"},
                              {"group": "qq:202", "workspace": "b"}]},
        "storage": {"data_dir": str(tmp_path)},
    })
    calls = []

    def skills(gid, kind, *, include_archived):
        calls.append((gid, kind, include_archived))
        return [row for row in rows.get(gid, []) if row["status"] == "active"]

    return SimpleNamespace(
        agents=SimpleNamespace(skills=skills), store=store,
        get_settings=lambda: settings, known_secrets=lambda: ["private-example-value"],
    ), calls


def _row(id, name, uses=1, updated=1, **extra):
    return {"id": id, "name": name, "uses": uses, "updated": updated,
            "description": "Description", "body": "Body", "origin": "explore",
            "status": "active", **extra}


def test_suggestions_served_active_task_sorted_excluded_and_masked(tmp_path, store):
    _skill(tmp_path / "skills", "Global-Skill")
    svc, calls = _svc(tmp_path, store, {
        "101": [_row(1, "global-skill", 100), _row(2, "older", 5, 10),
                _row(3, "archived", 100, status="archived"),
                _row(4, "private-example-value", 5, 20,
                     description="private-example-value", body="private-example-value")],
        "202": [_row(5, "frequent", 9)],
        "999": [_row(6, "unserved", 100)],
    })
    result = skills_web.list_suggestions(svc)
    assert [row["id"] for row in result] == [5, 4, 2]
    assert calls == [("101", "task", False), ("202", "task", False)]
    assert set(result[0]) == {"group_id", "group_name", "id", "name", "description", "body", "origin", "uses"}
    assert result[0]["group_id"] == "202"
    assert "private-example-value" not in repr(result)
    assert len(skills_web.list_suggestions(svc, {"FREQUENT", "OLDER"})) == 2


def test_suggestions_cap_twelve_and_requested_limit(tmp_path, store):
    svc, _ = _svc(tmp_path, store, {
        "101": [_row(i, f"skill-{i}", i) for i in range(10)],
        "202": [_row(i, f"skill-{i}", i) for i in range(10, 20)],
    })
    assert [row["id"] for row in skills_web.list_suggestions(svc)] == list(range(19, 7, -1))
    assert len(skills_web.list_suggestions(svc, limit=100)) == 12
    assert len(skills_web.list_suggestions(svc, limit=2)) == 2
    assert skills_web.list_suggestions(svc, limit=0) == []
    svc.agents = None
    assert skills_web.list_suggestions(svc) == []
