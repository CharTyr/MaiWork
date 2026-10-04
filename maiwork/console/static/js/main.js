// MaiWork 网页 · 入口：点击 / 提交等全局事件，然后启动。
import { $, desktop, gadmin, state, ui } from "./state.js";
import { toast } from "./util.js";
import { api, grp, gview } from "./api.js";
import { loadTask } from "./detail.js";
import { loadGA } from "./settings/index.js";
import { loadExt, parseMcpConfig, readHeaders, readRoles } from "./settings/ext.js";
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

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  if (el.dataset.act.startsWith("onb-")) return void onbAct(el);
  act(el, e);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (onb.open) return;
    if (state.sheet) closeSheet();
    else if (state.detail) {
      state.detail = null;
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
      toast(r && r.role === "group_admin" ? "已进入群管理员，能管这个群" : "已进入管理员，能看到全部群");
    } catch (err) {
      state.loginError = err.status === 429 ? "错太多次了，过 10 分钟再试。" : err.status === 401 ? "密码不对，再试一次。" : err.message;
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
    if (state.chat.running) return toast("上一句还在处理，等一下", true);
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
      toast("没有改动");
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
      toast((state.rules.reload_pending || []).length ? "写进 config.toml 了；标「重载后生效」的要等插件重载" : "写进 config.toml 了，马上生效");
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
    if (!/^https?:\/\/\S+$/.test(url)) return toast("地址要以 http:// 或 https:// 开头", true);
    btn.disabled = true;
    btn.textContent = "在取…";
    try {
      const r = await api("POST", `/api/groups/${encodeURIComponent(f.dataset.g)}/rss`, { url });
      await loadSettings();
      repaintSheet();
      toast(`加上了：${r.title || url}${r.items_count != null ? `，现在有 ${r.items_count} 篇` : ""}`);
    } catch (ex) {
      btn.disabled = false;
      btn.textContent = "加上";
      toast(ex.message, true);
    }
    return;
  }
  if (f.classList.contains("id-form")) {
    const kind = f.dataset.kind;
    const text = f.querySelector(".id-text").value;
    const limit = Number(f.querySelector(".id-text").dataset.limit) || 16384;
    if (new Blob([text]).size > limit) return toast(`太长了，最多 ${limit} 字节`, true);
    btn.disabled = true;
    try {
      await api("PUT", `/api/identity/${kind}`, { text });
      await loadIdentity();
      repaintSheet();
      toast("保存好了");
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
      if (skipped.length) toast(`跳过了：${skipped.map((x) => `${x.name}（${x.skipped}）`).join("、")}`, true);
      return;
    }
    btn.disabled = true;
    const done = [];
    const failed = [];
    for (const x of ok) {
      try {
        const r = await api("POST", "/api/extensions/mcp", { name: x.name, url: x.url, headers: x.headers, tools: [], roles: ["worker"], enabled: true, timeout_s: 30 });
        done.push(`${x.name}${r.ok ? "" : "（加上了但没连上）"}`);
      } catch (ex) {
        failed.push(`${x.name}：${ex.message}`);
      }
    }
    btn.disabled = false;
    state.extEdit = null;
    await loadExt();
    repaintSheet();
    toast([done.length ? `加上了 ${done.join("、")}` : "", failed.length ? `没加上 ${failed.join("；")}` : "", skipped.length ? `跳过 ${skipped.map((x) => x.name).join("、")}` : ""].filter(Boolean).join("。"), !!failed.length);
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
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) return fail("名字只能用字母、数字、下划线、横线。");
    let body;
    if (isMcp) {
      const url = $("x-url").value.trim();
      if (!/^https:\/\/\S+$/.test(url)) return fail("地址要以 https:// 开头。");
      const roles = readRoles("x");
      if (!roles.length) return fail("至少选一个「给谁用」。");
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
      if (!roles.length) return fail("至少选一个「给谁用」。");
      const text = $("k-body").value;
      if (!text.trim()) return fail("内容不能是空的。");
      if (new Blob([text]).size > 40000) return fail("内容太长了，最多 40KB。");
      body = { name, description: $("k-desc").value.trim(), roles, body: text };
    }
    btn.disabled = true;
    try {
      const kindPath = isMcp ? "mcp" : "skills";
      if (f.dataset.name) await api("PUT", `/api/extensions/${kindPath}/${encodeURIComponent(f.dataset.name)}`, body);
      else await api("POST", `/api/extensions/${kindPath}`, body);
      state.extEdit = null;
      await Promise.all([loadExt(), loadSettings()]);
      repaintSheet();
      toast(isMcp ? "保存好了，子 agent 下一次就能用" : "保存好了，子 agent 下一次就能看到");
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
      toast("保存好了");
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
      err.textContent = "挑一个理由，或者写一句。";
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
      toast("收到，下一轮找资讯会参考");
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
      err.textContent = ed.kind === "focus" ? "填一个 QQ 号。" : "内容不能是空的。";
      err.hidden = false;
      return;
    }
    btn.disabled = true;
    try {
      const g = grp();
      if (ed.kind === "focus") {
        if (!/^\d{5,12}$/.test(text)) throw new Error("QQ 号应该是 5 到 12 位数字。");
        await api("POST", `/api/groups/${encodeURIComponent(g.id)}/focus`, { user_id: text, action: "add" });
      } else if (ed.id) {
        await api("PATCH", `/api/profile/${ed.id}`, { text });
      } else {
        await api("POST", `/api/groups/${encodeURIComponent(g.id)}/profile`, { category: ed.cat, text });
      }
      closeSheet();
      await loadView(true);
      renderView();
      toast(ed.kind === "focus" ? "加上了" : "保存好了，这条已锁定");
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
