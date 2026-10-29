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
import { loadAttention } from "./settings/attention.js";
import { closeSheet, openSheet, repaintSheet } from "./sheet.js";
import { enterChat, loadChat } from "./chat.js";
import { render, renderRail, renderSide, renderTabbar, renderTop, renderView } from "./render.js";
import { maybeOnboard, openOnboarding } from "./onboarding.js";
import { loadUpdate } from "./update.js";
import { loadAppearance } from "./settings/appearance.js";
import { loadSocial, resetSocial } from "./settings/social.js";
import { loadTheme } from "./theme.js";
import { loadBoard, loadBoardItem } from "./board.js";

/* ───────────── 路由 ───────────── */

export function syncHash() {
  if (state.page === "chat") {
    const h = `#/chat/${state.chatId || ""}`;
    if (location.hash !== h) history.replaceState(null, "", h);
    return;
  }
  if (state.page === "board") {
    if (location.hash !== "#/board") history.replaceState(null, "", "#/board");
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
  if (parts[0] === "board") return { board: true, id: "", ref: "", tab: "", sub: "" };
  if (parts[0] === "chat") return { chat: true, id: parts[1] || "", ref: "", tab: "", sub: "" };
  if (parts[0] === "settings") return { settings: true, sub: parts[1] === "logs" ? "usage" : parts[1] === "social-login" ? "social" : parts[1] || "overview", ref: "", tab: "", id: "" };
  // 旧的目标页链接仍能打开原目标详情，只是落到「任务」。
  const tab = parts[1] === "goals" ? "tasks" : parts[1] || "";
  return { ref: decodeURIComponent(parts[0] || ""), tab, id: decodeURIComponent(parts[2] || "") };
}

export function applyHash() {
  const h = parseHash();
  state.page = h.settings && admin() ? "settings" : h.chat && admin() ? "chat" : h.board && admin() ? "board" : null;
  if (h.chat && h.id) state.chatId = Number(h.id) || h.id;
  if (h.settings) state.setSub = SET_SUBS.some((x) => x[0] === h.sub) ? h.sub : "overview";
  if (TABS.some((x) => x.id === h.tab)) state.tab = h.tab;
  if (gadmin()) {
    const hit = state.groups.find((x) => x.id === h.ref || x.token === h.ref);
    state.g = hit ? hit.id : state.g && state.groups.some((x) => x.id === state.g) ? state.g : state.groups.length ? state.groups[0].id : null;
  }
  if (!h.chat && !h.settings && !h.board && h.id) state.detail = { type: /^G-/.test(h.id) ? "goal" : /^I-/.test(h.id) ? "idea" : "task", id: h.id };
  else state.detail = null;
}

async function loadMe() {
  const h = parseHash();
  state.ref = h.ref;
  const me = await api("GET", "/api/me");
  state.me = me;
  if (me.now) state.skew = me.now - Date.now() / 1000;
  state.badLink = me.role === "none" ? linkProblem(h) : "";
  if (me.role === "member" || me.role === "group_admin") state.g = me.group;
}

// 没登录却带着地址进来：管理员 / 群管理员的地址是群号、设置或聊天 → 登录过期（"expired"）；
// 群友的地址是链接码 → 链接失效（"link"）；什么都没带 → ""。
export const linkProblem = (h) => (h.settings || h.chat || h.board || /^\d+$/.test(h.ref || "") ? "expired" : h.ref ? "link" : "");

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
    if (state.loadErr) delete state.loadErr.view;
    const i = state.groups.findIndex((x) => x.id === id);
    if (i >= 0) state.groups[i] = Object.assign({}, state.groups[i], pickSummary(v));
  } catch (e) {
    // 记下原因：没内容时原地显示「加载失败 + 重试」，有旧内容时保留旧内容并提示连接中断
    if (state.g === id) (state.loadErr = state.loadErr || {}).view = e.message;
    if (!quietly) toast(e.message, true);
    if (e.status === 401 || e.status === 403) return reboot();
  }
}

const pickSummary = (v) => ({ name: v.name, icon: v.icon, members: v.members, fresh: v.fresh, quiet: v.quiet, today: v.today, read_since: v.read_since });

export async function loadSettings() {
  if (!admin()) return;
  try {
    state.settings = await api("GET", "/api/settings");
    if (state.loadErr) delete state.loadErr.settings;
    await loadAttention();
  } catch (e) {
    (state.loadErr = state.loadErr || {}).settings = e.message;
    toast(e.message, true);
  }
}

// 各块「加载失败」里的「重试」（loading(原因, what)）：按 what 重新取那一块，再重画
export async function retryLoad(el) {
  const what = el.dataset.what;
  el.disabled = true;
  if (what === "trusted") {
    // 优质来源按群懒加载：删掉这个群的记录，重画时 sources.js 会重新去取
    if (state.trusted) delete state.trusted[el.dataset.g];
  } else if (what === "view") await loadView();
  else if (what === "settings") await loadSettings();
  else if (what === "rules") await loadRules();
  else if (what === "ext") await loadExt();
  else if (what === "usage") await Promise.all([loadUsage(), loadLogSummary()]);
  else if (what === "identity") await loadIdentity();
  else if (what === "chat") await loadChat(state.chatId, false);
  else if (what === "board") await loadBoard();
  ui.flash = true;
  render();
  if (state.sheet) repaintSheet();
}

export async function reboot() {
  resetSocial();
  state.loadErr = {};
  state.view = null;
  state.settings = null;
  state.groupControls = null;
  state.agents = null;
  state.agentSnapshot = null;
  state.agentEdit = null;
  state.agentLoadSeq = (state.agentLoadSeq || 0) + 1;
  state.tasks = {};
  state.board = null;
  loadTheme(); // 主题和音效：不挡开页，拿不到就沿用首屏缓存的
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
        if (state.page === "settings" && state.setSub === "appearance") await loadAppearance();
        if (state.page === "settings" && ["social"].includes(state.setSub)) await loadSocial();
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
  if (admin() && state.page === "board") enterBoard();
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
  else if (state.setSub === "overview") await loadAttention();
  if (state.setSub === "extensions") await loadExt();
  if (state.setSub === "agents") await loadAgents();
  if (state.setSub === "models") await loadModels();
  if (state.setSub === "rules") await loadRules();
  if (state.setSub === "usage") await Promise.all([loadUsage(), loadLogSummary()]);
  if (["memory", "agents"].includes(state.setSub)) await loadIdentity();
  if (state.setSub === "links") await loadGA();
  if (state.setSub === "appearance") await loadAppearance();
  if (["social"].includes(state.setSub)) await loadSocial();
  if (state.page === "settings") {
    renderView();
    renderSide();
    renderRail();
  }
}

export async function enterBoard() {
  if (!admin()) return;
  closeSheet();
  go({ page: "board", detail: null });
  await loadBoard();
  if (state.page === "board") {
    renderView();
    renderSide();
  }
}

export function go(patch) {
  const gChanged = patch.g && patch.g !== state.g;
  Object.assign(state, patch);
  if (gChanged) {
    state.view = null;
    if (state.loadErr) delete state.loadErr.view; // 上一个群的失败不带到新群
  }
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

/* 看板：页面在前台且停在看板时每 10 秒刷新；内容没变不重画 */
setInterval(() => {
  if (document.hidden || state.page !== "board" || !admin()) return;
  Promise.all([loadBoard(), state.sheet === "board-item" ? loadBoardItem(true) : null]).then(() => {
    if (state.page !== "board") return;
    renderView({ poll: true });
    if (state.sheet === "board-item") repaintSheet();
  });
}, 10000);

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
