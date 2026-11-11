// MaiWork 网页 · 看板（只总管理员，只看不动）。
// 每个专岗一栏平铺：上面「进行中」，下面「历史」（最近 N 小时）。点任意一件 → 抽屉里看完整过程：
// 工具调用（名字 / 输入 / 输出 / 耗时 / 成败）、思考（模型每次回复的文字）、状态变化。
// 数据：GET /api/board（列表，10 秒刷新，可带 ?hours=）、GET /api/board/items/{source}/{id}（过程，?after= 增量）。
// 轮次总览、步骤快筛和工具一行摘要在 board_steps.js（纯函数）；点击动作都在本文件 actBoard()。
import { TABS, admin, desktop, state } from "./state.js";
import { dur, esc, hhmm, tip, when } from "./util.js";
import { api } from "./api.js";
import { fishSvg } from "./fish.js";
import { loading } from "./pages/news.js";
import { mergeSteps, stepsSectionHTML } from "./board_steps.js";
import { boardFlowHTML } from "./board_flow.js";
import { actCtxMap } from "./ctxmap.js";
import { loadTask } from "./detail.js";
import { closeSheet, openSheet, repaintSheet } from "./sheet.js";
import { go } from "./router.js";
import { renderView } from "./render.js";

/* ───────────── 取数 ───────────── */

let seq = 0;
export async function loadBoard() {
  if (!admin()) return;
  const my = ++seq;
  try {
    // state.boardHours 记住管理员选的时间窗；没选就用后端默认
    const q = state.boardHours ? `?hours=${encodeURIComponent(state.boardHours)}` : "";
    const b = await api("GET", `/api/board${q}`);
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
    // 增量：按 uid 去重并进旧步骤（board_steps.mergeSteps，含 500 步上限）
    cur.data = after != null && cur.data ? mergeSteps(cur.data, d) : d;
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
const isFail = (x) => x.ok === false || x.status === "failed" || x.status === "rejected";
const laneTitle = (kind) => (((state.board && state.board.lanes) || []).find((l) => l.kind === kind) || {}).kind_title || kind || "";
const who = (k) => (k === "main" ? "主模型" : k === "*" ? "所有岗" : laneTitle(k));

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
    ${x.error_brief && (!live || x.ok === false) ? `<div class="bcard-err">${esc(x.error_brief)}</div>` : ""}
    ${live && x.last_step && x.last_step.text ? `<div class="bcard-last"><span>${esc(hhmm(x.last_step.ts))}</span>${esc(x.last_step.text)}</div>` : ""}
    <div class="bcard-m">${meta}${counts(x) ? ` · ${counts(x)}` : ""}${x.handoff_count ? ` · ${x.handoff_count} 轮交接` : ""}${x.note_count ? ` · 便条 ${x.note_count}` : ""}</div>
  </button>`;
}

// 状态快筛：active = 进行中的（卡住也在 running 里）；failed = 没做成的；all = 全部
const statusKeep = (x, live, st) => (st === "active" ? live : st === "failed" ? isFail(x) : true);

function laneHTML(lane, f, st) {
  const keep = (x) => (!f || x.group_id === f) && statusKeep(x, false, st);
  const run = (lane.running || []).filter((x) => (!f || x.group_id === f) && statusKeep(x, true, st));
  const histAll = (lane.history || []).filter(keep);
  const c = lane.counts || {};
  const head = `<header class="lane-h">
      ${fishOf(lane.kind, 40, run.length ? {} : { still: true })}
      <div class="lane-ht"><b>${esc(lane.kind_title || lane.kind)}</b><span>${run.length ? `进行中 ${run.length}` : "空闲"}${c.done ? ` · 完成 ${c.done}` : ""}${c.failed ? ` · <span class="bad-t">失败 ${c.failed}</span>` : ""}</span></div>
    </header>`;
  // 空闲栏收起：一行带过，不再叠「进行中 / 历史」两层空块
  if (!run.length && !histAll.length) {
    const why = st !== "all" || f ? "当前筛选下没有匹配的工作" : lane.kind === "main" ? "这里显示主模型的独立判断（是否接活、管理员对话等）；它在任务里的派活和验收记在对应任务的过程中。" : "空闲 · 这段时间没有记录";
    return `<section class="lane idle" aria-label="${esc(lane.kind_title)}">${head}<div class="lane-empty">${esc(why)}</div></section>`;
  }
  const expanded = !!(state.boardLaneOpen && state.boardLaneOpen[lane.kind]);
  const hist = expanded ? histAll : histAll.slice(0, 8);
  return `<section class="lane${run.length ? " busy" : ""}" aria-label="${esc(lane.kind_title)}">
    ${head}
    ${run.length ? `<div class="lane-sec">进行中</div>${run.map((x) => card(lane, x, true)).join("")}` : ""}
    ${histAll.length ? `<div class="lane-sec">历史</div>${hist.map((x) => card(lane, x, false)).join("")}` : ""}
    ${histAll.length > 8 ? `<button class="lane-more" type="button" data-act="board-lane-more" data-kind="${esc(lane.kind)}" aria-expanded="${expanded}">${expanded ? "收起" : `展开其余 ${histAll.length - 8} 条`}</button>` : ""}
  </section>`;
}

/* ───────────── 页面 ───────────── */

const BOARD_STATUS = [
  ["all", "全部"],
  ["active", "进行中与卡住"],
  ["failed", "没做成"],
];

export function boardPage() {
  const b = state.board;
  const fail = (state.loadErr || {}).board;
  let html = `<div class="board-head"><h1 class="h-page">看板${tip("实时展示各岗位正在执行与近期完成的工作，每 10 秒自动刷新，仅供查看。")}</h1>`;
  if (!b) return html + `</div>` + loading(fail, "board");
  const lanes = b.lanes || [];
  const notes = b.notes || [];
  // 群筛选的选项：lane 里的活 + 便条，两处都收，切了筛选也不会丢群名
  const groups = [];
  const take = (id, name) => {
    if (id && !groups.some((g) => g.id === id)) groups.push({ id, name: name || id });
  };
  for (const l of lanes) for (const x of [...(l.running || []), ...(l.history || [])]) take(x.group_id, gName(x));
  for (const n of notes) take(n.group_id, n.group_name);
  const f = state.boardFilter && groups.some((g) => g.id === state.boardFilter) ? state.boardFilter : "";
  const st = BOARD_STATUS.some(([k]) => k === state.boardStatus) ? state.boardStatus : "all";
  const curHours = state.boardHours || b.hours || 24;
  const scoped = (live) => lanes.flatMap((l) => (live ? l.running || [] : l.history || [])).filter((x) => !f || x.group_id === f);
  const busy = scoped(true).length;
  const failedCount = scoped(true).filter(isFail).length + scoped(false).filter(isFail).length;
  html += `<div class="board-bar"><p class="board-sum">进行中 ${busy} 件${failedCount ? ` · 没做成 ${failedCount} 件` : ""}</p>${notes.length ? `<button class="btn sm" type="button" data-act="board-notes">最近便条 ${notes.length}</button>` : ""}</div>`;
  const hrOn = (h) => `aria-pressed="${String(curHours) === h}"`;
  html += `<div class="board-filters filters" role="group" aria-label="按时间窗筛选"><button class="filter" type="button" data-act="board-hours" data-h="6" ${hrOn("6")}>6 小时</button><button class="filter" type="button" data-act="board-hours" data-h="24" ${hrOn("24")}>24 小时</button><button class="filter" type="button" data-act="board-hours" data-h="72" ${hrOn("72")}>3 天</button><button class="filter" type="button" data-act="board-hours" data-h="168" ${hrOn("168")}>7 天</button></div>`;
  const stOn = (k) => `aria-pressed="${st === k}"`;
  html += `<div class="board-filters filters" role="group" aria-label="按状态筛选"><button class="filter" type="button" data-act="board-status" data-st="all" ${stOn("all")}>全部</button><button class="filter" type="button" data-act="board-status" data-st="active" ${stOn("active")}>进行中与卡住</button><button class="filter" type="button" data-act="board-status" data-st="failed" ${stOn("failed")}>没做成</button></div>`;
  if (groups.length > 1)
    html += `<div class="filters" role="group" aria-label="按群筛选">
      <button class="filter" data-act="board-filter" data-g="" aria-pressed="${!f}">全部群</button>
      ${groups.map((g) => `<button class="filter" data-act="board-filter" data-g="${esc(g.id)}" aria-pressed="${f === g.id}">${esc(g.name)}</button>`).join("")}
    </div>`;
  html += `</div>`;
  if (fail) html += `<div class="warn-box stale-note" role="status">连接中断，正在重试。当前显示的可能不是最新内容。</div>`;
  html += boardFlowHTML(b, f) || "";
  const total = lanes.reduce((n, l) => n + (l.running || []).length + (l.history || []).length, 0);
  const matched = scoped(true).filter((x) => statusKeep(x, true, st)).length + scoped(false).filter((x) => statusKeep(x, false, st)).length;
  if (!lanes.length) html += `<p class="h-meta">暂无专岗</p>`;
  else if (total && !matched && (f || st !== "all"))
    html += `<div class="board-none"><p>当前筛选下没有工作。</p><button class="btn" type="button" data-act="board-reset">重置筛选</button></div>`;
  else html += `<div class="lanes">${lanes.map((l) => laneHTML(l, f, st)).join("")}</div>`;
  return html;
}

/* ───────────── 过程抽屉 ───────────── */

// 便条板（docs/34）：/api/board/items 回包顶层 notes = 这次交接单留给别的岗的便条；没有就不显示这块
function notesHTML(notes) {
  if (!Array.isArray(notes) || !notes.length) return "";
  return `<h2 class="h-sub">留给别的岗的便条 <small>${notes.length} 条</small></h2>
    <ul class="bi-notes">${notes.map((n) => `<li><span class="h-meta">${esc(who(n.from_kind))} → ${esc(who(n.to_kind))}</span><div>${esc(n.body)}</div></li>`).join("")}</ul>`;
}

const jumpLabel = (j) =>
  j.detail_type === "task" ? "去任务页查看" : j.detail_type === "goal" ? "去目标详情查看" : j.tab === "ideas" ? "去构想页查看" : j.tab === "news" ? "去资讯页查看" : "去本群页查看";

export function boardItemSheet() {
  const cur = state.boardItem;
  if (!cur) return "";
  const d = cur.data;
  const it = (d && d.item) || boardBriefOf(cur.source, cur.id) || {};
  const lane = ((state.board && state.board.lanes) || []).find((l) => l.kind === cur.kind);
  const canJump = it.jump && it.group_id && state.groups.some((g) => g.id === it.group_id);
  const askRef = cur.source === "idea" ? `构想#${cur.id}` : String(cur.id || "");
  const jumpBtn = canJump ? `<button class="btn sm" type="button" data-act="board-goto" data-g="${esc(it.group_id)}" data-tab="${esc(it.jump.tab || "group")}" data-dtype="${esc(it.jump.detail_type || "")}" data-did="${esc(it.jump.detail_id || "")}">${esc(jumpLabel(it.jump))}</button>` : "";
  const askBtn = admin() && askRef ? `<button class="btn sm" type="button" data-act="chat-ask-ref" data-g="${esc(it.group_id || "")}" data-ref="${esc(askRef)}" data-title="${esc(it.title || "")}">在对话中询问</button>` : "";
  return `<div class="bi">
    <div class="bi-h">${fishOf(cur.kind, 44)}<div><div class="bi-k">${esc(laneTitle(cur.kind))} · ${esc(gName(it))}</div>
      <h1 class="h-page">${esc(it.title || "（无标题）")}</h1></div></div>
    <div class="bi-meta"><span class="dot ${dotOf(it)}"></span>${esc(it.status_text || it.status || "")}${it.started ? ` · 开始 ${esc(when(it.started))}` : ""}${it.finished ? ` · 结束 ${esc(when(it.finished))}` : ""} · 用时 ${dur(it.elapsed_s)}</div>
    ${jumpBtn || askBtn ? `<div class="bi-actions">${jumpBtn}${askBtn}</div>` : ""}
    ${it.error ? `<div class="warn-box bad-box">${esc(it.error)}</div>` : ""}
    ${d && d.link === "time_window" ? `<div class="warn-box">这类工作没有直接记下归属，下面的过程是按同群、同时段推断的，可能混入同一时间的其他工作。</div>` : ""}
    ${cur.err ? `<div class="warn-box">${esc(cur.err)}</div>` : ""}
    ${notesHTML(d && d.notes)}
    ${!d && !cur.err ? loading() : stepsSectionHTML(d || {}, { type: state.boardStepFilter || "all", round: state.boardRound || "", open: state.boardOpen || {} })}
  </div>`;
}

// 列表里这件的最新概况（明细还没取回时先用它）
export function boardBriefOf(source, id) {
  for (const l of (state.board && state.board.lanes) || []) for (const x of [...(l.running || []), ...(l.history || [])]) if (sameItem(x, source, id)) return x;
  return null;
}

/* ───────────── 最近便条 ───────────── */

export function boardNotesSheet() {
  const notes = ((state.board && state.board.notes) || []).filter((n) => !state.boardFilter || n.group_id === state.boardFilter);
  return `<div class="bi">
    <h1 class="h-page">最近便条</h1>
    <div class="h-meta sheet-lead">各专岗交接时留给别的岗的便条，只读</div>
    ${notes.length ? `<ul class="bi-notes board-notes">${notes
      .map((n) => `<li><span class="h-meta">${esc(n.group_name || n.group_id || "")} · ${esc(who(n.from_kind))} → ${esc(who(n.to_kind))} · ${esc(when(n.created))}${n.handoff_id ? ` · ${esc(n.handoff_id)}` : ""}</span><div>${esc(n.body)}</div></li>`)
      .join("")}</ul>` : `<p class="h-meta">暂无便条。</p>`}
  </div>`;
}

/* ───────────── 点击动作（全部只读） ───────────── */

export async function actBoard(a, el) {
  if (!/^board-/.test(a || "")) return false;
  if (!admin()) return true;
  switch (a) {
    case "board-filter":
      state.boardFilter = el.dataset.g || "";
      renderView();
      break;
    case "board-hours":
      state.boardHours = Number(el.dataset.h) || 0;
      await loadBoard();
      renderView();
      break;
    case "board-status":
      state.boardStatus = el.dataset.st || "all";
      renderView();
      break;
    case "board-reset":
      state.boardFilter = "";
      state.boardStatus = "all";
      renderView();
      break;
    case "board-lane-more": {
      const kind = el.dataset.kind || "";
      state.boardLaneOpen = state.boardLaneOpen || {};
      state.boardLaneOpen[kind] = !state.boardLaneOpen[kind];
      renderView();
      break;
    }
    case "board-item": {
      state.boardItem = { source: el.dataset.src, id: el.dataset.id, kind: el.dataset.kind, data: null, err: "" };
      state.boardOpen = {};
      state.boardStepFilter = "all";
      state.boardRound = "";
      openSheet("board-item");
      renderView();
      await loadBoardItem();
      if (state.sheet === "board-item") repaintSheet();
      break;
    }
    case "board-step-filter":
      state.boardStepFilter = el.dataset.sf || "all";
      repaintSheet();
      break;
    case "board-round-filter":
      state.boardRound = el.dataset.round || "";
      repaintSheet();
      break;
    case "board-jump-end": {
      const sh = el.closest(".sheet") || document.getElementById("sheet");
      if (sh) sh.scrollTop = sh.scrollHeight;
      break;
    }
    case "board-goto": {
      const gid = el.dataset.g || "";
      const tab = el.dataset.tab || "group";
      const dtype = el.dataset.dtype || "";
      const did = el.dataset.did || "";
      if (!state.groups.some((g) => g.id === gid)) break;
      closeSheet();
      go({ page: null, g: gid, tab: TABS.some((t) => t.id === tab) ? tab : "group", detail: did && dtype ? { type: dtype, id: did } : null });
      if (dtype === "task" && did) loadTask(did);
      if (!desktop.matches && did && dtype) openSheet("detail");
      break;
    }
    case "board-notes":
      openSheet("board-notes");
      break;
    case "board-flow-toggle":
      state.boardFlowCollapsed = !state.boardFlowCollapsed;
      renderView();
      break;
    case "board-ctx-view":
    case "board-ctx-part":
      await actCtxMap(a, el);
      break;
  }
  return true;
}
