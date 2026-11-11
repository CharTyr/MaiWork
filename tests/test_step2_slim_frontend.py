"""Step 2 减负瘦身前端回归：
1. settings/agents.js：新建自定义专岗支持填写「擅长做什么」并透传 instructions，编辑页 SOUL.md / AGENTS.md 折叠进高级区，去除强制手改主模型 AGENTS.md 的提示。
2. settings/identity.js：通用记忆页明确「跨群通用经验」与「本群设置」两层边界。
3. pages/ideas.js：管理员查看可执行构想时「直接执行」提升为主按钮；ideaAsk 复制为精简一行口令并动态读取 state.me.bot.name。
4. pages/news.js：群友视角屏蔽后台未配搜索/MCP 等运维报错文案。
"""

import json
import shutil
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"

RUNNER = r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const cfg = %s;
const calls = [];
const toasts = [];
const fields = cfg.fields || {};
const state = cfg.state || {
  me: { role: 'admin', bot: { name: '小麦' } },
  g: 'g1',
  groups: [{ id: 'g1', name: '群甲' }],
  ideaOff: {},
  ideaMore: null,
  agents: { profiles: [] },
  agentSkills: [],
  agentDocs: {},
  mdl: { models: [{ id: 'm1', name: '主模型1', endpoint: 'ep1' }], endpoints: [{ id: 'ep1', name: '端点1' }] },
  identity: { memory: { text: '通用偏好' }, limits: { memory: 16384 } },
};
const esc = (s) => String(s ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const defaults = {
  state,
  TONES: ['#123456'],
  $: (id) => fields[id] || null,
  admin: () => state.me.role === 'admin',
  gadmin: () => ['admin', 'group_admin'].includes(state.me.role),
  esc,
  safeUrl: (u) => u || '#',
  SVG: { check: '[ok]', close: '[x]', copy: '[cp]', more: '[...]', lock: '[l]', down: 'v' },
  ico: () => '',
  tip: (h) => h ? `<i class="tip">${h}</i>` : '',
  toast: (msg, bad) => toasts.push({ msg, bad: !!bad }),
  dayWord: () => '今天',
  when: () => '',
  gface: () => '',
  agentFish: () => '<svg></svg>',
  agentSeed: () => '',
  gview: () => ({ ideas: cfg.ideas || [] }),
  loading: () => '<p>加载</p>',
  emptyState: (_, t) => `<p>${t}</p>`,
  fbButtons: () => '',
  hash: () => 0,
  avatarBlock: () => '<div>头像</div>',
  avatar: () => '/logo.png',
  render: () => {},
  repaintSheet: () => {},
  EFFORT_NAMES: {},
  loadModels: async () => {},
  confirmExecTools: () => true,
  execConfirmNeeded: () => false,
  skillIconName: () => 'sparkles',
  fishSvg: () => '',
  fishTraits: () => ({}),
  parseCustom: () => null,
  customSeed: () => '',
  FISH_SPECIES: {},
  FISH_EYES: {},
  FISH_COLORS: [],
  FISH_COLOR_NAMES: {},
  FISH_BASE_RE: /x/,
  ONB_STEPS: [],
  onb: { open: false },
  onbPane: () => '',
  api: async (method, url, body) => {
    calls.push({ method, url, body });
    if (method === 'POST' && url === '/api/agents') return { kind: 'c_abc123', title: body.title, instructions: body.instructions || '' };
    if (url === '/api/agents') return { profiles: [{ kind: 'c_abc123', title: '日文翻译', instructions: '翻译日文资料', model: 'm1', skills: null }], skills: [] };
    if (url.endsWith('/docs')) return { soul: { text: 's' }, agents: { text: 'a' }, limits: {} };
    return {};
  },
};
const context = vm.createContext({
  console,
  Blob,
  Date,
  setTimeout: () => 0,
  confirm: () => true,
  MutationObserver: class { observe() {} disconnect() {} },
  window: { addEventListener() {} },
  document: { body: {}, addEventListener() {}, querySelector: () => null, querySelectorAll: () => [] },
});
const mods = new Map();
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  let src = fs.readFileSync(file, 'utf8'), m;
  if (path.relative(cfg.root, file).split(path.sep).join('/') === cfg.target) {
    m = new vm.SourceTextModule(src, { context, identifier: file });
    mods.set(file, m);
    await m.link((s, p) => load(path.resolve(path.dirname(p.identifier), s)));
  } else {
    const names = [...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)/gm)].map((x) => x[1]))];
    m = new vm.SyntheticModule(names, function () {
      for (const n of names) this.setExport(n, defaults[n] ?? (() => ''));
    }, { context });
    mods.set(file, m);
    await m.link(() => {});
  }
  return m;
}
const m = await load(path.join(cfg.root, cfg.target));
await m.evaluate();

let out = { calls, toasts };
if (cfg.mode === 'ideas') {
  out.askAll = m.namespace.ideaAsk(cfg.idea);
  state.ideaOff = { [cfg.idea.id]: [2] };
  out.askSub = m.namespace.ideaAsk(cfg.idea);
  state.ideaOff = {};
  out.detail = m.namespace.ideaDetail(cfg.idea);
} else if (cfg.mode === 'agents_ui') {
  state.agentNew = true;
  out.newHtml = m.namespace.agentsFormSheet();
  state.agentNew = false;
  state.agentEdit = 'c_abc123';
  out.editHtml = m.namespace.agentsFormSheet();
  out.listHtml = m.namespace.agentsPage();
  await m.namespace.actAgents('agent-create', { disabled: false, dataset: {} });
} else if (cfg.mode === 'identity') {
  out.html = m.namespace.memoryPage();
} else if (cfg.mode === 'news_member') {
  out.html = m.namespace.viewNews({ fresh: false }, cfg.view);
}
console.log(JSON.stringify(out));
"""


def _run_js(tmp_path: Path, target: str, **cfg) -> dict:
    node = shutil.which("node")
    assert node, "需要 Node 运行前端测试"
    script = tmp_path / "step2_fe.mjs"
    script.write_text(RUNNER % json.dumps({"root": str(JS), "target": target, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


IDEA_MULTI = {
    "id": 12,
    "kind": "help",
    "title": "整理本周开源项目速览",
    "body": "很长一段正文不应该被整段塞进群剪贴板里刷屏",
    "state": "new",
    "items": [
        {"no": 1, "kind": "task", "title": "汇总榜单", "desc": ""},
        {"no": 2, "kind": "task", "title": "补充对比", "desc": ""},
    ],
}


def test_idea_ask_is_concise_and_admin_gets_primary_execute_button(tmp_path: Path) -> None:
    admin_out = _run_js(
        tmp_path,
        "pages/ideas.js",
        mode="ideas",
        idea=IDEA_MULTI,
        state={"me": {"role": "admin", "bot": {"name": "小夏麦"}}, "ideaOff": {}, "ideaMore": None},
    )
    assert admin_out["askAll"] == "@小夏麦 构想 #12"
    assert admin_out["askSub"] == "@小夏麦 构想 #12（要做：1）"
    assert "很长一段正文" not in admin_out["askAll"]
    # 管理员主按钮直接就是「直接执行」，不再藏在 ⋯ 菜单里
    assert 'class="btn primary idea-go" data-act="idea" data-op="do" data-id="12"' in admin_out["detail"]
    assert 'data-act="idea-copy"' in admin_out["detail"]
    assert "@小夏麦" in admin_out["detail"]

    member_out = _run_js(
        tmp_path,
        "pages/ideas.js",
        mode="ideas",
        idea=IDEA_MULTI,
        state={"me": {"role": "member", "bot": {"name": "小夏麦"}}, "ideaOff": {}, "ideaMore": None},
    )
    assert 'data-op="do"' not in member_out["detail"]
    assert 'class="btn primary idea-go" data-act="idea-copy" data-id="12"' in member_out["detail"]
    assert "@小夏麦" in member_out["detail"]


def test_agents_ui_auto_dispatch_field_and_folded_prompt_editors(tmp_path: Path) -> None:
    out = _run_js(
        tmp_path,
        "settings/agents.js",
        mode="agents_ui",
        fields={
            "agent-new-title": {"value": "日文翻译"},
            "agent-new-when": {"value": "翻译日文技术资料与推文"},
        },
        state={
            "me": {"role": "admin"},
            "groups": [],
            "mdl": {"models": [{"id": "m1", "name": "模型1", "endpoint": "ep1"}], "endpoints": [{"id": "ep1", "name": "端点1"}]},
            "agents": {
                "profiles": [
                    {"kind": "main", "title": "主模型", "model": "m1"},
                    {"kind": "c_abc123", "title": "日文翻译", "instructions": "翻译日文技术资料", "model": "m1", "skills": None},
                ]
            },
            "agentSkills": [],
            "agentDocs": {"c_abc123": {"soul": {"text": "S"}, "agents": {"text": "A"}, "limits": {}}},
        },
    )
    assert 'id="agent-new-when"' in out["newHtml"]
    assert "需在主模型的 AGENTS.md 中写明" not in out["newHtml"]
    assert 'id="agent-when"' in out["editHtml"]
    assert '<details class="cfg-advanced"><summary>高级 · 自定义岗位提示词（SOUL.md / AGENTS.md）</summary>' in out["editHtml"]
    assert "翻译日文技术资料" in out["listHtml"]
    create_call = next(c for c in out["calls"] if c["method"] == "POST" and c["url"] == "/api/agents")
    assert create_call["body"] == {"title": "日文翻译", "instructions": "翻译日文技术资料与推文"}
    assert not any("AGENTS.md" in t["msg"] for t in out["toasts"])


def test_identity_memory_page_explains_two_layer_boundary(tmp_path: Path) -> None:
    out = _run_js(tmp_path, "settings/identity.js", mode="identity")
    assert "跨所有群通用的经验" in out["html"]
    assert "本群设置" in out["html"]


def test_news_page_hides_internal_setup_note_from_members(tmp_path: Path) -> None:
    view = {
        "news": [
            {
                "id": 1,
                "slot_ts": 1760000000,
                "skipped": True,
                "items": [],
                "note": "还没指定联网搜索：去 设置 → 工具与平台 里选一个 MCP 用作联网搜索",
            }
        ],
        "guides": [],
    }
    member_out = _run_js(
        tmp_path,
        "pages/news.js",
        mode="news_member",
        view=view,
        state={"me": {"role": "member"}, "newsTab": "news"},
    )
    assert "设置 → 工具与平台" not in member_out["html"]
    assert "MCP" not in member_out["html"]
    assert "本轮暂无新资讯" in member_out["html"]
