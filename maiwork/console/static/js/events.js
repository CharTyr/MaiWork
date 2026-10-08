// MaiWork 网页 · 表单输入类事件：change / input / keydown，长名字滚动。
import { $, desktop, state } from "./state.js";
import { toast } from "./util.js";
import { api } from "./api.js";
import { loadExt } from "./settings/ext.js";
import { chipAdd } from "./settings/rules.js";
import { loadUsageDay } from "./settings/usage.js";
import { avatarSaved } from "./settings/identity.js";
import { loadLogs } from "./settings/logs.js";
import { repaintSheet } from "./sheet.js";
import { loadChats } from "./chat.js";
import { renderSide, renderView } from "./render.js";

document.addEventListener("change", async (e) => {
  if (e.target.classList && e.target.classList.contains("chat-group") && state.chat) {
    try {
      await api("PATCH", `/api/chat/${encodeURIComponent(state.chat.id)}`, { group_id: e.target.value });
      if (state.chat.info) state.chat.info.group_id = e.target.value;
      await loadChats();
      renderSide();
      toast(e.target.value ? "已切到本群" : "所有群");
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
    if (file.size > 2 * 1024 * 1024) return toast("图片太大，最多 2MB", true);
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return toast("只支持 png、jpg、webp、gif", true);
    try {
      const data = await new Promise((ok, bad) => {
        const r = new FileReader();
        r.onload = () => ok(String(r.result).split(",")[1] || "");
        r.onerror = () => bad(new Error("打不开这张图"));
        r.readAsDataURL(file);
      });
      await avatarSaved(await api("POST", "/api/settings/avatar", { data, mime: file.type }));
      toast("头像换好了");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (e.target.id !== "skill-zip") return;
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) return toast("文件太大，最多 5MB", true);
  const upload = async (replace) => {
    const res = await fetch(`/api/extensions/skills/upload${replace ? "?replace=1" : ""}`, {
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
  toast("安装中…");
  try {
    let { res, data } = await upload(false);
    if (res.status === 409 && confirm(`${(data && data.error) || "已有同名 skill"}，替换吗？`)) ({ res, data } = await upload(true));
    if (!res.ok) throw new Error((data && data.error) || `出错了（${res.status}）`);
    await loadExt();
    repaintSheet();
    toast(`已装好 ${data.name}，下次可用`);
  } catch (err) {
    toast(err.message, true);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.target.id === "chat-input" && e.key === "Enter" && !e.shiftKey && !e.isComposing && desktop.matches) {
    e.preventDefault();
    $("chat-form").requestSubmit();
    return;
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id !== "chat-input") return;
  e.target.style.height = "auto";
  e.target.style.height = `${Math.min(e.target.scrollHeight, 220)}px`;
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.target.closest) return;
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
