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
      <div class="chip">聊天</div>
      <div class="chip-sub">管理员</div>
      <button class="round-btn" data-act="tab" data-tab="${state.tab}" aria-label="返回">${SVG.close}</button>`;
    return;
  }
  if (state.page === "settings") {
    $("top").innerHTML = `
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="${esc(botName())}" />
      <div class="chip">设置</div>
      <div class="chip-sub">管理员</div>
      <button class="round-btn" data-act="tab" data-tab="${state.tab}" aria-label="返回">${SVG.close}</button>`;
    return;
  }
  const g = grp();
  const q = quiet(g);
  $("top").innerHTML = `
    <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="${esc(botName())}" />
    ${admin() && state.groups.length > 1 ? `<button class="chip" data-act="groups" aria-label="切换群，当前为 ${esc(gname(g))}">${mq(gname(g))}${SVG.down}</button>` : `<div class="chip">${mq(gname(g))}</div>`}
    <div class="chip-sub"><span class="dot ${q.live ? "ok" : ""}"></span>${esc(q.text)}</div>
    ${
      admin()
        ? `<button class="round-btn left" data-act="chat" aria-label="聊天">${SVG.chat}</button><button class="round-btn" data-act="settings" aria-label="设置">${SVG.sliders}</button>`
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
      <div class="brand-t"><div class="brand-name">MaiWork</div><div class="brand-sub">${admin() ? "管理员 · 所有群" : isGA() ? "群管理员" : esc(botName()) + " 的工作台"}</div></div>
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
      ${admin() ? `<button class="r-item" data-act="chat" title="聊天" aria-current="${state.page === "chat"}">${SVG.chat}<span class="r-text"><span class="r-name">聊天</span></span></button>` : ""}
      ${
        admin()
          ? `<button class="r-item" data-act="settings" title="设置" aria-current="${state.page === "settings"}">${SVG.sliders}<span class="r-text"><span class="r-name">设置</span></span>${state.settings && state.settings.models && !state.settings.models.ready ? `<span class="r-badge">!</span><span class="r-dot"></span>` : hasUpdate() ? `<span class="r-badge upd">有更新</span><span class="r-dot upd"></span>` : ""}</button>`
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
    $("side").innerHTML = `<button class="side-close" data-act="side-close" aria-label="关闭">${SVG.close}</button>${detailHTML()}`;
    return;
  }
  const v = gview();
  const t = g.today || {};
  const up = (v && v.upcoming) || [];
  $("side").innerHTML = `
    ${pulseCard(g, v, true)}
    <h2 class="h-sub">今日</h2>
    <div class="stats">
      <button class="stat" data-act="tab" data-tab="news"><b>${t.news || 0}</b><span>条资讯</span></button>
      <button class="stat" data-act="tab" data-tab="group"><b>${t.topics || 0}</b><span>次开启话题</span></button>
      <button class="stat" data-act="tab" data-tab="tasks"><b>${t.pending || 0}</b><span>${gadmin() ? "件待审批" : "件等待审批"}</span></button>
      <button class="stat" data-act="tab" data-tab="tasks"><b>${t.running || 0}</b><span>件进行中</span></button>
    </div>
    ${
      up.length
        ? `<h2 class="h-sub">日程</h2>${up.map((u) => `<div class="next">${ico(upcomingIcon(u))}<div><div class="next-time">${esc(when(u.at))}</div><div class="next-text">${esc(u.text)}</div></div></div>`).join("")}`
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
// 分栏的「样子签名」：按钮的动作、参数、文字都一样，只是选中项不同 → 可以沿用旧分栏
const tabSig = (bar) =>
  [...bar.querySelectorAll("button")].map((b) => `${b.dataset.act}|${b.dataset.sub || ""}|${b.dataset.s || ""}|${b.textContent}`).join("\n");
function tabBars() {
  const out = {};
  for (const sel of TAB_SCROLLERS) {
    const el = document.querySelector(`#view ${sel}`);
    if (el) out[sel] = el;
  }
  return out;
}
// 整页重画后，把新分栏换回旧的那个 DOM，只改选中项：
// 旧分栏的滚动位置和正在进行的平滑滚动都不中断，选中底色也能过渡，不会「先跳回去再滑过来」（2026-10-15 修抽搐）
function keepTabBars(old) {
  const kept = new Set();
  for (const sel of TAB_SCROLLERS) {
    const o = old[sel];
    const nu = document.querySelector(`#view ${sel}`);
    if (!o || !nu || o === nu || tabSig(o) !== tabSig(nu)) continue;
    const ob = o.querySelectorAll("button");
    nu.querySelectorAll("button").forEach((b, i) => ob[i].setAttribute("aria-selected", b.getAttribute("aria-selected") || "false"));
    nu.replaceWith(o);
    kept.add(sel);
  }
  return kept;
}
// 自己用 requestAnimationFrame 滑：不依赖浏览器原生平滑滚动（刚挂回页面的元素上各家表现不一，
// 会出现「先跳回原位再滑」的抽搐）；同一个分栏再点一次就从当前位置接着滑，不叠两段动画
const tabAnim = new WeakMap();
function slideTabs(bar, target) {
  cancelAnimationFrame(tabAnim.get(bar) || 0);
  const start = bar.scrollLeft;
  const dist = target - start;
  const t0 = performance.now();
  const dur = Math.min(420, 220 + Math.abs(dist) * 0.4);
  const step = (now) => {
    // rAF 给的帧时间可能早于 t0（同一帧里开的动画），不夹住会先往反方向退几像素
    const k = Math.max(0, Math.min(1, (now - t0) / dur));
    bar.scrollLeft = start + dist * (1 - Math.pow(1 - k, 3));
    if (k < 1) tabAnim.set(bar, requestAnimationFrame(step));
    else tabAnim.delete(bar);
  };
  tabAnim.set(bar, requestAnimationFrame(step));
}
function centerTabs(was) {
  for (const sel of TAB_SCROLLERS) {
    const bar = document.querySelector(`#view ${sel}`);
    if (!bar || bar.scrollWidth <= bar.clientWidth) continue;
    const on = bar.querySelector('[aria-selected="true"]');
    // 分栏离开过页面（整页重画时摘下又挂回），浏览器会把它的横向位置清零：先原样放回再滑
    const from = was && sel in was ? was[sel] : null;
    if (from != null) bar.scrollLeft = from;
    if (!on) continue;
    const max = bar.scrollWidth - bar.clientWidth;
    const br = bar.getBoundingClientRect();
    const r = on.getBoundingClientRect();
    const center = bar.scrollLeft + (r.left - br.left) + r.width / 2;
    const target = Math.max(0, Math.min(max, center - bar.clientWidth / 2));
    if (Math.abs(target - bar.scrollLeft) < 2) continue;
    // 第一次出现（没有旧位置）直接就位；之后平滑滑过去，减少动态效果时也直接就位
    if (from == null || calm()) bar.scrollLeft = target;
    else slideTabs(bar, target);
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
    const old = tabBars();
    lastView = null;
    $("view").innerHTML = settingsPage();
    keepTabBars(old);
    centerTabs(was);
    ui.flash = false;
    return;
  }
  const g = grp();
  const v = gview();
  const views = { news: viewNews, ideas: viewIdeas, tasks: viewTasks, group: viewGroup };
  const el = $("view");
  const fail = (state.loadErr || {}).view;
  // 没内容：其他页显示加载中；取失败了（含「群」页）原地显示原因和重试。
  // 有旧内容但最近一次刷新失败：内容照旧，顶部提示连接中断（不让人以为数据还是最新的）
  const body =
    !v && (state.tab !== "group" || fail)
      ? `<h1 class="h-page">${TABS.find((t) => t.id === state.tab).label}</h1>${loading(fail, "view")}`
      : (v && fail ? `<div class="warn-box stale-note" role="status">连接中断，正在重试。当前显示的可能不是最新内容。</div>` : "") + views[state.tab](g, v);
  const html = updateBanner() + body;
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
      ${
        bad === "expired"
          ? `<div class="warn-box" style="width:100%;text-align:left">登录已过期，请重新登录。</div>`
          : bad
            ? `<div class="warn-box" style="width:100%;text-align:left">链接已失效。请在群内发送 <b>/mw 网页</b> 获取新链接。</div>`
            : ""
      }
      <div class="landing-box">
        <div class="landing-t">群成员</div>
        <p>在群内发送 <b>/mw 网页</b> 获取本群链接。</p>
      </div>
      <button class="btn primary" data-act="login" style="margin-top:20px;height:48px;padding:0 28px">管理员登录</button>
    </div>`;
}

function noGroups() {
  return `
    <div class="landing">
      <img class="top-avatar" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" style="width:120px;height:120px" />
      <h1 class="h-page" style="margin-top:22px">暂无群</h1>
      <p class="landing-text">请先在设置中添加群</p>
      <button class="btn primary" data-act="settings" style="height:48px;padding:0 28px">前往设置</button>
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
    document.title = state.page === "chat" ? "聊天 · MaiWork" : "设置 · MaiWork";
    return;
  }
  const label = TABS.find((t) => t.id === state.tab).label;
  document.title = `${label} · ${gname(grp())} · MaiWork`;
}
