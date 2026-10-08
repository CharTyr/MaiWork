// MaiWork 网页 · 任务 / 目标详情（右栏或抽屉）。
import { $, DOT, STATUS, admin, desktop, gadmin, state } from "./state.js";
import { SVG, dur, esc, ico, msText, safeUrl, when } from "./util.js";
import { api, gview } from "./api.js";
import { loading } from "./pages/news.js";
import { findIdea, ideaDetail } from "./pages/ideas.js";
import { nextText } from "./pages/goals.js";
import { taskRow } from "./pages/tasks.js";
import { sheetChrome } from "./sheet.js";
import { renderSide } from "./render.js";

/* ───────────── 详情 ───────────── */

function findGoal(id) {
  const v = gview();
  return v && v.goals ? (v.goals.agent || []).find((x) => x.id === id) : null;
}
function findTaskRow(id) {
  const v = gview();
  return v && v.tasks ? (v.tasks.list || []).find((x) => x.id === id) : null;
}

// 长文字先收到 4 行，点「展开」看全文（2026-10-01 用户：「任务写的好长」）。
// 展开状态记在内存里，轮询重画后不会自己合上；直接改 class，不用整块重画。
const FOLD_CHARS = 140;
const FOLD_LINES = 4;
const foldOpen = {};
function foldText(key, text) {
  const s = String(text || "");
  const long = s.length > FOLD_CHARS || s.split("\n").length > FOLD_LINES;
  if (!long) return `<div class="dt-text">${esc(s)}</div>`;
  const open = !!foldOpen[key];
  return `<div class="dt-text fold${open ? " open" : ""}" data-fold="${esc(key)}">${esc(s)}</div><button class="link-btn dt-more" data-fold-btn="${esc(key)}" aria-expanded="${open}">${open ? "收起" : "展开"}</button>`;
}
document.addEventListener("click", (e) => {
  const btn = e.target && e.target.closest && e.target.closest("[data-fold-btn]");
  if (!btn) return;
  const key = btn.dataset.foldBtn;
  const open = !foldOpen[key];
  foldOpen[key] = open;
  const box = btn.previousElementSibling;
  if (box && box.dataset.fold === key) box.classList.toggle("open", open);
  btn.setAttribute("aria-expanded", String(open));
  btn.textContent = open ? "收起" : "展开";
});

// 验收意见 + 引用核对：结构化结果在 link_check，意见文本里同一行去掉免得重复
function reviewBlock(t) {
  const lc = t.link_check && typeof t.link_check === "object" ? t.link_check : null;
  let text = String(t.review || "");
  if (lc) text = text.split("\n").filter((l) => !/^\s*引用核对：/.test(l)).join("\n").trim();
  let html = text ? `<div class="dt-sec"><div class="dt-label">检查意见</div>${foldText(`review:${t.id}`, text)}</div>` : "";
  if (lc && lc.links) {
    const bad = lc.unopened || 0;
    const urls = (lc.unopened_urls || []).slice(0, 10);
    html += `<div class="dt-sec"><div class="dt-label">引用核对</div><div class="dt-text">${bad ? `${lc.links} 个链接里有 ${bad} 个没核实` : `${lc.links} 个链接都核实过`}</div>${
      urls.length ? `<ul class="dt-list lc-list">${urls.map((u) => `<li><a href="${safeUrl(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></li>`).join("")}</ul>` : ""
    }</div>`;
  }
  return html;
}

// 安全网自动暂停（paused_reason 只在自动暂停时有值；手动暂停为 null）
const wan = (n) => (n >= 1e4 ? `${Math.round(n / 1e4)} 万` : String(n || 0));
function pausedBlock(t) {
  const r = t.status === "paused" && t.paused_reason;
  if (!r || !["tokens", "time", "capability"].includes(r.kind)) return "";
  if (r.kind === "capability") {
    if (!r.text) return "";
    const tail = gadmin() ? "准备好了点「继续」，不做就「取消」。" : "等管理员决定。";
    return `<div class="dt-sec"><div class="dt-label">为什么停了</div><div class="dt-text">${esc(r.text)}${esc(tail)}</div></div>`;
  }
  const text =
    r.kind === "tokens"
      ? `用量到上限了（约 ${wan(r.used)} / ${wan(r.limit)} token），先停下等你定。`
      : `做了 ${dur(r.used)}，到时长上限（${dur(r.limit)}）了，先停下等你定。`;
  // 群管理员进不了设置页：上限在哪改只告诉总管理员
  const act = "点「继续」重新计算，或点「取消」。";
  const tail = admin()
    ? act + "上限在「设置 → 全部设置 → 任务上限」。"
    : gadmin()
      ? act + "上限由管理员定。"
      : "等管理员决定。";
  return `<div class="dt-sec"><div class="dt-label">为什么停了</div><div class="dt-text">${esc(text)}${esc(tail)}</div></div>`;
}

// 「怎样算完成」：有需求清单（docs/22 第一期）就按清单显示——原话里的是必须做到的，
// 补充的标「加分项」，要真人参与的另标一下；底线那条（R0）不占位置。没有清单的旧任务照旧列 criteria。
function reqBlock(t) {
  const items = (t.requirements || []).filter((r) => r && r.origin !== "底线");
  if (items.length) {
    const tag = (r) =>
      (r.origin === "补充" ? `<span class="ntag">加分</span>` : `<span class="ntag ok">原话</span>`) +
      (r.kind === "真人" ? ` <span class="ntag warn">要人参与</span>` : "");
    return `<div class="dt-sec"><div class="dt-label">完成标准</div><ul class="dt-list">${items
      .map((r) => `<li>${esc(r.text)} ${tag(r)}</li>`)
      .join("")}</ul><div class="fine">原话都做到、内容属实才算完成</div></div>`;
  }
  return (t.criteria || []).length
    ? `<div class="dt-sec"><div class="dt-label">完成标准</div><ul class="dt-list">${t.criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>`
    : "";
}

// 目标完成标准的打勾依据（docs/22 第三期）：哪条任务交付的、证据一句（证据只有管理员看得到，
// 服务端群友版本来就不带 evidence）。旧的、没留依据的勾显示「没留依据」，方便管理员复核。
function critProof(c) {
  if (!c || !c.done) return "";
  const bits = [];
  if (c.task_id) bits.push(`来自任务 <span class="mono">${esc(c.task_id)}</span>`);
  if (c.evidence) bits.push(esc(c.evidence));
  if (c.ts) bits.push(esc(when(c.ts)));
  if (!c.task_id && !c.evidence) return admin() ? `<div class="fine-inline">旧记录，没留依据</div>` : "";
  return `<div class="fine">${bits.join(" · ")}</div>`;
}

function taskDetail(id) {
  const t = state.tasks[id];
  const row = findTaskRow(id);
  if (!t) {
    if (t === null) return `<div class="empty">${ico("hourglass")}<b>找不到这个任务</b><span>可能已删除或不在本群</span></div>`;
    return row ? `<div class="dt-head">${ico(row.icon || "package")}<div><h2 class="dt-title">${esc(row.title)}</h2></div></div>${loading()}` : loading();
  }
  const tl = t.timeline || [];
  const st = t.status;
  const dot = DOT[st] !== undefined ? DOT[st] : st;
  let acts = "";
  if (gadmin()) {
    const b = (op, label, primary) => `<button class="btn${primary ? " primary" : ""}" data-act="task-op" data-op="${op}" data-id="${esc(t.id)}">${label}</button>`;
    if (["running", "reviewing", "queued", "waiting_input"].includes(st)) acts = b("pause", "暂停") + b("cancel", "取消");
    else if (st === "paused" || st === "shelved") acts = b("resume", "继续", true) + b("cancel", "取消");
    else if (st === "failed") acts = b("retry", "重试", true);
    else if (st === "completed") acts = b("redeliver", t.undelivered ? "再发一次" : "重新发", t.undelivered);
  }
  return `
    <div class="dt-head">
      ${ico(t.icon || "package")}
      <div>
        <h2 class="dt-title">${esc(t.title)}</h2>
        <div class="dt-state"><span class="dot ${dot}"></span>${STATUS[st] || esc(st)} · <span class="mono">${esc(t.id)}</span></div>
      </div>
    </div>
    <div class="dt-sec"><div class="dt-label">要做什么</div>${foldText(`req:${t.id}`, t.req || t.meta || "")}</div>
    ${pausedBlock(t)}
    ${t.auto_reason ? `<div class="dt-sec"><div class="dt-label">谁批的</div><div class="dt-text">自动批准：${esc(t.auto_reason)}</div></div>` : ""}
    ${t.question ? `<div class="dt-sec"><div class="dt-label">在等回复</div><div class="quote">${esc(t.question)}</div></div>` : ""}
    ${reqBlock(t)}
    ${admin() && t.env ? `<div class="dt-sec"><div class="dt-label">在哪做</div><div class="dt-text">${esc(t.env)}</div></div>` : ""}
    ${
      !admin() && t.steps
        ? `<div class="dt-sec"><div class="dt-label">过程</div><div class="dt-text">已做 ${t.steps} 步${st === "running" ? "，还在做" : ""} · 细节仅管理员可见</div></div>`
        : ""
    }
    ${
      admin() && tl.length
        ? `<div class="dt-sec"><div class="dt-label">过程${st === "running" ? " · 实时" : ""}</div><ol class="tl">${tl
            .map(
              (s) => `
          <li class="${s.ok ? "ok" : "bad"}">
            <div class="tl-top"><span>${esc(when(s.ts))}</span><span class="tl-actor">${esc(s.actor)}</span><span class="tl-tool">${esc(s.tool)}</span><span class="tl-ms">${s.ms != null ? msText(s.ms) : ""}</span></div>
            <div class="tl-io">${esc(s.input)}<span class="arrow">→</span><span class="tl-out">${esc(s.output)}</span></div>
          </li>`
            )
            .join("")}</ol></div>`
        : ""
    }
    ${
      admin() && (t.lane_notes || []).length
        ? `<div class="dt-sec"><div class="dt-label">返工记录</div><ul class="dt-list">${t.lane_notes
            .map((n) => `<li><span class="fine">${esc(when(n.ts))}</span> ${esc(n.note)}</li>`)
            .join("")}</ul></div>`
        : ""
    }
    ${reviewBlock(t)}
    ${
      (t.delivery || []).length
        ? `<div class="dt-sec"><div class="dt-label">成品</div>${t.delivery
            .map((x) => {
              const icon = x.kind === "群文件" ? "package" : x.kind === "here.now" ? "link" : "filebox";
              const title = x.url ? `<a href="${safeUrl(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.text)}</a>` : esc(x.text);
              return `<div class="deliv">${ico(icon)}<div><div class="deliv-k">${esc(x.kind)}</div><div class="deliv-t">${title}</div><div class="deliv-s">${esc(x.state)}</div></div></div>`;
            })
            .join("")}</div>`
        : ""
    }
    ${acts ? `<div class="actions" style="margin-top:28px">${acts}</div>` : ""}
    ${handoffEntry(t)}`;
}

// 交接包入口（docs/24）：任何状态都能带走；被带走次数只给管理员看
function handoffEntry(t) {
  const n = Number(t.handoff_count) || 0;
  const count = gadmin() && n > 0 ? `<span class="ho-count">被带走 ${n} 次</span>` : "";
  return `<div class="ho-entry ho-task"><button class="link-btn" data-act="handoff" data-kind="task" data-id="${esc(t.id)}">交给我的 agent</button>${count}</div>`;
}

function goalDetail(goal) {
  const crit = goal.criteria || [];
  const done = crit.filter((c) => c.done).length;
  const row = goal.task_id && findTaskRow(goal.task_id);
  let acts = "";
  if (gadmin() && goal.state !== "done" && goal.state !== "cancelled") {
    acts = `<div class="actions" style="margin-top:28px">${
      goal.state === "paused"
        ? `<button class="btn primary" data-act="goal-op" data-op="resume" data-id="${esc(goal.id)}">继续</button>`
        : `<button class="btn" data-act="goal-op" data-op="pause" data-id="${esc(goal.id)}">暂停</button>`
    }<button class="btn" data-act="goal-op" data-op="cancel" data-id="${esc(goal.id)}">不做了</button></div>`;
  }
  return `
    <div class="dt-head">
      ${ico(goal.icon || "bullseye")}
      <div>
        <h2 class="dt-title">${esc(goal.title)}</h2>
        <div class="dt-state"><span class="dot ${goal.state === "paused" ? "" : "running"}"></span>${goal.state === "paused" ? "暂停中" : "推进中"}${crit.length ? ` · ${done} / ${crit.length}` : ""}</div>
      </div>
    </div>
    <div class="dt-sec"><div class="dt-text">${esc(goal.body)}</div></div>
    ${
      crit.length
        ? `<div class="dt-sec"><div class="dt-label">完成标准</div>
      <ul class="checks">${crit.map((c) => `<li class="${c.done ? "done" : ""}"><span class="tick">${c.done ? SVG.check : ""}</span><span class="ck-body">${esc(c.text)}${critProof(c)}</span></li>`).join("")}</ul></div>`
        : ""
    }
    ${goal.last ? `<div class="dt-sec"><div class="dt-label">上次</div><div class="dt-text">${esc(when(goal.last.ts))} ${esc(goal.last.text)}</div></div>` : ""}
    <div class="dt-sec"><div class="dt-label">下次</div><div class="dt-text">${esc(nextText(goal))}</div></div>
    ${goal.by ? `<div class="dt-sec"><div class="dt-label">来源</div><div class="dt-text">${esc(goal.by)}</div></div>` : ""}
    ${row ? `<div class="dt-sec"><div class="dt-label">相关任务</div>${taskRow(row, 0)}</div>` : ""}
    ${acts}`;
}

export function detailHTML() {
  if (!state.detail) return "";
  if (state.detail.type === "task") return taskDetail(state.detail.id);
  if (state.detail.type === "idea") {
    const it = findIdea(state.detail.id);
    return it ? ideaDetail(it) : `<div class="empty">${ico("bulb")}<b>找不到了</b></div>`;
  }
  const goal = findGoal(state.detail.id);
  return goal ? goalDetail(goal) : `<div class="empty">${ico("bullseye")}<b>找不到了</b></div>`;
}

export async function loadTask(id) {
  try {
    state.tasks[id] = await api("GET", `/api/tasks/${encodeURIComponent(id)}`);
  } catch (e) {
    state.tasks[id] = null;
  }
  if (state.detail && state.detail.id === id) refreshDetail();
}

export function refreshDetail() {
  if (desktop.matches) renderSide();
  else if (state.sheet === "detail") {
    const sh = $("sheet");
    const top = sh.scrollTop;
    sh.innerHTML = sheetChrome() + detailHTML();
    sh.scrollTop = top;
  }
}
