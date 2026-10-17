// MaiWork 网页 · 按钮音效（主题绑定了音效包才有声；默认主题永远无声）。
// 只响应本人真实操作：悬停（仅鼠标）、点击、开关抽屉、成功 / 出错提示；后台刷新不出声。
// 浏览器要求先有用户操作才能放声音，所以 AudioContext 在第一次按下 / 按键后才建；
// 没绑定音效包或静音时不发任何请求。失败一律静默。
import { SOUND_KEY } from "./theme_core.js";

const VOLUME = 0.5;
const TARGET = 'button, [data-act], a[href], [role="tab"], summary, label.file-btn';
let files = null; // { kind: url }，当前主题绑定的包；null = 无音效
let ctx = null;
let gain = null;
let buffers = {}; // kind → AudioBuffer
let loadedFor = ""; // 已解码的是哪一套（按 url 拼起来比对）
let lastUser = 0; // 最近一次真实用户操作的时间
let lastHover = 0;
let hoverEl = null;

export function soundPref() {
  try {
    const v = localStorage.getItem(SOUND_KEY);
    if (v === "on" || v === "off") return v === "on";
  } catch (e) {}
  return !(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
}

export function setSoundPref(on) {
  try {
    localStorage.setItem(SOUND_KEY, on ? "on" : "off");
  } catch (e) {}
  if (on) warm();
}

export const hasSounds = () => !!(files && Object.keys(files).length);

// theme.js 换主题 / 拿到服务器设置后调用；传 null 表示当前主题没有音效
export function setSoundPack(map) {
  files = map && typeof map === "object" ? map : null;
  buffers = {};
  loadedFor = "";
  if (ctx && hasSounds() && soundPref()) warm();
}

function warm() {
  if (!hasSounds() || !soundPref()) return;
  try {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      ctx = new AC();
      gain = ctx.createGain();
      gain.gain.value = VOLUME;
      gain.connect(ctx.destination);
    }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
  } catch (e) {
    return;
  }
  const key = JSON.stringify(files);
  if (loadedFor === key) return;
  loadedFor = key;
  const mine = files;
  for (const [kind, url] of Object.entries(mine)) {
    fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject()))
      .then((b) => ctx.decodeAudioData(b))
      .then((buf) => {
        if (files === mine) buffers[kind] = buf;
      })
      .catch(() => {});
  }
}

function play(kind) {
  if (!ctx || !gain || !soundPref()) return;
  const buf = buffers[kind];
  if (!buf) return;
  try {
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(gain);
    src.start();
  } catch (e) {}
}

// 给 sheet.js / util.js 调：只有最近 3 秒内有过真实操作才响，挡住后台刷新引起的提示
export function sfx(kind) {
  if (!hasSounds() || Date.now() - lastUser > 3000) return;
  play(kind);
}

const fineHover = () => window.matchMedia && matchMedia("(hover: hover) and (pointer: fine)").matches;
const target = (el) => (el && el.closest ? el.closest(TARGET) : null);
const usable = (el) => el && !el.disabled && el.getAttribute("aria-disabled") !== "true";

export function initSound() {
  const mark = (e) => {
    if (!e.isTrusted) return;
    lastUser = Date.now();
    warm();
  };
  document.addEventListener("pointerdown", mark, true);
  document.addEventListener("keydown", mark, true);
  document.addEventListener(
    "click",
    (e) => {
      if (!e.isTrusted || !hasSounds()) return;
      lastUser = Date.now();
      const el = target(e.target);
      // 关闭按钮交给 closeSheet 出「close」声，这里不重复
      if (usable(el) && el.dataset.act !== "close") play("click");
    },
    true
  );
  document.addEventListener("pointerover", (e) => {
    if (!e.isTrusted || e.pointerType !== "mouse" || !hasSounds() || !fineHover()) return;
    const el = target(e.target);
    if (!usable(el) || el === hoverEl) return;
    hoverEl = el;
    const t = Date.now();
    if (t - lastHover < 60) return;
    lastHover = t;
    play("hover");
  });
  document.addEventListener("pointerout", (e) => {
    if (hoverEl && !hoverEl.contains(e.relatedTarget)) hoverEl = null;
  });
}

export const SOUND_KINDS = [
  ["hover", "悬停"],
  ["click", "点击"],
  ["open", "打开面板"],
  ["close", "关闭面板"],
  ["success", "成功"],
  ["error", "出错"],
];
