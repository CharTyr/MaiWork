// MaiWork 网页 · 入口：点击 / 提交等全局事件，然后启动。
import { $, desktop, gadmin, state, ui } from "./state.js";
import { calm, toast } from "./util.js";
import { api, grp, gview } from "./api.js";
import { loadTask } from "./detail.js";
import { loadGA } from "./settings/index.js";
import { confirmExecTools, execConfirmNeeded, loadExt, parseMcpConfig, readHeaders, readRoles } from "./settings/ext.js";
import { loadRules, readRules } from "./settings/rules.js";
import { loadUsage } from "./settings/usage.js";
import { loadIdentity } from "./settings/identity.js";
import { loadAgents } from "./settings/agents.js";
import { loadLogSummary } from "./settings/logs.js";
import { loadModels } from "./settings/models.js";
import { closeSheet, repaintSheet } from "./sheet.js";
import { chatPoll, enterChat, loadChat, paintChat } from "./chat.js";
import { render, renderRail, renderSide, renderView } from "./render.js";
import { applyHash, loadGroups, loadSettings, loadView, parseHash, reboot, syncHash } from "./router.js";
import { act } from "./actions.js";
import { onb, onbAct } from "./onboarding.js";
import { RATE_KEY, clientId, myRates } from "./pages/news.js";
import "./events.js"; // 只注册输入类事件，没有导出

const NAV_SPRINGS = {
  news: [
    { transform: "translateY(0) scale(1) rotate(0deg)" },
    { transform: "translateY(-2.4px) scale(1.08) rotate(3.5deg)", offset: 0.38 },
    { transform: "translateY(0.4px) scale(0.99) rotate(-1deg)", offset: 0.74 },
    { transform: "translateY(0) scale(1) rotate(0deg)" },
  ],
  ideas: [
    { transform: "translateY(0) scale(1) rotate(0deg)" },
    { transform: "translateY(-3px) scale(1.1) rotate(6.5deg)", offset: 0.34 },
    { transform: "translateY(-0.6px) scale(1.02) rotate(-3.5deg)", offset: 0.68 },
    { transform: "translateY(0) scale(1) rotate(0deg)" },
  ],
  tasks: [
    { transform: "translateY(0) scale(1)" },
    { transform: "translateY(-2.8px) scale(1.09)", offset: 0.38 },
    { transform: "translateY(0.6px) scale(0.985)", offset: 0.74 },
    { transform: "translateY(0) scale(1)" },
  ],
  group: [
    { transform: "translateY(0) scale(1) rotate(0deg)" },
    { transform: "translateY(-2px) scale(1.09) rotate(5.5deg)", offset: 0.38 },
    { transform: "translateY(0.3px) scale(0.99) rotate(-1.8deg)", offset: 0.74 },
    { transform: "translateY(0) scale(1) rotate(0deg)" },
  ],
  default: [
    { transform: "translateY(0) scale(1)" },
    { transform: "translateY(-2.2px) scale(1.08)", offset: 0.38 },
    { transform: "translateY(0.4px) scale(0.99)", offset: 0.74 },
    { transform: "translateY(0) scale(1)" },
  ],
};

function pulseNav(el) {
  if (!el || !el.matches(".tab, .r-item, .round-btn") || calm()) return;
  const icon = el.querySelector("svg, .gav");
  if (!icon || !icon.animate) return;
  const frames = NAV_SPRINGS[el.dataset.tab] || NAV_SPRINGS.default;
  try {
    icon.animate(frames, { duration: 480, easing: "cubic-bezier(0.22, 1, 0.36, 1)", composite: "add" });
  } catch (_) {
    icon.animate(frames, { duration: 480, easing: "cubic-bezier(0.22, 1, 0.36, 1)" });
  }
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  if (el.dataset.act.startsWith("onb-")) return void onbAct(el);
  act(el, e);
  pulseNav(el);
});

// 输入先记草稿；拼音组合中暂停所有搜索重绘，提交中文后再防抖。
function newsSearchInput(e) {
  if (e.target?.id !== "news-q") return;
  clearTimeout(state._newsQTimer);
  state.newsQ = e.target.value || "";
  if (e.isComposing || state.newsComposing) return;
  const group = state.g;
  state._newsQTimer = setTimeout(() => {
    if (state.g === group && !state.newsComposing && !state.page && state.tab === "news") renderView();
  }, 280);
}
document.addEventListener("compositionstart", (e) => {
  if (e.target?.id !== "news-q") return;
  state.newsComposing = true;
  clearTimeout(state._newsQTimer);
});
document.addEventListener("compositionend", (e) => {
  if (e.target?.id !== "news-q") return;
  state.newsComposing = false;
  newsSearchInput(e);
});
document.addEventListener("input", newsSearchInput);

// .ntag[data-act="news-topic"] 键盘可达（role=button, tabindex=0）
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target && e.target.matches && e.target.matches('.ntag[data-act="news-topic"]')) {
    e.preventDefault();
    e.target.click();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (onb.open) return;
    if (state.sheet) closeSheet();
    else if (state.detail) {
      state.detail = null;
      ui.sideFlash = true;
      syncHash();
      renderView();
      renderSide();
    }
  }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches(".item.tap")) {
    e.preventDefault();
    e.target.click();
  }
});
$("scrim").addEventListener("click", closeSheet);

document.addEventListener("submit", async (e) => {
  const f = e.target;
  e.preventDefault();
  const btn = f.querySelector('[type="submit"]');
  if (f.id === "login") {
    const pw = $("pw").value;
    if (!pw) return;
    btn.disabled = true;
    try {
      const r = await api("POST", "/api/login", { password: pw });
      closeSheet();
      await reboot();
      toast(r && r.role === "group_admin" ? "已登录，可管理本群" : "已登录，可查看所有群");
    } catch (err) {
      state.loginError = err.status === 429 ? "尝试次数过多，请 10 分钟后重试。" : err.status === 401 ? "密码错误。" : err.message;
      const p = f.querySelector(".err");
      p.textContent = state.loginError;
      p.hidden = false;
      $("pw").select();
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (f.id === "chat-form") {
    const input = $("chat-input");
    const text = input.value.trim();
    if (!text || !state.chat) return;
    if (state.chat.running) return toast("上一条消息仍在处理中", true);
    btn.disabled = true;
    try {
      await api("POST", `/api/chat/${encodeURIComponent(state.chat.id)}/messages`, { text });
      input.value = "";
      input.style.height = "";
      state.chat.running = true;
      await loadChat(state.chat.id, true);
      state.chat.running = true;
      paintChat("smooth");
      chatPoll();
    } catch (ex) {
      btn.disabled = false;
      toast(ex.message, true);
    }
    return;
  }
  if (f.id === "rules-form") {
    const errEl = $("r-err");
    let body;
    try {
      body = readRules();
    } catch (ex) {
      errEl.textContent = ex.message;
      errEl.hidden = false;
      return;
    }
    if (!Object.keys(body).length) {
      toast("没有修改");
      return;
    }
    btn.disabled = true;
    try {
      state.rules = await api("PUT", "/api/settings/config", body);
      await loadSettings();
      if (Object.hasOwn(body, "groups.serve")) {
        const oldGroup = state.g;
        await loadGroups();
        applyHash();
        if (state.g !== oldGroup) state.view = null;
        render();
      }
      repaintSheet();
      toast((state.rules.reload_pending || []).length ? "已保存，部分设置需重载后生效" : "已保存并生效");
    } catch (ex) {
      errEl.textContent = ex.message;
      errEl.hidden = false;
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (f.classList.contains("rss-add")) {
    const input = f.querySelector("input");
    const url = input.value.trim();
    if (!/^https?:\/\/\S+$/.test(url)) return toast("地址须以 http:// 或 https:// 开头", true);
    btn.disabled = true;
    btn.textContent = "正在获取…";
    try {
      const r = await api("POST", `/api/groups/${encodeURIComponent(f.dataset.g)}/rss`, { url });
      if (state.rssAuto) delete state.rssAuto[f.dataset.g];
      await loadSettings();
      repaintSheet();
      toast(`已添加 ${r.title || url}${r.items_count != null ? `，共 ${r.items_count} 篇` : ""}`);
    } catch (ex) {
      btn.disabled = false;
      btn.textContent = "添加";
      toast(ex.message, true);
    }
    return;
  }
  if (f.classList.contains("id-form")) {
    const kind = f.dataset.kind;
    const text = f.querySelector(".id-text").value;
    const limit = Number(f.querySelector(".id-text").dataset.limit) || 16384;
    if (new Blob([text]).size > limit) return toast(`内容过长，最多 ${limit} 字节`, true);
    btn.disabled = true;
    try {
      await api("PUT", `/api/identity/${kind}`, { text });
      await loadIdentity();
      repaintSheet();
      toast("已保存");
    } catch (ex) {
      toast(ex.message, true);
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (f.id === "mcp-paste-form") {
    const errEl = $("mp-err");
    let list;
    try {
      list = parseMcpConfig($("mp-text").value);
    } catch (ex) {
      errEl.textContent = ex.message;
      errEl.hidden = false;
      return;
    }
    const ok = list.filter((x) => !x.skipped);
    const skipped = list.filter((x) => x.skipped);
    if (!ok.length) {
      errEl.textContent = skipped.map((x) => `${x.name}：${x.skipped}`).join("；");
      errEl.hidden = false;
      return;
    }
    if (ok.length === 1) {
      state.extEdit = { kind: "mcp", name: null, prefill: { ...ok[0], roles: ["worker"], enabled: true, timeout_s: 30 } };
      repaintSheet();
      if (skipped.length) toast(`已跳过：${skipped.map((x) => `${x.name}（${x.skipped}）`).join("、")}`, true);
      return;
    }
    btn.disabled = true;
    const done = [];
    const failed = [];
    for (const x of ok) {
      try {
        const r = await api("POST", "/api/extensions/mcp", { name: x.name, url: x.url, headers: x.headers, tools: [], roles: ["worker"], enabled: true, timeout_s: 30 });
        done.push(`${x.name}${r.ok ? "" : "（已添加，但连接失败）"}`);
      } catch (ex) {
        failed.push(`${x.name}：${ex.message}`);
      }
    }
    btn.disabled = false;
    state.extEdit = null;
    await loadExt();
    repaintSheet();
    toast([done.length ? `已添加 ${done.join("、")}` : "", failed.length ? `添加失败 ${failed.join("；")}` : "", skipped.length ? `已跳过 ${skipped.map((x) => x.name).join("、")}` : ""].filter(Boolean).join("。"), !!failed.length);
    return;
  }
  if (f.id === "mcp-form" || f.id === "skill-form") {
    const isMcp = f.id === "mcp-form";
    const errEl = $(isMcp ? "x-err" : "k-err");
    const name = (f.dataset.name || ($(isMcp ? "x-name" : "k-name") || {}).value || "").trim();
    const fail = (t) => {
      errEl.textContent = t;
      errEl.hidden = false;
    };
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) return fail("名称只能用字母、数字、_ 和 -。");
    let body;
    if (isMcp) {
      const url = $("x-url").value.trim();
      if (!/^https:\/\/\S+$/.test(url)) return fail("地址须以 https:// 开头。");
      const roles = readRoles("x");
      if (!roles.length) return fail("请选择适用对象。");
      const { headers, remove } = readHeaders();
      body = {
        name,
        url,
        headers,
        remove_headers: remove,
        tools: $("x-tools").value.split(/[,，\s]+/).map((t) => t.trim()).filter(Boolean),
        roles,
        enabled: $("x-enabled").checked,
        timeout_s: Number($("x-timeout").value) || 30,
      };
    } else {
      const roles = readRoles("k");
      if (!roles.length) return fail("请选择适用对象。");
      const text = $("k-body").value;
      if (!text.trim()) return fail("内容不能为空。");
      if (new Blob([text]).size > 40000) return fail("内容过长，最多 40KB。");
      body = { name, description: $("k-desc").value.trim(), roles, body: text };
      // 要用的工具（allowed-tools）：表单里有清单才提交；没有就不带，服务端保持原样
      if ($("k-tools")) body.allowed_tools = [...document.querySelectorAll("#k-tools input:checked")].map((x) => x.value);
    }
    btn.disabled = true;
    try {
      const kindPath = isMcp ? "mcp" : "skills";
      const send = (b) => (f.dataset.name
        ? api("PUT", `/api/extensions/${kindPath}/${encodeURIComponent(f.dataset.name)}`, b)
        : api("POST", `/api/extensions/${kindPath}`, b));
      try {
        await send(body);
      } catch (ex) {
        // 带执行类工具：服务端要求确认，确认后带 confirm_exec 重试
        if (isMcp || !execConfirmNeeded(ex) || !confirmExecTools(ex)) throw ex;
        await send({ ...body, confirm_exec: true });
      }
      state.extEdit = null;
      await Promise.all([loadExt(), loadSettings()]);
      repaintSheet();
      toast("已保存，下次运行时生效");
    } catch (ex) {
      fail(ex.message);
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (f.id === "edit" && (state.editing || {}).kind === "ga") {
    const ed = state.editing;
    const err = $("ed-err");
    const pw = $("ga-pw").value;
    if (pw && pw.length < 8) {
      err.textContent = "密码至少 8 位。";
      err.hidden = false;
      return;
    }
    btn.disabled = true;
    try {
      state.ga[ed.gid] = await api("PUT", `/api/groups/${encodeURIComponent(ed.gid)}/group-admin`, { password: pw || undefined });
      closeSheet();
      repaintSheet();
      toast("已保存");
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (f.id === "edit" && (state.editing || {}).kind === "rate") {
    const ed = state.editing;
    const err = $("ed-err");
    const note = $("ed-text").value.trim().slice(0, 60);
    const reasons = ed.reasons || [];
    if (!reasons.length && !note) {
      err.textContent = "请选择问题或填写补充说明。";
      err.hidden = false;
      return;
    }
    btn.disabled = true;
    try {
      const r = await api("POST", `/api/news/${encodeURIComponent(ed.id)}/rate`, { client: clientId(), reasons, note });
      const all = myRates();
      all[ed.id] = (r && r.mine) || { reasons, note };
      localStorage.setItem(RATE_KEY, JSON.stringify(all));
      closeSheet();
      await loadView(true);
      renderView();
      toast("已提交，下次搜集时参考");
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
    }
    return;
  }
  if (f.id === "edit") {
    const ed = state.editing || {};
    const text = $("ed-text").value.trim();
    const err = $("ed-err");
    if (!text) {
      err.textContent = ed.kind === "focus" ? "请填写 QQ 号。" : "内容不能为空。";
      err.hidden = false;
      return;
    }
    btn.disabled = true;
    try {
      const g = grp();
      if (ed.kind === "focus") {
        if (!/^\d{5,12}$/.test(text)) throw new Error("QQ 号须为 5–12 位数字。");
        await api("POST", `/api/groups/${encodeURIComponent(g.id)}/focus`, { user_id: text, action: "add" });
      } else if (ed.id) {
        await api("PATCH", `/api/profile/${ed.id}`, { text });
      } else {
        await api("POST", `/api/groups/${encodeURIComponent(g.id)}/profile`, { category: ed.cat, text });
      }
      closeSheet();
      await loadView(true);
      renderView();
      toast(ed.kind === "focus" ? "已添加" : "已保存并锁定");
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }
});

desktop.addEventListener("change", () => {
  closeSheet();
  render();
});
window.addEventListener("hashchange", () => {
  const h = parseHash();
  if (!gadmin() && h.ref !== state.ref) return reboot();
  applyHash();
  if (state.page === "chat") {
    enterChat(state.chatId);
    return;
  }
  if (state.page === "settings") {
    ui.flash = true;
    render();
    Promise.all([
      state.settings ? null : loadSettings(),
      state.setSub === "extensions" && !state.ext ? loadExt() : null,
      state.setSub === "rules" ? loadRules() : null,
      state.setSub === "usage" ? loadUsage() : null,
      ["memory", "agents"].includes(state.setSub) ? loadIdentity() : null,
      state.setSub === "models" ? loadModels() : null,
      state.setSub === "agents" && !state.agents ? loadAgents() : null,
      state.setSub === "usage" ? loadLogSummary() : null,
      state.setSub === "links" ? loadGA() : null,
    ]).then(() => render());
    return;
  }
  ui.flash = true;
  const same = grp() && gview();
  if (!same) state.view = null;
  render();
  if (!same) loadView().then(() => ((ui.flash = true), render()));
  if (state.detail && state.detail.type === "task") loadTask(state.detail.id);
});

reboot();
