"""设置与群页前端重构验收测试（Trellis 10-31-settings-ui-polish）。

按 Step 1..4 逐块验证：
- Step 1：技能来源元数据 `origin`（explore / auto / admin）、`groupctx.js`「自动探索」徽章、`tasks.js` 筛选空态 +「查看全部」。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from CharTyr_MaiWork.maiwork import skill_explore
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.store import Store

PLUGIN_DIR = Path(__file__).resolve().parents[1]
assert PLUGIN_DIR.is_dir(), f"找不到插件目录：{PLUGIN_DIR}"
STATIC = PLUGIN_DIR / "maiwork/console/static"
JS = STATIC / "js"
assert JS.is_dir(), f"找不到前端目录：{JS}"


def _make_agents(tmp_path: Path, gid: str = "g1") -> Agents:
    store = Store(tmp_path / "maiwork.db")
    store.migrate()
    settings = SimpleNamespace(is_served=lambda g: str(g) == gid, model_list=[])
    return Agents(store, lambda: settings)


def test_skill_origin_distinguishes_explore_auto_and_admin(tmp_path: Path) -> None:
    ag = _make_agents(tmp_path, "g1")
    sid_exp = ag.skill_add(
        "g1", "task", name="csv-preview", description="预览 CSV",
        body="1. 先看表头", source="auto", note=skill_explore._NOTE,
    )
    sid_auto = ag.skill_add(
        "g1", "task", name="clean-table", description="整理表格",
        body="1. 去重", source="auto", note="复盘总结",
    )
    sid_adm = ag.skill_add(
        "g1", "task", name="manual-rule", description="管理员手写",
        body="1. 按模板导出", source="admin", note="",
    )

    by_name = {s["name"]: s for s in ag.skills("g1", "task")}
    assert by_name["csv-preview"]["origin"] == "explore"
    assert by_name["clean-table"]["origin"] == "auto"
    assert by_name["manual-rule"]["origin"] == "admin"

    # 仅锁定不换版本，保留 explore 来源；管理员改正文后来源变为 admin
    locked = ag.skill_update("g1", sid_exp, locked=True)
    assert locked["origin"] == "explore"
    edited = ag.skill_update("g1", sid_exp, body="1. 管理员修订版", source="admin")
    assert edited["origin"] == "admin"
    assert ag.skill_get("g1", sid_auto)["origin"] == "auto"
    assert ag.skill_by_name("g1", "task", "manual-rule")["origin"] == "admin"
    assert sid_adm > 0


def test_groupctx_renders_explore_badge_and_auto_meta(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "ctx_origin.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import vm from 'node:vm';
const FILE = %s;
const slots = [{ innerHTML: '' }];
const state = { g: 'g1', gctx: null, agents: null };
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({
  console, setTimeout: (f) => f(), confirm: () => true,
  document: { querySelectorAll: (sel) => (sel === '.gctx-slot' ? slots : []), querySelector: () => null, addEventListener() {} },
});
const skills = [
  { id: 1, kind: 'task', name: 'csv-preview', description: '预览 CSV', body: '1. 先看表头', locked: false, status: 'active', uses: 2, updated: 1790900000, origin: 'explore' },
  { id: 2, kind: 'task', name: 'clean-table', description: '整理表格', body: '1. 去重', locked: false, status: 'active', uses: 1, updated: 1790900000, origin: 'auto' },
  { id: 3, kind: 'task', name: 'manual-rule', description: '管理员手写', body: '1. 模板', locked: false, status: 'active', uses: 0, updated: 1790900000, origin: 'admin' },
];
const stubs = {
  '../state.js': { state, $: () => null },
  '../util.js': { tip: () => '', esc, toast() {}, SVG: { pen: '[pen]', pin: '[pin]', archive: '[archive]', trash: '[trash]', plus: '[plus]' } },
  '../api.js': { api: async (_m, url) => url.endsWith('/rules') ? { body: '', updated: 0, updated_by: '' } : { skills } },
};
const mods = new Map();
async function link(spec) {
  if (!mods.has(spec)) {
    const exports = stubs[spec];
    const m = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
    }, { context });
    mods.set(spec, m); await m.link(link);
  }
  return mods.get(spec);
}
const mod = new vm.SourceTextModule(fs.readFileSync(FILE, 'utf8'), { context });
await mod.link(link); await mod.evaluate();
mod.namespace.ctxSection('g1');
await new Promise((r) => setImmediate(r));
mod.namespace.repaintCtx();
console.log(JSON.stringify({ html: slots[0].innerHTML }));
"""
        % json.dumps(str(JS / "pages/groupctx.js")),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    html = json.loads(res.stdout.strip().splitlines()[-1])["html"]
    assert '<span class="tag">自动探索</span>' in html
    assert html.count("自动探索") == 1
    assert "自动整理" in html


def test_tasks_empty_filter_shows_reset_button(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "tasks_filter.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const ROOT = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const context = vm.createContext({
  console, URLSearchParams, Date,
  window: { matchMedia: () => ({ matches: false }), scrollTo() {} },
  document: { hidden: true, getElementById: () => null, addEventListener() {} },
  location: { hash: '', search: '' }, history: { replaceState() {} },
  setInterval() {}, setTimeout() {}, clearTimeout() {},
});
const defaults = {
  esc, SVG: { right: '>' }, dur: () => '1天', when: () => '明天', now: () => 100,
  ico: () => '<i></i>', agentFish: () => '<svg></svg>',
  emptyState: (_i, title, body) => `<p>${title}</p><p>${body}</p>`,
  findIdea: () => null, ideaItems: () => [], goalSections: () => '',
};
const mods = new Map();
const real = new Set(['state.js', 'pages/tasks.js']);
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(ROOT, file))) {
    mod = new vm.SourceTextModule(src, { context, identifier: file });
    mods.set(file, mod);
    await mod.link((spec, parent) => load(path.resolve(path.dirname(parent.identifier), spec)));
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm)].map((m) => m[1]))];
    mod = new vm.SyntheticModule(names, function () {
      for (const n of names) this.setExport(n, defaults[n] ?? (() => ''));
    }, { context });
    mods.set(file, mod); await mod.link(() => {});
  }
  return mod;
}
const st = await load(path.join(ROOT, 'state.js')); await st.evaluate();
st.namespace.state.me = { role: 'member', bot: { name: 'MaiBot' } };
st.namespace.state.filter = 'failed';
const tk = await load(path.join(ROOT, 'pages/tasks.js')); await tk.evaluate();
const html = tk.namespace.viewTasks({ id: 'g1' }, {
  tasks: { pending: [], list: [{ id: 'T-1', title: '已完成报表', status: 'completed' }] },
});
console.log(JSON.stringify({ html }));
"""
        % json.dumps(str(JS)),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    html = json.loads(res.stdout.strip().splitlines()[-1])["html"]
    assert "当前筛选下暂无任务" in html
    assert 'data-act="filter" data-f="all"' in html
    assert "查看全部" in html
    assert 'style="margin-top:' not in (JS / "pages/tasks.js").read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Step 2：全局设置 11 → 6 合并、页内 .seg 二级切换、旧路由兼容、群链接按群成组
# ----------------------------------------------------------------------


def test_settings_consolidated_to_six_sections_with_subtabs_and_group_cards(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "set6.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const ROOT = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const context = vm.createContext({
  console, URLSearchParams, Date,
  window: { matchMedia: () => ({ matches: false }), scrollTo() {} },
  document: { hidden: true, getElementById: () => null, addEventListener() {} },
  location: { hash: '', search: '', origin: 'https://mw.test' },
  history: { replaceState() {} },
  setInterval() {}, setTimeout() {}, clearTimeout() {},
});
const stubs = {
  esc, SVG: { check: 'v', right: '>' },
  gface: (g) => `<i class="gav" data-g="${g.id}"></i>`,
  ico: (k) => `<i data-ico="${k}"></i>`,
  tip: () => '', tokens: (n) => String(n || 0),
  api: async () => ({}), gname: (g) => g.name || g.id, mq: (s) => s, platTag: () => '', quiet: () => ({ text: '正常' }),
  loading: () => '<p>加载中</p>',
  extPage: (tab) => `<div data-sec="ext-${tab || 'all'}"></div>`,
  optDepsSection: () => '<div data-sec="optdeps"></div>',
  agentsPage: () => '<div data-sec="agents"></div>',
  jevSection: () => '<div data-sec="jev"></div>',
  rulesPage: () => '<div data-sec="rules"></div>',
  usagePage: () => '<div data-sec="usage"></div>',
  logsPage: () => '<div data-sec="logs"></div>',
  sourcesPage: () => '<div data-sec="sources"></div>',
  memoryPage: () => '<div data-sec="memory"></div>',
  fullLink: (g) => `https://mw.test/#/${g.token}/news`,
  modelsPage: (opts) => `<div data-sec="models-${opts && opts.omitJev ? 'nojev' : 'full'}"></div>`,
  updateSection: () => '', attentionSection: () => '',
  appearancePage: () => '<div data-sec="appearance"></div>',
  socialPage: () => '<div data-sec="social"></div>',
  repaintSheet: () => {},
};
const mods = new Map();
const real = new Set(['state.js', 'settings/index.js']);
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(ROOT, file))) {
    mod = new vm.SourceTextModule(src, { context, identifier: file });
    mods.set(file, mod);
    await mod.link((spec, parent) => load(path.resolve(path.dirname(parent.identifier), spec)));
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm)].map((m) => m[1]))];
    mod = new vm.SyntheticModule(names, function () {
      for (const n of names) this.setExport(n, stubs[n] ?? (() => ''));
    }, { context });
    mods.set(file, mod); await mod.link(() => {});
  }
  return mod;
}
const st = await load(path.join(ROOT, 'state.js')); await st.evaluate();
const state = st.namespace.state;
state.settings = { groups: [{ id: '10001', name: '测试一群', token: 'tk1' }, { id: '10002', name: '测试二群', token: 'tk2' }], models: { ready: true }, usage: {} };
state.ga = { '10001': { password_set: true }, '10002': { password_set: false } };
const idx = await load(path.join(ROOT, 'settings/index.js')); await idx.evaluate();

const pages = {};
for (const [id] of idx.namespace.SET_SUBS) {
  state.setSub = id;
  pages[id] = idx.namespace.settingsPage();
}
// 切换二级分栏
await idx.namespace.actSettingsNav('set-tab', { dataset: { sub: 'models', tab: 'agents' } });
state.setSub = 'models';
const modelsAgents = idx.namespace.settingsPage();

await idx.namespace.actSettingsNav('set-tab', { dataset: { sub: 'models', tab: 'jev' } });
const modelsJev = idx.namespace.settingsPage();

await idx.namespace.actSettingsNav('set-tab', { dataset: { sub: 'extensions', tab: 'mcp' } });
state.setSub = 'extensions';
const extMcp = idx.namespace.settingsPage();

await idx.namespace.actSettingsNav('set-tab', { dataset: { sub: 'sources', tab: 'feeds' } });
state.setSub = 'sources';
const srcFeeds = idx.namespace.settingsPage();

await idx.namespace.actSettingsNav('set-tab', { dataset: { sub: 'usage', tab: 'logs' } });
state.setSub = 'usage';
const usageLogs = idx.namespace.settingsPage();

await idx.namespace.actSettingsNav('set-tab', { dataset: { sub: 'rules', tab: 'config' } });
state.setSub = 'rules';
const rulesCfg = idx.namespace.settingsPage();

console.log(JSON.stringify({ subs: idx.namespace.SET_SUBS, pages, modelsAgents, modelsJev, extMcp, srcFeeds, usageLogs, rulesCfg }));
"""
        % json.dumps(str(JS)),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert [x[0] for x in out["subs"]] == ["overview", "models", "extensions", "sources", "usage", "rules"]
    assert [x[1] for x in out["subs"]] == ["概况", "模型与专岗", "工具与平台", "群与来源", "用量", "系统"]
    assert 'data-act="set-tab"' not in out["pages"]["overview"]
    assert 'data-sec="models-nojev"' in out["pages"]["models"]
    assert 'data-sec="agents"' in out["modelsAgents"]
    assert 'data-sec="jev"' in out["modelsJev"]
    assert 'data-sec="ext-search"' in out["pages"]["extensions"] and 'data-sec="social"' in out["pages"]["extensions"]
    assert 'data-sec="ext-mcp"' in out["extMcp"]
    # 群链接与群管：每群一个 .set-group 容器（2 个群 → 2 个 .set-group）
    links_html = out["pages"]["sources"]
    assert links_html.count('class="set-group"') == 2
    assert 'data-act="ga-edit"' in links_html and 'data-act="reset-link"' in links_html
    assert 'data-sec="sources"' in out["srcFeeds"]
    assert 'data-sec="usage"' in out["pages"]["usage"] and 'data-sec="logs"' not in out["pages"]["usage"]
    assert 'data-sec="logs"' in out["usageLogs"]
    assert 'data-sec="appearance"' in out["pages"]["rules"]
    assert 'data-sec="rules"' in out["rulesCfg"]


def test_legacy_settings_hashes_map_to_consolidated_sections(tmp_path: Path) -> None:
    from test_work_page_frontend import run_js as work_js

    # 让 work_js 里的 SET_SUBS 用真实 6 项分栏
    node = shutil.which("node")
    assert node
    script = tmp_path / "routes.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const ROOT = %s;
const context = vm.createContext({
  console, URLSearchParams, Date,
  window: { matchMedia: () => ({ matches: false }), scrollTo() {} },
  document: { hidden: true, getElementById: () => null, addEventListener() {} },
  location: { hash: '', search: '' }, history: { replaceState() {} },
  setInterval() {}, setTimeout() {}, clearTimeout() {},
});
const mods = new Map();
const real = new Set(['state.js', 'router.js']);
const SET_SUBS = [
  ['overview', '概况'], ['models', '模型与专岗'], ['extensions', '工具与平台'],
  ['sources', '群与来源'], ['usage', '用量'], ['rules', '系统'],
];
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(ROOT, file))) {
    mod = new vm.SourceTextModule(src, { context, identifier: file });
    mods.set(file, mod);
    await mod.link((spec, parent) => load(path.resolve(path.dirname(parent.identifier), spec)));
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm)].map((m) => m[1]))];
    mod = new vm.SyntheticModule(names, function () {
      for (const n of names) this.setExport(n, n === 'SET_SUBS' ? SET_SUBS : () => '');
    }, { context });
    mods.set(file, mod); await mod.link(() => {});
  }
  return mod;
}
const st = await load(path.join(ROOT, 'state.js')); await st.evaluate();
st.namespace.state.me = { role: 'admin', bot: { name: 'MaiBot' } };
const rt = await load(path.join(ROOT, 'router.js')); await rt.evaluate();
const cases = ['agents', 'jev', 'social', 'social-login', 'links', 'sources', 'logs', 'appearance', 'memory', 'rules'];
const out = {};
for (const c of cases) {
  context.location.hash = `#/settings/${c}`;
  rt.namespace.applyHash();
  out[c] = { setSub: st.namespace.state.setSub, setTab: { ...(st.namespace.state.setTab || {}) } };
}
console.log(JSON.stringify(out));
"""
        % json.dumps(str(JS)),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["agents"]["setSub"] == "models" and out["agents"]["setTab"]["models"] == "agents"
    assert out["jev"]["setSub"] == "models" and out["jev"]["setTab"]["models"] == "jev"
    assert out["social"]["setSub"] == "extensions" and out["social"]["setTab"]["extensions"] == "search"
    assert out["social-login"]["setSub"] == "extensions" and out["social-login"]["setTab"]["extensions"] == "search"
    assert out["links"]["setSub"] == "sources" and out["links"]["setTab"]["sources"] == "links"
    assert out["sources"]["setSub"] == "sources" and out["sources"]["setTab"]["sources"] == "feeds"
    assert out["logs"]["setSub"] == "usage" and out["logs"]["setTab"]["usage"] == "logs"
    assert out["appearance"]["setSub"] == "rules" and out["appearance"]["setTab"]["rules"] == "appearance"
    assert out["memory"]["setSub"] == "rules" and out["memory"]["setTab"]["rules"] == "memory"
    assert out["rules"]["setSub"] == "rules" and out["rules"]["setTab"]["rules"] == "config"
    assert work_js is not None


# ----------------------------------------------------------------------
# Step 3：群页管理员「动态与画像 | 本群设置」二级切换 + 群控布尔开关即时保存与回滚
# ----------------------------------------------------------------------


def test_group_page_splits_feed_and_admin_tabs_without_leaking_to_members(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "group_split.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const ROOT = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const context = vm.createContext({
  console, URLSearchParams, Date,
  window: { matchMedia: () => ({ matches: false }), scrollTo() {} },
  document: { hidden: true, getElementById: () => null, addEventListener() {} },
  location: { hash: '', search: '' }, history: { replaceState() {} },
  setInterval() {}, setTimeout() {}, clearTimeout() {},
});
const stubs = {
  esc, SVG: { plus: '+', pen: 'e', lock: 'l', trash: 'd', check: 'v', close: 'x', copy: 'c' },
  dayIndex: () => 1, dayWord: () => '今天', dur: () => '10分', hhmm: () => '12:00',
  ico: () => '<i></i>', now: () => 1000, richText: esc, safeUrl: (u) => u, tip: () => '', when: () => '今天',
  daySummary: () => '', judgeText: () => '可聊', stretches: () => [],
  gname: (g) => g.name || g.id, gplat: () => 'qq', platTag: () => '',
  pulseCard: () => '<div data-sec="pulse"></div>',
  emptyState: (_i, t) => `<div data-sec="empty">${t}</div>`,
  loading: () => '<p>加载中</p>',
  ctxSection: (gid) => `<div data-sec="ctx-${gid}"></div>`,
  controlsSection: (gid) => `<div data-sec="controls-${gid}"></div>`,
  renderView: () => {},
};
const mods = new Map();
const real = new Set(['state.js', 'pages/group.js']);
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(ROOT, file))) {
    mod = new vm.SourceTextModule(src, { context, identifier: file });
    mods.set(file, mod);
    await mod.link((spec, parent) => load(path.resolve(path.dirname(parent.identifier), spec)));
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm)].map((m) => m[1]))];
    mod = new vm.SyntheticModule(names, function () {
      for (const n of names) this.setExport(n, stubs[n] ?? (() => ''));
    }, { context });
    mods.set(file, mod); await mod.link(() => {});
  }
  return mod;
}
const st = await load(path.join(ROOT, 'state.js')); await st.evaluate();
const gp = await load(path.join(ROOT, 'pages/group.js')); await gp.evaluate();
const state = st.namespace.state;
const g = { id: 'g1', name: '测试群', members: 12 };
const v = {
  workspace: '/ws/g1', topic_log: [],
  profile: [{ category: 'recent', name: '近期话题', entries: [{ id: 1, text: '聊机器人' }] }],
  focus: [{ user_id: '42', display_name: '阿鲤', note: '常发言' }],
  commands: { text: '/mw 网页', items: [{ who: '群成员', syntax: '/mw 网页', desc: '获取网页链接' }] },
  group_space: { role: 'admin', files_list: true },
};

state.me = { role: 'member' };
const memberHtml = gp.namespace.viewGroup(g, v);

state.me = { role: 'admin' };
state.groupSub = 'feed';
const adminFeedHtml = gp.namespace.viewGroup(g, v);

gp.namespace.actGroup('group-sub', { dataset: { sub: 'admin' } });
const adminSettingsHtml = gp.namespace.viewGroup(g, v);

console.log(JSON.stringify({ memberHtml, adminFeedHtml, adminSettingsHtml }));
"""
        % json.dumps(str(JS)),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])

    # 普通群成员无二级分栏，且看不到任何管理区块
    assert 'data-act="group-sub"' not in out["memberHtml"]
    assert 'data-sec="controls-g1"' not in out["memberHtml"]
    assert 'data-sec="ctx-g1"' not in out["memberHtml"]
    assert "关注成员" not in out["memberHtml"]

    # 管理员默认「动态与画像」：有话题记录、群画像、关注成员、群指令，不含本群设置块
    assert 'data-act="group-sub"' in out["adminFeedHtml"]
    assert "群画像" in out["adminFeedHtml"] and "关注成员" in out["adminFeedHtml"]
    assert 'data-sec="controls-g1"' not in out["adminFeedHtml"]
    assert 'data-sec="ctx-g1"' not in out["adminFeedHtml"]

    # 管理员切到「本群设置」：包含 controlsSection、ctxSection、群空间，不混入话题与群画像
    assert 'data-sec="controls-g1"' in out["adminSettingsHtml"]
    assert 'data-sec="ctx-g1"' in out["adminSettingsHtml"]
    assert "群空间" in out["adminSettingsHtml"]
    assert "话题记录" not in out["adminSettingsHtml"]


def test_groupcontrols_switches_save_immediately_and_rollback_on_failure(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "gctl_switch.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import vm from 'node:vm';
const FILE = %s;
const calls = [], toasts = [], slots = [{ innerHTML: '', dataset: { g: 'g1' } }];
const state = { g: 'g1', me: { role: 'admin' }, groupControls: null };
let shouldFail = false;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const push = { config: { topics_enabled: true, news_card_enabled: false, news_card_count: 3, idea_mention_enabled: false, daily_max: 3, quiet_hours: '23:00-08:00' }, sent_today: 0, quota_used: 0, recent: [] };
const approval = { approvers: [], exempt_users: [], exempt_group: false, required: true };
const features = { group_id: 'g1', news: true, idea: true, task: true, disabled_ts: {} };
const context = vm.createContext({ console, document: { querySelectorAll: () => slots, addEventListener() {} } });
const stubs = {
  '../state.js': { state, $: () => null, admin: () => true, gadmin: () => true },
  '../util.js': { tip: () => '', esc, when: () => '今天', toast: (m, b) => toasts.push([m, !!b]) },
  '../api.js': {
    api: async (method, url, body) => {
      calls.push({ method, url, body });
      if (shouldFail && method !== 'GET') throw new Error('网络异常');
      if (url.endsWith('/push')) return method === 'GET' ? push : { ...push, config: { ...push.config, ...body } };
      if (url.endsWith('/features')) return method === 'GET' ? features : { ...features, ...body };
      return approval;
    },
  },
  './news.js': { loading: () => '<p>加载中</p>' },
};
const mods = new Map();
async function link(s) {
  if (!mods.has(s)) {
    const ex = stubs[s];
    const m = new vm.SyntheticModule(Object.keys(ex), function () { for (const [k, v] of Object.entries(ex)) this.setExport(k, v); }, { context });
    mods.set(s, m); await m.link(link);
  }
  return mods.get(s);
}
const m = new vm.SourceTextModule(fs.readFileSync(FILE, 'utf8'), { context });
await m.link(link); await m.evaluate();
m.namespace.controlsSection('g1');
await new Promise((r) => setImmediate(r));
calls.length = 0;

// 1. 默认态直接渲染 .switch，无需先点「修改」
const defaultHtml = slots[0].innerHTML;

// 2. 点击关闭 task 功能 → 立即 PUT /features 并常驻显示连带提示
await m.namespace.actControls('gctl-feature-toggle', { dataset: { g: 'g1', key: 'task' }, checked: false });
const afterTaskOffHtml = slots[0].innerHTML;

// 3. 点击开启资讯卡片 → 立即 PUT /push，失败时回滚为原值 false
shouldFail = true;
const toggleEl = { dataset: { g: 'g1', key: 'news_card_enabled' }, checked: true };
await m.namespace.actControls('gctl-push-toggle', toggleEl);
const afterFailHtml = slots[0].innerHTML;

console.log(JSON.stringify({
  defaultHtml, afterTaskOffHtml, afterFailHtml, calls, toasts,
  newsCardCheckedAfterFail: state.groupControls.push.config.news_card_enabled,
  elCheckedAfterFail: toggleEl.checked,
}));
"""
        % json.dumps(str(JS / "pages/groupcontrols.js")),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert 'data-act="gctl-feature-toggle"' in out["defaultHtml"]
    assert 'data-act="gctl-push-toggle"' in out["defaultHtml"]
    assert 'data-act="gctl-features-edit"' not in out["defaultHtml"]
    assert out["calls"][0] == {"method": "PUT", "url": "/api/groups/g1/features", "body": {"news": True, "idea": True, "task": False}}
    assert "目标检查" in out["afterTaskOffHtml"] and "手动恢复" in out["afterTaskOffHtml"]
    assert out["calls"][1]["url"] == "/api/groups/g1/push"
    assert out["newsCardCheckedAfterFail"] is False
    assert out["elCheckedAfterFail"] is False


# ----------------------------------------------------------------------
# Step 4：复杂表单抽屉化（settings-form）、optdeps 保活抽屉、模型就绪度条与 .set-group 容器样式
# ----------------------------------------------------------------------


def test_settings_forms_open_in_sheet_and_clear_on_close(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "sheet_forms.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const ROOT = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const mkClassList = () => ({ add() {}, remove() {}, toggle() {}, contains: () => false });
const dom = {
  sheet: { hidden: true, innerHTML: '', scrollTop: 0, offsetHeight: 480, dataset: {}, style: {}, classList: mkClassList(), setAttribute() {}, focus() {}, addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [] },
  scrim: { hidden: true, style: {}, classList: mkClassList() },
  app: { setAttribute() {}, removeAttribute() {} },
  tabbar: { setAttribute() {}, removeAttribute() {} },
};
const context = vm.createContext({
  console, URLSearchParams, Date, CSS: { escape: (x) => String(x) },
  window: { matchMedia: () => ({ matches: false }), scrollY: 0, scrollTo() {} },
  document: {
    hidden: false, activeElement: null, contains: () => false,
    getElementById: (id) => dom[id] || null,
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
  },
  location: { hash: '', search: '', origin: 'https://mw.test' },
  history: { replaceState() {} },
  requestAnimationFrame: (fn) => fn(),
  setInterval() {}, setTimeout: (fn) => { fn(); return 1; }, clearTimeout() {},
  confirm: () => true,
});
let renderCount = 0;
const stubs = {
  esc, calm: () => true, ico: (k) => `<i data-ico="${k}"></i>`,
  tip: () => '', tipLabel: (f, t) => `<label for="${f}">${t}</label>`,
  toast() {}, when: () => '今天', dayWord: () => '今天', msText: (ms) => `${ms}ms`,
  SVG: { close: 'x', right: '>', trash: 'd' },
  api: async () => ({ soul: { text: '' }, agents: { text: '' }, limits: {} }),
  gname: () => '群', grp: () => ({}), agentFish: () => '<svg></svg>', agentSeed: () => 'abc',
  fishSvg: () => '<svg></svg>', fishTraits: () => ({ species: 'koi', color: '#000', eyes: 'dot' }),
  parseCustom: () => null, customSeed: () => 'c1',
  FISH_SPECIES: { koi: '锦鲤' }, FISH_EYES: { dot: '圆眼' }, FISH_COLORS: ['#000'], FISH_COLOR_NAMES: ['墨黑'], FISH_BASE_RE: /^[a-z0-9]+$/,
  loading: () => '<p>加载中</p>', avatarBlock: () => '<div>头像</div>',
  loadOptDeps: async () => {}, optDepsSection: () => '',
  renderView: () => { renderCount += 1; }, renderSide: () => {}, renderRail: () => {},
  syncHash: () => {}, sfx: () => {},
};
const mods = new Map();
const real = new Set(['state.js', 'sheet.js', 'settings/models.js', 'settings/jev.js', 'settings/ext.js', 'settings/agents.js']);
function getMod(file) {
  if (mods.has(file)) return mods.get(file);
  const src = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(ROOT, file))) {
    mod = new vm.SourceTextModule(src, {
      context, identifier: file,
      importModuleDynamically: async (spec, parent) => {
        const m = getMod(path.resolve(path.dirname(parent.identifier), spec));
        if (m.status === 'unlinked') await m.link((s, p) => getMod(path.resolve(path.dirname(p.identifier), s)));
        if (m.status !== 'evaluated') await m.evaluate();
        return m;
      },
    });
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm)].map((m) => m[1]))];
    mod = new vm.SyntheticModule(names, function () {
      for (const n of names) this.setExport(n, stubs[n] ?? (() => ''));
    }, { context });
  }
  mods.set(file, mod);
  return mod;
}
for (const r of real) getMod(path.join(ROOT, r));
for (const m of mods.values()) if (m.status === 'unlinked') await m.link((spec, parent) => getMod(path.resolve(path.dirname(parent.identifier), spec)));
for (const r of real) { const m = mods.get(path.join(ROOT, r)); if (m.status !== 'evaluated') await m.evaluate(); }

const st = mods.get(path.join(ROOT, 'state.js')).namespace;
const sh = mods.get(path.join(ROOT, 'sheet.js')).namespace;
const md = mods.get(path.join(ROOT, 'settings/models.js')).namespace;
const jv = mods.get(path.join(ROOT, 'settings/jev.js')).namespace;
const ex = mods.get(path.join(ROOT, 'settings/ext.js')).namespace;
const ag = mods.get(path.join(ROOT, 'settings/agents.js')).namespace;

const state = st.state;
state.page = 'settings';
state.settings = { models: { ready: true } };
state.mdl = { endpoints: [{ id: 'e1', name: '端点一', protocol: 'openai', base_url: 'https://a.test/v1', key_set: true }], models: [] };
state.jev = { enabled: true, use: 'typesafe', endpoints: [{ id: 'typesafe', name: 'TypeSafe', preset: 'typesafe', protocol: 'systemone', url: 'https://a.test', model: 'jev-1', key_set: true, builtin: true }], presets: [{ id: 'typesafe', name: 'TypeSafe' }, { id: 'openrouter', name: 'OpenRouter', protocol: 'systemone', url: 'https://b.test', model: 'jev-1' }] };
state.ext = { mcp: [], skills: [] };
state.agents = { profiles: [{ kind: 'main', title: '主模型', model: '' }] };

// 1. 模型页新建端点 → 进 #sheet，背景列表不内联展开
await md.actModels('mdl-ep-new', { dataset: {} });
const epCase = { sheet: state.sheet, sheetHtml: dom.sheet.innerHTML, pageHtml: md.modelsPage({ omitJev: true }) };

// preserveSheet=true 刷新背景但保留抽屉内正在输入的内容
dom.sheet.innerHTML = '<div id="keep-typing">typing</div>';
const beforeRender = renderCount;
sh.repaintSheet({ preserveSheet: true });
const preserveCase = { sheetHtml: dom.sheet.innerHTML, rendered: renderCount > beforeRender, sheet: state.sheet };

sh.closeSheet();
const afterEpClose = { sheet: state.sheet, mdlEdit: state.mdlEdit };

// 2. Jev 选择服务 → 进 #sheet
await jv.actJev('jev-pick', { dataset: {} });
const jevCase = { sheet: state.sheet, sheetHtml: dom.sheet.innerHTML, pageHtml: jv.jevSection() };
sh.closeSheet();

// 3. MCP 新建 → 进 #sheet
await ex.actExt('ext-new', { dataset: { kind: 'mcp' } });
const extCase = { sheet: state.sheet, sheetHtml: dom.sheet.innerHTML, pageHtml: ex.extPage('mcp') };
sh.closeSheet();

// 4. 专岗新建 → 进 #sheet
await ag.actAgents('agent-new', { dataset: {} });
const agentCase = { sheet: state.sheet, sheetHtml: dom.sheet.innerHTML, pageHtml: ag.agentsPage() };
sh.closeSheet();
const afterAgentClose = { sheet: state.sheet, agentNew: state.agentNew, extEdit: state.extEdit, jevPick: state.jevPick };

console.log(JSON.stringify({ epCase, preserveCase, afterEpClose, jevCase, extCase, agentCase, afterAgentClose }));
"""
        % json.dumps(str(JS)),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])

    assert out["epCase"]["sheet"] == "settings-form"
    assert 'id="mdl-ep-form"' in out["epCase"]["sheetHtml"]
    assert 'id="mdl-ep-form"' not in out["epCase"]["pageHtml"]

    assert out["preserveCase"]["sheetHtml"] == '<div id="keep-typing">typing</div>'
    assert out["preserveCase"]["rendered"] is True
    assert out["preserveCase"]["sheet"] == "settings-form"

    assert out["afterEpClose"] == {"sheet": None, "mdlEdit": None}

    assert out["jevCase"]["sheet"] == "settings-form"
    assert 'id="jv-pick"' in out["jevCase"]["sheetHtml"]
    assert 'id="jv-pick"' not in out["jevCase"]["pageHtml"]

    assert out["extCase"]["sheet"] == "settings-form"
    assert 'id="mcp-form"' in out["extCase"]["sheetHtml"]
    assert 'id="mcp-form"' not in out["extCase"]["pageHtml"]

    assert out["agentCase"]["sheet"] == "settings-form"
    assert 'id="agent-new-title"' in out["agentCase"]["sheetHtml"]
    assert 'id="agent-new-title"' not in out["agentCase"]["pageHtml"]

    assert out["afterAgentClose"] == {"sheet": None, "agentNew": False, "extEdit": None, "jevPick": False}


def test_models_readiness_strip_vision_badge_and_optdeps_preserve_sheet(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "缺少 Node，无法运行前端模块测试"
    script = tmp_path / "mdl_ready.mjs"
    script.write_text(
        r"""
import fs from 'node:fs';
import vm from 'node:vm';
const FILE = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const state = {
  settings: { models: { ready: true } },
  agents: { profiles: [{ kind: 'main', title: '主模型', model: 'm-vis' }] },
  mdl: {
    image_model: 'm-img',
    endpoints: [{ id: 'e1', name: '主力端点', protocol: 'openai', base_url: 'https://api.test/v1', key_set: true }],
    models: [
      { id: 'm-vis', endpoint: 'e1', model: 'gpt-4o', name: '视觉主力', use: 'chat', vision: true, efforts: ['medium'], context_window: 128000, max_tokens: 16384 },
      { id: 'm-img', endpoint: 'e1', model: 'gpt-image-1', name: '生图主力', use: 'image', image_api: 'openai', image_default_size: '1024x1024', image_max_n: 1 },
    ],
  },
  mdlEdit: null, mdlVerification: {},
};
const icons = [];
const context = vm.createContext({ console, setTimeout, location: { origin: 'https://mw.test' }, document: { querySelectorAll: () => [], querySelector: () => null, addEventListener() {} } });
const stubs = {
  '../state.js': { state, $: () => null },
  '../util.js': { esc, ico: (k) => { icons.push(k); return `<i data-ico="${k}"></i>`; }, tip: () => '', tipLabel: (f, t) => `<label for="${f}">${t}</label>`, toast() {}, when: () => '今天' },
  '../api.js': { api: async () => ({}) },
  '../sheet.js': { repaintSheet() {} },
  '../pages/news.js': { loading: () => '' },
  './jev.js': { jevSection: () => '', loadJev: async () => {} },
};
const mods = new Map();
async function link(s) {
  if (!mods.has(s)) {
    const ex = stubs[s];
    const m = new vm.SyntheticModule(Object.keys(ex), function () { for (const [k, v] of Object.entries(ex)) this.setExport(k, v); }, { context });
    mods.set(s, m); await m.link(link);
  }
  return mods.get(s);
}
const mod = new vm.SourceTextModule(fs.readFileSync(FILE, 'utf8'), { context });
await mod.link(link); await mod.evaluate();
const html = mod.namespace.modelsPage({ omitJev: true });
console.log(JSON.stringify({ html, icons }));
"""
        % json.dumps(str(JS / "settings/models.js")),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    html = out["html"]

    # 顶部能力就绪度概览条：主模型 / 图片识别 / 图片生成
    assert 'class="cap-strip"' in html
    assert "主模型" in html and "视觉主力" in html
    assert "图片识别" in html and "1 个模型" in html
    assert "图片生成" in html and "生图主力" in html

    # 端点成组容器 + 视觉模型徽章 + 去掉每行重复 robot 图标与内联 margin-top:8px
    assert "set-group" in html
    assert '<span class="tag">支持图片</span>' in html
    assert "robot" not in out["icons"]
    assert "margin-top:8px" not in (JS / "settings/models.js").read_text(encoding="utf-8")

    # optdeps 后台轮询必须带 preserveSheet: true，不覆盖正在编辑的抽屉
    optdeps_src = (JS / "settings/optdeps.js").read_text(encoding="utf-8")
    assert "repaintSheet({ preserveSheet: true })" in optdeps_src

    # CSS 容器与复古主题适配齐全
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".set-group" in css
    assert ':root[data-theme="retro"] .set-group' in css
    assert ".cap-strip" in css


import pytest


@pytest.mark.asyncio
async def test_skills_http_endpoint_preserves_origin(tmp_path: Path) -> None:
    """GET /api/groups/{gid}/skills 必须透传 origin 字段，前端才能渲染「自动探索」徽章。"""
    from aiohttp.test_utils import TestClient, TestServer
    from fakes import FakeCtx, FakeProfiles
    from test_rules_web import PASSWORD, _raw_config, _write_plugin_dir
    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    raw = _raw_config(tmp_path / "data")
    _write_plugin_dir(tmp_path / "plug", raw)
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=tmp_path / "plug")
    app.profiles_cls = FakeProfiles
    await app.start()
    try:
        gid = next(iter(app.get_settings().groups.keys()))
        app.agents.skill_add(
            gid, "task", name="explore-pdf", description="PDF 表格",
            body="1. pdfplumber", source="auto", note=skill_explore._NOTE,
        )
        async with TestClient(TestServer(app.console.app)) as cli:
            r_login = await cli.post("/api/login", json={"password": PASSWORD})
            assert r_login.status == 200
            r_skills = await cli.get(f"/api/groups/{gid}/skills")
            assert r_skills.status == 200
            data = await r_skills.json()
            by_name = {s["name"]: s for s in data["skills"]}
            assert by_name["explore-pdf"]["origin"] == "explore"
    finally:
        await app.stop()




