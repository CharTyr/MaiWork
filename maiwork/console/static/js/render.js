// MaiWork 网页 · 整页渲染：顶栏、底栏、左栏、右栏、主视图。
import { $, TABS, admin, desktop, gadmin, isGA, state, ui } from "./state.js";
import { SVG, calm, esc, gface, ico, when } from "./util.js";
import { gname, grp, gview, mq, platTag, quiet } from "./api.js";
import { pulseCard } from "./pulse.js";
import { loading, viewNews } from "./pages/news.js";
import { viewIdeas } from "./pages/ideas.js";
import { viewTasks } from "./pages/tasks.js";
import { viewGroup } from "./pages/group.js";
import { detailHTML } from "./detail.js";
import { settingsPage, settingsSide } from "./settings/index.js";
import { chatPage, chatSide } from "./chat.js";
import { hasUpdate, updateBanner } from "./update.js";

/* ───────────── 渲染 ───────────── */

export const avatar = () => (state.me && state.me.bot && state.me.bot.avatar) || "/static/assets/logo.png";
const botName = () => (state.me && state.me.bot && state.me.bot.name) || "MaiBot";

export function renderTop() {
  if (state.page === "chat") {
    $("top").innerHTML = `
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="${esc(botName())}" />
      <div class="chip">和 MaiWork 聊</div>
      <div class="chip-sub">管理员</div>
      <button class="round-btn" data-act="tab" data-tab="${state.tab}" aria-label="回到群">${SVG.close}</button>`;
    return;
  }
  if (state.page === "settings") {
    $("top").innerHTML = `
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="${esc(botName())}" />
      <div class="chip">设置</div>
      <div class="chip-sub">管理员</div>
      <button class="round-btn" data-act="tab" data-tab="${state.tab}" aria-label="回到群">${SVG.close}</button>`;
    return;
  }
  const g = grp();
  const q = quiet(g);
  $("top").innerHTML = `
    <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="${esc(botName())}" />
    ${admin() && state.groups.length > 1 ? `<button class="chip" data-act="groups" aria-label="切换群，当前：${esc(gname(g))}">${mq(gname(g))}${SVG.down}</button>` : `<div class="chip">${mq(gname(g))}</div>`}
    <div class="chip-sub"><span class="dot ${q.live ? "ok" : ""}"></span>${esc(q.text)}</div>
    ${
      admin()
        ? `<button class="round-btn left" data-act="chat" aria-label="和 MaiWork 聊">${SVG.chat}</button><button class="round-btn" data-act="settings" aria-label="设置">${SVG.sliders}</button>`
        : `<button class="round-btn" data-act="login" aria-label="${isGA() ? "群管理员" : "管理员登录"}">${SVG.key}</button>`
    }`;
}

const pendingCount = (g) => (g && g.today ? g.today.pending || 0 : 0);

export function renderTabbar() {
  $("tabbar").style.setProperty("--tab-count", TABS.length);
  const g = grp();
  const idx = state.page ? -1 : TABS.findIndex((t) => t.id === state.tab);
  const n = pendingCount(g);
  $("tabbar").innerHTML =
    `<span class="tab-pill" style="transform:translateX(${Math.max(0, idx) * 100}%);opacity:${idx < 0 ? 0 : 1}"></span>` +
    TABS.map(
      (t) => `
      <button class="tab" data-act="tab" data-tab="${t.id}" aria-current="${idx >= 0 && t.id === state.tab}">
        ${SVG[t.id]}<span class="sr">${t.label}</span>
        ${gadmin() && t.id === "tasks" && n ? `<span class="badge">${n}</span>` : ""}
      </button>`
    ).join("");
}

export function renderRail() {
  const g = grp();
  const list = admin() ? state.groups : g ? [g] : [];
  const n = pendingCount(g);
  $("rail").innerHTML = `
    <div class="brand">
      <img src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="${esc(botName())}" />
      <div class="brand-t"><div class="brand-name">MaiWork</div><div class="brand-sub">${admin() ? "管理员 · 全部群" : isGA() ? "群管理员" : esc(botName()) + "的后台"}</div></div>
    </div>
    ${admin() ? `<div class="rail-label">群</div>` : ""}
    ${list
      .map(
        (x) => `
      <button class="r-item" data-act="group" data-g="${esc(x.id)}" aria-current="${x.id === state.g}" title="${esc(gname(x))}">
        ${gface(x)}
        <span class="r-text"><span class="r-name">${mq(gname(x))}${platTag(x)}</span><span class="r-sub">${esc(quiet(x).text)}</span></span>
        ${gadmin() && pendingCount(x) ? `<span class="r-badge">${pendingCount(x)}</span><span class="r-dot"></span>` : ""}
      </button>`
      )
      .join("")}
    <div class="rail-sep"></div>
    <div class="rail-label">${mq(gname(g))}</div>
    ${TABS.map(
      (t) => `
      <button class="r-item" data-act="tab" data-tab="${t.id}" aria-current="${!state.page && t.id === state.tab}" title="${t.label}">
        ${SVG[t.id]}<span class="r-text"><span class="r-name">${t.label}</span></span>
        ${gadmin() && t.id === "tasks" && n ? `<span class="r-badge">${n}</span><span class="r-dot"></span>` : ""}
      </button>`
    ).join("")}
    <div class="rail-foot">
      ${admin() ? `<button class="r-item" data-act="chat" title="和 MaiWork 聊" aria-current="${state.page === "chat"}">${SVG.chat}<span class="r-text"><span class="r-name">和 MaiWork 聊</span></span></button>` : ""}
      ${
        admin()
          ? `<button class="r-item" data-act="settings" title="设置" aria-current="${state.page === "settings"}">${SVG.sliders}<span class="r-text"><span class="r-name">设置</span></span>${state.settings && state.settings.models && !state.settings.models.ready ? `<span class="r-badge">!</span><span class="r-dot"></span>` : hasUpdate() ? `<span class="r-badge upd">新</span><span class="r-dot upd"></span>` : ""}</button>`
          : `<button class="r-item" data-act="login" title="${isGA() ? "群管理员" : "管理员"}">${SVG.key}<span class="r-text"><span class="r-name">${isGA() ? "群管理员" : "管理员"}</span></span></button>`
      }
    </div>`;
}

function upcomingIcon(u) {
  return u.icon || "calendar";
}

export function renderSide() {
  if (!desktop.matches) return;
  if (state.page === "chat") {
    $("side").innerHTML = chatSide();
    return;
  }
  if (state.page === "settings") {
    $("side").innerHTML = settingsSide();
    return;
  }
  const g = grp();
  if (!g) {
    $("side").innerHTML = "";
    return;
  }
  if (state.detail) {
    $("side").innerHTML = `<button class="side-close" data-act="side-close" aria-label="关闭详情">${SVG.close}</button>${detailHTML()}`;
    return;
  }
  const v = gview();
  const t = g.today || {};
  const up = (v && v.upcoming) || [];
  $("side").innerHTML = `
    ${pulseCard(g, v, true)}
    <h2 class="h-sub">今天</h2>
    <div class="stats">
      <button class="stat" data-act="tab" data-tab="news"><b>${t.news || 0}</b><span>条资讯</span></button>
      <button class="stat" data-act="tab" data-tab="group"><b>${t.topics || 0}</b><span>次开话题</span></button>
      <button class="stat" data-act="tab" data-tab="tasks"><b>${t.pending || 0}</b><span>${gadmin() ? "件等你批准" : "件等批准"}</span></button>
      <button class="stat" data-act="tab" data-tab="tasks"><b>${t.running || 0}</b><span>件在做</span></button>
    </div>
    ${
      up.length
        ? `<h2 class="h-sub">接下来</h2>${up.map((u) => `<div class="next">${ico(upcomingIcon(u))}<div><div class="next-time">${esc(when(u.at))}</div><div class="next-text">${esc(u.text)}</div></div></div>`).join("")}`
        : ""
    }`;
}

// 横向滚动的分栏（设置子页、全部配置的分节）：重绘前记下滚到哪，重绘后从原位置
// 平滑滚到「选中项居中」——两边被盖住的项能露出来，也不会每点一下就跳回最左
const TAB_SCROLLERS = [".set-nav", ".cfg-jump"];
function tabScrolls() {
  const out = {};
  for (const sel of TAB_SCROLLERS) {
    const el = document.querySelector(`#view ${sel}`);
    if (el) out[sel] = el.scrollLeft;
  }
  return out;
}
function centerTabs(was) {
  for (const sel of TAB_SCROLLERS) {
    const bar = document.querySelector(`#view ${sel}`);
    if (!bar || bar.scrollWidth <= bar.clientWidth) continue;
    const on = bar.querySelector('[aria-selected="true"]');
    const from = was && sel in was ? was[sel] : null;
    if (from != null) bar.scrollLeft = from;
    if (!on) continue;
    const max = bar.scrollWidth - bar.clientWidth;
    const br = bar.getBoundingClientRect();
    const r = on.getBoundingClientRect();
    const center = bar.scrollLeft + (r.left - br.left) + r.width / 2;
    const target = Math.max(0, Math.min(max, center - bar.clientWidth / 2));
    if (Math.abs(target - bar.scrollLeft) < 2) continue;
    // 第一次出现（没有旧位置）直接就位；之后用平滑滚动，减少动态效果时也直接就位
    bar.scrollTo({ left: target, behavior: from == null || calm() ? "auto" : "smooth" });
  }
}

// 同一群编辑时，刷新可以更新内容，但不能把键盘焦点和光标赶走。
function captureGroupInput(el) {
  const active = document.activeElement;
  if (!active || !active.id || !el.contains(active) || !active.matches("input, textarea, select")) return null;
  const scope = active.closest(".groupctl-slot, .gctx-slot");
  if (!scope || scope.dataset.g !== state.g) return null;
  let start = null, end = null;
  try { start = active.selectionStart; end = active.selectionEnd; } catch (_) { /* number / checkbox 没文字光标 */ }
  return { id: active.id, start, end };
}

function restoreGroupInput(saved, el) {
  if (!saved) return;
  const input = $(saved.id);
  if (!input || !el.contains(input) || input.disabled) return;
  input.focus({ preventScroll: true });
  if (typeof saved.start === "number" && typeof saved.end === "number") {
    try { input.setSelectionRange(saved.start, saved.end); } catch (_) { /* 不支持文字光标的字段 */ }
  }
}

export function renderView(opts) {
  if (state.page === "chat") {
    const typed = $("chat-input") ? $("chat-input").value : "";
    lastView = null;
    $("view").innerHTML = chatPage();
    if (typed && $("chat-input")) $("chat-input").value = typed;
    ui.flash = false;
    return;
  }
  if (state.page === "settings") {
    const was = tabScrolls();
    lastView = null;
    $("view").innerHTML = settingsPage();
    centerTabs(was);
    ui.flash = false;
    return;
  }
  const g = grp();
  const v = gview();
  const views = { news: viewNews, ideas: viewIdeas, tasks: viewTasks, group: viewGroup };
  const el = $("view");
  const html = updateBanner() + (!v && state.tab !== "group" ? `<h1 class="h-page">${TABS.find((t) => t.id === state.tab).label}</h1>${loading()}` : views[state.tab](g, v));
  // 定时轮询时内容没变就不重画：不让图解 iframe 重新加载（一闪），也不打断正在看的页面。
  // 只限轮询（opts.poll）：点按钮之后的重画照旧整块重画，按钮状态会复位
  if (opts && opts.poll && html === lastView && !ui.flash) return;
  const focused = captureGroupInput(el);
  lastView = html;
  el.innerHTML = html;
  restoreGroupInput(focused, el);
  if (!ui.flash) el.querySelectorAll(".enter").forEach((x) => x.classList.remove("enter"));
  ui.flash = false;
}
let lastView = null;

function landing() {
  const bad = state.badLink;
  return `
    <div class="landing">
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" style="width:120px;height:120px" />
      <h1 class="h-page" style="margin-top:22px">MaiWork</h1>
      <p class="landing-text">${esc(botName())}在群里的后台：资讯、构想和在做的事都在这里。</p>
      ${bad ? `<div class="warn-box" style="width:100%;text-align:left">这个链接打不开了：可能是管理员重置过，或者复制时少了几个字。请在群里重新发 <b>/mw 网页</b> 拿新链接。</div>` : ""}
      <div class="landing-box">
        <div class="landing-t">群友</div>
        <p>在群里发 <b>/mw 网页</b>，${esc(botName())}会回一个本群专属链接，打开就能看到这个群的内容。</p>
      </div>
      <button class="btn primary" data-act="login" style="margin-top:20px;height:48px;padding:0 28px">管理员登录</button>
    </div>`;
}

function noGroups() {
  return `
    <div class="landing">
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" style="width:120px;height:120px" />
      <h1 class="h-page" style="margin-top:22px">还没有服务群</h1>
      <p class="landing-text">在设置里加上要服务的群</p>
      <button class="btn primary" data-act="settings" style="height:48px;padding:0 28px">打开设置</button>
    </div>`;
}

export function render() {
  const onSettings = admin() && (state.page === "settings" || state.page === "chat");
  const lone = !onSettings && (!state.me || state.me.role === "none" || (admin() && !state.groups.length));
  document.body.classList.toggle("no-link", lone);
  document.body.classList.toggle("on-chat", state.page === "chat");
  document.body.classList.remove("booting");
  if (lone) {
    $("view").innerHTML = admin() ? noGroups() : landing();
    document.title = "MaiWork";
    return;
  }
  renderTop();
  renderTabbar();
  renderRail();
  renderView();
  renderSide();
  if (onSettings) {
    document.title = state.page === "chat" ? "和 MaiWork 聊 · MaiWork" : "设置 · MaiWork";
    return;
  }
  const label = TABS.find((t) => t.id === state.tab).label;
  document.title = `${label} · ${gname(grp())} · MaiWork`;
}
