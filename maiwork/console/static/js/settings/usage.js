// MaiWork 网页 · 设置 ·「用量」。
import { state } from "../state.js";
import { esc, toast, tokens } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";
import { ROLE_NAMES } from "./ext.js";
import { fmtMs, logsPage } from "./logs.js";

/* ───── 用量：按天看历史 ───── */
export async function loadUsage(days) {
  const u = (state.usage = state.usage || { days: 30, sel: "" });
  if (days) u.days = days;
  try {
    u.hist = await api("GET", `/api/usage/history?days=${u.days}`);
    const list = (u.hist && u.hist.days) || [];
    if (!u.sel || !list.some((d) => d.day === u.sel)) u.sel = list.length ? list[list.length - 1].day : "";
    if (u.sel) await loadUsageDay(u.sel);
  } catch (e) {
    u.hist = null;
    toast(e.message, true);
  }
}
export async function loadUsageDay(day) {
  const u = state.usage;
  u.sel = day;
  u.detail = null;
  try {
    u.detail = await api("GET", `/api/usage/history?date=${encodeURIComponent(day)}`);
    if (u.detail && !u.detail.day) u.detail.day = day;
  } catch (e) {
    toast(e.message, true);
  }
}

// 「9/28」：去掉前导 0，柱子下面放得下
const md = (day) => {
  const m = String(day || "").match(/^\d{4}-(\d{2})-(\d{2})$/);
  return m ? `${Number(m[1])}/${Number(m[2])}` : String(day || "");
};

export function usageAndLogsPage() {
  return usagePage() + `<h2 class="h-sub">请求日志</h2>` + logsPage();
}

export function usagePage() {
  const u = state.usage;
  if (!u || !u.hist) return loading();
  const list = u.hist.days || [];
  const t = u.hist.totals || {};
  const max = Math.max(1, ...list.map((d) => (d.main || 0) + (d.worker || 0)));
  // 日期标签隔几根柱子标一个（最右一根总是标），不然柱子太窄会把字截掉
  const step = list.length <= 7 ? 1 : list.length <= 14 ? 2 : list.length <= 31 ? 5 : 15;
  const bars = list
    .map((d, i) => {
      const showX = (list.length - 1 - i) % step === 0;
      const tot = (d.main || 0) + (d.worker || 0);
      const hm = ((d.main || 0) / max) * 100;
      const hw = ((d.worker || 0) / max) * 100;
      return `<button type="button" class="ub${u.sel === d.day ? " on" : ""}" data-act="usage-day" data-d="${esc(d.day)}" title="${esc(d.day)} · ${tokens(tot)} tokens · ${d.calls || 0} 次调用${d.errors ? ` · ${d.errors} 次出错` : ""}">
        <span class="ub-col"><span class="ub-w" style="height:${hw}%"></span><span class="ub-m" style="height:${hm}%"></span></span>
        <span class="ub-x">${showX ? esc(md(d.day)) : "&nbsp;"}</span></button>`;
    })
    .join("");
  return `
    <div class="h-sub-row"><h2 class="h-sub" style="margin-top:14px">最近 ${u.days} 天</h2>
      <span class="seg" role="tablist" style="margin:0">${[7, 14, 30].map((n) => `<button role="tab" aria-selected="${u.days === n}" data-act="usage-range" data-n="${n}">${n} 天</button>`).join("")}</span></div>
    <div class="usage usage-4">
      <div><b>${tokens(t.main)}</b><span>主模型 · tokens</span></div>
      <div><b>${tokens(t.worker)}</b><span>子 agent · tokens</span></div>
      <div><b>${t.calls || 0}</b><span>模型调用 · 次${t.errors ? `（出错 ${t.errors}）` : ""}</span></div>
      <div><b>${t.jev || 0}</b><span>Jev 判断 · 次</span></div>
    </div>
    <div class="ubars" style="--n:${list.length}">${bars}</div>
    <p class="fine"><span class="lg lg-m"></span>主模型 <span class="lg lg-w"></span>子 agent</p>
    <label class="u-pick"><span>直接看某一天</span><input type="date" class="u-date" value="${esc(u.sel || "")}" min="${esc((list[0] || {}).day || "")}" max="${esc((list[list.length - 1] || {}).day || "")}" /></label>
    ${usageDay(u)}`;
}

function usageDay(u) {
  const d = u.detail;
  if (!u.sel) return "";
  if (!d) return `<h2 class="h-sub">${esc(u.sel)}</h2>` + loading();
  const tbl = (head, rows) =>
    rows.length ? `<div class="utbl" style="--c:${head.length - 1}"><div class="utr uth">${head.map((h) => `<span>${h}</span>`).join("")}</div>${rows.map((r) => `<div class="utr">${r.map((c) => `<span>${c}</span>`).join("")}</div>`).join("")}</div>` : `<p class="h-meta">没有。</p>`;
  const hours = d.by_hour || [];
  const hmax = Math.max(1, ...hours);
  return `
    <h2 class="h-sub">${esc(d.day)} 的明细</h2>
    <div class="uhours">${hours.map((n, h) => `<span title="${h} 点 · ${tokens(n)} tokens" style="height:${Math.max(n ? 6 : 2, (n / hmax) * 100)}%"></span>`).join("")}</div>
    <div class="uhours-x"><span>0 点</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
    <h3 class="h-mini">按模型</h3>
    ${tbl(["模型", "调用", "输入", "输出", "出错", "平均耗时"], (d.by_model || []).map((m) => [`<span class="mono">${esc(m.model || "?")}</span> <small>${esc(ROLE_NAMES[m.role] || m.role || "")}</small>`, m.calls || 0, tokens(m.prompt), tokens(m.completion), m.errors || 0, fmtMs(m.avg_ms)]))}
    <h3 class="h-mini">按用途</h3>
    ${tbl(["用途", "调用", "tokens"], (d.by_purpose || []).map((p) => [esc(p.purpose || "其他"), p.calls || 0, tokens(p.tokens)]))}
    <h3 class="h-mini">按群</h3>
    ${tbl(["群", "调用", "tokens"], (d.by_group || []).map((g) => [esc(g.name || (g.group_id ? `群 ${g.group_id}` : "不属于哪个群")), g.calls || 0, tokens(g.tokens)]))}
    <p class="fine">这天 Jev 判断了 ${d.jev || 0} 次。</p>`;
}
