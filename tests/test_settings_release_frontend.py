"""0.8.0 设置瘦身：高级折叠，用量 / 日志同页且日志摘要不拉完整提示词。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from CharTyr_MaiWork.maiwork import rules
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store
from test_work_page_frontend import run_js as work_js

JS = Path(__file__).resolve().parents[1] / "maiwork/console/static/js"
CHECK = r"""
import fs from 'node:fs'; import path from 'node:path'; import vm from 'node:vm';
const cfg = %s, calls = [], fields = {};
const state = {logs: {tab:'model', failed:false, items:[], open:{}, summary:{today:{calls:9}, last_failure:null}},
  rules: {sections:[{id:'groups',label:'服务群',fields:[{key:'groups.serve',label:'服务群',type:'serve_groups',value:[],advanced:false}]},
    {id:'profile',label:'群画像',fields:[{key:'profile.batch_messages',label:'攒多少条提炼一次',type:'int',value:50,advanced:true}]}]},
  settings: {}, groups: [], usage: {days:7,hist:{days:[],totals:{}},sel:''}};
const esc = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context=vm.createContext({console,URLSearchParams,Date});
const defaults={state,$:id=>fields[id],esc,SVG:{},toast(){},tokens:n=>String(n||0),loading:()=>'<p>加载</p>',
  ROLE_NAMES:{},updateSection:()=>'',
  api:async(method,url)=>{calls.push(url); return url.includes('summary')?state.logs.summary:{items:[]};}};
const real=new Set([cfg.target]),mods=new Map();
async function load(file){
 if(mods.has(file))return mods.get(file);
 const src=fs.readFileSync(file,'utf8'); let m;
 if(real.has(path.relative(cfg.root,file))){
  m=new vm.SourceTextModule(src,{context,identifier:file});mods.set(file,m);
  await m.link((s,p)=>load(path.resolve(path.dirname(p.identifier),s)));
 }else{
  const names=[...new Set([...src.matchAll(/^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)/gm)].map(x=>x[1]))];
  m=new vm.SyntheticModule(names,function(){for(const n of names)this.setExport(n,defaults[n]??(()=>''));},{context});
  mods.set(file,m);await m.link(()=>{});
 }
 return m;
}
const m=await load(path.join(cfg.root,cfg.target));await m.evaluate();let html='',patch=null;
if(cfg.target==='settings/index.js')html=cfg.mode==='overview'?m.namespace.settingsPage():m.namespace.SET_SUBS;
if(cfg.target==='settings/logs.js'){
 if(cfg.mode==='load'){if(m.namespace.loadLogSummary)await m.namespace.loadLogSummary();else await m.namespace.loadLogs();}
 html=m.namespace.logsPage();
}
if(cfg.target==='settings/rules.js'){
 html=m.namespace.rulesPage();fields['c-profile-batch_messages']={value:'73'};patch=m.namespace.readRules();
}
console.log(JSON.stringify({html,calls,patch}));
"""


def settings_js(tmp_path, target, **cfg):
    node = shutil.which("node")
    assert node, "没有 Node，无法验证真实设置模块"
    script = tmp_path / "settings.mjs"
    script.write_text(CHECK % json.dumps({"root": str(JS), "target": target, **cfg}), encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(script)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_one_usage_logs_settings_entry(tmp_path):
    tabs = settings_js(tmp_path, "settings/index.js")["html"]
    assert any(t[0] == "usage" and t[1] == "用量与日志" for t in tabs)
    assert not any(t[0] == "logs" for t in tabs)


def test_log_summary_does_not_fetch_full_calls(tmp_path):
    out = settings_js(tmp_path, "settings/logs.js", mode="load")
    assert out["calls"] == ["/api/logs/summary"]
    assert 'data-act="log-expand"' in out["html"]
    assert 'class="log-bar"' not in out["html"]


def test_advanced_fields_collapsed_and_still_savable(tmp_path):
    out = settings_js(tmp_path, "settings/rules.js")
    advanced = re.search(r'<details class="cfg-advanced"[^>]*>(.*?)</details>', out["html"], re.S)
    assert advanced and "profile-batch_messages" in advanced[1]
    assert " open" not in advanced[0].split(">", 1)[0]
    assert "groups-serve" not in advanced[1]
    assert out["patch"] == {"profile.batch_messages": 73}


def test_old_logs_link_maps_to_combined_page(tmp_path):
    parsed = work_js(tmp_path, route=True, role="admin", hash="#/settings/logs")["route"]["parsed"]
    assert parsed["sub"] == "usage"


def test_config_metadata_has_advanced_flags(tmp_path):
    store = Store(tmp_path / "cfg.db")
    store.migrate()
    try:
        base, _ = load_settings({"storage": {"data_dir": str(tmp_path)}})
        fields = {f["key"]: f for s in rules.config_view(base, store)["sections"] for f in s["fields"]}
        for key in ("profile.batch_messages", "environments.memory_max", "jev.api_url", "feeds.collect_minutes", "console.listen"):
            assert fields[key].get("advanced") is True, key
        for key in ("groups.serve", "feeds.news_slots", "console.password", "environments.railway"):
            assert fields[key].get("advanced") is False, key
        assert "value" not in fields["console.password"]
    finally:
        store.close()


def test_overview_points_to_usage_without_duplicate_counters(tmp_path):
    html = settings_js(tmp_path, "settings/index.js", mode="overview")["html"]
    assert "主模型 · tokens" not in html and "子 agent · tokens" not in html
    assert 'data-sub="usage"' in html


def test_hash_navigation_loads_summary_for_merged_page():
    source = (JS / "main.js").read_text(encoding="utf-8")
    assert 'state.setSub === "usage" ? loadLogSummary() : null' in source
    assert 'state.setSub === "logs"' not in source
