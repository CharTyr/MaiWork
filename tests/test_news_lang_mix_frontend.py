"""资讯「这一轮怎么找的」里的中文 / 外文来源比例（docs/18 资讯偏中文整改）。"""
import json
import shutil
import subprocess
from pathlib import Path

NEWS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/pages/news.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const doc = { addEventListener: () => {}, querySelectorAll: () => [], contains: () => false, documentElement: {} };
const context = vm.createContext({ console, document: doc, window: { addEventListener: () => {} },
  MutationObserver: class { observe() {} }, location: { origin: 'http://local.test' }, setTimeout });
const fn = () => '';
const stubs = {
  '../state.js': { admin: () => true, gadmin: () => true, state: {} },
  '../util.js': { SVG: {}, dayWord: fn, dur: fn, esc, hhmm: fn, ico: fn, now: () => 0, richText: fn, safeUrl: fn, slotName: fn, tokens: fn, when: fn },
  '../api.js': { agentFish: fn, api: async () => ({}), gview: fn },
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
const mod = new vm.SourceTextModule(source, { context });
await mod.link(link); await mod.evaluate();
console.log(JSON.stringify({ html: mod.namespace.langMix(CFG.funnel) }));
"""


def render(tmp_path, funnel):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证资讯漏斗"
    assert NEWS.is_file(), "资讯页前端文件缺失"
    script = tmp_path / "lang.mjs"
    script.write_text(CHECK % json.dumps({"path": str(NEWS), "funnel": funnel}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def test_shows_zh_and_foreign_counts_per_stage(tmp_path):
    html = render(tmp_path, {
        "query_langs": {"zh": 6, "en": 4},
        "discovered_langs": {"zh": 30, "foreign": 12},
        "kept_langs": {"zh": 3, "foreign": 2},
    })
    assert "搜的词" in html and "中文 6 · 英文 4" in html
    assert "线索" in html and "中文 30 · 外文 12" in html
    assert "上网页" in html and "中文 3 · 外文 2" in html


def test_old_batches_without_lang_fields_show_nothing(tmp_path):
    assert render(tmp_path, {"queries": 10, "discovered": 40}) == ""
    assert render(tmp_path, {"kept_langs": {"zh": 0, "foreign": 0}}) == ""
