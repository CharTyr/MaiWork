"""运行真实模型端点 JS：高级请求头渲染/保存/试连，无网络。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js/settings/models.js"
CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(%s, 'utf8');
const rows = %s, calls = [], fields = {};
for (const [id, value] of Object.entries({
  'ep-name': '测试端点', 'ep-proto': 'openai', 'ep-url': 'https://example.test/v1',
  'ep-key': '', 'ep-retries': '0', 'ep-delay': '1', 'ep-conc': '2', 'ep-rpm': '0',
})) fields[id] = { value };
fields['ep-err'] = { hidden: true, textContent: '' };
fields['ep-check'] = { textContent: '', style: {} };
const rowEls = rows.map(([name, value, old]) => ({
  dataset: { old: old ? '1' : '0' },
  querySelector: (sel) => sel.includes('name') ? { value: name } : { value },
}));
fields['ep-headers'] = { querySelectorAll: () => rowEls };
const state = {
  mdl: { endpoints: [], models: [] },
  mdlEdit: { type: 'ep', isNew: true, draft: {
    id: 'e1', protocol: 'openai', key_set: true, header_names: ['X-Route', 'Authorization'],
    headers: { 'X-Route': 'SHOULD-NOT-RENDER' },
  } },
};
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console, location: { origin: 'http://local.test' }, setTimeout,
  document: { querySelectorAll: (s) => s.includes('ep-headers') ? rowEls : [], querySelector: () => null, addEventListener: () => {} },
});
const stubs = {
  '../state.js': { state, $: (id) => fields[id] },
  '../util.js': { esc, ico: () => '', toast: () => {}, when: () => '' },
  '../api.js': { api: async (method, url, body) => {
    calls.push({ method, url, body });
    return url.endsWith('/test') ? { ok: true, models: ['test-model'] }
      : url.endsWith('/endpoints') ? { endpoints: [], models: [] } : {};
  } },
  '../sheet.js': { repaintSheet: () => {} }, '../pages/news.js': { loading: () => '' },
  '../router.js': { loadSettings: async () => {} },
};
const modules = new Map();
async function link(spec) {
  if (!modules.has(spec)) {
    const exports = stubs[spec];
    if (!exports) throw new Error('unexpected import: ' + spec);
    const mod = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
    }, { context });
    modules.set(spec, mod); await mod.link(link);
  }
  return modules.get(spec);
}
const mod = new vm.SourceTextModule(source, { context, importModuleDynamically: async (s) => {
  const m = await link(s); if (m.status !== 'evaluated') await m.evaluate(); return m;
} });
await mod.link(link); await mod.evaluate();
const html = mod.namespace.modelsPage();
await mod.namespace.actModels(%s, { disabled: false, dataset: {} });
console.log(JSON.stringify({ html, calls, error: fields['ep-err'] }));
"""


def run_js(tmp_path, rows, action="mdl-ep-test"):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证模型端点表单"
    assert JS.is_file(), "模型端点前端目录缺失"
    script = tmp_path / "headers.mjs"
    script.write_text(CHECK % (json.dumps(str(JS)), json.dumps(rows), json.dumps(action)), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_advanced_settings_folded_and_values_write_only(tmp_path):
    html = run_js(tmp_path, [])["html"]
    assert "高级设置" in html
    assert 'id="ep-headers"' in html and 'id="ep-advanced"' in html
    assert 'id="ep-advanced" open' not in html
    assert "SHOULD-NOT-RENDER" not in html
    assert "X-Route" in html and "Authorization" in html
    assert "已填写" in html and "留空" in html
    assert 'data-act="mdl-header-add"' in html and 'data-act="mdl-header-del"' in html
    assert 'type="password"' in html


@pytest.mark.parametrize("action", ["mdl-ep-test", "mdl-ep-save"])
def test_test_and_save_use_full_set_keep_blank(tmp_path, action):
    out = run_js(tmp_path, [["Authorization", "", True], ["User-Agent", "MaiWork/advanced", False]], action)
    writes = [c for c in out["calls"] if c["method"] in ("POST", "PUT")]
    assert len(writes) == 1
    assert writes[0]["body"]["headers"] == {"Authorization": "", "User-Agent": "MaiWork/advanced"}
    assert "X-Route" not in writes[0]["body"]["headers"]


def test_empty_list_explicitly_clears(tmp_path):
    assert run_js(tmp_path, [], "mdl-ep-save")["calls"][0]["body"]["headers"] == {}


@pytest.mark.parametrize("rows", [
    [["X-Route", "a", False], ["x-route", "b", False]], [["Bad Name", "v", False]],
    [["X-Route", "\r\nInjected: v", False]], [["X-Route", "", False]],
    [["", "not-silently-dropped", False]], [["X-Route", "中文", False]], [["Content-Length", "1", False]],
    [["TE", "trailers", False]], [["X" * 129, "v", False]], [["X-Route", "v" * 8193, False]],
    [[f"X-{n}", "v", False] for n in range(33)],
])
@pytest.mark.parametrize("action", ["mdl-ep-test", "mdl-ep-save"])
def test_invalid_headers_stop_request(tmp_path, rows, action):
    out = run_js(tmp_path, rows, action)
    assert out["calls"] == [], "不能提交或悄悄跳过有问题的请求头"
    assert out["error"]["hidden"] is False and out["error"]["textContent"]
    for _, value, _ in rows:
        if len(value) > 10:
            assert value not in out["error"]["textContent"]


def test_valid_header_token_that_is_object_prototype_name(tmp_path):
    out = run_js(tmp_path, [["__proto__", "opaque-value", False]])
    assert out["calls"][0]["body"]["headers"] == {"__proto__": "opaque-value"}


def test_add_delete_preserve_other_unsaved_fields():
    text = JS.read_text(encoding="utf-8")
    for action in ("mdl-header-add", "mdl-header-del"):
        assert f'case "{action}"' in text
        block = text.split(f'case "{action}"', 1)[1].split('\n    case ', 1)[0]
        assert "repaintSheet(" not in block
    assert "Object.create(null)" in text
