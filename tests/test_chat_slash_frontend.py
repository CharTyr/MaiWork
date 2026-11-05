"""前端单测：主模型对话双层输入坞、/ 斜杠指令与技能面板、@ 引用面板、折叠与复制重试。

使用 Node --experimental-vm-modules 加载真实 ES 模块，找不到 node 直接报错（不许 skip）。
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
    script = tmp_path / "run_chat_slash.mjs"
    script.write_text(
        r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = %s;
const apiCalls = [];
let apiResponder = async (method, url, body) => ({});
const elements = new Map();
const makeEl = (id, init = {}) => {
  const el = {
    id,
    value: '',
    innerHTML: '',
    textContent: '',
    hidden: false,
    disabled: false,
    scrollTop: 0,
    scrollHeight: 40,
    selectionStart: 0,
    selectionEnd: 0,
    style: { setProperty() {}, removeProperty() {} },
    dataset: {},
    classList: {
      _s: new Set(),
      add(...xs) { xs.forEach((x) => this._s.add(x)); },
      remove(...xs) { xs.forEach((x) => this._s.delete(x)); },
      toggle(x, on) { if (on === undefined) on = !this._s.has(x); on ? this._s.add(x) : this._s.delete(x); },
      contains(x) { return this._s.has(x); },
    },
    focus() { el._focused = true; },
    setSelectionRange(a, b) { el.selectionStart = a; el.selectionEnd = b; },
    getBoundingClientRect: () => ({ bottom: 600, right: 200, left: 20 }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    requestSubmit() { el._submitted = (el._submitted || 0) + 1; },
    ...init,
  };
  elements.set(id, el);
  return el;
};
for (const id of ['view', 'side', 'rail', 'top', 'tabbar', 'sheet', 'scrim', 'toast', 'app', 'chat-stream', 'chat-input', 'chat-form']) {
  makeEl(id);
}

const context = vm.createContext({
  console, URL, URLSearchParams, Date, Math, JSON, Number, String, Boolean, Array, Object, Set, Map, WeakMap, RegExp, Error, Promise,
  Blob: class { constructor(parts) { this.size = Buffer.byteLength(parts.join('')); } },
  MutationObserver: class { observe() {} disconnect() {} },
  ResizeObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} },
  navigator: { clipboard: { writeText: async (t) => { context._copied = t; } } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '#/chat/1', search: '', origin: 'https://mw.test' },
  history: { replaceState(_s, _t, h) { context.location.hash = h; } },
  window: {
    innerHeight: 800, innerWidth: 1280, scrollY: 0,
    scrollTo() {},
    addEventListener() {},
    matchMedia: () => ({ matches: true, addEventListener() {} }),
  },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  requestAnimationFrame: () => 1,
  cancelAnimationFrame() {},
  setTimeout: () => 1,
  clearTimeout() {},
  setInterval: () => 1,
  clearInterval() {},
  confirm: () => true,
  prompt: (_msg, defVal) => defVal ? defVal + '（改）' : '新标题',
  fetch: async (url, opts) => {
    apiCalls.push({ method: (opts && opts.method) || 'GET', url, body: opts && opts.body ? JSON.parse(opts.body) : undefined });
    const data = await apiResponder((opts && opts.method) || 'GET', url, opts && opts.body ? JSON.parse(opts.body) : undefined);
    return { ok: true, status: 200, json: async () => data };
  },
  document: {
    hidden: false,
    body: { scrollHeight: 900, classList: makeEl('_body').classList },
    activeElement: null,
    contains: () => true,
    getElementById: (id) => elements.get(id) || null,
    querySelector: (sel) => {
      if (sel === '.chat-send') return makeEl('_send');
      return null;
    },
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
const chatMod = await load(path.join(ROOT, 'chat.js'));
const slashMod = await load(path.join(ROOT, 'chat_slash.js'));
const detailMod = await load(path.join(ROOT, 'detail.js'));
const boardMod = await load(path.join(ROOT, 'board.js'));
const { state } = stMod.namespace;
const chat = chatMod.namespace;
const slash = slashMod.namespace;
const detail = detailMod.namespace;
const board = boardMod.namespace;

%s
"""
        % (json.dumps(str(JS)), script_body),
        encoding="utf-8",
    )
    res = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=25)
    assert res.returncode == 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    lines = [ln for ln in res.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {}


def test_slash_and_ref_filtering_and_ring(tmp_path: Path) -> None:
    out = _run_vm(
        r"""
const skills = [
  { name: 'office-report', title: 'office-report', description: '整理周报与结构化文档', scope: 'global' },
  { name: '本群/周报模板', title: '周报模板', description: '按本群口吻汇总进展', scope: 'group', group_id: '10001' },
];
const allSlash = slash.filterSlashItems('', skills);
const names = allSlash.map((x) => x.key);
assert.ok(names.includes('/compact'), '应包含 /compact');
assert.ok(names.includes('/clear'), '应包含 /clear');
assert.ok(names.includes('/new'), '应包含 /new');
assert.ok(!names.includes('/stop') && !names.includes('/status') && !names.includes('/group'), '不应包含冗余的 /stop /status /group');
assert.ok(names.some((n) => n.includes('office-report')), '应包含全局技能');
assert.ok(names.some((n) => n.includes('本群/周报模板')), '应包含本群做法');

const hitCn = slash.filterSlashItems('周报', skills);
assert.equal(hitCn.length, 2, '中文关键词可同时命中全局技能简介与本群做法名');

const parsed = slash.parseTypedSlash('/office-report 帮我写一份本周总结', skills);
assert.equal(parsed.skill.name, 'office-report');
assert.equal(parsed.rest, '帮我写一份本周总结');

const refs = [
  { kind: 'task', id: 'T-18', label: 'T-18', title: '排查接口超时', status: 'failed', group_id: '10001', group_name: '研发群' },
  { kind: 'goal', id: 'G-3', label: 'G-3', title: '盯紧新版发布', status: 'active', group_id: '10001', group_name: '研发群' },
  { kind: 'idea', id: '构想#9', label: '构想#9', title: '周五自动发汇总贴', status: 'new', group_id: '10001', group_name: '研发群' },
];
const hitRef = slash.filterRefItems('超时', refs);
assert.equal(hitRef.length, 1);
assert.equal(hitRef[0].id, 'T-18');

const ring = slash.contextRingView({
  model: 'deepseek-v3.2',
  ready: true,
  estimated_input_tokens: 45000,
  context_window: 100000,
  trigger_threshold: 80000,
  summary_covers: 12,
  has_uncompacted: true,
});
assert.equal(ring.pct, 45);
assert.equal(ring.modelLabel, 'deepseek-v3.2');
assert.equal(ring.canCompact, true);

console.log(JSON.stringify({ ok: true }));
""",
        tmp_path,
    )
    assert out["ok"] is True


def test_chat_composer_stream_folding_copy_retry_and_cross_jump(tmp_path: Path) -> None:
    out = _run_vm(
        r"""
state.me = { role: 'admin' };
state.page = 'chat';
state.groups = [{ id: '10001', name: '测试研发群' }];
state.chatId = 1;
state.chatSkill = { name: 'office-report', scope: 'global', description: '结构化报告' };
state.chatOpen = {};
state.chat = {
  id: 1,
  running: false,
  info: { id: 1, title: '排查问题', group_id: '10001', group_name: '测试研发群' },
  status: { model: 'gpt-5.4', ready: true, estimated_input_tokens: 12000, context_window: 128000, trigger_threshold: 96000, summary_covers: 4, has_uncompacted: true },
  pending: [{ id: 77, tool: 'task_manage', summary: '创建任务：修复超时', status: 'pending', args: { action: 'create' } }],
  messages: [
    { id: 1, role: 'user', content: '查一下今天各群状态', meta: { skill: { name: 'office-report', scope: 'global' } } },
    { id: 2, role: 'assistant', content: '', tool_calls: [{ id: 'c1' }, { id: 'c2' }, { id: 'c3' }], meta: { reasoning: '先依次读取三个数据源再汇总。' } },
    { id: 3, role: 'tool', name: 'web_search', content: 'ok1', meta: { ok: true, label: '搜索网页', args: '{"query":"MaiBot 更新"}' } },
    { id: 4, role: 'tool', name: 'fetch_page', content: 'ok2', meta: { ok: true, label: '读取网页', args: '{"url":"https://example.com/a"}' } },
    { id: 5, role: 'tool', name: 'read_file', content: 'ok3', meta: { ok: true, label: '读取文件', args: '{"path":"/tmp/report.md"}' } },
    { id: 6, role: 'tool', name: 'task_manage', content: '已请求管理员确认（编号 77）', meta: { ok: true, label: '任务管理', args: '{"action":"create"}' } },
    { id: 7, role: 'assistant', content: '整理结果如下：\n```python\nprint(42)\n```', tool_calls: [], meta: {} },
    { id: 8, role: 'system_note', content: '已停止本轮回复', meta: { kind: 'stopped' } },
  ],
};
state.chats = [{ id: 1, title: '排查问题', group_id: '10001', group_name: '测试研发群', updated: 1700000000 }];

const pageIdle = chat.chatPage();
// 精简顶栏：有主模型鱼，原顶栏不再放压缩按钮，输入坞内放已选技能胶囊、[/] 按钮、群选择、上下文环、发送按钮
assert.match(pageIdle, /chat-skill-pill[\s\S]*office-report/, '输入坞应展示已选技能胶囊');
assert.match(pageIdle, /data-act="chat-slash-btn"/, '输入坞应有 [/] 斜杠触发钮');
assert.match(pageIdle, /data-act="chat-ctx-toggle"[\s\S]*gpt-5\.4/, '输入坞应显示上下文微环与主模型名');
assert.doesNotMatch(pageIdle, /class="chat-head"[\s\S]*?data-act="chat-compact"/, '顶栏不应再放旧的压缩对话按钮');

// 消息流断言：用户技能徽标、默认折叠的思考过程、连续 3 条普通工具自动折叠组、待确认工具单独外露、代码块与回复复制钮、末尾停止后的重试按钮
assert.match(pageIdle, /cm-skill-tag[\s\S]*office-report/, '用户消息应展示所用技能徽标');
assert.match(pageIdle, /cm-think[\s\S]*思考过程/, '应渲染思考过程折叠块');
assert.match(pageIdle, /data-act="chat-tool-group"[\s\S]*3/, '连续 3 条普通成功工具应折叠为一组');
assert.match(pageIdle, /待确认/, '待确认工具不可被吞进普通工具折叠组');
assert.match(pageIdle, /data-act="chat-copy-code"/, '代码块应带复制按钮');
assert.match(pageIdle, /data-act="chat-copy-msg"/, '主模型回复应带复制按钮');
assert.match(pageIdle, /data-act="chat-retry"/, '本轮已停止且无待决阻塞时应提供重试按钮');

// 运行中：发送按钮切换成「停止」按钮，消息流底部展示实时步骤
state.chat.running = true;
const pageRun = chat.chatPage();
assert.match(pageRun, /data-act="chat-stop"/, '运行中发送钮应变为停止按钮');

// 侧栏：包含重命名与归档入口
const sideHtml = chat.chatSide();
assert.match(sideHtml, /data-act="chat-rename"/, '侧栏应支持重命名');
assert.match(sideHtml, /data-act="chat-archive"/, '侧栏应支持归档');

// 任务详情与看板抽屉（管理员视角）：包含「在对话中询问」联动按钮
state.g = '10001';
state.tasks = { 'T-99': { id: 'T-99', group_id: '10001', title: '导出失败排查', status: 'failed', req: '导出报表', criteria: [], timeline: [], delivery: [] } };
state.detail = { type: 'task', id: 'T-99' };
const dtHtml = detail.detailHTML();
assert.match(dtHtml, /data-act="chat-ask-ref"[\s\S]*在对话中询问/, '管理员任务详情应提供在对话中询问入口');

state.board = { lanes: [{ kind: 'task', running: [], history: [{ source: 'task', id: 'T-99', group_id: '10001', title: '导出失败排查', status: 'failed' }] }] };
state.boardItem = { source: 'task', id: 'T-99', kind: 'task', data: { item: { group_id: '10001', title: '导出失败排查', status: 'failed', elapsed_s: 12 }, steps: [] }, err: '' };
const biHtml = board.boardItemSheet();
assert.match(biHtml, /data-act="chat-ask-ref"[\s\S]*在对话中询问/, '看板工作过程抽屉应提供在对话中询问入口');

console.log(JSON.stringify({ ok: true }));
""",
        tmp_path,
    )
    assert out["ok"] is True
