// 目标详情中的持久管理员对话。缓存按群分开，重画详情不丢草稿。
import { admin, state } from "./state.js";
import { api } from "./api.js";
import { esc } from "./util.js";

const chats = new Map();
const key = (id, group) => `${group}:${id}`;
function entry(id, group = state.g) {
  const k = key(id, group);
  if (!chats.has(k)) chats.set(k, {messages: [], pending: [], draft: "", loaded: false, loading: false, sending: false, running: false, error: "", checked: 0});
  return chats.get(k);
}
function visible(id, group) {
  return admin() && state.g === group && state.detail?.type === "goal" && state.detail.id === id;
}
function stream(c, id) {
  const messages = c.messages.map(m => {
    if (!m.content) return "";
    return `<div class="chat-msg ${m.role === "user" ? "user" : "assistant"}"><div class="chat-bubble">${esc(m.content)}</div></div>`;
  }).join("");
  const pending = c.pending.filter(p => p.status === "pending").map(p => `<div class="cm-pending">
    <div class="cm-pending-t">${esc(p.summary || p.tool)}</div>
    <details><summary>查看动作详情</summary><pre class="lm-text">${esc(JSON.stringify(p.args || {}, null, 2))}</pre></details>
    <div class="actions"><button class="btn primary small" data-act="goal-chat-confirm" data-id="${esc(id)}" data-pid="${esc(p.id)}" data-ok="1" ${c.sending ? "disabled" : ""}>确认</button>
    <button class="btn small" data-act="goal-chat-confirm" data-id="${esc(id)}" data-pid="${esc(p.id)}" data-ok="0" ${c.sending ? "disabled" : ""}>拒绝</button></div></div>`).join("");
  return (messages || `<div class="chat-placeholder">${c.loaded ? "可以询问受阻原因，或调整网址、检查频率和完成标准。" : "正在读取对话…"}</div>`) + pending +
    (c.running ? `<div class="chat-placeholder" role="status">正在处理，回复会自动显示…</div>` : "") +
    (c.error ? `<div class="chat-placeholder" role="alert">${esc(c.error)}</div>` : "");
}
function paint(id, group) {
  if (!visible(id, group)) return;
  const box = document.getElementById("goal-chat-panel");
  if (!box || box.dataset.goalId !== id || box.dataset.groupId !== String(group)) return;
  const c = entry(id, group);
  const messages = box.querySelector(".goal-chat-stream");
  if (messages) {
    const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
    messages.innerHTML = stream(c, id);
    if (atBottom) messages.scrollTop = messages.scrollHeight;
  }
  const send = box.querySelector("[data-act='goal-chat-send']");
  if (send) send.disabled = c.sending || c.running;
}
export async function loadGoalChat(id, group = state.g) {
  if (!visible(id, group)) return;
  const c = entry(id, group);
  if (c.loading) return;
  c.loading = true;
  try {
    const result = await api("GET", `/api/goals/${encodeURIComponent(id)}/chat`);
    if (!visible(id, group)) return;
    const changed = c.loaded && JSON.stringify([c.messages, c.pending, c.running]) !==
      JSON.stringify([result.messages || [], result.pending || [], !!result.running]);
    c.messages = result.messages || [];
    c.pending = result.pending || [];
    c.running = !!result.running;
    c.loaded = true;
    c.error = "";
    if (changed) {
      // 延迟加载避免为对话缓存引入初始化依赖；沿用既有详情刷新路径。
      const [{loadView, renderTopBits}, {refreshDetail}, {renderView}] = await Promise.all([
        import("./router.js"), import("./detail.js"), import("./render.js"),
      ]);
      if (!visible(id, group)) return;
      await loadView(true);
      if (visible(id, group)) { renderTopBits(); renderView(); refreshDetail(); }
    }
  } catch (error) {
    if (visible(id, group)) c.error = error.message || "读取对话失败";
  } finally {
    c.loading = false;
    c.checked = Date.now();
    paint(id, group);
  }
}
export function preserveGoalChatInput(render) {
  const focused = document.activeElement;
  const id = focused?.dataset?.goalId, group = state.g;
  if (!id || !visible(id, group)) return render();
  // 输入法候选尚未提交时不替换输入框，避免打断中文输入。
  if (focused.dataset.composing === "1") return;
  const selection = [focused.selectionStart, focused.selectionEnd];
  render();
  if (!visible(id, group)) return;
  const input = document.getElementById("goal-chat-panel")?.querySelector(".chat-input");
  if (input) {
    input.focus({preventScroll: true});
    input.setSelectionRange(...selection);
  }
}
export function goalChatHTML(goal) {
  if (!admin()) return "";
  const id = goal.id, group = state.g, c = entry(id, group);
  if (!c.loaded && !c.loading) setTimeout(() => loadGoalChat(id, group), 0);
  return `<div class="dt-sec"><div class="dt-label">和 MaiWork 调整这个目标</div>
    <div class="goal-chat" id="goal-chat-panel" data-goal-id="${esc(id)}" data-group-id="${esc(group)}">
    <div class="chat-messages goal-chat-stream" aria-live="polite">${stream(c, id)}</div>
    <div class="chat-input-row"><input type="text" class="chat-input" data-goal-id="${esc(id)}" aria-label="目标调整消息" placeholder="输入调整要求…" value="${esc(c.draft)}" />
    <button class="btn small" data-act="goal-chat-send" data-id="${esc(id)}" ${c.sending || c.running ? "disabled" : ""}>发送</button></div></div></div>`;
}
async function send(id) {
  const group = state.g;
  if (!visible(id, group)) return;
  const c = entry(id, group), text = c.draft.trim();
  if (!text || c.sending || c.running) return;
  c.sending = true;
  c.error = "";
  paint(id, group);
  try {
    await api("POST", `/api/goals/${encodeURIComponent(id)}/chat`, {text});
    if (c.draft.trim() === text) c.draft = "";
    if (visible(id, group)) {
      const input = document.getElementById("goal-chat-panel")?.querySelector(".chat-input");
      if (input && input.value.trim() === text) input.value = "";
    }
    await loadGoalChat(id, group);
  } catch (error) {
    c.error = error.message || "发送失败，请重试";
  } finally {
    c.sending = false;
    paint(id, group);
  }
}
for (const eventName of ["compositionstart", "compositionend"]) {
  document.addEventListener(eventName, event => {
    const input = event.target?.closest?.(".chat-input[data-goal-id]");
    if (input) input.dataset.composing = eventName === "compositionstart" ? "1" : "0";
  });
}
document.addEventListener("input", event => {
  const input = event.target?.closest?.(".chat-input[data-goal-id]");
  if (input && visible(input.dataset.goalId, state.g)) entry(input.dataset.goalId).draft = input.value;
});
document.addEventListener("click", async event => {
  const button = event.target?.closest?.("[data-act='goal-chat-send'], [data-act='goal-chat-confirm']");
  if (!button) return;
  const id = button.dataset.id, group = state.g;
  if (!visible(id, group)) return;
  if (button.dataset.act === "goal-chat-send") return send(id);
  const c = entry(id, group);
  if (c.sending || c.running) return;
  c.sending = true;
  paint(id, group);
  try {
    // 复用既有总管理员确认接口；小票归属由持久对话提供。
    await api("POST", `/api/chat/pending/${encodeURIComponent(button.dataset.pid)}`, {approve: button.dataset.ok === "1"});
    await loadGoalChat(id, group);
  } catch (error) {
    c.error = error.message || "确认失败，请重试";
  } finally {
    c.sending = false;
    paint(id, group);
  }
});
document.addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
  const input = event.target?.closest?.(".chat-input[data-goal-id]");
  if (!input) return;
  event.preventDefault();
  void send(input.dataset.goalId);
});
setInterval(() => {
  if (!admin()) { chats.clear(); return; }
  const d = state.detail;
  if (d?.type !== "goal" || !document.getElementById("goal-chat-panel")) return;
  const c = entry(d.id);
  if (Date.now() - c.checked > 1800 && !c.sending) void loadGoalChat(d.id);
}, 2000);
