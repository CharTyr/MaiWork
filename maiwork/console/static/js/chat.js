// MaiWork 网页 · 「和 MaiWork 聊」（管理员）。
import { $, admin, state } from "./state.js";
import { SVG, dayWord, esc, ico, toast, when } from "./util.js";
import { api, gname } from "./api.js";
import { loading } from "./pages/news.js";
import { closeSheet } from "./sheet.js";
import { avatar, render, renderSide } from "./render.js";
import { go } from "./router.js";

/* ───────────── 和 MaiWork 聊（管理员） ───────────── */
// 轻量 Markdown：先整体转义，再认少数几种写法（加粗、行内代码、链接、列表、标题、代码块）
function mdLite(src) {
  const parts = String(src || "").split(/```/);
  return parts
    .map((chunk, i) => {
      if (i % 2 === 1) return `<pre class="md-code">${esc(chunk.replace(/^[a-zA-Z0-9_-]*\n/, ""))}</pre>`;
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
  const C = state.chat && state.chat.id === id && incremental ? state.chat : { id, messages: [], pending: [], running: false, info: null };
  const last = C.messages.length ? C.messages[C.messages.length - 1].id : 0;
  try {
    const r = await api("GET", `/api/chat/${encodeURIComponent(id)}${incremental && last ? `?after=${last}` : ""}`);
    const seen = new Set(C.messages.map((m) => m.id));
    C.messages = C.messages.concat((r.messages || []).filter((m) => !seen.has(m.id)));
    C.pending = r.pending || [];
    C.running = !!r.running;
    C.info = r.chat || C.info;
    state.chat = C;
  } catch (e) {
    if (e.status === 404) {
      state.chat = null;
      state.chatId = null;
    } else toast(e.message, true);
  }
}

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

function chatToolMsg(m) {
  const meta = m.meta || {};
  const ok = meta.ok !== false;
  // 要确认的动作：工具层只记了小票，没真做——别画成绿点「做完了」
  const asked = ok && /^已请求管理员确认/.test(String(m.content || ""));
  // 小票还挂着 → 「待你确认」；已经同意/拒绝过 → 灰掉，结果看下面的系统提示
  const pid = asked ? (String(m.content).match(/编号\s*(\d+)/) || [])[1] : null;
  const waiting = asked && ((state.chat && state.chat.pending) || []).some((p) => String(p.id) === String(pid) && p.status === "pending");
  const open = state.chatOpen && state.chatOpen[m.id];
  return `
    <div class="cm-tool ${ok ? "" : "bad"}${asked ? " asked" : ""}${asked && !waiting ? " done" : ""}">
      <button class="cm-tool-h" data-act="chat-tool" data-id="${m.id}" aria-expanded="${!!open}">
        <span class="dot ${asked && waiting ? "pending" : asked ? "" : ok ? "ok" : "failed"}"></span>
        <span class="cm-tool-l">${esc(meta.label || m.name || "用了工具")}</span>${asked ? `<span class="cm-tool-s">${waiting ? "等你确认" : "已问过"}</span>` : ""}
        ${SVG.down}
      </button>
      ${open ? `<pre class="lm-text">${esc(m.content || "")}</pre>` : ""}
    </div>`;
}

// 整理出来的摘要（meta.kind = summary）：默认收起，点开看全文
function chatSummaryHTML(m) {
  const meta = m.meta || {};
  const open = state.chatOpen && state.chatOpen["s" + m.id];
  const span = meta.from_ts && meta.to_ts ? `${when(meta.from_ts)} – ${when(meta.to_ts)}` : "";
  return `
    <div class="cm-summary${open ? " open" : ""}">
      <button class="cm-summary-h" data-act="chat-summary" data-id="${m.id}" aria-expanded="${!!open}">
        ${ico("memo", "")}<span class="cm-summary-l">已整理成摘要${meta.covers ? `<small>前 ${meta.covers} 条${span ? ` · ${esc(span)}` : ""}</small>` : ""}</span>${SVG.down}
      </button>
      ${open ? `<div class="cm-summary-b md">${mdLite(m.content || "")}</div>` : ""}
    </div>`;
}

function chatMsgHTML(m) {
  if (m.meta && m.meta.kind === "summary") return chatSummaryHTML(m);
  if (m.role === "user") return `<div class="cm cm-user"><div class="cm-bubble">${esc(m.content || "")}</div></div>`;
  if (m.role === "tool") return chatToolMsg(m);
  if (m.role === "system_note") return `<div class="cm-note">${esc(m.content || "")}</div>`;
  // assistant：有字就显示；只有工具调用没字的，工具卡片自己会出现
  const calls = (m.tool_calls || []).length;
  if (!String(m.content || "").trim()) return calls ? "" : "";
  return `
    <div class="cm cm-bot">
      <img class="cm-ava" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" />
      <div class="cm-text md">${mdLite(m.content)}</div>
    </div>`;
}

function pendingCard(p) {
  return `
    <div class="cm-pending">
      <div class="cm-pending-h">${ico("lock")}<span>请确认</span></div>
      <div class="cm-pending-t">${esc(p.summary || p.tool)}</div>
      ${
        p.args && Object.keys(p.args).length
          ? `<details class="cm-pending-d"><summary>看细节</summary><pre class="lm-text">${esc(JSON.stringify(p.args, null, 2))}</pre></details>`
          : ""
      }
      <div class="actions">
        <button class="btn primary small" data-act="chat-confirm" data-id="${p.id}" data-ok="1">确认</button>
        <button class="btn small" data-act="chat-confirm" data-id="${p.id}" data-ok="0">不要</button>
      </div>
    </div>`;
}

const CHAT_SUGGEST = [
  "这几个群最近怎么样？",
  "今天资讯怎么这么少？",
  "把 19:00 找资讯改到 20:30",
  "帮我留意群里谁问 NAS",
];

function chatStream() {
  const C = state.chat;
  if (!C) return "";
  const msgs = C.messages.map(chatMsgHTML).join("");
  const pend = (C.pending || []).filter((p) => p.status === "pending").map(pendingCard).join("");
  const empty = !C.messages.length
    ? `<div class="chat-empty">
        <img src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" />
        <p>想做什么，直接说</p>
        <div class="chat-sugs">${CHAT_SUGGEST.map((t) => `<button class="btn small" data-act="chat-suggest" data-t="${esc(t)}">${esc(t)}</button>`).join("")}</div>
      </div>`
    : "";
  const typing = C.running ? `<div class="cm cm-bot cm-typing"><img class="cm-ava" src="${esc(avatar())}" onerror="this.onerror=null;this.src='/static/assets/logo.png'" alt="" /><div class="cm-dots"><i></i><i></i><i></i></div></div>` : "";
  return empty + msgs + pend + typing;
}

export function chatPage() {
  const C = state.chat;
  const info = (C && C.info) || {};
  const groups = state.groups || [];
  return `
    <div class="chat-head">
      <h1 class="h-page">聊天</h1>
      <div class="chat-tools">
        ${
          C
            ? `<select class="chat-group" data-act="chat-group" aria-label="聊哪个群">
                <option value="">所有群</option>
                ${groups.map((g) => `<option value="${esc(g.id)}" ${info.group_id === g.id ? "selected" : ""}>${esc(gname(g))}</option>`).join("")}
              </select>`
            : ""
        }
        ${C && C.messages.length ? `<button class="btn small" data-act="chat-compact" title="整理成摘要，省用量不忘事">压缩对话</button>` : ""}
        <button class="btn small chat-list-btn" data-act="chat-list">对话</button>
        <button class="btn small" data-act="chat-new">新对话</button>
      </div>
    </div>
    <div class="chat-stream" id="chat-stream">${C ? chatStream() : loading()}</div>
    <form id="chat-form" class="chat-composer" autocomplete="off">
      <textarea id="chat-input" rows="1" placeholder="说点什么…" ${C && C.running ? "" : ""}></textarea>
      <button class="chat-send" type="submit" aria-label="发送" ${C && C.running ? "disabled" : ""}>${SVG.send || "↑"}</button>
    </form>`;
}

export function chatSide() {
  const list = state.chats || [];
  return `
    <div class="h-sub-row" style="margin-top:6px"><h2 class="h-sub">对话</h2><button class="btn small" data-act="chat-new">新对话</button></div>
    ${
      list.length
        ? list
            .map(
              (c) => `
        <button class="chat-item${state.chatId === c.id ? " on" : ""}" data-act="chat-open" data-id="${c.id}">
          <span class="chat-item-t">${esc(c.title || "新对话")}${c.running ? `<span class="dot running"></span>` : ""}</span>
          <span class="chat-item-s">${esc(dayWord(c.updated))}${c.group_name ? ` · ${esc(c.group_name)}` : ""}</span>
        </button>`
            )
            .join("")
        : `<p class="h-meta">还没有对话</p>`
    }`;
}

// 只重画消息区，保住输入框里正在打的字和滚动位置
export function paintChat(stickBottom) {
  const box = $("chat-stream");
  if (!box || state.page !== "chat") return;
  const nearBottom = box.getBoundingClientRect().bottom - window.innerHeight < 160;
  box.innerHTML = state.chat ? chatStream() : loading();
  const send = document.querySelector(".chat-send");
  if (send) send.disabled = !!(state.chat && state.chat.running);
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
  go({ page: "chat", detail: null });
  await loadChat(cid, false);
  render();
  paintChat(true);
  chatPoll();
}
