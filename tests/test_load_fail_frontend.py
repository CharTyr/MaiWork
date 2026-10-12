"""网页加载失败：原地留下原因和「重试」，不再只剩一直转的「加载中」（design.md §8.3）。

覆盖：通用 loading(原因, 哪一块)、各设置子页 / 群视图 / 聊天的失败记录与清除、
来源页「优质来源」失败后可重试、管理员登录过期时的提示、「重试」按钮的接线。
真跑前端模块（node vm），依赖模块按 import 清单自动替身，个别名字按用例给真值。
"""
import json
import shutil
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const out = {};
const calls = [];
const toasts = [];
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const el = () => ({ innerHTML: '', style: { setProperty() {} }, classList: { toggle() {}, remove() {}, add() {} }, querySelectorAll: () => [], querySelector: () => null, contains: () => false, dataset: {} });
const els = {};
const $ = (id) => (els[id] ||= el());
const doc = { activeElement: null, hidden: false, body: { classList: { toggle() {}, remove() {}, add() {} }, scrollHeight: 0 }, title: '',
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
const context = vm.createContext({ console, document: doc, window: { scrollTo() {}, addEventListener() {}, innerHeight: 800, scrollY: 0 }, location: { hash: CFG.hash || '', search: '' },
  history: { replaceState() {} }, setInterval: () => 0, setTimeout: () => 0, clearTimeout() {}, localStorage: { getItem: () => null, setItem() {} },
  CSS: { escape: (s) => String(s) }, URLSearchParams, Blob, MutationObserver: class { observe() {} disconnect() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }) });
const state = Object.assign({ groups: [], tab: 'news', page: null, me: { role: 'admin' }, g: null, view: null, settings: null }, CFG.state || {});
let apiFail = CFG.apiFail || '';
const api = async (method, url) => {
  calls.push([method, url]);
  if (apiFail) { const e = new Error(apiFail); if (CFG.status) e.status = CFG.status; throw e; }
  return CFG.reply ?? {};
};
const loadingStub = (f, w, g) => (f ? `FAIL[${f}|${w}${g ? '|' + g : ''}]` : 'DOTS');
const given = {
  state, ui: { flash: true }, $, esc, api, toast: (m, b) => toasts.push([m, !!b]),
  admin: () => state.me && state.me.role === 'admin', gadmin: () => state.me && ['admin', 'group_admin'].includes(state.me.role),
  isGA: () => false, desktop: { matches: true },
  TABS: [{ id: 'news', label: '资讯' }, { id: 'ideas', label: '构想' }, { id: 'tasks', label: '任务' }, { id: 'group', label: '群' }],
  SVG: new Proxy({}, { get: () => '' }), CATS: {}, TONES: [], STATUS: {}, FILTERS: [], ROLE_NAMES: {}, SET_SUBS: [['overview', '设置']],
  grp: () => state.groups.find((x) => x.id === state.g) || null,
  gview: () => (state.view && state.view.id === state.g ? state.view : null),
  groupRef: (g) => g.id, gname: (g) => (g && g.name) || '', mq: (s) => esc(s),
  quiet: () => ({ text: '', usual: '', live: false }), loading: loadingStub,
  viewNews: () => 'NEWS', viewGroup: () => 'GROUP', viewIdeas: () => 'IDEAS', viewTasks: () => 'TASKS',
};
const record = (name) => (...args) => { calls.push(['call', name, ...args.map((a) => (a && a.dataset ? a.dataset : a))]); return ''; };
const modules = new Map();
async function link(spec, ref) {
  if (modules.has(spec)) return modules.get(spec);
  const names = new Set();
  // 被测模块对这个依赖 import 了哪些名字：给真值（given）或记录调用的替身
  for (const m of ref.__src.matchAll(/import\s*\{([^}]*)\}\s*from\s*["']([^"']+)["']/g)) {
    if (m[2] !== spec) continue;
    for (const n of m[1].split(',').map((x) => x.trim()).filter(Boolean)) names.add(n.split(/\s+as\s+/)[0]);
  }
  const mod = new vm.SyntheticModule([...names], function () {
    for (const n of names) this.setExport(n, n in given ? given[n] : record(n));
  }, { context });
  mod.__src = '';
  modules.set(spec, mod);
  return mod;
}
const src = fs.readFileSync(CFG.path, 'utf8');
const mod = new vm.SourceTextModule(src, { context });
mod.__src = src;
await mod.link(link);
await mod.evaluate();
const M = mod.namespace;
const tick = () => new Promise((r) => setImmediate(r));
%s
console.log(JSON.stringify({ out, calls, toasts, state: { loadErr: state.loadErr || null, badLink: state.badLink ?? null, trusted: state.trusted || null, view: state.view } }));
"""


def run(tmp_path, rel, body, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证网页加载失败的表现"
    path = JS / rel
    assert path.is_file(), rel
    script = tmp_path / "t.mjs"
    script.write_text(HARNESS % (json.dumps({"path": str(path), **cfg}), body), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ───── 通用组件：loading(原因, 哪一块) ─────

def test_loading_without_reason_is_unchanged(tmp_path):
    o = run(tmp_path, "pages/news.js", "out.a = M.loading();")
    assert o["out"]["a"] == '<div class="loading"><span></span><span></span><span></span></div>'


def test_loading_with_reason_shows_cause_and_retry(tmp_path):
    o = run(tmp_path, "pages/news.js", "out.a = M.loading('无法连接<请检查>', 'rules'); out.b = M.loading('出错', 'trusted', 'g1');")
    a, b = o["out"]["a"], o["out"]["b"]
    assert 'role="alert"' in a and "加载失败" in a
    assert "无法连接&lt;请检查&gt;" in a and "<请检查>" not in a
    assert 'data-act="load-retry"' in a and 'data-what="rules"' in a and "重试" in a
    assert 'class="loading"' not in a
    assert 'data-what="trusted"' in b and 'data-g="g1"' in b


# ───── 设置子页：失败记原因，成功清掉 ─────

def test_rules_failure_keeps_reason_then_clears(tmp_path):
    body = """
await M.loadRules(); out.fail = M.rulesPage();
apiFail = ''; await M.loadRules(); out.err = (state.loadErr || {}).rules ?? null;
"""
    o = run(tmp_path, "settings/rules.js", body, apiFail="无法连接，请检查网络", reply={"sections": []})
    assert o["out"]["fail"] == "FAIL[无法连接，请检查网络|rules]"
    assert o["out"]["err"] is None


def test_usage_failure_shows_retry(tmp_path):
    o = run(tmp_path, "settings/usage.js", "await M.loadUsage(); out.fail = M.usagePage();", apiFail="请求失败（502）")
    assert o["out"]["fail"] == "FAIL[请求失败（502）|usage]"


def test_ext_failure_is_not_shown_as_empty_list(tmp_path):
    o = run(tmp_path, "settings/ext.js", "await M.loadExt(); out.fail = M.extPage();", apiFail="请求失败（500）")
    assert o["out"]["fail"] == "FAIL[请求失败（500）|ext]"


def test_identity_failure_shows_retry(tmp_path):
    o = run(tmp_path, "settings/identity.js", "await M.loadIdentity(); out.fail = M.memoryPage({});", apiFail="请求失败（500）")
    assert o["out"]["fail"] == "FAIL[请求失败（500）|identity]"


def test_settings_shell_failure_shows_retry(tmp_path):
    o = run(tmp_path, "settings/index.js", "out.fail = M.settingsPage();", state={"setSub": "overview", "loadErr": {"settings": "请求失败（503）"}})
    assert "FAIL[请求失败（503）|settings]" in o["out"]["fail"]


def test_chat_first_load_failure_shows_retry(tmp_path):
    body = "await M.loadChat(7); out.fail = M.chatPage();"
    o = run(tmp_path, "chat.js", body, apiFail="请求失败（500）", state={"page": "chat", "chatId": 7, "chats": []})
    assert "FAIL[请求失败（500）|chat]" in o["out"]["fail"]


# ───── 路由：设置 / 群视图失败记录，登录过期判断，重试接线 ─────

def test_router_settings_and_view_failures_are_recorded(tmp_path):
    body = """
await M.loadSettings(); out.settings = state.loadErr.settings;
await M.loadView(true); out.view = state.loadErr.view;
out.keptView = state.view && state.view.id;
apiFail = ''; await M.loadView(true); out.cleared = state.loadErr.view ?? null;
"""
    o = run(tmp_path, "router.js", body, apiFail="无法连接，请检查网络",
            state={"g": "g1", "groups": [{"id": "g1"}], "view": {"id": "g1", "old": True}}, reply={"id": "g1"})
    assert o["out"]["settings"] == "无法连接，请检查网络"
    assert o["out"]["view"] == "无法连接，请检查网络"
    assert o["out"]["keptView"] == "g1"  # 轮询失败不清掉已有内容
    assert o["out"]["cleared"] is None


def test_link_problem_tells_admin_login_expired(tmp_path):
    body = """
out.admin = M.linkProblem({ ref: '123456789', tab: 'news' });
out.settings = M.linkProblem({ settings: true, ref: '' });
out.chat = M.linkProblem({ chat: true, ref: '' });
out.member = M.linkProblem({ ref: 'Ab3xKp9Q', tab: 'news' });
out.none = M.linkProblem({ ref: '' });
"""
    o = run(tmp_path, "router.js", body)
    assert o["out"] == {"admin": "expired", "settings": "expired", "chat": "expired", "member": "link", "none": ""}


def test_retry_reloads_the_failed_block(tmp_path):
    body = """
for (const what of ['rules', 'ext', 'identity', 'settings', 'chat']) await M.retryLoad({ dataset: { what }, disabled: false });
state.trusted = { g1: { error: 'x' }, g2: { trusted: [] } };
await M.retryLoad({ dataset: { what: 'trusted', g: 'g1' }, disabled: false });
"""
    o = run(tmp_path, "router.js", body, state={"chatId": 7, "g": None})
    names = [c[1] for c in o["calls"] if c[0] == "call"]
    for n in ("loadRules", "loadExt", "loadIdentity", "loadChat", "render"):
        assert n in names, n
    assert ["GET", "/api/settings"] in o["calls"]
    assert "g1" not in o["state"]["trusted"] and "g2" in o["state"]["trusted"]


def test_actions_route_retry_button_to_router(tmp_path):
    o = run(tmp_path, "actions.js", "await M.act({ dataset: { act: 'load-retry', what: 'rules' }, disabled: false });")
    assert ["call", "retryLoad", {"act": "load-retry", "what": "rules"}] in o["calls"]


# ───── 整页渲染：群视图失败 / 数据过期 / 登录过期 ─────

def test_group_view_failure_shows_retry_on_every_tab(tmp_path):
    for tab in ("news", "group"):
        o = run(tmp_path, "render.js", "M.renderView(); out.html = $('view').innerHTML;",
                state={"tab": tab, "g": "g1", "groups": [{"id": "g1"}], "loadErr": {"view": "无法连接，请检查网络"}})
        assert "FAIL[无法连接，请检查网络|view]" in o["out"]["html"], tab


def test_stale_view_gets_notice_but_keeps_content(tmp_path):
    o = run(tmp_path, "render.js", "M.renderView({ poll: true }); out.html = $('view').innerHTML;",
            state={"tab": "news", "g": "g1", "groups": [{"id": "g1"}], "view": {"id": "g1"}, "loadErr": {"view": "无法连接"}})
    html = o["out"]["html"]
    assert "NEWS" in html and "连接中断" in html and 'role="status"' in html


def test_expired_admin_sees_login_hint_not_group_command(tmp_path):
    o = run(tmp_path, "render.js", "M.render(); out.html = $('view').innerHTML;", state={"me": {"role": "none"}, "badLink": "expired"})
    html = o["out"]["html"]
    assert "登录已过期" in html and "请在群内发送 <b>/mw 网页</b> 获取新链接" not in html
    o = run(tmp_path, "render.js", "M.render(); out.html = $('view').innerHTML;", state={"me": {"role": "none"}, "badLink": "link"})
    assert "链接已失效" in o["out"]["html"]


# ───── 来源页：「优质来源」失败后不再永远「加载中」 ─────

def test_trusted_sources_failure_offers_retry(tmp_path):
    body = """
const g = { id: 'g1', name: '群一' };
out.first = M.sourcesPage({ groups: [g], feeds: { rss: {} } });
await tick(); await tick();
out.after = M.sourcesPage({ groups: [g], feeds: { rss: {} } });
out.requests = calls.filter((c) => c[1] && String(c[1]).includes('trusted-sources')).length;
"""
    o = run(tmp_path, "settings/sources.js", body, apiFail="请求失败（500）")
    after = o["out"]["after"]
    assert "FAIL[请求失败（500）|trusted|g1]" in after
    assert o["out"]["requests"] == 1  # 失败后不自动狂刷，等用户点重试
