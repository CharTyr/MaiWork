// MaiWork 网页 · 主模型对话输入坞、斜杠指令与技能面板、@ 引用面板、对话动作。
import { $, admin, state } from "./state.js";
import { SVG, esc, flashCopied, ico, reveal, tokens, toast } from "./util.js";
import { api, gname } from "./api.js";
import { closeSheet, openSheet } from "./sheet.js";
import { renderSide } from "./render.js";
import { chatPoll, enterChat, loadChat, loadChats, paintChat } from "./chat.js";
import { ctxMapHTML } from "./ctxmap.js";

export const SLASH_COMMANDS = [
  { key: "/compact", cmd: "compact", title: "/compact", hint: "[侧重说明]", description: "将历史消息整理为摘要，可附加侧重说明" },
  { key: "/clear", cmd: "clear", title: "/clear", hint: "", description: "归档当前对话并开启新对话" },
  { key: "/new", cmd: "new", title: "/new", hint: "", description: "保持当前群焦点开启新对话" },
];

export function filterSlashItems(q, skills = []) {
  const raw = String(q || "").trim().replace(/^\//, "").toLowerCase();
  const cmds = SLASH_COMMANDS.map((c) => ({
    kind: "command",
    key: c.key,
    cmd: c.cmd,
    title: c.title,
    hint: c.hint,
    description: c.description,
    scope: "cmd",
  }));
  const sks = (skills || []).map((s) => ({
    kind: "skill",
    key: `/${s.name}`,
    name: s.name,
    title: s.title || s.name,
    description: s.description || "",
    scope: s.scope || "global",
    group_id: s.group_id || "",
  }));
  const all = cmds.concat(sks);
  if (!raw) return all;
  return all.filter((it) =>
    [it.key, it.name, it.title, it.description].some((v) => String(v || "").toLowerCase().includes(raw))
  );
}

export function parseTypedSlash(text, skills = []) {
  const trimmed = String(text || "").trim();
  if (!trimmed.startsWith("/")) return { kind: "none", cmd: null, skill: null, rest: trimmed };
  const sp = trimmed.search(/\s/);
  const token = (sp < 0 ? trimmed.slice(1) : trimmed.slice(1, sp)).trim();
  const rest = sp < 0 ? "" : trimmed.slice(sp + 1).trim();
  if (!token) return { kind: "none", cmd: null, skill: null, rest: trimmed };
  const cmdHit = SLASH_COMMANDS.find((c) => c.cmd === token.toLowerCase());
  if (cmdHit) return { kind: "command", cmd: cmdHit.cmd, skill: null, rest };
  const low = token.toLowerCase();
  const skHit = (skills || []).find(
    (s) => String(s.name || "").toLowerCase() === low || String(s.title || "").toLowerCase() === low
  );
  if (skHit) return { kind: "skill", cmd: null, skill: skHit, rest };
  return { kind: "none", cmd: null, skill: null, rest: trimmed };
}

export function filterRefItems(q, refs = []) {
  const raw = String(q || "").trim().replace(/^@/, "").toLowerCase();
  const list = Array.isArray(refs) ? refs : [];
  if (!raw) return list.slice(0, 24);
  return list
    .filter((r) => [r.id, r.label, r.title, r.group_name].some((v) => String(v || "").toLowerCase().includes(raw)))
    .slice(0, 24);
}

export function contextRingView(status) {
  const s = status || {};
  const used = Math.max(0, Number(s.estimated_input_tokens) || 0);
  const win = Math.max(1, Number(s.context_window) || 128000);
  const pct = Math.min(100, Math.max(0, Math.round((used / win) * 100)));
  const modelLabel = s.ready === false && !s.model ? "未配置模型" : String(s.model || "主模型");
  const tone = pct >= 92 ? "bad" : pct >= 75 ? "warn" : "ok";
  return {
    pct,
    used,
    window: win,
    trigger: Math.max(0, Number(s.trigger_threshold) || 0),
    covers: Math.max(0, Number(s.summary_covers) || 0),
    modelLabel,
    ready: s.ready !== false,
    canCompact: Boolean(s.has_uncompacted),
    tone,
  };
}

export async function ensureChatCatalog(groupId = "", force = false) {
  if (!admin()) return;
  const gid = String(groupId || "");
  if (!force && state.chatCatalog && state.chatCatalog.groupId === gid && !state.chatCatalog.loading) return;
  state.chatCatalog = { groupId: gid, skills: (state.chatCatalog && state.chatCatalog.skills) || [], refs: (state.chatCatalog && state.chatCatalog.refs) || [], loading: true };
  const qs = gid ? `?group_id=${encodeURIComponent(gid)}` : "";
  try {
    const [sk, rf] = await Promise.all([
      api("GET", `/api/chat/skills${qs}`).catch(() => ({ skills: [] })),
      api("GET", `/api/chat/refs${qs}`).catch(() => ({ refs: [] })),
    ]);
    if (state.chatCatalog && state.chatCatalog.groupId === gid) {
      state.chatCatalog = { groupId: gid, skills: sk.skills || [], refs: rf.refs || [], loading: false };
      syncComposerPop();
    }
  } catch (_) {
    if (state.chatCatalog) state.chatCatalog.loading = false;
  }
}

function currentMenuItems() {
  const menu = state.chatMenu;
  if (!menu) return [];
  const cat = state.chatCatalog || { skills: [], refs: [] };
  return menu.kind === "slash" ? filterSlashItems(menu.q, cat.skills) : filterRefItems(menu.q, cat.refs);
}

function skillPillHTML() {
  const sk = state.chatSkill;
  if (!sk || !sk.name) return "";
  const isGroup = sk.scope === "group" || String(sk.name).startsWith("本群/");
  return `<div class="chat-skill-pill">
    ${ico(isGroup ? "pushpin" : "sparkles", "chat-skill-ico")}
    <span class="chat-skill-name">/${esc(sk.name)}</span>
    ${isGroup ? `<span class="tag">本群做法</span>` : `<span class="tag">技能</span>`}
    ${sk.description ? `<span class="chat-skill-desc">${esc(sk.description)}</span>` : ""}
    <button type="button" class="chat-skill-rm" data-act="chat-skill-clear" aria-label="移除所选技能" title="移除">${SVG.close}</button>
  </div>`;
}

function ctxPopoverHTML(C) {
  if (!state.chatCtxOpen || !C) return "";
  const r = contextRingView(C.status);
  const cm = C.status && C.status.context_map;
  const hasMap = cm && (cm.used_tokens || (Array.isArray(cm.parts) && cm.parts.length));
  return `<div class="chat-ctx-pop${hasMap ? " has-map" : ""}" role="dialog" aria-label="上下文与模型状态">
    <div class="chat-ctx-head"><b>${esc(r.modelLabel)}</b><span class="ntag ${r.ready ? "ok" : "warn"}">${r.ready ? "就绪" : "未就绪"}</span></div>
    <div class="chat-ctx-grid">
      <div><span>当前估算</span><b>${tokens(r.used)}</b></div>
      <div><span>上下文窗口</span><b>${tokens(r.window)}</b></div>
      <div><span>自动整理线</span><b>${r.trigger ? tokens(r.trigger) : "—"}</b></div>
      <div><span>已摘要消息</span><b>${r.covers} 条</b></div>
    </div>
    ${hasMap ? ctxMapHTML(cm, { id: "chat-ctx", compact: true, actPrefix: "ctxmap" }) || "" : `<div class="chat-ctx-bar"><i style="width:${r.pct}%"></i></div>`}
    <div class="chat-ctx-foot">
      <span class="fine">已占用 ${r.pct}% 窗口</span>
      <button type="button" class="btn small" data-act="chat-compact" ${C.running || !r.canCompact ? "disabled" : ""}>压缩对话</button>
    </div>
  </div>`;
}

function menuPopoverHTML() {
  const menu = state.chatMenu;
  if (!menu) return "";
  const items = currentMenuItems();
  const idx = Math.min(Math.max(0, menu.idx || 0), Math.max(0, items.length - 1));
  if (menu.kind === "slash") {
    return `<div class="chat-pop" role="listbox" aria-label="斜杠指令与技能">
      <div class="chat-pop-h"><span>指令与可用技能</span><span class="fine">↑↓ 选择 · Enter 填入</span></div>
      ${
        items.length
          ? items
              .map(
                (it, i) => `<button type="button" role="option" aria-selected="${i === idx}" class="chat-pop-item${i === idx ? " on" : ""}" data-act="chat-menu-pick" data-idx="${i}">
              <span class="chat-pop-ico">${it.kind === "command" ? `<span class="chat-cmd-badge">/</span>` : ico(it.scope === "group" ? "pushpin" : "sparkles", "ico")}</span>
              <span class="chat-pop-main">
                <span class="chat-pop-t"><span class="mono">${esc(it.key)}</span>${it.hint ? ` <span class="fine">${esc(it.hint)}</span>` : ""}${it.scope === "group" ? `<span class="tag">本群做法</span>` : it.kind === "skill" ? `<span class="tag">技能</span>` : `<span class="tag">指令</span>`}</span>
                <span class="chat-pop-d">${esc(it.description || "按该技能说明执行")}</span>
              </span>
            </button>`
              )
              .join("")
          : `<div class="chat-pop-empty">无匹配的指令或技能</div>`
      }
    </div>`;
  }
  const kindLabel = { task: "任务", goal: "目标", idea: "构想" };
  return `<div class="chat-pop" role="listbox" aria-label="引用任务、目标或构想">
    <div class="chat-pop-h"><span>引用群内事项</span><span class="fine">↑↓ 选择 · Enter 插入</span></div>
    ${
      items.length
        ? items
            .map(
              (r, i) => `<button type="button" role="option" aria-selected="${i === idx}" class="chat-pop-item${i === idx ? " on" : ""}" data-act="chat-menu-pick" data-idx="${i}">
            <span class="chat-pop-ico">${ico(r.kind === "goal" ? "bullseye" : r.kind === "idea" ? "bulb" : "package", "ico")}</span>
            <span class="chat-pop-main">
              <span class="chat-pop-t"><span class="mono">${esc(r.label || r.id)}</span> <span class="tag">${esc(kindLabel[r.kind] || "事项")}</span>${r.group_name ? `<span class="fine">${esc(r.group_name)}</span>` : ""}</span>
              <span class="chat-pop-d">${esc(r.title || "")}</span>
            </span>
          </button>`
            )
            .join("")
        : `<div class="chat-pop-empty">暂无可引用的事项</div>`
    }
  </div>`;
}

export function composerPopHTML(C) {
  return menuPopoverHTML() + ctxPopoverHTML(C);
}

export function composerBarHTML(C) {
  const info = (C && C.info) || {};
  const groups = state.groups || [];
  const ring = contextRingView(C && C.status);
  const c = 43.98;
  const off = (c * (1 - ring.pct / 100)).toFixed(2);
  return `
    <div class="chat-bar-l">
      <button type="button" class="chat-tool-btn" data-act="chat-slash-btn" title="斜杠指令与技能" aria-label="斜杠指令与技能">/</button>
      <button type="button" class="chat-tool-btn" data-act="chat-ref-btn" title="引用任务、目标或构想" aria-label="引用任务、目标或构想">@</button>
      ${
        C
          ? `<select class="chat-group" data-act="chat-group" aria-label="选择群">
              <option value="">所有群</option>
              ${groups.map((g) => `<option value="${esc(g.id)}" ${info.group_id === g.id ? "selected" : ""}>${esc(gname(g))}</option>`).join("")}
            </select>`
          : ""
      }
    </div>
    <div class="chat-bar-r">
      ${
        C
          ? `<button type="button" class="chat-ctx-btn ${ring.tone}" data-act="chat-ctx-toggle" aria-expanded="${!!state.chatCtxOpen}" title="上下文余量与主模型状态">
              <svg class="ctx-ring" viewBox="0 0 20 20" aria-hidden="true">
                <circle class="ctx-track" cx="10" cy="10" r="7" />
                <circle class="ctx-fill" cx="10" cy="10" r="7" stroke-dasharray="${c}" stroke-dashoffset="${off}" />
              </svg>
              <span class="chat-ctx-model">${esc(ring.modelLabel)}</span>
              <span class="chat-ctx-pct">${ring.pct}%</span>
            </button>`
          : ""
      }
      ${
        C && C.running
          ? `<button type="button" class="chat-send is-stop" data-act="chat-stop" aria-label="停止生成" title="停止本轮回复"><span class="stop-sq"></span></button>`
          : `<button class="chat-send" type="submit" aria-label="发送" title="发送（Ctrl+Enter）" ${!C ? "disabled" : ""}>${SVG.send || "↑"}</button>`
      }
    </div>`;
}

export function composerHTML(C) {
  return `<form id="chat-form" class="chat-composer" autocomplete="off">
    <div id="chat-pop-host" class="chat-pop-host">${composerPopHTML(C)}</div>
    <div id="chat-skill-host" class="chat-skill-host">${skillPillHTML()}</div>
    <textarea id="chat-input" rows="1" placeholder="输入消息，输入 / 调用技能或指令，输入 @ 引用事项…"></textarea>
    <div id="chat-bar" class="chat-bar">${composerBarHTML(C)}</div>
  </form>`;
}

export function syncComposerPop() {
  const host = $("chat-pop-host");
  if (host) host.innerHTML = composerPopHTML(state.chat);
}

export function syncComposerBar() {
  const sh = $("chat-skill-host");
  if (sh) sh.innerHTML = skillPillHTML();
  const bar = $("chat-bar");
  if (bar) bar.innerHTML = composerBarHTML(state.chat);
  syncComposerPop();
}

export function closeChatFloating() {
  let changed = false;
  if (state.chatMenu) {
    state.chatMenu = null;
    changed = true;
  }
  if (state.chatCtxOpen) {
    state.chatCtxOpen = false;
    changed = true;
  }
  if (changed) syncComposerBar();
  return changed;
}

function resizeChatInput(el) {
  if (!el || !el.style) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight || 44, 220)}px`;
}

function applyMenuItem(idx) {
  const menu = state.chatMenu;
  const items = currentMenuItems();
  const it = items[idx];
  const input = $("chat-input");
  if (!menu || !it || !input) return;
  const val = String(input.value || "");
  const before = val.slice(0, menu.start ?? 0);
  const after = val.slice(menu.end ?? val.length);
  state.chatMenu = null;
  if (menu.kind === "slash") {
    if (it.kind === "skill") {
      state.chatSkill = { name: it.name, scope: it.scope, description: it.description };
      input.value = (before + after).replace(/^\s+/, "");
      resizeChatInput(input);
      syncComposerBar();
      input.focus();
      return;
    }
    if (it.cmd === "compact") {
      const next = `${it.key} `;
      input.value = next + after.replace(/^\s+/, "");
      const caret = next.length;
      input.setSelectionRange?.(caret, caret);
      resizeChatInput(input);
      syncComposerPop();
      input.focus();
      return;
    }
    input.value = (before + after).trim();
    resizeChatInput(input);
    syncComposerPop();
    void runSlashCommand(it.cmd, "");
    return;
  }
  const token = `@[${it.label || it.id} ${it.title || ""}] `;
  const prefix = before && !/\s$/.test(before) ? `${before} ` : before;
  input.value = prefix + token + after.replace(/^\s+/, "");
  const caret = (prefix + token).length;
  input.setSelectionRange?.(caret, caret);
  resizeChatInput(input);
  syncComposerPop();
  input.focus();
}

export function onChatKeydown(e) {
  if (!e.target || e.target.id !== "chat-input") return false;
  if (state.chatMenu && !e.isComposing) {
    const items = currentMenuItems();
    if (e.key === "ArrowDown" && items.length) {
      e.preventDefault();
      state.chatMenu.idx = ((state.chatMenu.idx || 0) + 1) % items.length;
      syncComposerPop();
      return true;
    }
    if (e.key === "ArrowUp" && items.length) {
      e.preventDefault();
      state.chatMenu.idx = ((state.chatMenu.idx || 0) - 1 + items.length) % items.length;
      syncComposerPop();
      return true;
    }
    if ((e.key === "Enter" || e.key === "Tab") && !e.ctrlKey && !e.metaKey && items.length) {
      e.preventDefault();
      applyMenuItem(Math.min(state.chatMenu.idx || 0, items.length - 1));
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation?.();
      state.chatMenu = null;
      syncComposerPop();
      return true;
    }
  }
  if (e.key === "Backspace" && state.chatSkill && e.target.selectionStart === 0 && e.target.selectionEnd === 0) {
    state.chatSkill = null;
    syncComposerBar();
    return true;
  }
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) {
    e.preventDefault();
    $("chat-form")?.requestSubmit();
    return true;
  }
  return false;
}

export function onChatInput(e) {
  const el = e.target;
  if (!el || el.id !== "chat-input") return false;
  resizeChatInput(el);
  if (e.isComposing) return true;
  const val = String(el.value || "");
  const pos = typeof el.selectionStart === "number" ? el.selectionStart : val.length;
  const head = val.slice(0, pos);
  const gid = (state.chat && state.chat.info && state.chat.info.group_id) || "";
  const slashMatch = head.match(/^\/([^\s]*)$/);
  if (slashMatch) {
    state.chatCtxOpen = false;
    state.chatMenu = { kind: "slash", q: slashMatch[1], idx: 0, start: 0, end: pos };
    void ensureChatCatalog(gid);
    syncComposerPop();
    return true;
  }
  const refMatch = head.match(/(?:^|\s)@([^\s@\[\]]{0,24})$/);
  if (refMatch) {
    const atPos = head.lastIndexOf("@");
    state.chatCtxOpen = false;
    state.chatMenu = { kind: "ref", q: refMatch[1], idx: 0, start: atPos, end: pos };
    void ensureChatCatalog(gid);
    syncComposerPop();
    return true;
  }
  if (state.chatMenu) {
    state.chatMenu = null;
    syncComposerPop();
  }
  return true;
}

async function runChatCompact(focus = "", btnEl = null) {
  const C = state.chat;
  if (!C) return;
  if (C.running) {
    toast("请在回复完成后再压缩");
    return;
  }
  const orig = btnEl ? btnEl.textContent : "";
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "正在压缩…";
  }
  try {
    const body = String(focus || "").trim() ? { focus: String(focus).trim() } : {};
    await api("POST", `/api/chat/${encodeURIComponent(C.id)}/compact`, body);
    state.chatCtxOpen = false;
    await loadChat(C.id, false);
    paintChat(true);
    toast("已压缩");
  } catch (err) {
    toast(err.status === 409 ? "请在回复完成后再压缩" : err.message, true);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = orig;
    }
  }
}

async function createFocusedChat(groupId) {
  const c = await api("POST", "/api/chat", { group_id: groupId || "" });
  closeSheet();
  await enterChat(c.id);
  setTimeout(() => $("chat-input")?.focus(), 50);
}

async function runSlashCommand(cmd, rest) {
  const C = state.chat;
  const gid = (C && C.info && C.info.group_id) || state.g || "";
  if (cmd === "compact") {
    await runChatCompact(rest);
    return;
  }
  if (cmd === "new") {
    try {
      await createFocusedChat(gid);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (cmd === "clear") {
    try {
      if (C && C.id) await api("PATCH", `/api/chat/${encodeURIComponent(C.id)}`, { archived: true });
      await createFocusedChat(gid);
      toast("已归档旧对话并开启新对话");
    } catch (err) {
      toast(err.message, true);
    }
  }
}

export async function submitChatForm(f) {
  const input = $("chat-input");
  const C = state.chat;
  if (!input || !C) return;
  const raw = input.value.trim();
  const catSkills = (state.chatCatalog && state.chatCatalog.skills) || [];
  const parsed = parseTypedSlash(raw, catSkills);
  if (parsed.kind === "command") {
    input.value = "";
    resizeChatInput(input);
    state.chatMenu = null;
    syncComposerPop();
    await runSlashCommand(parsed.cmd, parsed.rest);
    return;
  }
  const activeSkill = state.chatSkill || parsed.skill;
  const text = parsed.kind === "skill" ? parsed.rest : raw;
  if (!text && !activeSkill) return;
  if (C.running) {
    toast("上一条消息仍在处理中", true);
    return;
  }
  const btn = f && f.querySelector ? f.querySelector('[type="submit"]') : null;
  if (btn) btn.disabled = true;
  try {
    const payload = { text };
    if (activeSkill && activeSkill.name) payload.skill = activeSkill.name;
    await api("POST", `/api/chat/${encodeURIComponent(C.id)}/messages`, payload);
    input.value = "";
    if (input.style) input.style.height = "";
    state.chatSkill = null;
    state.chatMenu = null;
    C.running = true;
    await loadChat(C.id, true);
    if (state.chat) state.chat.running = true;
    paintChat("smooth");
    chatPoll();
  } catch (ex) {
    if (btn) btn.disabled = false;
    toast(ex.message, true);
  }
}

export async function enterChatWithSkill(skillObj) {
  if (!admin() || !skillObj) return;
  state.chatSkill = {
    name: skillObj.name,
    scope: skillObj.scope || "global",
    description: skillObj.description || "",
  };
  state.chatMenu = null;
  state.chatCtxOpen = false;
  await enterChat();
  syncComposerBar();
  setTimeout(() => $("chat-input")?.focus(), 40);
}

export async function enterChatWithDraft(groupId, draftText) {
  if (!admin()) return;
  closeSheet();
  await loadChats();
  const gid = groupId && (state.groups || []).some((g) => g.id === groupId) ? groupId : "";
  let target = (state.chats || []).find((c) => !c.archived && (gid ? c.group_id === gid : true));
  if (!target && gid) {
    try {
      target = await api("POST", "/api/chat", { group_id: gid });
      state.chats = [target].concat(state.chats || []);
    } catch (_) {}
  }
  await enterChat(target && target.id);
  const input = $("chat-input");
  if (input) {
    input.value = draftText || "";
    resizeChatInput(input);
    input.focus();
    const len = input.value.length;
    input.setSelectionRange?.(len, len);
  }
}

export async function actChat(a, el) {
  if (!a || !a.startsWith("chat")) return false;
  if (a === "chat") {
    await enterChat();
    return true;
  }
  switch (a) {
    case "chat-open":
      closeSheet();
      await enterChat(Number(el.dataset.id));
      return true;
    case "chat-list":
      await loadChats();
      openSheet("chats");
      return true;
    case "chat-new":
      try {
        await createFocusedChat((state.chat && state.chat.info && state.chat.info.group_id) || state.g || "");
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    case "chat-suggest": {
      const input = $("chat-input");
      if (input) {
        input.value = el.dataset.t || "";
        resizeChatInput(input);
        input.focus();
      }
      return true;
    }
    case "chat-summary":
      state.chatOpen["s" + el.dataset.id] = !state.chatOpen["s" + el.dataset.id];
      paintChat(false);
      return true;
    case "chat-tool":
      state.chatOpen[el.dataset.id] = !state.chatOpen[el.dataset.id];
      paintChat(false);
      reveal(`.cm-tool-h[data-id="${el.dataset.id}"]`);
      return true;
    case "chat-tool-group":
      state.chatOpen["g" + el.dataset.gid] = !state.chatOpen["g" + el.dataset.gid];
      paintChat(false);
      return true;
    case "chat-compact":
      await runChatCompact("", el);
      return true;
    case "chat-stop": {
      const C = state.chat;
      if (!C) return true;
      el.disabled = true;
      try {
        await api("POST", `/api/chat/${encodeURIComponent(C.id)}/stop`, {});
        await loadChat(C.id, false);
        paintChat(true);
        toast("已停止本轮回复");
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "chat-retry": {
      const C = state.chat;
      if (!C) return true;
      el.disabled = true;
      try {
        await api("POST", `/api/chat/${encodeURIComponent(C.id)}/retry`, {});
        C.running = true;
        await loadChat(C.id, false);
        if (state.chat) state.chat.running = true;
        paintChat("smooth");
        chatPoll();
      } catch (err) {
        el.disabled = false;
        toast(err.message, true);
      }
      return true;
    }
    case "chat-copy-msg": {
      const mid = Number(el.dataset.id);
      const msg = ((state.chat && state.chat.messages) || []).find((m) => m.id === mid);
      if (!msg) return true;
      try {
        await navigator.clipboard.writeText(String(msg.content || ""));
        flashCopied(el, "已复制");
      } catch (_) {
        toast("复制失败，请手动复制", true);
      }
      return true;
    }
    case "chat-copy-code": {
      const wrap = el.closest && el.closest(".md-code-wrap");
      const pre = wrap && wrap.querySelector ? wrap.querySelector("pre") : null;
      const code = pre ? pre.textContent : el.dataset.code || "";
      try {
        await navigator.clipboard.writeText(String(code || ""));
        flashCopied(el, "已复制");
      } catch (_) {
        toast("复制失败，请手动复制", true);
      }
      return true;
    }
    case "chat-rename": {
      const cid = Number(el.dataset.id);
      const cur = ((state.chats || []).find((c) => c.id === cid) || {}).title || "";
      const next = typeof prompt === "function" ? prompt("输入新的对话标题", cur) : "";
      if (!next || !next.trim() || next.trim() === cur) return true;
      try {
        await api("PATCH", `/api/chat/${encodeURIComponent(cid)}`, { title: next.trim() });
        if (state.chat && state.chat.id === cid && state.chat.info) state.chat.info.title = next.trim();
        await loadChats();
        renderSide();
        if (state.sheet === "chats") openSheet("chats");
        toast("标题已更新");
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    }
    case "chat-archive": {
      const cid = Number(el.dataset.id);
      if (!confirm("归档该对话？归档后不再显示在列表中。")) return true;
      try {
        await api("PATCH", `/api/chat/${encodeURIComponent(cid)}`, { archived: true });
        await loadChats();
        if (state.chatId === cid) {
          const next = state.chats[0] && state.chats[0].id;
          state.chatId = null;
          state.chat = null;
          await enterChat(next || null);
        } else {
          renderSide();
          if (state.sheet === "chats") openSheet("chats");
        }
        toast("对话已归档");
      } catch (err) {
        toast(err.message, true);
      }
      return true;
    }
    case "chat-slash-btn": {
      const gid = (state.chat && state.chat.info && state.chat.info.group_id) || "";
      state.chatCtxOpen = false;
      if (state.chatMenu && state.chatMenu.kind === "slash") {
        state.chatMenu = null;
      } else {
        const input = $("chat-input");
        const val = input ? String(input.value || "") : "";
        state.chatMenu = { kind: "slash", q: "", idx: 0, start: 0, end: val.startsWith("/") ? val.split(/\s/)[0].length : 0 };
        void ensureChatCatalog(gid);
      }
      syncComposerPop();
      $("chat-input")?.focus();
      return true;
    }
    case "chat-ref-btn": {
      const gid = (state.chat && state.chat.info && state.chat.info.group_id) || "";
      state.chatCtxOpen = false;
      if (state.chatMenu && state.chatMenu.kind === "ref") {
        state.chatMenu = null;
      } else {
        const input = $("chat-input");
        const pos = input ? (typeof input.selectionStart === "number" ? input.selectionStart : input.value.length) : 0;
        state.chatMenu = { kind: "ref", q: "", idx: 0, start: pos, end: pos };
        void ensureChatCatalog(gid);
      }
      syncComposerPop();
      $("chat-input")?.focus();
      return true;
    }
    case "chat-menu-pick":
      applyMenuItem(Number(el.dataset.idx) || 0);
      return true;
    case "chat-skill-clear":
      state.chatSkill = null;
      syncComposerBar();
      $("chat-input")?.focus();
      return true;
    case "chat-ctx-toggle":
      state.chatMenu = null;
      state.chatCtxOpen = !state.chatCtxOpen;
      syncComposerBar();
      return true;
    case "chat-ask-ref": {
      const gid = el.dataset.g || state.g || "";
      const ref = el.dataset.ref || "";
      const title = el.dataset.title || "";
      const token = ref ? `@[${ref}${title ? ` ${title}` : ""}] ` : "";
      await enterChatWithDraft(gid, token);
      return true;
    }
  }
  return false;
}
