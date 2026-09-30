// MaiWork 网页 · 设置：导航、概览、群链接、群管理员。
import { state } from "../state.js";
import { SVG, esc, gface, ico, tokens } from "../util.js";
import { api, gname, mq, platTag, quiet } from "../api.js";
import { loading } from "../pages/news.js";
import { extPage } from "./ext.js";
import { agentsPage } from "./agents.js";
import { rulesPage } from "./rules.js";
import { usagePage } from "./usage.js";
import { sourcesPage } from "./sources.js";
import { identityPage } from "./identity.js";
import { logsPage } from "./logs.js";
import { fullLink, modelsPage } from "./models.js";
import { updateSection } from "../update.js";

/* ───────────── 设置 / 群切换（抽屉） ───────────── */

export function groupPicker() {
  return `<h1 class="h-page">切换群</h1>${state.groups
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
  ["overview", "总览", "gear", "运行状态和今天的用量"],
  ["identity", "身份", "lotus", "性格、规矩和记忆"],
  ["models", "模型", "robot", "用哪些模型"],
  ["extensions", "扩展", "tools", "联网搜索、MCP 和 skill"],
  ["agents", "专岗", "fish", "职责、各群经验和工作交接"],
  ["usage", "用量", "chart", "每天用了多少"],
  ["sources", "资讯来源", "newspaper", "RSS、优质来源和屏蔽的"],
  ["links", "群链接", "link", "群友看到的专属链接"],
  ["rules", "全部配置", "moon", "所有设置项"],
  ["logs", "请求日志", "memo", "出问题时看这里"],
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
  const body = { overview: settingsOverview, models: modelsPage, extensions: extPage, agents: agentsPage, usage: usagePage, sources: sourcesPage, links: linksPage, rules: rulesPage, identity: identityPage, logs: logsPage }[sub[0]];
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
        (g) => `<div class="set-row">${ico("link")}<div><div class="set-name">${esc(g.name || `群 ${g.id}`)}</div><div class="set-text mono-link">${esc(fullLink(g))}</div></div><span class="row-btns"><button class="btn small" data-act="copy" data-link="${esc(fullLink(g))}">复制</button><button class="btn small" data-act="reset-link" data-g="${esc(g.id)}">重置</button></span></div>
        <div class="set-row ga-row">${ico("lock")}<div><div class="set-name">群管理员</div><div class="set-text">${esc(gaText(state.ga[g.id]))}</div></div><span class="row-btns"><button class="btn small" data-act="ga-edit" data-g="${esc(g.id)}" data-name="${esc(g.name || `群 ${g.id}`)}">设置</button></span></div>`
      )
      .join("") || `<p class="h-meta">还没有服务群。</p>`}
    <p class="fine">群友用链接只能看到自己的群。链接外泄了就点「重置」。群管理员用自己的密码登录，只能管本群。</p>`;
}

// 群管理员一行的状态文字
function gaText(x) {
  if (!x) return "…";
  const n = (x.accounts || []).length;
  if (!x.password_set && !n) return "没设置";
  return [x.password_set ? "网页密码已设置" : "没有网页密码", n ? `群里 ${n} 人能批准` : ""].filter(Boolean).join(" · ");
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

function settingsOverview(s) {
  const u = (s.usage && s.usage.today) || {};
  const alert = (s.usage && s.usage.alert_daily_tokens) || 0;
  const used = (u.main || 0) + (u.worker || 0);
  const r = s.rules || {};
  return `
    ${(s.problems || []).length ? `<div class="warn-box"><b>配置里有几处问题，已经先跳过：</b><br />${s.problems.map(esc).join("<br />")}</div>` : ""}
    <h2 class="h-sub" style="margin-top:20px">各个群</h2>
    ${
      state.groups.length
        ? state.groups
            .map(
              (g) => `
      <button class="gpick" data-act="group" data-g="${esc(g.id)}">
        ${gface(g)}
        <span class="gpick-t"><span class="gpick-name" style="display:block;font-size:16px">${mq(gname(g))}</span>
        <span class="gpick-sub">${g.fresh ? "刚开始服务 · 还在熟悉" : `今天 ${g.today.news} 条资讯 · 开话题 ${g.today.topics} 次 · 待批 ${g.today.pending}`}</span></span>
        <span class="chev" style="color:var(--gray-2)">${SVG.right}</span>
      </button>`
            )
            .join("")
        : `<p class="h-meta">还没有服务群 <button type="button" class="link-btn" data-act="cfg-goto" data-s="groups">去添加</button></p>`
    }
    ${s.models && !s.models.ready ? `<div class="warn-box" style="margin-top:18px">还没配好模型，MaiWork 暂时不会工作<button class="btn small" data-act="set-sub" data-sub="models" style="margin-left:8px">去配</button></div>` : ""}
    <div class="h-sub-row"><h2 class="h-sub">今天的用量</h2><button class="btn small" data-act="set-sub" data-sub="usage">看以前的</button></div>
    <div class="usage">
      <div><b>${tokens(u.main)}</b><span>主模型 · tokens</span></div>
      <div><b>${tokens(u.worker)}</b><span>子 agent · tokens</span></div>
      <div><b>${u.jev || 0}</b><span>Jev 判断 · 次</span></div>
    </div>
    ${
      ((s.usage && s.usage.alerts) || []).length
        ? `<div class="warn-box"><b>今天超过提醒线了：</b><br />${s.usage.alerts.map((a) => esc(a.text)).join("<br />")}</div>`
        : ""
    }
    <p class="fine">${alert ? `每日提醒线 ${tokens(alert)} tokens，今天用了 ${Math.round((used / alert) * 100)}%。` : "没设提醒线。"}${u.errors ? ` 今天有 ${u.errors} 次调用出错。` : ""}</p>
    ${updateSection()}
    <h2 class="h-sub">运行状态</h2>
    ${(s.health || []).map((h) => `<div class="set-row">${ico(h.icon || "gear")}<div><div class="set-name">${esc(h.name)}</div><div class="set-text">${esc(h.text)}</div>${h.copy ? `<div class="set-text mono-link">${esc(h.copy)}</div><div class="actions" style="margin-top:8px"><button class="btn small" data-act="copy" data-link="${esc(h.copy)}" data-what="公钥">复制公钥</button></div>` : ""}</div><span class="dot ${h.state === "ok" ? "ok" : h.state === "warn" ? "pending" : ""}"></span></div>`).join("")}
    <div class="actions" style="margin-top:22px"><button class="btn" data-act="onb-restart">重新引导</button><button class="btn" data-act="logout">退出管理员</button></div>`;
}
