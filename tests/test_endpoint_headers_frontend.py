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
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const rows = CFG.rows, calls = [], fields = {};
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
fields['ep-headers'] = { querySelectorAll: () => rowEls, innerHTML: '' };
fields['ep-headers-jsonbox'] = { hidden: !CFG.json_mode };
fields['ep-headers-rowsbox'] = { hidden: !!CFG.json_mode };
fields['ep-headers-json'] = { value: CFG.json_text ?? '' };
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
await mod.namespace.actModels(CFG.action, { disabled: false, dataset: CFG.dataset || {} });
console.log(JSON.stringify({ html, calls, error: fields['ep-err'], json_value: fields['ep-headers-json'].value,
  jsonbox_hidden: fields['ep-headers-jsonbox'].hidden, rowsbox_hidden: fields['ep-headers-rowsbox'].hidden,
  rows_html: fields['ep-headers'].innerHTML }));
"""


def run_js(tmp_path, rows, action="mdl-ep-test", *, json_text=None, json_mode=None, dataset=None):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证模型端点表单"
    assert JS.is_file(), "模型端点前端目录缺失"
    script = tmp_path / "headers.mjs"
    cfg = {"path": str(JS), "rows": rows, "action": action, "dataset": dataset or {},
           "json_text": json_text, "json_mode": (json_text is not None) if json_mode is None else json_mode}
    script.write_text(CHECK % json.dumps(cfg), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_advanced_settings_folded_and_values_write_only(tmp_path):
    html = run_js(tmp_path, [])["html"]
    assert "<summary>高级" in html
    assert 'id="ep-headers"' in html and 'id="ep-advanced"' in html
    assert 'id="ep-advanced" open' not in html
    assert "SHOULD-NOT-RENDER" not in html
    assert "X-Route" in html and "Authorization" in html
    assert "已填" in html and "留空" in html
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


# ---------- JSON 填写方式 ----------

import html as _html
import re


def _writes(out):
    return [c for c in out["calls"] if c["method"] in ("POST", "PUT")]


def test_json_box_hidden_by_default_and_prefilled_names_without_values(tmp_path):
    html = run_js(tmp_path, [])["html"]
    assert 'data-act="mdl-header-mode"' in html and 'data-mode="json"' in html and 'data-mode="rows"' in html
    assert re.search(r'id="ep-headers-jsonbox"[^>]*\bhidden\b', html), "JSON 方式默认收起，默认仍是按行填写"
    assert not re.search(r'id="ep-headers-rowsbox"[^>]*\bhidden\b', html)
    m = re.search(r'<textarea id="ep-headers-json"[^>]*>(.*?)</textarea>', html, re.S)
    assert m, "缺 JSON 文本框"
    assert json.loads(_html.unescape(m.group(1))) == {"X-Route": "", "Authorization": ""}, "只列已存名称，值留空（保留原值），不回显"
    assert "SHOULD-NOT-RENDER" not in html


@pytest.mark.parametrize("action", ["mdl-ep-test", "mdl-ep-save"])
def test_json_mode_sends_the_object_and_ignores_rows(tmp_path, action):
    text = '{"Authorization": "", "X-Route": null, "User-Agent": "MaiWork/json"}'
    out = run_js(tmp_path, [["Ignored", "row-value", False]], action, json_text=text)
    assert _writes(out)[0]["body"]["headers"] == {"Authorization": "", "X-Route": "", "User-Agent": "MaiWork/json"}


def test_json_mode_empty_object_clears_and_blank_text_refused(tmp_path):
    out = run_js(tmp_path, [], "mdl-ep-save", json_text=" { } ")
    assert _writes(out)[0]["body"]["headers"] == {}
    out = run_js(tmp_path, [], "mdl-ep-save", json_text="   ")
    assert out["calls"] == [] and out["error"]["hidden"] is False and "{}" in out["error"]["textContent"]


LEAKY = ("secret-snippet-123", "Injected", "opaque-value-xyz")


@pytest.mark.parametrize("text", [
    "not json secret-snippet-123", "[]", '"x"', "null", "123", '{"X-Route": }',
    '{"X-Route": 123}', '{"X-Route": true}', '{"X-Route": {"a": 1}}', '{"X-Route": ["a"]}',
    '{"Authorization": "opaque-value-xyz", "X-Route": 123}',
    '{"New-Header": ""}', '{"New-Header": null}',
    '{"X-A": "1", "x-a": "2"}', '{"Bad Name": "v"}', '{"": "v"}',
    '{"Content-Length": "1"}', '{"TE": "x"}', '{"Via": "x"}',
    '{"X-Route": "\\r\\nInjected: v"}', '{"X-Route": "中文"}',
    json.dumps({f"X-{n}": "v" for n in range(33)}), json.dumps({"X" * 129: "v"}), json.dumps({"X-Route": "v" * 8193}),
])
@pytest.mark.parametrize("action", ["mdl-ep-test", "mdl-ep-save"])
def test_json_mode_invalid_input_stops_request_without_echoing_values(tmp_path, text, action):
    out = run_js(tmp_path, [], action, json_text=text)
    assert out["calls"] == [], "不合格的 JSON 不能提交，也不能悄悄丢项"
    assert out["error"]["hidden"] is False and out["error"]["textContent"]
    for leak in LEAKY:
        assert leak not in out["error"]["textContent"], "错误提示不能带出头值或 JSON 原文"


def test_json_mode_prototype_name_kept(tmp_path):
    out = run_js(tmp_path, [], "mdl-ep-save", json_text='{"__proto__": "opaque-value"}')
    assert _writes(out)[0]["body"]["headers"] == {"__proto__": "opaque-value"}


def test_switch_rows_to_json_carries_typed_values_and_blank_for_saved(tmp_path):
    rows = [["Authorization", "", True], ["User-Agent", "MaiWork/switch", False], ["", "", False]]
    out = run_js(tmp_path, rows, "mdl-header-mode", json_mode=False, dataset={"mode": "json"})
    assert json.loads(out["json_value"]) == {"Authorization": "", "User-Agent": "MaiWork/switch"}
    assert out["jsonbox_hidden"] is False and out["rowsbox_hidden"] is True
    assert out["calls"] == []


def test_switch_rows_to_json_refuses_duplicate_names_and_stays(tmp_path):
    rows = [["X-A", "1", False], ["X-A", "2", False]]
    out = run_js(tmp_path, rows, "mdl-header-mode", json_mode=False, dataset={"mode": "json"})
    assert out["jsonbox_hidden"] is True and out["error"]["hidden"] is False


def test_switch_json_to_rows_invalid_json_stays_in_json_and_keeps_text(tmp_path):
    out = run_js(tmp_path, [], "mdl-header-mode", json_text="oops secret-snippet-123", dataset={"mode": "rows"})
    assert out["jsonbox_hidden"] is False and out["rowsbox_hidden"] is True
    assert out["json_value"] == "oops secret-snippet-123", "切换失败不能丢用户已输入的文字"
    assert out["error"]["hidden"] is False and "secret-snippet-123" not in out["error"]["textContent"]


def test_switch_json_to_rows_builds_rows_saved_names_readonly(tmp_path):
    text = '{"x-route": "", "New-One": "new-private-value"}'
    out = run_js(tmp_path, [], "mdl-header-mode", json_text=text, dataset={"mode": "rows"})
    assert out["jsonbox_hidden"] is True and out["rowsbox_hidden"] is False
    rows = out["rows_html"].split('class="mdl-header-row"')[1:]
    assert len(rows) == 2
    assert 'data-old="1"' in rows[0] and "readonly" in rows[0] and 'value="x-route"' in rows[0], "已存名称（不分大小写）按已保存项处理"
    assert 'data-old="0"' in rows[1] and "readonly" not in rows[1] and 'value="New-One"' in rows[1]
    assert "new-private-value" not in out["rows_html"], "值通过输入框属性赋值，不写进 HTML 文本"


def test_switch_to_same_mode_is_noop(tmp_path):
    out = run_js(tmp_path, [["X-A", "1", False]], "mdl-header-mode", json_mode=False, dataset={"mode": "rows"})
    assert out["jsonbox_hidden"] is True and out["error"]["hidden"] is True
