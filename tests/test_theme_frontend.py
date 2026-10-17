"""网页主题与按钮音效（前端）：读源码 + 用 node 跑纯函数，不开浏览器。

规格见 .trellis/tasks/10-16-web-theme-sound/design.md §5：
- 默认主题不设 data-theme（外观与改动前一致）；复古终端设 data-theme="retro"；
- 首屏防闪：<head> 里在样式表之前按 localStorage 立刻设好主题，不发请求；
- 个人覆盖（localStorage "mw-theme"，"" = 跟随默认）优先于全站默认；
- 音效只响应真实用户操作、悬停只给鼠标、可静音、减少动态效果时默认静音；
- 所有身份都能打开「外观」，总管理员设置里有「外观」分节。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static"
JS = STATIC / "js"
if not STATIC.is_dir():  # 找不到插件目录直接报错，不许 skip 造成假绿
    raise RuntimeError(f"找不到前端目录：{STATIC}")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_head_boot_script_sets_theme_before_css() -> None:
    html = _read(STATIC / "index.html")
    head = html.split("</head>")[0]
    m = re.search(r"<script>(.*?)</script>", head, re.S)
    assert m, "head 里要有内联防闪脚本"
    boot = m.group(1)
    assert head.index("<script>") < head.index('rel="stylesheet"'), "防闪脚本要在样式表之前"
    assert "mw-theme" in boot and "mw-theme-default" in boot
    assert "data-theme" in boot or "dataset.theme" in boot
    assert "fetch" not in boot and "XMLHttpRequest" not in boot
    # 入口仍只有 main.js 一个模块
    assert html.count('type="module"') == 1


def test_retro_theme_css_block() -> None:
    css = _read(STATIC / "style.css")
    assert ':root[data-theme="retro"]' in css
    block = css.split(':root[data-theme="retro"]', 1)[1].split("}", 1)[0]
    for var in ("--bg", "--card", "--ink", "--line", "--fill", "--green"):
        assert var in block, f"复古终端要覆盖 {var}"
    assert "color-scheme: light" in block, "复古终端只有浅色"
    # 默认主题的 :root 没被改成依赖 data-theme
    assert css.lstrip().startswith("/*") and "\n:root {" in css


def _node():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("没有 node，无法跑前端纯函数检查")
    return node


_RESOLVE = """
const m = await import(%s);
const r = (p, d) => m.resolveTheme(p, d);
console.log(JSON.stringify([
  r("", "retro"), r(null, "retro"), r("default", "retro"), r("retro", "default"),
  r("bogus", "retro"), r("", "bogus"), r("", ""),
]));
"""


def test_resolve_theme_order(tmp_path) -> None:
    script = tmp_path / "resolve.mjs"
    script.write_text(_RESOLVE % json.dumps((JS / "theme_core.js").as_uri()), encoding="utf-8")
    res = subprocess.run([_node(), str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == ["retro", "retro", "default", "retro", "retro", "default", "default"]


def test_apply_theme_default_removes_attribute() -> None:
    src = _read(JS / "theme.js")
    assert 'removeAttribute("data-theme")' in src
    assert '"/api/theme"' in src
    assert "DEFAULT_KEY" in src and '"mw-theme-default"' in _read(JS / "theme_core.js")


def test_sound_module_contract() -> None:
    src = _read(JS / "sound.js")
    assert "isTrusted" in src, "只响应真实用户操作"
    assert "(hover: hover) and (pointer: fine)" in src, "悬停音效只给鼠标"
    assert re.search(r'pointerType [!=]== "mouse"', src)
    assert "SOUND_KEY" in src and '"mw-sound"' in _read(JS / "theme_core.js")
    assert "prefers-reduced-motion" in src
    assert "AudioContext" in src and "decodeAudioData" in src
    # 加载模块时不发请求、不建 AudioContext：fetch 只在函数里
    top = [ln for ln in src.splitlines() if ln and not ln.startswith((" ", "\t", "}", "//", "/*", "*"))]
    assert not any("fetch(" in ln or "new AudioContext" in ln or "new (window.AudioContext" in ln for ln in top)
    for kind in ("hover", "click", "open", "close", "success", "error"):
        assert f'"{kind}"' in src


def test_sound_hooks_in_sheet_and_toast() -> None:
    sheet = _read(JS / "sheet.js")
    assert 'sfx("open")' in sheet and 'sfx("close")' in sheet
    util = _read(JS / "util.js")
    assert 'sfx(bad ? "error" : "success")' in util


def test_appearance_entry_for_every_role() -> None:
    render = _read(JS / "render.js")
    foot = render.split('<div class="rail-foot">', 1)[1].split("</div>`", 1)[0]
    # 外观按钮在 admin() 三元之外：所有身份都有
    first = foot.strip().splitlines()[0]
    assert 'data-act="appearance"' in first
    top = render.split("export function renderTop", 1)[1].split("export function", 1)[0]
    assert 'data-act="appearance"' in top
    sheet = _read(JS / "sheet.js")
    assert 'kind === "appearance"' in sheet


def test_settings_has_appearance_section() -> None:
    idx = _read(JS / "settings" / "index.js")
    assert '["appearance", "外观"' in idx
    assert (JS / "settings" / "appearance.js").is_file()
    app = _read(JS / "settings" / "appearance.js")
    for path in ("/api/sound-packs/upload", "/api/sound-packs/github", "/api/sound-packs", '"/api/theme"'):
        assert path in app
    # default 主题不给绑定音效包
    assert "default" in app


@pytest.mark.parametrize("name", ["theme.js", "theme_core.js", "sound.js", "settings/appearance.js"])
def test_new_modules_imported(name) -> None:
    base = name.split("/")[-1]
    hits = [p for p in JS.rglob("*.js") if p.name != base and re.search(rf"from\s+\"[./]*(?:settings/)?{re.escape(base)}\"", _read(p))]
    assert hits, f"{name} 没有被引用"
