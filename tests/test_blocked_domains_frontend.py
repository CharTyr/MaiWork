"""屏蔽来源按群（docs/18 第一步）的网页侧：设置页按群列名单、解除按钮带群号、调按群接口。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
MODELS = STATIC / "settings/models.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console, location: { origin: 'http://local.test' }, setTimeout,
  document: { querySelectorAll: () => [], querySelector: () => null, addEventListener: () => {} } });
const stubs = {
  '../state.js': { state: {}, $: () => null },
  '../util.js': { esc, ico: () => '', toast: () => {}, when: () => '' },
  '../api.js': { api: async () => ({}) },
  '../sheet.js': { repaintSheet: () => {} }, '../pages/news.js': { loading: () => '' },
  '../router.js': { loadSettings: async () => {} },
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
console.log(JSON.stringify({ html: mod.namespace.feedsSettings(CFG.feeds, CFG.groups) }));
"""


def render(tmp_path, feeds, groups):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证屏蔽来源区"
    assert MODELS.is_file(), "设置前端文件缺失"
    script = tmp_path / "blocked.mjs"
    script.write_text(CHECK % json.dumps({"path": str(MODELS), "feeds": feeds, "groups": groups}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])["html"]


def test_lists_each_group_separately_with_group_id_on_unblock(tmp_path):
    html = render(
        tmp_path,
        {"blocked_domains": {"111": ["spam.com"], "222": ["x.com"]}, "auto_blocked": {"222": ["dead.net"]}},
        [{"id": "111", "name": "甲群"}, {"id": "222", "name": "乙群"}],
    )
    assert html.index("甲群") < html.index("spam.com") < html.index("乙群") < html.index("x.com")
    assert 'data-act="unblock-domain" data-g="111" data-domain="spam.com"' in html
    assert 'data-act="unblock-domain" data-g="222" data-domain="x.com"' in html
    assert "dead.net" in html and "自动屏蔽" in html
    assert 'data-domain="dead.net"' not in html  # 自动屏蔽不给解除按钮


def test_group_without_entries_says_none(tmp_path):
    html = render(tmp_path, {"blocked_domains": {}, "auto_blocked": {}}, [{"id": "111", "name": "甲群"}])
    assert "甲群" in html and "还没有" in html


def test_actions_use_per_group_endpoint_and_no_dead_calls():
    src = (STATIC / "actions.js").read_text(encoding="utf-8")
    assert "/api/feeds/domains" not in src
    assert re.search(r"/api/groups/\$\{encodeURIComponent\(bg\)\}/feeds/domains", src)
    assert "/api/identity/soul/sync" not in src
    news = (STATIC / "pages/news.js").read_text(encoding="utf-8")
    assert "chat_feed" not in news and "递给 MaiBot" not in news
