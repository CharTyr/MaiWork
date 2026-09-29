"""插件根目录布局（2026-09-28：代码收进 maiwork/ 子包，根目录只留宿主要的和给人看的）。

- 根目录只允许这几样：清单、入口 plugin.py、包入口、配置示例、许可证、.gitignore、
  maiwork/ 实现代码、tests/、tests_host/；运行时会出现的 config.toml / __pycache__ /
  苹果 exFAT 的 ._* 文件不算；
- 代码挪了一层，但「插件根目录」「默认数据目录」「config.toml 位置」的含义一个都不能变：
  数据目录仍是 <MaiBot>/data/maiwork（插件在 <MaiBot>/plugins/CharTyr_MaiWork/ 时）。
"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork.maiwork import config as _config
from CharTyr_MaiWork.maiwork import config_file as _config_file

PLUGIN_DIR = Path(__file__).resolve().parents[1]

_ALLOWED = {
    "_manifest.json", "plugin.py", "__init__.py", "config.example.toml", "LICENSE", ".gitignore",
    "maiwork", "tests", "tests_host",
    # 公开仓库根目录就是插件目录，额外带给人看的 README / logo / 截图（docs/images）
    "README.md", "logo.png", "logo.svg", "docs",
    # 代码地图（给开发者 / agent 读的说明文字，2026-09-29 用户同意随插件一起部署和公开）
    "codemap.md",
    # git clone 装的会有 .git（/.github）
    ".git", ".github",
}
_RUNTIME = {"config.toml", "__pycache__", ".pytest_cache", "data"}


def test_root_contains_only_allowed_entries() -> None:
    extra = sorted(
        p.name for p in PLUGIN_DIR.iterdir()
        if p.name not in _ALLOWED and p.name not in _RUNTIME and not p.name.startswith("._")
        and p.name != ".DS_Store"
    )
    assert extra == [], "插件根目录只放入口和清单，代码请放进 maiwork/：" + "、".join(extra)


def test_plugin_dir_still_means_plugin_root() -> None:
    assert _config._PLUGIN_DIR == PLUGIN_DIR
    assert (_config._PLUGIN_DIR / "_manifest.json").is_file()
    assert (_config._PLUGIN_DIR / "plugin.py").is_file()


def test_default_data_dir_unchanged() -> None:
    assert _config._default_data_dir() == PLUGIN_DIR.parent.parent / "data" / "maiwork"


def test_config_toml_path_at_plugin_root() -> None:
    assert _config_file.config_path(_config._PLUGIN_DIR) == PLUGIN_DIR / "config.toml"
