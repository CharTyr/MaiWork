// MaiWork 网页 · 设置 ·「请求日志」。
import { state } from "../state.js";
import { SVG, esc, toast, tokens } from "../util.js";
import { api } from "../api.js";
import { loading } from "../pages/news.js";

/* ───── 请求日志（管理员） ───── */
export async function loadLogs(more) {
  const L = (state.logs = state.logs || { tab: "model", failed: false, items: [], next: null, summary: null, open: {} });
  const base = L.tab === "model" ? "/api/logs/model-calls" : "/api/logs/tool-calls";
  const q = new URLSearchParams({ limit: "50" });
  if (L.failed) q.set("failed", "1");
  if (more && L.next) q.set("before_id", String(L.next));
  try {
    const [list, summary] = await Promise.all([api("GET", `${base}?${q}`), more ? Promise.resolve(L.summary) : api("GET", "/api/logs/summary")]);
    L.items = more ? L.items.concat(list.items || []) : list.items || [];
    L.next = list.next_before_id || null;
    L.summary = summary;
  } catch (e) {
    toast(e.message, true);
  }
}

export const fmtMs = (ms) => (ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${ms} 毫秒`);
const hms = (ts) => {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

export function logsPage() {
  const L = state.logs;
  if (!L || !L.summary) return loading();
  const t = (L.summary && L.summary.today) || {};
  const lf = L.summary.last_failure;
  return `
    <p class="h-meta">最近 3 天的模型和工具调用</p>
    <div class="usage" style="margin-top:14px">
      <div><b>${t.calls || 0}</b><span>今天调用模型 · 次</span></div>
      <div><b class="${t.failed ? "bad-t" : ""}">${t.failed || 0}</b><span>失败 · 次</span></div>
      <div><b>${t.retried || 0}</b><span>重试 · 次</span></div>
    </div>
    ${lf ? `<div class="warn-box" style="margin-top:12px"><b>最近一次失败：</b>${esc(hms(lf.ts))} · ${esc(lf.purpose_name || lf.purpose || "")} · ${esc(lf.model || "")}<br />${esc(lf.error || "")}</div>` : ""}
    <div class="log-bar">
      <div class="seg" role="tablist">
        <button role="tab" data-act="log-tab" data-t="model" aria-selected="${L.tab === "model"}">模型调用</button>
        <button role="tab" data-act="log-tab" data-t="tool" aria-selected="${L.tab === "tool"}">工具调用</button>
      </div>
      <label class="chk"><input type="checkbox" data-act="log-failed" ${L.failed ? "checked" : ""} />只看失败</label>
      <button class="btn small" data-act="log-refresh">刷新</button>
    </div>
    <div class="logs">
      ${L.items.length ? L.items.map((it) => (L.tab === "model" ? modelLogRow(it) : toolLogRow(it))).join("") : `<p class="h-meta">${L.failed ? "没有失败记录。" : "还没有记录。"}</p>`}
    </div>
    ${L.next ? `<div class="actions" style="justify-content:center"><button class="btn small" data-act="log-more">再往前看</button></div>` : ""}`;
}

function modelLogRow(it) {
  const open = state.logs.open[`m${it.id}`];
  const tok = (it.prompt_tokens || 0) + (it.completion_tokens || 0);
  return `
    <div class="log-row ${it.ok ? "" : "bad"}">
      <button class="log-head" data-act="log-open" data-kind="m" data-id="${it.id}" aria-expanded="${!!open}">
        <span class="dot ${it.ok ? "ok" : "failed"}"></span>
        <span class="log-main">
          <span class="log-title">${esc(it.purpose_name || it.purpose || "模型调用")}${it.attempt > 1 ? `<span class="tag">第 ${it.attempt} 次尝试</span>` : ""}</span>
          <span class="log-sub">${esc(hms(it.ts))} · ${esc(it.model || "")} · ${esc(fmtMs(it.ms))}${tok ? ` · ${tokens(tok)} tokens` : ""}${it.group_name ? ` · ${esc(it.group_name)}` : ""}${it.task_id ? ` · ${esc(it.task_id)}` : ""}</span>
          ${!it.ok && it.error ? `<span class="log-err">${esc(it.error)}</span>` : ""}
        </span>
        ${SVG.down}
      </button>
      ${open ? `<div class="log-detail">${open === "loading" ? loading() : modelLogDetail(open)}</div>` : ""}
    </div>`;
}

function modelLogDetail(d) {
  const req = d.request || {};
  const res = d.response || {};
  const msgs = (req.messages || [])
    .map(
      (m) => `
      <div class="lm">
        <div class="lm-role">${esc({ system: "系统提示", user: "发给模型", assistant: "模型", tool: "工具结果" }[m.role] || m.role)}${m.name ? ` · ${esc(m.name)}` : ""}</div>
        ${m.content ? `<pre class="lm-text">${esc(m.content)}</pre>` : ""}
        ${(m.tool_calls || []).map((tc) => `<pre class="lm-text lm-tc">调用 ${esc(tc.name || "")}：${esc(tc.arguments || "")}</pre>`).join("")}
      </div>`
    )
    .join("");
  return `
    ${d.error ? `<div class="warn-box">${esc(d.error)}${d.status ? `（HTTP ${d.status}）` : ""}</div>` : ""}
    <div class="ld-meta">${req.json_mode ? "要求返回 JSON · " : ""}${(req.tools || []).length ? `可用工具：${req.tools.map(esc).join("、")}` : "没给工具"}</div>
    <h3 class="ld-h">请求</h3>
    ${msgs || `<p class="h-meta">（没记下）</p>`}
    <h3 class="ld-h">回复</h3>
    ${
      d.ok
        ? `${res.text ? `<pre class="lm-text">${esc(res.text)}</pre>` : ""}${(res.tool_calls || []).map((tc) => `<pre class="lm-text lm-tc">调用 ${esc(tc.name || "")}：${esc(tc.arguments || "")}</pre>`).join("")}${!res.text && !(res.tool_calls || []).length ? `<p class="h-meta">（空）</p>` : ""}`
        : `<p class="h-meta">这次没拿到回复。</p>`
    }`;
}

function toolLogRow(it) {
  const open = state.logs.open[`t${it.id}`];
  return `
    <div class="log-row ${it.ok ? "" : "bad"}">
      <button class="log-head" data-act="log-open" data-kind="t" data-id="${it.id}" aria-expanded="${!!open}">
        <span class="dot ${it.ok ? "ok" : "failed"}"></span>
        <span class="log-main">
          <span class="log-title mono">${esc(it.tool || "")}<span class="tag">${esc(it.actor || "")}</span></span>
          <span class="log-sub">${esc(hms(it.ts))} · ${esc(fmtMs(it.ms))}${it.group_name ? ` · ${esc(it.group_name)}` : ""}${it.task_id ? ` · ${esc(it.task_id)}` : ""}</span>
          <span class="log-in">${esc(it.input || "")}</span>
          ${!it.ok && it.error ? `<span class="log-err">${esc(it.error)}</span>` : ""}
        </span>
        ${SVG.down}
      </button>
      ${
        open
          ? `<div class="log-detail">${
              open === "loading"
                ? loading()
                : `<h3 class="ld-h">输入</h3><pre class="lm-text">${esc(open.input || "")}</pre><h3 class="ld-h">${open.ok ? "输出" : "出错"}</h3><pre class="lm-text">${esc((open.ok ? open.output : open.error || open.output) || "")}</pre>`
            }</div>`
          : ""
      }
    </div>`;
}
