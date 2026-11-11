// MaiWork 网页 · 专岗：主模型和各专岗（模型、SOUL / AGENTS、小鱼）、每个群的工作记录（只读）。
import { state, $ } from "../state.js";
import { agentFish, agentSeed, api } from "../api.js";
import { fishSvg, fishTraits, parseCustom, customSeed, FISH_SPECIES, FISH_EYES, FISH_COLORS, FISH_COLOR_NAMES, FISH_BASE_RE } from "../fish.js";
import { esc, ico, tip, toast, SVG, gface, safeUrl, when } from "../util.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";
import { avatarBlock } from "./identity.js";
import { EFFORT_NAMES, loadModels } from "./models.js";
import { confirmExecTools, execConfirmNeeded } from "./ext.js";
import { skillIconName } from "./skills.js";


// 每个岗位一条程序生成的小鱼；fish_seed 为空时按岗位名固定，「换一条」只改这个岗位的种子。
const fishOf = (kind, size, opts) => agentFish(kind, size, opts);
const newSeed = () => Math.random().toString(36).slice(2, 10);
const LABELS = { queued: "排队中", running: "进行中", returned: "待检查", accepted: "已通过", rejected: "未采用", failed: "失败", cancelled: "已取消" };
const SPECIALISTS = new Set(["news", "idea", "goal"]);
const PHASES = { discover: "广泛搜索", verify: "核实内容", collect: "收集候选", research: "调研", check: "检查目标进展", execute: "执行任务", "idea.research": "调研构想", "goal.check": "检查目标进展", "task.execute": "执行任务" };

const ORDER = ["main", "news", "idea", "goal", "task"];
const TAGS = { main: "主模型", task: "通用执行" };
const LEADS = {
  main: "理解群聊、分派任务和检查结果；群内发言也使用该模型",
  news: "搜集资讯", idea: "提出构想", goal: "推进目标", task: "执行群成员提交的任务",
};
const sortProfiles = (list) => [...list].sort((a, b) => {
  const ia = ORDER.indexOf(a.kind), ib = ORDER.indexOf(b.kind);
  return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
});
const isCustom = (p) => !ORDER.includes(p.kind);
const modelById = (id) => ((state.mdl && state.mdl.models) || []).find((x) => x.id === id);
const modelName = (id) => { const x = modelById(id); return x ? x.name || x.model : id ? `${id}（已从模型库删除）` : ""; };

export function agentsFormSheet() {
  if (state.agentNew) return newAgentForm();
  const profiles = (state.agents && state.agents.profiles) || [];
  if (state.agentEdit) {
    const p = profiles.find((x) => x.kind === state.agentEdit);
    return p ? profileForm(p) : "";
  }
  if (state.agentFishEdit) {
    const p = profiles.find((x) => x.kind === state.agentFishEdit);
    return p ? fishForm(p) : "";
  }
  return "";
}

export function agentsPage() {
  const profiles = sortProfiles((state.agents && state.agents.profiles) || []);
  if (state.agentError && !profiles.length) return `<p class="err" role="alert">${esc(state.agentError)}</p><button class="btn" data-act="agent-reload">刷新</button>`;
  if (!state.agents) return loading();
  const groups = state.groups || [];
  const snapshot = state.agentSnapshot;
  const noModels = !((state.mdl && state.mdl.models) || []).length;
  const inSheet = state.sheet === "settings-form";
  const rows = profiles.map((p) => !inSheet && state.agentEdit === p.kind ? profileForm(p) : !inSheet && state.agentFishEdit === p.kind ? fishForm(p) : profileRow(p)).join("");
  return `
    ${noModels ? `<div class="warn-box">暂无模型<button class="btn small warn-act" data-act="set-tab" data-sub="models" data-tab="models">前往添加</button></div>` : ""}
    <div class="agent-avatar">${avatarBlock()}</div>
    <div class="h-sub-row"><h2 class="h-sub">专岗${tip("主模型负责分析和检查，专岗负责执行具体工作。")}</h2><button class="btn small" data-act="agent-new">新建</button></div>
    ${!inSheet && state.agentNew ? newAgentForm() : ""}
    ${rows ? `<div class="set-group">${rows}</div>` : ""}
    ${journalTop(groups)}
    ${state.agentError ? `<p class="err" role="alert">${esc(state.agentError)}</p><button class="btn small" data-act="agent-reload">刷新</button>` : ""}
    ${groups.length && !snapshot && !state.agentError ? loading() : ""}
    ${snapshot && snapshot.group_id === state.agentGroup ? journalBody(snapshot) : ""}`;
}

// 本群工作记录的标题行：一个群只显示胶囊，多个群才给下拉
function journalTop(groups) {
  let pick = "";
  if (groups.length === 1) pick = `<span class="journal-group-badge">${gface(groups[0])}<span>${esc(groups[0].name || `群 ${groups[0].id}`)}</span></span>`;
  else if (groups.length > 1) {
    const curGroup = groups.find((g) => g.id === state.agentGroup) || groups[0];
    const options = groups.map((g) => `<option value="${esc(g.id)}"${state.agentGroup === g.id ? " selected" : ""}>${esc(g.name || `群 ${g.id}`)}</option>`).join("");
    pick = `<div class="journal-group-pick">${curGroup ? gface(curGroup) : ""}<select id="agent-group" aria-label="选择服务群">${options}</select>${SVG.down || ""}</div>`;
  }
  return `<div class="h-sub-row journal-top"><h2 class="h-sub">本群工作记录${tip("展示各专岗在所选群中的最近交接与近期完成记录（用于避免重复工作）。本群规则和本群做法请在群页「本群设置」中修改。")}</h2>${pick}</div>
    ${groups.length ? "" : `<p class="fine">暂无服务群</p>`}`;
}

const journalTotal = (a) => (a.recent_handoffs || []).length + (a.kind === "task" ? 0 : (a.learned || []).length);

// 专岗胶囊切换 + 选中专岗的单块记录面板
function journalBody(snapshot) {
  const list = (snapshot.agents || []).filter((a) => a.kind !== "main");
  if (!list.length) return "";
  const cur = list.find((a) => a.kind === state.agentJournalKind)
    || list.find((a) => (a.recent_handoffs || []).length > 0 || (a.kind !== "task" && (a.learned || []).length > 0)) || list[0];
  const tabs = list.map((a) => {
    const on = a === cur;
    return `<button type="button" role="tab" class="journal-tab" data-act="agent-journal-tab" data-kind="${esc(a.kind)}" aria-selected="${on ? "true" : "false"}"><span class="agent-fish xs">${fishOf(a.kind, 22, on ? {} : { still: true })}</span><span>${esc(a.title)}</span><span class="journal-tab-cnt">${journalTotal(a)}</span></button>`;
  }).join("");
  return `<div class="journal-tabs" role="tablist" aria-label="选择专岗">${tabs}</div>${journalPanel(cur)}`;
}

function newAgentForm() {
  return `<div class="login ext-form">
    <div class="ext-form-h">新建专岗${tip("填写职责或分派场景后，主模型会在规划任务时自动选用，无需手动修改主模型 AGENTS.md。")}</div>
    <label for="agent-new-title">名称</label><input id="agent-new-title" maxlength="40" placeholder="例如：代码审查、日文翻译" />
    <label for="agent-new-when">擅长做什么（何时分派给它，可选）</label><input id="agent-new-when" maxlength="200" placeholder="例如：审查代码与 SQL 性能、翻译日文技术资料" />
    <div class="actions"><button class="btn primary" data-act="agent-create">创建</button><button class="btn" data-act="agent-new-cancel">取消</button></div>
  </div>`;
}

function countAgentSkills(p) {
  const all = state.agentSkills || (state.ext && state.ext.skills) || [];
  if (p.kind === "main") {
    return all.filter((s) => s.enabled !== false && (!(s.roles || []).length || (s.roles || []).includes("main"))).length;
  }
  const workerAll = all.filter((s) => s.enabled !== false && (!(s.roles || []).length || (s.roles || []).includes("worker")));
  if ((p.kind === "task" || isCustom(p)) && p.skills === null) return workerAll.length;
  const bound = new Set(p.skills || []);
  return workerAll.filter((s) => bound.has(s.name)).length;
}

function profileRow(p) {
  const tag = TAGS[p.kind] || (isCustom(p) ? "自定义" : "");
  const fresh = state.agentFishNew === p.kind;
  const off = p.kind !== "main" && p.enabled === false;
  const skCount = countAgentSkills(p);
  const model = p.model
    ? `${esc(modelName(p.model))}${p.effort ? ` · 思考强度 ${esc(EFFORT_NAMES[p.effort] || p.effort)}` : ""}${p.backup ? ` · 备用 ${esc(modelName(p.backup))}` : ""}`
    : `<b>未选择模型</b>${p.kind === "main" ? "（无法运行）" : "（暂用主模型）"}`;
  return `<div class="set-row agent-row"><span class="agent-fish${fresh ? " is-new" : ""}" data-fish="${esc(p.kind)}">${fishOf(p.kind, 48)}</span><div>
    <div class="set-name">${esc(p.title)}${tag ? `<span class="tag">${esc(tag)}</span>` : ""}</div>
    <div class="set-text">${esc(LEADS[p.kind] || (p.instructions && p.instructions.trim()) || "自定义专岗（自动纳入主模型分派清单）")}</div>
    <div class="set-text ext-st"><span class="dot ${off ? "" : "ok"}"></span>${off ? "已停用 · " : ""}${model} · <button type="button" class="link-btn ag-skill-link" data-act="agent-goto-skills" data-role="${esc(p.kind)}">${skCount ? `已启用 ${skCount} 项技能` : "查看技能"}</button></div>
    </div><div class="agent-row-acts"><button class="btn small ghost" data-act="agent-fish-reroll" data-kind="${esc(p.kind)}" title="随机生成">随机</button><button class="btn small ghost" data-act="agent-fish-custom" data-kind="${esc(p.kind)}" title="选择鱼种、颜色和眼神">自定义</button><button class="btn small" data-act="agent-profile-edit" data-kind="${esc(p.kind)}">设置</button></div></div>`;
}

function modelOptions(value, emptyLabel) {
  // 生图模型（use="image"）不能当聊天 / 干活模型：专岗下拉里不列它（服务端也会拒）。
  const list = ((state.mdl && state.mdl.models) || []).filter((x) => (x.use || "chat") !== "image");
  const eps = (state.mdl && state.mdl.endpoints) || [];
  const epName = (id) => { const e = eps.find((x) => x.id === id); return e ? e.name || e.id : id; };
  let html = `<option value="">${esc(emptyLabel)}</option>` + list.map((x) => `<option value="${esc(x.id)}"${x.id === value ? " selected" : ""}>${esc(x.name || x.model)} · ${esc(epName(x.endpoint))}</option>`).join("");
  if (value && !modelById(value)) html += `<option value="${esc(value)}" selected>${esc(value)}（已从模型库删除）</option>`;
  return html;
}

function effortOptions(modelId, value) {
  const x = modelById(modelId);
  const list = (x && x.efforts) || [];
  if (!list.length) return `<option value="">不支持</option>`;
  return `<option value="">默认</option>` + list.map((e) => `<option value="${e}"${e === value ? " selected" : ""}>${esc(EFFORT_NAMES[e] || e)}（${e}）</option>`).join("");
}

function docBlock(kind, which, title, lead, item, limit, extra) {
  const text = (item && item.text) || "";
  return `<div class="agent-doc">
    <div class="h-sub-row"><div class="lbl-row"><label for="agent-doc-${which}" class="set-name" style="margin:0">${title}</label>${tip(lead)}</div>${extra || ""}</div>
    <textarea id="agent-doc-${which}" class="mono-area" rows="10" spellcheck="false" data-limit="${limit || 16384}">${esc(text)}</textarea>
  </div>`;
}

// 岗位工具（GET /api/agents 的 tools_view，和自动回合实际给子 agent 的名单是同一份）
const TOOL_PHASES = { verify: "核对", recheck: "补打开", supplement: "补找", investigate: "调查", check: "检查进展" };
const toolChips = (names) => ((names || []).length
  ? `<span class="ext-tools">${names.map((n) => `<span class="ntag">${esc(n)}</span>`).join("")}</span>`
  : `<span class="fine">暂无</span>`);
const toolMark = (t) => (!t.registered ? "未注册" : t.task_only ? "仅派活时" : t.exec_class ? "执行类" : "");
function toolsBlock(p) {
  const v = p.tools_view;
  if (!v) return "";
  if (v.mode === "per_job") return `<div class="lbl-row"><label>工具</label>${tip("该岗位的工具在每次分派任务时由主模型选择；skill 声明的工具对该岗位不额外生效。")}</div><p class="fine" style="margin:0">每次分派时选择</p>`;
  if (v.mode !== "fixed_plus_skills") return "";
  const row = (name, inner) => `<div class="agent-tool-row"><span class="set-text">${esc(name)}</span>${inner}</div>`;
  const skills = (v.skills || []).map((e) => row(e.skill, `<span class="ext-tools">${(e.tools || []).map((t) => {
    const mark = toolMark(t);
    return `<span class="ntag${!t.registered || t.exec_class ? " warn" : ""}">${esc(t.name)}${mark ? ` · ${mark}` : ""}</span>`;
  }).join("")}</span>`)).join("");
  const phases = Object.entries(v.phases || {}).map(([ph, names]) => row(TOOL_PHASES[ph] || ph, toolChips(names))).join("");
  return `<div class="lbl-row"><label>工具</label>${tip("出厂工具固定。勾选声明了工具的 skill 后，岗位会带上这些工具并按 skill 内容使用；保存后下一轮生效。")}</div>
    <div class="agent-tools">
      ${row("出厂", toolChips(v.factory))}
      ${skills || row("skill 带来", `<span class="fine">暂无</span>`)}
      <p class="fine agent-tool-sub">各环节使用</p>
      ${phases}
    </div>`;
}

function resolveSkillsSelection(kind, boxes) {
  const checked = boxes.filter((x) => x.checked).map((x) => x.value);
  return (kind === "task" || isCustom({ kind })) && checked.length === boxes.length ? null : checked;
}

function profileForm(p) {
  const catalog = state.agentSkills || [];
  const generalistOpen = (p.kind === "task" || isCustom(p)) && p.skills === null;
  const selected = new Set(generalistOpen ? catalog.map((k) => k.name) : (p.skills || []));
  const names = [...new Set([...catalog.map((k) => k.name), ...selected])];
  const docs = (state.agentDocs || {})[p.kind];
  const main = p.kind === "main";
  const lim = (docs && docs.limits) || {};
  return `<div class="login ext-form agent-form">
    <div class="ext-form-h"><span class="agent-fish sm">${fishOf(p.kind, 28)}</span>设置 ${esc(p.title)}</div>
    <label for="agent-title">名称</label><input id="agent-title" maxlength="40" value="${esc(p.title)}" />
    ${isCustom(p) ? `<div class="lbl-row"><label for="agent-when">擅长做什么（何时分派给它）</label>${tip("主模型在规划任务时会自动读取这段说明，决定何时把步骤交给该专岗，无需手改主模型 AGENTS.md。")}</div><input id="agent-when" maxlength="200" value="${esc(p.instructions || "")}" placeholder="例如：负责翻译日文资料、审查代码质量" />` : ""}
    <h3 class="set-name" style="margin-top:14px">模型</h3>
    <label for="agent-model">模型</label><select id="agent-model">${modelOptions(p.model, main ? "请选择模型" : "同主模型")}</select>
    <label for="agent-effort">思考强度</label><select id="agent-effort">${effortOptions(p.model, p.effort)}</select>
    <label for="agent-backup">备用模型</label><select id="agent-backup">${modelOptions(p.backup, "不使用")}</select>
    ${main ? "" : `<div class="lbl-row"><label for="agent-escalate">返工模型</label>${tip("结果被退回两次后，改用该模型继续修改；与当前模型相同时不再重试。")}</div><select id="agent-escalate">${modelOptions(p.escalate, "同主模型")}</select>`}
    ${main ? "" : `<label class="chk"><input id="agent-enabled" type="checkbox"${p.enabled !== false ? " checked" : ""} />启用</label>`}
    ${main ? `<div class="lbl-row"><label>可用技能</label></div><p class="fine" style="margin:0">已启用 ${countAgentSkills(p)} 项主模型技能，可在 <button type="button" class="link-btn" data-act="agent-goto-skills" data-role="main">技能中心</button> 查看或管理。</p>` : `<div class="h-sub-row" style="margin-top:8px"><label style="margin:0">可用技能</label><button type="button" class="link-btn" data-act="agent-goto-skills" data-role="${esc(p.kind)}">前往技能中心</button></div><div id="agent-skills" class="ag-sk-list">${names.map((name) => {
      const k = catalog.find((x) => x.name === name);
      const brings = k && (k.allowed_tools || []).length ? `<span class="fine">（带工具：${k.allowed_tools.map(esc).join("、")}）</span>` : "";
      const iconName = typeof skillIconName === "function" ? skillIconName(k) : "sparkles";
      return `<label class="chk ag-sk-row"><input type="checkbox" value="${esc(name)}"${selected.has(name) ? " checked" : ""} /><span class="ag-sk-ico">${ico(iconName)}</span><span class="ag-sk-body"><span class="ag-sk-t">${esc(name)}${brings}${k && k.enabled === false ? `<span class="fine">（已停用）</span>` : ""}</span>${k && k.description ? `<span class="ag-sk-d">${esc(k.description)}</span>` : ""}</span></label>`;
    }).join("") || `<p class="fine">暂无技能</p>`}</div>`}
    ${main ? "" : toolsBlock(p)}
    ${!docs ? loading() : `<details class="cfg-advanced"><summary>高级 · 自定义岗位提示词（SOUL.md / AGENTS.md）</summary>
      ${docBlock(p.kind, "soul", "SOUL.md", "性格与语气" + (docs.soul && docs.soul.synced_from_maibot ? "；当前内容已从 MaiBot 同步。" : "。"), docs.soul, lim.soul, `<button type="button" class="btn small" data-act="agent-soul-sync" data-kind="${esc(p.kind)}">从 MaiBot 同步</button>`)}
      ${docBlock(p.kind, "agents", "AGENTS.md", main ? "高级工作规则；自定义专岗的分派说明已自动注入，一般无需手改。" : "高级工作规则，默认预设已足够日常使用。", docs.agents, lim.agents, `<button type="button" class="btn small ghost" data-act="agent-agents-reset" data-kind="${esc(p.kind)}">恢复默认</button>`)}</details>`}
    <p class="err" id="agent-err" hidden></p>
    <div class="actions"><button class="btn primary" data-act="agent-profile-save" data-kind="${esc(p.kind)}">保存</button><button class="btn" data-act="agent-profile-cancel">取消</button>${isCustom(p) ? `<button class="btn ghost danger" data-act="agent-delete" data-kind="${esc(p.kind)}">删除专岗</button>` : ""}</div>
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
    <div class="ext-form-h">${esc(p.title)} 的头像</div>
    <div class="fish-preview">${fishSvg(draftSeed(kind, d), { size: 96 })}<div><div class="set-name">${esc(FISH_SPECIES[d.species] || "")}</div><div class="fine">${esc(FISH_COLOR_NAMES[d.color] || "")} · ${esc(FISH_EYES[d.eyes] || "")}</div></div></div>
    <label>鱼种</label><div class="fish-opts" role="group" aria-label="鱼种">${species}</div>
    <label>颜色</label><div class="fish-swatches" role="group" aria-label="颜色">${colors}</div>
    <label>眼神</label><div class="fish-opts" role="group" aria-label="眼神">${eyes}</div>
    <div class="actions"><button class="btn primary" data-act="agent-fish-save" data-kind="${esc(kind)}">保存</button><button class="btn" data-act="agent-fish-cancel">取消</button><button class="btn ghost" data-act="agent-fish-shape" data-kind="${esc(kind)}" title="仅更换鳍和姿态">更换姿态</button></div>
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

// 这个群的工作记录（只读）：最近交接 + 近期完成。本群规矩和本群做法在群页「本群设置」（docs/17 §八.4）
function journalPanel(a) {
  const learned = a.learned || [];
  const handoffs = a.recent_handoffs || [];
  const isTask = a.kind === "task";
  const note = !isTask && a.index_note ? a.index_note : null;
  const hasNote = !!(note && note.content);
  const meta = [
    `最近交接 <b>${handoffs.length}</b>`,
    isTask ? "" : `近期完成 <b>${learned.length}</b>`,
    !isTask && hasNote ? `常驻笔记 <b>v${Number(note.version) || 1}</b>` : "",
  ]
    .filter(Boolean)
    .join('<span class="journal-dot">·</span>');
  const sub =
    !isTask && state.agentJournalSub === "note"
      ? "note"
      : !isTask && (state.agentJournalSub === "learned" || (!state.agentJournalSub && !handoffs.length && learned.length))
        ? "learned"
        : !isTask && !state.agentJournalSub && !handoffs.length && !learned.length && hasNote
          ? "note"
          : "handoffs";
  const segBtn = (key, label) => `<button type="button" role="tab" data-act="agent-journal-sub" data-sub="${key}" aria-selected="${sub === key ? "true" : "false"}"${sub === key ? ' class="on"' : ""}>${label}</button>`;
  const seg = !isTask && (handoffs.length || learned.length || hasNote)
    ? `<div class="seg journal-seg" role="tablist" aria-label="记录类型">${segBtn("handoffs", "最近交接")}${segBtn("learned", "近期完成")}${segBtn("note", "常驻笔记")}</div>`
    : "";
  const empty = (t) => `<div class="journal-empty"><p class="fine">${t}</p></div>`;
  let body;
  if (!handoffs.length && (isTask || (!learned.length && !hasNote && sub !== "note"))) {
    body = empty("暂无工作记录。该专岗在本群执行任务或完成检查后将在此留存。");
  } else if (sub === "note") {
    body = noteEntry(note);
  } else if (sub === "handoffs") {
    body = handoffs.length ? `<div class="journal-list">${handoffs.map(handoffEntry).join("")}</div>` : empty("暂无交接记录");
  } else {
    body = learned.length ? `<div class="journal-list">${[...learned].reverse().map(learnedEntry).join("")}</div>` : empty("暂无近期完成记录");
  }
  return `<section class="set-group agent-journal" data-kind="${esc(a.kind)}">
    <div class="journal-head"><div class="journal-who"><span class="agent-fish sm">${fishOf(a.kind, 34)}</span><div class="journal-title"><h3 class="set-name">${esc(a.title)}${a.enabled === false ? `<span class="tag">已停用</span>` : ""}</h3><div class="journal-meta">${meta}</div></div></div>${seg}</div>
    ${body}
  </section>`;
}

function noteEntry(note) {
  const content = note && note.content ? String(note.content) : "";
  const locked = Boolean(note && note.locked);
  const ts = note && note.updated_ts ? fmtTime(note.updated_ts) : "";
  return `<div class="journal-list"><div class="agent-entry learned-entry">
    ${content ? `<p class="jl-text" style="white-space:pre-wrap">${esc(content)}</p>` : `<p class="fine">暂无常驻索引笔记</p>`}
    <div class="jl-meta">
      ${locked ? `<span class="tag">已锁定</span>` : ""}
      ${ts ? `<span class="jh-time">${esc(ts)}</span>` : ""}
      <button type="button" class="link-btn" data-act="agent-goto-gctx" data-g="${esc(state.agentGroup || "")}">前往本群设置管理</button>
    </div>
  </div></div>`;
}

// 状态 → [圆点类, 色调类]
const STATUS_TONE = { accepted: ["ok", "ok"], running: ["running", "run"], queued: ["pool", "run"], returned: ["reviewing", "warn"], rejected: ["waiting", "warn"], failed: ["failed", "bad"] };
const uniq = (xs) => [...new Set(xs.map((x) => String(x).trim()).filter(Boolean))];

// 后端 review 可能是 JSON 字符串、对象或纯文本；只取摘要、依据、工具，绝不原样露 JSON
function parseReview(raw) {
  let v = raw;
  if (typeof v === "string") {
    const t = v.trim();
    if (!t) return { summary: "", refs: [], usedTools: [] };
    try { v = JSON.parse(t); } catch { return { summary: t, refs: [], usedTools: [] }; }
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return { summary: typeof v === "string" ? v : "", refs: [], usedTools: [] };
  const arr = (x) => (Array.isArray(x) ? x.filter((y) => typeof y === "string") : []);
  return { summary: typeof v.summary === "string" ? v.summary.trim() : "", refs: arr(v.refs), usedTools: arr(v.used_tools) };
}

function fmtTime(ts) {
  if (!ts) return "";
  const s = when(ts);
  return s || new Date(ts * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const refItem = (s) => (/^https?:\/\//i.test(s) ? `<a href="${safeUrl(s)}" target="_blank" rel="noopener noreferrer">查看依据 ↗</a>` : `<span class="ntag">${esc(s)}</span>`);
const chips = (xs) => `<span class="ext-tools">${xs.map((t) => `<span class="ntag">${esc(t)}</span>`).join("")}</span>`;
const field = (k, v) => `<div class="jh-field"><span class="jh-k">${k}</span>${v}</div>`;

function handoffEntry(h) {
  const [dot, tone] = STATUS_TONE[h.status] || ["", ""];
  const rev = parseReview(h.review);
  const criteria = Array.isArray(h.criteria) ? h.criteria.filter((c) => typeof c === "string" && c.trim()).map((c) => c.trim())
    : typeof h.criteria === "string" && h.criteria.trim() && !/^\s*\[\s*\]\s*$/.test(h.criteria) ? [h.criteria.trim()] : [];
  const tools = uniq([...(h.tools || []), ...rev.usedTools]);
  const skills = h.skills || [];
  const refs = uniq([...(h.evidence || []), ...rev.refs]);
  const summary = h.error ? `<p class="jh-summary bad-t">${esc(h.error)}</p>`
    : h.summary ? `<p class="jh-summary">${esc(h.summary)}</p>`
      : `<p class="jh-summary is-muted">${h.status === "running" || h.status === "queued" ? "正在执行，尚未提交结果" : "未提交结果摘要"}</p>`;
  const more = [
    h.brief ? field("任务要求", `<p>${esc(h.brief)}</p>`) : "",
    criteria.length ? field("检查标准", `<ul class="jh-criteria">${criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`) : "",
    tools.length ? field("使用工具", chips(tools)) : "",
    skills.length ? field("使用 skill", chips(skills)) : "",
    refs.length ? field("依据", `<span class="jh-refs">${refs.map(refItem).join("")}</span>`) : "",
    h.id ? `<div class="jh-id fine">编号 ${esc(h.id)}${h.parent_id ? ` · 上级 ${esc(h.parent_id)}` : ""}</div>` : "",
  ].join("");
  return `<div class="agent-entry handoff-entry">
    <div class="jh-top"><span class="jh-status ${tone}"><span class="dot ${dot}"></span>${esc(LABELS[h.status] || h.status || "未知")}</span><span class="jh-phase">${esc(PHASES[h.phase] || h.phase || "工作轮次")}</span>${h.task_id ? `<span class="ntag">${esc(h.task_id)}</span>` : ""}<span class="jh-time">${esc(fmtTime(h.updated || h.created))}</span></div>
    ${summary}
    ${rev.summary ? `<div class="jh-review"><span class="jh-k">检查意见</span><span>${esc(rev.summary)}</span></div>` : ""}
    ${more ? `<details class="jh-more"><summary>查看要求与明细</summary><div class="jh-more-body">${more}</div></details>` : ""}
  </div>`;
}

function learnedEntry(m) {
  const refs = uniq([m.source_id, ...(m.refs || [])].filter(Boolean));
  const t = m.updated ? fmtTime(m.updated) : "";
  const meta = (t ? `<span class="jh-time">${esc(t)}</span>` : "") + refs.map(refItem).join("");
  return `<div class="agent-entry learned-entry"><p class="jl-text">${esc(m.text || "")}</p>${meta ? `<div class="jl-meta">${meta}</div>` : ""}</div>`;
}

export async function loadAgents() {
  state.agentError = "";
  try {
    const [agents, extensions] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/extensions"), state.mdl ? null : loadModels()]);
    state.agents = agents;
    state.agentSkills = extensions.skills || [];
    if (Array.isArray(agents && agents.worker_tool_choices)) state.toolChoices = agents.worker_tool_choices;
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
  state.agentJournalSub = null; // 专岗保持不变，方便跨群对比
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
    case "agent-goto-skills": {
      state.skillRole = el.dataset.role || "";
      const { enterSettings } = await import("../router.js");
      await enterSettings("extensions", "skills");
      return true;
    }
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
      const instructions = ($("agent-new-when") && $("agent-new-when").value.trim()) || "";
      if (!title) return toast("请填写专岗名称", true), true;
      el.disabled = true;
      try {
        const p = await api("POST", "/api/agents", instructions ? { title, instructions } : { title });
        state.agentNew = false;
        await loadAgents();
        state.agentEdit = p.kind;
        repaintSheet();
        await loadDocs(p.kind);
        repaintSheet();
        toast(instructions ? "已创建，主模型将按分派说明自动选用" : "已创建，可补充该专岗擅长的工作场景");
      } catch (err) { el.disabled = false; toast(err.message, true); }
      return true;
    }
    case "agent-delete": {
      const kind = el.dataset.kind;
      const p = ((state.agents && state.agents.profiles) || []).find((x) => x.kind === kind);
      if (!p || !confirm(`删除「${p.title}」？文件将移入回收目录，工作记录保留。`)) return true;
      try {
        await api("DELETE", `/api/agents/${encodeURIComponent(kind)}`);
        state.agentEdit = null;
        await loadAgents(); repaintSheet(); toast("已删除专岗");
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
          ? (r.text ? "无法读取 MaiBot 人格，内容未更改" : "无法读取 MaiBot 人格，内容暂为空")
          : "已从 MaiBot 同步");
      } catch (err) { toast(err.message, true); }
      finally { el.disabled = false; }
      return true;
    }
    case "agent-agents-reset": {
      const kind = el.dataset.kind;
      if (!confirm("恢复默认内容？已修改的内容将丢失。")) return true;
      try {
        const r = await api("POST", `/api/agents/${encodeURIComponent(kind)}/docs/agents/reset`, {});
        setDocText(kind, "agents", r);
        toast("已恢复默认");
      } catch (err) { toast(err.message, true); }
      return true;
    }
    case "agent-journal-tab": state.agentJournalKind = el.dataset.kind || null; state.agentJournalSub = null; repaintSheet(); return true;
    case "agent-journal-sub": state.agentJournalSub = el.dataset.sub || null; repaintSheet(); return true;
    case "agent-goto-gctx": {
      const gid = el.dataset.g || state.agentGroup || state.g;
      if (!gid) return true;
      state.groupSub = "admin";
      const { go } = await import("../router.js");
      go({ page: null, g: gid, tab: "group", detail: null });
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
        toast("已更换");
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
      if (!body.title) return fail("请填写名称"), true;
      if (kind === "main" && !body.model) return fail("主模型必须选择模型"), true;
      if (body.backup && body.backup === body.model) return fail("备用模型不能与当前模型相同"), true;
      if ($("agent-when")) body.instructions = $("agent-when").value.trim();
      if ($("agent-escalate")) body.escalate = $("agent-escalate").value;
      if ($("agent-enabled")) body.enabled = $("agent-enabled").checked;
      if ($("agent-skills")) body.skills = resolveSkillsSelection(kind, [...document.querySelectorAll("#agent-skills input[type=checkbox]")]);
      const docs = (state.agentDocs || {})[kind];
      const writes = [];
      for (const which of ["soul", "agents"]) {
        const box = $(`agent-doc-${which}`);
        if (!box || !docs) continue;
        const text = box.value;
        const limit = Number(box.dataset.limit) || 16384;
        if (new Blob([text]).size > limit) return fail(`${which === "soul" ? "SOUL.md" : "AGENTS.md"} 过长，最多 ${limit} 字节。`), true;
        if (text !== ((docs[which] && docs[which].text) || "")) writes.push([which, text]);
      }
      el.disabled = true;
      try {
        const path = `/api/agents/${encodeURIComponent(kind)}`;
        try {
          await api("PUT", path, body);
        } catch (ex) {
          // 新挂的 skill 带执行类工具：服务端要求确认，确认后带 confirm_exec 重试
          if (!execConfirmNeeded(ex) || !confirmExecTools(ex)) throw ex;
          await api("PUT", path, { ...body, confirm_exec: true });
        }
        for (const [which, text] of writes) await api("PUT", `/api/agents/${encodeURIComponent(kind)}/docs/${which}`, { text });
        state.agentEdit = null;
        if (state.agentDocs) delete state.agentDocs[kind];
        await loadAgents();
        const { loadSettings } = await import("../router.js");
        await loadSettings().catch(() => null);
        repaintSheet(); toast("已保存，下次运行时生效");
      } catch (e) { el.disabled = false; fail(e.message); }
      return true;
    }
    default: return false;
  }
}
