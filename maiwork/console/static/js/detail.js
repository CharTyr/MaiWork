// MaiWork 网页 · 任务 / 目标详情（右栏或抽屉）。
import { $, DOT, STATUS, admin, desktop, gadmin, state } from "./state.js";
import { SVG, dur, esc, ico, msText, safeUrl, tip, when } from "./util.js";
import { api, gview } from "./api.js";
import { loading } from "./pages/news.js";
import { findIdea, ideaDetail } from "./pages/ideas.js";
import { critBacked, critPending, nextText } from "./pages/goals.js";
import { taskRow } from "./pages/tasks.js";
import { sheetChrome } from "./sheet.js";
import { renderSide } from "./render.js";
import { goalChatHTML, preserveGoalChatInput } from "./goal-chat.js";

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
    html += `<div class="dt-sec"><div class="dt-label">引用核对</div><div class="dt-text">${bad ? `${lc.links} 个链接中有 ${bad} 个未核实` : `${lc.links} 个链接均已核实`}</div>${
      urls.length ? `<ul class="dt-list lc-list">${urls.map((u) => `<li><a href="${safeUrl(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></li>`).join("")}</ul>` : ""
    }</div>`;
  }
  return html;
}

// 安全网自动暂停（paused_reason 只在自动暂停时有值；手动暂停为 null）
const wan = (n) => (n >= 1e4 ? `${Math.round(n / 1e4)} 万` : String(n || 0));
function pausedBlock(t) {
  const r = ["paused", "queued"].includes(t.status) && t.paused_reason;
  if (r && r.kind === "feature_off") {
    return `<div class="dt-sec"><div class="dt-label">搁置原因</div><div class="dt-text">任务功能关闭时已搁置。重新开启后，由管理员点击「恢复」。</div></div>`;
  }
  if (!r || !["tokens", "tokens_unknown", "time", "capability"].includes(r.kind)) return "";
  if (r.kind === "capability") {
    if (!r.text) return "";
    const tail = gadmin() ? "准备就绪后点击「继续」，不再执行请点击「取消」。" : "等待管理员处理。";
    return `<div class="dt-sec"><div class="dt-label">暂停原因</div><div class="dt-text">${esc(r.text)}${esc(tail)}</div></div>`;
  }
  const text =
    r.kind === "tokens"
      ? `用量已达上限（约 ${wan(r.used)} / ${wan(r.limit)} token），已暂停。`
      : r.kind === "tokens_unknown"
        ? `暂时无法核算本任务的用量（上限 ${wan(r.limit)} token），为防止超支已暂停。`
        : `已运行 ${dur(r.used)}，达到时长上限（${dur(r.limit)}），已暂停。`;
  // 群管理员进不了设置页：上限在哪改只告诉总管理员
  const act = "点击「继续」重新计算，或点击「取消」。";
  const tail = admin()
    ? act + "上限可在「设置 → 全部设置 → 任务上限」中修改。"
    : gadmin()
      ? act + "上限由管理员设置。"
      : "等待管理员处理。";
  return `<div class="dt-sec"><div class="dt-label">暂停原因</div><div class="dt-text">${esc(text)}${esc(tail)}</div></div>`;
}

// 「怎样算完成」：有需求清单（docs/22 第一期）就按清单显示——原话里的是必须做到的，
// 补充的标「加分项」，要真人参与的另标一下；底线那条（R0）不占位置。没有清单的旧任务照旧列 criteria。
function reqBlock(t) {
  const items = (t.requirements || []).filter((r) => r && r.origin !== "底线");
  if (items.length) {
    const tag = (r) =>
      (r.origin === "补充" ? `<span class="ntag">补充</span>` : `<span class="ntag ok">原始要求</span>`) +
      (r.kind === "真人" ? ` <span class="ntag warn">需人工参与</span>` : "");
    return `<div class="dt-sec"><div class="dt-label">完成标准${tip("原始要求全部达成且内容属实，任务才算完成。")}</div><ul class="dt-list">${items
      .map((r) => `<li>${esc(r.text)} ${tag(r)}</li>`)
      .join("")}</ul></div>`;
  }
  return (t.criteria || []).length
    ? `<div class="dt-sec"><div class="dt-label">完成标准</div><ul class="dt-list">${t.criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>`
    : "";
}

// 目标完成标准的打勾依据（docs/22 第三期）：哪条任务交付的、证据一句（证据只有管理员看得到，
// 服务端群友版本来就不带 evidence）。没留依据的旧勾是「待确认」：不算进完成，
// 群管理员可以点「算数」或「撤掉」（都会留记录）。
function critProof(c, goal, i) {
  if (!c || !c.done) return "";
  if (critPending(c)) {
    const open = gadmin() && goal.state !== "done" && goal.state !== "cancelled";
    const b = (op, label) => `<button class="link-btn" data-act="goal-op" data-op="${op}" data-id="${esc(goal.id)}" data-i="${i}">${label}</button>`;
    return `<div class="fine-inline"><b class="warn-t">待确认</b> · 此前标记完成但无依据，暂不计入完成${open ? `<span class="ck-acts">${b("confirm", "确认完成")}${b("revoke", "撤销")}</span>` : ""}</div>`;
  }
  const bits = [];
  if (c.task_id) bits.push(`来自任务 <span class="mono">${esc(c.task_id)}</span>`);
  if (c.evidence) bits.push(esc(c.evidence));
  if (c.confirmed_ts) bits.push("管理员已确认");
  if (c.ts || c.confirmed_ts) bits.push(esc(when(c.ts || c.confirmed_ts)));
  return bits.length ? `<div class="fine">${bits.join(" · ")}</div>` : "";
}

// 目标判断（立目标前 / 补判）：要群友自己动手的，MaiWork 盯不了——只提示，不自动改
function fitBlock(goal) {
  if (goal.fit !== "human" || goal.state === "done" || goal.state === "cancelled") return "";
  const why = goal.fit_reason ? `：${goal.fit_reason}` : "";
  return `<div class="warn-box">该目标需由群成员亲自完成，MaiWork 无法跟进${esc(why)}。${gadmin() ? "建议点击「取消目标」；保留也可以，MaiWork 不会再推进。" : "等待管理员决定是否保留。"}</div>`;
}

function taskDetail(id) {
  const t = state.tasks[id];
  const row = findTaskRow(id);
  if (!t) {
    if (t === null) return `<div class="empty">${ico("hourglass")}<b>未找到该任务</b><span>任务可能已删除或不属于本群</span></div>`;
    return row ? `<div class="dt-head">${ico(row.icon || "package")}<div><h2 class="dt-title">${esc(row.title)}</h2></div></div>${loading()}` : loading();
  }
  const tl = t.timeline || [];
  const st = t.status;
  const dot = DOT[st] !== undefined ? DOT[st] : st;
  let acts = "";
  if (gadmin()) {
    const b = (op, label, primary) => `<button class="btn${primary ? " primary" : ""}" data-act="task-op" data-op="${op}" data-id="${esc(t.id)}">${label}</button>`;
    if (st === "queued" && t.paused_reason && t.paused_reason.kind === "feature_off") acts = b("resume", "恢复", true) + b("cancel", "取消");
    else if (["running", "reviewing", "queued", "waiting_input"].includes(st)) acts = b("pause", "暂停") + b("cancel", "取消");
    else if (st === "paused" || st === "shelved") acts = b("resume", "继续", true) + b("cancel", "取消");
    else if (st === "failed") acts = b("retry", "重试", true);
    else if (st === "completed") acts = b("redeliver", t.undelivered ? "重新发送" : "再次发送", t.undelivered);
  }
  return `
    <div class="dt-head">
      ${ico(t.icon || "package")}
      <div>
        <h2 class="dt-title">${esc(t.title)}</h2>
        <div class="dt-state"><span class="dot ${dot}"></span>${STATUS[st] || esc(st)} · <span class="mono">${esc(t.id)}</span></div>
      </div>
    </div>
    <div class="dt-sec"><div class="dt-label">任务内容</div>${foldText(`req:${t.id}`, t.req || t.meta || "")}</div>
    ${pausedBlock(t)}
    ${t.auto_reason ? `<div class="dt-sec"><div class="dt-label">审批</div><div class="dt-text">自动审批：${esc(t.auto_reason)}</div></div>` : ""}
    ${t.question ? `<div class="dt-sec"><div class="dt-label">等待回复</div><div class="quote">${esc(t.question)}</div></div>` : ""}
    ${reqBlock(t)}
    ${admin() && t.env ? `<div class="dt-sec"><div class="dt-label">运行环境</div><div class="dt-text">${esc(t.env)}</div></div>` : ""}
    ${
      !admin() && t.steps
        ? `<div class="dt-sec"><div class="dt-label">过程</div><div class="dt-text">已完成 ${t.steps} 步${st === "running" ? "，进行中" : ""} · 详情仅管理员可见</div></div>`
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
        ? `<div class="dt-sec"><div class="dt-label">成果</div>${t.delivery
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
  const count = gadmin() && n > 0 ? `<span class="ho-count">已导出 ${n} 次</span>` : "";
  return `<div class="ho-entry ho-task"><button class="link-btn" data-act="handoff" data-kind="task" data-id="${esc(t.id)}">交给我的 agent</button>${count}</div>`;
}

function goalDetail(goal) {
  const crit = goal.criteria || [];
  const done = crit.filter(critBacked).length;
  const row = goal.task_id && findTaskRow(goal.task_id);
  let acts = "";
  if (gadmin() && goal.state !== "done" && goal.state !== "cancelled") {
    acts = `<div class="actions" style="margin-top:28px">${
      goal.state === "paused"
        ? `<button class="btn primary" data-act="goal-op" data-op="resume" data-id="${esc(goal.id)}">继续</button>`
        : `<button class="btn" data-act="goal-op" data-op="pause" data-id="${esc(goal.id)}">暂停</button>`
    }<button class="btn" data-act="goal-op" data-op="cancel" data-id="${esc(goal.id)}">取消目标</button></div>`;
  }
  // 受阻时显示对话入口（2026-10 需求）
  const staleBlock = goal.stale
    ? `<div class="warn-box"><b>⚠️ 目标受阻</b>：${esc(goal.stale_reason || "原因未知")}</div>`
    : "";
  const chatBlock = goalChatHTML(goal);
  return `
    <div class="dt-head">
      ${ico(goal.icon || "bullseye")}
      <div>
        <h2 class="dt-title">${esc(goal.title)}</h2>
        <div class="dt-state"><span class="dot ${goal.state === "paused" ? "" : "running"}"></span>${goal.state === "paused" ? "暂停中" : "推进中"}${crit.length ? ` · ${done} / ${crit.length}` : ""}</div>
      </div>
    </div>
    ${fitBlock(goal)}
    ${staleBlock}
    <div class="dt-sec"><div class="dt-text">${esc(goal.body)}</div></div>
    ${
      crit.length
        ? `<div class="dt-sec"><div class="dt-label">完成标准</div>
      <ul class="checks">${crit.map((c, i) => `<li class="${critBacked(c) ? "done" : ""}"><span class="tick">${critBacked(c) ? SVG.check : ""}</span><span class="ck-body">${esc(c.text)}${critProof(c, goal, i)}</span></li>`).join("")}</ul></div>`
        : ""
    }
    ${goal.last ? `<div class="dt-sec"><div class="dt-label">上次检查</div><div class="dt-text">${esc(when(goal.last.ts))} ${esc(goal.last.text)}</div></div>` : ""}
    <div class="dt-sec"><div class="dt-label">下次检查</div><div class="dt-text">${esc(nextText(goal))}</div></div>
    ${goal.by ? `<div class="dt-sec"><div class="dt-label">来源</div><div class="dt-text">${esc(goal.by)}</div></div>` : ""}
    ${row ? `<div class="dt-sec"><div class="dt-label">相关任务</div>${taskRow(row, 0)}</div>` : ""}
    ${chatBlock}
    ${acts}`;
}

export function detailHTML() {
  if (!state.detail) return "";
  if (state.detail.type === "task") return taskDetail(state.detail.id);
  if (state.detail.type === "idea") {
    const it = findIdea(state.detail.id);
    return it ? ideaDetail(it) : `<div class="empty">${ico("bulb")}<b>未找到该构想</b></div>`;
  }
  const goal = findGoal(state.detail.id);
  return goal ? goalDetail(goal) : `<div class="empty">${ico("bullseye")}<b>未找到该目标</b></div>`;
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
  else if (state.sheet === "detail") preserveGoalChatInput(() => {
    const sh = $("sheet");
    const top = sh.scrollTop;
    sh.innerHTML = sheetChrome() + detailHTML();
    sh.scrollTop = top;
  });
}
