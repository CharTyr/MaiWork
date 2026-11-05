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
const gface = (g) => `<span class="gav">${g.id}</span>`, safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "#"), when = (ts) => (ts ? "T" + ts : "");
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


def test_work_journal_is_capsule_tabs_and_direct_panel():
    _run(_source(), r'''
const html = agentsPage();
assert.match(html, /class="h-sub-row journal-top"/);
assert.match(html, /class="journal-group-badge"/, "单群显示静态胶囊");
assert.doesNotMatch(html, /<select id="agent-group"/);
assert.match(html, /class="journal-tabs" role="tablist"/);
assert.match(html, /data-act="agent-journal-tab" data-kind="news" aria-selected="true"/);
assert.match(html, /data-kind="task" aria-selected="false"/);
assert.match(html, /class="set-group agent-journal" data-kind="news"/);
assert.match(html, /近期完成 <b>1<\/b>/);
assert.match(html, /最近交接 <b>1<\/b>/);
assert.match(html, /class="seg journal-seg"/);
assert.match(html, /class="journal-list"/);
assert.match(html, /handoff-entry/);
assert.doesNotMatch(html, /journal-row|用于避免重复工作|交给我的/);
assert.doesNotMatch(html, /undefined/);
''')


def test_multi_group_selector_and_task_empty_state():
    _run(_source(), r'''
state.groups = [{id: "g1", name: "示例群"}, {id: "g2", name: "二群"}];
state.agentJournalKind = "task";
const html = agentsPage();
assert.match(html, /class="journal-group-pick"/);
assert.match(html, /<select id="agent-group" aria-label="选择服务群">/);
assert.match(html, /class="set-group agent-journal" data-kind="task"/);
assert.match(html, /暂无工作记录/);
assert.doesNotMatch(html, /journal-seg|近期完成 <b>/);
state.groups = [];
assert.match(agentsPage(), /暂无服务群/);
''')


def test_handoff_entry_never_leaks_raw_json():
    _run(_source(), r'''
const h = {id: "H-9", status: "returned", phase: "verify", task_id: "T-1", created: 100, criteria: [],
  review: JSON.stringify({accepted: true, summary: "核实通过", refs: ["https://a.example/x", "doc-1"], used_tools: ["web_search"]}),
  tools: ["web_search"], brief: "核对来源"};
const out = handoffEntry(h);
assert.match(out, /jh-status warn/);
assert.match(out, /dot reviewing/);
assert.match(out, /检查意见/);
assert.match(out, /核实通过/);
assert.doesNotMatch(out, /[{}\[\]]|检查标准|undefined/);
assert.equal(out.match(/>web_search</g).length, 1, "工具去重");
assert.match(out, /href="https:\/\/a.example\/x"/);
assert.match(out, /jh-more/);
assert.match(out, /未提交结果摘要/);
const c = handoffEntry({status: "running", criteria: [" 有来源 ", ""], review: '{"used_tools":["x"]}'});
assert.match(c, /<ul class="jh-criteria"><li>有来源<\/li><\/ul>/);
assert.match(c, /正在执行，尚未提交结果/);
assert.doesNotMatch(c, /used_tools/);
assert.match(handoffEntry({status: "failed", error: "超时"}), /jh-summary bad-t">超时/);
const l = learnedEntry({text: "经验", updated: 5, source_id: "S1", refs: ["S1", "https://b.example"]});
assert.match(l, /learned-entry/);
assert.equal(l.match(/S1/g).length, 1);
''')


def test_picker_is_only_wired_into_the_all_settings_page():
    index_src = (JS / "settings/index.js").read_text()
    rules_src = (JS / "settings/rules.js").read_text()
    assert "sectionPicker" not in index_src, "设置各分栏不再用竖向圆按钮"
    assert "sectionPicker" in rules_src, "「全部设置」保留竖向分类菜单"


def test_work_journal_css_invariants():
    import re
    css = (JS.parent / "style.css").read_text(encoding="utf-8")
    assert ".journal-who" in css
    assert ".journal-head-main" not in css
    assert ".set-group.agent-journal" in css
    assert ".agent-journal:last-child" not in css
    assert ".journal-row" not in css
    assert ".jh-refs" in css
    assert ".jl-meta .jh-time" in css
    for m in re.finditer(r"[^{}]*\.journal-tab-cnt[^{}]*\{([^}]*)\}", css):
        assert "255,255,255" not in m.group(1).replace(" ", "")
