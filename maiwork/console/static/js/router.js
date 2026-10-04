// MaiWork 网页 · 路由（网址 # 部分）、加载数据、定时刷新。
import { TABS, admin, desktop, gadmin, state, ui } from "./state.js";
import { toast } from "./util.js";
import { api, groupRef, grp } from "./api.js";
import { loadTask } from "./detail.js";
import { SET_SUBS, loadGA } from "./settings/index.js";
import { loadExt } from "./settings/ext.js";
import { loadAgents } from "./settings/agents.js";
import { loadRules } from "./settings/rules.js";
import { loadUsage } from "./settings/usage.js";
import { loadIdentity } from "./settings/identity.js";
import { loadModels } from "./settings/models.js";
import { loadLogSummary } from "./settings/logs.js";
import { closeSheet, openSheet, repaintSheet } from "./sheet.js";
import { enterChat } from "./chat.js";
import { render, renderRail, renderSide, renderTabbar, renderTop, renderView } from "./render.js";
import { maybeOnboard, openOnboarding } from "./onboarding.js";
import { loadUpdate } from "./update.js";

/* ───────────── 路由 ───────────── */

export function syncHash() {
  if (state.page === "chat") {
    const h = `#/chat/${state.chatId || ""}`;
    if (location.hash !== h) history.replaceState(null, "", h);
    return;
  }
  if (state.page === "settings") {
    const h = `#/settings/${state.setSub}`;
    if (location.hash !== h) history.replaceState(null, "", h);
    return;
  }
  const g = grp();
  if (!g) return;
  const ref = gadmin() ? g.id : state.ref;
  const h = `#/${ref}/${state.tab}` + (state.detail ? `/${state.detail.id}` : "");
  if (location.hash !== h) history.replaceState(null, "", h);
}

export function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "chat") return { chat: true, id: parts[1] || "", ref: "", tab: "", sub: "" };
  if (parts[0] === "settings") return { settings: true, sub: parts[1] === "logs" ? "usage" : parts[1] || "overview", ref: "", tab: "", id: "" };
  // 旧的目标页链接仍能打开原目标详情，只是落到「在做的事」。
  const tab = parts[1] === "goals" ? "tasks" : parts[1] || "";
  return { ref: decodeURIComponent(parts[0] || ""), tab, id: decodeURIComponent(parts[2] || "") };
}

export function applyHash() {
  const h = parseHash();
  state.page = h.settings && admin() ? "settings" : h.chat && admin() ? "chat" : null;
  if (h.chat && h.id) state.chatId = Number(h.id) || h.id;
  if (h.settings) state.setSub = SET_SUBS.some((x) => x[0] === h.sub) ? h.sub : "overview";
  if (TABS.some((x) => x.id === h.tab)) state.tab = h.tab;
  if (gadmin()) {
    const hit = state.groups.find((x) => x.id === h.ref || x.token === h.ref);
    state.g = hit ? hit.id : state.g && state.groups.some((x) => x.id === state.g) ? state.g : state.groups.length ? state.groups[0].id : null;
  }
  if (!h.chat && !h.settings && h.id) state.detail = { type: /^G-/.test(h.id) ? "goal" : /^I-/.test(h.id) ? "idea" : "task", id: h.id };
  else state.detail = null;
}

async function loadMe() {
  const h = parseHash();
  state.ref = h.ref;
  const me = await api("GET", "/api/me");
  state.me = me;
  if (me.now) state.skew = me.now - Date.now() / 1000;
  state.badLink = me.role === "none" && !!h.ref;
  if (me.role === "member" || me.role === "group_admin") state.g = me.group;
}

export async function loadGroups() {
  if (!state.me || state.me.role === "none") {
    state.groups = [];
    return;
  }
  state.groups = (await api("GET", "/api/groups")) || [];
}

export async function loadView(quietly) {
  const g = grp();
  if (!g) return;
  const id = g.id;
  try {
    const v = await api("GET", `/api/groups/${encodeURIComponent(groupRef(g))}`);
    if (state.g !== id) return;
    state.view = v;
    const i = state.groups.findIndex((x) => x.id === id);
    if (i >= 0) state.groups[i] = Object.assign({}, state.groups[i], pickSummary(v));
  } catch (e) {
    if (!quietly) toast(e.message, true);
    if (e.status === 401 || e.status === 403) return reboot();
  }
}

const pickSummary = (v) => ({ name: v.name, icon: v.icon, members: v.members, fresh: v.fresh, quiet: v.quiet, today: v.today, read_since: v.read_since });

export async function loadSettings() {
  if (!admin()) return;
  try {
    state.settings = await api("GET", "/api/settings");
  } catch (e) {
    toast(e.message, true);
  }
}

export async function reboot() {
  state.view = null;
  state.settings = null;
  state.groupControls = null;
  state.agents = null;
  state.agentSnapshot = null;
  state.agentEdit = null;
  state.agentLoadSeq = (state.agentLoadSeq || 0) + 1;
  state.tasks = {};
  try {
    await loadMe();
    await loadGroups();
  } catch (e) {
    state.me = state.me || { role: "none" };
    toast(e.message, true);
  }
  applyHash();
  if (!admin() && state.me && state.me.role === "member") state.g = state.me.group;
  syncHash();
  ui.flash = true;
  render();
  if (grp()) await loadView();
  if (admin()) pollUpdate(true);
  const settingsReady = admin()
    ? loadSettings().then(async () => {
        if (state.page === "settings" && state.setSub === "extensions") await loadExt();
        if (state.page === "settings" && state.setSub === "agents") await loadAgents();
        if (state.page === "settings" && state.setSub === "models") await loadModels();
        if (state.page === "settings" && state.setSub === "rules") await loadRules();
        if (state.page === "settings" && state.setSub === "usage") await Promise.all([loadUsage(), loadLogSummary()]);
        if (state.page === "settings" && ["memory", "agents"].includes(state.setSub)) await loadIdentity();
        if (state.page === "settings" && state.setSub === "links") await loadGA();
        renderRail();
        repaintSheet();
      })
    : null;
  ui.flash = true;
  render();
  if (state.detail) {
    if (state.detail.type === "task") loadTask(state.detail.id);
    if (!desktop.matches) openSheet("detail");
  }
  // 网址带 ?open=settings / ?open=models 时直接打开（方便从别处跳过来，也方便截图核对）
  if (admin() && state.page === "chat") enterChat(state.chatId);
  const open = new URLSearchParams(location.search).get("open");
  if (admin() && open === "onboarding") {
    await settingsReady;
    openOnboarding(null);
  } else if (admin()) settingsReady.then(maybeOnboard);
  if (admin() && ["settings", "models", "extensions"].includes(open)) {
    await settingsReady;
    enterSettings(open === "settings" ? "overview" : open);
  }
}

export async function enterSettings(sub) {
  if (!admin()) return;
  closeSheet();
  state.extEdit = null;
  go({ page: "settings", setSub: sub || state.setSub || "overview", detail: null });
  if (!state.settings) await loadSettings();
  if (state.setSub === "extensions") await loadExt();
  if (state.setSub === "agents") await loadAgents();
  if (state.setSub === "models") await loadModels();
  if (state.setSub === "rules") await loadRules();
  if (state.setSub === "usage") await Promise.all([loadUsage(), loadLogSummary()]);
  if (["memory", "agents"].includes(state.setSub)) await loadIdentity();
  if (state.setSub === "links") await loadGA();
  if (state.page === "settings") {
    renderView();
    renderSide();
    renderRail();
  }
}

export function go(patch) {
  const gChanged = patch.g && patch.g !== state.g;
  Object.assign(state, patch);
  if (gChanged) state.view = null;
  ui.flash = true;
  syncHash();
  render();
  window.scrollTo({ top: 0 });
  if (gChanged) loadView().then(() => ((ui.flash = true), render()));
}

/* 更新提醒：开页时拉一次（服务端到点才去 GitHub 查，查在后台）；12 秒后再拉一次拿后台结果，之后 30 分钟一次 */
let updTimer = null;
function repaintUpdate() {
  renderRail();
  if (state.page === "settings" && state.setSub === "overview") renderView();
  else if (!state.page && grp()) renderView({ poll: true });
}
export function pollUpdate(first) {
  clearTimeout(updTimer);
  if (!admin()) return;
  loadUpdate().then(repaintUpdate);
  updTimer = setTimeout(() => pollUpdate(false), first ? 12000 : 30 * 60 * 1000);
}

/* 定时刷新：页面在前台时每 30 秒拉一次当前群 */
setInterval(() => {
  if (document.hidden || !grp()) return;
  loadView(true).then(() => {
    if (state.page) return renderRail();
    if (state.sheet && state.sheet !== "detail") return renderTopBits();
    renderTopBits();
    renderView({ poll: true });
    if (!state.detail) renderSide();
  });
}, 30000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && grp() && !state.page) loadView(true).then(() => (renderTopBits(), renderView({ poll: true }), renderSide()));
});
export function renderTopBits() {
  if (!grp() && !state.page) return;
  renderTop();
  renderTabbar();
  renderRail();
}
