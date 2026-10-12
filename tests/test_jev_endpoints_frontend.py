"""运行真实「快速判断（Jev）服务」前端 JS：渲染 / 换用 / 保存 / 测试 / 删除，无网络。

后端接口：GET /api/settings/jev、PUT /api/settings/jev/use、
PUT|DELETE /api/settings/jev/endpoints/{id}、POST /api/settings/jev/endpoints/{id}/test。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
JS = ROOT / "settings/jev.js"

VIEW = {
    "enabled": True, "timeout_ms": 1200, "use": "openrouter", "use_ok": True,
    "endpoints": [
        {"id": "typesafe", "name": "TypeSafe 官方", "preset": "typesafe", "protocol": "systemone",
         "url": "https://api.typesafe.ai/v1/systemone", "model": "jev-1.13.0", "key_set": True,
         "key_source": "file", "builtin": True},
        {"id": "openrouter", "name": "OpenRouter", "preset": "openrouter", "protocol": "systemone",
         "url": "https://openrouter.ai/api/v1/systemone", "model": "typesafe/jev-1.13", "key_set": True,
         "builtin": False},
        {"id": "jabc12", "name": "我的中转", "preset": "", "protocol": "openai_decisions",
         "url": "https://relay.example.test/v1/decisions", "model": "gpt-6-luna", "key_set": False,
         "builtin": False},
    ],
    "presets": [
        {"id": "typesafe", "name": "TypeSafe 官方", "protocol": "systemone", "url": "https://api.typesafe.ai/v1/systemone",
         "model": "jev-1.13.0", "models": ["jev-1.13.0", "jev-latest"], "docs_url": "https://docs.typesafe.ai/api", "key_url": "", "note": ""},
        {"id": "openrouter", "name": "OpenRouter", "protocol": "systemone", "url": "https://openrouter.ai/api/v1/systemone",
         "model": "typesafe/jev-1.13", "models": ["typesafe/jev-1.13"], "docs_url": "https://openrouter.ai/docs/guides/community/jev",
         "key_url": "https://openrouter.ai/keys", "note": ""},
        {"id": "opencode", "name": "OpenCode Zen", "protocol": "systemone", "url": "https://opencode.ai/zen/v1/systemone",
         "model": "jev-1.13", "models": ["jev-1.13", "jev-1.13-free"], "docs_url": "https://opencode.ai/docs/zen",
         "key_url": "", "note": "jev-1.13-free 限时免费"},
        {"id": "openai", "name": "OpenAI Decisions", "protocol": "openai_decisions", "url": "https://api.openai.com/v1/decisions",
         "model": "gpt-6-luna", "models": ["gpt-6-luna"], "docs_url": "https://developers.openai.com/api/docs/guides/decisions",
         "key_url": "", "note": "OpenAI 自家的判断模型（公测），不是 Jev"},
    ],
}

CHECK = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const CFG = %s;
const source = fs.readFileSync(CFG.path, 'utf8');
const calls = [], fields = {}, toasts = [];
for (const [id, value] of Object.entries(CFG.fields || {})) fields[id] = { value };
fields['jv-err'] = { hidden: true, textContent: '' };
fields['jv-check'] = { textContent: '', style: {} };
const state = { jev: CFG.view, jevEdit: CFG.edit || null, jevPick: !!CFG.pick, mdl: { endpoints: [], models: [{ id: 'm1', model: 'cheap-mini', name: '便宜小模型' }, { id: 'm2', model: 'big-pro', name: '' }] } };
const esc = (s) => String(s ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({ console, setTimeout, confirm: () => true,
  document: { querySelectorAll: () => [], querySelector: () => null, addEventListener: () => {} },
});
const stubs = {
  '../state.js': { state, $: (id) => fields[id] },
  '../util.js': { esc, ico: () => '', SVG: { right: '<svg></svg>' }, tip: (h) => (h ? '<details class="tip">' + h + '</details>' : ''), tipLabel: (f, t, h) => '<label for="' + f + '">' + t + '</label>' + (h ? '<details class="tip">' + h + '</details>' : ''), toast: (t, bad) => toasts.push([t, !!bad]), msText: (ms) => ms + ' 毫秒' },
  '../api.js': { api: async (method, url, body) => {
    calls.push({ method, url, body: body === undefined ? null : body });
    if (url.endsWith('/test')) return CFG.test_reply || { ok: true, ms: 321, error: '' };
    if (url === '/api/settings/config') return { sections: [] };
    return CFG.view;
  } },
  '../sheet.js': { repaintSheet: () => {} },
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
const mod = new vm.SourceTextModule(source, { context });
await mod.link(link); await mod.evaluate();
let handled = null;
if (CFG.action) handled = await mod.namespace.actJev(CFG.action, { disabled: false, dataset: CFG.dataset || {}, textContent: '' });
const html = mod.namespace.jevSection();
console.log(JSON.stringify({ html, calls, handled, toasts, err: fields['jv-err'], check: fields['jv-check'], edit: state.jevEdit, pick: !!state.jevPick }));
"""


def run_js(tmp_path, *, action=None, dataset=None, fields=None, edit=None, view=None, test_reply=None, pick=False):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证快速判断服务的前端"
    assert JS.is_file(), "缺 settings/jev.js"
    script = tmp_path / "jev.mjs"
    cfg = {"path": str(JS), "view": view or VIEW, "action": action, "dataset": dataset or {},
           "fields": fields or {}, "edit": edit, "test_reply": test_reply, "pick": pick}
    script.write_text(CHECK % json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _rows(html):
    """按服务 id 切出每一行（data-jid）。"""
    out = {}
    for part in html.split('data-jid="')[1:]:
        jid = part.split('"', 1)[0]
        out[jid] = part
    return out


def test_render_marks_in_use_and_offers_switch_and_presets(tmp_path):
    html = run_js(tmp_path)["html"]
    rows = _rows(html)
    assert set(rows) >= {"typesafe", "openrouter", "jabc12"}
    assert "正在使用" in rows["openrouter"]
    assert 'data-act="jev-use"' not in rows["openrouter"]
    assert 'data-act="jev-use"' in rows["typesafe"] and 'data-act="jev-use"' in rows["jabc12"]
    # 内置的、正在用的都不能删
    assert 'data-act="jev-del"' not in rows["typesafe"]
    assert 'data-act="jev-del"' not in rows["openrouter"]
    assert 'data-act="jev-del"' in rows["jabc12"]
    # 没填密钥要说出来；接口格式说人话
    assert "未填写" in rows["jabc12"] and "OpenAI Decisions" in rows["jabc12"]
    # 平时不把预设一股脑列出来，只给一个「添加」入口
    assert 'data-act="jev-preset"' not in html
    assert 'data-act="jev-new"' not in html
    assert 'data-act="jev-pick"' in html
    # 默认格式不在每行重复标，只有不一样的才标
    assert "System One" not in rows["openrouter"]


def test_pick_lists_unused_presets_and_custom(tmp_path):
    out = run_js(tmp_path, action="jev-pick")
    html = out["html"]
    assert out["handled"] is True
    # 还没接的预设（opencode / openai）列出来，已经接上的（openrouter / typesafe）不重复
    assert 'data-act="jev-preset" data-id="opencode"' in html
    assert 'data-act="jev-preset" data-id="openai"' in html
    assert 'data-act="jev-preset" data-id="openrouter"' not in html
    assert 'data-act="jev-preset" data-id="typesafe"' not in html
    # 挑选框只列名称和模型；说明进选中后表单标题旁的「ⓘ」
    assert "jev-1.13" in html and "限时免费" not in html
    assert 'data-act="jev-new"' in html and 'data-act="jev-pick-cancel"' in html
    # 打开时「添加」按钮不再重复出现
    assert 'data-act="jev-pick"' not in html


def test_pick_cancel_and_choose_close_picker(tmp_path):
    out = run_js(tmp_path, action="jev-pick-cancel", pick=True)
    assert 'data-act="jev-preset"' not in out["html"] and 'data-act="jev-pick"' in out["html"]
    out = run_js(tmp_path, action="jev-preset", dataset={"id": "opencode"}, pick=True)
    assert 'data-act="jev-preset"' not in out["html"] and out["pick"] is False
    out = run_js(tmp_path, action="jev-new", pick=True)
    assert 'data-act="jev-preset"' not in out["html"] and out["pick"] is False


def _logo_view():
    v = json.loads(json.dumps(VIEW))
    for p in v["presets"]:
        p["logo"] = f"/static/assets/logos/jev/{p['id']}.png"
    return v


def test_rows_use_provider_logo(tmp_path):
    rows = _rows(run_js(tmp_path, view=_logo_view())["html"])
    # 接上的按预设认图标（内置 typesafe 也是）；自定义的没图标就用通用链接图标
    assert 'src="/static/assets/logos/jev/typesafe.png"' in rows["typesafe"]
    assert 'src="/static/assets/logos/jev/openrouter.png"' in rows["openrouter"]
    assert "/logos/jev/" not in rows["jabc12"]
    assert 'alt=""' in rows["openrouter"]  # 名字就在旁边，图标是装饰


def test_picker_shows_provider_logo(tmp_path):
    html = run_js(tmp_path, view=_logo_view(), action="jev-pick")["html"]
    pick = html.split('id="jv-pick"')[1]
    opencode = pick.split('data-id="opencode"')[1].split("</button>")[0]
    assert 'src="/static/assets/logos/jev/opencode.png"' in opencode
    custom = pick.split('data-act="jev-new"')[1].split("</button>")[0]
    assert "/logos/jev/" not in custom


def test_preset_form_shows_provider_logo(tmp_path):
    html = run_js(tmp_path, view=_logo_view(), action="jev-preset", dataset={"id": "opencode"})["html"]
    head = html.split('class="ext-form-h"')[1].split("</div>")[0]
    assert 'src="/static/assets/logos/jev/opencode.png"' in head


def test_all_presets_used_add_goes_straight_to_custom(tmp_path):
    view = dict(VIEW, presets=[p for p in VIEW["presets"] if p["id"] in ("typesafe", "openrouter")])
    html = run_js(tmp_path, view=view)["html"]
    assert 'data-act="jev-new"' in html and 'data-act="jev-pick"' not in html


def test_use_missing_shows_warning(tmp_path):
    view = dict(VIEW, use="gone", use_ok=False)
    html = run_js(tmp_path, view=view)["html"]
    assert "warn-box" in html


def test_switch_calls_use_route(tmp_path):
    out = run_js(tmp_path, action="jev-use", dataset={"id": "typesafe"})
    assert out["handled"] is True
    assert out["calls"][0] == {"method": "PUT", "url": "/api/settings/jev/use", "body": {"id": "typesafe"}}


def test_unknown_action_not_handled(tmp_path):
    assert run_js(tmp_path, action="mdl-ep-save")["handled"] is False


def test_preset_opens_prefilled_form(tmp_path):
    out = run_js(tmp_path, action="jev-preset", dataset={"id": "opencode"})
    edit = out["edit"]
    assert edit["isNew"] is True and edit["draft"]["id"] == "opencode" and edit["draft"]["preset"] == "opencode"
    assert edit["draft"]["url"] == "https://opencode.ai/zen/v1/systemone" and edit["draft"]["model"] == "jev-1.13"
    html = out["html"]
    assert 'id="jv-key"' in html and 'type="password"' in html
    assert "jev-1.13-free" in html  # 可选模型列表
    assert "https://opencode.ai/docs/zen" in html
    head = html.split('class="ext-form-h"')[1].split("</div>")[0]
    assert '<details class="tip">' in head and "jev-1.13-free 限时免费" in head


def _form(**kw):
    base = {"jv-name": "OpenCode Zen", "jv-proto": "systemone", "jv-url": "https://opencode.ai/zen/v1/systemone",
            "jv-model": "jev-1.13-free", "jv-key": "sk-new-secret"}
    base.update(kw)
    return base


NEW_EDIT = {"isNew": True, "id": "opencode", "draft": {"id": "opencode", "preset": "opencode", "protocol": "systemone",
                                                       "url": "https://opencode.ai/zen/v1/systemone", "model": "jev-1.13"}}


def test_save_new_preset_puts_endpoint(tmp_path):
    out = run_js(tmp_path, action="jev-save", fields=_form(), edit=NEW_EDIT)
    put = out["calls"][0]
    assert put["method"] == "PUT" and put["url"] == "/api/settings/jev/endpoints/opencode"
    assert put["body"] == {"name": "OpenCode Zen", "preset": "opencode", "protocol": "systemone",
                           "url": "https://opencode.ai/zen/v1/systemone", "model": "jev-1.13-free", "api_key": "sk-new-secret"}
    assert out["edit"] is None
    assert "sk-new-secret" not in out["html"]


def test_save_existing_blank_key_keeps_old(tmp_path):
    edit = {"isNew": False, "id": "openrouter", "draft": dict(VIEW["endpoints"][1])}
    out = run_js(tmp_path, action="jev-save", edit=edit,
                 fields=_form(**{"jv-name": "OpenRouter", "jv-url": "https://openrouter.ai/api/v1/systemone",
                                 "jv-model": "typesafe/jev-1.13", "jv-key": ""}))
    body = out["calls"][0]["body"]
    assert "api_key" not in body


def test_save_new_without_key_refused(tmp_path):
    out = run_js(tmp_path, action="jev-save", fields=_form(**{"jv-key": ""}), edit=NEW_EDIT)
    assert out["calls"] == [] and out["err"]["hidden"] is False and "密钥" in out["err"]["textContent"]


def test_save_requires_https_and_model(tmp_path):
    out = run_js(tmp_path, action="jev-save", fields=_form(**{"jv-url": "http://opencode.ai/zen/v1/systemone"}), edit=NEW_EDIT)
    assert out["calls"] == [] and "https" in out["err"]["textContent"]
    out = run_js(tmp_path, action="jev-save", fields=_form(**{"jv-model": " "}), edit=NEW_EDIT)
    assert out["calls"] == [] and out["err"]["hidden"] is False


def test_test_button_sends_unsaved_values_and_shows_result(tmp_path):
    out = run_js(tmp_path, action="jev-test", fields=_form(), edit=NEW_EDIT)
    post = out["calls"][0]
    assert post["method"] == "POST" and post["url"] == "/api/settings/jev/endpoints/opencode/test"
    assert post["body"] == {"protocol": "systemone", "url": "https://opencode.ai/zen/v1/systemone",
                            "model": "jev-1.13-free", "api_key": "sk-new-secret"}
    assert "321" in out["check"]["textContent"]


def test_test_failure_shown_in_place(tmp_path):
    out = run_js(tmp_path, action="jev-test", fields=_form(), edit=NEW_EDIT,
                 test_reply={"ok": False, "ms": 80, "error": "http_401: bad key"})
    assert "http_401" in out["check"]["textContent"]


def test_delete_calls_delete(tmp_path):
    out = run_js(tmp_path, action="jev-del", dataset={"id": "jabc12"})
    assert out["calls"][0]["method"] == "DELETE" and out["calls"][0]["url"] == "/api/settings/jev/endpoints/jabc12"


def test_new_custom_gets_valid_id(tmp_path):
    out = run_js(tmp_path, action="jev-new")
    jid = out["edit"]["draft"]["id"]
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", jid) and jid != "typesafe"
    assert out["edit"]["draft"]["preset"] == ""
    assert 'id="jv-proto"' in out["html"] and "OpenAI Decisions" in out["html"]


def test_wired_into_models_page_and_actions():
    models = (ROOT / "settings/models.js").read_text(encoding="utf-8")
    actions = (ROOT / "actions.js").read_text(encoding="utf-8")
    assert 'from "./jev.js"' in models and "jevSection()" in models and "loadJev" in models
    assert "actJev" in actions and "./settings/jev.js" in actions


QJ = {"enabled": True, "model": "m1", "keyword_filter": False, "daily_max": 30, "today": 4}


def test_quick_judge_block_renders_current_values(tmp_path):
    html = run_js(tmp_path, view=dict(VIEW, quick_judge=QJ))["html"]
    assert 'id="qj-enabled"' in html and 'id="qj-model"' in html and 'id="qj-keyword"' in html
    assert 'id="qj-max"' not in html  # 每天上限是内部设置，不放这里
    assert '<option value="m1" selected>' in html and "便宜小模型" in html
    assert '<option value="">' in html  # 跟主模型
    kw_tag = html.split('id="qj-keyword"')[1].split(">")[0]
    assert "checked" not in kw_tag
    en_tag = html.split('id="qj-enabled"')[1].split(">")[0]
    assert "checked" in en_tag
    assert "今日已判断 4 次" in html
    assert 'data-act="qj-save"' in html


def test_quick_judge_block_hidden_without_data(tmp_path):
    assert 'id="qj-enabled"' not in run_js(tmp_path)["html"]


def test_quick_judge_save_writes_config_and_reloads(tmp_path):
    fields = {"qj-model": "m2"}
    out = run_js(tmp_path, action="qj-save", view=dict(VIEW, quick_judge=QJ), fields=fields)
    put = [c for c in out["calls"] if c["method"] == "PUT"]
    assert put and put[0]["url"] == "/api/settings/config"
    body = put[0]["body"]
    assert body["quick_judge.model"] == "m2" and "quick_judge.daily_max" not in body
    assert body["quick_judge.enabled"] is True and body["quick_judge.keyword_filter"] is False
    assert any(c["method"] == "GET" and c["url"] == "/api/settings/jev" for c in out["calls"])
