"""前端单测：独立「技能」中心（参考图 1 双列目录 + 群经验推荐 + 小鱼角色筛选）、
阅读级技能详情抽屉（参考图 2 常驻开关 + 克隆/导出 + 专岗挂载 + 正文预览 + 在对话中试用）与专岗页联动。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "maiwork/console/static/js"


def _run_vm(script_body: str, tmp_path: Path) -> dict:
    node = shutil.which("node")
    assert node, "需要 node 运行前端模块测试"
    script = tmp_path / "run_skills_ui.mjs"
    script.write_text(
        r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = %s;
const apiCalls = [];
let apiResponder = async () => ({});
const elements = new Map();
const makeEl = (id) => {
  const el = {
    id, value: '', innerHTML: '', textContent: '', hidden: false, disabled: false, scrollTop: 0,
    style: { setProperty() {}, removeProperty() {} }, dataset: {},
    classList: { _s: new Set(), add(...xs) { xs.forEach((x) => this._s.add(x)); }, remove(...xs) { xs.forEach((x) => this._s.delete(x)); }, toggle() {}, contains(x) { return this._s.has(x); } },
    focus() {}, click() { el._clicked = true; },
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
  };
  elements.set(id, el);
  return el;
};
for (const id of ['view', 'side', 'rail', 'top', 'tabbar', 'sheet', 'scrim', 'toast', 'app', 'skill-q', 'skill-zip', 'chat-input']) {
  makeEl(id);
}

const context = vm.createContext({
  console, URL, URLSearchParams, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, WeakMap, RegExp, Error, Promise,
  Blob: class { constructor(parts) { this.size = Buffer.byteLength(parts.join('')); } },
  MutationObserver: class { observe() {} disconnect() {} },
  ResizeObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} },
  navigator: { clipboard: { writeText: async () => {} } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '#/settings/skills', search: '', origin: 'https://mw.test' },
  history: { replaceState(_s, _t, h) { context.location.hash = h; } },
  window: { innerHeight: 800, innerWidth: 1280, scrollY: 0, scrollTo() {}, addEventListener() {}, matchMedia: () => ({ matches: true, addEventListener() {} }) },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  requestAnimationFrame: () => 1,
  cancelAnimationFrame() {},
  setTimeout: () => 1,
  clearTimeout() {},
  setInterval: () => 1,
  clearInterval() {},
  confirm: () => true,
  fetch: async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
    apiCalls.push({ method: (opts && opts.method) || 'GET', url, body });
    const data = await apiResponder((opts && opts.method) || 'GET', url, body);
    return { ok: true, status: 200, json: async () => data };
  },
  document: {
    hidden: false,
    body: { scrollHeight: 900, classList: makeEl('_body').classList },
    activeElement: null,
    contains: () => true,
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
  },
});

const mods = new Map();
function getMod(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  const mod = new vm.SourceTextModule(src, { context, identifier: file });
  mods.set(file, mod);
  return mod;
}
async function load(file) {
  const mod = getMod(file);
  if (mod.status === 'unlinked') {
    await mod.link((spec, parent) => getMod(path.resolve(path.dirname(parent.identifier), spec)));
  }
  if (mod.status === 'linked') await mod.evaluate();
  return mod;
}

const stMod = await load(path.join(ROOT, 'state.js'));
const idxMod = await load(path.join(ROOT, 'settings/index.js'));
const skMod = await load(path.join(ROOT, 'settings/skills.js'));
const extMod = await load(path.join(ROOT, 'settings/ext.js'));
const agMod = await load(path.join(ROOT, 'settings/agents.js'));
const rtMod = await load(path.join(ROOT, 'router.js'));
const { state } = stMod.namespace;
const idx = idxMod.namespace;
const sk = skMod.namespace;
const ext = extMod.namespace;
const ag = agMod.namespace;
const rt = rtMod.namespace;

%s
"""
        % (json.dumps(str(JS)), script_body),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=25)
    assert res.returncode == 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    lines = [ln for ln in res.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {}


def test_skills_center_directory_recommendations_and_role_filter(tmp_path: Path) -> None:
    out = _run_vm(
        r"""
// 1. SET_TABS.extensions 拆出独立的 skills 与 mcp，且 #/settings/skills 路由正确映射
const extTabs = Array.from(idx.SET_TABS.extensions, (x) => x[0]);
assert.deepEqual(extTabs, ['search', 'skills', 'mcp', 'optdeps']);
context.location.hash = '#/settings/skills';
const parsed = rt.parseHash();
assert.equal(parsed.sub, 'extensions');
assert.equal(parsed.setTab, 'skills');

// 2. 构造 8 个内置技能 + 2 个自建技能 + 1 条群经验推荐
const builtins = Array.from({ length: 8 }, (_, i) => ({
  name: i === 0 ? 'news-digest' : i === 1 ? 'idea-spark' : `builtin-s${i}`,
  description: `内置技能说明 ${i}`,
  roles: i === 0 ? ['worker', 'main'] : ['worker'],
  source: 'builtin',
  enabled: true,
  manual_enabled: true,
  allowed_tools: [],
}));
const customs = [
  { name: 'my-weekly-doc', description: '生成团队周报文档', roles: ['main', 'worker'], source: 'web', enabled: true, manual_enabled: true, allowed_tools: ['write_file'] },
  { name: 'social-radar', description: '抓取社交平台讨论', roles: ['worker'], source: 'file', enabled: false, manual_enabled: false, disabled_reason: '已手动停用', allowed_tools: [] },
];
state.me = { role: 'admin' };
state.page = 'settings';
state.setSub = 'extensions';
state.setTab = { extensions: 'skills' };
state.settings = { groups: [], models: { ready: true }, usage: {} };
state.agents = {
  profiles: [
    { kind: 'main', title: '主模型', model: 'm1' },
    { kind: 'news', title: '资讯', model: 'm1', skills: ['news-digest'] },
    { kind: 'idea', title: '构想', model: 'm1', skills: ['idea-spark'] },
    { kind: 'task', title: '通用执行', model: 'm1', skills: null },
  ],
};
state.ext = {
  mcp: [],
  skills: [...customs, ...builtins],
  skill_suggestions: [
    { group_id: '10001', group_name: '产品研发群', id: 5, name: '发布前回归核对单', description: '上线前逐项核对接口与监控', body: '## 步骤\n1. 检查日志', uses: 4 },
  ],
};

const htmlAll = idx.settingsPage();
assert.match(htmlAll, /placeholder="搜索技能"/, '顶栏应有搜索技能胶囊框');
assert.match(htmlAll, /添加/, '顶栏应有添加下拉菜单');
assert.match(htmlAll, /在对话中生成技能/, '添加下拉应包含在对话中生成技能');
assert.match(htmlAll, /来自各群的经验[\s\S]*发布前回归核对单[\s\S]*设为全局技能/, '顶部应展示来自各群的经验推荐及设为全局技能按钮');
assert.match(htmlAll, /data-act="skills-role"[\s\S]*主模型[\s\S]*资讯/, '应展示带角色小鱼的筛选胶囊');
assert.match(htmlAll, /自建与已安装/, '应包含自建与已安装分组');
assert.match(htmlAll, /系统内置/, '应包含系统内置分组');
assert.match(htmlAll, /展开其余 2 项/, '超过 6 项时默认收拢其余条目');

// 按「资讯」专岗筛选：只显示 news 专岗挂载的 news-digest
state.skillRole = 'news';
const htmlNews = sk.skillsPage();
assert.match(htmlNews, /news-digest/);
assert.doesNotMatch(htmlNews, /idea-spark/);

console.log(JSON.stringify({ ok: true }));
""",
        tmp_path,
    )
    assert out["ok"] is True


def test_skill_detail_drawer_clone_export_bind_try_and_agents_page(tmp_path: Path) -> None:
    out = _run_vm(
        r"""
state.me = { role: 'admin' };
state.page = 'settings';
state.setSub = 'extensions';
state.setTab = { extensions: 'skills' };
state.chats = [{ id: 9, title: '主对话', group_id: '', updated: 1700000000 }];
apiResponder = async (method, url) => {
  if (url.startsWith('/api/chat/9')) return { chat: { id: 9, title: '主对话', group_id: '' }, messages: [], pending: [], running: false, status: {} };
  if (url.startsWith('/api/chat')) return { chats: state.chats, skills: [], refs: [] };
  return {};
};
state.agents = {
  profiles: [
    { kind: 'main', title: '主模型', model: 'm1' },
    { kind: 'news', title: '资讯', model: 'm1', skills: [] },
    { kind: 'task', title: '通用执行', model: 'm1', skills: ['my-weekly-doc'] },
  ],
};
state.agentSkills = [
  { name: 'my-weekly-doc', description: '生成团队周报文档', roles: ['main', 'worker'], enabled: true, allowed_tools: ['write_file'] },
  { name: 'news-digest', description: '整理每日行业资讯摘要', roles: ['worker'], enabled: true, allowed_tools: [] },
];
state.mdl = { models: [{ id: 'm1', name: '模型一', endpoint: 'ep1' }], endpoints: [{ id: 'ep1', name: '主端点' }] };
state.ext = {
  mcp: [],
  skills: [
    { name: 'my-weekly-doc', description: '生成团队周报文档', roles: ['main', 'worker'], source: 'web', enabled: true, manual_enabled: true, allowed_tools: ['write_file'] },
    { name: 'news-digest', description: '整理每日行业资讯摘要', roles: ['worker'], source: 'builtin', enabled: true, manual_enabled: true, allowed_tools: [] },
  ],
  skill_suggestions: [],
};

// 1. 自建技能详情抽屉：常驻开关、修改、导出 zip、挂载专岗、Markdown 渲染预览、删除、在对话中试用
state.extEdit = {
  kind: 'skill',
  name: 'my-weekly-doc',
  mode: 'view',
  data: { name: 'my-weekly-doc', description: '生成团队周报文档', roles: ['main', 'worker'], source: 'web', enabled: true, manual_enabled: true, allowed_tools: ['write_file'], body: '## 报告规范\n- 先给结论\n- **核实数据**', files: ['template.md'] },
};
const customDetail = ext.extFormSheet();
assert.match(customDetail, /class="switch[\s\S]*data-act="skill-switch"/, '详情右上角应有常驻绿色启停开关');
assert.match(customDetail, /data-act="skill-edit-mode"/, '自建技能应提供修改按钮');
assert.match(customDetail, /\/api\/extensions\/skills\/my-weekly-doc\/export/, '应提供导出 zip 下载链接');
assert.match(customDetail, /挂载专岗[\s\S]*data-act="skill-bind-agent"/, '应提供挂载专岗快捷勾选');
assert.match(customDetail, /sk-preview[\s\S]*<b>核实数据<\/b>/, '正文应在预览框内经 Markdown 渲染');
assert.match(customDetail, /data-act="skill-del"/, '自建技能左下角应有删除按钮');
assert.match(customDetail, /data-act="skill-try-chat"[\s\S]*在对话中试用/, '右下角应有在对话中试用主按钮');

// 2. 内置技能详情抽屉：不显示删除，显示「复制并自定义」，点按后预填 -custom 进入新建表单
state.extEdit = {
  kind: 'skill',
  name: 'news-digest',
  mode: 'view',
  data: { name: 'news-digest', description: '整理每日行业资讯摘要', roles: ['worker'], source: 'builtin', enabled: true, manual_enabled: true, allowed_tools: [], body: '按来源核实新闻。', files: [] },
};
const builtinDetail = ext.extFormSheet();
assert.match(builtinDetail, /data-act="skill-clone"[\s\S]*复制并自定义/, '内置技能应提供复制并自定义');
assert.doesNotMatch(builtinDetail, /data-act="skill-del"/, '内置技能不应显示删除按钮');

await sk.actSkills('skill-clone', { dataset: { name: 'news-digest' } });
assert.equal(state.extEdit.name, null, '克隆后切入新建技能态');
assert.equal(state.extEdit.mode, 'edit');
assert.equal(state.extEdit.data.name, 'news-digest-custom');
const cloneForm = ext.extFormSheet();
assert.match(cloneForm, /value="news-digest-custom"/, '新建表单应自动预填 -custom 名称');

// 3. 点击「在对话中试用」：跳到聊天页并自动挂上技能胶囊
await sk.actSkills('skill-try-chat', { dataset: { name: 'my-weekly-doc' } });
assert.equal(state.page, 'chat');
assert.equal(state.chatSkill.name, 'my-weekly-doc');

// 4. 专岗与人格页：列表行展示已启用技能数量，编辑表单展示简介与工具标记
const agPage = ag.agentsPage();
assert.match(agPage, /技能/, '专岗列表行应展示技能数量摘要');
state.agentDocs = { task: { soul: { text: '' }, agents: { text: '' }, limits: {} } };
state.agentEdit = 'task';
const agForm = ag.agentsFormSheet();
assert.match(agForm, /生成团队周报文档/, '专岗编辑抽屉技能行应展示技能简介');
assert.match(agForm, /（带工具：write_file）/, '专岗编辑抽屉技能行应保留带工具契约文案');

console.log(JSON.stringify({ ok: true }));
""",
        tmp_path,
    )
    assert out["ok"] is True
