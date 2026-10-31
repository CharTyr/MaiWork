// MaiWork 网页 · 设置：6 项一级导航 + 页内二级分栏、概览、群链接与群管理员。
import { state } from "../state.js";
import { SVG, esc, gface, ico, tip, tokens } from "../util.js";
import { api, gname, mq, platTag, quiet } from "../api.js";
import { repaintSheet } from "../sheet.js";
import { loading } from "../pages/news.js";
import { extPage } from "./ext.js";
import { optDepsSection } from "./optdeps.js";
import { agentsPage } from "./agents.js";
import { jevSection } from "./jev.js";
import { rulesPage } from "./rules.js";
import { usagePage } from "./usage.js";
import { logsPage } from "./logs.js";
import { sourcesPage } from "./sources.js";
import { memoryPage } from "./identity.js";
import { fullLink, modelsPage } from "./models.js";
import { updateSection } from "../update.js";
import { attentionSection } from "./attention.js";
import { appearancePage } from "./appearance.js";
import { socialPage } from "./social.js";

/* ───────────── 设置 / 群切换（抽屉） ───────────── */

export function groupPicker() {
  return `<h1 class="h-page">切换群</h1>${state.groups
    .map(
      (g) => `
    <button class="gpick" data-act="group" data-g="${esc(g.id)}">
      ${gface(g)}
      <span class="gpick-t"><span class="gpick-name">${mq(gname(g))}${platTag(g)}</span><span class="gpick-sub">${g.members ? `${g.members} 人 · ` : ""}${esc(quiet(g).text)}</span></span>
      ${g.id === state.g ? SVG.check : ""}
    </button>`
    )
    .join("")}`;
}

export const SET_SUBS = [
  ["overview", "概况", "gear", "运行状态和今日用量"],
  ["models", "模型与专岗", "robot", "端点、模型库、专岗和快速判断"],
  ["extensions", "工具与平台", "tools", "联网搜索、社交平台、MCP 和组件"],
  ["sources", "群与来源", "newspaper", "群链接、群管理员和资讯来源"],
  ["usage", "用量", "chart", "每日统计和调用记录"],
  ["rules", "系统", "moon", "外观、通用记忆和全部设置"],
];

export const SET_TABS = {
  models: [
    ["models", "模型库"],
    ["agents", "专岗与人格"],
    ["jev", "快速判断"],
  ],
  extensions: [
    ["search", "搜索与平台"],
    ["mcp", "MCP 与 skill"],
    ["optdeps", "可选组件"],
  ],
  sources: [
    ["links", "群链接与群管"],
    ["feeds", "资讯来源"],
  ],
  usage: [
    ["usage", "用量统计"],
    ["logs", "调用记录"],
  ],
  rules: [
    ["appearance", "外观"],
    ["memory", "通用记忆"],
    ["config", "全部设置"],
  ],
};

export function activeSetTab(sub) {
  const list = SET_TABS[sub];
  if (!list || !list.length) return "";
  const cur = (state.setTab || {})[sub];
  return list.some(([id]) => id === cur) ? cur : list[0][0];
}

function setNav() {
  return `<div class="set-nav" role="tablist">${SET_SUBS.map(
    ([id, label]) => `<button role="tab" data-act="set-sub" data-sub="${id}" aria-selected="${state.setSub === id}">${label}</button>`
  ).join("")}</div>`;
}

function setSubTabs(sub) {
  const list = SET_TABS[sub];
  if (!list || !list.length) return "";
  const cur = activeSetTab(sub);
  return `<div class="seg set-seg" role="tablist">${list.map(
    ([id, label]) => `<button type="button" role="tab" data-act="set-tab" data-sub="${sub}" data-tab="${id}" aria-selected="${cur === id}">${label}</button>`
  ).join("")}</div>`;
}

function renderModelsSection() {
  const t = activeSetTab("models");
  if (t === "agents") return agentsPage();
  if (t === "jev") return jevSection();
  return modelsPage({ omitJev: true });
}

function renderExtSection() {
  const t = activeSetTab("extensions");
  if (t === "mcp") return extPage("mcp");
  if (t === "optdeps") return state.ext ? optDepsSection() : extPage("optdeps");
  return extPage("search") + socialPage();
}

function renderSourcesSection(s) {
  return activeSetTab("sources") === "feeds" ? sourcesPage(s) : linksPage(s);
}

function renderUsageSection() {
  if (activeSetTab("usage") === "logs") {
    return `<h2 class="h-sub h-sub-tight">调用记录${tip("近 3 天的模型和工具调用记录，可按需展开查看明细。")}</h2>${logsPage()}`;
  }
  return usagePage();
}

function renderSystemSection() {
  const t = activeSetTab("rules");
  if (t === "memory") return memoryPage();
  if (t === "config") return rulesPage();
  return appearancePage();
}

export function settingsPage() {
  const s = state.settings;
  const sub = SET_SUBS.find((x) => x[0] === state.setSub) || SET_SUBS[0];
  const head = `<h1 class="h-page">${sub[0] === "overview" ? "设置" : esc(sub[1])}</h1>${setNav()}`;
  if (!s) return head + loading((state.loadErr || {}).settings, "settings");
  const body = {
    overview: settingsOverview,
    models: renderModelsSection,
    extensions: renderExtSection,
    sources: renderSourcesSection,
    usage: renderUsageSection,
    rules: renderSystemSection,
  }[sub[0]];
  return head + `<div class="set-body">${setSubTabs(sub[0])}${body(s)}</div>`;
}

export function settingsSide() {
  const s = state.settings || {};
  return `
    <h2 class="h-sub">设置</h2>
    ${SET_SUBS.map(
      ([id, label, icon, hint]) => `
      <button class="gpick side-set${state.setSub === id ? " on" : ""}" data-act="set-sub" data-sub="${id}">
        ${ico(icon)}<span class="gpick-t"><span class="gpick-name">${label}</span><span class="gpick-sub">${hint}</span></span>
        ${id === "models" && s.models && !s.models.ready ? `<span class="r-badge">!</span>` : `<span></span>`}
      </button>`
    ).join("")}`;
}

export async function actSettingsNav(a, el) {
  if (a !== "set-tab") return false;
  const sub = el.dataset.sub || state.setSub;
  const tab = el.dataset.tab;
  if (!sub || !tab || !SET_TABS[sub]) return false;
  state.setTab = state.setTab || {};
  state.setTab[sub] = tab;
  if (state.setSub !== sub) state.setSub = sub;
  repaintSheet();
  return true;
}

function linksPage(s) {
  const groups = s.groups || [];
  if (!groups.length) return `<p class="h-meta">暂无服务群</p>`;
  return `
    ${groups
      .map(
        (g) => `<div class="set-group">
          <div class="set-group-head">${gface(g)}<span class="set-name">${esc(g.name || `群 ${g.id}`)}</span></div>
          <div class="set-row">${ico("link")}<div><div class="set-name">群成员链接</div><div class="set-text mono-link">${esc(fullLink(g))}</div></div><span class="row-btns"><button class="btn small" data-act="copy" data-link="${esc(fullLink(g))}">复制</button><button class="btn small" data-act="reset-link" data-g="${esc(g.id)}">重置链接</button></span></div>
          <div class="set-row ga-row">${ico("lock")}<div><div class="set-name">群管理员</div><div class="set-text">${esc(gaText(state.ga[g.id]))}</div></div><span class="row-btns"><button class="btn small" data-act="ga-edit" data-g="${esc(g.id)}" data-name="${esc(g.name || `群 ${g.id}`)}">设置</button></span></div>
        </div>`
      )
      .join("")}
    <div class="fine">群链接仅能查看对应群的内容。${tip("链接泄露后，请重置链接；旧链接将立即失效。")}</div>`;
}

// 群管理员一行的状态文字
function gaText(x) {
  if (!x) return "…";
  return x.password_set ? "已设置密码" : "未设置密码";
}

export async function loadGA() {
  const gs = (state.settings && state.settings.groups) || [];
  await Promise.all(
    gs.map((g) =>
      api("GET", `/api/groups/${encodeURIComponent(g.id)}/group-admin`)
        .then((r) => (state.ga[g.id] = r))
        .catch(() => (state.ga[g.id] = { password_set: false, accounts: [] }))
    )
  );
}

// 引导没走完 / 先存下了：概况顶上给「接着引导」（回到第一项没配好的必要步骤）
function onbBanner(o) {
  if (!o || !["in_progress", "later"].includes(o.state)) return "";
  const miss = (o.missing || []).map((k) => ({ models: "模型", groups: "服务群" })[k] || k);
  const text = o.usable ? "初始设置还有可选步骤未完成" : `初始设置未完成：${miss.join("、")}`;
  return `<div class="warn-box warn-gap">${esc(text)}<button class="btn small warn-act" data-act="onb-continue">继续</button></div>`;
}

// 今天用量的补充说明：没报用量的调用、缓存命中、重试——只在数不是 0 时才说，不显示金额
function usageExtra(u) {
  const bits = [];
  if (u.unknown_calls) bits.push(`${u.unknown_calls} 次调用未返回用量，未计入`);
  if (u.cache_read) bits.push(`缓存命中 ${tokens(u.cache_read)} tokens`);
  if (u.retries) bits.push(`重试 ${u.retries} 次`);
  return bits.length ? " " + bits.join("；") + "。" : "";
}

function settingsOverview(s) {
  const u = (s.usage && s.usage.today) || {};
  const alert = (s.usage && s.usage.alert_daily_tokens) || 0;
  const used = (u.main || 0) + (u.worker || 0);
  return `
    ${(s.problems || []).length ? `<div class="warn-box"><b>以下设置有误，已暂时忽略：</b><br />${s.problems.map(esc).join("<br />")}</div>` : ""}
    <h2 class="h-sub h-sub-tight">各群</h2>
    ${
      state.groups.length
        ? `<div class="set-group">${state.groups
            .map(
              (g) => `
      <button class="gpick" data-act="group" data-g="${esc(g.id)}">
        ${gface(g)}
        <span class="gpick-t"><span class="gpick-name">${mq(gname(g))}</span>
        <span class="gpick-sub">${g.fresh ? "新加入 · 正在建立群画像" : `今日：资讯 ${g.today.news} · 话题 ${g.today.topics} · 待审批 ${g.today.pending}${g.today.asks_review ? ` · 待复核 ${g.today.asks_review}` : ""}`}</span></span>
        <span class="chev">${SVG.right}</span>
      </button>`
            )
            .join("")}</div>`
        : `<p class="h-meta">暂无群 <button type="button" class="link-btn" data-act="cfg-goto" data-s="groups">添加</button></p>`
    }
    ${onbBanner(s.onboarding)}
    ${attentionSection()}
    ${s.models && !s.models.ready ? `<div class="warn-box warn-gap">未配置模型，MaiWork 无法运行<button class="btn small warn-act" data-act="set-sub" data-sub="models">前往设置</button></div>` : ""}
    <div class="h-sub-row"><h2 class="h-sub">用量</h2><button class="btn small" data-act="set-sub" data-sub="usage">查看</button></div>
    ${
      ((s.usage && s.usage.alerts) || []).length
        ? `<div class="warn-box"><b>今日用量已超过提醒线：</b><br />${s.usage.alerts.map((a) => esc(a.text)).join("<br />")}</div>`
        : ""
    }
    <p class="fine">${alert ? `提醒线 ${tokens(alert)} tokens，已使用 ${Math.round((used / alert) * 100)}%。` : "未设置提醒线。"}${u.errors ? ` 今日错误 ${u.errors} 次。` : ""}${usageExtra(u)}</p>
    ${updateSection()}
    <h2 class="h-sub">运行状态</h2>
    <div class="set-group">${(s.health || []).map((h) => `<div class="set-row">${ico(h.icon || "gear")}<div><div class="set-name">${esc(h.name)}</div><div class="set-text">${esc(h.text)}</div>${h.copy ? `<div class="set-text mono-link">${esc(h.copy)}</div><div class="actions"><button class="btn small" data-act="copy" data-link="${esc(h.copy)}" data-what="公钥">复制公钥</button></div>` : ""}</div><span class="dot ${h.state === "ok" ? "ok" : h.state === "warn" ? "pending" : ""}"></span></div>`).join("")}</div>
    <div class="actions set-foot-acts"><button class="btn" data-act="onb-restart">重新进行初始设置</button><button class="btn" data-act="logout">退出登录</button></div>`;
}
