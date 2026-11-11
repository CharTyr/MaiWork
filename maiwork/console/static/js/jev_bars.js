// MaiWork 网页 · Jev 快速判断概率分布面板（纯函数，不碰 DOM，可直接用 Node 单测）。
// 同时服务三处：
//   1. 群页 · 话题记录（ok_p / ok_need / reason_probs / fits / fit_need / ms）
//   2. 任务页 · 待复核 @ 消息（probs / threshold / ms）
//   3. 看板过程抽屉 · Jev 判断步骤（probs 嵌套字典 / ms）
import { esc, msText } from "./util.js";
import { FIT_NEED, OK_NEED } from "./topiclog.js";

const REASON_LABELS = {
  fine: ["冷场可开", "var(--green)"],
  chatting: ["正在交谈", "var(--blue)"],
  busy_pair: ["他人对话中", "var(--blue)"],
  open_question: ["提问待回应", "var(--orange)"],
  serious: ["严肃或私密", "var(--red)"],
  done: ["话题刚结束", "var(--gray-2)"],
  self_spam: ["近期已发过", "var(--purple)"],
};

const ASK_LABELS = {
  prepare: ["任务请求", "var(--blue)"],
  task: ["任务请求", "var(--blue)"],
  goal: ["长期目标", "var(--purple)"],
  remind: ["定时提醒", "var(--orange)"],
  chat: ["普通闲聊", "var(--green)"],
  none: ["无需跟进", "var(--gray-2)"],
};

const QUESTION_LABELS = {
  ok: "开启适合度",
  reason: "群聊状态",
  kind: "意图分类",
};

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const clamp01 = (v) => Math.max(0, Math.min(1, Number(v) || 0));
const pctText = (v) => `${Math.round(clamp01(v) * 100)}%`;
const f2 = (v) => clamp01(v).toFixed(2);

function meterRow(label, val, need, extra = "", color = "") {
  const p = clamp01(val);
  const th = num(need);
  const pass = th == null ? true : p >= th;
  const fillCol = color || (pass ? "var(--green)" : "var(--blue)");
  return `<div class="jb-row">
    <span class="jb-lbl" title="${esc(label)}">${esc(label)}</span>
    <span class="jb-track">
      <i class="jb-fill" style="transform:scaleX(${p.toFixed(3)});background:${fillCol}"></i>
      ${th != null ? `<b class="jb-th" style="left:${(clamp01(th) * 100).toFixed(1)}%" title="阈值 ${f2(th)}"></b>` : ""}
    </span>
    <span class="jb-val">${f2(p)}${th != null ? `<small>/${f2(th)}</small>` : ""}${extra}</span>
  </div>`;
}

function distRow(label, mapObj, dictMeta) {
  const entries = Object.entries(mapObj || {})
    .map(([k, v]) => [k, clamp01(v)])
    .filter(([, v]) => v > 0.004)
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "";
  const titleOf = (k) => (dictMeta[k] && dictMeta[k][0]) || k;
  const colorOf = (k) => (dictMeta[k] && dictMeta[k][1]) || "var(--gray-2)";
  const segs = entries
    .map(([k, v]) => `<i style="flex:${Math.max(0.02, v).toFixed(3)};background:${colorOf(k)}" title="${esc(titleOf(k))} ${pctText(v)}"></i>`)
    .join("");
  const legend = entries
    .slice(0, 4)
    .map(([k, v]) => `<span class="jb-leg"><i style="background:${colorOf(k)}"></i>${esc(titleOf(k))} <b>${pctText(v)}</b></span>`)
    .join("");
  return `<div class="jb-dist-wrap">
    <div class="jb-row jb-dist-row">
      <span class="jb-lbl">${esc(label)}</span>
      <span class="jb-dist">${segs}</span>
      <span class="jb-val">${esc(titleOf(entries[0][0]))}</span>
    </div>
    <div class="jb-legs">${legend}</div>
  </div>`;
}

function flatProbBars(probs, threshold, dictMeta) {
  const entries = Object.entries(probs || {})
    .filter(([, v]) => typeof v === "number" && Number.isFinite(v))
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "";
  return entries
    .map(([k, v]) => {
      const meta = dictMeta[k];
      const label = (meta && meta[0]) || k;
      const col = (meta && meta[1]) || "var(--blue)";
      const actionable = k === "prepare" || k === "task" || k === "goal";
      return meterRow(label, v, actionable ? threshold : null, "", col);
    })
    .join("");
}

export function jevBarsHTML(jev, opts = {}) {
  if (!jev || typeof jev !== "object") return "";
  const compact = !!opts.compact;
  const ms = num(jev.ms);
  const msBadge = ms && ms > 0 ? `<span class="jb-ms">${esc(msText(ms))}</span>` : "";
  const blocks = [];

  // 1) 群话题冷场判断：ok_p + reason_probs + fits
  const okP = num(jev.ok_p);
  if (okP != null) {
    blocks.push(meterRow("开启适合度", okP, num(jev.ok_need) ?? OK_NEED));
  }
  if (jev.reason_probs && typeof jev.reason_probs === "object") {
    const d = distRow("群聊状态", jev.reason_probs, REASON_LABELS);
    if (d) blocks.push(d);
  }
  if (Array.isArray(jev.fits) && jev.fits.length) {
    const fitNeed = num(jev.fit_need) ?? FIT_NEED;
    for (const f of jev.fits.slice(0, compact ? 3 : 6)) {
      if (!f || num(f.fit_p) == null) continue;
      const tag = f.picked ? `<em class="jb-pick">选用</em>` : "";
      blocks.push(meterRow(`候选：${f.title || `#${(f.idx || 0) + 1}`}`, f.fit_p, fitNeed, tag, f.picked ? "var(--green)" : "var(--blue)"));
    }
  }

  // 2) 待复核 @ 消息 / 看板步骤：probs 可能是平面 {prepare:0.48,...} 或嵌套 {kind:{...}, reason:{...}}
  if (jev.probs && typeof jev.probs === "object" && !Array.isArray(jev.probs)) {
    const vals = Object.values(jev.probs);
    const isFlat = vals.length > 0 && vals.every((v) => typeof v === "number");
    if (isFlat) {
      const b = flatProbBars(jev.probs, num(jev.threshold), ASK_LABELS);
      if (b) blocks.push(b);
    } else {
      for (const [qk, qmap] of Object.entries(jev.probs)) {
        if (!qmap || typeof qmap !== "object" || Array.isArray(qmap)) continue;
        if (qk === "reason") {
          const d = distRow("群聊状态", qmap, REASON_LABELS);
          if (d) blocks.push(d);
        } else if (qk === "kind") {
          const b = flatProbBars(qmap, num(jev.threshold), ASK_LABELS);
          if (b) blocks.push(b);
        } else if (qk === "ok" || qk.startsWith("fit_")) {
          const yes = num(qmap.yes ?? qmap.true ?? qmap["是"] ?? qmap.fine);
          if (yes != null) {
            const lbl = qk === "ok" ? "开启适合度" : `候选 #${Number(qk.slice(4)) + 1}`;
            blocks.push(meterRow(lbl, yes, qk === "ok" ? OK_NEED : FIT_NEED));
          } else {
            const d = distRow(QUESTION_LABELS[qk] || qk, qmap, ASK_LABELS);
            if (d) blocks.push(d);
          }
        } else {
          const d = distRow(QUESTION_LABELS[qk] || qk, qmap, ASK_LABELS);
          if (d) blocks.push(d);
        }
      }
    }
  }

  if (!blocks.length) return "";
  return `<div class="jev-bars${compact ? " compact" : ""}">
    <div class="jb-head"><span>Jev 概率分布</span>${msBadge}</div>
    ${blocks.join("")}
  </div>`;
}
