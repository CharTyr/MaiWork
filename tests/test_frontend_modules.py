"""网页前端（console/static/js）的结构检查：不用编译，浏览器直接加载原生 ES 模块。

这里不跑浏览器，只做静态检查，挡住拆文件时最容易出的错：
- import 的文件不存在 / 引了对方没 export 的名字（浏览器里整页白屏）；
- 某个模块没人引用（它注册的事件就不会生效）；
- 单个文件又长回几千行。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "maiwork" / "console" / "static"
JS = STATIC / "js"
MAX_LINES = 900

_IMPORT = re.compile(r'^import\s+(?:\{([^}]*)\}\s+from\s+)?"([^"]+)";', re.M)
_EXPORT = re.compile(r"^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _modules() -> list[Path]:
    if not JS.is_dir():
        raise AssertionError(f"找不到前端模块目录 {JS}")
    return sorted(p for p in JS.rglob("*.js") if not p.name.startswith("._"))


def test_index_loads_only_the_entry_module() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/static/js/main.js"></script>' in html
    assert "app.js" not in html


def test_imports_resolve_and_names_are_exported() -> None:
    mods = _modules()
    exports = {p: set(_EXPORT.findall(p.read_text(encoding="utf-8"))) for p in mods}
    reached: set[Path] = set()
    for p in mods:
        text = p.read_text(encoding="utf-8")
        for names, spec in _IMPORT.findall(text):
            assert spec.startswith("./") or spec.startswith("../"), f"{p.name}: 只用相对路径 import（{spec}）"
            target = (p.parent / spec).resolve()
            assert target.exists(), f"{p.relative_to(JS)} import 的 {spec} 不存在"
            reached.add(target)
            for n in [x.strip() for x in names.split(",") if x.strip()]:
                assert n in exports[target], f"{p.relative_to(JS)} 引了 {spec} 里没 export 的 {n}"
    orphans = [p.relative_to(JS).as_posix() for p in mods if p.name != "main.js" and p.resolve() not in reached]
    assert not orphans, f"这些模块没人 import，不会被加载：{orphans}"


def test_every_module_is_short() -> None:
    long = {}
    for p in _modules():
        n = p.read_text(encoding="utf-8").count("\n")
        if n > MAX_LINES:
            long[p.relative_to(JS).as_posix()] = n
    assert not long, f"这些文件太长了，拆一拆（上限 {MAX_LINES} 行）：{long}"


def test_no_nul_separator_in_frontend() -> None:
    """HTML 属性值里的 NUL（\\u0000）会被浏览器换成 U+FFFD：拿它当 option 值的分隔符，
    读回来就拆不开（线上踩过：联网搜索选了工具却保存报「要给出 mcp 和 tool」，
    抓正文下拉也只剩「不用」）。前端一律不用 NUL 当分隔符。"""
    bad = [p.relative_to(JS).as_posix() for p in _modules() if "\\u0000" in p.read_text(encoding="utf-8")]
    assert not bad, f"这些文件用了 \\u0000 当分隔符：{bad}"


def test_skill_activation_controls():
    ext = (JS / "settings/ext.js").read_text(encoding="utf-8")
    actions = (JS / "actions_news.js").read_text(encoding="utf-8")
    assert 'data-act="skill-toggle"' in ext
    assert 'k.disabled_reason' in ext
    assert 'k.manual_enabled' in ext
    assert 'case "skill-toggle"' in actions
    assert '/toggle`' in actions and '/api/extensions/skills/' in actions


def test_specialist_admin_interface_contract():
    page = JS / "settings/agents.js"
    assert page.is_file(), "专岗管理页未实现"
    text = page.read_text(encoding="utf-8")
    assert "export function agentsPage" in text
    assert "export async function loadAgents" in text
    assert "export async function actAgents" in text
    assert "agent-memory-save" in text
    assert "agent-profile-save" in text
    assert "/api/agents" in text and "/agents/" in text
    assert "state.agentLoadSeq" in text, "切群时必须防止旧请求覆盖当前群"
    assert "gid !== state.agentGroup" in text, "旧群按钮不能保存当前群的提醒"
    assert 'a.kind === "task"' in text, "通用任务也应有只读交接记录"
    router = (JS / "router.js").read_text(encoding="utf-8")
    assert router.count('state.setSub === "agents"') >= 2
