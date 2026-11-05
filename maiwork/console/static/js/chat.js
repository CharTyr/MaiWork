// MaiWork 网页 · 「和 MaiWork 聊」（管理员主模型对话）。
import { $, admin, state } from "./state.js";
import { SVG, dayWord, esc, ico, toast, when } from "./util.js";
import { agentFish, api } from "./api.js";
import { toolSummary } from "./board_steps.js";
import { loading } from "./pages/news.js";
import { closeSheet } from "./sheet.js";
import { avatar, render, renderSide } from "./render.js";
import { go } from "./router.js";
import { composerHTML, ensureChatCatalog, syncComposerBar } from "./chat_slash.js";

/* ───────────── 轻量 Markdown 渲染 ───────────── */
// 先整体转义，再认常用写法（加粗、行内代码、链接、列表、标题、代码块）；技能详情预览与对话消息共用。
export function mdLite(src, opts = {}) {
  const copyable = opts.copyable !== false;
  const parts = String(src || "").split(/```/);
  return parts
    .map((chunk, i) => {
      if (i % 2 === 1) {
        const code = chunk.replace(/^[a-zA-Z0-9_-]*\n/, "");
        if (!copyable) return `<pre class="md-code">${esc(code)}</pre>`;
        return `<div class="md-code-wrap"><button type="button" class="md-code-copy" data-act="chat-copy-code" aria-label="复制代码" title="复制代码">${SVG.copy}<span>复制</span></button><pre class="md-code">${esc(code)}</pre></div>`;
      }
      const lines = esc(chunk).split("\n");
      let html = "";
      let list = null;
      const inline = (t) =>
        t
          .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
          .replace(/`([^`\n]{1,200})`/g, '<code class="icode">$1</code>')
          .replace(/\[([^\]\n]{1,120})\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
      const flush = () => {
        if (list) html += `</${list}>`;
        list = null;
      };
      for (const raw of lines) {
        const line = raw.trimEnd();
        let m;
        if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
          if (list !== "ul") (flush(), (html += "<ul>"), (list = "ul"));
          html += `<li>${inline(m[1])}</li>`;
        } else if ((m = line.match(/^\s*\d+[.)、]\s+(.*)$/))) {
          if (list !== "ol") (flush(), (html += "<ol>"), (list = "ol"));
          html += `<li>${inline(m[1])}</li>`;
        } else if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
          flush();
          html += `<p class="md-h">${inline(m[1])}</p>`;
        } else if (!line.trim()) {
          flush();
        } else {
          flush();
          html += `<p>${inline(line)}</p>`;
        }
      }
      flush();
      return html;
    })
    .join("");
}

export async function loadChats() {
  try {
    const r = await api("GET", "/api/chat");
    state.chats = r.chats || [];
  } catch (e) {
    state.chats = state.chats || [];
    toast(e.message, true);
  }
}

export async function loadChat(id, incremental) {
  const C = state.chat && state.chat.id === id && incremental ? state.chat : { id, messages: [], pending: [], running: false, info: null, status: null };
  const last = C.messages.length ? C.messages[C.messages.length - 1].id : 0;
  try {
    const r = await api("GET", `/api/chat/${encodeURIComponent(id)}${incremental && last ? `?after=${last}` : ""}`);
    const seen = new Set(C.messages.map((m) => m.id));
    C.messages = C.messages.concat((r.messages || []).filter((m) => !seen.has(m.id)));
    C.pending = r.pending || [];
    C.running = !!r.running;
    C.info = r.chat || C.info;
    if (r.status) C.status = r.status;
    state.chat = C;
    if (state.loadErr) delete state.loadErr.chat;
    void ensureChatCatalog((C.info && C.info.group_id) || "");
  } catch (e) {
    if (e.status === 404) {
      state.chat = null;
      state.chatId = null;
    } else {
      if (!state.chat) (state.loadErr = state.loadErr || {}).chat = e.message;
      toast(e.message, true);
    }
  }
}
const chatLoading = () => loading((state.loadErr || {}).chat, "chat");

let chatTimer = null;
export function chatPoll() {
  clearTimeout(chatTimer);
  const C = state.chat;
  if (state.page !== "chat" || !C || !(C.running || (C.pending || []).length)) return;
  chatTimer = setTimeout(async () => {
    const before = C.messages.length;
    const wasRunning = C.running;
    await loadChat(C.id, true);
    if (state.page !== "chat" || !state.chat || state.chat.id !== C.id) return;
    if (state.chat.messages.length !== before || state.chat.running !== wasRunning) {
      paintChat(true);
      if (!state.chat.running) loadChats().then(() => renderSide());
    }
    chatPoll();
  }, C.running ? 1500 : 5000);
}

function isGroupableTool(m) {
  if (!m || m.role !== "tool" || (m.meta && m.meta.kind === "summary")) return false;
  const meta = m.meta || {};
  if (meta.ok === false) return false;
  return !/^已请求管理员确认/.test(String(m.content || ""));
}

function chatToolMsg(m) {
  const meta = m.meta || {};
  const ok = meta.ok !== false;
  const asked = ok && /^已请求管理员确认/.test(String(m.content || ""));
  const pid = asked ? (String(m.content).match(/编号\s*(\d+)/) || [])[1] : null;
  const waiting = asked && ((state.chat && state.chat.pending) || []).some((p) => String(p.id) === String(pid) && p.status === "pending");
  const open = state.chatOpen && state.chatOpen[m.id];
  const brief = toolSummary(m.name || "", meta.args !== undefined ? meta.args : meta.input, m.content);
  return `
    <div class="cm-tool ${ok ? "" : "bad"}${asked ? " asked" : ""}${asked && !waiting ? " done" : ""}">
      <button class="cm-tool-h" type="button" data-act="chat-tool" data-id="${m.id}" aria-expanded="${!!open}">
        <span class="dot ${asked && waiting ? "pending" : asked ? "" : ok ? "ok" : "failed"}"></span>
        <span class="cm-tool-l">${esc(meta.label || m.name || "调用工具")}${brief ? `<span class="cm-tool-arg"> · ${esc(brief)}</span>` : ""}</span>${asked ? `<span class="cm-tool-s">${waiting ? "待确认" : "已请求确认"}</span>` : ""}
        ${SVG.down}
      </button>
      ${open ? `<pre class="lm-text">${esc(m.content || "")}</pre>` : ""}
    </div>`;
}

function chatToolGroupHTML(run) {
  const gid = run[0].id;
  const open = !!(state.chatOpen && state.chatOpen["g" + gid]);
  const names = [...new Set(run.map((m) => (m.meta && m.meta.label) || m.name || "工具"))].slice(0, 3).join("、");
  return `
    <div class="cm-tool cm-tool-group${open ? " open" : ""}">
      <button class="cm-tool-h" type="button" data-act="chat-tool-group" data-gid="${gid}" aria-expanded="${open}">
        <span class="dot ok"></span>
        <span class="cm-tool-l">已连续执行 ${run.length} 步工具调用${names ? `<span class="cm-tool-arg"> · ${esc(names)}</span>` : ""}</span>
        ${SVG.down}
      </button>
      ${open ? `<div class="cm-tool-group-b">${run.map(chatToolMsg).join("")}</div>` : ""}
    </div>`;
}

function chatSummaryHTML(m) {
  const meta = m.meta || {};
  const open = state.chatOpen && state.chatOpen["s" + m.id];
  const span = meta.from_ts && meta.to_ts ? `${when(meta.from_ts)} – ${when(meta.to_ts)}` : "";
  return `
    <div class="cm-summary${open ? " open" : ""}">
      <button class="cm-summary-h" type="button" data-act="chat-summary" data-id="${m.id}" aria-expanded="${!!open}">
        ${ico("memo", "")}<span class="cm-summary-l">已整理成摘要${meta.covers ? `<small>前 ${meta.covers} 条${span ? ` · ${esc(span)}` : ""}</small>` : ""}</span>${SVG.down}
      </button>
      ${open ? `<div class="cm-summary-b md">${mdLite(m.content || "", { copyable: false })}</div>` : ""}
    </div>`;
}

function chatMsgHTML(m) {
  if (m.meta && m.meta.kind === "summary") return chatSummaryHTML(m);
  if (m.role === "user") {
    const sk = m.meta && m.meta.skill;
    const isGroup = sk && (sk.scope === "group" || String(sk.name || "").startsWith("本群/"));
    return `<div class="cm cm-user"><div class="cm-user-col">${
      sk && sk.name ? `<div class="cm-skill-tag">${ico(isGroup ? "pushpin" : "sparkles", "cm-skill-ico")}<span>/${esc(sk.name)}</span></div>` : ""
    }<div class="cm-bubble">${esc(m.content || "")}</div></div></div>`;
  }
  if (m.role === "tool") return chatToolMsg(m);
  if (m.role === "system_note") return `<div class="cm-note">${esc(m.content || "")}</div>`;
  const meta = m.meta || {};
  const reasoning = String(meta.reasoning || "").trim();
  const redacted = !!meta.reasoning_redacted && !reasoning;
  const thinkHTML = reasoning
    ? `<details class="cm-think"><summary>思考过程</summary><pre class="cm-think-b">${esc(reasoning)}</pre></details>`
    : redacted
      ? `<div class="cm-think cm-think-redacted">思考过程已隐藏</div>`
      : "";
  const hasText = Boolean(String(m.content || "").trim());
  if (!hasText) {
    if (!thinkHTML) return "";
    return `<div class="cm cm-bot cm-think-only"><span class="cm-ava cm-fish">${agentFish("main", 30, { still: true })}</span><div class="cm-bot-col">${thinkHTML}</div></div>`;
  }
  return `
    <div class="cm cm-bot">
      <img class="cm-ava" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" />
      <div class="cm-bot-col">
        ${thinkHTML}
        <div class="cm-text md">${mdLite(m.content)}</div>
        <div class="cm-foot">
          <button type="button" class="cm-act-btn" data-act="chat-copy-msg" data-id="${m.id}" title="复制回复">${SVG.copy}<span>复制</span></button>
        </div>
      </div>
    </div>`;
}

function renderMessageList(messages) {
  const out = [];
  const list = messages || [];
  let i = 0;
  while (i < list.length) {
    if (isGroupableTool(list[i])) {
      let j = i + 1;
      while (j < list.length && isGroupableTool(list[j])) j++;
      const run = list.slice(i, j);
      if (run.length >= 3) out.push(chatToolGroupHTML(run));
      else run.forEach((m) => out.push(chatToolMsg(m)));
      i = j;
      continue;
    }
    out.push(chatMsgHTML(list[i]));
    i++;
  }
  return out.join("");
}

function pendingCard(p) {
  return `
    <div class="cm-pending">
      <div class="cm-pending-h">${ico("lock")}<span>请确认</span></div>
      <div class="cm-pending-t">${esc(p.summary || p.tool)}</div>
      ${
        p.args && Object.keys(p.args).length
          ? `<details class="cm-pending-d"><summary>查看详情</summary><pre class="lm-text">${esc(JSON.stringify(p.args, null, 2))}</pre></details>`
          : ""
      }
      <div class="actions">
        <button class="btn primary small" type="button" data-act="chat-confirm" data-id="${p.id}" data-ok="1">确认</button>
        <button class="btn small" type="button" data-act="chat-confirm" data-id="${p.id}" data-ok="0">拒绝</button>
      </div>
    </div>`;
}

const CHAT_SUGGEST = [
  "各群最近的情况如何？",
  "今天的资讯为什么较少？",
  "将 19:00 的资讯搜集改到 20:30",
  "留意群内询问 NAS 的成员",
];

function liveStepLabel(C) {
  const list = (C && C.messages) || [];
  const last = list.length ? list[list.length - 1] : null;
  if (!last) return "正在思考…";
  if (last.role === "tool") {
    const name = (last.meta && last.meta.label) || last.name || "工具";
    return `已完成「${name}」，正在整理结果…`;
  }
  if (last.role === "assistant" && (last.tool_calls || []).length) return "正在执行工具调用…";
  return "正在生成回复…";
}

function chatStream() {
  const C = state.chat;
  if (!C) return "";
  const msgs = renderMessageList(C.messages);
  const pend = (C.pending || []).filter((p) => p.status === "pending").map(pendingCard).join("");
  const last = C.messages.length ? C.messages[C.messages.length - 1] : null;
  const showRetry = !C.running && last && last.role === "system_note" && last.meta && (last.meta.kind === "stopped" || last.meta.kind === "turn_error");
  const hasPending = (C.pending || []).some((p) => p.status === "pending");
  const retryRow = showRetry
    ? `<div class="cm-retry-row"><button type="button" class="btn small" data-act="chat-retry" ${hasPending ? 'disabled title="请先处理待确认动作"' : ""}>重试本轮</button></div>`
    : "";
  const empty = !C.messages.length
    ? `<div class="chat-empty">
        <div class="chat-empty-fish">${agentFish("main", 64)}</div>
        <p>请输入需要处理的事项，或输入 <code class="icode">/</code> 调用技能与指令</p>
        <div class="chat-sugs">${CHAT_SUGGEST.map((t) => `<button class="btn small" type="button" data-act="chat-suggest" data-t="${esc(t)}">${esc(t)}</button>`).join("")}</div>
      </div>`
    : "";
  const typing = C.running
    ? `<div class="cm cm-bot cm-typing"><span class="cm-ava cm-fish">${agentFish("main", 30)}</span><div class="cm-typing-box"><div class="cm-dots"><i></i><i></i><i></i></div><span class="cm-live-step">${esc(liveStepLabel(C))}</span></div></div>`
    : "";
  return empty + msgs + pend + retryRow + typing;
}

export function chatPage() {
  const C = state.chat;
  return `
    <div class="chat-head">
      <h1 class="h-page h-fish">${agentFish("main", 38)}<span>聊天</span></h1>
      <div class="chat-tools">
        <button class="btn small chat-list-btn" type="button" data-act="chat-list">对话</button>
        <button class="btn small" type="button" data-act="chat-new">新对话</button>
      </div>
    </div>
    <div class="chat-stream" id="chat-stream">${C ? chatStream() : chatLoading()}</div>
    ${composerHTML(C)}`;
}

export function chatSide() {
  const list = state.chats || [];
  return `
    <div class="h-sub-row" style="margin-top:6px"><h2 class="h-sub">对话</h2><button class="btn small" type="button" data-act="chat-new">新对话</button></div>
    ${
      list.length
        ? list
            .map(
              (c) => `
        <div class="chat-item-row${state.chatId === c.id ? " on" : ""}">
          <button class="chat-item${state.chatId === c.id ? " on" : ""}" type="button" data-act="chat-open" data-id="${c.id}">
            <span class="chat-item-t">${esc(c.title || "新对话")}${c.running ? `<span class="dot running"></span>` : ""}</span>
            <span class="chat-item-s">${esc(dayWord(c.updated))}${c.group_name ? ` · ${esc(c.group_name)}` : ""}</span>
          </button>
          <div class="chat-item-acts">
            <button class="icon-btn" type="button" data-act="chat-rename" data-id="${c.id}" aria-label="重命名" title="重命名">${SVG.pen}</button>
            <button class="icon-btn" type="button" data-act="chat-archive" data-id="${c.id}" aria-label="归档" title="归档">${SVG.archive}</button>
          </div>
        </div>`
            )
            .join("")
        : `<p class="h-meta">暂无对话</p>`
    }`;
}

// 只重画消息区与输入坞底栏，保住输入框里正在打的字和滚动位置
export function paintChat(stickBottom) {
  const box = $("chat-stream");
  if (!box || state.page !== "chat") return;
  const nearBottom = box.getBoundingClientRect().bottom - window.innerHeight < 160;
  box.innerHTML = state.chat ? chatStream() : chatLoading();
  syncComposerBar();
  if (stickBottom || nearBottom) window.scrollTo({ top: document.body.scrollHeight, behavior: stickBottom === "smooth" ? "smooth" : "auto" });
}

export async function enterChat(id) {
  if (!admin()) return;
  closeSheet();
  await loadChats();
  let cid = id || state.chatId || (state.chats[0] && state.chats[0].id);
  if (!cid) {
    const c = await api("POST", "/api/chat", { group_id: state.g || "" });
    state.chats = [c].concat(state.chats || []);
    cid = c.id;
  }
  state.chatId = cid;
  state.chat = null;
  if (state.loadErr) delete state.loadErr.chat;
  go({ page: "chat", detail: null });
  await loadChat(cid, false);
  render();
  paintChat(true);
  chatPoll();
}
