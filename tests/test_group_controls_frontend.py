"""真实群控 JS：三种发送共用上限、每群审批、身份隔离与保存时切群。"""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
CHECK = r"""
import fs from 'node:fs'; import vm from 'node:vm';
const cfg=%s,calls=[],slots=[{innerHTML:'',dataset:{g:'g1'}}],listeners={},toasts=[];
const state={g:'g1',me:{role:cfg.role||'admin'},groupControls:null};
const fields={};for(const [id,v]of Object.entries(cfg.fields||{}))fields[id]=typeof v==='boolean'?{checked:v,value:''}:{value:String(v)};
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const push={config:{topics_enabled:true,news_card_enabled:false,news_card_count:3,idea_mention_enabled:false,daily_max:3,quiet_hours:'23:00-08:00'},
  sent_today:1,quota_used:2,recent:[{status:'uncertain',state:'不确定',text:'<script>不明消息',ts:1}]};
const approval={approvers:['qq:4321'],exempt_users:['qq:8765'],exempt_group:false,required:true};
const context=vm.createContext({console,document:{querySelectorAll:()=>slots,addEventListener:(k,f)=>(listeners[k]||=[]).push(f)}});
const stubs={
 '../state.js':{state,$:id=>fields[id]||null,admin:()=>state.me.role==='admin',gadmin:()=>['admin','group_admin'].includes(state.me.role)},
 '../util.js':{esc,SVG:{pen:'[pen]'},when:()=> '今天',toast:(x,b)=>toasts.push([x,!!b])},
 '../api.js':{api:async(method,url,body)=>{calls.push({method,url,body});if(cfg.fail&&method!=='GET')throw new Error('保存失败');
   if(url.endsWith('/push'))return method==='GET'?push:{...push,config:{...push.config,...body}};
   return method==='GET'?approval:body;}},
 './news.js':{loading:()=>'<p>加载中</p>'},
};
const mods=new Map();async function link(s){if(mods.has(s))return mods.get(s);const ex=stubs[s];if(!ex)throw new Error('unexpected '+s);
 const m=new vm.SyntheticModule(Object.keys(ex),function(){for(const[k,v]of Object.entries(ex))this.setExport(k,v);},{context});
 mods.set(s,m);await m.link(link);return m;}
const m=new vm.SourceTextModule(fs.readFileSync(cfg.path,'utf8'),{context});await m.link(link);await m.evaluate();
let initial=m.namespace.controlsSection('g1');await new Promise(r=>setImmediate(r));
const loads=[...calls];calls.length=0;
if(cfg.switchTo)state.g=cfg.switchTo;
for(const a of cfg.actions||[])await m.namespace.actControls(a,{dataset:{g:'g1'},disabled:false});
if(cfg.draftInput)for(const f of listeners.input||[])f({target:{dataset:{gctl:'approvers'}}});
if(cfg.snapshotUpdate)m.namespace.controlsSection('g1',{card_push:{group_push:{...push,sent_today:2,quota_used:3}}});
m.namespace.repaintControls();
console.log(JSON.stringify({initial,html:slots[0].innerHTML,loads,calls,toasts,controls:state.groupControls}));
"""


def run_controls(tmp_path, **cfg):
    node = shutil.which("node")
    assert node, "需要 Node 验证真实群控模块"
    p = JS / "pages/groupcontrols.js"
    assert p.is_file(), "群页统一发送与审批模块尚未实现"
    script = tmp_path / "controls.mjs"
    script.write_text(CHECK % json.dumps({"path": str(p), **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_member_does_not_load_or_show_private_controls(tmp_path):
    out = run_controls(tmp_path, role="member")
    assert out["loads"] == [] and out["initial"] == "" and out["html"] == ""


@pytest.mark.parametrize("role", ["admin", "group_admin"])
def test_controls_load_only_current_group_and_one_cap(tmp_path, role):
    out = run_controls(tmp_path, role=role)
    assert {c["url"] for c in out["loads"]} == {"/api/groups/g1/push", "/api/groups/g1/approval"}
    html = out["html"]
    for text in ("主动发言", "开话题", "资讯卡片", "提构想", "每天最多", "23:00-08:00", "派活审批"):
        assert text in html
    assert "不确定" in html and "&lt;script&gt;" in html and "<script>" not in html
    assert "news_card_daily_max" not in html and "idea_mention_daily_max" not in html


def test_save_push_uses_one_group_endpoint(tmp_path):
    out = run_controls(tmp_path, actions=["gctl-push-edit", "gctl-push-save"], fields={"gctl-daily_max": 5, "gctl-topics_enabled": False})
    assert len(out["calls"]) == 1
    call = out["calls"][0]
    assert call["method"] == "PUT" and call["url"] == "/api/groups/g1/push"
    assert call["body"]["daily_max"] == 5 and call["body"]["topics_enabled"] is False
    assert "news_card_daily_max" not in call["body"]


def test_approval_save_sends_four_valid_fields(tmp_path):
    out = run_controls(tmp_path, actions=["gctl-approval-edit", "gctl-approval-save"], fields={"gctl-approvers": "qq:4321\nqq:9753", "gctl-exempt_users": "qq:8765", "gctl-required": True})
    assert out["calls"] == [{"method": "PUT", "url": "/api/groups/g1/approval", "body": {
        "approvers": ["qq:4321", "qq:9753"], "exempt_users": ["qq:8765"], "exempt_group": False, "required": True}}]


def test_group_admin_cannot_edit_approval_even_by_spoofed_action(tmp_path):
    out = run_controls(tmp_path, role="group_admin", actions=["gctl-approval-edit", "gctl-approval-save"])
    assert out["calls"] == []
    assert 'data-act="gctl-approval-edit"' not in out["html"]


def test_switch_group_prevents_stale_write(tmp_path):
    out = run_controls(tmp_path, switchTo="g2", actions=["gctl-push-edit", "gctl-push-save", "gctl-approval-save"])
    assert out["calls"] == []


def test_invalid_quiet_hours_keeps_form_without_request(tmp_path):
    out = run_controls(tmp_path, actions=["gctl-push-edit", "gctl-push-save"], fields={"gctl-quiet_hours": "乱填"})
    assert out["calls"] == [] and "睡觉时段" in out["html"] and "乱填" in out["html"]


def test_failed_save_keeps_draft_and_inline_error(tmp_path):
    out = run_controls(tmp_path, fail=True, actions=["gctl-push-edit", "gctl-push-save"], fields={"gctl-daily_max": 5})
    assert "保存失败" in out["html"] and 'value="5"' in out["html"]


def test_group_page_and_main_actions_wire_new_controls():
    group = (JS / "pages/group.js").read_text(encoding="utf-8")
    actions = (JS / "actions.js").read_text(encoding="utf-8")
    assert "controlsSection(g.id, v)" in group and "pushBlock" not in group
    assert "actControls" in actions


def test_readonly_counts_follow_current_group_snapshot(tmp_path):
    out = run_controls(tmp_path, snapshotUpdate=True)
    assert "今天发了 2 条" in out["html"] and "用了 3" in out["html"]


def test_error_stays_next_to_the_form_that_failed(tmp_path):
    out = run_controls(tmp_path, actions=["gctl-push-edit", "gctl-push-save"], fields={"gctl-quiet_hours": "错时段"})
    assert out["html"].index("时段写成") < out["html"].index("派活审批")


def test_approval_draft_preserves_blank_lines_while_typing(tmp_path):
    out = run_controls(tmp_path, actions=["gctl-approval-edit"], fields={"gctl-approvers": "qq:4321\n\n"}, draftInput=True)
    assert "qq:4321\n\n</textarea>" in out["html"]
