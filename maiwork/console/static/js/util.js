// MaiWork 网页 · 小工具：转义、图标、提示条、时间格式。
import { $, state } from "./state.js";
import { sfx } from "./sound.js";

/* ───────────── 小工具 ───────────── */

export const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "#");
// 成品可走带群访问码的本地路径；拒绝协议相对地址、控制字符和路径跳转。
export function safeAssetUrl(u) {
  const s = String(u || "");
  if (!s || /[\x00-\x20\x7f\\]/.test(s)) return "#";
  try {
    if (/^https?:\/\//i.test(s)) { new URL(s); return esc(s); }
    const path = decodeURIComponent(s.split(/[?#]/)[0]);
    if (!/^\/(?:g\/|api\/tasks\/)/.test(path) || /[\\\x00-\x20\x7f]/.test(path)) return "#";
    if (path.split("/").some((p) => p === "." || p === "..")) return "#";
    return esc(s);
  } catch (_) { return "#"; }
}
// 正文里的 [文字](https://…) 渲染成链接；其余一律转义（内容来自网上和模型，不能信）
export const richText = (t) =>
  esc(t)
    .replace(/\[([^\]\n]{1,80})\]\((https?:\/\/[^\s)]+)\)/g, (_, txt, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`)
    .replace(/`([^`\n]{1,120})`/g, (_, code) => `<code class="icode">${code}</code>`);

const P = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"';
export const SVG = {
  news: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect class="n-page" x="5" y="3.5" width="14" height="17" rx="3.2"/><path class="n-l1" pathLength="1" d="M9 8.5h6"/><path class="n-l2" pathLength="1" d="M9 12h6"/><path class="n-l3" pathLength="1" d="M9 15.5h3.5"/></svg>`,
  ideas: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path class="i-base" d="M9.4 18.2h5.2M10.3 21h3.4"/><path class="i-bulb" d="M12 3.8a5.6 5.6 0 0 0-3.5 10c.55.45.9 1.1.9 1.8V16h5.2v-.4c0-.7.35-1.35.9-1.8A5.6 5.6 0 0 0 12 3.8z"/><g class="i-rays" stroke-width="1.9"><path d="M12 1v1.4"/><path d="M4.5 5l1.2.85"/><path d="M19.5 5l-1.2.85"/></g></svg>`,
  goals: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M8 12.3l2.8 2.8 5.3-5.6"/></svg>`,
  tasks: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path class="t-handle" d="M9 7V5.6A1.6 1.6 0 0 1 10.6 4h2.8A1.6 1.6 0 0 1 15 5.6V7"/><rect class="t-box" x="3.5" y="7" width="17" height="12.5" rx="3.2"/><path class="t-flap" d="M3.5 12.2h6.4M14.1 12.2h6.4"/><rect class="t-latch" x="9.9" y="10.8" width="4.2" height="2.8" rx="0.9"/></svg>`,
  group: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle class="g-p1" cx="7.4" cy="7.4" r="3.3"/><path class="g-p2" d="M16.6 4.2l3.4 5.8h-6.8z"/><rect class="g-p3" x="4.1" y="13.3" width="6.6" height="6.6" rx="1.8"/><circle class="g-p4" cx="16.6" cy="16.6" r="3.3"/></svg>`,
  sliders: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M3.5 8h17M3.5 16h17" stroke-opacity="0.32"/><circle class="s-k1" cx="15" cy="8" r="2.6" fill="var(--card)"/><circle class="s-k2" cx="9" cy="16" r="2.6" fill="var(--card)"/></svg>`,
  down: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.4"><path d="M6 9l6 6 6-6"/></svg>`,
  left: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M15 6l-6 6 6 6"/></svg>`,
  right: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M9 6l6 6-6 6"/></svg>`,
  close: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
  check: `<svg viewBox="0 0 24 24" ${P} stroke-width="3"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
  copy: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>`,
  more: `<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5.5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="18.5" cy="12" r="1.8"/></svg>`,
  up: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9"><path d="M7 11v9H4.5v-9zM7 11l3.8-7c1.3 0 2.2.9 2.2 2.2V9.5h5a2 2 0 0 1 2 2.3l-1.1 6.4a2.2 2.2 0 0 1-2.2 1.8H7"/></svg>`,
  dn: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9" style="transform:rotate(180deg)"><path d="M7 11v9H4.5v-9zM7 11l3.8-7c1.3 0 2.2.9 2.2 2.2V9.5h5a2 2 0 0 1 2 2.3l-1.1 6.4a2.2 2.2 0 0 1-2.2 1.8H7"/></svg>`,
  lock: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3"/></svg>`,
  key: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l2.5 2.5M14 9l2 2"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M4.5 7h15M10 7V5h4v2M6.8 7l.9 12.2h8.6l.9-12.2"/></svg>`,
  pin: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M9 4.5h6M10 4.5v5L7 13.5h10L14 9.5v-5M12 13.5v6"/></svg>`,
  archive: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="5" width="17" height="4" rx="1.5"/><path d="M5.5 9v9a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5V9M10 13h4"/></svg>`,
  pen: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M14.5 5.5l4 4M4.5 19.5l1-4.5L16 4.5a1.4 1.4 0 0 1 2 0l1.5 1.5a1.4 1.4 0 0 1 0 2L9 18.5z"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>`,
  send: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/></svg>`,
  board: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="4" width="17" height="16" rx="3.5"/><path d="M9.5 4v16M14.5 4v16"/><path class="b-c1" d="M5.8 8.5h1.6"/><path class="b-c2" d="M11.2 8.5h1.6"/><path class="b-c3" d="M16.6 8.5h1.6"/><path class="b-c4" d="M11.2 12.2h1.6"/></svg>`,
  chat: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9"><path class="c-bub" d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4.5 19.5l1.3-4A7.5 7.5 0 1 1 20 11.5z"/><circle class="c-d1" cx="9" cy="11.5" r="0.95" fill="currentColor" stroke="none"/><circle class="c-d2" cx="12" cy="11.5" r="0.95" fill="currentColor" stroke="none"/><circle class="c-d3" cx="15" cy="11.5" r="0.95" fill="currentColor" stroke="none"/></svg>`,
  ext: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M14 4h6v6M20 4l-9 9M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>`,
  palette: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M12 3.5a8.5 8.5 0 0 0 0 17c1.2 0 1.8-.8 1.8-1.7 0-1.2-1-1.6-1-2.6 0-1 .8-1.7 1.8-1.7h2.2a3.7 3.7 0 0 0 3.7-3.7C20.5 6.9 16.7 3.5 12 3.5z"/><circle class="p-d1" cx="7.8" cy="11" r="1.05" fill="currentColor" stroke="none"/><circle class="p-d2" cx="10.5" cy="7.3" r="1.05" fill="currentColor" stroke="none"/><circle class="p-d3" cx="15" cy="7.6" r="1.05" fill="currentColor" stroke="none"/></svg>`,
  info: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.2"/><circle cx="12" cy="7.8" r="0.6" fill="currentColor"/></svg>`,
};

// 「ⓘ」说明提示：说明文字默认收起，点图标才展开（design.md §8.5）。
// html 由调用方负责：动态内容先 esc()，这里不再转义（说明里可以有 <b>、链接）。
// 开关靠原生 <details>；点外面 / Esc 关闭、只开一个、防出屏在 events.js。
export const tip = (html, label = "说明") =>
  html ? `<details class="tip"><summary aria-label="${esc(label)}" title="${esc(label)}">${SVG.info}</summary><div class="tip-pop" role="note">${html}</div></details>` : "";
// 表单标签 + 说明：提示放在 <label> 外面，点图标不会把焦点抢到输入框
export const tipLabel = (forId, text, html) =>
  html ? `<div class="lbl-row"><label for="${esc(forId)}">${text}</label>${tip(html)}</div>` : `<label for="${esc(forId)}">${text}</label>`;

const ICONS = new Set("robot newspaper bulb monitor chart alarm moon speech tools package books filebox testtube palette seedling hourglass memo camera rocket joystick sparkles magnifier floppy link bell calendar dumpling bullseye flag pushpin lotus sleeping herb gear lock cloud teacup mailbox fish".split(" "));
// 群头像：有 avatar 就盖一张真图在 emoji 上，图挂了就删掉露出 emoji
export const gface = (g) => `<span class="gav" data-avatar="${esc(g.avatar || "")}">${ico(g.icon)}${g.avatar ? `<img class="gav-img" src="${esc(g.avatar)}" alt="" onerror="this.remove()" />` : ""}</span>`;
export const ico = (name, cls = "ico") => `<img class="${cls}" src="/static/assets/icons/${ICONS.has(name) ? name : "sparkles"}.png" alt="" loading="lazy" />`;
const enterAttr = (i) => `class="enter" style="--i:${i}"`;

export const calm = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
// 刚展开的那一块淡入；只加在这一次点开的元素上，定时刷新重画时不会再播
export function reveal(headSel) {
  const h = document.querySelector(headSel);
  const n = h && h.nextElementSibling;
  if (n && h.getAttribute("aria-expanded") === "true") n.classList.add("reveal");
}

const _copyOrig = new WeakMap();
const _copyTimer = new WeakMap();
// 复制按钮原地变形为「已复制」勾号，1.8 秒后还原；连点不覆盖原始文字。
export function flashCopied(el, label = "已复制") {
  if (!el) return;
  if (!_copyOrig.has(el)) _copyOrig.set(el, el.innerHTML);
  if (!_copyTimer.has(el) && el.offsetWidth > 0 && el.style) el.style.minWidth = `${el.offsetWidth}px`;
  clearTimeout(_copyTimer.get(el));
  const isLink = el.classList && el.classList.contains("link-btn");
  if (isLink) el.textContent = label;
  else el.innerHTML = `${SVG.check}${esc(label)}`;
  if (el.classList) el.classList.add("is-copied");
  const morph = (elNode) => {
    if (!elNode || !elNode.animate) return;
    elNode.animate(
      calm()
        ? [{ opacity: 0.7 }, { opacity: 1 }]
        : [{ opacity: 0.65, filter: "blur(2px)", transform: "scale(0.97)" }, { opacity: 1, filter: "blur(0px)", transform: "scale(1)" }],
      { duration: calm() ? 150 : 180, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }
    );
  };
  morph(el);
  _copyTimer.set(
    el,
    setTimeout(() => {
      const orig = _copyOrig.get(el);
      _copyOrig.delete(el);
      _copyTimer.delete(el);
      if (el.isConnected === false || orig === undefined) return;
      el.innerHTML = orig;
      if (el.style) el.style.minWidth = "";
      if (el.classList) el.classList.remove("is-copied");
      morph(el);
    }, 1800)
  );
}

const _FLIP_SEL = ":scope > .item, :scope > .h-sub, :scope > .filters, :scope > div > .row, :scope > .cm, :scope > .cm-pending, :scope > .cm-tool, :scope > .cm-summary";
const _flipSig = (n) => (n.dataset && (n.dataset.id || n.dataset.entry)) || `${n.tagName}:${n.className}:${String(n.textContent || "").slice(0, 32)}`;
// 待批/待确认卡片处理后先轻收退出，再用纯 transform FLIP 让下方内容平滑上移（不动画 height）。
export async function flipRemove(card, container, runRender) {
  if (!card || card.isConnected === false || calm() || !card.animate) {
    runRender();
    return;
  }
  try {
    const exit = card.animate(
      [{ opacity: 1, transform: "scale(1)" }, { opacity: 0, transform: "scale(0.97)" }],
      { duration: 160, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)", fill: "forwards" }
    );
    if (exit && exit.finished) await exit.finished;
  } catch (_) {}
  const firstTop = new Map();
  if (container && container.querySelectorAll) {
    for (const item of container.querySelectorAll(_FLIP_SEL)) {
      if (item !== card && item.getBoundingClientRect) firstTop.set(_flipSig(item), item.getBoundingClientRect().top);
    }
  }
  runRender();
  if (!firstTop.size || !container || !container.querySelectorAll) return;
  for (const item of container.querySelectorAll(_FLIP_SEL)) {
    const prev = firstTop.get(_flipSig(item));
    if (prev === undefined || !item.getBoundingClientRect || !item.animate) continue;
    const dy = prev - item.getBoundingClientRect().top;
    if (Math.abs(dy) > 2 && Math.abs(dy) < 600) {
      item.animate([{ transform: `translateY(${dy}px)` }, { transform: "translateY(0)" }], {
        duration: 220,
        easing: "cubic-bezier(0.32, 0.72, 0, 1)",
      });
    }
  }
}
export function toast(text, bad) {
  const t = $("toast");
  const was = t.classList.contains("on");
  t.textContent = text;
  t.classList.toggle("bad", !!bad);
  t.classList.add("on");
  // 上一条还没消失又来一条：轻轻弹一下，让人看出换了
  if (was && t.animate)
    t.animate(
      calm() ? [{ opacity: 0.55 }, { opacity: 1 }] : [{ transform: "translate(-50%, 0) scale(0.96)" }, { transform: "translate(-50%, 0) scale(1)" }],
      { duration: 160, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }
    );
  if (!/^正在/.test(String(text || ""))) sfx(bad ? "error" : "success"); // 「正在…」不是结果，不出声
  clearTimeout(toast.timer);
  // 错误按长度多留一会（至少 6 秒，长的最多 15 秒），点一下就关；成功提示照旧 2.4 秒
  const ms = bad ? Math.min(15000, Math.max(6000, String(text || "").length * 90)) : 2400;
  toast.timer = setTimeout(() => t.classList.remove("on"), ms);
  if (!t.dataset.tapClose) {
    t.dataset.tapClose = "1";
    t.addEventListener("click", () => {
      if (window.getSelection && String(window.getSelection())) return; // 正在选字复制，别关
      clearTimeout(toast.timer);
      t.classList.remove("on");
    });
  }
}

/* ───────────── 时间（一律按北京时间显示） ───────────── */

export const now = () => Date.now() / 1000 + state.skew;
export const BJ = 8 * 3600;
const bjDate = (ts) => new Date((ts + BJ) * 1000); // 用 getUTC* 读出北京时间
export const pad = (n) => String(n).padStart(2, "0");
export const hhmm = (ts) => {
  const d = bjDate(ts);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
};
export const dayIndex = (ts) => Math.floor((ts + BJ) / 86400);
const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
export function dayWord(ts) {
  const diff = dayIndex(ts) - dayIndex(now());
  if (diff === 0) return "今天";
  if (diff === -1) return "昨天";
  if (diff === 1) return "明天";
  if (diff === 2) return "后天";
  if (diff > -7 && diff < 0) return WEEK[bjDate(ts).getUTCDay()];
  const d = bjDate(ts);
  return `${d.getUTCMonth() + 1} 月 ${d.getUTCDate()} 日`;
}
export const when = (ts) => (ts ? (dayIndex(ts) === dayIndex(now()) ? hhmm(ts) : `${dayWord(ts)} ${hhmm(ts)}`) : "");
export function dur(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return "不到 1 分钟";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 && h < 6 ? `${h} 小时 ${m % 60} 分钟` : `${h} 小时`;
  return `${Math.floor(h / 24)} 天`;
}
export function slotName(ts) {
  const h = bjDate(ts).getUTCHours();
  const part = h < 6 ? "凌晨" : h < 11 ? "早上" : h < 13 ? "中午" : h < 17 ? "午后" : h < 20 ? "傍晚" : "晚上";
  const dw = dayWord(ts);
  return `${dw === "今天" ? "今天" : dw}${part}`;
}
export const msText = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + " 秒" : Math.round(ms) + " 毫秒");
export function tokens(n) {
  n = n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "k";
  return String(n);
}
