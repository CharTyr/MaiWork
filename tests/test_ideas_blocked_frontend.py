"""构想页「最近 7 天拦下 N 条 MaiWork 做不到的构想」（docs/18 §八）：只在后端给了 ideas_blocked 且条数>0 时出现。"""
import json
import shutil
import subprocess
from pathlib import Path

IDEAS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/pages/ideas.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console });
const stubs = {
  '../state.js': { TONES: ['#000'], gadmin: () => true, state: {} },
  '../util.js': { SVG: { lock: '<svg/>', check: '', close: '', copy: '', more: '' }, dayWord: () => '今天', esc, ico: () => '' },
  '../api.js': { agentFish: () => '', gview: () => null },
  './news.js': { emptyState: () => '<p>EMPTY</p>', fbButtons: () => '' },
  './group.js': { hash: () => 0 },
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
console.log(JSON.stringify({ html: mod.namespace.viewIdeas({ fresh: false }, CFG.view) }));
"""


def render(tmp_path, view):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证构想页"
    assert IDEAS.is_file(), "构想页前端文件缺失"
    script = tmp_path / "ideas.mjs"
    script.write_text(CHECK % json.dumps({"path": str(IDEAS), "view": view}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


IDEA = {"id": 1, "title": "我可以整理一份清单", "body": "b", "state": "new"}


def test_blocked_row_shows_count_titles_reasons_and_is_admin_only_labeled(tmp_path):
    html = render(tmp_path, {"ideas": [IDEA], "ideas_blocked": {"count": 3, "recent": [
        {"ts": 1, "kind": "group", "title": "帮大家约<开黑>", "reason": "要群友参与"},
        {"ts": 2, "kind": "personal", "title": "帮你找生蚝店", "reason": "要线下"},
    ]}})
    assert "拦下 3 条做不到的" in html
    assert "仅管理员可见" in html
    assert "帮大家约&lt;开黑&gt;" in html and "<开黑>" not in html
    assert "要群友参与" in html and "要线下" in html
    assert "给个人" in html
    # 折叠在副标题下、列表之前
    assert html.index("idea-blocked") < html.index("我可以整理一份清单")


def test_blocked_row_also_shows_when_no_ideas_left(tmp_path):
    html = render(tmp_path, {"ideas": [], "ideas_blocked": {"count": 1, "recent": []}})
    assert "拦下 1 条" in html and "EMPTY" in html


def test_no_row_without_field_or_zero_count(tmp_path):
    assert "idea-blocked" not in render(tmp_path, {"ideas": [IDEA]})
    assert "idea-blocked" not in render(tmp_path, {"ideas": [IDEA], "ideas_blocked": {"count": 0, "recent": []}})
