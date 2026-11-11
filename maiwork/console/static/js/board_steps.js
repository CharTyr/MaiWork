// MaiWork 网页 · 看板抽屉的过程部分（纯函数，不碰 DOM、不发请求，方便用 node 直接测）。
// 三件事：
//   toolSummary(tool, input, output)   工具调用的一行摘要（未转义原文，渲染时由调用方过 esc）
//   mergeSteps(prevData, nextData)     ?after= 增量回来的步骤并进旧数据（按 uid 去重）
//   stepsSectionHTML(d, filterState)   轮次总览 + 快筛条 + 步骤列表的 HTML
import { dur, esc, hhmm, msText, tokens } from "./util.js";
import { ctxMapHTML } from "./ctxmap.js";
import { jevBarsHTML } from "./jev_bars.js";

const STEP_CAP = 500; // 过程最多展示的步数（和 trace.py 上限一致）
const SUM_CAP = 72; // 工具一行摘要的长度上限

/* ───────────── 工具一行摘要 ───────────── */

const clip = (s) => (s.length > SUM_CAP ? s.slice(0, SUM_CAP - 1) + "…" : s);
// input / output 可能是 JSON 字符串、普通字符串或根本不是字符串；一律不抛错
const asObj = (v) => {
  if (v && typeof v === "object" && !Array.isArray(v)) return v;
  if (typeof v !== "string") return null;
  try {
    const o = JSON.parse(v);
    return o && typeof o === "object" && !Array.isArray(o) ? o : null;
  } catch (_) {
    return null;
  }
};
const asStr = (v) => (typeof v === "string" ? v : v == null ? "" : typeof v === "number" || typeof v === "boolean" ? String(v) : "");
const firstStr = (o, keys) => {
  for (const k of keys) {
    const v = o && o[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return "";
};
const firstLine = (s) => String(s || "").split("\n").find((x) => x.trim()) || "";
const baseName = (p) => {
  const s = String(p || "").trim().replace(/[\\/]+$/, "");
  return s ? s.split(/[\\/]/).pop() : "";
};
const shortUrl = (u) => {
  const s = String(u || "").trim();
  try {
    const url = new URL(/^https?:\/\//i.test(s) ? s : "https://" + s);
    return url.hostname.replace(/^www\./, "") + (url.pathname === "/" ? "" : clip(url.pathname));
  } catch (_) {
    return s;
  }
};

// 返回未转义的一行纯文本；渲染处必须过 esc()（步骤列表里就是这么做的）
export function toolSummary(tool, input, output) {
  const o = asObj(input);
  const raw = asStr(input).trim();
  switch (tool) {
    case "web_search":
    case "social_search": {
      let q = firstStr(o, ["q", "query", "keyword", "kw", "text"]);
      if (!q && o && Array.isArray(o.queries) && typeof o.queries[0] === "string") q = o.queries[0];
      const site = firstStr(o, ["platform", "site"]);
      const what = q || raw;
      return what ? clip(`搜：${what}${site ? `（${site}）` : ""}`) : "";
    }
    case "fetch_page": {
      const u = firstStr(o, ["url", "uri", "link"]) || raw;
      return u ? clip(`读网页：${shortUrl(u)}`) : "";
    }
    case "run_command":
    case "vm_run":
    case "machine_run": {
      const cmd = firstLine(firstStr(o, ["cmd", "command", "script", "code"]) || raw);
      return cmd ? clip(`$ ${cmd}`) : "";
    }
    case "read_file":
    case "write_file":
    case "edit_file":
    case "list_files":
    case "read_image":
    case "inspect_image": {
      const p = baseName(firstStr(o, ["path", "file", "dir", "directory", "file_path"]) || raw);
      return p ? clip(`文件：${p}`) : "";
    }
    case "submit_result": {
      const s = firstStr(o, ["summary", "result", "brief"]) || firstStr(asObj(output), ["summary", "result"]) || raw;
      return s ? clip(`交回：${s}`) : "";
    }
    default: {
      // 不认识的工具：JSON 里挑一个短标量（name / action / title / query / url / path）当说明
      if (o) {
        for (const k of ["name", "action", "title", "query", "url", "path"]) {
          const v = o[k];
          if (typeof v === "string" && v.trim() && v.trim().length <= SUM_CAP) return v.trim();
          if ((typeof v === "number" || typeof v === "boolean") && String(v).length <= SUM_CAP) return String(v);
        }
      }
      return "";
    }
  }
}

/* ───────────── 增量合并 ───────────── */

// 没 uid 的老步骤退到内容键（同戳不同步也能分清）
export const fallbackStepKey = (s) => `${s.ts}|${s.type}|${s.tool || ""}|${String(s.text || s.input || "").slice(0, 40)}`;
const stepKeyOf = (s) => (s.uid ? `u:${s.uid}` : fallbackStepKey(s));

export function mergeSteps(prevData, nextData) {
  if (!prevData || nextData.truncated) return nextData;
  const prev = prevData.steps || [];
  const next = nextData.steps || [];
  if (!next.length) return { ...nextData, steps: prev };
  const seen = new Set(prev.map(stepKeyOf));
  const added = next.filter((s) => !seen.has(stepKeyOf(s)));
  let steps = prev.concat(added);
  // 条目共时戳时按先后稳定排序；一件刚跑完时终态 s:end:* 一定在
  steps.sort((a, b) => (a.ts || 0) - (b.ts || 0));
  const capped = steps.length > STEP_CAP;
  if (capped) {
    // 中间省略：留首 250 + 一条占位 + 尾 249，凑满 500
    const last = steps.slice(-(STEP_CAP / 2 - 1));
    const midTs = last.length ? (last[0].ts || 0) - 0.001 : 0;
    steps = steps.slice(0, STEP_CAP / 2).concat([{ uid: "s:omit", ts: midTs, type: "status", text: "中间已省略部分步骤" }], last);
  }
  return { ...nextData, steps, truncated: !!nextData.truncated || capped || (nextData.total_steps || 0) > STEP_CAP };
}

/* ───────────── 轮次 + 步骤列表 ───────────── */

const stFailed = (s) => s.ok === false || !!s.error;

function roundChip(r, cur) {
  const on = cur === r.id;
  return `<button class="bi-round" type="button" data-act="board-round-filter" data-round="${esc(r.id)}" aria-pressed="${on}">${esc(`第 ${r.no} 轮`)}</button>`;
}

function roundsHTML(d, fs) {
  const rounds = d.rounds || [];
  if (!rounds.length) return "";
  const cards = rounds
    .map((r) => {
      const dot = r.ok === false || r.status === "failed" ? "failed" : r.ok === true || r.status === "done" || r.status === "accepted" ? "done" : "running";
      const secs = r.started && r.finished ? r.finished - r.started : 0;
      const line = r.error || r.review || r.summary || "";
      return `<div class="bi-round-row"><span class="dot ${dot}"></span><span class="bi-round-t">第 ${r.no} 轮 · ${esc(r.kind_title)}${r.phase ? `（${esc(r.phase)}）` : ""}</span><span class="bi-round-s">${esc(r.status_text || "")}${secs ? ` · ${dur(secs)}` : ""}</span>${line ? `<div class="bi-round-l">${esc(line)}</div>` : ""}</div>`;
    })
    .join("");
  const pick =
    rounds.length > 1
      ? `<div class="bi-round-pick" role="group" aria-label="按轮次筛选"><button class="bi-round" type="button" data-act="board-round-filter" data-round="" aria-pressed="${!fs.round}">全部轮次</button>${rounds
          .map((r) => roundChip(r, fs.round))
          .join("")}</div>`
      : "";
  return `<div class="bi-rounds">${cards}${pick}</div>`;
}

function stepHTML(s, i, open) {
  // 展开态按 uid 记（增量合并后下标会变）；没 uid 的老步骤退回下标，两种键都认
  const key = String(s.uid || i);
  const t = `<span class="st-time">${hhmm(s.ts)}</span>`;
  const who = [s.actor, s.model].filter(Boolean).map(esc).join(" · ");
  const ms = s.ms != null ? esc(msText(s.ms)) : "";
  if (s.type === "tool") {
    const bad = stFailed(s);
    const sub = [ms, who].filter(Boolean).join(" · ");
    const io = s.input || s.output || s.error;
    const sum = toolSummary(s.tool, s.input, s.output);
    return `<li class="st st-tool${bad ? " bad" : ""}">
      <div class="st-row">${t}<span class="st-badge tool">工具</span><b class="st-name">${esc(s.tool || "")}</b><span class="st-sub">${sub}${bad ? `${sub ? " · " : ""}<span class="bad-t">失败</span>` : ""}</span></div>
      ${sum ? `<div class="st-sum">${esc(sum)}</div>` : ""}
      ${io ? `<details class="st-io" data-i="${esc(key)}"${open && (open[key] || open[i]) ? " open" : ""}><summary>输入 / 输出</summary>
        ${s.input ? `<div class="st-l">输入</div><pre>${esc(s.input)}</pre>` : ""}
        ${s.output ? `<div class="st-l">输出</div><pre>${esc(s.output)}</pre>` : ""}
        ${s.error ? `<div class="st-l">错误</div><pre class="bad-t">${esc(s.error)}</pre>` : ""}
      </details>` : ""}
    </li>`;
  }
  if (s.type === "think") {
    const bad = stFailed(s);
    const ft = s.first_token_ms && s.first_token_ms > 0 ? `首字 ${esc(msText(s.first_token_ms))}` : "";
    const sub = [who, ft, ms].filter(Boolean).join(" · ");
    const hasCtx = s.context_map && (s.context_map.used_tokens || (Array.isArray(s.context_map.parts) && s.context_map.parts.length));
    return `<li class="st st-think${bad ? " bad" : ""}">
      <div class="st-row">${t}<span class="st-badge think">思考</span><span class="st-sub">${sub}${bad ? `${sub ? " · " : ""}<span class="bad-t">失败</span>` : ""}</span></div>
      ${s.jev ? jevBarsHTML(s.jev, { compact: true }) || "" : ""}
      ${hasCtx ? `<details class="st-io st-ctx" data-i="c:${esc(key)}"${open && open["c:" + key] ? " open" : ""}><summary>上下文构成 · ${tokens(s.context_map.used_tokens || 0)} token</summary>${ctxMapHTML(s.context_map, { id: "st-" + key, callId: s.model_call_id, compact: true, actPrefix: "board-ctx" })}</details>` : ""}
      ${s.reasoning ? `<details class="st-io st-reason" data-i="r:${esc(key)}"${open && (open["r:" + key] || open["r" + i]) ? " open" : ""}><summary>思考过程</summary><pre class="st-text st-why">${esc(s.reasoning)}</pre></details>` : s.reasoning_redacted ? `<div class="st-sub st-why-note">思考过程已由模型加密，无法查看</div>` : ""}
      ${s.text ? `<pre class="st-text">${esc(s.text)}</pre>` : ""}
      ${s.error ? `<pre class="st-text bad-t">${esc(s.error)}</pre>` : ""}
    </li>`;
  }
  return `<li class="st st-status"><div class="st-row">${t}<span class="st-badge">状态</span><span class="st-sub">${esc(s.text || "")}</span></div></li>`;
}

// filterState: {type: "all"|"think"|"tool"|"failed", round: "", open: {下标: true}}
export function stepsSectionHTML(d, filterState) {
  const fs = filterState || {};
  const all = d.steps || [];
  const byType = (s) => (fs.type === "think" ? s.type === "think" : fs.type === "tool" ? s.type === "tool" : fs.type === "failed" ? stFailed(s) : true);
  const failed = all.filter(stFailed).length;
  const steps = all.map((s, i) => ({ s, i })).filter(({ s }) => (!fs.round || s.round_id === fs.round) && byType(s));
  const sf = (k) => `aria-pressed="${fs.type === k || (!fs.type && k === "all")}"`;
  const chips =
    `<button class="filter" type="button" data-act="board-step-filter" data-sf="all" ${sf("all")}>全部<span class="n">${all.length}</span></button>` +
    `<button class="filter" type="button" data-act="board-step-filter" data-sf="think" ${sf("think")}>思考<span class="n">${all.filter((x) => x.type === "think").length}</span></button>` +
    `<button class="filter" type="button" data-act="board-step-filter" data-sf="tool" ${sf("tool")}>工具<span class="n">${all.filter((x) => x.type === "tool").length}</span></button>` +
    `<button class="filter" type="button" data-act="board-step-filter" data-sf="failed" ${sf("failed")}>失败<span class="n">${failed}</span></button>`;
  const total = d.total_steps || all.length;
  const note =
    d.truncated || total > STEP_CAP || all.length >= STEP_CAP
      ? `<p class="h-meta">共 ${total} 步，中间已省略部分步骤，仅保留首尾关键记录。</p>`
      : "";
  const hasTopCtx = d && d.context_map && (d.context_map.used_tokens || (Array.isArray(d.context_map.parts) && d.context_map.parts.length));
  const topCtx = hasTopCtx
    ? `<details class="st-io bi-ctx" data-i="c:top"${fs.open && fs.open["c:top"] ? " open" : ""}><summary>本轮上下文窗口 · ${tokens(d.context_map.used_tokens || 0)} / ${tokens(d.context_map.limit_tokens || 128000)} token</summary>${ctxMapHTML(d.context_map, { id: "bi-main", actPrefix: "board-ctx" })}</details>`
    : "";
  return `${topCtx}${roundsHTML(d, fs)}
  <div class="st-head"><h2 class="h-sub">过程 <small>${steps.length} 步 · 工具 ${all.filter((x) => x.type === "tool").length} · 思考 ${all.filter((x) => x.type === "think").length}</small></h2>${steps.length > 8 ? `<button class="btn sm" data-act="board-jump-end" type="button">跳到最新</button>` : ""}</div>
  <div class="st-filters" role="group" aria-label="筛选过程步骤">${chips}</div>
  ${steps.length ? `<ol class="steps">${steps.map(({ s, i }) => stepHTML(s, i, fs.open)).join("")}</ol>` : `<p class="h-meta">当前筛选下没有步骤。</p>`}
  ${note}`;
}
