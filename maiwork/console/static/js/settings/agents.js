// MaiWork 网页 · 专岗：职责、本群提醒、经主流程验收的经验与交接记录。
import { state, $ } from "../state.js";
import { agentFish, api } from "../api.js";
import { esc, toast } from "../util.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";


// 每个岗位一条程序生成的小鱼；fish_seed 为空时按岗位名固定，「换一条」只改这个岗位的种子。
const fishOf = (kind, size) => agentFish(kind, size);
const newSeed = () => Math.random().toString(36).slice(2, 10);
const LABELS = { queued: "排队", running: "进行中", returned: "已交回，等验收", accepted: "已验收", rejected: "未采用", failed: "没跑成", cancelled: "已取消" };
const SPECIALISTS = new Set(["news", "idea", "goal"]);
const PHASES = { discover: "撒网搜索", verify: "打开核验", collect: "收集候选", research: "调查", propose: "目标提案调查", check: "目标进展调查", execute: "任务执行", "idea.research": "构想调查", "goal.propose": "目标提案调查", "goal.check": "目标进展调查", "task.execute": "任务执行" };

export function agentsPage() {
  const profiles = (state.agents && state.agents.profiles) || [];
  if (state.agentError && !profiles.length) return `<p class="err" role="alert">${esc(state.agentError)}</p><button class="btn" data-act="agent-reload">重新加载</button>`;
  if (!state.agents) return loading();
  const groups = state.groups || [];
  const snapshot = state.agentSnapshot;
  return `
    <p class="h-meta">各管一件事，经验按群分开。具体工作仍交给子 agent，主模型负责验收。</p>
    <h2 class="h-sub">岗位职责</h2>
    ${profiles.map((p) => state.agentEdit === p.kind ? profileForm(p) : profileRow(p)).join("")}
    <h2 class="h-sub">这个群的工作册</h2>
    ${groups.length ? `<label for="agent-group" class="fine">选择服务群</label><select id="agent-group">${groups.map((g) => `<option value="${esc(g.id)}"${state.agentGroup === g.id ? " selected" : ""}>${esc(g.name || `群 ${g.id}`)}</option>`).join("")}</select>` : `<p class="fine">先在设置中添加服务群，再记录专岗经验。</p>`}
    <p class="fine">本群提醒由你填写；经验只在主流程验收后积累。未验收的候选不会变成已确认的事实。</p>
    ${state.agentError ? `<p class="err" role="alert">${esc(state.agentError)}</p><button class="btn small" data-act="agent-reload">重新加载</button>` : ""}
    ${groups.length && !snapshot && !state.agentError ? loading() : ""}
    ${snapshot && snapshot.group_id === state.agentGroup ? (snapshot.agents || []).filter((a) => SPECIALISTS.has(a.kind) || a.kind === "task").map(memorySection).join("") : ""}`;
}

function profileRow(p) {
  const task = p.kind === "task";
  const fresh = state.agentFishNew === p.kind;
  return `<div class="set-row agent-row"><span class="agent-fish${fresh ? " is-new" : ""}" data-fish="${esc(p.kind)}">${fishOf(p.kind, 48)}</span><div>
    <div class="set-name">${esc(p.title)}${task ? `<span class="tag">通用执行</span>` : ""}</div>
    <div class="set-text">${esc(p.instructions || "")}</div>
    <div class="set-text ext-st"><span class="dot ${p.enabled !== false ? "ok" : ""}"></span>${task ? "按本次批准范围执行，不积累跨任务记忆" : p.enabled !== false ? "已启用 · 沿用现有时段和事件触发" : "已停用"}</div>
    </div><div class="agent-row-acts"><button class="btn small ghost" data-act="agent-fish-reroll" data-kind="${esc(p.kind)}" title="给这个岗位换一条小鱼">换一条</button>${task ? "" : `<button class="btn small" data-act="agent-profile-edit" data-kind="${esc(p.kind)}">改职责</button>`}</div></div>`;
}

function profileForm(p) {
  const selected = new Set(p.skills || []);
  const catalog = state.agentSkills || [];
  const names = [...new Set([...catalog.map((k) => k.name), ...selected])];
  return `<div class="login ext-form">
    <div class="ext-form-h"><span class="agent-fish sm">${fishOf(p.kind, 28)}</span>改 ${esc(p.title)}</div>
    <label for="agent-title">岗位名称</label><input id="agent-title" maxlength="40" value="${esc(p.title)}" />
    <label for="agent-instructions">职责和工作要求</label><textarea id="agent-instructions" rows="6" maxlength="3000">${esc(p.instructions || "")}</textarea>
    <label class="chk"><input id="agent-enabled" type="checkbox"${p.enabled !== false ? " checked" : ""} />启用这个专岗</label>
    <label>可用 skill</label><div id="agent-skills">${names.map((name) => {
      const k = catalog.find((x) => x.name === name);
      return `<label class="chk"><input type="checkbox" value="${esc(name)}"${selected.has(name) ? " checked" : ""} />${esc(name)}${k && k.enabled === false ? `<span class="fine">（当前停用）</span>` : ""}</label>`;
    }).join("") || `<p class="fine">还没有可选择的 skill。</p>`}</div>
    <p class="fine">这里只选岗位会用到的 skill，不会把停用的 skill 打开。工具权限由程序限定，修改职责不能扩大权限。</p>
    <div class="actions"><button class="btn primary" data-act="agent-profile-save" data-kind="${esc(p.kind)}">保存职责</button><button class="btn" data-act="agent-profile-cancel">取消</button></div>
  </div>`;
}

function memorySection(a) {
  const learned = a.learned || [];
  const handoffs = a.recent_handoffs || [];
  if (a.kind === "task") return `<section class="agent-journal">
    <h3 class="set-name"><span class="agent-fish sm">${fishOf(a.kind, 30)}</span>${esc(a.title)}</h3>
    <p class="fine">只记录本次任务的交接与验收，不积累跨任务经验。</p>
    <details><summary>最近的交接 · ${handoffs.length}</summary>${handoffs.map(handoffEntry).join("") || `<p class="fine">还没有交接记录。获批任务开始执行后会显示在这里。</p>`}</details>
  </section>`;
  const hint = { news: "例如：优先看有原始资料的开发访谈，不追充值活动。", idea: "例如：先提一个周末能做出样品的点子。", goal: "例如：有新证据才更新进展，缺资源时先问。" }[a.kind] || "写一句本群工作要求";
  return `<section class="agent-journal">
    <h3 class="set-name"><span class="agent-fish sm">${fishOf(a.kind, 30)}</span>${esc(a.title)}</h3>
    <label class="fine" for="agent-notes-${esc(a.kind)}">本群提醒</label>
    <textarea id="agent-notes-${esc(a.kind)}" rows="3" maxlength="2000" placeholder="${esc(hint)}">${esc(a.notes || "")}</textarea>
    <div class="actions"><button class="btn small" data-act="agent-memory-save" data-kind="${esc(a.kind)}" data-g="${esc(state.agentGroup)}">保存本群提醒</button></div>
    <details><summary>已验收积累的经验 · ${learned.length}</summary>
      ${learned.map((m) => `<div class="agent-entry"><p>${esc(m.text || "")}</p>${referenceLinks(m.refs)}${m.source_id ? `<span class="fine">依据：${esc(m.source_id)}</span>` : ""}</div>`).join("") || `<p class="fine">还没有。这个专岗完成工作并经主流程验收后，会在这里留下经验。</p>`}
    </details>
    <details><summary>最近的交接 · ${handoffs.length}</summary>
      ${handoffs.map(handoffEntry).join("") || `<p class="fine">还没有交接记录。到现有时段或收到工作请求时，专岗才会启动，不会一直空转。</p>`}
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
    const [agents, extensions] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/extensions")]);
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
});

export async function actAgents(action, el) {
  switch (action) {
    case "agent-profile-edit": state.agentEdit = el.dataset.kind; repaintSheet(); return true;
    case "agent-profile-cancel": state.agentEdit = null; repaintSheet(); return true;
    case "agent-reload": await loadAgents(); repaintSheet(); return true;
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
      el.disabled = true;
      try {
        const skills = [...document.querySelectorAll("#agent-skills input:checked")].map((x) => x.value);
        await api("PUT", `/api/agents/${encodeURIComponent(el.dataset.kind)}`, { title: $("agent-title").value.trim(), instructions: $("agent-instructions").value.trim(), enabled: $("agent-enabled").checked, skills });
        state.agentEdit = null;
        await loadAgents(); repaintSheet(); toast("岗位职责已保存，下次工作时生效");
      } catch (err) { el.disabled = false; toast(err.message, true); }
      return true;
    }
    case "agent-memory-save": {
      el.disabled = true;
      const kind = el.dataset.kind;
      const gid = el.dataset.g;
      if (gid !== state.agentGroup) {
        el.disabled = false;
        toast("群已经切换，请在当前群重新填写提醒", true);
        return true;
      }
      try {
        await api("PUT", `/api/groups/${encodeURIComponent(gid)}/agents/${encodeURIComponent(kind)}/memory`, { notes: $(`agent-notes-${kind}`).value });
        if (gid === state.agentGroup) await loadAgentGroup(gid);
        toast("本群提醒已保存，只供这个群的专岗使用");
      } catch (err) { el.disabled = false; toast(err.message, true); }
      return true;
    }
    default: return false;
  }
}
