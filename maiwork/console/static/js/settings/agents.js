// MaiWork 网页 · 专岗：主模型和各专岗（模型、SOUL / AGENTS、小鱼）、每个群的工作记录（只读）。
import { state, $ } from "../state.js";
import { agentFish, agentSeed, api } from "../api.js";
import { fishSvg, fishTraits, parseCustom, customSeed, FISH_SPECIES, FISH_EYES, FISH_COLORS, FISH_COLOR_NAMES, FISH_BASE_RE } from "../fish.js";
import { esc, toast, SVG } from "../util.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";
import { avatarBlock } from "./identity.js";
import { EFFORT_NAMES, loadModels } from "./models.js";


// 每个岗位一条程序生成的小鱼；fish_seed 为空时按岗位名固定，「换一条」只改这个岗位的种子。
const fishOf = (kind, size) => agentFish(kind, size);
const newSeed = () => Math.random().toString(36).slice(2, 10);
const LABELS = { queued: "排队", running: "进行中", returned: "已交回，等验收", accepted: "已验收", rejected: "未采用", failed: "没跑成", cancelled: "已取消" };
const SPECIALISTS = new Set(["news", "idea", "goal"]);
const PHASES = { discover: "撒网搜索", verify: "打开核验", collect: "收集候选", research: "调查", check: "目标进展调查", execute: "任务执行", "idea.research": "构想调查", "goal.check": "目标进展调查", "task.execute": "任务执行" };

const ORDER = ["main", "news", "idea", "goal", "task"];
const TAGS = { main: "主模型", task: "通用执行" };
const LEADS = {
  main: "理解群、定方向、派活、验收；群里说话的口吻也用它的 SOUL",
  news: "找资讯", idea: "出构想", goal: "追目标", task: "按批准的范围做群友派的活",
};
const sortProfiles = (list) => [...list].sort((a, b) => {
  const ia = ORDER.indexOf(a.kind), ib = ORDER.indexOf(b.kind);
  return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
});
const isCustom = (p) => !ORDER.includes(p.kind);
const modelById = (id) => ((state.mdl && state.mdl.models) || []).find((x) => x.id === id);
const modelName = (id) => { const x = modelById(id); return x ? x.name || x.model : id ? `${id}（模型库里没有了）` : ""; };

export function agentsPage() {
  const profiles = sortProfiles((state.agents && state.agents.profiles) || []);
  if (state.agentError && !profiles.length) return `<p class="err" role="alert">${esc(state.agentError)}</p><button class="btn" data-act="agent-reload">重新加载</button>`;
  if (!state.agents) return loading();
  const groups = state.groups || [];
  const snapshot = state.agentSnapshot;
  const noModels = !((state.mdl && state.mdl.models) || []).length;
  return `
    <p class="h-meta">主模型负责想和验收，各专岗负责动手。每个专岗有自己的模型、性格（SOUL.md）和做事规矩（AGENTS.md）。</p>
    ${noModels ? `<div class="warn-box">模型库还是空的，先去「模型」页加端点和模型<button class="btn small" data-act="set-sub" data-sub="models" style="margin-left:8px">去加</button></div>` : ""}
    <div class="h-sub-row"><h2 class="h-sub">专岗</h2><button class="btn small" data-act="agent-new">新建专岗</button></div>
    ${state.agentNew ? newAgentForm() : ""}
    ${profiles.map((p) => state.agentEdit === p.kind ? profileForm(p) : state.agentFishEdit === p.kind ? fishForm(p) : profileRow(p) + (p.kind === "main" ? `<div class="agent-avatar">${avatarBlock()}</div>` : "")).join("")}
    <h2 class="h-sub">这个群的工作记录</h2>
    ${groups.length ? `<label for="agent-group" class="fine">选择服务群</label><select id="agent-group">${groups.map((g) => `<option value="${esc(g.id)}"${state.agentGroup === g.id ? " selected" : ""}>${esc(g.name || `群 ${g.id}`)}</option>`).join("")}</select>` : `<p class="fine">先在设置中添加服务群。</p>`}
    <p class="fine">各专岗在这个群最近做过什么、交接和验收记录。本群规矩和本群做法在群页的「这个群」区改。</p>
    ${state.agentError ? `<p class="err" role="alert">${esc(state.agentError)}</p><button class="btn small" data-act="agent-reload">重新加载</button>` : ""}
    ${groups.length && !snapshot && !state.agentError ? loading() : ""}
    ${snapshot && snapshot.group_id === state.agentGroup ? (snapshot.agents || []).filter((a) => a.kind !== "main").map(journalSection).join("") : ""}`;
}

function newAgentForm() {
  return `<div class="login ext-form">
    <div class="ext-form-h">新建专岗</div>
    <label for="agent-new-title">专岗名称</label><input id="agent-new-title" maxlength="40" placeholder="比如：写代码的、做表格的" />
    <p class="fine">新专岗不会自己定时跑。建好后在「主模型」的 AGENTS.md 里写清楚什么时候该派活给它，主模型会自己判断。</p>
    <div class="actions"><button class="btn primary" data-act="agent-create">建好，接着设置</button><button class="btn" data-act="agent-new-cancel">取消</button></div>
  </div>`;
}

function profileRow(p) {
  const tag = TAGS[p.kind] || (isCustom(p) ? "自定义" : "");
  const fresh = state.agentFishNew === p.kind;
  const off = p.kind !== "main" && p.enabled === false;
  const model = p.model
    ? `${esc(modelName(p.model))}${p.effort ? ` · 思考${esc(EFFORT_NAMES[p.effort] || p.effort)}` : ""}${p.backup ? ` · 备用 ${esc(modelName(p.backup))}` : ""}`
    : `<b>还没选模型</b>${p.kind === "main" ? "（MaiWork 暂时不会工作）" : "（先用主模型的）"}`;
  return `<div class="set-row agent-row"><span class="agent-fish${fresh ? " is-new" : ""}" data-fish="${esc(p.kind)}">${fishOf(p.kind, 48)}</span><div>
    <div class="set-name">${esc(p.title)}${tag ? `<span class="tag">${esc(tag)}</span>` : ""}</div>
    <div class="set-text">${esc(LEADS[p.kind] || "主模型按它 AGENTS.md 里写的判断什么时候派活给它")}</div>
    <div class="set-text ext-st"><span class="dot ${off ? "" : "ok"}"></span>${off ? "已停用 · " : ""}${model}</div>
    </div><div class="agent-row-acts"><button class="btn small ghost" data-act="agent-fish-reroll" data-kind="${esc(p.kind)}" title="随机换一条小鱼">换一条</button><button class="btn small ghost" data-act="agent-fish-custom" data-kind="${esc(p.kind)}" title="自己挑鱼种、颜色和眼神">定制</button><button class="btn small" data-act="agent-profile-edit" data-kind="${esc(p.kind)}">设置</button></div></div>`;
}

function modelOptions(value, emptyLabel) {
  const list = (state.mdl && state.mdl.models) || [];
  const eps = (state.mdl && state.mdl.endpoints) || [];
  const epName = (id) => { const e = eps.find((x) => x.id === id); return e ? e.name || e.id : id; };
  let html = `<option value="">${esc(emptyLabel)}</option>` + list.map((x) => `<option value="${esc(x.id)}"${x.id === value ? " selected" : ""}>${esc(x.name || x.model)} · ${esc(epName(x.endpoint))}</option>`).join("");
  if (value && !modelById(value)) html += `<option value="${esc(value)}" selected>${esc(value)}（模型库里没有了）</option>`;
  return html;
}

function effortOptions(modelId, value) {
  const x = modelById(modelId);
  const list = (x && x.efforts) || [];
  if (!list.length) return `<option value="">这个模型不调思考强度</option>`;
  return `<option value="">用模型默认的</option>` + list.map((e) => `<option value="${e}"${e === value ? " selected" : ""}>${esc(EFFORT_NAMES[e] || e)}（${e}）</option>`).join("");
}

function docBlock(kind, which, title, lead, item, limit, extra) {
  const text = (item && item.text) || "";
  return `<div class="agent-doc">
    <div class="h-sub-row"><label for="agent-doc-${which}" class="set-name" style="margin:0">${title}</label>${extra || ""}</div>
    <p class="fine" style="margin:2px 0 6px">${lead}</p>
    <textarea id="agent-doc-${which}" class="mono-area" rows="10" spellcheck="false" data-limit="${limit || 16384}">${esc(text)}</textarea>
  </div>`;
}

function profileForm(p) {
  const selected = new Set(p.skills || []);
  const catalog = state.agentSkills || [];
  const names = [...new Set([...catalog.map((k) => k.name), ...selected])];
  const docs = (state.agentDocs || {})[p.kind];
  const main = p.kind === "main";
  const lim = (docs && docs.limits) || {};
  return `<div class="login ext-form agent-form">
    <div class="ext-form-h"><span class="agent-fish sm">${fishOf(p.kind, 28)}</span>设置「${esc(p.title)}」</div>
    <label for="agent-title">名称</label><input id="agent-title" maxlength="40" value="${esc(p.title)}" />
    <h3 class="set-name" style="margin-top:14px">模型</h3>
    <label for="agent-model">用哪个模型</label><select id="agent-model">${modelOptions(p.model, main ? "选一个模型" : "跟主模型一样")}</select>
    <label for="agent-effort">思考强度</label><select id="agent-effort">${effortOptions(p.model, p.effort)}</select>
    <label for="agent-backup">出错时换用</label><select id="agent-backup">${modelOptions(p.backup, "不用备用")}</select>
    ${main ? "" : `<label for="agent-escalate">做不动时换用</label><select id="agent-escalate">${modelOptions(p.escalate, "用主模型的")}</select>
    <p class="fine">派给它的活被打回两次，第三次换这个模型接着改（带着前面做过的）。和现在用的是同一个，就不再试第三次。</p>`}
    ${main ? "" : `<label class="chk"><input id="agent-enabled" type="checkbox"${p.enabled !== false ? " checked" : ""} />启用这个专岗</label>`}
    ${main ? "" : `<label>可用 skill</label><div id="agent-skills">${names.map((name) => {
      const k = catalog.find((x) => x.name === name);
      return `<label class="chk"><input type="checkbox" value="${esc(name)}"${selected.has(name) ? " checked" : ""} />${esc(name)}${k && k.enabled === false ? `<span class="fine">（当前停用）</span>` : ""}</label>`;
    }).join("") || `<p class="fine">还没有可选择的 skill。</p>`}</div>`}
    ${!docs ? loading() : `
      ${docBlock(p.kind, "soul", "SOUL.md", "性格和说话的口吻" + (docs.soul && docs.soul.synced_from_maibot ? " · 已和 MaiBot 同步" : ""), docs.soul, lim.soul, `<button type="button" class="btn small" data-act="agent-soul-sync" data-kind="${esc(p.kind)}">从 MaiBot 同步</button>`)}
      ${docBlock(p.kind, "agents", "AGENTS.md", main ? "做事的规矩；新建的专岗写在这里：什么时候该派活给它" : "这个专岗做事的规矩和偏好", docs.agents, lim.agents, `<button type="button" class="btn small ghost" data-act="agent-agents-reset" data-kind="${esc(p.kind)}">恢复默认</button>`)}`}
    <p class="fine">工具权限由程序限定，改这里不能扩大权限。</p>
    <p class="err" id="agent-err" hidden></p>
    <div class="actions"><button class="btn primary" data-act="agent-profile-save" data-kind="${esc(p.kind)}">保存</button><button class="btn" data-act="agent-profile-cancel">取消</button>${isCustom(p) ? `<button class="btn ghost danger" data-act="agent-delete" data-kind="${esc(p.kind)}">删除这个专岗</button>` : ""}</div>
  </div>`;
}

// 小鱼定制：挑鱼种 / 颜色 / 眼神，边挑边看；保存时只写 fish_seed（定制种子，见 fish.js）
function fishDraftFor(kind) {
  const seed = agentSeed(kind);
  const c = parseCustom(seed);
  if (c) return { species: c.species, color: c.color, eyes: c.eyes, base: c.base };
  // 没定制过：按现在这条鱼填好，原样保存不变样；老种子放不进底子就换个新底子
  const base = FISH_BASE_RE.test(seed) ? seed : newSeed();
  const t = fishTraits(`agent:${kind}:${base}`);
  return { species: t.species, color: Math.max(0, FISH_COLORS.indexOf(t.color)), eyes: t.eyes, base };
}
const draftSeed = (kind, d, over = {}) => `agent:${kind}:${customSeed({ ...d, ...over })}`;

function fishForm(p) {
  const kind = p.kind;
  const d = state.agentFishDraft || fishDraftFor(kind);
  const pick = (k, v, on, inner, label, extra = "") => `<button type="button" class="${k === "color" ? "fish-swatch" : "fish-opt"}" data-act="agent-fish-pick" data-kind="${esc(kind)}" data-k="${k}" data-v="${esc(String(v))}" aria-pressed="${on ? "true" : "false"}" aria-label="${esc(label)}" title="${esc(label)}"${extra}>${inner}</button>`;
  const species = Object.entries(FISH_SPECIES).map(([k, name]) => pick("species", k, d.species === k, `${fishSvg(draftSeed(kind, d, { species: k }), { size: 40, still: true })}<span>${esc(name)}</span>`, name)).join("");
  const colors = FISH_COLORS.map((c, i) => pick("color", i, d.color === i, "", FISH_COLOR_NAMES[i], ` style="--c:${c}"`)).join("");
  const eyes = Object.entries(FISH_EYES).map(([k, name]) => pick("eyes", k, d.eyes === k, `${fishSvg(draftSeed(kind, d, { eyes: k }), { size: 40, still: true })}<span>${esc(name)}</span>`, name)).join("");
  return `<div class="login ext-form fish-form" data-kind="${esc(kind)}">
    <div class="ext-form-h">给「${esc(p.title)}」挑一条小鱼</div>
    <div class="fish-preview">${fishSvg(draftSeed(kind, d), { size: 96 })}<div><div class="set-name">${esc(FISH_SPECIES[d.species] || "")}</div><div class="fine">${esc(FISH_COLOR_NAMES[d.color] || "")} · ${esc(FISH_EYES[d.eyes] || "")}</div></div></div>
    <label>鱼种</label><div class="fish-opts" role="group" aria-label="鱼种">${species}</div>
    <label>颜色</label><div class="fish-swatches" role="group" aria-label="颜色">${colors}</div>
    <label>眼神</label><div class="fish-opts" role="group" aria-label="眼神">${eyes}</div>
    <div class="actions"><button class="btn primary" data-act="agent-fish-save" data-kind="${esc(kind)}">保存小鱼</button><button class="btn" data-act="agent-fish-cancel">取消</button><button class="btn ghost" data-act="agent-fish-shape" data-kind="${esc(kind)}" title="鱼种、颜色、眼神不变，鳍和姿态换一下">换个样子</button></div>
  </div>`;
}

// 只重画定制框，焦点留在刚点的那个选项上
function repaintFishForm(kind, focusSel) {
  const box = document.querySelector(`.fish-form[data-kind="${CSS.escape(kind)}"]`);
  const p = ((state.agents && state.agents.profiles) || []).find((x) => x.kind === kind);
  if (!box || !p) { repaintSheet(); return; }
  box.outerHTML = fishForm(p);
  if (focusSel) { const el = document.querySelector(focusSel); if (el) el.focus(); }
}

// 这个群的工作记录（只读）：最近做过的 + 交接。本群规矩和本群做法搬到了群页「这个群」区（docs/17 §八.4）
function journalSection(a) {
  const learned = a.learned || [];
  const handoffs = a.recent_handoffs || [];
  return `<section class="agent-journal">
    <h3 class="set-name"><span class="agent-fish sm">${fishOf(a.kind, 30)}</span>${esc(a.title)}</h3>
    ${a.kind === "task" ? "" : `<details><summary>最近做过的 · ${learned.length}</summary>
      <p class="fine">只用来提醒专岗别重复做同样的事。</p>
      ${learned.map((m) => `<div class="agent-entry"><p>${esc(m.text || "")}</p>${referenceLinks(m.refs)}${m.source_id ? `<span class="fine">依据：${esc(m.source_id)}</span>` : ""}</div>`).join("") || `<p class="fine">还没有。这个专岗完成工作并经主流程验收后，会在这里留下记录。</p>`}
    </details>`}
    <details><summary>最近的交接 · ${handoffs.length}</summary>
      ${handoffs.map(handoffEntry).join("") || `<p class="fine">还没有交接记录。${a.kind === "task" ? "获批任务开始执行后会显示在这里。" : "到现有时段或收到工作请求时，专岗才会启动，不会一直空转。"}</p>`}
    </details>
  </section>`;
}

function referenceLinks(refs) {
  return `<div class="agent-refs">${(refs || []).map((ref) => {
    const s = String(ref);
    return /^https?:\/\//i.test(s) ? `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer">查看依据 ↗</a>` : `<span class="fine">${esc(s)}</span>`;
  }).join(" · ")}</div>`;
}

function handoffEntry(h) {
  const created = h.created ? new Date(h.created * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  return `<details class="agent-entry"><summary><span class="tag">${esc(LABELS[h.status] || h.status || "")}</span>${esc(PHASES[h.phase] || h.phase || h.task_id || "工作回合")}<span class="fine"> ${esc(created)}</span></summary>
    <p>${esc(h.summary || "暂未交回结果")}</p>
    ${h.review ? `<p class="fine">验收：${esc(typeof h.review === "string" ? h.review : JSON.stringify(h.review))}</p>` : ""}
    <p class="fine">本次要求：${esc(h.brief || "")}</p>
    ${h.criteria ? `<p class="fine">验收要求：${esc(typeof h.criteria === "string" ? h.criteria : JSON.stringify(h.criteria))}</p>` : ""}
    ${referenceLinks(h.evidence)}
    ${(h.tools || []).length ? `<p class="fine">本次工具：${h.tools.map(esc).join("、")}</p>` : ""}
    ${(h.skills || []).length ? `<p class="fine">本次 skill：${h.skills.map(esc).join("、")}</p>` : ""}
    <p class="fine">交接编号：${esc(h.id || "")}${h.parent_id ? ` · 上级：${esc(h.parent_id)}` : ""}</p>
  </details>`;
}

export async function loadAgents() {
  state.agentError = "";
  try {
    const [agents, extensions] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/extensions"), state.mdl ? null : loadModels()]);
    state.agents = agents;
    state.agentSkills = extensions.skills || [];
    const groups = state.groups || [];
    if (!groups.some((g) => g.id === state.agentGroup)) state.agentGroup = groups.some((g) => g.id === state.g) ? state.g : groups[0] && groups[0].id;
    if (state.agentGroup) await loadAgentGroup(state.agentGroup);
  } catch (err) {
    state.agentError = err.message;
  }
}

async function loadAgentGroup(gid) {
  const seq = (state.agentLoadSeq || 0) + 1;
  state.agentLoadSeq = seq;
  state.agentGroup = gid;
  state.agentSnapshot = null;
  state.agentError = "";
  repaintSheet();
  try {
    const snapshot = await api("GET", `/api/groups/${encodeURIComponent(gid)}/agents`);
    if (seq === state.agentLoadSeq && gid === state.agentGroup) state.agentSnapshot = snapshot;
  } catch (err) {
    if (seq === state.agentLoadSeq) state.agentError = err.message;
  }
  repaintSheet();
}

document.addEventListener("change", (event) => {
  if (event.target.id === "agent-group") loadAgentGroup(event.target.value);
  // 换了模型：思考强度的选项跟着换（只能选这个模型支持的）
  if (event.target.id === "agent-model" && $("agent-effort")) $("agent-effort").innerHTML = effortOptions(event.target.value, $("agent-effort").value);
});

// 各专岗的 SOUL.md / AGENTS.md（打开设置时才拉）
async function loadDocs(kind) {
  try {
    const d = await api("GET", `/api/agents/${encodeURIComponent(kind)}/docs`);
    state.agentDocs = { ...(state.agentDocs || {}), [kind]: d };
  } catch (err) {
    state.agentDocs = { ...(state.agentDocs || {}), [kind]: { soul: { text: "" }, agents: { text: "" }, limits: {}, error: err.message } };
    toast(err.message, true);
  }
}

function setDocText(kind, which, item) {
  const docs = (state.agentDocs || {})[kind];
  if (docs) docs[which] = item || { text: "" };
  const box = $(`agent-doc-${which}`);
  if (box) box.value = (item && item.text) || "";
}

export async function actAgents(action, el) {
  switch (action) {
    case "agent-profile-edit": {
      const kind = el.dataset.kind;
      state.agentEdit = kind; state.agentFishEdit = null; state.agentFishDraft = null; state.agentNew = false;
      repaintSheet();
      await loadDocs(kind);
      if (state.agentEdit === kind) repaintSheet();
      return true;
    }
    case "agent-profile-cancel": state.agentEdit = null; repaintSheet(); return true;
    case "agent-new": state.agentNew = true; state.agentEdit = null; repaintSheet(); setTimeout(() => $("agent-new-title") && $("agent-new-title").focus(), 0); return true;
    case "agent-new-cancel": state.agentNew = false; repaintSheet(); return true;
    case "agent-create": {
      const title = ($("agent-new-title") && $("agent-new-title").value.trim()) || "";
      if (!title) return toast("给新专岗起个名字", true), true;
      el.disabled = true;
      try {
        const p = await api("POST", "/api/agents", { title });
        state.agentNew = false;
        await loadAgents();
        state.agentEdit = p.kind;
        repaintSheet();
        await loadDocs(p.kind);
        repaintSheet();
        toast("建好了。记得在「主模型」的 AGENTS.md 里写清楚什么时候派活给它");
      } catch (err) { el.disabled = false; toast(err.message, true); }
      return true;
    }
    case "agent-delete": {
      const kind = el.dataset.kind;
      const p = ((state.agents && state.agents.profiles) || []).find((x) => x.kind === kind);
      if (!p || !confirm(`删掉专岗「${p.title}」？它不会再被派活；SOUL.md 和 AGENTS.md 会挪进回收目录，以前的交接记录留着备查。`)) return true;
      try {
        await api("DELETE", `/api/agents/${encodeURIComponent(kind)}`);
        state.agentEdit = null;
        await loadAgents(); repaintSheet(); toast("删掉了。主模型的 AGENTS.md 里如果还提到它，记得删掉那几句");
      } catch (err) { toast(err.message, true); }
      return true;
    }
    case "agent-soul-sync": {
      const kind = el.dataset.kind;
      el.disabled = true;
      try {
        const r = await api("POST", `/api/agents/${encodeURIComponent(kind)}/docs/soul/sync`, {});
        setDocText(kind, "soul", r);
        toast(r && r.persona_missing
          ? (r.text ? "没读到 MaiBot 的人格设置，SOUL.md 没改" : "没读到 MaiBot 的人格设置，SOUL.md 先留空")
          : "已从 MaiBot 同步（直接生效）");
      } catch (err) { toast(err.message, true); }
      finally { el.disabled = false; }
      return true;
    }
    case "agent-agents-reset": {
      const kind = el.dataset.kind;
      if (!confirm("把 AGENTS.md 换回默认的？你改过的内容会被覆盖。")) return true;
      try {
        const r = await api("POST", `/api/agents/${encodeURIComponent(kind)}/docs/agents/reset`, {});
        setDocText(kind, "agents", r);
        toast("换回默认的了");
      } catch (err) { toast(err.message, true); }
      return true;
    }
    case "agent-reload": await loadAgents(); repaintSheet(); return true;
    case "agent-fish-custom": {
      state.agentEdit = null;
      state.agentFishEdit = el.dataset.kind;
      state.agentFishDraft = fishDraftFor(el.dataset.kind);
      repaintSheet();
      const first = document.querySelector('.fish-form [aria-pressed="true"]');
      if (first) first.focus();
      return true;
    }
    case "agent-fish-cancel": state.agentFishEdit = null; state.agentFishDraft = null; repaintSheet(); return true;
    case "agent-fish-pick": {
      const d = state.agentFishDraft;
      if (!d) return true;
      const { k, v, kind } = el.dataset;
      d[k] = k === "color" ? Number(v) : v;
      repaintFishForm(kind, `.fish-form [data-k="${k}"][data-v="${CSS.escape(v)}"]`);
      return true;
    }
    case "agent-fish-shape": {
      const d = state.agentFishDraft;
      if (!d) return true;
      d.base = newSeed();
      repaintFishForm(el.dataset.kind, '.fish-form [data-act="agent-fish-shape"]');
      return true;
    }
    case "agent-fish-save": {
      const kind = el.dataset.kind;
      const p = ((state.agents && state.agents.profiles) || []).find((x) => x.kind === kind);
      const d = state.agentFishDraft;
      if (!p || !d) return true;
      const seed = customSeed(d);
      el.disabled = true;
      try {
        const saved = await api("PUT", `/api/agents/${encodeURIComponent(kind)}`, { fish_seed: seed });
        p.fish_seed = saved && typeof saved.fish_seed === "string" ? saved.fish_seed : seed;
        state.agentFishEdit = null; state.agentFishDraft = null;
        state.agentFishNew = kind;
        repaintSheet();
        setTimeout(() => { if (state.agentFishNew === kind) state.agentFishNew = null; }, 700);
        toast("小鱼换好了，各页面标题旁的也跟着变");
      } catch (err) { el.disabled = false; toast(err.message, true); }
      return true;
    }
    case "agent-fish-reroll": {
      // 先换上新鱼（乐观更新），保存失败再换回来
      const kind = el.dataset.kind;
      const p = ((state.agents && state.agents.profiles) || []).find((x) => x.kind === kind);
      if (!p) return true;
      const old = p.fish_seed || "";
      p.fish_seed = newSeed();
      state.agentFishNew = kind;
      repaintSheet();
      setTimeout(() => { if (state.agentFishNew === kind) state.agentFishNew = null; }, 700);
      try {
        const saved = await api("PUT", `/api/agents/${encodeURIComponent(kind)}`, { fish_seed: p.fish_seed });
        if (saved && typeof saved.fish_seed === "string") p.fish_seed = saved.fish_seed;
      } catch (err) {
        p.fish_seed = old;
        state.agentFishNew = null;
        repaintSheet();
        toast(err.message, true);
      }
      return true;
    }
    case "agent-profile-save": {
      const kind = el.dataset.kind;
      const err = $("agent-err");
      const fail = (text) => { if (err) { err.textContent = text; err.hidden = false; } else toast(text, true); };
      const body = { title: $("agent-title").value.trim(), model: $("agent-model").value, effort: $("agent-effort").value, backup: $("agent-backup").value };
      if (!body.title) return fail("名称不能空着。"), true;
      if (kind === "main" && !body.model) return fail("主模型一定要选一个模型。"), true;
      if (body.backup && body.backup === body.model) return fail("备用模型不能和原来的一样。"), true;
      if ($("agent-escalate")) body.escalate = $("agent-escalate").value;
      if ($("agent-enabled")) body.enabled = $("agent-enabled").checked;
      if ($("agent-skills")) body.skills = [...document.querySelectorAll("#agent-skills input:checked")].map((x) => x.value);
      const docs = (state.agentDocs || {})[kind];
      const writes = [];
      for (const which of ["soul", "agents"]) {
        const box = $(`agent-doc-${which}`);
        if (!box || !docs) continue;
        const text = box.value;
        const limit = Number(box.dataset.limit) || 16384;
        if (new Blob([text]).size > limit) return fail(`${which === "soul" ? "SOUL.md" : "AGENTS.md"} 太长了，最多 ${limit} 字节。`), true;
        if (text !== ((docs[which] && docs[which].text) || "")) writes.push([which, text]);
      }
      el.disabled = true;
      try {
        await api("PUT", `/api/agents/${encodeURIComponent(kind)}`, body);
        for (const [which, text] of writes) await api("PUT", `/api/agents/${encodeURIComponent(kind)}/docs/${which}`, { text });
        state.agentEdit = null;
        if (state.agentDocs) delete state.agentDocs[kind];
        await loadAgents();
        const { loadSettings } = await import("../router.js");
        await loadSettings().catch(() => null);
        repaintSheet(); toast("保存好了，下次工作时生效");
      } catch (e) { el.disabled = false; fail(e.message); }
      return true;
    }
    default: return false;
  }
}
