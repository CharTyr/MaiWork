// MaiWork 网页 · 主题：全站默认（总管理员定）+ 个人覆盖（只存在本浏览器）。
// 主题只换外观（style.css 的 :root[data-theme=…]）；默认主题不设属性，外观与原来一致。
// 首屏防闪在 index.html <head> 的内联脚本里，这里负责拿服务器设置、切换和「外观」抽屉。
import { DEFAULT_KEY, THEMES, THEME_KEY, resolveTheme, themeName } from "./theme_core.js";
import { hasSounds, initSound, setSoundPack, setSoundPref, soundPref } from "./sound.js";
import { api } from "./api.js";
import { esc, tip, toast } from "./util.js";
import { openSheet, repaintSheet } from "./sheet.js";

export const theme = { info: null }; // GET /api/theme 的返回

const lsGet = (k) => {
  try {
    return localStorage.getItem(k) || "";
  } catch (e) {
    return "";
  }
};
const lsSet = (k, v) => {
  try {
    if (v) localStorage.setItem(k, v);
    else localStorage.removeItem(k);
  } catch (e) {}
};

export const personalTheme = () => lsGet(THEME_KEY);
export const serverDefault = () => (theme.info && theme.info.default) || lsGet(DEFAULT_KEY) || "default";
export const currentTheme = () => resolveTheme(personalTheme(), serverDefault());

function soundsFor(id) {
  const s = theme.info && theme.info.sounds && theme.info.sounds[id];
  return id !== "default" && s && s.files && Object.keys(s.files).length ? s.files : null;
}

export function applyTheme() {
  const id = currentTheme();
  const root = document.documentElement;
  if (id === "default") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", id);
  setSoundPack(soundsFor(id));
}

let started = false;
export async function loadTheme() {
  if (!started) {
    started = true;
    initSound();
  }
  try {
    theme.info = await api("GET", "/api/theme");
    lsSet(DEFAULT_KEY, theme.info && theme.info.default !== "default" ? theme.info.default : "");
  } catch (e) {
    /* 拿不到就用上次缓存的默认值，外观照常 */
  }
  applyTheme();
}

/* ───────────── 「外观」抽屉（所有身份） ───────────── */

export function appearanceSheet() {
  const mine = personalTheme();
  const def = serverDefault();
  const cur = currentTheme();
  const opt = (id, label, sub) => `
    <button class="gpick" data-act="theme-pick" data-theme="${esc(id)}" aria-pressed="${mine === id}">
      <span class="theme-swatch" data-swatch="${esc(id || def)}" aria-hidden="true"></span>
      <span class="gpick-t"><span class="gpick-name" style="display:block">${esc(label)}</span>${sub ? `<span class="gpick-sub">${esc(sub)}</span>` : ""}</span>
      <span class="theme-check">${mine === id ? "已选" : ""}</span>
    </button>`;
  const snd = hasSounds();
  return `
    <h1 class="h-page">外观</h1>
    <div class="h-meta sheet-lead">只影响本浏览器${tip("全站默认主题由管理员设置；在这里选择后，本浏览器不再跟随默认。")}</div>
    <h2 class="h-sub">主题</h2>
    ${opt("", "跟随默认", `当前默认：${themeName(def)}`)}
    ${THEMES.map((t) => opt(t.id, t.name, "")).join("")}
    <h2 class="h-sub">音效</h2>
    ${
      snd
        ? `<div class="set-row"><div><div class="set-name">按钮音效</div><div class="set-text">${soundPref() ? "已开启" : "已关闭"}</div></div>
           <span class="row-btns"><button class="btn small" data-act="sound-toggle" aria-pressed="${soundPref()}">${soundPref() ? "关闭" : "开启"}</button></span></div>`
        : `<p class="h-meta">「${esc(themeName(cur))}」主题无音效</p>`
    }`;
}

// act() 先问这里；处理了返回 true
export async function actTheme(a, el) {
  const repaint = repaintSheet;
  if (a === "appearance") {
    openSheet("appearance");
    return true;
  }
  if (a === "theme-pick") {
    lsSet(THEME_KEY, el.dataset.theme || "");
    applyTheme();
    repaint();
    toast(`已切换为「${themeName(currentTheme())}」`);
    return true;
  }
  if (a === "sound-toggle") {
    setSoundPref(!soundPref());
    repaint();
    toast(soundPref() ? "已开启音效" : "已关闭音效");
    return true;
  }
  return false;
}
