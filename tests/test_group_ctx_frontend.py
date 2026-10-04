"""运行真实群页 JS：「这个群」区（docs/17 §八.4）——本群规矩看 / 改 / 历史回退，本群做法每岗一份 + 通用执行列表，
改、锁定、归档、删除、历史回退；接口按群 /api/groups/{gid}/rules|skills；换群不写错群。无网络。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
CTX = JS / "pages/groupctx.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const calls = [], toasts = [], fields = {};
for (const [id, value] of Object.entries(CFG.fields || {})) fields[id] = { value, attrs: {}, focus() {}, setAttribute(k, v) { this.attrs[k] = String(v); } };
for (const k of ['news', 'task', 'rules']) fields['gctx-err-' + k] = { hidden: true, textContent: '' };
fields['gctx-rules-err'] = { hidden: true, textContent: '' };
const NEWS = { id: 1, kind: 'news', name: 'news-本群做法', description: '', body: '做法：\n1. 先找<b>原始公告</b>', locked: false, status: 'active', uses: 0, created: 1790900000, updated: 1790950000 };
const TASKS = [
  { id: 5, kind: 'task', name: '整理报名表', description: '要把报名整理成表格时', body: '1. 先去重', locked: true, status: 'active', uses: 3, created: 1790900000, updated: 1790900000 },
  { id: 6, kind: 'task', name: '旧方法', description: '不用了', body: '…', locked: false, status: 'archived', uses: 0, created: 1790800000, updated: 1790800000 },
];
const RULES = CFG.noRules ? { body: '', updated: 0, updated_by: '' } : { body: '别发<script>手机评测', updated: 1790950000, updated_by: 'migrate' };
const slots = [{ innerHTML: '' }];
const state = { g: CFG.g || 'g1', gctx: null, agents: null };
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const listeners = {};
const context = vm.createContext({ console, setTimeout: (f) => f(), confirm: () => CFG.confirm !== false,
  document: { querySelectorAll: (sel) => (sel === '.gctx-slot' ? slots : []), querySelector: () => null, addEventListener: (t, f) => (listeners[t] ||= []).push(f) },
});
const api = async (method, url, body) => {
  calls.push({ method, url, body });
  if (method !== 'GET' && CFG.fail) throw new Error('服务器说不行');
  if (url.endsWith('/rules')) return method === 'GET' ? RULES : { body: body.body, updated: 1791000000, updated_by: 'admin' };
  if (url.endsWith('/skills')) return method === 'GET' ? { skills: CFG.noNews ? TASKS : [NEWS, ...TASKS] } : { id: 9, skills: [{ ...NEWS, id: 9, body: '服务器回的' }] };
  if (url.includes('/rules/versions/')) return { body: '旧规矩', updated: 1791000000, updated_by: 'admin' };
  if (url.endsWith('/versions')) return { versions: [{ id: 7, source: 'auto', updated_by: 'admin', note: '第一次写', ts: 1790900000, body: '旧的一版' }] };
  return { skill: NEWS, skills: [{ ...NEWS, body: '服务器回的新做法' }] };
};
const stubs = {
  '../state.js': { state, $: (id) => fields[id] },
  '../util.js': { esc, toast: (t, bad) => toasts.push([t, !!bad]), SVG: { pen: '[pen]', pin: '[pin]', archive: '[archive]', trash: '[trash]', plus: '[plus]' } },
  '../api.js': { api },
};
const modules = new Map();
async function link(spec) {
  if (!modules.has(spec)) {
    const exports = stubs[spec];
    if (!exports) throw new Error('unexpected import: ' + spec);
    const mod = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
    }, { context });
    modules.set(spec, mod); await mod.link(link);
  }
  return modules.get(spec);
}
const mod = new vm.SourceTextModule(fs.readFileSync(CFG.path, 'utf8'), { context });
await mod.link(link); await mod.evaluate();
mod.namespace.ctxSection('g1');
await new Promise((r) => setImmediate(r));
calls.length = 0;
const c = state.gctx;
if (CFG.edit) c.edit = CFG.edit;
if (CFG.open) c.open = CFG.open;
for (const [k, v] of Object.entries(CFG.drafts || {})) for (const f of listeners.input || []) f({ target: { id: k, value: v } });
if (CFG.switchTo) state.g = CFG.switchTo;
const handled = [];
for (const [action, dataset, attrs] of CFG.actions || []) {
  handled.push(await mod.namespace.actCtx(action, { disabled: false, dataset: dataset || {}, getAttribute: (k) => (attrs || {})[k] ?? null }));
}
mod.namespace.repaintCtx();
console.log(JSON.stringify({ html: slots[0].innerHTML, calls, toasts, handled, errNews: fields['gctx-err-news'], errTask: fields['gctx-err-task'],
  edit: c.edit, hist: c.hist, rules: c.rules, skills: c.skills }));
"""

NEWS = {"kind": "news", "id": "1", "g": "g1"}
TASK = {"kind": "task", "id": "5", "g": "g1"}


def run_js(tmp_path, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证群页「这个群」区"
    assert CTX.is_file(), "群页「这个群」前端文件缺失"
    script = tmp_path / "ctx.mjs"
    script.write_text(CHECK % json.dumps({"path": str(CTX), **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_skill_meta_uses_current_api_fields():
    source = CTX.read_text(encoding="utf-8")
    assert "s.last_note" not in source, "技能列表没有 last_note，改动说明应从历史版本读取"
    assert "s.updated" in source


def test_loads_rules_and_skills_from_group_endpoints(tmp_path):
    html = run_js(tmp_path)["html"]
    assert "本群规矩" in html and "别发&lt;script&gt;手机评测" in html and "<script>" not in html
    assert "从旧设置搬来的" in html
    assert "本群做法" in html and "资讯" in html and "先找&lt;b&gt;原始公告" in html
    for kind in ("idea", "goal"):
        assert f'data-act="gctx-skill-add" data-kind="{kind}"' in html  # 没有的岗位给「自己先写一份」
    assert "通用执行积累的做事方法 · 1" in html and "用过 3 次" in html and "已锁定" in html
    assert "已归档 · 1" in html and 'data-to="active"' in html


def test_empty_rules_invites_writing(tmp_path):
    html = run_js(tmp_path, noRules=True)["html"]
    assert "还没有。写下这个群必须照做的事" in html and 'data-act="gctx-rules-edit"' in html


def test_save_rules_puts_body(tmp_path):
    out = run_js(tmp_path, edit="rules", fields={"gctx-rules": " 少发手机评测 "},
                 actions=[["gctx-rules-save", {"g": "g1"}]])
    assert out["calls"] == [{"method": "PUT", "url": "/api/groups/g1/rules", "body": {"body": "少发手机评测"}}]
    assert out["rules"]["body"] == "少发手机评测" and out["edit"] is None


def test_rules_draft_survives_repaint(tmp_path):
    out = run_js(tmp_path, edit="rules", drafts={"gctx-rules": "写了一半"})
    assert "写了一半" in out["html"]


def test_rules_history_and_restore(tmp_path):
    out = run_js(tmp_path, actions=[["gctx-hist", {"id": "rules", "g": "g1"}]])
    assert out["calls"] == [{"method": "GET", "url": "/api/groups/g1/rules/versions"}]
    assert "旧的一版" in out["html"] and "网页上改的" in out["html"]
    out = run_js(tmp_path, actions=[["gctx-restore", {"id": "rules", "vid": "7", "g": "g1"}]])
    assert out["calls"] == [{"method": "POST", "url": "/api/groups/g1/rules/versions/7/restore", "body": {}}]
    assert out["rules"]["body"] == "旧规矩"
    assert run_js(tmp_path, confirm=False, actions=[["gctx-restore", {"id": "rules", "vid": "7", "g": "g1"}]])["calls"] == []


def test_edit_specialist_skill_patches_and_replaces_kind(tmp_path):
    out = run_js(tmp_path, edit={"kind": "news", "id": "1"}, fields={"gctx-body-news": " 新做法 "},
                 actions=[["gctx-skill-save", NEWS]])
    assert out["calls"] == [{"method": "PATCH", "url": "/api/groups/g1/skills/1", "body": {"body": "新做法"}}]
    news = [s for s in out["skills"] if s["kind"] == "news"]
    assert len(news) == 1 and news[0]["body"] == "服务器回的新做法"
    assert len([s for s in out["skills"] if s["kind"] == "task"]) == 2  # 别的岗位不受影响


def test_new_specialist_skill_posts_kind(tmp_path):
    out = run_js(tmp_path, noNews=True, edit={"kind": "news", "id": "new"}, fields={"gctx-body-news": "1. 先找原文"},
                 actions=[["gctx-skill-save", {"kind": "news", "id": "new", "g": "g1"}]])
    assert out["calls"] == [{"method": "POST", "url": "/api/groups/g1/skills", "body": {"kind": "news", "body": "1. 先找原文"}}]


def test_new_task_skill_needs_name_and_posts_all(tmp_path):
    ds = {"kind": "task", "id": "new", "g": "g1"}
    out = run_js(tmp_path, edit={"kind": "task", "id": "new"}, actions=[["gctx-skill-save", ds]],
                 fields={"gctx-name-task": "", "gctx-desc-task": "要做表时", "gctx-body-task": "1."})
    assert out["calls"] == [] and "名字" in out["errTask"]["textContent"]
    out = run_js(tmp_path, edit={"kind": "task", "id": "new"}, actions=[["gctx-skill-save", ds]],
                 fields={"gctx-name-task": "整理表格", "gctx-desc-task": "要做表时", "gctx-body-task": "1. 先列字段"})
    assert out["calls"] == [{"method": "POST", "url": "/api/groups/g1/skills",
                             "body": {"kind": "task", "name": "整理表格", "description": "要做表时", "body": "1. 先列字段"}}]


def test_empty_body_shows_accessible_error(tmp_path):
    out = run_js(tmp_path, edit={"kind": "news", "id": "1"}, fields={"gctx-body-news": "  "}, actions=[["gctx-skill-save", NEWS]])
    assert out["calls"] == [] and out["errNews"]["hidden"] is False and "不能空着" in out["errNews"]["textContent"]
    assert 'aria-describedby="gctx-help-news gctx-err-news"' in out["html"]


def test_lock_archive_restore_delete(tmp_path):
    out = run_js(tmp_path, actions=[["gctx-skill-lock", NEWS, {"aria-pressed": "false"}]])
    assert out["calls"] == [{"method": "PATCH", "url": "/api/groups/g1/skills/1", "body": {"locked": True}}]
    out = run_js(tmp_path, actions=[["gctx-skill-status", {**TASK, "to": "archived"}]])
    assert out["calls"] == [{"method": "PATCH", "url": "/api/groups/g1/skills/5", "body": {"status": "archived"}}]
    out = run_js(tmp_path, actions=[["gctx-skill-status", {**TASK, "id": "6", "to": "active"}]])
    assert out["calls"][0]["body"] == {"status": "active"}
    assert run_js(tmp_path, confirm=False, actions=[["gctx-skill-del", TASK]])["calls"] == []
    assert run_js(tmp_path, actions=[["gctx-skill-del", TASK]])["calls"] == [{"method": "DELETE", "url": "/api/groups/g1/skills/5"}]


def test_skill_history_and_restore(tmp_path):
    out = run_js(tmp_path, actions=[["gctx-hist", NEWS]])
    assert out["calls"] == [{"method": "GET", "url": "/api/groups/g1/skills/1/versions"}]
    assert "旧的一版" in out["html"] and "MaiWork 改的" in out["html"] and 'data-act="gctx-restore"' in out["html"]
    out = run_js(tmp_path, actions=[["gctx-restore", {**NEWS, "vid": "7"}]])
    assert out["calls"] == [{"method": "POST", "url": "/api/groups/g1/skills/1/versions/7/restore", "body": {}}]


def test_write_refused_after_group_switch(tmp_path):
    out = run_js(tmp_path, switchTo="g2", actions=[["gctx-skill-lock", NEWS, {"aria-pressed": "false"}]])
    assert out["calls"] == [] and out["toasts"] and out["toasts"][0][1] is True


def test_server_error_keeps_form(tmp_path):
    out = run_js(tmp_path, fail=True, edit={"kind": "news", "id": "1"}, fields={"gctx-body-news": "新做法"},
                 actions=[["gctx-skill-save", NEWS]])
    assert out["errNews"]["textContent"] == "服务器说不行" and out["edit"] == {"kind": "news", "id": "1"}


def test_other_actions_not_handled():
    src = CTX.read_text(encoding="utf-8")
    assert 'if (!action || !action.startsWith("gctx-")) return false;' in src


def test_group_page_shows_area_only_to_group_admins_and_old_entries_gone():
    group = (JS / "pages/group.js").read_text(encoding="utf-8")
    assert re.search(r"function ctxBlock\(g\) \{\n  if \(!gadmin\(\)\) return \"\";", group)
    assert group.count("ctxBlock(g)") >= 3  # 定义 + 熟悉中 + 正常
    every = "\n".join(p.read_text(encoding="utf-8") for p in JS.rglob("*.js") if not p.name.startswith("._"))
    for gone in ("/feeds-pref", "/taste`", "group-memory", "agent-memory-save", "/agents/${encodeURIComponent(kind)}/memory", "agent_skills.js"):
        assert gone not in every, gone
    assert not (JS / "settings/agent_skills.js").exists()
