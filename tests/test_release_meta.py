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


# 插件代码里 import 的第三方包（maibot_sdk 是宿主自带，不声明）。
# 声明要让宿主依赖流水线：已装好且满足约束 → 不跑 pip；和宿主约束必须有交集
# （见 docs/06「manifest 依赖声明」一节的实测结果）。
THIRD_PARTY_PACKAGES = ("aiohttp", "httpx", "tomlkit")


def test_python_package_dependencies_declared():
    from packaging.specifiers import SpecifierSet

    deps = MANIFEST["dependencies"]
    assert isinstance(deps, list) and deps, "dependencies 不能是空的：代码 import 了 httpx / tomlkit / aiohttp"
    declared: dict[str, str] = {}
    for dep in deps:
        assert isinstance(dep, dict), dep
        assert dep["type"] == "python_package", dep
        # 宿主是严格模型（extra="forbid"），字段只能这三个
        assert set(dep) == {"type", "name", "version_spec"}, dep
        assert re.fullmatch(r"[A-Za-z0-9._-]+", dep["name"]), dep
        spec = dep["version_spec"]
        assert spec.strip() == spec and spec, dep
        SpecifierSet(spec)  # 不是合法 PEP 440 约束，宿主会报「Python 包依赖声明无效」
        # 宽松：只给下界，别把宿主已经装好的版本卡掉
        assert spec.startswith(">="), dep
        declared[dep["name"].lower().replace("_", "-")] = spec
    for name in THIRD_PARTY_PACKAGES:
        assert name in declared, f"没声明第三方依赖 {name}（宿主会当缺失去装，或运行时 import 失败）"


def test_urls_point_to_public_repo():
    u = MANIFEST["urls"]
    assert u["repository"] == REPO  # 不带 .git、不是个人主页
    for k in ("homepage", "documentation", "issues"):
        assert u[k].startswith(REPO), k


def test_license_file_matches_manifest():
    # 2026-09-28 起改为 AGPL-3.0-or-later（网页控制台属网络服务，改了要给用户源码）
    assert MANIFEST["license"] == "AGPL-3.0-or-later"
    text = (PLUGIN_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text and "Version 3, 19 November 2007" in text


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
