// MaiWork 网页 · 「在做的事」：待批准、任务、持续目标与提醒。
import { DOT, FILTERS, STATUS, gadmin, state } from "../state.js";
import { SVG, dur, esc, ico, when } from "../util.js";
import { agentFish } from "../api.js";
import { emptyState } from "./news.js";
import { findIdea, ideaItems } from "./ideas.js";
import { goalSections } from "./goals.js";

export function taskRow(t, i) {
  const sel = state.detail && state.detail.id === t.id ? " selected" : "";
  return `
    <button class="row enter${sel}" style="--i:${i}" data-act="task" data-id="${esc(t.id)}">
      ${ico(t.icon || "package")}
      <span>
        <span class="row-title">${esc(t.title)}</span>
        <span class="row-meta"><span class="dot ${DOT[t.status] !== undefined ? DOT[t.status] : t.status}"></span><span>${STATUS[t.status] || esc(t.status)}${t.meta ? ` · ${esc(t.meta)}` : ""}${t.auto_reason ? " · 自动审核通过" : ""}${t.undelivered ? ` · <b class="warn-t">做完了但还没发出去</b>` : ""}</span></span>
      </span>
      <span class="chev">${SVG.right}</span>
    </button>`;
}

// 来自构想的请求：列出这次要做的项目（没点名 = 全部）
function pendingItems(p) {
  if (p.source !== "idea" || !p.idea_id) return "";
  const it = findIdea(p.idea_id);
  const all = it ? ideaItems(it) : [];
  if (!all.length) return "";
  const want = Array.isArray(p.items) && p.items.length ? all.filter((x) => p.items.includes(x.no)) : all;
  if (!want.length) return "";
  return `<ul class="pend-items">${want.map((x) => `<li>${esc(x.title)}<span class="ii-kind">${x.kind === "goal" ? "目标" : "任务"}</span></li>`).join("")}</ul>`;
}

export function viewTasks(g, v) {
  let html = `<h1 class="h-page h-fish">${agentFish("task", 44)}<span>在做的事</span></h1>`;
  const tasks = (v && v.tasks) || { pending: [], list: [] };
  const pending = tasks.pending || [];
  const list = tasks.list || [];
  const botName = (state.me && state.me.bot && state.me.bot.name) || "MaiBot";
  const goalsHtml = goalSections(g, v);
  if (!pending.length && !list.length && !goalsHtml) {
    return html + emptyState("package", "还没有在做的事", `在群里 @${botName} 派活，或说「提醒我……」试试`);
  }
  let i = 0;
  if (pending.length) {
    html += `<h2 class="h-sub" style="margin-top:22px">${gadmin() ? "等你批准" : "等管理员批准"} <small>${pending.length} 件</small></h2>`;
    html += pending
      .map(
        (p) => `
        <article class="item ruled enter" style="--i:${i++}">
          ${ico(p.icon || "magnifier")}
          <div>
            <h3 class="item-title">${esc(p.title)}</h3>
            ${pendingItems(p)}
            ${p.quote ? `<div class="quote"><span class="quote-by">${esc(p.who)} · ${esc(when(p.ts))}</span>${esc(p.quote)}</div>` : ""}
            <div class="via">${esc(p.via || "")}${p.age_s > 86400 ? ` · <b class="warn-t">等了 ${dur(p.age_s)}</b>` : ""}</div>
            ${
              gadmin()
                ? `<div class="actions">
              <button class="btn primary" data-act="req" data-op="approve" data-id="${esc(p.id)}">批准</button>
              <button class="btn" data-act="req" data-op="reject" data-id="${esc(p.id)}">拒绝</button>
            </div>`
                : `<div class="note-ok"><span class="dot pending"></span>管理员批准后开工</div>`
            }
          </div>
        </article>`
      )
      .join("");
  }
  if (list.length) {
    html += `<h2 class="h-sub">全部任务</h2>`;
    html += `<div class="filters" role="group" aria-label="按状态筛选">${FILTERS.map((f) => {
      const n = list.filter(f.match).length;
      return `<button class="filter" data-act="filter" data-f="${f.id}" aria-pressed="${state.filter === f.id}">${f.label}<span class="n">${n}</span></button>`;
    }).join("")}</div>`;
    const f = FILTERS.find((x) => x.id === state.filter) || FILTERS[0];
    const rows = list.filter(f.match);
    html += rows.length ? `<div>${rows.map((t) => taskRow(t, i++)).join("")}</div>` : `<p class="h-meta" style="margin-top:18px">这个状态下没有任务，换个筛选看看。</p>`;
  }
  return html + goalsHtml;
}
