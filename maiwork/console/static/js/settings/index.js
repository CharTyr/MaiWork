// MaiWork 网页 · 设置：导航、概览、群链接、群管理员。
import { state } from "../state.js";
import { SVG, esc, gface, ico, tokens } from "../util.js";
import { api, gname, mq, platTag, quiet } from "../api.js";
import { loading } from "../pages/news.js";
import { extPage } from "./ext.js";
import { agentsPage } from "./agents.js";
import { rulesPage } from "./rules.js";
import { usageAndLogsPage } from "./usage.js";
import { sourcesPage } from "./sources.js";
import { memoryPage } from "./identity.js";
import { fullLink, modelsPage } from "./models.js";
import { updateSection } from "../update.js";

/* ───────────── 设置 / 群切换（抽屉） ───────────── */

export function groupPicker() {
  return `<h1 class="h-page">换个群</h1>${state.groups
    .map(
      (g) => `
    <button class="gpick" data-act="group" data-g="${esc(g.id)}">
      ${gface(g)}
      <span class="gpick-t"><span class="gpick-name" style="display:block">${mq(gname(g))}${platTag(g)}</span><span class="gpick-sub">${g.members ? `${g.members} 人 · ` : ""}${esc(quiet(g).text)}</span></span>
      ${g.id === state.g ? SVG.check : ""}
    </button>`
    )
    .join("")}`;
}

export const SET_SUBS = [
  ["overview", "概况", "gear", "运行状态和今天用量"],
  ["models", "模型", "robot", "连接和挑选模型"],
  ["extensions", "工具", "tools", "联网搜索、MCP 和 skill"],
  ["agents", "专岗", "fish", "各岗位的模型、性格和规矩"],
  ["memory", "记忆", "books", "所有群通用的经验"],
  ["usage", "用量", "chart", "每天用量和出错记录"],
  ["sources", "来源", "newspaper", "订阅、优质和屏蔽的网站"],
  ["links", "群链接", "link", "群友链接和群管理员密码"],
  ["rules", "全部设置", "moon", "所有选项都在这"],
];

function setNav() {
  return `<div class="set-nav" role="tablist">${SET_SUBS.map(
    ([id, label]) => `<button role="tab" data-act="set-sub" data-sub="${id}" aria-selected="${state.setSub === id}">${label}</button>`
  ).join("")}</div>`;
}

export function settingsPage() {
  const s = state.settings;
  const sub = SET_SUBS.find((x) => x[0] === state.setSub) || SET_SUBS[0];
  const head = `<h1 class="h-page">${sub[0] === "overview" ? "设置" : esc(sub[1])}</h1>${setNav()}`;
  if (!s) return head + loading();
  const body = { overview: settingsOverview, models: modelsPage, extensions: extPage, agents: agentsPage, usage: usageAndLogsPage, sources: sourcesPage, links: linksPage, rules: rulesPage, memory: memoryPage }[sub[0]];
  return head + `<div class="set-body">${body(s)}</div>`;
}

export function settingsSide() {
  const s = state.settings || {};
  return `
    <h2 class="h-sub" style="margin-top:6px">设置</h2>
    ${SET_SUBS.map(
      ([id, label, icon, hint]) => `
      <button class="gpick side-set${state.setSub === id ? " on" : ""}" data-act="set-sub" data-sub="${id}">
        ${ico(icon)}<span><span class="gpick-name" style="display:block;font-size:16px">${label}</span><span class="gpick-sub">${hint}</span></span>
        ${id === "models" && s.models && !s.models.ready ? `<span class="r-badge" style="position:static">!</span>` : `<span></span>`}
      </button>`
    ).join("")}`;
}

function linksPage(s) {
  return `
    ${(s.groups || [])
      .map(
        (g) => `<div class="set-row">${ico("link")}<div><div class="set-name">${esc(g.name || `群 ${g.id}`)}</div><div class="set-text mono-link">${esc(fullLink(g))}</div></div><span class="row-btns"><button class="btn small" data-act="copy" data-link="${esc(fullLink(g))}">复制</button><button class="btn small" data-act="reset-link" data-g="${esc(g.id)}">换新链接</button></span></div>
        <div class="set-row ga-row">${ico("lock")}<div><div class="set-name">群管理员</div><div class="set-text">${esc(gaText(state.ga[g.id]))}</div></div><span class="row-btns"><button class="btn small" data-act="ga-edit" data-g="${esc(g.id)}" data-name="${esc(g.name || `群 ${g.id}`)}">设置</button></span></div>`
      )
      .join("") || `<p class="h-meta">还没有服务群。</p>`}
    <p class="fine">链接只能看本群；外泄了就换新链接。</p>`;
}

// 群管理员一行的状态文字
function gaText(x) {
  if (!x) return "…";
  return x.password_set ? "已设密码" : "还没设密码";
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
  const miss = (o.missing || []).map((k) => ({ models: "模型", groups: "服务的群" })[k] || k);
  const text = o.usable ? "引导还剩几步可选" : `引导还差：${miss.join("、")}`;
  return `<div class="warn-box" style="margin-top:18px">${esc(text)}<button class="btn small" data-act="onb-continue" style="margin-left:8px">继续</button></div>`;
}

// 今天用量的补充说明：没报用量的调用、缓存命中、重试——只在数不是 0 时才说，不显示金额
function usageExtra(u) {
  const bits = [];
  if (u.unknown_calls) bits.push(`${u.unknown_calls} 次没报用量，未计入`);
  if (u.cache_read) bits.push(`缓存省了 ${tokens(u.cache_read)} tokens`);
  if (u.retries) bits.push(`重试 ${u.retries} 次`);
  return bits.length ? " " + bits.join("；") + "。" : "";
}

function settingsOverview(s) {
  const u = (s.usage && s.usage.today) || {};
  const alert = (s.usage && s.usage.alert_daily_tokens) || 0;
  const used = (u.main || 0) + (u.worker || 0);
  return `
    ${(s.problems || []).length ? `<div class="warn-box"><b>有几项设置不对，先跳过了：</b><br />${s.problems.map(esc).join("<br />")}</div>` : ""}
    <h2 class="h-sub" style="margin-top:20px">各群</h2>
    ${
      state.groups.length
        ? state.groups
            .map(
              (g) => `
      <button class="gpick" data-act="group" data-g="${esc(g.id)}">
        ${gface(g)}
        <span class="gpick-t"><span class="gpick-name" style="display:block;font-size:16px">${mq(gname(g))}</span>
        <span class="gpick-sub">${g.fresh ? "刚加入 · 正在了解" : `今天：资讯 ${g.today.news} · 话题 ${g.today.topics} · 待批 ${g.today.pending}`}</span></span>
        <span class="chev" style="color:var(--gray-2)">${SVG.right}</span>
      </button>`
            )
            .join("")
        : `<p class="h-meta">还没有群 <button type="button" class="link-btn" data-act="cfg-goto" data-s="groups">去添加</button></p>`
    }
    ${onbBanner(s.onboarding)}
    ${s.models && !s.models.ready ? `<div class="warn-box" style="margin-top:18px">还没配模型，MaiWork 没法干活<button class="btn small" data-act="set-sub" data-sub="models" style="margin-left:8px">去配置</button></div>` : ""}
    <div class="h-sub-row"><h2 class="h-sub">用量</h2><button class="btn small" data-act="set-sub" data-sub="usage">查看</button></div>
    <p class="h-meta">每天用量和出错记录</p>
    ${
      ((s.usage && s.usage.alerts) || []).length
        ? `<div class="warn-box"><b>今天用量超了提醒线：</b><br />${s.usage.alerts.map((a) => esc(a.text)).join("<br />")}</div>`
        : ""
    }
    <p class="fine">${alert ? `提醒线 ${tokens(alert)} tokens，已用 ${Math.round((used / alert) * 100)}%。` : "没设提醒线。"}${u.errors ? ` 今天出错 ${u.errors} 次。` : ""}${usageExtra(u)}</p>
    ${updateSection()}
    <h2 class="h-sub">运行状态</h2>
    ${(s.health || []).map((h) => `<div class="set-row">${ico(h.icon || "gear")}<div><div class="set-name">${esc(h.name)}</div><div class="set-text">${esc(h.text)}</div>${h.copy ? `<div class="set-text mono-link">${esc(h.copy)}</div><div class="actions" style="margin-top:8px"><button class="btn small" data-act="copy" data-link="${esc(h.copy)}" data-what="公钥">复制公钥</button></div>` : ""}</div><span class="dot ${h.state === "ok" ? "ok" : h.state === "warn" ? "pending" : ""}"></span></div>`).join("")}
    <div class="actions" style="margin-top:22px"><button class="btn" data-act="onb-restart">重新引导</button><button class="btn" data-act="logout">退出登录</button></div>`;
}
