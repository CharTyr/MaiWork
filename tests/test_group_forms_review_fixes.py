"""审查回归：跨群晚回错误、字段关联与手机命中区；真实 JS，无网络。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_group_ctx_frontend import CHECK as CTX_CHECK, CTX
from test_group_controls_frontend import CHECK as CONTROLS_CHECK, JS


def _node(tmp_path, script):
    node = shutil.which("node")
    assert node, "需要 Node 验证真实群表单模块"
    path = tmp_path / "review.mjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, "--experimental-vm-modules", str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _ctx(tmp_path, cfg, extra=""):
    assert CTX.is_file(), "真实群页模块缺失"
    check = CTX_CHECK.replace("const api = async (method, url, body) => {", "let rejectWrite, resolveWrite;\nconst api = async (method, url, body) => {")
    check = check.replace("calls.push({ method, url, body });", "calls.push({ method, url, body });\n  if (method !== 'GET' && CFG.defer) return new Promise((resolve,reject) => { resolveWrite=resolve; rejectWrite=reject; });")
    check = check.replace("const modules = new Map();", "for (const f of Object.values(fields)) if (f.attrs) { f.removeAttribute = k => delete f.attrs[k]; f.getAttribute = k => f.attrs[k] || null; }\nconst modules = new Map();")
    if extra:
        check = check.split("\nconst handled = [];")[0] + extra
    else:
        check += "\nconsole.log(JSON.stringify({fields,html:slots[0].innerHTML,calls}));\n"
    return _node(tmp_path, check % json.dumps({"path": str(CTX), **cfg}))


@pytest.mark.parametrize("kind", ["rules", "news"])
@pytest.mark.parametrize("transition", ["new-frame", "route-only", "return-new-frame"])
def test_old_save_error_cannot_modify_another_group_form(tmp_path, kind, transition):
    field = "gctx-rules" if kind == "rules" else "gctx-body-news"
    extra = r'''
const action = CFG.kind === 'rules' ? 'gctx-rules-save' : 'gctx-skill-save';
const pending = mod.namespace.actCtx(action, {disabled:false,dataset:{g:'g1',kind:'news',id:'1'}});
state.g = 'g2';
if (CFG.transition !== 'route-only') { mod.namespace.ctxSection('g2'); await new Promise(r=>setImmediate(r)); }
if (CFG.transition === 'return-new-frame') { state.g='g1'; mod.namespace.ctxSection('g1'); await new Promise(r=>setImmediate(r)); }
fields[CFG.field] = {value:'新表单',attrs:{},setAttribute(k,v){this.attrs[k]=v;}};
const box=CFG.kind==='rules'?'gctx-rules-err':'gctx-err-news';
fields[box] = {hidden:true,textContent:''};
fields['gctx-err-rules'] = fields['gctx-rules-err'];
rejectWrite(new Error('旧群请求的错误'));
await pending;
console.log(JSON.stringify({error:fields[box].textContent,attrs:fields[CFG.field].attrs,toasts}));
'''
    out = _ctx(tmp_path, {"kind": kind, "field": field, "transition": transition, "defer": True,
        "edit": "rules" if kind == "rules" else {"kind": "news", "id": "1"}, "fields": {field: "有效正文"}}, extra)
    assert out["error"] == ""
    assert "aria-invalid" not in out["attrs"]
    assert out["toasts"] == []


@pytest.mark.parametrize("bad_field,value", [("name", ""), ("description", ""), ("body", ""),
    ("name", "名" * 41), ("description", "用" * 121), ("body", "步" * 4001)])
def test_skill_validation_marks_actual_invalid_field(tmp_path, bad_field, value):
    names = {"name": "gctx-name-task", "description": "gctx-desc-task", "body": "gctx-body-task"}
    fields = {names["name"]: "整理报名表", names["description"]: "报名时用", names["body"]: "先整理字段"}
    fields[names[bad_field]] = value
    out = _ctx(tmp_path, {"edit": {"kind": "task", "id": "new"}, "fields": fields,
        "actions": [["gctx-skill-save", {"g": "g1", "kind": "task", "id": "new"}]]})
    assert out["calls"] == []
    for key, field in names.items():
        assert out["fields"][field]["attrs"].get("aria-invalid") == ("true" if key == bad_field else None)
    tag = re.search(r'<(?:input|textarea)\b[^>]*\bid="' + names[bad_field] + r'"[^>]*>', out["html"])
    assert tag and "gctx-err-task" in tag[0]


@pytest.mark.parametrize("key,value", [("quiet_hours", "错时段"), ("daily_max", "25"), ("news_card_count", "0")])
def test_push_validation_marks_field_and_associates_error(tmp_path, key, value):
    script = CONTROLS_CHECK + "\nconsole.log(JSON.stringify({html:slots[0].innerHTML,calls}));\n"
    out = _node(tmp_path, script % json.dumps({"path": str(JS / "pages/groupcontrols.js"),
        "actions": ["gctl-push-edit", "gctl-push-save"], "fields": {"gctl-" + key: value}}))
    assert out["calls"] == []
    tag = re.search(r'<(?:input|select)\b[^>]*\bid="gctl-' + key + r'"[^>]*>', out["html"])
    assert tag and 'aria-invalid="true"' in tag[0]
    described = re.search(r'aria-describedby="([^"]+)"', tag[0])
    assert described and "gctl-push-error" in described[1].split()
    assert 'id="gctl-push-error"' in out["html"]
    assert len(re.findall('aria-invalid="true"', out["html"])) == 1


def test_group_context_small_actions_have_44px_minimum_hit_area():
    css = (JS.parent / "style.css").read_text()
    blocks = re.findall(r'([^{}]+)\{([^{}]+)\}', css)
    rules = [body for selectors, body in blocks if ".gctx-slot .btn.small" in selectors]
    assert any(re.search(r'min-height:\s*44px', body) and re.search(r'min-width:\s*44px', body) for body in rules)


def test_readme_does_not_promise_automatic_lock_for_admin_writing():
    plugin = Path(__file__).resolve().parents[1]
    readme = plugin / "README.md"
    if not readme.is_file():
        readme = plugin.parent.parent / "README.md"
    assert readme.is_file(), "发布说明缺失"
    text = readme.read_text()
    assert "你写过的、你锁定的它不会动" not in text
    assert "如果没锁定" in text and "不想让它再改就锁定" in text


def test_skill_retry_clears_old_field_error(tmp_path):
    extra = r'''
const button={dataset:{g:'g1',kind:'task',id:'new'},disabled:false};
await mod.namespace.actCtx('gctx-skill-save',button);
const first={...fields['gctx-name-task'].attrs};
fields['gctx-name-task'].value='有效名字';
await mod.namespace.actCtx('gctx-skill-save',button);
console.log(JSON.stringify({first,name:fields['gctx-name-task'].attrs,desc:fields['gctx-desc-task'].attrs,body:fields['gctx-body-task'].attrs,calls}));
'''
    out = _ctx(tmp_path, {"edit":{"kind":"task","id":"new"}, "fields":{
        "gctx-name-task":"", "gctx-desc-task":"", "gctx-body-task":"有效步骤"}}, extra)
    assert out["first"].get("aria-invalid") == "true"
    assert "aria-invalid" not in out["name"] and "aria-invalid" not in out["body"]
    assert out["desc"].get("aria-invalid") == "true" and out["calls"] == []


def test_current_group_server_failure_is_not_a_field_validation_error(tmp_path):
    out = _ctx(tmp_path, {"fail":True,"edit":{"kind":"news","id":"1"},
        "fields":{"gctx-body-news":"有效步骤"},"actions":[["gctx-skill-save",{"g":"g1","kind":"news","id":"1"}]]})
    assert out["fields"]["gctx-err-news"]["textContent"] == "服务器说不行"
    assert "aria-invalid" not in out["fields"]["gctx-body-news"]["attrs"]


def test_push_retry_clears_old_field_error(tmp_path):
    check=CONTROLS_CHECK.split("\nfor(const a of cfg.actions||[])")[0] + r'''
const button={dataset:{g:'g1'},disabled:false};
await m.namespace.actControls('gctl-push-edit',button);
await m.namespace.actControls('gctl-push-save',button);
const first=slots[0].innerHTML;
fields['gctl-quiet_hours'].value='23:00-08:00';
await m.namespace.actControls('gctl-push-save',button);
console.log(JSON.stringify({first,html:slots[0].innerHTML,calls}));
'''
    out = _node(tmp_path, check % json.dumps({"path":str(JS/"pages/groupcontrols.js"),"fields":{"gctl-quiet_hours":"错时段"}}))
    assert 'aria-invalid="true"' in out["first"]
    assert 'aria-invalid="true"' not in out["html"]
    assert len(out["calls"]) == 1
