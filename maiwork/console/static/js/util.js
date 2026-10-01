// MaiWork 网页 · 小工具：转义、图标、提示条、时间格式。
import { $, state } from "./state.js";

/* ───────────── 小工具 ───────────── */

export const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "#");
// 正文里的 [文字](https://…) 渲染成链接；其余一律转义（内容来自网上和模型，不能信）
export const richText = (t) =>
  esc(t)
    .replace(/\[([^\]\n]{1,80})\]\((https?:\/\/[^\s)]+)\)/g, (_, txt, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`)
    .replace(/`([^`\n]{1,120})`/g, (_, code) => `<code class="icode">${code}</code>`);

const P = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"';
export const SVG = {
  news: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="5" y="3.5" width="14" height="17" rx="3.2"/><path d="M9 8.5h6M9 12h6M9 15.5h3.5"/></svg>`,
  ideas: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M9.2 18h5.6M10.2 21h3.6"/><path d="M12 3a6 6 0 0 0-3.7 10.7c.6.5 1 1.2 1 2V16h5.4v-.3c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z"/></svg>`,
  goals: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M8 12.3l2.8 2.8 5.3-5.6"/></svg>`,
  tasks: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><rect x="3.5" y="7" width="17" height="12.5" rx="3.2"/><path d="M9 7V5.6A1.6 1.6 0 0 1 10.6 4h2.8A1.6 1.6 0 0 1 15 5.6V7M3.5 12.3h17"/></svg>`,
  group: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><circle cx="7.4" cy="7.4" r="3.3"/><path d="M16.6 4.2l3.4 5.8h-6.8z"/><rect x="4.1" y="13.3" width="6.6" height="6.6" rx="1.8"/><circle cx="16.6" cy="16.6" r="3.3"/></svg>`,
  sliders: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M3.5 8h9M17.5 8h3M3.5 16h3M11.5 16h9"/><circle cx="15" cy="8" r="2.5"/><circle cx="9" cy="16" r="2.5"/></svg>`,
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
  pen: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M14.5 5.5l4 4M4.5 19.5l1-4.5L16 4.5a1.4 1.4 0 0 1 2 0l1.5 1.5a1.4 1.4 0 0 1 0 2L9 18.5z"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>`,
  send: `<svg viewBox="0 0 24 24" ${P} stroke-width="2.2"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/></svg>`,
  chat: `<svg viewBox="0 0 24 24" ${P} stroke-width="1.9"><path d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4.5 19.5l1.3-4A7.5 7.5 0 1 1 20 11.5z"/></svg>`,
  ext: `<svg viewBox="0 0 24 24" ${P} stroke-width="2"><path d="M14 4h6v6M20 4l-9 9M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>`,
};

const ICONS = new Set("robot newspaper bulb monitor chart alarm moon speech tools package books filebox testtube palette seedling hourglass memo camera rocket joystick sparkles magnifier floppy link bell calendar dumpling bullseye flag pushpin lotus sleeping herb gear lock cloud teacup mailbox fish".split(" "));
// 群头像：有 avatar 就盖一张真图在 emoji 上，图挂了就删掉露出 emoji
export const gface = (g) => `<span class="gav">${ico(g.icon)}${g.avatar ? `<img class="gav-img" src="${esc(g.avatar)}" alt="" loading="lazy" onerror="this.remove()" />` : ""}</span>`;
export const ico = (name, cls = "ico") => `<img class="${cls}" src="/static/assets/icons/${ICONS.has(name) ? name : "sparkles"}.png" alt="" loading="lazy" />`;
const enterAttr = (i) => `class="enter" style="--i:${i}"`;

export const calm = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
// 刚展开的那一块淡入；只加在这一次点开的元素上，定时刷新重画时不会再播
export function reveal(headSel) {
  const h = document.querySelector(headSel);
  const n = h && h.nextElementSibling;
  if (n && h.getAttribute("aria-expanded") === "true") n.classList.add("reveal");
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
