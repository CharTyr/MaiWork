"""专岗页布局回归：头像在最顶部，工作记录用列表而不是卡片墙。"""
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"


def _run(source: str, body: str):
    setup = r'''
import assert from "node:assert/strict";
const state = {
  agents: {profiles: [
    {kind: "main", title: "主模型", model: "m1"},
    {kind: "news", title: "资讯", model: "m1"},
    {kind: "task", title: "通用任务", model: "m1"},
  ]},
  mdl: {models: [{id: "m1", name: "模型一", endpoint: "ep1"}], endpoints: [{id: "ep1", name: "端点"}]},
  groups: [{id: "g1", name: "示例群"}],
  agentGroup: "g1",
  agentSnapshot: {group_id: "g1", agents: [
    {kind: "news", title: "资讯", learned: [{text: "经验一"}], recent_handoffs: [{id: "H-1", status: "accepted", summary: "已交付"}]},
    {kind: "task", title: "通用任务", learned: [], recent_handoffs: []},
  ]},
  agentNew: false, agentEdit: null, agentFishEdit: null, agentError: "", agentFishNew: null,
};
const esc = s => String(s ?? ""), tip = () => "", toast = () => "", SVG = {}, $ = () => null;
const loading = () => "加载中", avatarBlock = () => `<h2 class="h-sub">头像</h2><div class="av-row">头像块</div>`;
const agentFish = () => "<svg></svg>", agentSeed = () => "", fishSvg = () => "<svg></svg>";
const fishTraits = () => ({}), parseCustom = () => null, customSeed = () => "";
const FISH_SPECIES = [], FISH_EYES = [], FISH_COLORS = [], FISH_COLOR_NAMES = {}, FISH_BASE_RE = /x/;
const api = async () => ({}), repaintSheet = () => {}, loadModels = async () => {}, EFFORT_NAMES = {};
const document = {addEventListener: () => {}, querySelector: () => null};
'''
    result = subprocess.run(["node", "--input-type=module", "-e", setup + source + "\n" + body],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def _source() -> str:
    src = (JS / "settings/agents.js").read_text()
    return "\n".join(line for line in src.splitlines() if not line.startswith("import ")).replace("export ", "")


def test_avatar_sits_above_the_specialist_list():
    _run(_source(), r'''
const html = agentsPage();
const avatar = html.indexOf("头像");
const list = html.indexOf("理解群聊、分派任务和检查结果");
assert.ok(avatar > -1 && list > -1);
assert.ok(avatar < list, "头像块应在专岗列表之前");
assert.equal(html.match(/头像块/g).length, 1, "头像只出现一次，不挂在主模型行下");
''')


def test_work_journal_is_a_list_not_a_card_wall():
    _run(_source(), r'''
const html = agentsPage();
assert.match(html, /class="agent-journal"/);
assert.match(html, /class="journal-head"/);
assert.match(html, /class="journal-meta"/);
assert.match(html, /class="journal-row"/);
assert.doesNotMatch(html, /journal-stats|journal-details/);
assert.match(html, /近期完成 <b>1<\/b>/);
''')


def test_picker_is_only_wired_into_the_all_settings_page():
    index_src = (JS / "settings/index.js").read_text()
    rules_src = (JS / "settings/rules.js").read_text()
    assert "sectionPicker" not in index_src, "设置各分栏不再用竖向圆按钮"
    assert "sectionPicker" in rules_src, "「全部设置」保留竖向分类菜单"
