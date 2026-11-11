"""Step 1 开箱前端回归：
1. onboarding.js：未配置的模板端点隐藏 api.example.com/v1 与占位模型名；「群管理」一步支持填写群内审批人账号并同步到各服务群 /approval。
2. settings/rules.js：PLATFORM_NAMES 去掉不支持的平台；groups chip 支持 telegram 负数群 ID 与 qqbot openid。
3. settings/index.js：设置概况提供一键下载脱敏数据备份入口 /api/settings/backup。
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
const fields = cfg.fields || {};
const state = cfg.state || {
  me: { role: 'admin', bot: { name: 'MaiBot' } },
  g: 'g1',
  groups: [{ id: 'g1', name: '群甲' }, { id: '-100388359', name: 'TG群' }],
  settings: { health: [], usage: { today: {} } },
  mdl: { endpoints: [], models: [] },
  agents: { profiles: [] },
  rules: { sections: [] },
  usage: { days: 7, hist: { days: [], totals: {} }, sel: '' },
};
const esc = (s) => String(s ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const defaults = {
  state,
  $: (id) => fields[id] || null,
  admin: () => state.me.role === 'admin',
  gadmin: () => ['admin', 'group_admin'].includes(state.me.role),
  esc,
  SVG: { check: 'ok', close: 'x', plus: '+', trash: 't', lock: 'l', right: '>' },
  ico: () => '',
  tip: (h) => h ? `<i class="tip">${h}</i>` : '',
  toast: () => {},
  tokens: (n) => String(n || 0),
  gname: (g) => (g && g.name) || '',
  gface: () => '',
  mq: (s) => s,
  platTag: () => '',
  quiet: () => ({ text: '' }),
  loading: () => '<p>加载</p>',
  ROWS_COLS: { serve_groups: [['group', 'qq:群号'], ['workspace', '']] },
  AV_SRC: {},
  PROTOCOLS: { openai: { name: 'OpenAI 兼容', ph: 'https://api.openai.com/v1' } },
  api: async (method, url, body) => {
    calls.push({ method, url, body });
    if (url.endsWith('/approval')) return body;
    return { password_set: true, accounts: [] };
  },
};
const context = vm.createContext({ console, document: { addEventListener() {} }, Date, URLSearchParams });
const mods = new Map();
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  let src = fs.readFileSync(file, 'utf8'), m;
  if (path.relative(cfg.root, file).split(path.sep).join('/') === cfg.target) {
    if (cfg.target === 'onboarding.js') src += '\nexport { onbSave };';
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

let out = {};
if (cfg.mode === 'onb_models') {
  out.html = m.namespace.onbPane('models');
} else if (cfg.mode === 'onb_admins') {
  Object.assign(m.namespace.onb, cfg.onb || {});
  out.html = m.namespace.onbPane('admins');
  out.error = await m.namespace.onbSave('admins');
  out.calls = calls;
} else if (cfg.mode === 'rules_chips') {
  out.html = m.namespace.rulesPage();
  const added = [];
  for (const item of (cfg.chips || [])) {
    const errEl = { textContent: '', hidden: true };
    const platEl = { value: item.plat };
    const accEl = { value: item.acc };
    const existing = [];
    const listEl = {
      querySelectorAll: () => existing,
      querySelector: () => null,
      insertAdjacentHTML: (_, h) => {
        const mVal = /data-v="([^"]+)"/.exec(h);
        if (mVal) existing.push({ dataset: { v: mVal[1] } });
        added.push({ input: item, html: h, val: mVal ? mVal[1] : '' });
      },
    };
    const box = {
      dataset: { kind: item.kind || 'groups' },
      querySelector: (sel) => {
        if (sel === '.ci-err') return errEl;
        if (sel === '.ca-plat') return platEl;
        if (sel === '.ca-acc') return accEl;
        if (sel === '.ci-list') return listEl;
        return null;
      },
    };
    m.namespace.chipAdd(box);
    if (!errEl.hidden) added.push({ input: item, error: errEl.textContent });
  }
  out.added = added;
} else if (cfg.mode === 'overview') {
  out.html = m.namespace.settingsPage();
}
console.log(JSON.stringify(out));
"""


def _run_js(tmp_path: Path, target: str, **cfg) -> dict:
    node = shutil.which("node")
    assert node, "需要 Node 运行前端契约测试"
    script = tmp_path / "step1_fe.mjs"
    script.write_text(RUNNER % json.dumps({"root": str(JS), "target": target, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_onboarding_hides_template_placeholder_url_and_model(tmp_path: Path) -> None:
    out = _run_js(
        tmp_path,
        "onboarding.js",
        mode="onb_models",
        state={
            "me": {"role": "admin", "bot": {}},
            "mdl": {
                "endpoints": [
                    {
                        "id": "main_provider",
                        "name": "主用服务商",
                        "protocol": "openai",
                        "base_url": "https://api.example.com/v1",
                        "key_set": False,
                        "placeholder": True,
                    }
                ],
                "models": [
                    {"id": "strong", "model": "填服务商那边的模型名", "placeholder": True},
                ],
            },
            "agents": {"profiles": [{"kind": "main", "model": "strong"}, {"kind": "task", "model": "strong"}]},
        },
    )
    assert 'value="https://api.example.com/v1"' not in out["html"]
    assert "填服务商那边的模型名" not in out["html"]
    assert 'placeholder="https://api.openai.com/v1"' in out["html"]


def test_onboarding_admins_step_saves_approvers_to_served_groups(tmp_path: Path) -> None:
    out = _run_js(
        tmp_path,
        "onboarding.js",
        mode="onb_admins",
        fields={"onb-approvers": {"value": "12345678, tg:alice_bot"}},
        onb={
            "cfg": {"sections": [{"fields": [{"key": "groups.serve", "value": [{"group": "qq:g1"}, {"group": "telegram:-100388359"}]}]}]},
            "approvals": {
                "g1": {"approvers": [], "exempt_users": ["qq:999"], "required": True, "exempt_group": False},
                "-100388359": {"approvers": [], "exempt_users": [], "required": True, "exempt_group": False},
            },
            "ga": {},
        },
    )
    assert out["error"] == ""
    assert 'id="onb-approvers"' in out["html"]
    assert out["calls"] == [
        {
            "method": "PUT",
            "url": "/api/groups/g1/approval",
            "body": {
                "approvers": ["qq:12345678", "telegram:alice_bot"],
                "exempt_users": ["qq:999"],
                "required": True,
                "exempt_group": False,
            },
        },
        {
            "method": "PUT",
            "url": "/api/groups/-100388359/approval",
            "body": {
                "approvers": ["qq:12345678", "telegram:alice_bot"],
                "exempt_users": [],
                "required": True,
                "exempt_group": False,
            },
        },
    ]


def test_rules_platforms_and_non_qq_group_chips(tmp_path: Path) -> None:
    out = _run_js(
        tmp_path,
        "settings/rules.js",
        mode="rules_chips",
        state={
            "rules": {"sections": [{"id": "approval", "label": "审批", "fields": []}]},
        },
        chips=[
            {"kind": "groups", "plat": "telegram", "acc": "-1001234567890"},
            {"kind": "groups", "plat": "qqbot", "acc": "OPENID_GROUP_01"},
            {"kind": "groups", "plat": "qq", "acc": "not_digits"},
        ],
    )
    for dead in ("Discord", "微信", "KOOK"):
        assert dead not in out["html"]
    assert "QQ 官方" in out["html"]
    assert out["added"][0]["val"] == "telegram:-1001234567890"
    assert out["added"][1]["val"] == "qqbot:OPENID_GROUP_01"
    assert "error" in out["added"][2]


def test_settings_overview_has_backup_download_entry(tmp_path: Path) -> None:
    out = _run_js(
        tmp_path,
        "settings/index.js",
        mode="overview",
        state={
            "setSub": "overview",
            "groups": [],
            "settings": {"health": [], "usage": {"today": {}}},
        },
    )
    assert 'href="/api/settings/backup"' in out["html"]
    assert "数据备份" in out["html"]
