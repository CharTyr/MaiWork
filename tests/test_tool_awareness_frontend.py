"""网页侧：skill 的「要用的工具」与专岗页的工具展示（2026-10-30 调研岗做法工具）。

真跑一遍 JS（node，去掉 import 后注入桩）渲染 profileForm / skillForm / skillRow，
再补几条静态检查（保存时带 allowed_tools、遇到「执行类工具」400 时确认后带 confirm_exec 重试）。
找不到 node 直接失败（不许 skip 造成假绿）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"


def _strip(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return "\n".join(line for line in src.splitlines() if not line.startswith("import ")).replace("export ", "")


def _run(source: str, state: dict, body: str) -> None:
    setup = (
        'import assert from "node:assert/strict";\n'
        f"const state = {json.dumps(state, ensure_ascii=False)};\n"
        r"""
const esc = (s) => String(s ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const tip = (h) => (h ? `<details class="tip">${h}</details>` : "");
const tipLabel = (f, t, h) => `<label for="${f}">${t}</label>` + tip(h);
const toast = () => {}, SVG = { trash: "" }, $ = () => null, ico = () => "", dayWord = () => "今天";
const loading = () => "加载中", avatarBlock = () => "", loadOptDeps = async () => {}, optDepsSection = () => "";
const agentFish = () => "<svg></svg>", agentSeed = () => "", fishSvg = () => "<svg></svg>";
const fishTraits = () => ({}), parseCustom = () => null, customSeed = () => "";
const FISH_SPECIES = {}, FISH_EYES = {}, FISH_COLORS = [], FISH_COLOR_NAMES = [], FISH_BASE_RE = /x/;
const api = async () => ({}), repaintSheet = () => {}, loadModels = async () => {}, EFFORT_NAMES = {};
const document = { addEventListener: () => {}, querySelector: () => null, querySelectorAll: () => [] };
"""
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", setup + source + "\n" + body],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


NEWS_VIEW = {
    "mode": "fixed_plus_skills",
    "factory": ["web_search", "fetch_page", "submit_result"],
    "skills": [{
        "skill": "x-news",
        "tools": [
            {"name": "mcp_flovia_flovia_x", "registered": True, "exec_class": False, "task_only": False},
            {"name": "run_command", "registered": True, "exec_class": True, "task_only": False},
            {"name": "ghost_tool", "registered": False, "exec_class": False, "task_only": False},
        ],
        "unknown": ["ghost_tool"],
    }],
    "phases": {"verify": ["fetch_page", "mcp_flovia_flovia_x"], "supplement": ["mcp_flovia_flovia_x"]},
}


def _agents_state(view: dict, kind: str = "news") -> dict:
    return {
        "agents": {"profiles": [
            {"kind": "main", "title": "主模型", "model": "m1"},
            {"kind": kind, "title": "资讯" if kind == "news" else "通用任务", "model": "m1",
             "skills": ["x-news"], "tools_view": view},
        ]},
        "agentSkills": [
            {"name": "x-news", "enabled": True, "allowed_tools": ["mcp_flovia_flovia_x"]},
            {"name": "plain", "enabled": True, "allowed_tools": []},
        ],
        "agentDocs": {kind: {"soul": {"text": ""}, "agents": {"text": ""}, "limits": {}}},
        "mdl": {"models": [{"id": "m1", "name": "模型一", "endpoint": "ep1"}], "endpoints": [{"id": "ep1", "name": "端点"}]},
        "groups": [], "agentNew": False, "agentEdit": kind, "agentFishEdit": None, "agentError": "",
    }


def test_research_profile_shows_factory_skill_and_phase_tools():
    _run(_strip(JS / "settings/agents.js"), _agents_state(NEWS_VIEW), r"""
const html = profileForm(state.agents.profiles[1]);
assert.match(html, /class="agent-tools"/);
assert.match(html, /出厂[\s\S]*web_search/);
assert.match(html, /x-news[\s\S]*mcp_flovia_flovia_x/);
assert.match(html, /run_command · 执行类/);
assert.match(html, /ghost_tool · 未注册/);
assert.match(html, /核对[\s\S]*fetch_page/);
assert.match(html, /补找[\s\S]*mcp_flovia_flovia_x/);
assert.match(html, /带工具：mcp_flovia_flovia_x/, "可用 skill 列表里标出该 skill 带来的工具");
assert.doesNotMatch(html, /plain<span class="fine">（带工具/, "没声明工具的 skill 不标");
""")


def test_task_profile_says_tools_chosen_per_dispatch():
    view = {"mode": "per_job", "candidates": ["run_command", "mcp_flovia_flovia_x"], "note": "x"}
    _run(_strip(JS / "settings/agents.js"), _agents_state(view, kind="task"), r"""
const html = profileForm(state.agents.profiles[1]);
assert.match(html, /每次分派时选择/);
assert.doesNotMatch(html, /class="agent-tools"/);
""")


def _ext_state(data: dict, choices) -> dict:
    return {
        "ext": {"mcp": [], "skills": [{"name": "x-news", "description": "X 找资讯", "roles": ["worker"],
                                       "enabled": True, "allowed_tools": ["mcp_flovia_flovia_x"]}]},
        "extEdit": {"kind": "skill", "name": "x-news", "data": data},
        "toolChoices": choices,
    }


CHOICES = [
    {"name": "run_command", "label": "在工作区跑命令", "exec_class": True, "task_only": False, "needs_workspace": True},
    {"name": "vm_run", "label": "在云端机器跑命令", "exec_class": True, "task_only": True, "needs_workspace": False},
    {"name": "mcp_flovia_flovia_x", "label": "X 帖子", "exec_class": False, "task_only": False, "needs_workspace": False},
]


def test_skill_form_lists_tool_choices_with_marks():
    data = {"name": "x-news", "description": "X 找资讯", "roles": ["worker"], "body": "正文",
            "allowed_tools": ["mcp_flovia_flovia_x", "ghost_tool"]}
    _run(_strip(JS / "settings/ext.js"), _ext_state(data, CHOICES), r"""
const html = skillForm(state.ext.skills[0]);
assert.match(html, /要用的工具/);
assert.match(html, /id="k-tools"/);
assert.match(html, /value="mcp_flovia_flovia_x" checked/);
assert.match(html, /value="run_command" \/>/, "没声明的不勾");
assert.match(html, /run_command[\s\S]*?执行类/);
assert.match(html, /vm_run[\s\S]*?仅派活时/);
assert.match(html, /value="ghost_tool" checked[\s\S]*?未注册/, "声明了但没注册的照样列出并标明");
""")


def test_skill_form_without_choices_keeps_declared_tools_unmarked():
    data = {"name": "x-news", "description": "", "roles": ["worker"], "body": "正文",
            "allowed_tools": ["mcp_flovia_flovia_x"]}
    _run(_strip(JS / "settings/ext.js"), _ext_state(data, None), r"""
const html = skillForm(state.ext.skills[0]);
assert.match(html, /value="mcp_flovia_flovia_x" checked/, "工具清单没加载到时也保留已声明的");
assert.doesNotMatch(html, /未注册/, "清单没加载到时不能断言「未注册」");
""")


def test_skill_row_shows_declared_tools():
    _run(_strip(JS / "settings/ext.js"), _ext_state({}, CHOICES), r"""
const html = skillRow(state.ext.skills[0]);
assert.match(html, /class="ext-tools"[\s\S]*mcp_flovia_flovia_x/);
""")


def test_saves_send_allowed_tools_and_confirm_exec_retry():
    main = (JS / "main.js").read_text(encoding="utf-8")
    events = (JS / "events.js").read_text(encoding="utf-8")
    agents = (JS / "settings/agents.js").read_text(encoding="utf-8")
    ext = (JS / "settings/ext.js").read_text(encoding="utf-8")
    assert "export const execConfirmNeeded" in ext and "export function confirmExecTools" in ext
    assert "allowed_tools" in main and "confirm_exec: true" in main, "skill 表单保存带工具，执行类确认后重试"
    assert "confirm_exec=1" in events, "上传 zip 遇到执行类工具，确认后带 confirm_exec=1 重传"
    assert "confirm_exec: true" in agents, "专岗挂带执行类工具的 skill，确认后重试"
    assert "worker_tool_choices" in agents and "toolChoices" in ext
