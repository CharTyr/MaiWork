// MaiWork 网页 · 「构想」页。
import { TONES, gadmin, state } from "../state.js";
import { SVG, dayWord, esc, ico } from "../util.js";
import { agentFish, gview } from "../api.js";
import { emptyState, fbButtons } from "./news.js";
import { hash } from "./group.js";

const IDEA_STATE = { new: "", wanted: "有人想要，等批准", pending: "等批准", started: "在做了", dismissed: "已收起" };
const ideaDot = (st) => (st === "started" ? "running" : st === "wanted" || st === "pending" ? "pending" : "");

export function viewIdeas(g, v) {
  const ideas = (v && v.ideas) || [];
  let html = `<h1 class="h-page h-fish">${agentFish("idea", 44)}<span>构想</span></h1><p class="h-meta">看中了就复制，到群里 @MaiBot 开工</p>`;
  html += blockedNote(v && v.ideas_blocked);
  if (!ideas.length) {
    return html + emptyState("bulb", "还没有构想", g.fresh ? "了解这个群后就会提" : "");
  }
  html += ideas
    .map((it, k) => {
      const did = `I-${it.id}`;
      const sel = state.detail && state.detail.id === did ? " selected" : "";
      const st = IDEA_STATE[it.state] || "";
      return `
      <article class="item ruled tap enter${sel}${it.state === "dismissed" ? " faded" : ""}" style="--i:${k}" data-act="idea-open" data-id="${esc(did)}" tabindex="0">
        ${ico(it.icon || "bulb")}
        <div>
          <h2 class="item-title">${esc(it.title)}</h2>
          <p class="item-body soft clamp3">${esc(it.body)}</p>
          ${st ? `<div class="status"><span class="dot ${ideaDot(it.state)}"></span><span class="status-text">${esc(st)}</span></div>` : ""}
          ${forWho(it.for_member)}
        </div>
      </article>`;
    })
    .join("");
  return html;
}

// 管理员才有 ideas_blocked：生成时被「MaiWork 做不到」拦下的构想（不入库，只留最近几条原因）
export function blockedNote(b) {
  const n = b && Number(b.count) > 0 ? Number(b.count) : 0;
  if (!n) return "";
  const rows = (Array.isArray(b.recent) ? b.recent : [])
    .map((r) => `<li><span class="ib-title">${esc(r.title || "（没有标题）")}${r.kind === "personal" ? `<span class="ii-kind">给个人</span>` : ""}</span><span class="ib-why">${esc(r.reason || "")}${r.ts ? ` · ${esc(dayWord(r.ts))}` : ""}</span></li>`)
    .join("");
  return `<details class="idea-blocked"><summary>7 天内拦下 ${n} 条做不到的<span class="private">${SVG.lock}仅管理员可见</span></summary>${rows ? `<ul>${rows}</ul>` : ""}<p class="fine">要人配合、付钱、登录或线下的不提</p></details>`;
}

export function findIdea(did) {
  const v = gview();
  const id = String(did || "").replace(/^I-/, "");
  return v && v.ideas ? v.ideas.find((x) => String(x.id) === id) : null;
}

// 复制出去的要求：到群里 @MaiBot 粘贴发送；末尾的「构想 #id」让 MaiWork 认出是哪条构想
// 构想里「包含的项目」默认全选；state.ideaOff[构想id] 记被取消勾选的序号
export function ideaItems(it) {
  return Array.isArray(it.items) ? it.items : [];
}
export function ideaPicked(it) {
  const off = (state.ideaOff && state.ideaOff[it.id]) || [];
  return ideaItems(it).filter((x) => !off.includes(x.no));
}

export function ideaAsk(it) {
  const lines = [`帮我做这个构想：${it.title}`];
  if (it.body) lines.push(it.body);
  const all = ideaItems(it);
  const picked = ideaPicked(it);
  picked.forEach((x) => lines.push(`${x.no}. ${x.title}`));
  const some = all.length && picked.length < all.length;
  lines.push(some ? `（构想 #${it.id}，要做：${picked.map((x) => x.no).join("、")}）` : `（构想 #${it.id}）`);
  return lines.join("\n");
}

function ideaItemsBlock(it, open) {
  const items = ideaItems(it);
  if (!items.length) return "";
  const picked = ideaPicked(it).map((x) => x.no);
  const row = (x) => {
    const on = picked.includes(x.no);
    const kind = x.kind === "goal" ? "目标" : "任务";
    const inner = `
        <span class="ii-mark">${open ? (on ? SVG.check : "") : ""}</span>
        <span class="ii-text"><span class="ii-title">${esc(x.title)}<span class="ii-kind">${kind}</span></span>${x.desc ? `<span class="ii-desc">${esc(x.desc)}</span>` : ""}</span>`;
    return open
      ? `<button class="ii-row${on ? " on" : ""}" data-act="idea-item" data-id="${it.id}" data-no="${x.no}" aria-pressed="${on}">${inner}</button>`
      : `<div class="ii-row static">${inner}</div>`;
  };
  return `<div class="dt-sec idea-items"><div class="dt-label">包含哪些${open && items.length > 1 ? ` <small>${picked.length}/${items.length}</small>` : ""}</div>${items.map(row).join("")}</div>`;
}

export function ideaDetail(it) {
  const st = IDEA_STATE[it.state] || "新的";
  const f = it.feasibility || {};
  const rows = [];
  if (f.note) rows.push([f.level === "ok" ? "能做" : f.level === "need" ? "要人帮" : "也许能做", f.note]);
  if (it.basis) rows.push(["为什么想到", it.basis]);
  const open = it.state === "new" || it.state === "wanted";
  const none = open && ideaItems(it).length > 0 && !ideaPicked(it).length;
  const more = state.ideaMore === it.id;
  const menu = more
    ? `<div class="idea-menu">
        <div class="idea-menu-row"><span>有用吗</span>${fbButtons("ideas", it)}</div>
        ${gadmin() && open && !none ? `<button class="idea-menu-btn" data-act="idea" data-op="do" data-id="${it.id}">${SVG.check}<span>直接开工</span></button>` : ""}
        ${gadmin() && open ? `<button class="idea-menu-btn danger" data-act="idea" data-op="dismiss" data-id="${it.id}">${SVG.close}<span>不再提</span></button>` : ""}
      </div>`
    : "";
  let main;
  if (none) main = `<button class="btn primary idea-go" disabled>至少勾一项</button>`;
  else if (open) main = `<button class="btn primary idea-go" data-act="idea-copy" data-id="${it.id}">${SVG.copy}复制要求</button>`;
  else if (it.state === "started" && it.task_id) main = `<button class="btn primary idea-go" data-act="task" data-id="${esc(it.task_id)}">看任务</button>`;
  else if (it.state === "pending" && gadmin()) main = `<button class="btn primary idea-go" data-act="tab" data-tab="tasks">去批准</button>`;
  else main = `<button class="btn idea-go" disabled>${esc(st)}</button>`;
  return `
    <div class="dt-head">
      ${ico(it.icon || "bulb")}
      <div>
        <h2 class="dt-title">${esc(it.title)}</h2>
        <div class="dt-state"><span class="dot ${ideaDot(it.state)}"></span>${esc(st)}${it.created_ts ? ` · ${esc(dayWord(it.created_ts))}想到的` : ""}</div>
      </div>
    </div>
    <div class="dt-sec"><div class="dt-text">${esc(it.body)}</div></div>
    ${ideaItemsBlock(it, open)}
    ${rows.length ? `<div class="dt-sec idea-rows"><div class="dt-label">补充</div>${rows.map(([k, t]) => `<div class="idea-row"><div class="idea-row-k">${esc(k)}</div><div class="idea-row-v">${esc(t)}</div></div>`).join("")}</div>` : ""}
    ${forWho(it.for_member)}
    <div class="idea-bar">
      ${menu}
      <div class="idea-bar-row">
        <button class="idea-more" data-act="idea-more" data-id="${it.id}" aria-expanded="${more}" aria-label="更多">${SVG.more}</button>
        ${main}
      </div>
      ${open ? `<p class="idea-how">复制后到群里 @MaiBot${gadmin() ? "" : "，批准后开工"}</p>` : ""}
      ${handoffBtn(it, none)}
    </div>`;
}

// 交接包入口（docs/24）：带走给自己的个人 agent；被带走次数只给管理员看
function handoffBtn(it, none) {
  const n = Number(it.handoff_count) || 0;
  const count = gadmin() && n > 0 ? `<span class="ho-count">被带走 ${n} 次</span>` : "";
  return `<div class="ho-entry"><button class="link-btn" data-act="handoff" data-kind="idea" data-id="${it.id}"${none ? " disabled" : ""}>交给我的 agent</button>${count}</div>`;
}

// 构想「给谁的」：指明了人就在卡片底部放头像 + 名字；面向全群的不放
function forWho(m) {
  if (!m || !m.user_id) return "";
  const name = String(m.name || "群友");
  return `<div class="for-who"><span class="face" style="background:${TONES[Math.abs(hash(m.user_id)) % TONES.length]}">${esc(name.slice(0, 1))}${m.avatar ? `<img src="${esc(m.avatar)}" alt="" loading="lazy" onerror="this.remove()" />` : ""}</span><span>给 <b>${esc(name)}</b></span></div>`;
}
