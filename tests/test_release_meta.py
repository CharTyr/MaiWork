"""上架插件中心（Mai-with-u/plugin-repo）的仓库规范：manifest、LICENSE、.gitignore、换目录名也能跑测试。

规则来源：https://docs.mai-mai.org/plugin/submission 与 plugin-repo 的 validate-issue.yml。
公开仓库的根目录就是这个插件目录（sanitize_push 把 plugin/CharTyr_MaiWork 映射到根）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
REPO = "https://github.com/CharTyr/MaiWork"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_required_fields_and_formats():
    for f in ("manifest_version", "id", "name", "version", "description", "author", "license",
              "urls", "host_application", "sdk", "capabilities", "i18n"):
        assert MANIFEST.get(f), f
    assert MANIFEST["manifest_version"] == 2
    assert re.match(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", MANIFEST["id"])
    assert SEMVER.match(MANIFEST["version"])
    for r in ("host_application", "sdk"):
        assert SEMVER.match(MANIFEST[r]["min_version"]) and SEMVER.match(MANIFEST[r]["max_version"])
    a = MANIFEST["author"]
    assert isinstance(a, dict) and a["name"] and a["url"].startswith("https://")
    assert all(isinstance(c, str) and c.strip() for c in MANIFEST["capabilities"])
    i = MANIFEST["i18n"]
    assert i["default_locale"] in i.get("supported_locales", [i["default_locale"]])
    for legacy in ("homepage_url", "repository_url", "categories", "keywords", "plugin_info",
                   "default_locale", "locales_path"):
        assert legacy not in MANIFEST


def test_version_matches_code():
    from CharTyr_MaiWork.plugin import PLUGIN_ID, PLUGIN_VERSION

    assert MANIFEST["version"] == PLUGIN_VERSION
    assert MANIFEST["id"] == PLUGIN_ID


def test_urls_point_to_public_repo():
    u = MANIFEST["urls"]
    assert u["repository"] == REPO  # 不带 .git、不是个人主页
    for k in ("homepage", "documentation", "issues"):
        assert u[k].startswith(REPO), k


def test_license_file_matches_manifest():
    assert MANIFEST["license"] == "GPL-3.0-or-later"
    text = (PLUGIN_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text and "Version 3, 29 June 2007" in text


def test_gitignore_keeps_runtime_files_out():
    lines = (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
    for need in ("config.toml", "__pycache__/", "data/"):
        assert need in lines, need


def test_suite_runs_under_any_dir_name(tmp_path):
    """用户从插件市场装，目录名不一定是 CharTyr_MaiWork；测试照样能跑。"""
    dst = tmp_path / "MaiWork"
    shutil.copytree(PLUGIN_DIR, dst, ignore=shutil.ignore_patterns("__pycache__", "._*", "config.toml"))
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_clock.py", "-q", "-p", "no:cacheprovider"],
                       cwd=dst, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
