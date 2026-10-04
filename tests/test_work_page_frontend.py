"""真实 JS 验证四项导航、在做的事合页和旧目标链接兼容。"""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

STATIC = Path(__file__).resolve().parents[1] / "maiwork/console/static"
JS = STATIC / "js"
CHECK = r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const cfg = %s;
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const context = vm.createContext({console, URLSearchParams, Date,
  window: {matchMedia: () => ({matches: false}), scrollTo() {}},
  document: {hidden: true, getElementById: () => null, addEventListener() {}},
  location: {hash: cfg.hash || '', search: ''}, history: {replaceState() {}},
  setInterval() {}, setTimeout() {}, clearTimeout() {},
});
const defaults = {esc, SVG: {}, dur: () => '1天', when: () => '明天', now: () => 100,
  ico: () => '<i></i>', agentFish: (kind) => `<svg data-fish="${kind}"></svg>`,
  emptyState: (icon, title, body) => `<p>${title}</p><p>${body}</p>`,
  findIdea: () => null, ideaItems: () => [], grp: () => ({id: 'g1'}), SET_SUBS: [],
};
const mods = new Map();
const real = new Set(['state.js', 'pages/tasks.js', 'pages/goals.js', 'router.js']);
async function load(file) {
  if (mods.has(file)) return mods.get(file);
  const source = fs.readFileSync(file, 'utf8');
  let mod;
  if (real.has(path.relative(cfg.root, file))) {
    mod = new vm.SourceTextModule(source, {context, identifier: file});
    mods.set(file, mod);
    await mod.link((spec, parent) => load(path.resolve(path.dirname(parent.identifier), spec)));
  } else {
    const names = [...new Set([...source.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm)].map(m => m[1]))];
    mod = new vm.SyntheticModule(names, function () {
      for (const name of names) this.setExport(name, defaults[name] ?? (() => {}));
    }, {context});
    mods.set(file, mod); await mod.link(() => {});
  }
  return mod;
}
const st = await load(path.join(cfg.root, 'state.js')); await st.evaluate();
const state = st.namespace.state;
state.me = {role: cfg.role || 'member', bot: {name: 'MaiBot'}};
state.groups = [{id: 'g1'}]; state.g = 'g1';
let html = '', route = null;
if (cfg.route) {
  const mod = await load(path.join(cfg.root, 'router.js')); await mod.evaluate();
  mod.namespace.applyHash();
  route = {tab: state.tab, detail: state.detail, parsed: mod.namespace.parseHash()};
} else {
  const mod = await load(path.join(cfg.root, 'pages/tasks.js')); await mod.evaluate();
  html = mod.namespace.viewTasks({id: 'g1'}, cfg.view || {});
}
console.log(JSON.stringify({tabs: st.namespace.TABS, html, route}));
"""


def run_js(tmp_path, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，不能验证在做的事页面"
    assert JS.is_dir(), "真实前端目录缺失"
    script = tmp_path / "work.mjs"
    script.write_text(CHECK % json.dumps({"root": str(JS), **cfg}), encoding="utf-8")
    result = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_four_group_tabs(tmp_path):
    assert run_js(tmp_path)["tabs"] == [
        {"id": "news", "label": "资讯"}, {"id": "ideas", "label": "构想"},
        {"id": "tasks", "label": "在做的事"}, {"id": "group", "label": "群"},
    ]


def test_empty_work_page_has_one_heading(tmp_path):
    html = run_js(tmp_path)["html"]
    assert "在做的事" in html and "还没有在做的事" in html
    assert html.count("<h1") == 1


def test_tasks_goals_reminders_share_work_page(tmp_path):
    html = run_js(tmp_path, role="group_admin", view={
        "tasks": {"pending": [{"id": "R-1", "title": "整理报名", "age_s": 20}],
                  "list": [{"id": "T-1", "title": "已完成报表", "status": "completed", "undelivered": True}]},
        "goals": {"agent": [{"id": "G-1", "title": "持续盯进展", "body": "<private>", "criteria": [{"done": True}, {"done": False}], "state": "paused"}],
                  "member": [{"id": "M-1", "title": "活动提醒", "who": "阿鲤", "repeat": "daily"}]},
    })["html"]
    for text in ("在做的事", "等你批准", "已完成报表", "做完了但还没发出去", "我在推进", "暂停中", "帮大家记着", "活动提醒"):
        assert text in html
    assert html.index("等你批准") < html.index("全部任务") < html.index("我在推进")
    assert 'data-act="goal" data-id="G-1"' in html
    assert "&lt;private&gt;" in html and "<private>" not in html
    assert html.count("<h1") == 1


def test_goal_only_is_not_reported_as_no_work(tmp_path):
    html = run_js(tmp_path, view={"goals": {"agent": [{"id": "G-1", "title": "持续跟进", "state": "active"}]}})["html"]
    assert "持续跟进" in html and "还没有" not in html


@pytest.mark.parametrize("tab", ["goals", "tasks"])
def test_old_goal_detail_links_open_work_page(tmp_path, tab):
    out = run_js(tmp_path, route=True, hash=f"#/g1/{tab}/G-9")["route"]
    assert out["tab"] == "tasks"
    assert out["detail"] == {"type": "goal", "id": "G-9"}
    assert out["parsed"]["tab"] == "tasks"


def test_tabbar_width_tracks_four_tabs():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "repeat(var(--tab-count, 4), 1fr)" in css
    assert "width: calc((100% - 16px) / var(--tab-count, 4))" in css
    assert 'style.setProperty("--tab-count", TABS.length)' in (JS / "render.js").read_text(encoding="utf-8")
