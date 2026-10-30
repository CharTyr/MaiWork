// MaiWork 网页 · 看板（只总管理员，只看不动）。
// 每个专岗一栏平铺：上面「进行中」，下面「历史」（最近 N 小时）。点任意一件 → 抽屉里看完整过程：
// 工具调用（名字 / 输入 / 输出 / 耗时 / 成败）、思考（模型每次回复的文字）、状态变化。
// 数据：GET /api/board（列表，10 秒刷新）、GET /api/board/items/{source}/{id}（过程，?after= 增量）。
import { admin, state } from "./state.js";
import { dur, esc, hhmm, msText, when } from "./util.js";
import { api } from "./api.js";
import { fishSvg } from "./fish.js";
import { loading } from "./pages/news.js";

/* ───────────── 取数 ───────────── */

let seq = 0;
export async function loadBoard() {
  if (!admin()) return;
  const my = ++seq;
  try {
    const b = await api("GET", "/api/board");
    if (my !== seq) return;
    state.board = b;
    if (state.loadErr) delete state.loadErr.board;
  } catch (e) {
    if (my !== seq) return;
    (state.loadErr = state.loadErr || {}).board = e.message;
  }
}

// 过程明细：第一次取全量，之后按最后一步的时间增量取；换了别的条目就作废旧请求
let itemSeq = 0;
export async function loadBoardItem(more) {
  const cur = state.boardItem;
  if (!cur) return;
  const my = ++itemSeq;
  const steps = (more && cur.data && cur.data.steps) || [];
  const after = more && steps.length ? steps[steps.length - 1].ts : null;
  try {
    const q = after != null ? `?after=${encodeURIComponent(after)}` : "";
    const d = await api("GET", `/api/board/items/${encodeURIComponent(cur.source)}/${encodeURIComponent(cur.id)}${q}`);
    if (my !== itemSeq || state.boardItem !== cur) return;
    if (after != null && cur.data) {
      const seen = new Set(steps.map(stepKey));
      cur.data = { ...d, steps: steps.concat((d.steps || []).filter((s) => !seen.has(stepKey(s)))) };
    } else cur.data = d;
    cur.err = "";
  } catch (e) {
    if (my !== itemSeq || state.boardItem !== cur) return;
    cur.err = e.message;
  }
}
// 展开过的「输入 / 输出」在刷新后保持展开
document.addEventListener(
  "toggle",
  (e) => {
    const d = e.target;
    if (!d.classList || !d.classList.contains("st-io")) return;
    (state.boardOpen = state.boardOpen || {})[d.dataset.i] = d.open;
  },
  true
);
const stepKey = (s) => `${s.ts}|${s.type}|${s.tool || ""}|${(s.text || s.input || "").slice(0, 40)}`;

/* ───────────── 小零件 ───────────── */

const fishOf = (kind, size, opts = {}) => {
  const lane = ((state.board && state.board.lanes) || []).find((l) => l.kind === kind);
  const seed = lane && typeof lane.fish_seed === "string" ? lane.fish_seed : "";
  return `<span class="board-fish" aria-hidden="true">${fishSvg(`agent:${kind}:${seed}`, { size, ...opts })}</span>`;
};
const gName = (x) => x.group_name || x.group_id || "";
// 点开的那件来自 data-id（字符串），列表里资讯 / 构想 / 话题 / 主模型的 id 是数字：按字符串比
const sameItem = (a, source, id) => a.source === source && String(a.id) === String(id);
const dotOf = (x) => (x.stuck ? "waiting" : !x.finished && x.ok == null ? "running" : x.ok === false ? "failed" : x.ok === true ? "done" : "");

function counts(x) {
  const c = x.counts || {};
  const bits = [];
  if (c.tools) bits.push(`工具 ${c.tools}`);
  if (c.thinks) bits.push(`思考 ${c.thinks}`);
  return bits.join(" · ");
}

function card(lane, x, live) {
  const now = (state.board && state.board.now) || 0;
  const quiet = now - (x.updated || x.started || now);
  const sel = state.boardItem && sameItem(state.boardItem, x.source, x.id);
  const meta = live
    ? `已做 ${dur(x.elapsed_s)}${x.stuck ? ` · <b class="warn-t">${dur(quiet)}没有进展</b>` : ""}`
    : `${esc(when(x.finished || x.updated))} · 用时 ${dur(x.elapsed_s)}`;
  return `<button class="bcard${live ? " live" : ""}${sel ? " selected" : ""}" data-act="board-item" data-src="${esc(x.source)}" data-id="${esc(x.id)}" data-kind="${esc(lane.kind)}">
    <div class="bcard-top"><span class="dot ${dotOf(x)}"></span><span class="bcard-st">${esc(x.status_text || x.status || "")}</span><span class="bcard-g">${esc(gName(x))}</span></div>
    <div class="bcard-t">${esc(x.title || "（无标题）")}</div>
    ${live && x.last_step && x.last_step.text ? `<div class="bcard-last"><span>${esc(hhmm(x.last_step.ts))}</span>${esc(x.last_step.text)}</div>` : ""}
    <div class="bcard-m">${meta}${counts(x) ? ` · ${counts(x)}` : ""}</div>
  </button>`;
}

function laneHTML(lane, f) {
  const keep = (x) => !f || x.group_id === f;
  const run = (lane.running || []).filter(keep);
  const hist = (lane.history || []).filter(keep);
  const c = lane.counts || {};
  return `<section class="lane${run.length ? " busy" : ""}" aria-label="${esc(lane.kind_title)}">
    <header class="lane-h">
      ${fishOf(lane.kind, 40, run.length ? {} : { still: true })}
      <div class="lane-ht"><b>${esc(lane.kind_title || lane.kind)}</b><span>${run.length ? `进行中 ${run.length}` : "空闲"}${c.done ? ` · 完成 ${c.done}` : ""}${c.failed ? ` · <span class="bad-t">失败 ${c.failed}</span>` : ""}</span></div>
    </header>
    ${lane.kind === "main" && !run.length && !hist.length ? `<div class="lane-empty">这里显示主模型的独立判断（是否接活、管理员对话等）；它在任务里的派活和验收记在对应任务的过程中。</div>` : ""}
    <div class="lane-sec">进行中</div>
    ${run.length ? run.map((x) => card(lane, x, true)).join("") : `<div class="lane-empty">没有在做的事</div>`}
    <div class="lane-sec">历史</div>
    ${hist.length ? hist.map((x) => card(lane, x, false)).join("") : `<div class="lane-empty">这段时间没有记录</div>`}
  </section>`;
}

/* ───────────── 页面 ───────────── */

export function boardPage() {
  const b = state.board;
  const fail = (state.loadErr || {}).board;
  let html = `<div class="board-head"><h1 class="h-page">看板</h1>`;
  if (!b) return html + `</div>` + loading(fail, "board");
  const lanes = b.lanes || [];
  const groups = [];
  for (const l of lanes) for (const x of [...(l.running || []), ...(l.history || [])]) if (x.group_id && !groups.some((g) => g.id === x.group_id)) groups.push({ id: x.group_id, name: gName(x) });
  const f = state.boardFilter && groups.some((g) => g.id === state.boardFilter) ? state.boardFilter : "";
  const busy = lanes.reduce((n, l) => n + (l.running || []).filter((x) => !f || x.group_id === f).length, 0);
  html += `<p class="h-meta">进行中 ${busy} 件 · 历史为最近 ${esc(b.hours)} 小时 · 每 10 秒刷新，仅供查看</p>`;
  if (groups.length > 1)
    html += `<div class="filters" role="group" aria-label="按群筛选">
      <button class="filter" data-act="board-filter" data-g="" aria-pressed="${!f}">全部群</button>
      ${groups.map((g) => `<button class="filter" data-act="board-filter" data-g="${esc(g.id)}" aria-pressed="${f === g.id}">${esc(g.name)}</button>`).join("")}
    </div>`;
  html += `</div>`;
  if (fail) html += `<div class="warn-box stale-note" role="status">连接中断，正在重试。当前显示的可能不是最新内容。</div>`;
  html += lanes.length ? `<div class="lanes">${lanes.map((l) => laneHTML(l, f)).join("")}</div>` : `<p class="h-meta">暂无专岗</p>`;
  return html;
}

/* ───────────── 过程抽屉 ───────────── */

const clip = (s) => esc(String(s == null ? "" : s));

function stepHTML(s, i) {
  const t = `<span class="st-time">${esc(hhmm(s.ts))}</span>`;
  const who = [s.actor, s.model].filter(Boolean).map(esc).join(" · ");
  if (s.type === "tool") {
    const open = state.boardOpen && state.boardOpen[i];
    return `<li class="st st-tool${s.ok === false ? " bad" : ""}">
      <div class="st-row">${t}<span class="st-badge tool">工具</span><b class="st-name">${clip(s.tool)}</b><span class="st-sub">${s.ms ? esc(msText(s.ms)) : ""}${s.ok === false ? ` · <span class="bad-t">失败</span>` : ""}${who ? ` · ${who}` : ""}</span></div>
      <details class="st-io"${open ? " open" : ""} data-i="${i}"><summary>输入 / 输出</summary>
        ${s.input ? `<div class="st-l">输入</div><pre>${clip(s.input)}</pre>` : ""}
        ${s.output ? `<div class="st-l">输出</div><pre>${clip(s.output)}</pre>` : ""}
        ${s.error ? `<div class="st-l">错误</div><pre class="bad-t">${clip(s.error)}</pre>` : ""}
      </details>
    </li>`;
  }
  if (s.type === "think")
    return `<li class="st st-think${s.ok === false ? " bad" : ""}">
      <div class="st-row">${t}<span class="st-badge think">思考</span><span class="st-sub">${who}${s.ms ? ` · ${esc(msText(s.ms))}` : ""}${s.ok === false ? ` · <span class="bad-t">失败</span>` : ""}</span></div>
      ${s.reasoning ? `<details class="st-io st-reason"${state.boardOpen && state.boardOpen["r" + i] ? " open" : ""} data-i="r${i}"><summary>思考过程</summary><pre class="st-text st-why">${clip(s.reasoning)}</pre></details>` : s.reasoning_redacted ? `<div class="st-sub st-why-note">思考过程已由模型加密，无法查看</div>` : ""}
      ${s.text ? `<pre class="st-text">${clip(s.text)}</pre>` : ""}
      ${s.error ? `<pre class="st-text bad-t">${clip(s.error)}</pre>` : ""}
    </li>`;
  return `<li class="st st-status"><div class="st-row">${t}<span class="st-badge">状态</span><span class="st-sub">${clip(s.text)}</span></div></li>`;
}

export function boardItemSheet() {
  const cur = state.boardItem;
  if (!cur) return "";
  const d = cur.data;
  const it = (d && d.item) || boardBriefOf(cur.source, cur.id) || {};
  const steps = (d && d.steps) || [];
  const tools = steps.filter((s) => s.type === "tool").length;
  const thinks = steps.filter((s) => s.type === "think").length;
  const lane = ((state.board && state.board.lanes) || []).find((l) => l.kind === cur.kind);
  return `<div class="bi">
    <div class="bi-h">${fishOf(cur.kind, 44)}<div><div class="bi-k">${esc((lane && lane.kind_title) || cur.kind || "")} · ${esc(gName(it))}</div>
      <h1 class="h-page">${esc(it.title || "（无标题）")}</h1></div></div>
    <div class="bi-meta"><span class="dot ${dotOf(it)}"></span>${esc(it.status_text || it.status || "")}${it.started ? ` · 开始 ${esc(when(it.started))}` : ""}${it.finished ? ` · 结束 ${esc(when(it.finished))}` : ""} · 用时 ${dur(it.elapsed_s)}</div>
    ${d && d.link === "time_window" ? `<div class="warn-box">这类工作没有直接记下归属，下面的过程是按同群、同时段推断的，可能混入同一时间的其他工作。</div>` : ""}
    ${cur.err ? `<div class="warn-box">${esc(cur.err)}</div>` : ""}
    <h2 class="h-sub">过程 <small>${steps.length} 步 · 工具 ${tools} · 思考 ${thinks}</small></h2>
    ${!d && !cur.err ? loading() : steps.length ? `<ol class="steps">${steps.map(stepHTML).join("")}</ol>` : `<p class="h-meta">暂无过程记录。</p>`}
    ${steps.length >= 500 ? `<p class="h-meta">只显示前 500 步。</p>` : ""}
  </div>`;
}

// 列表里这件的最新概况（明细还没取回时先用它）
export function boardBriefOf(source, id) {
  for (const l of (state.board && state.board.lanes) || []) for (const x of [...(l.running || []), ...(l.history || [])]) if (sameItem(x, source, id)) return x;
  return null;
}
