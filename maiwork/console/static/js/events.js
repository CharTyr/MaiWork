// MaiWork 网页 · 表单输入类事件：change / input / keydown，长名字滚动。
import { $, state } from "./state.js";
import { toast } from "./util.js";
import { api } from "./api.js";
import { confirmExecTools, loadExt } from "./settings/ext.js";
import { chipAdd } from "./settings/rules.js";
import { loadUsageDay } from "./settings/usage.js";
import { avatarSaved } from "./settings/identity.js";
import { loadLogs } from "./settings/logs.js";
import { repaintSheet } from "./sheet.js";
import { refreshDetail } from "./detail.js";
import { loadChats } from "./chat.js";
import { renderSide, renderView } from "./render.js";

document.addEventListener("change", async (e) => {
  if (e.target.classList && e.target.classList.contains("chat-group") && state.chat) {
    try {
      await api("PATCH", `/api/chat/${encodeURIComponent(state.chat.id)}`, { group_id: e.target.value });
      if (state.chat.info) state.chat.info.group_id = e.target.value;
      await loadChats();
      renderSide();
      toast(e.target.value ? "已切换到本群" : "已切换到所有群");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (e.target.classList && e.target.classList.contains("u-date") && state.usage) {
    const day = e.target.value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    loadUsageDay(day).then(repaintSheet);
    repaintSheet();
    return;
  }
  if (e.target.dataset && e.target.dataset.act === "log-failed") {
    state.logs.failed = e.target.checked;
    state.logs.open = {};
    await loadLogs();
    repaintSheet();
    return;
  }
  if (e.target.id === "avatar-file") {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) return toast("图片过大，最多 2MB", true);
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return toast("仅支持 png、jpg、webp、gif", true);
    try {
      const data = await new Promise((ok, bad) => {
        const r = new FileReader();
        r.onload = () => ok(String(r.result).split(",")[1] || "");
        r.onerror = () => bad(new Error("无法打开该图片"));
        r.readAsDataURL(file);
      });
      await avatarSaved(await api("POST", "/api/settings/avatar", { data, mime: file.type }));
      toast("头像已更新");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (e.target.id !== "skill-zip") return;
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) return toast("文件过大，最多 5MB", true);
  const upload = async (replace, confirmExec) => {
    const q = [replace ? "replace=1" : "", confirmExec ? "confirm_exec=1" : ""].filter(Boolean).join("&");
    const res = await fetch(`/api/extensions/skills/upload${q ? `?${q}` : ""}`, {
      method: "POST",
      headers: { "Content-Type": "application/zip", "X-Filename": encodeURIComponent(file.name) },
      body: file,
      credentials: "same-origin",
    });
    let data = null;
    try {
      data = await res.json();
    } catch (x) {}
    return { res, data };
  };
  toast("正在安装…");
  try {
    let replace = false;
    let { res, data } = await upload(false, false);
    if (res.status === 409 && confirm(`${(data && data.error) || "已有同名 skill"}，是否替换？`)) {
      replace = true;
      ({ res, data } = await upload(true, false));
    }
    // 包里的 skill 带执行类工具：服务端要求确认，确认后带 confirm_exec=1 重传
    const execMsg = (data && data.error) || "";
    if (res.status === 400 && /执行类工具/.test(execMsg) && confirmExecTools({ message: execMsg })) ({ res, data } = await upload(replace, true));
    if (!res.ok) throw new Error((data && data.error) || `请求失败（${res.status}）`);
    await loadExt();
    repaintSheet();
    toast(`已安装 ${data.name}，下次运行时可用`);
  } catch (err) {
    toast(err.message, true);
  }
});
document.addEventListener("keydown", (e) => {
  // 管理员对话输入框：回车换行，Ctrl / Cmd + 回车发送（2026-10-30 用户定）
  if (e.target && e.target.id === "chat-input" && e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) {
    e.preventDefault();
    $("chat-form").requestSubmit();
    return;
  }
});
document.addEventListener("input", (e) => {
  if (!e.target || e.target.id !== "chat-input") return;
  e.target.style.height = "auto";
  e.target.style.height = `${Math.min(e.target.scrollHeight, 220)}px`;
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.target || !e.target.closest) return;
  const box = e.target.closest(".chips");
  if (!box || !e.target.closest(".ci-add")) return;
  e.preventDefault();
  chipAdd(box);
});
document.addEventListener("input", (e) => {
  if (!e.target.classList || !e.target.classList.contains("id-text")) return;
  const n = new Blob([e.target.value]).size;
  const lim = Number(e.target.dataset.limit) || 16384;
  const c = e.target.closest(".id-form").querySelector(".id-count");
  c.textContent = `${n} / ${lim} 字节`;
  c.classList.toggle("bad-t", n > lim);
});

/* 「ⓘ」说明提示、气泡菜单与原生 <details> 展开：
   - tip 同时只开一个，紧接着切到下一个时直接出现不重播动画；靠右时往左挪，不出屏
   - 点外面或按 Esc 关闭 tip、分栏菜单（section-picker）和构想「更多」菜单（idea-menu），不连带误关抽屉
   - 普通 <details> 只在用户点击展开时加 .reveal，轮询重绘已展开的节点不重播 */
const openTips = () => document.querySelectorAll("details.tip[open]");
const openPickers = () => document.querySelectorAll("details.section-picker[open]");
document.addEventListener(
  "click",
  (e) => {
    const sum = e.target && e.target.closest && e.target.closest("summary");
    const d = sum && sum.closest && sum.closest("details");
    if (d && d.dataset) d.dataset.userToggle = "1";
  },
  true
);
document.addEventListener(
  "toggle",
  (e) => {
    const d = e.target;
    if (!d || !d.matches) return;
    if (d.matches("details.tip")) {
      const pop = d.querySelector(".tip-pop");
      if (!d.open) {
        if (pop && pop.dataset) delete pop.dataset.instant;
        return;
      }
      const warm = [...openTips()].some((x) => x !== d);
      for (const x of openTips()) if (x !== d) x.open = false;
      if (!pop) return;
      if (pop.dataset) pop.dataset.instant = "1";
      pop.style.removeProperty("--tip-shift");
      const r = pop.getBoundingClientRect();
      const over = r.right - (window.innerWidth - 12);
      const under = 12 - r.left;
      if (over > 0) pop.style.setProperty("--tip-shift", `${-Math.ceil(over)}px`);
      else if (under > 0) pop.style.setProperty("--tip-shift", `${Math.ceil(under)}px`);
      if (!warm && pop.dataset) delete pop.dataset.instant;
      return;
    }
    if (d.matches("details") && !d.matches(".section-picker")) {
      const byUser = !!(d.dataset && d.dataset.userToggle === "1");
      if (d.dataset) delete d.dataset.userToggle;
      for (const ch of d.children || []) {
        if (ch.tagName === "SUMMARY" || !ch.classList) continue;
        if (d.open && byUser) ch.classList.add("reveal");
        else if (!d.open) ch.classList.remove("reveal");
      }
    }
  },
  true
);
document.addEventListener("click", (e) => {
  const insideTip = e.target.closest && e.target.closest("details.tip");
  for (const x of openTips()) if (x !== insideTip) x.open = false;
  const insidePicker = e.target.closest && e.target.closest("details.section-picker");
  for (const x of openPickers()) if (x !== insidePicker) x.open = false;
  if (state.ideaMore != null && !(e.target.closest && e.target.closest('.idea-menu, [data-act="idea-more"]'))) {
    state.ideaMore = null;
    refreshDetail();
  }
});
document.addEventListener(
  "keydown",
  (e) => {
    if (e.key !== "Escape") return;
    const tips = [...openTips()];
    if (tips.length) {
      e.stopImmediatePropagation(); // 先关提示，不要连带关掉抽屉
      for (const x of tips) x.open = false;
      const s = tips[0].querySelector("summary");
      if (s) s.focus();
      return;
    }
    const pickers = [...openPickers()];
    if (pickers.length) {
      e.stopImmediatePropagation();
      for (const x of pickers) x.open = false;
      const s = pickers[0].querySelector("summary");
      if (s) s.focus();
      return;
    }
    if (state.ideaMore != null) {
      e.stopImmediatePropagation();
      state.ideaMore = null;
      refreshDetail();
      const btn = document.querySelector(".idea-more");
      if (btn) btn.focus();
    }
  },
  true
);

/* 长名字滚动：只在真的放不下时才动；尊重「减少动态效果」 */
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
function mqStart(el) {
  const inner = el.firstElementChild;
  if (!inner || reduceMotion.matches) return;
  const d = inner.scrollWidth - el.clientWidth;
  if (d <= 2) return;
  el.style.setProperty("--mq-d", `${-d}px`);
  el.style.setProperty("--mq-t", `${Math.max(1.6, d / 45).toFixed(2)}s`);
  el.classList.add("mq-on");
}
const mqStop = (el) => el.classList.remove("mq-on");
document.addEventListener("pointerover", (e) => {
  if (e.pointerType !== "mouse") return;
  const el = e.target.closest && e.target.closest(".mq");
  if (el && !el.contains(e.relatedTarget)) mqStart(el);
});
document.addEventListener("pointerout", (e) => {
  if (e.pointerType !== "mouse") return;
  const el = e.target.closest && e.target.closest(".mq");
  if (el && !el.contains(e.relatedTarget)) mqStop(el);
});
document.addEventListener(
  "touchstart",
  (e) => {
    const host = e.target.closest && e.target.closest(".r-item, .gpick, .chip, .rail-label");
    const el = host && host.querySelector(".mq");
    if (!el) return;
    mqStart(el);
    clearTimeout(el._mqT);
    el._mqT = setTimeout(() => mqStop(el), 4200);
  },
  { passive: true }
);
