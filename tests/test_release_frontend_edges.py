"""真实前端模块：能力暂停不冒充时长、审批唯一入口、引导只设置网页密码。"""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
CHECK = r"""
import fs from 'node:fs'; import path from 'node:path'; import vm from 'node:vm';
const cfg=%s,calls=[];
const state={me:{role:cfg.role||'admin'},g:'g1',groups:[{id:'g1',name:'群甲'}],settings:{},detail:{type:'task',id:'T-1'},
 tasks:{'T-1':{id:'T-1',title:'找图片',status:'paused',criteria:[],paused_reason:{kind:cfg.kind||'capability',text:'工具不足，先别开工',used:5,limit:10}}}};
const fields={'onb-ga-g1':{querySelectorAll:()=>[{dataset:{v:'qq:4321'}}]},'onb-ga-pw-g1':{value:'local-only-password'}};
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const defaults={state,$:id=>fields[id]||null,admin:()=>state.me.role==='admin',gadmin:()=>['admin','group_admin'].includes(state.me.role),
 desktop:{matches:true},esc,SVG:{},DOT:{},STATUS:{paused:'暂停'},dur:n=>String(n||0)+'秒',ico:()=>'',gname:g=>g?.name||'',gview:()=>({tasks:{list:[]}}),
 api:async(method,url,body)=>{calls.push({method,url,body});return {password_set:true,accounts:['qq:4321']};},
 chipEditor:()=>'<div>旧批准名单编辑器</div>',ROWS_COLS:{},AV_SRC:{},PROTOCOLS:{}};
const context=vm.createContext({console,document:{addEventListener(){}},Date,URLSearchParams});
const mods=new Map();async function load(file){if(mods.has(file))return mods.get(file);let src=fs.readFileSync(file,'utf8'),m;
 if(path.relative(cfg.root,file)===cfg.target){
  if(cfg.target==='onboarding.js')src+='\nexport {onbSave};';
  m=new vm.SourceTextModule(src,{context,identifier:file});mods.set(file,m);await m.link((s,p)=>load(path.resolve(path.dirname(p.identifier),s)));
 }else{
  const names=[...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)/gm)].map(x=>x[1]))];
  m=new vm.SyntheticModule(names,function(){for(const n of names)this.setExport(n,defaults[n]??(()=>''));},{context});mods.set(file,m);await m.link(()=>{});
 }return m;}
const m=await load(path.join(cfg.root,cfg.target));await m.evaluate();let html='',error='';
if(cfg.target==='detail.js')html=m.namespace.detailHTML();
else{
 Object.assign(m.namespace.onb,{cfg:{sections:[{fields:[{key:'groups.serve',value:[{group:'qq:g1'}]}]}]},ga:{g1:{password_set:false,accounts:['qq:4321']}}});
 html=m.namespace.onbPane('admins');error=await m.namespace.onbSave('admins');
}
console.log(JSON.stringify({html,calls,error}));
"""


def render_js(tmp_path, target, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，不能验证前端收口"
    f = tmp_path / "edge.mjs"
    f.write_text(CHECK % json.dumps({"root": str(JS), "target": target, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("role", ["admin", "group_admin", "member"])
def test_capability_pause_shows_reason_not_false_time_limit(tmp_path, role):
    html = render_js(tmp_path, "detail.js", role=role)["html"]
    assert "工具不足，先别开工" in html and "为什么停了" in html
    assert "时长上限" not in html and "任务安全网" not in html
    if role != "member":
        assert "继续" in html and "取消" in html


def test_unknown_pause_reason_does_not_invent_time_limit(tmp_path):
    html = render_js(tmp_path, "detail.js", kind="future-kind")["html"]
    assert "时长上限" not in html


def test_onboarding_only_saves_web_password(tmp_path):
    out = render_js(tmp_path, "onboarding.js")
    assert out["error"] == ""
    assert out["calls"] == [{"method": "PUT", "url": "/api/groups/g1/group-admin", "body": {"password": "local-only-password"}}]
    assert "旧批准名单编辑器" not in out["html"] and "群页" in out["html"]
    assert "读不到配置" not in out["html"]


def test_no_duplicate_approval_editor_and_old_autosave():
    assert 'chipEditor("ga-accs"' not in (JS / "sheet.js").read_text(encoding="utf-8")
    assert '"approval.admins"' not in (JS / "onboarding.js").read_text(encoding="utf-8")
    assert 'e.target.dataset.cp' not in (JS / "events.js").read_text(encoding="utf-8")
    css = (JS.parent / "style.css").read_text(encoding="utf-8")
    assert '.gctl-row { grid-template-columns: minmax(0, 1fr) auto; }' in css
