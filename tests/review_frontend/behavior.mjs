// Production module executor. DOM/network are boundary doubles, module bodies are untouched.
import { readFile } from 'node:fs/promises';
import { resolve, dirname, relative, sep } from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const ROOT = resolve(import.meta.dirname, '../../maiwork/console/static/js');
// Module keys are always POSIX-style ('pages/tasks.js'); on Windows resolve()
// yields backslashes that would never match the base/real maps.
const relKey = (abs) => relative(ROOT, abs).split(sep).join('/');
const CASE = process.argv[2];
const noop = () => '';
const state = {g:'1', ref:'token', me:{role:'member'}, groups:[{id:'1',token:'token'},{id:'2',token:'two'}], tasks:{}, detail:null, tab:'news', page:null, newsQ:'', newsTopic:'', skew:0};
const ui = {flash:false};
const events = new Map(), nodes = new Map(), timers = new Map(), frames = new Map();
let timerId=0, frameId=0, renderCount=0, taskLoads=0, currentFish=null;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
class Element {
  constructor(id='') { this.id=id; this.dataset={}; this.attrs={}; this.style={setProperty:noop}; this.hidden=false; this.offsetParent={}; this.disabled=false; this.selectionStart=0; this.selectionEnd=0; this.isConnected=true; this.children=[]; this.html=''; this.classes=new Set(); this.classList={add:(v)=>this.classes.add(v),remove:(v)=>this.classes.delete(v),contains:(v)=>this.classes.has(v),toggle:noop}; }
  set innerHTML(s) {this.html=s; if(this.id==='sheet') {const frame=s.match(/<div id="pv-frame"[^>]*>([\s\S]*?)<\/div>/);if(frame)nodes.get('pv-frame').innerHTML=frame[1];} if(this.id==='view') {const m=s.match(/id="news-q"[^>]*value="([^"]*)"/); if(m) { const n=new Element('news-q');n.value=m[1];n.parent=this;nodes.set('news-q',n); document.activeElement=null; }}}
  get innerHTML(){return this.html;}
  addEventListener(){}
  setAttribute(k,v){this.attrs[k]=String(v);}
  removeAttribute(k){delete this.attrs[k];}
  hasAttribute(k){return k in this.attrs;}
  getAttribute(k){return this.attrs[k]??null;}
  focus(){document.activeElement=this;}
  contains(n){return n===this||n?.parent===this||this.children.includes(n);}
  querySelector(){return null;}
  querySelectorAll(){return this.children;}
  matches(s){if(s===':disabled')return this.disabled;return this.id==='news-q'&&s.includes('input');}
  closest(){return null;}
  setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}
}
for(const id of ['app','tabbar','sheet','scrim','side','view','top','rail','pv-frame','toast']) nodes.set(id,new Element(id));
const document = {activeElement:null,hidden:false,documentElement:new Element(),body:new Element(),getElementById:id=>nodes.get(id)||null,contains:n=>!!n?.isConnected,querySelector:()=>null,querySelectorAll:s=>s.includes('fish-swim')&&currentFish?[currentFish]:[],addEventListener:(type,fn)=>{if(!events.has(type))events.set(type,new Set());events.get(type).add(fn);},removeEventListener:(type,fn)=>events.get(type)?.delete(fn)};
const emit=(type,event)=>{for(const fn of [...(events.get(type)||[])])fn(event);};
const flushTimers=()=>{const run=[...timers.values()];timers.clear();for(const fn of run)fn();};
const media={matches:false,addEventListener:(t,fn)=>{media.listener=fn;}};
const location={hash:'#/1/news',search:'',origin:'https://maiwork.test'};
const context = vm.createContext({console,document,location,history:{replaceState:noop},window:{scrollTo:noop,addEventListener:document.addEventListener,scrollY:0},localStorage:{getItem:()=>null,setItem:noop},MutationObserver:class{observe(){}},crypto:{getRandomValues:b=>b.fill(1)},URL,URLSearchParams,Date,Uint8Array,performance:{now:()=>0},matchMedia:()=>media,setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),setInterval:noop,requestAnimationFrame:fn=>{frames.set(++frameId,fn);return frameId;},cancelAnimationFrame:id=>frames.delete(id)});
const base = {
 'state.js':{$:id=>nodes.get(id)||null,state,ui,desktop:{matches:false,addEventListener:noop},gadmin:()=>['admin','group_admin'].includes(state.me?.role),admin:()=>state.me?.role==='admin',isGA:()=>state.me?.role==='group_admin',TABS:[{id:'news',label:'资讯'},{id:'tasks',label:'任务'},{id:'group',label:'群'}],CATS:[],TONES:[],DOT:{},STATUS:{}},
 'util.js':{esc, safeUrl:u=>/^https?:\/\//i.test(u||'')?esc(u):'#',SVG:{},ico:noop,tip:noop,richText:esc,when:String,hhmm:String,dur:String,dayIndex:()=>1,dayWord:String,now:()=>1,slotName:String,calm:()=>false,toast:noop},
 'api.js':{api:async(method,path)=>path==='/api/groups'?[]:path==='/api/me'?{role:'none'}:{ok:true,url:'https://new.example/page'},gview:()=>state.view,grp:()=>state.groups.find(g=>g.id===state.g),gname:g=>g?.name||'测试群',gplat:()=> 'qq',platTag:noop,agentFish:noop,groupRef:g=>g.id},
 'render.js':{render:()=>renderCount++,renderView:()=>renderCount++,renderSide:noop,renderRail:noop,renderTop:noop,renderTabbar:noop},
 'detail.js':{loadTask:async()=>taskLoads++,refreshDetail:noop,detailHTML:()=>'<p>详情</p>'},
 'sheet.js':{openSheet:k=>{state.sheet=k;},sheetChrome:()=>'',closeSheet:()=>{state.sheet=null;},repaintSheet:noop},
 'settings/models.js':{draft:{models:null}},'settings/index.js':{SET_SUBS:[]},
 'router.js':{reboot:async()=>{},syncHash:noop},'onboarding.js':{onb:{open:false}},
};
async function load(real, overrides={}) {
 const realSet=new Set(real), modules=new Map(), names=new Map();
 const sources=new Map();
 for(const rel of real) {
   const src=await readFile(resolve(ROOT,rel),'utf8');sources.set(rel,src);
   for(const match of src.matchAll(/import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["']/g)) {
     const path=relKey(resolve(dirname(resolve(ROOT,rel)),match[2]));
     if(!names.has(path))names.set(path,new Set());
     for(const part of match[1].split(','))names.get(path).add(part.trim().split(/\s+as\s+/)[0]);
   }
 }
 function get(rel) {
  if(modules.has(rel))return modules.get(rel);
  let mod;
  if(realSet.has(rel))mod=new vm.SourceTextModule(sources.get(rel),{context,identifier:resolve(ROOT,rel)});
  else {const values={...(base[rel]||{}),...(overrides[rel]||{})};const keys=[...new Set([...(names.get(rel)||[]),...Object.keys(values)])];mod=new vm.SyntheticModule(keys,function(){for(const k of keys)this.setExport(k,k in values?values[k]:noop);},{context,identifier:resolve(ROOT,rel)});}
  modules.set(rel,mod);return mod;
 }
 const entry=get(real[0]);await entry.link((sp,ref)=>get(relKey(resolve(dirname(ref.identifier),sp))));await entry.evaluate();
 return rel=>get(rel).namespace;
}
const rec={kind:'here.now',text:'成品',state:'已发送',local_url:'/g/token/artifacts/T-1/',url:'https://old.example/page',expired:true};
if(CASE==='r3_detail') {
 state.detail={type:'task',id:'T-1'};state.tasks['T-1']={id:'T-1',status:'completed',delivery:[rec]};
 const ns=await load(['detail.js','util.js']);const html=ns('detail.js').detailHTML();
 assert.match(html,/data-act="preview"/);assert.match(html,/href="\/g\/token\/artifacts\/T-1\/"/);assert.match(html,/外链已过期/);assert.match(html,/data-expired="1"/);
} else if(CASE==='r3_preview') {
 const ns=await load(['preview.js','util.js','sheet.js']);ns('preview.js').openPreview(rec,'T-1');
 const html=ns('preview.js').previewSheet();assert.match(html,/href="\/g\/token\/artifacts\/T-1\/"/);assert.match(html,/preview-republish/);assert.match(nodes.get('pv-frame').innerHTML,/sandbox="allow-scripts"/);
} else if(CASE==='r3_urls') {
 const ns=await load(['preview.js','util.js','sheet.js']);
 for(const url of ['javascript:alert(1)','data:text/html,test','//evil.test','/g/../api/me','/g/token/\\evil','https://ok.test/\nattack']) {
  state.sheet=null;ns('preview.js').openPreview({url},'T-1');assert.equal(state.sheet,null,`unsafe URL accepted: ${url}`);
 }
 for(const url of ['/g/token/artifacts/T-1/','/api/tasks/T-1/artifacts/','https://ok.test/file']) {ns('preview.js').openPreview({url},'T-1');assert.equal(state.sheet,'preview');assert.ok(!ns('preview.js').previewSheet().includes('href="#"'));}
} else if(CASE==='r3_action') {
 const ns=await load(['actions.js','preview.js','util.js','sheet.js']);
 await ns('actions.js').act({dataset:{act:'preview',tid:'T-1',url:rec.url,localUrl:rec.local_url,expired:'1'}},{});
 assert.match(nodes.get('pv-frame').innerHTML,/src="\/g\/token\/artifacts\/T-1\/"/);assert.match(ns('preview.js').previewSheet(),/preview-republish/);
} else if(CASE==='r3_republish') {
 const ns=await load(['preview.js','util.js','sheet.js']);ns('preview.js').openPreview(rec,'T-1');
 await ns('preview.js').actPreview({dataset:{act:'preview-republish',tid:'T-1'}});
 assert.match(nodes.get('pv-frame').innerHTML,/https:\/\/new.example\/page/);assert.equal(taskLoads,1,'task detail must refresh');assert.match(nodes.get('sheet').innerHTML,/https:\/\/new.example\/page/);
} else if(CASE.startsWith('r11_')) {
 state.me.role=CASE==='r11_admin'?'group_admin':'member';
 const ns=await load(['pages/group.js','topiclog.js']);
 const topic_log=[{id:1,ts:200,quiet_s:100,opener:'已开场正文',jev:{stretch_ts:100,ok_p:.8}},{id:2,ts:150,quiet_s:50,opener:'',jev:{stretch_ts:100,ok_p:.4}},{id:3,ts:900,quiet_s:200,opener:'',jev:{stretch_ts:700,ok_p:.2,detail:'跳过原因'}}];
 const html=ns('pages/group.js').viewGroup({id:'1'}, {topic_log});
 assert.match(html,/已开场正文/);
 if(CASE==='r11_admin') {assert.match(html,/1 条没开启成话题/);assert.match(html,/共判断 2 次/);assert.match(html,/verdict/);assert.match(html,/跳过原因/);}
 else {assert.doesNotMatch(html,/跳过原因|阈值|Jev|最高/);assert.doesNotMatch(html,/<details class="disc">/);}
} else if(CASE==='r12_empty'||CASE==='r12_partial') {
 state.newsQ=CASE==='r12_empty'?'未匹配':'匹配';
 const ns=await load(['pages/news.js']);const html=ns('pages/news.js').viewNews({}, {news:[{slot_ts:1,items:[{id:1,title:'匹配资讯'}]},{slot_ts:2,items:[{id:2,title:'其他资讯'}]}]});
 assert.doesNotMatch(html,/本轮无合适资讯/);
 if(CASE==='r12_empty'){assert.match(html,/没有匹配的资讯/);assert.match(html,/data-act="news-clear"/);}else{assert.match(html,/匹配资讯/);assert.doesNotMatch(html,/其他资讯/);assert.equal((html.match(/ 搜集 · /g)||[]).length,1);}
} else if(CASE==='r12_input') {
 await load(['main.js']); const input=new Element('news-q');input.value='zhong';
 emit('compositionstart',{target:input});emit('input',{target:input,isComposing:true});flushTimers();assert.equal(renderCount,0,'must not repaint during composition');
 input.value='中文';emit('compositionend',{target:input});flushTimers();assert.equal(state.newsQ,'中文');assert.equal(renderCount,1);
 input.value='旧群搜索';emit('input',{target:input});state.g='2';state.newsQ='新群草稿';flushTimers();assert.equal(state.newsQ,'新群草稿','old timer may not change new group');
} else if(CASE==='r12_focus') {
 state.view={news:[{slot_ts:1,items:[]}]};
 const ns=await load(['render.js'], {'pages/news.js':{viewNews:()=>`<input id="news-q" value="中文搜索">`}});
 const input=new Element('news-q');input.parent=nodes.get('view');nodes.set('news-q',input);input.value='中文搜索';input.selectionStart=1;input.selectionEnd=3;input.selectionDirection='backward';input.focus();
 ns('render.js').renderView();const next=nodes.get('news-q');assert.equal(document.activeElement,next);assert.equal(next.selectionStart,1);assert.equal(next.selectionEnd,3);assert.equal(next.selectionDirection,'backward');
} else if(CASE==='r12_go'||CASE==='r12_hash') {
 state.me.role='admin';state.newsTopic='旧主题';state.newsQ='旧搜索';const ns=await load(['router.js']);
 if(CASE==='r12_go')ns('router.js').go({g:'2'});else{location.hash='#/2/news';ns('router.js').applyHash();}
 assert.equal(state.g,'2');assert.equal(state.newsTopic,'');assert.equal(state.newsQ,'');
} else if(CASE==='r13_inert') {
 const ns=await load(['sheet.js']);ns('sheet.js').openSheet('detail');assert.ok(nodes.get('app').hasAttribute('inert'));assert.ok(nodes.get('tabbar').hasAttribute('inert'));ns('sheet.js').closeSheet();assert.ok(!nodes.get('tabbar').hasAttribute('inert'));
} else if(CASE==='r13_back'||CASE==='r13_reboot') {
 const ns=await load(['router.js','sheet.js']);state.detail={type:'task',id:'T-1'};ns('sheet.js').openSheet('detail');
 if(CASE==='r13_back'){location.hash='#/1/news';ns('router.js').applyHash();}else await ns('router.js').reboot();
 assert.ok(!nodes.get('app').hasAttribute('inert'));assert.ok(!nodes.get('tabbar').hasAttribute('inert'));assert.equal(state.sheet,null);
} else if(CASE==='r13_preview_back') {
 const ns=await load(['sheet.js','preview.js','util.js']);state.detail={type:'task',id:'T-1'};ns('sheet.js').openSheet('detail');ns('preview.js').openPreview(rec,'T-1');ns('sheet.js').closeSheet();assert.equal(state.sheet,'detail');assert.equal(state.detail.id,'T-1');assert.ok(nodes.get('app').hasAttribute('inert'));ns('sheet.js').closeSheet();assert.equal(state.detail,null);assert.ok(!nodes.get('tabbar').hasAttribute('inert'));
} else if(CASE==='r13_tab') {
 const ns=await load(['sheet.js']);ns('sheet.js').openSheet('detail');const disabled=new Element(),first=new Element(),last=new Element();disabled.disabled=true;nodes.get('sheet').children=[disabled,first,last];document.activeElement=new Element('outside');let prevented=false;emit('keydown',{key:'Tab',shiftKey:false,preventDefault:()=>prevented=true});assert.ok(prevented);assert.equal(document.activeElement,first);emit('keydown',{key:'Tab',shiftKey:true,preventDefault:noop});assert.equal(document.activeElement,last);
} else if(CASE==='r13_motion') {
 const ns=await load(['fish.js']);const svg=ns('fish.js').fishSvg('review');const id=svg.match(/data-fish-id="(\d+)"/)[1];const body=new Element(),bob=new Element(),head=new Element();body.isConnected=true;currentFish={dataset:{fishId:id},querySelector:s=>s==='.fish-body'?body:bob,querySelectorAll:()=>[head]};
 const still=svg.match(/class="fish-body" d="([^"]+)"/)[1];
 const runFrame=t=>{const [id,fn]=frames.entries().next().value;frames.delete(id);fn(t);};runFrame(1000);assert.notEqual(body.getAttribute('d'),still);media.matches=true;media.listener({matches:true});assert.equal(frames.size,0,'reduced motion must cancel queued animation');assert.equal(body.getAttribute('d'),still);assert.equal(bob.getAttribute('transform'),null);assert.equal(head.getAttribute('transform'),null);media.matches=false;media.listener({matches:false});assert.equal(frames.size,1);media.listener({matches:false});assert.equal(frames.size,1,'only one loop may resume');
} else if(CASE==='r13_actions') {
 state.view={news:[{items:[{id:1}]}]};let calm=true;let posts=0;
 const ns=await load(['actions.js','actions_news.js'],{'util.js':{calm:()=>calm},'api.js':{api:async()=>{posts++;return {feedback:{up:1}};}}});
 const button=new Element();button.dataset={act:'fb',kind:'news',id:'1',v:'up'};await ns('actions.js').act(button,{});assert.ok(!button.classes.has('fb-spring'));assert.equal(posts,1);calm=false;await ns('actions.js').act(button,{});assert.ok(button.classes.has('fb-spring'));
 const src=await readFile(resolve(ROOT,'actions.js'),'utf8');assert.ok(src.split('\n').length<880,'actions dispatcher needs safety margin');
} else if(CASE==='s3_republish_ui') {
 state.me.role='admin'; const toasts=[];
 const ns=await load(['preview.js','sheet.js'],{'util.js':{esc,safeAssetUrl:u=>u||'#',toast:(m,err)=>toasts.push([m,!!err])},'api.js':{api:async()=>({ok:false,reason:'throttled'})}});
 ns('preview.js').openPreview({url:'https://ext.example/doc',local_url:'',expired:false},'T-1');
 assert.doesNotMatch(ns('preview.js').previewSheet(),/preview-republish/,'non-herenow external link has no local snapshot to republish');
 ns('preview.js').openPreview(rec,'T-1');
 assert.match(ns('preview.js').previewSheet(),/preview-republish/);
 await ns('preview.js').actPreview({dataset:{act:'preview-republish',tid:'T-1'}});
 assert.deepEqual(toasts,[['操作过于频繁，请稍后再试',true]]);
} else if(CASE==='s4_composing_tab') {
 state.view={news:[],tasks:{list:[]}};state.newsComposing=true;
 const input=new Element('news-q');input.parent=nodes.get('view');nodes.set('news-q',input);input.focus();
 const ns=await load(['render.js'],{'pages/tasks.js':{viewTasks:()=>'<div id="tasks-page">任务页</div>'}});
 state.tab='tasks';ns('render.js').renderView();
 assert.match(nodes.get('view').innerHTML,/任务页/);
} else if(CASE==='s4_desktop_preview_close') {
 const ns=await load(['sheet.js','preview.js','util.js'],{'state.js':{desktop:{matches:true,addEventListener:noop}}});
 state.detail={type:'task',id:'T-1'};ns('sheet.js').openSheet('detail');ns('preview.js').openPreview(rec,'T-1');
 ns('sheet.js').closeSheet();assert.equal(state.sheet,null,'desktop detail lives in main/side pane, not mobile sheet');
} else throw Error(`unknown case ${CASE}`);
console.log(`PASS ${CASE}`);
